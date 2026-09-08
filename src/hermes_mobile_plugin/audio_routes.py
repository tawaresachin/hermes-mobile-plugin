"""aiohttp routes for STT/TTS, registered into the api_server platform.

Endpoints:
  POST /api/audio/transcribe   {audio_b64, mime_type, profile?}  -> {text}
  POST /api/audio/speak        {text, profile?}                    -> {data_url, mime_type, provider}
  GET  /api/audio/health                                          -> {ok, stt_available, tts_available}

Both reuse ``hermes_mobile_plugin.audio`` (which wraps hermes-agent's
``transcribe_recording`` and ``text_to_speech_tool``).

Mounted via ``ctx.register_platform_handler("api_server", _wire)`` in
``__init__.register(ctx)``.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Optional

from aiohttp import web

from .audio import AudioBackendError, transcribe_audio, synthesize_speech

logger = logging.getLogger(__name__)


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
        text = transcribe_audio(audio_b64, mime_type=mime_type, profile=profile, model=model)
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
        audio_bytes, mime_type, provider = synthesize_speech(text, profile=profile)
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
    """GET /api/audio/health — mobile app uses this to detect availability."""
    stt = True
    tts = True
    try:
        import tools.voice_mode  # noqa: F401
    except ImportError:
        stt = False
    try:
        import tools.tts_tool  # noqa: F401
    except ImportError:
        tts = False

    return _json_response(
        {
            "ok": stt and tts,
            "stt_available": stt,
            "tts_available": tts,
            "plugin_version": "0.0.2",
        }
    )


def register(native_app: web.Application) -> None:
    """Attach aiohttp routes to the api_server's native web.Application."""
    native_app.router.add_post("/api/audio/transcribe", _transcribe_route)
    native_app.router.add_post("/api/audio/speak", _speak_route)
    native_app.router.add_get("/api/audio/health", _health_route)
    logger.info(
        "[hermes-mobile-qr v0.0.2] Audio routes registered: "
        "POST /api/audio/transcribe, POST /api/audio/speak, "
        "GET /api/audio/health"
    )
