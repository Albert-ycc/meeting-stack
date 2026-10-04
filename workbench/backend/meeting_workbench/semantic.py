from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

from .busy import BusySignal
from .config import Settings
from .db import Database, utc_now


# 后台编码每批最多几条（每批拿一次 encode_lock）
BACKGROUND_BATCH = 32


class Embedder(Protocol):
    def encode(self, texts: list[str], **kwargs: Any) -> Any: ...


class SemanticUnavailable(RuntimeError):
    pass


class SemanticBusy(RuntimeError):
    pass


class SemanticPaused(RuntimeError):
    pass


class SemanticIndex:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        busy_check: Callable[[], bool] | None = None,
    ):
        self.db = db
        self.settings = settings
        self._embedder = embedder
        # 默认只看进程列表和库（命令行 semantic-index 用）；服务里传完整的 busy.BusySignal。
        self.busy_check = busy_check if busy_check is not None else BusySignal(db)
        self._rebuild_lock = threading.Lock()
        # 第四期 4a：后台编码（材料向量、会议段落重建、4d 的相关窗口）每批最多 32 条拿一次这把锁，
        # 轮流用模型；用户搜索的 encode_query 不拿锁，不会被后台卡住。
        self.encode_lock = threading.Lock()

    def _model(self) -> Embedder:
        if self._embedder is None:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:
                raise SemanticUnavailable("本地语义模型依赖尚未安装") from error
            try:
                self._embedder = SentenceTransformer(
                    self.settings.semantic_model,
                    local_files_only=True,
                    device="cpu",
                )
            except Exception as error:
                raise SemanticUnavailable(
                    f"本地模型 {self.settings.semantic_model} 不可用，请先离线安装"
                ) from error
        return self._embedder

    def warm(self) -> bool:
        if not self.settings.semantic_enabled:
            return False
        self._model()
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把一批短文本编码为归一化向量（项目归属语义匹配用，复用同一模型实例）。"""
        if not texts:
            return []
        vectors = self._normalize(self._model().encode(texts, show_progress_bar=False))
        return vectors.tolist()

    def encode_texts(
        self, texts: list[str], *, background: bool = True, batch_size: int = BACKGROUND_BATCH
    ) -> np.ndarray:
        """材料片段的向量（3f 向量循环用）：归一化的 float32，一行一段。

        background=True（后台编码）时按 batch_size（最多 32）条一批，每批拿一次 encode_lock。"""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        if not background:
            return self._encode(texts, batch_size)
        size = max(1, min(int(batch_size), BACKGROUND_BATCH))
        parts: list[np.ndarray] = []
        for start in range(0, len(texts), size):
            with self.encode_lock:
                parts.append(self._encode(texts[start : start + size], size))
        return np.vstack(parts)

    def _encode(self, texts: list[str], batch_size: int) -> np.ndarray:
        return self._normalize(
            self._model().encode(
                texts, batch_size=batch_size, show_progress_bar=False, normalize_embeddings=False
            )
        )

    def encode_query(self, query: str) -> np.ndarray | None:
        """搜索词的向量只算一次，会议和材料的「意思相近」共用。语义检索关着或词为空时回 None。"""
        if not self.settings.semantic_enabled or not query.strip():
            return None
        return self._normalize(
            self._model().encode(
                [query.strip()], show_progress_bar=False, normalize_embeddings=False
            )
        )[0]

    @staticmethod
    def _normalize(vectors: np.ndarray) -> np.ndarray:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)

    def rebuild(self, *, force: bool = False) -> int:
        if not self.settings.semantic_enabled:
            return 0
        if self.busy_check():
            raise SemanticPaused("会议正在转写，语义索引已暂停")
        if not self._rebuild_lock.acquire(blocking=False):
            raise SemanticBusy("语义索引正在重建")
        try:
            return self._rebuild_locked(force=force)
        finally:
            self._rebuild_lock.release()

    def _rebuild_locked(self, *, force: bool) -> int:
        # 扫描循环每轮都跑这条。先在 (segment_id, model) 主键索引里挑出不再属于当前版本的段落，
        # 再按主键删。以前写成 NOT IN（全部当前段落），每轮要把十几万个段落 id 乱序灌进临时 B 树，
        # 缓存装不下就反复溢写，一轮写两百多 MB 临时文件，还整表扫一遍五百多 MB 的向量。
        with self.db.transaction() as connection:
            connection.execute(
                """DELETE FROM embeddings
                   WHERE model = ? AND segment_id IN (
                     SELECT e.segment_id FROM embeddings e
                     LEFT JOIN segments s ON s.id = e.segment_id
                     LEFT JOIN meetings m ON m.current_transcript_version_id = s.version_id
                     WHERE e.model = ? AND m.id IS NULL
                   )""",
                (self.settings.semantic_model, self.settings.semantic_model),
            )
        rows = self.db.query_all(
            """SELECT s.id, s.text FROM segments s JOIN meetings m ON m.current_transcript_version_id = s.version_id
               LEFT JOIN embeddings e ON e.segment_id = s.id AND e.model = ?
               WHERE ? OR e.segment_id IS NULL ORDER BY s.meeting_id, s.ordinal""",
            (self.settings.semantic_model, int(force)),
        )
        stored = 0
        # 32 条一批编码、一批一写：编码要几分钟，期间用户拆段合段会删掉旧段落 id，
        # 已经不在的段落跳过，不能让一个外键冲突把整轮编码全部回滚。
        for start in range(0, len(rows), BACKGROUND_BATCH):
            batch = rows[start : start + BACKGROUND_BATCH]
            vectors = self.encode_texts([row["text"] for row in batch], background=True)
            with self.db.transaction() as connection:
                for row, vector in zip(batch, vectors, strict=True):
                    stored += connection.execute(
                        """INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at)
                           SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM segments WHERE id = ?)
                           ON CONFLICT(segment_id, model) DO UPDATE SET
                             dimensions=excluded.dimensions,
                             vector=excluded.vector, created_at=excluded.created_at""",
                        (
                            row["id"],
                            self.settings.semantic_model,
                            int(vector.shape[0]),
                            vector.astype(np.float32).tobytes(),
                            utc_now(),
                            row["id"],
                        ),
                    ).rowcount
        return stored

    def search(
        self, query: str, *, limit: int = 20, scope: str | None = None
    ) -> list[dict[str, Any]]:
        query_vector = self.encode_query(query)
        if query_vector is None:
            return []
        return self.search_vector(query_vector, scope=scope, limit=limit)

    def search_vector(
        self, query_vector: np.ndarray, *, scope: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """会议段落里意思相近的。先按范围过滤再打分（以前先取全局前 60 再过滤，小项目里常常什么都不剩）。
        scope：None 全部；"none" 只看没归项目的会；其他是项目 id。"""
        clause = ""
        params: list[Any] = [self.settings.semantic_model]
        if scope == "none":
            clause = " AND m.project_id IS NULL"
        elif scope:
            clause = " AND m.project_id = ?"
            params.append(scope)
        rows = self.db.query_all(
            f"""SELECT s.id AS segment_id, s.meeting_id, m.title, m.canonical_dir,
                      m.recording_date, m.project_id,
                      p.name AS project_name, p.color AS project_color,
                      'segment' AS match_kind, s.start_ms, s.end_ms,
                      s.speaker_name, s.speaker_label, s.text, e.dimensions, e.vector
                 FROM embeddings e
                 JOIN segments s ON s.id = e.segment_id
                 JOIN meetings m ON m.current_transcript_version_id = s.version_id
                 LEFT JOIN projects p ON p.id = m.project_id
                WHERE e.model = ?{clause}""",
            params,
        )
        scored = []
        for row in rows:
            vector = np.frombuffer(row.pop("vector"), dtype=np.float32, count=row.pop("dimensions"))
            if vector.shape != query_vector.shape:
                continue
            row["score"] = float(np.dot(query_vector, vector))
            scored.append(row)
        scored.sort(key=lambda row: row["score"], reverse=True)
        return scored[:limit]
