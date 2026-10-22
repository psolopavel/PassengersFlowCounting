"""Визити машини в зонах type='final_stop' (прив'язка через all_car_routes.zones_id).

Кожен неперервний заход у радіус зони — окремий відрізок для підрахунку
(як візит зупинки в міжмісті, але без маршрутів і without gap-fill).
"""

from __future__ import annotations

import json

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.utils.debug_log import debug_log
from app.utils.geo import haversine, parse_point

DEFAULT_ZONE_RADIUS_M = 80
REVISIT_MIN_SEC = 120


def _car_zone_ids(routes_db: Session, imei: str) -> list[int]:
    rows = routes_db.execute(
        text("SELECT zones_id FROM all_car_routes WHERE imei = :imei"),
        {"imei": str(imei).strip()},
    ).all()
    ids: list[int] = []
    seen: set[int] = set()
    for (raw,) in rows:
        try:
            arr = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            arr = []
        for zid in arr if isinstance(arr, list) else []:
            try:
                zid = int(zid)
            except (ValueError, TypeError):
                continue
            if zid not in seen:
                seen.add(zid)
                ids.append(zid)
    return ids


def load_final_stop_zones(routes_db: Session, imei: str) -> list[dict]:
    """Зони final_stop машини з геометрією та радіусом."""
    zone_ids = _car_zone_ids(routes_db, imei)
    if not zone_ids:
        return []
    stmt = text(
        "SELECT id, name, radius_m, ST_AsText(geom) AS wkt FROM zones "
        "WHERE type = 'final_stop' AND active = 1 AND id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    rows = routes_db.execute(stmt, {"ids": zone_ids}).all()
    zones: list[dict] = []
    for r in rows:
        pt = parse_point(r.wkt)
        if pt is None:
            continue
        radius = int(r.radius_m) if r.radius_m and int(r.radius_m) > 0 else DEFAULT_ZONE_RADIUS_M
        zones.append({
            "id": int(r.id),
            "name": r.name or f"Зона #{r.id}",
            "lat": pt[0],
            "lon": pt[1],
            "radius_m": radius,
        })
    debug_log(
        "load_final_stop_zones",
        tag="final_stop",
        data={"imei": str(imei).strip(), "zone_ids": zone_ids, "loaded": len(zones)},
    )
    return zones


def _zone_visits(track: dict, zone: dict) -> list[dict]:
    """Усі візити однієї зони: [{arrival_utc, departure_utc}, ...]."""
    lat, lon, times = track["lat"], track["lon"], track["time"]
    radius = zone.get("radius_m") or DEFAULT_ZONE_RADIUS_M
    zlat, zlon = zone["lat"], zone["lon"]
    n = len(times)
    if n == 0:
        return []

    raw: list[tuple[int, int]] = []
    cluster: list[int] = []
    for i in range(n):
        if haversine(lat[i], lon[i], zlat, zlon) <= radius:
            cluster.append(i)
        elif cluster:
            raw.append((int(times[cluster[0]]), int(times[cluster[-1]])))
            cluster = []
    if cluster:
        raw.append((int(times[cluster[0]]), int(times[cluster[-1]])))

    merged: list[list[int]] = []
    for arr, dep in raw:
        if merged and arr - merged[-1][1] <= REVISIT_MIN_SEC:
            merged[-1][1] = max(merged[-1][1], dep)
        else:
            merged.append([arr, dep])
    return [{"arrival_utc": a, "departure_utc": d} for a, d in merged]


def build_zone_segments(track: dict, zones: list[dict]) -> list[dict]:
    """Відрізки візитів у зонах, відсортовані за часом."""
    segments: list[dict] = []
    occ_by_zone: dict[int, int] = {}
    for zone in zones:
        visits = _zone_visits(track, zone)
        zid = zone["id"]
        for visit in visits:
            occ_by_zone[zid] = occ_by_zone.get(zid, 0) + 1
            occurrence = occ_by_zone[zid]
            segments.append({
                "seg_key": f"zone:{zid}:{occurrence}",
                "zone_id": zid,
                "zone_name": zone["name"],
                "occurrence": occurrence,
                "period_start_utc": visit["arrival_utc"],
                "period_end_utc": visit["departure_utc"],
            })
    segments.sort(key=lambda s: s["period_start_utc"])
    debug_log(
        "build_zone_segments",
        tag="final_stop",
        data={"zones": len(zones), "segments": len(segments)},
    )
    return segments
