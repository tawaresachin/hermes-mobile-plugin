"""The gateway PTY shell must follow the *connected* machine, not a literal.

Round 10: the child's default directory is resolved when the shell spawns,
from the live Hermes home (HERMES_HOME / hermes-agent profile), and its env
keeps the identity variables a shell needs to render its own prompt. No test
here asserts a particular user, host or path string in the product source.
"""

from __future__ import annotations

import getpass
import os
import pty
import select
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_mobile_plugin import pty_routes

AUTH = {"Authorization": "Bearer test-key"}


class _Adapter:
    """Minimal api_server adapter: the same auth source the live gateway uses."""

    def __init__(self, key="test-key", *, raises=False):
        self.key = key
        self.raises = raises

    def _expected_api_key(self):
        if self.raises:
            raise RuntimeError("key store unavailable")
        return self.key


async def _client(adapter=None):
    app = web.Application()
    app["api_server_adapter"] = adapter or _Adapter()
    pty_routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _no_agent_constants(monkeypatch) -> None:
    # None in sys.modules makes `from hermes_constants import ...` raise,
    # exercising the standalone fallbacks without touching builtins.
    monkeypatch.setitem(sys.modules, "hermes_constants", None)


def test_hermes_cwd_follows_temp_home_env(monkeypatch, tmp_path) -> None:
    """A temporary HERMES_HOME becomes the shell's resolved cwd."""
    _no_agent_constants(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert pty_routes._hermes_cwd() == tmp_path
    assert pty_routes._hermes_cwd() != Path.home() / ".hermes"


def test_hermes_cwd_reads_agent_resolver_at_call_time(monkeypatch, tmp_path) -> None:
    """When hermes-agent is importable its live resolver wins, not a constant."""
    from_agent = tmp_path / "profile-home"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        SimpleNamespace(get_hermes_home=lambda: from_agent),
    )

    assert pty_routes._hermes_cwd() == from_agent

    # A different value on the next call is picked up: nothing is cached.
    other = tmp_path / "other-home"
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        SimpleNamespace(get_hermes_home=lambda: other),
    )
    assert pty_routes._hermes_cwd() == other


def test_hermes_cwd_has_plain_home_fallback(monkeypatch) -> None:
    """No agent resolver and no HERMES_HOME -> ~/.hermes, never a literal host."""
    _no_agent_constants(monkeypatch)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert pty_routes._hermes_cwd() == Path.home() / ".hermes"


def test_spawn_passes_runtime_cwd_and_identity_env(monkeypatch, tmp_path) -> None:
    """The spawn plumbing hands the resolved cwd and the real identity env on."""
    _no_agent_constants(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = {
        "HOME": str(tmp_path / "home"),
        "USER": "shell-user",
        "LOGNAME": "shell-user",
        "SHELL": "/bin/sh",
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_MESSAGES": "en_US.UTF-8",
    }
    for key, value in identity.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GATEWAY_API_KEY", "must-not-leak")

    captured: dict[str, object] = {}

    def fake_fork(shell, slave_fd, cols, rows, env, cwd):
        captured.update(shell=shell, env=env, cwd=cwd)
        # Implicit None: _PtySession.kill() skips a proc that is None.

    monkeypatch.setattr(pty_routes, "_fork_shell", fake_fork)
    sess = pty_routes._PtySession("test", 80, 24)
    sess.spawn()
    try:
        assert captured["cwd"] == tmp_path
        env = captured["env"]
        assert isinstance(env, dict)
        for key, value in identity.items():
            assert env[key] == value
        assert "GATEWAY_API_KEY" not in env
    finally:
        sess.kill()


def test_spawned_process_default_dir_is_runtime_home(monkeypatch, tmp_path) -> None:
    """A real child process starts in the temporary Hermes home."""
    _no_agent_constants(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = dict(os.environ)
    env.update(pty_routes._clean_child_env())

    master, slave = pty.openpty()
    proc = None
    try:
        proc = pty_routes._fork_shell(
            "/bin/pwd", slave, 80, 24, env, pty_routes._hermes_cwd()
        )
        os.close(slave)
        slave = -1
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], 5)
            if not ready:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        assert str(tmp_path).encode() in out
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        for fd in (master, slave):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


@pytest.mark.asyncio
async def test_pty_info_requires_bearer_key() -> None:
    """The info route is gated like every other /api/mobile/* route."""
    client = await _client(_Adapter(key="test-key"))
    try:
        for headers in ({}, {"Authorization": "Bearer wrong-key"}):
            resp = await client.get("/api/mobile/pty/info", headers=headers)
            assert resp.status == 401
            assert (await resp.json())["error"] == "unauthorized"
        resp = await client.get("/api/mobile/pty/info", headers=AUTH)
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pty_info_fails_closed_when_key_unresolvable() -> None:
    """An unresolved key is not a keyless gateway — refuse the request."""
    client = await _client(_Adapter(raises=True))
    try:
        resp = await client.get("/api/mobile/pty/info")
        assert resp.status == 401
        assert (await resp.json())["error"] == "unauthorized"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pty_info_follows_temp_home_env(monkeypatch, tmp_path) -> None:
    """hermes_home and cwd follow a temporary HERMES_HOME, with live identity."""
    _no_agent_constants(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client = await _client()
    try:
        resp = await client.get("/api/mobile/pty/info", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["user"] == getpass.getuser()
        assert body["host"] == socket.gethostname()
        assert body["hermes_home"] == str(tmp_path)
        assert body["cwd"] == str(tmp_path)
        # The reported cwd is the very path the spawn would receive.
        assert body["cwd"] == str(pty_routes._hermes_cwd())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pty_info_resolves_on_every_request(monkeypatch, tmp_path) -> None:
    """Two calls with a changed resolver return two different paths."""
    _no_agent_constants(monkeypatch)
    client = await _client()
    try:
        for name in ("first-home", "second-home"):
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / name))
            resp = await client.get("/api/mobile/pty/info", headers=AUTH)
            assert (await resp.json())["cwd"] == str(tmp_path / name)
    finally:
        await client.close()


def test_source_has_no_hardcoded_identity_or_synthetic_prompt() -> None:
    """No fixed user/host/path, and no hand-written prompt, in the bridge."""
    source = Path(pty_routes.__file__).read_text(encoding="utf-8")
    for literal in (
        "sachintaware",
        "@192",
        "192.168",
        "user@host",
        "%n@%m",
        "%1~",
        ".hermes %",
        "PS1",
        "PROMPT=",
    ):
        assert literal not in source
