"""1g 关系图：整张图的节点、连线、状态句；SQL 条数固定；不读盘；ETag；面板用的小接口。"""
import json
import time
from datetime import UTC, date, datetime, timedelta


from meeting_workbench import graph
from meeting_workbench.db import Database, utc_now

from .helpers import count_reads
from .test_tasks_api import make_client, write_headers

TODAY = date(2026, 9, 26)


def days_ago(n, today=TODAY):
    return f"{(today - timedelta(days=n)).isoformat()}T10:00:00"


def add_project(db, project_id, name, color="#2c8d83"):
    db.execute(
        "INSERT INTO projects(id, name, color, created_at) VALUES (?, ?, ?, ?)",
        (project_id, name, color, utc_now()),
    )


def add_meeting(
    db,
    meeting_id,
    *,
    ago,
    project_id=None,
    origin=None,
    title=None,
    today=TODAY,
    segments=None,
    minutes=None,
):
    db.execute(
        """INSERT INTO meetings(id, title, recording_date, status, project_id, project_origin,
                                created_at, updated_at)
           VALUES (?, ?, ?, 'completed_unreviewed', ?, ?, ?, ?)""",
        (meeting_id, title or f"会 {meeting_id}", days_ago(ago, today), project_id, origin,
         utc_now(), utc_now()),
    )
    if segments is not None:
        version = db.create_transcript_version(meeting_id, "funasr", published=True)
        db.replace_segments(
            version,
            meeting_id,
            [
                {"id": f"{meeting_id}-s{index}", "ordinal": index, "start_ms": start,
                 "end_ms": start + 4000, "text": text}
                for index, (start, text) in enumerate(segments)
            ],
        )
    if minutes is not None:
        db.execute(
            """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind,
                                            published, created_at)
               VALUES (?, ?, 1, ?, '', 'generated', 0, ?)""",
            (f"mv-{meeting_id}", meeting_id, minutes, utc_now()),
        )
        db.execute(
            "UPDATE meetings SET current_minutes_version_id=? WHERE id=?",
            (f"mv-{meeting_id}", meeting_id),
        )


def add_link(db, meeting_id, *, status="done", project_id=None, evidence=(), candidates=None, reason=""):
    db.execute(
        """INSERT INTO project_links(meeting_id, minutes_version_id, status, method, project_id,
                                     evidence_json, candidates_json, reason, created_at)
           VALUES (?, ?, ?, 'literal', ?, ?, ?, ?, ?)""",
        (
            meeting_id,
            f"mv-{meeting_id}-{status}",
            status,
            project_id,
            json.dumps(list(evidence), ensure_ascii=False),
            json.dumps(candidates, ensure_ascii=False) if candidates is not None else None,
            reason,
            utc_now(),
        ),
    )


def cue(project_id, text, count, *, source="term", term_id=None, anchors=(0,)):
    entry = {
        "kind": "literal",
        "project_id": project_id,
        "cue": text,
        "source": source,
        "count": count,
        "anchors_ms": list(anchors),
        "where": {"title": 0, "transcript": count, "minutes": 0},
    }
    if term_id:
        entry["term_id"] = term_id
    return entry


def add_term(db, term_id, term, project_id, *, is_cue=1):
    db.execute(
        """INSERT INTO glossary_terms(id, term, scope, project_id, is_cue, confirmed,
                                      created_at, updated_at)
           VALUES (?, ?, '项目', ?, ?, 1, ?, ?)""",
        (term_id, term, project_id, is_cue, utc_now(), utc_now()),
    )


def add_requirement(db, requirement_id, project_id, title, priority="P1", *, updated_ago=1, status="active"):
    stamp = (datetime.now(UTC) - timedelta(days=updated_ago)).isoformat()
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (requirement_id, project_id, title, priority, status, stamp, stamp),
    )


def add_task(db, task_id, *, meeting_id, project_id, status="pending_confirm", requirement_id=None):
    now = utc_now()
    db.execute(
        """INSERT INTO tasks(id, title, status, meeting_id, project_id, requirement_id,
                             status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, f"任务 {task_id}", status, meeting_id, project_id, requirement_id, now, now, now),
    )


def make_db(tmp_path):
    client, settings = make_client(tmp_path)
    return client, settings, Database(settings.database_path)


def build(db, project_id, **kwargs):
    kwargs.setdefault("today", TODAY)
    with db.autocommit() as connection:
        return graph.project_graph(connection, project_id, **kwargs)


def seed_three_projects(db):
    add_project(db, "p-yt", "云图AI", "#2c8d83")
    add_project(db, "p-zt", "数据中台", "#7a5af8")
    add_project(db, "p-x", "其他项目", "#f79009")
    add_term(db, "t-rule", "初审规则", "p-yt")
    add_term(db, "t-off", "驻场排班", "p-yt", is_cue=0)
    # 云图AI：近 7 天 3 场、7–28 天 4 场、更早 5 场
    for index, ago in enumerate([0, 2, 5]):
        meeting_id = f"yt-in-{index}"
        add_meeting(db, meeting_id, ago=ago, project_id="p-yt", origin="ai")
        add_link(
            db, meeting_id, project_id="p-yt",
            evidence=[cue("p-yt", "初审规则", 3 + index, term_id="t-rule"),
                      cue("p-yt", "驻场排班", 5, term_id="t-off")],
        )
    for index, ago in enumerate([8, 12, 20, 27]):
        add_meeting(db, f"yt-mid-{index}", ago=ago, project_id="p-yt", origin="manual")
    for index, ago in enumerate([30, 40, 60, 100, 200]):
        add_meeting(db, f"yt-old-{index}", ago=ago, project_id="p-yt", origin="manual")
    # 待复核：已经归在云图AI，但最新批次要你选
    add_meeting(db, "yt-review", ago=3, project_id="p-yt", origin="ai")
    add_link(db, "yt-review", status="needs_review", project_id="p-yt",
             candidates=[{"project_id": "p-yt", "count": 2}, {"project_id": "p-zt", "count": 1}])
    # 门口：没归项目、候选里有云图AI
    add_meeting(db, "door-1", ago=1)
    add_link(db, "door-1", status="needs_review",
             candidates=[{"project_id": "p-zt", "count": 3}, {"project_id": "p-yt", "count": 2}],
             reason="提到了初审规则也提到了中台")
    add_meeting(db, "door-other", ago=1)
    add_link(db, "door-other", status="needs_review",
             candidates=[{"project_id": "p-zt", "count": 3}, {"project_id": "p-x", "count": 1}])
    add_meeting(db, "pending-1", ago=0)  # 还在判断归属
    # 需求
    add_requirement(db, "r-p0", "p-yt", "白名单运营后台", "P0", updated_ago=1)
    add_requirement(db, "r-p2", "p-yt", "能耗看板", "P2", updated_ago=2)
    add_requirement(db, "r-stale", "p-yt", "老报表", "P1", updated_ago=40)
    add_requirement(db, "r-done", "p-yt", "已完成的", "P0", status="done")
    add_requirement(db, "r-zt", "p-zt", "中台需求", "P1")
    now = utc_now()
    for requirement_id, meeting_id in (("r-p0", "yt-in-0"), ("r-p2", "yt-mid-0"), ("r-zt", "yt-in-1")):
        db.execute(
            "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
            (requirement_id, meeting_id, now),
        )
    # 任务：待确认 2 条；一条挂在数据中台（跨项目），一条已完成不算
    add_task(db, "t1", meeting_id="yt-in-0", project_id="p-yt")
    add_task(db, "t2", meeting_id="yt-in-1", project_id="p-yt")
    add_task(db, "t3", meeting_id="yt-in-2", project_id="p-zt", status="confirmed")
    add_task(db, "t4", meeting_id="yt-in-2", project_id="p-zt", status="done")
    # 数据中台的一些会
    for index in range(4):
        add_meeting(db, f"zt-{index}", ago=index * 3, project_id="p-zt", origin="ai")


def test_project_graph_places_nodes_by_type_and_age(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    seed_three_projects(db)

    body = build(db, "p-yt")

    assert body["window"] == {"requested": None, "effective": "28d", "days": 28, "widened_reason": None}
    rings = {node["meeting_id"]: node["ring"] for node in body["meetings"]}
    assert rings["yt-in-0"] == "inner" and rings["yt-review"] == "inner"
    assert rings["yt-mid-3"] == "middle"
    assert not any(key.startswith("yt-old") for key in rings)
    # 最新的在最前
    assert body["meetings"][0]["meeting_id"] == "yt-in-0"
    older = next(group for group in body["collapsed"] if group["id"] == "c:older")
    assert older["count"] == 5 and older["label"] == "更早 5 场"
    assert body["project"]["meeting_count"] == 8

    # 门口只收没归项目、候选里有本项目的会；本项目排在候选第一
    assert [item["meeting_id"] for item in body["doorstep"]] == ["door-1"]
    assert body["doorstep"][0]["candidates"][0]["project_id"] == "p-yt"
    assert body["doorstep"][0]["candidates"][1]["project_name"] == "数据中台"

    # 需求：只要进行中的，P0 在前；没动静的漂到外圈
    assert [item["requirement_id"] for item in body["requirements"]] == ["r-p0", "r-stale", "r-p2"]
    stale = next(item for item in body["requirements"] if item["requirement_id"] == "r-stale")
    assert stale["ring"] == "outer" and stale["stale_text"] == "5 周没动静"

    # 线索词：关掉的词不上图；次数按窗口内合计
    assert [(item["text"], item["total"]) for item in body["cues"]] == [("初审规则", 12)]
    assert {edge["to"] for edge in body["edges"] if edge["kind"] == "cue"} == {
        "m:yt-in-0", "m:yt-in-1", "m:yt-in-2"
    }

    by_id = {node["meeting_id"]: node for node in body["meetings"]}
    # 线上的字照当时的证据写，词后来关掉了也不改
    assert by_id["yt-in-0"]["attribution"] == {"label": "自动 · 提到『驻场排班』5 次", "source": "ai"}
    assert by_id["yt-mid-0"]["attribution"]["label"] == "你归的"
    assert by_id["yt-review"]["attribution"]["source"] == "review"
    assert by_id["yt-in-0"]["pending_tasks"] == 1

    # 讨论线只连本项目的会和本项目的需求；跨项目的变成信标
    discussions = {(edge["from"], edge["to"]) for edge in body["edges"] if edge["kind"] == "discussion"}
    assert discussions == {("m:yt-in-0", "r:r-p0"), ("m:yt-mid-0", "r:r-p2")}
    assert [beacon["label"] for beacon in body["beacons"]] == ["→ 数据中台 ×2"]
    kinds = sorted(item["kind"] for item in body["beacons"][0]["items"])
    assert kinds == ["meeting_requirement", "task_elsewhere"]

    status = body["status"]
    assert status["ok"] == []  # 近 7 天有一场待复核，不算都归好了
    texts = [item["text"] for item in status["waiting"]]
    assert texts == ["1 场可能是这个项目的", "1 场归属待复核", "2 条任务待确认"]
    assert status["note"] == "另有 1 场新会还在判断归属"
    assert sum(week["count"] for week in body["weekly"]) == 11  # 100、200 天前的在 12 周以外
    assert body["weekly"][-1]["to"] == TODAY.isoformat()


def test_default_window_widens_when_few_meetings_but_explicit_window_does_not(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_meeting(db, "m-1", ago=3, project_id="p", origin="manual")
    add_meeting(db, "m-2", ago=40, project_id="p", origin="manual")
    add_meeting(db, "m-3", ago=60, project_id="p", origin="manual")

    widened = build(db, "p")
    assert widened["window"]["effective"] == "90d"
    assert widened["window"]["widened_reason"] == "自动放宽到 90 天：28 天内只有 1 场会"
    # 90 天窗口里 28 天以外的会按月成簇
    assert [group["kind"] for group in widened["collapsed"]] == ["month", "month"]

    explicit = build(db, "p", window="28d")
    assert explicit["window"]["effective"] == "28d"
    assert explicit["window"]["widened_reason"] is None

    focused = build(db, "p", window="28d", focus="m:m-3")
    assert focused["window"]["effective"] == "90d"
    assert focused["window"]["widened_reason"] == "放宽到 90 天：要看的会在 28 天以外"


def test_visible_nodes_stay_within_budget_under_pressure(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    for index in range(40):
        add_term(db, f"t-{index}", f"线索词{index:02d}", "p")
    for index in range(30):
        meeting_id = f"m-{index}"
        add_meeting(db, meeting_id, ago=index % 27, project_id="p", origin="ai")
        add_link(db, meeting_id, project_id="p",
                 evidence=[cue("p", f"线索词{index:02d}", 3, term_id=f"t-{index}")])
    for index in range(14):
        add_requirement(db, f"r-{index}", "p", f"需求{index}", f"P{index % 4}", updated_ago=index % 6)
        db.execute(
            "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, ?)",
            (f"r-{index}", f"/材料/云图AI/需求{index}", utc_now()),
        )
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/材料/云图AI', ?)",
        (utc_now(),),
    )

    body = build(db, "p")

    visible = (
        1
        + len(body["meetings"])
        + len(body["collapsed"])
        + len(body["doorstep"])
        + len(body["requirements"])
        + (1 if body["requirements_more"] else 0)
        + len(body["folders"])
        + (1 if body["folders_more"] else 0)
        + (1 if body["loose"] else 0)
        + len(body["cues"])
        + len(body["beacons"])
        # 4f：文件节点（含琥珀色的固定一项）和「更多文件」
        + len(body["files"])
        + (1 if body["files_more"] else 0)
    )
    assert visible <= graph.VISIBLE_BUDGET
    assert len([m for m in body["meetings"] if m["ring"] == "inner"]) == graph.INNER_CAP
    hidden = sum(group["count"] for group in body["collapsed"])
    assert len(body["meetings"]) + hidden == 30


def test_sql_statement_count_does_not_grow_with_meetings(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")

    def count_statements():
        return count_reads(db, lambda connection: graph.project_graph(connection, "p", today=TODAY))

    for index in range(10):
        add_meeting(db, f"m-{index}", ago=index, project_id="p", origin="ai")
        add_link(db, f"m-{index}", project_id="p", evidence=[cue("p", "初审规则", 2)])
    small = count_statements()
    for index in range(10, 200):
        add_meeting(db, f"m-{index}", ago=index % 120, project_id="p" if index % 3 else None)
        add_task(db, f"t-{index}", meeting_id=f"m-{index}", project_id="p")
    # 4f：大的一轮再加上关联行、决议和交付物（⑫ 仍是一条 WITH … UNION ALL）
    root_id = db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/材料/云图AI', ?)",
        (utc_now(),),
    )
    rows = []
    for index in range(10, 60):
        file_id = _file(db, root_id, f"文件{index}.docx", f"k-{index}")
        _decision(db, f"dec-{index}", f"m-{index}", f"决议{index}")
        if index % 3:
            rows.append(_affects(f"dec-{index}", f"m-{index}", file_id, f"k-{index}"))
            db.execute("UPDATE tasks SET status = 'confirmed' WHERE id = ?", (f"t-{index}",))
            rows.append(_produced(f"t-{index}", file_id, f"k-{index}"))
            _deliver(db, f"t-{index}", root_id, f"文件{index}.docx", f"k-{index}")
    _upsert(db, rows)
    large = count_statements()

    assert small == large
    assert large <= 12


def test_graph_endpoint_never_reads_disk(tmp_path, monkeypatch):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/Volumes/睡着的盘/云图AI', ?)",
        (utc_now(),),
    )
    add_meeting(db, "m-1", ago=0, project_id="p", origin="manual", today=date.today())

    def sleepy(_path):
        time.sleep(5)
        return "online"

    monkeypatch.setattr("meeting_workbench.materials.volume_state", sleepy)
    monkeypatch.setattr("meeting_workbench.graph.volume_state", sleepy)
    started = time.monotonic()
    response = client.get("/api/graph/projects/p")
    assert response.status_code == 200
    roots = client.get("/api/graph/projects/p/roots").json()
    assert time.monotonic() - started < 2
    assert roots["roots"][0]["state"] == "checking" and roots["checking"] is True


def test_etag_changes_after_writes_and_across_days(tmp_path):
    client, settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_meeting(db, "m-1", ago=0, project_id="p", origin="manual", today=date.today())

    first = client.get("/api/graph/projects/p")
    etag = first.headers["etag"]
    cached = client.get("/api/graph/projects/p", headers={"If-None-Match": etag})
    assert cached.status_code == 304

    add_task(db, "t-1", meeting_id="m-1", project_id="p")
    after_write = client.get("/api/graph/projects/p", headers={"If-None-Match": etag})
    assert after_write.status_code == 200
    assert after_write.headers["etag"] != etag
    assert after_write.json()["meetings"][0]["pending_tasks"] == 1

    with db.autocommit() as connection:
        today_tag = graph.graph_etag(connection, "p", None, None, TODAY)
        tomorrow_tag = graph.graph_etag(connection, "p", None, None, TODAY + timedelta(days=1))
        other_window = graph.graph_etag(connection, "p", "90d", None, TODAY)
    assert len({today_tag, tomorrow_tag, other_window}) == 3

    assert client.get("/api/graph/projects/nope").status_code == 404
    assert client.get("/api/graph/projects/p", params={"window": "3d"}).status_code == 422


def test_moved_out_meetings_within_undo_window_are_listed(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_meeting(db, "m-1", ago=0, project_id="p", origin="ai", today=date.today())
    headers = write_headers(client)

    moved = client.patch("/api/meetings/m-1", json={"project_id": "q"}, headers=headers)
    assert moved.status_code == 200

    body = client.get("/api/graph/projects/p").json()
    assert [item["meeting_id"] for item in body["moved_out"]] == ["m-1"]
    assert body["moved_out"][0]["to_project_name"] == "数据中台"
    assert client.get("/api/graph/projects/q").json()["meetings"][0]["attribution"]["label"] == "你归的"

    client.post("/api/meetings/m-1/project/undo", json={}, headers=headers)
    assert client.get("/api/graph/projects/p").json()["moved_out"] == []


def test_confirmed_meeting_reads_as_confirmed(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_meeting(db, "m-1", ago=0, project_id="p", origin="ai", today=date.today())
    add_link(db, "m-1", project_id="p", evidence=[cue("p", "初审规则", 6)])
    assert client.get("/api/graph/projects/p").json()["meetings"][0]["attribution"]["label"] == (
        "自动 · 提到『初审规则』6 次"
    )

    client.post("/api/meetings/m-1/project/confirm", json={}, headers=write_headers(client))

    node = client.get("/api/graph/projects/p").json()["meetings"][0]
    assert node["attribution"] == {"label": "你确认过", "source": "confirmed"}


def test_old_rule_meeting_without_evidence_says_so(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_meeting(db, "m-1", ago=0, project_id="p", origin="ai")
    assert build(db, "p")["meetings"][0]["attribution"]["label"] == "AI 归的 · 旧规则，没记原话"


MINUTES = """# 初审规则沟通

## 一分钟摘要

这次定了初审阈值先按 0.8 执行 [00:03:10]，月总牵头对接数理学会。

## 决议

1. 阈值先按 0.8 执行 [00:12:34]
2. **驻场排班**下周起改成两班 `[00:20:00]`

## 待办
- 月总：对接数理学会
"""


def test_meeting_brief_is_small_and_carries_decisions_with_times(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_meeting(
        db, "m-1", ago=0, project_id="p", origin="ai", today=date.today(),
        segments=[(0, "开场"), (5000, "初审规则这次要定下来"), (60000, "长段落" * 400)],
        minutes=MINUTES,
    )
    add_link(db, "m-1", project_id="p", evidence=[cue("p", "初审规则", 6, anchors=(5000,))])
    add_task(db, "t-1", meeting_id="m-1", project_id="p", status="confirmed")
    add_task(db, "t-2", meeting_id="m-1", project_id="p")

    response = client.get("/api/meetings/m-1/brief")
    body = response.json()

    assert len(response.content) < 20_000
    assert body["summary"] == "这次定了初审阈值先按 0.8 执行，月总牵头对接数理学会。"
    # 测试里 links 循环关着，决议当场解析：id 为 null，later 到 4c 才有
    assert body["decisions"] == [
        {"id": None, "text": "阈值先按 0.8 执行", "start_ms": 754_000, "later": None},
        {"id": None, "text": "驻场排班下周起改成两班", "start_ms": 1_200_000, "later": None},
    ]
    assert body["decisions_note"] is None
    assert [task["id"] for task in body["tasks"]] == ["t-2", "t-1"]  # 待确认的排前面
    assert body["evidence_quotes"][0]["quotes"] == [{"start_ms": 5000, "text": "初审规则这次要定下来"}]
    assert body["attribution"]["state"] == "auto"
    assert body["meeting"]["audio_url"] is None
    assert client.get("/api/meetings/nope/brief").status_code == 404


def test_minutes_without_decision_section_says_so_not_that_nothing_was_decided():
    outline = graph.minutes_outline("# 周会\n\n## 会议背景\n\n聊了排期。\n\n### 议题一 排期\n")
    assert outline["decisions"] == []
    assert outline["decisions_note"] == "这场纪要没有决议段"
    assert outline["summary"] == "聊了排期。"


def test_quotes_return_segments_around_each_anchor(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_meeting(
        db, "m-1", ago=0, today=date.today(),
        segments=[(0, "开场"), (10_000, "第二段"), (20_000, "第三段"), (60_000, "很后面")],
    )

    body = client.get("/api/meetings/m-1/quotes", params=[("at", 12_000), ("at", 61_000)]).json()

    assert [[seg["text"] for seg in item["segments"]] for item in body["quotes"]] == [
        ["第二段", "第三段"],
        ["很后面"],
    ]
    assert body["quotes"][0]["segments"][0]["speaker"] is None


def test_cue_term_detail_lists_meetings_and_which_rely_on_it_alone(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_term(db, "t-rule", "初审规则", "p")
    add_meeting(db, "m-alone", ago=1, project_id="p", origin="ai", today=date.today())
    add_link(db, "m-alone", project_id="p", evidence=[cue("p", "初审规则", 4, term_id="t-rule")])
    add_meeting(db, "m-both", ago=0, project_id="p", origin="ai", today=date.today())
    add_link(db, "m-both", project_id="p", evidence=[
        cue("p", "初审规则", 2, term_id="t-rule"), cue("p", "云图", 5, source="name"),
    ])
    add_meeting(db, "m-other", ago=0, project_id="p", origin="manual", today=date.today())

    body = client.get("/api/glossary/terms/t-rule").json()

    assert body["term"] == "初审规则" and body["is_cue"] is True
    assert [(item["meeting_id"], item["count"], item["only_cue"]) for item in body["cue_meetings"]] == [
        ("m-both", 2, False),
        ("m-alone", 4, True),
    ]
    assert client.get("/api/glossary/terms/nope").status_code == 404


def test_requirement_meeting_links_change_by_diff(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_requirement(db, "r-1", "p", "白名单运营后台")
    add_requirement(db, "r-2", "p", "能耗看板")
    add_meeting(db, "m-1", ago=0, project_id="p", origin="manual")
    add_meeting(db, "m-2", ago=0, project_id="p", origin="manual")
    headers = write_headers(client)
    db.execute(
        "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES ('r-1', 'm-1', '2026-01-01')"
    )

    added = client.post("/api/requirements/r-1/meetings/m-2", json={}, headers=headers)
    assert added.status_code == 200
    again = client.post("/api/requirements/r-1/meetings/m-2", json={}, headers=headers)
    assert again.status_code == 200
    client.patch("/api/meetings/m-1", json={"requirement_ids": ["r-1", "r-2"]}, headers=headers)
    client.put("/api/requirements/r-1/meetings", json={"meeting_ids": ["m-1"]}, headers=headers)

    rows = db.query_all(
        "SELECT requirement_id, meeting_id, created_at FROM requirement_meetings ORDER BY requirement_id, meeting_id"
    )
    assert [(row["requirement_id"], row["meeting_id"]) for row in rows] == [("r-1", "m-1"), ("r-2", "m-1")]
    # 没变的那条保留原来的关联时间
    assert rows[0]["created_at"] == "2026-01-01"
    assert client.post("/api/requirements/r-1/meetings/nope", json={}, headers=headers).status_code == 404


def test_roots_cache_reports_disk_state_and_loose_files(tmp_path):
    client, settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    root = tmp_path / "云图AI"
    (root / "子文件夹").mkdir(parents=True)
    (root / "报价单 v3.xlsx").write_bytes(b"x" * 10)
    (root / ".DS_Store").write_bytes(b"x")
    (root / "纪要.docx").write_bytes(b"y")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
        (str(root), utc_now()),
    )
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/Volumes/没插的盘/云图AI', ?)",
        (utc_now(),),
    )
    cache = client.app.state.roots_cache

    cache.refresh()
    body = client.get("/api/graph/projects/p/roots").json()

    states = [item["state"] for item in body["roots"]]
    assert states == ["online", "volume_offline"]
    assert body["loose"]["count"] == 2
    assert sorted(item["name"] for item in body["loose"]["recent"]) == ["报价单 v3.xlsx", "纪要.docx"]
    assert body["checking"] is False


# ---------------------------------------------------------------------- 4f：第四期的线


def _phase4(tmp_path):
    """第四期的样本：项目 p、一个根目录、一场一天前的会 m（test_relations.setup）。延后导入，免得和
    test_relations 互相导入。"""
    from .test_relations import setup

    return setup(tmp_path)


def _file(db, root_id, rel, key=None, day="2026-09-01"):
    from .test_file_mentions import add_file

    file_id = add_file(db, root_id, rel, day=day)
    if key:
        db.execute("UPDATE material_files SET content_key = ? WHERE id = ?", (key, file_id))
    return file_id


def _upsert(db, rows):
    from .test_relations import upsert

    upsert(db, rows)


def _row(**overrides):
    from .test_relations import system_row

    return system_row(**overrides)


def _decision(db, decision_id, meeting_id, text, start_ms=754_000, ordinal=0):
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, start_ms, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (decision_id, meeting_id, ordinal, text, text, start_ms, utc_now(), utc_now()),
    )


def _affects(decision_id, meeting_id, file_id, key):
    return _row(kind="affects", ident=f"{decision_id}|{key}", status="suggested", origin="rule",
                meeting_id=meeting_id, decision_id=decision_id, stem_key=None, file_id=file_id, content_key=key,
                quote="", evidence={"rule": "value", "terms": ["总价"]})


def _produced(task_id, file_id, key, folder="能耗看板/"):
    return _row(kind="produced", ident=f"{task_id}|{key}", status="suggested", origin="rule", meeting_id=None,
                task_id=task_id, stem_key=None, file_id=file_id, content_key=key, quote="",
                evidence={"days": 3, "ref": "meeting", "folder": folder, "event_kind": "added"})


def _deliver(db, task_id, root_id, rel_path, key, created_at=None):
    deliverable_id = db.execute(
        "INSERT INTO deliverables(task_id, kind, url, title, created_at) VALUES (?, 'file', 'x', 'x', ?)",
        (task_id, created_at or utc_now()),
    )
    db.execute(
        "INSERT INTO deliverable_files(deliverable_id, content_key, root_id, rel_path) VALUES (?, ?, ?, ?)",
        (deliverable_id, key, root_id, rel_path),
    )
    return deliverable_id


def _keys(value):
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_project_graph_carries_phase_four_edges(tmp_path):
    from .test_relation_read import literal, loose

    db, root_id = _phase4(tmp_path)
    quote = _file(db, root_id, "报价单.xlsx", "k-q")
    plan = _file(db, root_id, "方案.docx", "k-plan")
    stale = _file(db, root_id, "报价/报价单 v3.xlsx", "k-v3")
    fresh = _file(db, root_id, "能耗看板/能耗看板方案.key", "k-key")
    done = _file(db, root_id, "接口清单.xlsx", "k-api")
    add_meeting(db, "m-2", ago=2, project_id="p")
    literal(db, "m", "报价单", quote, count=3)
    loose(db, "m-2", "方案", plan)
    add_requirement(db, "r-2", "p", "能耗看板")
    add_task(db, "t-19", meeting_id="m", project_id="p", status="confirmed", requirement_id="r-2")
    add_task(db, "t-12", meeting_id="m-2", project_id="p", status="in_progress")
    db.execute("UPDATE tasks SET title = '写一版方案', anchor_ms = 310000, anchor_quote = '写一版方案，周五前给' WHERE id = 't-19'")
    db.execute("UPDATE tasks SET title = '整理接口清单', anchor_ms = 95000, anchor_quote = '接口清单这周整理出来' WHERE id = 't-12'")
    _decision(db, "dec-a", "m", "总价下调 5%")
    _upsert(db, [_affects("dec-a", "m", stale, "k-v3"), _produced("t-19", fresh, "k-key")])
    deliverable_id = _deliver(db, "t-12", root_id, "接口清单.xlsx", "k-api")

    assert count_reads(db, lambda connection: graph.project_graph(connection, "p", today=TODAY)) == 12
    body = build(db, "p")
    edges = {edge["id"]: edge for edge in body["edges"]}
    rid = {row["kind"]: row["id"] for row in db.query_all("SELECT id, kind FROM relations")}
    assert f"e:file:{quote}:m" in edges and f"e:file:{plan}:m-2" in edges
    produced = edges[f"e:prod:{rid['produced']}"]
    affects = edges[f"e:aff:{rid['affects']}"]
    delivered = edges[f"e:dlv:{deliverable_id}"]
    assert (produced["state"], affects["state"], delivered["state"]) == ("ask", "ask", "ok")
    assert produced["from"] == "r:r-2" and produced["relation_ids"] == [rid["produced"]]
    assert produced["label"] == "会后 3 天新增在『能耗看板/』，是任务『写一版方案』的交付物吗？"
    assert (produced["meeting_id"], produced["at_ms"], produced["quote"]) == ("m", 310000, "写一版方案，周五前给")
    assert affects["from"] == "m:m" and affects["label"].endswith("定的『总价下调 5%』，报价单 v3 之后没改过")
    assert delivered["label"] == "任务『整理接口清单』的交付物 · 你标的" and delivered["from"] == "m:m-2"
    assert delivered["to"] == f"file:{done}"
    loose_edge = edges[f"e:file:{plan}:m-2"]
    assert loose_edge["origin"] == "llm" and loose_edge["relation_id"] is not None
    for edge in (produced, affects, delivered, loose_edge):
        assert {"meeting_id", "at_ms", "quote"} <= set(edge)
    assert "score" not in _keys(body)
    files = {item["file_id"]: item for item in body["files"]}
    assert [item["file_id"] for item in body["files"][:2]] == [stale, fresh]
    assert files[stale]["stale"] is True and "asks_deliverable" not in files[stale]
    assert files[fresh]["asks_deliverable"] is True and "stale" not in files[fresh]
    assert "stale" not in files[quote]
    waiting = {item["text"]: item["node_ids"] for item in body["status"]["waiting"]}
    assert waiting["1 个文件可能过时"] == [f"file:{stale}"]
    assert waiting["1 个新文件等你认交付物"] == [f"file:{fresh}"]


def test_loose_mention_shadowing(tmp_path):
    from .test_relation_read import literal, loose

    db, root_id = _phase4(tmp_path)
    quote = _file(db, root_id, "报价单.xlsx")
    other = _file(db, root_id, "报价/报价单 v2.xlsx")
    literal(db, "m", "报价单", quote)
    relation_id = loose(db, "m", "报价单", quote)
    mentioned = [edge for edge in build(db, "p")["edges"] if edge["kind"] == "mentioned"]
    assert [(edge["to"], "origin" in edge) for edge in mentioned] == [(f"file:{quote}", False)]
    db.execute("UPDATE relations SET origin = 'manual', file_id = ? WHERE id = ?", (other, relation_id))
    mentioned = [edge for edge in build(db, "p")["edges"] if edge["kind"] == "mentioned"]
    assert [(edge["to"], edge.get("origin")) for edge in mentioned] == [(f"file:{other}", "manual")]
    db.execute("UPDATE relations SET status = 'rejected' WHERE id = ?", (relation_id,))
    assert [edge for edge in build(db, "p")["edges"] if edge["kind"] == "mentioned"] == []


def test_related_rows_never_reach_the_project_graph(tmp_path):
    db, root_id = _phase4(tmp_path)
    quote = _file(db, root_id, "报价单.xlsx", "k-q")
    with db.autocommit() as connection:
        before = graph.graph_etag(connection, "p", None, None, TODAY)
    now = utc_now()
    with db.transaction() as connection:
        connection.executemany(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, content_key, file_id, quote,
               evidence_json, score, created_at, updated_at)
           VALUES ('related', 'p', ?, 'shown', 'vector', 'm', 'k-q', ?, '', '{}', 0.9, ?, ?)""",
        [(f"m|k-{index}", quote, now, now) for index in range(1000)],
    )
    body = build(db, "p")
    assert "related" not in {edge["kind"] for edge in body["edges"]}
    assert body["files"] == []
    with db.autocommit() as connection:
        assert graph.graph_etag(connection, "p", None, None, TODAY) == before


def test_produced_proxy_end(tmp_path):
    db, root_id = _phase4(tmp_path)
    add_requirement(db, "r-shown", "p", "能耗看板", "P0")
    for index in range(graph.REQUIREMENT_CAP):
        add_requirement(db, f"r-{index}", "p", f"需求{index}", "P0")
    add_requirement(db, "r-folded", "p", "老报表", "P3", updated_ago=60)
    add_meeting(db, "m-old", ago=200, project_id="p")
    tasks = {
        "t-req": dict(meeting_id="m", requirement_id="r-shown"),
        "t-fold": dict(meeting_id="m", requirement_id="r-folded"),
        "t-meet": dict(meeting_id="m", requirement_id=None),
        "t-old": dict(meeting_id="m-old", requirement_id=None),
        "t-none": dict(meeting_id=None, requirement_id=None),
    }
    rows = []
    files = {}
    for task_id, extra in tasks.items():
        add_task(db, task_id, project_id="p", status="confirmed", **extra)
        files[task_id] = _file(db, root_id, f"{task_id}.docx", f"k-{task_id}")
        rows.append(_produced(task_id, files[task_id], f"k-{task_id}"))
    _upsert(db, rows)
    body = build(db, "p", window="28d")
    ends = {edge["task_id"]: edge for edge in body["edges"] if edge["kind"] == "produced"}
    assert ends["t-req"]["from"] == "r:r-shown"
    assert ends["t-fold"]["from"] == "r:more"
    assert ends["t-meet"]["from"] == "m:m" and ends["t-meet"]["meeting_id"] == "m"
    older = next(group for group in body["collapsed"] if "m-old" in group["meeting_ids"])
    assert ends["t-old"]["from"] == older["id"] and ends["t-old"]["meeting_id"] == "m-old"
    assert "t-none" not in ends
    shown = {item["file_id"]: item for item in body["files"]}
    assert shown[files["t-none"]]["asks_deliverable"] is True


def test_amber_files_always_shown_up_to_cap(tmp_path):
    from .test_relation_read import literal

    db, root_id = _phase4(tmp_path)
    for index in range(40):
        add_term(db, f"t-{index}", f"线索词{index:02d}", "p")
    for index in range(30):
        meeting_id = f"m-{index}"
        add_meeting(db, meeting_id, ago=index % 27, project_id="p", origin="ai")
        add_link(db, meeting_id, project_id="p", evidence=[cue("p", f"线索词{index:02d}", 3, term_id=f"t-{index}")])
    for index in range(14):
        add_requirement(db, f"r-{index}", "p", f"需求{index}", f"P{index % 4}", updated_ago=index % 6)
    rows = []
    for index in range(10):
        file_id = _file(db, root_id, f"过时/报价{index:02d}.xlsx", f"k-s{index}")
        _decision(db, f"dec-{index}", "m", f"决议{index}", start_ms=index * 60_000, ordinal=index)
        rows.append(_affects(f"dec-{index}", "m", file_id, f"k-s{index}"))
    _upsert(db, rows)
    for index in range(12):
        file_id = _file(db, root_id, f"提到/文件{index:02d}.docx")
        literal(db, f"m-{index}", f"文件{index:02d}", file_id, count=5)
    body = build(db, "p")
    stale_ids = [
        int(row["file_id"]) for row in db.query_all(
            "SELECT r.file_id FROM relations r JOIN decisions d ON d.id = r.decision_id ORDER BY d.start_ms DESC"
        )
    ]
    assert [item["file_id"] for item in body["files"][:graph.AMBER_FILE_CAP]] == stale_ids[:8]
    assert all(item["stale"] for item in body["files"][:8])
    assert body["files_more"]["file_ids"][:2] == stale_ids[8:]
    waiting = {item["text"]: item["node_ids"] for item in body["status"]["waiting"]}
    assert waiting["10 个文件可能过时"] == [f"file:{file_id}" for file_id in stale_ids]
    visible = (
        1 + len(body["meetings"]) + len(body["collapsed"]) + len(body["doorstep"]) + len(body["requirements"])
        + (1 if body["requirements_more"] else 0) + len(body["folders"]) + (1 if body["folders_more"] else 0)
        + (1 if body["loose"] else 0) + len(body["cues"]) + len(body["beacons"])
        + len(body["files"]) + (1 if body["files_more"] else 0)
    )
    assert visible <= graph.VISIBLE_BUDGET
    assert len(body["files"]) <= graph.FILE_CAP + graph.AMBER_FILE_CAP


def test_deliverable_edges_cap_and_moves(tmp_path):
    db, root_id = _phase4(tmp_path)
    add_task(db, "t", meeting_id="m", project_id="p", status="in_progress")
    ids = []
    for index in range(25):
        _file(db, root_id, f"交付/清单{index:02d}.xlsx", f"k-{index}")
        ids.append(_deliver(db, "t", root_id, f"交付/清单{index:02d}.xlsx", f"k-{index}", created_at=f"2026-09-2{index % 5}T0{index % 10}:00:00+00:00"))
    edges = [edge for edge in build(db, "p")["edges"] if edge["kind"] == "deliverable"]
    assert len(edges) == graph.DELIVERABLE_EDGE_CAP
    target = edges[0]
    old_id = target["file_id"]
    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), old_id))
    key = db.query_one("SELECT content_key FROM material_files WHERE id = ?", (old_id,))["content_key"]
    new_id = _file(db, root_id, "挪过去/清单.xlsx", key)
    moved = {edge["id"]: edge for edge in build(db, "p")["edges"] if edge["kind"] == "deliverable"}
    assert moved[target["id"]]["to"] == f"file:{new_id}"
    # 任务做完了：只留在时间窗里登记的
    db.execute("UPDATE tasks SET status = 'done' WHERE id = 't'")
    db.execute("UPDATE deliverables SET created_at = '2025-01-01T00:00:00+00:00' WHERE id != ?", (ids[0],))
    db.execute("UPDATE deliverables SET created_at = ? WHERE id = ?", (f"{TODAY.isoformat()}T01:00:00+00:00", ids[0]))
    assert [edge["id"] for edge in build(db, "p", window="28d")["edges"] if edge["kind"] == "deliverable"] == [f"e:dlv:{ids[0]}"]


def test_affects_one_line_per_file_newest_decision(tmp_path):
    db, root_id = _phase4(tmp_path)
    add_meeting(db, "m-old", ago=6, project_id="p")
    stale = _file(db, root_id, "报价单 v3.xlsx", "k-v3")
    _decision(db, "dec-old", "m-old", "总价下调 3%")
    _decision(db, "dec-new", "m", "总价下调 5%")
    _decision(db, "dec-gone", "m", "总价不变", ordinal=1, start_ms=900_000)
    _upsert(db, [_affects("dec-old", "m-old", stale, "k-v3"), _affects("dec-new", "m", stale, "k-v3"),
                 _affects("dec-gone", "m", stale, "k-v3")])
    db.execute("UPDATE decisions SET gone_at = ? WHERE id = 'dec-gone'", (utc_now(),))
    body = build(db, "p")
    [line] = [edge for edge in body["edges"] if edge["kind"] == "affects"]
    assert line["decision_id"] == "dec-new" and line["from"] == "m:m"
    assert "『总价下调 5%』" in line["label"]
    waiting = {item["text"] for item in body["status"]["waiting"]}
    assert "1 个文件可能过时" in waiting


def test_phase_four_labels_vocabulary(tmp_path):
    from .test_copy_vocabulary import problems
    from .test_relation_questions import world

    w = world(tmp_path)
    body = w.client.get("/api/graph/projects/p").json()
    labels = [edge["label"] for edge in body["edges"] if edge["kind"] in ("produced", "affects", "deliverable", "mentioned")]
    labels += [item["text"] for item in body["status"]["waiting"]]
    assert any(edge["kind"] == "affects" for edge in body["edges"])
    assert [label for label in labels if problems(label)] == []
    for sample in (
        graph.affects_label("总价下调 5%", "报价单 v3.xlsx", TODAY, TODAY),
        graph.produced_label('{"days": 3, "folder": "能耗看板/"}', "写一版方案"),
        graph.deliverable_label("整理接口清单"),
    ):
        assert problems(sample) == []


def test_project_graph_timing_with_all_kinds(tmp_path):
    """200 场会、2 万个文件、四类线都有：语句数正好 12 是硬断言；p95 超过 80 毫秒只提醒（Mac 上看 M4）。"""
    import warnings

    from .test_relation_read import literal

    db, root_id = _phase4(tmp_path)
    now = utc_now()
    with db.transaction() as connection:
        connection.executemany(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone,
               seen_at, content_key)
           VALUES (?, ?, ?, ?, ?, ?, 'xlsx', 1, ?, 'normal', ?, ?)""",
        [(root_id, f"d{index % 50}/文件{index}.xlsx", f"d{index % 50}", f"文件{index}.xlsx", f"文件{index}",
          f"文件{index}", 1_700_000_000_000_000_000 + index, now, f"k-{index}") for index in range(20_000)],
    )
    add_requirement(db, "r", "p", "能耗看板")
    rows = []
    for index in range(200):
        meeting_id = f"m-{index}"
        add_meeting(db, meeting_id, ago=index % 150, project_id="p")
        literal(db, meeting_id, f"文件{index}", index + 1, count=2)
        _decision(db, f"dec-{index}", meeting_id, f"决议{index}")
        if index % 4 == 0:
            add_task(db, f"t-{index}", meeting_id=meeting_id, project_id="p", status="confirmed", requirement_id="r")
            rows.append(_produced(f"t-{index}", index + 300, f"k-{index + 299}"))
            _deliver(db, f"t-{index}", root_id, f"d{(index + 600) % 50}/文件{index + 600}.xlsx", f"k-{index + 600}")
        if index % 3 == 0:
            rows.append(_affects(f"dec-{index}", meeting_id, index + 1000, f"k-{index + 999}"))
        rows.append(_row(ident=f"{meeting_id}|松{index}", meeting_id=meeting_id, stem_key=f"松{index}",
                         file_id=index + 2000, content_key=f"k-{index + 1999}"))
    _upsert(db, rows)
    assert count_reads(db, lambda connection: graph.project_graph(connection, "p", today=TODAY)) == 12
    body = build(db, "p")
    kinds = {edge["kind"] for edge in body["edges"]}
    assert {"mentioned", "produced", "affects", "deliverable"} <= kinds
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        build(db, "p")
        timings.append((time.perf_counter() - started) * 1000)
    p95 = sorted(timings)[18]
    if p95 > 80:
        warnings.warn(f"project_graph p95 {p95:.1f} 毫秒，超过 80 毫秒（Mac 上再看 M4）", stacklevel=1)
