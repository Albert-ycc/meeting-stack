import hashlib
import json
import os

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter
from meeting_workbench.integrity import AudioIntegrityVerifier
from meeting_workbench.parsers import parse_whisper_json
from meeting_workbench.service import ConflictError, MeetingService

from .helpers import seed_editable_meeting


def _settings(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    archive.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )


def _write_explicit_meeting(root, meeting_id, *, transcript="相同", audio=b"same-audio"):
    directory = root / meeting_id
    directory.mkdir(parents=True)
    (directory / f"{meeting_id}.m4a").write_bytes(audio)
    (directory / f"{meeting_id}.srt").write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{transcript}\n", encoding="utf-8"
    )
    return directory


def test_typed_conflicts_are_independent_and_drive_compatibility_cache(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, tmp_path / "archive")
    meeting_id = "vm-20260102-101500"

    external_id = db.conflicts.open(
        meeting_id,
        "external_source_change",
        source_signature="signature-1",
        payload={"changed_resources": ["transcript"]},
    )
    audio_id = db.conflicts.open(
        meeting_id,
        "audio_integrity",
        payload={"reason": "missing"},
    )

    assert db.conflicts.has(meeting_id)
    assert {item["kind"] for item in db.conflicts.list(meeting_id)} == {
        "external_source_change",
        "audio_integrity",
    }
    assert db.query_one("SELECT conflict FROM meetings WHERE id=?", (meeting_id,))["conflict"] == 1

    assert db.conflicts.resolve(audio_id, "verified")
    assert db.conflicts.has(meeting_id, "external_source_change")
    assert db.query_one("SELECT conflict FROM meetings WHERE id=?", (meeting_id,))["conflict"] == 1

    assert db.conflicts.resolve(external_id, "keep_draft")
    assert not db.conflicts.has(meeting_id)
    assert db.query_one("SELECT conflict FROM meetings WHERE id=?", (meeting_id,))["conflict"] == 0


def test_v3_conflict_migration_uses_latest_conflict_event_kind(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, tmp_path / "archive")
    meeting_id = "vm-20260102-101500"
    db.execute("UPDATE meetings SET conflict=1 WHERE id=?", (meeting_id,))
    db.add_event(
        "audio_integrity_conflict",
        meeting_id=meeting_id,
        payload={"reason": "missing", "expected_sha256": "a" * 64},
    )
    db.execute("PRAGMA user_version=3")

    db.initialize()

    rows = db.conflicts.list(meeting_id)
    assert len(rows) == 1
    assert rows[0]["kind"] == "audio_integrity"
    assert rows[0]["payload"]["reason"] == "missing"


def test_distinct_explicit_vm_ids_never_merge_even_with_identical_sources(tmp_path):
    settings = _settings(tmp_path)
    first = "vm-20260701-100000-aaaaaaaa"
    second = "vm-20260701-110000-bbbbbbbb"
    _write_explicit_meeting(settings.archive_root, first)
    _write_explicit_meeting(settings.archive_root, second)
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.errors == 0
    assert {row["id"] for row in db.query_all("SELECT id FROM meetings")} == {first, second}
    assert db.query_one("SELECT COUNT(*) AS count FROM artifacts")["count"] == 4


def test_ambiguous_fingerprint_bundle_does_not_bridge_two_explicit_meetings(tmp_path):
    settings = _settings(tmp_path)
    first = "vm-20260701-100000-aaaaaaaa"
    second = "vm-20260701-110000-bbbbbbbb"
    _write_explicit_meeting(settings.archive_root, first)
    _write_explicit_meeting(settings.archive_root, second)
    ambiguous = settings.staging_root / "unknown-copy"
    ambiguous.mkdir()
    (ambiguous / "recording.m4a").write_bytes(b"same-audio")
    (ambiguous / "recording.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n相同\n", encoding="utf-8"
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.errors == 1
    assert {row["id"] for row in db.query_all("SELECT id FROM meetings")} == {first, second}
    assert (
        db.query_one("SELECT id FROM artifacts WHERE path=?", (str(ambiguous / "recording.m4a"),))
        is None
    )


def test_reassigning_an_artifact_records_identity_audit_event(tmp_path):
    settings = _settings(tmp_path)
    first = "vm-20260701-100000-aaaaaaaa"
    second = "vm-20260701-110000-bbbbbbbb"
    first_dir = _write_explicit_meeting(settings.archive_root, first, transcript="第一场")
    _write_explicit_meeting(settings.archive_root, second, transcript="第二场", audio=b"other")
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    path = str(first_dir / f"{first}.srt")
    db.execute("UPDATE artifacts SET meeting_id=? WHERE path=?", (second, path))

    importer.scan()

    assert (
        db.query_one("SELECT meeting_id FROM artifacts WHERE path=?", (path,))["meeting_id"]
        == first
    )
    event = db.query_one(
        """SELECT payload_json FROM events
           WHERE meeting_id=? AND event_type='artifact_identity_reassigned'
           ORDER BY id DESC LIMIT 1""",
        (first,),
    )
    assert json.loads(event["payload_json"])["previous_meeting_id"] == second


def test_minutes_draft_survives_external_scan_and_external_versions_are_noncurrent(tmp_path):
    settings = _settings(tmp_path)
    meeting_id = "vm-20260701-100000-aaaaaaaa"
    directory = _write_explicit_meeting(settings.archive_root, meeting_id, transcript="原始逐字稿")
    (directory / "会议纪要.md").write_text("# 原始纪要", encoding="utf-8")
    (directory / "会议纪要.html").write_text("<h1>原始纪要</h1>", encoding="utf-8")
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    service = MeetingService(db)
    draft_minutes = service.save_minutes(meeting_id, "# 人工纪要草稿")
    before = db.query_one(
        "SELECT current_transcript_version_id, status FROM meetings WHERE id=?", (meeting_id,)
    )
    (directory / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n外部逐字稿\n", encoding="utf-8"
    )

    report = importer.scan()

    meeting = db.query_one(
        """SELECT current_transcript_version_id, current_minutes_version_id, status, conflict
           FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert report.conflicts == 1
    assert meeting == {
        "current_transcript_version_id": before["current_transcript_version_id"],
        "current_minutes_version_id": draft_minutes,
        "status": "draft_modified",
        "conflict": 1,
    }
    conflict = db.conflicts.list(meeting_id, kind="external_source_change")[0]
    external_id = conflict["payload"]["external_transcript_version_id"]
    assert external_id != meeting["current_transcript_version_id"]
    assert (
        db.query_one("SELECT text FROM segments WHERE version_id=?", (external_id,))["text"]
        == "外部逐字稿"
    )


def test_mixed_whisper_segment_shapes_are_a_recoverable_structure_error(tmp_path):
    broken = tmp_path / "broken.whisper.json"
    broken.write_text(
        json.dumps({"segments": [{"start": 0, "end": 1, "text": "ok"}, None]}),
        encoding="utf-8",
    )

    try:
        parse_whisper_json(broken)
    except ValueError as error:
        assert "segment" in str(error).lower()
    else:
        raise AssertionError("mixed segment list must be rejected")


def test_identical_minutes_provenance_is_not_duplicated_by_mtime_only_rescan(tmp_path):
    settings = _settings(tmp_path)
    meeting_id = "vm-20260701-100000-aaaaaaaa"
    directory = _write_explicit_meeting(settings.archive_root, meeting_id)
    markdown = directory / "会议纪要.md"
    markdown.write_text("# 同一份纪要", encoding="utf-8")
    (directory / "会议纪要.html").write_text("<h1>同一份纪要</h1>", encoding="utf-8")
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    original = markdown.stat()
    os.utime(markdown, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))

    importer.scan()

    versions = db.query_all(
        "SELECT content_sha256 FROM minutes_versions WHERE meeting_id=?", (meeting_id,)
    )
    assert versions == [
        {"content_sha256": hashlib.sha256("# 同一份纪要".encode("utf-8")).hexdigest()}
    ]


def test_rollbacks_copy_history_into_new_drafts(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    _directory, _audio, original_transcript = seed_editable_meeting(db, tmp_path / "archive")
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db)
    newer_transcript = service.ensure_draft(meeting_id)
    first_minutes = service.save_minutes(meeting_id, "# 第一版")
    newer_minutes = service.save_minutes(meeting_id, "# 第二版")

    rollback_transcript = service.rollback_transcript(meeting_id, original_transcript)
    rollback_minutes = service.rollback_minutes(meeting_id, first_minutes)

    assert rollback_transcript not in {original_transcript, newer_transcript}
    assert db.query_one(
        "SELECT kind, based_on_id FROM transcript_versions WHERE id=?", (rollback_transcript,)
    ) == {"kind": "draft", "based_on_id": original_transcript}
    assert rollback_minutes not in {first_minutes, newer_minutes}
    assert db.query_one(
        "SELECT kind, based_on_id, markdown FROM minutes_versions WHERE id=?", (rollback_minutes,)
    ) == {"kind": "draft", "based_on_id": first_minutes, "markdown": "# 第一版"}


def test_audio_recovery_resolves_only_audio_conflict_and_requires_canonical_copy(tmp_path):
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    db.initialize()
    meeting_dir, audio, _version = seed_editable_meeting(db, settings.archive_root)
    meeting_id = "vm-20260102-101500"
    expected = audio.read_bytes()
    fallback = settings.staging_root / "fallback.m4a"
    fallback.write_bytes(expected)
    stat = fallback.stat()
    db.execute("DELETE FROM artifacts WHERE path=?", (str(audio),))
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
           VALUES (?, 'audio', 'source', 'staging', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            meeting_id,
            str(fallback),
            hashlib.sha256(expected).hexdigest(),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )
    audio.unlink()
    verifier = AudioIntegrityVerifier(db, settings)

    missing = verifier.verify()

    assert missing.missing == 1
    assert db.conflicts.has(meeting_id, "audio_integrity")
    db.conflicts.open(
        meeting_id, "external_source_change", payload={"changed_resources": ["transcript"]}
    )
    audio.write_bytes(expected)
    stat = audio.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
           VALUES (?, 'audio', 'source', 'archive', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            meeting_id,
            str(audio),
            hashlib.sha256(expected).hexdigest(),
            stat.st_size,
            stat.st_mtime_ns,
        ),
    )

    recovered = verifier.verify()

    assert recovered.issues == 0
    assert not db.conflicts.has(meeting_id, "audio_integrity")
    assert db.conflicts.has(meeting_id, "external_source_change")
    assert db.query_one("SELECT conflict FROM meetings WHERE id=?", (meeting_id,))["conflict"] == 1


def test_publish_rejects_managed_source_changed_since_scan_and_opens_typed_conflict(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_dir, _audio, _version = seed_editable_meeting(db, archive / ".workbench-drafts")
    meeting_id = "vm-20260102-101500"
    db.execute(
        """UPDATE meetings SET source_priority=51, canonical_dir=? WHERE id=?""",
        (str(meeting_dir), meeting_id),
    )
    db.execute("UPDATE artifacts SET source_root='draft' WHERE meeting_id=?", (meeting_id,))
    source = db.query_one(
        "SELECT id, path FROM artifacts WHERE meeting_id=? AND kind='funasr_json'", (meeting_id,)
    )
    path = os.fspath(source["path"])
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    db.execute("UPDATE artifacts SET sha256=? WHERE id=?", (before, source["id"]))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"changed": true}')
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 待发布")

    with pytest.raises(ConflictError, match="源文件"):
        service.publish(meeting_id)

    conflict = db.conflicts.list(meeting_id, kind="external_source_change")[0]
    assert conflict["payload"]["changed_resources"] == ["managed_sources"]


def test_legacy_text_bundle_keeps_its_single_existing_explicit_owner(tmp_path):
    settings = _settings(tmp_path)
    directory = settings.archive_root / "formal-legacy"
    directory.mkdir()
    transcript = directory / "转写带时间戳.srt"
    text = directory / "转写原文.txt"
    transcript.write_text("1\n00:00:00,000 --> 00:00:01,000\n沿用历史归属\n", encoding="utf-8")
    text.write_text("沿用历史归属\n", encoding="utf-8")
    db = Database(settings.database_path)
    db.initialize()
    meeting_id = "vm-20260101-120000-existing"
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, '既有会议', 'published')",
        (meeting_id,),
    )
    for path, kind in ((transcript, "srt"), (text, "txt")):
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
               VALUES (?, ?, 'source', 'archive', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                meeting_id,
                kind,
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )

    ArchiveImporter(db, settings).scan()

    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 1
    assert {
        row["meeting_id"]
        for row in db.query_all(
            "SELECT meeting_id FROM artifacts WHERE path LIKE ?", (f"{directory}%",)
        )
    } == {meeting_id}


def test_unidentified_audio_bundle_keeps_its_single_existing_legacy_owner(tmp_path):
    settings = _settings(tmp_path)
    directory = settings.archive_root / "old-recording"
    directory.mkdir()
    audio = directory / "old-recording.m4a"
    transcript = directory / "old-recording.srt"
    audio.write_bytes(b"legacy-audio")
    transcript.write_text("1\n00:00:00,000 --> 00:00:01,000\n历史录音\n", encoding="utf-8")
    db = Database(settings.database_path)
    db.initialize()
    meeting_id = "legacy-existing-owner"
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, '历史录音', 'completed_unreviewed')",
        (meeting_id,),
    )
    for path, kind in ((audio, "audio"), (transcript, "srt")):
        stat = path.stat()
        db.execute(
            """INSERT INTO artifacts
               (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
               VALUES (?, ?, 'source', 'archive', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                meeting_id,
                kind,
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )

    ArchiveImporter(db, settings).scan()

    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 1
    assert {
        row["meeting_id"]
        for row in db.query_all(
            "SELECT meeting_id FROM artifacts WHERE path LIKE ?", (f"{directory}%",)
        )
    } == {meeting_id}


def test_minutes_only_external_change_does_not_mark_or_replace_transcript_draft(tmp_path):
    settings = _settings(tmp_path)
    meeting_id = "vm-20260702-100000-aaaaaaaa"
    directory = _write_explicit_meeting(settings.archive_root, meeting_id, transcript="原始逐字稿")
    minutes = directory / "会议纪要.md"
    minutes.write_text("# 原始纪要", encoding="utf-8")
    (directory / "会议纪要.html").write_text("<h1>原始纪要</h1>", encoding="utf-8")
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    service = MeetingService(db)
    transcript_draft = service.ensure_draft(meeting_id)
    minutes_draft = service.save_minutes(meeting_id, "# 人工纪要")
    minutes.write_text("# 外部纪要变更", encoding="utf-8")

    importer.scan()

    conflict = db.conflicts.list(meeting_id, kind="external_source_change")[0]["payload"]
    meeting = db.query_one(
        """SELECT current_transcript_version_id, current_minutes_version_id
           FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert conflict["changed_resources"] == ["minutes"]
    assert conflict["external_transcript_version_id"] is None
    assert isinstance(conflict["external_minutes_version_id"], str)
    assert meeting == {
        "current_transcript_version_id": transcript_draft,
        "current_minutes_version_id": minutes_draft,
    }


def test_transcript_only_external_change_does_not_mark_or_replace_minutes_draft(tmp_path):
    settings = _settings(tmp_path)
    meeting_id = "vm-20260702-100000-aaaaaaaa"
    directory = _write_explicit_meeting(settings.archive_root, meeting_id, transcript="原始逐字稿")
    (directory / "会议纪要.md").write_text("# 原始纪要", encoding="utf-8")
    (directory / "会议纪要.html").write_text("<h1>原始纪要</h1>", encoding="utf-8")
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    service = MeetingService(db)
    transcript_draft = service.ensure_draft(meeting_id)
    minutes_draft = service.save_minutes(meeting_id, "# 人工纪要")
    (directory / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n外部逐字稿变更\n", encoding="utf-8"
    )

    importer.scan()

    conflict = db.conflicts.list(meeting_id, kind="external_source_change")[0]["payload"]
    meeting = db.query_one(
        """SELECT current_transcript_version_id, current_minutes_version_id
           FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert conflict["changed_resources"] == ["transcript"]
    assert isinstance(conflict["external_transcript_version_id"], str)
    assert conflict["external_minutes_version_id"] is None
    assert meeting == {
        "current_transcript_version_id": transcript_draft,
        "current_minutes_version_id": minutes_draft,
    }


def test_published_revision_rejects_tampered_canonical_managed_artifact(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    _meeting_dir, _audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    db.execute("UPDATE meetings SET status='published' WHERE id=?", (meeting_id,))
    source = db.query_one(
        "SELECT id, path FROM artifacts WHERE meeting_id=? AND kind='funasr_json'",
        (meeting_id,),
    )
    source_path = os.fspath(source["path"])
    indexed_sha256 = hashlib.sha256(open(source_path, "rb").read()).hexdigest()
    db.execute("UPDATE artifacts SET sha256=? WHERE id=?", (indexed_sha256, source["id"]))
    with open(source_path, "w", encoding="utf-8") as handle:
        handle.write('{"tampered": true}')
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 正式修订")

    with pytest.raises(ConflictError, match="源文件"):
        service.publish(meeting_id)

    assert db.conflicts.has(meeting_id, "external_source_change")


def test_published_revision_rejects_corrupt_canonical_audio_even_with_staging_fallback(
    tmp_path,
):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    staging.mkdir()
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    _meeting_dir, canonical_audio, _version = seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    expected = canonical_audio.read_bytes()
    db.execute("UPDATE meetings SET status='published' WHERE id=?", (meeting_id,))
    fallback = staging / canonical_audio.name
    fallback.write_bytes(expected)
    fallback_stat = fallback.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
           VALUES (?, 'audio', 'source', 'staging', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            meeting_id,
            str(fallback),
            hashlib.sha256(expected).hexdigest(),
            fallback_stat.st_size,
            fallback_stat.st_mtime_ns,
        ),
    )
    canonical_audio.write_bytes(b"corrupt-canonical-audio")
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    service.save_minutes(meeting_id, "# 正式修订")

    with pytest.raises(ConflictError, match="原音频"):
        service.publish(meeting_id)

    assert db.conflicts.has(meeting_id, "audio_integrity")


def test_each_transcript_save_forks_immutable_draft_and_rejects_reused_base(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, tmp_path / "archive")
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db)
    base_id = service.ensure_draft(meeting_id)
    base_segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (base_id,)
    )
    base_segment_ids = {segment["id"] for segment in base_segments}
    original_text = base_segments[0]["text"]
    edited = [dict(segment) for segment in base_segments]
    edited[0]["text"] = "第一页保存的新内容"

    saved_id = service.save_segments(meeting_id, edited, expected_base_version_id=base_id)

    assert saved_id != base_id
    assert (
        db.query_one(
            "SELECT text FROM segments WHERE version_id=? ORDER BY ordinal LIMIT 1", (base_id,)
        )["text"]
        == original_text
    )
    saved_segments = db.query_all(
        "SELECT id, text FROM segments WHERE version_id=? ORDER BY ordinal", (saved_id,)
    )
    assert {segment["id"] for segment in saved_segments}.isdisjoint(base_segment_ids)
    assert saved_segments[0]["text"] == "第一页保存的新内容"

    with pytest.raises(ConflictError, match="版本已变化"):
        service.save_segments(meeting_id, edited, expected_base_version_id=base_id)

    assert (
        db.query_one(
            "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
        )["current_transcript_version_id"]
        == saved_id
    )


def test_explicit_manifest_identity_ignores_vm_ids_mentioned_only_in_body(tmp_path):
    settings = _settings(tmp_path)
    explicit_id = "vm-20260703-100000-aaaaaaaa"
    mentioned_id = "vm-20260703-110000-bbbbbbbb"
    directory = settings.archive_root / f"{explicit_id} 明确归属"
    directory.mkdir()
    (directory / "recording.m4a").write_bytes(b"identity-audio")
    (directory / "转写原文.md").write_text(
        f"本场会议讨论了历史记录 {mentioned_id}，但不是那场会议。", encoding="utf-8"
    )
    (directory / "会议纪要.md").write_text(
        f"# 当前纪要\n\n关联历史会议 {mentioned_id}", encoding="utf-8"
    )
    (directory / "会议纪要.html").write_text("<h1>当前纪要</h1>", encoding="utf-8")
    (directory / "workbench-manifest.json").write_text(
        json.dumps({"meeting_id": explicit_id}), encoding="utf-8"
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    assert {row["id"] for row in db.query_all("SELECT id FROM meetings")} == {explicit_id}
    artifacts = db.query_all("SELECT meeting_id, path FROM artifacts")
    assert len(artifacts) == 5
    assert {row["meeting_id"] for row in artifacts} == {explicit_id}
    assert any(row["path"].endswith("recording.m4a") for row in artifacts)


def test_publish_recovery_resolves_only_matching_post_commit_token(tmp_path):
    archive = tmp_path / "archive"
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, archive)
    meeting_id = "vm-20260102-101500"
    service = MeetingService(db, archive_root=archive)
    recovered_token = "a" * 32
    unrelated_token = "b" * 32
    post_commit_id = db.conflicts.open(
        meeting_id,
        "publish_post_commit",
        payload={"publish_token": recovered_token},
    )
    recovery_id = db.conflicts.open(
        meeting_id,
        "publish_recovery",
        payload={"publish_token": unrelated_token},
    )
    external_id = db.conflicts.open(
        meeting_id,
        "external_source_change",
        payload={"changed_resources": ["transcript"]},
    )
    journal = archive / f".workbench-publish-journal-{recovered_token}.json"
    journal.write_text("{}", encoding="utf-8")

    service._finish_publish_recovery(
        archive, journal, meeting_id, recovered_token, "committed_cleanup"
    )

    assert not db.conflicts.has(meeting_id, "publish_post_commit")
    assert {
        row["id"]
        for row in db.conflicts.list(meeting_id, status="resolved", kind="publish_post_commit")
    } == {post_commit_id}
    assert {row["id"] for row in db.conflicts.list(meeting_id)} == {
        recovery_id,
        external_id,
    }
