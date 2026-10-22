from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from app.config import get_settings
from app.database import get_db, get_routes_db
from app.routers import admin, api_v1, auth, cars, counters, panel, schedules, tariffs
from app.security import (
    SESSION_MAX_AGE,
    NotAuthenticated,
    NotAuthorized,
    get_current_user,
)
from app.templating import flash

settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Passengers Flow Counting", debug=settings.debug)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=SESSION_MAX_AGE,          # сессия живёт 7 дней
    same_site="lax",
    https_only=False,                 # включить True при работе только по HTTPS
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(auth.router)
app.include_router(counters.router)
app.include_router(cars.router)
app.include_router(tariffs.router)
app.include_router(schedules.router)
app.include_router(admin.router)
app.include_router(panel.router)
app.include_router(api_v1.router)


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@app.exception_handler(NotAuthorized)
async def _not_authorized(request: Request, exc: NotAuthorized):
    # Редирект на "/", который сам направит по роли (без циклов /admin ↔ /panel).
    flash(request, "Недостатньо прав для цієї дії", "error")
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    target = "/admin" if user.is_manager else "/panel"
    return RedirectResponse(target, status_code=HTTP_303_SEE_OTHER)


@app.get("/api/health")
def health(
    db: Session = Depends(get_db),
    routes_db: Session = Depends(get_routes_db),
):
    """Проверка, что обе БД доступны."""
    result = {"status": "ok", "app_db": "unknown", "routes_db": "unknown"}
    try:
        db.execute(text("SELECT 1"))
        result["app_db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["app_db"] = f"error: {exc}"
        result["status"] = "degraded"
    try:
        routes_db.execute(text("SELECT 1"))
        result["routes_db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["routes_db"] = f"error: {exc}"
        result["status"] = "degraded"
    return JSONResponse(result)
