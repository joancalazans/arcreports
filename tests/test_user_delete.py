import asyncio
from types import SimpleNamespace

from app.models import AdminActionLog, ReportExecution, User
from app.routes import admin_access
from app.security import hash_password


def _request():
    return SimpleNamespace()


def _user(db_session, username, *, is_active=False, is_admin=False, is_local=True, portal_usuarios=False):
    user = User(
        username=username,
        is_local=is_local,
        auth_source="local" if is_local else "ldap",
        is_admin=is_admin,
        is_active=is_active,
        portal_usuarios=portal_usuarios,
        password_hash=hash_password("Senha@123") if is_local else None,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _delete(monkeypatch, db_session, admin_user, target_user):
    monkeypatch.setattr(admin_access, "require_usuarios", lambda request, db: admin_user)
    return asyncio.run(admin_access.delete_user_access(target_user.id, _request(), db_session))


def test_delete_inactive_user_succeeds_and_logs(monkeypatch, db_session, admin_local_user):
    target = _user(db_session, "inactive_user", is_active=False)

    response = _delete(monkeypatch, db_session, admin_local_user, target)

    assert response.status_code == 303
    assert "message=" in response.headers["location"]
    assert db_session.get(User, target.id) is None
    log = db_session.query(AdminActionLog).filter_by(action="user_delete").one()
    assert log.user_id == admin_local_user.id
    assert log.username == admin_local_user.username
    assert log.message == "Usuário inactive_user excluído"


def test_delete_active_user_is_blocked(monkeypatch, db_session, admin_local_user):
    target = _user(db_session, "active_user", is_active=True)

    response = _delete(monkeypatch, db_session, admin_local_user, target)

    assert response.status_code == 303
    assert "Desative" in response.headers["location"]
    assert db_session.get(User, target.id) is not None


def test_delete_self_is_blocked(monkeypatch, db_session, admin_local_user):
    response = _delete(monkeypatch, db_session, admin_local_user, admin_local_user)

    assert response.status_code == 303
    assert "pr%C3%B3pria%20conta" in response.headers["location"]
    assert db_session.get(User, admin_local_user.id) is not None


def test_delete_only_local_admin_is_blocked(monkeypatch, db_session):
    operator = _user(db_session, "operator", is_active=True, is_local=False, portal_usuarios=True)
    target = _user(db_session, "inactive_admin", is_active=False, is_admin=True, is_local=True)

    response = _delete(monkeypatch, db_session, operator, target)

    assert response.status_code == 303
    assert "%C3%BAnico%20administrador%20local" in response.headers["location"]
    assert db_session.get(User, target.id) is not None


def test_delete_user_preserves_report_execution(monkeypatch, db_session, admin_local_user):
    target = _user(db_session, "history_user", is_active=False)
    execution = ReportExecution(
        user_id=target.id,
        sql_query="SELECT 1",
        status="success",
        row_count=1,
        duration_ms=10,
    )
    db_session.add(execution)
    db_session.commit()

    response = _delete(monkeypatch, db_session, admin_local_user, target)

    assert response.status_code == 303
    stored_execution = db_session.get(ReportExecution, execution.id)
    assert stored_execution is not None
    assert stored_execution.user_id is None
