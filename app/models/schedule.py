from sqlalchemy import Column, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.mysql import BIGINT, ENUM, LONGTEXT
from sqlalchemy.orm import relationship

from app.database import Base


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    counter_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    car_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("cars.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Тип підрахунку розкладу: цілоденний (місто/міжмісто) або лише кінцеві
    # зупинки. Обидва типи незалежні й можуть існувати для однієї машини.
    type = Column(
        ENUM("whole_day", "final_stops"),
        nullable=False,
        default="whole_day",
        server_default="whole_day",
    )
    single_days = Column(LONGTEXT, nullable=True)
    recurring = Column(LONGTEXT, nullable=True)
    exception_days = Column(LONGTEXT, nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    counter_user = relationship("User", foreign_keys=[counter_id])
    car = relationship("Car", foreign_keys=[car_id])
