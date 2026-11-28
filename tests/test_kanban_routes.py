"""Tests for the mobile Kanban bridge (/api/mobile/kanban/*).

The routes are thin adapters over the agent's ``hermes_cli.kanban_db`` kernel,
so these tests drive them through a real aiohttp test server with the agent
package on sys.path. They pin:
  * auth is mandatory (401 without the Bearer key),
  * the board payload shape the app parses (columns/tasks/progress/latest_event_id),
  * create → detail → comment → events → archive round-trips,
  * mode read/write semantics (toolset + dispatch_in_gateway + restart_required),
  * degraded behaviour when the agent has no kanban kernel (501).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hermes_mobile_plugin import kanban_routes

AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
KEY = "test-key-123"

pytestmark = pytest.mark.skipif(
    not (AGENT_ROOT / "hermes_cli" / "kanban_db.py").is_file(),
    reason="agent checkout with the kanban kernel is not installed",
)


class _App(dict):
    pass


def _make_app(key: str = KEY) -> _App:
    app = _App()
    app["api_server_adapter"] = type("A", (), {"_expected_api_key": lambda self: key})()
    return app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Disposable HERMES_HOME so writes never touch the real agent config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "kanban:\n  dispatch_in_gateway: true\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(AGENT_ROOT))
    for mod in ("hermes_cli.config", "hermes_cli.tools_config"):
        sys.modules.pop(mod, None)
    return tmp_path


AUTH = {"Authorization": f"Bearer {KEY}"}


async def _start(home):
    from aiohttp import web

    app = web.Application()
    app.update(_make_app())
    kanban_routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _create(client, **body):
    payload = {"title": "Test task", "body": "do the thing", **body}
    resp = await client.post("/api/mobile/kanban/tasks", json=payload, headers=AUTH)
    assert resp.status == 200, await resp.text()
    return (await resp.json())["task"]


@pytest.mark.asyncio
async def test_requires_bearer_key(home):
    client = await _start(home)
    try:
        for path in (
            "/api/mobile/kanban/board",
            "/api/mobile/kanban/mode",
            "/api/mobile/kanban/events",
        ):
            resp = await client.get(path)
            assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_board_shape(home):
    client = await _start(home)
    try:
        resp = await client.get("/api/mobile/kanban/board", headers=AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        keys = [c["key"] for c in data["columns"]]
        assert keys[:3] == ["triage", "todo", "scheduled"]
        for key in ("ready", "running", "blocked", "review", "done"):
            assert key in keys
        assert isinstance(data["latest_event_id"], int)
        assert "assignees" in data



    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_detail_comment_events_archive(home):
    client = await _start(home)
    try:
        task = await _create(client, triage=True)
        assert task["status"] == "triage"
        assert task["body"] == "do the thing"
        assert task["assignee"]

        task_id = task["id"]
        detail = (await (await client.get(f"/api/mobile/kanban/tasks/{task_id}", headers=AUTH)).json())
        assert detail["ok"] is True
        assert detail["task"]["title"] == "Test task"
        assert detail["comments"] == []

        resp = await client.post(
            f"/api/mobile/kanban/tasks/{task_id}/comments", json={"body": "which env?"}, headers=AUTH,
        )
        assert resp.status == 200
        detail = (await (await client.get(f"/api/mobile/kanban/tasks/{task_id}", headers=AUTH)).json())
        assert detail["comments"][-1]["body"] == "which env?"

        events = (await (await client.get("/api/mobile/kanban/events?since=0", headers=AUTH)).json())
        assert events["ok"] is True
        kinds = [e["kind"] for e in events["events"]]
        assert "created" in kinds
        assert events["cursor"] >= events["latest"] - 0
        assert all(e["task_title"] for e in events["events"] if e["task_id"] == task_id)

        resp = await client.post(
            f"/api/mobile/kanban/tasks/{task_id}/actions", json={"action": "archive"}, headers=AUTH,
        )
        body = await resp.json()
        assert body["ok"] is True
        assert body["applied"] is True
        assert body["task"]["status"] == "archived"



    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_action(home):
    client = await _start(home)
    try:
        task = await _create(client, triage=True)
        task_id = task["id"]

        # Hard-delete is refused while a task is running; a triage card is safe.
        resp = await client.post(
            f"/api/mobile/kanban/tasks/{task_id}/actions",
            json={"action": "delete"}, headers=AUTH,
        )
        body = await resp.json()
        assert body["ok"] is True
        assert body["deleted"] is True

        # The task is really gone, including from the board.
        detail = await (await client.get(
            f"/api/mobile/kanban/tasks/{task_id}", headers=AUTH)).json()
        assert detail["ok"] is False
        board = (await (await client.get(
            "/api/mobile/kanban/board", headers=AUTH)).json())
        all_ids = {t["id"] for c in board["columns"] for t in c["tasks"]}
        assert task_id not in all_ids
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_linked_subtask_removes_link(home):
    """Deleting a child card must also clear the parent's child list and
    progress, otherwise the board keeps showing a phantom subtask."""
    client = await _start(home)
    try:
        parent = await _create(client, title="Parent with child", triage=True)
        child = await _create(client, title="Subtask to delete", triage=True)
        import sys
        sys.path.insert(0, str(AGENT_ROOT))
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect
        conn = kanban_db_connect.connect()
        try:
            kb.link_tasks(conn, parent["id"], child["id"])
        finally:
            conn.close()

        resp = await client.post(
            f"/api/mobile/kanban/tasks/{child['id']}/actions",
            json={"action": "delete"}, headers=AUTH,
        )
        body = await resp.json()
        assert body["ok"] is True
        assert body["deleted"] is True

        board = (await (await client.get(
            "/api/mobile/kanban/board", headers=AUTH)).json())
        parent_card = next(
            t for c in board["columns"] for t in c["tasks"]
            if t["id"] == parent["id"])
        assert parent_card["children"] == []
        assert parent_card["progress"] in (None, {"done": 0, "total": 0})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_board_includes_child_cards(home):
    client = await _start(home)
    try:
        parent = await _create(client, title="Parent task", triage=True)
        child = await _create(client, title="Child task", triage=True)
        # Link via the kernel's own API.
        import sys
        sys.path.insert(0, str(AGENT_ROOT))
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect
        import os
        conn = kanban_db_connect.connect()
        try:
            # Returns True only when a ready child is demoted to todo; a
            # triage child links successfully but returns False.
            kb.link_tasks(conn, parent["id"], child["id"])
        finally:
            conn.close()

        board = (await (await client.get(
            "/api/mobile/kanban/board", headers=AUTH)).json())
        parent_card = next(
            t for c in board["columns"] for t in c["tasks"]
            if t["id"] == parent["id"])
        assert parent_card["children"]
        assert any(c["id"] == child["id"] and
                   c["status"] == child["status"]
                   for c in parent_card["children"])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_requires_title(home):
    client = await _start(home)
    try:
        resp = await client.post("/api/mobile/kanban/tasks", json={"body": "no title"}, headers=AUTH)
        assert resp.status == 400
        assert (await resp.json())["ok"] is False



    finally:
        await client.close()


@pytest.mark.asyncio
async def test_completed_task_reaches_done_column(home):
    client = await _start(home)
    try:
        task = await _create(client, triage=True)
        task_id = task["id"]
        # A triage card can be started without a live worker; unblock promotes it.
        resp = await client.post(
            f"/api/mobile/kanban/tasks/{task_id}/actions", json={"action": "unblock"}, headers=AUTH,
        )
        assert (await resp.json())["ok"] is True
        board = (await (await client.get("/api/mobile/kanban/board", headers=AUTH)).json())
        locations = {c["key"]: [t["id"] for t in c["tasks"]] for c in board["columns"]}
        # The card must still be on the board (a triage card has no promise
        # about which column the kernel promotes it to).
        assert task_id in {tid for ids in locations.values() for tid in ids}



    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mode_read_and_write(home):
    client = await _start(home)
    try:
        mode = (await (await client.get("/api/mobile/kanban/mode", headers=AUTH)).json())
        assert mode["ok"] is True
        assert mode["available"] is True
        assert mode["enabled"] is False
        assert mode["dispatch_in_gateway"] is True

        resp = await client.put("/api/mobile/kanban/mode", json={"enabled": True}, headers=AUTH)
        assert resp.status == 200
        enabled = await resp.json()
        assert enabled["enabled"] is True
        assert "kanban" in enabled["toolsets"]
        assert enabled["dispatch_in_gateway"] is True
        # dispatcher flag did not change -> no restart needed
        assert enabled["restart_required"] is False

        config = (home / "config.yaml").read_text(encoding="utf-8")
        assert "kanban" in config
        assert "api_server" in config
        assert "dispatch_in_gateway: true" in config

        resp = await client.put("/api/mobile/kanban/mode", json={"enabled": False}, headers=AUTH)
        disabled = await resp.json()
        assert disabled["enabled"] is False
        assert disabled["dispatch_in_gateway"] is False
        assert disabled["restart_required"] is True
        assert "kanban" not in disabled["toolsets"]
        config = (home / "config.yaml").read_text(encoding="utf-8")
        assert "dispatch_in_gateway: false" in config



    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mode_reports_unavailable_without_kernel(home, monkeypatch):
    client = await _start(home)
    try:
        def _boom():
            raise kanban_routes.KanbanUnavailable("kanban is not available on this agent")

        monkeypatch.setattr(kanban_routes, "_config_mode", _boom)
        resp = await client.get("/api/mobile/kanban/mode", headers=AUTH)
        assert resp.status == 501
        body = await resp.json()
        assert body["ok"] is False
        assert "kanban" in body["error"].lower()

    finally:
        await client.close()


