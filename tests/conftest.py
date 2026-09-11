import base64
import hashlib
import hmac
import json
import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


os.environ.setdefault("SECRET_KEY", "x" * 48)

from app.database import Base
from app.models import PortalGroup, User
from app.security import create_session_token, generate_csrf_token, hash_password, settings


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_sqlite(type_, compiler, **kw):
    return "TEXT"


@pytest.fixture(autouse=True)
def reports_database_name(monkeypatch):
    """Testa o default da nova instalação sem depender do .env de homologação."""
    monkeypatch.setattr(settings, "local_db_name", "reports")
    monkeypatch.setattr("app.reporting.ALLOWED_REPORT_DATABASES", {"reports"})


@pytest.fixture
def secret_key(monkeypatch):
    value = "s" * 48
    monkeypatch.setenv("SECRET_KEY", value)
    settings.secret_key = value
    return value


@pytest.fixture
def session_token(secret_key):
    return create_session_token(123)


@pytest.fixture
def expired_session_token(secret_key):
    payload = {"user_id": 123, "iat": int(time.time()) - (24 * 3600)}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(secret_key.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload_json}:{signature}".encode()).decode()


@pytest.fixture
def csrf_token(session_token):
    return generate_csrf_token(session_token)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _portal_group(db_session, name):
    group = PortalGroup(name=name)
    db_session.add(group)
    db_session.flush()
    return group


@pytest.fixture
def admin_local_user(db_session):
    group = _portal_group(db_session, "portal_admin")
    user = User(
        username="admin_local",
        is_local=True,
        auth_source="local",
        is_admin=True,
        is_active=True,
        password_hash=hash_password("Senha@123"),
        portal_groups=[group],
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def view_user(db_session):
    group = _portal_group(db_session, "portal_view")
    user = User(
        username="view_user",
        is_local=True,
        auth_source="local",
        is_admin=False,
        is_active=True,
        password_hash=hash_password("Senha@123"),
        portal_groups=[group],
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user
