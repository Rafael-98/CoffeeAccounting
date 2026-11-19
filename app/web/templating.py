"""Shared Jinja2 environment (autoescape on) and the `render()` helper for all routes.

Every template gets: `request`, `settings`, `t` (string catalog), `csrf_token` (for the
hidden `csrf_token` field of user-side forms; admin templates pass their own) and `header_user`
(the signed-in account and its profile for the header's profile and leaderboard buttons, or None;
never resolved on /admin pages, which have their own header). Use plain
paths for assets (`/static/...`), never absolute URLs.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app.config import get_settings
from app.money import format_money
from app.strings import t

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = ROOT / "templates"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["t"] = t
_env.filters["money"] = format_money


def initials(name: str | None) -> str:
    """Up to two capital initials of a display name ("Anna Maria Schmidt" -> "AS"), for pictureless avatars."""
    words = [w for w in (name or "").split() if w[:1].isalnum()]
    if not words:
        return "?"
    letters = words[0][0] + (words[-1][0] if len(words) > 1 else "")
    return letters.upper()


_env.filters["initials"] = initials
templates = Jinja2Templates(env=_env)


def _header_user(request: Request) -> dict | None:
    if request.url.path.startswith("/admin"):
        return None
    from app.db import read_session
    from app.services import profiles
    from app.web.deps import current_user

    ctx = current_user(request)
    if ctx is None:
        return None
    with read_session() as db:
        profile = profiles.get_profile(db, ctx.user.id)
    return {"id": ctx.user.id, "name": ctx.user.display_name, "profile": profile}


def render(request: Request, name: str, context: dict | None = None, *, status_code: int = 200,
           headers: dict | None = None) -> HTMLResponse:
    from app.web.deps import csrf_token_for  # local import: deps imports services only

    ctx = {"settings": get_settings(), "header_user": _header_user(request)}
    if context is None or "csrf_token" not in context:
        ctx["csrf_token"] = csrf_token_for(request)
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)
    new_cookie = getattr(request.state, "new_csrf_cookie", None)
    if new_cookie:  # `deps.form_csrf_token` issued a pre-session binding for this page
        from app.web.deps import set_csrf_cookie

        set_csrf_cookie(response, new_cookie)
    return response
