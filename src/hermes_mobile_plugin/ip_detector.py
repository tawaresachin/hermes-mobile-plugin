"""IP address detection utilities for Hermes Mobile Plugin."""

import logging
import socket
import subprocess
from typing import Optional

from .constants import (
    NETWORK_COMMANDS,
    TAILSCALE_CLI_COMMAND,
    TAILSCALE_IP_PREFIX,
)

logger = logging.getLogger(__name__)


class IPDetectionError(Exception):
    """Raised when IP detection fails."""
    pass


def _run_command(cmd: list[str], timeout: int = 2) -> Optional[str]:
    """Run a shell command and return stdout.

    Args:
        cmd: Command and arguments.
        timeout: Timeout in seconds.

    Returns:
        Command stdout, or None if command failed.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        return result.stdout.strip() if result.stdout else None
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("Command %s failed: %s", cmd, e)
        return None


def _parse_tailscale_ip_from_output(output: str) -> Optional[str]:
    """Parse Tailscale IP from command output.

    Args:
        output: Command output text.

    Returns:
        First IP starting with Tailscale prefix, or None.
    """
    if not output:
        return None

    for line in output.splitlines():
        # Look for 'inet 100.x.x.x' pattern
        if "inet " in line and TAILSCALE_IP_PREFIX in line:
            parts = line.strip().split()
            for part in parts:
                if part.startswith(TAILSCALE_IP_PREFIX):
                    # Remove CIDR notation if present
                    return part.split("/")[0]
    return None


def get_tailscale_ip() -> str:
    """Detect Tailscale IP address with multiple fallback strategies.

    Detection order:
    1. tailscale CLI command
    2. Parse network interfaces for 100.x.x.x range
    3. Local IP via UDP socket
    4. Loopback as last resort

    Returns:
        Detected IP address string.
    """
    # Method 1: Tailscale CLI
    output = _run_command(TAILSCALE_CLI_COMMAND)
    if ip := _parse_tailscale_ip_from_output(output):
        logger.debug("Tailscale IP detected via CLI: %s", ip)
        return ip

    # Method 2: Parse network interfaces
    for cmd in NETWORK_COMMANDS:
        output = _run_command(cmd)
        if ip := _parse_tailscale_ip_from_output(output):
            logger.debug("Tailscale IP detected via %s: %s", cmd[0], ip)
            return ip

    # Method 3: UDP socket trick to get local IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 1))
            local_ip = s.getsockname()[0]
            logger.debug("Local IP detected via UDP: %s", local_ip)
            return local_ip
    except (OSError, socket.error) as e:
        logger.debug("UDP socket detection failed: %s", e)

    # Method 4: Fallback
    logger.warning("Could not detect IP address, using loopback")
    return "127.0.0.1"


def is_valid_ip(ip: str) -> bool:
    """Validate that a string is a valid IPv4 address.

    Args:
        ip: IP address string to validate.

    Returns:
        True if valid IPv4, False otherwise.
    """
    try:
        socket.inet_aton(ip)
        return True
    except (socket.error, OSError):
        return False