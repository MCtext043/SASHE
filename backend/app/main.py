from __future__ import annotations

import csv
import hashlib
import io
import os
from xml.sax.saxutils import escape
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from sqlalchemy import Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from .analysis import EVENTS, quality_signals, year_metrics


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://quality:quality@localhost:5432/quality"
    cors_origin: str = "http://localhost:3000"


settings = Settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine)


class Base(DeclarativeBase):
    pass


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("product", "site", "year", name="uq_review_scope"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product: Mapped[str] = mapped_column(String(160))
    site: Mapped[str] = mapped_column(String(160))
    year: Mapped[int] = mapped_column(Integer)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    actions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    records: Mapped[list[QualityRecord]] = relationship(back_populates="review", cascade="all, delete-orphan")
    imports: Mapped[list[ImportRun]] = relationship(back_populates="review", cascade="all, delete-orphan")
    attestations: Mapped[list[SectionAttestation]] = relationship(back_populates="review", cascade="all, delete-orphan")
    findings: Mapped[list[EvidenceFinding]] = relationship(back_populates="review", cascade="all, delete-orphan")


class QualityRecord(Base):
    __tablename__ = "quality_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    category: Mapped[str] = mapped_column(String(40))
    record_date: Mapped[date] = mapped_column(Date)
    batch_id: Mapped[str] = mapped_column(String(80), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    review: Mapped[Review] = relationship(back_populates="records")


class ImportRun(Base):
    __tablename__ = "import_runs"
    __table_args__ = (UniqueConstraint("review_id", "category", "sha256", name="uq_import_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    category: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    review: Mapped[Review] = relationship(back_populates="imports")


class SectionAttestation(Base):
    __tablename__ = "section_attestations"
    __table_args__ = (UniqueConstraint("review_id", "category", name="uq_attestation_category"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    category: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(255))
    reviewer: Mapped[str] = mapped_column(String(160))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    review: Mapped[Review] = relationship(back_populates="attestations")


class EvidenceFinding(Base):
    __tablename__ = "evidence_findings"
    __table_args__ = (UniqueConstraint("review_id", "signal_key", name="uq_finding_signal"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    signal_key: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[int]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    review: Mapped[Review] = relationship(back_populates="findings")


SCHEMAS = {
    "materials": ["id", "date", "batch_id", "status", "description"],
    "packaging": ["id", "date", "batch_id", "status", "description"],
    "batches": ["batch_id", "date", "status", "yield_pct"],
    "process": ["batch_id", "date", "parameter", "result", "unit", "lower_limit", "upper_limit"],
    "tests": ["batch_id", "date", "parameter", "result", "unit", "lower_limit", "upper_limit"],
    "deviations": ["id", "date", "batch_id", "status", "description"],
    "oos": ["id", "date", "batch_id", "status", "description"],
    "changes": ["id", "date", "batch_id", "status", "description"],
    "complaints": ["id", "date", "batch_id", "status", "description"],
    "capa": ["id", "date", "batch_id", "status", "description"],
    "stability": ["batch_id", "date", "parameter", "result", "unit", "lower_limit", "upper_limit"],
    "recalls": ["id", "date", "batch_id", "status", "description"],
    "regulatory": ["id", "date", "batch_id", "status", "description"],
    "validation": ["id", "date", "batch_id", "status", "description"],
    "equipment": ["id", "date", "batch_id", "status", "description"],
    "agreements": ["id", "date", "batch_id", "status", "description"],
}
LABELS = {
    "materials": "Исходное сырьё", "packaging": "Упаковочные материалы",
    "batches": "Произведённые серии", "tests": "Контроль качества", "deviations": "Отклонения",
    "process": "Контроль процесса",
    "oos": "OOS / OOT", "changes": "Изменения", "complaints": "Рекламации",
    "capa": "CAPA", "stability": "Стабильность",
    "recalls": "Отзывы продукции", "regulatory": "Изменения в досье",
    "validation": "Валидация", "equipment": "Оборудование и помещения",
    "agreements": "Технические соглашения",
}


class ReviewCreate(BaseModel):
    product: str = Field(min_length=2, max_length=160)
    site: str = Field(min_length=2, max_length=160)
    year: int = Field(ge=2020, le=2100)


class ReviewUpdate(BaseModel):
    conclusion: str = Field(max_length=10000)
    actions: str = Field(max_length=10000)


class AttestationCreate(BaseModel):
    category: str
    source: str = Field(min_length=3, max_length=255)
    reviewer: str = Field(min_length=2, max_length=160)


class FindingCreate(BaseModel):
    signal_key: str
    text: str = Field(min_length=5, max_length=3000)


class FindingUpdate(BaseModel):
    text: str = Field(min_length=5, max_length=3000)


app = FastAPI(title="Биннофарм · Обзор качества", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=[settings.cors_origin], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)


def db():
    with SessionLocal() as session:
        yield session


def review_or_404(session: Session, review_id: int) -> Review:
    review = session.get(Review, review_id)
    if not review:
        raise HTTPException(404, "Обзор не найден")
    return review


def review_dict(review: Review) -> dict[str, Any]:
    return {"id": review.id, "product": review.product, "site": review.site, "year": review.year,
            "conclusion": review.conclusion, "actions": review.actions}


@app.get("/health")
def health(session: Session = Depends(db)):
    session.execute(select(1))
    return {"status": "ok"}


@app.get("/schemas")
def schemas():
    return {key: {"label": LABELS[key], "columns": value} for key, value in SCHEMAS.items()}


@app.get("/templates/{category}")
def template(category: str):
    if category not in SCHEMAS:
        raise HTTPException(404, "Неизвестный раздел")
    buffer = io.StringIO()
    csv.writer(buffer).writerow(SCHEMAS[category])
    return Response("\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{category}.csv"'})


@app.get("/reviews")
def list_reviews(session: Session = Depends(db)):
    return [review_dict(x) for x in session.scalars(select(Review).order_by(Review.year.desc(), Review.created_at.desc())).all()]


@app.post("/reviews", status_code=201)
def create_review(payload: ReviewCreate, session: Session = Depends(db)):
    existing = session.scalar(select(Review).where(Review.product == payload.product.strip(),
        Review.site == payload.site.strip(), Review.year == payload.year))
    if existing:
        return review_dict(existing)
    review = Review(product=payload.product.strip(), site=payload.site.strip(), year=payload.year)
    session.add(review)
    session.commit()
    session.refresh(review)
    return review_dict(review)


@app.get("/reviews/{review_id}")
def get_review(review_id: int, session: Session = Depends(db)):
    return review_dict(review_or_404(session, review_id))


@app.patch("/reviews/{review_id}")
def update_review(review_id: int, payload: ReviewUpdate, session: Session = Depends(db)):
    review = review_or_404(session, review_id)
    review.conclusion, review.actions = payload.conclusion, payload.actions
    session.commit()
    return review_dict(review)


def parse_file(content: bytes, filename: str) -> list[dict[str, str]]:
    if filename.lower().endswith(".csv"):
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = content.decode("cp1251")
        sample = decoded[:4096]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        return [{str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(io.StringIO(decoded), delimiter=delimiter)]
    if filename.lower().endswith(".xlsx"):
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header = [str(v or "").strip() for v in next(rows, [])]
        return [{header[i]: (v.date().isoformat() if isinstance(v, datetime) else v.isoformat() if isinstance(v, date) else str(v if v is not None else "").strip())
                 for i, v in enumerate(row) if i < len(header)}
                for row in rows if any(v is not None for v in row)]
    raise HTTPException(400, "Поддерживаются CSV и XLSX")


def validate_rows(rows: list[dict[str, str]], category: str, year: int):
    errors: list[str] = []
    columns = SCHEMAS[category]
    if not rows:
        return ["Файл не содержит строк данных"]
    missing = [col for col in columns if col not in rows[0]]
    if missing:
        return ["Не хватает столбцов: " + ", ".join(missing)]
    seen = set()
    for number, row in enumerate(rows, 2):
        for col in columns:
            if col != "batch_id" or category in ("batches", "tests", "stability", "process"):
                if not row.get(col):
                    errors.append(f"Строка {number}: пустое поле {col}")
        try:
            parsed = date.fromisoformat(row.get("date", ""))
            if parsed.year != year:
                errors.append(f"Строка {number}: дата вне {year} года")
        except ValueError:
            errors.append(f"Строка {number}: дата должна быть ГГГГ-ММ-ДД")
        if category not in ("tests", "stability", "process"):
            unique = row.get("batch_id") if category == "batches" else row.get("id")
            if unique in seen:
                errors.append(f"Строка {number}: повторяется идентификатор {unique}")
            seen.add(unique)
        numeric = ["yield_pct"] if category == "batches" else (["result", "lower_limit", "upper_limit"] if category in ("tests", "stability", "process") else [])
        for col in numeric:
            try:
                Decimal(row.get(col, "").replace(",", "."))
            except InvalidOperation:
                errors.append(f"Строка {number}: {col} должно быть числом")
        if category in ("tests", "stability", "process"):
            try:
                lower = Decimal(row["lower_limit"].replace(",", "."))
                upper = Decimal(row["upper_limit"].replace(",", "."))
                if lower > upper:
                    errors.append(f"Строка {number}: нижняя граница выше верхней")
            except (InvalidOperation, KeyError):
                pass
    return errors[:100]


@app.post("/reviews/{review_id}/imports")
async def import_file(review_id: int, category: str = Form(...), file: UploadFile = File(...),
                      session: Session = Depends(db)):
    review = review_or_404(session, review_id)
    if category not in SCHEMAS:
        raise HTTPException(400, "Неизвестный раздел")
    if session.scalar(select(SectionAttestation).where(SectionAttestation.review_id == review_id,
        SectionAttestation.category == category)):
        raise HTTPException(409, "Сначала отзовите подтверждение отсутствия событий в этом разделе")
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(413, "Максимальный размер файла — 5 МБ")
    digest = hashlib.sha256(content).hexdigest()
    if session.scalar(select(ImportRun).where(ImportRun.review_id == review_id,
        ImportRun.category == category, ImportRun.sha256 == digest)):
        raise HTTPException(409, "Этот файл уже загружен в раздел")
    try:
        rows = parse_file(content, file.filename or "")
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, "Не удалось прочитать файл") from exc
    errors = validate_rows(rows, category, review.year)
    if errors:
        return {"imported": 0, "errors": errors}
    if category not in ("tests", "stability", "process"):
        existing = session.scalars(select(QualityRecord).where(QualityRecord.review_id == review_id,
            QualityRecord.category == category)).all()
        known = {r.data.get("batch_id") if category == "batches" else r.data.get("id") for r in existing}
        duplicates = [(r.get("batch_id") if category == "batches" else r.get("id")) for r in rows]
        duplicates = [x for x in duplicates if x in known]
        if duplicates:
            return {"imported": 0, "errors": ["Уже загружены идентификаторы: " + ", ".join(duplicates[:20])]}
    for source_row, row in enumerate(rows, start=2):
        row["_source_file"] = (file.filename or "")[:255]
        row["_source_sha256"] = digest
        row["_source_row"] = source_row
        session.add(QualityRecord(review_id=review_id, category=category,
            record_date=date.fromisoformat(row["date"]), batch_id=row.get("batch_id", ""), data=row))
    session.add(ImportRun(review_id=review_id, category=category, filename=(file.filename or "")[:255],
        sha256=digest, row_count=len(rows)))
    session.commit()
    return {"imported": len(rows), "errors": []}


def numeric(value: str) -> float:
    return float(value.replace(",", "."))


def snapshot(review: Review, session: Session) -> dict[str, Any]:
    records = session.scalars(select(QualityRecord).where(QualityRecord.review_id == review.id)
                              .order_by(QualityRecord.record_date)).all()
    by_category = defaultdict(list)
    for record in records:
        by_category[record.category].append(record)
    counts = {key: len(by_category[key]) for key in SCHEMAS}
    series = []
    out_of_spec = 0
    for category in ("tests", "stability", "process"):
        groups = defaultdict(list)
        for record in by_category[category]:
            data = record.data
            value, lower, upper = numeric(data["result"]), numeric(data["lower_limit"]), numeric(data["upper_limit"])
            if not lower <= value <= upper:
                out_of_spec += 1
            groups[(data["parameter"], data["unit"])].append({"id": record.id, "date": record.record_date.isoformat(), "value": value,
                "lower": lower, "upper": upper, "batch_id": record.batch_id})
        for (parameter, unit), points in groups.items():
            series.append({"category": category, "parameter": parameter, "unit": unit, "points": points})
    imports = session.scalars(select(ImportRun).where(ImportRun.review_id == review.id)
                              .order_by(ImportRun.imported_at.desc())).all()
    attestations = session.scalars(select(SectionAttestation).where(SectionAttestation.review_id == review.id)).all()
    confirmed = {item.category: item for item in attestations}
    statuses = {key: "loaded" if counts[key] else "confirmed_none" if key in confirmed else "missing"
                for key in SCHEMAS}
    source_by_category = defaultdict(list)
    for item in imports:
        source_by_category[item.category].append(item.filename)
    def evidence(record: QualityRecord) -> dict[str, Any]:
        source = record.data.get("_source_file")
        if not source and len(source_by_category[record.category]) == 1:
            source = source_by_category[record.category][0]
        return {"id": record.id, "category": record.category, "date": record.record_date.isoformat(),
                "batch_id": record.batch_id, "source": source or "Источник не установлен",
                "source_row": record.data.get("_source_row"),
                "data": record.data}
    by_id = {r.id: r for r in records}
    findings = session.scalars(select(EvidenceFinding).where(EvidenceFinding.review_id == review.id)
                               .order_by(EvidenceFinding.created_at)).all()
    previous = session.scalar(select(Review).where(Review.product == review.product, Review.site == review.site,
                                                  Review.year == review.year - 1))
    comparison = None
    if previous:
        prior_records = session.scalars(select(QualityRecord).where(QualityRecord.review_id == previous.id)).all()
        comparison = {"previous_year": previous.year, "current": year_metrics(records),
                      "previous": year_metrics(prior_records)}
    return {"review": review_dict(review), "counts": counts,
            "missing": [key for key, status in statuses.items() if status == "missing"],
            "statuses": statuses, "total": len(records), "out_of_spec": out_of_spec, "series": series,
            "signals": quality_signals(records), "comparison": comparison,
            "attestations": [{"category": x.category, "source": x.source, "reviewer": x.reviewer,
                              "confirmed_at": x.confirmed_at.isoformat()} for x in attestations],
            "findings": [{"id": x.id, "signal_key": x.signal_key, "title": x.title, "text": x.text,
                          "evidence": [evidence(by_id[i]) for i in x.evidence_ids if i in by_id]}
                         for x in findings],
            "recent_records": [evidence(r) for r in records[-80:][::-1]],
            "imports": [{"category": x.category, "filename": x.filename, "row_count": x.row_count,
                         "imported_at": x.imported_at.isoformat()} for x in imports]}


@app.get("/reviews/{review_id}/snapshot")
def get_snapshot(review_id: int, session: Session = Depends(db)):
    return snapshot(review_or_404(session, review_id), session)


@app.get("/reviews/{review_id}/batches/{batch_id}/timeline")
def batch_timeline(review_id: int, batch_id: str, session: Session = Depends(db)):
    review_or_404(session, review_id)
    records = session.scalars(select(QualityRecord).where(QualityRecord.review_id == review_id,
        QualityRecord.batch_id == batch_id).order_by(QualityRecord.record_date, QualityRecord.id)).all()
    if not records:
        raise HTTPException(404, "Записи по серии не найдены")
    return {"batch_id": batch_id, "records": [{"id": r.id, "category": r.category,
        "date": r.record_date.isoformat(), "data": r.data} for r in records]}


@app.post("/reviews/{review_id}/attestations", status_code=201)
def attest_no_events(review_id: int, payload: AttestationCreate, session: Session = Depends(db)):
    review_or_404(session, review_id)
    if payload.category not in EVENTS:
        raise HTTPException(400, "Для этого раздела подтверждение отсутствия событий недоступно")
    if not payload.source.strip() or not payload.reviewer.strip():
        raise HTTPException(400, "Укажите источник проверки и имя проверившего")
    if session.scalar(select(QualityRecord.id).where(QualityRecord.review_id == review_id,
        QualityRecord.category == payload.category)):
        raise HTTPException(409, "В разделе уже есть записи; подтвердить ноль нельзя")
    if session.scalar(select(SectionAttestation.id).where(SectionAttestation.review_id == review_id,
        SectionAttestation.category == payload.category)):
        raise HTTPException(409, "Отсутствие событий уже подтверждено")
    item = SectionAttestation(review_id=review_id, category=payload.category,
                              source=payload.source.strip(), reviewer=payload.reviewer.strip())
    session.add(item)
    session.commit()
    return {"category": item.category, "source": item.source, "reviewer": item.reviewer,
            "confirmed_at": item.confirmed_at.isoformat()}


@app.delete("/reviews/{review_id}/attestations/{category}", status_code=204)
def revoke_no_events(review_id: int, category: str, session: Session = Depends(db)):
    review_or_404(session, review_id)
    item = session.scalar(select(SectionAttestation).where(SectionAttestation.review_id == review_id,
        SectionAttestation.category == category))
    if not item:
        raise HTTPException(404, "Подтверждение не найдено")
    session.delete(item)
    session.commit()
    return Response(status_code=204)


@app.post("/reviews/{review_id}/findings", status_code=201)
def add_finding(review_id: int, payload: FindingCreate, session: Session = Depends(db)):
    review_or_404(session, review_id)
    if not payload.text.strip():
        raise HTTPException(400, "Введите текст вывода")
    if session.scalar(select(EvidenceFinding.id).where(EvidenceFinding.review_id == review_id,
        EvidenceFinding.signal_key == payload.signal_key)):
        raise HTTPException(409, "Этот сигнал уже добавлен в отчёт")
    records = session.scalars(select(QualityRecord).where(QualityRecord.review_id == review_id)).all()
    signal = next((x for x in quality_signals(records) if x["key"] == payload.signal_key), None)
    if not signal:
        raise HTTPException(404, "Сигнал не найден в текущих данных")
    item = EvidenceFinding(review_id=review_id, signal_key=payload.signal_key,
                           title=signal["title"], text=payload.text.strip(),
                           evidence_ids=signal["evidence_ids"])
    session.add(item)
    session.commit()
    return {"id": item.id, "title": item.title, "text": item.text, "evidence_ids": item.evidence_ids}


@app.patch("/reviews/{review_id}/findings/{finding_id}")
def edit_finding(review_id: int, finding_id: int, payload: FindingUpdate, session: Session = Depends(db)):
    review_or_404(session, review_id)
    item = session.scalar(select(EvidenceFinding).where(EvidenceFinding.id == finding_id,
        EvidenceFinding.review_id == review_id))
    if not item:
        raise HTTPException(404, "Вывод не найден")
    if not payload.text.strip():
        raise HTTPException(400, "Введите текст вывода")
    item.text = payload.text.strip()
    session.commit()
    return {"id": item.id, "text": item.text}


@app.delete("/reviews/{review_id}/findings/{finding_id}", status_code=204)
def delete_finding(review_id: int, finding_id: int, session: Session = Depends(db)):
    review_or_404(session, review_id)
    item = session.scalar(select(EvidenceFinding).where(EvidenceFinding.id == finding_id,
        EvidenceFinding.review_id == review_id))
    if not item:
        raise HTTPException(404, "Вывод не найден")
    session.delete(item)
    session.commit()
    return Response(status_code=204)


@app.post("/demo", status_code=201)
def create_demo(session: Session = Depends(db)):
    product, site, year = "Препарат А · таблетки 10 мг", "АЛИУМ · Оболенск", 2025
    previous = session.scalar(select(Review).where(Review.product == product, Review.site == site, Review.year == 2024))
    if not previous:
        previous = Review(product=product, site=site, year=2024,
                          conclusion="Показатели 2024 года использованы как база для сравнения.")
        session.add(previous)
        session.flush()
        for index in range(1, 11):
            batch = f"A24-{index:03d}"
            day = index * 27
            stamp = f"2024-{(day-1)//28+1:02d}-{(day-1)%28+1:02d}"
            for category, values in [("batches", {"status": "Выпущена", "yield_pct": "98"}),
                ("tests", {"parameter": "Растворение", "result": str(91 + index % 3), "unit": "%",
                           "lower_limit": "80", "upper_limit": "100"})]:
                payload = {"date": stamp, "batch_id": batch, "_source_file": "Демонстрационные данные", **values}
                session.add(QualityRecord(review_id=previous.id, category=category,
                    record_date=date.fromisoformat(stamp), batch_id=batch, data=payload))
        for ident, day, batch in [("DEV-014", 100, "A24-004"), ("DEV-019", 210, "A24-008")]:
            stamp = f"2024-{(day-1)//28+1:02d}-{(day-1)%28+1:02d}"
            payload = {"id": ident, "date": stamp, "batch_id": batch, "status": "Закрыто",
                       "description": "Отклонение процесса", "_source_file": "Демонстрационные данные"}
            session.add(QualityRecord(review_id=previous.id, category="deviations",
                record_date=date.fromisoformat(stamp), batch_id=batch, data=payload))
        for category, count in [("batches", 10), ("tests", 10), ("deviations", 2)]:
            session.add(ImportRun(review_id=previous.id, category=category, filename="Демонстрационные данные",
                sha256=hashlib.sha256(f"demo:2024:{category}".encode()).hexdigest(), row_count=count))
    review = session.scalar(select(Review).where(Review.product == product, Review.site == site, Review.year == year))
    if review:
        session.commit()
        return review_dict(review)
    review = Review(product=product, site=site, year=year,
        conclusion="Процесс в целом воспроизводим. Требуется оценить нисходящий тренд показателя растворения и закрытие отклонения DEV-024.",
        actions="Проверить причины тренда растворения; завершить CAPA-011; подтвердить эффективность мер в следующем обзоре.")
    session.add(review)
    session.flush()
    def add(category: str, day: int, batch: str, **data: str):
        payload = {"date": f"2025-{(day-1)//28+1:02d}-{(day-1)%28+1:02d}", "batch_id": batch, **data}
        session.add(QualityRecord(review_id=review.id, category=category,
            record_date=date.fromisoformat(payload["date"]), batch_id=batch, data=payload))
    for index in range(1, 13):
        batch = f"A25-{index:03d}"
        day = index * 25
        add("batches", day, batch, status="Выпущена", yield_pct=str(96 + index % 4))
        add("tests", day, batch, parameter="Растворение", result=str(91 - index * .7), unit="%",
            lower_limit="80", upper_limit="100")
        add("tests", day, batch, parameter="Содержание действующего вещества", result=str(98 + index % 3),
            unit="%", lower_limit="95", upper_limit="105")
    for category, ident, day, batch, status, description in [
        ("deviations", "DEV-024", 140, "A25-006", "Открыто", "Отклонение температуры при упаковке"),
        ("oos", "OOS-003", 168, "A25-007", "Закрыто", "Повторное испытание показателя растворения"),
        ("changes", "CHG-018", 196, "", "Утверждено", "Изменение поставщика упаковки"),
        ("complaints", "CMP-004", 224, "A25-009", "Закрыто", "Повреждение вторичной упаковки"),
        ("capa", "CAPA-011", 252, "", "В работе", "Дополнительный контроль упаковочной линии")]:
        add(category, day, batch, id=ident, status=status, description=description)
    for index in range(1, 5):
        add("stability", index * 65, "A25-001", parameter="Содержание действующего вещества",
            result=str(100 - index * .4), unit="%", lower_limit="95", upper_limit="105")
    for category, count in [("batches", 12), ("tests", 24), ("stability", 4),
                             ("deviations", 1), ("oos", 1), ("changes", 1), ("complaints", 1), ("capa", 1)]:
        session.add(ImportRun(review_id=review.id, category=category, filename="Демонстрационные данные",
            sha256=hashlib.sha256(f"demo:{category}".encode()).hexdigest(), row_count=count))
    session.commit()
    return review_dict(review)


def register_font():
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not os.path.exists(path):
        path = "C:/Windows/Fonts/arial.ttf"
    if os.path.exists(path) and "ReportFont" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("ReportFont", path))
    return "ReportFont" if "ReportFont" in pdfmetrics.getRegisteredFontNames() else "Helvetica"


@app.get("/reviews/{review_id}/report.pdf")
def report_pdf(review_id: int, session: Session = Depends(db)):
    review = review_or_404(session, review_id)
    data = snapshot(review, session)
    font = register_font()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                            topMargin=17*mm, bottomMargin=17*mm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BodyRu", fontName=font, fontSize=9, leading=14, spaceAfter=7))
    styles.add(ParagraphStyle(name="TitleRu", fontName=font, fontSize=17, leading=23, textColor=colors.HexColor("#073D78"), spaceAfter=12))
    styles.add(ParagraphStyle(name="HeadRu", fontName=font, fontSize=11, leading=15, textColor=colors.HexColor("#073D78"), spaceBefore=13, spaceAfter=7))
    styles.add(ParagraphStyle(name="SmallRu", fontName=font, fontSize=7, leading=10))
    story = [Paragraph("Годовой обзор качества продукции", styles["TitleRu"]),
             Paragraph(f"{escape(review.product)} · {escape(review.site)} · {review.year}", styles["BodyRu"]),
             Paragraph("ЧЕРНОВИК · требует проверки и утверждения специалистом по качеству", styles["BodyRu"]),
             Paragraph("1. Полнота исходных данных", styles["HeadRu"])]
    rows = [["Раздел", "Записей", "Состояние"]]
    for key in SCHEMAS:
        count = data["counts"][key]
        status = {"loaded": "Загружен", "confirmed_none": "Подтверждён ноль", "missing": "Не проверено"}[data["statuses"][key]]
        rows.append([LABELS[key], str(count), status])
    table = Table(rows, colWidths=[85*mm, 25*mm, 55*mm], repeatRows=1)
    table.setStyle(TableStyle([("FONTNAME", (0,0),(-1,-1),font), ("FONTSIZE",(0,0),(-1,-1),8),
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E9F1F7")), ("LINEBELOW",(0,0),(-1,-1),0.3,colors.HexColor("#CAD7E1")),
        ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story.append(table)
    for item in data["attestations"]:
        story.append(Paragraph(escape(f"{LABELS[item['category']]}: отсутствие событий подтвердил(а) "
            f"{item['reviewer']}; источник: {item['source']}; дата: {item['confirmed_at'][:10]}"), styles["SmallRu"]))
    story += [Paragraph("2. Ключевые показатели", styles["HeadRu"]),
              Paragraph(f"Серий: {data['counts']['batches']}. Испытаний: {data['counts']['tests']}. "
                        f"Результатов вне указанных границ: {data['out_of_spec']}. "
                        f"Отклонений: {data['counts']['deviations']}. Рекламаций: {data['counts']['complaints']}.", styles["BodyRu"]),
              Paragraph("3. Тенденции показателей", styles["HeadRu"])]
    if data["series"]:
        for item in data["series"]:
            values = [point["value"] for point in item["points"]]
            story.append(Paragraph(f"{LABELS[item['category']]} · {escape(item['parameter'])} ({escape(item['unit'])}): "
                f"n={len(values)}, min={min(values):g}, max={max(values):g}, среднее={sum(values)/len(values):.2f}", styles["BodyRu"]))
    else:
        story.append(Paragraph("Данные испытаний и стабильности не загружены.", styles["BodyRu"]))
    if data["signals"]:
        story.append(Paragraph("Сигналы для экспертной проверки", styles["HeadRu"]))
        for item in data["signals"]:
            story.append(Paragraph(escape(f"{item['title']} · {item['parameter']}: {item['detail']} "
                f"Правило: {item['rule']} Записи: {', '.join(map(str, item['evidence_ids']))}."), styles["SmallRu"]))
    story.append(Paragraph("4. Сравнение с предыдущим годом", styles["HeadRu"]))
    comparison = data["comparison"]
    if comparison:
        for key, title in [("batches", "Серии"), ("deviations", "Отклонения"),
                           ("complaints", "Рекламации"), ("oos", "OOS/OOT"), ("open_capa", "Открытые CAPA")]:
            current_value = comparison["current"]["counts"][key]
            previous_value = comparison["previous"]["counts"][key]
            story.append(Paragraph(f"{title}: {comparison['previous_year']} — {previous_value}; "
                                   f"{review.year} — {current_value}.", styles["SmallRu"]))
        for key, title in [("deviations", "Отклонения"), ("complaints", "Рекламации"), ("oos", "OOS/OOT")]:
            current_rate = comparison["current"]["rates_per_100_batches"][key]
            previous_rate = comparison["previous"]["rates_per_100_batches"][key]
            story.append(Paragraph(f"{title} на 100 серий: {comparison['previous_year']} — "
                f"{previous_rate if previous_rate is not None else 'н/д'}; {review.year} — "
                f"{current_rate if current_rate is not None else 'н/д'}.", styles["SmallRu"]))
        prior_averages = {(item["category"], item["parameter"], item["unit"]): item
                          for item in comparison["previous"]["averages"]}
        for item in comparison["current"]["averages"]:
            prior = prior_averages.get((item["category"], item["parameter"], item["unit"]))
            if prior:
                story.append(Paragraph(escape(f"{item['parameter']} ({item['unit']}): среднее "
                    f"{comparison['previous_year']} — {prior['mean']}; {review.year} — {item['mean']} "
                    f"(n={prior['n']} / {item['n']})."), styles["SmallRu"]))
    else:
        story.append(Paragraph("Обзор за предыдущий год не найден.", styles["BodyRu"]))
    story.append(Paragraph("5. Реестр сведений по разделам", styles["HeadRu"]))
    record_rows = session.scalars(select(QualityRecord).where(QualityRecord.review_id == review.id)
                                  .order_by(QualityRecord.category, QualityRecord.record_date)).all()
    grouped = defaultdict(list)
    for record in record_rows:
        grouped[record.category].append(record)
    for key in SCHEMAS:
        story.append(Paragraph(f"{LABELS[key]} · {len(grouped[key])}", styles["HeadRu"]))
        if not grouped[key]:
            attestation = next((x for x in data["attestations"] if x["category"] == key), None)
            if attestation:
                story.append(Paragraph(escape(f"Отсутствие событий подтверждено. Источник: {attestation['source']}; "
                                              f"проверил(а): {attestation['reviewer']}."), styles["SmallRu"]))
            else:
                story.append(Paragraph("Нет загруженных данных; отсутствие событий не подтверждено.", styles["SmallRu"]))
            continue
        for record in grouped[key]:
            fields = record.data
            detail = fields.get("description") or (f"{fields.get('parameter')}: {fields.get('result')} {fields.get('unit')} "
                f"[{fields.get('lower_limit')}–{fields.get('upper_limit')}]" if fields.get("parameter") else fields.get("status", ""))
            story.append(Paragraph(escape(f"{record.record_date} · {record.batch_id or fields.get('id', '—')} · {detail}"), styles["SmallRu"]))
    story.append(Paragraph("6. Выводы с доказательствами", styles["HeadRu"]))
    if data["findings"]:
        for item in data["findings"]:
            story.append(Paragraph(escape(item["title"]), styles["HeadRu"]))
            story.append(Paragraph(escape(item["text"]).replace("\n", "<br/>"), styles["BodyRu"]))
            for record in item["evidence"]:
                record_data = record["data"]
                detail = (f"{record_data['parameter']}: {record_data['result']} {record_data.get('unit', '')}"
                          if record_data.get("parameter") else record_data.get("description", ""))
                story.append(Paragraph(escape(f"Запись #{record['id']} · {record['date']} · "
                    f"{record['batch_id'] or 'без серии'} · {detail} · {record['source']}"
                    f"{', строка ' + str(record['source_row']) if record['source_row'] else ''}"), styles["SmallRu"]))
    else:
        story.append(Paragraph("Доказательные выводы не добавлены.", styles["SmallRu"]))
    story += [Paragraph("7. Заключение специалиста", styles["HeadRu"]),
              Paragraph(escape(review.conclusion or "Не заполнено.").replace("\n", "<br/>"), styles["BodyRu"]),
              Paragraph("8. Предлагаемые действия", styles["HeadRu"]),
              Paragraph(escape(review.actions or "Не заполнено.").replace("\n", "<br/>"), styles["BodyRu"]),
              Paragraph("Источники данных", styles["HeadRu"])]
    for item in data["imports"]:
        story.append(Paragraph(f"{LABELS[item['category']]}: {escape(item['filename'])} · {item['row_count']} строк · {item['imported_at'][:10]}", styles["SmallRu"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Сформировано автоматически. Проверка полноты и интерпретация результатов обязательны.", styles["SmallRu"]))
    doc.build(story)
    return Response(buffer.getvalue(), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="quality-review-{review.year}.pdf"'})
