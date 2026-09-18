import pytest

from app.reporting import apply_dynamic_filters, dynamic_filter_params
from app.routes.dashboard import (
    create_temp_report_table,
    dynamic_filter_column_payload,
    is_dynamic_filter_column,
)


def test_filter_columns_returns_list():
    payload = dynamic_filter_column_payload("Status", ["Fechado", "Resolvido"])

    assert payload == {
        "name": "Status",
        "type": "list",
        "values": ["Fechado", "Resolvido"],
    }


def test_filter_columns_returns_like():
    payload = dynamic_filter_column_payload("CampoLivre", list(range(201)))

    assert payload == {"name": "CampoLivre", "type": "like"}
    assert dynamic_filter_column_payload("Titulo", ["Curto"]) == {
        "name": "Titulo",
        "type": "like",
    }


@pytest.mark.parametrize("column", ["author_id", "__saved_at", "type_id", "qualquer_id"])
def test_technical_columns_ignored(column):
    assert is_dynamic_filter_column(column) is False


@pytest.mark.parametrize("column", ["created_at", "solved_at", "CreatedDate", "Início"])
def test_temporal_columns_ignored(column):
    assert is_dynamic_filter_column(column) is False


def _allow_columns(monkeypatch, *columns):
    monkeypatch.setattr(
        "app.reporting.available_columns_for_table",
        lambda db, table_name: set(columns),
    )


def test_dynamic_filter_list_injection(db_session, monkeypatch):
    _allow_columns(monkeypatch, "Status")

    sql = apply_dynamic_filters(
        "SELECT Status FROM chamados",
        {"Status": ["Resolvido", "Fechado"]},
        "chamados",
        db_session,
    )
    params = dynamic_filter_params(
        {"Status": ["Resolvido", "Fechado"]},
        "chamados",
        db_session,
    )

    assert "dynamic_filter.`Status` IN (:dynamic_0_0, :dynamic_0_1)" in sql
    assert params == {"dynamic_0_0": "Resolvido", "dynamic_0_1": "Fechado"}
    assert "Resolvido" not in sql


def test_dynamic_filter_like_injection(db_session, monkeypatch):
    _allow_columns(monkeypatch, "Titulo")

    sql = apply_dynamic_filters(
        "SELECT Titulo FROM chamados",
        {"Titulo": "impressora"},
        "chamados",
        db_session,
    )
    params = dynamic_filter_params(
        {"Titulo": "impressora"},
        "chamados",
        db_session,
    )

    assert "dynamic_filter.`Titulo` LIKE :dynamic_0" in sql
    assert params == {"dynamic_0": "%impressora%"}
    assert "impressora" not in sql


def test_dynamic_filter_combined(db_session, monkeypatch):
    _allow_columns(monkeypatch, "Status", "Titulo")
    temporal_sql = "SELECT * FROM chamados WHERE `created_at` BETWEEN :date_from AND :date_to"

    sql = apply_dynamic_filters(
        temporal_sql,
        {"Status": ["Resolvido"], "Titulo": "impressora"},
        "chamados",
        db_session,
    )

    assert temporal_sql in sql
    assert "dynamic_filter.`Status` IN (:dynamic_0_0)" in sql
    assert "AND dynamic_filter.`Titulo` LIKE :dynamic_1" in sql


def test_dynamic_filter_sql_injection(db_session, monkeypatch):
    _allow_columns(monkeypatch, "Status")

    with pytest.raises(ValueError, match="Coluna de filtro invalida"):
        apply_dynamic_filters(
            "SELECT Status FROM chamados",
            {"Status` OR 1=1 --": ["Resolvido"]},
            "chamados",
            db_session,
        )


def test_dynamic_filter_without_temporal_plan_accepts_null_selected_column(
    db_session,
    monkeypatch,
):
    """Filtro dinâmico sem período não depende de TemporalPlan."""
    from app.models import Report

    report = Report(
        name="Sem período",
        sql_query="SELECT Status FROM chamados",
        destination_table="chamados",
        modo_salvamento="substituir",
    )
    db_session.add(report)
    db_session.commit()
    monkeypatch.setattr("app.routes.dashboard.get_allowed_databases", lambda db: ["reports"])
    monkeypatch.setattr(
        "app.routes.dashboard.apply_dynamic_filters",
        lambda sql, filters, table_name, db: sql,
    )
    monkeypatch.setattr(
        "app.routes.dashboard.dynamic_filter_params",
        lambda filters, table_name, db: {},
    )

    class EmptyResult:
        def keys(self):
            return ["Status"]

        def mappings(self):
            return iter(())

    class Connection:
        def execute(self, statement, params=None):
            if str(statement).startswith("SELECT Status"):
                return EmptyResult()
            return None

    class BeginContext:
        def __enter__(self):
            return Connection()

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("app.routes.dashboard.local_engine.begin", lambda: BeginContext())
    monkeypatch.setattr("app.routes.dashboard.apply_query_timeout", lambda connection, seconds: None)

    table_name, row_count = create_temp_report_table(
        report,
        user_id=1,
        date_from=None,
        date_to=None,
        plan=None,
        db=db_session,
        dynamic_filters={"Status": ["Resolvido"]},
    )

    assert table_name.startswith(f"tmp_report_{report.id}_1_")
    assert row_count == 0
