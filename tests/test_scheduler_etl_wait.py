from datetime import datetime, timedelta
import threading

from app import main
from app.models import AdminActionLog, ConnectorRun, Report, SystemConfig


def set_config(db_session, key, value):
    db_session.add(SystemConfig(key=key, value=value))
    db_session.commit()


def add_running_etl(db_session, started_at):
    run = ConnectorRun(
        connector_type="glpi",
        table_name="glpi_computers",
        mode="incremental",
        origin="scheduler",
        status="running",
        started_at=started_at,
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_is_etl_running_false_without_running_run(db_session):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "30")

    assert main.is_etl_running(db_session) is False


def test_is_etl_running_true_with_recent_running_run(db_session):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "30")
    add_running_etl(db_session, datetime.utcnow())

    assert main.is_etl_running(db_session) is True


def test_is_etl_running_false_with_old_running_run(db_session):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "30")
    add_running_etl(db_session, datetime.utcnow() - timedelta(minutes=31))

    assert main.is_etl_running(db_session) is False


def test_is_etl_running_disabled_by_zero_config(db_session):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "0")
    add_running_etl(db_session, datetime.utcnow())

    assert main.is_etl_running(db_session) is False


def test_wait_for_etl_returns_true_without_waiting(db_session, monkeypatch):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "30")
    slept = []
    monkeypatch.setattr(main.time, "sleep", lambda seconds: slept.append(seconds))

    assert main.wait_for_etl(db_session) is True
    assert slept == []


def test_wait_for_etl_returns_true_when_etl_finishes_during_polling(db_session, monkeypatch):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "30")
    run = add_running_etl(db_session, datetime.utcnow())

    def finish_etl(seconds):
        run.status = "success"
        run.finished_at = datetime.utcnow()
        db_session.flush()

    monkeypatch.setattr(main.time, "sleep", finish_etl)

    assert main.wait_for_etl(db_session) is True
    action = db_session.query(AdminActionLog).filter(AdminActionLog.action == "reports_waited_for_etl").one()
    assert "30s" in action.message


def test_wait_for_etl_returns_false_after_timeout(db_session, monkeypatch):
    set_config(db_session, main.ETL_WAIT_BEFORE_REPORTS_KEY, "1")
    add_running_etl(db_session, datetime(2026, 1, 1, 0, 1, 0))

    class FakeDateTime:
        current = datetime(2026, 1, 1, 0, 0, 0)

        @classmethod
        def utcnow(cls):
            cls.current += timedelta(seconds=31)
            return cls.current

    monkeypatch.setattr(main, "datetime", FakeDateTime)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    assert main.wait_for_etl(db_session) is False
    action = db_session.query(AdminActionLog).filter(AdminActionLog.action == "reports_etl_timeout").one()
    assert action.status == "warning"


def test_run_all_scheduled_reports_waits_before_executing(db_session, monkeypatch):
    set_config(db_session, main.REPORT_SCHEDULE_ACTIVE_KEY, "true")
    report = Report(
        name="Relatorio agendado",
        sql_query="SELECT 1",
        destination_table="relatorio_agendado",
        is_active=True,
        is_primary=True,
    )
    db_session.add(report)
    db_session.commit()

    calls = []
    monkeypatch.setattr(main, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(main.app.state, "report_global_scheduler_lock", threading.Lock())
    monkeypatch.setattr(main, "wait_for_etl", lambda db: calls.append("wait") or True)
    monkeypatch.setattr(main, "run_select", lambda *args, **kwargs: calls.append("run"))

    main.run_all_scheduled_reports()

    assert calls == ["wait", "run"]
