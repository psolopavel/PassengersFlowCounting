"""Панель рахувальника: список машин, які треба порахувати за обраний день.

Доступна рахувальникам (бачать себе) і менеджерам (обирають рахувальника
у випадаючому списку для перегляду).
"""

import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.database import get_db, get_gps_db, get_routes_db
from app.models import Car, User
from app.security import require_panel_access
from app.services import car_service as car_svc
from app.services import counting_build_service
from app.services import counting_session_service as css
from app.services import panel_service as svc
from app.services import summary_service
from app.services.counting_session_service import count_type_for_panel
from app.services.schedule_service import normalize_type
from app.templating import flash, render
from app.utils.local_tz import local_today

router = APIRouter(prefix="/panel")


def _parse_day(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _today_local() -> date:
    return local_today()


def _target_counter_id(user: User, counter_id: int | None) -> int:
    """Рахувальник работает за себя; менеджер — за выбранного (counter_id)."""
    if user.is_manager and counter_id:
        return int(counter_id)
    return int(user.id)


def _parse_route_kind(value: str) -> str | None:
    value = (value or "").strip()
    return value if value in ("city", "intercity") else None


def _parse_panel_type(value: str) -> str:
    return normalize_type(value or "whole_day")


def _other_panel_type(panel_type: str) -> str:
    return "final_stops" if panel_type == "whole_day" else "whole_day"


def _panel_type_qs(panel_type: str) -> str:
    return f"&type={panel_type}" if panel_type != "whole_day" else ""


@router.get("/")
def cars_view(
    request: Request,
    day: str = "",
    type: str = "whole_day",
    counter_id: int | None = None,
    db: Session = Depends(get_db),
    gps_db: Session = Depends(get_gps_db),
    user: User = Depends(require_panel_access),
):
    today_local = _today_local()
    ref_day = _parse_day(day) or today_local
    panel_type = _parse_panel_type(type)
    other_type = _other_panel_type(panel_type)
    type_qs = _panel_type_qs(panel_type)

    counters: list[User] = []
    if user.is_manager:
        counters = svc.list_counters(db)
        target_id = counter_id
    else:
        target_id = int(user.id)

    common = {
        "current_user": user,
        "active_page": "cars",
        "is_manager_view": user.is_manager,
        "counters": counters,
        "selected_counter_id": target_id,
        "ref_day": ref_day,
        "prev_day": ref_day - timedelta(days=1),
        "next_day": ref_day + timedelta(days=1),
        "today_local": today_local,
        "is_today": ref_day == today_local,
        "route_kind_labels": svc.ROUTE_KIND_LABELS,
        "panel_type": panel_type,
        "other_panel_type": other_type,
        "panel_mode_label": svc.PANEL_TYPE_LABELS.get(panel_type, ""),
        "other_mode_label": svc.PANEL_TYPE_LABELS.get(other_type, ""),
        "type_qs": type_qs,
    }

    # Менеджер ещё не выбрал рахувальника — показываем только селектор.
    if target_id is None:
        return render(
            request, "panel/cars.html",
            counter=None, view=None, other_pending_count=0,
            pending_days=[], flexible_weeks=[], **common,
        )

    selected = user if not user.is_manager else db.get(User, target_id)
    pending_days = svc.get_pending_days_summary(
        db, counter_id=target_id, schedule_type=panel_type,
    )
    flexible_weeks = svc.get_flexible_weeks_summary(
        db, counter_id=target_id, schedule_type=panel_type,
    )
    view = svc.get_panel_view(
        db, gps_db, counter_id=target_id, ref_day=ref_day,
        schedule_type=panel_type,
    )
    other_view = svc.get_panel_view(
        db, gps_db, counter_id=target_id, ref_day=ref_day,
        schedule_type=other_type,
    )
    return render(
        request, "panel/cars.html",
        counter=selected, view=view, pending_days=pending_days,
        flexible_weeks=flexible_weeks,
        other_pending_count=other_view["pending_count"],
        **common,
    )


def _counter_qs(user: User, counter_id: int | None, target_id: int) -> str:
    """Хвост ?counter_id= для менеджера, который смотрит за рахувальника."""
    return f"&counter_id={target_id}" if (user.is_manager and counter_id) else ""


def _count_qs(
    user: User, counter_id: int | None, target_id: int, panel_type: str
) -> str:
    qs = _counter_qs(user, counter_id, target_id)
    if panel_type != "whole_day":
        qs += f"&type={panel_type}"
    return qs


@router.get("/count/{car_id}")
def count_view(
    car_id: int,
    request: Request,
    day: str = "",
    type: str = "whole_day",
    counter_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Платформа підрахунку: місто / міжмісто / final_stop — повний UI."""
    car = db.get(Car, car_id)
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    count_type = count_type_for_panel(panel_type)

    session = None
    data = None
    if car is not None:
        session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
        data = css.load_data(session) if session else None

    if car is not None and data is not None and data.get("kind") in (
        "city", "intercity", "final_stop"
    ):
        qs = _count_qs(user, counter_id, target_id, panel_type)
        panel_mode_label = svc.PANEL_TYPE_LABELS.get(panel_type, "")
        config = {
            "carId": car.id,
            "imei": car.imei,
            "dayIso": ref_day.isoformat(),
            "counterId": target_id if (user.is_manager and counter_id) else None,
            "panelType": panel_type,
            "cameras": car.cameras or [],
            "source": car.count_source,
            "sourcePriority": getattr(car, "source_priority", None) or "video",
            "window": data.get("window") or {},
            "status": session.status,
            "mediaUrl": f"/panel/count/{car.id}/media?day={ref_day.isoformat()}{qs}",
            "autosaveUrl": f"/panel/count/{car.id}/save",
        }
        if data.get("kind") == "city":
            total = (data.get("result") or {}).get("in_salon_total")
            config["total"] = total
            config["saveUrl"] = f"/panel/count/{car.id}/city-save"
            return render(
                request, "panel/count_city.html",
                current_user=user, car=car, ref_day=ref_day, session=session,
                total=total, counter_id=target_id,
                panel_mode_label=panel_mode_label,
                config_json=json.dumps(config, ensure_ascii=False),
                active_page="count",
            )

        config["segments"] = data.get("segments") or []
        if data.get("kind") == "final_stop":
            config["kind"] = "final_stop"
            config["saveUrl"] = f"/panel/count/{car.id}/final-stop-save"
            filled = css.final_stop_filled_count(data)
            is_final_stop = True
        else:
            config["kind"] = "intercity"
            config["maxPeopleCountInCar"] = car_svc.max_people_for_car(car)
            config["saveUrl"] = f"/panel/count/{car.id}/intercity-save"
            filled = sum(1 for s in config["segments"] if s.get("count") is not None)
            is_final_stop = False
        return render(
            request, "panel/count_intercity.html",
            current_user=user, car=car, ref_day=ref_day, session=session,
            segments=config["segments"], filled=filled,
            counter_id=target_id,
            is_final_stop=is_final_stop,
            panel_mode_label=panel_mode_label,
            config_json=json.dumps(config, ensure_ascii=False),
            active_page="count",
        )

    return render(
        request, "panel/count_placeholder.html",
        current_user=user, car=car, ref_day=ref_day,
        session=session, data=data, counter_id=target_id, active_page="count",
        panel_type=panel_type,
        panel_mode_label=svc.PANEL_TYPE_LABELS.get(panel_type, ""),
        default_route_kind=svc._car_route_kind(car) or "city",
        route_kind_chose=bool(car and getattr(car, "route_kind_chose", False)),
    )


@router.post("/car/{car_id}/comment")
async def car_comment_save(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Збереження коментаря про машину (рахувальник або менеджер у режимі перегляду)."""
    body = await request.json()
    target_id = _target_counter_id(user, body.get("counter_id"))

    car = db.get(Car, car_id)
    if car is None:
        return JSONResponse({"ok": False, "error": "car_not_found"}, status_code=404)
    if not svc.counter_has_car(db, target_id, car_id):
        return JSONResponse({"ok": False, "error": "access_denied"}, status_code=403)

    comment = (body.get("comment") or "").strip()
    car = car_svc.update_car_comment(db, car, comment or None)
    return JSONResponse({"ok": True, "comment": car.comment or ""})


@router.get("/count/{car_id}/media")
def count_media(
    car_id: int,
    day: str = "",
    counter_id: int | None = None,
    db: Session = Depends(get_db),
    gps_db: Session = Depends(get_gps_db),
    user: User = Depends(require_panel_access),
):
    """Медіа (відео/фото) + ряд швидкості за день — тягнеться на льоту, не з data."""
    car = db.get(Car, car_id)
    if car is None:
        return JSONResponse({"error": "car_not_found"}, status_code=404)
    ref_day = _parse_day(day) or _today_local()
    payload = counting_build_service.fetch_city_media(car, ref_day, gps_db)
    return JSONResponse(payload)


@router.post("/count/{car_id}/city-save")
async def count_city_save(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Збереження міського підрахунку: {day, counter_id?, total, add_time_sec?}.

    Пише result.in_salon_total і завершує сесію (status=ready).
    """
    body = await request.json()
    ref_day = _parse_day(body.get("day", "")) or _today_local()
    target_id = _target_counter_id(user, body.get("counter_id"))
    count_type = count_type_for_panel(body.get("panelType") or "whole_day")
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
    if session is None:
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)

    css.save_city_fields(
        db, session,
        total=body.get("total"),
        add_time_sec=int(body.get("add_time_sec") or 0),
    )
    css.set_status(db, session, "ready")
    summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
    return JSONResponse({"ok": True})


@router.post("/count/{car_id}/intercity-save")
async def count_intercity_save(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Фінальне збереження міжміста: {day, counter_id?, counts, add_time_sec?}.

    Пише segments[].count і завершує сесію лише коли всі відрізки заповнені.
    """
    body = await request.json()
    ref_day = _parse_day(body.get("day", "")) or _today_local()
    target_id = _target_counter_id(user, body.get("counter_id"))
    count_type = count_type_for_panel(body.get("panelType") or "whole_day")
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
    if session is None:
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)

    car = db.get(Car, car_id)
    max_people = car_svc.max_people_for_car(car)
    counts = body.get("counts") or {}
    if css.intercity_counts_exceed_max(counts, max_people):
        return JSONResponse(
            {"ok": False, "error": "count_too_high", "max": max_people},
            status_code=400,
        )

    css.save_intercity_fields(
        db, session,
        counts=counts,
        add_time_sec=int(body.get("add_time_sec") or 0),
    )
    data = css.load_data(session)
    missing = css.intercity_unfilled(data)
    if not (data or {}).get("segments"):
        return JSONResponse(
            {"ok": False, "error": "no_segments", "missing": 0},
            status_code=400,
        )
    if missing:
        return JSONResponse(
            {
                "ok": False,
                "error": "incomplete",
                "missing": len(missing),
            },
            status_code=400,
        )
    css.set_status(db, session, "ready")
    summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
    return JSONResponse({"ok": True})


@router.post("/count/{car_id}/final-stop-save")
async def count_final_stop_save(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Фінальне збереження final_stop: {day, counter_id?, counts, add_time_sec?, allow_empty?}.

    allow_empty=true — рахувальник підтвердив пропуск порожніх полів (лишаються null).
    """
    body = await request.json()
    ref_day = _parse_day(body.get("day", "")) or _today_local()
    target_id = _target_counter_id(user, body.get("counter_id"))
    count_type = count_type_for_panel(body.get("panelType") or "final_stops")
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
    if session is None:
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)

    css.save_final_stop_fields(
        db, session,
        counts=body.get("counts") or {},
        add_time_sec=int(body.get("add_time_sec") or 0),
    )
    data = css.load_data(session)
    missing = css.final_stop_unfilled(data)
    if not (data or {}).get("segments"):
        return JSONResponse(
            {"ok": False, "error": "no_segments", "missing": 0},
            status_code=400,
        )
    if missing and body.get("allow_empty") is not True:
        return JSONResponse(
            {"ok": False, "error": "incomplete", "missing": len(missing)},
            status_code=400,
        )
    css.set_status(db, session, "ready")
    summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
    return JSONResponse({"ok": True})


@router.post("/count/{car_id}/start")
def count_start(
    car_id: int,
    request: Request,
    day: str = Form(""),
    type: str = Form("whole_day"),
    counter_id: int | None = Form(None),
    route_kind: str = Form(""),
    db: Session = Depends(get_db),
    gps_db: Session = Depends(get_gps_db),
    routes_db: Session = Depends(get_routes_db),
    user: User = Depends(require_panel_access),
):
    """«Почати підрахунок»: створює сесію і синхронно збирає data."""
    car = db.get(Car, car_id)
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    type_qs = _panel_type_qs(panel_type)

    if car is None:
        flash(request, "Машину не знайдено", "error")
        return RedirectResponse("/panel/", status_code=HTTP_303_SEE_OTHER)

    now_utc = datetime.now(timezone.utc)
    if not svc._is_day_countable(ref_day, car, now_utc):
        flash(request, "Підрахунок за цей день ще недоступний", "error")
        return RedirectResponse(
            f"/panel/?day={ref_day.isoformat()}{type_qs}",
            status_code=HTTP_303_SEE_OTHER,
        )

    flexible = svc.is_flexible_pick(
        db, target_id, car_id, ref_day, schedule_type=panel_type,
    )
    try:
        css.start_or_get(
            db, gps_db, routes_db,
            counter_id=target_id, car=car, day=ref_day, flexible=flexible,
            route_kind=_parse_route_kind(route_kind),
            panel_type=panel_type,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Не вдалося підготувати дані: {exc}", "error")
        return RedirectResponse(
            f"/panel/?day={ref_day.isoformat()}{type_qs}",
            status_code=HTTP_303_SEE_OTHER,
        )

    count_type = count_type_for_panel(panel_type)
    summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
    url = f"/panel/count/{car_id}?day={ref_day.isoformat()}{type_qs}"
    if user.is_manager and counter_id:
        url += f"&counter_id={target_id}"
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


@router.post("/count/{car_id}/skip")
def count_skip(
    car_id: int,
    request: Request,
    day: str = Form(""),
    type: str = Form("whole_day"),
    counter_id: int | None = Form(None),
    route_kind: str = Form(""),
    comment: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """«Пропустити»: створює або оновлює сесію зі status=skipped."""
    car = db.get(Car, car_id)
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    type_qs = _panel_type_qs(panel_type)

    if car is None:
        flash(request, "Машину не знайдено", "error")
    else:
        now_utc = datetime.now(timezone.utc)
        if not svc._is_day_countable(ref_day, car, now_utc):
            flash(request, "Підрахунок за цей день ще недоступний", "error")
        else:
            flexible = svc.is_flexible_pick(
                db, target_id, car_id, ref_day, schedule_type=panel_type,
            )
            css.skip_session(
                db, counter_id=target_id, car_id=car_id, day=ref_day,
                car=car, flexible=flexible,
                route_kind=_parse_route_kind(route_kind),
                comment=comment.strip() or None,
                panel_type=panel_type,
            )
            count_type = count_type_for_panel(panel_type)
            summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
            flash(request, "Підрахунок пропущено", "info")

    url = f"/panel/?day={ref_day.isoformat()}{type_qs}"
    if user.is_manager and counter_id:
        url += f"&counter_id={target_id}"
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


@router.post("/count/{car_id}/skip-comment")
async def count_skip_comment_update(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Оновлення причини пропуску для сесії зі status=skipped."""
    body = await request.json()
    ref_day = _parse_day(body.get("day", "")) or _today_local()
    target_id = _target_counter_id(user, body.get("counter_id"))

    count_type = count_type_for_panel(body.get("panelType") or "whole_day")
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
    if session is None:
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)
    if session.status != "skipped":
        return JSONResponse({"ok": False, "error": "not_skipped"}, status_code=400)

    comment = (body.get("comment") or "").strip()
    try:
        css.update_skip_comment(db, session, comment or None)
    except ValueError:
        return JSONResponse({"ok": False, "error": "not_skipped"}, status_code=400)

    return JSONResponse({"ok": True, "comment": comment})


@router.post("/count/{car_id}/delete")
def count_delete(
    car_id: int,
    request: Request,
    day: str = Form(""),
    type: str = Form("whole_day"),
    counter_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """«Видалити запис»: прибирає сесію дня (для довільного розкладу — щоб обрати інший день)."""
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    type_qs = _panel_type_qs(panel_type)
    count_type = count_type_for_panel(panel_type)
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)

    if session is None:
        flash(request, "Сесію не знайдено", "error")
    else:
        css.delete_session(db, session)
        summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
        flash(request, "Запис видалено", "info")

    url = f"/panel/?day={ref_day.isoformat()}{type_qs}"
    if user.is_manager and counter_id:
        url += f"&counter_id={target_id}"
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


@router.post("/count/{car_id}/rebuild")
def count_rebuild(
    car_id: int,
    request: Request,
    day: str = Form(""),
    type: str = Form("whole_day"),
    counter_id: int | None = Form(None),
    route_kind: str = Form(""),
    db: Session = Depends(get_db),
    gps_db: Session = Depends(get_gps_db),
    routes_db: Session = Depends(get_routes_db),
    user: User = Depends(require_panel_access),
):
    """«Перезібрати» (міжмісто): видаляє сесію і збирає нову з нуля.

    Тип нової сесії — обраний рахувальником (route_kind, якщо авто це дозволяє);
    інакше зберігається поточний тип сесії.
    """
    car = db.get(Car, car_id)
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    type_qs = _panel_type_qs(panel_type)
    count_type = count_type_for_panel(panel_type)
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type) if car else None

    if car is None or session is None:
        flash(request, "Сесію не знайдено", "error")
        return RedirectResponse(
            f"/panel/?day={ref_day.isoformat()}{type_qs}",
            status_code=HTTP_303_SEE_OTHER,
        )

    if panel_type == "final_stops":
        chosen_kind = None
    else:
        current_kind = (
            session.kind if session.kind in ("city", "intercity")
            else svc._car_route_kind(car)
        )
        chose_enabled = bool(getattr(car, "route_kind_chose", False))
        if current_kind != "intercity" and not chose_enabled:
            flash(request, "Перезібрати доступно лише для міжміського транспорту", "error")
            return RedirectResponse(
                f"/panel/?day={ref_day.isoformat()}{type_qs}",
                status_code=HTTP_303_SEE_OTHER,
            )
        chosen_kind = _parse_route_kind(route_kind) or current_kind

    was_flexible = bool((css.load_data(session) or {}).get("counter_choice"))

    css.delete_session(db, session)
    try:
        css.start_or_get(
            db, gps_db, routes_db,
            counter_id=target_id, car=car, day=ref_day, flexible=was_flexible,
            route_kind=chosen_kind,
            panel_type=panel_type,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Не вдалося перезібрати дані: {exc}", "error")
        return RedirectResponse(
            f"/panel/?day={ref_day.isoformat()}{type_qs}",
            status_code=HTTP_303_SEE_OTHER,
        )
    summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
    flash(request, "Сесію перезібрано", "info")

    url = f"/panel/count/{car_id}?day={ref_day.isoformat()}{type_qs}"
    if user.is_manager and counter_id:
        url += f"&counter_id={target_id}"
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


@router.post("/count/{car_id}/reset")
def count_reset(
    car_id: int,
    request: Request,
    day: str = Form(""),
    type: str = Form("whole_day"),
    counter_id: int | None = Form(None),
    route_kind: str = Form(""),
    db: Session = Depends(get_db),
    gps_db: Session = Depends(get_gps_db),
    routes_db: Session = Depends(get_routes_db),
    user: User = Depends(require_panel_access),
):
    """«Скинути підрахунок»: пересбор data зі збереженням введених чисел."""
    car = db.get(Car, car_id)
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    type_qs = _panel_type_qs(panel_type)
    count_type = count_type_for_panel(panel_type)
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type) if car else None

    if car is None or session is None:
        flash(request, "Сесію не знайдено", "error")
    else:
        try:
            css.reset_session(
                db, gps_db, routes_db, session, car,
                route_kind=_parse_route_kind(route_kind),
            )
            summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
            flash(request, "Дані підрахунку перезібрано", "info")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            flash(request, f"Не вдалося перезібрати дані: {exc}", "error")

    url = f"/panel/count/{car_id}?day={ref_day.isoformat()}{type_qs}"
    if user.is_manager and counter_id:
        url += f"&counter_id={target_id}"
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


@router.post("/count/{car_id}/ready")
def count_ready(
    car_id: int,
    request: Request,
    day: str = Form(""),
    type: str = Form("whole_day"),
    counter_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Завершення підрахунку: status -> ready."""
    ref_day = _parse_day(day) or _today_local()
    target_id = _target_counter_id(user, counter_id)
    panel_type = _parse_panel_type(type)
    type_qs = _panel_type_qs(panel_type)
    count_type = count_type_for_panel(panel_type)
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
    if session is None:
        flash(request, "Сесію не знайдено", "error")
    else:
        css.set_status(db, session, "ready")
        summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
        flash(request, "Підрахунок завершено", "success")
    url = f"/panel/?day={ref_day.isoformat()}{type_qs}"
    if user.is_manager and counter_id:
        url += f"&counter_id={target_id}"
    return RedirectResponse(url, status_code=HTTP_303_SEE_OTHER)


@router.post("/count/{car_id}/save")
async def count_save(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_panel_access),
):
    """Автозбереження: JSON {day, counter_id?, total? | counts?, add_time_sec?}."""
    body = await request.json()
    ref_day = _parse_day(body.get("day", "")) or _today_local()
    target_id = _target_counter_id(user, body.get("counter_id"))
    panel_type = _parse_panel_type(body.get("panelType") or "whole_day")
    count_type = count_type_for_panel(panel_type)
    session = css.get_session(db, target_id, car_id, ref_day, count_type=count_type)
    if session is None:
        return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)

    data = css.load_data(session)
    kind = (data or {}).get("kind")
    if kind == "city":
        css.save_city_fields(
            db, session,
            total=body.get("total"),
            add_time_sec=int(body.get("add_time_sec") or 0),
        )
    elif kind == "intercity":
        car = db.get(Car, car_id)
        max_people = car_svc.max_people_for_car(car)
        counts = body.get("counts")
        if isinstance(counts, dict) and css.intercity_counts_exceed_max(counts, max_people):
            return JSONResponse(
                {"ok": False, "error": "count_too_high", "max": max_people},
                status_code=400,
            )
        css.save_intercity_fields(
            db, session,
            counts=counts,
            add_time_sec=int(body.get("add_time_sec") or 0),
        )
    elif kind == "final_stop":
        css.save_final_stop_fields(
            db, session,
            counts=body.get("counts"),
            add_time_sec=int(body.get("add_time_sec") or 0),
        )
    else:
        css.save_progress(
            db, session,
            data=body.get("data"),
            add_time_sec=int(body.get("add_time_sec") or 0),
        )
    # Пересобираем агрегат, лише коли сесія вже завершена (ready) — під час
    # активного підрахунку рядка немає, зайвих запитів не робимо.
    if session.status == "ready":
        summary_service.safe_materialize_day(db, car_id, ref_day, count_type=count_type)
    return JSONResponse({"ok": True})
