"""材料片段的向量（第三期 3f）：后台慢慢补向量，内存里放一份矩阵给「意思相近」打分。

补向量（embed_round，由 material_embed_loop 每 30 秒调一次，语义检索关着时不跑）：
- 找还没有向量的片段，每批 64 个，一批一个短事务；每批之间查忙信号；每轮最多 30 秒。
- 写回用 INSERT … SELECT … WHERE EXISTS：算向量期间片段已被删（重读或清理）就丢掉。
- float16 存，每行约 1KB。不走 SemanticIndex.rebuild、不改 semantic_status：否则首页会一直显示
  「语义检索 重建中」，扫描循环也会被拖到超时。

内存矩阵（search）：
- float16，按片段 id 增量补（id 只增不复用，只要取比上次大的）；最多放 40 万个片段（约 400MB），超过时
  只放最近修改的文件的片段，日志里写明少放了多少。
- 片段被删时只在内存里记成作废（搜索结果连回片段时查不到的），打分时跳过；作废超过 20% 或距上次重建
  满 1 小时才整个重建，重建时先建好新的再换掉旧的。
- 打分分块转成 float32 算（每块不超过 48MB 临时内存），范围过滤用在分数上，不给矩阵做下标。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .db import Database, utc_now

logger = logging.getLogger(__name__)

FIRST_DELAY_SECONDS = 5
LOOP_SECONDS = 30
ROUND_SECONDS = 30
BATCH = 64
MAX_ROWS = 400_000
REBUILD_INVALID_RATIO = 0.2
REBUILD_EVERY_SECONDS = 3600
BLOCK_BYTES = 48 * 1024 * 1024
FETCH = 60


class _Matrix:
    """一份矩阵：float16 向量、片段 id、内容编号、作废标记。容量按 1.5 倍长，追加不整份复制。"""

    def __init__(self, dim: int, capacity: int = 1024):
        self.dim = dim
        self.n = 0
        self.vectors = np.zeros((capacity, dim), dtype=np.float16)
        self.ids = np.zeros(capacity, dtype=np.int64)
        self.codes = np.zeros(capacity, dtype=np.int32)
        self.valid = np.zeros(capacity, dtype=bool)
        self.invalid = 0
        self.max_id = 0
        self.code_of: dict[str, int] = {}
        self.row_of: dict[int, int] = {}

    def _grow(self, need: int) -> None:
        capacity = self.vectors.shape[0]
        if need <= capacity:
            return
        size = max(need, int(capacity * 1.5) + 1)
        for name in ("vectors", "ids", "codes", "valid"):
            old = getattr(self, name)
            shape = (size, self.dim) if name == "vectors" else (size,)
            fresh = np.zeros(shape, dtype=old.dtype)
            fresh[: self.n] = old[: self.n]
            setattr(self, name, fresh)

    def extend(self, chunk_ids: list[int], content_keys: list[str], vectors: np.ndarray) -> None:
        count = len(chunk_ids)
        if count == 0:
            return
        self._grow(self.n + count)
        start, end = self.n, self.n + count
        self.vectors[start:end] = vectors
        self.ids[start:end] = chunk_ids
        self.codes[start:end] = [self.code_of.setdefault(key, len(self.code_of)) for key in content_keys]
        self.valid[start:end] = True
        for offset, chunk_id in enumerate(chunk_ids):
            self.row_of[chunk_id] = start + offset
        self.n = end
        self.max_id = max(self.max_id, max(chunk_ids))

    def forget(self, chunk_id: int) -> None:
        row = self.row_of.get(chunk_id)
        if row is not None and self.valid[row]:
            self.valid[row] = False
            self.invalid += 1


class MaterialVectors:
    def __init__(
        self,
        db: Database,
        settings: Any,
        semantic: Any,
        *,
        busy_check: Callable[[], bool] | None = None,
        stop: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_rows: int = MAX_ROWS,
        block_bytes: int = BLOCK_BYTES,
    ):
        self.db = db
        self.settings = settings
        self.semantic = semantic
        self.busy_check = busy_check
        self.stop = stop
        self.clock = clock
        self.max_rows = max_rows
        self.block_bytes = block_bytes
        self._cursor = 0
        self._lock = threading.Lock()
        self._matrix: _Matrix | None = None
        self._built_at: float | None = None

    @property
    def model(self) -> str:
        return str(self.settings.semantic_model)

    # ------------------------------------------------------------------ 补向量

    def embed_round(self, *, round_seconds: float = ROUND_SECONDS, batch: int = BATCH) -> dict[str, Any]:
        """一轮：返回 {embedded, ended}。ended：disabled、stopping、busy、budget、done。"""
        stats: dict[str, Any] = {"embedded": 0, "ended": None}
        if not self.settings.semantic_enabled:
            stats["ended"] = "disabled"
            return stats
        deadline = self.clock() + round_seconds
        while True:
            if self.stop is not None and self.stop.is_set():
                stats["ended"] = "stopping"
                return stats
            if self.busy_check is not None and self.busy_check():
                stats["ended"] = "busy"
                return stats
            if self.clock() >= deadline:
                stats["ended"] = "budget"
                return stats
            rows = self.db.query_all(
                """SELECT c.id, c.text FROM material_chunks c
                    WHERE c.id > ? AND NOT EXISTS (
                        SELECT 1 FROM material_chunk_vectors v WHERE v.chunk_id = c.id AND v.model = ?)
                    ORDER BY c.id LIMIT ?""",
                (self._cursor, self.model, batch),
            )
            if not rows:
                # id 只增不复用：新片段的 id 总比游标大，游标不用回头
                stats["ended"] = "done"
                return stats
            vectors = self.semantic.encode_texts([row["text"] for row in rows])
            now = utc_now()
            with self.db.transaction() as connection:
                for row, vector in zip(rows, vectors, strict=True):
                    connection.execute(
                        """INSERT OR REPLACE INTO material_chunk_vectors(chunk_id, model, vector, created_at)
                           SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM material_chunks WHERE id = ?)""",
                        (
                            row["id"],
                            self.model,
                            np.asarray(vector, dtype=np.float16).tobytes(),
                            now,
                            row["id"],
                        ),
                    )
            self._cursor = int(rows[-1]["id"])
            stats["embedded"] += len(rows)

    # ------------------------------------------------------------------ 内存矩阵

    def _load(self, matrix: _Matrix | None, *, after: int, limit: int | None, recent_first: bool) -> _Matrix | None:
        if recent_first:
            sql = """SELECT v.chunk_id, v.vector, c.content_key
                       FROM material_chunk_vectors v
                       JOIN material_chunks c ON c.id = v.chunk_id
                       LEFT JOIN (SELECT content_key, MAX(mtime_ns) AS latest FROM material_files
                                   WHERE gone_at IS NULL AND content_key IS NOT NULL GROUP BY content_key) k
                         ON k.content_key = c.content_key
                      WHERE v.model = ?
                      ORDER BY COALESCE(k.latest, 0) DESC, v.chunk_id DESC
                      LIMIT ?"""
            params: tuple[Any, ...] = (self.model, limit)
        else:
            sql = """SELECT v.chunk_id, v.vector, c.content_key
                       FROM material_chunk_vectors v
                       JOIN material_chunks c ON c.id = v.chunk_id
                      WHERE v.model = ? AND v.chunk_id > ?
                      ORDER BY v.chunk_id"""
            params = (self.model, after)
            if limit is not None:
                sql += " LIMIT ?"
                params = (*params, limit)
        connection = self.db.connect()
        try:
            cursor = connection.execute(sql, params)
            while True:
                rows = cursor.fetchmany(4096)
                if not rows:
                    break
                if matrix is None:
                    matrix = _Matrix(len(rows[0]["vector"]) // 2)
                width = matrix.dim * 2
                rows = [row for row in rows if len(row["vector"]) == width]
                if not rows:
                    continue
                vectors = np.frombuffer(b"".join(row["vector"] for row in rows), dtype=np.float16)
                matrix.extend(
                    [int(row["chunk_id"]) for row in rows],
                    [str(row["content_key"]) for row in rows],
                    vectors.reshape(len(rows), matrix.dim),
                )
        finally:
            connection.close()
        return matrix

    def _rebuild(self) -> _Matrix | None:
        total = int((self.db.query_one(
            "SELECT COUNT(*) AS n FROM material_chunk_vectors WHERE model = ?", (self.model,)
        ) or {"n": 0})["n"])
        if total > self.max_rows:
            logger.warning("材料向量有 %d 个片段，内存里只放最近修改的 %d 个，少放了 %d 个", total, self.max_rows, total - self.max_rows)
            matrix = self._load(None, after=0, limit=self.max_rows, recent_first=True)
            if matrix is not None:
                # 之后只补比现在所有片段都新的
                top = self.db.query_one("SELECT COALESCE(MAX(chunk_id), 0) AS m FROM material_chunk_vectors WHERE model = ?", (self.model,))
                matrix.max_id = max(matrix.max_id, int((top or {"m": 0})["m"]))
            return matrix
        return self._load(None, after=0, limit=None, recent_first=False)

    def refresh(self) -> _Matrix | None:
        """作废超过 20% 或满 1 小时整个重建（先建好新的再换）；否则只补 id 比上次大的。"""
        with self._lock:
            matrix = self._matrix
            now = self.clock()
            stale = (
                matrix is None
                or self._built_at is None
                or now - self._built_at >= REBUILD_EVERY_SECONDS
                or (matrix.n > 0 and matrix.invalid / matrix.n > REBUILD_INVALID_RATIO)
            )
            if stale:
                matrix = self._rebuild()
                self._built_at = now
            else:
                room = self.max_rows - matrix.n
                if room > 0:
                    matrix = self._load(matrix, after=matrix.max_id, limit=room, recent_first=False)
            self._matrix = matrix
            return matrix

    def forget(self, chunk_ids: list[int]) -> None:
        """搜索结果连回片段时查不到的：记成作废，打分时跳过。"""
        with self._lock:
            if self._matrix is None:
                return
            for chunk_id in chunk_ids:
                self._matrix.forget(int(chunk_id))

    def search(self, query_vector: np.ndarray, *, allowed: set[str] | None, fetch: int = FETCH) -> list[tuple[int, float]]:
        """分数最高的 fetch 个片段 (id, 分数)。allowed：范围里的内容标识，None 表示不限。"""
        matrix = self.refresh()
        if matrix is None or matrix.n == 0:
            return []
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        if query.shape[0] != matrix.dim:
            return []
        with self._lock:
            n = matrix.n
            allowed_codes = None
            if allowed is not None:
                allowed_codes = np.fromiter(
                    (matrix.code_of[key] for key in allowed if key in matrix.code_of), dtype=np.int32
                )
                if allowed_codes.size == 0:
                    return []
            block = max(1024, self.block_bytes // (matrix.dim * 4))
            best_ids: list[np.ndarray] = []
            best_scores: list[np.ndarray] = []
            for start in range(0, n, block):
                end = min(n, start + block)
                scores = matrix.vectors[start:end].astype(np.float32) @ query
                mask = matrix.valid[start:end].copy()
                if allowed_codes is not None:
                    mask &= np.isin(matrix.codes[start:end], allowed_codes)
                scores[~mask] = -np.inf
                keep = min(fetch, end - start)
                top = np.argpartition(-scores, keep - 1)[:keep]
                top = top[np.isfinite(scores[top])]
                best_ids.append(matrix.ids[start:end][top])
                best_scores.append(scores[top])
        if not best_ids:
            return []
        ids = np.concatenate(best_ids)
        scores = np.concatenate(best_scores)
        order = np.argsort(-scores, kind="stable")[:fetch]
        return [(int(ids[i]), float(scores[i])) for i in order]
