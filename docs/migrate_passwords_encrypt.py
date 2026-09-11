#!/usr/bin/env python3
"""
Script de migracao: criptografar senhas existentes.
Executar UMA VEZ apos deploy do codigo novo.
Executar como: python docs/migrate_passwords_encrypt.py
"""
import os
import sys

sys.path.insert(0, "/opt/sites/glpi-portal")
os.chdir("/opt/sites/glpi-portal")

from app.config import settings
from app.crypto import encrypt_password, is_encrypted
from app.database import SessionLocal
from app.models import ConnectorConfig, LdapConfig


def migrate():
    db = SessionLocal()
    try:
        connectors = db.query(ConnectorConfig).all()
        for connector in connectors:
            if connector.password and not is_encrypted(connector.password):
                connector.password = encrypt_password(
                    connector.password, settings.encryption_key
                )
                print(f"Criptografado: conector {connector.name}")

        ldap_configs = db.query(LdapConfig).all()
        for ldap_config in ldap_configs:
            if ldap_config.bind_password and not is_encrypted(ldap_config.bind_password):
                ldap_config.bind_password = encrypt_password(
                    ldap_config.bind_password, settings.encryption_key
                )
                print(f"Criptografado: LDAP id={ldap_config.id}")

        db.commit()
        print("Migracao concluida com sucesso.")
    except Exception as exc:
        db.rollback()
        print(f"Erro na migracao: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
