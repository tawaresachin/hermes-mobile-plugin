"""Gateway supervisor for Hermes Mobile Plugin.

Monitors Hermes Agent gateway health and auto-restarts if needed.
Runs as a daemon process 24/7.
"""

import logging
import os
import shutil
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
    SUPERVISOR_LOG_FILE,
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
            hermes_bin = shutil.which("hermes")
            if not hermes_bin:
                # Fallback: the venv this plugin runs from usually has the launcher.
                candidate = Path(sys.executable).parent / "hermes"
                hermes_bin = str(candidate) if candidate.exists() else None
            if not hermes_bin:
                logger.error("Cannot restart gateway: 'hermes' not found on PATH")
                return False

            # Stop existing gateway
            subprocess.run(
                [hermes_bin, "gateway", "stop"],
                timeout=10,
                capture_output=True,
            )
            time.sleep(2)

            # Start new gateway detached from this process group.
            proc = subprocess.Popen(
                [hermes_bin, "gateway", "run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
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

        failed_restarts = 0
        try:
            while self._running:
                if self.is_gateway_alive():
                    if consecutive_failures > 0:
                        logger.info("Gateway is healthy again")
                    consecutive_failures = 0
                    failed_restarts = 0
                else:
                    consecutive_failures += 1
                    logger.warning(
                        "Gateway check failed (%s/%s)",
                        consecutive_failures,
                        SUPERVISOR_FAILURE_THRESHOLD,
                    )

                    if consecutive_failures >= SUPERVISOR_FAILURE_THRESHOLD:
                        if shutil.which("hermes") is None and not (
                            Path(sys.executable).parent / "hermes"
                        ).exists():
                            # Hermes Agent is gone — nothing to supervise.
                            logger.error(
                                "hermes binary not found; supervisor exiting"
                            )
                            break
                        if self.restart_gateway():
                            consecutive_failures = 0
                            failed_restarts += 1
                            # Escalating backoff: a gateway that crash-loops
                            # (bad config) must not be hammered every 30s.
                            delay = (
                                SUPERVISOR_RESTART_DELAY
                                if failed_restarts < 5
                                else SUPERVISOR_RESTART_DELAY * 10
                            )
                            logger.info(
                                "Waiting %ss for gateway to stabilize...", delay
                            )
                            time.sleep(delay)
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


def is_supervisor_running() -> bool:
    """True if a supervisor daemon is alive (per PID file)."""
    if not SUPERVISOR_PID_FILE.exists():
        return False
    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OSError):
        SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        return False


def ensure_running() -> int:
    """Spawn the detached gateway watchdog if not already running.

    Safe to call from inside the gateway (plugin register/startup): the
    child is fully detached (new session, own stdio), so it survives the
    gateway dying and can bring it back. Idempotent via the PID file.

    Returns the supervisor PID, or -1 on failure.
    """
    if is_supervisor_running():
        return int(SUPERVISOR_PID_FILE.read_text().strip())

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(SUPERVISOR_LOG_FILE, "a", encoding="utf-8")
        # Re-exec this module as __main__ with the plugin's own src dir on
        # PYTHONPATH (the package lives in ~/.hermes/plugins, not site-packages).
        pkg_root = Path(__file__).resolve().parent   # .../hermes_mobile_plugin
        env = dict(os.environ)
        env["PYTHONPATH"] = (
            str(pkg_root.parent)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_mobile_plugin.supervisor"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            cwd="/",
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        logger.info("Spawned gateway watchdog (PID %s)", proc.pid)
        return proc.pid
    except Exception as e:  # noqa: BLE001 - watchdog spawn must never break the gateway
        logger.error("Failed to spawn gateway watchdog: %s", e)
        return -1


def run_foreground() -> None:
    """Daemon main: single-instance guard, PID file, then the supervise loop."""
    if is_supervisor_running():
        return
    supervisor = GatewaySupervisor()
    supervisor.run()


if __name__ == "__main__":
    # Launched detached by ensure_running(): stdio already wired to the log.
    run_foreground()