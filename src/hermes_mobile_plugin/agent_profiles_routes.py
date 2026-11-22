"""Agent Profiles routes for the Hermes Mobile app.

Routes:
  GET  /api/mobile/agent-profiles          -> {ok, profiles, count}
  POST /api/mobile/agent-profiles          -> {ok, profile}
  GET  /api/mobile/agent-profiles/{name}   -> {ok, profile}
  PATCH /api/mobile/agent-profiles/{name}  -> {ok, profile}
  DELETE /api/mobile/agent-profiles/{name} -> {ok}
  GET  /api/mobile/agent-profiles/active   -> {ok, profile}
  POST /api/mobile/agent-profiles/active   -> {ok, profile}
  GET  /api/mobile/agent-profiles/{name}/soul -> {ok, content}
  PUT  /api/mobile/agent-profiles/{name}/soul -> {ok, content}

All state changes delegate to ``hermes_cli.profiles`` (normalize/validate/
create/delete/set-active/get-active/read-write-meta/list) through
``asyncio.to_thread``. When hermes-agent is not importable (standalone dev /
tests) the single ``_LocalProfileCore`` helper implements the same core root
semantics without copying config.yaml, .env or credentials. No file I/O, YAML
parsing or rmtree runs on the aiohttp event loop.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from aiohttp import web

from .audio import AudioBackendError
from .audio_routes import (
    _expected_key,
    _json_response,
    _read_json,
    _unauthorized,
    require_key,
)
from .constants import HERMES_HOME

logger = logging.getLogger(__name__)

# Route-name collisions and Hermes' own reserved ids (core reserves the latter
# in validate_profile_name; we repeat them in the isolated fallback helper).
_MOBILE_RESERVED_NAMES = frozenset({"active"})
_LOCAL_RESERVED_NAMES = frozenset({
    "hermes", "default", "test", "tmp", "root", "sudo", "active",
})
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_MAX_SOUL_BYTES = 1024 * 1024
_MAX_LIST_PROFILES = 200
_MAX_LIST_FIELD_CHARS = 2048
_BLOCKING_TIMEOUT_SECONDS = 30


# ── off-loop I/O + auth ────────────────────────────────────────────────


async def _in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run blocking profile I/O off the event loop with a hard deadline."""
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs), timeout=_BLOCKING_TIMEOUT_SECONDS
    )


def require_key_strict(handler: Any) -> Any:
    """Sensitive routes: 401 when the gateway key cannot be RESOLVED.

    ``audio_routes.require_key`` already fails closed on an exception, but it
    treats a resolved empty key as a keyless gateway. Profile state must never
    be exposed on that drift, so we require a non-empty key first.
    """
    base = require_key(handler)

    @functools.wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        try:
            if not _expected_key(request.app):
                return _unauthorized()
        except Exception as exc:  # noqa: BLE001 - unresolved key is never "open"
            logger.warning("profile route: API key unresolved; refusing: %s", exc)
            return _unauthorized()
        return await base(request)

    return wrapped


async def _read_json_object(request: web.Request) -> dict[str, Any]:
    """Sanitized JSON object body; 413 stays 413, anything else is a 400."""
    try:
        body = await _read_json(request)
    except AudioBackendError as exc:
        if exc.status == 413:
            raise AudioBackendError("body too large", status=413) from None
        raise AudioBackendError("invalid JSON body", status=400) from None
    if not isinstance(body, dict):
        raise AudioBackendError("JSON body must be an object", status=400)
    return body


# ── small shared file helpers ──────────────────────────────────────────


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp + fsync + rename; failures propagate (never silently swallowed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Per-file corrupt-YAML isolation: one bad file cannot break a listing."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_yaml(path: Path, updates: dict[str, Any]) -> None:
    data = _load_yaml(path)
    data.update(updates)
    _atomic_write_text(path, yaml.safe_dump(data, sort_keys=False))


def _write_soul(home: Path | str, content: str) -> None:
    """Atomic, size-capped SOUL.md write (ME-13/LO-3)."""
    if len(content.encode("utf-8")) > _MAX_SOUL_BYTES:
        raise ValueError("SOUL.md exceeds the 1 MiB size limit")
    _atomic_write_text(Path(home) / "SOUL.md", content)


def _safe_read_soul(home: Path | str | None) -> tuple[str, bool]:
    """(content, readable). Missing -> ('', True); unreadable -> ('', False)."""
    if not home:
        return "", True
    path = Path(home) / "SOUL.md"
    try:
        if not path.exists():
            return "", True
    except (OSError, TypeError, ValueError):
        return "", False
    try:
        return path.read_text(encoding="utf-8"), True
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return "", False


def _soul_readable(home: Path | str | None) -> bool:
    """Cheap readability probe for list entries (no whole-file read)."""
    if not home:
        return True
    try:
        path = Path(home) / "SOUL.md"
        if not path.exists():
            return True
        with path.open("rb") as fh:
            fh.read(1)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _pid_from_file(path: Path) -> int | None:
    """Live pidfiles are JSON ({"pid": N}); a bare integer is accepted too."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    pid: Any = None
    if text.startswith("{"):
        try:
            pid = json.loads(text).get("pid")
        except (ValueError, AttributeError, TypeError):
            return None
    elif text.isdigit():
        pid = int(text)
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        return pid
    return None


def _pid_alive(pid: int | None) -> bool:
    """Liveness probe WITHOUT the Windows os.kill hazard (os.kill(pid, 0)
    terminates the process on Windows)."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION, 259 = STILL_ACTIVE.
        try:
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                kernel.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - never kill to probe
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _local_gateway_running(profile_dir: Path | str) -> bool:
    return _pid_alive(_pid_from_file(Path(profile_dir) / "gateway.pid"))


# ── one core helper: hermes_cli.profiles, or the smallest equivalent ───


class _LocalProfileCore:
    """Credential-free equivalent of the core profile API for standalone use.

    Mirrors core root semantics: named profiles at <root>/profiles/<id>, the
    sticky active name in <root>/active_profile. Nothing is copied from the
    default profile (no config.yaml, .env or credentials).
    """

    @staticmethod
    def root() -> Path:
        try:
            from hermes_constants import get_default_hermes_root

            return Path(get_default_hermes_root())
        except Exception:  # noqa: BLE001 - standalone fallback
            return HERMES_HOME

    @classmethod
    def profiles_root(cls) -> Path:
        return cls.root() / "profiles"

    @staticmethod
    def normalize_profile_name(name: Any) -> str:
        if not isinstance(name, str):
            raise TypeError("profile name must be a string")
        stripped = name.strip()
        if not stripped:
            raise ValueError("profile name cannot be empty")
        if stripped.casefold() == "default":
            return "default"
        return stripped.lower()

    @staticmethod
    def validate_profile_name(name: str) -> None:
        if name == "default":
            return
        if not _PROFILE_ID_RE.match(name) or name in _LOCAL_RESERVED_NAMES:
            raise ValueError("invalid profile name")

    @classmethod
    def get_profile_dir(cls, name: str) -> Path:
        canon = cls.normalize_profile_name(name)
        cls.validate_profile_name(canon)
        if canon == "default":
            return cls.root()
        return cls.profiles_root() / canon

    @classmethod
    def create_profile(cls, name: str, **_: Any) -> Path:
        canon = cls.normalize_profile_name(name)
        cls.validate_profile_name(canon)
        if canon == "default":
            raise ValueError("cannot create the default profile")
        target = _contained_profile_dir(canon)
        if target.exists():
            raise FileExistsError("profile already exists")
        target.mkdir(parents=True)
        # Identity markers only — a fresh, credential-free profile.
        _atomic_write_text(target / "config.yaml", "{}\n")
        _atomic_write_text(target / "profile.yaml", "{}\n")
        return target

    @classmethod
    def delete_profile(cls, name: str, yes: bool = False) -> None:
        canon = cls.normalize_profile_name(name)
        cls.validate_profile_name(canon)
        if canon == "default":
            raise ValueError("cannot delete the default profile")
        target = _contained_profile_dir(canon)
        if target.is_dir():
            shutil.rmtree(target)  # propagates; off-loop via _in_thread
        cls._retarget_active(canon)

    @classmethod
    def _active_path(cls) -> Path:
        return cls.root() / "active_profile"

    @classmethod
    def _retarget_active(cls, canon: str) -> None:
        path = cls._active_path()
        if path.is_file() and path.read_text(encoding="utf-8-sig").strip() == canon:
            path.unlink()

    @classmethod
    def get_active_profile(cls) -> str:
        try:
            name = cls._active_path().read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            return "default"
        if not name:
            return "default"
        try:
            canon = cls.normalize_profile_name(name)
            cls.validate_profile_name(canon)
            return canon
        except ValueError:
            return "default"

    @classmethod
    def set_active_profile(cls, name: str) -> None:
        canon = cls.normalize_profile_name(name)
        cls.validate_profile_name(canon)
        target = _contained_profile_dir(canon)
        if canon != "default" and not target.is_dir():
            raise FileNotFoundError("profile not found")
        path = cls._active_path()
        if canon == "default":
            if path.exists():
                path.unlink()
        else:
            _atomic_write_text(path, canon + "\n")

    @staticmethod
    def _check_gateway_running(profile_dir: Path) -> bool:
        return _local_gateway_running(profile_dir)

    @staticmethod
    def read_profile_meta(profile_dir: Path) -> dict[str, Any]:
        data = _load_yaml(Path(profile_dir) / "profile.yaml")
        ui = data.get("ui_meta") if isinstance(data.get("ui_meta"), dict) else {}
        bots = ui.get("hermes-bots") if isinstance(ui.get("hermes-bots"), dict) else {}
        return {
            "description": str(data.get("description") or ""),
            "description_auto": bool(data.get("description_auto", False)),
            "display_name": str(data.get("display_name") or ""),
            "bot_title": str(bots.get("title") or ""),
            "ui_meta": ui,
        }

    @classmethod
    def write_profile_meta(
        cls,
        profile_dir: Path,
        *,
        description: str | None = None,
        description_auto: bool | None = None,
        display_name: str | None = None,
        ui_meta: dict | None = None,
    ) -> None:
        path = Path(profile_dir) / "profile.yaml"
        data = _load_yaml(path)
        if description is not None:
            data["description"] = str(description).strip()
        if description_auto is not None:
            data["description_auto"] = bool(description_auto)
        if display_name is not None:
            if display_name.strip():
                data["display_name"] = display_name.strip()
            else:
                data.pop("display_name", None)
        if ui_meta is not None:
            data["ui_meta"] = ui_meta
        _atomic_write_text(path, yaml.safe_dump(data, sort_keys=False))

    @classmethod
    def list_profiles(cls, *, lazy_skill_count: bool = False) -> list[Any]:
        result = [cls._info_for("default", cls.root(), True)]
        try:
            entries = sorted(cls.profiles_root().iterdir())
        except OSError:
            return result
        markers = ("config.yaml", "profile.yaml", "SOUL.md", ".env", "auth.json", "state.db")
        for entry in entries:
            if not entry.is_dir() or not _PROFILE_ID_RE.match(entry.name):
                continue
            try:
                if not any((entry / m).exists() for m in markers):
                    continue
                result.append(cls._info_for(entry.name, entry, False))
            except OSError:
                continue  # one corrupt profile must not break the list
        return result

    @staticmethod
    def _info_for(name: str, home: Path, is_default: bool) -> Any:
        home = Path(home)
        meta = _LocalProfileCore.read_profile_meta(home)
        cfg = _load_yaml(home / "config.yaml")
        model = cfg.get("model")
        provider = cfg.get("provider")
        if isinstance(model, dict):
            provider = model.get("provider") or provider
            model = model.get("default") or model.get("model")
        if not isinstance(model, (str, type(None))):
            model = None
        try:
            has_env = (home / ".env").is_file()
        except OSError:
            has_env = False
        try:
            skill_count = sum(1 for _ in (home / "skills").rglob("SKILL.md"))
        except OSError:
            skill_count = 0
        return SimpleNamespace(
            name=name, path=home, is_default=is_default, model=model, provider=provider,
            has_env=has_env, skill_count=skill_count,
            gateway_running=_local_gateway_running(home),
            description=meta["description"], description_auto=meta["description_auto"],
            display_name=meta["display_name"], bot_title=meta["bot_title"],
            distribution_name=None, distribution_version=None, distribution_source=None,
            has_alias=False,
        )


_CORE: Any = None


def _core() -> Any:
    """The single core accessor: real hermes_cli.profiles when installed."""
    global _CORE
    if _CORE is None:
        try:
            from hermes_cli import profiles as real_core
        except Exception:  # noqa: BLE001 - hermes-agent absent in standalone use
            logger.info("hermes_cli.profiles unavailable; using local profile helper")
            _CORE = _LocalProfileCore()
        else:
            _CORE = real_core
    return _CORE


def _hermes_root() -> Path:
    getter = getattr(_core(), "_get_default_hermes_home", None)
    if getter is not None:
        try:
            return Path(getter())
        except Exception as exc:  # noqa: BLE001 - fallback below
            logger.debug("core root getter failed: %s", exc)
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001
        return HERMES_HOME


def _profiles_root() -> Path:
    getter = getattr(_core(), "_get_profiles_root", None)
    if getter is not None:
        try:
            return Path(getter())
        except Exception as exc:  # noqa: BLE001 - fallback below
            logger.debug("core profiles-root getter failed: %s", exc)
    return _hermes_root() / "profiles"


def _normalize_and_validate(name: Any) -> str:
    core = _core()
    canon = core.normalize_profile_name(name)
    if canon in _MOBILE_RESERVED_NAMES:
        raise ValueError("reserved profile name")
    core.validate_profile_name(canon)
    return canon


def _contained_profile_dir(name: str) -> Path:
    """Fail closed on traversal/symlink escapes from the core profile root."""
    canon = _normalize_and_validate(name)
    if canon == "default":
        return _hermes_root().resolve()
    raw = Path(_core().get_profile_dir(canon))
    root = _profiles_root().resolve()
    if raw.parent.resolve() != root or raw.resolve().parent != root:
        raise ValueError("profile path escapes the Hermes profile root")
    return raw


def _read_meta(profile_dir: Path | str) -> dict[str, Any]:
    try:
        data = _core().read_profile_meta(Path(profile_dir))
        if isinstance(data, dict):
            return dict(data)
    except Exception:
        logger.exception("corrupt profile meta; using defaults")
    return {"description": "", "description_auto": False, "display_name": "", "bot_title": "", "ui_meta": {}}


def _write_meta(profile_dir: Path | str, meta: dict[str, Any]) -> None:
    """Delegate to core's writer; core has no ui_meta arg, so bot_title is
    merged into profile.yaml separately (both writes are atomic)."""
    core = _core()
    path = Path(profile_dir) / "profile.yaml"
    if isinstance(core, _LocalProfileCore):
        ui = dict(meta.get("ui_meta") or {})
        bots = dict(ui.get("hermes-bots") or {})
        if "bot_title" in meta:
            bots["title"] = str(meta["bot_title"])
        if bots:
            ui["hermes-bots"] = bots
        core.write_profile_meta(
            path.parent,
            description=meta.get("description"),
            description_auto=meta.get("description_auto"),
            display_name=meta.get("display_name"),
            ui_meta=ui,
        )
        return
    kwargs: dict[str, Any] = {}
    if "description" in meta:
        kwargs["description"] = str(meta["description"])
    if "description_auto" in meta:
        kwargs["description_auto"] = bool(meta["description_auto"])
    if "display_name" in meta:
        kwargs["display_name"] = str(meta["display_name"])
    core.write_profile_meta(path.parent, **kwargs)
    if "bot_title" in meta:
        ui = dict(_load_yaml(path).get("ui_meta") or {})
        bots = dict(ui.get("hermes-bots") or {})
        bots["title"] = str(meta["bot_title"])
        ui["hermes-bots"] = bots
        _merge_yaml(path, {"ui_meta": ui})


def _apply_model_provider(home: Path | str, model: str | None, provider: str | None) -> None:
    """Merge ONLY model/provider into this profile's own config.yaml."""
    path = Path(home) / "config.yaml"
    cfg = _load_yaml(path)
    if model is not None:
        cfg["model"] = model
    if provider is not None:
        cfg["provider"] = provider
    _atomic_write_text(path, yaml.safe_dump(cfg, sort_keys=False))


# ── response mapping (list entries are lean + bounded) ─────────────────


def _clip(value: Any, limit: int = _MAX_LIST_FIELD_CHARS) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit]


def _profile_entry(info: Any, *, detail: bool = False) -> dict[str, Any]:
    def field(name: str, default: Any = None) -> Any:
        try:
            return getattr(info, name, default)
        except Exception:  # noqa: BLE001 - isolate a corrupt per-profile info
            return default

    name = str(field("name", "") or "")
    home = field("path", None)
    soul_content, soul_readable = ("", True)
    if detail:
        soul_content, soul_readable = _safe_read_soul(home)
    else:
        soul_readable = _soul_readable(home)
    try:
        skill_count = int(field("skill_count", 0) or 0)
    except (TypeError, ValueError):
        skill_count = 0
    entry: dict[str, Any] = {
        "name": name,
        "is_default": bool(field("is_default", False)),
        "model": _clip(field("model")),
        "provider": _clip(field("provider")),
        "has_env": bool(field("has_env", False)),
        "skill_count": skill_count,
        "gateway_running": bool(field("gateway_running", False)),
        "description": _clip(field("description", "")),
        "description_auto": bool(field("description_auto", False)),
        "display_name": _clip(field("display_name", "") or name),
        "bot_title": _clip(field("bot_title", "")),
        "distribution_name": _clip(field("distribution_name")),
        "distribution_version": _clip(field("distribution_version")),
        "distribution_source": _clip(field("distribution_source")),
        "has_alias": bool(field("has_alias", False)),
        "soul_readable": soul_readable,
    }
    if detail:
        entry["path"] = str(home) if home is not None else ""
        entry["soul_content"] = soul_content
    return entry


def _default_entry(*, detail: bool = True) -> dict[str, Any]:
    info = SimpleNamespace(
        name="default", path=_hermes_root(), is_default=True, gateway_running=False,
        model=None, provider=None, has_env=False, skill_count=0, description="",
        description_auto=False, display_name="default", bot_title="",
        distribution_name=None, distribution_version=None, distribution_source=None,
        has_alias=False,
    )
    return _profile_entry(info, detail=detail)


def _profiles_list() -> tuple[list[dict[str, Any]], bool]:
    """Lean list: no path/soul_content, capped count, per-profile isolation."""
    entries: list[dict[str, Any]] = []
    try:
        infos = list(_core().list_profiles(lazy_skill_count=True))
    except Exception:
        logger.exception("list profiles failed")
        return [_default_entry(detail=False)], True
    for info in infos:
        try:
            entries.append(_profile_entry(info, detail=False))
        except Exception:
            logger.exception("skipping corrupt profile entry")
    entries.sort(key=lambda e: e["name"])
    return entries[:_MAX_LIST_PROFILES], len(entries) > _MAX_LIST_PROFILES


async def _find_profile_async(name: str, *, detail: bool = True) -> dict[str, Any] | None:
    def find() -> dict[str, Any] | None:
        for info in _core().list_profiles():
            if getattr(info, "name", None) == name:
                return _profile_entry(info, detail=detail)
        return None

    return await _in_thread(find)


async def _profiles_list_async() -> tuple[list[dict[str, Any]], bool]:
    return await _in_thread(_profiles_list)


# ── routes ─────────────────────────────────────────────────────────────


@require_key_strict
async def _list_profiles_route(request: web.Request) -> web.Response:
    """GET /api/mobile/agent-profiles -> {ok, profiles, count}."""
    try:
        entries, truncated = await _profiles_list_async()
        payload: dict[str, Any] = {"ok": True, "profiles": entries, "count": len(entries)}
        if truncated:
            payload["truncated"] = True
        return _json_response(payload)
    except Exception:
        logger.exception("list profiles failed")
        return _json_response({"ok": False, "error": "Unable to load profiles"}, status=500)


@require_key_strict
async def _create_profile_route(request: web.Request) -> web.Response:
    """POST /api/mobile/agent-profiles -> {ok, profile} (201 on success)."""
    try:
        body = await _read_json_object(request)
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)

    raw_name = body.get("name", "")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        name = await _in_thread(_normalize_and_validate, raw_name)
        await _in_thread(_contained_profile_dir, name)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    if name == "default":
        return _json_response({"ok": False, "error": "cannot create the default profile"}, status=400)

    display_name = str(body.get("display_name", name)).strip() or name
    description = str(body.get("description", "")).strip()
    model = str(body.get("model", "")).strip() or None
    provider = str(body.get("provider", "")).strip() or None

    try:
        profile_dir = await _in_thread(_core().create_profile, name, no_alias=True, seed_model=False)
    except FileExistsError:
        return _json_response({"ok": False, "error": "profile already exists"}, status=409)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    except Exception:
        logger.exception("create profile failed")
        return _json_response({"ok": False, "error": "Could not create profile"}, status=500)

    soul = f"# {display_name}\n\n{description}" if description else f"# {display_name}\n"
    try:
        await _in_thread(_write_soul, profile_dir, soul)
    except ValueError:
        await _cleanup_failed_create(name, profile_dir)
        return _json_response({"ok": False, "error": "SOUL.md exceeds the 1 MiB size limit"}, status=413)
    except Exception:
        logger.exception("write soul failed")
        await _cleanup_failed_create(name, profile_dir)
        return _json_response({"ok": False, "error": "Could not save SOUL.md"}, status=500)

    if model is not None or provider is not None:
        try:
            await _in_thread(_apply_model_provider, profile_dir, model, provider)
        except Exception:
            logger.exception("update config model/provider failed")
            await _cleanup_failed_create(name, profile_dir)
            return _json_response({"ok": False, "error": "Could not save profile configuration"}, status=500)

    try:
        entry = await _find_profile_async(name)
    except Exception:
        logger.exception("build created profile entry failed")
        return _json_response({"ok": False, "error": "Could not read new profile"}, status=500)
    if entry is None:
        return _json_response({"ok": False, "error": "Could not read new profile"}, status=500)
    return _json_response({"ok": True, "profile": entry}, status=201)


async def _cleanup_failed_create(name: str, profile_dir: Path) -> None:
    """A half-created profile is removed; a failed unlink is reported, not hidden."""
    try:
        await _in_thread(_core().delete_profile, name, yes=True)
    except Exception:
        logger.exception("cleanup of failed profile create failed: %s", profile_dir)


@require_key_strict
async def _get_profile_route(request: web.Request) -> web.Response:
    """GET /api/mobile/agent-profiles/{name} -> {ok, profile}."""
    name = request.match_info.get("name", "")
    if not name:
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        canon = await _in_thread(_normalize_and_validate, name)
        await _in_thread(_contained_profile_dir, canon)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    if canon == "default":
        return _json_response({"ok": True, "profile": await _in_thread(_default_entry)})
    try:
        entry = await _find_profile_async(canon)
    except Exception:
        logger.exception("find profile failed")
        return _json_response({"ok": False, "error": "Unable to load profile"}, status=500)
    if entry is None:
        return _json_response({"ok": False, "error": "profile not found"}, status=404)
    return _json_response({"ok": True, "profile": entry})


@require_key_strict
async def _update_profile_route(request: web.Request) -> web.Response:
    """PATCH /api/mobile/agent-profiles/{name} -> {ok, profile}."""
    name = request.match_info.get("name", "")
    if not name:
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        canon = await _in_thread(_normalize_and_validate, name)
        await _in_thread(_contained_profile_dir, canon)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)

    existing = await _find_profile_async(canon)
    if existing is None:
        return _json_response({"ok": False, "error": "profile not found"}, status=404)
    try:
        body = await _read_json_object(request)
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)

    meta = await _in_thread(_read_meta, existing["path"])
    meta.setdefault("description", "")
    meta.setdefault("description_auto", False)
    meta.setdefault("display_name", canon)
    meta.setdefault("bot_title", "")
    meta.setdefault("ui_meta", {})

    for key in ("description", "description_auto", "display_name", "bot_title"):
        if key in body:
            meta[key] = bool(body[key]) if key == "description_auto" else str(body[key]).strip()
    try:
        await _in_thread(_write_meta, existing["path"], meta)
    except Exception:
        logger.exception("write meta failed")
        return _json_response({"ok": False, "error": "Could not save metadata"}, status=500)

    model, provider = body.get("model"), body.get("provider")
    if model is not None or provider is not None:
        try:
            await _in_thread(
                _apply_model_provider, existing["path"],
                str(model).strip() or None if model is not None else None,
                str(provider).strip() or None if provider is not None else None,
            )
        except Exception:
            logger.exception("update config model/provider failed")
            return _json_response({"ok": False, "error": "Could not save profile configuration"}, status=500)

    try:
        entry = await _find_profile_async(canon)
    except Exception:
        logger.exception("build updated profile entry failed")
        return _json_response({"ok": False, "error": "Could not read updated profile"}, status=500)
    return _json_response({"ok": True, "profile": entry})


@require_key_strict
async def _delete_profile_route(request: web.Request) -> web.Response:
    """DELETE /api/mobile/agent-profiles/{name} -> {ok}. Refuses the active profile."""
    name = request.match_info.get("name", "")
    if not name:
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        canon = await _in_thread(_normalize_and_validate, name)
        target = await _in_thread(_contained_profile_dir, canon)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    if canon == "default":
        return _json_response({"ok": False, "error": "cannot delete the default profile"}, status=400)

    existing = await _find_profile_async(canon)
    if existing is None:
        return _json_response({"ok": False, "error": "profile not found"}, status=404)
    try:
        active = await _in_thread(_core().get_active_profile)
        if active == canon:
            return _json_response({"ok": False, "error": "cannot delete the active profile"}, status=409)
        if await _in_thread(_core()._check_gateway_running, target):
            return _json_response({"ok": False, "error": "profile gateway is running"}, status=409)
    except Exception:
        logger.exception("active/gateway check failed")
        return _json_response({"ok": False, "error": "Could not verify profile state"}, status=409)

    try:
        await _in_thread(_core().delete_profile, canon, yes=True)
        return _json_response({"ok": True})
    except Exception:
        logger.exception("delete profile failed")
        return _json_response({"ok": False, "error": "Could not delete profile"}, status=500)


@require_key_strict
async def _get_active_profile_route(request: web.Request) -> web.Response:
    """GET /api/mobile/agent-profiles/active -> {ok, profile}."""
    try:
        active_name = await _in_thread(_core().get_active_profile) or "default"
    except Exception:
        logger.exception("get active profile failed")
        return _json_response({"ok": False, "error": "Could not read active profile"}, status=500)
    if active_name == "default":
        return _json_response({"ok": True, "profile": await _in_thread(_default_entry)})
    entry = await _find_profile_async(active_name)
    if entry is None:
        return _json_response({"ok": True, "profile": await _in_thread(_default_entry)})
    return _json_response({"ok": True, "profile": entry})


@require_key_strict
async def _set_active_profile_route(request: web.Request) -> web.Response:
    """POST /api/mobile/agent-profiles/active -> {ok, profile}."""
    try:
        body = await _read_json_object(request)
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)

    raw_name = body.get("name", "")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        name = await _in_thread(_normalize_and_validate, raw_name)
        await _in_thread(_contained_profile_dir, name)
        await _in_thread(_core().set_active_profile, name)
    except (TypeError, ValueError, FileNotFoundError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    except Exception:
        logger.exception("set active profile failed")
        return _json_response({"ok": False, "error": "Could not set active profile"}, status=500)

    entry = await _find_profile_async(name)
    if entry is None:
        entry = await _in_thread(_default_entry)
    return _json_response({"ok": True, "profile": entry})


@require_key_strict
async def _get_soul_route(request: web.Request) -> web.Response:
    """GET /api/mobile/agent-profiles/{name}/soul -> {ok, content}."""
    name = request.match_info.get("name", "")
    if not name:
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        canon = await _in_thread(_normalize_and_validate, name)
        home = await _in_thread(_contained_profile_dir, canon)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    try:
        content, readable = await _in_thread(_safe_read_soul, home)
    except Exception:
        logger.exception("read soul failed")
        return _json_response({"ok": False, "error": "Could not read SOUL.md"}, status=500)
    if not readable:
        return _json_response({"ok": False, "error": "SOUL.md could not be read"}, status=500)
    return _json_response({"ok": True, "content": content})


@require_key_strict
async def _put_soul_route(request: web.Request) -> web.Response:
    """PUT /api/mobile/agent-profiles/{name}/soul -> {ok, content}."""
    name = request.match_info.get("name", "")
    if not name:
        return _json_response({"ok": False, "error": "name is required"}, status=400)
    try:
        canon = await _in_thread(_normalize_and_validate, name)
        home = await _in_thread(_contained_profile_dir, canon)
    except (TypeError, ValueError, OSError):
        return _json_response({"ok": False, "error": "invalid profile name"}, status=400)
    try:
        body = await _read_json_object(request)
    except AudioBackendError as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=exc.status)
    content = body.get("content", "")
    if not isinstance(content, str):
        return _json_response({"ok": False, "error": "content must be a string"}, status=400)
    try:
        await _in_thread(_write_soul, home, content)
    except ValueError:
        return _json_response({"ok": False, "error": "SOUL.md exceeds the 1 MiB size limit"}, status=413)
    except Exception:
        logger.exception("write soul failed")
        return _json_response({"ok": False, "error": "Could not save SOUL.md"}, status=500)
    return _json_response({"ok": True, "content": content})


def register(native_app: web.Application) -> None:
    """Attach agent profile routes to the api_server's web.Application."""
    native_app.router.add_get("/api/mobile/agent-profiles", _list_profiles_route)
    native_app.router.add_post("/api/mobile/agent-profiles", _create_profile_route)
    # Static "active" must precede the dynamic {name} resource or aiohttp
    # resolves /active as a profile named "active".
    native_app.router.add_get("/api/mobile/agent-profiles/active", _get_active_profile_route)
    native_app.router.add_post("/api/mobile/agent-profiles/active", _set_active_profile_route)
    native_app.router.add_get("/api/mobile/agent-profiles/{name}", _get_profile_route)
    native_app.router.add_patch("/api/mobile/agent-profiles/{name}", _update_profile_route)
    native_app.router.add_delete("/api/mobile/agent-profiles/{name}", _delete_profile_route)
    native_app.router.add_get("/api/mobile/agent-profiles/{name}/soul", _get_soul_route)
    native_app.router.add_put("/api/mobile/agent-profiles/{name}/soul", _put_soul_route)
    logger.info(
        "[hermes-mobile-plugin] agent profile routes registered: "
        "GET/POST /api/mobile/agent-profiles, GET/PATCH/DELETE /api/mobile/agent-profiles/{name}, "
        "GET/POST /api/mobile/agent-profiles/active, "
        "GET/PUT /api/mobile/agent-profiles/{name}/soul"
    )
