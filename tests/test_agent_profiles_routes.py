"""Focused regression tests for the mobile agent-profile routes.

Covers the audit findings: auth fail-closed, path containment, non-object
bodies, corrupt-meta isolation, active round-trip, and SOUL 413.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hermes_mobile_plugin import agent_profiles_routes as routes
from hermes_mobile_plugin.audio import AudioBackendError

AUTH = {"Authorization": "Bearer test-key"}
_MAX_SOUL = 1024 * 1024


class _Adapter:
    def __init__(self, key="test-key", *, raises=False):
        self.key = key
        self.raises = raises

    def _expected_api_key(self):
        if self.raises:
            raise RuntimeError("key store unavailable")
        return self.key


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    profiles = root / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(
        routes._LocalProfileCore, "root", classmethod(lambda cls: root)
    )
    monkeypatch.setattr(routes, "_hermes_root", lambda: root)
    monkeypatch.setattr(routes, "_profiles_root", lambda: profiles)
    monkeypatch.setattr(routes, "_CORE", routes._LocalProfileCore())
    return root


async def _client(home, *, adapter=None, client_max_size=2 * 1024 * 1024):
    app = web.Application(client_max_size=client_max_size)
    app["api_server_adapter"] = adapter or _Adapter()
    routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _create(client, name):
    return await client.post(
        "/api/mobile/agent-profiles", json={"name": name, "description": f"role {name}"},
        headers=AUTH,
    )


@pytest.mark.asyncio
async def test_auth_fails_closed_when_key_unresolved_or_empty(home):
    for adapter in (_Adapter(key=None), _Adapter(raises=True)):
        client = await _client(home, adapter=adapter)
        try:
            for path in (
                "/api/mobile/agent-profiles",
                "/api/mobile/agent-profiles/active",
                "/api/mobile/agent-profiles/default/soul",
            ):
                resp = await client.get(path)
                assert resp.status == 401
                assert (await resp.json())["error"] == "unauthorized"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_valid_key_allows_route(home):
    client = await _client(home)
    try:
        resp = await client.get("/api/mobile/agent-profiles", headers=AUTH)
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_traversal_and_symlink_containment(home, tmp_path):
    client = await _client(home)
    try:
        resp = await client.post(
            "/api/mobile/agent-profiles", json={"name": "../escape"}, headers=AUTH
        )
        assert resp.status == 400
        assert not (tmp_path / "escape").exists()

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SOUL.md").write_text("outside secret", encoding="utf-8")
        (home / "profiles" / "link").symlink_to(outside, target_is_directory=True)

        for path in (
            "/api/mobile/agent-profiles/link",
            "/api/mobile/agent-profiles/link/soul",
        ):
            resp = await client.get(path, headers=AUTH)
            assert resp.status == 400

        resp = await client.delete("/api/mobile/agent-profiles/link", headers=AUTH)
        assert resp.status == 400
        assert (outside / "SOUL.md").read_text(encoding="utf-8") == "outside secret"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_object_body_is_400(home):
    client = await _client(home)
    try:
        resp = await client.post(
            "/api/mobile/agent-profiles", json=["not", "an", "object"], headers=AUTH
        )
        assert resp.status == 400
        assert "must be an object" in (await resp.json())["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_read_json_object_preserves_413(monkeypatch):
    async def too_large(_request):
        raise AudioBackendError("body too large", status=413)

    monkeypatch.setattr(routes, "_read_json", too_large)
    with pytest.raises(AudioBackendError) as exc:
        await routes._read_json_object(object())
    assert exc.value.status == 413


@pytest.mark.asyncio
async def test_corrupt_meta_is_isolated(home):
    client = await _client(home)
    try:
        assert (await _create(client, "alpha")).status == 201
        assert (await _create(client, "beta")).status == 201
        (home / "profiles" / "alpha" / "profile.yaml").write_text(
            "::not: valid: [", encoding="utf-8"
        )

        resp = await client.get("/api/mobile/agent-profiles", headers=AUTH)
        assert resp.status == 200
        payload = await resp.json()
        names = {entry["name"] for entry in payload["profiles"]}
        assert {"alpha", "beta"} <= names
        for entry in payload["profiles"]:
            assert "path" not in entry
            assert "soul_content" not in entry

        resp = await client.patch(
            "/api/mobile/agent-profiles/alpha", json={"description": "fixed"}, headers=AUTH
        )
        assert resp.status == 200
        assert (await resp.json())["profile"]["description"] == "fixed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_active_round_trip(home):
    client = await _client(home)
    try:
        assert (await _create(client, "alpha")).status == 201
        assert (await _create(client, "beta")).status == 201

        for name in ("alpha", "beta"):
            resp = await client.post(
                "/api/mobile/agent-profiles/active", json={"name": name}, headers=AUTH
            )
            assert resp.status == 200
            assert (await resp.json())["profile"]["name"] == name
            resp = await client.get("/api/mobile/agent-profiles/active", headers=AUTH)
            assert resp.status == 200
            assert (await resp.json())["profile"]["name"] == name

        resp = await client.post(
            "/api/mobile/agent-profiles/active", json=["bad"], headers=AUTH
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_soul_write_size_cap_is_413(home):
    client = await _client(home)
    try:
        assert (await _create(client, "alpha")).status == 201
        resp = await client.put(
            "/api/mobile/agent-profiles/alpha/soul",
            json={"content": "x" * (_MAX_SOUL + 1)},
            headers=AUTH,
        )
        assert resp.status == 413
        assert (await resp.json())["error"] == "SOUL.md exceeds the 1 MiB size limit"
    finally:
        await client.close()
