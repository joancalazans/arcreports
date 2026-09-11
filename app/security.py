from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional
from urllib.parse import parse_qs

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AdminActionLog, PortalGroup, Report, ReportCategory, User


settings = get_settings()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return f"pbkdf2_sha256${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt_b64, digest_b64 = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    expected = hash_password(password, base64.b64decode(salt_b64)).split("$", 2)[2]
    return hmac.compare_digest(expected, digest_b64)


def create_session_token(user_id: int) -> str:
    payload = {"user_id": user_id, "iat": int(time.time())}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(settings.secret_key.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload_json}:{signature}".encode()).decode()


def read_session_token(token: str) -> Optional[int]:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        payload_json, signature = decoded.rsplit(":", 1)
        payload = json.loads(payload_json)
        user_id = int(payload["user_id"])
        issued_at = int(payload["iat"])
    except Exception:
        return None
    expected = hmac.new(settings.secret_key.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        ttl_hours = int(os.getenv("SESSION_TTL_HOURS", "8"))
    except ValueError:
        ttl_hours = 8
    if time.time() - issued_at > ttl_hours * 3600:
        return None
    return user_id


def generate_csrf_token(session_token: str) -> str:
    return hmac.new(
        settings.secret_key.encode(),
        session_token.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_csrf_token(session_token: str, csrf_token: str) -> bool:
    if not session_token or not csrf_token:
        return False
    expected = generate_csrf_token(session_token)
    return hmac.compare_digest(expected, csrf_token)


async def verify_csrf(request: Request) -> None:
    session_token = request.cookies.get(settings.cookie_name, "")
    if not session_token:
        return
    csrf_token = request.headers.get("X-CSRF-Token", "").strip()
    if not csrf_token:
        body = await request.body()
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            csrf_token = str(payload.get("csrf_token", "") or "").strip()
        else:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            csrf_token = (parsed.get("csrf_token", [""])[0] or "").strip()
    if not verify_csrf_token(session_token, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")


async def verify_csrf_header(request: Request) -> None:
    session_token = request.cookies.get(settings.cookie_name, "")
    if not session_token:
        return
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not verify_csrf_token(session_token, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")


def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    user_id = read_session_token(token)
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user if user and user.is_active else None


def require_user(request: Request, db: Session) -> User | RedirectResponse:
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return user


def require_admin(request: Request, db: Session) -> User | RedirectResponse:
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not user.is_admin:
        return RedirectResponse("/home", status_code=status.HTTP_303_SEE_OTHER)
    return user


def require_portal_permission(request: Request, db: Session, permission_name: str) -> User | RedirectResponse:
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.is_admin or bool(getattr(user, permission_name, False)):
        return user
    return RedirectResponse("/home", status_code=status.HTTP_303_SEE_OTHER)


def require_relatorios(request: Request, db: Session) -> User | RedirectResponse:
    return require_portal_permission(request, db, "portal_relatorios")


def require_dashboard(request: Request, db: Session) -> User | RedirectResponse:
    return require_portal_permission(request, db, "portal_dashboard")


def require_importacao(request: Request, db: Session) -> User | RedirectResponse:
    return require_portal_permission(request, db, "portal_importacao")


def require_usuarios(request: Request, db: Session) -> User | RedirectResponse:
    return require_portal_permission(request, db, "portal_usuarios")


def require_history(request: Request, db: Session) -> User | RedirectResponse:
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.is_admin or user.portal_relatorios or user.portal_importacao:
        return user
    return RedirectResponse("/home", status_code=status.HTTP_303_SEE_OTHER)


def user_has_portal_group(user: User, group_name: str) -> bool:
    return any(group.name == group_name for group in user.portal_groups)


def user_report_category_names(user: User) -> set[str]:
    direct = {category.name for category in user.report_categories if category.is_active}
    inherited = {
        category.name
        for group in user.portal_groups
        for category in group.report_categories
        if category.is_active
    }
    return direct | inherited


def can_access_report_category(user: User, category: str | None) -> bool:
    if user.is_admin:
        return True
    if not category:
        return False
    return category in user_report_category_names(user)


def can_access_report(user: User, report: Report | None) -> bool:
    return bool(report and can_access_report_category(user, report.category))


def log_category_denial(db: Session, user: User | None, report: Report | None, action: str) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            report_id=report.id if report else None,
            report_name=report.name if report else None,
            table_name=report.destination_table if report else None,
            status="denied",
            message=(
                "Acesso negado por categoria: "
                f"{report.category or 'sem categoria'}"
                if report
                else "Acesso negado por categoria."
            ),
        )
    )
    db.commit()


def active_report_categories(db: Session) -> list[ReportCategory]:
    return db.query(ReportCategory).filter(ReportCategory.is_active.is_(True)).order_by(ReportCategory.name.asc()).all()


def require_view_user(request: Request, db: Session) -> User | RedirectResponse:
    user = require_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if (
        user.is_admin
        or user.portal_relatorios
        or user.portal_dashboard
        or user_has_portal_group(user, "portal_view")
    ):
        return user
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


def ensure_portal_groups(db: Session) -> None:
    existing = {group.name for group in db.query(PortalGroup).all()}
    for group_name in ("portal_admin", "portal_view"):
        if group_name not in existing:
            db.add(PortalGroup(name=group_name))
    db.commit()


def ensure_admin_user(db: Session) -> None:
    if not settings.local_admin_configured:
        return
    ensure_portal_groups(db)
    portal_admin = db.query(PortalGroup).filter(PortalGroup.name == "portal_admin").first()
    existing = db.query(User).filter(User.username == settings.admin_username).first()
    if existing:
        existing.auth_source = "local"
        existing.is_local = True
        if not existing.password_hash:
            existing.password_hash = hash_password(settings.admin_password)
        existing.is_admin = True
        existing.is_active = True
        if portal_admin and portal_admin not in existing.portal_groups:
            existing.portal_groups.append(portal_admin)
        db.commit()
        return
    admin = User(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        auth_source="local",
        is_local=True,
        is_admin=True,
        is_active=True,
    )
    if portal_admin:
        admin.portal_groups.append(portal_admin)
    db.add(admin)
    db.commit()
