"""Mobile Settings: skill manifest + enable/disable toggle for the app.

Why the manifest route exists: the core gateway's ``GET /v1/skills``
enumerates skills via ``tools.skills_tool._find_all_skills``. Release
0.22.2 ships a broken call site (the Collective-Wisdom revert stripped
the ``include_editorial`` kwarg from the tool but left the caller in
``api_server.py``), so the endpoint 500s and the mobile app's Settings
"Scheduled jobs & skills" panel reports "Skills (0)" even when the
machine has skills installed. This route enumerates the SAME catalog
itself, tolerating whatever signature the installed hermes release
exposes, and degrades to an honest ``{ok: false}`` 500 instead of a
silent failure the app cannot distinguish from an empty set.

  GET /api/mobile/skills          -> {ok, skills:[{name, description,
                                        category, enabled}], count}
  PUT /api/mobile/skills/toggle   {name, enabled} -> {ok, name, enabled}
      400 invalid name / essential skill, 404 unknown skill, 500 write
      failure.

Auth: Bearer key (the manifest exposes installed skill names +
descriptions; the toggle rewrites the agent's config, so it is gated
too).

The manifest lists skills INCLUDING disabled ones — the Settings
switch must be able to re-enable what the user disabled, and hiding it
is a dead end. Each entry carries its ``enabled`` state so the app's
switches seed from server truth. The manifest is cached for 60 s —
the skill scan hits the disk (``?fresh`` bypasses); a successful
toggle invalidates the cache.

The toggle rewrites the on-disk config the way the core does: the
global ``skills.disabled`` list plus the CURRENT platform's
``platform_disabled`` entry. "Disable" only needs the global list;
"enable" must clear BOTH (a skill platform-disabled stays disabled
even when the global entry is gone). Writes go through the core's
own ``save_config`` so canonicalization/stripping/locking match the
CLI, with a tolerant manual atomic-write fallback for a release where
the writer moved. The read-modify-write is serialized: a module lock
against this plugin's own concurrency, and the core's config lock when
it is present, against the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import threading
import time
from typing import Any

from aiohttp import web

from .audio_routes import _json_response, require_key

logger = logging.getLogger(__name__)

_TTL_S = 60.0
# (expiry_monotonic, payload)
_cache: tuple[float, dict] | None = None
# Serializes this plugin's read-modify-write of config.yaml against
# itself (two devices toggling in parallel must not lose an update).
_toggle_lock = threading.Lock()
# Per-thread reentrancy tracker for the cross-process config lock
# (the core's _file_lock is reentrant via holder.depth).
_config_lock_holder = threading.local()

# Skill names are flat slugs from SKILL.md frontmatter; anything else
# is not a skill name — reject it before it can reach the config file
# (no traversal-shaped or oversized input, ever).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _list_skills(including_disabled: bool = False) -> list[dict[str, Any]]:
    """The same catalog ``/skills list`` shows, tolerating the installed
    release: the functions were private in 0.22.2 (``_find_all_skills``)
    and renamed public on later trees (``find_all_skills``); pass
    ``include_editorial`` / ``skip_disabled`` only when the installed
    tool accepts them.

    ``including_disabled=True`` is the config-UI form: the toggle
    switch needs to see what the user disabled, so it can turn it
    back on."""
    from tools import skills_tool

    find_all = getattr(skills_tool, "find_all_skills", None)
    if find_all is None:
        find_all = skills_tool._find_all_skills
    sort = getattr(skills_tool, "sort_skills", None)
    if sort is None:
        sort = skills_tool._sort_skills

    params = inspect.signature(find_all).parameters
    kwargs: dict[str, Any] = {}
    if "include_editorial" in params:
        kwargs["include_editorial"] = True
    if "skip_disabled" in params:
        # Inverted name, per the core: skip_disabled=True IGNORES the
        # disabled state (lists everything, config-UI semantics).
        kwargs["skip_disabled"] = including_disabled
    return sort(find_all(**kwargs))


def _disabled_names() -> set[str]:
    """The effective disabled set (global ∪ current platform, essentials
    subtracted) — exactly what the agent would skip loading."""
    try:
        from agent.skill_utils import get_disabled_skill_names as g
        return set(g() or ())
    except Exception:
        pass
    try:
        from tools import skills_tool
        g = getattr(skills_tool, "_get_disabled_skill_names", None)
        if g is not None:
            return set(g() or ())
    except Exception:
        pass
    # Both resolution paths are gone on this release: the manifest will
    # claim every skill enabled. Say so in the log — a silent empty set
    # is indistinguishable from "nothing disabled".
    logger.warning(
        "mobile skills: no disabled-set resolver on this hermes release "
        "(agent.skill_utils.get_disabled_skill_names / "
        "skills_tool._get_disabled_skill_names missing); manifest "
        "'enabled' flags assume nothing is disabled")
    return set()


def _essential_skills() -> set[str]:
    """Skills the core refuses to treat as disabled, whatever config
    says — disabling one from the app must be an explicit error, not a
    silently-ineffective config entry."""
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
        return set(ESSENTIAL_SKILLS or ())
    except Exception:
        return set()


def _current_platform() -> str | None:
    """Same resolution the core applies when reading
    ``platform_disabled`` (``HERMES_PLATFORM`` env first, then the
    session env) — mirror it, don't guess."""
    session = None
    try:
        from gateway.session_context import get_session_env
        session = get_session_env("HERMES_SESSION_PLATFORM")
    except Exception:
        pass
    return os.environ.get("HERMES_PLATFORM") or session or None


def _parse_string_list(value: Any) -> list[str]:
    """``hermes config set`` stores lists as quoted JSON strings — a raw
    ``set(value)`` would treat one list as a single name and silently
    keep the skill disabled. Tolerate the core helper when present."""
    try:
        from agent.skill_utils import parse_config_string_list as p
        return p(value)
    except Exception:
        import ast

        if isinstance(value, str):
            if value.strip().startswith("["):
                try:
                    parsed = ast.literal_eval(value.strip())
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except (ValueError, SyntaxError):
                    pass
            return [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return [str(x) for x in value]
        return []


def _apply_toggle(name: str, enabled: bool) -> None:
    """Flip one skill's disabled state in the on-disk config.

    Runs in a worker thread (config IO). Serialization, outermost to
    innermost: module lock (plugin vs plugin), the core's cross-process
    file lock (gateway vs CLI, when the release exposes it), then the
    core's ``_CONFIG_LOCK`` (in-process, CLI vs plugin)."""
    from hermes_cli import config as c

    # Managed installs (nix/brew) make the core's save_config a silent
    # no-op (it prints to stderr and returns). Detect it up front and
    # fail with 500 instead of reporting success for a write that
    # never happened — the app rolls the switch back.
    is_managed = getattr(c, "is_managed", None)
    if callable(is_managed) and is_managed():
        raise RuntimeError(
            "managed install: config writes are read-only; "
            "update the managed Hermes installation instead"
        )

    with _toggle_lock:
        path = c.get_config_path()
        file_lock = _config_file_lock(path)
        with file_lock:
            with getattr(c, "_CONFIG_LOCK", None) or contextlib.nullcontext():
                raw = c.require_readable_config_before_write(path) or {}
                skills_cfg = raw.setdefault("skills", {})
                cur = set(_parse_string_list(skills_cfg.get("disabled")))
                plat_cfg = skills_cfg.get("platform_disabled")
                platform = _current_platform()
                plat = (
                    set(_parse_string_list(plat_cfg[platform]))
                    if isinstance(plat_cfg, dict) and platform
                    else set()
                )
                if enabled:
                    cur.discard(name)
                    plat.discard(name)
                else:
                    cur.add(name)
                    plat.add(name)
                skills_cfg["disabled"] = sorted(cur)
                if isinstance(plat_cfg, dict) and platform:
                    plat_cfg[platform] = sorted(plat)
                    if not plat:
                        plat_cfg.pop(platform, None)
                    if not plat_cfg:
                        skills_cfg.pop("platform_disabled", None)
                _persist(c, path, raw)


def _config_file_lock(path: Any):
    """The core's cross-process flock on ``<config dir>/config.yaml.lock``
    when the installed release exposes it (``hermes_cli.auth._file_lock``
    — the same primitive its own .env read-modify-write uses). Without
    it, a concurrent ``hermes config set`` process can interleave with
    our read-modify-write and lose an update; this is advisory and
    degrades to a no-op context manager on older releases."""
    from contextlib import contextmanager

    @contextmanager
    def _noop():
        yield

    try:
        from hermes_cli.auth import _file_lock as file_lock
    except Exception:
        file_lock = None
    with_name = getattr(path, "with_name", None)
    if file_lock is None or with_name is None:
        return _noop()
    # Reentrant per-thread (holder.depth), mirroring the core's usage.
    lock_path = with_name(path.name + ".lock")
    return file_lock(lock_path, _config_lock_holder, 15.0,
                     f"timed out waiting for config lock ({lock_path})")


def _persist(c: Any, path: Any, raw: dict[str, Any]) -> None:
    """Prefer the core's own writer (canonicalization + stripping +
    ``_secure_file`` match the CLI); fall back to a manual atomic write
    for a release where it moved."""
    save = getattr(c, "save_config", None)
    if save is not None:
        save(raw)
        return

    # Fallback: atomic replace, 0600 perms, flushed + fsynced BEFORE the
    # replace lands — config.yaml holds credentials, it must never be
    # left at umask perms or half-written.
    tmp = path.with_name(path.name + ".tmp-mobile")
    try:
        from ruamel.yaml import YAML  # round-trip preserves comments

        with open(tmp, "w", encoding="utf-8") as fh:
            YAML().dump(raw, fh)
            fh.flush()
            os.fsync(fh.fileno())
    except ModuleNotFoundError:
        import yaml

        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _invalidate_cache() -> None:
    global _cache
    _cache = None


@require_key
async def _skills_route(request: web.Request) -> web.Response:
    global _cache
    now = time.monotonic()
    if _cache and _cache[0] > now and not request.query.get("fresh"):
        return _json_response(_cache[1])
    try:
        raw = await asyncio.to_thread(_list_skills, True)
        disabled = await asyncio.to_thread(_disabled_names)
        payload: dict[str, Any] = {
            "ok": True,
            "skills": [
                {
                    "name": str(s.get("name", "")),
                    "description": str(s.get("description", "")),
                    "category": str(s.get("category", "")),
                    "enabled": str(s.get("name", "")) not in disabled,
                }
                for s in raw
                if s.get("name")
            ],
        }
        payload["count"] = len(payload["skills"])
    except Exception as exc:
        logger.exception("mobile skills: enumeration failed")
        return _json_response({"ok": False, "error": str(exc)}, status=500)
    _cache = (now + _TTL_S, payload)
    return _json_response(payload)


@require_key
async def _skills_toggle_route(request: web.Request) -> web.Response:
    """Flip one skill's enabled state. See the module docstring for the
    config semantics (global list + current platform entry)."""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return _json_response({"ok": False, "error": "JSON object expected"}, status=400)
    name = str(body.get("name", "")).strip()
    # Strict bool: a JSON string like "false" must not coerce to True
    # (bool("false") is True — it would disable the wrong thing).
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        return _json_response(
            {"ok": False, "error": "'enabled' must be a JSON boolean"}, status=400)
    if not _NAME_RE.match(name):
        return _json_response({"ok": False, "error": "invalid skill name"}, status=400)
    # Unknown skills would leave a dangling entry in the config; the
    # toggle list comes from the manifest, so 404 here means the app
    # saw an older manifest, or the skill was uninstalled in between.
    catalog = await asyncio.to_thread(_list_skills, True)
    if name not in {s.get("name") for s in catalog}:
        return _json_response({"ok": False, "error": f"unknown skill '{name}'"}, status=404)
    if not enabled and name in _essential_skills():
        return _json_response(
            {"ok": False, "error": f"'{name}' is an essential skill and cannot be disabled"},
            status=400,
        )
    try:
        await asyncio.to_thread(_apply_toggle, name, enabled)
    except Exception as exc:
        logger.exception("mobile skills: toggle write failed")
        return _json_response({"ok": False, "error": f"toggle failed: {exc}"}, status=500)
    _invalidate_cache()
    return _json_response({"ok": True, "name": name, "enabled": enabled})


def register(native_app: web.Application) -> None:
    """Attach the skill-manifest + toggle routes to the api_server's
    web.Application."""
    native_app.router.add_get("/api/mobile/skills", _skills_route)
    native_app.router.add_put("/api/mobile/skills/toggle", _skills_toggle_route)
    logger.info(
        "[hermes-mobile-plugin] skills routes registered: "
        "GET /api/mobile/skills, PUT /api/mobile/skills/toggle"
    )
