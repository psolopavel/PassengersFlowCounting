"""Жизненный цикл сессии подсчёта (таблица counting_sessions).

Кнопки в панели:
  * «Почати підрахунок» — start_or_get: создаёт сессию и синхронно собирает data;
  * «Дорахувати»        — продолжение (сессия уже in_progress или skipped);
  * «Пропустити»        — skip_session: создаёт/обновляет сессию со status=skipped;
  * «Перерахувати»/«Скинути» — reset: пересобирает data, но переносит уже
     введённые count туда, где отрезок не изменился (умное скидання);
  * «Перезібрати» (міжмісто) — delete_session + start_or_get с нуля;
  * автосейв             — save_progress (data + time_spent_sec);
  * завершение           — set_status(ready).
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CountingSession
from app.services import counting_build_service

# Допуск сдвига границ отрезка (сек) для переноса count при скиданні.
_SEG_TIME_TOL = 60
_MISSING = object()

WHOLE_DAY_KINDS = ("city", "intercity")
FINAL_STOP_KIND = "final_stop"
COUNT_TYPE_FULL_DAY = "full_day"
COUNT_TYPE_FINISH_STOPS = "finish_stops"


def count_type_for_panel(panel_type: str) -> str:
    """Тип панелі (schedules.type) → count_type сесії/summary."""
    if panel_type == "final_stops":
        return COUNT_TYPE_FINISH_STOPS
    return COUNT_TYPE_FULL_DAY


def get_session(
    db: Session,
    counter_id: int,
    car_id: int,
    day: date,
    *,
    count_type: str = COUNT_TYPE_FULL_DAY,
) -> CountingSession | None:
    stmt = (
        select(CountingSession)
        .where(
            CountingSession.counter_id == counter_id,
            CountingSession.car_id == car_id,
            CountingSession.count_day == day,
            CountingSession.count_type == count_type,
        )
        .order_by(CountingSession.updated_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def load_data(session: CountingSession) -> dict | None:
    if not session.data:
        return None
    try:
        return json.loads(session.data)
    except (json.JSONDecodeError, TypeError):
        return None


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _preserve_counter_choice(session: CountingSession, data: dict) -> dict:
    """Не втрачати позначку довільного дня рахувальника при оновленні data."""
    prev = load_data(session) or {}
    if prev.get("counter_choice"):
        data["counter_choice"] = True
    return data


def _preserve_route_kind_choice(session: CountingSession, data: dict) -> dict:
    prev = load_data(session) or {}
    if prev.get("route_kind_choice") in ("city", "intercity"):
        data["route_kind_choice"] = prev["route_kind_choice"]
    return data


def _preserve_session_flags(session: CountingSession, data: dict) -> dict:
    data = _preserve_counter_choice(session, data)
    return _preserve_route_kind_choice(session, data)


def _default_kind_for_car(car) -> str:
    if car and car.route_kind and "intercity" in car.route_kind:
        return "intercity"
    return "city"


def _resolve_kind(car, route_kind: str | None = None) -> str:
    """Тип сесії: дефолт з cars.route_kind або вибір рахувальника."""
    default = _default_kind_for_car(car)
    if getattr(car, "route_kind_chose", False) and route_kind in ("city", "intercity"):
        return route_kind
    return default


def _apply_route_kind_choice(data: dict, car, kind: str) -> dict:
    default = _default_kind_for_car(car)
    if getattr(car, "route_kind_chose", False) and kind != default:
        data["route_kind_choice"] = kind
    elif "route_kind_choice" in data and kind == default:
        data.pop("route_kind_choice", None)
    return data


def _apply_skip_comment(data: dict, comment: str | None) -> dict:
    text = (comment or "").strip()
    if text:
        data["skip_comment"] = text
    else:
        data.pop("skip_comment", None)
    return data


def _session_kind_for_car(car, route_kind: str | None = None) -> str:
    return _resolve_kind(car, route_kind)


def _build_kind_for_panel(car, panel_type: str, route_kind: str | None = None) -> str:
    if panel_type == "final_stops":
        return FINAL_STOP_KIND
    return _resolve_kind(car, route_kind)


def start_or_get(
    db: Session,
    gps_db: Session,
    routes_db: Session,
    *,
    counter_id: int,
    car,
    day: date,
    flexible: bool = False,
    route_kind: str | None = None,
    panel_type: str = "whole_day",
) -> CountingSession:
    """Возвращает существующую сессию или создаёт новую со свежесобранной data.

    flexible=True — день обран рахувальником довільно (розклад «на розсуд»).
    panel_type — режим панелі (whole_day / final_stops), визначає kind і count_type.
    """
    session_count_type = count_type_for_panel(panel_type)
    existing = get_session(db, counter_id, car.id, day, count_type=session_count_type)
    if existing is not None:
        if existing.status == "skipped":
            data = load_data(existing) or {}
            was_flexible = bool(data.get("counter_choice"))
            if existing.kind == FINAL_STOP_KIND:
                build_kind = FINAL_STOP_KIND
            else:
                stored_kind = data.get("route_kind_choice")
                build_kind = _resolve_kind(car, route_kind or stored_kind)
            if not data.get("kind"):
                built = counting_build_service.build_data(
                    db, gps_db, routes_db, car, day, kind=build_kind
                )
                if was_flexible or flexible:
                    built["counter_choice"] = True
                if build_kind != FINAL_STOP_KIND:
                    built = _apply_route_kind_choice(built, car, build_kind)
                data = built
            elif flexible or was_flexible:
                data["counter_choice"] = True
            if build_kind != FINAL_STOP_KIND:
                data = _apply_route_kind_choice(data, car, build_kind)
            data.pop("skip_comment", None)
            existing.data = _dump(data)
            existing.kind = build_kind
            existing.count_type = session_count_type
            existing.status = "in_progress"
            db.commit()
            db.refresh(existing)
        return existing

    effective_kind = _build_kind_for_panel(car, panel_type, route_kind)
    data = counting_build_service.build_data(
        db, gps_db, routes_db, car, day, kind=effective_kind
    )
    if flexible:
        data["counter_choice"] = True
    if effective_kind != FINAL_STOP_KIND:
        data = _apply_route_kind_choice(data, car, effective_kind)
    session = CountingSession(
        counter_id=counter_id,
        car_id=car.id,
        count_day=day,
        status="in_progress",
        kind=effective_kind,
        count_type=session_count_type,
        time_spent_sec=0,
        data=_dump(data),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def skip_session(
    db: Session,
    *,
    counter_id: int,
    car_id: int,
    day: date,
    car=None,
    flexible: bool = False,
    route_kind: str | None = None,
    comment: str | None = None,
    panel_type: str = "whole_day",
) -> CountingSession:
    """Пропуск подсчёта: создаёт пустую сессию или меняет статус существующей.

    flexible=True — день обран рахувальником довільно; позначка йде в data.
    comment — причина пропуску, зберігається в data.skip_comment.
    """
    session_count_type = count_type_for_panel(panel_type)
    session_kind = _build_kind_for_panel(car, panel_type, route_kind)
    existing = get_session(db, counter_id, car_id, day, count_type=session_count_type)
    if existing is not None:
        existing.status = "skipped"
        data = load_data(existing) or {}
        if flexible:
            data["counter_choice"] = True
        if session_kind != FINAL_STOP_KIND:
            data = _apply_route_kind_choice(data, car, session_kind)
        data = _apply_skip_comment(data, comment)
        existing.data = _dump(data) if data else None
        existing.kind = session_kind
        existing.count_type = session_count_type
        db.commit()
        db.refresh(existing)
        return existing

    payload: dict = {}
    if flexible:
        payload["counter_choice"] = True
    if session_kind != FINAL_STOP_KIND:
        payload = _apply_route_kind_choice(payload, car, session_kind)
    payload = _apply_skip_comment(payload, comment)
    session = CountingSession(
        counter_id=counter_id,
        car_id=car_id,
        count_day=day,
        status="skipped",
        kind=session_kind,
        count_type=session_count_type,
        time_spent_sec=0,
        data=_dump(payload) if payload else None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def update_skip_comment(
    db: Session,
    session: CountingSession,
    comment: str | None,
) -> CountingSession:
    """Оновлює причину пропуску в data.skip_comment для сесії зі status=skipped."""
    if session.status != "skipped":
        raise ValueError("session is not skipped")
    data = load_data(session) or {}
    data = _apply_skip_comment(data, comment)
    session.data = _dump(data) if data else None
    db.commit()
    db.refresh(session)
    return session


def delete_session(db: Session, session: CountingSession) -> None:
    db.delete(session)
    db.commit()


def _parse_city_total(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        n = float(raw)
        if n < 0:
            return None
        return n
    except (TypeError, ValueError):
        return None


def save_city_fields(
    db: Session,
    session: CountingSession,
    *,
    total=_MISSING,
    add_time_sec: int = 0,
) -> CountingSession:
    """Автосейв / фінальне збереження міста: total + time_spent."""
    data = load_data(session) or {}
    if total is not _MISSING:
        data.setdefault("result", {})["in_salon_total"] = _parse_city_total(total)
    data = _preserve_session_flags(session, data)
    return save_progress(db, session, data=data, add_time_sec=add_time_sec)


def save_intercity_fields(
    db: Session,
    session: CountingSession,
    *,
    counts=_MISSING,
    add_time_sec: int = 0,
) -> CountingSession:
    """Автосейв / фінальне збереження міжміста: counts по seg_key + time_spent.

    counts — dict {seg_key: int|null}. Оновлюються лише відомі відрізки.
    """
    data = load_data(session) or {}
    if counts is not _MISSING and isinstance(counts, dict):
        by_key = {s.get("seg_key"): s for s in data.get("segments") or []}
        for key, raw in counts.items():
            seg = by_key.get(str(key)) or by_key.get(key)
            if seg is None:
                continue
            seg["count"] = _parse_city_total(raw)
    data = _preserve_session_flags(session, data)
    return save_progress(db, session, data=data, add_time_sec=add_time_sec)


def intercity_unfilled(data: dict | None) -> list[str]:
    """seg_key відрізків без count. Порожній список — усе заповнено."""
    segs = (data or {}).get("segments") or []
    return [s.get("seg_key") or "" for s in segs if s.get("count") is None]


def intercity_counts_exceed_max(counts: dict | None, max_people: int) -> bool:
    if not counts or max_people < 0:
        return False
    for raw in counts.values():
        n = _parse_city_total(raw)
        if n is not None and n > max_people:
            return True
    return False


def save_final_stop_fields(
    db: Session,
    session: CountingSession,
    *,
    counts=_MISSING,
    add_time_sec: int = 0,
) -> CountingSession:
    """Автосейв / фінальне збереження final_stop: entered/exited по seg_key."""
    data = load_data(session) or {}
    if counts is not _MISSING and isinstance(counts, dict):
        by_key = {s.get("seg_key"): s for s in data.get("segments") or []}
        for key, raw in counts.items():
            seg = by_key.get(str(key)) or by_key.get(key)
            if seg is None or not isinstance(raw, dict):
                continue
            seg["entered"] = _parse_city_total(raw.get("entered"))
            seg["exited"] = _parse_city_total(raw.get("exited"))
    data = _preserve_session_flags(session, data)
    return save_progress(db, session, data=data, add_time_sec=add_time_sec)


def final_stop_unfilled(data: dict | None) -> list[str]:
    segs = (data or {}).get("segments") or []
    out: list[str] = []
    for s in segs:
        if s.get("entered") is None or s.get("exited") is None:
            out.append(s.get("seg_key") or "")
    return out


def final_stop_filled_count(data: dict | None) -> int:
    segs = (data or {}).get("segments") or []
    return sum(
        1 for s in segs
        if s.get("entered") is not None and s.get("exited") is not None
    )


def save_progress(
    db: Session,
    session: CountingSession,
    *,
    data: dict | None = None,
    add_time_sec: int = 0,
) -> CountingSession:
    if data is not None:
        data = _preserve_session_flags(session, data)
        session.data = _dump(data)
    if add_time_sec:
        session.time_spent_sec = int(session.time_spent_sec or 0) + int(add_time_sec)
    db.commit()
    db.refresh(session)
    return session


def set_status(db: Session, session: CountingSession, status: str) -> CountingSession:
    if status not in ("in_progress", "ready", "skipped"):
        raise ValueError(f"invalid status: {status!r}")
    session.status = status
    db.commit()
    db.refresh(session)
    return session


def _merge_counts(old: dict | None, new: dict) -> dict:
    """Переносит введённые count из старой data в новую (умное скидання)."""
    if not old:
        return new

    if old.get("meta"):
        new.setdefault("meta", {}).update(old["meta"])

    if old.get("counter_choice"):
        new["counter_choice"] = True

    if new.get("kind") == "city":
        old_val = (old.get("result") or {}).get("in_salon_total")
        new.setdefault("result", {})["in_salon_total"] = old_val
        return new

    if new.get("kind") == FINAL_STOP_KIND:
        old_by_key = {s.get("seg_key"): s for s in old.get("segments", [])}
        for seg in new.get("segments", []):
            prev = old_by_key.get(seg.get("seg_key"))
            if not prev:
                continue
            if (
                abs(int(prev.get("period_start_utc", 0)) - seg["period_start_utc"]) <= _SEG_TIME_TOL
                and abs(int(prev.get("period_end_utc", 0)) - seg["period_end_utc"]) <= _SEG_TIME_TOL
            ):
                if prev.get("entered") is not None:
                    seg["entered"] = prev["entered"]
                if prev.get("exited") is not None:
                    seg["exited"] = prev["exited"]
        return new

    old_by_key = {s.get("seg_key"): s for s in old.get("segments", [])}
    for seg in new.get("segments", []):
        prev = old_by_key.get(seg.get("seg_key"))
        if not prev or prev.get("count") is None:
            continue
        if (
            abs(int(prev.get("period_start_utc", 0)) - seg["period_start_utc"]) <= _SEG_TIME_TOL
            and abs(int(prev.get("period_end_utc", 0)) - seg["period_end_utc"]) <= _SEG_TIME_TOL
        ):
            seg["count"] = prev["count"]
    return new


def reset_session(
    db: Session,
    gps_db: Session,
    routes_db: Session,
    session: CountingSession,
    car,
    route_kind: str | None = None,
) -> CountingSession:
    """Пересобирает data, сохраняя уже введённые count на неизменённых отрезках.

    route_kind (для авто з route_kind_chose) — новий тип підрахунку. Якщо він
    відрізняється від поточного, вже введені числа не переносяться (структура data
    інша: місто ↔ міжмісто).
    """
    old = load_data(session)
    if session.kind == FINAL_STOP_KIND:
        prev_kind = FINAL_STOP_KIND
        build_kind = FINAL_STOP_KIND
    else:
        prev_kind = (
            session.kind if session.kind in WHOLE_DAY_KINDS
            else _default_kind_for_car(car)
        )
        build_kind = _resolve_kind(car, route_kind) if route_kind is not None else prev_kind
    kind_changed = build_kind != prev_kind

    new = counting_build_service.build_data(
        db, gps_db, routes_db, car, session.count_day, kind=build_kind
    )
    # Переносимо введені числа лише коли тип не змінився — інакше структура несумісна.
    merged = new if kind_changed else _merge_counts(old, new)
    merged = _preserve_counter_choice(session, merged)
    if build_kind != FINAL_STOP_KIND:
        merged = _apply_route_kind_choice(merged, car, build_kind)
    prev = (old or {}).get("meta", {}).get("rebuild_count", 0)
    merged.setdefault("meta", {})["rebuild_count"] = prev + 1
    session.data = _dump(merged)
    session.kind = build_kind
    session.status = "in_progress"
    db.commit()
    db.refresh(session)
    return session
