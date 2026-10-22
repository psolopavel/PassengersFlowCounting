"""Read-only модель остановки (таблица `stops` в БД RoutesAndAdvertisement).

Остановки принадлежат маршруту (`route_id`) и упорядочены по `order_index`.
Геометрия (`geom`, точка) не мапится — координату читаем сырым SQL через
ST_AsText/ST_X/ST_Y (см. route_match_service).
"""

from sqlalchemy import Boolean, Column, Integer, String

from app.database import Base


class Stop(Base):
    __tablename__ = "stops"

    id = Column(Integer, primary_key=True)
    route_id = Column(Integer, nullable=False)
    name = Column(String(255), nullable=True)
    radius_m = Column(Integer, nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
