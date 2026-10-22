"""Публичное API выручки/детализации (внешний сайт).

Аутентификация — статический ключ (X-API-Key, см. security_api). Два эндпоинта:

  * POST /api/v1/revenue            — сводка по дням (быстрая, из агрегата);
  * GET  /api/v1/session/{imei}/{day} — детализация дня (полный data + тарифы).

Type 1 читает только counting_daily_summary (без парсинга больших data), деньги
считает на чтении из trips_json по тарифу, актуальному на count_day.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Car, CountingDailySummary, CountingSession
from app.security_api import require_api_key
from app.services import revenue_service as rev

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


# ── Type 1: сводка выручки ───────────────────────────────────────────

class RevenueRequest(BaseModel):
    imeis: list[str] = Field(default_factory=list)
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


@router.post("/revenue")
def revenue_summary(payload: RevenueRequest, db: Session = Depends(get_db)):
    imeis = [str(i).strip() for i in payload.imeis if str(i).strip()]
    if not imeis:
        raise HTTPException(status_code=400, detail="Порожній список imei")

    from_day = _parse_range_date(payload.from_)
    to_day = _parse_range_date(payload.to)
    if from_day > to_day:
        from_day, to_day = to_day, from_day

    rows = db.execute(
        select(CountingDailySummary)
        .where(
            CountingDailySummary.imei.in_(imeis),
            CountingDailySummary.count_day >= from_day,
            CountingDailySummary.count_day <= to_day,
            CountingDailySummary.status == "ready",
        )
        .order_by(CountingDailySummary.imei, CountingDailySummary.count_day)
    ).scalars().all()

    days: list[dict] = []
    seen_imeis: set[str] = set()
    for row in rows:
        seen_imeis.add(row.imei)
        item = {
            "imei": row.imei,
            "day": row.count_day.isoformat(),
            "kind": row.kind,
            "distance_km": float(row.distance_km),
            "passengers": float(row.passengers_total),
            "revenue_uah": None,
            "warnings": [],
        }
        if row.kind == "intercity":
            trips = _parse_trips(row.trips_json)
            revenue, warnings = rev.compute_revenue(
                db, int(row.car_id), trips, row.count_day
            )
            item["revenue_uah"] = revenue
            item["warnings"] = warnings
        days.append(item)

    missing_imeis = [i for i in imeis if i not in seen_imeis]
    return {
        "from": from_day.isoformat(),
        "to": to_day.isoformat(),
        "days": days,
        "missing_imeis": missing_imeis,
    }


def _parse_trips(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


# ── Type 2: детализация дня ──────────────────────────────────────────

@router.get("/session/{imei}/{day}")
def session_detail(imei: str, day: str, db: Session = Depends(get_db)):
    ref_day = _parse_iso_day(day)
    imei = str(imei).strip()

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
