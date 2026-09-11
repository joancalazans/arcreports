from __future__ import annotations

import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db, local_engine
from app.models import AdminActionLog
from app.config import get_settings
from app.reporting import apply_query_timeout, mask_comments_and_strings
from app.routes.common import form_data, get_allowed_databases, render
from app.security import require_admin, verify_csrf
from app.system_config import get_system_config_value


router = APIRouter()
settings = get_settings()
PER_PAGE = 100
READ_COMMANDS = {"SELECT", "DESCRIBE", "SHOW"}
ALLOWED_WRITE_TABLES = {
    "reports",
    "dashboard_sources",
    "dashboard_widgets",
    "system_config",
    "trs_sla_contratos",
}
ALLOWED_WRITE_PREFIXES = ("relatorio_", "dashboard_", "memora_")
BLOCKED_WRITE_TABLES = {"users", "auth_logs", "admin_action_logs"}
BLOCKED_WRITE_PREFIXES = ("glpi_",)


class SqlConsoleError(ValueError):
    pass


def is_sql_console_enabled(db: Session) -> bool:
    value = get_system_config_value(db, "sql_console_enabled", "true") or "true"
    return value.lower() == "true"


def allowed_sql_console_databases(db: Session) -> list[str]:
    return get_allowed_databases(db)


def split_first_statement(sql_query: str) -> tuple[str, bool]:
    cleaned = sql_query.strip()
    if not cleaned:
        raise SqlConsoleError("Informe um comando SQL.")
    masked = mask_comments_and_strings(cleaned)
    semicolon_pos = masked.find(";")
    if semicolon_pos < 0:
        return cleaned, False
    first = cleaned[:semicolon_pos].strip()
    if not first:
        raise SqlConsoleError("Informe um comando SQL antes do ponto e virgula.")
    return first, bool(cleaned[semicolon_pos + 1 :].strip())


def command_name(sql_query: str) -> str:
    match = re.match(r"^\s*([A-Za-z]+)\b", mask_comments_and_strings(sql_query))
    if not match:
        raise SqlConsoleError("Comando SQL nao identificado.")
    return match.group(1).upper()


def parse_table_reference(sql_query: str, keyword_pattern: str) -> str | None:
    pattern = re.compile(
        rf"^\s*{keyword_pattern}\s+"
        r"(?:(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[A-Za-z0-9_]+))\s*\.\s*)?"
        r"(?:`(?P<table_bt>[^`]+)`|(?P<table>[A-Za-z0-9_]+))",
        re.I,
    )
    match = pattern.match(mask_comments_and_strings(sql_query))
    if not match:
        return None
    return match.group("table_bt") or match.group("table")


def is_write_table_allowed(table_name: str | None) -> bool:
    if not table_name:
        return False
    normalized = table_name.lower()
    if normalized in BLOCKED_WRITE_TABLES or normalized.startswith(BLOCKED_WRITE_PREFIXES):
        return False
    return normalized in ALLOWED_WRITE_TABLES or normalized.startswith(ALLOWED_WRITE_PREFIXES)


def is_destination_table(table_name: str | None) -> bool:
    if not table_name:
        return False
    normalized = table_name.lower()
    if normalized in BLOCKED_WRITE_TABLES or normalized.startswith(BLOCKED_WRITE_PREFIXES):
        return False
    if normalized in ALLOWED_WRITE_TABLES:
        return False
    return normalized.startswith(ALLOWED_WRITE_PREFIXES)


def ensure_where_for_update(sql_query: str) -> None:
    masked = mask_comments_and_strings(sql_query)
    if not re.search(r"\bWHERE\b", masked, re.I):
        raise SqlConsoleError(
            "UPDATE sem WHERE pode afetar todos os registros. "
            "Adicione uma cláusula WHERE para continuar."
        )


def validate_show(sql_query: str) -> None:
    masked = mask_comments_and_strings(sql_query)
    if not re.match(r"^\s*SHOW\s+(COLUMNS|TABLES)\b", masked, re.I):
        raise SqlConsoleError("SHOW permitido apenas para SHOW COLUMNS ou SHOW TABLES.")


def referenced_databases(sql_query: str) -> set[str]:
    masked = mask_comments_and_strings(sql_query)
    table_reference_pattern = re.compile(
        r"\b(?:FROM|JOIN|UPDATE|INTO|DESCRIBE|TABLE)\s+"
        r"(?:(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[A-Za-z0-9_]+))\s*\.\s*)"
        r"(?:`[^`]+`|[A-Za-z0-9_]+)",
        re.I,
    )
    show_tables_database_pattern = re.compile(
        r"\bSHOW\s+TABLES\b.*?\b(?:FROM|IN)\s+"
        r"(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[A-Za-z0-9_]+))\b",
        re.I,
    )
    show_columns_database_pattern = re.compile(
        r"\bSHOW\s+COLUMNS\s+(?:FROM|IN)\s+"
        r"(?:`[^`]+`|[A-Za-z0-9_]+)\s+"
        r"(?:FROM|IN)\s+(?:`(?P<schema_bt>[^`]+)`|(?P<schema>[A-Za-z0-9_]+))\b",
        re.I,
    )
    databases = set()
    for match in table_reference_pattern.finditer(masked):
        schema = match.group("schema_bt") or match.group("schema")
        if schema:
            databases.add(schema)
    for match in show_tables_database_pattern.finditer(masked):
        schema = match.group("schema_bt") or match.group("schema")
        if schema:
            databases.add(schema)
    for match in show_columns_database_pattern.finditer(masked):
        schema = match.group("schema_bt") or match.group("schema")
        if schema:
            databases.add(schema)
    return databases


def validate_allowed_databases(sql_query: str, db: Session, command: str) -> None:
    allowed_databases = set(allowed_sql_console_databases(db))
    for database_name in referenced_databases(sql_query):
        if database_name not in allowed_databases:
            raise SqlConsoleError(f"{command} bloqueado para o database '{database_name}'.")


def validate_alter(sql_query: str) -> str:
    table_name = parse_table_reference(sql_query, r"ALTER\s+TABLE")
    if not re.match(
        r"^\s*ALTER\s+TABLE\s+(?:`[^`]+`|[A-Za-z0-9_]+)(?:\s*\.\s*(?:`[^`]+`|[A-Za-z0-9_]+))?\s+ADD\s+COLUMN\b",
        mask_comments_and_strings(sql_query),
        re.I,
    ):
        raise SqlConsoleError("ALTER permitido apenas no formato ALTER TABLE ... ADD COLUMN.")
    if not is_destination_table(table_name):
        raise SqlConsoleError(f"ALTER bloqueado para a tabela '{table_name or '-'}'.")
    return table_name or ""


def validate_sql_console_command(sql_query: str, db: Session) -> tuple[str, str]:
    command = command_name(sql_query)
    if command in {"DROP", "TRUNCATE", "DELETE"}:
        raise SqlConsoleError(f"Comando {command} bloqueado neste console.")
    if command not in {"SELECT", "UPDATE", "INSERT", "ALTER", "DESCRIBE", "SHOW"}:
        raise SqlConsoleError(
            "Comando bloqueado. Use apenas SELECT, UPDATE, INSERT, ALTER, DESCRIBE ou SHOW."
        )

    validate_allowed_databases(sql_query, db, command)

    if command == "SELECT":
        return command, ""
    if command == "DESCRIBE":
        table_name = parse_table_reference(sql_query, "DESCRIBE") or ""
        return command, table_name
    if command == "SHOW":
        validate_show(sql_query)
        return command, ""
    if command == "UPDATE":
        table_name = parse_table_reference(sql_query, "UPDATE")
        ensure_where_for_update(sql_query)
        if not is_write_table_allowed(table_name):
            raise SqlConsoleError(f"UPDATE bloqueado para a tabela '{table_name or '-'}'.")
        return command, table_name or ""
    if command == "INSERT":
        table_name = parse_table_reference(sql_query, r"INSERT\s+INTO")
        if not is_write_table_allowed(table_name):
            raise SqlConsoleError(f"INSERT bloqueado para a tabela '{table_name or '-'}'.")
        return command, table_name or ""
    return command, validate_alter(sql_query)


def log_sql_console_action(
    db: Session,
    user,
    command_text: str,
    status_value: str,
    result_text: str,
    request: Request,
    table_name: str = "",
) -> None:
    ip_address = request.client.host if request.client else ""
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action="sql_console",
            table_name=table_name or None,
            status=status_value,
            message=f"detalhe: {command_text[:500]} | resultado: {result_text}",
            ip_address=ip_address or None,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()


def execute_sql_console_command(sql_query: str, command: str) -> dict:
    if command in READ_COMMANDS:
        with local_engine.connect() as connection:
            apply_query_timeout(connection)
            result = connection.execute(text(sql_query))
            rows = [dict(row) for row in result.mappings().all()]
            return {
                "kind": "rows",
                "columns": list(result.keys()),
                "rows": rows,
                "row_count": len(rows),
            }

    with local_engine.begin() as connection:
        apply_query_timeout(connection)
        result = connection.execute(text(sql_query))
        affected = result.rowcount if result.rowcount and result.rowcount > 0 else 0
        return {"kind": "message", "affected": affected}


def sql_console_context(**overrides) -> dict:
    context = {
        "active": "sql_console",
        "sql_query": "",
        "result": None,
        "error": "",
        "warning": "",
        "history": [],
        "per_page": PER_PAGE,
        "allowed_write_tables": sorted(ALLOWED_WRITE_TABLES),
        "allowed_write_prefixes": ALLOWED_WRITE_PREFIXES,
        "blocked_operations": [
            "DROP",
            "TRUNCATE",
            "DELETE",
            "UPDATE/INSERT em users, auth_logs, admin_action_logs e glpi_*",
            "UPDATE sem WHERE",
            "Comandos fora da lista permitida",
        ],
    }
    context.update(overrides)
    return context


@router.get("/admin/sql-console", response_class=HTMLResponse)
def sql_console_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not is_sql_console_enabled(db):
        raise HTTPException(
            status_code=403,
            detail="Console SQL desativado pelo administrador.",
        )
    return render(request, "sql_console.html", sql_console_context(), db)


@router.post("/admin/sql-console", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def sql_console_run(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not is_sql_console_enabled(db):
        raise HTTPException(
            status_code=403,
            detail="Console SQL desativado pelo administrador.",
        )

    data = await form_data(request)
    raw_sql = data.get("sql_query", "")
    try:
        history = json.loads(data.get("session_history", "[]"))
        if not isinstance(history, list):
            history = []
    except json.JSONDecodeError:
        history = []

    result = None
    error = ""
    warning = ""
    executed_sql = raw_sql.strip()
    table_name = ""
    try:
        executable_sql, ignored_extra = split_first_statement(raw_sql)
        command, table_name = validate_sql_console_command(executable_sql, db)
        result = execute_sql_console_command(executable_sql, command)
        executed_sql = executable_sql
        if ignored_extra:
            warning = "Multiplos comandos detectados. Apenas o primeiro foi executado; os demais foram ignorados."
        result_text = (
            f"sucesso {result['row_count']} linhas"
            if result["kind"] == "rows"
            else f"sucesso {result['affected']} linhas"
        )
        log_sql_console_action(db, user, executable_sql, "success", result_text, request, table_name)
        history = [executable_sql, *[item for item in history if item != executable_sql]][:10]
    except (SqlConsoleError, SQLAlchemyError, ValueError) as exc:
        error = str(exc)
        log_sql_console_action(db, user, executed_sql, "error", f"erro: {error}", request, table_name)

    return render(
        request,
        "sql_console.html",
        sql_console_context(
            sql_query=raw_sql,
            result=result,
            error=error,
            warning=warning,
            history=history[:10],
        ),
        db,
    )
