"""备份轮转：只校验新生成的那份，轮转带走旁路文件。

以前每次备份对本地和镜像里的每一份副本逐个跑 PRAGMA integrity_check（14 份 × 2 个目录），
库越大越慢；而且只读打开副本校验会在旁边留下 -wal / -shm，轮转删掉主文件时没带走它们，
生产备份目录里攒了两百多个孤儿旁路文件。
"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database

START = datetime(2026, 1, 1, tzinfo=UTC)


def _manager(tmp_path, *, retention=14):
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
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-1', '备份', 'published')")
    return settings, BackupManager(db, settings, retention=retention)


def _mirror_dir(settings):
    return settings.archive_root / ".meeting-workbench-backups"


def _count_integrity_checks(monkeypatch):
    statements = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(
            lambda sql: statements.append(sql) if "integrity_check" in sql.lower() else None
        )
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return statements


def _sidecars(directory):
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.name.endswith(("-wal", "-shm")) and path.is_file()
    )


def test_a_backup_integrity_checks_only_the_new_copies_not_the_ones_already_there(
    tmp_path, monkeypatch
):
    settings, manager = _manager(tmp_path)
    for day in range(14):
        manager.create(now=START + timedelta(days=day))
    assert len(list(settings.backup_dir.glob("workbench-*.sqlite3"))) == 14
    checks = _count_integrity_checks(monkeypatch)

    result = manager.create(now=START + timedelta(days=14))

    assert result.status == "healthy"
    # 本地新副本（在线备份时）一次、镜像新副本（拷过去之后）一次，已有的 28 份一份都不再打开
    assert len(checks) == 2
    assert len(list(settings.backup_dir.glob("workbench-*.sqlite3"))) == 14
    assert len(list(_mirror_dir(settings).glob("workbench-*.sqlite3"))) == 14


def test_rotating_a_copy_out_removes_its_wal_and_shm_with_it(tmp_path):
    settings, manager = _manager(tmp_path, retention=2)
    oldest = manager.create(now=START)
    manager.create(now=START + timedelta(days=1))
    for path in (oldest.local_path, oldest.mirror_path):
        (path.parent / f"{path.name}-shm").write_bytes(b"shm")
        (path.parent / f"{path.name}-wal").write_bytes(b"")

    manager.create(now=START + timedelta(days=2))

    for path in (oldest.local_path, oldest.mirror_path):
        assert not path.exists()
        assert _sidecars(path.parent) == []


def test_orphan_sidecars_are_swept_and_the_ones_with_a_main_file_are_left_alone(tmp_path):
    settings, manager = _manager(tmp_path, retention=3)
    manager.create(now=START)
    manager.create(now=START + timedelta(days=1))
    kept_sidecars = {}
    for directory in (settings.backup_dir, _mirror_dir(settings)):
        mains = sorted(directory.glob("workbench-*.sqlite3"))
        for main in mains:
            (directory / f"{main.name}-shm").write_bytes(b"shm")
            (directory / f"{main.name}-wal").write_bytes(b"")
        kept_sidecars[directory] = _sidecars(directory)
        # 主文件早就被轮转删掉、旁路文件还留着的孤儿，两种命名都有
        for orphan in (
            "workbench-20251201T000000.000000Z-aaaaaaaa.sqlite3",
            ".workbench-20251202T000000.000000Z-bbbbbbbb.sqlite3.cafe1234.candidate",
        ):
            (directory / f"{orphan}-shm").write_bytes(b"shm")
            (directory / f"{orphan}-wal").write_bytes(b"")
        # 不是备份副本的旁路文件和别的文件不碰
        (directory / "别的库.sqlite3-wal").write_bytes(b"x")
        (directory / "notes.txt").write_text("别动", encoding="utf-8")

    manager.create(now=START + timedelta(days=2))

    for directory in (settings.backup_dir, _mirror_dir(settings)):
        assert _sidecars(directory) == sorted([*kept_sidecars[directory], "别的库.sqlite3-wal"])
        assert (directory / "notes.txt").read_text(encoding="utf-8") == "别动"
        assert len(list(directory.glob("workbench-*.sqlite3"))) == 3


def test_the_new_copy_is_never_the_one_rotated_out(tmp_path):
    settings, manager = _manager(tmp_path, retention=1)
    # 目录里有一份文件名排在新副本后面的（时间戳在未来）：只按文件名倒序取最新 1 份的话，
    # 留下的会是它，刚生成并验过的新副本反而被挤掉
    for directory in (settings.backup_dir, _mirror_dir(settings)):
        directory.mkdir(exist_ok=True)
        (directory / "workbench-99991231T235959.999999Z-dead0001.sqlite3").write_bytes(b"later")

    result = manager.create(now=START)

    assert list(settings.backup_dir.glob("workbench-*.sqlite3")) == [result.local_path]
    assert list(_mirror_dir(settings).glob("workbench-*.sqlite3")) == [result.mirror_path]


def test_leftover_files_that_cannot_be_removed_do_not_fail_the_backup(
    tmp_path, monkeypatch, caplog
):
    settings, manager = _manager(tmp_path, retention=2)
    oldest = manager.create(now=START)
    manager.create(now=START + timedelta(days=1))
    for path in (oldest.local_path, oldest.mirror_path):
        (path.parent / f"{path.name}-shm").write_bytes(b"shm")  # 轮转时要连主文件一起清的
    for directory in (settings.backup_dir, _mirror_dir(settings)):
        (directory / "workbench-20251201T000000.000000Z-aaaaaaaa.sqlite3-wal").write_bytes(b"")
    real_unlink = Path.unlink

    def refuse_sidecars(self, *args, **kwargs):
        if self.name.endswith(("-wal", "-shm")):
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_sidecars)

    with caplog.at_level(logging.WARNING, logger="meeting_workbench.backup"):
        result = manager.create(now=START + timedelta(days=2))

    # 残留的旁路文件清不掉只记日志，新副本照常装好、回执照常写
    assert result.status == "healthy"
    assert result.local_path.is_file() and result.mirror_path.is_file()
    assert (settings.backup_dir / "last-backup.json").is_file()
    assert not oldest.local_path.exists() and not oldest.mirror_path.exists()
    assert any("清不掉" in record.getMessage() for record in caplog.records)
