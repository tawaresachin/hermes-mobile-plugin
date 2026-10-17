"""Zero-manual install features: API-key seeding, runtime-venv detection,
live port resolution and the supervised-restart plumbing.

These pin the contracts added in 0.0.19 — the bug that motivated the
feature (a gateway running from an isolated bootstrap venv that a plain
toolchain ``pip install`` never reached) plus the two user-facing
consequences: pairing without a key, and the supervisor silently watching
the default port on a host that moved it.
"""
import argparse
import json
import os
import re
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
        # The bind host is seeded too: the phone dials the LAN/Tailscale
        # address the QR advertises, which a loopback-only api_server cannot
        # serve (observed live: QR said 192.168.1.10:8642, gateway bound
        # 127.0.0.1).
        assert on_disk["platforms"]["api_server"]["extra"]["host"] == "0.0.0.0"
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

        # The KEY is never regenerated and never lost — but the missing bind
        # host IS seeded, which is the one write that makes the config dirty.
        assert report["seeded_config"] is True
        assert report["seeded_env"] is False
        assert report["key"] == strong
        config_text = config_path.read_text(encoding="utf-8")
        assert strong in config_text
        assert (yaml.safe_load(config_text)["platforms"]["api_server"]["extra"]["host"]
                == "0.0.0.0")
        env_lines = env_path.read_text(encoding="utf-8")
        assert f"API_SERVER_KEY={strong}" in env_lines
        assert "# existing stuff" in env_lines  # user content preserved

    def test_user_chosen_bind_host_is_never_overridden(self, tmp_path, monkeypatch):
        """A host the user set — loopback on purpose, say — must survive, and
        a run that changes nothing must not claim it wrote the config."""
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        strong = "d" * 64
        config_path.write_text(
            yaml.dump({"platforms": {"api_server": {
                "extra": {"key": strong, "host": "127.0.0.1"}}}}),
            encoding="utf-8",
        )
        env_path = tmp_path / ".env"
        env_path.write_text(f"API_SERVER_KEY={strong}\n", encoding="utf-8")
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)

        report = key_seeder.seed_api_key(config=None, env_path=env_path)

        assert report["seeded_config"] is False
        assert report["seeded_env"] is False
        assert report["key"] == strong
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["platforms"]["api_server"]["extra"]["host"] == "127.0.0.1"

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

    def test_unreadable_config_is_never_replaced_with_a_stub(self, tmp_path, monkeypatch):
        """config.yaml that exists but is not a mapping must be left alone:
        the old loader returned {} for it, and the key-only write destroyed
        the user's whole file."""
        from hermes_mobile_plugin import key_seeder

        config_path = tmp_path / "config.yaml"
        original = "- a top-level list, not a mapping\n"
        config_path.write_text(original, encoding="utf-8")
        monkeypatch.setattr(key_seeder, "CONFIG_PATH", config_path)
        env_path = tmp_path / ".env"

        report = key_seeder.seed_api_key(env_path=env_path)

        assert report["key"] is None
        assert report["seeded_config"] is False
        assert report["seeded_env"] is False
        assert config_path.read_text(encoding="utf-8") == original
        assert not config_path.with_name("config.yaml.bak").exists()
        assert not env_path.exists()

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
        plugins_venv = _make_venv(tmp_path / "plugins" / "hermes-mobile-plugin" / "venv")
        monkeypatch.setattr(envdetect, "INSTALLS_DIR", installs)
        found = envdetect.find_runtime_venv()
        assert found is not None
        assert found != plugins_venv

    def test_venv_module_version_probe_is_honest(self, monkeypatch):
        """venv_module_version must reflect the VENV's own site, not the
        caller's sys.path (an -I probe). Probing the TEST runner's own
        interpreter: it has the plugin, so the real version comes back; a
        module that cannot exist anywhere comes back as None."""
        import sys
        from hermes_mobile_plugin import envdetect
        from hermes_mobile_plugin.constants import PLUGIN_VERSION

        monkeypatch.setattr(envdetect, "_venv_pip", lambda v: Path(sys.executable))
        probe = type("P", (), {"_py": sys.executable})
        assert envdetect.venv_module_version(probe) == PLUGIN_VERSION
        # The negative probe: a module that cannot exist anywhere.
        assert envdetect.venv_module_version(probe, "definitely_not_a_module_xyz") is None
        assert envdetect.venv_module_version(None) is None

    def test_stale_runtime_venv_is_upgraded_to_the_expected_version(
            self, tmp_path, monkeypatch):
        """End-to-end on a REAL venv: a venv that cannot import the plugin is
        re-seeded and afterwards reports the EXPECTED version — the live bug
        was a venv that kept serving 0.0.28 after a 0.0.32 install.

        Network-free deliberately: _PLUGIN_DEPS is emptied so exactly the
        version-aware decision plus the .pth repair are under test.
        """
        from hermes_mobile_plugin import envdetect, heal_runtime_venv
        from hermes_mobile_plugin.constants import PLUGIN_VERSION

        installs = tmp_path / "installs"
        venv = installs / "aaaa" / "environments" / "1111" / "venv"
        venv.parent.mkdir(parents=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)],
                       check=True, capture_output=True)
        assert envdetect.venv_module_version(venv) is None  # fresh: nothing yet

        # The "deployed" plugin as deploy_plugin_files leaves it: a plain
        # package dir (no pyproject) exposing the version the probe reads.
        home = tmp_path / "home"
        deployed = home / "plugins" / "hermes-mobile-plugin" / "hermes_mobile_plugin"
        deployed.mkdir(parents=True)
        (deployed / "__init__.py").write_text("", encoding="utf-8")
        (deployed / "constants.py").write_text(
            f'PLUGIN_VERSION = "{PLUGIN_VERSION}"\n', encoding="utf-8")

        monkeypatch.setattr(envdetect, "INSTALLS_DIR", installs)
        monkeypatch.setattr(envdetect, "HERMES_HOME", home)
        monkeypatch.setattr(heal_runtime_venv, "_hermes_home", lambda: home)
        monkeypatch.setattr(heal_runtime_venv, "_PLUGIN_DEPS", ())

        msg = envdetect.ensure_runtime_venv()

        assert PLUGIN_VERSION in msg and "⚠️" not in msg, msg
        assert envdetect.venv_module_version(venv) == PLUGIN_VERSION

    def test_stale_version_is_never_taken_as_healthy(self, tmp_path, monkeypatch, capsys):
        """heal() given an expected version must not accept a venv that
        cannot import it as "already healthy" (version-blind early exit was
        the 0.0.28-vs-0.0.32 bug)."""
        from hermes_mobile_plugin import heal_runtime_venv

        home = tmp_path / "home"
        (home / "plugins" / "hermes-mobile-plugin").mkdir(parents=True)
        fake_python = tmp_path / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

        monkeypatch.setattr(heal_runtime_venv, "_hermes_home", lambda: home)

        assert heal_runtime_venv.heal(tmp_path / "venv",
                                      expected_version="9.9.9") == 0
        out = capsys.readouterr().out
        assert "re-seeding" in out
        assert "already healthy" not in out


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


def test_enable_plugin_uses_the_hermes_cli_argv(monkeypatch):
    """Enabling goes through Hermes' own CLI — a deployed directory plugin is
    INERT until then (every /api/mobile/* route 404s) — as an argv list with
    no shell, so POSIX and Windows behave identically."""
    from hermes_mobile_plugin import cli
    from hermes_mobile_plugin.constants import PLUGIN_NAME

    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="enabled", stderr="")

    monkeypatch.setattr(cli, "find_hermes_cli", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    ok, detail = cli.enable_plugin()

    assert ok is True and detail == "enabled"
    argv, kw = calls[0]
    assert argv == ["/usr/bin/hermes", "plugins", "enable", PLUGIN_NAME]
    assert "shell" not in kw  # a list, not a shell string: Windows-safe


def test_enable_plugin_never_raises(monkeypatch):
    """A missing launcher or a failing CLI degrades to (False, why) so the
    install continues and can still print the one command the user needs."""
    from hermes_mobile_plugin import cli

    monkeypatch.setattr(cli, "find_hermes_cli", lambda: None)
    ok, detail = cli.enable_plugin()
    assert ok is False and "hermes" in detail

    monkeypatch.setattr(cli, "find_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(
                            argv, 1, stdout="", stderr="boom\nplugin not found"))
    ok, detail = cli.enable_plugin()
    assert ok is False and detail == "plugin not found"

    def explode(argv, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(cli.subprocess, "run", explode)
    assert cli.enable_plugin()[0] is False


# ---------------------------------------------------------------------------
# Feature 6: plugin naming, the pre-0.0.34 migration, and the restart decision
# ---------------------------------------------------------------------------

class TestPluginNaming:
    """The manifest ``name`` is the registry key (`hermes plugins list`,
    enable/disable) AND the directory Hermes scans for the plugin, and the
    pip distribution + console script carry the same string. 0.0.34 renamed
    it from hermes-mobile-qr, so everything must move together."""

    def test_manifest_distribution_and_deploy_dir_agree(self):
        from hermes_mobile_plugin.constants import PLUGIN_DIR, PLUGIN_NAME

        root = Path(__file__).resolve().parent.parent
        manifest = yaml.safe_load((root / "plugin.yaml").read_text(encoding="utf-8"))
        bundled = yaml.safe_load(
            (root / "src" / "hermes_mobile_plugin" / "plugin.yaml").read_text(encoding="utf-8"))
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

        assert PLUGIN_NAME == "hermes-mobile-plugin"
        assert manifest["name"] == PLUGIN_NAME
        assert bundled["name"] == PLUGIN_NAME
        assert re.search(rf'^name = "{re.escape(PLUGIN_NAME)}"', pyproject, re.M), \
            "pyproject [project].name diverges from the manifest"
        assert re.search(rf'^{re.escape(PLUGIN_NAME)} = "', pyproject, re.M), \
            "no console script named after the plugin"
        assert PLUGIN_DIR.name == PLUGIN_NAME

    def test_description_is_one_short_block(self):
        """It is rendered in `hermes plugins list`: keep it concise."""
        root = Path(__file__).resolve().parent.parent
        manifest = yaml.safe_load((root / "plugin.yaml").read_text(encoding="utf-8"))
        description = manifest["description"].strip()

        assert "\n\n" not in description  # one paragraph, not an essay
        assert len(" ".join(description.split())) <= 160


class TestLegacyInstallMigration:
    """0.0.34 renamed the plugin. Hermes keys a directory plugin by its
    manifest name, so a surviving plugins/hermes-mobile-qr registers a SECOND
    plugin whose /api/* routes collide with this one."""

    def test_removes_the_pre_rename_directory(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import cli

        legacy = tmp_path / "plugins" / "hermes-mobile-qr"
        legacy.mkdir(parents=True)
        (legacy / "plugin.yaml").write_text("name: hermes-mobile-qr\n", encoding="utf-8")
        monkeypatch.setattr(cli, "PLUGINS_DIR", tmp_path / "plugins")
        monkeypatch.setattr(cli, "find_hermes_cli", lambda: None)  # rmtree fallback

        assert cli.remove_legacy_install() is True
        assert not legacy.exists()

    def test_prefers_hermes_own_uninstall(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import cli, constants

        (tmp_path / "plugins" / "hermes-mobile-qr").mkdir(parents=True)
        monkeypatch.setattr(cli, "PLUGINS_DIR", tmp_path / "plugins")
        monkeypatch.setattr(cli, "find_hermes_cli", lambda: "/usr/bin/hermes")
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", fake_run)

        assert cli.remove_legacy_install() is True
        argv, kw = calls[0]
        assert argv == ["/usr/bin/hermes", "plugins", "uninstall",
                        constants.LEGACY_PLUGIN_NAME]
        assert "shell" not in kw  # Windows-safe list form

    def test_without_a_legacy_install_it_is_a_noop(self, tmp_path, monkeypatch):
        from hermes_mobile_plugin import cli

        monkeypatch.setattr(cli, "PLUGINS_DIR", tmp_path / "plugins")

        assert cli.remove_legacy_install() is False


class TestInstallRestart:
    """cmd_install's post-install step: a RUNNING gateway is restarted only
    when this run changed something. No test used to call cmd_install at all,
    which is how a NameError (`changed`) shipped to users."""

    @staticmethod
    def _patch(monkeypatch, *, seeded, deployed_before=None):
        from hermes_mobile_plugin import cli, envdetect

        killed = []

        async def fake_qr(*args, **kwargs):
            return "qr.html"

        monkeypatch.setattr(cli, "load_hermes_config", lambda: {})
        monkeypatch.setattr(cli, "validate_config", lambda config: [])
        monkeypatch.setattr(cli, "seed_api_key", lambda config: {
            "seeded_config": seeded, "seeded_env": False, "key": "k" * 32})
        monkeypatch.setattr(cli, "print_seed_report", lambda report: None)
        monkeypatch.setattr(cli, "remove_legacy_install", lambda: False)
        # Never read the developer's real deploy dir.
        monkeypatch.setattr(cli, "_deployed_version", lambda target: deployed_before)
        monkeypatch.setattr(cli, "deploy_plugin_files", lambda target: [])
        monkeypatch.setattr(envdetect, "find_runtime_venv", lambda: None)
        monkeypatch.setattr(envdetect, "venv_module_version", lambda venv: None)
        monkeypatch.setattr(envdetect, "ensure_runtime_venv", lambda: "   ok")
        monkeypatch.setattr(cli, "enable_plugin", lambda: (True, "enabled"))
        monkeypatch.setattr(cli, "generate_qr", fake_qr)
        monkeypatch.setattr(cli, "print_connection_details", lambda config: None)
        monkeypatch.setattr(cli, "resolve_gateway_port", lambda: 8642)
        monkeypatch.setattr(cli, "_gateway_health", lambda port: True)
        monkeypatch.setattr(cli, "gateway_pid", lambda: 4242)
        monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(cli, "is_supervisor_running", lambda: True)
        monkeypatch.setattr(cli, "read_supervisor_pid", lambda: 99)
        monkeypatch.setattr(cli, "terminate_pid", killed.append)
        monkeypatch.setattr(cli, "_wait_for_exit", lambda pid, timeout_s=90.0: True)
        monkeypatch.setattr(cli, "_wait_for_health", lambda port, timeout_s: True)
        return killed

    def test_unchanged_run_leaves_a_healthy_gateway_running(self, monkeypatch, capsys):
        from hermes_mobile_plugin import cli

        killed = self._patch(monkeypatch, seeded=False)

        assert cli.cmd_install(argparse.Namespace()) == 0
        assert killed == []
        assert "no restart needed" in capsys.readouterr().out

    def test_changed_run_restarts_the_gateway(self, monkeypatch, capsys):
        from hermes_mobile_plugin import cli

        killed = self._patch(monkeypatch, seeded=True)

        assert cli.cmd_install(argparse.Namespace()) == 0
        assert killed == [4242]

    def test_plugin_upgrade_restarts_the_gateway(self, monkeypatch, capsys):
        """The venv imports the plugin through a .pth at the deploy dir, so
        after deployment it already reports the NEW version — the pre-deploy
        version is the only signal that the running gateway is on old code."""
        from hermes_mobile_plugin import cli

        killed = self._patch(monkeypatch, seeded=False, deployed_before="0.0.1")

        assert cli.cmd_install(argparse.Namespace()) == 0
        assert killed == [4242]

    def test_same_version_reinstall_is_left_alone(self, monkeypatch, capsys):
        from hermes_mobile_plugin import cli
        from hermes_mobile_plugin.constants import PLUGIN_VERSION

        killed = self._patch(monkeypatch, seeded=False,
                             deployed_before=PLUGIN_VERSION)

        assert cli.cmd_install(argparse.Namespace()) == 0
        assert killed == []
        assert "no restart needed" in capsys.readouterr().out

    def test_changed_run_waits_for_the_old_gateway_to_exit_first(self, monkeypatch, capsys):
        """Ordering is the fix: a draining gateway still answers /health and
        still holds the port, so health must be read only after it is gone."""
        from hermes_mobile_plugin import cli

        order = []
        self._patch(monkeypatch, seeded=True)
        monkeypatch.setattr(cli, "_wait_for_exit",
                            lambda pid, timeout_s=90.0: (order.append("exit"), True)[1])
        monkeypatch.setattr(cli, "_wait_for_health",
                            lambda port, timeout_s: (order.append("health"), True)[1])

        assert cli.cmd_install(argparse.Namespace()) == 0
        assert order == ["exit", "health"]

    def test_respawn_into_a_held_port_gets_one_more_restart(self, monkeypatch, capsys):
        """A gateway drains while still holding the port, so the watchdog's
        replacement hits EADDRINUSE and parks api_server for good — the phone
        then reaches nothing (seen live). Install restarts that instance once
        more, now that the port is free."""
        from hermes_mobile_plugin import cli

        killed = self._patch(monkeypatch, seeded=True)
        calls = {"n": 0}

        def pid_after_restart():
            calls["n"] += 1
            return 4242 if calls["n"] == 1 else 5555

        monkeypatch.setattr(cli, "gateway_pid", pid_after_restart)
        # Healthy only once the retry has happened.
        monkeypatch.setattr(cli, "_wait_for_health",
                            lambda port, timeout_s: len(killed) > 1)

        assert cli.cmd_install(argparse.Namespace()) == 0
        assert killed == [4242, 5555]
        out = capsys.readouterr().out
        assert "restarting it once more" in out
        assert "Gateway is healthy" in out
