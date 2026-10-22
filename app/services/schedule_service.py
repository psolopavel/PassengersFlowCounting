"""Бизнес-логика по расписаниям (таблица `schedules`).

Расписание = привязка считальщика к машине + правила повторений.

JSON-форматы хранимых полей:

single_days   — ["2026-07-15", "2026-07-20"]
                Дополнительные дни подсчёта вне расписания.

recurring     — {"type": "weekly", "mode": "fixed", "days": [1,3,5],
                 "start": "2026-07-07"}
                або {"type": "weekly", "mode": "counter_choice",
                     "days_count": 3, "start": "2026-07-07"}
                або {"type": "every_n_weeks", "mode": "fixed", "every": 2,
                     "day": 1, "start": "2026-07-07"}
                або {"type": "every_n_weeks", "mode": "counter_choice",
                     "every": 2, "start": "2026-07-07"}
                Регулярное расписание.

exception_days — ["2026-07-14"]
                 Дни-исключения, которые пропускаются несмотря на расписание.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Car, Schedule, User

DAY_LABELS = {
    1: "Понеділок",
    2: "Вівторок",
    3: "Середа",
    4: "Четвер",
    5: "П'ятниця",
    6: "Субота",
    7: "Неділя",
}

# Горизонт перевірки перетину календарних дат (~12 тижнів).
_CONFLICT_LOOKAHEAD_DAYS = 84

# Типи підрахунку розкладу (schedules.type). whole_day — стара логіка
# (місто/міжмісто), final_stops — підрахунок лише на кінцевих зупинках.
SCHEDULE_TYPES = ("whole_day", "final_stops")
DEFAULT_SCHEDULE_TYPE = "whole_day"


def normalize_type(value: str | None) -> str:
    """Будь-яке значення типу зводимо до валідного (за замовчуванням whole_day)."""
    return value if value in SCHEDULE_TYPES else DEFAULT_SCHEDULE_TYPE


class ServiceError(Exception):
    pass


def _parse_json(raw: str | None) -> list | dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _dump_json(obj) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _recurring_mode(recurring: dict | None) -> str:
    if not recurring:
        return "fixed"
    return recurring.get("mode") or "fixed"


def validate_recurring(recurring: dict | None) -> None:
    if not recurring:
        return
    rtype = recurring.get("type")
    mode = _recurring_mode(recurring)
    if rtype == "weekly":
        if mode == "counter_choice":
            try:
                days_count = int(recurring.get("days_count") or 0)
            except (TypeError, ValueError):
                days_count = 0
            if days_count < 1 or days_count > 7:
                raise ServiceError("Кількість днів на тиждень — від 1 до 7")
        elif not recurring.get("days"):
            raise ServiceError("Оберіть хоча б один день тижня")
    elif rtype == "every_n_weeks":
        if mode != "counter_choice" and not recurring.get("day"):
            raise ServiceError("Оберіть день тижня")
    else:
        raise ServiceError("Невідомий тип розкладу")


def validate_schedule_content(
    recurring: dict | None,
    single_days: list[str] | None,
) -> None:
    """Потрібен або повторюваний розклад, або хоча б один окремий день."""
    singles = _as_str_list(single_days)
    if not recurring and not singles:
        raise ServiceError(
            "Заповніть правила повторення або додайте окремі дні підрахунку"
        )
    validate_recurring(recurring)


def _as_str_list(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _weekday_labels(days: set[int]) -> str:
    return ", ".join(DAY_LABELS.get(d, str(d)) for d in sorted(days))


def _weekly_fixed_weekdays(recurring: dict | None) -> set[int]:
    if not recurring or recurring.get("type") != "weekly":
        return set()
    if _recurring_mode(recurring) == "counter_choice":
        return set()
    out: set[int] = set()
    for d in recurring.get("days") or []:
        try:
            out.add(int(d))
        except (TypeError, ValueError):
            continue
    return out


def _weekly_choice_count(recurring: dict | None) -> int:
    if not recurring or recurring.get("type") != "weekly":
        return 0
    if _recurring_mode(recurring) != "counter_choice":
        return 0
    try:
        return int(recurring.get("days_count") or 0)
    except (TypeError, ValueError):
        return 0


def _is_every_n_counter_choice(recurring: dict | None) -> bool:
    return bool(
        recurring
        and recurring.get("type") == "every_n_weeks"
        and _recurring_mode(recurring) == "counter_choice"
    )


def _explicit_scheduled_dates(
    recurring: dict | None,
    single_days: list[str] | None,
    exception_days: list[str] | None,
    from_day: date,
    to_day: date,
) -> set[date]:
    out: set[date] = set()
    d = from_day
    while d <= to_day:
        if is_scheduled_on(recurring, single_days, exception_days, d):
            out.add(d)
        d += timedelta(days=1)
    return out


def _describe_pair_conflict(
    rec_a: dict | None,
    singles_a: list[str],
    except_a: list[str],
    rec_b: dict | None,
    singles_b: list[str],
    except_b: list[str],
    from_day: date,
    to_day: date,
) -> str | None:
    """Повертає текст конфлікту між двома розкладами або None."""
    dates_a = _explicit_scheduled_dates(rec_a, singles_a, except_a, from_day, to_day)
    dates_b = _explicit_scheduled_dates(rec_b, singles_b, except_b, from_day, to_day)
    overlap = sorted(dates_a & dates_b)
    if overlap:
        sample = ", ".join(d.strftime("%d.%m.%Y") for d in overlap[:5])
        extra = f" та ще {len(overlap) - 5}" if len(overlap) > 5 else ""
        return f"спільні дати підрахунку: {sample}{extra}"

    fixed_a = _weekly_fixed_weekdays(rec_a)
    fixed_b = _weekly_fixed_weekdays(rec_b)
    choice_a = _weekly_choice_count(rec_a)
    choice_b = _weekly_choice_count(rec_b)

    if fixed_a and fixed_b:
        common = fixed_a & fixed_b
        if common:
            return f"спільні дні тижня: {_weekday_labels(common)}"

    if fixed_a and choice_b and len(fixed_a) + choice_b > 7:
        return (
            f"інший рахувальник обирає {choice_b} дн./тиждень, "
            f"а тут зафіксовано {_weekday_labels(fixed_a)}"
        )
    if fixed_b and choice_a and len(fixed_b) + choice_a > 7:
        return (
            f"рахувальник обирає {choice_a} дн./тиждень, "
            f"а в іншому розкладі зафіксовано {_weekday_labels(fixed_b)}"
        )
    if choice_a and choice_b and choice_a + choice_b > 7:
        return (
            f"обидва рахувальники обирають дні на тиждень "
            f"({choice_a} і {choice_b}) — разом більше 7"
        )

    if _is_every_n_counter_choice(rec_a) and fixed_b and len(fixed_b) >= 7:
        return "інший розклад займає всі дні тижня"
    if _is_every_n_counter_choice(rec_b) and fixed_a and len(fixed_a) >= 7:
        return "цей розклад займає всі дні тижня"

    return None


def _counter_display(user: User | None) -> str:
    if user is None:
        return "?"
    return (user.name or user.email or f"#{user.id}").strip()


def check_schedule_conflicts(
    db: Session,
    *,
    car_id: int,
    counter_id: int,
    recurring: dict | None,
    single_days: list[str] | None,
    exception_days: list[str] | None,
    exclude_schedule_id: int | None = None,
    count_type: str = DEFAULT_SCHEDULE_TYPE,
) -> None:
    """Перевірка перетину з розкладами тієї ж машини в інших рахувальників.

    Перетин рахується ЛИШЕ в межах одного типу підрахунку (count_type): розклад
    whole_day і final_stops однієї машини незалежні й не конфліктують.
    """
    count_type = normalize_type(count_type)
    others = list(
        db.execute(
            select(Schedule)
            .where(
                Schedule.car_id == car_id,
                Schedule.counter_id != counter_id,
                Schedule.type == count_type,
            )
            .options(joinedload(Schedule.counter_user))
        ).scalars()
    )
    if exclude_schedule_id:
        others = [s for s in others if s.id != exclude_schedule_id]
    if not others:
        return

    singles = _as_str_list(single_days)
    excepts = _as_str_list(exception_days)
    from_day = date.today()
    to_day = from_day + timedelta(days=_CONFLICT_LOOKAHEAD_DAYS)

    for other in others:
        other_rec = _parse_json(other.recurring)
        if not isinstance(other_rec, dict):
            other_rec = None
        msg = _describe_pair_conflict(
            recurring,
            singles,
            excepts,
            other_rec,
            _as_str_list(_parse_json(other.single_days)),
            _as_str_list(_parse_json(other.exception_days)),
            from_day,
            to_day,
        )
        if msg:
            who = _counter_display(other.counter_user)
            raise ServiceError(
                f"Машина вже має перетинний розклад у рахувальника «{who}»: {msg}. "
                f"Змініть один із розкладів."
            )


def list_counters_with_schedules(
    db: Session, count_type: str = DEFAULT_SCHEDULE_TYPE
) -> list[dict]:
    """Все считальщики + их расписания выбранного типа, сгруппированные."""
    count_type = normalize_type(count_type)
    counters = list(
        db.execute(
            select(User)
            .where(User.role == "counter")
            .options(joinedload(User.counter))
            .order_by(User.name, User.id)
        ).scalars()
    )

    schedules = list(
        db.execute(
            select(Schedule)
            .where(Schedule.type == count_type)
            .options(joinedload(Schedule.car))
            .order_by(Schedule.car_id)
        ).scalars()
    )

    sched_by_counter: dict[int, list[Schedule]] = {}
    for s in schedules:
        sched_by_counter.setdefault(s.counter_id, []).append(s)

    result = []
    for c in counters:
        result.append({
            "user": c,
            "schedules": sched_by_counter.get(c.id, []),
        })
    return result


def get_schedule(db: Session, schedule_id: int) -> Schedule | None:
    return db.execute(
        select(Schedule)
        .where(Schedule.id == schedule_id)
        .options(joinedload(Schedule.car), joinedload(Schedule.counter_user))
    ).scalar_one_or_none()


def get_schedules_for_counter(
    db: Session, counter_id: int, count_type: str | None = None
) -> list[Schedule]:
    stmt = (
        select(Schedule)
        .where(Schedule.counter_id == counter_id)
        .options(joinedload(Schedule.car))
        .order_by(Schedule.car_id)
    )
    if count_type is not None:
        stmt = stmt.where(Schedule.type == normalize_type(count_type))
    return list(db.execute(stmt).scalars())


def list_available_cars(db: Session) -> list[Car]:
    return list(
        db.execute(
            select(Car).where(Car.is_active.is_(True)).order_by(Car.name)
        ).scalars()
    )


def create_schedule(
    db: Session,
    *,
    counter_id: int,
    car_id: int,
    recurring: dict | None,
    single_days: list[str] | None,
    exception_days: list[str] | None,
    comment: str | None,
    count_type: str = DEFAULT_SCHEDULE_TYPE,
) -> Schedule:
    count_type = normalize_type(count_type)
    counter = db.execute(
        select(User).where(User.id == counter_id, User.role == "counter")
    ).scalar_one_or_none()
    if counter is None:
        raise ServiceError("Рахувальника не знайдено")

    car = db.get(Car, car_id)
    if car is None:
        raise ServiceError("Машину не знайдено")

    existing = db.execute(
        select(Schedule).where(
            Schedule.counter_id == counter_id,
            Schedule.car_id == car_id,
            Schedule.type == count_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ServiceError("Розклад для цієї машини в цього рахувальника вже існує")

    validate_schedule_content(recurring, single_days)
    check_schedule_conflicts(
        db,
        car_id=car_id,
        counter_id=counter_id,
        recurring=recurring,
        single_days=single_days,
        exception_days=exception_days,
        count_type=count_type,
    )

    schedule = Schedule(
        counter_id=counter_id,
        car_id=car_id,
        type=count_type,
        recurring=_dump_json(recurring),
        single_days=_dump_json(single_days),
        exception_days=_dump_json(exception_days),
        comment=comment or None,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def update_schedule(
    db: Session,
    schedule: Schedule,
    *,
    recurring: dict | None,
    single_days: list[str] | None,
    exception_days: list[str] | None,
    comment: str | None,
) -> Schedule:
    validate_schedule_content(recurring, single_days)
    check_schedule_conflicts(
        db,
        car_id=schedule.car_id,
        counter_id=schedule.counter_id,
        recurring=recurring,
        single_days=single_days,
        exception_days=exception_days,
        exclude_schedule_id=schedule.id,
        count_type=schedule.type,
    )
    schedule.recurring = _dump_json(recurring)
    schedule.single_days = _dump_json(single_days)
    schedule.exception_days = _dump_json(exception_days)
    schedule.comment = comment or None
    db.commit()
    db.refresh(schedule)
    return schedule


def delete_schedule(db: Session, schedule: Schedule) -> None:
    db.delete(schedule)
    db.commit()


def _date_or_none(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def is_scheduled_on(
    recurring: dict | None,
    single_days: list[str] | None,
    exception_days: list[str] | None,
    day: date,
) -> bool:
    """Нужно ли считать машину в конкретный день `day`.

    Логика зеркалит compute_dates(): день считается, если он попадает в
    single_days или в recurring и при этом НЕ входит в exception_days.
    """
    iso = day.isoformat()
    if iso in (exception_days or []):
        return False
    if iso in (single_days or []):
        return True
    if not recurring:
        return False

    rtype = recurring.get("type")
    mode = _recurring_mode(recurring)
    if mode == "counter_choice":
        return False
    if rtype == "weekly":
        weekdays = set(recurring.get("days", []))
        start_date = _date_or_none(recurring.get("start"))
        if day.isoweekday() in weekdays and (start_date is None or day >= start_date):
            return True
    elif rtype == "every_n_weeks":
        every = recurring.get("every", 1) or 1
        day_of_week = recurring.get("day", 1)
        start_date = _date_or_none(recurring.get("start"))
        if start_date is None:
            return False
        if day.isoweekday() == day_of_week and day >= start_date:
            week_num = (day - start_date).days // 7
            if week_num % every == 0:
                return True
    return False


def compute_dates(
    recurring: dict | None,
    single_days: list[str] | None,
    exception_days: list[str] | None,
    year: int,
    month: int,
) -> list[dict]:
    """Вычислить все дни подсчёта за месяц. Возвращает список
    {"date": "2026-07-07", "source": "recurring|single", "excluded": bool}."""
    from calendar import monthrange

    _, days_in_month = monthrange(year, month)
    first = date(year, month, 1)
    last = date(year, month, days_in_month)

    recurring_dates: set[date] = set()
    if recurring:
        rtype = recurring.get("type")
        mode = _recurring_mode(recurring)
        if mode != "counter_choice" and rtype == "weekly":
            weekdays = set(recurring.get("days", []))
            start_str = recurring.get("start")
            if start_str:
                try:
                    start_date = date.fromisoformat(start_str)
                except ValueError:
                    start_date = None
            else:
                start_date = None

            d = first
            while d <= last:
                if d.isoweekday() in weekdays:
                    if start_date is None or d >= start_date:
                        recurring_dates.add(d)
                d += timedelta(days=1)

        elif mode != "counter_choice" and rtype == "every_n_weeks":
            every = recurring.get("every", 1)
            day_of_week = recurring.get("day", 1)
            start_str = recurring.get("start")
            if start_str:
                try:
                    start_date = date.fromisoformat(start_str)
                except ValueError:
                    start_date = first
            else:
                start_date = first

            d = first
            while d <= last:
                if d.isoweekday() == day_of_week:
                    delta_days = (d - start_date).days
                    week_num = delta_days // 7
                    if delta_days >= 0 and week_num % every == 0:
                        recurring_dates.add(d)
                d += timedelta(days=1)

    single_set: set[date] = set()
    for ds in (single_days or []):
        try:
            dt = date.fromisoformat(ds)
            if dt.year == year and dt.month == month:
                single_set.add(dt)
        except ValueError:
            continue

    exception_set: set[date] = set()
    for ds in (exception_days or []):
        try:
            dt = date.fromisoformat(ds)
            if dt.year == year and dt.month == month:
                exception_set.add(dt)
        except ValueError:
            continue

    all_dates = recurring_dates | single_set
    result = []
    for d in sorted(all_dates):
        excluded = d in exception_set
        source = "single" if d in single_set and d not in recurring_dates else "recurring"
        result.append({
            "date": d.isoformat(),
            "source": source,
            "excluded": excluded,
        })

    for d in sorted(exception_set - all_dates):
        result.append({
            "date": d.isoformat(),
            "source": "exception_only",
            "excluded": True,
        })

    result.sort(key=lambda x: x["date"])
    return result


def schedule_to_dict(s: Schedule) -> dict:
    """Сериализация расписания для JSON API."""
    return {
        "id": s.id,
        "counter_id": s.counter_id,
        "car_id": s.car_id,
        "type": s.type,
        "car_name": (
            f"{s.car.name} · {s.car.company_name}"
            if s.car and s.car.name and s.car.company_name
            else (s.car.name if s.car else "")
        ),
        "recurring": _parse_json(s.recurring),
        "single_days": _parse_json(s.single_days) or [],
        "exception_days": _parse_json(s.exception_days) or [],
        "comment": s.comment or "",
    }


def build_schedule_data(counters_data: list[dict]) -> dict[int, list[dict]]:
    """Данные расписаний для страницы (сериализуются в шаблоне через |tojson)."""
    payload: dict[int, list[dict]] = {}
    for item in counters_data:
        counter_id = item["user"].id
        payload[counter_id] = [
            schedule_to_dict(s) for s in item["schedules"]
        ]
    return payload
