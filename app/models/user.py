from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.mysql import BIGINT, ENUM, SET, TINYINT
from sqlalchemy.orm import relationship

from app.database import Base

# Користувач вважається «онлайн», якщо останній heartbeat був не давніше цього.
ONLINE_THRESHOLD_SEC = 90


class User(Base):
    __tablename__ = "users"

    id = Column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, default="")
    email = Column(String(255), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    photo_url = Column(String(512), nullable=True)
    role = Column(ENUM("manager", "counter"), nullable=False)
    manager_access = Column(ENUM("view", "full"), nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    # Останній heartbeat з відкритої вкладки — джерело правди для «онлайн».
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    counter = relationship(
        "Counter",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def is_online(self) -> bool:
        """Онлайн = був heartbeat не давніше ONLINE_THRESHOLD_SEC.

        Самоочищається: якщо вкладку закрито / втрачено мережу / сервер падав —
        користувач стане офлайн сам, коли last_seen_at «протухне».
        """
        if self.last_seen_at is None:
            return False
        last = self.last_seen_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last) <= timedelta(
            seconds=ONLINE_THRESHOLD_SEC
        )

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"

    @property
    def is_counter(self) -> bool:
        return self.role == "counter"

    @property
    def can_edit(self) -> bool:
        """full-менеджер может изменять данные, view — только смотреть."""
        return self.role == "manager" and self.manager_access == "full"


class Counter(Base):
    __tablename__ = "counters"

    user_id = Column(
        BIGINT(unsigned=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    age = Column(TINYINT(unsigned=True), nullable=True)
    disability = Column(Boolean, nullable=False, default=False)
    gender = Column(ENUM("male", "female", "other"), nullable=True)
    employment = Column(
        ENUM("full", "part", "none"), nullable=False, default="none"
    )
    comment = Column(Text, nullable=True)
    rating = Column(TINYINT(unsigned=True), nullable=True)  # 1..5, видно только менеджерам
    car_types = Column(SET("city", "intercity"), nullable=False, default="")

    user = relationship("User", back_populates="counter")
