"""任务代办、AI 抽取与项目看板 API 测试（260804 新增）。"""
from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.main import create_app
from meeting_workbench.relay_client import RelayUnavailable
from meeting_workbench.tasks import TaskService

from .helpers import seed_editable_meeting


class FakeRelayClient:
    def __init__(self):
        pass

    def health(self):
        return {"status": "healthy", "worker": {"state": "idle"}, "counts": {}}

    def list_jobs(self, *, status=None, limit=200):
        return []

    def status(self, job_id):
        raise RelayUnavailable("no job")

    def set_substate(self, job_id, name, target, attempt=1):
        return {"job_id": job_id, "substates": {name: {"status": target}}}


def make_client(tmp_path, material_browse_root=None):
    # 测试里 _call_llm 都被 monkeypatch，但 extract_pending 的无 key 短路
    # 检查真实文件，所以要放一个假 key。
    key_file = tmp_path / "api-key"
    key_file.write_text("test-key", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        lark_webhook_url="",
        llm_api_key_file=key_file,
        # 需求层材料目录测试要挂一个可控的浏览根；不传时用 tmp_path 下的占位目录，
        # 不影响原有不碰材料接口的测试（默认值 /Volumes/资料盘 在 CI 沙箱里本就不存在）。
        material_browse_root=material_browse_root or (tmp_path / "materials-browse-root"),
    )
    relay = FakeRelayClient()
    return TestClient(create_app(settings, relay)), settings


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": "http://testserver"}


def seed_minutes(client, settings, meeting_id="vm-20260102-101500", markdown="# 摘要\n拍板做 X。"):
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-1', ?, 1, ?, '<p></p>', 'generated', 1, ?)""",
        (meeting_id, markdown, utc_now()),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-1' WHERE id=?", (meeting_id,))


def test_manual_task_lifecycle(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)

    created = client.post(
        "/api/tasks",
        json={"title": "测试任务", "detail": "描述", "assignee": "me"},
        headers=headers,
    )
    assert created.status_code == 200
    task = created.json()
    assert task["status"] == "confirmed"
    assert task["origin"] == "manual"
    task_id = task["id"]

    listing = client.get("/api/tasks").json()
    assert listing["total"] == 1

    # 合法流转 confirmed -> in_progress -> done
    assert client.post(
        f"/api/tasks/{task_id}/status", json={"status": "in_progress"}, headers=headers
    ).json()["status"] == "in_progress"
    assert client.post(
        f"/api/tasks/{task_id}/status", json={"status": "done"}, headers=headers
    ).json()["status"] == "done"
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["status"] == "done"
    assert any(e["kind"] == "status_changed" for e in detail["events"])


def test_status_transition_rejected(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, status_changed_at, created_at, updated_at)
           VALUES ('t-p1', '草稿1', 'pending_confirm', 'ai', 'ai', ?, ?, ?)""",
        (utc_now(), utc_now(), utc_now()),
    )
    # 待确认 -> done 是非法迁移
    response = client.post(
        "/api/tasks/t-p1/status", json={"status": "done"}, headers=headers
    )
    assert response.status_code == 409
    # 同状态幂等不报错
    response = client.post(
        "/api/tasks/t-p1/status", json={"status": "pending_confirm"}, headers=headers
    )
    assert response.status_code == 200


def test_confirm_with_edits(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    client.post("/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers)
    # 「保存并确认」针对的是待确认草稿。手工新建的任务一创建就是已确认，拿它再确认
    # 本就不该多记一条「已确认」事件（260914 验收：重复确认会让撤销打回旧任务），
    # 那种情况由 test_tasks_review.py 的 test_repeat_confirm_with_edits_only_records_edit 覆盖。
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, status_changed_at, created_at, updated_at)
           VALUES ('t-draft', '原标题', 'pending_confirm', 'ai', 'me', ?, ?, ?)""",
        (utc_now(), utc_now(), utc_now()),
    )
    confirmed = client.post(
        "/api/tasks/t-draft/confirm",
        json={"title": "改后标题", "assignee": "ai"},
        headers=headers,
    ).json()
    assert confirmed["title"] == "改后标题"
    assert confirmed["assignee"] == "ai"
    assert confirmed["status"] == "confirmed"
    assert any(e["kind"] == "confirmed" for e in confirmed["events"])


def test_confirm_no_longer_creates_suggested_project(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, suggested_project_name,
            status_changed_at, created_at, updated_at)
           VALUES ('t-1', '新事项', 'pending_confirm', 'ai', 'ai', '新项目',
                   ?, ?, ?)""",
        (utc_now(), utc_now(), utc_now()),
    )
    projects_before = client.get("/api/projects").json()

    confirmed = client.post("/api/tasks/t-1/confirm", json={}, headers=headers).json()

    assert confirmed["status"] == "confirmed"
    assert confirmed["project_id"] is None
    assert client.get("/api/projects").json() == projects_before
    # 建议名保留，以后建出同名项目时还能挂过去
    assert confirmed["suggested_project_name"] == "新项目"


def test_confirm_with_explicit_null_project_clears_suggestion(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project = client.post("/api/projects", json={"name": "云图AI"}, headers=headers).json()
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, project_id, suggested_project_name,
            status_changed_at, created_at, updated_at)
           VALUES ('t-1', '新事项', 'pending_confirm', 'ai', 'ai', ?, '新项目', ?, ?, ?)""",
        (project["id"], utc_now(), utc_now(), utc_now()),
    )

    confirmed = client.post(
        "/api/tasks/t-1/confirm", json={"project_id": None}, headers=headers
    ).json()

    assert confirmed["project_id"] is None
    assert confirmed["suggested_project_name"] is None


def test_update_with_explicit_null_project_clears_it(tmp_path):
    client, _settings = make_client(tmp_path)
    headers = write_headers(client)
    project = client.post("/api/projects", json={"name": "云图AI"}, headers=headers).json()
    task = client.post(
        "/api/tasks", json={"title": "整理需求", "project_id": project["id"]}, headers=headers
    ).json()
    assert task["project_id"] == project["id"]

    untouched = client.patch(
        f"/api/tasks/{task['id']}", json={"title": "整理需求 v2"}, headers=headers
    ).json()
    assert untouched["project_id"] == project["id"]

    cleared = client.patch(
        f"/api/tasks/{task['id']}", json={"project_id": None}, headers=headers
    ).json()
    assert cleared["status"] == "confirmed"
    assert cleared["project_id"] is None


def test_batch_confirm_skips_non_pending(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    done_id = client.post("/api/tasks", json={"title": "已确认"}, headers=headers).json()["id"]
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, status_changed_at, created_at, updated_at)
           VALUES ('t-p1', '草稿1', 'pending_confirm', 'ai', 'ai', ?, ?, ?)""",
        (utc_now(), utc_now(), utc_now()),
    )
    result = client.post(
        "/api/tasks/batch-confirm", json={"task_ids": [done_id, "t-p1"]}, headers=headers
    ).json()
    assert result["confirmed"] == ["t-p1"]
    # done_id 不在待确认状态，不应被确认列表返回（确认动作忽略）
    assert done_id not in result["confirmed"]


def test_comment_and_deliverable_mark_done(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    task_id = client.post("/api/tasks", json={"title": "A"}, headers=headers).json()["id"]

    commented = client.post(
        f"/api/tasks/{task_id}/comments", json={"body": "补充一句"}, headers=headers
    ).json()
    assert any(e["kind"] == "comment" for e in commented["events"])

    delivered = client.post(
        f"/api/tasks/{task_id}/deliverables",
        json={"kind": "figma", "url": "figma.com/x", "title": "原型", "mark_done": True},
        headers=headers,
    ).json()
    assert delivered["status"] == "done"
    assert delivered["deliverables"][0]["kind"] == "figma"


def test_project_stats_and_board(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    seed_editable_meeting(Database(settings.database_path), settings.archive_root)
    seed_minutes(client, settings)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    # 会议挂进项目
    client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"project_id": project_id},
        headers=headers,
    )
    # 一条任务挂项目，另一条不挂
    client.post(
        "/api/tasks", json={"title": "在项目里", "project_id": project_id}, headers=headers
    )
    client.post("/api/tasks", json={"title": "不在项目里"}, headers=headers)

    projects = client.get("/api/projects").json()
    project = next(p for p in projects if p["id"] == project_id)
    assert project["meeting_count"] == 1
    assert project["task_count"] == 1

    board = client.get(f"/api/projects/{project_id}/board").json()
    assert board["meeting_count"] == 1
    assert any(
        meeting["title"] == "未关联会议"
        and any(task["title"] == "在项目里" for task in meeting["tasks"])
        for meeting in board["meetings"]
    )
    assert board["glossary_count"] == 0
    assert board["glossary_terms"] == []


def test_project_board_includes_glossary_count_and_preview(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    for term in ("云图A", "云图B", "云图C"):
        client.post(
            "/api/glossary/terms",
            json={"term": term, "aliases": ["别名"], "category": "术语", "project_id": project_id},
            headers=headers,
        )
    # 挂了别的项目的术语不该混进来
    other_project_id = client.post(
        "/api/projects", json={"name": "ACME", "color": "#111111"}, headers=headers
    ).json()["id"]
    client.post(
        "/api/glossary/terms",
        json={"term": "ACME专用词", "project_id": other_project_id},
        headers=headers,
    )

    board = client.get(f"/api/projects/{project_id}/board").json()
    assert board["glossary_count"] == 3
    assert {t["term"] for t in board["glossary_terms"]} == {"云图A", "云图B", "云图C"}
    assert board["glossary_terms"][0]["aliases"] == ["别名"]


def test_update_project_rename_syncs_glossary_scope_and_snapshot(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    client.post(
        "/api/glossary/terms",
        json={"term": "领药码", "project_id": project_id},
        headers=headers,
    )

    renamed = client.patch(
        f"/api/projects/{project_id}", json={"name": "云图科研用药"}, headers=headers
    )
    assert renamed.status_code == 200

    terms = client.get("/api/glossary/terms").json()
    assert terms[0]["scope"] == "云图科研用药"
    assert terms[0]["project_name"] == "云图科研用药"

    snapshot = client.get("/api/glossary/snapshot").json()
    assert snapshot["terms"][0]["scope"] == "云图科研用药"


def test_patch_project_id_marks_manual_origin(tmp_path):
    """人工改会议项目（含清空）一律写 manual，与会议自动归属项目的 ai 标记互斥。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    seed_editable_meeting(Database(settings.database_path), settings.archive_root)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]

    client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"project_id": project_id},
        headers=headers,
    )
    detail = client.get("/api/meetings/vm-20260102-101500").json()
    assert detail["project_id"] == project_id
    assert detail["project_origin"] == "manual"

    client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"project_id": ""},
        headers=headers,
    )
    detail = client.get("/api/meetings/vm-20260102-101500").json()
    assert detail["project_id"] is None
    assert detail["project_origin"] == "manual"


def test_re_extract_creates_pending_tasks(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)

    prompts: list[str] = []

    def fake_llm(self, prompt):
        prompts.append(prompt)
        return (
            '{"tasks":[{"title":"做看板","detail":"按口径表","anchor_quote":"第一段内容",'
            '"assignee_suggestion":"ai","project_match":null,"suggested_project_name":"云图看板"}]}'
        )

    monkeypatch.setattr(TaskService, "_call_llm", fake_llm)
    result = client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract",
        json={"supplement": "参考项目经理分工清单"},
        headers=headers,
    ).json()
    assert result["status"] == "done"
    # 任务不再单独猜项目：提示词里没有项目判断，返回的建议名也不落库
    assert "project_match" not in prompts[0]
    assert "suggested_project_name" not in prompts[0]

    listing = client.get("/api/tasks").json()
    assert listing["total"] == 1
    task = listing["items"][0]
    assert task["status"] == "pending_confirm"
    assert task["origin"] == "ai"
    assert task["meeting_title"] == "需求复盘会"
    # anchor 定位到「第一段内容」的 segment（start_ms=1000）
    assert task["anchor_ms"] == 1000
    assert task["anchor_quote"] == "第一段内容"
    assert task["project_id"] is None
    assert task["suggested_project_name"] is None

    # 补充上下文重新生成会替换旧草稿
    result2 = client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract",
        json={"supplement": "再来一轮"},
        headers=headers,
    ).json()
    assert result2["status"] == "done"
    assert client.get("/api/tasks").json()["total"] == 1


def test_re_extract_after_scan_seed_does_not_hit_unique_index(tmp_path, monkeypatch):
    """回归：scan 已给空 supplement 预 seed 后，用户再点「重新抽取」（空 supplement）不得撞唯一索引报 500。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)

    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: '{"tasks":[]}')
    # 模拟 scan_loop 已 seed：先跑一次 extract_pending 触发 _seed_extractions
    TaskService(db, settings).extract_pending()
    # 用户重新抽取（空 supplement），此前会撞唯一索引 (meeting, version, '')
    result = client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract",
        json={"supplement": ""},
        headers=headers,
    )
    assert result.status_code == 200
    assert result.json()["status"] == "done"


def test_extract_pending_seeds_from_new_minutes(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    extracted = {}

    def fake_llm(self, prompt):
        extracted["prompt"] = prompt
        return '{"tasks":[{"title":"自动抽取","anchor_quote":"第二段内容",' \
               '"assignee_suggestion":"me","project_match":null,"suggested_project_name":null}]}'

    monkeypatch.setattr(TaskService, "_call_llm", fake_llm)
    db.execute(
        """INSERT INTO task_extractions(meeting_id, minutes_version_id, supplement, created_at)
           VALUES ('vm-20260102-101500', 'mv-1', '', ?)""",
        (utc_now(),),
    )
    stats = TaskService(db, settings).extract_pending()
    assert stats["succeeded"] == 1
    task = client.get("/api/tasks").json()["items"][0]
    assert task["title"] == "自动抽取"
    assert task["anchor_ms"] == 3200  # 「第二段内容」segment 起始
    assert "会议纪要" in extracted["prompt"]


def test_parse_llm_tasks_tolerates_fence(tmp_path):
    from meeting_workbench.tasks import TaskService

    payload = TaskService._parse_llm_tasks(
        '```json\n{"tasks": [{"title": "A"}]}\n```'
    )
    assert payload["tasks"][0]["title"] == "A"
    payload2 = TaskService._parse_llm_tasks('{"tasks": [{"title": "B"}')
    assert payload2["tasks"][0]["title"] == "B"


def test_parse_llm_tasks_drops_trailing_garbage_and_non_dict_items(tmp_path):
    from meeting_workbench.tasks import TaskService

    payload = TaskService._parse_llm_tasks(
        '{"tasks": [{"title": "A"}]} 这里是模型多余追加的解释文字，不是 JSON。'
    )
    assert payload["tasks"][0]["title"] == "A"

    payload2 = TaskService._parse_llm_tasks(
        '{"tasks": [{"title": "B"}, "驳回", {"title": "C"}]}'
    )
    assert [task["title"] for task in payload2["tasks"]] == ["B", "C"]


def test_extract_skips_non_dict_task_and_keeps_valid_ones(tmp_path, monkeypatch):
    """回归：LLM 偶发在 tasks 数组里混入非对象元素时，只跳过那一条，其余任务照常入库。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)

    monkeypatch.setattr(
        TaskService,
        "_call_llm",
        lambda self, prompt: (
            '{"tasks":[{"title":"任务A","anchor_quote":"第一段内容"},'
            '"驳回",'
            '{"title":"任务B","anchor_quote":"第二段内容"}]}'
        ),
    )
    result = client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract",
        json={},
        headers=headers,
    ).json()

    assert result["status"] == "done"
    titles = {task["title"] for task in client.get("/api/tasks").json()["items"]}
    assert titles == {"任务A", "任务B"}


def test_bootstrap_reports_pending_count(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, status_changed_at, created_at, updated_at)
           VALUES ('t-p1', '草稿1', 'pending_confirm', 'ai', 'ai', ?, ?, ?)""",
        (utc_now(), utc_now(), utc_now()),
    )
    payload = client.get("/api/bootstrap").json()
    assert payload["pending_confirm_count"] == 1
    assert payload["mobile_task_write"] is True


def test_first_seed_skips_backlog_then_extracts_new(tmp_path, monkeypatch):
    """回归：v7 迁移上线时，存量纪要不得自动全量抽取；之后的新纪要正常抽。"""
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)

    monkeypatch.setattr(
        TaskService, "_call_llm",
        lambda self, prompt: '{"tasks":[{"title":"不该出现的存量任务"}]}',
    )
    service = TaskService(db, settings)
    stats = service.extract_pending()
    assert stats["started"] == 0
    row = db.query_one(
        "SELECT status FROM task_extractions WHERE meeting_id='vm-20260102-101500'"
    )
    assert row["status"] == "skipped"
    assert client.get("/api/tasks").json()["total"] == 0

    # 上线后的新纪要正常进入自动抽取
    db.execute(
        """INSERT INTO meetings (id, title, status, created_at, updated_at)
           VALUES ('vm-20260890-090000', '上线后的新会', 'completed_unreviewed', ?, ?)""",
        (utc_now(), utc_now()),
    )
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-new', 'vm-20260890-090000', 1, '# 摘要', '<p></p>', 'generated', 1, ?)""",
        (utc_now(),),
    )
    db.execute(
        "UPDATE meetings SET current_minutes_version_id='mv-new' WHERE id='vm-20260890-090000'"
    )
    monkeypatch.setattr(
        TaskService, "_call_llm",
        lambda self, prompt: '{"tasks":[{"title":"新会任务"}]}',
    )
    stats2 = service.extract_pending()
    assert stats2["succeeded"] == 1
    items = client.get("/api/tasks").json()["items"]
    assert [task["title"] for task in items] == ["新会任务"]


def test_recover_uses_claim_time_not_created_time(tmp_path):
    """回归：超时回收判据必须是认领时刻；用批次创建时间会把重试批次误杀双跑。"""
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    service = TaskService(db, settings)
    old = "2026-08-04T00:00:00+00:00"
    db.execute(
        """INSERT INTO task_extractions
           (meeting_id, minutes_version_id, supplement, status, created_at, claimed_at)
           VALUES ('vm-20260102-101500', 'mv-1', 'fresh-claim', 'running', ?, ?)""",
        (old, utc_now()),
    )
    db.execute(
        """INSERT INTO task_extractions
           (meeting_id, minutes_version_id, supplement, status, created_at, claimed_at)
           VALUES ('vm-20260102-101500', 'mv-1', 'stale-claim', 'running', ?, ?)""",
        (old, old),
    )
    service._recover_stalled_extractions()
    fresh = db.query_one(
        "SELECT status FROM task_extractions WHERE supplement='fresh-claim'"
    )
    stale = db.query_one(
        "SELECT status, claimed_at FROM task_extractions WHERE supplement='stale-claim'"
    )
    assert fresh["status"] == "running"
    assert stale["status"] == "pending"
    assert stale["claimed_at"] is None


def test_re_extract_row_is_running_during_llm_call(tmp_path, monkeypatch):
    """回归：re_extract 的批次行在 LLM 调用期间必须已是 running，scan 不得并发认领。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    observed = {}

    def probing_llm(self, prompt):
        observed["status"] = db.query_one(
            "SELECT status, claimed_at FROM task_extractions ORDER BY id DESC LIMIT 1"
        )
        return '{"tasks":[]}'

    monkeypatch.setattr(TaskService, "_call_llm", probing_llm)
    response = client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract",
        json={"supplement": "并发探测"},
        headers=headers,
    )
    assert response.status_code == 200
    assert observed["status"]["status"] == "running"
    assert observed["status"]["claimed_at"] is not None


def test_digest_due_any_time_after_nine(tmp_path, monkeypatch):
    """回归：晨报判据是「当天 >= 09:00」，精确到分钟会被慢扫描轮跨过而漏发。"""
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    service = TaskService(db, settings)
    import meeting_workbench.tasks as tasks_mod

    class _Now:
        def __init__(self, hour, minute):
            self.hour = hour
            self.minute = minute

        def astimezone(self):
            return self

    for hour, minute, expected in [(8, 59, False), (9, 0, True), (14, 30, True)]:
        monkeypatch.setattr(
            tasks_mod,
            "datetime",
            type("_D", (), {"now": staticmethod(lambda h=hour, m=minute: _Now(h, m))}),
        )
        assert service._digest_due() is expected
