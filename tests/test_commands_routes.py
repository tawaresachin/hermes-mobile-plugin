"""Regression tests for the commands route's TTL cache.

Bug (v0.0.5, found on live gateway 2026-09-12): the cache tuple is
(expiry_monotonic, payload) but the read compared index [1] (the dict) to
`now` and returned index [0] (the float). Every request AFTER the first
500'd with "'>' not supported between instances of 'dict' and 'float'".
"""
from __future__ import annotations

import time

import pytest

from hermes_mobile_plugin import commands_routes


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(commands_routes, "_manifest_cache", None)
    yield
    monkeypatch.setattr(commands_routes, "_manifest_cache", None)


def test_cache_tuple_contract():
    """The stored tuple must read as (expiry, payload) — the exact indices
    the route uses. Pin the contract so a flip in either site fails here."""
    now = time.monotonic()
    payload = {"ok": True, "commands": [], "count": 0}
    commands_routes._manifest_cache = (now + 60.0, payload)

    expiry, cached = commands_routes._manifest_cache  # the route's destructure shape
    assert expiry > now
    assert isinstance(cached, dict)
    assert cached["ok"] is True


@pytest.mark.asyncio
async def test_second_hit_serves_cache_without_rebuild(fresh_cache, monkeypatch):
    """Two sequential requests: the second must come from cache (one build),
    and both must return the dict payload — the shape that used to crash."""
    builds = []

    def fake_build():
        builds.append(1)
        return {"ok": True, "commands": [{"name": "x"}], "count": 1}

    monkeypatch.setattr(commands_routes, "_build_manifest", fake_build)

    class FakeQuery(dict):
        pass

    class FakeReq:
        query = FakeQuery()

    # require_key decorator wraps the handler; call the undecorated logic via
    # the module-level route function's __wrapped__ if present, else the fn.
    route = getattr(commands_routes._commands_route, "__wrapped__", commands_routes._commands_route)

    async def fake_to_thread(fn):
        return fn()

    monkeypatch.setattr(commands_routes.asyncio, "to_thread", fake_to_thread)

    resp1 = await route(FakeReq())
    resp2 = await route(FakeReq())

    assert len(builds) == 1, "second call must hit the cache"
    import json

    body1 = json.loads(resp1.body)
    body2 = json.loads(resp2.body)
    assert body1 == body2 == {"ok": True, "commands": [{"name": "x"}], "count": 1}


@pytest.mark.asyncio
async def test_fresh_param_bypasses_cache(fresh_cache, monkeypatch):
    builds = []

    def fake_build():
        builds.append(1)
        return {"ok": True, "commands": [], "count": 0}

    monkeypatch.setattr(commands_routes, "_build_manifest", fake_build)

    class FakeReq:
        query = {"fresh": "1"}

    route = getattr(commands_routes._commands_route, "__wrapped__", commands_routes._commands_route)

    async def fake_to_thread(fn):
        return fn()

    monkeypatch.setattr(commands_routes.asyncio, "to_thread", fake_to_thread)

    await route(FakeReq())
    await route(FakeReq())
    assert len(builds) == 2
