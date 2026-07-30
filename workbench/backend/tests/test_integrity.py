import hashlib
import json
import os

from meeting_workbench import cli
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.integrity import AudioIntegrityVerifier, last_audio_integrity_result

from .helpers import seed_editable_meeting


def make_verifier(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    staging.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )
    db = Database(settings.database_path)
    db.initialize()
    meeting_dir, audio, _version = seed_editable_meeting(db, archive)
    return AudioIntegrityVerifier(db, settings), db, settings, meeting_dir, audio


def test_audio_integrity_match_does_not_clear_existing_conflict(tmp_path):
    verifier, db, _settings, _meeting_dir, _audio = make_verifier(tmp_path)
    db.execute("UPDATE meetings SET conflict=1 WHERE id='vm-20260102-101500'")

    result = verifier.verify()

    assert result.checked == 1
    assert result.issues == 0
    assert db.query_one("SELECT conflict FROM meetings")["conflict"] == 1


def test_audio_integrity_uses_baseline_artifact_within_preferred_archive(tmp_path):
    verifier, db, _settings, meeting_dir, audio = make_verifier(tmp_path)
    expected = db.query_one("SELECT original_audio_sha256 FROM meetings")["original_audio_sha256"]
    db.execute("UPDATE artifacts SET sha256=? WHERE path=?", (expected, str(audio)))
    converted = meeting_dir / "converted.wav"
    converted.write_bytes(b"derived-audio-payload")
    converted_hash = hashlib.sha256(converted.read_bytes()).hexdigest()
    stat = converted.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
           VALUES ('vm-20260102-101500', 'audio', 'source', 'archive', ?, ?, ?, ?, ?)""",
        (str(converted), converted_hash, stat.st_size, stat.st_mtime_ns, utc_now()),
    )

    result = verifier.verify()

    assert result.checked == 1
    assert result.issues == 0


def test_audio_integrity_missing_uses_archive_artifact_and_deduplicates_event(tmp_path):
    verifier, db, settings, _meeting_dir, audio = make_verifier(tmp_path)
    expected = db.query_one("SELECT original_audio_sha256 FROM meetings")["original_audio_sha256"]
    draft_audio = settings.staging_root / "fallback.m4a"
    draft_audio.write_bytes(audio.read_bytes())
    stat = draft_audio.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('vm-20260102-101500', 'audio', 'source', 'draft', ?, ?, ?, ?)""",
        (str(draft_audio), stat.st_size, stat.st_mtime_ns, utc_now()),
    )
    audio.unlink()

    first = verifier.verify()
    second = verifier.verify()

    assert first.missing == 1 and second.missing == 1
    assert db.query_one("SELECT conflict FROM meetings")["conflict"] == 1
    assert (
        db.query_one("SELECT original_audio_sha256 FROM meetings")["original_audio_sha256"]
        == expected
    )
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM events WHERE event_type='audio_integrity_conflict'"
        )["count"]
        == 1
    )


def test_audio_integrity_detects_same_size_same_mtime_replacement(tmp_path):
    verifier, db, _settings, _meeting_dir, audio = make_verifier(tmp_path)
    expected = db.query_one("SELECT original_audio_sha256 FROM meetings")["original_audio_sha256"]
    original_stat = audio.stat()
    replacement = b"x" * original_stat.st_size
    assert hashlib.sha256(replacement).hexdigest() != expected
    audio.write_bytes(replacement)
    os.utime(audio, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    result = verifier.verify()

    assert result.mismatched == 1
    assert db.query_one("SELECT conflict FROM meetings")["conflict"] == 1
    assert (
        db.query_one("SELECT original_audio_sha256 FROM meetings")["original_audio_sha256"]
        == expected
    )


def test_verify_audio_cli_returns_zero_one_and_two(tmp_path, monkeypatch, capsys):
    verifier, _db, settings, _meeting_dir, audio = make_verifier(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert cli.main(["verify-audio"]) == 0
    matching_payload = json.loads(capsys.readouterr().out)
    assert matching_payload["issues"] == 0

    audio.unlink()
    assert cli.main(["verify-audio"]) == 1
    assert json.loads(capsys.readouterr().out)["missing"] == 1

    settings.archive_root.rename(tmp_path / "archive-offline")
    assert cli.main(["verify-audio"]) == 2
    assert "正式归档根目录不可用" in capsys.readouterr().err
    assert last_audio_integrity_result(Database(settings.database_path))["status"] == "failed"


def test_verify_audio_cli_rejects_missing_database_without_creating_empty_one(
    tmp_path, monkeypatch, capsys
):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    archive.mkdir()
    staging.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "missing.sqlite3",
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert cli.main(["verify-audio"]) == 2

    assert "数据库尚不存在" in capsys.readouterr().err
    assert not settings.database_path.exists()


def test_doctor_includes_latest_audio_integrity_result(tmp_path, monkeypatch, capsys):
    verifier, _db, settings, _meeting_dir, _audio = make_verifier(tmp_path)
    verifier.verify()
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert cli.main(["doctor"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["last_audio_verification"]["issues"] == 0
