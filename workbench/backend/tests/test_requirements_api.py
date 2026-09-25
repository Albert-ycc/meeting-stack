"""需求层：CRUD、D6/D7 任务-项目不变量、D8 留痕、D11 重名冲突（260915 新增）。"""
from meeting_workbench.db import Database, utc_now

from .helpers import seed_editable_meeting
from .test_tasks_api import make_client, write_headers


def delete_headers(client):
    return {**write_headers(client), "Content-Type": "application/json"}


def make_requirement_client(tmp_path):
    browse_root = tmp_path / "browse-root"
    browse_root.mkdir()
    client, settings = make_client(tmp_path, material_browse_root=browse_root)
    return client, settings, browse_root


def make_project_with_root(client, headers, browse_root, name="云图科研用药"):
    root = browse_root / name
    root.mkdir()
    project_id = client.post(
        "/api/projects",
        json={"name": name, "color": "#2c8d83", "material_roots": [str(root)]},
        headers=headers,
    ).json()["id"]
    return project_id, root


def test_create_requirement_with_folder_and_get_detail(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, root = make_project_with_root(client, headers, browse_root)
    folder = root / "材料文件夹"
    folder.mkdir()
    (folder / "接口字段事实.md").write_text("x", encoding="utf-8")

    created = client.post(
        "/api/requirements",
        json={
            "project_id": project_id, "title": "北辰仓快递配送", "priority": "P1",
            "folder_paths": [str(folder)],
        },
        headers=headers,
    )
    assert created.status_code == 200
    detail = created.json()
    assert detail["status"] == "active"
    assert detail["project_name"] == "云图科研用药"
    assert detail["folders"][0]["name"] == "材料文件夹"
    assert detail["folders"][0]["exists"] is True
    assert detail["folders"][0]["file_count"] == 1
    assert detail["folders"][0]["preview_files"][0]["relative_path"] == "接口字段事实.md"
    assert detail["meetings"] == []
    assert detail["tasks"] == []
    assert detail["open_task_count"] == 0
    assert detail["folder_count"] == 1

    fetched = client.get(f"/api/requirements/{detail['id']}").json()
    assert fetched == detail


def test_create_requirement_rejects_folder_not_direct_child_of_root(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, root = make_project_with_root(client, headers, browse_root)
    nested = root / "一级" / "二级"
    nested.mkdir(parents=True)

    response = client.post(
        "/api/requirements",
        json={
            "project_id": project_id, "title": "不合规文件夹", "priority": "P2",
            "folder_paths": [str(nested)],
        },
        headers=headers,
    )
    assert response.status_code == 400


def test_create_requirement_rejects_folder_outside_any_root(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, root = make_project_with_root(client, headers, browse_root)
    elsewhere = browse_root / "不属于任何根目录"
    elsewhere.mkdir()

    response = client.post(
        "/api/requirements",
        json={
            "project_id": project_id, "title": "越界文件夹", "priority": "P2",
            "folder_paths": [str(elsewhere)],
        },
        headers=headers,
    )
    assert response.status_code == 400


def test_duplicate_title_in_same_project_conflicts_409(tmp_path):
    """D11：同一项目下需求名忽略大小写、去首尾空格后唯一。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "发药日历", "priority": "P1"},
        headers=headers,
    )

    duplicate = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "  发药日历  ", "priority": "P2"},
        headers=headers,
    )
    assert duplicate.status_code == 409

    other_project_id, _ = make_project_with_root(client, headers, browse_root, name="ACME")
    elsewhere_ok = client.post(
        "/api/requirements",
        json={"project_id": other_project_id, "title": "发药日历", "priority": "P1"},
        headers=headers,
    )
    assert elsewhere_ok.status_code == 200


def test_update_requirement_rename_and_priority_status(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "旧标题", "priority": "P3"},
        headers=headers,
    ).json()["id"]

    updated = client.patch(
        f"/api/requirements/{requirement_id}",
        json={"title": "新标题", "priority": "P0", "status": "shelved"},
        headers=headers,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["title"] == "新标题"
    assert body["priority"] == "P0"
    assert body["status"] == "shelved"


def test_change_project_without_folder_paths_returns_400(tmp_path):
    """D6：换了所属项目而请求没带 folder_paths → 400。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_a, _root_a = make_project_with_root(client, headers, browse_root, name="项目A")
    project_b, _root_b = make_project_with_root(client, headers, browse_root, name="项目B")
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_a, "title": "需求X", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    response = client.patch(
        f"/api/requirements/{requirement_id}",
        json={"project_id": project_b},
        headers=headers,
    )
    assert response.status_code == 400
    assert "材料文件夹" in response.json()["detail"]


def test_change_project_cascades_task_project_in_same_transaction(tmp_path):
    """D7：需求换项目，名下任务的项目跟着改。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_a, root_a = make_project_with_root(client, headers, browse_root, name="项目A")
    project_b, root_b = make_project_with_root(client, headers, browse_root, name="项目B")
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_a, "title": "需求Y", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    task_id = client.post(
        "/api/tasks",
        json={"title": "挂需求的任务", "requirement_id": requirement_id},
        headers=headers,
    ).json()["id"]
    assert client.get(f"/api/tasks/{task_id}").json()["project_id"] == project_a

    folder_b = root_b / "换项目后的文件夹"
    folder_b.mkdir()
    updated = client.patch(
        f"/api/requirements/{requirement_id}",
        json={"project_id": project_b, "folder_paths": [str(folder_b)]},
        headers=headers,
    )
    assert updated.status_code == 200

    task_after = client.get(f"/api/tasks/{task_id}").json()
    assert task_after["project_id"] == project_b
    assert task_after["requirement_id"] == requirement_id


def test_task_confirm_requirement_and_conflicting_project_400(tmp_path):
    """D7：请求里同时给了不一致的 project_id → 400。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_a, _ = make_project_with_root(client, headers, browse_root, name="项目A")
    project_b, _ = make_project_with_root(client, headers, browse_root, name="项目B")
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_a, "title": "需求Z", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    response = client.post(
        "/api/tasks",
        json={"title": "冲突任务", "requirement_id": requirement_id, "project_id": project_b},
        headers=headers,
    )
    assert response.status_code == 400


def test_task_create_with_requirement_inherits_project_and_writes_event(tmp_path):
    """D7/D8：创建任务时给了 requirement_id，任务项目自动变成需求项目并留痕。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "北辰仓快递配送", "priority": "P1"},
        headers=headers,
    ).json()

    created = client.post(
        "/api/tasks",
        json={"title": "新建任务", "requirement_id": requirement["id"]},
        headers=headers,
    ).json()
    assert created["project_id"] == project_id
    assert created["requirement_id"] == requirement["id"]
    assert any(
        event["kind"] == "requirement_changed" and "挂到需求「北辰仓快递配送」" in event["body"]
        for event in created["events"]
    )


def test_task_update_requirement_id_null_detaches_and_writes_event(tmp_path):
    """D7/D8：PATCH 任务 requirement_id 传 null → 移出需求，留痕。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求W", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    task_id = client.post(
        "/api/tasks",
        json={"title": "待移出的任务", "requirement_id": requirement_id},
        headers=headers,
    ).json()["id"]

    updated = client.patch(
        f"/api/tasks/{task_id}", json={"requirement_id": None}, headers=headers
    ).json()
    assert updated["requirement_id"] is None
    assert updated["project_id"] == project_id  # 项目不变，只是移出需求
    assert any(
        event["kind"] == "requirement_changed" and "移出需求「需求W」" in event["body"]
        for event in updated["events"]
    )


def test_task_update_missing_requirement_id_field_does_not_change_it(tmp_path):
    """D21 对照：不带 requirement_id 字段（如 card_listener 的 confirm {}）时不改需求。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求不变", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    task_id = client.post(
        "/api/tasks",
        json={"title": "任务", "requirement_id": requirement_id},
        headers=headers,
    ).json()["id"]

    updated = client.patch(f"/api/tasks/{task_id}", json={"title": "改了标题"}, headers=headers).json()
    assert updated["requirement_id"] == requirement_id
    assert updated["title"] == "改了标题"


def test_task_project_change_conflicting_with_requirement_auto_detaches(tmp_path):
    """D7：只改任务项目、没带 requirement_id，而原需求属于别的项目 → 自动移出需求并留痕。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_a, _ = make_project_with_root(client, headers, browse_root, name="项目A")
    project_b, _ = make_project_with_root(client, headers, browse_root, name="项目B")
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_a, "title": "需求V", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    task_id = client.post(
        "/api/tasks",
        json={"title": "任务", "requirement_id": requirement_id},
        headers=headers,
    ).json()["id"]

    updated = client.patch(
        f"/api/tasks/{task_id}", json={"project_id": project_b}, headers=headers
    ).json()
    assert updated["project_id"] == project_b
    assert updated["requirement_id"] is None
    assert any(
        event["kind"] == "requirement_changed" and "移出需求「需求V」" in event["body"]
        for event in updated["events"]
    )


def test_task_project_change_to_same_requirement_project_keeps_link(tmp_path):
    """反例对照：改项目但改成的项目恰好等于需求项目，不应移出需求。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求U", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    task_id = client.post(
        "/api/tasks",
        json={"title": "任务", "requirement_id": requirement_id},
        headers=headers,
    ).json()["id"]

    updated = client.patch(
        f"/api/tasks/{task_id}", json={"project_id": project_id}, headers=headers
    ).json()
    assert updated["requirement_id"] == requirement_id


def test_attach_existing_tasks_forces_project_and_writes_event(tmp_path):
    """D7/D8：/api/requirements/{id}/tasks 挂已有任务，任务项目跟着改并留痕。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求T", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    loose_task_id = client.post("/api/tasks", json={"title": "游离任务"}, headers=headers).json()["id"]

    attached = client.post(
        f"/api/requirements/{requirement_id}/tasks",
        json={"task_ids": [loose_task_id]},
        headers=headers,
    )
    assert attached.status_code == 200
    detail = attached.json()
    assert any(task["id"] == loose_task_id for task in detail["tasks"])

    task_detail = client.get(f"/api/tasks/{loose_task_id}").json()
    assert task_detail["project_id"] == project_id
    assert task_detail["requirement_id"] == requirement_id
    assert any(
        event["kind"] == "requirement_changed" and "挂到需求「需求T」" in event["body"]
        for event in task_detail["events"]
    )


def test_attach_unknown_task_id_404(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求S", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    response = client.post(
        f"/api/requirements/{requirement_id}/tasks",
        json={"task_ids": ["task-not-exist"]},
        headers=headers,
    )
    assert response.status_code == 404


def test_remove_folder_and_not_found(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, root = make_project_with_root(client, headers, browse_root)
    folder = root / "待移除文件夹"
    folder.mkdir()
    requirement = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求R", "priority": "P1", "folder_paths": [str(folder)]},
        headers=headers,
    ).json()
    folder_id = requirement["folders"][0]["id"]

    removed = client.delete(
        f"/api/requirements/{requirement['id']}/folders/{folder_id}", headers=delete_headers(client)
    )
    assert removed.status_code == 200
    assert removed.json()["folders"] == []

    missing = client.delete(
        f"/api/requirements/{requirement['id']}/folders/{folder_id}", headers=delete_headers(client)
    )
    assert missing.status_code == 404


def test_folder_files_listing_sorted_and_paginated(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, root = make_project_with_root(client, headers, browse_root)
    folder = root / "文件清单"
    folder.mkdir()
    for name in ("b.md", "a.md", "c.md"):
        (folder / name).write_text("x", encoding="utf-8")
    requirement = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求Q", "priority": "P1", "folder_paths": [str(folder)]},
        headers=headers,
    ).json()
    folder_id = requirement["folders"][0]["id"]

    payload = client.get(
        f"/api/requirements/{requirement['id']}/folders/{folder_id}/files",
        params={"limit": 2, "offset": 0},
    ).json()
    assert payload["total"] == 3
    assert [item["relative_path"] for item in payload["items"]] == ["a.md", "b.md"]


def test_meetings_association_via_requirement_and_meeting_endpoints(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求P", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    linked = client.put(
        f"/api/requirements/{requirement_id}/meetings",
        json={"meeting_ids": ["vm-20260102-101500"]},
        headers=headers,
    )
    assert linked.status_code == 200
    assert linked.json()["meetings"][0]["id"] == "vm-20260102-101500"

    meeting_detail = client.get("/api/meetings/vm-20260102-101500").json()
    assert meeting_detail["requirements"][0]["id"] == requirement_id

    removed = client.delete(
        f"/api/requirements/{requirement_id}/meetings/vm-20260102-101500",
        headers=delete_headers(client),
    )
    assert removed.json()["meetings"] == []


def test_patch_meeting_requirement_ids_replaces_association(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求O", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    patched = client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"requirement_ids": [requirement_id]},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["requirements"][0]["id"] == requirement_id

    cleared = client.patch(
        "/api/meetings/vm-20260102-101500", json={"requirement_ids": []}, headers=headers
    )
    assert cleared.json()["requirements"] == []


def test_patch_meeting_requirement_ids_unknown_requirement_404(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)

    response = client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"requirement_ids": ["requirement-not-exist"]},
        headers=headers,
    )
    assert response.status_code == 404


def test_list_requirements_sorted_by_priority_then_latest_meeting_then_created(tmp_path):
    """D2：优先级 P0→P3，同级按最近关联会议日期倒序（无会议排最后），再按创建时间倒序。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    db = Database(settings.database_path)
    for meeting_id, recording_date in (
        ("vm-old", "2026-01-01T00:00:00+00:00"),
        ("vm-new", "2026-06-01T00:00:00+00:00"),
    ):
        db.execute(
            """INSERT INTO meetings(id, title, recording_date, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (meeting_id, meeting_id, recording_date, utc_now(), utc_now()),
        )

    r_p1_no_meeting = client.post(
        "/api/requirements", json={"project_id": project_id, "title": "P1无会议", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    r_p1_old_meeting = client.post(
        "/api/requirements", json={"project_id": project_id, "title": "P1旧会议", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    r_p1_new_meeting = client.post(
        "/api/requirements", json={"project_id": project_id, "title": "P1新会议", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    r_p0 = client.post(
        "/api/requirements", json={"project_id": project_id, "title": "P0需求", "priority": "P0"},
        headers=headers,
    ).json()["id"]
    client.put(
        f"/api/requirements/{r_p1_old_meeting}/meetings",
        json={"meeting_ids": ["vm-old"]}, headers=headers,
    )
    client.put(
        f"/api/requirements/{r_p1_new_meeting}/meetings",
        json={"meeting_ids": ["vm-new"]}, headers=headers,
    )

    items = client.get("/api/requirements", params={"project_id": project_id, "limit": 50}).json()["items"]
    ids = [item["id"] for item in items]
    assert ids == [r_p0, r_p1_new_meeting, r_p1_old_meeting, r_p1_no_meeting]


def test_list_requirements_counts_ignore_status_filter(tmp_path):
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    active_id = client.post(
        "/api/requirements", json={"project_id": project_id, "title": "进行中", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    shelved_id = client.post(
        "/api/requirements", json={"project_id": project_id, "title": "已搁置", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    client.patch(f"/api/requirements/{shelved_id}", json={"status": "shelved"}, headers=headers)

    payload = client.get(
        "/api/requirements", params={"project_id": project_id, "status": "active"}
    ).json()
    assert [item["id"] for item in payload["items"]] == [active_id]
    assert payload["counts"] == {"active": 1, "done": 0, "shelved": 1, "all": 2}


def test_create_requirement_rejects_hidden_folder_segment(tmp_path):
    """D27：文件夹路径任一段以 `.` 开头即拒绝，三个入口共用同一判定函数。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, root = make_project_with_root(client, headers, browse_root)
    hidden_folder = root / ".隐藏材料"
    hidden_folder.mkdir()

    response = client.post(
        "/api/requirements",
        json={
            "project_id": project_id, "title": "隐藏文件夹需求", "priority": "P1",
            "folder_paths": [str(hidden_folder)],
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert "隐藏目录" in response.json()["detail"]


def test_attach_tasks_dedupes_duplicate_ids(tmp_path):
    """D24：批量 id 先去重，重复传同一个 id 不应报错也不应重复写事件。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求Dup", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    task_id = client.post("/api/tasks", json={"title": "任务"}, headers=headers).json()["id"]

    attached = client.post(
        f"/api/requirements/{requirement_id}/tasks",
        json={"task_ids": [task_id, task_id]},
        headers=headers,
    )
    assert attached.status_code == 200
    assert [t["id"] for t in attached.json()["tasks"]] == [task_id]
    events = [
        event for event in client.get(f"/api/tasks/{task_id}").json()["events"]
        if event["kind"] == "requirement_changed"
    ]
    assert len(events) == 1


def test_attach_tasks_missing_id_rejects_all_and_writes_nothing(tmp_path):
    """D24：任一 id 不存在 → 整批 404，事务内一条都不写（先验证好的那条也回滚）。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求Miss", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    real_task_id = client.post("/api/tasks", json={"title": "真实任务"}, headers=headers).json()["id"]

    response = client.post(
        f"/api/requirements/{requirement_id}/tasks",
        json={"task_ids": [real_task_id, "task-not-exist"]},
        headers=headers,
    )
    assert response.status_code == 404

    real_task_after = client.get(f"/api/tasks/{real_task_id}").json()
    assert real_task_after["requirement_id"] is None


def test_set_meetings_dedupes_duplicate_ids(tmp_path):
    """D24：同一 meeting_id 传两次不该撞 requirement_meetings 主键。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("vm-dup", "会议", utc_now(), utc_now()),
    )
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求DupMeeting", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    linked = client.put(
        f"/api/requirements/{requirement_id}/meetings",
        json={"meeting_ids": ["vm-dup", "vm-dup"]},
        headers=headers,
    )
    assert linked.status_code == 200
    assert [m["id"] for m in linked.json()["meetings"]] == ["vm-dup"]


def test_set_meetings_missing_id_rejects_all_and_keeps_previous_association(tmp_path):
    """D24：任一 id 不存在 → 整批 404，已有关联不会被清空（事务回滚）。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("vm-real", "会议", utc_now(), utc_now()),
    )
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求MissMeeting", "priority": "P1"},
        headers=headers,
    ).json()["id"]
    client.put(
        f"/api/requirements/{requirement_id}/meetings",
        json={"meeting_ids": ["vm-real"]},
        headers=headers,
    )

    response = client.put(
        f"/api/requirements/{requirement_id}/meetings",
        json={"meeting_ids": ["vm-real", "vm-not-exist"]},
        headers=headers,
    )
    assert response.status_code == 404

    detail = client.get(f"/api/requirements/{requirement_id}").json()
    assert [m["id"] for m in detail["meetings"]] == ["vm-real"]


def test_patch_meeting_requirement_ids_dedupes_and_rejects_all_on_missing_id(tmp_path):
    """D24：PATCH 会议的 requirement_ids 同样先去重、任一不存在整批 404。"""
    client, settings, browse_root = make_requirement_client(tmp_path)
    headers = write_headers(client)
    project_id, _root = make_project_with_root(client, headers, browse_root)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    requirement_id = client.post(
        "/api/requirements",
        json={"project_id": project_id, "title": "需求MissPatch", "priority": "P1"},
        headers=headers,
    ).json()["id"]

    dup = client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"requirement_ids": [requirement_id, requirement_id]},
        headers=headers,
    )
    assert dup.status_code == 200
    assert [r["id"] for r in dup.json()["requirements"]] == [requirement_id]

    missing = client.patch(
        "/api/meetings/vm-20260102-101500",
        json={"requirement_ids": [requirement_id, "requirement-not-exist"]},
        headers=headers,
    )
    assert missing.status_code == 404
    detail = client.get("/api/meetings/vm-20260102-101500").json()
    assert [r["id"] for r in detail["requirements"]] == [requirement_id]
