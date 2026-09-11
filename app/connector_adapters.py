from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL

from app.config import Settings, settings
from app.crypto import decrypt_password


ALWAYS_LARGE_TABLES = {
    "events",
    "history",
    "history_uint",
    "history_str",
    "history_log",
    "history_text",
    "history_bin",
    "trends",
    "trends_uint",
    "alerts",
    "auditlog",
    "event_tag",
}

# Perfis versionados de carga incremental por sistema.
# Apenas as tabelas explicitamente aprovadas abaixo usam incremental;
# toda tabela ausente do perfil usa FULL por padrão.
INCREMENTAL_PROFILES_VERSION = "2026-07-24"
INCREMENTAL_PROFILES: dict[str, dict[str, str]] = {
    "zabbix": {
        "events": "clock",
        "problem": "clock",
    },
    "glpi": {
        "glpi_logs": "date_mod",
        "glpi_crontasklogs": "date_mod",
        "glpi_events": "date_mod",
        "glpi_rulematchedlogs": "date_mod",
        "glpi_tickets": "date_mod",
        "glpi_tickets_users": "date_mod",
        "glpi_groups_tickets": "date_mod",
        "glpi_changes": "date_mod",
        "glpi_problems": "date_mod",
    },
    "redmine": {
        "journals": "updated_on",
        "journal_details": "id",
        "time_entries": "updated_on",
        "issues": "updated_on",
        "changesets": "committed_on",
        "changes": "id",
    },
}


def get_table_load_profile(
    connector_type: str,
    table_name: str,
) -> tuple[str, str | None]:
    """Retorna a carga configurada para a tabela, com FULL conservador."""
    normalized_connector = str(connector_type or "").strip().lower()
    normalized_table = str(table_name or "").strip().lower()
    profile = INCREMENTAL_PROFILES.get(normalized_connector, {})
    incremental_column = profile.get(normalized_table)
    if incremental_column:
        return "incremental", incremental_column
    return "full", None


class SourceAdapter:
    """Adaptador de origem de dados por db_type."""

    def build_engine(self, config) -> Engine:
        """Cria engine SQLAlchemy para a origem."""
        raise NotImplementedError

    def quote_identifier(self, name: str) -> str:
        """Envolve identificador com quoting correto."""
        raise NotImplementedError

    def qualified_table(self, config, table_name: str) -> str:
        """Retorna nome qualificado da tabela."""
        raise NotImplementedError

    def get_tables(self, connection, prefix: str, schema: str) -> list[str]:
        """Descobre tabelas da origem por prefixo."""
        raise NotImplementedError

    def get_columns(self, connection, config, table_name: str) -> list[str]:
        """Retorna colunas de uma tabela da origem."""
        raise NotImplementedError

    def get_full_query(self, config, table_name: str) -> str:
        """SELECT completo da tabela."""
        raise NotImplementedError

    def get_incremental_query(self, config, table_name: str, incremental_col: str, since) -> tuple[str, dict]:
        """SELECT incremental com parâmetros."""
        raise NotImplementedError

    def build_initial_days_filter(
        self,
        incremental_column: str,
        initial_days: int,
        column_type: str = "datetime",
    ) -> str:
        """Filtro de janela inicial no dialeto da origem."""
        raise NotImplementedError

    def normalize_clock_value(self, value, col_type: str):
        """Normaliza valor de clock/timestamp."""
        raise NotImplementedError

    def get_incremental_candidates(self) -> list[str]:
        """Colunas candidatas para incremental."""
        raise NotImplementedError

    def is_large_table(self, table_name: str) -> bool:
        """Tabela volumosa que exige janela temporal."""
        return str(table_name).lower() in ALWAYS_LARGE_TABLES


class MySQLAdapter(SourceAdapter):
    def build_engine(self, config) -> Engine:
        plain_password = decrypt_password(config.password or "", settings.encryption_key)
        url = Settings.mysql_url(config.username, plain_password, config.host, config.port, config.database_name)
        return create_engine(url, pool_pre_ping=True, future=True)

    def quote_identifier(self, name: str) -> str:
        return f"`{str(name).replace('`', '``')}`"

    def qualified_table(self, config, table_name: str) -> str:
        schema = getattr(config, "database_name", None)
        if schema:
            return f"{self.quote_identifier(schema)}.{self.quote_identifier(table_name)}"
        return self.quote_identifier(table_name)

    def get_tables(self, connection, prefix: str, schema: str) -> list[str]:
        like_prefix = f"{prefix or ''}%"
        result = connection.execute(
            text(
                "SELECT TABLE_NAME "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :schema "
                "AND TABLE_TYPE = 'BASE TABLE' "
                "AND TABLE_NAME LIKE :prefix "
                "ORDER BY TABLE_NAME"
            ),
            {"schema": schema, "prefix": like_prefix},
        )
        return [row[0] for row in result]

    def get_columns(self, connection, config, table_name: str) -> list[str]:
        result = connection.execute(text(f"SELECT * FROM {self.qualified_table(config, table_name)} LIMIT 0"))
        return list(result.keys())

    def get_full_query(self, config, table_name: str) -> str:
        return f"SELECT * FROM {self.qualified_table(config, table_name)}"

    def get_incremental_query(self, config, table_name: str, incremental_col: str, since) -> tuple[str, dict]:
        table_sql = self.qualified_table(config, table_name)
        column_sql = self.quote_identifier(incremental_col)
        return (
            f"SELECT * FROM {table_sql} "
            f"WHERE {column_sql} >= DATE_SUB(:since, INTERVAL 2 HOUR) "
            f"ORDER BY {column_sql}",
            {"since": since},
        )

    def build_initial_days_filter(
        self,
        incremental_column: str,
        initial_days: int,
        column_type: str = "datetime",
    ) -> str:
        column_sql = self.quote_identifier(incremental_column)
        days = int(initial_days)
        if column_type == "unix_timestamp":
            return f"{column_sql} >= UNIX_TIMESTAMP(NOW() - INTERVAL {days} DAY)"
        return f"{column_sql} >= NOW() - INTERVAL {days} DAY"

    def normalize_clock_value(self, value, col_type: str):
        return value

    def get_incremental_candidates(self) -> list[str]:
        return ["date_mod", "date_creation", "date", "updated_at"]

    def is_large_table(self, table_name: str) -> bool:
        return super().is_large_table(table_name)


class PostgreSQLAdapter(SourceAdapter):
    PG_TO_MARIADB = {
        "smallint": "SMALLINT",
        "integer": "INT",
        "bigint": "BIGINT",
        "int2": "SMALLINT",
        "int4": "INT",
        "int8": "BIGINT",
        "numeric": "DECIMAL(20,6)",
        "decimal": "DECIMAL(20,6)",
        "real": "FLOAT",
        "double precision": "DOUBLE",
        "boolean": "TINYINT(1)",
        "bool": "TINYINT(1)",
        "character varying": "VARCHAR(255)",
        "varchar": "VARCHAR(255)",
        "character": "CHAR(1)",
        "text": "TEXT",
        "name": "VARCHAR(64)",
        "uuid": "VARCHAR(36)",
        "timestamp without time zone": "DATETIME",
        "timestamp with time zone": "DATETIME",
        "timestamp": "DATETIME",
        "date": "DATE",
        "interval": "VARCHAR(64)",
        "json": "JSON",
        "jsonb": "JSON",
        "bytea": "LONGBLOB",
        "inet": "VARCHAR(45)",
        "oid": "BIGINT",
    }
    _TIMESTAMP_SUFFIXES = (
        "clock",
        "_at",
        "_date",
        "_since",
        "_till",
        "_time",
        "_from",
        "_to",
        "lastchange",
        "start_date",
        "active_since",
        "active_till",
        "maintenance_from",
    )

    def build_engine(self, config) -> Engine:
        query: dict[str, Any] = {}
        schema = (getattr(config, "schema_name", None) or "").strip()
        if schema:
            query["options"] = f"-c search_path={schema}"
        url = URL.create(
            "postgresql+psycopg2",
            username=config.username,
            password=decrypt_password(config.password or "", settings.encryption_key),
            host=config.host,
            port=int(config.port or 5432),
            database=config.database_name,
            query=query,
        )
        return create_engine(url, pool_pre_ping=True, future=True)

    def quote_identifier(self, name: str) -> str:
        return f'"{str(name).replace(chr(34), chr(34) * 2)}"'

    def qualified_table(self, config, table_name: str) -> str:
        schema = (getattr(config, "schema_name", None) or "public").strip() or "public"
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(table_name)}"

    def get_tables(self, connection, prefix: str, schema: str) -> list[str]:
        params = {"schema": schema or "public"}
        prefix_clause = ""
        if prefix:
            prefix_clause = "AND table_name LIKE :prefix "
            params["prefix"] = f"{prefix}%"
        result = connection.execute(
            text(
                "SELECT table_name "
                "FROM information_schema.tables "
                "WHERE table_schema = :schema "
                "AND table_type = 'BASE TABLE' "
                f"{prefix_clause}"
                "ORDER BY table_name"
            ),
            params,
        )
        return [row[0] for row in result]

    def get_columns(self, connection, config, table_name: str) -> list[str]:
        result = connection.execute(text(f"SELECT * FROM {self.qualified_table(config, table_name)} LIMIT 0"))
        return list(result.keys())

    def get_column_info(self, source_conn, schema: str, table: str) -> list[dict]:
        """Retorna metadados da origem PostgreSQL e o tipo equivalente no MariaDB."""
        result = source_conn.execute(
            text(
                "SELECT column_name, data_type, character_maximum_length, "
                "numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "ORDER BY ordinal_position"
            ),
            {"schema": schema or "public", "table": table},
        )
        columns = []
        for row in result.mappings():
            pg_type = str(row["data_type"] or "").lower()
            char_len = row["character_maximum_length"]
            num_prec = row["numeric_precision"]
            num_scale = row["numeric_scale"]
            if pg_type in ("character varying", "varchar"):
                mariadb_type = f"VARCHAR({min(char_len or 255, 65535)})"
            elif pg_type == "character":
                mariadb_type = f"CHAR({char_len or 1})"
            elif pg_type in ("numeric", "decimal"):
                if num_prec and num_scale is not None:
                    mariadb_type = f"DECIMAL({num_prec},{num_scale})"
                else:
                    mariadb_type = "DECIMAL(20,6)"
            else:
                mariadb_type = self.PG_TO_MARIADB.get(pg_type, "TEXT")
            is_unix = pg_type in {
                "integer", "bigint", "int4", "int8", "int2", "smallint"
            } and self._looks_like_timestamp(row["column_name"])
            if is_unix:
                mariadb_type = "DATETIME"
            columns.append(
                {
                    "column_name": row["column_name"],
                    "pg_type": pg_type,
                    "mariadb_type": mariadb_type,
                    "is_unix_timestamp": is_unix,
                    "char_max_length": char_len,
                    "numeric_precision": num_prec,
                    "numeric_scale": num_scale,
                }
            )
        self._unix_timestamp_columns = {
            column["column_name"]
            for column in columns
            if column["is_unix_timestamp"]
        }
        self._numeric_columns = {
            column["column_name"]
            for column in columns
            if column["pg_type"] in {
                "smallint", "integer", "bigint", "int2", "int4", "int8"
            }
            and not column["is_unix_timestamp"]
        }
        return columns

    def _looks_like_timestamp(self, col: str) -> bool:
        col_lower = str(col).lower()
        return any(
            col_lower == suffix or col_lower.endswith(suffix)
            for suffix in self._TIMESTAMP_SUFFIXES
        )

    def get_full_query(self, config, table_name: str) -> str:
        return f"SELECT * FROM {self.qualified_table(config, table_name)}"

    def get_incremental_query(self, config, table_name: str, incremental_col: str, since) -> tuple[str, dict]:
        table_sql = self.qualified_table(config, table_name)
        column_sql = self.quote_identifier(incremental_col)
        unix_columns = getattr(self, "_unix_timestamp_columns", {"clock"})
        numeric_columns = getattr(self, "_numeric_columns", set())
        if incremental_col in numeric_columns:
            return (
                f"SELECT * FROM {table_sql} WHERE {column_sql} >= :since_value ORDER BY {column_sql}",
                {"since_value": int(since)},
            )
        if incremental_col in unix_columns:
            since_dt = since
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            since_epoch = int(since_dt.timestamp()) - 7200
            return (
                f"SELECT * FROM {table_sql} WHERE {column_sql} >= :since_epoch ORDER BY {column_sql}",
                {"since_epoch": since_epoch},
            )
        since_value = since - timedelta(hours=2)
        return (
            f"SELECT * FROM {table_sql} WHERE {column_sql} >= :since ORDER BY {column_sql}",
            {"since": since_value},
        )

    def build_initial_days_filter(
        self,
        incremental_column: str,
        initial_days: int,
        column_type: str = "datetime",
    ) -> str:
        column_sql = self.quote_identifier(incremental_column)
        days = int(initial_days)
        if column_type == "unix_timestamp":
            return (
                f"{column_sql} >= "
                f"EXTRACT(EPOCH FROM NOW() - INTERVAL '{days} days')"
            )
        return f"{column_sql} >= NOW() - INTERVAL '{days} days'"

    def normalize_clock_value(self, value, col_type: str):
        if value is None:
            return None
        if col_type == "integer":
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        return value

    def get_incremental_candidates(self) -> list[str]:
        return ["clock", "updated_at", "date_mod", "created_at"]

    def is_large_table(self, table_name: str) -> bool:
        return super().is_large_table(table_name)


def get_adapter(db_type: str) -> SourceAdapter:
    """Retorna adaptador correto por db_type."""
    db_type = str(db_type or "").strip().lower()
    if db_type == "postgresql":
        return PostgreSQLAdapter()
    if db_type in ("mysql", "mariadb"):
        return MySQLAdapter()
    if db_type == "oracle":
        raise ValueError(
            "Conector Oracle ainda não suportado. "
            "Disponível na v0.3."
        )
    raise ValueError(
        f"Tipo de banco '{db_type}' não suportado. "
        "Use: mysql, mariadb ou postgresql."
    )
