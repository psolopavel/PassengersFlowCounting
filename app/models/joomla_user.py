"""Read-only модель аккаунтов Joomla (таблица `jos_users` в БД joomla).

Живёт в отдельной БД (доступ через usgps2), поэтому работать с ней нужно
только через сессию JoomlaSession / зависимость get_joomla_db.
Никогда не пишем в эту таблицу из данного приложения.
"""

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.database import Base


class JosUser(Base):
    __tablename__ = "jos_users"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, default="")
    username = Column(String(150), nullable=False, default="")
    email = Column(String(100), nullable=False, default="")
    usertype = Column(String(25), nullable=True)
    block = Column(Boolean, nullable=False, default=False)
    register_date = Column("registerDate", DateTime, nullable=True)
    last_visit_date = Column("lastvisitDate", DateTime, nullable=True)

    @property
    def display_name(self) -> str:
        return self.name or self.username or self.email or f"#{self.id}"
