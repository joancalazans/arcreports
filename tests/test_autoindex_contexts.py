from types import SimpleNamespace

from app import glpi_import, main, reporting
from app.models import ConnectorConfig, ConnectorRun, ConnectorSyncTable


class Rows:
    def __init__(self, values):
        self.values = values

    def __iter__(self):
        return iter(self.values)


class ConnectorIndexConnection:
    def __init__(self):
        self.created = []
        self.queries = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.queries.append(sql)
        if "information_schema.COLUMNS" in sql:
            return Rows([("severity", "int"), ("description", "varchar(100)")])
        if "information_schema.STATISTICS" in sql:
            return Rows([])
        if sql.startswith("ALTER TABLE"):
            self.created.append(sql)
            return Rows([])
        raise AssertionError(f"SQL inesperado: {sql}")

    def commit(self):
        return None


class ConnectContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


def test_ensure_connector_indexes_no_threshold(monkeypatch):
    """Conector indexa tabelas pequenas sem threshold."""
    connection = ConnectorIndexConnection()
    monkeypatch.setattr(glpi_import.local_engine, "connect", lambda: ConnectContext(connection))

    result = glpi_import.ensure_connector_indexes("zabbix_local", ["events"])

    assert result == {"events": ["idx_severity"]}
    assert len(connection.created) == 1
    assert not any("system_config" in query or "TABLE_ROWS" in query for query in connection.queries)


def test_severity_in_join_index_re():
    assert glpi_import.should_auto_index("severity") is True


def test_pre_full_index_check(db_session, monkeypatch):
    """Pré-FULL verifica e cria índices faltantes antes da importação."""
    config = ConnectorConfig(
        name="Zabbix", connector_type="zabbix", db_type="mysql", host="localhost",
        port="3306", database_name="zabbix", target_database="zabbix_local",
        username="user", password="pass", is_active=True,
    )
    table = ConnectorSyncTable(connector_type="zabbix", table_name="events", is_active=True)
    db_session.add_all([config, table])
    db_session.commit()
    calls = []
    monkeypatch.setattr(glpi_import, "ensure_target_database", lambda config, local_engine=None: (True, None))
    monkeypatch.setattr(glpi_import, "discover_source_tables", lambda db, config=None: {"events"})
    monkeypatch.setattr(glpi_import, "get_existing_tables", lambda database: ["hosts"])
    monkeypatch.setattr(
        glpi_import,
        "ensure_connector_indexes",
        lambda database, tables=None: calls.append((database, tables)) or {},
    )
    monkeypatch.setattr(
        glpi_import,
        "run_table_import",
        lambda db, sync_table, mode: ConnectorRun(
            connector_type="zabbix", table_name=sync_table.table_name, mode=mode, status="success"
        ),
    )

    glpi_import.run_connector_import(db_session, "full", connector_type="zabbix")

    assert calls == [("zabbix_local", ["hosts"])]


def test_daily_index_uses_threshold(monkeypatch):
    """Job diário respeita threshold configurado."""
    connection = SimpleNamespace()
    monkeypatch.setattr(reporting, "auto_index_threshold_rows", lambda conn: 1000)
    monkeypatch.setattr(reporting, "table_row_estimate", lambda conn, table: 999)
    monkeypatch.setattr(reporting.local_engine, "begin", lambda: ConnectContext(connection))

    assert reporting.ensure_indexes_if_needed("relatorio_pequeno") == []


def test_threshold_default_1000():
    """Valor padrão do threshold é 1.000."""
    assert reporting.DEFAULT_AUTO_INDEX_THRESHOLD_ROWS == 1000
    assert main.DEFAULT_AUTO_INDEX_THRESHOLD_ROWS == "1000"
