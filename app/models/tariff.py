from sqlalchemy import Column, Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.mysql import BIGINT, JSON

from app.database import Base


class Tariff(Base):
    """Тарифная сетка маршрута (создаётся в программе маршрутов, здесь только читается)."""

    __tablename__ = "tariffs"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    route_id = Column(BIGINT(unsigned=True), nullable=False)
    name = Column(String(255), nullable=False)
    prices = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CarRouteTariff(Base):
    """Одна запись истории: с какой даты для маршрута машины действует тариф.

    Конец периода вычисляется = день до начала следующей записи (по возрастанию даты),
    последняя запись действует «по сегодня».

    Целостность держится на FK:
      * car_id  -> cars.id     ON DELETE CASCADE  (удалили машину — ушли её периоды);
      * tariff_id -> tariffs.id ON DELETE CASCADE (удалили тариф — ушли периоды с ним;
        а удаление маршрута каскадом удаляет его тарифы -> и эти периоды).
    UNIQUE(car_id, route_id, start_date) запрещает повторяющиеся даты в одном маршруте.
    """

    __tablename__ = "car_route_tariffs"
    __table_args__ = (
        UniqueConstraint("car_id", "route_id", "start_date", name="uq_car_route_date"),
    )

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    car_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("cars.id", ondelete="CASCADE"),
        nullable=False,
    )
    route_id = Column(BIGINT(unsigned=True), nullable=False)
    tariff_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("tariffs.id", ondelete="CASCADE"),
        nullable=False,
    )
    start_date = Column(Date, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
