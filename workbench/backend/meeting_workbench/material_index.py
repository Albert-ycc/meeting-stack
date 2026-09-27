"""文件名索引（第二期 2d）：在后台把项目材料文件夹里的文件名收进库，只存名字，不读内容。

- 每轮最多 3 秒或 5000 个条目；FunASR 在转写时让路。按根目录断点续扫：cursor 是 DFS 栈，
  只在每轮结束时写一次。读盘时不拿数据库连接，每读完一个目录用一个短事务写这个目录的结果。
- 目录修改时间没变就不重读，也不写任何东西（子目录照样往下走）；每 24 小时整轮重读一次。
- 「不见了」只按目录判断：重读一个目录时，这次没列出来的文件写 gone_at，重新出现的清掉；
  父目录里不见了的子目录，整棵子树写 gone_at。没重读的目录什么都不动。
- 读不了就停，不当成空：根目录读不了、盘不在线、根目录换了路径、数据库忙，立即结束这个根目录
  的本轮，不推进 cursor、不写 gone_at。子目录读失败只跳过这棵子树。
- 规则沿用 1f 盘点：系统影子文件和 Office 的 ~$ 锁文件跳过；node_modules、.git 和点开头的
  文件夹只读一层记个数；声档会议记录里的文件标 cards、代码和配置文件标 code，只收名字不比对；
  .key、.pages 这类包算一个文件；不跟随符号链接；受保护目录和嵌套的其他根目录整棵跳过。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .db import Database, utc_now
from .file_stems import derive_stem, stem_key
from .material_rules import silent_skip
from .material_walk import NAME_ONLY_DIRS, PACKAGE_EXTS, file_ext
from .materials import CARDS_DIR_NAME, ROOT_MISSING, ROOT_ONLINE, volume_state

logger = logging.getLogger(__name__)

ROUND_SECONDS = 3.0
ROUND_ENTRIES = 5000
LOOP_SECONDS = 60.0
FULL_EVERY = timedelta(hours=24)

# 代码和配置文件：收名字、不比对。
CODE_EXTS = frozenset(
    {
        "py", "pyc", "js", "mjs", "cjs", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml", "ini",
        "cfg", "conf", "css", "scss", "less", "lock", "log", "map", "sh", "bat", "ps1", "java",
        "class", "go", "rs", "c", "h", "cpp", "hpp", "cs", "rb", "php", "swift", "kt", "sql",
        "xml", "plist", "env", "gradle", "vue", "svelte", "o", "so", "dll", "dylib", "whl",
    }
)
# 拿来比对的文件区域。
MATCH_ZONES = ("normal", "package")

STATE_PENDING = "pending"
STATE_WALKING = "walking"
STATE_DONE = "done"
STATE_OFFLINE = "offline"
STATE_MISSING = "missing"
STATE_ERROR = "error"


class _Stop(Exception):
    """结束这个根目录的本轮：state 为 None 时不改状态（换了路径、数据库忙）。"""

    def __init__(self, state: str | None, error: str | None = None):
        super().__init__(state or "stop")
        self.state = state
        self.error = error


def _parent(dir_rel: str) -> str:
    return dir_rel.rpartition("/")[0]


def _join(dir_rel: str, name: str) -> str:
    return f"{dir_rel}/{name}" if dir_rel else name


def _is_name_only(name: str) -> bool:
    return name != CARDS_DIR_NAME and (name in NAME_ONLY_DIRS or name.startswith("."))


def _dir_zone(dir_rel: str) -> str:
    parts = dir_rel.split("/") if dir_rel else []
    if parts and _is_name_only(parts[-1]):
        return "name_only"
    return "cards" if CARDS_DIR_NAME in parts else "normal"


def _file_zone(name: str, dir_zone: str, *, package: bool) -> str:
    if dir_zone == "cards":
        return "cards"
    if package:
        return "package"
    return "code" if file_ext(name) in CODE_EXTS else "normal"


def _skipped_name(name: str) -> bool:
    return silent_skip(name)


def _prefix_where(column: str = "dir_rel") -> str:
    """dir_rel 等于它、或在它下面。按范围比（'/' 后面紧跟的字符是 '0'），能用上 (root_id, dir_rel)
    索引；不用 LIKE：文件夹名里的 _ 和 % 会被当成通配符。参数：同一个 dir_rel 传三次。"""
    return f"({column} = ? OR ({column} >= ? || '/' AND {column} < ? || '0'))"


class MaterialIndexer:
    def __init__(
        self,
        db: Database,
        settings: Any,
        *,
        state_of: Callable[[str], str] = volume_state,
        busy_check: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        round_seconds: float = ROUND_SECONDS,
        round_entries: int = ROUND_ENTRIES,
    ):
        self.db = db
        self.settings = settings
        self.state_of = state_of
        self.busy_check = busy_check
        self.clock = clock
        self.now = now or (lambda: datetime.now(UTC))
        self.round_seconds = round_seconds
        self.round_entries = round_entries

    # ------------------------------------------------------------------ 一轮

    def run_round(self) -> dict[str, Any]:
        if self.busy_check is not None and self.busy_check():
            return {"skipped": "busy", "roots": 0, "entries": 0}
        roots = self.db.query_all(
            """SELECT r.id, r.project_id, r.path, s.state, s.cursor, s.last_full_at, s.sweep_started_at
                 FROM project_material_roots r
                 LEFT JOIN material_index_state s ON s.root_id = r.id
                ORDER BY r.id"""
        )
        # 断点续扫的先走，再按上一轮开始的先后
        roots.sort(key=lambda row: (row["state"] != STATE_WALKING, row["sweep_started_at"] or "", row["id"]))
        skip_real = self._skip_paths(roots)
        self._deadline = self.clock() + self.round_seconds
        self._entries = 0
        walked = 0
        for root in roots:
            if self._out_of_budget():
                break
            walked += 1
            try:
                self._walk_root(root, skip_real)
            except _Stop as stop:
                if stop.state is not None:
                    self._set_state(root["id"], stop.state, stop.error)
            except sqlite3.OperationalError as error:
                if "locked" not in str(error) and "busy" not in str(error):
                    raise
                # 数据库忙：本轮结束，下轮重来
                break
        return {"roots": walked, "entries": self._entries}

    def _out_of_budget(self) -> bool:
        return self._entries >= self.round_entries or self.clock() >= self._deadline

    def _skip_paths(self, roots: list[dict[str, Any]]) -> dict[int, set[str]]:
        """每个根目录里要整棵跳过的相对路径：嵌套在里面的其他根目录、受保护目录。"""
        protected: list[str] = []
        for name in ("archive_root", "staging_root", "data_dir"):
            value = getattr(self.settings, name, None)
            if value:
                protected.append(os.path.realpath(os.path.expanduser(str(value))))
        reals = {row["id"]: os.path.realpath(row["path"]) for row in roots}
        result: dict[int, set[str]] = {}
        for root_id, real in reals.items():
            inside: set[str] = set()
            for other in [*protected, *(path for other_id, path in reals.items() if other_id != root_id)]:
                if other.startswith(real + os.sep):
                    inside.add(os.path.relpath(other, real).replace(os.sep, "/"))
            result[root_id] = inside
        return result

    def _set_state(self, root_id: int, state: str, error: str | None = None) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO material_index_state(root_id, state, error, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(root_id) DO UPDATE SET state = excluded.state, error = excluded.error,
                       updated_at = excluded.updated_at
                   WHERE material_index_state.state IS NOT excluded.state
                      OR material_index_state.error IS NOT excluded.error""",
                (root_id, state, error, utc_now()),
            )

    # ------------------------------------------------------------------ 一个根目录

    def _check_root(self, root: dict[str, Any]) -> None:
        state = self.state_of(root["path"])
        if state == ROOT_ONLINE:
            return
        raise _Stop(STATE_MISSING if state == ROOT_MISSING else STATE_OFFLINE)

    def _walk_root(self, root: dict[str, Any], skip_real: dict[int, set[str]]) -> None:
        root_id = int(root["id"])
        self._root = root
        self._check_root(root)
        self._skip = skip_real.get(root_id, set())
        cursor = None
        # 有断点就接着走（盘拔掉又插上时状态是 offline，断点还在）
        if root["cursor"]:
            try:
                cursor = json.loads(root["cursor"])
            except ValueError:
                cursor = None
        if not isinstance(cursor, dict) or not isinstance(cursor.get("stack"), list):
            last_full = _parse(root["last_full_at"])
            cursor = {
                "stack": [""],
                "full": last_full is None or self.now() - last_full >= FULL_EVERY,
                "started_at": self.now().isoformat(),
            }
        stack: list[str] = [str(item) for item in cursor["stack"]]
        full = bool(cursor.get("full"))
        self._dirs = {
            row["dir_rel"]: row
            for row in self.db.query_all(
                "SELECT dir_rel, mtime_ns, zone, child_count FROM material_dirs WHERE root_id = ?",
                (root_id,),
            )
        }
        self._children: dict[str, set[str]] = {}
        for dir_rel in self._dirs:
            if dir_rel:
                self._children.setdefault(_parent(dir_rel), set()).add(dir_rel)
        base = Path(root["path"])
        while stack:
            if self._out_of_budget():
                self._save_cursor(root_id, {"stack": stack, "full": full, "started_at": cursor["started_at"]})
                return
            dir_rel = stack.pop()
            children = self._visit(root_id, base, dir_rel, full=full)
            stack.extend(sorted(children, reverse=True))
        self._finish_pass(root_id, full=full, started_at=cursor["started_at"])

    def _save_cursor(self, root_id: int, cursor: dict[str, Any]) -> None:
        with self.db.transaction() as connection:
            # 本轮中途换了位置：触发器已经清掉断点，不能再把旧位置的栈写回去
            self._same_root(connection)
            connection.execute(
                """INSERT INTO material_index_state(root_id, state, cursor, sweep_started_at, error, updated_at)
                   VALUES (?, 'walking', ?, ?, NULL, ?)
                   ON CONFLICT(root_id) DO UPDATE SET state = 'walking', cursor = excluded.cursor,
                       sweep_started_at = excluded.sweep_started_at, error = NULL,
                       updated_at = excluded.updated_at""",
                (root_id, json.dumps(cursor, ensure_ascii=False), cursor["started_at"], utc_now()),
            )

    def _finish_pass(self, root_id: int, *, full: bool, started_at: str) -> None:
        """一整轮扫完：词干和它们的位置变了才给 stems_rev 加一。

        摘要带上 rel_path：文件从先扫的目录挪到后扫的目录时，中间比对的那一轮会把提到删掉（那时
        新位置还没收进来），新行入库时又没有提到可以置脏；只看词干集合的话这场会就再也不会重比。"""
        with self.db.transaction() as connection:
            self._same_root(connection)
            digest = hashlib.sha1()
            for row in connection.execute(
                f"""SELECT stem_key, rel_path FROM material_files
                     WHERE root_id = ? AND gone_at IS NULL AND stem_key != ''
                       AND zone IN ({", ".join("?" for _ in MATCH_ZONES)})
                     ORDER BY stem_key, rel_path""",
                (root_id, *MATCH_ZONES),
            ):
                digest.update(row["stem_key"].encode("utf-8"))
                digest.update(b"\0")
                digest.update(row["rel_path"].encode("utf-8"))
                digest.update(b"\0")
            stems_hash = digest.hexdigest()
            files = connection.execute(
                "SELECT COUNT(*) AS n FROM material_files WHERE root_id = ? AND gone_at IS NULL", (root_id,)
            ).fetchone()["n"]
            previous = connection.execute(
                "SELECT stems_hash, stems_rev, last_full_at FROM material_index_state WHERE root_id = ?",
                (root_id,),
            ).fetchone()
            rev = int(previous["stems_rev"]) if previous else 0
            if previous is None or previous["stems_hash"] != stems_hash:
                rev += 1
            last_full = self.now().isoformat() if full else (previous["last_full_at"] if previous else None)
            connection.execute(
                """INSERT INTO material_index_state(root_id, state, cursor, files, stems_rev, stems_hash,
                       sweep_started_at, last_full_at, error, updated_at)
                   VALUES (?, 'done', NULL, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(root_id) DO UPDATE SET state = 'done', cursor = NULL, files = excluded.files,
                       stems_rev = excluded.stems_rev, stems_hash = excluded.stems_hash,
                       sweep_started_at = excluded.sweep_started_at, last_full_at = excluded.last_full_at,
                       error = NULL, updated_at = excluded.updated_at""",
                (root_id, files, rev, stems_hash, started_at, last_full, utc_now()),
            )

    def _same_root(self, connection: Any) -> None:
        row = connection.execute(
            "SELECT path FROM project_material_roots WHERE id = ?", (self._root["id"],)
        ).fetchone()
        if row is None or row["path"] != self._root["path"]:
            raise _Stop(None)

    # ------------------------------------------------------------------ 一个目录

    def _visit(self, root_id: int, base: Path, dir_rel: str, *, full: bool) -> list[str]:
        """处理一个目录，返回要继续往下走的子目录。"""
        absolute = base.joinpath(*dir_rel.split("/")) if dir_rel else base
        zone = _dir_zone(dir_rel)
        self._entries += 1
        try:
            info = os.stat(absolute, follow_symlinks=False)
        except OSError as error:
            return self._unreadable(dir_rel, error)
        if not stat.S_ISDIR(info.st_mode):
            return []
        known = self._dirs.get(dir_rel)
        unchanged = known is not None and known["mtime_ns"] == info.st_mtime_ns and known["zone"] == zone
        if zone == "name_only":
            # 只读一层记个数，按它自己的修改时间跳过，整轮重读时也不重数
            if unchanged:
                return []
            try:
                with os.scandir(absolute) as entries:
                    count = sum(1 for entry in entries if not _skipped_name(entry.name))
            except OSError as error:
                return self._unreadable(dir_rel, error)
            self._entries += count
            self._write_dir(root_id, dir_rel, info.st_mtime_ns, zone, count, files=None, subdirs=None)
            return []
        if unchanged and not full:
            children = self._children.get(dir_rel, set())
            nested = {child for child in children if child in self._skip}
            if nested:
                # 后来挂上的嵌套根目录（或受保护目录）：文件归内层项目，外层这边整棵去掉
                now = utc_now()
                with self.db.transaction() as connection:
                    self._same_root(connection)
                    dropped: list[int] = []
                    for child in sorted(nested):
                        dropped.extend(self._drop_subtree(connection, root_id, child, now))
                    self._dirty_meetings(connection, root_id, dropped, set())
                self._children[dir_rel] = children - nested
            return sorted(children - nested)
        try:
            with os.scandir(absolute) as iterator:
                entries = list(iterator)
        except OSError as error:
            return self._unreadable(dir_rel, error)
        files: dict[str, dict[str, Any]] = {}
        subdirs: list[str] = []
        partial = False
        for entry in entries:
            name = entry.name
            self._entries += 1
            if _skipped_name(name):
                continue
            rel = _join(dir_rel, name)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if rel in self._skip:
                        continue
                    if file_ext(name) in PACKAGE_EXTS:
                        child = entry.stat(follow_symlinks=False)
                        files[rel] = self._file_row(name, dir_rel, zone, None, child.st_mtime_ns, package=True)
                    else:
                        subdirs.append(rel)
                    continue
                child = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # 列目录和 stat 之间被删掉了：就当不在
            except OSError:
                # 一时读不了（资料盘刚从休眠醒来最常见）：先看盘还在不在，不在就停这一轮；
                # 在的话这个目录这次没读全，已知的行一律不标不见、不删子树，下一轮一定重读。
                self._check_root(self._root)
                partial = True
                continue
            if not stat.S_ISREG(child.st_mode):
                continue
            files[rel] = self._file_row(name, dir_rel, zone, child.st_size, child.st_mtime_ns, package=False)
        unchanged_listing = (
            unchanged and known is not None and int(known["child_count"]) == len(entries)
        )
        self._write_dir(
            root_id, dir_rel, None if partial else info.st_mtime_ns, zone, len(entries), files=files,
            subdirs=subdirs, touch_dir=partial or not unchanged_listing, partial=partial,
        )
        return subdirs

    def _unreadable(self, dir_rel: str, error: OSError) -> list[str]:
        """读不了：根目录读不了就停；子目录先看盘还在不在，在就只跳过这棵子树。"""
        if not dir_rel:
            self._check_root(self._root)
            raise _Stop(STATE_ERROR, error.strerror or type(error).__name__)
        self._check_root(self._root)
        return []

    def _file_row(
        self, name: str, dir_rel: str, dir_zone: str, size: int | None, mtime_ns: int, *, package: bool
    ) -> dict[str, Any]:
        stem = derive_stem(name)
        return {
            "name": name,
            "dir_rel": dir_rel,
            "stem": stem,
            "stem_key": stem_key(stem),
            "ext": file_ext(name),
            "size": size,
            "mtime_ns": mtime_ns,
            "zone": _file_zone(name, dir_zone, package=package),
        }

    def _write_dir(
        self,
        root_id: int,
        dir_rel: str,
        mtime_ns: int | None,
        zone: str,
        child_count: int,
        *,
        files: dict[str, dict[str, Any]] | None,
        subdirs: list[str] | None,
        touch_dir: bool = True,
        partial: bool = False,
    ) -> None:
        """partial：这次有条目没读出来。列出来的照常写，没列出来的行不标不见、子目录不删；
        mtime_ns 写成空（和新子目录的占位一样），下一轮一定重读。"""
        now = utc_now()
        with self.db.transaction() as connection:
            self._same_root(connection)
            dirty_files: list[int] = []
            new_stems: set[str] = set()
            if files is not None:
                existing = {
                    row["rel_path"]: row
                    for row in connection.execute(
                        """SELECT id, rel_path, name, stem, stem_key, size, mtime_ns, zone, gone_at
                             FROM material_files WHERE root_id = ? AND dir_rel = ?""",
                        (root_id, dir_rel),
                    ).fetchall()
                }
                for rel, item in files.items():
                    row = existing.get(rel)
                    if row is None:
                        connection.execute(
                            """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key,
                                   ext, size, mtime_ns, zone, seen_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (root_id, rel, dir_rel, item["name"], item["stem"], item["stem_key"], item["ext"],
                             item["size"], item["mtime_ns"], item["zone"], now),
                        )
                        if item["zone"] in MATCH_ZONES:
                            new_stems.add(item["stem_key"])
                        continue
                    changed = (
                        row["size"] != item["size"]
                        or row["mtime_ns"] != item["mtime_ns"]
                        or row["zone"] != item["zone"]
                        or row["stem"] != item["stem"]
                        or row["gone_at"] is not None
                    )
                    if not changed:
                        continue
                    connection.execute(
                        """UPDATE material_files SET name = ?, stem = ?, stem_key = ?, ext = ?, size = ?,
                               mtime_ns = ?, zone = ?, seen_at = ?, gone_at = NULL WHERE id = ?""",
                        (item["name"], item["stem"], item["stem_key"], item["ext"], item["size"],
                         item["mtime_ns"], item["zone"], now, row["id"]),
                    )
                    # 重新出现、改过内容（修改时间决定按会议日期选哪一版）：提过这个词干的会重新比对
                    if item["zone"] in MATCH_ZONES:
                        new_stems.add(item["stem_key"])
                    if row["stem_key"] != item["stem_key"] or item["zone"] not in MATCH_ZONES:
                        dirty_files.append(row["id"])
                gone = [
                    row["id"]
                    for rel, row in existing.items()
                    if rel not in files and row["gone_at"] is None and not partial
                ]
                if gone:
                    connection.executemany(
                        "UPDATE material_files SET gone_at = ? WHERE id = ?", [(now, file_id) for file_id in gone]
                    )
                    dirty_files.extend(gone)
            if subdirs is not None:
                current = set(subdirs)
                if partial:
                    # 这次列不出的子目录照旧保留
                    current |= self._children.get(dir_rel, set())
                for child in sorted(self._children.get(dir_rel, set()) - current):
                    dirty_files.extend(self._drop_subtree(connection, root_id, child, now))
                # 先给新出现的子目录占一行（修改时间为空＝还没读过）：本轮在读到它之前停下时，
                # 下一轮父目录没变也知道要往下走。
                for child in sorted(current - self._children.get(dir_rel, set())):
                    connection.execute(
                        """INSERT OR IGNORE INTO material_dirs(root_id, dir_rel, mtime_ns, listed_at, zone)
                           VALUES (?, ?, NULL, ?, ?)""",
                        (root_id, child, now, _dir_zone(child)),
                    )
                self._children[dir_rel] = current
            if touch_dir:
                connection.execute(
                    """INSERT INTO material_dirs(root_id, dir_rel, mtime_ns, listed_at, zone, child_count)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(root_id, dir_rel) DO UPDATE SET mtime_ns = excluded.mtime_ns,
                           listed_at = excluded.listed_at, zone = excluded.zone,
                           child_count = excluded.child_count""",
                    (root_id, dir_rel, mtime_ns, now, zone, child_count),
                )
            self._dirty_meetings(connection, root_id, dirty_files, new_stems)
        self._dirs[dir_rel] = {"dir_rel": dir_rel, "mtime_ns": mtime_ns, "zone": zone, "child_count": child_count}

    def _drop_subtree(self, connection: Any, root_id: int, dir_rel: str, now: str) -> list[int]:
        """父目录里不见了的子目录：整棵子树的文件写 gone_at，目录记录删掉。"""
        where = _prefix_where()
        params = (dir_rel, dir_rel, dir_rel)
        ids = [
            row["id"]
            for row in connection.execute(
                f"SELECT id FROM material_files WHERE root_id = ? AND gone_at IS NULL AND {where}",
                (root_id, *params),
            ).fetchall()
        ]
        if ids:
            connection.execute(
                f"UPDATE material_files SET gone_at = ? WHERE root_id = ? AND gone_at IS NULL AND {where}",
                (now, root_id, *params),
            )
        connection.execute(f"DELETE FROM material_dirs WHERE root_id = ? AND {where}", (root_id, *params))
        for known in [key for key in self._dirs if key == dir_rel or key.startswith(dir_rel + "/")]:
            self._dirs.pop(known, None)
            self._children.pop(known, None)
        return ids

    def _dirty_meetings(self, connection: Any, root_id: int, file_ids: list[int], stems: set[str]) -> None:
        """文件不见了或换了名字：指向它的会重新比对；新文件入库：本项目提过同一词干的会重新比对。"""
        if file_ids:
            for start in range(0, len(file_ids), 500):
                chunk = file_ids[start : start + 500]
                connection.execute(
                    f"""UPDATE meeting_file_scan SET dirty = dirty + 1
                         WHERE meeting_id IN (
                             SELECT meeting_id FROM meeting_file_mentions
                              WHERE status = 'active' AND file_id IN ({", ".join("?" for _ in chunk)}))""",
                    chunk,
                )
        stems.discard("")
        if stems:
            keys = sorted(stems)
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                connection.execute(
                    f"""UPDATE meeting_file_scan SET dirty = dirty + 1
                         WHERE meeting_id IN (
                             SELECT fm.meeting_id FROM meeting_file_mentions fm
                               JOIN project_material_roots r ON r.project_id = fm.project_id
                              WHERE r.id = ? AND fm.stem_key IN ({", ".join("?" for _ in chunk)}))""",
                    (root_id, *chunk),
                )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def index_status(connection: Any, project_id: str | None = None) -> list[dict[str, Any]]:
    """GET /api/materials/index-status：每个根目录的索引进度。只查库。"""
    params: tuple[Any, ...] = ()
    where = ""
    if project_id is not None:
        where = "WHERE r.project_id = ?"
        params = (project_id,)
    rows = connection.execute(
        f"""SELECT r.id AS root_id, r.project_id, r.path,
                   COALESCE(s.state, 'pending') AS state, s.files, s.last_full_at, s.error,
                   s.sweep_started_at, s.updated_at, s.stems_hash IS NOT NULL AS indexed_once,
                   (SELECT COUNT(*) FROM material_files f
                     WHERE f.root_id = r.id AND f.gone_at IS NULL) AS files_now,
                   (SELECT COUNT(*) FROM material_dirs d
                     WHERE d.root_id = r.id AND d.zone = 'name_only') AS name_only_dirs
              FROM project_material_roots r
              LEFT JOIN material_index_state s ON s.root_id = r.id
              {where}
             ORDER BY r.project_id, r.created_at, r.id""",
        params,
    ).fetchall()
    return [
        {
            "root_id": row["root_id"],
            "project_id": row["project_id"],
            "path": row["path"],
            "state": row["state"],
            # 扫完的按那一轮的结果，扫到一半的按已经认得的
            "files": int(row["files"] if row["state"] == STATE_DONE and row["files"] is not None else row["files_now"]),
            "name_only_dirs": int(row["name_only_dirs"]),
            "indexed_once": bool(row["indexed_once"]),
            "last_full_at": row["last_full_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
        }
        for row in rows
    ]
