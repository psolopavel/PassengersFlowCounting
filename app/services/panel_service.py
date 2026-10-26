"""Сборка данных для панели рахувальника (страница со списком машин).

Список строится из расписаний (schedules) рахувальника на выбранный день:
  * машина попадает в список, если день входит в её расписание и is_active = да;
  * доступ к подсчёту открывается, когда машина завершила работу по своему
    графику (now_utc >= day + end_utc);
  * надпись на кнопке зависит от counting_sessions (нет / in_progress / ready).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func as safunc
from sqlalchemy import cast, select, String
from sqlalchemy.orm import Session, joinedload

from app.models import Car, CountingSession, Schedule, Tracker, User
from app.services.car_service import parse_schedule_to_local
from app.services.counting_session_service import count_type_for_panel
from app.services.schedule_service import is_scheduled_on, normalize_type
from app.utils.local_tz import combine_local, local_today, parse_hhmm

# Legacy: в JSON графика время хранится как «UTC» = местное − 3ч (фикс.).
_SCHEDULE_LEGACY_OFFSET = timedelta(hours=3)
_DEFAULT_SCHEDULE_UTC = (time(3, 0, 0), time(19, 0, 0))

ROUTE_KIND_LABELS = {"city": "Місто", "intercity": "Міжмісто"}
PANEL_TYPE_LABELS = {
    "whole_day": "Цілий день",
    "final_stops": "Кінцеві зупинки",
}

SESSION_STATUS_LABELS = {
    None: "не підраховано",
    "in_progress": "в процессі",
    "ready": "підраховано",
    "skipped": "пропущено",
}

_DONE_STATUSES = frozenset({"ready", "skipped"})


# ── вспомогательные ──────────────────────────────────────────────────

def _parse_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_time(value: str) -> time:
    parts = re.split(r"[:.]", value.strip())
    if len(parts) < 2:
        raise ValueError(f"invalid time: {value!r}")
    h = int(parts[0]) % 24
    m = int(parts[1]) % 60
    s = int(parts[2]) % 60 if len(parts) > 2 else 0
    return time(h, m, s)


def parse_car_schedule_utc(raw) -> tuple[time, time]:
    """schedule из cars -> (start_utc, end_utc) как объекты time."""
    data = raw if isinstance(raw, dict) else _parse_json(raw)
    if not isinstance(data, dict):
        return _DEFAULT_SCHEDULE_UTC
    start = data.get("start_utc") or data.get("sart_utc") or "03:00:00"
    end = data.get("end_utc") or "19:00:00"
    try:
        return _parse_time(str(start)), _parse_time(str(end))
    except (ValueError, IndexError):
        return _DEFAULT_SCHEDULE_UTC


def _utc_time_to_local_str(t: time) -> str:
    dt = datetime(2000, 1, 1, t.hour, t.minute, t.second) + _SCHEDULE_LEGACY_OFFSET
    return dt.strftime("%H:%M")


def _car_route_kind(car: Car) -> str | None:
    """route_kind — SET, но по факту выбрано одно значення (місто/міжмісто)."""
    rk = car.route_kind
    if not rk:
        return None
    for k in ("city", "intercity"):
        if k in rk:
            return k
    return None


def _alternate_route_kind(kind: str) -> str:
    return "intercity" if kind == "city" else "city"


def _session_route_kind(session: CountingSession | None) -> str | None:
    if session is None:
        return None
    data = _parse_json(session.data) if session.data else None
    if data and data.get("route_kind_choice") in ("city", "intercity"):
        return data["route_kind_choice"]
    if session.kind in ("city", "intercity"):
        return session.kind
    return None


def _route_kind_row_fields(
    car: Car, session: CountingSession | None
) -> dict:
    """Поля типу маршруту для рядка панелі (дефолт з БД + вибір рахувальника)."""
    default = _car_route_kind(car) or "city"
    session_kind = _session_route_kind(session)
    effective = session_kind or default
    chose_enabled = bool(getattr(car, "route_kind_chose", False))
    kind_chosen = chose_enabled and effective != default
    # Змінити тип можна на будь-якому етапі, якщо авто це дозволяє:
    #  * немає сесії / skipped → тип застосується при «Почати підрахунок»;
    #  * ready                 → тип застосується при «Перерахувати» (пересбор);
    #  * in_progress           → тип застосується при «Перезібрати» (нова сесія).
    # «Дорахувати» тип не змінює — для зміни типу треба натиснути «Перезібрати».
    can_choose = chose_enabled
    return {
        "default_kind": default,
        "kind": effective,
        "kind_label": ROUTE_KIND_LABELS.get(effective, "—"),
        "kind_chosen": kind_chosen,
        "route_kind_chose": chose_enabled,
        "can_choose_kind": can_choose,
        "alternate_kind": _alternate_route_kind(default),
        "alternate_kind_label": ROUTE_KIND_LABELS.get(
            _alternate_route_kind(default), "—"
        ),
    }


def list_counters(db: Session) -> list[User]:
    return list(
        db.execute(
            select(User).where(User.role == "counter").order_by(User.name, User.id)
        ).scalars()
    )


def _normalize_imei(imei) -> str:
    """Единый ключ для сопоставления cars.imei и trackers.imei."""
    return str(imei).strip()


def _last_seen_map(
    gps_db: Session, imeis: set[str]
) -> tuple[dict[str, int], str | None, set[str]]:
    """last_seen по IMEI из gps.trackers.

    Возвращает (словарь imei->unix_ts, текст_ошибки_запроса, найденные_imei).
    Ключи всегда str — иначе int из MySQL не совпадёт со str из cars.imei.
    """
    keys = {_normalize_imei(i) for i in imeis if i and _normalize_imei(i)}
    if not keys:
        return {}, None, set()
    try:
        imei_str = cast(Tracker.imei, String)
        rows = gps_db.execute(
            select(imei_str, safunc.max(Tracker.last_seen))
            .where(imei_str.in_(keys))
            .group_by(imei_str)
        ).all()
    except Exception as exc:  # noqa: BLE001
        return {}, f"Помилка gps.trackers: {exc}", set()

    result: dict[str, int] = {}
    found: set[str] = set()
    for imei, ls in rows:
        key = _normalize_imei(imei)
        found.add(key)
        if ls is None:
            continue
        try:
            result[key] = int(ls)
        except (TypeError, ValueError):
            continue
    return result, None, found


def _sessions_map(
    db: Session, counter_id: int, day: date, *, count_type: str
) -> dict[int, CountingSession]:
    rows = db.execute(
        select(CountingSession).where(
            CountingSession.counter_id == counter_id,
            CountingSession.count_day == day,
            CountingSession.count_type == count_type,
        )
    ).scalars()
    return {s.car_id: s for s in rows}


def _sessions_in_range(
    db: Session,
    counter_id: int,
    from_day: date,
    to_day: date,
    *,
    count_type: str,
) -> dict[tuple[int, date], str]:
    rows = db.execute(
        select(
            CountingSession.car_id,
            CountingSession.count_day,
            CountingSession.status,
        ).where(
            CountingSession.counter_id == counter_id,
            CountingSession.count_day >= from_day,
            CountingSession.count_day <= to_day,
            CountingSession.count_type == count_type,
        )
    ).all()
    return {(car_id, count_day): status for car_id, count_day, status in rows}


def _external_done_map(
    db: Session, counter_id: int, day: date, *, count_type: str
) -> dict[int, tuple[CountingSession, str]]:
    """Авто, вже підраховані (ready) ІНШИМИ рахувальниками цього дня та типу.

    Повертає car_id -> (сесія, ім'я рахувальника). Один день+тип+авто рахується
    лише раз незалежно від того, хто саме порахував (напр. розклад «на розсуд»
    в іншого рахувальника)."""
    rows = db.execute(
        select(CountingSession, User.name, User.email)
        .join(User, User.id == CountingSession.counter_id, isouter=True)
        .where(
            CountingSession.counter_id != counter_id,
            CountingSession.count_day == day,
            CountingSession.count_type == count_type,
            CountingSession.status == "ready",
        )
        .order_by(CountingSession.updated_at.desc())
    ).all()
    out: dict[int, tuple[CountingSession, str]] = {}
    for sess, name, email in rows:
        if sess.car_id in out:
            continue
        who = (name or email or f"#{sess.counter_id}").strip()
        out[sess.car_id] = (sess, who)
    return out


def _external_done_in_range(
    db: Session,
    counter_id: int,
    from_day: date,
    to_day: date,
    *,
    count_type: str,
) -> set[tuple[int, date]]:
    """Пари (car_id, day), підраховані (ready) іншими рахувальниками у діапазоні."""
    rows = db.execute(
        select(CountingSession.car_id, CountingSession.count_day).where(
            CountingSession.counter_id != counter_id,
            CountingSession.count_day >= from_day,
            CountingSession.count_day <= to_day,
            CountingSession.count_type == count_type,
            CountingSession.status == "ready",
        )
    ).all()
    return {(car_id, count_day) for car_id, count_day in rows}


def _is_day_countable(ref_day: date, car: Car, now_utc: datetime) -> bool:
    if getattr(car, "non_shedule_count", False) and ref_day == local_today(now_utc):
        return True
    _start_s, end_s = parse_schedule_to_local(car.schedule)
    unlock_dt = combine_local(ref_day, parse_hhmm(end_s))
    start_utc, end_utc = parse_car_schedule_utc(car.schedule)
    if end_utc <= start_utc:
        unlock_dt += timedelta(days=1)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc >= unlock_dt.astimezone(timezone.utc)


def _date_or_none(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _iso_week_start(d: date) -> date:
    """Понеділок тижня, в який входить дата."""
    return d - timedelta(days=d.isoweekday() - 1)


def _counter_choice_required(recurring: dict | None, ref_day: date) -> int:
    """Скільки разів авто треба порахувати на тижні ref_day (режим «на розсуд»).

    * weekly / counter_choice        → days_count разів щотижня;
    * every_n_weeks / counter_choice → 1 раз у кожен N-й тиждень.
    Повертає 0, якщо тиждень поза розкладом або режим не «на розсуд».
    """
    if not recurring or recurring.get("mode") != "counter_choice":
        return 0

    start_date = _date_or_none(recurring.get("start"))
    ref_week = _iso_week_start(ref_day)
    if start_date is not None and ref_week < _iso_week_start(start_date):
        return 0

    rtype = recurring.get("type")
    if rtype == "weekly":
        try:
            n = int(recurring.get("days_count") or 0)
        except (TypeError, ValueError):
            n = 0
        return max(0, min(7, n))

    if rtype == "every_n_weeks":
        if start_date is None:
            return 0
        every = 1
        try:
            every = int(recurring.get("every") or 1) or 1
        except (TypeError, ValueError):
            every = 1
        week_index = (ref_week - _iso_week_start(start_date)).days // 7
        if week_index >= 0 and week_index % every == 0:
            return 1
        return 0

    return 0


def _last_seen_fields(
    car: Car,
    ls_map: dict[str, int],
    ls_error: str | None,
    ls_found: set[str],
) -> tuple[int | None, str | None]:
    imei_key = _normalize_imei(car.imei)
    last_seen = ls_map.get(imei_key)
    if last_seen is not None:
        return last_seen, None
    if ls_error:
        return None, ls_error
    if not imei_key:
        return None, "IMEI не вказано"
    if imei_key in ls_found:
        return None, "last_seen порожній у gps.trackers"
    return None, f"IMEI {imei_key} не знайдено в gps.trackers"


def _load_scheduled_items(
    db: Session, counter_id: int, schedule_type: str = "whole_day"
) -> list[tuple]:
    """Расписания рахувальника с уже распарсенным JSON."""
    schedule_type = normalize_type(schedule_type)
    schedules = db.execute(
        select(Schedule)
        .where(
            Schedule.counter_id == counter_id,
            Schedule.type == schedule_type,
        )
        .options(joinedload(Schedule.car))
    ).scalars()
    items = []
    for s in schedules:
        car = s.car
        if car is None or not car.is_active:
            continue
        items.append((
            s,
            car,
            _parse_json(s.recurring),
            _parse_json(s.single_days) or [],
            _parse_json(s.exception_days) or [],
        ))
    return items


def get_pending_days_summary(
    db: Session,
    *,
    counter_id: int,
    schedule_type: str = "whole_day",
    lookback_days: int = 90,
) -> list[dict]:
    """Дни с непідрахованими машинами (status != ready, доступ уже открыт).

    Возвращает список {day, count, overdue}, от новых к старым.
    """
    now_utc = datetime.now(timezone.utc)
    today_local = local_today(now_utc)
    start_day = today_local - timedelta(days=lookback_days)

    schedule_type = normalize_type(schedule_type)
    count_type = count_type_for_panel(schedule_type)
    items = _load_scheduled_items(db, counter_id, schedule_type)
    if not items:
        return []

    sessions = _sessions_in_range(
        db, counter_id, start_day, today_local, count_type=count_type
    )
    external_done = _external_done_in_range(
        db, counter_id, start_day, today_local, count_type=count_type
    )
    result: list[dict] = []

    day = start_day
    while day <= today_local:
        pending = 0
        overdue = False
        deadline = day + timedelta(days=1)
        for _s, car, recurring, single, exception in items:
            if not is_scheduled_on(recurring, single, exception, day):
                continue
            if not _is_day_countable(day, car, now_utc):
                continue
            if sessions.get((car.id, day)) in _DONE_STATUSES:
                continue
            if (car.id, day) in external_done:
                continue
            pending += 1
            if today_local > deadline:
                overdue = True
        if pending > 0:
            result.append({"day": day, "count": pending, "overdue": overdue})
        day += timedelta(days=1)

    result.sort(key=lambda x: x["day"], reverse=True)
    return result


def counter_has_car(
    db: Session,
    counter_id: int,
    car_id: int,
    schedule_type: str = "whole_day",
) -> bool:
    """Чи є машина в розкладі рахувальника."""
    schedule_type = normalize_type(schedule_type)
    row = db.execute(
        select(Schedule.id).where(
            Schedule.counter_id == counter_id,
            Schedule.car_id == car_id,
            Schedule.type == schedule_type,
        )
    ).scalar_one_or_none()
    return row is not None


def is_flexible_pick(
    db: Session,
    counter_id: int,
    car_id: int,
    ref_day: date,
    schedule_type: str = "whole_day",
) -> bool:
    """True, якщо для (counter, car) розклад «на розсуд» і ref_day — довільний вибір
    (не фіксований/додатковий день розкладу)."""
    schedule_type = normalize_type(schedule_type)
    s = db.execute(
        select(Schedule).where(
            Schedule.counter_id == counter_id,
            Schedule.car_id == car_id,
            Schedule.type == schedule_type,
        )
    ).scalar_one_or_none()
    if s is None:
        return False
    recurring = _parse_json(s.recurring)
    single = _parse_json(s.single_days) or []
    exception = _parse_json(s.exception_days) or []
    if is_scheduled_on(recurring, single, exception, ref_day):
        return False
    return _counter_choice_required(recurring, ref_day) > 0


def get_flexible_weeks_summary(
    db: Session,
    *,
    counter_id: int,
    schedule_type: str = "whole_day",
    lookback_weeks: int = 12,
) -> list[dict]:
    """Тижні з невиконаними «довільними» підрахунками (розклад «на розсуд»).

    Повертає список {week_start, week_end, count, overdue, is_current, is_last},
    від нових тижнів до старих. count — скільки авто ще треба порахувати того тижня.
    """
    now_utc = datetime.now(timezone.utc)
    today_local = local_today(now_utc)
    cur_week_start = _iso_week_start(today_local)
    last_week_start = cur_week_start - timedelta(days=7)
    start_week = cur_week_start - timedelta(days=7 * lookback_weeks)

    schedule_type = normalize_type(schedule_type)
    count_type = count_type_for_panel(schedule_type)
    items = _load_scheduled_items(db, counter_id, schedule_type)
    flex_items = [
        (car, recurring)
        for (_s, car, recurring, _single, _exc) in items
        if recurring and recurring.get("mode") == "counter_choice"
    ]
    if not flex_items:
        return []

    span_end = cur_week_start + timedelta(days=6)
    sessions = _sessions_in_range(
        db, counter_id, start_week, span_end, count_type=count_type
    )
    used: dict[tuple[int, date], int] = {}
    for (car_id, d), _status in sessions.items():
        wk = _iso_week_start(d)
        used[(car_id, wk)] = used.get((car_id, wk), 0) + 1

    result: list[dict] = []
    week = start_week
    while week <= cur_week_start:
        week_end = week + timedelta(days=6)
        cars = 0
        for car, recurring in flex_items:
            required = _counter_choice_required(recurring, week)
            if required <= 0:
                continue
            if required - used.get((car.id, week), 0) > 0:
                cars += 1
        if cars > 0:
            result.append({
                "week_start": week,
                "week_end": week_end,
                "count": cars,
                "overdue": week_end < today_local,
                "is_current": week == cur_week_start,
                "is_last": week == last_week_start,
            })
        week += timedelta(days=7)

    result.sort(key=lambda x: x["week_start"], reverse=True)
    return result


def _row_kind_fields(
    car: Car, session: CountingSession | None, schedule_type: str
) -> dict:
    if schedule_type == "final_stops":
        return {
            "default_kind": "final_stop",
            "kind": "final_stop",
            "kind_label": "Кінцеві зупинки",
            "kind_chosen": False,
            "route_kind_chose": False,
            "can_choose_kind": False,
            "alternate_kind": "",
            "alternate_kind_label": "—",
        }
    return _route_kind_row_fields(car, session)


# ── основная сборка ──────────────────────────────────────────────────

def _make_external_done_row(
    car: Car,
    *,
    ref_day: date,
    schedule_type: str,
    external_done: tuple[CountingSession, str],
    ls_map: dict[str, int],
    ls_error: str | None,
    ls_found: set[str],
) -> dict:
    """Рядок для авто, підрахованого іншим рахувальником (лише перегляд)."""
    ext_session, who = external_done
    deadline = ref_day + timedelta(days=1)
    kind_fields = _row_kind_fields(car, ext_session, schedule_type)
    comment = (car.comment or "").strip() or None
    start_utc, end_utc = parse_car_schedule_utc(car.schedule)
    last_seen, last_seen_hint = _last_seen_fields(car, ls_map, ls_error, ls_found)

    return {
        "car": car,
        **kind_fields,
        "count_day": ref_day,
        "deadline": deadline,
        "last_seen": last_seen,
        "last_seen_hint": last_seen_hint,
        "schedule_local": (
            _utc_time_to_local_str(start_utc),
            _utc_time_to_local_str(end_utc),
        ),
        "countable": True,
        "status": "ready",
        "status_label": SESSION_STATUS_LABELS.get("ready", "підраховано"),
        "action": None,
        "button_label": "Підраховано",
        "can_skip": False,
        "can_delete": False,
        "use_start_form": False,
        "show_rebuild": False,
        "flexible": False,
        "remaining": None,
        "overdue": False,
        "comment": comment,
        "skip_comment": None,
        "counted_by_other": True,
        "other_counter_name": who,
        "schedule_type": schedule_type,
        "count_url": None,
    }


def _make_row(
    car: Car,
    *,
    counter_id: int,
    ref_day: date,
    today_local: date,
    now_utc: datetime,
    session: CountingSession | None,
    schedule_type: str,
    ls_map: dict[str, int],
    ls_error: str | None,
    ls_found: set[str],
    external_done: tuple[CountingSession, str] | None = None,
) -> dict:
    """Звичайний рядок: конкретний день підрахунку (фіксований розклад або пік)."""
    deadline = ref_day + timedelta(days=1)
    countable = _is_day_countable(ref_day, car, now_utc)
    status = session.status if session else None

    # Немає власної сесії, але авто вже підраховане іншим рахувальником —
    # показуємо як готове (тільки для перегляду), без кнопок дій.
    if session is None and external_done is not None:
        return _make_external_done_row(
            car,
            ref_day=ref_day,
            schedule_type=schedule_type,
            external_done=external_done,
            ls_map=ls_map,
            ls_error=ls_error,
            ls_found=ls_found,
        )

    if not countable:
        action, label = None, "Ще недоступно"
        can_skip = False
        use_start_form = False
    elif status is None:
        action, label = "start", "Почати підрахунок"
        can_skip = True
        use_start_form = False
    elif status == "in_progress":
        action, label = "continue", "Дорахувати"
        can_skip = True
        use_start_form = False
    elif status == "skipped":
        action, label = "continue", "Дорахувати"
        can_skip = False
        use_start_form = True
    else:
        action, label = "recount", "Перерахувати"
        can_skip = False
        use_start_form = False

    kind_fields = _row_kind_fields(car, session, schedule_type)
    kind = kind_fields["kind"]
    show_rebuild = session is not None and (
        schedule_type == "final_stops"
        or kind == "intercity"
        or kind_fields["route_kind_chose"]
    )

    if action == "start" and (
        schedule_type == "final_stops" or kind_fields["route_kind_chose"]
    ):
        use_start_form = True
    comment = (car.comment or "").strip() or None
    start_utc, end_utc = parse_car_schedule_utc(car.schedule)
    last_seen, last_seen_hint = _last_seen_fields(car, ls_map, ls_error, ls_found)

    # Сесія обрана рахувальником довільно (розклад «на розсуд») + пропущена —
    # даємо змогу видалити запис і обрати інший день.
    session_data = _parse_json(session.data) if session and session.data else None
    is_flexible_session = bool(session_data and session_data.get("counter_choice"))
    can_delete = is_flexible_session and status in ("skipped", "ready")
    skip_comment = None
    if status == "skipped" and session_data:
        raw_skip = (session_data.get("skip_comment") or "").strip()
        skip_comment = raw_skip or None

    return {
        "car": car,
        **kind_fields,
        "count_day": ref_day,
        "deadline": deadline,
        "last_seen": last_seen,
        "last_seen_hint": last_seen_hint,
        "schedule_local": (
            _utc_time_to_local_str(start_utc),
            _utc_time_to_local_str(end_utc),
        ),
        "countable": countable,
        "status": status,
        "status_label": SESSION_STATUS_LABELS.get(status, "—"),
        "action": action,
        "button_label": label,
        "can_skip": can_skip,
        "can_delete": can_delete,
        "use_start_form": use_start_form,
        "show_rebuild": show_rebuild,
        "flexible": False,
        "remaining": None,
        "overdue": status not in _DONE_STATUSES and today_local > deadline,
        "comment": comment,
        "skip_comment": skip_comment,
        "counted_by_other": False,
        "other_counter_name": None,
        "schedule_type": schedule_type,
        "count_url": (
            f"/panel/count/{car.id}?day={ref_day.isoformat()}"
            f"&type={schedule_type}&counter_id={counter_id}"
        ),
    }


def _make_flexible_row(
    car: Car,
    *,
    counter_id: int,
    ref_day: date,
    now_utc: datetime,
    remaining: int,
    schedule_type: str,
    ls_map: dict[str, int],
    ls_error: str | None,
    ls_found: set[str],
) -> dict:
    """Гнучкий рядок: авто треба порахувати будь-якого дня тижня (режим «на розсуд»)."""
    countable = _is_day_countable(ref_day, car, now_utc)
    if countable:
        action, label = "start", "Почати підрахунок"
        can_skip = True
    else:
        action, label = None, "Ще недоступно"
        can_skip = False

    kind_fields = _row_kind_fields(car, None, schedule_type)
    comment = (car.comment or "").strip() or None
    start_utc, end_utc = parse_car_schedule_utc(car.schedule)
    last_seen, last_seen_hint = _last_seen_fields(car, ls_map, ls_error, ls_found)

    use_start_form = action == "start" and (
        schedule_type == "final_stops" or kind_fields["route_kind_chose"]
    )

    return {
        "car": car,
        **kind_fields,
        "count_day": None,
        "deadline": None,
        "last_seen": last_seen,
        "last_seen_hint": last_seen_hint,
        "schedule_local": (
            _utc_time_to_local_str(start_utc),
            _utc_time_to_local_str(end_utc),
        ),
        "countable": countable,
        "status": None,
        "status_label": SESSION_STATUS_LABELS.get(None, "—"),
        "action": action,
        "button_label": label,
        "can_skip": can_skip,
        "can_delete": False,
        "use_start_form": use_start_form,
        "show_rebuild": False,
        "flexible": True,
        "remaining": remaining,
        "overdue": False,
        "comment": comment,
        "schedule_type": schedule_type,
        "count_url": (
            f"/panel/count/{car.id}?day={ref_day.isoformat()}"
            f"&type={schedule_type}&counter_id={counter_id}"
        ),
    }


def _build_rows(
    db: Session,
    gps_db: Session,
    *,
    counter_id: int,
    ref_day: date,
    today_local: date,
    now_utc: datetime,
    schedule_type: str = "whole_day",
) -> tuple[list[dict], list[dict]]:
    schedule_type = normalize_type(schedule_type)
    count_type = count_type_for_panel(schedule_type)
    schedules = db.execute(
        select(Schedule)
        .where(
            Schedule.counter_id == counter_id,
            Schedule.type == schedule_type,
        )
        .options(joinedload(Schedule.car))
    ).scalars()

    sess_map = _sessions_map(db, counter_id, ref_day, count_type=count_type)
    external_done = _external_done_map(
        db, counter_id, ref_day, count_type=count_type
    )

    week_start = _iso_week_start(ref_day)
    week_end = week_start + timedelta(days=6)
    week_sessions = _sessions_in_range(
        db, counter_id, week_start, week_end, count_type=count_type
    )
    week_used_by_car: dict[int, int] = {}
    for (car_id, _day), _status in week_sessions.items():
        week_used_by_car[car_id] = week_used_by_car.get(car_id, 0) + 1

    concrete: list[tuple[Schedule, Car]] = []
    flexible: list[tuple[Schedule, Car, int]] = []
    for s in schedules:
        car = s.car
        if car is None or not car.is_active:
            continue
        recurring = _parse_json(s.recurring)
        single = _parse_json(s.single_days) or []
        exception = _parse_json(s.exception_days) or []

        is_concrete = is_scheduled_on(recurring, single, exception, ref_day)
        has_session_today = car.id in sess_map
        done_by_other = car.id in external_done
        if is_concrete or has_session_today or done_by_other:
            concrete.append((s, car))
            continue

        required = _counter_choice_required(recurring, ref_day)
        if required > 0:
            remaining = required - week_used_by_car.get(car.id, 0)
            if remaining > 0:
                flexible.append((s, car, remaining))

    all_cars = [c for _s, c in concrete] + [c for _s, c, _r in flexible]
    ls_map, ls_error, ls_found = _last_seen_map(
        gps_db, {c.imei for c in all_cars if c.imei}
    )

    rows = [
        _make_row(
            car,
            counter_id=counter_id,
            ref_day=ref_day,
            today_local=today_local,
            now_utc=now_utc,
            session=sess_map.get(car.id),
            schedule_type=schedule_type,
            ls_map=ls_map,
            ls_error=ls_error,
            ls_found=ls_found,
            external_done=external_done.get(car.id),
        )
        for _s, car in concrete
    ]
    flexible_rows = [
        _make_flexible_row(
            car,
            counter_id=counter_id,
            ref_day=ref_day,
            now_utc=now_utc,
            remaining=remaining,
            schedule_type=schedule_type,
            ls_map=ls_map,
            ls_error=ls_error,
            ls_found=ls_found,
        )
        for _s, car, remaining in flexible
    ]

    # Сначала те, что нужно посчитать (не ready/skipped), потом по названию.
    rows.sort(
        key=lambda r: (r["status"] in _DONE_STATUSES, (r["car"].name or "").lower())
    )
    flexible_rows.sort(key=lambda r: (r["car"].name or "").lower())
    return rows, flexible_rows


def get_panel_view(
    db: Session,
    gps_db: Session,
    *,
    counter_id: int,
    ref_day: date,
    schedule_type: str = "whole_day",
) -> dict:
    now_utc = datetime.now(timezone.utc)
    today_local = local_today(now_utc)
    schedule_type = normalize_type(schedule_type)

    rows, flexible_rows = _build_rows(
        db, gps_db,
        counter_id=counter_id,
        ref_day=ref_day,
        today_local=today_local,
        now_utc=now_utc,
        schedule_type=schedule_type,
    )

    pending_count = (
        sum(1 for r in rows if r["status"] not in _DONE_STATUSES)
        + sum(1 for r in flexible_rows if r["status"] not in _DONE_STATUSES)
    )
    total = len(rows) + len(flexible_rows)

    return {
        "rows": rows,
        "flexible_rows": flexible_rows,
        "total": total,
        "pending_count": pending_count,
        "today_local": today_local,
    }
