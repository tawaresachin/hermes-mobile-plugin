"""Constants and configuration for Hermes Mobile Plugin."""

from pathlib import Path
from typing import Final

# Plugin metadata
PLUGIN_NAME: Final[str] = "hermes-mobile-qr"
PLUGIN_VERSION: Final[str] = "0.0.1"
PACKAGE_NAME: Final[str] = "hermes-mobile-plugin"

# Gateway configuration
GATEWAY_PORT: Final[int] = 8642
GATEWAY_HEALTH_CHECK_URL: Final[str] = "http://127.0.0.1:8642/health"
DEFAULT_GATEWAY_PORT: Final[int] = 8642

# Supervisor configuration
SUPERVISOR_CHECK_INTERVAL: Final[int] = 10  # seconds
SUPERVISOR_FAILURE_THRESHOLD: Final[int] = 3
SUPERVISOR_RESTART_DELAY: Final[int] = 30  # seconds

# File paths
HERMES_HOME: Final[Path] = Path.home() / ".hermes"
CONFIG_PATH: Final[Path] = HERMES_HOME / "config.yaml"
PLUGINS_DIR: Final[Path] = HERMES_HOME / "plugins"
PLUGIN_DIR: Final[Path] = PLUGINS_DIR / PLUGIN_NAME
LOGS_DIR: Final[Path] = HERMES_HOME / "logs"
SUPERVISOR_LOG_FILE: Final[Path] = LOGS_DIR / "gateway_supervisor.log"
SUPERVISOR_PID_FILE: Final[Path] = LOGS_DIR / "gateway_supervisor.pid"
QR_HTML_FILE: Final[str] = "qr_code.html"

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