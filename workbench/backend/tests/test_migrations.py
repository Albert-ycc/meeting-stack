import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, SCHEMA_VERSION
from meeting_workbench.qwen_shadow import QwenShadowService


def test_existing_database_is_backed_up_before_schema_migration(tmp_path):
    database_path = tmp_path / "data" / "workbench.sqlite3"
    database_path.parent.mkdir()
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT)")
        connection.execute("INSERT INTO legacy_marker VALUES ('before-migration')")
        connection.execute("PRAGMA user_version=1")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=database_path,
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    db = Database(database_path)

    db.initialize(before_migrate=lambda: BackupManager(db, settings).create())

    backup = next(settings.backup_dir.glob("workbench-*.sqlite3"))
    with sqlite3.connect(backup) as connection:
        assert (
            connection.execute("SELECT value FROM legacy_marker").fetchone()[0]
            == "before-migration"
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.user_version() == SCHEMA_VERSION


def test_importing_app_factory_does_not_create_default_database(tmp_path):
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "from meeting_workbench.main import create_app"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".meeting-workbench").exists()


def test_version_two_migration_replaces_legacy_minutes_html_with_safe_rendering(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO meetings(id, title, status) VALUES ('vm-safe', 'safe', 'published')"
        )
        connection.execute(
            """INSERT INTO minutes_versions
               (id, meeting_id, version_no, markdown, html, kind, published, created_at)
               VALUES ('mv-unsafe', 'vm-safe', 1, '# 纪要', '<script>fetch(\"//evil\")</script>',
                       'imported', 1, CURRENT_TIMESTAMP)"""
        )
        connection.execute("PRAGMA user_version=1")

    db.initialize()

    rendered = db.query_one("SELECT html FROM minutes_versions WHERE id='mv-unsafe'")["html"]
    assert "<script>" not in rendered
    assert "default-src 'none'" in rendered


def test_version_three_migration_adds_minutes_attempt_provenance(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE minutes_versions (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                markdown TEXT NOT NULL,
                html TEXT,
                kind TEXT NOT NULL DEFAULT 'generated',
                published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            PRAGMA user_version=2;
            """
        )

    db = Database(database_path)
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(minutes_versions)")}
    assert {
        "source_job_id",
        "source_attempt",
        "requested_stage",
        "input_transcript_sha256",
    } <= columns
    assert db.user_version() == SCHEMA_VERSION


def test_version_five_migration_adds_asr_quality_tables(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        gold_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(asr_gold_samples)")
        }
        run_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(asr_shadow_runs)")
        }

    assert {"asr_gold_samples", "asr_shadow_runs"} <= tables
    assert {
        "meeting_id",
        "segment_id",
        "start_ms",
        "end_ms",
        "reference",
        "entities_json",
        "numbers_json",
        "tags_json",
        "source_audio_sha256",
    } <= gold_columns
    assert {
        "meeting_id",
        "engine",
        "model",
        "state",
        "transcript_version_id",
        "audio_sha256",
        "config_json",
        "metrics_json",
    } <= run_columns
    assert db.user_version() == SCHEMA_VERSION


def test_version_six_migration_adds_qwen_owner_lease_and_heartbeat(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(asr_shadow_runs)")}

    assert {"owner_id", "lease_expires_at", "heartbeat_at"} <= columns
    assert db.user_version() == SCHEMA_VERSION


def test_version_nine_migration_backfills_manual_project_origin(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                recording_date TEXT,
                duration_ms INTEGER,
                status TEXT NOT NULL DEFAULT 'completed_unreviewed',
                project_id TEXT,
                canonical_dir TEXT,
                source_priority INTEGER NOT NULL DEFAULT 0,
                source_signature TEXT,
                current_transcript_version_id TEXT,
                current_minutes_version_id TEXT,
                conflict INTEGER NOT NULL DEFAULT 0,
                original_audio_sha256 TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO meetings(id, title, project_id) VALUES ('m-manual', '已归属会议', 'p1');
            INSERT INTO meetings(id, title, project_id) VALUES ('m-unassigned', '未归属会议', NULL);
            PRAGMA user_version=8;
            """
        )

    db = Database(database_path)
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        origins = dict(connection.execute("SELECT id, project_origin FROM meetings"))

    assert origins["m-manual"] == "manual"
    assert origins["m-unassigned"] is None
    assert db.user_version() == SCHEMA_VERSION


def test_version_ten_migration_backfills_project_id_from_matching_scope_name(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#667085',
                origin TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            );
            CREATE TABLE glossary_terms (
                id TEXT PRIMARY KEY,
                term TEXT NOT NULL UNIQUE,
                aliases TEXT NOT NULL DEFAULT '[]',
                scope TEXT NOT NULL DEFAULT '通用',
                category TEXT NOT NULL DEFAULT '其他',
                source TEXT NOT NULL DEFAULT 'manual',
                confirmed INTEGER NOT NULL DEFAULT 1,
                hit_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO projects(id, name, created_at) VALUES ('proj-mdt', 'MDT', '2026-01-01');
            INSERT INTO glossary_terms(id, term, scope, created_at, updated_at)
                VALUES ('gt-1', 'MDT专家', 'MDT', '2026-01-01', '2026-01-01');
            INSERT INTO glossary_terms(id, term, scope, created_at, updated_at)
                VALUES ('gt-2', '云图', '云图', '2026-01-01', '2026-01-01');
            PRAGMA user_version=9;
            """
        )

    db = Database(database_path)
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            row["id"]: dict(row)
            for row in connection.execute("SELECT id, scope, project_id FROM glossary_terms")
        }

    # scope 与项目名精确相等（MDT）的补上 project_id
    assert rows["gt-1"]["project_id"] == "proj-mdt"
    assert rows["gt-1"]["scope"] == "MDT"
    # 没有同名项目（云图）的保持 project_id 为空，scope 原样不动
    assert rows["gt-2"]["project_id"] is None
    assert rows["gt-2"]["scope"] == "云图"
    assert db.user_version() == SCHEMA_VERSION


def test_version_twelve_migration_adds_requirement_tables_and_task_column(tmp_path):
    """v11→v12：项目→需求→任务三层，新表 + tasks.requirement_id 只加不改。"""
    database_path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#667085',
                origin TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending_confirm',
                origin TEXT NOT NULL DEFAULT 'ai',
                assignee TEXT NOT NULL DEFAULT 'me',
                meeting_id TEXT,
                project_id TEXT,
                status_changed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO projects(id, name, created_at) VALUES ('proj-1', '存量项目', '2026-01-01');
            INSERT INTO tasks(id, title, status_changed_at, created_at, updated_at)
                VALUES ('task-1', '存量任务', '2026-01-01', '2026-01-01', '2026-01-01');
            PRAGMA user_version=11;
            """
        )

    db = Database(database_path)
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        # 存量数据原样保留，只加列不改行。
        task_row = dict(
            connection.execute("SELECT id, title, requirement_id FROM tasks WHERE id='task-1'").fetchone()
        )

    assert {"project_material_roots", "requirements", "requirement_folders", "requirement_meetings"} <= tables
    assert "requirement_id" in task_columns
    assert task_row == {"id": "task-1", "title": "存量任务", "requirement_id": None}
    assert db.user_version() == SCHEMA_VERSION

    # 需求名唯一索引：同项目同名（大小写/首尾空格不敏感）第二次插入应报冲突。
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
               VALUES ('req-1', 'proj-1', '需求名', 'P1', 'active', '2026-01-01', '2026-01-01')"""
        )
        try:
            connection.execute(
                """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
                   VALUES ('req-2', 'proj-1', '  需求名  ', 'P2', 'active', '2026-01-01', '2026-01-01')"""
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("同项目同名需求应该撞唯一索引")


def test_real_version_five_running_shadow_migrates_and_completes_idempotently(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "workbench-v5.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                recording_date TEXT,
                duration_ms INTEGER,
                status TEXT NOT NULL DEFAULT 'completed_unreviewed',
                project_id TEXT,
                canonical_dir TEXT,
                source_priority INTEGER NOT NULL DEFAULT 0,
                source_signature TEXT,
                source_job_id TEXT,
                current_transcript_version_id TEXT,
                current_minutes_version_id TEXT,
                conflict INTEGER NOT NULL DEFAULT 0,
                original_audio_sha256 TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE transcript_versions (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                kind TEXT NOT NULL,
                based_on_id TEXT,
                source_path TEXT,
                source_sha256 TEXT,
                published INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(meeting_id, version_no)
            );
            CREATE TABLE asr_shadow_runs (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL,
                engine TEXT NOT NULL,
                model TEXT NOT NULL,
                state TEXT NOT NULL,
                transcript_version_id TEXT,
                audio_sha256 TEXT,
                config_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            INSERT INTO meetings(id, title, status)
            VALUES ('vm-v5', 'v5 meeting', 'published');
            INSERT INTO asr_shadow_runs
                (id, meeting_id, engine, model, state, audio_sha256, config_json,
                 error, created_at, updated_at)
            VALUES ('shadow-v5', 'vm-v5', 'qwen3_asr', 'Qwen/Qwen3-ASR-0.6B',
                    'running', 'abc123', '{"legacy":true}', 'old-error',
                    '2026-07-01T00:00:00+00:00', '2026-07-01T00:01:00+00:00');
            PRAGMA user_version=5;
            """
        )

    db = Database(database_path)
    db.initialize()
    db.initialize()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(asr_shadow_runs)")}
        row = connection.execute(
            """SELECT id, meeting_id, state, audio_sha256, config_json, error,
                      owner_id, lease_expires_at, heartbeat_at
                 FROM asr_shadow_runs WHERE id='shadow-v5'"""
        ).fetchone()
        lease_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_leases'"
        ).fetchone()

    assert {"owner_id", "lease_expires_at", "heartbeat_at"} <= columns
    assert dict(row) == {
        "id": "shadow-v5",
        "meeting_id": "vm-v5",
        "state": "queued",
        "audio_sha256": "abc123",
        "config_json": '{"legacy":true}',
        "error": "租约信息不完整后重新排队",
        "owner_id": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
    }
    assert lease_table is not None
    assert db.user_version() == SCHEMA_VERSION

    archive_root = tmp_path / "archive"
    audio = archive_root / "vm-v5" / "vm-v5.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"v5-audio")
    audio_sha256 = hashlib.sha256(audio.read_bytes()).hexdigest()
    stat = audio.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
           VALUES ('vm-v5', 'audio', 'source', 'archive', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (str(audio), audio_sha256, stat.st_size, stat.st_mtime_ns),
    )
    db.execute(
        "UPDATE asr_shadow_runs SET audio_sha256=? WHERE id='shadow-v5'",
        (audio_sha256,),
    )
    binary = tmp_path / "bin" / "mlx-qwen3-asr"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=database_path,
        archive_root=archive_root,
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        qwen_binary=binary,
    )
    relay = SimpleNamespace(list_jobs=lambda **_kwargs: [])
    service = QwenShadowService(db, settings, relay)

    def successful_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        (output / "meeting.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nv5 恢复成功\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("meeting_workbench.qwen_shadow.subprocess.run", successful_run)

    assert service.recover_orphaned() == 0
    assert service.recover_orphaned() == 0
    assert service.run_once() is True
    assert db.query_one(
        "SELECT state, owner_id, transcript_version_id FROM asr_shadow_runs WHERE id='shadow-v5'"
    ) == {
        "state": "ready",
        "owner_id": None,
        "transcript_version_id": db.query_one(
            "SELECT id FROM transcript_versions WHERE kind='qwen_reference'"
        )["id"],
    }
