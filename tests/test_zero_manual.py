"""Zero-manual install features: API-key seeding, runtime-venv detection,
live port resolution and the supervised-restart plumbing.

These pin the contracts added in 0.0.19 — the bug that motivated the
feature (a gateway running from an isolated bootstrap venv that a plain
toolchain ``pip install`` never reached) plus the two user-facing
consequences: pairing without a key, and the supervisor silently watching
the default port on a host that moved it.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _make_venv(tree: Path, name: str = "venv") -> Path:
    """Create a venv-looking dir with a bin/python file (a POSIX test
    stand-in: detection only ever checks the path shape, never executes
    it here — the real-execution test uses ensure_runtime_venv below)."""
    venv = tree / name
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return venv


# ---------------------------------------------------------------------------
# Feature 1: key seeding
# ---------------------------------------------------------------------------

class TestKeySeeder:
    def test_seeds_config_and_env_when_absent(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({"platforms": {"api_server": {"enabled": True}}}),
            encoding="utf-8",
        )
        env_path = tmp_path / ".env"
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        report = key_seeder.seed_api_key(config={"platforms": {"api_server": {"enabled": True}}})

        assert report["seeded_config"] is True
        assert report["seeded_env"] is True
        key = report["key"]
        assert key and len(key) >= key_seeder.MIN_KEY_LENGTH

        # config.yaml: key landed, every other top-level key survived
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["platforms"]["api_server"]["extra"]["key"] == key
        assert on_disk["platforms"]["api_server"]["enabled"] is True
        # .env: single API_SERVER_KEY line with the SAME key (one secret,
        # two targets — they can never disagree)
        env_text = env_path.read_text(encoding="utf-8")
        assert f"API_SERVER_KEY={key}" in env_text
        assert env_text.count("API_SERVER_KEY=") == 1
        # secret file perms (POSIX only — NTFS has no chmod, so on
        # Windows the mode comes back as the filesystem default; the
        # seeder's chmod there is best-effort and must not break install)
        if os.name != "nt":
            assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        # .bak backup written before the first mutation
        assert (tmp_path / "config.yaml.bak").is_file()

    def test_existing_strong_key_is_never_clobbered(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        strong = "a" * 64
        config_path.write_text(
            yaml.dump({"platforms": {"api_server": {"extra": {"key": strong}}}}),
            encoding="utf-8",
        )
        env_path = tmp_path / ".env"
        env_path.write_text(f"# existing stuff\nAPI_SERVER_KEY={strong}\n", encoding="utf-8")
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        report = key_seeder.seed_api_key(config=None, env_path=env_path)

        # Nothing written, and the report surfaces the EXISTING key — the
        # user's secret is never regenerated or lost.
        assert report["seeded_config"] is False
        assert report["seeded_env"] is False
        assert report["key"] == strong
        assert strong in config_path.read_text(encoding="utf-8")
        env_lines = env_path.read_text(encoding="utf-8")
        assert f"API_SERVER_KEY={strong}" in env_lines
        assert "# existing stuff" in env_lines  # user content preserved

    def test_weak_env_key_replaced_by_strong_config_key(self, tmp_path, monkeypatch):
        """Never clobber: when config has the only strong key, it is COPIED
        into .env (replacing the weak line) instead of generating a new
        secret the user didn't ask for."""
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        strong = "b" * 64
        config_path.write_text(
            yaml.dump({"platforms": {"api_server": {"extra": {"key": strong}}}}),
            encoding="utf-8",
        )
        env_path = tmp_path / ".env"
        env_path.write_text("OTHER=1\nAPI_SERVER_KEY=short\n", encoding="utf-8")
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        report = key_seeder.seed_api_key(config=None, env_path=env_path)

        assert report["key"] == strong
        assert report["seeded_env"] is True
        env_text = env_path.read_text(encoding="utf-8")
        assert f"API_SERVER_KEY={strong}" in env_text
        assert "API_SERVER_KEY=short" not in env_text
        assert "OTHER=1" in env_text  # unrelated lines untouched

    def test_weak_config_key_replaced_by_strong_env_key(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({"platforms": {"api_server": {"extra": {"key": "tiny"}}}}),
            encoding="utf-8",
        )
        env_path = tmp_path / ".env"
        strong = "c" * 64
        env_path.write_text(f"API_SERVER_KEY={strong}\n", encoding="utf-8")
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        report = key_seeder.seed_api_key(config=None, env_path=env_path)

        assert report["key"] == strong
        assert report["seeded_config"] is True
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["platforms"]["api_server"]["extra"]["key"] == strong
        # .env untouched (its key was the source of truth)
        assert f"API_SERVER_KEY={strong}" in env_path.read_text(encoding="utf-8")

    def test_missing_env_file_is_created(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        env_path = tmp_path / ".env"
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        report = key_seeder.seed_api_key(config={}, env_path=env_path)
        assert env_path.is_file()
        assert f"API_SERVER_KEY={report['key']}" in env_path.read_text(encoding="utf-8")

    def test_backup_written_once_not_clobbered_on_rerun(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        original = {"user_data": {"kept": True}}
        config_path.write_text(yaml.dump(original), encoding="utf-8")
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        key_seeder.seed_api_key(config={}, env_path=tmp_path / ".env")
        first_bak = config_path.with_name("config.yaml.bak").read_text(encoding="utf-8")
        key_seeder.seed_api_key(config=None, env_path=tmp_path / ".env")
        # .bak still holds the ORIGINAL pre-seed content, not the re-seeded one
        assert yaml.safe_load(first_bak) == original

    def test_env_reader_ignores_comments_and_quotes(self, tmp_path):
        from hermes_mobile_plugin import key_seeder

        env = tmp_path / ".env"
        env.write_text(
            "# API_SERVER_KEY=commented\n"
            "OTHER=1\n"
            'API_SERVER_KEY="quoted_value_here"\n',
            encoding="utf-8",
        )
        assert key_seeder.existing_env_key(env) == "quoted_value_here"
        env2 = tmp_path / "missing"
        assert key_seeder.existing_env_key(env2) is None


# ---------------------------------------------------------------------------
# Feature 2: runtime-venv detection
# ---------------------------------------------------------------------------

class TestRuntimeVenvDetection:
    @pytest.fixture(autouse=True)
    def _isolate_pidfile(self, tmp_path, monkeypatch):
        """Make detection deterministic: no real gateway.pidfile on the
        test host may feed /proc/<pid>/exe into the lookup."""
        import hermes_mobile_plugin.constants as c
        monkeypatch.setattr(c, "GATEWAY_PID_FILE", tmp_path / "no-such-gateway.pid")

    def test_no_installs_tree_returns_none(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import envdetect

        monkeypatch.setattr(envdetect, "INSTALLS_DIR", tmp_path / "installs")
        assert envdetect.find_runtime_venv() is None
        # The installer bridge degrades to a one-line warning, never fails.
        msg = envdetect.ensure_runtime_venv()
        assert "⚠️" in msg or "No isolated runtime venv" in msg

    def test_scan_picks_newest_mtime_venv(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import envdetect

        installs = tmp_path / "installs"
        old = _make_venv(installs / "aaaa" / "environments" / "1111")
        new = _make_venv(installs / "aaaa" / "environments" / "2222")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.setattr(envdetect, "INSTALLS_DIR", installs)
        monkeypatch.setattr(envdetect, "HERMES_HOME", tmp_path)
        monkeypatch.setattr(envdetect, "_gateway_exe", lambda: None)  # scan-only
        assert envdetect.find_runtime_venv() == new

    def test_gateway_exe_inside_venv_wins_over_scan(self, tmp_path, monkeypatch):
        """Linux pidfile path: /proc/<pid>/exe pointing into an OLDER venv
        must beat the newest-mtime scan (authoritative, not a guess)."""
        from hermes_mobile_plugin import envdetect

        installs = tmp_path / "installs"
        old = _make_venv(installs / "hh" / "environments" / "1111")
        new = _make_venv(installs / "hh" / "environments" / "2222")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))

        fake_proc = tmp_path / "proc" / "4663"
        fake_proc.mkdir(parents=True)
        exe = fake_proc / "exe"
        exe.symlink_to(old / "bin" / "python")
        # Mirror the real _gateway_exe: resolve the /proc symlink to its target.
        target = os.path.realpath(str(exe))
        monkeypatch.setattr(envdetect, "INSTALLS_DIR", installs)
        monkeypatch.setattr(envdetect, "HERMES_HOME", tmp_path)
        monkeypatch.setattr(envdetect, "_gateway_exe", lambda: target)
        assert envdetect.find_runtime_venv() == old

    def test_runtime_venv_inside_plugin_dir_is_excluded(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import envdetect

        monkeypatch.setattr(envdetect, "HERMES_HOME", tmp_path)
        installs = tmp_path / "installs"
        _make_venv(installs / "x" / "environments" / "1")
        plugins_venv = _make_venv(tmp_path / "plugins" / "hermes-mobile-qr" / "venv")
        monkeypatch.setattr(envdetect, "INSTALLS_DIR", installs)
        found = envdetect.find_runtime_venv()
        assert found is not None
        assert found != plugins_venv

    def test_venv_module_probe_is_honest(self, monkeypatch):
        """venv_has_module must reflect the VENV's own site, not the
        caller's sys.path (an -I probe). Probing the TEST runner's own
        interpreter: 'sys' always imports; a bogus module never does."""
        import sys
        from hermes_mobile_plugin import envdetect

        monkeypatch.setattr(envdetect, "_venv_pip", lambda v: Path(sys.executable))
        probe = type("P", (), {"_py": sys.executable})
        assert envdetect.venv_has_module(probe, "sys") is True
        # The negative probe: a module that cannot exist anywhere.
        assert envdetect.venv_has_module(probe, "definitely_not_a_module_xyz") is False

    def test_same_interpreter_as_caller(self, monkeypatch):
        import sys
        from hermes_mobile_plugin import envdetect

        monkeypatch.setattr(envdetect, "_venv_pip", lambda v: Path(sys.executable))
        assert envdetect.same_interpreter_as_caller(object()) is True

        monkeypatch.setattr(
            envdetect,
            "_venv_pip",
            lambda v: Path("/nonexistent/venv/bin/python"),
        )
        assert envdetect.same_interpreter_as_caller(object()) is False

    def test_ensure_importable_real_venv_end_to_end(self, tmp_path):
        """End-to-end on a REAL venv: the venv's own pip installs a
        package (ensurepip bootstraps if pip is missing) and the module
        becomes importable from that venv — the exact live-machine fix."""
        if os.name == "nt" or sys.version_info < (3, 7):
            pytest.skip("venv/ensurepip not available here")
        from hermes_mobile_plugin import envdetect

        venv = tmp_path / "runtime-venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
        )
        real_py = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
        if not real_py.exists():
            pytest.skip("real venv python missing")

        # A trivial pip-installable package as the "source"
        pkg = tmp_path / "pkg"
        (pkg / "mytinyplugin").mkdir(parents=True)
        (pkg / "mytinyplugin" / "__init__.py").write_text("V = 1\n", encoding="utf-8")
        (pkg / "pyproject.toml").write_text(
            "[build-system]\nrequires = ['setuptools']\n"
            "build-backend = 'setuptools.build_meta'\n"
            "[project]\nname = 'mytinyplugin'\nversion = '0.1.0'\n",
            encoding="utf-8",
        )

        assert envdetect.venv_has_module(venv, "mytinyplugin") is False
        assert envdetect.ensure_importable(venv, str(pkg), "mytinyplugin") is True
        assert envdetect.venv_has_module(venv, "mytinyplugin") is True
        # idempotent second call is a fast no-op
        assert envdetect.ensure_importable(venv, str(pkg), "mytinyplugin") is True


# ---------------------------------------------------------------------------
# Feature 3: live port resolution + gateway pidfile
# ---------------------------------------------------------------------------

class TestPortResolution:
    def test_default_when_nothing_set(self, monkeypatch):
        from hermes_mobile_plugin.constants import GATEWAY_PORT, resolve_gateway_port

        monkeypatch.delenv("API_SERVER_PORT", raising=False)
        assert resolve_gateway_port({}) == GATEWAY_PORT

    def test_config_port_wins_over_env(self, monkeypatch):
        from hermes_mobile_plugin.constants import resolve_gateway_port

        monkeypatch.setenv("API_SERVER_PORT", "1111")
        cfg = {"platforms": {"api_server": {"extra": {"port": 9999}}}}
        assert resolve_gateway_port(cfg) == 9999

    def test_env_port_used_when_config_missing(self, monkeypatch):
        from hermes_mobile_plugin.constants import resolve_gateway_port

        monkeypatch.setenv("API_SERVER_PORT", "1234")
        assert resolve_gateway_port({}) == 1234

    def test_invalid_values_fall_back(self, monkeypatch):
        from hermes_mobile_plugin.constants import GATEWAY_PORT, resolve_gateway_port

        monkeypatch.setenv("API_SERVER_PORT", "not-a-number")
        cfg = {"platforms": {"api_server": {"extra": {"port": "also-bad"}}}}
        assert resolve_gateway_port(cfg) == GATEWAY_PORT

    def test_qr_url_follows_env_port(self, monkeypatch):
        from hermes_mobile_plugin.qr_generator import build_connection_config

        monkeypatch.setenv("API_SERVER_PORT", "7777")
        data = build_connection_config({}, ip_override="1.2.3.4")
        assert data["url"] == "http://1.2.3.4:7777"

    def test_gateway_pid_json_and_plain_and_garbage(self, tmp_path, monkeypatch):
        import hermes_mobile_plugin.constants as c
        pidfile = tmp_path / "gateway.pid"
        monkeypatch.setattr(c, "GATEWAY_PID_FILE", pidfile)
        assert c.gateway_pid() is None  # absent

        pidfile.write_text(json.dumps({"pid": 4663, "kind": "hermes-gateway"}), encoding="utf-8")
        assert c.gateway_pid() == 4663

        pidfile.write_text("12345\n", encoding="utf-8")
        assert c.gateway_pid() == 12345

        pidfile.write_text("garbage{", encoding="utf-8")
        assert c.gateway_pid() is None

        pidfile.write_text(json.dumps({"pid": -1}), encoding="utf-8")
        assert c.gateway_pid() is None


# ---------------------------------------------------------------------------
# Installer wiring: hermes-mobile-install never breaks on env detection
# ---------------------------------------------------------------------------

def test_install_plugin_reports_deploy_and_never_raises(tmp_path, capsys, monkeypatch):
    """The console-script path (install.sh/.bat's final step) must succeed
    even with no runtime venv present, and must still deploy the layout."""
    import argparse
    from hermes_mobile_plugin import cli
    from hermes_mobile_plugin import envdetect

    monkeypatch.setattr(envdetect, "INSTALLS_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(envdetect, "HERMES_HOME", tmp_path)
    ns = argparse.Namespace(target=str(tmp_path / "out"))
    assert cli.install_plugin(ns) == 0
    out = capsys.readouterr().out
    assert "deployed" in out.lower()
