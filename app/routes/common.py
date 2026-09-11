from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ConnectorConfig
from app.security import get_current_user
from app.system_config import get_system_config_value


settings = get_settings()


async def form_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


async def form_lists(request: Request) -> dict[str, list[str]]:
    body = (await request.body()).decode("utf-8")
    return parse_qs(body, keep_blank_values=True)


def render(request: Request, template: str, context: dict, db: Session, status_code: int = 200) -> HTMLResponse:
    current_user = get_current_user(request, db)
    sql_console_enabled = (get_system_config_value(db, "sql_console_enabled", "true") or "true").lower() == "true"
    base_context = {
        "request": request,
        "current_user": current_user,
        "app_name": request.app.state.settings.app_name,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "sql_console_enabled": sql_console_enabled,
    }
    base_context.update(context)
    return request.app.state.templates.TemplateResponse(template, base_context, status_code=status_code)


def get_allowed_databases(db: Session) -> list[str]:
    allowed = [settings.local_db_name, "glpi_local", "information_schema"]
    for config in db.query(ConnectorConfig).filter(ConnectorConfig.is_active.is_(True)):
        target_database = (config.target_database or "").strip()
        if target_database and target_database not in allowed:
            allowed.append(target_database)
    return allowed
