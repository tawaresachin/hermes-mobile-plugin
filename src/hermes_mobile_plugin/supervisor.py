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
import signal as signal_module
import time
from pathlib import Path
from typing import Final, Optional

from .constants import (
    DEFAULT_GATEWAY_PORT,
    LOGS_DIR,
    SUPERVISOR_CHECK_INTERVAL,
    SUPERVISOR_FAILURE_THRESHOLD,
    SUPERVISOR_LOG_FILE,
    SUPERVISOR_PID_FILE,
    SUPERVISOR_RESTART_DELAY,
    find_hermes_cli,
    gateway_health_url,
)

logger = logging.getLogger(__name__)

# Lock byte sits past any PID text (a PID is < 11 chars): Windows mandatory
# locks must never cover the content other processes need to read.
PID_LOCK_OFFSET: Final[int] = 32


def _detach_kwargs() -> dict:
    """Popen kwargs that detach a child from the current console/session."""
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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
        self._lock_handle = None

    def is_gateway_alive(self) -> bool:
        """Check if gateway is responding.

        HTTP 200 on /health is the ONLY liveness verdict. A raw TCP connect
        used to be an accepted fallback, but it returns True for a process
        wedged on the event loop (e.g. a blocking whisper call) — exactly the
        failure a watchdog must catch. The socket probe now runs only to
        enrich the log line, never to flip the verdict.
        """
        try:
            import urllib.request
            with urllib.request.urlopen(gateway_health_url(self.port), timeout=10) as resp:
                if resp.status == 200:
                    return True
                logger.warning("Health check returned HTTP %s", resp.status)
        except Exception as e:
            logger.debug("Health check failed: %s", e)
            # ponytail: 10s absorbs a loaded Termux probe; a wedged
            # gateway still fails the check, so no real down is
            # missed — it just takes a few extra probe cycles.
            time.sleep(5)

        # Diagnostic only: listening socket but no HTTP 200 == wedged process.
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                logger.warning(
                    "Gateway accepts TCP on %s but /health is not answering "
                    "(event loop wedged?) — treating as down", self.port)
        except Exception:
            pass
        return False

    def gateway_process_exists(self) -> bool:
        """True when ANY live hermes-gateway process holds the port.

        The supervisor only supervises a gateway IT started, so a live
        process in our own spawned set is the "alive" signal; a foreign
        hermes process on the host is ignored (we never stop/restart it).
        ponytail: /proc argv scan, single-host box — good enough here;
        upgrade to gateway.status.looks_like_gateway_command_line if it
        ever spans hosts/process managers.
        """
        spawned = getattr(self, "_spawned_pids", [])
        for pid in spawned:
            pid_dir = Path("/proc", str(pid))
            if pid_dir.exists():
                try:
                    argv = pid_dir.joinpath("cmdline").read_bytes().replace(b"\0", b" ")
                except OSError:
                    argv = b""
                if b"gateway" in argv and b"run" in argv:
                    return True
        # Boot-time gateway we did not spawn ourselves: trust it too, via
        # pid-file or the /proc scan of the host's `hermes gateway run`.
        for pid_dir in Path("/proc").glob("[0-9]*"):
            try:
                argv = pid_dir.joinpath("cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if b"gateway" in argv and b"run" in argv and b"hermes" in argv:
                return True
        return False

    def restart_gateway(self) -> bool:
        """Attempt to restart the Hermes Agent gateway.

        Returns:
            True if restart initiated successfully, False otherwise.
        """
        logger.warning("Gateway is down. Attempting restart...")

        try:
            hermes_bin = find_hermes_cli()
            if not hermes_bin:
                logger.error("Cannot restart gateway: 'hermes' not found on PATH")
                return False

            # Stop existing gateway. A WEDGED gateway can hang even the
            # host's own stop (observed: pre-config zombie ignoring kill
            # until taskkill /F) — bound it and fail with a clear hint
            # instead of surfacing a bare timeout.
            try:
                subprocess.run(
                    [hermes_bin, "gateway", "stop"],
                    timeout=10,
                    capture_output=True,
                )
            except subprocess.TimeoutExpired:
                logger.error(
                    "'hermes gateway stop' timed out after 10s — the "
                    "running gateway appears wedged. Kill it manually "
                    "(taskkill /F /PID <pid> / Process Explorer) before "
                    "the watchdog can restart it."
                )
                return False
            time.sleep(2)

            # Port-ownership guard: after an authoritative 'gateway stop',
            # something still listening on the port is NOT ours (Windows
            # iphlpsvc serves netsh portproxy rules; also VPN/proxy tools).
            # Spawning into it bind-fails and leaves a zombie that health
            # checks then restart-loop against forever — observed live when a
            # stale WSL portproxy held 8642 on svchost.
            if self._port_listening(self.port):
                logger.error(
                    "Port %s is still occupied after 'hermes gateway stop' — "
                    "another process or service owns it (VPN/proxy/WSL "
                    "port-forward?). NOT spawning a gateway that cannot "
                    "bind; free the port or set "
                    "platforms.api_server.extra.port.",
                    self.port,
                )
                return False

            # Start new gateway detached from this process group (POSIX: new
            # session; Windows: DETACHED_PROCESS so it survives us).
            proc = subprocess.Popen(
                [hermes_bin, "gateway", "run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_detach_kwargs(),
            )
            logger.info("Spawned gateway process (PID: %s)", proc.pid)
            # Remember what we spawned; process-exists checks must only
            # trust gateways WE started — a user-run gateway (different
            # port, manual launch) must never be judged "ours".
            self._spawned_pids = getattr(self, "_spawned_pids", []) + [proc.pid]
            return True

        except Exception as e:
            logger.error("Failed to restart gateway: %s", e)
            return False

    @staticmethod
    def _port_listening(port: int, timeout: float = 3.0) -> bool:
        """True when ANY process accepts TCP on 127.0.0.1:port."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    def _write_pid(self) -> None:
        """Acquire an exclusive flock on the PID file and stamp our PID.

        The lock, not the file contents, is the single-instance guarantee:
        the old read-then-write check was TOCTOU (a gateway restart racing
        `cli install` could spawn two watchdogs whose restart storms fought
        each other). flock dies with the process, so a killed watchdog frees
        the lock instantly. Returns silently-alive if someone else holds it.
        """
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock_handle = open(SUPERVISOR_PID_FILE, "a+", encoding="utf-8")
        try:
            self._acquire_file_lock(self._lock_handle)
        except OSError:
            self._lock_handle.close()
            self._lock_handle = None
            logger.info("Another supervisor holds the lock; exiting")
            self._running = False
            return
        # Only the lock holder writes — no TOCTOU.
        self._lock_handle.seek(0)
        self._lock_handle.truncate()
        self._lock_handle.write(str(os.getpid()))
        self._lock_handle.flush()

    @staticmethod
    def _acquire_file_lock(handle) -> None:
        """Exclusive non-blocking lock on the PID file (raises OSError if
        held). fcntl on POSIX, msvcrt byte-range lock on Windows; no-op on
        platforms with neither (single-instance then rests on the PID probe).

        Windows locks a byte at PID_LOCK_OFFSET — past any PID text — NOT
        offset 0: byte-range locks there are mandatory, so locking the text
        made the PID unreadable to every other handle (PermissionError) and
        broke stop/status while a supervisor ran. A lock past EOF still
        serializes supervisors without hiding the content. POSIX flock is
        advisory and whole-file; the offset is irrelevant there."""
        if os.name == "nt":
            import msvcrt
            handle.seek(PID_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        try:
            import fcntl
        except ImportError:
            return
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _remove_pid(self) -> None:
        """Release the lock we own (file stays; the holder is authoritative)."""
        handle = getattr(self, "_lock_handle", None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            self._lock_handle = None

    def run(self) -> None:
        """Run the supervisor main loop.

        Checks gateway health periodically and restarts if needed.
        """
        self._running = True
        self._write_pid()
        if not self._running:
            return

        # Auto-reap the gateways we spawn; without this every restart leaves
        # a <defunct> child (we are their parent) until the watchdog dies.
        # POSIX-only: Windows has no SIGCHLD and no zombie semantics.
        if hasattr(signal_module, "SIGCHLD"):
            signal_module.signal(signal_module.SIGCHLD, signal_module.SIG_IGN)

        logger.info("Gateway supervisor started (port %s)", self.port)
        # Give the gateway a moment to become reachable after startup.
        time.sleep(SUPERVISOR_CHECK_INTERVAL)
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
                        if find_hermes_cli() is None:
                            # Hermes Agent is gone — nothing to supervise.
                            logger.error(
                                "hermes binary not found; supervisor exiting"
                            )
                            break
                    if self.gateway_process_exists():
                        # Process is alive but /health is not answering: slow
                        # gateway under load / mid-turn / draining session.
                        # NEVER stop+restart it — that SIGTERMs the live
                        # gateway and every active task dies with a
                        # "Hermes is shutting down" notice to each chat.
                        # Just keep probing; only a GONE process restarts.
                        logger.warning(
                            "Gateway process alive but /health silent — "
                            "treating as slow, not restarting"
                        )
                        time.sleep(SUPERVISOR_RESTART_DELAY)
                        continue
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
                        # Give the freshly started gateway a moment before next health check.
                        time.sleep(SUPERVISOR_CHECK_INTERVAL)
                        continue
                    else:
                        # Restart refused (port owned by another service,
                        # wedged gateway, spawn error): back off the same way
                        # so a blocked port cannot be hammered every 10s.
                        failed_restarts += 1
                        consecutive_failures = 0
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
    if os.name == "nt":
        # fork/setsid/dup2 are POSIX. Windows: spawn the same detached
        # re-exec that ensure_running() uses (stdout/stderr already wired
        # to the log file there).
        return ensure_running()
    # Check if already running
    if SUPERVISOR_PID_FILE.exists():
        try:
            old_pid = int(SUPERVISOR_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = 0
        if old_pid and pid_alive(old_pid):
            logger.warning("Supervisor already running (PID: %s)", old_pid)
            return old_pid
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


def pid_alive(pid: int) -> bool:
    """Liveness probe WITHOUT signalling. POSIX: kill(pid,0). Windows:
    os.kill there would send CTRL_C_EVENT for 0 — OpenProcess+GetExitCode
    instead."""
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def terminate_pid(pid: int) -> bool:
    """Ask a process to die (supervisor stop)."""
    if os.name == "nt":
        import ctypes
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 15))
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 15)
        return True
    except (ProcessLookupError, OSError):
        return False


def read_supervisor_pid() -> int:
    """PID from the PID file; 0 when absent/garbage, -1 when unreadable.

    -1 means a legacy supervisor holds a mandatory lock over the text
    (pre-0.0.11 layout) — i.e. RUNNING but PID unknown; callers fall back
    to find_supervisor_pids()."""
    if not SUPERVISOR_PID_FILE.exists():
        return 0
    try:
        return int(SUPERVISOR_PID_FILE.read_text().strip())
    except PermissionError:
        return -1
    except (ValueError, OSError):
        return 0


def find_supervisor_pids() -> list[int]:
    """Supervisor daemon PIDs via process enumeration — fallback when the
    PID file is unreadable (legacy lock) or stale.

    Matches only genuine `python -m hermes_mobile_plugin.supervisor` launches
    (a bare substring match also catches shells whose command text merely
    MENTIONS the module — that terminated our own caller once). Windows also
    excludes the querying process's full ancestor chain: those command lines
    can legitimately contain the module string (e.g. a shell running this
    very stop command) and must never be terminated."""
    if os.name == "nt":
        ps_script = (
            "$ex = New-Object System.Collections.Generic.HashSet[uint32]; "
            "$cur = $PID; "
            "for ($i = 0; $i -lt 10 -and $cur; $i++) { "
            "$p = Get-CimInstance Win32_Process -Filter (\"ProcessId = $cur\"); "
            "if (-not $p) { break }; "
            "[void]$ex.Add([uint32]$p.ProcessId); $cur = $p.ParentProcessId }; "
            "Get-CimInstance Win32_Process "
            "-Filter \"CommandLine LIKE '% -m hermes_mobile_plugin.supervisor%'\" "
            "| Where-Object { -not $ex.Contains([uint32]$_.ProcessId) } "
            "| Select-Object -ExpandProperty ProcessId"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=20,
            ).stdout
            return [int(x) for x in out.split() if x.strip().isdigit()]
        except Exception:
            return []
    try:
        out = subprocess.run(
            ["pgrep", "-f", "python.* -m hermes_mobile_plugin[.]supervisor"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def is_supervisor_running() -> bool:
    """True if a supervisor daemon is alive (per PID file).

    Windows: a LIVE supervisor holds an msvcrt lock on the PID file, and its
    open handle denies other handles read access — so read_text() raising
    PermissionError means "running", not "broken". The old code treated it
    as corrupt, then crashed inside its own cleanup when unlink() hit the
    same lock (WinError 32). When the file can be read but is garbage or its
    PID is dead, unlink() failures there are likewise non-fatal: another
    process mid-cleanup must not turn our answer into a crash."""
    if not SUPERVISOR_PID_FILE.exists():
        return False
    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
    except PermissionError:
        return True  # locked by a live holder
    except (ValueError, OSError):
        try:
            SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        except OSError:
            return True  # cannot clear it; assume live rather than double-spawn
        return False
    if not pid_alive(pid):
        try:
            SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


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
        detach = _detach_kwargs()
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
            cwd=os.path.abspath(os.sep),  # "/" POSIX, drive root Windows
            env=env,
            close_fds=True,
            **detach,
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