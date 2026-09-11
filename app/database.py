from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


settings = get_settings()


class Base(DeclarativeBase):
    pass


local_engine = create_engine(settings.local_database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=local_engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
