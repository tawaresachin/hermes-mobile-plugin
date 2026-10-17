"""Regression tests for the post-update runtime-venv heal step.

`hermes update` rotates the gateway's isolated runtime venv, dropping the
plugin + its pip deps, so the restarted gateway 404s every /api/mobile/*
route. The apply chain now runs heal_runtime_venv.py between the update and
the restart; these tests pin its decision paths (no-op, best-effort, and
that the wired-in step argv actually runs it).
"""
import subprocess
from pathlib import Path

from hermes_mobile_plugin import heal_runtime_venv, update_routes


def test_no_installs_tree_is_clean_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rc = heal_runtime_venv.heal()
    assert rc == 0
    assert not (tmp_path / "installs").exists()


def test_broken_venv_heals_best_effort_never_fails(tmp_path, monkeypatch):
    # Fake installs tree: a "venv" whose bin/python is not a real
    # interpreter (ensurepip/pip probes fail gracefully) + a deployed
    # plugin dir. heal() must attempt the repair, exit 0, and never raise.
    venv = tmp_path / "installs" / "h" / "environments" / "h" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 127\n")
    (venv / "bin" / "python").chmod(0o755)
    (tmp_path / "plugins" / "hermes-mobile-plugin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rc = heal_runtime_venv.heal()
    assert rc == 0  # best-effort: the update chain must proceed regardless


def test_pth_write_drops_the_pre_rename_pointer(tmp_path, monkeypatch):
    # 0.0.34 renamed the plugin; a leftover hermes-mobile-qr.pth would keep
    # the venv importing whatever sits at the old deploy dir.
    sp = tmp_path / "site-packages"
    sp.mkdir()
    (sp / "hermes-mobile-qr.pth").write_text("/old/deploy\n", encoding="utf-8")
    monkeypatch.setattr(heal_runtime_venv, "_venv_site_packages", lambda v: sp)

    pth = heal_runtime_venv._write_pth(tmp_path / "venv", tmp_path / "deploy")

    assert pth is not None and pth.name == "hermes-mobile-plugin.pth"
    assert pth.read_text(encoding="utf-8").strip() == str(tmp_path / "deploy")
    assert not (sp / "hermes-mobile-qr.pth").exists()


def test_heal_step_argv_actually_runs_the_script(monkeypatch):
    # The apply route splices _heal_step_args() into the chain; that argv
    # must be a real, executable invocation of this script (not a no-op []
    # that silently skips the heal on real hosts).
    argv = update_routes._heal_step_args()
    assert argv and Path(argv[-1]).name == "heal_runtime_venv.py"
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                       env={"HERMES_HOME": str(Path.home() / ".nonexistent-home")})
    assert r.returncode == 0
    assert "nothing to do" in r.stdout  # no installs tree under that home
