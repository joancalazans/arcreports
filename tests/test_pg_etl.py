from types import SimpleNamespace

from app.connector_adapters import PostgreSQLAdapter
from app.glpi_import import (
    build_create_table_sql,
    build_insert_sql,
    index_prefix_for_type,
    should_auto_index,
)


class MappingResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class SourceConnection:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def execute(self, statement, params):
        self.params = params
        return MappingResult(self.rows)


def pg_row(name, data_type, length=None, precision=None, scale=None):
    return {
        "column_name": name,
        "data_type": data_type,
        "character_maximum_length": length,
        "numeric_precision": precision,
        "numeric_scale": scale,
    }


def test_get_column_info_detects_unix_clock():
    adapter = PostgreSQLAdapter()
    connection = SourceConnection([pg_row("clock", "integer", precision=32, scale=0)])

    columns = adapter.get_column_info(connection, "public", "events")

    assert columns[0]["is_unix_timestamp"] is True
    assert columns[0]["mariadb_type"] == "DATETIME"
    assert connection.params == {"schema": "public", "table": "events"}
    assert adapter._looks_like_timestamp("created_at")
    assert adapter._looks_like_timestamp("active_since")
    assert not adapter._looks_like_timestamp("hostid")
    assert not adapter._looks_like_timestamp("status")


def test_map_pg_type_bigint():
    adapter = PostgreSQLAdapter()
    columns = adapter.get_column_info(
        SourceConnection([pg_row("hostid", "bigint")]), "public", "hosts"
    )

    assert columns[0]["mariadb_type"] == "BIGINT"
    assert columns[0]["is_unix_timestamp"] is False


def test_get_column_info_preserves_varchar_and_numeric_sizes():
    adapter = PostgreSQLAdapter()
    columns = adapter.get_column_info(
        SourceConnection(
            [pg_row("name", "character varying", length=120), pg_row("amount", "numeric", precision=12, scale=2)]
        ),
        "custom",
        "orders",
    )

    assert columns[0]["mariadb_type"] == "VARCHAR(120)"
    assert columns[1]["mariadb_type"] == "DECIMAL(12,2)"


def test_build_create_table_sql_uses_native_types_and_hash_pk():
    sql = build_create_table_sql(
        "events",
        "zabbix_local",
        [{"column_name": "eventid", "mariadb_type": "BIGINT", "is_unix_timestamp": False}],
    )

    assert "`eventid` BIGINT NULL" in sql
    assert "PRIMARY KEY (`__row_hash`)" in sql


def test_build_insert_sql_unix_timestamp():
    columns = [
        {"column_name": "eventid", "mariadb_type": "BIGINT", "is_unix_timestamp": False},
        {"column_name": "clock", "mariadb_type": "DATETIME", "is_unix_timestamp": True},
    ]

    sql = build_insert_sql("events", "zabbix_local", columns)

    assert "FROM_UNIXTIME(%(clock)s)" in sql
    assert "%(eventid)s" in sql
    assert "- INTERVAL 3 HOUR" in sql


def test_unix_zero_converts_to_null():
    """unix_timestamp = 0 deve gerar NULL no INSERT."""
    columns = [
        {"column_name": "r_clock", "mariadb_type": "DATETIME", "is_unix_timestamp": True},
    ]

    sql = build_insert_sql("problem", "zabbix_local", columns)

    assert "CASE WHEN %(r_clock)s IS NULL OR %(r_clock)s = 0" in sql
    assert "THEN NULL" in sql


def test_unix_nonzero_converts_normally():
    """unix_timestamp > 0 deve usar FROM_UNIXTIME."""
    columns = [
        {"column_name": "clock", "mariadb_type": "DATETIME", "is_unix_timestamp": True},
    ]

    sql = build_insert_sql("events", "zabbix_local", columns)

    assert "ELSE FROM_UNIXTIME(%(clock)s) - INTERVAL 3 HOUR END" in sql


def test_dynamic_auto_index_helpers():
    assert should_auto_index("hostid") is True
    assert should_auto_index("custom_id") is True
    assert should_auto_index("__row_hash") is False
    assert index_prefix_for_type("BIGINT") == ""
    assert index_prefix_for_type("longtext") == "(20)"
    assert index_prefix_for_type("VARCHAR(255)") == "(50)"
