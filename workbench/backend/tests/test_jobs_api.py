import hashlib
import base64
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.main import create_app
from meeting_workbench.relay_client import RelayUnavailable

from .helpers import seed_editable_meeting


class FakeRelayClient:
    def __init__(self):
        self.failure_stage = "transcribing"
        self.audio_path = "~/Downloads/vm-20260102-101500.m4a"
        self.enqueued = []
        self.retried = []
        self.stopped = []
        self.cancelled = []
        self.statuses = {}
        self.published = []
        self.publish_error = None
        self.substate_retried = []
        self.enqueue_error = None
        self.enqueue_options = []
        self.retry_transcripts = []
        self.draft_modified = []
        self.draft_modified_error = None
        self.hotword_calls = []
        self.backend_calls = []

    def list_jobs(self, *, status=None, limit=200):
        jobs = [
            {
                "job_id": "job-abc",
                "status": self.statuses.get("job-abc", "failed"),
                "current_attempt": 1,
                "failure_stage": self.failure_stage,
                "last_error": "模型失败",
            }
        ]
        return [job for job in jobs if status is None or job["status"] == status][:limit]

    def status(self, job_id):
        return {
            "job_id": job_id,
            "status": self.statuses.get(job_id, "failed"),
            "failure_stage": self.failure_stage,
            "audio_path": self.audio_path,
            "attempts": [],
            "events": [],
        }

    def enqueue(
        self, audio_path, *, stage=None, transcript_path=None, hotwords=None, backend=None
    ):
        if self.enqueue_error:
            raise RelayUnavailable(self.enqueue_error)
        self.enqueued.append(str(audio_path))
        snapshot = Path(transcript_path).read_text(encoding="utf-8") if transcript_path else None
        self.enqueue_options.append(
            (stage, str(transcript_path) if transcript_path else None, snapshot)
        )
        self.hotword_calls.append(("enqueue", list(hotwords or [])))
        self.backend_calls.append(("enqueue", backend))
        return "job-new"

    def retry(self, job_id, stage, *, transcript_path=None, hotwords=None, backend=None):
        self.retried.append((job_id, stage))
        snapshot = Path(transcript_path).read_text(encoding="utf-8") if transcript_path else None
        self.retry_transcripts.append((str(transcript_path) if transcript_path else None, snapshot))
        self.hotword_calls.append(("retry", list(hotwords or [])))
        self.backend_calls.append(("retry", backend))
        return {"job_id": job_id, "attempt": 2}

    def mark_draft_modified(self, job_id):
        if self.draft_modified_error:
            raise RelayUnavailable(self.draft_modified_error)
        self.draft_modified.append(job_id)
        return {"job_id": job_id, "status": "draft_modified"}

    def stop_after_stage(self, job_id):
        self.stopped.append(job_id)
        return {"job_id": job_id, "stop_after_stage": True}

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}

    def mark_published(self, job_id, manifest_path, meeting_id):
        if self.publish_error:
            raise RelayUnavailable(self.publish_error)
        self.published.append((job_id, manifest_path, meeting_id))
        return {"job_id": job_id, "status": "published"}

    def retry_substate(self, job_id, name):
        self.substate_retried.append((job_id, name))
        return {"job_id": job_id, "substates": {name: {"status": "pending"}}}


def make_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    relay = FakeRelayClient()
    return TestClient(create_app(settings, relay)), relay


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": "http://testserver"}


def test_jobs_are_visible_and_stage_actions_reach_relay(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)

    assert client.get("/api/jobs").json()["items"][0]["failure_stage"] == "transcribing"
    assert client.get("/api/jobs/job-abc").json()["status"] == "failed"
    retry = client.post("/api/jobs/job-abc/retry", json={"stage": "transcribing"}, headers=headers)
    stop = client.post("/api/jobs/job-abc/stop-after-stage", json={}, headers=headers)
    cancel = client.post("/api/jobs/job-abc/cancel", json={}, headers=headers)
    substate = client.post("/api/jobs/job-abc/substates/whisper/retry", json={}, headers=headers)

    assert retry.status_code == 200
    assert stop.status_code == 200
    assert cancel.status_code == 200
    assert substate.status_code == 200
    assert relay.retried == [("job-abc", "transcribing")]
    assert relay.stopped == ["job-abc"]
    assert relay.cancelled == ["job-abc"]
    assert relay.substate_retried == [("job-abc", "whisper")]


def test_jobs_api_reflects_workbench_draft_modified_status(tmp_path):
    client, relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    relay.statuses["job-abc"] = "completed_unreviewed"
    db.execute(
        """INSERT INTO meetings(id, title, status, source_job_id)
           VALUES ('vm-draft', '草稿会', 'draft_modified', 'job-abc')"""
    )

    job = client.get("/api/jobs").json()["items"][0]

    assert job["meeting_id"] == "vm-draft"
    # 转写录音页卡片标题用会议名，不再只有会议编号
    assert job["meeting_title"] == "草稿会"
    assert job["status"] == "draft_modified"
    assert job["relay_status"] == "completed_unreviewed"


def test_chunked_json_upload_is_saved_locally_and_immediately_enqueued(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    started = client.post(
        "/api/uploads/start",
        json={"filename": "meeting.m4a", "size_bytes": len(b"audio-data")},
        headers=headers,
    )
    upload_id = started.json()["upload_id"]

    chunk = client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        json={"content_base64": base64.b64encode(b"audio-data").decode()},
        headers=headers,
    )
    response = client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers)

    assert chunk.status_code == 200
    assert response.status_code == 200
    assert response.json()["job_id"] == "job-new"
    assert response.json()["status"] == "queued"
    assert relay.enqueued and relay.enqueued[0].endswith("meeting.m4a")


def test_completed_upload_is_retryable_without_retransferring_when_relay_is_down(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    payload = b"audio-data"
    upload_id = client.post(
        "/api/uploads/start",
        json={"filename": "meeting.m4a", "size_bytes": len(payload)},
        headers=headers,
    ).json()["upload_id"]
    client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        json={"content_base64": base64.b64encode(payload).decode()},
        headers=headers,
    )
    relay.enqueue_error = "relay unavailable"

    pending = client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers)

    assert pending.status_code == 202
    assert pending.json()["status"] == "saved_pending_enqueue"
    assert pending.json()["job_id"] is None
    assert pending.json()["path"]

    relay.enqueue_error = None
    retried = client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers)

    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert retried.json()["job_id"] == "job-new"


def test_concurrent_upload_completion_allows_only_one_relay_enqueue_owner(tmp_path, monkeypatch):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    payload = b"audio-data"
    upload_id = client.post(
        "/api/uploads/start",
        json={"filename": "meeting.m4a", "size_bytes": len(payload)},
        headers=headers,
    ).json()["upload_id"]
    client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        json={"content_base64": base64.b64encode(payload).decode()},
        headers=headers,
    )
    original_enqueue = relay.enqueue
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_enqueue(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(3)
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(relay, "enqueue", slow_enqueue)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.post, f"/api/uploads/{upload_id}/complete", json={}, headers=headers
        )
        assert started.wait(3)
        second = executor.submit(
            client.post, f"/api/uploads/{upload_id}/complete", json={}, headers=headers
        )
        second_response = second.result(timeout=3)
        release.set()
        first_response = first.result(timeout=3)

    assert calls == 1
    assert first_response.status_code == 200
    assert second_response.status_code == 202
    assert second_response.json()["status"] == "enqueueing"


def test_upload_recovers_relay_success_before_receipt_commit_with_same_job(
    tmp_path, monkeypatch
):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    payload = b"audio-data"
    upload_id = client.post(
        "/api/uploads/start",
        json={"filename": "meeting.m4a", "size_bytes": len(payload), "hotwords": ["云图"]},
        headers=headers,
    ).json()["upload_id"]
    client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        json={"content_base64": base64.b64encode(payload).decode()},
        headers=headers,
    )
    uploads = client.app.state.uploads
    original_mark = uploads.mark_enqueued
    calls = 0

    def fail_receipt_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated crash after relay success")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(uploads, "mark_enqueued", fail_receipt_once)
    with pytest.raises(OSError, match="after relay success"):
        client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers)
    receipt_path = uploads.sessions_root / upload_id / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["enqueue_lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    uploads._atomic_text(receipt_path, json.dumps(receipt))

    recovered = client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers)

    assert recovered.status_code == 200
    assert recovered.json()["job_id"] == "job-new"
    assert relay.hotword_calls[-2:] == [("enqueue", ["云图"]), ("enqueue", ["云图"])]
    assert uploads.receipt(upload_id)["status"] == "queued"


def test_manual_path_enqueue_is_json_and_returns_job_id(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    audio_path = client.app.state.settings.staging_root / "meeting.m4a"

    response = client.post(
        "/api/jobs/enqueue", json={"audio_path": str(audio_path)}, headers=headers
    )

    assert response.json() == {"job_id": "job-new", "status": "queued"}
    assert relay.enqueued == [str(audio_path.resolve())]


@pytest.mark.parametrize(
    "audio_path",
    ["/etc/passwd", "", "--help", "~/x.m4a"],
)
def test_manual_path_enqueue_rejects_paths_outside_managed_roots(tmp_path, audio_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)

    response = client.post(
        "/api/jobs/enqueue", json={"audio_path": audio_path}, headers=headers
    )

    assert response.status_code == 400
    assert relay.enqueued == []


def test_hotwords_reach_manual_upload_retry_and_retranscribe_without_event_plaintext(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)

    manual = client.post(
        "/api/jobs/enqueue",
        json={
            "audio_path": str(client.app.state.settings.staging_root / "meeting.m4a"),
            "hotwords": [" ＡＣＭＥ ", "ACME", "云图"],
        },
        headers=headers,
    )
    retried = client.post(
        "/api/jobs/job-abc/retry",
        json={"stage": "transcribing", "hotwords": ["客户A"]},
        headers=headers,
    )
    payload = b"audio-data"
    upload_id = client.post(
        "/api/uploads/start",
        json={"filename": "meeting.m4a", "size_bytes": len(payload), "hotwords": ["药品名"]},
        headers=headers,
    ).json()["upload_id"]
    client.put(
        f"/api/uploads/{upload_id}/chunks/0",
        json={"content_base64": base64.b64encode(payload).decode()},
        headers=headers,
    )
    uploaded = client.post(f"/api/uploads/{upload_id}/complete", json={}, headers=headers)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-hotwords")
    db.execute("UPDATE meetings SET source_job_id='job-hotwords' WHERE id='vm-hotwords'")
    relay.statuses["job-hotwords"] = "published"
    retranscribed = client.post(
        "/api/meetings/vm-hotwords/retranscribe",
        json={"hotwords": ["术语甲"]},
        headers=headers,
    )

    assert (manual.status_code, retried.status_code, uploaded.status_code, retranscribed.status_code) == (
        200,
        200,
        200,
        200,
    )
    assert relay.hotword_calls == [
        ("enqueue", ["ACME", "云图"]),
        ("retry", ["客户A"]),
        ("enqueue", ["药品名"]),
        ("retry", ["术语甲"]),
    ]
    event_payload = "\n".join(row["payload_json"] for row in db.query_all("SELECT payload_json FROM events"))
    assert all(term not in event_payload for term in ["ACME", "云图", "客户A", "药品名", "术语甲"])


def test_concurrent_retranscribe_of_legacy_meeting_enqueues_relay_job_once(tmp_path, monkeypatch):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-race")

    original_enqueue = relay.enqueue
    started = threading.Event()
    release = threading.Event()

    def slow_enqueue(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(relay, "enqueue", slow_enqueue)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.post, "/api/meetings/vm-race/retranscribe", json={}, headers=headers
        )
        assert started.wait(3)
        second = executor.submit(
            client.post, "/api/meetings/vm-race/retranscribe", json={}, headers=headers
        )
        release.set()
        first_response = first.result(timeout=3)
        second_response = second.result(timeout=3)

    assert len(relay.enqueued) == 1
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["job_id"] == second_response.json()["job_id"] == "job-new"

    # 连拍第三次：source_job_id 已落库，不应再触发新的 enqueue。
    third = client.post("/api/meetings/vm-race/retranscribe", json={}, headers=headers)
    assert third.status_code == 200
    assert len(relay.enqueued) == 1


def test_minutes_regeneration_requires_and_retries_linked_relay_job(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(
        db, client.app.state.settings.archive_root, meeting_id="vm-linked"
    )
    db.execute(
        """UPDATE meetings SET source_job_id='job-linked', canonical_dir=?
           WHERE id='vm-linked'""",
        (str(meeting_dir),),
    )
    relay.statuses["job-linked"] = "completed_unreviewed"
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-legacy', '历史会', 'published')"
    )

    linked = client.post("/api/meetings/vm-linked/minutes/regenerate", json={}, headers=headers)
    legacy = client.post("/api/meetings/vm-legacy/minutes/regenerate", json={}, headers=headers)

    assert linked.status_code == 200
    assert relay.retried == [("job-linked", "minutes_generating")]
    snapshot_path, snapshot = relay.retry_transcripts[0]
    assert snapshot == (
        "1\n00:00:01,000 --> 00:00:03,000\nSPEAKER_00：第一段内容\n\n"
        "2\n00:00:03,200 --> 00:00:05,000\nSPEAKER_01：第二段内容\n"
    )
    assert snapshot_path.endswith(".srt")
    assert not Path(snapshot_path).exists()
    assert legacy.status_code == 409
    assert "没有可用原音频" in legacy.json()["detail"]


def test_minutes_regeneration_pins_the_requested_backend(tmp_path):
    """「用 Claude 重写纪要」必须把后端透传到 relay，并挡住乱填的值。"""
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(
        db, client.app.state.settings.archive_root, meeting_id="vm-linked"
    )
    db.execute(
        """UPDATE meetings SET source_job_id='job-linked', canonical_dir=?
           WHERE id='vm-linked'""",
        (str(meeting_dir),),
    )
    relay.statuses["job-linked"] = "completed_unreviewed"

    default = client.post(
        "/api/meetings/vm-linked/minutes/regenerate", json={}, headers=headers
    )
    assert default.status_code == 200
    assert relay.backend_calls[-1] == ("retry", None)

    pinned = client.post(
        "/api/meetings/vm-linked/minutes/regenerate",
        json={"backend": "claude"},
        headers=headers,
    )
    assert pinned.status_code == 200
    assert relay.backend_calls[-1] == ("retry", "claude")
    assert pinned.json()["llm_backend"] == "claude"

    rejected = client.post(
        "/api/meetings/vm-linked/minutes/regenerate",
        json={"backend": "gpt5"},
        headers=headers,
    )
    assert rejected.status_code == 422
    assert relay.backend_calls[-1] == ("retry", "claude")


def test_minutes_regeneration_enqueues_historical_audio_at_minutes_stage(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    _meeting_dir, _audio, _version = seed_editable_meeting(
        db, client.app.state.settings.archive_root, meeting_id="vm-history"
    )

    response = client.post("/api/meetings/vm-history/minutes/regenerate", json={}, headers=headers)

    assert response.status_code == 200
    assert relay.enqueued
    stage, snapshot_path, snapshot = relay.enqueue_options[-1]
    assert stage == "minutes_generating"
    assert snapshot == (
        "1\n00:00:01,000 --> 00:00:03,000\nSPEAKER_00：第一段内容\n\n"
        "2\n00:00:03,200 --> 00:00:05,000\nSPEAKER_01：第二段内容\n"
    )
    assert snapshot_path.endswith(".srt")
    assert not Path(snapshot_path).exists()


def test_minutes_regeneration_uses_edited_transcript_and_rejects_active_job(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-edited")
    db.execute("UPDATE meetings SET source_job_id='job-edited' WHERE id='vm-edited'")
    service = client.app.state.service
    draft_id = service.ensure_draft("vm-edited")
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (draft_id,)
    )
    segments[0]["text"] = "这是刚刚修改后的正文"
    service.save_segments("vm-edited", segments)
    relay.statuses["job-edited"] = "published"

    response = client.post("/api/meetings/vm-edited/minutes/regenerate", json={}, headers=headers)

    assert response.status_code == 200
    snapshot_path, snapshot = relay.retry_transcripts[-1]
    assert "这是刚刚修改后的正文" in snapshot
    assert hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    assert not Path(snapshot_path).exists()

    relay.statuses["job-edited"] = "transcribing"
    blocked = client.post("/api/meetings/vm-edited/minutes/regenerate", json={}, headers=headers)
    assert blocked.status_code == 409
    assert "正在处理" in blocked.json()["detail"]


def test_minutes_snapshot_is_removed_when_relay_rejects_regeneration(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-cleanup")
    db.execute("UPDATE meetings SET source_job_id='job-cleanup' WHERE id='vm-cleanup'")
    relay.statuses["job-cleanup"] = "published"

    def fail_retry(job_id, stage, *, transcript_path=None, hotwords=None, backend=None):
        relay.retry_transcripts.append((str(transcript_path), Path(transcript_path).read_text()))
        raise RelayUnavailable("rejected")

    relay.retry = fail_retry
    response = client.post("/api/meetings/vm-cleanup/minutes/regenerate", json={}, headers=headers)

    assert response.status_code == 409
    assert not Path(relay.retry_transcripts[-1][0]).exists()


def test_edit_marks_linked_relay_job_draft_modified_without_losing_edit_on_relay_error(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-edit-state")
    db.execute("UPDATE meetings SET source_job_id='job-edit-state' WHERE id='vm-edit-state'")

    first = client.put(
        "/api/meetings/vm-edit-state/minutes",
        json={"base_version_id": None, "markdown": "# 人工修改"},
        headers=headers,
    )
    assert first.status_code == 200
    assert relay.draft_modified == ["job-edit-state"]

    relay.draft_modified_error = "relay db unavailable"
    second = client.put(
        "/api/meetings/vm-edit-state/minutes",
        json={"base_version_id": first.json()["version_id"], "markdown": "# 第二次修改"},
        headers=headers,
    )
    assert second.status_code == 200
    assert db.query_one("SELECT status FROM meetings WHERE id='vm-edit-state'")["status"] == (
        "draft_modified"
    )
    event = db.query_one(
        "SELECT event_type FROM events WHERE meeting_id='vm-edit-state' ORDER BY id DESC LIMIT 1"
    )
    assert event["event_type"] == "relay_draft_modified_sync_failed"


def test_publish_acknowledges_relay_and_does_not_report_ack_failure_as_success(tmp_path):
    client, relay = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(client.app.state.settings.database_path)
    settings = client.app.state.settings
    meeting_dir, _audio, _version = seed_editable_meeting(
        db, settings.archive_root / ".workbench-drafts" / "job-publish"
    )
    db.execute(
        """UPDATE meetings SET canonical_dir=?, source_priority=51,
           source_job_id='job-publish' WHERE id='vm-20260102-101500'""",
        (str(meeting_dir),),
    )
    db.execute("UPDATE artifacts SET source_root='draft' WHERE meeting_id='vm-20260102-101500'")
    service = client.app.state.service
    service.ensure_draft("vm-20260102-101500")
    minutes_id = service.save_minutes("vm-20260102-101500", "# 发布回执")
    manifest_path = meeting_dir / "workbench-manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "job_id": "job-publish", "attempt": 1}),
        encoding="utf-8",
    )
    stat = manifest_path.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('vm-20260102-101500', 'manifest', 'source', 'draft', ?, ?, ?, CURRENT_TIMESTAMP)""",
        (str(manifest_path), stat.st_size, stat.st_mtime_ns),
    )
    db.execute(
        """UPDATE minutes_versions SET source_job_id='job-publish', source_attempt=1
           WHERE id=?""",
        (minutes_id,),
    )

    success = client.post("/api/meetings/vm-20260102-101500/publish", json={}, headers=headers)

    assert success.status_code == 200
    assert relay.published and relay.published[0][0] == "job-publish"
    assert (
        db.query_one("SELECT status FROM meetings WHERE id='vm-20260102-101500'")["status"]
        == "published"
    )

    db.execute("UPDATE meetings SET status='draft_modified' WHERE id='vm-20260102-101500'")
    relay.publish_error = "relay db unavailable"
    failed = client.post("/api/meetings/vm-20260102-101500/publish", json={}, headers=headers)

    assert failed.status_code == 503
    assert "未显示成功" in failed.json()["detail"]
    assert (
        db.query_one("SELECT status FROM meetings WHERE id='vm-20260102-101500'")["status"]
        == "draft_modified"
    )


def _seed_stalled_minutes_job(client, tmp_path, *, job_id="job-abc"):
    db = client.app.state.db
    seed_editable_meeting(db, tmp_path / "archive")
    db.execute(
        "UPDATE meetings SET source_job_id=? WHERE id='vm-20260102-101500'",
        (job_id,),
    )


def test_stalled_minutes_are_regenerated_once_per_cooldown(tmp_path):
    client, relay = make_client(tmp_path)
    relay.failure_stage = "codex_callback"
    _seed_stalled_minutes_job(client, tmp_path)

    assert client.app.state.recover_stalled_minutes() == 1
    assert relay.retried == [("job-abc", "minutes_generating")]

    # 冷却期内不重复重派，避免同一个任务被反复打回纪要阶段。
    assert client.app.state.recover_stalled_minutes() == 0
    assert relay.retried == [("job-abc", "minutes_generating")]

    events = client.app.state.db.query_all(
        "SELECT actor, event_type FROM events WHERE event_type='minutes_auto_recovery_requested'"
    )
    assert [event["actor"] for event in events] == ["system"]


def test_auto_recovery_skips_meetings_that_already_have_minutes(tmp_path):
    client, relay = make_client(tmp_path)
    relay.failure_stage = "codex_callback"
    _seed_stalled_minutes_job(client, tmp_path)
    client.app.state.db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, kind, published, created_at)
           VALUES ('mv-1', 'vm-20260102-101500', 1, '# 已有纪要', 'agent', 0, ?)""",
        ("2026-07-20T00:00:00+00:00",),
    )

    assert client.app.state.recover_stalled_minutes() == 0
    assert relay.retried == []


def test_auto_recovery_ignores_failures_outside_the_minutes_stage(tmp_path):
    client, relay = make_client(tmp_path)
    relay.failure_stage = "transcribing"
    _seed_stalled_minutes_job(client, tmp_path)

    assert client.app.state.recover_stalled_minutes() == 0
    assert relay.retried == []


def test_auto_recovery_stops_after_the_attempt_ceiling(tmp_path):
    client, relay = make_client(tmp_path)
    relay.failure_stage = "codex_callback"
    _seed_stalled_minutes_job(client, tmp_path)
    db = client.app.state.db
    for _ in range(2):
        db.add_event(
            "minutes_auto_recovery_requested",
            meeting_id="vm-20260102-101500",
            job_id="job-abc",
            actor="system",
            payload={},
        )
    db.execute(
        "UPDATE events SET created_at='2020-01-01T00:00:00+00:00' "
        "WHERE event_type='minutes_auto_recovery_requested'"
    )

    assert client.app.state.recover_stalled_minutes() == 0
    assert relay.retried == []


def test_auto_recovery_links_a_job_to_its_meeting_by_recording_name(tmp_path):
    client, relay = make_client(tmp_path)
    relay.failure_stage = "codex_callback"
    # 纪要从未生成的任务没有 manifest，两侧一开始并没有关联。
    seed_editable_meeting(client.app.state.db, tmp_path / "archive")

    assert client.app.state.recover_stalled_minutes() == 1
    assert relay.retried == [("job-abc", "minutes_generating")]
    assert client.app.state.db.query_one(
        "SELECT source_job_id FROM meetings WHERE id='vm-20260102-101500'"
    )["source_job_id"] == "job-abc"


def test_auto_recovery_ignores_an_unrelated_recording_name(tmp_path):
    client, relay = make_client(tmp_path)
    relay.failure_stage = "codex_callback"
    relay.audio_path = "~/Downloads/vm-20991231-235959-deadbeef.m4a"
    seed_editable_meeting(client.app.state.db, tmp_path / "archive")

    assert client.app.state.recover_stalled_minutes() == 0
    assert relay.retried == []
