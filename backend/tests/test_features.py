"""HTTP tests for the five review workflows, using an isolated database."""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

temporary = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(temporary.name) / "features.db").as_posix()
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
