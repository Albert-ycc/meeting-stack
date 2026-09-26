from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

from .config import Settings
from .db import Database, utc_now


class Embedder(Protocol):
    def encode(self, texts: list[str], **kwargs: Any) -> Any: ...


class SemanticUnavailable(RuntimeError):
    pass


class SemanticBusy(RuntimeError):
    pass


class SemanticPaused(RuntimeError):
    pass


def funasr_is_busy() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "funasr|auto_model|transcribe_funasr"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


class SemanticIndex:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        busy_check: Callable[[], bool] = funasr_is_busy,
    ):
        self.db = db
        self.settings = settings
        self._embedder = embedder
        self.busy_check = busy_check
        self._rebuild_lock = threading.Lock()

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
            raise SemanticPaused("FunASR 转写运行中，语义索引已暂停")
        if not self._rebuild_lock.acquire(blocking=False):
            raise SemanticBusy("语义索引正在重建")
        try:
            return self._rebuild_locked(force=force)
        finally:
            self._rebuild_lock.release()

    def _rebuild_locked(self, *, force: bool) -> int:
        with self.db.transaction() as connection:
            connection.execute(
                """DELETE FROM embeddings
                   WHERE model = ? AND segment_id NOT IN (
                     SELECT s.id FROM segments s
                     JOIN meetings m ON m.current_transcript_version_id = s.version_id
                   )""",
                (self.settings.semantic_model,),
            )
        rows = self.db.query_all(
            """SELECT s.id, s.text FROM segments s JOIN meetings m ON m.current_transcript_version_id = s.version_id
               LEFT JOIN embeddings e ON e.segment_id = s.id AND e.model = ?
               WHERE ? OR e.segment_id IS NULL ORDER BY s.meeting_id, s.ordinal""",
            (self.settings.semantic_model, int(force)),
        )
        if not rows:
            return 0
        vectors = self._normalize(
            self._model().encode(
                [row["text"] for row in rows],
                batch_size=32,
                show_progress_bar=False,
                normalize_embeddings=False,
            )
        )
        with self.db.transaction() as connection:
            for row, vector in zip(rows, vectors, strict=True):
                connection.execute(
                    """INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(segment_id, model) DO UPDATE SET dimensions=excluded.dimensions,
                         vector=excluded.vector, created_at=excluded.created_at""",
                    (
                        row["id"],
                        self.settings.semantic_model,
                        int(vector.shape[0]),
                        vector.astype(np.float32).tobytes(),
                        utc_now(),
                    ),
                )
        return len(rows)

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not self.settings.semantic_enabled or not query.strip():
            return []
        query_vector = self._normalize(
            self._model().encode(
                [query.strip()], show_progress_bar=False, normalize_embeddings=False
            )
        )[0]
        rows = self.db.query_all(
            """SELECT s.id AS segment_id, s.meeting_id, m.title, m.canonical_dir,
                      m.recording_date, m.project_id,
                      p.name AS project_name, p.color AS project_color,
                      'segment' AS match_kind, s.start_ms, s.end_ms,
                      s.speaker_name, s.speaker_label, s.text, e.dimensions, e.vector
                 FROM embeddings e
                 JOIN segments s ON s.id = e.segment_id
                 JOIN meetings m ON m.current_transcript_version_id = s.version_id
                 LEFT JOIN projects p ON p.id = m.project_id
                WHERE e.model = ?""",
            (self.settings.semantic_model,),
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
