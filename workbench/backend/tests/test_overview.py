"""第二期 2c：全部项目概览，和项目图里的「像是新需求」、等补建的文件夹。"""

import json

from meeting_workbench import graph, overview
from meeting_workbench.db import utc_now

from .helpers import count_reads
from .test_graph import (
    TODAY,
    add_link,
    add_meeting,
    add_project,
    add_requirement,
    build,
    days_ago,
    make_db,
    seed_three_projects,
)
from .test_tasks_api import write_headers


def run(db, **kwargs):
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("ai_configured", True)
    with db.autocommit() as connection:
        return overview.overview(connection, **kwargs)


def set_names(db, meeting_id, **columns):
    assignments = ", ".join(f"{name}=?" for name in columns)
    values = [
        json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
        for value in columns.values()
    ]
    db.execute(
        f"""UPDATE project_links SET {assignments}
             WHERE id=(SELECT MAX(id) FROM project_links WHERE meeting_id=?)""",
        (*values, meeting_id),
    )


def test_islands_waiting_counts_match_the_project_graph(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    seed_three_projects(db)

    body = run(db)

    islands = {island["id"]: island for island in body["islands"]}
    yt = islands["p-yt"]
    assert yt["meetings_total"] == 13
    assert yt["meetings"] == 8  # 28 天内：近 7 天 3 场 + 7–28 天 4 场 + 待复核 1 场
    assert yt["waiting"] == {"review": 1, "doorstep": 1, "tasks": 2}
    assert yt["requirements_active"] == 3
    assert yt["last_day"] == TODAY.isoformat()
    assert islands["p-zt"]["waiting"]["doorstep"] == 2
    assert islands["p-x"]["waiting"]["doorstep"] == 1
    # 和项目图的状态句同一个口径
    status = build(db, "p-yt", window="28d")["status"]["waiting"]
    texts = {item["text"] for item in status}
    assert {"1 场可能是这个项目的", "1 场归属待复核", "2 条任务待确认"} <= texts
    assert body["bridges"] == [{"a": "p-yt", "b": "p-zt", "count": 2}]


def test_harbour_counts_states_and_lists_recent_meetings(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    seed_three_projects(db)
    add_meeting(db, "none-1", ago=2)
    add_link(db, "none-1", status="unresolved")
    add_meeting(db, "new-1", ago=4)
    add_link(db, "new-1", status="unresolved")
    set_names(db, "new-1", new_project_name="云图看板", new_name_spoken=["看板"])
    add_meeting(db, "new-2", ago=6)
    add_link(db, "new-2", status="unresolved")
    set_names(db, "new-2", new_project_name="云图看板项目")
    add_meeting(db, "mine", ago=1, origin="manual")  # 你标了「不归项目」：不进港湾
    add_meeting(db, "old", ago=60)
    add_link(db, "old", status="unresolved")

    body = run(db)

    harbour = body["harbour"]
    assert harbour["counts"] == {"ai_pending": 1, "needs_review": 2, "none": 1, "new_project": 2}
    assert harbour["total"] == 6
    ids = [item["id"] for item in harbour["recent"]]
    assert "mine" not in ids and "old" not in ids
    door = next(item for item in harbour["recent"] if item["id"] == "door-1")
    assert [candidate["project_id"] for candidate in door["candidates"]] == ["p-zt", "p-yt"]
    assert set(door["candidates"][0]) == {
        "project_id",
        "project_name",
        "project_color",
        "count",
        "llm",
        "current",
    }
    new = next(item for item in harbour["recent"] if item["id"] == "new-1")
    assert new["name_hint"] == {"kind": "project", "name": "云图看板", "spoken": ["看板"]}
    assert body["suggested_projects"] == [
        {
            "name": "云图看板",
            "key": "云图看板",
            "meeting_count": 2,
            "meeting_ids": ["new-1", "new-2"],
            "last_at": body["suggested_projects"][0]["last_at"],
        }
    ]
    assert run(db, ai_configured=False)["harbour"]["ai_configured"] is False


def test_all_projects_come_back_whatever_the_window(tmp_path):
    """太阳系按最近一次会分圈、最外圈多了前端自己收成小行星带：45 个项目全部返回，不按时间窗折叠。"""
    _client, _settings, db = make_db(tmp_path)
    for index in range(45):
        add_project(db, f"p-{index:02d}", f"项目{index:02d}")
    for index in range(5):
        add_meeting(db, f"m-{index}", ago=1, project_id=f"p-{index + 40:02d}", origin="manual")
    add_meeting(db, "m-ten", ago=10, project_id="p-00", origin="manual")

    for window in ("7d", "28d"):
        body = run(db, window=window)
        assert len(body["islands"]) == 45
        assert body["islands_more"] is None
    # 7 天窗口里没会、10 天前开过会的项目照样在图上，last_day 照给（它落在 28 天那圈）
    p00 = next(island for island in run(db, window="7d")["islands"] if island["id"] == "p-00")
    assert p00["meetings"] == 0
    assert p00["last_day"] == days_ago(10)[:10]


def test_beyond_the_cap_the_longest_quiet_projects_fold(tmp_path, monkeypatch):
    """多到上限时按最近一次会由近到远留，最久没开会的（没开过会的排最后）折起来，和时间窗无关。"""
    monkeypatch.setattr(overview, "MAX_ISLANDS", 10)
    _client, _settings, db = make_db(tmp_path)
    for index in range(15):
        add_project(db, f"p-{index:02d}", f"项目{index:02d}")
    # 开过会的都是后建的项目，免得「按创建先后留」碰巧也对
    for index, ago in ((14, 2), (13, 3), (12, 20), (11, 40), (10, 60), (9, 90)):
        add_meeting(db, f"m-{index}", ago=ago, project_id=f"p-{index:02d}", origin="manual")

    for window in ("7d", "28d", "all"):
        body = run(db, window=window)
        # 6 个开过会的全留，剩下 4 个位置给没开过会的里建得最早的；返回仍按创建先后排
        assert [island["id"] for island in body["islands"]] == [
            "p-00",
            "p-01",
            "p-02",
            "p-03",
            "p-09",
            "p-10",
            "p-11",
            "p-12",
            "p-13",
            "p-14",
        ]
        assert body["islands_more"] == {
            "count": 5,
            "project_ids": ["p-04", "p-05", "p-06", "p-07", "p-08"],
        }


def test_overview_sql_count_is_fixed(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    seed_three_projects(db)

    def count():
        statements = []
        with db.autocommit() as connection:
            connection.set_trace_callback(
                lambda sql: (
                    statements.append(sql)
                    if sql.lstrip().upper().startswith(("SELECT", "WITH"))
                    else None
                )
            )
            overview.overview(connection, today=TODAY, ai_configured=True)
        return len(statements)

    small = count()
    for index in range(30):
        add_project(db, f"extra-{index}", f"额外{index}")
        add_meeting(db, f"extra-m-{index}", ago=index, project_id=f"extra-{index}", origin="ai")
        add_requirement(db, f"extra-r-{index}", f"extra-{index}", f"需求{index}")
    assert count() == small <= 10


def test_overview_endpoint_uses_etag(tmp_path):
    client, _settings, db = make_db(tmp_path)
    seed_three_projects(db)

    first = client.get("/api/graph/overview?window=28d")
    assert first.status_code == 200
    again = client.get(
        "/api/graph/overview?window=28d", headers={"If-None-Match": first.headers["ETag"]}
    )
    assert again.status_code == 304
    add_meeting(db, "fresh", ago=0)
    changed = client.get(
        "/api/graph/overview?window=28d", headers={"If-None-Match": first.headers["ETag"]}
    )
    assert changed.status_code == 200
    assert client.get("/api/graph/overview?window=5d").status_code == 422


def test_overview_folders_follow_the_project_parent(tmp_path):
    client, _settings, _db = make_db(tmp_path)
    headers = write_headers(client)
    browse = tmp_path / "materials-browse-root"
    parent = browse / "项目"
    for name in ("云图AI", "数据中台", "报价"):
        (parent / name).mkdir(parents=True)
    assert client.get("/api/graph/overview/folders").json()["state"] == "unset"

    assert (
        client.put(
            "/api/settings/project-parent", json={"path": str(parent)}, headers=headers
        ).status_code
        == 200
    )
    client.app.state.roots_cache.refresh()
    body = client.get("/api/graph/overview/folders").json()

    assert body["state"] == "ready"
    assert body["parent"]["path"] == str(parent.resolve())
    assert {folder["name"] for folder in body["folders"]} == {"云图AI", "数据中台", "报价"}
    assert all("action" in folder for folder in body["folders"])
    assert body["more"] == 0


# ---------------------------------------------------------------------- 项目图


def test_project_graph_shows_suggested_requirements_and_pending_folder(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_requirement(db, "r-shelved", "p", "报价单模板", status="shelved")
    for index, name in enumerate(["数据看板", "数据 看板", "权限中心", "报价单模板"]):
        meeting_id = f"m-{index}"
        add_meeting(db, meeting_id, ago=index + 1, project_id="p", origin="ai")
        add_link(db, meeting_id, project_id="p")
        set_names(
            db,
            meeting_id,
            new_requirement_name=name,
            new_name_project_id="p",
            new_name_spoken=[name],
        )
    db.execute(
        """INSERT INTO pending_project_folders(project_id, parent, name, state, created_at)
           VALUES ('p', '/Volumes/资料盘/项目', NULL, 'waiting', ?)""",
        (utc_now(),),
    )

    body = build(db, "p", window="28d")

    suggested = body["suggested_requirements"]
    assert [(item["name"], item["count"]) for item in suggested] == [
        ("数据看板", 2),
        ("权限中心", 1),
    ]
    assert suggested[0]["id"].startswith("nr:") and suggested[0]["meeting_ids"] == ["m-0", "m-1"]
    edges = [edge for edge in body["edges"] if edge["kind"] == "suggested"]
    assert {(edge["from"], edge["to"]) for edge in edges} == {
        ("m:m-0", suggested[0]["id"]),
        ("m:m-1", suggested[0]["id"]),
        ("m:m-2", suggested[1]["id"]),
    }
    pending = [folder for folder in body["folders"] if folder["kind"] == "pending"]
    assert pending == [
        {
            "id": "pending:p",
            "kind": "pending",
            "name": "云图AI",
            "path": "/Volumes/资料盘/项目/云图AI",
            "parent": "/Volumes/资料盘/项目",
            "state": "waiting",
            "reason": None,
            "ring": "inner",
        }
    ]
    # 只有待补建的文件夹时，不画卡片和散放文件（那些只看真正的根目录）
    assert not any(folder["kind"] in ("cards", "root") for folder in body["folders"])
    assert body["loose"] is None
    assert graph.GRAPH_API_VERSION == 4


def test_project_graph_sql_count_unchanged_with_hints(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")

    def count():
        return count_reads(db, lambda connection: graph.project_graph(connection, "p", today=TODAY))

    before = count()
    for index in range(5):
        add_meeting(db, f"m-{index}", ago=index, project_id="p", origin="ai")
        add_link(db, f"m-{index}", project_id="p")
        set_names(db, f"m-{index}", new_requirement_name=f"新需求{index}", new_name_project_id="p")
    assert count() == before <= 12
