"""aiohttp routes for STT/TTS + attachment upload, registered into the api_server platform.

Endpoints (all require the gateway's Bearer API key):
  POST /api/audio/transcribe   {audio_b64, mime_type, profile?, model?}  -> {text}
  POST /api/audio/speak        {text, profile?}                          -> {data_url, mime_type, provider}
  GET  /api/audio/health                                                -> {ok, stt_available, tts_available, providers, plugin_version}
  POST /api/audio/upload       multipart form (file[, session_id])      -> {url}
  GET  /api/audio/download/{session_id}/{filename}                      -> file bytes
  DELETE /api/audio/files/{session_id}/{filename}                       -> {ok}

Both audio actions reuse ``hermes_mobile_plugin.audio`` (which wraps hermes-agent's
``transcribe_recording`` and ``text_to_speech_tool``). Uploads land under
``$HERMES_HOME/mobile-uploads/<session_id>/`` and are served back for the
mobile chat's attachment bubbles.

Mounted via ``ctx.register_platform_handler("api_server", _wire)`` in
``__init__.register(ctx)``.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from .audio import AudioBackendError, transcribe_audio, synthesize_speech
from .constants import PLUGIN_VERSION, UPLOADS_DIR

logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 9 * 1024 * 1024  # 9 MB — matches the app's cap. The
# api_server app's aiohttp client_max_size (MAX_REQUEST_BYTES = 10 MB) 413s
# any larger body BEFORE this route runs; 9 MB leaves framing headroom so
# the route's own error (clear message) fires before the gateway's generic 413.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")  # session ids: api_... / uuid-ish
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")  # filenames (no path separators)


# ─── Bearer auth (gateway api_server key) ─────────────────────────────

_expected_key_cache: tuple[float, Optional[str]] = (0.0, None)
_KEY_CACHE_TTL_SECONDS = 60.0


def _expected_key() -> Optional[str]:
    """The api_server platform key from config.yaml, TTL-cached. None when unset."""
    global _expected_key_cache
    now = time.monotonic()
    stamp, value = _expected_key_cache
    if now - stamp < _KEY_CACHE_TTL_SECONDS:
        return value
    key: Optional[str] = None
    try:
        from .config import load_hermes_config
        cfg = load_hermes_config()
        key = (
            cfg.get("platforms", {}).get("api_server", {}).get("extra", {}).get("key")
            or None
        )
    except Exception as exc:  # noqa: BLE001 — config read must never 500 a route
        logger.warning("hermes-mobile-qr: could not read api_server key: %s", exc)
        return value  # keep the previous cached value for one more cycle
    if not key:
        # api_server resolves its own key as extra.key OR $API_SERVER_KEY
        # (gateway/platforms/api_server.py). Without this fallback a
        # env-keyed deployment would leave these routes keyless while the
        # rest of the gateway enforces Bearer auth.
        key = os.environ.get("API_SERVER_KEY") or None
    _expected_key_cache = (now, key)
    return key


def _authorized(request: web.Request) -> bool:
    """True when the request carries the gateway Bearer key (or the server has no key)."""
    expected = _expected_key()
    if not expected:
        # No platform key configured: api_server itself runs keyless, so these
        # routes match its posture rather than inventing a second auth scheme.
        return True
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        import hmac
        return hmac.compare_digest(header[len("Bearer "):].strip(), expected)
    return False


def _unauthorized() -> web.Response:
    return web.json_response(
        {"ok": False, "error": "unauthorized"},
        status=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_key(handler: Any) -> Any:
    """Guard a route with the gateway Bearer key. /api/audio/health stays
    open on purpose: the mobile app probes it to decide whether voice is
    available, before/independently of any session, and it exposes no
    user data. Everything that costs money or touches disk is gated."""

    @functools.wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _unauthorized()
        return await handler(request)

    return _wrapped


async def _read_json(request: web.Request) -> dict:
    try:
        body = await request.read()
    except Exception as exc:
        raise AudioBackendError(f"Could not read body: {exc}", status=400)
    if not body:
        raise AudioBackendError("Body is required", status=400)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AudioBackendError(f"Body is not valid JSON: {exc}", status=400)


def _json_response(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


# whisper-cli / edge-tts are blocking subprocess+HTTP calls. Running them on
# the aiohttp event loop wedges EVERY platform (Telegram, agent turns) for
# their full duration — and the supervisor's TCP fallback happily declares a
# wedged gateway alive. Ship them to the default thread executor with a hard
# timeout instead.
_BLOCKING_TIMEOUT_SECONDS = 180.0


# Upload retention: sweep at most once an hour, off the event loop.
_SWEEP_INTERVAL_SECONDS = 3600.0
_SWEEP_MAX_AGE_SECONDS = 30 * 86400
_last_sweep_monotonic = 0.0


def _sweep_expired_uploads() -> int:
    """Delete upload files older than 30 days; drop emptied session dirs.
    Returns the number of files removed. Blocking (stat/unlink) - callers
    run it via asyncio.to_thread."""
    removed = 0
    cutoff = time.time() - _SWEEP_MAX_AGE_SECONDS
    try:
        sessions = list(UPLOADS_DIR.iterdir()) if UPLOADS_DIR.is_dir() else []
    except OSError:
        return 0
    for sess in sessions:
        try:
            if not sess.is_dir():
                continue
            for old in list(sess.iterdir()):
                try:
                    if old.is_file() and old.stat().st_mtime < cutoff:
                        old.unlink()
                        removed += 1
                except OSError:
                    pass
            if not any(sess.iterdir()):
                sess.rmdir()
        except OSError:
            continue
    if removed:
        logger.info("upload retention: dropped %d expired file(s)", removed)
    return removed


async def _run_blocking(fn, *args, **kwargs):
    import asyncio
    import functools

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, functools.partial(fn, *args, **kwargs)),
        timeout=_BLOCKING_TIMEOUT_SECONDS,
    )


@require_key
async def _transcribe_route(request: web.Request) -> web.Response:
    """POST /api/audio/transcribe"""
    started = time.monotonic()
    try:
        payload = await _read_json(request)
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)

    audio_b64 = (payload.get("audio_b64") or "").strip()
    mime_type = (payload.get("mime_type") or "audio/webm").strip()
    profile = payload.get("profile") or None
    model = payload.get("model") or None

    # Accept data: URLs too — convenient when the client prefers them
    # over a raw base64 string. The mobile app sends raw base64.
    if audio_b64.startswith("data:") and "," in audio_b64:
        header, audio_b64 = audio_b64.split(",", 1)
        if ";base64" not in header:
            return _json_response(
                {"ok": False, "error": "data: URL must be base64"},
                status=400,
            )

    try:
        text = await _run_blocking(
            transcribe_audio, audio_b64,
            mime_type=mime_type, profile=profile, model=model,
        )
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)
    except Exception as exc:
        logger.exception("Unexpected error in /api/audio/transcribe")
        return _json_response(
            {"ok": False, "error": f"Internal error: {exc}"}, status=500
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("mobile transcribe: %dms (%d chars)", elapsed_ms, len(text))
    return _json_response(
        {"ok": True, "text": text, "elapsed_ms": elapsed_ms}
    )


@require_key
async def _speak_route(request: web.Request) -> web.Response:
    """POST /api/audio/speak"""
    started = time.monotonic()
    try:
        payload = await _read_json(request)
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)

    text = payload.get("text")
    if not isinstance(text, str):
        return _json_response(
            {"ok": False, "error": "text must be a string"}, status=400
        )
    profile = payload.get("profile") or None

    try:
        audio_bytes, mime_type, provider = await _run_blocking(
            synthesize_speech, text, profile=profile
        )
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)
    except Exception as exc:
        logger.exception("Unexpected error in /api/audio/speak")
        return _json_response(
            {"ok": False, "error": f"Internal error: {exc}"}, status=500
        )

    encoded = base64.b64encode(audio_bytes).decode("ascii")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "mobile speak: %dms (%d bytes, %s, provider=%s)",
        elapsed_ms, len(audio_bytes), mime_type, provider,
    )
    return _json_response(
        {
            "ok": True,
            "data_url": f"data:{mime_type};base64,{encoded}",
            "mime_type": mime_type,
            "provider": provider,
            "elapsed_ms": elapsed_ms,
            "size_bytes": len(audio_bytes),
        }
    )


async def _health_route(request: web.Request) -> web.Response:
    """GET /api/audio/health — mobile app uses this to detect availability.

    Intentionally unauthenticated: it discloses no user data and the app
    probes it before/independently of a session. Reports which provider
    chains actually work so the app can disable unavailable modes instead
    of failing mid-recording.
    """
    stt = True
    tts = True
    stt_provider = None
    tts_provider = None
    try:
        from tools.transcription_tools import _get_provider as _stt_get_provider, _load_stt_config
        stt_provider = _stt_get_provider(_load_stt_config())
    except Exception:
        pass
    try:
        import tools.voice_mode  # noqa: F401
    except ImportError:
        stt = False
    try:
        import tools.tts_tool  # noqa: F401
        try:
            from tools.tts_tool import _get_provider as _tts_get_provider
            from hermes_cli.config import load_config as _load_cfg
            tts_provider = _tts_get_provider(_load_cfg().get("tts", {}))
        except Exception:
            pass
    except ImportError:
        tts = False

    return _json_response(
        {
            "ok": stt and tts,
            "stt_available": stt,
            "tts_available": tts,
            "stt_provider": stt_provider,
            "tts_provider": tts_provider,
            "plugin_version": PLUGIN_VERSION,
            "upload": True,
        }
    )


# ─── Attachment upload / download ─────────────────────────────────────

@require_key
async def _upload_route(request: web.Request) -> web.Response:
    """POST /api/audio/upload — multipart form: file (+ optional session_id).

    Stores under $HERMES_HOME/mobile-uploads/<session_id>/<unique>-<name> and
    returns a relative download URL the app renders inside message text.
    """
    session_id = (request.query.get("session_id") or "").strip()
    field: Optional[BodyPartReader] = None
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if not isinstance(part, BodyPartReader):
                continue
            if part.name == "file":
                field = part
                break  # file found; remaining fields are ignored
            if part.name == "session_id" and not session_id:
                session_id = ((await part.text()) or "").strip()
    if field is None:
        return _json_response({"ok": False, "error": "file field is required"}, status=400)

    if session_id and not _SAFE_ID_RE.match(session_id):
        return _json_response({"ok": False, "error": "invalid session_id"}, status=400)
    session_id = session_id or "misc"

    raw_name = Path(field.filename or "upload.bin").name
    safe_name = re.sub(r"[^A-Za-z0-9._\-]", "_", raw_name)[:120] or "upload.bin"
    stored = f"{secrets.token_hex(4)}-{safe_name}"
    if not _SAFE_NAME_RE.match(stored):
        return _json_response({"ok": False, "error": "invalid filename"}, status=400)

    target_dir = UPLOADS_DIR / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / stored

    written = 0
    try:
        with open(target, "wb") as fh:
            while True:
                chunk = await field.read_chunk(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise AudioBackendError(
                        f"file too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                        status=413,
                    )
                fh.write(chunk)
    except AudioBackendError as exc:
        target.unlink(missing_ok=True)
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        logger.exception("upload failed")
        return _json_response({"ok": False, "error": f"upload failed: {exc}"}, status=500)

    if written == 0:
        target.unlink(missing_ok=True)
        return _json_response({"ok": False, "error": "empty file"}, status=400)

    # Best-effort disk retention: drop uploads older than 30 days so the
    # store cannot silently grow. Sweeping is O(all upload files) with a
    # stat each, so it is throttled to once an hour and runs off the
    # event loop (never inside the accepted-upload response path).
    global _last_sweep_monotonic
    if time.monotonic() - _last_sweep_monotonic > _SWEEP_INTERVAL_SECONDS:
        _last_sweep_monotonic = time.monotonic()
        try:
            await asyncio.to_thread(_sweep_expired_uploads)
        except Exception:
            logger.debug("upload retention sweep failed", exc_info=True)

    url = f"/api/audio/download/{session_id}/{stored}"
    logger.info("mobile upload: %s (%d bytes)", url, written)
    return _json_response({
        "ok": True, "url": url, "size": written, "name": safe_name,
        # Absolute server path — lets a mobile client put the same
        # "saved at" note into the user message that Telegram does, so the
        # file survives into agent history (readable after a failed turn
        # or model switch), not just as a download link.
        "path": str(target.resolve()),
    })


@require_key
async def _download_route(request: web.Request) -> web.Response:
    """GET /api/audio/download/{session_id}/{filename}"""
    session_id = request.match_info.get("session_id", "")
    filename = request.match_info.get("filename", "")
    if not _SAFE_ID_RE.match(session_id) or not _SAFE_NAME_RE.match(filename):
        return _json_response({"ok": False, "error": "not found"}, status=404)
    path = (UPLOADS_DIR / session_id / filename).resolve()
    try:
        path.relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return _json_response({"ok": False, "error": "not found"}, status=404)
    if not path.is_file():
        return _json_response({"ok": False, "error": "not found"}, status=404)
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": ctype})


@require_key
async def _delete_route(request: web.Request) -> web.Response:
    """DELETE /api/audio/files/{session_id}/{filename} — app clears bubbles it deletes."""
    session_id = request.match_info.get("session_id", "")
    filename = request.match_info.get("filename", "")
    if not _SAFE_ID_RE.match(session_id) or not _SAFE_NAME_RE.match(filename):
        return _json_response({"ok": False, "error": "not found"}, status=404)
    path = (UPLOADS_DIR / session_id / filename).resolve()
    try:
        path.relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return _json_response({"ok": False, "error": "not found"}, status=404)
    if not path.is_file():
        return _json_response({"ok": False, "error": "not found"}, status=404)
    path.unlink(missing_ok=True)
    return _json_response({"ok": True})


def register(native_app: web.Application) -> None:
    """Attach aiohttp routes to the api_server's native web.Application."""
    native_app.router.add_post("/api/audio/transcribe", _transcribe_route)
    native_app.router.add_post("/api/audio/speak", _speak_route)
    native_app.router.add_get("/api/audio/health", _health_route)
    native_app.router.add_post("/api/audio/upload", _upload_route)
    native_app.router.add_get("/api/audio/download/{session_id}/{filename}", _download_route)
    native_app.router.add_delete("/api/audio/files/{session_id}/{filename}", _delete_route)
    logger.info(
        "[hermes-mobile-qr v%s] Audio routes registered: "
        "POST /api/audio/transcribe, POST /api/audio/speak, GET /api/audio/health, "
        "POST /api/audio/upload, GET /api/audio/download/{sid}/{name}, "
        "DELETE /api/audio/files/{sid}/{name}",
        PLUGIN_VERSION,
    )
