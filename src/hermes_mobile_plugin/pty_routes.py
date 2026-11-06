"""Gateway-side PTY bridge for the Hermes Mobile app's Terminal tab.

Why this exists: the app's terminal batch 1-3 is device-side SSH
(SSHJ on the phone). B4 adds the *gateway-local* shell the desktop
app has (node-pty): the phone gets a real PTY on the machine that
runs the gateway, over the same authenticated API the rest of the
mobile surface uses. No SSH daemon, no credentials, works wherever
the gateway runs (desktop, server, VPS).

Endpoint
--------
  GET /api/mobile/pty/ws   (WebSocket upgrade, Bearer key)

Frame protocol (JSON text frames, binary not used):

  client -> server
    {"t":"hello","cols":N,"rows":N}   required FIRST frame: spawn the
                                       shell at that geometry
    {"t":"in","d":"<base64>"}         stdin bytes
    {"t":"resize","cols":N,"rows":N}  live TIOCSWINSZ
    {"t":"close"}                     kill the shell, end the session

  server -> client
    {"t":"ok"}                        spawn succeeded
    {"t":"out","d":"<base64>"}        pty master output
    {"t":"exit","code":N}            shell exited (ws stays open a
                                      moment; client should close)
    {"t":"err","m":"<msg>"}           spawn/protocol failure

Security model (mirrors the rest of the mobile surface):
  - The upgrade itself is gated on the gateway Bearer key via
    ``audio_routes._authorized`` — a key that cannot be resolved
    fails closed, exactly like /api/mobile/* .
  - The spawned shell gets a SANITIZED environment: only
    PATH/HOME/USER/LOGNAME/SHELL/TERM/LANG/LC_ALL/TZ survive.
    Anything containing KEY/TOKEN/SECRET/PASSWORD/PRIVATE (case
    insensitive) is dropped, so gateway credentials never leak into
    a remote-viewed shell.
  - Sessions are capped (MAX_PTY_SESSIONS); a disconnect SIGKILLs the
    process group. There is NO idle-kill: quiet is normal for a
    running command, and killing it would defeat the purpose.
  - Output is streamed ONLY to the owning socket; nothing is
    logged or persisted. The log line carries session id + byte
    counts, never content.

Unix only (loop.add_reader): the gateway targets macOS/Linux.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import logging
import os
import signal
import struct
import termios
import time
from typing import Any, Dict, Optional, Set

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)

MAX_PTY_SESSIONS = 3
MAX_FRAME_BYTES = 256 * 1024        # one client frame: generous, not infinite
_CHILD_KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL",
                   "TERM", "LANG", "LC_ALL", "TZ")
_FORBIDDEN_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE")

_live_sessions: Set[str] = set()


def _session_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


def _clean_child_env() -> Dict[str, str]:
    """Minimal env for the spawned shell: no gateway credentials."""
    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        if k in _CHILD_KEEP_ENV:
            env[k] = v
        else:
            ku = k.upper()
            if any(m in ku for m in _FORBIDDEN_ENV_MARKERS):
                continue
            # Belt: only pass through look-harmless values.
            if k in ("EDITOR", "PAGER", "DISPLAY"):
                env[k] = v
    env.setdefault("TERM", "xterm-256color")
    return env


def _authorized_ws(request: web.Request) -> bool:
    """Gate the WS upgrade on the gateway key (keyless = open, same
    posture as the rest of the mobile routes; unresolvable = closed)."""
    from .audio_routes import _authorized
    return _authorized(request)


class _PtySession:
    """One live shell: master fd + child process + idle tracking."""

    def __init__(self, sid: str, cols: int, rows: int) -> None:
        self.sid = sid
        self.cols = cols
        self.rows = rows
        self.master: Optional[int] = None
        self.slave: Optional[int] = None
        self.proc: Optional[Any] = None
        self.last_active = time.monotonic()
        self.closed = False

    def spawn(self) -> None:
        import pty
        self.master, self.slave = pty.openpty()
        shell = os.environ.get("SHELL") or "/bin/sh"
        # Popen is a few ms of blocking; acceptable at connect time,
        # and 'ok' must not go out before the child actually exists.
        self.proc = _fork_shell(shell, self.slave, self.cols, self.rows,
                                _clean_child_env())
        os.close(self.slave)  # parent keeps only master + the child's copy
        self.slave = self.master  # stdin writes go to the master side

    def term_size(self, cols: int, rows: int) -> None:
        if self.master is None:
            return
        self.cols, self.rows = cols, rows
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                         struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass  # master already gone; the exit frame will follow

    def feed_stdin(self, data: bytes) -> None:
        if self.slave is None or self.closed:
            return
        try:
            os.write(self.slave, data)
        except OSError:
            pass  # child exited; exit frame is on its way

    def kill(self) -> None:
        self.closed = True
        proc = self.proc
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    proc.kill()
                except Exception:
                    pass
        for fd in dict.fromkeys((self.master, self.slave)).values():
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self.slave = None
        _live_sessions.discard(self.sid)


def _fork_shell(shell: str, slave_fd: int, cols: int, rows: int,
                env: Dict[str, str]):
    """Spawn the child in its own process group, stdio -> pty."""
    import subprocess

    def _prep():
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ,
                     struct.pack("HHHH", rows, cols, 0, 0))

    return subprocess.Popen(
        [shell],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        preexec_fn=_prep,
        close_fds=True,
    )


async def _pty_loop(sess: _PtySession, ws: web.WebSocketResponse) -> None:
    """Readable(master) pump: base64-chunk out frames, detect exit."""
    loop = asyncio.get_running_loop()
    assert sess.master is not None
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    def _drop_oldest() -> None:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    def _queue_safe_put(item: Any) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            _drop_oldest()
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def _on_readable() -> None:
        if sess.master is None:
            return
        try:
            data = os.read(sess.master, 32 * 1024)
        except OSError:
            data = b""
        if not data:
            _queue_safe_put(None)  # EOF marker
            return
        _queue_safe_put(data)

    loop.add_reader(sess.master, _on_readable)
    exit_code: Optional[int] = None
    try:
        while not ws.closed:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # No output this tick: poll for shell exit. (Deliberately
                # NO idle-kill here: a running command — a 20-minute
                # build, a tail -f — is quiet by nature; killing it would
                # make this a toy. The session cap + disconnect-kill are
                # the resource bounds.)
                if sess.proc is not None and sess.proc.poll() is not None:
                    exit_code = sess.proc.returncode
                    break
                continue
            if chunk is None:
                # master EOF: pump whatever is left, then report exit.
                while not queue.empty():
                    extra = queue.get_nowait()
                    if extra:
                        await ws.send_json(
                            {"t": "out", "d": base64.b64encode(extra).decode("ascii")})
                if exit_code is None:
                    exit_code = (sess.proc.poll() if sess.proc else 0) or 0
                break
            await ws.send_json(
                {"t": "out", "d": base64.b64encode(chunk).decode("ascii")})
    except (ConnectionError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("pty session %s drain error: %s", sess.sid, exc)
    finally:
        try:
            loop.remove_reader(sess.master or -1)
        except (OSError, ValueError):
            pass
        if sess.proc is not None and sess.proc.poll() is None:
            sess.kill()
        if exit_code is not None and not ws.closed:
            try:
                await ws.send_json({"t": "exit", "code": int(exit_code)})
            except Exception:  # noqa: BLE001
                pass


def _pump(sess: _PtySession, ws: web.WebSocketResponse) -> None:
    """Readable-callback body; keeps output flowing without blocking."""
    try:
        data = os.read(sess.master, 32 * 1024)
    except OSError:
        return
    if data and not ws.closed:
        try:
            ws.get_loop().create_task(ws.send_json(
                {"t": "out", "d": base64.b64encode(data).decode("ascii")}))
        except Exception:  # noqa: BLE001
            pass


async def _pty_ws_route(request: web.Request) -> web.WebSocketResponse:
    if not _authorized_ws(request):
        return web.json_response({"ok": False, "error": "unauthorized"},
                                 status=401)  # type: ignore[return-value]
    ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=MAX_FRAME_BYTES)
    await ws.prepare(request)
    sid = _session_id()
    sess: Optional[_PtySession] = None
    logger.info("mobile pty: session %s opened", sid)
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR,
                           WSMsgType.CLOSED):
                break
            if msg.type != WSMsgType.TEXT:
                continue
            if len(msg.data) > MAX_FRAME_BYTES:
                await ws.send_json({"t": "err", "m": "frame too large"})
                continue
            try:
                frame = json.loads(msg.data)
            except ValueError:
                await ws.send_json({"t": "err", "m": "bad frame"})
                continue
            if not isinstance(frame, dict):
                await ws.send_json({"t": "err", "m": "bad frame"})
                continue
            kind = frame.get("t")
            if sess is not None:
                sess.last_active = time.monotonic()

            if kind == "hello":
                if sess is not None:
                    # One shell per socket: a second hello would leak the
                    # first session (its _pty_loop task + pty + child).
                    await ws.send_json({"t": "err", "m": "shell already running"})
                    continue
                cols = _clamped_int(frame.get("cols"), 80, 2, 512)
                rows = _clamped_int(frame.get("rows"), 24, 1, 256)
                if len(_live_sessions) >= MAX_PTY_SESSIONS:
                    await ws.send_json({"t": "err",
                                        "m": "pty session limit reached"})
                    break
                s = _PtySession(sid, cols, rows)
                try:
                    s.spawn()
                except Exception as exc:  # noqa: BLE001
                    await ws.send_json({"t": "err", "m": f"spawn failed: {exc}"})
                    s.kill()
                    break
                sess = s
                _live_sessions.add(sid)
                await ws.send_json({"t": "ok"})
                asyncio.get_running_loop().create_task(
                    _pty_loop(s, ws))
            elif sess is None:
                await ws.send_json({"t": "err", "m": "hello first"})
            elif kind == "in":
                raw = _b64(frame.get("d"))
                if raw is not None:
                    sess.feed_stdin(raw)
            elif kind == "resize":
                sess.term_size(
                    _clamped_int(frame.get("cols"), 80, 2, 512),
                    _clamped_int(frame.get("rows"), 24, 1, 256))
            elif kind == "close":
                await ws.send_json({"t": "exit", "code": 0})
                break
    finally:
        if sess is not None:
            sess.kill()
            _live_sessions.discard(sid)
        logger.info("mobile pty: session %s closed", sid)
    return ws


def _b64(v: Any) -> Optional[bytes]:
    if not isinstance(v, str):
        return None
    try:
        return base64.b64decode(v, validate=True)
    except Exception:  # noqa: BLE001
        return None


def _clamped_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def register(native_app: web.Application) -> None:
    """Mount the PTY bridge on the api_server app (idempotent, like the
    sibling mobile route modules)."""
    app = getattr(native_app, "_app", native_app)
    router = getattr(app, "router", None)
    if router is None:
        raise RuntimeError("mobile pty: api_server app not available")
    router.add_get("/api/mobile/pty/ws", _pty_ws_route)
    logger.info("hermes-mobile-plugin: PTY bridge mounted")
