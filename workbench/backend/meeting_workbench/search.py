"""1e 检索：包含原词的命中（逐字稿、纪要、标题，带词典同义展开）在前，意思相近的另列。

- 同义展开：词条的正确写法、错写、也叫算一组。搜索词里出现组里的某个写法（3 字以上的写法按子串认，
  2 字的写法只在整个搜索词就是它时才认），就换成组里其他写法生成变体。3 字以上的变体自动一起搜
  （最多 5 个），2 字的只作为可点的提示，免得「随方」这种短词把不相干的句子也搜进来。
- 原词命中按会议时间倒序；同一场会最多列 3 条逐字稿命中、2 条纪要命中，免得一场会刷满一页。
- 纪要命中给命中所在那一行的摘录，播放位置取这一行里离命中最近的 [HH:MM:SS]，没有就是 None
  （前端直接打开纪要页签）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .db import Database

AUTO_LIMIT = 5
HINT_LIMIT = 5
MIN_AUTO_LEN = 3
MIN_SUBSTRING_MEMBER_LEN = 3
SEGMENTS_PER_MEETING = 3
MINUTES_PER_MEETING = 2
SNIPPET_CHARS = 120
SNIPPET_CONTEXT = 50
PUBLIC_SCOPE = "通用"
# 意思相近的另列：最多 10 条，低于这个相似度的不列（bge-small-zh 下不相干的句子一般在 0.4 上下）
SIMILAR_LIMIT = 10
SIMILAR_FETCH = 60
SIMILAR_MIN_SCORE = 0.45

# 纪要里的时间点：[00:12:34]、[12:34]，也认全角冒号
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?\]")
_LINE_MARKER_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|>\s*|\d+[.、)]\s+)")

Scope = str | None  # None 全部；"none" 只看没归项目的会；其他是项目 id


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _strip(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def _term_groups(db: Database, project_id: Scope) -> list[list[str]]:
    """每条词条一组：[正确写法, *错写, *也叫]。在项目里搜时只用本项目和公共词，本项目优先。"""
    rows = db.query_all(
        """SELECT term, aliases, also, project_id, scope
             FROM glossary_terms WHERE confirmed=1
            ORDER BY term"""
    )

    def rank(row: dict[str, Any]) -> int:
        if project_id and project_id != "none" and row["project_id"] == project_id:
            return 0
        if row["project_id"] is None:
            return 1 if row["scope"] == PUBLIC_SCOPE else 2
        return 3

    groups: list[list[str]] = []
    for row in sorted(rows, key=rank):
        own_project = row["project_id"]
        if project_id and project_id != "none" and own_project not in (None, project_id):
            continue
        members: list[str] = []
        seen: set[str] = set()
        for value in [row["term"], *_strip(json.loads(row["aliases"] or "[]")),
                      *_strip(json.loads(row["also"] or "[]"))]:
            key = fold(value.strip())
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            members.append(value.strip())
        if len(members) > 1:
            groups.append(members)
    return groups


def expand_query(db: Database, query: str, *, project_id: Scope = None) -> dict[str, list[str]]:
    """返回 {"expanded": 自动一起搜的变体, "hints": 只作为提示的 2 字变体}。"""
    base = unicodedata.normalize("NFKC", query.strip())
    folded = base.casefold()
    if not base:
        return {"expanded": [], "hints": []}
    # casefold 改了长度（如 ß）时位置对不上，只认整个搜索词
    positional = len(folded) == len(base)

    groups = _term_groups(db, project_id)
    occurrences: list[tuple[int, int, int, str]] = []
    for group_index, members in enumerate(groups):
        for member in members:
            key = fold(member)
            if key == folded:
                occurrences.append((0, len(base), group_index, member))
            elif positional and len(key) >= MIN_SUBSTRING_MEMBER_LEN:
                start = folded.find(key)
                while start != -1:
                    occurrences.append((start, start + len(key), group_index, member))
                    start = folded.find(key, start + 1)

    # 长的写法优先，重叠的短写法不再换（「数理协会」里的「数理」不单独展开）
    occurrences.sort(key=lambda item: (-(item[1] - item[0]), item[2], item[0]))
    taken: list[tuple[int, int]] = []
    chosen: list[tuple[int, int, int, str]] = []
    for start, end, group_index, member in occurrences:
        if any(start < other_end and other_start < end for other_start, other_end in taken):
            continue
        taken.append((start, end))
        chosen.append((start, end, group_index, member))

    expanded: list[str] = []
    hints: list[str] = []
    seen = {folded}
    for start, end, group_index, member in chosen:
        for other in groups[group_index]:
            if fold(other) == fold(member):
                continue
            variant = base[:start] + other + base[end:]
            key = fold(variant)
            if key in seen:
                continue
            seen.add(key)
            if len(variant) >= MIN_AUTO_LEN:
                if len(expanded) < AUTO_LIMIT:
                    expanded.append(variant)
            elif len(hints) < HINT_LIMIT:
                hints.append(variant)
    return {"expanded": expanded, "hints": hints}


def _scope_clause(scope: Scope) -> tuple[str, list[Any]]:
    if scope is None:
        return "", []
    if scope == "none":
        return " AND m.project_id IS NULL", []
    return " AND m.project_id = ?", [scope]


def _like(needle: str) -> str:
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fts_phrase(needle: str) -> str:
    return f'"{needle.replace(chr(34), chr(34) * 2)}"'


_MEETING_FIELDS = """m.title, m.canonical_dir, m.recording_date, m.created_at AS meeting_created_at,
                     m.project_id, p.name AS project_name, p.color AS project_color"""


def _segment_rows(db: Database, needle: str, scope: Scope, fetch: int) -> list[dict[str, Any]]:
    """当前逐字稿里含 needle 的段落，每场会只取前几条（按段落顺序）。"""
    clause, params = _scope_clause(scope)
    if len(needle) >= 3:
        source = """FROM segments_fts f
                    JOIN segments s ON s.id = f.segment_id
                    JOIN meetings m ON m.id = s.meeting_id
                   WHERE segments_fts MATCH ?
                     AND s.version_id = m.current_transcript_version_id"""
        match_param: Any = _fts_phrase(needle)
    else:
        source = """FROM segments s
                    JOIN meetings m ON m.id = s.meeting_id
                   WHERE s.version_id = m.current_transcript_version_id
                     AND s.text LIKE ? ESCAPE '\\'"""
        match_param = _like(needle)
    sql = f"""SELECT hit.*, p.name AS project_name, p.color AS project_color
                FROM (
                    SELECT s.id AS segment_id, s.meeting_id, m.title, m.canonical_dir,
                           m.recording_date, m.created_at AS meeting_created_at, m.project_id,
                           'segment' AS match_kind, s.start_ms, s.end_ms,
                           s.speaker_name, s.speaker_label, s.text, s.ordinal,
                           ROW_NUMBER() OVER (PARTITION BY s.meeting_id ORDER BY s.ordinal) AS nth
                      {source}{clause}
                ) hit
                LEFT JOIN projects p ON p.id = hit.project_id
               WHERE hit.nth <= ?
               ORDER BY COALESCE(hit.recording_date, hit.meeting_created_at) DESC, hit.ordinal ASC
               LIMIT ?"""
    return db.query_all(sql, [match_param, *params, SEGMENTS_PER_MEETING, fetch])


def _title_rows(db: Database, needle: str, scope: Scope, fetch: int) -> list[dict[str, Any]]:
    clause, params = _scope_clause(scope)
    return db.query_all(
        f"""SELECT s.id AS segment_id, m.id AS meeting_id, {_MEETING_FIELDS},
                   'title' AS match_kind, s.start_ms, s.end_ms,
                   s.speaker_name, s.speaker_label, s.text
              FROM meetings m
              LEFT JOIN projects p ON p.id = m.project_id
              JOIN segments s ON s.version_id = m.current_transcript_version_id
               AND s.ordinal = (
                   SELECT MIN(anchor.ordinal) FROM segments anchor
                    WHERE anchor.version_id = m.current_transcript_version_id
               )
             WHERE m.title LIKE ? ESCAPE '\\'{clause}
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC
             LIMIT ?""",
        [_like(needle), *params, fetch],
    )


def _minutes_candidates(db: Database, needle: str, scope: Scope, fetch: int) -> list[dict[str, Any]]:
    clause, params = _scope_clause(scope)
    select = f"""SELECT m.id AS meeting_id, {_MEETING_FIELDS}, mv.markdown
                   FROM meetings m
                   JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                   LEFT JOIN projects p ON p.id = m.project_id"""
    order = " ORDER BY COALESCE(m.recording_date, m.created_at) DESC LIMIT ?"
    if len(needle) >= 3:
        sql = f"""{select}
                  WHERE m.id IN (SELECT meeting_id FROM minutes_fts WHERE minutes_fts MATCH ?){clause}{order}"""
        return db.query_all(sql, [_fts_phrase(needle), *params, fetch])
    sql = f"""{select} WHERE mv.markdown LIKE ? ESCAPE '\\'{clause}{order}"""
    return db.query_all(sql, [_like(needle), *params, fetch])


def _timestamp_ms(match: re.Match[str]) -> int:
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is None:
        hours, minutes, seconds = 0, int(first), int(second)
    else:
        hours, minutes, seconds = int(first), int(second), int(third)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def minutes_line_hits(markdown: str, needle: str) -> list[dict[str, Any]]:
    """纪要里含 needle 的每一行：摘录、离命中最近的时间点（毫秒，没有就 None）、行号。"""
    key = fold(needle)
    hits: list[dict[str, Any]] = []
    in_frontmatter = False
    for line_no, line in enumerate(markdown.splitlines()):
        if line_no == 0 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
            continue
        folded = fold(line)
        position = folded.find(key) if len(folded) == len(line) else -1
        if position == -1 and key not in folded:
            continue
        if position == -1:
            position = 0
        stamps = list(_TIMESTAMP_RE.finditer(line))
        start_ms = None
        if stamps:
            nearest = min(stamps, key=lambda stamp: abs(stamp.start() - position))
            start_ms = _timestamp_ms(nearest)
        hits.append({"line": line_no, "text": _snippet(line, needle), "start_ms": start_ms})
    return hits


def _snippet(line: str, needle: str) -> str:
    text = re.sub(r"\s*" + _TIMESTAMP_RE.pattern, "", _LINE_MARKER_RE.sub("", line))
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= SNIPPET_CHARS:
        return text
    position = fold(text).find(fold(needle)) if len(fold(text)) == len(text) else -1
    if position == -1:
        return text[:SNIPPET_CHARS] + "…"
    start = max(0, position - SNIPPET_CONTEXT)
    end = min(len(text), start + SNIPPET_CHARS)
    start = max(0, end - SNIPPET_CHARS)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _public_item(row: dict[str, Any], matched: str) -> dict[str, Any]:
    return {
        "segment_id": row.get("segment_id"),
        "meeting_id": row["meeting_id"],
        "title": row["title"],
        "canonical_dir": row.get("canonical_dir"),
        "recording_date": row.get("recording_date"),
        "match_kind": row["match_kind"],
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "speaker_name": row.get("speaker_name"),
        "speaker_label": row.get("speaker_label"),
        "text": row["text"],
        "matched": matched,
        "project_id": row.get("project_id"),
        "project_name": row.get("project_name"),
        "project_color": row.get("project_color"),
    }


def literal_search(
    db: Database, needles: list[str], *, scope: Scope = None, limit: int = 30
) -> list[dict[str, Any]]:
    """每个写法在逐字稿、纪要、标题里搜，按会议时间倒序合并；同一场会有条数上限。"""
    fetch = max(limit * 2, 40)
    meetings: dict[str, dict[str, Any]] = {}

    def bucket(row: dict[str, Any]) -> dict[str, Any]:
        entry = meetings.get(row["meeting_id"])
        if entry is None:
            entry = {
                "sort": row.get("recording_date") or row.get("meeting_created_at") or "",
                "title": None,
                "minutes": [],
                "segments": [],
                "minutes_lines": set(),
                "segment_ids": set(),
            }
            meetings[row["meeting_id"]] = entry
        return entry

    for needle in needles:
        if not needle.strip():
            continue
        for row in _title_rows(db, needle, scope, fetch):
            entry = bucket(row)
            if entry["title"] is None:
                entry["title"] = _public_item(row, needle)
        for row in _minutes_candidates(db, needle, scope, fetch):
            entry = bucket(row)
            for hit in minutes_line_hits(row["markdown"] or "", needle):
                if len(entry["minutes"]) >= MINUTES_PER_MEETING:
                    break
                if hit["line"] in entry["minutes_lines"]:
                    continue
                entry["minutes_lines"].add(hit["line"])
                entry["minutes"].append(
                    _public_item(
                        {
                            **row,
                            "segment_id": None,
                            "match_kind": "minutes",
                            "start_ms": hit["start_ms"],
                            "end_ms": None,
                            "text": hit["text"],
                        },
                        needle,
                    )
                )
        for row in _segment_rows(db, needle, scope, fetch):
            entry = bucket(row)
            if row["segment_id"] in entry["segment_ids"]:
                continue
            if len(entry["segments"]) >= SEGMENTS_PER_MEETING:
                continue
            entry["segment_ids"].add(row["segment_id"])
            entry["segments"].append((row["ordinal"], _public_item(row, needle)))

    items: list[dict[str, Any]] = []
    for _meeting_id, entry in sorted(meetings.items(), key=lambda pair: pair[1]["sort"], reverse=True):
        ordered = [entry["title"]] if entry["title"] else []
        ordered += entry["minutes"]
        ordered += [item for _ordinal, item in sorted(entry["segments"], key=lambda pair: pair[0])]
        for item in ordered:
            if len(items) >= limit:
                return items
            items.append(item)
    return items

