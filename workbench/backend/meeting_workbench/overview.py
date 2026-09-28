"""全部项目概览（第二期 2c）：项目岛、港湾（没归项目的会）、像新项目的名字、跨项目的线。

只查库，SQL 条数固定（8 条）；口径和项目图的状态句一致：
  review   已归这个项目、归属待复核的会（不分时间窗）
  doorstep 没归项目、窗口内待你选且候选里有它的会
  tasks    这个项目的会上待确认的任务
  stopped_cards 停了的卡片（卡片开着、项目没暂停、挂了文件夹时才算）
文件夹缓存的部分（还没挂的文件夹）走 overview_folders，不在这里读。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from .attribution import ATTRIBUTION_STATE_SQL, LATEST_LINK_JOIN, group_new_project_names
from .graph import (
    GRAPH_API_VERSION,
    WINDOWS,
    _age,
    _in_window,
    _json_list,
    _OPEN,
    _short_hash,
    card_category,
    graph_rev,
    local_day,
)
from .name_hints import HintContext
from .project_folders import CHECKING, parent_status, pending_folders

DEFAULT_WINDOW = "28d"
HARBOUR_RECENT = 8
# 项目数超过这么多时，窗口内没有会的项目折成「其余 N 个项目」。
MAX_ISLANDS = 40
MAX_FOLDERS = 12
HARBOUR_STATES = ("ai_pending", "needs_review", "none", "new_project")


def overview_etag(connection: Any, window: str, today: date, ai_configured: bool) -> str:
    digest = _short_hash(
        "overview",
        str(GRAPH_API_VERSION),
        window,
        today.isoformat(),
        "ai" if ai_configured else "no-ai",
        str(graph_rev(connection)),
    )
    return f'W/"o-{digest}"'


def _candidates(
    raw: Any, projects: dict[str, dict[str, Any]], current: str | None
) -> list[dict[str, Any]]:
    """和资料库那一行同一个形状（attribution.resolve_candidates），只是不再查库。"""
    candidates: list[dict[str, Any]] = []
    for item in _json_list(raw):
        if not isinstance(item, dict):
            continue
        project = projects.get(str(item.get("project_id")))
        if project is None or any(c["project_id"] == project["id"] for c in candidates):
            continue
        candidates.append(
            {
                "project_id": project["id"],
                "project_name": project["name"],
                "project_color": project["color"],
                "count": int(item.get("count") or 0),
                "llm": bool(item.get("llm")),
                "current": project["id"] == current,
            }
        )
    candidates.sort(key=lambda item: not item["current"])
    return candidates[:2]


def overview(
    connection: Any,
    *,
    window: str = DEFAULT_WINDOW,
    ai_configured: bool,
    today: date | None = None,
) -> dict[str, Any]:
    if window not in WINDOWS:
        raise ValueError("时间窗只能是 7d、28d、90d 或 all")
    today = today or datetime.now().astimezone().date()
    days = WINDOWS[window]

    # ① 项目和它进行中的需求数
    projects = {
        row["id"]: dict(row)
        for row in connection.execute(
            """SELECT p.id, p.name, p.color, p.created_at,
                      (SELECT COUNT(*) FROM requirements r
                        WHERE r.project_id = p.id AND r.status = 'active') AS requirements_active
                 FROM projects p ORDER BY p.created_at, p.id"""
        ).fetchall()
    }
    # ② 全部会议：归属状态、最新批次的候选和新名字、待确认任务数、卡片台账
    meetings = [
        dict(row)
        for row in connection.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.created_at, m.project_id, m.project_origin,
                       {ATTRIBUTION_STATE_SQL} AS state,
                       pl.candidates_json, pl.new_project_name, pl.new_requirement_name,
                       pl.new_name_project_id, pl.new_name_spoken,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.meeting_id = m.id AND t.status = 'pending_confirm') AS pending_tasks,
                       c.state AS card_state, c.reason AS card_reason, c.error AS card_error,
                       c.synced_at AS card_synced_at, c.project_id AS card_project_id
                  FROM meetings m {LATEST_LINK_JOIN}
                  LEFT JOIN meeting_cards c ON c.meeting_id = m.id"""
        ).fetchall()
    ]
    # ③ 材料根目录
    roots: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT id, project_id, path FROM project_material_roots ORDER BY created_at, id"
    ).fetchall():
        roots.setdefault(row["project_id"], []).append(
            {"id": row["id"], "name": Path(row["path"]).name or row["path"]}
        )
    # ④ 等补建的文件夹
    pending = pending_folders(connection)
    # ⑤ 跨项目的线：需求关联了别的项目的会、未完成任务和会议不在同一个项目
    bridge_rows = connection.execute(
        f"""SELECT r.project_id AS a, m.project_id AS b
              FROM requirement_meetings rm
              JOIN requirements r ON r.id = rm.requirement_id
              JOIN meetings m ON m.id = rm.meeting_id
             WHERE m.project_id IS NOT NULL AND r.project_id != m.project_id
            UNION ALL
            SELECT t.project_id AS a, m.project_id AS b
              FROM tasks t JOIN meetings m ON m.id = t.meeting_id
             WHERE t.status IN ({_OPEN}) AND t.project_id IS NOT NULL
               AND m.project_id IS NOT NULL AND t.project_id != m.project_id"""
    ).fetchall()
    # ⑥ 卡片开关
    state = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key, value FROM app_state WHERE key IN ('cards_enabled', 'cards_paused_projects')"
        ).fetchall()
    }
    cards_enabled = state.get("cards_enabled") != "0"
    paused = {str(item) for item in _json_list(state.get("cards_paused_projects"))}
    # ⑦⑧ 名字占用和决定（HintContext 固定查两条）
    hints = HintContext(connection)

    islands: dict[str, dict[str, Any]] = {}
    for project_id, project in projects.items():
        islands[project_id] = {
            "id": project_id,
            "name": project["name"],
            "color": project["color"],
            "meetings": 0,
            "meetings_total": 0,
            "last_day": None,
            "requirements_active": int(project["requirements_active"] or 0),
            "waiting": {"review": 0, "doorstep": 0, "tasks": 0},
            "stopped_cards": 0,
            "roots": roots.get(project_id, []),
            "pending_folder": pending.get(project_id),
        }
    harbour_counts = dict.fromkeys(HARBOUR_STATES, 0)
    harbour_rows: list[dict[str, Any]] = []
    for row in meetings:
        day = local_day(row["recording_date"], row["created_at"])
        inside = _in_window(_age(day, today), days)
        project_id = row["project_id"]
        island = islands.get(project_id) if project_id else None
        if island is not None:
            island["meetings_total"] += 1
            island["meetings"] += int(inside)
            if island["last_day"] is None or day.isoformat() > island["last_day"]:
                island["last_day"] = day.isoformat()
            if row["state"] == "needs_review":
                island["waiting"]["review"] += 1
            island["waiting"]["tasks"] += int(row["pending_tasks"] or 0)
            if (
                cards_enabled
                and project_id not in paused
                and island["roots"]
                and card_category(row)[0] == "stopped"
            ):
                island["stopped_cards"] += 1
            continue
        if project_id is not None or row["project_origin"] == "manual" or not inside:
            continue
        candidates = _candidates(row["candidates_json"], projects, None)
        if row["state"] == "needs_review":
            for candidate in candidates:
                islands[candidate["project_id"]]["waiting"]["doorstep"] += 1
        if row["state"] in harbour_counts:
            harbour_counts[row["state"]] += 1
        harbour_rows.append(
            {
                "id": row["id"],
                "title": row["title"],
                "day": day.isoformat(),
                "state": row["state"],
                "candidates": candidates if row["state"] == "needs_review" else [],
                "name_hint": hints.hint(
                    project_id=None,
                    state=row["state"],
                    new_project_name=row["new_project_name"],
                    new_requirement_name=row["new_requirement_name"],
                    new_name_project_id=row["new_name_project_id"],
                    new_name_spoken=row["new_name_spoken"],
                ),
                "_sort": (day.isoformat(), row["created_at"] or "", row["id"]),
            }
        )
    harbour_rows.sort(key=lambda item: item["_sort"], reverse=True)
    recent = harbour_rows[:HARBOUR_RECENT]
    for item in recent:
        item.pop("_sort")

    ordered = list(islands.values())
    more: dict[str, Any] | None = None
    if len(ordered) > MAX_ISLANDS:
        quiet = [island for island in ordered if island["meetings"] == 0]
        keep = len(ordered) - len(quiet)
        # 窗口内有会的全留；空位按创建先后给没会的项目，剩下的折起来。
        room = max(0, MAX_ISLANDS - keep)
        folded = {island["id"] for island in quiet[room:]}
        if folded:
            ordered = [island for island in ordered if island["id"] not in folded]
            more = {"count": len(folded), "project_ids": [pid for pid in islands if pid in folded]}

    pairs: dict[tuple[str, str], int] = {}
    for row in bridge_rows:
        if row["a"] not in projects or row["b"] not in projects:
            continue
        a, b = sorted((row["a"], row["b"]))
        pairs[(a, b)] = pairs.get((a, b), 0) + 1
    bridges = [
        {"a": a, "b": b, "count": count}
        for (a, b), count in sorted(pairs.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "window": {"effective": window, "days": days},
        "today": today.isoformat(),
        "islands": ordered,
        "islands_more": more,
        "harbour": {
            "total": sum(harbour_counts.values()),
            "counts": harbour_counts,
            "recent": recent,
            "ai_configured": ai_configured,
        },
        "suggested_projects": [
            {
                "key": item["norm_key"],
                "name": item["name"],
                "meeting_count": item["meeting_count"],
                "meeting_ids": item["meeting_ids"],
                "last_at": item["last_at"],
            }
            for item in group_new_project_names(meetings, hints.taken_project_keys)
        ],
        "bridges": bridges,
    }


def overview_folders(connection: Any, settings: Any, cache: Any) -> dict[str, Any]:
    """GET /api/graph/overview/folders：项目总文件夹下还没挂的文件夹（读缓存），最多 12 个，
    按修改时间倒序。每个带认领框同样的默认动作，灰色岛上的按钮走 2a 的认领接口。"""
    status = parent_status(connection, settings, cache)
    unclaimed = status["unclaimed"]
    state = {
        "unset": "unset",
        CHECKING: "checking",
        "ready": "ready",
        "invalid": "conflict",
        "conflict": "conflict",
    }.get(unclaimed["state"], "offline")
    folders = sorted(
        unclaimed["folders"],
        key=lambda folder: (folder["modified_at"], folder["name"]),
        reverse=True,
    )
    return {
        "state": state,
        "parent": {"path": status["path"], "state": status["state"], "reason": status["reason"]}
        if status["path"]
        else None,
        "suggested": status["suggested"],
        "folders": folders[:MAX_FOLDERS],
        "more": max(0, len(folders) - MAX_FOLDERS),
    }
