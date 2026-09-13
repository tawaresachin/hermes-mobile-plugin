"""Hermes Mobile plugin: server update check/apply routes.

Wraps the OFFICIAL `hermes update` CLI — same pipeline the desktop
dashboard Update button drives (hermes_cli/web_routers/actions.py).
No custom update logic: the plugin only shells out, parses, and
reports. Termux quirks (uv bootstrap, abi3 .so relinks) are handled
by `hermes update` + the on-device hermes-update-termux skill; on
failure the log tail rides back to the app.

Routes (require API key):
  GET  /api/mobile/update/check  -> {ok, up_to_date, behind, current_sha,
                                     latest_sha, branch, detail}
  POST /api/mobile/update/apply  -> {ok, log}  (detached: update, then
                                     restart the gateway to load it)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aiohttp import web

from .audio_routes import _json_response, require_key
from .constants import HERMES_HOME

logger = logging.getLogger(__name__)

_UPDATE_TIMEOUT_S = 120
_CHECK_CACHE_TTL_S = 60.0
_check_cache: Optional[Tuple[float, Dict[str, Any]]] = None
_log_path = str(HERMES_HOME / "logs" / "mobile_update.log")

_SHA_LINE = re.compile(r"\b([0-9a-f]{7,40})\b")


def _installed_version() -> str:
    # The plugin runs INSIDE the gateway process, which is the hermes
    # install — importing the package IS the installed version, no
    # subprocess needed.
    try:
        import hermes_cli
        return getattr(hermes_cli, "__version__", "") or ""
    except Exception:
        return ""


def _hermes_bin() -> Optional[str]:
    """The hermes CLI entry point next to the running venv, or PATH.
    Shared with the supervisor; resolves hermes.exe on Windows."""
    from .constants import find_hermes_cli
    return find_hermes_cli()


def _checkout_dir() -> Optional[Path]:
    import sys
    d = Path(sys.executable).parent.parent
    return d if (d / ".git").exists() or (d.parent / ".git").exists() else None


def _run_check_git() -> Dict[str, Any]:
    """Report-only fallback: fetch + count, no install side effects. Same
    plumbing `hermes update --check` wraps; used when the CLI itself errors
    so the card never dead-ends."""
    import sys
    repo = Path(sys.executable).parent.parent
    try:
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=30).stdout.strip() or "main"
        subprocess.run(["git", "-C", str(repo), "fetch", "origin", branch],
                       capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S)
        behind_s = subprocess.run(
            ["git", "-C", str(repo), "rev-list", f"HEAD..origin/{branch}", "--count"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        cur = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        lat = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", f"origin/{branch}"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        behind = int(behind_s) if behind_s.isdigit() else None
        return {"ok": behind is not None, "up_to_date": behind == 0, "behind": behind,
                "current_sha": cur, "latest_sha": lat, "branch": branch,
                "installed_version": _installed_version(),
                "detail": f"git: {cur} -> {lat} ({behind} behind origin/{branch})"}
    except Exception as exc:
        return {"ok": False, "error": f"git fallback failed: {str(exc)[:200]}"}


def _run_check() -> Dict[str, Any]:
    hermes = _hermes_bin()
    if not hermes:
        return {"ok": False, "error": "hermes CLI not found on server"}
    try:
        proc = subprocess.run(
            [hermes, "update", "--check"],
            capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "update check timed out"}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)[:300]}
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    # Strip ANSI just in case NO_COLOR was ignored.
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    if "Traceback" in out:
        # Upstream CLI crashed mid-check (observed: update_cmd.py int('')) —
        # fall back to plain git plumbing so the card still reports truth.
        fb = _run_check_git()
        if fb.get("ok"):
            fb["detail"] = "hermes update --check crashed; counted via git.\n" + fb["detail"]
            return fb
        return {"ok": False, "error": "update check crashed (see detail)",
                "detail": out[-1200:]}
    low = out.lower()
    up = any(k in low for k in (
        "already up to date", "up-to-date", "no updates",
        "currently up to date", "is up to date"))
    behind = None
    m = re.search(r"(\d+)\s+commit", out, re.I)
    if m:
        behind = int(m.group(1))
    shas = _SHA_LINE.findall(out)
    current = shas[0][:7] if shas else ""
    latest = shas[-1][:7] if len(shas) > 1 else (current if up else "")
    if not current:
        # CLI output format drifted (no SHA lines) — git plumbing is the
        # source of truth for where HEAD actually is.
        fb = _run_check_git()
        current = fb.get("current_sha") or ""
        latest = fb.get("latest_sha") or latest
        if behind is None:
            behind = fb.get("behind")
    branch = ""
    bm = re.search(r"branch '?([A-Za-z0-9._/-]+)'?", out)
    if bm:
        branch = bm.group(1)
    return {
        "ok": True,
        "up_to_date": bool(up or (behind == 0)),
        "behind": behind,
        "current_sha": current,
        "latest_sha": latest,
        "branch": branch,
        "installed_version": _installed_version(),
        "detail": out[-1200:],
    }


@require_key
async def _update_check_route(request: web.Request) -> web.Response:
    global _check_cache
    now = time.monotonic()
    if _check_cache and _check_cache[0] > now and not request.query.get("fresh"):
        return _json_response(_check_cache[1])
    try:
        data = await asyncio.to_thread(_run_check)
    except Exception as exc:
        logger.exception("mobile update check failed")
        return _json_response({"ok": False, "error": str(exc)[:300]}, status=500)
    _check_cache = (now + _CHECK_CACHE_TTL_S, data)
    return _json_response(data)


@require_key
async def _update_apply_route(request: web.Request) -> web.Response:
    """Detached update: `hermes update` then a gateway restart to load it.

    setsid + redirect so the chain survives this gateway being restarted
    by it. The app polls /api/mobile/update/check + server health to
    confirm the swap (the response itself cannot promise completion).
    """
    hermes = _hermes_bin()
    if not hermes:
        return _json_response({"ok": False, "error": "hermes CLI not found on server"}, status=500)
    Path(_log_path).parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # PowerShell equivalent; DETACHED so it outlives the gateway restart.
        cmd = ["powershell", "-NoProfile", "-Command",
               f"Start-Sleep 2; & '{hermes}' update; & '{hermes}' gateway restart"]
        detach = {"creationflags": 0x00000008 | 0x00000200}  # DETACHED|NEW_GROUP
    else:
        script = (
            f"sleep 2; "
            f"'{hermes}' update; "
            f"'{hermes}' gateway restart"
        )
        cmd = ["setsid", "bash", "-c", script]
        detach = {"start_new_session": True}
    try:
        with open(_log_path, "ab") as logf:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                **detach,
            )
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)[:300]}, status=500)
    global _check_cache
    _check_cache = None
    return _json_response({"ok": True, "log": _log_path,
                           "note": "Update + gateway restart started; poll update/check to confirm"})


@require_key
async def _update_version_route(request: web.Request) -> web.Response:
    """Instant: installed version + local sha. NO fetch, NO CLI — the About
    row renders from this; the round-arrow check runs the full route."""
    sha = ""
    try:
        sha = subprocess.run(
            ["git", "-C", str(Path(sys.executable).parent.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        pass
    return _json_response({"ok": True, "version": _installed_version(), "sha": sha})


def register(app: web.Application) -> None:
    app.router.add_get("/api/mobile/update/version", _update_version_route)
    app.router.add_get("/api/mobile/update/check", _update_check_route)
    app.router.add_post("/api/mobile/update/apply", _update_apply_route)
