from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.crypto import encrypt_password, is_encrypted
from app.ldap_auth import (
    DEFAULT_USER_FILTER,
    authenticate_ldap_user,
    get_ldap_config,
    record_ldap_status,
    search_user_dn,
    sync_ldap_env,
)
from app.models import AdminActionLog, PortalGroup, ReportCategory, ReportExecution, User, user_portal_groups, user_report_categories
from app.routes.common import form_data, form_lists, render
from app.security import ensure_portal_groups, hash_password, require_usuarios, verify_csrf


router = APIRouter()
LDAP_GROUPS = ("portal_admin", "portal_view")
PERMISSION_FIELDS = ("portal_relatorios", "portal_dashboard", "portal_importacao", "portal_usuarios")
PROFILE_FORM_FIELDS = ("portal_admin", *PERMISSION_FIELDS, "portal_view")


def checkbox_selected(data: dict[str, str], field_name: str) -> bool:
    return data.get(field_name) == "on"


def groups_by_name(db: Session) -> dict[str, PortalGroup]:
    ensure_portal_groups(db)
    return {group.name: group for group in db.query(PortalGroup).filter(PortalGroup.name.in_(LDAP_GROUPS)).all()}


def selected_groups(data: dict[str, str], available: dict[str, PortalGroup]) -> list[PortalGroup]:
    return [available[name] for name in LDAP_GROUPS if checkbox_selected(data, name) and name in available]


def selected_permission_flags(data: dict[str, str]) -> dict[str, bool]:
    return {field: checkbox_selected(data, field) for field in PERMISSION_FIELDS}


def has_any_permission(data: dict[str, str]) -> bool:
    return any(checkbox_selected(data, field) for field in PROFILE_FORM_FIELDS)


def apply_permission_flags(user: User, data: dict[str, str]) -> None:
    for field, value in selected_permission_flags(data).items():
        setattr(user, field, value)


def is_portal_admin_group_selected(groups: list[PortalGroup]) -> bool:
    return any(group.name == "portal_admin" for group in groups)


def is_last_local_admin(db: Session, exclude_user_id: int | None = None) -> bool:
    query = db.query(User).filter(
        User.is_admin.is_(True),
        User.is_active.is_(True),
        User.auth_source != "scheduler",
    )
    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    return query.count() == 0


def is_only_active_local_admin(db: Session, user: User) -> bool:
    return bool(user.is_local and user.is_admin and user.is_active and is_last_local_admin(db, user.id))


def log_admin_access_action(db: Session, admin: User, action: str, target: User | None, status_value: str, message: str) -> None:
    db.add(
        AdminActionLog(
            user_id=admin.id,
            username=admin.username,
            action=action,
            report_id=None,
            report_name=None,
            table_name=None,
            status=status_value,
            message=f"{target.username if target else '-'}: {message}",
        )
    )
    db.commit()


def users_groups_redirect(success: bool, message: str) -> RedirectResponse:
    key = "message" if success else "error"
    return RedirectResponse(f"/admin/users-groups?{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


def active_categories(db: Session) -> list[ReportCategory]:
    return db.query(ReportCategory).filter(ReportCategory.is_active.is_(True)).order_by(ReportCategory.name.asc()).all()


def selected_categories(db: Session, values: list[str]) -> list[ReportCategory]:
    category_ids = []
    for value in values:
        try:
            category_ids.append(int(value))
        except ValueError:
            continue
    if not category_ids:
        return []
    return (
        db.query(ReportCategory)
        .filter(ReportCategory.id.in_(category_ids), ReportCategory.is_active.is_(True))
        .order_by(ReportCategory.name.asc())
        .all()
    )


async def parsed_form_lists(request: Request) -> dict[str, list[str]]:
    return await form_lists(request)


@router.get("/admin/ldap", response_class=HTMLResponse)
def ldap_page(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return render(
        request,
        "admin_ldap.html",
        {
            "active": "ldap",
            "config": get_ldap_config(db),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/ldap", dependencies=[Depends(verify_csrf)])
async def save_ldap_config(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    config = get_ldap_config(db)
    config.enabled = data.get("enabled") == "on"
    config.server = data.get("server", "").strip()
    config.port = data.get("port", "389").strip() or "389"
    config.use_ssl = data.get("use_ssl") == "on"
    config.base_dn = data.get("base_dn", "").strip()
    config.bind_dn = data.get("bind_dn", "").strip()
    config.user_filter = data.get("user_filter", "").strip() or DEFAULT_USER_FILTER
    bind_password = data.get("bind_password", "")
    if bind_password:
        config.bind_password = (
            bind_password
            if is_encrypted(bind_password)
            else encrypt_password(bind_password, settings.encryption_key)
        )
    db.commit()
    sync_ldap_env(config)
    return RedirectResponse("/admin/ldap?message=Configuracao%20LDAP%20salva.", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/ldap/test", dependencies=[Depends(verify_csrf)])
async def test_ldap(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    username = data.get("username", "").strip()
    if not username:
        return RedirectResponse(
            "/admin/ldap?error=Informe%20um%20usuario%20para%20testar%20a%20busca%20LDAP.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    config = get_ldap_config(db)
    outcome = search_user_dn(config, username)
    record_ldap_status(db, config, outcome.ok, None if outcome.ok else outcome.message)
    key = "message" if outcome.ok else "error"
    message = "Bind e busca LDAP concluidos." if outcome.ok else outcome.message
    return RedirectResponse(f"/admin/ldap?{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/ldap/test-auth", dependencies=[Depends(verify_csrf)])
async def test_ldap_auth(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return RedirectResponse(
            "/admin/ldap?error=Informe%20usuario%20e%20senha%20para%20testar%20a%20autenticacao.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    config = get_ldap_config(db)
    outcome = authenticate_ldap_user(config, username, password)
    record_ldap_status(db, config, outcome.ok, None if outcome.ok else outcome.message)
    key = "message" if outcome.ok else "error"
    message = "Autenticacao LDAP concluida." if outcome.ok else outcome.message
    return RedirectResponse(f"/admin/ldap?{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/users-groups", response_class=HTMLResponse)
def users_groups_page(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    users = db.query(User).filter(User.auth_source != "scheduler").order_by(User.username.asc()).all()
    return render(
        request,
        "admin_users_groups.html",
        {
            "active": "users_groups",
            "users": users,
            "categories": active_categories(db),
            "last_local_admin_id": next((item.id for item in users if is_only_active_local_admin(db, item)), None),
            "delete_blocked_admin_ids": {
                item.id for item in users if item.is_admin and is_last_local_admin(db, item.id)
            },
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/users-groups", dependencies=[Depends(verify_csrf)])
async def create_domain_user(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    parsed = await parsed_form_lists(request)
    data = {key: values[0] for key, values in parsed.items()}
    username = data.get("username", "").strip()
    if not username:
        return RedirectResponse("/admin/users-groups?error=Informe%20o%20usuario%20de%20dominio.", status_code=303)
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return RedirectResponse("/admin/users-groups?error=Usuario%20ja%20cadastrado.", status_code=303)
    available = groups_by_name(db)
    groups = selected_groups(data, available)
    domain_user = User(
        username=username,
        password_hash=None,
        auth_source="ldap",
        is_local=False,
        is_active=data.get("is_active") == "on",
        is_admin=is_portal_admin_group_selected(groups),
        portal_groups=groups,
        **selected_permission_flags(data),
        report_categories=selected_categories(db, parsed.get("category_ids", [])),
    )
    db.add(domain_user)
    db.commit()
    log_admin_access_action(db, user, "create_domain_user", domain_user, "success", "Usuario de dominio cadastrado.")
    return RedirectResponse("/admin/users-groups?message=Usuario%20de%20dominio%20cadastrado.", status_code=303)


@router.post("/admin/users-groups/local", dependencies=[Depends(verify_csrf)])
async def create_local_user(request: Request, db: Session = Depends(get_db)):
    admin = require_usuarios(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    parsed = await parsed_form_lists(request)
    data = {key: values[0] for key, values in parsed.items()}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    confirm_password = data.get("confirm_password", "")
    if not username:
        return users_groups_redirect(False, "Informe o usuario local.")
    if db.query(User).filter(User.username == username).first():
        return users_groups_redirect(False, "Usuario ja cadastrado.")
    if len(password) < 8:
        return users_groups_redirect(False, "A senha deve ter no minimo 8 caracteres.")
    if password != confirm_password:
        return users_groups_redirect(False, "Senha e confirmacao devem ser iguais.")
    groups = selected_groups(data, groups_by_name(db))
    if not has_any_permission(data):
        return users_groups_redirect(False, "Selecione ao menos uma permissao.")
    local_user = User(
        username=username,
        password_hash=hash_password(password),
        auth_source="local",
        is_local=True,
        is_active=data.get("is_active") == "on",
        is_admin=is_portal_admin_group_selected(groups),
        portal_groups=groups,
        **selected_permission_flags(data),
        report_categories=selected_categories(db, parsed.get("category_ids", [])),
    )
    db.add(local_user)
    db.commit()
    log_admin_access_action(db, admin, "create_local_user", local_user, "success", "Usuario local cadastrado.")
    return users_groups_redirect(True, "Usuario local cadastrado.")


@router.post("/admin/users-groups/{user_id}", dependencies=[Depends(verify_csrf)])
async def update_user_access(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_usuarios(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    managed_user = db.get(User, user_id)
    if not managed_user or managed_user.auth_source == "scheduler":
        return RedirectResponse("/admin/users-groups?error=Usuario%20nao%20encontrado.", status_code=303)
    parsed = await parsed_form_lists(request)
    data = {key: values[0] for key, values in parsed.items()}
    groups = selected_groups(data, groups_by_name(db))
    new_is_admin = is_portal_admin_group_selected(groups)
    new_is_active = data.get("is_active") == "on"
    if is_only_active_local_admin(db, managed_user) and (not new_is_admin or not new_is_active):
        log_admin_access_action(
            db,
            admin,
            "update_user_access",
            managed_user,
            "blocked",
            "Nao e possivel remover o ultimo administrador local.",
        )
        return users_groups_redirect(False, "Não é possível remover o último administrador local.")
    managed_user.portal_groups = groups
    managed_user.report_categories = selected_categories(db, parsed.get("category_ids", []))
    managed_user.is_admin = new_is_admin
    apply_permission_flags(managed_user, data)
    managed_user.is_active = new_is_active
    if not managed_user.is_local:
        managed_user.password_hash = None
    db.commit()
    log_admin_access_action(db, admin, "update_user_access", managed_user, "success", "Acesso do usuario atualizado.")
    return RedirectResponse("/admin/users-groups?message=Acesso%20do%20usuario%20atualizado.", status_code=303)


@router.post("/admin/users-groups/{user_id}/delete", dependencies=[Depends(verify_csrf)])
async def delete_user_access(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_usuarios(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    managed_user = db.get(User, user_id)
    if not managed_user or managed_user.auth_source == "scheduler":
        return users_groups_redirect(False, "Usuario nao encontrado.")
    if managed_user.id == admin.id:
        return users_groups_redirect(False, "Você não pode excluir sua própria conta.")
    if managed_user.is_active:
        return users_groups_redirect(False, "Desative o usuário antes de excluir.")
    if managed_user.is_admin and is_last_local_admin(db, managed_user.id):
        return users_groups_redirect(False, "Não é possível excluir o único administrador local do sistema.")

    username = managed_user.username
    db.execute(delete(user_portal_groups).where(user_portal_groups.c.user_id == managed_user.id))
    db.execute(delete(user_report_categories).where(user_report_categories.c.user_id == managed_user.id))
    db.query(ReportExecution).filter(ReportExecution.user_id == managed_user.id).update(
        {ReportExecution.user_id: None},
        synchronize_session=False,
    )
    db.query(AdminActionLog).filter(AdminActionLog.user_id == managed_user.id).update(
        {AdminActionLog.user_id: None},
        synchronize_session=False,
    )
    db.delete(managed_user)
    db.flush()
    db.add(
        AdminActionLog(
            user_id=admin.id,
            username=admin.username,
            action="user_delete",
            status="success",
            message=f"Usuário {username} excluído",
        )
    )
    db.commit()
    return users_groups_redirect(True, f"Usuário {username} excluído.")


@router.post("/admin/users-groups/local/{user_id}/password", dependencies=[Depends(verify_csrf)])
async def update_local_user_password(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_usuarios(request, db)
    if isinstance(admin, RedirectResponse):
        return admin
    managed_user = db.get(User, user_id)
    if not managed_user or not managed_user.is_local:
        return users_groups_redirect(False, "Nao e permitido alterar senha de usuario de dominio.")
    data = await form_data(request)
    new_password = data.get("new_password", "")
    confirm_password = data.get("confirm_password", "")
    if len(new_password) < 8:
        return users_groups_redirect(False, "A nova senha deve ter no minimo 8 caracteres.")
    if new_password != confirm_password:
        return users_groups_redirect(False, "Senha e confirmacao devem ser iguais.")
    managed_user.password_hash = hash_password(new_password)
    db.commit()
    log_admin_access_action(db, admin, "update_local_user_password", managed_user, "success", "Senha local alterada.")
    return users_groups_redirect(True, "Senha do usuario local alterada.")
