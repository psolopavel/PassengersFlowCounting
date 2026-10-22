"""Read-only модель маршрута (таблица `routes` в БД RoutesAndAdvertisement).

Живёт в отдельной БД (read-only), работать только через RoutesSession /
зависимость get_routes_db. Геометрия (`geom`) здесь НЕ мапится — её читаем
сырым SQL через ST_AsText (см. route_match_service), т.к. это GEOMETRY.
"""

from sqlalchemy import Boolean, Column, Integer, String

from app.database import Base


class Route(Base):
    __tablename__ = "routes"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=True)
    route_kind = Column(String(32), nullable=True)  # city / intercity
    length_m = Column(Integer, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
