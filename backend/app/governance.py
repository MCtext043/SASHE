"""Access, profile, source and review controls for the local review workspace."""
from __future__ import annotations

import hashlib
import hmac
import io
import importlib
import json
import secrets
import base64
import jwt
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import (Base, Review, QualityRecord, ImportRun, SCHEMAS, LABELS, SessionLocal,
                   app, db, settings, review_or_404, validate_rows, snapshot, report_pdf)


class AppUser(Base):
    __tablename__ = "app_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    salt: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(64))
    roles: Mapped[list[str]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(default=True)


class AppSession(Base):
    __tablename__ = "app_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class OidcAttempt(Base):
    __tablename__ = "oidc_attempts"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    verifier: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class QualityProfile(Base):
    __tablename__ = "quality_profiles"
    __table_args__ = (UniqueConstraint("product", "site", "version", name="uq_quality_profile_scope_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product: Mapped[str] = mapped_column(String(160))
    site: Mapped[str] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(Integer)
    required_sections: Mapped[list[str]] = mapped_column(JSON)
    extra_fields: Mapped[dict[str, list[str]]] = mapped_column(JSON)
    near_limit_pct: Mapped[float] = mapped_column(default=15.0)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewProfileLink(Base):
    __tablename__ = "review_profile_links"
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("quality_profiles.id"))


class AuditHead(Base):
    __tablename__ = "audit_head"
    id: Mapped[int] = mapped_column(primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer, default=1)
    last_hash: Mapped[str] = mapped_column(String(64), default="0" * 64)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(160))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    occurred_at: Mapped[str] = mapped_column(String(40))
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class ReviewWorkflow(Base):
    __tablename__ = "review_workflows"
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default="draft")
    submitted_by: Mapped[str] = mapped_column(String(80), default="")
    checked_by: Mapped[str] = mapped_column(String(80), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewRelease(Base):
    __tablename__ = "review_releases"
    __table_args__ = (UniqueConstraint("review_id", "version", name="uq_review_release_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    version: Mapped[int] = mapped_column(Integer)
    checked_by: Mapped[str] = mapped_column(String(80))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sha256: Mapped[str] = mapped_column(String(64))
    pdf: Mapped[bytes] = mapped_column(LargeBinary)


def password_digest(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310_000).hex()


def seed_local_users() -> None:
    if settings.auth_mode == "test" and not settings.database_url.startswith("sqlite:"):
        raise RuntimeError("AUTH_MODE=test допустим только с временной SQLite базой")
    if settings.auth_mode != "local":
        return
    credentials = [("admin", settings.local_admin_password, ["admin"]),
                   ("author", settings.local_author_password, ["author"]),
                   ("reviewer", settings.local_reviewer_password, ["reviewer"])]
    if any(not password for _, password, _ in credentials):
        raise RuntimeError("Для AUTH_MODE=local задайте три LOCAL_*_PASSWORD")
    with SessionLocal() as session:
        if not session.get(AuditHead, 1):
            session.add(AuditHead(id=1, next_seq=1, last_hash="0" * 64))
        for username, password, roles in credentials:
            existing = session.scalar(select(AppUser).where(AppUser.username == username))
            if not existing:
                salt = secrets.token_hex(16)
                session.add(AppUser(username=username, salt=salt,
                                    password_hash=password_digest(password, salt), roles=roles))
            elif not hmac.compare_digest(existing.password_hash, password_digest(password, existing.salt)):
                existing.salt = secrets.token_hex(16)
                existing.password_hash = password_digest(password, existing.salt)
                session.query(AppSession).filter(AppSession.user_id == existing.id).delete()
        session.commit()


def actor(request: Request) -> dict[str, Any]:
    return request.state.actor


def require_roles(request: Request, *allowed: str) -> dict[str, Any]:
    principal = actor(request)
    if not any(role in principal["roles"] for role in allowed):
        with SessionLocal() as session:
            audit_event(session, request, "access.denied", request.url.path, {"required_roles": list(allowed)})
            session.commit()
        raise HTTPException(403, "Для этого действия нужна другая роль")
    return principal


def event_payload(seq: int, actor_name: str, action: str, target: str,
                  detail: dict[str, Any], occurred_at: str, prev_hash: str) -> str:
    return json.dumps({"seq": seq, "actor": actor_name, "action": action, "target": target,
                       "detail": detail, "occurred_at": occurred_at, "prev_hash": prev_hash},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def audit_event(session: Session, request: Request, action: str, target: str,
                detail: dict[str, Any] | None = None) -> None:
    head = session.scalar(select(AuditHead).where(AuditHead.id == 1).with_for_update())
    if not head:
        head = AuditHead(id=1, next_seq=1, last_hash="0" * 64)
        session.add(head)
        session.flush()
    actor_name = actor(request)["username"]
    now = datetime.now(timezone.utc).isoformat()
    detail = detail or {}
    payload = event_payload(head.next_seq, actor_name, action, target, detail, now, head.last_hash)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    session.add(AuditEvent(seq=head.next_seq, actor=actor_name, action=action, target=target,
                           detail=detail, occurred_at=now, prev_hash=head.last_hash, event_hash=digest))
    head.next_seq += 1
    head.last_hash = digest


def verify_audit(session: Session) -> tuple[bool, int]:
    prior = "0" * 64
    events = session.scalars(select(AuditEvent).order_by(AuditEvent.seq)).all()
    for expected, item in enumerate(events, 1):
        payload = event_payload(item.seq, item.actor, item.action, item.target,
                                item.detail, item.occurred_at, item.prev_hash)
        if item.seq != expected or item.prev_hash != prior or item.event_hash != hashlib.sha256(payload.encode()).hexdigest():
            return False, len(events)
        prior = item.event_hash
    head = session.get(AuditHead, 1)
    return bool(head and head.next_seq == len(events) + 1 and head.last_hash == prior), len(events)


def profile_dict(profile: QualityProfile | None) -> dict[str, Any]:
    if not profile:
        return {"id": None, "version": 0, "product": "", "site": "", "required_sections": list(SCHEMAS),
                "extra_fields": {}, "near_limit_pct": 15.0, "label": "Базовый профиль"}
    return {"id": profile.id, "version": profile.version, "product": profile.product, "site": profile.site,
            "required_sections": profile.required_sections, "extra_fields": profile.extra_fields,
            "near_limit_pct": profile.near_limit_pct,
            "label": f"{profile.product} · {profile.site} · версия {profile.version}"}


def profile_for_review(session: Session, review_id: int) -> dict[str, Any]:
    link = session.get(ReviewProfileLink, review_id)
    return profile_dict(session.get(QualityProfile, link.profile_id) if link else None)


def bind_latest_profile(session: Session, review: Review) -> None:
    profile = session.scalar(select(QualityProfile).where(QualityProfile.product == review.product,
        QualityProfile.site == review.site).order_by(QualityProfile.version.desc()))
    if profile:
        session.add(ReviewProfileLink(review_id=review.id, profile_id=profile.id))


def workflow_for_review(session: Session, review_id: int) -> ReviewWorkflow:
    item = session.get(ReviewWorkflow, review_id)
    if not item:
        item = ReviewWorkflow(review_id=review_id, version=1, state="draft")
        session.add(item)
        session.flush()
    return item


def ensure_editable(session: Session, review_id: int) -> None:
    item = session.get(ReviewWorkflow, review_id)
    if item and item.state in {"in_review", "checked"}:
        raise HTTPException(409, "Обзор находится на проверке или уже проверен")


def workflow_dict(item: ReviewWorkflow) -> dict[str, Any]:
    return {"version": item.version, "state": item.state, "submitted_by": item.submitted_by,
            "checked_by": item.checked_by, "comment": item.comment,
            "changed_at": item.changed_at.isoformat() if item.changed_at else ""}


def auth_error(status: int, message: str, origin: str | None) -> JSONResponse:
    response = JSONResponse({"detail": message}, status_code=status)
    if origin == settings.cors_origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def oidc_configured() -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id and settings.oidc_redirect_uri)


def oidc_metadata() -> dict[str, Any]:
    issuer = settings.oidc_issuer.rstrip("/")
    if not oidc_configured() or urlparse(issuer).scheme != "https":
        raise HTTPException(503, "Корпоративный вход не настроен")
    try:
        with urlopen(f"{issuer}/.well-known/openid-configuration", timeout=8) as response:
            metadata = json.load(response)
        if metadata.get("issuer") != issuer:
            raise ValueError("OIDC issuer mismatch")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if urlparse(metadata.get(key, "")).scheme != "https":
                raise ValueError(f"OIDC {key} must use HTTPS")
        return metadata
    except Exception as exc:
        raise HTTPException(502, "Не удалось проверить параметры корпоративного входа") from exc


def session_response(user: AppUser, session: Session, redirect: bool = False) -> Response:
    token = secrets.token_urlsafe(40)
    session.add(AppSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                           expires_at=datetime.utcnow() + timedelta(hours=8)))
    session.commit()
    response = RedirectResponse(settings.cors_origin, status_code=303) if redirect else JSONResponse(
        {"username": user.username, "roles": user.roles})
    response.set_cookie("sashe_session", token, max_age=8 * 3600, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")
    return response


@app.middleware("http")
async def authentication(request: Request, call_next):
    if settings.auth_mode == "test":
        request.state.actor = {"username": "test", "roles": ["admin", "author", "reviewer"]}
        return await call_next(request)
    if request.method == "OPTIONS" or request.url.path in {"/health", "/auth/login", "/auth/config", "/auth/oidc/start", "/auth/oidc/callback", "/openapi.json", "/docs", "/docs/oauth2-redirect"}:
        return await call_next(request)
    token = request.cookies.get("sashe_session", "")
    principal = None
    if token:
        with SessionLocal() as session:
            stored = session.get(AppSession, hashlib.sha256(token.encode()).hexdigest())
            if stored and stored.expires_at > datetime.utcnow():
                user = session.get(AppUser, stored.user_id)
                if user and user.active:
                    principal = {"username": user.username, "roles": user.roles}
    if not principal:
        return auth_error(401, "Войдите в рабочее пространство", request.headers.get("origin"))
    request.state.actor = principal
    if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.headers.get("x-requested-with") != "SASHE":
        return auth_error(403, "Отсутствует защитный заголовок запроса", request.headers.get("origin"))
    return await call_next(request)


router = APIRouter()


@router.get("/auth/config")
def auth_config():
    return {"local": settings.auth_mode == "local", "oidc": oidc_configured()}


@router.get("/auth/oidc/start")
def oidc_start(session: Session = Depends(db)):
    metadata = oidc_metadata()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session.add(OidcAttempt(state_hash=hashlib.sha256(state.encode()).hexdigest(), verifier=verifier,
                            nonce=nonce, expires_at=datetime.utcnow() + timedelta(minutes=5)))
    session.commit()
    url = metadata["authorization_endpoint"] + "?" + urlencode({"response_type": "code",
        "client_id": settings.oidc_client_id, "redirect_uri": settings.oidc_redirect_uri,
        "scope": "openid profile email", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    return RedirectResponse(url, status_code=302)


@router.get("/auth/oidc/callback")
def oidc_callback(code: str, state: str, session: Session = Depends(db)):
    metadata = oidc_metadata()
    attempt = session.get(OidcAttempt, hashlib.sha256(state.encode()).hexdigest())
    if not attempt or attempt.expires_at < datetime.utcnow():
        raise HTTPException(401, "Сеанс корпоративного входа истёк")
    verifier, nonce = attempt.verifier, attempt.nonce
    session.delete(attempt)
    session.commit()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.oidc_redirect_uri,
            "client_id": settings.oidc_client_id, "code_verifier": verifier}
    if settings.oidc_client_secret:
        form["client_secret"] = settings.oidc_client_secret
    try:
        request = UrlRequest(metadata["token_endpoint"], data=urlencode(form).encode(),
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(request, timeout=8) as response:
            tokens = json.load(response)
        claims = jwt.decode(tokens["id_token"], jwt.PyJWKClient(metadata["jwks_uri"]).get_signing_key_from_jwt(
            tokens["id_token"]).key, algorithms=["RS256", "ES256"],
            audience=settings.oidc_client_id, issuer=settings.oidc_issuer.rstrip("/"))
        if claims.get("nonce") != nonce:
            raise ValueError("OIDC nonce mismatch")
        subject = str(claims["sub"])
        if len(subject) > 160:
            raise ValueError("OIDC subject too long")
        configured_groups = json.loads(settings.oidc_role_groups or "{}")
        groups = claims.get(settings.oidc_groups_claim, [])
        if not isinstance(groups, list) or not isinstance(configured_groups, dict):
            raise ValueError("Invalid OIDC role mapping")
        roles = [role for role in ("author", "reviewer", "admin")
                 if isinstance(configured_groups.get(role), list) and
                 set(groups).intersection(configured_groups[role])]
        if not roles:
            raise HTTPException(403, "Для корпоративной учётной записи не назначена роль")
        username = f"oidc:{hashlib.sha256((settings.oidc_issuer + subject).encode()).hexdigest()[:48]}"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, "Не удалось подтвердить корпоративный вход") from exc
    user = session.scalar(select(AppUser).where(AppUser.username == username))
    if not user:
        user = AppUser(username=username, salt="", password_hash="", roles=roles)
        session.add(user)
        session.flush()
    if not user.active:
        raise HTTPException(403, "Учётная запись отключена")
    user.roles = roles
    session.commit()
    return session_response(user, session, redirect=True)


class LoginPayload(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(payload: LoginPayload, session: Session = Depends(db)):
    if settings.auth_mode != "local":
        raise HTTPException(403, "Локальный вход отключён")
    user = session.scalar(select(AppUser).where(AppUser.username == payload.username.strip()))
    if not user or not user.active or not hmac.compare_digest(user.password_hash,
            password_digest(payload.password, user.salt)):
        raise HTTPException(401, "Не удалось подтвердить учётные данные")
    return session_response(user, session)


@router.get("/auth/me")
def me(request: Request):
    return actor(request)


@router.post("/auth/logout")
def logout(request: Request, session: Session = Depends(db)):
    token = request.cookies.get("sashe_session", "")
    if token:
        stored = session.get(AppSession, hashlib.sha256(token.encode()).hexdigest())
        if stored:
            session.delete(stored)
            session.commit()
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("sashe_session", path="/")
    return response


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=128)
    roles: list[str] = Field(min_length=1)


@router.get("/auth/users")
def users(request: Request, session: Session = Depends(db)):
    require_roles(request, "admin")
    return [{"username": user.username, "roles": user.roles, "active": user.active}
            for user in session.scalars(select(AppUser).order_by(AppUser.username)).all()]


@router.post("/auth/users", status_code=201)
def create_user(payload: UserCreate, request: Request, session: Session = Depends(db)):
    require_roles(request, "admin")
    if not set(payload.roles) <= {"admin", "author", "reviewer"}:
        raise HTTPException(400, "Неизвестная роль")
    if session.scalar(select(AppUser).where(AppUser.username == payload.username)):
        raise HTTPException(409, "Учётная запись уже существует")
    salt = secrets.token_hex(16)
    session.add(AppUser(username=payload.username, salt=salt,
                        password_hash=password_digest(payload.password, salt), roles=payload.roles))
    audit_event(session, request, "user.created", payload.username, {"roles": payload.roles})
    session.commit()
    return {"username": payload.username, "roles": payload.roles}


@router.get("/audit")
def list_audit(request: Request, session: Session = Depends(db)):
    require_roles(request, "admin", "reviewer")
    events = session.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(100)).all()
    return [{"seq": e.seq, "actor": e.actor, "action": e.action, "target": e.target,
             "detail": e.detail, "occurred_at": e.occurred_at} for e in events]


@router.get("/audit/verify")
def audit_status(request: Request, session: Session = Depends(db)):
    require_roles(request, "admin", "reviewer")
    valid, count = verify_audit(session)
    return {"valid": valid, "count": count}


class ProfileCreate(BaseModel):
    product: str = Field(min_length=2, max_length=160)
    site: str = Field(min_length=2, max_length=160)
    required_sections: list[str] = Field(min_length=1)
    extra_fields: dict[str, list[str]] = Field(default_factory=dict)
    near_limit_pct: float = Field(ge=0, le=50)


@router.get("/profiles")
def profiles(session: Session = Depends(db)):
    return [profile_dict(item) for item in session.scalars(select(QualityProfile).order_by(
        QualityProfile.site, QualityProfile.product, QualityProfile.version.desc())).all()]


@router.post("/profiles", status_code=201)
def create_profile(payload: ProfileCreate, request: Request, session: Session = Depends(db)):
    require_roles(request, "admin")
    if len(set(payload.required_sections)) != len(payload.required_sections) or not set(payload.required_sections) <= set(SCHEMAS):
        raise HTTPException(400, "Проверьте обязательные разделы")
    if not set(payload.extra_fields) <= set(SCHEMAS):
        raise HTTPException(400, "Неизвестный раздел дополнительных полей")
    for fields in payload.extra_fields.values():
        if len(fields) != len(set(fields)) or any(not x.isidentifier() or len(x) > 60 for x in fields):
            raise HTTPException(400, "Некорректные дополнительные поля")
    product, site = payload.product.strip(), payload.site.strip()
    previous = session.scalar(select(QualityProfile).where(QualityProfile.product == product,
        QualityProfile.site == site).order_by(QualityProfile.version.desc()))
    version = previous.version + 1 if previous else 1
    item = QualityProfile(product=product, site=site, version=version,
                          required_sections=payload.required_sections, extra_fields=payload.extra_fields,
                          near_limit_pct=payload.near_limit_pct, created_by=actor(request)["username"])
    session.add(item)
    session.flush()
    audit_event(session, request, "profile.created", f"profile:{item.id}", {"version": version})
    session.commit()
    return profile_dict(item)


@router.get("/reviews/{review_id}/profile")
def review_profile(review_id: int, session: Session = Depends(db)):
    review_or_404(session, review_id)
    return profile_for_review(session, review_id)


@router.post("/reviews/{review_id}/profile/{profile_id}")
def assign_profile(review_id: int, profile_id: int, request: Request, session: Session = Depends(db)):
    require_roles(request, "admin")
    review = review_or_404(session, review_id)
    ensure_editable(session, review_id)
    if session.scalar(select(QualityRecord.id).where(QualityRecord.review_id == review_id)):
        raise HTTPException(409, "Профиль можно назначить до первой загрузки данных")
    profile = session.get(QualityProfile, profile_id)
    if not profile or profile.product != review.product or profile.site != review.site:
        raise HTTPException(400, "Профиль не соответствует препарату и площадке")
    link = session.get(ReviewProfileLink, review_id)
    if link:
        link.profile_id = profile_id
    else:
        session.add(ReviewProfileLink(review_id=review_id, profile_id=profile_id))
    audit_event(session, request, "profile.assigned", f"review:{review_id}", {"profile_id": profile_id})
    session.commit()
    return profile_dict(profile)


@router.get("/source/status")
def source_status():
    configured = bool(settings.source_adapter_module or (settings.source_base_url and settings.source_api_key))
    return {"configured": configured,
            "adapter": settings.source_adapter_module or "http_json",
            "mode": "demo" if configured and not settings.source_adapter_module and urlparse(settings.source_base_url).hostname in {"source", "localhost", "127.0.0.1"}
                    else "external" if configured else "unconfigured"}


@router.post("/reviews/{review_id}/sync/{category}")
def sync_source(review_id: int, category: str, request: Request, session: Session = Depends(db)):
    require_roles(request, "author")
    review = review_or_404(session, review_id)
    ensure_editable(session, review_id)
    if category not in SCHEMAS:
        raise HTTPException(400, "Неизвестный раздел")
    if not settings.source_adapter_module and (not settings.source_base_url or not settings.source_api_key):
        raise HTTPException(503, "Подключение LIMS/QMS не настроено")
    parsed = urlparse(settings.source_base_url)
    if not settings.source_adapter_module and parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"source", "localhost", "127.0.0.1"}):
        raise HTTPException(500, "Источник должен использовать HTTPS")
    from .main import SectionAttestation
    if session.scalar(select(SectionAttestation.id).where(SectionAttestation.review_id == review_id,
        SectionAttestation.category == category)):
        raise HTTPException(409, "Сначала отзовите подтверждение отсутствия событий")
    try:
        if settings.source_adapter_module:
            if not settings.source_adapter_module.startswith("app.connectors."):
                raise ValueError("Модуль адаптера должен находиться в app.connectors")
            adapter = importlib.import_module(settings.source_adapter_module)
            payload = adapter.fetch_export(review.product, review.site, review.year, category, settings)
        else:
            query = urlencode({"product": review.product, "site": review.site, "year": review.year, "category": category})
            url = settings.source_base_url.rstrip("/") + "/exports?" + query
            upstream = UrlRequest(url, headers={"Authorization": f"Bearer {settings.source_api_key}",
                                                "Accept": "application/json"}, method="GET")
            with urlopen(upstream, timeout=12) as response:
                payload = json.load(response)
    except Exception as exc:
        raise HTTPException(502, "Источник недоступен или вернул ошибку") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list) or not isinstance(payload.get("export_id"), str) or not 1 <= len(payload["export_id"].strip()) <= 180:
        raise HTTPException(502, "Некорректный ответ источника")
    if payload.get("count") != len(payload["rows"]):
        raise HTTPException(422, "Источник вернул неполный набор данных. Записи не сохранены.")
    if len(payload["rows"]) > 20_000:
        raise HTTPException(413, "Слишком много записей в одной выгрузке")
    rows = []
    for row in payload["rows"]:
        if not isinstance(row, dict):
            raise HTTPException(422, "Некорректная строка источника")
        rows.append({str(k): str(v if v is not None else "").strip() for k, v in row.items()})
    if not rows:
        return {"imported": 0, "errors": [], "empty": True, "export_id": payload["export_id"]}
    source_label = f"LIMS/QMS: {payload['export_id'].strip()}"
    if session.scalar(select(ImportRun.id).where(ImportRun.review_id == review_id,
        ImportRun.category == category, ImportRun.filename == source_label)):
        raise HTTPException(409, "Эта выгрузка уже загружена")
    profile = profile_for_review(session, review_id)
    errors = validate_rows(rows, category, review.year, profile["extra_fields"].get(category, []))
    if errors:
        return {"imported": 0, "errors": errors}
    digest = hashlib.sha256(json.dumps({"export_id": payload["export_id"], "rows": rows},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if session.scalar(select(ImportRun.id).where(ImportRun.review_id == review_id,
        ImportRun.category == category, ImportRun.sha256 == digest)):
        raise HTTPException(409, "Эта выгрузка уже загружена")
    if category not in ("tests", "stability", "process"):
        existing = session.scalars(select(QualityRecord).where(QualityRecord.review_id == review_id,
            QualityRecord.category == category)).all()
        known = {item.data.get("batch_id") if category == "batches" else item.data.get("id") for item in existing}
        duplicate = [row.get("batch_id") if category == "batches" else row.get("id") for row in rows]
        if any(value in known for value in duplicate):
            raise HTTPException(409, "Часть идентификаторов уже есть в обзоре")
    for number, row in enumerate(rows, 1):
        record = {**row, "_source_file": source_label, "_source_system": "LIMS/QMS",
                  "_source_export_id": payload["export_id"], "_source_row": number}
        session.add(QualityRecord(review_id=review_id, category=category,
            record_date=date.fromisoformat(row["date"]), batch_id=row.get("batch_id", ""), data=record))
    session.add(ImportRun(review_id=review_id, category=category, filename=source_label,
                          sha256=digest, row_count=len(rows)))
    audit_event(session, request, "source.synced", f"review:{review_id}",
                {"category": category, "export_id": payload["export_id"], "count": len(rows), "sha256": digest})
    session.commit()
    return {"imported": len(rows), "errors": [], "export_id": payload["export_id"], "sha256": digest}


@router.get("/reviews/{review_id}/workflow")
def get_workflow(review_id: int, session: Session = Depends(db)):
    review_or_404(session, review_id)
    return workflow_dict(workflow_for_review(session, review_id))


@router.post("/reviews/{review_id}/submit")
def submit_review(review_id: int, request: Request, session: Session = Depends(db)):
    require_roles(request, "author")
    review = review_or_404(session, review_id)
    state = workflow_for_review(session, review_id)
    if state.state not in {"draft", "returned"}:
        raise HTTPException(409, "Эту версию нельзя передать на проверку")
    if not review.conclusion.strip():
        raise HTTPException(422, "Заполните заключение специалиста")
    missing = snapshot(review, session)["missing"]
    if missing:
        raise HTTPException(422, "Проверьте обязательные разделы: " + ", ".join(LABELS[x] for x in missing))
    state.state = "in_review"
    state.submitted_by = actor(request)["username"]
    state.checked_by = ""
    state.comment = ""
    state.changed_at = datetime.utcnow()
    audit_event(session, request, "review.submitted", f"review:{review_id}", {"version": state.version})
    session.commit()
    return workflow_dict(state)


class ReturnPayload(BaseModel):
    comment: str = Field(min_length=5, max_length=2000)


@router.post("/reviews/{review_id}/return")
def return_review(review_id: int, payload: ReturnPayload, request: Request,
                  session: Session = Depends(db)):
    require_roles(request, "reviewer")
    review_or_404(session, review_id)
    state = workflow_for_review(session, review_id)
    if state.state != "in_review":
        raise HTTPException(409, "Обзор не находится на проверке")
    if state.submitted_by == actor(request)["username"]:
        raise HTTPException(403, "Нельзя проверить собственный обзор")
    state.state = "returned"
    state.comment = payload.comment.strip()
    state.checked_by = actor(request)["username"]
    state.changed_at = datetime.utcnow()
    audit_event(session, request, "review.returned", f"review:{review_id}",
                {"version": state.version, "comment": state.comment})
    session.commit()
    return workflow_dict(state)


@router.post("/reviews/{review_id}/check")
def check_review(review_id: int, request: Request, session: Session = Depends(db)):
    require_roles(request, "reviewer")
    review_or_404(session, review_id)
    state = workflow_for_review(session, review_id)
    if state.state != "in_review":
        raise HTTPException(409, "Обзор не находится на проверке")
    reviewer = actor(request)["username"]
    if state.submitted_by == reviewer:
        raise HTTPException(403, "Нельзя проверить собственный обзор")
    pdf = report_pdf(review_id, session).body
    digest = hashlib.sha256(pdf).hexdigest()
    session.add(ReviewRelease(review_id=review_id, version=state.version, checked_by=reviewer,
                              pdf=pdf, sha256=digest))
    state.state = "checked"
    state.checked_by = reviewer
    state.changed_at = datetime.utcnow()
    audit_event(session, request, "review.checked", f"review:{review_id}",
                {"version": state.version, "pdf_sha256": digest})
    session.commit()
    return {**workflow_dict(state), "pdf_sha256": digest}


@router.post("/reviews/{review_id}/reopen")
def reopen_review(review_id: int, request: Request, session: Session = Depends(db)):
    require_roles(request, "author")
    review_or_404(session, review_id)
    state = workflow_for_review(session, review_id)
    if state.state != "checked":
        raise HTTPException(409, "Новая версия доступна после внутренней проверки")
    state.version += 1
    state.state = "draft"
    state.submitted_by = ""
    state.checked_by = ""
    state.comment = ""
    state.changed_at = datetime.utcnow()
    audit_event(session, request, "review.reopened", f"review:{review_id}", {"version": state.version})
    session.commit()
    return workflow_dict(state)


@router.get("/reviews/{review_id}/releases")
def releases(review_id: int, session: Session = Depends(db)):
    review_or_404(session, review_id)
    items = session.scalars(select(ReviewRelease).where(ReviewRelease.review_id == review_id)
                            .order_by(ReviewRelease.version.desc())).all()
    return [{"version": item.version, "checked_by": item.checked_by,
             "checked_at": item.checked_at.isoformat(), "sha256": item.sha256} for item in items]


@router.get("/reviews/{review_id}/releases/{version}.pdf")
def release_pdf(review_id: int, version: int, session: Session = Depends(db)):
    review_or_404(session, review_id)
    item = session.scalar(select(ReviewRelease).where(ReviewRelease.review_id == review_id,
        ReviewRelease.version == version))
    if not item:
        raise HTTPException(404, "Версия не найдена")
    return Response(item.pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="review-{review_id}-v{version}.pdf"',
                             "X-Content-SHA256": item.sha256})


app.include_router(router)
