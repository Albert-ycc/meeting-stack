import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database


def test_online_backup_is_readable_and_mirrored(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
        staging_root=tmp_path / "staging",
    )
    db = Database(settings.database_path)
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-backup', '备份验证', 'published')"
    )

    result = BackupManager(db, settings).create()

    with sqlite3.connect(result.local_path) as connection:
        assert (
            connection.execute("SELECT title FROM meetings WHERE id='vm-backup'").fetchone()[0]
            == "备份验证"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert result.mirror_path is not None
    assert result.mirror_path.parent == archive / ".meeting-workbench-backups"
    assert result.mirror_path.read_bytes() == result.local_path.read_bytes()
    assert result.status == "healthy"
    receipt = json.loads((settings.backup_dir / "last-backup.json").read_text())
    assert receipt["local_sha256"] == receipt["mirror_sha256"]
    assert receipt["status"] == "healthy"
    assert receipt["mirror_ok"] is True


def test_backup_rotation_keeps_fourteen_newest(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "missing-archive",
    )
    db = Database(settings.database_path)
    db.initialize()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    manager = BackupManager(db, settings)

    for day in range(16):
        manager.create(now=start + timedelta(days=day))

    assert len(list(settings.backup_dir.glob("workbench-*.sqlite3"))) == 14
    assert not (settings.backup_dir / "workbench-20260101T000000Z.sqlite3").exists()


def test_sixteen_concurrent_backups_are_unique_and_readable(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    db.execute("INSERT INTO meetings(id, title) VALUES ('vm-concurrent', '并发备份')")
    manager = BackupManager(db, settings, retention=20)
    same_instant = datetime(2026, 7, 11, 12, 0, 0, 123456, tzinfo=UTC)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda _index: manager.create(now=same_instant), range(16)))

    local_paths = {result.local_path for result in results}
    mirror_paths = {result.mirror_path for result in results}
    assert len(local_paths) == 16
    assert len(mirror_paths) == 16
    for result in results:
        assert result.status == "healthy"
        assert result.mirror_path is not None
        assert result.local_path.read_bytes() == result.mirror_path.read_bytes()
        with sqlite3.connect(result.local_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_mirror_failure_keeps_local_backup_and_records_degraded_receipt(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    manager = BackupManager(db, settings)

    with patch.object(manager, "_atomic_copy", side_effect=OSError("mirror offline")):
        result = manager.create()

    assert result.local_path.is_file()
    assert result.mirror_path is None
    assert result.status == "degraded"
    assert result.mirror_error == "OSError"
    receipt = json.loads((settings.backup_dir / "last-backup.json").read_text())
    assert receipt["status"] == "degraded"
    assert receipt["mirror_ok"] is False
    assert receipt["mirror_error"] == "OSError"
    assert receipt["local_sha256"]


def test_failed_mirror_validation_removes_installed_candidate(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    manager = BackupManager(db, settings)
    original_verify = manager._verify_database
    mirror_dir = archive / ".meeting-workbench-backups"

    def reject_mirror(path):
        if path.parent == mirror_dir:
            raise sqlite3.DatabaseError("simulated corrupt mirror")
        return original_verify(path)

    with patch.object(manager, "_verify_database", side_effect=reject_mirror):
        result = manager.create()

    assert result.status == "degraded"
    assert result.local_path.is_file()
    assert list(mirror_dir.glob("workbench-*.sqlite3")) == []


def test_failed_mirror_fsync_removes_installed_candidate(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    manager = BackupManager(db, settings)
    mirror_dir = archive / ".meeting-workbench-backups"

    def install_then_fail(source, destination):
        destination.write_bytes(source.read_bytes())
        raise OSError("simulated directory fsync failure")

    with patch.object(manager, "_atomic_copy", side_effect=install_then_fail):
        result = manager.create()

    assert result.status == "degraded"
    assert result.local_path.is_file()
    assert list(mirror_dir.glob("workbench-*.sqlite3")) == []


def test_rotation_discards_corrupt_snapshots_before_counting_retention(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    mirror_dir = archive / ".meeting-workbench-backups"
    mirror_dir.mkdir()
    corrupt = mirror_dir / "workbench-99991231T235959.999999Z-deadbeef.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()

    result = BackupManager(db, settings, retention=1).create()

    assert result.status == "healthy"
    assert not corrupt.exists()
    assert list(mirror_dir.glob("workbench-*.sqlite3")) == [result.mirror_path]


def _seed_segments_with_embeddings(db: Database, count: int) -> None:
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-emb', '向量验证', 'published')")
    db.execute(
        """INSERT INTO transcript_versions(id, meeting_id, version_no, kind, published, created_at)
           VALUES ('ver-emb', 'vm-emb', 1, 'asr', 1, '2026-08-09T00:00:00Z')"""
    )
    for ordinal in range(count):
        segment_id = f"seg-{ordinal}"
        db.execute(
            """INSERT INTO segments(id, version_id, meeting_id, ordinal, start_ms, end_ms, text)
               VALUES (?, 'ver-emb', 'vm-emb', ?, ?, ?, ?)""",
            (segment_id, ordinal, ordinal * 1000, ordinal * 1000 + 900, f"第 {ordinal} 句转写"),
        )
        db.execute(
            """INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at)
               VALUES (?, 'bge-small-zh-v1.5', 512, ?, '2026-08-09T00:00:00Z')""",
            (segment_id, b"\x00" * 2048),
        )


def test_backup_excludes_embeddings_and_keeps_business_data(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    _seed_segments_with_embeddings(db, 400)

    result = BackupManager(db, settings).create()

    with sqlite3.connect(result.local_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        # segments 是重算 embeddings 的源，必须原样保留
        assert connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 400
        assert (
            connection.execute("SELECT text FROM segments WHERE id='seg-7'").fetchone()[0]
            == "第 7 句转写"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert result.derived_stripped is True
    assert result.local_path.stat().st_size < settings.database_path.stat().st_size
    receipt = json.loads((settings.backup_dir / "last-backup.json").read_text())
    assert receipt["derived_stripped"] is True
    assert receipt["derived_tables"] == ["embeddings"]


def test_backup_survives_when_derived_table_is_absent(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-noemb', '无向量表', 'published')")
    db.execute("DROP TABLE embeddings")

    result = BackupManager(db, settings).create()

    assert result.status == "healthy"
    assert result.derived_stripped is False
    with sqlite3.connect(result.local_path) as connection:
        assert (
            connection.execute("SELECT title FROM meetings WHERE id='vm-noemb'").fetchone()[0]
            == "无向量表"
        )
    receipt = json.loads((settings.backup_dir / "last-backup.json").read_text())
    assert receipt["derived_stripped"] is False
    assert receipt["derived_tables"] == []
