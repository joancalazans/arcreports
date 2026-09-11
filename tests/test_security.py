import base64
import importlib
import json

import pytest

from app import config
from app.security import (
    create_session_token,
    generate_csrf_token,
    hash_password,
    read_session_token,
    verify_csrf_token,
    verify_password,
)


def _decode_session_payload(token):
    decoded = base64.urlsafe_b64decode(token.encode()).decode()
    payload_json, _ = decoded.rsplit(":", 1)
    return json.loads(payload_json)


def test_hash_password_returns_string_different_from_original():
    assert hash_password("Senha@123") != "Senha@123"


def test_verify_password_accepts_correct_password():
    stored_hash = hash_password("Senha@123")
    assert verify_password("Senha@123", stored_hash) is True


def test_verify_password_rejects_wrong_password():
    stored_hash = hash_password("Senha@123")
    assert verify_password("errada", stored_hash) is False


def test_hash_password_uses_random_salt():
    assert hash_password("Senha@123") != hash_password("Senha@123")


def test_create_session_token_returns_non_empty_string(session_token):
    assert isinstance(session_token, str)
    assert session_token


def test_read_session_token_returns_user_id_for_fresh_token(session_token):
    assert read_session_token(session_token) == 123


def test_read_session_token_returns_none_for_tampered_token(session_token):
    tampered = session_token[:-2] + "xx"
    assert read_session_token(tampered) is None


def test_read_session_token_returns_none_for_expired_token(expired_session_token):
    assert read_session_token(expired_session_token) is None


def test_session_token_payload_contains_iat(session_token):
    payload = _decode_session_payload(session_token)
    assert "iat" in payload


def test_generate_csrf_token_returns_non_empty_string(session_token):
    assert generate_csrf_token(session_token)


def test_verify_csrf_token_accepts_same_session(session_token, csrf_token):
    assert verify_csrf_token(session_token, csrf_token) is True


def test_verify_csrf_token_rejects_different_session(csrf_token):
    other_session = create_session_token(456)
    assert verify_csrf_token(other_session, csrf_token) is False


def test_verify_csrf_token_rejects_tampered_token(session_token, csrf_token):
    assert verify_csrf_token(session_token, csrf_token[:-2] + "xx") is False


def test_generate_csrf_token_is_deterministic(session_token):
    assert generate_csrf_token(session_token) == generate_csrf_token(session_token)


def test_require_secret_key_raises_when_missing(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        config.require_secret_key()


def test_require_secret_key_raises_when_too_short(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "curta")
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        config.require_secret_key()


def test_require_secret_key_accepts_valid_value(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "v" * 48)
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    importlib.reload(config)
    assert config.get_settings().secret_key == "v" * 48
