from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.database import get_db, get_joomla_db
from app.models import User
from app.security import require_full_manager, require_manager
from app.services import car_service as svc
from app.services import joomla_service
from app.templating import flash, render
from app.utils import clamp_rating, parse_optional_int

_DEFAULT_SCHEDULE_START = "06:00"
_DEFAULT_SCHEDULE_END = "22:00"

router = APIRouter(prefix="/admin/cars")

_META = {
    "count_sources": svc.COUNT_SOURCES,
    "source_priorities": svc.SOURCE_PRIORITIES,
    "route_kinds": svc.ROUTE_KINDS,
    "camera_orientations": svc.CAMERA_ORIENTATIONS,
    "count_source_labels": svc.COUNT_SOURCE_LABELS,
    "source_priority_labels": svc.SOURCE_PRIORITY_LABELS,
    "route_kind_labels": svc.ROUTE_KIND_LABELS,
    "camera_orientation_labels": svc.CAMERA_ORIENTATION_LABELS,
}


def _resolve_owner(
    joomla_db: Session, owner_user_id: str
) -> tuple[int | None, str, str | None]:
    """Проверяет выбранный аккаунт Joomla.

    Возвращает (id, отображаемое_имя, текст_ошибки).
    company_name автозаполняется именем аккаунта.
    """
    owner_id = parse_optional_int(owner_user_id)
    if owner_id is None:
        return None, "", "Оберіть обліковий запис-власника машини"
    owner = joomla_service.get_owner(joomla_db, owner_id)
    if owner is None:
        return None, "", "Обраний обліковий запис не знайдено в базі Joomla"
    return owner.id, owner.display_name, None


@router.get("/")
def list_view(
    request: Request,
    db: Session = Depends(get_db),
    joomla_db: Session = Depends(get_joomla_db),
    user: User = Depends(require_manager),
):
    cars = svc.list_cars(db)
    owners = joomla_service.owners_by_id(
        joomla_db, {c.owner_user_id for c in cars if c.owner_user_id}
    )
    cars.sort(
        key=lambda c: (
            (owners[c.owner_user_id].display_name if c.owner_user_id in owners else c.company_name or "").lower(),
            (c.name or c.imei or "").lower(),
        )
    )
    owner_accounts: list[dict] = []
    seen_owners: set[int] = set()
    for c in cars:
        if not c.owner_user_id or c.owner_user_id in seen_owners:
            continue
        owner = owners.get(c.owner_user_id)
        if owner is None:
            continue
        seen_owners.add(c.owner_user_id)
        owner_accounts.append({"id": owner.id, "name": owner.display_name})
    owner_accounts.sort(key=lambda o: o["name"].lower())
    return render(
        request, "admin/cars/list.html",
        current_user=user, active_tab="cars", cars=cars, owners=owners,
        owner_accounts=owner_accounts, **_META,
    )


@router.get("/new")
def new_form(
    request: Request,
    joomla_db: Session = Depends(get_joomla_db),
    user: User = Depends(require_full_manager),
):
    return render(
        request, "admin/cars/form.html",
        current_user=user, active_tab="cars",
        car=None, cameras=[], mode="create",
        owners=joomla_service.list_owners(joomla_db),
        schedule_start=_DEFAULT_SCHEDULE_START,
        schedule_end=_DEFAULT_SCHEDULE_END,
        **_META,
    )


@router.post("/new")
def create(
    request: Request,
    name: str = Form(...),
    imei: str = Form(...),
    camera_id: list[str] = Form([]),
    camera_orientation: list[str] = Form([]),
    owner_user_id: str = Form(""),
    comment: str = Form(""),
    is_active: bool = Form(False),
    difficulty_rating: str = Form(""),
    count_source: str = Form("photo"),
    source_priority: str = Form("video"),
    non_shedule_count: bool = Form(False),
    alarms_only: bool = Form(False),
    auto_gap_filling: bool = Form(False),
    route_kind: list[str] = Form([]),
    route_kind_chose: bool = Form(False),
    schedule_start: str = Form(_DEFAULT_SCHEDULE_START),
    schedule_end: str = Form(_DEFAULT_SCHEDULE_END),
    max_people_count_in_car: str = Form("100"),
    db: Session = Depends(get_db),
    joomla_db: Session = Depends(get_joomla_db),
    user: User = Depends(require_full_manager),
):
    owner_id, owner_name, owner_err = _resolve_owner(joomla_db, owner_user_id)
    if owner_err:
        flash(request, owner_err, "error")
        return RedirectResponse("/admin/cars/new", status_code=HTTP_303_SEE_OTHER)

    try:
        cameras = svc.parse_cameras_from_form(camera_id, camera_orientation)
        svc.create_car(
            db,
            name=name,
            imei=imei,
            cameras=cameras,
            owner_user_id=owner_id,
            company_name=owner_name,
            comment=comment,
            is_active=is_active,
            difficulty_rating=clamp_rating(parse_optional_int(difficulty_rating)),
            count_source=count_source,
            source_priority=source_priority,
            non_shedule_count=non_shedule_count,
            alarms_only=alarms_only,
            auto_gap_filling=auto_gap_filling,
            route_kind=route_kind,
            route_kind_chose=route_kind_chose,
            schedule_start=schedule_start.strip(),
            schedule_end=schedule_end.strip(),
            max_people_count_in_car=parse_optional_int(max_people_count_in_car),
        )
    except svc.ServiceError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/admin/cars/new", status_code=HTTP_303_SEE_OTHER)

    flash(request, "Машину додано", "success")
    return RedirectResponse("/admin/cars/", status_code=HTTP_303_SEE_OTHER)


@router.get("/{car_id}/edit")
def edit_form(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    joomla_db: Session = Depends(get_joomla_db),
    user: User = Depends(require_full_manager),
):
    car = svc.get_car(db, car_id)
    if car is None:
        flash(request, "Машину не знайдено", "error")
        return RedirectResponse("/admin/cars/", status_code=HTTP_303_SEE_OTHER)
    start_local, end_local = svc.parse_schedule_to_local(car.schedule)
    return render(
        request, "admin/cars/form.html",
        current_user=user, active_tab="cars",
        car=car, cameras=car.cameras or [], mode="edit",
        owners=joomla_service.list_owners(joomla_db),
        schedule_start=start_local,
        schedule_end=end_local,
        **_META,
    )


@router.post("/{car_id}/edit")
def update(
    car_id: int,
    request: Request,
    name: str = Form(...),
    imei: str = Form(...),
    camera_id: list[str] = Form([]),
    camera_orientation: list[str] = Form([]),
    owner_user_id: str = Form(""),
    comment: str = Form(""),
    is_active: bool = Form(False),
    difficulty_rating: str = Form(""),
    count_source: str = Form("photo"),
    source_priority: str = Form("video"),
    non_shedule_count: bool = Form(False),
    alarms_only: bool = Form(False),
    auto_gap_filling: bool = Form(False),
    route_kind: list[str] = Form([]),
    route_kind_chose: bool = Form(False),
    schedule_start: str = Form(_DEFAULT_SCHEDULE_START),
    schedule_end: str = Form(_DEFAULT_SCHEDULE_END),
    max_people_count_in_car: str = Form("100"),
    db: Session = Depends(get_db),
    joomla_db: Session = Depends(get_joomla_db),
    user: User = Depends(require_full_manager),
):
    car = svc.get_car(db, car_id)
    if car is None:
        flash(request, "Машину не знайдено", "error")
        return RedirectResponse("/admin/cars/", status_code=HTTP_303_SEE_OTHER)

    owner_id, owner_name, owner_err = _resolve_owner(joomla_db, owner_user_id)
    if owner_err:
        flash(request, owner_err, "error")
        return RedirectResponse(
            f"/admin/cars/{car_id}/edit", status_code=HTTP_303_SEE_OTHER
        )

    try:
        cameras = svc.parse_cameras_from_form(camera_id, camera_orientation)
        svc.update_car(
            db, car,
            name=name,
            imei=imei,
            cameras=cameras,
            owner_user_id=owner_id,
            company_name=owner_name,
            comment=comment,
            is_active=is_active,
            difficulty_rating=clamp_rating(parse_optional_int(difficulty_rating)),
            count_source=count_source,
            source_priority=source_priority,
            non_shedule_count=non_shedule_count,
            alarms_only=alarms_only,
            auto_gap_filling=auto_gap_filling,
            route_kind=route_kind,
            route_kind_chose=route_kind_chose,
            schedule_start=schedule_start.strip(),
            schedule_end=schedule_end.strip(),
            max_people_count_in_car=parse_optional_int(max_people_count_in_car),
        )
    except svc.ServiceError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(
            f"/admin/cars/{car_id}/edit", status_code=HTTP_303_SEE_OTHER
        )

    flash(request, "Дані машини оновлено", "success")
    return RedirectResponse("/admin/cars/", status_code=HTTP_303_SEE_OTHER)


@router.post("/{car_id}/delete")
def delete(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    car = svc.get_car(db, car_id)
    if car is None:
        flash(request, "Машину не знайдено", "error")
    else:
        try:
            svc.delete_car(db, car)
            flash(request, "Машину видалено разом із розкладами, сесіями підрахунку та історією тарифів", "info")
        except Exception:  # noqa: BLE001
            db.rollback()
            flash(request, "Не вдалося видалити машину: є пов'язані записи", "error")
    return RedirectResponse("/admin/cars/", status_code=HTTP_303_SEE_OTHER)
