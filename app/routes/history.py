from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminActionLog, AuthLog, ReportExecution
from app.routes.common import render
from app.security import require_admin, require_history
from app.system_config import get_system_config_value


router = APIRouter()
PER_PAGE = 50
DEFAULT_LOG_RETENTION_DAYS = 90


ACTION_LABELS = {
    "duplicate_report": "Relatorio duplicado",
    "toggle_primary": "Relatorio principal alterado",
    "appearance_update": "Personalizacao atualizada",
    "glpi_schedule_toggle": "Agendamento GLPI alterado",
    "toggle_source": "Fonte alternada",
    "enable_source": "Fonte habilitada",
    "run_report": "Relatorio executado",
    "schedule_run_now": "Execucao manual do agendamento",
    "bulk_export": "Exportacao em massa",
    "bulk_run": "Execucao em massa",
    "bulk_clear": "Limpeza em massa",
    "bulk_activate": "Ativacao em massa",
    "bulk_deactivate": "Desativacao em massa",
    "bulk_delete": "Exclusao em massa",
    "clear": "Tabela limpa",
    "recreate": "Tabela recriada",
    "delete": "Registro excluido",
    "export": "Exportacao solicitada",
    "refresh": "Atualizacao solicitada",
    "cleanup_temp_reports": "Limpeza temporaria executada",
    "create_dashboard": "Dashboard criado",
    "update_dashboard": "Dashboard atualizado",
    "duplicate_dashboard": "Dashboard duplicado",
    "delete_dashboard": "Dashboard excluido",
    "create_report_category": "Categoria criada",
    "update_report_category": "Categoria atualizada",
    "delete_report_category": "Categoria excluida",
    "create_domain_user": "Usuario de dominio cadastrado",
    "create_local_user": "Usuario local cadastrado",
    "update_user_access": "Acesso de usuario atualizado",
    "update_local_user_password": "Senha local alterada",
    "connector_delete": "Conector excluido",
    "connector_db_drop": "Banco do conector excluido",
    "user_delete": "Usuario excluido",
    "dashboard_source_delete": "Fonte de dashboard excluida",
    "report_delete": "Relatorio excluido",
    "dashboard_delete": "Dashboard excluido",
    "widget_delete": "Widget excluido",
    "category_delete": "Categoria excluida",
    "db_user_delete": "Usuario DB excluido",
    "db_user_remove_orphan": "Registro orfao de usuario DB removido",
    "connector_table_remove": "Tabela removida da whitelist",
}


EXCLUSAO_ACTIONS = {
    "connector_delete",
    "connector_db_drop",
    "user_delete",
    "dashboard_source_delete",
    "report_delete",
    "dashboard_delete",
    "widget_delete",
    "category_delete",
    "db_user_delete",
    "db_user_remove_orphan",
    "connector_table_remove",
    # Acoes destrutivas historicas mantidas para classificar registros existentes.
    "delete",
    "delete_dashboard_source",
    "cascade_delete_dashboard_source",
    "delete_dashboard",
    "cascade_delete_dashboard",
    "delete_dashboard_widget",
    "delete_report_category",
    "db_user_remove_missing",
    "bulk_delete",
    "bulk_delete_dashboard_widgets",
    "bulk_delete_dashboard_widget",
    "bulk_delete_all_dashboard_widget",
    "cascade_delete_report",
    "cascade_delete_local_table",
    "delete_old_snapshots",
    "snapshot_auto_cleanup",
}


def parse_int(value: str | None, default: int = 1) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def format_duration(duration_ms: int | None) -> str:
    if duration_ms is None:
        return ""
    if duration_ms < 1000:
        return f"{duration_ms} ms"
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m {remaining}s"


def format_action(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    return action.replace("_", " ").strip().capitalize()


def admin_category(action: str) -> str:
    if action in EXCLUSAO_ACTIONS:
        return "Exclusão"
    if action in {"duplicate_report", "toggle_primary"}:
        return "Relatório"
    if action == "appearance_update" or action.startswith("glpi_schedule_") or action.startswith("glpi_import_"):
        return "Sistema"
    if action in {"toggle_source", "enable_source"}:
        return "Tabela"
    if "dashboard" in action:
        return "Dashboard"
    if action.startswith("bulk_") or action in {"clear", "recreate", "delete", "export", "refresh"}:
        return "Tabela"
    if "report" in action or "category" in action:
        return "Relatório"
    return "Sistema"


def build_query(params: dict[str, str], **overrides) -> str:
    merged = {key: value for key, value in params.items() if value}
    for key, value in overrides.items():
        if value in ("", None):
            merged.pop(key, None)
        else:
            merged[key] = value
    query = urlencode(merged)
    return f"?{query}" if query else "?"


def get_log_retention_days(db: Session) -> int:
    raw_value = get_system_config_value(db, "log_retention_days", str(DEFAULT_LOG_RETENTION_DAYS))
    try:
        return max(1, int(str(raw_value or DEFAULT_LOG_RETENTION_DAYS).strip()))
    except ValueError:
        return DEFAULT_LOG_RETENTION_DAYS


def normalize_audit_status(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"success", "error", "warning", "info"}:
        return normalized
    if normalized in {"failure", "failed"}:
        return "error"
    if normalized in {"blocked", "running", "pending"}:
        return "warning"
    return "info"


def build_audit_log_query(
    db: Session,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    user: str | None = None,
    status_filter: str | None = None,
    retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
) -> list[dict]:
    """Return all filtered audit logs consolidated from the three log sources."""
    logs = []
    retention_cutoff = datetime.utcnow() - timedelta(days=max(1, retention_days))
    executions = (
        db.query(ReportExecution)
        .options(joinedload(ReportExecution.report), joinedload(ReportExecution.user))
        .filter(ReportExecution.executed_at >= retention_cutoff)
        .all()
    )
    for execution in executions:
        report_name = execution.report.name if execution.report else "Manual"
        destination = execution.destination_table or "-"
        logs.append(
            {
                "timestamp": execution.executed_at,
                "categoria": "Execução",
                "usuario": execution.user.username if execution.user else "-",
                "acao": "Relatório executado" if execution.report_id else "Execução manual",
                "detalhe": f"{report_name} -> {destination}",
                "status": normalize_audit_status(execution.status),
                "duracao": format_duration(execution.duration_ms),
                "duracao_ms": execution.duration_ms,
            }
        )

    admin_logs = db.query(AdminActionLog).filter(AdminActionLog.created_at >= retention_cutoff).all()
    for item in admin_logs:
        details = item.message or item.report_name or item.table_name or ""
        logs.append(
            {
                "timestamp": item.created_at,
                "categoria": admin_category(item.action),
                "usuario": item.username or "-",
                "acao": format_action(item.action),
                "detalhe": details,
                "status": normalize_audit_status(item.status),
                "duracao": "",
                "duracao_ms": None,
            }
        )

    auth_logs = db.query(AuthLog).filter(AuthLog.created_at >= retention_cutoff).all()
    for item in auth_logs:
        success = (item.status or "").strip().lower() == "success"
        logs.append(
            {
                "timestamp": item.created_at,
                "categoria": "Autenticação",
                "usuario": item.username,
                "acao": "Login realizado" if success else "Login falhou",
                "detalhe": item.message or item.auth_source or "",
                "status": normalize_audit_status(item.status),
                "duracao": "",
                "duracao_ms": None,
            }
        )

    from_dt = parse_date((date_from or "").strip())
    to_dt = parse_date((date_to or "").strip())
    if to_dt:
        to_dt += timedelta(days=1)

    filtered_logs = []
    for log in logs:
        timestamp = log["timestamp"]
        if not timestamp:
            continue
        if category and log["categoria"] != category:
            continue
        if user and user.lower() not in (log["usuario"] or "").lower():
            continue
        if status_filter and log["status"] != status_filter:
            continue
        if from_dt and timestamp < from_dt:
            continue
        if to_dt and timestamp >= to_dt:
            continue
        filtered_logs.append(log)

    filtered_logs.sort(key=lambda item: item["timestamp"], reverse=True)
    return filtered_logs


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/admin/history", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/history", response_class=HTMLResponse)
def admin_history(request: Request, db: Session = Depends(get_db)):
    user = require_history(request, db)
    if isinstance(user, RedirectResponse):
        return user

    query_params = request.query_params
    page = parse_int(query_params.get("page"), 1)
    categoria = (query_params.get("categoria") or "").strip()
    usuario = (query_params.get("usuario") or "").strip()
    status_filter = (query_params.get("status") or "").strip()
    date_from = (query_params.get("date_from") or "").strip()
    date_to = (query_params.get("date_to") or "").strip()

    current_filters = {
        "categoria": categoria,
        "usuario": usuario,
        "status": status_filter,
        "date_from": date_from,
        "date_to": date_to,
    }
    retention_days = get_log_retention_days(db)
    filtered_logs = build_audit_log_query(
        db,
        date_from=date_from,
        date_to=date_to,
        category=categoria,
        user=usuario,
        status_filter=status_filter,
        retention_days=retention_days,
    )
    user_logs = build_audit_log_query(db, retention_days=retention_days)
    usuarios = sorted({log["usuario"] for log in user_logs if log["usuario"] and log["usuario"] != "-"})
    total = len(filtered_logs)
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    page_items = filtered_logs[start:end]
    next_cleanup = (
        datetime.now().replace(hour=3, minute=0, second=0, microsecond=0) + timedelta(days=1)
    ).strftime("%d/%m/%Y às 03:00")

    return render(
        request,
        "history.html",
        {
            "active": "history",
            "logs": page_items,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "per_page": PER_PAGE,
            "next_cleanup": next_cleanup,
            "usuarios": usuarios,
            "filtros": current_filters,
            "current_query_string": urlencode({key: value for key, value in current_filters.items() if value}),
            "prev_page_url": build_query(current_filters, page=page - 1),
            "next_page_url": build_query(current_filters, page=page + 1),
            "clear_filters_url": "/admin/history",
        },
        db,
    )


@router.get("/admin/audit-log/export")
def export_audit_log(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    query_params = request.query_params
    filters = {
        "categoria": (query_params.get("category") or query_params.get("categoria") or "").strip(),
        "usuario": (query_params.get("user") or query_params.get("usuario") or "").strip(),
        "status": (query_params.get("status") or "").strip(),
        "date_from": (query_params.get("date_from") or "").strip(),
        "date_to": (query_params.get("date_to") or "").strip(),
    }
    logs = build_audit_log_query(
        db,
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        category=filters["categoria"],
        user=filters["usuario"],
        status_filter=filters["status"],
        retention_days=get_log_retention_days(db),
    )

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Data/Hora", "Categoria", "Usuário", "Ação", "Detalhe", "Status", "Duração (ms)"])
    for log in logs:
        writer.writerow(
            [
                log["timestamp"].strftime("%d/%m/%Y %H:%M:%S"),
                log["categoria"],
                log["usuario"],
                log["acao"],
                log["detalhe"],
                log["status"],
                "" if log["duracao_ms"] is None else log["duracao_ms"],
            ]
        )

    today = datetime.now().strftime("%Y-%m-%d")
    filename_from = filters["date_from"] or today
    filename_to = filters["date_to"] or today
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="auditoria_{filename_from}_{filename_to}.csv"'},
    )
