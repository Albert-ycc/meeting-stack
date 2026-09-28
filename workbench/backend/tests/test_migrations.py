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


V14_TABLES = (
    "requirement_name_decisions",
    "pending_project_folders",
    "folder_declines",
    "root_fingerprints",
    "meeting_file_mentions",
    "meeting_file_scan",
    "material_index_state",
    "material_dirs",
    "material_files",
)
V14_GRAPH_REV_TABLES = (
    "name_decisions",
    "requirement_name_decisions",
    "pending_project_folders",
    "meeting_file_mentions",
)
# v14 / 2d：建在旧表上、引用新表的触发器，退回 v13 时要先删。
V14_TRIGGERS = (
    "pending_project_folders_drop_on_mount",
    "meeting_file_scan_dirty_meeting",
    "meeting_file_scan_dirty_transcript_edit",
    "meeting_file_mentions_leave_project",
    "material_index_root_moved",
)


# v14 / 2b：project_links 上的新需求名、AI 当时选的项目、会上的叫法。
V14_LINK_COLUMNS = ("new_requirement_name", "new_name_project_id", "new_name_spoken")


# v15 / 3a：材料内容。触发器建在新表上，删表时一起删；material_files 上的新列和两个索引。
V15_TRIGGERS = (
    "material_chunks_fts_insert",
    "material_chunks_fts_delete",
    "material_chunks_fts_update",
)
V15_TABLES = (
    "deliverable_files",
    "material_media_jobs",
    "material_chunk_vectors",
    "material_chunks_fts",
    "material_chunks",
    "material_contents",
)
V15_FILE_INDEXES = ("idx_material_files_content", "idx_material_files_mtime")
V15_FILE_COLUMNS = (
    "content_key",
    "content_size",
    "content_mtime_ns",
    "content_error",
    "content_attempts",
    "content_checked_at",
)


# v16 / 4a：第四期。11 张新表，11 个手写触发器，三张表进关系图版本号（生成 9 个触发器），
# 24 个索引（新表上 22 个，meetings、requirement_meetings 上各 1 个），6 个 app_state 键。
V16_TABLES = (
    "relations",
    "decisions",
    "decision_scan",
    "mention_extractions",
    "meeting_related_scan",
    "meeting_windows",
    "meeting_window_passages",
    "material_file_events",
    "glossary_candidates",
    "glossary_mining_scan",
    "glossary_mining_seeds",
)
V16_TRIGGERS = (
    "relations_leave_project",
    "graph_rev_relations_insert",
    "graph_rev_relations_update",
    "graph_rev_relations_delete",
    "related_rev_relations_insert",
    "related_rev_relations_update",
    "related_rev_relations_delete",
    "material_file_events_insert",
    "material_file_events_update",
    "meeting_related_scan_dirty_meeting",
    "meeting_related_scan_dirty_edit",
)
V16_GRAPH_REV_TABLES = ("decisions", "deliverables", "deliverable_files")
V16_OLD_TABLE_INDEXES = ("idx_meetings_project", "idx_requirement_meetings_meeting")
V16_INDEXES = (
    "idx_decisions_meeting",
    "idx_decisions_requirement",
    "idx_decisions_text_key",
    "idx_decision_scan_pair",
    "idx_relations_project",
    "idx_relations_meeting",
    "idx_relations_task",
    "idx_relations_decision",
    "idx_relations_to_decision",
    "idx_relations_file",
    "idx_relations_content",
    "idx_relations_deliverable",
    "idx_mention_extractions_state",
    "idx_meeting_related_scan_dirty",
    "idx_meeting_window_passages_chunk",
    "idx_meeting_window_passages_content",
    "idx_material_file_events_root",
    "idx_material_file_events_file",
    "idx_material_file_events_day",
    "idx_material_file_events_sig",
    "idx_material_file_events_changed_day",
    "idx_glossary_candidates_status",
    *V16_OLD_TABLE_INDEXES,
)
V16_KEYS = (
    "links_since",
    "related_rev",
    "links_llm_usage",
    "links_housekeeping_at",
    "related_chunk_mark",
    "card_index_paths",
)


def _downgrade_to_v15(connection: sqlite3.Connection) -> None:
    """把刚建好的 v16 库退回 v15 的形状：先删新触发器（文件流水的触发器会挡住删 content_key 列），
    再删新表，再删老表上的两个新索引和六个键。"""
    for trigger in V16_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in V16_GRAPH_REV_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS graph_rev_{table}_{action}")
    for table in V16_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for index in V16_OLD_TABLE_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    connection.execute(
        f"DELETE FROM app_state WHERE key IN ({', '.join('?' for _ in V16_KEYS)})", V16_KEYS
    )
    connection.execute("PRAGMA user_version=15")


def _downgrade_to_v14(connection: sqlite3.Connection) -> None:
    """把刚建好的 v15 库退回 v14 的形状：先删新触发器，再删新表，再删两个新索引，最后删新列
    （material_files 上六个、material_dirs 上一个）。"""
    _downgrade_to_v15(connection)
    for trigger in V15_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in V15_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for index in V15_FILE_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    for column in V15_FILE_COLUMNS:
        connection.execute(f"ALTER TABLE material_files DROP COLUMN {column}")
    connection.execute("ALTER TABLE material_dirs DROP COLUMN symlinks")
    connection.execute("DELETE FROM app_state WHERE key='ocr_engine'")
    connection.execute("PRAGMA user_version=14")


def _downgrade_to_v13(connection: sqlite3.Connection) -> None:
    """把刚建好的 v14 库退回 v13 的形状：先删新触发器（它们引用新表），再删新表。"""
    _downgrade_to_v14(connection)
    for table in V14_GRAPH_REV_TABLES:
        for action in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS graph_rev_{table}_{action}")
    for trigger in V14_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in V14_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for column in V14_LINK_COLUMNS:
        connection.execute(f"ALTER TABLE project_links DROP COLUMN {column}")
    connection.execute("PRAGMA user_version=13")


def _downgrade_to_v12(connection: sqlite3.Connection) -> None:
    """把刚建好的库退回 v12 的形状：先退到 v13，再去掉 v13 新增的表、虚表和触发器。"""
    _downgrade_to_v13(connection)
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'graph_rev_%'"
    ).fetchall():
        connection.execute(f"DROP TRIGGER {name}")
    connection.execute("DROP TABLE IF EXISTS app_state")
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS minutes_fts_after_meeting_insert;
        DROP TRIGGER IF EXISTS minutes_fts_after_minutes_pointer_update;
        DROP TRIGGER IF EXISTS minutes_fts_after_meeting_delete;
        DROP TABLE IF EXISTS minutes_fts;
        DROP TABLE IF EXISTS name_decisions;
        ALTER TABLE projects DROP COLUMN also_names;
        ALTER TABLE project_links DROP COLUMN evidence_json;
        ALTER TABLE project_links DROP COLUMN candidates_json;
        ALTER TABLE project_links DROP COLUMN new_project_name;
        ALTER TABLE project_links DROP COLUMN reason;
        ALTER TABLE glossary_terms DROP COLUMN also;
        ALTER TABLE glossary_terms DROP COLUMN is_cue;
        PRAGMA user_version=12;
        """
    )


def _seed_v12_fixture(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        INSERT INTO projects(id, name, created_at) VALUES ('p-a', '云图AI', '2026-09-01');
        INSERT INTO meetings(id, title, status, project_id, project_origin)
        VALUES ('m-a', '云图周会', 'published', 'p-a', 'ai'),
               ('m-none', '内部例会', 'published', NULL, NULL);
        INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, kind, created_at)
        VALUES ('mv-a1', 'm-a', 1, '旧纪要：树立协会', 'generated', '2026-09-01'),
               ('mv-a2', 'm-a', 2, '当前纪要：数理协会 初审规则', 'draft', '2026-09-02'),
               ('mv-n1', 'm-none', 1, '例会纪要：排期', 'generated', '2026-09-01');
        UPDATE meetings SET current_minutes_version_id='mv-a2' WHERE id='m-a';
        UPDATE meetings SET current_minutes_version_id='mv-n1' WHERE id='m-none';
        INSERT INTO requirements(id, project_id, title, priority, created_at, updated_at)
        VALUES ('r-a', 'p-a', '初审规则', 'P0', '2026-09-01', '2026-09-01');
        INSERT INTO tasks(id, title, status, meeting_id, project_id, requirement_id,
                          status_changed_at, created_at, updated_at)
        VALUES
          ('t-draft', '草稿', 'pending_confirm', 'm-a', NULL, NULL, 'x', 'x', 'x'),
          ('t-expired', '过期', 'expired', 'm-a', NULL, NULL, 'x', 'x', 'x'),
          ('t-confirmed', '已确认且人工清空', 'confirmed', 'm-a', NULL, NULL, 'x', 'x', 'x'),
          ('t-other', '别的项目', 'pending_confirm', 'm-a', NULL, 'r-a', 'x', 'x', 'x'),
          ('t-none', '会没项目', 'pending_confirm', 'm-none', NULL, NULL, 'x', 'x', 'x');
        INSERT INTO glossary_terms(id, term, scope, project_id, created_at, updated_at)
        VALUES ('g-orphan', '初审规则', '云图AI', NULL, 'x', 'x'),
               ('g-public', '数理协会', '通用', NULL, 'x', 'x');
        """
    )


def test_version_thirteen_migration_backfills_tasks_glossary_and_minutes_index(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        _downgrade_to_v12(connection)
        _seed_v12_fixture(connection)
    backups: list[int] = []

    db.initialize(before_migrate=lambda: backups.append(db.user_version()))

    assert backups == [12]
    assert db.user_version() == SCHEMA_VERSION
    columns = {
        table: {row["name"] for row in db.query_all(f"PRAGMA table_info({table})")}
        for table in ("projects", "project_links", "glossary_terms")
    }
    assert "also_names" in columns["projects"]
    assert {"evidence_json", "candidates_json", "new_project_name", "reason"} <= columns["project_links"]
    assert {"also", "is_cue"} <= columns["glossary_terms"]
    assert db.query_one(
        "SELECT also_names FROM projects WHERE id='p-a'"
    ) == {"also_names": "[]"}
    assert db.query_one(
        "SELECT also, is_cue FROM glossary_terms WHERE id='g-orphan'"
    ) == {"also": "[]", "is_cue": 1}
    tasks = {
        row["id"]: row["project_id"]
        for row in db.query_all("SELECT id, project_id FROM tasks")
    }
    assert tasks == {
        "t-draft": "p-a",
        "t-expired": "p-a",
        "t-confirmed": None,
        "t-other": None,
        "t-none": None,
    }
    glossary = {
        row["id"]: row["project_id"]
        for row in db.query_all("SELECT id, project_id FROM glossary_terms")
    }
    assert glossary == {"g-orphan": "p-a", "g-public": None}
    indexed = db.query_all("SELECT meeting_id, text FROM minutes_fts ORDER BY meeting_id")
    assert indexed == [
        {"meeting_id": "m-a", "text": "当前纪要：数理协会 初审规则"},
        {"meeting_id": "m-none", "text": "例会纪要：排期"},
    ]
    assert [
        row["meeting_id"]
        for row in db.query_all(
            "SELECT meeting_id FROM minutes_fts WHERE minutes_fts MATCH ?", ('"数理协会"',)
        )
    ] == ["m-a"]


def test_version_thirteen_migration_is_idempotent(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        _downgrade_to_v12(connection)
        _seed_v12_fixture(connection)

    def snapshot() -> dict[str, list[dict]]:
        return {
            "tasks": db.query_all("SELECT id, project_id, status FROM tasks ORDER BY id"),
            "glossary": db.query_all("SELECT id, project_id FROM glossary_terms ORDER BY id"),
            "fts": db.query_all("SELECT meeting_id, text FROM minutes_fts ORDER BY meeting_id"),
        }

    db.initialize()
    first = snapshot()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version=12")
    db.initialize()
    db.initialize()

    assert snapshot() == first
    assert db.user_version() == SCHEMA_VERSION


def test_minutes_index_follows_the_current_minutes_pointer(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('m-1', '周会', 'published')")
    db.execute(
        """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, kind, created_at)
           VALUES ('mv-1', 'm-1', 1, '第一版 灰度方案', 'generated', 'x'),
                  ('mv-2', 'm-1', 2, '第二版 全量上线', 'draft', 'x')"""
    )

    def hits(term: str) -> list[str]:
        return [
            row["meeting_id"]
            for row in db.query_all(
                "SELECT meeting_id FROM minutes_fts WHERE minutes_fts MATCH ?", (f'"{term}"',)
            )
        ]

    assert hits("灰度方案") == []
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-1' WHERE id='m-1'")
    assert hits("灰度方案") == ["m-1"]
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-2' WHERE id='m-1'")
    assert hits("灰度方案") == []
    assert hits("全量上线") == ["m-1"]
    db.execute("UPDATE meetings SET title='改名' WHERE id='m-1'")
    assert db.query_one("SELECT COUNT(*) AS n FROM minutes_fts")["n"] == 1
    db.execute("DELETE FROM meetings WHERE id='m-1'")
    assert hits("全量上线") == []


def _tables(db: Database) -> set[str]:
    return {
        row["name"]
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")
    }


def test_version_fourteen_migration_adds_tables_and_keeps_data(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        _downgrade_to_v13(connection)
        connection.executescript(
            """
            INSERT INTO projects(id, name, created_at) VALUES ('p-a', '云图AI', '2026-09-01');
            INSERT INTO project_material_roots(project_id, path, created_at)
            VALUES ('p-a', '/Volumes/资料盘/项目/云图AI', '2026-09-01');
            INSERT INTO name_decisions(norm_key, name, decision, decided_at)
            VALUES ('云图二期', '云图二期', 'ignored', '2026-09-01');
            INSERT INTO meetings(id, title, status, created_at, updated_at)
            VALUES ('m-1', '周会', 'completed_unreviewed', '2026-09-01', '2026-09-01');
            INSERT INTO project_links(meeting_id, minutes_version_id, status, new_project_name, created_at)
            VALUES ('m-1', 'mv-1', 'unresolved', '数据中台', '2026-09-01');
            """
        )
    assert not set(V14_TABLES) & _tables(db)
    backups: list[int] = []

    db.initialize(before_migrate=lambda: backups.append(db.user_version()))

    assert backups == [13]
    assert db.user_version() == SCHEMA_VERSION == 16
    assert set(V14_TABLES) <= _tables(db)
    assert db.query_one("SELECT name FROM projects WHERE id='p-a'") == {"name": "云图AI"}
    assert db.query_one("SELECT decision FROM name_decisions") == {"decision": "ignored"}
    assert db.query_one(
        """SELECT new_project_name, new_requirement_name, new_name_project_id, new_name_spoken
             FROM project_links"""
    ) == {
        "new_project_name": "数据中台",
        "new_requirement_name": None,
        "new_name_project_id": None,
        "new_name_spoken": "[]",
    }
    # 再跑一遍什么都不变
    db.initialize()
    db.initialize()
    assert db.user_version() == 16
    assert db.query_one("SELECT COUNT(*) AS n FROM project_material_roots") == {"n": 1}


def test_version_fifteen_migration_adds_material_content_and_keeps_data(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        _downgrade_to_v14(connection)
        connection.executescript(
            """
            INSERT INTO projects(id, name, created_at) VALUES ('p-a', '云图AI', '2026-09-01');
            INSERT INTO project_material_roots(project_id, path, created_at)
            VALUES ('p-a', '/Volumes/资料盘/项目/云图AI', '2026-09-01');
            INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, size, mtime_ns, seen_at)
            VALUES (1, '报价单.xlsx', '', '报价单.xlsx', '报价单', '报价单', 10, 20, '2026-09-01');
            """
        )
    assert not set(V15_TABLES) & _tables(db)
    backups: list[int] = []

    db.initialize(before_migrate=lambda: backups.append(db.user_version()))

    assert backups == [14]
    assert db.user_version() == SCHEMA_VERSION == 16
    assert set(V15_TABLES) | set(V15_TRIGGERS) <= _tables(db)
    columns = {row["name"] for row in db.query_all("PRAGMA table_info(material_files)")}
    assert set(V15_FILE_COLUMNS) <= columns
    indexes = {row["name"] for row in db.query_all("PRAGMA index_list(material_files)")}
    assert set(V15_FILE_INDEXES) <= indexes
    assert db.query_one("SELECT name, size, content_key FROM material_files") == {
        "name": "报价单.xlsx",
        "size": 10,
        "content_key": None,
    }
    assert db.query_one("SELECT value FROM app_state WHERE key='ocr_engine'") == {"value": "auto"}
    db.initialize()
    db.initialize()
    assert db.user_version() == 16
    assert db.query_one("SELECT COUNT(*) AS n FROM material_files") == {"n": 1}


def test_material_chunks_keep_the_full_text_index_in_sync(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        """INSERT INTO material_contents(content_key, layer, created_at, updated_at)
           VALUES ('q2:a', 'text', 'x', 'x')"""
    )
    db.execute("INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('q2:a', 0, '报价单第一版')")
    db.execute("INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('q2:a', 1, '排期表')")

    def hits(word):
        return [
            row["rowid"]
            for row in db.query_all(
                "SELECT rowid FROM material_chunks_fts WHERE material_chunks_fts MATCH ?", (f'"{word}"',)
            )
        ]

    assert len(hits("报价单")) == 1
    db.execute("UPDATE material_chunks SET text='报价单第二版' WHERE ordinal=0")
    assert len(hits("第二版")) == 1 and hits("第一版") == []
    first_id = db.query_one("SELECT MAX(id) AS id FROM material_chunks")["id"]
    db.execute("DELETE FROM material_contents")  # 级联删片段，触发器同步删全文索引
    assert hits("报价单") == [] and hits("排期表") == []
    # id 只增不复用
    db.execute(
        """INSERT INTO material_contents(content_key, layer, created_at, updated_at)
           VALUES ('q2:b', 'text', 'x', 'x')"""
    )
    db.execute("INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('q2:b', 0, '新的')")
    assert db.query_one("SELECT id FROM material_chunks")["id"] > first_id
    db.execute("PRAGMA integrity_check")
    assert db.query_one("SELECT COUNT(*) AS n FROM material_chunks_fts")["n"] == 1


def test_version_fourteen_tables_bump_the_graph_revision(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")

    def rev() -> int:
        row = db.query_one("SELECT value FROM app_state WHERE key='graph_rev'")
        return int(row["value"]) if row else 0

    before = rev()
    db.execute(
        """INSERT INTO pending_project_folders(project_id, parent, created_at)
           VALUES ('p', '/Volumes/资料盘/项目', 'x')"""
    )
    db.execute(
        """INSERT INTO requirement_name_decisions(project_id, name_key, name, decision, decided_at)
           VALUES ('p', '数据看板', '数据看板', 'ignored', 'x')"""
    )
    db.execute(
        "INSERT INTO name_decisions(norm_key, name, decision, decided_at) VALUES ('k', 'k', 'ignored', 'x')"
    )
    assert rev() == before + 3
    # 后台写的指纹和拒绝记录不进版本号，免得每轮刷新都让关系图缓存失效
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/x/云图AI', 'x')"
    )
    after_root = rev()
    db.execute("INSERT INTO root_fingerprints(root_id, child_names, updated_at) VALUES (1, '[]', 'x')")
    db.execute(
        "INSERT INTO folder_declines(kind, scope, path, decided_at) VALUES ('unclaimed', '', '/x/资料', 'x')"
    )
    assert rev() == after_root
    # 2d：文件名索引每扫一轮都会写，也不进版本号；会上提到的文件进
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, seen_at)
           VALUES (1, '报价单.xlsx', '', '报价单.xlsx', '报价单', '报价单', 'x')"""
    )
    db.execute("INSERT INTO material_dirs(root_id, dir_rel, listed_at) VALUES (1, '', 'x')")
    db.execute("INSERT INTO material_index_state(root_id) VALUES (1)")
    db.execute("INSERT INTO meetings(id, title) VALUES ('m', '周会')")
    db.execute("INSERT INTO meeting_file_scan(meeting_id) VALUES ('m')")
    assert rev() == after_root + 1  # 插入会议本身
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, updated_at)
           VALUES ('m', 'p', '报价单', 1, '报价单', 'x')"""
    )
    assert rev() == after_root + 2
    # 3a：材料内容那几张表都是后台在写，不进版本号
    marker = rev()
    db.execute("UPDATE material_files SET content_key='q2:a', content_size=1, content_mtime_ns=1")
    db.execute(
        """INSERT INTO material_contents(content_key, layer, created_at, updated_at)
           VALUES ('q2:a', 'text', 'x', 'x')"""
    )
    db.execute("INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('q2:a', 0, '片段')")
    db.execute("INSERT INTO material_media_jobs(content_key, updated_at) VALUES ('q2:a', 'x')")
    assert rev() == marker


def test_mounting_a_folder_drops_the_pending_folder(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute(
        """INSERT INTO pending_project_folders(project_id, parent, created_at)
           VALUES ('p', '/Volumes/资料盘/项目', 'x')"""
    )
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/x/云图AI', 'x')"
    )
    assert db.query_one("SELECT COUNT(*) AS n FROM pending_project_folders") == {"n": 0}
    # 删项目时待办和指纹跟着删
    db.execute(
        """INSERT INTO pending_project_folders(project_id, parent, created_at)
           VALUES ('p', '/Volumes/资料盘/项目', 'x')"""
    )
    db.execute("INSERT INTO root_fingerprints(root_id, child_names, updated_at) VALUES (1, '[]', 'x')")
    db.execute("DELETE FROM projects WHERE id='p'")
    assert db.query_one("SELECT COUNT(*) AS n FROM pending_project_folders") == {"n": 0}
    assert db.query_one("SELECT COUNT(*) AS n FROM root_fingerprints") == {"n": 0}


def _schema_objects(db: Database) -> dict[str, set[str]]:
    objects: dict[str, set[str]] = {"table": set(), "trigger": set(), "index": set()}
    for row in db.query_all(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger', 'index')"
    ):
        objects[row["type"]].add(row["name"])
    return objects


def _v16_generated_triggers() -> set[str]:
    return {
        f"graph_rev_{table}_{action}"
        for table in V16_GRAPH_REV_TABLES
        for action in ("insert", "update", "delete")
    }


def test_version_sixteen_migration_adds_tables_and_keeps_data(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        _downgrade_to_v15(connection)
        connection.executescript(
            """
            INSERT INTO projects(id, name, created_at) VALUES ('p-a', '云图AI', '2026-09-01');
            INSERT INTO meetings(id, title, status, project_id, created_at, updated_at)
            VALUES ('m-1', '周会', 'published', 'p-a', '2026-09-01', '2026-09-01');
            INSERT INTO project_material_roots(project_id, path, created_at)
            VALUES ('p-a', '/Volumes/资料盘/项目/云图AI', '2026-09-01');
            INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, size, mtime_ns, seen_at)
            VALUES (1, '报价单.xlsx', '', '报价单.xlsx', '报价单', '报价单', 10, 20, '2026-09-01');
            """
        )
    objects = _schema_objects(db)
    assert not set(V16_TABLES) & objects["table"]
    assert not (set(V16_TRIGGERS) | _v16_generated_triggers()) & objects["trigger"]
    assert not set(V16_INDEXES) & objects["index"]
    backups: list[int] = []

    db.initialize(before_migrate=lambda: backups.append(db.user_version()))

    assert backups == [15]
    assert db.user_version() == SCHEMA_VERSION == 16
    objects = _schema_objects(db)
    assert set(V16_TABLES) <= objects["table"]
    assert len(V16_TABLES) == 11
    assert set(V16_TRIGGERS) <= objects["trigger"] and len(V16_TRIGGERS) == 11
    assert _v16_generated_triggers() <= objects["trigger"] and len(_v16_generated_triggers()) == 9
    assert set(V16_INDEXES) <= objects["index"] and len(V16_INDEXES) == 24
    keys = {
        row["key"]: row["value"]
        for row in db.query_all("SELECT key, value FROM app_state WHERE key IN ('links_since', 'related_rev')")
    }
    assert keys["related_rev"] == "0" and keys["links_since"]
    # 不回填：老数据原样，新表都是空的
    assert db.query_one("SELECT project_id FROM meetings WHERE id='m-1'") == {"project_id": "p-a"}
    assert db.query_one("SELECT name FROM material_files") == {"name": "报价单.xlsx"}
    for table in V16_TABLES:
        assert db.query_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 0}
    # 再跑两遍什么都不变，links_since 写一次、之后不改
    snapshot = db.query_all("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    db.initialize()
    db.initialize()
    assert db.user_version() == 16
    assert db.query_all("SELECT type, name, sql FROM sqlite_master ORDER BY type, name") == snapshot
    assert db.query_one("SELECT value FROM app_state WHERE key='links_since'") == {
        "value": keys["links_since"]
    }


def test_version_sixteen_rollback_marker_keeps_objects(tmp_path):
    """回滚是改 user_version=15（不删新表、新触发器和新索引）；回到 v16 时对象和行都在，
    迁移前备份再做一次。"""
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute("INSERT INTO meetings(id, title, project_id) VALUES ('m', '周会', 'p')")
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, stem_key,
                                 created_at, updated_at)
           VALUES ('mention', 'p', 'm|报价单', 'rejected', 'llm', 'm', '报价单', 'x', 'x')"""
    )
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, created_at, updated_at)
           VALUES ('dec-0123456789abcdef', 'm', 0, '阈值先按 0.8 执行', '阈值先按08执行', 'x', 'x')"""
    )
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/x/云图AI', 'x')"
    )
    db.execute(
        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, day, at)
           VALUES (1, 7, '报价单.xlsx', '', 'added', '2026-09-27', '2026-09-27T01:00:00.000Z')"""
    )
    before = db.query_all("SELECT type, name FROM sqlite_master ORDER BY type, name")
    since = db.query_one("SELECT value FROM app_state WHERE key='links_since'")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version=15")
    backups: list[int] = []

    db.initialize(before_migrate=lambda: backups.append(db.user_version()))

    assert backups == [15]
    assert db.user_version() == 16
    assert db.query_all("SELECT type, name FROM sqlite_master ORDER BY type, name") == before
    assert db.query_one("SELECT status FROM relations") == {"status": "rejected"}
    assert db.query_one("SELECT text FROM decisions") == {"text": "阈值先按 0.8 执行"}
    assert db.query_one("SELECT COUNT(*) AS n FROM material_file_events") == {"n": 1}
    assert db.query_one("SELECT value FROM app_state WHERE key='links_since'") == since
    # 比代码新的库照旧拒绝打开
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version=17")
    try:
        db.initialize()
    except sqlite3.DatabaseError as error:
        assert "database version 17 is newer than supported 16" in str(error)
    else:
        raise AssertionError("v17 的库不该被打开")


def test_version_sixteen_graph_rev_policy(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute("INSERT INTO meetings(id, title, project_id) VALUES ('m', '周会', 'p')")
    db.execute(
        """INSERT INTO tasks(id, title, status, meeting_id, project_id, status_changed_at,
                             created_at, updated_at)
           VALUES ('t', '整理报价单', 'confirmed', 'm', 'p', 'x', 'x', 'x')"""
    )
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/x/云图AI', 'x')"
    )
    db.execute(
        """INSERT INTO material_contents(content_key, layer, created_at, updated_at)
           VALUES ('q2:a', 'text', 'x', 'x')"""
    )
    db.execute("INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('q2:a', 0, '片段')")

    def revs() -> tuple[int, int]:
        rows = {
            row["key"]: int(row["value"])
            for row in db.query_all(
                "SELECT key, value FROM app_state WHERE key IN ('graph_rev', 'related_rev')"
            )
        }
        return rows.get("graph_rev", 0), rows.get("related_rev", 0)

    def relation(kind: str, ident: str, status: str = "shown") -> None:
        db.execute(
            """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id,
                                     content_key, score, created_at, updated_at)
               VALUES (?, 'p', ?, ?, 'rule', 'm', 'q2:a', 0.5, 'x', 'x')""",
            (kind, ident, status),
        )

    graph, related = revs()
    # 关联表：非相关的行给 graph_rev 加一，相关的行只给 related_rev 加一
    relation("produced", "t|q2:a", "suggested")
    assert revs() == (graph + 1, related)
    db.execute("UPDATE relations SET status='confirmed' WHERE kind='produced'")
    db.execute("DELETE FROM relations WHERE kind='produced'")
    assert revs() == (graph + 3, related)
    relation("related", "m|q2:a")
    db.execute("UPDATE relations SET score=0.61 WHERE kind='related'")
    db.execute("DELETE FROM relations WHERE kind='related'")
    assert revs() == (graph + 3, related + 3)
    # 决议、交付物、交付物文件进关系图版本号
    graph, related = revs()
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, created_at, updated_at)
           VALUES ('dec-0000000000000001', 'm', 0, '先做报价', '先做报价', 'x', 'x')"""
    )
    db.execute(
        "INSERT INTO deliverables(task_id, kind, url, created_at) VALUES ('t', 'file', 'file:///x', 'x')"
    )
    db.execute(
        """INSERT INTO deliverable_files(deliverable_id, content_key, root_id, rel_path)
           VALUES (1, 'q2:a', 1, '报价单.xlsx')"""
    )
    assert revs() == (graph + 3, related)
    # 生成的触发器在 UPDATE 什么都没改时也会触发，所以写入都要带 IS NOT 条件
    db.execute("UPDATE decisions SET text = text")
    assert revs() == (graph + 4, related)
    # 台账、派生表、文件流水、待确认的词都不加
    graph, related = revs()
    db.execute(
        "INSERT INTO decision_scan(meeting_id, updated_at) VALUES ('m', 'x')"
    )
    db.execute("UPDATE decision_scan SET pair_state='pending'")
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, created_at, updated_at)
           VALUES ('m', 'v', 's', 'x', 'x')"""
    )
    db.execute("INSERT INTO meeting_related_scan(meeting_id) VALUES ('m')")
    db.execute("INSERT INTO glossary_mining_scan(project_id, sig, mined_at) VALUES ('p', 's', 'x')")
    db.execute(
        """INSERT INTO meeting_windows(meeting_id, model, start_ms, end_ms, text_sha, chars, bar, vector)
           VALUES ('m', 'bge', 0, 90000, 's', 10, 0.6, x'00')"""
    )
    db.execute(
        """INSERT INTO meeting_window_passages(meeting_id, start_ms, rank, chunk_id, content_key,
                                               ordinal, score, seg_ms)
           VALUES ('m', 0, 0, 1, 'q2:a', 0, 0.7, 0)"""
    )
    db.execute(
        """INSERT INTO glossary_mining_seeds(content_key, miner, source_sig, terms_json, mined_at)
           VALUES ('q2:a', 1, 's', '[]', 'x')"""
    )
    db.execute(
        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, day, at)
           VALUES (1, 7, '报价单.xlsx', '', 'added', '2026-09-27', '2026-09-27T01:00:00.000Z')"""
    )
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, created_at, updated_at)
           VALUES ('p', '海藻酸', '海藻酸', 'x', 'x')"""
    )
    db.execute("UPDATE glossary_candidates SET status='accepted'")
    assert revs() == (graph, related)


def test_version_sixteen_triggers_follow_meetings(tmp_path):
    """会议离开项目只删系统行（连同决议两端的标记），你的回答和手动换过的行留着；换项目、换逐字稿、
    原地改逐字稿都给相关记一笔要重算。"""
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', 'x')")
    db.execute("INSERT INTO meetings(id, title, project_id) VALUES ('m', '周会', 'p')")
    db.execute("INSERT INTO meetings(id, title, project_id) VALUES ('n', '复盘', 'p')")
    for decision_id, meeting_id in (("dec-000000000000000a", "m"), ("dec-000000000000000b", "n")):
        db.execute(
            """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, created_at, updated_at)
               VALUES (?, ?, 0, '先做报价', '先做报价', 'x', 'x')""",
            (decision_id, meeting_id),
        )
    rows = (
        ("mention", "m|a", "shown", "llm", "m", None, None),
        ("mention", "m|b", "shown", "manual", "m", None, None),
        ("mention", "m|c", "rejected", "llm", "m", None, None),
        ("related", "m|q2:a", "cleared", "vector", "m", None, None),
        ("later_changed", "dec-000000000000000b|dec-000000000000000a", "shown", "llm", "m",
         "dec-000000000000000b", "dec-000000000000000a"),
        ("restated", "dec-000000000000000a|dec-000000000000000b", "shown", "llm", "n",
         "dec-000000000000000a", "dec-000000000000000b"),
        ("mention", "n|a", "shown", "llm", "n", None, None),
    )
    for kind, ident, status, origin, meeting_id, decision_id, to_decision_id in rows:
        db.execute(
            """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id,
                                     decision_id, to_decision_id, created_at, updated_at)
               VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'x', 'x')""",
            (kind, ident, status, origin, meeting_id, decision_id, to_decision_id),
        )
    db.execute("DELETE FROM meeting_related_scan")

    db.execute("UPDATE meetings SET project_id='q' WHERE id='m'")

    assert {row["ident"] for row in db.query_all("SELECT ident FROM relations")} == {
        "m|b",
        "m|c",
        "n|a",
    }
    assert db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id='m'") == {"dirty": 1}
    db.execute("UPDATE meetings SET current_transcript_version_id='tv-2' WHERE id='m'")
    db.add_event("segment_split", meeting_id="m")
    db.add_event("minutes_saved", meeting_id="m")
    assert db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id='m'") == {"dirty": 3}
    # 启动时改 conflict 不算
    db.execute("UPDATE meetings SET conflict=0")
    assert db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id='m'") == {"dirty": 3}
