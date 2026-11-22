"""Capability discovery routes for the Hermes Mobile app.

Routes the app calls:
  GET /api/mobile/capabilities/plugins     -> {ok, plugins, count}
  GET /api/mobile/capabilities/mcp-servers -> {ok, mcp_servers, count}
  GET /api/mobile/capabilities/projects    -> {ok, projects, count}
  GET /api/mobile/capabilities/skills      -> {ok, skills, count}
  GET /api/mobile/capabilities/all         -> {ok, plugins, mcp_servers, projects, skills}

Older paths (``/api/mobile/capabilities``, ``/api/mobile/mcp-servers``,
``/api/mobile/all``) stay registered for installed-app compatibility.

MCP env/header VALUES are secrets: only key names are returned, and URLs lose
userinfo passwords and query strings. Blocking reads run off-loop with a hard
deadline, and exceptions are logged server-side but never echoed to clients.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml
from aiohttp import web

from .audio_routes import _json_response, _unauthorized, require_key
from .constants import HERMES_HOME

logger = logging.getLogger(__name__)

_BLOCKING_TIMEOUT_SECONDS = 30
_MAX_ITEMS = 200
_CONFIG_NAMES = ("config.yaml", "config.yml")
_MCP_KEYS = ("mcpServers", "mcp_servers", "mcp")


def require_key_strict(handler: Any) -> Any:
    """Sensitive capability routes: 401 when the gateway key cannot be resolved."""
    base = require_key(handler)

    @functools.wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        from .audio_routes import _expected_key

        try:
            if not _expected_key(request.app):
                return _unauthorized()
        except Exception as exc:  # noqa: BLE001 - unresolved key is never "open"
            logger.warning("capability route: API key unresolved; refusing: %s", exc)
            return _unauthorized()
        return await base(request)

    return wrapped


async def _in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs), timeout=_BLOCKING_TIMEOUT_SECONDS
    )


def _clip(value: Any, limit: int = 300) -> Any:
    """Keep response fields bounded; never serialize nested config blobs."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
        return text[:limit]
    if isinstance(value, list):
        return [_clip(v, limit) for v in value[:20]]
    return None


def _redact_url(url: Any) -> str:
    """Keep scheme/host/port/path; drop credentials and query/fragment."""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            user = userinfo.split(":", 1)[0]
            netloc = f"{user}:…redacted…@{host}" if user else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (ValueError, TypeError):
        return ""


def _sanitize_server(raw: Any) -> dict[str, Any]:
    """One MCP server: scalars + key names only; no env/header VALUES."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if key == "env" and isinstance(value, dict):
            out["env_keys"] = sorted(str(k) for k in value)
        elif key == "headers" and isinstance(value, dict):
            out["header_keys"] = sorted(str(k) for k in value)
        elif key.lower() in {"url", "endpoint"}:
            out[key] = _redact_url(value)
        elif key in {"command", "args", "type", "transport", "disabled", "enabled"}:
            out[key] = _clip(value)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read one small metadata/config file; corrupt files become empty dicts."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _configs_from_env() -> list[Path]:
    paths: list[Path] = []
    for env_name in ("HERMES_HOME", "HERMES_CONFIG_DIR", "HERMES_PROFILE_DIR"):
        value = os.environ.get(env_name, "").strip()
        if value:
            for name in _CONFIG_NAMES:
                path = Path(value) / name
                if path.is_file():
                    paths.append(path)
    return paths


def _get_mcp_servers() -> list[dict[str, Any]]:
    """Collect MCP servers from this profile's config.yaml (never .env)."""
    candidates = _configs_from_env()
    candidates.append(Path(HERMES_HOME) / "config.yaml")

    servers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in candidates:
        data = _read_yaml(path)
        raw: Any = {}
        for key in _MCP_KEYS:
            value = data.get(key)
            if isinstance(value, dict):
                raw = value
                break
        for name, cfg in raw.items():
            key = str(name)
            if key in seen:
                continue
            seen.add(key)
            item = _sanitize_server(cfg)
            item["name"] = key
            item.setdefault("enabled", True)
            servers.append(item)
            if len(servers) >= _MAX_ITEMS:
                return servers
    return servers


def _get_plugins() -> list[dict[str, Any]]:
    """Installed plugins from HERMES_HOME/plugins (no absolute paths)."""
    root = Path(HERMES_HOME) / "plugins"
    plugins: list[dict[str, Any]] = []
    if not root.is_dir():
        return plugins
    try:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            meta = _read_yaml(entry / "plugin.yaml")
            plugins.append(
                {
                    "name": entry.name,
                    "enabled": True,
                    "version": _clip(meta.get("version", "unknown"), 80),
                    "description": _clip(meta.get("description", "")),
                    "author": _clip(meta.get("author", ""), 120),
                }
            )
            if len(plugins) >= _MAX_ITEMS:
                break
    except OSError as exc:
        logger.warning("list plugins failed: %s", exc)
    return plugins


def _get_projects() -> list[dict[str, Any]]:
    """Projects from HERMES_HOME/projects."""
    root = Path(HERMES_HOME) / "projects"
    projects: list[dict[str, Any]] = []
    if not root.is_dir():
        return projects
    try:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            meta = _read_yaml(entry / "project.yaml")
            projects.append(
                {
                    "name": entry.name,
                    "description": _clip(meta.get("description", "")),
                    "active": True,
                }
            )
            if len(projects) >= _MAX_ITEMS:
                break
    except OSError as exc:
        logger.warning("list projects failed: %s", exc)
    return projects


def _get_skills() -> list[dict[str, Any]]:
    """Skills from the same catalog as /api/mobile/skills, with enabled state."""
    try:
        from .skills_routes import _disabled_names, _list_skills
    except Exception as exc:  # noqa: BLE001 - unavailable core is an empty section
        logger.warning("skill catalog unavailable: %s", exc)
        return []
    try:
        raw = _list_skills(including_disabled=True)
        disabled = _disabled_names()
    except Exception as exc:  # noqa: BLE001 - never fail the whole /all response
        logger.warning("list skills failed: %s", exc)
        return []

    skills: list[dict[str, Any]] = []
    for item in raw[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        name = _clip(item.get("name", ""), 80)
        if not name:
            continue
        skills.append(
            {
                "name": name,
                "description": _clip(item.get("description", "")),
                "category": _clip(item.get("category", ""), 80),
                "enabled": name not in disabled,
            }
        )
    return skills


def _get_capabilities() -> list[dict[str, Any]]:
    """Static capability manifest kept in sync with plugin.yaml registration."""
    return [
        {
            "id": "voice.transcribe",
            "name": "Voice Transcription",
            "description": "Speech-to-text via configured STT backend",
            "endpoints": ["/api/mobile/audio/transcribe"],
        },
        {
            "id": "voice.synthesize",
            "name": "Voice Synthesis",
            "description": "Text-to-speech via configured TTS backend",
            "endpoints": ["/api/mobile/audio/synthesize"],
        },
        {
            "id": "agent.chat",
            "name": "Agent Chat",
            "description": "Streaming chat with the Hermes agent",
            "endpoints": ["/api/mobile/agent"],
        },
        {
            "id": "agent.profiles",
            "name": "Agent Profiles",
            "description": "Create, switch and manage isolated agent profiles",
            "endpoints": ["/api/mobile/agent-profiles"],
        },
        {
            "id": "system.capabilities",
            "name": "Capability Discovery",
            "description": "Discover plugins, MCP servers, projects and skills",
            "endpoints": [
                "/api/mobile/capabilities/plugins",
                "/api/mobile/capabilities/mcp-servers",
                "/api/mobile/capabilities/projects",
                "/api/mobile/capabilities/skills",
                "/api/mobile/capabilities/all",
            ],
        },
    ]


async def _safe_discovery(fn: Any, *args: Any) -> Any:
    return await _in_thread(fn, *args)


def _failure(what: str) -> web.Response:
    return _json_response({"ok": False, "error": f"Unable to load {what}"}, status=500)


@require_key_strict
async def _capabilities_route(request: web.Request) -> web.Response:
    try:
        caps = await _safe_discovery(_get_capabilities)
        return _json_response({"ok": True, "capabilities": caps, "count": len(caps)})
    except Exception:
        logger.exception("capability discovery failed")
        return _failure("capabilities")


@require_key_strict
async def _plugins_route(request: web.Request) -> web.Response:
    try:
        plugins = await _safe_discovery(_get_plugins)
        return _json_response({"ok": True, "plugins": plugins, "count": len(plugins)})
    except Exception:
        logger.exception("list plugins failed")
        return _failure("plugins")


@require_key_strict
async def _mcp_servers_route(request: web.Request) -> web.Response:
    try:
        servers = await _safe_discovery(_get_mcp_servers)
        return _json_response({"ok": True, "mcp_servers": servers, "count": len(servers)})
    except Exception:
        logger.exception("list MCP servers failed")
        return _failure("MCP servers")


@require_key_strict
async def _projects_route(request: web.Request) -> web.Response:
    try:
        projects = await _safe_discovery(_get_projects)
        return _json_response({"ok": True, "projects": projects, "count": len(projects)})
    except Exception:
        logger.exception("list projects failed")
        return _failure("projects")


@require_key_strict
async def _skills_route(request: web.Request) -> web.Response:
    try:
        skills = await _safe_discovery(_get_skills)
        return _json_response({"ok": True, "skills": skills, "count": len(skills)})
    except Exception:
        logger.exception("list skills failed")
        return _failure("skills")


@require_key_strict
async def _legacy_mcp_servers_route(request: web.Request) -> web.Response:
    """Older installed apps expect the key ``servers``."""
    try:
        servers = await _safe_discovery(_get_mcp_servers)
        return _json_response({"ok": True, "servers": servers, "count": len(servers)})
    except Exception:
        logger.exception("list MCP servers failed")
        return _failure("MCP servers")


@require_key_strict
async def _all_capabilities_route(request: web.Request) -> web.Response:
    try:
        plugins, servers, projects, skills = await asyncio.gather(
            _safe_discovery(_get_plugins),
            _safe_discovery(_get_mcp_servers),
            _safe_discovery(_get_projects),
            _safe_discovery(_get_skills),
        )
        return _json_response(
            {
                "ok": True,
                "plugins": plugins,
                "mcp_servers": servers,
                "projects": projects,
                "skills": skills,
                "counts": {
                    "plugins": len(plugins),
                    "mcp_servers": len(servers),
                    "projects": len(projects),
                    "skills": len(skills),
                },
            }
        )
    except Exception:
        logger.exception("combined capability discovery failed")
        return _failure("capabilities")


@require_key_strict
async def _all_route(request: web.Request) -> web.Response:
    """Legacy aggregate shape kept for older installed apps."""
    try:
        caps, servers = await asyncio.gather(
            _safe_discovery(_get_capabilities), _safe_discovery(_get_mcp_servers)
        )
        return _json_response(
            {
                "ok": True,
                "capabilities": caps,
                "mcp_servers": servers,
                "counts": {"capabilities": len(caps), "mcp_servers": len(servers)},
            }
        )
    except Exception:
        logger.exception("combined discovery failed")
        return _failure("capabilities")


def register(native_app: web.Application) -> None:
    """Attach capability routes to the api_server's web.Application."""
    routes = (
        ("/api/mobile/capabilities/plugins", _plugins_route),
        ("/api/mobile/capabilities/mcp-servers", _mcp_servers_route),
        ("/api/mobile/capabilities/projects", _projects_route),
        ("/api/mobile/capabilities/skills", _skills_route),
        ("/api/mobile/capabilities/all", _all_capabilities_route),
        ("/api/mobile/capabilities", _capabilities_route),
        ("/api/mobile/mcp-servers", _legacy_mcp_servers_route),
        ("/api/mobile/all", _all_route),
    )
    for path, handler in routes:
        native_app.router.add_get(path, handler)
    logger.info(
        "[hermes-mobile-plugin] capability routes registered: %s",
        ", ".join(path for path, _ in routes),
    )
