"""第三期 3g：关系图里的文件（最近改过的文件、内容计数、文件面板、需求文件夹查库、交付物连到文件）。"""

from meeting_workbench import material_graph, material_status
from meeting_workbench.db import utc_now
from meeting_workbench.materials import ROOT_ONLINE, ROOT_VOLUME_OFFLINE

from .test_graph import add_meeting, add_project, add_task, make_db
from .test_graph_focus import local_client, make_root
from .test_material_search import add_content, add_file
from .test_tasks_api import write_headers


def add_folder(db, requirement_id, path, *, project_id="p"):
    if db.query_one("SELECT 1 FROM requirements WHERE id = ?", (requirement_id,)) is None:
        db.execute(
            """INSERT INTO requirements(id, project_id, title, priority, created_at, updated_at)
               VALUES (?, ?, ?, 'P1', ?, ?)""",
            (requirement_id, project_id, f"需求 {requirement_id}", utc_now(), utc_now()),
        )
    db.execute(
        "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, ?)",
        (requirement_id, str(path), utc_now()),
    )
    return db.query_one("SELECT id FROM requirement_folders WHERE path = ?", (str(path),))["id"]


def index_done(db, root_id, state="done"):
    db.execute(
        "INSERT OR REPLACE INTO material_index_state(root_id, state, updated_at) VALUES (?, ?, ?)",
        (root_id, state, utc_now()),
    )


def graph_world(tmp_path):
    client, settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root, root_id = make_root(db, tmp_path)
    return client, db, root, root_id


# ---------------------------------------------------------------------- 最近改过的文件、计数


def test_roots_have_recent_files_split_between_root_and_requirement_folders(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    (root / "白名单").mkdir()
    add_folder(db, "r1", root / "白名单")
    for index in range(8):
        add_file(db, root_id, f"方案/草稿{index}.docx", mtime=10 + index)
    add_file(db, root_id, "白名单/名单.xlsx", mtime=50)
    add_file(db, root_id, "白名单/说明.pdf", mtime=40, error="permission")
    add_file(db, root_id, "声档会议记录/0926.md", zone="cards", mtime=99)
    add_file(db, root_id, "旧的.docx", mtime=98, gone=True)
    add_content(db, "k-done", ["正文"])
    add_file(db, root_id, "方案/定稿.docx", key="k-done", mtime=60)
    add_file(db, root_id, "图.zip", mtime=55)
    client.app.state.material_content.progress["roots"] = {
        root_id: {"files": 13, "done": 1, "unreadable": 1}
    }

    body = client.get("/api/graph/projects/p/roots").json()

    [item] = body["roots"]
    # 根目录最多 6 个候选：不含声档会议记录、不含已不见的、不含需求文件夹里的
    assert [entry["name"] for entry in item["recent_files"]] == [
        "定稿.docx",
        "图.zip",
        "草稿7.docx",
        "草稿6.docx",
        "草稿5.docx",
        "草稿4.docx",
    ]
    first = item["recent_files"][0]
    assert first["dir_rel"] == "方案" and first["state"] == "done" and first["ext"] == "docx"
    assert first["mtime"].startswith("1970-01-01T00:01:00")
    assert item["recent_files"][1]["state"] == "names_only"
    assert item["recent_files"][2]["state"] == "pending"
    assert item["content"] == {"files": 13, "done": 1, "unreadable": 1}
    [folder] = body["folders"]
    assert folder["root_id"] == root_id
    assert [(entry["name"], entry["state"]) for entry in folder["recent_files"]] == [
        ("名单.xlsx", "pending"),
        ("说明.pdf", "unreadable"),
    ]


def test_root_content_is_null_until_the_content_loop_has_counted(tmp_path):
    client, db, _root, _root_id = graph_world(tmp_path)
    client.app.state.material_content.progress.pop("roots", None)

    [item] = client.get("/api/graph/projects/p/roots").json()["roots"]

    assert item["content"] is None and item["recent_files"] == []


def test_root_counts_match_coverage_buckets(tmp_path):
    _client, db, _root, root_id = graph_world(tmp_path)
    add_content(db, "k-done", ["正文"])
    add_content(db, "k-bad", [], state="unreadable", reason="password")
    add_content(db, "k-wait", [], layer="image", state="waiting")
    add_file(db, root_id, "a.docx", key="k-done")
    add_file(db, root_id, "b.pdf", key="k-bad")
    add_file(db, root_id, "c.png", key="k-wait")
    add_file(db, root_id, "d.docx", error="corrupt")
    add_file(db, root_id, "e.docx", error="io")
    add_file(db, root_id, "f.zip")
    add_file(db, root_id, "声档会议记录/x.md", zone="cards")
    add_file(db, root_id, "g.docx", key="k-done", gone=True)

    with db.autocommit() as connection:
        counts = material_status.root_counts(connection)

    assert counts == {root_id: {"files": 7, "done": 1, "unreadable": 2}}


def test_decorate_roots_gives_loose_files_ids_without_touching_the_cache(tmp_path):
    _client, db, root, root_id = graph_world(tmp_path)
    loose_id = add_file(db, root_id, "报价.xlsx")
    cached = [
        {"name": "报价.xlsx", "path": str(root / "报价.xlsx"), "size": 1, "mtime": "2026"},
        {"name": "新放的.txt", "path": str(root / "新放的.txt"), "size": 1, "mtime": "2026"},
    ]
    result = {
        "roots": [{"root_id": root_id, "path": str(root)}],
        "folders": [],
        "loose": {"count": 2, "recent": cached},
    }

    with db.autocommit() as connection:
        material_graph.decorate_roots(connection, "p", result, counts=None)

    assert [entry["file_id"] for entry in result["loose"]["recent"]] == [loose_id, None]
    assert "file_id" not in cached[0]
    assert result["roots"][0]["content"] is None


def test_cards_files_only_lists_the_cards_zone(tmp_path):
    client, db, _root, root_id = graph_world(tmp_path)
    card = add_file(db, root_id, "声档会议记录/0926 周会.md", zone="cards", mtime=5)
    add_file(db, root_id, "声档会议记录/0901 周会.md", zone="cards", mtime=3, gone=True)
    add_file(db, root_id, "方案.docx", mtime=9)

    body = client.get("/api/graph/projects/p/cards-files").json()

    assert body["files"] == [
        {
            "file_id": card,
            "name": "0926 周会.md",
            "rel_path": "声档会议记录/0926 周会.md",
            "root_id": root_id,
            "mtime": body["files"][0]["mtime"],
        }
    ]
    assert client.get("/api/graph/projects/nope/cards-files").status_code == 404


def test_expand_gives_file_rows_their_ids(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    (root / "方案").mkdir()
    (root / "方案" / "定稿.docx").write_bytes(b"x")
    (root / "方案" / "刚放进去.txt").write_bytes(b"x")
    known = add_file(db, root_id, "方案/定稿.docx")

    body = client.get("/api/graph/expand", params={"root": root_id, "dir": "方案"}).json()

    ids = {item["name"]: item["file_id"] for item in body["files"]}
    assert ids == {"定稿.docx": known, "刚放进去.txt": None}


# ---------------------------------------------------------------------- 需求文件夹的文件


def test_requirement_folder_files_come_from_the_index_when_the_root_is_scanned(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    (root / "白名单").mkdir()
    folder_id = add_folder(db, "r1", root / "白名单")
    index_done(db, root_id)
    b = add_file(db, root_id, "白名单/b.xlsx", mtime=2)
    a = add_file(db, root_id, "白名单/子/a.docx", mtime=1)
    add_file(db, root_id, "白名单/没了.docx", gone=True)
    add_file(db, root_id, "白名单外.docx")

    body = client.get(f"/api/requirements/r1/folders/{folder_id}/files").json()

    assert body["exists"] is True and body["total"] == 2 and body["capped"] is False
    assert [(item["relative_path"], item["file_id"]) for item in body["items"]] == [
        ("b.xlsx", b),
        ("子/a.docx", a),
    ]
    assert body["items"][0]["size_bytes"] == 10
    paged = client.get(
        f"/api/requirements/r1/folders/{folder_id}/files", params={"offset": 1, "limit": 1}
    ).json()
    assert [item["relative_path"] for item in paged["items"]] == ["子/a.docx"] and paged[
        "total"
    ] == 2


def test_requirement_folder_files_fall_back_to_disk_until_the_index_is_done(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    (root / "白名单").mkdir()
    (root / "白名单" / "盘上的.txt").write_bytes(b"abc")
    folder_id = add_folder(db, "r1", root / "白名单")
    add_file(db, root_id, "白名单/库里的.xlsx")
    index_done(db, root_id, state="walking")

    body = client.get(f"/api/requirements/r1/folders/{folder_id}/files").json()

    assert [item["relative_path"] for item in body["items"]] == ["盘上的.txt"]


def test_folder_files_from_index_needs_the_root_online(tmp_path):
    _client, db, root, root_id = graph_world(tmp_path)
    index_done(db, root_id)
    add_file(db, root_id, "白名单/b.xlsx")

    with db.autocommit() as connection:
        offline = material_graph.folder_files_from_index(
            connection,
            "p",
            str(root / "白名单"),
            limit=10,
            offset=0,
            state_of=lambda path: ROOT_VOLUME_OFFLINE,
        )
        online = material_graph.folder_files_from_index(
            connection,
            "p",
            str(root / "白名单"),
            limit=10,
            offset=0,
            state_of=lambda path: ROOT_ONLINE,
        )
        elsewhere = material_graph.folder_files_from_index(
            connection,
            "p",
            str(tmp_path / "别处"),
            limit=10,
            offset=0,
            state_of=lambda path: ROOT_ONLINE,
        )

    assert offline is None and elsewhere is None
    assert [item["relative_path"] for item in online["items"]] == ["b.xlsx"]


# ---------------------------------------------------------------------- 文件面板和交付物


def test_file_panel_has_state_and_deliverables(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    add_meeting(db, "m1", ago=1, project_id="p")
    add_task(db, "t1", meeting_id="m1", project_id="p", status="confirmed")
    add_content(db, "k-plan", ["正文"])
    (root / "方案.docx").write_bytes(b"plan")
    file_id = add_file(db, root_id, "方案.docx", key="k-plan")
    headers = write_headers(client)
    client.post("/api/tasks/t1/deliverables", json={"file_id": file_id}, headers=headers)

    body = client.get(f"/api/graph/files/{file_id}").json()

    assert body["state"]["kind"] == "done"
    assert [(item["task_id"], item["title"]) for item in body["deliverables"]] == [
        ("t1", "任务 t1")
    ]


def test_mark_a_file_as_deliverable_then_undo(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    add_meeting(db, "m1", ago=1, project_id="p")
    add_task(db, "t1", meeting_id="m1", project_id="p", status="in_progress")
    add_task(db, "t2", meeting_id="m1", project_id="p", status="confirmed")
    (root / "交付").mkdir()
    (root / "交付" / "定稿.pdf").write_bytes(b"final version")
    file_id = add_file(db, root_id, "交付/定稿.pdf")
    headers = write_headers(client)

    added = client.post("/api/tasks/t1/deliverables", json={"file_id": file_id}, headers=headers)

    assert added.status_code == 200
    body = added.json()
    deliverable_id = body["deliverable_id"]
    [item] = body["deliverables"]
    assert item["id"] == deliverable_id and item["kind"] == "file" and item["title"] == "定稿.pdf"
    assert item["url"] == f"{root}/交付/定稿.pdf"
    assert item["file_id"] == file_id and item["gone"] is False
    # 标为交付物不顺带把任务标成完成
    assert body["status"] == "in_progress"
    link = db.query_one(
        "SELECT * FROM deliverable_files WHERE deliverable_id = ?", (deliverable_id,)
    )
    # 还没算过内容标识的文件，盘在就现算一个
    assert (
        link["content_key"] and link["root_id"] == root_id and link["rel_path"] == "交付/定稿.pdf"
    )

    other = client.request(
        "DELETE", f"/api/tasks/t2/deliverables/{deliverable_id}", json={}, headers=headers
    )
    assert other.status_code == 404
    undone = client.request(
        "DELETE", f"/api/tasks/t1/deliverables/{deliverable_id}", json={}, headers=headers
    )
    assert undone.status_code == 200
    assert undone.json()["deliverables"] == []
    assert undone.json()["events"][-1]["kind"] == "deliverable_removed"
    assert (
        db.query_one("SELECT 1 FROM deliverable_files WHERE deliverable_id = ?", (deliverable_id,))
        is None
    )
    assert (
        client.request(
            "DELETE", f"/api/tasks/t1/deliverables/{deliverable_id}", json={}, headers=headers
        ).status_code
        == 404
    )


def test_deliverable_input_takes_exactly_one_of_url_and_file_id(tmp_path):
    client, db, _root, root_id = graph_world(tmp_path)
    add_meeting(db, "m1", ago=1, project_id="p")
    add_task(db, "t1", meeting_id="m1", project_id="p", status="confirmed")
    file_id = add_file(db, root_id, "a.docx")
    gone_id = add_file(db, root_id, "b.docx", gone=True)
    headers = write_headers(client)

    def post(body):
        return client.post("/api/tasks/t1/deliverables", json=body, headers=headers).status_code

    assert post({"file_id": file_id, "url": "/x", "kind": "file"}) == 422
    assert post({}) == 422
    assert post({"url": "https://figma.com/x"}) == 422
    assert post({"file_id": 999999}) == 404
    assert post({"file_id": gone_id}) == 404
    assert post({"url": "https://figma.com/x", "kind": "figma"}) == 200


def test_file_deliverable_follows_the_content_after_a_move(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    add_meeting(db, "m1", ago=1, project_id="p")
    add_task(db, "t1", meeting_id="m1", project_id="p", status="confirmed")
    add_content(db, "k-final", ["正文"])
    old = add_file(db, root_id, "交付/定稿.pdf", key="k-final")
    headers = write_headers(client)
    client.post("/api/tasks/t1/deliverables", json={"file_id": old}, headers=headers)
    # 挪了位置：旧行不见了，新位置同样的内容
    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), old))
    moved = add_file(db, root_id, "归档/定稿-final.pdf", key="k-final")

    [item] = client.get("/api/tasks/t1").json()["deliverables"]
    assert item["file_id"] == moved and item["name"] == "定稿-final.pdf" and item["gone"] is False
    [task] = [
        task for task in client.get("/api/graph/meetings/m1").json()["tasks"] if task["id"] == "t1"
    ]
    assert task["deliverables"][0]["file_id"] == moved

    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), moved))
    [item] = client.get("/api/tasks/t1").json()["deliverables"]
    assert item["file_id"] is None and item["gone"] is True and item["name"] == "定稿.pdf"


def test_old_typed_paths_match_a_root_by_prefix(tmp_path):
    client, db, root, root_id = graph_world(tmp_path)
    add_meeting(db, "m1", ago=1, project_id="p")
    add_task(db, "t1", meeting_id="m1", project_id="p", status="confirmed")
    file_id = add_file(db, root_id, "交付/定稿.pdf")
    headers = write_headers(client)
    client.post(
        "/api/tasks/t1/deliverables",
        json={"kind": "file", "url": f"{root}/交付/定稿.pdf"},
        headers=headers,
    )
    client.post(
        "/api/tasks/t1/deliverables",
        json={"kind": "file", "url": f"{root}-副本/交付/定稿.pdf"},
        headers=headers,
    )
    client.post(
        "/api/tasks/t1/deliverables",
        json={"kind": "file", "url": f"{root}/交付/早就删了.pdf"},
        headers=headers,
    )
    client.post(
        "/api/tasks/t1/deliverables",
        json={"kind": "figma", "url": "https://figma.com/x"},
        headers=headers,
    )

    items = client.get("/api/tasks/t1").json()["deliverables"]

    # 不在任何根目录下的（「-副本」只是名字前缀相同）没法找，不算找不到；在根目录里找过没有的才算
    assert [(item.get("file_id"), item.get("gone")) for item in items] == [
        (file_id, False),
        (None, False),
        (None, True),
        (None, None),
    ]
    assert client.get(f"/api/graph/files/{file_id}").json()["deliverables"][0]["task_id"] == "t1"


def test_cards_reveal_is_only_for_this_machine(tmp_path):
    client, db, _root, _root_id = graph_world(tmp_path)

    remote = client.post(
        "/api/cards/reveal", json={"project_id": "p"}, headers=write_headers(client)
    )
    local, headers = local_client(client)
    here = local.post("/api/cards/reveal", json={"project_id": "p"}, headers=headers)

    assert remote.status_code == 403
    assert here.status_code != 403
