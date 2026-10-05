"""备份副本从源头不留 -wal / -shm。

备份副本由 SQLite 备份接口写出，头部带着源库的 WAL 标记（第 18、19 字节是 2）。写完时 WAL 其实已经
合并干净（os.replace 前旁边没有 -wal），只是副本被标成了 WAL 模式，所以之后每次只读打开（校验、恢复前
查看）都会在旁边建 -wal / -shm；轮转删掉主文件后它们成了孤儿，生产备份目录里攒了二百多个。

改成副本写完时落成非 WAL（journal_mode=DELETE）：整份内容都在一个文件里，只读打开不留任何文件；
恢复成库以后 initialize 会把库重新落回 WAL。校验不能为了不留文件改用 immutable=1：它会忽略 WAL，
WAL 里还有已提交的内容时读到的是不完整的库，坏副本会被校验成完好。
"""

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database

# 备份读的是带着 -wal 的库，连接关闭时的检查点也是被测行为：不给这些用例常驻连接
pytestmark = pytest.mark.real_database_files

START = datetime(2026, 1, 1, tzinfo=UTC)


def _manager(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
        staging_root=tmp_path / "staging",
    )
    db = Database(settings.database_path)
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-1', '备份验证', 'published')")
    return settings, db, BackupManager(db, settings)


def _names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def test_new_backup_copies_are_self_contained_and_leave_nothing_beside_them(tmp_path):
    settings, _db, manager = _manager(tmp_path)
    manager.create(now=START)
    result = manager.create(now=START + timedelta(days=1))

    local_copies = {path.name for path in settings.backup_dir.glob("workbench-*.sqlite3")}
    assert len(local_copies) == 2
    assert _names(settings.backup_dir) == {*local_copies, "backup.lock", "last-backup.json"}
    assert _names(result.mirror_path.parent) == local_copies
    for path in (result.local_path, result.mirror_path):
        assert path.read_bytes()[18:20] == b"\x01\x01"  # 回滚日志模式，不是 WAL（2、2）
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT title FROM meetings").fetchone()[0] == "备份验证"
        finally:
            connection.close()
        assert path.name in _names(path.parent)  # 校验这几次读取后旁边还是没有新文件
    assert _names(settings.backup_dir) == {*local_copies, "backup.lock", "last-backup.json"}
    assert _names(result.mirror_path.parent) == local_copies


def test_verifying_a_copy_creates_no_files(tmp_path):
    settings, _db, manager = _manager(tmp_path)
    result = manager.create(now=START)
    before = {
        directory: _names(directory)
        for directory in (settings.backup_dir, result.mirror_path.parent)
    }

    BackupManager._verify_database(result.local_path)
    BackupManager._verify_database(result.mirror_path)

    assert {directory: _names(directory) for directory in before} == before


def _copy_with_corruption_only_in_the_wal(directory: Path) -> Path:
    """主文件是完好的库，WAL 里躺着一笔已提交、会让库损坏的改动（索引的根页指到不存在的页）。"""
    live_dir = directory / "live"
    live_dir.mkdir()
    live = sqlite3.connect(live_dir / "live.sqlite3")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("CREATE TABLE t(v)")
    live.execute("CREATE INDEX idx_t ON t(v)")
    live.execute("INSERT INTO t VALUES (1)")
    live.commit()
    live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    live.execute("PRAGMA writable_schema=ON")
    live.execute("UPDATE sqlite_master SET rootpage = rootpage + 50 WHERE name = 'idx_t'")
    live.commit()
    copy_dir = directory / "copy"
    copy_dir.mkdir()
    copy = copy_dir / "workbench-copy.sqlite3"
    shutil.copyfile(live_dir / "live.sqlite3", copy)
    shutil.copyfile(live_dir / "live.sqlite3-wal", copy_dir / "workbench-copy.sqlite3-wal")
    live.close()
    return copy


def test_verification_reads_the_wal_so_corruption_that_only_lives_there_is_caught(tmp_path):
    copy = _copy_with_corruption_only_in_the_wal(tmp_path)

    with pytest.raises(sqlite3.DatabaseError):
        BackupManager._verify_database(copy)

    # 为什么不能图省事用 immutable=1：它忽略 WAL，主文件本身是完好的，同一份坏副本会被校验成「ok」
    immutable = sqlite3.connect(copy.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        assert immutable.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        immutable.close()


def test_a_backup_that_cannot_leave_wal_mode_fails_instead_of_installing_an_incomplete_copy(
    tmp_path, monkeypatch
):
    settings, _db, manager = _manager(tmp_path)
    held: list[sqlite3.Connection] = []
    real_strip = BackupManager._strip_derived_tables

    def strip_then_keep_another_connection_open(connection):
        stripped = real_strip(connection)
        # 副本这时还有另一个连接开着：WAL 合并不进主文件、也切不出 WAL 模式。
        # 这时只把主文件 os.replace 过去，装上的就是一份缺内容的备份。
        temporary = connection.execute("PRAGMA database_list").fetchone()[2]
        other = sqlite3.connect(temporary)
        other.execute("SELECT count(*) FROM sqlite_master").fetchone()
        held.append(other)
        return stripped

    monkeypatch.setattr(
        BackupManager,
        "_strip_derived_tables",
        staticmethod(strip_then_keep_another_connection_open),
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            manager.create(now=START)
    finally:
        for connection in held:
            connection.close()

    assert list(settings.backup_dir.glob("workbench-*.sqlite3")) == []
    assert not (settings.backup_dir / "last-backup.json").exists()
    monkeypatch.undo()
    assert manager.create(now=START).status == "healthy"


def test_leaving_wal_mode_that_silently_did_not_happen_is_an_error():
    class StillWal:
        def execute(self, _sql):
            return self

        def fetchone(self):
            return ("wal",)

    with pytest.raises(sqlite3.DatabaseError, match="wal"):
        BackupManager._leave_wal_mode(StillWal())


def test_restoring_a_backup_puts_the_database_back_in_wal_mode(tmp_path):
    settings, db, manager = _manager(tmp_path)
    backup = manager.create(now=START).local_path
    restored = tmp_path / "restored" / "workbench.sqlite3"
    restored.parent.mkdir()
    shutil.copyfile(backup, restored)

    restored_db = Database(restored)
    restored_db.initialize()

    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()
    assert restored_db.query_one("SELECT title FROM meetings WHERE id='vm-1'") == {
        "title": "备份验证"
    }
