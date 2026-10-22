from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()

# Новая БД (passengers_flow) — сюда пишем/читаем
app_engine = create_engine(
    settings.app_db_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
)
AppSession = sessionmaker(bind=app_engine, autoflush=False, autocommit=False)

# Старая БД (маршруты) — только чтение, отдельный движок
routes_engine = create_engine(
    settings.routes_db_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
)
RoutesSession = sessionmaker(bind=routes_engine, autoflush=False, autocommit=False)

# БД Joomla (аккаунты jos_users) — только чтение, отдельный движок
joomla_engine = create_engine(
    settings.joomla_db_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
)
JoomlaSession = sessionmaker(bind=joomla_engine, autoflush=False, autocommit=False)

# БД gps (трекеры, last_seen — чтение; passengers — запись міського підрахунку)
gps_engine = create_engine(
    settings.gps_db_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
)
GpsSession = sessionmaker(bind=gps_engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """Зависимость FastAPI: сессия новой БД."""
    db = AppSession()
    try:
        yield db
    finally:
        db.close()


def get_routes_db():
    """Зависимость FastAPI: сессия старой БД маршрутов (read-only)."""
    db = RoutesSession()
    try:
        yield db
    finally:
        db.close()


def get_joomla_db():
    """Зависимость FastAPI: сессия БД Joomla с аккаунтами (read-only)."""
    db = JoomlaSession()
    try:
        yield db
    finally:
        db.close()


def get_gps_db():
    """Зависимость FastAPI: сессия БД gps (трекеры — read; passengers — write из summary)."""
    db = GpsSession()
    try:
        yield db
    finally:
        db.close()
