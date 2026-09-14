from __future__ import annotations

import base64
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import AdminActionLog, SystemConfig
from app.routes.common import render
from app.config import get_settings
from app.security import require_admin, verify_csrf, verify_csrf_token


router = APIRouter()

DEFAULT_APPEARANCE = {
    "portal_name": "ArcReports",
    "portal_initials": "AR",
    "timezone": "America/Sao_Paulo",
    "login_bg_mode": "color",
    "login_bg_color": "#1e2a3a",
    "login_bg_image": "",
    "login_overlay_color": "#1e2a3a",
    "login_overlay_opacity": "50",
    "login_title": "ArcReports",
    "login_subtitle": "Acesse os relatórios operacionais do GLPI.",
    "login_btn_text": "Entrar",
    "font_family": "system-ui, sans-serif",
    "font_size": "normal",
}

PDF_CONFIG_KEYS = ("pdf_logo", "pdf_titulo", "pdf_subtitulo", "pdf_rodape")

ALLOWED_TIMEZONES = [
    "America/Sao_Paulo",
    "America/Manaus",
    "America/Belem",
    "America/Fortaleza",
    "America/Recife",
    "America/Maceio",
    "America/Bahia",
    "America/Cuiaba",
    "America/Porto_Velho",
    "America/Boa_Vista",
    "America/Rio_Branco",
    "America/Noronha",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Lisbon",
    "Europe/Madrid",
    "Europe/Paris",
    "UTC",
]

APPEARANCE_DIR = Path("static/uploads/appearance")
LOGIN_BG_PATH = APPEARANCE_DIR / "login_bg.jpg"
LOGIN_BG_STATIC_PATH = "uploads/appearance/login_bg.jpg"
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
LOGIN_BG_MAX_SIZE = 7 * 1024 * 1024
PDF_LOGO_MAX_SIZE = 2 * 1024 * 1024
settings = get_settings()


def load_appearance(db: Session) -> dict[str, str]:
    configs = (
        db.query(SystemConfig)
        .filter(SystemConfig.key.like("appearance_%"))
        .all()
    )
    values = DEFAULT_APPEARANCE.copy()
    for config in configs:
        short_key = config.key.removeprefix("appearance_")
        if short_key in values:
            values[short_key] = config.value or ""
    return values


def load_pdf_config(db: Session, portal_name: str | None = None) -> dict[str, str]:
    default_title = portal_name or load_appearance(db).get("portal_name", DEFAULT_APPEARANCE["portal_name"])
    values = {
        "pdf_logo": "",
        "pdf_titulo": default_title or DEFAULT_APPEARANCE["portal_name"],
        "pdf_subtitulo": "",
        "pdf_rodape": "",
    }
    configs = (
        db.query(SystemConfig)
        .filter(SystemConfig.key.in_(PDF_CONFIG_KEYS))
        .all()
    )
    for config in configs:
        if config.key in values:
            values[config.key] = config.value or ""
    return values


def upsert_config(db: Session, key: str, value: str) -> None:
    config = db.get(SystemConfig, key)
    if config:
        config.value = value
        return
    db.add(SystemConfig(key=key, value=value))


def refresh_template_appearance(request: Request) -> None:
    db = SessionLocal()
    try:
        current_appearance = load_appearance(db)
        pdf_config = load_pdf_config(db, current_appearance.get("portal_name"))
        request.app.state.templates.env.globals["appearance"] = current_appearance
        request.app.state.templates.env.globals["portal_tz"] = current_appearance.get(
            "timezone",
            DEFAULT_APPEARANCE["timezone"],
        )
        for key, value in pdf_config.items():
            request.app.state.templates.env.globals[key] = value
    finally:
        db.close()


def appearance_redirect(message: str, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(
        f"/admin/appearance?{key}={quote(message)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def log_appearance_action(db: Session, user, message: str) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action="appearance_update",
            status="success",
            message=message,
        )
    )


async def parse_appearance_form(request: Request) -> tuple[dict[str, str], dict[str, dict[str, bytes | str]]]:
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    fields: dict[str, str] = {}
    files: dict[str, dict[str, bytes | str]] = {}

    if content_type.startswith("multipart/form-data"):
        raw_message = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
            + body
        )
        message = BytesParser(policy=default).parsebytes(raw_message)
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = {"filename": filename, "content": payload}
            elif filename is None:
                charset = part.get_content_charset() or "utf-8"
                fields[name] = payload.decode(charset, errors="replace")
    else:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        fields = {key: values[0] for key, values in parsed.items()}

    return fields, files


def image_data_url(filename: str, content: bytes) -> str | None:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return None
    mime = "image/png" if extension == ".png" else "image/jpeg"
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def validate_image_upload(
    filename: str,
    content: bytes,
    max_size: int,
    field_name: str,
) -> str | None:
    """
    Valida extensão e tamanho do arquivo.
    Retorna mensagem de erro ou None se válido.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return (
            f"Extensão não permitida: {ext}. "
            f"Use: JPG, JPEG ou PNG."
        )
    if len(content) > max_size:
        max_mb = max_size // (1024 * 1024)
        size_mb = len(content) / (1024 * 1024)
        return (
            f"Arquivo muito grande: "
            f"{size_mb:.1f}MB. "
            f"Máximo permitido: {max_mb}MB."
        )
    return None


def verify_appearance_csrf(request: Request, csrf_token: str) -> None:
    session_token = request.cookies.get(settings.cookie_name, "")
    if session_token and not verify_csrf_token(session_token, csrf_token.strip()):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")


@router.get("/admin/appearance", response_class=HTMLResponse)
def appearance_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    current_appearance = load_appearance(db)
    return render(
        request,
        "admin_appearance.html",
        {
            "active": "appearance",
            "appearance": current_appearance,
            "pdf_config": load_pdf_config(db, current_appearance.get("portal_name")),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/appearance")
async def update_appearance(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    form, image = await parse_appearance_form(request)
    verify_appearance_csrf(request, form.get("csrf_token", ""))
    image_path = form.get("current_login_bg_image", "")
    image_file = image.get("login_bg_image", {"filename": "", "content": b""})

    if image_file.get("filename"):
        content = bytes(image_file["content"])
        validation_error = validate_image_upload(
            str(image_file["filename"]),
            content,
            LOGIN_BG_MAX_SIZE,
            "Imagem de fundo",
        )
        if validation_error:
            return appearance_redirect(validation_error, True)
        APPEARANCE_DIR.mkdir(parents=True, exist_ok=True)
        LOGIN_BG_PATH.write_bytes(content)
        image_path = LOGIN_BG_STATIC_PATH

    current_appearance = load_appearance(db)
    timezone = str(form.get("timezone", "") or "").strip()
    if timezone not in ALLOWED_TIMEZONES:
        timezone = current_appearance.get("timezone", DEFAULT_APPEARANCE["timezone"])

    values = {
        "portal_name": str(form.get("portal_name", DEFAULT_APPEARANCE["portal_name"]) or DEFAULT_APPEARANCE["portal_name"]).strip()[:50],
        "portal_initials": str(form.get("portal_initials", DEFAULT_APPEARANCE["portal_initials"]) or DEFAULT_APPEARANCE["portal_initials"]).strip().upper()[:3],
        "timezone": timezone,
        "login_bg_mode": str(form.get("login_bg_mode", "color") or "color"),
        "login_bg_color": str(form.get("login_bg_color", DEFAULT_APPEARANCE["login_bg_color"]) or DEFAULT_APPEARANCE["login_bg_color"]),
        "login_bg_image": str(image_path or ""),
        "login_overlay_color": str(form.get("login_overlay_color", DEFAULT_APPEARANCE["login_overlay_color"]) or DEFAULT_APPEARANCE["login_overlay_color"]),
        "login_overlay_opacity": str(form.get("login_overlay_opacity", DEFAULT_APPEARANCE["login_overlay_opacity"]) or DEFAULT_APPEARANCE["login_overlay_opacity"]),
        "login_title": str(form.get("login_title", DEFAULT_APPEARANCE["login_title"]) or DEFAULT_APPEARANCE["login_title"]),
        "login_subtitle": str(form.get("login_subtitle", DEFAULT_APPEARANCE["login_subtitle"]) or DEFAULT_APPEARANCE["login_subtitle"]),
        "login_btn_text": str(form.get("login_btn_text", DEFAULT_APPEARANCE["login_btn_text"]) or DEFAULT_APPEARANCE["login_btn_text"]),
        "font_family": str(form.get("font_family", DEFAULT_APPEARANCE["font_family"]) or DEFAULT_APPEARANCE["font_family"]),
        "font_size": str(form.get("font_size", DEFAULT_APPEARANCE["font_size"]) or DEFAULT_APPEARANCE["font_size"]),
    }

    for key, value in values.items():
        upsert_config(db, f"appearance_{key}", value)
    log_appearance_action(db, user, "Personalização atualizada.")
    db.commit()
    refresh_template_appearance(request)
    return appearance_redirect("Configurações salvas.")


@router.get("/admin/appearance/pdf", response_class=HTMLResponse)
def pdf_appearance_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    current_appearance = load_appearance(db)
    return render(
        request,
        "admin_appearance.html",
        {
            "active": "appearance",
            "appearance": current_appearance,
            "pdf_config": load_pdf_config(db, current_appearance.get("portal_name")),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
        db,
    )


@router.post("/admin/appearance/pdf")
async def update_pdf_appearance(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    form, files = await parse_appearance_form(request)
    verify_appearance_csrf(request, form.get("csrf_token", ""))

    current_appearance = load_appearance(db)
    current_pdf = load_pdf_config(db, current_appearance.get("portal_name"))
    pdf_logo = current_pdf.get("pdf_logo", "")
    logo_file = files.get("pdf_logo", {"filename": "", "content": b""})

    if form.get("remove_pdf_logo") == "1":
        pdf_logo = ""
    elif logo_file.get("filename"):
        content = bytes(logo_file["content"])
        validation_error = validate_image_upload(
            str(logo_file["filename"]),
            content,
            PDF_LOGO_MAX_SIZE,
            "Logo da empresa",
        )
        if validation_error:
            return appearance_redirect(validation_error, True)
        data_url = image_data_url(str(logo_file["filename"]), content)
        pdf_logo = data_url

    pdf_values = {
        "pdf_logo": pdf_logo,
        "pdf_titulo": str(form.get("pdf_titulo", "") or current_appearance.get("portal_name") or DEFAULT_APPEARANCE["portal_name"]).strip()[:120],
        "pdf_subtitulo": str(form.get("pdf_subtitulo", "") or "").strip()[:180],
        "pdf_rodape": str(form.get("pdf_rodape", "") or "").strip()[:180],
    }
    for key, value in pdf_values.items():
        upsert_config(db, key, value)
    log_appearance_action(db, user, "Personalização do PDF atualizada.")
    db.commit()
    refresh_template_appearance(request)
    return appearance_redirect("Configurações PDF salvas.")


@router.post("/admin/appearance/remove-image", dependencies=[Depends(verify_csrf)])
def remove_appearance_image(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    if LOGIN_BG_PATH.exists():
        LOGIN_BG_PATH.unlink()
    upsert_config(db, "appearance_login_bg_image", "")
    upsert_config(db, "appearance_login_bg_mode", "color")
    log_appearance_action(db, user, "Imagem de fundo removida.")
    db.commit()
    refresh_template_appearance(request)
    return appearance_redirect("Imagem removida.")
