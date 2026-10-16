"""Cross-platform compatibility regression tests.

These pin the three platform-specific fixes that made the plugin actually
run on Windows and macOS, not just on Linux/Termux:

* the update/apply heal-step splice is tokenised into two separate shell
  words (POSIX) / invoked with the & call operator (Windows) instead of
  collapsing to one unrunnable command word;
* envdetect recognises a Windows ``Scripts/python.exe`` venv layout and a
  backslash-separated site-packages path;
* the supervisor's "is the gateway alive" check works without /proc
  (macOS) by trusting a live spawned pid and falling back to pgrep.
"""
import inspect
import os
import shlex
import subprocess
import sys
from pathlib import Path

import hermes_mobile_plugin.envdetect as ed
import hermes_mobile_plugin.supervisor as sup
from hermes_mobile_plugin import update_routes


# --- update/apply heal-step splice ------------------------------------------

def _real_heal_argv() -> list:
    """A real [python, heal-script] argv. The CI env has the package
    installed, so heal_runtime_venv.py sits next to update_routes."""
    script = Path(update_routes.__file__).parent / "heal_runtime_venv.py"
    return [sys.executable, str(script)]


def test_posix_heal_splice_is_two_shell_words():
    heal = _real_heal_argv()
    # The fixed splice: each argv item quoted, then joined — two shell words.
    new = " ".join(shlex.quote(a) for a in heal)
    # The pre-fix splice: the whole "python script" quoted as ONE shell word.
    old = shlex.quote(" ".join(heal))
    assert shlex.split(new) == heal, "fix must preserve the two-arg argv"
    # The old form collapsed into a single word (bash: command not found).
    assert shlex.split(old) == [" ".join(heal)], "old form was one word"
    assert shlex.split(new) != shlex.split(old)


def test_posix_heal_splice_actually_runs_the_script(tmp_path):
    # Prove the fixed splice, when executed, really runs the heal script with
    # its argv intact (not a silent no-op). Runs the argv exactly as the
    # fixed splice tokenises it, against a fake empty home so heal does
    # "nothing to do" and exits 0.
    heal = _real_heal_argv()
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "nohome"))
    argv = shlex.split(" ".join(shlex.quote(a) for a in heal))
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                       env=env)
    assert r.returncode == 0
    assert "nothing to do" in r.stdout


def test_windows_heal_splice_uses_call_operator():
    # PowerShell: bare 'py' 'script' is inert; the & call operator is needed.
    src = inspect.getsource(update_routes)
    assert '"; & " + " ".join(' in src, "Windows heal step lost its & operator"
    assert "for a in heal" in src
    # The POSIX splice must quote each argv item separately.
    assert '" ".join(shlex.quote(a) for a in heal)' in src
    # The old broken POSIX form must be gone.
    assert "shlex.quote(' '.join(heal))" not in src


# --- envdetect: Windows venv layout ------------------------------------------

def test_is_runtime_venv_accepts_windows_scripts_layout(tmp_path):
    # A Windows venv has Scripts/python.exe, not bin/python. The POSIX-only
    # check used to reject it, making find_runtime_venv() return None on
    # every Windows isolated-venv host.
    venv = tmp_path / "installs" / "h" / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("")
    assert ed._is_runtime_venv(venv) is True
    # And a bare dir with neither layout is still rejected.
    bare = tmp_path / "installs" / "no" / "venv"
    bare.mkdir(parents=True)
    assert ed._is_runtime_venv(bare) is False


def test_venv_from_sys_path_normalizes_windows_backslashes():
    # A Windows site-packages entry is backslash-separated. The forward-slash
    # regex used to never match it, silently skipping the entry; the fix
    # normalises the entry first. Prove the contract: raw backslash input is
    # unmatched, the normalised form is, and POSIX entries are unaffected.
    import re
    src = inspect.getsource(ed._venv_from_sys_path)
    assert '.replace("\\\\", "/")' in src, "backslash normalisation missing"
    entry = r"C:\h\installs\h\venv\Lib\python3.12\site-packages"
    pat = r"(.*)/(site-packages|python\d+\.\d+/site-packages)$"
    assert re.search(pat, entry) is None          # pre-fix: backslash skipped
    assert re.search(pat, entry.replace("\\", "/")) is not None  # now handled
    posix = "/h/installs/h/venv/lib/python3.12/site-packages"
    assert posix.replace("\\", "/") == posix       # POSIX entry unchanged


# --- supervisor: liveness without /proc (macOS) ------------------------------

def test_gateway_exists_trusts_live_spawned_pid():
    # A gateway we launched is judged by liveness alone (os.kill(0)/OpenProcess),
    # so this holds on macOS too where /proc is absent.
    svc = sup.GatewaySupervisor(port=80)
    svc._spawned_pids = [os.getpid()]  # our own pid: guaranteed live
    assert svc.gateway_process_exists() is True


def test_gateway_exists_falls_back_to_pgrep_without_proc(monkeypatch):
    # With no /proc and no spawned pids, the foreign-gateway check must ask
    # pgrep; confirm it is invoked with the expected pattern and its output
    # drives the verdict.
    svc = sup.GatewaySupervisor(port=80)
    svc._spawned_pids = []

    calls = {}

    class _Res:
        stdout = "  777  \n"

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _Res()

    import subprocess as sp
    monkeypatch.setattr(sp, "run", _fake_run)

    # Force /proc to appear absent on any host by handing back a proxy whose
    # .exists() reports False ONLY for /proc (a real Path's methods are
    # read-only, so we wrap rather than mutate).
    real_path = sup.Path
    def _fake_path(*args, **kwargs):
        p = real_path(*args, **kwargs)
        if str(p) != "/proc":
            return p
        class _Procless:
            def exists(self, *a, **k):
                return False
            def glob(self, *a, **k):
                return iter(())
        return _Procless()
    monkeypatch.setattr(sup, "Path", _fake_path)

    assert svc.gateway_process_exists() is True
    assert calls.get("cmd") == ["pgrep", "-f", "hermes.*gateway.*run"]

    # And when pgrep finds nothing, the verdict is False (no false "alive").
    _Res.stdout = "   \n"
    assert svc.gateway_process_exists() is False
