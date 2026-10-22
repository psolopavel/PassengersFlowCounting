"""Бизнес-логика по считальщикам.

Считальщик = запись в `users` (role='counter') + запись в `counters` (1:1).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Counter, User
from app.security import hash_password

GENDERS = ["male", "female", "other"]
EMPLOYMENTS = ["full", "part", "none"]
CAR_TYPES = ["city", "intercity"]

EMPLOYMENT_LABELS = {"full": "Повна", "part": "Неповна", "none": "Не працює"}
GENDER_LABELS = {"male": "Чоловіча", "female": "Жіноча", "other": "Інша"}
CAR_TYPE_LABELS = {"city": "Місто", "intercity": "Міжмісто"}


class ServiceError(Exception):
    """Ошибка бизнес-логики с человекочитаемым сообщением."""


def list_counters(db: Session) -> list[User]:
    return list(
        db.execute(
            select(User)
            .where(User.role == "counter")
            .options(joinedload(User.counter))
            .order_by(User.created_at.desc())
        ).scalars()
    )


def get_counter(db: Session, user_id: int) -> User | None:
    return db.execute(
        select(User)
        .where(User.id == user_id, User.role == "counter")
        .options(joinedload(User.counter))
    ).scalar_one_or_none()


def _email_taken(db: Session, email: str, exclude_id: int | None = None) -> bool:
    stmt = select(User.id).where(User.email == email)
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return db.execute(stmt).first() is not None


def _clean_car_types(values: list[str]) -> set[str]:
    return {v for v in values if v in CAR_TYPES}


def create_counter(
    db: Session,
    *,
    name: str,
    email: str,
    password: str,
    photo_url: str | None,
    age: int | None,
    disability: bool,
    gender: str | None,
    employment: str,
    comment: str | None,
    rating: int | None,
    car_types: list[str],
) -> User:
    name = name.strip()
    email = email.strip().lower()
    if not name:
        raise ServiceError("Вкажіть ім'я")
    if not email:
        raise ServiceError("Вкажіть пошту")
    if not password:
        raise ServiceError("Вкажіть пароль")
    if _email_taken(db, email):
        raise ServiceError("Користувач з такою поштою вже існує")

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        photo_url=photo_url,
        role="counter",
        manager_access=None,
    )
    user.counter = Counter(
        age=age,
        disability=disability,
        gender=gender or None,
        employment=employment if employment in EMPLOYMENTS else "none",
        comment=comment or None,
        rating=rating,
        car_types=_clean_car_types(car_types),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_counter(
    db: Session,
    user: User,
    *,
    name: str,
    email: str,
    password: str | None,
    photo_url: str | None,
    age: int | None,
    disability: bool,
    gender: str | None,
    employment: str,
    comment: str | None,
    rating: int | None,
    car_types: list[str],
) -> User:
    name = name.strip()
    email = email.strip().lower()
    if not name:
        raise ServiceError("Вкажіть ім'я")
    if not email:
        raise ServiceError("Вкажіть пошту")
    if _email_taken(db, email, exclude_id=user.id):
        raise ServiceError("Інший користувач вже використовує цю пошту")

    user.name = name
    user.email = email
    if password:
        user.password_hash = hash_password(password)
    if photo_url is not None:
        user.photo_url = photo_url

    if user.counter is None:
        user.counter = Counter()
    counter = user.counter
    counter.age = age
    counter.disability = disability
    counter.gender = gender or None
    counter.employment = employment if employment in EMPLOYMENTS else "none"
    counter.comment = comment or None
    counter.rating = rating
    counter.car_types = _clean_car_types(car_types)

    db.commit()
    db.refresh(user)
    return user


def delete_counter(db: Session, user: User) -> None:
    db.delete(user)  # counters удалится каскадом (cascade + FK ON DELETE CASCADE)
    db.commit()
