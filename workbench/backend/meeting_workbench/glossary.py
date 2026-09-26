"""术语词典库：权威写法 + 错写别名。

纪要生成时给 LLM 注入快照里的术语对照表；编辑纪要时用 diff 捕获错字更正，
写入待确认队列，确认后反写进词典，越用越准。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .db import Database, utc_now

SNAPSHOT_FILENAME = "glossary-snapshot.json"
SNAPSHOT_SCHEMA_VERSION = 1

# 分类字典：与表结构契约一致
CATEGORIES = ("人名", "机构", "术语", "药品", "地名", "其他")

# diff 提取只认 2-8 字的短片段（错写更正几乎都发生在词级）
MIN_DIFF_LEN = 2
MAX_DIFF_LEN = 8
# 手动录入的权威写法允许更长（机构全称等），别名仍限 2-8 字
MAX_TERM_LEN = 40
# 2 字片段沿前后共同的文字扩成整词：遇到标点、常见虚词或人称就停，整词最长 6 字。
# 停得早只会让整词短一点，用户还有「只记 2 字」兜底。
EXPAND_MAX_LEN = 6
# 向左扩只补 2 个字（凑成最常见的 4 字词），左边没有词边界可依，扩多了容易带上前一个词
EXPAND_LEFT_MAX = 2
# 旧纪要和逐字稿里都没有重复可依时，向右只扩到 4 字
EXPAND_DEFAULT_LEN = 4
_EXPAND_STOP_CHARS = frozenset(
    "的地得了着过是在和与及或把被就也都还又吗呢吧啊"
    "说去来到给让对向从跟请我你他她它这那们个"
)

# 确认一条建议时记到哪：auto=按确认那一刻会议所属的项目；public=公共；其余当 project_id
TARGET_AUTO = "auto"
TARGET_PUBLIC = "public"


# 「也叫」：不改写，只用于识别项目和搜索；规则与项目的叫法一致
ALSO_MIN_LEN = 2
ALSO_MAX_LEN = 20
PUBLIC_SCOPE = "通用"


class GlossaryError(ValueError):
    pass


class DuplicateTermError(GlossaryError):
    """正确写法已是另一条词条：带上那条词条在哪、有哪些错写，前端就地给「加到那条」。"""

    def __init__(self, message: str, conflict: dict[str, Any]):
        super().__init__(message)
        self.conflict = conflict


def validate_term_text(
    value: str, *, what: str, min_len: int = 1, max_len: int = MAX_TERM_LEN
) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise GlossaryError(f"{what} 不能为空")
    if len(normalized) < min_len:
        raise GlossaryError(f"{what} 长度不能少于 {min_len} 个字符")
    if len(normalized) > max_len:
        raise GlossaryError(f"{what} 长度不能超过 {max_len} 个字符")
    if re.fullmatch(r"\d+", normalized):
        raise GlossaryError(f"{what} 不能是纯数字")
    if not re.search(r"[A-Za-z一-鿿]", normalized):
        raise GlossaryError(f"{what} 必须包含中文或字母")
    return normalized


def normalize_aliases(aliases: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in aliases:
        if not isinstance(value, str):
            raise GlossaryError("别名必须是字符串")
        alias = validate_term_text(
            value, what="别名", min_len=MIN_DIFF_LEN, max_len=MAX_DIFF_LEN
        )
        if alias not in seen:
            seen.add(alias)
            normalized.append(alias)
    return normalized


def normalize_also(values: list[str], *, term: str | None = None) -> list[str]:
    """「也叫」：2–20 字，不能纯数字，必须含中文或字母，不能是太常见的词。"""
    from .project_profile import GENERIC_FOLDER_NAMES

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise GlossaryError("叫法必须是字符串")
        text = unicodedata.normalize("NFKC", value).strip()
        if (
            not ALSO_MIN_LEN <= len(text) <= ALSO_MAX_LEN
            or text.isdigit()
            or not re.search(r"[A-Za-z一-鿿]", text)
        ):
            raise GlossaryError("叫法要 2–20 个字，不能是纯数字")
        if text in GENERIC_FOLDER_NAMES:
            raise GlossaryError(f"「{text}」太常见，不能当叫法")
        if text == term or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _term_conflict(db: Database, term_row: dict[str, Any]) -> dict[str, Any]:
    project = (
        db.query_one("SELECT name FROM projects WHERE id=?", (term_row["project_id"],))
        if term_row.get("project_id")
        else None
    )
    return {
        "term_id": term_row["id"],
        "term": term_row["term"],
        "project_id": term_row.get("project_id"),
        "project_name": project["name"] if project else None,
        "aliases": json.loads(term_row.get("aliases") or "[]"),
        "also": json.loads(term_row.get("also") or "[]"),
    }


def _raise_duplicate(db: Database, term: str) -> None:
    row = db.query_one("SELECT * FROM glossary_terms WHERE term=?", (term,))
    if row is None:
        return
    conflict = _term_conflict(db, row)
    where = f"{conflict['project_name']} 项目" if conflict["project_name"] else "公共 词典"
    raise DuplicateTermError(f"「{term}」已在 {where}", conflict)


def _check_names(
    db: Database,
    *,
    term: str,
    aliases: list[str],
    also: list[str],
    exclude_term_id: str | None,
    check_others: bool = True,
) -> None:
    """错写和叫法不能打架：同一个词不能既要改掉又不改；也不能是别的词条的写法、错写或叫法。"""
    both = set(aliases) & set(also)
    if both:
        raise GlossaryError(f"「{sorted(both)[0]}」不能既是错写又是叫法")
    if term in aliases:
        raise GlossaryError("错写不能和正确写法相同")
    if not also or not check_others:
        return
    for row in db.query_all("SELECT id, term, aliases, also FROM glossary_terms"):
        if row["id"] == exclude_term_id:
            continue
        taken = {row["term"], *json.loads(row["aliases"] or "[]"), *json.loads(row["also"] or "[]")}
        for name in also:
            if name in taken:
                raise GlossaryError(f"「{name}」已经用在词条「{row['term']}」上了")


# —— diff 反写：疑似错字更正提取 ——


def _is_candidate(text: str) -> bool:
    """2-8 字的中文/字母词，非纯数字、非纯标点。"""
    if not (MIN_DIFF_LEN <= len(text) <= MAX_DIFF_LEN):
        return False
    normalized = unicodedata.normalize("NFKC", text)
    if not re.fullmatch(r"\w+", normalized):
        return False
    if re.fullmatch(r"\d+", normalized):
        return False
    return bool(re.search(r"[A-Za-z一-鿿]", normalized))


def _punct_only(a: str, b: str) -> bool:
    """去掉标点后相同但原文不同 → 纯标点修正。"""
    return a != b and re.sub(r"[\W_]+", "", a) == re.sub(r"[\W_]+", "", b)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离；中文按字符计。"""
    if a == b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for row, ca in enumerate(a, 1):
        current = [row] + [0] * len(b)
        for col, cb in enumerate(b, 1):
            current[col] = min(
                previous[col] + 1,
                current[col - 1] + 1,
                previous[col - 1] + (ca != cb),
            )
        previous = current
    return previous[-1]


def _covering_known_term(
    text: str, start: int, end: int, known_terms: set[str]
) -> str | None:
    """找 text 中覆盖 [start, end) 的最长已知权威词；没有则 None。"""
    best: str | None = None
    for term in known_terms:
        offset = text.find(term)
        while offset != -1:
            if offset <= start and offset + len(term) >= end:
                if best is None or len(term) > len(best):
                    best = term
            offset = text.find(term, offset + 1)
    return best


def _correction_spans(
    old_text: str, new_text: str, known_terms: set[str], *, short_into_known: bool = False
) -> list[tuple[int, int, int, int]]:
    """diff 出疑似错字更正的片段位置 [(a_start, a_end, b_start, b_end), ...]。

    捕获规则：replace 块两段均为 2-8 字中文/字母词，且编辑距离 ≤2（同音/形近），
    或 correct 是已知权威写法（用户在往正确写法改）。
    排除：纯标点修正、两个已知权威词之间的实质替换（字符级 diff 会把整词替换拆成
    小块，所以用「已知词覆盖」判断：候选块两侧各自落在某个权威词里即为内容变更）。
    short_into_known=True 时，改一个字、改完落在某个已知词里的（随方→随访）也算，
    由调用方扩成整词。
    """
    spans: list[tuple[int, int, int, int]] = []
    matcher = difflib.SequenceMatcher(None, old_text, new_text)
    for operation, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if operation != "replace":
            continue
        wrong = old_text[a_start:a_end]
        correct = new_text[b_start:b_end]
        if (
            short_into_known
            and len(wrong) < MIN_DIFF_LEN
            and len(correct) < MIN_DIFF_LEN
            and _expandable(wrong)
            and _expandable(correct)
            and not wrong.isdigit()
            and not correct.isdigit()
            and _covering_known_term(new_text, b_start, b_end, known_terms)
            and not _covering_known_term(old_text, a_start, a_end, known_terms)
        ):
            spans.append((a_start, a_end, b_start, b_end))
            continue
        if not _is_candidate(wrong) or not _is_candidate(correct):
            continue
        if wrong == correct or _punct_only(wrong, correct):
            continue
        if _covering_known_term(
            old_text, a_start, a_end, known_terms
        ) and _covering_known_term(new_text, b_start, b_end, known_terms):
            # 两个权威写法之间的实质替换（如把数理协会整体改成数学学会），不是错字更正
            continue
        corrected_to_known = correct in known_terms
        if corrected_to_known and wrong in known_terms:
            continue
        if _edit_distance(wrong, correct) > 2 and not corrected_to_known:
            continue
        spans.append((a_start, a_end, b_start, b_end))
    return spans


def extract_corrections(
    old_text: str, new_text: str, known_terms: set[str]
) -> list[tuple[str, str]]:
    """从新旧文本 diff 提取疑似错字更正，返回 [(wrong, correct), ...]（已去重，不扩词）。"""
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a_start, a_end, b_start, b_end in _correction_spans(old_text, new_text, known_terms):
        pair = (old_text[a_start:a_end], new_text[b_start:b_end])
        if pair not in seen:
            seen.add(pair)
            candidates.append(pair)
    return candidates


def _expandable(char: str) -> bool:
    normalized = unicodedata.normalize("NFKC", char)
    return bool(re.fullmatch(r"\w", normalized)) and char not in _EXPAND_STOP_CHARS


def _expand_to_known_term(
    old_text: str, new_text: str, span: tuple[int, int, int, int], known_terms: set[str]
) -> tuple[str, str] | None:
    """改后的片段落在某个已知词里、旧文本同位置的前后文也一样时，扩成「整条错写 → 已知词」。"""
    a_start, a_end, b_start, b_end = span
    best: tuple[str, str] | None = None
    for term in known_terms:
        if len(term) > MAX_DIFF_LEN:
            continue
        offset = new_text.find(term)
        while offset != -1:
            if offset <= b_start and offset + len(term) >= b_end:
                left = new_text[offset:b_start]
                right = new_text[b_end : offset + len(term)]
                if (
                    old_text[max(0, a_start - len(left)) : a_start] == left
                    and old_text[a_end : a_end + len(right)] == right
                ):
                    wrong = left + old_text[a_start:a_end] + right
                    if MIN_DIFF_LEN <= len(wrong) <= MAX_DIFF_LEN and (
                        best is None or len(term) > len(best[1])
                    ):
                        best = (wrong, term)
            offset = new_text.find(term, offset + 1)
    return best


def _expand_short_fragment(
    old_text: str, new_text: str, span: tuple[int, int, int, int], corpus: str = ""
) -> tuple[str, str] | None:
    """2 字片段扩成整词：沿新旧文本共同的前后文找候选（遇标点、虚词停，整词最长 6 字）。

    没有词表可查词边界，就看旧纪要和逐字稿里哪个扩法重复出现得多（同一个错写，
    转写里每提到一次就错一次）：取出现 2 次以上里最多、同样多取最长的。
    没有重复可依时，向右扩到 4 字；右边扩不出再向左补 2 字。
    """
    a_start, a_end, b_start, b_end = span
    wrong = old_text[a_start:a_end]
    correct = new_text[b_start:b_end]
    budget = EXPAND_MAX_LEN - max(len(wrong), len(correct))
    right: list[str] = []
    i, j = a_end, b_end
    while (
        len(right) < budget
        and i < len(old_text)
        and j < len(new_text)
        and old_text[i] == new_text[j]
        and _expandable(old_text[i])
    ):
        right.append(old_text[i])
        i += 1
        j += 1
    left: list[str] = []
    i, j = a_start - 1, b_start - 1
    while (
        len(left) < min(budget, EXPAND_LEFT_MAX)
        and i >= 0
        and j >= 0
        and old_text[i] == new_text[j]
        and _expandable(old_text[i])
    ):
        left.append(old_text[i])
        i -= 1
        j -= 1
    head = "".join(reversed(left))
    tail = "".join(right)
    options = [(wrong + tail[:n], correct + tail[:n]) for n in range(1, len(tail) + 1)]
    options += [(head[-n:] + wrong, head[-n:] + correct) for n in range(1, len(head) + 1)]
    if not options:
        return None
    haystack = f"{old_text}\n{corpus}"
    counted = [(haystack.count(option[0]), len(option[0]), option) for option in options]
    repeated = [item for item in counted if item[0] >= 2]
    if repeated:
        return max(repeated, key=lambda item: (item[0], item[1]))[2]
    if tail:
        keep = max(1, EXPAND_DEFAULT_LEN - len(wrong))
        return wrong + tail[:keep], correct + tail[:keep]
    return head + wrong, head + correct


def extract_correction_candidates(
    old_text: str, new_text: str, known_terms: set[str], corpus: str = ""
) -> list[dict[str, str | None]]:
    """带扩词的错字更正候选：[{wrong, correct, alt_wrong, alt_correct}, ...]。

    改后的片段落在某个已知词里，就直接记成「整条错写 → 这个词」；否则 2 字片段按前后文
    扩成整词。扩出来的整词作为默认建议，原来的片段放进 alt_*（「只记 2 字」）。
    """
    candidates: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for span in _correction_spans(old_text, new_text, known_terms, short_into_known=True):
        a_start, a_end, b_start, b_end = span
        raw = (old_text[a_start:a_end], new_text[b_start:b_end])
        expanded = _expand_to_known_term(old_text, new_text, span, known_terms)
        if expanded is None and len(raw[0]) < MIN_DIFF_LEN:
            # 单字改动只在能扩成已知词时才算
            continue
        if expanded is None and len(raw[0]) == MIN_DIFF_LEN and len(raw[1]) == MIN_DIFF_LEN:
            expanded = _expand_short_fragment(old_text, new_text, span, corpus)
        if expanded is not None and (
            expanded == raw or not _is_candidate(expanded[0]) or not _is_candidate(expanded[1])
        ):
            expanded = None
        pair = expanded or raw
        if pair in seen:
            continue
        seen.add(pair)
        # 单字那对不能单独记（错写至少 2 字），就不给「只记 2 字」
        keep_alt = expanded is not None and len(raw[0]) >= MIN_DIFF_LEN
        candidates.append(
            {
                "wrong": pair[0],
                "correct": pair[1],
                "alt_wrong": raw[0] if keep_alt else None,
                "alt_correct": raw[1] if keep_alt else None,
            }
        )
    return candidates


def _context_snippet(text: str, needle: str, width: int = 30) -> str:
    """改对的那句话：去掉 markdown 标记和时间戳，取包含它的那一句（太长就截两头）。"""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*)", "", line)
        line = re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]", "", line).strip()
        if line:
            lines.append(line)
    sentences = [part.strip() for part in re.split(r"[。！？；!?;\n]", "\n".join(lines)) if part.strip()]
    for sentence in sentences:
        index = sentence.find(needle)
        if index < 0:
            continue
        if len(sentence) <= len(needle) + 2 * width:
            return sentence
        start = max(0, index - width)
        return sentence[start : index + len(needle) + width]
    return text[: 2 * width]


def _wrong_is_known_name(db: Database, wrong: str) -> bool:
    """错写本身已是某个词条、某条「也叫」，或某个项目的名称、叫法——这种不自动记。"""
    for row in db.query_all("SELECT term, also FROM glossary_terms"):
        if row["term"] == wrong or wrong in json.loads(row["also"] or "[]"):
            return True
    # 延迟导入：project_profile 依赖较重，词典模块保持轻量
    from .project_names import also_entries
    from .project_profile import norm_key

    key = norm_key(wrong)
    for row in db.query_all("SELECT name, also_names FROM projects"):
        if norm_key(row["name"]) == key:
            return True
        if any(norm_key(entry["name"]) == key for entry in also_entries(row["also_names"])):
            return True
    return False


def record_corrections_from_diff(
    db: Database,
    old_markdown: str | None,
    new_markdown: str | None,
    *,
    meeting_id: str,
    scope: str = "通用",
    snapshot_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """把新旧纪要 markdown 的疑似错字更正写入待确认队列，返回新建的建议行。

    同时满足三个条件就直接记入（返回行的 auto_recorded 为 True，前端提示可撤销）：
    改成的写法已是某个词条；错写不是任何词条、也叫或项目叫法；会议已有项目。
    """
    if not old_markdown or not new_markdown or old_markdown == new_markdown:
        return []
    known_terms = {row["term"] for row in db.query_all("SELECT term FROM glossary_terms")}
    # 逐字稿里错写重复出现的次数，帮着判断 2 字片段该扩成哪个整词
    transcript = "\n".join(
        row["text"]
        for row in db.query_all(
            """SELECT s.text FROM segments s
                 JOIN meetings m ON m.current_transcript_version_id = s.version_id
                WHERE m.id=? ORDER BY s.ordinal""",
            (meeting_id,),
        )
    )
    candidates = extract_correction_candidates(
        old_markdown, new_markdown, known_terms, corpus=transcript
    )
    scope = str(scope or "").strip() or "通用"
    meeting = db.query_one("SELECT project_id FROM meetings WHERE id=?", (meeting_id,))
    has_project = bool(meeting and meeting["project_id"])
    created: list[tuple[str, bool]] = []
    for candidate in candidates:
        suggestion_id = add_suggestion(
            db,
            wrong=str(candidate["wrong"]),
            correct=str(candidate["correct"]),
            scope=scope,
            meeting_id=meeting_id,
            context=_context_snippet(new_markdown, str(candidate["correct"])),
            alt_wrong=candidate["alt_wrong"],
            alt_correct=candidate["alt_correct"],
        )
        if not suggestion_id:
            continue
        auto = (
            has_project
            and candidate["correct"] in known_terms
            and not _wrong_is_known_name(db, str(candidate["wrong"]))
            and confirm_suggestion(db, suggestion_id) is not None
        )
        created.append((suggestion_id, auto))
    if any(auto for _, auto in created) and snapshot_path is not None:
        rewrite_snapshot(db, snapshot_path)
    rows = {row["id"]: row for row in list_suggestions(db, ids=[sid for sid, _ in created])}
    return [
        {**rows[suggestion_id], "auto_recorded": auto}
        for suggestion_id, auto in created
        if suggestion_id in rows
    ]


# —— glossary_suggestions：待确认反写队列 ——


def add_suggestion(
    db: Database,
    *,
    wrong: str,
    correct: str,
    scope: str = "通用",
    meeting_id: str | None = None,
    context: str | None = None,
    alt_wrong: str | None = None,
    alt_correct: str | None = None,
) -> str | None:
    """排进待确认队列，返回新建的建议 id；重复或已在词典里时返回 None。"""
    wrong = validate_term_text(wrong, what="错写", min_len=MIN_DIFF_LEN, max_len=MAX_DIFF_LEN)
    correct = validate_term_text(
        correct, what="正确写法", min_len=MIN_DIFF_LEN, max_len=MAX_DIFF_LEN
    )
    if wrong == correct:
        return None
    # 同一 (wrong, correct) 无论处于哪个状态都不重复建
    if db.query_one(
        "SELECT 1 AS present FROM glossary_suggestions WHERE wrong=? AND correct=?",
        (wrong, correct),
    ):
        return None
    # 该映射已入词典（correct 的 aliases 已含 wrong）则不再排队；扩词前的 2 字那对也算
    pairs = [(wrong, correct)]
    if alt_wrong and alt_correct:
        pairs.append((alt_wrong, alt_correct))
    for pair_wrong, pair_correct in pairs:
        term = db.query_one("SELECT aliases FROM glossary_terms WHERE term=?", (pair_correct,))
        if term and pair_wrong in json.loads(term["aliases"] or "[]"):
            return None
    now = utc_now()
    suggestion_id = f"gs-{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO glossary_suggestions
           (id, wrong, correct, scope, meeting_id, context, status, alt_wrong, alt_correct,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (
            suggestion_id,
            wrong,
            correct,
            scope,
            meeting_id,
            context,
            alt_wrong if alt_wrong and alt_correct else None,
            alt_correct if alt_wrong and alt_correct else None,
            now,
            now,
        ),
    )
    return suggestion_id


_SUGGESTION_SELECT = """
    SELECT s.*,
           m.title AS meeting_title,
           m.project_id AS target_project_id,
           p.name AS target_project_name,
           p.color AS target_project_color,
           t.id AS existing_term_id,
           t.project_id AS existing_term_project_id,
           tp.name AS existing_term_project_name
      FROM glossary_suggestions s
      LEFT JOIN meetings m ON m.id = s.meeting_id
      LEFT JOIN projects p ON p.id = m.project_id
      LEFT JOIN glossary_terms t ON t.term = s.correct
      LEFT JOIN projects tp ON tp.id = t.project_id
"""


def list_suggestions(
    db: Database, status: str | None = None, *, ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """待确认队列；每行带会议当前的项目（默认记到哪）和同名词条已在哪。"""
    conditions: list[str] = []
    params: list[Any] = []
    if status is not None:
        conditions.append("s.status=?")
        params.append(status)
    if ids is not None:
        if not ids:
            return []
        conditions.append(f"s.id IN ({', '.join('?' for _ in ids)})")
        params.extend(ids)
    query = _SUGGESTION_SELECT
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY s.created_at DESC, s.id DESC"
    return db.query_all(query, params)


def get_suggestion(db: Database, suggestion_id: str) -> dict[str, Any] | None:
    rows = list_suggestions(db, ids=[suggestion_id])
    return rows[0] if rows else None


def _resolve_target(
    connection: Any, target: str | None, meeting_id: str | None
) -> tuple[str | None, str]:
    """确认目标 → (project_id, scope)。auto 按确认那一刻会议所属的项目，没有项目就记公共。"""
    target = target or TARGET_AUTO
    if target == TARGET_PUBLIC:
        return None, "通用"
    if target == TARGET_AUTO:
        row = (
            connection.execute(
                """SELECT p.id, p.name FROM meetings m
                   JOIN projects p ON p.id = m.project_id WHERE m.id=?""",
                (meeting_id,),
            ).fetchone()
            if meeting_id
            else None
        )
        return (row["id"], row["name"]) if row else (None, "通用")
    row = connection.execute("SELECT id, name FROM projects WHERE id=?", (target,)).fetchone()
    if row is None:
        raise GlossaryError(f"项目不存在：{target}")
    return row["id"], row["name"]


def confirm_suggestion(
    db: Database,
    suggestion_id: str,
    snapshot_path: Path | str | None = None,
    *,
    target: str | None = TARGET_AUTO,
    short: bool = False,
) -> dict[str, Any] | None:
    """确认一条反写建议：错写并入正确写法的 aliases，必要时新建权威词条。

    target 决定新词条记到哪（auto / public / project_id）；同名词条已存在时照旧把错写
    加进那条，不挪它的归属。short=True 时记扩词前的 2 字那一对。
    返回 {term, created, wrong, correct}；建议不存在或已处理时返回 None。
    """
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT wrong, correct, scope, meeting_id, alt_wrong, alt_correct
                 FROM glossary_suggestions WHERE id=? AND status='pending'""",
            (suggestion_id,),
        ).fetchone()
        if not row:
            return None
        wrong, correct = row["wrong"], row["correct"]
        if short and row["alt_wrong"] and row["alt_correct"]:
            wrong, correct = row["alt_wrong"], row["alt_correct"]
        now = utc_now()
        existing = connection.execute(
            "SELECT id, aliases FROM glossary_terms WHERE term=?", (correct,)
        ).fetchone()
        created = False
        recorded_wrong: str | None = None
        if existing:
            term_id = existing["id"]
            aliases = json.loads(existing["aliases"] or "[]")
            if wrong not in aliases:
                aliases.append(wrong)
                recorded_wrong = wrong
                connection.execute(
                    "UPDATE glossary_terms SET aliases=?, updated_at=? WHERE id=?",
                    (json.dumps(aliases, ensure_ascii=False), now, term_id),
                )
        else:
            project_id, scope = _resolve_target(connection, target, row["meeting_id"])
            term_id = f"gt-{uuid.uuid4().hex}"
            created = True
            recorded_wrong = wrong
            connection.execute(
                """INSERT INTO glossary_terms
                   (id, term, aliases, scope, category, source, confirmed, hit_count,
                    project_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, '其他', 'auto', 1, 0, ?, ?, ?)""",
                (
                    term_id,
                    correct,
                    json.dumps([wrong], ensure_ascii=False),
                    scope,
                    project_id,
                    now,
                    now,
                ),
            )
        connection.execute(
            """UPDATE glossary_suggestions
                  SET status='confirmed', confirmed_term_id=?, confirmed_wrong=?, updated_at=?
                WHERE id=?""",
            (term_id, recorded_wrong, now, suggestion_id),
        )
    if snapshot_path is not None:
        rewrite_snapshot(db, snapshot_path)
    term = db.query_one(
        """SELECT g.*, p.name AS project_name, p.color AS project_color
             FROM glossary_terms g LEFT JOIN projects p ON p.id = g.project_id
            WHERE g.id=?""",
        (term_id,),
    )
    return {
        "term": _decode_aliases(term) if term else None,
        "created": created,
        "wrong": wrong,
        "correct": correct,
    }


def undo_confirm_suggestion(
    db: Database, suggestion_id: str, snapshot_path: Path | str | None = None
) -> bool:
    """撤销确认：把记进去的错写从词条里拿掉；自动建的词条拿空了就整条删掉；建议回到待确认。"""
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT wrong, correct, confirmed_term_id, confirmed_wrong
                 FROM glossary_suggestions WHERE id=? AND status='confirmed'""",
            (suggestion_id,),
        ).fetchone()
        if not row:
            return False
        now = utc_now()
        if row["confirmed_term_id"]:
            term = connection.execute(
                "SELECT * FROM glossary_terms WHERE id=?", (row["confirmed_term_id"],)
            ).fetchone()
            wrong = row["confirmed_wrong"]
        else:
            # 旧版本确认的建议没记落点：按正确写法找词条，错写当时一定是新加进去的
            term = connection.execute(
                "SELECT * FROM glossary_terms WHERE term=?", (row["correct"],)
            ).fetchone()
            wrong = row["wrong"]
        if term is not None and wrong:
            aliases = [alias for alias in json.loads(term["aliases"] or "[]") if alias != wrong]
            also = json.loads(term["also"] or "[]") if "also" in term.keys() else []
            if term["source"] == "auto" and not aliases and not also:
                connection.execute("DELETE FROM glossary_terms WHERE id=?", (term["id"],))
            else:
                connection.execute(
                    "UPDATE glossary_terms SET aliases=?, updated_at=? WHERE id=?",
                    (json.dumps(aliases, ensure_ascii=False), now, term["id"]),
                )
        connection.execute(
            """UPDATE glossary_suggestions
                  SET status='pending', confirmed_term_id=NULL, confirmed_wrong=NULL,
                      updated_at=?
                WHERE id=?""",
            (now, suggestion_id),
        )
    if snapshot_path is not None:
        rewrite_snapshot(db, snapshot_path)
    return True


def reject_suggestion(db: Database, suggestion_id: str) -> bool:
    now = utc_now()
    return (
        db.execute_rowcount(
            """UPDATE glossary_suggestions
               SET status='rejected', updated_at=?
               WHERE id=? AND status='pending'""",
            (now, suggestion_id),
        )
        == 1
    )


def restore_suggestion(db: Database, suggestion_id: str) -> bool:
    """已驳回的建议恢复成待确认。"""
    now = utc_now()
    return (
        db.execute_rowcount(
            """UPDATE glossary_suggestions
               SET status='pending', updated_at=?
               WHERE id=? AND status='rejected'""",
            (now, suggestion_id),
        )
        == 1
    )


# —— glossary_terms：权威词典 CRUD ——


def _decode_aliases(row: dict[str, Any]) -> dict[str, Any]:
    row["aliases"] = json.loads(row["aliases"] or "[]")
    if "also" in row:
        row["also"] = json.loads(row["also"] or "[]")
    if "is_cue" in row:
        row["is_cue"] = bool(row["is_cue"])
    return row


def list_terms(
    db: Database, scope: str | None = None, project_id: str | None = None
) -> list[dict[str, Any]]:
    query = """SELECT g.*, p.name AS project_name, p.color AS project_color
                 FROM glossary_terms g
                 LEFT JOIN projects p ON p.id = g.project_id"""
    conditions: list[str] = []
    params: list[Any] = []
    if scope is not None:
        conditions.append("g.scope=?")
        params.append(scope)
    if project_id is not None:
        if project_id == "none":
            conditions.append("g.project_id IS NULL")
        else:
            conditions.append("g.project_id=?")
            params.append(project_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    rows = db.query_all(query, params)
    # 与快照同序：人名 > 中文词 > 英文缩写，界面显示与注入截断一致
    rows.sort(key=_snapshot_sort_key)
    return [_decode_aliases(row) for row in rows]


def list_scopes(db: Database) -> list[dict[str, Any]]:
    """筛选器分组：公共 → 全部项目（含 0 个词的，按最近一场会的录音日期排）→ 旧分组桶。

    旧分组桶只在库里还有没挂项目、也不是公共的词条时出现（整理旧分组被撤销的情况）。
    """
    counts = {
        (row["project_id"], row["scope"]): row["count"]
        for row in db.query_all(
            """SELECT project_id, CASE WHEN project_id IS NULL THEN scope END AS scope,
                      COUNT(*) AS count
                 FROM glossary_terms GROUP BY 1, 2"""
        )
    }
    general = [
        {
            "kind": "general",
            "key": PUBLIC_SCOPE,
            "label": "公共",
            "color": None,
            "count": counts.get((None, PUBLIC_SCOPE), 0),
        }
    ]
    projects = [
        {
            "kind": "project",
            "key": row["id"],
            "label": row["name"],
            "color": row["color"],
            "count": counts.get((row["id"], None), 0),
            "last_meeting_at": row["last_meeting_at"],
        }
        for row in db.query_all(
            """SELECT p.id, p.name, p.color,
                      MAX(COALESCE(m.recording_date, m.created_at)) AS last_meeting_at
                 FROM projects p LEFT JOIN meetings m ON m.project_id = p.id
                GROUP BY p.id"""
        )
    ]
    # 最近开过会的在前；没开过会的按名字排在最后
    projects.sort(key=lambda item: item["label"])
    projects.sort(key=lambda item: item["last_meeting_at"] or "", reverse=True)
    buckets = sorted(
        (
            {"kind": "bucket", "key": scope, "label": scope, "color": None, "count": count}
            for (project_id, scope), count in counts.items()
            if project_id is None and scope != PUBLIC_SCOPE
        ),
        key=lambda item: item["label"],
    )
    return general + projects + buckets


def get_term(db: Database, term_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM glossary_terms WHERE id=?", (term_id,))
    return _decode_aliases(row) if row else None


def create_term(
    db: Database,
    *,
    term: str,
    aliases: list[str] | None = None,
    scope: str = "通用",
    category: str = "其他",
    source: str = "manual",
    confirmed: bool = True,
    project_id: str | None = None,
    is_cue: bool = True,
    also: list[str] | None = None,
    snapshot_path: Path | str | None = None,
) -> dict[str, Any]:
    term = validate_term_text(term, what="术语")
    normalized_aliases = normalize_aliases(aliases or [])
    normalized_also = normalize_also(also or [], term=term)
    if category not in CATEGORIES:
        raise GlossaryError(f"分类必须是 {CATEGORIES}")
    scope = str(scope or "").strip() or "通用"
    _raise_duplicate(db, term)
    _check_names(
        db, term=term, aliases=normalized_aliases, also=normalized_also, exclude_term_id=None
    )
    now = utc_now()
    term_id = f"gt-{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO glossary_terms
           (id, term, aliases, scope, category, source, confirmed, hit_count,
            project_id, is_cue, also, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
        (
            term_id,
            term,
            json.dumps(normalized_aliases, ensure_ascii=False),
            scope,
            category,
            source,
            int(bool(confirmed)),
            project_id,
            int(bool(is_cue)),
            json.dumps(normalized_also, ensure_ascii=False),
            now,
            now,
        ),
    )
    if snapshot_path is not None:
        rewrite_snapshot(db, snapshot_path)
    return get_term(db, term_id)


# update_term 的 project_id 需要区分「本次不改」与「本次改成 None（解绑）」，
# 用哨兵值区分——None 是合法的目标值，不能借用它表示「未传」。
_UNSET: Any = object()


def update_term(
    db: Database,
    term_id: str,
    *,
    term: str | None = None,
    aliases: list[str] | None = None,
    scope: str | None = None,
    category: str | None = None,
    confirmed: bool | None = None,
    project_id: str | None = _UNSET,
    is_cue: bool | None = None,
    also: list[str] | None = None,
    snapshot_path: Path | str | None = None,
) -> dict[str, Any] | None:
    existing = db.query_one("SELECT * FROM glossary_terms WHERE id=?", (term_id,))
    if not existing:
        return None
    now = utc_now()
    fields = ["updated_at=?"]
    params: list[Any] = [now]
    final_term = existing["term"]
    if term is not None:
        term = validate_term_text(term, what="术语")
        if term != existing["term"]:
            _raise_duplicate(db, term)
        fields.append("term=?")
        params.append(term)
        final_term = term
    final_aliases = json.loads(existing["aliases"] or "[]")
    if aliases is not None:
        final_aliases = normalize_aliases(aliases)
        fields.append("aliases=?")
        params.append(json.dumps(final_aliases, ensure_ascii=False))
    final_also = json.loads(existing["also"] or "[]")
    if also is not None:
        final_also = normalize_also(also, term=final_term)
        fields.append("also=?")
        params.append(json.dumps(final_also, ensure_ascii=False))
    if term is not None or aliases is not None or also is not None:
        _check_names(
            db,
            term=final_term,
            aliases=final_aliases,
            also=final_also,
            exclude_term_id=term_id,
            check_others=also is not None,
        )
    if scope is not None:
        scope = str(scope or "").strip() or "通用"
        fields.append("scope=?")
        params.append(scope)
    if category is not None:
        if category not in CATEGORIES:
            raise GlossaryError(f"分类必须是 {CATEGORIES}")
        fields.append("category=?")
        params.append(category)
    if confirmed is not None:
        fields.append("confirmed=?")
        params.append(int(bool(confirmed)))
    if project_id is not _UNSET:
        fields.append("project_id=?")
        params.append(project_id)
    if is_cue is not None:
        fields.append("is_cue=?")
        params.append(int(bool(is_cue)))
    params.append(term_id)
    db.execute(f"UPDATE glossary_terms SET {', '.join(fields)} WHERE id=?", params)
    if snapshot_path is not None:
        rewrite_snapshot(db, snapshot_path)
    return get_term(db, term_id)


def merge_into_term(
    db: Database,
    term_id: str,
    *,
    aliases: list[str] | None = None,
    also: list[str] | None = None,
    make_public: bool = False,
    snapshot_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """重名时的合并：把新写的错写、叫法并进已有词条；make_public 时顺手改成公共词。"""
    existing = db.query_one("SELECT * FROM glossary_terms WHERE id=?", (term_id,))
    if not existing:
        return None
    merged_aliases = json.loads(existing["aliases"] or "[]")
    for alias in normalize_aliases(aliases or []):
        if alias != existing["term"] and alias not in merged_aliases:
            merged_aliases.append(alias)
    merged_also = json.loads(existing["also"] or "[]")
    for name in normalize_also(also or [], term=existing["term"]):
        if name not in merged_also and name not in merged_aliases:
            merged_also.append(name)
    kwargs: dict[str, Any] = {}
    if make_public:
        kwargs = {"project_id": None, "scope": PUBLIC_SCOPE}
    return update_term(
        db,
        term_id,
        aliases=merged_aliases,
        also=merged_also,
        snapshot_path=snapshot_path,
        **kwargs,
    )


def delete_term(
    db: Database, term_id: str, snapshot_path: Path | str | None = None
) -> bool:
    changed = db.execute_rowcount("DELETE FROM glossary_terms WHERE id=?", (term_id,))
    if changed == 1 and snapshot_path is not None:
        rewrite_snapshot(db, snapshot_path)
    return changed == 1


# —— 快照导出（relay 消费契约）——


def _snapshot_sort_key(row: dict[str, Any]) -> tuple:
    """纠错价值排序：人名 > 含中文的词 > 纯英文缩写，同档按字典序。

    relay 注入有 50 条上限，让最需要纠错的词（人名、中文术语）排在前面，
    避免英文缩写（ADHD/CRF 这类几乎不会写错）占名额、把人名挤出注入表。
    """
    term = row["term"]
    is_person = row["category"] == "人名"
    has_cjk = any("一" <= ch <= "鿿" for ch in term)
    return (0 if is_person else 1, 0 if has_cjk else 1, term)


def _snapshot_projects(db: Database) -> list[dict[str, Any]]:
    """各项目的名字、也叫和识别线索，relay 没有项目提示时靠它在逐字稿里认项目
    （glossary/injection.py）。线索和工作台认项目用的是同一张表，两边判得一致。"""
    from .project_profile import also_name_list, build_cue_table

    with db.autocommit() as connection:
        table = build_cue_table(connection)
        rows = connection.execute(
            "SELECT id, name, also_names FROM projects ORDER BY name, id"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "also": also_name_list(row["also_names"]),
            "cues": [{"text": cue.text, "kind": cue.kind} for cue in table.get(row["id"], [])],
        }
        for row in rows
    ]


def rewrite_snapshot(db: Database, snapshot_path: Path | str) -> None:
    """原子重写快照文件：临时文件 + os.replace，词典或项目（名字、也叫、文件夹）变更后调用。

    每个词条的 project_id、also 和顶层 projects 都是可选字段，schema_version 保持 1。
    """
    rows = db.query_all(
        """SELECT term, aliases, scope, category, project_id, also
             FROM glossary_terms WHERE confirmed=1"""
    )
    rows.sort(key=_snapshot_sort_key)
    terms: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {
            "term": row["term"],
            "aliases": json.loads(row["aliases"] or "[]"),
            "scope": row["scope"],
            "category": row["category"],
        }
        # 可选字段，schema_version 保持 1：读取方遇到别的版本会整份当空（snapshot_export.py）
        if row["project_id"]:
            entry["project_id"] = row["project_id"]
        also = json.loads(row["also"] or "[]")
        if also:
            entry["also"] = also
        terms.append(entry)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "updated_at": utc_now(),
        "terms": terms,
        "projects": _snapshot_projects(db),
    }
    path = Path(snapshot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_snapshot(snapshot_path: Path | str) -> dict[str, Any]:
    path = Path(snapshot_path)
    if not path.is_file():
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "updated_at": None,
            "terms": [],
        }
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
