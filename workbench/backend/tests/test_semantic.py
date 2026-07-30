import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench import semantic as semantic_module
from meeting_workbench.semantic import SemanticBusy, SemanticIndex, SemanticPaused
from meeting_workbench.main import create_app


class FakeEmbedder:
    def encode(self, texts, **_kwargs):
        vectors = []
        for text in texts:
            if "随访" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "营养" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return np.asarray(vectors, dtype=np.float32)


def test_semantic_search_returns_sentence_and_time_anchor(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "workbench.sqlite3",
        semantic_enabled=True,
    )
    db = Database(settings.database_path)
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-sem', '语义测试', 'completed_unreviewed')"
    )
    version = db.create_transcript_version("vm-sem", "funasr", published=True)
    db.replace_segments(
        version,
        "vm-sem",
        [
            {
                "id": "seg-follow",
                "ordinal": 0,
                "start_ms": 8800,
                "end_ms": 9300,
                "text": "患者随访需要延长两周",
            },
            {
                "id": "seg-nutri",
                "ordinal": 1,
                "start_ms": 9400,
                "end_ms": 9800,
                "text": "营养方案下周调整",
            },
        ],
    )
    index = SemanticIndex(db, settings, embedder=FakeEmbedder())

    assert index.rebuild() == 2
    results = index.search("随访计划", limit=1)

    assert results[0]["segment_id"] == "seg-follow"
    assert results[0]["match_kind"] == "segment"
    assert results[0]["start_ms"] == 8800
    assert results[0]["text"] == "患者随访需要延长两周"


def test_semantic_rebuild_pauses_while_transcriber_is_busy(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "db.sqlite3")
    db = Database(settings.database_path)
    db.initialize()
    index = SemanticIndex(db, settings, embedder=FakeEmbedder(), busy_check=lambda: True)

    with pytest.raises(SemanticPaused, match="转写"):
        index.rebuild()


def test_semantic_rebuild_drops_embeddings_from_noncurrent_versions(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "db.sqlite3")
    db = Database(settings.database_path)
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-old', '版本', 'published')")
    old = db.create_transcript_version("vm-old", "funasr", published=True)
    db.replace_segments(
        old,
        "vm-old",
        [{"id": "old-segment", "start_ms": 0, "end_ms": 1, "text": "旧随访"}],
    )
    index = SemanticIndex(db, settings, embedder=FakeEmbedder(), busy_check=lambda: False)
    index.rebuild()
    new = db.create_transcript_version("vm-old", "draft", published=False)
    db.replace_segments(
        new,
        "vm-old",
        [{"id": "new-segment", "start_ms": 0, "end_ms": 1, "text": "新随访"}],
    )

    index.rebuild()

    rows = db.query_all("SELECT segment_id FROM embeddings ORDER BY segment_id")
    assert rows == [{"segment_id": "new-segment"}]


def test_semantic_search_get_does_not_rebuild_or_sync_relay_substate(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    app = create_app(settings)
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO meetings(id, title, status, source_job_id)
           VALUES ('vm-read-only', '只读语义', 'published', 'job-read-only')"""
    )
    version = db.create_transcript_version("vm-read-only", "funasr", published=True)
    db.replace_segments(
        version,
        "vm-read-only",
        [{"id": "seg-read-only", "start_ms": 0, "end_ms": 1000, "text": "只读查询"}],
    )
    calls = {"rebuild": 0, "relay_status": 0, "relay_set": 0}

    def rebuild(*_args, **_kwargs):
        calls["rebuild"] += 1
        return 0

    def relay_status(_job_id):
        calls["relay_status"] += 1
        return {
            "current_attempt": 2,
            "substates": {"index": {"status": "ready"}},
        }

    def relay_set(*_args, **_kwargs):
        calls["relay_set"] += 1
        return {}

    monkeypatch.setattr(app.state.semantic, "rebuild", rebuild)
    monkeypatch.setattr(app.state.semantic, "search", lambda _query, limit: [])
    monkeypatch.setattr(app.state.relay, "status", relay_status)
    monkeypatch.setattr(app.state.relay, "set_substate", relay_set)

    response = TestClient(app).get("/api/search", params={"q": "查询", "mode": "semantic"})

    assert response.status_code == 200
    assert calls == {"rebuild": 0, "relay_status": 0, "relay_set": 0}


def test_semantic_rebuild_is_single_flight_and_second_caller_gets_busy(tmp_path):
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "db.sqlite3")
    db = Database(settings.database_path)
    db.initialize()
    db.execute("INSERT INTO meetings(id, title) VALUES ('vm-busy', '并发索引')")
    version = db.create_transcript_version("vm-busy", "funasr", published=True)
    db.replace_segments(
        version,
        "vm-busy",
        [{"id": "seg-busy", "start_ms": 0, "end_ms": 1, "text": "并发重建"}],
    )
    entered = threading.Event()

    class BlockingEmbedder(FakeEmbedder):
        def encode(self, texts, **kwargs):
            entered.set()
            time.sleep(0.15)
            return super().encode(texts, **kwargs)

    index = SemanticIndex(db, settings, embedder=BlockingEmbedder(), busy_check=lambda: False)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(index.rebuild)
        assert entered.wait(timeout=1)
        second = executor.submit(index.rebuild)
        with pytest.raises(semantic_module.SemanticBusy, match="正在重建"):
            second.result(timeout=1)
        assert first.result(timeout=1) == 1


def test_admin_semantic_rebuild_returns_409_when_another_rebuild_is_running(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    app = create_app(settings)

    def busy(**_kwargs):
        raise SemanticBusy("语义索引正在重建")

    monkeypatch.setattr(app.state.semantic, "rebuild", busy)
    client = TestClient(app)
    token = client.get("/api/bootstrap").json()["csrf_token"]

    response = client.post(
        "/api/admin/semantic/rebuild",
        json={},
        headers={"Origin": "http://testserver", "X-CSRF-Token": token},
    )

    assert response.status_code == 409


def test_admin_semantic_rebuild_returns_409_when_transcription_pauses_index(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    app = create_app(settings)

    def paused(**_kwargs):
        raise SemanticPaused("FunASR 转写运行中，语义索引已暂停")

    monkeypatch.setattr(app.state.semantic, "rebuild", paused)
    client = TestClient(app)
    token = client.get("/api/bootstrap").json()["csrf_token"]

    response = client.post(
        "/api/admin/semantic/rebuild",
        json={},
        headers={"Origin": "http://testserver", "X-CSRF-Token": token},
    )

    assert response.status_code == 409


def test_health_reports_semantic_paused_while_funasr_is_busy(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    archive.mkdir()
    staging.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
        staging_root=staging,
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=True,
        scan_interval_seconds=0.05,
    )
    app = create_app(settings)
    app.state.semantic._embedder = FakeEmbedder()
    app.state.semantic.busy_check = lambda: True

    with TestClient(app) as client:
        health = client.get("/api/health").json()

    assert health["services"]["semantic"] == "paused"
    assert health["details"]["semantic"]["status"] == "paused"
    assert health["details"]["semantic"]["consecutive_failures"] == 0
