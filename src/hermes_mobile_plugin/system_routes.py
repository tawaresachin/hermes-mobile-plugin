"""System + diagnostics routes for the Hermes Mobile app.

The gateway api_server has no system-control surface; the mobile app's
"Keep Computer Awake" toggle and diag-log upload call these. Mounted on the
same api_server aiohttp application as the audio routes.

  GET    /api/system/status           -> {os, platform, python, awake, awake_mechanism}
  POST   /api/system/awake   {awake}  -> {ok, mechanism}
  POST   /api/diag/log     {device, version, log} -> {ok, path}

Awake control uses Termux's termux-wake-lock / termux-wake-unlock when
present (this install runs on Termux/Android); on other platforms the routes
report the capability honestly (awake=false, mechanism=null, POST -> 501) so
the app UI degrades instead of lying. All routes require the gateway Bearer
key except GET /api/system/status (no state change, no user data — mirrors
/api/audio/health's posture).
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from typing import Any, Optional

from aiohttp import web

from .audio_routes import _json_response, _read_json, require_key
from .constants import HERMES_HOME, PLUGIN_VERSION

logger = logging.getLogger(__name__)

_AWAKE_FLAG = HERMES_HOME / "mobile-awake.state"
_DIAG_DIR = HERMES_HOME / "mobile-logs" / "diag"


def _wake_lock_bin() -> Optional[str]:
    return shutil.which("termux-wake-lock")


def _read_awake_state() -> bool:
    try:
        return _AWAKE_FLAG.read_text(encoding="utf-8").strip() == "on"
    except OSError:
        return False


async def _system_status_route(request: web.Request) -> web.Response:
    """GET /api/system/status"""
    return _json_response(
        {
            "ok": True,
            "os": platform.system(),
            "platform": "termux" if _wake_lock_bin() else platform.system().lower(),
            "python": sys.version.split()[0],
            "awake": _read_awake_state() if _wake_lock_bin() else False,
            "awake_mechanism": "termux-wake-lock" if _wake_lock_bin() else None,
            "plugin_version": PLUGIN_VERSION,
        }
    )


@require_key
async def _system_awake_route(request: web.Request) -> web.Response:
    """POST /api/system/awake {awake: bool}"""
    try:
        payload = await _read_json(request)
    except Exception as exc:  # AudioBackendError also lands here
        return _json_response({"ok": False, "error": str(exc)}, status=400)

    awake = bool(payload.get("awake"))
    if not _wake_lock_bin():
        return _json_response(
            {"ok": False, "error": "keep-awake not supported on this host"},
            status=501,
        )
    binary = "termux-wake-lock" if awake else "termux-wake-unlock"
    try:
        proc = await _run(binary)
        if proc.returncode != 0:
            return _json_response(
                {"ok": False, "error": f"{binary} exited {proc.returncode}"}, status=500
            )
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=500)

    try:
        _AWAKE_FLAG.write_text("on" if awake else "off", encoding="utf-8")
    except OSError:
        pass
    logger.info("mobile keep-awake -> %s (%s)", "ON" if awake else "OFF", binary)
    return _json_response({"ok": True, "awake": awake, "mechanism": "termux-wake-lock"})


async def _run(binary: str) -> Any:
    proc = await asyncio.create_subprocess_exec(
        binary, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    await proc.communicate()
    return proc


@require_key
async def _diag_route(request: web.Request) -> web.Response:
    """POST /api/diag/log {device, version, log} — store on-device diag log."""
    try:
        payload = await _read_json(request)
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=400)

    log = payload.get("log")
    if not isinstance(log, str) or not log.strip():
        return _json_response({"ok": False, "error": "log is required"}, status=400)
    if len(log) > 2 * 1024 * 1024:
        return _json_response({"ok": False, "error": "log too large (2 MB max)"}, status=413)

    safe_device = "".join(
        c if c.isalnum() or c in "-_." else "_" for c in str(payload.get("device") or "device")
    )[:64]
    _DIAG_DIR.mkdir(parents=True, exist_ok=True)
    path = _DIAG_DIR / f"{safe_device}.log"
    header = (
        f"--- diag upload v{payload.get('version', '?')} at "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + log)
    except OSError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=500)
    logger.info("mobile diag log stored: %s (%d bytes)", path.name, len(log))
    return _json_response({"ok": True, "path": str(path)})


# ── Model context window (chat-screen usage meter) ─────────────────────
# The app shows "used / total" tokens under the input bar. `total` is
# resolved server-side with Hermes' own multi-tier context detection
# (config override -> endpoint probe -> catalog -> family defaults), so the
# phone never guesses a window. Cached: resolution can hit the network.

_CTX_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_CTX_TTL = 600.0
_CTX_MAX = 64  # client-controlled keys (model/provider); cap + evict oldest


@require_key
async def _context_window_route(request: web.Request) -> web.Response:
    """GET /api/mobile/context-window?model=X&provider=Y -> {context_length}."""
    model = (request.query.get("model") or "").strip()[:120]
    provider = (request.query.get("provider") or "").strip()[:120]
    if not model:
        return _json_response({"ok": False, "error": "model is required"}, status=400)
    now = time.monotonic()
    cached = _CTX_CACHE.get((model, provider))
    if cached and cached[1] > now:
        return _json_response({"ok": True, "model": model, "provider": provider,
                               "context_length": cached[0]})

    def _resolve() -> Any:
        from agent.model_metadata import get_model_context_length
        base_url, api_key = "", ""
        try:
            from gateway.run import _resolve_runtime_agent_kwargs_for_provider
            kw = _resolve_runtime_agent_kwargs_for_provider(provider) or {}
            base_url = str(kw.get("base_url") or "")
            api_key = str(kw.get("api_key") or "")
        except Exception:
            pass
        return get_model_context_length(
            model, base_url=base_url, api_key=api_key, provider=provider)

    try:
        loop = asyncio.get_running_loop()
        total = await loop.run_in_executor(None, _resolve)
        total = int(total) if total else None
    except Exception as exc:
        logger.debug("context-window resolve failed: %s", exc)
        total = None
    if total:
        _CTX_CACHE[(model, provider)] = (total, now + _CTX_TTL)
        while len(_CTX_CACHE) > _CTX_MAX:
            _CTX_CACHE.popitem(last=False)  # drop oldest (insertion order)
    return _json_response({"ok": bool(total), "model": model, "provider": provider,
                           "context_length": total})


# ── Live context usage (chat-screen meter) ─────────────────────────────
# `used` = tokens the NEXT turn of this session will carry: the last turn's
# prompt_tokens + its reply + the new user message (estimate). Read from
# state.db (WAL, cheap) so it is accurate even after a server-side
# compression rotated/compacted the transcript while the app was away.

_CTX_EST_CHARS_PER_TOKEN = 4


def _read_state_db_ro():
    import sqlite3
    p = HERMES_HOME / "state.db"
    if not p.exists():
        return None
    try:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None


@require_key
async def _context_usage_route(request: web.Request) -> web.Response:
    """GET /api/mobile/context-usage?session_id=X -> {used, total, model, provider}."""
    sid = (request.query.get("session_id") or "").strip()[:200]
    if not sid:
        return _json_response({"ok": False, "error": "session_id is required"}, status=400)

    def _query(sid=sid):
        db = _read_state_db_ro()
        if db is None:
            return None
        try:
            row = db.execute(
                "SELECT model, output_tokens FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                # Compression rotates transcripts (new session id). Follow
                # the parent->child chain (max 8 hops) to the live session.
                cur, hops = sid, 0
                while hops < 8:
                    nxt = db.execute(
                        "SELECT id FROM sessions WHERE parent_session_id = ? "
                        "ORDER BY started_at DESC LIMIT 1", (cur,),
                    ).fetchone()
                    if not nxt:
                        break
                    cur = nxt[0]
                    hops += 1
                if cur != sid:
                    sid = cur
                    row = db.execute(
                        "SELECT model, output_tokens FROM sessions WHERE id = ?",
                        (sid,),
                    ).fetchone()
            if row is None:
                # First-ever turn for an app-declared id: empty history.
                return {"model": "", "last_prompt": 0, "carry": 0}
            # LIVE context = token_count over ACTIVE rows only. The
            # compressor flips superseded history to active=0, so this
            # sum is exactly what the next turn carries — correct even
            # mid-compression while the app was closed.
            used = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN token_count > 0 THEN token_count "
                "ELSE LENGTH(COALESCE(content, '')) / 4 END), 0) FROM messages "
                "WHERE session_id = ? AND active = 1", (sid,),
            ).fetchone()[0]
            # The live user turn in flight isn't a row yet; add the system
            # prompt estimate of the session row when present.
            sp = db.execute(
                "SELECT COALESCE(LENGTH(system_prompt), 0) / 4 FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            used = int(used or 0) + int((sp[0] if sp else 0) or 0)
            return {"model": row[0] or "", "last_prompt": int(used or 0),
                    "carry": (row[1] or 0)}
        except sqlite3.Error:
            return None
        finally:
            db.close()

    data = await asyncio.to_thread(_query)
    if data is None:
        return _json_response({"ok": False, "error": "session not found"}, status=404)
    return _json_response({
        "ok": True, "session_id": sid, "model": data["model"],
        "last_prompt_tokens": data["last_prompt"], "carry_output_tokens": data["carry"],
    })



def register(native_app: web.Application) -> None:
    """Attach system/diag routes to the api_server's web.Application."""
    native_app.router.add_get("/api/system/status", _system_status_route)
    native_app.router.add_post("/api/system/awake", _system_awake_route)
    native_app.router.add_post("/api/diag/log", _diag_route)
    native_app.router.add_get("/api/mobile/context-window", _context_window_route)
    native_app.router.add_get("/api/mobile/context-usage", _context_usage_route)
    logger.info(
        "[hermes-mobile-qr v%s] system routes registered: GET /api/system/status, "
        "POST /api/system/awake, POST /api/diag/log, "
        "GET /api/mobile/context-window, GET /api/mobile/context-usage",
        PLUGIN_VERSION,
    )
