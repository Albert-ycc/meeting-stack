"""SQLite 连接必须显式关闭。

sqlite3.Connection 的语句缓存反向引用连接本身，只用 `with sqlite3.connect()` 不会关闭连接，
句柄要等循环 GC 才释放。2026-09-07 与 09-14 两次因此冲破进程句柄上限（EMFILE）宕机。
"""

import gc
from contextlib import closing
import os
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from meeting_workbench import cli
from meeting_workbench import db as db_module
from meeting_workbench.db import WAL_SIZE_LIMIT_BYTES, Database, utc_now


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "meeting_workbench"
UNCLOSED_PATTERN = re.compile(r"with\s+(?:sqlite3|self|[\w.]+)\.connect\(")


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_query_helpers_release_file_handles_without_gc(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, ?, ?)",
        ("vm-fd", "句柄会", "completed_unreviewed"),
    )
    gc.collect()
    gc.disable()
    try:
        baseline = _open_fds()
        for _ in range(40):
            db.query_one("SELECT id FROM meetings WHERE id=?", ("vm-fd",))
            db.query_all("SELECT id FROM meetings")
            db.execute_rowcount("UPDATE meetings SET title=title WHERE id=?", ("vm-fd",))
            db.execute("UPDATE meetings SET title=title WHERE id=?", ("vm-fd",))
            db.user_version()
        leaked = _open_fds() - baseline
    finally:
        gc.enable()
    assert leaked <= 0, f"查询辅助函数留下 {leaked} 个未关闭的句柄"


def test_failed_statement_still_closes_connection(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    gc.collect()
    gc.disable()
    try:
        baseline = _open_fds()
        for _ in range(20):
            try:
                db.execute("INSERT INTO no_such_table VALUES (1)")
            except Exception:
                pass
        leaked = _open_fds() - baseline
    finally:
        gc.enable()
    assert leaked <= 0


def test_package_source_has_no_unclosed_connect_pattern():
    offenders = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if UNCLOSED_PATTERN.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], "用 closing(...) 或 Database.autocommit() 代替：\n" + "\n".join(
        offenders
    )


def test_serve_raises_soft_open_file_limit(monkeypatch):
    import resource

    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (256, 1 << 20))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: calls.append(limits))
    cli._raise_open_file_limit()
    assert calls == [(4096, 1 << 20)]


class _RelayStub:
    def list_jobs(self, *, status=None, limit=200):
        return []

    def health(self):
        return {
            "status": "healthy",
            "mode": "controlled",
            "worker": {"state": "idle", "heartbeat_age_seconds": 0},
            "counts": {"queued": 0, "active": 0, "failed": 0},
        }


def _app(tmp_path):
    from meeting_workbench.config import Settings
    from meeting_workbench.main import create_app

    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    return create_app(settings, _RelayStub())


def test_health_reports_process_file_handles(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_app(tmp_path))
    payload = client.get("/api/health").json()
    process = payload["details"]["process"]
    assert isinstance(process["open_files"], int) and process["open_files"] > 0
    assert payload["services"]["process"] == "healthy"


def test_health_degrades_when_handles_near_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from meeting_workbench import main

    monkeypatch.setattr(
        main, "process_file_handles", lambda: {"open_files": 230, "open_files_limit": 256}
    )
    client = TestClient(_app(tmp_path))
    payload = client.get("/api/health").json()
    assert payload["services"]["process"] == "degraded"
    assert payload["status"] == "degraded"


def test_serve_keeps_higher_existing_limit(monkeypatch):
    import resource

    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (10240, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: calls.append(limits))
    cli._raise_open_file_limit()
    assert calls == []


def test_connections_wait_five_seconds_for_a_write_lock(tmp_path):
    with closing(Database(tmp_path / "w.sqlite3").connect()) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


# —— reuse_connection：一段代码里先后的查询共用一个连接 ——


def _seeded(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, ?, ?)",
        ("vm-reuse", "复用会", "completed_unreviewed"),
    )
    return db


def _count_connects(monkeypatch):
    opened = []
    original = Database.connect

    def connect(self):
        connection = original(self)
        opened.append(connection)
        return connection

    monkeypatch.setattr(Database, "connect", connect)
    return opened


def _title(db):
    return db.query_one("SELECT title FROM meetings WHERE id=?", ("vm-reuse",))["title"]


def test_sequential_helpers_share_one_connection_closed_at_the_end(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    opened = _count_connects(monkeypatch)
    with db.reuse_connection():
        for _ in range(20):
            db.query_one("SELECT id FROM meetings WHERE id=?", ("vm-reuse",))
            db.query_all("SELECT id FROM meetings")
            db.execute("UPDATE meetings SET title=title WHERE id=?", ("vm-reuse",))
            db.execute_rowcount("UPDATE meetings SET title=title WHERE id=?", ("vm-reuse",))
            with db.autocommit() as connection:
                connection.execute("SELECT 1").fetchall()
        with db.reuse_connection():
            _title(db)
        _title(db)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    _title(db)
    assert len(opened) == 2


def test_writes_through_the_shared_connection_are_committed_call_by_call(tmp_path):
    db = _seeded(tmp_path)
    with db.reuse_connection():
        db.execute("UPDATE meetings SET title=? WHERE id=?", ("改过", "vm-reuse"))
        with closing(sqlite3.connect(db.path)) as other:
            row = other.execute("SELECT title FROM meetings WHERE id=?", ("vm-reuse",)).fetchone()
        assert row[0] == "改过"


def test_helpers_inside_a_borrowed_block_open_their_own_connection(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    opened = _count_connects(monkeypatch)
    with db.reuse_connection():
        with db.autocommit() as outer:
            outer.execute("UPDATE meetings SET title=? WHERE id=?", ("还没提交", "vm-reuse"))
            # 和不复用时一样：另开的连接看不到外层块里还没提交的改动
            seen_inside = _title(db)
        assert seen_inside == "复用会"
        assert _title(db) == "还没提交"
    assert len(opened) == 2


def test_transaction_keeps_its_own_connection_inside_reuse(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    opened = _count_connects(monkeypatch)
    with db.reuse_connection():
        _title(db)
        with db.transaction() as connection:
            connection.execute("UPDATE meetings SET title=? WHERE id=?", ("事务里", "vm-reuse"))
            seen_during = _title(db)
        assert seen_during == "复用会"
        assert _title(db) == "事务里"
    assert len(opened) == 2


def test_failed_borrow_rolls_back_and_the_connection_keeps_serving(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    opened = _count_connects(monkeypatch)
    with db.reuse_connection():
        with pytest.raises(RuntimeError):
            with db.autocommit() as connection:
                connection.execute("UPDATE meetings SET title=? WHERE id=?", ("回滚掉", "vm-reuse"))
                raise RuntimeError("中途出错")
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO no_such_table VALUES (1)")
        assert _title(db) == "复用会"
        db.execute("UPDATE meetings SET title=? WHERE id=?", ("出错之后照常写", "vm-reuse"))
    assert _title(db) == "出错之后照常写"
    assert len(opened) == 2


def test_other_threads_never_get_the_shared_connection(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    opened = _count_connects(monkeypatch)
    seen = []
    with db.reuse_connection():
        _title(db)
        worker = threading.Thread(target=lambda: seen.append(_title(db)))
        worker.start()
        worker.join()
        _title(db)
    assert seen == ["复用会"]
    assert len(opened) == 2


# —— held_open：服务在跑时常驻一个空闲连接 ——


def test_held_connection_keeps_wal_files_and_never_blocks_a_full_checkpoint(tmp_path):
    db = _seeded(tmp_path)
    wal, shm = Path(f"{db.path}-wal"), Path(f"{db.path}-shm")
    _title(db)
    assert not wal.exists() and not shm.exists()
    with db.held_open():
        for _ in range(3):
            db.execute("UPDATE meetings SET title=title WHERE id=?", ("vm-reuse",))
            _title(db)
            assert wal.exists() and shm.exists()
        # 常驻连接不占读快照：别的连接能把 WAL 全部写回主库并截断
        with closing(sqlite3.connect(db.path, timeout=0.5)) as other:
            busy, _frames, _done = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 0
        assert wal.stat().st_size == 0
    assert not wal.exists()


def test_wal_grown_by_a_big_transaction_shrinks_back_while_held_open(tmp_path, monkeypatch):
    assert WAL_SIZE_LIMIT_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(db_module, "WAL_SIZE_LIMIT_BYTES", 64 * 1024)
    db = _seeded(tmp_path)
    wal = Path(f"{db.path}-wal")
    with db.held_open():
        with db.transaction() as connection:
            connection.executemany(
                "INSERT INTO events(meeting_id, event_type, payload_json, created_at) "
                "VALUES (?, 'wal-test', ?, ?)",
                [("vm-reuse", "x" * 2000, utc_now()) for _ in range(1000)],
            )
        assert wal.stat().st_size > 1024 * 1024
        with closing(db.connect()) as connection:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        db.execute("UPDATE meetings SET title=title WHERE id=?", ("vm-reuse",))
        assert wal.stat().st_size <= 64 * 1024
