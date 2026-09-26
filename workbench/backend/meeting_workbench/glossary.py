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


class GlossaryError(ValueError):
    pass


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


def extract_corrections(
    old_text: str, new_text: str, known_terms: set[str]
) -> list[tuple[str, str]]:
    """从新旧文本 diff 提取疑似错字更正，返回 [(wrong, correct), ...]（已去重）。

    捕获规则：replace 块两段均为 2-8 字中文/字母词，且编辑距离 ≤2（同音/形近），
    或 correct 是已知权威写法（用户在往正确写法改）。
    排除：纯标点修正、两个已知权威词之间的实质替换（字符级 diff 会把整词替换拆成
    小块，所以用「已知词覆盖」判断：候选块两侧各自落在某个权威词里即为内容变更）。
    """
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    matcher = difflib.SequenceMatcher(None, old_text, new_text)
    for operation, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if operation != "replace":
            continue
        wrong = old_text[a_start:a_end]
        correct = new_text[b_start:b_end]
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
        pair = (wrong, correct)
        if pair not in seen:
            seen.add(pair)
            candidates.append(pair)
    return candidates


def _context_snippet(text: str, needle: str, width: int = 20) -> str:
    index = text.find(needle)
    if index < 0:
        return text[:width]
    start = max(0, index - width)
    end = min(len(text), index + len(needle) + width)
    return text[start:end]


def record_corrections_from_diff(
    db: Database,
    old_markdown: str | None,
    new_markdown: str | None,
    *,
    meeting_id: str,
    scope: str = "通用",
) -> int:
    """把新旧纪要 markdown 的疑似错字更正写入待确认队列，返回新增条数。"""
    if not old_markdown or not new_markdown or old_markdown == new_markdown:
        return 0
    known_terms = {row["term"] for row in db.query_all("SELECT term FROM glossary_terms")}
    candidates = extract_corrections(old_markdown, new_markdown, known_terms)
    scope = str(scope or "").strip() or "通用"
    count = 0
    for wrong, correct in candidates:
        if add_suggestion(
            db,
            wrong=wrong,
            correct=correct,
            scope=scope,
            meeting_id=meeting_id,
            context=_context_snippet(new_markdown, correct),
        ):
            count += 1
    return count


# —— glossary_suggestions：待确认反写队列 ——


def add_suggestion(
    db: Database,
    *,
    wrong: str,
    correct: str,
    scope: str = "通用",
    meeting_id: str | None = None,
    context: str | None = None,
) -> bool:
    wrong = validate_term_text(wrong, what="错写", min_len=MIN_DIFF_LEN, max_len=MAX_DIFF_LEN)
    correct = validate_term_text(
        correct, what="正确写法", min_len=MIN_DIFF_LEN, max_len=MAX_DIFF_LEN
    )
    if wrong == correct:
        return False
    # 同一 (wrong, correct) 无论处于哪个状态都不重复建
    if db.query_one(
        "SELECT 1 AS present FROM glossary_suggestions WHERE wrong=? AND correct=?",
        (wrong, correct),
    ):
        return False
    # 该映射已入词典（correct 的 aliases 已含 wrong）则不再排队
    term = db.query_one("SELECT aliases FROM glossary_terms WHERE term=?", (correct,))
    if term and wrong in json.loads(term["aliases"] or "[]"):
        return False
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_suggestions
           (id, wrong, correct, scope, meeting_id, context, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (f"gs-{uuid.uuid4().hex}", wrong, correct, scope, meeting_id, context, now, now),
    )
    return True


def list_suggestions(db: Database, status: str | None = None) -> list[dict[str, Any]]:
    if status is None:
        return db.query_all(
            "SELECT * FROM glossary_suggestions ORDER BY created_at DESC, id DESC"
        )
    return db.query_all(
        "SELECT * FROM glossary_suggestions WHERE status=? ORDER BY created_at DESC, id DESC",
        (status,),
    )


def confirm_suggestion(
    db: Database, suggestion_id: str, snapshot_path: Path | str | None = None
) -> bool:
    """确认一条反写建议：错写并入正确写法的 aliases，必要时新建权威词条。"""
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT wrong, correct, scope FROM glossary_suggestions
               WHERE id=? AND status='pending'""",
            (suggestion_id,),
        ).fetchone()
        if not row:
            return False
        now = utc_now()
        connection.execute(
            "UPDATE glossary_suggestions SET status='confirmed', updated_at=? WHERE id=?",
            (now, suggestion_id),
        )
        existing = connection.execute(
            "SELECT id, aliases FROM glossary_terms WHERE term=?", (row["correct"],)
        ).fetchone()
        if existing:
            aliases = json.loads(existing["aliases"] or "[]")
            if row["wrong"] not in aliases:
                aliases.append(row["wrong"])
                connection.execute(
                    "UPDATE glossary_terms SET aliases=?, updated_at=? WHERE id=?",
                    (json.dumps(aliases, ensure_ascii=False), now, existing["id"]),
                )
        else:
            connection.execute(
                """INSERT INTO glossary_terms
                   (id, term, aliases, scope, category, source, confirmed, hit_count,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, '其他', 'auto', 1, 0, ?, ?)""",
                (
                    f"gt-{uuid.uuid4().hex}",
                    row["correct"],
                    json.dumps([row["wrong"]], ensure_ascii=False),
                    row["scope"] or "通用",
                    now,
                    now,
                ),
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
    """筛选器分组：通用 → 项目（按名）→ 其他桶（按名），只列有术语的分组。"""
    rows = db.query_all(
        """SELECT g.scope, g.project_id, p.name AS project_name, p.color AS project_color,
                  COUNT(*) AS count
             FROM glossary_terms g
             LEFT JOIN projects p ON p.id = g.project_id
            GROUP BY g.scope, g.project_id"""
    )
    general: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    buckets: list[dict[str, Any]] = []
    for row in rows:
        if row["project_id"]:
            projects.append(
                {
                    "kind": "project",
                    "key": row["project_id"],
                    "label": row["project_name"],
                    "color": row["project_color"],
                    "count": row["count"],
                }
            )
        elif row["scope"] == "通用":
            general.append(
                {"kind": "general", "key": "通用", "label": "通用", "color": None, "count": row["count"]}
            )
        else:
            buckets.append(
                {
                    "kind": "bucket",
                    "key": row["scope"],
                    "label": row["scope"],
                    "color": None,
                    "count": row["count"],
                }
            )
    projects.sort(key=lambda item: item["label"])
    buckets.sort(key=lambda item: item["label"])
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
    snapshot_path: Path | str | None = None,
) -> dict[str, Any]:
    term = validate_term_text(term, what="术语")
    normalized_aliases = normalize_aliases(aliases or [])
    if category not in CATEGORIES:
        raise GlossaryError(f"分类必须是 {CATEGORIES}")
    scope = str(scope or "").strip() or "通用"
    if db.query_one("SELECT 1 AS present FROM glossary_terms WHERE term=?", (term,)):
        raise GlossaryError("术语已存在")
    now = utc_now()
    term_id = f"gt-{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO glossary_terms
           (id, term, aliases, scope, category, source, confirmed, hit_count,
            project_id, is_cue, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
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
    snapshot_path: Path | str | None = None,
) -> dict[str, Any] | None:
    existing = db.query_one("SELECT * FROM glossary_terms WHERE id=?", (term_id,))
    if not existing:
        return None
    now = utc_now()
    fields = ["updated_at=?"]
    params: list[Any] = [now]
    if term is not None:
        term = validate_term_text(term, what="术语")
        if term != existing["term"] and db.query_one(
            "SELECT 1 AS present FROM glossary_terms WHERE term=?", (term,)
        ):
            raise GlossaryError("术语已存在")
        fields.append("term=?")
        params.append(term)
    if aliases is not None:
        fields.append("aliases=?")
        params.append(json.dumps(normalize_aliases(aliases), ensure_ascii=False))
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


def rewrite_snapshot(db: Database, snapshot_path: Path | str) -> None:
    """原子重写快照文件：临时文件 + os.replace，任何词典变更后调用。"""
    rows = db.query_all(
        "SELECT term, aliases, scope, category FROM glossary_terms WHERE confirmed=1"
    )
    rows.sort(key=_snapshot_sort_key)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "updated_at": utc_now(),
        "terms": [
            {
                "term": row["term"],
                "aliases": json.loads(row["aliases"] or "[]"),
                "scope": row["scope"],
                "category": row["category"],
            }
            for row in rows
        ],
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
