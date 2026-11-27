"""Clearly synthetic read-only LIMS/QMS endpoint for local integration checks."""
import hashlib
import os

from fastapi import FastAPI, Header, HTTPException, Query


app = FastAPI(title="Учебный источник LIMS/QMS")


@app.get("/health")
def health():
    return {"status": "ok", "mode": "demo"}


@app.get("/exports")
def export(product: str = Query(min_length=2), site: str = Query(min_length=2),
           year: int = Query(ge=2020, le=2100), category: str = Query(),
           authorization: str = Header(default="")):
    if authorization != f"Bearer {os.environ.get('SOURCE_API_KEY', '')}":
        raise HTTPException(401, "Неверный ключ источника")
    if category not in {"batches", "tests", "process"}:
        return {"export_id": f"demo-{category}-{year}", "count": 0, "rows": []}
    scope = hashlib.sha256(f"{product}|{site}|{year}".encode()).hexdigest()[:7].upper()
    batches = [f"{scope}-{year % 100:02d}-{i:02d}" for i in range(1, 5)]
    dates = [f"{year}-{month:02d}-15" for month in (1, 4, 7, 10)]
    if category == "batches":
        rows = [{"batch_id": batch, "date": stamp, "status": "Выпущена", "yield_pct": "98"}
                for batch, stamp in zip(batches, dates)]
    else:
        rows = [{"batch_id": batch, "date": stamp,
                 "parameter": "Растворение" if category == "tests" else "Температура процесса",
                 "result": str(94 - i * 2) if category == "tests" else str(8 + i / 10),
                 "unit": "%" if category == "tests" else "°C",
                 "lower_limit": "80" if category == "tests" else "7",
                 "upper_limit": "100" if category == "tests" else "10"}
                for i, (batch, stamp) in enumerate(zip(batches, dates))]
    return {"export_id": f"demo-{scope}-{year}-{category}", "count": len(rows), "rows": rows}
