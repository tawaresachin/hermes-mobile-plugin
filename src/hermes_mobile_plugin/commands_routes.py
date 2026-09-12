"""Dynamic slash-command manifest for the Hermes Mobile app.

Why this exists: Telegram renders its slash menu via ``set_my_commands``,
built from ``hermes_cli.commands.COMMAND_REGISTRY`` + plugin commands +
skill commands (``hermes_cli.commands_platforms.telegram_menu_commands``).
The gateway api_server never sees slash commands — it is a plain prompt
API — so the mobile app had a hardcoded list that drifted from the server's
real command set. This route serves the SAME sources the Telegram adapter
uses, so the app's palette is server-truth instead of a snapshot.

  GET  /api/mobile/commands           -> {ok, commands:[...], hidden}
  POST /api/mobile/command/resolve    -> {ok, expanded} | {ok:false}

The resolve route mirrors gateway/run_inbound.py's skill dispatch: the app
sends ``{"command": "/archify", "args": "..."}`` and gets back the exact
expanded user-message the gateway injects for Telegram
(``build_skill_invocation_message``), which it then runs as a durable turn.
Stacked skills (/a /b) are v1-skipped: resolve handles the single leading
command, the rest rides as args.

Auth: both routes require the gateway Bearer key (they expose skill names +
invoke content). Manifest is cached for 60s — skill scanning hits the disk.
"""

from __future__ import annotations

import logging
import asyncio
import time
from typing import Any, Optional

from aiohttp import web

from .audio_routes import _json_response, _read_json, require_key

logger = logging.getLogger(__name__)

_MANIFEST_TTL_S = 60.0
# (expiry_monotonic, payload)
_manifest_cache: tuple[float, dict] | None = None


def _build_manifest() -> dict:
    """Core + plugin + skill commands, same tier order Telegram uses."""
    commands: list[dict] = []
    seen: set[str] = set()

    # 1. Core CommandDefs available on gateway surfaces.
    try:
        from hermes_cli.commands_platforms import _gateway_available_commands
        for cmd in _gateway_available_commands():
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            commands.append({
                "name": cmd.name,
                "description": cmd.description,
                "category": getattr(cmd, "category", ""),
                "args_hint": getattr(cmd, "args_hint", ""),
                "aliases": list(getattr(cmd, "aliases", ()) or ()),
                "source": "core",
            })
    except Exception as exc:  # registry unavailable (dev/test) — degrade
        logger.warning("mobile commands: core registry failed: %s", exc)

    # 2. Plugin slash commands.
    try:
        from hermes_cli.plugins import get_plugin_commands
        for name, meta in sorted(get_plugin_commands().items()):
            if name in seen:
                continue
            seen.add(name)
            commands.append({
                "name": name,
                "description": str(meta.get("description", "Plugin command")),
                "category": "Plugin",
                "args_hint": str(meta.get("args_hint") or ""),
                "aliases": [],
                "source": "plugin",
            })
    except Exception as exc:
        logger.warning("mobile commands: plugin commands failed: %s", exc)

    # 3. Skill commands (global-disabled already applied by get_skill_commands;
    #    name collisions with core were skipped at scan time with a warning).
    try:
        from agent.skill_commands import get_skill_commands
        for cmd_key, info in sorted(get_skill_commands().items()):
            name = str(cmd_key).lstrip("/")
            if not name or name in seen:
                continue
            seen.add(name)
            commands.append({
                "name": name,
                "description": str(info.get("description", "")),
                "category": "Skill",
                "args_hint": "[instruction]",
                "aliases": [],
                "source": "skill",
            })
    except Exception as exc:
        logger.warning("mobile commands: skill commands failed: %s", exc)

    return {"ok": True, "commands": commands, "count": len(commands)}


@require_key
async def _commands_route(request: web.Request) -> web.Response:
    global _manifest_cache
    now = time.monotonic()
    if _manifest_cache and _manifest_cache[1] > now and not request.query.get("fresh"):
        return _json_response(_manifest_cache[0])
    try:
        manifest = await asyncio.to_thread(_build_manifest)
    except Exception as exc:
        logger.exception("mobile commands: build failed")
        return _json_response({"ok": False, "error": str(exc)}, status=500)
    _manifest_cache = (now + _MANIFEST_TTL_S, manifest)
    return _json_response(manifest)


@require_key
async def _command_resolve_route(request: web.Request) -> web.Response:
    """Expand a skill slash command into the invocation message the gateway
    injects for Telegram. Only skill commands expand here — core commands
    are adapter-executed (the app owns its local handlers) and unknown
    names resolve to nothing."""
    try:
        body = await _read_json(request)
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=400)
    command = str(body.get("command") or "").strip()[:200]
    args = str(body.get("args") or "").strip()[:8000]
    if not command:
        return _json_response({"ok": False, "error": "command is required"}, status=400)
    if time.monotonic() - getattr(request.app, "_last_resolve", 0.0) < 0.5:
        # Resolve builds prompt content from disk; cheap anti-hammering floor.
        return _json_response({"ok": False, "error": "slow down"}, status=429)
    request.app._last_resolve = time.monotonic()

    def _resolve() -> Optional[str]:
        from agent.skill_commands import (
            build_skill_invocation_message, resolve_skill_command_key)
        cmd_key = resolve_skill_command_key(command)
        if cmd_key is None:
            return None
        return build_skill_invocation_message(cmd_key, user_instruction=args)

    try:
        expanded = await asyncio.to_thread(_resolve)
    except Exception as exc:
        logger.exception("mobile command resolve failed")
        return _json_response({"ok": False, "error": str(exc)}, status=500)
    if expanded is None:
        return _json_response({"ok": False, "error": "not a skill command"}, status=404)
    return _json_response({"ok": True, "expanded": expanded})


def register(native_app: web.Application) -> None:
    """Attach command-manifest routes to the api_server's web.Application."""
    native_app.router.add_get("/api/mobile/commands", _commands_route)
    native_app.router.add_post("/api/mobile/command/resolve", _command_resolve_route)
    logger.info(
        "[hermes-mobile-qr] command routes registered: "
        "GET /api/mobile/commands, POST /api/mobile/command/resolve"
    )
