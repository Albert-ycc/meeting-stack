"""1h 关系图：展开一场会、宽原话、残影日期、信标里的任务、全文次数、子文件夹、在访达中显示。"""
import os
import time
from datetime import date

import pytest
from starlette.testclient import TestClient

from meeting_workbench import graph
from meeting_workbench.db import utc_now

from .test_graph import (
    add_meeting,
    add_project,
    add_requirement,
    add_task,
    add_term,
    build,
    make_db,
)
from .test_tasks_api import write_headers

FOCUS_MINUTES = """# 周会

## 一分钟摘要
定了初审阈值。

## 决议
### 决议 1 · 阈值先按 0.8 执行 [00:12:34]
理由是上周误报太多。
下周复盘一次。
### 决议 2 · 驻场排班改两班
"""


def add_deliverable(db, task_id, url):
    db.execute(
        "INSERT INTO deliverables(task_id, kind, url, title, created_at) VALUES (?, 'file', ?, '', ?)",
        (task_id, url, utc_now()),
    )


def set_anchor(db, task_id, anchor_ms, quote=""):
    db.execute("UPDATE tasks SET anchor_ms=?, anchor_quote=? WHERE id=?", (anchor_ms, quote, task_id))


def test_meeting_focus_has_timeline_items_and_neighbours(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_meeting(db, "m-old", ago=9, project_id="p", origin="manual", title="上一场")
    add_meeting(
        db, "m-1", ago=5, project_id="p", origin="manual", title="这一场",
        segments=[(0, "开场"), (600_000, "中间"), (1_500_000, "收尾")],
        minutes=FOCUS_MINUTES,
    )
    add_meeting(db, "m-new", ago=1, project_id="p", origin="manual", title="下一场")
    add_meeting(db, "m-other", ago=3, project_id="q", origin="manual")
    add_requirement(db, "r-1", "p", "白名单运营后台")
    db.execute(
        "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES ('r-1', 'm-1', ?)",
        (utc_now(),),
    )
    add_task(db, "t-late", meeting_id="m-1", project_id="p", status="confirmed", requirement_id="r-1")
    add_task(db, "t-early", meeting_id="m-1", project_id="p")
    add_task(db, "t-none", meeting_id="m-1", project_id="p", status="done")
    add_task(db, "t-gone", meeting_id="m-1", project_id="p", status="cancelled")
    set_anchor(db, "t-late", 900_000, "这个月底前把白名单做完")
    set_anchor(db, "t-early", 120_000)
    add_deliverable(db, "t-late", "/Volumes/资料盘/云图AI/白名单/v1.xlsx")

    response = client.get("/api/graph/meetings/m-1")
    body = response.json()

    assert response.status_code == 200
    # 没记录音长度时按逐字稿最后一段的结束时间
    assert body["meeting"]["duration_ms"] == 1_504_000
    assert body["decisions"] == [
        {"text": "阈值先按 0.8 执行", "start_ms": 754_000, "detail": "理由是上周误报太多。 下周复盘一次。"},
        {"text": "驻场排班改两班", "start_ms": None, "detail": ""},
    ]
    # 按时间点排，没时间点的排最后；已取消的不画
    assert [task["id"] for task in body["tasks"]] == ["t-early", "t-late", "t-none"]
    late = body["tasks"][1]
    assert late["requirement_title"] == "白名单运营后台"
    assert late["anchor_quote"] == "这个月底前把白名单做完"
    assert late["deliverables"] == [{"kind": "file", "url": "/Volumes/资料盘/云图AI/白名单/v1.xlsx", "title": ""}]
    assert [item["id"] for item in body["requirements"]] == ["r-1"]
    assert body["previous"]["meeting_id"] == "m-old" and body["previous"]["title"] == "上一场"
    assert body["next"]["meeting_id"] == "m-new"
    assert client.get("/api/graph/meetings/nope").status_code == 404


def test_meeting_focus_without_project_has_no_neighbours(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_meeting(db, "m-1", ago=0)
    body = client.get("/api/graph/meetings/m-1").json()
    assert body["previous"] is None and body["next"] is None
    assert body["decisions"] == [] and body["decisions_note"] == "纪要还没写好"
    assert body["meeting"]["duration_ms"] is None


def test_wide_quotes_reach_twenty_seconds_each_side(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_meeting(
        db, "m-1", ago=0,
        segments=[(0, "开场"), (14_000, "前面"), (30_000, "锚点"), (46_000, "后面"), (80_000, "太远")],
    )
    short = client.get("/api/meetings/m-1/quotes", params={"at": 30_000}).json()
    wide = client.get("/api/meetings/m-1/quotes", params={"at": 30_000, "span": "wide"}).json()
    assert [seg["text"] for seg in short["quotes"][0]["segments"]] == ["锚点"]
    assert [seg["text"] for seg in wide["quotes"][0]["segments"]] == ["前面", "锚点", "后面"]


def test_moved_out_carries_date_for_the_ghost_slot(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_meeting(db, "m-1", ago=3, project_id="p", origin="ai", today=date.today())
    client.patch("/api/meetings/m-1", json={"project_id": "q"}, headers=write_headers(client))

    moved = client.get("/api/graph/projects/p").json()["moved_out"][0]

    assert moved["age_days"] == 3
    assert moved["date"] == (date.today().fromordinal(date.today().toordinal() - 3)).isoformat()


def test_beacon_task_items_name_the_requirement_they_would_leave(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_requirement(db, "r-1", "p", "白名单运营后台")
    # 会已经搬到数据中台，挂在云图AI需求上的任务留在云图AI
    add_meeting(db, "m-1", ago=1, project_id="q", origin="manual")
    add_task(db, "t-1", meeting_id="m-1", project_id="p", status="confirmed", requirement_id="r-1")

    body = build(db, "p")

    beacon = body["beacons"][0]
    item = beacon["items"][0]
    assert item["kind"] == "task_from_elsewhere"
    assert item["requirement_title"] == "白名单运营后台"
    assert (item["task_project_id"], item["meeting_project_id"]) == ("p", "q")
    # 这头落在需求上，而不是看不见的会
    assert any(edge["kind"] == "cross" and edge["from"] == "r:r-1" for edge in body["edges"])

    other = build(db, "q")["beacons"][0]["items"][0]
    assert other["kind"] == "task_elsewhere" and other["requirement_id"] == "r-1"


def test_fulltext_counts_all_spellings_once_and_ignores_window(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_term(db, "t-zt", "数据中台", "p")
    db.execute(
        "UPDATE glossary_terms SET aliases=?, also=? WHERE id='t-zt'",
        ('["数据中太"]', '["中台"]'),
    )
    add_meeting(db, "m-new", ago=0, project_id="p", segments=[(0, "数据中台和中台是一回事"), (5000, "数据中太")])
    add_meeting(db, "m-old", ago=200, project_id="p", segments=[(0, "老会议提到中台"), (7000, "没提")])
    add_meeting(db, "m-q", ago=0, project_id="q", segments=[(0, "中台中台中台")])
    add_meeting(db, "m-api", ago=1, project_id="p", segments=[(0, "API 和 api 都算")])

    by_term = client.get("/api/graph/projects/p/fulltext", params={"term": "t-zt"}).json()
    assert by_term["variants"] == ["数据中台", "数据中太", "中台"]
    # 「数据中台」不再拆出一次「中台」；别的项目的会不算；200 天前的也算
    assert [(item["meeting_id"], item["count"], item["first_ms"]) for item in by_term["meetings"]] == [
        ("m-new", 3, 0),
        ("m-old", 1, 0),
    ]
    assert by_term["total"] == 4 and by_term["meeting_count"] == 2

    by_text = client.get("/api/graph/projects/p/fulltext", params={"q": "Api"}).json()
    assert [(item["meeting_id"], item["count"]) for item in by_text["meetings"]] == [("m-api", 2)]

    assert client.get("/api/graph/projects/p/fulltext").status_code == 400
    assert client.get("/api/graph/projects/p/fulltext", params={"term": "nope"}).status_code == 404
    assert client.get("/api/graph/projects/nope/fulltext", params={"q": "x"}).status_code == 404


def make_root(db, tmp_path, name="云图AI", project_id="p"):
    root = tmp_path / "materials" / name
    root.mkdir(parents=True)
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, str(root), utc_now()),
    )
    root_id = db.query_one("SELECT id FROM project_material_roots WHERE path=?", (str(root),))["id"]
    return root, root_id


def touch_dir(path, mtime):
    path.mkdir(parents=True, exist_ok=True)
    os.utime(path, (mtime, mtime))


def test_expand_reads_one_level_inside_a_registered_root(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root, root_id = make_root(db, tmp_path)
    now = time.time()
    touch_dir(root / "报价", now - 300)
    touch_dir(root / "白名单" / "第一版", now - 600)
    touch_dir(root / "白名单", now - 100)
    touch_dir(root / "声档会议记录", now)
    touch_dir(root / ".git", now)
    (root / "说明.docx").write_bytes(b"abc")
    (root / "~$说明.docx").write_bytes(b"lock")
    (root / "白名单" / "v1.xlsx").write_bytes(b"12345")

    body = client.get("/api/graph/expand", params={"root": root_id}).json()

    assert body["dir"] == "" and body["crumbs"] == [] and body["state"] == "online"
    # 按修改时间排；声档会议记录/ 在图上另有节点，隐藏目录不列
    assert [item["name"] for item in body["dirs"]] == ["白名单", "报价"]
    assert [item["name"] for item in body["files"]] == ["说明.docx"]
    assert body["files"][0]["size"] == 3

    inner = client.get("/api/graph/expand", params={"root": root_id, "dir": "白名单"}).json()
    assert [item["name"] for item in inner["dirs"]] == ["第一版"]
    assert inner["dirs"][0]["dir"] == "白名单/第一版"
    assert [item["name"] for item in inner["files"]] == ["v1.xlsx"]
    assert inner["crumbs"] == [{"name": "白名单", "dir": "白名单"}]
    cards = client.get("/api/graph/expand", params={"root": root_id, "dir": "声档会议记录"})
    assert cards.status_code == 200


def test_expand_refuses_paths_outside_the_root(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root, root_id = make_root(db, tmp_path)
    (tmp_path / "materials" / "别的项目").mkdir()
    (root / ".secret").mkdir()
    (root / "外链").symlink_to(tmp_path / "materials" / "别的项目")

    for bad in ("../别的项目", "/etc", ".secret", "外链"):
        response = client.get("/api/graph/expand", params={"root": root_id, "dir": bad})
        assert response.status_code == 400, bad
    assert client.get("/api/graph/expand", params={"root": root_id, "dir": "没有这个"}).status_code == 404
    assert client.get("/api/graph/expand", params={"root": 999}).status_code == 404


def test_expand_on_an_unplugged_disk_says_so_without_reading(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/Volumes/没插的盘/云图AI', ?)",
        (utc_now(),),
    )
    root_id = db.query_one("SELECT id FROM project_material_roots")["id"]
    body = client.get("/api/graph/expand", params={"root": root_id}).json()
    assert body["state"] == "volume_offline" and body["dirs"] == []


def test_roots_cache_keeps_three_recent_subfolders(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root, _root_id = make_root(db, tmp_path)
    now = time.time()
    for index, name in enumerate(["A", "B", "C", "D"]):
        touch_dir(root / name, now - index * 100)
    touch_dir(root / "声档会议记录", now + 100)

    client.app.state.roots_cache.refresh()
    body = client.get("/api/graph/projects/p/roots").json()

    assert [item["name"] for item in body["roots"][0]["recent_dirs"]] == ["A", "B", "C"]
    assert body["roots"][0]["recent_dirs"][0]["dir"] == "A"
    # testserver 不算本机，不给「在访达中显示」
    assert body["can_reveal"] is False


def local_client(client):
    local = TestClient(client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    token = local.get("/api/bootstrap").json()["csrf_token"]
    return local, {"X-CSRF-Token": token, "Origin": "http://127.0.0.1"}


def test_reveal_only_from_this_machine_and_only_registered_folders(tmp_path, monkeypatch):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root, _root_id = make_root(db, tmp_path)
    (root / "报价.xlsx").write_bytes(b"x")
    (root / ".hidden").mkdir()
    (tmp_path / "别处.txt").write_bytes(b"x")
    opened = []
    monkeypatch.setattr(graph, "reveal", lambda path: opened.append(path))

    remote = client.post(
        "/api/materials/reveal", json={"path": str(root / "报价.xlsx")}, headers=write_headers(client)
    )
    assert remote.status_code == 403

    local, headers = local_client(client)
    assert local.get("/api/graph/projects/p/roots").json()["can_reveal"] is True
    ok = local.post("/api/materials/reveal", json={"path": str(root / "报价.xlsx")}, headers=headers)
    assert ok.status_code == 200
    assert opened == [(root / "报价.xlsx").resolve()]
    for bad, status in (
        (str(tmp_path / "别处.txt"), 400),
        ("相对/路径", 400),
        (str(root / ".hidden"), 400),
        (str(root / "../别处.txt"), 400),
        (str(root / "没有.xlsx"), 404),
    ):
        response = local.post("/api/materials/reveal", json={"path": bad}, headers=headers)
        assert response.status_code == status, bad
    assert len(opened) == 1


@pytest.mark.parametrize(
    ("host", "client_host", "expected"),
    [
        ("127.0.0.1:8765", "127.0.0.1", True),
        ("localhost:8765", "::1", True),
        ("[::1]:8765", "::1", True),
        ("mac.tail1234.ts.net", "127.0.0.1", False),
        ("127.0.0.1:8765", "100.64.0.2", False),
        (None, "127.0.0.1", False),
    ],
)
def test_local_request_means_host_and_peer_are_this_machine(host, client_host, expected):
    assert graph.is_local_request(host, client_host) is expected


def test_reveal_command_selects_the_file_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(graph.sys, "platform", "darwin")
    assert graph.reveal_command(tmp_path / "a.txt") == ["open", "-R", str(tmp_path / "a.txt")]
    calls = []
    graph.reveal(tmp_path, run=lambda command, **kwargs: calls.append(command))
    assert calls == [["open", "-R", str(tmp_path)]]


def test_drag_preview_counts_match_what_the_move_does(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_requirement(db, "r-1", "p", "白名单运营后台")
    add_meeting(db, "m-1", ago=0, project_id="p", origin="manual", today=date.today())
    add_task(db, "t-plain", meeting_id="m-1", project_id="p", status="confirmed")
    add_task(db, "t-draft", meeting_id="m-1", project_id=None)
    add_task(db, "t-done", meeting_id="m-1", project_id="p", status="done")
    add_task(db, "t-req", meeting_id="m-1", project_id="p", requirement_id="r-1")

    node = client.get("/api/graph/projects/p").json()["meetings"][0]
    moved = client.patch("/api/meetings/m-1", json={"project_id": "q"}, headers=write_headers(client)).json()

    assert (node["tasks_follow"], node["tasks_stay"]) == (3, 1)
    assert moved["effects"]["tasks_moved"] == node["tasks_follow"]
    assert len(moved["effects"]["tasks_left"]) == node["tasks_stay"]
