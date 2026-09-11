from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import glpi_import
from app.connector_adapters import (
    MySQLAdapter,
    PostgreSQLAdapter,
    get_adapter,
    get_table_load_profile,
)
from app.models import ConnectorConfig, ConnectorRun, ConnectorSyncTable


def make_config(**overrides) -> ConnectorConfig:
    values = {
        "name": "Zabbix Teste",
        "connector_type": "zabbix",
        "db_type": "postgresql",
        "host": "localhost",
        "port": "5432",
        "database_name": "zabbix",
        "target_database": "zabbix_local",
        "table_prefix": "",
        "schema_name": "public",
        "initial_days": 90,
        "username": "user",
        "password": "pass",
        "suggested_frequency": "08:00",
        "is_active": True,
    }
    values.update(overrides)
    return ConnectorConfig(**values)


def test_get_adapter_factory():
    assert isinstance(get_adapter("mysql"), MySQLAdapter)
    assert isinstance(get_adapter("mariadb"), MySQLAdapter)
    assert isinstance(get_adapter("postgresql"), PostgreSQLAdapter)
    with pytest.raises(ValueError):
        get_adapter("oracle")


def test_get_table_load_profile_zabbix_events():
    assert get_table_load_profile("zabbix", "events") == ("incremental", "clock")


def test_get_table_load_profile_zabbix_services():
    assert get_table_load_profile("zabbix", "services") == ("full", None)


def test_get_table_load_profile_unknown_connector():
    assert get_table_load_profile("unknown_system", "any_table") == ("full", None)


def test_get_table_load_profile_glpi_logs():
    assert get_table_load_profile("glpi", "glpi_logs") == ("incremental", "date_mod")


def test_get_adapter_unknown_type_raises():
    with pytest.raises(ValueError, match="não suportado"):
        get_adapter("unknown_db")


def test_quote_identifier_sanitizes_quotes():
    assert MySQLAdapter().quote_identifier("nome") == "`nome`"
    assert PostgreSQLAdapter().quote_identifier("nome") == '"nome"'
    assert MySQLAdapter().quote_identifier("ta`bela") == "`ta``bela`"
    assert PostgreSQLAdapter().quote_identifier('ta"bela') == '"ta""bela"'


def test_incremental_candidates_order():
    assert MySQLAdapter().get_incremental_candidates()[0] == "date_mod"
    assert PostgreSQLAdapter().get_incremental_candidates()[0] == "clock"


def test_large_table_detection():
    assert MySQLAdapter().is_large_table("history") is True
    assert MySQLAdapter().is_large_table("events") is True
    assert PostgreSQLAdapter().is_large_table("history") is True
    assert PostgreSQLAdapter().is_large_table("hosts") is False
    assert PostgreSQLAdapter().is_large_table("events") is True
    assert PostgreSQLAdapter().is_large_table("trends") is True


def test_postgresql_build_initial_days_filter_datetime():
    adapter = PostgreSQLAdapter()
    result = adapter.build_initial_days_filter("date_mod", 90, "datetime")

    assert "INTERVAL '90 days'" in result
    assert '"date_mod"' in result


def test_postgresql_build_initial_days_filter_unix():
    adapter = PostgreSQLAdapter()
    result = adapter.build_initial_days_filter("clock", 90, "unix_timestamp")

    assert "EXTRACT(EPOCH" in result
    assert '"clock"' in result


def test_mysql_build_initial_days_filter_datetime():
    adapter = MySQLAdapter()
    result = adapter.build_initial_days_filter("date_mod", 90, "datetime")

    assert "INTERVAL 90 DAY" in result
    assert "`date_mod`" in result


def test_is_large_table_postgresql_events():
    adapter = PostgreSQLAdapter()

    assert adapter.is_large_table("events") is True
    assert adapter.is_large_table("hosts") is False


def test_normalize_clock_value():
    value = 1719270000
    assert MySQLAdapter().normalize_clock_value(value, "integer") == value
    converted = PostgreSQLAdapter().normalize_clock_value(value, "integer")
    assert converted == datetime.fromtimestamp(value, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    assert PostgreSQLAdapter().normalize_clock_value(now, "datetime") == now


def test_postgresql_incremental_query_datetime_and_clock():
    config = SimpleNamespace(schema_name="public")
    adapter = PostgreSQLAdapter()
    since = datetime(2024, 6, 25, 12, 0, tzinfo=timezone.utc)

    sql, params = adapter.get_incremental_query(config, "hosts", "updated_at", since)
    assert 'WHERE "updated_at" >= :since' in sql
    assert params["since"] == since - timedelta(hours=2)

    sql, params = adapter.get_incremental_query(config, "history", "clock", since)
    assert 'WHERE "clock" >= :since_epoch' in sql
    assert isinstance(params["since_epoch"], int)
    assert params["since_epoch"] == int(since.timestamp()) - 7200


def test_postgresql_incremental_query_numeric_watermark():
    config = SimpleNamespace(schema_name="public")
    adapter = PostgreSQLAdapter()
    adapter._numeric_columns = {"id"}

    sql, params = adapter.get_incremental_query(
        config,
        "journal_details",
        "id",
        123,
    )

    assert 'WHERE "id" >= :since_value' in sql
    assert params["since_value"] == 123


def test_large_table_without_initial_days_is_skipped(db_session, monkeypatch):
    config = make_config(initial_days=None)
    table = ConnectorSyncTable(connector_type="zabbix", table_name="history", is_active=True, load_type="full")
    db_session.add_all([config, table])
    db_session.commit()
    config.initial_days = None
    db_session.commit()

    monkeypatch.setattr(glpi_import, "ensure_target_database", lambda config, local_engine=None: (True, None))
    monkeypatch.setattr(glpi_import, "build_source_engine", lambda config: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(glpi_import, "get_table_columns", lambda engine, config, table_name, adapter: ["id", "clock"])

    run = glpi_import.run_table_import(db_session, table, "full")

    assert run.status == "skipped"
    assert "Tabela volumosa requer janela inicial" in run.error_message


def test_large_table_full_uses_initial_days_window(db_session, monkeypatch):
    config = make_config(initial_days=90)
    table = ConnectorSyncTable(connector_type="zabbix", table_name="history", is_active=True, load_type="full")
    db_session.add_all([config, table])
    db_session.commit()
    captured = {}

    class FakeResult:
        def mappings(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execution_options(self, **kwargs):
            return self

        def execute(self, sql, params=None):
            statement = str(sql)
            if "information_schema.columns" in statement:
                return SimpleNamespace(first=lambda: ("integer",))
            captured["sql"] = statement
            captured["params"] = params or {}
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

        def dispose(self):
            return None

    monkeypatch.setattr(glpi_import, "ensure_target_database", lambda config, local_engine=None: (True, None))
    monkeypatch.setattr(glpi_import, "build_source_engine", lambda config: FakeEngine())
    monkeypatch.setattr(glpi_import, "get_table_columns", lambda engine, config, table_name, adapter: ["id", "clock"])
    monkeypatch.setattr(
        PostgreSQLAdapter,
        "get_column_info",
        lambda self, connection, schema, table: [
            {"column_name": "id", "pg_type": "bigint", "mariadb_type": "BIGINT", "is_unix_timestamp": False},
            {"column_name": "clock", "pg_type": "integer", "mariadb_type": "DATETIME", "is_unix_timestamp": True},
        ],
    )
    monkeypatch.setattr(glpi_import, "ensure_postgresql_import_table", lambda database, table, columns: True)
    monkeypatch.setattr(
        glpi_import,
        "insert_postgresql_rows",
        lambda target_database, table_name, rows, columns, include_row_hash=True: len(rows),
    )
    monkeypatch.setattr(glpi_import, "auto_create_connector_indexes", lambda database, table: [])

    run = glpi_import.run_table_import(db_session, table, "full")

    assert run.status == "success"
    assert captured["params"] == {}
    assert "EXTRACT(EPOCH FROM NOW() - INTERVAL '90 days')" in captured["sql"]
    assert 'WHERE "clock" >=' in captured["sql"]
    assert table.configured_load_type == "full"
    assert table.effective_load_type == "incremental"
    assert table.effective_incremental_column == "clock"


class FakeBegin:
    def __init__(self, connection):
        self.connection = connection

    def execution_options(self, **kwargs):
        self.connection.execution_options(**kwargs)
        return self

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeLocalEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return FakeBegin(self.connection)

    def connect(self):
        return FakeBegin(self.connection)


class FakeDatabaseConnection:
    def __init__(self, exists=False, fail=False):
        self.exists = exists
        self.fail = fail
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        if "information_schema.SCHEMATA" in str(statement):
            return SimpleNamespace(first=lambda: ("zabbix_local",) if self.exists else None)
        if self.fail:
            raise SQLAlchemyError("permission denied")
        return SimpleNamespace(first=lambda: None)

    def execution_options(self, **kwargs):
        self.statements.append(f"execution_options={kwargs}")
        return self


def test_ensure_target_database_already_exists():
    connection = FakeDatabaseConnection(exists=True)
    ok, sql = glpi_import.ensure_target_database(make_config(), FakeLocalEngine(connection))

    assert ok is True
    assert sql is None


def test_ensure_target_database_creates_when_allowed():
    connection = FakeDatabaseConnection(exists=False)
    ok, sql = glpi_import.ensure_target_database(make_config(), FakeLocalEngine(connection))

    assert ok is True
    assert sql is None
    assert any("CREATE DATABASE IF NOT EXISTS `zabbix_local`" in stmt for stmt in connection.statements)
    assert any("GRANT ALL PRIVILEGES ON `zabbix_local`.*" in stmt for stmt in connection.statements)


def test_ensure_target_database_returns_operational_message_without_permission():
    connection = FakeDatabaseConnection(exists=False, fail=True)
    ok, message = glpi_import.ensure_target_database(make_config(), FakeLocalEngine(connection))

    assert ok is False
    assert "Database criada. Erro ao configurar permissoes" in message
    assert "Contate o administrador do sistema" in message
