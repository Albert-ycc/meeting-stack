"""会议归属状态（v13 / 第一期 1b-1）：状态推导、列表筛选、详情证据、确认、撤销、汇总。"""
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

from meeting_workbench.attribution import attribution_state
from meeting_workbench.db import Database, utc_now
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.project_profile import build_cue_table

from .test_project_linking import (
    HIGH,
    llm_answers,
    make_project,
    make_term,
    meeting_row,
    seed_meeting,
)
from .test_tasks_api import make_client, write_headers


def _setup(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    return client, settings, headers, Database(settings.database_path)


def _add_link(
    db,
    meeting_id,
    status,
    *,
    project_id=None,
    candidates=None,
    new_project_name=None,
    evidence=None,
    method=None,
    version=None,
):
    # 默认挂在会议当前纪要上（seed_meeting 建的是 mv-<会议 id>），和真实批次一致
    db.execute(
        """INSERT INTO project_links
           (meeting_id, minutes_version_id, status, project_id, method, candidates_json,
            new_project_name, evidence_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            meeting_id,
            version or f"mv-{meeting_id}",
            status,
            project_id,
            method,
            json.dumps(candidates, ensure_ascii=False) if candidates is not None else None,
            new_project_name,
            json.dumps(evidence, ensure_ascii=False) if evidence is not None else None,
            utc_now(),
        ),
    )


def _draft_task(db, meeting_id, project_id=None, status="pending_confirm"):
    task_id = f"task-{uuid.uuid4().hex}"
    now = utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, meeting_id, project_id,
            status_changed_at, created_at, updated_at)
           VALUES (?, '任务', ?, 'ai', 'ai', ?, ?, ?, ?, ?)""",
        (task_id, status, meeting_id, project_id, now, now, now),
    )
    return task_id


def _task_project(db, task_id):
    return db.query_one("SELECT project_id FROM tasks WHERE id=?", (task_id,))["project_id"]


def _link_statuses(db, meeting_id):
    return [
        row["status"]
        for row in db.query_all(
            "SELECT status FROM project_links WHERE meeting_id=? ORDER BY id", (meeting_id,)
        )
    ]


def _list(client, **params):
    response = client.get("/api/meetings", params=params)
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["items"]}


def test_sql_and_python_state_rules_agree(tmp_path):
    client, _settings, _headers, db = _setup(tmp_path)
    project = make_project(db, "云图AI")
    combos = list(
        itertools.product(
            (None, project),
            (None, "ai", "manual"),
            (None, "pending", "running", "needs_review", "unresolved", "done", "failed"),
            (None, "智慧园区"),
        )
    )
    expected = {}
    for index, (project_id, origin, link_status, name) in enumerate(combos):
        meeting_id = f"m-{index:03d}"
        seed_meeting(db, meeting_id, f"会 {index}", project_id=project_id, origin=origin)
        if link_status is not None:
            _add_link(db, meeting_id, link_status, new_project_name=name)
        expected[meeting_id] = attribution_state(
            project_id=project_id,
            origin=origin,
            link_status=link_status,
            new_project_name=name,
        )

    listed = {
        meeting_id: item["attribution_state"]
        for meeting_id, item in _list(client, limit=500).items()
    }

    assert listed == expected
    # 抽几条按规格核对
    assert expected["m-000"] == "ai_pending"  # 没项目、没来源、没批次
    assert attribution_state(
        project_id=project, origin="ai", link_status="needs_review", new_project_name=None
    ) == "needs_review"
    assert attribution_state(
        project_id=None, origin="manual", link_status="needs_review", new_project_name=None
    ) == "manual_none"
    assert attribution_state(
        project_id=None, origin=None, link_status="unresolved", new_project_name="智慧园区"
    ) == "new_project"


def test_list_filters_by_attribution_and_carries_candidates(tmp_path):
    client, _settings, _headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "m-auto", "云图周会", project_id=project_a, origin="ai")
    _add_link(db, "m-auto", "done", project_id=project_a)
    seed_meeting(db, "m-review", "评审会")
    _add_link(
        db,
        "m-review",
        "needs_review",
        candidates=[
            {"project_id": project_b, "project_name": "数据中台", "count": 1, "llm": True},
            {"project_id": project_a, "project_name": "云图AI", "count": 3, "llm": False},
        ],
    )
    seed_meeting(db, "m-new", "园区沟通会")
    _add_link(db, "m-new", "unresolved", new_project_name="智慧园区")
    seed_meeting(db, "m-pending", "刚录的会")
    seed_meeting(db, "m-none", "闲聊", origin="manual")
    seed_meeting(db, "m-manual", "人工归的会", project_id=project_b, origin="manual")

    [review] = _list(client, attribution="needs_review").values()
    assert review["id"] == "m-review"
    assert [(c["project_id"], c["project_name"], c["llm"]) for c in review["candidates"]] == [
        (project_b, "数据中台", True),
        (project_a, "云图AI", False),
    ]
    assert review["new_project_name"] is None

    [new] = _list(client, attribution="new_project").values()
    assert (new["id"], new["new_project_name"], new["candidates"]) == ("m-new", "智慧园区", [])

    assert set(_list(client, attribution="auto")) == {"m-auto"}
    assert set(_list(client, attribution="ai_pending")) == {"m-pending"}
    assert set(_list(client, attribution="manual_none")) == {"m-none"}
    assert set(_list(client, attribution="manual")) == {"m-manual"}
    assert set(_list(client, project_id="none")) == {"m-review", "m-new", "m-pending", "m-none"}
    assert set(_list(client, project_id="none", attribution="needs_review")) == {"m-review"}
    response = client.get("/api/meetings", params={"attribution": "whatever"})
    assert response.status_code == 400


def test_detail_shows_evidence_and_where_the_meeting_came_from(tmp_path, monkeypatch):
    client, settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    term_id = make_term(db, project_a, "灰度方案")
    seed_meeting(
        db,
        "m-1",
        "周三例会",
        segments=[(1000, "先说灰度方案"), (5000, "灰度方案下周上线")],
    )
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))
    ProjectLinker(db, settings).link_pending()

    attribution = client.get("/api/meetings/m-1").json()["attribution"]
    assert attribution["state"] == "auto"
    assert attribution["method"] == "llm_high"
    assert attribution["reason"] == "标题即项目名"
    assert attribution["ai_configured"] is True
    literal = next(e for e in attribution["evidence"] if e["kind"] == "literal")
    assert (literal["cue"], literal["project_name"], literal["anchors_ms"]) == (
        "灰度方案",
        "云图AI",
        [1000, 5000],
    )
    llm = next(e for e in attribution["evidence"] if e["kind"] == "llm")
    assert (llm["project_id"], llm["confidence"]) == (project_a, "high")
    assert attribution["reassigned_from"] is None

    detail = client.patch(
        "/api/meetings/m-1", json={"project_id": project_b}, headers=headers
    ).json()

    assert detail["effects"]["cue_hint"] == {
        "term_id": term_id,
        "term": "灰度方案",
        "cue": "灰度方案",
    }
    came_from = detail["attribution"]["reassigned_from"]
    assert detail["attribution"]["state"] == "manual"
    assert (came_from["project_id"], came_from["project_name"]) == (project_a, "云图AI")
    assert came_from["origin_before"] == "ai"
    assert came_from["can_undo"] is True
    assert came_from["cue_hint"]["term_id"] == term_id

    # 「以后不再用这个词判断项目」：关掉参与识别后提示消失，线索表里也没有它了
    response = client.put(
        f"/api/glossary/terms/{term_id}", json={"is_cue": False}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_cue"] is False
    attribution = client.get("/api/meetings/m-1").json()["attribution"]
    assert attribution["reassigned_from"]["cue_hint"] is None
    with db.autocommit() as connection:
        cues = build_cue_table(connection)[project_a]
    assert [cue.text for cue in cues] == ["云图AI"]


def test_no_cue_hint_when_the_ai_used_only_the_project_name(tmp_path, monkeypatch):
    client, settings, headers, db = _setup(tmp_path)
    make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "m-1", "云图AI 周会")
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))
    ProjectLinker(db, settings).link_pending()

    detail = client.patch(
        "/api/meetings/m-1", json={"project_id": project_b}, headers=headers
    ).json()

    assert "cue_hint" not in detail["effects"]
    assert detail["attribution"]["reassigned_from"]["cue_hint"] is None


def test_confirming_keeps_the_project_and_closes_the_question(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    # 复评时的样子：会议还挂在 A（AI 归的），最新批次却拿不准
    seed_meeting(db, "m-1", "周会", project_id=project_a, origin="ai")
    _add_link(db, "m-1", "done", project_id=project_a, version="mv-old")
    _add_link(
        db,
        "m-1",
        "needs_review",
        candidates=[
            {"project_id": project_b, "count": 2, "llm": True},
            {"project_id": project_a, "count": 1, "llm": False},
        ],
    )
    task = _draft_task(db, "m-1", project_a)

    attribution = client.get("/api/meetings/m-1").json()["attribution"]
    assert attribution["state"] == "needs_review"
    assert [(c["project_id"], c["current"]) for c in attribution["candidates"]] == [
        (project_a, True),
        (project_b, False),
    ]

    response = client.post("/api/meetings/m-1/project/confirm", json={}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "manual"
    assert meeting_row(db, "m-1") == {"project_id": project_a, "project_origin": "manual"}
    assert _link_statuses(db, "m-1") == ["done", "done"]
    assert _task_project(db, task) == project_a
    event = db.query_one(
        "SELECT payload_json FROM events WHERE event_type='meeting_project_confirmed'"
    )
    assert json.loads(event["payload_json"])["origin_before"] == "ai"


def test_confirming_without_a_project_is_refused(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    seed_meeting(db, "m-1", "周会")

    response = client.post("/api/meetings/m-1/project/confirm", json={}, headers=headers)

    assert response.status_code == 409
    assert meeting_row(db, "m-1") == {"project_id": None, "project_origin": None}


def test_picking_a_candidate_then_undoing_restores_the_question(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "m-1", "周会")
    _add_link(
        db,
        "m-1",
        "needs_review",
        candidates=[{"project_id": project_a, "count": 2}, {"project_id": project_b, "count": 1}],
    )
    draft = _draft_task(db, "m-1")

    picked = client.patch(
        "/api/meetings/m-1", json={"project_id": project_a}, headers=headers
    ).json()
    assert picked["attribution"]["state"] == "manual"
    assert _link_statuses(db, "m-1") == ["done"]
    assert _task_project(db, draft) == project_a

    response = client.post("/api/meetings/m-1/project/undo", json={}, headers=headers)

    assert response.status_code == 200, response.text
    undone = response.json()
    assert undone["effects"] == {"tasks_restored": 1}
    assert undone["attribution"]["state"] == "needs_review"
    assert [c["project_id"] for c in undone["attribution"]["candidates"]] == [project_a, project_b]
    assert meeting_row(db, "m-1") == {"project_id": None, "project_origin": None}
    assert _task_project(db, draft) is None

    again = client.post("/api/meetings/m-1/project/undo", json={}, headers=headers)
    assert again.status_code == 409
    assert "没有可以撤销" in again.json()["detail"]


def test_undo_moves_tasks_back_to_where_each_one_was(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "m-1", "周会", project_id=project_a, origin="ai")
    on_a = _draft_task(db, "m-1", project_a, status="confirmed")
    late_draft = _draft_task(db, "m-1")  # 会议已有项目后才抽出来、还没补上项目的草稿

    client.patch("/api/meetings/m-1", json={"project_id": project_b}, headers=headers)
    assert (_task_project(db, on_a), _task_project(db, late_draft)) == (project_b, project_b)

    response = client.post("/api/meetings/m-1/project/undo", json={}, headers=headers)

    assert response.status_code == 200, response.text
    assert meeting_row(db, "m-1") == {"project_id": project_a, "project_origin": "ai"}
    assert _task_project(db, on_a) == project_a
    assert _task_project(db, late_draft) is None
    assert response.json()["attribution"]["state"] == "auto"


def test_undo_after_ten_minutes_is_refused(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    seed_meeting(db, "m-1", "周会")
    client.patch("/api/meetings/m-1", json={"project_id": project_a}, headers=headers)
    stale = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    db.execute(
        "UPDATE events SET created_at=? WHERE event_type='meeting_project_reassigned'", (stale,)
    )

    response = client.post("/api/meetings/m-1/project/undo", json={}, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "已超过撤销时间，请直接改回"
    assert meeting_row(db, "m-1") == {"project_id": project_a, "project_origin": "manual"}
    came_from = client.get("/api/meetings/m-1").json()["attribution"]["reassigned_from"]
    assert came_from["can_undo"] is False


def test_undo_is_refused_when_the_project_changed_since(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "m-1", "周会")
    client.patch("/api/meetings/m-1", json={"project_id": project_a}, headers=headers)
    db.execute("UPDATE meetings SET project_id=? WHERE id='m-1'", (project_b,))

    response = client.post("/api/meetings/m-1/project/undo", json={}, headers=headers)

    assert response.status_code == 409
    assert meeting_row(db, "m-1")["project_id"] == project_b


def test_marking_no_project_can_be_undone(tmp_path):
    client, _settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    seed_meeting(db, "m-1", "周会")
    _add_link(db, "m-1", "needs_review", candidates=[{"project_id": project_a, "count": 2}])

    detail = client.patch("/api/meetings/m-1", json={"project_id": ""}, headers=headers).json()

    assert detail["attribution"]["state"] == "manual_none"
    assert detail["effects"]["undo_until"]
    assert _link_statuses(db, "m-1") == ["done"]

    undone = client.post("/api/meetings/m-1/project/undo", json={}, headers=headers).json()
    assert undone["attribution"]["state"] == "needs_review"


def test_summary_counts_open_questions_new_names_and_corrections(tmp_path, monkeypatch):
    client, settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI")
    project_b = make_project(db, "数据中台")
    seed_meeting(db, "m-review", "评审会")
    _add_link(db, "m-review", "needs_review", candidates=[{"project_id": project_a}])
    seed_meeting(db, "m-review-old", "去年的评审会")
    db.execute("UPDATE meetings SET recording_date='2025-01-02' WHERE id='m-review-old'")
    _add_link(db, "m-review-old", "needs_review", candidates=[{"project_id": project_a}])
    for meeting_id, name in (
        ("m-new-1", "智慧园区"),
        ("m-new-2", "智慧园区项目"),
        ("m-new-3", "数据中台 二期"),  # 已有同名项目
        ("m-new-4", "内部分享"),  # 用户说过不是新项目
    ):
        seed_meeting(db, meeting_id, "沟通会")
        _add_link(db, meeting_id, "unresolved", new_project_name=name)
    db.execute(
        """INSERT INTO name_decisions(norm_key, name, decision, decided_at)
           VALUES ('内部分享', '内部分享', 'ignored', ?)""",
        (utc_now(),),
    )
    seed_meeting(db, "m-auto-1", "云图AI 周会")
    seed_meeting(db, "m-auto-2", "云图AI 评审")
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))
    ProjectLinker(db, settings).link_pending()
    client.patch("/api/meetings/m-auto-1", json={"project_id": project_b}, headers=headers)
    client.patch("/api/meetings/m-auto-2", json={"project_id": project_b}, headers=headers)
    client.post("/api/meetings/m-auto-2/project/undo", json={}, headers=headers)

    summary = client.get("/api/attribution/summary").json()

    assert summary["needs_review_recent"] == 1
    assert summary["needs_review_total"] == 2
    assert [(n["name"], n["meeting_count"]) for n in summary["new_project_names"]] == [
        (summary["new_project_names"][0]["name"], 2)
    ]
    assert summary["new_project_names"][0]["norm_key"] == "智慧园区"
    assert sorted(summary["new_project_names"][0]["meeting_ids"]) == ["m-new-1", "m-new-2"]
    assert summary["auto_30d"] == 2
    assert summary["corrected_30d"] == 1


def test_project_board_explains_how_the_project_is_recognized(tmp_path, monkeypatch):
    client, settings, headers, db = _setup(tmp_path)
    project_a = make_project(db, "云图AI", also=["云图"])
    project_b = make_project(db, "数据中台")
    make_term(db, project_a, "灰度方案")
    make_term(db, project_a, "指标口径", is_cue=0)
    seed_meeting(db, "m-1", "云图AI 周会")
    seed_meeting(db, "m-2", "云图AI 评审")
    llm_answers(monkeypatch, HIGH.format(name="云图AI"))
    ProjectLinker(db, settings).link_pending()
    client.patch("/api/meetings/m-2", json={"project_id": project_b}, headers=headers)

    profile = client.get(f"/api/projects/{project_a}/board").json()["profile"]

    assert profile == {
        "also_names": [{"name": "云图", "source": "manual"}],
        "folder_names": [],
        "cue_terms": {"total": 2, "cue": 1},
        "auto_30d": 2,
        "corrected_30d": 1,
    }
