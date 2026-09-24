"""Constants and configuration for Hermes Mobile Plugin."""

from pathlib import Path
from typing import Final

# Plugin metadata
PLUGIN_NAME: Final[str] = "hermes-mobile-qr"
PACKAGE_NAME: Final[str] = "hermes-mobile-plugin"


def _read_plugin_version() -> str:
    """Version is read from plugin.yaml — the manifest the loader and the
    release tool both trust. Hardcoding it here is what let the two numbers
    drift (yaml 0.0.46 vs this 0.0.6). Walk up for plugin.yaml so it resolves
    from src/ checkout, installed package, or the deployed plugin dir.
    A wheel install carries no manifest, so fall back to the package
    metadata — the value pip records at build time (mirrors plugin.yaml)."""
    import re

    # OUTERMOST manifest wins: the repo-root manifest is the release source
    # of truth, but the wheel ships a bundled copy INSIDE the package, and a
    # nearest-first walk made that stale copy shadow the root (version
    # drift regressed). Deployed plugin dirs sit above their package, so
    # outermost-first is correct in all three layouts (repo, deployed,
    # wheel).
    for parent in reversed(list(Path(__file__).resolve().parents)):
        manifest = parent / "plugin.yaml"
        if manifest.is_file():
            try:
                m = re.search(
                    r"^version:\s*[\"']?([0-9][0-9A-Za-z.\-]*)[\"']?\s*$",
                    manifest.read_text(encoding="utf-8"), re.M)
                if m:
                    return m.group(1)
            except OSError:
                pass
    try:
        from importlib import metadata
        return metadata.version("hermes-mobile-plugin")
    except Exception:
        return "0.0.0+unknown"


PLUGIN_VERSION: Final[str] = _read_plugin_version()

# HTTP contract version between app and plugin — bumped ONLY when a route the
# app depends on changes shape or disappears (an API break), NEVER for feature
# releases. The app gates on this number, not on PLUGIN_VERSION, so the two
# products can version independently and forever.
#   1 = everything up to and including 0.0.6 (runs, files, voice, commands,
#       usage, model options, update, diag upload)
PROTOCOL_VERSION: Final[int] = 1

# Gateway configuration. DEFAULT_GATEWAY_PORT is the historical name kept
# as an alias; GATEWAY_PORT is the single source of truth.
GATEWAY_PORT: Final[int] = 8642
DEFAULT_GATEWAY_PORT: Final[int] = GATEWAY_PORT

def gateway_health_url(port: int = GATEWAY_PORT) -> str:
    """Health URL for the GIVEN port (the old constant hard-coded 8642, so a
    supervisor constructed with a custom port silently checked another)."""
    return f"http://127.0.0.1:{port}/health"

GATEWAY_HEALTH_CHECK_URL: Final[str] = gateway_health_url()

# Supervisor configuration
SUPERVISOR_CHECK_INTERVAL: Final[int] = 10  # seconds
SUPERVISOR_FAILURE_THRESHOLD: Final[int] = 5
SUPERVISOR_RESTART_DELAY: Final[int] = 30  # seconds

# File paths. hermes-agent is profile-aware (HERMES_HOME override /
# profiles via hermes_constants.get_hermes_home()); matching it keeps the
# plugin's config, logs, PID file and uploads under the SAME home the
# running gateway uses instead of pinning to the default one.
def _resolve_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # standalone use (CLI before hermes-agent is importable)
        return Path.home() / ".hermes"


HERMES_HOME: Final[Path] = _resolve_hermes_home()
CONFIG_PATH: Final[Path] = HERMES_HOME / "config.yaml"
PLUGINS_DIR: Final[Path] = HERMES_HOME / "plugins"
PLUGIN_DIR: Final[Path] = PLUGINS_DIR / PLUGIN_NAME
LOGS_DIR: Final[Path] = HERMES_HOME / "logs"
SUPERVISOR_LOG_FILE: Final[Path] = LOGS_DIR / "gateway_supervisor.log"
SUPERVISOR_PID_FILE: Final[Path] = LOGS_DIR / "gateway_supervisor.pid"
QR_HTML_FILE: Final[str] = "qr_code.html"

# Attachment uploads served to the mobile app
UPLOADS_DIR: Final[Path] = HERMES_HOME / "mobile-uploads"

# Tailscale configuration
TAILSCALE_IP_PREFIX: Final[str] = "100."
TAILSCALE_CLI_COMMAND: Final[list[str]] = ["tailscale", "ip", "-4"]
NETWORK_COMMANDS: Final[list[list[str]]] = [
    ["ip", "addr"],
    ["ifconfig"],
]

# QR code configuration
QR_ERROR_CORRECTION: Final[str] = "ERROR_CORRECT_L"
QR_BOX_SIZE: Final[int] = 10
QR_BORDER: Final[int] = 4

# Security
API_KEY_DISPLAY_TRUNCATION: Final[int] = 20


def find_hermes_cli() -> "str | None":
    """Path to the hermes launcher: venv bin/Scripts dir (hermes(.exe) on
    Windows) or PATH. Single source of truth for supervisor + update routes."""
    import sys
    from pathlib import Path
    from shutil import which
    bin_dir = Path(sys.executable).parent
    names = ["hermes.exe", "hermes"] if sys.platform == "win32" else ["hermes"]
    for n in names:
        cand = bin_dir / n
        if cand.exists():
            return str(cand)
    return which("hermes.exe" if sys.platform == "win32" else "hermes")
