"""Configuration loader for Hermes Mobile Plugin."""

import logging
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from .constants import CONFIG_PATH

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""
    pass


def load_hermes_config() -> dict:
    """Load Hermes Agent configuration from standard location.

    Returns:
        Dictionary containing Hermes configuration, or empty dict if not found.

    Raises:
        ConfigError: If configuration file exists but is invalid.
    """
    if not CONFIG_PATH.exists():
        logger.warning("Hermes config not found at %s", CONFIG_PATH)
        return {}

    if yaml is None:
        logger.error("pyyaml not installed. Cannot load configuration.")
        return {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            if config is None:
                return {}
            return config
    except yaml.YAMLError as e:
        logger.error("Failed to parse Hermes config: %s", e)
        raise ConfigError(f"Invalid YAML in {CONFIG_PATH}: {e}") from e
    except PermissionError:
        logger.error("Permission denied reading %s", CONFIG_PATH)
        return {}
    except OSError as e:
        logger.error("Failed to read %s: %s", CONFIG_PATH, e)
        return {}


def validate_config(config: dict) -> list[str]:
    """Validate Hermes configuration for mobile plugin requirements.

    Args:
        config: Loaded Hermes configuration dictionary.

    Returns:
        List of warning messages. Empty list means config is valid.
    """
    warnings: list[str] = []

    # Check API key
    api_key = config.get("platforms", {}).get("api_server", {}).get("extra", {}).get("key")
    if not api_key:
        warnings.append(
            "No API key found in platforms.api_server.extra.key. "
            "Mobile app authentication will fail."
        )
    elif len(api_key) < 32:
        warnings.append("API key appears too short (expected 32+ characters).")

    # Check port
    port = config.get("platforms", {}).get("api_server", {}).get("extra", {}).get("port")
    if port and not isinstance(port, int):
        warnings.append(f"Invalid port value: {port} (expected integer)")

    # Check compression setting
    compression = config.get("compression", {}).get("enabled")
    if compression is not None and not isinstance(compression, bool):
        warnings.append(f"Invalid compression.enabled value: {compression} (expected boolean)")

    return warnings


def get_plugin_config(config: dict) -> dict:
    """Extract plugin-specific configuration.

    Args:
        config: Full Hermes configuration.

    Returns:
        Plugin configuration dictionary with defaults.
    """
    return config.get("plugins", {}).get("hermes-mobile-qr", {
        "auto_generate": True,
        "auto_start_supervisor": True,
    })