from __future__ import annotations

import hmac
import math
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.ldap_auth import authenticate_ldap_user, get_ldap_config, record_ldap_status
from app.models import AuthLog, User
from app.routes.common import form_data, render
from app.security import create_session_token, get_current_user, hash_password, user_has_portal_group, verify_csrf, verify_password


router = APIRouter()
INVALID_LOGIN_MESSAGE = "Usuario ou senha invalidos."
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW = timedelta(minutes=10)
BLOCK_DURATION = timedelta(minutes=30)
login_attempts: dict[str, dict[str, object]] = {}
login_attempts_lock = threading.Lock()


def log_auth(
    db: Session,
    username: str,
    auth_source: str,
    auth_status: str,
    message: str,
    level: str = "info",
) -> None:
    db.add(
        AuthLog(
            username=username[:120] or "-",
            auth_source=auth_source,
            status=auth_status,
            level=level,
            message=message,
        )
    )
    db.commit()


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:45] or "unknown"
    return (request.client.host if request.client else "unknown")[:45]


def _cleanup_login_attempts(now: datetime) -> None:
    expired = []
    for ip_address, state in login_attempts.items():
        blocked_until = state.get("bloqueado_ate")
        window_started = state.get("janela_iniciada_em")
        if blocked_until and blocked_until <= now:
            expired.append(ip_address)
        elif not blocked_until and window_started and now - window_started >= ATTEMPT_WINDOW:
            expired.append(ip_address)
    for ip_address in expired:
        login_attempts.pop(ip_address, None)


def check_ip_blocked(ip_address: str, now: datetime | None = None) -> tuple[bool, int]:
    now = now or datetime.utcnow()
    with login_attempts_lock:
        _cleanup_login_attempts(now)
        state = login_attempts.get(ip_address)
        blocked_until = state.get("bloqueado_ate") if state else None
        if not blocked_until:
            return False, 0
        return True, max(1, math.ceil((blocked_until - now).total_seconds() / 60))


def register_failed_attempt(ip_address: str, now: datetime | None = None) -> tuple[int, bool, int]:
    now = now or datetime.utcnow()
    with login_attempts_lock:
        _cleanup_login_attempts(now)
        state = login_attempts.setdefault(
            ip_address,
            {"tentativas": 0, "bloqueado_ate": None, "janela_iniciada_em": now},
        )
        state["tentativas"] = int(state["tentativas"]) + 1
        attempts = int(state["tentativas"])
        if attempts >= MAX_FAILED_ATTEMPTS:
            state["bloqueado_ate"] = now + BLOCK_DURATION
            return 0, True, int(BLOCK_DURATION.total_seconds() // 60)
        return MAX_FAILED_ATTEMPTS - attempts, False, 0


def reset_login_attempts(ip_address: str) -> None:
    with login_attempts_lock:
        login_attempts.pop(ip_address, None)


def unblock_ip(ip_address: str) -> bool:
    with login_attempts_lock:
        return login_attempts.pop(ip_address, None) is not None


def blocked_ips(now: datetime | None = None) -> list[dict[str, object]]:
    now = now or datetime.utcnow()
    with login_attempts_lock:
        _cleanup_login_attempts(now)
        result = []
        for ip_address, state in login_attempts.items():
            blocked_until = state.get("bloqueado_ate")
            if blocked_until and blocked_until > now:
                result.append(
                    {
                        "ip": ip_address,
                        "attempts": int(state["tentativas"]),
                        "remaining_minutes": max(
                            1,
                            math.ceil((blocked_until - now).total_seconds() / 60),
                        ),
                    }
                )
        return sorted(result, key=lambda item: str(item["ip"]))


def login_response(request: Request, user: User):
    response = RedirectResponse("/home", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        request.app.state.settings.cookie_name,
        create_session_token(user.id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 8,
    )
    return response


def reject_login(
    request: Request,
    db: Session,
    username: str,
    auth_source: str,
    message: str,
    ip_address: str,
):
    log_auth(db, username, auth_source, "failure", message)
    remaining, is_blocked, blocked_minutes = register_failed_attempt(ip_address)
    if is_blocked:
        log_auth(
            db,
            username,
            auth_source,
            "blocked",
            f"IP {ip_address} bloqueado por {blocked_minutes} minutos após {MAX_FAILED_ATTEMPTS} falhas.",
            level="warning",
        )
        error = f"Acesso temporariamente bloqueado. Tente novamente em {blocked_minutes} minutos."
        return render(request, "login.html", {"error": error, "warning": None}, db, 429)
    warning = (
        f"Atenção: {remaining} tentativas restantes antes do bloqueio"
        if remaining < 3
        else None
    )
    return render(
        request,
        "login.html",
        {"error": INVALID_LOGIN_MESSAGE, "warning": warning},
        db,
        401,
    )


def complete_login(request: Request, db: Session, user: User, auth_source: str, ip_address: str):
    reset_login_attempts(ip_address)
    user.last_login_at = datetime.utcnow()
    db.commit()
    log_auth(db, user.username, auth_source, "success", "Login concluido.")
    return login_response(request, user)


@router.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/home", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse("/home", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", {"error": None, "warning": None}, db)


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login(request: Request, db: Session = Depends(get_db)):
    data = await form_data(request)
    username = data.get("username", "").strip()
    password = data.get("password", "")
    ip_address = get_client_ip(request)
    is_blocked, remaining_minutes = check_ip_blocked(ip_address)
    if is_blocked:
        log_auth(
            db,
            username,
            "rate_limit",
            "blocked",
            f"Tentativa de login recusada para IP bloqueado {ip_address}.",
            level="warning",
        )
        return render(
            request,
            "login.html",
            {
                "error": (
                    "Acesso temporariamente bloqueado. "
                    f"Tente novamente em {remaining_minutes} minutos."
                ),
                "warning": None,
            },
            db,
            429,
        )
    settings = request.app.state.settings
    user = db.query(User).filter(User.username == username).first()

    if user and user.is_local:
        if (
            not user.is_active
            or not user.password_hash
            or not verify_password(password, user.password_hash)
        ):
            return reject_login(request, db, username, "local", "Credenciais locais rejeitadas.", ip_address)
        return complete_login(request, db, user, "local", ip_address)

    if not user and settings.local_admin_configured and hmac.compare_digest(username, settings.admin_username):
        if not hmac.compare_digest(password, settings.admin_password):
            return reject_login(request, db, username, "local", "Credenciais locais rejeitadas.", ip_address)
        if not user:
            user = User(
                username=username,
                password_hash=hash_password(password),
                auth_source="local",
                is_local=True,
                is_admin=True,
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        user.password_hash = hash_password(password)
        user.auth_source = "local"
        user.is_local = True
        user.is_admin = True
        user.is_active = True
        return complete_login(request, db, user, "local", ip_address)

    ldap_config = get_ldap_config(db)
    if ldap_config.enabled:
        outcome = authenticate_ldap_user(ldap_config, username, password)
        record_ldap_status(db, ldap_config, outcome.ok, None if outcome.ok else outcome.message)
        if not outcome.ok:
            return reject_login(request, db, username, "ldap", outcome.message, ip_address)
        user = db.query(User).filter(User.username == username, User.is_local.is_(False), User.auth_source == "ldap").first()
        if not user or not user.is_active:
            return reject_login(request, db, username, "ldap", "Usuario LDAP sem acesso local ativo.", ip_address)
        if not user_has_portal_group(user, "portal_admin") and not user_has_portal_group(user, "portal_view"):
            return reject_login(request, db, username, "ldap", "Usuario LDAP sem grupo local permitido.", ip_address)
        user.password_hash = None
        user.is_admin = user_has_portal_group(user, "portal_admin")
        return complete_login(request, db, user, "ldap", ip_address)

    user = db.query(User).filter(User.username == username, User.is_local.is_(True)).first()
    if (
        not user
        or not user.is_active
        or not user.password_hash
        or not verify_password(password, user.password_hash)
    ):
        return reject_login(request, db, username, "local", "Credenciais locais rejeitadas.", ip_address)
    return complete_login(request, db, user, "local", ip_address)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(request.app.state.settings.cookie_name)
    return response
