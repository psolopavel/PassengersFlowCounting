from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.database import get_db
from app.models import User
from app.security import require_full_manager, require_manager
from app.services import counter_service as svc
from app.templating import flash, render
from app.utils import clamp_rating, parse_optional_int, save_upload

router = APIRouter(prefix="/admin/counters")

_META = {
    "genders": svc.GENDERS,
    "employments": svc.EMPLOYMENTS,
    "car_types": svc.CAR_TYPES,
    "gender_labels": svc.GENDER_LABELS,
    "employment_labels": svc.EMPLOYMENT_LABELS,
    "car_type_labels": svc.CAR_TYPE_LABELS,
}


@router.get("/")
def list_view(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    counters = svc.list_counters(db)
    return render(
        request, "admin/counters/list.html",
        current_user=user, active_tab="counters", counters=counters, **_META,
    )


@router.get("/new")
def new_form(
    request: Request,
    user: User = Depends(require_full_manager),
):
    return render(
        request, "admin/counters/form.html",
        current_user=user, active_tab="counters",
        counter=None, mode="create", **_META,
    )


@router.post("/new")
def create(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    age: str = Form(""),
    disability: bool = Form(False),
    gender: str = Form(""),
    employment: str = Form("none"),
    comment: str = Form(""),
    rating: str = Form(""),
    car_types: list[str] = Form([]),
    photo: UploadFile | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    try:
        svc.create_counter(
            db,
            name=name,
            email=email,
            password=password,
            photo_url=save_upload(photo),
            age=parse_optional_int(age),
            disability=disability,
            gender=gender,
            employment=employment,
            comment=comment,
            rating=clamp_rating(parse_optional_int(rating)),
            car_types=car_types,
        )
    except svc.ServiceError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/admin/counters/new", status_code=HTTP_303_SEE_OTHER)

    flash(request, "Рахувальника створено", "success")
    return RedirectResponse("/admin/counters/", status_code=HTTP_303_SEE_OTHER)


@router.get("/{counter_id}/edit")
def edit_form(
    counter_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    counter = svc.get_counter(db, counter_id)
    if counter is None:
        flash(request, "Рахувальника не знайдено", "error")
        return RedirectResponse("/admin/counters/", status_code=HTTP_303_SEE_OTHER)
    return render(
        request, "admin/counters/form.html",
        current_user=user, active_tab="counters",
        counter=counter, mode="edit", **_META,
    )


@router.post("/{counter_id}/edit")
def update(
    counter_id: int,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(""),
    age: str = Form(""),
    disability: bool = Form(False),
    gender: str = Form(""),
    employment: str = Form("none"),
    comment: str = Form(""),
    rating: str = Form(""),
    car_types: list[str] = Form([]),
    photo: UploadFile | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    counter = svc.get_counter(db, counter_id)
    if counter is None:
        flash(request, "Рахувальника не знайдено", "error")
        return RedirectResponse("/admin/counters/", status_code=HTTP_303_SEE_OTHER)

    try:
        svc.update_counter(
            db, counter,
            name=name,
            email=email,
            password=password or None,
            photo_url=save_upload(photo),
            age=parse_optional_int(age),
            disability=disability,
            gender=gender,
            employment=employment,
            comment=comment,
            rating=clamp_rating(parse_optional_int(rating)),
            car_types=car_types,
        )
    except svc.ServiceError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(
            f"/admin/counters/{counter_id}/edit", status_code=HTTP_303_SEE_OTHER
        )

    flash(request, "Дані оновлено", "success")
    return RedirectResponse("/admin/counters/", status_code=HTTP_303_SEE_OTHER)


@router.post("/{counter_id}/delete")
def delete(
    counter_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    counter = svc.get_counter(db, counter_id)
    if counter is None:
        flash(request, "Рахувальника не знайдено", "error")
    else:
        svc.delete_counter(db, counter)
        flash(request, "Рахувальника видалено", "info")
    return RedirectResponse("/admin/counters/", status_code=HTTP_303_SEE_OTHER)
