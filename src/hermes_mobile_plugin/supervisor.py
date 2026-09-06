"""Gateway supervisor for Hermes Mobile Plugin.

Monitors Hermes Agent gateway health and auto-restarts if needed.
Runs as a daemon process 24/7.
"""

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .constants import (
    DEFAULT_GATEWAY_PORT,
    GATEWAY_HEALTH_CHECK_URL,
    LOGS_DIR,
    SUPERVISOR_CHECK_INTERVAL,
    SUPERVISOR_FAILURE_THRESHOLD,
    SUPERVISOR_PID_FILE,
    SUPERVISOR_RESTART_DELAY,
)

logger = logging.getLogger(__name__)


class GatewaySupervisor:
    """Monitors and restarts Hermes Agent gateway if needed."""

    def __init__(self, port: int = DEFAULT_GATEWAY_PORT):
        """Initialize supervisor.

        Args:
            port: Gateway port to monitor.
        """
        self.port = port
        self._running = False
        self._pid: Optional[int] = None

    def is_gateway_alive(self) -> bool:
        """Check if gateway is responding.

        Returns:
            True if gateway is healthy, False otherwise.
        """
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def restart_gateway(self) -> bool:
        """Attempt to restart the Hermes Agent gateway.

        Returns:
            True if restart initiated successfully, False otherwise.
        """
        logger.warning("Gateway is down. Attempting restart...")

        try:
            # Stop existing gateway
            subprocess.run(
                ["hermes", "gateway", "stop"],
                timeout=10,
                capture_output=True,
            )
            time.sleep(2)

            # Start new gateway
            env = os.environ.copy()
            env["PATH"] = (
                "/data/data/com.termux/files/home/.hermes/hermes-agent/venv/bin:"
                "/data/data/com.termux/files/usr/bin:"
                "/data/data/com.termux/files/home/.cargo/bin:"
                "/usr/bin:/bin"
            )
            proc = subprocess.Popen(
                ["hermes", "gateway"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            logger.info("Spawned gateway process (PID: %s)", proc.pid)
            return True

        except Exception as e:
            logger.error("Failed to restart gateway: %s", e)
            return False

    def _write_pid(self) -> None:
        """Write current PID to file."""
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        SUPERVISOR_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pid(self) -> None:
        """Remove PID file."""
        if SUPERVISOR_PID_FILE.exists():
            SUPERVISOR_PID_FILE.unlink()

    def run(self) -> None:
        """Run the supervisor main loop.

        Checks gateway health periodically and restarts if needed.
        """
        self._running = True
        self._write_pid()

        logger.info("Gateway supervisor started (port %s)", self.port)
        consecutive_failures = 0

        try:
            while self._running:
                if self.is_gateway_alive():
                    if consecutive_failures > 0:
                        logger.info("Gateway is healthy again")
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    logger.warning(
                        "Gateway check failed (%s/%s)",
                        consecutive_failures,
                        SUPERVISOR_FAILURE_THRESHOLD,
                    )

                    if consecutive_failures >= SUPERVISOR_FAILURE_THRESHOLD:
                        if self.restart_gateway():
                            consecutive_failures = 0
                            logger.info(
                                "Waiting %ss for gateway to stabilize...",
                                SUPERVISOR_RESTART_DELAY,
                            )
                            time.sleep(SUPERVISOR_RESTART_DELAY)
                            continue

                time.sleep(SUPERVISOR_CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Supervisor stopped by keyboard interrupt")
        except Exception as e:
            logger.error("Supervisor crashed: %s", e, exc_info=True)
        finally:
            self._remove_pid()
            logger.info("Supervisor stopped")

    def stop(self) -> None:
        """Signal supervisor to stop."""
        self._running = False


def start_daemon() -> int:
    """Start supervisor as a daemon process.

    Returns:
        Daemon PID if successful, -1 on failure.
    """
    # Check if already running
    if SUPERVISOR_PID_FILE.exists():
        try:
            old_pid = int(SUPERVISOR_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            logger.warning("Supervisor already running (PID: %s)", old_pid)
            return old_pid
        except (ProcessLookupError, ValueError):
            # Stale PID file
            SUPERVISOR_PID_FILE.unlink(missing_ok=True)

    try:
        pid = os.fork()
        if pid > 0:
            # Parent returns daemon PID
            return pid
        # Child continues as daemon
    except OSError as e:
        logger.error("Fork failed: %s", e)
        return -1

    # Set new session
    os.setsid()

    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()

    log_path = LOGS_DIR / "gateway_supervisor.log"
    devnull = open(os.devnull, 'r', encoding='utf-8')
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    log_file = open(log_path, 'a', encoding='utf-8')
    os.dup2(log_file.fileno(), sys.stdout.fileno())
    os.dup2(log_file.fileno(), sys.stderr.fileno())

    # Run supervisor
    supervisor = GatewaySupervisor()
    supervisor.run()

    return 0


if __name__ == "__main__":
    daemon_pid = start_daemon()
    if daemon_pid > 0:
        print(f"Gateway supervisor started as daemon (PID: {daemon_pid})")
        print(f"Logs: {LOGS_DIR / 'gateway_supervisor.log'}")
    else:
        print("Failed to start supervisor")
        sys.exit(1)