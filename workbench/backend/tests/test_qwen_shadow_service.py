import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.main import create_app
from meeting_workbench.qwen_shadow import QwenShadowService
from fastapi.testclient import TestClient

from .helpers import seed_editable_meeting


class Relay:
    def __init__(self, *, transcribing=False):
        self.transcribing = transcribing

    def list_jobs(self, *, status=None, limit=200):
        if status == "transcribing" and self.transcribing:
            return [{"job_id": "job-active", "status": "transcribing"}]
        return []

    def status(self, job_id):
        return {"job_id": job_id, "status": "completed_unreviewed", "substates": {}}


def setup(tmp_path, *, binary=True, relay=None, **setting_overrides):
    binary_path = tmp_path / "bin" / "mlx-qwen3-asr"
    if binary:
        binary_path.parent.mkdir()
        binary_path.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        qwen_binary=binary_path,
        qwen_timeout_seconds=30,
        **setting_overrides,
    )
    db = Database(settings.database_path)
    db.initialize()
    _meeting_dir, audio, version = seed_editable_meeting(
        db, settings.archive_root, meeting_id="vm-qwen"
    )
    return settings, db, audio, version, QwenShadowService(db, settings, relay or Relay())


def test_qwen_shadow_runs_offline_imports_srt_as_noncurrent_reference(tmp_path, monkeypatch):
    settings, db, audio, current_version, service = setup(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nQwen影子稿\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", fake_run)
    run = service.request("vm-qwen")

    assert service.run_once() is True
    saved = db.query_one("SELECT * FROM asr_shadow_runs WHERE id=?", (run["id"],))
    assert saved["state"] == "ready"
    version = db.query_one(
        "SELECT * FROM transcript_versions WHERE id=?", (saved["transcript_version_id"],)
    )
    assert version["kind"] == "qwen_reference"
    assert (
        db.query_one("SELECT current_transcript_version_id FROM meetings WHERE id='vm-qwen'")[
            "current_transcript_version_id"
        ]
        == current_version
    )
    assert db.query_one("SELECT text FROM segments WHERE version_id=?", (version["id"],))[
        "text"
    ] == ("Qwen影子稿")
    command, kwargs = calls[0]
    assert command[:2] == [str(settings.qwen_binary), str(audio)]
    assert "Qwen/Qwen3-ASR-0.6B" in command
    assert "--timestamps" in command and command[command.index("-f") + 1] == "all"
    assert kwargs["timeout"] == 30
    assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert kwargs["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert kwargs["env"]["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert kwargs["env"]["DO_NOT_TRACK"] == "1"
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == saved["audio_sha256"]


def test_qwen_missing_binary_is_unavailable_without_path_or_transcript_leak(tmp_path):
    settings, db, _audio, current_version, service = setup(tmp_path, binary=False)

    run = service.request("vm-qwen")

    assert run["state"] == "unavailable"
    assert str(settings.qwen_binary) not in (run["error"] or "")
    assert (
        db.query_one("SELECT current_transcript_version_id FROM meetings WHERE id='vm-qwen'")[
            "current_transcript_version_id"
        ]
        == current_version
    )


def test_qwen_audio_mutation_fails_closed_and_creates_no_version(tmp_path, monkeypatch):
    _settings, db, audio, current_version, service = setup(tmp_path)

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n不得导入\n", encoding="utf-8"
        )
        audio.write_bytes(b"changed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", fake_run)
    run = service.request("vm-qwen")

    assert service.run_once() is True
    saved = db.query_one("SELECT * FROM asr_shadow_runs WHERE id=?", (run["id"],))
    assert saved["state"] == "failed"
    assert saved["error"] == "原音频完整性校验失败"
    assert "不得导入" not in (saved["error"] or "")
    assert saved["transcript_version_id"] is None
    assert (
        db.query_one("SELECT current_transcript_version_id FROM meetings WHERE id='vm-qwen'")[
            "current_transcript_version_id"
        ]
        == current_version
    )


def test_qwen_skips_while_relay_transcribes_and_recovers_orphaned_running(tmp_path):
    _settings, db, _audio, _current, service = setup(tmp_path, relay=Relay(transcribing=True))
    run = service.request("vm-qwen")
    assert service.run_once() is False
    assert db.query_one("SELECT state FROM asr_shadow_runs WHERE id=?", (run["id"],))["state"] == (
        "queued"
    )
    db.execute(
        """UPDATE asr_shadow_runs
              SET state='running', owner_id='expired-owner', heartbeat_at=?,
                  lease_expires_at='2000-01-01T00:00:00+00:00', updated_at=?
            WHERE id=?""",
        (utc_now(), utc_now(), run["id"]),
    )

    assert service.recover_orphaned() == 1
    recovered = db.query_one("SELECT state, error FROM asr_shadow_runs WHERE id=?", (run["id"],))
    assert recovered == {"state": "queued", "error": "租约过期后重新排队"}


def test_qwen_retry_creates_new_run_and_rejects_output_symlink(tmp_path, monkeypatch):
    _settings, db, _audio, _current, service = setup(tmp_path)
    original = service.request("vm-qwen")
    db.execute(
        "UPDATE asr_shadow_runs SET state='failed', finished_at=? WHERE id=?",
        (utc_now(), original["id"]),
    )
    retry = service.retry("vm-qwen", original["id"])
    assert retry["id"] != original["id"]
    assert json.loads(retry["config_json"])["retry_of"] == original["id"]

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").symlink_to(tmp_path / "outside.srt")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", fake_run)
    assert service.run_once() is True
    failed = db.query_one("SELECT state, error FROM asr_shadow_runs WHERE id=?", (original["id"],))
    assert failed["state"] == "failed"
    assert str(tmp_path) not in (failed["error"] or "")


def test_qwen_rejects_non_utf8_srt_and_checks_audio_after_timeout(tmp_path, monkeypatch):
    _settings, db, audio, current_version, service = setup(tmp_path)

    def invalid_utf8(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\n\xff\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", invalid_utf8)
    first = service.request("vm-qwen")
    assert service.run_once() is True
    assert (
        db.query_one("SELECT state FROM asr_shadow_runs WHERE id=?", (first["id"],))["state"]
        == "failed"
    )
    assert (
        db.query_one("SELECT current_transcript_version_id FROM meetings WHERE id='vm-qwen'")[
            "current_transcript_version_id"
        ]
        == current_version
    )

    def timeout_after_mutation(_command, **_kwargs):
        audio.write_bytes(b"mutated-on-timeout")
        raise subprocess.TimeoutExpired("mlx-qwen3-asr", 30)

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", timeout_after_mutation)
    second = service.request("vm-qwen")
    assert service.run_once() is True
    assert db.query_one("SELECT error FROM asr_shadow_runs WHERE id=?", (second["id"],))[
        "error"
    ] == ("原音频完整性校验失败")


def test_qwen_api_exposes_sanitized_runs_and_job_substate_and_retry(tmp_path):
    settings, db, _audio, _current, _service = setup(tmp_path, binary=False)
    db.execute("UPDATE meetings SET source_job_id='job-qwen' WHERE id='vm-qwen'")
    client = TestClient(create_app(settings, Relay()))
    token = client.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": token, "Origin": "http://testserver"}

    created = client.post("/api/meetings/vm-qwen/asr-shadow/qwen", json={}, headers=headers)
    run_id = created.json()["id"]
    detail = client.get("/api/meetings/vm-qwen").json()
    job = client.get("/api/jobs/job-qwen").json()
    retried = client.post(
        f"/api/meetings/vm-qwen/asr-shadow/qwen/{run_id}/retry",
        json={},
        headers=headers,
    )

    assert created.status_code == 202
    assert created.json()["state"] == "unavailable"
    assert detail["asr_shadow_runs"][0]["id"] == run_id
    assert "config_json" not in detail["asr_shadow_runs"][0]
    assert str(settings.qwen_binary) not in json.dumps(detail, ensure_ascii=False)
    assert job["substates"]["qwen"]["status"] == "unavailable"
    assert retried.status_code == 202
    assert retried.json()["id"] != run_id


def test_two_service_instances_atomically_claim_one_run_only(tmp_path, monkeypatch):
    settings, db, _audio, _current, first = setup(tmp_path)
    second = QwenShadowService(db, settings, Relay())
    first.request("vm-qwen")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_run(command, **_kwargs):
        calls.append(command)
        started.set()
        assert release.wait(3)
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n唯一影子稿\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", blocking_run)
    worker = threading.Thread(target=first.run_once)
    worker.start()
    assert started.wait(3)

    assert second.run_once() is False
    release.set()
    worker.join(3)

    assert not worker.is_alive()
    assert len(calls) == 1
    row = db.query_one("SELECT state, owner_id FROM asr_shadow_runs")
    assert row == {"state": "ready", "owner_id": None}


def test_qwen_request_is_idempotent_while_same_meeting_has_active_run(tmp_path):
    _settings, db, _audio, _current, service = setup(tmp_path)

    first = service.request("vm-qwen")
    second = service.request("vm-qwen")

    assert second["id"] == first["id"]
    assert db.query_one("SELECT COUNT(*) AS count FROM asr_shadow_runs")["count"] == 1
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM events WHERE event_type='qwen_shadow_requested'"
        )["count"]
        == 1
    )


def test_qwen_global_worker_lease_limits_two_different_runs_to_one_process(tmp_path, monkeypatch):
    settings, db, _audio, _current, first = setup(tmp_path)
    second_dir = settings.archive_root / "vm-qwen-two"
    second_dir.mkdir()
    second_audio = second_dir / "vm-qwen-two.m4a"
    second_audio.write_bytes(b"second-audio")
    second_stat = second_audio.stat()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-qwen-two', '第二场', 'published')"
    )
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
           VALUES ('vm-qwen-two', 'audio', 'source', 'archive', ?, ?, ?, ?, ?)""",
        (
            str(second_audio),
            hashlib.sha256(second_audio.read_bytes()).hexdigest(),
            second_stat.st_size,
            second_stat.st_mtime_ns,
            utc_now(),
        ),
    )
    second = QwenShadowService(db, settings, Relay())
    first.request("vm-qwen")
    second.request("vm-qwen-two")
    started = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    peak = 0
    calls = 0

    def controlled_run(command, **_kwargs):
        nonlocal active, peak, calls
        with guard:
            active += 1
            peak = max(peak, active)
            calls += 1
        started.set()
        assert release.wait(3)
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n顺序影子稿\n", encoding="utf-8"
        )
        with guard:
            active -= 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", controlled_run)
    workers = [threading.Thread(target=service.run_once) for service in (first, second)]
    for worker in workers:
        worker.start()
    assert started.wait(3)
    time.sleep(0.05)

    assert peak == 1
    lease = db.query_one("SELECT owner_id, run_id FROM runtime_leases WHERE name='qwen_shadow'")
    assert lease is not None

    release.set()
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive()
    assert first.run_once() is True
    assert calls == 2
    assert peak == 1
    assert (
        db.query_one("SELECT COUNT(*) AS count FROM asr_shadow_runs WHERE state='ready'")["count"]
        == 2
    )


def test_qwen_request_and_ready_event_failures_roll_back_data(tmp_path, monkeypatch):
    _settings, db, _audio, _current, service = setup(tmp_path)
    original_add_event = db.add_event

    def fail_requested(event_type, **kwargs):
        if event_type == "qwen_shadow_requested":
            raise sqlite3.OperationalError("event insert failed")
        return original_add_event(event_type, **kwargs)

    monkeypatch.setattr(db, "add_event", fail_requested)
    with pytest.raises(sqlite3.OperationalError):
        service.request("vm-qwen")
    assert db.query_one("SELECT COUNT(*) AS count FROM asr_shadow_runs")["count"] == 0

    monkeypatch.setattr(db, "add_event", original_add_event)
    run = service.request("vm-qwen")

    def successful_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n不得半提交\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fail_ready(event_type, **kwargs):
        if event_type == "qwen_shadow_ready":
            raise sqlite3.OperationalError("ready event insert failed")
        return original_add_event(event_type, **kwargs)

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", successful_run)
    monkeypatch.setattr(db, "add_event", fail_ready)

    assert service.run_once() is True
    assert (
        db.query_one("SELECT state FROM asr_shadow_runs WHERE id=?", (run["id"],))["state"]
        == "failed"
    )
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM transcript_versions WHERE kind='qwen_reference'"
        )["count"]
        == 0
    )


def test_recovery_waits_for_live_lease_and_only_requeues_expired_lease(tmp_path):
    settings, db, _audio, _current, first = setup(tmp_path)
    run = first.request("vm-qwen")
    now = datetime.now(UTC)
    db.execute(
        """UPDATE asr_shadow_runs
              SET state='running', owner_id='old-owner', heartbeat_at=?, lease_expires_at=?
            WHERE id=?""",
        (now.isoformat(), (now + timedelta(minutes=5)).isoformat(), run["id"]),
    )
    restarted = QwenShadowService(db, settings, Relay())

    assert restarted.recover_orphaned() == 0
    db.initialize()
    assert (
        db.query_one("SELECT state FROM asr_shadow_runs WHERE id=?", (run["id"],))["state"]
        == "running"
    )

    db.execute(
        "UPDATE asr_shadow_runs SET lease_expires_at=? WHERE id=?",
        ((now - timedelta(seconds=1)).isoformat(), run["id"]),
    )
    assert restarted.recover_orphaned() == 1
    assert db.query_one(
        "SELECT state, owner_id, lease_expires_at FROM asr_shadow_runs WHERE id=?", (run["id"],)
    ) == {"state": "queued", "owner_id": None, "lease_expires_at": None}


@pytest.mark.parametrize("missing_field", ["owner_id", "heartbeat_at", "lease_expires_at"])
def test_recovery_requeues_incomplete_legacy_running_lease_idempotently(tmp_path, missing_field):
    settings, db, _audio, _current, service = setup(tmp_path)
    run = service.request("vm-qwen")
    now = datetime.now(UTC)
    values = {
        "owner_id": "legacy-owner",
        "heartbeat_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    values[missing_field] = None
    db.execute(
        """UPDATE asr_shadow_runs
              SET state='running', owner_id=?, heartbeat_at=?, lease_expires_at=?
            WHERE id=?""",
        (
            values["owner_id"],
            values["heartbeat_at"],
            values["lease_expires_at"],
            run["id"],
        ),
    )

    assert service.recover_orphaned() == 1
    assert service.recover_orphaned() == 0
    assert db.query_one(
        "SELECT state, owner_id, heartbeat_at, lease_expires_at FROM asr_shadow_runs WHERE id=?",
        (run["id"],),
    ) == {
        "state": "queued",
        "owner_id": None,
        "heartbeat_at": None,
        "lease_expires_at": None,
    }


def test_run_once_periodically_recovers_expired_lease_before_claiming(tmp_path, monkeypatch):
    _settings, db, _audio, _current, service = setup(tmp_path)
    run = service.request("vm-qwen")
    db.execute(
        """UPDATE asr_shadow_runs
              SET state='running', owner_id='dead-owner', heartbeat_at=?, lease_expires_at=?
            WHERE id=?""",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:01+00:00", run["id"]),
    )

    def successful_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n周期恢复成功\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", successful_run)

    assert service.run_once() is True
    assert db.query_one(
        "SELECT state, owner_id, transcript_version_id FROM asr_shadow_runs WHERE id=?",
        (run["id"],),
    ) == {
        "state": "ready",
        "owner_id": None,
        "transcript_version_id": db.query_one(
            "SELECT id FROM transcript_versions WHERE kind='qwen_reference'"
        )["id"],
    }


def test_late_success_from_expired_owner_cannot_pollute_reclaimed_run(tmp_path, monkeypatch):
    settings, db, _audio, _current, first = setup(
        tmp_path, qwen_heartbeat_seconds=0.02, qwen_lease_seconds=0.08
    )
    second = QwenShadowService(db, settings, Relay())
    run = first.request("vm-qwen")
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()

    def stopped_heartbeat(_run_id, stop, _lease_lost):
        stop.wait(3)

    monkeypatch.setattr(first, "_heartbeat_loop", stopped_heartbeat)

    def controlled_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        if first.owner_id in str(output):
            first_started.set()
            assert release_first.wait(3)
            text = "过期所有者A"
        else:
            assert second.owner_id in str(output)
            second_started.set()
            assert release_second.wait(3)
            text = "有效所有者B"
        (output / "meeting.srt").write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", controlled_run)
    first_worker = threading.Thread(target=first.run_once, name="qwen-first-owner")
    first_worker.start()
    assert first_started.wait(3)

    lease_expires_at = db.query_one(
        "SELECT lease_expires_at FROM asr_shadow_runs WHERE id=?", (run["id"],)
    )["lease_expires_at"]
    deadline = time.monotonic() + 3
    while datetime.now(UTC).isoformat() <= lease_expires_at and time.monotonic() < deadline:
        time.sleep(0.01)
    assert datetime.now(UTC).isoformat() > lease_expires_at

    second_worker = threading.Thread(target=second.run_once, name="qwen-second-owner")
    second_worker.start()
    assert second_started.wait(3)

    release_first.set()
    first_worker.join(3)
    assert not first_worker.is_alive()
    assert db.query_one(
        "SELECT state, owner_id, transcript_version_id FROM asr_shadow_runs WHERE id=?",
        (run["id"],),
    ) == {
        "state": "running",
        "owner_id": second.owner_id,
        "transcript_version_id": None,
    }
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM transcript_versions WHERE kind='qwen_reference'"
        )["count"]
        == 0
    )

    release_second.set()
    second_worker.join(3)
    assert not second_worker.is_alive()
    saved = db.query_one(
        "SELECT state, owner_id, transcript_version_id FROM asr_shadow_runs WHERE id=?",
        (run["id"],),
    )
    assert saved["state"] == "ready"
    assert saved["owner_id"] is None
    assert saved["transcript_version_id"] is not None
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM transcript_versions WHERE kind='qwen_reference'"
        )["count"]
        == 1
    )
    assert (
        db.query_one(
            "SELECT text FROM segments WHERE version_id=?", (saved["transcript_version_id"],)
        )["text"]
        == "有效所有者B"
    )


def test_long_qwen_subprocess_refreshes_heartbeat_lease(tmp_path, monkeypatch):
    settings, db, _audio, _current, service = setup(
        tmp_path, qwen_heartbeat_seconds=0.02, qwen_lease_seconds=0.08
    )
    run = service.request("vm-qwen")
    observed = []

    def slow_run(command, **_kwargs):
        first = db.query_one(
            "SELECT heartbeat_at, lease_expires_at FROM asr_shadow_runs WHERE id=?", (run["id"],)
        )
        time.sleep(0.07)
        second = db.query_one(
            "SELECT heartbeat_at, lease_expires_at FROM asr_shadow_runs WHERE id=?", (run["id"],)
        )
        observed.extend([first, second])
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n长任务\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", slow_run)

    assert service.run_once() is True
    assert observed[0]["heartbeat_at"] != observed[1]["heartbeat_at"]
    assert observed[0]["lease_expires_at"] != observed[1]["lease_expires_at"]
