"""冷启动整理（v13 第一期 1b-4）：弱归属复评、旧分组、同名文件夹、重复挂载。"""
import json

from meeting_workbench import cold_start
from meeting_workbench.db import utc_now
from meeting_workbench.project_linking import ProjectLinker

from .test_project_linking import HIGH, LOW, llm_answers, make_db, make_project, seed_meeting
from .test_project_names import _project, _setup


def _weak(db, meeting_id, project_id, method="semantic"):
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='ai' WHERE id=?", (project_id, meeting_id)
    )
    db.execute(
        """INSERT INTO project_links(meeting_id, minutes_version_id, status, project_id, method, created_at)
           VALUES (?, ?, 'done', ?, ?, ?)""",
        (meeting_id, f"mv-{meeting_id}", project_id, method, utc_now()),
    )


def _link(db, meeting_id):
    return db.query_one(
        "SELECT * FROM project_links WHERE meeting_id=? ORDER BY id DESC LIMIT 1", (meeting_id,)
    )


def _app_state(db, key):
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (key,))
    return row["value"] if row else None


def _term(db, term, scope, project_id=None):
    term_id = f"gt-{term}"
    db.execute(
        """INSERT INTO glossary_terms(id, term, scope, project_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (term_id, term, scope, project_id, utc_now(), utc_now()),
    )
    return term_id


def _term_row(db, term_id):
    return db.query_one("SELECT scope, project_id FROM glossary_terms WHERE id=?", (term_id,))


# ---------------------------------------------------------------------- 弱归属复评


def test_same_conclusion_only_adds_evidence(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    seed_meeting(db, "vm-1", "云图科研用药周会")
    _weak(db, "vm-1", project_id, "task_majority")
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"))

    stats = cold_start.run(db, settings, ProjectLinker(db, settings))

    link = _link(db, "vm-1")
    assert (link["status"], link["method"]) == ("done", "llm_high")
    assert json.loads(link["evidence_json"])
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-1'")
    assert (meeting["project_id"], meeting["project_origin"]) == (project_id, "ai")
    assert stats["reevaluated"] == 1 and stats["done"] is True
    assert _app_state(db, "reeval_weak_ai") == "done"
    assert _app_state(db, "cold_start_done") == "1"


def test_different_conclusion_asks_the_user_with_the_old_project_first(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    old = make_project(db, "云图科研用药")
    other = make_project(db, "数据中台")
    seed_meeting(db, "vm-2", "指标口径", "# 摘要\n数据中台的指标口径要统一。")
    _weak(db, "vm-2", old)
    llm_answers(monkeypatch, HIGH.format(name="数据中台"))

    cold_start.run(db, settings, ProjectLinker(db, settings))

    link = _link(db, "vm-2")
    assert (link["status"], link["method"]) == ("needs_review", "reeval")
    candidates = json.loads(link["candidates_json"])
    assert [candidate["project_id"] for candidate in candidates] == [old, other]
    assert "内容相近" in link["reason"]
    # 会议先不动
    meeting = db.query_one("SELECT project_id, project_origin FROM meetings WHERE id='vm-2'")
    assert (meeting["project_id"], meeting["project_origin"]) == (old, "ai")


def test_reevaluation_is_done_in_small_rounds_and_only_once(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    for index in range(cold_start.REEVAL_PER_ROUND + 2):
        seed_meeting(db, f"vm-{index}", "云图科研用药周会")
        _weak(db, f"vm-{index}", project_id)
    calls = []
    llm_answers(monkeypatch, HIGH.format(name="云图科研用药"), prompts=calls)
    linker = ProjectLinker(db, settings)

    first = cold_start.run(db, settings, linker)
    assert first["reevaluated"] == cold_start.REEVAL_PER_ROUND and first["done"] is False
    second = cold_start.run(db, settings, linker)
    assert second["reevaluated"] == 2 and second["done"] is True
    third = cold_start.run(db, settings, linker)
    assert third == {"groups": None, "reevaluated": 0, "done": True}
    assert len(calls) == cold_start.REEVAL_PER_ROUND + 2


def test_llm_failure_leaves_the_meeting_for_the_next_round(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    seed_meeting(db, "vm-9", "周会")
    _weak(db, "vm-9", project_id)
    llm_answers(monkeypatch, TimeoutError("超时"))

    stats = cold_start.run(db, settings, ProjectLinker(db, settings))

    link = _link(db, "vm-9")
    assert (link["status"], link["method"], link["attempts"]) == ("done", "semantic", 1)
    assert stats["done"] is False


def test_trusted_and_manual_attributions_are_not_reevaluated(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    seed_meeting(db, "vm-a", "周会", project_id=project_id, origin="manual")
    seed_meeting(db, "vm-b", "周会")
    _weak(db, "vm-b", project_id, method="llm")
    calls = []
    llm_answers(monkeypatch, LOW, prompts=calls)

    stats = cold_start.run(db, settings, ProjectLinker(db, settings))

    assert stats["reevaluated"] == 0 and calls == []
    assert _link(db, "vm-b")["method"] == "llm"


# ---------------------------------------------------------------------- 旧分组


def test_legacy_groups_are_organized_and_can_be_undone(tmp_path):
    client, settings, headers, db, _root = _setup(tmp_path)
    yuntu = _project(client, headers, "云图科研用药")["id"]
    _project(client, headers, "数据中台")
    same = _term(db, "随访窗口", "数据中台")  # 和项目同名：直接挂上，不打扰
    part = _term(db, "受试者编号", "云图")  # 是某个项目名的一部分：归那个项目
    other = _term(db, "互联网诊疗", "互联网医院")  # 都不是：归公共
    public = _term(db, "会议纪要", "通用")

    with db.transaction() as connection:
        result = cold_start.organize_legacy_groups(db, connection)

    assert result == {"silent": 1, "organized": 2, "changed_terms": 3}
    assert _term_row(db, same)["project_id"] is not None
    assert (_term_row(db, part)["project_id"], _term_row(db, part)["scope"]) == (yuntu, "云图科研用药")
    assert (_term_row(db, other)["project_id"], _term_row(db, other)["scope"]) == (None, "通用")
    assert _term_row(db, public)["scope"] == "通用"

    summary = client.get("/api/glossary/legacy-groups").json()["summary"]
    assert [(group["scope"], group["project_name"]) for group in summary["groups"]] == [
        ("云图", "云图科研用药"),
        ("互联网医院", None),
    ]
    assert summary["undone"] is False

    # 整理后被人改过的词条，撤销时不动
    db.execute("UPDATE glossary_terms SET project_id=NULL, scope='通用' WHERE id=?", (part,))
    undone = client.post("/api/glossary/legacy-groups/undo", json={}, headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json() == {"restored": 1}
    assert _term_row(db, other)["scope"] == "互联网医院"
    assert _term_row(db, part)["scope"] == "通用"
    assert client.get("/api/glossary/legacy-groups").json()["summary"]["undone"] is True
    assert client.post("/api/glossary/legacy-groups/undo", json={}, headers=headers).status_code == 409

    assert client.post("/api/glossary/legacy-groups/dismiss", json={}, headers=headers).status_code == 200
    assert client.get("/api/glossary/legacy-groups").json()["summary"] is None


def test_cold_start_organizes_groups_once(tmp_path, monkeypatch):
    db, settings = make_db(tmp_path)
    make_project(db, "云图科研用药")
    term_id = _term(db, "受试者编号", "云图")
    llm_answers(monkeypatch, LOW)
    linker = ProjectLinker(db, settings)

    first = cold_start.run(db, settings, linker)
    assert first["groups"]["organized"] == 1
    assert (settings.data_dir / "glossary-snapshot.json").exists()
    # 之后用户自己又建了一个分组，不会再被自动整理
    db.execute("UPDATE glossary_terms SET project_id=NULL, scope='新分组' WHERE id=?", (term_id,))
    cold_start.run(db, settings, linker)
    assert _term_row(db, term_id)["scope"] == "新分组"


# ---------------------------------------------------------------------- 同名文件夹、重复挂载


def test_projects_without_folders_get_same_name_suggestions(tmp_path):
    client, settings, headers, db, browse_root = _setup(tmp_path)
    parent = browse_root / "项目"
    for name in ("数据中台", "云图科研用药二期资料", "北辰仓储"):
        (parent / name).mkdir(parents=True)
    mounted = _project(client, headers, "北辰仓储", material_roots=[str(parent / "北辰仓储")])
    zt = _project(client, headers, "数据中台")["id"]
    yt = _project(client, headers, "云图科研用药")["id"]
    _project(client, headers, "蓝鲸云")  # 找不到文件夹的不出现

    items = client.get("/api/cold-start/folders").json()["items"]
    by_project = {item["project_id"]: item for item in items}
    assert set(by_project) == {zt, yt}
    assert by_project[zt]["match"] == "exact"
    assert by_project[zt]["path"].endswith("/项目/数据中台")
    assert by_project[yt]["match"] == "similar"
    assert mounted["id"] not in by_project

    # 说过不挂的不再问
    assert client.post(
        "/api/cold-start/folders/decline", json={"project_ids": [yt]}, headers=headers
    ).status_code == 200
    assert [item["project_id"] for item in client.get("/api/cold-start/folders").json()["items"]] == [zt]

    # 稍后：几天内都不问
    snoozed = client.post("/api/cold-start/folders/snooze", json={}, headers=headers).json()
    after = client.get("/api/cold-start/folders").json()
    assert after == {"items": [], "snoozed_until": snoozed["snoozed_until"]}


def test_board_lists_other_projects_sharing_a_folder(tmp_path):
    client, settings, headers, db, browse_root = _setup(tmp_path)
    folder = browse_root / "项目" / "云图"
    folder.mkdir(parents=True)
    first = _project(client, headers, "云图科研用药", material_roots=[str(folder)])["id"]
    second = _project(client, headers, "云图二期")["id"]
    # 老数据里的重复挂载：新接口会拒绝，这里直接写库
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (second, str(folder.resolve()), utc_now()),
    )

    root = client.get(f"/api/projects/{second}/board").json()["material_roots"][0]
    assert root["shared_with"] == [{"project_id": first, "project_name": "云图科研用药"}]
    assert root["cards_owner_id"] == first
    own = client.get(f"/api/projects/{first}/board").json()["material_roots"][0]
    assert own["cards_owner_id"] == first
    assert own["shared_with"] == [{"project_id": second, "project_name": "云图二期"}]
