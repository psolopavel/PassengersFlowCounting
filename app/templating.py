from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def flash(request: Request, message: str, category: str = "info") -> None:
    """Добавить одноразовое сообщение (info | success | error).

    На клиенте скрывается автоматически через 7 с (static/js/flashes.js).
    """
    flashes = request.session.setdefault("_flashes", [])
    flashes.append({"message": message, "category": category})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("_flashes", [])


def render(request: Request, template_name: str, current_user=None, **context):
    """Единая точка рендеринга: всегда прокидывает request, пользователя и flash."""
    context["request"] = request
    context["current_user"] = current_user
    context["flashes"] = pop_flashes(request)
    context.setdefault("active_tab", None)
    return templates.TemplateResponse(template_name, context)
