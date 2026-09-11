from __future__ import annotations

from datetime import datetime

import pytz


def utc_naive_to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    local_value = format_datetime_portal(value, "America/Sao_Paulo", "%Y-%m-%d %H:%M:%S")
    return datetime.strptime(local_value, "%Y-%m-%d %H:%M:%S")


def format_datetime_portal(
    dt: datetime | None,
    timezone_str: str,
    fmt: str = "%d/%m/%Y %H:%M",
) -> str:
    if dt is None:
        return ""
    try:
        tz = pytz.timezone(timezone_str)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        dt_local = dt.astimezone(tz)
        return dt_local.strftime(fmt)
    except Exception:
        return dt.strftime(fmt)


def format_local_datetime(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M:%S") -> str:
    return format_datetime_portal(value, "America/Sao_Paulo", fmt)
