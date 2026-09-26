"""会议归属状态（第一期 1b）：列表、详情、汇总共用的只读推导。

状态按顺序判断，前后端共用同一套：
  ① origin=manual 且有项目 → manual
  ② origin=manual 且没项目 → manual_none（人工标了「不归项目」）
  ③ 最新归属批次是 needs_review → needs_review（待你选）
  ④ origin=ai 且有项目 → auto
  ⑤ 没有批次，或最新批次还在 pending/running → ai_pending
  ⑥ 其余（unresolved 等）→ none；批次里记了像新项目的名字时 → new_project
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .project_linking import last_reassignment
from .project_names import also_entries
from .project_profile import norm_key
from .tasks import UNDO_WINDOW_SECONDS

ATTRIBUTION_STATES = (
    "needs_review",
    "ai_pending",
    "auto",
    "manual",
    "manual_none",
    "none",
    "new_project",
)
# 工作台「N 场会等你选项目」只数最近这么多天的会，更早的留在资料库筛选里。
REVIEW_RECENT_DAYS = 14
# 自动归属、被改正的次数按这么多天统计。
STATS_DAYS = 30

# 每场会最新的一条归属批次（id 自增，越大越新）。查询里会议表的别名必须是 m。
LATEST_LINK_JOIN = """LEFT JOIN project_links pl ON pl.id = (
    SELECT MAX(pl_latest.id) FROM project_links pl_latest WHERE pl_latest.meeting_id = m.id)"""

ATTRIBUTION_STATE_SQL = """CASE
    WHEN m.project_origin = 'manual' AND m.project_id IS NOT NULL THEN 'manual'
    WHEN m.project_origin = 'manual' THEN 'manual_none'
    WHEN pl.status = 'needs_review' THEN 'needs_review'
    WHEN m.project_id IS NOT NULL AND m.project_origin = 'ai' THEN 'auto'
    WHEN m.project_id IS NOT NULL THEN 'manual'
    WHEN pl.id IS NULL OR pl.status IN ('pending', 'running') THEN 'ai_pending'
    WHEN COALESCE(pl.new_project_name, '') != '' THEN 'new_project'
    ELSE 'none'
END"""


def attribution_state(
    *,
    project_id: str | None,
    origin: str | None,
    link_status: str | None,
    new_project_name: str | None,
) -> str:
    """与 ATTRIBUTION_STATE_SQL 同一套规则的 Python 版（link_status=None 表示没有批次）。"""
    if origin == "manual":
        return "manual" if project_id else "manual_none"
    if link_status == "needs_review":
        return "needs_review"
    if project_id:
        return "auto" if origin == "ai" else "manual"
    if link_status is None or link_status in ("pending", "running"):
        return "ai_pending"
    return "new_project" if new_project_name else "none"


def _json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _projects_by_id(connection: Any, project_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not project_ids:
        return {}
    ids = sorted(project_ids)
    rows = connection.execute(
        f"SELECT id, name, color FROM projects WHERE id IN ({', '.join('?' for _ in ids)})",
        ids,
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def resolve_candidates(
    connection: Any, candidates_json: Any, current_project_id: str | None
) -> list[dict[str, Any]]:
    """把批次里存的候选对到现有项目（删掉的项目跳过），最多 2 个。

    会议现在所属的项目排第一并标 current=True（复评时「原来的」那个）。
    """
    raw = [item for item in _json_list(candidates_json) if isinstance(item, dict)]
    projects = _projects_by_id(
        connection, {str(item.get("project_id")) for item in raw if item.get("project_id")}
    )
    candidates: list[dict[str, Any]] = []
    for item in raw:
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
                "current": project["id"] == current_project_id,
            }
        )
    candidates.sort(key=lambda item: not item["current"])
    return candidates[:2]


def _with_project_names(connection: Any, evidence: list[Any]) -> list[dict[str, Any]]:
    entries = [dict(entry) for entry in evidence if isinstance(entry, dict)]
    projects = _projects_by_id(
        connection, {entry["project_id"] for entry in entries if entry.get("project_id")}
    )
    for entry in entries:
        project = projects.get(entry.get("project_id") or "")
        entry["project_name"] = project["name"] if project else None
    return entries


def _reassigned_from(
    connection: Any, meeting_id: str, project_id: str | None
) -> dict[str, Any] | None:
    """「由 X 改来」：最近一次人工改归属，而且会议现在还在它改到的地方。"""
    last = last_reassignment(connection, meeting_id)
    if last is None:
        return None
    payload = last["payload"]
    if payload.get("to") != project_id:
        return None
    from_id = payload.get("from")
    project = _projects_by_id(connection, {from_id} if from_id else set()).get(from_id or "")
    at = datetime.fromisoformat(last["at"])
    undo_until = at + timedelta(seconds=UNDO_WINDOW_SECONDS)
    left_ids = [task_id for task_id in payload.get("left_task_ids") or [] if task_id]
    tasks_left: list[dict[str, Any]] = []
    if left_ids and from_id:
        tasks_left = [
            dict(row)
            for row in connection.execute(
                f"""SELECT t.id, t.title, t.requirement_id, r.title AS requirement_title
                      FROM tasks t JOIN requirements r ON r.id = t.requirement_id
                     WHERE t.id IN ({', '.join('?' for _ in left_ids)}) AND t.project_id = ?
                     ORDER BY t.created_at, t.id""",
                (*left_ids, from_id),
            ).fetchall()
        ]
    cue_hint = payload.get("cue_hint")
    if isinstance(cue_hint, dict) and cue_hint.get("term_id"):
        still_cue = connection.execute(
            "SELECT 1 FROM glossary_terms WHERE id=? AND is_cue=1", (cue_hint["term_id"],)
        ).fetchone()
        if still_cue is None:
            cue_hint = None
    else:
        cue_hint = None
    return {
        "project_id": from_id,
        "project_name": project["name"] if project else None,
        "origin_before": payload.get("origin_before"),
        "at": last["at"],
        "undo_until": undo_until.isoformat(),
        "can_undo": datetime.now(UTC) <= undo_until,
        "tasks_left": tasks_left,
        "cue_hint": cue_hint,
    }


def meeting_attribution(
    connection: Any, meeting_id: str, *, ai_configured: bool
) -> dict[str, Any] | None:
    """会议详情里的 attribution 对象。"""
    row = connection.execute(
        f"""SELECT m.project_id, m.project_origin,
                   pl.id AS link_id, pl.status AS link_status, pl.method, pl.reason,
                   pl.evidence_json, pl.candidates_json, pl.new_project_name,
                   {ATTRIBUTION_STATE_SQL} AS state
              FROM meetings m {LATEST_LINK_JOIN}
             WHERE m.id = ?""",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return None
    state = row["state"]
    method = "manual" if row["project_origin"] == "manual" else row["method"]
    return {
        "state": state,
        "project_id": row["project_id"],
        "origin": row["project_origin"],
        "method": method,
        "evidence": _with_project_names(connection, _json_list(row["evidence_json"])),
        "candidates": (
            resolve_candidates(connection, row["candidates_json"], row["project_id"])
            if state == "needs_review"
            else []
        ),
        "reason": row["reason"] or "",
        "new_project_name": row["new_project_name"] if state == "new_project" else None,
        "reassigned_from": (
            _reassigned_from(connection, meeting_id, row["project_id"])
            if row["project_origin"] == "manual"
            else None
        ),
        "ai_configured": ai_configured,
    }


def decorate_meeting_rows(connection: Any, rows: list[dict[str, Any]]) -> None:
    """列表行：查询里已带 attribution_state、_candidates_json、_new_project_name，就地整理。"""
    for row in rows:
        candidates_json = row.pop("_candidates_json", None)
        new_project_name = row.pop("_new_project_name", None)
        state = row.get("attribution_state")
        row["candidates"] = (
            resolve_candidates(connection, candidates_json, row.get("project_id"))
            if state == "needs_review"
            else []
        )
        row["new_project_name"] = new_project_name if state == "new_project" else None


def attribution_summary(connection: Any) -> dict[str, Any]:
    """工作台上「N 场会等你选项目」（近 14 天）、「像新项目」和近 30 天自动归属、被改正的次数。"""
    now = datetime.now(UTC)
    since = (now - timedelta(days=STATS_DAYS)).isoformat()
    since_date = (now - timedelta(days=REVIEW_RECENT_DAYS)).isoformat()[:10]
    states = connection.execute(
        f"""SELECT m.id, m.title, m.recording_date, m.created_at,
                   pl.new_project_name, {ATTRIBUTION_STATE_SQL} AS state
              FROM meetings m {LATEST_LINK_JOIN}"""
    ).fetchall()
    needs_review_total = 0
    needs_review_recent = 0
    names: dict[str, dict[str, Any]] = {}
    for row in states:
        when = row["recording_date"] or (row["created_at"] or "")[:10]
        if row["state"] == "needs_review":
            needs_review_total += 1
            if when >= since_date:
                needs_review_recent += 1
        elif row["state"] == "new_project":
            key = norm_key(row["new_project_name"])
            if not key:
                continue
            bucket = names.setdefault(
                key,
                {"name": row["new_project_name"], "meeting_ids": [], "last_at": ""},
            )
            bucket["meeting_ids"].append(row["id"])
            if when > bucket["last_at"]:
                bucket["last_at"] = when
                bucket["name"] = row["new_project_name"]
    # 用户说过「不是新项目」的名字、已经有同名项目的名字都不再提。
    taken = {
        row["norm_key"] for row in connection.execute("SELECT norm_key FROM name_decisions")
    } | {norm_key(row["name"]) for row in connection.execute("SELECT name FROM projects")}
    new_project_names = [
        {
            "name": bucket["name"],
            "norm_key": key,
            "meeting_count": len(bucket["meeting_ids"]),
            "meeting_ids": bucket["meeting_ids"],
            "last_at": bucket["last_at"],
        }
        for key, bucket in names.items()
        if key not in taken
    ]
    new_project_names.sort(key=lambda item: item["last_at"], reverse=True)
    new_project_names.sort(key=lambda item: item["meeting_count"], reverse=True)
    auto_30d = connection.execute(
        """SELECT COUNT(DISTINCT meeting_id) AS n FROM events
            WHERE event_type='meeting_project_auto_assigned' AND created_at >= ?""",
        (since,),
    ).fetchone()["n"]
    corrected_30d = connection.execute(
        """SELECT COUNT(DISTINCT meeting_id) AS n FROM events
            WHERE event_type='meeting_project_reassigned' AND created_at >= ?
              AND json_extract(payload_json, '$.origin_before') = 'ai'
              AND NOT EXISTS (
                  SELECT 1 FROM events u
                   WHERE u.event_type='meeting_project_reassign_undone'
                     AND u.meeting_id = events.meeting_id
                     AND json_extract(u.payload_json, '$.event_id') = events.id)""",
        (since,),
    ).fetchone()["n"]
    return {
        "needs_review_recent": needs_review_recent,
        "needs_review_total": needs_review_total,
        "new_project_names": new_project_names,
        "auto_30d": int(auto_30d),
        "corrected_30d": int(corrected_30d),
    }


def recognition_profile(connection: Any, project_id: str) -> dict[str, Any]:
    """项目详情「系统怎么认出这个项目」：叫法、文件夹名、项目词、近 30 天的归属情况。"""
    project = connection.execute(
        "SELECT also_names FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    folder_names = list(
        dict.fromkeys(
            Path(row["path"]).name
            for row in connection.execute(
                "SELECT path FROM project_material_roots WHERE project_id=? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        )
    )
    terms = connection.execute(
        """SELECT COUNT(*) AS total, COALESCE(SUM(is_cue), 0) AS cue
             FROM glossary_terms WHERE project_id=?""",
        (project_id,),
    ).fetchone()
    since = (datetime.now(UTC) - timedelta(days=STATS_DAYS)).isoformat()
    auto_30d = connection.execute(
        """SELECT COUNT(DISTINCT meeting_id) AS n FROM events
            WHERE event_type='meeting_project_auto_assigned' AND created_at >= ?
              AND json_extract(payload_json, '$.project_id') = ?""",
        (since, project_id),
    ).fetchone()["n"]
    corrected_30d = connection.execute(
        """SELECT COUNT(DISTINCT meeting_id) AS n FROM events
            WHERE event_type='meeting_project_reassigned' AND created_at >= ?
              AND json_extract(payload_json, '$.origin_before') = 'ai'
              AND json_extract(payload_json, '$.from') = ?
              AND NOT EXISTS (
                  SELECT 1 FROM events u
                   WHERE u.event_type='meeting_project_reassign_undone'
                     AND u.meeting_id = events.meeting_id
                     AND json_extract(u.payload_json, '$.event_id') = events.id)""",
        (since, project_id),
    ).fetchone()["n"]
    return {
        "also_names": also_entries(project["also_names"]) if project else [],
        "folder_names": folder_names,
        "cue_terms": {"total": int(terms["total"]), "cue": int(terms["cue"])},
        "auto_30d": int(auto_30d),
        "corrected_30d": int(corrected_30d),
    }
