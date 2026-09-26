"""材料根目录（v13 / 第一期 1a）：按差异增删、原子替换、三态、挂载前校验。"""
from pathlib import Path

from meeting_workbench import materials
from meeting_workbench.db import Database, utc_now

from .test_tasks_api import make_client, write_headers


def _client(tmp_path, browse_root=None):
    browse_root = browse_root or (tmp_path / "browse-root")
    browse_root.mkdir(exist_ok=True)
    client, settings = make_client(tmp_path, material_browse_root=browse_root)
    return client, settings, write_headers(client), browse_root


def _project(client, headers, name, roots=None):
    body = {"name": name}
    if roots is not None:
        body["material_roots"] = [str(root) for root in roots]
    response = client.post("/api/projects", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _roots(client, project_id):
    project = next(p for p in client.get("/api/projects").json() if p["id"] == project_id)
    return project["material_roots"]


def test_volume_state_distinguishes_offline_disk_from_missing_folder(tmp_path):
    folder = tmp_path / "云图AI"
    folder.mkdir()

    assert materials.volume_state(folder) == "online"
    assert materials.volume_state(tmp_path / "不存在") == "missing"
    # 沙箱里没有 /Volumes/<盘> 这个真实挂载点，等同于盘没插
    assert materials.volume_state("/Volumes/资料盘/项目/云图AI") == "volume_offline"


def test_updating_roots_keeps_unchanged_rows_and_their_ids(tmp_path):
    client, _settings, headers, browse_root = _client(tmp_path)
    keep = browse_root / "云图AI"
    drop = browse_root / "旧资料"
    add = browse_root / "新资料"
    for folder in (keep, drop, add):
        folder.mkdir()
    project = _project(client, headers, "云图AI", roots=[keep, drop])
    before = {root["path"]: root for root in project["material_roots"]}

    updated = client.patch(
        f"/api/projects/{project['id']}",
        json={"material_roots": [str(keep), str(add)]},
        headers=headers,
    ).json()

    after = {root["path"]: root for root in updated["material_roots"]}
    assert set(after) == {str(keep.resolve()), str(add.resolve())}
    kept_before = before[str(keep.resolve())]
    kept_after = after[str(keep.resolve())]
    assert (kept_after["id"], kept_after["created_at"]) == (
        kept_before["id"],
        kept_before["created_at"],
    )
    assert all(root["state"] == "online" and root["exists"] for root in after.values())


def test_renaming_project_works_while_its_disk_is_unplugged(tmp_path):
    client, settings, headers, _browse_root = _client(tmp_path)
    project = _project(client, headers, "智慧园区")
    offline_path = "/Volumes/资料盘/项目/智慧园区"
    Database(settings.database_path).execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project["id"], offline_path, utc_now()),
    )

    response = client.patch(
        f"/api/projects/{project['id']}",
        json={"name": "智慧园区二期", "color": "#123456", "material_roots": [offline_path]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    [root] = response.json()["material_roots"]
    assert root["path"] == offline_path
    assert root["state"] == "volume_offline"
    assert root["exists"] is False
    assert not Path(offline_path).exists()


def test_atomic_replace_keeps_root_id(tmp_path):
    client, _settings, headers, browse_root = _client(tmp_path)
    old = browse_root / "云图AI"
    new = browse_root / "云图AI（改名后）"
    old.mkdir()
    project = _project(client, headers, "云图AI", roots=[old])
    [root] = project["material_roots"]
    old.rename(new)
    assert _roots(client, project["id"])[0]["state"] == "missing"

    response = client.post(
        f"/api/projects/{project['id']}/material-roots/{root['id']}/replace",
        json={"path": str(new)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    replaced = response.json()
    assert (replaced["id"], replaced["created_at"]) == (root["id"], root["created_at"])
    assert replaced["path"] == str(new.resolve())
    assert replaced["state"] == "online"


def test_atomic_replace_failure_keeps_the_old_root(tmp_path):
    client, _settings, headers, browse_root = _client(tmp_path)
    old = browse_root / "云图AI"
    old.mkdir()
    project = _project(client, headers, "云图AI", roots=[old])
    [root] = project["material_roots"]

    response = client.post(
        f"/api/projects/{project['id']}/material-roots/{root['id']}/replace",
        json={"path": str(browse_root / "不存在")},
        headers=headers,
    )

    assert response.status_code == 400
    assert [r["id"] for r in _roots(client, project["id"])] == [root["id"]]


def test_mounting_inside_the_archive_is_rejected(tmp_path):
    client, settings, headers, _browse_root = _client(tmp_path, browse_root=tmp_path)
    inside_archive = settings.archive_root / "260926 周会"
    inside_archive.mkdir(parents=True)
    inside_staging = settings.staging_root / "job-1"
    inside_staging.mkdir(parents=True)
    project = _project(client, headers, "云图AI")

    for folder, message in (
        (settings.archive_root, "会议归档目录"),
        (inside_archive, "会议归档目录"),
        (inside_staging, "转写暂存目录"),
    ):
        response = client.post(
            f"/api/projects/{project['id']}/material-roots",
            json={"path": str(folder)},
            headers=headers,
        )
        assert response.status_code == 400, folder
        assert message in response.json()["detail"]


def test_same_folder_on_another_project_is_rejected_with_its_name(tmp_path):
    client, _settings, headers, browse_root = _client(tmp_path)
    folder = browse_root / "云图AI"
    folder.mkdir()
    _project(client, headers, "云图AI", roots=[folder])
    other = _project(client, headers, "数据中台")

    response = client.post(
        f"/api/projects/{other['id']}/material-roots",
        json={"path": str(folder)},
        headers=headers,
    )

    assert response.status_code == 409
    assert "云图AI" in response.json()["detail"]


def test_nested_mount_is_allowed_with_a_hint(tmp_path):
    client, _settings, headers, browse_root = _client(tmp_path)
    parent = browse_root / "客户A"
    child = parent / "云图科研用药"
    child.mkdir(parents=True)
    owner = _project(client, headers, "云图科研用药", roots=[child])
    project = _project(client, headers, "客户A总")

    response = client.post(
        f"/api/projects/{project['id']}/material-roots",
        json={"path": str(parent)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["nested"] == [
        {
            "path": str(child.resolve()),
            "project_id": owner["id"],
            "project_name": "云图科研用药",
        }
    ]


def test_cards_folder_is_not_project_material(tmp_path):
    client, _settings, headers, browse_root = _client(tmp_path)
    root = browse_root / "云图AI"
    (root / "声档会议记录").mkdir(parents=True)
    (root / "需求A").mkdir()
    project = _project(client, headers, "云图AI", roots=[root])

    subfolders = client.get(f"/api/projects/{project['id']}/material-subfolders").json()
    names = [folder["name"] for folder in subfolders["roots"][0]["folders"]]
    assert names == ["需求A"]

    response = client.post(
        "/api/requirements",
        json={
            "project_id": project["id"],
            "title": "登录改版",
            "priority": "P1",
            "folder_paths": [str(root / "声档会议记录")],
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "声档会议记录" in response.json()["detail"]
