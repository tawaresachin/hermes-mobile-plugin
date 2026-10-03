"""API-key auto-seeding at install time.

The mobile app authenticates with ``platforms.api_server.extra.key``;
without it pairing fails, and the api_server platform stays off without
the ``API_SERVER_KEY`` entry in the Hermes ``.env`` (the documented
enable path — the config key alone does not flip the platform on). This
module generates the key once and writes it to BOTH, idempotently and
never clobbering user data:

* ``config.yaml`` — load/modify/save with pyyaml; a ``.bak`` backup is
  written before the first mutation (comments are lost; all user keys
  survive the round-trip).
* ``.env`` — ``API_SERVER_KEY=<key>`` is appended only when no such
  line exists yet.

All functions return a report instead of raising, so installers can
never die in this step.
"""

import logging
import secrets
from pathlib import Path
from typing import Final

from .constants import CONFIG_PATH

# Hermes gates api_server enablement on API_SERVER_KEY in .env; a key
# shorter than 16 chars is treated as unset, so a seeded key must beat
# that. 48 chars of token_urlsafe gives ~324 bits of entropy.
MIN_KEY_LENGTH: Final[int] = 16
SEED_KEY_LENGTH: Final[int] = 48
ENV_KEY_LINE: Final[str] = "API_SERVER_KEY="

logger = logging.getLogger(__name__)


def _env_path_for(config_path: Path) -> Path:
    """The .env next to the config.yaml (the Hermes agent's .env path)."""
    return config_path.parent / ".env"


def existing_config_key(config: dict) -> str | None:
    """platforms.api_server.extra.key from a loaded config dict (or None)."""
    platforms = config.get("platforms") or {}
    if not isinstance(platforms, dict):
        return None
    api_server = platforms.get("api_server") or {}
    if not isinstance(api_server, dict):
        return None
    extra = api_server.get("extra") or {}
    if not isinstance(extra, dict):
        return None
    key = extra.get("key")
    return key if isinstance(key, str) and key else None


def existing_env_key(env_path: Path) -> str | None:
    """API_SERVER_KEY from the .env file, or None when absent/not set.

    Only an ``API_SERVER_KEY=`` line counts; comment lines and other env
    vars are ignored. Never raises on a missing file."""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(ENV_KEY_LINE):
            value = line[len(ENV_KEY_LINE):].strip().strip('"').strip("'")
            return value or None
    return None


def key_is_weak(key: str | None) -> bool:
    """Missing or too short to gate api_server (see MIN_KEY_LENGTH)."""
    return not key or len(key) < MIN_KEY_LENGTH


def _load_config_dict(config_path: Path) -> dict:
    """config.yaml as a plain dict (never raises; {} when absent)."""
    try:
        import yaml
        if yaml is None or not config_path.exists():
            return {}
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - install must never die here
        return {}


def _save_config_dict(data: dict, config_path: Path) -> None:
    """Round-trip the existing top-level structure back to config.yaml
    and keep a .bak of what was there. The backup is written ONCE,
    before the first mutation, so re-runs never clobber it."""
    import yaml
    if not isinstance(data, dict):
        data = {}
    backup = config_path.with_name(config_path.name + ".bak")
    if config_path.exists() and not backup.exists():
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def _ensure_config_key_dict(cfg: dict, key: str) -> None:
    """Nest platforms.api_server.extra.key in-place."""
    platforms = cfg.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
        cfg["platforms"] = platforms
    api_server = platforms.get("api_server")
    if not isinstance(api_server, dict):
        api_server = {}
        platforms["api_server"] = api_server
    extra = api_server.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        api_server["extra"] = extra
    extra["key"] = key


def _write_env_key(env_path: Path, key: str) -> None:
    """Put API_SERVER_KEY=<key> into .env: replace an existing (weak) line
    in place, append when absent. Creates the file with 0600 (it holds a
    secret); never touches any other line the user has in there."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = existing_env_key(env_path)
    if existing is not None:
        # An API_SERVER_KEY= line exists but was weak (short) — replace
        # exactly that line, preserve everything else byte-for-byte.
        lines = env_path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(ENV_KEY_LINE):
                lines[i] = f"{ENV_KEY_LINE}{key}"
                break
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    needs_newline = env_path.exists() and not _ends_with_newline(env_path)
    try:
        with open(env_path, "a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write(f"{ENV_KEY_LINE}{key}\n")
    except OSError:
        prefix = ""
        try:
            content = env_path.read_text(encoding="utf-8")
            if content and not content.endswith("\n"):
                prefix = "\n"
        except OSError:
            prefix = ""
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(prefix + f"{ENV_KEY_LINE}{key}\n")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def _ends_with_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(-1, 2)
            return f.read(1) == b"\n"
    except OSError:
        return False


def seed_api_key(
    config: dict | None = None,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> dict:
    """Seed a strong API key into config.yaml AND the .env file.

    Args:
        config: loaded config dict (None = load it from config_path).
        config_path: where config.yaml lives (None = the standard path).
        env_path: where the .env lives (None = beside the config).

    Returns a report::

        {"key": str,             # the key that is now in effect (the one
                                 # generated, or copied from the strong
                                 # side when only one side had it)
         "seeded_config": bool,  # True when config.yaml was written
         "seeded_env": bool,     # True when .env was written
         "reasons": [str]}      # what happened and why, for the UI

    Idempotent and non-clobbering: a key that is at least
    MIN_KEY_LENGTH chars long is NEVER regenerated. When only one side
    has a strong key, it is COPIED to the other side (so the two can
    never disagree); only when neither side has one is a new key
    generated and written to both.
    """
    cfg_path = config_path or CONFIG_PATH
    if env_path is None:
        env_path = _env_path_for(cfg_path)

    cfg = config
    if cfg is None:
        cfg = _load_config_dict(cfg_path)
    cfg_key = existing_config_key(cfg)
    env_key = existing_env_key(env_path)

    need_config = key_is_weak(cfg_key)
    need_env = key_is_weak(env_key)
    reasons: list[str] = []

    if not need_config and not need_env:
        return {
            "key": cfg_key,
            "seeded_config": False,
            "seeded_env": False,
            "reasons": ["existing API key is fine (config + .env both set)"],
        }

    if need_config and not need_env:
        # .env has the only strong key: copy it into the config, don't
        # generate a new one that would clobber the user's existing
        # secret.
        key = env_key
        _ensure_config_key_dict(cfg, key)
        _save_config_dict(cfg, cfg_path)
        reasons.append("copied the existing .env key into config.yaml")
    elif need_env and not need_config:
        # config has the only strong key: copy it into .env (replacing
        # the weak line there), so the api_server enable path agrees
        # with what the gateway authenticates against.
        key = cfg_key
        _write_env_key(env_path, key)
        reasons.append("copied the existing config key into .env "
                       "(the api_server enable path)")
    else:
        # Neither location has a strong key: generate one and write it to
        # BOTH, so config and .env can never disagree about the secret.
        key = secrets.token_urlsafe(SEED_KEY_LENGTH)
        _ensure_config_key_dict(cfg, key)
        _save_config_dict(cfg, cfg_path)
        _write_env_key(env_path, key)
        reasons.append("generated a new API key and wrote it to "
                       "config.yaml + .env")

    logger.info("API key seed report: %s", "; ".join(reasons))
    return {
        "key": key,
        "seeded_config": need_config,
        "seeded_env": need_env,
        "reasons": reasons,
    }


def print_seed_report(report: dict, step_label: str = "API key") -> None:
    """Human-readable summary of what seeding did (or deliberately did not)."""
    if not report["seeded_config"] and not report["seeded_env"]:
        print(f"   ✅ {report['reasons'][0]}")
        return
    for reason in report["reasons"]:
        mark = "✅" if "copied" in reason or "generated" in reason else "ℹ️ "
        print(f"   {mark} {reason}")
    display = report["key"][:8]
    print(f"   🔐 {step_label} in effect: {display}... "
          "(full key in config.yaml + .env)")
