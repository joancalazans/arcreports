from __future__ import annotations

import logging
import time
import re
from collections import defaultdict
from datetime import date, datetime, time as datetime_time, timedelta
from io import BytesIO
from threading import Lock
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal, get_db, local_engine
from app.deletion_dependencies import dashboard_dependencies, delete_widgets, source_dependencies
from app.models import AdminActionLog, Dashboard, DashboardSource, DashboardWidget, ConnectorRun, ConnectorSyncTable, Report, ReportCategory, ReportExecution, TempReportResult
from app.reporting import (
    CONTROL_COLUMN_ALIASES,
    IDENTIFIER_RE,
    apply_query_timeout,
    cleanup_expired_temp_report_tables,
    is_temp_report_table,
    quote_column,
    quote_identifier,
    run_select,
    validate_select,
)
from app.routes.common import form_data, form_lists, get_allowed_databases, render
from app.security import can_access_report, log_category_denial, require_admin, require_dashboard, require_relatorios, require_view_user, user_report_category_names, verify_csrf, verify_csrf_header
from app.timezone import format_datetime_portal
from app.utils import TemporalPlan, get_date_columns


router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)
HIDDEN_COLUMNS_KEY = "hidden_columns"
DEFAULT_HIDDEN_COLUMNS = (
    "reference_date,created_at,solved_at,closed_at,target_end_at,type_id,status_id,priority_id,"
    "urgency_id,impact_id,sla_id,prazo_sla_segundos,tempo_util_segundos,"
    "tempo_primeiro_atendimento_segundos"
)
PREVIEW_ROW_LIMIT = 5
PREVIEW_COLUMN_LIMIT = 6
COUNT_CACHE_TTL = 300
TOTAL_COUNT_CACHE: dict[str, tuple[float, int]] = {}
TABLES_CACHE_TTL = 300
_tables_cache: dict[str, object] = {"data": None, "expires": 0}
_tables_cache_lock = Lock()
REPORT_DETAIL_DATE_COLUMNS = ("solved_at", "created_at", "reference_date", "closed_at", "solvedate", "date_creation", "date_mod", "closedate")
REPORT_DETAIL_PAGE_SIZE = 100
REPORT_DETAIL_MAX_PAGE_SIZE = 500
REPORT_TEMP_TTL = timedelta(hours=2)
PERIOD_FILTER_FIELD_RE = re.compile(
    r"^[^\x00`'\"\\.]+(?:\.[^\x00`'\"\\.]+)?$"
)
BRAZILIAN_DATETIME_SECONDS_FMT = "'%d/%m/%Y %H:%i:%s'"
BRAZILIAN_DATETIME_FMT = "'%d/%m/%Y %H:%i'"
BRAZILIAN_DATE_FMT = "'%d/%m/%Y'"
ISO_DATETIME_SECONDS_FMT = "'%Y-%m-%d %H:%i:%s'"
ISO_DATETIME_FMT = "'%Y-%m-%d %H:%i'"
ISO_DATE_FMT = "'%Y-%m-%d'"
DASHBOARD_EXCLUDED_PREFIXES = ("tmp_", "etl_", "logs_")
DASHBOARD_EXCLUDED_DATABASES = {
    "information_schema",
    "mysql",
    "performance_schema",
}
DASHBOARD_INTERNAL_TABLES = {
    "admin_action_logs",
    "auth_logs",
    "dashboard_categories",
    "dashboard_sources",
    "dashboard_widgets",
    "dashboards",
    "connector_configs",
    "glpi_import_logs",
    "connector_runs",
    "connector_sync_tables",
    "db_users",
    "db_user_databases",
    "ldap_configs",
    "portal_groups",
    "portal_group_report_categories",
    "report_categories",
    "report_executions",
    "reports",
    "system_config",
    "temp_report_results",
    "user_report_categories",
    "user_portal_groups",
    "users",
}
DASHBOARD_WIDGET_TYPES = {"barra", "barra_horizontal", "linha", "pizza", "area", "kpi", "tabela", "gauge", "comparativo"}
DASHBOARD_AGGREGATIONS = {"count", "sum", "avg", "min", "max"}
DASHBOARD_COLORS = {"blue", "green", "amber", "red", "violet", "slate"}
DASHBOARD_SIZES = {"pequeno", "medio", "grande", "total"}
DASHBOARD_BULK_ACTIONS = {"activate", "deactivate", "delete", "delete_all"}
DESTRUCTIVE_DASHBOARD_BULK_ACTIONS = {"delete", "delete_all"}
DASHBOARD_COMPARE_PERIODS = {"mes_anterior", "ano_anterior"}
DASHBOARD_ICONS = {
    "ti-ticket",
    "ti-check",
    "ti-clock",
    "ti-alert-triangle",
    "ti-users",
    "ti-chart-bar",
    "ti-calendar",
    "ti-trending-up",
}


def clean_dashboard_text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def dashboard_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value or default), maximum))
    except (TypeError, ValueError):
        return default


def dashboard_float(value: str | None, minimum: float, maximum: float) -> float | None:
    cleaned = (value or "").strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return max(minimum, min(parsed, maximum))


def is_dashboard_source_table(table_name: str) -> bool:
    normalized = (table_name or "").lower()
    return (
        bool(IDENTIFIER_RE.match(table_name or ""))
        and normalized not in DASHBOARD_INTERNAL_TABLES
        and not normalized.startswith(DASHBOARD_EXCLUDED_PREFIXES)
    )


def dashboard_source_identifier(database_name: str, table_name: str) -> str:
    return table_name if database_name == settings.local_db_name else f"{database_name}.{table_name}"


def dashboard_source_parts(source_table: str) -> tuple[str, str]:
    value = (source_table or "").strip()
    if "." not in value:
        return settings.local_db_name, value
    database_name, table_name = value.split(".", 1)
    if IDENTIFIER_RE.match(database_name or "") and IDENTIFIER_RE.match(table_name or ""):
        return database_name, table_name
    return settings.local_db_name, value


def dashboard_source_valid(source_table: str) -> bool:
    database_name, table_name = dashboard_source_parts(source_table)
    return IDENTIFIER_RE.match(database_name or "") is not None and is_dashboard_source_table(table_name)


def reset_dashboard_tables_cache() -> None:
    with _tables_cache_lock:
        _tables_cache["data"] = None
        _tables_cache["expires"] = 0


def dashboard_allowed_databases(db: Session) -> list[str]:
    return [
        database_name
        for database_name in get_allowed_databases(db)
        if database_name not in DASHBOARD_EXCLUDED_DATABASES and IDENTIFIER_RE.match(database_name or "")
    ]


def dashboard_available_tables(db: Session) -> list[dict]:
    now = time.monotonic()
    with _tables_cache_lock:
        cached_data = _tables_cache["data"]
        if cached_data is not None and now < float(_tables_cache["expires"] or 0):
            return list(cached_data)

    allowed_databases = dashboard_allowed_databases(db)
    result: list[dict] = []
    try:
        with local_engine.connect() as connection:
            for database_name in allowed_databases:
                rows = connection.execute(
                    text(
                        "SELECT TABLE_NAME, TABLE_SCHEMA, TABLE_ROWS, TABLE_COMMENT "
                        "FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = :database_name AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                        "ORDER BY TABLE_NAME"
                    ),
                    {"database_name": database_name},
                ).mappings()
                for row in rows:
                    table_name = row["TABLE_NAME"]
                    if not is_dashboard_source_table(table_name):
                        continue
                    source_name = dashboard_source_identifier(database_name, table_name)
                    result.append(
                        {
                            "name": source_name,
                            "table_name": table_name,
                            "database": database_name,
                            "display_name": source_name,
                            "estimated_rows": row["TABLE_ROWS"] or 0,
                            "comment": row["TABLE_COMMENT"] or "",
                        }
                    )
    except SQLAlchemyError:
        logger.exception("Falha ao consultar tabelas disponiveis para Fontes Dashboard.")
        return []
    with _tables_cache_lock:
        _tables_cache["data"] = result
        _tables_cache["expires"] = now + TABLES_CACHE_TTL
    return result


def dashboard_database_icon(database_name: str) -> str:
    normalized = (database_name or "").lower()
    if normalized == settings.local_db_name:
        return "ti-database"
    if normalized == "glpi_local":
        return "ti-table"
    if normalized.startswith("zabbix"):
        return "ti-activity"
    if normalized.startswith("redmine"):
        return "ti-git-branch"
    return "ti-plug"


def dashboard_database_metadata(db: Session) -> list[dict]:
    tables = dashboard_available_tables(db)
    allowed = dashboard_allowed_databases(db)
    counts = {database_name: 0 for database_name in allowed}
    for table in tables:
        counts[table["database"]] = counts.get(table["database"], 0) + 1
    return [
        {
            "name": database_name,
            "display_name": database_name,
            "table_count": counts.get(database_name, 0),
            "is_primary": database_name == settings.local_db_name,
            "icon": dashboard_database_icon(database_name),
        }
        for database_name in allowed
    ]


def dashboard_page_params(page: int = 1, per_page: int = 20) -> tuple[int, int]:
    return max(1, page or 1), max(1, min(per_page or 20, 100))


def dashboard_paginate(items: list[dict], page: int, per_page: int) -> tuple[list[dict], int, int]:
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    current_page = min(page, pages)
    start = (current_page - 1) * per_page
    return items[start : start + per_page], pages, current_page


def dashboard_table_list_item(table: dict, source: DashboardSource | None) -> dict:
    return {
        "name": table["table_name"],
        "table_name": table["name"],
        "display_name": table["display_name"],
        "database": table["database"],
        "estimated_rows": table["estimated_rows"],
        "row_count": table["estimated_rows"],
        "is_enabled": bool(source and source.is_active),
        "is_active": bool(source and source.is_active),
        "source_id": source.id if source else None,
        "id": source.id if source else None,
        "table_exists": True,
    }


def dashboard_source_table_exists(table_name: str) -> bool:
    if not dashboard_source_valid(table_name):
        return False
    database_name, physical_table = dashboard_source_parts(table_name)
    try:
        with local_engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name "
                        "AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')"
                    ),
                    {"database_name": database_name, "table_name": physical_table},
                ).scalar_one()
            )
    except SQLAlchemyError:
        logger.exception("Falha ao consultar existencia da Fonte Dashboard %s.", table_name)
        return False


def dashboard_table_columns(table_name: str) -> list[dict]:
    return [
        column
        for column in dashboard_all_table_columns(table_name)
        if not is_dashboard_technical_column(column["name"])
    ]


def dashboard_all_table_columns(table_name: str) -> list[dict]:
    if not dashboard_source_table_exists(table_name):
        return []
    database_name, physical_table = dashboard_source_parts(table_name)
    try:
        with local_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT COLUMN_NAME, DATA_TYPE "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name "
                    "ORDER BY ORDINAL_POSITION"
                ),
                {"database_name": database_name, "table_name": physical_table},
            ).mappings()
            return [
                {"name": row["COLUMN_NAME"], "data_type": row["DATA_TYPE"]}
                for row in rows
                if IDENTIFIER_RE.match(row["COLUMN_NAME"])
            ]
    except SQLAlchemyError:
        logger.exception("Falha ao consultar colunas da Fonte Dashboard %s.", table_name)
        return []


def dashboard_column_names(source: DashboardSource) -> set[str]:
    return {column["name"] for column in dashboard_table_columns(source.source_table)}


NATIVE_TEMPORAL_TYPES = {"date", "datetime", "timestamp"}
STRING_TEMPORAL_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}
CANONICAL_TEMPORAL_COLUMNS = (
    "created_at",
    "solved_at",
    "closed_at",
    "reference_date",
    "target_end_at",
    "updated_at",
    "start_date",
    "due_date",
    "CompletedDate",
    "CreatedDate",
    "CloseDate",
    "Concluído",
    "Criado em",
    "Alterado em",
    "Início",
    "Data prevista",
)


def dashboard_temporal_column_is_native(column: dict) -> bool:
    return (column["data_type"] or "").lower() in NATIVE_TEMPORAL_TYPES


def dashboard_temporal_column_has_valid_dates(source: DashboardSource, column_name: str) -> bool:
    """Verifica se a coluna tem datas validas em formato ISO ou brasileiro."""
    database_name, physical_table = dashboard_source_parts(source.source_table)
    safe_database = quote_identifier(database_name)
    safe_table = quote_identifier(physical_table)
    safe_column = quote_column(column_name)
    query = (
        f"SELECT COUNT(*) FROM {safe_database}.{safe_table} "
        "WHERE COALESCE("
        f"STR_TO_DATE({safe_column}, {ISO_DATETIME_SECONDS_FMT}), "
        f"STR_TO_DATE({safe_column}, {ISO_DATETIME_FMT}), "
        f"STR_TO_DATE({safe_column}, {ISO_DATE_FMT}), "
        f"STR_TO_DATE({safe_column}, {BRAZILIAN_DATETIME_SECONDS_FMT}), "
        f"STR_TO_DATE({safe_column}, {BRAZILIAN_DATETIME_FMT}), "
        f"STR_TO_DATE({safe_column}, {BRAZILIAN_DATE_FMT})"
        f") IS NOT NULL AND {safe_column} IS NOT NULL LIMIT 1"
    )
    try:
        with local_engine.connect() as connection:
            apply_query_timeout(connection)
            return bool(connection.execute(text(query)).scalar() or 0)
    except SQLAlchemyError:
        logger.exception(
            "Falha ao validar campo temporal %s da Fonte Dashboard %s.",
            column_name,
            source.source_table,
        )
        return False


def dashboard_temporal_column_is_usable(
    source: DashboardSource,
    column: dict | None,
    configured: str | None = None,
) -> bool:
    if not column:
        return False
    if dashboard_temporal_column_is_native(column):
        return True
    data_type = (column["data_type"] or "").lower()
    column_name = column["name"]
    if data_type not in STRING_TEMPORAL_TYPES:
        return False
    is_canonical = column_name in CANONICAL_TEMPORAL_COLUMNS
    is_configured = bool(configured and configured == column_name)
    if is_canonical or is_configured:
        return dashboard_temporal_column_has_valid_dates(source, column_name)
    return False


def first_dashboard_datetime_column(table_name: str, columns: set[str] | None = None) -> str | None:
    allowed_columns = columns or {column["name"] for column in dashboard_all_table_columns(table_name)}
    for column in dashboard_all_table_columns(table_name):
        if column["name"] in allowed_columns and dashboard_temporal_column_is_native(column):
            return column["name"]
    return None


def source_columns_map(sources: list[DashboardSource]) -> dict[int, list[dict]]:
    return {source.id: dashboard_table_columns(source.source_table) for source in sources}


def dashboard_source_row_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    with SessionLocal() as db:
        tables = dashboard_available_tables(db)
    for table in tables:
        counts[table["name"]] = table["estimated_rows"]
        counts[table["name"].lower()] = table["estimated_rows"]
    return counts


def sync_dashboard_sources(db: Session) -> list[dict]:
    tables = dashboard_available_tables(db)
    configured_sources = db.query(DashboardSource).all()
    configured_keys = {source.source_table.lower() for source in configured_sources}
    changed = False

    orphaned = (
        db.query(DashboardSource)
        .filter(
            DashboardSource.source_table.in_(DASHBOARD_INTERNAL_TABLES),
            DashboardSource.is_active.is_(False),
        )
        .all()
    )
    for source in orphaned:
        db.delete(source)
        changed = True

    for table in tables:
        source_name = table["name"]
        if source_name.lower() in configured_keys:
            continue
        db.add(DashboardSource(name=table["display_name"], source_table=source_name, is_active=False))
        changed = True

    if changed:
        db.commit()
    return tables


def dashboard_source_payload(
    source: DashboardSource,
    row_counts: dict[str, int] | None = None,
    include_columns: bool = False,
) -> dict:
    row_counts = row_counts or {}
    row_count = row_counts.get(source.source_table, row_counts.get(source.source_table.lower(), 0))
    table_exists = dashboard_source_table_exists(source.source_table)
    database_name, physical_table = dashboard_source_parts(source.source_table)
    payload = {
        "id": source.id,
        "table_name": source.source_table,
        "physical_table_name": physical_table,
        "database": database_name,
        "display_table_name": source.source_table,
        "display_name": source.name,
        "name": source.name,
        "is_active": source.is_active,
        "row_count": row_count if table_exists else None,
        "table_exists": table_exists,
        "status_badge": None if table_exists else "Tabela não encontrada",
        "source_type": "tabela_origem",
        "default_time_field": source.default_time_field,
        "category": source.category,
        "description": source.description,
        "note": source.note,
    }
    if include_columns:
        payload["columns"] = [column["name"] for column in dashboard_table_columns(source.source_table)]
    return payload


def dashboard_source_ajax_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in accept
        or "application/json" in content_type
    )


def dashboard_ajax_error(request: Request, message: str, status_code: int = 400) -> JSONResponse | None:
    if dashboard_source_ajax_request(request):
        return JSONResponse({"success": False, "error": message}, status_code=status_code)
    return None


def dashboard_default_dates() -> tuple[date, date]:
    today = date.today()
    return today.replace(month=1, day=1), today


def dashboard_dates(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    default_from, default_to = dashboard_default_dates()
    try:
        parsed_from = date.fromisoformat(date_from) if date_from else default_from
        parsed_to = date.fromisoformat(date_to) if date_to else default_to
    except ValueError as exc:
        raise ValueError("Periodo global invalido. Use datas no formato YYYY-MM-DD.") from exc
    if parsed_from > parsed_to:
        raise ValueError("Data inicial do Dashboard deve ser menor ou igual a data final.")
    return parsed_from, parsed_to


def calcular_periodo_comparativo(data_inicio: date, data_fim: date, comparar_com: str | None) -> tuple[date, date] | None:
    if comparar_com == "mes_anterior":
        primeiro_mes_atual = data_inicio.replace(day=1)
        fim_mes_anterior = primeiro_mes_atual - timedelta(days=1)
        return fim_mes_anterior.replace(day=1), fim_mes_anterior
    if comparar_com == "ano_anterior":
        try:
            return data_inicio.replace(year=data_inicio.year - 1), data_fim.replace(year=data_fim.year - 1)
        except ValueError:
            return (
                data_inicio.replace(year=data_inicio.year - 1, day=28),
                data_fim.replace(year=data_fim.year - 1, day=28),
            )
    return None


def dashboard_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def widget_time_field(widget: DashboardWidget, columns: set[str]) -> str | None:
    return source_time_field(widget.source, columns, widget.time_field, widget.source.default_time_field)


def source_time_field(
    source: DashboardSource,
    columns: set[str],
    configured: str | None = None,
    default_time_field: str | None = None,
) -> str | None:
    table_columns = dashboard_all_table_columns(source.source_table)
    column_by_name = {column["name"]: column for column in table_columns}
    candidates = [
        (configured or "").strip(),
        (default_time_field or source.default_time_field or "").strip(),
        *CANONICAL_TEMPORAL_COLUMNS,
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        column = column_by_name.get(candidate)
        if dashboard_temporal_column_is_usable(source, column, configured=candidate):
            return candidate
    return first_dashboard_datetime_column(source.source_table, {column["name"] for column in table_columns})


def widget_result_columns(label_field: str | None, series_field: str | None) -> list[dict]:
    columns = []
    if label_field:
        columns.append({"key": "label", "title": label_field})
    if series_field and series_field != label_field:
        columns.append({"key": "series", "title": series_field})
    return columns


def widget_value_title(widget: DashboardWidget) -> str:
    return widget.value_field if widget.value_field and widget.aggregation != "count" else "Total"


def widget_aggregate_sql(widget: DashboardWidget) -> str:
    if widget.aggregation == "count":
        return "COUNT(*)"
    return f"{widget.aggregation.upper()}({quote_column(widget.value_field)})"


def execute_widget_aggregate(
    widget: DashboardWidget,
    time_field: str | None,
    date_from: date | None = None,
    date_to: date | None = None,
    force_count: bool = False,
) -> float:
    database_name, physical_table = dashboard_source_parts(widget.source.source_table)
    safe_database = quote_identifier(database_name)
    safe_table = quote_identifier(physical_table)
    where_sql = ""
    params = {}
    if time_field and date_from and date_to:
        col_type = _get_column_type(physical_table, time_field, database_name)
        filter_expr = _period_filter_expr(quote_column(time_field), col_type)
        where_sql = f" WHERE {filter_expr}"
        params = {"date_from": date_from, "date_to": date_to}
    aggregate_sql = "COUNT(*)" if force_count else widget_aggregate_sql(widget)
    query = f"SELECT COALESCE({aggregate_sql}, 0) AS `value` FROM {safe_database}.{safe_table}{where_sql}"
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        return float(connection.execute(text(query), params).scalar() or 0)


def get_source_count(db: Session | None, source: DashboardSource, data_inicio: date, data_fim: date) -> int:
    columns = dashboard_column_names(source)
    safe_time_field = source_time_field(source, columns)
    database_name, physical_table = dashboard_source_parts(source.source_table)
    safe_database = quote_identifier(database_name)
    safe_table = quote_identifier(physical_table)
    where_sql = ""
    params = {}
    if safe_time_field:
        col_type = _get_column_type(physical_table, safe_time_field, database_name)
        filter_expr = _period_filter_expr(quote_column(safe_time_field), col_type)
        where_sql = f" WHERE {filter_expr}"
        params = {"date_from": data_inicio, "date_to": data_fim}
    else:
        logger.warning(
            "Dashboard source %s (%s) has no valid temporal field; counting without period filter.",
            source.id,
            source.source_table,
        )
    query = f"SELECT COUNT(*) FROM {safe_database}.{safe_table}{where_sql}"
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        return int(connection.execute(text(query), params).scalar() or 0)


def apply_widget_comparison(result: dict, widget: DashboardWidget, time_field: str | None, date_from: date, date_to: date) -> None:
    if widget.source_id_b and widget.source_b:
        valor_a = get_source_count(None, widget.source, date_from, date_to)
        valor_b = get_source_count(None, widget.source_b, date_from, date_to)
        variacao_pct = None
        direcao = "neutral"
        if valor_b:
            variacao_pct = ((valor_a - valor_b) / valor_b) * 100
            if valor_a > valor_b:
                direcao = "up"
            elif valor_a < valor_b:
                direcao = "down"
        result["valor_a"] = valor_a
        result["valor_b"] = valor_b
        result["valor_atual"] = valor_a
        result["valor_comparativo"] = valor_b
        result["variacao_pct"] = variacao_pct
        result["direcao"] = direcao
        result["label_a"] = widget.title
        result["label_b"] = widget.source_b.name
        result["rows"] = [{"label": widget.title, "value": valor_a}, {"label": widget.source_b.name, "value": valor_b}]
        return

    current_value = execute_widget_aggregate(widget, time_field, date_from, date_to)
    compare_value = None
    variation = None
    direction = "neutral"
    compare_period = calcular_periodo_comparativo(date_from, date_to, widget.comparar_com)
    if compare_period:
        compare_value = execute_widget_aggregate(widget, time_field, compare_period[0], compare_period[1])
        if compare_value:
            variation = ((current_value - compare_value) / compare_value) * 100
            if variation > 0:
                direction = "up"
            elif variation < 0:
                direction = "down"
        elif current_value:
            variation = 100.0
            direction = "up"
        else:
            variation = 0.0
    result["valor_atual"] = current_value
    result["valor_comparativo"] = compare_value
    result["variacao_pct"] = variation
    result["direcao"] = direction
    result["rows"] = [{"label": "Total", "value": current_value}]


def apply_widget_gauge(result: dict, widget: DashboardWidget, time_field: str | None, date_from: date, date_to: date) -> None:
    if widget.source_id_b and widget.source_b:
        total_a = get_source_count(settings, widget.source, date_from, date_to)
        total_b = get_source_count(settings, widget.source_b, date_from, date_to)
        universo = total_a + total_b
        percentual = (total_a / universo * 100) if universo else 0.0
        status_value = "ok"
        if widget.meta_gauge is not None:
            if percentual >= widget.meta_gauge:
                status_value = "ok"
            elif percentual >= widget.meta_gauge * 0.85:
                status_value = "atencao"
            else:
                status_value = "critico"
        result["percentual"] = percentual
        result["meta_gauge"] = widget.meta_gauge
        result["status"] = status_value
        result["total_a"] = total_a
        result["total_b"] = total_b
        result["universo"] = universo
        result["rows"] = [{"label": widget.source.name, "value": total_a}, {"label": widget.source_b.name, "value": total_b}]
        return

    total = execute_widget_aggregate(widget, None, force_count=True)
    filtered = execute_widget_aggregate(widget, time_field, date_from, date_to, force_count=True) if time_field else total
    percentual = (filtered / total * 100) if total else 0
    status_value = "ok"
    if widget.meta_gauge is not None:
        if percentual >= widget.meta_gauge:
            status_value = "ok"
        elif percentual >= widget.meta_gauge * 0.85:
            status_value = "atencao"
        else:
            status_value = "critico"
    result["total"] = total
    result["filtrado"] = filtered
    result["percentual"] = percentual
    result["status"] = status_value
    result["rows"] = [{"label": "Filtrado", "value": filtered}, {"label": "Total", "value": total}]


def load_widget_data(widget: DashboardWidget, date_from: date, date_to: date) -> dict:
    source = widget.source
    columns = dashboard_column_names(source)
    time_field = widget_time_field(widget, columns)
    result = {
        "widget": widget,
        "source": source,
        "rows": [],
        "columns": [],
        "time_field": time_field,
        "uses_timeline": bool(time_field),
        "error": None,
    }
    if not source.is_active or not dashboard_source_table_exists(source.source_table):
        result["error"] = "Fonte Dashboard indisponivel."
        return result
    if widget.source_id_b and (
        not widget.source_b
        or not widget.source_b.is_active
        or not dashboard_source_table_exists(widget.source_b.source_table)
    ):
        result["error"] = "Fonte B indisponivel."
        return result

    label_field = widget.label_field if widget.label_field in columns else None
    series_field = widget.series_field if widget.series_field in columns else None
    if widget.aggregation not in DASHBOARD_AGGREGATIONS:
        result["error"] = "Agregacao do widget invalida."
        return result
    if widget.aggregation != "count" and widget.value_field not in columns:
        result["error"] = "Campo valor nao esta disponivel na fonte."
        return result
    result["columns"] = [
        *widget_result_columns(label_field, series_field),
        {"key": "value", "title": widget_value_title(widget)},
    ]

    if widget.widget_type in {"kpi", "comparativo", "gauge"}:
        try:
            if widget.widget_type == "gauge":
                apply_widget_gauge(result, widget, time_field, date_from, date_to)
            else:
                apply_widget_comparison(result, widget, time_field, date_from, date_to)
        except SQLAlchemyError as exc:
            result["error"] = f"Nao foi possivel consultar a fonte local: {exc}"
        return result

    selected = []
    group_fields = []
    if label_field:
        selected.append(f"{quote_column(label_field)} AS `label`")
        group_fields.append(label_field)
    if series_field and series_field != label_field:
        selected.append(f"{quote_column(series_field)} AS `series`")
        group_fields.append(series_field)
    value_sql = widget_aggregate_sql(widget)
    selected.append(f"{value_sql} AS `value`")

    database_name, physical_table = dashboard_source_parts(source.source_table)
    safe_database = quote_identifier(database_name)
    safe_table = quote_identifier(physical_table)
    where_sql = ""
    params = {}
    if time_field:
        col_type = _get_column_type(physical_table, time_field, database_name)
        filter_expr = _period_filter_expr(quote_column(time_field), col_type)
        where_sql = f" WHERE {filter_expr}"
        params = {"date_from": date_from, "date_to": date_to}
    group_sql = f" GROUP BY {', '.join(quote_column(field) for field in group_fields)}" if group_fields else ""
    top_n = dashboard_int(str(widget.top_n), 10, 1, 100)
    query = (
        f"SELECT {', '.join(selected)} FROM {safe_database}.{safe_table}"
        f"{where_sql}{group_sql} ORDER BY `value` DESC LIMIT {top_n}"
    )
    try:
        with local_engine.connect() as connection:
            apply_query_timeout(connection)
            rows = connection.execute(text(query), params).mappings()
            result["rows"] = [
                {key: dashboard_value(value) for key, value in row.items()}
                for row in rows
            ]
    except SQLAlchemyError as exc:
        result["error"] = f"Nao foi possivel consultar a fonte local: {exc}"
    return result


def hidden_dashboard_columns() -> set[str]:
    with local_engine.connect() as connection:
        value = connection.execute(
            text("SELECT `value` FROM system_config WHERE `key` = :key"),
            {"key": HIDDEN_COLUMNS_KEY},
        ).scalar()
    raw = value or DEFAULT_HIDDEN_COLUMNS
    return {item.strip() for item in raw.split(",") if item.strip()}


def is_dashboard_technical_column(column: str) -> bool:
    return column.startswith("__") or column in hidden_dashboard_columns()


def preview_destination_table(destination_table: str | None) -> dict:
    preview = {
        "columns": [],
        "rows": [],
        "message": None,
        "updated_at": datetime.utcnow(),
    }
    if not destination_table:
        preview["message"] = "Tabela destino local não configurada."
        return preview
    if not IDENTIFIER_RE.match(destination_table):
        preview["message"] = "Tabela destino local com nome invalido."
        return preview

    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(destination_table)
    try:
        with local_engine.connect() as connection:
            exists = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
                ),
                {"database_name": settings.local_db_name, "table_name": destination_table},
            ).scalar_one()
            if not exists:
                preview["message"] = "Tabela local ainda nao existe."
                return preview

            result = connection.execute(text(f"SELECT * FROM {safe_database}.{safe_table} LIMIT {PREVIEW_ROW_LIMIT}"))
            columns = [
                column
                for column in result.keys()
                if not is_dashboard_technical_column(column)
            ][:PREVIEW_COLUMN_LIMIT]
            rows = [{column: row._mapping.get(column) for column in columns} for row in result]
            preview["columns"] = columns
            preview["rows"] = rows
            if not rows:
                preview["message"] = "Tabela local ainda não possui dados."
            elif not columns:
                preview["message"] = "Tabela local possui apenas colunas técnicas para ocultar."
            return preview
    except SQLAlchemyError as exc:
        preview["message"] = f"Nao foi possivel carregar o preview da tabela local: {exc}"
        return preview


def visible_report_columns(columns: list[str]) -> list[str]:
    return [column for column in columns if not is_dashboard_technical_column(column)]


def first_datetime_column(table_name: str) -> str | None:
    columns = get_date_columns(table_name)
    return columns[0]["name"] if columns else None


def default_report_detail_dates() -> tuple[date, date]:
    today = date.today()
    return today.replace(month=1, day=1), today


def parse_report_detail_dates(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    default_from, default_to = default_report_detail_dates()
    try:
        start = date.fromisoformat(date_from) if date_from else default_from
        end = date.fromisoformat(date_to) if date_to else default_to
    except ValueError as exc:
        raise ValueError("Filtro de data invalido. Use o formato YYYY-MM-DD.") from exc
    if start > end:
        raise ValueError("Data inicio deve ser menor ou igual a data fim.")
    return start, end


def report_detail_page_params(page: str | None, page_size: str | None) -> tuple[int, int]:
    try:
        parsed_page = int(page or 1)
    except ValueError:
        parsed_page = 1
    try:
        parsed_page_size = int(page_size or REPORT_DETAIL_PAGE_SIZE)
    except ValueError:
        parsed_page_size = REPORT_DETAIL_PAGE_SIZE
    return max(1, parsed_page), max(1, min(parsed_page_size, REPORT_DETAIL_MAX_PAGE_SIZE))


def report_table_metadata(
    table_name: str | None,
    report: Report | None = None,
    db: Session | None = None,
) -> tuple[dict | None, str | None]:
    if not table_name or not IDENTIFIER_RE.match(table_name):
        return None, "Relatorio sem tabela destino local valida."

    try:
        with local_engine.connect() as connection:
            table = connection.execute(
                text(
                    "SELECT TABLE_ROWS FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
                ),
                {"database_name": settings.local_db_name, "table_name": table_name},
            ).mappings().first()
            if not table:
                return None, "Tabela destino local ainda nao existe."
            columns = [
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name "
                        "ORDER BY ORDINAL_POSITION"
                    ),
                    {"database_name": settings.local_db_name, "table_name": table_name},
                )
            ]
    except SQLAlchemyError as exc:
        return None, f"Nao foi possivel inspecionar a tabela local: {exc}"

    visible_columns = visible_report_columns(columns)
    if not visible_columns:
        return None, "Tabela destino local nao possui colunas visiveis do relatorio."
    plan = resolve_temporal_plan(db, report) if db is not None and report is not None else None
    date_column = plan.default_column if plan else next(
        (column for column in REPORT_DETAIL_DATE_COLUMNS if column in columns),
        None,
    )
    if not date_column:
        date_column = first_datetime_column(table_name)
    return {
        "destination_table": table_name,
        "columns": visible_columns,
        "date_column": date_column,
        "date_source_table": plan.source_table if plan else table_name,
        "date_columns": plan.available_columns if plan else get_date_columns(table_name),
        "date_inherited": bool(plan and plan.inherited),
        "temporal_plan": plan,
        "estimated_rows": int(table["TABLE_ROWS"] or 0),
    }, None


def principal_report_metadata(report: Report, db: Session | None = None) -> tuple[dict | None, str | None]:
    return report_table_metadata(report.destination_table, report, db)


def detail_empty_filter() -> tuple[str, dict, str | None]:
    return "", {}, None


def report_detail_date_range(destination_table: str, date_column: str | None) -> dict | None:
    if not date_column:
        return None
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(destination_table)
    safe_column = quote_column(date_column)
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        row = connection.execute(
            text(
                f"SELECT MIN(DATE({safe_column})) AS date_from, MAX(DATE({safe_column})) AS date_to "
                f"FROM {safe_database}.{safe_table} WHERE {safe_column} IS NOT NULL AND {safe_column} <> ''"
            )
        ).mappings().one()
    if not row["date_from"] and not row["date_to"]:
        return None
    return {
        "date_from": report_detail_date_value(row["date_from"]),
        "date_to": report_detail_date_value(row["date_to"]),
        "column": date_column,
    }


def report_detail_date_value(value) -> str | None:
    if not value:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def format_modal_exact_datetime(value: datetime | None) -> str:
    if not value:
        return ""
    return format_datetime_portal(value, "America/Sao_Paulo", "%d/%m/%Y %H:%M")


def modal_freshness_payload(value: datetime | None) -> dict:
    if not value:
        return {"label": "Atualização não identificada", "level": "neutral", "exact": ""}
    elapsed = max(0, int((datetime.utcnow() - value.replace(tzinfo=None)).total_seconds()))
    if elapsed < 60:
        label = "Atualizado agora"
    else:
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        if hours:
            label = f"Atualizado há {hours}h {minutes}min"
        else:
            label = f"Atualizado há {minutes}min"
    level = "neutral" if elapsed < 3600 else "amber" if elapsed <= 21600 else "red"
    return {"label": label, "level": level, "exact": format_modal_exact_datetime(value)}


def report_freshness(db: Session, report: Report) -> dict:
    latest_execution = (
        db.query(ReportExecution.executed_at)
        .filter(ReportExecution.report_id == report.id, ReportExecution.status == "success")
        .order_by(ReportExecution.executed_at.desc())
        .first()
    )
    value = latest_execution[0] if latest_execution else report.updated_at
    return modal_freshness_payload(value)


def source_freshness(db: Session, source: DashboardSource) -> dict:
    report = db.query(Report).filter(Report.destination_table == source.source_table).first()
    if report:
        return report_freshness(db, report)
    return modal_freshness_payload(source.updated_at)


def total_table_count(table_name: str) -> int:
    if not IDENTIFIER_RE.match(table_name or ""):
        return 0
    now = time.time()
    cached = TOTAL_COUNT_CACHE.get(table_name)
    if cached and cached[0] > now:
        return cached[1]
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        total = int(connection.execute(text(f"SELECT COUNT(*) FROM {safe_database}.{safe_table}")).scalar_one())
    TOTAL_COUNT_CACHE[table_name] = (now + COUNT_CACHE_TTL, total)
    return total


def detail_count_payload(total: int, period_total: int | None = None) -> dict:
    return {"total": total, "period": period_total}


def serialize_detail_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def serialize_detail_row(row, columns: list[str]) -> dict:
    return {column: serialize_detail_value(row._mapping.get(column)) for column in columns}


def count_report_detail_rows(metadata: dict, filter_sql: str, params: dict) -> int:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(metadata["destination_table"])
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        return int(
            connection.execute(
                text(f"SELECT COUNT(*) FROM {safe_database}.{safe_table}{filter_sql}"),
                params,
            ).scalar_one()
        )


def search_filter_sql(columns: list[str], search: str | None) -> tuple[str, dict]:
    cleaned = (search or "").strip()
    if not cleaned or not columns:
        return "", {}
    clauses = [f"CAST({quote_column(column)} AS CHAR) LIKE :search" for column in columns]
    return f"({' OR '.join(clauses)})", {"search": f"%{cleaned}%"}


def combine_where(parts: list[str]) -> str:
    filtered = [part for part in parts if part]
    return f" WHERE {' AND '.join(filtered)}" if filtered else ""


def search_table_detail(
    table_name: str,
    columns: list[str],
    page: int,
    page_size: int,
    search: str | None,
    sort_col: str | None,
    sort_dir: str | None,
    date_column: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[int, list[dict]]:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    column_sql = ", ".join(quote_column(column) for column in columns)
    if not columns:
        logger.debug(
            "Dashboard detail search skipped for %s: no visible columns. search=%r date_column=%r date_from=%r date_to=%r",
            table_name,
            search,
            date_column,
            date_from,
            date_to,
        )
        return 0, []
    search_sql, params = search_filter_sql(columns, search)
    date_sql = ""
    if date_column and date_from and date_to:
        date_sql = f"{quote_column(date_column)} BETWEEN :date_from AND :date_to"
        params.update({"date_from": date_from, "date_to": date_to})
    where_sql = combine_where([search_sql, date_sql])
    order_sql = report_detail_order_sql({"columns": columns, "date_column": date_column}, sort_col, sort_dir)
    query_params = {**params, "limit": page_size, "offset": (page - 1) * page_size}
    count_query = f"SELECT COUNT(*) FROM {safe_database}.{safe_table}{where_sql}"
    rows_query = f"SELECT {column_sql} FROM {safe_database}.{safe_table}{where_sql}{order_sql} LIMIT :limit OFFSET :offset"
    logger.debug(
        "Dashboard detail search SQL table=%s columns=%s search=%r count_query=%s rows_query=%s params=%s",
        table_name,
        columns,
        search,
        count_query,
        rows_query,
        params,
    )
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        total = int(
            connection.execute(
                text(count_query),
                params,
            ).scalar_one()
        )
        rows = connection.execute(
            text(rows_query),
            query_params,
        )
        return total, [serialize_detail_row(row, columns) for row in rows]


def safe_detail_sort(metadata: dict, sort_col: str | None, sort_dir: str | None) -> tuple[str | None, str | None]:
    if sort_col not in metadata["columns"]:
        return None, None
    normalized_dir = (sort_dir or "").lower()
    if normalized_dir not in {"asc", "desc"}:
        return None, None
    return sort_col, normalized_dir


def report_detail_order_sql(metadata: dict, sort_col: str | None, sort_dir: str | None) -> str:
    safe_sort_col, safe_sort_dir = safe_detail_sort(metadata, sort_col, sort_dir)
    if safe_sort_col and safe_sort_dir:
        return f" ORDER BY {quote_column(safe_sort_col)} {safe_sort_dir.upper()}"
    if metadata.get("date_inherited"):
        return ""
    return f" ORDER BY {quote_column(metadata['date_column'])} DESC" if metadata.get("date_column") else ""


def fetch_report_detail_rows(
    metadata: dict,
    filter_sql: str,
    params: dict,
    page: int,
    page_size: int,
    sort_col: str | None = None,
    sort_dir: str | None = None,
) -> list[dict]:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(metadata["destination_table"])
    column_sql = ", ".join(quote_column(column) for column in metadata["columns"])
    query_params = {**params, "limit": page_size, "offset": (page - 1) * page_size}
    order_sql = report_detail_order_sql(metadata, sort_col, sort_dir)
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        rows = connection.execute(
            text(
                f"SELECT {column_sql} FROM {safe_database}.{safe_table}"
                f"{filter_sql}{order_sql} LIMIT :limit OFFSET :offset"
            ),
            query_params,
        )
        return [serialize_detail_row(row, metadata["columns"]) for row in rows]


def stream_report_detail_rows(metadata: dict, filter_sql: str, params: dict, sort_col: str | None = None, sort_dir: str | None = None):
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(metadata["destination_table"])
    column_sql = ", ".join(quote_column(column) for column in metadata["columns"])
    order_sql = report_detail_order_sql(metadata, sort_col, sort_dir)
    with local_engine.connect() as connection:
        apply_query_timeout(connection)
        result = connection.execution_options(stream_results=True).execute(
            text(f"SELECT {column_sql} FROM {safe_database}.{safe_table}{filter_sql}{order_sql}"),
            params,
        )
        for row in result:
            yield serialize_detail_row(row, metadata["columns"])


def temp_report_name(report_id: int, user_id: int) -> str:
    return f"tmp_report_{report_id}_{user_id}_{int(time.time() * 1000000)}"


def date_period_params(date_from: date, date_to: date) -> dict:
    return {
        "date_from": datetime.combine(date_from, datetime_time.min),
        "date_to": datetime.combine(date_to, datetime_time.max.replace(microsecond=0)),
    }


def top_level_keyword_index(sql: str, keyword: str) -> int:
    target = keyword.lower()
    depth = 0
    quote_char = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote_char:
            if char == "\\":
                index += 2
                continue
            if char == quote_char:
                quote_char = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote_char = char
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0 and sql[index : index + len(keyword)].lower() == target:
            before = sql[index - 1] if index > 0 else " "
            after_index = index + len(keyword)
            after = sql[after_index] if after_index < len(sql) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return index
        index += 1
    return -1


def top_level_clause_index(sql: str) -> int:
    indexes = [
        top_level_keyword_index(sql, keyword)
        for keyword in ("GROUP BY", "HAVING", "ORDER BY", "LIMIT")
    ]
    valid_indexes = [index for index in indexes if index >= 0]
    return min(valid_indexes) if valid_indexes else len(sql)


def quote_period_field(period_field: str) -> str:
    return ".".join(quote_column(part) for part in period_field.split("."))


def split_top_level_select_items(select_list: str) -> list[str]:
    items = []
    start = 0
    depth = 0
    quote_char = ""
    index = 0
    while index < len(select_list):
        char = select_list[index]
        if quote_char:
            if char == "\\":
                index += 2
                continue
            if char == quote_char:
                quote_char = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote_char = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            items.append(select_list[start:index].strip())
            start = index + 1
        index += 1
    tail = select_list[start:].strip()
    if tail:
        items.append(tail)
    return items


def unquote_identifier_part(identifier: str) -> str:
    value = identifier.strip()
    if len(value) >= 2 and value[0] == "`" and value[-1] == "`":
        return value[1:-1].replace("``", "`")
    return value


def select_item_exposes_column(item: str, column_name: str) -> bool:
    cleaned = item.strip()
    if cleaned == "*" or cleaned.endswith(".*") or cleaned.endswith("`.*"):
        return True

    alias_match = re.search(r"\s+AS\s+(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)\s*$", cleaned, re.I)
    if alias_match:
        return unquote_identifier_part(alias_match.group(1)) == column_name

    parts = re.split(r"\s+", cleaned)
    if len(parts) > 1:
        possible_alias = parts[-1]
        if re.fullmatch(r"`[^`]+`|[A-Za-z_][A-Za-z0-9_]*", possible_alias):
            return unquote_identifier_part(possible_alias) == column_name

    expression = cleaned.rsplit(".", 1)[-1]
    return unquote_identifier_part(expression) == column_name


def select_exposes_column(sql: str, column_name: str) -> bool:
    select_index = top_level_keyword_index(sql, "SELECT")
    if select_index < 0:
        return False
    from_index = top_level_keyword_index(sql[select_index + len("SELECT"):], "FROM")
    if from_index < 0:
        return False
    select_start = select_index + len("SELECT")
    select_end = select_start + from_index
    select_items = split_top_level_select_items(sql[select_start:select_end])
    return any(select_item_exposes_column(item, column_name) for item in select_items)


def _get_column_type(table: str, column: str, database: str) -> str | None:
    """Retorna o tipo SQL real da coluna usada pelo plano temporal."""
    with local_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT column_type "
                "FROM information_schema.columns "
                "WHERE table_schema = :database "
                "AND table_name = :table "
                "AND column_name = :column"
            ),
            {"database": database, "table": table, "column": column},
        ).first()
    return str(row[0]).upper() if row else None


def _period_filter_expr(column_sql: str, col_type: str | None) -> str:
    """Gera comparação temporal nativa ou converte datas ISO/BR textuais."""
    if col_type and "TEXT" in col_type.upper():
        converted = (
            "COALESCE("
            f"STR_TO_DATE({column_sql}, {ISO_DATETIME_SECONDS_FMT}), "
            f"STR_TO_DATE({column_sql}, {ISO_DATETIME_FMT}), "
            f"STR_TO_DATE({column_sql}, {ISO_DATE_FMT}), "
            f"STR_TO_DATE({column_sql}, {BRAZILIAN_DATETIME_SECONDS_FMT}), "
            f"STR_TO_DATE({column_sql}, {BRAZILIAN_DATETIME_FMT}), "
            f"STR_TO_DATE({column_sql}, {BRAZILIAN_DATE_FMT})"
            ")"
        )
        return (
            f"{converted} BETWEEN "
            "STR_TO_DATE(:date_from, '%Y-%m-%d') AND "
            "STR_TO_DATE(:date_to, '%Y-%m-%d')"
        )
    return f"{column_sql} BETWEEN :date_from AND :date_to"


def direct_destination_period_filter_sql(
    destination_table: str,
    outer_column: str,
    col_type: str | None = None,
) -> str:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(destination_table)
    return (
        f"SELECT * FROM {safe_database}.{safe_table} "
        f"WHERE {_period_filter_expr(quote_column(outer_column), col_type)}"
    )


def inject_source_period_filter(
    sql: str,
    period_field: str,
    col_type: str | None = None,
) -> str:
    clause_index = top_level_clause_index(sql)
    head = sql[:clause_index].rstrip()
    tail = sql[clause_index:]
    operator = "AND" if top_level_keyword_index(head, "WHERE") >= 0 else "WHERE"
    condition = _period_filter_expr(quote_period_field(period_field), col_type)
    tail_sql = f" {tail.lstrip()}" if tail else ""
    return f"{head} {operator} {condition}{tail_sql}"


def destination_date_columns_with_values(table_name: str) -> list[dict]:
    columns = get_date_columns(table_name or "")
    if not table_name or not columns:
        return []
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    available_columns = []
    with local_engine.connect() as connection:
        for column in columns:
            column_name = column["name"]
            row = connection.execute(
                text(
                    f"SELECT 1 FROM {safe_database}.{safe_table} "
                    f"WHERE {quote_column(column_name)} IS NOT NULL LIMIT 1"
                )
            ).first()
            if row:
                available_columns.append(column)
    return available_columns


def get_parent_primary_report(db: Session, report: Report) -> Report | None:
    """Encontra o primário cuja tabela destino é usada no FROM/JOIN do derivado."""
    if report.is_primary or not report.sql_query:
        return None
    table_refs = re.findall(
        r"\bFROM\s+`?(\w+)`?|\bJOIN\s+`?(\w+)`?",
        report.sql_query,
        re.IGNORECASE,
    )
    referenced = {table for pair in table_refs for table in pair if table}
    for table_name in referenced:
        parent = db.query(Report).filter(
            Report.destination_table == table_name,
            Report.is_primary.is_(True),
            Report.is_active.is_(True),
        ).first()
        if parent:
            return parent
    return None


def _choose_injection_strategy(report: Report, column: str) -> str:
    """Escolhe a forma de aplicar o plano sem voltar a resolver a coluna."""
    if not report.sql_query:
        return "direct"
    if select_exposes_column(report.sql_query, column):
        return "wrapper"
    return "source"


def resolve_temporal_plan(
    db: Session,
    report: Report,
    campo_data: str | None = None,
) -> TemporalPlan | None:
    """Resolve uma vez as opções, a seleção e a estratégia temporal do relatório."""
    allowed = list(get_allowed_databases(db))
    destination_columns = get_date_columns(
        report.destination_table or "",
        campo_sql_periodo=(report.campo_sql_periodo or "").rsplit(".", 1)[-1] or None,
    )
    parent = None
    if destination_columns:
        available = destination_columns
        source_table = report.destination_table or ""
        inherited = False
    else:
        parent = get_parent_primary_report(db, report)
        if not parent:
            return None
        available = get_date_columns(
            parent.destination_table or "",
            campo_sql_periodo=(parent.campo_sql_periodo or "").rsplit(".", 1)[-1] or None,
        )
        if not available:
            return None
        source_table = parent.destination_table or ""
        inherited = True

    valid_names = {column["name"] for column in available}
    configured = (report.campo_sql_periodo or "").strip().rsplit(".", 1)[-1]
    if not configured and inherited and parent is not None:
        configured = (parent.campo_sql_periodo or "").strip().rsplit(".", 1)[-1]
    default_column = configured if configured in valid_names else available[0]["name"]
    requested = (campo_data or "").strip().rsplit(".", 1)[-1]
    selected = requested if requested in valid_names else default_column
    strategy = "source" if inherited else _choose_injection_strategy(report, selected)
    return TemporalPlan(
        selected_column=selected,
        source_table=source_table,
        source_database=settings.local_db_name,
        inherited=inherited,
        parent_report_id=parent.id if inherited and parent is not None else None,
        injection_strategy=strategy,
        allowed_databases=allowed,
        available_columns=available,
        default_column=default_column,
    )


def resolve_temporal_field(db: Session, report: Report) -> dict | None:
    """Compatibilidade interna para consumidores de metadados temporais."""
    plan = resolve_temporal_plan(db, report)
    if not plan:
        return None
    label = next(
        (item["label"] for item in plan.available_columns if item["name"] == plan.selected_column),
        plan.selected_column,
    )
    payload = {
        "column": plan.selected_column,
        "label": label,
        "source_table": plan.source_table,
        "source_database": plan.source_database,
        "inherited": plan.inherited,
    }
    if plan.parent_report_id is not None:
        payload["parent_id"] = plan.parent_report_id
    return payload


def period_filter_sql(
    report: Report,
    plan: TemporalPlan,
) -> tuple[str, str]:
    """Gera SQL usando exclusivamente o plano temporal já resolvido."""
    cleaned_sql = validate_select(report.sql_query, plan.allowed_databases)
    mode = report.modo_filtro_periodo or "filtro_externo"
    if mode == "nenhum":
        raise ValueError("Relatorio sem modo de filtro de periodo configurado.")
    if mode != "filtro_externo":
        raise ValueError("Modo de filtro de periodo invalido.")
    if not PERIOD_FILTER_FIELD_RE.fullmatch(plan.selected_column):
        raise ValueError("Filtro de período não disponível para este relatório.")
    column_type = _get_column_type(
        plan.source_table,
        plan.selected_column.rsplit(".", 1)[-1],
        plan.source_database,
    )
    if plan.injection_strategy == "source":
        return validate_select(
            inject_source_period_filter(cleaned_sql, plan.selected_column, column_type),
            plan.allowed_databases,
        ), plan.selected_column
    if plan.injection_strategy == "direct":
        return validate_select(
            direct_destination_period_filter_sql(
                report.destination_table,
                plan.selected_column,
                column_type,
            ),
            plan.allowed_databases,
        ), plan.selected_column
    if plan.injection_strategy != "wrapper":
        raise ValueError("Estrategia de filtro de periodo invalida.")
    filter_expression = _period_filter_expr(
        f"base_filter.{quote_column(plan.selected_column)}",
        column_type,
    )
    filtered_sql = (
        "SELECT * FROM ("
        f"{cleaned_sql}"
        f") AS base_filter WHERE {filter_expression}"
    )
    return validate_select(filtered_sql, plan.allowed_databases), plan.selected_column


def create_temp_report_table(
    report: Report,
    user_id: int,
    date_from: date,
    date_to: date,
    plan: TemporalPlan,
    db: Session,
) -> tuple[str, int]:
    filtered_sql, _ = period_filter_sql(report, plan)
    table_name = temp_report_name(report.id, user_id)
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    created_at = datetime.utcnow()
    expires_at = created_at + REPORT_TEMP_TTL
    catalog_entry = TempReportResult(
        temp_table=table_name,
        report_id=report.id,
        user_id=user_id,
        created_at=created_at,
        expires_at=expires_at,
        row_count=0,
        selected_column=plan.selected_column,
        status="pending",
    )
    db.add(catalog_entry)
    db.commit()
    db.refresh(catalog_entry)
    row_count = 0
    try:
        with local_engine.begin() as connection:
            apply_query_timeout(connection, report.filter_timeout_seconds)
            result = connection.execute(text(filtered_sql), date_period_params(date_from, date_to))
            columns = list(result.keys())
            if not columns:
                raise ValueError("Filtro retornou resultado sem colunas.")
            column_defs = [f"{quote_column(column)} LONGTEXT NULL" for column in columns]
            connection.execute(
                text(
                    f"CREATE TABLE {safe_database}.{safe_table} "
                    f"({', '.join(column_defs)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
            )
            insert_sql = (
                f"INSERT INTO {safe_database}.{safe_table} "
                f"({', '.join(quote_column(column) for column in columns)}) "
                f"VALUES ({', '.join(f':p{index}' for index in range(len(columns)))})"
            )
            batch = []
            for row in result.mappings():
                payload = {f"p{index}": row.get(column) for index, column in enumerate(columns)}
                batch.append(payload)
                if len(batch) >= 1000:
                    connection.execute(text(insert_sql), batch)
                    row_count += len(batch)
                    batch = []
            if batch:
                connection.execute(text(insert_sql), batch)
                row_count += len(batch)
        catalog_entry.row_count = row_count
        catalog_entry.status = "ready"
        db.commit()
    except Exception:
        db.rollback()
        catalog_entry = db.get(TempReportResult, catalog_entry.id)
        if catalog_entry is not None:
            catalog_entry.status = "error"
            db.commit()
        raise
    return table_name, row_count


def temp_report_metadata(
    table_name: str,
    user_id: int,
    report_id: int,
    db: Session,
) -> tuple[dict | None, str | None]:
    if not is_temp_report_table(table_name):
        return None, "Nome de tabela temporaria invalido."
    entry = (
        db.query(TempReportResult)
        .filter(TempReportResult.temp_table == table_name)
        .first()
    )
    if not entry:
        return None, "Resultado temporario nao encontrado."
    if entry.user_id != user_id:
        return None, "Resultado temporario nao pertence ao usuario atual."
    if entry.report_id != report_id:
        return None, "Resultado temporario nao pertence a este relatorio."
    if entry.expires_at <= datetime.utcnow():
        return None, "Resultado temporario expirado."
    if entry.status == "pending":
        return None, "Resultado temporario ainda sendo processado."
    if entry.status == "error":
        return None, "Erro ao gerar resultado temporario."
    if entry.status != "ready":
        return None, "Estado de resultado temporario invalido."
    metadata, error = report_table_metadata(table_name)
    if error:
        return None, error
    metadata["date_column"] = None
    metadata["row_count"] = entry.row_count
    metadata["selected_column"] = entry.selected_column
    metadata["expires_at"] = entry.expires_at
    return metadata, None


def log_report_detail_access(
    db: Session,
    report: Report,
    user_id: int | None,
    status_value: str,
    row_count: int,
    started: float,
) -> None:
    finished_at = datetime.utcnow()
    db.add(
        ReportExecution(
            report_id=report.id,
            user_id=user_id,
            sql_query="Operacao controlada do modal da dashboard em dados locais.",
            destination_table=report.destination_table,
            status=status_value,
            row_count=row_count,
            duration_ms=int((time.perf_counter() - started) * 1000),
            executed_at=finished_at,
            finished_at=finished_at,
        )
    )
    db.commit()


def report_detail_filename(report: Report, date_from: date, date_to: date) -> str:
    safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in report.name).strip("_")
    return f"{safe_name or 'relatorio'}_{date_from.isoformat()}_a_{date_to.isoformat()}.xlsx"


def build_report_card(report: Report, db: Session) -> dict:
    last_execution = (
        db.query(ReportExecution)
        .filter(ReportExecution.report_id == report.id, ReportExecution.status.in_(("success", "error")))
        .order_by(ReportExecution.executed_at.desc())
        .first()
    )
    return {
        "report": report,
        "last_execution": last_execution,
        "preview": preview_destination_table(report.destination_table),
    }


def visible_reports_query(db: Session, user):
    query = db.query(Report)
    if user.is_admin:
        return query
    allowed_categories = user_report_category_names(user)
    if not allowed_categories:
        return query.filter(False)
    return query.filter(Report.category.in_(allowed_categories))


def report_dashboard_access_denied(db: Session, user, report: Report | None, action: str):
    if report:
        log_category_denial(db, user, report, action)


def dashboard_redirect(path: str, message: str, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(
        f"{path}?{key}={quote(message)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def log_dashboard_admin_action(
    db: Session,
    user,
    action: str,
    widget: DashboardWidget | None,
    status_value: str,
    message: str,
) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            table_name=widget.source.source_table if widget and widget.source else None,
            status=status_value,
            message=f"{widget.title}: {message}" if widget else message,
        )
    )
    db.commit()


def log_dashboard_action(
    db: Session,
    user,
    action: str,
    dashboard_item: Dashboard | None,
    status_value: str,
    message: str,
) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            report_name=dashboard_item.name if dashboard_item else None,
            status=status_value,
            message=message,
        )
    )
    db.commit()


def dashboard_categories_from_ids(db: Session, values: list[str]) -> list[ReportCategory]:
    category_ids = []
    for value in values:
        try:
            category_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not category_ids:
        return []
    return (
        db.query(ReportCategory)
        .filter(ReportCategory.id.in_(list(dict.fromkeys(category_ids))))
        .order_by(ReportCategory.name.asc())
        .all()
    )


def accessible_dashboards_query(db: Session, user, only_active: bool = True):
    query = db.query(Dashboard)
    if only_active:
        query = query.filter(Dashboard.is_active.is_(True))
    if user.is_admin:
        return query
    allowed_categories = user_report_category_names(user)
    if not allowed_categories:
        return query.filter(False)
    return query.join(Dashboard.categories).filter(ReportCategory.name.in_(allowed_categories))


def accessible_dashboards(db: Session, user, only_active: bool = True) -> list[Dashboard]:
    return (
        accessible_dashboards_query(db, user, only_active)
        .options(selectinload(Dashboard.categories), selectinload(Dashboard.widgets))
        .order_by(Dashboard.sort_order.asc(), Dashboard.name.asc())
        .distinct()
        .all()
    )


def user_can_access_dashboard(user, dashboard_item: Dashboard | None) -> bool:
    if not dashboard_item or not dashboard_item.is_active:
        return False
    if user.is_admin:
        return True
    allowed_categories = user_report_category_names(user)
    return any(category.name in allowed_categories for category in dashboard_item.categories if category.is_active)


def dashboard_config_path(dashboard_id: int | None = None) -> str:
    return f"/admin/dashboard-config?dashboard_id={dashboard_id}" if dashboard_id else "/admin/dashboard-config"


def selected_widget_ids_from_form(parsed: dict[str, list[str]]) -> list[int]:
    widget_ids = []
    for value in parsed.get("widget_ids", []):
        try:
            widget_ids.append(int(value))
        except ValueError:
            continue
    return list(dict.fromkeys(widget_ids))


@router.get("/admin/dashboard-sources", response_class=HTMLResponse)
def admin_dashboard_sources(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    tables = sync_dashboard_sources(db)
    configured = {source.source_table: source for source in db.query(DashboardSource).all()}
    row_counts = {table["name"]: table["estimated_rows"] for table in tables}
    for table in tables:
        table["source"] = configured.get(table["name"])
        table["columns"] = dashboard_table_columns(table["name"])
    sources = (
        db.query(DashboardSource)
        .order_by(DashboardSource.is_active.desc(), DashboardSource.source_table.asc())
        .all()
    )
    enabled_sources = [
        dashboard_source_payload(source, row_counts, include_columns=True)
        for source in sources
        if source.is_active
    ]
    categories = db.query(ReportCategory).filter(ReportCategory.is_active.is_(True)).order_by(ReportCategory.name.asc()).all()
    return render(
        request,
        "admin_dashboard_sources.html",
        {
            "active": "dashboard_sources",
            "tables": tables,
            "enabled_sources": enabled_sources,
            "total_sources": len(tables),
            "active_sources": len(enabled_sources),
            "categories": categories,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.get("/admin/dashboard-sources/search")
def search_admin_dashboard_sources(
    request: Request,
    q: str = "",
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    tables = sync_dashboard_sources(db)
    cleaned = (q or "").strip()
    if cleaned:
        lowered = cleaned.lower()
        tables = [
            table
            for table in tables
            if lowered in table["name"].lower()
            or lowered in table["table_name"].lower()
            or lowered in table["database"].lower()
        ]
    sources = {source.source_table.lower(): source for source in db.query(DashboardSource).all()}
    items = [
        dashboard_table_list_item(table, sources.get(table["name"].lower()))
        for table in sorted(tables, key=lambda item: (item["database"], item["table_name"]))
    ]
    page, per_page = dashboard_page_params(page, per_page)
    paged_items, pages, current_page = dashboard_paginate(items, page, per_page)
    return JSONResponse(
        {
            "items": paged_items,
            "total": len(items),
            "pages": pages,
            "current_page": current_page,
            "per_page": per_page,
            "query": cleaned,
        }
    )


@router.get("/admin/dashboard-sources/databases")
def list_admin_dashboard_source_databases(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    sync_dashboard_sources(db)
    return JSONResponse(dashboard_database_metadata(db))


@router.get("/admin/dashboard-sources/tables")
def list_admin_dashboard_source_tables(
    request: Request,
    database: str,
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    allowed_databases = set(dashboard_allowed_databases(db))
    if database not in allowed_databases:
        return JSONResponse({"success": False, "error": "Database invalida."}, status_code=400)
    tables = [table for table in sync_dashboard_sources(db) if table["database"] == database]
    sources = {source.source_table.lower(): source for source in db.query(DashboardSource).all()}
    items = [
        dashboard_table_list_item(table, sources.get(table["name"].lower()))
        for table in sorted(tables, key=lambda item: item["table_name"])
    ]
    page, per_page = dashboard_page_params(page, per_page)
    paged_items, pages, current_page = dashboard_paginate(items, page, per_page)
    return JSONResponse(
        {
            "database": database,
            "items": paged_items,
            "total": len(items),
            "pages": pages,
            "current_page": current_page,
            "per_page": per_page,
        }
    )


@router.post("/admin/dashboard-sources/refresh-cache", dependencies=[Depends(verify_csrf_header)])
def refresh_dashboard_sources_cache(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    reset_dashboard_tables_cache()
    return JSONResponse({"success": True})


@router.post("/admin/dashboard-sources/{table_name}")
async def save_admin_dashboard_source(table_name: str, request: Request, db: Session = Depends(get_db)):
    is_ajax = dashboard_source_ajax_request(request)
    if is_ajax:
        await verify_csrf_header(request)
    else:
        await verify_csrf(request)
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    source = db.query(DashboardSource).filter(DashboardSource.source_table == table_name).first()
    table_exists = dashboard_source_table_exists(table_name)
    if not table_exists and source is None:
        if is_ajax:
            return JSONResponse({"success": False, "error": "Tabela local nao permitida para Dashboard."}, status_code=400)
        return dashboard_redirect("/admin/dashboard-sources", "Tabela local nao permitida para Dashboard.", True)
    data = await form_data(request)
    columns = {column["name"] for column in dashboard_table_columns(table_name)} if table_exists else set()
    default_time_field = clean_dashboard_text(data.get("default_time_field"))
    if table_exists and default_time_field and default_time_field not in columns:
        if is_ajax:
            return JSONResponse({"success": False, "error": "Campo temporal padrao invalido."}, status_code=400)
        return dashboard_redirect("/admin/dashboard-sources", "Campo temporal padrao invalido.", True)
    if source is None:
        source = DashboardSource(name=table_name, source_table=table_name)
        db.add(source)
    source.name = clean_dashboard_text(data.get("name")) or table_name
    source.description = clean_dashboard_text(data.get("description"))
    source.is_active = data.get("is_active") == "on"
    source.default_time_field = default_time_field
    source.category = clean_dashboard_text(data.get("category"))
    source.note = clean_dashboard_text(data.get("note"))
    db.commit()
    if is_ajax:
        return JSONResponse({"success": True, "source_id": source.id})
    return dashboard_redirect("/admin/dashboard-sources", f"Fonte Dashboard salva: {source.name}.")


@router.post("/admin/dashboard-sources/{source_id}/enable", dependencies=[Depends(verify_csrf_header)])
def enable_admin_dashboard_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    source = db.get(DashboardSource, source_id)
    if source is None:
        return JSONResponse({"success": False, "error": "Fonte nao encontrada."}, status_code=404)
    source.is_active = True
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action="dashboard_source_enable",
            table_name=source.source_table,
            status="success",
            message=f"Fonte Dashboard habilitada: {source.name}.",
        )
    )
    db.commit()
    row_counts = dashboard_source_row_counts()
    return JSONResponse(
        {
            "success": True,
            "is_active": True,
            "source": dashboard_source_payload(source, row_counts, include_columns=True),
        }
    )


@router.post("/admin/dashboard-sources/{source_id}/disable", dependencies=[Depends(verify_csrf_header)])
def disable_admin_dashboard_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    source = db.get(DashboardSource, source_id)
    if source is None:
        return JSONResponse({"success": False, "error": "Fonte nao encontrada."}, status_code=404)
    source.is_active = False
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action="dashboard_source_disable",
            table_name=source.source_table,
            status="success",
            message=f"Fonte Dashboard desabilitada: {source.name}.",
        )
    )
    db.commit()
    return JSONResponse({"success": True, "is_active": False})


@router.get("/admin/dashboard-sources/{source_id}/dependencies")
def dashboard_source_dependencies(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    deps = source_dependencies(db, source_id)
    if deps["source"] is None:
        return JSONResponse({"success": False, "error": "Fonte nao encontrada."}, status_code=404)
    return JSONResponse({"success": True, **deps["payload"]})


@router.post("/admin/dashboard-sources/{source_id}/delete", dependencies=[Depends(verify_csrf_header)])
def delete_admin_dashboard_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    cascade = request.headers.get("X-Cascade-Confirm") == "true"
    deps = source_dependencies(db, source_id)
    source = deps["source"]
    if source is None:
        return JSONResponse({"success": False, "error": "Fonte nao encontrada."}, status_code=404)
    widgets = deps["widgets"]
    if widgets and not cascade:
        return JSONResponse({"success": False, "requires_cascade": True, **deps["payload"]}, status_code=409)
    detail = (
        f"Fonte Dashboard excluida: {source.name} ({source.source_table}). "
        f"Widgets removidos em cascata: {len(widgets)}"
    )
    if widgets:
        detail += "; " + "; ".join(f"{widget.dashboard.name if widget.dashboard else '-'} - {widget.title}" for widget in widgets)
    delete_widgets(db, widgets)
    db.delete(source)
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action="dashboard_source_delete",
            table_name=source.source_table,
            status="success",
            message=detail,
        )
    )
    db.commit()
    return JSONResponse({"success": True, "message": detail})


def dashboard_widget_context(request: Request, db: Session, error: str | None = None, dashboard_id: int | None = None, user=None) -> HTMLResponse:
    sources = (
        db.query(DashboardSource)
        .filter(DashboardSource.is_active.is_(True))
        .order_by(DashboardSource.category.asc(), DashboardSource.name.asc())
        .all()
    )
    dashboard_query = accessible_dashboards_query(db, user, only_active=False) if user else db.query(Dashboard)
    dashboards = (
        dashboard_query
        .options(selectinload(Dashboard.categories), selectinload(Dashboard.widgets))
        .order_by(Dashboard.sort_order.asc(), Dashboard.name.asc())
        .distinct()
        .all()
    )
    current_dashboard = None
    if dashboards:
        if dashboard_id:
            current_dashboard = next((item for item in dashboards if item.id == dashboard_id), None)
        current_dashboard = current_dashboard or dashboards[0]
    widgets = (
        db.query(DashboardWidget)
        .join(DashboardWidget.source)
        .filter(DashboardWidget.dashboard_id == current_dashboard.id if current_dashboard else False)
        .order_by(DashboardWidget.sort_order.asc(), DashboardWidget.title.asc())
        .all()
    )
    categories = db.query(ReportCategory).order_by(ReportCategory.name.asc()).all()
    return render(
        request,
        "admin_dashboard_config.html",
        {
            "active": "dashboard_config",
            "dashboards": dashboards,
            "current_dashboard": current_dashboard,
            "categories": categories,
            "sources": sources,
            "source_columns": source_columns_map(sources),
            "widgets": widgets,
            "widget_types": sorted(DASHBOARD_WIDGET_TYPES),
            "aggregations": ["count", "sum", "avg", "min", "max"],
            "colors": ["blue", "green", "amber", "red", "violet", "slate"],
            "sizes": ["pequeno", "medio", "grande", "total"],
            "message": request.query_params.get("message"),
            "error": error or request.query_params.get("error"),
        },
        db,
    )


def dashboard_widget_payload(widget: DashboardWidget) -> dict:
    return {
        "id": widget.id,
        "dashboard_id": widget.dashboard_id,
        "title": widget.title,
        "widget_type": widget.widget_type,
        "source": widget.source.name if widget.source else "-",
        "source_table": widget.source.source_table if widget.source else "-",
        "source_id": widget.source_id,
        "source_id_b": widget.source_id_b,
        "sort_order": widget.sort_order,
        "size": widget.size,
    }


def apply_dashboard_widget(
    widget: DashboardWidget,
    source: DashboardSource,
    source_b: DashboardSource | None,
    data: dict[str, str],
) -> None:
    columns = dashboard_column_names(source)
    widget_type = data.get("widget_type", "barra").strip()
    widget_type = widget_type if widget_type in DASHBOARD_WIDGET_TYPES else "barra"
    fields = {
        "label_field": clean_dashboard_text(data.get("label_field")),
        "value_field": clean_dashboard_text(data.get("value_field")),
        "series_field": clean_dashboard_text(data.get("series_field")),
        "time_field": clean_dashboard_text(data.get("time_field")),
    }
    invalid_fields = [value for value in fields.values() if value and value not in columns]
    if invalid_fields:
        raise ValueError(f"Campo nao disponivel na fonte: {invalid_fields[0]}.")
    aggregation = "count" if widget_type == "gauge" else data.get("aggregation", "count").strip().lower()
    if aggregation not in DASHBOARD_AGGREGATIONS:
        raise ValueError("Agregacao do widget invalida.")
    if aggregation != "count" and not fields["value_field"]:
        raise ValueError("Campo valor e obrigatorio para esta agregacao.")

    widget.source = source
    widget.source_b = source_b if widget_type in {"gauge", "comparativo"} else None
    widget.title = clean_dashboard_text(data.get("title")) or f"Widget {source.name}"
    widget.widget_type = widget_type
    widget.label_field = fields["label_field"]
    widget.value_field = fields["value_field"]
    widget.series_field = fields["series_field"]
    widget.time_field = fields["time_field"]
    widget.aggregation = aggregation
    widget.top_n = dashboard_int(data.get("top_n"), 10, 1, 100)
    color = data.get("color", "blue").strip()
    widget.color = color if color in DASHBOARD_COLORS else "blue"
    icon = clean_dashboard_text(data.get("icone"))
    widget.icone = icon if icon in DASHBOARD_ICONS else None
    compare = clean_dashboard_text(data.get("comparar_com"))
    widget.comparar_com = compare if compare in DASHBOARD_COMPARE_PERIODS else None
    widget.meta_gauge = dashboard_float(data.get("meta_gauge"), 0, 100)
    secondary_color = clean_dashboard_text(data.get("cor_secundaria"))
    if widget_type in {"gauge", "comparativo"}:
        widget.cor_secundaria = secondary_color if secondary_color in DASHBOARD_COLORS else "red"
    else:
        widget.cor_secundaria = None
    widget.sort_order = dashboard_int(data.get("sort_order"), 100, 0, 10000)
    size = data.get("size", "medio").strip()
    widget.size = size if size in DASHBOARD_SIZES else "medio"


@router.get("/admin/dashboard-config", response_class=HTMLResponse)
def admin_dashboard_config(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return dashboard_widget_context(
        request,
        db,
        dashboard_id=dashboard_int(request.query_params.get("dashboard_id"), 0, 0, 2147483647) or None,
        user=user,
    )


@router.post("/admin/dashboard-config/dashboards", dependencies=[Depends(verify_csrf)])
async def create_admin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    parsed = await form_lists(request)
    name = (parsed.get("name", [""])[0] or "").strip()
    if not name:
        return dashboard_redirect("/admin/dashboard-config", "Informe o nome do dashboard.", True)
    dashboard_item = Dashboard(
        name=name,
        description=clean_dashboard_text(parsed.get("description", [""])[0]),
        is_active=(parsed.get("is_active", [""])[0] == "on"),
        sort_order=dashboard_int(parsed.get("sort_order", ["100"])[0], 100, 0, 10000),
        categories=dashboard_categories_from_ids(db, parsed.get("category_ids", [])),
    )
    db.add(dashboard_item)
    db.commit()
    log_dashboard_action(db, user, "create_dashboard", dashboard_item, "success", "Dashboard criado.")
    return dashboard_redirect(dashboard_config_path(dashboard_item.id), f"Dashboard criado: {dashboard_item.name}.")


@router.post("/admin/dashboard-config/dashboards/{dashboard_id}", dependencies=[Depends(verify_csrf)])
async def update_admin_dashboard(dashboard_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dashboard_item = db.get(Dashboard, dashboard_id)
    if dashboard_item is None:
        return dashboard_redirect("/admin/dashboard-config", "Dashboard nao encontrado.", True)
    parsed = await form_lists(request)
    name = (parsed.get("name", [""])[0] or "").strip()
    if not name:
        return dashboard_redirect(dashboard_config_path(dashboard_item.id), "Informe o nome do dashboard.", True)
    dashboard_item.name = name
    dashboard_item.description = clean_dashboard_text(parsed.get("description", [""])[0])
    dashboard_item.is_active = parsed.get("is_active", [""])[0] == "on"
    dashboard_item.sort_order = dashboard_int(parsed.get("sort_order", ["100"])[0], 100, 0, 10000)
    dashboard_item.categories = dashboard_categories_from_ids(db, parsed.get("category_ids", []))
    db.commit()
    log_dashboard_action(db, user, "update_dashboard", dashboard_item, "success", "Dashboard atualizado.")
    return dashboard_redirect(dashboard_config_path(dashboard_item.id), f"Dashboard atualizado: {dashboard_item.name}.")


@router.post("/admin/dashboard-config/dashboards/{dashboard_id}/duplicate", dependencies=[Depends(verify_csrf)])
def duplicate_admin_dashboard(dashboard_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    original = (
        db.query(Dashboard)
        .options(selectinload(Dashboard.categories), selectinload(Dashboard.widgets))
        .filter(Dashboard.id == dashboard_id)
        .first()
    )
    if original is None:
        return dashboard_redirect("/admin/dashboard-config", "Dashboard nao encontrado.", True)
    duplicate = Dashboard(
        name=f"Cópia de {original.name}",
        description=original.description,
        is_active=original.is_active,
        sort_order=original.sort_order + 1,
        categories=list(original.categories),
    )
    db.add(duplicate)
    db.flush()
    for widget in original.widgets:
        copied = DashboardWidget(
            dashboard=duplicate,
            source_id=widget.source_id,
            source_id_b=widget.source_id_b,
            title=widget.title,
            widget_type=widget.widget_type,
            label_field=widget.label_field,
            value_field=widget.value_field,
            series_field=widget.series_field,
            time_field=widget.time_field,
            aggregation=widget.aggregation,
            top_n=widget.top_n,
            color=widget.color,
            icone=widget.icone,
            comparar_com=widget.comparar_com,
            meta_gauge=widget.meta_gauge,
            cor_secundaria=widget.cor_secundaria,
            sort_order=widget.sort_order,
            size=widget.size,
        )
        db.add(copied)
    db.commit()
    log_dashboard_action(db, user, "duplicate_dashboard", duplicate, "success", f"Dashboard duplicado de {original.name}.")
    return dashboard_redirect(dashboard_config_path(duplicate.id), f"Dashboard duplicado: {duplicate.name}.")


@router.get("/admin/dashboard-config/dashboards/{dashboard_id}/dependencies")
def admin_dashboard_dependencies(dashboard_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
    deps = dashboard_dependencies(db, dashboard_id)
    if deps["dashboard"] is None:
        return JSONResponse({"success": False, "error": "Dashboard nao encontrado."}, status_code=404)
    return JSONResponse({"success": True, **deps["payload"]})


@router.post("/admin/dashboard-config/dashboards/{dashboard_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_admin_dashboard(dashboard_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dashboard_item = db.get(Dashboard, dashboard_id)
    if dashboard_item is None:
        return dashboard_redirect("/admin/dashboard-config", "Dashboard nao encontrado.", True)
    active_count = db.query(Dashboard).filter(Dashboard.is_active.is_(True)).count()
    if dashboard_item.is_active and active_count <= 1:
        log_dashboard_action(db, user, "delete_dashboard", dashboard_item, "blocked", "Nao e possivel excluir o unico dashboard ativo.")
        return dashboard_redirect(dashboard_config_path(dashboard_item.id), "Nao e possivel excluir o unico dashboard ativo.", True)
    cascade = request.headers.get("X-Cascade-Confirm") == "true" or request.query_params.get("cascade") == "1"
    deps = dashboard_dependencies(db, dashboard_id)
    widgets = deps["widgets"]
    if widgets and not cascade:
        return dashboard_redirect(dashboard_config_path(dashboard_item.id), "Exclusao bloqueada: confirme a cascata dos widgets vinculados.", True)
    name = dashboard_item.name
    detail = f"Dashboard excluido: {name}. Widgets removidos em cascata: {len(widgets)}."
    if widgets:
        detail += " " + "; ".join(widget.title for widget in widgets)
    log_dashboard_action(db, user, "dashboard_delete", dashboard_item, "success", detail)
    db.delete(dashboard_item)
    db.commit()
    next_dashboard = db.query(Dashboard).order_by(Dashboard.sort_order.asc(), Dashboard.name.asc()).first()
    return dashboard_redirect(dashboard_config_path(next_dashboard.id if next_dashboard else None), f"Dashboard excluido: {name}.")


@router.post("/admin/dashboard-config/widgets", dependencies=[Depends(verify_csrf)])
async def create_admin_dashboard_widget(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        if dashboard_source_ajax_request(request):
            return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
        return user
    data = await form_data(request)
    dashboard_id = dashboard_int(data.get("dashboard_id"), 0, 0, 2147483647)
    current_dashboard = db.get(Dashboard, dashboard_id) if dashboard_id else None
    if current_dashboard is None:
        ajax_error = dashboard_ajax_error(request, "Selecione um dashboard valido.")
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, "Selecione um dashboard valido.", dashboard_id)
    source = db.get(DashboardSource, dashboard_int(data.get("source_id"), 0, 0, 2147483647))
    if source is None or not source.is_active or not dashboard_source_table_exists(source.source_table):
        ajax_error = dashboard_ajax_error(request, "Selecione uma Fonte Dashboard ativa.")
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, "Selecione uma Fonte Dashboard ativa.", dashboard_id)
    source_id_b = (data.get("source_id_b") or "").strip()
    source_b = db.get(DashboardSource, dashboard_int(source_id_b, 0, 0, 2147483647)) if source_id_b else None
    if source_id_b and (source_b is None or not source_b.is_active or not dashboard_source_table_exists(source_b.source_table)):
        ajax_error = dashboard_ajax_error(request, "Selecione uma Fonte B ativa.")
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, "Selecione uma Fonte B ativa.", dashboard_id)
    widget = DashboardWidget(source=source, dashboard=current_dashboard, title="")
    try:
        apply_dashboard_widget(widget, source, source_b, data)
    except ValueError as exc:
        ajax_error = dashboard_ajax_error(request, str(exc))
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, str(exc), dashboard_id)
    db.add(widget)
    db.commit()
    if dashboard_source_ajax_request(request):
        db.refresh(widget)
        return JSONResponse({"success": True, "widget": dashboard_widget_payload(widget)})
    return dashboard_redirect(dashboard_config_path(current_dashboard.id), f"Widget salvo: {widget.title}.")


@router.post("/admin/dashboard-config/widgets/{widget_id}", dependencies=[Depends(verify_csrf)])
async def update_admin_dashboard_widget(widget_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        if dashboard_source_ajax_request(request):
            return JSONResponse({"success": False, "error": "Acesso negado."}, status_code=403)
        return user
    widget = db.get(DashboardWidget, widget_id)
    if widget is None:
        ajax_error = dashboard_ajax_error(request, "Widget nao encontrado.", 404)
        if ajax_error:
            return ajax_error
        return dashboard_redirect("/admin/dashboard-config", "Widget nao encontrado.", True)
    data = await form_data(request)
    dashboard_id = dashboard_int(data.get("dashboard_id"), widget.dashboard_id or 0, 0, 2147483647)
    current_dashboard = db.get(Dashboard, dashboard_id) if dashboard_id else widget.dashboard
    if current_dashboard is None:
        ajax_error = dashboard_ajax_error(request, "Dashboard nao encontrado.", 404)
        if ajax_error:
            return ajax_error
        return dashboard_redirect("/admin/dashboard-config", "Dashboard nao encontrado.", True)
    source = db.get(DashboardSource, dashboard_int(data.get("source_id"), 0, 0, 2147483647))
    if source is None or not source.is_active or not dashboard_source_table_exists(source.source_table):
        ajax_error = dashboard_ajax_error(request, "Selecione uma Fonte Dashboard ativa.")
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, "Selecione uma Fonte Dashboard ativa.", current_dashboard.id)
    source_id_b = (data.get("source_id_b") or "").strip()
    source_b = db.get(DashboardSource, dashboard_int(source_id_b, 0, 0, 2147483647)) if source_id_b else None
    if source_id_b and (source_b is None or not source_b.is_active or not dashboard_source_table_exists(source_b.source_table)):
        ajax_error = dashboard_ajax_error(request, "Selecione uma Fonte B ativa.")
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, "Selecione uma Fonte B ativa.", current_dashboard.id)
    try:
        apply_dashboard_widget(widget, source, source_b, data)
        widget.dashboard = current_dashboard
    except ValueError as exc:
        ajax_error = dashboard_ajax_error(request, str(exc))
        if ajax_error:
            return ajax_error
        return dashboard_widget_context(request, db, str(exc), current_dashboard.id)
    db.commit()
    if dashboard_source_ajax_request(request):
        db.refresh(widget)
        return JSONResponse({"success": True, "widget": dashboard_widget_payload(widget)})
    return dashboard_redirect(dashboard_config_path(current_dashboard.id), f"Widget atualizado: {widget.title}.")


@router.post("/admin/dashboard-config/widgets/{widget_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_admin_dashboard_widget(widget_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    widget = db.get(DashboardWidget, widget_id)
    if widget is None:
        return dashboard_redirect("/admin/dashboard-config", "Widget nao encontrado.", True)
    dashboard_id = widget.dashboard_id
    title = widget.title
    log_dashboard_admin_action(db, user, "widget_delete", widget, "success", "Widget excluido.")
    db.delete(widget)
    db.commit()
    if dashboard_source_ajax_request(request):
        return JSONResponse({"success": True, "message": f"Widget excluido: {title}."})
    return dashboard_redirect(dashboard_config_path(dashboard_id), f"Widget excluido: {title}.")


@router.post("/dashboard/widgets/bulk-delete", dependencies=[Depends(verify_csrf_header)])
async def bulk_delete_dashboard_widgets(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"deleted": 0, "errors": ["Acesso negado."]}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"deleted": 0, "errors": ["Payload invalido."]}, status_code=400)
    raw_ids = payload.get("widget_ids", [])
    if not isinstance(raw_ids, list):
        return JSONResponse({"deleted": 0, "errors": ["widget_ids deve ser uma lista."]}, status_code=400)
    widget_ids = []
    for value in raw_ids:
        try:
            widget_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    widget_ids = list(dict.fromkeys(widget_ids))
    if not widget_ids:
        return JSONResponse({"deleted": 0, "errors": ["Nenhum widget informado."]}, status_code=400)
    widgets = db.query(DashboardWidget).filter(DashboardWidget.id.in_(widget_ids)).all()
    found_ids = {widget.id for widget in widgets}
    errors = [f"Widget {widget_id} nao encontrado." for widget_id in widget_ids if widget_id not in found_ids]
    details = []
    for widget in widgets:
        details.append(f"{widget.dashboard.name if widget.dashboard else '-'} - {widget.title}")
        db.delete(widget)
    deleted = len(widgets)
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action="bulk_delete_dashboard_widgets",
            status="success" if deleted else "blocked",
            message=f"Widgets excluidos em massa: {deleted}. " + "; ".join(details),
        )
    )
    db.commit()
    return JSONResponse({"deleted": deleted, "errors": errors})


@router.post("/admin/dashboard-config/widgets/bulk", dependencies=[Depends(verify_csrf)])
async def bulk_admin_dashboard_widgets(request: Request, db: Session = Depends(get_db)):
    user = require_dashboard(request, db)
    if isinstance(user, RedirectResponse):
        return user
    parsed = await form_lists(request)
    action = (parsed.get("bulk_action", [""])[0] or "").strip()
    confirmation = (parsed.get("bulk_confirmation", [""])[0] or "").strip()
    widget_ids = selected_widget_ids_from_form(parsed)
    dashboard_id = dashboard_int((parsed.get("dashboard_id", ["0"])[0] or "0"), 0, 0, 2147483647)

    if action not in DASHBOARD_BULK_ACTIONS:
        return dashboard_redirect(dashboard_config_path(dashboard_id), "Selecione uma acao em massa valida.", True)
    if action == "delete_all":
        widgets = (
            db.query(DashboardWidget)
            .join(DashboardWidget.source)
            .filter(DashboardWidget.dashboard_id == dashboard_id if dashboard_id else True)
            .order_by(DashboardWidget.title.asc())
            .all()
        )
    else:
        if not widget_ids:
            return dashboard_redirect(dashboard_config_path(dashboard_id), "Selecione ao menos um widget.", True)
        widgets = (
            db.query(DashboardWidget)
            .join(DashboardWidget.source)
            .filter(DashboardWidget.id.in_(widget_ids))
            .filter(DashboardWidget.dashboard_id == dashboard_id if dashboard_id else True)
            .order_by(DashboardWidget.title.asc())
            .all()
        )
    if not widgets:
        return dashboard_redirect(dashboard_config_path(dashboard_id), "Nenhum widget encontrado para a acao.", True)
    if action in DESTRUCTIVE_DASHBOARD_BULK_ACTIONS and confirmation != "CONFIRMAR":
        return dashboard_redirect(
            dashboard_config_path(dashboard_id),
            f"Acao bloqueada: confirme explicitamente os {len(widgets)} widgets afetados.",
            True,
        )

    success_count = 0
    for widget in list(widgets):
        if action == "activate":
            widget.source.is_active = True
            db.commit()
            log_dashboard_admin_action(db, user, "bulk_activate_dashboard_widget", widget, "success", "Fonte do widget ativada.")
            success_count += 1
        elif action == "deactivate":
            widget.source.is_active = False
            db.commit()
            log_dashboard_admin_action(db, user, "bulk_deactivate_dashboard_widget", widget, "success", "Fonte do widget desativada.")
            success_count += 1
        elif action in {"delete", "delete_all"}:
            log_dashboard_admin_action(db, user, f"bulk_{action}_dashboard_widget", widget, "success", "Widget excluido.")
            db.delete(widget)
            db.commit()
            success_count += 1

    return dashboard_redirect(dashboard_config_path(dashboard_id), f"Acao em massa concluida. Widgets afetados: {success_count}.")


@router.get("/home", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    cleanup_expired_temp_report_tables(db)
    message = request.query_params.get("message")
    error = request.query_params.get("error")
    visible_reports = visible_reports_query(db, user)
    total_reports = visible_reports.count()
    active_reports = visible_reports.filter(Report.is_active.is_(True)).count()
    reports_with_error = db.execute(
        text(
            """
            SELECT COUNT(*) AS total
            FROM report_executions re
            INNER JOIN (
                SELECT report_id,
                       MAX(executed_at) AS ultima
                FROM report_executions
                GROUP BY report_id
            ) ult
                ON re.report_id = ult.report_id
               AND re.executed_at = ult.ultima
            INNER JOIN reports r
                ON re.report_id = r.id
               AND r.is_active = 1
               AND r.destination_table IS NOT NULL
               AND r.destination_table != ''
            WHERE re.status = 'error'
            """
        )
    ).scalar() or 0
    last_import = (
        db.query(ConnectorRun)
        .order_by(ConnectorRun.started_at.desc())
        .first()
    )
    total_imported_rows = (
        db.query(func.coalesce(func.sum(ConnectorSyncTable.row_count), 0))
        .scalar()
        or 0
    )
    featured_reports = (
        visible_reports_query(db, user)
        .filter(Report.show_on_dashboard.is_(True))
        .order_by(Report.dashboard_order.asc(), Report.name.asc())
        .all()
    )
    last_executions = {}
    for report in featured_reports:
        last_executions[report.id] = db.execute(
            text(
                """
                SELECT status, executed_at,
                       row_count, error_message
                FROM report_executions
                WHERE report_id = :rid
                ORDER BY executed_at DESC
                LIMIT 1
                """
            ),
            {"rid": report.id},
        ).fetchone()

    grouped = defaultdict(list)
    for report in featured_reports:
        category = report.category or "Sem categoria"
        last_exec = last_executions.get(report.id)
        grouped[category].append(
            {
                "report": report,
                "last_exec": last_exec,
                "last_execution": last_exec,
                "preview": preview_destination_table(report.destination_table),
            }
        )
    grouped_featured = dict(
        sorted(
            grouped.items(),
            key=lambda item: (
                item[0] == "Sem categoria",
                item[0].lower(),
            ),
        )
    )
    grouped_featured_total = sum(len(items) for items in grouped_featured.values())
    return render(
        request,
        "home.html",
        {
            "active": "home",
            "total_reports": total_reports,
            "active_reports": active_reports,
            "last_import": last_import,
            "reports_with_error": reports_with_error,
            "total_imported_rows": total_imported_rows,
            "grouped_featured": grouped_featured,
            "grouped_featured_total": grouped_featured_total,
            "message": message,
            "error": error,
        },
        db,
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    dashboards = accessible_dashboards(db, user)
    if len(dashboards) == 1:
        suffix = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/dashboard/{dashboards[0].id}{suffix}", status_code=status.HTTP_303_SEE_OTHER)
    return render(
        request,
        "dashboard_list.html",
        {
            "active": "dashboard",
            "dashboards": dashboards,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.get("/dashboard/{dashboard_id}", response_class=HTMLResponse)
def dashboard_detail(dashboard_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    current_dashboard = (
        db.query(Dashboard)
        .options(selectinload(Dashboard.categories))
        .filter(Dashboard.id == dashboard_id)
        .first()
    )
    if not user_can_access_dashboard(user, current_dashboard):
        return RedirectResponse("/dashboard?error=Dashboard%20indisponivel%20ou%20sem%20permissao.", status_code=status.HTTP_303_SEE_OTHER)
    error = None
    try:
        date_from, date_to = dashboard_dates(
            request.query_params.get("data_inicio") or request.query_params.get("date_from"),
            request.query_params.get("data_fim") or request.query_params.get("date_to"),
        )
    except ValueError as exc:
        date_from, date_to = dashboard_default_dates()
        error = str(exc)
    widgets = (
        db.query(DashboardWidget)
        .join(DashboardWidget.source)
        .filter(DashboardSource.is_active.is_(True))
        .filter(DashboardWidget.dashboard_id == current_dashboard.id)
    )
    widgets = widgets.order_by(DashboardWidget.sort_order.asc(), DashboardWidget.title.asc()).all()
    return render(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "dashboard": current_dashboard,
            "widgets": [load_widget_data(widget, date_from, date_to) for widget in widgets],
            "filters": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
            "error": error,
        },
        db,
    )


@router.get("/dashboard/report-preview/{report_id}", response_class=HTMLResponse)
def dashboard_report_preview(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = db.get(Report, report_id)
    if not report or not report.show_on_dashboard:
        return HTMLResponse("<div class=\"empty-state\">Relatorio nao encontrado no dashboard.</div>", status_code=404)
    if not can_access_report(user, report):
        report_dashboard_access_denied(db, user, report, "denied_dashboard_preview")
        return HTMLResponse("<div class=\"empty-state\">Acesso negado para a categoria deste relatorio.</div>", status_code=403)
    item = build_report_card(report, db)
    return request.app.state.templates.TemplateResponse(
        "_dashboard_report_card_body.html",
        {
            "request": request,
            "current_user": user,
            "app_name": settings.app_name,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "item": item,
        },
    )


@router.get("/dashboard/report-detail/{report_id}/search")
def search_dashboard_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = db.get(Report, report_id)
    if not report or not report.show_on_dashboard:
        return JSONResponse({"error": "Relatorio nao encontrado no dashboard."}, status_code=404)
    if not can_access_report(user, report):
        report_dashboard_access_denied(db, user, report, "denied_dashboard_search")
        return JSONResponse({"error": "Acesso negado para a categoria deste relatorio."}, status_code=403)

    temp_table = request.query_params.get("temp_table", "").strip()
    metadata, error = (
        temp_report_metadata(temp_table, user.id, report.id, db)
        if temp_table
        else principal_report_metadata(report, db)
    )
    if error:
        return JSONResponse({"error": error}, status_code=404)

    page, page_size = report_detail_page_params(
        request.query_params.get("page"),
        request.query_params.get("page_size"),
    )
    search = (request.query_params.get("search") or "").strip()
    sort_col = (request.query_params.get("sort_col") or "").strip() or None
    sort_dir = (request.query_params.get("sort_dir") or "").strip().lower() or None
    date_column = None
    date_from = None
    date_to = None
    if not temp_table:
        requested_date_column = (request.query_params.get("campo_data") or "").strip()
        allowed_date_columns = {item["name"] for item in get_date_columns(metadata["destination_table"])}
        if requested_date_column in allowed_date_columns:
            date_column = requested_date_column
            date_from = (request.query_params.get("date_from") or "").strip() or None
            date_to = (request.query_params.get("date_to") or "").strip() or None

    try:
        total_count, rows = search_table_detail(
            metadata["destination_table"],
            metadata["columns"],
            page,
            page_size,
            search,
            sort_col,
            sort_dir,
            date_column,
            date_from,
            date_to,
        )
    except SQLAlchemyError as exc:
        return JSONResponse({"error": f"Nao foi possivel pesquisar a tabela local: {exc}"}, status_code=500)

    total_unfiltered = total_table_count(report.destination_table or metadata["destination_table"])
    period_total = total_table_count(temp_table) if temp_table else None
    return JSONResponse(
        {
            "report": {
                "id": report.id,
                "name": report.name,
                "description": report.description or "",
                "destination_table": report.destination_table,
                "date_column": metadata["date_column"],
            },
            "active_table": metadata["destination_table"],
            "is_temporary": bool(temp_table),
            "columns": metadata["columns"],
            "rows": rows,
            "date_columns": metadata.get("date_columns", get_date_columns(report.destination_table or "")),
            "default_column": metadata.get("date_column"),
            "date_inherited": metadata.get("date_inherited", False),
            "date_source_table": metadata.get("date_source_table", metadata["destination_table"]),
            "total_count": total_count,
            "count_meta": detail_count_payload(total_unfiltered, period_total),
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total_count + page_size - 1) // page_size),
            "sort_col": sort_col if sort_col in metadata["columns"] else None,
            "sort_dir": sort_dir if sort_col in metadata["columns"] and sort_dir in {"asc", "desc"} else None,
            "search": search,
            "update_info": report_freshness(db, report),
        }
    )


@router.get("/dashboard/report-detail/{report_id}")
def dashboard_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = db.get(Report, report_id)
    if not report or not report.show_on_dashboard:
        return JSONResponse({"error": "Relatorio nao encontrado no dashboard."}, status_code=404)
    if not can_access_report(user, report):
        report_dashboard_access_denied(db, user, report, "denied_dashboard_detail")
        return JSONResponse({"error": "Acesso negado para a categoria deste relatorio."}, status_code=403)
    temp_table = request.query_params.get("temp_table", "").strip()
    metadata, error = (
        temp_report_metadata(temp_table, user.id, report.id, db)
        if temp_table
        else principal_report_metadata(report, db)
    )
    if error:
        return JSONResponse({"error": error}, status_code=404)

    page, page_size = report_detail_page_params(
        request.query_params.get("page"),
        request.query_params.get("page_size"),
    )
    sort_col = (request.query_params.get("sort_col") or "").strip() or None
    sort_dir = (request.query_params.get("sort_dir") or "").strip().lower() or None
    started = time.perf_counter()
    filter_sql, params, warning = detail_empty_filter()
    if temp_table:
        warning = "Resultado filtrado temporario gerado para o periodo selecionado."
    try:
        total_count = count_report_detail_rows(metadata, filter_sql, params)
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = fetch_report_detail_rows(metadata, filter_sql, params, page, page_size, sort_col, sort_dir)
        date_range = report_detail_date_range(
            metadata.get("date_source_table", metadata["destination_table"]),
            metadata["date_column"],
        )
    except SQLAlchemyError as exc:
        return JSONResponse({"error": f"Nao foi possivel carregar a tabela local: {exc}"}, status_code=500)

    log_report_detail_access(db, report, user.id, "modal_open", total_count, started)
    return JSONResponse(
        {
            "report": {
                "id": report.id,
                "name": report.name,
                "description": report.description or "",
                "destination_table": report.destination_table,
                "date_column": metadata["date_column"],
            },
            "active_table": metadata["destination_table"],
            "is_temporary": bool(temp_table),
            "columns": metadata["columns"],
            "rows": rows,
            "date_columns": metadata.get("date_columns", get_date_columns(report.destination_table or "")),
            "default_column": metadata.get("date_column"),
            "date_inherited": metadata.get("date_inherited", False),
            "date_source_table": metadata.get("date_source_table", metadata["destination_table"]),
            "total_count": total_count,
            "count_meta": detail_count_payload(
                total_table_count(report.destination_table or metadata["destination_table"]),
                total_count if temp_table else None,
            ),
            "update_info": report_freshness(db, report),
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "sort_col": sort_col if sort_col in metadata["columns"] else None,
            "sort_dir": sort_dir if sort_col in metadata["columns"] and sort_dir in {"asc", "desc"} else None,
            "warning": warning,
            "date_range_available": date_range,
        }
    )


@router.post("/dashboard/report-detail/{report_id}/filter", dependencies=[Depends(verify_csrf_header)])
def filter_dashboard_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = db.get(Report, report_id)
    if not report or not report.show_on_dashboard:
        return JSONResponse({"error": "Relatorio nao encontrado no dashboard."}, status_code=404)
    if not can_access_report(user, report):
        report_dashboard_access_denied(db, user, report, "denied_dashboard_filter")
        return JSONResponse({"error": "Acesso negado para a categoria deste relatorio."}, status_code=403)
    plan = resolve_temporal_plan(
        db,
        report,
        campo_data=request.query_params.get("campo_data"),
    )
    if not plan:
        return JSONResponse(
            {
                "no_filter_available": True,
                "warning": "Filtro de período não disponível para este relatório.",
                "count_meta": detail_count_payload(total_table_count(report.destination_table or ""), None),
            }
        )
    try:
        date_from, date_to = parse_report_detail_dates(
            request.query_params.get("date_from"),
            request.query_params.get("date_to"),
        )
        started = time.perf_counter()
        table_name, row_count = create_temp_report_table(
            report,
            user.id,
            date_from,
            date_to,
            plan,
            db,
        )
    except (ValueError, SQLAlchemyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    log_report_detail_access(db, report, user.id, "modal_filter_execute", row_count, started)
    return JSONResponse(
        {
            "temp_table": table_name,
            "row_count": row_count,
            "count_meta": detail_count_payload(total_table_count(report.destination_table or ""), row_count),
            "warning": "Resultado filtrado temporario gerado para o periodo selecionado.",
        }
    )


@router.get("/dashboard/report-detail/{report_id}/export")
def export_dashboard_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_view_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = db.get(Report, report_id)
    if not report or not report.show_on_dashboard:
        return PlainTextResponse("Relatorio nao encontrado no dashboard.", status_code=404)
    if not can_access_report(user, report):
        report_dashboard_access_denied(db, user, report, "denied_dashboard_export")
        return PlainTextResponse("Acesso negado para a categoria deste relatorio.", status_code=403)
    temp_table = request.query_params.get("temp_table", "").strip()
    sort_col = (request.query_params.get("sort_col") or "").strip() or None
    sort_dir = (request.query_params.get("sort_dir") or "").strip().lower() or None
    metadata, error = (
        temp_report_metadata(temp_table, user.id, report.id, db)
        if temp_table
        else principal_report_metadata(report, db)
    )
    if error:
        return PlainTextResponse(error, status_code=404)
    try:
        date_from, date_to = parse_report_detail_dates(
            request.query_params.get("date_from"),
            request.query_params.get("date_to"),
        )
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)

    started = time.perf_counter()
    filter_sql, params, _ = detail_empty_filter()
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Relatorio")
    sheet.append(metadata["columns"])
    row_count = 0
    try:
        for row in stream_report_detail_rows(metadata, filter_sql, params, sort_col, sort_dir):
            sheet.append([row.get(column) for column in metadata["columns"]])
            row_count += 1
    except SQLAlchemyError as exc:
        return PlainTextResponse(f"Nao foi possivel exportar a tabela local: {exc}", status_code=500)

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    log_report_detail_access(db, report, user.id, "modal_export", row_count, started)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{report_detail_filename(report, date_from, date_to)}"'},
    )


@router.post("/dashboard/reports/{report_id}/run", dependencies=[Depends(verify_csrf)])
def run_dashboard_report(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_relatorios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = db.get(Report, report_id)
    if not report:
        return RedirectResponse(
            "/home?error=Relatorio%20nao%20encontrado.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not can_access_report(user, report):
        report_dashboard_access_denied(db, user, report, "denied_dashboard_run")
        return PlainTextResponse("Acesso negado para a categoria deste relatorio.", status_code=403)
    if not report.is_active:
        return RedirectResponse(
            f"/home?error={quote('Relatorio inativo.')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    execution, _ = run_select(db, report.sql_query, user.id, report.id, report.destination_table, report.modo_salvamento)
    if execution.status == "success":
        message = quote(f"Relatorio executado com sucesso: {report.name}.")
        return RedirectResponse(f"/home?message={message}", status_code=status.HTTP_303_SEE_OTHER)
    error = quote(execution.error_message or "Falha ao executar relatorio.")
    return RedirectResponse(f"/home?error={error}", status_code=status.HTTP_303_SEE_OTHER)
