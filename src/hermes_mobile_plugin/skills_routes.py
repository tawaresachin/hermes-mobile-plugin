"""Mobile Settings fallback: skill manifest for the Hermes Mobile app.

Why this exists: the core gateway's ``GET /v1/skills`` enumerates skills via
``tools.skills_tool._find_all_skills``. Release 0.22.2 ships a broken call
site (the Collective-Wisdom revert stripped the ``include_editorial`` kwarg
from the tool but left the caller in ``api_server.py``), so the endpoint 500s
and the mobile app's Settings "Scheduled jobs & skills" panel reports
"Skills (0)" even when the machine has skills installed. This route
enumerates the SAME catalog itself, tolerating whatever signature the
installed hermes release exposes, and degrades to an honest ``{ok: false}``
500 instead of a silent failure the app cannot distinguish from an empty set.

  GET /api/mobile/skills -> {ok, skills:[{name, description, category}]}

Auth: Bearer key (exposes installed skill names + descriptions). Manifest is
cached for 60 s — the skill scan hits the disk (``?fresh`` bypasses).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any

from aiohttp import web

from .audio_routes import _json_response, require_key

logger = logging.getLogger(__name__)

_TTL_S = 60.0
# (expiry_monotonic, payload)
_cache: tuple[float, dict] | None = None


def _list_skills() -> list[dict[str, Any]]:
    """The same catalog ``/skills list`` shows, tolerating any release's
    ``_find_all_skills`` signature: pass ``include_editorial`` only when the
    installed tool accepts it (newer trees); current releases take
    ``skip_disabled`` alone. Returns dicts the route serializes verbatim."""
    from tools.skills_tool import _find_all_skills, _sort_skills

    params = inspect.signature(_find_all_skills).parameters
    kwargs: dict[str, Any] = {"skip_disabled": False}
    if "include_editorial" in params:
        kwargs["include_editorial"] = True
    return _sort_skills(_find_all_skills(**kwargs))


@require_key
async def _skills_route(request: web.Request) -> web.Response:
    global _cache
    now = time.monotonic()
    if _cache and _cache[0] > now and not request.query.get("fresh"):
        return _json_response(_cache[1])
    try:
        raw = await asyncio.to_thread(_list_skills)
        payload: dict[str, Any] = {
            "ok": True,
            "skills": [
                {
                    "name": str(s.get("name", "")),
                    "description": str(s.get("description", "")),
                    "category": str(s.get("category", "")),
                }
                for s in raw
                if s.get("name")
            ],
        }
        payload["count"] = len(payload["skills"])
    except Exception as exc:
        logger.exception("mobile skills: enumeration failed")
        return _json_response({"ok": False, "error": str(exc)}, status=500)
    _cache = (now + _TTL_S, payload)
    return _json_response(payload)


def register(native_app: web.Application) -> None:
    """Attach the skill-manifest route to the api_server's web.Application."""
    native_app.router.add_get("/api/mobile/skills", _skills_route)
    logger.info(
        "[hermes-mobile-plugin] skills route registered: GET /api/mobile/skills"
    )
