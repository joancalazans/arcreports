from __future__ import annotations

import hashlib
import json
import logging
import threading
import re
import time
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.connector_adapters import (
    PostgreSQLAdapter,
    SourceAdapter,
    get_adapter,
    get_table_load_profile,
)
from app.crypto import encrypt_password, is_encrypted
from app.database import local_engine
from app.models import ConnectorConfig, GlpiImportLog, ConnectorRun, ConnectorSyncTable
from app.timezone import utc_naive_to_local


settings = get_settings()
logger = logging.getLogger(__name__)

_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

def get_cancel_event(connector_type: str) -> threading.Event:
    with _cancel_lock:
        return _cancel_events.setdefault(connector_type, threading.Event())

def request_cancel(connector_type: str) -> None:
    get_cancel_event(connector_type).set()

def clear_cancel(connector_type: str) -> None:
    get_cancel_event(connector_type).clear()

def is_cancelled(connector_type: str) -> bool:
    return get_cancel_event(connector_type).is_set()
BATCH_SIZE = 1000
DEFAULT_LOCAL_ENGINE = local_engine
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
INTEGER_COLUMN_TYPES = {
    "bigint",
    "int",
    "integer",
    "mediumint",
    "smallint",
    "tinyint",
}
REPORT_INDEXES = {
    "glpi_logs": {
        "idx_portal_logs_ticket_date": [
            ("items_id", 32),
            ("itemtype", 32),
            ("id_search_option", 32),
            ("date_mod", 32),
            ("id", None),
        ],
        "idx_portal_logs_ticket_new_date": [
            ("items_id", 32),
            ("itemtype", 32),
            ("id_search_option", 32),
            ("new_value", 32),
            ("date_mod", 32),
            ("id", None),
        ],
        "idx_portal_logs_reopen": [
            ("itemtype", 32),
            ("id_search_option", 32),
            ("old_value", 32),
            ("new_value", 32),
            ("items_id", 32),
            ("date_mod", 32),
            ("id", None),
        ],
        "idx_portal_logs_ticket": [
            ("items_id", 32),
            ("itemtype", 32),
            ("id_search_option", 32),
            ("old_value", 32),
            ("new_value", 32),
        ],
    },
    "glpi_tickets": {
        "idx_portal_tickets_solved": [
            ("is_deleted", 8),
            ("status", 8),
            ("solvedate", 32),
            ("id", None),
        ],
    },
    "glpi_tickets_users": {
        "idx_portal_tu_ticket_type": [
            ("tickets_id", 32),
            ("type", 8),
            ("users_id", 32),
        ],
        "idx_portal_tu_user_type": [
            ("users_id", 32),
            ("type", 8),
            ("tickets_id", 32),
        ],
    },
    "glpi_groups_tickets": {
        "idx_portal_gt_ticket_type": [
            ("tickets_id", 32),
            ("type", 8),
            ("groups_id", 32),
        ],
    },
}

JOIN_INDEX_RE = re.compile(
    r"^(id|.*id|id_.+|.*_id|.*name|clock|status|type|object|source|value|severity|"
    r"r_eventid|objectid|groupid|hostid|serviceid|triggerid|itemid|eventid|"
    r"slaid|created_at|lastchange|start_date)$",
    re.IGNORECASE,
)

INITIAL_SYNC_TABLES = [
    "glpi_tickets",
    "glpi_logs",
    "glpi_tickets_users",
    "glpi_groups_tickets",
    "glpi_users",
    "glpi_groups",
    "glpi_entities",
    "glpi_slas",
    "glpi_slaplans",
    "glpi_slalevels",
    "glpi_requesttypes",
    "glpi_itilcategories",
    "glpi_locations",
    "glpi_tickettasks",
    "glpi_itilfollowups",
    "glpi_solutiontemplates",
    "glpi_solutiontypes",
    "glpi_ticketvalidations",
    "glpi_ticketsatisfactions",
    "glpi_items_tickets",
    "glpi_computers",
    "glpi_printers",
    "glpi_networkequipments",
    "glpi_monitors",
    "glpi_peripherals",
    "glpi_software",
    "glpi_softwares",
    "glpi_softwareversions",
    "glpi_operatingsystems",
    "glpi_states",
    "glpi_manufacturers",
    "glpi_suppliers",
    "glpi_contracts",
    "glpi_contracts_items",
    "glpi_groups_users",
    "glpi_profiles_users",
    "glpi_profiles",
    "glpi_useremails",
    "glpi_authldapreplicates",
    "glpi_calendars",
    "glpi_calendarsegments",
    "glpi_calendars_holidays",
    "glpi_holidays",
    "glpi_olas",
    "glpi_olalevels",
    "glpi_olaplans",
    "glpi_tickets_tickets",
    "glpi_problems",
    "glpi_changes",
]


def quote_identifier(identifier: str) -> str:
    if not IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Identificador invalido: {identifier}")
    return f"`{identifier}`"


def default_incremental_column(table_name: str) -> str:
    if table_name in {"glpi_logs"}:
        return "date_mod"
    return "date_mod"


def seed_connector_settings(db: Session) -> None:
    if not db.query(ConnectorConfig).filter(ConnectorConfig.connector_type == "glpi").first():
        db.add(
            ConnectorConfig(
                name="GLPI Produção",
                connector_type="glpi",
                db_type="mysql",
                host=settings.glpi_db_host,
                port=settings.glpi_db_port,
                database_name=settings.glpi_db_name,
                target_database="glpi_local",
                table_prefix="glpi_",
                username=settings.glpi_db_user,
                password=(
                    settings.glpi_db_pass
                    if is_encrypted(settings.glpi_db_pass)
                    else encrypt_password(settings.glpi_db_pass, settings.encryption_key)
                ),
                suggested_frequency="08:00,20:00",
                is_active=True,
            )
        )

    existing = {
        row.table_name
        for row in db.query(ConnectorSyncTable.table_name)
        .filter(ConnectorSyncTable.connector_type == "glpi")
        .all()
    }
    for table_name in INITIAL_SYNC_TABLES:
        if table_name not in existing:
            load_type, incremental_column = get_table_load_profile("glpi", table_name)
            db.add(
                ConnectorSyncTable(
                    connector_type="glpi",
                    table_name=table_name,
                    is_active=True,
                    load_type=load_type,
                    incremental_column=incremental_column,
                    configured_load_type=load_type,
                )
            )
    db.commit()


def get_connection_config(db: Session, connector_type: str | None = None) -> ConnectorConfig | None:
    query = db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True))
    if connector_type:
        query = query.filter(ConnectorConfig.connector_type == connector_type)
    config = query.order_by(ConnectorConfig.id.asc()).first()
    if config:
        return config
    if connector_type and connector_type != "glpi":
        return None
    seed_connector_settings(db)
    query = db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True))
    if connector_type:
        query = query.filter(ConnectorConfig.connector_type == connector_type)
    return query.order_by(ConnectorConfig.id.asc()).first()


def build_source_engine(config: ConnectorConfig) -> Engine:
    return get_adapter(config.db_type).build_engine(config)


def target_import_database(config: ConnectorConfig) -> str:
    target_database = (getattr(config, "target_database", None) or settings.local_db_name).strip()
    if not IDENTIFIER_RE.match(target_database):
        raise ValueError(f"Database de destino invalido: {target_database}")
    return target_database


def build_target_database_admin_sql(target_database: str) -> str:
    safe_database = quote_identifier(target_database)
    safe_user = sql_string_literal(settings.local_db_user or "glpi_portal")
    return (
        f"CREATE DATABASE IF NOT EXISTS {safe_database} "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
        f"GRANT ALL PRIVILEGES ON {safe_database}.* TO {safe_user}@'localhost';\n"
        f"GRANT ALL PRIVILEGES ON {safe_database}.* TO 'portal_db_admin'@'localhost' WITH GRANT OPTION;\n"
        f"GRANT SELECT ON {safe_database}.* TO 'portal_db_user'@'localhost' WITH GRANT OPTION;\n"
        "FLUSH PRIVILEGES;"
    )


def sql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def build_local_provision_engine() -> Engine | None:
    """
    Engine usada apenas para CREATE DATABASE e GRANT em novos conectores.

    Usa LOCAL_DB_PROVISION_USER quando configurado e mantém
    LOCAL_DB_ADMIN_USER como fallback compatível. Não é usada nas operações
    normais da aplicação.
    """
    user = settings.local_db_provision_user or settings.local_db_admin_user
    password = settings.local_db_provision_pass or settings.local_db_admin_pass
    if not user or not password:
        return None
    safe_user = quote_plus(user)
    safe_password = quote_plus(password)
    url = (
        f"mysql+pymysql://{safe_user}:{safe_password}"
        f"@{settings.local_db_host}:{settings.local_db_port}/"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        future=True,
    )


def target_database_permission_error(exc: Exception, target_database: str) -> str:
    detail = translate_connection_error(exc, database=target_database)
    return f"Database criada. Erro ao configurar permissoes: {detail}. Contate o administrador do sistema."


def ensure_target_database(
    config: ConnectorConfig,
    local_engine: Engine = DEFAULT_LOCAL_ENGINE,
    admin_engine: Engine | None = None,
) -> tuple[bool, str | None]:
    """
    Tenta criar o target_database se não existir.
    Retorna (sucesso, mensagem_operacional_se_falhar).
    """
    target_database = target_import_database(config)
    safe_database = quote_identifier(target_database)
    try:
        with local_engine.begin() as connection:
            exists = connection.execute(
                text(
                    "SELECT SCHEMA_NAME "
                    "FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME = :target_database"
                ),
                {"target_database": target_database},
            ).first()
            if exists:
                return True, None
    except SQLAlchemyError as exc:
        logger.warning(
            "Falha ao verificar database destino %s: %s",
            target_database,
            translate_connection_error(exc, database=target_database),
        )
        return False, f"Erro ao verificar database destino {target_database}. Contate o administrador do sistema."

    owns_admin_engine = False
    if admin_engine is None:
        if local_engine is not DEFAULT_LOCAL_ENGINE:
            admin_engine = local_engine
        else:
            admin_engine = build_local_provision_engine()
            owns_admin_engine = admin_engine is not None
    if not admin_engine:
        logger.warning(
            "Database destino %s nao pode ser criada automaticamente: "
            "credencial de provisionamento ausente.",
            target_database,
        )
        return False, "Credencial de provisionamento de banco nao configurada. Contate o administrador do sistema."
    try:
        local_user = sql_string_literal(settings.local_db_user or "glpi_portal")
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS {safe_database} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f"GRANT ALL PRIVILEGES ON {safe_database}.* TO {local_user}@'localhost'"))
            connection.execute(
                text(
                    f"GRANT ALL PRIVILEGES ON {safe_database}.* "
                    "TO 'portal_db_admin'@'localhost' WITH GRANT OPTION"
                )
            )
            connection.execute(
                text(
                    f"GRANT SELECT ON {safe_database}.* "
                    "TO 'portal_db_user'@'localhost' WITH GRANT OPTION"
                )
            )
            connection.execute(text("FLUSH PRIVILEGES"))
        return True, None
    except SQLAlchemyError as exc:
        logger.warning(
            "Falha ao configurar permissoes da database destino %s. "
            "LOCAL_DB_PROVISION_USER precisa conseguir conceder privilegios na nova database: %s",
            target_database,
            translate_connection_error(exc, database=target_database),
        )
        return False, target_database_permission_error(exc, target_database)
    finally:
        if owns_admin_engine:
            admin_engine.dispose()


def _exception_errno(exc: Exception) -> int | None:
    for item in getattr(exc, "args", ()):
        if isinstance(item, int):
            return item
        if isinstance(item, BaseException):
            nested_errno = _exception_errno(item)
            if nested_errno is not None:
                return nested_errno
    original = getattr(exc, "orig", None)
    if original is not None and original is not exc:
        return _exception_errno(original)
    return None


def translate_connection_error(
    exc: Exception,
    host: str | None = None,
    port: str | None = None,
    database: str | None = None,
) -> str:
    message = str(exc)
    lower_message = message.lower()
    errno = _exception_errno(exc)
    endpoint = f"{host}:{port}" if host and port else (host or "")
    database_name = database or ""

    if "connection refused" in lower_message or errno == 111:
        target = endpoint or "origem"
        return (
            f"Nao foi possivel conectar ao host {target}. "
            "Verifique se o servidor esta acessivel e a porta esta correta."
        )
    if errno == 1045:
        return "Credenciais invalidas. Verifique o usuario e a senha configurados."
    if errno in (1044, 1142, 1143):
        return (
            "Permissao insuficiente. O usuario configurado em "
            "LOCAL_DB_PROVISION_USER nao tem privilegios suficientes para "
            "criar databases ou conceder privilegios. Contate o administrador do sistema."
        )
    if "access denied" in lower_message:
        return "Acesso negado. Verifique as credenciais e permissoes."
    if "unknown database" in lower_message or errno == 1049:
        return f"Database '{database_name}' nao encontrada. Verifique o nome do banco de dados."
    if "can't connect to mysql server" in lower_message or "can not connect to mysql server" in lower_message:
        return "Nao foi possivel conectar ao servidor MySQL/MariaDB. Verifique o host e porta."
    if "could not connect to server" in lower_message:
        return "Nao foi possivel conectar ao servidor PostgreSQL. Verifique host, porta e firewall."
    if "password authentication failed" in lower_message:
        return "Senha incorreta para o usuario PostgreSQL informado."
    if "timeout" in lower_message or "timed out" in lower_message:
        return "Conexao expirou. O servidor pode estar inacessivel ou bloqueado por firewall."
    return f"Erro de conexao: {message}"


def test_glpi_connection(db: Session, connector_type: str | None = None) -> tuple[bool, str]:
    config = get_connection_config(db, connector_type)
    if not config:
        return False, "Conector ativo nao encontrado."
    engine = build_source_engine(config)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "Conexao testada com sucesso."
    except SQLAlchemyError as exc:
        return False, translate_connection_error(exc, config.host, config.port, config.database_name)
    finally:
        engine.dispose()


def log_import(db: Session, level: str, message: str, run_id: int | None = None) -> None:
    db.add(GlpiImportLog(run_id=run_id, level=level, message=message))
    db.commit()


def discover_source_tables(db: Session, config: ConnectorConfig | None = None) -> set[str]:
    config = config or get_connection_config(db)
    if not config:
        return set()
    engine = build_source_engine(config)
    adapter = get_adapter(config.db_type)
    try:
        with engine.connect() as connection:
            schema = config.schema_name or config.database_name
            if config.db_type == "postgresql":
                schema = config.schema_name or "public"
            table_names = adapter.get_tables(connection, config.table_prefix, schema)
    finally:
        engine.dispose()

    if config.import_mode == "custom":
        whitelist = config.whitelist_tables
        if whitelist:
            table_names = [table_name for table_name in table_names if table_name in whitelist]
        else:
            logger.warning(
                "Conector %s em modo custom sem whitelist configurada. Nenhuma tabela sera importada.",
                config.connector_type,
            )
            log_import(
                db,
                "warning",
                (
                    f"Conector {config.connector_type} em modo custom sem whitelist configurada. "
                    "Nenhuma tabela sera importada."
                ),
            )
            return set()

    existing = {
        row.table_name
        for row in db.query(ConnectorSyncTable.table_name)
        .filter(ConnectorSyncTable.connector_type == config.connector_type)
        .all()
    }
    discovered = {table_name for table_name in table_names if IDENTIFIER_RE.match(table_name)}
    created = 0
    skipped = 0
    for table_name in table_names:
        if table_name in existing:
            continue
        if not IDENTIFIER_RE.match(table_name):
            skipped += 1
            continue
        load_type, incremental_column = get_table_load_profile(
            config.connector_type,
            table_name,
        )
        db.add(
            ConnectorSyncTable(
                connector_type=config.connector_type,
                table_name=table_name,
                is_active=True,
                load_type=load_type,
                incremental_column=incremental_column,
                configured_load_type=load_type,
            )
        )
        existing.add(table_name)
        created += 1

    missing_tables = []
    if config.import_mode == "custom":
        whitelist = set(config.whitelist_tables)
        whitelist_missing = sorted(table_name for table_name in whitelist if table_name not in discovered)
        if whitelist_missing:
            logger.warning(
                "Conector %s em modo custom possui tabela(s) da whitelist ausente(s) na origem: %s",
                config.connector_type,
                ", ".join(whitelist_missing),
            )
            log_import(
                db,
                "warning",
                (
                    f"Conector {config.connector_type} em modo custom possui "
                    f"{len(whitelist_missing)} tabela(s) da whitelist ausente(s) na origem."
                ),
            )
    else:
        missing_tables = (
            db.query(ConnectorSyncTable)
            .filter(ConnectorSyncTable.connector_type == config.connector_type)
            .filter(ConnectorSyncTable.is_active.is_(True))
            .filter(ConnectorSyncTable.table_name.notin_(discovered))
            .all()
        )
        for sync_table in missing_tables:
            sync_table.is_active = False
            sync_table.last_error = f"Tabela nao encontrada na descoberta automatica do conector {config.connector_type}."

    if created:
        log_import(db, "info", f"Descoberta automatica cadastrou {created} tabela(s) do conector {config.connector_type}.")
    if missing_tables:
        log_import(db, "warning", f"Descoberta automatica desativou {len(missing_tables)} tabela(s) inexistente(s) no conector {config.connector_type}.")
    if skipped:
        log_import(db, "warning", f"Descoberta automatica ignorou {skipped} tabela(s) com nome invalido.")
    if not created and not skipped and not missing_tables:
        db.commit()
    return discovered


def get_table_columns(engine: Engine, config: ConnectorConfig, table_name: str, adapter: SourceAdapter) -> list[str]:
    with engine.connect() as connection:
        return adapter.get_columns(connection, config, table_name)


def detect_column_type(connection, table_name: str, column_name: str, db_type: str, schema: str | None = None) -> str:
    if not column_name:
        return "datetime"

    db_type = (db_type or "mysql").lower()
    params = {"table_name": table_name, "column_name": column_name}
    if db_type == "postgresql":
        params["schema"] = schema or "public"
        result = connection.execute(
            text(
                "SELECT data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema "
                "AND table_name = :table_name "
                "AND column_name = :column_name"
            ),
            params,
        ).first()
    else:
        params["schema"] = schema
        result = connection.execute(
            text(
                "SELECT DATA_TYPE AS data_type "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :schema "
                "AND TABLE_NAME = :table_name "
                "AND COLUMN_NAME = :column_name"
            ),
            params,
        ).first()

    data_type = str(result[0]).lower() if result and result[0] else ""
    if column_name == "clock" and data_type in INTEGER_COLUMN_TYPES:
        return "unix_timestamp"
    if data_type in INTEGER_COLUMN_TYPES:
        return "unix_timestamp"
    return "datetime"


def build_create_table_sql(table: str, database: str, columns: list[dict]) -> str:
    """Gera o DDL nativo de uma tabela importada do PostgreSQL."""
    if not IDENTIFIER_RE.match(table) or not IDENTIFIER_RE.match(database):
        raise ValueError("Database ou tabela invalida para importacao.")
    col_defs = []
    for column in columns:
        name = str(column["column_name"])
        if not IDENTIFIER_RE.match(name):
            raise ValueError(f"Coluna invalida para importacao: {name}")
        col_defs.append(f"{quote_identifier(name)} {column['mariadb_type']} NULL")
    col_defs.append("`__row_hash` VARCHAR(64) NOT NULL")
    cols_sql = ",\n  ".join(col_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(database)}.{quote_identifier(table)} (\n"
        f"  {cols_sql},\n  PRIMARY KEY (`__row_hash`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )


def build_insert_sql(
    table: str,
    database: str,
    columns: list[dict],
    timezone_offset_hours: int = -3,
    include_row_hash: bool = True,
) -> str:
    """Gera upsert DBAPI; epochs são convertidos pelo MariaDB, não pelo Python."""
    if not IDENTIFIER_RE.match(table) or not IDENTIFIER_RE.match(database):
        raise ValueError("Database ou tabela invalida para importacao.")
    names = [str(column["column_name"]) for column in columns]
    if any(not IDENTIFIER_RE.match(name) for name in names):
        raise ValueError("Coluna invalida para importacao.")
    insert_names = [quote_identifier(name) for name in names]
    values = []
    sign = "-" if timezone_offset_hours < 0 else "+"
    offset = abs(int(timezone_offset_hours))
    for column, name in zip(columns, names):
        parameter = f"%({name})s"
        if column.get("is_unix_timestamp"):
            values.append(
                f"CASE WHEN {parameter} IS NULL OR {parameter} = 0 "
                f"THEN NULL ELSE FROM_UNIXTIME({parameter}) "
                f"{sign} INTERVAL {offset} HOUR END"
            )
        else:
            values.append(parameter)
    if include_row_hash:
        insert_names.append("`__row_hash`")
        values.append("%(__row_hash)s")
    key_columns = {"__row_hash"} if include_row_hash else {"id"}
    updates = [
        f"{quote_identifier(name)} = VALUES({quote_identifier(name)})"
        for name in names
        if name not in key_columns
    ]
    if not updates:
        updates = [f"{quote_identifier(names[0])} = VALUES({quote_identifier(names[0])})"]
    return (
        f"INSERT INTO {quote_identifier(database)}.{quote_identifier(table)} "
        f"({', '.join(insert_names)}) VALUES ({', '.join(values)}) "
        f"ON DUPLICATE KEY UPDATE {', '.join(updates)}"
    )


def ensure_postgresql_import_table(
    target_database: str,
    table_name: str,
    columns: list[dict],
) -> bool:
    """Cria tabela nativa e retorna se o upsert deve usar __row_hash."""
    safe_database = quote_identifier(target_database)
    safe_table = quote_identifier(table_name)
    with local_engine.begin() as connection:
        connection.execute(text(build_create_table_sql(table_name, target_database, columns)))
        existing_columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
                ),
                {"database_name": target_database, "table_name": table_name},
            )
        }
        for column in columns:
            name = column["column_name"]
            if name not in existing_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {safe_database}.{safe_table} ADD COLUMN "
                        f"{quote_identifier(name)} {column['mariadb_type']} NULL"
                    )
                )
        if "__row_hash" in existing_columns:
            return True
        if "id" in existing_columns:
            return False
        raise RuntimeError(
            f"Tabela existente {target_database}.{table_name} nao possui __row_hash nem id; "
            "execute primeiro o script de migracao gerado."
        )


def ensure_import_table(target_database: str, table_name: str, columns: Iterable[str]) -> None:
    safe_table = quote_identifier(table_name)
    safe_database = quote_identifier(target_database)
    safe_columns = [column for column in columns if IDENTIFIER_RE.match(column)]
    if not safe_columns:
        raise ValueError(f"Tabela {table_name} sem colunas validas para importacao.")

    column_defs = [f"{quote_identifier(column)} LONGTEXT NULL" for column in safe_columns]
    if "id" in safe_columns:
        column_defs = [
            "`id` VARCHAR(191) NOT NULL" if column == "id" else f"{quote_identifier(column)} LONGTEXT NULL"
            for column in safe_columns
        ]
        primary_key = "PRIMARY KEY (`id`)"
    else:
        column_defs.append("`__row_hash` VARCHAR(64) NOT NULL")
        primary_key = "PRIMARY KEY (`__row_hash`)"

    with local_engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {safe_database}.{safe_table} "
                f"({', '.join(column_defs)}, {primary_key}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        result = connection.execute(
            text(
                "SELECT COLUMN_NAME "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :database_name "
                "AND TABLE_NAME = :table_name"
            ),
            {"database_name": target_database, "table_name": table_name},
        )
        existing_columns = {row[0] for row in result}
        for column in safe_columns:
            if column not in existing_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {safe_database}.{safe_table} "
                        f"ADD COLUMN {quote_identifier(column)} LONGTEXT NULL"
                    )
                )
    ensure_report_indexes(target_database, table_name, safe_columns)


def ensure_report_indexes(target_database: str, table_name: str, columns: Iterable[str]) -> None:
    indexes = REPORT_INDEXES.get(table_name)
    if not indexes:
        return

    safe_table = quote_identifier(table_name)
    safe_database = quote_identifier(target_database)
    available_columns = set(columns)
    with local_engine.begin() as connection:
        existing_indexes = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT DISTINCT INDEX_NAME "
                    "FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA = :database_name AND TABLE_NAME = :table_name"
                ),
                {"database_name": target_database, "table_name": table_name},
            )
        }
        for index_name, index_columns in indexes.items():
            if index_name in existing_indexes:
                continue
            if any(column not in available_columns for column, _ in index_columns):
                continue
            column_sql = ", ".join(
                f"{quote_identifier(column)}({prefix})" if prefix else quote_identifier(column)
                for column, prefix in index_columns
            )
            connection.execute(
                text(
                    f"CREATE INDEX {quote_identifier(index_name)} "
                    f"ON {safe_database}.{safe_table} ({column_sql})"
                )
            )


def stable_hash(row: dict) -> str:
    payload = json.dumps(row, default=str, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def insert_rows(
    target_database: str,
    table_name: str,
    rows: list[dict],
    columns: list[str],
    adapter: SourceAdapter | None = None,
) -> int:
    if not rows:
        return 0

    safe_table = quote_identifier(table_name)
    safe_database = quote_identifier(target_database)
    safe_columns = [column for column in columns if IDENTIFIER_RE.match(column)]
    use_id = "id" in safe_columns
    insert_columns = list(safe_columns)
    if not use_id:
        insert_columns.append("__row_hash")

    column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
    value_sql = ", ".join(f":{column}" for column in insert_columns)
    update_columns = [column for column in insert_columns if column not in {"id", "__row_hash"}]
    update_sql = ", ".join(f"{quote_identifier(column)} = VALUES({quote_identifier(column)})" for column in update_columns)
    if not update_sql:
        update_sql = f"{quote_identifier(insert_columns[0])} = VALUES({quote_identifier(insert_columns[0])})"

    payload = []
    for row in rows:
        item = {column: row.get(column) for column in safe_columns}
        if adapter and "clock" in item and isinstance(item["clock"], int) and not isinstance(item["clock"], bool):
            item["clock"] = adapter.normalize_clock_value(item["clock"], "integer")
        if not use_id:
            item["__row_hash"] = stable_hash(item)
        payload.append(item)

    with local_engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {safe_database}.{safe_table} ({column_sql}) "
                f"VALUES ({value_sql}) ON DUPLICATE KEY UPDATE {update_sql}"
            ),
            payload,
        )
    return len(payload)


def insert_postgresql_rows(
    target_database: str,
    table_name: str,
    rows: list[dict],
    columns: list[dict],
    include_row_hash: bool = True,
) -> int:
    if not rows:
        return 0
    names = [column["column_name"] for column in columns]
    payload = []
    for row in rows:
        item = {name: row.get(name) for name in names}
        if include_row_hash:
            item["__row_hash"] = stable_hash(item)
        payload.append(item)
    insert_sql = build_insert_sql(
        table_name,
        target_database,
        columns,
        include_row_hash=include_row_hash,
    )
    with local_engine.begin() as connection:
        connection.exec_driver_sql(insert_sql, payload)
    return len(payload)


def should_auto_index(col_name: str) -> bool:
    return col_name != "__row_hash" and bool(JOIN_INDEX_RE.match(col_name))


def index_prefix_for_type(col_type: str) -> str:
    normalized = str(col_type or "").upper()
    if any(value in normalized for value in ("LONGTEXT", "MEDIUMTEXT", "TEXT", "BLOB")):
        return "(20)"
    if "VARCHAR" in normalized:
        try:
            size = int(normalized.split("(", 1)[1].rstrip(")"))
            return f"({min(size, 50)})"
        except (IndexError, ValueError):
            return "(50)"
    return ""


def _create_missing_indexes(connection, target_database: str, table_name: str) -> list[str]:
    """Cria e retorna os índices JOIN faltantes de uma tabela de conector."""
    created: list[str] = []
    table_columns = {
        row[0]: row[1]
        for row in connection.execute(
            text(
                "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table"
            ),
            {"database": target_database, "table": table_name},
        )
    }
    indexed = {
        row[0]
        for row in connection.execute(
            text(
                "SELECT DISTINCT COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table"
            ),
            {"database": target_database, "table": table_name},
        )
    }
    for name, column_type in table_columns.items():
        if not should_auto_index(name) or name in indexed:
            continue
        index_name = f"idx_{name}"[:64]
        prefix = index_prefix_for_type(column_type)
        try:
            connection.execute(
                text(
                    f"ALTER TABLE {quote_identifier(target_database)}.{quote_identifier(table_name)} "
                    f"ADD INDEX {quote_identifier(index_name)} ({quote_identifier(name)}{prefix})"
                )
            )
            connection.commit()
            created.append(index_name)
        except Exception as exc:
            logger.warning(
                "índice falhou %s.%s.%s: %s",
                target_database,
                table_name,
                name,
                exc,
            )
    return created


def ensure_connector_indexes(
    target_database: str,
    table_names: list[str] | None = None,
) -> dict[str, list[str]]:
    """Garante índices JOIN em tabelas de conector, sem threshold de volume."""
    if not IDENTIFIER_RE.match(target_database):
        raise ValueError(f"Database inválido: {target_database}")

    result: dict[str, list[str]] = {}
    with local_engine.connect() as connection:
        if table_names is None:
            rows = connection.execute(
                text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE'"
                ),
                {"db": target_database},
            )
            table_names = [row[0] for row in rows]
        for table_name in table_names:
            if not IDENTIFIER_RE.match(table_name):
                logger.warning("ensure_connector_indexes: tabela inválida ignorada: %s", table_name)
                continue
            created = _create_missing_indexes(connection, target_database, table_name)
            if created:
                result[table_name] = created
                logger.info("ensure_connector_indexes: %s.%s → %s", target_database, table_name, created)
    return result


def get_existing_tables(target_database: str) -> list[str]:
    """Retorna as tabelas base já existentes no database destino."""
    if not IDENTIFIER_RE.match(target_database):
        raise ValueError(f"Database inválido: {target_database}")
    with local_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :db AND TABLE_TYPE = 'BASE TABLE'"
            ),
            {"db": target_database},
        )
        return [row[0] for row in rows]


def get_target_max_value(
    target_database: str,
    table_name: str,
    column_name: str,
):
    """Obtém o maior watermark numérico já materializado no destino."""
    for identifier in (target_database, table_name, column_name):
        if not IDENTIFIER_RE.match(identifier):
            raise ValueError(f"Identificador invalido: {identifier}")
    with local_engine.connect() as connection:
        return connection.execute(
            text(
                f"SELECT MAX({quote_identifier(column_name)}) "
                f"FROM {quote_identifier(target_database)}.{quote_identifier(table_name)}"
            )
        ).scalar_one_or_none()


def auto_create_connector_indexes(target_database: str, table_name: str) -> list[str]:
    """Wrapper de compatibilidade para indexar uma tabela de conector."""
    result = ensure_connector_indexes(target_database, [table_name])
    return result.get(table_name, [])


def resolve_incremental_column(
    sync_table: ConnectorSyncTable,
    columns: Iterable[str],
    adapter: SourceAdapter | None = None,
) -> Optional[str]:
    available_columns = set(columns)
    if sync_table.incremental_column in available_columns:
        return sync_table.incremental_column

    candidates = adapter.get_incremental_candidates() if adapter else ("date_mod", "date_creation", "date")
    for candidate in candidates:
        if candidate in available_columns:
            return candidate
    return None


def build_select(
    config: ConnectorConfig,
    sync_table: ConnectorSyncTable,
    mode: str,
    columns: Iterable[str],
    adapter: SourceAdapter,
    since_override=None,
    initial_filter_clause: str | None = None,
    initial_incremental_column: str | None = None,
) -> tuple[str, dict, Optional[str]]:
    params = {}
    if initial_filter_clause:
        incremental_column = initial_incremental_column or resolve_incremental_column(sync_table, columns, adapter)
        if not incremental_column:
            return adapter.get_full_query(config, sync_table.table_name), params, None
        table_sql = adapter.qualified_table(config, sync_table.table_name)
        column_sql = adapter.quote_identifier(incremental_column)
        return (
            f"SELECT * FROM {table_sql} WHERE {initial_filter_clause} ORDER BY {column_sql}",
            params,
            incremental_column,
        )
    if since_override is not None or (
        mode == "incremental"
        and sync_table.incremental_column
        and sync_table.last_success_at
    ):
        incremental_column = resolve_incremental_column(sync_table, columns, adapter)
        if not incremental_column:
            return adapter.get_full_query(config, sync_table.table_name), params, None
        since = (
            since_override
            if since_override is not None
            else utc_naive_to_local(sync_table.last_success_at)
        )
        select_sql, params = adapter.get_incremental_query(config, sync_table.table_name, incremental_column, since)
        return select_sql, params, incremental_column
    return adapter.get_full_query(config, sync_table.table_name), params, None


def run_table_import(db: Session, sync_table: ConnectorSyncTable, mode: str) -> ConnectorRun:
    if mode not in {"full", "incremental"}:
        raise ValueError("Modo de importacao invalido.")
    if not sync_table.is_active:
        raise ValueError("Tabela inativa.")

    sync_table.configured_load_type = sync_table.load_type
    sync_table.effective_load_type = None
    sync_table.effective_incremental_column = None

    config = get_connection_config(db, sync_table.connector_type)
    if not config:
        raise ValueError(f"Conector ativo nao encontrado: {sync_table.connector_type}")
    target_database = target_import_database(config)
    target_ready, target_error = ensure_target_database(config, local_engine)
    if not target_ready:
        started = time.perf_counter()
        now = datetime.utcnow()
        message = target_error or f"Database destino {target_database} indisponivel."
        run = ConnectorRun(
            connector_type=sync_table.connector_type,
            sync_table_id=sync_table.id,
            table_name=sync_table.table_name,
            mode=mode,
            status="skipped",
            error_message=message,
            started_at=now,
            finished_at=now,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        sync_table.last_run_at = now
        sync_table.last_error = message
        db.add(run)
        db.commit()
        db.refresh(run)
        log_import(db, "warning", f"{sync_table.table_name}: {message}", run.id)
        return run
    engine = build_source_engine(config)
    adapter = get_adapter(config.db_type)
    started = time.perf_counter()
    now = datetime.utcnow()
    run = ConnectorRun(
        connector_type=sync_table.connector_type,
        sync_table_id=sync_table.id,
        table_name=sync_table.table_name,
        mode=mode,
        status="running",
        started_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    logger.info("ETL iniciado: tabela=%s modo=%s run_id=%s", sync_table.table_name, mode, run.id)

    imported = 0
    try:
        columns = get_table_columns(engine, config, sync_table.table_name, adapter)
        if adapter.is_large_table(sync_table.table_name) and not sync_table.last_success_at and not config.initial_days:
            message = (
                "Tabela volumosa requer janela inicial configurada. "
                "Configure initial_days no conector."
            )
            finished = datetime.utcnow()
            run.status = "skipped"
            run.error_message = message
            run.finished_at = finished
            run.duration_ms = int((time.perf_counter() - started) * 1000)
            sync_table.last_run_at = finished
            sync_table.last_error = message
            db.commit()
            log_import(db, "warning", f"{sync_table.table_name}: {message}", run.id)
            return run
        column_info: list[dict] | None = None
        use_row_hash = False
        if isinstance(adapter, PostgreSQLAdapter):
            source_schema = (config.schema_name or "public").strip() or "public"
            with engine.connect() as connection:
                column_info = adapter.get_column_info(
                    connection,
                    source_schema,
                    sync_table.table_name,
                )
            columns = [column["column_name"] for column in column_info]
            if not columns:
                raise ValueError(
                    f"Nenhuma coluna encontrada em {source_schema}.{sync_table.table_name}."
                )
        since_override = None
        initial_filter_clause = None
        initial_incremental_column = None
        if not sync_table.last_success_at and config.initial_days and int(config.initial_days) > 0:
            initial_incremental_column = resolve_incremental_column(sync_table, columns, adapter)
            if initial_incremental_column:
                schema = config.schema_name or config.database_name
                if (config.db_type or "").lower() == "postgresql":
                    schema = config.schema_name or "public"
                if column_info is not None:
                    incremental_info = next(
                        (
                            column
                            for column in column_info
                            if column["column_name"] == initial_incremental_column
                        ),
                        None,
                    )
                    if incremental_info and incremental_info["is_unix_timestamp"]:
                        column_type = "unix_timestamp"
                    elif (
                        incremental_info
                        and incremental_info["pg_type"] in {
                            "smallint", "integer", "bigint", "int2", "int4", "int8"
                        }
                    ):
                        column_type = "numeric_watermark"
                    else:
                        column_type = "datetime"
                else:
                    with engine.connect() as connection:
                        column_type = detect_column_type(
                            connection,
                            sync_table.table_name,
                            initial_incremental_column,
                            config.db_type,
                            schema,
                        )
                if column_type != "numeric_watermark":
                    initial_filter_clause = adapter.build_initial_days_filter(
                        incremental_column=initial_incremental_column,
                        initial_days=int(config.initial_days),
                        column_type=column_type,
                    )
        if column_info is not None:
            use_row_hash = ensure_postgresql_import_table(
                target_database,
                sync_table.table_name,
                column_info,
            )
        else:
            ensure_import_table(target_database, sync_table.table_name, columns)
        if (
            mode == "incremental"
            and sync_table.last_success_at
            and column_info is not None
        ):
            resolved_column = resolve_incremental_column(sync_table, columns, adapter)
            resolved_info = next(
                (
                    column
                    for column in column_info
                    if column["column_name"] == resolved_column
                ),
                None,
            )
            if (
                resolved_info
                and resolved_info["pg_type"] in {
                    "smallint", "integer", "bigint", "int2", "int4", "int8"
                }
                and not resolved_info["is_unix_timestamp"]
            ):
                since_override = get_target_max_value(
                    target_database,
                    sync_table.table_name,
                    resolved_column,
                )
                if since_override is None:
                    since_override = 0
        previous_incremental_column = sync_table.incremental_column
        if (
            mode == "incremental"
            and previous_incremental_column
            and previous_incremental_column not in columns
        ):
            resolved_incremental_column = resolve_incremental_column(sync_table, columns, adapter)
            if resolved_incremental_column:
                sync_table.incremental_column = resolved_incremental_column
                log_import(
                    db,
                    "warning",
                    (
                        f"{sync_table.table_name}: coluna incremental '{previous_incremental_column}' "
                        f"nao existe na origem; usando '{resolved_incremental_column}'."
                    ),
                    run.id,
                )
            else:
                sync_table.incremental_column = None
                sync_table.load_type = "full"
                log_import(
                    db,
                    "warning",
                    (
                        f"{sync_table.table_name}: coluna incremental '{previous_incremental_column}' "
                        "nao existe na origem; executando carga completa."
                    ),
                    run.id,
                )
        select_sql, params, effective_incremental_column = build_select(
            config,
            sync_table,
            mode,
            columns,
            adapter,
            since_override,
            initial_filter_clause,
            initial_incremental_column,
        )
        sync_table.effective_load_type = (
            "incremental" if effective_incremental_column else "full"
        )
        sync_table.effective_incremental_column = effective_incremental_column
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(text(select_sql), params)
            batch: list[dict] = []
            for row in result.mappings():
                batch.append(dict(row))
                if len(batch) >= BATCH_SIZE:
                    if column_info is not None:
                        imported += insert_postgresql_rows(
                            target_database,
                            sync_table.table_name,
                            batch,
                            column_info,
                            use_row_hash,
                        )
                    else:
                        imported += insert_rows(target_database, sync_table.table_name, batch, columns, adapter)
                    batch = []
            if column_info is not None:
                imported += insert_postgresql_rows(
                    target_database,
                    sync_table.table_name,
                    batch,
                    column_info,
                    use_row_hash,
                )
            else:
                imported += insert_rows(target_database, sync_table.table_name, batch, columns, adapter)

        if mode == "full":
            try:
                auto_create_connector_indexes(target_database, sync_table.table_name)
            except Exception as exc:
                logger.warning(
                    "auto-index pos-FULL falhou %s.%s: %s",
                    target_database,
                    sync_table.table_name,
                    exc,
                )

        finished = datetime.utcnow()
        run.status = "success"
        run.row_count = imported
        run.finished_at = finished
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        sync_table.last_run_at = finished
        sync_table.last_success_at = finished
        sync_table.row_count = imported
        sync_table.last_error = None
        db.commit()
        logger.info(
            "ETL concluído: tabela=%s modo=%s linhas=%s duração_ms=%s",
            sync_table.table_name,
            mode,
            imported,
            run.duration_ms,
        )
    except Exception as exc:
        finished = datetime.utcnow()
        run.status = "error"
        run.error_message = str(exc)
        run.finished_at = finished
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        sync_table.last_run_at = finished
        sync_table.last_error = str(exc)
        db.commit()
        log_import(db, "error", f"{sync_table.table_name}: {exc}", run.id)
        logger.error("ETL falhou: tabela=%s modo=%s erro=%s", sync_table.table_name, mode, exc)
    finally:
        engine.dispose()

    db.refresh(run)
    return run


def run_connector_import(
    db: Session,
    mode: str,
    table_id: int | None = None,
    connector_type: str | None = None,
) -> list[ConnectorRun]:
    if connector_type:
        clear_cancel(connector_type)
    active_connector_types_query = db.query(ConnectorConfig.connector_type).filter(ConnectorConfig.is_active.is_(True))
    blocked_connector_types: set[str] = set()
    if table_id is None:
        configs_query = db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True))
        if connector_type:
            configs_query = configs_query.filter(ConnectorConfig.connector_type == connector_type)
        for config in configs_query.order_by(ConnectorConfig.connector_type.asc()).all():
            success, target_error = ensure_target_database(config, local_engine)
            if not success:
                blocked_connector_types.add(config.connector_type)
                log_import(
                    db,
                    "warning",
                    target_error or f"Database destino {config.target_database} indisponivel.",
                )
                continue
            discover_source_tables(db, config)

    query = db.query(ConnectorSyncTable).filter(ConnectorSyncTable.is_active.is_(True))
    if connector_type:
        query = query.filter(ConnectorSyncTable.connector_type == connector_type)
    else:
        query = query.filter(ConnectorSyncTable.connector_type.in_(active_connector_types_query))
    if blocked_connector_types:
        query = query.filter(ConnectorSyncTable.connector_type.notin_(blocked_connector_types))
    if table_id:
        query = query.filter(ConnectorSyncTable.id == table_id)
    tables = query.order_by(ConnectorSyncTable.table_name.asc()).all()
    runs = []
    configs_by_type = {
        config.connector_type: config
        for config in db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True)).all()
    }
    if mode == "full":
        selected_connector_types = {table.connector_type for table in tables}
        target_databases = {
            target_import_database(config)
            for config in configs_by_type.values()
            if config.connector_type not in blocked_connector_types
            and config.connector_type in selected_connector_types
            and (not connector_type or config.connector_type == connector_type)
        }
        for target_database in target_databases:
            try:
                existing = get_existing_tables(target_database)
                if existing:
                    logger.info(
                        "pré-FULL: verificando índices em %s (%d tabelas existentes)",
                        target_database,
                        len(existing),
                    )
                    ensure_connector_indexes(target_database, existing)
            except Exception as exc:
                logger.warning("pré-FULL index check falhou em %s: %s", target_database, exc)
    for sync_table in tables:
        if is_cancelled(sync_table.connector_type):
            logger.info("ETL %s cancelado antes da tabela %s", sync_table.connector_type, sync_table.table_name)
            break
        config = configs_by_type.get(sync_table.connector_type)
        if config and config.import_mode == "custom" and config.whitelist_tables:
            if sync_table.table_name not in config.whitelist_tables:
                continue
        effective_mode = mode if mode == "full" else sync_table.load_type
        if mode == "incremental":
            effective_mode = "incremental"
        run = run_table_import(db, sync_table, effective_mode)
        runs.append(run)
        if is_cancelled(sync_table.connector_type):
            run.status = "cancelled"
            db.commit()
            logger.info("ETL %s cancelado após tabela %s", sync_table.connector_type, sync_table.table_name)
            break
    return runs
