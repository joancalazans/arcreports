from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, local_engine
from app.models import GlpiImportLog, Report, ReportExecution, TempReportResult
from app.routes.common import get_allowed_databases


settings = get_settings()
logger = logging.getLogger(__name__)
PREVIEW_LIMIT = 100
QUERY_TIMEOUT_SECONDS = 60
FORBIDDEN_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE)\b", re.I)
TABLE_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:`([^`]+)`|([A-Za-z0-9_]+))\s*\.\s*(?:`[^`]+`|[A-Za-z0-9_]+)",
    re.I,
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
TEMP_REPORT_TABLE_RE = re.compile(r"^tmp_report_\d+_\d+_\d+$")
ALLOWED_REPORT_DATABASES = {settings.local_db_name}
SAVE_MODES = {"substituir", "acrescentar"}
DESTINATION_SYSTEM_COLUMNS = ("__saved_at",)
DERIVED_CONTROL_COLUMNS = ("created_at", "solved_at", "closed_at", "reference_date", "target_end_at")
CONTROL_COLUMN_ALIASES = {
    "created_at": [
        "date_creation", "CreatedDate", "created_date",
        "data_criacao", "DataCriacao", "open_date", "Criado em",
    ],
    "solved_at": [
        "solvedate", "CompletedDate", "completed_date",
        "resolved_date", "ResolvedDate", "solve_date",
        "data_resolucao",
    ],
    "closed_at": [
        "closedate", "CloseDate", "close_date",
        "closed_date", "data_fechamento", "Concluído",
    ],
    "updated_at": ["Alterado em"],
    "start_date": ["Início"],
    "due_date": ["Data prevista"],
    "reference_date": [
        "date", "StartDate", "start_date",
        "data_inicio", "DataInicio",
    ],
    "target_end_at": [
        "time_to_resolve", "TargetEndDate", "target_end_date",
        "due_date", "DueDate", "data_prazo",
    ],
}
DERIVED_JOIN_KEY_CANDIDATES = ("ID", "id")
PRIMARY_DESTINATION_INDEXES = {
    "created_at": "idx_created_at",
    "solved_at": "idx_solved_at",
    "closed_at": "idx_closed_at",
    "reference_date": "idx_reference_date",
    "status_id": "idx_status_id",
    "type_id": "idx_type_id",
    "priority_id": "idx_priority_id",
}
AUTO_INDEX_THRESHOLD_KEY = "auto_index_threshold_rows"
DEFAULT_AUTO_INDEX_THRESHOLD_ROWS = 1000
AUTO_INDEX_CONTROL_COLUMNS = (
    "created_at",
    "solved_at",
    "closed_at",
    "reference_date",
    "target_end_at",
    "status_id",
    "type_id",
    "priority_id",
)
PREFIX_INDEX_DATA_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}
INTERNAL_DESTINATION_TABLES = {
    "admin_action_logs",
    "auth_logs",
    "dashboard_sources",
    "dashboard_widgets",
    "connector_configs",
    "glpi_import_logs",
    "connector_runs",
    "connector_sync_tables",
    "ldap_configs",
    "portal_groups",
    "report_executions",
    "reports",
    "report_categories",
    "user_report_categories",
    "portal_group_report_categories",
    "user_portal_groups",
    "users",
}
INITIAL_REPORT_CATEGORIES = (
    "Departamento 1",
    "Departamento 2",
    "Departamento 3",
    "Departamento 4",
    "Departamento 5",
)


def quote_identifier(identifier: str) -> str:
    if not IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Identificador invalido: {identifier}")
    return f"`{identifier}`"


def quote_column(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError("Coluna retornada com nome invalido.")
    return f"`{identifier.replace('`', '``')}`"


def is_temp_report_table(table_name: str) -> bool:
    return bool(TEMP_REPORT_TABLE_RE.match(table_name or ""))


def mask_comments_and_strings(sql_query: str) -> str:
    output: list[str] = []
    index = 0
    length = len(sql_query)
    state = "normal"
    quote_char = ""

    while index < length:
        char = sql_query[index]
        next_char = sql_query[index + 1] if index + 1 < length else ""

        if state == "normal":
            if char == "-" and next_char == "-":
                state = "line_comment"
                output.extend("  ")
                index += 2
                continue
            if char == "#":
                state = "line_comment"
                output.append(" ")
                index += 1
                continue
            if char == "/" and next_char == "*":
                state = "block_comment"
                output.extend("  ")
                index += 2
                continue
            if char in {"'", '"'}:
                state = "string"
                quote_char = char
                output.append(" ")
                index += 1
                continue
            output.append(char)
            index += 1
            continue

        if state == "line_comment":
            output.append("\n" if char == "\n" else " ")
            if char == "\n":
                state = "normal"
            index += 1
            continue

        if state == "block_comment":
            if char == "*" and next_char == "/":
                output.extend("  ")
                index += 2
                state = "normal"
                continue
            output.append("\n" if char == "\n" else " ")
            index += 1
            continue

        if state == "string":
            if char == "\\" and quote_char != "`":
                output.extend("  ")
                index += 2
                continue
            if char == quote_char:
                if index + 1 < length and sql_query[index + 1] == quote_char:
                    output.extend("  ")
                    index += 2
                    continue
                state = "normal"
            output.append("\n" if char == "\n" else " ")
            index += 1

    return "".join(output)


def strip_single_trailing_semicolon(sql_query: str, masked_sql: str) -> tuple[str, str]:
    sql = sql_query.rstrip()
    masked = masked_sql.rstrip()
    if masked.endswith(";"):
        return sql[:-1].rstrip(), masked[:-1].rstrip()
    return sql, masked


def validate_select(sql_query: str, allowed_databases: Iterable[str] | None = None) -> str:
    cleaned = sql_query.strip()
    if not cleaned:
        raise ValueError("SQL vazio.")

    masked = mask_comments_and_strings(cleaned)
    cleaned, masked = strip_single_trailing_semicolon(cleaned, masked)
    if ";" in masked:
        raise ValueError("SQL invalido: execute apenas uma consulta por vez.")
    if not re.match(r"^\s*(SELECT|WITH)\b", masked, re.I):
        raise ValueError("SQL invalido: a consulta principal deve iniciar com SELECT ou WITH.")
    if FORBIDDEN_SQL.search(masked):
        raise ValueError("SQL bloqueado: comandos destrutivos ou de escrita nao sao permitidos.")
    allowed_report_databases = set(allowed_databases or ALLOWED_REPORT_DATABASES)
    for match in TABLE_REFERENCE.finditer(masked):
        database_name = match.group(1) or match.group(2)
        if database_name not in allowed_report_databases:
            raise ValueError("SQL bloqueado: relatorios podem consultar somente o banco local configurado.")
    return cleaned


def build_preview_sql(cleaned_sql: str) -> str:
    return f"SELECT * FROM ({cleaned_sql}) AS report_preview LIMIT {PREVIEW_LIMIT}"


def extract_local_table_references(sql_query: str) -> list[str]:
    masked = mask_comments_and_strings(sql_query)
    references: list[str] = []
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+"
        r"(?:(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[A-Za-z0-9_]+))\s*\.\s*)?"
        r"(?:`(?P<table_bt>[^`]+)`|(?P<table>[A-Za-z0-9_]+))",
        re.I,
    )
    for match in pattern.finditer(masked):
        schema = match.group("schema_bt") or match.group("schema")
        table_name = match.group("table_bt") or match.group("table")
        if schema and schema != settings.local_db_name:
            continue
        if table_name and table_name not in references:
            references.append(table_name)
    return references


def primary_source_report_for_sql(db: Session, sql_query: str) -> Report | None:
    references = extract_local_table_references(sql_query)
    if not references:
        return None
    return (
        db.query(Report)
        .filter(Report.destination_table.in_(references), Report.is_primary.is_(True))
        .order_by(Report.id.asc())
        .first()
    )


def control_column_alias_names() -> list[str]:
    return [
        alias
        for aliases in CONTROL_COLUMN_ALIASES.values()
        for alias in aliases
    ]


def control_column_alias_pairs(columns: Iterable[str]) -> dict[str, str]:
    available = set(columns)
    pairs: dict[str, str] = {}
    for control_column, aliases in CONTROL_COLUMN_ALIASES.items():
        if control_column in available:
            continue
        alias = next((candidate for candidate in aliases if candidate in available), None)
        if alias:
            pairs[control_column] = alias
    return pairs


def available_control_columns_for_table(db: Session, source_table: str) -> set[str]:
    if not source_table:
        return set()
    statement = text(
        "SELECT column_name "
        "FROM information_schema.columns "
        "WHERE table_name = :tabela_fonte "
        "AND table_schema = :database "
        "AND column_name IN :control_columns"
    ).bindparams(bindparam("control_columns", expanding=True))
    try:
        rows = db.execute(
            statement,
            {
                "tabela_fonte": source_table,
                "database": settings.local_db_name,
                "control_columns": tuple([*DERIVED_CONTROL_COLUMNS, *control_column_alias_names()]),
            },
        )
    except SQLAlchemyError:
        logger.exception("Falha ao consultar colunas de controle da tabela fonte %s", source_table)
        return set()
    columns = {row[0] for row in rows}
    return columns | {
        control_column
        for control_column, alias in control_column_alias_pairs(columns).items()
        if alias
    }


def available_columns_for_table(db: Session, source_table: str) -> set[str]:
    if not source_table:
        return set()
    try:
        rows = db.execute(
            text(
                "SELECT column_name "
                "FROM information_schema.columns "
                "WHERE table_name = :tabela_fonte "
                "AND table_schema = :database"
            ),
            {"tabela_fonte": source_table, "database": settings.local_db_name},
        )
    except SQLAlchemyError:
        logger.exception("Falha ao consultar colunas da tabela fonte %s", source_table)
        return set()
    return {row[0] for row in rows}


def _dynamic_filter_parts(
    filters: dict[str, list[str] | str],
    allowed_columns: set[str],
) -> tuple[list[str], dict[str, object]]:
    """Monta clausulas e parametros sem interpolar valores fornecidos pelo usuario."""
    clauses: list[str] = []
    params: dict[str, object] = {}
    for filter_index, (column_name, raw_value) in enumerate(filters.items()):
        if column_name not in allowed_columns:
            raise ValueError(f"Coluna de filtro invalida: {column_name}")
        safe_column = f"dynamic_filter.{quote_column(column_name)}"
        if isinstance(raw_value, list):
            values = [str(value) for value in raw_value]
            if not values:
                continue
            placeholders = []
            for value_index, value in enumerate(values):
                parameter = f"dynamic_{filter_index}_{value_index}"
                placeholders.append(f":{parameter}")
                params[parameter] = value
            clauses.append(f"{safe_column} IN ({', '.join(placeholders)})")
            continue
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if not value:
                continue
            parameter = f"dynamic_{filter_index}"
            clauses.append(f"{safe_column} LIKE :{parameter}")
            params[parameter] = f"%{value}%"
            continue
        raise ValueError(f"Valor de filtro invalido para a coluna: {column_name}")
    return clauses, params


def apply_dynamic_filters(
    sql: str,
    filters: dict[str, list[str] | str],
    table_name: str,
    db: Session,
) -> str:
    """
    Injeta filtros dinamicos parametrizados no SQL.

    Os nomes de coluna sao validados contra a tabela destino. Os valores ficam em
    parametros nomeados, retornados por ``dynamic_filter_params`` para execucao.
    """
    allowed_columns = available_columns_for_table(db, table_name)
    clauses, _ = _dynamic_filter_parts(filters, allowed_columns)
    if not clauses:
        return sql
    return f"SELECT * FROM ({sql}) AS dynamic_filter WHERE {' AND '.join(clauses)}"


def dynamic_filter_params(
    filters: dict[str, list[str] | str],
    table_name: str,
    db: Session,
) -> dict[str, object]:
    """Retorna os parametros correspondentes a ``apply_dynamic_filters``."""
    allowed_columns = available_columns_for_table(db, table_name)
    _, params = _dynamic_filter_parts(filters, allowed_columns)
    return params


def detect_derived_join_key(result_columns: Iterable[str], source_columns: Iterable[str]) -> str | None:
    result_column_set = set(result_columns)
    source_column_set = set(source_columns)
    for candidate in DERIVED_JOIN_KEY_CANDIDATES:
        if candidate in result_column_set and candidate in source_column_set:
            return candidate
    return None


def wrap_derived_control_query(
    cleaned_sql: str,
    source_table: str,
    join_key: str,
    columns: Iterable[str],
) -> str:
    control_sql = ", ".join(
        f"fonte.{quote_column(column)} AS {quote_column(column)}"
        for column in columns
    )
    return (
        "SELECT resultado_base.*, "
        f"{control_sql} "
        f"FROM ({cleaned_sql}) AS resultado_base "
        f"JOIN {quote_identifier(settings.local_db_name)}.{quote_identifier(source_table)} AS fonte "
        f"ON fonte.{quote_column(join_key)} = resultado_base.{quote_column(join_key)}"
    )


def inject_derived_control_columns(
    db: Session,
    cleaned_sql: str,
    report_id: int | None,
    result_columns: Iterable[str],
) -> str:
    if not report_id:
        return cleaned_sql
    report = db.get(Report, report_id)
    if not report or report.is_primary:
        return cleaned_sql
    current_columns = set(result_columns)
    missing = [column for column in DERIVED_CONTROL_COLUMNS if column not in current_columns]
    if not missing:
        return cleaned_sql
    source_report = primary_source_report_for_sql(db, cleaned_sql)
    if not source_report or not source_report.destination_table:
        logger.warning(
            "Relatorio derivado id=%s sem fonte primaria identificavel para injetar colunas de controle: %s",
            report_id,
            ", ".join(missing),
        )
        return cleaned_sql
    existing_control_columns = available_control_columns_for_table(db, source_report.destination_table)
    missing = [column for column in missing if column in existing_control_columns]
    if not missing:
        return cleaned_sql
    source_columns = available_columns_for_table(db, source_report.destination_table)
    join_key = detect_derived_join_key(result_columns, source_columns)
    if not join_key:
        logger.warning(
            "Relatorio derivado id=%s sem chave de JOIN detectavel para injetar colunas de controle: %s",
            report_id,
            ", ".join(missing),
        )
        return cleaned_sql
    injected_sql = wrap_derived_control_query(
        cleaned_sql,
        source_report.destination_table,
        join_key,
        missing,
    )
    logger.warning(
        "Relatorio derivado id=%s recebeu colunas de controle ausentes no SELECT: %s",
        report_id,
        ", ".join(missing),
    )
    return validate_select(injected_sql)


def apply_query_timeout(connection, seconds: int | None = None) -> None:
    timeout = max(1, min(int(seconds or QUERY_TIMEOUT_SECONDS), 600))
    connection.execute(text(f"SET SESSION max_statement_time={timeout}"))


def classify_sql_error(exc: Exception) -> str:
    message = str(exc)
    lower = message.lower()
    if "max_statement_time exceeded" in lower or "query execution was interrupted" in lower:
        return (
            "Tempo limite excedido no banco local: a consulta demorou mais de "
            f"{QUERY_TIMEOUT_SECONDS} segundos. Revise filtros, joins, subqueries ou salve uma versao otimizada."
        )
    if "doesn't exist" in lower or "unknown table" in lower or "unknown column" in lower:
        return f"Tabela ou coluna inexistente: {message}"
    if "access denied" in lower or "permission" in lower or "command denied" in lower:
        return f"Permissao negada no banco local: {message}"
    if "can't connect" in lower or "connection" in lower or "lost connection" in lower:
        return f"Falha de conexao com o banco local: {message}"
    if isinstance(exc, DBAPIError):
        return f"Erro SQL retornado pelo banco local: {message}"
    return f"Erro SQL: {message}"


def ensure_reporting_schema() -> None:
    from app.glpi_import import ensure_report_indexes

    statements = [
        "ALTER TABLE `reports` MODIFY COLUMN `description` LONGTEXT NULL",
        "ALTER TABLE `reports` MODIFY COLUMN `sql_query` LONGTEXT NOT NULL",
        "ALTER TABLE `report_executions` MODIFY COLUMN `sql_query` LONGTEXT NOT NULL",
        "ALTER TABLE `report_executions` MODIFY COLUMN `error_message` LONGTEXT NULL",
        "ALTER TABLE `report_executions` MODIFY COLUMN `result_json` LONGTEXT NULL",
        "ALTER TABLE `users` MODIFY COLUMN `password_hash` VARCHAR(255) NULL",
    ]
    with local_engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        ensure_column(connection, "reports", "destination_table", "`destination_table` VARCHAR(120) NULL")
        ensure_column(connection, "reports", "modo_salvamento", "`modo_salvamento` VARCHAR(20) NOT NULL DEFAULT 'substituir'")
        ensure_column(connection, "reports", "campo_sql_periodo", "`campo_sql_periodo` VARCHAR(255) NULL")
        ensure_column(connection, "reports", "modo_filtro_periodo", "`modo_filtro_periodo` VARCHAR(30) NOT NULL DEFAULT 'filtro_externo'")
        ensure_column(connection, "reports", "clausula_periodo_original", "`clausula_periodo_original` TEXT NULL")
        ensure_column(connection, "reports", "query_timeout_seconds", "`query_timeout_seconds` INT NOT NULL DEFAULT 60")
        ensure_column(connection, "reports", "filter_timeout_seconds", "`filter_timeout_seconds` INT NULL DEFAULT 30")
        ensure_column(connection, "reports", "report_type", "`report_type` VARCHAR(30) NOT NULL DEFAULT 'manual'")
        ensure_column(connection, "reports", "builder_config", "`builder_config` LONGTEXT NULL")
        ensure_column(connection, "reports", "show_on_dashboard", "`show_on_dashboard` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "reports", "dashboard_order", "`dashboard_order` INT NOT NULL DEFAULT 100")
        ensure_column(connection, "reports", "category", "`category` VARCHAR(120) NULL")
        ensure_column(connection, "reports", "card_color", "`card_color` VARCHAR(30) NULL")
        ensure_column(connection, "reports", "icon", "`icon` VARCHAR(80) NULL")
        ensure_column(connection, "reports", "executive_highlight", "`executive_highlight` TINYINT(1) NOT NULL DEFAULT 0")
        ensure_column(connection, "report_executions", "destination_table", "`destination_table` VARCHAR(120) NULL")
        ensure_column(connection, "report_executions", "finished_at", "`finished_at` DATETIME NULL")
        ensure_column(connection, "users", "auth_source", "`auth_source` VARCHAR(20) NOT NULL DEFAULT 'local'")
        ensure_column(connection, "users", "is_active", "`is_active` TINYINT(1) NOT NULL DEFAULT 1")
        ensure_column(connection, "users", "last_login_at", "`last_login_at` DATETIME NULL")
        ensure_admin_action_logs(connection)
        ensure_report_category_schema(connection)
        seed_auto_index_config(connection)
        seed_report_categories(connection)
        glpi_index_database = "glpi_local" if connection.execute(
            text("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = 'glpi_local'")
        ).scalar_one() else settings.local_db_name
        indexed_tables = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :database_name "
                    "AND TABLE_TYPE = 'BASE TABLE' "
                    "AND TABLE_NAME IN ('glpi_logs', 'glpi_tickets', 'glpi_tickets_users', 'glpi_groups_tickets')"
                ),
                {"database_name": glpi_index_database},
            )
        ]
        table_columns = {}
        for table_name in indexed_tables:
            rows = connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
                ),
                {"database_name": glpi_index_database, "table_name": table_name},
            )
            table_columns[table_name] = [row[0] for row in rows]
    for table_name, columns in table_columns.items():
        ensure_report_indexes(glpi_index_database, table_name, columns)


def ensure_admin_action_logs(connection) -> None:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS `admin_action_logs` ("
            "`id` INT NOT NULL AUTO_INCREMENT,"
            "`user_id` INT NULL,"
            "`username` VARCHAR(80) NULL,"
            "`action` VARCHAR(80) NOT NULL,"
            "`report_id` INT NULL,"
            "`report_name` VARCHAR(160) NULL,"
            "`table_name` VARCHAR(120) NULL,"
            "`status` VARCHAR(30) NOT NULL,"
            "`message` TEXT NULL,"
            "`created_at` DATETIME NOT NULL,"
            "PRIMARY KEY (`id`),"
            "INDEX `idx_admin_action_logs_report_id` (`report_id`),"
            "INDEX `idx_admin_action_logs_table_name` (`table_name`)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    )
    ensure_column(connection, "admin_action_logs", "report_id", "`report_id` INT NULL")
    ensure_column(connection, "admin_action_logs", "report_name", "`report_name` VARCHAR(160) NULL")
    ensure_column(connection, "admin_action_logs", "ip_address", "`ip_address` VARCHAR(45) NULL")


def ensure_report_category_schema(connection) -> None:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS `report_categories` ("
            "`id` INT NOT NULL AUTO_INCREMENT,"
            "`name` VARCHAR(120) NOT NULL,"
            "`is_active` TINYINT(1) NOT NULL DEFAULT 1,"
            "`created_at` DATETIME NOT NULL,"
            "`updated_at` DATETIME NOT NULL,"
            "PRIMARY KEY (`id`),"
            "UNIQUE KEY `uq_report_categories_name` (`name`)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS `user_report_categories` ("
            "`user_id` INT NOT NULL,"
            "`category_id` INT NOT NULL,"
            "PRIMARY KEY (`user_id`, `category_id`),"
            "INDEX `idx_user_report_categories_category` (`category_id`)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS `portal_group_report_categories` ("
            "`group_id` INT NOT NULL,"
            "`category_id` INT NOT NULL,"
            "PRIMARY KEY (`group_id`, `category_id`),"
            "INDEX `idx_portal_group_report_categories_category` (`category_id`)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    )


def seed_report_categories(connection) -> None:
    already_seeded = connection.execute(
        text(
            "SELECT COUNT(*) FROM system_config "
            "WHERE `key` = 'categories_seeded' "
            "AND `value` = 'true'"
        )
    ).scalar_one()
    if already_seeded:
        return
    now = datetime.utcnow()
    for category_name in INITIAL_REPORT_CATEGORIES:
        exists = connection.execute(
            text("SELECT COUNT(*) FROM `report_categories` WHERE `name` = :name"),
            {"name": category_name},
        ).scalar_one()
        if not exists:
            connection.execute(
                text(
                    "INSERT INTO `report_categories` (`name`, `is_active`, `created_at`, `updated_at`) "
                    "VALUES (:name, 1, :created_at, :updated_at)"
                ),
                {"name": category_name, "created_at": now, "updated_at": now},
            )
    default_category_exists = connection.execute(
        text("SELECT COUNT(*) FROM `report_categories` WHERE `name` = 'Departamento 1'")
    ).scalar_one()
    if default_category_exists:
        connection.execute(
            text(
                "UPDATE `reports` SET `category` = 'Departamento 1' "
                "WHERE `category` IS NULL OR `category` = ''"
            )
        )
    connection.execute(
        text(
            "INSERT INTO system_config "
            "(`key`, `value`) "
            "VALUES ('categories_seeded', 'true') "
            "ON DUPLICATE KEY UPDATE "
            "`value` = 'true'"
        )
    )


def seed_auto_index_config(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO system_config "
            "(`key`, `value`) "
            "VALUES (:key, :value) "
            "ON DUPLICATE KEY UPDATE "
            "`value` = `value`"
        ),
        {
            "key": AUTO_INDEX_THRESHOLD_KEY,
            "value": str(DEFAULT_AUTO_INDEX_THRESHOLD_ROWS),
        },
    )


def ensure_column(connection, table_name: str, column_name: str, definition: str) -> None:
    result = connection.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name AND COLUMN_NAME = :column_name"
        ),
        {"database_name": settings.local_db_name, "table_name": table_name, "column_name": column_name},
    )
    if result.scalar_one() == 0:
        connection.execute(text(f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {definition}"))


def cleanup_expired_temp_report_tables(db: Session) -> int:
    """Remove resultados expirados usando exclusivamente o catálogo UTC."""
    expired = (
        db.query(TempReportResult)
        .filter(TempReportResult.expires_at <= datetime.utcnow())
        .all()
    )
    removed = 0
    safe_database = quote_identifier(settings.local_db_name)
    for entry in expired:
        if not is_temp_report_table(entry.temp_table):
            logger.warning("Catalogo temporario contem nome invalido: %s", entry.temp_table)
            continue
        try:
            safe_table = quote_identifier(entry.temp_table)
            with local_engine.begin() as connection:
                connection.execute(text(f"DROP TABLE IF EXISTS {safe_database}.{safe_table}"))
            db.delete(entry)
            db.commit()
            removed += 1
        except Exception as exc:
            db.rollback()
            logger.warning("cleanup temp table %s: %s", entry.temp_table, exc)
    return removed


def cleanup_orphan_temp_report_tables(db: Session) -> int:
    """Remove no startup tabelas tmp_report_* anteriores ao catálogo."""
    catalog_tables = {
        value
        for (value,) in db.query(TempReportResult.temp_table).all()
    }
    safe_database = quote_identifier(settings.local_db_name)
    removed = 0
    with local_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME LIKE 'tmp_report_%'"
            ),
            {"database_name": settings.local_db_name},
        )
        for (table_name,) in rows:
            if table_name in catalog_tables or not is_temp_report_table(table_name):
                continue
            connection.execute(
                text(
                    f"DROP TABLE IF EXISTS {safe_database}.{quote_identifier(table_name)}"
                )
            )
            removed += 1
    return removed


def ensure_destination_table(
    destination_table: str,
    columns: Iterable[str],
    apply_control_aliases: bool = False,
) -> None:
    safe_table = quote_identifier(destination_table)
    result_columns = destination_result_columns(columns)
    if not result_columns:
        raise ValueError("Tabela destino exige uma consulta com colunas retornadas.")

    with local_engine.begin() as connection:
        portal_columns = [column for column in DESTINATION_SYSTEM_COLUMNS if column not in result_columns]
        column_defs = [
            *[f"{quote_column(column)} DATETIME NULL" for column in portal_columns],
            *[f"{quote_column(column)} LONGTEXT NULL" for column in result_columns],
        ]
        connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {quote_identifier(settings.local_db_name)}.{safe_table} "
                f"({', '.join(column_defs)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        existing = connection.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
            ),
            {"database_name": settings.local_db_name, "table_name": destination_table},
        )
        existing_columns = {row[0] for row in existing}
        for column in [*portal_columns, *result_columns]:
            if column not in existing_columns:
                column_type = "DATETIME NULL" if column in portal_columns else "LONGTEXT NULL"
                connection.execute(
                    text(
                        f"ALTER TABLE {quote_identifier(settings.local_db_name)}.{safe_table} "
                        f"ADD COLUMN {quote_column(column)} {column_type}"
                    )
                )
        if apply_control_aliases:
            ensure_destination_control_alias_columns(connection, destination_table, [*existing_columns, *result_columns])


def recreate_destination_table(db: Session, report_id: int | None, destination_table: str | None, columns: Iterable[str]) -> None:
    table_name = assert_report_destination_can_be_cleared(db, report_id, destination_table)
    report = (
        db.query(Report)
        .filter(Report.id == report_id, Report.destination_table == table_name)
        .first()
    )
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    result_columns = destination_result_columns(columns)
    if not result_columns:
        raise ValueError("Tabela destino exige uma consulta com colunas retornadas.")

    portal_columns = [column for column in DESTINATION_SYSTEM_COLUMNS if column not in result_columns]
    column_defs = [
        *[f"{quote_column(column)} DATETIME NULL" for column in portal_columns],
        *[f"{quote_column(column)} LONGTEXT NULL" for column in result_columns],
    ]

    with local_engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {safe_database}.{safe_table}"))
        connection.execute(
            text(
                f"CREATE TABLE {safe_database}.{safe_table} "
                f"({', '.join(column_defs)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        if report and report.is_primary:
            ensure_destination_control_alias_columns(connection, table_name, result_columns)
            ensure_primary_destination_indexes(connection, table_name)


def ensure_destination_control_alias_columns(connection, table_name: str, columns: Iterable[str]) -> list[str]:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    current_columns = set(columns)
    added_columns = []
    for control_column in DERIVED_CONTROL_COLUMNS:
        if control_column in current_columns:
            continue
        alias = control_column_alias_pairs(current_columns).get(control_column)
        if not alias:
            continue
        exists = connection.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :database_name "
                "AND TABLE_NAME = :table_name "
                "AND COLUMN_NAME = :column_name"
            ),
            {
                "database_name": settings.local_db_name,
                "table_name": table_name,
                "column_name": control_column,
            },
        ).scalar_one()
        if exists:
            current_columns.add(control_column)
            continue
        connection.execute(
            text(
                f"ALTER TABLE {safe_database}.{safe_table} "
                f"ADD COLUMN {quote_column(control_column)} LONGTEXT NULL"
            )
        )
        current_columns.add(control_column)
        added_columns.append(control_column)
    return added_columns


def normalized_control_alias_expression(alias: str) -> str:
    alias_sql = quote_column(alias)
    return (
        f"COALESCE("
        f"DATE_FORMAT(STR_TO_DATE(NULLIF({alias_sql}, ''), '%d/%m/%Y %H:%i:%s'), '%Y-%m-%d %H:%i:%s'), "
        f"DATE_FORMAT(STR_TO_DATE(NULLIF({alias_sql}, ''), '%Y-%m-%d %H:%i:%s'), '%Y-%m-%d %H:%i:%s'), "
        f"NULLIF({alias_sql}, '')"
        f")"
    )


def backfill_destination_control_aliases(
    destination_table: str,
    columns: Iterable[str],
    apply_control_aliases: bool = False,
) -> None:
    if not apply_control_aliases:
        return
    pairs = control_column_alias_pairs(columns)
    if not pairs:
        return
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(destination_table)
    with local_engine.begin() as connection:
        existing_columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
                ),
                {"database_name": settings.local_db_name, "table_name": destination_table},
            )
        }
        for control_column, alias in pairs.items():
            if control_column not in existing_columns or alias not in existing_columns:
                continue
            connection.execute(
                text(
                    f"UPDATE {safe_database}.{safe_table} "
                    f"SET {quote_column(control_column)} = {normalized_control_alias_expression(alias)} "
                    f"WHERE {quote_column(control_column)} IS NULL"
                )
            )


def ensure_primary_destination_indexes(connection, table_name: str) -> list[str]:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    target_columns = tuple(PRIMARY_DESTINATION_INDEXES.keys())
    columns = {
        row["COLUMN_NAME"]: row["DATA_TYPE"]
        for row in connection.execute(
            text(
                "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :database_name "
                "AND TABLE_NAME = :table_name "
                "AND COLUMN_NAME IN :column_names"
            ).bindparams(bindparam("column_names", expanding=True)),
            {
                "database_name": settings.local_db_name,
                "table_name": table_name,
                "column_names": target_columns,
            },
        ).mappings()
    }
    if not columns:
        return []

    existing_indexes = {
        row[0]
        for row in connection.execute(
            text(
                "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
            ),
            {"database_name": settings.local_db_name, "table_name": table_name},
        )
    }
    created_indexes = []
    for column_name, index_name in PRIMARY_DESTINATION_INDEXES.items():
        data_type = (columns.get(column_name) or "").lower()
        if not data_type or index_name in existing_indexes:
            continue
        column_sql = quote_column(column_name)
        if data_type in PREFIX_INDEX_DATA_TYPES:
            column_sql = f"{column_sql}(191)"
        connection.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {quote_identifier(index_name)} "
                f"ON {safe_database}.{safe_table} ({column_sql})"
            )
        )
        existing_indexes.add(index_name)
        created_indexes.append(index_name)
    return created_indexes


def auto_index_threshold_rows(connection) -> int:
    raw_value = connection.execute(
        text("SELECT `value` FROM system_config WHERE `key` = :key"),
        {"key": AUTO_INDEX_THRESHOLD_KEY},
    ).scalar()
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return DEFAULT_AUTO_INDEX_THRESHOLD_ROWS


def table_row_estimate(connection, table_name: str) -> int:
    value = connection.execute(
        text(
            "SELECT COALESCE(TABLE_ROWS, 0) "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = :database_name "
            "AND TABLE_NAME = :table_name"
        ),
        {"database_name": settings.local_db_name, "table_name": table_name},
    ).scalar()
    return int(value or 0)


def report_table_control_columns(connection, table_name: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT COLUMN_NAME, DATA_TYPE "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :database_name "
            "AND TABLE_NAME = :table_name "
            "AND COLUMN_NAME IN :column_names"
        ).bindparams(bindparam("column_names", expanding=True)),
        {
            "database_name": settings.local_db_name,
            "table_name": table_name,
            "column_names": AUTO_INDEX_CONTROL_COLUMNS,
        },
    ).mappings()
    return {row["COLUMN_NAME"]: (row["DATA_TYPE"] or "").lower() for row in rows}


def indexed_columns_for_table(connection, table_name: str) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT DISTINCT COLUMN_NAME "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = :database_name "
            "AND TABLE_NAME = :table_name"
        ),
        {"database_name": settings.local_db_name, "table_name": table_name},
    )
    return {row[0] for row in rows if row[0]}


def log_auto_index_creation(connection, table_name: str, created_indexes: list[str], row_count: int) -> None:
    if not created_indexes:
        return
    connection.execute(
        text(
            "INSERT INTO admin_action_logs "
            "(`user_id`, `username`, `action`, `table_name`, `status`, `message`, `created_at`) "
            "VALUES (NULL, 'scheduler', 'auto_index_create', :table_name, 'success', :message, :created_at)"
        ),
        {
            "table_name": table_name,
            "message": (
                f"Indices automaticos criados: {', '.join(created_indexes)}; "
                f"linhas_estimadas={row_count}"
            ),
            "created_at": datetime.utcnow(),
        },
    )


def ensure_indexes_if_needed(table_name: str) -> list[str]:
    if not table_name or not IDENTIFIER_RE.match(table_name):
        raise ValueError("Tabela destino invalida para auto-indexacao.")
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(table_name)
    created_indexes: list[str] = []
    with local_engine.begin() as connection:
        threshold = auto_index_threshold_rows(connection)
        row_count = table_row_estimate(connection, table_name)
        if row_count <= threshold:
            return []

        columns = report_table_control_columns(connection, table_name)
        if not columns:
            return []

        indexed_columns = indexed_columns_for_table(connection, table_name)
        for column_name in AUTO_INDEX_CONTROL_COLUMNS:
            data_type = columns.get(column_name)
            if not data_type or column_name in indexed_columns:
                continue
            column_sql = quote_column(column_name)
            if data_type in PREFIX_INDEX_DATA_TYPES:
                column_sql = f"{column_sql}(191)"
            index_name = f"idx_{column_name}"
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {quote_identifier(index_name)} "
                    f"ON {safe_database}.{safe_table} ({column_sql})"
                )
            )
            indexed_columns.add(column_name)
            created_indexes.append(index_name)

        log_auto_index_creation(connection, table_name, created_indexes, row_count)
    return created_indexes


def local_report_table_names(connection) -> list[str]:
    rows = connection.execute(
        text(
            "SELECT DISTINCT destination_table "
            "FROM reports "
            "WHERE destination_table IS NOT NULL "
            "AND destination_table <> '' "
            "ORDER BY destination_table"
        )
    )
    return [row[0] for row in rows if row[0] and IDENTIFIER_RE.match(row[0])]


def run_auto_index_maintenance() -> dict[str, object]:
    inspected = 0
    changed: dict[str, list[str]] = {}
    with local_engine.connect() as connection:
        table_names = local_report_table_names(connection)
    for table_name in table_names:
        inspected += 1
        created = ensure_indexes_if_needed(table_name)
        if created:
            changed[table_name] = created
    return {"inspected": inspected, "indexed_tables": changed}


def auto_index_health_stats(db: Session) -> dict[str, int]:
    threshold_row = db.execute(
        text("SELECT `value` FROM system_config WHERE `key` = :key"),
        {"key": AUTO_INDEX_THRESHOLD_KEY},
    ).scalar()
    try:
        threshold = max(0, int(threshold_row))
    except (TypeError, ValueError):
        threshold = DEFAULT_AUTO_INDEX_THRESHOLD_ROWS

    rows = db.execute(
        text(
            "SELECT t.TABLE_NAME AS table_name, COALESCE(t.TABLE_ROWS, 0) AS table_rows, "
            "COUNT(DISTINCT s.COLUMN_NAME) AS indexed_control_columns "
            "FROM information_schema.TABLES t "
            "JOIN reports r ON r.destination_table = t.TABLE_NAME "
            "LEFT JOIN information_schema.STATISTICS s "
            "ON s.TABLE_SCHEMA = t.TABLE_SCHEMA "
            "AND s.TABLE_NAME = t.TABLE_NAME "
            "AND s.COLUMN_NAME IN :column_names "
            "WHERE t.TABLE_SCHEMA = :database_name "
            "AND t.TABLE_TYPE = 'BASE TABLE' "
            "AND COALESCE(t.TABLE_ROWS, 0) > :threshold "
            "GROUP BY t.TABLE_NAME, t.TABLE_ROWS"
        ).bindparams(bindparam("column_names", expanding=True)),
        {
            "database_name": settings.local_db_name,
            "threshold": threshold,
            "column_names": AUTO_INDEX_CONTROL_COLUMNS,
        },
    ).mappings().all()
    return {
        "threshold": threshold,
        "large_tables": len(rows),
        "indexed_tables": sum(1 for row in rows if int(row["indexed_control_columns"] or 0) > 0),
    }


def destination_result_columns(columns: Iterable[str]) -> list[str]:
    return list(columns)


def normalize_save_mode(value: str | None) -> str:
    return value if value in SAVE_MODES else "substituir"


def assert_report_destination_can_be_cleared(db: Session, report_id: int | None, destination_table: str | None) -> str:
    from app.models import Report

    table_name = (destination_table or "").strip()
    if not report_id:
        raise ValueError("Limpeza bloqueada: tabela destino sem relatorio vinculado.")
    if not table_name or not IDENTIFIER_RE.match(table_name):
        raise ValueError("Limpeza bloqueada: tabela destino invalida.")
    if table_name.startswith("glpi_"):
        raise ValueError("Limpeza bloqueada: tabelas glpi_* nao podem ser limpas.")
    if table_name in INTERNAL_DESTINATION_TABLES:
        raise ValueError("Limpeza bloqueada: tabela interna do portal nao pode ser limpa.")
    linked = (
        db.query(Report)
        .filter(Report.id == report_id, Report.destination_table == table_name)
        .first()
    )
    if not linked:
        raise ValueError("Limpeza bloqueada: tabela destino nao esta vinculada ao relatorio informado.")
    return table_name


def clear_report_destination_table(db: Session, report_id: int | None, destination_table: str | None) -> None:
    table_name = assert_report_destination_can_be_cleared(db, report_id, destination_table)
    with local_engine.begin() as connection:
        connection.execute(
            text(
                f"TRUNCATE TABLE {quote_identifier(settings.local_db_name)}."
                f"{quote_identifier(table_name)}"
            )
        )


def save_destination_rows(destination_table: str, execution_id: int, rows: list[dict]) -> int:
    if not rows:
        return 0
    safe_table = quote_identifier(destination_table)
    result_columns = destination_result_columns(rows[0].keys())
    portal_columns = [column for column in DESTINATION_SYSTEM_COLUMNS if column not in result_columns]
    columns = [*portal_columns, *result_columns]
    column_sql = ", ".join(quote_column(column) for column in columns)
    bind_names = [f"p{index}" for index, _ in enumerate(columns)]
    value_sql = ", ".join(f":{name}" for name in bind_names)
    saved_at = datetime.utcnow()
    payload = []
    for row in rows:
        item = {}
        for index, column in enumerate(portal_columns):
            item[f"p{index}"] = saved_at if column == "__saved_at" else None
        for index, column in enumerate(result_columns, start=len(portal_columns)):
            item[f"p{index}"] = row.get(column)
        payload.append(item)

    with local_engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO {quote_identifier(settings.local_db_name)}.{safe_table} ({column_sql}) VALUES ({value_sql})"),
            payload,
        )
    return len(payload)


def analyze_destination_table(destination_table: str) -> None:
    safe_database = quote_identifier(settings.local_db_name)
    safe_table = quote_identifier(destination_table)
    with local_engine.begin() as connection:
        connection.execute(text(f"ANALYZE TABLE {safe_database}.{safe_table}"))


def save_full_result(
    db: Session,
    cleaned_sql: str,
    destination_table: str,
    execution_id: int,
    modo_salvamento: str = "acrescentar",
    report_id: int | None = None,
) -> int:
    saved = 0
    save_mode = normalize_save_mode(modo_salvamento)
    report = db.get(Report, report_id) if report_id else None
    timeout = report.query_timeout_seconds if report else None
    with local_engine.connect() as connection:
        apply_query_timeout(connection, timeout)
        metadata_result = connection.execute(
            text(f"SELECT * FROM ({cleaned_sql}) AS report_columns LIMIT 0")
        )
        result_columns = list(metadata_result.keys())
        if close_result := getattr(metadata_result, "close", None):
            close_result()
        injected_sql = inject_derived_control_columns(db, cleaned_sql, report_id, result_columns)
        result = connection.execution_options(stream_results=True).execute(text(injected_sql))
        result_columns = list(result.keys())
        logger.debug("Colunas do resultado: %s", result_columns)
        if save_mode == "substituir":
            recreate_destination_table(db, report_id, destination_table, result_columns)
        else:
            ensure_destination_table(destination_table, result_columns, bool(report and report.is_primary))
        batch: list[dict] = []
        for row in result.mappings():
            batch.append(dict(row))
            if len(batch) >= 1000:
                saved += save_destination_rows(destination_table, execution_id, batch)
                batch = []
        saved += save_destination_rows(destination_table, execution_id, batch)
        if saved:
            backfill_destination_control_aliases(destination_table, result_columns, bool(report and report.is_primary))
            if save_mode == "substituir":
                analyze_destination_table(destination_table)
                ensure_indexes_if_needed(destination_table)
    from app.utils import invalidate_date_column_cache

    invalidate_date_column_cache(destination_table)
    return saved


def preview_report(
    db: Session,
    sql_query: str,
    user_id: int | None,
    report_id: int | None = None,
    allowed_databases: list[str] | None = None,
) -> tuple[ReportExecution, list[dict]]:
    """Executa somente o preview, sem materializar a tabela destino."""
    started = time.perf_counter()
    execution = ReportExecution(
        report_id=report_id,
        user_id=user_id,
        sql_query=sql_query,
        destination_table=None,
        status="success",
        executed_at=datetime.utcnow(),
    )
    rows: list[dict] = []
    try:
        cleaned = validate_select(
            sql_query,
            allowed_databases=allowed_databases or get_allowed_databases(db),
        )
        with local_engine.connect() as connection:
            apply_query_timeout(connection)
            result = connection.execute(text(build_preview_sql(cleaned)))
            rows = [dict(row) for row in result.mappings().fetchmany(PREVIEW_LIMIT)]
        execution.row_count = len(rows)
        execution.result_json = json.dumps(rows, default=str, ensure_ascii=False)
    except (ValueError, SQLAlchemyError) as exc:
        execution.status = "error"
        execution.error_message = str(exc) if isinstance(exc, ValueError) else classify_sql_error(exc)
        rows = []
    finally:
        execution.finished_at = datetime.utcnow()
        execution.duration_ms = int((time.perf_counter() - started) * 1000)
        db.add(execution)
        db.commit()
        db.refresh(execution)
    return execution, rows


def refresh_report_background(report_id: int, user_id: int | None, execution_id: int) -> None:
    """Executa a carga completa em background usando uma sessão própria."""
    db = SessionLocal()
    started = time.perf_counter()
    try:
        report = db.get(Report, report_id)
        execution = db.get(ReportExecution, execution_id)
        if not report or not execution:
            return
        cleaned = validate_select(report.sql_query, allowed_databases=get_allowed_databases(db))
        execution.row_count = save_full_result(
            db,
            cleaned,
            report.destination_table,
            execution_id,
            normalize_save_mode(report.modo_salvamento),
            report_id,
        )
        execution.status = "success"
    except Exception as exc:
        db.rollback()
        execution = db.get(ReportExecution, execution_id)
        if execution:
            execution.status = "error"
            execution.error_message = str(exc)[:500]
        db.add(
            GlpiImportLog(
                level="error",
                message=f"refresh_report_background report_id={report_id}: {exc}",
            )
        )
    finally:
        execution = db.get(ReportExecution, execution_id)
        if execution:
            execution.finished_at = datetime.utcnow()
            execution.duration_ms = int((time.perf_counter() - started) * 1000)
            db.add(execution)
        db.commit()
        db.close()


def run_select(
    db: Session,
    sql_query: str,
    user_id: int | None,
    report_id: int | None = None,
    destination_table: str | None = None,
    modo_salvamento: str = "acrescentar",
) -> tuple[ReportExecution, list[dict]]:
    destination_table = destination_table.strip() if destination_table else None
    if not destination_table:
        return preview_report(db, sql_query, user_id, report_id)

    started = time.perf_counter()
    started_at = datetime.utcnow()
    execution = ReportExecution(
        report_id=report_id,
        user_id=user_id,
        sql_query=sql_query,
        destination_table=destination_table or None,
        status="success",
        executed_at=started_at,
    )
    rows: list[dict] = []

    try:
        cleaned = validate_select(sql_query, allowed_databases=get_allowed_databases(db))
        if destination_table and not IDENTIFIER_RE.match(destination_table):
            raise ValueError("Tabela destino invalida. Use apenas letras, numeros e underscore.")

        db.add(execution)
        db.commit()
        db.refresh(execution)
        execution.row_count = save_full_result(
            db,
            cleaned,
            destination_table,
            execution.id,
            normalize_save_mode(modo_salvamento),
            report_id,
        )
    except ValueError as exc:
        execution.status = "error"
        execution.error_message = str(exc)
        rows = []
    except SQLAlchemyError as exc:
        execution.status = "error"
        execution.error_message = classify_sql_error(exc)
        rows = []
    finally:
        execution.finished_at = datetime.utcnow()
        execution.duration_ms = int((time.perf_counter() - started) * 1000)
        db.add(execution)
        db.commit()
        db.refresh(execution)

    return execution, rows
