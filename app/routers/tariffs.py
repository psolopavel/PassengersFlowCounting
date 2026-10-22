from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.database import get_db, get_routes_db
from app.models import User
from app.security import require_full_manager, require_manager
from app.services import car_service, tariff_service as svc
from app.templating import flash, render

router = APIRouter(prefix="/admin/tariffs")

_META = {
    "route_kinds": svc.ROUTE_KINDS,
    "route_kind_labels": svc.ROUTE_KIND_LABELS,
}


@router.get("/")
def list_view(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    cars = car_service.list_cars(db)
    return render(
        request, "admin/tariffs/list.html",
        current_user=user, active_tab="tariffs", cars=cars, **_META,
    )


@router.get("/{car_id}")
def car_view(
    car_id: int,
    request: Request,
    db: Session = Depends(get_db),
    routes_db: Session = Depends(get_routes_db),
    user: User = Depends(require_manager),
):
    car = car_service.get_car(db, car_id)
    if car is None:
        flash(request, "Машину не знайдено", "error")
        return RedirectResponse("/admin/tariffs/", status_code=HTTP_303_SEE_OTHER)

    blocks = svc.build_car_page(db, routes_db, car)
    return render(
        request, "admin/tariffs/car.html",
        current_user=user, active_tab="tariffs",
        car=car, blocks=blocks, **_META,
    )


@router.post("/{car_id}/route/{route_id}")
def save_route(
    car_id: int,
    route_id: int,
    request: Request,
    start: list[str] = Form([]),
    tariff_id: list[str] = Form([]),
    db: Session = Depends(get_db),
    user: User = Depends(require_full_manager),
):
    car = car_service.get_car(db, car_id)
    if car is None:
        flash(request, "Машину не знайдено", "error")
        return RedirectResponse("/admin/tariffs/", status_code=HTTP_303_SEE_OTHER)

    periods = [
        {"from": s, "tariff_id": t}
        for s, t in zip(start, tariff_id)
        if s.strip() and t.strip()
    ]
    try:
        svc.save_route_history(db, car_id, route_id, periods)
    except svc.ServiceError as exc:
        flash(request, str(exc), "error")
    else:
        flash(request, "Історію тарифів збережено", "success")

    return RedirectResponse(
        f"/admin/tariffs/{car_id}#route-{route_id}", status_code=HTTP_303_SEE_OTHER
    )
