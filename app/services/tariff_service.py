"""Бизнес-логика страницы «Тарифы».

Данные разнесены по двум БД:
  * маршруты и привязка машин к маршрутам — RoutesAndAdvertisement (read-only):
      all_car_routes(imei, routes_id JSON), routes(id, name, route_kind);
  * тарифы и история — новая БД: tariffs(route_id, name, prices),
      car_route_tariffs(car_id, route_id, tariff_id, start_date).

Целостность обеспечивают FK ON DELETE CASCADE (см. app/models/tariff.py и sql/tariff_relations.sql):
удаление машины/тарифа/маршрута каскадно вычищает связанные строки истории.
"""

import json
from datetime import date, datetime

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.orm import Session

from app.models import CarRouteTariff, Tariff

ROUTE_KINDS = ["city", "intercity"]
ROUTE_KIND_LABELS = {"city": "Місто", "intercity": "Міжмісто"}


class ServiceError(Exception):
    pass


# ---------- Старая БД: маршруты ----------

def _parse_json_array(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return []
    return list(data) if isinstance(data, list) else []


def get_car_route_ids(routes_db: Session, imei: str) -> list[int]:
    """ID маршрутов, привязанных к машине (по imei), без дублей, с сохранением порядка."""
    rows = routes_db.execute(
        text("SELECT routes_id FROM all_car_routes WHERE imei = :imei"),
        {"imei": imei},
    ).all()
    ids: list[int] = []
    seen: set[int] = set()
    for (routes_id,) in rows:
        for rid in _parse_json_array(routes_id):
            try:
                rid = int(rid)
            except (ValueError, TypeError):
                continue
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


def get_routes(routes_db: Session, route_ids: list[int]) -> dict[int, dict]:
    """{route_id: {'id', 'name', 'route_kind'}} для переданных id."""
    if not route_ids:
        return {}
    stmt = text(
        "SELECT id, name, route_kind FROM routes WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    rows = routes_db.execute(stmt, {"ids": route_ids}).all()
    return {
        int(r.id): {"id": int(r.id), "name": r.name, "route_kind": r.route_kind}
        for r in rows
    }


# ---------- Новая БД: тарифы ----------

def get_tariffs_for_routes(db: Session, route_ids: list[int]) -> dict[int, list[Tariff]]:
    if not route_ids:
        return {}
    tariffs = list(
        db.execute(
            select(Tariff)
            .where(Tariff.route_id.in_(route_ids))
            .order_by(Tariff.name)
        ).scalars()
    )
    grouped: dict[int, list[Tariff]] = {}
    for t in tariffs:
        grouped.setdefault(int(t.route_id), []).append(t)
    return grouped


# ---------- Новая БД: история (нормализованная таблица) ----------

def load_periods(db: Session, car_id: int) -> dict[int, list[dict]]:
    """{route_id: [{'tariff_id', 'from'}, ...]} по возрастанию даты."""
    rows = list(
        db.execute(
            select(CarRouteTariff)
            .where(CarRouteTariff.car_id == car_id)
            .order_by(CarRouteTariff.route_id, CarRouteTariff.start_date)
        ).scalars()
    )
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        grouped.setdefault(int(r.route_id), []).append(
            {"tariff_id": int(r.tariff_id), "from": r.start_date.isoformat()}
        )
    return grouped


def save_route_history(
    db: Session, car_id: int, route_id: int, periods: list[dict]
) -> None:
    """Полностью заменяет историю периодов для конкретного маршрута машины.

    periods: [{'from': 'YYYY-MM-DD', 'tariff_id': '<id>'}, ...]
    """
    allowed_tariff_ids = {
        int(tid)
        for tid in db.execute(
            select(Tariff.id).where(Tariff.route_id == route_id)
        ).scalars()
    }

    cleaned: list[tuple[date, int]] = []
    seen_dates: set[date] = set()
    for p in periods:
        raw_date = (p.get("from") or "").strip()
        try:
            start = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            raise ServiceError(f"Некоректна дата періоду: {raw_date or '—'}")
        try:
            tariff_id = int(p.get("tariff_id"))
        except (ValueError, TypeError):
            raise ServiceError("Не обрано тариф для одного з періодів")
        if tariff_id not in allowed_tariff_ids:
            raise ServiceError("Обрано тариф, що не належить цьому маршруту")
        if start in seen_dates:
            raise ServiceError(
                f"Дата початку періоду повторюється: {start.isoformat()}"
            )
        seen_dates.add(start)
        cleaned.append((start, tariff_id))

    db.execute(
        delete(CarRouteTariff).where(
            CarRouteTariff.car_id == car_id,
            CarRouteTariff.route_id == route_id,
        )
    )
    for start, tariff_id in cleaned:
        db.add(
            CarRouteTariff(
                car_id=car_id,
                route_id=route_id,
                tariff_id=tariff_id,
                start_date=start,
            )
        )
    db.commit()


# ---------- Сборка данных для страницы ----------

def build_route_view(
    route: dict, tariffs: list[Tariff], periods: list[dict]
) -> dict:
    """Готовит данные одного маршрута для шаблона."""
    tariff_by_id = {int(t.id): t for t in tariffs}
    today = date.today().isoformat()

    enriched = []
    for p in periods:
        t = tariff_by_id.get(p["tariff_id"])
        enriched.append(
            {
                "from": p["from"],
                "tariff_id": p["tariff_id"],
                "tariff_name": t.name if t else f"тариф #{p['tariff_id']} (видалено)",
            }
        )

    current = None
    for p in enriched:
        if p["from"] <= today:
            current = p
    if current is None and enriched:
        current = enriched[0]

    used_ids: list[int] = []
    for p in enriched:
        if p["tariff_id"] not in used_ids:
            used_ids.append(p["tariff_id"])
    used = [
        {"id": tid, "name": tariff_by_id[tid].name}
        for tid in used_ids
        if tid in tariff_by_id
    ]

    return {
        "id": route["id"],
        "name": route["name"] or f"Маршрут #{route['id']}",
        "kind": route["route_kind"],
        "tariffs": [{"id": int(t.id), "name": t.name} for t in tariffs],
        "available_count": len(tariffs),
        "periods": enriched,
        "current": current,
        "used": used,
    }


def build_car_page(db: Session, routes_db: Session, car) -> dict[str, list[dict]]:
    """Возвращает {'city': [route_view, ...], 'intercity': [...]} для маршрутов машины."""
    route_ids = get_car_route_ids(routes_db, car.imei)
    routes = get_routes(routes_db, route_ids)
    tariffs_by_route = get_tariffs_for_routes(db, route_ids)
    periods_by_route = load_periods(db, car.id)

    blocks: dict[str, list[dict]] = {"city": [], "intercity": []}
    for rid in route_ids:
        route = routes.get(rid)
        if route is None:
            continue
        kind = route["route_kind"]
        if kind not in blocks:
            continue
        view = build_route_view(
            route,
            tariffs_by_route.get(rid, []),
            periods_by_route.get(rid, []),
        )
        blocks[kind].append(view)

    for kind in blocks:
        blocks[kind].sort(key=lambda r: r["name"].lower())
    return blocks
