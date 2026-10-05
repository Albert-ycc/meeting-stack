"""后台循环里「每次调用都新开连接」的几处：一轮、一次查询共用一个连接，开连接的次数不再随数据量涨。"""

import hashlib
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter
from meeting_workbench.main import create_app


def seed_managed_drafts(archive, relay_db, count):
    """造 count 个 relay 受管草稿目录，并在 relay 任务库里登记，和 relay 完成转写后落盘的样子一致。"""
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        for index in range(count):
            meeting_id = f"vm-20260710-15{index:02d}00-abcd{index:04x}"
            job_id = f"job-shared-{index}"
            attempt = archive / ".workbench-drafts" / job_id / "attempt-1"
            attempt.mkdir(parents=True)
            files = {
                f"{meeting_id}.m4a": b"audio",
                f"{meeting_id}.srt": b"1\n00:00:00,000 --> 00:00:01,000\nshared\n",
                f"{meeting_id}.txt": b"shared",
                "spk.txt": b"SPEAKER_00\tuser",
                f"{meeting_id}.funasr.json": b"{}",
                f"{meeting_id}.funasr.log": b"ok",
                "meeting.md": b"# shared minutes",
                "meeting.html": b"<h1>shared minutes</h1>",
            }
            for name, content in files.items():
                (attempt / name).write_bytes(content)
            (attempt / "workbench-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "attempt": 1,
                        "artifacts": [
                            {
                                "path": name,
                                "bytes": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                            for name, content in files.items()
                        ],
                    }
                ),
                encoding="utf-8",
            )
            connection.execute(
                "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?)", (job_id, str(attempt))
            )


def test_one_scan_opens_the_relay_jobs_db_once_however_many_managed_drafts_it_checks(
    tmp_path, monkeypatch
):
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
    seed_managed_drafts(settings.archive_root, settings.relay_jobs_db, 5)
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    relay_opens = []
    real_connect = sqlite3.connect

    def counting_connect(target, *args, **kwargs):
        if "relay.sqlite3" in str(target):
            relay_opens.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    first = importer.scan()
    opens_first = len(relay_opens)
    relay_opens.clear()
    second = importer.scan()

    assert first.meetings_created == 5 and first.errors == 0
    assert second.meetings_created == 0 and second.errors == 0
    # 5 个草稿目录各要查一次任务库：原来各开一个连接，现在整轮共用一个
    assert opens_first == 1
    assert len(relay_opens) == 1
    assert importer._relay_shared is None  # 轮结束连接已关、没留在实例上
    assert not importer._relay_sharing


def test_relay_jobs_db_connection_is_per_call_outside_a_scan(tmp_path, monkeypatch):
    """发布时算签名走的是 scan 之外的路径：不借用整轮连接，用完即关。"""
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
    seed_managed_drafts(settings.archive_root, settings.relay_jobs_db, 3)
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)

    with importer._relay_jobs_connection() as first:
        first.execute("SELECT 1").fetchone()
    with importer._relay_jobs_connection() as second:
        assert second is not first
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")


class FailedJobsRelay:
    """relay 里有几个失败的任务：刷新「要你处理」的快照时每个任务都要查几次库。"""

    def __init__(self, count):
        self.jobs = [
            {"job_id": f"job-attn-{index}", "status": "failed", "failure_stage": "whisper"}
            for index in range(count)
        ]

    def list_jobs(self, *, status=None, limit=200):
        return self.jobs

    def health(self):
        return {
            "status": "healthy",
            "mode": "controlled",
            "worker": {"state": "idle", "heartbeat_age_seconds": 0},
            "counts": {"queued": 0, "active": 0, "failed": len(self.jobs)},
        }


def test_attention_refresh_opens_one_database_connection_however_many_jobs_need_attention(
    tmp_path, monkeypatch
):
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
    app = create_app(settings, FailedJobsRelay(6))
    opens = []
    real_connect = sqlite3.connect

    this_thread = threading.get_ident()

    def counting_connect(target, *args, **kwargs):
        # 只数本线程：后台循环自己也在开连接，不是这一次刷新开的
        if "db.sqlite3" in str(target) and threading.get_ident() == this_thread:
            opens.append(str(target))
        return real_connect(target, *args, **kwargs)

    with TestClient(app):
        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        items = app.state.refresh_attention_jobs()

    assert items is not None and len(items) == 6
    # 6 个任务各查一次会议：原来每次新开一个连接，现在整轮共用一个
    assert len(opens) == 1
