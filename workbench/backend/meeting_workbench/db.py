from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 12

CONFLICT_KINDS = {
    "external_source_change",
    "audio_integrity",
    "publish_recovery",
    "publish_post_commit",
    "legacy_unknown",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def escape_like_pattern(value: str) -> str:
    """转义 LIKE 通配符，供 `... LIKE ? ESCAPE '\\\\'` 场景使用；main/tasks/requirements 三处共用。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def dedupe_preserve_order(values: list[str]) -> list[str]:
    """去重且保留首次出现的顺序（D24）；批量 id 类入参（task_ids/meeting_ids/requirement_ids）
    在 main/requirements 三处共用这一份实现。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#667085',
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    recording_date TEXT,
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'completed_unreviewed',
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    project_origin TEXT,
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

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'source',
    source_root TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_meeting ON artifacts(meeting_id);

CREATE TABLE IF NOT EXISTS transcript_versions (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    based_on_id TEXT REFERENCES transcript_versions(id),
    source_path TEXT,
    source_sha256 TEXT,
    published INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(meeting_id, version_no)
);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES transcript_versions(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_label TEXT,
    speaker_name TEXT,
    text TEXT NOT NULL,
    UNIQUE(version_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_segments_version_ordinal ON segments(version_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    segment_id UNINDEXED,
    meeting_id UNINDEXED,
    version_id UNINDEXED,
    text,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS speakers (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    display_name TEXT,
    UNIQUE(meeting_id, label)
);

CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#667085',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_tags (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(meeting_id, tag_id)
);

CREATE TABLE IF NOT EXISTS minutes_versions (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    based_on_id TEXT REFERENCES minutes_versions(id),
    markdown TEXT NOT NULL,
    html TEXT,
    kind TEXT NOT NULL DEFAULT 'generated',
    published INTEGER NOT NULL DEFAULT 0,
    content_sha256 TEXT,
    source_job_id TEXT,
    source_attempt INTEGER,
    requested_stage TEXT,
    input_transcript_sha256 TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(meeting_id, version_no)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT REFERENCES meetings(id) ON DELETE CASCADE,
    job_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_conflicts (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN (
        'external_source_change', 'audio_integrity', 'publish_recovery',
        'publish_post_commit', 'legacy_unknown'
    )),
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    source_signature TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    resolution TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_conflicts_one_open_kind
    ON meeting_conflicts(meeting_id, kind) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_meeting_conflicts_meeting_status
    ON meeting_conflicts(meeting_id, status, created_at);

CREATE TRIGGER IF NOT EXISTS meeting_conflicts_after_insert
AFTER INSERT ON meeting_conflicts
BEGIN
    UPDATE meetings
       SET conflict = EXISTS(
           SELECT 1 FROM meeting_conflicts
            WHERE meeting_id = NEW.meeting_id AND status = 'open'
       )
     WHERE id = NEW.meeting_id;
END;

CREATE TRIGGER IF NOT EXISTS meeting_conflicts_after_update
AFTER UPDATE OF status, meeting_id ON meeting_conflicts
BEGIN
    UPDATE meetings
       SET conflict = EXISTS(
           SELECT 1 FROM meeting_conflicts
            WHERE meeting_id = OLD.meeting_id AND status = 'open'
       )
     WHERE id = OLD.meeting_id;
    UPDATE meetings
       SET conflict = EXISTS(
           SELECT 1 FROM meeting_conflicts
            WHERE meeting_id = NEW.meeting_id AND status = 'open'
       )
     WHERE id = NEW.meeting_id;
END;

CREATE TRIGGER IF NOT EXISTS meeting_conflicts_after_delete
AFTER DELETE ON meeting_conflicts
BEGIN
    UPDATE meetings
       SET conflict = EXISTS(
           SELECT 1 FROM meeting_conflicts
            WHERE meeting_id = OLD.meeting_id AND status = 'open'
       )
     WHERE id = OLD.meeting_id;
END;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    meeting_id TEXT REFERENCES meetings(id) ON DELETE SET NULL,
    state TEXT NOT NULL,
    active_attempt_id TEXT,
    stop_after_stage INTEGER NOT NULL DEFAULT 0,
    failure_stage TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    start_stage TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS embeddings (
    segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(segment_id, model)
);

CREATE TABLE IF NOT EXISTS fingerprint_cache (
    path TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    pcm_sha256 TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asr_gold_samples (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    segment_id TEXT REFERENCES segments(id) ON DELETE SET NULL,
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
    reference TEXT NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '[]',
    numbers_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_audio_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asr_gold_samples_meeting_start
    ON asr_gold_samples(meeting_id, start_ms, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_asr_gold_samples_segment
    ON asr_gold_samples(meeting_id, segment_id) WHERE segment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS asr_shadow_runs (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'queued', 'running', 'ready', 'failed', 'cancelled', 'unavailable'
    )),
    transcript_version_id TEXT REFERENCES transcript_versions(id) ON DELETE SET NULL,
    audio_sha256 TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT,
    error TEXT,
    owner_id TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_asr_shadow_runs_meeting_created
    ON asr_shadow_runs(meeting_id, created_at DESC);

CREATE TABLE IF NOT EXISTS runtime_leases (
    name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES asr_shadow_runs(id) ON DELETE CASCADE,
    heartbeat_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_confirm',
    origin TEXT NOT NULL DEFAULT 'ai',
    assignee TEXT NOT NULL DEFAULT 'me',
    meeting_id TEXT REFERENCES meetings(id) ON DELETE SET NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    extraction_id INTEGER,
    anchor_ms INTEGER,
    anchor_quote TEXT,
    suggested_project_name TEXT,
    status_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_meeting ON tasks(meeting_id);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);

-- v11：用户确认「知道了」的失败/归档未完成任务。job_updated_at 记确认时任务的更新时间，
-- 任务之后又有变化（重试后再次失败）就不再算已确认，会重新出现在「需要处理」里。
CREATE TABLE IF NOT EXISTS job_acknowledgements (
    job_id TEXT PRIMARY KEY,
    job_updated_at TEXT NOT NULL DEFAULT '',
    acknowledged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliverables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliverables_task ON deliverables(task_id);

CREATE TABLE IF NOT EXISTS task_extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    minutes_version_id TEXT NOT NULL,
    supplement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    raw_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_extraction_unique
    ON task_extractions(meeting_id, minutes_version_id, supplement);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_key TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_unique ON notifications(kind, ref_key);

CREATE TABLE IF NOT EXISTS project_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    minutes_version_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    method TEXT,
    project_id TEXT,
    raw_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_links_unique
    ON project_links(meeting_id, minutes_version_id);

CREATE TABLE IF NOT EXISTS glossary_terms (
    id TEXT PRIMARY KEY,
    term TEXT NOT NULL UNIQUE,
    aliases TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '通用',
    category TEXT NOT NULL DEFAULT '其他',
    source TEXT NOT NULL DEFAULT 'manual',
    confirmed INTEGER NOT NULL DEFAULT 1,
    hit_count INTEGER NOT NULL DEFAULT 0,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glossary_terms_scope ON glossary_terms(scope);

CREATE TABLE IF NOT EXISTS glossary_suggestions (
    id TEXT PRIMARY KEY,
    wrong TEXT NOT NULL,
    correct TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '通用',
    meeting_id TEXT,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glossary_suggestions_status ON glossary_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_glossary_suggestions_pair ON glossary_suggestions(wrong, correct);

-- v12：项目 → 需求 → 任务三层（260915 新增）。
CREATE TABLE IF NOT EXISTS project_material_roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, path)
);
CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','shelved')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 同一项目下需求名（忽略大小写、去首尾空格）唯一，创建/改名时靠这条索引兜底判重。
CREATE UNIQUE INDEX IF NOT EXISTS idx_requirements_project_title
    ON requirements(project_id, lower(trim(title)));
CREATE INDEX IF NOT EXISTS idx_requirements_project_status ON requirements(project_id, status);
CREATE TABLE IF NOT EXISTS requirement_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(requirement_id, path)
);
CREATE TABLE IF NOT EXISTS requirement_meetings (
    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (requirement_id, meeting_id)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conflicts = ConflictStore(self)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def user_version(self) -> int:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return 0
        uri = f"file:{self.path.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def initialize(self, *, before_migrate: Callable[[], Any] | None = None) -> None:
        had_database = self.path.is_file() and self.path.stat().st_size > 0
        current_version = self.user_version()
        if current_version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"database version {current_version} is newer than supported {SCHEMA_VERSION}"
            )
        if had_database and current_version < SCHEMA_VERSION and before_migrate:
            before_migrate()
        with self.autocommit() as connection:
            connection.executescript(SCHEMA)
            connection.execute("BEGIN EXCLUSIVE")
            transcript_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(transcript_versions)").fetchall()
            }
            if "source_path" not in transcript_columns:
                connection.execute("ALTER TABLE transcript_versions ADD COLUMN source_path TEXT")
            if "source_sha256" not in transcript_columns:
                connection.execute("ALTER TABLE transcript_versions ADD COLUMN source_sha256 TEXT")
            meeting_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(meetings)").fetchall()
            }
            if "source_job_id" not in meeting_columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN source_job_id TEXT")
            if "project_origin" not in meeting_columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN project_origin TEXT")
            project_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "origin" not in project_columns:
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
                )
            glossary_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(glossary_terms)").fetchall()
            }
            if "project_id" not in glossary_columns:
                connection.execute(
                    "ALTER TABLE glossary_terms ADD COLUMN project_id "
                    "TEXT REFERENCES projects(id) ON DELETE SET NULL"
                )
            # 建索引放到列存在之后：executescript(SCHEMA) 早于这里执行，SCHEMA 里若
            # 直接带这条 CREATE INDEX，旧库补列之前就会报 "no such column"。
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_glossary_terms_project "
                "ON glossary_terms(project_id)"
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "requirement_id" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN requirement_id "
                    "TEXT REFERENCES requirements(id) ON DELETE SET NULL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_requirement ON tasks(requirement_id)"
            )
            minutes_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(minutes_versions)").fetchall()
            }
            for name, declaration in (
                ("based_on_id", "TEXT REFERENCES minutes_versions(id)"),
                ("content_sha256", "TEXT"),
                ("source_job_id", "TEXT"),
                ("source_attempt", "INTEGER"),
                ("requested_stage", "TEXT"),
                ("input_transcript_sha256", "TEXT"),
            ):
                if name not in minutes_columns:
                    connection.execute(
                        f"ALTER TABLE minutes_versions ADD COLUMN {name} {declaration}"
                    )
            shadow_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(asr_shadow_runs)").fetchall()
            }
            for name in ("owner_id", "lease_expires_at", "heartbeat_at"):
                if name not in shadow_columns:
                    connection.execute(f"ALTER TABLE asr_shadow_runs ADD COLUMN {name} TEXT")
            connection.execute(
                """DELETE FROM runtime_leases
                    WHERE run_id IN (
                        SELECT id FROM asr_shadow_runs
                         WHERE state='running'
                           AND (owner_id IS NULL OR lease_expires_at IS NULL
                                OR heartbeat_at IS NULL)
                    )"""
            )
            connection.execute(
                """UPDATE asr_shadow_runs
                      SET state='queued', error='租约信息不完整后重新排队',
                          owner_id=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                          updated_at=?, finished_at=NULL
                    WHERE state='running'
                      AND (owner_id IS NULL OR lease_expires_at IS NULL
                           OR heartbeat_at IS NULL)""",
                (utc_now(),),
            )
            for row in connection.execute(
                "SELECT id, markdown FROM minutes_versions WHERE content_sha256 IS NULL"
            ).fetchall():
                connection.execute(
                    "UPDATE minutes_versions SET content_sha256=? WHERE id=?",
                    (hashlib.sha256(row["markdown"].encode("utf-8")).hexdigest(), row["id"]),
                )
            if current_version < 2:
                from .rendering import render_safe_markdown

                for row in connection.execute(
                    "SELECT id, markdown FROM minutes_versions"
                ).fetchall():
                    connection.execute(
                        "UPDATE minutes_versions SET html=? WHERE id=?",
                        (render_safe_markdown(row["markdown"]), row["id"]),
                    )
            if current_version < 4:
                event_kind = {
                    "external_change_conflict": "external_source_change",
                    "audio_integrity_conflict": "audio_integrity",
                    "publish_recovery_conflict": "publish_recovery",
                    "publish_post_commit_conflict": "publish_post_commit",
                }
                conflicted = connection.execute(
                    "SELECT id, source_signature FROM meetings WHERE conflict=1"
                ).fetchall()
                for meeting in conflicted:
                    if connection.execute(
                        """SELECT 1 FROM meeting_conflicts
                           WHERE meeting_id=? AND status='open' LIMIT 1""",
                        (meeting["id"],),
                    ).fetchone():
                        continue
                    event = connection.execute(
                        """SELECT event_type, payload_json FROM events
                           WHERE meeting_id=? AND event_type IN (
                               'external_change_conflict', 'audio_integrity_conflict',
                               'publish_recovery_conflict', 'publish_post_commit_conflict'
                           )
                           ORDER BY id DESC LIMIT 1""",
                        (meeting["id"],),
                    ).fetchone()
                    kind = (
                        event_kind.get(event["event_type"], "legacy_unknown")
                        if event
                        else ("legacy_unknown")
                    )
                    payload_json = event["payload_json"] if event else "{}"
                    now = utc_now()
                    connection.execute(
                        """INSERT INTO meeting_conflicts
                           (id, meeting_id, kind, status, source_signature, payload_json,
                            created_at, updated_at)
                           VALUES (?, ?, ?, 'open', ?, ?, ?, ?)""",
                        (
                            f"conflict-{uuid.uuid4().hex}",
                            meeting["id"],
                            kind,
                            meeting["source_signature"],
                            payload_json,
                            now,
                            now,
                        ),
                    )
            connection.execute(
                """UPDATE meetings
                   SET conflict = EXISTS(
                       SELECT 1 FROM meeting_conflicts mc
                        WHERE mc.meeting_id=meetings.id AND mc.status='open'
                   )"""
            )
            connection.execute("UPDATE segments SET end_ms = start_ms WHERE end_ms < start_ms")
            if current_version < 9:
                # v9 之前挂了项目的会议一律是人工填的（会议自动归属项目功能上线前，
                # 只有 PATCH /api/meetings 这一条写入路径），origin 补 manual。
                connection.execute(
                    """UPDATE meetings SET project_origin='manual'
                        WHERE project_id IS NOT NULL AND project_origin IS NULL"""
                )
            if current_version < 10:
                # 词典范围与项目打通：scope 与某个项目名精确相等的术语补上 project_id，
                # 之后 project_id 才是范围的唯一来源、scope 变成随项目改名同步的派生标签。
                # 名字不一致的旧桶（如「云图」vs 项目名「云图科研用药」）不在此处处理，
                # 交给一次性 SQL 按业务口径迁移。
                connection.execute(
                    """UPDATE glossary_terms
                          SET project_id = (
                              SELECT id FROM projects WHERE projects.name = glossary_terms.scope
                          )
                        WHERE project_id IS NULL
                          AND EXISTS (
                              SELECT 1 FROM projects WHERE projects.name = glossary_terms.scope
                          )"""
                )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def autocommit(self) -> Iterator[sqlite3.Connection]:
        """成功提交、异常回滚，并且一定关闭连接。

        sqlite3.Connection 的语句缓存反向引用连接本身，`with connect()` 只提交不关闭，
        句柄要等循环 GC 才释放；扫描高峰叠加页面请求会冲破进程句柄上限（2026-09-07、
        09-14 两次 EMFILE 宕机）。凡是不需要 BEGIN IMMEDIATE 的地方都走这里。
        """
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.autocommit() as connection:
            cursor = connection.execute(sql, params)
            return cursor.lastrowid

    def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行 UPDATE/DELETE 并返回实际命中的行数（原子认领等需要）。"""
        with self.autocommit() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.autocommit() as connection:
            row = connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.autocommit() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def create_transcript_version(
        self,
        meeting_id: str,
        kind: str,
        *,
        published: bool = False,
        based_on_id: str | None = None,
        source_path: str | None = None,
        source_sha256: str | None = None,
        make_current: bool = True,
    ) -> str:
        with self.transaction() as connection:
            return self.create_transcript_version_with_connection(
                connection,
                meeting_id,
                kind,
                published=published,
                based_on_id=based_on_id,
                source_path=source_path,
                source_sha256=source_sha256,
                make_current=make_current,
            )

    @staticmethod
    def create_transcript_version_with_connection(
        connection: sqlite3.Connection,
        meeting_id: str,
        kind: str,
        *,
        published: bool = False,
        based_on_id: str | None = None,
        source_path: str | None = None,
        source_sha256: str | None = None,
        make_current: bool = True,
    ) -> str:
        version_id = f"tv-{uuid.uuid4().hex}"
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM transcript_versions WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO transcript_versions
               (id, meeting_id, version_no, kind, based_on_id, source_path,
                source_sha256, published, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id,
                meeting_id,
                version_no,
                kind,
                based_on_id,
                source_path,
                source_sha256,
                int(published),
                utc_now(),
            ),
        )
        if make_current:
            connection.execute(
                "UPDATE meetings SET current_transcript_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, utc_now(), meeting_id),
            )
        return version_id

    def replace_segments(
        self,
        version_id: str,
        meeting_id: str,
        segments: Iterable[dict[str, Any]],
    ) -> None:
        with self.transaction() as connection:
            self.replace_segments_with_connection(connection, version_id, meeting_id, segments)

    @staticmethod
    def replace_segments_with_connection(
        connection: sqlite3.Connection,
        version_id: str,
        meeting_id: str,
        segments: Iterable[dict[str, Any]],
    ) -> None:
        normalized = list(segments)
        old_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM segments WHERE version_id = ?", (version_id,)
            ).fetchall()
        ]
        connection.execute("DELETE FROM segments_fts WHERE version_id = ?", (version_id,))
        connection.execute("DELETE FROM segments WHERE version_id = ?", (version_id,))
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            connection.execute(
                f"DELETE FROM embeddings WHERE segment_id IN ({placeholders})", old_ids
            )
        for ordinal, segment in enumerate(normalized):
            segment_id = segment.get("id") or f"seg-{uuid.uuid4().hex}"
            start_ms = max(0, int(segment.get("start_ms", 0)))
            end_ms = max(start_ms, int(segment.get("end_ms", start_ms)))
            values = (
                segment_id,
                version_id,
                meeting_id,
                # ordinal 由服务端按传入顺序重排，不采信客户端字段：否则两段都传同一个
                # ordinal 会撞 UNIQUE(version_id, ordinal) 直接 500。
                ordinal,
                start_ms,
                end_ms,
                segment.get("speaker_label"),
                segment.get("speaker_name"),
                str(segment.get("text", "")).strip(),
            )
            connection.execute(
                """INSERT INTO segments
                   (id, version_id, meeting_id, ordinal, start_ms, end_ms,
                    speaker_label, speaker_name, text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            connection.execute(
                "INSERT INTO segments_fts(segment_id, meeting_id, version_id, text) VALUES (?, ?, ?, ?)",
                (segment_id, meeting_id, version_id, values[-1]),
            )

    @staticmethod
    def sync_transcript_metadata_with_connection(
        connection: sqlite3.Connection,
        meeting_id: str,
        version_id: str | None,
    ) -> None:
        rows = connection.execute(
            """SELECT end_ms, speaker_label, speaker_name
               FROM segments WHERE version_id=? ORDER BY ordinal""",
            (version_id,),
        ).fetchall()
        duration_ms = max((int(row["end_ms"]) for row in rows), default=0)
        speakers: dict[str, str | None] = {}
        for row in rows:
            label = row["speaker_label"]
            if label:
                speakers[str(label)] = row["speaker_name"]

        connection.execute(
            "UPDATE meetings SET duration_ms=? WHERE id=?",
            (duration_ms, meeting_id),
        )
        connection.execute("DELETE FROM speakers WHERE meeting_id=?", (meeting_id,))
        for label, display_name in speakers.items():
            speaker_id = (
                f"speaker-{hashlib.sha1(f'{meeting_id}:{label}'.encode()).hexdigest()[:20]}"
            )
            connection.execute(
                """INSERT INTO speakers(id, meeting_id, label, display_name)
                   VALUES (?, ?, ?, ?)""",
                (speaker_id, meeting_id, label, display_name),
            )

    def exact_search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        common = """
            SELECT s.id AS segment_id, s.meeting_id, m.title, m.canonical_dir,
                   m.recording_date, 'segment' AS match_kind,
                   s.start_ms, s.end_ms,
                   s.speaker_name, s.speaker_label, s.text
              FROM segments s
              JOIN meetings m ON m.id = s.meeting_id
        """
        if len(query) < 3:
            sql = (
                common
                + """
                WHERE s.version_id = m.current_transcript_version_id
                  AND s.text LIKE ? ESCAPE '\\'
                ORDER BY m.recording_date DESC, s.ordinal ASC
                LIMIT ?
            """
            )
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            results = self.query_all(sql, (f"%{escaped}%", limit))
        else:
            match_query = f'"{query.replace(chr(34), chr(34) * 2)}"'
            sql = """
                SELECT s.id AS segment_id, s.meeting_id, m.title, m.canonical_dir,
                       m.recording_date,
                       'segment' AS match_kind, s.start_ms, s.end_ms,
                       s.speaker_name, s.speaker_label, s.text
                  FROM segments_fts f
                  JOIN segments s ON s.id = f.segment_id
                  JOIN meetings m ON m.id = s.meeting_id
                 WHERE segments_fts MATCH ?
                   AND s.version_id = m.current_transcript_version_id
                 ORDER BY bm25(segments_fts), m.recording_date DESC, s.ordinal ASC
                 LIMIT ?
            """
            results = self.query_all(sql, (match_query, limit))
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        title_rows = self.query_all(
            """SELECT s.id AS segment_id, s.meeting_id, m.title, m.canonical_dir,
                      m.recording_date, 'title' AS match_kind,
                      s.start_ms, s.end_ms,
                      s.speaker_name, s.speaker_label, s.text
                 FROM meetings m
                 JOIN segments s ON s.version_id = m.current_transcript_version_id
                  AND s.ordinal = (
                      SELECT MIN(anchor.ordinal) FROM segments anchor
                       WHERE anchor.version_id = m.current_transcript_version_id
                  )
                WHERE m.title LIKE ? ESCAPE '\\'
                ORDER BY COALESCE(m.recording_date, m.created_at) DESC
                LIMIT ?""",
            (f"%{escaped}%", limit),
        )
        result_positions = {row["meeting_id"]: index for index, row in enumerate(results)}
        for row in title_rows:
            existing_index = result_positions.get(row["meeting_id"])
            if existing_index is not None:
                results[existing_index] = row
            elif len(results) < limit:
                results.append(row)
                result_positions[row["meeting_id"]] = len(results) - 1
        return results[:limit]

    def add_event(
        self,
        event_type: str,
        *,
        meeting_id: str | None = None,
        job_id: str | None = None,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        statement = """INSERT INTO events
                       (meeting_id, job_id, event_type, actor, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)"""
        values = (
            meeting_id,
            job_id,
            event_type,
            actor,
            json.dumps(payload or {}, ensure_ascii=False),
            utc_now(),
        )
        if connection is not None:
            return connection.execute(statement, values).lastrowid
        return self.execute(statement, values)


class ConflictStore:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in CONFLICT_KINDS:
            raise ValueError(f"unsupported conflict kind: {kind}")

    def open(
        self,
        meeting_id: str,
        kind: str,
        *,
        source_signature: str | None = None,
        payload: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        self._validate_kind(kind)
        if connection is not None:
            return self._open_with_connection(
                connection,
                meeting_id,
                kind,
                source_signature=source_signature,
                payload=payload,
            )
        with self.db.transaction() as transaction:
            return self._open_with_connection(
                transaction,
                meeting_id,
                kind,
                source_signature=source_signature,
                payload=payload,
            )

    @staticmethod
    def _open_with_connection(
        connection: sqlite3.Connection,
        meeting_id: str,
        kind: str,
        *,
        source_signature: str | None,
        payload: dict[str, Any] | None,
    ) -> str:
        conflict_id = f"conflict-{uuid.uuid4().hex}"
        now = utc_now()
        connection.execute(
            """INSERT INTO meeting_conflicts
               (id, meeting_id, kind, status, source_signature, payload_json,
                created_at, updated_at)
               VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
               ON CONFLICT(meeting_id, kind) WHERE status='open' DO UPDATE SET
                 source_signature=COALESCE(excluded.source_signature,
                                           meeting_conflicts.source_signature),
                 payload_json=excluded.payload_json,
                 updated_at=excluded.updated_at""",
            (
                conflict_id,
                meeting_id,
                kind,
                source_signature,
                json.dumps(payload or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = connection.execute(
            """SELECT id FROM meeting_conflicts
               WHERE meeting_id=? AND kind=? AND status='open'""",
            (meeting_id, kind),
        ).fetchone()
        return str(row["id"])

    def list(
        self,
        meeting_id: str,
        *,
        status: str = "open",
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        if status not in {"open", "resolved"}:
            raise ValueError(f"unsupported conflict status: {status}")
        if kind is not None:
            self._validate_kind(kind)
        sql = "SELECT * FROM meeting_conflicts WHERE meeting_id=? AND status=?"
        params: list[Any] = [meeting_id, status]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at, id"
        rows = self.db.query_all(sql, params)
        for row in rows:
            try:
                decoded = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            row["payload"] = decoded if isinstance(decoded, dict) else {}
        return rows

    def has(self, meeting_id: str, kind: str | None = None) -> bool:
        if kind is not None:
            self._validate_kind(kind)
        sql = "SELECT 1 AS present FROM meeting_conflicts WHERE meeting_id=? AND status='open'"
        params: list[Any] = [meeting_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " LIMIT 1"
        return self.db.query_one(sql, params) is not None

    def resolve(
        self,
        conflict_id: str,
        resolution: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        if connection is not None:
            return self._resolve_with_connection(connection, conflict_id, resolution)
        with self.db.transaction() as transaction:
            return self._resolve_with_connection(transaction, conflict_id, resolution)

    @staticmethod
    def _resolve_with_connection(
        connection: sqlite3.Connection, conflict_id: str, resolution: str
    ) -> bool:
        now = utc_now()
        cursor = connection.execute(
            """UPDATE meeting_conflicts
               SET status='resolved', resolution=?, resolved_at=?, updated_at=?
               WHERE id=? AND status='open'""",
            (resolution, now, now, conflict_id),
        )
        return cursor.rowcount == 1

    def resolve_kind(
        self,
        meeting_id: str,
        kind: str,
        resolution: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        self._validate_kind(kind)
        if connection is not None:
            return self._resolve_kind_with_connection(connection, meeting_id, kind, resolution)
        with self.db.transaction() as transaction:
            return self._resolve_kind_with_connection(transaction, meeting_id, kind, resolution)

    @staticmethod
    def _resolve_kind_with_connection(
        connection: sqlite3.Connection, meeting_id: str, kind: str, resolution: str
    ) -> int:
        now = utc_now()
        cursor = connection.execute(
            """UPDATE meeting_conflicts
               SET status='resolved', resolution=?, resolved_at=?, updated_at=?
               WHERE meeting_id=? AND kind=? AND status='open'""",
            (resolution, now, now, meeting_id, kind),
        )
        return cursor.rowcount
