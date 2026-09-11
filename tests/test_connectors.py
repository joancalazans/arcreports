from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import glpi_import
from app.database import local_engine
from app.models import ConnectorConfig, ConnectorRun, ConnectorSyncTable


def make_config(connector_type: str, *, is_active: bool = True, target_database: str | None = None) -> ConnectorConfig:
    return ConnectorConfig(
        name=f"{connector_type.upper()} Teste",
        connector_type=connector_type,
        db_type="mysql",
        host="localhost",
        port="3306",
        database_name=f"{connector_type}_source",
        target_database=target_database or f"{connector_type}_local",
        table_prefix=f"{connector_type}_",
        username="user",
        password="pass",
        suggested_frequency="08:00",
        is_active=is_active,
    )


def test_connector_sync_table_unique_constraint_is_composite(db_session):
    db_session.add_all(
        [
            ConnectorSyncTable(connector_type="glpi", table_name="hosts", is_active=True),
            ConnectorSyncTable(connector_type="zabbix", table_name="hosts", is_active=True),
        ]
    )
    db_session.commit()

    db_session.add(ConnectorSyncTable(connector_type="glpi", table_name="hosts", is_active=True))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_inactive_connector_is_not_included_in_connector_import(db_session, monkeypatch):
    db_session.add_all(
        [
            make_config("glpi", is_active=True),
            make_config("zabbix", is_active=False),
            ConnectorSyncTable(connector_type="glpi", table_name="hosts", is_active=True),
            ConnectorSyncTable(connector_type="zabbix", table_name="hosts", is_active=True),
        ]
    )
    db_session.commit()

    imported = []

    monkeypatch.setattr(glpi_import, "ensure_target_database", lambda config, local_engine=None: (True, None))
    monkeypatch.setattr(glpi_import, "discover_source_tables", lambda db, config=None: set())

    def fake_run_table_import(db, sync_table, mode):
        imported.append((sync_table.connector_type, sync_table.table_name, mode))
        return ConnectorRun(
            connector_type=sync_table.connector_type,
            table_name=sync_table.table_name,
            mode=mode,
            status="success",
        )

    monkeypatch.setattr(glpi_import, "run_table_import", fake_run_table_import)

    glpi_import.run_connector_import(db_session, "incremental")

    assert imported == [("glpi", "hosts", "incremental")]
    assert glpi_import.get_connection_config(db_session, "zabbix") is None


def test_get_connection_config_generic_and_by_type(db_session):
    glpi = make_config("glpi", is_active=True)
    zabbix = make_config("zabbix", is_active=True)
    inactive = make_config("redmine", is_active=False)
    db_session.add_all([glpi, zabbix, inactive])
    db_session.commit()

    assert glpi_import.get_connection_config(db_session).connector_type == "glpi"
    assert glpi_import.get_connection_config(db_session, "glpi").connector_type == "glpi"
    assert glpi_import.get_connection_config(db_session, "zabbix").connector_type == "zabbix"
    assert glpi_import.get_connection_config(db_session, "redmine") is None


def test_target_import_database_uses_config_target_database():
    config = make_config("zabbix", target_database="zabbix_local")

    assert glpi_import.target_import_database(config) == "zabbix_local"


def test_glpi_tables_exist_only_in_connector_database():
    try:
        with local_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA IN ('reports', 'glpi_local') "
                    "AND TABLE_NAME = 'glpi_tickets'"
                )
            ).all()
    except SQLAlchemyError as exc:
        pytest.skip(f"Banco local indisponivel para information_schema: {exc}")

    table_types = {(row.TABLE_SCHEMA, row.TABLE_NAME): row.TABLE_TYPE for row in rows}
    assert ("reports", "glpi_tickets") not in table_types
    assert table_types[("glpi_local", "glpi_tickets")] == "BASE TABLE"
