import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter, SourceBundle, artifact_kind
from meeting_workbench.rendering import render_transcript_txt
from meeting_workbench.service import MeetingService


def test_minutes_evidence_has_a_dedicated_artifact_kind():
    assert artifact_kind(Path("/tmp/minutes-evidence.json")) == "minutes_evidence"
    assert artifact_kind(Path("/tmp/minutes-plan.json")) == "minutes_plan"


def write_meeting(root, dirname, *, official, transcript_text):
    meeting_dir = root / dirname
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "vm-20260101-120000.m4a").write_bytes(b"fake-audio")
    (meeting_dir / "vm-20260101-120000.srt").write_text(
        f"1\n00:00:02,000 --> 00:00:04,000\n{transcript_text}\n",
        encoding="utf-8",
    )
    if official:
        (meeting_dir / "workbench-manifest.json").write_text(
            json.dumps({"meeting_id": "vm-20260101-120000"}), encoding="utf-8"
        )
    return meeting_dir


def write_managed_unreviewed_bundle(directory, meeting_id, job_id, *, attempt=1, text="待校对正文"):
    directory.mkdir(parents=True)
    files = {
        f"{meeting_id}.m4a": b"audio",
        f"{meeting_id}.srt": (f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n".encode("utf-8")),
        f"{meeting_id}.txt": text.encode("utf-8"),
        "spk.txt": "SPEAKER_00\t张三".encode(),
        f"{meeting_id}.funasr.json": b"{}",
        f"{meeting_id}.funasr.log": b"ok",
        "会议纪要.md": f"# {text}纪要".encode("utf-8"),
        "会议纪要.html": f"<h1>{text}纪要</h1>".encode("utf-8"),
    }
    for relative, content in files.items():
        (directory / relative).write_bytes(content)
    (directory / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "attempt": attempt,
                "status": "completed_unreviewed",
                "artifacts": [
                    {
                        "path": relative,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    for relative, content in files.items()
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


def write_complete_whisper_reference(directory, meeting_id, *, text="Whisper 对照正文"):
    whisper = directory / "whisper-ref"
    whisper.mkdir(parents=True)
    (whisper / f"{meeting_id}.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "start": 1.0,
                        "end": 2.0,
                        "text": text,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (whisper / f"{meeting_id}.srt").write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n",
        encoding="utf-8",
    )
    (whisper / f"{meeting_id}.txt").write_text(text, encoding="utf-8")
    (whisper / f"{meeting_id}.tsv").write_text(
        f"start\tend\ttext\n1000\t2000\t{text}\n", encoding="utf-8"
    )
    (whisper / f"{meeting_id}.vtt").write_text(
        f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{text}\n", encoding="utf-8"
    )
    (whisper / "whisper.log").write_text("ok\n", encoding="utf-8")
    return whisper


def test_official_archive_wins_but_staging_source_is_traceable(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    write_meeting(staging, "staging-copy", official=False, transcript_text="降级稿内容")
    official_dir = write_meeting(archive, "正式会议", official=True, transcript_text="正式稿内容")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    meeting = db.query_one("SELECT * FROM meetings WHERE id = ?", ("vm-20260101-120000",))
    artifacts = db.query_all(
        "SELECT source_root, path FROM artifacts WHERE meeting_id = ? ORDER BY source_root",
        ("vm-20260101-120000",),
    )
    assert report.meetings_seen == 1
    assert meeting["canonical_dir"] == str(official_dir)
    assert meeting["status"] == "published"
    assert {row["source_root"] for row in artifacts} == {"archive", "staging"}
    assert db.exact_search("正式稿内容")
    assert db.exact_search("降级稿内容") == []


def test_scan_isolates_deep_json_and_still_imports_normal_meeting(tmp_path):
    archive = tmp_path / "archive"
    bad = archive / "bad-json"
    bad.mkdir(parents=True)
    (bad / "vm-20260711-100000-deadbeef.funasr.json").write_text(
        "[" * 5000 + "0" + "]" * 5000,
        encoding="utf-8",
    )
    write_meeting(archive, "正常会议", official=True, transcript_text="正常会议仍应导入")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.errors >= 1
    assert db.exact_search("正常会议仍应导入")


def test_scan_filters_invalid_json_even_when_srt_is_preferred(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = write_meeting(
        archive, "JSON 单源隔离", official=True, transcript_text="SRT 仍可正常导入"
    )
    (meeting_dir / "workbench-manifest.json").write_text(
        "[" * 129 + "0" + "]" * 129,
        encoding="utf-8",
    )
    oversized = meeting_dir / "vm-20260101-120000.funasr.json"
    with oversized.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.errors == 2
    assert db.exact_search("SRT 仍可正常导入")
    assert (
        db.query_one("SELECT COUNT(*) AS count FROM artifacts WHERE path LIKE '%.json'")["count"]
        == 0
    )


def test_scan_isolates_directory_discovery_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    bad = archive / "读取失败"
    bad.mkdir(parents=True)
    write_meeting(archive, "正常会议", official=True, transcript_text="其他目录仍应导入")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    original = importer._files_under

    def fail_one_directory(directory):
        if directory == bad:
            raise OSError("simulated unreadable directory")
        return original(directory)

    monkeypatch.setattr(importer, "_files_under", fail_one_directory)

    report = importer.scan()

    assert report.errors == 1
    assert db.exact_search("其他目录仍应导入")


def test_scan_isolates_directory_stat_permission_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    bad = archive / "stat-失败"
    bad.mkdir(parents=True)
    write_meeting(archive, "正常会议", official=True, transcript_text="目录 stat 失败不应饿死扫描")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    original_is_dir = type(bad).is_dir

    def fail_one_stat(path):
        if path == bad:
            raise PermissionError("simulated stat permission error")
        return original_is_dir(path)

    monkeypatch.setattr(type(bad), "is_dir", fail_one_stat)

    report = importer.scan()

    assert report.errors == 1
    assert db.exact_search("目录 stat 失败不应饿死扫描")


def test_sqlite_failure_still_aborts_scan(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    write_meeting(archive, "正常会议", official=True, transcript_text="数据库错误不能吞")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)

    monkeypatch.setattr(
        importer,
        "_import_meeting",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("fatal")),
    )

    with pytest.raises(sqlite3.OperationalError, match="fatal"):
        importer.scan()


def test_transcript_version_segments_and_current_pointer_are_one_transaction(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    write_meeting(archive, "事务会议", official=True, transcript_text="事务正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    def fail_segment_write(*_args, **_kwargs):
        raise RuntimeError("simulated segment write failure")

    monkeypatch.setattr(
        Database,
        "replace_segments_with_connection",
        staticmethod(fail_segment_write),
    )

    with pytest.raises(RuntimeError, match="segment write failure"):
        ArchiveImporter(db, settings).scan()

    meeting = db.query_one(
        "SELECT current_transcript_version_id FROM meetings WHERE id='vm-20260101-120000'"
    )
    assert meeting and meeting["current_transcript_version_id"] is None
    assert db.query_one("SELECT COUNT(*) AS count FROM transcript_versions")["count"] == 0


def test_fast_path_repairs_current_transcript_version_with_zero_segments(tmp_path):
    archive = tmp_path / "archive"
    write_meeting(archive, "修复会议", official=True, transcript_text="应被重建的正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    meeting_id = "vm-20260101-120000"
    version_id = db.query_one(
        "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )["current_transcript_version_id"]
    db.execute("DELETE FROM segments_fts WHERE version_id=?", (version_id,))
    db.execute("DELETE FROM segments WHERE version_id=?", (version_id,))

    report = importer.scan()

    assert report.errors == 0
    assert (
        db.query_one("SELECT COUNT(*) AS count FROM segments WHERE version_id=?", (version_id,))[
            "count"
        ]
        > 0
    )
    assert db.query_one(
        "SELECT id FROM events WHERE meeting_id=? AND event_type='transcript_version_repaired'",
        (meeting_id,),
    )


def test_manual_title_survives_later_archive_rescan(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    official = write_meeting(archive, "正式目录名", official=True, transcript_text="初始正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    db.execute("UPDATE meetings SET title='人工命名的会议' WHERE id='vm-20260101-120000'")
    db.add_event(
        "meeting_metadata_updated",
        meeting_id="vm-20260101-120000",
        actor="user",
        payload={"fields": ["title"], "title": "人工命名的会议"},
    )
    (official / "vm-20260101-120000.srt").write_text(
        "1\n00:00:02,000 --> 00:00:04,000\n外部更新正文\n", encoding="utf-8"
    )

    importer.scan()

    assert (
        db.query_one("SELECT title FROM meetings WHERE id='vm-20260101-120000'")["title"]
        == "人工命名的会议"
    )


def test_same_size_and_mtime_text_change_imports_a_new_version_without_draft(tmp_path):
    archive = tmp_path / "archive"
    formal = write_meeting(archive, "正式会议", official=True, transcript_text="正式正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    meeting_id = "vm-20260101-120000"
    original_version = db.query_one(
        "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )["current_transcript_version_id"]
    srt = formal / f"{meeting_id}.srt"
    original_stat = srt.stat()
    original_text = srt.read_text(encoding="utf-8")
    changed_text = original_text.replace("正式正文", "外部改写")
    assert len(changed_text.encode("utf-8")) == len(original_text.encode("utf-8"))
    srt.write_text(changed_text, encoding="utf-8")
    os.utime(srt, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    importer.scan()

    current_version = db.query_one(
        "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )["current_transcript_version_id"]
    assert current_version != original_version
    assert db.exact_search("外部改写")
    assert (
        db.query_one("SELECT sha256 FROM artifacts WHERE path=?", (str(srt),))["sha256"]
        == hashlib.sha256(srt.read_bytes()).hexdigest()
    )

    MeetingService(db, archive_root=archive).ensure_draft(meeting_id)
    second = importer.scan()

    assert second.conflicts == 0
    assert db.query_one("SELECT conflict FROM meetings WHERE id=?", (meeting_id,))["conflict"] == 0


def test_minutes_only_attempt_does_not_hide_external_change_while_draft_exists(tmp_path):
    archive = tmp_path / "archive"
    formal = write_meeting(archive, "正式会议", official=True, transcript_text="正式正文")
    relay_db = tmp_path / "relay.sqlite3"
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    meeting_id = "vm-20260101-120000"
    service = MeetingService(db, archive_root=archive)
    service.ensure_draft(meeting_id)
    snapshot = service.render_current_transcript(meeting_id)
    snapshot_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    attempt = archive / ".workbench-drafts" / "job-conflict-minutes" / "attempt-2"
    attempt.mkdir(parents=True)
    files = {
        f"{meeting_id}.m4a": b"fake-audio",
        "input-transcript.txt": snapshot.encode("utf-8"),
        "会议纪要.md": b"# generated",
        "会议纪要.html": b"<h1>generated</h1>",
    }
    for relative, content in files.items():
        (attempt / relative).write_bytes(content)
    (attempt / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "meeting_id": meeting_id,
                "job_id": "job-conflict-minutes",
                "attempt": 2,
                "status": "completed_unreviewed",
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": snapshot_hash,
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
            "INSERT INTO jobs VALUES ('job-conflict-minutes', 'completed_unreviewed', 2, ?)",
            (str(attempt),),
        )
    srt = formal / f"{meeting_id}.srt"
    original_stat = srt.stat()
    original_text = srt.read_text(encoding="utf-8")
    changed_text = original_text.replace("正式正文", "外部改写")
    assert len(changed_text.encode("utf-8")) == len(original_text.encode("utf-8"))
    srt.write_text(changed_text, encoding="utf-8")
    os.utime(srt, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    first = importer.scan()
    second = importer.scan()

    meeting = db.query_one(
        "SELECT conflict, current_minutes_version_id FROM meetings WHERE id=?", (meeting_id,)
    )
    current_kind = db.query_one(
        """SELECT tv.kind FROM transcript_versions tv JOIN meetings m
           ON m.current_transcript_version_id=tv.id WHERE m.id=?""",
        (meeting_id,),
    )["kind"]
    assert first.conflicts == 1
    assert second.conflicts == 0
    assert meeting == {"conflict": 1, "current_minutes_version_id": None}
    assert current_kind == "draft"


def test_external_archive_disconnect_does_not_downgrade_existing_formal_meeting(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    write_meeting(staging, "staging-copy", official=False, transcript_text="中转正文")
    official = write_meeting(archive, "正式会议", official=True, transcript_text="正式正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    disconnected = tmp_path / "archive-disconnected"
    archive.rename(disconnected)
    (staging / "staging-copy" / "vm-20260101-120000.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n掉盘后的中转改动\n", encoding="utf-8"
    )

    importer.scan()

    meeting = db.query_one(
        "SELECT canonical_dir, source_priority, status FROM meetings WHERE id='vm-20260101-120000'"
    )
    current = db.query_one(
        """SELECT s.text FROM segments s JOIN meetings m
           ON m.current_transcript_version_id=s.version_id
           WHERE m.id='vm-20260101-120000' LIMIT 1"""
    )["text"]
    assert meeting["canonical_dir"] == str(official)
    assert meeting["source_priority"] == 100
    assert meeting["status"] == "published"
    assert current == "正式正文"


def test_new_manifest_can_keep_formal_directory_unreviewed(tmp_path):
    archive = tmp_path / "archive"
    directory = write_meeting(archive, "新流水线", official=True, transcript_text="自动稿")
    (directory / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "meeting_id": "vm-20260101-120000",
                "job_id": "job-manifest",
                "status": "completed_unreviewed",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    meeting = db.query_one("SELECT status, source_job_id FROM meetings")
    assert meeting == {"status": "completed_unreviewed", "source_job_id": "job-manifest"}


def test_whisper_reference_is_a_separate_noncurrent_transcript_version(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260706-120000-ABCDEF12"
    directory = archive / "260706 双引擎"
    whisper = directory / "whisper-ref"
    whisper.mkdir(parents=True)
    (directory / f"{meeting_id}.m4a").write_bytes(b"audio")
    (directory / f"{meeting_id}.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nFunASR 主稿\n", encoding="utf-8"
    )
    (whisper / f"{meeting_id}.srt").write_text(
        "1\n00:00:01,100 --> 00:00:02,100\nWhisper 对照稿\n", encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)

    importer.scan()
    importer.scan()

    meeting = db.query_one("SELECT current_transcript_version_id FROM meetings")
    versions = db.query_all(
        "SELECT id, kind, source_path FROM transcript_versions ORDER BY version_no"
    )
    assert len(versions) == 2
    assert versions[0]["kind"] == "funasr"
    assert versions[0]["id"] == meeting["current_transcript_version_id"]
    assert versions[1]["kind"] == "whisper_reference"
    reference = db.query_one(
        "SELECT text, start_ms FROM segments WHERE version_id = ?", (versions[1]["id"],)
    )
    assert reference == {"text": "Whisper 对照稿", "start_ms": 1100}


def test_appledouble_files_are_ignored(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = archive / "会议"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "._vm-20260101-120000.m4a").write_bytes(b"noise")
    (meeting_dir / ".DS_Store").write_bytes(b"noise")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.meetings_seen == 0
    assert db.query_all("SELECT * FROM artifacts") == []


def test_two_vm_recordings_in_one_official_directory_stay_separate(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = archive / "260702 同日两场沟通"
    meeting_dir.mkdir(parents=True)
    for meeting_id, transcript in (
        ("vm-20260702-100000-AAAAAAAA", "上午会议"),
        ("vm-20260702-150000-BBBBBBBB", "下午会议"),
    ):
        (meeting_dir / f"{meeting_id}.m4a").write_bytes(meeting_id.encode())
        (meeting_dir / f"{meeting_id}.srt").write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\n{transcript}\n", encoding="utf-8"
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.meetings_seen == 2
    assert {row["id"] for row in db.query_all("SELECT id FROM meetings")} == {
        "vm-20260702-100000-aaaaaaaa",
        "vm-20260702-150000-bbbbbbbb",
    }
    assert db.exact_search("上午会议")[0]["meeting_id"].endswith("aaaaaaaa")
    assert db.exact_search("下午会议")[0]["meeting_id"].endswith("bbbbbbbb")


def test_support_directories_do_not_become_official_meetings(tmp_path):
    archive = tmp_path / "archive"
    (archive / ".obsidian").mkdir(parents=True)
    (archive / ".obsidian" / "config.json").write_text("{}", encoding="utf-8")
    (archive / "funasr-poc-260708").mkdir(parents=True)
    (archive / "funasr-poc-260708" / "sample.m4a").write_bytes(b"poc")
    history = archive / "meeting-relay-历史产物" / "vm-20260701-111111-CCCCCCCC"
    history.mkdir(parents=True)
    (history / "vm-20260701-111111-CCCCCCCC.m4a").write_bytes(b"history")
    (history / "vm-20260701-111111-CCCCCCCC.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n历史稿\n", encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    rows = db.query_all("SELECT id, source_priority FROM meetings")
    assert rows == [{"id": "vm-20260701-111111-cccccccc", "source_priority": 1}]
    artifact = db.query_one(
        "SELECT source_root FROM artifacts WHERE meeting_id = ? LIMIT 1", (rows[0]["id"],)
    )
    assert artifact["source_root"] == "history"


def test_prompt_txt_is_never_selected_as_transcript(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = archive / "260703 Prompt 隔离"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "vm-20260703-120000-DDDDDDDD.m4a").write_bytes(b"audio")
    (meeting_dir / "prompt.txt").write_text("不要把提示词放进逐字稿", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    assert db.exact_search("提示词") == []


def test_minutes_are_imported_and_html_is_sanitized(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = archive / "260704 纪要导入"
    meeting_dir.mkdir(parents=True)
    meeting_id = "vm-20260704-120000-EEEEEEEE"
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n逐字稿\n", encoding="utf-8"
    )
    (meeting_dir / "会议纪要.md").write_text("# 正式纪要\n\n- 决策", encoding="utf-8")
    (meeting_dir / "会议纪要.html").write_text(
        '<h1 onclick="bad()">正式纪要</h1><script>alert(1)</script><img src="https://cdn.example/a.png">',
        encoding="utf-8",
    )
    (meeting_dir / "转写原文.md").write_text("这不是纪要", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    meeting = db.query_one(
        "SELECT current_minutes_version_id FROM meetings WHERE id = ?", (meeting_id.lower(),)
    )
    minutes = db.query_one(
        "SELECT * FROM minutes_versions WHERE id = ?", (meeting["current_minutes_version_id"],)
    )
    assert "正式纪要" in minutes["markdown"]
    assert "这不是纪要" not in minutes["markdown"]
    assert "script" not in minutes["html"].lower()
    assert "onclick" not in minutes["html"].lower()
    assert "https://" not in minutes["html"].lower()


def test_topic_named_markdown_and_html_pair_is_imported_as_minutes(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = archive / "260709 产品二期需求评审"
    meeting_dir.mkdir(parents=True)
    meeting_id = "vm-20260709-120000-1212ABAB"
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n逐字稿正文\n", encoding="utf-8"
    )
    (meeting_dir / "产品二期需求评审.md").write_text(
        "# 产品二期需求评审\n\n- 已确认二期排期", encoding="utf-8"
    )
    (meeting_dir / "产品二期需求评审.html").write_text(
        '<h1 onclick="bad()">产品二期需求评审</h1>'
        "<script>alert(1)</script><p>已确认二期排期</p>",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    meeting = db.query_one(
        "SELECT current_minutes_version_id FROM meetings WHERE id = ?", (meeting_id.lower(),)
    )
    minutes = db.query_one(
        "SELECT markdown, html FROM minutes_versions WHERE id = ?",
        (meeting["current_minutes_version_id"],),
    )
    assert "已确认二期排期" in minutes["markdown"]
    assert "script" not in minutes["html"].lower()
    assert "onclick" not in minutes["html"].lower()


def test_draft_title_uses_topic_named_minutes_heading(tmp_path):
    draft = tmp_path / "attempt-1"
    draft.mkdir()
    markdown = draft / "工作台端到端验收测试.md"
    html = draft / "工作台端到端验收测试.html"
    markdown.write_text("# 工作台端到端验收测试\n", encoding="utf-8")
    html.write_text("<h1>工作台端到端验收测试</h1>", encoding="utf-8")
    bundle = SourceBundle("vm-20260711-013605-b0b322f3", draft, "draft", 51, [markdown, html])

    title = ArchiveImporter._display_title(bundle, bundle.meeting_id)

    assert title == "工作台端到端验收测试"


def test_minutes_heading_suffix_does_not_leave_a_dangling_separator(tmp_path):
    draft = tmp_path / "attempt-1"
    draft.mkdir()
    markdown = draft / "麻醉MDT病例分享会需求沟通 · 会议纪要.md"
    html = draft / "麻醉MDT病例分享会需求沟通 · 会议纪要.html"
    markdown.write_text("# 麻醉MDT病例分享会需求沟通 · 会议纪要\n", encoding="utf-8")
    html.write_text("<h1>麻醉MDT病例分享会需求沟通 · 会议纪要</h1>", encoding="utf-8")
    bundle = SourceBundle("vm-20260806-024252-41ea3066", draft, "draft", 51, [markdown, html])

    title = ArchiveImporter._display_title(bundle, bundle.meeting_id)

    assert title == "麻醉MDT病例分享会需求沟通"


def test_missing_minutes_fall_back_to_a_dated_placeholder_title(tmp_path):
    draft = tmp_path / "attempt-1"
    draft.mkdir()
    srt = draft / "vm-20260715-214003-1438D4B5.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n开场\n", encoding="utf-8")
    bundle = SourceBundle("vm-20260715-214003-1438d4b5", draft, "draft", 51, [srt])

    title = ArchiveImporter._display_title(bundle, bundle.meeting_id)

    assert title == "260715 未命名录音"
    assert ArchiveImporter.is_untitled(title, bundle.meeting_id)


def test_staging_directory_named_after_the_job_is_not_used_as_a_title(tmp_path):
    meeting_id = "vm-20260721-185009-97460a4d"
    staging = tmp_path / "vm-20260721-185009-97460A4D"
    staging.mkdir()
    audio = staging / f"{meeting_id}.m4a"
    audio.write_bytes(b"audio")
    bundle = SourceBundle(meeting_id, staging, "staging", 10, [audio])

    title = ArchiveImporter._display_title(bundle, meeting_id)

    assert title == "260721 未命名录音"


def test_real_meeting_titles_are_never_treated_as_placeholders():
    assert not ArchiveImporter.is_untitled("协会MDT需求评审与估时", "vm-20260728-190347-23c29b65")


def test_non_minutes_document_pairs_are_not_imported_as_minutes(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = archive / "260710 文档排除"
    meeting_dir.mkdir(parents=True)
    meeting_id = "vm-20260710-120000-3434CDCD"
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n会议正文\n", encoding="utf-8"
    )
    for stem in ("转写原文", "项目逐字稿", "prompt", "客户合同法务审阅"):
        (meeting_dir / f"{stem}.md").write_text(f"# {stem}", encoding="utf-8")
        (meeting_dir / f"{stem}.html").write_text(f"<h1>{stem}</h1>", encoding="utf-8")
    (meeting_dir / "单独的方案说明.md").write_text("# 方案说明", encoding="utf-8")
    (meeting_dir / "单独的评审结论.html").write_text("<h1>评审结论</h1>", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    meeting = db.query_one(
        "SELECT current_minutes_version_id FROM meetings WHERE id = ?", (meeting_id.lower(),)
    )
    assert meeting["current_minutes_version_id"] is None
    assert db.query_one("SELECT COUNT(*) AS count FROM minutes_versions")["count"] == 0


def test_rescan_is_idempotent_and_external_change_conflicts_with_draft(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = write_meeting(archive, "正式会议", official=True, transcript_text="原始正文")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)

    importer.scan()
    importer.scan()
    assert db.query_one("SELECT COUNT(*) AS count FROM transcript_versions")["count"] == 1
    MeetingService(db).ensure_draft("vm-20260101-120000")
    (meeting_dir / "vm-20260101-120000.srt").write_text(
        "1\n00:00:02,000 --> 00:00:04,000\n外部修订正文\n", encoding="utf-8"
    )

    report = importer.scan()

    assert report.conflicts == 1
    assert (
        db.query_one("SELECT conflict FROM meetings WHERE id='vm-20260101-120000'")["conflict"] == 1
    )
    assert db.exact_search("外部修订正文") == []


def test_transcript_markdown_is_searchable_but_not_misclassified_as_minutes(tmp_path):
    archive = tmp_path / "archive"
    directory = archive / "260705 Markdown 原文"
    directory.mkdir(parents=True)
    (directory / "vm-20260705-120000-FFFFFFFF.m4a").write_bytes(b"audio")
    (directory / "转写原文.md").write_text("这是一段 Markdown 逐字稿", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    assert db.exact_search("Markdown 逐字稿")
    meeting = db.query_one("SELECT current_minutes_version_id FROM meetings")
    assert meeting["current_minutes_version_id"] is None


def test_completed_workbench_draft_is_imported_as_unreviewed_with_job_link(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260708-120000-1234ABCD"
    attempt = archive / ".workbench-drafts" / "job-draft" / "attempt-2"
    attempt.mkdir(parents=True)
    (attempt / f"{meeting_id}.m4a").write_bytes(b"audio")
    (attempt / f"{meeting_id}.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n待校对正文\n", encoding="utf-8"
    )
    (attempt / f"{meeting_id}.txt").write_text("待校对正文", encoding="utf-8")
    (attempt / "spk.txt").write_text("SPEAKER_00\t张三", encoding="utf-8")
    (attempt / f"{meeting_id}.funasr.json").write_text("{}", encoding="utf-8")
    (attempt / f"{meeting_id}.funasr.log").write_text("ok", encoding="utf-8")
    (attempt / "会议纪要.md").write_text("# 待校对纪要", encoding="utf-8")
    (attempt / "会议纪要.html").write_text("<h1>待校对纪要</h1>", encoding="utf-8")
    listed = [path for path in attempt.rglob("*") if path.is_file()]
    (attempt / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-draft",
                "attempt": 2,
                "artifacts": [
                    {
                        "path": str(path.relative_to(attempt)),
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in listed
                ],
            }
        ),
        encoding="utf-8",
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-draft', 'completed_unreviewed', 2, ?)",
            (str(attempt),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    meeting = db.query_one(
        "SELECT title, status, source_priority, source_job_id, canonical_dir, current_minutes_version_id FROM meetings"
    )
    assert meeting["title"] == "待校对"
    assert meeting["status"] == "completed_unreviewed"
    assert meeting["source_priority"] == 52
    assert meeting["source_job_id"] == "job-draft"
    assert meeting["canonical_dir"] == str(attempt)
    assert meeting["current_minutes_version_id"]


def test_pending_review_children_are_separate_managed_unreviewed_meetings(tmp_path):
    archive = tmp_path / "archive"
    pending_root = archive / "待校对"
    first_id = "vm-20260712-181605-0b445e55"
    second_id = "vm-20260712-215202-23ca270d"
    first = write_managed_unreviewed_bundle(
        pending_root / "260712 第一场", first_id, "job-first", text="第一场正文"
    )
    second = write_managed_unreviewed_bundle(
        pending_root / "260712 第二场", second_id, "job-second", text="第二场正文"
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.executemany(
            "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?)",
            [("job-first", str(first)), ("job-second", str(second))],
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)

    importer.scan()
    importer.scan()

    meetings = db.query_all(
        "SELECT id, status, canonical_dir, source_priority FROM meetings ORDER BY id"
    )
    assert meetings == [
        {
            "id": first_id,
            "status": "completed_unreviewed",
            "canonical_dir": str(first),
            "source_priority": 51,
        },
        {
            "id": second_id,
            "status": "completed_unreviewed",
            "canonical_dir": str(second),
            "source_priority": 51,
        },
    ]
    assert {
        row["source_root"] for row in db.query_all("SELECT DISTINCT source_root FROM artifacts")
    } == {"draft"}
    assert db.query_one("SELECT COUNT(*) AS count FROM transcript_versions")["count"] == 2
    assert db.exact_search("第一场正文")[0]["meeting_id"] == first_id
    assert db.exact_search("第二场正文")[0]["meeting_id"] == second_id


def test_top_level_managed_directory_remains_unreviewed_draft_source(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260712-181605-0b445e55"
    directory = write_managed_unreviewed_bundle(
        archive / "260712 一级目录会议",
        meeting_id,
        "job-top-level",
        text="一级目录未校对正文",
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-top-level', 'completed_unreviewed', 1, ?)",
            (str(directory),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    assert db.query_one(
        "SELECT status, canonical_dir, source_priority FROM meetings WHERE id=?",
        (meeting_id,),
    ) == {
        "status": "completed_unreviewed",
        "canonical_dir": str(directory),
        "source_priority": 51,
    }
    assert db.query_one(
        "SELECT DISTINCT source_root FROM artifacts WHERE meeting_id=?",
        (meeting_id,),
    ) == {"source_root": "draft"}


def test_managed_directory_survives_stale_whisper_reference_hash(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260803-200807-2baec05c"
    directory = write_managed_unreviewed_bundle(
        archive / "260803 长录音会议",
        meeting_id,
        "job-whisper-async",
        text="长录音未校对正文",
    )
    whisper = write_complete_whisper_reference(directory, meeting_id)
    manifest_path = directory / "workbench-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].append(
        {
            "path": "whisper-ref/whisper.log",
            "bytes": 3,
            "sha256": hashlib.sha256(b"old").hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # Whisper 是异步子状态：manifest 落盘后它还在追加日志，快照哈希从此永远对不上。
    # 这不该把整个归档目录判成无效 manifest，否则标题会一直停在占位名上。
    (whisper / "whisper.log").write_text("ok\n追加于 manifest 之后\n", encoding="utf-8")
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-whisper-async', 'completed_unreviewed', 1, ?)",
            (str(directory),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    meeting = db.query_one(
        "SELECT title, canonical_dir, source_priority FROM meetings WHERE id=?",
        (meeting_id,),
    )
    assert report.errors == 0
    assert meeting["canonical_dir"] == str(directory)
    assert meeting["source_priority"] == 51
    assert not ArchiveImporter.is_untitled(meeting["title"], meeting_id)


def test_top_level_managed_directory_with_stale_relay_attempt_is_isolated(tmp_path):
    archive = tmp_path / "archive"
    directory = write_managed_unreviewed_bundle(
        archive / "260712 一级旧 attempt",
        "vm-20260712-181605-0b445e59",
        "job-stale-top-level",
        attempt=1,
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-stale-top-level', 'completed_unreviewed', 2, ?)",
            (str(directory),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    # 隔离≠扫描出错：目录照样进不来，但不能把 errors 顶起来去阻断全库清理。
    assert report.errors == 0
    assert report.quarantined == 1
    assert report.quarantine_details[0]["directory"] == str(directory)
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 0


def _seed_top_level_managed_meeting(tmp_path, *, meeting_id, job_id):
    archive = tmp_path / "archive"
    directory = write_managed_unreviewed_bundle(archive / f"{job_id} 目录", meeting_id, job_id)
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?)",
            (job_id, str(directory)),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    first = importer.scan()
    assert first.quarantined == 0
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 1
    return db, importer, directory


def test_already_imported_managed_directory_tolerates_extra_untracked_file(tmp_path):
    meeting_id = "vm-20260904-090000-aa112233"
    db, importer, directory = _seed_top_level_managed_meeting(
        tmp_path, meeting_id=meeting_id, job_id="job-extra-file"
    )

    note = directory / "我的笔记.md"
    note.write_text("旁置笔记", encoding="utf-8")
    report = importer.scan()

    assert report.quarantined == 0
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 1
    artifact = db.query_one(
        "SELECT kind FROM artifacts WHERE meeting_id=? AND path=?",
        (meeting_id, str(note)),
    )
    assert artifact == {"kind": "document_md"}


def test_already_imported_managed_directory_rewritten_minutes_updates_without_quarantine(
    tmp_path,
):
    # 这场会还没人复核过（current_minutes_version 是 'generated'，不是
    # 'draft'），所以纠错改写走的是既有的「静默更新」分支，不开外部变更
    # 冲突——一旦人工先接手编辑过，才会改走 `_capture_external_change`。
    meeting_id = "vm-20260904-090100-aa112244"
    db, importer, directory = _seed_top_level_managed_meeting(
        tmp_path, meeting_id=meeting_id, job_id="job-rewrite-minutes"
    )

    (directory / "会议纪要.md").write_text("# 改写后的纪要内容", encoding="utf-8")
    report = importer.scan()

    assert report.quarantined == 0
    meeting = db.query_one(
        "SELECT title, conflict, current_minutes_version_id FROM meetings WHERE id=?",
        (meeting_id,),
    )
    assert meeting["conflict"] == 0
    minutes = db.query_one(
        "SELECT markdown FROM minutes_versions WHERE id=?",
        (meeting["current_minutes_version_id"],),
    )
    assert minutes["markdown"] == "# 改写后的纪要内容"


def test_already_imported_managed_directory_audio_rewrite_is_quarantined(tmp_path):
    meeting_id = "vm-20260904-090200-aa112255"
    db, importer, directory = _seed_top_level_managed_meeting(
        tmp_path, meeting_id=meeting_id, job_id="job-rewrite-audio"
    )

    (directory / f"{meeting_id}.m4a").write_bytes(b"tampered-audio-bytes")
    report = importer.scan()

    assert report.quarantined == 1
    assert report.quarantine_details[0]["directory"] == str(directory)
    assert "音频" in report.quarantine_details[0]["reason"]
    # 隔离目录的既有记录原样保留，不会被 `_cleanup_stale_artifacts` 连坐清空。
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 1


def test_already_imported_managed_directory_missing_registered_file_is_quarantined(tmp_path):
    meeting_id = "vm-20260904-090300-aa112266"
    db, importer, directory = _seed_top_level_managed_meeting(
        tmp_path, meeting_id=meeting_id, job_id="job-missing-html"
    )

    (directory / "会议纪要.html").unlink()
    report = importer.scan()

    assert report.quarantined == 1
    assert report.quarantine_details[0]["directory"] == str(directory)
    assert "缺失" in report.quarantine_details[0]["reason"]
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 1


@pytest.mark.parametrize("location", ["hidden", "pending", "flat"])
def test_pending_archive_failure_is_not_imported_as_completed(tmp_path, location):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260712-181605-0b445e56"
    job_id = f"job-pending-archive-failed-{location}"
    if location == "hidden":
        directory = archive / ".workbench-drafts" / job_id / "attempt-1"
    elif location == "pending":
        directory = archive / "待校对" / "260712 归档失败"
    else:
        directory = archive / "260712 一级归档失败"
    write_managed_unreviewed_bundle(directory, meeting_id, job_id)
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            """CREATE TABLE jobs(
                   job_id TEXT PRIMARY KEY,
                   status TEXT,
                   current_attempt INTEGER,
                   archive_dir TEXT,
                   failure_stage TEXT
               )"""
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?, 'pending_archive')",
            (job_id, str(directory)),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.meetings_seen == 0
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 0


def test_hidden_to_pending_relocation_preserves_manual_drafts_without_conflict(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260712-223621-22853426"
    job_id = "job-relocated"
    hidden = write_managed_unreviewed_bundle(
        archive / ".workbench-drafts" / job_id / "attempt-1",
        meeting_id,
        job_id,
        text="自动正文",
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?)",
            (job_id, str(hidden)),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    service = MeetingService(db, archive_root=archive)
    transcript_draft_id = service.ensure_draft(meeting_id)
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (transcript_draft_id,)
    )
    segments[0]["text"] = "人工修订正文"
    service.save_segments(meeting_id, segments, expected_base_version_id=transcript_draft_id)
    minutes_draft_id = service.save_minutes(
        meeting_id,
        "# 人工修订纪要",
        expected_base_version_id=db.query_one(
            "SELECT current_minutes_version_id FROM meetings WHERE id=?", (meeting_id,)
        )["current_minutes_version_id"],
    )
    before = db.query_one(
        "SELECT current_transcript_version_id, current_minutes_version_id FROM meetings WHERE id=?",
        (meeting_id,),
    )

    pending = archive / "待校对" / "260712 路径迁移"
    pending.parent.mkdir()
    hidden.rename(pending)
    with sqlite3.connect(relay_db) as connection:
        connection.execute("UPDATE jobs SET archive_dir=? WHERE job_id=?", (str(pending), job_id))

    report = importer.scan()

    meeting = db.query_one(
        """SELECT status, canonical_dir, source_priority, current_transcript_version_id,
                  current_minutes_version_id, conflict FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert report.conflicts == 0
    assert meeting == {
        "status": "draft_modified",
        "canonical_dir": str(pending),
        "source_priority": 51,
        "current_transcript_version_id": before["current_transcript_version_id"],
        "current_minutes_version_id": before["current_minutes_version_id"],
        "conflict": 0,
    }
    assert minutes_draft_id == before["current_minutes_version_id"]
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM artifacts WHERE meeting_id=? AND path LIKE ?",
            (meeting_id, f"{hidden}%"),
        )["count"]
        == 0
    )
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM events WHERE meeting_id=? AND event_type='artifact_source_relocated'",
            (meeting_id,),
        )["count"]
        == 1
    )


def test_pending_whisper_install_preserves_manual_drafts_without_external_conflict(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260712-223621-22853427"
    job_id = "job-late-whisper"
    hidden = write_managed_unreviewed_bundle(
        archive / ".workbench-drafts" / job_id / "attempt-1",
        meeting_id,
        job_id,
        text="自动正文",
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?)",
            (job_id, str(hidden)),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()

    pending = archive / "待校对" / "260712 异步 Whisper"
    pending.parent.mkdir()
    hidden.rename(pending)
    with sqlite3.connect(relay_db) as connection:
        connection.execute("UPDATE jobs SET archive_dir=? WHERE job_id=?", (str(pending), job_id))
    importer.scan()

    service = MeetingService(db, archive_root=archive)
    transcript_draft_id = service.ensure_draft(meeting_id)
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (transcript_draft_id,)
    )
    segments[0]["text"] = "人工修订正文"
    service.save_segments(meeting_id, segments, expected_base_version_id=transcript_draft_id)
    minutes_draft_id = service.save_minutes(
        meeting_id,
        "# 人工修订纪要",
        expected_base_version_id=db.query_one(
            "SELECT current_minutes_version_id FROM meetings WHERE id=?", (meeting_id,)
        )["current_minutes_version_id"],
    )
    before = db.query_one(
        """SELECT status, source_signature, current_transcript_version_id,
                  current_minutes_version_id FROM meetings WHERE id=?""",
        (meeting_id,),
    )

    write_complete_whisper_reference(pending, meeting_id)
    report = importer.scan()

    meeting = db.query_one(
        """SELECT status, source_signature, current_transcript_version_id,
                  current_minutes_version_id, conflict FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert report.conflicts == 0
    assert meeting["status"] == "draft_modified"
    assert meeting["source_signature"] != before["source_signature"]
    assert meeting["current_transcript_version_id"] == before["current_transcript_version_id"]
    assert meeting["current_minutes_version_id"] == before["current_minutes_version_id"]
    assert meeting["current_minutes_version_id"] == minutes_draft_id
    assert meeting["conflict"] == 0
    assert {
        row["kind"]
        for row in db.query_all(
            "SELECT kind FROM artifacts WHERE meeting_id=? AND kind LIKE 'whisper_%'",
            (meeting_id,),
        )
    } == {
        "whisper_json",
        "whisper_srt",
        "whisper_txt",
        "whisper_tsv",
        "whisper_vtt",
        "whisper_log",
    }
    assert (
        db.query_one(
            """SELECT COUNT(*) AS count FROM transcript_versions
           WHERE meeting_id=? AND kind='whisper_reference'""",
            (meeting_id,),
        )["count"]
        == 1
    )

    replacement = pending / "whisper-ref" / f"{meeting_id}.srt"
    replacement.write_text("1\n00:00:01,000 --> 00:00:02,000\nWhisper 替换正文\n", encoding="utf-8")
    replacement_report = importer.scan()

    replaced = db.query_one(
        """SELECT status, current_transcript_version_id, current_minutes_version_id, conflict
           FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert replacement_report.conflicts == 0
    assert replaced == {
        "status": "draft_modified",
        "current_transcript_version_id": before["current_transcript_version_id"],
        "current_minutes_version_id": before["current_minutes_version_id"],
        "conflict": 0,
    }
    assert (
        db.query_one(
            """SELECT COUNT(*) AS count FROM transcript_versions
           WHERE meeting_id=? AND kind='whisper_reference'""",
            (meeting_id,),
        )["count"]
        == 2
    )


def test_hidden_relocation_with_new_staging_whisper_is_safe_in_same_scan(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    meeting_id = "vm-20260712-223621-22853428"
    job_id = "job-relocation-staging-whisper"
    hidden = write_managed_unreviewed_bundle(
        archive / ".workbench-drafts" / job_id / "attempt-1",
        meeting_id,
        job_id,
        text="自动正文",
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, 'completed_unreviewed', 1, ?)",
            (job_id, str(hidden)),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    service = MeetingService(db, archive_root=archive)
    transcript_draft_id = service.ensure_draft(meeting_id)
    minutes_draft_id = service.save_minutes(
        meeting_id,
        "# 人工纪要",
        expected_base_version_id=db.query_one(
            "SELECT current_minutes_version_id FROM meetings WHERE id=?", (meeting_id,)
        )["current_minutes_version_id"],
    )
    before = db.query_one(
        "SELECT current_transcript_version_id, current_minutes_version_id FROM meetings WHERE id=?",
        (meeting_id,),
    )

    pending = archive / "待校对" / "260712 搬迁同轮 Whisper"
    pending.parent.mkdir()
    hidden.rename(pending)
    with sqlite3.connect(relay_db) as connection:
        connection.execute("UPDATE jobs SET archive_dir=? WHERE job_id=?", (str(pending), job_id))
    write_complete_whisper_reference(staging / meeting_id, meeting_id)

    report = importer.scan()

    meeting = db.query_one(
        """SELECT status, canonical_dir, current_transcript_version_id,
                  current_minutes_version_id, conflict FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    assert report.conflicts == 0
    assert meeting == {
        "status": "draft_modified",
        "canonical_dir": str(pending),
        "current_transcript_version_id": before["current_transcript_version_id"],
        "current_minutes_version_id": before["current_minutes_version_id"],
        "conflict": 0,
    }
    assert meeting["current_transcript_version_id"] == transcript_draft_id
    assert meeting["current_minutes_version_id"] == minutes_draft_id
    assert (
        db.query_one(
            """SELECT COUNT(*) AS count FROM artifacts
           WHERE meeting_id=? AND source_root='staging' AND kind LIKE 'whisper_%'""",
            (meeting_id,),
        )["count"]
        == 6
    )
    assert (
        db.query_one(
            """SELECT COUNT(*) AS count FROM transcript_versions
           WHERE meeting_id=? AND kind='whisper_reference'""",
            (meeting_id,),
        )["count"]
        == 1
    )


def test_manifest_cannot_inject_unsafe_meeting_id(tmp_path):
    archive = tmp_path / "archive"
    directory = archive / "260710 恶意清单"
    directory.mkdir(parents=True)
    (directory / "audio.m4a").write_bytes(b"audio")
    (directory / "audio.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n正文\n", encoding="utf-8"
    )
    (directory / "workbench-manifest.json").write_text(
        json.dumps({"meeting_id": "../../escaped-by-manifest"}), encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    ids = [row["id"] for row in db.query_all("SELECT id FROM meetings")]
    assert ids and all(value.startswith("fp-") for value in ids)
    assert "../../escaped-by-manifest" not in ids


def test_archive_scanner_does_not_follow_top_level_directory_symlink(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    outside = tmp_path / "outside"
    write_meeting(outside, "越界会议", official=True, transcript_text="越界正文")
    (archive / "linked-outside").symlink_to(outside / "越界会议", target_is_directory=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.meetings_seen == 0
    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 0


def test_staging_manifest_cannot_claim_published_status(tmp_path):
    staging = tmp_path / "staging"
    directory = staging / "vm-20260710-130000-ABCD1234"
    directory.mkdir(parents=True)
    meeting_id = "vm-20260710-130000-ABCD1234"
    (directory / f"{meeting_id}.m4a").write_bytes(b"audio")
    (directory / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n正文\n", encoding="utf-8"
    )
    (directory / "workbench-manifest.json").write_text(
        json.dumps({"meeting_id": meeting_id, "status": "published"}), encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=staging,
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    meeting = db.query_one("SELECT status, source_priority FROM meetings")
    assert meeting == {"status": "completed_unreviewed", "source_priority": 10}


def test_incomplete_draft_is_not_imported_even_if_relay_job_says_completed(tmp_path):
    archive = tmp_path / "archive"
    attempt = archive / ".workbench-drafts" / "job-incomplete" / "attempt-1"
    attempt.mkdir(parents=True)
    (attempt / "vm-20260710-140000-ABCD1234.m4a").write_bytes(b"audio")
    (attempt / "vm-20260710-140000-ABCD1234.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n正文\n", encoding="utf-8"
    )
    listed = [path for path in attempt.rglob("*") if path.is_file()]
    (attempt / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-incomplete",
                "attempt": 1,
                "artifacts": [
                    {
                        "path": str(path.relative_to(attempt)),
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    for path in listed
                ],
            }
        ),
        encoding="utf-8",
    )
    relay_db = tmp_path / "relay.sqlite3"
    with sqlite3.connect(relay_db) as connection:
        connection.execute(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-incomplete', 'completed_unreviewed', 1, ?)",
            (str(attempt),),
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    assert db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"] == 0


def test_hidden_history_and_stale_artifact_rows_are_removed_from_current_sources(tmp_path):
    archive = tmp_path / "archive"
    meeting_dir = write_meeting(archive, "正式会议", official=True, transcript_text="当前正文")
    hidden = meeting_dir / ".workbench-history" / "release-1"
    hidden.mkdir(parents=True)
    hidden_file = hidden / "vm-20260101-120000.srt"
    hidden_file.write_text("1\n00:00:00,000 --> 00:00:01,000\n旧正文\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, created_at)
           VALUES ('vm-20260101-120000', 'srt', 'source', 'archive', ?, CURRENT_TIMESTAMP)""",
        (str(hidden_file),),
    )

    importer.scan()

    assert db.query_one("SELECT 1 AS ok FROM artifacts WHERE path=?", (str(hidden_file),)) is None


def test_completed_reprocess_attempt_overrides_formal_working_versions_but_keeps_canonical_dir(
    tmp_path,
):
    archive = tmp_path / "archive"
    formal = write_meeting(archive, "正式会议", official=True, transcript_text="旧转写")
    (formal / "会议纪要.md").write_text("# 旧纪要", encoding="utf-8")
    (formal / "会议纪要.html").write_text("<h1>旧纪要</h1>", encoding="utf-8")
    relay_db = tmp_path / "relay.sqlite3"
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()

    meeting_id = "vm-20260101-120000"
    attempt = archive / ".workbench-drafts" / "job-reprocess" / "attempt-2"
    attempt.mkdir(parents=True)
    files = {
        f"{meeting_id}.m4a": b"fake-audio",
        f"{meeting_id}.srt": b"1\n00:00:00,000 --> 00:00:01,000\nnew transcript\n",
        f"{meeting_id}.txt": b"new transcript",
        "spk.txt": b"SPEAKER_00\tuser",
        f"{meeting_id}.funasr.json": b"{}",
        f"{meeting_id}.funasr.log": b"ok",
        "meeting.md": b"# new minutes",
        "meeting.html": b"<h1>new minutes</h1>",
    }
    for relative, content in files.items():
        (attempt / relative).write_bytes(content)
    (attempt / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "job-reprocess",
                "attempt": 2,
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
            "INSERT INTO jobs VALUES ('job-reprocess', 'completed_unreviewed', 2, ?)",
            (str(attempt),),
        )

    importer.scan()

    meeting = db.query_one(
        """SELECT status, canonical_dir, source_priority, source_job_id,
                  current_minutes_version_id FROM meetings WHERE id=?""",
        (meeting_id,),
    )
    current_text = db.query_one(
        """SELECT s.text FROM segments s JOIN meetings m
           ON m.current_transcript_version_id=s.version_id WHERE m.id=? LIMIT 1""",
        (meeting_id,),
    )["text"]
    minutes = db.query_one(
        "SELECT markdown FROM minutes_versions WHERE id=?",
        (meeting["current_minutes_version_id"],),
    )["markdown"]
    assert meeting["status"] == "completed_unreviewed"
    assert meeting["canonical_dir"] == str(formal)
    assert meeting["source_priority"] == 100
    assert meeting["source_job_id"] == "job-reprocess"
    assert current_text == "new transcript"
    assert "new minutes" in minutes


def test_minutes_only_attempt_updates_minutes_without_overwriting_edited_transcript(tmp_path):
    archive = tmp_path / "archive"
    formal = write_meeting(archive, "正式会议", official=True, transcript_text="旧转写")
    (formal / "会议纪要.md").write_text("# 旧纪要", encoding="utf-8")
    (formal / "会议纪要.html").write_text("<h1>旧纪要</h1>", encoding="utf-8")
    relay_db = tmp_path / "relay.sqlite3"
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    meeting_id = "vm-20260101-120000"
    service = MeetingService(db, archive_root=archive)
    draft_id = service.ensure_draft(meeting_id)
    segments = db.query_all(
        "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (draft_id,)
    )
    segments[0]["text"] = "人工编辑后的逐字稿"
    service.save_segments(meeting_id, segments)
    snapshot = render_transcript_txt(segments).rstrip() + "\n"
    snapshot_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    attempt = archive / ".workbench-drafts" / "job-minutes-only" / "attempt-2"
    attempt.mkdir(parents=True)
    files = {
        f"{meeting_id}.m4a": b"fake-audio",
        "input-transcript.txt": snapshot.encode("utf-8"),
        "会议纪要.md": b"# regenerated minutes",
        "会议纪要.html": b"<h1>regenerated minutes</h1>",
    }
    for relative, content in files.items():
        (attempt / relative).write_bytes(content)
    (attempt / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "meeting_id": meeting_id,
                "job_id": "job-minutes-only",
                "attempt": 2,
                "status": "completed_unreviewed",
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": snapshot_hash,
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
            "INSERT INTO jobs VALUES ('job-minutes-only', 'completed_unreviewed', 2, ?)",
            (str(attempt),),
        )

    report = importer.scan()

    meeting = db.query_one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    current = db.query_one(
        "SELECT kind, published FROM transcript_versions WHERE id=?",
        (meeting["current_transcript_version_id"],),
    )
    current_text = db.query_one(
        "SELECT text FROM segments WHERE version_id=? ORDER BY ordinal LIMIT 1",
        (meeting["current_transcript_version_id"],),
    )["text"]
    minutes = db.query_one(
        """SELECT markdown, kind, published, source_job_id, source_attempt,
                  requested_stage, input_transcript_sha256
             FROM minutes_versions WHERE id=?""",
        (meeting["current_minutes_version_id"],),
    )
    assert report.conflicts == 0
    assert meeting["canonical_dir"] == str(formal)
    assert meeting["status"] == "draft_modified"
    assert meeting["source_job_id"] == "job-minutes-only"
    assert current_text == "人工编辑后的逐字稿"
    assert current == {"kind": "draft", "published": 0}
    assert "regenerated minutes" in minutes["markdown"]
    assert minutes["kind"] == "generated"
    assert minutes["published"] == 0
    assert minutes["source_job_id"] == "job-minutes-only"
    assert minutes["source_attempt"] == 2
    assert minutes["requested_stage"] == "minutes_generating"
    assert minutes["input_transcript_sha256"] == snapshot_hash

    db.execute(
        """UPDATE minutes_versions SET source_job_id=NULL, source_attempt=NULL,
           requested_stage=NULL, input_transcript_sha256=NULL WHERE id=?""",
        (meeting["current_minutes_version_id"],),
    )
    importer.scan()
    backfilled = db.query_one(
        """SELECT source_job_id, source_attempt, requested_stage,
                  input_transcript_sha256 FROM minutes_versions WHERE id=?""",
        (meeting["current_minutes_version_id"],),
    )
    assert backfilled == {
        "source_job_id": "job-minutes-only",
        "source_attempt": 2,
        "requested_stage": "minutes_generating",
        "input_transcript_sha256": snapshot_hash,
    }


def test_rescan_repairs_legacy_id_titles_without_touching_sources(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260715-214003-1438d4b5"
    meeting_dir = archive / "vm-20260715-214003-1438D4B5"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n开场白\n", encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    # 模拟历史库里遗留的编号标题。
    db.execute("UPDATE meetings SET title = ? WHERE id = ?", (meeting_id.upper(), meeting_id))

    importer.scan()

    assert db.query_one("SELECT title FROM meetings WHERE id=?", (meeting_id,))["title"] == (
        "260715 未命名录音"
    )


def test_rescan_keeps_a_manually_named_meeting(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260715-214003-1438d4b5"
    meeting_dir = archive / "vm-20260715-214003-1438D4B5"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n开场白\n", encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    db.execute("UPDATE meetings SET title = '协会MDT评审' WHERE id = ?", (meeting_id,))
    db.add_event(
        "meeting_updated",
        meeting_id=meeting_id,
        actor="user",
        payload={"fields": ["title"]},
    )

    importer.scan()

    assert db.query_one("SELECT title FROM meetings WHERE id=?", (meeting_id,))["title"] == (
        "协会MDT评审"
    )


def test_meetings_sharing_one_material_folder_keep_their_own_titles(tmp_path):
    """一批原始录音被挪进同一个文件夹后，标题不能整批变成那个文件夹名。"""
    archive = tmp_path / "archive"
    first = "vm-20260712-181605-0b445e55"
    second = "vm-20260712-215202-23ca270d"
    for meeting_id, folder, heading in (
        (first, "260712 数字营销平台规划沟通", "数字营销平台规划沟通"),
        (second, "260712 MDT病例与医生注册优化", "MDT病例与医生注册优化"),
    ):
        meeting_dir = archive / folder
        meeting_dir.mkdir(parents=True)
        (meeting_dir / f"{meeting_id}.m4a").write_bytes(meeting_id.encode())
        (meeting_dir / f"{meeting_id}.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n开场白\n", encoding="utf-8"
        )
        (meeting_dir / f"{heading}.md").write_text(f"# {heading}\n\n- 决策", encoding="utf-8")
        (meeting_dir / f"{heading}.html").write_text(f"<h1>{heading}</h1>", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()

    # 磁盘治理：原始录音被平铺进一个共用文件夹，各自的会议目录不复存在。
    shared = archive / "relay原始录音-260809"
    shared.mkdir()
    for meeting_id, folder in (
        (first, "260712 数字营销平台规划沟通"),
        (second, "260712 MDT病例与医生注册优化"),
    ):
        (shared / f"{meeting_id}.m4a").write_bytes(meeting_id.encode())
        shutil.rmtree(archive / folder)

    importer.scan()

    titles = {row["id"]: row["title"] for row in db.query_all("SELECT id, title FROM meetings", ())}
    assert titles[first] == "数字营销平台规划沟通"
    assert titles[second] == "MDT病例与医生注册优化"


def test_shared_material_folder_without_minutes_falls_back_to_dated_titles(tmp_path):
    archive = tmp_path / "archive"
    shared = archive / "relay原始录音-260809"
    shared.mkdir(parents=True)
    first = "vm-20260705-194000-0e7baae8"
    second = "vm-20260707-184448-e82656f1"
    for meeting_id in (first, second):
        (shared / f"{meeting_id}.m4a").write_bytes(meeting_id.encode())
        (shared / f"{meeting_id}.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n开场白\n", encoding="utf-8"
        )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    ArchiveImporter(db, settings).scan()

    titles = {row["id"]: row["title"] for row in db.query_all("SELECT id, title FROM meetings", ())}
    assert titles[first] == "260705 未命名录音"
    assert titles[second] == "260707 未命名录音"


def test_recording_timestamp_in_the_id_is_read_as_local_wall_clock(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260729-020903-dc7adeb9"
    meeting_dir = archive / "260729 ACME内容征集流程走查"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n开场白\n", encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()

    recorded_at = db.query_one(
        "SELECT recording_date FROM meetings WHERE id=?", (meeting_id,)
    )["recording_date"]
    parsed = datetime.fromisoformat(recorded_at)
    # 归档目录写的是 260729，界面上的日期必须是同一天。
    assert (parsed.year, parsed.month, parsed.day) == (2026, 7, 29)
    assert (parsed.hour, parsed.minute) == (2, 9)
    assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()


def test_rescan_repairs_a_recording_date_that_was_stored_as_utc(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260729-020903-dc7adeb9"
    meeting_dir = archive / "260729 ACME内容征集流程走查"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n开场白\n", encoding="utf-8"
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "db.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    db.execute(
        "UPDATE meetings SET recording_date = '2026-07-29T02:09:03+00:00' WHERE id = ?",
        (meeting_id,),
    )

    importer.scan()

    recorded_at = db.query_one(
        "SELECT recording_date FROM meetings WHERE id=?", (meeting_id,)
    )["recording_date"]
    assert datetime.fromisoformat(recorded_at).utcoffset() == (
        datetime.now().astimezone().utcoffset()
    )


def test_regenerated_minutes_adopt_the_new_archive_and_title(tmp_path):
    """纪要重生成走完整闭环：v3 的 SRT 快照要被认作新鲜，标题跟着补回来。"""
    archive = tmp_path / "archive"
    archive.mkdir(parents=True)
    staging = tmp_path / "staging"
    meeting_id = "vm-20260715-214003-1438d4b5"
    # 纪要生成失败过的会议只剩转写产物，标题退成占位。
    staging_dir = staging / meeting_id.upper()
    staging_dir.mkdir(parents=True)
    (staging_dir / f"{meeting_id}.m4a").write_bytes(b"fake-audio")
    (staging_dir / f"{meeting_id}.srt").write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n开场先对齐范围\n", encoding="utf-8"
    )
    relay_db = tmp_path / "relay.sqlite3"
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        database_path=tmp_path / "data" / "workbench.sqlite3",
        relay_jobs_db=relay_db,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    importer.scan()
    assert db.query_one("SELECT title FROM meetings WHERE id=?", (meeting_id,))["title"] == (
        "260715 未命名录音"
    )

    service = MeetingService(db, archive_root=archive)
    snapshot_path, snapshot_hash = service.create_transcript_snapshot(
        meeting_id, tmp_path / "relay-inputs"
    )
    regenerated = archive / "260715 协会版MDT原型走查收敛"
    regenerated.mkdir(parents=True)
    files = {
        f"{meeting_id}.m4a": (staging_dir / f"{meeting_id}.m4a").read_bytes(),
        "input-transcript.srt": snapshot_path.read_bytes(),
        "协会版MDT原型走查收敛.md": "# 协会版MDT原型走查收敛\n".encode("utf-8"),
        "协会版MDT原型走查收敛.html": b"<h1>generated</h1>",
    }
    for relative, content in files.items():
        (regenerated / relative).write_bytes(content)
    (regenerated / "workbench-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "meeting_id": meeting_id,
                "job_id": "job-regenerated",
                "attempt": 6,
                "minutes_protocol_version": 3,
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": snapshot_hash,
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
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, "
            "current_attempt INTEGER, archive_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-regenerated', 'completed_unreviewed', 6, ?)",
            (str(regenerated),),
        )

    report = importer.scan()

    meeting = db.query_one(
        "SELECT title, canonical_dir, current_minutes_version_id FROM meetings WHERE id=?",
        (meeting_id,),
    )
    assert report.errors == 0
    assert meeting["title"] == "协会版MDT原型走查收敛"
    assert meeting["canonical_dir"] == str(regenerated)
    assert meeting["current_minutes_version_id"] is not None
    assert not db.query_all(
        "SELECT 1 FROM events WHERE meeting_id=? AND event_type='stale_minutes_generation_ignored'",
        (meeting_id,),
    )


def write_meeting_with_funasr_speakers(root, dirname, meeting_id, *, srt_text, funasr_sentences):
    meeting_dir = root / dirname
    meeting_dir.mkdir(parents=True)
    (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"fake-audio")
    (meeting_dir / f"{meeting_id}.srt").write_text(srt_text, encoding="utf-8")
    (meeting_dir / f"{meeting_id}.funasr.json").write_text(
        json.dumps({"sentence_info": funasr_sentences}), encoding="utf-8"
    )
    return meeting_dir


def test_new_meeting_import_backfills_speaker_labels_from_funasr_json(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260101-120000"
    write_meeting_with_funasr_speakers(
        archive,
        "多说话人会议",
        meeting_id,
        srt_text=(
            "1\n00:00:01,000 --> 00:00:02,000\n第一句发言\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n第二句发言\n"
        ),
        funasr_sentences=[
            {"start": 1000, "end": 2000, "spk": 0, "text": "第一句发言"},
            {"start": 3000, "end": 4000, "spk": 1, "text": "第二句发言"},
        ],
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.errors == 0
    assert report.speaker_backfill_applied == 1
    segments = db.query_all(
        """SELECT s.ordinal, s.speaker_label FROM segments s
           JOIN meetings m ON m.current_transcript_version_id = s.version_id
           WHERE m.id = ? ORDER BY s.ordinal""",
        (meeting_id,),
    )
    assert [row["speaker_label"] for row in segments] == ["SPEAKER_00", "SPEAKER_01"]
    speakers = {
        row["label"] for row in db.query_all("SELECT label FROM speakers WHERE meeting_id=?", (meeting_id,))
    }
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}


def test_speaker_backfill_skips_safely_when_funasr_json_text_mismatches_srt(tmp_path):
    archive = tmp_path / "archive"
    meeting_id = "vm-20260101-130000"
    write_meeting_with_funasr_speakers(
        archive,
        "文本不一致会议",
        meeting_id,
        srt_text="1\n00:00:01,000 --> 00:00:02,000\n真实转写文本\n",
        funasr_sentences=[{"start": 1000, "end": 2000, "spk": 0, "text": "另一份不同的文本"}],
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()

    report = ArchiveImporter(db, settings).scan()

    assert report.errors == 0
    assert report.speaker_backfill_applied == 0
    assert report.speaker_backfill_skipped == 1
    segments = db.query_all(
        """SELECT s.speaker_label FROM segments s
           JOIN meetings m ON m.current_transcript_version_id = s.version_id
           WHERE m.id = ?""",
        (meeting_id,),
    )
    assert segments and all(row["speaker_label"] is None for row in segments)
    assert db.query_all("SELECT 1 FROM speakers WHERE meeting_id=?", (meeting_id,)) == []
    # 逐字稿导入本身不受补标失败影响。
    assert db.exact_search("真实转写文本")


def test_cached_sha256_reuses_fingerprint_cache_row_without_rehashing(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    path = tmp_path / "audio.m4a"
    path.write_bytes(b"cached-audio-bytes")
    stat = path.stat()
    cached_sha256 = "c" * 64
    db.execute(
        """INSERT INTO fingerprint_cache(path, size_bytes, mtime_ns, sha256, pcm_sha256, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(path), stat.st_size, stat.st_mtime_ns, cached_sha256, "p" * 64, "2026-09-05T00:00:00Z"),
    )
    calls = []
    monkeypatch.setattr(
        "meeting_workbench.importer.sha256_file",
        lambda candidate: calls.append(candidate) or "should-not-be-used",
    )
    importer = ArchiveImporter(db, settings)

    assert importer._cached_sha256(path) == cached_sha256
    assert calls == []

    # 未命中缓存（不同路径，压根没记录）时照旧现算，不吞异常也不返回假值。
    miss_path = tmp_path / "not-cached.m4a"
    miss_path.write_bytes(b"never-hashed-before")
    assert importer._cached_sha256(miss_path) == "should-not-be-used"
    assert calls == [miss_path]
