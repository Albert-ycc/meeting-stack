"""1g 关系图：整张图的节点、连线、状态句；SQL 条数固定；不读盘；ETag；面板用的小接口。"""
import json
import time
from datetime import UTC, date, datetime, timedelta


from meeting_workbench import graph
from meeting_workbench.db import Database, utc_now

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
        statements = []
        with db.autocommit() as connection:
            connection.set_trace_callback(
                lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
            )
            graph.project_graph(connection, "p", today=TODAY)
        return len(statements)

    for index in range(10):
        add_meeting(db, f"m-{index}", ago=index, project_id="p", origin="ai")
        add_link(db, f"m-{index}", project_id="p", evidence=[cue("p", "初审规则", 2)])
    small = count_statements()
    for index in range(10, 200):
        add_meeting(db, f"m-{index}", ago=index % 120, project_id="p" if index % 3 else None)
        add_task(db, f"t-{index}", meeting_id=f"m-{index}", project_id="p")
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
    assert body["decisions"] == [
        {"text": "阈值先按 0.8 执行", "start_ms": 754_000},
        {"text": "驻场排班下周起改成两班", "start_ms": 1_200_000},
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
