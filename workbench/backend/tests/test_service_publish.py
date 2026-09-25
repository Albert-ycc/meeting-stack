import errno
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import meeting_workbench.service as service_module
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter
from meeting_workbench.main import create_app
from meeting_workbench.service import ConflictError, MeetingService, PublishValidationError

from .helpers import seed_editable_meeting
from .test_jobs_api import FakeRelayClient


def test_copy_required_sources_preserves_exact_attempt_evidence_plan_and_source_srt(tmp_path):
    source = tmp_path / "attempt" / "source.srt"
    source.parent.mkdir()
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\n精确来源\n", encoding="utf-8")
    decoy = source.parent / "decoy.srt"
    decoy.write_text("1\n00:00:00,000 --> 00:00:01,000\n错误来源\n", encoding="utf-8")
    evidence = source.parent / "minutes-evidence.json"
    evidence.write_text('{"schema_version":1}', encoding="utf-8")
    plan = source.parent / "minutes-plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "source_srt_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "publish"
    destination.mkdir()

    MeetingService._copy_required_sources(
        [
            {
                "kind": "minutes_evidence",
                "source_root": "draft",
                "path": str(evidence),
            },
            {"kind": "minutes_plan", "source_root": "draft", "path": str(plan)},
            {"kind": "srt", "source_root": "draft", "path": str(decoy)},
            {"kind": "srt", "source_root": "draft", "path": str(source)},
        ],
        destination,
    )

    assert (destination / "minutes-evidence.json").read_text(encoding="utf-8") == (
        '{"schema_version":1}'
    )
    assert (destination / "minutes-plan.json").is_file()
    assert (destination / "input-transcript.srt").read_bytes() == source.read_bytes()


def test_attempt_provenance_revalidation_fails_closed_on_relay_or_source_change(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            """CREATE TABLE attempts (
                job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                requested_stage TEXT NOT NULL, input_transcript_sha256 TEXT,
                source_srt_sha256 TEXT, minutes_plan_sha256 TEXT,
                minutes_protocol_version INTEGER NOT NULL,
                PRIMARY KEY(job_id, attempt_no))"""
        )
        connection.execute(
            "INSERT INTO attempts VALUES ('job-strict', 1, 'transcribing', NULL, ?, ?, 3)",
            ("a" * 64, "b" * 64),
        )
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    manifest_path = attempt / "workbench-manifest.json"
    manifest_path.write_text('{"job_id":"job-strict","attempt":1}', encoding="utf-8")
    sidecar = attempt / "minutes-plan.json"
    sidecar.write_text("trusted", encoding="utf-8")
    provenance = service_module.AttemptPublishProvenance(
        public_metadata={},
        source_directory=attempt,
        source_manifest_path=manifest_path,
        source_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        files={
            "minutes-plan.json": (
                sidecar,
                hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            )
        },
        relay_attempt={
            "requested_stage": "transcribing",
            "input_transcript_sha256": None,
            "source_srt_sha256": "a" * 64,
            "minutes_plan_sha256": "c" * 64,
            "minutes_protocol_version": 3,
        },
    )
    service = MeetingService(db, relay_jobs_db=relay_db)

    with pytest.raises(PublishValidationError, match="Relay attempt"):
        service._validate_attempt_provenance(provenance)

    sidecar.write_text("changed", encoding="utf-8")
    with pytest.raises(PublishValidationError, match="证据链文件已变化"):
        service._validate_attempt_provenance(provenance)


def test_publish_metadata_preserves_minutes_protocol_version(tmp_path):
    manifest = tmp_path / "workbench-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-protocol",
                "attempt": 3,
                "minutes_protocol_version": 2,
            }
        ),
        encoding="utf-8",
    )

    metadata = MeetingService._minutes_protocol_publish_metadata(
        [
            {
                "kind": "manifest",
                "source_root": "draft",
                "path": str(manifest),
                "mtime_ns": manifest.stat().st_mtime_ns,
            }
        ],
        {"source_job_id": "job-protocol"},
        {"source_job_id": "job-protocol", "source_attempt": 3},
    )

    assert metadata == {"minutes_protocol_version": 2}


def test_edit_split_merge_speaker_and_atomic_publish(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, tmp_path / "archive")
    service = MeetingService(db)
    original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()

    draft_id = service.ensure_draft("vm-20260102-101500")
    service.rename_speaker("vm-20260102-101500", "SPEAKER_00", "张三")
    first_draft_segment = db.query_one(
        "SELECT id FROM segments WHERE version_id = ? ORDER BY ordinal LIMIT 1", (draft_id,)
    )["id"]
    split_ids = service.split_segment("vm-20260102-101500", first_draft_segment, 3)
    service.merge_segments("vm-20260102-101500", *split_ids)
    minutes_id = service.save_minutes("vm-20260102-101500", "# 会议纪要\n\n- 已确认下一步")

    result = service.publish("vm-20260102-101500")

    assert result.transcript_version_id == draft_id
    assert result.minutes_version_id == minutes_id
    assert (meeting_dir / "vm-20260102-101500.srt").exists()
    assert (meeting_dir / "vm-20260102-101500.txt").exists()
    assert (meeting_dir / "spk.txt").exists()
    assert (meeting_dir / "会议纪要.md").exists()
    manifest = json.loads((meeting_dir / "workbench-manifest.json").read_text(encoding="utf-8"))
    assert manifest["meeting_id"] == "vm-20260102-101500"
    assert manifest["status"] == "published"
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == original_hash
    assert (
        db.query_one("SELECT status FROM meetings WHERE id = ?", (manifest["meeting_id"],))[
            "status"
        ]
        == "published"
    )


def test_external_conflict_blocks_publish(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, tmp_path / "archive")
    service = MeetingService(db)
    service.ensure_draft("vm-20260102-101500")
    db.execute("UPDATE meetings SET conflict = 1 WHERE id = ?", ("vm-20260102-101500",))

    with pytest.raises(ConflictError):
        service.publish("vm-20260102-101500")


def test_rollback_copies_historical_transcript_into_new_draft_without_deleting_newer(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    _, _, original = seed_editable_meeting(db, tmp_path / "archive")
    service = MeetingService(db)
    newer = service.ensure_draft("vm-20260102-101500")

    rollback = service.rollback_transcript("vm-20260102-101500", original)

    meeting = db.query_one(
        "SELECT current_transcript_version_id FROM meetings WHERE id = ?", ("vm-20260102-101500",)
    )
    assert meeting["current_transcript_version_id"] == rollback
    assert rollback not in {original, newer}
    assert db.query_one(
        "SELECT kind, based_on_id FROM transcript_versions WHERE id = ?", (rollback,)
    ) == {"kind": "draft", "based_on_id": original}
    assert db.query_one("SELECT id FROM transcript_versions WHERE id = ?", (newer,))


def test_rollback_transcript_recomputes_duration_and_speakers_from_copied_version(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    _, _, original = seed_editable_meeting(db, tmp_path / "archive")
    service = MeetingService(db)
    service.ensure_draft("vm-20260102-101500")
    db.execute(
        "UPDATE meetings SET duration_ms = 99000 WHERE id = ?",
        ("vm-20260102-101500",),
    )
    db.execute("DELETE FROM speakers WHERE meeting_id = ?", ("vm-20260102-101500",))
    db.execute(
        """INSERT INTO speakers(id, meeting_id, label, display_name)
           VALUES ('speaker-stale', ?, 'SPEAKER_99', '旧说话人')""",
        ("vm-20260102-101500",),
    )

    service.rollback_transcript("vm-20260102-101500", original)

    assert db.query_one(
        "SELECT duration_ms FROM meetings WHERE id = ?", ("vm-20260102-101500",)
    ) == {"duration_ms": 5000}
    assert db.query_all(
        "SELECT label, display_name FROM speakers WHERE meeting_id = ? ORDER BY label",
        ("vm-20260102-101500",),
    ) == [
        {"label": "SPEAKER_00", "display_name": None},
        {"label": "SPEAKER_01", "display_name": None},
    ]


def test_unreviewed_draft_promotes_complete_archive_without_mutating_draft(tmp_path):
    archive = tmp_path / "archive"
    draft_root = archive / ".workbench-drafts" / "job-e2e" / "attempt-1"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, draft_root.parent)
    db.execute(
        """UPDATE meetings SET canonical_dir = ?, source_priority = 51,
           recording_date = '2026-01-02T10:15:00+00:00', status = 'completed_unreviewed'
           WHERE id = 'vm-20260102-101500'""",
        (str(meeting_dir),),
    )
    db.execute("UPDATE artifacts SET source_root='draft' WHERE meeting_id='vm-20260102-101500'")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes("vm-20260102-101500", "# 正式纪要")
    draft_hash = hashlib.sha256(audio.read_bytes()).hexdigest()

    result = service.publish("vm-20260102-101500")

    destination = archive / "260102 需求复盘会"
    assert result.archive_dir == str(destination)
    assert destination.is_dir()
    assert (destination / audio.name).exists()
    assert (destination / "vm-20260102-101500.funasr.json").exists()
    assert (destination / "vm-20260102-101500.funasr.log").exists()
    for suffix in ("json", "srt", "txt", "tsv", "vtt"):
        assert (destination / "whisper-ref" / f"vm-20260102-101500.{suffix}").exists()
    assert (destination / "whisper-ref" / "whisper.log").exists()
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == draft_hash
    assert db.query_one(
        "SELECT canonical_dir, source_priority, status FROM meetings WHERE id='vm-20260102-101500'"
    ) == {"canonical_dir": str(destination), "source_priority": 100, "status": "published"}


def test_top_level_unreviewed_directory_publishes_in_place(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, archive)
    original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    db.execute(
        """UPDATE meetings SET source_priority=51, status='completed_unreviewed'
           WHERE id='vm-20260102-101500'"""
    )
    db.execute(
        "UPDATE artifacts SET source_root='draft' WHERE meeting_id='vm-20260102-101500'"
    )
    db.execute(
        "UPDATE meetings SET source_job_id='job-top-level-publish' "
        "WHERE id='vm-20260102-101500'"
    )
    source_manifest = meeting_dir / "workbench-manifest.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-top-level-publish",
                "attempt": 1,
            }
        ),
        encoding="utf-8",
    )
    manifest_stat = source_manifest.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('vm-20260102-101500', 'manifest', 'source', 'draft', ?, ?, ?, CURRENT_TIMESTAMP)""",
        (str(source_manifest), manifest_stat.st_size, manifest_stat.st_mtime_ns),
    )
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft("vm-20260102-101500")
    minutes_id = service.save_minutes(
        "vm-20260102-101500", "# 一级目录正式纪要"
    )
    db.execute(
        "UPDATE minutes_versions SET source_job_id='job-top-level-publish', "
        "source_attempt=1 WHERE id=?",
        (minutes_id,),
    )

    result = service.publish("vm-20260102-101500")

    assert result.archive_dir == str(meeting_dir)
    assert meeting_dir.is_dir()
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == original_hash
    assert json.loads(
        (meeting_dir / "workbench-manifest.json").read_text(encoding="utf-8")
    )["status"] == "published"
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-top-level-publish', 'completed_unreviewed', 1, ?)",
            (str(meeting_dir),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=db.path,
        relay_jobs_db=relay_db,
    )
    importer = ArchiveImporter(db, settings)
    importer.scan()
    edited_transcript = service.ensure_draft("vm-20260102-101500")
    edited_minutes = service.save_minutes(
        "vm-20260102-101500", "# 发布后继续人工编辑"
    )
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "UPDATE jobs SET status='draft_modified' "
            "WHERE job_id='job-top-level-publish'"
        )

    importer.scan()

    assert db.query_one(
        """SELECT canonical_dir, source_priority, status, conflict,
                  current_transcript_version_id, current_minutes_version_id
             FROM meetings WHERE id='vm-20260102-101500'"""
    ) == {
        "canonical_dir": str(meeting_dir),
        "source_priority": 100,
        "status": "draft_modified",
        "conflict": 0,
        "current_transcript_version_id": edited_transcript,
        "current_minutes_version_id": edited_minutes,
    }


def test_top_level_unreviewed_publish_uses_staged_recovery_on_enotsup(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, archive)
    original_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    db.execute(
        "UPDATE meetings SET source_priority=51, status='completed_unreviewed' "
        "WHERE id='vm-20260102-101500'"
    )
    db.execute(
        "UPDATE artifacts SET source_root='draft' "
        "WHERE meeting_id='vm-20260102-101500'"
    )
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes("vm-20260102-101500", "# 一级目录 exFAT 纪要")
    monkeypatch.setattr(
        service_module,
        "atomic_swap_directories",
        lambda _left, _right: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "operation not supported")
        ),
    )

    result = service.publish("vm-20260102-101500")

    assert result.archive_dir == str(meeting_dir)
    assert "一级目录 exFAT 纪要" in (meeting_dir / "会议纪要.md").read_text(
        encoding="utf-8"
    )
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == original_hash
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-backup-*"))


def test_existing_historical_archive_can_publish_without_new_pipeline_artifacts(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, _audio, _ = seed_editable_meeting(db, tmp_path / "archive")
    keep = {"audio", "srt", "txt", "minutes_md", "minutes_html"}
    for artifact in db.query_all(
        "SELECT id, kind, path FROM artifacts WHERE meeting_id='vm-20260102-101500'"
    ):
        if artifact["kind"] in keep:
            continue
        path = Path(artifact["path"])
        path.unlink(missing_ok=True)
        db.execute("DELETE FROM artifacts WHERE id=?", (artifact["id"],))
    service = MeetingService(db, archive_root=tmp_path / "archive")
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes("vm-20260102-101500", "# 历史纪要")

    result = service.publish("vm-20260102-101500")

    assert Path(result.manifest_path).exists()
    assert (meeting_dir / "vm-20260102-101500.srt").exists()


def test_text_only_historical_archive_can_publish_without_inventing_audio(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, tmp_path / "archive")
    audio.unlink()
    db.execute("DELETE FROM artifacts WHERE kind='audio'")
    db.execute("UPDATE meetings SET original_audio_sha256=NULL WHERE id='vm-20260102-101500'")
    service = MeetingService(db, archive_root=tmp_path / "archive")
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes("vm-20260102-101500", "# 无音频历史纪要")

    service.publish("vm-20260102-101500")

    manifest = json.loads((meeting_dir / "workbench-manifest.json").read_text(encoding="utf-8"))
    assert manifest["original_audio"] is None


def test_existing_archive_with_audio_baseline_rejects_missing_original_audio(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, audio, _ = seed_editable_meeting(db, tmp_path / "archive")
    audio.unlink()
    service = MeetingService(db, archive_root=tmp_path / "archive")
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes("vm-20260102-101500", "# 不得绕过原音频")

    with pytest.raises((ConflictError, PublishValidationError), match="音频"):
        service.publish("vm-20260102-101500")

    assert not (meeting_dir / "workbench-manifest.json").exists()
    assert (
        db.query_one("SELECT status FROM meetings WHERE id='vm-20260102-101500'")["status"]
        == "draft_modified"
    )


def test_publish_is_repeatable_and_rescan_does_not_create_self_conflict(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    staging.mkdir()
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, archive)
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=db.path,
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )
    importer = ArchiveImporter(db, settings)
    service = MeetingService(
        db,
        archive_root=archive,
        source_signature_resolver=importer.signature_for_meeting_locked,
    )
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes("vm-20260102-101500", "# 可重复发布")

    service.publish("vm-20260102-101500")
    assert (
        db.query_one("SELECT kind FROM artifacts WHERE path LIKE '%/spk.txt'")["kind"]
        == "speaker_map"
    )
    service.publish("vm-20260102-101500")
    report = importer.scan()

    assert report.conflicts == 0
    assert (
        db.query_one("SELECT conflict FROM meetings WHERE id='vm-20260102-101500'")["conflict"] == 0
    )


def test_existing_archive_publish_falls_back_to_recoverable_staged_install_on_enotsup(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# exFAT staged 发布")

    def unsupported_swap(_left, _right):
        raise OSError(errno.ENOTSUP, "operation not supported")

    monkeypatch.setattr(service_module, "atomic_swap_directories", unsupported_swap)

    result = service.publish(meeting_id)

    assert result.archive_dir == str(formal)
    assert "exFAT staged 发布" in (formal / "会议纪要.md").read_text(encoding="utf-8")
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-backup-*"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_existing_archive_publish_does_not_fallback_for_permission_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    previous = formal / f"{meeting_id}.txt"
    previous.write_text("权限错误前的正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 不应降级")

    def denied_swap(_left, _right):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(service_module, "atomic_swap_directories", denied_swap)

    with pytest.raises(OSError) as caught:
        service.publish(meeting_id)

    assert caught.value.errno == errno.EACCES
    assert previous.read_text(encoding="utf-8") == "权限错误前的正式文本\n"
    assert not list(archive.glob(".*.workbench-backup-*"))


def test_staged_publish_rolls_back_if_old_moved_journal_update_gets_io_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    previous = formal / f"{meeting_id}.txt"
    previous.write_text("staged I/O 错误前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# staged I/O 错误新纪要")
    original_update = service._update_publish_journal

    monkeypatch.setattr(
        service_module,
        "atomic_swap_directories",
        lambda _left, _right: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported")),
    )

    def fail_old_moved_update(journal, **changes):
        if changes.get("phase") == "old_moved":
            raise OSError(errno.EIO, "simulated journal I/O failure")
        original_update(journal, **changes)

    monkeypatch.setattr(service, "_update_publish_journal", fail_old_moved_update)

    with pytest.raises(OSError) as caught:
        service.publish(meeting_id)

    assert caught.value.errno == errno.EIO
    assert previous.read_text(encoding="utf-8") == "staged I/O 错误前正式文本\n"
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-backup-*"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_atomic_swap_rolls_back_if_new_installed_journal_update_gets_io_error(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    previous = formal / f"{meeting_id}.txt"
    previous.write_text("atomic I/O 错误前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# atomic I/O 错误新纪要")
    original_update = service._update_publish_journal

    def fail_new_installed_update(journal, **changes):
        if changes.get("phase") == "new_installed":
            raise OSError(errno.EIO, "simulated journal I/O failure")
        original_update(journal, **changes)

    monkeypatch.setattr(service, "_update_publish_journal", fail_new_installed_update)

    with pytest.raises(OSError) as caught:
        service.publish(meeting_id)

    assert caught.value.errno == errno.EIO
    assert previous.read_text(encoding="utf-8") == "atomic I/O 错误前正式文本\n"
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_schema_v3_prepared_recovery_discards_new_directory_idempotently(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    previous = formal / f"{meeting_id}.txt"
    previous.write_text("prepared 前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# prepared 新纪要")

    def exit_after_prepared(*_args, **_kwargs):
        raise SystemExit(70)

    monkeypatch.setattr(service, "_assert_publish_snapshot_current", exit_after_prepared)
    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    journal = next(archive.glob(".workbench-publish-journal-*.json"))
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["phase"] == "prepared"
    assert payload["strategy"] == "rename_swap"
    assert payload["backup_dir"]

    MeetingService(db, archive_root=archive)
    MeetingService(db, archive_root=archive)

    assert previous.read_text(encoding="utf-8") == "prepared 前正式文本\n"
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_schema_v3_old_moved_recovery_restores_old_directory_idempotently(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    previous = formal / f"{meeting_id}.txt"
    previous.write_text("old_moved 前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# old_moved 新纪要")
    original_update = service._update_publish_journal

    monkeypatch.setattr(
        service_module,
        "atomic_swap_directories",
        lambda _left, _right: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported")),
    )

    def exit_after_old_moved(journal, **changes):
        original_update(journal, **changes)
        if changes.get("phase") == "old_moved":
            raise SystemExit(71)

    monkeypatch.setattr(service, "_update_publish_journal", exit_after_old_moved)
    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    assert not formal.exists()
    assert list(archive.glob(".*.workbench-backup-*"))
    MeetingService(db, archive_root=archive)
    MeetingService(db, archive_root=archive)

    assert previous.read_text(encoding="utf-8") == "old_moved 前正式文本\n"
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-backup-*"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_schema_v3_new_installed_recovery_restores_old_directory_idempotently(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    previous = formal / f"{meeting_id}.txt"
    previous.write_text("new_installed 前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# new_installed 新纪要")
    original_update = service._update_publish_journal

    monkeypatch.setattr(
        service_module,
        "atomic_swap_directories",
        lambda _left, _right: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported")),
    )

    def exit_after_new_installed(journal, **changes):
        original_update(journal, **changes)
        if changes.get("phase") == "new_installed":
            raise SystemExit(72)

    monkeypatch.setattr(service, "_update_publish_journal", exit_after_new_installed)
    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    assert "new_installed 新纪要" in (formal / "会议纪要.md").read_text(encoding="utf-8")
    assert list(archive.glob(".*.workbench-backup-*"))
    MeetingService(db, archive_root=archive)
    MeetingService(db, archive_root=archive)

    assert previous.read_text(encoding="utf-8") == "new_installed 前正式文本\n"
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-backup-*"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_schema_v3_committed_recovery_keeps_new_directory_and_cleans_backup(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# committed staged 新纪要")
    monkeypatch.setattr(
        service_module,
        "atomic_swap_directories",
        lambda _left, _right: (_ for _ in ()).throw(OSError(errno.ENOTSUP, "unsupported")),
    )

    def exit_before_journal_cleanup(_journal):
        raise SystemExit(73)

    monkeypatch.setattr(service, "_remove_publish_journal", exit_before_journal_cleanup)
    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    assert "committed staged 新纪要" in (formal / "会议纪要.md").read_text(encoding="utf-8")
    assert list(archive.glob(".workbench-publish-journal-*.json"))

    MeetingService(db, archive_root=archive)
    MeetingService(db, archive_root=archive)

    assert "committed staged 新纪要" in (formal / "会议纪要.md").read_text(encoding="utf-8")
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-backup-*"))


@pytest.mark.parametrize("schema_version", [1, 2])
def test_startup_keeps_legacy_publish_journal_compatibility(tmp_path, schema_version):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, version_id = seed_editable_meeting(db, archive)
    token = f"{schema_version:032x}"
    work = archive / f".{formal.name}.workbench-publish-{token}"
    work.mkdir()
    (work / "workbench-manifest.json").write_text(
        json.dumps({"schema_version": 1, "publish_token": token}), encoding="utf-8"
    )
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "publish_token": token,
                "meeting_id": "vm-20260102-101500",
                "transcript_version_id": version_id,
                "minutes_version_id": "mv-not-committed",
                "archive_dir": str(formal),
                "work_dir": str(work),
                "promoting": False,
                "expected_artifacts": [],
                "expected_manifest_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    MeetingService(db, archive_root=archive)

    assert formal.is_dir()
    assert not work.exists()
    assert not journal.exists()


def test_staged_recovery_is_idempotent_after_old_directory_was_already_restored(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, version_id = seed_editable_meeting(db, archive)
    token = "a" * 32
    work = archive / f".{formal.name}.workbench-publish-{token}"
    backup = archive / f".{formal.name}.workbench-backup-{token}"
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "publish_token": token,
                "meeting_id": "vm-20260102-101500",
                "transcript_version_id": version_id,
                "minutes_version_id": "mv-not-committed",
                "archive_dir": str(formal),
                "work_dir": str(work),
                "backup_dir": str(backup),
                "promoting": False,
                "strategy": "staged",
                "phase": "new_installed",
                "expected_artifacts": [],
                "expected_manifest_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    MeetingService(db, archive_root=archive)
    MeetingService(db, archive_root=archive)

    assert formal.is_dir()
    assert not journal.exists()
    assert (
        db.query_one("SELECT conflict FROM meetings WHERE id='vm-20260102-101500'")["conflict"] == 0
    )


def test_invalid_schema_v3_strategy_marks_conflict_and_preserves_journal(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, version_id = seed_editable_meeting(db, archive)
    token = "b" * 32
    work = archive / f".{formal.name}.workbench-publish-{token}"
    backup = archive / f".{formal.name}.workbench-backup-{token}"
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "publish_token": token,
                "meeting_id": "vm-20260102-101500",
                "transcript_version_id": version_id,
                "minutes_version_id": "mv-not-committed",
                "archive_dir": str(formal),
                "work_dir": str(work),
                "backup_dir": str(backup),
                "promoting": False,
                "strategy": "unknown",
                "phase": "prepared",
                "expected_artifacts": [],
                "expected_manifest_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    MeetingService(db, archive_root=archive)

    assert journal.exists()
    assert (
        db.query_one("SELECT conflict FROM meetings WHERE id='vm-20260102-101500'")["conflict"] == 1
    )


def test_invalid_schema_v3_fields_mark_conflict_and_preserve_journal(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, version_id = seed_editable_meeting(db, archive)
    token = "f" * 32
    work = archive / f".{formal.name}.workbench-publish-{token}"
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "publish_token": token,
                "meeting_id": "vm-20260102-101500",
                "transcript_version_id": version_id,
                "minutes_version_id": "mv-not-committed",
                "archive_dir": str(formal),
                "work_dir": str(work),
                "promoting": False,
                "strategy": "staged",
                "phase": "prepared",
                "expected_artifacts": [],
                "expected_manifest_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    MeetingService(db, archive_root=archive)

    assert journal.exists()
    assert (
        db.query_one("SELECT conflict FROM meetings WHERE id='vm-20260102-101500'")["conflict"] == 1
    )


def test_deep_corrupt_publish_journal_does_not_crash_service_startup(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    token = "c" * 32
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text("[" * 5000 + "0" + "]" * 5000, encoding="utf-8")

    MeetingService(db, archive_root=archive)

    assert journal.exists()


def test_unreadable_publish_journal_fails_service_startup_closed(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    token = "e" * 32
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text("{}", encoding="utf-8")
    original_load_json_file = service_module.load_json_file

    def deny_journal_read(path):
        if path == journal:
            raise PermissionError(errno.EACCES, "simulated unreadable journal")
        return original_load_json_file(path)

    monkeypatch.setattr(service_module, "load_json_file", deny_journal_read)

    with pytest.raises(PermissionError):
        MeetingService(db, archive_root=archive)


def test_recovery_io_error_marks_conflict_and_fails_closed(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, version_id = seed_editable_meeting(db, archive)
    token = "d" * 32
    work = archive / f".{formal.name}.workbench-publish-{token}"
    backup = archive / f".{formal.name}.workbench-backup-{token}"
    shutil.copytree(formal, work)
    (work / "workbench-manifest.json").write_text(
        json.dumps({"schema_version": 1, "publish_token": token}), encoding="utf-8"
    )
    os.replace(formal, backup)
    journal = archive / f".workbench-publish-journal-{token}.json"
    journal.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "publish_token": token,
                "meeting_id": "vm-20260102-101500",
                "transcript_version_id": version_id,
                "minutes_version_id": "mv-not-committed",
                "archive_dir": str(formal),
                "work_dir": str(work),
                "backup_dir": str(backup),
                "promoting": False,
                "strategy": "staged",
                "phase": "old_moved",
                "expected_artifacts": [],
                "expected_manifest_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    original_replace = service_module.os.replace

    def deny_restore(source, destination):
        if Path(source) == backup and Path(destination) == formal:
            raise PermissionError(errno.EACCES, "simulated recovery denial")
        return original_replace(source, destination)

    monkeypatch.setattr(service_module.os, "replace", deny_restore)

    with pytest.raises(PermissionError):
        MeetingService(db, archive_root=archive)

    assert not formal.exists()
    assert backup.exists() and journal.exists()
    assert (
        db.query_one("SELECT conflict FROM meetings WHERE id='vm-20260102-101500'")["conflict"] == 1
    )


def test_unsafe_meeting_id_cannot_escape_publish_directory(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir = tmp_path / "archive" / "meeting"
    meeting_dir.mkdir(parents=True)
    meeting_id = "../../escaped-by-meeting-id"
    db.execute(
        "INSERT INTO meetings(id, title, status, canonical_dir, source_priority) VALUES (?, 'x', 'draft_modified', ?, 100)",
        (meeting_id, str(meeting_dir)),
    )
    version = db.create_transcript_version(meeting_id, "draft")
    db.replace_segments(
        version,
        meeting_id,
        [{"id": "unsafe-seg", "start_ms": 0, "end_ms": 1, "text": "x"}],
    )
    service = MeetingService(db, archive_root=tmp_path / "archive")
    service.save_minutes(meeting_id, "# x")

    with pytest.raises(PublishValidationError, match="meeting_id 不安全"):
        service.publish(meeting_id)

    assert not (tmp_path / "escaped-by-meeting-id.srt").exists()


def test_minutes_html_is_always_rendered_locally_from_escaped_markdown(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, _audio, _ = seed_editable_meeting(db, tmp_path / "archive")
    service = MeetingService(db, archive_root=tmp_path / "archive")
    service.ensure_draft("vm-20260102-101500")
    service.save_minutes(
        "vm-20260102-101500",
        "# 安全纪要\n\n<script>fetch('https://evil.example')</script>\n<img src=https://evil.example/x>",
    )

    service.publish("vm-20260102-101500")

    rendered = (meeting_dir / "会议纪要.html").read_text(encoding="utf-8")
    assert "<script>" not in rendered
    assert "<img " not in rendered
    assert "default-src 'none'" in rendered


def test_publishing_reprocess_attempt_updates_managed_sources_in_existing_formal_archive(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, formal_audio, _ = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    attempt = archive / ".workbench-drafts" / "job-reprocess" / "attempt-2"
    whisper = attempt / "whisper-ref"
    whisper.mkdir(parents=True)
    draft_sources = {
        "audio": attempt / formal_audio.name,
        "funasr_json": attempt / f"{meeting_id}.funasr.json",
        "funasr_log": attempt / f"{meeting_id}.funasr.log",
        "speaker_map": attempt / "spk.txt",
        "whisper_json": whisper / f"{meeting_id}.json",
        "whisper_txt": whisper / f"{meeting_id}.txt",
        "whisper_srt": whisper / f"{meeting_id}.srt",
        "whisper_tsv": whisper / f"{meeting_id}.tsv",
        "whisper_vtt": whisper / f"{meeting_id}.vtt",
        "whisper_log": whisper / "whisper.log",
    }
    for kind, path in draft_sources.items():
        content = formal_audio.read_bytes() if kind == "audio" else b"new-source"
        if kind == "whisper_json" or kind == "funasr_json":
            content = b"{}"
        path.write_bytes(content)
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
               VALUES (?, ?, 'source', 'draft', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                meeting_id,
                kind,
                str(path),
                hashlib.sha256(content).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
    version = db.create_transcript_version(meeting_id, "funasr")
    db.replace_segments(
        version,
        meeting_id,
        [{"id": "new-segment", "start_ms": 0, "end_ms": 1000, "text": "新转写"}],
    )
    db.execute(
        """UPDATE meetings SET source_job_id='job-reprocess', status='completed_unreviewed'
           WHERE id=?""",
        (meeting_id,),
    )
    manifest_path = attempt / "workbench-manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "job_id": "job-reprocess", "attempt": 2}),
        encoding="utf-8",
    )
    manifest_stat = manifest_path.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES (?, 'manifest', 'source', 'draft', ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            meeting_id,
            str(manifest_path),
            manifest_stat.st_size,
            manifest_stat.st_mtime_ns,
        ),
    )
    service = MeetingService(db, archive_root=archive)
    minutes_id = service.save_minutes(meeting_id, "# 新纪要")
    db.execute(
        """UPDATE minutes_versions SET source_job_id='job-reprocess', source_attempt=2
           WHERE id=?""",
        (minutes_id,),
    )

    service.publish(meeting_id)

    assert (formal / f"{meeting_id}.funasr.log").read_bytes() == b"new-source"
    assert (formal / "whisper-ref" / f"{meeting_id}.txt").read_bytes() == b"new-source"
    published_source = db.query_one(
        "SELECT source_root, sha256 FROM artifacts WHERE path=?",
        (str(formal / f"{meeting_id}.funasr.log"),),
    )
    assert published_source == {
        "source_root": "archive",
        "sha256": hashlib.sha256(b"new-source").hexdigest(),
    }
    assert (
        hashlib.sha256(formal_audio.read_bytes()).hexdigest()
        == db.query_one("SELECT original_audio_sha256 FROM meetings WHERE id=?", (meeting_id,))[
            "original_audio_sha256"
        ]
    )
    assert db.query_one("SELECT kind FROM transcript_versions WHERE id=?", (version,))["kind"] == (
        "published_edit"
    )


def test_publishing_minutes_only_attempt_preserves_existing_engine_sources(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    original_funasr = (formal / f"{meeting_id}.funasr.log").read_bytes()
    original_whisper = (formal / "whisper-ref" / f"{meeting_id}.txt").read_bytes()
    service = MeetingService(db, archive_root=archive)
    draft_id = service.ensure_draft(meeting_id)
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (draft_id,)
    )
    segments[0]["text"] = "发布这版人工修订"
    service.save_segments(meeting_id, segments)
    service.save_minutes(meeting_id, "# 只重生成的纪要")
    snapshot = service.render_current_transcript(meeting_id)
    snapshot_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    attempt = archive / ".workbench-drafts" / "job-minutes" / "attempt-2"
    attempt.mkdir(parents=True)
    input_path = attempt / "input-transcript.txt"
    input_path.write_text(snapshot, encoding="utf-8")
    draft_minutes = attempt / "会议纪要.md"
    draft_minutes.write_text("# 只重生成的纪要", encoding="utf-8")
    draft_html = attempt / "会议纪要.html"
    draft_html.write_text("<h1>只重生成的纪要</h1>", encoding="utf-8")
    manifest_path = attempt / "workbench-manifest.json"
    attempt_files = [input_path, draft_minutes, draft_html]
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 2,
                "meeting_id": meeting_id,
                "job_id": "job-minutes",
                "attempt": 2,
                "status": "completed_unreviewed",
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": snapshot_hash,
                "artifacts": [
                    {
                        "path": path.name,
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in attempt_files
                ],
            }
        ),
        encoding="utf-8",
    )
    for kind, path in {
        "txt": input_path,
        "minutes_md": draft_minutes,
        "minutes_html": draft_html,
        "manifest": manifest_path,
    }.items():
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
               VALUES (?, ?, 'source', 'draft', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                meeting_id,
                kind,
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
    db.execute("UPDATE meetings SET source_job_id='job-minutes' WHERE id=?", (meeting_id,))
    db.execute(
        """UPDATE minutes_versions SET source_job_id='job-minutes', source_attempt=2,
           requested_stage='minutes_generating', input_transcript_sha256=?
           WHERE id=(SELECT current_minutes_version_id FROM meetings WHERE id=?)""",
        (snapshot_hash, meeting_id),
    )

    result = service.publish(meeting_id)

    assert Path(result.manifest_path).is_file()
    assert (formal / f"{meeting_id}.funasr.log").read_bytes() == original_funasr
    assert (formal / "whisper-ref" / f"{meeting_id}.txt").read_bytes() == original_whisper
    assert "发布这版人工修订" in (formal / f"{meeting_id}.txt").read_text(encoding="utf-8")
    published_manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert published_manifest["requested_stage"] == "minutes_generating"
    assert published_manifest["input_transcript_sha256"] == snapshot_hash
    assert published_manifest["minutes_protocol_version"] == 2
    relay_db = tmp_path / "relay-v2.sqlite3"
    sqlite3.connect(relay_db).close()
    settings = Settings(
        data_dir=tmp_path / "data-v2",
        database_path=db.path,
        archive_root=archive,
        staging_root=tmp_path / "staging-v2",
        relay_jobs_db=relay_db,
        semantic_enabled=False,
    )
    with TestClient(create_app(settings, FakeRelayClient())) as client:
        response = client.get(f"/api/meetings/{meeting_id}/minutes-evidence")
    assert response.status_code == 409


def test_minutes_only_v3_publish_preserves_attempt_provenance_through_scan_and_api(tmp_path):
    archive = tmp_path / "archive"
    relay_db = tmp_path / "relay.sqlite3"
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive, relay_jobs_db=relay_db)
    service.ensure_draft(meeting_id)
    markdown = "# 只重生成的纪要\n\n- 形成结论 [00:00:01]\n"
    minutes_id = service.save_minutes(meeting_id, markdown)
    source_srt = service._normalized_transcript_srt(service._current_segments(meeting_id))
    source_sha256 = hashlib.sha256(source_srt.encode("utf-8")).hexdigest()

    attempt_dir = archive / ".workbench-drafts" / "job-minutes-v3" / "attempt-2"
    attempt_dir.mkdir(parents=True)
    source_path = attempt_dir / "input-transcript.srt"
    source_path.write_text(source_srt, encoding="utf-8")
    cue_texts = ["SPEAKER_00：第一段内容", "SPEAKER_01：第二段内容"]
    plan = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "source_srt_sha256": source_sha256,
        "input_transcript_sha256": source_sha256,
        "total_duration_sec": 5,
        "cue_count": 2,
        "windows": [
            {
                "window_id": "W001",
                "start_sec": 0,
                "end_sec": 5,
                "cues": [
                    {
                        "cue_index": 1,
                        "source_start_sec": 1,
                        "source_end_sec": 3,
                        "source_text_sha256": hashlib.sha256(
                            cue_texts[0].encode("utf-8")
                        ).hexdigest(),
                    },
                    {
                        "cue_index": 2,
                        "source_start_sec": 3.2,
                        "source_end_sec": 5,
                        "source_text_sha256": hashlib.sha256(
                            cue_texts[1].encode("utf-8")
                        ).hexdigest(),
                    },
                ],
            }
        ],
    }
    plan_path = attempt_dir / "minutes-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    evidence = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "strategy": "single_pass",
        "coverage": {"total_items": 1, "included_items": 1, "omitted_items": 0},
        "topics": [
            {
                "topic_id": "T01",
                "title": "议题",
                "start_sec": 1,
                "end_sec": 3,
                "items": [
                    {
                        "item_id": "D01",
                        "kind": "decision",
                        "text": "形成结论",
                        "source_start_sec": 1,
                        "source_end_sec": 3,
                        "source_window_id": "W001",
                        "source_text_sha256": plan["windows"][0]["cues"][0][
                            "source_text_sha256"
                        ],
                        "minutes_anchor": "[00:00:01]",
                        "status": "included",
                    }
                ],
            }
        ],
    }
    evidence_path = attempt_dir / "minutes-evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    minutes_path = attempt_dir / "会议纪要.md"
    minutes_path.write_text(markdown, encoding="utf-8")
    html_path = attempt_dir / "会议纪要.html"
    html_path.write_text("<h1>只重生成的纪要</h1>", encoding="utf-8")
    attempt_files = [source_path, plan_path, evidence_path, minutes_path, html_path]
    manifest = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "meeting_id": meeting_id,
        "job_id": "job-minutes-v3",
        "attempt": 2,
        "requested_stage": "minutes_generating",
        "input_transcript_sha256": source_sha256,
        "source_srt_sha256": source_sha256,
        "minutes_plan_sha256": plan_sha256,
        "artifacts": [
            {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in attempt_files
        ],
    }
    manifest_path = attempt_dir / "workbench-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for kind, path in {
        "srt": source_path,
        "minutes_plan": plan_path,
        "minutes_evidence": evidence_path,
        "minutes_md": minutes_path,
        "minutes_html": html_path,
        "manifest": manifest_path,
    }.items():
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, sha256, size_bytes,
                mtime_ns, created_at)
               VALUES (?, ?, 'source', 'draft', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                meeting_id,
                kind,
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
    with sqlite3.connect(relay_db) as connection:
        connection.executescript(
            """
            CREATE TABLE attempts (
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                requested_stage TEXT NOT NULL,
                input_transcript_sha256 TEXT,
                source_srt_sha256 TEXT,
                minutes_plan_sha256 TEXT,
                minutes_protocol_version INTEGER NOT NULL,
                PRIMARY KEY(job_id, attempt_no)
            );
            """
        )
        connection.execute(
            """INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, 3)""",
            (
                "job-minutes-v3",
                2,
                "minutes_generating",
                source_sha256,
                source_sha256,
                plan_sha256,
            ),
        )
    db.execute("UPDATE meetings SET source_job_id='job-minutes-v3' WHERE id=?", (meeting_id,))
    db.execute(
        """UPDATE minutes_versions SET source_job_id='job-minutes-v3', source_attempt=2,
           requested_stage='minutes_generating', input_transcript_sha256=? WHERE id=?""",
        (source_sha256, minutes_id),
    )

    result = service.publish(meeting_id)

    published_manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert (formal / "minutes-plan.json").read_bytes() == plan_path.read_bytes()
    assert (formal / "minutes-evidence.json").read_bytes() == evidence_path.read_bytes()
    assert (formal / "input-transcript.srt").read_bytes() == source_path.read_bytes()
    assert published_manifest["minutes_plan_sha256"] == plan_sha256
    assert published_manifest["source_srt_sha256"] == source_sha256
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=database_path,
        archive_root=archive,
        staging_root=tmp_path / "staging",
        relay_jobs_db=relay_db,
        semantic_enabled=False,
    )
    ArchiveImporter(db, settings).scan()
    with TestClient(create_app(settings, FakeRelayClient())) as client:
        response = client.get(f"/api/meetings/{meeting_id}/minutes-evidence")
    assert response.status_code == 200, response.text
    assert response.json()["coverage"]["included_items"] == 1


@pytest.mark.parametrize("initial_publish", [False, True])
def test_managed_initial_or_retranscribe_v3_publish_preserves_exact_attempt_provenance(
    tmp_path, initial_publish
):
    archive = tmp_path / "archive"
    relay_db = tmp_path / "relay.sqlite3"
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive, relay_jobs_db=relay_db)
    draft_id = service.ensure_draft(meeting_id)
    db.execute(
        "DELETE FROM segments WHERE version_id=? AND ordinal > 0",
        (draft_id,),
    )
    markdown = "# 重转写纪要\n\n- 确认结论 [00:00:01]\n"
    minutes_id = service.save_minutes(meeting_id, markdown)
    source_srt = service._normalized_transcript_srt(service._current_segments(meeting_id))
    source_sha256 = hashlib.sha256(source_srt.encode("utf-8")).hexdigest()
    source_text_sha256 = hashlib.sha256(
        "SPEAKER_00：第一段内容".encode("utf-8")
    ).hexdigest()
    attempt_dir = archive / ".workbench-drafts" / "job-retranscribe-v3" / "attempt-3"
    attempt_dir.mkdir(parents=True)
    source_path = attempt_dir / "input-transcript.srt"
    source_path.write_text(source_srt, encoding="utf-8")
    plan_path = attempt_dir / "minutes-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "source_srt_sha256": source_sha256,
                "input_transcript_sha256": None,
                "total_duration_sec": 3,
                "cue_count": 1,
                "windows": [
                    {
                        "window_id": "W001",
                        "start_sec": 0,
                        "end_sec": 3,
                        "cues": [
                            {
                                "cue_index": 1,
                                "source_start_sec": 1,
                                "source_end_sec": 3,
                                "source_text_sha256": source_text_sha256,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    evidence_path = attempt_dir / "minutes-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "strategy": "single_pass",
                "coverage": {"total_items": 1, "included_items": 1, "omitted_items": 0},
                "topics": [
                    {
                        "topic_id": "T01",
                        "title": "议题",
                        "start_sec": 1,
                        "end_sec": 3,
                        "items": [
                            {
                                "item_id": "D01",
                                "kind": "decision",
                                "text": "确认结论",
                                "source_start_sec": 1,
                                "source_end_sec": 3,
                                "source_window_id": "W001",
                                "source_text_sha256": source_text_sha256,
                                "minutes_anchor": "[00:00:01]",
                                "status": "included",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    minutes_path = attempt_dir / "会议纪要.md"
    minutes_path.write_text(markdown, encoding="utf-8")
    html_path = attempt_dir / "会议纪要.html"
    html_path.write_text("<h1>重转写纪要</h1>", encoding="utf-8")
    indexed = {
        "srt": source_path,
        "minutes_plan": plan_path,
        "minutes_evidence": evidence_path,
        "minutes_md": minutes_path,
        "minutes_html": html_path,
    }
    for row in db.query_all(
        "SELECT kind, path FROM artifacts WHERE meeting_id=? AND source_root='archive'",
        (meeting_id,),
    ):
        source = Path(row["path"])
        target = (
            attempt_dir / "whisper-ref" / source.name
            if row["kind"].startswith("whisper_")
            else attempt_dir / source.name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        indexed[row["kind"]] = target
    attempt_files = list(indexed.values())
    manifest = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "meeting_id": meeting_id,
        "job_id": "job-retranscribe-v3",
        "attempt": 3,
        "requested_stage": "transcribing",
        "input_transcript_sha256": None,
        "source_srt_sha256": source_sha256,
        "minutes_plan_sha256": plan_sha256,
        "artifacts": [
            {
                "path": path.relative_to(attempt_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in attempt_files
        ],
    }
    manifest_path = attempt_dir / "workbench-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    indexed["manifest"] = manifest_path
    for kind, path in indexed.items():
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, sha256, size_bytes,
                mtime_ns, created_at)
               VALUES (?, ?, 'source', 'draft', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                meeting_id,
                kind,
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            """CREATE TABLE attempts (
                job_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                requested_stage TEXT NOT NULL, input_transcript_sha256 TEXT,
                source_srt_sha256 TEXT, minutes_plan_sha256 TEXT,
                minutes_protocol_version INTEGER NOT NULL,
                PRIMARY KEY(job_id, attempt_no))"""
        )
        connection.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, 3)",
            (
                "job-retranscribe-v3",
                3,
                "transcribing",
                None,
                source_sha256,
                plan_sha256,
            ),
        )
    db.execute(
        "UPDATE meetings SET source_job_id='job-retranscribe-v3' WHERE id=?",
        (meeting_id,),
    )
    db.execute(
        """UPDATE minutes_versions SET source_job_id='job-retranscribe-v3',
           source_attempt=3, requested_stage='transcribing', input_transcript_sha256=NULL
           WHERE id=?""",
        (minutes_id,),
    )
    if initial_publish:
        db.execute(
            "DELETE FROM artifacts WHERE meeting_id=? AND source_root='archive'",
            (meeting_id,),
        )
        shutil.rmtree(formal)
        db.execute(
            """UPDATE meetings SET canonical_dir=?, source_priority=51,
               status='completed_unreviewed' WHERE id=?""",
            (str(attempt_dir), meeting_id),
        )

    result = service.publish(meeting_id)

    published_dir = Path(result.archive_dir)
    published_manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert (published_dir / "input-transcript.srt").read_bytes() == source_path.read_bytes()
    assert (published_dir / "minutes-plan.json").read_bytes() == plan_path.read_bytes()
    assert (published_dir / "minutes-evidence.json").read_bytes() == evidence_path.read_bytes()
    assert published_manifest["source_srt_sha256"] == source_sha256
    assert published_manifest["minutes_plan_sha256"] == plan_sha256
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=database_path,
        archive_root=archive,
        staging_root=tmp_path / "staging",
        relay_jobs_db=relay_db,
        semantic_enabled=False,
    )
    ArchiveImporter(db, settings).scan()
    with TestClient(create_app(settings, FakeRelayClient())) as client:
        response = client.get(f"/api/meetings/{meeting_id}/minutes-evidence")
    assert response.status_code == 200, response.text


def test_minutes_only_publish_rejects_transcript_changed_after_generation(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    draft_id = service.ensure_draft(meeting_id)
    snapshot = service.render_current_transcript(meeting_id)
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    service.save_minutes(meeting_id, "# 基于旧快照生成")

    attempt = archive / ".workbench-drafts" / "job-stale-minutes" / "attempt-2"
    attempt.mkdir(parents=True)
    input_path = attempt / "input-transcript.txt"
    input_path.write_text(snapshot, encoding="utf-8")
    md_path = attempt / "会议纪要.md"
    md_path.write_text("# 基于旧快照生成", encoding="utf-8")
    html_path = attempt / "会议纪要.html"
    html_path.write_text("<h1>基于旧快照生成</h1>", encoding="utf-8")
    listed = [input_path, md_path, html_path]
    manifest_path = attempt / "workbench-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-stale-minutes",
                "attempt": 2,
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": digest,
                "artifacts": [
                    {
                        "path": path.name,
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in listed
                ],
            }
        ),
        encoding="utf-8",
    )
    for kind, path in {
        "txt": input_path,
        "minutes_md": md_path,
        "minutes_html": html_path,
        "manifest": manifest_path,
    }.items():
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
               VALUES (?, ?, 'source', 'draft', ?, ?, ?, CURRENT_TIMESTAMP)""",
            (meeting_id, kind, str(path), stat.st_size, stat.st_mtime_ns),
        )
    db.execute("UPDATE meetings SET source_job_id='job-stale-minutes' WHERE id=?", (meeting_id,))
    db.execute(
        """UPDATE minutes_versions SET source_job_id='job-stale-minutes', source_attempt=2,
           requested_stage='minutes_generating', input_transcript_sha256=?
           WHERE id=(SELECT current_minutes_version_id FROM meetings WHERE id=?)""",
        (digest, meeting_id),
    )
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (draft_id,)
    )
    segments[0]["text"] = "生成后又修改了逐字稿"
    service.save_segments(meeting_id, segments)

    with pytest.raises(PublishValidationError, match="请重新生成纪要"):
        service.publish(meeting_id)

    assert not (formal / "workbench-manifest.json").exists()
    assert not list(archive.glob(".*workbench-publish-*"))


def test_publish_rejects_concurrent_save_before_swap_without_diverging_files_and_db(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    old_txt = formal / f"{meeting_id}.txt"
    old_txt.write_text("发布前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    draft_id = service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 并发发布测试")
    manifest_written = threading.Event()
    resume_publish = threading.Event()
    original_atomic_write = service_module.atomic_write_text

    def blocking_atomic_write(path, content):
        original_atomic_write(path, content)
        if path.name == "workbench-manifest.json":
            manifest_written.set()
            assert resume_publish.wait(5)

    monkeypatch.setattr(service_module, "atomic_write_text", blocking_atomic_write)
    errors = []

    def run_publish():
        try:
            service.publish(meeting_id)
        except Exception as error:  # noqa: BLE001 - captured for cross-thread assertion
            errors.append(error)

    thread = threading.Thread(target=run_publish)
    thread.start()
    assert manifest_written.wait(5)
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (draft_id,)
    )
    segments[0]["text"] = "发布过程中保存的新文本"
    concurrent_draft_id = service.save_segments(meeting_id, segments)
    resume_publish.set()
    thread.join(10)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ConflictError)
    assert old_txt.read_text(encoding="utf-8") == "发布前正式文本\n"
    current = db.query_one(
        "SELECT status, current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )
    assert current == {
        "status": "draft_modified",
        "current_transcript_version_id": concurrent_draft_id,
    }
    assert (
        db.query_one("SELECT published FROM transcript_versions WHERE id=?", (draft_id,))[
            "published"
        ]
        == 0
    )
    assert not list(archive.glob(".*workbench-publish-*"))


def test_save_started_after_publish_lock_creates_a_fresh_draft(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    published_version_id = service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 并发保存")
    edited_segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal",
        (published_version_id,),
    )
    published_text = edited_segments[0]["text"]
    edited_segments[0]["text"] = "发布锁之后提交的新编辑"
    publish_swapped = threading.Event()
    resume_publish = threading.Event()
    save_started = threading.Event()
    original_record = service._record_published_artifacts
    errors = []

    def blocking_record(*args, **kwargs):
        publish_swapped.set()
        assert resume_publish.wait(5)
        return original_record(*args, **kwargs)

    monkeypatch.setattr(service, "_record_published_artifacts", blocking_record)

    def run_publish():
        try:
            service.publish(meeting_id)
        except Exception as error:  # noqa: BLE001 - cross-thread assertion
            errors.append(error)

    def run_save():
        save_started.set()
        try:
            service.save_segments(meeting_id, edited_segments)
        except Exception as error:  # noqa: BLE001 - cross-thread assertion
            errors.append(error)

    publish_thread = threading.Thread(target=run_publish)
    publish_thread.start()
    assert publish_swapped.wait(5)
    save_thread = threading.Thread(target=run_save)
    save_thread.start()
    assert save_started.wait(5)
    time.sleep(0.05)
    resume_publish.set()
    publish_thread.join(10)
    save_thread.join(10)

    assert not publish_thread.is_alive() and not save_thread.is_alive()
    assert errors == []
    meeting = db.query_one(
        "SELECT status, current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )
    current = db.query_one(
        "SELECT kind, published, based_on_id FROM transcript_versions WHERE id=?",
        (meeting["current_transcript_version_id"],),
    )
    published = db.query_one(
        "SELECT kind, published FROM transcript_versions WHERE id=?",
        (published_version_id,),
    )
    published_db_text = db.query_one(
        "SELECT text FROM segments WHERE version_id=? ORDER BY ordinal LIMIT 1",
        (published_version_id,),
    )["text"]
    current_text = db.query_one(
        "SELECT text FROM segments WHERE version_id=? ORDER BY ordinal LIMIT 1",
        (meeting["current_transcript_version_id"],),
    )["text"]
    assert meeting["status"] == "draft_modified"
    assert meeting["current_transcript_version_id"] != published_version_id
    assert current == {"kind": "draft", "published": 0, "based_on_id": published_version_id}
    assert published == {"kind": "published_edit", "published": 1}
    assert published_db_text == published_text
    assert current_text == "发布锁之后提交的新编辑"
    assert published_text in (formal / f"{meeting_id}.txt").read_text(encoding="utf-8")


def test_publish_restores_formal_directory_when_database_recording_fails(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    old_txt = formal / f"{meeting_id}.txt"
    old_txt.write_text("数据库失败前的正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    draft_id = service.ensure_draft(meeting_id)
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (draft_id,)
    )
    segments[0]["text"] = "准备发布的新文本"
    saved_draft_id = service.save_segments(meeting_id, segments)
    service.save_minutes(meeting_id, "# 准备发布的新纪要")

    def fail_after_directory_swap(*_args, **_kwargs):
        raise RuntimeError("simulated database write failure")

    monkeypatch.setattr(service, "_record_published_artifacts", fail_after_directory_swap)

    with pytest.raises(RuntimeError, match="simulated database write failure"):
        service.publish(meeting_id)

    assert old_txt.read_text(encoding="utf-8") == "数据库失败前的正式文本\n"
    state = db.query_one(
        "SELECT status, current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )
    assert state == {
        "status": "draft_modified",
        "current_transcript_version_id": saved_draft_id,
    }
    assert (
        db.query_one("SELECT published FROM transcript_versions WHERE id=?", (draft_id,))[
            "published"
        ]
        == 0
    )
    assert not list(archive.glob(".*workbench-publish-*"))


def test_publish_rejects_audio_changed_after_directory_swap_and_restores_original(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    original_audio = audio.read_bytes()
    service = MeetingService(db, archive_root=archive)
    draft_id = service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 文件竞态")
    original_record = service._record_published_artifacts

    def mutate_after_swap(*args, **kwargs):
        result = original_record(*args, **kwargs)
        audio.write_bytes(b"tampered-after-swap")
        return result

    monkeypatch.setattr(service, "_record_published_artifacts", mutate_after_swap)

    with pytest.raises(ConflictError, match="原音频|发布产物"):
        service.publish(meeting_id)

    assert audio.read_bytes() == original_audio
    assert not (formal / "workbench-manifest.json").exists()
    state = db.query_one("SELECT status FROM meetings WHERE id=?", (meeting_id,))["status"]
    assert state == "draft_modified"
    assert (
        db.query_one("SELECT published FROM transcript_versions WHERE id=?", (draft_id,))[
            "published"
        ]
        == 0
    )


def test_publish_marks_conflict_and_keeps_recovery_copy_for_post_commit_change(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 提交窗口校验")
    original_assert = service._assert_published_files_current
    calls = 0

    def mutate_after_commit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            (formal / "会议纪要.md").write_text("# 提交后被篡改\n", encoding="utf-8")
        return original_assert(*args, **kwargs)

    monkeypatch.setattr(service, "_assert_published_files_current", mutate_after_commit)

    with pytest.raises(ConflictError, match="事务已提交"):
        service.publish(meeting_id)

    assert calls == 3
    assert db.query_one("SELECT status, conflict FROM meetings WHERE id=?", (meeting_id,)) == {
        "status": "published",
        "conflict": 1,
    }
    assert list(archive.glob(".workbench-publish-journal-*.json"))
    assert list(archive.glob(".*.workbench-publish-*"))


def test_publish_copy_is_not_a_hard_link(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"immutable")

    service_module._link_or_copy(str(source), str(destination))

    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_ino != source.stat().st_ino


def test_directory_cleanup_ignores_appledouble_entry_that_already_disappeared(
    tmp_path, monkeypatch
):
    directory = tmp_path / "whisper-ref"
    directory.mkdir()

    def simulate_exfat_appledouble_cleanup(path, *, onexc):
        onexc(
            Path.unlink,
            path / "._segment.json",
            FileNotFoundError(2, "No such file", "._segment.json"),
        )
        path.rmdir()

    monkeypatch.setattr(service_module.shutil, "rmtree", simulate_exfat_appledouble_cleanup)

    service_module.remove_directory_tree(directory)

    assert not directory.exists()


def test_startup_recovers_publish_interrupted_after_directory_swap(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    old_txt = formal / f"{meeting_id}.txt"
    old_txt.write_text("中断前正式文本\n", encoding="utf-8")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 中断恢复")

    def simulate_process_exit(*_args, **_kwargs):
        raise SystemExit(77)

    monkeypatch.setattr(service, "_record_published_artifacts", simulate_process_exit)

    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    assert "中断前正式文本" not in old_txt.read_text(encoding="utf-8")
    assert list(archive.glob(".workbench-publish-journal-*.json"))
    assert list(archive.glob(".*.workbench-publish-*"))

    MeetingService(db, archive_root=archive)

    assert old_txt.read_text(encoding="utf-8") == "中断前正式文本\n"
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-publish-*"))
    assert db.query_one("SELECT status FROM meetings WHERE id=?", (meeting_id,))["status"] == (
        "draft_modified"
    )


def test_startup_keeps_committed_publish_when_a_new_draft_was_saved(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 已提交发布")

    def exit_before_journal_cleanup(*_args, **_kwargs):
        raise SystemExit(78)

    monkeypatch.setattr(service, "_remove_publish_journal", exit_before_journal_cleanup)
    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    published_text = (formal / f"{meeting_id}.txt").read_text(encoding="utf-8")
    segments = service._current_segments(meeting_id)
    segments[0]["text"] = "发布之后继续编辑的新草稿"
    new_draft_id = service.save_segments(meeting_id, segments)
    assert db.query_one("SELECT status FROM meetings WHERE id=?", (meeting_id,))["status"] == (
        "draft_modified"
    )

    MeetingService(db, archive_root=archive)

    assert (formal / f"{meeting_id}.txt").read_text(encoding="utf-8") == published_text
    assert db.query_one(
        "SELECT current_transcript_version_id, status FROM meetings WHERE id=?", (meeting_id,)
    ) == {"current_transcript_version_id": new_draft_id, "status": "draft_modified"}
    assert not list(archive.glob(".workbench-publish-journal-*.json"))
    assert not list(archive.glob(".*.workbench-publish-*"))


def test_startup_preserves_recovery_copy_when_committed_files_were_tampered(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 已提交发布")

    def exit_before_recovery_copy_cleanup(*_args, **_kwargs):
        raise SystemExit(79)

    monkeypatch.setattr(service, "_remove_recovery_directory", exit_before_recovery_copy_cleanup)
    with pytest.raises(SystemExit):
        service.publish(meeting_id)

    audio.write_bytes(b"tampered-after-commit")
    MeetingService(db, archive_root=archive)

    assert db.query_one("SELECT status, conflict FROM meetings WHERE id=?", (meeting_id,)) == {
        "status": "published",
        "conflict": 1,
    }
    assert list(archive.glob(".workbench-publish-journal-*.json"))
    assert list(archive.glob(".*.workbench-publish-*"))
    assert (formal / "workbench-manifest.json").exists()


def test_publish_rejects_rolled_back_minutes_from_noncurrent_attempt(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    formal, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    snapshot = service.render_current_transcript(meeting_id)
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def add_attempt(attempt_no, markdown):
        attempt = archive / ".workbench-drafts" / "job-rollback" / f"attempt-{attempt_no}"
        attempt.mkdir(parents=True)
        input_path = attempt / "input-transcript.txt"
        input_path.write_text(snapshot, encoding="utf-8")
        md_path = attempt / "纪要.md"
        md_path.write_text(markdown, encoding="utf-8")
        html_path = attempt / "纪要.html"
        html_path.write_text(f"<h1>{markdown}</h1>", encoding="utf-8")
        listed = [input_path, md_path, html_path]
        manifest_path = attempt / "workbench-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": "job-rollback",
                    "attempt": attempt_no,
                    "requested_stage": "minutes_generating",
                    "input_transcript_sha256": digest,
                    "artifacts": [
                        {
                            "path": path.name,
                            "bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                        for path in listed
                    ],
                }
            ),
            encoding="utf-8",
        )
        for kind, path in {
            "txt": input_path,
            "minutes_md": md_path,
            "minutes_html": html_path,
            "manifest": manifest_path,
        }.items():
            stat = path.stat()
            db.execute(
                """INSERT INTO artifacts
                   (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
                   VALUES (?, ?, 'source', 'draft', ?, ?, ?, CURRENT_TIMESTAMP)""",
                (meeting_id, kind, str(path), stat.st_size, stat.st_mtime_ns),
            )
        minutes_id = service.save_minutes(meeting_id, markdown)
        db.execute(
            """UPDATE minutes_versions SET source_job_id='job-rollback', source_attempt=?,
               requested_stage='minutes_generating', input_transcript_sha256=? WHERE id=?""",
            (attempt_no, digest, minutes_id),
        )
        return minutes_id

    minutes_a = add_attempt(2, "# 纪要 A")
    add_attempt(3, "# 纪要 B")
    db.execute("UPDATE meetings SET source_job_id='job-rollback' WHERE id=?", (meeting_id,))
    service.rollback_minutes(meeting_id, minutes_a)

    with pytest.raises(PublishValidationError, match="attempt"):
        service.publish(meeting_id)

    assert not (formal / "workbench-manifest.json").exists()
