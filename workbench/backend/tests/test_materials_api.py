"""项目材料目录：路径安全、递归统计上限、材料根目录 CRUD（260915 新增）。"""
import os

from meeting_workbench.db import Database

from .test_tasks_api import make_client, write_headers


def make_material_client(tmp_path):
    browse_root = tmp_path / "browse-root"
    browse_root.mkdir()
    return make_client(tmp_path, material_browse_root=browse_root), browse_root


def test_browse_rejects_hidden_segment(tmp_path):
    """D27：路径任一段以 `.` 开头即拒绝（这里直接选中隐藏目录本身）。"""
    (client, settings), browse_root = make_material_client(tmp_path)
    hidden = browse_root / ".隐藏目录"
    hidden.mkdir()

    response = client.get("/api/materials/browse", params={"path": str(hidden)})
    assert response.status_code == 400
    assert "隐藏目录" in response.json()["detail"]


def test_browse_rejects_hidden_ancestor_segment(tmp_path):
    """D27：隐藏段不必是路径末段，中间任一段是隐藏目录也要拒绝。"""
    (client, settings), browse_root = make_material_client(tmp_path)
    nested = browse_root / ".隐藏父目录" / "子目录"
    nested.mkdir(parents=True)

    response = client.get("/api/materials/browse", params={"path": str(nested)})
    assert response.status_code == 400
    assert "隐藏目录" in response.json()["detail"]


def test_add_material_root_rejects_hidden_segment(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    hidden_root = browse_root / ".隐藏根目录"
    hidden_root.mkdir()
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "隐藏根测试", "color": "#111111"}, headers=headers
    ).json()["id"]

    response = client.post(
        f"/api/projects/{project_id}/material-roots",
        json={"path": str(hidden_root)},
        headers=headers,
    )
    assert response.status_code == 400
    assert "隐藏目录" in response.json()["detail"]


def test_create_project_duplicate_name_conflicts_409(tmp_path):
    """D26：HTTP 手动建项目撞名要报 409，不能悄悄把 material_roots 挂到既有项目上。"""
    (client, settings), browse_root = make_material_client(tmp_path)
    headers = write_headers(client)
    client.post("/api/projects", json={"name": "重复项目名", "color": "#111111"}, headers=headers)

    duplicate = client.post(
        "/api/projects",
        json={"name": "重复项目名", "color": "#222222", "material_roots": [str(browse_root)]},
        headers=headers,
    )
    assert duplicate.status_code == 409

    existing = next(
        p for p in client.get("/api/projects").json() if p["name"] == "重复项目名"
    )
    assert existing["material_roots"] == []


def test_update_project_rename_to_duplicate_name_conflicts_409_not_500(tmp_path):
    """D26：改名撞名要报 409，不许 500。"""
    (client, settings), browse_root = make_material_client(tmp_path)
    headers = write_headers(client)
    client.post("/api/projects", json={"name": "项目甲", "color": "#111111"}, headers=headers)
    project_b = client.post(
        "/api/projects", json={"name": "项目乙", "color": "#222222"}, headers=headers
    ).json()["id"]

    response = client.patch(f"/api/projects/{project_b}", json={"name": "项目甲"}, headers=headers)
    assert response.status_code == 409

    unchanged = client.get("/api/projects").json()
    assert any(p["id"] == project_b and p["name"] == "项目乙" for p in unchanged)


def test_create_project_ai_origin_idempotent_merge_unchanged(tmp_path):
    """D26 附带约束：TaskService 内部 AI 建项目（origin=ai）幂等合并语义不受影响——
    撞名不报错、直接合并到既有项目并把 origin 升级为 ai；只有 HTTP 手动建项目
    （origin=manual）路径改成报 409。合并升级本身落库生效（用后续 GET 核实），
    这里不去动 create_project 既有实现，按团队交代原样保留。"""
    (client, settings), browse_root = make_material_client(tmp_path)
    headers = write_headers(client)
    manual_id = client.post(
        "/api/projects", json={"name": "AI合并测试", "color": "#111111"}, headers=headers
    ).json()["id"]

    from meeting_workbench.tasks import TaskService

    merged = TaskService(Database(settings.database_path), settings).create_project(
        name="AI合并测试", color="#333333", origin="ai"
    )
    assert merged["id"] == manual_id

    refetched = next(p for p in client.get("/api/projects").json() if p["id"] == manual_id)
    assert refetched["origin"] == "ai"


def test_browse_rejects_dotdot_traversal(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    (browse_root / "项目A").mkdir()

    response = client.get(
        "/api/materials/browse", params={"path": str(browse_root / "项目A" / ".." / "..")}
    )
    assert response.status_code == 400


def test_browse_rejects_symlink_escaping_root(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = browse_root / "指向外部"
    os.symlink(outside, link)

    response = client.get("/api/materials/browse", params={"path": str(link)})
    assert response.status_code == 400


def test_browse_works_when_browse_root_itself_is_symlink(tmp_path):
    """ADV-M-06 回归：浏览根本身是符号链接时浏览正常（base/path/breadcrumbs 口径一致，
    不再因为「未解析的 base」与「已解析的 target」前缀对不上抛原始 Python 异常），
    越界判定仍按 realpath 生效。"""
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    (real_target / "子目录").mkdir()
    browse_root = tmp_path / "browse-root-symlink"
    os.symlink(real_target, browse_root)
    client, settings = make_client(tmp_path, material_browse_root=browse_root)

    root_payload = client.get("/api/materials/browse").json()
    assert [d["name"] for d in root_payload["dirs"]] == ["子目录"]
    assert root_payload["parent"] is None
    real_target_resolved = str(real_target.resolve())
    assert root_payload["base"] == root_payload["path"] == real_target_resolved
    assert root_payload["breadcrumbs"] == [
        {"name": real_target.resolve().name, "path": real_target_resolved}
    ]

    sub_payload = client.get(
        "/api/materials/browse", params={"path": str(real_target / "子目录")}
    ).json()
    assert sub_payload["parent"] == real_target_resolved
    assert [c["name"] for c in sub_payload["breadcrumbs"]] == [
        real_target.resolve().name, "子目录",
    ]

    outside = tmp_path / "outside"
    outside.mkdir()
    rejected = client.get("/api/materials/browse", params={"path": str(outside)})
    assert rejected.status_code == 400


def test_browse_rejects_path_outside_browse_root(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    other = tmp_path / "别的地方"
    other.mkdir()

    response = client.get("/api/materials/browse", params={"path": str(other)})
    assert response.status_code == 400


def test_browse_rejects_relative_path(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    response = client.get("/api/materials/browse", params={"path": "相对路径"})
    assert response.status_code == 400


def test_browse_non_directory_is_not_found(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    file_path = browse_root / "不是文件夹.txt"
    file_path.write_text("x", encoding="utf-8")

    response = client.get("/api/materials/browse", params={"path": str(file_path)})
    assert response.status_code == 404


def test_browse_skips_hidden_and_blacklisted_dirs(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    (browse_root / ".隐藏目录").mkdir()
    (browse_root / "node_modules").mkdir()
    (browse_root / "__MACOSX").mkdir()
    (browse_root / "正常目录").mkdir()

    payload = client.get("/api/materials/browse", params={"path": str(browse_root)}).json()

    assert [item["name"] for item in payload["dirs"]] == ["正常目录"]
    assert payload["base"] == str(browse_root.resolve())
    assert payload["parent"] is None


def test_browse_breadcrumbs_and_parent(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    nested = browse_root / "一级" / "二级"
    nested.mkdir(parents=True)

    payload = client.get("/api/materials/browse", params={"path": str(nested)}).json()

    assert payload["parent"] == str(nested.parent)
    assert [item["name"] for item in payload["breadcrumbs"]][-2:] == ["一级", "二级"]


def test_folder_stat_caps_at_limit_and_skips_hidden(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    project_id = client.post(
        "/api/projects",
        json={"name": "样品项目", "color": "#2c8d83", "material_roots": [str(browse_root)]},
        headers=write_headers(client),
    ).json()["id"]
    target = browse_root / "材料文件夹"
    target.mkdir()
    from meeting_workbench.materials import MAX_FOLDER_FILES

    for index in range(MAX_FOLDER_FILES + 5):
        (target / f"file-{index}.txt").write_text("x", encoding="utf-8")
    # AppleDouble 影子文件与黑名单目录不计入统计
    (target / "._shadow.txt").write_text("x", encoding="utf-8")
    (target / "node_modules").mkdir()
    (target / "node_modules" / "a.txt").write_text("x", encoding="utf-8")

    payload = client.get(f"/api/projects/{project_id}/material-subfolders").json()
    folder = next(f for f in payload["roots"][0]["folders"] if f["name"] == "材料文件夹")

    assert folder["file_count"] == MAX_FOLDER_FILES
    assert folder["file_count_capped"] is True


def test_material_root_add_remove_and_duplicate_conflict(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    headers = write_headers(client)
    sub = browse_root / "材料根目录"
    sub.mkdir()
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]

    added = client.post(
        f"/api/projects/{project_id}/material-roots", json={"path": str(sub)}, headers=headers
    )
    assert added.status_code == 200
    root_id = added.json()["id"]

    duplicate = client.post(
        f"/api/projects/{project_id}/material-roots", json={"path": str(sub)}, headers=headers
    )
    assert duplicate.status_code == 409

    outside = tmp_path / "outside-root"
    outside.mkdir()
    rejected = client.post(
        f"/api/projects/{project_id}/material-roots", json={"path": str(outside)}, headers=headers
    )
    assert rejected.status_code == 400

    removed = client.delete(
        f"/api/projects/{project_id}/material-roots/{root_id}",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert removed.status_code == 200
    assert client.get(f"/api/projects/{project_id}/board").json()["material_roots"] == []


def test_project_material_roots_included_in_list_and_detail(tmp_path):
    (client, settings), browse_root = make_material_client(tmp_path)
    headers = write_headers(client)
    sub = browse_root / "根目录"
    sub.mkdir()
    project_id = client.post(
        "/api/projects",
        json={"name": "云图科研用药", "color": "#2c8d83", "material_roots": [str(sub)]},
        headers=headers,
    ).json()["id"]

    listed = next(p for p in client.get("/api/projects").json() if p["id"] == project_id)
    assert listed["material_roots"][0]["path"] == str(sub.resolve())
    assert listed["material_roots"][0]["exists"] is True
    assert listed["requirement_counts"] == {"active": 0, "done": 0, "shelved": 0, "all": 0}
    assert listed["open_task_count"] == 0

    board = client.get(f"/api/projects/{project_id}/board").json()
    assert board["material_roots"][0]["path"] == str(sub.resolve())
