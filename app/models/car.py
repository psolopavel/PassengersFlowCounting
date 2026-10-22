from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.mysql import BIGINT, ENUM, JSON, LONGTEXT, SET, TINYINT

from app.database import Base


class Car(Base):
    __tablename__ = "cars"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, default="")
    imei = Column(String(32), nullable=False, unique=True)
    # Список камер: [{"id": "7NVTT", "orientation": "landscape"}, ...]
    # orientation: landscape | portrait (можна додавати інші поля пізніше)
    cameras = Column(JSON, nullable=False, default=list)
    # Владелец машины — аккаунт из БД Joomla (jos_users.id). Межбазовая мягкая
    # связь без жёсткого FK: значение резолвится через сессию Joomla.
    owner_user_id = Column(Integer, nullable=True)
    company_name = Column(String(255), nullable=False)
    comment = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    difficulty_rating = Column(TINYINT(unsigned=True), nullable=True)  # 1..5
    count_source = Column(ENUM("photo", "video", "photoAndVideo"), nullable=False)
    # При count_source=photoAndVideo: що показувати в плеєрі спочатку (фото чи відео).
    source_priority = Column(
        ENUM("photo", "video"), nullable=False, default="video", server_default="video"
    )
    # Дозволити рахувальнику рахувати сьогодні без очікування кінця розкладу.
    non_shedule_count = Column(Boolean, nullable=False, default=False, server_default="0")
    alarms_only = Column(Boolean, nullable=False, default=False)
    auto_gap_filling = Column(Boolean, nullable=False, default=False)
    route_kind = Column(SET("city", "intercity"), nullable=False, default="")
    # Рахувальник сам обирає тип маршруту (місто/міжмісто) у панелі перед підрахунком.
    route_kind_chose = Column(Boolean, nullable=False, default=False)
    # Максимум людей у салоні для підрахунку міжміста.
    max_people_count_in_car = Column(Integer, nullable=False, default=100, server_default="100")
    schedule = Column(
        LONGTEXT, nullable=False,
        default='{"start_utc":"03:00:00","end_utc":"19:00:00"}',
        server_default='{"start_utc":"03:00:00","end_utc":"19:00:00"}',
    )
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
