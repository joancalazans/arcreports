from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import Base, SessionLocal, local_engine
from app.glpi_import import run_connector_import, seed_connector_settings
from app.models import AdminActionLog, Dashboard, DashboardWidget, GlpiImportLog, ConnectorConfig, ConnectorRun, Report, ReportCategory, ReportExecution, SystemConfig, User
from app.reporting import cleanup_expired_temp_report_tables, cleanup_orphan_temp_report_tables, ensure_reporting_schema, run_auto_index_maintenance
from app.reporting import run_select
from app.routes import admin_access, appearance, auth, dashboard, db_users, history, imports, local_tables, report_categories, reports, sql_console, system_health
from app.security import ensure_admin_user, ensure_portal_groups, generate_csrf_token
from app.system_config import set_system_config_value
from app.timezone import format_datetime_portal, format_local_datetime


LOG_DIR = Path("/opt/sites/glpi-portal/logs")
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(getattr(handler, "_glpi_portal_console", False) for handler in root_logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        console_handler._glpi_portal_console = True
        root_logger.addHandler(console_handler)

    def add_rotating_handler(target_logger: logging.Logger, filename: str, marker: str) -> None:
        if any(getattr(handler, marker, False) for handler in target_logger.handlers):
            return
        handler = RotatingFileHandler(
            LOG_DIR / filename,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(formatter)
        setattr(handler, marker, True)
        target_logger.addHandler(handler)

    add_rotating_handler(root_logger, "portal.log", "_glpi_portal_general")
    add_rotating_handler(logging.getLogger("app.glpi_import"), "etl.log", "_glpi_portal_etl")
    add_rotating_handler(logging.getLogger("apscheduler"), "scheduler.log", "_glpi_portal_scheduler")


configure_logging()
settings = get_settings()
logger = logging.getLogger(__name__)
app = FastAPI(title="ArcReports")
templates = Jinja2Templates(directory="templates")
templates.env.filters["local_datetime"] = format_local_datetime


def jinja_format_dt(dt, fmt: str = "%d/%m/%Y %H:%M") -> str:
    tz_str = templates.env.globals.get("portal_tz", "America/Sao_Paulo")
    return format_datetime_portal(dt, tz_str, fmt)


templates.env.filters["portal_dt"] = jinja_format_dt


def format_number(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value or 0)
    if number.is_integer():
        return f"{int(number):,}".replace(",", ".")
    return f"{number:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


templates.env.filters["format_number"] = format_number

app.state.settings = settings
app.state.templates = templates
app.state.scheduler = None
app.state.glpi_import_lock = imports.import_lock
app.state.report_global_scheduler_lock = threading.Lock()
app.state.glpi_import_schedule_times = []
app.mount("/static", StaticFiles(directory="static"), name="static")


SCHEDULE_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SCHEDULER_TZ = ZoneInfo("America/Sao_Paulo")
GLPI_JOB_PREFIX = "glpi-import-"
REPORT_JOB_PREFIX = "report_global_"
REPORT_SCHEDULE_HOURS_KEY = "report_schedule_hours"
REPORT_SCHEDULE_ACTIVE_KEY = "report_schedule_active"
DEFAULT_REPORT_SCHEDULE_HOURS = "06:30,13:30,20:30"
DEFAULT_REPORT_SCHEDULE_ACTIVE = "true"
ETL_WAIT_BEFORE_REPORTS_KEY = "etl_wait_before_reports"
DEFAULT_ETL_WAIT_BEFORE_REPORTS = "30"
ETL_WAIT_POLL_SECONDS = 30
GLPI_SCHEDULE_ACTIVE_KEY = "glpi_schedule_active"
DEFAULT_GLPI_SCHEDULE_ACTIVE = "true"
GLPI_SCHEDULE_DAYS_KEY = "glpi_schedule_dias"
DEFAULT_GLPI_SCHEDULE_DAYS = "0,1,2,3,4,5,6"
GLPI_FULL_SCHEDULE_ACTIVE_KEY = "glpi_full_schedule_active"
DEFAULT_GLPI_FULL_SCHEDULE_ACTIVE = "false"
AUTO_INDEX_THRESHOLD_KEY = "auto_index_threshold_rows"
DEFAULT_AUTO_INDEX_THRESHOLD_ROWS = "1000"
SQL_CONSOLE_ENABLED_KEY = "sql_console_enabled"
LOG_RETENTION_DAYS_KEY = "log_retention_days"
DEFAULT_LOG_RETENTION_DAYS = "90"
SNAPSHOT_MAX_COUNT_KEY = "snapshot_max_count"
DEFAULT_SNAPSHOT_MAX_COUNT = "10"
SNAPSHOT_MAX_GB_KEY = "snapshot_max_gb"
DEFAULT_SNAPSHOT_MAX_GB = "20"


@app.middleware("http")
async def csrf_context_middleware(request: Request, call_next):
    session_token = request.cookies.get(settings.cookie_name, "")
    request.state.csrf_token = generate_csrf_token(session_token) if session_token else ""
    return await call_next(request)


def parse_schedule_times(value: str | None) -> list[tuple[int, int, str]]:
    times: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for item in (value or "").split(","):
        raw = item.strip()
        match = SCHEDULE_TIME_RE.match(raw)
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2))
        label = f"{hour:02d}:{minute:02d}"
        if label not in seen:
            times.append((hour, minute, label))
            seen.add(label)
    return sorted(times, key=lambda value: (value[0], value[1]))


def parse_schedule_days(value: str | None, default: str | None = None) -> set[int]:
    raw_value = value if value not in (None, "") else default
    days: set[int] = set()
    for item in (raw_value or "").split(","):
        raw = item.strip()
        if raw.isdigit():
            day = int(raw)
            if 0 <= day <= 6:
                days.add(day)
    return days


def current_schedule_day(now: datetime) -> int:
    return now.isoweekday() % 7


def ensure_column(connection, table_name: str, column_name: str, definition: str) -> None:
    exists = connection.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name AND COLUMN_NAME = :column_name"
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar_one()
    if not exists:
        connection.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN {definition}"))


def ensure_scheduler_schema() -> None:
    with local_engine.begin() as connection:
        ensure_column(connection, "users", "is_local", "`is_local` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "users", "portal_relatorios", "`portal_relatorios` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "users", "portal_dashboard", "`portal_dashboard` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "users", "portal_importacao", "`portal_importacao` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "users", "portal_usuarios", "`portal_usuarios` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "auth_logs", "level", "`level` VARCHAR(20) NOT NULL DEFAULT 'info'")
        ensure_column(connection, "reports", "schedule_auto", "`schedule_auto` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "reports", "schedule_horarios", "`schedule_horarios` VARCHAR(120) NOT NULL DEFAULT ''")
        connection.execute(text("ALTER TABLE reports ADD COLUMN IF NOT EXISTS is_primary TINYINT(1) NOT NULL DEFAULT 0"))
        ensure_column(
            connection,
            "connector_configs",
            "connector_type",
            "`connector_type` VARCHAR(50) NOT NULL DEFAULT 'glpi'",
        )
        ensure_column(connection, "connector_configs", "name", "`name` VARCHAR(120) NULL")
        ensure_column(connection, "connector_configs", "db_type", "`db_type` VARCHAR(30) NOT NULL DEFAULT 'mysql'")
        ensure_column(connection, "connector_configs", "target_database", "`target_database` VARCHAR(120) NOT NULL DEFAULT 'glpi_local'")
        ensure_column(connection, "connector_configs", "table_prefix", "`table_prefix` VARCHAR(80) NOT NULL DEFAULT 'glpi_'")
        ensure_column(connection, "connector_configs", "schema_name", "`schema_name` VARCHAR(120) NULL")
        ensure_column(
            connection,
            "connector_configs",
            "full_schedule_horarios",
            "`full_schedule_horarios` VARCHAR(100) NULL",
        )
        ensure_column(
            connection,
            "connector_configs",
            "full_schedule_dias",
            "`full_schedule_dias` VARCHAR(50) NULL",
        )
        ensure_column(
            connection,
            "connector_configs",
            "is_active",
            "`is_active` TINYINT(1) NOT NULL DEFAULT 1",
        )
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS db_user_databases ("
                "id INT AUTO_INCREMENT PRIMARY KEY, "
                "db_user_id INT NOT NULL, "
                "target_database VARCHAR(120) NOT NULL, "
                "UNIQUE KEY uq_db_user_database (db_user_id, target_database), "
                "FOREIGN KEY (db_user_id) REFERENCES db_users(id)"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT IGNORE INTO db_user_databases (db_user_id, target_database) "
                "SELECT id, target_database FROM db_users "
                "WHERE target_database IS NOT NULL AND target_database <> ''"
            )
        )
        ensure_column(
            connection,
            "connector_sync_tables",
            "connector_type",
            "`connector_type` VARCHAR(50) NOT NULL DEFAULT 'glpi'",
        )
        ensure_column(
            connection,
            "connector_runs",
            "connector_type",
            "`connector_type` VARCHAR(50) NOT NULL DEFAULT 'glpi'",
        )
        ensure_column(connection, "connector_runs", "origin", "`origin` VARCHAR(30) NOT NULL DEFAULT 'manual'")
        ensure_column(connection, "dashboard_widgets", "icone", "`icone` VARCHAR(80) NULL DEFAULT NULL")
        ensure_column(connection, "dashboard_widgets", "source_id_b", "`source_id_b` INT NULL DEFAULT NULL")
        ensure_column(connection, "dashboard_widgets", "comparar_com", "`comparar_com` VARCHAR(30) NULL DEFAULT NULL")
        ensure_column(connection, "dashboard_widgets", "meta_gauge", "`meta_gauge` FLOAT NULL DEFAULT NULL")
        ensure_column(connection, "dashboard_widgets", "cor_secundaria", "`cor_secundaria` VARCHAR(30) NULL DEFAULT NULL")
        ensure_column(connection, "dashboard_widgets", "dashboard_id", "`dashboard_id` INT NULL DEFAULT NULL")


def ensure_system_config_schema() -> None:
    with local_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `system_config` ("
                "`key` VARCHAR(100) NOT NULL,"
                "`value` TEXT NULL,"
                "`updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`key`)"
                ")"
            )
        )


def get_system_config_value(db, key: str, default: str | None = None) -> str | None:
    config = db.get(SystemConfig, key)
    return config.value if config and config.value is not None else default


def set_default_system_config(db, key: str, value: str) -> None:
    if not db.get(SystemConfig, key):
        db.add(SystemConfig(key=key, value=value))
        db.commit()


def seed_system_config(db) -> None:
    set_default_system_config(db, REPORT_SCHEDULE_HOURS_KEY, DEFAULT_REPORT_SCHEDULE_HOURS)
    set_default_system_config(db, REPORT_SCHEDULE_ACTIVE_KEY, DEFAULT_REPORT_SCHEDULE_ACTIVE)
    set_default_system_config(db, ETL_WAIT_BEFORE_REPORTS_KEY, DEFAULT_ETL_WAIT_BEFORE_REPORTS)
    set_default_system_config(db, GLPI_SCHEDULE_ACTIVE_KEY, DEFAULT_GLPI_SCHEDULE_ACTIVE)
    set_default_system_config(db, GLPI_SCHEDULE_DAYS_KEY, DEFAULT_GLPI_SCHEDULE_DAYS)
    set_default_system_config(db, GLPI_FULL_SCHEDULE_ACTIVE_KEY, DEFAULT_GLPI_FULL_SCHEDULE_ACTIVE)
    set_default_system_config(db, AUTO_INDEX_THRESHOLD_KEY, DEFAULT_AUTO_INDEX_THRESHOLD_ROWS)
    set_default_system_config(db, SQL_CONSOLE_ENABLED_KEY, "true")
    set_default_system_config(db, LOG_RETENTION_DAYS_KEY, DEFAULT_LOG_RETENTION_DAYS)
    set_default_system_config(db, SNAPSHOT_MAX_COUNT_KEY, DEFAULT_SNAPSHOT_MAX_COUNT)
    set_default_system_config(db, SNAPSHOT_MAX_GB_KEY, DEFAULT_SNAPSHOT_MAX_GB)
    set_default_system_config(db, "appearance_portal_name", "ArcReports")
    set_default_system_config(db, "appearance_portal_initials", "AR")
    set_default_system_config(db, "appearance_timezone", "America/Sao_Paulo")
    set_default_system_config(
        db,
        "hidden_columns",
        "reference_date,created_at,solved_at,closed_at,target_end_at,type_id,status_id,priority_id,"
        "urgency_id,impact_id,sla_id,prazo_sla_segundos,tempo_util_segundos,"
        "tempo_primeiro_atendimento_segundos",
    )
    for key, value in appearance.DEFAULT_APPEARANCE.items():
        set_default_system_config(db, f"appearance_{key}", value)
    set_default_system_config(db, "pdf_logo", "")
    set_default_system_config(db, "pdf_titulo", get_system_config_value(db, "appearance_portal_name", "ArcReports") or "ArcReports")
    set_default_system_config(db, "pdf_subtitulo", "")
    set_default_system_config(db, "pdf_rodape", "")


def ensure_default_dashboard(db) -> None:
    orphan_widgets = db.query(DashboardWidget).filter(DashboardWidget.dashboard_id.is_(None)).all()
    if not orphan_widgets:
        return
    principal = db.query(Dashboard).filter(Dashboard.name == "Dashboard principal").first()
    if principal is None:
        principal = Dashboard(name="Dashboard principal", is_active=True, sort_order=100)
        db.add(principal)
        db.flush()
    for widget in orphan_widgets:
        widget.dashboard = principal
    db.commit()


def menu_dashboards_for_user(user) -> list[Dashboard]:
    if not user:
        return []
    db = SessionLocal()
    try:
        query = db.query(Dashboard).filter(Dashboard.is_active.is_(True))
        if not user.is_admin:
            allowed_categories = {
                category.name for category in user.report_categories if category.is_active
            } | {
                category.name
                for group in user.portal_groups
                for category in group.report_categories
                if category.is_active
            }
            query = query.join(Dashboard.categories).filter(
                ReportCategory.name.in_(allowed_categories) if allowed_categories else False
            )
        return (
            query.options(selectinload(Dashboard.categories), selectinload(Dashboard.widgets))
            .order_by(Dashboard.sort_order.asc(), Dashboard.name.asc())
            .distinct()
            .all()
        )
    finally:
        db.close()


templates.env.globals["menu_dashboards_for_user"] = menu_dashboards_for_user


def get_appearance_for_template():
    db = SessionLocal()
    try:
        return appearance.load_appearance(db)
    except Exception:
        return appearance.DEFAULT_APPEARANCE.copy()
    finally:
        db.close()


def get_pdf_config_for_template():
    db = SessionLocal()
    try:
        current_appearance = appearance.load_appearance(db)
        return appearance.load_pdf_config(db, current_appearance.get("portal_name"))
    except Exception:
        return {
            "pdf_logo": "",
            "pdf_titulo": "ArcReports",
            "pdf_subtitulo": "",
            "pdf_rodape": "",
        }
    finally:
        db.close()


templates.env.globals["appearance"] = get_appearance_for_template()
templates.env.globals["portal_tz"] = templates.env.globals["appearance"].get(
    "timezone",
    "America/Sao_Paulo",
)
templates.env.globals.update(get_pdf_config_for_template())


def ensure_scheduler_user(db) -> User:
    user = db.query(User).filter(User.username == "scheduler").first()
    if user:
        return user
    user = User(
        username="scheduler",
        password_hash=None,
        auth_source="scheduler",
        is_admin=False,
        is_active=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def remove_jobs_by_prefix(scheduler: BackgroundScheduler, prefix: str) -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            scheduler.remove_job(job.id)


def mark_import_runs_origin(db, runs: list[ConnectorRun], origin: str) -> None:
    for run in runs:
        run.origin = origin
    db.commit()


def cleanup_stuck_runs() -> None:
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=2)

        stuck_glpi = (
            db.query(ConnectorRun)
            .filter(
                ConnectorRun.status == "running",
                ConnectorRun.started_at < cutoff,
            )
            .all()
        )
        for run in stuck_glpi:
            run.status = "error"
            run.finished_at = datetime.utcnow()
            run.error_message = "Execucao interrompida - processo reiniciado ou travado"
            logger.warning(
                "Startup: limpando importacao GLPI travada id=%s tabela=%s",
                run.id,
                getattr(run, "table_name", "?"),
            )

        stuck_reports = (
            db.query(ReportExecution)
            .filter(
                ReportExecution.status == "running",
                ReportExecution.executed_at < cutoff,
            )
            .all()
        )
        for execution in stuck_reports:
            execution.status = "error"
            execution.finished_at = datetime.utcnow()
            execution.error_message = "Execucao interrompida - processo reiniciado ou travado"
            logger.warning(
                "Startup: limpando relatorio travado id=%s report_id=%s",
                execution.id,
                execution.report_id,
            )

        if stuck_glpi or stuck_reports:
            db.commit()
            logger.info(
                "Startup: %d importacoes GLPI e %d relatorios travados corrigidos.",
                len(stuck_glpi),
                len(stuck_reports),
            )
    except Exception as e:
        logger.error("Startup: erro no cleanup: %s", e)
    finally:
        db.close()


def configured_log_retention_days(db) -> int:
    raw_value = get_system_config_value(db, LOG_RETENTION_DAYS_KEY, DEFAULT_LOG_RETENTION_DAYS)
    try:
        return max(1, int(str(raw_value or DEFAULT_LOG_RETENTION_DAYS).strip()))
    except ValueError:
        return int(DEFAULT_LOG_RETENTION_DAYS)


def cleanup_old_logs(db=None) -> None:
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        retention_days = configured_log_retention_days(db)
        cutoff = datetime.utcnow() - timedelta(days=retention_days)

        deleted_exec = db.execute(
            text("DELETE FROM report_executions WHERE executed_at < :cutoff"),
            {"cutoff": cutoff},
        ).rowcount

        deleted_admin = db.execute(
            text("DELETE FROM admin_action_logs WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        ).rowcount

        deleted_auth = db.execute(
            text("DELETE FROM auth_logs WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        ).rowcount

        db.commit()

        if any([deleted_exec, deleted_admin, deleted_auth]):
            logger.info(
                "Limpeza de logs %d dias: %d execuções, %d admin, %d auth removidos.",
                retention_days,
                deleted_exec,
                deleted_admin,
                deleted_auth,
            )
    except Exception as e:
        logger.error("Erro na limpeza de logs: %s", str(e))
        db.rollback()
    finally:
        if owns_session:
            db.close()


def run_snapshot_cleanup_job() -> None:
    try:
        result = system_health.cleanup_old_snapshots()
        if result["deleted"]:
            logger.info(
                "Limpeza automática de snapshots: %d arquivos removidos, %s GB liberados.",
                result["deleted"],
                result["deleted_gb"],
            )
    except Exception as exc:
        logger.error("Erro na limpeza automática de snapshots: %s", exc)


def run_snapshot_create_job() -> None:
    """Cria um snapshot automático diário e aplica a política de retenção."""
    try:
        with SessionLocal() as db:
            result = system_health.create_snapshot(label="auto", db=db)
            logger.info(
                "Snapshot automático criado: %s (%s MB)",
                result["filename"], result["size_mb"],
            )
            system_health.cleanup_old_snapshots(db)
    except Exception as exc:
        logger.error("Erro no snapshot automático: %s", exc)


def run_auto_index_job() -> None:
    try:
        result = run_auto_index_maintenance()
        indexed_tables = result.get("indexed_tables", {})
        if indexed_tables:
            logger.info(
                "Auto-indexacao: %d tabelas inspecionadas, %d tabelas alteradas.",
                result.get("inspected", 0),
                len(indexed_tables),
            )
    except Exception as exc:
        logger.error("Erro na auto-indexacao de relatorios: %s", exc)


def run_scheduled_glpi_import() -> None:
    db = SessionLocal()
    try:
        if get_system_config_value(db, "migration_lock", "0") == "1":
            logger.info("Scheduler GLPI: migration_lock ativo, pulando execucao.")
            return
        now_local = datetime.now(SCHEDULER_TZ)
        current_time = now_local.strftime("%H:%M")
        schedule_config = db.query(SystemConfig).filter(SystemConfig.key == GLPI_SCHEDULE_ACTIVE_KEY).first()
        incremental_active = schedule_config.value == "true" if schedule_config else True
        schedule_days = parse_schedule_days(
            get_system_config_value(db, GLPI_SCHEDULE_DAYS_KEY, DEFAULT_GLPI_SCHEDULE_DAYS),
            DEFAULT_GLPI_SCHEDULE_DAYS,
        )
        full_active = get_system_config_value(
            db,
            GLPI_FULL_SCHEDULE_ACTIVE_KEY,
            DEFAULT_GLPI_FULL_SCHEDULE_ACTIVE,
        ) == "true"
        scheduled_configs = []
        for config in db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True)).order_by(ConnectorConfig.connector_type.asc()).all():
            incremental_times = {label for _, _, label in parse_schedule_times(config.suggested_frequency)}
            full_times = {label for _, _, label in parse_schedule_times(config.full_schedule_horarios)}
            full_days = parse_schedule_days(config.full_schedule_dias)
            full_match = bool(full_active and full_times and current_time in full_times and current_schedule_day(now_local) in full_days)
            incremental_match = bool(
                incremental_active
                and current_time in incremental_times
                and current_schedule_day(now_local) in schedule_days
            )
            if full_match or incremental_match:
                scheduled_configs.append((config.connector_type, "full" if full_match else "incremental"))
        if not scheduled_configs:
            logger.info("Scheduler conectores: nenhum conector ativo com horario correspondente.")
            return
    finally:
        db.close()

    lock = app.state.glpi_import_lock
    if not lock.acquire(blocking=False):
        return
    db = SessionLocal()
    started_at = None
    try:
        cutoff = datetime.utcnow() - timedelta(hours=2)
        running = (
            db.query(ConnectorRun)
            .filter(
                ConnectorRun.status == "running",
                ConnectorRun.started_at > cutoff,
            )
            .first()
        )
        if running:
            logger.info(
                "Scheduler GLPI: importacao em andamento (id=%s), pulando execucao.",
                running.id,
            )
            return
        stuck = (
            db.query(ConnectorRun)
            .filter(
                ConnectorRun.status == "running",
                ConnectorRun.started_at <= cutoff,
            )
            .first()
        )
        if stuck:
            stuck.status = "error"
            stuck.finished_at = datetime.utcnow()
            stuck.error_message = "Execucao travada - corrigida automaticamente pelo scheduler"
            db.commit()
            logger.warning(
                "Scheduler GLPI: execucao travada id=%s corrigida automaticamente.",
                stuck.id,
            )
        started_at = datetime.utcnow()
        mode = "incremental"
        all_runs = []
        for connector_type, scheduled_mode in scheduled_configs:
            mode = scheduled_mode
            runs = run_connector_import(db, scheduled_mode, connector_type=connector_type)
            all_runs.extend(runs)
        mark_import_runs_origin(db, all_runs, "scheduler")
    except Exception as exc:
        db.add(
            ConnectorRun(
                connector_type="scheduler",
                table_name="__scheduler__",
                mode=mode if "mode" in locals() else "incremental",
                origin="scheduler",
                status="error",
                error_message=str(exc),
                started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
            )
        )
        db.add(GlpiImportLog(level="error", message=f"Importacao automatica: {exc}"))
        db.commit()
    finally:
        if started_at is not None:
            finished_at = datetime.utcnow()
            try:
                imports.record_connector_import_admin_log(db, mode if "mode" in locals() else "incremental", "scheduler", started_at, finished_at)
            except Exception as exc:
                db.rollback()
                db.add(GlpiImportLog(level="error", message=f"Log administrativo da importacao automatica: {exc}"))
                db.commit()
        db.close()
        lock.release()


def reload_glpi_import_schedule() -> None:
    scheduler: BackgroundScheduler | None = app.state.scheduler
    if not scheduler:
        return
    db = SessionLocal()
    try:
        remove_jobs_by_prefix(scheduler, GLPI_JOB_PREFIX)
        configs = db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True)).order_by(ConnectorConfig.connector_type.asc()).all()
        incremental_times = []
        for config in configs:
            incremental_times.extend(parse_schedule_times(config.suggested_frequency))
        full_active = get_system_config_value(
            db,
            GLPI_FULL_SCHEDULE_ACTIVE_KEY,
            DEFAULT_GLPI_FULL_SCHEDULE_ACTIVE,
        ) == "true"
        full_times = []
        if full_active:
            for config in configs:
                full_times.extend(parse_schedule_times(config.full_schedule_horarios))
        times_by_label = {label: (hour, minute, label) for hour, minute, label in incremental_times}
        for hour, minute, label in full_times:
            times_by_label.setdefault(label, (hour, minute, label))
        app.state.glpi_import_schedule_times = [label for _, _, label in incremental_times]
        for hour, minute, label in sorted(times_by_label.values(), key=lambda value: (value[0], value[1])):
            scheduler.add_job(
                run_scheduled_glpi_import,
                CronTrigger(hour=hour, minute=minute, timezone=SCHEDULER_TZ),
                id=f"{GLPI_JOB_PREFIX}{label.replace(':', '')}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
    finally:
        db.close()


def record_scheduled_report_error(db, report: Report, scheduler_user: User, error: str) -> None:
    db.add(
        ReportExecution(
            report_id=report.id,
            user_id=scheduler_user.id,
            sql_query=report.sql_query,
            destination_table=report.destination_table,
            status="error",
            row_count=0,
            duration_ms=0,
            error_message=error,
            executed_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    )
    db.commit()


def get_etl_wait_before_reports_minutes(db) -> int:
    raw_value = get_system_config_value(
        db,
        ETL_WAIT_BEFORE_REPORTS_KEY,
        DEFAULT_ETL_WAIT_BEFORE_REPORTS,
    )
    try:
        return max(0, int(raw_value or 0))
    except (TypeError, ValueError):
        logger.warning(
            "Valor invalido para %s: %r. Usando padrao %s.",
            ETL_WAIT_BEFORE_REPORTS_KEY,
            raw_value,
            DEFAULT_ETL_WAIT_BEFORE_REPORTS,
        )
        return int(DEFAULT_ETL_WAIT_BEFORE_REPORTS)


def is_etl_running(db) -> bool:
    wait_minutes = get_etl_wait_before_reports_minutes(db)
    if wait_minutes == 0:
        return False

    cutoff = datetime.utcnow() - timedelta(minutes=wait_minutes)
    running = (
        db.query(ConnectorRun)
        .filter(
            ConnectorRun.status == "running",
            ConnectorRun.started_at >= cutoff,
        )
        .first()
    )
    return running is not None


def log_scheduler_admin_action(db, action: str, detail: str, status_value: str = "success") -> None:
    db.add(
        AdminActionLog(
            user_id=None,
            username="scheduler",
            action=action,
            status=status_value,
            message=detail,
        )
    )
    db.commit()


def wait_for_etl(db) -> bool:
    wait_minutes = get_etl_wait_before_reports_minutes(db)
    if wait_minutes == 0:
        return True

    deadline = datetime.utcnow() + timedelta(minutes=wait_minutes)
    waited_seconds = 0

    while datetime.utcnow() < deadline:
        if not is_etl_running(db):
            if waited_seconds > 0:
                log_scheduler_admin_action(
                    db,
                    "reports_waited_for_etl",
                    f"Relatorios aguardaram {waited_seconds}s pelo ETL antes de iniciar.",
                )
            return True
        logger.info("Aguardando ETL terminar antes de executar relatorios...")
        time.sleep(ETL_WAIT_POLL_SECONDS)
        waited_seconds += ETL_WAIT_POLL_SECONDS

    log_scheduler_admin_action(
        db,
        "reports_etl_timeout",
        "Timeout aguardando ETL. Relatorios executados com dados possivelmente incompletos.",
        "warning",
    )
    logger.warning(
        "Timeout aguardando ETL. Relatorios serao executados com dados possivelmente incompletos."
    )
    return False


def run_all_scheduled_reports() -> None:
    lock: threading.Lock = app.state.report_global_scheduler_lock
    if not lock.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        if get_system_config_value(db, REPORT_SCHEDULE_ACTIVE_KEY, DEFAULT_REPORT_SCHEDULE_ACTIVE) != "true":
            return
        etl_concluido = wait_for_etl(db)
        if not etl_concluido:
            logger.warning(
                "Relatorios iniciados apos timeout de espera pelo ETL. Dados podem estar incompletos."
            )
        cutoff = datetime.utcnow() - timedelta(hours=2)
        running = (
            db.query(ReportExecution)
            .filter(
                ReportExecution.status == "running",
                ReportExecution.executed_at > cutoff,
            )
            .first()
        )
        if running:
            logger.info(
                "Scheduler relatorios: execucao em andamento (id=%s), pulando execucao.",
                running.id,
            )
            return
        stuck = (
            db.query(ReportExecution)
            .filter(
                ReportExecution.status == "running",
                ReportExecution.executed_at <= cutoff,
            )
            .first()
        )
        if stuck:
            stuck.status = "error"
            stuck.finished_at = datetime.utcnow()
            stuck.error_message = "Execucao travada - corrigida automaticamente pelo scheduler"
            db.commit()
            logger.warning(
                "Scheduler relatorios: execucao travada id=%s corrigida automaticamente.",
                stuck.id,
            )
        scheduler_user = ensure_scheduler_user(db)
        primary_reports = (
            db.query(Report)
            .filter(
                Report.is_active.is_(True),
                Report.destination_table.isnot(None),
                Report.destination_table != "",
                Report.is_primary.is_(True),
            )
            .order_by(Report.id.asc())
            .all()
        )
        if primary_reports:
            logger.info(
                "Scheduler fase 1: %d relatorios primarios.",
                len(primary_reports),
            )
        for index, report in enumerate(primary_reports):
            try:
                logger.info(
                    "Primario: '%s' -> %s",
                    report.name,
                    report.destination_table,
                )
                run_select(
                    db,
                    report.sql_query,
                    scheduler_user.id,
                    report.id,
                    report.destination_table,
                    report.modo_salvamento,
                )
            except Exception as exc:
                db.rollback()
                record_scheduled_report_error(db, report, scheduler_user, str(exc))
                logger.error("Erro primario '%s': %s", report.name, str(exc))
            if index < len(primary_reports) - 1:
                time.sleep(2)

        dependent_reports = (
            db.query(Report)
            .filter(
                Report.is_active.is_(True),
                Report.destination_table.isnot(None),
                Report.destination_table != "",
                Report.is_primary.is_(False),
            )
            .order_by(Report.id.asc())
            .all()
        )
        if dependent_reports:
            logger.info(
                "Scheduler fase 2: %d relatorios dependentes.",
                len(dependent_reports),
            )
        for index, report in enumerate(dependent_reports):
            try:
                logger.info(
                    "Dependente: '%s' -> %s",
                    report.name,
                    report.destination_table,
                )
                run_select(
                    db,
                    report.sql_query,
                    scheduler_user.id,
                    report.id,
                    report.destination_table,
                    report.modo_salvamento,
                )
            except Exception as exc:
                db.rollback()
                record_scheduled_report_error(db, report, scheduler_user, str(exc))
                logger.error("Erro dependente '%s': %s", report.name, str(exc))
            if index < len(dependent_reports) - 1:
                time.sleep(2)
        logger.info(
            "Scheduler concluido: %d primarios, %d dependentes.",
            len(primary_reports),
            len(dependent_reports),
        )
    finally:
        db.close()
        lock.release()


def reload_report_scheduler(db=None) -> None:
    scheduler: BackgroundScheduler | None = app.state.scheduler
    if not scheduler:
        return
    remove_jobs_by_prefix(scheduler, REPORT_JOB_PREFIX)
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        schedule_hours = get_system_config_value(db, REPORT_SCHEDULE_HOURS_KEY, DEFAULT_REPORT_SCHEDULE_HOURS)
        for hour, minute, label in parse_schedule_times(schedule_hours):
            scheduler.add_job(
                run_all_scheduled_reports,
                CronTrigger(hour=hour, minute=minute, timezone=SCHEDULER_TZ),
                id=f"{REPORT_JOB_PREFIX}{label}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
    finally:
        if owns_session:
            db.close()


def reload_report_schedule(report_id: int | None = None) -> None:
    reload_report_scheduler()


def scheduler_status() -> dict:
    scheduler: BackgroundScheduler | None = app.state.scheduler
    glpi_jobs = [
        job
        for job in scheduler.get_jobs()
        if scheduler and job.id.startswith(GLPI_JOB_PREFIX)
    ] if scheduler else []
    return {
        "active": bool(scheduler and scheduler.state == STATE_RUNNING and glpi_jobs),
        "times": list(app.state.glpi_import_schedule_times),
    }


@app.on_event("startup")
def startup() -> None:
    os.makedirs("static/uploads/appearance", exist_ok=True)
    ensure_system_config_schema()
    Base.metadata.create_all(bind=local_engine)
    ensure_scheduler_schema()
    db = SessionLocal()
    try:
        ensure_admin_user(db)
        ensure_portal_groups(db)
        seed_connector_settings(db)
        seed_system_config(db)
        if not settings.sql_console_enabled:
            set_system_config_value(db, SQL_CONSOLE_ENABLED_KEY, "false")
        templates.env.globals["appearance"] = get_appearance_for_template()
        templates.env.globals["portal_tz"] = templates.env.globals["appearance"].get(
            "timezone",
            "America/Sao_Paulo",
        )
        templates.env.globals.update(get_pdf_config_for_template())
        ensure_reporting_schema()
        ensure_scheduler_user(db)
        ensure_default_dashboard(db)
        cleanup_orphan_temp_report_tables(db)
        cleanup_expired_temp_report_tables(db)
    finally:
        db.close()
    cleanup_stuck_runs()
    cleanup_old_logs()
    app.state.scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)
    app.state.scheduler.start()
    app.state.scheduler.add_job(
        cleanup_old_logs,
        "cron",
        hour=3,
        minute=0,
        id="cleanup-logs-daily",
        replace_existing=True,
    )
    app.state.scheduler.add_job(
        run_auto_index_job,
        "cron",
        hour=3,
        minute=30,
        id="auto-index-reports-daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    app.state.scheduler.add_job(
        run_snapshot_create_job,
        "cron",
        hour=2,
        minute=0,
        id="snapshot-create-daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    app.state.scheduler.add_job(
        run_snapshot_cleanup_job,
        "cron",
        hour=3,
        minute=20,
        id="cleanup-snapshots-daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    app.state.reload_glpi_import_schedule = reload_glpi_import_schedule
    app.state.reload_report_schedule = reload_report_schedule
    app.state.reload_report_scheduler = reload_report_scheduler
    app.state.run_all_scheduled_reports = run_all_scheduled_reports
    app.state.scheduler_status = scheduler_status
    reload_glpi_import_schedule()
    reload_report_scheduler()


@app.on_event("shutdown")
def shutdown() -> None:
    scheduler: BackgroundScheduler | None = app.state.scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(reports.router)
app.include_router(history.router)
app.include_router(imports.router)
app.include_router(local_tables.router)
app.include_router(report_categories.router)
app.include_router(admin_access.router)
app.include_router(db_users.router)
app.include_router(appearance.router)
app.include_router(sql_console.router)
app.include_router(system_health.router)
