"""Read-only модель трекера (таблица `trackers` в БД gps).

Живёт в отдельной БД (доступ usgps2), работать только через GpsSession /
зависимость get_gps_db. Нужна лишь для чтения last_seen по imei — когда
машина последний раз присылала координату.
"""

from sqlalchemy import BigInteger, Column, Integer, String

from app.database import Base


class Tracker(Base):
    __tablename__ = "trackers"

    id = Column(Integer, primary_key=True)
    imei = Column(String(32), nullable=False)
    last_seen = Column(BigInteger, nullable=True)  # unix timestamp (сек)
