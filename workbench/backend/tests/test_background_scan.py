import hashlib
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.main import create_app
from meeting_workbench.qwen_shadow import QwenShadowService
from meeting_workbench.semantic import SemanticIndex, SemanticUnavailable
from meeting_workbench.uploads import UploadManager


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


@pytest.mark.parametrize("failing_method", ["cleanup_expired", "pending"])
def test_background_upload_phase_failure_degrades_scanner_without_killing_loop(
    tmp_path, monkeypatch, failing_method
):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        scan_interval_seconds=0.02,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    calls = 0

    def fail_phase(_self):
        nonlocal calls
        calls += 1
        raise RuntimeError(f"simulated {failing_method} failure")

    monkeypatch.setattr(UploadManager, failing_method, fail_phase)
    app = create_app(settings, RelayStub())

    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        health = client.get("/api/health").json()
        while time.monotonic() < deadline:
            health = client.get("/api/health").json()
            if (
                health["services"]["scanner"] == "degraded"
                and health["details"]["scanner"]["consecutive_failures"] >= 2
            ):
                break
            time.sleep(0.02)

        assert calls >= 2
        assert health["services"]["scanner"] == "degraded"
        assert health["details"]["scanner"]["consecutive_failures"] >= 2
        assert health["details"]["scanner"]["last_error_type"] == "RuntimeError"
        assert health["details"]["scanner"]["loop_alive"] is True


def test_startup_upload_phase_failure_is_immediately_reported_as_degraded(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        scan_interval_seconds=60,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()

    def fail_cleanup(_self):
        raise RuntimeError("simulated startup cleanup failure")

    monkeypatch.setattr(UploadManager, "cleanup_expired", fail_cleanup)
    app = create_app(settings, RelayStub())

    with TestClient(app) as client:
        health = client.get("/api/health").json()

        assert health["services"]["scanner"] == "degraded"
        assert health["details"]["scanner"]["consecutive_failures"] == 1
        assert health["details"]["scanner"]["last_error_type"] == "RuntimeError"
        assert health["details"]["scanner"]["loop_alive"] is True


@pytest.mark.parametrize("visible_pending", [False, True])
def test_completed_relay_draft_appears_without_manual_scan(tmp_path, visible_pending):
    archive = tmp_path / "archive"
    relay_db = tmp_path / "relay.sqlite3"
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        relay_jobs_db=relay_db,
        semantic_enabled=False,
        scan_interval_seconds=0.05,
    )
    app = create_app(settings, RelayStub())

    with TestClient(app) as client:
        meeting_id = "vm-20260710-150000-abcd1234"
        attempt = (
            archive / "待校对" / "260710 后台扫描"
            if visible_pending
            else archive / ".workbench-drafts" / "job-background" / "attempt-1"
        )
        attempt.mkdir(parents=True)
        files = {
            f"{meeting_id}.m4a": b"audio",
            f"{meeting_id}.srt": b"1\n00:00:00,000 --> 00:00:01,000\nauto scan\n",
            f"{meeting_id}.txt": b"auto scan",
            "spk.txt": b"SPEAKER_00\tuser",
            f"{meeting_id}.funasr.json": b"{}",
            f"{meeting_id}.funasr.log": b"ok",
            "meeting.md": b"# auto minutes",
            "meeting.html": b"<h1>auto minutes</h1>",
        }
        for relative, content in files.items():
            (attempt / relative).write_bytes(content)
        (attempt / "workbench-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": "job-background",
                    "attempt": 1,
                    "artifacts": [
                        {
                            "path": relative,
                            "bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                        for relative, content in files.items()
                    ],
                }
            ),
            encoding="utf-8",
        )
        with sqlite3.connect(relay_db) as connection:
            connection.execute(
                "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
            )
            connection.execute(
                "INSERT INTO jobs VALUES ('job-background', 'completed_unreviewed', 1, ?)",
                (str(attempt),),
            )

        deadline = time.monotonic() + 3
        items = []
        while time.monotonic() < deadline:
            items = client.get("/api/meetings").json()["items"]
            if items:
                break
            time.sleep(0.05)

        assert len(items) == 1
        assert items[0]["id"] == meeting_id
        assert items[0]["status"] == "completed_unreviewed"


def test_background_semantic_failure_does_not_mark_scanner_failed(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=True,
        scan_interval_seconds=0.02,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    calls = 0

    monkeypatch.setattr(SemanticIndex, "warm", lambda _self: None)

    def fail_after_startup(_self, *, force=False):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise SemanticUnavailable("simulated semantic outage")
        return 0

    monkeypatch.setattr(SemanticIndex, "rebuild", fail_after_startup)
    app = create_app(settings, RelayStub())

    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        health = client.get("/api/health").json()
        while time.monotonic() < deadline:
            health = client.get("/api/health").json()
            if health["services"]["semantic"] == "unavailable":
                break
            time.sleep(0.02)

        assert health["services"]["semantic"] == "unavailable"
        assert health["services"]["scanner"] == "healthy"


def test_unexpected_semantic_failure_does_not_end_future_scans(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=True,
        scan_interval_seconds=0.02,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    calls = 0

    monkeypatch.setattr(SemanticIndex, "warm", lambda _self: None)

    def fail_once_after_startup(_self, *, force=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unexpected encoder failure")
        return 0

    monkeypatch.setattr(SemanticIndex, "rebuild", fail_once_after_startup)
    app = create_app(settings, RelayStub())

    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        while calls < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls >= 2

        source = settings.archive_root / "new-after-failure"
        source.mkdir()
        (source / "new.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n扫描仍然存活\n",
            encoding="utf-8",
        )

        items = []
        while time.monotonic() < deadline:
            items = client.get("/api/meetings").json()["items"]
            if items:
                break
            time.sleep(0.02)

        assert len(items) == 1
        health = client.get("/api/health").json()
        assert health["services"]["scanner"] == "healthy"
        assert health["details"]["scanner"]["loop_alive"] is True


def test_health_reads_cached_relay_probe_without_calling_slow_job_list(tmp_path):
    class SlowRelay(RelayStub):
        def list_jobs(self, *, status=None, limit=200):
            time.sleep(0.4)
            return []

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
    app = create_app(settings, SlowRelay())

    with TestClient(app) as client:
        started = time.monotonic()
        health = client.get("/api/health").json()
        elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert health["services"]["relay_worker"] == "healthy"


def test_qwen_loop_reports_failure_and_continues_next_iteration(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        scan_interval_seconds=0.02,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    second_started = threading.Event()
    release_second = threading.Event()
    calls = 0

    def fail_once_then_block(_self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated qwen worker failure")
        second_started.set()
        release_second.wait(3)
        return False

    monkeypatch.setattr(QwenShadowService, "run_once", fail_once_then_block)
    app = create_app(settings, RelayStub())

    with TestClient(app) as client:
        assert second_started.wait(2)
        health = client.get("/api/health").json()

        assert health["services"]["qwen_worker"] == "degraded"
        assert health["details"]["qwen_worker"]["loop_alive"] is True
        assert health["details"]["qwen_worker"]["consecutive_failures"] == 1
        assert health["details"]["qwen_worker"]["last_error_type"] == "RuntimeError"

        release_second.set()
        deadline = time.monotonic() + 2
        while calls < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls >= 3


def test_health_degrades_for_permanently_queued_qwen_without_leaking_content(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    app = create_app(settings, RelayStub())
    app.state.db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-secret', '不得泄露标题', 'published')"
    )
    app.state.db.execute(
        """INSERT INTO asr_shadow_runs
           (id, meeting_id, engine, model, state, config_json, created_at, updated_at)
           VALUES ('shadow-stuck', 'vm-secret', 'qwen3_asr', 'Qwen/Qwen3-ASR-0.6B',
                   'queued', '{}', '2000-01-01T00:00:00+00:00',
                   '2000-01-01T00:00:00+00:00')"""
    )

    health = TestClient(app).get("/api/health").json()

    assert health["services"]["qwen_worker"] == "degraded"
    assert health["details"]["qwen_worker"]["queued_count"] == 1
    assert health["details"]["qwen_worker"]["queued_stale"] is True
    serialized = json.dumps(health, ensure_ascii=False)
    assert "不得泄露标题" not in serialized
    assert "vm-secret" not in serialized


def test_health_degrades_for_incomplete_running_qwen_lease(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    app = create_app(settings, RelayStub())
    app.state.db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-orphan', '孤儿任务', 'published')"
    )
    app.state.db.execute(
        """INSERT INTO asr_shadow_runs
           (id, meeting_id, engine, model, state, config_json, owner_id,
            created_at, updated_at)
           VALUES ('shadow-orphan', 'vm-orphan', 'qwen3_asr', 'Qwen/Qwen3-ASR-0.6B',
                   'running', '{}', 'legacy-owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
    )

    health = TestClient(app).get("/api/health").json()

    assert health["services"]["qwen_worker"] == "degraded"
    assert health["details"]["qwen_worker"]["orphaned_running_count"] == 1
