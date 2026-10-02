"""第四期 4f：以一份文件为中心的局部图（GET /api/graph/files/{id}/map）和来龙去脉（GET /api/graph/trace）。

两样都只查库、不读盘、不写库，也不设 ETag（文件挪位置、原地改都不动 graph_rev，带 ETag 会把挪走的
文件显示成旧名字）。材料正文一个字都不出：相关线只给共同词和位置，不给段落文字。

- 局部图：id 只是入口，按 4a 的来历规则找活文件，把项目里同 content_key 的每一份活文件的线都收进来。
  一跳的邻居最多画 12 个，按 8 级顺序（在问的可能过时、在问的产出、交付物、提到、标过已更新的可能
  过时、同名的别的版本、装着它的需求文件夹、相关），其余进 hidden。最多 12 条语句，通常 7 条。
- 来龙去脉：只走有原话或你确认过的线，分三级（0 你做的，1 字面或规则，2 AI），时间单调，每个方向最多
  3 个算数的步，全链最多 12 个节点；包含关系（会议到决议、会议到任务）不算步但占节点。每展开一个节点
  用 relation_read.edges_of（2 条）加 1 条节点和包含关系的 SELECT，中心是文件时再加 1 条同名的 SELECT；
  包含关系的一步先展开那个决议或任务看能不能接着走，没走通的最多 3 次；总共不超过 48 条语句。
  中心文件按同内容那一组读（挪过位置、找不到活文件时旧 id 上的线也算）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from . import graph
from . import related
from . import relation_read
from .decisions import decision_moment
from .file_mentions import _meeting_ns
from .file_stems import STEM_YES, stem_usability
from .relation_read import AUDIO_ID_SQL, LIVE_ID_SQL, _marks, _short, mention_union

MAP_NEIGHBOURS = 12
HIDDEN_ROWS = 20
SAME_NAME_CAP = 2
REQUIREMENT_CAP = 1
RELATED_CAP = 3
TRACE_STEPS = 3
TRACE_NODES = 12
TRACE_LOOKAHEAD = (
    3  # 包含关系的一步先看一眼能不能接着走；没走通的展开最多这么多次（语句总数仍 ≤ 48）
)
NODE_PATTERN = (
    r"^(file:\d{1,12}|m:[A-Za-z0-9_-]{1,64}|dec:[A-Za-z0-9_-]{1,64}|task:[A-Za-z0-9_-]{1,64})$"
)
TRACE_TASK_STATUSES = ("confirmed", "in_progress", "done")

FILE_MISSING = "这份文件不在索引里了"
FILE_NO_PROJECT = "这份文件不在任何项目的资料盘里"
MEETING_MISSING = "会议不存在"
DECISION_MISSING = "这条决议已经不在了"
TASK_MISSING = "这条任务已经不在了"
MEETING_NO_PROJECT = "这场会没归项目"

# 局部图才有的线上的字
TASK_FROM_TEXT = "会上提到这条任务"
LATER_CHANGED_TEXT = "后来改了"
RESTATED_TEXT = "后来又提到"


class LocalError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


# ---------------------------------------------------------------------- 小工具


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone().isoformat(timespec="seconds") if moment else None


def _meeting_moment(recording_date: str | None, created_at: str | None) -> datetime | None:
    ns = _meeting_ns(recording_date, created_at)
    return datetime.fromtimestamp(ns / 1_000_000_000, UTC) if ns is not None else None


def _file_moment(mtime_ns: int | None) -> datetime | None:
    return datetime.fromtimestamp(int(mtime_ns) / 1_000_000_000, UTC) if mtime_ns else None


def _ms(moment: datetime | None) -> int | None:
    return int(moment.timestamp() * 1000) if moment else None


def _caption(
    title: str | None, recording_date: str | None, created_at: str | None, today: date
) -> str:
    day = graph.local_day(recording_date, created_at)
    return f"{graph.month_day(day, today)} {title or ''}".strip()


def _audio(audio_id: Any) -> str | None:
    return f"/api/media/{audio_id}" if audio_id else None


def _words_label(words: list[str]) -> str:
    return "共同词：" + "、".join(words[:4]) if words else "共同词"


# ---------------------------------------------------------------------- 中心文件和它的来历


@dataclass
class _Center:
    given: dict[str, Any]
    live: dict[str, Any] | None
    copies: list[dict[str, Any]]
    project_id: str
    earlier: list[int]  # 同内容、已经不见了的旧行（挪走前的 id，旧的提到还挂在上面）

    @property
    def row(self) -> dict[str, Any]:
        return self.live or self.given

    @property
    def file_id(self) -> int:
        return int(self.row["id"])

    @property
    def group(self) -> list[int]:
        ids = [
            self.file_id,
            int(self.given["id"]),
            *(int(copy["id"]) for copy in self.copies),
            *self.earlier,
        ]
        return list(dict.fromkeys(ids))

    @property
    def content_key(self) -> str | None:
        return self.row.get("content_key") or self.given.get("content_key")


def _resolve_file(connection: Any, file_id: int) -> _Center:
    """两条语句：这一行本身（连根目录定项目），和同项目里同内容或同位置的活文件。"""
    given = connection.execute(
        """SELECT f.*, pr.project_id, pr.path AS root_path FROM material_files f
             LEFT JOIN project_material_roots pr ON pr.id = f.root_id WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if given is None:
        raise LocalError(404, FILE_MISSING)
    given = dict(given)
    if not given.get("project_id"):
        raise LocalError(409, FILE_NO_PROJECT)
    # 同项目里同内容（活的和不见了的都要：旧行上还挂着提到）或同位置的行
    found = [
        dict(row)
        for row in connection.execute(
            """SELECT f.*, pr.project_id, pr.path AS root_path FROM material_files f
                 JOIN project_material_roots pr ON pr.id = f.root_id
                WHERE pr.project_id = ?
                  AND (f.id = ? OR (? IS NOT NULL AND f.content_key = ?) OR (f.root_id = ? AND f.rel_path = ?))
                ORDER BY f.mtime_ns DESC, f.id""",
            (
                given["project_id"],
                file_id,
                given["content_key"],
                given["content_key"],
                given["root_id"],
                given["rel_path"],
            ),
        ).fetchall()
    ]
    rows = [row for row in found if row["gone_at"] is None]
    live: dict[str, Any] | None = None
    if given["gone_at"] is None:
        live = given
    else:
        same_content = [
            row
            for row in rows
            if given["content_key"] and row["content_key"] == given["content_key"]
        ]
        same_place = [
            row
            for row in rows
            if row["root_id"] == given["root_id"] and row["rel_path"] == given["rel_path"]
        ]
        live = (same_content or same_place or [None])[0]
    copies = []
    key = (live or given).get("content_key")
    if live is not None and key:
        copies = [row for row in rows if row["id"] != live["id"] and row["content_key"] == key]
    earlier = [
        int(row["id"])
        for row in found
        if row["gone_at"] is not None and key and row["content_key"] == key
    ]
    return _Center(
        given=given, live=live, copies=copies, project_id=str(given["project_id"]), earlier=earlier
    )


def _center_payload(center: _Center, *, stale: bool, asks: bool) -> dict[str, Any]:
    row = center.row
    folder = PurePosixPath(str(row.get("dir_rel") or "")).name
    payload: dict[str, Any] = {
        "id": f"file:{center.file_id}",
        "kind": "file",
        "file_id": center.file_id,
        "name": row["name"],
        "ext": row["ext"],
        "root_id": row["root_id"],
        "rel_path": row["rel_path"],
        "folder": folder,
        "project_id": center.project_id,
        "at": _iso(_file_moment(row.get("mtime_ns"))),
        "gone": center.live is None,
        "copies": [{"file_id": int(copy["id"]), "name": copy["name"]} for copy in center.copies],
        "stale": stale,
        "asks_deliverable": asks,
    }
    if center.live is not None and int(center.live["id"]) != int(center.given["id"]):
        payload["moved_from"] = int(center.given["id"])
    return payload


# ---------------------------------------------------------------------- 局部图


@dataclass
class _Neighbour:
    level: int
    at_ms: int
    node_id: str
    edge: dict[str, Any]
    order: int = 0  # 同级里的次序（提到：字面先于放宽）


def _requirement_of(
    root_path: str | None, rel_path: str, folders: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """装着这份文件的需求文件夹（最深的那个）。"""
    if not root_path:
        return None
    full = str(PurePosixPath(root_path) / rel_path)
    best = None
    for folder in folders:
        base = str(folder["path"]).rstrip("/")
        if (
            base
            and full.startswith(base + "/")
            and (best is None or len(base) > len(str(best["path"])))
        ):
            best = folder
    return best


def file_map(
    connection: Any, file_id: int, *, related_on: bool = False, today: date | None = None
) -> dict[str, Any]:
    """局部图。语句：文件本身、同内容的活文件、提到、非相关的关联行、交付物、同名的版本、需求文件夹、
    相关（related_on 时）、会议一批、提到的原话；最多 10 条。"""
    today = today or datetime.now().astimezone().date()
    center = _resolve_file(connection, file_id)
    group = center.group
    marks = _marks(group)
    project_id = center.project_id
    content_key = center.content_key
    center_id = f"file:{center.file_id}"

    # ③ 提到（字面和放宽的两半，按内容标识收同内容的每一份）
    mention_rows = [
        dict(row)
        for row in connection.execute(
            f"""WITH u AS ({
                mention_union(
                    literal=f"fm.file_id IN ({marks}) AND fm.project_id = ?",
                    loose="r.project_id = ?",
                )
            })
                SELECT u.* FROM u WHERE u.file_id IN ({marks})""",
            (*group, project_id, project_id, *group),
        ).fetchall()
    ]
    # 一场会只留一行：字面的胜过放宽的，再比次数
    per_meeting: dict[str, dict[str, Any]] = {}
    for row in mention_rows:
        kept = per_meeting.get(row["meeting_id"])
        rank = (row["via"] == "literal", int(row["count"]))
        if kept is None or rank > (kept["via"] == "literal", int(kept["count"])):
            per_meeting[row["meeting_id"]] = row

    # ④ 非相关的关联行：在问的产出、影响，标过已更新的影响
    relation_rows = [
        dict(row)
        for row in connection.execute(
            f"""SELECT r.id, r.kind, r.status, r.origin, r.meeting_id, r.task_id, r.decision_id, r.at_ms, r.quote,
                       r.evidence_json, r.created_at, {LIVE_ID_SQL} AS live_id,
                       t.title AS task_title, t.status AS task_status, t.meeting_id AS task_meeting_id,
                       t.anchor_ms, t.anchor_quote,
                       d.text AS decision_text, d.meeting_id AS decision_meeting_id, d.start_ms AS decision_ms
                  FROM relations r
                  LEFT JOIN tasks t ON t.id = r.task_id
                  LEFT JOIN decisions d ON d.id = r.decision_id AND d.gone_at IS NULL
                 WHERE r.project_id = ?
                   AND ((r.kind IN ('produced', 'affects') AND r.status = 'suggested')
                        OR (r.kind = 'affects' AND r.status = 'resolved'))
                   AND ((? IS NOT NULL AND r.content_key = ?) OR r.file_id IN ({marks}))""",
            (project_id, content_key, content_key, *group),
        ).fetchall()
    ]
    if center.live is not None:
        relation_rows = [row for row in relation_rows if row["live_id"] in group]

    # ⑤ 交付物（按内容标识或位置）
    places = [
        (center.row["root_id"], center.row["rel_path"]),
        (center.given["root_id"], center.given["rel_path"]),
    ]
    places += [(copy["root_id"], copy["rel_path"]) for copy in center.copies]
    places = list(dict.fromkeys(places))
    place_values = ", ".join("(?, ?)" for _ in places)
    deliverable_rows = [
        dict(row)
        for row in connection.execute(
            f"""SELECT d.id AS deliverable_id, d.created_at, t.id AS task_id, t.title AS task_title,
                       t.status AS task_status, t.meeting_id, t.anchor_ms, t.anchor_quote
                  FROM deliverables d
                  JOIN deliverable_files df ON df.deliverable_id = d.id
                  JOIN tasks t ON t.id = d.task_id
                 WHERE t.status NOT IN ('cancelled', 'expired')
                   AND ((? IS NOT NULL AND df.content_key = ?) OR (df.root_id, df.rel_path) IN (VALUES {place_values}))""",
            (content_key, content_key, *[value for place in places for value in place]),
        ).fetchall()
    ]

    # ⑥ 同名的别的版本（同 stem_key、不同内容）：stem_key 要过 stem_usability 这道判断，PRD、
    # README 这类通用文件名在别的文件夹里也很常见，不代表是同一份的别的版本（见 D7）
    same_rows: list[dict[str, Any]] = []
    stem_key = center.row.get("stem_key")
    if stem_key and stem_usability(stem_key) == STEM_YES:
        same_rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT f.id, f.name, f.ext, f.mtime_ns FROM material_files f
                      JOIN project_material_roots pr ON pr.id = f.root_id
                     WHERE pr.project_id = ? AND f.stem_key = ? AND f.gone_at IS NULL AND f.id NOT IN ({marks})
                       AND (? IS NULL OR f.content_key IS NULL OR f.content_key != ?)
                     ORDER BY f.mtime_ns DESC, f.id DESC LIMIT ?""",
                (project_id, stem_key, *group, content_key, content_key, SAME_NAME_CAP),
            ).fetchall()
        ]

    # ⑦ 需求文件夹
    folders = [
        dict(row)
        for row in connection.execute(
            """SELECT rf.requirement_id, rf.path, rq.title FROM requirement_folders rf
                 JOIN requirements rq ON rq.id = rf.requirement_id WHERE rq.project_id = ?""",
            (project_id,),
        ).fetchall()
    ]
    requirement = _requirement_of(center.row.get("root_path"), str(center.row["rel_path"]), folders)

    # ⑧ 相关（只在关系图的［相关］开着时）：只给共同词和位置，不给材料文字
    related_rows: list[dict[str, Any]] = []
    if related_on and content_key:
        related_rows = [
            dict(row)
            for row in connection.execute(
                """SELECT r.id, r.meeting_id, r.at_ms, r.quote, r.evidence_json FROM relations r
                     JOIN meetings m ON m.id = r.meeting_id AND m.project_id = r.project_id
                    WHERE r.project_id = ? AND r.kind = 'related' AND r.status = 'shown' AND r.content_key = ?
                    ORDER BY r.score DESC, r.id LIMIT ?""",
                (project_id, content_key, RELATED_CAP * 2),
            ).fetchall()
        ]

    # ⑨ 会议一批（提到、决议、任务、相关用到的会）
    meeting_ids = set(per_meeting)
    for row in relation_rows:
        meeting_ids.update(
            filter(None, (row["meeting_id"], row["decision_meeting_id"], row["task_meeting_id"]))
        )
    meeting_ids.update(filter(None, (row["meeting_id"] for row in deliverable_rows)))
    meeting_ids.update(filter(None, (row["meeting_id"] for row in related_rows)))
    meetings = _meetings(connection, sorted(meeting_ids), today)

    neighbours: list[_Neighbour] = []
    nodes: dict[str, dict[str, Any]] = {}
    stale = asks = False

    def meeting_node(meeting_id: str) -> str:
        node_id = f"m:{meeting_id}"
        meeting = meetings[meeting_id]
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": "meeting",
                "meeting_id": meeting_id,
                "title": meeting["title"],
                "at": _iso(meeting["moment"]),
                "caption": meeting["caption"],
                "audio_url": _audio(meeting["audio_id"]),
            },
        )
        return node_id

    def task_node(row: dict[str, Any], meeting_id: str | None, anchor_ms: Any) -> tuple[str, int]:
        node_id = f"task:{row['task_id']}"
        meeting = meetings.get(meeting_id or "")
        moment = (
            meeting["moment"] + timedelta(milliseconds=int(anchor_ms or 0))
            if meeting and meeting["moment"]
            else None
        )
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": "task",
                "task_id": row["task_id"],
                "title": row["task_title"],
                "status": row["task_status"],
                "meeting_id": meeting_id,
                "meeting_caption": meeting["caption"] if meeting else None,
                "anchor_ms": anchor_ms,
                "at": _iso(moment),
                "audio_url": _audio(meeting["audio_id"]) if meeting else None,
            },
        )
        return node_id, _ms(moment) or 0

    def decision_node(row: dict[str, Any]) -> tuple[str, int, str]:
        node_id = f"dec:{row['decision_id']}"
        meeting = meetings.get(row["decision_meeting_id"] or "")
        moment = decision_moment(meeting["row"], row["decision_ms"]) if meeting else None
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": "decision",
                "decision_id": row["decision_id"],
                "text": row["decision_text"],
                "meeting_id": row["decision_meeting_id"],
                "meeting_caption": meeting["caption"] if meeting else None,
                "start_ms": row["decision_ms"],
                "at": _iso(moment),
                "audio_url": _audio(meeting["audio_id"]) if meeting else None,
            },
        )
        day = (
            graph.local_day(meeting["row"]["recording_date"], meeting["row"]["created_at"])
            if meeting
            else None
        )
        return (
            node_id,
            _ms(moment) or 0,
            graph.affects_label(row["decision_text"], center.row["name"], day, today),
        )

    for row in relation_rows:
        if row["kind"] == "affects":
            if row["decision_text"] is None:
                continue
            node_id, at_ms, label = decision_node(row)
            resolved = row["status"] == "resolved"
            if resolved:
                label = label.rsplit("，", 1)[0] + "，你标过已更新"
            else:
                stale = True
            neighbours.append(
                _Neighbour(
                    4 if resolved else 0,
                    at_ms,
                    node_id,
                    {
                        "id": f"e:aff:{row['id']}",
                        "kind": "affects",
                        "from": node_id,
                        "to": center_id,
                        "state": "ok" if resolved else "ask",
                        "label": label,
                        "relation_id": row["id"],
                        "decision_id": row["decision_id"],
                        "meeting_id": row["decision_meeting_id"] or row["meeting_id"],
                        "at_ms": row["decision_ms"]
                        if row["decision_ms"] is not None
                        else row["at_ms"],
                        "quote": _short(row["decision_text"], relation_read.QUOTE_CHARS),
                    },
                )
            )
        elif row["kind"] == "produced" and row["task_title"] is not None:
            asks = True
            node_id, at_ms = task_node(row, row["task_meeting_id"], row["anchor_ms"])
            neighbours.append(
                _Neighbour(
                    1,
                    at_ms,
                    node_id,
                    {
                        "id": f"e:prod:{row['id']}",
                        "kind": "produced",
                        "from": node_id,
                        "to": center_id,
                        "state": "ask",
                        "label": graph.produced_label(row["evidence_json"], row["task_title"]),
                        "relation_id": row["id"],
                        "task_id": row["task_id"],
                        "meeting_id": row["task_meeting_id"],
                        "at_ms": row["anchor_ms"],
                        "quote": _short(row["anchor_quote"] or "", relation_read.QUOTE_CHARS),
                    },
                )
            )
    seen_deliverables: set[int] = set()
    for row in deliverable_rows:
        if row["deliverable_id"] in seen_deliverables:
            continue
        seen_deliverables.add(row["deliverable_id"])
        node_id, at_ms = task_node(row, row["meeting_id"], row["anchor_ms"])
        neighbours.append(
            _Neighbour(
                2,
                at_ms,
                node_id,
                {
                    "id": f"e:dlv:{row['deliverable_id']}",
                    "kind": "deliverable",
                    "from": node_id,
                    "to": center_id,
                    "state": "ok",
                    "label": graph.deliverable_label(row["task_title"]),
                    "deliverable_id": row["deliverable_id"],
                    "task_id": row["task_id"],
                    "meeting_id": row["meeting_id"],
                    "at_ms": row["anchor_ms"],
                    "quote": _short(row["anchor_quote"] or "", relation_read.QUOTE_CHARS),
                },
            )
        )
    for meeting_id, row in per_meeting.items():
        if meeting_id not in meetings:
            continue
        node_id = meeting_node(meeting_id)
        edge = {
            "id": f"e:file:{center.file_id}:{meeting_id}",
            "kind": "mentioned",
            "from": node_id,
            "to": center_id,
            "label": graph.mention_label(row),
            "meeting_id": meeting_id,
            "at_ms": row["first_ms"],
            "quote": _short(row["quote"] or "", relation_read.QUOTE_CHARS),
            "source": row["source"],
            "stem_key": row["stem_key"],
            "needle": row["needle"],
        }
        if row["relation_id"] is not None:
            edge.update(
                origin="manual" if row["via"] == "manual" else "llm", relation_id=row["relation_id"]
            )
        neighbours.append(
            _Neighbour(
                3,
                _ms(meetings[meeting_id]["moment"]) or 0,
                node_id,
                edge,
                0 if row["via"] == "literal" else 1,
            )
        )
    stem = str(center.row.get("stem") or graph.file_stem(center.row["name"]))
    for row in same_rows:
        node_id = f"file:{row['id']}"
        moment = _file_moment(row["mtime_ns"])
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "kind": "file",
                "file_id": int(row["id"]),
                "name": row["name"],
                "ext": row["ext"],
                "at": _iso(moment),
            },
        )
        neighbours.append(
            _Neighbour(
                5,
                _ms(moment) or 0,
                node_id,
                {
                    "id": f"e:same:{row['id']}:{center.file_id}",
                    "kind": "same_name",
                    "from": node_id,
                    "to": center_id,
                    "label": f"同属『{stem}』",
                },
            )
        )
    if requirement is not None:
        node_id = f"r:{requirement['requirement_id']}"
        nodes[node_id] = {
            "id": node_id,
            "kind": "requirement",
            "requirement_id": requirement["requirement_id"],
            "title": requirement["title"],
        }
        neighbours.append(
            _Neighbour(
                6,
                0,
                node_id,
                {
                    "id": f"e:belongs:{requirement['requirement_id']}:{center.file_id}",
                    "kind": "belongs",
                    "from": node_id,
                    "to": center_id,
                    "label": f"同属需求『{requirement['title']}』的文件夹",
                },
            )
        )
    related_meetings: set[str] = set()
    for row in related_rows:
        meeting_id = row["meeting_id"]
        if (
            meeting_id in per_meeting
            or meeting_id in related_meetings
            or meeting_id not in meetings
        ):
            continue
        if len(related_meetings) >= RELATED_CAP:
            break
        related_meetings.add(meeting_id)
        evidence = related._json_obj(row["evidence_json"])
        words = [str(word) for word in evidence.get("words") or []]
        material = evidence.get("material") if isinstance(evidence.get("material"), dict) else {}
        node_id = meeting_node(meeting_id)
        neighbours.append(
            _Neighbour(
                7,
                _ms(meetings[meeting_id]["moment"]) or 0,
                node_id,
                {
                    "id": f"e:rel:{row['id']}",
                    "kind": "related",
                    "from": node_id,
                    "to": center_id,
                    "state": "ok",
                    "label": _words_label(words),
                    "relation_id": row["id"],
                    "meeting_id": meeting_id,
                    "at_ms": row["at_ms"],
                    "quote": _short(row["quote"] or "", relation_read.QUOTE_CHARS),
                    "words": words,
                    # 只给位置，不给材料里的字
                    "passage": {
                        "loc": material.get("loc") or "",
                        "content_key": material.get("content_key"),
                        "ordinal": material.get("ordinal"),
                    },
                },
            )
        )

    neighbours.sort(key=lambda item: (item.level, item.order, -item.at_ms, item.node_id))
    drawn: list[str] = []
    hidden_nodes: dict[str, _Neighbour] = {}
    for item in neighbours:
        if item.node_id in drawn or item.node_id in hidden_nodes:
            continue
        if len(drawn) < MAP_NEIGHBOURS:
            drawn.append(item.node_id)
        else:
            hidden_nodes[item.node_id] = item
    drawn_set = set(drawn)
    edges = [item.edge for item in neighbours if item.node_id in drawn_set]

    # 包含关系：两端都画出来才画
    for node_id in drawn:
        node = nodes[node_id]
        meeting_id = node.get("meeting_id")
        if (
            node["kind"] not in ("decision", "task")
            or not meeting_id
            or f"m:{meeting_id}" not in drawn_set
        ):
            continue
        if node["kind"] == "decision":
            edges.append(
                {
                    "id": f"e:in:{node['decision_id']}",
                    "kind": "in_meeting",
                    "from": f"m:{meeting_id}",
                    "to": node_id,
                    "label": "",
                    "meeting_id": meeting_id,
                    "at_ms": node["start_ms"],
                }
            )
        else:
            edges.append(_task_from_edge(node, meeting_id))

    # 陆续补上提到的原话（字面行没有 quote）：一条语句按 (meeting_id, start_ms) 取当前逐字稿的段
    wanted = [
        (edge["meeting_id"], int(edge["at_ms"]))
        for edge in edges
        if edge["kind"] == "mentioned" and not edge["quote"] and edge.get("at_ms") is not None
    ]
    if wanted:
        quotes = _segments_at(connection, wanted)
        for edge in edges:
            if edge["kind"] == "mentioned" and not edge["quote"]:
                edge["quote"] = quotes.get((edge["meeting_id"], int(edge.get("at_ms") or -1)), "")

    # 没画出来的：带上节点本身和那条线（和画出来的一样的字段），面板里点了能直接打开它的面板
    hidden = [
        {
            "edge_id": item.edge["id"],
            "label": item.edge["label"],
            "node_id": item.node_id,
            "node_label": _node_label(nodes[item.node_id]),
            "node": nodes[item.node_id],
            "edge": item.edge,
        }
        for item in list(hidden_nodes.values())[:HIDDEN_ROWS]
    ]
    return {
        "center": _center_payload(center, stale=stale, asks=asks),
        "nodes": [nodes[node_id] for node_id in drawn],
        "edges": edges,
        "hidden": hidden,
        "hidden_count": len(hidden_nodes),
    }


def _task_from_edge(node: dict[str, Any], meeting_id: str) -> dict[str, Any]:
    anchor = node.get("anchor_ms")
    label = TASK_FROM_TEXT + (f" · {graph._clock_hms(anchor)}" if anchor is not None else "")
    return {
        "id": f"e:task:{node['task_id']}",
        "kind": "task_from",
        "from": f"m:{meeting_id}",
        "to": node["id"],
        "label": label,
        "meeting_id": meeting_id,
        "at_ms": anchor,
    }


def _node_label(node: dict[str, Any]) -> str:
    kind = node["kind"]
    if kind == "meeting":
        return node.get("caption") or node.get("title") or ""
    if kind == "decision":
        return f"决议『{_short(node.get('text') or '', 20)}』"
    if kind == "task":
        return f"任务『{node.get('title') or ''}』"
    if kind == "requirement":
        return f"需求『{node.get('title') or ''}』"
    return node.get("name") or ""


def _meetings(connection: Any, meeting_ids: list[str], today: date) -> dict[str, dict[str, Any]]:
    if not meeting_ids:
        return {}
    rows = connection.execute(
        f"""SELECT m.id, m.title, m.recording_date, m.created_at, m.project_id,
                   {AUDIO_ID_SQL.format(meeting="m.id")} AS audio_id
              FROM meetings m WHERE m.id IN ({_marks(meeting_ids)})""",
        meeting_ids,
    ).fetchall()
    return {
        row["id"]: {
            "row": dict(row),
            "title": row["title"],
            "moment": _meeting_moment(row["recording_date"], row["created_at"]),
            "caption": _caption(row["title"], row["recording_date"], row["created_at"], today),
            "audio_id": row["audio_id"],
        }
        for row in rows
    }


def _segments_at(connection: Any, anchors: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    pairs = sorted(set(anchors))
    values = ", ".join("(?, ?)" for _ in pairs)
    meeting_ids = sorted({meeting_id for meeting_id, _ in pairs})
    rows = connection.execute(
        f"""SELECT s.meeting_id, s.start_ms, s.text FROM meetings m
              JOIN segments s ON s.version_id = m.current_transcript_version_id
             WHERE m.id IN ({_marks(meeting_ids)}) AND (s.meeting_id, s.start_ms) IN (VALUES {values})""",
        (*meeting_ids, *[value for pair in pairs for value in pair]),
    ).fetchall()
    return {
        (row["meeting_id"], int(row["start_ms"])): _short(
            row["text"] or "", relation_read.QUOTE_CHARS
        )
        for row in rows
    }


# ---------------------------------------------------------------------- 来龙去脉

_TRACE_KINDS = frozenset(
    {"mention", "deliverable", "affects_resolved", "later_changed", "restated"}
)


@dataclass
class _Step:
    target: str
    level: int
    counted: bool
    edge: dict[str, Any]


def _level(kind: str, via: str | None) -> int:
    if kind in ("deliverable", "affects_resolved"):
        return 0
    if via == "manual":
        return 0
    if via in ("literal", "rule"):
        return 1
    return 2


class _Tracer:
    """一次来龙去脉：节点信息缓存、每个节点的展开结果缓存。"""

    def __init__(self, connection: Any, today: date) -> None:
        self.connection = connection
        self.today = today
        self.info: dict[str, dict[str, Any]] = {}
        self.expanded: dict[str, list[_Step]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.center_file: _Center | None = None
        self.center_id = ""

    # -------------------------------------------------------------- 节点信息
    def _put_meeting(self, row: dict[str, Any]) -> None:
        moment = _meeting_moment(row["recording_date"], row["created_at"])
        node_id = f"m:{row['id']}"
        self.info[node_id] = {
            "id": node_id,
            "kind": "meeting",
            "meeting_id": row["id"],
            "title": row["label"],
            "caption": _caption(row["label"], row["recording_date"], row["created_at"], self.today),
            "at": _iso(moment),
            "audio_url": _audio(row["audio_id"]),
            "_ms": _ms(moment),
            "_project": row.get("project_id"),
        }

    def _put_decision(self, row: dict[str, Any]) -> None:
        moment = decision_moment(
            {"recording_date": row["recording_date"], "created_at": row["created_at"]}, row["ms"]
        )
        node_id = f"dec:{row['id']}"
        self.info[node_id] = {
            "id": node_id,
            "kind": "decision",
            "decision_id": row["id"],
            "text": row["label"],
            "meeting_id": row["meeting_id"],
            "start_ms": row["ms"],
            "meeting_caption": _caption(
                row["caption"], row["recording_date"], row["created_at"], self.today
            ),
            "at": _iso(moment),
            "audio_url": _audio(row["audio_id"]),
            "_ms": _ms(moment),
            "_project": row.get("project_id"),
            "_day": graph.local_day(row["recording_date"], row["created_at"]),
        }

    def _put_task(self, row: dict[str, Any]) -> None:
        base = (
            _meeting_moment(row["recording_date"], row["created_at"]) if row["meeting_id"] else None
        )
        moment = base + timedelta(milliseconds=int(row["ms"] or 0)) if base else None
        node_id = f"task:{row['id']}"
        self.info[node_id] = {
            "id": node_id,
            "kind": "task",
            "task_id": row["id"],
            "title": row["label"],
            "status": row["status"],
            "meeting_id": row["meeting_id"],
            "anchor_ms": row["ms"],
            "meeting_caption": (
                _caption(row["caption"], row["recording_date"], row["created_at"], self.today)
                if row["meeting_id"]
                else None
            ),
            "at": _iso(moment),
            "audio_url": _audio(row["audio_id"]),
            "_ms": _ms(moment),
            "_project": row.get("project_id"),
        }

    def _put_file(self, row: dict[str, Any]) -> None:
        moment = _file_moment(row["mtime_ns"])
        node_id = f"file:{row['id']}"
        self.info[node_id] = {
            "id": node_id,
            "kind": "file",
            "file_id": int(row["id"]),
            "name": row["label"],
            "ext": row["ext"],
            "at": _iso(moment),
            "_ms": _ms(moment),
            "_gone": row["status"] is not None,
            "_stem": row.get("stem"),
            "_stem_key": row.get("stem_key"),
        }

    _INFO_SQL = """
SELECT 'm' AS p, m.id, m.title AS label, m.recording_date, m.created_at, NULL AS ms, NULL AS meeting_id,
       NULL AS status, NULL AS mtime_ns, NULL AS ext, {audio_m} AS audio_id, NULL AS caption, m.project_id,
       NULL AS stem, NULL AS stem_key
  FROM meetings m WHERE m.id IN ({meetings})
UNION ALL
SELECT 'dec', d.id, d.text, m.recording_date, m.created_at, d.start_ms, d.meeting_id, NULL, NULL, NULL,
       {audio_d}, m.title, m.project_id, NULL, NULL
  FROM decisions d JOIN meetings m ON m.id = d.meeting_id
 WHERE d.gone_at IS NULL AND (d.id IN ({decisions}) OR d.meeting_id = ?)
UNION ALL
SELECT 'task', t.id, t.title, m.recording_date, m.created_at, t.anchor_ms, t.meeting_id, t.status, NULL, NULL,
       {audio_t}, m.title, t.project_id, NULL, NULL
  FROM tasks t LEFT JOIN meetings m ON m.id = t.meeting_id
 WHERE t.id IN ({tasks}) OR (t.meeting_id = ? AND t.status IN ({statuses}))
UNION ALL
SELECT 'file', f.id, f.name, NULL, NULL, NULL, NULL, f.gone_at, f.mtime_ns, f.ext, NULL, NULL, NULL, f.stem,
       f.stem_key
  FROM material_files f WHERE f.id IN ({files})"""

    def _load(self, ids: set[str], containment_meeting: str | None) -> list[dict[str, Any]]:
        """一条语句：还没有信息的节点，加上 containment_meeting 这场会的决议和任务（包含关系）。"""
        by_prefix: dict[str, list[str]] = {"m": [], "dec": [], "task": [], "file": []}
        for node_id in ids:
            prefix, _, value = node_id.partition(":")
            if prefix in by_prefix and node_id not in self.info:
                by_prefix[prefix].append(value)
        if not containment_meeting and not any(by_prefix.values()):
            return []
        sql = self._INFO_SQL.format(
            audio_m=AUDIO_ID_SQL.format(meeting="m.id"),
            audio_d=AUDIO_ID_SQL.format(meeting="m.id"),
            audio_t=AUDIO_ID_SQL.format(meeting="m.id"),
            meetings=_marks(by_prefix["m"]),
            decisions=_marks(by_prefix["dec"]),
            tasks=_marks(by_prefix["task"]),
            statuses=_marks(TRACE_TASK_STATUSES),
            files=_marks(by_prefix["file"]),
        )
        params = [
            *by_prefix["m"],
            *by_prefix["dec"],
            containment_meeting or "",
            *by_prefix["task"],
            containment_meeting or "",
            *TRACE_TASK_STATUSES,
            *[int(value) for value in by_prefix["file"]],
        ]
        rows = [dict(row) for row in self.connection.execute(sql, params).fetchall()]
        contained = []
        for row in rows:
            put = {
                "m": self._put_meeting,
                "dec": self._put_decision,
                "task": self._put_task,
                "file": self._put_file,
            }[row["p"]]
            put(row)
            if (
                containment_meeting
                and row["p"] in ("dec", "task")
                and row["meeting_id"] == containment_meeting
            ):
                contained.append(row)
        return contained

    # -------------------------------------------------------------- 线
    def _edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        return self.edges.setdefault(edge["id"], edge)

    def _containment(self, meeting_id: str, node_id: str) -> dict[str, Any]:
        """包含关系的线：会议到决议（in_meeting）、会议到任务（task_from）。"""
        node = self.info[node_id]
        if node["kind"] == "decision":
            edge = {
                "id": f"e:in:{node['decision_id']}",
                "kind": "in_meeting",
                "from": f"m:{meeting_id}",
                "to": node_id,
                "label": "",
                "meeting_id": meeting_id,
                "at_ms": node["start_ms"],
            }
        else:
            edge = _task_from_edge(node, meeting_id)
        edge["level"] = 1
        return self._edge(edge)

    def expand(self, node_id: str, *, first_from_center: bool) -> list[_Step]:
        if node_id in self.expanded:
            steps = self.expanded[node_id]
        else:
            steps = self._expand(node_id)
            self.expanded[node_id] = steps
        if first_from_center and self.center_file is not None and node_id == self.center_id:
            steps = steps + self._same_name()
        return steps

    def _expand(self, node_id: str) -> list[_Step]:
        prefix = node_id.partition(":")[0]
        # 中心文件按同内容那一组读（和局部图一样）：挪过位置、找不到活文件时旧 id 上的线也算在它上面
        group = (
            self.center_file.group
            if self.center_file is not None and node_id == self.center_id
            else None
        )
        raw = relation_read.edges_of(self.connection, node_id, _TRACE_KINDS, file_group=group)
        others = set()
        for edge in raw:
            others.update((edge["from"], edge["to"]))
        others.discard(node_id)
        info = self.info.get(node_id, {})
        own_meeting = info.get("meeting_id") if prefix in ("dec", "task") else None
        if own_meeting:
            others.add(f"m:{own_meeting}")
        contained = self._load(others, node_id.partition(":")[2] if prefix == "m" else None)
        steps: list[_Step] = []
        for edge in raw:
            other = edge["to"] if edge["from"] == node_id else edge["from"]
            other_info = self.info.get(other)
            if other_info is None:
                continue
            kind = edge["kind"]
            if kind == "deliverable":
                task = self.info.get(edge["from"])
                if task is None or task.get("status") not in TRACE_TASK_STATUSES:
                    continue
            steps.append(
                _Step(
                    other, _level(kind, edge.get("via")), True, self._edge(self._trace_edge(edge))
                )
            )
        # 包含关系：会议到它的决议和（已确认、进行中、完成的）任务；决议、任务到它的会
        if prefix == "m":
            meeting_id = node_id.partition(":")[2]
            for row in contained:
                target = f"{row['p']}:{row['id']}"
                steps.append(_Step(target, 1, False, self._containment(meeting_id, target)))
        elif own_meeting and f"m:{own_meeting}" in self.info:
            if prefix != "task" or info.get("status") in TRACE_TASK_STATUSES:
                steps.append(
                    _Step(f"m:{own_meeting}", 1, False, self._containment(own_meeting, node_id))
                )
        return steps

    def _same_name(self) -> list[_Step]:
        center = self.center_file
        assert center is not None
        stem_key = center.row.get("stem_key")
        # PRD、README 这类通用文件名在别的文件夹里也很常见，不代表是同一份的别的版本（见 D7）
        if not stem_key or stem_usability(stem_key) != STEM_YES:
            return []
        group = center.group
        rows = self.connection.execute(
            f"""SELECT f.id, f.name AS label, f.ext, f.mtime_ns, NULL AS status, f.stem, f.stem_key
                  FROM material_files f JOIN project_material_roots pr ON pr.id = f.root_id
                 WHERE pr.project_id = ? AND f.stem_key = ? AND f.gone_at IS NULL AND f.id NOT IN ({_marks(group)})
                   AND (? IS NULL OR f.content_key IS NULL OR f.content_key != ?)
                 ORDER BY f.mtime_ns DESC, f.id DESC LIMIT ?""",
            (
                center.project_id,
                stem_key,
                *group,
                center.content_key,
                center.content_key,
                SAME_NAME_CAP,
            ),
        ).fetchall()
        stem = str(center.row.get("stem") or graph.file_stem(center.row["name"]))
        steps = []
        for row in rows:
            self._put_file(dict(row))
            node_id = f"file:{row['id']}"
            edge = {
                "id": f"e:same:{row['id']}:{center.file_id}",
                "kind": "same_name",
                "from": node_id,
                "to": self.center_id,
                "label": f"同属『{stem}』",
                "level": 2,
            }
            steps.append(_Step(node_id, 2, True, self._edge(edge)))
        return steps

    def _trace_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        kind = edge["kind"]
        level = _level(kind, edge.get("via"))
        base = {
            "from": edge["from"],
            "to": edge["to"],
            "meeting_id": edge.get("meeting_id"),
            "at_ms": edge.get("at_ms"),
            "quote": _short(edge.get("quote") or "", relation_read.QUOTE_CHARS),
            "level": level,
        }
        if kind == "mention":
            meeting_id = edge["meeting_id"]
            needle = edge.get("needle") or ""
            label = (
                f"会上说『{needle}』· {graph._clock_hms(edge.get('at_ms'))}"
                if needle
                else "会上提到"
            )
            item = {
                "id": f"e:file:{edge['file_id']}:{meeting_id}",
                "kind": "mentioned",
                "label": label,
                "needle": needle,
            }
            if edge.get("relation_id") is not None:
                item.update(
                    relation_id=edge["relation_id"],
                    origin="manual" if edge.get("via") == "manual" else "llm",
                )
            return {**base, **item}
        if kind == "deliverable":
            task = self.info.get(edge["from"], {})
            return {
                **base,
                "id": f"e:dlv:{edge['deliverable_id']}",
                "kind": "deliverable",
                "label": graph.deliverable_label(task.get("title")),
                "deliverable_id": edge["deliverable_id"],
                "task_id": task.get("task_id"),
            }
        if kind == "affects_resolved":
            decision = self.info.get(edge["from"], {})
            file = self.info.get(edge["to"], {})
            text = decision.get("text") or edge.get("quote") or ""
            day = decision.get("_day")
            label = graph.affects_label(text, file.get("name") or "", day, self.today).rsplit(
                "，", 1
            )[0]
            return {
                **base,
                "id": f"e:aff:{edge['relation_id']}",
                "kind": "affects",
                "state": "ok",
                "label": label + "，你标过已更新",
                "relation_id": edge["relation_id"],
                "decision_id": edge.get("decision_id"),
            }
        # 后来改了、后来又提到：箭头指向后一条
        first, second = edge["from"], edge["to"]
        first_ms = self.info.get(first, {}).get("_ms") or 0
        second_ms = self.info.get(second, {}).get("_ms") or 0
        if first_ms > second_ms:
            first, second = second, first
        return {
            **base,
            "from": first,
            "to": second,
            "id": f"e:{'later' if kind == 'later_changed' else 'restated'}:{edge['relation_id']}",
            "kind": kind,
            "label": LATER_CHANGED_TEXT if kind == "later_changed" else RESTATED_TEXT,
            "relation_id": edge["relation_id"],
        }


def _trace_center(tracer: _Tracer, node: str) -> dict[str, Any]:
    """中心：文件按来历找活文件；会议、决议、任务各自的 404 和没归项目的 409。1 到 2 条语句。"""
    prefix, _, value = node.partition(":")
    connection = tracer.connection
    if prefix == "file":
        center = _resolve_file(connection, int(value))
        tracer.center_file = center
        tracer.center_id = f"file:{center.file_id}"
        row = center.row
        tracer._put_file(
            {
                "id": center.file_id,
                "label": row["name"],
                "ext": row["ext"],
                "mtime_ns": row.get("mtime_ns"),
                "status": None if center.live is not None else "gone",
                "stem": row.get("stem"),
                "stem_key": row.get("stem_key"),
            }
        )
        return _center_payload(center, stale=False, asks=False)
    audio = AUDIO_ID_SQL.format(meeting="m.id")
    if prefix == "m":
        row = connection.execute(
            f"""SELECT m.id, m.title AS label, m.recording_date, m.created_at, m.project_id, {audio} AS audio_id
                  FROM meetings m WHERE m.id = ?""",
            (value,),
        ).fetchone()
        if row is None:
            raise LocalError(404, MEETING_MISSING)
        if not row["project_id"]:
            raise LocalError(409, MEETING_NO_PROJECT)
        tracer._put_meeting(dict(row))
    elif prefix == "dec":
        row = connection.execute(
            f"""SELECT d.id, d.text AS label, d.start_ms AS ms, d.meeting_id, m.title AS caption, m.recording_date,
                       m.created_at, m.project_id, {audio} AS audio_id
                  FROM decisions d JOIN meetings m ON m.id = d.meeting_id WHERE d.id = ? AND d.gone_at IS NULL""",
            (value,),
        ).fetchone()
        if row is None:
            raise LocalError(404, DECISION_MISSING)
        if not row["project_id"]:
            raise LocalError(409, MEETING_NO_PROJECT)
        tracer._put_decision(dict(row))
    else:
        row = connection.execute(
            f"""SELECT t.id, t.title AS label, t.status, t.anchor_ms AS ms, t.meeting_id, m.title AS caption,
                       m.recording_date, m.created_at, t.project_id, {audio} AS audio_id
                  FROM tasks t LEFT JOIN meetings m ON m.id = t.meeting_id WHERE t.id = ?""",
            (value,),
        ).fetchone()
        if row is None:
            raise LocalError(404, TASK_MISSING)
        tracer._put_task(dict(row))
    tracer.center_id = node
    return _public(tracer.info[node])


def _public(node: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in node.items() if not key.startswith("_")}


def trace(connection: Any, node: str, *, today: date | None = None) -> dict[str, Any]:
    """来龙去脉：从中心往前、往后各走最多 3 个算数的步。每一步在还没进链的候选里按
    （级，时间差的绝对值，节点 id）取最好的一个；往前每一步都严格早于当前节点，往后严格晚于。
    包含关系的一步（会议到决议、任务）只在走进去以后还能沿同一方向接着走时才取（见下面的修正）。"""
    if not re.match(NODE_PATTERN, node):
        raise LocalError(422, "节点格式不对")
    tracer = _Tracer(connection, today or datetime.now().astimezone().date())
    center_payload = _trace_center(tracer, node)
    center_id = tracer.center_id
    chain_nodes = {center_id}
    sides: dict[str, list[str]] = {"back": [], "forward": []}
    chain_edges: set[str] = set()
    cut = {"back": False, "forward": False}
    spare = [TRACE_LOOKAHEAD]

    def candidates_of(
        node_id: str, direction: str, taken: set[str]
    ) -> list[tuple[int, int, str, _Step]]:
        current_ms = tracer.info[node_id].get("_ms")
        if current_ms is None:
            return []
        found = []
        for step in tracer.expand(node_id, first_from_center=node_id == center_id):
            if step.target in taken:
                continue
            target_ms = tracer.info.get(step.target, {}).get("_ms")
            if target_ms is None:
                continue
            if (direction == "back" and target_ms < current_ms) or (
                direction == "forward" and target_ms > current_ms
            ):
                found.append((step.level, abs(target_ms - current_ms), step.target, step))
        return sorted(found, key=lambda item: item[:3])

    def leads_on(step: _Step, direction: str) -> bool:
        """包含关系的一步（会议到决议、任务）只在那个节点还能沿同一方向往下走时才走。多展开的那一次
        之后反正要用（走进去就是链上的下一个节点）；没走通的最多 TRACE_LOOKAHEAD 次，用完了就不再试。"""
        if step.target in tracer.expanded:
            return bool(candidates_of(step.target, direction, chain_nodes | {step.target}))
        if spare[0] <= 0:
            return False
        ok = bool(candidates_of(step.target, direction, chain_nodes | {step.target}))
        if not ok:
            spare[0] -= 1
        return ok

    for direction in ("back", "forward"):
        current = center_id
        counted = 0
        while True:
            candidates = candidates_of(current, direction, chain_nodes)
            if not candidates:
                break
            if counted >= TRACE_STEPS or len(chain_nodes) >= TRACE_NODES:
                cut[direction] = True
                break
            # 对规格取法的修正：规格是在候选里直接取（级，时间差，id）最小的一个。包含关系级 1、时间差小，
            # 几乎总先被取中，会上一条没有下文的决议就把这个方向截断了（cut 还是 false）。这里仍按那个
            # 顺序看，但包含关系的一步只在它还能接着走时才取；都走不通时退回原来的取法（取排第一的，
            # 这时链就停在它上面）。
            best = next(
                (step for *_order, step in candidates if step.counted or leads_on(step, direction)),
                candidates[0][3],
            )
            chain_nodes.add(best.target)
            sides[direction].append(best.target)
            chain_edges.add(best.edge["id"])
            if best.counted:
                counted += 1
            current = best.target
    chain = list(reversed(sides["back"])) + [center_id] + sides["forward"]
    in_chain = set(chain)
    edges = []
    for edge in tracer.edges.values():
        if edge["from"] in in_chain and edge["to"] in in_chain:
            edges.append({**edge, "on_chain": edge["id"] in chain_edges})
    return {
        "center": center_payload,
        "nodes": [_public(tracer.info[node_id]) for node_id in chain if node_id != center_id],
        "edges": edges,
        "chain": chain,
        "center_index": len(sides["back"]),
        "cut": cut,
    }
