from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

from app import main
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


class ScheduledDateTime:
    fixed = datetime(2026, 11, 13, 10, 0, 0)

    @classmethod
    def now(cls, timezone=None):
        return cls.fixed.replace(tzinfo=timezone) if timezone else cls.fixed

    @classmethod
    def utcnow(cls):
        return cls.fixed


def configure_scheduled_connectors(db_session, monkeypatch):
    full_config = make_config(
        name="Conector Alfa",
        connector_type="connector_alpha",
        suggested_frequency="",
        full_schedule_horarios="10:00",
        full_schedule_dias="5",
    )
    incremental_config = make_config(
        name="Conector Beta",
        connector_type="connector_beta",
        suggested_frequency="10:00",
        full_schedule_horarios=None,
        full_schedule_dias=None,
    )
    db_session.add_all(
        [
            full_config,
            incremental_config,
            SystemConfig(key=main.GLPI_SCHEDULE_ACTIVE_KEY, value="true"),
            SystemConfig(key=main.GLPI_SCHEDULE_DAYS_KEY, value="5"),
            SystemConfig(key=main.GLPI_FULL_SCHEDULE_ACTIVE_KEY, value="true"),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(main, "datetime", ScheduledDateTime)
    monkeypatch.setattr(main.app.state, "glpi_import_lock", threading.Lock())
    return full_config, incremental_config


def successful_scheduled_run(db, mode, connector_type):
    run = ConnectorRun(
        connector_type=connector_type,
        table_name=f"{connector_type}_table",
        mode=mode,
        status="success",
        row_count=7,
        started_at=ScheduledDateTime.utcnow(),
        finished_at=ScheduledDateTime.utcnow(),
    )
    db.add(run)
    db.commit()
    return [run]


def test_scheduled_dispatcher_logs_each_connector_with_own_mode_and_origin(db_session, monkeypatch):
    configure_scheduled_connectors(db_session, monkeypatch)
    calls = []

    def fake_import(db, mode, connector_type):
        calls.append((connector_type, mode))
        return successful_scheduled_run(db, mode, connector_type)

    monkeypatch.setattr(main, "run_connector_import", fake_import)

    main.run_scheduled_glpi_import()

    assert calls == [
        ("connector_alpha", "full"),
        ("connector_beta", "incremental"),
    ]
    logs = db_session.query(AdminActionLog).order_by(AdminActionLog.id).all()
    assert [(log.action, log.table_name) for log in logs] == [
        ("connector_run_start", "Conector Alfa"),
        ("connector_run_finish", "Conector Alfa"),
        ("connector_run_start", "Conector Beta"),
        ("connector_run_finish", "Conector Beta"),
    ]
    assert "modo: full" in logs[0].message
    assert "modo: full" in logs[1].message
    assert "modo: incremental" in logs[2].message
    assert "modo: incremental" in logs[3].message
    assert all("origem: agendada" in log.message for log in logs)
    assert {run.origin for run in db_session.query(ConnectorRun).all()} == {"scheduler"}


def test_scheduled_connector_failure_does_not_suppress_other_audits(db_session, monkeypatch):
    configure_scheduled_connectors(db_session, monkeypatch)
    calls = []

    def fake_import(db, mode, connector_type):
        calls.append(connector_type)
        if connector_type == "connector_alpha":
            raise RuntimeError("source unavailable")
        return successful_scheduled_run(db, mode, connector_type)

    monkeypatch.setattr(main, "run_connector_import", fake_import)

    main.run_scheduled_glpi_import()

    assert calls == ["connector_alpha", "connector_beta"]
    finish_logs = {
        log.table_name: log
        for log in db_session.query(AdminActionLog).filter_by(action="connector_run_finish").all()
    }
    assert set(finish_logs) == {"Conector Alfa", "Conector Beta"}
    assert finish_logs["Conector Alfa"].status == "error"
    assert "resultado: falha" in finish_logs["Conector Alfa"].message
    assert finish_logs["Conector Beta"].status == "success"


def test_scheduled_audit_exception_does_not_interrupt_connector_loop(db_session, monkeypatch, caplog):
    configure_scheduled_connectors(db_session, monkeypatch)
    calls = []
    original_helper = imports.record_connector_import_admin_log

    def fake_import(db, mode, connector_type):
        calls.append(connector_type)
        return successful_scheduled_run(db, mode, connector_type)

    def flaky_audit(*args, connector_type=None, **kwargs):
        if connector_type == "connector_alpha":
            raise RuntimeError("audit unavailable")
        return original_helper(*args, connector_type=connector_type, **kwargs)

    monkeypatch.setattr(main, "run_connector_import", fake_import)
    monkeypatch.setattr(imports, "record_connector_import_admin_log", flaky_audit)

    main.run_scheduled_glpi_import()

    assert calls == ["connector_alpha", "connector_beta"]
    assert db_session.query(AdminActionLog).filter_by(action="connector_run_start").count() == 2
    finish_logs = db_session.query(AdminActionLog).filter_by(action="connector_run_finish").all()
    assert [log.table_name for log in finish_logs] == ["Conector Beta"]
    assert "Auditoria de conclusao do conector connector_alpha falhou" in caplog.text
