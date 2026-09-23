"""Voice backend: STT (Whisper) + TTS (Edge/OpenAI/ElevenLabs) via hermes-agent.

Wraps the existing ``tools.voice_mode.transcribe_recording`` and
``tools.tts_tool.text_to_speech_tool`` so the mobile app can use the same
provider chain the desktop voice mode uses, without re-implementing any
provider logic.

Provider chain is configured in ``~/.hermes/config.yaml`` under ``tts.`` and
``voice.`` — this module just delegates.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import tempfile
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# Mirrors hermes-agent's gateway MAX_REQUEST_BYTES (25 MB, api_server.py); a
# 25 MB recording base64-encodes to ~33 MB and dies at the gateway first.
_MAX_TRANSCRIPTION_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
_AUDIO_MIME_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".mp4",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "audio/x-m4a": ".m4a",
    "video/webm": ".webm",
}
_AUDIO_EXT_TO_MIME = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def _audio_extension_for_mime(mime_type: str) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    return _AUDIO_MIME_EXTENSIONS.get(normalized, ".webm")


def _is_supported_audio_mime(mime_type: str) -> bool:
    if not mime_type:
        return False
    normalized = mime_type.split(";", 1)[0].strip().lower()
    return normalized.startswith("audio/") or normalized == "video/webm"


class AudioBackendError(Exception):
    """Raised when STT or TTS fails. Carries an HTTP status for the route handler."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


def _resolve_profile_scope(profile: Optional[str]) -> Any:
    """Return a context manager that scopes config resolution to *profile*.

    None / "" / "current" => no scope (use default home).
    Mirrors ``_config_profile_scope`` in hermes_cli/web_server.py.
    """
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return nullcontext()

    # Imported lazily — these live inside hermes-agent and are not on the
    # plugin's static-analysis path.
    from hermes_constants import (
        set_hermes_home_override,
        reset_hermes_home_override,
    )
    from hermes_cli.web_server import _resolve_profile_dir

    profile_dir = _resolve_profile_dir(requested)

    @contextmanager
    def _scoped() -> Iterator[Any]:
        token = set_hermes_home_override(str(profile_dir))
        try:
            yield profile_dir
        finally:
            reset_hermes_home_override(token)

    return _scoped()


def transcribe_audio(
    audio_b64: str,
    mime_type: str = "audio/webm",
    profile: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Decode base64 audio and return its transcript.

    Args:
        audio_b64: Base64-encoded audio bytes.
        mime_type: MIME type (e.g. ``audio/webm``).
        profile: Optional Hermes profile name.

    Returns:
        Transcript text. Empty string on silence (per
        ``transcribe_recording`` contract).

    Raises:
        AudioBackendError: 400 for bad input, 413 for too-large, 500 for
            provider failures, 503 if no transcription provider is available.
    """
    if not audio_b64 or not audio_b64.strip():
        raise AudioBackendError("audio_b64 is required", status=400)
    if not _is_supported_audio_mime(mime_type):
        raise AudioBackendError("mime_type must be audio/*", status=400)

    try:
        audio_bytes = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioBackendError(f"audio_b64 is not valid base64: {exc}", status=400)

    if not audio_bytes:
        raise AudioBackendError("Audio recording is empty", status=400)
    if len(audio_bytes) > _MAX_TRANSCRIPTION_UPLOAD_BYTES:
        raise AudioBackendError("Audio recording is too large", status=413)

    suffix = _audio_extension_for_mime(mime_type)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="hermes-mobile-voice-", suffix=suffix, delete=False
        ) as tmp:
            tmp.write(audio_bytes)
            temp_path = tmp.name

        try:
            from tools.voice_mode import transcribe_recording
        except ImportError as exc:
            raise AudioBackendError(
                f"Transcription provider unavailable: {exc}", status=503
            )

        scope = _resolve_profile_scope(profile)
        with scope:
            try:
                result = transcribe_recording(temp_path, model=model)
            except Exception as exc:
                logger.exception("Mobile voice transcription failed")
                raise AudioBackendError(
                    f"Transcription failed: {exc}", status=500
                )

        if not isinstance(result, dict):
            # Defensive: if a provider returns a raw string, accept it.
            return (str(result) if result else "").strip()

        if not result.get("success"):
            raise AudioBackendError(
                result.get("error") or "Transcription failed",
                status=400,
            )

        return (result.get("transcript") or "").strip()
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def synthesize_speech(
    text: str,
    profile: Optional[str] = None,
) -> tuple[bytes, str, Optional[str]]:
    """Synthesize text to audio bytes.

    Args:
        text: Text to speak.
        profile: Optional Hermes profile name.

    Returns:
        Tuple of ``(audio_bytes, mime_type, provider_name)``.

    Raises:
        AudioBackendError: 400 for empty text, 500 for provider failures,
            503 if no TTS provider is available.
    """
    text = (text or "").strip()
    if not text:
        raise AudioBackendError("text is required", status=400)

    try:
        from tools.tts_tool import text_to_speech_tool
    except ImportError as exc:
        raise AudioBackendError(
            f"TTS provider unavailable: {exc}", status=503
        )

    import json as _json

    scope = _resolve_profile_scope(profile)
    with scope:
        try:
            result_json = text_to_speech_tool(text)
            result = _json.loads(result_json) if isinstance(result_json, str) else result_json
        except Exception as exc:
            logger.exception("Mobile voice TTS failed")
            raise AudioBackendError(
                f"Speech synthesis failed: {exc}", status=500
            )

    if not result.get("success"):
        raise AudioBackendError(
            result.get("error") or "Speech synthesis failed",
            status=400,
        )

    file_path = result.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise AudioBackendError("Audio file missing", status=500)

    try:
        with open(file_path, "rb") as fh:
            audio_bytes = fh.read()
    except OSError as exc:
        raise AudioBackendError(f"Could not read audio: {exc}", status=500)
    finally:
        try:
            os.unlink(file_path)
        except OSError:
            pass

    ext = os.path.splitext(file_path)[1].lower()
    mime_type = _AUDIO_EXT_TO_MIME.get(ext, "audio/mpeg")
    provider = result.get("provider")
    return audio_bytes, mime_type, provider
