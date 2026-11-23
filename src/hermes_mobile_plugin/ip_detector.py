"""IP address detection utilities for Hermes Mobile Plugin.

The phone can only pair with an address that is actually reachable. A stopped
Tailscale daemon still answers ``tailscale ip -4`` with a 100.x address, so that
output is only trusted when ``tailscale status --json`` says the backend is
Running. The LAN/default-route address is always offered as a candidate.
"""

import json
import logging
import socket
import subprocess
from typing import Optional

from .constants import (
    NETWORK_COMMANDS,
    TAILSCALE_CLI_COMMAND,
    TAILSCALE_IP_PREFIX,
    TAILSCALE_STATUS_COMMAND,
)

logger = logging.getLogger(__name__)

TAILSCALE_LABEL = "Tailscale"
LAN_LABEL = "Same Wi-Fi"
LOOPBACK_LABEL = "This computer only"
LOOPBACK_IP = "127.0.0.1"


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


def _parse_tailscale_ip_from_output(output: Optional[str]) -> Optional[str]:
    """First 100.x IPv4 address in an interface dump, or None.

    Token-based on purpose: `ip addr` and `ifconfig` print
    ``inet 100.x.x.x/32`` while Windows `ipconfig` prints
    ``IPv4 Address. . . : 100.x.x.x``. Matching the address itself accepts
    all three, and the 100. prefix keeps 192.168.x.x/255.x.x.x out.

    Args:
        output: Command output text.

    Returns:
        First IPv4 address starting with the Tailscale prefix, or None.
    """
    if not output:
        return None

    for line in output.splitlines():
        # "/" splits off CIDR notation, so the bare address is its own token.
        for part in line.replace("/", " ").split():
            token = part.strip().rstrip(",;")
            if token.startswith(TAILSCALE_IP_PREFIX) and is_valid_ip(token):
                return token
    return None


def _tailscale_backend_state() -> Optional[str]:
    """BackendState from ``tailscale status --json``, or None if unavailable."""
    output = _run_command(TAILSCALE_STATUS_COMMAND, timeout=3)
    if not output:
        return None
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug("Tailscale status was not JSON: %s", e)
        return None
    state = data.get("BackendState") if isinstance(data, dict) else None
    return str(state) if state else None


def is_tailscale_running() -> bool:
    """True only when the Tailscale backend reports itself Running."""
    return _tailscale_backend_state() == "Running"


def _tailscale_ip() -> Optional[str]:
    """Live Tailscale IPv4: CLI first, then interface dumps as fallback."""
    out = (_run_command(TAILSCALE_CLI_COMMAND) or "").strip()
    if out:
        ip = out.splitlines()[0].strip().split("/")[0]
        if is_valid_ip(ip):
            return ip

    for cmd in NETWORK_COMMANDS:
        if ip := _parse_tailscale_ip_from_output(_run_command(cmd)):
            return ip
    return None


def _lan_ip() -> Optional[str]:
    """Default-route IPv4 via a UDP connect (no packet is sent).

    This is the address a phone on the same Wi-Fi/gateway can reach, and it is
    the address macOS ``ipconfig getifaddr en0`` reports on this host.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 1))
            ip = s.getsockname()[0]
    except (OSError, socket.error) as e:
        logger.debug("LAN IP detection failed: %s", e)
        return None
    if not ip or not is_valid_ip(ip) or ip.startswith("127."):
        return None
    return ip


def get_ip_candidates() -> list[dict]:
    """Reachable connection candidates, deterministic primary first.

    Returns:
        ``[{"ip": str, "kind": "tailscale"|"lan"|"loopback", "label": str}]``.
        Tailscale is included only when its backend is Running; the LAN address
        is always offered; loopback is a last resort with a warning.
    """
    candidates: list[dict] = []

    state = _tailscale_backend_state()
    ts_ip: Optional[str] = None
    if state == "Running":
        ts_ip = _tailscale_ip()
        if ts_ip:
            candidates.append({
                "ip": ts_ip, "kind": "tailscale", "label": TAILSCALE_LABEL,
            })
        else:
            logger.debug("Tailscale is Running but no IPv4 was detected")
    elif state:
        logger.debug("Tailscale backend is %s — its IP is not advertised", state)

    lan_ip = _lan_ip()
    if lan_ip and lan_ip != ts_ip:
        candidates.append({
            "ip": lan_ip, "kind": "lan", "label": LAN_LABEL,
        })

    if not candidates:
        logger.warning(
            "No routable IP detected; falling back to loopback — a phone cannot connect"
        )
        candidates.append({
            "ip": LOOPBACK_IP, "kind": "loopback", "label": LOOPBACK_LABEL,
        })

    return candidates


def get_tailscale_ip() -> str:
    """Legacy single-IP helper: the primary candidate's address.

    Kept so existing callers keep working; new code should use
    ``get_ip_candidates()`` so a stopped Tailscale address is never advertised.
    """
    return get_ip_candidates()[0]["ip"]


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
