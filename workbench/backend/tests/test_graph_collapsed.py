"""graph.collapsed_meetings：不再为找一个折叠组把整张图算一遍，返回和整张图算出来的一致。

折叠哪些会由节点预算定，文件节点（会上提到的、琥珀色的）占预算，所以折叠组不是「只看会的日期」就算得出来：
预算紧的项目里文件会把中圈的名额挤掉几个，被挤掉的会就折进「更早 N 场」。只有预算的余量大到文件怎么挤
都挤不动时，才能不读 ⑫（会上提到的文件，占整张图七成的时间）。"""

import json
import random
from datetime import UTC, datetime

import pytest

from meeting_workbench import graph, relation_read
from meeting_workbench.db import Database, utc_now

from .helpers import count_reads
from .test_graph import (
    TODAY,
    _affects,
    _decision,
    _file,
    _upsert,
    add_link,
    add_meeting,
    add_project,
    add_requirement,
    add_task,
    add_term,
    cue,
    make_db,
    stop_clock,
)
from .test_relation_read import literal

WINDOWS = (None, "7d", "28d", "90d", "all")


def new_db(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "w.sqlite3")
    db.initialize()
    return db


# 名字: (会, 线索词, 需求, 带文件夹的需求, 有根目录, 文件, 琥珀色文件, 门口, 信标, 像是新需求, 会的年龄分布)
PRESETS = {
    "空项目": (0, 0, 0, 0, False, 0, 0, 0, 0, 0, "mixed"),
    "几场会": (6, 2, 1, 0, False, 0, 0, 0, 0, 0, "mixed"),
    "宽松": (60, 3, 2, 1, True, 6, 0, 1, 1, 1, "mixed"),
    "近期很密": (45, 20, 4, 2, True, 14, 2, 2, 2, 2, "recent"),
    "全面挤满": (80, 45, 14, 10, True, 24, 10, 3, 4, 3, "recent"),
    "文件把预算挤紧": (60, 8, 3, 1, True, 40, 8, 1, 1, 0, "recent"),
    "窗外一大堆": (200, 4, 2, 1, True, 8, 0, 1, 1, 1, "old"),
    "跨好几个月": (120, 10, 5, 2, True, 20, 3, 1, 2, 1, "months"),
}


def ago_of(rng, shape):
    roll = rng.random()
    if shape == "recent":
        return (
            rng.randint(0, 6)
            if roll < 0.45
            else rng.randint(7, 27)
            if roll < 0.9
            else rng.randint(28, 120)
        )
    if shape == "old":
        return rng.randint(0, 27) if roll < 0.15 else rng.randint(28, 400)
    if shape == "months":
        return rng.randint(0, 27) if roll < 0.2 else rng.randint(28, 300)
    if roll < 0.2:
        return rng.randint(0, 6)
    return (
        rng.randint(7, 27)
        if roll < 0.5
        else rng.randint(28, 90)
        if roll < 0.75
        else rng.randint(91, 400)
    )


def build_world(db, preset, seed):
    """随机造一个项目 p：会、线索词、需求和文件夹、根目录、文件和会上提到、在问的影响、门口、信标、
    像是新需求。种子和预设定每样东西的多少，密的世界会把节点预算挤紧。"""
    rng = random.Random(seed * 1000 + sum(map(ord, preset)))
    meetings, cues, reqs, req_folders, root, files, amber, door, beacons, suggested, shape = (
        PRESETS[preset]
    )
    add_project(db, "p", "云图AI", "#2c8d83")
    add_project(db, "q", "数据中台", "#7a5af8")
    for index in range(cues):
        add_term(db, f"t{index}", f"线索词{index:02d}", "p")
    ids = []
    for index in range(meetings):
        meeting_id = f"m{index:03d}"
        ids.append(meeting_id)
        origin = rng.choice(["ai", "ai", "manual"])
        add_meeting(db, meeting_id, ago=ago_of(rng, shape), project_id="p", origin=origin)
        if cues and origin == "ai":
            picks = rng.sample(range(cues), k=min(cues, rng.randint(1, 3)))
            evidence = [
                cue("p", f"线索词{pick:02d}", rng.randint(1, 6), term_id=f"t{pick}")
                for pick in picks
            ]
            add_link(db, meeting_id, project_id="p", evidence=evidence)
    for index in range(suggested):
        for meeting_id in rng.sample(ids, k=min(len(ids), 2)):
            link = db.query_one(
                "SELECT id FROM project_links WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
                (meeting_id,),
            )
            if link is None:
                add_link(db, meeting_id, project_id="p")
                link = db.query_one(
                    "SELECT id FROM project_links WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
                    (meeting_id,),
                )
            db.execute(
                """UPDATE project_links SET new_requirement_name = ?, new_name_project_id = 'p',
                          new_name_spoken = ? WHERE id = ?""",
                (
                    f"新需求方案{index}号",
                    json.dumps([f"新需求方案{index}号"], ensure_ascii=False),
                    link["id"],
                ),
            )
    for index in range(door):
        add_meeting(db, f"door{index}", ago=rng.randint(0, 20))
        add_link(
            db,
            f"door{index}",
            status="needs_review",
            candidates=[{"project_id": "q", "count": 3}, {"project_id": "p", "count": 2}],
            reason="像是这个项目",
        )
    for index in range(reqs):
        add_requirement(
            db,
            f"r{index}",
            "p",
            f"需求{index}号标题",
            f"P{index % 4}",
            updated_ago=rng.randint(0, 60),
        )
        if index < req_folders:
            db.execute(
                "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, ?)",
                (f"r{index}", f"/材料/云图AI/需求{index}", utc_now()),
            )
        if ids and rng.random() < 0.5:
            db.execute(
                "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
                (f"r{index}", rng.choice(ids), utc_now()),
            )
    for index in range(beacons if ids else 0):
        add_requirement(db, f"q-r{index}", "q", f"中台需求{index}", "P1")
        db.execute(
            "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
            (f"q-r{index}", rng.choice(ids), utc_now()),
        )
        add_task(db, f"x{index}", meeting_id=rng.choice(ids), project_id="q", status="confirmed")
    if root:
        db.execute(
            "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/材料/云图AI', ?)",
            (utc_now(),),
        )
        root_id = db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'p'")["id"]
        for index in range(files):
            file_id = _file(db, root_id, f"资料/文件{index:02d}.docx", key=f"k{index}")
            for meeting_id in rng.sample(ids, k=min(len(ids), rng.randint(1, 5))) if ids else []:
                literal(db, meeting_id, f"文件{index:02d}", file_id, count=rng.randint(1, 6))
        rows = []
        for index, meeting_id in enumerate(rng.sample(ids, k=min(len(ids), amber)) if ids else []):
            file_id = _file(db, root_id, f"过时/报价{index:02d}.xlsx", key=f"ks{index}")
            _decision(
                db,
                f"dec{index}",
                meeting_id,
                f"决议{index}",
                start_ms=index * 60_000,
                ordinal=index,
            )
            rows.append(_affects(f"dec{index}", meeting_id, file_id, f"ks{index}"))
        if rows:
            _upsert(db, rows)


def whole_graph_collapsed_meetings(connection, project_id, group_id, *, window=None, today=None):
    """改前的 collapsed_meetings，原样留在这里当对拍的标准：为找一个折叠组把整张图算一遍。"""
    body = graph.project_graph(connection, project_id, window=window, today=today)
    group = next((item for item in body["collapsed"] if item["id"] == group_id), None)
    if group is None:
        raise graph.GraphNotFound("折叠节点不存在")
    ids = group["meeting_ids"]
    placeholders = ", ".join("?" for _ in ids)
    rows = (
        [
            dict(row)
            for row in connection.execute(
                f"""SELECT m.id, m.title, m.recording_date, m.created_at,
                       (SELECT COUNT(*) FROM tasks t
                         WHERE t.meeting_id = m.id AND t.status IN ({graph._OPEN})) AS open_tasks
                  FROM meetings m WHERE m.id IN ({placeholders})""",
                ids,
            ).fetchall()
        ]
        if ids
        else []
    )
    months = {}
    for row in rows:
        day = graph.local_day(row["recording_date"], row["created_at"])
        months.setdefault(day.strftime("%Y-%m"), []).append(
            {
                "meeting_id": row["id"],
                "title": row["title"],
                "date": day.isoformat(),
                "open_tasks": int(row["open_tasks"] or 0),
            }
        )
    result = []
    for key in sorted(months, reverse=True):
        items = sorted(
            months[key], key=lambda item: (item["date"], item["meeting_id"]), reverse=True
        )
        result.append({"month": key, "label": f"{key[:4]} 年 {int(key[5:])} 月", "meetings": items})
    return {"id": group_id, "label": group["label"], "months": result}


@pytest.fixture
def edges_spy(monkeypatch):
    """project_edges（⑫）被调了几次。"""
    calls = []
    real = relation_read.project_edges

    def spy(*args, **kwargs):
        calls.append(args[1:])
        return real(*args, **kwargs)

    monkeypatch.setattr(relation_read, "project_edges", spy)
    return calls


@pytest.mark.parametrize("seed", range(2))
@pytest.mark.parametrize("preset", PRESETS)
def test_every_collapsed_group_is_what_the_whole_graph_says(tmp_path, preset, seed):
    db = new_db(tmp_path)
    build_world(db, preset, seed)
    checked = 0
    with db.autocommit() as connection:
        for window in WINDOWS:
            body = graph.project_graph(connection, "p", window=window, today=TODAY)
            for group in body["collapsed"]:
                new = graph.collapsed_meetings(
                    connection, "p", group["id"], window=window, today=TODAY
                )
                old = whole_graph_collapsed_meetings(
                    connection, "p", group["id"], window=window, today=TODAY
                )
                assert new == old, (window, group["id"])
                assert sorted(
                    m["meeting_id"] for month in new["months"] for m in month["meetings"]
                ) == sorted(group["meeting_ids"])
                checked += 1
            with pytest.raises(graph.GraphNotFound, match="折叠节点不存在"):
                graph.collapsed_meetings(
                    connection, "p", "c:没有这一组", window=window, today=TODAY
                )
    assert checked or preset == "空项目"


def test_missing_project_and_bad_window_still_raise(tmp_path):
    db = new_db(tmp_path)
    build_world(db, "几场会", 0)
    with db.autocommit() as connection:
        with pytest.raises(graph.GraphNotFound, match="项目不存在"):
            graph.collapsed_meetings(connection, "nope", "c:older", today=TODAY)
        with pytest.raises(ValueError, match="时间窗只能是"):
            graph.collapsed_meetings(connection, "p", "c:older", window="1y", today=TODAY)


def test_loose_budget_skips_the_mentions_query_and_tight_budget_reads_it(tmp_path, edges_spy):
    """预算有余量的项目不查 ⑫，预算紧的项目每次查一次；两种都和整张图一致（上一条用例逐组对过）。"""
    loose_db = new_db(tmp_path / "loose")
    build_world(loose_db, "几场会", 1)
    tight_db = new_db(tmp_path / "tight")
    build_world(tight_db, "文件把预算挤紧", 1)

    for db, expected_reads_per_group in ((loose_db, 0), (tight_db, 1)):
        with db.autocommit() as connection:
            groups = graph.project_graph(connection, "p", today=TODAY)["collapsed"]
            assert groups
            edges_spy.clear()
            for group in groups:
                graph.collapsed_meetings(connection, "p", group["id"], today=TODAY)
            assert len(edges_spy) == expected_reads_per_group * len(groups)


def test_a_tight_budget_needs_the_files_to_know_what_is_collapsed(tmp_path):
    """文件把中圈的名额挤掉几个：不读文件算出来的折叠组少折几场会，和整张图对不上。"""
    db = new_db(tmp_path)
    build_world(db, "文件把预算挤紧", 1)
    with db.autocommit() as connection:
        now = datetime.now(UTC)
        without_files = graph._assemble(
            **graph._graph_inputs(connection, "p", now),
            window="28d",
            focus=None,
            today=TODAY,
            now=now,
            layout_only=True,
        )["collapsed"]
        body = graph.project_graph(connection, "p", window="28d", today=TODAY)
        assert without_files != body["collapsed"]
        assert len([m for m in body["meetings"] if m["ring"] == "middle"]) < graph.MIDDLE_CAP

        got = graph.collapsed_meetings(connection, "p", "c:older", window="28d", today=TODAY)

    older = next(group for group in body["collapsed"] if group["id"] == "c:older")
    listed = sorted(m["meeting_id"] for month in got["months"] for m in month["meetings"])
    assert listed == sorted(older["meeting_ids"])


def test_statement_count_with_and_without_the_mentions_query(tmp_path):
    """余量够时不查 ⑫：十一条读库语句加最后取会的一条；预算紧时多一条 ⑫。整张图是十二条。"""
    loose_db = new_db(tmp_path / "loose")
    build_world(loose_db, "几场会", 1)
    tight_db = new_db(tmp_path / "tight")
    build_world(tight_db, "文件把预算挤紧", 1)

    def collapsed(connection):
        return graph.collapsed_meetings(connection, "p", "c:older", today=TODAY)

    with loose_db.autocommit() as connection:
        loose_group = graph.project_graph(connection, "p", today=TODAY)["collapsed"][-1]["id"]
    assert loose_group == "c:older"
    assert count_reads(loose_db, collapsed) == 12
    assert count_reads(tight_db, collapsed) == 13
    assert count_reads(tight_db, lambda c: graph.project_graph(c, "p", today=TODAY)) == 12


def build_boundary_world(db, requirements):
    """节点预算的门槛上：中圈放满 10 个时，文件以外占 23 + requirements 个节点（项目 1、内圈 8、中圈 10、更早
    1、根目录 2、散放 1，再加进行中的需求）。琥珀色文件 9 个、提到的文件 16 个，文件占满收缩后的 12 个名额。"""
    add_project(db, "p", "云图AI")
    for index in range(12):
        add_meeting(db, f"in{index:02d}", ago=index % 7, project_id="p", origin="manual")
    for index in range(14):
        add_meeting(db, f"mid{index:02d}", ago=7 + index, project_id="p", origin="manual")
    for index, ago in enumerate((40, 60, 100)):
        add_meeting(db, f"old{index}", ago=ago, project_id="p", origin="manual")
    for index in range(requirements):
        add_requirement(db, f"r{index}", "p", f"需求{index}号", "P1", updated_ago=1)
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/材料/云图AI', ?)",
        (utc_now(),),
    )
    root_id = db.query_one("SELECT id FROM project_material_roots")["id"]
    for index in range(16):
        file_id = _file(db, root_id, f"资料/文件{index:02d}.docx", key=f"k{index}")
        for meeting_id in (f"in{index % 12:02d}", f"in{(index + 5) % 12:02d}"):
            literal(db, meeting_id, f"文件{index:02d}", file_id, count=3)
    rows = []
    for index in range(9):
        file_id = _file(db, root_id, f"过时/报价{index:02d}.xlsx", key=f"ks{index}")
        _decision(
            db,
            f"dec{index}",
            f"mid{index:02d}",
            f"决议{index}",
            start_ms=index * 60_000,
            ordinal=index,
        )
        rows.append(_affects(f"dec{index}", f"mid{index:02d}", file_id, f"ks{index}"))
    _upsert(db, rows)


@pytest.mark.parametrize(
    ("requirements", "reads", "middle_shown"),
    [(5, 0, graph.MIDDLE_CAP), (6, 1, graph.MIDDLE_CAP - 1)],
    ids=["余量刚好够，不查 ⑫", "差一个节点，要查 ⑫"],
)
def test_the_threshold_is_exactly_what_the_files_can_take(
    tmp_path, edges_spy, requirements, reads, middle_shown
):
    """余量等于文件最多占的节点数（FILE_NODES_MAX）时，文件占满了也只是刚好 40 个，中圈不会少；差一个就会少一个。"""
    db = new_db(tmp_path)
    build_boundary_world(db, requirements)
    with db.autocommit() as connection:
        body = graph.project_graph(connection, "p", window="28d", today=TODAY)
        assert len([m for m in body["meetings"] if m["ring"] == "middle"]) == middle_shown
        edges_spy.clear()
        new = graph.collapsed_meetings(connection, "p", "c:older", window="28d", today=TODAY)
        assert len(edges_spy) == reads
        assert new == whole_graph_collapsed_meetings(
            connection, "p", "c:older", window="28d", today=TODAY
        )


def test_collapsed_endpoint_returns_what_the_function_returns(tmp_path, monkeypatch):
    """经路由：group、window 两个参数照旧，找不到的折叠组、找不到的项目照旧 404。"""
    today = stop_clock(monkeypatch)
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    for index, ago in enumerate((0, 3, 9, 15, 30, 45, 61, 120, 200)):
        add_meeting(db, f"m{index}", ago=ago, project_id="p", origin="manual", today=today)

    with db.autocommit() as connection:
        expected = graph.collapsed_meetings(connection, "p", "c:older", window="28d", today=today)
    response = client.get(
        "/api/graph/projects/p/collapsed", params={"group": "c:older", "window": "28d"}
    )

    assert response.status_code == 200 and response.json() == expected
    assert [m["meeting_id"] for month in expected["months"] for m in month["meetings"]]
    missing_group = client.get("/api/graph/projects/p/collapsed", params={"group": "c:没有这一组"})
    assert (missing_group.status_code, missing_group.json()["detail"]) == (404, "折叠节点不存在")
    missing_project = client.get("/api/graph/projects/nope/collapsed", params={"group": "c:older"})
    assert (missing_project.status_code, missing_project.json()["detail"]) == (404, "项目不存在")
