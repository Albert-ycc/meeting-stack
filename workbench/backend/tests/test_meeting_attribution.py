"""会议归属地基（v13 / 第一期 1a）：只在归属真的变了时才写、任务跟着会议走、
只有新生成的纪要才重判归属。"""
import json
import uuid

from meeting_workbench import project_linking as project_linking_module
from meeting_workbench.db import Database, utc_now
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.tasks import TaskService

from .helpers import seed_editable_meeting
from .test_tasks_api import make_client, seed_minutes, write_headers

MEETING = "vm-20260102-101500"


def _setup(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    return client, settings, headers, db


def _project(client, headers, name):
    return client.post("/api/projects", json={"name": name}, headers=headers).json()["id"]


def _task(db, *, project_id=None, requirement_id=None, status="pending_confirm", meeting_id=MEETING):
    task_id = f"task-{uuid.uuid4().hex}"
    now = utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, meeting_id, project_id, requirement_id,
            status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', 'ai', ?, ?, ?, ?, ?, ?)""",
        (task_id, task_id[-6:], status, meeting_id, project_id, requirement_id, now, now, now),
    )
    return task_id


def _meeting(db):
    return db.query_one(
        "SELECT project_id, project_origin FROM meetings WHERE id=?", (MEETING,)
    )


def _task_project(db, task_id):
    return db.query_one("SELECT project_id FROM tasks WHERE id=?", (task_id,))["project_id"]


def _events(db, event_type):
    return [
        json.loads(row["payload_json"])
        for row in db.query_all(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY id", (event_type,)
        )
    ]


def test_saving_only_tags_keeps_attribution_open(tmp_path):
    client, settings, headers, db = _setup(tmp_path)
    tag = client.post("/api/tags", json={"name": "周会"}, headers=headers).json()

    response = client.patch(
        f"/api/meetings/{MEETING}", json={"tag_ids": [tag["id"]]}, headers=headers
    )

    assert response.status_code == 200
    assert "effects" not in response.json()
    assert _meeting(db) == {"project_id": None, "project_origin": None}
    assert ProjectLinker(db, settings).seed() == 1


def test_choosing_no_project_marks_manual_without_touching_tasks(tmp_path):
    client, settings, headers, db = _setup(tmp_path)
    draft = _task(db)

    client.patch(f"/api/meetings/{MEETING}", json={"project_id": ""}, headers=headers)

    assert _meeting(db) == {"project_id": None, "project_origin": "manual"}
    assert _task_project(db, draft) is None
    assert ProjectLinker(db, settings).seed() == 0
    payload = _events(db, "meeting_metadata_updated")[-1]
    assert payload["fields"] == ["project_id"]
    assert payload["origin_before"] is None


def test_first_assignment_brings_draft_tasks_along(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = _project(client, headers, "云图AI")
    draft = _task(db)
    expired = _task(db, status="expired")
    cleared = _task(db, status="confirmed")

    detail = client.patch(
        f"/api/meetings/{MEETING}", json={"project_id": project_a}, headers=headers
    ).json()

    assert _meeting(db) == {"project_id": project_a, "project_origin": "manual"}
    assert _task_project(db, draft) == project_a
    assert _task_project(db, expired) == project_a
    # 已确认、项目为空的任务可能是人工清空过的，不动
    assert _task_project(db, cleared) is None
    assert detail["effects"]["tasks_moved"] == 2
    assert detail["effects"]["tasks_left"] == []
    assert detail["effects"]["undo_until"]
    [reassigned] = _events(db, "meeting_project_reassigned")
    assert sorted(reassigned.pop("moved_task_ids")) == sorted([draft, expired])
    assert reassigned == {
        "from": None,
        "to": project_a,
        "origin_before": None,
        "left_task_ids": [],
    }


def test_moving_to_another_project_leaves_requirement_tasks_behind(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = _project(client, headers, "云图AI")
    project_b = _project(client, headers, "数据中台")
    requirement = client.post(
        "/api/requirements",
        json={"project_id": project_a, "title": "登录改版", "priority": "P1"},
        headers=headers,
    ).json()
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='ai' WHERE id=?", (project_a, MEETING)
    )
    confirmed_on_a = _task(db, project_id=project_a, status="confirmed")
    draft_on_a = _task(db, project_id=project_a)
    on_requirement = _task(
        db, project_id=project_a, requirement_id=requirement["id"], status="confirmed"
    )
    cleared = _task(db, status="confirmed")
    other_meeting_task = _task(db, project_id=project_a, meeting_id=None)

    detail = client.patch(
        f"/api/meetings/{MEETING}", json={"project_id": project_b}, headers=headers
    ).json()

    assert _meeting(db) == {"project_id": project_b, "project_origin": "manual"}
    assert _task_project(db, confirmed_on_a) == project_b
    assert _task_project(db, draft_on_a) == project_b
    assert _task_project(db, on_requirement) == project_a
    assert _task_project(db, cleared) is None
    assert _task_project(db, other_meeting_task) == project_a
    assert detail["effects"]["tasks_moved"] == 2
    assert detail["effects"]["tasks_left"] == [
        {
            "id": on_requirement,
            "title": on_requirement[-6:],
            "requirement_id": requirement["id"],
            "requirement_title": "登录改版",
        }
    ]
    event = _events(db, "meeting_project_reassigned")[-1]
    assert event["from"] == project_a
    assert event["to"] == project_b
    assert event["origin_before"] == "ai"
    assert event["left_task_ids"] == [on_requirement]
    metadata = _events(db, "meeting_metadata_updated")[-1]
    assert (metadata["project_from"], metadata["project_to"]) == (project_a, project_b)
    # 挂在旧需求上的任务没被写事件，刚确认的任务仍能撤销确认
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id IN (?, ?)",
        (confirmed_on_a, draft_on_a),
    )["n"] == 0


def test_resending_the_same_project_changes_nothing(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = _project(client, headers, "云图AI")
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='ai' WHERE id=?", (project_a, MEETING)
    )

    detail = client.patch(
        f"/api/meetings/{MEETING}", json={"project_id": project_a}, headers=headers
    ).json()

    assert _meeting(db) == {"project_id": project_a, "project_origin": "ai"}
    assert "effects" not in detail
    assert _events(db, "meeting_project_reassigned") == []
    assert _events(db, "meeting_metadata_updated")[-1]["fields"] == []


def test_returning_to_ai_clears_origin_and_reseeds(tmp_path):
    client, settings, headers, db = _setup(tmp_path)
    project_a = _project(client, headers, "云图AI")
    linker = ProjectLinker(db, settings)
    client.patch(f"/api/meetings/{MEETING}", json={"project_id": project_a}, headers=headers)
    moved = _task(db, project_id=project_a)
    assert linker.seed() == 0

    client.patch(f"/api/meetings/{MEETING}", json={"project_id": "__ai__"}, headers=headers)

    assert _meeting(db) == {"project_id": None, "project_origin": None}
    assert _task_project(db, moved) is None
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM project_links WHERE meeting_id=?", (MEETING,)
    )["n"] == 0
    assert linker.seed() == 1


def test_unknown_project_is_rejected_without_writing(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)

    response = client.patch(
        f"/api/meetings/{MEETING}", json={"project_id": "project-missing"}, headers=headers
    )

    assert response.status_code == 404
    assert _meeting(db) == {"project_id": None, "project_origin": None}


def test_auto_attribution_brings_draft_tasks_along(tmp_path, monkeypatch):
    client, settings, headers, db = _setup(tmp_path)
    project_a = _project(client, headers, "云图AI")
    draft = _task(db)
    on_nothing_confirmed = _task(db, status="confirmed")
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: (
            '{"project_match":"云图AI","confidence":"high","reason":"标题"}'
        ),
    )

    ProjectLinker(db, settings).link_pending()

    assert _meeting(db) == {"project_id": project_a, "project_origin": "ai"}
    assert _task_project(db, draft) == project_a
    assert _task_project(db, on_nothing_confirmed) is None
    event = _events(db, "meeting_project_auto_assigned")[-1]
    assert event["adopted_task_ids"] == [draft]


def test_only_newly_generated_minutes_reopen_attribution(tmp_path, monkeypatch):
    client, settings, headers, db = _setup(tmp_path)
    monkeypatch.setattr(
        project_linking_module,
        "call_llm",
        lambda settings, prompt, *, system: '{"project_match":null,"confidence":"low","reason":"x"}',
    )
    linker = ProjectLinker(db, settings)
    linker.link_pending()
    assert db.query_one(
        "SELECT status FROM project_links WHERE meeting_id=?", (MEETING,)
    )["status"] == "unresolved"

    for version_no, kind in ((2, "draft"), (3, "published_edit"), (4, "superseded")):
        db.execute(
            """INSERT INTO minutes_versions
               (id, meeting_id, version_no, markdown, kind, created_at)
               VALUES (?, ?, ?, '# 手改', ?, ?)""",
            (f"mv-{kind}", MEETING, version_no, kind, utc_now()),
        )
        db.execute(
            "UPDATE meetings SET current_minutes_version_id=? WHERE id=?", (f"mv-{kind}", MEETING)
        )
        assert linker.seed() == 0, kind

    db.execute(
        """INSERT INTO minutes_versions (id, meeting_id, version_no, markdown, kind, created_at)
           VALUES ('mv-regen', ?, 5, '# 重新生成', 'generated', ?)""",
        (MEETING, utc_now()),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-regen' WHERE id=?", (MEETING,))
    assert linker.seed() == 1


def test_extracted_tasks_take_the_meeting_project(tmp_path, monkeypatch):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = _project(client, headers, "云图AI")
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='ai' WHERE id=?", (project_a, MEETING)
    )
    monkeypatch.setattr(
        TaskService,
        "_call_llm",
        lambda self, prompt: (
            '{"tasks":[{"title":"做看板","detail":"","anchor_quote":"第一段内容",'
            '"assignee_suggestion":"ai","project_match":"数据中台"}]}'
        ),
    )

    result = client.post(
        f"/api/meetings/{MEETING}/tasks/re-extract", json={}, headers=headers
    ).json()

    assert result["status"] == "done"
    items = client.get("/api/tasks").json()["items"]
    assert [item["project_id"] for item in items] == [project_a]
