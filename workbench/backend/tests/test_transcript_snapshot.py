import hashlib

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.parsers import parse_srt
from meeting_workbench.service import MeetingService, PublishValidationError

from .helpers import seed_editable_meeting


def service(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    db = Database(settings.database_path)
    db.initialize()
    return db, MeetingService(db, archive_root=settings.archive_root)


def test_transcript_snapshot_is_atomic_utf8_srt_with_matching_hash(tmp_path):
    db, meetings = service(tmp_path)
    seed_editable_meeting(db, tmp_path / "archive", meeting_id="vm-snapshot")

    path, digest = meetings.create_transcript_snapshot("vm-snapshot", tmp_path / "snapshots")

    content = path.read_bytes()
    assert path.suffix == ".srt"
    assert digest == hashlib.sha256(content).hexdigest()
    assert [item["text"] for item in parse_srt(path)] == ["第一段内容", "第二段内容"]
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_transcript_snapshot_rejects_empty_current_transcript(tmp_path):
    db, meetings = service(tmp_path)
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-empty', '空稿', 'published')")

    with pytest.raises(PublishValidationError, match="逐字稿为空"):
        meetings.create_transcript_snapshot("vm-empty", tmp_path / "snapshots")


def test_transcript_snapshot_rejects_non_monotonic_timing(tmp_path):
    db, meetings = service(tmp_path)
    _directory, _audio, version = seed_editable_meeting(
        db, tmp_path / "archive", meeting_id="vm-timing"
    )
    db.execute(
        "UPDATE segments SET start_ms=500, end_ms=800 WHERE version_id=? AND ordinal=1", (version,)
    )

    with pytest.raises(PublishValidationError, match="时间顺序"):
        meetings.create_transcript_snapshot("vm-timing", tmp_path / "snapshots")
