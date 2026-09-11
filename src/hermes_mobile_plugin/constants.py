"""Constants and configuration for Hermes Mobile Plugin."""

from pathlib import Path
from typing import Final

# Plugin metadata
PLUGIN_NAME: Final[str] = "hermes-mobile-qr"
PLUGIN_VERSION: Final[str] = "0.0.4"
PACKAGE_NAME: Final[str] = "hermes-mobile-plugin"

# Gateway configuration. DEFAULT_GATEWAY_PORT is the historical name kept
# as an alias; GATEWAY_PORT is the single source of truth.
GATEWAY_PORT: Final[int] = 8642
GATEWAY_HEALTH_CHECK_URL: Final[str] = "http://127.0.0.1:8642/health"
DEFAULT_GATEWAY_PORT: Final[int] = GATEWAY_PORT

# Supervisor configuration
SUPERVISOR_CHECK_INTERVAL: Final[int] = 10  # seconds
SUPERVISOR_FAILURE_THRESHOLD: Final[int] = 3
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