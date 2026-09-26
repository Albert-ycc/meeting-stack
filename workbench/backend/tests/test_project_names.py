"""项目名字与项目整理（v13 / 第一期 1b-2）：近似重名、也叫、新项目回扫、忽略名字、
合并、删除空项目、同名文件夹。"""
import json
import os
import uuid
from pathlib import Path

from meeting_workbench import project_linking as project_linking_module
from meeting_workbench.db import Database, utc_now
from meeting_workbench.project_names import sanitize_folder_name

from .test_attribution_state import _add_link, _draft_task, _task_project
from .test_project_linking import make_term, meeting_row, seed_meeting
from .test_tasks_api import make_client, write_headers


def _setup(tmp_path, browse_root=None):
    browse_root = browse_root or (tmp_path / "browse")
    if not str(browse_root).startswith("/Volumes"):
        browse_root.mkdir(exist_ok=True)
    client, settings = make_client(tmp_path, material_browse_root=browse_root)
    return client, settings, write_headers(client), Database(settings.database_path), browse_root


def _create(client, headers, name, **extra):
    return client.post("/api/projects", json={"name": name, **extra}, headers=headers)


def _project(client, headers, name, **extra):
    response = _create(client, headers, name, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client, headers, project_id, **body):
    return client.patch(f"/api/projects/{project_id}", json=body, headers=headers)


def _state(client, meeting_id):
    return client.get(f"/api/meetings/{meeting_id}").json()["attribution"]


def _no_llm(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("回扫不该调模型")

    monkeypatch.setattr(project_linking_module, "call_llm", refuse)


# ---------------------------------------------------------------------- 近似重名


def test_similar_project_name_asks_before_creating(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    existing = _project(client, headers, "云图科研用药")
    assert _patch(client, headers, existing["id"], also_names=["云图"]).status_code == 200

    same = _create(client, headers, "云图 项目")
    similar = _create(client, headers, "云图科研")
    forced = _create(client, headers, "云图科研", force=True)

    assert same.status_code == 409
    assert same.json()["detail"] == "已有「云图科研用药」（又称 云图），是不是它？"
    assert same.json()["suggestion"] == {
        "project_id": existing["id"],
        "name": "云图科研用药",
        "also_names": ["云图"],
        "matched": "云图",
        "match": "same",
    }
    assert similar.status_code == 409
    assert similar.json()["suggestion"]["match"] == "similar"
    assert forced.status_code == 200, forced.text
    assert {p["name"] for p in client.get("/api/projects").json()} == {"云图科研用药", "云图科研"}


def test_short_names_are_not_treated_as_similar(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    _project(client, headers, "云图科研用药")

    response = _create(client, headers, "云药")

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------- 也叫与改名


def test_renaming_keeps_the_old_name_as_a_former_name(tmp_path):
    client, settings, headers, db, _root = _setup(tmp_path)
    project = _project(client, headers, "云图AI")
    term_id = make_term(db, project["id"], "灰度方案")
    db.execute("UPDATE glossary_terms SET scope='云图AI' WHERE id=?", (term_id,))

    renamed = _patch(client, headers, project["id"], name="云图智能").json()

    assert renamed["also_names"] == [{"name": "云图AI", "source": "former"}]
    assert db.query_one("SELECT scope FROM glossary_terms WHERE id=?", (term_id,))["scope"] == (
        "云图智能"
    )
    # 改回旧名：旧名从也叫里拿掉，刚才的名字变成曾用名
    back = _patch(client, headers, project["id"], name="云图AI").json()
    assert back["also_names"] == [{"name": "云图智能", "source": "former"}]


def test_also_names_are_validated(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    other = _project(client, headers, "云图科研用药")
    _patch(client, headers, other["id"], also_names=["云图"])
    project = _project(client, headers, "数据中台")

    digits = _patch(client, headers, project["id"], also_names=["2024"])
    too_long = _patch(client, headers, project["id"], also_names=["数" * 21])
    generic = _patch(client, headers, project["id"], also_names=["方案"])
    taken = _patch(client, headers, project["id"], also_names=["云图"])
    taken_name = _patch(client, headers, project["id"], also_names=["云图科研用药"])
    ok = _patch(client, headers, project["id"], also_names=["中台", "DataHub", "中台", "数据中台"])

    assert (digits.status_code, digits.json()["detail"]) == (400, "叫法要 2–20 个字，不能是纯数字")
    assert too_long.status_code == 400
    assert (generic.status_code, generic.json()["detail"]) == (400, "「方案」太常见，不能当叫法")
    assert taken.status_code == 409
    assert taken.json()["detail"] == "「云图」已经是「云图科研用药」的叫法，一个叫法只能指向一个项目"
    assert taken_name.status_code == 409
    assert ok.status_code == 200, ok.text
    # 两字叫法可以存；重复的、和正式名相同的去掉
    assert ok.json()["also_names"] == [
        {"name": "中台", "source": "manual"},
        {"name": "DataHub", "source": "manual"},
    ]


def test_renaming_onto_another_projects_name_is_refused(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    other = _project(client, headers, "云图科研用药")
    _patch(client, headers, other["id"], also_names=["云图"])
    project = _project(client, headers, "数据中台")

    response = _patch(client, headers, project["id"], name="云图")

    assert response.status_code == 409
    assert "云图科研用药" in response.json()["detail"]


def test_former_names_survive_editing_the_also_list(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    project = _project(client, headers, "云图AI")
    _patch(client, headers, project["id"], name="云图智能")

    updated = _patch(
        client, headers, project["id"], also_names=["云图AI", "云图看板"]
    ).json()

    assert updated["also_names"] == [
        {"name": "云图AI", "source": "former"},
        {"name": "云图看板", "source": "manual"},
    ]


# ---------------------------------------------------------------------- 新项目回扫


def test_new_project_turns_matching_unresolved_meetings_into_questions(tmp_path, monkeypatch):
    client, _settings, headers, db, _root = _setup(tmp_path)
    _no_llm(monkeypatch)
    seed_meeting(db, "m-minutes", "周三沟通", markdown="# 摘要\n智慧园区的招标下周开。")
    _add_link(db, "m-minutes", "unresolved")
    seed_meeting(
        db, "m-transcript", "客户电话", segments=[(1000, "我们聊聊"), (8000, "智慧园区那边怎么样")]
    )
    _add_link(db, "m-transcript", "unresolved")
    seed_meeting(db, "m-named", "现场踏勘")
    _add_link(db, "m-named", "unresolved", new_project_name="智慧园区项目")
    seed_meeting(db, "m-other", "闲聊")
    _add_link(db, "m-other", "unresolved")
    seed_meeting(db, "m-manual", "智慧园区复盘", origin="manual")
    seed_meeting(db, "m-pending", "智慧园区启动会")

    created = _project(client, headers, "智慧园区")

    assert sorted(created["needs_review_meeting_ids"]) == ["m-minutes", "m-named", "m-transcript"]
    for meeting_id in ("m-minutes", "m-named", "m-transcript"):
        attribution = _state(client, meeting_id)
        assert attribution["state"] == "needs_review", meeting_id
        assert [c["project_id"] for c in attribution["candidates"]] == [created["id"]]
        assert meeting_row(db, meeting_id) == {"project_id": None, "project_origin": None}
    transcript = _state(client, "m-transcript")
    assert transcript["evidence"][0]["anchors_ms"] == [8000]
    assert "提到它 1 次" in transcript["reason"]
    assert "像新项目「智慧园区项目」" in _state(client, "m-named")["reason"]
    assert _state(client, "m-other")["state"] == "none"
    assert _state(client, "m-manual")["state"] == "manual_none"
    assert _state(client, "m-pending")["state"] == "ai_pending"


def test_two_character_project_name_needs_two_mentions(tmp_path, monkeypatch):
    client, _settings, headers, db, _root = _setup(tmp_path)
    _no_llm(monkeypatch)
    seed_meeting(db, "m-once", "周会", markdown="# 摘要\n云图上线。")
    _add_link(db, "m-once", "unresolved")
    seed_meeting(db, "m-twice", "周会", markdown="# 摘要\n云图上线，云图验收。")
    _add_link(db, "m-twice", "unresolved")

    created = _project(client, headers, "云图")

    assert created["needs_review_meeting_ids"] == ["m-twice"]


def test_building_a_new_project_from_meetings_assigns_them(tmp_path, monkeypatch):
    client, _settings, headers, db, _root = _setup(tmp_path)
    _no_llm(monkeypatch)
    seed_meeting(db, "m-1", "园区沟通")
    _add_link(db, "m-1", "unresolved", new_project_name="智慧园区")
    draft = _draft_task(db, "m-1")

    created = _project(client, headers, "智慧园区", meeting_ids=["m-1"])

    assert created["meetings_assigned"] == 1
    assert meeting_row(db, "m-1") == {"project_id": created["id"], "project_origin": "manual"}
    assert _task_project(db, draft) == created["id"]
    assert created["needs_review_meeting_ids"] == []

    missing = _create(client, headers, "数据中台", meeting_ids=["m-missing"])
    assert missing.status_code == 404
    assert "数据中台" not in {p["name"] for p in client.get("/api/projects").json()}


# ---------------------------------------------------------------------- 忽略名字


def test_ignoring_a_name_stops_suggesting_it(tmp_path):
    client, _settings, headers, db, _root = _setup(tmp_path)
    seed_meeting(db, "m-1", "分享会")
    _add_link(db, "m-1", "unresolved", new_project_name="内部分享")
    seed_meeting(db, "m-2", "分享会")
    _add_link(db, "m-2", "unresolved", new_project_name="内部 分享")
    assert _state(client, "m-1")["state"] == "new_project"

    response = client.post(
        "/api/project-names/ignore", json={"name": "内部分享"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["meetings_updated"] == 2
    assert _state(client, "m-1")["state"] == "none"
    assert _state(client, "m-2")["state"] == "none"
    assert client.get("/api/attribution/summary").json()["new_project_names"] == []
    decision = db.query_one("SELECT decision FROM name_decisions WHERE norm_key='内部分享'")
    assert decision["decision"] == "ignored"


# ---------------------------------------------------------------------- 合并与删除


def test_merging_moves_everything_and_keeps_the_name_as_also(tmp_path):
    client, settings, headers, db, root = _setup(tmp_path)
    shared_folder = root / "云图"
    own_folder = root / "云图旧资料"
    for folder in (shared_folder, own_folder):
        folder.mkdir()
    dst = _project(client, headers, "云图科研用药", material_roots=[str(shared_folder)])
    src = _project(client, headers, "云图AI", force=True)
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (src["id"], str(shared_folder.resolve()), utc_now()),
    )
    _patch(client, headers, src["id"], material_roots=[str(shared_folder.resolve()), str(own_folder)])
    seed_meeting(db, "m-ai", "周会", project_id=src["id"], origin="ai")
    seed_meeting(db, "m-review", "评审")
    _add_link(db, "m-review", "needs_review", candidates=[{"project_id": src["id"], "count": 2}])
    task = _draft_task(db, "m-ai", src["id"], status="confirmed")
    for project in (src, dst):
        client.post(
            "/api/requirements",
            json={"project_id": project["id"], "title": "登录改版", "priority": "P1"},
            headers=headers,
        )
    term_id = make_term(db, src["id"], "灰度方案")

    response = client.post(
        f"/api/projects/{src['id']}/merge-into/{dst['id']}", json={}, headers=headers
    )

    assert response.status_code == 200, response.text
    merged = response.json()
    assert merged["id"] == dst["id"]
    assert merged["merge"]["meetings_moved"] == 1
    assert merged["merge"]["renamed_requirements"][0]["to"] == "登录改版（原 云图AI）"
    assert {"name": "云图AI", "source": "merged"} in merged["also_names"]
    assert sorted(root_row["path"] for root_row in merged["material_roots"]) == sorted(
        [str(shared_folder.resolve()), str(own_folder.resolve())]
    )
    assert meeting_row(db, "m-ai") == {"project_id": dst["id"], "project_origin": "ai"}
    assert _task_project(db, task) == dst["id"]
    term = db.query_one("SELECT project_id, scope FROM glossary_terms WHERE id=?", (term_id,))
    assert term == {"project_id": dst["id"], "scope": "云图科研用药"}
    assert [c["project_id"] for c in _state(client, "m-review")["candidates"]] == [dst["id"]]
    assert db.query_one("SELECT 1 AS present FROM projects WHERE id=?", (src["id"],)) is None
    titles = sorted(
        r["title"] for r in db.query_all("SELECT title FROM requirements WHERE project_id=?", (dst["id"],))
    )
    assert titles == ["登录改版", "登录改版（原 云图AI）"]


def test_merging_into_itself_is_refused(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path)
    project = _project(client, headers, "云图AI")

    response = client.post(
        f"/api/projects/{project['id']}/merge-into/{project['id']}", json={}, headers=headers
    )

    assert response.status_code == 400


def test_only_empty_projects_can_be_deleted(tmp_path):
    client, settings, headers, db, _root = _setup(tmp_path)
    busy = _project(client, headers, "云图AI")
    seed_meeting(db, "m-1", "周会", project_id=busy["id"], origin="manual")
    orphan = _project(client, headers, "数据中台")
    task_id = f"task-{uuid.uuid4().hex}"
    db.execute(
        """INSERT INTO tasks (id, title, status, origin, assignee, project_id,
                              status_changed_at, created_at, updated_at)
           VALUES (?, '做看板', 'confirmed', 'ai', 'ai', ?, ?, ?, ?)""",
        (task_id, orphan["id"], utc_now(), utc_now(), utc_now()),
    )
    term_id = make_term(db, orphan["id"], "指标口径")

    json_headers = {**headers, "Content-Type": "application/json"}
    refused = client.delete(f"/api/projects/{busy['id']}", headers=json_headers)
    deleted = client.delete(f"/api/projects/{orphan['id']}", headers=json_headers)

    assert refused.status_code == 409
    assert refused.json()["detail"] == "项目下还有1 场会，请先合并到别的项目"
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "tasks_unassigned": 1, "terms_to_public": 1}
    assert _task_project(db, task_id) is None
    term = db.query_one("SELECT project_id, scope FROM glossary_terms WHERE id=?", (term_id,))
    assert term == {"project_id": None, "scope": "通用"}
    snapshot = json.loads((settings.data_dir / "glossary-snapshot.json").read_text())
    assert snapshot is not None


# ---------------------------------------------------------------------- 同名文件夹


def test_sanitize_folder_name_replaces_exfat_illegal_characters():
    assert sanitize_folder_name("云图/看板") == ("云图-看板", ["/"])
    assert sanitize_folder_name('A:B*C?. ') == ("A-B-C-", ["*", ":", "?"])


def test_folder_matches_lists_unmounted_same_or_similar_folders(tmp_path):
    client, _settings, headers, _db, root = _setup(tmp_path)
    for name in ("云图AI", "云图AI资料", "数据中台", ".hidden", "声档会议记录", "已挂的"):
        (root / name).mkdir()
    (root / "客户A" / "云图 AI").mkdir(parents=True)
    os.utime(root / "数据中台", (2_000_000_000, 2_000_000_000))
    _project(client, headers, "别的项目", material_roots=[str(root / "已挂的")])

    result = client.get("/api/projects/folder-matches", params={"name": "云图AI"}).json()

    # 有已挂的根目录时只看它们的父目录一层，客户A/ 下的不算
    assert [(m["name"], m["match"]) for m in result["matches"]] == [
        ("云图AI", "exact"),
        ("云图AI资料", "similar"),
    ]
    names = [folder["name"] for folder in result["recent"]]
    assert names[0] == "数据中台"
    assert not {".hidden", "声档会议记录", "已挂的"} & set(names)
    assert result["create_parent"] == str(root.resolve())
    assert result["create_name"] == "云图AI"
    assert result["create_parent_state"] == "online"


def test_folder_matches_walks_two_levels_when_nothing_is_mounted(tmp_path):
    client, _settings, headers, _db, root = _setup(tmp_path)
    (root / "客户A" / "云图AI").mkdir(parents=True)
    project = _project(client, headers, "云图AI")

    result = client.get(f"/api/projects/{project['id']}/folder-suggestions").json()

    assert [(m["path"], m["match"]) for m in result["matches"]] == [
        (str((root / "客户A" / "云图AI").resolve()), "exact")
    ]


def test_creating_a_project_can_mount_or_create_its_folder(tmp_path):
    client, _settings, headers, _db, root = _setup(tmp_path)
    (root / "云图AI").mkdir()

    mounted = _project(
        client, headers, "云图AI", folder={"mode": "mount", "path": str(root / "云图AI")}
    )
    created = _project(
        client, headers, "云图/看板", folder={"mode": "create", "path": str(root)}
    )

    assert [r["path"] for r in mounted["material_roots"]] == [str((root / "云图AI").resolve())]
    assert (root / "云图-看板").is_dir()
    assert [r["path"] for r in created["material_roots"]] == [str((root / "云图-看板").resolve())]


def test_creating_a_folder_on_an_unplugged_disk_still_creates_the_project(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path, browse_root=Path("/Volumes"))

    created = _project(
        client,
        headers,
        "云图看板",
        folder={"mode": "create", "path": "/Volumes/资料盘/项目"},
    )

    assert created["material_roots"] == []
    assert created["folder_pending"] == {
        "path": "/Volumes/资料盘/项目/云图看板",
        "reason": "资料盘未连接，插上后再建文件夹",
    }
    assert not Path("/Volumes/资料盘").exists()


def test_failed_project_creation_removes_the_new_folder(tmp_path):
    client, _settings, headers, db, root = _setup(tmp_path)

    response = _create(
        client,
        headers,
        "云图看板",
        folder={"mode": "create", "path": str(root)},
        meeting_ids=["m-missing"],
    )

    assert response.status_code == 404
    assert not (root / "云图看板").exists()
