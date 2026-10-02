"""发布不能在持有数据库写锁时读原音频。

发布在 BEGIN IMMEDIATE 的写事务里原来把归档目录（含原音频）读了约 5 遍算哈希：300MB 音频在锁里
读 1500MB。本机热缓存的 SSD 上锁持有 0.67 秒，外置盘冷读、wav 原音频时会逼近 5 秒的
busy_timeout，期间所有写库的请求排队。完整哈希挪到事务外，事务内只比对前后都没变的廉价指纹
（inode、大小、修改时间、变更时间，relay 发布回执同一套写法）。
"""

import builtins
import hashlib
import io
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import meeting_workbench.service as service_module
from meeting_workbench.db import Database
from meeting_workbench.service import ConflictError, MeetingService

from .helpers import seed_editable_meeting

MEETING = "vm-20260102-101500"


def _published_world(tmp_path, audio_size=2 * 1024 * 1024):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, audio, _version = seed_editable_meeting(db, archive)
    audio.write_bytes(os.urandom(audio_size))
    status = audio.stat()
    db.execute(
        "UPDATE meetings SET original_audio_sha256=? WHERE id=?",
        (hashlib.sha256(audio.read_bytes()).hexdigest(), MEETING),
    )
    db.execute(
        "UPDATE artifacts SET size_bytes=?, mtime_ns=? WHERE path=?",
        (status.st_size, status.st_mtime_ns, str(audio)),
    )
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(MEETING)
    service.save_minutes(MEETING, "# 写锁")
    return db, service, archive, formal, audio


def _work_directories(archive: Path) -> list[Path]:
    # 发布日志 .workbench-publish-journal-*.json 是文件，不算工作目录
    return sorted(path for path in archive.glob(".*workbench-publish-*") if path.is_dir())


def _tree_bytes(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_publish_reads_no_audio_while_holding_the_write_lock(tmp_path, monkeypatch):
    db, service, _archive, _formal, _audio = _published_world(tmp_path)
    real_transaction = Database.transaction
    real_sha256_file = service_module.sha256_file
    real_open = io.open
    inside = threading.local()
    read_under_lock: list[tuple[str, int]] = []

    @contextmanager
    def lock_window(self):
        with real_transaction(self) as connection:
            inside.on = True
            try:
                yield connection
            finally:
                inside.on = False

    def guarded_open(file, *args, **kwargs):
        # 事务内打开任何音频文件（不管走哪个函数读）都算读了正文，发布就失败
        if getattr(inside, "on", False) and str(file).endswith(".m4a"):
            raise AssertionError(f"持有写锁时打开了音频：{file}")
        return real_open(file, *args, **kwargs)

    def counting_sha256_file(path):
        if getattr(inside, "on", False):
            read_under_lock.append((Path(path).name, Path(path).stat().st_size))
        return real_sha256_file(path)

    monkeypatch.setattr(Database, "transaction", lock_window)
    monkeypatch.setattr(service_module, "sha256_file", counting_sha256_file)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(builtins, "open", guarded_open)

    service.publish(MEETING)

    assert db.query_one("SELECT status FROM meetings WHERE id=?", (MEETING,))["status"] == (
        "published"
    )
    # 锁里最多只给小文件（manifest）算哈希，不碰音频
    assert [name for name, _size in read_under_lock if name.endswith(".m4a")] == []
    assert sum(size for _name, size in read_under_lock) < 64 * 1024
    # 省掉的哈希不能让登记的值走样：artifacts 里每个文件的 sha256 都是它现在的真哈希
    rows = db.query_all("SELECT path, sha256 FROM artifacts WHERE meeting_id=?", (MEETING,))
    published = [row for row in rows if row["sha256"] and Path(row["path"]).is_file()]
    assert any(row["path"].endswith("workbench-manifest.json") for row in published)
    for row in published:
        assert row["sha256"] == real_sha256_file(Path(row["path"])), row["path"]


@pytest.mark.parametrize("how", ["append_a_byte", "same_size_and_mtime_restored", "small_file"])
def test_publish_refuses_when_a_prepared_file_changes_before_the_transaction(
    tmp_path, monkeypatch, how
):
    db, service, archive, formal, _audio = _published_world(tmp_path)
    before = _tree_bytes(formal)
    real_transaction = Database.transaction

    def tamper(path: Path) -> None:
        stamp = path.stat().st_mtime_ns
        if how == "append_a_byte":
            with path.open("ab") as handle:
                handle.write(b"x")
        else:
            data = bytearray(path.read_bytes())
            data[0] ^= 0xFF
            path.write_bytes(bytes(data))
            if how == "same_size_and_mtime_restored":
                # 大小一样、修改时间也改回去：只剩 inode / 变更时间能看出来
                os.utime(path, ns=(stamp, stamp))

    @contextmanager
    def tamper_after_verification(self):
        # 完整校验做完、进写事务之前：工作目录里已校验过的文件被改了
        for work in _work_directories(archive):
            target = next(work.rglob("会议纪要.md" if how == "small_file" else "*.m4a"))
            tamper(target)
        with real_transaction(self) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", tamper_after_verification)

    with pytest.raises(ConflictError, match="发布准备期间文件发生变化"):
        service.publish(MEETING)

    # 没校验过的内容一个字节都没发布出去
    assert _tree_bytes(formal) == before
    assert db.query_one("SELECT status FROM meetings WHERE id=?", (MEETING,))["status"] == (
        "draft_modified"
    )
    assert _work_directories(archive) == []
    assert not list(archive.glob(".workbench-publish-journal-*.json"))


def test_publish_still_notices_a_source_audio_changed_before_the_directory_swap(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    draft_root = archive / ".workbench-drafts" / "job-e2e" / "attempt-1"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, draft_root.parent)
    db.execute(
        """UPDATE meetings SET canonical_dir = ?, source_priority = 51,
           recording_date = '2026-01-02T10:15:00+00:00', status = 'completed_unreviewed'
           WHERE id = ?""",
        (str(meeting_dir), MEETING),
    )
    db.execute("UPDATE artifacts SET source_root='draft' WHERE meeting_id=?", (MEETING,))
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(MEETING)
    service.save_minutes(MEETING, "# 升格")
    original_audio = audio.read_bytes()
    real_transaction = Database.transaction

    @contextmanager
    def source_audio_changes_before_the_transaction(self):
        # 升格时来源音频在草稿目录里（不在要装上去的新目录里），校验之后被改了
        if _work_directories(archive):
            with audio.open("ab") as handle:
                handle.write(b"tampered")
        with real_transaction(self) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", source_audio_changes_before_the_transaction)

    with pytest.raises(ConflictError, match="原音频"):
        service.publish(MEETING)

    assert not (archive / "260102 需求复盘会").exists()
    assert audio.read_bytes() == original_audio + b"tampered"
    assert db.query_one("SELECT status FROM meetings WHERE id=?", (MEETING,))["status"] == (
        "draft_modified"
    )
    assert _work_directories(archive) == []
