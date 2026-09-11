import os
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from dotenv import dotenv_values, load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
DOTENV_PATH = BASE_DIR / ".env"
LOCAL_ENV_PATH = BASE_DIR / "local.env"
LDAP_ENV_PATH = BASE_DIR / "ldap.env"

load_dotenv(DOTENV_PATH)
load_dotenv(LDAP_ENV_PATH)
local_env = dotenv_values(LOCAL_ENV_PATH)


def require_secret_key() -> str:
    secret_key = os.getenv("SECRET_KEY") or os.getenv("APP_SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "ERRO: SECRET_KEY não definida. "
            "Defina SECRET_KEY no .env com no mínimo "
            "32 caracteres antes de iniciar."
        )
    if len(secret_key) < 32:
        raise RuntimeError(
            "ERRO: SECRET_KEY muito curta. "
            "Use no mínimo 32 caracteres."
        )
    return secret_key


def env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_name = "ArcReports"
    cookie_name = "reports_session"
    secret_key = require_secret_key()
    admin_username = (local_env.get("ADMIN_USERNAME") or "").strip()
    admin_password = local_env.get("ADMIN_PASSWORD") or ""
    max_rows = int(os.getenv("REPORT_MAX_ROWS", "500"))
    sql_console_enabled = env_bool("SQL_CONSOLE_ENABLED", True)
    ldap_env_path = LDAP_ENV_PATH

    local_db_host = os.getenv("LOCAL_DB_HOST", "localhost")
    local_db_name = os.getenv("LOCAL_DB_NAME", "reports")
    local_db_user = os.getenv("LOCAL_DB_USER", "")
    local_db_pass = os.getenv("LOCAL_DB_PASS", "")
    local_db_port = os.getenv("LOCAL_DB_PORT", "3306")
    local_db_admin_user = os.getenv("LOCAL_DB_ADMIN_USER", "")
    local_db_admin_pass = os.getenv("LOCAL_DB_ADMIN_PASS", "")
    # Credencial usada apenas para criar databases e conceder privilégios.
    # Quando ausente, o fluxo de provisionamento usa a credencial administrativa.
    local_db_provision_user: Optional[str] = os.getenv("LOCAL_DB_PROVISION_USER") or None
    local_db_provision_pass: Optional[str] = os.getenv("LOCAL_DB_PROVISION_PASS") or None
    local_db_user_admin_user = os.getenv("LOCAL_DB_USER_ADMIN_USER", "")
    local_db_user_admin_pass = os.getenv("LOCAL_DB_USER_ADMIN_PASS", "")

    glpi_db_host = os.getenv("GLPI_DB_HOST", "localhost")
    glpi_db_name = os.getenv("GLPI_DB_NAME", "glpi")
    glpi_db_user = os.getenv("GLPI_DB_USER", "")
    glpi_db_pass = os.getenv("GLPI_DB_PASS", "")
    glpi_db_port = os.getenv("GLPI_DB_PORT", "3306")

    @property
    def encryption_key(self) -> str:
        return self.secret_key

    @staticmethod
    def mysql_url(user: str, password: str, host: str, port: str, database: str) -> str:
        safe_user = quote_plus(user)
        safe_password = quote_plus(password)
        safe_database = quote_plus(database)
        return f"mysql+pymysql://{safe_user}:{safe_password}@{host}:{port}/{safe_database}?charset=utf8mb4"

    @property
    def local_database_url(self) -> str:
        return self.mysql_url(
            self.local_db_user,
            self.local_db_pass,
            self.local_db_host,
            self.local_db_port,
            self.local_db_name,
        )

    @property
    def glpi_database_url(self) -> str:
        return self.mysql_url(
            self.glpi_db_user,
            self.glpi_db_pass,
            self.glpi_db_host,
            self.glpi_db_port,
            self.glpi_db_name,
        )

    @property
    def local_admin_configured(self) -> bool:
        return bool(self.admin_username and self.admin_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
