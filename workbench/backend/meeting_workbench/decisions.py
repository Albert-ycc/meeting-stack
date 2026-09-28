"""决议入库（第四期 4a）：纪要决议段里的一条是 decisions 的一行，id 跨纪要版本不变。

来源只有 meetings.current_minutes_version_id 那一版纪要的决议段，在本机解析，不调 AI，也不读
minutes-evidence.json（改过一次稿就验证不了）。
- parse_decisions：选段、拆条、认时间点、清理、去重；graph.minutes_outline 也用它，notify.py 不动；
- carry_over：新解析出的条目和这场会已有的行配对，接回原来的 id；配不上的旧行记 gone_at，不删；
- ingest_pending：links_loop 的 L1，每轮最多 100 场会或 1.0 秒，会议转写时照跑；
- ledger_decisions：简报和聚焦视图读表，台账落后时返回 None，调用方当场解析（id 为 null）；
- place：POST /api/decisions/{id}/placement，只改 placement，不把会挂到需求上。

4c：
- link_pairs：L1 写事务里的两步，只写 relations——同项目两场会 text_key 相同（至少 4 个字）的决议写
  规则版「后来又提到」（origin rule）；实质变化时 llm 写的 shown 对比行原话对不上的立刻 cleared；
- effective_requirement、title_match：决议放在哪个需求下，读的时候算；
- requirement_log：需求页「决议」卡（最多 5 条语句）；pair_state_line：卡和时间线［决议］的状态句；
- superseded_sql、project_decisions：给 4e、4h。
对比行读的时候两条决议都还在（gone_at IS NULL）才算数。
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
from collections.abc import Callable, Iterable, Mapping, Sequence
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
        if project_id is not None:
            # 4c 加的两步，只写 relations：规则版「后来又提到」，实质变化时原话对不上的对比行收回
            link_pairs(connection, meeting_id, project_id, current, material=material, stamp=stamp)
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



# ---------------------------------------------------------------------- 4c：L1 里的两步（只写 relations）

PAIR_KINDS = ("later_changed", "restated")
# 规则版「后来又提到」：text_key 至少这么多个字
RESTATED_MIN_CHARS = 4
_PAIR_KINDS_SQL = "'later_changed', 'restated'"
_IN_BATCH = 400


def pair_ident(early_id: str, late_id: str) -> str:
    """later_changed、restated 的 ident：早的决议 id 加晚的决议 id，中间用竖线（第 2 节）。"""
    return f"{early_id}|{late_id}"


def decision_order(recording_date: str | None, created_at: str | None, start_ms: int | None, meeting_id: str) -> tuple:
    """两条决议谁在前：会议时间，同一时间按 start_ms，再按会议 id。"""
    ns = _meeting_ns(recording_date, created_at)
    return (ns if ns is not None else 0, int(start_ms or 0), str(meeting_id))


def squash(text: str | None) -> str:
    """比原话用：先照发给 AI 时的处理（llm.neutralise），再去掉所有空白。"""
    from .llm import neutralise

    return "".join(neutralise(text or "", 1_000_000).split())


def _rejected_idents(connection: Any, project_id: str, idents: Sequence[str]) -> set[str]:
    """你标过［不是一回事］的对（两类里任一类 rejected）：规则和 AI 都不再写这一对。"""
    found: set[str] = set()
    for start in range(0, len(idents), _IN_BATCH):
        part = list(idents[start : start + _IN_BATCH])
        found.update(
            row["ident"]
            for row in connection.execute(
                f"""SELECT ident FROM relations
                     WHERE project_id = ? AND kind IN ({_PAIR_KINDS_SQL}) AND status = 'rejected'
                       AND ident IN ({", ".join("?" for _ in part)})""",
                [project_id, *part],
            ).fetchall()
        )
    return found


def _clear_ids(connection: Any, ids: Sequence[int], stamp: str) -> int:
    cleared = 0
    for start in range(0, len(ids), _IN_BATCH):
        part = list(ids[start : start + _IN_BATCH])
        cleared += connection.execute(
            f"""UPDATE relations SET status = 'cleared', updated_at = ?
                 WHERE id IN ({", ".join("?" for _ in part)}) AND status = 'shown' AND origin != 'manual'""",
            [stamp, *part],
        ).rowcount
    return cleared


def link_pairs(
    connection: Any, meeting_id: str, project_id: str, meeting: Mapping[str, Any], *, material: bool, stamp: str
) -> None:
    """L1 写事务里 4c 的两步（ingest_meeting 在真写了决议或换了项目时调；快路径不调）。

    1. 这场会没了的决议上挂着的系统标记收回（任一条决议没了变 cleared）；
    2. 规则版「后来又提到」：同项目另一场会有 text_key 完全相同、至少 4 个字的还在的决议，写
       kind='restated'、origin='rule'，早的在前；这场会的规则行对不上了的收回；
    3. 实质变化时：origin='llm'、status='shown' 的对比行，这场会这一头存的原话不再是新文字的一部分的，
       立刻 cleared。你标过［不是一回事］的 rejected 行不动（这几步都只动系统行）。
    """
    from . import relations  # relations 引用 tasks，tasks 在 decisions 之前载入，这里晚一点再引

    mine_sql = "SELECT id FROM decisions WHERE meeting_id = ?"
    gone = [
        int(row["id"])
        for row in connection.execute(
            f"""SELECT id FROM relations
                 WHERE kind IN ({_PAIR_KINDS_SQL}) AND status = 'shown' AND origin != 'manual'
                   AND (decision_id IN ({mine_sql} AND gone_at IS NOT NULL)
                        OR to_decision_id IN ({mine_sql} AND gone_at IS NOT NULL))""",
            (meeting_id, meeting_id),
        ).fetchall()
    ]
    _clear_ids(connection, gone, stamp)

    mine = [
        dict(row)
        for row in connection.execute(
            "SELECT id, text, text_key, start_ms FROM decisions WHERE meeting_id = ? AND gone_at IS NULL",
            (meeting_id,),
        ).fetchall()
    ]
    keys = sorted({row["text_key"] for row in mine if len(row["text_key"] or "") >= RESTATED_MIN_CHARS})
    matches: list[dict[str, Any]] = []
    for start in range(0, len(keys), _IN_BATCH):
        part = keys[start : start + _IN_BATCH]
        matches.extend(
            dict(row)
            for row in connection.execute(
                f"""SELECT d.id, d.text, d.text_key, d.start_ms, m.id AS meeting_id, m.recording_date, m.created_at
                      FROM decisions d JOIN meetings m ON m.id = d.meeting_id
                     WHERE d.text_key IN ({", ".join("?" for _ in part)}) AND d.gone_at IS NULL
                       AND m.project_id = ? AND m.id != ?""",
                [*part, project_id, meeting_id],
            ).fetchall()
        )
    candidates: dict[str, dict[str, Any]] = {}
    for own in mine:
        own_order = decision_order(meeting["recording_date"], meeting["created_at"], own["start_ms"], meeting_id)
        for other in matches:
            if other["text_key"] != own["text_key"]:
                continue
            other_order = decision_order(other["recording_date"], other["created_at"], other["start_ms"], other["meeting_id"])
            if own_order <= other_order:
                early, late, late_meeting = own, other, other["meeting_id"]
            else:
                early, late, late_meeting = other, own, meeting_id
            ident = pair_ident(early["id"], late["id"])
            candidates[ident] = {
                "kind": "restated",
                "project_id": project_id,
                "ident": ident,
                "status": "shown",
                "origin": "rule",
                "meeting_id": late_meeting,
                "at_ms": late["start_ms"],
                "decision_id": early["id"],
                "to_decision_id": late["id"],
                "quote": late["text"],
                "evidence": {"rule": "same_text"},
            }
    rejected = _rejected_idents(connection, project_id, sorted(candidates))
    keep = [ident for ident in sorted(candidates) if ident not in rejected]
    relations.upsert_system(connection, [candidates[ident] for ident in keep], stamp, since=stamp)
    stale = [
        int(row["id"])
        for row in connection.execute(
            f"""SELECT id, ident, kind, origin FROM relations
                 WHERE project_id = ? AND kind IN ({_PAIR_KINDS_SQL}) AND status = 'shown' AND origin != 'manual'
                   AND (decision_id IN ({mine_sql}) OR to_decision_id IN ({mine_sql}))""",
            (project_id, meeting_id, meeting_id),
        ).fetchall()
        # 规则行对不上了收回；一对只留一种：规则写了「后来又提到」的，同一对的「后来改了」收回
        if (row["kind"] == "restated" and row["origin"] == "rule" and row["ident"] not in keep)
        or (row["kind"] == "later_changed" and row["ident"] in keep)
    ]
    _clear_ids(connection, stale, stamp)

    if not material:
        return
    texts = {row["id"]: row["text"] for row in mine}
    # 规则这一步刚写过的「后来又提到」不再按 AI 的原话查：AI 原来写的那一行被规则接手以后 origin 仍是
    # llm、evidence 已换成规则的，按原话查会把它收回，这一对以后就再也立不起来
    ruled = set(keep)
    mismatched: list[int] = []
    for row in connection.execute(
        f"""SELECT id, kind, ident, decision_id, to_decision_id, evidence_json FROM relations
             WHERE kind IN ({_PAIR_KINDS_SQL}) AND status = 'shown' AND origin = 'llm'
               AND (decision_id IN ({mine_sql}) OR to_decision_id IN ({mine_sql}))""",
        (meeting_id, meeting_id),
    ).fetchall():
        if row["kind"] == "restated" and row["ident"] in ruled:
            continue
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except ValueError:
            evidence = {}
        for column, key in (("decision_id", "why_earlier"), ("to_decision_id", "why_later")):
            text = texts.get(row[column])
            if text is None:
                continue
            quote = squash(str(evidence.get(key) or "")) if isinstance(evidence, dict) else ""
            if not quote or quote not in squash(text):
                mismatched.append(int(row["id"]))
                break
    _clear_ids(connection, mismatched, stamp)


# ---------------------------------------------------------------------- 4c：决议放在哪个需求下（读的时候算）

# 名字对上时不算数的片段（规格第 5 节「决议放在哪个需求下」）
PLACE_STOPWORDS = ("需求", "项目", "功能", "优化", "一期", "二期", "三期")
_HAN_RUN = re.compile(r"[一-鿿]+")
_ALNUM_RUN = re.compile(r"[0-9a-z]+")


def _fold(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def title_fragments(title: str, excluded: Sequence[str] = ()) -> set[str]:
    """需求名里拿来和决议比的片段：3 个汉字或 4 个字母数字；去掉停用词，落在项目名、也叫里的不算。"""
    folded = _fold(title)
    for word in PLACE_STOPWORDS:
        folded = folded.replace(word, " ")
    found: set[str] = set()
    for run in _HAN_RUN.findall(folded):
        found.update(run[index : index + 3] for index in range(len(run) - 2))
    for run in _ALNUM_RUN.findall(folded):
        found.update(run[index : index + 4] for index in range(len(run) - 3))
    names = [_fold(name) for name in excluded if name]
    return {piece for piece in found if not any(piece in name for name in names)}


def title_match(text: str, requirements: Sequence[Mapping[str, Any]], excluded: Sequence[str] = ()) -> str | None:
    """恰好一个需求名和决议（text 加 detail）有共同片段时返回它的 id，否则 None。"""
    body = _fold(text)
    hits = [
        requirement["id"]
        for requirement in requirements
        if any(piece in body for piece in title_fragments(requirement["title"], excluded))
    ]
    return hits[0] if len(hits) == 1 else None


def effective_requirement(
    decision: Mapping[str, Any],
    linked: Sequence[Mapping[str, Any]],
    project_id: str | None,
    excluded: Sequence[str] = (),
) -> tuple[str | None, str]:
    """(需求 id 或 None, how)。decision 要有 placement、requirement_id、requirement_project（picked 那个需求
    所在的项目）、text、detail；linked 是这场会关联的需求 [{id, title}]。how：
    picked、ai、only、title 放进那个需求；none 你说不属于具体需求；project 这场会没关联需求（项目层）；
    unplaced 关联了两个以上、名字对不上（没归到具体需求）。"""
    placement = decision.get("placement")
    chosen = decision.get("requirement_id")
    if placement == "picked" and chosen and project_id and decision.get("requirement_project") == project_id:
        return chosen, "picked"
    if placement == "ai" and chosen and any(item["id"] == chosen for item in linked):
        return chosen, "ai"
    if placement == "none":
        return None, "none"
    if not linked:
        return None, "project"
    if len(linked) == 1:
        return linked[0]["id"], "only"
    matched = title_match(f"{decision.get('text') or ''} {decision.get('detail') or ''}", linked, excluded)
    return (matched, "title") if matched else (None, "unplaced")


def project_names(name: str | None, also_names: str | None) -> list[str]:
    """项目名加也叫（projects.also_names 是 JSON 列表）。"""
    names = [name] if name else []
    try:
        extra = json.loads(also_names or "[]")
    except ValueError:
        extra = []
    if isinstance(extra, list):
        names.extend(str(item) for item in extra if item)
    return names


# ---------------------------------------------------------------------- 4c：状态句（需求卡、时间线［决议］共用）

PAIR_WAITING = "还在对比前后几场会的决议，对完会标出后来改了的"
PAIR_NO_KEY = "没配置 AI，不标哪些决议后来改了"
PAIR_BAD_KEY = "AI 的 key 不对，不标哪些决议后来改了"
PAIR_CAPPED = "今天的 AI 用量到上限了，明天接着对比"
PAIR_BALANCE = "AI 账户余额不足，不标哪些决议后来改了"
PAIR_UNREACHABLE = "AI 连不上，过一会儿自动再对比"
PAIR_FAILED = "这场会的决议没对比成"
PAIR_LLM_OFF = "后台 AI 整理关着，不标哪些决议后来改了"
PAIR_LINKS_OFF = "关联整理关着，决议按纪要现读，不标后来改了"
PAIR_SENTENCES = (
    PAIR_WAITING, PAIR_NO_KEY, PAIR_BAD_KEY, PAIR_CAPPED, PAIR_BALANCE, PAIR_UNREACHABLE, PAIR_FAILED,
    PAIR_LLM_OFF, PAIR_LINKS_OFF,
)
_PAIR_LLM_TEXTS = {
    "no_key": PAIR_NO_KEY,
    "auth": PAIR_BAD_KEY,
    "capped": PAIR_CAPPED,
    "balance": PAIR_BALANCE,
    "failing": PAIR_UNREACHABLE,
    "backoff": PAIR_UNREACHABLE,
    "network": PAIR_UNREACHABLE,
}
RETRY_ACTION = {"kind": "retry", "label": "现在重试"}


def _line(kind: str, text: str | None = None, action: Mapping[str, str] | None = None) -> dict[str, Any]:
    return {"kind": kind, "text": text, "action": dict(action) if action else None}


def pair_state_line(worker: Any, settings: Any, states: Iterable[str | None]) -> dict[str, Any]:
    """一句话，最多一个按钮。states 是这些会的 pair_state。都对比完（或没有要对比的）时不写。
    规则版「后来又提到」不用 AI，这里说的停下只关「后来改了」。worker 是 deep_links 的快照（不查库）。"""
    from .relation_read import _snapshot

    snap = _snapshot(worker)
    if not getattr(settings, "links_enabled", True) or snap.get("enabled") is False:
        return _line("stopped", PAIR_LINKS_OFF)
    seen = set(states)
    pending = bool(seen & {"pending", "running"})
    if not pending and "failed" not in seen:
        return _line("ok")
    llm_off = (
        not getattr(settings, "links_llm_enabled", True)
        or int(getattr(settings, "links_llm_daily_calls", 200)) <= 0
        or snap.get("llm") == "off"
    )
    if llm_off:
        return _line("stopped", PAIR_LLM_OFF)
    if "failed" in seen:
        return _line("stopped", PAIR_FAILED, RETRY_ACTION)
    llm_state = str(snap.get("llm") or "")
    if llm_state in _PAIR_LLM_TEXTS:
        return _line("stopped", _PAIR_LLM_TEXTS[llm_state])
    return _line("waiting", PAIR_WAITING)


# ---------------------------------------------------------------------- 4c：需求页「决议」卡


class RequirementNotFound(LookupError):
    pass


def _audio_url(audio_id: Any) -> str | None:
    return f"/api/media/{audio_id}" if audio_id else None


def _day(recording_date: str | None, created_at: str | None) -> str:
    from .graph import local_day  # graph 引用本模块，这里晚一点再引

    return local_day(recording_date, created_at).isoformat()


def _pair_rows_sql(where: str) -> str:
    """对比行连两头的决议和会（两头都还在才算数），带另一头要显示的字和录音。一条语句。"""
    from .relation_read import AUDIO_ID_SQL

    return f"""
SELECT r.id AS relation_id, r.kind, r.status, r.quote, r.decided_at, r.decision_id, r.to_decision_id,
       a.text AS a_text, a.start_ms AS a_start, a.meeting_id AS a_meeting,
       am.title AS a_title, am.recording_date AS a_rec, am.created_at AS a_created,
       {AUDIO_ID_SQL.format(meeting='am.id')} AS a_audio,
       b.text AS b_text, b.start_ms AS b_start, b.meeting_id AS b_meeting,
       bm.title AS b_title, bm.recording_date AS b_rec, bm.created_at AS b_created,
       {AUDIO_ID_SQL.format(meeting='bm.id')} AS b_audio
  FROM relations r
  JOIN decisions a ON a.id = r.decision_id AND a.gone_at IS NULL
  JOIN decisions b ON b.id = r.to_decision_id AND b.gone_at IS NULL
  JOIN meetings am ON am.id = a.meeting_id
  JOIN meetings bm ON bm.id = b.meeting_id
 WHERE r.kind IN ({_PAIR_KINDS_SQL}) AND {where}
 ORDER BY r.id"""


def _end(row: Mapping[str, Any], side: str) -> dict[str, Any]:
    """对比行的一头（a 早、b 晚）。"""
    return {
        "decision_id": row["decision_id"] if side == "a" else row["to_decision_id"],
        "text": row[f"{side}_text"],
        "start_ms": row[f"{side}_start"],
        "meeting": {
            "id": row[f"{side}_meeting"],
            "title": row[f"{side}_title"],
            "date": _day(row[f"{side}_rec"], row[f"{side}_created"]),
        },
        "audio_url": _audio_url(row[f"{side}_audio"]),
        "order": decision_order(row[f"{side}_rec"], row[f"{side}_created"], row[f"{side}_start"], row[f"{side}_meeting"]),
    }


def link_ref(row: Mapping[str, Any], side: str) -> dict[str, Any]:
    """LinkRef：{relation_id, decision_id, meeting{id, title, date}, text, start_ms, quote, audio_url}，
    side 是另一头（a 早、b 晚）。需求卡和展开一场会同一个样子。"""
    end = _end(row, side)
    return {
        "relation_id": row["relation_id"],
        "decision_id": end["decision_id"],
        "meeting": end["meeting"],
        "text": end["text"],
        "start_ms": end["start_ms"],
        "quote": row["quote"] or "",
        "audio_url": end["audio_url"],
    }


class _Groups:
    """「后来又提到」用 union-find 连成组。"""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def pair_marks(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """按决议 id 分好的标记：later、earlier、restated、dismissed。rows 是 _pair_rows_sql 读出的行。

    - restated：shown 的「后来又提到」连成一组，挂在组里最早的那条下面，每个后来的会一行；
    - later：组里任一条有指向组外的 shown 的「后来改了」，整组都算后来改了；
    - earlier：这条改了的之前的决议（shown 的「后来改了」的另一头）；
    - dismissed：你标过［不是一回事］的（rejected），过了 600 秒也能在这里改回。
    """
    groups = _Groups()
    orders: dict[str, tuple] = {}
    ends: dict[str, dict[str, Any]] = {}
    for row in rows:
        for side in ("a", "b"):
            end = _end(row, side)
            orders[end["decision_id"]] = end["order"]
            ends[end["decision_id"]] = end
        if row["kind"] == "restated" and row["status"] == "shown":
            groups.union(row["decision_id"], row["to_decision_id"])
    members: dict[str, list[str]] = {}
    for decision_id in orders:
        members.setdefault(groups.find(decision_id), []).append(decision_id)
    marks: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def slot(decision_id: str) -> dict[str, list[dict[str, Any]]]:
        return marks.setdefault(decision_id, {"later": [], "earlier": [], "restated": [], "dismissed": []})

    edge_of: dict[str, int] = {}
    group_later: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "rejected":
            for own, other in (("a", "b"), ("b", "a")):
                end = _end(row, other)
                slot(row["decision_id"] if own == "a" else row["to_decision_id"])["dismissed"].append(
                    {
                        "relation_id": row["relation_id"],
                        "kind": row["kind"],
                        "other": {"date": end["meeting"]["date"], "meeting_title": end["meeting"]["title"], "text": end["text"]},
                        "decided_at": row["decided_at"],
                    }
                )
            continue
        if row["kind"] == "restated":
            for decision_id in (row["decision_id"], row["to_decision_id"]):
                edge_of.setdefault(decision_id, row["relation_id"])
            continue
        # later_changed
        slot(row["to_decision_id"])["earlier"].append(link_ref(row, "a"))
        root = groups.find(row["decision_id"])
        if groups.find(row["to_decision_id"]) != root:
            group_later.setdefault(root, []).append(link_ref(row, "b"))
    for root, ids in members.items():
        later = group_later.get(root, [])
        seen: set[str] = set()
        unique = [ref for ref in later if not (ref["decision_id"] in seen or seen.add(ref["decision_id"]))]
        for decision_id in ids:
            if unique:
                slot(decision_id)["later"] = list(unique)
        if len(ids) < 2:
            continue
        ordered_ids = sorted(ids, key=lambda item: orders[item])
        first = ordered_ids[0]
        first_meeting = ends[first]["meeting"]["id"]
        seen_meetings = {first_meeting}
        for decision_id in ordered_ids[1:]:
            end = ends[decision_id]
            if end["meeting"]["id"] in seen_meetings:
                continue
            seen_meetings.add(end["meeting"]["id"])
            slot(first)["restated"].append(
                {
                    "relation_id": edge_of.get(decision_id),
                    "decision_id": decision_id,
                    "meeting": end["meeting"],
                    "start_ms": end["start_ms"],
                    "audio_url": end["audio_url"],
                }
            )
    return marks


_LOG_MEETINGS = """
    SELECT meeting_id FROM requirement_meetings WHERE requirement_id = :rid
    UNION SELECT meeting_id FROM decisions WHERE requirement_id = :rid AND gone_at IS NULL"""


def requirement_log(
    connection: Any, requirement_id: str, *, worker: Any = None, settings: Any = None
) -> dict[str, Any]:
    """需求页「决议」卡（GET /api/requirements/{id}/decisions），最多 6 条语句：
    1. 需求和它的项目；2. 这些会（关联这个需求的，和有决议放到这个需求的）带台账、录音，台账落后时
    带纪要原文；3. 这些会还在的决议；4. 这些会关联的需求名；5. 项目里 shown、rejected 的对比行；
    6. 这些决议在问的可能过时（4e，relation_read.decision_questions，文件这一边的说法）。
    台账落后或 links_enabled 关着时这场会按纪要现读，id 为 null，没有标记和按钮。"""
    from .relation_read import AUDIO_ID_SQL

    live = bool(getattr(settings, "links_enabled", True)) if settings is not None else True
    requirement = connection.execute(
        """SELECT r.id, r.title, r.project_id, p.name AS project_name, p.also_names
             FROM requirements r LEFT JOIN projects p ON p.id = r.project_id WHERE r.id = ?""",
        (requirement_id,),
    ).fetchone()
    if requirement is None:
        raise RequirementNotFound("需求不存在")
    project_id = requirement["project_id"]
    excluded = project_names(requirement["project_name"], requirement["also_names"])
    params = {"rid": requirement_id, "live": 1 if live else 0, "parser": PARSER_VERSION}
    meetings = [
        dict(row)
        for row in connection.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.created_at, m.project_id,
                       m.id IN (SELECT meeting_id FROM requirement_meetings WHERE requirement_id = :rid) AS linked,
                       s.note, s.pair_state,
                       (:live = 0 OR s.meeting_id IS NULL OR s.minutes_version_id IS NOT m.current_minutes_version_id
                        OR s.parser != :parser) AS behind,
                       CASE WHEN :live = 0 OR s.meeting_id IS NULL
                                 OR s.minutes_version_id IS NOT m.current_minutes_version_id OR s.parser != :parser
                            THEN mv.markdown END AS markdown,
                       {AUDIO_ID_SQL.format(meeting='m.id')} AS audio_id
                  FROM ({_LOG_MEETINGS}) ms
                  JOIN meetings m ON m.id = ms.meeting_id
                  LEFT JOIN decision_scan s ON s.meeting_id = m.id
                  LEFT JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id""",
            params,
        ).fetchall()
    ]
    decisions_by_meeting: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        f"""SELECT d.id, d.meeting_id, d.ordinal, d.text, d.detail, d.start_ms, d.end_ms, d.placement,
                   d.requirement_id, rq.project_id AS requirement_project
              FROM decisions d LEFT JOIN requirements rq ON rq.id = d.requirement_id
             WHERE d.meeting_id IN ({_LOG_MEETINGS}) AND d.gone_at IS NULL
             ORDER BY d.meeting_id, d.ordinal, d.id""",
        params,
    ).fetchall():
        decisions_by_meeting.setdefault(row["meeting_id"], []).append(dict(row))
    linked_by_meeting: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        f"""SELECT rm.meeting_id, r.id, r.title FROM requirement_meetings rm
              JOIN requirements r ON r.id = rm.requirement_id
             WHERE rm.meeting_id IN ({_LOG_MEETINGS})
             ORDER BY r.created_at, r.id""",
        params,
    ).fetchall():
        linked_by_meeting.setdefault(row["meeting_id"], []).append({"id": row["id"], "title": row["title"]})
    marks: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if live and project_id and any(not meeting["behind"] for meeting in meetings):
        rows = connection.execute(
            _pair_rows_sql("r.project_id = :pid AND r.status IN ('shown', 'rejected')"), {"pid": project_id}
        ).fetchall()
        marks = pair_marks([dict(row) for row in rows])

    groups: list[dict[str, Any]] = []
    counts = {"decisions": 0, "later_changed": 0, "unplaced": 0}
    states: list[str | None] = []
    for meeting in meetings:
        linked = linked_by_meeting.get(meeting["id"], [])
        if meeting["behind"]:
            parsed = parse_safely(meeting["markdown"])
            note = parsed.note
            items: list[dict[str, Any]] = [
                {"id": None, "text": item.text, "detail": item.detail, "start_ms": item.start_ms,
                 "end_ms": item.end_ms, "placement": None, "requirement_id": None, "requirement_project": None}
                for item in parsed.items
            ]
        else:
            note = meeting["note"]
            items = decisions_by_meeting.get(meeting["id"], [])
            states.append(meeting["pair_state"])
        placed: list[dict[str, Any]] = []
        unplaced: list[dict[str, Any]] = []
        for item in items:
            chosen, how = effective_requirement(item, linked, meeting["project_id"], excluded)
            mark = marks.get(item["id"] or "", {}) if item["id"] else {}
            entry = {
                "id": item["id"],
                "text": item["text"],
                "detail": item["detail"] or "",
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "placement": {"how": how if chosen == requirement_id else "unplaced", "requirement_id": chosen},
                "later": mark.get("later", []),
                "earlier": mark.get("earlier", []),
                "restated": mark.get("restated", []),
                "dismissed": mark.get("dismissed", []),
                # 4e：下面按决议 id 一条语句填
                "stale_files": [],
            }
            if chosen == requirement_id:
                placed.append(entry)
            elif how == "unplaced" and meeting["linked"]:
                unplaced.append(entry)
        if not meeting["linked"] and not placed:
            continue
        counts["decisions"] += len(placed)
        counts["later_changed"] += sum(1 for entry in placed if entry["later"])
        counts["unplaced"] += len(unplaced)
        groups.append(
            {
                "meeting": {
                    "id": meeting["id"],
                    "title": meeting["title"],
                    "date": _day(meeting["recording_date"], meeting["created_at"]),
                    "audio_url": _audio_url(meeting["audio_id"]),
                },
                "note": NOTE_TEXT.get(note or ""),
                "decisions": placed,
                "unplaced": unplaced,
                "_order": decision_order(meeting["recording_date"], meeting["created_at"], 0, meeting["id"]),
            }
        )
    groups.sort(key=lambda group: group["_order"], reverse=True)
    for group in groups:
        group.pop("_order")
    # 4e：在问的可能过时，「报价单 v3 之后没改过，可能过时」，一条语句
    entries = [entry for group in groups for entry in (*group["decisions"], *group["unplaced"]) if entry["id"]]
    if live and entries:
        from .relation_read import decision_questions

        stale = decision_questions(connection, [entry["id"] for entry in entries])
        for entry in entries:
            entry["stale_files"] = stale.get(entry["id"], [])
    return {
        "requirement": {"id": requirement["id"], "title": requirement["title"]},
        "counts": counts,
        "state": pair_state_line(worker, settings, states),
        "meetings": groups,
    }


# ---------------------------------------------------------------------- 4c：给 4e、4h 的读法


def superseded_sql(alias: str) -> str:
    """一条 SQL 条件：决议 {alias} 有指向还在的决议的 shown 的「后来改了」（4e 用它清掉被改过的决议的
    「可能过时」）。"""
    return f"""EXISTS (SELECT 1 FROM relations sr JOIN decisions sd ON sd.id = sr.to_decision_id
                 WHERE sr.decision_id = {alias}.id AND sr.kind = 'later_changed' AND sr.status = 'shown'
                   AND sd.gone_at IS NULL)"""


def project_decisions(
    connection: Any, project_id: str, *, include_superseded: bool = False
) -> list[dict[str, Any]]:
    """项目里还在的决议（只读，最多 2 条语句），按（决议日期，会议 id，序号）排。每条带 id、meeting_id、
    text、start_ms、按放需求的规则算出的 requirement_id（没归到的为空）、placement（how）、
    earlier[{decision_id, date, text}]、restated[{meeting_id, date, start_ms}]（挂在最早那条上）。
    默认不含被后来改掉的决议。4h 写 00 索引.md 用。"""
    rows = [
        dict(row)
        for row in connection.execute(
            """SELECT d.id, d.meeting_id, d.ordinal, d.text, d.detail, d.start_ms, d.placement, d.requirement_id,
                      rq.project_id AS requirement_project, m.recording_date, m.created_at, m.project_id,
                      p.name AS project_name, p.also_names,
                      (SELECT json_group_array(json_object('id', r.id, 'title', r.title))
                         FROM (SELECT r.id, r.title FROM requirement_meetings rm
                                 JOIN requirements r ON r.id = rm.requirement_id
                                WHERE rm.meeting_id = m.id ORDER BY r.created_at, r.id) r) AS linked_json
                 FROM meetings m
                 JOIN decisions d ON d.meeting_id = m.id AND d.gone_at IS NULL
                 LEFT JOIN requirements rq ON rq.id = d.requirement_id
                 LEFT JOIN projects p ON p.id = m.project_id
                WHERE m.project_id = ?""",
            (project_id,),
        ).fetchall()
    ]
    pairs = [
        dict(row)
        for row in connection.execute(
            _pair_rows_sql("r.project_id = :pid AND r.status = 'shown'"), {"pid": project_id}
        ).fetchall()
    ]
    marks = pair_marks(pairs)
    result: list[dict[str, Any]] = []
    for row in rows:
        mark = marks.get(row["id"], {})
        if mark.get("later") and not include_superseded:
            continue
        try:
            linked = json.loads(row["linked_json"] or "[]")
        except ValueError:
            linked = []
        chosen, how = effective_requirement(
            row, linked, row["project_id"], project_names(row["project_name"], row["also_names"])
        )
        day = _day(row["recording_date"], row["created_at"])
        result.append(
            {
                "id": row["id"],
                "meeting_id": row["meeting_id"],
                "text": row["text"],
                "start_ms": row["start_ms"],
                "requirement_id": chosen,
                "placement": how,
                "earlier": [
                    {"decision_id": ref["decision_id"], "date": ref["meeting"]["date"], "text": ref["text"]}
                    for ref in mark.get("earlier", [])
                ],
                "restated": [
                    {"meeting_id": ref["meeting"]["id"], "date": ref["meeting"]["date"], "start_ms": ref["start_ms"]}
                    for ref in mark.get("restated", [])
                ],
                "superseded": bool(mark.get("later")),
                "_sort": (day, row["meeting_id"], row["ordinal"]),
            }
        )
    result.sort(key=lambda item: item["_sort"])
    for item in result:
        item.pop("_sort")
    return result
