from app.models import Report
from app.reporting import inject_derived_control_columns


def _report(db_session, name, destination_table, is_primary):
    report = Report(
        name=name,
        sql_query=f"SELECT * FROM {destination_table}",
        destination_table=destination_table,
        modo_salvamento="substituir",
        is_primary=is_primary,
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)
    return report


def test_derived_control_columns_are_not_injected_when_source_table_lacks_them(db_session, monkeypatch):
    source = _report(db_session, "Fonte dashboard", "dashboard_6anos", True)
    derived = _report(db_session, "Derivado dashboard", "dashboard_6anos_filtrado", False)
    sql = f"SELECT * FROM {source.destination_table} WHERE Status IN ('Novo','Em Atendimento')"

    monkeypatch.setattr("app.reporting.available_control_columns_for_table", lambda db, table: set())

    assert inject_derived_control_columns(db_session, sql, derived.id, ["Status"]) == sql


def test_control_columns_are_not_injected_for_primary_reports(db_session, monkeypatch):
    primary = _report(db_session, "Primario STI", "dashboard_6anos", True)
    sql = "SELECT ID, Status FROM glpi_tickets"

    monkeypatch.setattr(
        "app.reporting.primary_source_report_for_sql",
        lambda db, cleaned_sql: primary,
    )
    monkeypatch.setattr(
        "app.reporting.available_control_columns_for_table",
        lambda db, table: {"created_at", "solved_at"},
    )

    assert inject_derived_control_columns(db_session, sql, primary.id, ["ID", "Status"]) == sql


def test_derived_control_columns_injects_only_columns_existing_in_source_table(db_session, monkeypatch):
    source = _report(db_session, "Fonte chamados", "tickets_base", True)
    derived = _report(db_session, "Derivado chamados", "tickets_filtrado", False)
    sql = f"SELECT ID, Status FROM {source.destination_table} b WHERE Status = 'Novo'"

    monkeypatch.setattr(
        "app.reporting.available_control_columns_for_table",
        lambda db, table: {"created_at", "closed_at"},
    )
    monkeypatch.setattr(
        "app.reporting.available_columns_for_table",
        lambda db, table: {"ID", "Status", "created_at", "closed_at"},
    )

    injected = inject_derived_control_columns(db_session, sql, derived.id, ["ID", "Status"])

    assert f"FROM ({sql}) AS resultado_base" in injected
    assert f"JOIN `reports`.`{source.destination_table}` AS fonte" in injected
    assert "fonte.`ID` = resultado_base.`ID`" in injected
    assert "fonte.`created_at` AS `created_at`" in injected
    assert "fonte.`closed_at` AS `closed_at`" in injected
    assert "solved_at" not in injected
    assert "reference_date" not in injected
    assert "target_end_at" not in injected


def test_derived_control_columns_falls_back_without_detectable_join_key(db_session, monkeypatch, caplog):
    source = _report(db_session, "Fonte chamados", "tickets_base", True)
    derived = _report(db_session, "Derivado chamados", "tickets_filtrado", False)
    sql = f"SELECT Status FROM {source.destination_table} b WHERE Status = 'Novo'"

    monkeypatch.setattr(
        "app.reporting.available_control_columns_for_table",
        lambda db, table: {"created_at"},
    )
    monkeypatch.setattr(
        "app.reporting.available_columns_for_table",
        lambda db, table: {"ID", "Status", "created_at"},
    )

    injected = inject_derived_control_columns(db_session, sql, derived.id, ["Status"])

    assert injected == sql
    assert "sem chave de JOIN detectavel" in caplog.text
