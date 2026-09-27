"""材料内容（第三期 3a）：按内容认文件，后台慢慢读正文、认字、转写。

- 内容标识（content_key）：16MB 以内的文件整份算 sha256；更大的用大小、开头 64KB、结尾 64KB、
  中间均匀取的 8 块各 4KB。前缀 q2:，取前 32 位十六进制。不用 inode（exFAT 上挪位置会变），
  也不凭修改时间判断内容：大小或修改时间变了只重算标识，标识没变就不重读。挪位置、改名、复制因此
  都不重读。
- 算标识前先 stat：修改时间在 2 分钟以内（音视频 10 分钟以内）或大小和行里的不一样，这一轮跳过；
  写回用带条件的短事务（id 相同、大小和修改时间没变、没有 gone_at、根目录路径没变），同一个事务
  里写标识、INSERT OR IGNORE 内容行、清掉这份内容的 orphan_since。
- 读的时候出错一律先查盘，盘不在线就停这一轮，什么都不写。盘在线时：PermissionError 记在文件行上，
  24 小时后再试；其余 OSError 记 io，1 小时后、再 24 小时后各试一次，第三次才记 corrupt。
- 读正文、认字、转写之前和之后各 stat 一次，大小和修改时间都要等于算标识时的，否则结果丢掉。
- 没人引用的内容记 orphan_since；超过 30 天、所有根目录在线、文件名索引都扫完才删。
- 读盘、认字、转写时不拿数据库连接；每个文件的结果用一个短事务写入；数据库忙就结束这一轮。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import stat as stat_module
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .db import Database, utc_now
from .material_helpers import HelperStopped, StopFlag
from .material_rules import (
    CONTENT_EXTS,
    CONTENT_LAYERS,
    IWORK_EXTS,
    LAYER_MEDIA,
    LAYER_UNSUPPORTED,
    layer_for_ext,
)
from .materials import ROOT_ONLINE, volume_state

logger = logging.getLogger(__name__)

KEY_PREFIX = "q2:"
WHOLE_HASH_LIMIT = 16 * 1024 * 1024
EDGE_BYTES = 64 * 1024
SAMPLE_BLOCKS = 8
SAMPLE_BYTES = 4 * 1024
READ_CHUNK = 1024 * 1024

SETTLE_SECONDS = 120
MEDIA_SETTLE_SECONDS = 600
PERMISSION_RETRY = timedelta(hours=24)
IO_FIRST_RETRY = timedelta(hours=1)
IO_SECOND_RETRY = timedelta(hours=24)
IO_ATTEMPTS_BEFORE_CORRUPT = 3
ORPHAN_AFTER = timedelta(days=30)
ORPHAN_EVERY_SECONDS = 600.0

ROUND_SECONDS = 20.0
WORK_LOOP_SECONDS = 10.0
IDLE_LOOP_SECONDS = 60.0
KEY_BATCH = 200
EXTRACT_BATCH = 50

ERROR_PERMISSION = "permission"
ERROR_IO = "io"
ERROR_CORRUPT = "corrupt"
ERROR_UNSUPPORTED = "unsupported"

_CONTENT_EXTS_SQL = ", ".join(f"'{ext}'" for ext in sorted(CONTENT_EXTS | IWORK_EXTS))
_KEYED_EXTS_SQL = ", ".join(f"'{ext}'" for ext in sorted(CONTENT_EXTS))


class _EndRound(Exception):
    """结束这一轮（数据库忙、一次 IO 错、会议开始转写、服务在关闭）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _RootOffline(Exception):
    pass


# ---------------------------------------------------------------------- 内容标识


def package_main_file(path: Path) -> Path | None:
    """目录形式的包（.rtfd）用里面的主文件算标识。"""
    main = path / "TXT.rtf"
    if main.is_file():
        return main
    try:
        candidates = sorted(child for child in path.iterdir() if child.suffix.lower() == ".rtf")
    except OSError:
        raise
    return candidates[0] if candidates else None


def compute_content_key(path: Path, *, check: Callable[[], None] | None = None) -> tuple[str, int]:
    """返回 (content_key, 算的时候读到的大小)。目录形式的包用主文件。"""
    target = path
    if path.is_dir():
        main = package_main_file(path)
        if main is None:
            raise FileNotFoundError(f"包里没有主文件：{path}")
        target = main
    with target.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        digest = hashlib.sha256()
        if size <= WHOLE_HASH_LIMIT:
            while True:
                block = handle.read(READ_CHUNK)
                if not block:
                    break
                digest.update(block)
        else:
            digest.update(f"{size}\0".encode("ascii"))
            digest.update(handle.read(EDGE_BYTES))
            span = size - 2 * EDGE_BYTES - SAMPLE_BYTES
            for index in range(1, SAMPLE_BLOCKS + 1):
                handle.seek(EDGE_BYTES + span * index // (SAMPLE_BLOCKS + 1))
                digest.update(handle.read(SAMPLE_BYTES))
            handle.seek(size - EDGE_BYTES)
            digest.update(handle.read(EDGE_BYTES))
        if check is not None:
            check()
    return KEY_PREFIX + digest.hexdigest()[:32], size


def stat_signature(path: Path) -> tuple[int | None, int]:
    """(大小, 修改时间)：和文件名索引写行时一样，包的大小为空。"""
    info = os.stat(path, follow_symlinks=False)
    if stat_module.S_ISDIR(info.st_mode):
        return None, info.st_mtime_ns
    return info.st_size, info.st_mtime_ns


# ---------------------------------------------------------------------- 读取结果


@dataclass
class ExtractResult:
    """读取一份内容的结果。status：ok，或 io_error、password、corrupt、unsupported、timeout、
    permission、waiting（识别程序没装，note=engine_missing）、relayer（其实是别的层）。"""

    status: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    chars: int = 0
    pages: int | None = None
    duration_ms: int | None = None
    truncated: bool = False
    note: str | None = None
    extractor: str | None = None
    extractor_version: int | None = None
    what: str | None = None
    layer: str | None = None  # status=relayer 时：看开头字节后该换到的层
    # 3d 录音：已经合好的片段（带 start_ms、end_ms，每段不超过 400 字），不再按段落切
    spans: list[dict[str, Any]] | None = None
    sha256: str | None = None
    meeting_id: str | None = None


class Extractor(Protocol):
    def __call__(self, path: Path, layer: str, row: dict[str, Any]) -> ExtractResult: ...


# ---------------------------------------------------------------------- 循环


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _locked(error: sqlite3.OperationalError) -> bool:
    text = str(error)
    return "locked" in text or "busy" in text


class MaterialContent:
    """材料内容的后台循环：每轮先算标识，再读内容（读取器按层注册，3b 起接上），再清理没人引用的。"""

    def __init__(
        self,
        db: Database,
        settings: Any,
        *,
        state_of: Callable[[str], str] = volume_state,
        busy_check: Callable[[], bool] | None = None,
        stop: StopFlag | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        wall: Callable[[], float] = time.time,
        round_seconds: float = ROUND_SECONDS,
        extractors: dict[str, Extractor] | None = None,
        before_round: Callable[[], Any] | None = None,
        fts_rebuilding: Callable[[], bool] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.state_of = state_of
        self.busy_check = busy_check
        self.stop = stop
        self.clock = clock
        self.now = now or (lambda: datetime.now(UTC))
        self.wall = wall
        self.round_seconds = round_seconds
        self.extractors: dict[str, Extractor] = dict(extractors or {})
        self.before_round = before_round
        # 3f：恢复备份后全文表还没补完时，只算内容标识，不写、不删 material_chunks
        self.fts_rebuilding = fts_rebuilding
        self._last_orphan_pass: float | None = None
        self._running = threading.Lock()
        self.progress: dict[str, Any] = {"pending": 0, "paused": None, "offline_pending": 0}

    def close(self) -> None:
        """服务关闭：关掉各层读取器的常驻进程。"""
        for extractor in self.extractors.values():
            close = getattr(extractor, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.exception("关闭材料读取器失败")

    def wait_idle(self, timeout: float) -> bool:
        """服务关闭时等正在跑的这一轮返回（停止标记置上后最多 1 秒左右）。"""
        if self._running.acquire(timeout=timeout):
            self._running.release()
            return True
        return False

    # ------------------------------------------------------------------ 一轮

    def run_round(self) -> dict[str, Any]:
        """一轮：返回做了多少，有没有活（决定下一轮隔多久）。"""
        with self._running:
            return self._run_round()

    def _run_round(self) -> dict[str, Any]:
        stats: dict[str, Any] = {"keyed": 0, "read": 0, "ended": None}
        if self._stopping():
            stats["ended"] = "stopping"
            return stats
        if self.busy_check is not None and self.busy_check():
            stats["ended"] = "busy"
            self._refresh_progress(paused="busy")
            return stats
        if self.before_round is not None:
            # 3c：每 10 分钟看一次认字、转写程序有没有新装上（没到时间立刻返回）
            try:
                self.before_round()
            except Exception:  # noqa: BLE001
                logger.exception("检查材料读取程序失败")
        deadline = self.clock() + self.round_seconds
        roots = {
            int(row["id"]): row["path"]
            for row in self.db.query_all("SELECT id, path FROM project_material_roots ORDER BY id")
        }
        online = {root_id: path for root_id, path in roots.items() if self.state_of(path) == ROOT_ONLINE}
        self._offline_now: set[int] = set()
        rebuilding = self.chunks_frozen()
        try:
            if online:
                stats["keyed"] = self._key_pass(online, deadline)
                if not rebuilding:
                    stats["read"] = self._extract_pass(online, deadline)
            if rebuilding:
                stats["ended"] = "fts_rebuild"
            else:
                self._maybe_orphan_pass(roots, online)
        except _EndRound as end:
            stats["ended"] = end.reason
        except sqlite3.OperationalError as error:
            if not _locked(error):
                raise
            stats["ended"] = "db_busy"
        paused = "busy" if stats["ended"] == "busy" else None
        try:
            self._refresh_progress(paused=paused)
        except sqlite3.OperationalError as error:
            if not _locked(error):
                raise
        stats["work"] = bool(stats["keyed"] or stats["read"]) or stats["ended"] in {"budget"}
        return stats

    def _stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def chunks_frozen(self) -> bool:
        """恢复备份后全文表还没补完：在没补进去的片段上删除会报 malformed，补到时再插又会重复索引。"""
        if self.fts_rebuilding is None:
            return False
        try:
            return bool(self.fts_rebuilding())
        except Exception:  # noqa: BLE001
            return True

    def _checkpoint(self, deadline: float) -> None:
        """每处理完一个文件查一次：停止标记、忙信号、预算。"""
        if self._stopping():
            raise _EndRound("stopping")
        if self.busy_check is not None and self.busy_check():
            raise _EndRound("busy")
        if self.clock() >= deadline:
            raise _EndRound("budget")

    # ------------------------------------------------------------------ 算标识

    def _priority_sql(self, online: dict[int, str]) -> tuple[str, list[Any]]:
        """排序：会上被提到过的、需求文件夹里的、其余；同一档按修改时间从新到旧。"""
        folders: list[tuple[int, str]] = []
        for row in self.db.query_all("SELECT path FROM requirement_folders"):
            folder = os.path.normpath(str(row["path"]))
            for root_id, root_path in online.items():
                base = os.path.normpath(root_path)
                if folder == base:
                    folders.append((root_id, ""))
                elif folder.startswith(base + os.sep):
                    folders.append((root_id, os.path.relpath(folder, base).replace(os.sep, "/")))
        params: list[Any] = []
        clauses = []
        for root_id, rel in folders[:50]:
            if rel:
                clauses.append("(f.root_id = ? AND f.rel_path >= ? || '/' AND f.rel_path < ? || '0')")
                params.extend([root_id, rel, rel])
            else:
                clauses.append("(f.root_id = ?)")
                params.append(root_id)
        requirement = " OR ".join(clauses) if clauses else "0"
        sql = f"""CASE
            WHEN EXISTS (SELECT 1 FROM meeting_file_mentions fm
                          WHERE fm.file_id = f.id AND fm.status = 'active') THEN 1
            WHEN {requirement} THEN 2
            ELSE 3 END"""
        return sql, params

    def _retry_sql(self) -> tuple[str, list[Any]]:
        moment = self.now()
        return (
            """(
                (f.content_key IS NULL AND f.content_error IS NULL)
                OR f.content_size IS NOT f.size OR f.content_mtime_ns IS NOT f.mtime_ns
                OR (f.content_error = 'permission' AND f.content_checked_at <= ?)
                OR (f.content_error = 'io' AND COALESCE(f.content_attempts, 1) <= 1
                    AND f.content_checked_at <= ?)
                OR (f.content_error = 'io' AND f.content_attempts >= 2 AND f.content_checked_at <= ?)
            )""",
            [
                _iso(moment - PERMISSION_RETRY),
                _iso(moment - IO_FIRST_RETRY),
                _iso(moment - IO_SECOND_RETRY),
            ],
        )

    def _needs_key_sql(self) -> str:
        return "(f.content_key IS NULL OR f.content_size IS NOT f.size OR f.content_mtime_ns IS NOT f.mtime_ns)"

    def key_candidates(self, online: dict[int, str], limit: int = KEY_BATCH) -> list[dict[str, Any]]:
        """要算标识的活文件，按优先级排好。第一档是被［换成这份］选过、又需要重找的（含只收
        文件名的文件），然后是会上提到的、需求文件夹里的、其余按修改时间从新到旧。"""
        if not online:
            return []
        marks = ", ".join("?" for _ in online)
        retry_sql, retry_params = self._retry_sql()
        chosen: dict[int, dict[str, Any]] = {}
        # 第一档：picked 的提到指着的文件不见了，同词干组里还没算标识的活文件
        for row in self.db.query_all(
            f"""SELECT DISTINCT f.* FROM meeting_file_mentions fm
                  JOIN material_files g ON g.id = fm.file_id AND g.gone_at IS NOT NULL
                  JOIN project_material_roots r ON r.project_id = fm.project_id
                  JOIN material_files f ON f.root_id = r.id AND f.stem_key = fm.stem_key
                 WHERE fm.picked = 1 AND fm.status = 'active' AND f.gone_at IS NULL
                   AND f.root_id IN ({marks}) AND f.zone != 'cards'
                   AND {self._needs_key_sql()} AND {retry_sql}
                 LIMIT ?""",
            (*online, *retry_params, limit),
        ):
            chosen.setdefault(int(row["id"]), dict(row))
        # 第一档：交付物指着的内容找不到活的副本了，同项目里同名或同大小的活文件
        for row in self._lost_deliverable_candidates(online, retry_sql, retry_params, limit):
            chosen.setdefault(int(row["id"]), row)
        if len(chosen) < limit:
            priority_sql, priority_params = self._priority_sql(online)
            for row in self.db.query_all(
                f"""SELECT f.*, {priority_sql} AS priority FROM material_files f
                     WHERE f.root_id IN ({marks}) AND f.gone_at IS NULL AND f.zone != 'cards'
                       AND f.ext IN ({_CONTENT_EXTS_SQL}) AND {retry_sql}
                     ORDER BY priority, f.mtime_ns DESC, f.id
                     LIMIT ?""",
                (*priority_params, *online, *retry_params, limit - len(chosen)),
            ):
                chosen.setdefault(int(row["id"]), dict(row))
        return list(chosen.values())

    def _lost_deliverable_candidates(
        self, online: dict[int, str], retry_sql: str, retry_params: list[Any], limit: int
    ) -> list[dict[str, Any]]:
        lost = self.db.query_all(
            """SELECT df.content_key, df.rel_path, t.project_id,
                      (SELECT MAX(g.content_size) FROM material_files g WHERE g.content_key = df.content_key)
                          AS size
                 FROM deliverable_files df
                 JOIN deliverables d ON d.id = df.deliverable_id
                 JOIN tasks t ON t.id = d.task_id
                WHERE df.content_key IS NOT NULL AND t.project_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM material_files x
                       WHERE x.content_key = df.content_key AND x.gone_at IS NULL
                         AND x.content_size IS x.size AND x.content_mtime_ns IS x.mtime_ns)"""
        )
        if not lost:
            return []
        marks = ", ".join("?" for _ in online)
        found: list[dict[str, Any]] = []
        for item in lost[:20]:
            name = str(item["rel_path"] or "").rpartition("/")[2]
            for row in self.db.query_all(
                f"""SELECT f.* FROM material_files f
                      JOIN project_material_roots r ON r.id = f.root_id
                     WHERE r.project_id = ? AND f.gone_at IS NULL AND f.root_id IN ({marks})
                       AND f.zone != 'cards' AND (f.name = ? OR (? IS NOT NULL AND f.size = ?))
                       AND {self._needs_key_sql()} AND {retry_sql}
                     LIMIT ?""",
                (item["project_id"], *online, name, item["size"], item["size"], *retry_params, limit),
            ):
                found.append(dict(row))
        return found

    def _key_pass(self, online: dict[int, str], deadline: float) -> int:
        keyed = 0
        for row in self.key_candidates(online):
            if int(row["root_id"]) in self._offline_now:
                continue
            try:
                keyed += int(self.key_file(row, online[int(row["root_id"])]))
            except _RootOffline:
                self._offline_now.add(int(row["root_id"]))
            self._checkpoint(deadline)
        return keyed

    def key_file(self, row: dict[str, Any], root_path: str) -> bool:
        """给一行算标识并写回。返回是否写了标识。"""
        ext = str(row["ext"] or "").lower()
        path = Path(root_path).joinpath(*str(row["rel_path"]).split("/"))
        if ext in IWORK_EXTS:
            return self._mark_error(row, root_path, ERROR_UNSUPPORTED, signature=(row["size"], row["mtime_ns"]))
        try:
            size, mtime_ns = stat_signature(path)
        except FileNotFoundError:
            return False  # 文件名索引下一轮会标不见
        except OSError as error:
            self._on_os_error(row, root_path, error)
            return False
        if size != row["size"] or mtime_ns != row["mtime_ns"]:
            # 还在拷贝或刚改过：顺手把行里的大小和修改时间改成实时值，这一轮跳过
            self._refresh_signature(row, root_path, size, mtime_ns)
            return False
        layer = layer_for_ext(ext)
        settle = MEDIA_SETTLE_SECONDS if layer == LAYER_MEDIA else SETTLE_SECONDS
        if self.wall() - mtime_ns / 1e9 < settle:
            return False
        try:
            key, _read_size = compute_content_key(path)
            after = stat_signature(path)
        except FileNotFoundError:
            return False
        except OSError as error:
            self._on_os_error(row, root_path, error)
            return False
        if after != (size, mtime_ns):
            return False  # 算的时候文件变了，留给下一轮
        return self._write_key(row, root_path, key, layer)

    def _same_root(self, connection: sqlite3.Connection, row: dict[str, Any], root_path: str) -> bool:
        current = connection.execute(
            "SELECT path FROM project_material_roots WHERE id = ?", (row["root_id"],)
        ).fetchone()
        return current is not None and current["path"] == root_path

    def _write_key(self, row: dict[str, Any], root_path: str, key: str, layer: str | None) -> bool:
        now = utc_now()
        with self.db.transaction() as connection:
            if not self._same_root(connection, row, root_path):
                return False
            # IO 错的次数只在文件变了时清零：算标识成功不代表读内容也成功
            updated = connection.execute(
                """UPDATE material_files
                      SET content_key = ?, content_size = size, content_mtime_ns = mtime_ns,
                          content_error = NULL,
                          content_attempts = CASE WHEN content_size IS size AND content_mtime_ns IS mtime_ns
                                                  THEN content_attempts END,
                          content_checked_at = ?
                    WHERE id = ? AND size IS ? AND mtime_ns IS ? AND gone_at IS NULL""",
                (key, now, row["id"], row["size"], row["mtime_ns"]),
            ).rowcount
            if updated != 1:
                return False
            if layer in CONTENT_LAYERS and row.get("zone") != "cards":
                connection.execute(
                    """INSERT OR IGNORE INTO material_contents(content_key, layer, state, size, created_at, updated_at)
                       VALUES (?, ?, 'pending', ?, ?, ?)""",
                    (key, layer, row["size"], now, now),
                )
                connection.execute(
                    "UPDATE material_contents SET orphan_since = NULL WHERE content_key = ? AND orphan_since IS NOT NULL",
                    (key,),
                )
            follow_content(connection, key)
        return True

    def _refresh_signature(self, row: dict[str, Any], root_path: str, size: int | None, mtime_ns: int) -> None:
        with self.db.transaction() as connection:
            if not self._same_root(connection, row, root_path):
                return
            connection.execute(
                """UPDATE material_files SET size = ?, mtime_ns = ?
                    WHERE id = ? AND size IS ? AND mtime_ns IS ? AND gone_at IS NULL""",
                (size, mtime_ns, row["id"], row["size"], row["mtime_ns"]),
            )

    def _mark_error(
        self,
        row: dict[str, Any],
        root_path: str,
        error: str,
        *,
        signature: tuple[int | None, int | None],
        attempts: int | None = None,
        keep_key: bool = False,
    ) -> bool:
        """在文件行上记出错。keep_key：读内容时出的错，标识仍然对得上这一版文件，留着；算标识时
        出的错把标识清掉（旧标识可能是上一版的）。"""
        with self.db.transaction() as connection:
            if not self._same_root(connection, row, root_path):
                return False
            connection.execute(
                f"""UPDATE material_files
                      SET content_key = {"content_key" if keep_key else "NULL"}, content_error = ?,
                          content_attempts = ?, content_size = ?, content_mtime_ns = ?, content_checked_at = ?
                    WHERE id = ? AND size IS ? AND mtime_ns IS ? AND gone_at IS NULL""",
                (error, attempts, signature[0], signature[1], _iso(self.now()), row["id"], row["size"],
                 row["mtime_ns"]),
            )
        return False

    def _on_os_error(
        self, row: dict[str, Any], root_path: str, error: OSError, *, keep_key: bool = False
    ) -> None:
        """读的时候出错：先查盘；盘不在线就停这个根目录这一轮，什么都不写。"""
        if self.state_of(root_path) != ROOT_ONLINE:
            raise _RootOffline(root_path)
        signature = (row["size"], row["mtime_ns"])
        if isinstance(error, PermissionError):
            self._mark_error(row, root_path, ERROR_PERMISSION, signature=signature, keep_key=keep_key,
                             attempts=row.get("content_attempts"))
            return
        # 同一版文件（大小和修改时间没变）连着出 IO 错才累计；读成功一次就清零
        same_file = row.get("content_size") == row["size"] and row.get("content_mtime_ns") == row["mtime_ns"]
        previous = int(row.get("content_attempts") or 0) if same_file else 0
        attempts = previous + 1
        if attempts >= IO_ATTEMPTS_BEFORE_CORRUPT:
            self._mark_error(row, root_path, ERROR_CORRUPT, signature=signature, attempts=attempts,
                             keep_key=keep_key)
        else:
            self._mark_error(row, root_path, ERROR_IO, signature=signature, attempts=attempts, keep_key=keep_key)
        # 资料盘刚醒来时会短暂报 EIO，这一轮到此结束
        raise _EndRound("io")

    # ------------------------------------------------------------------ 读内容

    def extract_candidates(
        self, online: dict[int, str], limit: int = EXTRACT_BATCH, *, layers: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """要读的内容：有读取器的层、state=pending、到了该试的时间，每份内容挑一个在线的副本。
        录音和视频由自己的循环来挑（layers=[media]），顺序一样。"""
        if layers is None:
            # 读取器可以说「这一层这会儿先别读」（例如 Vision 程序正在编译）
            layers = [
                layer
                for layer, extractor in self.extractors.items()
                if layer != LAYER_MEDIA and getattr(extractor, "available", lambda: True)()
            ]
        if not layers or not online:
            return []
        marks = ", ".join("?" for _ in online)
        layer_marks = ", ".join("?" for _ in layers)
        priority_sql, priority_params = self._priority_sql(online)
        rows = self.db.query_all(
            f"""SELECT c.content_key, c.layer, c.attempts, c.reason, c.state,
                       f.id, f.root_id, f.rel_path, f.name, f.ext, f.zone, f.size, f.mtime_ns,
                       f.content_size, f.content_mtime_ns, f.content_error, f.content_attempts,
                       {priority_sql} AS priority
                  FROM material_contents c
                  JOIN material_files f ON f.content_key = c.content_key
                 WHERE c.state = 'pending' AND c.layer IN ({layer_marks})
                   AND (c.next_try_at IS NULL OR c.next_try_at <= ?)
                   AND f.gone_at IS NULL AND f.root_id IN ({marks}) AND f.zone != 'cards'
                   AND f.content_size IS f.size AND f.content_mtime_ns IS f.mtime_ns
                   AND f.content_error IS NULL
                 ORDER BY CASE c.layer WHEN 'text' THEN 0 ELSE 1 END, priority, f.mtime_ns DESC, f.id
                 LIMIT ?""",
            (*priority_params, *layers, _iso(self.now()), *online, limit * 4),
        )
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["content_key"] in seen:
                continue
            seen.add(row["content_key"])
            result.append(row)
            if len(result) >= limit:
                break
        return result

    def _extract_pass(self, online: dict[int, str], deadline: float) -> int:
        read = 0
        candidates = self.extract_candidates(online)
        if not candidates and self.queue_rereads(online):
            candidates = self.extract_candidates(online)
        for row in candidates:
            if int(row["root_id"]) in self._offline_now:
                continue
            try:
                read += int(self.extract_one(row, online[int(row["root_id"])]))
            except _RootOffline:
                self._offline_now.add(int(row["root_id"]))
            self._checkpoint(deadline)
        return read

    def queue_rereads(self, online: dict[int, str], limit: int = EXTRACT_BATCH) -> int:
        """读取程序升级了（extractor_version 变大）：没有别的活时，把旧版本读的内容低优先级地重读。
        只挑有在线副本的，免得盘不在时读好的内容变成等待读取。"""
        if not online:
            return 0
        marks = ", ".join("?" for _ in online)
        queued = 0
        for layer, extractor in self.extractors.items():
            version = getattr(extractor, "version", None)
            if layer == LAYER_MEDIA or not version:
                continue
            with self.db.transaction() as connection:
                queued += connection.execute(
                    f"""UPDATE material_contents
                           SET state = 'pending', reason = NULL, attempts = 0, next_try_at = NULL, updated_at = ?
                         WHERE content_key IN (
                               SELECT c.content_key FROM material_contents c
                                WHERE c.layer = ? AND c.extractor_version IS NOT NULL AND c.extractor_version < ?
                                  AND (c.state = 'done' OR (c.state = 'unreadable' AND c.reason IN ('corrupt', 'unsupported')))
                                  AND EXISTS (
                                      SELECT 1 FROM material_files f
                                       WHERE f.content_key = c.content_key AND f.gone_at IS NULL
                                         AND f.root_id IN ({marks}) AND f.content_error IS NULL)
                                LIMIT ?)""",
                    (utc_now(), layer, int(version), *online, limit),
                ).rowcount
        return queued

    def extract_one(self, row: dict[str, Any], root_path: str) -> bool:
        extractor = self.extractors.get(row["layer"])
        if extractor is None:
            return False
        path = Path(root_path).joinpath(*str(row["rel_path"]).split("/"))
        expected = (row["content_size"], row["content_mtime_ns"])
        try:
            before = stat_signature(path)
        except FileNotFoundError:
            return False
        except OSError as error:
            self._on_os_error(row, root_path, error, keep_key=True)
            return False
        if before != expected:
            return False  # 下一轮先重算标识
        try:
            result = extractor(path, row["layer"], row)
        except HelperStopped as stopped:
            # 服务在关、或者会议开始转写：这份下一轮从头读，什么都不写
            raise _EndRound(stopped.reason or "stopping") from stopped
        return self.finish_extract(row, root_path, path, expected, result)

    def finish_extract(
        self,
        row: dict[str, Any],
        root_path: str,
        path: Path,
        expected: tuple[int | None, int | None],
        result: ExtractResult,
    ) -> bool:
        """读完一份（3d 的录音转写也走这里）：再 stat 一次，没变才写；读不了的先查盘。"""
        if result.status == "io_error":
            self._on_os_error(row, root_path, OSError("io_error"), keep_key=True)
            return False
        if result.status == "permission":
            self._on_os_error(row, root_path, PermissionError("permission"), keep_key=True)
            return False
        try:
            after = stat_signature(path)
        except FileNotFoundError:
            return False
        except OSError as error:
            self._on_os_error(row, root_path, error, keep_key=True)
            return False
        if after != expected:
            return False  # 读的途中文件被改了：结果不写到旧标识下
        if result.status in {"password", "corrupt", "unsupported", "timeout"} and self.state_of(root_path) != ROOT_ONLINE:
            raise _RootOffline(root_path)
        with self.db.transaction() as connection:
            if not self._same_root(connection, row, root_path):
                return False
            current = connection.execute(
                "SELECT state, attempts FROM material_contents WHERE content_key = ?", (row["content_key"],)
            ).fetchone()
            if current is None:
                return False
            store_result(connection, row["content_key"], result, attempts=int(current["attempts"] or 0),
                         now=self.now())
            if row.get("content_attempts"):
                connection.execute(
                    "UPDATE material_files SET content_attempts = NULL WHERE id = ? AND content_error IS NULL",
                    (row["id"],),
                )
        return True

    # ------------------------------------------------------------------ 没人引用的内容

    def _maybe_orphan_pass(self, roots: dict[int, str], online: dict[int, str]) -> None:
        now = self.clock()
        if self._last_orphan_pass is not None and now - self._last_orphan_pass < ORPHAN_EVERY_SECONDS:
            return
        self._last_orphan_pass = now
        self.orphan_pass(all_online=bool(roots) and len(online) == len(roots) or not roots)

    def orphan_pass(self, *, all_online: bool) -> dict[str, int]:
        """记下、清掉 orphan_since；超过 30 天、所有根目录在线、文件名索引都扫完才删。"""
        now = utc_now()
        with self.db.transaction() as connection:
            # 文件行的标识在内容表里找不到（刚被清理掉）：重新插一行 pending
            revived = connection.execute(
                f"""INSERT OR IGNORE INTO material_contents(content_key, layer, state, size, created_at, updated_at)
                    SELECT f.content_key,
                           CASE WHEN f.ext = 'pdf' THEN 'pdf'
                                WHEN f.ext IN ({", ".join(f"'{ext}'" for ext in sorted(_image_exts()))}) THEN 'image'
                                WHEN f.ext IN ({", ".join(f"'{ext}'" for ext in sorted(_media_exts()))}) THEN 'media'
                                ELSE 'text' END,
                           'pending', MAX(f.size), ?, ?
                      FROM material_files f
                     WHERE f.content_key IS NOT NULL AND f.gone_at IS NULL AND f.zone != 'cards'
                       AND f.ext IN ({_KEYED_EXTS_SQL})
                       AND NOT EXISTS (SELECT 1 FROM material_contents c WHERE c.content_key = f.content_key)
                     GROUP BY f.content_key""",
                (now, now),
            ).rowcount
            marked = connection.execute(
                """UPDATE material_contents SET orphan_since = ?
                    WHERE orphan_since IS NULL AND NOT EXISTS (
                        SELECT 1 FROM material_files f
                         WHERE f.content_key = material_contents.content_key AND f.gone_at IS NULL)""",
                (now,),
            ).rowcount
            cleared = connection.execute(
                """UPDATE material_contents SET orphan_since = NULL
                    WHERE orphan_since IS NOT NULL AND EXISTS (
                        SELECT 1 FROM material_files f
                         WHERE f.content_key = material_contents.content_key AND f.gone_at IS NULL)"""
            ).rowcount
            deleted = 0
            if all_online and self._index_done(connection):
                deleted = connection.execute(
                    """DELETE FROM material_contents
                        WHERE orphan_since < ? AND NOT EXISTS (
                            SELECT 1 FROM material_files f
                             WHERE f.content_key = material_contents.content_key AND f.gone_at IS NULL)""",
                    (_iso(self.now() - ORPHAN_AFTER),),
                ).rowcount
        return {"revived": revived, "marked": marked, "cleared": cleared, "deleted": deleted}

    @staticmethod
    def _index_done(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            """SELECT COUNT(*) AS n FROM project_material_roots r
                 LEFT JOIN material_index_state s ON s.root_id = r.id
                WHERE COALESCE(s.state, 'pending') != 'done'"""
        ).fetchone()
        return int(row["n"]) == 0

    # ------------------------------------------------------------------ 进度（只给首页）

    def _refresh_progress(self, *, paused: str | None) -> None:
        counts = pending_counts(self.db, online=None, state_of=self.state_of)
        self.progress = {
            "pending": counts["pending"],
            "offline_pending": counts["offline_pending"],
            "paused": paused,
        }


def _image_exts() -> frozenset[str]:
    from .material_rules import IMAGE_EXTS

    return IMAGE_EXTS


def _media_exts() -> frozenset[str]:
    from .material_rules import MEDIA_EXTS

    return MEDIA_EXTS


def pending_counts(
    db: Database,
    *,
    online: set[int] | None = None,
    state_of: Callable[[str], str] = volume_state,
    root_ids: list[int] | None = None,
) -> dict[str, int]:
    """还没读的活文件：扩展名在内容层、不在声档会议记录里、content_error 为空，而且没有内容标识
    或内容 state=pending。不含 waiting 和读不了的。offline_pending 是其中在没插的盘上的。
    root_ids：只数这几个根目录（搜索选了项目时）。"""
    root_clause = ""
    if root_ids is not None:
        if not root_ids:
            return {"pending": 0, "offline_pending": 0}
        root_clause = f" AND f.root_id IN ({', '.join(str(int(root_id)) for root_id in root_ids)})"
    rows = db.query_all(
        f"""SELECT f.root_id, COUNT(*) AS n
              FROM material_files f
              LEFT JOIN material_contents c ON c.content_key = f.content_key
             WHERE f.gone_at IS NULL AND f.zone != 'cards' AND f.ext IN ({_KEYED_EXTS_SQL})
               AND f.content_error IS NULL
               AND (f.content_key IS NULL OR c.state = 'pending' OR c.content_key IS NULL){root_clause}
             GROUP BY f.root_id"""
    )
    if not rows:
        return {"pending": 0, "offline_pending": 0}
    if online is None:
        paths = {int(row["id"]): row["path"] for row in db.query_all("SELECT id, path FROM project_material_roots")}
        online = {root_id for root_id, path in paths.items() if state_of(path) == ROOT_ONLINE}
    total = sum(int(row["n"]) for row in rows)
    offline = sum(int(row["n"]) for row in rows if int(row["root_id"]) not in online)
    return {"pending": total, "offline_pending": offline}


CHUNK_CHARS = 400
MAX_CONTENT_CHARS = 200_000
TIMEOUT_RETRY = timedelta(hours=1)
_SENTENCE_END = re.compile(r"(?<=[。？！；?!;\n])|(?<=\.)(?=\s)")


def _split_long(text: str, limit: int) -> list[str]:
    """一段超过 400 字：在句号、问号、叹号、分号、换行处切，一句还超过的硬切。"""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        while len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def chunk_blocks(
    blocks: list[dict[str, Any]], *, limit: int = CHUNK_CHARS, max_chars: int = MAX_CONTENT_CHARS
) -> tuple[list[tuple[str | None, str]], bool]:
    """按段落切成片段：每段不超过 400 字；同一个位置里相邻的短段合并到 400 字以内。
    每份内容最多收 20 万字，多的丢掉。返回 (片段 [(loc, text)], 是否截断)。"""
    chunks: list[tuple[str | None, str]] = []
    total = 0
    truncated = False
    buffer = ""
    buffer_loc: str | None = None

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append((buffer_loc, buffer.strip()))
        buffer = ""

    for block in blocks:
        loc = block.get("loc") or None
        text = str(block.get("text") or "").replace("\x00", "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            text = text[: max(max_chars - total, 0)]
            truncated = True
        total += len(text)
        if loc != buffer_loc:
            flush()
            buffer_loc = loc
        for piece in _split_long(text, limit) if len(text) > limit else [text]:
            if buffer and len(buffer) + 1 + len(piece) <= limit:
                buffer = f"{buffer}\n{piece}"
            else:
                flush()
                buffer = piece
        if truncated:
            break
    flush()
    return chunks, truncated


def store_result(
    connection: sqlite3.Connection,
    content_key: str,
    result: ExtractResult,
    *,
    attempts: int,
    now: datetime,
) -> None:
    """把一份内容的读取结果写进去（在 extract_one 的短事务里）：片段和状态同一个事务。"""
    stamp = utc_now()
    if result.status == "ok":
        if result.spans is not None:
            rows, cut = _cap_spans(result.spans)
        else:
            chunks, cut = chunk_blocks(result.blocks)
            rows = [{"loc": loc, "start_ms": None, "end_ms": None, "text": text} for loc, text in chunks]
        truncated = result.truncated or cut
        note = "truncated" if truncated else result.note
        connection.execute("DELETE FROM material_chunks WHERE content_key = ?", (content_key,))
        connection.executemany(
            """INSERT INTO material_chunks(content_key, ordinal, loc, start_ms, end_ms, text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (content_key, ordinal, row.get("loc"), row.get("start_ms"), row.get("end_ms"), row["text"])
                for ordinal, row in enumerate(rows)
            ],
        )
        connection.execute(
            """UPDATE material_contents SET state = 'done', reason = NULL, note = ?, extractor = ?,
                   extractor_version = ?, chars = ?, chunks = ?, pages = ?, duration_ms = ?, attempts = 0,
                   next_try_at = NULL, sha256 = COALESCE(?, sha256), meeting_id = ?, updated_at = ?
             WHERE content_key = ?""",
            (note, result.extractor, result.extractor_version, sum(len(row["text"]) for row in rows),
             len(rows), result.pages, result.duration_ms, result.sha256, result.meeting_id, stamp, content_key),
        )
        # 录音转完：断点行和片段同一个事务删掉，不留两份文字
        connection.execute("DELETE FROM material_media_jobs WHERE content_key = ?", (content_key,))
    elif result.status == "relayer" and result.layer in CONTENT_LAYERS:
        # 扩展名骗人（.doc 其实是 PDF）：换层，照旧等着读
        connection.execute(
            """UPDATE material_contents SET layer = ?, state = 'pending', reason = NULL, attempts = 0,
                   next_try_at = NULL, updated_at = ? WHERE content_key = ?""",
            (result.layer, stamp, content_key),
        )
    elif result.status == "waiting":
        connection.execute(
            """UPDATE material_contents SET state = 'waiting', reason = NULL, note = 'engine_missing',
                   updated_at = ? WHERE content_key = ?""",
            (stamp, content_key),
        )
    elif result.status == "timeout" and attempts == 0:
        # 第一次处理超时：一小时后用 120 秒再试一次
        connection.execute(
            """UPDATE material_contents SET state = 'pending', reason = 'timeout', attempts = 1,
                   next_try_at = ?, extractor = ?, extractor_version = ?, updated_at = ?
             WHERE content_key = ?""",
            (_iso(now + TIMEOUT_RETRY), result.extractor, result.extractor_version, stamp, content_key),
        )
    else:
        reason = result.status if result.status in {"password", "corrupt", "unsupported", "timeout"} else "corrupt"
        connection.execute(
            """UPDATE material_contents SET state = 'unreadable', reason = ?, note = NULL, attempts = ?,
                   next_try_at = NULL, extractor = ?, extractor_version = ?, updated_at = ?
             WHERE content_key = ?""",
            (reason, attempts + 1, result.extractor, result.extractor_version, stamp, content_key),
        )
        connection.execute("DELETE FROM material_media_jobs WHERE content_key = ?", (content_key,))


def _cap_spans(spans: list[dict[str, Any]], *, max_chars: int = MAX_CONTENT_CHARS) -> tuple[list[dict[str, Any]], bool]:
    """录音片段已经合好，只管每份内容最多 20 万字。"""
    rows: list[dict[str, Any]] = []
    total = 0
    for span in spans:
        text = str(span.get("text") or "").replace("\x00", "").strip()
        if not text:
            continue
        if total + len(text) > max_chars:
            return rows, True
        total += len(text)
        rows.append({"loc": span.get("loc"), "start_ms": span.get("start_ms"), "end_ms": span.get("end_ms"),
                     "text": text})
    return rows, False


def follow_content(connection: sqlite3.Connection, content_key: str) -> int:
    """算出一个标识时：已不见的行带同一个标识、且被 picked 的提到或交付物引用，就让相关的会重新比对。"""
    return connection.execute(
        """UPDATE meeting_file_scan SET dirty = dirty + 1
            WHERE meeting_id IN (
                SELECT fm.meeting_id FROM meeting_file_mentions fm
                  JOIN material_files g ON g.id = fm.file_id
                 WHERE fm.picked = 1 AND fm.status = 'active'
                   AND g.gone_at IS NOT NULL AND g.content_key = ?
                UNION
                SELECT t.meeting_id FROM deliverable_files df
                  JOIN deliverables d ON d.id = df.deliverable_id
                  JOIN tasks t ON t.id = d.task_id
                 WHERE df.content_key = ? AND t.meeting_id IS NOT NULL)""",
        (content_key, content_key),
    ).rowcount


def key_file_now(db: Database, file_id: int, *, state_of: Callable[[str], str] = volume_state) -> str | None:
    """［换成这份］和标交付物时当场算标识（只收文件名的文件也算，但不建内容行）。盘不在、读不了就算了。"""
    row = db.query_one(
        """SELECT f.*, r.path AS root_path FROM material_files f
             JOIN project_material_roots r ON r.id = f.root_id WHERE f.id = ?""",
        (file_id,),
    )
    if row is None or row["gone_at"] is not None:
        return None
    if row["content_key"] and row["content_size"] == row["size"] and row["content_mtime_ns"] == row["mtime_ns"]:
        return str(row["content_key"])
    if state_of(row["root_path"]) != ROOT_ONLINE:
        return None
    path = Path(row["root_path"]).joinpath(*str(row["rel_path"]).split("/"))
    try:
        size, mtime_ns = stat_signature(path)
        if size != row["size"] or mtime_ns != row["mtime_ns"]:
            return None
        key, _read = compute_content_key(path)
        if stat_signature(path) != (size, mtime_ns):
            return None
    except OSError:
        return None
    layer = layer_for_ext(str(row["ext"] or ""))
    now = utc_now()
    with db.transaction() as connection:
        current = connection.execute(
            "SELECT path FROM project_material_roots WHERE id = ?", (row["root_id"],)
        ).fetchone()
        if current is None or current["path"] != row["root_path"]:
            return None
        updated = connection.execute(
            """UPDATE material_files
                  SET content_key = ?, content_size = size, content_mtime_ns = mtime_ns,
                      content_error = NULL, content_attempts = NULL, content_checked_at = ?
                WHERE id = ? AND size IS ? AND mtime_ns IS ? AND gone_at IS NULL""",
            (key, now, file_id, row["size"], row["mtime_ns"]),
        ).rowcount
        if updated != 1:
            return None
        if layer in CONTENT_LAYERS and layer != LAYER_UNSUPPORTED and row["zone"] != "cards":
            connection.execute(
                """INSERT OR IGNORE INTO material_contents(content_key, layer, state, size, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?, ?)""",
                (key, layer, row["size"], now, now),
            )
            connection.execute(
                "UPDATE material_contents SET orphan_since = NULL WHERE content_key = ?", (key,)
            )
    return key
