from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import require_full_manager, require_manager
from app.services import schedule_service as svc
from app.templating import render

router = APIRouter(prefix="/admin/schedules")


# ── HTML ────────────────────────────────────────────────────────────

@router.get("/")
def schedule_page(
    request: Request,
    type: str = "whole_day",
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    active_type = svc.normalize_type(type)
    data = svc.list_counters_with_schedules(db, active_type)
    cars = svc.list_available_cars(db)
    return render(
        request,
        "admin/schedules/list.html",
        current_user=user,
        active_tab="schedules",
        active_type=active_type,
        schedule_types=svc.SCHEDULE_TYPES,
        counters_data=data,
        cars=cars,
        schedule_data=svc.build_schedule_data(data),
    )


# ── JSON API ────────────────────────────────────────────────────────

class ScheduleBody(BaseModel):
    counter_id: int
    car_id: int
    type: str | None = "whole_day"
    recurring: dict | None = None
    single_days: list[str] | None = None
    exception_days: list[str] | None = None
    comment: str | None = None


class ScheduleUpdateBody(BaseModel):
    recurring: dict | None = None
    single_days: list[str] | None = None
    exception_days: list[str] | None = None
    comment: str | None = None


class ComputeDatesBody(BaseModel):
    recurring: dict | None = None
    single_days: list[str] | None = None
    exception_days: list[str] | None = None
    year: int
    month: int


@router.get("/api/counter/{counter_id}")
def api_counter_schedules(
    counter_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    schedules = svc.get_schedules_for_counter(db, counter_id)
    return JSONResponse([svc.schedule_to_dict(s) for s in schedules])


@router.get("/api/{schedule_id}")
def api_get_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    schedule = svc.get_schedule(db, schedule_id)
    if schedule is None:
        return JSONResponse({"error": "Не знайдено"}, status_code=404)
    return JSONResponse(svc.schedule_to_dict(schedule))


@router.post("/api/create")
def api_create(
    body: ScheduleBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    try:
        s = svc.create_schedule(
            db,
            counter_id=body.counter_id,
            car_id=body.car_id,
            recurring=body.recurring,
            single_days=body.single_days,
            exception_days=body.exception_days,
            comment=body.comment,
            count_type=svc.normalize_type(body.type),
        )
    except svc.ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(svc.schedule_to_dict(s), status_code=201)


@router.post("/api/{schedule_id}/update")
def api_update(
    schedule_id: int,
    body: ScheduleUpdateBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    schedule = svc.get_schedule(db, schedule_id)
    if schedule is None:
        return JSONResponse({"error": "Не знайдено"}, status_code=404)
    try:
        s = svc.update_schedule(
            db,
            schedule,
            recurring=body.recurring,
            single_days=body.single_days,
            exception_days=body.exception_days,
            comment=body.comment,
        )
    except svc.ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(svc.schedule_to_dict(s))


@router.post("/api/{schedule_id}/delete")
def api_delete(
    schedule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    schedule = svc.get_schedule(db, schedule_id)
    if schedule is None:
        return JSONResponse({"error": "Не знайдено"}, status_code=404)
    svc.delete_schedule(db, schedule)
    return JSONResponse({"ok": True})


@router.post("/api/compute-dates")
def api_compute_dates(
    body: ComputeDatesBody,
    user: User = Depends(require_manager),
):
    dates = svc.compute_dates(
        recurring=body.recurring,
        single_days=body.single_days,
        exception_days=body.exception_days,
        year=body.year,
        month=body.month,
    )
    return JSONResponse(dates)
