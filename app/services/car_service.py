"""Бизнес-логика по машинам (таблица `cars` в новой БД).

Связь с маршрутами (старая БД RoutesAndAdvertisement) идёт по imei — здесь не трогаем.
"""

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Car

_UTC_OFFSET = timedelta(hours=3)
_DEFAULT_SCHEDULE_LOCAL = ("06:00", "22:00")

COUNT_SOURCES = ["photo", "video", "photoAndVideo"]
SOURCE_PRIORITIES = ["photo", "video"]
ROUTE_KINDS = ["city", "intercity"]
CAMERA_ORIENTATIONS = ["landscape", "portrait"]

COUNT_SOURCE_LABELS = {"photo": "Фото", "video": "Відео", "photoAndVideo": "Фото та відео"}
SOURCE_PRIORITY_LABELS = {"photo": "Фото", "video": "Відео"}
ROUTE_KIND_LABELS = {"city": "Місто", "intercity": "Міжмісто"}
CAMERA_ORIENTATION_LABELS = {"landscape": "Горизонтальна", "portrait": "Вертикальна"}
DEFAULT_MAX_PEOPLE_IN_CAR = 100


class ServiceError(Exception):
    pass


def list_cars(db: Session) -> list[Car]:
    return list(
        db.execute(select(Car).order_by(Car.created_at.desc())).scalars()
    )


def get_car(db: Session, car_id: int) -> Car | None:
    return db.get(Car, car_id)


def camera_id(cam: str | dict) -> str:
    """ID камери з рядка (legacy) або об'єкта."""
    if isinstance(cam, dict):
        return str(cam.get("id") or "").strip()
    return str(cam).strip()


def camera_ids(cameras: list) -> list[str]:
    """Список ID для API медіа та ключів day_media."""
    return [cid for cid in (camera_id(c) for c in (cameras or [])) if cid]


def parse_cameras_from_form(
    ids: list[str], orientations: list[str]
) -> list[dict]:
    """Поля форми -> [{"id": "...", "orientation": "landscape"|"portrait"}, ...]."""
    out: list[dict] = []
    seen: set[str] = set()
    for i, raw_id in enumerate(ids):
        cam_id = raw_id.strip()
        if not cam_id:
            continue
        if cam_id in seen:
            raise ServiceError(f"Дубльований ID камери: {cam_id}")
        seen.add(cam_id)
        orient = orientations[i].strip() if i < len(orientations) else "landscape"
        if orient not in CAMERA_ORIENTATIONS:
            orient = "landscape"
        out.append({"id": cam_id, "orientation": orient})
    return out


def _imei_taken(db: Session, imei: str, exclude_id: int | None = None) -> bool:
    stmt = select(Car.id).where(Car.imei == imei)
    if exclude_id is not None:
        stmt = stmt.where(Car.id != exclude_id)
    return db.execute(stmt).first() is not None


def _clean_route_kind(values: list[str]) -> set[str]:
    return {v for v in values if v in ROUTE_KINDS}


def _clean_source_priority(value: str | None, count_source: str) -> str:
    """Пріоритет медіа має сенс лише для photoAndVideo; інакше — video."""
    if count_source != "photoAndVideo":
        return "video"
    if value in SOURCE_PRIORITIES:
        return value
    return "video"


def _normalize_time_str(value: str) -> str:
    """'03.00.00' / '03:00:00' -> '03:00:00'."""
    parts = re.split(r"[:.]", value.strip())
    if len(parts) < 2:
        raise ValueError(f"invalid time: {value!r}")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    return f"{h:02d}:{m:02d}:{s:02d}"


def _time_local_to_utc(local_str: str) -> str:
    """'HH:MM' (UTC+3) -> 'HH:MM:SS' (UTC)."""
    parts = local_str.strip().split(":")
    if len(parts) < 2:
        raise ServiceError("Вкажіть коректний час розкладу")
    h, m = int(parts[0]), int(parts[1])
    dt = datetime(2000, 1, 1, h, m) - _UTC_OFFSET
    return dt.strftime("%H:%M:%S")


def _time_utc_to_local(utc_str: str) -> str:
    """'HH:MM:SS' (UTC) -> 'HH:MM' (UTC+3) for the form input."""
    normalized = _normalize_time_str(utc_str)
    h, m, _s = (int(x) for x in normalized.split(":"))
    dt = datetime(2000, 1, 1, h, m) + _UTC_OFFSET
    return dt.strftime("%H:%M")


def build_schedule_json(schedule_start: str, schedule_end: str) -> str:
    return json.dumps(
        {
            "start_utc": _time_local_to_utc(schedule_start),
            "end_utc": _time_local_to_utc(schedule_end),
        },
        ensure_ascii=False,
    )


def parse_schedule_to_local(schedule_raw: str | dict | None) -> tuple[str, str]:
    """Parse schedule from DB and return (start_local, end_local) as HH:MM."""
    if not schedule_raw:
        return _DEFAULT_SCHEDULE_LOCAL
    try:
        data = schedule_raw if isinstance(schedule_raw, dict) else json.loads(schedule_raw)
        start = data.get("start_utc") or data.get("sart_utc")
        end = data.get("end_utc")
        if not start or not end:
            return _DEFAULT_SCHEDULE_LOCAL
        return _time_utc_to_local(str(start)), _time_utc_to_local(str(end))
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        return _DEFAULT_SCHEDULE_LOCAL


def clean_max_people_count(value: int | None) -> int:
    if value is None or value < 1:
        return DEFAULT_MAX_PEOPLE_IN_CAR
    return min(int(value), 999)


def max_people_for_car(car: Car | None) -> int:
    if car is None:
        return DEFAULT_MAX_PEOPLE_IN_CAR
    return clean_max_people_count(getattr(car, "max_people_count_in_car", None))


def _save_schedule(db: Session, car_id: int, schedule_json: str) -> None:
    """Прямой UPDATE — без зависимости от ORM-маппинга колонки."""
    result = db.execute(
        text("UPDATE `cars` SET `schedule` = :schedule WHERE `id` = :id"),
        {"schedule": schedule_json, "id": car_id},
    )
    if result.rowcount != 1:
        raise ServiceError("Не вдалося зберегти розклад")


def create_car(
    db: Session,
    *,
    name: str,
    imei: str,
    cameras: list[dict],
    owner_user_id: int | None,
    company_name: str,
    comment: str | None,
    is_active: bool,
    difficulty_rating: int | None,
    count_source: str,
    source_priority: str,
    non_shedule_count: bool,
    alarms_only: bool,
    auto_gap_filling: bool,
    route_kind: list[str],
    route_kind_chose: bool = False,
    schedule_start: str = "06:00",
    schedule_end: str = "22:00",
    max_people_count_in_car: int | None = None,
) -> Car:
    name = name.strip()
    imei = imei.strip()
    if not name:
        raise ServiceError("Вкажіть назву машини")
    if not imei:
        raise ServiceError("Вкажіть IMEI")
    if owner_user_id is None:
        raise ServiceError("Оберіть обліковий запис-власника машини")
    if not company_name.strip():
        raise ServiceError("Вкажіть назву фірми")
    if _imei_taken(db, imei):
        raise ServiceError("Машина з таким IMEI вже існує")

    schedule_json = build_schedule_json(schedule_start, schedule_end)
    effective_source = count_source if count_source in COUNT_SOURCES else "photo"
    kinds = _clean_route_kind(route_kind)
    max_people = (
        clean_max_people_count(max_people_count_in_car)
        if "intercity" in kinds
        else DEFAULT_MAX_PEOPLE_IN_CAR
    )
    car = Car(
        name=name,
        imei=imei,
        cameras=cameras,
        owner_user_id=owner_user_id,
        company_name=company_name.strip(),
        comment=comment or None,
        is_active=is_active,
        difficulty_rating=difficulty_rating,
        count_source=effective_source,
        source_priority=_clean_source_priority(source_priority, effective_source),
        non_shedule_count=non_shedule_count,
        alarms_only=alarms_only,
        auto_gap_filling=auto_gap_filling,
        route_kind=kinds,
        route_kind_chose=route_kind_chose,
        max_people_count_in_car=max_people,
        schedule=schedule_json,
    )
    db.add(car)
    db.commit()
    db.refresh(car)
    # На случай, если ORM не записал колонку — допишем явно
    _save_schedule(db, car.id, schedule_json)
    db.commit()
    db.refresh(car)
    return car


def update_car(
    db: Session,
    car: Car,
    *,
    name: str,
    imei: str,
    cameras: list[dict],
    owner_user_id: int | None,
    company_name: str,
    comment: str | None,
    is_active: bool,
    difficulty_rating: int | None,
    count_source: str,
    source_priority: str,
    non_shedule_count: bool,
    alarms_only: bool,
    auto_gap_filling: bool,
    route_kind: list[str],
    route_kind_chose: bool = False,
    schedule_start: str = "06:00",
    schedule_end: str = "22:00",
    max_people_count_in_car: int | None = None,
) -> Car:
    name = name.strip()
    imei = imei.strip()
    if not name:
        raise ServiceError("Вкажіть назву машини")
    if not imei:
        raise ServiceError("Вкажіть IMEI")
    if owner_user_id is None:
        raise ServiceError("Оберіть обліковий запис-власника машини")
    if not company_name.strip():
        raise ServiceError("Вкажіть назву фірми")
    if _imei_taken(db, imei, exclude_id=car.id):
        raise ServiceError("Інша машина вже використовує цей IMEI")

    schedule_json = build_schedule_json(schedule_start, schedule_end)
    kinds = _clean_route_kind(route_kind)

    car.name = name
    car.imei = imei
    car.cameras = cameras
    car.owner_user_id = owner_user_id
    car.company_name = company_name.strip()
    car.comment = comment or None
    car.is_active = is_active
    car.difficulty_rating = difficulty_rating
    effective_source = count_source if count_source in COUNT_SOURCES else "photo"
    car.count_source = effective_source
    car.source_priority = _clean_source_priority(source_priority, effective_source)
    car.non_shedule_count = non_shedule_count
    car.alarms_only = alarms_only
    car.auto_gap_filling = auto_gap_filling
    car.route_kind = kinds
    car.route_kind_chose = route_kind_chose
    if "intercity" in kinds:
        car.max_people_count_in_car = clean_max_people_count(max_people_count_in_car)
    car.schedule = schedule_json

    db.commit()
    # Явный UPDATE schedule — гарантированно пишем в БД
    _save_schedule(db, car.id, schedule_json)
    db.commit()
    db.refresh(car)
    return car


def update_car_comment(db: Session, car: Car, comment: str | None) -> Car:
    car.comment = (comment or "").strip() or None
    db.commit()
    db.refresh(car)
    return car


def delete_car(db: Session, car: Car) -> None:
    """Удаляет машину вместе со всеми зависимостями.

    В реальной БД внешние ключи могли быть созданы без ON DELETE CASCADE,
    поэтому зависимые строки удаляем явно — это работает независимо от
    настроек ограничений и не роняет запрос в 500.
    """
    car_id = car.id
    db.execute(
        text("DELETE FROM `counting_daily_summary` WHERE `car_id` = :id"),
        {"id": car_id},
    )
    db.execute(
        text("DELETE FROM `counting_sessions` WHERE `car_id` = :id"),
        {"id": car_id},
    )
    db.execute(
        text("DELETE FROM `schedules` WHERE `car_id` = :id"), {"id": car_id}
    )
    db.execute(
        text("DELETE FROM `car_route_tariffs` WHERE `car_id` = :id"), {"id": car_id}
    )
    db.delete(car)
    db.commit()
