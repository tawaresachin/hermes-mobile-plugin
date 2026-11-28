"""Kanban routes for the Hermes Mobile app.

The agent's kanban feature lives in ``hermes_cli.kanban_db`` (the same code the
CLI and the dashboard plugin use). The mobile app talks to the api_server on
8642, which does not mount the dashboard's FastAPI plugin, so these routes are
thin aiohttp adapters over the same DB/kernel:

  GET  /api/mobile/kanban/mode          -> {enabled, dispatch_in_gateway, ...}
  PUT  /api/mobile/kanban/mode          {enabled} -> same
  GET  /api/mobile/kanban/boards        -> [{slug, name, ...}]
  GET  /api/mobile/kanban/board?board=  -> columns/tasks/assignees
  POST /api/mobile/kanban/tasks         {title, body, assignee, ...}
  GET  /api/mobile/kanban/tasks/{id}    -> task + comments + events + runs
  POST /api/mobile/kanban/tasks/{id}/comments {body}
  POST /api/mobile/kanban/tasks/{id}/actions  {action, ...}
  GET  /api/mobile/kanban/events?since= -> events after a cursor (notifier)
  GET  /api/mobile/kanban/assignees      -> [{name, on_disk, counts}]

``mode`` is the agent-side switch: the ``kanban`` toolset on the api_server
platform (so chats can use the board) plus ``kanban.dispatch_in_gateway`` (so
the gateway actually executes ready tasks). ``dispatch_in_gateway`` is read at
gateway boot, so a change to it reports ``restart_required``; the app surfaces
that instead of pretending the toggle took effect.

Every blocking SQLite call runs in a worker thread. A missing/older agent
without kanban returns 501 with a stable ``error`` so the app can hide the
feature instead of crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any, Optional

from aiohttp import web

from .audio_routes import _json_response, _read_json, require_key

logger = logging.getLogger(__name__)

_BLOCKING_TIMEOUT_SECONDS = 30.0


class KanbanUnavailable(RuntimeError):
    """Agent install has no kanban kernel (older agent)."""


def _kb() -> Any:
    try:
        from hermes_cli import kanban_db  # type: ignore
    except Exception as exc:  # noqa: BLE001 - older agent / missing module
        raise KanbanUnavailable("kanban is not available on this agent") from exc
    return kanban_db


def _kbc() -> Any:
    try:
        from hermes_cli import kanban_db_connect  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise KanbanUnavailable("kanban connect is not available on this agent") from exc
    return kanban_db_connect


def _kbb() -> Any:
    try:
        from hermes_cli import kanban_db_boards  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise KanbanUnavailable("kanban boards are not available on this agent") from exc
    return kanban_db_boards


def _workflow() -> Any:
    try:
        from hermes_cli import kanban_workflow  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise KanbanUnavailable("kanban workflow is not available on this agent") from exc
    return kanban_workflow


async def _in_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs), timeout=_BLOCKING_TIMEOUT_SECONDS
    )


def _error(message: str, status: int = 400) -> web.Response:
    return _json_response({"ok": False, "error": message}, status=status)


def _unavailable(exc: Exception) -> web.Response:
    logger.info("kanban route unavailable: %s", exc)
    return _error(str(exc) or "kanban is not available on this agent", status=501)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _board_param(request: web.Request) -> Optional[str]:
    raw = request.query.get("board", "").strip()
    return raw or None


def _open(board: Optional[str]) -> sqlite3.Connection:
    kb = _kb()
    kb.init_db(board=board)
    return _kbc().connect(board=board)


def _task_dict(task: Any, *, include_body: bool = True) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": getattr(task, "tenant", None),
        "created_at": getattr(task, "created_at", None),
        "started_at": getattr(task, "started_at", None),
        "completed_at": getattr(task, "completed_at", None),
        "latest_summary": None,
        "block_kind": getattr(task, "block_kind", None),
        "block_recurrences": getattr(task, "block_recurrences", 0),
        "worker_pid": getattr(task, "worker_pid", None),
        "last_heartbeat_at": getattr(task, "last_heartbeat_at", None),
        "goal_mode": bool(getattr(task, "goal_mode", 0)),
    }
    if include_body:
        d["body"] = getattr(task, "body", None)
        d["result"] = getattr(task, "result", None)
    else:
        body = getattr(task, "body", None) or ""
        d["body_preview"] = body[:240]
    return d


def _read_board(board: Optional[str], *, include_archived: bool = False) -> dict[str, Any]:
    """Board grouped by workflow column; mirrors the dashboard's assembly."""
    kb = _kb()
    wf = _workflow()
    kbb = _kbb()
    conn = _open(board)
    try:
        tasks = kb.list_tasks(conn, include_archived=include_archived)
        link_counts: dict[str, dict[str, int]] = {}
        for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
            link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
            link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
        comment_counts = {
            r["task_id"]: r["n"]
            for r in conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")
        }
        progress: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l "
            "JOIN tasks t ON t.id = l.child_id"
        ).fetchall():
            p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
            p["total"] += 1
            p["done"] += 1 if row["cstatus"] == "done" else 0
        # Child cards per parent, with the stage (status) of each — the mobile
        # UI renders these as nested cards under their parent with a badge.
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.id, t.title, t.status, t.assignee "
            "FROM task_links l JOIN tasks t ON t.id = l.child_id "
            "ORDER BY t.created_at"
        ).fetchall():
            children_by_parent.setdefault(row["pid"], []).append({
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "assignee": row["assignee"],
            })
        summaries = kb.latest_summaries(conn, [t.id for t in tasks])
        run_starts = kb.current_run_started_ats(conn, [t.id for t in tasks])
        latest_event_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()["m"]
        workflow = wf.DEFAULT_WORKFLOW
        columns: dict[str, list[dict[str, Any]]] = {c.key: [] for c in workflow.columns}
        if include_archived:
            columns["archived"] = []
        for t in tasks:
            d = _task_dict(t, include_body=False)
            d["latest_summary"] = summaries.get(t.id)
            d["comment_count"] = comment_counts.get(t.id, 0)
            d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
            d["progress"] = progress.get(t.id)
            d["current_run_started_at"] = run_starts.get(t.id)
            d["children"] = children_by_parent.get(t.id, [])
            key = t.status if t.status in columns else "todo"
            columns[key].append(d)
        # Done history newest-first; queue columns keep dispatch order.
        columns["done"].sort(key=lambda d: d["id"], reverse=True)
        columns["done"].sort(key=lambda d: (d.get("completed_at") is None, -(d.get("completed_at") or 0)))
        tenants = [
            r["tenant"]
            for r in conn.execute(
                "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant"
            )
        ]
        assignees = [entry["name"] for entry in kb.known_assignees(conn)]
        stats = kb.board_stats(conn)
        return {
            "ok": True,
            "board": board or kb.get_current_board(),
            "boards": [b.get("slug") for b in kbb.list_boards(include_archived=False)],
            "columns": [
                {"key": c.key, "label": c.label, "icon": c.icon, "tasks": columns[c.key]}
                for c in workflow.columns
            ] + ([{"key": "archived", "label": "Archived", "icon": "▣", "tasks": columns["archived"]}]
                 if include_archived else []),
            "assignees": assignees,
            "tenants": tenants,
            "latest_event_id": int(latest_event_id),
            "now": int(time.time()),
            "counts": stats.get("by_status", {}),
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _config_mode() -> dict[str, Any]:
    """Read agent-side Kanban mode from config.yaml."""
    from hermes_cli.config import load_config  # type: ignore

    cfg = load_config()
    explicit = ((cfg.get("platform_toolsets") or {}).get("api_server"))
    if isinstance(explicit, list):
        toolsets = [str(x) for x in explicit]
        enabled = "kanban" in toolsets
    else:
        try:
            from hermes_cli.tools_config import _get_platform_tools  # type: ignore

            toolsets = sorted(_get_platform_tools(
                cfg, "api_server", include_default_mcp_servers=False))
        except Exception:  # noqa: BLE001 - config probe must not break status
            toolsets = []
        enabled = "kanban" in toolsets
    dispatch = bool((cfg.get("kanban") or {}).get("dispatch_in_gateway", True))
    return {
        "enabled": enabled,
        "dispatch_in_gateway": dispatch,
        "toolsets": toolsets,
        "explicit": isinstance(explicit, list),
    }


def _set_config_mode(enabled: bool) -> dict[str, Any]:
    """Flip the api_server kanban toolset + dispatcher flag in config.yaml."""
    from hermes_cli.config import load_config, save_config  # type: ignore

    cfg = load_config()
    before = bool((cfg.get("kanban") or {}).get("dispatch_in_gateway", True))
    explicit = ((cfg.get("platform_toolsets") or {}).get("api_server"))
    if isinstance(explicit, list):
        base = [str(x) for x in explicit]
    else:
        try:
            from hermes_cli.tools_config import _get_platform_tools  # type: ignore

            base = sorted(_get_platform_tools(
                cfg, "api_server", include_default_mcp_servers=False))
        except Exception:  # noqa: BLE001
            base = []
    if enabled:
        if "kanban" not in base:
            base.append("kanban")
    else:
        base = [x for x in base if x != "kanban"]
    cfg.setdefault("platform_toolsets", {})["api_server"] = sorted(base)
    cfg.setdefault("kanban", {})["dispatch_in_gateway"] = bool(enabled)
    save_config(cfg, merge_existing=True)
    after = bool((cfg.get("kanban") or {}).get("dispatch_in_gateway", True))
    return {
        "enabled": "kanban" in base,
        "dispatch_in_gateway": after,
        "toolsets": sorted(base),
        "explicit": True,
        "restart_required": before != after,
    }


# --- Routes -----------------------------------------------------------------

@require_key
async def _mode_get(request: web.Request) -> web.Response:
    try:
        mode = await _in_thread(_config_mode)
        boards: list[dict[str, Any]] = []
        try:
            boards = await _in_thread(_kbb().list_boards, include_archived=False)
        except Exception as exc:  # noqa: BLE001 - mode still useful without a board
            logger.debug("kanban boards probe failed: %s", exc)
        mode.update({"ok": True, "available": True, "boards": boards})
        return _json_response(mode)
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban mode read failed")
        return _error(f"Could not read Kanban mode: {exc}", 500)


@require_key
async def _mode_put(request: web.Request) -> web.Response:
    try:
        body = await _read_json(request)
        enabled = bool(body.get("enabled"))
        result = await _in_thread(_set_config_mode, enabled)
        result.update({"ok": True, "available": True})
        return _json_response(result)
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban mode write failed")
        return _error(f"Could not update Kanban mode: {exc}", 500)


@require_key
async def _boards(request: web.Request) -> web.Response:
    try:
        return _json_response({"ok": True, "boards": await _in_thread(
            _kbb().list_boards, include_archived=False)})
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban boards failed")
        return _error(f"Could not list boards: {exc}", 500)


@require_key
async def _board(request: web.Request) -> web.Response:
    try:
        include_archived = request.query.get("include_archived", "0") in {"1", "true", "yes"}
        data = await _in_thread(_read_board, _board_param(request), include_archived=include_archived)
        return _json_response(data)
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban board read failed")
        return _error(f"Could not read the Kanban board: {exc}", 500)


@require_key
async def _create_task(request: web.Request) -> web.Response:
    try:
        body = await _read_json(request)
        title = str(body.get("title") or "").strip()
        if not title:
            return _error("title is required")
        board = str(body.get("board") or "").strip() or _board_param(request)
        payload: dict[str, Any] = {
            "title": title,
            "body": (str(body.get("body")).strip() or None) if body.get("body") is not None else None,
            "assignee": str(body.get("assignee") or "").strip() or None,
            "triage": bool(body.get("triage", False)),
            "priority": _as_int(body.get("priority"), 0),
            "goal_mode": bool(body.get("goal_mode", False)),
            "created_by": "mobile",
            "workspace_kind": "scratch",
        }
        if not payload["assignee"]:
            try:
                from hermes_cli.config import load_config  # type: ignore

                default_assignee = str(
                    ((load_config().get("kanban") or {}).get("default_assignee") or "")
                ).strip()
            except Exception:  # noqa: BLE001
                default_assignee = ""
            payload["assignee"] = default_assignee or "default"

        def _run() -> dict[str, Any]:
            kb = _kb()
            conn = _open(board)
            try:
                task_id = kb.create_task(conn, board=board, **payload)
                task = kb.get_task(conn, task_id)
                return {"ok": True, "task": _task_dict(task) if task else None}
            finally:
                conn.close()

        return _json_response(await _in_thread(_run))
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban create failed")
        return _error(f"Could not create the task: {exc}", 500)


@require_key
async def _task_detail(request: web.Request) -> web.Response:
    task_id = request.match_info.get("task_id", "")
    if not task_id:
        return _error("task id is required")
    board = _board_param(request)

    def _run() -> dict[str, Any]:
        kb = _kb()
        conn = _open(board)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "task not found"}
            d = _task_dict(task, include_body=True)
            d["latest_summary"] = (kb.latest_summaries(conn, [task_id]) or {}).get(task_id)
            d["current_run_started_at"] = kb.current_run_started_ats(conn, [task_id]).get(task_id)
            return {
                "ok": True,
                "task": d,
                "comments": [
                    {"id": c.id, "author": c.author, "body": c.body, "created_at": c.created_at}
                    for c in kb.list_comments(conn, task_id)
                ],
                "events": [
                    {"id": e.id, "kind": e.kind, "payload": e.payload, "created_at": e.created_at}
                    for e in kb.list_events(conn, task_id)[-80:]
                ],
                "runs": [
                    {"id": r.id, "status": r.status, "outcome": r.outcome,
                     "summary": r.summary, "error": r.error,
                     "started_at": r.started_at, "ended_at": r.ended_at}
                    for r in kb.list_runs(conn, task_id)
                ],
            }
        finally:
            conn.close()

    try:
        data = await _in_thread(_run)
        return _json_response(data, 200 if data.get("ok") else 404)
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban task read failed")
        return _error(f"Could not read the task: {exc}", 500)


@require_key
async def _comment(request: web.Request) -> web.Response:
    task_id = request.match_info.get("task_id", "")
    try:
        body = await _read_json(request)
        text = str(body.get("body") or "").strip()
        if not text:
            return _error("comment body is required")
        author = str(body.get("author") or "mobile").strip() or "mobile"
        board = _board_param(request)

        def _run() -> dict[str, Any]:
            kb = _kb()
            conn = _open(board)
            try:
                kb.add_comment(conn, task_id, author, text)
                return {"ok": True}
            finally:
                conn.close()

        return _json_response(await _in_thread(_run))
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban comment failed")
        return _error(f"Could not post the comment: {exc}", 500)


@require_key
async def _action(request: web.Request) -> web.Response:
    task_id = request.match_info.get("task_id", "")
    try:
        body = await _read_json(request)
        action = str(body.get("action") or "").strip().lower()
        board = _board_param(request)

        def _run() -> dict[str, Any]:
            kb = _kb()
            conn = _open(board)
            try:
                task = kb.get_task(conn, task_id)
                if task is None:
                    return {"ok": False, "error": "task not found"}
                ok = False
                if action == "unblock":
                    ok = bool(kb.unblock_task(conn, task_id))
                elif action == "retry":
                    if task.status == "running":
                        kb.reclaim_task(conn, task_id, reason="retry from mobile")
                    ok = bool(kb.unblock_task(conn, task_id)) or task.status == "ready"
                elif action == "approve":
                    ok = bool(kb.complete_task(
                        conn, task_id, summary="Approved from Hermes Mobile", force=True))
                elif action == "archive":
                    ok = bool(kb.archive_task(conn, task_id))
                elif action == "delete":
                    # Hard-delete only for non-running cards (running ones must
                    # be reclaimed first). The deleted flag signals the caller
                    # to skip the post-action get_task() lookup.
                    deleted = bool(kb.delete_task(conn, task_id)) if task.status != "running" else False
                    return {"ok": True, "deleted": deleted, "applied": deleted}
                elif action == "reassign":
                    assignee = str(body.get("assignee") or "").strip() or None
                    ok = bool(kb.reassign_task(conn, task_id, assignee, reclaim_first=True))
                else:
                    return {"ok": False, "error": f"unknown action {action!r}"}
                updated = kb.get_task(conn, task_id)
                return {"ok": True, "applied": ok, "task": _task_dict(updated) if updated else None}
            finally:
                conn.close()

        return _json_response(await _in_thread(_run))
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban action failed")
        return _error(f"Could not apply the action: {exc}", 500)


@require_key
async def _events(request: web.Request) -> web.Response:
    try:
        since = _as_int(request.query.get("since"), 0)
        limit = max(1, min(_as_int(request.query.get("limit"), 200), 500))
        board = _board_param(request)

        def _run() -> dict[str, Any]:
            conn = _open(board)
            try:
                rows = conn.execute(
                    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, e.run_id, "
                    "       t.title AS task_title "
                    "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
                    "WHERE e.id > ? ORDER BY e.id ASC LIMIT ?",
                    (since, limit),
                ).fetchall()
                events = []
                for r in rows:
                    payload = r["payload"]
                    try:
                        payload = json.loads(payload) if payload else None
                    except (TypeError, ValueError):
                        payload = None
                    events.append({
                        "id": r["id"],
                        "task_id": r["task_id"],
                        "task_title": r["task_title"] or "",
                        "kind": r["kind"],
                        "payload": payload,
                        "created_at": r["created_at"],
                        "run_id": r["run_id"],
                    })
                latest = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
                ).fetchone()["m"]
                return {"ok": True, "events": events, "cursor": int(latest), "latest": int(latest)}
            finally:
                conn.close()

        return _json_response(await _in_thread(_run))
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban events failed")
        return _error(f"Could not read Kanban events: {exc}", 500)


@require_key
async def _assignees(request: web.Request) -> web.Response:
    try:
        board = _board_param(request)

        def _run() -> list[dict[str, Any]]:
            kb = _kb()
            conn = _open(board)
            try:
                return kb.known_assignees(conn)
            finally:
                conn.close()

        return _json_response({"ok": True, "assignees": await _in_thread(_run)})
    except KanbanUnavailable as exc:
        return _unavailable(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kanban assignees failed")
        return _error(f"Could not list assignees: {exc}", 500)


def register(app: web.Application) -> None:
    app.router.add_get("/api/mobile/kanban/mode", _mode_get)
    app.router.add_put("/api/mobile/kanban/mode", _mode_put)
    app.router.add_get("/api/mobile/kanban/boards", _boards)
    app.router.add_get("/api/mobile/kanban/board", _board)
    app.router.add_post("/api/mobile/kanban/tasks", _create_task)
    app.router.add_get("/api/mobile/kanban/tasks/{task_id}", _task_detail)
    app.router.add_post("/api/mobile/kanban/tasks/{task_id}/comments", _comment)
    app.router.add_post("/api/mobile/kanban/tasks/{task_id}/actions", _action)
    app.router.add_get("/api/mobile/kanban/events", _events)
    app.router.add_get("/api/mobile/kanban/assignees", _assignees)
    logger.info(
        "[hermes-mobile-plugin] kanban routes registered: GET/PUT /api/mobile/kanban/mode, "
        "GET /api/mobile/kanban/board, POST /api/mobile/kanban/tasks, "
        "GET/POST task detail/comments/actions, GET events/assignees"
    )
