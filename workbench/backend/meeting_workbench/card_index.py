"""00 索引.md v2（第四期 4h）：交给 Claude Code 的项目背景。

- 只写已经定下来的东西：纪要里来的文字（决议、摘要）、任务标题、文件位置。不写相关、可能过时和影响、
  还在问你的产出建议（任何 suggested 的行）、材料原文、没记入的词、分数。
- 只算卡片在这个根目录里的会：会议链接、决议、关键文件的场数、摘要都只取这些会。补写历史卡片答了
  「先不要」、还在等你选项目、暂停的项目，都不会借索引漏出去。
- 渲染只由库里的行决定：日期写 YYYY-MM-DD，时间点由 start_ms 写成 HH:MM:SS，每节按固定的键排序，
  不用当前时间；同样的数据渲染两次一字不差，插入顺序打乱也一样。
- load_index_data 最多 9 条语句：项目、卡片、行动项（v1 的 3 条），需求 1 条（文件夹和关联会用子查询
  带出），决议 2 条（decisions.project_decisions），交付物 1 条，关键文件的提到 1 条，最新 10 场的
  纪要 1 条。
- cards.render_index 调这里；这个模块不写盘，也不碰文件系统。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from . import decisions as decisions_module
from .cards import (
    APP_FILE_NAMES,
    ASSIGNEE_LABELS,
    ENABLED_KEY,
    GENERATED_BY,
    PAUSED_KEY,
    TASK_STATUS_LABELS,
    USER_EDITED,
    _one_line,
    _yaml,
)
from .db import GRAPH_REV_KEY
from .file_stems import STEM_NO, stem_usability
from .relation_read import mention_union
from .rendering import format_clock

INDEX_VERSION = 2

DECISIONS_PER_REQUIREMENT = 10
OTHER_DECISIONS = 20
CLOSED_REQUIREMENTS = 20
KEY_FILES = 15
KEY_FILE_MIN_MEETINGS = 2
SUMMARY_MEETINGS = 10
SUMMARY_CHARS = 90
PRIORITIES = ("P0", "P1", "P2", "P3")

# 固定字句（进用词测试）
QUOTE_MAINTAINED = "> 本文件由声档自动维护，请勿手改。把这个项目文件夹交给 Claude Code 时，先让它读这一份。"
QUOTE_NO_MATERIAL = "> 这里只有会上说过的话、任务和文件位置，不摘材料的内容；文件内容请直接打开文件看。"
TITLE_SUFFIX = " · 会议记录索引"
TASKS_HEADING = "## 进行中的行动项"
NO_TASKS = "暂无进行中的行动项。"
REQUIREMENTS_HEADING = "## 需求"
CLOSED_HEADING = "### 已完成或搁置"
OTHER_DECISIONS_HEADING = "## 其他决议（不属于哪个需求）"
KEY_FILES_HEADING = "## 关键文件"
MEETINGS_HEADING = "## 会议（按时间倒序）"
NO_MEETINGS = "这个项目还没有会议卡片。"
PRIORITY_LABEL = "- 优先级：{priority}"
FOLDER_LABEL = "- 文件夹：{path}"
MEETINGS_LABEL = "- 会议：{links}"
ELSEWHERE_NOTE = "（另有 {n} 场会的纪要不在这个文件夹）"
ELSEWHERE_ONLY = "另有 {n} 场会的纪要不在这个文件夹"
DECIDED_LABEL = "- 定了什么："
MORE_DECISIONS = "  - 另有 {n} 条，见各场会的纪要"
ACTIONS_LABEL = "- 行动项：{items}"
PRODUCED_LABEL = "- 产出：{items}"
CHANGED_SUFFIX = " · 这次改了 {date} 定的『{text}』"
RESTATED_SUFFIX = " · {dates} 后来又提到"
DELIVERABLE_OF = "「{task}」的产出"
MENTIONED_IN = "在 {n} 场会上被提到"
SUMMARY_LINE = "  - 摘要：{text}"
AI_ATTRIBUTED = " · AI 自动归属"
CARD_EDITED = " · 你改过这张卡"
REQUIREMENT_STATUS = {"active": "进行中", "done": "已完成", "shelved": "搁置"}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CARD_STATES = "('synced', 'user_edited')"
_IN_ROOT = f"""SELECT meeting_id FROM meeting_cards
    WHERE project_id = :pid AND root_path = :root AND rel_path IS NOT NULL AND state IN {_CARD_STATES}"""
# 任务状态：还在等你确认的不算定下来；取消、过期的不算
_SETTLED_TASKS = "('confirmed', 'in_progress', 'done')"


@dataclass
class IndexData:
    """渲染一份索引要的全部数据（从库里读好，渲染本身不碰库）。"""

    project_name: str
    root: str
    roots: dict[int, str] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)  # 按开会时间倒序，带 start、day、file_name
    tasks: list[dict[str, Any]] = field(default_factory=list)
    requirements: list[dict[str, Any]] = field(default_factory=list)  # 进行中的，已排好
    closed: list[dict[str, Any]] = field(default_factory=list)  # 已完成或搁置，已排好、截好
    decisions: list[dict[str, Any]] = field(default_factory=list)  # 这个根目录的会里还在的，最新的在前
    deliverables: list[dict[str, Any]] = field(default_factory=list)
    mentioned: list[dict[str, Any]] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)
    requirement_ids: set[str] = field(default_factory=set)  # 这个项目的全部需求（含已完成、搁置）


# ---------------------------------------------------------------------- 签名


def index_signature(connection: Any, project_id: str, root: str) -> str:
    """什么时候要重渲：INDEX_VERSION、graph_rev、项目各根目录 stems_rev 之和、cards_enabled、是否暂停。
    一条语句。graph_rev 跟着会议、任务、需求、卡片、词条、提到、决议、交付物和非相关的 relations 变；
    stems_rev 管改名和挪位置。root 只是和缓存键对齐，不进签名（签名按（项目，根目录）分开记）。"""
    del root
    row = connection.execute(
        """SELECT (SELECT value FROM app_state WHERE key = ?) AS rev,
                  (SELECT COALESCE(SUM(s.stems_rev), 0) FROM project_material_roots r
                     JOIN material_index_state s ON s.root_id = r.id WHERE r.project_id = ?) AS stems,
                  (SELECT value FROM app_state WHERE key = ?) AS enabled,
                  (SELECT value FROM app_state WHERE key = ?) AS paused""",
        (GRAPH_REV_KEY, project_id, ENABLED_KEY, PAUSED_KEY),
    ).fetchone()
    try:
        paused_list = json.loads(row["paused"] or "[]")
    except ValueError:
        paused_list = []
    paused = 1 if isinstance(paused_list, list) and project_id in paused_list else 0
    enabled = 0 if row["enabled"] == "0" else 1
    return f"{INDEX_VERSION}:{row['rev'] or 0}:{int(row['stems'] or 0)}:{enabled}:{paused}"


# ---------------------------------------------------------------------- 读库


def meeting_start(row: dict[str, Any]) -> datetime:
    """会议的本地开始时间：录音日期（没带时区按本机），否则 created_at（没带时区按 UTC）。
    都没有时退回 1970 年，不用当前时间。"""
    for key in ("recording_date", "created_at"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC) if key == "created_at" else value.astimezone()
        return value.astimezone()
    return _EPOCH.astimezone()


def load_index_data(connection: Any, project_id: str, root: str) -> IndexData:
    """最多 9 条语句（见模块说明）。"""
    params = {"pid": project_id, "root": root}
    # 1. 项目和它的根目录
    project = connection.execute(
        """SELECT p.name,
                  (SELECT json_group_array(json_array(r.id, r.path))
                     FROM project_material_roots r WHERE r.project_id = p.id) AS roots_json
             FROM projects p WHERE p.id = ?""",
        (project_id,),
    ).fetchone()
    data = IndexData(project_name=str(project["name"]) if project else "", root=root)
    if project is not None:
        for root_id, path in json.loads(project["roots_json"] or "[]"):
            data.roots[int(root_id)] = str(path)
    # 2. 卡片在这个根目录里的会
    cards = [
        dict(row)
        for row in connection.execute(
            """SELECT c.meeting_id, c.rel_path, c.state, m.title, m.recording_date,
                      m.created_at, m.project_origin
                 FROM meeting_cards c JOIN meetings m ON m.id = c.meeting_id
                WHERE c.project_id = :pid AND c.root_path = :root AND c.rel_path IS NOT NULL
                  AND c.state IN ('synced', 'user_edited')""",
            params,
        ).fetchall()
    ]
    for card in cards:
        card["start"] = meeting_start(card)  # 每场会只算一次
        card["day"] = card["start"].date().isoformat()
        card["file_name"] = PurePosixPath(card["rel_path"]).name
    cards.sort(key=lambda card: (card["start"], card["meeting_id"]), reverse=True)
    data.cards = cards
    in_root = {card["meeting_id"]: card for card in cards}
    # 3. 进行中的行动项（v1 的查询，顺带 requirement_id）
    data.tasks = [
        dict(row)
        for row in connection.execute(
            """SELECT title, status, assignee, meeting_id, requirement_id FROM tasks
                WHERE project_id = ? AND status IN ('confirmed', 'in_progress')
                ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, created_at, id""",
            (project_id,),
        ).fetchall()
    ]
    # 4. 需求：第一个文件夹、关联的会
    requirements = []
    for row in connection.execute(
        """SELECT q.id, q.title, q.priority, q.status, q.created_at, q.updated_at,
                  (SELECT f.path FROM requirement_folders f WHERE f.requirement_id = q.id
                    ORDER BY f.id LIMIT 1) AS folder,
                  (SELECT json_group_array(rm.meeting_id) FROM requirement_meetings rm
                    WHERE rm.requirement_id = q.id) AS meetings_json
             FROM requirements q WHERE q.project_id = ?""",
        (project_id,),
    ).fetchall():
        item = dict(row)
        linked = sorted(set(json.loads(item.pop("meetings_json") or "[]")))
        here = sorted(
            (in_root[meeting_id] for meeting_id in linked if meeting_id in in_root),
            key=lambda card: (card["start"], card["meeting_id"]),
        )
        item["meetings"] = here
        item["elsewhere"] = len(linked) - len(here)
        requirements.append(item)
    active = [item for item in requirements if item["status"] == "active"]
    active.sort(key=lambda item: (_priority_rank(item["priority"]), item["created_at"], item["id"]))
    closed = [item for item in requirements if item["status"] != "active"]
    closed.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    data.requirements = active
    data.requirement_ids = {item["id"] for item in requirements}
    data.closed = closed[:CLOSED_REQUIREMENTS]
    # 5、6. 决议（只读，含被改掉的，好认出「这次改了」的另一头在不在这个根目录里）
    every = decisions_module.project_decisions(connection, project_id, include_superseded=True)
    home = {item["id"]: item["meeting_id"] for item in every}
    shown = []
    for index, item in enumerate(every):
        if item["superseded"] or item["meeting_id"] not in in_root:
            continue
        card = in_root[item["meeting_id"]]
        shown.append(
            {
                "id": item["id"],
                "meeting_id": item["meeting_id"],
                "text": item["text"],
                "start_ms": item["start_ms"],
                "requirement_id": item["requirement_id"],
                "day": card["day"],
                "order": index,
                "earlier": [
                    ref for ref in item["earlier"] if home.get(ref["decision_id"]) in in_root
                ],
                "restated": [ref for ref in item["restated"] if ref["meeting_id"] in in_root],
            }
        )
    # 后来又提到：一组折成一行，挂在最早那条上
    folded = {
        ref["decision_id"] for item in shown for ref in item["restated"] if ref.get("decision_id")
    }
    shown = [item for item in shown if item["id"] not in folded]
    # 决议日期倒序；同一天按会议 id、纪要里的顺序
    shown.sort(key=lambda item: (-_day_number(item["day"]), item["meeting_id"], item["order"]))
    data.decisions = shown
    # 7. 登记的交付物（只在这个项目自己的根目录里找：先认记下的位置，再按内容标识取 id 最小的一份）
    data.deliverables = [
        dict(row)
        for row in connection.execute(
            f"""SELECT x.deliverable_id, x.task_title, x.requirement_id, f.id AS file_id,
                       f.root_id, f.rel_path
                  FROM (SELECT d.id AS deliverable_id, t.title AS task_title, t.requirement_id,
                               t.created_at AS task_created,
                               COALESCE(
                                 (SELECT f1.id FROM material_files f1
                                    JOIN project_material_roots r1 ON r1.id = f1.root_id
                                   WHERE f1.root_id = df.root_id AND f1.rel_path = df.rel_path
                                     AND f1.gone_at IS NULL AND r1.project_id = :pid),
                                 (SELECT MIN(f2.id) FROM material_files f2
                                    JOIN project_material_roots r2 ON r2.id = f2.root_id
                                   WHERE r2.project_id = :pid AND f2.content_key = df.content_key
                                     AND f2.gone_at IS NULL AND f2.zone != 'cards'),
                                 (SELECT f3.id FROM project_material_roots r3
                                    JOIN material_files f3 ON f3.root_id = r3.id
                                   WHERE df.deliverable_id IS NULL AND r3.project_id = :pid
                                     AND substr(d.url, 1, length(r3.path) + 1) = r3.path || '/'
                                     AND f3.rel_path = substr(d.url, length(r3.path) + 2)
                                     AND f3.gone_at IS NULL
                                   ORDER BY r3.created_at, r3.id LIMIT 1)) AS fid
                          FROM deliverables d
                          JOIN tasks t ON t.id = d.task_id
                          LEFT JOIN deliverable_files df ON df.deliverable_id = d.id
                         WHERE t.project_id = :pid AND d.kind = 'file'
                           AND t.status IN {_SETTLED_TASKS}) x
                  JOIN material_files f ON f.id = x.fid
                 WHERE f.zone != 'cards'
                 ORDER BY x.task_created, x.deliverable_id""",
            {"pid": project_id},
        ).fetchall()
        if row["rel_path"] and PurePosixPath(row["rel_path"]).name not in APP_FILE_NAMES
    ]
    # 8. 在这个根目录的会里至少 2 场被提到的文件：字面和放宽的一条 UNION ALL，按会去重；
    #    同一内容多份时算到本项目根目录里 id 最小的那份
    union = mention_union(
        literal=f"fm.project_id = :pid AND fm.meeting_id IN ({_IN_ROOT})",
        loose=f"r.project_id = :pid AND r.meeting_id IN ({_IN_ROOT})",
    )
    names = ", ".join(f"'{name}'" for name in sorted(APP_FILE_NAMES))
    data.mentioned = [
        dict(row)
        for row in connection.execute(
            f"""WITH mu AS ({union}),
                hits AS (
                  SELECT DISTINCT mu.meeting_id,
                         COALESCE(
                           (SELECT MIN(c.id) FROM material_files c
                              JOIN project_material_roots cr ON cr.id = c.root_id
                             WHERE f.content_key IS NOT NULL AND cr.project_id = :pid
                               AND c.content_key = f.content_key AND c.gone_at IS NULL
                               AND c.zone != 'cards'),
                           CASE WHEN f.gone_at IS NULL AND f.root_id IN (
                                  SELECT id FROM project_material_roots WHERE project_id = :pid)
                                THEN f.id END) AS fid
                    FROM mu JOIN material_files f ON f.id = mu.file_id)
                SELECT f.id AS file_id, f.root_id, f.rel_path, f.stem_key, COUNT(*) AS meetings
                  FROM hits JOIN material_files f ON f.id = hits.fid
                 WHERE f.zone != 'cards' AND f.name NOT IN ({names})
                 GROUP BY f.id HAVING COUNT(*) >= {KEY_FILE_MIN_MEETINGS}
                 ORDER BY meetings DESC, f.rel_path, f.id LIMIT 100""",
            params,
        ).fetchall()
        if stem_usability(str(row["stem_key"] or "")) != STEM_NO
    ]
    # 9. 最新 10 场会的纪要（只取摘要那一段）
    latest = [card["meeting_id"] for card in cards[:SUMMARY_MEETINGS]]
    if latest:
        from .graph import minutes_outline  # graph 引用 cards，这里晚一点再引

        marks = ", ".join("?" for _ in latest)
        for row in connection.execute(
            f"""SELECT m.id, mv.markdown FROM meetings m
                  JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                 WHERE m.id IN ({marks})""",
            latest,
        ).fetchall():
            summary = _clip(_one_line(minutes_outline(row["markdown"] or "")["summary"]), SUMMARY_CHARS)
            if summary:
                data.summaries[row["id"]] = summary
    return data


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def _day_number(day: str) -> int:
    return datetime.fromisoformat(day).toordinal()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------- 渲染


def _link(card: dict[str, Any]) -> str:
    title = _one_line(card["title"]) or PurePosixPath(card["file_name"]).stem
    return f"[{title}](<{card['file_name']}>)"


def _file_path(data: IndexData, root_id: int, rel_path: str) -> str:
    """卡片根目录里的写相对路径，项目别的根目录里的写绝对路径（都用反引号）。"""
    base = data.roots.get(int(root_id), "")
    if base == data.root:
        return f"`{rel_path}`"
    return f"`{base.rstrip('/')}/{rel_path}`"


def _folder_path(data: IndexData, folder: str) -> str:
    root = data.root.rstrip("/")
    if folder == root:
        return "`.`"
    if folder.startswith(root + "/"):
        return f"`{folder[len(root) + 1:]}`"
    return f"`{folder}`"


def _decision_line(item: dict[str, Any], cards: dict[str, dict[str, Any]]) -> str:
    """「2026-09-26 阈值先按 0.8 执行 · [初审规则沟通](<260926 初审规则沟通.md>) 00:12:34」，
    改了之前的决议、后来又提到的，行尾各加一段。"""
    line = f"{item['day']} {_one_line(item['text'])} · {_link(cards[item['meeting_id']])}"
    if item["start_ms"] is not None:
        line += f" {format_clock(item['start_ms'])}"
    for ref in item["earlier"]:
        line += CHANGED_SUFFIX.format(date=ref["date"], text=_one_line(ref["text"]))
    dates = sorted({ref["date"] for ref in item["restated"]})
    if dates:
        line += RESTATED_SUFFIX.format(dates="、".join(dates))
    return line


def render_sections(data: IndexData) -> list[str]:
    """按节渲染（每节一段文字，空节不出）。拼法：节之间空一行，结尾一个换行。"""
    cards = {card["meeting_id"]: card for card in data.cards}
    sections = [
        "\n".join(["---", f"project: {_yaml(data.project_name)}", f"generated_by: {GENERATED_BY}", "---"]),
        "\n".join([QUOTE_MAINTAINED, QUOTE_NO_MATERIAL]),
        f"# {_one_line(data.project_name)}{TITLE_SUFFIX}",
    ]
    # 进行中的行动项（v1 不变）
    lines = [TASKS_HEADING, ""]
    if data.tasks:
        for task in data.tasks:
            parts = [
                _one_line(task["title"]),
                ASSIGNEE_LABELS.get(task.get("assignee") or "", "我"),
                TASK_STATUS_LABELS.get(task["status"], task["status"]),
            ]
            source = cards.get(task.get("meeting_id") or "")
            if source:
                parts.append(f"来自 {_link(source)}")
            lines.append("- " + " · ".join(parts))
    else:
        lines.append(NO_TASKS)
    sections.append("\n".join(lines))
    # 需求
    by_requirement: dict[str, list[dict[str, Any]]] = {}
    for item in data.decisions:
        if item["requirement_id"]:
            by_requirement.setdefault(item["requirement_id"], []).append(item)
    deliverables_of: dict[str, list[dict[str, Any]]] = {}
    for item in data.deliverables:
        if item["requirement_id"]:
            deliverables_of.setdefault(item["requirement_id"], []).append(item)
    if data.requirements or data.closed:
        lines = [REQUIREMENTS_HEADING]
        for requirement in data.requirements:
            lines += ["", f"### {_one_line(requirement['title'])}"]
            lines.append(PRIORITY_LABEL.format(priority=requirement["priority"]))
            if requirement["folder"]:
                lines.append(FOLDER_LABEL.format(path=_folder_path(data, str(requirement["folder"]))))
            links = "、".join(_link(card) for card in requirement["meetings"])
            if requirement["elsewhere"]:
                links += (ELSEWHERE_NOTE if links else ELSEWHERE_ONLY).format(n=requirement["elsewhere"])
            if links:
                lines.append(MEETINGS_LABEL.format(links=links))
            decided = by_requirement.get(requirement["id"], [])
            if decided:
                lines.append(DECIDED_LABEL)
                lines += [f"  - {_decision_line(item, cards)}" for item in decided[:DECISIONS_PER_REQUIREMENT]]
                if len(decided) > DECISIONS_PER_REQUIREMENT:
                    lines.append(MORE_DECISIONS.format(n=len(decided) - DECISIONS_PER_REQUIREMENT))
            actions = [
                f"{_one_line(task['title'])}（{TASK_STATUS_LABELS.get(task['status'], task['status'])}）"
                for task in data.tasks
                if task.get("requirement_id") == requirement["id"]
            ]
            if actions:
                lines.append(ACTIONS_LABEL.format(items="、".join(actions)))
            produced = [
                f"{_file_path(data, item['root_id'], item['rel_path'])}"
                f"（{DELIVERABLE_OF.format(task=_one_line(item['task_title']))}）"
                for item in deliverables_of.get(requirement["id"], [])
            ]
            if produced:
                lines.append(PRODUCED_LABEL.format(items="、".join(produced)))
        if data.closed:
            lines += ["", CLOSED_HEADING]
            lines += [
                f"- {_one_line(item['title'])}（{REQUIREMENT_STATUS.get(item['status'], item['status'])}）"
                for item in data.closed
            ]
        sections.append("\n".join(lines))
    # 其他决议：没归到需求（包括你说不属于具体需求的）。归到已完成、搁置的需求的不再单列。
    other = [
        item
        for item in data.decisions
        if not item["requirement_id"] or item["requirement_id"] not in data.requirement_ids
    ]
    if other:
        lines = [OTHER_DECISIONS_HEADING, ""]
        lines += [f"- {_decision_line(item, cards)}" for item in other[:OTHER_DECISIONS]]
        sections.append("\n".join(lines))
    # 关键文件：先交付物，再被提到的，合计最多 15 行，只写路径
    key_lines: list[str] = []
    listed: set[int] = set()
    for item in data.deliverables:
        if len(key_lines) >= KEY_FILES:
            break
        if int(item["file_id"]) in listed:
            continue
        listed.add(int(item["file_id"]))
        key_lines.append(
            f"- {_file_path(data, item['root_id'], item['rel_path'])} · "
            + DELIVERABLE_OF.format(task=_one_line(item["task_title"]))
        )
    for item in data.mentioned:
        if len(key_lines) >= KEY_FILES:
            break
        if int(item["file_id"]) in listed:
            continue
        listed.add(int(item["file_id"]))
        key_lines.append(
            f"- {_file_path(data, item['root_id'], item['rel_path'])} · "
            + MENTIONED_IN.format(n=int(item["meetings"]))
        )
    if key_lines:
        sections.append("\n".join([KEY_FILES_HEADING, "", *key_lines]))
    # 会议（按时间倒序），最新 10 场带摘要
    lines = [MEETINGS_HEADING, ""]
    if data.cards:
        for card in data.cards:
            extra = AI_ATTRIBUTED if card.get("project_origin") == "ai" else ""
            edited = CARD_EDITED if card["state"] == USER_EDITED else ""
            lines.append(f"- {card['start']:%Y-%m-%d %H:%M} · {_link(card)}{extra}{edited}")
            summary = data.summaries.get(card["meeting_id"])
            if summary:
                lines.append(SUMMARY_LINE.format(text=summary))
    else:
        lines.append(NO_MEETINGS)
    sections.append("\n".join(lines))
    return sections


def render(data: IndexData) -> str:
    return "\n\n".join(render_sections(data)) + "\n"
