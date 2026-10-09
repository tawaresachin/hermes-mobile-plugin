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

import shlex
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


def _installed_version() -> str:
    # ponytail: delegate to Hermes's own resolver — the same API
    # `hermes --version` uses. It resolves the release base
    # ("0.21.5+5832"), falls back to git.<sha> on untagged checkouts,
    # and reads the install stamp on no-git (ZIP) installs. No
    # parallel stamp logic here. Ceiling: spawns one git call,
    # 3s timeout, process-cached — fine for a rarely-polled About
    # row; not for a hot loop.
    try:
        from hermes_cli.version_info import get_version_info
        return str(get_version_info().display_version)
    except Exception:
        return ""


def _hermes_bin() -> Optional[str]:
    """The hermes CLI entry point next to the running venv, or PATH.
    Shared with the supervisor; resolves hermes.exe on Windows."""
    from .constants import find_hermes_cli
    return find_hermes_cli()


def _heal_step_args() -> list[str]:
    """argv to re-seed the gateway's runtime venv after `hermes update`
    rotates it (which drops the plugin + its qrcode/pyyaml/aiohttp deps,
    so the restarted gateway 404s every /api/mobile/* route).

    Runs the standalone heal script (pure stdlib, ships next to this
    module) under any python3; it does the real pip work via the venv's
    own interpreter. Returns [] when the script or a python3 cannot be
    located — the step then no-ops and the update chain still proceeds.
    ponytail: best-effort heal, never blocks the update.
    """
    import shutil
    script = Path(__file__).with_name("heal_runtime_venv.py")
    if not script.is_file():
        return []
    py = shutil.which("python3") or shutil.which("python")
    if py is None:
        hermes_bin = _hermes_bin()
        py = str(Path(hermes_bin).parent / ("python.exe" if os.name == "nt" else "python3")) if hermes_bin else None
    if py is None:
        return []
    return [py, str(script)]


def _git_argv(args: list[str]) -> list[str]:
    """Reset SIGCHLD before exec'ing git.

    The Termux host runs the gateway with SIGCHLD=IGNORE; git's helper
    children (git-remote-https, rev-list) then die on waitpid and `git
    fetch` silently fails to advance refs — the check counted against a
    stale origin/main and reported a months-old install as "Latest".
    Same reason the apply path perl-wraps. POSIX only; no perl -> plain.
    """
    import shutil
    if os.name != "nt" and shutil.which("perl"):
        return ["perl", "-e", r'$SIG{CHLD}="DEFAULT"; exec @ARGV', *args]
    return args


def _repo_dir() -> Path:
    # ponytail: find the checkout by probing .git — hermes's own
    # _resolve_repo_dir needs hermes_constants importable, which it is
    # not under the toolchain interpreter pre-bootstrap (returned the
    # toolchain root, no .git -> empty SHAs, no update forever). Also
    # pm.paths.repo_root() is the venv workspace (no .git). Candidates
    # in confidence order; last resort venv parent. Ceiling: none of
    # them a repo -> git queries return empty, check reports ok:false.
    import os
    import sys
    cands = []
    hh = os.environ.get("HERMES_HOME")
    if hh:
        cands.append(Path(hh) / "hermes-agent")
    try:
        from hermes_cli.version_info import _resolve_repo_dir
        d = _resolve_repo_dir()
        if d is not None:
            cands.append(Path(d))
    except Exception:
        pass
    cands.append(Path(sys.executable).parent.parent)
    for c in cands:
        if (c / ".git").exists():
            return c
    return cands[-1]


def _run_check_git() -> Dict[str, Any]:
    """Report-only fallback: fetch + count, no install side effects. Same
    plumbing `hermes update --check` wraps; used when the CLI itself errors
    so the card never dead-ends."""
    repo = _repo_dir()
    try:
        branch = subprocess.run(_git_argv(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"]),
                                capture_output=True, text=True, timeout=30).stdout.strip() or "main"
        subprocess.run(_git_argv(["git", "-C", str(repo), "fetch", "origin", branch]),
                       capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S)
        behind_s = subprocess.run(
            _git_argv(["git", "-C", str(repo), "rev-list", f"HEAD..origin/{branch}", "--count"]),
            capture_output=True, text=True, timeout=60).stdout.strip()
        cur = subprocess.run(_git_argv(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]),
                             capture_output=True, text=True, timeout=30).stdout.strip()
        lat = subprocess.run(_git_argv(["git", "-C", str(repo), "rev-parse", "--short", f"origin/{branch}"]),
                             capture_output=True, text=True, timeout=30).stdout.strip()
        behind = int(behind_s) if behind_s.isdigit() else None
        return {"ok": behind is not None, "up_to_date": behind == 0, "behind": behind,
                "current_sha": cur, "latest_sha": lat, "branch": branch,
                "installed_version": _installed_version(),
                "detail": f"git: {cur} -> {lat} ({behind} behind origin/{branch})"}
    except Exception as exc:
        return {"ok": False, "error": f"git fallback failed: {str(exc)[:200]}"}


def _cli_update_refused() -> bool:
    """Run the real `hermes update --check` and detect a DESIGN refusal.

    On a Termux source checkout the updater refuses on purpose (building
    Python packages on-device); nothing the app can do will make the
    button work. The refusal must reach the row as a reason. Crashes /
    timeouts count as "not refused" — the apply path still runs the CLI.
    """
    hermes = _hermes_bin()
    if not hermes:
        return False
    try:
        proc = subprocess.run(
            [hermes, "update", "--check"],
            capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except Exception:
        return False
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    return "not supported on termux" in out


def _run_check() -> Dict[str, Any]:
    """Git plumbing is the source of truth for branch/behind/SHAs; the CLI
    check adds only whether `hermes update` is permitted on this host.

    The old code regexed `[0-9a-f]{7,40}` out of raw CLI output, which on a
    refused/failed run picked up hex fragments from error text (pm-runtime
    install paths) — the row then showed two plausible-looking SHAs that were
    neither HEAD nor upstream, plus an Update button that no-ops forever
    ("updating but the update never happened"). Git says the truth; the CLI
    says whether acting on it is allowed."""
    data = _run_check_git()
    data["installed_version"] = _installed_version()
    if not data.get("ok"):
        return data
    if _cli_update_refused():
        data["ok"] = False
        data["error"] = ("Update is refused on this host (source checkout on "
                         "Termux — hermes update would build packages on-device). "
                         "Switch to the APT install: pkg install hermes-agent")
        return data
    data["ok"] = True
    data["up_to_date"] = data.get("behind") == 0
    return data


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
    # A failed probe must not silence the truth for a full TTL: cache errors
    # with an already-expired stamp so the next request retries live.
    _check_cache = (now + _CHECK_CACHE_TTL_S if data.get("ok") else now, data)
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
    if _cli_update_refused():
        # Pre-flight: the updater refuses this host (Termux source
        # checkout). Firing the chain would restart the gateway and
        # change NOTHING — the app's "updating…" would end with the
        # same version, and the button would be back. Report instead.
        return _json_response({"ok": False, "error":
            "hermes update is refused on this host (Termux source "
            "checkout — it would build Python packages on-device). "
            "Switch to the APT install (pkg install hermes-agent), then "
            "update."}, status=409)
    Path(_log_path).parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Service-manager host: `gateway restart` is correct here (systemd
        # /launchd equivalent owns relaunch). DETACHED so it outlives the
        # gateway dying mid-update.
        # PowerShell single-quoted strings escape ' by doubling it.
        ps_hermes = hermes.replace("'", "''")
        # Re-seed the runtime venv after the update (same as the POSIX
        # chain); PowerShell single-quotes, internal ' doubled.
        heal = _heal_step_args()
        ps_heal = (
            "; " + " ".join(f"'{a.replace(chr(39), chr(39) * 2)}'" for a in heal)
            if heal else ""
        )
        cmd = ["powershell", "-NoProfile", "-Command",
               f"Start-Sleep 2; & '{ps_hermes}' update --yes{ps_heal}; & '{ps_hermes}' gateway restart"]
        detach = {"creationflags": 0x00000008 | 0x00000200}  # DETACHED|NEW_GROUP
    else:
        # Restart authority depends on topology:
        # - this plugin's detached supervisor watchdog is running (mobile
        #   host): `gateway stop` is the bounded hand-off — the watchdog's
        #   health loop (3 x 10s failures + 30s settle) relaunches the
        #   gateway within ~a minute, so an update never leaves it stopped.
        # - otherwise (systemd/launchd/desktop): the service manager owns
        #   relaunch, and `gateway restart` is the correct verb.
        # We never use `gateway restart` on the supervised mobile host: it is
        # drain-aware (waits up to 180s for in-flight turns — on an
        # app-triggered update that is the app's OWN turn) and it races the
        # watchdog's restart loop; observed as a restart wedged 13+ min.
        from .supervisor import is_supervisor_running
        restart_cmd = (
            f"{shlex.quote(hermes)} gateway stop"
            if is_supervisor_running()
            else f"{shlex.quote(hermes)} gateway restart"
        )
        # Termux host runs with SIGCHLD=IGNORE; a setsid child inherits the
        # disposition and `hermes update`'s git-remote-https child dies
        # (waitpid failure -> exit 1, no code swap). The perl wrapper resets
        # it to the default handler before exec'ing. Skipped on hosts
        # without perl — the SIGCHLD quirk is Termux-specific.
        heal = _heal_step_args()
        heal_step = f"{shlex.quote(' '.join(heal))} 2>&1; " if heal else ""
        script = (
            f"sleep 2; "
            f"{shlex.quote(hermes)} update --yes 2>&1; "
            f"{heal_step}"
            f"{restart_cmd} 2>&1"
        )
        import shutil
        if shutil.which("setsid"):
            # Linux/Termux. The Termux host runs with SIGCHLD=IGNORE; a
            # setsid child inherits the disposition and `hermes update`'s
            # git-remote-https child dies (waitpid failure -> exit 1, no
            # code swap). The perl wrapper resets it to the default handler
            # before exec'ing. Skipped on hosts without perl.
            if shutil.which("perl"):
                cmd = ["setsid", "perl", "-e",
                       r'$SIG{CHLD}="DEFAULT"; exec @ARGV',
                       "bash", "-c", script]
            else:
                cmd = ["setsid", "bash", "-c", script]
        else:
            # macOS/BSD ship no setsid BINARY — start_new_session below
            # already detaches via the setsid() syscall, so plain bash is
            # fully detached there. (The old code always prefixed setsid,
            # which made the mobile update button 500 on every Mac.)
            cmd = ["bash", "-c", script]
        detach = {"start_new_session": True}
    start = time.monotonic()
    try:
        with open(_log_path, "ab") as logf:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                **detach,
            )
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)[:300]}, status=500)
    # Wait a short window so the caller learns whether the detached
    # chain actually started and is making progress -- not just that
    # Popen() accepted the argv.  A process that dies immediately
    # (refused updater, missing interpreter, SIGCHLD quirk) must not
    # be reported as "ok" while the app spins "updating..." for 5 min.
    deadline = start + 6.0
    try:
        last = Path(_log_path).stat().st_size
    except FileNotFoundError:
        last = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        try:
            cur = Path(_log_path).stat().st_size
        except FileNotFoundError:
            continue
        if cur > last:
            last = cur
            break
        if proc.poll() is not None:
            await asyncio.sleep(0.6)
            break
    if proc.poll() is not None and proc.returncode != 0:
        return _json_response(
            {"ok": False,
             "error": f"detached update exited {proc.returncode}; see {_log_path}",
             "log": _log_path},
            status=500)
    if time.monotonic() - start > 6.0 and last == 0:
        return _json_response(
            {"ok": False,
             "error": "detached update did not start writing; see " + _log_path},
            status=500)
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
            _git_argv(["git", "-C", str(_repo_dir()), "rev-parse", "--short", "HEAD"]),
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        pass
    return _json_response({"ok": True, "version": _installed_version(), "sha": sha})


def register(app: web.Application) -> None:
    app.router.add_get("/api/mobile/update/version", _update_version_route)
    app.router.add_get("/api/mobile/update/check", _update_check_route)
    app.router.add_post("/api/mobile/update/apply", _update_apply_route)
