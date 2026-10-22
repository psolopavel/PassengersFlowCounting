"""Read-only модель привязки машины к маршрутам.

Таблица `all_car_routes` в БД RoutesAndAdvertisement: по `imei` хранит JSON-массив
`routes_id` — список id маршрутов, по которым ездит машина.
"""

from sqlalchemy import Column, Integer, String, Text

from app.database import Base


class AllCarRoute(Base):
    __tablename__ = "all_car_routes"

    id = Column(Integer, primary_key=True)
    imei = Column(String(32), nullable=False)
    routes_id = Column(Text, nullable=True)  # JSON: [193, 285]
