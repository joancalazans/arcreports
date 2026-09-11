from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import SystemConfig


def get_system_config_value(db: Session, key: str, default: str | None = None) -> str | None:
    config = db.get(SystemConfig, key)
    return config.value if config and config.value is not None else default


def set_system_config_value(db: Session, key: str, value: str) -> None:
    config = db.get(SystemConfig, key)
    if config is None:
        config = SystemConfig(key=key, value=value)
        db.add(config)
    else:
        config.value = value
    db.commit()
