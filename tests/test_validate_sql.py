import pytest

from app.models import ConnectorConfig
from app.reporting import validate_select
from app.routes.common import get_allowed_databases


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
    "sql",
    [
        "SELECT id, name FROM glpi_tickets",
        """
        SELECT t.id, (SELECT COUNT(*) FROM glpi_logs l
        WHERE l.items_id = t.id) AS total
        FROM glpi_tickets t
        """,
        """
        SELECT t.id, u.name FROM glpi_tickets t
        LEFT JOIN glpi_users u ON t.id = u.id
        LEFT JOIN glpi_groups g ON t.id = g.id
        """,
        "SELECT CASE WHEN t.status = 1 THEN 'Novo' ELSE 'Outro' END FROM glpi_tickets t",
        "WITH base AS (SELECT id FROM glpi_tickets) SELECT * FROM base",
        "SELECT DISTINCT t.id FROM glpi_tickets t ORDER BY t.id DESC",
        "SELECT COUNT(*), SUM(id), AVG(id) FROM glpi_tickets",
        "SELECT IFNULL(name,''), COALESCE(name,''), DATE_FORMAT(date,'%d/%m/%Y') FROM glpi_tickets",
        """
        SELECT (SELECT MIN(date_mod) FROM glpi_logs
        WHERE items_id = 1) AS primeira,
        (SELECT MAX(date_mod) FROM glpi_logs
        WHERE items_id = 1) AS ultima
        """,
        """
        SELECT t.id FROM glpi_tickets t
        WHERE EXISTS (SELECT 1 FROM glpi_logs l
        WHERE l.items_id = t.id)
        """,
        """
        SELECT id FROM glpi_tickets WHERE
        name = 'x' -- INSERT INTO tabela VALUES (1)
        """,
    ],
)
def test_validate_select_accepts_allowed_queries(sql):
    assert validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO glpi_tickets (name) VALUES ('x')",
        "UPDATE glpi_tickets SET name='x' WHERE id=1",
        "DELETE FROM glpi_tickets WHERE id=1",
        "DROP TABLE glpi_tickets",
        "ALTER TABLE glpi_tickets ADD COLUMN x INT",
        "TRUNCATE TABLE glpi_tickets",
        "CREATE TABLE nova_tabela (id INT)",
        "REPLACE INTO glpi_tickets (id,name) VALUES (1,'x')",
        "SELECT id FROM glpi_tickets; DROP TABLE glpi_tickets",
        "SELECT * FROM outro_banco.glpi_tickets",
        "",
        "   ",
    ],
)
def test_validate_select_blocks_invalid_queries(sql):
    with pytest.raises(ValueError):
        validate_select(sql)


def test_validate_select_masks_forbidden_words_inside_strings():
    assert validate_select("SELECT 'DROP TABLE glpi_tickets' AS texto FROM glpi_tickets")


def test_validate_select_allows_active_connector_database(db_session):
    db_session.add(make_connector_config(target_database="zabbix_local", is_active=True))
    db_session.commit()

    assert validate_select(
        "SELECT h.host FROM zabbix_local.hosts h",
        get_allowed_databases(db_session),
    )


def test_validate_select_blocks_inactive_connector_database(db_session):
    db_session.add(make_connector_config(target_database="zabbix_local", is_active=False))
    db_session.commit()

    with pytest.raises(ValueError) as exc_info:
        validate_select(
            "SELECT h.host FROM zabbix_local.hosts h",
            get_allowed_databases(db_session),
        )

    assert "relatorios podem consultar somente o banco local configurado" in str(exc_info.value)
