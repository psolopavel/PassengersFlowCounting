"""Очередь отправки клиентских xlsx-отчётов (таблица `report_deliveries`).

Пишется из report_delivery_service при материализации ready-сессии, читается
и отправляется демоном app/workers/report_mailer.py. Подробности — в
sql/report_deliveries.sql.
"""

from sqlalchemy import Column, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, JSON, TINYINT

from app.database import Base

DELIVERY_STATUSES = ("pending", "sending", "sent", "failed", "cancelled", "no_recipient")


class ReportDelivery(Base):
    __tablename__ = "report_deliveries"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    session_id = Column(BIGINT(unsigned=True), nullable=False)
    car_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("cars.id", ondelete="CASCADE"),
        nullable=False,
    )
    imei = Column(String(32), nullable=False, default="")
    count_day = Column(Date, nullable=False)
    count_type = Column(
        ENUM("full_day", "finish_stops"), nullable=False, default="full_day",
    )
    kind = Column(ENUM("city", "intercity"), nullable=False)
    data_hash = Column(String(64), nullable=False)
    version = Column(INTEGER(unsigned=True), nullable=True)
    recipient_emails = Column(JSON, nullable=True)
    skipped_emails = Column(JSON, nullable=True)
    status = Column(ENUM(*DELIVERY_STATUSES), nullable=False, default="pending")
    attempts = Column(TINYINT(unsigned=True), nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False)
    last_error = Column(Text, nullable=True)
    provider_message_id = Column(String(128), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
