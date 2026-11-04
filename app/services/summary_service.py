"""Материализация завершённой сессии (ready / skipped) в агрегат counting_daily_summary.

Тяжёлый разбор data (FIFO пассажиров, километраж) делается здесь — один раз
при завершении сессии, а не на каждом запросе внешнего API. Выручка НЕ
хранится: считается на чтении из trips_json по актуальному тарифу.

Каноническая строка на (car_id, count_day, count_type): при нескольких сессиях
разных рахувальников берём ready с максимальным updated_at, а если ready нет —
последнюю skipped. Skipped-строка — вердикт «день пропущено» (+ comment): чисел
в ней нет (0 / NULL), в gps.passengers, API выручки и письма она не попадает.
Если нет ни ready, ни skipped — строка агрегата удаляется.

Точки вызова (хуки): set_status(ready), финальные автосейвы города/межгорода,
reset/rebuild/skip/delete — всё сводится к materialize_day(db, car_id, day).
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database import GpsSession
from app.models import Car, CountingDailySummary, CountingSession
from app.services import gps_passengers_service as gps_pass
from app.services import report_delivery_service
from app.services import revenue_service as rev
from app.services.counting_session_service import COUNT_TYPE_FULL_DAY, comment_from_data


def _parse_data(session: CountingSession) -> dict | None:
    if not session.data:
        return None
    try:
        return json.loads(session.data)
    except (json.JSONDecodeError, TypeError):
        return None


def _best_session(
    db: Session,
    car_id: int,
    day: date,
    *,
    count_type: str = COUNT_TYPE_FULL_DAY,
) -> CountingSession | None:
    """Каноническая сессия дня: последняя ready, иначе последняя skipped."""
    return db.execute(
        select(CountingSession)
        .where(
            CountingSession.car_id == car_id,
            CountingSession.count_day == day,
            CountingSession.status.in_(("ready", "skipped")),
            CountingSession.count_type == count_type,
        )
        .order_by(
            (CountingSession.status == "ready").desc(),
            CountingSession.updated_at.desc(),
        )
        .limit(1)
    ).scalars().first()


def _final_stop_totals(data: dict) -> tuple[float, float, float]:
    enters = 0.0
    outs = 0.0
    for seg in data.get("segments") or []:
        try:
            if seg.get("entered") is not None:
                enters += float(seg["entered"])
        except (TypeError, ValueError):
            pass
        try:
            if seg.get("exited") is not None:
                outs += float(seg["exited"])
        except (TypeError, ValueError):
            pass
    return enters, outs, enters + outs


def _get_summary(
    db: Session, car_id: int, day: date, *, count_type: str
) -> CountingDailySummary | None:
    return db.execute(
        select(CountingDailySummary).where(
            CountingDailySummary.car_id == car_id,
            CountingDailySummary.count_day == day,
            CountingDailySummary.count_type == count_type,
        )
    ).scalar_one_or_none()


def _distance_km(data: dict) -> float:
    try:
        return round(float(data.get("distance_km") or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _summary_fields(session: CountingSession, data: dict, imei: str) -> dict:
    """Готовит поля агрегата из data сессии (без выручки).

    Skipped: числа не считаются — частично введённые до пропуска данные
    не являются результатом подсчёта.
    """
    kind = data.get("kind") or session.kind
    count_type = session.count_type or COUNT_TYPE_FULL_DAY
    if session.status != "ready":
        return {
            "session_id": int(session.id),
            "car_id": int(session.car_id),
            "imei": imei,
            "count_day": session.count_day,
            "kind": kind if kind in ("city", "intercity", "final_stop") else "city",
            "count_type": count_type,
            "status": session.status,
            "distance_km": 0,
            "passengers_total": 0,
            "trips_json": None,
            "comment": comment_from_data(data),
        }
    if kind == "intercity":
        trips, passengers, _warnings = rev.compute_intercity_passengers(data)
        trips_json = json.dumps(trips, ensure_ascii=False)
    elif kind == "final_stop":
        total_enters, total_outs, passengers = _final_stop_totals(data)
        trips_json = json.dumps(
            {"total_enters": total_enters, "total_outs": total_outs},
            ensure_ascii=False,
        )
    else:
        kind = "city"
        passengers = rev.city_passengers(data)
        trips_json = None
    return {
        "session_id": int(session.id),
        "car_id": int(session.car_id),
        "imei": imei,
        "count_day": session.count_day,
        "kind": kind,
        "count_type": count_type,
        "status": session.status,
        "distance_km": _distance_km(data),
        "passengers_total": float(passengers),
        "trips_json": trips_json,
        "comment": comment_from_data(data),
    }


def _sync_gps_city(imei: str, day: date, passengers: float) -> None:
    """Запись в gps.passengers; ошибка не ломает основной поток."""
    if not imei:
        return
    gps_db = GpsSession()
    try:
        gps_pass.sync_city_count(gps_db, imei, day, passengers)
        gps_db.commit()
    except Exception:  # noqa: BLE001
        gps_db.rollback()
    finally:
        gps_db.close()


def _delete_gps_city(imei: str, day: date) -> None:
    if not imei:
        return
    gps_db = GpsSession()
    try:
        gps_pass.delete_city_count(gps_db, imei, day)
        gps_db.commit()
    except Exception:  # noqa: BLE001
        gps_db.rollback()
    finally:
        gps_db.close()


def materialize_day(
    db: Session,
    car_id: int,
    day: date,
    *,
    count_type: str = COUNT_TYPE_FULL_DAY,
) -> CountingDailySummary | None:
    """Пересобирает каноническую строку агрегата для (car_id, day, count_type).

    Нет ни ready, ни skipped сессии для count_type → удаляет строку агрегата
    з тим самим count_type.
    """
    session = _best_session(db, car_id, day, count_type=count_type)
    existing = _get_summary(db, car_id, day, count_type=count_type)

    if session is None:
        if existing is not None:
            if existing.kind == "city" and existing.imei:
                _delete_gps_city(existing.imei, day)
            db.delete(existing)
            db.commit()
        return None

    data = _parse_data(session)
    if data is None and session.status == "skipped":
        # Пропуск без жодних даних (data = NULL) — вердикт однаково фіксуємо.
        data = {}
    if data is None:
        if existing is not None:
            if existing.kind == "city" and existing.imei:
                _delete_gps_city(existing.imei, day)
            db.delete(existing)
            db.commit()
        return None

    imei = ""
    car = db.get(Car, int(car_id))
    if car is not None and car.imei:
        imei = str(car.imei).strip()

    fields = _summary_fields(session, data, imei)
    was_city = (
        existing is not None
        and existing.status == "ready"
        and existing.kind == "city"
        and existing.imei
    )
    if existing is None:
        existing = CountingDailySummary(**fields)
        db.add(existing)
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
    db.commit()
    db.refresh(existing)
    if fields["status"] == "ready" and fields["kind"] == "city" and imei:
        _sync_gps_city(imei, day, float(fields["passengers_total"]))
    elif was_city:
        _delete_gps_city(existing.imei, day)
    return existing


def safe_sync_comment(db: Session, session: CountingSession) -> None:
    """Оновлює лише comment в агрегаті цієї сесії — без повної матеріалізації.

    Числа не змінились, тож перерахунок і черга звітів тут не потрібні.
    Агрегат є лише для ready-сесій; для інших рядків просто нічого не оновиться.
    """
    try:
        db.execute(
            update(CountingDailySummary)
            .where(CountingDailySummary.session_id == int(session.id))
            .values(
                comment=comment_from_data(_parse_data(session)),
                # computed_at = собі: нотатка не є перерахунком даних.
                computed_at=CountingDailySummary.computed_at,
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def safe_materialize_day(
    db: Session,
    car_id: int,
    day: date,
    *,
    count_type: str = COUNT_TYPE_FULL_DAY,
) -> None:
    """Обёртка для хуков: не даём сбою материализации сломать основной поток.

    После успешной материализации синхронизирует очередь клиентских отчётов.
    """
    try:
        summary = materialize_day(db, car_id, day, count_type=count_type)
    except Exception:  # noqa: BLE001
        db.rollback()
        return
    report_delivery_service.safe_schedule_for_day(
        db, car_id, day, count_type=count_type, summary=summary,
    )
