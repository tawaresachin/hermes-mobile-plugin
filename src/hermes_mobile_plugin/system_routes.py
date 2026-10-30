"""System + diagnostics routes for the Hermes Mobile app.

The gateway api_server has no system-control surface; the mobile app's
"Keep Computer Awake" toggle and diag-log upload call these. Mounted on the
same api_server aiohttp application as the audio routes.

  GET    /api/system/status  (key)    -> {os, platform, python, awake, awake_mechanism}
  POST   /api/system/awake   {awake}  -> {ok, mechanism}
  POST   /api/diag/log     {device, version, log} -> {ok, path}

Awake control picks the native mechanism for the host OS — termux-wake-lock
(Termux/Android), caffeinate (macOS), SetThreadExecutionState (Windows), or
systemd-inhibit (Linux) — and degrades honestly (awake_mechanism=null,
POST -> 501) when a host has none available, so the app UI degrades instead
of lying. All routes — including the status probe — require the gateway
Bearer key.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from aiohttp import web

from .audio_routes import _json_response, _read_json, require_key
from .constants import HERMES_HOME, PLUGIN_VERSION, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

_AWAKE_FLAG = HERMES_HOME / "mobile-awake.state"
_AWAKE_PID = HERMES_HOME / "mobile-awake.pid"
_DIAG_DIR = HERMES_HOME / "mobile-logs" / "diag"


# ── Keep-awake backends (one per host OS) ───────────────────────────────
# The app's "Keep Computer Awake" toggle reaches a host that is usually a real
# computer (mac/linux/windows) or a Termux/Android phone. We pick the native
# mechanism and degrade honestly (awake_mechanism=null, POST->501) when the
# host has none:
#
#   termux-wake-lock  Termux/Android — fire-and-forget; the Termux service
#                     persists the lock across restarts.
#   caffeinate        macOS — a background `caffeinate -di` child (display +
#                     idle sleep), terminated on toggle-off or gateway restart.
#   power-request     Windows — SetThreadExecutionState() via ctypes, held by
#                     the gateway process and re-asserted on restart.
#   systemd-inhibit   Linux — an idle inhibitor held by a background
#                     `systemd-inhibit ... sleep infinity` child.

_awake_proc: Optional[subprocess.Popen] = None  # caffeinate / systemd-inhibit
# Serializes acquire/release across the route (worker thread), the Windows
# inline call (loop thread) and the startup re-acquire thread, so the
# idempotency check can't be passed twice and spawn a duplicate helper.
_AWAKE_LOCK = threading.Lock()


def _is_termux() -> bool:
    return shutil.which("termux-wake-lock") is not None


def _mechanism() -> Optional[str]:
    """Best keep-awake mechanism available on this host, or None (degraded)."""
    if _is_termux():
        return "termux-wake-lock"
    sysname = platform.system()
    if sysname == "Darwin":
        return "caffeinate" if shutil.which("caffeinate") else None
    if sysname == "Windows":
        return "power-request" if hasattr(ctypes, "windll") else None
    if sysname == "Linux":
        return "systemd-inhibit" if shutil.which("systemd-inhibit") else None
    return None


def _read_awake_state() -> bool:
    try:
        return _AWAKE_FLAG.read_text(encoding="utf-8").strip() == "on"
    except OSError:
        return False


def _spawn_background(cmd: list) -> subprocess.Popen:
    """Start a long-lived keep-awake helper that dies WITH the gateway.

    No start_new_session: on a restart the child is reaped and
    _ensure_awake_on_startup() re-spawns it if the state flag still says on.
    """
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pid_command(pid: int) -> list:
    """Best-effort argv tokens for a PID ([] if unknown) — used to confirm a
    recorded helper is really ours before we kill it. `ps -ww` avoids the
    default column truncation that would otherwise mangle the command."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return fh.read().split(b"\x00")
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip().split()
    except (OSError, subprocess.SubprocessError):
        return []


def _is_our_helper(argv: list) -> bool:
    """True only when argv looks exactly like a helper we spawned.

    A bare substring match would SIGTERM a user's own `caffeinate -u -t 3600`
    or unrelated `systemd-inhibit` run. We require argv[0]'s basename to be
    our binary AND our own marker to be present.
    """
    if not argv:
        return False
    exe = os.path.basename(argv[0].decode(errors="replace") if isinstance(argv[0], bytes) else str(argv[0]))
    if exe == "caffeinate":
        tokens = [t.decode(errors="replace") if isinstance(t, bytes) else str(t) for t in argv[1:]]
        # Our helper is always `caffeinate -di -w <gateway-pid>`; a bare user
        # `caffeinate -di` (no -w) must never be mistaken for it and killed.
        return "-di" in tokens and "-w" in tokens
    if exe == "systemd-inhibit":
        joined = " ".join(t.decode(errors="replace") if isinstance(t, bytes) else str(t) for t in argv)
        return "hermes-mobile" in joined
    return False


def _reap_tracked_helper() -> None:
    """Terminate a keep-awake helper a previous gateway process orphaned.

    A fresh process cannot see the old child, so without this a re-acquire on
    startup would spawn a second helper and they'd accumulate. We only touch a
    PID we wrote to _AWAKE_PID and verify its command line first, so a recycled
    PID is never killed.
    """
    try:
        pid = int(_AWAKE_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if pid <= 1:
        # 0 = our own process group, -1 = every signalable process: a corrupt
        # or truncated pidfile must never signal anything.
        _AWAKE_PID.unlink(missing_ok=True)
        return
    argv = _pid_command(pid)
    if not _is_our_helper(argv):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.warning("could not signal keep-awake helper pid %d", pid)
    try:
        _AWAKE_PID.unlink(missing_ok=True)
    except OSError:
        pass


def _record_helper_pid(proc: "subprocess.Popen") -> None:
    try:
        _AWAKE_PID.write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass


def _check_cmd(binary: str) -> None:
    """Run a short keep-awake helper (termux-wake-lock/-unlock), raise on fail."""
    proc = subprocess.run(
        [binary],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{binary} exited {proc.returncode}")


def _win32_power(on: bool) -> None:
    """Assert/release the Windows system-required power request via ctypes."""
    if not hasattr(ctypes, "windll"):
        raise RuntimeError("Windows power API unavailable")
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    kernel32 = getattr(ctypes, "windll").kernel32
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
    kernel32.GetThreadExecutionState.restype = ctypes.c_uint32
    if on:
        kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        if not kernel32.GetThreadExecutionState() & ES_SYSTEM_REQUIRED:
            raise RuntimeError("failed to assert Windows wake request")
    else:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # release all


def _verify_helper_alive(proc: "subprocess.Popen", name: str) -> None:
    """Confirm a freshly spawned helper is still running, else fail.

    `systemd-inhibit` in particular exits at once when it cannot reach the
    bus or polkit denies the inhibitor (headless/SSH Linux, WSL2 without
    systemd) — a successful Popen alone is not proof the lock is held. Runs in
    a worker thread (route) / the startup thread, so this sleep never blocks
    the event loop.
    """
    time.sleep(0.3)
    if proc.poll() is not None:
        try:
            _AWAKE_PID.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"{name} exited immediately ({proc.returncode}) — keep-awake unavailable on this host"
        )


def _acquire(mechanism: str) -> None:
    """Turn host keep-awake ON. Raises RuntimeError on failure."""
    global _awake_proc
    with _AWAKE_LOCK:
        if mechanism == "termux-wake-lock":
            _check_cmd("termux-wake-lock")
        elif mechanism == "caffeinate":
            if _awake_proc is not None and _awake_proc.poll() is None:
                return  # helper already running — idempotent, don't double-spawn
            _reap_tracked_helper()
            # -w <gateway-pid>: caffeinate exits when the gateway dies, so a
            # normal stop can't orphan the helper; -w is also the distinctive
            # marker _is_our_helper matches (a bare user `caffeinate -di` won't).
            _awake_proc = _spawn_background(["caffeinate", "-di", "-w", str(os.getpid())])
            _record_helper_pid(_awake_proc)
            _verify_helper_alive(_awake_proc, "caffeinate")
        elif mechanism == "systemd-inhibit":
            if _awake_proc is not None and _awake_proc.poll() is None:
                return
            _reap_tracked_helper()
            _awake_proc = _spawn_background(
                [
                    "systemd-inhibit",
                    "--what", "idle",
                    "--who", "hermes-mobile",
                    "--why", "Hermes mobile keep-awake",
                    "--mode", "block",
                    "sleep", "infinity",
                ]
            )
            _record_helper_pid(_awake_proc)
            _verify_helper_alive(_awake_proc, "systemd-inhibit")
        elif mechanism == "power-request":
            _win32_power(True)
        else:
            raise RuntimeError(f"unknown keep-awake mechanism {mechanism!r}")


def _release(mechanism: str) -> None:
    """Turn host keep-awake OFF."""
    global _awake_proc
    with _AWAKE_LOCK:
        if mechanism == "termux-wake-lock":
            _check_cmd("termux-wake-unlock")
        elif mechanism in ("caffeinate", "systemd-inhibit"):
            if _awake_proc is not None:
                _awake_proc.terminate()
                try:
                    _awake_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _awake_proc.kill()
                    _awake_proc.wait(timeout=5)
                _awake_proc = None
            else:
                # Fresh process: we can't terminate the old child, but a
                # recorded orphan from the previous gateway can still be
                # reaped by pid.
                _reap_tracked_helper()
            try:
                _AWAKE_PID.unlink(missing_ok=True)
            except OSError:
                pass
        elif mechanism == "power-request":
            _win32_power(False)


def _apply_subprocess(awake: bool, mechanism: str) -> None:
    """Apply keep-awake for the subprocess backends. Runs in a worker thread
    (the route wraps it in asyncio.to_thread) so subprocess.run / Popen.wait
    never block the aiohttp event loop. termux, caffeinate and
    systemd-inhibit all use this; only the Windows ctypes path stays inline
    (it must be asserted and released on the same thread)."""
    if awake:
        _acquire(mechanism)
    else:
        _release(mechanism)


def _ensure_awake_on_startup() -> None:
    """Re-establish keep-awake a previous gateway process left ON.

    The state flag is persisted across restarts, so a previously-ON toggle is
    honored on boot and the reap+spawn reconciles any orphan.

    - The Windows power-request is asserted inline on the calling (main)
      thread: the execution state is thread-affine (ES_CONTINUOUS) and would
      die with a short-lived worker thread, so it must be held by the
      long-lived main thread.
    - The subprocess backends' blocking ps look-up + spawn run in a daemon
      thread so they never block the event loop or register() at startup.
    """
    mechanism = _mechanism()
    if mechanism is None or not _read_awake_state():
        return
    if mechanism == "power-request":
        _startup_reacquire(mechanism)  # inline: main thread, fast syscall
        return
    threading.Thread(
        target=_startup_reacquire, args=(mechanism,), daemon=True
    ).start()


def _startup_reacquire(mechanism: str) -> None:
    try:
        _acquire(mechanism)
        logger.info("keep-awake re-acquired on startup (%s)", mechanism)
    except Exception as exc:  # startup is best-effort; never block registration
        logger.warning("could not re-acquire keep-awake on startup: %s", exc)


@require_key
async def _system_status_route(request: web.Request) -> web.Response:
    """GET /api/system/status — gated like every other route: it reports
    plugin version, OS and keep-awake state, which a LAN/VPN neighbor has
    no business enumerating. The app's interceptor always sends the key."""
    mechanism = _mechanism()
    awake = _read_awake_state() if mechanism else False
    # If we hold an in-process helper that has since died, the persisted flag
    # is stale — report off AND self-heal the flag so a later restart doesn't
    # silently re-arm keep-awake after the UI already showed it off.
    if awake and _awake_proc is not None and _awake_proc.poll() is not None:
        awake = False
        try:
            _AWAKE_FLAG.write_text("off", encoding="utf-8")
        except OSError:
            pass
    return _json_response(
        {
            "ok": True,
            "os": platform.system(),
            "platform": "termux" if _is_termux() else platform.system().lower(),
            "python": sys.version.split()[0],
            "awake": awake,
            "awake_mechanism": mechanism,
            "plugin_version": PLUGIN_VERSION,
            "plugin_protocol": PROTOCOL_VERSION,
        }
    )


@require_key
async def _system_awake_route(request: web.Request) -> web.Response:
    """POST /api/system/awake {awake: bool} -> {ok, awake, mechanism}."""
    try:
        payload = await _read_json(request)
    except Exception as exc:  # AudioBackendError also lands here
        return _json_response({"ok": False, "error": str(exc)}, status=400)

    awake = bool(payload.get("awake"))
    mechanism = _mechanism()
    if mechanism is None:
        return _json_response(
            {"ok": False, "error": "keep-awake not supported on this host"},
            status=501,
        )
    try:
        if mechanism == "power-request":
            # Inline on the loop thread (thread affinity), under the lock so a
            # POST can't race the startup re-acquire on the main thread.
            with _AWAKE_LOCK:
                _win32_power(awake)
        else:
            # Subprocess backends: run off the event loop so a slow
            # subprocess.run / Popen.wait / ps never wedges all requests.
            await asyncio.to_thread(_apply_subprocess, awake, mechanism)
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=500)

    try:
        _AWAKE_FLAG.write_text("on" if awake else "off", encoding="utf-8")
    except OSError:
        pass
    logger.info("mobile keep-awake -> %s (%s)", "ON" if awake else "OFF", mechanism)
    return _json_response({"ok": True, "awake": awake, "mechanism": mechanism})


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
    """GET /api/mobile/context-usage?session_id=X -> {used, total, model, source}.

    Parity contract with Telegram /status and the desktop meter: the number
    must come from the agent's own measurement chain, NOT a raw SQL text sum.
    Chain (first hit wins):
      1. live/cached gateway-runner agent -> context_compressor.last_prompt_tokens
         (provider-exact; Telegram-originated sessions),
      2. the session row's persisted provider usage anchor + delta estimate,
         else agent estimate_messages_tokens_rough over the REAL conversation
         loader (counts tool_calls JSON / reasoning / images — the app's own
         sessions land here),
      3. degraded: LENGTH/4 SQL sum (source="estimate") for ancient gateways
         without SessionDB reachable from this process.
    The old implementation was arm 3 only — it undercounted tool-heavy
    sessions by ~20% vs Telegram on the same conversation.
    """
    sid = (request.query.get("session_id") or "").strip()[:200]
    if not sid:
        return _json_response({"ok": False, "error": "session_id is required"}, status=400)
    runner = request.app.get("gateway_runner")

    def _rotate(sid):
        """Follow compression parent->child chain (max 8 hops) to the live tip."""
        import sqlite3
        db = _read_state_db_ro()
        if db is None:
            return sid, None
        try:
            row = db.execute("SELECT model, output_tokens FROM sessions WHERE id = ?", (sid,)).fetchone()
            cur, hops = sid, 0
            while row is None and hops < 8:
                nxt = db.execute(
                    "SELECT id FROM sessions WHERE parent_session_id = ? ORDER BY started_at DESC LIMIT 1",
                    (cur,)).fetchone()
                if not nxt:
                    break
                cur = nxt[0]
                hops += 1
                row = db.execute("SELECT model, output_tokens FROM sessions WHERE id = ?", (cur,)).fetchone()
            return cur, row
        except sqlite3.Error:
            return sid, None
        finally:
            db.close()

    def _agent_route(sid):
        """(used, total, model) from the live/cached runner agent. None when
        this session has no runner-side agent (app sessions don't)."""
        if runner is None:
            return None
        try:
            entry = runner.session_store.lookup_by_session_id(sid)
            if entry is None:
                return None
            agent = runner._running_agents.get(entry.session_key)
            if agent is None:
                agent = runner._cached_agent_for(entry.session_key)
            comp = getattr(agent, "context_compressor", None) if agent is not None else None
            used = max(0, int(getattr(comp, "last_prompt_tokens", 0) or 0)) if comp else 0
            total = int(getattr(comp, "context_length", 0) or 0) if comp else 0
            if used <= 0:
                used = max(0, int(getattr(entry, "last_prompt_tokens", 0) or 0))
            model = str(getattr(agent, "model", "") or "") if agent is not None else ""
            return (used, total, model) if used > 0 or total > 0 else None
        except Exception:
            logger.debug("context-usage: agent route unavailable", exc_info=True)
            return None

    def _session_db_route(sid):
        """(used, model) via the host's SessionDB + the agent's own estimators;
        None when unreachable (caller falls back to the SQL sum)."""
        try:
            adapter = request.app.get("api_server_adapter")
            if adapter is None:
                return None
            # Lazily open through the adapter's own cached accessor (thread
            # call is fine: single-flight lock, DB object cached after first
            # open). Reading _session_db directly would miss fresh gateways.
            sdb = adapter._ensure_session_db()
            if sdb is None:
                return None
            msgs = sdb.get_messages_as_conversation(sid)
            from agent.model_metadata import estimate_messages_tokens_rough
            from agent.usage_anchor import (USAGE_ANCHOR_MODEL_CONFIG_KEY,
                                            anchored_context_tokens, message_fingerprint)
            anchor = None
            getter = getattr(sdb, "get_session_model_config_value", None)
            if callable(getter):
                try:
                    anchor = getter(sid, USAGE_ANCHOR_MODEL_CONFIG_KEY, None)
                except Exception:
                    anchor = None
            used = None
            if isinstance(anchor, dict) and anchor:
                used = anchored_context_tokens(msgs, anchor, charge_stale_thinking=False)
                if used is None:
                    # Row-count drift between the agent's live message list
                    # and the loader's output can stale the base_count (the
                    # fingerprint still exists, just at another index).
                    # Re-anchor by fingerprint so the provider-exact priced
                    # prefix is reused instead of dropping to full-estimate.
                    fp = anchor.get("base_last_fp")
                    bc = int(anchor.get("base_count") or 0)
                    want_role = anchor.get("base_last_role")
                    if isinstance(fp, str) and fp:
                        for idx in range(max(0, bc - 12), len(msgs)):
                            m = msgs[idx]
                            if (m.get("role") == want_role
                                    and message_fingerprint(m) == fp):
                                used = anchored_context_tokens(
                                    msgs, {**anchor, "base_count": idx + 1},
                                    charge_stale_thinking=False)
                                break
            if used is None:
                used = estimate_messages_tokens_rough(msgs, charge_stale_thinking=False)
            return (max(0, int(used or 0)), "")
        except Exception:
            logger.debug("context-usage: session-db route unavailable", exc_info=True)
            return None

    def _sql_estimate(sid, row):
        """Degraded fallback: active-row token_count sum (+system prompt
        estimate). Only counts what SQL can see; kept for old installs where
        SessionDB import/adapter access fails."""
        import sqlite3
        db = _read_state_db_ro()
        if db is None:
            return None
        try:
            used = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN token_count > 0 THEN token_count "
                "ELSE LENGTH(COALESCE(content, '')) / 4 END), 0) FROM messages "
                "WHERE session_id = ? AND active = 1", (sid,)).fetchone()[0]
            sp = db.execute(
                "SELECT COALESCE(LENGTH(system_prompt), 0) / 4 FROM sessions WHERE id = ?",
                (sid,)).fetchone()
            used = int(used or 0) + int((sp[0] if sp else 0) or 0)
            return {"model": (row[0] if row else "") or "", "last_prompt": used,
                    "carry": ((row[1] if row else 0) or 0)}
        except sqlite3.Error:
            return None
        finally:
            db.close()

    tip_sid, row = await asyncio.to_thread(_rotate, sid)
    hit = await asyncio.to_thread(_agent_route, tip_sid)
    if row is None and hit is None:
        # Unknown session (first-ever app-declared id, empty history): zeros,
        # not 404 — the meter starts fresh, matching the previous contract.
        return _json_response({"ok": True, "session_id": sid, "model": "",
                               "last_prompt_tokens": 0, "carry_output_tokens": 0,
                               "source": "empty"})
    if hit:
        used_a, total_a, model_a = hit
        return _json_response({
            "ok": True, "session_id": tip_sid, "model": model_a,
            "last_prompt_tokens": used_a, "context_length": total_a, "source": "agent",
        })
    db_hit = await asyncio.to_thread(_session_db_route, tip_sid)
    if db_hit is not None and (db_hit[0] > 0 or row is None):
        return _json_response({
            "ok": True, "session_id": tip_sid,
            "model": db_hit[1] or ((row[0] if row else "") or ""),
            "last_prompt_tokens": int(db_hit[0]), "carry_output_tokens": (row[1] if row else 0) or 0,
            "source": "agent",
        })
    data = await asyncio.to_thread(_sql_estimate, tip_sid, row)
    if data is None:
        return _json_response({"ok": False, "error": "session not found"}, status=404)
    return _json_response({
        "ok": True, "session_id": tip_sid, "model": data["model"],
        "last_prompt_tokens": data["last_prompt"], "carry_output_tokens": data["carry"],
        "source": "estimate",
    })


@require_key
async def _serve_file_route(request: web.Request) -> web.Response:
    """GET /api/mobile/file?path=<abs> -> raw bytes of a server file.

    The mobile counterpart of Telegram's native document delivery: when the
    agent ends a turn with MEDIA:/abs/file.pdf, the app can actually fetch
    it. Security rides on the host's own gate: validate_media_delivery_path
    (resolved symlinks, credential/system denylist, strict-mode aware) — the
    SAME check api_server applies to MEDIA tags before Telegram delivery.
    Anything it rejects is a 404; we never reveal why.
    """
    import mimetypes
    from pathlib import Path

    raw = (request.query.get("path") or "").strip()
    if not raw:
        return _json_response({"ok": False, "error": "path is required"}, status=400)
    try:
        from gateway.platforms.base import validate_media_delivery_path
        safe = validate_media_delivery_path(raw)
    except Exception:
        logger.warning("mobile file: host validator unavailable; refusing")
        return _json_response({"ok": False, "error": "not found"}, status=404)
    if not safe:
        return _json_response({"ok": False, "error": "not found"}, status=404)
    p = Path(safe)
    if not p.is_file():
        return _json_response({"ok": False, "error": "not found"}, status=404)
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return web.FileResponse(p, headers={
        "Content-Type": ctype,
        "Content-Disposition": f'inline; filename="{p.name}"',
    })



def register(native_app: web.Application) -> None:
    """Attach system/diag routes to the api_server's web.Application."""
    _ensure_awake_on_startup()
    native_app.router.add_get("/api/system/status", _system_status_route)
    native_app.router.add_post("/api/system/awake", _system_awake_route)
    native_app.router.add_post("/api/diag/log", _diag_route)
    native_app.router.add_get("/api/mobile/context-window", _context_window_route)
    native_app.router.add_get("/api/mobile/context-usage", _context_usage_route)
    native_app.router.add_get("/api/mobile/file", _serve_file_route)
    logger.info(
        "[hermes-mobile-plugin v%s] system routes registered: GET /api/system/status, "
        "POST /api/system/awake, POST /api/diag/log, "
        "GET /api/mobile/context-window, GET /api/mobile/context-usage, "
        "GET /api/mobile/file",
        PLUGIN_VERSION,
    )
