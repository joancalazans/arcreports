import pytest

from app.models import Report, User
from app.reporting import (
    INTERNAL_DESTINATION_TABLES,
    assert_report_destination_can_be_cleared,
    clear_report_destination_table,
)
from app.routes.admin_access import is_last_local_admin
from app.routes.local_tables import is_safe_local_user_table, is_safe_report_result_table
from app.security import hash_password


def _report(db_session, destination_table):
    report = Report(
        name=f"Relatorio {destination_table or 'sem destino'}",
        sql_query="SELECT id FROM glpi_tickets",
        destination_table=destination_table,
        modo_salvamento="substituir",
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)
    return report


def test_glpi_prefix_tables_cannot_be_cleared(db_session):
    report = _report(db_session, "glpi_tickets")

    with pytest.raises(ValueError, match="glpi_"):
        assert_report_destination_can_be_cleared(db_session, report.id, report.destination_table)


def test_internal_portal_tables_cannot_be_cleared(db_session):
    table_name = next(name for name in INTERNAL_DESTINATION_TABLES if not name.startswith("glpi_"))
    report = _report(db_session, table_name)

    with pytest.raises(ValueError, match="interna"):
        assert_report_destination_can_be_cleared(db_session, report.id, report.destination_table)


def test_orphan_local_table_can_be_deleted_but_not_modified(db_session):
    assert is_safe_local_user_table("resultado_antigo") is True
    assert is_safe_report_result_table("resultado_antigo", db_session) is False


def test_protected_local_tables_cannot_be_deleted(db_session):
    assert is_safe_local_user_table("reports") is False
    assert is_safe_local_user_table("glpi_tickets") is False
    assert is_safe_local_user_table("nome-invalido") is False


def test_report_without_destination_table_does_not_reach_cleanup_engine(db_session, monkeypatch):
    report = _report(db_session, None)
    called = False

    def fake_begin():
        nonlocal called
        called = True
        raise AssertionError("local_engine.begin should not be called")

    monkeypatch.setattr("app.reporting.local_engine.begin", fake_begin)

    with pytest.raises(ValueError, match="invalida"):
        clear_report_destination_table(db_session, report.id, report.destination_table)
    assert called is False


def test_deactivating_last_local_admin_is_blocked(db_session, admin_local_user):
    new_is_active = False
    assert is_last_local_admin(db_session, admin_local_user.id) is True
    assert is_last_local_admin(db_session, admin_local_user.id) and not new_is_active


def test_removing_admin_flag_from_last_local_admin_is_blocked(db_session, admin_local_user):
    new_is_admin = False
    assert is_last_local_admin(db_session, admin_local_user.id) is True
    assert is_last_local_admin(db_session, admin_local_user.id) and not new_is_admin


def test_with_two_local_admins_deactivating_one_is_allowed(db_session, admin_local_user):
    second_admin = User(
        username="second_admin",
        is_local=True,
        auth_source="local",
        is_admin=True,
        is_active=True,
        password_hash=hash_password("Senha@123"),
    )
    db_session.add(second_admin)
    db_session.commit()

    assert is_last_local_admin(db_session, admin_local_user.id) is False
