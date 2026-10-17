"""Runtime-environment detection for the gateway host.

The hermes bootstrap installs its third-party packages into an ISOLATED
runtime venv under ``<HERMES_HOME>/installs/<hash>/environments/<hash>/venv``
— NOT into the toolchain Python that owns the ``hermes`` CLI. A plain
``pip install`` into the toolchain interpreter therefore leaves the running
gateway without the plugin (observed live: "No module named
'hermes_mobile_plugin'" until the package was also installed into the
runtime venv). This module finds that venv so installers on every OS can
install into it too. It is the single shared implementation: install.sh,
install.bat and the ``hermes-mobile-install``/``install`` CLI paths all
reuse it, so the detection logic can never drift between platforms.

All functions are pure/cheap and side-effect free (they only READ the
filesystem), so they are safe to call from inside the gateway process.
"""

import os
import re
import sys
from pathlib import Path
from typing import Final

from .constants import HERMES_HOME, PLUGIN_VERSION

# ``installs`` under the Hermes home holds one directory per toolchain
# hash, each with an ``environments/`` tree of runtime venvs.
INSTALLS_DIR: Final[Path] = HERMES_HOME / "installs"


def _is_runtime_venv(v: Path) -> bool:
    """A runtime venv is the install-time venv, not the plugin's own
    deployment directory: it has bin/python (POSIX) or Scripts/python.exe
    (Windows) and is NOT inside plugins/. Mirrors heal_runtime_venv's
    layout check so the two modules agree on what a Windows venv looks
    like — a POSIX-only probe here made find_runtime_venv() return None on
    every Windows isolated-venv host and the plugin never landed in the
    gateway's venv (its /api/mobile/* routes then 404)."""
    if not ((v / "bin" / "python").exists()
            or (v / "Scripts" / "python.exe").exists()):
        return False
    try:
        v.resolve().relative_to(HERMES_HOME / "plugins")
        return False
    except ValueError:
        return True


def _runtime_venvs() -> list[Path]:
    """Every candidate runtime venv under HERMES_HOME, newest mtime first.

    Two layouts exist: the live bootstrap layout
    (installs/<h>/environments/<h>/venv) and, on some hosts, a flat
    installs/<h>/venv. ``_is_runtime_venv`` filters out anything that is
    not a real venv or that lives inside the plugin dir."""
    candidates: list[Path] = []
    if not INSTALLS_DIR.is_dir():
        return candidates
    for env in INSTALLS_DIR.rglob("venv"):
        if _is_runtime_venv(env):
            candidates.append(env)
    for p in INSTALLS_DIR.rglob("python"):
        # Flat layout: <install-hash>/venv/bin/python
        v = p.parent.parent
        if v.name == "venv" and _is_runtime_venv(v) and v not in candidates:
            candidates.append(v)
    try:
        candidates.sort(key=lambda v: os.path.getmtime(v), reverse=True)
    except OSError:
        pass
    return candidates


def _venv_pip(venv: Path) -> Path | None:
    """The pip entry point inside a venv, per-platform (Windows uses
    Scripts/, POSIX uses bin/); None when the venv has no pip yet."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _gateway_exe() -> str | None:
    """Linux: /proc/<pid>/exe -> interpreter the gateway actually runs on.

    The gateway pidfile is JSON on live hosts ({"pid": N, "argv": [...]},
    written by the hermes bootstrap) and a bare int string otherwise —
    gateway_pid() in constants parses both. Windows/macOS have no /proc;
    this returns None there and the caller falls back to the scan."""
    from .constants import gateway_pid

    pid = gateway_pid()
    if not pid:
        return None
    exe = Path("/proc", str(pid), "exe")
    try:
        return os.path.realpath(str(exe))
    except OSError:
        return None


def _venv_from_sys_path() -> Path | None:
    """When this code runs INSIDE the gateway process (register-time, or a
    CLI launched by it), the bootstrap has already put the runtime venv's
    site-packages on sys.path. Return that venv — but ONLY one under the
    installs tree (the bootstrap layout): a venv outside it (the test
    runner's own environment, an ad-hoc venv) is not the runtime venv
    and must not shadow the scan."""
    for entry in sys.path:
        if not entry:
            continue
        # .../venv/lib/python3.14/site-packages  ->  .../venv
        # Normalize Windows backslash separators so the forward-slash regex
        # also matches a native C:\...\venv\Lib\python3.12\site-packages entry;
        # on POSIX the replace is a no-op.
        m = re.search(
            r"(.*)/(site-packages|python\d+\.\d+/site-packages)$",
            entry.replace("\\", "/"),
        )
        if not m:
            continue
        venv = Path(m.group(1))
        if not ((venv / "bin").exists() or (venv / "Scripts").exists()):
            continue
        try:
            if venv.resolve().is_relative_to(INSTALLS_DIR.resolve()):
                return venv
        except (OSError, ValueError):
            continue
    return None


def find_runtime_venv() -> Path | None:
    """Locate the gateway's runtime venv, or None when it cannot be found.

    Strategy 1 (Linux, authoritative): the gateway pidfile names the
    running gateway; ``/proc/<pid>/exe`` is the interpreter it runs on.
    Strategy 2 (in-process): if THIS process is running inside the
    runtime venv (bootstrap adds its site-packages to sys.path), that
    venv is the one — the gateway's own sys.path beats any guess.
    Strategy 3 (fallback, every OS): the most recently modified runtime
    venv under HERMES_HOME is the active one. Returns None when no
    installs/ tree exists — standard desktop installs may not use
    isolated envs, and installers must only log a one-line warning in
    that case, never fail.
    """
    exe = _gateway_exe()
    if exe:
        exep = Path(exe)
        for v in _runtime_venvs():
            try:
                exep.relative_to(v)
                return v
            except ValueError:
                continue
    venv = _venv_from_sys_path()
    if venv is not None:
        return venv
    candidates = _runtime_venvs()
    return candidates[0] if candidates else None


def venv_module_version(
    venv: Path | None, module: str = "hermes_mobile_plugin"
) -> str | None:
    """``module``'s PLUGIN_VERSION as importable from the venv, else None.

    -I (isolated) so the CALLER's sys.path cannot answer for the gateway's,
    and a pure import so it works without pip in the venv. None means the
    venv cannot import it at all. The VERSION — not mere presence — is what
    the installer must check: a venv holding an older copy answers "yes" to
    presence while the gateway keeps serving the old code (observed live:
    0.0.28 served after a 0.0.32 install)."""
    if venv is None:
        return None
    py = _venv_pip(venv)
    if py is None or not py.exists():
        return None
    import subprocess
    try:
        probe = subprocess.run(
            [str(py), "-I", "-c",
             f"from {module}.constants import PLUGIN_VERSION;print(PLUGIN_VERSION)"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return (probe.stdout or "").strip() or None


def ensure_runtime_venv() -> str:
    """Shared bridge for the shell installers (install.sh/install.bat) and
    the install CLI step: make the GATEWAY's runtime venv import THIS
    version of the plugin, and report one line.

    The deployed plugin dir is the source of truth here (the installer
    rewrites it just before this call) and is NOT a pip project, so the
    repair is heal_runtime_venv.heal() — the same code path ``hermes
    update`` uses, now version-aware so an upgrade actually lands.

    Returns the status line; the caller prints it (and exits 0 — this MUST
    never fail the install: a host without isolated envs is normal).
    """
    from . import heal_runtime_venv

    venv = find_runtime_venv()
    if venv is None:
        return (
            "⚠️  No isolated runtime venv detected under "
            f"{HERMES_HOME}/installs — nothing extra to do "
            "(standard desktop installs do not use isolated envs)"
        )
    venv = Path(venv)
    have = venv_module_version(venv)
    if have == PLUGIN_VERSION:
        return f"✅ Runtime env already has plugin v{have}: {venv}"

    heal_runtime_venv.heal(venv, expected_version=PLUGIN_VERSION)
    now = venv_module_version(venv)
    if now == PLUGIN_VERSION:
        return (
            f"✅ installed plugin v{PLUGIN_VERSION} into runtime env: {venv}"
            + (f" (replaced v{have})" if have else "")
        )
    return (
        f"⚠️  Could not update the gateway runtime env {venv} — it still "
        f"imports {('v' + have) if have else 'no plugin'}; the running "
        "gateway keeps the version it already loaded. Re-run this installer "
        "after fixing pip in that env."
    )
