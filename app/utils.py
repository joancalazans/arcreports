from __future__ import annotations

from dataclasses import dataclass
import re
import threading

from sqlalchemy import text

from app.config import get_settings
from app.database import local_engine
from app.reporting import CONTROL_COLUMN_ALIASES, quote_column, quote_identifier


settings = get_settings()
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
TEMPORAL_SUFFIX_RE = re.compile(
    r"(Date|date|_at|_clock|_time)$|^(Data[A-Z])",
    re.IGNORECASE,
)
DURATION_DENYLIST_RE = re.compile(
    r"(duracao|duration|elapsed|horas|hours|minutos|minutes|seconds|segundos)$",
    re.IGNORECASE,
)
_sample_cache: dict[tuple, bool] = {}
_sample_lock = threading.Lock()


@dataclass
class TemporalPlan:
    """Plano temporal resolvido uma vez e compartilhado por todo o filtro."""

    selected_column: str
    source_table: str
    source_database: str
    inherited: bool
    parent_report_id: int | None
    injection_strategy: str
    allowed_databases: list[str]
    available_columns: list[dict]
    default_column: str | None = None


DATE_COLUMN_LABELS = {
    "date_creation": "Data de Criação",
    "CreatedDate": "Data de Criação",
    "created_at": "Data de Criação",
    "Criado em": "Data de Criação",
    "solvedate": "Data de Conclusão",
    "CompletedDate": "Data de Conclusão",
    "solved_at": "Data de Conclusão",
    "closedate": "Data de Fechamento",
    "CloseDate": "Data de Fechamento",
    "closed_at": "Data de Fechamento",
    "Concluído": "Data de Conclusão",
    "Alterado em": "Data de Alteração",
    "Início": "Data de Início",
    "Data prevista": "Data Prevista",
    "date": "Início do SLA",
    "StartDate": "Início do SLA",
    "reference_date": "Início do SLA",
    "time_to_resolve": "Vencimento do SLA",
    "TargetEndDate": "Vencimento do SLA",
    "target_end_at": "Vencimento do SLA",
}
DATE_ALIAS_COLUMNS = tuple(
    alias
    for aliases in CONTROL_COLUMN_ALIASES.values()
    for alias in aliases
)
DATE_ALIAS_SQL = ", ".join(f"'{column}'" for column in DATE_ALIAS_COLUMNS)
CANONICAL_DATE_ALIAS_COLUMNS = {
    canonical: set(aliases)
    for canonical, aliases in CONTROL_COLUMN_ALIASES.items()
}


def is_internal_portal_column(column_name: str | None) -> bool:
    name = column_name or ""
    return name.startswith("__")


def _is_temporal_by_sample(
    connection,
    database: str,
    table: str,
    column: str,
    sample_size: int = 20,
) -> bool:
    cache_key = (database, table, column)
    with _sample_lock:
        if cache_key in _sample_cache:
            return _sample_cache[cache_key]
    formats = (
        "'%d/%m/%Y %H:%i:%s'",
        "'%Y-%m-%d %H:%i:%s'",
        "'%Y-%m-%d'",
        "'%d/%m/%Y'",
    )
    try:
        safe_database = quote_identifier(database)
        safe_table = quote_identifier(table)
        safe_column = quote_column(column)
        for date_format in formats:
            row = connection.execute(
                text(
                    f"SELECT 1 FROM {safe_database}.{safe_table} "
                    f"WHERE {safe_column} IS NOT NULL AND {safe_column} != '' "
                    f"AND STR_TO_DATE({safe_column}, {date_format}) IS NOT NULL "
                    f"LIMIT {int(sample_size)}"
                )
            ).first()
            if row:
                with _sample_lock:
                    _sample_cache[cache_key] = True
                return True
        with _sample_lock:
            _sample_cache[cache_key] = False
        return False
    except Exception:
        return False


def get_date_columns(
    tabela_destino: str,
    campo_sql_periodo: str | None = None,
) -> list[dict]:
    """Detecta colunas temporais por configuração, tipo, alias, nome e amostra."""
    if not IDENTIFIER_RE.match(tabela_destino or ""):
        return []
    database = settings.local_db_name
    columns: list[dict] = []
    seen: set[str] = set()

    def add_col(name: str, label: str | None = None) -> None:
        if name not in seen:
            seen.add(name)
            columns.append({"name": name, "label": label or DATE_COLUMN_LABELS.get(name, name)})

    with local_engine.connect() as connection:
        rows = list(connection.execute(
            text(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = :database "
                "AND table_name = :tabela "
                "ORDER BY ordinal_position"
            ),
            {"tabela": tabela_destino, "database": database},
        ).mappings())
        all_columns = {row["column_name"]: row["data_type"] for row in rows}
        suppressed_aliases = {
            alias
            for canonical, aliases in CANONICAL_DATE_ALIAS_COLUMNS.items()
            if canonical in all_columns
            for alias in aliases
        }

        if (
            campo_sql_periodo
            and campo_sql_periodo in all_columns
            and not is_internal_portal_column(campo_sql_periodo)
        ):
            add_col(campo_sql_periodo)

        for column, data_type in all_columns.items():
            if (
                not is_internal_portal_column(column)
                and column not in suppressed_aliases
                and data_type in ("datetime", "date", "timestamp")
            ):
                add_col(column)

        for column, data_type in all_columns.items():
            if (
                not is_internal_portal_column(column)
                and column not in suppressed_aliases
                and data_type == "longtext"
                and column in DATE_COLUMN_LABELS
            ):
                add_col(column)

        for column, data_type in all_columns.items():
            if (
                is_internal_portal_column(column)
                or column in suppressed_aliases
                or data_type != "longtext"
                or column in seen
            ):
                continue
            if DURATION_DENYLIST_RE.search(column) or not TEMPORAL_SUFFIX_RE.search(column):
                continue
            if _is_temporal_by_sample(connection, database, tabela_destino, column):
                add_col(column)
        return columns


def invalidate_date_column_cache(table_name: str) -> None:
    """Invalida as validações amostrais após atualizar uma tabela destino."""
    database = settings.local_db_name
    with _sample_lock:
        keys = [key for key in _sample_cache if key[0] == database and key[1] == table_name]
        for key in keys:
            del _sample_cache[key]
