from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import fcntl

from .config import Settings
from .db import Database

# 可从 segments 重算的派生数据，备份时清空。embeddings 占主库七成体积，
# 而服务启动后的后台循环会调 SemanticIndex.rebuild() 自动补齐，
# 留在备份里只是把同一批向量复制 retention 份。
DERIVED_TABLES: tuple[str, ...] = ("embeddings",)


@dataclass(frozen=True, slots=True)
class BackupResult:
    local_path: Path
    mirror_path: Path | None
    status: str
    local_sha256: str
    mirror_sha256: str | None = None
    mirror_error: str | None = None
    derived_stripped: bool = False


class BackupManager:
    def __init__(self, db: Database, settings: Settings, *, retention: int = 14):
        self.db = db
        self.settings = settings
        self.retention = retention

    def create(self, *, now: datetime | None = None) -> BackupResult:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        self.settings.backup_dir.mkdir(parents=True, exist_ok=True)
        with self._backup_lock():
            filename = (
                f"workbench-{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{secrets.token_hex(4)}.sqlite3"
            )
            local_path = self.settings.backup_dir / filename
            derived_stripped = self._online_backup(local_path)
            local_sha256 = self._sha256(local_path)
            self._verify_database(local_path)
            self._rotate(self.settings.backup_dir)

            mirror_path: Path | None = None
            mirror_sha256: str | None = None
            mirror_error: str | None = None
            mirror_candidate: Path | None = None
            installed_mirror: Path | None = None
            try:
                if not self.settings.archive_root.is_dir():
                    raise OSError("archive mirror unavailable")
                mirror_dir = self.settings.archive_root / ".meeting-workbench-backups"
                mirror_dir.mkdir(parents=True, exist_ok=True)
                mirror_path = mirror_dir / filename
                mirror_candidate = mirror_dir / f".{filename}.{secrets.token_hex(4)}.candidate"
                self._atomic_copy(local_path, mirror_candidate)
                self._verify_database(mirror_candidate)
                mirror_sha256 = self._sha256(mirror_candidate)
                if mirror_sha256 != local_sha256:
                    raise OSError("backup mirror hash mismatch")
                os.replace(mirror_candidate, mirror_path)
                installed_mirror = mirror_path
                self._fsync_file(mirror_path)
                self._fsync_directory(mirror_dir)
                self._rotate(mirror_dir)
            except (OSError, sqlite3.DatabaseError) as error:
                mirror_error = type(error).__name__
                for failed_path in (mirror_candidate, installed_mirror):
                    if failed_path is None:
                        continue
                    with suppress(OSError):
                        failed_path.unlink(missing_ok=True)
                if mirror_candidate is not None:
                    with suppress(OSError):
                        self._fsync_directory(mirror_candidate.parent)
                mirror_path = None
                mirror_sha256 = None

            status = "healthy" if mirror_path is not None else "degraded"
            receipt = {
                "created_at": now.isoformat(),
                "status": status,
                "local_path": str(local_path),
                "local_sha256": local_sha256,
                "mirror_path": str(mirror_path) if mirror_path else None,
                "mirror_sha256": mirror_sha256,
                "mirror_ok": mirror_path is not None,
                "mirror_error": mirror_error,
                "derived_stripped": derived_stripped,
                "derived_tables": list(DERIVED_TABLES) if derived_stripped else [],
            }
            self._atomic_text(
                self.settings.backup_dir / "last-backup.json",
                json.dumps(receipt, ensure_ascii=False, separators=(",", ":")),
            )
            return BackupResult(
                local_path=local_path,
                mirror_path=mirror_path,
                status=status,
                local_sha256=local_sha256,
                mirror_sha256=mirror_sha256,
                mirror_error=mirror_error,
                derived_stripped=derived_stripped,
            )

    @contextmanager
    def _backup_lock(self) -> Iterator[None]:
        lock_path = self.settings.backup_dir / "backup.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _online_backup(self, destination: Path) -> bool:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            source_connection = self.db.connect()
            destination_connection = sqlite3.connect(temporary)
            try:
                source_connection.backup(destination_connection, pages=256)
                stripped = self._strip_derived_tables(destination_connection)
                result = destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise sqlite3.DatabaseError(f"backup integrity check failed: {result}")
            finally:
                destination_connection.close()
                source_connection.close()
            os.replace(temporary, destination)
            self._fsync_file(destination)
            self._fsync_directory(destination.parent)
            return stripped
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _strip_derived_tables(connection: sqlite3.Connection) -> bool:
        """清空 DERIVED_TABLES 并回收页面，让备份只留业务数据本身。

        清理失败不该拖垮备份 —— 宁可留一份完整副本，也不能因为省空间没备份成。
        """
        placeholders = ",".join("?" * len(DERIVED_TABLES))
        try:
            present = [
                row[0]
                for row in connection.execute(
                    f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
                    DERIVED_TABLES,
                )
            ]
            if not present:
                return False
            for table in present:
                connection.execute(f'DELETE FROM "{table}"')
            connection.commit()
        except sqlite3.DatabaseError:
            with suppress(sqlite3.DatabaseError):
                connection.rollback()
            return False
        # VACUUM 必须在事务外执行；它失败只是没回收到页面，备份内容已经是干净的。
        with suppress(sqlite3.DatabaseError):
            connection.execute("VACUUM")
        return True

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            BackupManager._fsync_file(destination)
            BackupManager._fsync_directory(destination.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            BackupManager._fsync_file(path)
            BackupManager._fsync_directory(path.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _verify_database(path: Path) -> None:
        uri = f"file:{path.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        if result != "ok":
            raise sqlite3.DatabaseError(f"backup integrity check failed: {result}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _rotate(self, directory: Path) -> None:
        verified: list[Path] = []
        removed = False
        for path in sorted(directory.glob("workbench-*.sqlite3"), reverse=True):
            try:
                if path.is_symlink() or not path.is_file():
                    raise OSError("backup snapshot is not a regular file")
                self._verify_database(path)
            except (OSError, sqlite3.DatabaseError):
                path.unlink(missing_ok=True)
                removed = True
                continue
            verified.append(path)
        for path in verified[self.retention :]:
            path.unlink()
            removed = True
        if removed:
            self._fsync_directory(directory)
