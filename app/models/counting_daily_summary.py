"""Агрегат «день-машина» для внешнего API выручки (таблица `counting_daily_summary`).

Компактная материализация одной завершённой сессии подсчёта: километраж,
пассажиры за день и (для межгорода) FIFO-разбор поездок между остановками.
Заполняется при `status → ready` и финальных автосейвах (см. summary_service),
а также разовым бэкофиллом. Выручка здесь НЕ хранится — считается на чтении из
`trips_json` по тарифу, актуальному на `count_day`.
"""

from sqlalchemy import Column, Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.mysql import BIGINT, DECIMAL, ENUM, INTEGER, LONGTEXT

from app.database import Base


class CountingDailySummary(Base):
    __tablename__ = "counting_daily_summary"
    __table_args__ = (
        UniqueConstraint(
            "car_id", "count_day", "count_type",
            name="uq_summary_car_day_type",
        ),
    )

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    session_id = Column(BIGINT(unsigned=True), nullable=False)
    car_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("cars.id", ondelete="CASCADE"),
        nullable=False,
    )
    imei = Column(String(32), nullable=False)
    count_day = Column(Date, nullable=False)
    kind = Column(ENUM("city", "intercity", "final_stop"), nullable=False)
    count_type = Column(
        ENUM("full_day", "finish_stops"),
        nullable=False,
        default="full_day",
    )
    status = Column(ENUM("in_progress", "ready", "skipped"), nullable=False)
    distance_km = Column(DECIMAL(10, 2), nullable=False, default=0)
    passengers_total = Column[float](DECIMAL(unsigned=True), nullable=False, default=0)
    # Межгород: JSON-массив [{route_id, from_id, to_id, pax}]. Город: NULL.
    trips_json = Column(LONGTEXT, nullable=True)
    computed_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
