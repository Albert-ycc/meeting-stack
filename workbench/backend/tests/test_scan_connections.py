"""导入扫描的查询共用一个连接，服务在跑时常驻一个空闲连接。

2026-10-04 生产 8765 每个扫描周期有约 11 秒持续写盘（35～40MiB/s）：导入扫描逐场、逐个文件查库，
一轮开 8104 个连接；进程里没有别的连接开着，每次关闭都做检查点、删掉 -wal/-shm，下一个连接再建，
一轮写 400 多 MiB，CPU 大半花在每个新连接重新解析 schema 上。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter
from meeting_workbench.main import create_app


def _write_meeting(root: Path, meeting_id: str, text: str) -> None:
    directory = root / meeting_id
    directory.mkdir(parents=True)
    (directory / f"{meeting_id}.m4a").write_bytes(f"audio-{meeting_id}".encode())
    (directory / f"{meeting_id}.srt").write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n", encoding="utf-8"
    )
    (directory / "会议纪要.md").write_text(f"# {text}\n", encoding="utf-8")


def _count_connections(monkeypatch) -> dict[str, int]:
    counts = {"connect": 0, "transaction": 0}
    connect, transaction = Database.connect, Database.transaction

    def counting_connect(self):
        counts["connect"] += 1
        return connect(self)

    def counting_transaction(self):
        counts["transaction"] += 1
        return transaction(self)

    monkeypatch.setattr(Database, "connect", counting_connect)
    monkeypatch.setattr(Database, "transaction", counting_transaction)
    return counts


def test_scan_queries_share_one_connection_however_many_meetings_and_files(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    for day in range(1, 5):
        _write_meeting(archive, f"vm-2026010{day}-120000", f"第{day}场正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    first = importer.scan()
    assert first.meetings_seen == 4 and first.errors == 0

    counts = _count_connections(monkeypatch)
    steady = importer.scan()

    assert steady.meetings_seen == 4 and steady.artifacts_seen == 12
    # 写事务照旧各开各的连接拿写锁；其余查询（原来每场会十几次、每个文件一次）只开一个
    assert counts["connect"] - counts["transaction"] == 1


def _app(tmp_path):
    class RelayStub:
        def list_jobs(self, *, status=None, limit=200):
            return []

        def health(self):
            return {
                "status": "healthy",
                "mode": "controlled",
                "worker": {"state": "idle", "heartbeat_age_seconds": 0},
                "counts": {"queued": 0, "active": 0, "failed": 0},
            }

    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        scan_interval_seconds=1,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    return create_app(settings, RelayStub()), settings


def test_service_holds_an_idle_connection_from_startup_to_shutdown(tmp_path, monkeypatch):
    events = []
    held_open = Database.held_open

    def recording_held_open(self):
        events.append("open")
        return held_open(self)

    monkeypatch.setattr(Database, "held_open", recording_held_open)
    app, settings = _app(tmp_path)
    wal = Path(f"{settings.database_path}-wal")
    lifespan_active = []

    with TestClient(app) as client:
        assert events == ["open"]
        for _ in range(5):
            assert client.get("/api/health").status_code == 200
            app.state.db.query_one("SELECT 1 AS one")
            # 短连接关光了 -wal 也还在：常驻连接一直开着
            assert wal.exists()
        lifespan_active.append(app.state.lifespan_active)

    assert lifespan_active == [True]
    assert app.state.lifespan_active is False
