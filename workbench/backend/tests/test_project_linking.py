"""会议自动归属项目测试（260905 新增）。"""
import uuid

import numpy as np

from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench import project_linking as project_linking_module
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.semantic import SemanticIndex


class FakeEmbedder:
    """按关键词分桶的假嵌入，语义匹配测试用（同 test_semantic.py 的写法）。"""

    def encode(self, texts, **_kwargs):
        vectors = []
        for text in texts:
            if "随访" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "营养" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return np.asarray(vectors, dtype=np.float32)


def make_db(tmp_path, *, with_key: bool = True) -> tuple[Database, Settings]:
    if with_key:
        key_file = tmp_path / "api-key"
        key_file.write_text("test-key", encoding="utf-8")
    else:
        key_file = tmp_path / "missing-key"
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        llm_api_key_file=key_file,
    )
    db = Database(settings.database_path)
    db.initialize()
    return db, settings


def seed_meeting(db: Database, meeting_id: str, title: str, markdown: str = "# 摘要\n拍板做 X。") -> str:
    now = utc_now()
    db.execute(
        "INSERT INTO meetings(id, title, status, created_at, updated_at) "
        "VALUES (?, ?, 'completed_unreviewed', ?, ?)",
        (meeting_id, title, now, now),
    )
    version_id = f"mv-{meeting_id}"
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES (?, ?, 1, ?, '<p></p>', 'generated', 1, ?)""",
        (version_id, meeting_id, markdown, now),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (version_id, meeting_id))
    return version_id


def make_project(db: Database, name: str) -> str:
    project_id = f"project-{uuid.uuid4().hex[:12]}"
    db.execute(
        "INSERT INTO projects(id, name, color, origin, created_at) VALUES (?, ?, '#2c8d83', 'manual', ?)",
        (project_id, name, utc_now()),
    )
    return project_id


def make_task(db: Database, meeting_id: str, project_id: str | None, status: str = "confirmed") -> None:
    task_id = f"task-{uuid.uuid4().hex}"
    now = utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, detail, status, origin, assignee, meeting_id, project_id,
            status_changed_at, created_at, updated_at)
           VALUES (?, '任务', '', ?, 'ai', 'ai', ?, ?, ?, ?, ?)""",
        (task_id, status, meeting_id, project_id, now, now, now),
    )


def test_llm_tier_high_confidence_matches(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-1", "云图周会")
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":"云图","confidence":"high","reason":"标题即项目名"}',
    )

    linker = ProjectLinker(db, settings)
    stats = linker.link_pending()

    assert stats == {
        "started": 1, "linked": 1, "unresolved": 0, "failed": 0, "skipped_no_key": 0,
        "items": stats["items"],
    }
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-1'")
    assert meeting["project_id"] == project_id
    assert meeting["project_origin"] == "ai"
    link = db.query_one("SELECT status, method, project_id FROM project_links WHERE meeting_id='vm-1'")
    assert link == {"status": "done", "method": "llm", "project_id": project_id}
    event = db.query_one(
        "SELECT event_type, payload_json FROM events WHERE event_type='meeting_project_auto_assigned'"
    )
    assert event is not None
    assert '"method": "llm"' in event["payload_json"] or '"method":"llm"' in event["payload_json"]


def test_llm_low_confidence_not_adopted_falls_through_to_majority(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_a = make_project(db, "A项目")
    make_project(db, "B项目")
    seed_meeting(db, "vm-2", "跨项目周会")
    make_task(db, "vm-2", project_a, status="confirmed")
    make_task(db, "vm-2", project_a, status="in_progress")
    make_task(db, "vm-2", None, status="cancelled")  # 取消的任务不计入分母
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":null,"confidence":"low","reason":"内容笼统"}',
    )

    linker = ProjectLinker(db, settings)
    stats = linker.link_pending()

    assert stats["linked"] == 1
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-2'")
    assert meeting["project_id"] == project_a
    assert meeting["project_origin"] == "ai"
    link = db.query_one("SELECT method FROM project_links WHERE meeting_id='vm-2'")
    assert link["method"] == "task_majority"


def test_semantic_tier_matches_without_llm_key(tmp_path):
    """无 key 时级②③仍照常跑：语义兜底命中即写库（沙盒验收对应场景）。"""
    db, settings = make_db(tmp_path, with_key=False)
    project_id = make_project(db, "随访项目")
    seed_meeting(db, "vm-3", "随访计划沟通会")

    linker = ProjectLinker(db, settings, semantic=SemanticIndex(db, settings, embedder=FakeEmbedder()))
    stats = linker.link_pending()

    assert stats["linked"] == 1
    assert stats["skipped_no_key"] == 0
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-3'")
    assert meeting["project_id"] == project_id
    assert meeting["project_origin"] == "ai"
    link = db.query_one("SELECT method FROM project_links WHERE meeting_id='vm-3'")
    assert link["method"] == "semantic"


def test_task_majority_tier_requires_fifty_percent(tmp_path):
    db, settings = make_db(tmp_path, with_key=False)
    project_id = make_project(db, "多数项目")
    make_project(db, "少数项目")
    seed_meeting(db, "vm-4", "普通例会")
    make_task(db, "vm-4", project_id, status="confirmed")
    make_task(db, "vm-4", project_id, status="confirmed")
    make_task(db, "vm-4", None, status="pending_confirm")  # 没项目也计入分母，拉低占比

    linker = ProjectLinker(db, settings)
    stats = linker.link_pending()

    # 2/3 = 66.7% ≥ 50%，命中
    assert stats["linked"] == 1
    meeting = db.query_one("SELECT project_id FROM meetings WHERE id='vm-4'")
    assert meeting["project_id"] == project_id


def test_task_majority_tier_below_threshold_is_unresolved_when_llm_tried(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "多数项目")
    seed_meeting(db, "vm-5", "普通例会")
    make_task(db, "vm-5", project_id, status="confirmed")
    make_task(db, "vm-5", None, status="pending_confirm")
    make_task(db, "vm-5", None, status="pending_confirm")
    # 1/3 = 33% < 50%，多数判据不采信
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":null,"confidence":"low","reason":"占比不足"}',
    )

    linker = ProjectLinker(db, settings)
    stats = linker.link_pending()

    assert stats["unresolved"] == 1
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-5'")
    assert meeting["project_id"] is None
    assert meeting["project_origin"] is None
    link = db.query_one("SELECT status FROM project_links WHERE meeting_id='vm-5'")
    assert link["status"] == "unresolved"
    event = db.query_one(
        "SELECT event_type FROM events WHERE event_type='meeting_project_unresolved'"
    )
    assert event is not None


def test_no_key_and_no_signal_reverts_to_pending_not_unresolved(tmp_path):
    """没 key 时②③也没答案：不抢先判 unresolved，原样放回 pending 等 key 配好。"""
    db, settings = make_db(tmp_path, with_key=False)
    seed_meeting(db, "vm-6", "无从判断的会")

    linker = ProjectLinker(db, settings)
    stats = linker.link_pending()

    assert stats == {
        "started": 1, "linked": 0, "unresolved": 0, "failed": 0, "skipped_no_key": 1,
        "items": stats["items"],
    }
    link = db.query_one(
        "SELECT status, attempts, claimed_at FROM project_links WHERE meeting_id='vm-6'"
    )
    assert link["status"] == "pending"
    assert link["attempts"] == 0
    assert link["claimed_at"] is None
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-6'")
    assert meeting["project_id"] is None
    assert meeting["project_origin"] is None


def test_manual_assignment_never_reseeded_or_overwritten(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-7", "云图周会")
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='manual' WHERE id='vm-7'", (project_id,)
    )
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":"云图","confidence":"high","reason":"x"}',
    )

    linker = ProjectLinker(db, settings)
    seeded = linker.seed()
    stats = linker.link_pending()

    assert seeded == 0
    assert stats["started"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM project_links WHERE meeting_id='vm-7'")["n"] == 0
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-7'")
    assert meeting["project_id"] == project_id
    assert meeting["project_origin"] == "manual"


def test_unresolved_same_minutes_version_not_reseeded_new_version_reseeded(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    seed_meeting(db, "vm-8", "无法判断的会")
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":null,"confidence":"low","reason":"x"}',
    )
    linker = ProjectLinker(db, settings)
    linker.link_pending()
    assert db.query_one(
        "SELECT status FROM project_links WHERE meeting_id='vm-8'"
    )["status"] == "unresolved"

    # 同一份纪要版本不再重跑
    reseeded = linker.seed()
    assert reseeded == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM project_links WHERE meeting_id='vm-8'")["n"] == 1

    # 纪要重新生成出新版本后会再跑一次
    now = utc_now()
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-vm-8-v2', 'vm-8', 2, '# 新版摘要', '<p></p>', 'generated', 1, ?)""",
        (now,),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-vm-8-v2' WHERE id='vm-8'")
    reseeded = linker.seed()
    assert reseeded == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM project_links WHERE meeting_id='vm-8'")["n"] == 2


def test_backfill_dry_run_does_not_write(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-9", "云图周会")
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":"云图","confidence":"high","reason":"x"}',
    )

    linker = ProjectLinker(db, settings)
    result = linker.backfill(dry_run=True)

    assert result["dry_run"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["project_id"] == project_id
    assert result["results"][0]["method"] == "llm"
    # 不写库：会议、project_links 都不应该有变化
    assert db.query_one("SELECT project_id FROM meetings WHERE id='vm-9'")["project_id"] is None
    assert db.query_one("SELECT COUNT(*) AS n FROM project_links")["n"] == 0


def test_backfill_writes_and_summarizes(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图")
    seed_meeting(db, "vm-10", "云图周会")
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":"云图","confidence":"high","reason":"x"}',
    )

    linker = ProjectLinker(db, settings)
    result = linker.backfill(dry_run=False)

    assert result["method_counts"] == {"llm": 1}
    assert result["unresolved"] == 0
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-10'")
    assert meeting["project_id"] == project_id
    assert meeting["project_origin"] == "ai"


def test_semantic_tier_skipped_when_llm_answered_low(tmp_path, monkeypatch):
    """LLM 读过全文并明确说 low 时，只看标题向量的语义兜底不能推翻它。"""
    db, settings = make_db(tmp_path)
    make_project(db, "随访项目")
    seed_meeting(db, "vm-4", "随访计划沟通会")
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":null,"confidence":"low","reason":"公司级分享"}',
    )

    linker = ProjectLinker(db, settings, semantic=SemanticIndex(db, settings, embedder=FakeEmbedder()))
    stats = linker.link_pending()

    assert stats["linked"] == 0
    assert stats["unresolved"] == 1
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-4'")
    assert meeting["project_id"] is None
    assert meeting["project_origin"] is None


def test_new_project_reopens_unresolved_meetings(tmp_path, monkeypatch):
    """项目表多了新项目后，之前判不归属的会议在下一轮重新判一次；已归属的不受影响。"""
    from meeting_workbench.tasks import TaskService

    db, settings = make_db(tmp_path)
    seed_meeting(db, "vm-5", "微课堂扫码签到沟通")
    answers = iter(
        [
            '{"project_match":null,"confidence":"low","reason":"列表里没有微课堂"}',
            '{"project_match":"微课堂","confidence":"high","reason":"新建的项目正好对上"}',
        ]
    )
    monkeypatch.setattr(
        project_linking_module, "call_llm", lambda settings, prompt, *, system: next(answers)
    )
    linker = ProjectLinker(db, settings)
    assert linker.link_pending()["unresolved"] == 1

    project = TaskService(db, settings).create_project(name="微课堂", color="#123456")
    assert db.query_one("SELECT COUNT(*) AS n FROM project_links")["n"] == 0

    stats = linker.link_pending()

    assert stats["linked"] == 1
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-5'")
    assert meeting["project_id"] == project["id"]
    assert meeting["project_origin"] == "ai"
