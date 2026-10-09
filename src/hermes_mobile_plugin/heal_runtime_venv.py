#!/usr/bin/env python3
"""Re-seed the gateway's runtime venv after `hermes update` rotates it.

`hermes update` rebuilds the isolated runtime venv
(``<HERMES_HOME>/installs/<hash>/environments/<hash>/venv``). Anything
pip-installed into the PREVIOUS venv — including this plugin, and its
qrcode/pyyaml/aiohttp deps — is gone, so the restarted gateway fails to
import ``hermes_mobile_plugin`` and every ``/api/mobile/*`` route 404s.

This script fixes it. It is deliberately STANDALONE (stdlib only, no
``hermes_mobile_plugin`` import — the package is exactly what is missing
when it runs) and best-effort: it never fails the update chain, only logs.

The apply route in ``update_routes.py`` runs it between ``hermes update``
and the gateway restart, i.e. after the venv has been rebuilt and before
the restarted gateway needs to import the plugin.

Steps (all idempotent — a no-op when the active venv already works):
  1. Locate the active runtime venv (newest-mtime under HERMES_HOME/installs,
     the same rule envdetect.find_runtime_venv uses).
  2. ensurepip if the venv has no pip (post-update venvs often don't).
  3. pip install the plugin's third-party deps (pyyaml, qrcode, aiohttp).
  4. Drop a .pth pointing at the deployed plugin dir so
     ``import hermes_mobile_plugin`` resolves (the deployed dir is not a
     pip project, so a .pth is the lazy equivalent of a pip install of it).
  5. Probe and report; exit 0 either way.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Third-party deps the plugin hard-imports (see pyproject.toml). hermes-agent
# itself ships aiohttp, but a rotated venv may not, so we install all three.
_PLUGIN_DEPS = ("pyyaml>=6.0", "qrcode>=7.4", "aiohttp>=3.8")
# The modules a healthy gateway must be able to import: the plugin package +
# its third-party deps. yaml (not pyyaml) and aiohttp are the import names.
_PROBE_MODULES = ("hermes_mobile_plugin", "qrcode", "yaml", "aiohttp")
PLUGIN_SLUG = "hermes-mobile-qr"


def _hermes_home() -> Path:
    # $HERMES_HOME (the gateway sets it) > hermes_constants > ~/.hermes.
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    try:
        import hermes_constants
        return Path(hermes_constants.get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _is_runtime_venv(v: Path) -> bool:
    # A runtime venv has bin/python (or Scripts/python.exe) and is NOT the
    # plugin's own deploy dir. Mirrors envdetect._is_runtime_venv.
    if not ((v / "bin" / "python").exists() or (v / "Scripts" / "python.exe").exists()):
        return False
    plugins_root = _hermes_home() / "plugins"
    try:
        v.resolve().relative_to(plugins_root)
        return False
    except (ValueError, OSError):
        return True


def _find_active_venv() -> Path | None:
    """Newest-mtime runtime venv under HERMES_HOME/installs (the one
    `hermes update` just (re)built), or None when no installs tree exists
    (standard desktop installs — nothing to heal)."""
    installs = _hermes_home() / "installs"
    if not installs.is_dir():
        return None
    cands: list[Path] = []
    for v in installs.rglob("venv"):
        if _is_runtime_venv(v):
            cands.append(v)
    # Flat layout: <install-hash>/venv/bin/python
    for p in installs.rglob("python"):
        v = p.parent.parent
        if v.name == "venv" and _is_runtime_venv(v) and v not in cands:
            cands.append(v)
    if not cands:
        return None
    try:
        cands.sort(key=lambda v: v.stat().st_mtime, reverse=True)
    except OSError:
        pass
    return cands[0]


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[heal] command failed: {' '.join(map(str, cmd))[:120]} -> {exc}")
        return None


def _venv_site_packages(venv: Path) -> Path | None:
    """The venv's site-packages via its own sysconfig (no path guessing)."""
    py = _venv_python(venv)
    if not py.exists():
        return None
    r = _run([str(py), "-I", "-c",
             "import sysconfig;print(sysconfig.get_paths()['purelib'])"], timeout=60)
    if r is None or r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return Path(out) if out else None


def _can_import(venv: Path, mods: tuple[str, ...]) -> bool:
    py = _venv_python(venv)
    if not py.exists():
        return False
    r = _run([str(py), "-I", "-c", f"import {','.join(mods)}"], timeout=60)
    return r is not None and r.returncode == 0


def _write_pth(venv: Path, target_dir: Path) -> Path | None:
    sp = _venv_site_packages(venv)
    if sp is None or not sp.is_dir():
        return None
    pth = sp / f"{PLUGIN_SLUG}.pth"
    pth.write_text(f"{target_dir}\n", encoding="utf-8")
    return pth


def heal() -> int:
    home = _hermes_home()
    deployed = home / "plugins" / PLUGIN_SLUG
    venv = _find_active_venv()

    if venv is None:
        print(f"[heal] no runtime venv under {home}/installs — nothing to do")
        return 0
    if not deployed.is_dir():
        print(f"[heal] plugin not deployed at {deployed} — nothing to heal")
        return 0
    if _can_import(venv, _PROBE_MODULES):
        print(f"[heal] active venv already healthy: {venv}")
        return 0

    py = _venv_python(venv)
    print(f"[heal] re-seeding runtime venv: {venv}")

    # 1. pip may be absent in a freshly built venv — bootstrap it.
    _run([str(py), "-m", "ensurepip"], timeout=300)
    # 2. third-party deps.
    r = _run([str(py), "-m", "pip", "install", "--quiet", *(_PLUGIN_DEPS)])
    if r is not None and r.returncode != 0:
        print(f"[heal] pip install deps failed: {r.stderr.strip()[:200]}")
    # 3. make the deployed package importable via a .pth (the deploy dir is
    #    not a pip project, so a path file is the minimal mechanism).
    pth = _write_pth(venv, deployed)
    print(f"[heal] .pth -> {deployed}: "
          f"{'written ' + str(pth) if pth else 'site-packages not found'}")

    # 4. report; exit 0 so the chain (and restart) always proceeds.
    if _can_import(venv, _PROBE_MODULES):
        print(f"[heal] OK — {venv} now imports the plugin + deps")
    else:
        print(f"[heal] WARN — {venv} still missing {list(_PROBE_MODULES)} after heal; "
              f"the next gateway start may still 404 mobile routes")
    return 0


if __name__ == "__main__":
    sys.exit(heal())
