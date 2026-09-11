"""Gera a migração de tipos de zabbix_local sem alterar nenhum banco.

Uso:
    venv/bin/python etl/generate_zabbix_native_migration.py \
        --output docs/migrate_zabbix_native_types_2026-07-21.sql
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from app.connector_adapters import PostgreSQLAdapter
from app.database import SessionLocal, local_engine
from app.models import ConnectorConfig


TEXT_TYPES = {"tinytext", "text", "mediumtext", "longtext"}


def quote_identifier(value: str) -> str:
    return f"`{str(value).replace('`', '``')}`"


def generate_sql() -> str:
    with SessionLocal() as session:
        config = (
            session.query(ConnectorConfig)
            .filter(
                ConnectorConfig.connector_type == "zabbix",
                ConnectorConfig.is_active.is_(True),
            )
            .order_by(ConnectorConfig.id.asc())
            .first()
        )
        if not config:
            raise RuntimeError("Conector Zabbix ativo nao encontrado.")
        if (config.db_type or "").lower() != "postgresql":
            raise RuntimeError("O conector Zabbix ativo nao usa PostgreSQL.")
        target_database = config.target_database or "zabbix_local"
        schema = (config.schema_name or "public").strip() or "public"
        adapter = PostgreSQLAdapter()
        source_engine = adapter.build_engine(config)

    try:
        with local_engine.connect() as local_conn, source_engine.connect() as source_conn:
            table_names = [
                row[0]
                for row in local_conn.execute(
                    text(
                        "SELECT TABLE_NAME FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = :database AND TABLE_TYPE = 'BASE TABLE' "
                        "ORDER BY TABLE_NAME"
                    ),
                    {"database": target_database},
                )
            ]
            statements = []
            for table_name in table_names:
                source_columns = {
                    column["column_name"]: column
                    for column in adapter.get_column_info(source_conn, schema, table_name)
                }
                target_columns = {
                    row[0]: str(row[1] or "").lower()
                    for row in local_conn.execute(
                        text(
                            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table"
                        ),
                        {"database": target_database, "table": table_name},
                    )
                }
                for column_name, column in source_columns.items():
                    current_type = target_columns.get(column_name)
                    if current_type not in TEXT_TYPES:
                        # clock/events e problem já são DATETIME: nunca voltar para INT.
                        continue
                    qualified = (
                        f"{quote_identifier(target_database)}.{quote_identifier(table_name)}"
                    )
                    quoted_column = quote_identifier(column_name)
                    if column["is_unix_timestamp"]:
                        statements.append(
                            f"UPDATE {qualified} SET {quoted_column} = "
                            f"FROM_UNIXTIME(CAST({quoted_column} AS UNSIGNED)) - INTERVAL 3 HOUR "
                            f"WHERE {quoted_column} REGEXP '^[0-9]+$';"
                        )
                    statements.append(
                        f"ALTER TABLE {qualified} MODIFY COLUMN {quoted_column} "
                        f"{column['mariadb_type']} NULL;"
                    )
    finally:
        source_engine.dispose()

    header = [
        "-- Migração gerada por etl/generate_zabbix_native_migration.py",
        "-- Data: 2026-07-21",
        "-- Revisar e executar manualmente. Este arquivo nunca é aplicado pelo portal.",
        "-- clock de events/problem já DATETIME é preservado e não aparece abaixo.",
        "",
    ]
    return "\n".join([*header, *statements, ""])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate_sql(), encoding="utf-8")
    print(f"SQL gerado em {output}")


if __name__ == "__main__":
    main()
