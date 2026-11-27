"""HTTP tests for the five review workflows, using an isolated database."""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

temporary = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(temporary.name) / "features.db").as_posix()
os.environ["AUTH_MODE"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import app, engine  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()
    temporary.cleanup()


@pytest.fixture(scope="module")
def demo(client):
    response = client.post("/demo")
    assert response.status_code == 201
    return response.json()


def test_demo_signals_and_batch_timeline(client, demo):
    data = client.get(f"/reviews/{demo['id']}/snapshot").json()
    falling = next(s for s in data["signals"] if s["kind"] == "falling" and s["parameter"] == "Растворение")
    assert len(falling["evidence_ids"]) == 4
    assert falling["points"][0]["value"] > falling["points"][-1]["value"]
    assert any(s["kind"] == "lower_near" and s["parameter"] == "Растворение" for s in data["signals"])
    assert data["out_of_spec"] == 0
    timeline = client.get(f"/reviews/{demo['id']}/batches/A25-006/timeline").json()
    assert {item["category"] for item in timeline["records"]} >= {"batches", "tests", "deviations"}
    assert all(item["data"]["batch_id"] == "A25-006" for item in timeline["records"])
    assert client.get(f"/reviews/{demo['id']}/batches/UNKNOWN/timeline").status_code == 404


def test_year_comparison_uses_same_scope_and_rates(client, demo):
    data = client.get(f"/reviews/{demo['id']}/snapshot").json()
    comparison = data["comparison"]
    assert comparison["previous_year"] == 2024
    assert comparison["previous"]["counts"]["batches"] == 10
    assert comparison["current"]["counts"]["batches"] == 12
    assert comparison["previous"]["rates_per_100_batches"]["deviations"] == 20
    assert comparison["current"]["rates_per_100_batches"]["deviations"] == 8.33
    assert comparison["previous"]["averages"][0]["parameter"] == "Растворение"


def test_finding_keeps_record_evidence_and_can_be_edited(client, demo):
    review_id = demo["id"]
    signal = next(s for s in client.get(f"/reviews/{review_id}/snapshot").json()["signals"]
                  if s["kind"] == "falling" and s["parameter"] == "Растворение")
    created = client.post(f"/reviews/{review_id}/findings", json={"signal_key": signal["key"],
        "text": "Последовательное снижение требует оценки специалистом."})
    assert created.status_code == 201, created.text
    finding_id = created.json()["id"]
    assert client.post(f"/reviews/{review_id}/findings", json={"signal_key": signal["key"],
        "text": "Повторный вывод"}).status_code == 409
    data = client.get(f"/reviews/{review_id}/snapshot").json()
    finding = next(item for item in data["findings"] if item["id"] == finding_id)
    assert [row["id"] for row in finding["evidence"]] == signal["evidence_ids"]
    assert all(row["source"] == "Демонстрационные данные" for row in finding["evidence"])
    edited = client.patch(f"/reviews/{review_id}/findings/{finding_id}", json={"text": "Эксперт подтвердил необходимость расследования."})
    assert edited.status_code == 200
    assert client.patch(f"/reviews/{review_id}/findings/{finding_id}", json={"text": "       "}).status_code == 400
    assert client.get(f"/reviews/{review_id}/report.pdf").content.startswith(b"%PDF")
    assert client.delete(f"/reviews/{review_id}/findings/{finding_id}").status_code == 204
    assert not client.get(f"/reviews/{review_id}/snapshot").json()["findings"]


def test_attestation_is_distinct_from_missing_and_blocks_import(client):
    review = client.post("/reviews", json={"product": "Проверка нулей", "site": "Тестовая площадка", "year": 2026}).json()
    review_id = review["id"]
    assert client.get(f"/reviews/{review_id}/snapshot").json()["comparison"] is None
    assert client.post(f"/reviews/{review_id}/attestations", json={"category": "batches",
        "source": "Реестр", "reviewer": "Специалист"}).status_code == 400
    response = client.post(f"/reviews/{review_id}/attestations", json={"category": "recalls",
        "source": "Журнал QMS", "reviewer": "Иванова"})
    assert response.status_code == 201, response.text
    data = client.get(f"/reviews/{review_id}/snapshot").json()
    assert data["statuses"]["recalls"] == "confirmed_none"
    assert "recalls" not in data["missing"]
    assert data["statuses"]["complaints"] == "missing"
    csv = b"id,date,batch_id,status,description\nR-1,2026-02-01,B-1,Open,Recall\n"
    blocked = client.post(f"/reviews/{review_id}/imports", data={"category": "recalls"},
        files={"file": ("recalls.csv", csv, "text/csv")})
    assert blocked.status_code == 409
    assert client.get(f"/reviews/{review_id}/report.pdf").status_code == 200
    assert client.delete(f"/reviews/{review_id}/attestations/recalls").status_code == 204
    assert client.get(f"/reviews/{review_id}/snapshot").json()["statuses"]["recalls"] == "missing"
    imported = client.post(f"/reviews/{review_id}/imports", data={"category": "recalls"},
        files={"file": ("recalls.csv", csv, "text/csv")})
    assert imported.json()["imported"] == 1
    assert client.post(f"/reviews/{review_id}/attestations", json={"category": "recalls",
        "source": "Журнал", "reviewer": "Иванова"}).status_code == 409


def test_imported_file_is_traceable_and_bad_file_is_atomic(client):
    review = client.post("/reviews", json={"product": "Проверка источника", "site": "Тестовая площадка", "year": 2026}).json()
    review_id = review["id"]
    bad = b"batch_id,date,parameter,result,unit,lower_limit,upper_limit\nB-1,2025-01-01,pH,7,unit,5,9\n"
    response = client.post(f"/reviews/{review_id}/imports", data={"category": "tests"},
        files={"file": ("bad.csv", bad, "text/csv")})
    assert response.json()["imported"] == 0
    assert client.get(f"/reviews/{review_id}/snapshot").json()["total"] == 0
    rows = "batch_id,date,parameter,result,unit,lower_limit,upper_limit\n" + "".join(
        f"B-{i},2026-01-{i:02d},pH,{8-i/4},unit,5,9\n" for i in range(1, 5))
    imported = client.post(f"/reviews/{review_id}/imports", data={"category": "tests"},
        files={"file": ("tests.csv", rows.encode(), "text/csv")})
    assert imported.json()["imported"] == 4
    signal = next(s for s in client.get(f"/reviews/{review_id}/snapshot").json()["signals"] if s["kind"] == "falling")
    finding = client.post(f"/reviews/{review_id}/findings", json={"signal_key": signal["key"],
        "text": "Проверить снижение pH."})
    assert finding.status_code == 201
    evidence = client.get(f"/reviews/{review_id}/snapshot").json()["findings"][0]["evidence"]
    assert all(row["source"] == "tests.csv" for row in evidence)
    assert [row["source_row"] for row in evidence] == [2, 3, 4, 5]
    assert client.get(f"/reviews/{review_id}/report.pdf").status_code == 200


def test_local_roles_profile_source_workflow_and_audit(client, monkeypatch):
    import io
    import json
    from urllib.parse import parse_qs, urlsplit
    from sqlalchemy import select
    from app.main import SessionLocal
    from app import governance

    monkeypatch.setattr(governance.settings, "local_admin_password", "DemoAdmin2026!")
    monkeypatch.setattr(governance.settings, "local_author_password", "DemoAuthor2026!")
    monkeypatch.setattr(governance.settings, "local_reviewer_password", "DemoReviewer2026!")
    monkeypatch.setattr(governance.settings, "source_base_url", "http://localhost:8001")
    monkeypatch.setattr(governance.settings, "source_api_key", "demo-source-key")
    monkeypatch.setattr(governance.settings, "auth_mode", "local")
    governance.seed_local_users()
    headers = {"X-Requested-With": "SASHE"}
    assert client.get("/reviews").status_code == 401

    def login_as(name):
        response = client.post("/auth/login", json={"username": name,
            "password": f"Demo{name.capitalize()}2026!"})
        assert response.status_code == 200, response.text

    login_as("admin")
    profile = client.post("/profiles", headers=headers, json={"product": "Контрактный препарат",
        "site": "Учебный завод", "required_sections": ["batches", "tests"],
        "extra_fields": {"tests": ["method_id"]}, "near_limit_pct": 30})
    assert profile.status_code == 201, profile.text
    assert profile.json()["version"] == 1
    client.post("/auth/logout", headers=headers)

    login_as("author")
    review = client.post("/reviews", headers=headers, json={"product": "Контрактный препарат",
        "site": "Учебный завод", "year": 2026})
    assert review.status_code == 201, review.text
    review_id = review.json()["id"]
    assert client.get(f"/reviews/{review_id}/profile").json()["version"] == 1
    template = client.get(f"/templates/tests?review_id={review_id}").text
    assert "method_id" in template

    def source_reply(request, timeout):
        category = parse_qs(urlsplit(request.full_url).query)["category"][0]
        batches = [f"S-0{i}" for i in range(1, 5)]
        dates = [f"2026-0{i}-15" for i in range(1, 5)]
        if category == "batches":
            rows = [{"batch_id": b, "date": d, "status": "Выпущена", "yield_pct": "98"}
                    for b, d in zip(batches, dates)]
        elif category == "tests":
            rows = [{"batch_id": b, "date": d, "parameter": "Растворение", "result": str(92 - i * 2),
                     "unit": "%", "lower_limit": "80", "upper_limit": "100", "method_id": "M-1"}
                    for i, (b, d) in enumerate(zip(batches, dates))]
        else:
            rows = []
        return io.BytesIO(json.dumps({"export_id": f"export-{category}",
            "count": len(rows), "rows": rows}).encode())

    monkeypatch.setattr(governance, "urlopen", source_reply)
    for category in ("batches", "tests"):
        response = client.post(f"/reviews/{review_id}/sync/{category}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["imported"] == 4
    empty = client.post(f"/reviews/{review_id}/sync/deviations", headers=headers)
    assert empty.status_code == 200 and empty.json()["empty"] is True
    assert client.get(f"/reviews/{review_id}/snapshot").json()["counts"]["deviations"] == 0
    assert client.post(f"/reviews/{review_id}/sync/tests", headers=headers).status_code == 409
    data = client.get(f"/reviews/{review_id}/snapshot").json()
    assert data["missing"] == []
    assert data["profile"]["near_limit_pct"] == 30
    assert any(item["kind"] == "lower_near" for item in data["signals"])
    assert client.patch(f"/reviews/{review_id}", headers=headers,
        json={"conclusion": "Проверить результаты", "actions": "Исследовать тренд"}).status_code == 200
    assert client.post(f"/reviews/{review_id}/submit", headers=headers).status_code == 200
    assert client.patch(f"/reviews/{review_id}", headers=headers,
        json={"conclusion": "Изменение", "actions": ""}).status_code == 409
    client.post("/auth/logout", headers=headers)

    login_as("reviewer")
    assert client.post("/reviews", headers=headers,
        json={"product": "XX", "site": "YY", "year": 2026}).status_code == 403
    checked = client.post(f"/reviews/{review_id}/check", headers=headers)
    assert checked.status_code == 200, checked.text
    released = client.get(f"/reviews/{review_id}/releases/1.pdf")
    assert released.status_code == 200 and released.content.startswith(b"%PDF")
    assert released.headers["x-content-sha256"] == checked.json()["pdf_sha256"]
    assert client.get("/audit/verify").json()["valid"] is True
    with SessionLocal() as session:
        event = session.scalar(select(governance.AuditEvent).order_by(governance.AuditEvent.seq))
        original = event.action
        event.action = "tampered"
        session.commit()
    assert client.get("/audit/verify").json()["valid"] is False
    with SessionLocal() as session:
        event = session.scalar(select(governance.AuditEvent).order_by(governance.AuditEvent.seq))
        event.action = original
        session.commit()
    assert client.get("/audit/verify").json()["valid"] is True
    client.post("/auth/logout", headers=headers)
    login_as("author")
    reopened = client.post(f"/reviews/{review_id}/reopen", headers=headers)
    assert reopened.status_code == 200 and reopened.json()["version"] == 2
    assert client.patch(f"/reviews/{review_id}", headers=headers,
        json={"conclusion": "Уточнённое заключение", "actions": "Повторная проверка"}).status_code == 200
    assert client.post(f"/reviews/{review_id}/submit", headers=headers).status_code == 200
    client.post("/auth/logout", headers=headers)
    login_as("reviewer")
    returned = client.post(f"/reviews/{review_id}/return", headers=headers,
        json={"comment": "Уточнить результаты исследования"})
    assert returned.status_code == 200 and returned.json()["state"] == "returned"
    client.post("/auth/logout", headers=headers)
    login_as("author")
    assert client.patch(f"/reviews/{review_id}", headers=headers,
        json={"conclusion": "После замечания проверяющего", "actions": "Повторная проверка"}).status_code == 200
    assert client.get(f"/reviews/{review_id}/releases/1.pdf").content == released.content


def test_oidc_pkce_callback_checks_claims_and_maps_roles(client, monkeypatch):
    import io
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app import governance
    from app.main import SessionLocal

    monkeypatch.setattr(governance.settings, "auth_mode", "oidc")
    monkeypatch.setattr(governance.settings, "oidc_issuer", "https://id.example.test")
    monkeypatch.setattr(governance.settings, "oidc_client_id", "sashe-test")
    monkeypatch.setattr(governance.settings, "oidc_redirect_uri", "http://localhost:8000/auth/oidc/callback")
    monkeypatch.setattr(governance.settings, "oidc_role_groups", '{"reviewer":["quality-review"]}')
    monkeypatch.setattr(governance, "oidc_metadata", lambda: {
        "authorization_endpoint": "https://id.example.test/authorize",
        "token_endpoint": "https://id.example.test/token", "jwks_uri": "https://id.example.test/jwks"})
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(governance.jwt, "PyJWKClient", lambda _: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private_key.public_key())))
    start = client.get("/auth/oidc/start", follow_redirects=False)
    assert start.status_code == 302
    params = parse_qs(urlsplit(start.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid profile email"]
    state = params["state"][0]
    with SessionLocal() as session:
        attempt = session.get(governance.OidcAttempt, governance.hashlib.sha256(state.encode()).hexdigest())
        nonce = attempt.nonce
    now = datetime.now(timezone.utc)
    token = jwt.encode({"iss": "https://id.example.test", "aud": "sashe-test", "sub": "person-1",
        "nonce": nonce, "groups": ["quality-review"], "iat": now, "exp": now + timedelta(minutes=5)},
        private_key, algorithm="RS256")
    monkeypatch.setattr(governance, "urlopen", lambda request, timeout: io.BytesIO(
        json.dumps({"id_token": token}).encode()))
    callback = client.get(f"/auth/oidc/callback?code=valid&state={state}", follow_redirects=False)
    assert callback.status_code == 303, callback.text
    assert client.get("/auth/me").json()["roles"] == ["reviewer"]
    assert client.get(f"/auth/oidc/callback?code=valid&state={state}").status_code == 401
