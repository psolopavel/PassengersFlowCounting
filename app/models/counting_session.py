"""Сессия подсчёта одной машины за один день (таблица `counting_sessions`).

Определяет надпись на кнопке в панели рахувальника:
  * нет строки           → «Почати підрахунок»
  * status = in_progress → «Дорахувати»
  * status = ready       → «Перерахувати»
  * status = skipped     → «Дорахувати» (продовжити після пропуску)
"""

from sqlalchemy import Column, Date, DateTime, func
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, LONGTEXT

from app.database import Base


class CountingSession(Base):
    __tablename__ = "counting_sessions"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    counter_id = Column(BIGINT(unsigned=True), nullable=False)
    car_id = Column(BIGINT(unsigned=True), nullable=False)
    count_day = Column(Date, nullable=False)
    time_spent_sec = Column(INTEGER(unsigned=True), nullable=False, default=0)
    status = Column(
        ENUM("in_progress", "ready", "skipped"),
        nullable=False,
        default="in_progress",
    )
    kind = Column(
        ENUM("city", "intercity", "final_stop"),
        nullable=False,
        default="city",
    )
    count_type = Column(
        ENUM("full_day", "finish_stops"),
        nullable=False,
        default="full_day",
    )
    data = Column(LONGTEXT, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
