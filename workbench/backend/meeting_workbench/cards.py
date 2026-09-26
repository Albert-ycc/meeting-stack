"""会议卡片（第一期 1c）：把声档里的当前纪要写成项目文件夹「声档会议记录/」里的一张 Markdown。

卡片是给你和 Claude Code 看的自动副本，原件始终在声档里：
- 写在项目最早挂上的材料根目录下，文件名「YYMMDD 主题.md」，旁边是「逐字稿/」副本和「00 索引.md」；
- 靠 frontmatter 里的 meeting_id 认卡片，不靠文件名：在 Finder 里改名、换根目录都认得回来；
- 「## 我的笔记」这一行以下永远归你；判断纪要部分改没改过，先去掉空白和标点再比指纹，
  编辑器重排格式不算改过；
- 只动自己写的文件，撤下一律移进回收区（数据目录下的 card-retired/），盘不在绝不造目录。

扫描线程（reconcile）和接口线程（改归属后立刻 sync_meeting）会并发，所有卡片的磁盘动作
共用一把进程内锁，拿到锁后重新读库再决定。
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, utc_now
from .importer import ArchiveImporter
from .materials import (
    CARDS_DIR_NAME,
    ROOT_ONLINE,
    ROOT_VOLUME_OFFLINE,
    _protected_roots,
    _within,
    resolve_within,
    volume_state,
)
from .rendering import format_clock, render_transcript_timestamped

logger = logging.getLogger(__name__)

TRANSCRIPTS_DIR_NAME = "逐字稿"
INDEX_NAME = "00 索引.md"
NOTES_HEADING = "## 我的笔记（这一行以下不会被自动覆盖）"
_NOTES_PREFIX = "## 我的笔记"
GENERATED_BY = "shengdang"
RETIRED_DIR_NAME = "card-retired"
# 记住最近几次写入的指纹：文件的纪要部分对得上其中一个，就还是声档写的那份。
KEEP_FPS = 5
BACKFILL_SNOOZE_DAYS = 3

# 卡片行的状态
PENDING = "pending"
SYNCED = "synced"
USER_EDITED = "user_edited"
MISSING = "missing"
RETIRED = "retired"
BLOCKED = "blocked"
_LOCATED_STATES = (SYNCED, USER_EDITED)

# 写不了、还没写的原因
WAITING_MINUTES = "waiting_minutes"
WAITING_PROJECT = "waiting_project"
NEEDS_REVIEW = "needs_review"
NOT_BACKFILLED = "not_backfilled"
NO_ROOT = "no_root"
ROOT_OFFLINE = "root_offline"
ROOT_MISSING = "root_missing"
ROOT_REFUSED = "root_in_archive"
PAUSED = "paused"
DISABLED = "disabled"
QUEUED = "queued"
# 卡片行的 state：这些原因记成 blocked（根目录或开关的问题），其余记成 pending（在等纪要、项目）。
BLOCKED_REASONS = {NO_ROOT, ROOT_OFFLINE, ROOT_MISSING, ROOT_REFUSED, PAUSED, DISABLED}
# 界面分三类（好了 / 在等什么 / 为什么停了）：这些原因要你处理才会动，算「停了」，其余算「在等」。
STOP_REASONS = {ROOT_MISSING, ROOT_REFUSED, PAUSED, DISABLED}
# 这些原因不靠库里的变化解除（插上盘、找回文件夹），行保持 dirty，每轮重试。
_RETRY_REASONS = {ROOT_OFFLINE, ROOT_MISSING}

# app_state 键
ENABLED_KEY = "cards_enabled"
SINCE_KEY = "cards_since"
BACKFILL_KEY = "cards_backfill_answer"
BACKFILL_SNOOZE_KEY = "cards_backfill_snoozed_until"
PAUSED_KEY = "cards_paused_projects"
NOTICES_KEY = "cards_notices"
RETIRE_QUEUE_KEY = "cards_retire_queue"

TASK_STATUS_LABELS = {
    "pending_confirm": "待确认",
    "confirmed": "已确认",
    "in_progress": "进行中",
    "done": "已完成",
}
ASSIGNEE_LABELS = {"me": "我", "ai": "AI"}

# 所有卡片磁盘动作共用的锁（跨 CardWriter 实例，整个进程一把）。
_LOCK = threading.RLock()

_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_FRONTMATTER_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")
_YAML_PLAIN = re.compile(r"^[^\s\-?:,\[\]{}#&*!|>'\"%@`][^:#\n]*$")
_YAML_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~"}


class CardsError(ValueError):
    """接口层把它转成 4xx，文案直接给用户看。"""


class CardWriteError(RuntimeError):
    """磁盘上写不了（目录被换成了文件、符号链接等），记在卡片行的 error 里，下一轮重试。"""


# ---------------------------------------------------------------------- 状态


def read_state(connection: Any, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def write_state(connection: Any, key: str, value: str) -> None:
    connection.execute(
        """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, utc_now()),
    )


def _json_state(connection: Any, key: str) -> list[Any]:
    try:
        value = json.loads(read_state(connection, key) or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def paused_projects(connection: Any) -> set[str]:
    return {str(item) for item in _json_state(connection, PAUSED_KEY)}


def cards_enabled(connection: Any) -> bool:
    return read_state(connection, ENABLED_KEY) != "0"


# ---------------------------------------------------------------------- 内容与指纹


def _squash(text: str) -> str:
    """NFC 后去掉空白、标点、符号和控制字符：换列表符号、改 YAML 写法、重排空行都不影响指纹。"""
    text = unicodedata.normalize("NFC", text)
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PSZC")


def split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return lines[1:index], "\n".join(lines[index + 1 :])
    return None, text


def content_fp(text: str) -> str:
    """卡片纪要部分的指纹：frontmatter 按顶层键切块、各自压扁后排序，再拼上压扁的正文。

    编辑器把属性换了顺序、把 [张三, 李四] 改写成块列表、换了列表符号，指纹都不变；
    改了一个字就变。
    """
    front, body = split_frontmatter(text)
    blocks: list[str] = []
    if front is not None:
        current: list[str] = []
        for line in front:
            if _FRONTMATTER_KEY.match(line) and current:
                blocks.append("\n".join(current))
                current = []
            current.append(line)
        if current:
            blocks.append("\n".join(current))
    squashed = sorted(value for value in (_squash(block) for block in blocks) if value)
    payload = "\x00".join(squashed) + "\x01" + _squash(body)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_card(text: str) -> tuple[str, str | None]:
    """切成（纪要部分，笔记部分）。纪要部分含「## 我的笔记」这一行；找不到这一行时笔记为 None。"""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.lstrip().startswith(_NOTES_PREFIX):
            return "\n".join(lines[: index + 1]), "\n".join(lines[index + 1 :])
    return text, None


def _compose(auto: str, notes: str | None) -> str:
    return f"{auto}\n{notes or ''}"


def _yaml(value: Any, *, flow: bool = False) -> str:
    text = str(value)
    plain = (
        bool(text)
        and _YAML_PLAIN.match(text) is not None
        and not text.endswith(" ")
        and text.lower() not in _YAML_RESERVED
        and not re.fullmatch(r"[-+.]?\d[\d_.:eE+-]*", text)
        and not (flow and any(ch in text for ch in ",[]{}"))
    )
    return text if plain else json.dumps(text, ensure_ascii=False)


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


@dataclass
class CardView:
    """渲染一张卡片需要的全部数据（从库里读好，渲染本身不碰库）。"""

    meeting_id: str
    title: str
    start: datetime
    duration_ms: int | None
    project_name: str
    project_origin: str | None
    participants: list[str]
    requirements: list[dict[str, str | None]]
    transcript_rel: str | None
    workbench_url: str
    minutes_markdown: str
    tasks: list[dict[str, Any]]


def _minutes_body(markdown: str) -> str:
    """纪要去掉第一行大标题（卡片自己有标题），防止正文里也有「## 我的笔记」抢走分界线。"""
    body = re.sub(r"\A\s*#\s+[^\n]*\n+", "", markdown.strip() + "\n").strip()
    return re.sub(r"(?m)^(\s*)## 我的笔记", r"\1## 纪要里的笔记", body)


def render_card(view: CardView) -> str:
    """卡片的纪要部分（以「## 我的笔记」这一行结尾），格式见方案 v2 第四节。"""
    front = [
        "---",
        f"meeting_id: {_yaml(view.meeting_id)}",
        f"title: {_yaml(_one_line(view.title))}",
        f"date: {view.start:%Y-%m-%d}",
        f'start: "{view.start:%H:%M}"',
    ]
    if view.duration_ms:
        front.append(f"duration_min: {max(1, round(view.duration_ms / 60_000))}")
    front.append(f"project: {_yaml(_one_line(view.project_name))}")
    front.append(f"project_source: {'ai' if view.project_origin == 'ai' else 'manual'}")
    people = ", ".join(_yaml(name, flow=True) for name in view.participants)
    front.append(f"participants: [{people}]")
    if view.requirements:
        front.append("requirements:")
        for requirement in view.requirements:
            front.append(f"  - title: {_yaml(_one_line(requirement['title']))}")
            if requirement.get("folder"):
                front.append(f"    folder: {_yaml(requirement['folder'])}")
    if view.transcript_rel:
        front.append(f"transcript: {_yaml(view.transcript_rel)}")
    front.append(f"workbench_url: {_yaml(view.workbench_url)}")
    front.append(f"generated_by: {GENERATED_BY}")
    front.append("---")

    source = (
        "AI 自动判断（未人工确认）" if view.project_origin == "ai" else "人工确认"
    )
    notes = [
        "> 本卡由声档自动生成并持续更新。要改纪要请到声档改；补充想法写在最下面「我的笔记」里。"
        "AI 助手请勿改动、移动或重命名本文件。",
        f"> 项目归属：{source}。",
    ]
    if view.transcript_rel:
        notes.append(f"> [HH:MM:SS] 是录音时间，原话在 {view.transcript_rel} 里。")

    groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("已确认 / 进行中", [t for t in view.tasks if t["status"] in ("confirmed", "in_progress")]),
        ("待你确认（AI 提取）", [t for t in view.tasks if t["status"] == "pending_confirm"]),
        ("已完成", [t for t in view.tasks if t["status"] == "done"]),
    ]
    actions: list[str] = []
    for heading, items in groups:
        if not items:
            continue
        actions.append(f"### {heading}")
        for task in items:
            parts = [
                _one_line(task["title"]),
                ASSIGNEE_LABELS.get(task.get("assignee") or "", "我"),
                TASK_STATUS_LABELS.get(task["status"], task["status"]),
            ]
            if task.get("anchor_ms") is not None:
                parts.append(f"[{format_clock(task['anchor_ms'])}]")
            actions.append("- " + " · ".join(parts))
    if not actions:
        actions.append("这场会没有行动项。")

    body = _minutes_body(view.minutes_markdown)
    sections = [
        "\n".join(front),
        "\n".join(notes),
        f"# {_one_line(view.title)}",
        body,
        "## 行动项（声档实时状态）\n" + "\n".join(actions),
        NOTES_HEADING,
    ]
    return "\n\n".join(section for section in sections if section)


# ---------------------------------------------------------------------- 文件名


def _local_start(meeting: dict[str, Any]) -> datetime:
    for key in ("recording_date", "created_at"):
        raw = meeting.get(key)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC) if key == "created_at" else value.astimezone()
        return value.astimezone()
    return datetime.now().astimezone()


def _topic(title: str) -> str:
    text = _ILLEGAL_NAME_CHARS.sub("-", title or "")
    text = re.sub(r"^(?:20)?\d{6}\s*", "", text.strip())
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text[:60].rstrip(" .-")


def stem_candidates(meeting: dict[str, Any]) -> list[str]:
    """文件名候选（不含 .md），按顺序取第一个没被占用的：日期和归档文件夹同一条规则，
    同一天重名时追加开始时间，标题还没生成时先叫「YYMMDD HHMM 会议」。"""
    start = _local_start(meeting)
    date_code = f"{start:%y%m%d}"
    clock = f"{start:%H%M}"
    topic = _topic(str(meeting.get("title") or ""))
    untitled = not topic or ArchiveImporter.is_untitled(str(meeting.get("title") or ""), meeting["id"])
    base = f"{date_code} {clock} 会议" if untitled else f"{date_code} {topic}"
    candidates = [base] if untitled else [base, f"{base} {clock}"]
    last = candidates[-1]
    candidates.extend(f"{last}-{index}" for index in range(2, 50))
    return candidates


def _pick_stem(candidates: list[str], taken: set[str]) -> str:
    for stem in candidates:
        if f"{stem}.md".casefold() not in taken:
            return stem
    return candidates[-1]


def _read_meeting_id(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(4096).decode("utf-8", errors="ignore")
    except OSError:
        return None
    front, _body = split_frontmatter(head)
    if front is None:
        # frontmatter 超过 4KB 时读不到结尾的 ---，照样逐行找
        if not head.startswith("---"):
            return None
        front = head.split("\n")[1:]
    for line in front:
        match = re.match(r"^meeting_id\s*:\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip("\"'")
    return None


# ---------------------------------------------------------------------- 一轮的缓存


@dataclass
class _Round:
    """一轮 reconcile（或一次 sync_meeting）里共用的缓存：根目录状态、卡片目录里的认领表。"""

    root_states: dict[str, str] = field(default_factory=dict)
    dir_index: dict[str, dict[str, str]] = field(default_factory=dict)
    touched: set[tuple[str, str]] = field(default_factory=set)
    writes: int = 0

    def index(self, cards_dir: Path) -> dict[str, str]:
        """卡片目录里 meeting_id → 文件名。只读每个 .md 开头 4KB。"""
        key = str(cards_dir)
        if key not in self.dir_index:
            found: dict[str, str] = {}
            try:
                entries = list(os.scandir(cards_dir))
            except OSError:
                entries = []
            for entry in sorted(entries, key=lambda item: item.name):
                name = entry.name
                if (
                    name.startswith(".")
                    or not name.endswith(".md")
                    or name == INDEX_NAME
                    or not entry.is_file(follow_symlinks=False)
                ):
                    continue
                meeting_id = _read_meeting_id(Path(entry.path))
                if meeting_id and meeting_id not in found:
                    found[meeting_id] = name
            self.dir_index[key] = found
        return self.dir_index[key]

    def names(self, cards_dir: Path) -> set[str]:
        try:
            return {entry.name.casefold() for entry in os.scandir(cards_dir) if entry.name.endswith(".md")}
        except OSError:
            return set()

    def forget(self, cards_dir: Path) -> None:
        self.dir_index.pop(str(cards_dir), None)


@dataclass
class _Card:
    meeting_id: str
    exists: bool = False
    project_id: str | None = None
    root_path: str | None = None
    rel_path: str | None = None
    transcript_rel_path: str | None = None
    state: str = PENDING
    reason: str | None = None
    written_fps: list[str] = field(default_factory=list)
    transcript_fp: str | None = None
    retired_path: str | None = None
    retired_edited: int = 0
    carry_notes: str | None = None
    user_named: int = 0
    dirty: int = 1
    synced_at: str | None = None
    error: str | None = None
    # 本次要整份搬走的旧卡片（你改过的，换项目时原样搬过去）
    pending_move: Path | None = None

    @classmethod
    def from_row(cls, meeting_id: str, row: dict[str, Any] | None) -> _Card:
        if row is None:
            return cls(meeting_id=meeting_id)
        try:
            fps = json.loads(row["written_fps"] or "[]")
        except json.JSONDecodeError:
            fps = []
        return cls(
            meeting_id=meeting_id,
            exists=True,
            project_id=row["project_id"],
            root_path=row["root_path"],
            rel_path=row["rel_path"],
            transcript_rel_path=row["transcript_rel_path"],
            state=row["state"],
            reason=row["reason"],
            written_fps=[str(value) for value in fps if isinstance(value, str)],
            transcript_fp=row["transcript_fp"],
            retired_path=row["retired_path"],
            retired_edited=int(row["retired_edited"] or 0),
            carry_notes=row["carry_notes"],
            user_named=int(row["user_named"] or 0),
            dirty=int(row["dirty"] or 0),
            synced_at=row["synced_at"],
            error=row["error"],
        )

    @property
    def located(self) -> bool:
        return bool(self.root_path and self.rel_path and self.state in _LOCATED_STATES)

    def path(self) -> Path | None:
        if not self.root_path or not self.rel_path:
            return None
        return Path(self.root_path) / self.rel_path

    def clear_location(self) -> None:
        self.root_path = None
        self.rel_path = None
        self.transcript_rel_path = None
        self.transcript_fp = None
        self.user_named = 0


@dataclass
class _Snapshot:
    meeting: dict[str, Any] | None
    card: _Card
    enabled: bool
    eligible: bool
    paused: set[str]
    root: str | None
    link_status: str | None
    minutes: dict[str, Any] | None


# ---------------------------------------------------------------------- 写卡片


class CardWriter:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        # 00 索引.md 最近一次写入的内容指纹，避免每轮都读文件比对
        self._index_fps: dict[str, str] = {}

    # ------------------------------------------------------------ 入口

    def reconcile(self, max_writes: int = 50, budget_s: float = 10.0) -> dict[str, Any]:
        """扫描线程每轮调用：处理删掉的会、脏卡片、还没卡片的新会；每轮最多写 max_writes 张、
        最多花 budget_s 秒，剩下的留给下一轮。"""
        started = time.monotonic()
        round_ = _Round()
        stats = {"processed": 0, "written": 0, "left": 0}
        with _LOCK:
            self._drain_retire_queue(round_)
        with self.db.autocommit() as connection:
            enabled = cards_enabled(connection)
            since = read_state(connection, SINCE_KEY) or ""
            backfill_yes = read_state(connection, BACKFILL_KEY) == "yes"
            orphans = [
                row["meeting_id"]
                for row in connection.execute(
                    """SELECT c.meeting_id FROM meeting_cards c
                         LEFT JOIN meetings m ON m.id = c.meeting_id
                        WHERE m.id IS NULL"""
                ).fetchall()
            ]
            dirty = [
                row["meeting_id"]
                for row in connection.execute(
                    "SELECT meeting_id FROM meeting_cards WHERE dirty > 0 ORDER BY updated_at, meeting_id"
                ).fetchall()
            ]
            fresh: list[str] = []
            if enabled:
                fresh = [
                    row["id"]
                    for row in connection.execute(
                        """SELECT m.id FROM meetings m
                             JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                             LEFT JOIN meeting_cards c ON c.meeting_id = m.id
                            WHERE c.meeting_id IS NULL AND m.project_id IS NOT NULL
                              AND (? OR mv.created_at >= ?)
                            ORDER BY COALESCE(m.recording_date, m.created_at), m.id""",
                        (1 if backfill_yes else 0, since),
                    ).fetchall()
                ]
        queue = list(dict.fromkeys(orphans + dirty + fresh))
        for index, meeting_id in enumerate(queue):
            if round_.writes >= max_writes or time.monotonic() - started > budget_s:
                stats["left"] = len(queue) - index
                break
            before = round_.writes
            with _LOCK:
                self._sync_locked(meeting_id, None, round_)
            stats["processed"] += 1
            stats["written"] += round_.writes - before
        with _LOCK:
            self._refresh_indexes(round_, every_project=True)
        return stats

    def reconcile_project(
        self, project_id: str, max_writes: int = 50, budget_s: float = 5.0
    ) -> int:
        """挂上或换了文件夹、恢复写入之后，当场把这个项目积压的卡片补写掉；返回写了几张。"""
        started = time.monotonic()
        round_ = _Round()
        with self.db.autocommit() as connection:
            if not cards_enabled(connection):
                return 0
            since = read_state(connection, SINCE_KEY) or ""
            backfill_yes = read_state(connection, BACKFILL_KEY) == "yes"
            queue = [
                row["id"]
                for row in connection.execute(
                    """SELECT m.id FROM meetings m
                         JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                         LEFT JOIN meeting_cards c ON c.meeting_id = m.id
                        WHERE m.project_id=?
                          AND (c.meeting_id IS NULL OR c.dirty > 0 OR c.state IN ('pending', 'blocked'))
                          AND (? OR mv.created_at >= ? OR c.synced_at IS NOT NULL)
                        ORDER BY COALESCE(m.recording_date, m.created_at), m.id""",
                    (project_id, 1 if backfill_yes else 0, since),
                ).fetchall()
            ]
        for meeting_id in queue:
            if round_.writes >= max_writes or time.monotonic() - started > budget_s:
                break
            with _LOCK:
                self._sync_locked(meeting_id, None, round_)
        with _LOCK:
            self._refresh_indexes(round_)
        return round_.writes

    def sync_meeting(self, meeting_id: str, *, mode: str | None = None) -> dict[str, Any]:
        """改归属、确认、撤销之后立刻同步这一场会的卡片，返回给提示用的去向。"""
        round_ = _Round()
        with _LOCK:
            result = self._sync_locked(meeting_id, mode, round_)
            self._refresh_indexes(round_)
        return result

    # ------------------------------------------------------------ 读库

    def _snapshot(self, connection: Any, meeting_id: str) -> _Snapshot:
        meeting = connection.execute(
            """SELECT m.*, p.name AS project_name FROM meetings m
                 LEFT JOIN projects p ON p.id = m.project_id WHERE m.id=?""",
            (meeting_id,),
        ).fetchone()
        meeting = dict(meeting) if meeting else None
        row = connection.execute(
            "SELECT * FROM meeting_cards WHERE meeting_id=?", (meeting_id,)
        ).fetchone()
        card = _Card.from_row(meeting_id, dict(row) if row else None)
        minutes = None
        root = None
        link_status = None
        eligible = False
        if meeting:
            if meeting["current_minutes_version_id"]:
                found = connection.execute(
                    "SELECT id, markdown, created_at FROM minutes_versions WHERE id=?",
                    (meeting["current_minutes_version_id"],),
                ).fetchone()
                minutes = dict(found) if found else None
            if meeting["project_id"]:
                found = connection.execute(
                    """SELECT path FROM project_material_roots WHERE project_id=?
                        ORDER BY created_at, id LIMIT 1""",
                    (meeting["project_id"],),
                ).fetchone()
                root = found["path"] if found else None
            found = connection.execute(
                "SELECT status FROM project_links WHERE meeting_id=? ORDER BY id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
            link_status = found["status"] if found else None
            since = read_state(connection, SINCE_KEY) or ""
            eligible = (
                read_state(connection, BACKFILL_KEY) == "yes"
                or (minutes is not None and str(minutes["created_at"]) >= since)
                or card.synced_at is not None
            )
        return _Snapshot(
            meeting=meeting,
            card=card,
            enabled=cards_enabled(connection),
            eligible=eligible,
            paused=paused_projects(connection),
            root=root,
            link_status=link_status,
            minutes=minutes,
        )

    def _view(self, snapshot: _Snapshot, root: Path, transcript_rel: str | None) -> CardView:
        meeting = snapshot.meeting
        assert meeting is not None and snapshot.minutes is not None
        with self.db.autocommit() as connection:
            tasks = [
                dict(row)
                for row in connection.execute(
                    """SELECT title, status, assignee, anchor_ms FROM tasks
                        WHERE meeting_id=? AND status IN ('pending_confirm','confirmed','in_progress','done')
                        ORDER BY COALESCE(anchor_ms, 9e18), created_at, id""",
                    (meeting["id"],),
                ).fetchall()
            ]
            participants = [
                row["display_name"].strip()
                for row in connection.execute(
                    """SELECT display_name FROM speakers
                        WHERE meeting_id=? AND TRIM(COALESCE(display_name, '')) != ''
                        ORDER BY label""",
                    (meeting["id"],),
                ).fetchall()
            ]
            requirements = [
                dict(row)
                for row in connection.execute(
                    """SELECT r.title,
                              (SELECT f.path FROM requirement_folders f WHERE f.requirement_id = r.id
                                ORDER BY f.created_at, f.id LIMIT 1) AS folder
                         FROM requirement_meetings rm JOIN requirements r ON r.id = rm.requirement_id
                        WHERE rm.meeting_id=? ORDER BY r.created_at, r.id""",
                    (meeting["id"],),
                ).fetchall()
            ]
        for requirement in requirements:
            folder = requirement.get("folder")
            if folder and _within(Path(folder), root):
                requirement["folder"] = os.path.relpath(folder, root)
        host = self.settings.host if self.settings.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
        return CardView(
            meeting_id=meeting["id"],
            title=str(meeting.get("title") or meeting["id"]),
            start=_local_start(meeting),
            duration_ms=meeting.get("duration_ms"),
            project_name=str(meeting.get("project_name") or ""),
            project_origin=meeting.get("project_origin"),
            participants=list(dict.fromkeys(participants)),
            requirements=requirements,
            transcript_rel=transcript_rel,
            workbench_url=f"http://{host}:{self.settings.port}/#meetings/{meeting['id']}",
            minutes_markdown=str(snapshot.minutes["markdown"] or ""),
            tasks=tasks,
        )

    def _transcript_text(self, meeting: dict[str, Any]) -> str:
        version_id = meeting.get("current_transcript_version_id")
        if not version_id:
            return ""
        with self.db.autocommit() as connection:
            segments = [
                dict(row)
                for row in connection.execute(
                    """SELECT start_ms, speaker_label, speaker_name, text FROM segments
                        WHERE version_id=? ORDER BY ordinal""",
                    (version_id,),
                ).fetchall()
            ]
            names = {
                row["label"]: row["display_name"].strip()
                for row in connection.execute(
                    """SELECT label, display_name FROM speakers
                        WHERE meeting_id=? AND TRIM(COALESCE(display_name, '')) != ''""",
                    (meeting["id"],),
                ).fetchall()
            }
        return render_transcript_timestamped(segments, names)

    # ------------------------------------------------------------ 根目录

    def _root_state(self, root: str, round_: _Round) -> str:
        """材料根目录能不能写：online / root_offline / root_missing / root_in_archive。"""
        if root in round_.root_states:
            return round_.root_states[root]
        state = ROOT_ONLINE
        try:
            resolved = resolve_within(self.settings.material_browse_root, root)
        except ValueError:
            state = ROOT_REFUSED
        else:
            volume = volume_state(resolved)
            if volume == ROOT_VOLUME_OFFLINE:
                state = ROOT_OFFLINE
            elif volume != ROOT_ONLINE:
                state = ROOT_MISSING
            elif Path(root).is_symlink() or any(
                _within(resolved, protected) for protected, _message in _protected_roots(self.settings)
            ):
                state = ROOT_REFUSED
        round_.root_states[root] = state
        return state

    def _target(self, snapshot: _Snapshot, round_: _Round) -> tuple[str | None, str | None]:
        """这场会的卡片该写在哪个根目录；写不了时返回原因。"""
        meeting = snapshot.meeting
        if meeting is None:
            return None, None
        if not snapshot.enabled:
            return None, DISABLED
        if snapshot.minutes is None:
            return None, WAITING_MINUTES
        if not meeting["project_id"]:
            return None, NEEDS_REVIEW if snapshot.link_status == "needs_review" else WAITING_PROJECT
        if not snapshot.eligible:
            return None, NOT_BACKFILLED
        if meeting["project_id"] in snapshot.paused:
            return None, PAUSED
        if not snapshot.root:
            return None, NO_ROOT
        state = self._root_state(snapshot.root, round_)
        if state != ROOT_ONLINE:
            return None, state
        return snapshot.root, None

    def _label(self, root: str | None, rel_path: str | None) -> str | None:
        """提示里给人看的位置：「项目文件夹名/声档会议记录/文件名」。"""
        if not root or not rel_path:
            return None
        return f"{Path(root).name}/{rel_path}"

    # ------------------------------------------------------------ 同步一场会

    def _sync_locked(self, meeting_id: str, mode: str | None, round_: _Round) -> dict[str, Any]:
        with self.db.autocommit() as connection:
            snapshot = self._snapshot(connection, meeting_id)
        card = snapshot.card
        dirty_seen: int | None = card.dirty if card.exists else 0
        action: dict[str, Any] = {"action": "none", "from": None, "to": None, "reason": None}
        try:
            self._apply(snapshot, card, mode, round_, action)
            card.error = None
        except (OSError, CardWriteError) as error:
            logger.warning("会议卡片写入失败 %s：%s", meeting_id, error)
            card.error = str(error)[:500]
            dirty_seen = None
        if card.reason in _RETRY_REASONS:
            dirty_seen = None
        self._store(snapshot, card, dirty_seen)
        action["state"] = card.state
        if action["reason"] is None:
            action["reason"] = card.reason
        return action

    def _apply(
        self,
        snapshot: _Snapshot,
        card: _Card,
        mode: str | None,
        round_: _Round,
        action: dict[str, Any],
    ) -> None:
        meeting = snapshot.meeting
        if meeting is None:
            if card.located:
                self._vacate(card, None, round_, action)
            return
        project_id = meeting["project_id"]
        target, reason = self._target(snapshot, round_)
        card.reason = None

        if mode == "regenerate" and card.state == MISSING:
            card.state = PENDING
            card.clear_location()

        if card.located and card.project_id == project_id:
            if target is not None and target == card.root_path:
                self._update_in_place(snapshot, card, mode, round_, action)
                return
            if target is not None:
                # 根目录换了（Finder 里改名后重新选了）：先在新根目录按 meeting_id 认领，认到了
                # 只更新路径，不重写、不出两份。
                cards_dir = Path(target) / CARDS_DIR_NAME
                claimed = round_.index(cards_dir).get(card.meeting_id) if cards_dir.is_dir() else None
                if claimed:
                    card.root_path = target
                    card.rel_path = f"{CARDS_DIR_NAME}/{claimed}"
                    card.user_named = 1
                    tx_name = Path(card.transcript_rel_path).name if card.transcript_rel_path else None
                    tx_rel = f"{CARDS_DIR_NAME}/{TRANSCRIPTS_DIR_NAME}/{tx_name}" if tx_name else None
                    card.transcript_rel_path = (
                        tx_rel if tx_rel and (Path(target) / tx_rel).is_file() else None
                    )
                    self._update_in_place(snapshot, card, mode, round_, action)
                    return
            elif reason in _RETRY_REASONS or reason == ROOT_REFUSED:
                # 同一个项目，只是盘不在或文件夹找不到：卡片留在原处，能写了再更新。
                card.reason = reason
                return

        if card.located:
            self._vacate(card, target, round_, action)
            if target is None:
                card.state = RETIRED
        if card.state == MISSING and card.project_id == project_id:
            # 你删掉的卡片在这个项目里不再生成；改到别的项目后照常写。
            return
        if target is None:
            card.project_id = project_id
            if card.state != RETIRED or reason in BLOCKED_REASONS:
                card.state = BLOCKED if reason in BLOCKED_REASONS else PENDING
            card.reason = reason
            if action["action"] == "none":
                action.update(action="waiting", reason=reason)
            return
        self._write_fresh(snapshot, card, target, round_, action)

    def _update_in_place(
        self,
        snapshot: _Snapshot,
        card: _Card,
        mode: str | None,
        round_: _Round,
        action: dict[str, Any],
        *,
        adopted: bool = False,
    ) -> None:
        assert card.root_path and card.rel_path and snapshot.meeting is not None
        root = Path(card.root_path)
        cards_dir = root / CARDS_DIR_NAME
        project_id = snapshot.meeting["project_id"]
        if not cards_dir.is_dir():
            # 你删了整个「声档会议记录/」：这个项目停止写卡片，不重建。
            self._pause(project_id)
            card.clear_location()
            card.state = BLOCKED
            card.reason = PAUSED
            action.update(action="waiting", reason=PAUSED)
            return
        path = root / card.rel_path
        if not path.is_file():
            claimed = round_.index(cards_dir).get(card.meeting_id)
            if not claimed:
                # 卡片被移走或删了：只对当前项目有效，不复活。
                card.clear_location()
                card.project_id = project_id
                card.state = MISSING
                return
            card.rel_path = f"{CARDS_DIR_NAME}/{claimed}"
            card.user_named = 1
            path = cards_dir / claimed
        self._assert_card_dir(root, cards_dir)
        text = path.read_text(encoding="utf-8", errors="replace")
        auto, notes = split_card(text)
        current_fp = content_fp(auto)

        if card.user_named:
            stem = path.stem
        else:
            taken = round_.names(cards_dir) - {path.name.casefold()}
            stem = _pick_stem(stem_candidates(snapshot.meeting), taken)
        tx_stem = (
            Path(card.transcript_rel_path).stem
            if card.user_named and card.transcript_rel_path
            else stem
        )
        transcript = self._transcript_text(snapshot.meeting)
        tx_rel = f"{TRANSCRIPTS_DIR_NAME}/{tx_stem}.txt" if transcript else None
        new_auto = render_card(self._view(snapshot, root, tx_rel))
        new_fp = content_fp(new_auto)

        edited = card.state == USER_EDITED or current_fp not in card.written_fps
        if edited and adopted and current_fp == new_fp:
            edited = False
        if edited:
            if mode != "rewrite":
                card.project_id = project_id
                card.state = USER_EDITED
                return
            # 用最新纪要重写：你的版本先进回收区，笔记照样带过去。
            self._retire_file(path, card.meeting_id)
            current_fp = ""
            round_.forget(cards_dir)

        new_name = f"{stem}.md"
        new_path = cards_dir / new_name
        if new_fp != current_fp or new_name != path.name or not path.exists():
            self._atomic_write(root, cards_dir, new_name, _compose(new_auto, notes))
            if path.name != new_name and path.exists() and not _same_file(path, new_path):
                path.unlink()
            card.written_fps = (card.written_fps + [new_fp])[-KEEP_FPS:]
            round_.writes += 1
            round_.forget(cards_dir)
            action.update(action="updated", to=self._label(card.root_path, f"{CARDS_DIR_NAME}/{new_name}"))
            card.synced_at = utc_now()
        card.rel_path = f"{CARDS_DIR_NAME}/{new_name}"
        card.project_id = project_id
        card.state = SYNCED
        card.synced_at = card.synced_at or utc_now()
        self._sync_transcript(card, root, cards_dir, tx_stem, transcript, round_)
        round_.touched.add((project_id, card.root_path))

    def _vacate(
        self, card: _Card, target: str | None, round_: _Round, action: dict[str, Any]
    ) -> None:
        """卡片要离开原来的位置（换了项目、不该再有卡片、会议删了）。

        没改过的：新位置能写就把笔记带过去、旧文件进回收区；你改过的：新位置能写就整份原样
        搬过去，写不了就先进回收区，以后能写时再从回收区搬回来。旧盘不在时记进待撤下队列。
        """
        old_path = card.path()
        assert old_path is not None and card.root_path is not None
        old_root = card.root_path
        action.update(action="retired", **{"from": self._label(old_root, card.rel_path)})
        round_.touched.add((card.project_id or "", old_root))
        state = self._root_state(old_root, round_)
        tx_path = Path(old_root) / card.transcript_rel_path if card.transcript_rel_path else None
        if state == ROOT_ONLINE:
            cards_dir = Path(old_root) / CARDS_DIR_NAME
            if not old_path.is_file():
                claimed = round_.index(cards_dir).get(card.meeting_id) if cards_dir.is_dir() else None
                old_path = cards_dir / claimed if claimed else None
            if old_path is not None:
                text = old_path.read_text(encoding="utf-8", errors="replace")
                auto, notes = split_card(text)
                edited = card.state == USER_EDITED or content_fp(auto) not in card.written_fps
                if target is not None and edited:
                    card.pending_move = old_path
                else:
                    retired = self._retire_file(old_path, card.meeting_id)
                    card.retired_path = str(retired)
                    card.retired_edited = 1 if edited else 0
                    card.carry_notes = None if edited else notes
                round_.forget(cards_dir)
            if tx_path is not None and tx_path.is_file():
                self._retire_file(tx_path, card.meeting_id)
        elif state == ROOT_OFFLINE:
            queued = [{"path": str(old_path), "meeting_id": card.meeting_id, "kind": "card"}]
            if tx_path is not None:
                queued.append({"path": str(tx_path), "meeting_id": card.meeting_id, "kind": "transcript"})
            self._queue_retire(queued)
        card.clear_location()

    def _write_fresh(
        self,
        snapshot: _Snapshot,
        card: _Card,
        target: str,
        round_: _Round,
        action: dict[str, Any],
    ) -> None:
        assert snapshot.meeting is not None
        project_id = snapshot.meeting["project_id"]
        root = Path(target)
        cards_dir = root / CARDS_DIR_NAME
        if not cards_dir.exists():
            if self._project_had_cards(project_id, target, card.meeting_id):
                # 这个根目录下写过卡片、整个文件夹却没了：是你删的，停止写卡片，不重建。
                self._pause(project_id)
                card.project_id = project_id
                card.state = BLOCKED
                card.reason = PAUSED
                action.update(action="waiting", reason=PAUSED)
                return
            self._make_cards_dir(root, cards_dir, snapshot.meeting)
        self._assert_card_dir(root, cards_dir)

        claimed = round_.index(cards_dir).get(card.meeting_id)
        if claimed and card.pending_move is None:
            # 目录里已经有这场会的卡片（库恢复过、或别处搬来的）：认领，不另写一份。
            card.root_path = target
            card.rel_path = f"{CARDS_DIR_NAME}/{claimed}"
            card.user_named = 1
            card.state = SYNCED
            self._update_in_place(snapshot, card, None, round_, action, adopted=True)
            return

        taken = round_.names(cards_dir)
        restore = (
            Path(card.retired_path)
            if card.retired_edited and card.retired_path and Path(card.retired_path).is_file()
            else None
        )
        moving = card.pending_move or restore
        transcript = self._transcript_text(snapshot.meeting)
        if moving is not None:
            # 你改过的卡片整份原样搬过来，名字不撞就沿用原来的。
            name = moving.name if moving.name.casefold() not in taken else None
            stem = Path(name).stem if name else _pick_stem(stem_candidates(snapshot.meeting), taken)
            name = f"{stem}.md"
            self._assert_card_dir(root, cards_dir)
            _move_file(moving, cards_dir / name)
            card.state = USER_EDITED
            card.user_named = 1
        else:
            stem = _pick_stem(stem_candidates(snapshot.meeting), taken)
            name = f"{stem}.md"
            tx_rel = f"{TRANSCRIPTS_DIR_NAME}/{stem}.txt" if transcript else None
            auto = render_card(self._view(snapshot, root, tx_rel))
            self._atomic_write(root, cards_dir, name, _compose(auto, card.carry_notes))
            card.written_fps = (card.written_fps + [content_fp(auto)])[-KEEP_FPS:]
            card.state = SYNCED
            card.user_named = 0
        round_.writes += 1
        round_.forget(cards_dir)
        card.pending_move = None
        card.carry_notes = None
        card.retired_path = None
        card.retired_edited = 0
        card.project_id = project_id
        card.root_path = target
        card.rel_path = f"{CARDS_DIR_NAME}/{name}"
        card.synced_at = utc_now()
        card.transcript_rel_path = None
        card.transcript_fp = None
        self._sync_transcript(card, root, cards_dir, stem, transcript, round_)
        round_.touched.add((project_id, target))
        label = self._label(target, card.rel_path)
        if action["action"] == "retired":
            action.update(action="moved", to=label)
        else:
            action.update(action="written", to=label)

    def _sync_transcript(
        self,
        card: _Card,
        root: Path,
        cards_dir: Path,
        stem: str,
        text: str,
        round_: _Round,
    ) -> None:
        """逐字稿副本：内容没变不写；你删掉的副本只在内容变了时才补回来。"""
        if not text:
            return
        fp = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rel = f"{CARDS_DIR_NAME}/{TRANSCRIPTS_DIR_NAME}/{stem}.txt"
        if card.transcript_rel_path == rel and card.transcript_fp == fp:
            return
        tx_dir = cards_dir / TRANSCRIPTS_DIR_NAME
        if not tx_dir.exists():
            self._assert_card_dir(root, cards_dir)
            tx_dir.mkdir(exist_ok=True)
        self._assert_card_dir(root, tx_dir)
        self._atomic_write(root, tx_dir, f"{stem}.txt", text)
        old = card.transcript_rel_path
        if old and old != rel:
            old_path = root / old
            if old_path.is_file() and not _same_file(old_path, tx_dir / f"{stem}.txt"):
                old_path.unlink()
        card.transcript_rel_path = rel
        card.transcript_fp = fp

    # ------------------------------------------------------------ 写库

    def _store(self, snapshot: _Snapshot, card: _Card, dirty_seen: int | None) -> None:
        now = utc_now()
        with self.db.transaction() as connection:
            if snapshot.meeting is None:
                connection.execute("DELETE FROM meeting_cards WHERE meeting_id=?", (card.meeting_id,))
                return
            values = (
                card.project_id,
                card.root_path,
                card.rel_path,
                card.transcript_rel_path,
                card.state,
                card.reason,
                json.dumps(card.written_fps),
                card.transcript_fp,
                card.retired_path,
                card.retired_edited,
                card.carry_notes,
                card.user_named,
                card.synced_at,
                card.error,
                now,
            )
            if not card.exists:
                connection.execute(
                    """INSERT INTO meeting_cards
                       (project_id, root_path, rel_path, transcript_rel_path, state, reason,
                        written_fps, transcript_fp, retired_path, retired_edited, carry_notes,
                        user_named, synced_at, error, updated_at, meeting_id, dirty, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(meeting_id) DO NOTHING""",
                    (*values, card.meeting_id, 0 if dirty_seen is not None else 1, now),
                )
                return
            connection.execute(
                """UPDATE meeting_cards
                      SET project_id=?, root_path=?, rel_path=?, transcript_rel_path=?, state=?,
                          reason=?, written_fps=?, transcript_fp=?, retired_path=?,
                          retired_edited=?, carry_notes=?, user_named=?, synced_at=?, error=?,
                          updated_at=?,
                          dirty = CASE WHEN ? IS NOT NULL AND dirty = ? THEN 0 ELSE dirty END
                    WHERE meeting_id=?""",
                (*values, dirty_seen, dirty_seen, card.meeting_id),
            )

    def _project_had_cards(self, project_id: str, root: str, meeting_id: str) -> bool:
        with self.db.autocommit() as connection:
            return (
                connection.execute(
                    """SELECT 1 FROM meeting_cards
                        WHERE project_id=? AND root_path=? AND meeting_id != ?
                          AND state IN ('synced', 'user_edited') LIMIT 1""",
                    (project_id, root, meeting_id),
                ).fetchone()
                is not None
            )

    def _pause(self, project_id: str) -> None:
        with self.db.transaction() as connection:
            paused = paused_projects(connection)
            if project_id not in paused:
                write_state(connection, PAUSED_KEY, json.dumps(sorted(paused | {project_id})))

    # ------------------------------------------------------------ 磁盘

    def _assert_card_dir(self, root: Path, directory: Path) -> None:
        """每次落盘前重新核对：目录是真实文件夹、在根目录的卡片区之内、根目录没被换到禁区。"""
        if directory.is_symlink() or not directory.is_dir():
            raise CardWriteError(f"「{directory.name}」不是文件夹，卡片先不写")
        round_ = _Round()
        if self._root_state(str(root), round_) != ROOT_ONLINE:
            raise CardWriteError("项目文件夹现在写不了")
        resolved = directory.resolve()
        allowed = (root / CARDS_DIR_NAME).resolve()
        if resolved != allowed and resolved.parent != allowed:
            raise CardWriteError("卡片目录超出了项目文件夹")

    def _make_cards_dir(self, root: Path, cards_dir: Path, meeting: dict[str, Any]) -> None:
        # 只建这一层（parents=False）：根目录必须已经在，盘不在时绝不在系统盘上造目录。
        if self._root_state(str(root), _Round()) != ROOT_ONLINE or not root.is_dir():
            raise CardWriteError("项目文件夹现在写不了")
        cards_dir.mkdir(exist_ok=True)
        with self.db.transaction() as connection:
            notices = [
                item
                for item in _json_state(connection, NOTICES_KEY)
                if isinstance(item, dict) and item.get("project_id") != meeting["project_id"]
            ]
            notices.append(
                {
                    "project_id": meeting["project_id"],
                    "project_name": meeting.get("project_name") or "",
                    "path": str(cards_dir),
                    "at": utc_now(),
                }
            )
            write_state(connection, NOTICES_KEY, json.dumps(notices[-10:], ensure_ascii=False))

    def _atomic_write(self, root: Path, directory: Path, name: str, text: str) -> None:
        """先写同目录的隐藏临时文件再替换：写到一半断电也不会出现半张卡片。"""
        self._assert_card_dir(root, directory)
        target = directory / name
        if target.is_symlink():
            raise CardWriteError(f"「{name}」是替身，不覆盖")
        fd, temp = tempfile.mkstemp(prefix=".shengdang-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _retire_file(self, path: Path, meeting_id: str) -> Path:
        """撤下一律移进回收区，不删除。"""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.settings.data_dir / RETIRED_DIR_NAME / f"{stamp}-{_safe_part(meeting_id)}"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / path.name
        _move_file(path, destination)
        return destination

    def _queue_retire(self, entries: list[dict[str, Any]]) -> None:
        with self.db.transaction() as connection:
            queue = [item for item in _json_state(connection, RETIRE_QUEUE_KEY) if isinstance(item, dict)]
            known = {item.get("path") for item in queue}
            queue.extend(entry for entry in entries if entry["path"] not in known)
            write_state(connection, RETIRE_QUEUE_KEY, json.dumps(queue, ensure_ascii=False))

    def _drain_retire_queue(self, round_: _Round) -> None:
        """旧盘插回来后，把换项目时没来得及撤下的旧卡片移进回收区（只认带同一个 meeting_id 的）。"""
        with self.db.autocommit() as connection:
            queue = [item for item in _json_state(connection, RETIRE_QUEUE_KEY) if isinstance(item, dict)]
            located = {
                (row["root_path"] or "") + "/" + (row["rel_path"] or "")
                for row in connection.execute(
                    "SELECT root_path, rel_path FROM meeting_cards WHERE rel_path IS NOT NULL"
                ).fetchall()
            }
        if not queue:
            return
        remaining = []
        for item in queue:
            path = Path(str(item.get("path") or ""))
            if volume_state(path.parent) == ROOT_VOLUME_OFFLINE:
                remaining.append(item)
                continue
            try:
                if path.is_file() and str(path) not in located:
                    if item.get("kind") == "transcript" or _read_meeting_id(path) == item.get("meeting_id"):
                        self._retire_file(path, str(item.get("meeting_id") or "card"))
            except OSError:
                remaining.append(item)
        with self.db.transaction() as connection:
            write_state(connection, RETIRE_QUEUE_KEY, json.dumps(remaining, ensure_ascii=False))

    # ------------------------------------------------------------ 00 索引.md

    def _refresh_indexes(self, round_: _Round, *, every_project: bool = False) -> None:
        pairs = set(round_.touched)
        if every_project:
            with self.db.autocommit() as connection:
                pairs |= {
                    (row["project_id"], row["root_path"])
                    for row in connection.execute(
                        """SELECT DISTINCT project_id, root_path FROM meeting_cards
                            WHERE rel_path IS NOT NULL AND state IN ('synced', 'user_edited')"""
                    ).fetchall()
                }
        for project_id, root in sorted(pairs):
            if not project_id or not root:
                continue
            try:
                self._write_index(project_id, Path(root), round_)
            except (OSError, CardWriteError) as error:
                logger.warning("会议卡片索引写入失败 %s：%s", root, error)

    def _write_index(self, project_id: str, root: Path, round_: _Round) -> None:
        cards_dir = root / CARDS_DIR_NAME
        if self._root_state(str(root), round_) != ROOT_ONLINE or not cards_dir.is_dir():
            return
        text = self.render_index(project_id, str(root))
        fp = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = str(cards_dir / INDEX_NAME)
        target = cards_dir / INDEX_NAME
        if self._index_fps.get(key) == fp and target.is_file():
            return
        if target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")
            if current == text:
                self._index_fps[key] = fp
                return
            front, _body = split_frontmatter(current)
            if not any(line.strip() == f"generated_by: {GENERATED_BY}" for line in front or []):
                return  # 不是声档写的，不碰
        self._atomic_write(root, cards_dir, INDEX_NAME, text)
        self._index_fps[key] = fp

    def render_index(self, project_id: str, root: str) -> str:
        with self.db.autocommit() as connection:
            project = connection.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
            cards = [
                dict(row)
                for row in connection.execute(
                    """SELECT c.meeting_id, c.rel_path, c.state, m.title, m.recording_date,
                              m.created_at, m.project_origin
                         FROM meeting_cards c JOIN meetings m ON m.id = c.meeting_id
                        WHERE c.project_id=? AND c.root_path=? AND c.rel_path IS NOT NULL
                          AND c.state IN ('synced', 'user_edited')""",
                    (project_id, root),
                ).fetchall()
            ]
            tasks = [
                dict(row)
                for row in connection.execute(
                    """SELECT title, status, assignee, meeting_id FROM tasks
                        WHERE project_id=? AND status IN ('confirmed', 'in_progress')
                        ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, created_at, id""",
                    (project_id,),
                ).fetchall()
            ]
        name = str(project["name"]) if project else ""
        by_meeting = {card["meeting_id"]: card for card in cards}
        cards.sort(key=lambda card: (_local_start(card | {"id": card["meeting_id"]}), card["meeting_id"]), reverse=True)

        def link(card: dict[str, Any]) -> str:
            file_name = Path(card["rel_path"]).name
            title = _one_line(card["title"]) or Path(file_name).stem
            return f"[{title}](<{file_name}>)"

        lines = [
            "---",
            f"project: {_yaml(name)}",
            f"generated_by: {GENERATED_BY}",
            "---",
            "",
            "> 本文件由声档自动维护，请勿手改。把这个项目文件夹交给 Claude Code 时，先让它读这一份。",
            "",
            f"# {_one_line(name)} · 会议记录索引",
            "",
            "## 进行中的行动项",
            "",
        ]
        if tasks:
            for task in tasks:
                parts = [
                    _one_line(task["title"]),
                    ASSIGNEE_LABELS.get(task.get("assignee") or "", "我"),
                    TASK_STATUS_LABELS.get(task["status"], task["status"]),
                ]
                source = by_meeting.get(task.get("meeting_id") or "")
                if source:
                    parts.append(f"来自 {link(source)}")
                lines.append("- " + " · ".join(parts))
        else:
            lines.append("暂无进行中的行动项。")
        lines += ["", "## 会议（按时间倒序）", ""]
        if cards:
            for card in cards:
                start = _local_start(card | {"id": card["meeting_id"]})
                extra = " · AI 自动归属" if card.get("project_origin") == "ai" else ""
                edited = " · 你改过这张卡" if card["state"] == USER_EDITED else ""
                lines.append(f"- {start:%Y-%m-%d %H:%M} · {link(card)}{extra}{edited}")
        else:
            lines.append("这个项目还没有会议卡片。")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ 查询

    def meeting_card(self, connection: Any, meeting_id: str) -> dict[str, Any]:
        """会议详情里的 card 对象：{state, reason, category, path, synced_at, error}。

        category 是界面的三类：ok 已写入；waiting 在等什么（reason 说等什么）；
        stopped 为什么停了（你改过、被删了、暂停了、文件夹找不到……），每类最多一个按钮。
        """
        result = self._meeting_card(connection, meeting_id)
        state, reason = result["state"], result["reason"]
        if state == SYNCED:
            result["category"] = "ok"
        elif state in (USER_EDITED, MISSING) or reason in STOP_REASONS or result["error"]:
            result["category"] = "stopped"
        else:
            result["category"] = "waiting"
        return result

    def _meeting_card(self, connection: Any, meeting_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(connection, meeting_id)
        card = snapshot.card
        _target, reason = self._target(snapshot, _Round())
        meeting = snapshot.meeting or {}
        result: dict[str, Any] = {
            "state": PENDING,
            "reason": reason,
            "path": None,
            "synced_at": card.synced_at,
            "error": card.error,
        }
        same_project = card.project_id == meeting.get("project_id")
        if card.located and same_project:
            result["path"] = str(card.path())
            if reason in _RETRY_REASONS:
                result["state"] = PENDING
            else:
                result["state"] = card.state
                result["reason"] = None
            return result
        if card.state == MISSING and same_project and reason is None:
            result["state"] = MISSING
            return result
        if reason is None:
            result["reason"] = QUEUED
            return result
        result["state"] = BLOCKED if reason in BLOCKED_REASONS else PENDING
        return result

    def project_cards(self, connection: Any, project_id: str) -> dict[str, Any]:
        """项目看板的卡片汇总：写在哪、写了几张、还有几张在等、为什么在等。"""
        root_row = connection.execute(
            """SELECT path FROM project_material_roots WHERE project_id=?
                ORDER BY created_at, id LIMIT 1""",
            (project_id,),
        ).fetchone()
        root = root_row["path"] if root_row else None
        counts = {
            row["state"]: row["n"]
            for row in connection.execute(
                """SELECT state, COUNT(*) AS n FROM meeting_cards WHERE project_id=?
                      AND (state NOT IN ('synced', 'user_edited') OR root_path IS ?)
                    GROUP BY state""",
                (project_id, root),
            ).fetchall()
        }
        since = read_state(connection, SINCE_KEY) or ""
        backfill_yes = read_state(connection, BACKFILL_KEY) == "yes"
        eligible = connection.execute(
            """SELECT COUNT(*) AS n FROM meetings m
                 JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                 LEFT JOIN meeting_cards c ON c.meeting_id = m.id
                WHERE m.project_id=? AND (? OR mv.created_at >= ? OR c.synced_at IS NOT NULL)""",
            (project_id, 1 if backfill_yes else 0, since),
        ).fetchone()["n"]
        written = counts.get(SYNCED, 0) + counts.get(USER_EDITED, 0)
        paused = project_id in paused_projects(connection)
        reason = None
        if not cards_enabled(connection):
            reason = DISABLED
        elif paused:
            reason = PAUSED
        elif not root:
            reason = NO_ROOT
        else:
            state = self._root_state(root, _Round())
            reason = None if state == ROOT_ONLINE else state
        cards_dir = str(Path(root) / CARDS_DIR_NAME) if root else None
        return {
            "root": cards_dir,
            "index_path": str(Path(cards_dir) / INDEX_NAME) if cards_dir else None,
            "written": written,
            "edited": counts.get(USER_EDITED, 0),
            "missing": counts.get(MISSING, 0),
            "waiting": max(0, eligible - written - counts.get(MISSING, 0)),
            "waiting_reason": reason,
            "paused": paused,
        }

    # ------------------------------------------------------------ 你的操作

    def rewrite(self, meeting_id: str, action: str) -> dict[str, Any]:
        """［用最新纪要重写］（你改过的卡片）/［重新生成］（被移走或删掉的卡片）。"""
        if action not in ("rewrite", "regenerate"):
            raise CardsError("不认识的卡片操作")
        with self.db.autocommit() as connection:
            if connection.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone() is None:
                raise LookupError("会议不存在")
        self.sync_meeting(meeting_id, mode=action)
        with self.db.autocommit() as connection:
            return self.meeting_card(connection, meeting_id)

    def pause_project(self, project_id: str) -> dict[str, Any]:
        """［不要写］：这个项目停止写卡片，已写的没改过的卡片移进回收区，改过的留在原处。"""
        self._pause(project_id)
        result = self._retire_where("c.project_id=?", (project_id,), PAUSED)
        self.dismiss_notice(project_id)
        return result

    def resume_project(self, project_id: str) -> dict[str, Any]:
        """［恢复写入］：从暂停名单里拿掉，文件已经不在的卡片按新卡片重写。"""
        with _LOCK:
            with self.db.transaction() as connection:
                paused = paused_projects(connection)
                write_state(connection, PAUSED_KEY, json.dumps(sorted(paused - {project_id})))
                rows = connection.execute(
                    "SELECT meeting_id, root_path, rel_path, state FROM meeting_cards WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                for row in rows:
                    still_there = (
                        row["rel_path"]
                        and row["root_path"]
                        and (Path(row["root_path"]) / row["rel_path"]).is_file()
                    )
                    if still_there and row["state"] in _LOCATED_STATES:
                        connection.execute(
                            "UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id=?",
                            (row["meeting_id"],),
                        )
                        continue
                    connection.execute(
                        """UPDATE meeting_cards
                              SET state='pending', reason=NULL, root_path=NULL, rel_path=NULL,
                                  transcript_rel_path=NULL, transcript_fp=NULL, user_named=0,
                                  dirty = dirty + 1, updated_at=?
                            WHERE meeting_id=?""",
                        (utc_now(), row["meeting_id"]),
                    )
        return {"ok": True}

    def retire_all(self) -> dict[str, Any]:
        """回滚第一步：关掉卡片，没改过的移进回收区，改过的留在原处并列出来。"""
        with _LOCK:
            with self.db.transaction() as connection:
                write_state(connection, ENABLED_KEY, "0")
        return self._retire_where("1=1", (), DISABLED)

    def enable(self) -> dict[str, Any]:
        with _LOCK:
            with self.db.transaction() as connection:
                write_state(connection, ENABLED_KEY, "1")
                connection.execute("UPDATE meeting_cards SET dirty = dirty + 1")
        return {"ok": True}

    def _retire_where(self, where: str, params: tuple[Any, ...], reason: str) -> dict[str, Any]:
        retired = 0
        kept: list[dict[str, Any]] = []
        round_ = _Round()
        with _LOCK:
            with self.db.autocommit() as connection:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        f"""SELECT c.*, m.title FROM meeting_cards c
                              LEFT JOIN meetings m ON m.id = c.meeting_id
                             WHERE {where} AND c.rel_path IS NOT NULL
                               AND c.state IN ('synced', 'user_edited')""",
                        params,
                    ).fetchall()
                ]
            for row in rows:
                card = _Card.from_row(row["meeting_id"], row)
                path = card.path()
                if path is None or self._root_state(card.root_path or "", round_) != ROOT_ONLINE:
                    continue
                try:
                    if not path.is_file():
                        edited = False
                    else:
                        auto, _notes = split_card(path.read_text(encoding="utf-8", errors="replace"))
                        edited = card.state == USER_EDITED or content_fp(auto) not in card.written_fps
                    if edited:
                        kept.append(
                            {"meeting_id": card.meeting_id, "title": row.get("title"), "path": str(path)}
                        )
                        continue
                    if path.is_file():
                        self._retire_file(path, card.meeting_id)
                    if card.transcript_rel_path:
                        tx = Path(card.root_path or "") / card.transcript_rel_path
                        if tx.is_file():
                            self._retire_file(tx, card.meeting_id)
                except OSError as error:
                    logger.warning("撤下会议卡片失败 %s：%s", card.meeting_id, error)
                    continue
                retired += 1
                round_.touched.add((card.project_id or "", card.root_path or ""))
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE meeting_cards
                              SET state='retired', reason=?, root_path=NULL, rel_path=NULL,
                                  transcript_rel_path=NULL, transcript_fp=NULL, user_named=0,
                                  updated_at=?
                            WHERE meeting_id=?""",
                        (reason, utc_now(), card.meeting_id),
                    )
            for _project_id, root in round_.touched:
                index = Path(root) / CARDS_DIR_NAME / INDEX_NAME
                if reason == DISABLED and index.is_file():
                    try:
                        self._retire_file(index, "index")
                    except OSError:
                        pass
                    self._index_fps.pop(str(index), None)
            if reason != DISABLED:
                self._refresh_indexes(round_)
        return {"retired": retired, "kept": kept}

    # ------------------------------------------------------------ 历史补写

    def backfill_preview(self, connection: Any) -> dict[str, Any]:
        """上线前的历史会议能补写多少：{meetings, projects, ai_attributed, no_folder_projects, top[]}。"""
        since = read_state(connection, SINCE_KEY) or ""
        rows = connection.execute(
            """SELECT m.project_id, p.name AS project_name,
                      COUNT(*) AS n, SUM(CASE WHEN m.project_origin='ai' THEN 1 ELSE 0 END) AS ai,
                      (SELECT r.path FROM project_material_roots r WHERE r.project_id = m.project_id
                        ORDER BY r.created_at, r.id LIMIT 1) AS root
                 FROM meetings m
                 JOIN projects p ON p.id = m.project_id
                 JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                 LEFT JOIN meeting_cards c ON c.meeting_id = m.id
                WHERE mv.created_at < ? AND (c.meeting_id IS NULL OR c.synced_at IS NULL)
                GROUP BY m.project_id
                ORDER BY n DESC, p.name""",
            (since,),
        ).fetchall()
        paused = paused_projects(connection)
        top = [
            {
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "count": row["n"],
                "path": str(Path(row["root"]) / CARDS_DIR_NAME) if row["root"] else None,
                "paused": row["project_id"] in paused,
            }
            for row in rows
        ]
        with_folder = [item for item in top if item["path"] and not item["paused"]]
        return {
            "meetings": sum(item["count"] for item in with_folder),
            "projects": len(with_folder),
            "ai_attributed": sum(
                row["ai"] or 0 for row in rows if row["root"] and row["project_id"] not in paused
            ),
            "no_folder_projects": sum(1 for item in top if not item["path"]),
            "no_folder_meetings": sum(item["count"] for item in top if not item["path"]),
            "top": top,
        }

    def backfill_banner(self, connection: Any) -> dict[str, Any] | None:
        """工作台横幅：没回答过、没在「稍后」期间、确实有可补写的会时才出现。"""
        if not cards_enabled(connection) or read_state(connection, BACKFILL_KEY):
            return None
        snoozed = read_state(connection, BACKFILL_SNOOZE_KEY)
        if snoozed and snoozed > utc_now():
            return None
        preview = self.backfill_preview(connection)
        return preview if preview["meetings"] else None

    def answer_backfill(self, answer: str) -> dict[str, Any]:
        with self.db.transaction() as connection:
            if answer == "later":
                until = (datetime.now(UTC) + timedelta(days=BACKFILL_SNOOZE_DAYS)).isoformat()
                write_state(connection, BACKFILL_SNOOZE_KEY, until)
            elif answer in ("yes", "no"):
                write_state(connection, BACKFILL_KEY, answer)
            else:
                raise CardsError("只能回答补写、不补写或稍后")
        return {"answer": answer}

    # ------------------------------------------------------------ 第一张卡片提示

    def notices(self, connection: Any) -> list[dict[str, Any]]:
        return [item for item in _json_state(connection, NOTICES_KEY) if isinstance(item, dict)]

    def dismiss_notice(self, project_id: str) -> None:
        with self.db.transaction() as connection:
            remaining = [
                item
                for item in _json_state(connection, NOTICES_KEY)
                if isinstance(item, dict) and item.get("project_id") != project_id
            ]
            write_state(connection, NOTICES_KEY, json.dumps(remaining, ensure_ascii=False))

    def reveal(self, *, project_id: str | None = None, meeting_id: str | None = None) -> str:
        """在访达里打开项目的卡片文件夹（或选中某场会的卡片）。只认声档自己的卡片区，不接受任意路径。"""
        with self.db.autocommit() as connection:
            if meeting_id:
                card = self.meeting_card(connection, meeting_id)
                target = card.get("path")
                if not target:
                    raise CardsError("这场会还没有卡片")
                reveal_args = ["-R", target]
            else:
                summary = self.project_cards(connection, project_id or "")
                target = summary["root"]
                if not target or not Path(target).is_dir():
                    raise CardsError("这个项目还没有会议卡片文件夹")
                reveal_args = [target]
        if sys.platform != "darwin":
            raise CardsError("只能在运行声档的 Mac 上打开文件夹")
        subprocess.run(["open", *reveal_args], check=False, timeout=10)
        return target


# ---------------------------------------------------------------------- 工具


def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value)[:80] or "card"


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _move_file(source: Path, destination: Path) -> None:
    """同一块盘直接改名；跨盘先复制、比对哈希一致后再删源文件。"""
    if destination.exists():
        raise CardWriteError(f"「{destination.name}」已经存在，不覆盖")
    try:
        os.rename(source, destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    shutil.copy2(source, destination)
    if _sha256(source) != _sha256(destination):
        destination.unlink(missing_ok=True)
        raise CardWriteError("跨盘移动卡片时校验不一致，原文件保留")
    source.unlink()
