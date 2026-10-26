from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Car, User
from app.security import require_manager
from app.services import statistics_service
from app.templating import render
from app.utils.local_tz import local_today

router = APIRouter(prefix="/admin")


@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    counters_count = db.execute(
        select(func.count()).select_from(User).where(User.role == "counter")
    ).scalar_one()
    cars_count = db.execute(select(func.count()).select_from(Car)).scalar_one()
    active_cars = db.execute(
        select(func.count()).select_from(Car).where(Car.is_active.is_(True))
    ).scalar_one()

    stats = {
        "counters": counters_count,
        "cars": cars_count,
        "active_cars": active_cars,
    }
    return render(
        request, "admin/dashboard.html",
        current_user=user, active_tab="dashboard", stats=stats,
    )


def _placeholder(tab: str, title: str):
    def handler(request: Request, user: User = Depends(require_manager)):
        return render(
            request, "admin/placeholder.html",
            current_user=user, active_tab=tab, tab_title=title,
        )
    return handler


# Вкладки, которые появятся позже — сейчас заглушки, но уже есть в меню.
for _tab, _title in (
    ("managers", "Менеджери"),
):
    router.add_api_route(f"/{_tab}", _placeholder(_tab, _title), methods=["GET"])


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@router.get("/statistics")
def statistics(
    request: Request,
    dim: str = "counter",
    entity: int | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    preset: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_manager),
):
    if dim not in statistics_service.DIMENSIONS:
        dim = "counter"

    # Списки сутностей для вибору.
    if dim == "car":
        entities = statistics_service.list_cars(db)
    else:
        entities = statistics_service.list_counters(db)

    # Діапазон дат: за замовчуванням понеділок поточного тижня — сьогодні (локальний час).
    today_local = local_today()
    default_from, default_to = statistics_service.default_date_range(today_local)

    preset_range = statistics_service.preset_date_range(preset, today_local) if preset else None
    if preset_range:
        from_day, to_day = preset_range
    else:
        from_day = _parse_day(from_) or default_from
        to_day = _parse_day(to) or default_to
        if from_day > to_day:
            from_day, to_day = to_day, from_day

    result = None
    selected_entity = None
    if entity is not None:
        selected_entity = statistics_service.get_entity(db, dim, entity)
        if selected_entity is not None:
            result = statistics_service.analyze(
                db,
                dimension=dim,
                entity_id=entity,
                from_day=from_day,
                to_day=to_day,
            )

    return render(
        request, "admin/statistics.html",
        current_user=user, active_tab="statistics",
        dim=dim,
        dimension_labels=statistics_service.DIMENSION_LABELS,
        entities=entities,
        entity_id=entity,
        selected_entity=selected_entity,
        from_day=from_day.isoformat(),
        to_day=to_day.isoformat(),
        active_preset=preset if preset in ("week", "2weeks", "month") else None,
        result=result,
        kind_labels=statistics_service.KIND_LABELS,
        status_labels=statistics_service.STATUS_LABELS,
    )
