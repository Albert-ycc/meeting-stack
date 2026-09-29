"""第二期 2a：项目总文件夹、还没挂的文件夹、认领、盘不在时补建、文件夹改名后找回。"""

import json
import os
import time
from pathlib import Path

from meeting_workbench import folder_scan, project_folders
from meeting_workbench.db import Database, utc_now

from .test_tasks_api import make_client, write_headers


def _setup(tmp_path, browse_root=None):
    browse_root = browse_root or (tmp_path / "browse")
    if not str(browse_root).startswith("/Volumes"):
        browse_root.mkdir(exist_ok=True)
    client, settings = make_client(tmp_path, material_browse_root=browse_root)
    return client, settings, write_headers(client), Database(settings.database_path), browse_root


def _project(client, headers, name, **extra):
    response = client.post("/api/projects", json={"name": name, **extra}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _refresh(client):
    client.app.state.roots_cache.refresh()


def _parent(client):
    return client.get("/api/settings/project-parent").json()


def _set_parent(client, headers, path):
    return client.put("/api/settings/project-parent", json={"path": path}, headers=headers)


def JSON_HEADERS(headers):
    return {**headers, "Content-Type": "application/json"}


def _mkdirs(base: Path, *names: str) -> None:
    for name in names:
        (base / name).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------- 项目总文件夹


def test_parent_folder_validation(tmp_path):
    client, settings, headers, _db, root = _setup(tmp_path)
    _mkdirs(root, "项目/云图AI/需求A")
    _project(client, headers, "云图AI", material_roots=[str(root / "项目" / "云图AI")])

    assert _set_parent(client, headers, str(tmp_path)).status_code == 400  # 浏览根之外
    inside = _set_parent(client, headers, str(root / "项目" / "云图AI" / "需求A"))
    assert inside.status_code == 400 and "云图AI" in inside.json()["detail"]
    missing = _set_parent(client, headers, str(root / "没有这个"))
    assert missing.status_code == 400 and missing.json()["detail"] == "这个文件夹不存在"

    ok = _set_parent(client, headers, str(root / "项目"))
    assert ok.status_code == 200, ok.text
    assert ok.json()["path"] == str((root / "项目").resolve())
    # 清除
    cleared = _set_parent(client, headers, None).json()
    assert cleared["path"] is None


def test_parent_folder_refuses_protected_folders(tmp_path):
    browse = tmp_path
    client, settings, headers, _db, _root = _setup(tmp_path, browse_root=browse)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    inside = _set_parent(client, headers, str(settings.data_dir))
    contains = _set_parent(client, headers, str(tmp_path))
    assert inside.status_code == 400
    assert contains.status_code == 400
    assert "数据目录" in contains.json()["detail"]


def test_parent_folder_on_an_unplugged_disk_can_be_saved(tmp_path):
    client, _settings, headers, _db, _root = _setup(tmp_path, browse_root=Path("/Volumes"))

    saved = _set_parent(client, headers, "/Volumes/资料盘/项目")
    assert saved.status_code == 200, saved.text
    _refresh(client)
    status = _parent(client)
    assert status["path"] == "/Volumes/资料盘/项目"
    assert status["state"] == "volume_offline"
    assert status["unclaimed"]["state"] == "volume_offline"
    # 整个 /Volumes 不行
    assert _set_parent(client, headers, "/Volumes").status_code == 400


def test_suggested_parent_counts_where_mounted_folders_live(tmp_path):
    client, _settings, headers, _db, root = _setup(tmp_path)
    _mkdirs(root, "项目/云图AI", "项目/北辰", "归档/老项目")
    _project(client, headers, "云图AI", material_roots=[str(root / "项目" / "云图AI")])
    _project(client, headers, "北辰", material_roots=[str(root / "项目" / "北辰")])
    _project(client, headers, "老项目", material_roots=[str(root / "归档" / "老项目")])

    status = _parent(client)

    assert status["path"] is None
    assert status["suggested"] == {
        "path": str((root / "项目").resolve()),
        "count": 2,
        "total": 3,
    }


def test_no_suggestion_without_any_mounted_folder(tmp_path):
    client, _settings, _headers, _db, _root = _setup(tmp_path)
    assert _parent(client)["suggested"] is None


# ---------------------------------------------------------------------- 还没挂的文件夹


def test_unclaimed_folders_are_filtered_at_request_time(tmp_path):
    client, _settings, headers, db, root = _setup(tmp_path)
    parent = root / "项目"
    _mkdirs(
        parent,
        "云图AI",
        "云图AI二期",
        "容器/北辰",
        "拒绝过的",
        "资料",
        ".隐藏",
        "声档会议记录",
        "node_modules",
        "$RECYCLE.BIN",
    )
    (parent / "一个文件.txt").write_text("x")
    _project(client, headers, "云图AI二期", material_roots=[str(parent / "云图AI二期")])
    _project(client, headers, "北辰", material_roots=[str(parent / "容器" / "北辰")])
    assert _set_parent(client, headers, str(parent)).status_code == 200
    _refresh(client)

    names = [row["name"] for row in _parent(client)["unclaimed"]["folders"]]
    # 云图AI 不会被当成已挂的云图AI二期的上级；容器、隐藏、系统和声档自己的文件夹不算
    assert names == ["云图AI", "拒绝过的", "资料"]

    # 写完立刻生效，不等缓存：「不是项目」和撤销
    decline = client.post(
        "/api/settings/project-parent/decline",
        json={"path": str((parent / "拒绝过的").resolve())},
        headers=headers,
    )
    assert decline.status_code == 200
    assert "拒绝过的" not in [row["name"] for row in _parent(client)["unclaimed"]["folders"]]
    client.delete(
        "/api/settings/project-parent/decline",
        params={"path": str((parent / "拒绝过的").resolve())},
        headers=JSON_HEADERS(headers),
    )
    assert "拒绝过的" in [row["name"] for row in _parent(client)["unclaimed"]["folders"]]

    # 挂上之后也立刻不算
    project_id = db.query_one("SELECT id FROM projects WHERE name='北辰'")["id"]
    client.post(
        f"/api/projects/{project_id}/material-roots",
        json={"path": str(parent / "云图AI")},
        headers=headers,
    )
    assert "云图AI" not in [row["name"] for row in _parent(client)["unclaimed"]["folders"]]


def test_unclaimed_rows_get_default_actions(tmp_path):
    client, _settings, headers, _db, root = _setup(tmp_path)
    parent = root / "项目"
    _mkdirs(
        parent,
        "云图 AI",
        "云图ai",
        "北辰仓储",
        "北辰仓储资料",
        "蓝鲸云",
        "归档",
        "海豚",
        "别处/海豚",
    )
    yt = _project(client, headers, "云图AI")
    bc = _project(client, headers, "北辰仓储", also_names=["北辰"])
    hd = _project(client, headers, "海豚", material_roots=[str(parent / "别处" / "海豚")])
    _set_parent(client, headers, str(parent))
    _refresh(client)

    rows = {row["name"]: row for row in _parent(client)["unclaimed"]["folders"]}

    # 轻键相同、项目还没挂文件夹：挂到它，每个项目只默认勾一行
    assert (rows["云图 AI"]["kind"], rows["云图 AI"]["action"]) == ("exact", "mount")
    assert rows["云图 AI"]["project_id"] == yt["id"]
    assert [rows["云图 AI"]["checked"], rows["云图ai"]["checked"]] == [True, False]
    assert rows["北辰仓储"]["checked"] is True and rows["北辰仓储"]["project_id"] == bc["id"]
    # 名字相近：挂到它？不勾
    assert (rows["北辰仓储资料"]["kind"], rows["北辰仓储资料"]["checked"]) == ("similar", False)
    # 项目已经挂了文件夹：再挂这个？不勾
    assert rows["海豚"]["kind"] == "exact_mounted" and rows["海豚"]["project_id"] == hd["id"]
    assert rows["海豚"]["project_roots"] == [str((parent / "别处" / "海豚").resolve())]
    # 通用名排到最后
    assert rows["归档"]["kind"] == "generic" and rows["归档"]["action"] is None
    assert rows["蓝鲸云"]["kind"] == "new" and rows["蓝鲸云"]["action"] == "create"
    ordered = [row["name"] for row in _parent(client)["unclaimed"]["folders"]]
    assert ordered[-1] == "归档"


def test_folder_endpoints_never_read_disk(tmp_path, monkeypatch):
    client, _settings, headers, db, root = _setup(tmp_path)
    _mkdirs(root, "项目/云图AI")
    project = _project(client, headers, "云图AI")
    db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)",
        (folder_scan.PARENT_KEY, str(root / "项目"), utc_now()),
    )

    def sleepy(*_args, **_kwargs):
        time.sleep(5)
        return []

    cache = client.app.state.roots_cache
    monkeypatch.setattr(cache, "_list_dirs", sleepy)
    monkeypatch.setattr(cache, "_state_of", lambda _p: time.sleep(5) or "online")
    monkeypatch.setattr("meeting_workbench.materials.volume_state", lambda _p: time.sleep(5))
    monkeypatch.setattr("meeting_workbench.project_folders.volume_state", lambda _p: time.sleep(5))
    started = time.monotonic()
    assert _parent(client)["state"] == "checking"
    assert (
        client.get("/api/projects/folder-matches", params={"name": "云图AI"}).json()["state"]
        == "checking"
    )
    assert (
        client.get(f"/api/projects/{project['id']}/folder-suggestions").json()["state"]
        == "checking"
    )
    assert client.get("/api/cold-start/folders").json()["state"] == "checking"
    assert time.monotonic() - started < 2


# ---------------------------------------------------------------------- 认领


def test_claiming_mounts_and_creates_in_one_batch(tmp_path, monkeypatch):
    client, settings, headers, db, root = _setup(tmp_path)
    parent = root / "项目"
    _mkdirs(parent, "云图AI", "蓝鲸云", "北辰仓储", "北辰", "海豚/子项目")
    yt = _project(client, headers, "云图AI")
    _project(client, headers, "北辰仓储二期")
    _project(client, headers, "子项目", material_roots=[str(parent / "海豚" / "子项目")])
    snapshots = []
    _count_snapshots(monkeypatch, snapshots)
    _set_parent(client, headers, str(parent))
    _refresh(client)

    result = client.post(
        "/api/settings/project-parent/claim",
        json={
            "items": [
                {"path": str(parent / "云图AI"), "action": "mount", "project_id": yt["id"]},
                {"path": str(parent / "蓝鲸云"), "action": "create"},
                {"path": str(parent / "北辰仓储"), "action": "create"},
                {"path": str(parent / "北辰"), "action": "create", "force": True},
            ]
        },
        headers=headers,
    ).json()

    items = {Path(item["path"]).name: item for item in result["items"]}
    assert items["云图AI"]["ok"] and items["云图AI"]["project_name"] == "云图AI"
    assert items["蓝鲸云"]["ok"] and items["蓝鲸云"]["project_name"] == "蓝鲸云"
    # 近似重名：这一行问「是不是它」
    assert items["北辰仓储"]["ok"] is False
    assert items["北辰仓储"]["suggestion"]["name"] == "北辰仓储二期"
    # 你选了仍然新建
    assert items["北辰"]["ok"] is True
    assert (result["created"], result["mounted"]) == (2, 1)
    # 整批只刷新一次快照
    assert len(snapshots) == 1
    roots = {row["path"] for row in db.query_all("SELECT path FROM project_material_roots")}
    assert str((parent / "蓝鲸云").resolve()) in roots
    # 新建的项目按色板轮着用颜色
    color = db.query_one("SELECT color FROM projects WHERE name='蓝鲸云'")["color"]
    assert color in project_folders.PROJECT_COLORS

    # 完全同名：只问「是不是它」（带上已有项目），不会建出第二个
    exact = client.post(
        "/api/settings/project-parent/claim",
        json={"items": [{"path": str(parent / "云图AI"), "action": "create"}]},
        headers=headers,
    ).json()["items"][0]
    assert exact["ok"] is False and exact["suggestion"]["id"] == yt["id"]


def _count_snapshots(monkeypatch, calls):
    """数认领时刷新了几次词典快照。"""
    import meeting_workbench.main as main_module

    real = main_module.rewrite_snapshot

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(main_module, "rewrite_snapshot", counting)


def test_claiming_into_an_existing_project_reports_nested_folders(tmp_path):
    client, _settings, headers, _db, root = _setup(tmp_path)
    parent = root / "项目"
    _mkdirs(parent, "云图AI/北辰")
    yt = _project(client, headers, "云图AI")
    _project(client, headers, "北辰", material_roots=[str(parent / "云图AI" / "北辰")])

    item = client.post(
        "/api/settings/project-parent/claim",
        json={
            "items": [{"path": str(parent / "云图AI"), "action": "mount", "project_id": yt["id"]}]
        },
        headers=headers,
    ).json()["items"][0]

    assert item["ok"] is True
    assert [row["project_name"] for row in item["nested"]] == ["北辰"]


# ---------------------------------------------------------------------- 盘不在时补建


def test_offline_project_folder_is_queued_and_shown(tmp_path):
    client, _settings, headers, db, _root = _setup(tmp_path, browse_root=Path("/Volumes"))

    created = _project(
        client, headers, "云图看板", folder={"mode": "create", "path": "/Volumes/资料盘/项目"}
    )

    assert created["pending_folder"] == {
        "path": "/Volumes/资料盘/项目/云图看板",
        "parent": "/Volumes/资料盘/项目",
        "state": "waiting",
        "reason": None,
    }
    row = db.query_one("SELECT parent, name, state FROM pending_project_folders")
    assert row == {"parent": "/Volumes/资料盘/项目", "name": None, "state": "waiting"}
    listed = next(p for p in client.get("/api/projects").json() if p["id"] == created["id"])
    assert listed["pending_folder"]["state"] == "waiting"
    # 项目改名后待建的文件夹名跟着变
    client.patch(f"/api/projects/{created['id']}", json={"name": "云图数据看板"}, headers=headers)
    detail = client.get(f"/api/projects/{created['id']}/board").json()
    assert detail["pending_folder"]["path"] == "/Volumes/资料盘/项目/云图数据看板"


def _queue(db, project_id, parent, name=None):
    db.execute(
        """INSERT INTO pending_project_folders(project_id, parent, name, state, created_at)
           VALUES (?, ?, ?, 'waiting', ?)""",
        (project_id, str(parent), name, utc_now()),
    )


def test_pending_folder_is_created_and_mounted_when_the_disk_is_back(tmp_path):
    client, _settings, headers, db, root = _setup(tmp_path)
    parent = root / "项目"
    parent.mkdir()
    project = _project(client, headers, "云图看板")
    _queue(db, project["id"], parent)

    _refresh(client)

    target = parent / "云图看板"
    assert target.is_dir()
    assert db.query_all(
        "SELECT path FROM project_material_roots WHERE project_id=?", (project["id"],)
    ) == [{"path": str(target.resolve())}]
    assert db.query_one("SELECT COUNT(*) AS n FROM pending_project_folders") == {"n": 0}
    notices = client.get("/api/cards/banner").json()["notices"]
    assert notices[-1]["kind"] == "folder_created"
    assert notices[-1]["project_name"] == "云图看板"
    assert notices[-1]["path"] == str(target.resolve())


def test_pending_folder_stops_when_the_location_is_gone(tmp_path):
    client, _settings, headers, db, root = _setup(tmp_path)
    project = _project(client, headers, "云图看板")
    _queue(db, project["id"], root / "不存在的位置")

    _refresh(client)
    row = db.query_one("SELECT state, last_error FROM pending_project_folders")
    assert row == {"state": "stopped", "last_error": "要放新文件夹的位置不存在了"}

    # 停下后不再每轮重试，也不再写库
    def rev():
        return int(db.query_one("SELECT value FROM app_state WHERE key='graph_rev'")["value"])

    before = rev()
    (root / "不存在的位置").mkdir()
    _refresh(client)
    assert rev() == before
    assert not (root / "不存在的位置" / "云图看板").exists()

    # 改位置后当场试一次
    moved = client.put(
        f"/api/projects/{project['id']}/pending-folder",
        json={"parent": str(root / "不存在的位置")},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["pending_folder"] is None
    assert (root / "不存在的位置" / "云图看板").is_dir()


def test_pending_folder_retries_after_the_disk_comes_back(tmp_path):
    _client, settings, _headers, db, root = _setup(tmp_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图看板', 'x')")
    parent = root / "项目"
    _queue(db, "p", parent)
    states = iter(["missing", "volume_offline", "online"])
    worker = project_folders.PendingFolders(db, settings, state_of=lambda _p: next(states))

    worker.drain()
    assert db.query_one("SELECT state FROM pending_project_folders")["state"] == "stopped"
    worker.drain()  # 掉盘
    parent.mkdir()
    done = worker.drain()  # 重新插上：再试一次
    assert [item["project_id"] for item in done] == ["p"]


def test_pending_folder_does_not_take_a_folder_owned_by_another_project(tmp_path):
    client, _settings, headers, db, root = _setup(tmp_path)
    parent = root / "项目"
    (parent / "云图看板").mkdir(parents=True)
    _project(client, headers, "别的", material_roots=[str(parent / "云图看板")])
    project = _project(client, headers, "云图看板")
    _queue(db, project["id"], parent)

    _refresh(client)

    row = db.query_one("SELECT state, last_error FROM pending_project_folders")
    assert row["state"] == "stopped" and "别的" in row["last_error"]
    # 本来就在的文件夹不删
    assert (parent / "云图看板").is_dir()


def test_giving_up_on_a_pending_folder(tmp_path):
    client, _settings, headers, db, root = _setup(tmp_path)
    project = _project(client, headers, "云图看板")
    _queue(db, project["id"], root / "没有")

    url = f"/api/projects/{project['id']}/pending-folder"
    assert client.delete(url, headers=JSON_HEADERS(headers)).status_code == 200
    assert db.query_one("SELECT COUNT(*) AS n FROM pending_project_folders") == {"n": 0}
    assert client.delete(url, headers=JSON_HEADERS(headers)).status_code == 404


# ---------------------------------------------------------------------- 改名找回


def _card(folder: Path, meeting_id: str, name="260926 周会.md") -> None:
    cards = folder / "声档会议记录"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / name).write_text(f"---\nmeeting_id: {meeting_id}\n---\n\n# 周会\n", encoding="utf-8")


def _rename_setup(tmp_path):
    client, settings, headers, db, root = _setup(tmp_path)
    parent = root / "项目"
    old = parent / "云图_AI"
    _mkdirs(old, "需求A", "需求B", "需求C", "需求D", "嵌套项目")
    _mkdirs(parent, "云图_AI二期")
    project = _project(client, headers, "云图AI", material_roots=[str(old)])
    nested = _project(client, headers, "嵌套项目", material_roots=[str(old / "嵌套项目")])
    sibling = _project(client, headers, "云图二期", material_roots=[str(parent / "云图_AI二期")])
    added = client.post(
        "/api/requirements",
        json={
            "project_id": project["id"],
            "title": "需求A",
            "priority": "P1",
            "folder_paths": [str(old / "需求A")],
        },
        headers=headers,
    )
    assert added.status_code == 200, added.text
    db.execute(
        "INSERT INTO meetings(id, title, status, project_id) VALUES ('m-yt', '周会', 'published', ?)",
        (project["id"],),
    )
    return client, settings, headers, db, parent, old, project, nested, sibling


def _root_id(db, project_id):
    return db.query_one(
        "SELECT id FROM project_material_roots WHERE project_id=? ORDER BY id", (project_id,)
    )["id"]


def test_renamed_folder_is_found_by_its_cards_and_repointed(tmp_path):
    client, _settings, headers, db, parent, old, project, nested, sibling = _rename_setup(tmp_path)
    root_id = _root_id(db, project["id"])
    new = parent / "云图_AI-2026"
    os.rename(old, new)
    _card(new, "m-yt")
    _mkdirs(parent, "不相干")
    _refresh(client)

    found = client.get(
        f"/api/projects/{project['id']}/material-roots/{root_id}/rename-candidates"
    ).json()
    assert found["state"] == "ready"
    assert [item["name"] for item in found["candidates"]] == ["云图_AI-2026"]
    assert found["candidates"][0]["evidence"]["kind"] == "cards"
    assert found["default_path"] == str(new)
    # 有强证据的候选不算「还没挂的文件夹」
    client.put("/api/settings/project-parent", json={"path": str(parent)}, headers=headers)
    _refresh(client)
    unclaimed = [row["name"] for row in _parent(client)["unclaimed"]["folders"]]
    assert "云图_AI-2026" not in unclaimed and "不相干" in unclaimed

    db.execute(
        """INSERT INTO meeting_cards(meeting_id, project_id, root_path, rel_path, state, dirty,
                                     created_at, updated_at)
           VALUES ('m-yt', ?, ?, '声档会议记录/260926 周会.md', 'synced', 0, 'x', 'x')""",
        (project["id"], str(old.resolve())),
    )
    cards_before = db.query_all("SELECT meeting_id, root_path, rel_path FROM meeting_cards")
    result = client.post(
        f"/api/projects/{project['id']}/material-roots/{root_id}/repoint",
        json={"path": str(new)},
        headers=headers,
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["path"] == str(new.resolve()) and body["id"] == root_id
    assert [item["project_name"] for item in body["moved_roots"]] == ["嵌套项目"]
    assert body["moved_folders"] == 1
    paths = {
        row["project_id"]: row["path"]
        for row in db.query_all("SELECT project_id, path FROM project_material_roots")
    }
    assert paths[nested["id"]] == str(new.resolve() / "嵌套项目")
    # 兄弟文件夹（名字带 _，前缀相同）不动
    assert paths[sibling["id"]] == str((parent / "云图_AI二期").resolve())
    folders = [row["path"] for row in db.query_all("SELECT path FROM requirement_folders")]
    assert folders == [str(new.resolve() / "需求A")]
    # 卡片表的路径不在这里改（卡片模块按会议 id 在新位置认回，认不到就重写）
    assert db.query_all("SELECT meeting_id, root_path, rel_path FROM meeting_cards") == cards_before


def test_renamed_folder_is_found_by_its_subfolders(tmp_path):
    client, _settings, headers, db, parent, old, project, _nested, _sibling = _rename_setup(
        tmp_path
    )
    root_id = _root_id(db, project["id"])
    _refresh(client)  # 在线时记下指纹
    fingerprint = json.loads(
        db.query_one("SELECT child_names FROM root_fingerprints WHERE root_id=?", (root_id,))[
            "child_names"
        ]
    )
    assert fingerprint == ["嵌套项目", "需求A", "需求B", "需求C", "需求D"]
    os.rename(old, parent / "云图资料2026")
    _refresh(client)

    found = client.get(
        f"/api/projects/{project['id']}/material-roots/{root_id}/rename-candidates"
    ).json()

    assert [item["name"] for item in found["candidates"]] == ["云图资料2026"]
    evidence = found["candidates"][0]["evidence"]
    assert (evidence["kind"], evidence["matched"], evidence["total"]) == ("children", 5, 5)
    assert evidence["text"] == "里面 5 个子文件夹有 5 个对得上"

    # 「不是」之后不再列
    client.post(
        f"/api/projects/{project['id']}/material-roots/{root_id}/rename-decline",
        json={"path": found["candidates"][0]["path"]},
        headers=headers,
    )
    again = client.get(
        f"/api/projects/{project['id']}/material-roots/{root_id}/rename-candidates"
    ).json()
    assert again["candidates"] == []


def test_equally_strong_candidates_have_no_default(tmp_path):
    client, _settings, _headers, db, parent, old, project, _nested, _sibling = _rename_setup(
        tmp_path
    )
    root_id = _root_id(db, project["id"])
    os.rename(old, parent / "云图甲")
    _card(parent / "云图甲", "m-yt")
    _card(parent / "云图乙", "m-yt")
    _refresh(client)

    found = client.get(
        f"/api/projects/{project['id']}/material-roots/{root_id}/rename-candidates"
    ).json()

    assert sorted(item["name"] for item in found["candidates"]) == ["云图乙", "云图甲"]
    assert found["default_path"] is None


def test_reselecting_an_online_root_only_moves_that_root(tmp_path):
    client, _settings, headers, db, parent, old, project, nested, _sibling = _rename_setup(tmp_path)
    root_id = _root_id(db, project["id"])
    other = parent / "另一个"
    other.mkdir()

    result = client.post(
        f"/api/projects/{project['id']}/material-roots/{root_id}/replace",
        json={"path": str(other)},
        headers=headers,
    ).json()

    assert result["path"] == str(other.resolve())
    assert result["moved_roots"] == [] and result["moved_folders"] == 0
    nested_path = db.query_one(
        "SELECT path FROM project_material_roots WHERE project_id=?", (nested["id"],)
    )["path"]
    assert nested_path == str((old / "嵌套项目").resolve())
