"""第二期 2b：「像是新项目 / 新需求」——AI 多给的名字、过滤、共用提示、做了决定就清掉并能撤销、
候选名、从提示建成项目或需求。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from meeting_workbench.db import Database, utc_now
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.text_scan import FormScanner

from .test_project_linking import llm_answers, make_db, make_project, seed_meeting
from .test_tasks_api import make_client, write_headers


def _answer(project=None, *, confidence="high", new_project=None, new_requirement=None, spoken=()):
    return json.dumps(
        {
            "project_match": project,
            "confidence": confidence,
            "new_project_name": new_project,
            "new_requirement_name": new_requirement,
            "spoken_names": list(spoken),
            "reason": "测试",
        },
        ensure_ascii=False,
    )


def _link(db, meeting_id):
    return db.query_one(
        """SELECT status, new_project_name, new_requirement_name, new_name_project_id, new_name_spoken
             FROM project_links WHERE meeting_id=? ORDER BY id DESC LIMIT 1""",
        (meeting_id,),
    )


def _requirement(db, project_id, title, status="active"):
    requirement_id = f"requirement-{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES (?, ?, ?, 'P1', ?, ?, ?)""",
        (requirement_id, project_id, title, status, utc_now(), utc_now()),
    )
    return requirement_id


BOARD = [(1000, "今天说说数据看板"), (5000, "数据看板先做首页"), (9000, "别的事")]


# ---------------------------------------------------------------------- 一遍扫描


def test_scanner_prefers_longest_form_and_checks_letter_boundaries():
    scanner = FormScanner(["数据看板", "看板", "AI"])
    counts = scanner.scan_segments(
        [
            {"start_ms": 0, "text": "数据 看板和看板"},
            {"start_ms": 10, "text": "said ai, AI助手，OpenAI"},
        ]
    )
    assert counts["数据看板"]["count"] == 1
    assert counts["看板"]["count"] == 1
    # 「said」「OpenAI」里的 ai 不算，「ai,」「AI助手」算。
    assert counts["AI"] == {"count": 2, "first_ms": 10, "anchors_ms": [10]}


# ---------------------------------------------------------------------- 分类


def test_auto_assigned_meeting_keeps_new_requirement_name(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI")
    seed_meeting(db, "m-1", "云图AI 周会", "# 摘要\n云图AI 要做数据看板。", segments=BOARD)
    llm_answers(
        monkeypatch,
        _answer("云图AI", new_requirement="数据看板", spoken=["数据看板", "不存在的词"]),
    )

    ProjectLinker(db, settings).link_pending()

    link = _link(db, "m-1")
    assert link["status"] == "done"
    assert link["new_requirement_name"] == "数据看板"
    assert link["new_name_project_id"] == project_id
    assert json.loads(link["new_name_spoken"]) == ["数据看板"]


def test_requirement_name_filters(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI", also=["云图"])
    _requirement(db, project_id, "云图数据看板", status="shelved")
    make_project(db, "报价系统")
    db.execute(
        """INSERT INTO requirement_name_decisions(project_id, name_key, name, decision, decided_at)
           VALUES (?, '首页改版', '首页改版', 'ignored', ?)""",
        (project_id, utc_now()),
    )
    cases = {
        "m-similar": ("数据看板", BOARD),  # 和已搁置的需求标题相近
        "m-own": ("云图项目", [(0, "云图项目"), (1, "云图项目")]),  # 就是项目自己的也叫
        "m-other": ("报价系统", [(0, "云图AI 报价系统"), (1, "云图AI 报价系统")]),  # 是别的项目
        "m-ignored": ("首页改版", [(0, "首页改版"), (1, "首页改版")]),  # 说过「不算新需求」
        "m-rare": ("权限中心", [(0, "权限中心"), (1, "别的")]),  # 逐字稿里只说了 1 次
        "m-long": ("长" * 41, [(0, "长" * 41), (1, "长" * 41)]),
    }
    for meeting_id, (name, segments) in cases.items():
        seed_meeting(db, meeting_id, "云图AI 周会", f"# 摘要\n{name}", segments=segments)
    answers = [_answer("云图AI", new_requirement=name) for name, _segments in cases.values()]
    llm_answers(monkeypatch, *answers)

    ProjectLinker(db, settings).link_pending(max_batches=10)

    for meeting_id in cases:
        link = _link(db, meeting_id)
        assert link["status"] == "done", meeting_id
        assert link["new_requirement_name"] is None, meeting_id


def test_new_project_name_is_kept_when_review_and_also_names_block_it(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    make_project(db, "智慧园区")
    make_project(db, "云图AI", also=["云图看板"])
    # 字面线索碰巧命中「智慧园区」一次，AI 没选项目、提了新项目名 → 待你选，名字留着。
    seed_meeting(db, "m-review", "沟通", "# 摘要\n智慧园区顺带提了一句；主要谈数据中台。")
    seed_meeting(db, "m-also", "沟通", "# 摘要\n谈云图看板。")
    llm_answers(
        monkeypatch,
        _answer(None, confidence="low", new_project="数据中台"),
        _answer(None, confidence="low", new_project="云图看板"),
    )

    ProjectLinker(db, settings).link_pending()

    review = _link(db, "m-review")
    assert review["status"] == "needs_review"
    assert review["new_project_name"] == "数据中台"
    assert _link(db, "m-also")["new_project_name"] is None


def test_prompt_lists_twenty_requirements_and_hides_the_answer_in_evaluate(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图AI")
    for index in range(25):
        _requirement(db, project_id, f"需求{index:02d}")
    seed_meeting(db, "m-answer", "云图周会", project_id=project_id, origin="manual", segments=BOARD)
    only_here = _requirement(db, project_id, "只关联这场会的需求")
    db.execute(
        "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, 'm-answer', ?)",
        (only_here, utc_now()),
    )
    prompts: list[str] = []
    llm_answers(monkeypatch, _answer("云图AI", new_requirement="数据看板"), prompts=prompts)

    linker = ProjectLinker(db, settings)
    assert "只关联这场会的需求" in linker._build_prompt(title="x", minutes="y")
    result = linker.evaluate(apply=False)

    assert "只关联这场会的需求" not in prompts[0]
    listed = prompts[0].split("在做的需求 ")[1].split("）")[0].split("；")[0].split("、")
    assert len(listed) == 20
    assert "new_requirement_name" in prompts[0] and "spoken_names" in prompts[0]
    assert result["requirement_hints"] == {"meetings": 1, "total": 1}
    assert result["results"][0]["new_requirement_name"] == "数据看板"


# ---------------------------------------------------------------------- 共用提示 + 决定


def _api(tmp_path, browse_root=None):
    browse_root = browse_root or (tmp_path / "browse")
    browse_root.mkdir(exist_ok=True)
    client, settings = make_client(tmp_path, material_browse_root=browse_root)
    return client, settings, write_headers(client), Database(settings.database_path), browse_root


def _hint(client, meeting_id):
    return client.get(f"/api/meetings/{meeting_id}").json()["attribution"]["name_hint"]


def _set_link(db, meeting_id, status, **columns):
    names = ["meeting_id", "minutes_version_id", "status", "created_at", *columns]
    values = [meeting_id, f"mv-{meeting_id}", status, utc_now()]
    for value in columns.values():
        values.append(json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value)
    db.execute(
        f"INSERT INTO project_links({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        tuple(values),
    )


def _requirement_hint_meeting(
    db, meeting_id, project_id, name="数据看板", segments=BOARD, origin="ai"
):
    seed_meeting(
        db, meeting_id, "云图周会", segments=segments, project_id=project_id, origin=origin
    )
    _set_link(
        db,
        meeting_id,
        "done",
        project_id=project_id,
        new_requirement_name=name,
        new_name_project_id=project_id,
        new_name_spoken=[name],
    )


def test_name_hint_is_shared_by_detail_and_list(tmp_path):
    client, _settings, _headers, db, _root = _api(tmp_path)
    project_id = make_project(db, "云图AI")
    other_id = make_project(db, "报价系统")
    _requirement_hint_meeting(db, "m-req", project_id)
    # 归属改到别的项目后，AI 当时选的项目对不上号，不提示。
    _requirement_hint_meeting(db, "m-moved", project_id)
    db.execute("UPDATE meetings SET project_id=? WHERE id='m-moved'", (other_id,))
    seed_meeting(db, "m-new", "沟通")
    _set_link(db, "m-new", "unresolved", new_project_name="数据中台", new_name_spoken=["中台"])
    seed_meeting(db, "m-review", "沟通")
    _set_link(
        db,
        "m-review",
        "needs_review",
        new_project_name="数据中台",
        candidates_json=json.dumps([{"project_id": other_id, "count": 1, "llm": False}]),
    )

    assert _hint(client, "m-req") == {
        "kind": "requirement",
        "name": "数据看板",
        "spoken": ["数据看板"],
        "project_id": project_id,
        "project_name": "云图AI",
    }
    assert _hint(client, "m-moved") is None
    assert _hint(client, "m-new") == {"kind": "project", "name": "数据中台", "spoken": ["中台"]}
    assert _hint(client, "m-review")["kind"] == "project"
    rows = {row["id"]: row for row in client.get("/api/meetings?limit=50").json()["items"]}
    for meeting_id in ("m-req", "m-moved", "m-new", "m-review"):
        assert rows[meeting_id]["name_hint"] == _hint(client, meeting_id)
    brief = client.get("/api/meetings/m-req/brief").json()
    assert brief["attribution"]["name_hint"]["kind"] == "requirement"

    # 后来建了标题相近的需求：提示自然消失。
    _requirement(db, project_id, "数据看板一期")
    assert _hint(client, "m-req") is None


def test_ignore_requirement_name_clears_only_that_project_and_can_be_undone(tmp_path):
    client, _settings, headers, db, _root = _api(tmp_path)
    project_id = make_project(db, "云图AI")
    _requirement_hint_meeting(db, "m-1", project_id, name="云图二期")
    _requirement_hint_meeting(db, "m-2", project_id, name="云图 二期")
    _requirement_hint_meeting(db, "m-3", project_id, name="云图三期")

    response = client.post(
        "/api/project-names/ignore",
        json={
            "name": "云图二期",
            "kind": "requirement",
            "project_id": project_id,
            "meeting_id": "m-1",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meetings_updated"] == 2
    assert _hint(client, "m-1") is None and _hint(client, "m-2") is None
    assert _hint(client, "m-3")["name"] == "云图三期"

    undone = client.post(
        "/api/name-decisions/undo", json={"event_id": body["event_id"]}, headers=headers
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["meetings_restored"] == 2
    assert _hint(client, "m-1")["name"] == "云图二期"
    assert db.query_one("SELECT COUNT(*) AS n FROM requirement_name_decisions")["n"] == 0
    again = client.post(
        "/api/name-decisions/undo", json={"event_id": body["event_id"]}, headers=headers
    )
    assert again.status_code == 409


def test_ignore_project_name_keeps_project_decision_and_restores_previous_row(tmp_path):
    client, _settings, headers, db, _root = _api(tmp_path)
    seed_meeting(db, "m-1", "沟通")
    _set_link(db, "m-1", "unresolved", new_project_name="数据中台")
    other_id = make_project(db, "报价系统")
    db.execute(
        """INSERT INTO name_decisions(norm_key, name, decision, target_id, decided_at)
           VALUES ('报价', '报价', 'project', ?, '2026-01-01')""",
        (other_id,),
    )
    # 已经「建成了别的项目」的名字，「不是新项目」不覆盖那一行。
    client.post("/api/project-names/ignore", json={"name": "报价"}, headers=headers)
    assert (
        db.query_one("SELECT decision FROM name_decisions WHERE norm_key='报价'")["decision"]
        == "project"
    )
    response = client.post(
        "/api/project-names/ignore", json={"name": "数据中台项目"}, headers=headers
    )
    assert response.json()["meetings_updated"] == 1
    assert _hint(client, "m-1") is None
    client.post(
        "/api/name-decisions/undo", json={"event_id": response.json()["event_id"]}, headers=headers
    )
    assert _hint(client, "m-1")["name"] == "数据中台"
    assert (
        db.query_one("SELECT COUNT(*) AS n FROM name_decisions WHERE norm_key='数据中台'")["n"] == 0
    )


def test_undo_window_expires(tmp_path):
    client, _settings, headers, db, _root = _api(tmp_path)
    seed_meeting(db, "m-1", "沟通")
    _set_link(db, "m-1", "unresolved", new_project_name="数据中台")
    event_id = client.post(
        "/api/project-names/ignore", json={"name": "数据中台"}, headers=headers
    ).json()["event_id"]
    old = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    db.execute("UPDATE events SET created_at=? WHERE id=?", (old, event_id))
    assert (
        client.post(
            "/api/name-decisions/undo", json={"event_id": event_id}, headers=headers
        ).status_code
        == 409
    )


def test_summary_counts_also_names_as_taken_and_delete_clears_decisions(tmp_path):
    client, _settings, headers, db, _root = _api(tmp_path)
    make_project(db, "云图AI", also=["数据中台"])
    seed_meeting(db, "m-1", "沟通")
    _set_link(db, "m-1", "unresolved", new_project_name="数据中台")
    assert client.get("/api/attribution/summary").json()["new_project_names"] == []
    assert _hint(client, "m-1") is None

    created = client.post("/api/projects", json={"name": "智慧园区"}, headers=headers).json()
    assert (
        db.query_one("SELECT target_id FROM name_decisions WHERE norm_key='智慧园区'")["target_id"]
        == created["id"]
    )
    client.delete(
        f"/api/projects/{created['id']}", headers={**headers, "Content-Type": "application/json"}
    )
    assert (
        db.query_one("SELECT COUNT(*) AS n FROM name_decisions WHERE norm_key='智慧园区'")["n"] == 0
    )


# ---------------------------------------------------------------------- 候选名


def test_name_candidates_orders_folder_spoken_ai_and_similar(tmp_path):
    client, settings, _headers, db, browse = _api(tmp_path)
    (browse / "云图看板").mkdir()
    (browse / "云图看板2026").mkdir()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, created_at, updated_at)
           VALUES ('gt-1', '数据看板', '["数据看版"]', '通用', ?, ?)""",
        (utc_now(), utc_now()),
    )
    segments = [(1000, "数据看版先说"), (4000, "数据看板怎么排"), (7000, "云图看板的首页")]
    seed_meeting(db, "m-1", "沟通", segments=segments)
    _set_link(
        db,
        "m-1",
        "unresolved",
        new_project_name="云图数据看板",
        new_name_spoken=["云图看板", "数据看板"],
    )
    seed_meeting(db, "m-2", "再沟通", segments=[(2500, "数据看板")])
    _set_link(db, "m-2", "unresolved", new_project_name="云图数据看板项目")
    client.app.state.roots_cache.refresh()

    body = client.get("/api/meetings/m-1/name-candidates").json()

    assert body["folders_state"] == "ready"
    names = [
        (
            item["name"],
            bool(item["folder_path"]),
            item["spoken"],
            item["ai"],
            bool(item["similar_folder_path"]),
        )
        for item in body["candidates"]
    ]
    assert names == [
        ("云图看板", True, None, False, False),
        (
            "数据看板",
            False,
            {"count": 2, "first_ms": 1000, "anchors_ms": [1000, 4000]},
            False,
            False,
        ),
        ("云图数据看板", False, None, True, False),
        ("云图看板2026", False, None, False, True),
    ]
    assert body["default_action"] == "create_project"
    assert body["folder"] == {"mode": "mount", "path": str((browse / "云图看板").resolve())}
    assert [item["id"] for item in body["meetings"]] == ["m-1", "m-2"]
    assert body["meetings"][1]["said_ms"] == 2500
    assert body["create_parent"] is None  # 还没有可参照的项目文件夹


def test_name_candidates_for_requirement_uses_root_children(tmp_path):
    client, settings, headers, db, browse = _api(tmp_path)
    root = browse / "云图AI"
    (root / "数据看板").mkdir(parents=True)
    project = client.post(
        "/api/projects", json={"name": "云图AI", "material_roots": [str(root)]}, headers=headers
    ).json()
    _requirement_hint_meeting(db, "m-1", project["id"])
    client.app.state.roots_cache.refresh()

    body = client.get("/api/meetings/m-1/name-candidates").json()

    assert body["default_action"] == "create_requirement"
    assert body["project"] == {"id": project["id"], "name": "云图AI"}
    assert body["candidates"][0]["folder_path"] == str((root / "数据看板").resolve())
    assert body["folder"] == {"mode": "mount", "path": str((root / "数据看板").resolve())}


# ---------------------------------------------------------------------- 建成项目


def test_create_project_from_hint_settles_names_and_adds_spoken_also(tmp_path):
    client, _settings, headers, db, _browse = _api(tmp_path)
    segments = [(1000, "数据看板先说"), (4000, "数据看板怎么排"), (7000, "云图看板")]
    seed_meeting(db, "m-1", "沟通", segments=segments)
    _set_link(
        db, "m-1", "unresolved", new_project_name="云图数据看板", new_name_spoken=["数据看板"]
    )
    seed_meeting(db, "m-2", "沟通", segments=[(1, "数据看板")])
    _set_link(db, "m-2", "unresolved", new_project_name="云图数据看板")

    response = client.post(
        "/api/projects",
        json={"name": "云图看板", "source_name": "云图数据看板", "meeting_ids": ["m-1", "m-2"]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meetings_assigned"] == 2
    assert body["spoken_added"]["name"] == "数据看板"
    project = client.get(f"/api/projects/{body['id']}/board").json()
    also = project.get("also_names") or project["project"]["also_names"]
    assert {"name": "数据看板", "source": "spoken"} in also
    keys = {
        row["norm_key"]: row["decision"] for row in db.query_all("SELECT * FROM name_decisions")
    }
    assert keys == {"云图看板": "project", "云图数据看板": "project", "数据看板": "project"}
    assert client.get("/api/attribution/summary").json()["new_project_names"] == []

    undone = client.post(
        f"/api/projects/{body['id']}/also-names/spoken/undo",
        json={"event_id": body["spoken_added"]["event_id"]},
        headers=headers,
    )
    assert undone.json() == {"ok": True, "removed": True}
    also = db.query_one("SELECT also_names FROM projects WHERE id=?", (body["id"],))["also_names"]
    assert "数据看板" not in also
    assert (
        db.query_one("SELECT COUNT(*) AS n FROM name_decisions WHERE norm_key='数据看板'")["n"] == 0
    )


def test_spoken_also_is_skipped_when_not_qualified(tmp_path):
    client, _settings, headers, db, _browse = _api(tmp_path)
    make_project(db, "报价系统", also=["数据看板"])
    seed_meeting(db, "m-1", "沟通", segments=[(1, "数据看板"), (2, "数据看板")])
    _set_link(
        db, "m-1", "unresolved", new_project_name="云图数据看板", new_name_spoken=["数据看板"]
    )

    body = client.post(
        "/api/projects",
        json={"name": "云图看板", "source_name": "云图数据看板", "meeting_ids": ["m-1"]},
        headers=headers,
    ).json()

    assert body["spoken_added"] is None  # 「数据看板」已经是别的项目的也叫
    assert body["meetings_assigned"] == 1


def test_exact_duplicate_project_name_returns_suggestion(tmp_path):
    client, _settings, headers, _db, _browse = _api(tmp_path)
    existing = client.post("/api/projects", json={"name": "云图AI"}, headers=headers).json()

    response = client.post("/api/projects", json={"name": "云图AI", "force": True}, headers=headers)

    assert response.status_code == 409
    assert response.json()["suggestion"]["id"] == existing["id"]
    assert response.json()["suggestion"]["exact"] is True


# ---------------------------------------------------------------------- 建成需求


def test_name_as_requirement_for_unassigned_meetings_and_undo(tmp_path):
    client, _settings, headers, db, browse = _api(tmp_path)
    root = browse / "云图AI"
    (root / "数据看板").mkdir(parents=True)
    project = client.post(
        "/api/projects", json={"name": "云图AI", "material_roots": [str(root)]}, headers=headers
    ).json()
    seed_meeting(db, "m-1", "沟通", segments=BOARD)
    _set_link(db, "m-1", "unresolved", new_project_name="数据看板")
    seed_meeting(db, "m-2", "沟通")
    _set_link(db, "m-2", "unresolved", new_project_name="数据看板项目")

    missing = client.post(
        "/api/meetings/m-1/name-as-requirement", json={"title": "数据看板"}, headers=headers
    )
    assert missing.status_code == 400

    response = client.post(
        "/api/meetings/m-1/name-as-requirement",
        json={
            "title": "数据看板",
            "project_id": project["id"],
            "folder_path": str(root / "数据看板"),
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["existing"] is False and body["priority"] == "P2"
    assert body["meetings_assigned"] == 2 and body["meetings_linked"] == 2
    assert body["folder_attached"] == str((root / "数据看板").resolve())
    for meeting_id in ("m-1", "m-2"):
        row = db.query_one(
            "SELECT project_id, project_origin FROM meetings WHERE id=?", (meeting_id,)
        )
        assert (row["project_id"], row["project_origin"]) == (project["id"], "manual")
    requirement = db.query_one("SELECT * FROM requirements WHERE id=?", (body["requirement_id"],))
    assert requirement["priority"] == "P2"
    assert (
        db.query_one("SELECT name, decision FROM name_decisions WHERE norm_key='数据看板'")[
            "decision"
        ]
        == "ignored"
    )

    undone = client.post("/api/meetings/m-1/name-as-requirement/undo", json={}, headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["requirement_deleted"] is True
    assert undone.json()["meetings_restored"] == 2
    assert db.query_one("SELECT COUNT(*) AS n FROM requirements")["n"] == 0
    assert db.query_one("SELECT project_id FROM meetings WHERE id='m-2'")["project_id"] is None
    assert _hint(client, "m-2")["name"] == "数据看板项目"
    assert (
        db.query_one("SELECT COUNT(*) AS n FROM name_decisions WHERE norm_key='数据看板'")["n"] == 0
    )


def test_name_as_requirement_links_existing_requirement_and_keeps_it_on_undo(tmp_path):
    client, _settings, headers, db, _browse = _api(tmp_path)
    project_id = make_project(db, "云图AI")
    other_id = make_project(db, "报价系统")
    existing = _requirement(db, project_id, "数据 看板")
    _requirement_hint_meeting(db, "m-1", project_id, origin="manual")

    wrong = client.post(
        "/api/meetings/m-1/name-as-requirement",
        json={"title": "数据看板", "project_id": other_id},
        headers=headers,
    )
    assert wrong.status_code == 400
    body = client.post(
        "/api/meetings/m-1/name-as-requirement", json={"title": "数据看板"}, headers=headers
    ).json()

    assert body["existing"] is True and body["requirement_id"] == existing
    assert body["meetings_assigned"] == 0 and body["meetings_linked"] == 1
    undone = client.post(
        "/api/meetings/m-1/name-as-requirement/undo", json={}, headers=headers
    ).json()
    assert undone["requirement_deleted"] is False
    assert db.query_one("SELECT COUNT(*) AS n FROM requirements WHERE id=?", (existing,))["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM requirement_meetings")["n"] == 0


def test_name_as_requirement_keeps_going_when_folder_is_invalid(tmp_path):
    client, _settings, headers, db, browse = _api(tmp_path)
    project_id = make_project(db, "云图AI")
    _requirement_hint_meeting(db, "m-1", project_id)

    body = client.post(
        "/api/meetings/m-1/name-as-requirement",
        json={"title": "数据看板", "folder_path": str(browse / "不存在")},
        headers=headers,
    ).json()

    assert body["folder_attached"] is None and body["folder_error"]
    assert db.query_one("SELECT COUNT(*) AS n FROM requirements")["n"] == 1
    assert _hint(client, "m-1") is None
