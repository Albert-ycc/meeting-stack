"""SQLite 连接必须显式关闭。

sqlite3.Connection 的语句缓存反向引用连接本身，只用 `with sqlite3.connect()` 不会关闭连接，
句柄要等循环 GC 才释放。2026-09-07 与 09-14 两次因此冲破进程句柄上限（EMFILE）宕机。
"""

import gc
import os
import re
from pathlib import Path

from meeting_workbench import cli
from meeting_workbench.db import Database


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
    assert offenders == [], "用 closing(...) 或 Database.autocommit() 代替：\n" + "\n".join(offenders)


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
