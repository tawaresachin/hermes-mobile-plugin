"""Cross-platform invariants for the mobile plugin.

The plugin ships on Windows/macOS/Linux/Termux hosts; these guard the
platform seams (launcher lookup, detach kwargs, PID probing, HTML escape).
"""
import os
import sys

import pytest

from hermes_mobile_plugin.constants import find_hermes_cli, gateway_health_url
from hermes_mobile_plugin.supervisor import _detach_kwargs, pid_alive, terminate_pid
from hermes_mobile_plugin.qr_generator import get_html_template


def test_health_url_follows_port():
    assert gateway_health_url(9999) == "http://127.0.0.1:9999/health"
    assert "8642" in gateway_health_url()


def test_find_hermes_cli_returns_str_or_none():
    result = find_hermes_cli()
    assert result is None or isinstance(result, str)
    if sys.platform == "win32":  # pragma: no cover - not CI
        assert result is None or result.endswith(".exe") or "hermes" in result


def test_detach_kwargs_platform_shape():
    kw = _detach_kwargs()
    if os.name == "nt":
        assert "creationflags" in kw and "start_new_session" not in kw
    else:
        assert kw == {"start_new_session": True}


def test_pid_alive_semantics():
    assert pid_alive(os.getpid()) is True
    # A pid that cannot exist (reaped long ago) must read dead, and probes
    # must never raise.
    assert pid_alive(0x7FFFFFF0) is False


def test_terminate_pid_guards_self_kill():
    # Terminate our OWN pid only on a throwaway sleep child, never the pytest
    # process: spawn sleep 30, terminate it, confirm it dies.
    import subprocess, time
    if os.name == "nt":  # pragma: no cover
        proc = subprocess.Popen(["ping", "-n", "30", "127.0.0.1"],
                                stdout=subprocess.DEVNULL)
    else:
        proc = subprocess.Popen(["sleep", "30"])
    time.sleep(0.2)
    assert terminate_pid(proc.pid) is True
    proc.wait(timeout=5)


def test_status_html_escapes_injection():
    evil = {
        "url": 'http://x"><script>alert(1)</script>',
        "api_key": 'k" onload="x',
        "context_compression": True,
        "tailscale_ip": "1.2.3.4",
    }
    html = get_html_template("<svg/>", evil)
    assert "<script>alert(1)</script>" not in html
    assert '"><' not in html          # no raw quote can close an attr/tag
    assert '" onload=' not in html    # quote escaped -> no event handler
    assert "&lt;script&gt;" in html   # present as inert text


def test_plugin_version_derives_from_manifest():
    """The version constant and the manifest can never disagree — the drift
    between constants.py and plugin.yaml is what broke the About row."""
    import re
    from pathlib import Path
    from hermes_mobile_plugin.constants import PLUGIN_VERSION
    manifest = Path(__file__).resolve().parent.parent / "plugin.yaml"
    text = manifest.read_text(encoding="utf-8")
    m = re.search(r"""^version:\s*["']?([0-9][0-9A-Za-z.\-]*)["']?\s*$""", text, re.M)
    assert m, "plugin.yaml has no version line"
    assert m.group(1) == PLUGIN_VERSION
