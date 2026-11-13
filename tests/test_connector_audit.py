from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

from app.models import AdminActionLog, ConnectorConfig, ConnectorRun, SystemConfig
from app.routes import history, imports


def make_config(**overrides) -> ConnectorConfig:
    values = {
        "name": "Conector Teste",
        "connector_type": "connector_test",
        "db_type": "mysql",
        "host": "localhost",
        "port": "3306",
        "database_name": "connector_source",
        "target_database": "connector_local",
        "table_prefix": "",
        "username": "user",
        "password": "pass",
        "suggested_frequency": "08:00",
        "full_schedule_horarios": "02:00",
        "full_schedule_dias": "0",
        "is_active": True,
        "import_mode": "automatic",
    }
    values.update(overrides)
    return ConnectorConfig(**values)


def test_manual_background_records_connector_mode_origin_and_exact_counts(db_session, monkeypatch):
    config = make_config()
    db_session.add(config)
    db_session.commit()
    imported = []

    def fake_import(db, mode, table_id, connector_type):
        imported.append((mode, table_id, connector_type))
        runs = [
            ConnectorRun(
                connector_type=connector_type,
                table_name="table_a",
                mode=mode,
                status="success",
                row_count=10,
            ),
            ConnectorRun(
                connector_type=connector_type,
                table_name="table_b",
                mode=mode,
                status="success",
                row_count=20,
            ),
            ConnectorRun(
                connector_type=connector_type,
                table_name="table_c",
                mode=mode,
                status="skipped",
                row_count=0,
            ),
        ]
        db.add_all(runs)
        db.commit()
        return runs

    test_lock = threading.Lock()
    monkeypatch.setattr(imports, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(imports, "execute_connector_import", fake_import)
    monkeypatch.setattr(imports, "import_lock", test_lock)

    imports.run_connector_import_background(
        "full",
        username="admin",
        user_id=None,
        connector_type="connector_test",
    )

    log = db_session.query(AdminActionLog).filter_by(action="connector_run_finish").one()
    assert imported == [("full", None, "connector_test")]
    assert log.table_name == "Conector Teste"
    assert "tipo: connector_test" in log.message
    assert "modo: full" in log.message
    assert "origem: manual" in log.message
    assert "tabelas ok: 2" in log.message
    assert "com erro: 0" in log.message
    assert "puladas: 1" in log.message
    assert "linhas importadas: 30" in log.message
    assert test_lock.acquire(blocking=False) is True
    test_lock.release()


def test_audit_helper_failure_does_not_interrupt_manual_import(db_session, monkeypatch):
    imported = []
    test_lock = threading.Lock()
    monkeypatch.setattr(imports, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(imports, "import_lock", test_lock)
    monkeypatch.setattr(
        imports,
        "execute_connector_import",
        lambda *args: imported.append("completed") or [],
    )
    monkeypatch.setattr(
        imports,
        "record_connector_import_admin_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    imports.run_connector_import_background("incremental", connector_type="connector_test")

    assert imported == ["completed"]
    assert test_lock.acquire(blocking=False) is True
    test_lock.release()


def test_history_labels_new_and_legacy_connector_actions(db_session):
    db_session.add_all(
        [
            AdminActionLog(
                username="admin",
                action="connector_run_start",
                status="success",
                message="start",
                table_name="Conector Alfa",
            ),
            AdminActionLog(
                username="admin",
                action="connector_run_finish",
                status="success",
                message="finish",
                table_name="Conector Alfa",
            ),
            AdminActionLog(
                username="admin",
                action="connector_run_finish",
                status="error",
                message="failed",
                table_name="Conector Beta",
            ),
            AdminActionLog(
                username="admin",
                action="glpi_import_full",
                status="success",
                message="legacy",
            ),
            AdminActionLog(
                username="admin",
                action="glpi_import_incremental",
                status="success",
                message="legacy",
            ),
        ]
    )
    db_session.commit()

    labels = [item["acao"] for item in history.build_audit_log_query(db_session)]

    assert "Importação de conector: início — Conector Alfa" in labels
    assert "Importação de conector: concluída — Conector Alfa" in labels
    assert "Importação de conector: falha — Conector Beta" in labels
    assert labels.count("Importação de conector (legado)") == 2
    assert all(item["categoria"] == "Sistema" for item in history.build_audit_log_query(db_session))


def test_save_schedule_logs_before_after_and_preserves_persisted_values(db_session, monkeypatch):
    config = make_config()
    db_session.add(config)
    db_session.add_all(
        [
            SystemConfig(key=imports.GLPI_SCHEDULE_ACTIVE_KEY, value="true"),
            SystemConfig(key=imports.GLPI_SCHEDULE_DAYS_KEY, value="1,2"),
            SystemConfig(key=imports.GLPI_FULL_SCHEDULE_ACTIVE_KEY, value="false"),
        ]
    )
    db_session.commit()
    user = SimpleNamespace(id=None, username="admin")
    payload = {
        "connector_id": [str(config.id)],
        "suggested_frequency": ["09:00,14:00"],
        "glpi_schedule_active": ["on"],
        "schedule_dias": ["1", "3", "5"],
        "full_schedule_active": ["on"],
        "full_schedule_horarios": ["03:00"],
        "full_schedule_dias": ["0", "6"],
    }

    async def fake_form_lists(request):
        return payload

    reload_calls = []
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(reload_glpi_import_schedule=lambda: reload_calls.append(True))
        )
    )
    monkeypatch.setattr(imports, "require_importacao", lambda request, db: user)
    monkeypatch.setattr(imports, "form_lists", fake_form_lists)

    asyncio.run(imports.save_glpi_import_schedule(request, db_session))

    db_session.refresh(config)
    assert config.suggested_frequency == "09:00,14:00"
    assert config.full_schedule_horarios == "03:00"
    assert config.full_schedule_dias == "0,6"
    assert db_session.get(SystemConfig, imports.GLPI_SCHEDULE_ACTIVE_KEY).value == "true"
    assert db_session.get(SystemConfig, imports.GLPI_SCHEDULE_DAYS_KEY).value == "1,3,5"
    assert db_session.get(SystemConfig, imports.GLPI_FULL_SCHEDULE_ACTIVE_KEY).value == "true"
    assert reload_calls == [True]

    log = db_session.query(AdminActionLog).filter_by(action="glpi_schedule_save").one()
    assert "Conector: Conector Teste" in log.message
    assert "incremental: horários 08:00 -> 09:00,14:00" in log.message
    assert "dias 1,2 -> 1,3,5" in log.message
    assert "ativo true -> true" in log.message
    assert "FULL: horários 02:00 -> 03:00" in log.message
    assert "dias 0 -> 0,6" in log.message
    assert "ativo false -> true" in log.message
