from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from dotenv import dotenv_values
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_password, encrypt_password, is_encrypted
from app.models import LdapConfig


settings = get_settings()
LDAP_ENV_KEYS = (
    "LDAP_ENABLED",
    "LDAP_SERVER",
    "LDAP_PORT",
    "LDAP_USE_SSL",
    "LDAP_BASE_DN",
    "LDAP_BIND_DN",
    "LDAP_BIND_PASSWORD",
    "LDAP_USER_FILTER",
)
DEFAULT_USER_FILTER = "(sAMAccountName={username})"


@dataclass
class LdapOutcome:
    ok: bool
    message: str
    user_dn: str | None = None


def parse_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "sim"}


def ldap_error_message(exc: Exception) -> str:
    name = exc.__class__.__name__
    if name in {"LDAPBindError", "LDAPInvalidCredentialsResult"}:
        return "Credenciais LDAP rejeitadas."
    if name in {"LDAPSocketOpenError", "LDAPSocketReceiveError", "LDAPSocketSendError"}:
        return "Nao foi possivel conectar ao servidor LDAP."
    return "Falha na operacao LDAP."


def ldap_bind_result_message(result: dict | None) -> str:
    if not result:
        return "Credenciais LDAP rejeitadas."
    diagnostic = str(result.get("message") or "")
    match = re.search(r"\bdata\s+([0-9a-fA-F]+)\b", diagnostic)
    data_code = match.group(1).lower() if match else ""
    messages = {
        "52e": "Credenciais LDAP rejeitadas.",
        "532": "Senha da conta LDAP expirada.",
        "533": "Conta LDAP desabilitada.",
        "701": "Conta LDAP expirada.",
        "773": "Conta LDAP exige redefinicao de senha.",
        "775": "Conta LDAP bloqueada.",
    }
    return messages.get(data_code, "Credenciais LDAP rejeitadas.")


def get_ldap_config(db: Session) -> LdapConfig:
    config = db.query(LdapConfig).order_by(LdapConfig.id.asc()).first()
    if config:
        return config
    env = dotenv_values(settings.ldap_env_path)
    bind_password = env.get("LDAP_BIND_PASSWORD") or ""
    if bind_password and not is_encrypted(bind_password):
        bind_password = encrypt_password(bind_password, settings.encryption_key)
    config = LdapConfig(
        enabled=parse_bool(env.get("LDAP_ENABLED")),
        server=(env.get("LDAP_SERVER") or "").strip(),
        port=(env.get("LDAP_PORT") or "389").strip() or "389",
        use_ssl=parse_bool(env.get("LDAP_USE_SSL")),
        base_dn=(env.get("LDAP_BASE_DN") or "").strip(),
        bind_dn=(env.get("LDAP_BIND_DN") or "").strip(),
        bind_password=bind_password,
        user_filter=(env.get("LDAP_USER_FILTER") or DEFAULT_USER_FILTER).strip() or DEFAULT_USER_FILTER,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def config_values(config: LdapConfig) -> dict[str, str]:
    return {
        "LDAP_ENABLED": "true" if config.enabled else "false",
        "LDAP_SERVER": config.server,
        "LDAP_PORT": config.port,
        "LDAP_USE_SSL": "true" if config.use_ssl else "false",
        "LDAP_BASE_DN": config.base_dn,
        "LDAP_BIND_DN": config.bind_dn,
        "LDAP_BIND_PASSWORD": config.bind_password,
        "LDAP_USER_FILTER": config.user_filter or DEFAULT_USER_FILTER,
    }


def env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
    return f'"{escaped}"'


def sync_ldap_env(config: LdapConfig) -> None:
    path = Path(settings.ldap_env_path)
    values = config_values(config)
    content = "\n".join(f"{key}={env_value(values[key])}" for key in LDAP_ENV_KEYS) + "\n"
    path.write_text(content, encoding="utf-8")


def record_ldap_status(db: Session, config: LdapConfig, ok: bool, error: str | None = None) -> None:
    config.last_connection_status = "success" if ok else "error"
    config.last_connection_at = datetime.utcnow()
    config.last_error = None if ok else error
    db.commit()


def validate_config(config: LdapConfig) -> LdapOutcome | None:
    if not config.server or not config.base_dn or not config.bind_dn:
        return LdapOutcome(False, "Configuracao LDAP incompleta.")
    if not config.bind_password:
        return LdapOutcome(False, "Senha do bind LDAP nao configurada.")
    try:
        port = int(config.port)
    except ValueError:
        return LdapOutcome(False, "Porta LDAP invalida.")
    if port < 1 or port > 65535:
        return LdapOutcome(False, "Porta LDAP invalida.")
    return None


def ldap_library():
    try:
        from ldap3 import Connection, NONE, Server
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        return None
    return Connection, NONE, Server, LDAPException, escape_filter_chars


def user_search_filter(config: LdapConfig, username: str, escape_filter_chars) -> str:
    safe_username = escape_filter_chars(username)
    template = config.user_filter or DEFAULT_USER_FILTER
    if "{username}" in template:
        return template.replace("{username}", safe_username)
    return f"(&{template}(sAMAccountName={safe_username}))"


def search_user_dn(config: LdapConfig, username: str) -> LdapOutcome:
    validation = validate_config(config)
    if validation:
        return validation
    ldap = ldap_library()
    if not ldap:
        return LdapOutcome(False, "Dependencia LDAP indisponivel no servidor.")
    Connection, NONE, Server, LDAPException, escape_filter_chars = ldap
    try:
        server = Server(config.server, port=int(config.port), use_ssl=config.use_ssl, get_info=NONE, connect_timeout=5)
        plain_bind_password = decrypt_password(config.bind_password or "", settings.encryption_key)
        bind_connection = Connection(
            server,
            user=config.bind_dn,
            password=plain_bind_password,
            receive_timeout=5,
            raise_exceptions=False,
        )
        bind_connection.open()
        if not bind_connection.bind():
            message = ldap_bind_result_message(bind_connection.result)
            bind_connection.unbind()
            return LdapOutcome(False, message)
        try:
            found = bind_connection.search(
                config.base_dn,
                user_search_filter(config, username, escape_filter_chars),
                attributes=["distinguishedName"],
                size_limit=2,
            )
            if not found or len(bind_connection.entries) != 1:
                return LdapOutcome(False, "Usuario nao encontrado no LDAP.")
            return LdapOutcome(True, "Bind e busca LDAP concluidos.", str(bind_connection.entries[0].entry_dn))
        finally:
            bind_connection.unbind()
    except Exception as exc:
        return LdapOutcome(False, ldap_error_message(exc))


def authenticate_ldap_user(config: LdapConfig, username: str, password: str) -> LdapOutcome:
    if not password:
        return LdapOutcome(False, "Credenciais LDAP rejeitadas.")
    searched = search_user_dn(config, username)
    if not searched.ok or not searched.user_dn:
        return searched
    ldap = ldap_library()
    if not ldap:
        return LdapOutcome(False, "Dependencia LDAP indisponivel no servidor.")
    Connection, NONE, Server, LDAPException, _ = ldap
    try:
        server = Server(config.server, port=int(config.port), use_ssl=config.use_ssl, get_info=NONE, connect_timeout=5)
        user_connection = Connection(
            server,
            user=searched.user_dn,
            password=password,
            receive_timeout=5,
            raise_exceptions=False,
        )
        user_connection.open()
        if not user_connection.bind():
            message = ldap_bind_result_message(user_connection.result)
            user_connection.unbind()
            return LdapOutcome(False, message)
        user_connection.unbind()
        return LdapOutcome(True, "Autenticacao LDAP concluida.", searched.user_dn)
    except Exception as exc:
        return LdapOutcome(False, ldap_error_message(exc))
