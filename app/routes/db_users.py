from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db, local_engine
from app.glpi_import import IDENTIFIER_RE, quote_identifier
from app.models import AdminActionLog, DbUser, DbUserDatabase
from app.routes.common import form_lists, get_allowed_databases, render
from app.security import require_admin, verify_csrf


router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys", "reports"}


def redirect_db_users(message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append(f"message={quote(message)}")
    if error:
        params.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/admin/db-users{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def redirect_db_user_edit(db_user_id: int, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append(f"message={quote(message)}")
    if error:
        params.append(f"error={quote(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/admin/db-users/{db_user_id}/edit{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def sql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def validate_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username))


def validate_host_ip(host_ip: str) -> bool:
    if host_ip == "%":
        return False
    try:
        ipaddress.IPv4Address(host_ip)
        return True
    except ValueError:
        return False


def validate_db_user_identity(username: str, host_ip: str) -> bool:
    return validate_username(username) and validate_host_ip(host_ip)


def db_user_admin_engine():
    if not settings.local_db_user_admin_user:
        raise RuntimeError("LOCAL_DB_USER_ADMIN_USER nao configurado.")
    url = settings.mysql_url(
        settings.local_db_user_admin_user,
        settings.local_db_user_admin_pass,
        settings.local_db_host,
        settings.local_db_port,
        settings.local_db_name,
    )
    return create_engine(url, pool_pre_ping=True, future=True)


def list_local_databases() -> list[str]:
    with local_engine.connect() as connection:
        query = text(
            "SELECT SCHEMA_NAME "
            "FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME NOT IN :excluded "
            "ORDER BY SCHEMA_NAME"
        ).bindparams(bindparam("excluded", expanding=True))
        rows = connection.execute(query, {"excluded": tuple(SYSTEM_DATABASES)}).scalars()
        return [row for row in rows if IDENTIFIER_RE.match(row)]


def managed_database_names(db: Session) -> list[str]:
    allowed = set(get_allowed_databases(db))
    return [database_name for database_name in list_local_databases() if database_name in allowed]


def parse_target_databases(parsed: dict[str, list[str]]) -> list[str]:
    raw_databases = parsed.get("target_databases", [])
    return list(dict.fromkeys(item.strip() for item in raw_databases if item and item.strip()))


def validate_target_databases(target_databases: list[str], managed_databases: list[str]) -> str | None:
    if not target_databases:
        return "Selecione uma ou mais databases."
    if any(not IDENTIFIER_RE.match(database_name) for database_name in target_databases):
        return "Database invalida."
    allowed = set(managed_databases)
    not_allowed = [database_name for database_name in target_databases if database_name not in allowed]
    if not_allowed:
        return f"Database nao gerenciada: {', '.join(not_allowed)}."
    existing_databases = existing_database_names(target_databases)
    missing_databases = [database_name for database_name in target_databases if database_name not in existing_databases]
    if missing_databases:
        return f"Database inexistente: {', '.join(missing_databases)}."
    return None


def check_mariadb_user_exists(username: str, host_ip: str) -> bool:
    if not validate_db_user_identity(username, host_ip):
        return True
    engine = db_user_admin_engine()
    try:
        account = f"{sql_string_literal(username)}@{sql_string_literal(host_ip)}"
        with engine.connect() as conn:
            conn.execute(text(f"SHOW GRANTS FOR {account}"))
        return True
    except Exception as exc:
        error_code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
        if error_code == 1141:
            return False
        logger.warning(
            "check_mariadb_user_exists falhou para %s@%s: %s",
            username,
            host_ip,
            exc,
        )
        return True
    finally:
        engine.dispose()


def existing_database_names(database_names: list[str]) -> set[str]:
    if not database_names:
        return set()
    with local_engine.connect() as connection:
        query = text(
            "SELECT SCHEMA_NAME "
            "FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME IN :database_names"
        ).bindparams(bindparam("database_names", expanding=True))
        return set(connection.execute(query, {"database_names": tuple(database_names)}).scalars())


def record_admin_action(db: Session, user, action: str, status_value: str, message: str) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id,
            username=user.username,
            action=action,
            status=status_value,
            message=message,
        )
    )


def log_admin_failure(db: Session, user, action: str, exc: Exception) -> None:
    try:
        record_admin_action(db, user, action, "error", str(exc)[:500])
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Falha ao registrar log administrativo %s.", action)


@router.get("/admin/db-users", response_class=HTMLResponse)
def db_users_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    users = (
        db.query(DbUser)
        .options(joinedload(DbUser.databases))
        .order_by(DbUser.created_at.desc(), DbUser.username.asc())
        .all()
    )
    for db_user in users:
        db_user.exists_in_mariadb = check_mariadb_user_exists(db_user.username, db_user.host_ip)
    return render(
        request,
        "admin_db_users.html",
        {
            "active": "db_users",
            "db_users": users,
            "databases": managed_database_names(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/db-users/create", dependencies=[Depends(verify_csrf)])
async def create_db_user(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    parsed = await form_lists(request)
    data = {key: values[0] for key, values in parsed.items()}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    host_ip = data.get("host_ip", "").strip()
    target_databases = parse_target_databases(parsed)

    if not validate_username(username):
        return redirect_db_users(error="Usuario invalido. Use apenas letras, numeros e _.")
    if len(password) < 8:
        return redirect_db_users(error="Senha deve ter no minimo 8 caracteres.")
    if not validate_host_ip(host_ip):
        return redirect_db_users(error="IP origem invalido. Informe um IPv4, nunca %.")
    database_error = validate_target_databases(target_databases, managed_database_names(db))
    if database_error:
        return redirect_db_users(error=database_error)
    if db.query(DbUser).filter(DbUser.username == username).first():
        return redirect_db_users(error="Usuario DB ja cadastrado.")

    safe_user = sql_string_literal(username)
    safe_host = sql_string_literal(host_ip)
    safe_password = sql_string_literal(password)
    engine = None
    granted_databases: list[str] = []
    failed_databases: list[str] = []
    try:
        engine = db_user_admin_engine()
        with engine.begin() as connection:
            connection.execute(text(f"CREATE USER {safe_user}@{safe_host} IDENTIFIED BY {safe_password}"))
        for database_name in target_databases:
            safe_database = quote_identifier(database_name)
            try:
                with engine.begin() as connection:
                    connection.execute(text(f"GRANT SELECT ON {safe_database}.* TO {safe_user}@{safe_host}"))
                granted_databases.append(database_name)
            except SQLAlchemyError as exc:
                failed_databases.append(database_name)
                logger.exception("Falha ao conceder SELECT em %s para %s@%s: %s", database_name, username, host_ip, exc)
                log_admin_failure(db, user, "db_user_grant_failed", exc)
    except RuntimeError as exc:
        return redirect_db_users(error=str(exc))
    except SQLAlchemyError as exc:
        logger.exception("Falha ao criar usuario DB: %s", exc)
        log_admin_failure(db, user, "db_user_create_failed", exc)
        return redirect_db_users(
            error="Falha ao criar usuario DB. Verifique credenciais, permissoes e se o usuario ja existe no MariaDB."
        )
    finally:
        if engine:
            engine.dispose()

    db_user = DbUser(
        username=username,
        host_ip=host_ip,
        target_database=granted_databases[0] if granted_databases else target_databases[0],
        permission="SELECT",
        is_active=True,
        created_by=user.username,
    )
    db.add(db_user)
    db.flush()
    for database_name in granted_databases:
        db.add(DbUserDatabase(db_user_id=db_user.id, target_database=database_name))
    record_admin_action(
        db,
        user,
        "db_user_create",
        "success" if not failed_databases else "warning",
        f"Usuario DB {username}@{host_ip} criado com SELECT em {', '.join(granted_databases) or '-'}",
    )
    db.commit()
    if failed_databases:
        return redirect_db_users(
            message=f"Usuario DB criado. SELECT concedido em: {', '.join(granted_databases) or 'nenhuma'}.",
            error=f"Falha ao conceder SELECT em: {', '.join(failed_databases)}.",
        )
    return redirect_db_users(message="Usuario DB criado.")


@router.get("/admin/db-users/{db_user_id}/edit", response_class=HTMLResponse)
def edit_db_user_page(db_user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    db_user = (
        db.query(DbUser)
        .options(joinedload(DbUser.databases))
        .filter(DbUser.id == db_user_id)
        .first()
    )
    if not db_user:
        return redirect_db_users(error="Usuario DB nao encontrado.")
    db_user.exists_in_mariadb = check_mariadb_user_exists(db_user.username, db_user.host_ip)
    return render(
        request,
        "admin_db_users.html",
        {
            "active": "db_users",
            "db_users": [],
            "edit_db_user": db_user,
            "selected_databases": {item.target_database for item in db_user.databases},
            "databases": managed_database_names(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/db-users/{db_user_id}/edit", dependencies=[Depends(verify_csrf)])
async def save_db_user_edit(db_user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    db_user = (
        db.query(DbUser)
        .options(joinedload(DbUser.databases))
        .filter(DbUser.id == db_user_id)
        .first()
    )
    if not db_user:
        return redirect_db_users(error="Usuario DB nao encontrado.")

    parsed = await form_lists(request)
    data = {key: values[0] for key, values in parsed.items()}
    host_ip = data.get("host_ip", "").strip()
    new_password = data.get("new_password", "")
    target_databases = parse_target_databases(parsed)

    if not validate_db_user_identity(db_user.username, db_user.host_ip):
        return redirect_db_user_edit(db_user.id, error="Usuario DB local invalido.")
    if host_ip != db_user.host_ip:
        return redirect_db_user_edit(db_user.id, error="Mudanca de IP requer recriar o usuario DB.")
    if new_password and len(new_password) < 8:
        return redirect_db_user_edit(db_user.id, error="Nova senha deve ter no minimo 8 caracteres.")
    database_error = validate_target_databases(target_databases, managed_database_names(db))
    if database_error:
        return redirect_db_user_edit(db_user.id, error=database_error)
    if not check_mariadb_user_exists(db_user.username, db_user.host_ip):
        return redirect_db_user_edit(db_user.id, error="Usuario nao existe no MariaDB. Remova o registro local.")

    current_databases = {item.target_database for item in db_user.databases}
    new_databases = set(target_databases)
    revoke_databases = sorted(current_databases - new_databases)
    grant_databases = sorted(new_databases - current_databases)
    safe_user = sql_string_literal(db_user.username)
    safe_host = sql_string_literal(db_user.host_ip)
    engine = None
    try:
        engine = db_user_admin_engine()
        if new_password:
            safe_password = sql_string_literal(new_password)
            with engine.begin() as connection:
                connection.execute(text(f"ALTER USER {safe_user}@{safe_host} IDENTIFIED BY {safe_password}"))
        for database_name in revoke_databases:
            safe_database = quote_identifier(database_name)
            with engine.begin() as connection:
                connection.execute(text(f"REVOKE SELECT ON {safe_database}.* FROM {safe_user}@{safe_host}"))
        for database_name in grant_databases:
            safe_database = quote_identifier(database_name)
            with engine.begin() as connection:
                connection.execute(text(f"GRANT SELECT ON {safe_database}.* TO {safe_user}@{safe_host}"))
    except RuntimeError as exc:
        return redirect_db_user_edit(db_user.id, error=str(exc))
    except SQLAlchemyError as exc:
        logger.exception("Falha ao editar usuario DB %s@%s: %s", db_user.username, db_user.host_ip, exc)
        log_admin_failure(db, user, "db_user_edit_failed", exc)
        return redirect_db_user_edit(db_user.id, error="Falha ao editar usuario DB. Verifique credenciais e permissoes.")
    finally:
        if engine:
            engine.dispose()

    for database_link in list(db_user.databases):
        if database_link.target_database in revoke_databases:
            db.delete(database_link)
    for database_name in grant_databases:
        db.add(DbUserDatabase(db_user_id=db_user.id, target_database=database_name))
    db_user.target_database = target_databases[0]
    record_admin_action(
        db,
        user,
        "db_user_edit",
        "success",
        f"Usuario DB {db_user.username}@{db_user.host_ip} editado; grants +{grant_databases} -{revoke_databases}",
    )
    db.commit()
    return redirect_db_users(message="Usuario DB atualizado.")


@router.post("/admin/db-users/{db_user_id}/remove", dependencies=[Depends(verify_csrf)])
def remove_missing_db_user(db_user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    db_user = db.get(DbUser, db_user_id)
    if not db_user:
        return redirect_db_users(error="Usuario DB nao encontrado.")
    if check_mariadb_user_exists(db_user.username, db_user.host_ip):
        return redirect_db_users(error="Usuario ainda existe no MariaDB. Use Desativar ou exclua diretamente no banco.")

    username = db_user.username
    host_ip = db_user.host_ip
    db.delete(db_user)
    record_admin_action(
        db,
        user,
        "db_user_remove_orphan",
        "success",
        f"Registro orfao removido para usuario DB inexistente no MariaDB: {username}@{host_ip}",
    )
    db.commit()
    return redirect_db_users(message="Registro local removido.")


@router.post("/admin/db-users/{db_user_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_db_user(db_user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    db_user = db.get(DbUser, db_user_id)
    if not db_user:
        return redirect_db_users(error="Usuario DB nao encontrado.")

    action_sql = "ACCOUNT LOCK" if db_user.is_active else "ACCOUNT UNLOCK"
    action_name = "db_user_disable" if db_user.is_active else "db_user_enable"
    label = "desativado" if db_user.is_active else "reativado"
    safe_user = sql_string_literal(db_user.username)
    safe_host = sql_string_literal(db_user.host_ip)
    engine = None
    try:
        engine = db_user_admin_engine()
        with engine.begin() as connection:
            connection.execute(text(f"ALTER USER {safe_user}@{safe_host} {action_sql}"))
    except RuntimeError as exc:
        return redirect_db_users(error=str(exc))
    except SQLAlchemyError as exc:
        logger.exception("Falha ao alterar usuario DB %s@%s: %s", db_user.username, db_user.host_ip, exc)
        log_admin_failure(db, user, f"{action_name}_failed", exc)
        return redirect_db_users(error="Falha ao alterar usuario DB. Verifique credenciais e permissoes no MariaDB.")
    finally:
        if engine:
            engine.dispose()

    db_user.is_active = not db_user.is_active
    record_admin_action(
        db,
        user,
        action_name,
        "success",
        f"Usuario DB {db_user.username}@{db_user.host_ip} {label}",
    )
    db.commit()
    return redirect_db_users(message=f"Usuario DB {label}.")
