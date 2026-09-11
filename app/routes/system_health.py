from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from glob import glob
from pathlib import Path
from socket import getfqdn
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import AdminActionLog, ConnectorRun, Report, ReportExecution
from app.reporting import auto_index_health_stats
from app.routes.auth import blocked_ips, get_client_ip, unblock_ip
from app.routes.common import form_data, render
from app.security import require_admin, verify_csrf
from app.system_config import get_system_config_value, set_system_config_value


router = APIRouter()
PORTAL_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = PORTAL_ROOT / "snapshots"
LOG_RETENTION_DAYS_KEY = "log_retention_days"
SNAPSHOT_MAX_COUNT_KEY = "snapshot_max_count"
SNAPSHOT_MAX_GB_KEY = "snapshot_max_gb"
DEFAULT_LOG_RETENTION_DAYS = 90
DEFAULT_SNAPSHOT_MAX_COUNT = 10
DEFAULT_SNAPSHOT_MAX_GB = 20
AUTO_INDEX_THRESHOLD_KEY = "auto_index_threshold_rows"
ALLOWED_CONFIG_KEYS = {
    AUTO_INDEX_THRESHOLD_KEY,
    LOG_RETENTION_DAYS_KEY,
    SNAPSHOT_MAX_COUNT_KEY,
    SNAPSHOT_MAX_GB_KEY,
}
PORTAL_TZ = ZoneInfo("America/Sao_Paulo")
NGINX_CONFIG_GLOBS = (
    "/etc/nginx/conf.d/*.conf",
    "/etc/nginx/sites-enabled/*",
)
SSL_CERT_GLOBS = (
    "/etc/ssl/certs/*.pem",
    "/etc/pki/tls/certs/*.pem",
    "/etc/letsencrypt/live/*/*.pem",
)


def bytes_to_gb(value: int | float) -> float:
    return round(float(value or 0) / (1024 ** 3), 2)


def bytes_to_mb(value: int | float) -> float:
    return round(float(value or 0) / (1024 ** 2), 2)


def _snapshot_is_complete(path: Path) -> bool:
    """Verifica o marcador final emitido por dumps MariaDB/MySQL completos."""
    marker = "-- Dump completed"
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 200))
            tail = handle.read().decode("utf-8", errors="replace")
        return marker in tail
    except OSError:
        return False


def create_snapshot(label: str = "", db: Session | None = None) -> dict[str, object]:
    """Cria e valida um snapshot do banco local de relatórios via mysqldump."""
    from app.config import get_settings

    settings = get_settings()
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:40]
    prefix = f"{safe_label}_" if safe_label else ""
    filename = f"{prefix}{ts}.sql"
    tmp_path = SNAPSHOT_DIR / f".tmp_{filename}"
    final_path = SNAPSHOT_DIR / filename
    cmd = [
        "mysqldump",
        f"--host={settings.local_db_host}",
        f"--port={settings.local_db_port}",
        f"--user={settings.local_db_user}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--add-drop-table",
        settings.local_db_name,
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = settings.local_db_pass

    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as handle:
            result = subprocess.run(
                cmd, stdout=handle, stderr=subprocess.PIPE, env=env, timeout=600
            )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            stderr = re.sub(
                r"(password|MYSQL_PWD)[^\n]*",
                "[REDACTED]",
                stderr,
                flags=re.IGNORECASE,
            )
            raise RuntimeError(
                f"mysqldump falhou (code {result.returncode}): {stderr[:500]}"
            )
        if not _snapshot_is_complete(tmp_path):
            raise RuntimeError(
                "Dump gerado está incompleto — marcador de conclusão ausente."
            )

        tmp_path.rename(final_path)
        size_mb = bytes_to_mb(final_path.stat().st_size)
        if db:
            db.add(AdminActionLog(
                username="system", action="snapshot_create", status="success",
                message=f"Snapshot criado: {filename} ({size_mb} MB)",
            ))
            db.commit()
        return {"success": True, "filename": filename, "size_mb": size_mb}
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        if db:
            db.add(AdminActionLog(
                username="system", action="snapshot_create", status="error",
                message=str(exc)[:500],
            ))
            db.commit()
        raise


def config_int(db: Session, key: str, default: int, minimum: int = 1) -> int:
    raw_value = get_system_config_value(db, key, str(default))
    try:
        return max(minimum, int(str(raw_value or default).strip()))
    except ValueError:
        return default


def _glob_files(patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        for path_name in glob(pattern):
            path = Path(path_name)
            if path.is_file() and not path.is_symlink():
                files.append(path)
    return files


def _read_certificate_expiration(path: Path) -> datetime | None:
    try:
        from cryptography.x509 import load_pem_x509_certificate
    except ImportError:
        return None

    try:
        cert = load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None

    expires_at = getattr(cert, "not_valid_after_utc", None)
    if expires_at is None:
        expires_at = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(timezone.utc).replace(tzinfo=None)


def check_https_status(now: datetime | None = None) -> dict[str, object]:
    nginx_config_path = None
    nginx_has_ssl = False
    for path in _glob_files(NGINX_CONFIG_GLOBS):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "listen 443" in content:
            nginx_config_path = str(path)
            nginx_has_ssl = True
            break

    cert_path = None
    cert_expires_at = None
    for path in _glob_files(SSL_CERT_GLOBS):
        cert_path = str(path)
        cert_expires_at = _read_certificate_expiration(path)
        if cert_expires_at is not None:
            break

    current_time = (now or datetime.utcnow()).replace(tzinfo=None)
    cert_days_remaining = None
    cert_expired = False
    if cert_expires_at is not None:
        cert_days_remaining = (cert_expires_at - current_time).days
        cert_expired = cert_expires_at <= current_time

    cert_found = cert_path is not None
    return {
        "https_configured": bool(nginx_has_ssl and cert_found),
        "cert_found": cert_found,
        "cert_path": cert_path,
        "cert_expires_at": cert_expires_at,
        "cert_days_remaining": cert_days_remaining,
        "cert_expired": cert_expired,
        "nginx_config_path": nginx_config_path,
        "nginx_has_ssl": nginx_has_ssl,
    }


def generate_nginx_ssl_config() -> str:
    hostname = getfqdn()
    return f"""# Redirecionar HTTP -> HTTPS
server {{
    listen 80;
    server_name {hostname};
    return 301 https://$host$request_uri;
}}

# HTTPS
server {{
    listen 443 ssl;
    server_name {hostname};

    ssl_certificate /caminho/do/certificado.pem;
    ssl_certificate_key /caminho/da/chave.key;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Headers de seguranca
    add_header Strict-Transport-Security
      "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options
      "nosniff" always;
    add_header X-Frame-Options
      "SAMEORIGIN" always;
    add_header Referrer-Policy
      "strict-origin-when-cross-origin" always;

    location / {{
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For
          $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto
          $scheme;
    }}
}}"""


def get_sql_console_status(db: Session) -> dict[str, object]:
    enabled = (get_system_config_value(db, "sql_console_enabled", "true") or "true").lower() == "true"
    return {"enabled": enabled, "label": "Ativo" if enabled else "Inativo"}


def snapshot_files() -> list[dict[str, object]]:
    if not SNAPSHOT_DIR.exists():
        return []
    files = []
    for path in SNAPSHOT_DIR.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "path": path,
                "size_bytes": stat.st_size,
                "size_mb": bytes_to_mb(stat.st_size),
                "modified_at": datetime.utcfromtimestamp(stat.st_mtime),
                "integrity": (
                    "invalid" if stat.st_size == 0
                    else "ok" if _snapshot_is_complete(path) else "incomplete"
                ),
            }
        )
    return sorted(files, key=lambda item: item["modified_at"], reverse=True)


def snapshots_older_than(days: int, now: datetime | None = None) -> list[dict[str, object]]:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=days)
    return [item for item in snapshot_files() if item["modified_at"] < cutoff]


def get_log_retention_days(db: Session) -> int:
    return config_int(db, LOG_RETENTION_DAYS_KEY, DEFAULT_LOG_RETENTION_DAYS)


def get_snapshot_policy(db: Session) -> dict[str, int]:
    return {
        "max_count": config_int(db, SNAPSHOT_MAX_COUNT_KEY, DEFAULT_SNAPSHOT_MAX_COUNT),
        "max_gb": config_int(db, SNAPSHOT_MAX_GB_KEY, DEFAULT_SNAPSHOT_MAX_GB),
    }


def snapshot_cleanup_plan(db: Session) -> dict[str, object]:
    policy = get_snapshot_policy(db)
    files = sorted(
        (item for item in snapshot_files() if int(item["size_bytes"]) > 0),
        key=lambda item: item["modified_at"],
    )
    total_bytes = sum(int(item["size_bytes"]) for item in files)
    max_total_bytes = int(policy["max_gb"] * (1024 ** 3))
    remaining_count = len(files)
    remaining_bytes = total_bytes
    remove: list[dict[str, object]] = []

    for item in files:
        if remaining_count <= policy["max_count"] and remaining_bytes <= max_total_bytes:
            break
        remove.append(item)
        remaining_count -= 1
        remaining_bytes -= int(item["size_bytes"])

    return {
        "policy": policy,
        "current_count": len(files),
        "current_gb": bytes_to_gb(total_bytes),
        "remove": remove,
        "delete_count": len(remove),
        "delete_gb": bytes_to_gb(sum(int(item["size_bytes"]) for item in remove)),
        "remaining_count": remaining_count,
        "remaining_gb": bytes_to_gb(remaining_bytes),
    }


def cleanup_old_snapshots(db: Session | None = None) -> dict[str, object]:
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    deleted = 0
    deleted_bytes = 0
    try:
        plan = snapshot_cleanup_plan(db)
        for item in plan["remove"]:
            path = item["path"]
            if path.parent.resolve() != SNAPSHOT_DIR.resolve():
                continue
            path.unlink()
            deleted += 1
            deleted_bytes += int(item["size_bytes"])
            db.add(
                AdminActionLog(
                    username="system",
                    action="snapshot_auto_cleanup",
                    status="success",
                    message=f"Removido {item['name']} ({item['size_mb']}MB) por política automática",
                )
            )
        db.commit()
        return {
            **plan,
            "deleted": deleted,
            "deleted_gb": bytes_to_gb(deleted_bytes),
        }
    except OSError:
        db.rollback()
        raise
    finally:
        if owns_session:
            db.close()


def next_cleanup_at(now: datetime | None = None) -> datetime:
    local_now = (now or datetime.now(timezone.utc)).astimezone(PORTAL_TZ)
    candidate = datetime.combine(local_now.date(), time(3, 0), tzinfo=PORTAL_TZ)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def audit_stats(db: Session) -> list[dict[str, object]]:
    definitions = (
        ("report_executions", "executed_at"),
        ("admin_action_logs", "created_at"),
        ("auth_logs", "created_at"),
    )
    result = []
    for table_name, date_column in definitions:
        row = db.execute(
            text(
                f"SELECT COUNT(*) AS total, MIN({date_column}) AS oldest, "
                f"MAX({date_column}) AS newest FROM {table_name}"
            )
        ).mappings().one()
        result.append(
            {
                "table": table_name,
                "total": int(row["total"] or 0),
                "oldest": row["oldest"],
                "newest": row["newest"],
            }
        )
    return result


def database_stats(db: Session, database_name: str) -> dict[str, object]:
    summary = db.execute(
        text(
            "SELECT COUNT(*) AS table_count, "
            "COALESCE(SUM(data_length + index_length), 0) AS total_bytes "
            "FROM information_schema.tables WHERE table_schema = :database_name"
        ),
        {"database_name": database_name},
    ).mappings().one()
    top_tables = db.execute(
        text(
            "SELECT table_name, table_rows, data_length + index_length AS size_bytes "
            "FROM information_schema.tables WHERE table_schema = :database_name "
            "ORDER BY size_bytes DESC LIMIT 5"
        ),
        {"database_name": database_name},
    ).mappings().all()
    report_tables = db.execute(
        text(
            "SELECT table_name, table_rows, data_length + index_length AS size_bytes "
            "FROM information_schema.tables "
            "WHERE table_schema = :database_name "
            "AND table_name REGEXP '^(dashboard_|relatorio_|memora_)' "
            "ORDER BY table_name"
        ),
        {"database_name": database_name},
    ).mappings().all()
    return {
        "table_count": int(summary["table_count"] or 0),
        "total_gb": bytes_to_gb(summary["total_bytes"]),
        "top_tables": [
            {
                "name": row["table_name"],
                "rows": int(row["table_rows"] or 0),
                "size_mb": bytes_to_mb(row["size_bytes"]),
            }
            for row in top_tables
        ],
        "report_tables": [
            {
                "name": row["table_name"],
                "rows": int(row["table_rows"] or 0),
                "size_mb": bytes_to_mb(row["size_bytes"]),
            }
            for row in report_tables
        ],
    }


def log_admin_action(
    db: Session,
    request: Request,
    user,
    action: str,
    status_value: str,
    message: str,
) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id,
            username=user.username,
            action=action,
            status=status_value,
            message=message,
            ip_address=get_client_ip(request),
        )
    )
    db.commit()


@router.get("/admin/system-health")
def system_health(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    usage = shutil.disk_usage(PORTAL_ROOT)
    disk_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0
    disk_level = "danger" if disk_percent > 85 else "warning" if disk_percent >= 70 else "success"

    snapshots = snapshot_files()
    last_etl = (
        db.query(ConnectorRun)
        .order_by(ConnectorRun.started_at.desc())
        .first()
    )
    last_report = (
        db.query(ReportExecution, Report)
        .outerjoin(Report, Report.id == ReportExecution.report_id)
        .order_by(ReportExecution.executed_at.desc())
        .first()
    )

    return render(
        request,
        "admin_system_health.html",
        {
            "active": "system_health",
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "disk": {
                "total_gb": bytes_to_gb(usage.total),
                "used_gb": bytes_to_gb(usage.used),
                "free_gb": bytes_to_gb(usage.free),
                "percent": disk_percent,
                "level": disk_level,
            },
            "audit_tables": audit_stats(db),
            "retention_days": get_log_retention_days(db),
            "next_cleanup": next_cleanup_at(),
            "snapshots": {
                "count": len(snapshots),
                "total_gb": bytes_to_gb(sum(int(item["size_bytes"]) for item in snapshots)),
                "oldest": snapshots[-1]["modified_at"] if snapshots else None,
                "newest": snapshots[0]["modified_at"] if snapshots else None,
                "recent": snapshots[:10],
                "policy": get_snapshot_policy(db),
                "cleanup_plan": snapshot_cleanup_plan(db),
            },
            "database": database_stats(db, request.app.state.settings.local_db_name),
            "auto_index": auto_index_health_stats(db),
            "auto_index_threshold": config_int(db, AUTO_INDEX_THRESHOLD_KEY, 1000, minimum=0),
            "https_status": check_https_status(),
            "nginx_ssl_config": generate_nginx_ssl_config(),
            "sql_console_status": get_sql_console_status(db),
            "python_version": sys.version.split()[0],
            "last_etl": last_etl,
            "last_report_execution": last_report[0] if last_report else None,
            "last_report_name": (
                last_report[1].name
                if last_report and last_report[1]
                else (last_report[0].destination_table if last_report else None)
            ),
            "blocked_ips": blocked_ips(),
        },
        db,
    )


@router.post("/admin/system-health/config", dependencies=[Depends(verify_csrf)])
async def update_system_config_key(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"error": "Não autorizado."}, status_code=401)
    data = await form_data(request)
    key = data.get("key", "")
    value = data.get("value", "").strip()
    if key not in ALLOWED_CONFIG_KEYS:
        return JSONResponse({"error": "Configuração não permitida."}, status_code=400)
    try:
        numeric_value = int(value)
    except ValueError:
        return JSONResponse({"error": "Valor deve ser numérico."}, status_code=400)
    if numeric_value < 0:
        return JSONResponse({"error": "Valor deve ser maior ou igual a zero."}, status_code=400)
    set_system_config_value(db, key, str(numeric_value))
    log_admin_action(
        db,
        request,
        user,
        "system_config_update",
        "success",
        f"Configuração {key} alterada para {numeric_value}.",
    )
    return JSONResponse({"success": True})


@router.get("/admin/system-health/snapshots/preview")
def snapshot_cleanup_preview(request: Request, days: int = 30, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return JSONResponse({"error": "Acesso negado."}, status_code=403)
    days = max(1, min(days, 3650))
    files = snapshots_older_than(days)
    return {
        "days": days,
        "count": len(files),
        "total_gb": bytes_to_gb(sum(int(item["size_bytes"]) for item in files)),
        "files": [
            {
                "name": item["name"],
                "size_mb": item["size_mb"],
                "modified_at": item["modified_at"].isoformat(),
            }
            for item in files
        ],
    }


@router.post(
    "/admin/system-health/snapshots/create",
    dependencies=[Depends(verify_csrf)],
)
async def create_snapshot_manual(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    label = (data.get("label") or "manual").strip()
    try:
        result = create_snapshot(label=label, db=db)
        log_admin_action(
            db, request, user, "snapshot_create_manual", "success",
            f"Snapshot manual criado: {result['filename']}",
        )
        message = (
            f"Snapshot criado com sucesso: {result['filename']} "
            f"({result['size_mb']} MB)"
        )
        return RedirectResponse(
            f"/admin/system-health?message={quote(message)}", status_code=303
        )
    except Exception as exc:
        message = f"Erro ao criar snapshot: {exc}"
        log_admin_action(
            db, request, user, "snapshot_create_manual", "error", message
        )
        return RedirectResponse(
            f"/admin/system-health?error={quote(message)}", status_code=303
        )


@router.post("/admin/system-health/snapshots/delete", dependencies=[Depends(verify_csrf)])
async def delete_old_snapshots(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    try:
        days = max(1, min(int(data.get("days", "30")), 3650))
    except ValueError:
        days = 30
    files = snapshots_older_than(days)
    deleted = 0
    deleted_bytes = 0
    try:
        for item in files:
            path = item["path"]
            if path.parent.resolve() != SNAPSHOT_DIR.resolve():
                continue
            path.unlink()
            deleted += 1
            deleted_bytes += int(item["size_bytes"])
        message = (
            f"Excluídos {deleted} snapshots com mais de {days} dias "
            f"({bytes_to_gb(deleted_bytes)} GB)."
        )
        log_admin_action(db, request, user, "delete_old_snapshots", "success", message)
        return RedirectResponse(
            f"/admin/system-health?message={quote(message)}",
            status_code=303,
        )
    except OSError as exc:
        db.rollback()
        message = f"Falha ao excluir snapshots: {exc}"
        log_admin_action(db, request, user, "delete_old_snapshots", "error", message)
        return RedirectResponse(
            f"/admin/system-health?error={quote(message)}",
            status_code=303,
        )


@router.post("/admin/system-health/snapshots/cleanup-policy", dependencies=[Depends(verify_csrf)])
def cleanup_snapshots_by_policy(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        result = cleanup_old_snapshots(db)
        message = (
            f"Limpeza automática removeu {result['deleted']} snapshots "
            f"({result['deleted_gb']} GB)."
        )
        log_admin_action(db, request, user, "snapshot_policy_cleanup_manual", "success", message)
        return RedirectResponse(
            f"/admin/system-health?message={quote(message)}",
            status_code=303,
        )
    except OSError as exc:
        db.rollback()
        message = f"Falha na limpeza automática de snapshots: {exc}"
        log_admin_action(db, request, user, "snapshot_policy_cleanup_manual", "error", message)
        return RedirectResponse(
            f"/admin/system-health?error={quote(message)}",
            status_code=303,
        )


@router.post("/admin/system-health/rate-limit/unblock", dependencies=[Depends(verify_csrf)])
async def release_blocked_ip(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    ip_address = data.get("ip_address", "").strip()[:45]
    released = unblock_ip(ip_address)
    message = (
        f"IP {ip_address} desbloqueado manualmente."
        if released
        else f"IP {ip_address} não estava bloqueado."
    )
    log_admin_action(
        db,
        request,
        user,
        "unblock_login_ip",
        "success" if released else "not_found",
        message,
    )
    return RedirectResponse(
        f"/admin/system-health?message={quote(message)}",
        status_code=303,
    )


@router.post("/admin/system-health/sql-console/toggle", dependencies=[Depends(verify_csrf)])
async def toggle_sql_console(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    current_enabled = get_sql_console_status(db)["enabled"]
    next_enabled = not current_enabled
    set_system_config_value(db, "sql_console_enabled", "true" if next_enabled else "false")
    message = f"Console SQL {'ativado' if next_enabled else 'desativado'}."
    log_admin_action(
        db,
        request,
        user,
        "sql_console_toggle",
        "success",
        message,
    )
    return RedirectResponse(
        f"/admin/system-health?message={quote(message)}",
        status_code=303,
    )
