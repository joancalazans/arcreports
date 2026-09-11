from __future__ import annotations

import logging
import threading
import json
from datetime import datetime
from urllib.parse import quote, urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import create_engine, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.connector_adapters import get_adapter, get_table_load_profile
from app.crypto import encrypt_password, is_encrypted
from app.database import SessionLocal, get_db
from app.glpi_import import (
    IDENTIFIER_RE,
    build_source_engine,
    ensure_target_database,
    get_connection_config,
    run_connector_import as execute_connector_import,
    request_cancel,
    seed_connector_settings,
    test_glpi_connection,
    translate_connection_error,
)
from app.models import (
    AdminActionLog,
    DashboardSource,
    GlpiImportLog,
    ConnectorConfig,
    ConnectorRun,
    ConnectorSyncTable,
    Report,
    SystemConfig,
)
from app.routes.common import form_data, form_lists, render
from app.security import require_importacao, verify_csrf

logger = logging.getLogger(__name__)


router = APIRouter()
import_lock = threading.Lock()
GLPI_SCHEDULE_ACTIVE_KEY = "glpi_schedule_active"
GLPI_SCHEDULE_DAYS_KEY = "glpi_schedule_dias"
GLPI_FULL_SCHEDULE_ACTIVE_KEY = "glpi_full_schedule_active"
DEFAULT_GLPI_SCHEDULE_DAYS = "0,1,2,3,4,5,6"
WEEK_DAYS = [
    (1, "Seg"),
    (2, "Ter"),
    (3, "Qua"),
    (4, "Qui"),
    (5, "Sex"),
    (6, "Sab"),
    (0, "Dom"),
]


def reset_dashboard_sources_cache() -> None:
    from app.routes.dashboard import reset_dashboard_tables_cache

    reset_dashboard_tables_cache()


def parse_day_values(value: str | None, default: str | None = None) -> set[int]:
    raw_value = value if value not in (None, "") else default
    days: set[int] = set()
    for item in (raw_value or "").split(","):
        raw = item.strip()
        if raw.isdigit():
            day = int(raw)
            if 0 <= day <= 6:
                days.add(day)
    return days


def serialize_day_values(values) -> str:
    days: list[int] = []
    for value in values:
        raw = str(value).strip()
        if raw.isdigit():
            day = int(raw)
            if 0 <= day <= 6 and day not in days:
                days.append(day)
    return ",".join(str(day) for day in sorted(days))


def get_system_config_value(db: Session, key: str, default: str = "") -> str:
    config = db.get(SystemConfig, key)
    return config.value if config and config.value is not None else default


def set_system_config_value(db: Session, key: str, value: str) -> None:
    config = db.get(SystemConfig, key)
    if not config:
        config = SystemConfig(key=key, value=value)
        db.add(config)
    else:
        config.value = value


def connector_redirect(
    config: ConnectorConfig | None,
    message: str | None = None,
    error: str | None = None,
    admin_sql: str | None = None,
    warning: str | None = None,
) -> RedirectResponse:
    params = []
    if config:
        params.append(f"connector_id={config.id}")
    if message:
        params.append(f"message={quote(message)}")
    if error:
        params.append(f"error={quote(error)}")
    if warning:
        params.append(f"warning={quote(warning)}")
    if admin_sql:
        params.append(f"admin_sql={quote(admin_sql)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/admin/connectors{suffix}", status_code=status.HTTP_303_SEE_OTHER)


VALID_DB_TYPES = {"mysql", "mariadb", "postgresql"}


def normalize_db_type(value: str) -> str:
    db_type = str(value or "").strip().lower()
    if db_type not in VALID_DB_TYPES:
        accepted = ", ".join(sorted(VALID_DB_TYPES))
        raise ValueError(
            f"Tipo de banco inválido: '{value}'. "
            f"Valores aceitos: {accepted}."
        )
    return db_type


def form_value(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value) if value is not None else default


def parse_optional_int(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    return int(raw) if raw.isdigit() else None


def apply_connector_form(config: ConnectorConfig, data: dict, preserve_password: bool = True) -> None:
    db_type = normalize_db_type(form_value(data, "db_type", "mysql"))
    connector_type = (form_value(data, "connector_type") or "custom").strip().lower()
    config.name = (form_value(data, "name") or connector_type.upper()).strip()
    config.connector_type = connector_type
    config.db_type = db_type
    config.host = form_value(data, "host").strip()
    config.port = form_value(data, "port").strip() or ("5432" if db_type == "postgresql" else "3306")
    config.database_name = form_value(data, "database_name").strip()
    config.schema_name = form_value(data, "schema_name").strip() or None
    config.initial_days = parse_optional_int(form_value(data, "initial_days"))
    config.username = form_value(data, "username").strip()
    password = form_value(data, "password")
    if password:
        config.password = (
            password
            if is_encrypted(password)
            else encrypt_password(password, settings.encryption_key)
        )
    elif not preserve_password:
        config.password = ""
    config.table_prefix = form_value(data, "table_prefix").strip()
    config.target_database = form_value(data, "target_database").strip() or f"{connector_type}_local"
    config.suggested_frequency = form_value(data, "suggested_frequency").strip()
    config.full_schedule_horarios = form_value(data, "full_schedule_horarios").strip() or None
    config.full_schedule_dias = serialize_day_values(data.get("full_schedule_dias", [])) or None
    import_mode = form_value(data, "import_mode", "automatic")
    if import_mode not in ("automatic", "custom"):
        import_mode = "automatic"
    config.import_mode = import_mode
    table_whitelist = form_value(data, "table_whitelist", None)
    config.table_whitelist = table_whitelist if table_whitelist else None
    config.is_active = data.get("is_active") == "on"


def log_admin_action(
    db: Session,
    user,
    action: str,
    detail: str,
    status_value: str = "success",
    table_name: str | None = None,
) -> None:
    db.add(
        AdminActionLog(
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", None),
            action=action,
            status=status_value,
            message=detail,
            table_name=table_name,
        )
    )


async def request_payload(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return await form_lists(request)


def get_table_metadata(adapter, connection, table_name, schema) -> dict:
    db_type = adapter.__class__.__name__.lower()
    if "mysql" in db_type:
        row = connection.execute(
            text(
                "SELECT table_rows, "
                "ROUND((data_length + index_length) /1024/1024, 2) AS size_mb "
                "FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :table"
            ),
            {"schema": schema, "table": table_name},
        ).mappings().first()
    else:
        row = connection.execute(
            text(
                "SELECT reltuples::bigint AS table_rows, "
                "ROUND(pg_total_relation_size(quote_ident(:table))::numeric / 1024 / 1024, 2) AS size_mb "
                "FROM pg_class WHERE relname = :table"
            ),
            {"table": table_name},
        ).mappings().first()
    return {
        "rows": int(row["table_rows"] or 0) if row else 0,
        "size_mb": float(row["size_mb"] or 0) if row else 0.0,
        "is_large": bool(adapter.is_large_table(table_name)),
    }


def format_import_duration(started_at: datetime, finished_at: datetime) -> str:
    total_seconds = int((finished_at - started_at).total_seconds())
    if total_seconds >= 60:
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}m {seconds}s"
    return f"{total_seconds}s"


def record_connector_import_admin_log(
    db: Session,
    mode: str,
    username: str,
    started_at: datetime,
    finished_at: datetime,
    user_id: int | None = None,
    connector_type: str | None = None,
) -> None:
    connector_filter = "AND connector_type = :connector_type" if connector_type else ""
    params = {"inicio_execucao": started_at}
    if connector_type:
        params["connector_type"] = connector_type
    counts = db.execute(
        text(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS sucesso, "
            "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS erros "
            "FROM connector_runs "
            f"WHERE started_at >= :inicio_execucao {connector_filter}"
        ),
        params,
    ).mappings().one()
    tables_total = int(counts["total"] or 0)
    tables_success = int(counts["sucesso"] or 0)
    tables_error = int(counts["erros"] or 0)

    if tables_error > 0:
        details = f"{tables_success}/{tables_total} tabelas, {tables_error} erros"
    else:
        details = f"{tables_success}/{tables_total} tabelas sincronizadas"

    if tables_success == 0:
        status_value = "error"
    elif tables_error > 0:
        status_value = "warning"
    else:
        status_value = "success"

    duration = format_import_duration(started_at, finished_at)
    action_mode = "full" if mode == "full" else "incremental"
    db.add(
        AdminActionLog(
            user_id=user_id,
            username=username,
            action=f"glpi_import_{action_mode}",
            status=status_value,
            message=f"{details} em {duration}",
        )
    )
    db.commit()


def run_connector_import_background(
    mode: str,
    table_id: int | None = None,
    username: str = "manual",
    user_id: int | None = None,
    connector_type: str | None = None,
) -> None:
    if not import_lock.acquire(blocking=False):
        return

    db = SessionLocal()
    started_at = datetime.utcnow()
    try:
        execute_connector_import(db, mode, table_id, connector_type)
    except Exception as exc:
        db.add(GlpiImportLog(level="error", message=f"Importacao em segundo plano: {exc}"))
        db.commit()
    finally:
        finished_at = datetime.utcnow()
        try:
            record_connector_import_admin_log(db, mode, username, started_at, finished_at, user_id, connector_type)
        except Exception as exc:
            db.rollback()
            db.add(GlpiImportLog(level="error", message=f"Log administrativo da importacao: {exc}"))
            db.commit()
        db.close()
        import_lock.release()


def connector_display_name(config: ConnectorConfig) -> str:
    return config.name or config.connector_type


def connector_table_names(db: Session, config: ConnectorConfig) -> list[str]:
    rows = (
        db.query(ConnectorSyncTable.table_name)
        .filter(ConnectorSyncTable.connector_type == config.connector_type)
        .order_by(ConnectorSyncTable.table_name.asc())
        .all()
    )
    return [row[0] for row in rows if row[0]]


def connector_database_exists(db: Session, target_database: str) -> bool:
    result = db.execute(
        text(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME = :db_name"
        ),
        {"db_name": target_database},
    ).first()
    return result is not None


def connector_blocking_refs(db: Session, config: ConnectorConfig) -> list[dict]:
    try:
        refs: list[dict] = []
        seen: set[tuple[str, int]] = set()
        table_names = connector_table_names(db, config)

        if table_names:
            sources = (
                db.query(DashboardSource)
                .filter(DashboardSource.source_table.in_(table_names))
                .order_by(DashboardSource.name.asc())
                .all()
            )
            for source in sources:
                key = ("fonte de dashboard", source.id)
                if key not in seen:
                    refs.append({"type": "fonte de dashboard", "name": source.name, "id": source.id})
                    seen.add(key)

            reports_by_table = (
                db.query(Report)
                .filter(Report.destination_table.in_(table_names))
                .order_by(Report.name.asc())
                .all()
            )
            for report in reports_by_table:
                key = ("relatório", report.id)
                if key not in seen:
                    refs.append({"type": "relatório", "name": report.name, "id": report.id})
                    seen.add(key)

        target_database = (config.target_database or "").strip()
        if target_database:
            reports_by_sql = (
                db.query(Report)
                .filter(
                    or_(
                        Report.sql_query.like(f"%`{target_database}`.%"),
                        Report.sql_query.like(f"%{target_database}.%"),
                    )
                )
                .order_by(Report.name.asc())
                .all()
            )
            for report in reports_by_sql:
                key = ("relatório", report.id)
                if key not in seen:
                    refs.append({"type": "relatório", "name": report.name, "id": report.id})
                    seen.add(key)

        return refs
    except SQLAlchemyError as exc:
        logger.warning("connector_blocking_refs falhou: %s", exc)
        return []


def connector_delete_check_payload(db: Session, config: ConnectorConfig) -> dict:
    blocking_refs = connector_blocking_refs(db, config)
    target_database = config.target_database
    return {
        "blocked": len(blocking_refs) > 0,
        "blocking_refs": blocking_refs,
        "database_exists": connector_database_exists(db, target_database),
        "target_database": target_database,
        "connector_name": connector_display_name(config),
    }


@router.get("/admin/connectors", response_class=HTMLResponse)
def connectors_page(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    seed_connector_settings(db)
    configs = db.query(ConnectorConfig).order_by(ConnectorConfig.connector_type.asc(), ConnectorConfig.id.asc()).all()
    selected_id = request.query_params.get("connector_id", "")
    config = None
    if selected_id.isdigit():
        config = db.get(ConnectorConfig, int(selected_id))
    if not config:
        selected_type = request.query_params.get("connector_type", "")
        if selected_type:
            config = (
                db.query(ConnectorConfig)
                .filter(ConnectorConfig.connector_type == selected_type)
                .order_by(ConnectorConfig.id.asc())
                .first()
            )
    if not config:
        config = configs[0] if configs else get_connection_config(db)
    selected_connector_type = config.connector_type if config else "glpi"
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    page_size = 50
    tables_query = db.query(ConnectorSyncTable).filter(ConnectorSyncTable.connector_type == selected_connector_type)
    total_tables = tables_query.count()
    total_pages = max(1, (total_tables + page_size - 1) // page_size)
    offset = (page - 1) * page_size
    tables = (
        tables_query
        .order_by(ConnectorSyncTable.table_name.asc())
        .offset(offset)
        .limit(page_size)
        .all()
    )
    pagination_links = []
    base_query_params = dict(request.query_params)
    for page_number in range(1, total_pages + 1):
        query_params = {**base_query_params, "page": str(page_number)}
        pagination_links.append((page_number, f"/admin/connectors?{urlencode(query_params)}"))
    prev_url = None
    next_url = None
    if page > 1:
        prev_params = {**base_query_params, "page": str(page - 1)}
        prev_url = f"/admin/connectors?{urlencode(prev_params)}"
    if page < total_pages:
        next_params = {**base_query_params, "page": str(page + 1)}
        next_url = f"/admin/connectors?{urlencode(next_params)}"
    runs = (
        db.query(ConnectorRun)
        .filter(ConnectorRun.connector_type == selected_connector_type)
        .order_by(ConnectorRun.started_at.desc())
        .limit(50)
        .all()
    )
    logs = db.query(GlpiImportLog).order_by(GlpiImportLog.created_at.desc()).limit(10).all()
    last_run = (
        db.query(ConnectorRun)
        .filter(ConnectorRun.connector_type == selected_connector_type)
        .order_by(ConnectorRun.started_at.desc())
        .first()
    )
    running_import = (
        db.query(ConnectorRun)
        .filter(ConnectorRun.connector_type == selected_connector_type, ConnectorRun.status == "running")
        .order_by(ConnectorRun.started_at.desc())
        .first()
    )
    table_counts = dict(
        db.execute(
            text(
                "SELECT connector_type, COUNT(*) AS total "
                "FROM connector_sync_tables "
                "GROUP BY connector_type"
            )
        ).all()
    )
    running_by_type = {r[0] for r in db.query(ConnectorRun.connector_type).filter(ConnectorRun.status == "running").all()}
    last_runs_by_type = {}
    for item in configs:
        last_runs_by_type[item.connector_type] = (
            db.query(ConnectorRun)
            .filter(ConnectorRun.connector_type == item.connector_type)
            .order_by(ConnectorRun.started_at.desc())
            .first()
        )
    full_days_by_config = {item.id: parse_day_values(item.full_schedule_dias) for item in configs}
    active_table_count = tables_query.filter(ConnectorSyncTable.is_active.is_(True)).count()
    imported_table_count = (
        tables_query
        .filter(
            ConnectorSyncTable.is_active.is_(True),
            ConnectorSyncTable.last_success_at.isnot(None),
        )
        .count()
    )
    scheduler_status = getattr(request.app.state, "scheduler_status", lambda: {"active": False, "times": []})()
    schedule_config = db.query(SystemConfig).filter(SystemConfig.key == GLPI_SCHEDULE_ACTIVE_KEY).first()
    glpi_schedule_active = schedule_config.value == "true" if schedule_config else True
    schedule_days_config = db.get(SystemConfig, GLPI_SCHEDULE_DAYS_KEY)
    incremental_days = parse_day_values(
        schedule_days_config.value if schedule_days_config else None,
        DEFAULT_GLPI_SCHEDULE_DAYS,
    )
    full_schedule_active = get_system_config_value(db, GLPI_FULL_SCHEDULE_ACTIVE_KEY, "false") == "true"
    return render(
        request,
        "glpi_import.html",
        {
            "active": "glpi_import",
            "configs": configs,
            "config": config,
            "selected_connector_type": selected_connector_type,
            "table_counts": table_counts,
            "last_runs_by_type": last_runs_by_type,
            "running_by_type": running_by_type,
            "tables": tables,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_tables": total_tables,
            "pagination_links": pagination_links,
            "prev_page_url": prev_url,
            "next_page_url": next_url,
            "table_page_start": offset + 1 if total_tables and tables else 0,
            "table_page_end": min(offset + len(tables), total_tables),
            "runs": runs,
            "logs": logs,
            "last_run": last_run,
            "running_import": running_import,
            "active_table_count": active_table_count,
            "imported_table_count": imported_table_count,
            "scheduler_active": scheduler_status["active"],
            "scheduler_times": scheduler_status["times"],
            "glpi_schedule_active": glpi_schedule_active,
            "week_days": WEEK_DAYS,
            "incremental_days": incremental_days,
            "full_days_by_config": full_days_by_config,
            "full_schedule_active": full_schedule_active,
            "proximas_execucoes": ", ".join(scheduler_status["times"]) if scheduler_status["times"] else "nenhuma",
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "warning": request.query_params.get("warning"),
            "admin_sql": request.query_params.get("admin_sql"),
        },
        db,
    )


@router.get("/admin/connectors/")
def connectors_page_slash():
    return RedirectResponse("/admin/connectors", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/connectors/{connector_id}/cancel", dependencies=[Depends(verify_csrf)])
def cancel_connector_import(connector_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    config = db.get(ConnectorConfig, connector_id)
    if not config:
        return JSONResponse({"error": "Conector não encontrado."}, status_code=404)
    request_cancel(config.connector_type)
    db.add(AdminActionLog(user_id=getattr(user, "id", None), username=getattr(user, "username", str(user)), action="connector_cancel", status="success", message=f"Cancelamento solicitado: {config.name or config.connector_type}."))
    db.commit()
    return JSONResponse({"success": True, "message": "Cancelamento solicitado. O ETL para após a tabela atual."})


@router.get("/admin/connectors/{connector_id}/delete-check")
def delete_connector_check(connector_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    config = db.get(ConnectorConfig, connector_id)
    if not config:
        return JSONResponse({"error": "Conector não encontrado."})
    if config.is_active:
        return JSONResponse({"error": "Desative o conector antes de excluir."})
    return JSONResponse(connector_delete_check_payload(db, config))


@router.post("/admin/connectors/{connector_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_connector(connector_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_lists(request)
    config = db.get(ConnectorConfig, connector_id)
    if not config:
        return connector_redirect(None, error="Conector não encontrado.")
    if config.is_active:
        return connector_redirect(config, error="Desative o conector antes de excluir.")

    blocking_refs = connector_blocking_refs(db, config)
    if blocking_refs:
        details = "; ".join(f"{ref['type']} {ref['name']} (ID {ref['id']})" for ref in blocking_refs)
        return connector_redirect(config, error=f"Remova as referências antes de excluir: {details}.")

    name = connector_display_name(config)
    target_db = config.target_database
    drop_database = form_value(data, "drop_database") == "on"
    database_exists = connector_database_exists(db, target_db)
    connector_type = config.connector_type

    try:
        run_ids = [
            row.id
            for row in db.query(ConnectorRun.id)
            .filter(ConnectorRun.connector_type == connector_type)
            .all()
        ]
        if run_ids:
            db.query(GlpiImportLog).filter(GlpiImportLog.run_id.in_(run_ids)).delete(
                synchronize_session=False
            )
        db.query(ConnectorRun).filter(ConnectorRun.connector_type == connector_type).delete(
            synchronize_session=False
        )
        db.query(ConnectorSyncTable).filter(ConnectorSyncTable.connector_type == connector_type).delete(
            synchronize_session=False
        )
        db.delete(config)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Erro ao excluir conector %s: %s", name, exc)
        return connector_redirect(
            None,
            error="Erro ao excluir conector: verifique o log do sistema.",
        )

    if drop_database and database_exists:
        if not IDENTIFIER_RE.match(target_db or ""):
            db.add(
                AdminActionLog(
                    user_id=getattr(user, "id", None),
                    username=getattr(user, "username", None),
                    action="connector_db_drop",
                    status="error",
                    message=f"Database {target_db} não excluída: identificador inválido.",
                )
            )
            db.commit()
        else:
            ddl_engine = create_engine(
                settings.mysql_url(
                    settings.local_db_user,
                    settings.local_db_pass,
                    settings.local_db_host,
                    settings.local_db_port,
                    settings.local_db_name,
                )
            )
            try:
                with ddl_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    conn.execute(text(f"DROP DATABASE IF EXISTS `{target_db}`"))
                db.add(
                    AdminActionLog(
                        user_id=getattr(user, "id", None),
                        username=getattr(user, "username", None),
                        action="connector_db_drop",
                        status="success",
                        message=f"Database {target_db} excluída junto com conector {name}.",
                    )
                )
                db.commit()
            except SQLAlchemyError as exc:
                db.add(
                    AdminActionLog(
                        user_id=getattr(user, "id", None),
                        username=getattr(user, "username", None),
                        action="connector_db_drop",
                        status="error",
                        message=f"Falha ao excluir database {target_db}: {exc}",
                    )
                )
                db.commit()
            finally:
                ddl_engine.dispose()

    log_admin_action(db, user, "connector_delete", f"Conector {name} excluído.")
    db.commit()
    reset_dashboard_sources_cache()
    return RedirectResponse(
        f"/admin/connectors?message={quote(f'Conector {name} excluído.')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/connectors/discover", dependencies=[Depends(verify_csrf)])
async def discover_connector_tables(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await request_payload(request)
    connector_id = form_value(data, "connector_id", "")
    config = db.get(ConnectorConfig, int(connector_id)) if connector_id.isdigit() else None
    if not config:
        config = ConnectorConfig(
            name=form_value(data, "name", "Descoberta temporaria") or "Descoberta temporaria",
            connector_type=(form_value(data, "connector_type", "custom") or "custom").strip().lower(),
            db_type=normalize_db_type(form_value(data, "db_type", "mysql")),
            host="",
            port="3306",
            database_name="",
            target_database="custom_local",
            table_prefix="",
            username="",
            password="",
            suggested_frequency="",
            is_active=True,
        )
        apply_connector_form(config, data, preserve_password=False)

    engine = build_source_engine(config)
    adapter = get_adapter(config.db_type)
    try:
        with engine.connect() as connection:
            schema = config.schema_name or config.database_name
            if config.db_type == "postgresql":
                schema = config.schema_name or "public"
            whitelist = set(config.whitelist_tables)
            table_names = adapter.get_tables(connection, config.table_prefix, schema)
            tables = []
            for table_name in table_names:
                metadata = get_table_metadata(adapter, connection, table_name, schema)
                tables.append(
                    {
                        "name": table_name,
                        "rows": metadata["rows"],
                        "size_mb": metadata["size_mb"],
                        "is_large": metadata["is_large"],
                        "in_whitelist": table_name in whitelist,
                    }
                )
        return JSONResponse({"success": True, "tables": tables})
    except SQLAlchemyError as exc:
        return JSONResponse(
            {
                "success": False,
                "error": translate_connection_error(exc, config.host, config.port, config.database_name),
            }
        )
    except Exception as exc:
        return JSONResponse({"success": False, "error": f"Erro de conexao: {exc}"})
    finally:
        engine.dispose()


@router.post("/admin/connectors/new", dependencies=[Depends(verify_csrf)])
async def create_connector(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_lists(request)
    logger.warning(
        "[create_connector] connector_type=%s db_type=%s name=%s",
        data.get("connector_type", [""])[0],
        data.get("db_type", [""])[0],
        data.get("name", [""])[0],
    )
    config = ConnectorConfig(
        name="",
        connector_type="custom",
        db_type="mysql",
        host="",
        port="3306",
        database_name="",
        target_database="custom_local",
        table_prefix="",
        username="",
        password="",
        suggested_frequency="",
        is_active=True,
    )
    apply_connector_form(config, data, preserve_password=False)
    success, target_error = ensure_target_database(config)
    db.add(config)
    log_admin_action(
        db,
        user,
        "connector_create",
        f"Conector {config.name} ({config.connector_type}) criado.",
    )
    db.commit()
    db.refresh(config)
    reset_dashboard_sources_cache()
    if success:
        return connector_redirect(config, f"Database {config.target_database} criada ✅")
    return connector_redirect(config, warning=target_error)


@router.post("/admin/connectors/{connector_id}/edit", dependencies=[Depends(verify_csrf)])
async def edit_connector(connector_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    config = db.get(ConnectorConfig, connector_id)
    if not config:
        return connector_redirect(None, error="Conector nao encontrado.")
    data = await form_lists(request)
    old_connector_type = config.connector_type
    apply_connector_form(config, data)
    if config.connector_type != old_connector_type:
        db.query(ConnectorSyncTable).filter(ConnectorSyncTable.connector_type == old_connector_type).update(
            {ConnectorSyncTable.connector_type: config.connector_type},
            synchronize_session=False,
        )
        db.query(ConnectorRun).filter(ConnectorRun.connector_type == old_connector_type).update(
            {ConnectorRun.connector_type: config.connector_type},
            synchronize_session=False,
        )
    log_admin_action(db, user, "connector_edit", f"Conector {config.name} atualizado.")
    db.commit()
    success, target_error = ensure_target_database(config)
    reset_dashboard_sources_cache()
    if success:
        return connector_redirect(config, "Conector atualizado.")
    return connector_redirect(config, error=target_error)


@router.post("/admin/connectors/{connector_id}/toggle", dependencies=[Depends(verify_csrf)])
def toggle_connector(connector_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    config = db.get(ConnectorConfig, connector_id)
    if not config:
        return connector_redirect(None, error="Conector nao encontrado.")
    config.is_active = not config.is_active
    label = "ativado" if config.is_active else "desativado"
    log_admin_action(db, user, "connector_toggle", f"Conector {config.name} {label}.")
    db.commit()
    reset_dashboard_sources_cache()
    return connector_redirect(config, f"Conector {label}.")


@router.post("/admin/connectors/connection", dependencies=[Depends(verify_csrf)])
async def save_glpi_import_connection(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_lists(request)
    connector_id = form_value(data, "connector_id")
    config = db.get(ConnectorConfig, int(connector_id)) if connector_id.isdigit() else get_connection_config(db)
    if not config:
        return connector_redirect(None, error="Conector ativo nao encontrado.")
    apply_connector_form(config, data)
    db.commit()
    success, target_error = ensure_target_database(config)
    reset_dashboard_sources_cache()
    if success:
        return connector_redirect(config, "Configuracao salva.")
    return connector_redirect(config, error=target_error)


@router.post("/admin/connectors/schedule", dependencies=[Depends(verify_csrf)])
async def save_glpi_import_schedule(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_lists(request)
    connector_id = form_value(data, "connector_id")
    config = db.get(ConnectorConfig, int(connector_id)) if connector_id.isdigit() else get_connection_config(db)
    if not config:
        return connector_redirect(None, error="Conector ativo nao encontrado.")
    config.suggested_frequency = (data.get("suggested_frequency", [""])[0] or "").strip()
    set_system_config_value(
        db,
        GLPI_SCHEDULE_ACTIVE_KEY,
        "true" if data.get("glpi_schedule_active", [""])[0] == "on" else "false",
    )
    set_system_config_value(
        db,
        GLPI_SCHEDULE_DAYS_KEY,
        serialize_day_values(data.get("schedule_dias", [])) or DEFAULT_GLPI_SCHEDULE_DAYS,
    )
    full_schedule_active = data.get("full_schedule_active", [""])[0] == "on"
    set_system_config_value(db, GLPI_FULL_SCHEDULE_ACTIVE_KEY, "true" if full_schedule_active else "false")
    config.full_schedule_horarios = (data.get("full_schedule_horarios", [""])[0] or "").strip() or None
    config.full_schedule_dias = serialize_day_values(data.get("full_schedule_dias", [])) or None
    db.add(
        AdminActionLog(
            user_id=user.id,
            username=user.username,
            action="glpi_schedule_save",
            status="success",
            message="Agendamento GLPI atualizado",
        )
    )
    db.commit()
    reload_schedule = getattr(request.app.state, "reload_glpi_import_schedule", None)
    if reload_schedule:
        reload_schedule()
    return connector_redirect(config, "Agendamento salvo.")


@router.post("/admin/connectors/schedule-toggle", dependencies=[Depends(verify_csrf)])
def toggle_glpi_import_schedule(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    config = db.query(SystemConfig).filter(SystemConfig.key == GLPI_SCHEDULE_ACTIVE_KEY).first()
    if not config:
        config = SystemConfig(key=GLPI_SCHEDULE_ACTIVE_KEY, value="true")
        db.add(config)
        db.flush()
    config.value = "false" if config.value == "true" else "true"
    db.add(
        AdminActionLog(
            user_id=user.id,
            username=user.username,
            action="glpi_schedule_toggle",
            status="success",
            message=f"Novo estado: {config.value}",
        )
    )
    db.commit()
    state_label = "ativado" if config.value == "true" else "pausado"
    return RedirectResponse(
        f"/admin/connectors?message=Agendamento%20GLPI%20{state_label}.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/connectors/test", dependencies=[Depends(verify_csrf)])
def test_glpi_import_connection(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    connector_id = request.query_params.get("connector_id", "")
    config = db.get(ConnectorConfig, int(connector_id)) if connector_id.isdigit() else get_connection_config(db)
    ok, message = test_glpi_connection(db, config.connector_type if config else None)
    log_admin_action(
        db,
        user,
        "connector_test",
        f"Teste de conexao: {'sucesso' if ok else 'falha'}.",
        "success" if ok else "error",
    )
    db.commit()
    query_key = "message" if ok else "error"
    connector_param = f"connector_id={config.id}&" if config else ""
    return RedirectResponse(f"/admin/connectors?{connector_param}{query_key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/connectors/{connector_id}/whitelist", dependencies=[Depends(verify_csrf)])
async def save_connector_whitelist(connector_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    config = db.get(ConnectorConfig, connector_id)
    if not config:
        return JSONResponse({"success": False, "error": "Conector nao encontrado."}, status_code=404)

    previous_tables = set(config.whitelist_tables)
    data = await request_payload(request)
    import_mode = form_value(data, "import_mode", "automatic")
    if import_mode not in ("automatic", "custom"):
        import_mode = "automatic"
    raw_tables = data.get("tables", [])
    if isinstance(raw_tables, str):
        try:
            parsed_tables = json.loads(raw_tables)
            raw_tables = parsed_tables if isinstance(parsed_tables, list) else [raw_tables]
        except json.JSONDecodeError:
            raw_tables = [raw_tables] if raw_tables else []
    tables = []
    for table_name in raw_tables or []:
        value = str(table_name).strip()
        if value and IDENTIFIER_RE.match(value) and value not in tables:
            tables.append(value)

    config.import_mode = import_mode
    config.table_whitelist = json.dumps(tables) if import_mode == "custom" else None
    table_count = len(tables) if import_mode == "custom" else 0
    current_tables = set(tables) if import_mode == "custom" else set()
    for table_name in sorted(previous_tables - current_tables):
        log_admin_action(
            db,
            user,
            "connector_table_remove",
            f"Tabela '{table_name}' removida da whitelist do conector '{connector_display_name(config)}'.",
            table_name=table_name,
        )
    log_admin_action(
        db,
        user,
        "connector_mode_change",
        f"Modo alterado para {import_mode}. {table_count} tabelas na whitelist.",
    )
    db.commit()
    return JSONResponse({"success": True, "import_mode": config.import_mode, "table_count": table_count})


@router.post("/admin/connectors/tables", dependencies=[Depends(verify_csrf)])
async def save_glpi_import_tables(request: Request, db: Session = Depends(get_db)):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    connector_id = data.get("connector_id", "")
    config = db.get(ConnectorConfig, int(connector_id)) if connector_id.isdigit() else get_connection_config(db)
    if not config:
        return connector_redirect(None, error="Conector ativo nao encontrado.")
    table_ids = []
    for key in data.keys():
        if key.startswith(("active_", "load_type_", "incremental_column_")):
            raw_id = key.rsplit("_", 1)[-1]
            if raw_id.isdigit():
                table_ids.append(int(raw_id))
    tables = (
        db.query(ConnectorSyncTable)
        .filter(
            ConnectorSyncTable.connector_type == config.connector_type,
            ConnectorSyncTable.id.in_(set(table_ids) or {-1}),
        )
        .all()
    )
    updated_count = 0
    for table in tables:
        table.is_active = data.get(f"active_{table.id}") == "on"
        load_type = data.get(f"load_type_{table.id}", "incremental")
        table.load_type = load_type if load_type in {"full", "incremental"} else "incremental"
        table.configured_load_type = table.load_type
        incremental_column = data.get(f"incremental_column_{table.id}", "").strip()
        table.incremental_column = incremental_column if incremental_column and IDENTIFIER_RE.match(incremental_column) else None
        updated_count += 1

    new_table_name = data.get("new_table_name", "").strip()
    if new_table_name:
        if not IDENTIFIER_RE.match(new_table_name):
            return connector_redirect(config, error="Nome de tabela invalido.")
        existing = (
            db.query(ConnectorSyncTable)
            .filter(
                ConnectorSyncTable.connector_type == config.connector_type,
                ConnectorSyncTable.table_name == new_table_name,
            )
            .first()
        )
        if not existing:
            new_incremental_column = data.get("new_incremental_column", "").strip()
            profile_load_type, profile_column = get_table_load_profile(
                config.connector_type,
                new_table_name,
            )
            new_load_type = data.get("new_load_type", profile_load_type)
            configured_load_type = (
                new_load_type
                if new_load_type in {"full", "incremental"}
                else profile_load_type
            )
            configured_column = None
            if configured_load_type == "incremental":
                configured_column = (
                    new_incremental_column
                    if new_incremental_column and IDENTIFIER_RE.match(new_incremental_column)
                    else profile_column
                )
            db.add(
                ConnectorSyncTable(
                    connector_type=config.connector_type,
                    table_name=new_table_name,
                    is_active=True,
                    load_type=configured_load_type,
                    configured_load_type=configured_load_type,
                    incremental_column=configured_column,
                )
            )
            updated_count += 1
    log_admin_action(db, user, "connector_tables_save", f"{updated_count} tabelas atualizadas.")
    db.commit()
    return connector_redirect(config, "Tabelas salvas.")


@router.post("/admin/connectors/run", dependencies=[Depends(verify_csrf)])
async def run_connector_import(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = require_importacao(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    mode = data.get("mode", "incremental")
    table_id_raw = data.get("table_id", "")
    table_id = int(table_id_raw) if table_id_raw.isdigit() else None
    connector_id = data.get("connector_id", "")
    config = db.get(ConnectorConfig, int(connector_id)) if connector_id.isdigit() else get_connection_config(db)
    connector_type = config.connector_type if config else None

    running = (
        db.query(ConnectorRun)
        .filter(ConnectorRun.status == "running")
        .first()
    )
    if running:
        return RedirectResponse(
            "/admin/connectors?message=Ja%20existe%20uma%20importacao%20em%20andamento.",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    import_label = "full" if mode == "full" else "incremental"
    log_admin_action(
        db,
        user,
        "connector_run_start",
        f"Importacao {import_label} iniciada para {connector_type or 'todos'}.",
    )
    db.commit()
    background_tasks.add_task(run_connector_import_background, mode, table_id, user.username, user.id, connector_type)
    return connector_redirect(config, "Importacao iniciada em segundo plano.")
