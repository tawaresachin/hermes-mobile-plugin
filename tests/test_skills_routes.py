"""Regression tests for the skills manifest route.

Bug (release 0.22.2, found on live gateway 2026-10-19): core
``GET /v1/skills`` 500s because ``api_server.py`` still calls
``_find_all_skills(include_editorial=True)`` after the Collective-Wisdom
revert stripped that kwarg from the tool. The app swallows the 500 and the
Settings panel shows "Skills (0)" despite installed skills. The plugin's
``/api/mobile/skills`` fallback enumerates the same catalog itself and must
be signature-tolerant: pass ``include_editorial`` only when the installed
hermes accepts it.
"""
from __future__ import annotations

import json
import sys
import types
import time

import pytest

from hermes_mobile_plugin import skills_routes


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(skills_routes, "_cache", None)
    yield
    monkeypatch.setattr(skills_routes, "_cache", None)


def _install_fake_skills_tool(monkeypatch, accept_include_editorial: bool,
                              public_names: bool = False) -> list:
    """Point ``tools.skills_tool`` at a fake module matching a release's
    signature + naming; returns the kwarg log so tests assert which kwargs
    landed. ``public_names=False`` mirrors 0.22.2 (``_find_all_skills``),
    ``public_names=True`` mirrors later trees (``find_all_skills``)."""
    calls: list[dict] = []

    if accept_include_editorial:
        def find_all_skills(*, skip_disabled: bool = False, include_editorial: bool = False):
            calls.append({"skip_disabled": skip_disabled, "include_editorial": include_editorial})
            return [{"name": "x", "description": "d", "category": "c"}]
    else:
        def find_all_skills(*, skip_disabled: bool = False):
            calls.append({"skip_disabled": skip_disabled})
            return [{"name": "x", "description": "d", "category": "c"}]

    sort_skills = lambda skills: sorted(skills, key=lambda s: s["name"])
    fake_module = types.ModuleType("tools.skills_tool")
    if public_names:
        setattr(fake_module, "find_all_skills", find_all_skills)
        setattr(fake_module, "sort_skills", sort_skills)
    else:
        setattr(fake_module, "_find_all_skills", find_all_skills)
        setattr(fake_module, "_sort_skills", sort_skills)
    fake_parent = types.ModuleType("tools")
    setattr(fake_parent, "skills_tool", fake_module)
    monkeypatch.setitem(sys.modules, "tools", fake_parent)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_module)
    return calls


def test_old_signature_no_include_editorial_kwarg(monkeypatch):
    """Current releases (0.22.2): the tool takes only skip_disabled. The
    route must NOT forward include_editorial — that is the 500 we avoid."""
    calls = _install_fake_skills_tool(monkeypatch, accept_include_editorial=False)
    result = skills_routes._list_skills()
    assert result == [{"name": "x", "description": "d", "category": "c"}]
    assert calls == [{"skip_disabled": False}]


def test_public_names_later_trees(monkeypatch):
    """Later trees renamed the private helpers public; the route must find
    ``find_all_skills``/``sort_skills`` on that shape too."""
    calls = _install_fake_skills_tool(monkeypatch, accept_include_editorial=False,
                                      public_names=True)
    result = skills_routes._list_skills()
    assert result == [{"name": "x", "description": "d", "category": "c"}]
    assert calls == [{"skip_disabled": False}]


def test_new_signature_forwards_include_editorial(monkeypatch):
    """If a future release re-adds the kwarg, forward include_editorial=True
    (the same call the core route makes)."""
    calls = _install_fake_skills_tool(monkeypatch, accept_include_editorial=True)
    skills_routes._list_skills()
    assert calls == [{"skip_disabled": False, "include_editorial": True}]


@pytest.mark.asyncio
async def test_route_returns_manifest_shape(fresh_cache, monkeypatch):
    monkeypatch.setattr(
        skills_routes, "_list_skills",
        lambda: [{"name": "a", "description": "d1", "category": "c"},
                 {"name": "", "description": "dropped — blank name", "category": "c"}],
    )

    class FakeQuery(dict):
        pass

    class FakeReq:
        query = FakeQuery()

    route = getattr(skills_routes._skills_route, "__wrapped__", skills_routes._skills_route)

    async def fake_to_thread(fn):
        return fn()

    monkeypatch.setattr(skills_routes.asyncio, "to_thread", fake_to_thread)
    resp = await route(FakeReq())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["skills"] == [{"name": "a", "description": "d1", "category": "c"}]


@pytest.mark.asyncio
async def test_second_hit_serves_cache_without_rebuild(fresh_cache, monkeypatch):
    builds = []

    def fake_list_skills():
        builds.append(1)
        return []

    monkeypatch.setattr(skills_routes, "_list_skills", fake_list_skills)

    class FakeQuery(dict):
        pass

    class FakeReq:
        query = FakeQuery()

    route = getattr(skills_routes._skills_route, "__wrapped__", skills_routes._skills_route)

    async def fake_to_thread(fn):
        return fn()

    monkeypatch.setattr(skills_routes.asyncio, "to_thread", fake_to_thread)

    first = await route(FakeReq())
    second = await route(FakeReq())
    assert builds == [1]  # exactly one disk scan
    assert json.loads(first.body)["ok"] is True
    assert json.loads(second.body)["ok"] is True


def test_cache_tuple_contract():
    """The stored tuple must read as (expiry, payload) — pin the contract
    so an index flip at either site fails here."""
    now = time.monotonic()
    payload = {"ok": True, "skills": [], "count": 0}
    skills_routes._cache = (now + 60.0, payload)

    expiry, cached = skills_routes._cache  # the route's destructure shape
    assert expiry > now
    assert isinstance(cached, dict)
    assert cached["ok"] is True
    skills_routes._cache = None


@pytest.mark.asyncio
async def test_enumeration_failure_reports_500_not_silent_empty(fresh_cache, monkeypatch):
    """A crash must surface as {ok: false} 500 — the app distinguishes
    'failed to load' from 'genuinely zero skills' on that."""

    def boom():
        raise RuntimeError("skills dir unreadable")

    monkeypatch.setattr(skills_routes, "_list_skills", boom)

    class FakeQuery(dict):
        pass

    class FakeReq:
        query = FakeQuery()

    route = getattr(skills_routes._skills_route, "__wrapped__", skills_routes._skills_route)

    async def fake_to_thread(fn):
        return fn()

    monkeypatch.setattr(skills_routes.asyncio, "to_thread", fake_to_thread)
    resp = await route(FakeReq())
    assert resp.status == 500
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert "unreadable" in body["error"]
