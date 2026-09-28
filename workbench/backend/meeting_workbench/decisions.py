"""决议入库（第四期 4a）：纪要决议段里的一条是 decisions 的一行，id 跨纪要版本不变。

来源只有 meetings.current_minutes_version_id 那一版纪要的决议段，在本机解析，不调 AI，也不读
minutes-evidence.json（改过一次稿就验证不了）。
- parse_decisions：选段、拆条、认时间点、清理、去重；graph.minutes_outline 也用它，notify.py 不动；
- carry_over：新解析出的条目和这场会已有的行配对，接回原来的 id；配不上的旧行记 gone_at，不删；
- ingest_pending：links_loop 的 L1，每轮最多 100 场会或 1.0 秒，会议转写时照跑；
- ledger_decisions：简报和聚焦视图读表，台账落后时返回 None，调用方当场解析（id 为 null）；
- place：POST /api/decisions/{id}/placement，只改 placement，不把会挂到需求上。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .file_mentions import _meeting_ns
from .tasks import UNDO_WINDOW_SECONDS

logger = logging.getLogger(__name__)

# 解析规则改了就加一：台账里 parser 对不上的会整场重新入库，id 照样接回。
PARSER_VERSION = 1

ROUND_MEETINGS = 100
ROUND_SECONDS = 1.0
# 不见了的行多久以内还能复活（回滚纪要时接回原来的 id）
REVIVE_DAYS = 180
# 配对：相似比不低于 0.8，或两边时间点相差 5 秒以内且不低于 0.5；时间点近的加 0.2
MATCH_RATIO = 0.8
NEAR_RATIO = 0.5
NEAR_MS = 5_000
NEAR_BONUS = 0.2
# 配上的一对相似比低于这个算实质变化
SAME_RATIO = 0.85

NO_MINUTES = "no_minutes"
NO_SECTION = "no_section"
EMPTY = "empty"
# 简报和聚焦视图现有的三句
NOTE_TEXT = {
    NO_MINUTES: "纪要还没写好",
    NO_SECTION: "这场纪要没有决议段",
    EMPTY: "决议段是空的",
}
PLACEMENTS = ("none", "picked", "ai")

# 只看 ## 标题（和 notify._DECISION_SECTION 同一级）；段落到下一个 # 或 ## 标题为止
_H2 = re.compile(r"^##[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_SECTION_END = re.compile(r"^#{1,2}[ \t]", re.MULTILINE)
_H3 = re.compile(r"^###[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_HEAD_NUMBER = re.compile(r"^[一二三四五六七八九十百\d]+\s*[、.．:：)）]?\s*")
_EXACT_HEAD = re.compile(r"(核心|会议|主要|本次)?决议(事项|清单|汇总)?")
_NEGATIVE_HEAD = re.compile(r"(未|待|没有|无)\s*(达成)?\s*(决议|结论|共识)|待定|未决")
# 列表项：「1.」「1)」「-」「*」「+」后面要有空白，「1、」可以不带
_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:\d+[.)）]|[-*+•])[ \t]+|\d+、[ \t]*)(?P<body>\S.*?)[ \t]*$"
)
# 时间点：[MM:SS]、[HH:MM:SS]，范围用破折号、连字符、波浪号、「至」「到」连起来，外面可以包反引号
_TIME = r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?"
_RANGE = re.compile(
    r"`?\[\s*" + _TIME + r"(?:\s*(?:[—–\-~～〜]+|至|到)\s*" + _TIME + r")?\s*\]`?"
)
# 清理时去掉的时间点：方括号里时间后面跟别的字（说话人之类）也一起去掉
_ANCHOR_ANY = re.compile(r"\s*`?\[\s*\d{1,2}:\d{2}(?::\d{2})?[^\]\n]*\]`?")
# 「决议一：」「结论 2·」这类前缀；后面必须跟编号或分隔符，「结论是……」不动
_PREFIX = re.compile(
    r"^(?:决议|结论|共识)\s*(?:[一二三四五六七八九十百\d]+\s*[·:：、.．\-—]?|[·:：、.．\-—])\s*"
)
_EMPTY_KEYS = frozenset({"无", "暂无", "无决议", "暂无决议"})
# 数值词：日期、星期、数字和百分数、中文数字（百分号写成 \x25，用词检查不把正则当界面文字）
_VALUE_TOKEN = re.compile(
    r"\d{1,2}月\d{1,2}[日号]|(?:周|星期)[一二三四五六日天]|\d+(?:\.\d+)?\x25?|[零一二两三四五六七八九十百千万]+"
)


class DecisionNotFound(LookupError):
    pass


class DecisionGone(RuntimeError):
    pass


class PlacementRejected(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    text: str
    detail: str
    text_key: str
    start_ms: int | None
    end_ms: int | None


@dataclass(frozen=True)
class Parsed:
    items: list[Item]
    note: str | None
    section_hash: str | None


# ---------------------------------------------------------------------- 解析


def _ms(hours: str, minutes: str, seconds: str | None) -> int:
    if seconds is None:
        # [MM:SS]
        return (int(hours) * 60 + int(minutes)) * 1000
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000


def parse_anchor(text: str) -> tuple[int | None, int | None]:
    """第一个时间点的 (start_ms, end_ms)；不是范围时 end_ms 为 None。"""
    match = _RANGE.search(text or "")
    if match is None:
        return None, None
    start = _ms(match.group(1), match.group(2), match.group(3))
    end = _ms(match.group(4), match.group(5), match.group(6)) if match.group(4) else None
    return start, end


def text_key(text: str) -> str:
    """统一全半角和大小写，去掉标点、符号、空白后的文字：配对和判断「同一句话」用。"""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] not in "PSZC")


def value_tokens(text: str) -> list[str]:
    """数值词（数字、百分数、中文数字、「9月30日」「周三」），排好序。"""
    return sorted(_VALUE_TOKEN.findall(unicodedata.normalize("NFKC", text or "")))


def _clean(raw: str) -> str:
    """先去时间点和 **、__，再去「决议一：」这类前缀。存的时候不截断。"""
    text = _ANCHOR_ANY.sub("", raw).replace("**", "").replace("__", "")
    text = text.strip(" \t　`|")
    text = _PREFIX.sub("", text)
    return text.strip(" \t　`|-—·：:")


def _head_rank(title: str) -> int | None:
    """标题就是「决议」这类的排 0，含「决议」的 1，含「结论」「共识」的 2；否定的和别的不要。"""
    title = _HEAD_NUMBER.sub("", title.replace("**", "").replace("__", "").strip()).strip()
    if _NEGATIVE_HEAD.search(title):
        return None
    if _EXACT_HEAD.fullmatch(title):
        return 0
    if "决议" in title:
        return 1
    if "结论" in title or "共识" in title:
        return 2
    return None


def pick_section(markdown: str) -> str | None:
    """选决议段的正文：排名最前的，同一级取靠前的。没有就是 None。"""
    best: tuple[int, int, str] | None = None
    for index, head in enumerate(_H2.finditer(markdown)):
        rank = _head_rank(head.group(1))
        if rank is None or (best is not None and rank >= best[0]):
            continue
        tail = _SECTION_END.search(markdown, head.end())
        best = (rank, index, markdown[head.end(): tail.start() if tail else len(markdown)])
    return None if best is None else best[2]


def _detail_piece(line: str) -> str:
    match = _ITEM.match(line)
    return _clean(match.group("body") if match else line)


def _heading_items(body: str, heads: list[re.Match[str]]) -> list[dict[str, Any]]:
    """小标题式（v3 六段式「### 决议 1 · …」）：一个小标题一条，正文整理后放 detail。"""
    items = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(body)
        rest = body[head.end():end]
        start, stop = parse_anchor(head.group(1))
        if start is None:
            start, stop = parse_anchor(rest)
        pieces = [
            _detail_piece(line)
            for line in rest.strip().splitlines()
            if not line.lstrip().startswith(("#", ">"))
        ]
        items.append(
            {
                "text": _clean(head.group(1)),
                "detail": " ".join(piece for piece in pieces if piece),
                "start_ms": start,
                "end_ms": stop,
            }
        )
    return items


def _list_items(body: str) -> list[dict[str, Any]]:
    """列表式：以第一项的缩进为准，更深的子项和续行并进上一条的 detail。"""
    items: list[dict[str, Any]] = []
    base: int | None = None
    current: dict[str, Any] | None = None
    for line in body.splitlines():
        if not line.strip():
            continue
        match = _ITEM.match(line)
        indent = len(match.group("indent").expandtabs(4)) if match else None
        if match and indent is not None and (base is None or indent <= base):
            base = indent if base is None else base
            start, stop = parse_anchor(match.group("body"))
            current = {"text": _clean(match.group("body")), "detail": "", "start_ms": start, "end_ms": stop}
            items.append(current)
            continue
        if current is None or line.lstrip().startswith(("#", ">", "|", "---")):
            continue
        piece = _detail_piece(line)
        if piece:
            current["detail"] = f"{current['detail']} {piece}".strip()
        if current["start_ms"] is None:
            current["start_ms"], current["end_ms"] = parse_anchor(line)
    return items


def parse_decisions(markdown: str) -> Parsed:
    """纪要决议段里的条目。note 是 no_section 或 empty；section_hash 是选中那一段正文的 sha1。"""
    body = pick_section(markdown or "")
    if body is None:
        return Parsed([], NO_SECTION, None)
    heads = list(_H3.finditer(body))
    raw = _heading_items(body, heads) if heads else _list_items(body)
    seen: set[str] = set()
    items: list[Item] = []
    for entry in raw:
        key = text_key(entry["text"])
        if not key or key in _EMPTY_KEYS or key in seen:
            continue
        seen.add(key)
        items.append(Item(entry["text"], entry["detail"], key, entry["start_ms"], entry["end_ms"]))
    section_hash = hashlib.sha1(body.strip().encode("utf-8")).hexdigest()
    return Parsed(items, None if items else EMPTY, section_hash)


def parse_safely(markdown: str | None) -> Parsed:
    """纪要没写好记 no_minutes；解析出错按「决议段是空的」记，不停这一轮（日志只记错误类型）。"""
    if not markdown:
        return Parsed([], NO_MINUTES, None)
    try:
        return parse_decisions(markdown)
    except Exception as error:  # noqa: BLE001  一场会的纪要出错不影响别的会
        logger.warning("决议段解析出错：%s", type(error).__name__)
        return Parsed([], EMPTY, None)


# ---------------------------------------------------------------------- 接回 id


def _ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def _near(left: int | None, right: int | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= NEAR_MS


def carry_over(old: Sequence[Mapping[str, Any]], new: Sequence[Item]) -> list[str | None]:
    """给每条新条目找原来的 id（None 是新的）。old 是活的行和 180 天内不见了的行。

    1. text_key 相同的先配，活的优先，序号最近的优先；
    2. 剩下的比 text_key 的相似比：不低于 0.8，或两边时间点相差 5 秒以内且不低于 0.5 才能配；
       时间点近的加 0.2，贪心取最高的。
    """
    ids: list[str | None] = [None] * len(new)
    free = {row["id"]: row for row in old}
    for index, item in enumerate(new):
        same = [row for row in free.values() if row["text_key"] == item.text_key]
        if same:
            row = min(same, key=lambda r: (r["gone_at"] is not None, abs(int(r["ordinal"]) - index), r["id"]))
            ids[index] = row["id"]
            free.pop(row["id"])
    candidates: list[tuple[float, bool, int, str]] = []
    for index, item in enumerate(new):
        if ids[index] is not None:
            continue
        for row in free.values():
            ratio = _ratio(row["text_key"], item.text_key)
            near = _near(row["start_ms"], item.start_ms)
            if ratio >= MATCH_RATIO or (near and ratio >= NEAR_RATIO):
                candidates.append((ratio + (NEAR_BONUS if near else 0.0), row["gone_at"] is None, index, row["id"]))
    for _score, _live, index, row_id in sorted(candidates, key=lambda c: (-c[0], not c[1], c[2], c[3])):
        if ids[index] is None and row_id in free:
            ids[index] = row_id
            free.pop(row_id)
    return ids


def _material_pair(row: Mapping[str, Any], item: Item) -> bool:
    """配上的一对算不算实质变化：数值词不同，或相似比低于 0.85。改错字不算。"""
    if value_tokens(row["text"]) != value_tokens(item.text):
        return True
    return row["text_key"] != item.text_key and _ratio(row["text_key"], item.text_key) < SAME_RATIO


def pair_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """排好序的 id 和数值词的 sha1（4c 对比完也记这个）。"""
    payload = sorted([row["id"], value_tokens(row["text"])] for row in rows)
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------- 入库（L1）

_PENDING_SQL = """
SELECT m.id, m.project_id, m.current_minutes_version_id AS version_id
  FROM meetings m LEFT JOIN decision_scan s ON s.meeting_id = m.id
 WHERE s.meeting_id IS NULL
    OR s.minutes_version_id IS NOT m.current_minutes_version_id
    OR s.project_id IS NOT m.project_id
    OR s.parser != ?
 ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id
 LIMIT ?"""


def pending_count(connection: Any) -> int:
    """还没入库或台账落后的会有几场（健康检查的 waiting.decisions，links_loop 每轮末尾数一次）。"""
    return int(
        connection.execute(
            """SELECT COUNT(*) FROM meetings m LEFT JOIN decision_scan s ON s.meeting_id = m.id
                WHERE s.meeting_id IS NULL
                   OR s.minutes_version_id IS NOT m.current_minutes_version_id
                   OR s.project_id IS NOT m.project_id
                   OR s.parser != ?""",
            (PARSER_VERSION,),
        ).fetchone()[0]
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def backfill_cutoff(now: datetime, backfill_days: int, links_since: str | None) -> datetime:
    """会议时间早于它的会入库时不进 pending、直接 done。0 表示只做新会：分界是 links_since。"""
    if backfill_days > 0:
        return now - timedelta(days=backfill_days)
    if links_since:
        try:
            since = datetime.fromisoformat(links_since.replace("Z", "+00:00"))
        except ValueError:
            return now
        return since if since.tzinfo else since.replace(tzinfo=UTC)
    return now


def decision_moment(meeting_row: Mapping[str, Any], start_ms: int | None) -> datetime | None:
    """决议定下的时间：会议时间（file_mentions._meeting_ns）加上 start_ms。影响的时间窗和时间线用。"""
    ns = _meeting_ns(meeting_row["recording_date"], meeting_row["created_at"])
    if ns is None:
        return None
    moment = datetime.fromtimestamp(ns // 1_000_000_000, UTC) + timedelta(microseconds=(ns % 1_000_000_000) // 1000)
    return moment + timedelta(milliseconds=int(start_ms or 0))


def _write_items(
    connection: Any, meeting_id: str, items: Sequence[Item], stamp: str, now: datetime
) -> bool:
    """配 id，只写真变了的列。返回有没有实质变化（多了或少了 id、配上的一对变了实质）。"""
    revive_since = _stamp(now - timedelta(days=REVIVE_DAYS))
    rows = [
        dict(row)
        for row in connection.execute(
            """SELECT id, ordinal, text, detail, text_key, start_ms, end_ms, gone_at FROM decisions
                WHERE meeting_id = ? AND (gone_at IS NULL OR gone_at >= ?)""",
            (meeting_id, revive_since),
        ).fetchall()
    ]
    by_id = {row["id"]: row for row in rows}
    ids = carry_over(rows, items)
    material = False
    for ordinal, (item, matched) in enumerate(zip(items, ids, strict=True)):
        values = (ordinal, item.text, item.detail, item.text_key, item.start_ms, item.end_ms)
        if matched is None:
            material = True
            connection.execute(
                """INSERT INTO decisions(id, meeting_id, ordinal, text, detail, text_key, start_ms,
                                         end_ms, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("dec-" + secrets.token_hex(8), meeting_id, *values, stamp, stamp),
            )
            continue
        row = by_id[matched]
        if row["gone_at"] is not None or _material_pair(row, item):
            material = True
        connection.execute(
            """UPDATE decisions
                  SET ordinal = ?, text = ?, detail = ?, text_key = ?, start_ms = ?, end_ms = ?,
                      gone_at = NULL, updated_at = ?
                WHERE id = ?
                  AND (ordinal IS NOT ? OR text IS NOT ? OR detail IS NOT ? OR text_key IS NOT ?
                       OR start_ms IS NOT ? OR end_ms IS NOT ? OR gone_at IS NOT NULL)""",
            (*values, stamp, matched, *values),
        )
    kept = set(ids)
    for row in rows:
        if row["gone_at"] is None and row["id"] not in kept:
            material = True
            connection.execute(
                "UPDATE decisions SET gone_at = ?, updated_at = ? WHERE id = ? AND gone_at IS NULL",
                (stamp, stamp, row["id"]),
            )
    return material


def ingest_meeting(
    db: Database, row: Mapping[str, Any], *, now: datetime, cutoff: datetime
) -> str:
    """入库一场会：事务外读纪要、解析，BEGIN IMMEDIATE 里再核对版本和项目，对不上就不写。

    返回 written、fast（决议段没变，只改台账）或 skipped（解析期间版本或项目变了）。
    """
    meeting_id = row["id"]
    markdown = None
    if row["version_id"] is not None:
        with db.autocommit() as connection:
            found = connection.execute(
                "SELECT markdown FROM minutes_versions WHERE id = ?", (row["version_id"],)
            ).fetchone()
        markdown = found["markdown"] if found else None
    parsed = parse_safely(markdown)
    stamp = _stamp(now)
    with db.transaction() as connection:
        current = connection.execute(
            """SELECT current_minutes_version_id AS version_id, project_id, recording_date, created_at
                 FROM meetings WHERE id = ?""",
            (meeting_id,),
        ).fetchone()
        if (
            current is None
            or current["version_id"] != row["version_id"]
            or current["project_id"] != row["project_id"]
        ):
            return "skipped"
        project_id = current["project_id"]
        scan = connection.execute(
            "SELECT parser, section_hash, note, project_id FROM decision_scan WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
        same_section = (
            scan is not None
            and scan["parser"] == PARSER_VERSION
            and scan["section_hash"] == parsed.section_hash
            and scan["note"] == parsed.note
        )
        moved = scan is not None and scan["project_id"] != project_id
        connection.execute(
            """INSERT INTO decision_scan(meeting_id, minutes_version_id, project_id, parser,
                                         section_hash, note, scanned_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(meeting_id) DO UPDATE SET
                   minutes_version_id = excluded.minutes_version_id,
                   project_id = excluded.project_id, parser = excluded.parser,
                   section_hash = excluded.section_hash, note = excluded.note,
                   scanned_at = excluded.scanned_at, updated_at = excluded.updated_at""",
            (meeting_id, row["version_id"], project_id, PARSER_VERSION, parsed.section_hash,
             parsed.note, stamp, stamp),
        )
        if same_section and not moved:
            # 快路径：台账不在 GRAPH_REV_TABLES 里，关系图缓存不动
            return "fast"
        material = False if same_section else _write_items(connection, meeting_id, parsed.items, stamp, now)
        if moved:
            # 换了项目：ai 放到别的项目需求里的改回自动；picked 和 none 不动（读的时候只认同项目的需求）
            connection.execute(
                """UPDATE decisions SET placement = NULL, requirement_id = NULL, placed_at = NULL,
                                        updated_at = ?
                    WHERE meeting_id = ? AND placement = 'ai'
                      AND requirement_id IN (SELECT id FROM requirements WHERE project_id IS NOT ?)""",
                (stamp, meeting_id, project_id),
            )
        if (material or moved) and project_id is not None:
            _queue_pair(connection, meeting_id, current, now=now, cutoff=cutoff, stamp=stamp)
    return "written"


def _queue_pair(
    connection: Any, meeting_id: str, meeting: Mapping[str, Any], *, now: datetime, cutoff: datetime, stamp: str
) -> None:
    """实质变化或换了项目：等 4c 对比（pending）。早于回补窗口的会直接 done，pair_hash 照算。"""
    live = connection.execute(
        "SELECT id, text FROM decisions WHERE meeting_id = ? AND gone_at IS NULL", (meeting_id,)
    ).fetchall()
    if not live:
        return
    ns = _meeting_ns(meeting["recording_date"], meeting["created_at"])
    old = ns is not None and ns < int(cutoff.timestamp()) * 1_000_000_000
    connection.execute(
        """UPDATE decision_scan
              SET pair_state = ?, pair_hash = CASE WHEN ? IS NULL THEN pair_hash ELSE ? END,
                  pair_attempts = 0, pair_claimed_at = NULL, pair_after = NULL, pair_error = NULL,
                  updated_at = ?
            WHERE meeting_id = ?""",
        (
            "done" if old else "pending",
            pair_hash(live) if old else None,
            pair_hash(live) if old else None,
            stamp,
            meeting_id,
        ),
    )


def ingest_pending(
    db: Database,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    backfill_days: int = 180,
    max_meetings: int = ROUND_MEETINGS,
    max_seconds: float = ROUND_SECONDS,
) -> dict[str, int]:
    """links_loop 的 L1：一条待办查询（没有台账、纪要版本或项目对不上、解析器升级），新的在前。

    每轮最多 max_meetings 场会或 max_seconds 秒；纪要内容出错按会记，只有数据库错误结束这一轮。
    """
    moment = now or datetime.now(UTC)
    with db.autocommit() as connection:
        todo = [
            dict(row)
            for row in connection.execute(_PENDING_SQL, (PARSER_VERSION, max_meetings)).fetchall()
        ]
        since = connection.execute("SELECT value FROM app_state WHERE key = 'links_since'").fetchone()
    cutoff = backfill_cutoff(moment, backfill_days, since["value"] if since else None)
    deadline = clock() + max_seconds
    counts = {"pending": len(todo), "tried": 0, "written": 0, "fast": 0, "skipped": 0}
    for row in todo:
        if clock() >= deadline:
            break
        counts["tried"] += 1
        try:
            counts[ingest_meeting(db, row, now=moment, cutoff=cutoff)] += 1
        except sqlite3.OperationalError:
            raise
        except Exception:  # noqa: BLE001
            # 某场会一直出同一个意料之外的错时，不能让它每轮都结束整轮、把 L2 和清理一起卡住
            logger.exception("决议入库跳过一场会：%s", row.get("id"))
            counts["skipped"] += 1
    return counts


# ---------------------------------------------------------------------- 读


def ledger_decisions(
    connection: Any, meeting_id: str, minutes_version_id: str | None
) -> tuple[list[dict[str, Any]], str | None] | None:
    """台账跟上当前纪要版本和解析器时，按序号给还在的决议和 note；落后时 None。只有一条 SELECT。"""
    rows = connection.execute(
        """SELECT s.minutes_version_id, s.parser, s.note,
                  d.id, d.text, d.detail, d.start_ms, d.end_ms
             FROM decision_scan s
             LEFT JOIN decisions d ON d.meeting_id = s.meeting_id AND d.gone_at IS NULL
            WHERE s.meeting_id = ?
            ORDER BY d.ordinal, d.id""",
        (meeting_id,),
    ).fetchall()
    if not rows or rows[0]["minutes_version_id"] != minutes_version_id or rows[0]["parser"] != PARSER_VERSION:
        return None
    items = [
        {key: row[key] for key in ("id", "text", "detail", "start_ms", "end_ms")}
        for row in rows
        if row["id"] is not None
    ]
    return items, rows[0]["note"]


def serialize(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "id", "meeting_id", "text", "detail", "start_ms", "end_ms", "placement",
            "requirement_id", "placed_at",
        )
    }


# ---------------------------------------------------------------------- 归需求


def place(
    connection: Any,
    decision_id: str,
    placement: str | None,
    requirement_id: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把一条决议放到需求下（picked）、说它不属于具体需求（none）或回到自动（None）。

    ai 只用于撤销时把原值原样发回，校验和 picked 一样：需求要在这场会的项目里。只改 placement，
    不把会挂到需求上（挂上会让只关联一个需求的会变成关联两个，它别的决议会悄悄变成没归属）。
    """
    row = connection.execute(
        """SELECT d.*, m.project_id FROM decisions d JOIN meetings m ON m.id = d.meeting_id
            WHERE d.id = ?""",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise DecisionNotFound("决议不存在")
    if row["gone_at"] is not None:
        raise DecisionGone("这条决议已经不在纪要里了")
    if placement is not None and placement not in PLACEMENTS:
        raise PlacementRejected("只能放到同一个项目的需求里")
    target = None
    if placement in ("picked", "ai"):
        found = (
            connection.execute(
                "SELECT 1 FROM requirements WHERE id = ? AND project_id = ?",
                (requirement_id, row["project_id"]),
            ).fetchone()
            if requirement_id and row["project_id"]
            else None
        )
        if found is None:
            raise PlacementRejected("只能放到同一个项目的需求里")
        target = requirement_id
    moment = now or datetime.now(UTC)
    undo = {"placement": row["placement"], "requirement_id": row["requirement_id"]}
    decision = dict(row)
    if (row["placement"], row["requirement_id"]) != (placement, target):
        stamp = _stamp(moment)
        connection.execute(
            """UPDATE decisions SET placement = ?, requirement_id = ?, placed_at = ?, updated_at = ?
                WHERE id = ?""",
            (placement, target, stamp, stamp, decision_id),
        )
        decision.update(placement=placement, requirement_id=target, placed_at=stamp)
    return {
        "decision": serialize(decision),
        "undo": undo,
        "undo_until": _z(moment + timedelta(seconds=UNDO_WINDOW_SECONDS)),
    }

