import csv
import io
from datetime import datetime, timedelta
from pathlib import Path

from starlette.requests import Request

from app.models import AdminActionLog, AuthLog, Dashboard, Report, ReportExecution, SystemConfig
from app.routes import dashboard as dashboard_routes
from app.routes import history, reports


def _request(query_string: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/audit-log/export",
            "query_string": query_string.encode(),
            "headers": [],
        }
    )


def test_filtered_logs_respect_retention_and_category(db_session):
    db_session.add(SystemConfig(key="log_retention_days", value="90"))
    db_session.add_all(
        [
            AdminActionLog(
                action="appearance_update",
                status="success",
                username="admin",
                message="recente",
                created_at=datetime.utcnow() - timedelta(days=2),
            ),
            AdminActionLog(
                action="appearance_update",
                status="success",
                username="admin",
                message="expirado",
                created_at=datetime.utcnow() - timedelta(days=91),
            ),
            AdminActionLog(
                action="duplicate_report",
                status="success",
                username="admin",
                message="outra categoria",
                created_at=datetime.utcnow() - timedelta(days=1),
            ),
        ]
    )
    db_session.commit()

    logs = history.build_audit_log_query(
        db_session, category="Sistema", retention_days=90
    )

    assert [log["detalhe"] for log in logs] == ["recente"]


def test_export_audit_log_csv_headers_filters_and_filename(db_session, monkeypatch):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    db_session.add(
        AdminActionLog(
            action="appearance_update",
            status="success",
            username="admin",
            message="alteração, com vírgula; e ponto e vírgula",
            created_at=datetime.utcnow(),
        )
    )
    db_session.commit()
    monkeypatch.setattr(history, "require_admin", lambda request, db: object())

    response = history.export_audit_log(
        _request(f"category=Sistema&user=admin&status=success&date_from={today}&date_to={today}"),
        db_session,
    )

    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="auditoria_{today}_{today}.csv"'
    )
    csv_content = response.body.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(csv_content), delimiter=";"))
    assert rows[0] == ["Data/Hora", "Categoria", "Usuário", "Ação", "Detalhe", "Status", "Duração (ms)"]
    assert rows[1][1:] == [
        "Sistema",
        "admin",
        "Personalizacao atualizada",
        "alteração, com vírgula; e ponto e vírgula",
        "success",
        "",
    ]
    assert '"alteração, com vírgula; e ponto e vírgula"' in csv_content


def test_build_audit_log_query_consolidates_sources_duration_and_status(db_session):
    timestamp = datetime.utcnow() - timedelta(hours=1)
    db_session.add_all(
        [
            ReportExecution(
                sql_query="SELECT 1",
                destination_table="resultado",
                status="success",
                duration_ms=321,
                executed_at=timestamp,
            ),
            AdminActionLog(
                action="delete_dashboard",
                status="blocked",
                username="admin",
                created_at=timestamp,
            ),
            AuthLog(
                username="joan",
                auth_source="local",
                status="failure",
                created_at=timestamp,
            ),
        ]
    )
    db_session.commit()

    logs = history.build_audit_log_query(db_session, retention_days=90)
    by_category = {log["categoria"]: log for log in logs}

    assert {"Execução", "Exclusão", "Autenticação"} <= set(by_category)
    assert by_category["Execução"]["duracao_ms"] == 321
    assert by_category["Execução"]["status"] == "success"
    assert by_category["Exclusão"]["duracao_ms"] is None
    assert by_category["Exclusão"]["status"] == "warning"
    assert by_category["Autenticação"]["status"] == "error"


def test_delete_report_logs_exclusion_action(db_session, monkeypatch, admin_local_user):
    report = Report(name="Temporario", sql_query="SELECT 1", destination_table="tmp_report")
    db_session.add(report)
    db_session.commit()
    report_id = report.id
    monkeypatch.setattr(reports, "require_relatorios", lambda request, db: admin_local_user)

    response = reports.delete_report(report_id, _request(), db_session)

    assert response.status_code == 303
    assert db_session.get(Report, report_id) is None
    log = db_session.query(AdminActionLog).filter_by(action="report_delete").one()
    assert history.admin_category(log.action) == "Exclusão"


def test_delete_dashboard_logs_exclusion_action(db_session, monkeypatch, admin_local_user):
    dashboard = Dashboard(name="Temporario", is_active=True)
    remaining = Dashboard(name="Principal", is_active=True)
    db_session.add_all([dashboard, remaining])
    db_session.commit()
    dashboard_id = dashboard.id
    monkeypatch.setattr(dashboard_routes, "require_dashboard", lambda request, db: admin_local_user)

    response = dashboard_routes.delete_admin_dashboard(dashboard_id, _request(), db_session)

    assert response.status_code == 303
    assert db_session.get(Dashboard, dashboard_id) is None
    log = db_session.query(AdminActionLog).filter_by(action="dashboard_delete").one()
    assert history.admin_category(log.action) == "Exclusão"


def test_exclusion_category_is_available_in_audit_filter():
    template = Path("templates/history.html").read_text(encoding="utf-8")

    assert template.count("'Exclusão'") == 2
