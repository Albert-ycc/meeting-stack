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


SCHEMA_VERSION = 17

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

-- v13：智能关联第一期。app_state 放全局开关与一次性任务的进度（键见各模块）。
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- v13：用户对「像新项目」名字的决定。norm_key 是归一化后的名字（见 project_profile.norm_key）；
-- decision=ignored 表示「不是新项目」，以后同名不再提示；decision=project 表示已建成 target_id。
CREATE TABLE IF NOT EXISTS name_decisions (
    norm_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ignored', 'project')),
    target_id TEXT,
    decided_at TEXT NOT NULL
);

-- v13：当前纪要的全文索引，供检索与归属规则回溯。只索引每场会的当前纪要，
-- 由 meetings 上的三个触发器维护；纪要正文入库后不会原地改写，所以不需要
-- minutes_versions 上的触发器。
CREATE VIRTUAL TABLE IF NOT EXISTS minutes_fts USING fts5(
    meeting_id UNINDEXED,
    text,
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS minutes_fts_after_meeting_insert
AFTER INSERT ON meetings
WHEN NEW.current_minutes_version_id IS NOT NULL
BEGIN
    INSERT INTO minutes_fts(meeting_id, text)
    SELECT NEW.id, markdown FROM minutes_versions WHERE id = NEW.current_minutes_version_id;
END;
CREATE TRIGGER IF NOT EXISTS minutes_fts_after_minutes_pointer_update
AFTER UPDATE OF current_minutes_version_id ON meetings
WHEN NEW.current_minutes_version_id IS NOT OLD.current_minutes_version_id
BEGIN
    DELETE FROM minutes_fts WHERE meeting_id = OLD.id;
    INSERT INTO minutes_fts(meeting_id, text)
    SELECT NEW.id, markdown FROM minutes_versions WHERE id = NEW.current_minutes_version_id;
END;
CREATE TRIGGER IF NOT EXISTS minutes_fts_after_meeting_delete
AFTER DELETE ON meetings
BEGIN
    DELETE FROM minutes_fts WHERE meeting_id = OLD.id;
END;

-- v13 / 1c：会议卡片台账（见 cards.py）。一场会最多一张卡片，写在项目最早挂上的材料根目录
-- 下的「声档会议记录/」。state：pending 等待写入 / synced 已写入 / user_edited 你改过纪要部分，
-- 停止自动更新 / missing 卡片被移走或删了（只对当前项目有效）/ retired 已撤下进回收区 /
-- blocked 写不了（原因见 reason）。written_fps 是最近 5 次写入的内容指纹，用来认出你改没改过。
-- dirty 是计数：触发器只加一，写完只在计数没变时清零，写的过程中又变了就留到下一轮。
-- 不设外键：会议删掉后由 reconcile 把卡片移进回收区再删行。
CREATE TABLE IF NOT EXISTS meeting_cards (
    meeting_id TEXT PRIMARY KEY,
    project_id TEXT,
    root_path TEXT,
    rel_path TEXT,
    transcript_rel_path TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    written_fps TEXT NOT NULL DEFAULT '[]',
    transcript_fp TEXT,
    retired_path TEXT,
    retired_edited INTEGER NOT NULL DEFAULT 0,
    carry_notes TEXT,
    user_named INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meeting_cards_project ON meeting_cards(project_id);

-- 1d-2：出纪要时这场会用了哪些词。relay 按这场会挑词后在草稿目录写 glossary-injection.json，
-- 导入后原样存进 payload；job_id/attempt 用来对上是哪一版纪要用的。
CREATE TABLE IF NOT EXISTS meeting_glossary_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    job_id TEXT,
    attempt INTEGER,
    sha256 TEXT NOT NULL,
    project_id TEXT,
    project_name TEXT,
    project_source TEXT,
    term_count INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(meeting_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_meeting_glossary_receipts_job
    ON meeting_glossary_receipts(meeting_id, job_id, attempt);

-- 1d-2：纪要体检。每场会一行状态：查的是哪一版纪要、按哪个项目的词典（basis：receipt 按回执的项目 /
-- project 按会议当前项目或你点的项目 / public 只有公共词）；替换过的记下替换前的版本，撤销就回到那一版。
CREATE TABLE IF NOT EXISTS meeting_glossary_checks (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    minutes_version_id TEXT,
    project_id TEXT,
    basis TEXT NOT NULL,
    receipt_id INTEGER,
    applied_version_id TEXT,
    applied_from_version_id TEXT,
    applied_by TEXT,
    applied_count INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    checked_at TEXT NOT NULL
);

-- 体检明细：corrected 错写在逐字稿里、正确写法在纪要里、错写不在纪要里（AI 已经纠过来了）；
-- missed 纪要里还留着错写（可能漏纠）。随纪要版本整批重算。
CREATE TABLE IF NOT EXISTS meeting_glossary_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    minutes_version_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    term TEXT NOT NULL,
    wrong TEXT NOT NULL,
    term_project_id TEXT,
    transcript_count INTEGER NOT NULL DEFAULT 0,
    minutes_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meeting_glossary_hits_meeting ON meeting_glossary_hits(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_cards_dirty ON meeting_cards(dirty) WHERE dirty > 0;

-- 卡片脏标记：只给已有卡片行的会加一，新会由 reconcile 自己找。拆段、合段走先删后插，
-- 行级触发器抓不到，所以逐字稿和说话人的改动靠 events 表里的编辑事件。
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_meeting
AFTER UPDATE OF title, project_id, project_origin, current_minutes_version_id,
    current_transcript_version_id, recording_date, duration_ms ON meetings
WHEN NEW.title IS NOT OLD.title
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.project_origin IS NOT OLD.project_origin
    OR NEW.current_minutes_version_id IS NOT OLD.current_minutes_version_id
    OR NEW.current_transcript_version_id IS NOT OLD.current_transcript_version_id
    OR NEW.recording_date IS NOT OLD.recording_date
    OR NEW.duration_ms IS NOT OLD.duration_ms
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_task_insert
AFTER INSERT ON tasks
WHEN NEW.meeting_id IS NOT NULL
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id = NEW.meeting_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_task_update
AFTER UPDATE ON tasks
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1
     WHERE meeting_id IN (NEW.meeting_id, OLD.meeting_id);
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_task_delete
AFTER DELETE ON tasks
WHEN OLD.meeting_id IS NOT NULL
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id = OLD.meeting_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_project_name
AFTER UPDATE OF name ON projects
WHEN NEW.name IS NOT OLD.name
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE project_id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_requirement_title
AFTER UPDATE OF title ON requirements
WHEN NEW.title IS NOT OLD.title
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1
     WHERE meeting_id IN (
        SELECT meeting_id FROM requirement_meetings WHERE requirement_id = NEW.id);
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_requirement_link
AFTER INSERT ON requirement_meetings
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id = NEW.meeting_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_requirement_unlink
AFTER DELETE ON requirement_meetings
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id = OLD.meeting_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_requirement_folder_add
AFTER INSERT ON requirement_folders
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1
     WHERE meeting_id IN (
        SELECT meeting_id FROM requirement_meetings WHERE requirement_id = NEW.requirement_id);
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_requirement_folder_remove
AFTER DELETE ON requirement_folders
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1
     WHERE meeting_id IN (
        SELECT meeting_id FROM requirement_meetings WHERE requirement_id = OLD.requirement_id);
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_root_add
AFTER INSERT ON project_material_roots
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE project_id = NEW.project_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_root_change
AFTER UPDATE OF path ON project_material_roots
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE project_id = NEW.project_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_root_remove
AFTER DELETE ON project_material_roots
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE project_id = OLD.project_id;
END;
CREATE TRIGGER IF NOT EXISTS meeting_cards_dirty_transcript_edit
AFTER INSERT ON events
WHEN NEW.meeting_id IS NOT NULL AND NEW.event_type IN (
    'speaker_renamed', 'segment_split', 'segments_merged',
    'transcript_draft_saved', 'minutes_saved')
BEGIN
    UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id = NEW.meeting_id;
END;

-- v14 / 2b：需求名的决定，按项目记。name_key 是「轻键」（project_profile.light_key：NFKC、
-- 大小写、去空白标点，不去「二期」「v2」「项目」），所以「云图二期」和「云图三期」互不影响。
-- decision=made 表示已建成需求 requirement_id；ignored 表示「不算新需求」。项目名的决定仍在
-- name_decisions，两张表互不覆盖。
CREATE TABLE IF NOT EXISTS requirement_name_decisions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name_key TEXT NOT NULL,
    name TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('made', 'ignored')),
    requirement_id TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name_key)
);

-- v14 / 2a：盘不在时先建项目、后补文件夹的待办。name 为 NULL 表示跟着项目当前的名字；
-- state：waiting 等盘插上 / stopped 出错停下（原因在 last_error），盘重新插上或改了位置再试。
-- 只在 state 或 last_error 变了时才写（这张表进关系图版本号）。
CREATE TABLE IF NOT EXISTS pending_project_folders (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    parent TEXT NOT NULL,
    name TEXT,
    state TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'stopped')),
    last_error TEXT,
    created_at TEXT NOT NULL
);
-- 你手动挂了文件夹，待办就作废。
CREATE TRIGGER IF NOT EXISTS pending_project_folders_drop_on_mount
AFTER INSERT ON project_material_roots
BEGIN
    DELETE FROM pending_project_folders WHERE project_id = NEW.project_id;
END;

-- v14 / 2a：「不是项目」（kind=unclaimed，scope 空串）和改名找回时的「不是它」（kind=rename，
-- scope 是根目录 id）。
CREATE TABLE IF NOT EXISTS folder_declines (
    kind TEXT NOT NULL CHECK (kind IN ('unclaimed', 'rename')),
    scope TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (kind, scope, path)
);

-- v14 / 2a：根目录在线时记下的一级子文件夹名（最多 200 个），文件夹改名后靠它找回。内容变了才写。
CREATE TABLE IF NOT EXISTS root_fingerprints (
    root_id INTEGER PRIMARY KEY REFERENCES project_material_roots(id) ON DELETE CASCADE,
    child_names TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

-- v14 / 2d：文件名索引，只存名字，不读内容（material_index.py 在后台一轮轮扫）。
-- zone：normal 拿来比对；cards 是声档自己写的会议卡片、code 是代码和配置文件，只收名字；
-- package 是 .key、.pages 这类在访达里看起来是一个文件的包。gone_at 只按目录重读时判断。
CREATE TABLE IF NOT EXISTS material_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES project_material_roots(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    dir_rel TEXT NOT NULL,
    name TEXT NOT NULL,
    stem TEXT NOT NULL,
    stem_key TEXT NOT NULL,
    ext TEXT NOT NULL DEFAULT '',
    size INTEGER,
    mtime_ns INTEGER,
    zone TEXT NOT NULL DEFAULT 'normal' CHECK (zone IN ('normal', 'cards', 'code', 'package')),
    seen_at TEXT NOT NULL,
    gone_at TEXT,
    UNIQUE(root_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_material_files_dir ON material_files(root_id, dir_rel);
CREATE INDEX IF NOT EXISTS idx_material_files_stem ON material_files(stem_key);

-- 目录修改时间没变就不重读；name_only（node_modules、.git、点开头的）只记直接子项个数。
CREATE TABLE IF NOT EXISTS material_dirs (
    root_id INTEGER NOT NULL REFERENCES project_material_roots(id) ON DELETE CASCADE,
    dir_rel TEXT NOT NULL,
    mtime_ns INTEGER,
    listed_at TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT 'normal' CHECK (zone IN ('normal', 'name_only', 'cards')),
    child_count INTEGER NOT NULL DEFAULT 0,
    -- v15 / 3e：这一层里没跟进去的符号链接个数（覆盖率里写「符号链接 N 个没跟进去」）
    symlinks INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (root_id, dir_rel)
);

-- 每个根目录一行，断点续扫：cursor 是 DFS 栈（JSON），只在每轮结束时写。stems_rev 只在一整轮
-- 扫完、词干集合（stems_hash）变了时加一，会议比对的 stems_sig 靠它。
CREATE TABLE IF NOT EXISTS material_index_state (
    root_id INTEGER PRIMARY KEY REFERENCES project_material_roots(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'walking', 'done', 'offline', 'missing', 'error')),
    cursor TEXT,
    files INTEGER NOT NULL DEFAULT 0,
    stems_rev INTEGER NOT NULL DEFAULT 0,
    stems_hash TEXT,
    sweep_started_at TEXT,
    last_full_at TEXT,
    error TEXT,
    updated_at TEXT
);
-- 根目录换了路径（改名找回、重新选）：下一轮从头整轮重读，旧位置才有的文件按目录判断成不见了。
CREATE TRIGGER IF NOT EXISTS material_index_root_moved
AFTER UPDATE OF path ON project_material_roots
WHEN NEW.path IS NOT OLD.path
BEGIN
    UPDATE material_index_state SET state = 'pending', cursor = NULL, last_full_at = NULL
     WHERE root_id = NEW.id;
END;

-- 会上提到文件名。rejected 是「不是这份文件」，只挡同一场会、同一个项目，比对永远不改回 active；
-- picked 是你手动换过文件，比对不再自动改（文件不在了才换）。
CREATE TABLE IF NOT EXISTS meeting_file_mentions (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    stem_key TEXT NOT NULL,
    file_id INTEGER REFERENCES material_files(id) ON DELETE SET NULL,
    needle TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    first_ms INTEGER,
    anchors_json TEXT NOT NULL DEFAULT '[]',
    minutes_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'transcript' CHECK (source IN ('transcript', 'minutes')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'rejected')),
    picked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (meeting_id, project_id, stem_key)
);
CREATE INDEX IF NOT EXISTS idx_meeting_file_mentions_file ON meeting_file_mentions(file_id);
CREATE INDEX IF NOT EXISTS idx_meeting_file_mentions_stem ON meeting_file_mentions(project_id, stem_key);

-- 哪些会要重新比对文件名。dirty 是计数（和卡片一样防丢更新）：比对开始时记下值，写结果时
-- 只在值没变时写。已归项目、还没有这一行的会也要比对。触发器用插入或加一，免得比对进行中
-- 第一次改稿时没有行可加、结果被旧稿覆盖。
CREATE TABLE IF NOT EXISTS meeting_file_scan (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    stems_sig TEXT NOT NULL DEFAULT '',
    dirty INTEGER NOT NULL DEFAULT 0,
    scanned_at TEXT
);
CREATE TRIGGER IF NOT EXISTS meeting_file_scan_dirty_meeting
AFTER UPDATE OF project_id, current_transcript_version_id, current_minutes_version_id ON meetings
WHEN NEW.project_id IS NOT OLD.project_id
    OR NEW.current_transcript_version_id IS NOT OLD.current_transcript_version_id
    OR NEW.current_minutes_version_id IS NOT OLD.current_minutes_version_id
BEGIN
    INSERT INTO meeting_file_scan(meeting_id, dirty) VALUES (NEW.id, 1)
    ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1;
END;
CREATE TRIGGER IF NOT EXISTS meeting_file_scan_dirty_transcript_edit
AFTER INSERT ON events
WHEN NEW.meeting_id IS NOT NULL AND NEW.event_type IN (
    'speaker_renamed', 'segment_split', 'segments_merged',
    'transcript_draft_saved', 'minutes_saved')
BEGIN
    INSERT INTO meeting_file_scan(meeting_id, dirty)
    SELECT id, 1 FROM meetings WHERE id = NEW.meeting_id
    ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1;
END;
-- 会议离开项目：它在旧项目的有效提到立刻去掉，「不是这份文件」留着（只挡原项目）。
CREATE TRIGGER IF NOT EXISTS meeting_file_mentions_leave_project
AFTER UPDATE OF project_id ON meetings
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    DELETE FROM meeting_file_mentions
     WHERE meeting_id = NEW.id AND status = 'active' AND project_id IS NOT NEW.project_id;
END;

-- v15 / 3a：材料内容，一份内容一行（按 material_files.content_key），挪位置、改名、复制都共用。
-- layer：text / pdf / image / media。state：pending 待读、done 读完、unreadable 读不了（reason
-- 四种之一）、waiting 在等识别程序（note=engine_missing）。note 都不算读不了：small_image、
-- no_text、no_speech、truncated、meeting_audio、engine_missing。没有权限记在文件行上，不在这里。
CREATE TABLE IF NOT EXISTS material_contents (
    content_key TEXT PRIMARY KEY,
    layer TEXT NOT NULL CHECK (layer IN ('text', 'pdf', 'image', 'media')),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'done', 'unreadable', 'waiting')),
    reason TEXT CHECK (reason IS NULL OR reason IN ('password', 'corrupt', 'unsupported', 'timeout')),
    note TEXT,
    extractor TEXT,
    extractor_version INTEGER,
    size INTEGER,
    chars INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    pages INTEGER,
    duration_ms INTEGER,
    sha256 TEXT,
    meeting_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at TEXT,
    orphan_since TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_material_contents_state ON material_contents(state, layer);

-- 段落级片段，每段不超过 400 字。id 只增不复用：向量循环和内存矩阵靠它判断新旧。
CREATE TABLE IF NOT EXISTS material_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_key TEXT NOT NULL REFERENCES material_contents(content_key) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    loc TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    text TEXT NOT NULL,
    UNIQUE(content_key, ordinal)
);

-- 材料的全文索引：外部内容表，文字只存一份；和会议的两张全文表分开。
CREATE VIRTUAL TABLE IF NOT EXISTS material_chunks_fts USING fts5(
    text,
    content='material_chunks',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS material_chunks_fts_insert
AFTER INSERT ON material_chunks
BEGIN
    INSERT INTO material_chunks_fts(rowid, text) VALUES (NEW.id, NEW.text);
END;
CREATE TRIGGER IF NOT EXISTS material_chunks_fts_delete
AFTER DELETE ON material_chunks
BEGIN
    INSERT INTO material_chunks_fts(material_chunks_fts, rowid, text) VALUES ('delete', OLD.id, OLD.text);
END;
CREATE TRIGGER IF NOT EXISTS material_chunks_fts_update
AFTER UPDATE OF text ON material_chunks
BEGIN
    INSERT INTO material_chunks_fts(material_chunks_fts, rowid, text) VALUES ('delete', OLD.id, OLD.text);
    INSERT INTO material_chunks_fts(rowid, text) VALUES (NEW.id, NEW.text);
END;

-- 意思相近用的向量（float16）。会议那张 embeddings 表的外键指着逐字稿段落，放不进材料。
CREATE TABLE IF NOT EXISTS material_chunk_vectors (
    chunk_id INTEGER NOT NULL REFERENCES material_chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, model)
);

-- 音视频转写的断点，转完就删。parts 是已转好的句子（JSON）。
CREATE TABLE IF NOT EXISTS material_media_jobs (
    content_key TEXT PRIMARY KEY REFERENCES material_contents(content_key) ON DELETE CASCADE,
    done_ms INTEGER NOT NULL DEFAULT 0,
    total_ms INTEGER,
    parts TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    pid INTEGER,
    updated_at TEXT NOT NULL
);

-- 交付物是资料盘里的文件时记下它的内容标识，挪了位置照样找得到。
CREATE TABLE IF NOT EXISTS deliverable_files (
    deliverable_id INTEGER PRIMARY KEY REFERENCES deliverables(id) ON DELETE CASCADE,
    content_key TEXT,
    root_id INTEGER,
    rel_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_deliverable_files_content ON deliverable_files(content_key);

-- v16 / 4a：第四期（深度关联）。全部新表、触发器和索引在这一段一次建好，后面几步不再改库结构。

-- 纪要决议段里的一条是一行。id 是 dec- 加 16 位十六进制，跨纪要版本不变；纪要里没了记 gone_at，
-- 不删。text_key 是统一全半角、大小写，去掉标点和空白后的文字。placement 为空表示自动归到需求，
-- ai 是 AI 放的，picked 是你放的，none 是你说它不属于具体需求。版本记在 decision_scan。
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    text_key TEXT NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    placement TEXT CHECK (placement IS NULL OR placement IN ('ai', 'picked', 'none')),
    requirement_id TEXT REFERENCES requirements(id) ON DELETE SET NULL,
    placed_at TEXT,
    gone_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_meeting ON decisions(meeting_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_decisions_requirement ON decisions(requirement_id);
CREATE INDEX IF NOT EXISTS idx_decisions_text_key ON decisions(text_key);

-- 每场会一行台账：入库（纪要版本、项目、解析器、决议段哈希、note）、对比（pair_*）、影响
-- （affects_*）三件事共用。note：no_minutes / no_section / empty。pair_after 是失败后的下次时间。
CREATE TABLE IF NOT EXISTS decision_scan (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    minutes_version_id TEXT,
    project_id TEXT,
    parser INTEGER NOT NULL DEFAULT 0,
    section_hash TEXT,
    note TEXT CHECK (note IS NULL OR note IN ('no_minutes', 'no_section', 'empty')),
    scanned_at TEXT,
    pair_hash TEXT,
    pair_state TEXT NOT NULL DEFAULT 'idle'
        CHECK (pair_state IN ('idle', 'pending', 'running', 'done', 'failed')),
    pair_attempts INTEGER NOT NULL DEFAULT 0,
    pair_claimed_at TEXT,
    pair_after TEXT,
    pair_error TEXT,
    affects_hash TEXT,
    affects_chunk_mark INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_scan_pair ON decision_scan(pair_state)
    WHERE pair_state IN ('pending', 'running');

-- 放宽的提到、相关、产出、影响，决议之间的「后来改了」「后来又提到」，以及你对它们的回答。
-- 字面的提到仍在 meeting_file_mentions，确认过的产出仍在 deliverables。ident 只用稳定的部分拼，
-- 写入时定下、之后不改；file_id 只是当前活文件的缓存。score 只用来排序和过门槛，从不返回。
-- 每个外键子列都有索引（删根目录时连带的关联行才删得快）。
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL
        CHECK (kind IN ('mention', 'related', 'produced', 'affects', 'later_changed', 'restated')),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ident TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('shown', 'suggested', 'confirmed', 'rejected', 'resolved', 'cleared')),
    origin TEXT NOT NULL CHECK (origin IN ('rule', 'llm', 'vector', 'manual')),
    meeting_id TEXT REFERENCES meetings(id) ON DELETE CASCADE,
    at_ms INTEGER,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    decision_id TEXT REFERENCES decisions(id) ON DELETE CASCADE,
    to_decision_id TEXT REFERENCES decisions(id) ON DELETE CASCADE,
    content_key TEXT,
    root_id INTEGER,
    rel_path TEXT,
    stem_key TEXT,
    file_id INTEGER REFERENCES material_files(id) ON DELETE SET NULL,
    quote TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    score REAL,
    deliverable_id INTEGER REFERENCES deliverables(id) ON DELETE SET NULL,
    prev_json TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, project_id, ident)
);
CREATE INDEX IF NOT EXISTS idx_relations_project ON relations(project_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_relations_meeting ON relations(meeting_id, kind)
    WHERE meeting_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relations_task ON relations(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relations_decision ON relations(decision_id)
    WHERE decision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relations_to_decision ON relations(to_decision_id)
    WHERE to_decision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relations_file ON relations(file_id) WHERE file_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relations_content ON relations(content_key)
    WHERE content_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relations_deliverable ON relations(deliverable_id)
    WHERE deliverable_id IS NOT NULL;

-- 会议离开项目：只删它在旧项目里的系统行（origin 不是 manual 的 shown、suggested、cleared），
-- 包括通过它的决议连着的两类标记；你回答过的行和你［换成这份］过的行留着。产出的行 meeting_id
-- 为空，不归这里管。
CREATE TRIGGER IF NOT EXISTS relations_leave_project
AFTER UPDATE OF project_id ON meetings
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    DELETE FROM relations
     WHERE project_id IS NOT NEW.project_id
       AND status IN ('shown', 'suggested', 'cleared') AND origin != 'manual'
       AND (meeting_id = NEW.id
            OR decision_id IN (SELECT id FROM decisions WHERE meeting_id = NEW.id)
            OR to_decision_id IN (SELECT id FROM decisions WHERE meeting_id = NEW.id));
END;

-- relations 不进 GRAPH_REV_TABLES：相关（kind='related'）只给自己的 related_rev 加一，其余给
-- graph_rev 加一。相关每次重算都可能写，不能让星图和总览的缓存跟着失效。
CREATE TRIGGER IF NOT EXISTS graph_rev_relations_insert
AFTER INSERT ON relations
WHEN NEW.kind != 'related'
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('graph_rev', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS graph_rev_relations_update
AFTER UPDATE ON relations
WHEN NEW.kind != 'related'
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('graph_rev', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS graph_rev_relations_delete
AFTER DELETE ON relations
WHEN OLD.kind != 'related'
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('graph_rev', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS related_rev_relations_insert
AFTER INSERT ON relations
WHEN NEW.kind = 'related'
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('related_rev', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS related_rev_relations_update
AFTER UPDATE ON relations
WHEN NEW.kind = 'related'
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('related_rev', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS related_rev_relations_delete
AFTER DELETE ON relations
WHEN OLD.kind = 'related'
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('related_rev', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;

-- 4b 的 AI 结果：按逐字稿版本存从逐字稿里挑出的说法、原话和时间点，和本机对文件名的结果分开。
-- hints_json 是给 2d 比对用的提示；resolved_sig 记上次对文件名时的签名，变了才重对。
CREATE TABLE IF NOT EXISTS mention_extractions (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    version_id TEXT NOT NULL,
    text_sha TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'running', 'done', 'failed')),
    parts INTEGER NOT NULL DEFAULT 1,
    parts_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    phrases_json TEXT NOT NULL DEFAULT '[]',
    hints_json TEXT NOT NULL DEFAULT '{}',
    resolved_sig TEXT,
    error TEXT,
    claimed_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mention_extractions_state ON mention_extractions(state)
    WHERE state IN ('pending', 'running');

-- 相关（4d）的台账。dirty 是计数，和 meeting_file_scan 一样防丢更新。copies_json 记这场会自己的
-- 纪要、逐字稿被导出到资料盘的那几份内容。note：no_project / no_transcript / no_roots。
CREATE TABLE IF NOT EXISTS meeting_related_scan (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    sig TEXT NOT NULL DEFAULT '',
    dirty INTEGER NOT NULL DEFAULT 1,
    chunk_mark INTEGER NOT NULL DEFAULT 0,
    windows INTEGER NOT NULL DEFAULT 0,
    passages INTEGER NOT NULL DEFAULT 0,
    copies_json TEXT NOT NULL DEFAULT '[]',
    partial INTEGER NOT NULL DEFAULT 0,
    note TEXT CHECK (note IS NULL OR note IN ('no_project', 'no_transcript', 'no_roots')),
    scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_meeting_related_scan_dirty ON meeting_related_scan(dirty)
    WHERE dirty > 0;
-- 换项目、换逐字稿版本，或原地改逐字稿（拆句、合并、保存草稿不换版本号）时记一笔要重算。
CREATE TRIGGER IF NOT EXISTS meeting_related_scan_dirty_meeting
AFTER UPDATE OF project_id, current_transcript_version_id ON meetings
WHEN NEW.project_id IS NOT OLD.project_id
    OR NEW.current_transcript_version_id IS NOT OLD.current_transcript_version_id
BEGIN
    INSERT INTO meeting_related_scan(meeting_id, dirty) VALUES (NEW.id, 1)
    ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1;
END;
CREATE TRIGGER IF NOT EXISTS meeting_related_scan_dirty_edit
AFTER INSERT ON events
WHEN NEW.meeting_id IS NOT NULL AND NEW.event_type IN (
    'segment_split', 'segments_merged', 'transcript_draft_saved')
BEGIN
    INSERT INTO meeting_related_scan(meeting_id, dirty)
    SELECT id, 1 FROM meetings WHERE id = NEW.meeting_id
    ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1;
END;

-- 逐字稿按 90 秒一窗、每 45 秒一窗，每窗一个向量（float16）和它自己的门槛 bar。能重算，不进备份。
CREATE TABLE IF NOT EXISTS meeting_windows (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text_sha TEXT NOT NULL,
    chars INTEGER NOT NULL,
    bar REAL NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (meeting_id, model, start_ms)
);

-- 每窗最多 3 段对得上的材料。chunk_id 只用来级联删除和取文字，对外的锚点是 (content_key, ordinal)；
-- words 是最多 3 个共同词，seg_ms 是第一个共同词在会上说到的那一句。能重算，不进备份。
CREATE TABLE IF NOT EXISTS meeting_window_passages (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL REFERENCES material_chunks(id) ON DELETE CASCADE,
    content_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    score REAL NOT NULL,
    words TEXT NOT NULL DEFAULT '[]',
    seg_ms INTEGER NOT NULL,
    PRIMARY KEY (meeting_id, start_ms, rank)
);
CREATE INDEX IF NOT EXISTS idx_meeting_window_passages_chunk ON meeting_window_passages(chunk_id);
CREATE INDEX IF NOT EXISTS idx_meeting_window_passages_content
    ON meeting_window_passages(content_key, meeting_id);

-- 离开项目的触发器、时间线、决议对比都按项目找会；决议归需求、决议日志都按会找需求。
CREATE INDEX IF NOT EXISTS idx_meetings_project ON meetings(project_id);
CREATE INDEX IF NOT EXISTS idx_requirement_meetings_meeting ON requirement_meetings(meeting_id);

-- 文件新增、修改、不见的流水，只追加，由 material_files 上的两个触发器写。file_id 不设外键：
-- 文件行删掉以后流水还在，判断挪位置要用。day 是本机日期，at 是 UTC 时刻。
-- (root_id, size, mtime_ns) 给挪位置配对；changed 同一个文件同一天只一行。
CREATE TABLE IF NOT EXISTS material_file_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES project_material_roots(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    dir_rel TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('added', 'changed', 'gone')),
    content_key TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    day TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_material_file_events_root ON material_file_events(root_id, at);
CREATE INDEX IF NOT EXISTS idx_material_file_events_file ON material_file_events(file_id);
CREATE INDEX IF NOT EXISTS idx_material_file_events_day ON material_file_events(root_id, day);
CREATE INDEX IF NOT EXISTS idx_material_file_events_sig
    ON material_file_events(root_id, size, mtime_ns);
CREATE UNIQUE INDEX IF NOT EXISTS idx_material_file_events_changed_day
    ON material_file_events(file_id, day) WHERE kind = 'changed';

-- 只记 normal 区，和 package 区里的 key、pages、numbers；这个根目录第一次整轮（last_full_at 为空，
-- 换了路径以后也会清空）一行都不记。比较一律用 IS：包的 size 是空的。
CREATE TRIGGER IF NOT EXISTS material_file_events_insert
AFTER INSERT ON material_files
WHEN (NEW.zone = 'normal' OR (NEW.zone = 'package' AND NEW.ext IN ('key', 'pages', 'numbers')))
    AND NEW.gone_at IS NULL
    AND EXISTS (SELECT 1 FROM material_index_state s
                 WHERE s.root_id = NEW.root_id AND s.last_full_at IS NOT NULL)
BEGIN
    INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                     size, mtime_ns, day, at)
    VALUES (NEW.root_id, NEW.id, NEW.rel_path, NEW.dir_rel, 'added', NULL, NEW.size, NEW.mtime_ns,
            date('now', 'localtime'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;
-- 只看 size、mtime_ns、gone_at 三列（只填 content_key 不记）。gone_at 从空变有记 gone（带原来的
-- size、mtime 和 content_key），从有变空记 added（又出现了）；活文件的 size 或 mtime 变了记
-- changed（带变之前的 content_key），同一天只一行、保留当天第一次的旧标识。changed 的 day 在 mtime
-- 落在过去 2 天到未来 5 分钟之间时取 mtime 的本机日期。exFAT、FAT 盘换时区会让整盘的修改时间一起
-- 挪整刻钟：大小没变、mtime 差正好是 15 分钟的整数倍时不记。
CREATE TRIGGER IF NOT EXISTS material_file_events_update
AFTER UPDATE OF size, mtime_ns, gone_at ON material_files
WHEN (NEW.zone = 'normal' OR (NEW.zone = 'package' AND NEW.ext IN ('key', 'pages', 'numbers')))
    AND (NEW.size IS NOT OLD.size OR NEW.mtime_ns IS NOT OLD.mtime_ns
         OR NEW.gone_at IS NOT OLD.gone_at)
    AND EXISTS (SELECT 1 FROM material_index_state s
                 WHERE s.root_id = NEW.root_id AND s.last_full_at IS NOT NULL)
BEGIN
    INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                     size, mtime_ns, day, at)
    SELECT NEW.root_id, NEW.id, NEW.rel_path, NEW.dir_rel,
           CASE WHEN NEW.gone_at IS NOT NULL THEN 'gone' ELSE 'added' END,
           CASE WHEN NEW.gone_at IS NOT NULL THEN OLD.content_key END,
           CASE WHEN NEW.gone_at IS NOT NULL THEN OLD.size ELSE NEW.size END,
           CASE WHEN NEW.gone_at IS NOT NULL THEN OLD.mtime_ns ELSE NEW.mtime_ns END,
           date('now', 'localtime'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE (OLD.gone_at IS NULL) != (NEW.gone_at IS NULL);
    INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                     size, mtime_ns, day, at)
    SELECT NEW.root_id, NEW.id, NEW.rel_path, NEW.dir_rel, 'changed', OLD.content_key,
           NEW.size, NEW.mtime_ns,
           CASE WHEN NEW.mtime_ns IS NOT NULL
                 AND NEW.mtime_ns / 1000000000
                     BETWEEN CAST(strftime('%s', 'now') AS INTEGER) - 172800
                         AND CAST(strftime('%s', 'now') AS INTEGER) + 300
                THEN date(NEW.mtime_ns / 1000000000, 'unixepoch', 'localtime')
                ELSE date('now', 'localtime') END,
           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE OLD.gone_at IS NULL AND NEW.gone_at IS NULL
       AND NOT (NEW.size IS OLD.size AND NEW.mtime_ns IS NOT NULL AND OLD.mtime_ns IS NOT NULL
                AND (NEW.mtime_ns - OLD.mtime_ns) % 900000000000 = 0)
    ON CONFLICT(file_id, day) WHERE kind = 'changed'
    DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, at = excluded.at;
END;

-- 4h 从材料里挖出来、等你确认的词。不写进 glossary_terms 当未确认词；［记入］以后才复制过去，
-- term_id 和 undo_json 给 600 秒内撤销用。evidence_json 只存位置和次数。dropped 是证据没了。
CREATE TABLE IF NOT EXISTS glossary_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    term_key TEXT NOT NULL,
    wrong TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0,
    spoken INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'dropped')),
    term_id TEXT,
    undo_json TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, term_key, wrong)
);
CREATE INDEX IF NOT EXISTS idx_glossary_candidates_status ON glossary_candidates(status);

-- 每个项目上次挖词时的签名，变了才重挖。
CREATE TABLE IF NOT EXISTS glossary_mining_scan (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    sig TEXT NOT NULL,
    mined_at TEXT NOT NULL
);

-- 每份材料内容挖出的候选词（最多 24 个，带次数），只在本机；内容被回收时跟着删。
CREATE TABLE IF NOT EXISTS glossary_mining_seeds (
    content_key TEXT PRIMARY KEY REFERENCES material_contents(content_key) ON DELETE CASCADE,
    miner INTEGER NOT NULL,
    source_sig TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    mined_at TEXT NOT NULL
);

-- v17 需求池改版（260930）：需求候选＝AI 从会议纪要里抽出来、还没认领的需求，认领后才建成需求。
-- 不放进 requirements：那张表被关系图、决议、会议卡片、时间线等 18 个模块直接查，候选混进去会到处漏。
-- 候选不存项目，跟着来源会议当前的归属走（认领前会议改了归属，候选随之改）。
-- status：pending 待认领、claimed 已认领、merged 已合并、dropped 已丢掉。处理过的行留着，重新抽取时
-- 据此跳过；requirement_id 是认领建成或合并进去的那条需求。丢掉的 30 天内能撤销，name_key（轻键）
-- 一直留着，「同一项目下同名候选以后不再提示」靠它。similar_requirement_id 是 AI 判断的相近需求。
CREATE TABLE IF NOT EXISTS requirement_candidates (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    extraction_id INTEGER,
    title TEXT NOT NULL,
    name_key TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    similar_requirement_id TEXT REFERENCES requirements(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'merged', 'dropped')),
    requirement_id TEXT REFERENCES requirements(id) ON DELETE SET NULL,
    dropped_at TEXT,
    -- 抽出（或上次核过）时会议的归属：会议事后改了项目，墙面取数时按新项目重核去重
    project_id_seen TEXT,
    -- 丢掉时会议的归属：「同项目丢掉过的不再提示」按它算，会议后来改了归属也不跟着搬家
    dropped_project_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requirement_candidates_meeting
    ON requirement_candidates(meeting_id, status);
CREATE INDEX IF NOT EXISTS idx_requirement_candidates_status
    ON requirement_candidates(status, name_key);

-- v17：需求的来源＝提出它的会议、会上原话和时间锚。属于一条需求或一条候选（二选一），认领、合并时
-- 整行改挂过去。origin 是提出它的那句，每条至多一句；merged 是合并进来的原话，via_candidate_title
-- 记合并自哪条候选。
CREATE TABLE IF NOT EXISTS requirement_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id TEXT REFERENCES requirements(id) ON DELETE CASCADE,
    candidate_id TEXT REFERENCES requirement_candidates(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('origin', 'merged')),
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    quote TEXT NOT NULL DEFAULT '',
    anchor_ms INTEGER,
    via_candidate_title TEXT,
    created_at TEXT NOT NULL,
    CHECK ((requirement_id IS NULL) <> (candidate_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_requirement_sources_requirement
    ON requirement_sources(requirement_id);
CREATE INDEX IF NOT EXISTS idx_requirement_sources_candidate
    ON requirement_sources(candidate_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_sources_origin_requirement
    ON requirement_sources(requirement_id) WHERE kind = 'origin' AND requirement_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_sources_origin_candidate
    ON requirement_sources(candidate_id) WHERE kind = 'origin' AND candidate_id IS NOT NULL;
-- 需求的来源一定在它的关联会议里（选定来源会议时同时关联，R04-4）：一场会从需求的关联会议里
-- 移出（详情页移除、会议页改关联、整组替换、撤销建成需求），这场会的原话也从来源里去掉。
CREATE TRIGGER IF NOT EXISTS requirement_sources_follow_unlink
AFTER DELETE ON requirement_meetings
BEGIN
    DELETE FROM requirement_sources
     WHERE requirement_id = OLD.requirement_id AND meeting_id = OLD.meeting_id;
END;
"""

# 1g：关系图的持久版本号。这些表每次增删改都给 app_state 里的 graph_rev 加一，图接口拿它
# 算 ETag。放在触发器里而不是进程内计数，重启进程、命令行工具写库都能算进去。
GRAPH_REV_KEY = "graph_rev"
GRAPH_REV_TABLES = (
    "meetings",
    "projects",
    "tasks",
    "requirements",
    "requirement_meetings",
    "requirement_folders",
    "project_links",
    "project_material_roots",
    "meeting_cards",
    "glossary_terms",
    # v14：名字的决定和待补建的文件夹会改变图上的「像新项目 / 新需求」和文件夹节点。
    "name_decisions",
    "requirement_name_decisions",
    "pending_project_folders",
    # 2d：会上提到的文件。文件名索引那几张表不进，否则后台每扫一轮都会让关系图缓存失效。
    "meeting_file_mentions",
    # v16：决议画在关系图上；交付物就是确认过的产出线（以前不在这里，标交付物后缓存不失效）。
    # relations 用上面手写的触发器，不放进来。
    "decisions",
    "deliverables",
    "deliverable_files",
)
SCHEMA += "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS graph_rev_{table}_{action.lower()}
AFTER {action} ON {table}
BEGIN
    INSERT INTO app_state(key, value, updated_at)
    VALUES ('{GRAPH_REV_KEY}', '1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(key) DO UPDATE
        SET value = CAST(app_state.value AS INTEGER) + 1, updated_at = excluded.updated_at;
END;
"""
    for table in GRAPH_REV_TABLES
    for action in ("INSERT", "UPDATE", "DELETE")
)


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
            if "also_names" not in project_columns:
                # 元素 {"name": "...", "source": "manual|former"}；former 是改名前的旧名。
                connection.execute(
                    "ALTER TABLE projects ADD COLUMN also_names TEXT NOT NULL DEFAULT '[]'"
                )
            if "seat" not in project_columns:
                # v17：项目座次（需求池「我的方向」），NULL 是未排座次。只管排序和展示，不参与归属判断。
                connection.execute("ALTER TABLE projects ADD COLUMN seat INTEGER")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_seat ON projects(seat) "
                "WHERE seat IS NOT NULL"
            )
            requirement_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(requirements)").fetchall()
            }
            if "summary" not in requirement_columns:
                # v17：需求的说明（一两句话，最多 70 字），可空。
                connection.execute(
                    "ALTER TABLE requirements ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
                )
            link_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(project_links)").fetchall()
            }
            for name in (
                "evidence_json",
                "candidates_json",
                "new_project_name",
                "reason",
                # v14：AI 觉得像是这个项目里的新需求时起的名字、它当时选的项目。
                "new_requirement_name",
                "new_name_project_id",
            ):
                if name not in link_columns:
                    connection.execute(f"ALTER TABLE project_links ADD COLUMN {name} TEXT")
            if "new_name_spoken" not in link_columns:
                # v14：新名字在纪要里的原样写法（JSON 字符串数组，最多 3 个）。
                connection.execute(
                    "ALTER TABLE project_links ADD COLUMN new_name_spoken TEXT NOT NULL DEFAULT '[]'"
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
            if "also" not in glossary_columns:
                # 「也叫」：不改写，只用于识别项目和检索（2–20 字）。
                connection.execute(
                    "ALTER TABLE glossary_terms ADD COLUMN also TEXT NOT NULL DEFAULT '[]'"
                )
            if "is_cue" not in glossary_columns:
                # 项目词是否参与识别会议属于哪个项目。
                connection.execute(
                    "ALTER TABLE glossary_terms ADD COLUMN is_cue INTEGER NOT NULL DEFAULT 1"
                )
            # 建索引放到列存在之后：executescript(SCHEMA) 早于这里执行，SCHEMA 里若
            # 直接带这条 CREATE INDEX，旧库补列之前就会报 "no such column"。
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_glossary_terms_project "
                "ON glossary_terms(project_id)"
            )
            suggestion_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(glossary_suggestions)").fetchall()
            }
            for name in (
                # 2 字错字扩成整词后，原来的 2 字那一对（「只记 2 字」）
                "alt_wrong",
                "alt_correct",
                # 确认时实际写进了哪条词条、记的是哪个错写（撤销确认用）
                "confirmed_term_id",
                "confirmed_wrong",
            ):
                if name not in suggestion_columns:
                    connection.execute(f"ALTER TABLE glossary_suggestions ADD COLUMN {name} TEXT")
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
            if "candidate_id" not in task_columns:
                # v17：挂在需求候选上的任务。认领后随需求走、合并后挂到目标需求、丢掉后清空。
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN candidate_id "
                    "TEXT REFERENCES requirement_candidates(id) ON DELETE SET NULL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_candidate ON tasks(candidate_id)"
            )
            # v17 候选表后补的两列（建过 v17 早期版本的库补上；新库建表时就有）
            candidate_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(requirement_candidates)"
                ).fetchall()
            }
            for name in ("project_id_seen", "dropped_project_id"):
                if name not in candidate_columns:
                    connection.execute(f"ALTER TABLE requirement_candidates ADD COLUMN {name} TEXT")
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
            if current_version < 13:
                # 词典范围与项目打通：scope 与某个项目名精确相等的术语补上 project_id，
                # 之后 project_id 才是范围的唯一来源、scope 变成随项目改名同步的派生标签。
                # 名字不一致的旧桶（如「云图」vs 项目名「云图科研用药」）不在此处处理，
                # 交给一次性 SQL 按业务口径迁移。v10 首次执行；v13 再跑一遍，补上 v10 之后
                # 确认纠错词时只写了 scope 的孤儿词。
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
            if current_version < 13:
                # 任务跟会议走：已归项目的会议下，还没挂项目也没挂需求的草稿/过期任务补上
                # 会议的项目。已确认的任务可能是人工清空过项目，不动。
                connection.execute(
                    """UPDATE tasks
                          SET project_id = (
                              SELECT project_id FROM meetings WHERE meetings.id = tasks.meeting_id
                          ),
                              updated_at = ?
                        WHERE project_id IS NULL
                          AND requirement_id IS NULL
                          AND status IN ('pending_confirm', 'expired')
                          AND meeting_id IN (
                              SELECT id FROM meetings WHERE project_id IS NOT NULL
                          )""",
                    (utc_now(),),
                )
                # 纪要全文索引首次建立：先清空再按当前纪要全量回填，重复执行结果不变。
                connection.execute("DELETE FROM minutes_fts")
                connection.execute(
                    """INSERT INTO minutes_fts(meeting_id, text)
                       SELECT m.id, mv.markdown
                         FROM meetings m
                         JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id"""
                )
            file_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(material_files)").fetchall()
            }
            for name, declaration in (
                # v15 / 3a：内容标识和算它那一刻的大小、修改时间；只和这一处文件有关的出错
                # （permission / io / corrupt / unsupported）、IO 错第几次、上次检查的时间。
                ("content_key", "TEXT"),
                ("content_size", "INTEGER"),
                ("content_mtime_ns", "INTEGER"),
                ("content_error", "TEXT"),
                ("content_attempts", "INTEGER"),
                ("content_checked_at", "TEXT"),
            ):
                if name not in file_columns:
                    connection.execute(
                        f"ALTER TABLE material_files ADD COLUMN {name} {declaration}"
                    )
            dir_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(material_dirs)").fetchall()
            }
            if "symlinks" not in dir_columns:
                connection.execute(
                    "ALTER TABLE material_dirs ADD COLUMN symlinks INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_material_files_content "
                "ON material_files(content_key)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_material_files_mtime "
                "ON material_files(root_id, mtime_ns)"
            )
            # 会议卡片（1c）默认开启，但只对这之后新生成纪要的会自动写；上线前的历史会议
            # 等工作台横幅问过再补写。两个键都只在第一次启动时写入，之后不再改。
            now = utc_now()
            connection.execute(
                """INSERT OR IGNORE INTO app_state(key, value, updated_at)
                   VALUES ('cards_enabled', '1', ?)""",
                (now,),
            )
            connection.execute(
                """INSERT OR IGNORE INTO app_state(key, value, updated_at)
                   VALUES ('cards_since', ?, ?)""",
                (now, now),
            )
            # v15：认字引擎（auto / vision / tesseract / off），命令 materials ocr-engine 改。
            connection.execute(
                """INSERT OR IGNORE INTO app_state(key, value, updated_at)
                   VALUES ('ocr_engine', 'auto', ?)""",
                (now,),
            )
            # v16：表和触发器都在 SCHEMA 里，不回填，积压由两个循环慢慢补；这里只写两个键。
            # links_since 是第一次用上第四期的时间，写一次、之后不改；related_rev 是相关自己的版本号。
            connection.execute(
                """INSERT OR IGNORE INTO app_state(key, value, updated_at)
                   VALUES ('links_since', ?, ?)""",
                (now, now),
            )
            connection.execute(
                """INSERT OR IGNORE INTO app_state(key, value, updated_at)
                   VALUES ('related_rev', '0', ?)""",
                (now,),
            )
            # v17：需求候选只从这之后建的抽取批次里出，上线前的历史会议不自动回填（260804 任务抽取上线时
            # 出过存量轰炸）；历史会议在会议详情手动补抽。写一次、之后不改。
            connection.execute(
                """INSERT OR IGNORE INTO app_state(key, value, updated_at)
                   VALUES ('requirement_candidates_since', ?, ?)""",
                (now, now),
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
