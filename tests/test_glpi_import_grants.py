from types import SimpleNamespace

from app import glpi_import


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        raw = str(statement)
        self.statements.append(raw)
        if "information_schema.SCHEMATA" in raw:
            return SimpleNamespace(first=lambda: None)
        return SimpleNamespace(first=lambda: None)

    def execution_options(self, **kwargs):
        self.statements.append(f"execution_options={kwargs}")
        return self


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self

    def connect(self):
        return self

    def execution_options(self, **kwargs):
        return self

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


def test_provision_engine_uses_provision_user(monkeypatch):
    captured = {}
    sentinel = object()
    monkeypatch.setattr(glpi_import.settings, "local_db_provision_user", "root_provision")
    monkeypatch.setattr(glpi_import.settings, "local_db_provision_pass", "provision pass")
    monkeypatch.setattr(glpi_import.settings, "local_db_admin_user", "portal_db_admin")
    monkeypatch.setattr(glpi_import.settings, "local_db_admin_pass", "admin pass")

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(glpi_import, "create_engine", fake_create_engine)

    assert glpi_import.build_local_provision_engine() is sentinel
    assert "root_provision:provision+pass@" in captured["url"]
    assert "portal_db_admin" not in captured["url"]
    assert captured["kwargs"]["pool_size"] == 1
    assert captured["kwargs"]["max_overflow"] == 0


def test_provision_engine_fallback_to_admin(monkeypatch):
    captured = {}
    sentinel = object()
    monkeypatch.setattr(glpi_import.settings, "local_db_provision_user", None)
    monkeypatch.setattr(glpi_import.settings, "local_db_provision_pass", None)
    monkeypatch.setattr(glpi_import.settings, "local_db_admin_user", "portal_db_admin")
    monkeypatch.setattr(glpi_import.settings, "local_db_admin_pass", "admin pass")

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        return sentinel

    monkeypatch.setattr(glpi_import, "create_engine", fake_create_engine)

    assert glpi_import.build_local_provision_engine() is sentinel
    assert "portal_db_admin:admin+pass@" in captured["url"]


def test_ensure_target_database_grants_admin_and_db_user_options():
    connection = FakeConnection()
    config = SimpleNamespace(target_database="redmine_local")

    ok, sql = glpi_import.ensure_target_database(config, FakeEngine(connection))

    assert ok is True
    assert sql is None
    assert any(
        "GRANT ALL PRIVILEGES ON `redmine_local`.* TO 'portal_db_admin'@'localhost' WITH GRANT OPTION" in statement
        for statement in connection.statements
    )
    assert any(
        "GRANT SELECT ON `redmine_local`.* TO 'portal_db_user'@'localhost' WITH GRANT OPTION" in statement
        for statement in connection.statements
    )


def test_translate_errno_1045_auth():
    assert "Credenciais invalidas" in glpi_import.translate_connection_error(Exception(1045, "Access denied"))


def test_translate_errno_1044_authz():
    assert "Permissao insuficiente" in glpi_import.translate_connection_error(Exception(1044, "Access denied"))


def test_translate_errno_1142_authz():
    assert "Permissao insuficiente" in glpi_import.translate_connection_error(Exception(1142, "Command denied"))
