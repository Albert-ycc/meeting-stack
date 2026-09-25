"""待确认闸门减负（260914）：草稿自动过期、服务端计数、批量驳回、撤销、抽取收窄。

背景：747 条 AI 任务里待确认积压 383 条（205 条超过 7 天），两周无人处理；
任务池前端只拉 500 条自己数，「已完成」页签显示 0 而库里有 73 条。
"""
from datetime import UTC, datetime, timedelta

from meeting_workbench import tasks as tasks_module
from meeting_workbench.db import Database, utc_now
from meeting_workbench.tasks import TaskService

from .helpers import seed_editable_meeting
from .test_tasks_api import make_client, seed_minutes, write_headers


def insert_task(
    db, task_id, status, *,
    changed_at=None, project_id=None, meeting_id=None, requirement_id=None, assignee="ai",
):
    stamp = changed_at or utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, project_id, meeting_id, requirement_id,
            status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, f"任务 {task_id}", status, assignee, project_id, meeting_id,
            requirement_id, stamp, stamp, utc_now(),
        ),
    )


def days_ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def task_status(db, task_id):
    return db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))["status"]


def test_expire_moves_only_stale_pending_drafts(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    insert_task(db, "old", "pending_confirm", changed_at=days_ago(8))
    insert_task(db, "fresh", "pending_confirm", changed_at=days_ago(1))
    insert_task(db, "old-confirmed", "confirmed", changed_at=days_ago(30))

    expired = TaskService(db, settings).expire_stale_drafts()

    assert expired == 1
    assert task_status(db, "old") == "expired"
    assert task_status(db, "fresh") == "pending_confirm"
    assert task_status(db, "old-confirmed") == "confirmed"
    events = db.query_all("SELECT kind FROM task_events WHERE task_id='old'")
    assert [event["kind"] for event in events] == ["expired"]


def test_restored_draft_is_not_expired_again_next_round(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    insert_task(db, "old", "pending_confirm", changed_at=days_ago(10))
    service = TaskService(db, settings)
    service.expire_stale_drafts()

    restored = client.post(
        "/api/tasks/old/status", json={"status": "pending_confirm"}, headers=headers
    )

    assert restored.status_code == 200
    assert restored.json()["status"] == "pending_confirm"
    assert service.expire_stale_drafts() == 0
    assert task_status(db, "old") == "pending_confirm"


def test_expire_can_be_disabled(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    insert_task(db, "old", "pending_confirm", changed_at=days_ago(30))
    settings.task_draft_expire_days = 0

    assert TaskService(db, settings).expire_stale_drafts() == 0
    assert task_status(db, "old") == "pending_confirm"


def test_expired_draft_can_be_confirmed_or_rejected_directly(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    insert_task(db, "a", "expired")
    insert_task(db, "b", "expired")

    assert client.post("/api/tasks/a/confirm", json={}, headers=headers).json()["status"] == "confirmed"
    assert client.post("/api/tasks/b/reject", json={}, headers=headers).json()["status"] == "cancelled"


def test_expired_cannot_jump_to_done(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    insert_task(db, "a", "expired")

    response = client.post("/api/tasks/a/status", json={"status": "done"}, headers=headers)

    assert response.status_code == 409


def test_list_counts_ignore_status_filter_and_accept_status_list(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    for task_id, status in [
        ("p1", "pending_confirm"),
        ("p2", "pending_confirm"),
        ("c1", "confirmed"),
        ("i1", "in_progress"),
        ("d1", "done"),
        ("x1", "cancelled"),
        ("e1", "expired"),
    ]:
        insert_task(db, task_id, status)

    payload = client.get("/api/tasks", params={"status": "confirmed,in_progress"}).json()

    assert {item["id"] for item in payload["items"]} == {"c1", "i1"}
    assert payload["total"] == 2
    assert payload["counts"] == {
        "pending_confirm": 2,
        "confirmed": 1,
        "in_progress": 1,
        "done": 1,
        "cancelled": 1,
        "expired": 1,
    }


def test_list_counts_respect_project_filter(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "样品项目", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "in-project", "done", project_id=project_id)
    insert_task(db, "elsewhere", "done")

    payload = client.get("/api/tasks", params={"project_id": project_id, "status": "done"}).json()

    assert payload["total"] == 1
    assert payload["counts"]["done"] == 1


def test_list_project_counts_ignore_project_and_status_filter(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "样品项目", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "a1", "confirmed", project_id=project_id)
    insert_task(db, "a2", "pending_confirm", project_id=project_id)
    insert_task(db, "n1", "confirmed")
    insert_task(db, "n2", "done")

    payload = client.get(
        "/api/tasks", params={"project_id": project_id, "status": "confirmed"}
    ).json()

    assert payload["total"] == 1
    assert payload["project_counts"][project_id]["confirmed"] == 1
    assert payload["project_counts"][project_id]["pending_confirm"] == 1
    assert payload["project_counts"]["none"]["confirmed"] == 1
    assert payload["project_counts"]["none"]["done"] == 1


def test_list_filters_unassigned_tasks_with_none(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "样品项目", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "in-project", "confirmed", project_id=project_id)
    insert_task(db, "loose-1", "confirmed")
    insert_task(db, "loose-2", "done")

    payload = client.get("/api/tasks", params={"project_id": "none"}).json()

    assert {item["id"] for item in payload["items"]} == {"loose-1", "loose-2"}
    assert payload["counts"]["confirmed"] == 1
    assert payload["counts"]["done"] == 1


def test_list_rejects_unknown_status_in_list(tmp_path):
    client, _settings = make_client(tmp_path)

    response = client.get("/api/tasks", params={"status": "done,bogus"})

    assert response.status_code == 400


def test_batch_reject_only_touches_drafts(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    insert_task(db, "p1", "pending_confirm")
    insert_task(db, "e1", "expired")
    insert_task(db, "c1", "confirmed")

    result = client.post(
        "/api/tasks/batch-reject",
        json={"task_ids": ["p1", "e1", "c1", "missing"]},
        headers=headers,
    ).json()

    assert result["rejected"] == ["p1", "e1"]
    assert result["failed"] == [{"task_id": "missing", "error": "任务不存在"}]
    assert task_status(db, "c1") == "confirmed"


def test_undo_restores_recent_confirm_and_reject(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    insert_task(db, "p1", "pending_confirm")
    insert_task(db, "p2", "pending_confirm")
    client.post("/api/tasks/batch-confirm", json={"task_ids": ["p1"]}, headers=headers)
    client.post("/api/tasks/batch-reject", json={"task_ids": ["p2"]}, headers=headers)

    result = client.post(
        "/api/tasks/undo-review", json={"task_ids": ["p1", "p2"]}, headers=headers
    ).json()

    assert result == {"reverted": ["p1", "p2"], "failed": []}
    assert task_status(db, "p1") == "pending_confirm"
    assert task_status(db, "p2") == "pending_confirm"
    kinds = [row["kind"] for row in db.query_all("SELECT kind FROM task_events WHERE task_id='p1' ORDER BY id")]
    assert kinds == ["confirmed", "reverted"]


def test_undo_after_confirm_with_requirement_change_keeps_requirement_id(tmp_path):
    """D25：撤销只回退状态，确认时改过的字段（含 requirement_id）保持修改后的值。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求N", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "p1", "pending_confirm")

    confirmed = client.post(
        "/api/tasks/p1/confirm", json={"requirement_id": requirement_id}, headers=headers
    ).json()
    assert confirmed["requirement_id"] == requirement_id
    assert confirmed["project_id"] == project_id
    assert [event["kind"] for event in confirmed["events"]][-1] == "confirmed"

    result = client.post("/api/tasks/undo-review", json={"task_ids": ["p1"]}, headers=headers).json()
    assert result == {"reverted": ["p1"], "failed": []}
    assert task_status(db, "p1") == "pending_confirm"

    after_undo = client.get("/api/tasks/p1").json()
    assert after_undo["requirement_id"] == requirement_id
    assert after_undo["project_id"] == project_id


def test_undo_refuses_when_task_moved_on(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    insert_task(db, "p1", "pending_confirm")
    client.post("/api/tasks/p1/confirm", json={}, headers=headers)
    client.post("/api/tasks/p1/status", json={"status": "in_progress"}, headers=headers)

    result = client.post("/api/tasks/undo-review", json={"task_ids": ["p1"]}, headers=headers).json()

    assert result["reverted"] == []
    assert result["failed"][0]["task_id"] == "p1"
    assert task_status(db, "p1") == "in_progress"


def test_undo_refuses_manual_task_and_expired_window(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    manual_id = client.post("/api/tasks", json={"title": "手建任务"}, headers=headers).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "p1", "pending_confirm")
    client.post("/api/tasks/p1/confirm", json={}, headers=headers)
    monkeypatch.setattr(tasks_module, "UNDO_WINDOW_SECONDS", -1)

    result = client.post(
        "/api/tasks/undo-review", json={"task_ids": [manual_id, "p1"]}, headers=headers
    ).json()

    assert result["reverted"] == []
    assert {item["task_id"] for item in result["failed"]} == {manual_id, "p1"}
    assert task_status(db, "p1") == "confirmed"


def test_extraction_is_capped_and_scoped_to_me(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    prompts = []

    def fake_llm(self, prompt):
        prompts.append(prompt)
        items = ",".join(
            f'{{"title":"事项{index}","detail":"","anchor_quote":"","assignee_suggestion":"me",'
            f'"project_match":null,"suggested_project_name":null}}'
            for index in range(1, 6)
        )
        return f'{{"tasks":[{items}]}}'

    monkeypatch.setattr(TaskService, "_call_llm", fake_llm)
    result = client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract", json={"supplement": ""}, headers=headers
    ).json()

    assert result["status"] == "done"
    titles = sorted(item["title"] for item in client.get("/api/tasks").json()["items"])
    assert titles == ["事项1", "事项2", "事项3"]
    assert "由我负责推进" in prompts[0]
    assert f"最多 {tasks_module.MAX_TASKS_PER_EXTRACTION} 条" in prompts[0]


def test_re_extract_replaces_expired_drafts_of_that_meeting(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    insert_task(db, "stale", "expired", meeting_id="vm-20260102-101500")
    insert_task(db, "kept", "confirmed", meeting_id="vm-20260102-101500")

    monkeypatch.setattr(
        TaskService,
        "_call_llm",
        lambda self, prompt: '{"tasks":[{"title":"新草稿","anchor_quote":"","assignee_suggestion":"ai"}]}',
    )
    client.post(
        "/api/meetings/vm-20260102-101500/tasks/re-extract", json={"supplement": ""}, headers=headers
    )

    ids = {row["id"] for row in db.query_all("SELECT id FROM tasks")}
    assert "stale" not in ids
    assert "kept" in ids


def test_repeat_confirm_is_noop_and_cannot_undo_old_confirmation(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    old = days_ago(4)
    insert_task(db, "old", "confirmed", changed_at=old)

    response = client.post("/api/tasks/old/confirm", json={}, headers=headers)
    undo = client.post("/api/tasks/undo-review", json={"task_ids": ["old"]}, headers=headers).json()

    assert response.status_code == 200
    row = db.query_one("SELECT status, status_changed_at FROM tasks WHERE id='old'")
    assert row == {"status": "confirmed", "status_changed_at": old}
    assert db.query_all("SELECT kind FROM task_events WHERE task_id='old'") == []
    assert undo["reverted"] == []
    assert task_status(db, "old") == "confirmed"


def test_repeat_confirm_with_edits_only_records_edit(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    old = days_ago(4)
    insert_task(db, "old", "confirmed", changed_at=old)

    client.post("/api/tasks/old/confirm", json={"title": "改过的标题"}, headers=headers)

    row = db.query_one("SELECT title, status_changed_at FROM tasks WHERE id='old'")
    assert row == {"title": "改过的标题", "status_changed_at": old}
    kinds = [event["kind"] for event in db.query_all("SELECT kind FROM task_events WHERE task_id='old'")]
    assert kinds == ["edited"]


def test_repeat_reject_is_noop_and_cannot_undo(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    old = days_ago(2)
    insert_task(db, "gone", "cancelled", changed_at=old)

    client.post("/api/tasks/gone/reject", json={}, headers=headers)
    undo = client.post("/api/tasks/undo-review", json={"task_ids": ["gone"]}, headers=headers).json()

    assert db.query_one("SELECT status_changed_at FROM tasks WHERE id='gone'")["status_changed_at"] == old
    assert undo["reverted"] == []
    assert task_status(db, "gone") == "cancelled"


def test_same_status_set_writes_nothing(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    old = days_ago(5)
    insert_task(db, "doing", "in_progress", changed_at=old)

    client.post("/api/tasks/doing/status", json={"status": "in_progress"}, headers=headers)

    assert db.query_one("SELECT status_changed_at FROM tasks WHERE id='doing'")["status_changed_at"] == old
    assert db.query_all("SELECT kind FROM task_events WHERE task_id='doing'") == []


def test_concurrent_batch_confirm_claims_each_task_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    ids = [f"p{index}" for index in range(5)]
    for task_id in ids:
        insert_task(db, task_id, "pending_confirm")
    service = TaskService(db, settings)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.batch_confirm(ids), range(4)))

    claimed = [task_id for result in results for task_id in result["confirmed"]]
    assert sorted(claimed) == sorted(ids)
    for task_id in ids:
        kinds = [row["kind"] for row in db.query_all("SELECT kind FROM task_events WHERE task_id=?", (task_id,))]
        assert kinds == ["confirmed"]
