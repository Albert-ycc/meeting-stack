"""关系图（1g）：以项目为中心的星图数据。

图接口只查库、不读盘，语句条数固定（不随会议数增长）：资料盘在线状态和根目录散放文件由
RootsCache 在后台每 30 秒刷新一次，另走 /roots 接口，盘休眠或被拔掉时画布照样秒开。

方向是类型，远近是新旧：会议在左、材料在右、进行中的需求在上、线索词在下；内圈最近 7 天、
中圈最近 28 天、外圈更早。三圈都按滚动天数算，录音日期先统一转成本地时区再算。这里只决定
每个节点在哪个方向、哪一圈、同圈里的先后，以及哪些折叠起来；坐标由前端的 layoutStarMap 算。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .attribution import ATTRIBUTION_STATE_SQL, LATEST_LINK_JOIN
from .cards import BLOCKED, MISSING, STOP_REASONS, SYNCED, USER_EDITED
from .db import GRAPH_REV_KEY
from .materials import CARDS_DIR_NAME, ROOT_ONLINE, volume_state
from .notify import (
    _ANCHOR,
    _DECISION_SECTION,
    _LIST_ITEM,
    _SUBHEADING,
    _SUMMARY_SECTION,
    _clean_item,
    _section_body,
    _truncate,
)
from .tasks import OPEN_TASK_STATUSES, UNDO_WINDOW_SECONDS

logger = logging.getLogger(__name__)

# 前端缓存按这个版本失效：接口字段改了就加一，免得浏览器拿旧 ETag 命中旧结构。
GRAPH_API_VERSION = 1

WINDOWS: dict[str, int | None] = {"7d": 7, "28d": 28, "90d": 90, "all": None}
DEFAULT_WINDOW = "28d"
WIDEN_ORDER = ("7d", "28d", "90d", "all")
# 默认 28 天里不足这么多场会时自动放宽。
AUTO_WIDEN_MIN = 3

INNER_DAYS = 7
MIDDLE_DAYS = 28
# 需求多久没动静算「漂到外圈」。
STALE_DAYS = 28

INNER_CAP = 8
MIDDLE_CAP = 10
MIDDLE_MIN = 4
DOORSTEP_CAP = 3
REQUIREMENT_CAP = 10
REQUIREMENT_MIN = 5
FOLDER_CAP = 8
FOLDER_MIN = 4
CUE_CAP = 8
CUE_MIN = 4
CUE_MIN_COUNT = 2
MONTH_CLUSTER_CAP = 6
BEACON_CAP = 6
VISIBLE_BUDGET = 40
WEEKS = 12

_OPEN = ", ".join(f"'{status}'" for status in OPEN_TASK_STATUSES)
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
CUE_SOURCES = ("term", "folder")

CARD_REASON_LABELS = {
    "waiting_minutes": "等纪要写好",
    "waiting_project": "等归项目",
    "needs_review": "等你确认归属",
    "not_backfilled": "上线前的会，要补写时在项目页点一下",
    "no_root": "项目还没挂材料文件夹",
    "root_offline": "资料盘未连接",
    "root_missing": "材料文件夹找不到了",
    "root_in_archive": "材料文件夹在声档归档目录里",
    "root_shared": "材料文件夹同时挂在别的项目下",
    "paused": "这个项目的卡片暂停了",
    "disabled": "会议卡片整体关掉了",
    "queued": "排队写入",
}


class GraphNotFound(LookupError):
    pass


# ---------------------------------------------------------------------- 日期


def local_day(recording_date: str | None, created_at: str | None) -> date:
    """会议的本地日期。录音日期没带时区的按本机时区，created_at 没带时区的按 UTC。"""
    for raw, naive_is_utc in ((recording_date, False), (created_at, True)):
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC) if naive_is_utc else value.astimezone()
        return value.astimezone().date()
    return datetime.now().astimezone().date()


def _age(day: date, today: date) -> int:
    return max(0, (today - day).days)


def _ring_for_age(age: int) -> str:
    if age < INNER_DAYS:
        return "inner"
    if age < MIDDLE_DAYS:
        return "middle"
    return "outer"


def _in_window(age: int, days: int | None) -> bool:
    return days is None or age < days


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:10]


def _json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------- ETag


def graph_rev(connection: Any) -> int:
    row = connection.execute(
        "SELECT value FROM app_state WHERE key=?", (GRAPH_REV_KEY,)
    ).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def graph_etag(
    connection: Any, project_id: str, window: str | None, focus: str | None, today: date
) -> str:
    """项目、时间窗、深链目标、今天的日期和持久版本号的 hash；资料盘状态不算在内。"""
    digest = _short_hash(
        str(GRAPH_API_VERSION),
        project_id,
        window or "",
        focus or "",
        today.isoformat(),
        str(graph_rev(connection)),
    )
    return f'W/"g-{digest}"'


# ---------------------------------------------------------------------- 归属证据


def _project_literals(evidence: list[Any], project_id: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in evidence
        if isinstance(entry, dict)
        and entry.get("kind") == "literal"
        and entry.get("project_id") == project_id
    ]


def attribution_label(
    *,
    state: str,
    origin: str | None,
    last_decision: str | None,
    evidence: list[Any],
    project_id: str,
) -> tuple[str, str]:
    """归属线上的字和来源：(label, source)。source：review / confirmed / manual / ai / legacy。"""
    if state == "needs_review":
        return "待复核：AI 拿不准是不是这个项目", "review"
    if origin == "manual" or (origin not in ("ai", None) and state == "manual"):
        if last_decision == "meeting_project_confirmed":
            return "你确认过", "confirmed"
        return "你归的", "manual"
    if origin is None:
        return "你归的", "manual"
    literals = sorted(
        _project_literals(evidence, project_id),
        key=lambda entry: int(entry.get("count") or 0),
        reverse=True,
    )
    if literals:
        top = literals[0]
        return f"自动 · 提到『{top.get('cue')}』{int(top.get('count') or 0)} 次", "ai"
    llm = next(
        (
            entry
            for entry in evidence
            if isinstance(entry, dict)
            and entry.get("kind") == "llm"
            and entry.get("project_id") == project_id
        ),
        None,
    )
    if llm is not None:
        reason = _truncate(str(llm.get("reason") or "").strip(), 30)
        return (f"自动 · AI 判断：{reason}" if reason else "自动 · AI 判断"), "ai"
    return "AI 归的 · 旧规则，没记原话", "legacy"


# ---------------------------------------------------------------------- 卡片


def card_category(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """只看卡片台账（不读盘）：(ok / waiting / stopped / None, 原因的人话)。没有台账行不画角标。"""
    state = row.get("card_state")
    if state is None:
        return None, None
    reason = row.get("card_reason")
    if row.get("card_project_id") != row.get("project_id"):
        return "waiting", CARD_REASON_LABELS.get("queued")
    if state == SYNCED and not row.get("card_error"):
        synced = row.get("card_synced_at")
        return "ok", f"已写入 {_clock(synced)}" if synced else "已写入"
    if state == USER_EDITED:
        return "stopped", "你改过纪要部分，已停止更新"
    if state == MISSING:
        return "stopped", "卡片被移走或删了"
    if row.get("card_error"):
        return "stopped", "上次写入失败"
    if state == BLOCKED and reason in STOP_REASONS:
        return "stopped", CARD_REASON_LABELS.get(reason, "写不了")
    return "waiting", CARD_REASON_LABELS.get(reason or "queued", "排队写入")


def _clock(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone()
    return f"{local.month}/{local.day} {local:%H:%M}"


# ---------------------------------------------------------------------- 整张图


@dataclass
class _Context:
    project: dict[str, Any]
    projects: dict[str, dict[str, Any]]
    today: date
    now: datetime
    window_key: str
    window_days: int | None
    meetings: list[dict[str, Any]] = field(default_factory=list)


def _select_window(
    requested: str | None,
    ages: list[int],
    focus_age: int | None,
) -> tuple[str, str | None]:
    """返回 (实际时间窗, 放宽原因)。只有没指定时间窗时才按会数自动放宽；深链目标在窗口外时
    放宽到刚好能包含它的窗口。"""
    key = requested if requested in WINDOWS else DEFAULT_WINDOW
    reason: str | None = None
    if requested is None:
        inside = sum(1 for age in ages if _in_window(age, WINDOWS[key]))
        if inside < AUTO_WIDEN_MIN:
            chosen = None
            for candidate in WIDEN_ORDER[WIDEN_ORDER.index(key) + 1:]:
                widened = sum(1 for age in ages if _in_window(age, WINDOWS[candidate]))
                if widened >= AUTO_WIDEN_MIN or (candidate == "all" and widened > inside):
                    chosen = candidate
                    break
            if chosen is not None:
                reason = f"自动放宽到{_window_label(chosen)}：{WINDOWS[key]} 天内只有 {inside} 场会"
                key = chosen
    if focus_age is not None and not _in_window(focus_age, WINDOWS[key]):
        for candidate in WIDEN_ORDER[WIDEN_ORDER.index(key) + 1:]:
            if _in_window(focus_age, WINDOWS[candidate]):
                reason = f"放宽到{_window_label(candidate)}：要看的会在 {WINDOWS[key]} 天以外"
                key = candidate
                break
    return key, reason


def _window_label(key: str) -> str:
    return "全部" if WINDOWS[key] is None else f" {WINDOWS[key]} 天"


def _cue_id(source: str, text: str) -> str:
    return f"cue:{_short_hash(source, text.casefold())}"


def project_graph(
    connection: Any,
    project_id: str,
    *,
    window: str | None = None,
    focus: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """一次取回整张项目图。window=None 表示用默认时间窗且允许自动放宽。"""
    if window is not None and window not in WINDOWS:
        raise ValueError("时间窗只能是 7d、28d、90d 或 all")
    today = today or datetime.now().astimezone().date()
    now = now or datetime.now(UTC)

    # ① 全部项目（候选、信标要项目名和颜色）
    projects = {
        row["id"]: dict(row)
        for row in connection.execute("SELECT id, name, color FROM projects").fetchall()
    }
    project = projects.get(project_id)
    if project is None:
        raise GraphNotFound("项目不存在")

    # ② 全局开关
    state = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key, value FROM app_state WHERE key IN ('cards_enabled', 'cards_paused_projects')"
        ).fetchall()
    }
    cards_on = state.get("cards_enabled") != "0" and project_id not in {
        str(item) for item in _json_list(state.get("cards_paused_projects"))
    }

    # ③ 本项目的会：归属状态、最近一条 AI 判断过的批次的证据、任务数、卡片台账、最近一次拍板
    meeting_rows = [
        dict(row)
        for row in connection.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.created_at, m.duration_ms,
                       m.project_id, m.project_origin,
                       m.current_minutes_version_id IS NOT NULL AS has_minutes,
                       {ATTRIBUTION_STATE_SQL} AS state,
                       ev.evidence_json AS evidence_json,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.meeting_id = m.id AND t.status IN ({_OPEN})) AS open_tasks,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.meeting_id = m.id AND t.status = 'pending_confirm') AS pending_tasks,
                       c.state AS card_state, c.reason AS card_reason, c.error AS card_error,
                       c.synced_at AS card_synced_at, c.project_id AS card_project_id,
                       (SELECT e.event_type FROM events e
                         WHERE e.meeting_id = m.id
                           AND e.event_type IN ('meeting_project_confirmed',
                                                'meeting_project_reassigned')
                         ORDER BY e.id DESC LIMIT 1) AS last_decision
                  FROM meetings m {LATEST_LINK_JOIN}
                  LEFT JOIN project_links ev ON ev.id = (
                      SELECT MAX(x.id) FROM project_links x
                       WHERE x.meeting_id = m.id AND x.status IN ('done', 'needs_review')
                         AND x.evidence_json IS NOT NULL)
                  LEFT JOIN meeting_cards c ON c.meeting_id = m.id
                 WHERE m.project_id = ?""",
            (project_id,),
        ).fetchall()
    ]

    # ④ 没归项目的会：门口（待你选且候选里有本项目）和「还在判断归属」的新会
    unattributed = [
        dict(row)
        for row in connection.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.created_at,
                       pl.id AS link_id, pl.status AS link_status, pl.candidates_json,
                       pl.evidence_json, pl.reason
                  FROM meetings m {LATEST_LINK_JOIN}
                 WHERE m.project_id IS NULL AND COALESCE(m.project_origin, '') != 'manual'
                   AND (pl.id IS NULL OR pl.status IN ('pending', 'running', 'needs_review'))"""
        ).fetchall()
    ]

    # ⑤ 进行中的需求
    requirement_rows = [
        dict(row)
        for row in connection.execute(
            f"""SELECT r.id, r.title, r.priority, r.status, r.created_at, r.updated_at,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.requirement_id = r.id AND t.status IN ({_OPEN})) AS open_tasks,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.requirement_id = r.id AND t.status = 'pending_confirm')
                           AS pending_tasks,
                       (SELECT MAX(t.status_changed_at) FROM tasks t
                         WHERE t.requirement_id = r.id AND t.status != 'expired') AS task_activity,
                       (SELECT MAX(rm.created_at) FROM requirement_meetings rm
                         WHERE rm.requirement_id = r.id) AS link_activity,
                       (SELECT MAX(COALESCE(m.recording_date, m.created_at))
                          FROM requirement_meetings rm JOIN meetings m ON m.id = rm.meeting_id
                         WHERE rm.requirement_id = r.id) AS meeting_activity
                  FROM requirements r
                 WHERE r.project_id = ? AND r.status = 'active'""",
            (project_id,),
        ).fetchall()
    ]

    # ⑥ 会议和需求的关联（任意一头在本项目）
    link_rows = [
        dict(row)
        for row in connection.execute(
            """SELECT rm.requirement_id, rm.meeting_id, rm.created_at,
                      r.project_id AS requirement_project_id, r.title AS requirement_title,
                      r.status AS requirement_status,
                      m.project_id AS meeting_project_id, m.title AS meeting_title
                 FROM requirement_meetings rm
                 JOIN requirements r ON r.id = rm.requirement_id
                 JOIN meetings m ON m.id = rm.meeting_id
                WHERE r.project_id = ? OR m.project_id = ?""",
            (project_id, project_id),
        ).fetchall()
    ]

    # ⑦ 需求文件夹
    folder_rows = [
        dict(row)
        for row in connection.execute(
            """SELECT f.id, f.requirement_id, f.path FROM requirement_folders f
                 JOIN requirements r ON r.id = f.requirement_id
                WHERE r.project_id = ? AND r.status = 'active'
                ORDER BY f.created_at, f.id""",
            (project_id,),
        ).fetchall()
    ]

    # ⑧ 材料根目录
    root_rows = [
        dict(row)
        for row in connection.execute(
            """SELECT id, path FROM project_material_roots WHERE project_id = ?
                ORDER BY created_at, id""",
            (project_id,),
        ).fetchall()
    ]

    # ⑨ 项目不一致的未完成任务
    cross_task_rows = [
        dict(row)
        for row in connection.execute(
            f"""SELECT t.id, t.title, t.project_id, t.meeting_id,
                       m.project_id AS meeting_project_id, m.title AS meeting_title
                  FROM tasks t JOIN meetings m ON m.id = t.meeting_id
                 WHERE t.status IN ({_OPEN}) AND t.project_id IS NOT NULL
                   AND m.project_id IS NOT NULL AND t.project_id != m.project_id
                   AND (t.project_id = ? OR m.project_id = ?)""",
            (project_id, project_id),
        ).fetchall()
    ]

    # ⑩ 撤销期内从本项目移走的会
    since = (now - timedelta(seconds=UNDO_WINDOW_SECONDS)).isoformat()
    moved_rows = [
        dict(row)
        for row in connection.execute(
            """SELECT e.id, e.meeting_id, e.created_at, e.payload_json,
                      m.title, m.project_id AS now_project_id
                 FROM events e JOIN meetings m ON m.id = e.meeting_id
                WHERE e.event_type = 'meeting_project_reassigned' AND e.created_at >= ?
                  AND json_extract(e.payload_json, '$.from') = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM events u
                       WHERE u.event_type = 'meeting_project_reassign_undone'
                         AND u.meeting_id = e.meeting_id
                         AND json_extract(u.payload_json, '$.event_id') = e.id)
                ORDER BY e.id DESC""",
            (since, project_id),
        ).fetchall()
    ]

    # ⑪ 本项目的词条（线索词要看它还参不参与识别）
    terms = {
        row["id"]: dict(row)
        for row in connection.execute(
            "SELECT id, term, is_cue FROM glossary_terms WHERE project_id = ?", (project_id,)
        ).fetchall()
    }

    return _assemble(
        project=project,
        projects=projects,
        cards_on=cards_on and bool(root_rows),
        meeting_rows=meeting_rows,
        unattributed=unattributed,
        requirement_rows=requirement_rows,
        link_rows=link_rows,
        folder_rows=folder_rows,
        root_rows=root_rows,
        cross_task_rows=cross_task_rows,
        moved_rows=moved_rows,
        terms=terms,
        window=window,
        focus=focus,
        today=today,
        now=now,
    )


def _assemble(
    *,
    project: dict[str, Any],
    projects: dict[str, dict[str, Any]],
    cards_on: bool,
    meeting_rows: list[dict[str, Any]],
    unattributed: list[dict[str, Any]],
    requirement_rows: list[dict[str, Any]],
    link_rows: list[dict[str, Any]],
    folder_rows: list[dict[str, Any]],
    root_rows: list[dict[str, Any]],
    cross_task_rows: list[dict[str, Any]],
    moved_rows: list[dict[str, Any]],
    terms: dict[str, dict[str, Any]],
    window: str | None,
    focus: str | None,
    today: date,
    now: datetime,
) -> dict[str, Any]:
    project_id = project["id"]

    # ---- 会议：本地日期、年龄
    for row in meeting_rows:
        row["day"] = local_day(row["recording_date"], row["created_at"])
        row["age"] = _age(row["day"], today)
        row["evidence"] = _json_list(row.pop("evidence_json", None))
    meeting_rows.sort(key=lambda row: (row["day"], row["created_at"] or "", row["id"]), reverse=True)
    by_meeting = {row["id"]: row for row in meeting_rows}

    focus_age = None
    if focus and focus.startswith("m:") and focus[2:] in by_meeting:
        focus_age = by_meeting[focus[2:]]["age"]
    window_key, widened_reason = _select_window(
        window, [row["age"] for row in meeting_rows], focus_age
    )
    days = WINDOWS[window_key]

    # ---- 门口与「还在判断归属」
    doorstep_all: list[dict[str, Any]] = []
    pending_attribution = 0
    for row in unattributed:
        day = local_day(row["recording_date"], row["created_at"])
        age = _age(day, today)
        if not _in_window(age, days):
            continue
        if row["link_status"] == "needs_review":
            candidates = _resolve_candidates(row["candidates_json"], projects)
            if not any(item["project_id"] == project_id for item in candidates):
                continue
            # 本项目排第一，另一个候选排第二
            candidates.sort(key=lambda item: item["project_id"] != project_id)
            doorstep_all.append(
                {
                    "id": f"d:{row['id']}",
                    "meeting_id": row["id"],
                    "title": row["title"],
                    "date": day.isoformat(),
                    "age_days": age,
                    "candidates": candidates[:2],
                    "reason": _truncate(str(row["reason"] or "").strip(), 80),
                }
            )
        else:
            pending_attribution += 1
    doorstep_all.sort(key=lambda item: (item["date"], item["meeting_id"]), reverse=True)
    doorstep = doorstep_all[:DOORSTEP_CAP]
    doorstep_more = len(doorstep_all) - len(doorstep)

    # ---- 会议分圈：内圈、中圈、折叠
    in_window = [row for row in meeting_rows if _in_window(row["age"], days)]
    out_window = [row for row in meeting_rows if not _in_window(row["age"], days)]
    inner = [row for row in in_window if row["age"] < INNER_DAYS]
    middle = [row for row in in_window if INNER_DAYS <= row["age"] < MIDDLE_DAYS]
    older_in_window = [row for row in in_window if row["age"] >= MIDDLE_DAYS]
    # 内圈满了顺延到中圈（节点上保留日期）
    if len(inner) > INNER_CAP:
        middle = inner[INNER_CAP:] + middle
        inner = inner[:INNER_CAP]
    middle_cap = MIDDLE_CAP

    # ---- 需求
    requirements = _requirements(requirement_rows, today)
    requirement_ids = {item["requirement_id"] for item in requirements}

    # ---- 材料
    folders: list[dict[str, Any]] = []
    for row in root_rows:
        folders.append(
            {
                "id": f"root:{row['id']}",
                "kind": "root",
                "root_id": row["id"],
                "name": Path(row["path"]).name or row["path"],
                "path": row["path"],
                "ring": "inner",
            }
        )
    card_counts = {"ok": 0, "waiting": 0, "stopped": 0}
    for row in meeting_rows:
        category, _ = card_category(row) if cards_on else (None, None)
        if category:
            card_counts[category] += 1
    if root_rows:
        folders.append(
            {
                "id": "cards",
                "kind": "cards",
                "name": f"{CARDS_DIR_NAME}/",
                "path": str(Path(root_rows[0]["path"]) / CARDS_DIR_NAME),
                "ring": "inner",
                "written": card_counts["ok"],
                "stopped": card_counts["stopped"],
                "waiting": card_counts["waiting"],
                "enabled": cards_on,
            }
        )
    for row in folder_rows:
        folders.append(
            {
                "id": f"rf:{row['id']}",
                "kind": "requirement_folder",
                "folder_id": row["id"],
                "requirement_id": row["requirement_id"],
                "name": Path(row["path"]).name or row["path"],
                "path": row["path"],
                "ring": "middle",
            }
        )
    loose = (
        {"id": "loose", "kind": "loose", "name": "散放文件", "ring": "outer", "count": None}
        if root_rows
        else None
    )

    # ---- 线索词：窗口内、AI 判断过的会的证据里，来源是项目词或文件夹名的条目
    cue_map: dict[str, dict[str, Any]] = {}
    for row in in_window:
        for entry in _project_literals(row["evidence"], project_id):
            source = entry.get("source")
            text = str(entry.get("cue") or "").strip()
            if source not in CUE_SOURCES or not text:
                continue
            term_id = entry.get("term_id")
            if source == "term":
                term = terms.get(term_id or "")
                if term is None or not term["is_cue"]:
                    continue
                text = term["term"] if term["term"].casefold() == text.casefold() else text
            cue_id = _cue_id(source, text)
            bucket = cue_map.setdefault(
                cue_id,
                {
                    "id": cue_id,
                    "text": text,
                    "source": source,
                    "term_id": term_id if source == "term" else None,
                    "total": 0,
                    "meetings": [],
                },
            )
            count = int(entry.get("count") or 0)
            bucket["total"] += count
            bucket["meetings"].append(
                {
                    "meeting_id": row["id"],
                    "count": count,
                    "anchors_ms": [int(ms) for ms in entry.get("anchors_ms") or []][:3],
                }
            )
    cues_all = sorted(
        (cue for cue in cue_map.values() if cue["total"] >= CUE_MIN_COUNT),
        key=lambda cue: (-cue["total"], cue["text"]),
    )
    cue_cap = CUE_CAP

    # ---- 信标
    beacons = _beacons(project_id, projects, link_rows, cross_task_rows)

    # ---- 可见节点预算：先收中圈的会，再收线索词、需求、文件夹
    requirement_cap = REQUIREMENT_CAP
    folder_cap = FOLDER_CAP

    def visible_count() -> int:
        shown_middle = min(len(middle), middle_cap)
        hidden_meetings = len(middle) - shown_middle + len(older_in_window) + len(out_window)
        collapsed_nodes = _collapsed_node_count(
            hidden_meetings, older_in_window, middle[middle_cap:], out_window, window_key
        )
        return (
            1
            + len(inner)
            + shown_middle
            + collapsed_nodes
            + len(doorstep)
            + (1 if doorstep_more else 0)
            + min(len(requirements), requirement_cap)
            + (1 if len(requirements) > requirement_cap else 0)
            + min(len(folders), folder_cap)
            + (1 if len(folders) > folder_cap else 0)
            + (1 if loose else 0)
            + min(len(cues_all), cue_cap)
            + len(beacons)
        )

    while visible_count() > VISIBLE_BUDGET and middle_cap > MIDDLE_MIN:
        middle_cap -= 1
    while visible_count() > VISIBLE_BUDGET and cue_cap > CUE_MIN:
        cue_cap -= 1
    while visible_count() > VISIBLE_BUDGET and requirement_cap > REQUIREMENT_MIN:
        requirement_cap -= 1
    while visible_count() > VISIBLE_BUDGET and folder_cap > FOLDER_MIN:
        folder_cap -= 1

    shown_middle = middle[:middle_cap]
    overflow = middle[middle_cap:]
    collapsed = _collapse(overflow, older_in_window, out_window, window_key)
    visible_meetings = inner + shown_middle
    for row in inner:
        row["ring"] = "inner"
    for row in shown_middle:
        row["ring"] = "middle"
    collapsed_of = {
        meeting_id: group["id"] for group in collapsed for meeting_id in group["meeting_ids"]
    }

    shown_requirements = requirements[:requirement_cap]
    hidden_requirements = requirements[requirement_cap:]
    shown_folders = folders[:folder_cap]
    hidden_folders = folders[folder_cap:]
    shown_cues = cues_all[:cue_cap]

    # ---- 节点
    meeting_nodes = []
    for row in visible_meetings:
        category, card_text = card_category(row) if cards_on else (None, None)
        label, source = attribution_label(
            state=row["state"],
            origin=row["project_origin"],
            last_decision=row["last_decision"],
            evidence=row["evidence"],
            project_id=project_id,
        )
        meeting_nodes.append(
            {
                "id": f"m:{row['id']}",
                "meeting_id": row["id"],
                "title": row["title"],
                "date": row["day"].isoformat(),
                "age_days": row["age"],
                "ring": row["ring"],
                "state": row["state"],
                "attribution": {"label": label, "source": source},
                "open_tasks": int(row["open_tasks"] or 0),
                "pending_tasks": int(row["pending_tasks"] or 0),
                "card": category,
                "card_text": card_text,
                "has_minutes": bool(row["has_minutes"]),
            }
        )
    visible_ids = {row["id"] for row in visible_meetings}

    requirement_nodes = shown_requirements
    shown_requirement_ids = {item["requirement_id"] for item in shown_requirements}
    more_requirements = (
        {
            "id": "r:more",
            "count": len(hidden_requirements),
            "requirement_ids": [item["requirement_id"] for item in hidden_requirements],
        }
        if hidden_requirements
        else None
    )

    # ---- 连线
    edges: list[dict[str, Any]] = []
    for node in meeting_nodes:
        edges.append(
            {
                "id": f"e:attr:{node['meeting_id']}",
                "kind": "attribution",
                "from": node["id"],
                "to": "project",
                "label": node["attribution"]["label"],
                "state": "review" if node["state"] == "needs_review" else "ok",
                "source": node["attribution"]["source"],
            }
        )
    for cue in shown_cues:
        for hit in cue["meetings"]:
            target = _meeting_node_id(hit["meeting_id"], visible_ids, collapsed_of)
            if target is None:
                continue
            edges.append(
                {
                    "id": f"e:cue:{cue['id'][4:]}:{hit['meeting_id']}",
                    "kind": "cue",
                    "from": cue["id"],
                    "to": target,
                    "label": f"{hit['count']} 次",
                    "count": hit["count"],
                    "meeting_id": hit["meeting_id"],
                    "anchors_ms": hit["anchors_ms"],
                }
            )
    discussion_pairs = set()
    for link in link_rows:
        if link["requirement_project_id"] != project_id or link["meeting_project_id"] != project_id:
            continue
        if link["requirement_id"] not in requirement_ids:
            continue
        meeting_target = _meeting_node_id(link["meeting_id"], visible_ids, collapsed_of)
        requirement_target = (
            f"r:{link['requirement_id']}"
            if link["requirement_id"] in shown_requirement_ids
            else "r:more"
        )
        if meeting_target is None:
            continue
        pair = (meeting_target, requirement_target)
        if pair in discussion_pairs:
            continue
        discussion_pairs.add(pair)
        edges.append(
            {
                "id": f"e:disc:{link['requirement_id']}:{link['meeting_id']}",
                "kind": "discussion",
                "from": meeting_target,
                "to": requirement_target,
                "label": "你关联的",
                "meeting_id": link["meeting_id"],
                "requirement_id": link["requirement_id"],
            }
        )
    shown_folder_ids = {item["id"] for item in shown_folders}
    for folder in folders:
        if folder["kind"] != "requirement_folder" or folder["requirement_id"] not in requirement_ids:
            continue
        source_id = (
            f"r:{folder['requirement_id']}"
            if folder["requirement_id"] in shown_requirement_ids
            else "r:more"
        )
        target_id = folder["id"] if folder["id"] in shown_folder_ids else "f:more"
        edges.append(
            {
                "id": f"e:folder:{folder['folder_id']}",
                "kind": "folder",
                "from": source_id,
                "to": target_id,
                "label": "",
            }
        )
    if root_rows and cards_on:
        for node, row in zip(meeting_nodes, visible_meetings, strict=True):
            if node["card"] is None:
                continue
            edges.append(
                {
                    "id": f"e:write:{row['id']}",
                    "kind": "write",
                    "from": node["id"],
                    "to": "cards",
                    "label": node["card_text"] or "",
                    "state": node["card"],
                }
            )
    for beacon in beacons:
        sources = sorted(
            {
                _local_end(item, project_id, visible_ids, collapsed_of, shown_requirement_ids)
                for item in beacon["items"]
            }
            - {None}
        )
        for source_id in sources:
            edges.append(
                {
                    "id": f"e:cross:{beacon['project_id']}:{source_id}",
                    "kind": "cross",
                    "from": source_id,
                    "to": beacon["id"],
                    "label": f"→ {beacon['project_name']}",
                }
            )

    # ---- 状态句
    status = _status_sentence(
        inner=inner,
        meeting_rows=meeting_rows,
        doorstep_all=doorstep_all,
        doorstep=doorstep,
        visible_ids=visible_ids,
        collapsed_of=collapsed_of,
        cards_on=cards_on,
        pending_attribution=pending_attribution,
    )

    # ---- 每周会数（滚动 7 天一桶，最右是最近 7 天）
    weekly = []
    for index in range(WEEKS - 1, -1, -1):
        start_age, end_age = index * 7 + 6, index * 7
        weekly.append(
            {
                "from": (today - timedelta(days=start_age)).isoformat(),
                "to": (today - timedelta(days=end_age)).isoformat(),
                "count": sum(1 for row in meeting_rows if end_age <= row["age"] <= start_age),
            }
        )

    moved_out = []
    for row in moved_rows:
        payload = _json_dict(row["payload_json"])
        if row["now_project_id"] != payload.get("to") or row["now_project_id"] == project_id:
            continue
        if any(item["meeting_id"] == row["meeting_id"] for item in moved_out):
            continue
        target = projects.get(payload.get("to") or "")
        moved_out.append(
            {
                "meeting_id": row["meeting_id"],
                "title": row["title"],
                "to_project_id": payload.get("to"),
                "to_project_name": target["name"] if target else None,
                "undo_until": (
                    datetime.fromisoformat(row["created_at"])
                    + timedelta(seconds=UNDO_WINDOW_SECONDS)
                ).isoformat(),
            }
        )

    window_meeting_count = len(in_window)
    return {
        "project": {
            "id": project_id,
            "name": project["name"],
            "color": project["color"],
            "meeting_count": window_meeting_count,
        },
        "window": {
            "requested": window,
            "effective": window_key,
            "days": days,
            "widened_reason": widened_reason,
        },
        "today": today.isoformat(),
        "meetings": meeting_nodes,
        "collapsed": collapsed,
        "doorstep": doorstep,
        "doorstep_more": doorstep_more,
        "requirements": requirement_nodes,
        "requirements_more": more_requirements,
        "folders": shown_folders,
        "folders_more": (
            {"id": "f:more", "count": len(hidden_folders), "paths": [f["path"] for f in hidden_folders]}
            if hidden_folders
            else None
        ),
        "loose": loose,
        "cues": shown_cues,
        "beacons": beacons,
        "edges": edges,
        "status": status,
        "weekly": weekly,
        "moved_out": moved_out,
    }


def _meeting_node_id(
    meeting_id: str, visible_ids: set[str], collapsed_of: dict[str, str]
) -> str | None:
    if meeting_id in visible_ids:
        return f"m:{meeting_id}"
    return collapsed_of.get(meeting_id)


def _local_end(
    item: dict[str, Any],
    project_id: str,
    visible_ids: set[str],
    collapsed_of: dict[str, str],
    shown_requirement_ids: set[str],
) -> str | None:
    if item.get("requirement_id") and item.get("requirement_project_id") == project_id:
        rid = item["requirement_id"]
        return f"r:{rid}" if rid in shown_requirement_ids else "r:more"
    if item.get("meeting_id"):
        return _meeting_node_id(item["meeting_id"], visible_ids, collapsed_of)
    return None


def _resolve_candidates(
    raw: Any, projects: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
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
            }
        )
    return candidates


def _requirements(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        stamps = [
            local_day(None, value)
            for value in (row["updated_at"], row["task_activity"], row["link_activity"])
            if value
        ]
        if row["meeting_activity"]:
            stamps.append(local_day(row["meeting_activity"], row["meeting_activity"]))
        last = max(stamps) if stamps else local_day(None, row["created_at"])
        age = _age(last, today)
        items.append(
            {
                "id": f"r:{row['id']}",
                "requirement_id": row["id"],
                "title": row["title"],
                "priority": row["priority"],
                "open_tasks": int(row["open_tasks"] or 0),
                "pending_tasks": int(row["pending_tasks"] or 0),
                "last_activity": last.isoformat(),
                "age_days": age,
                "ring": _ring_for_age(age),
                "stale": age >= STALE_DAYS,
                "stale_text": f"{age // 7} 周没动静" if age >= STALE_DAYS else None,
                "_sort": (PRIORITY_ORDER.get(row["priority"], 9), age, row["title"]),
            }
        )
    items.sort(key=lambda item: item["_sort"])
    return [{key: value for key, value in item.items() if key != "_sort"} for item in items]


def _collapsed_node_count(
    hidden: int,
    older_in_window: list[dict[str, Any]],
    overflow: list[dict[str, Any]],
    out_window: list[dict[str, Any]],
    window_key: str,
) -> int:
    if hidden == 0:
        return 0
    return len(_collapse(overflow, older_in_window, out_window, window_key))


def _collapse(
    overflow: list[dict[str, Any]],
    older_in_window: list[dict[str, Any]],
    out_window: list[dict[str, Any]],
    window_key: str,
) -> list[dict[str, Any]]:
    """折叠节点。28 天以内的窗口：中圈放不下的和窗口外的一起折成「更早 N 场」；
    90 天、全部：窗口内 28 天以外的按月成簇（最多 6 簇），其余再并进「更早 N 场」。"""
    groups: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    if WINDOWS[window_key] is not None and WINDOWS[window_key] <= MIDDLE_DAYS:
        rest = overflow + older_in_window + out_window
    else:
        months: dict[str, list[dict[str, Any]]] = {}
        for row in overflow + older_in_window:
            months.setdefault(row["day"].strftime("%Y-%m"), []).append(row)
        keys = sorted(months, reverse=True)
        for key in keys[:MONTH_CLUSTER_CAP]:
            rows = months[key]
            groups.append(_group(f"c:{key}", "month", rows, label=f"{int(key[5:])} 月 {len(rows)} 场"))
        for key in keys[MONTH_CLUSTER_CAP:]:
            rest.extend(months[key])
        rest.extend(out_window)
    if rest:
        groups.append(_group("c:older", "older", rest, label=f"更早 {len(rest)} 场"))
    return groups


def _group(group_id: str, kind: str, rows: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    days = sorted(row["day"] for row in rows)
    return {
        "id": group_id,
        "kind": kind,
        "label": label,
        "count": len(rows),
        "from": days[0].isoformat(),
        "to": days[-1].isoformat(),
        "meeting_ids": [row["id"] for row in rows],
        "ring": "outer",
    }


def _beacons(
    project_id: str,
    projects: dict[str, dict[str, Any]],
    link_rows: list[dict[str, Any]],
    cross_task_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """跨项目信标：同一个对方项目合成一个。已完成、已取消的任务不产生信标（查询里已过滤）。"""
    buckets: dict[str, dict[str, Any]] = {}

    def add(other_id: str | None, item: dict[str, Any]) -> None:
        if not other_id or other_id == project_id or other_id not in projects:
            return
        other = projects[other_id]
        bucket = buckets.setdefault(
            other_id,
            {
                "id": f"b:{other_id}",
                "project_id": other_id,
                "project_name": other["name"],
                "project_color": other["color"],
                "items": [],
            },
        )
        bucket["items"].append(item)

    for link in link_rows:
        requirement_project = link["requirement_project_id"]
        meeting_project = link["meeting_project_id"]
        if requirement_project == meeting_project:
            continue
        if meeting_project == project_id:
            add(
                requirement_project,
                {
                    "kind": "meeting_requirement",
                    "meeting_id": link["meeting_id"],
                    "requirement_id": link["requirement_id"],
                    "requirement_project_id": requirement_project,
                    "text": (
                        f"会议「{link['meeting_title']}」关联了"
                        f"{projects.get(requirement_project or '', {}).get('name', '别的项目')}"
                        f"的需求「{link['requirement_title']}」"
                    ),
                },
            )
        elif requirement_project == project_id:
            add(
                meeting_project,
                {
                    "kind": "requirement_meeting",
                    "meeting_id": link["meeting_id"],
                    "requirement_id": link["requirement_id"],
                    "requirement_project_id": requirement_project,
                    "text": (
                        f"需求「{link['requirement_title']}」关联了"
                        f"{projects.get(meeting_project or '', {}).get('name', '没归项目')}"
                        f"的会「{link['meeting_title']}」"
                    ),
                },
            )
    for task in cross_task_rows:
        if task["meeting_project_id"] == project_id:
            add(
                task["project_id"],
                {
                    "kind": "task_elsewhere",
                    "task_id": task["id"],
                    "meeting_id": task["meeting_id"],
                    "text": (
                        f"会议「{task['meeting_title']}」的任务「{task['title']}」在"
                        f"{projects[task['project_id']]['name'] if task['project_id'] in projects else '别的项目'}"
                    ),
                },
            )
        elif task["project_id"] == project_id:
            add(
                task["meeting_project_id"],
                {
                    "kind": "task_from_elsewhere",
                    "task_id": task["id"],
                    "meeting_id": task["meeting_id"],
                    "meeting_project_id": task["meeting_project_id"],
                    "text": (
                        f"任务「{task['title']}」来自"
                        f"{projects[task['meeting_project_id']]['name'] if task['meeting_project_id'] in projects else '别的项目'}"
                        f"的会「{task['meeting_title']}」"
                    ),
                },
            )
    beacons = sorted(buckets.values(), key=lambda item: (-len(item["items"]), item["project_name"]))
    for beacon in beacons:
        beacon["count"] = len(beacon["items"])
        beacon["label"] = f"→ {beacon['project_name']} ×{beacon['count']}"
    return beacons[:BEACON_CAP]


def _status_sentence(
    *,
    inner: list[dict[str, Any]],
    meeting_rows: list[dict[str, Any]],
    doorstep_all: list[dict[str, Any]],
    doorstep: list[dict[str, Any]],
    visible_ids: set[str],
    collapsed_of: dict[str, str],
    cards_on: bool,
    pending_attribution: int,
) -> dict[str, Any]:
    """顶部状态句的三类短语，每条带要点亮的节点。"""

    def node_ids(meeting_ids: Iterable[str]) -> list[str]:
        ids: list[str] = []
        for meeting_id in meeting_ids:
            target = _meeting_node_id(meeting_id, visible_ids, collapsed_of)
            if target and target not in ids:
                ids.append(target)
        return ids

    ok: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    settled = [row for row in inner if row["state"] in ("auto", "manual")]
    if inner and len(settled) == len(inner):
        ok.append(
            {
                "text": f"近 7 天 {len(inner)} 场会都归好了",
                "node_ids": [f"m:{row['id']}" for row in inner],
            }
        )
    if doorstep_all:
        waiting.append(
            {
                "text": f"{len(doorstep_all)} 场可能是这个项目的",
                "node_ids": [item["id"] for item in doorstep],
            }
        )
    review = [row["id"] for row in meeting_rows if row["state"] == "needs_review"]
    if review:
        waiting.append({"text": f"{len(review)} 场归属待复核", "node_ids": node_ids(review)})
    pending_tasks = sum(int(row["pending_tasks"] or 0) for row in meeting_rows)
    if pending_tasks:
        waiting.append(
            {
                "text": f"{pending_tasks} 条任务待确认",
                "node_ids": node_ids(
                    row["id"] for row in meeting_rows if int(row["pending_tasks"] or 0)
                ),
            }
        )
    if cards_on:
        stopped_cards = [row["id"] for row in meeting_rows if card_category(row)[0] == "stopped"]
        if stopped_cards:
            stopped.append(
                {"text": f"{len(stopped_cards)} 张卡片", "node_ids": node_ids(stopped_cards)}
            )
    return {
        "ok": ok,
        "waiting": waiting,
        "stopped": stopped,
        "note": f"另有 {pending_attribution} 场新会还在判断归属" if pending_attribution else None,
    }


def collapsed_meetings(
    connection: Any,
    project_id: str,
    group_id: str,
    *,
    window: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """折叠节点里的会，按月列出。"""
    graph = project_graph(connection, project_id, window=window, today=today)
    group = next((item for item in graph["collapsed"] if item["id"] == group_id), None)
    if group is None:
        raise GraphNotFound("折叠节点不存在")
    ids = group["meeting_ids"]
    placeholders = ", ".join("?" for _ in ids)
    rows = [
        dict(row)
        for row in connection.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.created_at,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.meeting_id = m.id AND t.status IN ({_OPEN})) AS open_tasks
                  FROM meetings m WHERE m.id IN ({placeholders})""",
            ids,
        ).fetchall()
    ] if ids else []
    months: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = local_day(row["recording_date"], row["created_at"])
        months.setdefault(day.strftime("%Y-%m"), []).append(
            {
                "meeting_id": row["id"],
                "title": row["title"],
                "date": day.isoformat(),
                "open_tasks": int(row["open_tasks"] or 0),
            }
        )
    result = []
    for key in sorted(months, reverse=True):
        items = sorted(months[key], key=lambda item: (item["date"], item["meeting_id"]), reverse=True)
        result.append({"month": key, "label": f"{key[:4]} 年 {int(key[5:])} 月", "meetings": items})
    return {"id": group_id, "label": group["label"], "months": result}


# ---------------------------------------------------------------------- 会议简报


_BRIEF_TASKS = 20
_BRIEF_DECISIONS = 8
_BRIEF_QUOTES = 3
_QUOTE_CHARS = 120
_HHMMSS = re.compile(r"\[(\d{2}):(\d{2})(?::(\d{2}))?")


def _anchor_ms(raw: str) -> int | None:
    match = _HHMMSS.search(raw)
    if match is None:
        return None
    hours, minutes, seconds = match.group(1), match.group(2), match.group(3)
    if seconds is None:
        # [MM:SS]
        return (int(hours) * 60 + int(minutes)) * 1000
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000


def minutes_outline(markdown: str) -> dict[str, Any]:
    """「一分钟摘要」和「定了什么」（带时间点）。老纪要没有决议段时明写，不说「没有决议」。"""
    text = markdown or ""
    summary = ""
    body = _section_body(text, _SUMMARY_SECTION.search(text))
    if body:
        paragraph = next(
            (
                line.strip()
                for line in body.strip().split("\n")
                if line.strip() and not line.strip().startswith(("#", "|", ">", "---"))
            ),
            "",
        )
        summary = _truncate(_ANCHOR.sub("", paragraph).replace("**", "").strip(), 240)
    header = _DECISION_SECTION.search(text)
    decision_body = _section_body(text, header)
    items: list[dict[str, Any]] = []
    raw_items: list[str] = []
    if decision_body:
        sub = list(_SUBHEADING.finditer(decision_body))
        if sub:
            # 老六段式：「### 决议 1 · xxx」，时间点可能写在下面的正文里
            for index, match in enumerate(sub):
                end = sub[index + 1].start() if index + 1 < len(sub) else len(decision_body)
                raw_items.append(match.group(1) + " " + decision_body[match.end():end])
        else:
            raw_items = _LIST_ITEM.findall(decision_body)
    for raw in raw_items:
        first_line = raw.strip().split("\n")[0]
        item = _clean_item(first_line)
        if not item or any(existing["text"] == item for existing in items):
            continue
        items.append({"text": _truncate(item, 90), "start_ms": _anchor_ms(raw)})
    note = None
    if header is None:
        note = "这场纪要没有决议段"
    elif not items:
        note = "决议段是空的"
    return {"summary": summary, "decisions": items[:_BRIEF_DECISIONS], "decisions_note": note}


def meeting_brief(
    connection: Any,
    meeting_id: str,
    *,
    attribution: dict[str, Any] | None,
    card: dict[str, Any] | None,
) -> dict[str, Any]:
    """关系图会议面板：小于 20KB。现有会议详情接口带全部逐字稿，有 100–300KB。"""
    meeting = connection.execute(
        """SELECT m.id, m.title, m.recording_date, m.created_at, m.duration_ms, m.project_id,
                  p.name AS project_name, p.color AS project_color,
                  mv.markdown AS minutes_markdown,
                  (SELECT a.id FROM artifacts a WHERE a.meeting_id = m.id AND a.kind = 'audio'
                    ORDER BY CASE a.source_root
                      WHEN 'archive' THEN 0 WHEN 'draft' THEN 1 WHEN 'staging' THEN 2 ELSE 3 END,
                      a.role DESC, a.path LIMIT 1) AS audio_id
             FROM meetings m
             LEFT JOIN projects p ON p.id = m.project_id
             LEFT JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
            WHERE m.id = ?""",
        (meeting_id,),
    ).fetchone()
    if meeting is None:
        raise GraphNotFound("会议不存在")
    meeting = dict(meeting)
    day = local_day(meeting["recording_date"], meeting["created_at"])
    outline = (
        minutes_outline(meeting["minutes_markdown"])
        if meeting["minutes_markdown"]
        else {"summary": "", "decisions": [], "decisions_note": "纪要还没写好"}
    )

    evidence_quotes: list[dict[str, Any]] = []
    if attribution is not None and meeting["project_id"]:
        literals = sorted(
            _project_literals(attribution.get("evidence") or [], meeting["project_id"]),
            key=lambda entry: int(entry.get("count") or 0),
            reverse=True,
        )[:_BRIEF_QUOTES]
        wanted = sorted(
            {int(ms) for entry in literals for ms in (entry.get("anchors_ms") or [])[:2]}
        )
        texts = _segments_at(connection, meeting_id, wanted)
        for entry in literals:
            evidence_quotes.append(
                {
                    "cue": entry.get("cue"),
                    "source": entry.get("source"),
                    "count": int(entry.get("count") or 0),
                    "quotes": [
                        {"start_ms": int(ms), "text": texts.get(int(ms), "")}
                        for ms in (entry.get("anchors_ms") or [])[:2]
                    ],
                }
            )

    tasks = [
        dict(row)
        for row in connection.execute(
            f"""SELECT t.id, t.title, t.status, t.anchor_ms, t.anchor_quote, t.project_id,
                       t.requirement_id
                  FROM tasks t
                 WHERE t.meeting_id = ? AND t.status IN ({_OPEN})
                 ORDER BY CASE t.status WHEN 'pending_confirm' THEN 0 ELSE 1 END,
                          t.created_at, t.id""",
            (meeting_id,),
        ).fetchall()
    ]
    for task in tasks:
        task["title"] = _truncate(task["title"] or "", 80)
        task["anchor_quote"] = _truncate(task["anchor_quote"] or "", _QUOTE_CHARS)
    requirements = [
        dict(row)
        for row in connection.execute(
            """SELECT r.id, r.title, r.priority, r.status, r.project_id,
                      p.name AS project_name
                 FROM requirement_meetings rm
                 JOIN requirements r ON r.id = rm.requirement_id
                 LEFT JOIN projects p ON p.id = r.project_id
                WHERE rm.meeting_id = ?
                ORDER BY r.created_at""",
            (meeting_id,),
        ).fetchall()
    ]
    return {
        "meeting": {
            "id": meeting["id"],
            "title": meeting["title"],
            "date": day.isoformat(),
            "duration_ms": meeting["duration_ms"],
            "project_id": meeting["project_id"],
            "project_name": meeting["project_name"],
            "project_color": meeting["project_color"],
            "has_minutes": bool(meeting["minutes_markdown"]),
            "audio_url": f"/api/media/{meeting['audio_id']}" if meeting["audio_id"] else None,
        },
        "attribution": attribution,
        "evidence_quotes": evidence_quotes,
        **outline,
        "tasks": tasks[:_BRIEF_TASKS],
        "tasks_more": max(0, len(tasks) - _BRIEF_TASKS),
        "requirements": requirements,
        "card": card,
        "files_note": "第二期起在这里列出会上提到的文件",
    }


def _segments_at(connection: Any, meeting_id: str, starts: list[int]) -> dict[int, str]:
    """锚点所在的那一段原话。锚点记的是命中段的开始时间。"""
    if not starts:
        return {}
    placeholders = ", ".join("?" for _ in starts)
    rows = connection.execute(
        f"""SELECT s.start_ms, s.text FROM segments s
             WHERE s.version_id = (SELECT current_transcript_version_id FROM meetings WHERE id = ?)
               AND s.start_ms IN ({placeholders})""",
        (meeting_id, *starts),
    ).fetchall()
    return {int(row["start_ms"]): _truncate(row["text"] or "", _QUOTE_CHARS) for row in rows}


QUOTE_BEFORE_MS = 5_000
QUOTE_AFTER_MS = 15_000
QUOTE_MAX_ANCHORS = 12
QUOTE_MAX_SEGMENTS = 4


def meeting_quotes(connection: Any, meeting_id: str, anchors: list[int]) -> dict[str, Any]:
    """锚点前后的原话：每个锚点取和 [锚点 - 5 秒, 锚点 + 15 秒] 重叠的段，最多 4 段。"""
    meeting = connection.execute(
        "SELECT current_transcript_version_id AS version_id FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if meeting is None:
        raise GraphNotFound("会议不存在")
    anchors = sorted({max(0, int(value)) for value in anchors})[:QUOTE_MAX_ANCHORS]
    result: list[dict[str, Any]] = []
    if not anchors or meeting["version_id"] is None:
        return {"meeting_id": meeting_id, "quotes": [{"at": at, "segments": []} for at in anchors]}
    low = anchors[0] - QUOTE_BEFORE_MS
    high = anchors[-1] + QUOTE_AFTER_MS
    rows = [
        dict(row)
        for row in connection.execute(
            """SELECT s.id, s.start_ms, s.end_ms, s.text, s.speaker_label,
                      sp.display_name AS speaker_name
                 FROM segments s
                 LEFT JOIN speakers sp ON sp.meeting_id = ? AND sp.label = s.speaker_label
                WHERE s.version_id = ? AND s.end_ms >= ? AND s.start_ms <= ?
                ORDER BY s.start_ms""",
            (meeting_id, meeting["version_id"], low, high),
        ).fetchall()
    ]
    for at in anchors:
        segments = [
            {
                "segment_id": row["id"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "text": _truncate(row["text"] or "", 240),
                # 说话人大多没命名，没有显示名时不显示说话人。
                "speaker": row["speaker_name"] or None,
            }
            for row in rows
            if row["end_ms"] >= at - QUOTE_BEFORE_MS and row["start_ms"] <= at + QUOTE_AFTER_MS
        ][:QUOTE_MAX_SEGMENTS]
        result.append({"at": at, "segments": segments})
    return {"meeting_id": meeting_id, "quotes": result}


# ---------------------------------------------------------------------- 线索词面板


def cue_term_detail(connection: Any, term_id: str) -> dict[str, Any] | None:
    """「这个词让 N 场会归到这里」：最近一条 AI 判断过的批次证据里用到这个词的会。

    only_cue：这场会是 AI 归的，而且证据里这个项目只有这一条字面线索——把词关掉以后这类会
    最该复核（已归好的会不动，只列出来）。
    """
    term = connection.execute(
        """SELECT t.id, t.term, t.aliases, t.also, t.project_id, t.is_cue, t.confirmed,
                  t.source, t.category, p.name AS project_name
             FROM glossary_terms t LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.id = ?""",
        (term_id,),
    ).fetchone()
    if term is None:
        return None
    term = dict(term)
    meetings: list[dict[str, Any]] = []
    if term["project_id"]:
        rows = connection.execute(
            """SELECT m.id, m.title, m.recording_date, m.created_at, m.project_id,
                      m.project_origin, ev.evidence_json
                 FROM meetings m
                 JOIN project_links ev ON ev.id = (
                      SELECT MAX(x.id) FROM project_links x
                       WHERE x.meeting_id = m.id AND x.status IN ('done', 'needs_review')
                         AND x.evidence_json IS NOT NULL)
                WHERE m.project_id = ? AND instr(ev.evidence_json, ?) > 0""",
            (term["project_id"], term_id),
        ).fetchall()
        for row in rows:
            literals = _project_literals(_json_list(row["evidence_json"]), term["project_id"])
            mine = [entry for entry in literals if entry.get("term_id") == term_id]
            if not mine:
                continue
            entry = mine[0]
            day = local_day(row["recording_date"], row["created_at"])
            meetings.append(
                {
                    "meeting_id": row["id"],
                    "title": row["title"],
                    "date": day.isoformat(),
                    "count": int(entry.get("count") or 0),
                    "anchors_ms": [int(ms) for ms in entry.get("anchors_ms") or []][:3],
                    "origin": row["project_origin"],
                    "only_cue": row["project_origin"] == "ai" and len(literals) == 1,
                }
            )
    meetings.sort(key=lambda item: (item["date"], item["meeting_id"]), reverse=True)
    return {
        "id": term["id"],
        "term": term["term"],
        "aliases": _json_list(term["aliases"]),
        "also": _json_list(term["also"]),
        "project_id": term["project_id"],
        "project_name": term["project_name"],
        "is_cue": bool(term["is_cue"]),
        "confirmed": bool(term["confirmed"]),
        "category": term["category"],
        "source": term["source"],
        "cue_meetings": meetings,
    }


# ---------------------------------------------------------------------- 资料盘缓存


LOOSE_RECENT = 30
ROOTS_REFRESH_SECONDS = 30.0


def _loose_files(path: Path) -> tuple[int, list[dict[str, Any]]]:
    """根目录里散放的文件：只读一层，不递归；跳过隐藏文件和系统影子文件。"""
    entries: list[dict[str, Any]] = []
    count = 0
    with os.scandir(path) as iterator:
        for entry in iterator:
            name = entry.name
            if name.startswith((".", "~$")) or name in ("Thumbs.db", "desktop.ini", "Icon\r"):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            count += 1
            entries.append(
                {
                    "name": name,
                    "path": str(Path(entry.path)),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                }
            )
    entries.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return count, entries[:LOOSE_RECENT]


class RootsCache:
    """材料根目录和需求文件夹的在线状态、根目录散放文件。后台每 30 秒刷新一次；图接口只读。

    读盘可能卡住（外置盘休眠），所以刷新只在后台线程里跑，同一时间最多一个。
    """

    def __init__(self, db: Any, *, state_of=volume_state, loose_of=_loose_files):
        self.db = db
        self._state_of = state_of
        self._loose_of = loose_of
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._running = threading.Lock()

    def paths(self) -> tuple[list[str], list[str]]:
        with self.db.autocommit() as connection:
            roots = [
                row["path"]
                for row in connection.execute(
                    "SELECT DISTINCT path FROM project_material_roots"
                ).fetchall()
            ]
            folders = [
                row["path"]
                for row in connection.execute(
                    "SELECT DISTINCT path FROM requirement_folders"
                ).fetchall()
            ]
        return roots, folders

    def refresh(self) -> None:
        if not self._running.acquire(blocking=False):
            return
        try:
            roots, folders = self.paths()
            fresh: dict[str, dict[str, Any]] = {}
            for path in roots + [p for p in folders if p not in roots]:
                checked_at = datetime.now(UTC).isoformat()
                try:
                    state = self._state_of(path)
                except OSError:
                    state = "missing"
                entry: dict[str, Any] = {"state": state, "checked_at": checked_at}
                if path in roots and state == ROOT_ONLINE:
                    try:
                        entry["loose_count"], entry["loose_recent"] = self._loose_of(Path(path))
                    except OSError:
                        entry["loose_count"], entry["loose_recent"] = None, []
                fresh[path] = entry
            with self._lock:
                self._entries = fresh
        except Exception:  # noqa: BLE001 — 后台刷新失败只记日志，下一轮再来
            logger.exception("刷新资料盘状态失败")
        finally:
            self._running.release()

    def refresh_in_background(self) -> None:
        threading.Thread(target=self.refresh, name="graph-roots-refresh", daemon=True).start()

    def get(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(path)
            return dict(entry) if entry else None


def project_roots(connection: Any, cache: RootsCache, project_id: str) -> dict[str, Any]:
    if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise GraphNotFound("项目不存在")
    roots = connection.execute(
        """SELECT id, path FROM project_material_roots WHERE project_id = ?
            ORDER BY created_at, id""",
        (project_id,),
    ).fetchall()
    folders = connection.execute(
        """SELECT f.id, f.path, f.requirement_id FROM requirement_folders f
             JOIN requirements r ON r.id = f.requirement_id
            WHERE r.project_id = ? ORDER BY f.created_at, f.id""",
        (project_id,),
    ).fetchall()
    checking = False
    root_items = []
    loose_total = 0
    loose_recent: list[dict[str, Any]] = []
    for row in roots:
        entry = cache.get(row["path"])
        if entry is None:
            checking = True
            entry = {"state": "checking"}
        count = entry.get("loose_count")
        if isinstance(count, int):
            loose_total += count
            loose_recent.extend(entry.get("loose_recent") or [])
        root_items.append(
            {
                "id": f"root:{row['id']}",
                "root_id": row["id"],
                "path": row["path"],
                "state": entry["state"],
                "loose_count": count,
                "checked_at": entry.get("checked_at"),
            }
        )
    folder_items = []
    for row in folders:
        entry = cache.get(row["path"])
        if entry is None:
            checking = True
        folder_items.append(
            {
                "id": f"rf:{row['id']}",
                "folder_id": row["id"],
                "requirement_id": row["requirement_id"],
                "path": row["path"],
                "state": entry["state"] if entry else "checking",
            }
        )
    loose_recent.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return {
        "roots": root_items,
        "folders": folder_items,
        "loose": {"count": loose_total, "recent": loose_recent[:LOOSE_RECENT]},
        "checking": checking,
    }
