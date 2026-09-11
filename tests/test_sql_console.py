import asyncio

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from app.models import ConnectorConfig, SystemConfig
from app.routes import sql_console as sql_console_routes
from app.routes.sql_console import (
    SqlConsoleError,
    is_sql_console_enabled,
    sql_console_page,
    split_first_statement,
    validate_sql_console_command,
)


def make_connector_config(**overrides) -> ConnectorConfig:
    values = {
        "name": "Zabbix",
        "connector_type": "zabbix",
        "db_type": "mysql",
        "host": "127.0.0.1",
        "port": "3306",
        "database_name": "zabbix",
        "target_database": "zabbix_local",
        "table_prefix": "",
        "username": "zabbix",
        "password": "secret",
        "is_active": True,
    }
    values.update(overrides)
    return ConnectorConfig(**values)


@pytest.mark.parametrize(
    "sql_query,expected_command,expected_table",
    [
        ("SELECT * FROM glpi_tickets", "SELECT", ""),
        ("DESCRIBE reports", "DESCRIBE", "reports"),
        ("SHOW TABLES", "SHOW", ""),
        ("SHOW COLUMNS FROM reports", "SHOW", ""),
        ("UPDATE reports SET campo_sql_periodo = 'created_at' WHERE id = 1", "UPDATE", "reports"),
        ("INSERT INTO dashboard_sources (source_table) VALUES ('relatorio_x')", "INSERT", "dashboard_sources"),
        ("ALTER TABLE relatorio_chamados ADD COLUMN created_at DATETIME NULL", "ALTER", "relatorio_chamados"),
    ],
)
def test_sql_console_allows_expected_commands(db_session, sql_query, expected_command, expected_table):
    assert validate_sql_console_command(sql_query, db_session) == (expected_command, expected_table)


@pytest.mark.parametrize(
    "sql_query,expected_message",
    [
        ("DROP TABLE reports", "DROP"),
        ("TRUNCATE TABLE reports", "TRUNCATE"),
        ("DELETE FROM reports WHERE id = 1", "DELETE"),
        ("UPDATE reports SET name = 'x'", "UPDATE sem WHERE pode afetar todos os registros."),
        ("UPDATE users SET username = 'x' WHERE id = 1", "UPDATE bloqueado"),
        ("INSERT INTO glpi_tickets (id) VALUES (1)", "INSERT bloqueado"),
        ("ALTER TABLE reports ADD COLUMN teste INT NULL", "ALTER bloqueado"),
        ("ALTER TABLE reports DROP COLUMN name", "ALTER permitido apenas"),
        ("SHOW DATABASES", "SHOW permitido apenas"),
        ("CREATE TABLE x (id int)", "Comando bloqueado"),
    ],
)
def test_sql_console_blocks_unsafe_commands(db_session, sql_query, expected_message):
    with pytest.raises(SqlConsoleError) as exc_info:
        validate_sql_console_command(sql_query, db_session)

    assert expected_message in str(exc_info.value)


def test_sql_console_allows_active_connector_target_database(db_session):
    db_session.add(make_connector_config(target_database="zabbix_local", is_active=True))
    db_session.commit()

    assert validate_sql_console_command("SELECT * FROM zabbix_local.hosts", db_session) == ("SELECT", "")
    assert validate_sql_console_command(
        "UPDATE zabbix_local.reports SET campo_sql_periodo = 'created_at' WHERE id = 1",
        db_session,
    ) == ("UPDATE", "reports")


def test_sql_console_blocks_inactive_connector_target_database(db_session):
    db_session.add(make_connector_config(target_database="zabbix_local", is_active=False))
    db_session.commit()

    with pytest.raises(SqlConsoleError) as exc_info:
        validate_sql_console_command("SELECT * FROM zabbix_local.hosts", db_session)

    assert "SELECT bloqueado para o database 'zabbix_local'." in str(exc_info.value)


def test_sql_console_executes_only_first_statement():
    first, has_extra = split_first_statement("SELECT 1; UPDATE reports SET name = 'x' WHERE id = 1")

    assert first == "SELECT 1"
    assert has_extra is True


def test_sql_console_ignores_semicolon_inside_string():
    first, has_extra = split_first_statement("SELECT ';' AS value")

    assert first == "SELECT ';' AS value"
    assert has_extra is False


def test_sql_console_denies_non_admin(monkeypatch, view_user):
    monkeypatch.setattr(
        sql_console_routes,
        "require_admin",
        lambda request, db: RedirectResponse("/home", status_code=303),
    )

    response = sql_console_page(object(), None)

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/home"


def test_sql_console_page_allows_admin(monkeypatch, db_session, admin_local_user):
    monkeypatch.setattr(sql_console_routes, "require_admin", lambda request, db: admin_local_user)
    monkeypatch.setattr(sql_console_routes, "render", lambda request, template, context, db: context)

    context = sql_console_page(object(), db_session)

    assert context["active"] == "sql_console"


def test_sql_console_page_denies_when_disabled(monkeypatch, db_session, admin_local_user):
    db_session.add(SystemConfig(key="sql_console_enabled", value="false"))
    db_session.commit()
    monkeypatch.setattr(sql_console_routes, "require_admin", lambda request, db: admin_local_user)

    with pytest.raises(HTTPException) as exc_info:
        sql_console_page(object(), db_session)

    assert exc_info.value.status_code == 403
    assert "Console SQL desativado" in exc_info.value.detail


def test_sql_console_run_denies_when_disabled(monkeypatch, db_session, admin_local_user):
    db_session.add(SystemConfig(key="sql_console_enabled", value="false"))
    db_session.commit()
    monkeypatch.setattr(sql_console_routes, "require_admin", lambda request, db: admin_local_user)

    request = type("Request", (), {"client": None})()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(sql_console_routes.sql_console_run(request, db_session))

    assert exc_info.value.status_code == 403
    assert "Console SQL desativado" in exc_info.value.detail


def test_is_sql_console_enabled_values(db_session):
    assert is_sql_console_enabled(db_session) is True

    db_session.add(SystemConfig(key="sql_console_enabled", value="true"))
    db_session.commit()
    assert is_sql_console_enabled(db_session) is True

    config = db_session.get(SystemConfig, "sql_console_enabled")
    config.value = "false"
    db_session.commit()
    assert is_sql_console_enabled(db_session) is False


def test_sql_console_run_allows_admin(monkeypatch, db_session, admin_local_user):
    async def fake_form_data(request):
        return {"sql_query": "SELECT 1 AS ok", "session_history": "[]"}

    monkeypatch.setattr(sql_console_routes, "require_admin", lambda request, db: admin_local_user)
    monkeypatch.setattr(sql_console_routes, "form_data", fake_form_data)
    monkeypatch.setattr(
        sql_console_routes,
        "execute_sql_console_command",
        lambda sql_query, command: {
            "kind": "rows",
            "columns": ["ok"],
            "rows": [{"ok": 1}],
            "row_count": 1,
        },
    )
    monkeypatch.setattr(sql_console_routes, "log_sql_console_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(sql_console_routes, "render", lambda request, template, context, db: context)

    request = type("Request", (), {"client": None})()
    context = asyncio.run(sql_console_routes.sql_console_run(request, db_session))

    assert context["error"] == ""
    assert context["result"]["row_count"] == 1
