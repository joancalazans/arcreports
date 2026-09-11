from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminActionLog, Dashboard, PortalGroup, Report, ReportCategory
from app.routes.common import form_data, render
from app.security import require_usuarios, verify_csrf


router = APIRouter()


def category_redirect(message: str, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(
        f"/admin/report-categories?{key}={quote(message)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def log_category_action(db: Session, user, action: str, category: ReportCategory | None, status_value: str, message: str) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            report_name=category.name if category else None,
            status=status_value,
            message=message,
        )
    )
    db.commit()


def category_usage(db: Session, category: ReportCategory) -> dict:
    report_count = db.query(Report).filter(Report.category == category.name).count()
    user_count = len(category.users)
    group_count = len(category.portal_groups)
    dashboard_count = db.query(Dashboard).join(Dashboard.categories).filter(ReportCategory.id == category.id).count()
    return {
        "report_count": report_count,
        "user_count": user_count,
        "group_count": group_count,
        "dashboard_count": dashboard_count,
        "can_delete": report_count == 0 and user_count == 0 and group_count == 0 and dashboard_count == 0,
    }


@router.get("/admin/report-categories", response_class=HTMLResponse)
def report_categories_page(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    categories = db.query(ReportCategory).order_by(ReportCategory.name.asc()).all()
    return render(
        request,
        "admin_report_categories.html",
        {
            "active": "report_categories",
            "categories": [{"category": category, **category_usage(db, category)} for category in categories],
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/report-categories", dependencies=[Depends(verify_csrf)])
async def create_report_category(request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    name = data.get("name", "").strip()
    if not name:
        return category_redirect("Informe o nome da categoria.", True)
    if db.query(ReportCategory).filter(ReportCategory.name == name).first():
        return category_redirect("Categoria ja cadastrada.", True)
    category = ReportCategory(name=name, is_active=data.get("is_active") == "on")
    db.add(category)
    db.commit()
    log_category_action(db, user, "create_report_category", category, "success", "Categoria criada.")
    return category_redirect("Categoria criada.")


@router.post("/admin/report-categories/{category_id}", dependencies=[Depends(verify_csrf)])
async def update_report_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    category = db.get(ReportCategory, category_id)
    if not category:
        return category_redirect("Categoria nao encontrada.", True)
    data = await form_data(request)
    new_name = data.get("name", "").strip()
    if not new_name:
        return category_redirect("Informe o nome da categoria.", True)
    duplicate = db.query(ReportCategory).filter(ReportCategory.name == new_name, ReportCategory.id != category.id).first()
    if duplicate:
        return category_redirect("Ja existe outra categoria com este nome.", True)
    old_name = category.name
    category.name = new_name
    category.is_active = data.get("is_active") == "on"
    if old_name != new_name:
        db.query(Report).filter(Report.category == old_name).update(
            {Report.category: new_name},
            synchronize_session=False,
        )
    db.commit()
    log_category_action(db, user, "update_report_category", category, "success", "Categoria atualizada.")
    return category_redirect("Categoria atualizada.")


@router.post("/admin/report-categories/{category_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_report_category(category_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_usuarios(request, db)
    if isinstance(user, RedirectResponse):
        return user
    category = db.get(ReportCategory, category_id)
    if not category:
        return category_redirect("Categoria nao encontrada.", True)
    usage = category_usage(db, category)
    if not usage["can_delete"]:
        message = "Exclusao bloqueada: categoria em uso por relatorios, usuarios, grupos ou dashboards."
        log_category_action(db, user, "delete_report_category", category, "blocked", message)
        return category_redirect(message, True)
    log_category_action(db, user, "category_delete", category, "success", "Categoria excluida.")
    db.delete(category)
    db.commit()
    return category_redirect("Categoria excluida.")
