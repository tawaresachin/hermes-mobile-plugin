"""Hermes Mobile plugin: model-provider registration routes.

Mirrors the dashboard's custom-endpoint CRUD (web_routers/config_env.py)
onto the mobile API so the phone can add/edit/rotate/delete OpenAI-
compatible providers without touching a desktop. No reimplementation:
every route delegates to the SAME hermes_cli functions the dashboard
calls, so both surfaces always agree on semantics:

  - keys never enter config.yaml: upsert writes .env and references it
    via key_env (submitted "" = clear, None = keep — the router's rule)
  - list responses carry redacted previews only (has_api_key +
    api_key_preview); plaintext never leaves the server
  - validation probes the endpoint's /models with httpx, proxy-bypassing
    local/Tailscale hosts (#63472 fix lives in the router)

Routes (require API key):
  GET    /api/mobile/providers                 -> {ok, endpoints, current}
  POST   /api/mobile/providers                 -> upsert {ok, id, endpoints}
  DELETE /api/mobile/providers/{endpoint_id}   -> {ok, endpoints}
  POST   /api/mobile/providers/{id}/activate   -> {ok, provider, model}
  POST   /api/mobile/providers/validate        -> {ok, reachable, message, models}
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from aiohttp import web

from .audio_routes import _json_response, require_key

logger = logging.getLogger(__name__)


def _router():
    """Late import of the dashboard router module (gateway may start without
    the web stack; keeps import errors localized to these routes)."""
    from hermes_cli.web_routers import config_env
    return config_env


def _require_key_field(body: Dict[str, Any]) -> Optional[str]:
    """The plaintext key, or None when the caller omitted/blanked it.

    App sends api_key=string to set/rotate, or omits it to keep. A literal
    "" reaches the router as "clear" — same contract as the dashboard."""
    v = body.get("api_key")
    return v if isinstance(v, str) else None


async def _list(request: web.Request) -> web.Response:
    try:
        import asyncio
        ce = _router()
        # The FastAPI route fn takes (profile=None); call the underlying
        # sync body via asyncio.to_thread so the event loop stays free.
        data = await asyncio.to_thread(ce.list_custom_endpoints, None)
        data = dict(data)
        data["ok"] = True
        return _json_response(data)
    except Exception as exc:
        logger.exception("mobile providers list failed")
        return _json_response({"ok": False, "error": str(exc)[:300]}, status=500)


async def _upsert(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    if not name or not base_url:
        return _json_response({"ok": False, "error": "name and base_url are required"}, status=400)
    try:
        import asyncio
        from hermes_cli.web_models import CustomEndpointUpdate
        ce = _router()
        models = body.get("models")
        upd = CustomEndpointUpdate(
            id=body.get("id") or "",
            name=name,
            base_url=base_url,
            # Router requires a non-empty default model; first discovered/
            # selected model serves when the caller left it implicit.
            model=body.get("model") or (models[0] if models else ""),
            api_key=_require_key_field(body),
            context_length=body.get("context_length"),
            discover_models=bool(body.get("discover_models", True)),
            make_default=bool(body.get("make_default", False)),
            models=models if isinstance(models, list) else None,
        )
        data = await asyncio.to_thread(ce.upsert_custom_endpoint, upd, None)
        data = dict(data)
        data["ok"] = True
        # Freshness: the gateway merges providers per-request, and the app
        # drops its own model cache client-side after a successful save.
        return _json_response(data)
    except Exception as exc:
        logger.exception("mobile providers upsert failed")
        # FastAPI HTTPException carries .detail — surface it, not the wrapper.
        detail = getattr(exc, "detail", None)
        return _json_response(
            {"ok": False, "error": str(detail or exc)[:300]},
            status=int(getattr(exc, "status_code", 500) or 500))


async def _delete(request: web.Request) -> web.Response:
    endpoint_id = (request.match_info.get("endpoint_id") or "").strip()
    if not endpoint_id:
        return _json_response({"ok": False, "error": "endpoint_id required"}, status=400)
    try:
        import asyncio
        ce = _router()
        data = await asyncio.to_thread(ce.delete_custom_endpoint, endpoint_id, None)
        data = dict(data)
        data["ok"] = True
        return _json_response(data)
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        status = int(getattr(exc, "status_code", 500) or 500)
        if status == 404:
            return _json_response({"ok": False, "error": "custom endpoint not found"}, status=404)
        logger.exception("mobile providers delete failed")
        return _json_response({"ok": False, "error": str(detail or exc)[:300]}, status=500)


async def _activate(request: web.Request) -> web.Response:
    endpoint_id = (request.match_info.get("endpoint_id") or "").strip()
    if not endpoint_id:
        return _json_response({"ok": False, "error": "endpoint_id required"}, status=400)
    try:
        import asyncio
        ce = _router()
        data = await asyncio.to_thread(ce.activate_custom_endpoint, endpoint_id, None)
        data = dict(data)
        data["ok"] = True
        return _json_response(data)
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        status = int(getattr(exc, "status_code", 500) or 500)
        if status == 404:
            return _json_response({"ok": False, "error": "custom endpoint not found"}, status=404)
        logger.exception("mobile providers activate failed")
        return _json_response({"ok": False, "error": str(detail or exc)[:300]}, status=500)


async def _validate(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    try:
        import asyncio
        from hermes_cli.web_models import CustomEndpointUpdate
        ce = _router()
        models = body.get("models")
        upd = CustomEndpointUpdate(
            id=body.get("id") or "",
            name=body.get("name") or "validate",
            base_url=body.get("base_url") or "",
            model=body.get("model") or (models[0] if models else ""),
            api_key=_require_key_field(body),
        )
        # Async FastAPI route — call directly on the loop (1-arg signature).
        data = await ce.validate_custom_endpoint(upd)
        data = dict(data)
        data.setdefault("ok", False)
        return _json_response(data)
    except Exception as exc:
        logger.exception("mobile providers validate failed")
        return _json_response({"ok": False, "reachable": False,
                               "error": str(exc)[:300], "message": str(exc)[:200],
                               "models": []}, status=500)


def register(app: web.Application) -> None:
    app.router.add_get("/api/mobile/providers", require_key(_list))
    app.router.add_post("/api/mobile/providers", require_key(_upsert))
    app.router.add_delete("/api/mobile/providers/{endpoint_id}", require_key(_delete))
    app.router.add_post("/api/mobile/providers/{endpoint_id}/activate", require_key(_activate))
    app.router.add_post("/api/mobile/providers/validate", require_key(_validate))
