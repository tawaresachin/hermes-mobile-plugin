"""Regression tests for the scripted installers' Python deployment path.

install.bat, install.sh and the ``hermes-mobile-install`` console script
must all produce the same plugin layout; these tests pin the contract that
broke 0.0.9 (dead entry point, nested copy, stub forwarder without
register()).
"""
import argparse
import importlib
import subprocess
import sys
from pathlib import Path

from hermes_mobile_plugin import cli
from hermes_mobile_plugin.constants import PLUGIN_VERSION


def test_install_plugin_entry_point_is_importable():
    """pyproject.toml ships ``hermes-mobile-install = ...cli:install_plugin``;
    pointing a console script at a missing callable breaks the final step of
    every scripted installer with ImportError — the 0.0.9 regression."""
    module = importlib.import_module("hermes_mobile_plugin.cli")
    assert callable(getattr(module, "install_plugin", None))


def test_deploy_produces_loader_compatible_layout(tmp_path):
    target = tmp_path / "hermes-mobile-qr"
    copied = cli.deploy_plugin_files(target)

    names = {p.name for p in copied}
    assert {"hermes_mobile_plugin", "plugin.yaml", "__init__.py"} <= names

    # The real entry points live in the package; the outer forwarder must
    # re-export both. A stub without register(ctx) makes the loader skip the
    # plugin and every /api/* route 404s.
    inner = (target / "hermes_mobile_plugin" / "__init__.py").read_text(encoding="utf-8")
    assert "def register(" in inner
    assert "def create_plugin(" in inner
    forwarder = (target / "__init__.py").read_text(encoding="utf-8")
    assert "register" in forwarder and "create_plugin" in forwarder

    # Manifest version matches the constant (loader + About row trust it).
    manifest = (target / "plugin.yaml").read_text(encoding="utf-8")
    assert f'version: "{PLUGIN_VERSION}"' in manifest

    # The deployed tree must import from an arbitrary cwd, like the loader
    # does (hermes_home/plugins is NOT the repo root).
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import hermes_mobile_plugin as m; "
        "print(m.register.__name__, m.PLUGIN_VERSION)" % target
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("register ")


def test_deploy_is_idempotent(tmp_path):
    """Re-install/upgrade must REPLACE the package, not nest a stale copy
    inside itself (the cp -r bug install.sh shipped for releases)."""
    target = tmp_path / "hermes-mobile-qr"
    cli.deploy_plugin_files(target)
    stale = target / "hermes_mobile_plugin" / "stale_module.py"
    stale.write_text("# gone after redeploy\n", encoding="utf-8")
    cli.deploy_plugin_files(target)
    assert not stale.exists()
    assert (target / "hermes_mobile_plugin" / "cli.py").is_file()
    # No accidental nesting.
    assert not (target / "hermes_mobile_plugin" / "hermes_mobile_plugin").exists()


def test_install_plugin_cli_writes_target(tmp_path, capsys):
    ns = argparse.Namespace(target=str(tmp_path / "out"))
    assert cli.install_plugin(ns) == 0
    assert (tmp_path / "out" / "plugin.yaml").is_file()
    assert (tmp_path / "out" / "__init__.py").is_file()
    assert "deployed" in capsys.readouterr().out.lower()
