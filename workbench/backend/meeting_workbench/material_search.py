"""材料进搜索（第三期 3f）：正文、两个字的词、文件名；一份内容一行。都只查库，不读盘。

- 用和会议一样的那组词（原词加词典展开的）。3 个字以上的词走 material_chunks_fts 的短语匹配，几个词
  合成一条 OR；短于 3 个字的词合成一条 instr 扫描：选了项目时只扫这个项目根目录里的片段，「全部项目」
  时材料只按文件名匹配（全部片段有几百 MB，逐字扫要好几秒），界面上写一句灰字。
- 文件名：material_files.name 里包含这个词（ASCII 不分大小写）。「声档会议记录/」里的文件不算，它们和
  会议标题重复。
- 一份内容一行：代表文件是范围里修改时间最新的那个（文件名命中的优先），另有几处写 copies；每行最多
  两处原文，带位置（loc 或 start_ms），另有几处写 more_hits。最多 20 份，按代表文件的修改时间从新到旧，
  和会议按日期排一致，不按相关度打分。
- 整条材料查询 1.5 秒预算（set_progress_handler），到点返回已经找到的，partial=true。
- 「意思相近的」材料（similar_rows）：向量那边给出片段和分数，这里连回片段和代表文件，查不到的丢掉。
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from . import relation_read
from .db import Database
from .material_rules import PLAYABLE_TYPES, REASON_LABELS
from .material_status import FILE_ERRORS, _iso_ns
from .materials import ROOT_ONLINE, volume_state
from .search import _fts_phrase, _like, _snippet, fold

MATERIAL_LIMIT = 20
HITS_PER_ROW = 2
BUDGET_SECONDS = 1.5
MIN_FTS_LEN = 3
# 一条查询最多取这么多片段（每份内容最多两段）、这么多文件名命中；到了上限也算结果可能不全
CHUNK_FETCH = 4000
NAME_FETCH = 2000
PROGRESS_OPS = 1000
PARAM_BATCH = 500
OFFLINE_TEXT = "资料盘未连接"

Scope = str | None  # None 全部；"none" 只看没归项目的会（不出材料）；其他是项目 id


class _Budget:
    """sqlite3 的 progress handler：到点返回非 0，正在跑的语句报 interrupted。"""

    def __init__(self, seconds: float, clock: Callable[[], float]):
        self.clock = clock
        self.deadline = clock() + seconds
        self.spent = False

    def __call__(self) -> int:
        if self.clock() >= self.deadline:
            self.spent = True
            return 1
        return 0


def scope_roots(connection: Any, scope: Scope) -> list[dict[str, Any]]:
    if scope == "none":
        return []
    sql = """SELECT r.id, r.path, r.project_id, p.name AS project_name, p.color AS project_color
               FROM project_material_roots r JOIN projects p ON p.id = r.project_id"""
    params: list[Any] = []
    if scope:
        sql += " WHERE r.project_id = ?"
        params.append(scope)
    return [dict(row) for row in connection.execute(sql + " ORDER BY r.id", params).fetchall()]


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _batches(values: Sequence[Any], size: int = PARAM_BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


_LIVE = "f.gone_at IS NULL AND f.zone != 'cards'"


def _chunk_rows(connection: Any, source: str, where: str, params: list[Any]) -> list[dict[str, Any]]:
    """命中的片段：每份内容按顺序取前两段，total 是这份内容一共命中几段。"""
    sql = f"""SELECT id, content_key, ordinal, loc, start_ms, text, total FROM (
                  SELECT c.id, c.content_key, c.ordinal, c.loc, c.start_ms, c.text,
                         ROW_NUMBER() OVER (PARTITION BY c.content_key ORDER BY c.ordinal) AS nth,
                         COUNT(*) OVER (PARTITION BY c.content_key) AS total
                    {source}
                   WHERE {where}
              ) WHERE nth <= {HITS_PER_ROW}
              LIMIT {CHUNK_FETCH}"""
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _body_hits_long(connection: Any, needles: list[str], root_ids: list[int]) -> list[dict[str, Any]]:
    match = " OR ".join(_fts_phrase(needle) for needle in needles)
    return _chunk_rows(
        connection,
        "FROM material_chunks_fts JOIN material_chunks c ON c.id = material_chunks_fts.rowid",
        f"""material_chunks_fts MATCH ?
            AND EXISTS (SELECT 1 FROM material_files f
                         WHERE f.content_key = c.content_key AND {_LIVE}
                           AND f.root_id IN ({_marks(root_ids)}))""",
        [match, *root_ids],
    )


def _body_hits_short(connection: Any, needles: list[str], root_ids: list[int]) -> list[dict[str, Any]]:
    """两个字的词：先由 material_files 按根目录取内容标识，再按内容标识取片段，不扫全表。"""
    ors = " OR ".join("instr(lower(c.text), ?) > 0" for _ in needles)
    return _chunk_rows(
        connection,
        "FROM material_chunks c",
        f"""c.content_key IN (SELECT f.content_key FROM material_files f
                               WHERE {_LIVE} AND f.content_key IS NOT NULL
                                 AND f.root_id IN ({_marks(root_ids)}))
            AND ({ors})""",
        [*root_ids, *[needle.lower() for needle in needles]],
    )


_FILE_FIELDS = """f.id, f.content_key, f.name, f.ext, f.rel_path, f.dir_rel, f.root_id, f.mtime_ns,
                  f.content_error"""


def _name_hits(connection: Any, needles: list[str], root_ids: list[int]) -> list[dict[str, Any]]:
    ors = " OR ".join("f.name LIKE ? ESCAPE '\\'" for _ in needles)
    rows = connection.execute(
        f"""SELECT {_FILE_FIELDS} FROM material_files f
             WHERE {_LIVE} AND f.root_id IN ({_marks(root_ids)}) AND ({ors})
             LIMIT {NAME_FETCH}""",
        [*root_ids, *[_like(needle) for needle in needles]],
    ).fetchall()
    return [dict(row) for row in rows]


def _files_for_contents(connection: Any, keys: list[str], root_ids: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in _batches(keys):
        result += [
            dict(row)
            for row in connection.execute(
                f"""SELECT {_FILE_FIELDS} FROM material_files f
                     WHERE f.content_key IN ({_marks(part)}) AND {_LIVE}
                       AND f.root_id IN ({_marks(root_ids)})""",
                [*part, *root_ids],
            ).fetchall()
        ]
    return result


def _matched(text: str, needles: list[str]) -> str:
    folded = fold(text)
    for needle in needles:
        if fold(needle) in folded:
            return needle
    return needles[0] if needles else ""


def _group_key(row: dict[str, Any]) -> str:
    return str(row["content_key"]) if row.get("content_key") else f"file:{row['id']}"


def _representative(files: list[dict[str, Any]], preferred: set[int]) -> dict[str, Any]:
    """文件名命中的优先，其次修改时间最新，再按 id 定下来。"""
    return max(files, key=lambda row: (row["id"] in preferred, row.get("mtime_ns") or 0, row["id"]))


class _Assembler:
    """把命中的内容和文件拼成一行：代表文件、根目录、项目、状态那一句、被会上提到几场。"""

    def __init__(self, connection: Any, roots: list[dict[str, Any]], state_of: Callable[[str], str]):
        self.connection = connection
        self.roots = {int(root["id"]): root for root in roots}
        self.root_ids = list(self.roots)
        self._online: dict[int, bool] = {}
        self.state_of = state_of

    def online(self, root_id: int) -> bool:
        if root_id not in self._online:
            self._online[root_id] = self.state_of(str(self.roots[root_id]["path"])) == ROOT_ONLINE
        return self._online[root_id]

    def groups(self, keys: Iterable[str], extra_files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """每个组（内容标识，或没有标识的单个文件）在范围里的活文件。"""
        content_keys = [key for key in dict.fromkeys(keys) if not key.startswith("file:")]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in _files_for_contents(self.connection, content_keys, self.root_ids):
            grouped.setdefault(str(row["content_key"]), []).append(row)
        for row in extra_files:
            if not row.get("content_key"):
                grouped.setdefault(_group_key(row), []).append(row)
        return grouped

    def rows(
        self,
        picked: list[tuple[str, dict[str, Any], int, bool]],
        hits: dict[str, list[dict[str, Any]]],
        totals: dict[str, int],
    ) -> list[dict[str, Any]]:
        """picked：(组, 代表文件, 另有几处, 文件名命中)。"""
        keys = [key for key, *_ in picked if not key.startswith("file:")]
        contents: dict[str, dict[str, Any]] = {}
        for part in _batches(keys):
            for row in self.connection.execute(
                f"SELECT content_key, layer, state, reason FROM material_contents WHERE content_key IN ({_marks(part)})",
                list(part),
            ).fetchall():
                contents[row["content_key"]] = dict(row)
        mentioned = self._mentions([rep["id"] for _key, rep, _copies, _name in picked])
        result = []
        for key, rep, copies, name_hit in picked:
            root = self.roots[int(rep["root_id"])]
            content = contents.get(key)
            online = self.online(int(rep["root_id"]))
            root_path = str(root["path"]).rstrip("/")
            layer = content["layer"] if content else None
            shown = hits.get(key, [])
            result.append(
                {
                    "file_id": rep["id"],
                    "content_key": rep.get("content_key"),
                    "name": rep["name"],
                    "ext": rep["ext"],
                    "path": f"{root_path}/{rep['rel_path']}",
                    "rel_path": rep["rel_path"],
                    "folder_path": f"{root_path}/{rep['dir_rel']}" if rep["dir_rel"] else root_path,
                    "root_id": rep["root_id"],
                    "project_id": root["project_id"],
                    "project_name": root["project_name"],
                    "project_color": root["project_color"],
                    "modified_at": _iso_ns(rep.get("mtime_ns")),
                    "root_online": online,
                    "playable": str(rep["ext"] or "") in PLAYABLE_TYPES,
                    "copies": copies,
                    "name_hit": name_hit,
                    "hits": [
                        {
                            "kind": layer or "text",
                            "loc": hit.get("loc"),
                            "start_ms": hit.get("start_ms"),
                            "text": _snippet(hit["text"], hit.get("matched") or ""),
                            "matched": hit.get("matched") or "",
                        }
                        for hit in shown
                    ],
                    "more_hits": max(0, totals.get(key, len(shown)) - len(shown)),
                    "state_text": self._state_text(rep, content, online),
                    "mentioned_meetings": mentioned.get(int(rep["id"]), 0),
                }
            )
        return result

    @staticmethod
    def _state_text(rep: dict[str, Any], content: dict[str, Any] | None, online: bool) -> str | None:
        error = rep.get("content_error")
        if error in FILE_ERRORS:
            return f"读不了：{REASON_LABELS[error]}"
        if content is not None and content.get("state") == "unreadable":
            return f"读不了：{REASON_LABELS.get(content.get('reason'), REASON_LABELS['corrupt'])}"
        if not online:
            return OFFLINE_TEXT
        return None

    def _mentions(self, file_ids: list[int]) -> dict[int, int]:
        # v16：字面和放宽的提到一起数（relation_read）
        return relation_read.file_mention_counts(self.connection, file_ids)


def material_search(
    db: Database,
    needles: list[str],
    *,
    scope: Scope = None,
    limit: int = MATERIAL_LIMIT,
    budget_seconds: float = BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    state_of: Callable[[str], str] = volume_state,
) -> dict[str, Any]:
    """返回 {items, partial}。"""
    words = [needle.strip() for needle in needles if needle and needle.strip()]
    result: dict[str, Any] = {"items": [], "partial": False}
    if not words or scope == "none":
        return result
    connection = db.connect()
    try:
        roots = scope_roots(connection, scope)
        if not roots:
            return result
        root_ids = [int(root["id"]) for root in roots]
        long_words = [word for word in words if len(word) >= MIN_FTS_LEN]
        short_words = [word for word in words if len(word) < MIN_FTS_LEN]
        budget = _Budget(budget_seconds, clock)
        connection.set_progress_handler(budget, PROGRESS_OPS)
        chunk_rows: list[dict[str, Any]] = []
        name_rows: list[dict[str, Any]] = []
        truncated = False
        steps: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []
        if long_words:
            steps.append(("chunks", lambda: _body_hits_long(connection, long_words, root_ids)))
        if short_words and scope:
            steps.append(("chunks", lambda: _body_hits_short(connection, short_words, root_ids)))
        steps.append(("names", lambda: _name_hits(connection, words, root_ids)))
        for kind, step in steps:
            if budget.spent:
                break
            try:
                rows = step()
            except sqlite3.OperationalError as error:
                # 到点打断时多半报 interrupted；打断在全文表初始化时报 vtable constructor failed
                if not budget.spent and "interrupted" not in str(error):
                    raise
                budget.spent = True
                break
            if kind == "chunks":
                truncated = truncated or len(rows) >= CHUNK_FETCH
                chunk_rows += rows
            else:
                truncated = truncated or len(rows) >= NAME_FETCH
                name_rows += rows
        connection.set_progress_handler(None, 0)
        result["partial"] = budget.spent or truncated

        # 一份内容最多两处原文；长词、短词两条查询可能命中同一段
        hits: dict[str, list[dict[str, Any]]] = {}
        totals: dict[str, int] = {}
        seen_chunks: set[int] = set()
        for row in sorted(chunk_rows, key=lambda item: (item["content_key"], item["ordinal"])):
            key = str(row["content_key"])
            totals[key] = max(totals.get(key, 0), int(row["total"]))
            if row["id"] in seen_chunks:
                continue
            seen_chunks.add(row["id"])
            bucket = hits.setdefault(key, [])
            if len(bucket) < HITS_PER_ROW:
                bucket.append({**row, "matched": _matched(row["text"], words)})
        name_ids = {int(row["id"]) for row in name_rows}
        name_groups = {_group_key(row) for row in name_rows}

        assembler = _Assembler(connection, roots, state_of)
        grouped = assembler.groups([*hits, *name_groups], name_rows)
        ranked = []
        for key, files in grouped.items():
            if key not in hits and key not in name_groups:
                continue
            rep = _representative(files, name_ids)
            ranked.append((rep.get("mtime_ns") or 0, int(rep["id"]), key, rep, len(files) - 1))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        picked = [
            (key, rep, copies, any(int(row["id"]) in name_ids for row in grouped[key]))
            for _mtime, _id, key, rep, copies in ranked[:limit]
        ]
        result["items"] = assembler.rows(picked, hits, totals)
        return result
    finally:
        connection.close()


def similar_rows(
    db: Database,
    scored: list[tuple[int, float]],
    *,
    scope: Scope,
    exclude: set[str],
    limit: int,
    min_score: float,
    state_of: Callable[[str], str] = volume_state,
) -> tuple[list[dict[str, Any]], list[int]]:
    """向量给出的 (片段 id, 分数) 连回片段和代表文件：一份内容一行，去掉已经列在 materials 里的。
    返回 (行, 查不到的片段 id)；后者给内存矩阵记作废。"""
    if not scored or scope == "none":
        return [], []
    connection = db.connect()
    try:
        roots = scope_roots(connection, scope)
        if not roots:
            return [], []
        ids = [chunk_id for chunk_id, _score in scored]
        chunks: dict[int, dict[str, Any]] = {}
        for part in _batches(ids):
            for row in connection.execute(
                f"SELECT id, content_key, ordinal, loc, start_ms, text FROM material_chunks WHERE id IN ({_marks(part)})",
                list(part),
            ).fetchall():
                chunks[int(row["id"])] = dict(row)
        missing = [chunk_id for chunk_id in ids if chunk_id not in chunks]
        best: dict[str, tuple[float, dict[str, Any]]] = {}
        for chunk_id, score in scored:
            chunk = chunks.get(chunk_id)
            if chunk is None or score < min_score:
                continue
            key = str(chunk["content_key"])
            if key in exclude or key in best:
                continue
            best[key] = (score, chunk)
        assembler = _Assembler(connection, roots, state_of)
        grouped = assembler.groups(list(best), [])
        ordered = sorted(
            ((score, key, chunk) for key, (score, chunk) in best.items() if grouped.get(key)),
            key=lambda item: item[0],
            reverse=True,
        )[:limit]
        picked = [(key, _representative(grouped[key], set()), len(grouped[key]) - 1, False) for _s, key, _c in ordered]
        hits = {key: [{**chunk, "matched": ""}] for _score, key, chunk in ordered}
        rows = assembler.rows(picked, hits, {key: 1 for key in hits})
        for row, (score, _key, _chunk) in zip(rows, ordered, strict=True):
            row["score"] = round(float(score), 4)
        return rows, missing
    finally:
        connection.close()


def allowed_content_keys(db: Database, scope: Scope) -> set[str] | None:
    """「意思相近」的范围：范围里活文件的内容标识。None 表示不出材料。"""
    if scope == "none":
        return None
    connection = db.connect()
    try:
        roots = scope_roots(connection, scope)
        if not roots:
            return set()
        root_ids = [int(root["id"]) for root in roots]
        rows = connection.execute(
            f"""SELECT DISTINCT f.content_key FROM material_files f
                 WHERE {_LIVE} AND f.content_key IS NOT NULL AND f.root_id IN ({_marks(root_ids)})""",
            root_ids,
        ).fetchall()
        return {str(row[0]) for row in rows}
    finally:
        connection.close()
