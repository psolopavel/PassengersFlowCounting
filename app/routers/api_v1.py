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
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Car, CountingDailySummary, CountingSession
from app.security_api import require_api_key
from app.services import report_data_service as rds
from app.services import report_delivery_service as delivery
from app.services import report_xlsx_service as xlsx
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
def revenue_summary(payload: RevenueRequest, db: Session = Depends(get_db)):
    imeis, from_day, to_day = _request_range(payload)
    days, missing_imeis = _revenue_days(db, imeis, from_day, to_day)
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
    """-> (дни из агрегата с выручкой межгорода, imei без ready-дней)."""
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
    return days, missing_imeis


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
