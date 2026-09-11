import os
import sys
from logging.config import fileConfig
from urllib.parse import quote_plus

from sqlalchemy import create_engine, pool

from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# O Alembic gerencia somente as tabelas de sistema declaradas nos modelos ORM.
# A allowlist também impede que tabelas de relatórios fora dos prefixos conhecidos
# sejam interpretadas pelo autogenerate como candidatas a remoção.
MANAGED_TABLES = {
    "users",
    "reports",
    "report_categories",
    "report_executions",
    "dashboards",
    "dashboard_categories",
    "dashboard_widgets",
    "dashboard_sources",
    "system_config",
    "admin_action_logs",
    "auth_logs",
    "connector_configs",
    "connector_runs",
    "connector_sync_tables",
    "ldap_configs",
    "portal_groups",
    "portal_group_report_categories",
    "user_portal_groups",
    "user_report_categories",
    "db_users",
    "db_user_databases",
    "glpi_import_logs",
    "temp_report_results",
}

EXCLUDE_PREFIXES = (
    "memora_",
    "sti_",
    "zabbix_",
    "glpi_",
    "tmp_report_",
    "redmine_",
    "bookstack_",
    "custom_",
)
EXCLUDE_EXACT = {"dashboard_1ano", "alembic_version"}


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in MANAGED_TABLES
    return True


def get_url() -> str:
    return (
        "mysql+pymysql://"
        f"{quote_plus(settings.local_db_user)}"
        f":{quote_plus(settings.local_db_pass)}"
        f"@{settings.local_db_host}"
        f":{settings.local_db_port}"
        f"/{settings.local_db_name}"
    )


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = create_engine(get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
