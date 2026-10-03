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

from .constants import HERMES_HOME

# ``installs`` under the Hermes home holds one directory per toolchain
# hash, each with an ``environments/`` tree of runtime venvs.
INSTALLS_DIR: Final[Path] = HERMES_HOME / "installs"


def _is_runtime_venv(v: Path) -> bool:
    """A runtime venv is the install-time venv, not the plugin's own
    deployment directory: it has bin/python and is NOT inside plugins/."""
    if not (v / "bin" / "python").exists():
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
        m = re.search(r"(.*)/(site-packages|python\d+\.\d+/site-packages)$", entry)
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


def venv_has_module(venv: Path, module: str = "hermes_mobile_plugin") -> bool:
    """True when the venv's python can import ``module`` already.

    Imports with -I (isolated) so sitecustomize of the CALLER's
    environment cannot shadow the probe — this answers "does the GATEWAY's
    runtime venv have the plugin", not "does this shell have it". Works
    without pip installed in the venv."""
    py = _venv_pip(venv)
    if py is None or not py.exists():
        return False
    import subprocess
    try:
        probe = subprocess.run(
            [str(py), "-I", "-c", f"import {module}"],
            capture_output=True,
            timeout=60,
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def venv_has_pip(venv: Path) -> bool:
    """True when the venv's python can import pip (its own site only, via
    -I; pip is bundled with install-time venvs but a Termux/bootstrap venv
    may not have it)."""
    py = _venv_pip(venv)
    if py is None or not py.exists():
        return False
    import subprocess
    try:
        probe = subprocess.run(
            [str(py), "-I", "-c", "import pip"],
            capture_output=True,
            timeout=60,
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def bootstrap_pip(venv: Path) -> bool:
    """Ensure the venv has a usable pip: run ensurepip when it is missing
    (observed live: a runtime venv had no pip). Idempotent — a no-op when
    pip already imports."""
    if venv_has_pip(venv):
        return True
    py = _venv_pip(venv)
    if py is None:
        return False
    import subprocess
    try:
        result = subprocess.run(
            [str(py), "-m", "ensurepip"],
            capture_output=True,
            timeout=180,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_importable(venv: Path, package_source: str, module: str = "hermes_mobile_plugin") -> bool:
    """Make ``module`` importable from the venv's python by pip-installing
    ``package_source`` with the venv's own pip (bootstrap with ensurepip
    first if the venv has no pip). The final verdict probes THE GIVEN
    MODULE (default: the plugin package the gateway must load). Returns
    True when the venv can import it afterwards. Never deletes user data:
    pip install is additive/update-only."""
    if venv_has_module(venv, module):
        return True
    if not bootstrap_pip(venv):
        return False
    py = _venv_pip(venv)
    if py is None or not py.exists():
        return False
    import subprocess
    try:
        result = subprocess.run(
            [str(py), "-m", "pip", "install", "--quiet", package_source],
            capture_output=True,
            timeout=600,
        )
        return venv_has_module(venv, module)
    except (OSError, subprocess.SubprocessError):
        return False


def same_interpreter_as_caller(venv: Path) -> bool:
    """True when the venv's python is the interpreter running this code
    (a no-install case: the plugin is already here, nothing to copy)."""
    py = _venv_pip(venv)
    if py is None:
        return False
    try:
        return Path(py.resolve()).samefile(Path(sys.executable).resolve())
    except OSError:
        return False


def ensure_runtime_venv(package_source: str | None = None) -> str:
    """Shared bridge for the shell installers (install.sh/install.bat):
    print a one-line status and, when the runtime venv lacks the plugin,
    install it there. package_source is the pip-installable path of the
    package being installed (the checkout or the deployed plugin dir);
    when omitted it is derived the same way the CLI does it.

    Returns the status line; the caller prints it (and exits 0 — this
    MUST never fail the install: a host without isolated envs is normal).
    """
    if package_source is None:
        # Same derivation as the CLI: checkout root, else the deployed
        # plugin dir (importable in place), else the wheel name.
        here = Path(__file__).resolve()
        repo_root = here.parent.parent.parent
        if (repo_root / "pyproject.toml").is_file():
            package_source = str(repo_root)
        else:
            package_source = str(here.parent.parent)

    venv = find_runtime_venv()
    if venv is None:
        return (
            "⚠️  No isolated runtime venv detected under "
            f"{HERMES_HOME}/installs — nothing extra to do "
            "(standard desktop installs do not use isolated envs)"
        )
    venv = Path(venv)
    if same_interpreter_as_caller(venv) or venv_has_module(venv):
        return f"✅ Runtime env already has the plugin: {venv}"
    if ensure_importable(venv, package_source):
        return f"✅ installed into runtime env: {venv}"
    return (
        f"⚠️  Could not install into runtime env {venv} — run "
        f"`{venv}/{'Scripts' if os.name == 'nt' else 'bin'}/python -m pip install "
        f"{package_source}` manually"
    )
