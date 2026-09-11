from app import utils


def _mock_columns(monkeypatch, rows, sample_matches=()):
    class FakeResult:
        def __init__(self, values=None, matched=False):
            self.values = values
            self.matched = matched

        def mappings(self):
            return self.values

        def first(self):
            return (1,) if self.matched else None

    class FakeConnection:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "information_schema.columns" in sql:
                return FakeResult(rows)
            return FakeResult(matched=any(f"`{name}`" in sql for name in sample_matches))

    class FakeConnect:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, tb):
            return False

    utils._sample_cache.clear()
    monkeypatch.setattr(utils.local_engine, "connect", lambda: FakeConnect())


def test_get_date_columns_prefers_canonical_columns_over_aliases(monkeypatch):
    rows = [
        {"column_name": "CreatedDate", "data_type": "longtext"},
        {"column_name": "created_at", "data_type": "longtext"},
        {"column_name": "CompletedDate", "data_type": "longtext"},
        {"column_name": "solved_at", "data_type": "longtext"},
        {"column_name": "CloseDate", "data_type": "longtext"},
        {"column_name": "closed_at", "data_type": "longtext"},
        {"column_name": "StartDate", "data_type": "longtext"},
        {"column_name": "reference_date", "data_type": "longtext"},
        {"column_name": "TargetEndDate", "data_type": "longtext"},
        {"column_name": "target_end_at", "data_type": "longtext"},
    ]

    class FakeResult:
        def mappings(self):
            return rows

    class FakeConnection:
        def execute(self, statement, params=None):
            return FakeResult()

    class FakeConnect:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(utils.local_engine, "connect", lambda: FakeConnect())

    columns = utils.get_date_columns("tabela_teste")

    assert [column["name"] for column in columns] == [
        "created_at",
        "solved_at",
        "closed_at",
        "reference_date",
        "target_end_at",
    ]


def test_get_date_columns_keeps_alias_when_canonical_column_is_absent(monkeypatch):
    rows = [
        {"column_name": "CreatedDate", "data_type": "longtext"},
        {"column_name": "CompletedDate", "data_type": "longtext"},
    ]

    class FakeResult:
        def mappings(self):
            return rows

    class FakeConnection:
        def execute(self, statement, params=None):
            return FakeResult()

    class FakeConnect:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(utils.local_engine, "connect", lambda: FakeConnect())

    columns = utils.get_date_columns("tabela_teste")

    assert [column["name"] for column in columns] == ["CreatedDate", "CompletedDate"]


def test_criado_em_reconhecido(monkeypatch):
    """'Criado em' deve ser reconhecido como coluna temporal."""
    _mock_columns(
        monkeypatch,
        [{"column_name": "Criado em", "data_type": "longtext"}],
    )

    assert utils.get_date_columns("tabela_redmine") == [
        {"name": "Criado em", "label": "Data de Criação"}
    ]


def test_concluido_reconhecido(monkeypatch):
    """'Concluído' deve ser reconhecido como coluna temporal."""
    _mock_columns(
        monkeypatch,
        [{"column_name": "Concluído", "data_type": "longtext"}],
    )

    assert utils.get_date_columns("tabela_redmine") == [
        {"name": "Concluído", "label": "Data de Conclusão"}
    ]


def test_get_date_columns_clock_suffix(monkeypatch):
    _mock_columns(
        monkeypatch,
        [{"column_name": "problema_clock", "data_type": "longtext"}],
        sample_matches={"problema_clock"},
    )
    assert [item["name"] for item in utils.get_date_columns("zabbix_dashboard_base")] == ["problema_clock"]


def test_get_date_columns_data_prefix(monkeypatch):
    _mock_columns(
        monkeypatch,
        [{"column_name": "DataPrimeiraReabertura", "data_type": "longtext"}],
        sample_matches={"DataPrimeiraReabertura"},
    )
    assert [item["name"] for item in utils.get_date_columns("reaberturas")] == ["DataPrimeiraReabertura"]


def test_get_date_columns_excludes_duration(monkeypatch):
    _mock_columns(
        monkeypatch,
        [{"column_name": "duracao_horas", "data_type": "longtext"}],
        sample_matches={"duracao_horas"},
    )
    assert utils.get_date_columns("duracoes") == []


def test_invalidate_date_column_cache(monkeypatch):
    monkeypatch.setattr(utils.settings, "local_db_name", "reports")
    utils._sample_cache.update({("reports", "alvo", "data"): True, ("reports", "outra", "data"): True})
    utils.invalidate_date_column_cache("alvo")
    assert ("reports", "alvo", "data") not in utils._sample_cache
    assert ("reports", "outra", "data") in utils._sample_cache
