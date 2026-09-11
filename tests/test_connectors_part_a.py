from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import glpi_import
from app.models import AdminActionLog, ConnectorConfig, ConnectorRun, ConnectorSyncTable, GlpiImportLog
from app.routes import imports


def make_config(**overrides) -> ConnectorConfig:
    values = {
        "name": "Fonte Teste",
        "connector_type": "zabbix",
        "db_type": "mysql",
        "host": "localhost",
        "port": "3306",
        "database_name": "zabbix",
        "target_database": "zabbix_local",
        "table_prefix": "",
        "username": "user",
        "password": "pass",
        "suggested_frequency": "08:00",
        "is_active": True,
        "import_mode": "automatic",
        "table_whitelist": None,
    }
    values.update(overrides)
    return ConnectorConfig(**values)


def test_connector_initial_days_defaults_to_none(db_session):
    config = make_config()
    db_session.add(config)
    db_session.commit()

    assert config.initial_days is None


def test_apply_connector_form_accepts_empty_and_positive_initial_days():
    config = make_config(initial_days=90)

    imports.apply_connector_form(config, {"initial_days": [""]})
    assert config.initial_days is None

    imports.apply_connector_form(config, {"initial_days": ["45"]})
    assert config.initial_days == 45


class FakeAdapter:
    def __init__(self, tables):
        self.tables = tables

    def get_tables(self, connection, prefix, schema):
        return self.tables

    def get_incremental_candidates(self):
        return ["updated_at"]

    def is_large_table(self, table_name):
        return table_name == "history"


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeEngine:
    def connect(self):
        return FakeConnection()

    def dispose(self):
        return None


def test_discover_source_tables_automatic_keeps_all_tables(db_session, monkeypatch):
    config = make_config(import_mode="automatic")
    db_session.add(config)
    db_session.commit()

    monkeypatch.setattr(glpi_import, "build_source_engine", lambda config: FakeEngine())
    monkeypatch.setattr(glpi_import, "get_adapter", lambda db_type: FakeAdapter(["hosts", "items"]))

    discovered = glpi_import.discover_source_tables(db_session, config)

    assert discovered == {"hosts", "items"}
    assert db_session.query(ConnectorSyncTable).count() == 2
    rows = db_session.query(ConnectorSyncTable).order_by(ConnectorSyncTable.table_name).all()
    assert [(row.table_name, row.load_type, row.incremental_column) for row in rows] == [
        ("hosts", "full", None),
        ("items", "full", None),
    ]
    assert all(row.configured_load_type == "full" for row in rows)


def test_new_table_uses_profile_not_hardcode_incremental(db_session, monkeypatch):
    config = make_config(import_mode="automatic")
    db_session.add(config)
    db_session.commit()

    monkeypatch.setattr(glpi_import, "build_source_engine", lambda config: FakeEngine())
    monkeypatch.setattr(
        glpi_import,
        "get_adapter",
        lambda db_type: FakeAdapter(["events", "services"]),
    )

    glpi_import.discover_source_tables(db_session, config)

    rows = {
        row.table_name: row
        for row in db_session.query(ConnectorSyncTable).all()
    }
    assert rows["events"].load_type == "incremental"
    assert rows["events"].incremental_column == "clock"
    assert rows["services"].load_type == "full"
    assert rows["services"].incremental_column is None


def test_normalize_db_type_rejects_unknown_type():
    with pytest.raises(ValueError, match="Tipo de banco inválido"):
        imports.normalize_db_type("oracle")


def test_discover_source_tables_custom_filters_whitelist(db_session, monkeypatch):
    config = make_config(import_mode="custom", table_whitelist='["hosts"]')
    db_session.add(config)
    db_session.commit()

    monkeypatch.setattr(glpi_import, "build_source_engine", lambda config: FakeEngine())
    monkeypatch.setattr(glpi_import, "get_adapter", lambda db_type: FakeAdapter(["hosts", "items"]))

    discovered = glpi_import.discover_source_tables(db_session, config)

    assert discovered == {"hosts"}
    assert [row.table_name for row in db_session.query(ConnectorSyncTable).all()] == ["hosts"]


def test_discover_source_tables_custom_empty_whitelist_returns_empty(db_session, monkeypatch, caplog):
    config = make_config(import_mode="custom", table_whitelist=None)
    db_session.add(config)
    db_session.commit()

    monkeypatch.setattr(glpi_import, "build_source_engine", lambda config: FakeEngine())
    monkeypatch.setattr(glpi_import, "get_adapter", lambda db_type: FakeAdapter(["hosts", "items"]))

    with caplog.at_level("WARNING"):
        discovered = glpi_import.discover_source_tables(db_session, config)

    assert discovered == set()
    assert "modo custom sem whitelist configurada" in caplog.text


def test_run_connector_import_custom_skips_tables_outside_whitelist(db_session, monkeypatch):
    db_session.add(make_config(import_mode="custom", table_whitelist='["hosts"]'))
    db_session.add_all(
        [
            ConnectorSyncTable(connector_type="zabbix", table_name="hosts", is_active=True),
            ConnectorSyncTable(connector_type="zabbix", table_name="items", is_active=True),
        ]
    )
    db_session.commit()
    imported = []

    monkeypatch.setattr(glpi_import, "ensure_target_database", lambda config, local_engine=None: (True, None))
    monkeypatch.setattr(glpi_import, "discover_source_tables", lambda db, config=None: {"hosts"})

    def fake_run_table_import(db, sync_table, mode):
        imported.append(sync_table.table_name)
        return ConnectorRun(
            connector_type=sync_table.connector_type,
            table_name=sync_table.table_name,
            mode=mode,
            status="success",
        )

    monkeypatch.setattr(glpi_import, "run_table_import", fake_run_table_import)

    glpi_import.run_connector_import(db_session, "incremental", connector_type="zabbix")

    assert imported == ["hosts"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SQLAlchemyError("Connection refused"), "Nao foi possivel conectar ao host"),
        (SQLAlchemyError("Access denied"), "Acesso negado"),
        (SQLAlchemyError("Unknown database"), "nao encontrada"),
        (SQLAlchemyError("timeout"), "Conexao expirou"),
    ],
)
def test_translate_connection_error(error, expected):
    assert expected in glpi_import.translate_connection_error(error, "db.local", "3306", "origem")


class FakeJsonRequest:
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeConnectorsRequest:
    def __init__(self, query_params=None):
        self.query_params = query_params or {}
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                scheduler_status=lambda: {"active": True, "times": ["08:00"]},
            )
        )


def run_delete_connector(db_session, monkeypatch, config):
    user = SimpleNamespace(id=1, username="admin")

    async def empty_form(_request):
        return {}

    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)
    monkeypatch.setattr(imports, "form_lists", empty_form)
    monkeypatch.setattr(imports, "connector_database_exists", lambda db, target_database: False)
    monkeypatch.setattr(imports, "reset_dashboard_sources_cache", lambda: None)
    return asyncio.run(imports.delete_connector(config.id, SimpleNamespace(), db_session))


def test_delete_connector_with_import_logs(db_session, monkeypatch):
    """Exclusão com logs vinculados deve funcionar."""
    config = make_config(is_active=False)
    sync_table = ConnectorSyncTable(connector_type="zabbix", table_name="hosts", is_active=True)
    db_session.add_all([config, sync_table])
    db_session.flush()
    run = ConnectorRun(
        connector_type="zabbix",
        sync_table_id=sync_table.id,
        table_name="hosts",
        mode="full",
        status="success",
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(GlpiImportLog(run_id=run.id, level="info", message="Importação concluída"))
    db_session.commit()

    response = run_delete_connector(db_session, monkeypatch, config)

    assert response.status_code == 303
    assert "Conector%20Fonte%20Teste%20exclu%C3%ADdo." in response.headers["location"]
    assert db_session.query(GlpiImportLog).count() == 0
    assert db_session.query(ConnectorRun).count() == 0
    assert db_session.query(ConnectorSyncTable).count() == 0
    assert db_session.get(ConnectorConfig, config.id) is None


def test_delete_connector_no_refs(db_session, monkeypatch):
    """Exclusão sem referências deve redirecionar com mensagem de sucesso."""
    config = make_config(is_active=False)
    db_session.add(config)
    db_session.commit()

    response = run_delete_connector(db_session, monkeypatch, config)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/connectors?message=")
    assert db_session.get(ConnectorConfig, config.id) is None


def test_delete_connector_rollback_on_error(db_session, monkeypatch):
    """Erro de FK deve retornar mensagem amigável, não HTTP 500."""
    config = make_config(is_active=False)
    db_session.add(config)
    db_session.commit()
    original_commit = db_session.commit

    def failing_commit():
        raise SQLAlchemyError("foreign key constraint failed")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    response = run_delete_connector(db_session, monkeypatch, config)
    monkeypatch.setattr(db_session, "commit", original_commit)

    assert response.status_code == 303
    assert "Erro%20ao%20excluir%20conector" in response.headers["location"]
    assert db_session.get(ConnectorConfig, config.id) is not None


def test_discover_endpoint_returns_metadata_without_changing_sync_tables(db_session, monkeypatch):
    config = make_config(import_mode="custom", table_whitelist='["hosts"]')
    db_session.add_all([config, ConnectorSyncTable(connector_type="zabbix", table_name="existing", is_active=True)])
    db_session.commit()
    user = SimpleNamespace(id=1, username="admin")

    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)
    monkeypatch.setattr(imports, "build_source_engine", lambda config: FakeEngine())
    monkeypatch.setattr(imports, "get_adapter", lambda db_type: FakeAdapter(["hosts", "history"]))
    monkeypatch.setattr(
        imports,
        "get_table_metadata",
        lambda adapter, connection, table_name, schema: {
            "rows": 10,
            "size_mb": 1.5,
            "is_large": adapter.is_large_table(table_name),
        },
    )

    response = asyncio.run(
        imports.discover_connector_tables(FakeJsonRequest({"connector_id": str(config.id)}), db_session)
    )
    payload = response.body.decode("utf-8")

    assert '"success":true' in payload
    assert '"name":"hosts"' in payload
    assert '"in_whitelist":true' in payload
    assert db_session.query(ConnectorSyncTable).count() == 1


def test_discover_endpoint_returns_friendly_error(db_session, monkeypatch):
    config = make_config()
    db_session.add(config)
    db_session.commit()
    user = SimpleNamespace(id=1, username="admin")

    class FailingEngine:
        def connect(self):
            raise SQLAlchemyError("Access denied")

        def dispose(self):
            return None

    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)
    monkeypatch.setattr(imports, "build_source_engine", lambda config: FailingEngine())

    response = asyncio.run(
        imports.discover_connector_tables(FakeJsonRequest({"connector_id": str(config.id)}), db_session)
    )

    assert "Acesso negado" in response.body.decode("utf-8")


def test_whitelist_endpoint_saves_mode_and_logs(db_session, monkeypatch):
    config = make_config()
    db_session.add(config)
    db_session.commit()
    user = SimpleNamespace(id=1, username="admin")

    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)

    response = asyncio.run(
        imports.save_connector_whitelist(
            config.id,
            FakeJsonRequest({"import_mode": "custom", "tables": ["hosts", "items"]}),
            db_session,
        )
    )
    db_session.refresh(config)
    log = db_session.query(AdminActionLog).filter_by(action="connector_mode_change").one()

    assert response.body.decode("utf-8") == '{"success":true,"import_mode":"custom","table_count":2}'
    assert config.import_mode == "custom"
    assert config.table_whitelist == '["hosts", "items"]'
    assert "2 tabelas na whitelist" in log.message


def test_whitelist_endpoint_logs_each_removed_table(db_session, monkeypatch):
    config = make_config(import_mode="custom", table_whitelist='["hosts", "items"]')
    db_session.add(config)
    db_session.commit()
    user = SimpleNamespace(id=1, username="admin")
    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)

    asyncio.run(
        imports.save_connector_whitelist(
            config.id,
            FakeJsonRequest({"import_mode": "custom", "tables": ["hosts"]}),
            db_session,
        )
    )

    log = db_session.query(AdminActionLog).filter_by(action="connector_table_remove").one()
    assert log.table_name == "items"
    assert "Fonte Teste" in log.message


def test_connectors_page_paginates_sync_tables(db_session, monkeypatch):
    config = make_config(connector_type="pagetest")
    db_session.add(config)
    db_session.flush()
    db_session.add_all(
        [
            ConnectorSyncTable(connector_type="pagetest", table_name=f"pagetest_table_{index:03d}", is_active=True)
            for index in range(1, 121)
        ]
    )
    db_session.commit()
    captured = {}
    user = SimpleNamespace(id=1, username="admin")

    def fake_render(request, template, context, db):
        captured.update(context)
        return context

    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)
    monkeypatch.setattr(imports, "render", fake_render)

    imports.connectors_page(FakeConnectorsRequest({"connector_id": str(config.id)}), db_session)

    assert captured["page"] == 1
    assert len(captured["tables"]) == 50
    assert captured["total_tables"] == 120
    assert captured["total_pages"] == 3
    assert captured["tables"][0].table_name == "pagetest_table_001"
    assert captured["tables"][-1].table_name == "pagetest_table_050"

    imports.connectors_page(FakeConnectorsRequest({"connector_id": str(config.id), "page": "2"}), db_session)

    assert captured["page"] == 2
    assert len(captured["tables"]) == 50
    assert captured["tables"][0].table_name == "pagetest_table_051"
    assert captured["tables"][-1].table_name == "pagetest_table_100"

    imports.connectors_page(FakeConnectorsRequest({"connector_id": str(config.id), "page": "999"}), db_session)

    assert captured["page"] == 999
    assert captured["total_pages"] == 3
    assert captured["tables"] == []


def test_save_connector_tables_updates_only_submitted_page_subset(db_session, monkeypatch):
    config = make_config(connector_type="glpi")
    hidden_before = ConnectorSyncTable(
        connector_type="glpi",
        table_name="glpi_hidden_before",
        is_active=True,
        load_type="incremental",
    )
    visible = ConnectorSyncTable(
        connector_type="glpi",
        table_name="glpi_visible",
        is_active=True,
        load_type="incremental",
        incremental_column="date_mod",
    )
    hidden_after = ConnectorSyncTable(
        connector_type="glpi",
        table_name="glpi_hidden_after",
        is_active=True,
        load_type="incremental",
    )
    db_session.add_all([config, hidden_before, visible, hidden_after])
    db_session.commit()
    user = SimpleNamespace(id=1, username="admin")

    async def fake_form_data(request):
        return {
            "connector_id": str(config.id),
            f"load_type_{visible.id}": "full",
            f"incremental_column_{visible.id}": "",
        }

    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)
    monkeypatch.setattr(imports, "form_data", fake_form_data)

    asyncio.run(imports.save_glpi_import_tables(SimpleNamespace(), db_session))

    db_session.refresh(hidden_before)
    db_session.refresh(visible)
    db_session.refresh(hidden_after)

    assert hidden_before.is_active is True
    assert hidden_before.load_type == "incremental"
    assert visible.is_active is False
    assert visible.load_type == "full"
    assert visible.incremental_column is None
    assert hidden_after.is_active is True
