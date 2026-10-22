"""Статистика підрахунків для менеджера (сторінка /admin/statistics).

Дані беруться з колонок `counting_sessions`; з JSON `data` читається лише `skip_comment`
для пропущених сесій,
з приєднанням `cars` / `users` для назв та фото. Два виміри:

  * by_counter (за замовчуванням) — обираємо рахувальника, у списку машини;
  * by_car — обираємо машину, у списку оператори (ім'я + фото).

Домовленості (узгоджено з користувачем):
  * «коли мав відбутися» = дедлайн = count_day + 1 день;
  * «коли відбувся» = updated_at (наближення, окремого поля немає);
  * над списком — зведення (ready / skipped / in_progress, сума часу, прострочено).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Car, CountingSession, User

# Локальний час інтерфейсу — UTC+3 (як і в решті проєкту).
_LOCAL_OFFSET = timedelta(hours=3)

DIMENSIONS = ("counter", "car")
DIMENSION_LABELS = {"counter": "За рахувальником", "car": "За машиною"}

KIND_LABELS = {
    "city": "Місто",
    "intercity": "Міжмісто",
    "final_stop": "Кінцеві зупинки",
}
STATUS_LABELS = {
    "in_progress": "в процесі",
    "ready": "підраховано",
    "skipped": "пропущено",
}
_DONE_STATUSES = frozenset({"ready", "skipped"})


# ── списки сутностей для вибору ───────────────────────────────────────

def list_counters(db: Session) -> list[User]:
    return list(
        db.execute(
            select(User)
            .where(User.role == "counter")
            .order_by(User.name, User.id)
        ).scalars()
    )


def list_cars(db: Session) -> list[Car]:
    return list(
        db.execute(
            select(Car).order_by(Car.name, Car.imei)
        ).scalars()
    )


# ── допоміжні ─────────────────────────────────────────────────────────

def _to_local(dt: datetime | None) -> datetime | None:
    """Привести UTC-datetime із БД до локального часу (UTC+3)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) + _LOCAL_OFFSET).replace(tzinfo=None)


def _deadline(count_day: date) -> date:
    return count_day + timedelta(days=1)


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def default_date_range(today: date) -> tuple[date, date]:
    """Понеділок поточного тижня — сьогодні."""
    return monday_of_week(today), today


def preset_date_range(preset: str, today: date) -> tuple[date, date] | None:
    if preset == "week":
        return monday_of_week(today), today
    if preset == "2weeks":
        return monday_of_week(today) - timedelta(days=7), today
    if preset == "month":
        return today.replace(day=1), today
    return None


def _parse_session_data(raw) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _skip_comment_from_data(status: str, raw_data) -> str | None:
    if status != "skipped":
        return None
    text = (_parse_session_data(raw_data).get("skip_comment") or "").strip()
    return text or None


def _session_kind_key(session: CountingSession) -> str:
    """Ключ для фільтра «Тип» у таблиці статистики."""
    count_type = getattr(session, "count_type", None) or "full_day"
    if count_type == "finish_stops" or session.kind == "final_stop":
        return "final_stop"
    return session.kind or "city"


def _session_kind_label(session: CountingSession) -> str:
    return KIND_LABELS.get(_session_kind_key(session), session.kind or "—")


# ── основна вибірка ───────────────────────────────────────────────────

def analyze(
    db: Session,
    *,
    dimension: str,
    entity_id: int,
    from_day: date,
    to_day: date,
) -> dict:
    """Повертає рядки статистики + зведення за діапазон [from_day, to_day].

    dimension:
      * "counter" — entity_id = users.id рахувальника, у рядках дані машин;
      * "car"     — entity_id = cars.id машини, у рядках дані операторів.
    """
    if dimension not in DIMENSIONS:
        dimension = "counter"

    stmt = (
        select(CountingSession)
        .where(
            CountingSession.count_day >= from_day,
            CountingSession.count_day <= to_day,
        )
        .order_by(CountingSession.count_day.desc(), CountingSession.id.desc())
    )
    if dimension == "counter":
        stmt = stmt.where(CountingSession.counter_id == entity_id)
    else:
        stmt = stmt.where(CountingSession.car_id == entity_id)

    sessions = list(db.execute(stmt).scalars())

    # Довантажуємо пов'язані сутності одним запитом.
    if dimension == "counter":
        related_ids = {s.car_id for s in sessions}
        related = {
            c.id: c
            for c in db.execute(
                select(Car).where(Car.id.in_(related_ids))
            ).scalars()
        } if related_ids else {}
    else:
        related_ids = {s.counter_id for s in sessions}
        related = {
            u.id: u
            for u in db.execute(
                select(User).where(User.id.in_(related_ids))
            ).scalars()
        } if related_ids else {}

    rows: list[dict] = []
    summary = {
        "total": 0,
        "ready": 0,
        "skipped": 0,
        "in_progress": 0,
        "overdue": 0,
        "time_spent_sec": 0,
    }

    for s in sessions:
        deadline = _deadline(s.count_day)
        done_at = _to_local(s.updated_at)
        overdue = _is_overdue(s.status, done_at, deadline)

        row = {
            "session_id": s.id,
            "count_day": s.count_day,
            "deadline": deadline,
            "done_at": done_at,
            "time_spent_sec": int(s.time_spent_sec or 0),
            "status": s.status,
            "status_label": STATUS_LABELS.get(s.status, s.status),
            "kind": _session_kind_key(s),
            "kind_label": _session_kind_label(s),
            "count_type": getattr(s, "count_type", None) or "full_day",
            "overdue": overdue,
            "skip_comment": _skip_comment_from_data(s.status, s.data),
        }

        if dimension == "counter":
            car = related.get(s.car_id)
            row["entity"] = {
                "id": s.car_id,
                "title": (car.name or car.imei) if car else f"#{s.car_id}",
                "subtitle": car.imei if car else "",
                "photo_url": None,
            }
        else:
            op = related.get(s.counter_id)
            row["entity"] = {
                "id": s.counter_id,
                "title": (op.name or op.email) if op else f"#{s.counter_id}",
                "subtitle": op.email if op else "",
                "photo_url": op.photo_url if op else None,
            }

        rows.append(row)

        summary["total"] += 1
        summary["time_spent_sec"] += row["time_spent_sec"]
        if s.status in summary:
            summary[s.status] += 1
        if overdue:
            summary["overdue"] += 1

    return {"rows": rows, "summary": summary}


def _is_overdue(status: str, done_at: datetime | None, deadline: date) -> bool:
    """Прострочено, якщо підрахунок завершено пізніше дедлайну,
    або ще не завершено (in_progress) і сьогодні вже після дедлайну."""
    if status == "ready":
        return done_at is not None and done_at.date() > deadline
    if status in _DONE_STATUSES:  # skipped
        return False
    # in_progress / інше
    today_local = (datetime.now(timezone.utc) + _LOCAL_OFFSET).date()
    return today_local > deadline


def get_entity(db: Session, dimension: str, entity_id: int):
    """Обрана сутність (для заголовка сторінки результатів)."""
    if dimension == "car":
        return db.get(Car, entity_id)
    return db.get(User, entity_id)
