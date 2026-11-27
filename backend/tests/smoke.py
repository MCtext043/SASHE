"""API smoke test with a temporary SQLite database; deployment uses PostgreSQL."""
import os
import sys
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

with tempfile.TemporaryDirectory() as temporary:
    os.environ["DATABASE_URL"] = "sqlite:///" + (Path(temporary) / "smoke.db").as_posix()
    os.environ["AUTH_MODE"] = "test"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.main import app, engine

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        demo = client.post("/demo")
        assert demo.status_code == 201, demo.text
        review_id = demo.json()["id"]
        snapshot = client.get(f"/reviews/{review_id}/snapshot").json()
        assert snapshot["counts"]["batches"] == 12
        assert len(snapshot["missing"]) > 0
        pdf = client.get(f"/reviews/{review_id}/report.pdf")
        assert pdf.status_code == 200, pdf.text
        assert pdf.content.startswith(b"%PDF")

        review = client.post("/reviews", json={"product": "Тестовый препарат", "site": "Площадка", "year": 2025}).json()
        review_id = review["id"]
        bad = b"batch_id,date,status,yield_pct\nB-01,2024-01-01,Released,99\n"
        response = client.post(f"/reviews/{review_id}/imports", data={"category": "batches"},
                               files={"file": ("bad.csv", bad, "text/csv")})
        assert response.json()["imported"] == 0
        assert client.get(f"/reviews/{review_id}/snapshot").json()["total"] == 0

        good = b"batch_id,date,status,yield_pct\nB-01,2025-01-01,Released,99\n"
        response = client.post(f"/reviews/{review_id}/imports", data={"category": "batches"},
                               files={"file": ("good.csv", good, "text/csv")})
        assert response.json()["imported"] == 1, response.text
        duplicate = client.post(f"/reviews/{review_id}/imports", data={"category": "batches"},
                                files={"file": ("good.csv", good, "text/csv")})
        assert duplicate.status_code == 409
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["batch_id", "date", "status", "yield_pct"])
        sheet.append(["B-02", datetime(2025, 2, 1), "Released", 98])
        stream = BytesIO()
        workbook.save(stream)
        excel = client.post(f"/reviews/{review_id}/imports", data={"category": "batches"},
                            files={"file": ("batches.xlsx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert excel.json()["imported"] == 1, excel.text
    engine.dispose()
    print("API smoke test passed")
