from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.models.user import ONLINE_THRESHOLD_SEC

SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 дней в секундах


class NotAuthenticated(Exception):
    """Пользователь не вошёл — редирект на /login."""


class NotAuthorized(Exception):
    """Недостаточно прав."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def login_session(request: Request, user: User) -> None:
    request.session["user_id"] = int(user.id)


def logout_session(request: Request) -> None:
    request.session.clear()


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    user = get_current_user(request, db)
    if user is None:
        raise NotAuthenticated()
    return user


def require_manager(user: User = Depends(require_user)) -> User:
    if not user.is_manager:
        raise NotAuthorized()
    return user


def require_panel_access(user: User = Depends(require_user)) -> User:
    """Панель рахувальника: доступна рахувальникам и менеджерам (для перегляду)."""
    if not (user.is_counter or user.is_manager):
        raise NotAuthorized()
    return user


def require_full_manager(user: User = Depends(require_manager)) -> User:
    if user.manager_access != "full":
        raise NotAuthorized()
    return user


def touch_login(db: Session, user: User) -> None:
    now = datetime.now(timezone.utc)
    user.last_login_at = now
    user.last_seen_at = now
    db.commit()


def touch_seen(db: Session, user: User) -> None:
    """Heartbeat з відкритої вкладки — оновлює last_seen_at (джерело «онлайн»)."""
    user.last_seen_at = datetime.now(timezone.utc)
    db.commit()


def mark_offline(db: Session, user: User) -> None:
    """Явний вихід/закриття вкладки — одразу «протухаємо» last_seen_at."""
    user.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=ONLINE_THRESHOLD_SEC + 1
    )
    db.commit()
