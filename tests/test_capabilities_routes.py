"""Regression tests for the HI-4 MCP secrets leak fix.

Bug: capabilities_routes._get_mcp_servers returned MCP server `env` and
`headers` values verbatim (tokens, DB URLs) in the mobile API. This module
pins that the response carries ONLY key names (env_keys / header_keys) and
that the url userinfo is redacted.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_mobile_plugin import capabilities_routes

CONFIG_YAML = """\
mcp_servers:
  github:
    command: ["npx", "-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "ghp_0123456789abcdef"
    headers:
      Authorization: "Bearer sk-0123456789abcdef"
    url: "https://user:pass@api.github.com/user?token=ghp_QUERYSECRET"
    transport: stdio
  filesystem:
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem"]
    url: "https://localhost:3000/"
    transport: sse
"""


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    return cfg


class _App(dict):
    pass


def _app_with(adapter_key="scoped"):
    app = _App()
    app["api_server_adapter"] = type("A", (), {"_expected_api_key": lambda self: adapter_key})()
    return app


def _call_get_mcp_servers(tmp_path):
    """Point HERMES_HOME at a disposable config, call the helper, restore."""
    original = capabilities_routes.HERMES_HOME
    capabilities_routes.HERMES_HOME = tmp_path.resolve()
    try:
        return capabilities_routes._get_mcp_servers()
    finally:
        capabilities_routes.HERMES_HOME = original


@pytest.fixture
def mcp_servers(tmp_path):
    _write_config(tmp_path)
    return _call_get_mcp_servers(tmp_path)


def test_no_secret_values_leak_in_env(mcp_servers):
    """`env` *values* (the token) must not appear anywhere in the response."""
    for entry in mcp_servers:
        assert "env" not in entry
        assert "ghp_0123456789abcdef" not in str(entry)


def test_no_secret_values_leak_in_headers(mcp_servers):
    """`headers` *values* (the bearer token) must not appear anywhere."""
    for entry in mcp_servers:
        assert "headers" not in entry
        assert "sk-0123456789abcdef" not in str(entry)


def test_env_and_headers_are_key_names_only(mcp_servers):
    github = next(e for e in mcp_servers if e["name"] == "github")
    assert "env_keys" in github
    assert "header_keys" in github
    assert github["env_keys"] == ["GITHUB_TOKEN"]
    assert github["header_keys"] == ["Authorization"]


def test_url_userinfo_and_query_are_redacted(mcp_servers):
    github = next(e for e in mcp_servers if e["name"] == "github")
    assert "pass@" not in github["url"]
    assert "user:pass" not in github["url"]
    assert "token=" not in github["url"]
    assert "ghp_QUERYSECRET" not in github["url"]
    # userinfo still present (username only) but password is redacted.
    assert "user:…redacted…" in github["url"] or "user@api.github.com" in github["url"]


def test_url_no_userinfo_unchanged(mcp_servers):
    filesystem = next(e for e in mcp_servers if e["name"] == "filesystem")
    assert "https://localhost:3000/" in filesystem["url"]


# ── HTTP-level regression: the *response body* must never carry secrets ────


class _Adapter:
    def __init__(self, key="scoped", raises=False):
        self.key = key
        self.raises = raises

    def _expected_api_key(self):
        if self.raises:
            raise RuntimeError("key store unavailable")
        return self.key


async def _client(tmp_path, monkeypatch, *, adapter=None):
    for name in ("HERMES_HOME", "HERMES_CONFIG_DIR", "HERMES_PROFILE_DIR"):
        monkeypatch.delenv(name, raising=False)
    _write_config(tmp_path)
    original = capabilities_routes.HERMES_HOME
    capabilities_routes.HERMES_HOME = tmp_path.resolve()
    try:
        app = web.Application()
        app["api_server_adapter"] = adapter or _Adapter()
        capabilities_routes.register(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        return client, original
    except Exception:
        capabilities_routes.HERMES_HOME = original
        raise


async def _close(client, original):
    await client.close()
    capabilities_routes.HERMES_HOME = original


@pytest.mark.asyncio
async def test_http_mcp_servers_response_has_no_secret_values(tmp_path, monkeypatch):
    client, original = await _client(tmp_path, monkeypatch)
    try:
        resp = await client.get("/api/mobile/mcp-servers", headers={"Authorization": "Bearer scoped"})
        assert resp.status == 200
        body = await resp.text()
        for secret in ("ghp_0123456789abcdef", "sk-0123456789abcdef", "ghp_QUERYSECRET", "pass@"):
            assert secret not in body
        payload = await resp.json()
        github = next(s for s in payload["servers"] if s["name"] == "github")
        assert github["env_keys"] == ["GITHUB_TOKEN"]
        assert github["header_keys"] == ["Authorization"]
    finally:
        await _close(client, original)


@pytest.mark.asyncio
async def test_http_all_response_has_no_secret_values(tmp_path, monkeypatch):
    client, original = await _client(tmp_path, monkeypatch)
    try:
        resp = await client.get("/api/mobile/all", headers={"Authorization": "Bearer scoped"})
        assert resp.status == 200
        body = await resp.text()
        for secret in ("ghp_0123456789abcdef", "sk-0123456789abcdef", "ghp_QUERYSECRET"):
            assert secret not in body
        payload = await resp.json()
        assert payload["counts"]["capabilities"] == len(payload["capabilities"])
        assert any(s["name"] == "github" for s in payload["mcp_servers"])
    finally:
        await _close(client, original)


@pytest.mark.asyncio
async def test_http_new_capability_routes_have_expected_shape(tmp_path, monkeypatch):
    client, original = await _client(tmp_path, monkeypatch)
    try:
        cases = (
            ("/api/mobile/capabilities/plugins", "plugins"),
            ("/api/mobile/capabilities/mcp-servers", "mcp_servers"),
            ("/api/mobile/capabilities/projects", "projects"),
            ("/api/mobile/capabilities/skills", "skills"),
        )
        for path, key in cases:
            resp = await client.get(path, headers={"Authorization": "Bearer scoped"})
            assert resp.status == 200
            payload = await resp.json()
            assert payload["ok"] is True
            assert isinstance(payload[key], list)
            assert payload["count"] == len(payload[key])

        resp = await client.get("/api/mobile/capabilities/all", headers={"Authorization": "Bearer scoped"})
        assert resp.status == 200
        payload = await resp.json()
        assert payload["ok"] is True
        for key in ("plugins", "mcp_servers", "projects", "skills"):
            assert isinstance(payload[key], list)
    finally:
        await _close(client, original)


@pytest.mark.asyncio
async def test_http_new_all_response_has_no_secret_values(tmp_path, monkeypatch):
    client, original = await _client(tmp_path, monkeypatch)
    try:
        resp = await client.get("/api/mobile/capabilities/all", headers={"Authorization": "Bearer scoped"})
        assert resp.status == 200
        body = await resp.text()
        for secret in ("ghp_0123456789abcdef", "sk-0123456789abcdef", "ghp_QUERYSECRET", "pass@"):
            assert secret not in body
        payload = await resp.json()
        github = next(s for s in payload["mcp_servers"] if s["name"] == "github")
        assert github["env_keys"] == ["GITHUB_TOKEN"]
        assert github["header_keys"] == ["Authorization"]
    finally:
        await _close(client, original)


@pytest.mark.asyncio
async def test_http_capability_routes_fail_closed(tmp_path, monkeypatch):
    for adapter in (_Adapter(key=None), _Adapter(raises=True)):
        client, original = await _client(tmp_path, monkeypatch, adapter=adapter)
        try:
            for path in (
                "/api/mobile/capabilities",
                "/api/mobile/capabilities/plugins",
                "/api/mobile/capabilities/mcp-servers",
                "/api/mobile/capabilities/projects",
                "/api/mobile/capabilities/skills",
                "/api/mobile/capabilities/all",
                "/api/mobile/mcp-servers",
                "/api/mobile/all",
            ):
                resp = await client.get(path)
                assert resp.status == 401
                assert (await resp.json())["error"] == "unauthorized"
        finally:
            await _close(client, original)
