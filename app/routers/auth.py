from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.database import get_db
from app.models import User
from app.security import (
    get_current_user,
    login_session,
    logout_session,
    mark_offline,
    touch_login,
    touch_seen,
    verify_password,
)
from app.templating import flash, render

router = APIRouter()


def _home_for(user: User) -> str:
    """Куда отправлять пользователя после входа в зависимости от роли."""
    return "/admin" if user.is_manager else "/panel"


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is not None:
        return RedirectResponse(_home_for(user), status_code=HTTP_303_SEE_OTHER)
    return render(request, "login.html")


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        flash(request, "Невірна пошта або пароль", "error")
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)

    login_session(request, user)
    touch_login(db, user)
    flash(request, "Ви увійшли в систему", "success")
    return RedirectResponse(_home_for(user), status_code=HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is not None:
        mark_offline(db, user)
    logout_session(request)
    flash(request, "Ви вийшли з системи", "info")
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@router.post("/api/heartbeat")
def heartbeat(request: Request, db: Session = Depends(get_db)):
    """Пінг з відкритої вкладки — тримає користувача «онлайн»."""
    user = get_current_user(request, db)
    if user is None:
        return JSONResponse({"ok": False}, status_code=401)
    touch_seen(db, user)
    return JSONResponse({"ok": True})


@router.post("/api/offline")
def offline(request: Request, db: Session = Depends(get_db)):
    """Сигнал про закриття вкладки (sendBeacon) — одразу робимо офлайн."""
    user = get_current_user(request, db)
    if user is not None:
        mark_offline(db, user)
    return JSONResponse({"ok": True})
