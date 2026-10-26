"""Сборка структуры подсчёта (counting_sessions.data) для города и межгорода.

В data храним только «продукт» оператора и минимум меты. Списки видео/фото
и камеры — на лету из настроек машины:
    GET /panel/count/{car}/media  ->  fetch_city_media(...)

Город    — окно дня + result (одно число).
Межгород — трек-мета + периоды маршрутов + отрезки между остановками.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.services.car_service import camera_ids, parse_schedule_to_local
from app.services import final_stop_service, media_service, route_match_service, track_service
from app.services import gap_fill_service
from app.utils.debug_log import debug_log
from app.utils.local_tz import combine_local, parse_hhmm, unix_to_local

_SPEED_MAX_POINTS = 800


def _window_unix(car, day: date) -> tuple[int, int]:
    """Окно рабочего дня в unix (UTC): местные часы графика на календарный day (DST)."""
    start_s, end_s = parse_schedule_to_local(car.schedule)
    start_dt = combine_local(day, parse_hhmm(start_s))
    end_dt = combine_local(day, parse_hhmm(end_s))
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _local_days(start_unix: int, end_unix: int) -> list[date]:
    """Локальные даты, которые перекрывает окно — для запроса медиа."""
    s = unix_to_local(start_unix)
    e = unix_to_local(end_unix)
    days = []
    d = s.date()
    while d <= e.date():
        days.append(d)
        d += timedelta(days=1)
    return days


def _fetch_media(
    cameras: list[str], days: list[date], source: str = "photoAndVideo"
) -> tuple[dict, dict, list[str]]:
    """Видео/фото по каждой камере за нужные дни. (videos_by_cam, photos_by_cam, warnings).

    По `source` (count_source) тянем только нужный тип, не дёргая внешнее API зря:
      photo         — только фото; video — только видео; photoAndVideo — оба.
    """
    want_video = source != "photo"
    want_photo = source != "video"
    videos_by_cam: dict[str, list[dict]] = {}
    photos_by_cam: dict[str, list[dict]] = {}
    warnings: list[str] = []
    for cam in cameras:
        vids: list[dict] = []
        phts: list[dict] = []
        for day in days:
            if want_video:
                v, verr = media_service.fetch_day_videos(cam, day)
                vids.extend(v)
                if verr:
                    warnings.append(f"{cam}: {verr}")
            if want_photo:
                p, perr = media_service.fetch_day_photos(cam, day)
                phts.extend(p)
                if perr:
                    warnings.append(f"{cam}: {perr}")
        vids.sort(key=lambda x: x["chunk_start_utc"])
        phts.sort(key=lambda x: x["utc"])
        videos_by_cam[cam] = vids
        photos_by_cam[cam] = phts
    return videos_by_cam, photos_by_cam, warnings


def _pick_multi_camera(
    videos_by_cam: dict, photos_by_cam: dict, start: int, stop: int, source: str
) -> tuple[list[dict], str]:
    """Медиа под окно по всем камерам. Статус = лучший среди камер."""
    media: list[dict] = []
    statuses: list[str] = []
    for cam in videos_by_cam:
        items, status = media_service.pick_for_window(
            videos_by_cam.get(cam, []), photos_by_cam.get(cam, []), start, stop, source
        )
        media.extend(items)
        statuses.append(status)
    if "ok" in statuses:
        overall = "ok"
    elif "partial" in statuses:
        overall = "partial"
    else:
        overall = "no_media"
    return media, overall


# ── скорость (для графика) ──────────────────────────────────────────

def _day_speed_series(
    gps_db: Session, imei: str, start_unix: int, end_unix: int
) -> list[dict]:
    """Прорежённый ряд скорости [{t, v}] за окно дня для графика (город и межгород)."""
    track, _src = track_service.get_day_track(gps_db, imei, start_unix, end_unix)
    times = track.get("time", [])
    speeds = track.get("speed", [])
    n = len(times)
    if n == 0:
        return []
    step = max(1, n // _SPEED_MAX_POINTS)
    out: list[dict] = []
    for i in range(0, n, step):
        try:
            out.append({"t": int(times[i]), "v": round(float(speeds[i]), 1)})
        except (TypeError, ValueError):
            continue
    return out


# ── город ───────────────────────────────────────────────────────────

def _build_city(car, day: date, gps_db: Session) -> dict:
    """Облегчённая data города: окно дня + result. Медиа/камеры — на лету."""
    start_unix, end_unix = _window_unix(car, day)
    track, source_table = track_service.get_day_track(
        gps_db, car.imei, start_unix, end_unix
    )
    distance_km = track_service.track_distance_km(track)
    return {
        "kind": "city",
        "source_used": car.count_source,
        "window": {"start_utc": start_unix, "end_utc": end_unix},
        "distance_km": distance_km,
        "track": {
            "points": len(track.get("lat", [])),
            "distance_km": distance_km,
            "source": f"gps.{source_table}" if source_table else None,
        },
        "result": {"in_salon_total": None},
        "meta": {"rebuild_count": 0},
        "warnings": [],
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
    }


def fetch_city_media(car, day: date, gps_db: Session) -> dict:
    """Медиа за день по всем камерам + ряд скорости. Не хранится в data (live)."""
    cameras_cfg = list(car.cameras or [])
    cam_ids = camera_ids(cameras_cfg)
    start_unix, end_unix = _window_unix(car, day)
    days = _local_days(start_unix, end_unix)
    videos_by_cam, photos_by_cam, warnings = _fetch_media(
        cam_ids, days, car.count_source
    )
    day_media = {
        cam: {
            "videos": videos_by_cam.get(cam, []),
            "photos": photos_by_cam.get(cam, []),
        }
        for cam in cam_ids
    }
    has_media = any(
        day_media[c]["videos"] or day_media[c]["photos"] for c in cam_ids
    )
    return {
        "kind": "city",
        "source_used": car.count_source,
        "cameras": cameras_cfg,
        "window": {"start_utc": start_unix, "end_utc": end_unix},
        "day_media": day_media,
        "media_status": "ok" if has_media else "no_media",
        "speed": _day_speed_series(gps_db, car.imei, start_unix, end_unix),
        "warnings": warnings,
    }


# ── межгород ────────────────────────────────────────────────────────

def _build_intercity(
    car, day: date, gps_db: Session, routes_db: Session
) -> dict:
    warnings: list[str] = []
    start_unix, end_unix = _window_unix(car, day)

    track, source_table = track_service.get_day_track(
        gps_db, car.imei, start_unix, end_unix
    )
    if source_table is None:
        warnings.append(f"IMEI {car.imei} не знайдено в gps.trackers")

    routes = route_match_service.load_car_routes(routes_db, car.imei)
    if not routes:
        warnings.append("Немає прив'язаних маршрутів (all_car_routes)")

    raw_points = len(track.get("lat", []))
    debug_log(
        "build_intercity: input",
        tag="intercity",
        data={
            "car_id": car.id,
            "imei": car.imei,
            "day": day.isoformat(),
            "window_utc": [start_unix, end_unix],
            "source_table": source_table,
            "track_points_raw": raw_points,
            "routes": len(routes),
            "auto_gap_filling": bool(car.auto_gap_filling),
        },
    )

    gap_filled = False
    gap_diag: list[dict] = []
    if car.auto_gap_filling and routes and len(track.get("lat", [])) >= 2:
        track, gap_diag = gap_fill_service.fill_track(track, routes)
        gap_filled = True
    debug_log(
        "build_intercity: after gap-fill",
        tag="intercity",
        data={
            "gap_filled": gap_filled,
            "track_points_after": len(track.get("lat", [])),
            "gap_diag": gap_diag,
        },
    )

    periods = route_match_service.detect_route_periods(track, routes) if routes else []
    segments_raw = route_match_service.build_segments(track, routes, periods)
    debug_log(
        "build_intercity: result",
        tag="intercity",
        data={
            "periods": len(periods),
            "segments": len(segments_raw),
            "warnings": warnings,
        },
    )

    # Медиа отрезков НЕ храним в data — фронт грузит день целиком
    # (GET /panel/count/{car}/media) и сужает плеер до period_*_utc.
    distance_km = track_service.track_distance_km(track)
    segments = [
        {
            **seg,
            "count": None,
            "distance_km": track_service.track_distance_km(
                track, seg["period_start_utc"], seg["period_end_utc"]
            ),
            "gap_generated": track_service.segment_gap_generated(
                track, seg["period_start_utc"], seg["period_end_utc"]
            ),
        }
        for seg in segments_raw
    ]

    route_periods = [
        {
            "route_id": p["route_id"],
            "route_name": p["route_name"],
            "start_utc": p["start_utc"],
            "end_utc": p["end_utc"],
            "occurrence": p["occurrence"],
        }
        for p in periods
    ]

    return {
        "kind": "intercity",
        "source_used": car.count_source,
        "window": {"start_utc": start_unix, "end_utc": end_unix},
        "distance_km": distance_km,
        "track": {
            "points": len(track.get("lat", [])),
            "gap_filled": gap_filled,
            "distance_km": distance_km,
            "source": f"gps.{source_table}" if source_table else None,
        },
        "route_periods": route_periods,
        "segments": segments,
        "meta": {"rebuild_count": 0},
        "warnings": warnings,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
    }


def _build_final_stop(
    car, day: date, gps_db: Session, routes_db: Session
) -> dict:
    warnings: list[str] = []
    start_unix, end_unix = _window_unix(car, day)

    track, source_table = track_service.get_day_track(
        gps_db, car.imei, start_unix, end_unix
    )
    if source_table is None:
        warnings.append(f"IMEI {car.imei} не знайдено в gps.trackers")

    zones = final_stop_service.load_final_stop_zones(routes_db, car.imei)
    if not zones:
        warnings.append("Немає прив'язаних зон final_stop (all_car_routes.zones_id)")

    segments_raw = (
        final_stop_service.build_zone_segments(track, zones) if zones else []
    )
    distance_km = track_service.track_distance_km(track)
    segments = [
        {
            **seg,
            "entered": None,
            "exited": None,
            "distance_km": track_service.track_distance_km(
                track, seg["period_start_utc"], seg["period_end_utc"]
            ),
        }
        for seg in segments_raw
    ]

    return {
        "kind": "final_stop",
        "source_used": car.count_source,
        "window": {"start_utc": start_unix, "end_utc": end_unix},
        "distance_km": distance_km,
        "track": {
            "points": len(track.get("lat", [])),
            "distance_km": distance_km,
            "source": f"gps.{source_table}" if source_table else None,
        },
        "zones": [{"id": z["id"], "name": z["name"]} for z in zones],
        "segments": segments,
        "meta": {"rebuild_count": 0},
        "warnings": warnings,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
    }


def build_data(
    db: Session,
    gps_db: Session,
    routes_db: Session,
    car,
    day: date,
    *,
    kind: str | None = None,
) -> dict:
    """Собирает data для сессии подсчёта в зависимости от типа маршрута машины."""
    if kind == "final_stop":
        return _build_final_stop(car, day, gps_db, routes_db)
    if kind not in ("city", "intercity"):
        kind = "intercity" if "intercity" in (car.route_kind or "") else "city"
    if kind == "intercity":
        return _build_intercity(car, day, gps_db, routes_db)
    return _build_city(car, day, gps_db)
