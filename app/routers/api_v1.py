"""Публичное API выручки/детализации (внешний сайт).

Аутентификация — статический ключ (X-API-Key, см. security_api). Эндпоинты:

  * POST /api/v1/revenue            — сводка по дням (быстрая, из агрегата);
  * POST /api/v1/revenue/xlsx       — та же сводка файлом xlsx (общий отчёт);
  * GET  /api/v1/session/{imei}/{day} — детализация дня (полный data + тарифы);
  * GET  /api/v1/session/{imei}/{day}/xlsx — xlsx-отчёт дня (как в письме клиенту).

Type 1 читает только counting_daily_summary (без парсинга больших data), деньги
считает на чтении из trips_json по тарифу, актуальному на count_day.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db, get_gps_db
from app.models import Car, CountingDailySummary, CountingSession
from app.security_api import require_api_key
from app.services import report_data_service as rds
from app.services import report_delivery_service as delivery
from app.services import report_xlsx_service as xlsx
from app.services import revenue_service as rev
from app.services import track_service
from app.utils.debug_log import debug_exc
from app.utils.local_tz import local_today

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


# ── разбор входных дат ───────────────────────────────────────────────

def _parse_range_date(value: str) -> date:
    """'DD-MM-YYYY HH:MM' | 'DD-MM-YYYY' -> date (время игнорируем)."""
    token = (value or "").strip().split()[0] if (value or "").strip() else ""
    try:
        return datetime.strptime(token, "%d-%m-%Y").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Невірний формат дати: {value!r} (очікується DD-MM-YYYY HH:MM)",
        ) from exc


def _parse_iso_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Невірний день: {value!r} (очікується YYYY-MM-DD)"
        ) from exc


def _xlsx_response(content: bytes, filename: str) -> Response:
    disposition = f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=content,
        media_type=xlsx.XLSX_MIME,
        headers={"Content-Disposition": disposition},
    )


# ── Type 1: сводка выручки ───────────────────────────────────────────

class RevenueRequest(BaseModel):
    imeis: list[str] = Field(default_factory=list)
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


class RevenueXlsxRequest(RevenueRequest):
    title: str = ""
    # imei -> название машины так, как его видит клиент (иначе — cars.name)
    names: dict[str, str] = Field(default_factory=dict)


def _request_range(payload: RevenueRequest) -> tuple[list[str], date, date]:
    imeis = [str(i).strip() for i in payload.imeis if str(i).strip()]
    if not imeis:
        raise HTTPException(status_code=400, detail="Порожній список imei")

    from_day = _parse_range_date(payload.from_)
    to_day = _parse_range_date(payload.to)
    if from_day > to_day:
        from_day, to_day = to_day, from_day
    return imeis, from_day, to_day


@router.post("/revenue")
def revenue_summary(
    payload: RevenueRequest,
    db: Session = Depends(get_db),
    gps_db: Session = Depends(get_gps_db),
):
    imeis, from_day, to_day = _request_range(payload)
    days, missing_imeis = _revenue_days(db, imeis, from_day, to_day)
    _add_uncounted_km(gps_db, days, imeis, from_day, to_day)
    return {
        "from": from_day.isoformat(),
        "to": to_day.isoformat(),
        "days": days,
        "missing_imeis": missing_imeis,
    }


@router.post("/revenue/xlsx")
def revenue_summary_xlsx(payload: RevenueXlsxRequest, db: Session = Depends(get_db)):
    imeis, from_day, to_day = _request_range(payload)
    days, missing_imeis = _revenue_days(db, imeis, from_day, to_day)

    names = {str(k).strip(): str(v).strip() for k, v in payload.names.items() if str(v).strip()}
    unnamed = [i for i in imeis if i not in names]
    if unnamed:
        for imei, name in db.execute(
            select(Car.imei, Car.name).where(Car.imei.in_(unnamed))
        ).all():
            if (name or "").strip():
                names[imei] = name.strip()

    report = rds.build_overview_report(
        title=payload.title.strip(),
        from_day=from_day,
        to_day=to_day,
        imeis=imeis,
        days=days,
        missing_imeis=missing_imeis,
        names=names,
    )
    return _xlsx_response(xlsx.build_overview_xlsx(report), xlsx.overview_filename(report))


def _revenue_days(
    db: Session, imeis: list[str], from_day: date, to_day: date
) -> tuple[list[dict], list[str]]:
    """-> (дни из агрегата с выручкой межгорода, imei без ready-дней).

    Дни со status=skipped тоже возвращаются (вердикт «пропущено» + comment),
    числа в них 0 и выручка не считается.
    """
    rows = db.execute(
        select(CountingDailySummary)
        .where(
            CountingDailySummary.imei.in_(imeis),
            CountingDailySummary.count_day >= from_day,
            CountingDailySummary.count_day <= to_day,
            CountingDailySummary.status.in_(("ready", "skipped")),
        )
        .order_by(CountingDailySummary.imei, CountingDailySummary.count_day)
    ).scalars().all()

    days: list[dict] = []
    ready_imeis: set[str] = set()
    for row in rows:
        item = {
            "imei": row.imei,
            "day": row.count_day.isoformat(),
            "kind": row.kind,
            "status": row.status,
            "distance_km": float(row.distance_km),
            "passengers": float(row.passengers_total),
            "revenue_uah": None,
            "warnings": [],
            "comment": row.comment,
        }
        if row.status != "ready":
            days.append(item)
            continue
        ready_imeis.add(row.imei)
        if row.kind == "intercity":
            trips = _parse_trips(row.trips_json)
            revenue, warnings = rev.compute_revenue(
                db, int(row.car_id), trips, row.count_day
            )
            item["revenue_uah"] = revenue
            item["warnings"] = warnings
        days.append(item)

    missing_imeis = [i for i in imeis if i not in ready_imeis]
    return days, missing_imeis


def _add_uncounted_km(
    gps_db: Session, days: list[dict], imeis: list[str], from_day: date, to_day: date
) -> None:
    """Км із трекера для днів без підрахунку (як dailyKm у revenue3.php).

    * skipped-рядкам підставляє реальний пробіг замість 0;
    * для днів діапазону без жодного рядка в агрегаті (по сьогодні включно)
      додає рядки status=not_counted — лише км, решта порожня.
    imei без трекера в gps-БД рядків не отримує. Збій gps-БД не ламає відповідь.
    """
    last_day = min(to_day, local_today())
    period: list[date] = []
    d = from_day
    while d <= last_day:
        period.append(d)
        d += timedelta(days=1)

    for imei in dict.fromkeys(imeis):
        own = [item for item in days if item["imei"] == imei]
        covered = {item["day"] for item in own}
        skipped = [item for item in own if item["status"] == "skipped"]
        uncounted = [day for day in period if day.isoformat() not in covered]
        need = sorted(set(uncounted) | {date.fromisoformat(i["day"]) for i in skipped})
        if not need:
            continue
        try:
            km_by_day = track_service.daily_speed_km(gps_db, imei, need)
        except Exception as exc:  # noqa: BLE001
            gps_db.rollback()
            debug_exc(f"daily_speed_km failed imei={imei}", tag="api", exc=exc)
            continue
        if km_by_day is None:
            continue

        for item in skipped:
            item["distance_km"] = km_by_day.get(date.fromisoformat(item["day"]), 0.0)
        for day in uncounted:
            days.append({
                "imei": imei,
                "day": day.isoformat(),
                "kind": None,
                "status": "not_counted",
                "distance_km": km_by_day.get(day, 0.0),
                "passengers": 0.0,
                "revenue_uah": None,
                "warnings": [],
                "comment": None,
            })

    days.sort(key=lambda item: (item["imei"], item["day"]))


def _parse_trips(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


# ── Type 2: детализация дня ──────────────────────────────────────────

def _day_session(db: Session, imei: str, ref_day: date) -> tuple[Car, CountingSession]:
    car = db.execute(
        select(Car).where(Car.imei == imei)
    ).scalar_one_or_none()
    if car is None:
        raise HTTPException(status_code=404, detail="Машину не знайдено")

    # Предпочитаем ready-сессию (последнюю по updated_at), иначе любую с data.
    session = db.execute(
        select(CountingSession)
        .where(
            CountingSession.car_id == car.id,
            CountingSession.count_day == ref_day,
        )
        .order_by(
            (CountingSession.status == "ready").desc(),
            CountingSession.updated_at.desc(),
        )
    ).scalars().first()

    if session is None or not session.data:
        raise HTTPException(status_code=404, detail="Сесію не знайдено")
    return car, session


@router.get("/session/{imei}/{day}")
def session_detail(imei: str, day: str, db: Session = Depends(get_db)):
    ref_day = _parse_iso_day(day)
    imei = str(imei).strip()
    car, session = _day_session(db, imei, ref_day)

    try:
        data = json.loads(session.data)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=500, detail="Пошкоджені дані сесії")

    route_ids = [
        p.get("route_id") for p in (data.get("route_periods") or [])
        if p.get("route_id") is not None
    ]
    tariffs = rev.active_tariffs_for_routes(db, int(car.id), route_ids, ref_day)

    return {
        "imei": imei,
        "day": ref_day.isoformat(),
        "kind": data.get("kind") or session.kind,
        "status": session.status,
        "data": data,
        "tariffs": tariffs,
    }


@router.get("/session/{imei}/{day}/xlsx")
def session_detail_xlsx(imei: str, day: str, db: Session = Depends(get_db)):
    ref_day = _parse_iso_day(day)
    car, session = _day_session(db, str(imei).strip(), ref_day)
    try:
        content, filename = delivery.build_download(db, car, session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _xlsx_response(content, filename)
