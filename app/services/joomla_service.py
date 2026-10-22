"""Чтение аккаунтов из БД Joomla (jos_users).

Используется для привязки машины к аккаунту-владельцу (cars.owner_user_id).
Только чтение — работаем через сессию JoomlaSession / get_joomla_db.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JosUser


def list_owners(joomla_db: Session) -> list[JosUser]:
    """Список незаблокированных аккаунтов Joomla для выбора владельца."""
    return list(
        joomla_db.execute(
            select(JosUser)
            .where(JosUser.block.is_(False))
            .order_by(JosUser.name, JosUser.username)
        ).scalars()
    )


def get_owner(joomla_db: Session, user_id: int) -> JosUser | None:
    return joomla_db.get(JosUser, user_id)


def owners_by_id(joomla_db: Session, ids: set[int]) -> dict[int, JosUser]:
    """Мапа id -> аккаунт для набора id (для отображения владельцев в списке)."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = joomla_db.execute(
        select(JosUser).where(JosUser.id.in_(ids))
    ).scalars()
    return {u.id: u for u in rows}
