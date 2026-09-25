"""`/api/tasks` 新筛选（requirement_id / assignee / meeting_date_from/to / q）与
counts、project_counts 口径（260915 新增，D20）。"""
from meeting_workbench.db import Database, utc_now

from .test_tasks_api import make_client, write_headers
from .test_tasks_review import insert_task


def test_filter_by_requirement_id_and_none(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求A", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "with-req", "confirmed", requirement_id=requirement_id, project_id=project_id)
    insert_task(db, "without-req", "confirmed")

    with_req = client.get("/api/tasks", params={"requirement_id": requirement_id}).json()
    assert {item["id"] for item in with_req["items"]} == {"with-req"}
    assert with_req["items"][0]["requirement_title"] == "需求A"
    assert with_req["items"][0]["requirement_priority"] == "P1"
    assert with_req["items"][0]["requirement_status"] == "active"

    none_req = client.get("/api/tasks", params={"requirement_id": "none"}).json()
    assert {item["id"] for item in none_req["items"]} == {"without-req"}


def test_filter_by_assignee(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    insert_task(db, "for-me", "confirmed", assignee="me")
    insert_task(db, "for-ai", "confirmed", assignee="ai")

    payload = client.get("/api/tasks", params={"assignee": "me"}).json()
    assert {item["id"] for item in payload["items"]} == {"for-me"}


def test_filter_rejects_unknown_assignee(tmp_path):
    client, settings = make_client(tmp_path)
    response = client.get("/api/tasks", params={"assignee": "someone"})
    assert response.status_code == 400


def test_filter_by_title_query(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    insert_task(db, "t-a", "confirmed")
    db.execute("UPDATE tasks SET title='包含关键词的任务' WHERE id='t-a'")
    insert_task(db, "t-b", "confirmed")
    db.execute("UPDATE tasks SET title='不相关任务' WHERE id='t-b'")

    payload = client.get("/api/tasks", params={"q": "关键词"}).json()
    assert {item["id"] for item in payload["items"]} == {"t-a"}


def test_filter_by_meeting_date_range_matches_meetings_endpoint_convention(tmp_path):
    """D20：与 /api/meetings 的 date_from/date_to 同一口径（字符串比较 recording_date）。"""
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    for meeting_id, recording_date in (
        ("vm-early", "2026-01-01T00:00:00+00:00"),
        ("vm-late", "2026-06-01T00:00:00+00:00"),
    ):
        db.execute(
            """INSERT INTO meetings(id, title, recording_date, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (meeting_id, meeting_id, recording_date, utc_now(), utc_now()),
        )
    insert_task(db, "early-task", "confirmed", meeting_id="vm-early")
    insert_task(db, "late-task", "confirmed", meeting_id="vm-late")

    payload = client.get(
        "/api/tasks", params={"meeting_date_from": "2026-03-01"}
    ).json()
    assert {item["id"] for item in payload["items"]} == {"late-task"}
    assert payload["items"][0]["meeting_recording_date"] == "2026-06-01T00:00:00+00:00"

    both = client.get(
        "/api/tasks", params={"meeting_date_to": "2026-12-31"}
    ).json()
    assert {item["id"] for item in both["items"]} == {"early-task", "late-task"}


def test_filter_by_meeting_date_from_rejects_non_date_string(tmp_path):
    """ADV-B-10 回归：meeting_date_from 不是 YYYY-MM-DD → 400，不许静默返回空结果。"""
    client, settings = make_client(tmp_path)
    response = client.get("/api/tasks", params={"meeting_date_from": "不是日期"})
    assert response.status_code == 400
    assert response.json()["detail"] == "日期格式应为 YYYY-MM-DD"


def test_filter_by_meeting_date_to_rejects_non_date_string(tmp_path):
    client, settings = make_client(tmp_path)
    response = client.get("/api/tasks", params={"meeting_date_to": "也不是"})
    assert response.status_code == 400
    assert response.json()["detail"] == "日期格式应为 YYYY-MM-DD"


def test_filter_by_meeting_date_rejects_malformed_but_calendar_like_string(tmp_path):
    """格式对但不是真实日期（02 月 30 日）同样要拒，不能只查正则。"""
    client, settings = make_client(tmp_path)
    response = client.get("/api/tasks", params={"meeting_date_from": "2026-02-30"})
    assert response.status_code == 400
    assert response.json()["detail"] == "日期格式应为 YYYY-MM-DD"


def test_counts_affected_by_new_filters_but_not_status(tmp_path):
    """counts 受除 status 外的全部筛选影响。"""
    client, settings = make_client(tmp_path)
    project_id = client.post(
        "/api/projects", json={"name": "样品项目", "color": "#2c8d83"}, headers=write_headers(client)
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "me-confirmed", "confirmed", assignee="me", project_id=project_id)
    insert_task(db, "me-done", "done", assignee="me", project_id=project_id)
    insert_task(db, "ai-confirmed", "confirmed", assignee="ai", project_id=project_id)

    payload = client.get(
        "/api/tasks", params={"assignee": "me", "status": "confirmed"}
    ).json()
    assert payload["total"] == 1
    assert payload["counts"] == {
        "pending_confirm": 0, "confirmed": 1, "in_progress": 0,
        "done": 1, "cancelled": 0, "expired": 0,
    }


def test_project_counts_affected_by_requirement_filter_but_not_project_id(tmp_path):
    """project_counts 受除 project_id、status 外的全部筛选影响。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求B", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    db = Database(settings.database_path)
    insert_task(db, "with-req", "confirmed", requirement_id=requirement_id, project_id=project_id)
    insert_task(db, "no-req-same-project", "confirmed", project_id=project_id)

    payload = client.get(
        "/api/tasks", params={"project_id": project_id, "requirement_id": requirement_id}
    ).json()
    # project_counts 不受 project_id 筛选影响：即便选中了这个项目，project_counts 里仍然
    # 按 requirement_id 筛选之后的口径统计全部项目（这里只有一条任务符合 requirement_id）。
    assert payload["project_counts"][project_id]["confirmed"] == 1
