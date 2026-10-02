"""第四期 4f：以文件为中心的局部图（/map）和来龙去脉（/trace）：按内容标识找活文件、邻居的顺序和上限、
语句数、相关只在打开时给；来龙去脉时间单调、每边最多 3 步、级别、包含关系不算步、不走相关和在问的建议；
错误码、只有 GET、会议和决议节点的 audio_url。"""

from datetime import date

from meeting_workbench import graph, graph_local
from meeting_workbench.db import Database, utc_now

from .helpers import count_reads
from .test_file_mentions import add_file
from .test_graph import TODAY, add_meeting, add_requirement, add_task, stop_clock
from .test_relation_read import literal
from .test_relations import keyed, setup, system_row, upsert
from .test_tasks_api import make_client, write_headers


def read(db, fn, *args, **kwargs):
    with db.autocommit() as connection:
        return fn(connection, *args, **kwargs)


def file_map(db, file_id, **kwargs):
    kwargs.setdefault("today", TODAY)
    return read(db, graph_local.file_map, file_id, **kwargs)


def trace(db, node):
    return read(db, graph_local.trace, node, today=TODAY)


def gone(db, file_id):
    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), file_id))


def add_decision(db, decision_id, meeting_id, text, start_ms=754_000):
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, start_ms, created_at, updated_at)
           VALUES (?, ?, (SELECT COUNT(*) FROM decisions WHERE meeting_id = ?), ?, ?, ?, ?, ?)""",
        (decision_id, meeting_id, meeting_id, text, text, start_ms, utc_now(), utc_now()),
    )


def affects(decision_id, meeting_id, file_id, key, *, status="suggested"):
    return system_row(
        kind="affects",
        ident=f"{decision_id}|{key}",
        status=status,
        origin="rule",
        meeting_id=meeting_id,
        decision_id=decision_id,
        stem_key=None,
        file_id=file_id,
        content_key=key,
        quote="总价下调 5%",
        evidence={"rule": "value", "terms": ["总价"]},
    )


def deliver(db, task_id, root_id, rel_path, key):
    deliverable_id = db.execute(
        "INSERT INTO deliverables(task_id, kind, url, title, created_at) VALUES (?, 'file', 'x', 'x', ?)",
        (task_id, utc_now()),
    )
    db.execute(
        "INSERT INTO deliverable_files(deliverable_id, content_key, root_id, rel_path) VALUES (?, ?, ?, ?)",
        (deliverable_id, key, root_id, rel_path),
    )
    return deliverable_id


# ---------------------------------------------------------------------- 局部图


def test_moved_file_is_found_by_content_and_keeps_its_lines(tmp_path):
    db, root_id = setup(tmp_path)
    old = add_file(db, root_id, "报价/报价单 v3.xlsx")
    keyed(db, old, "k-quote")
    literal(db, "m", "报价单", old)
    before = file_map(db, old)
    assert before["center"]["file_id"] == old and "moved_from" not in before["center"]
    assert [edge["id"] for edge in before["edges"]] == [f"e:file:{old}:m"]

    gone(db, old)
    new = add_file(db, root_id, "2026/报价单 v3.xlsx")
    keyed(db, new, "k-quote")
    moved = file_map(db, old)
    assert moved["center"]["file_id"] == new and moved["center"]["moved_from"] == old
    assert moved["center"]["folder"] == "2026" and moved["center"]["gone"] is False
    assert [(edge["id"], edge["from"], edge["to"]) for edge in moved["edges"]] == [
        (f"e:file:{new}:m", "m:m", f"file:{new}")
    ]
    assert file_map(db, new)["edges"] == moved["edges"]


def test_copies_count_on_the_center_and_gone_files_still_draw(tmp_path):
    db, root_id = setup(tmp_path)
    first = add_file(db, root_id, "报价单.xlsx")
    copy = add_file(db, root_id, "备份/报价单.xlsx")
    keyed(db, first, "k")
    keyed(db, copy, "k")
    literal(db, "m", "报价单", copy)
    body = file_map(db, first)
    assert body["center"]["copies"] == [{"file_id": copy, "name": "报价单.xlsx"}]
    assert [edge["from"] for edge in body["edges"]] == ["m:m"]
    assert "rel_path" not in body["center"]["copies"][0]

    lonely = add_file(db, root_id, "方案.docx")
    literal(db, "m", "方案", lonely)
    gone(db, lonely)
    body = file_map(db, lonely)
    assert body["center"]["gone"] is True and body["center"]["file_id"] == lonely
    assert [edge["id"] for edge in body["edges"]] == [f"e:file:{lonely}:m"]


def test_meeting_captions_follow_the_today_passed_in(tmp_path):
    """会议节点的说明和决议、任务一样按传进来的 today 决定带不带年份。"""
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    keyed(db, quote, "k")
    db.execute("UPDATE meetings SET recording_date = '2026-09-20', title = '周会' WHERE id = 'm'")
    literal(db, "m", "报价单", quote)

    captions = [
        next(node for node in file_map(db, quote, today=today)["nodes"] if node["id"] == "m:m")[
            "caption"
        ]
        for today in (date(2026, 9, 26), date(2027, 1, 2))
    ]

    assert captions == ["9/20 周会", "2026/9/20 周会"]


def test_asks_come_first_twelve_are_drawn_and_the_rest_hidden(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    keyed(db, quote, "k")
    for index in range(20):
        add_meeting(db, f"m-{index:02d}", ago=index + 2, project_id="p", title=f"周会{index}")
        literal(db, f"m-{index:02d}", "报价单", quote, count=2)
    add_decision(db, "dec-a", "m-00", "总价下调 5%")
    add_decision(db, "dec-b", "m-19", "总价下调 3%")
    upsert(db, [affects("dec-a", "m-00", quote, "k"), affects("dec-b", "m-19", quote, "k")])

    body = file_map(db, quote)
    ids = [node["id"] for node in body["nodes"]]
    assert ids[:2] == ["dec:dec-a", "dec:dec-b"]
    assert len(ids) == graph_local.MAP_NEIGHBOURS and body["hidden_count"] == 10
    assert len(body["hidden"]) == 10
    assert all(
        row["label"].startswith("会上说『报价单』2 次") and "周会" in row["node_label"]
        for row in body["hidden"]
    )
    # 没画出来的行带着节点本身和那条线：面板里点了直接打开它的面板
    row = body["hidden"][0]
    assert row["node"]["id"] == row["node_id"] and row["node"]["kind"] == "meeting"
    assert (
        row["node"]["at"]
        and "audio_url" in row["node"]
        and row["node"]["caption"] == row["node_label"]
    )
    assert row["edge"]["id"] == row["edge_id"] and row["edge"]["kind"] == "mentioned"
    assert body["center"]["stale"] is True
    aff = next(edge for edge in body["edges"] if edge["id"].startswith("e:aff:"))
    assert aff["state"] == "ask" and aff["label"].endswith("报价单 之后没改过")
    # 会议到决议：两端都画出来才画（m-00 最新，画了；m-19 最旧，没画）
    assert [edge["id"] for edge in body["edges"] if edge["kind"] == "in_meeting"] == ["e:in:dec-a"]
    decision = next(node for node in body["nodes"] if node["id"] == "dec:dec-b")
    assert decision["meeting_caption"].endswith("周会19") and decision["start_ms"] == 754_000
    assert count_reads(db, lambda connection: graph_local.file_map(connection, quote)) <= 12
    # 字面提到的原话从当前逐字稿取（这里没有逐字稿，是空串，但键在）
    assert all("quote" in edge for edge in body["edges"] if edge["kind"] == "mentioned")


def test_map_neighbour_kinds_and_related_only_when_on(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "能耗看板/报价单 v3.xlsx", day="2026-09-18")
    keyed(db, quote, "k3")
    older = add_file(db, root_id, "旧/报价单 v2.xlsx", day="2026-09-09")
    keyed(db, older, "k2")
    add_requirement(db, "r-2", "p", "能耗看板")
    db.execute(
        "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES ('r-2', ?, ?)",
        (str(tmp_path / "云图AI" / "能耗看板"), utc_now()),
    )
    add_meeting(db, "m-2", ago=3, project_id="p", title="报价沟通")
    add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    deliver(db, "t", root_id, "能耗看板/报价单 v3.xlsx", "k3")
    upsert(
        db,
        [
            system_row(
                kind="related",
                ident="m-2|k3",
                origin="vector",
                meeting_id="m-2",
                stem_key=None,
                file_id=quote,
                content_key="k3",
                quote="报价单再核一下",
                evidence={
                    "words": ["报价单", "驻场"],
                    "windows": 2,
                    "material": {"content_key": "k3", "ordinal": 0, "loc": "第 1 页"},
                },
            )
        ],
    )
    db.execute(
        "INSERT INTO material_contents(content_key, layer, state, created_at, updated_at) VALUES ('k3', 'text', 'done', ?, ?)",
        (utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, loc, text) VALUES ('k3', 0, '第 1 页', '材料正文不出')"
    )
    body = file_map(db, quote)
    kinds = {edge["kind"] for edge in body["edges"]}
    assert kinds == {"deliverable", "same_name", "belongs"}
    same = next(edge for edge in body["edges"] if edge["kind"] == "same_name")
    assert same["label"] == "同属『报价单』" and same["from"] == f"file:{older}"
    belongs = next(edge for edge in body["edges"] if edge["kind"] == "belongs")
    assert belongs["label"] == "同属需求『能耗看板』的文件夹" and belongs["from"] == "r:r-2"
    with_related = file_map(db, quote, related_on=True)
    [rel] = [edge for edge in with_related["edges"] if edge["kind"] == "related"]
    assert rel["id"].startswith("e:rel:") and rel["label"] == "共同词：报价单、驻场"
    assert rel["passage"]["loc"] == "第 1 页" and "text" not in rel["passage"]
    assert "材料正文不出" not in str(with_related)


def test_generic_file_name_does_not_pair_as_same_name(tmp_path):
    """D7：PRD.md、README.md 这类通用文件名，别的文件夹下的同名文件不是「同一份的别的版本」，
    不该连 same_name（也不进局部图的同名邻居）。真正的词干（报价单）还是照样连，见上一个用例。"""
    db, root_id = setup(tmp_path)
    prd_a = add_file(db, root_id, "260910-赠药套利与发票风控/N2/PRD.md")
    keyed(db, prd_a, "k-prd-a")
    prd_b = add_file(db, root_id, "别的需求/PRD.md")
    keyed(db, prd_b, "k-prd-b")
    readme_a = add_file(db, root_id, "项目A/README.md")
    keyed(db, readme_a, "k-readme-a")
    readme_b = add_file(db, root_id, "项目B/README.md")
    keyed(db, readme_b, "k-readme-b")
    literal(db, "m", "赠药套利", prd_a)
    literal(db, "m", "项目A", readme_a)
    for center in (prd_a, readme_a):
        body = file_map(db, center)
        assert "same_name" not in {edge["kind"] for edge in body["edges"]}


def test_meeting_and_decision_nodes_carry_audio_url(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    keyed(db, quote, "k")
    add_meeting(db, "m-2", ago=3, project_id="p")
    literal(db, "m", "报价单", quote)
    literal(db, "m-2", "报价单", quote)
    add_decision(db, "dec-a", "m", "总价下调 5%")
    upsert(db, [affects("dec-a", "m", quote, "k")])
    audio_id = db.execute(
        """INSERT INTO artifacts(meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('m', 'audio', 'source', 'archive', '/a/m.m4a', 1, 1, ?)""",
        (utc_now(),),
    )
    body = file_map(db, quote)
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes["m:m"]["audio_url"] == f"/api/media/{audio_id}"
    assert nodes["dec:dec-a"]["audio_url"] == f"/api/media/{audio_id}"
    assert nodes["m:m-2"]["audio_url"] is None
    assert "e:in:dec-a" in {edge["id"] for edge in body["edges"]}


# ---------------------------------------------------------------------- 来龙去脉


def chain_world(tmp_path):
    """f3（9/18）是中心：往前有你标的交付物（任务 t 在 9/16 的会上）和更近的字面提到（9/17 的会）；
    往后是一串字面提到：f3 → m-c1 → g1 → m-c2 → g2 → m-c3。"""
    db, root_id = setup(tmp_path)
    f3 = add_file(db, root_id, "报价单 v3.xlsx", day="2026-09-18")
    keyed(db, f3, "k3")
    f2 = add_file(db, root_id, "旧/报价单 v2.xlsx", day="2026-09-09")
    keyed(db, f2, "k2")
    add_meeting(db, "m-b", ago=10, project_id="p", title="周会")  # 9/16
    add_meeting(db, "m-x", ago=9, project_id="p", title="闲聊")  # 9/17
    add_task(db, "t", meeting_id="m-b", project_id="p", status="confirmed")
    db.execute("UPDATE tasks SET anchor_ms = 310000 WHERE id = 't'")
    deliver(db, "t", root_id, "报价单 v3.xlsx", "k3")
    literal(db, "m-x", "报价单", f3)
    literal(db, "m-b", "报价单", f2)
    g1 = add_file(db, root_id, "接口清单.xlsx", day="2026-09-20")
    g2 = add_file(db, root_id, "驻场计划.docx", day="2026-09-22")
    add_meeting(db, "m-c1", ago=7, project_id="p")  # 9/19
    add_meeting(db, "m-c2", ago=5, project_id="p")  # 9/21
    add_meeting(db, "m-c3", ago=3, project_id="p")  # 9/23
    literal(db, "m-c1", "报价单", f3)
    literal(db, "m-c1", "接口清单", g1)
    literal(db, "m-c2", "接口清单", g1)
    literal(db, "m-c2", "驻场计划", g2)
    literal(db, "m-c3", "驻场计划", g2)
    return db, root_id, f3, f2, g1, g2


def test_trace_is_monotonic_capped_and_prefers_your_deliverable(tmp_path):
    db, _root_id, f3, f2, g1, _g2 = chain_world(tmp_path)
    body = trace(db, f"file:{f3}")
    chain = body["chain"]
    assert chain == [
        f"file:{f2}",
        "m:m-b",
        "task:t",
        f"file:{f3}",
        "m:m-c1",
        f"file:{g1}",
        "m:m-c2",
    ]
    assert body["center_index"] == 3
    assert body["cut"] == {"back": False, "forward": True}
    nodes = {node["id"]: node for node in body["nodes"]}
    nodes[f"file:{f3}"] = body["center"]
    moments = [nodes[node_id]["at"] for node_id in chain]
    assert moments == sorted(moments) and len(set(moments)) == len(moments)
    chain_edges = [edge for edge in body["edges"] if edge["on_chain"]]
    assert {edge["kind"] for edge in chain_edges} == {"mentioned", "task_from", "deliverable"}
    # 包含关系（会议到任务）不算步：往前只有交付物和提到两个算数的步
    assert len(chain) <= graph_local.TRACE_NODES
    assert count_reads(db, lambda connection: graph_local.trace(connection, f"file:{f3}")) <= 48


def test_trace_skips_related_asks_and_requirements(tmp_path):
    db, root_id, f3, *_ = chain_world(tmp_path)
    add_meeting(db, "m-r", ago=8, project_id="p")
    add_task(db, "t-ask", meeting_id="m-r", project_id="p", status="confirmed")
    add_decision(db, "dec-a", "m-r", "总价下调 5%")
    upsert(
        db,
        [
            system_row(
                kind="related",
                ident="m-r|k3",
                origin="vector",
                meeting_id="m-r",
                stem_key=None,
                file_id=f3,
                content_key="k3",
            ),
            system_row(
                kind="produced",
                ident="t-ask|k3",
                status="suggested",
                origin="rule",
                meeting_id=None,
                task_id="t-ask",
                stem_key=None,
                file_id=f3,
                content_key="k3",
            ),
            affects("dec-a", "m-r", f3, "k3"),
        ],
    )
    body = trace(db, f"file:{f3}")
    assert not {"m:m-r", "task:t-ask", "dec:dec-a"} & set(body["chain"])
    assert not {edge["kind"] for edge in body["edges"]} & {"related", "produced", "belongs"}
    assert all(not node_id.startswith("r:") for node_id in body["chain"])


def test_trace_from_a_meeting_and_a_decision(tmp_path):
    db, _root_id, f3, *_ = chain_world(tmp_path)
    add_decision(db, "dec-a", "m-c1", "总价下调 5%", start_ms=60_000)
    add_decision(db, "dec-b", "m-c3", "总价下调 3%", start_ms=60_000)
    upsert(
        db,
        [
            system_row(
                kind="later_changed",
                ident="dec-a|dec-b",
                origin="rule",
                meeting_id="m-c3",
                decision_id="dec-b",
                to_decision_id="dec-a",
                stem_key=None,
            )
        ],
    )
    body = trace(db, "dec:dec-a")
    assert body["chain"][body["center_index"]] == "dec:dec-a"
    assert "dec:dec-b" in body["chain"]
    later = next(edge for edge in body["edges"] if edge["kind"] == "later_changed")
    assert (later["from"], later["to"], later["label"]) == ("dec:dec-a", "dec:dec-b", "后来改了")
    meeting = trace(db, "m:m-c1")
    assert meeting["center"]["id"] == "m:m-c1" and meeting["center"]["kind"] == "meeting"
    assert "audio_url" in meeting["center"]


def test_trace_is_not_cut_short_by_a_decision_with_nothing_after_it(tmp_path):
    """会上一条没有下文的决议（包含关系、级 1、时间差最小）不能把往后的方向截断。"""
    db, _root_id, f3, _f2, g1, _g2 = chain_world(tmp_path)
    add_decision(db, "dec-x", "m-c1", "先这样", start_ms=30_000)
    add_task(db, "t-x", meeting_id="m-c1", project_id="p", status="confirmed")
    db.execute("UPDATE tasks SET anchor_ms = 40000 WHERE id = 't-x'")
    body = trace(db, f"file:{f3}")
    assert body["chain"][body["center_index"] :] == [f"file:{f3}", "m:m-c1", f"file:{g1}", "m:m-c2"]
    assert body["cut"] == {"back": False, "forward": True}
    assert count_reads(db, lambda connection: graph_local.trace(connection, f"file:{f3}")) <= 48

    # 包含关系还能接着走时照样走：会上的决议后来被改了
    add_decision(db, "dec-a", "m-c1", "总价下调 5%", start_ms=60_000)
    add_decision(db, "dec-b", "m-c3", "总价下调 3%", start_ms=60_000)
    upsert(
        db,
        [
            system_row(
                kind="later_changed",
                ident="dec-a|dec-b",
                origin="rule",
                meeting_id="m-c3",
                decision_id="dec-b",
                to_decision_id="dec-a",
                stem_key=None,
            )
        ],
    )
    meeting = trace(db, "m:m-c1")
    forward = meeting["chain"][meeting["center_index"] :]
    assert forward[:3] == ["m:m-c1", "dec:dec-a", "dec:dec-b"]
    assert count_reads(db, lambda connection: graph_local.trace(connection, "m:m-c1")) <= 48


def test_trace_dead_end_lookahead_stays_within_the_statement_budget(tmp_path):
    """每场会都挂一堆没有下文的决议和任务：多看一眼的展开有上限，语句总数仍 ≤ 48。"""
    db, _root_id, f3, *_ = chain_world(tmp_path)
    for meeting_id in ("m-b", "m-x", "m-c1", "m-c2", "m-c3"):
        for index in range(6):
            add_decision(
                db, f"dec-{meeting_id}-{index}", meeting_id, f"决议{index}", start_ms=1_000 + index
            )
            add_task(
                db,
                f"t-{meeting_id}-{index}",
                meeting_id=meeting_id,
                project_id="p",
                status="confirmed",
            )
    body = trace(db, f"file:{f3}")
    assert f"file:{f3}" in body["chain"] and "m:m-c1" in body["chain"]
    assert count_reads(db, lambda connection: graph_local.trace(connection, f"file:{f3}")) <= 48
    assert count_reads(db, lambda connection: graph_local.trace(connection, "m:m-c2")) <= 48


def test_trace_of_a_moved_or_gone_file_keeps_the_lines_on_its_old_id(tmp_path):
    db, root_id = setup(tmp_path)
    add_meeting(db, "m-a", ago=5, project_id="p")
    old = add_file(db, root_id, "报价/报价单 v3.xlsx", day="2026-09-18")
    keyed(db, old, "k-quote")
    literal(db, "m-a", "报价单", old)
    gone(db, old)
    new = add_file(db, root_id, "2026/报价单 v3.xlsx", day="2026-09-18")
    keyed(db, new, "k-quote")
    for node in (f"file:{old}", f"file:{new}"):
        body = trace(db, node)
        assert body["center"]["file_id"] == new
        assert body["chain"] == [f"file:{new}", "m:m-a"]
        edge = next(edge for edge in body["edges"] if edge["kind"] == "mentioned")
        assert (edge["id"], edge["to"]) == (f"e:file:{new}:m-a", f"file:{new}")

    # 找不到活文件：照样按同内容那一组读，画它还在时的线
    gone(db, new)
    body = trace(db, f"file:{old}")
    assert body["center"]["gone"] is True
    assert "m:m-a" in body["chain"]


def test_endpoints_are_get_only_and_speak_plainly(tmp_path, monkeypatch):
    today = stop_clock(monkeypatch)
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    root_id = db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
        (str(tmp_path / "云图AI"), utc_now()),
    )
    add_meeting(db, "m", ago=1, project_id="p", today=today)
    add_meeting(db, "m-free", ago=1, today=today)
    quote = add_file(db, root_id, "报价单.xlsx")
    keyed(db, quote, "k")
    literal(db, "m", "报价单", quote)
    add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    upsert(
        db,
        [
            system_row(
                kind="produced",
                ident="t|k",
                status="suggested",
                origin="rule",
                meeting_id=None,
                task_id="t",
                stem_key=None,
                file_id=quote,
                content_key="k",
            )
        ],
    )

    response = client.get(f"/api/graph/files/{quote}/map")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert response.json()["center"]["asks_deliverable"] is True
    assert client.get(f"/api/graph/files/{quote}/map", params={"related": 2}).status_code == 422
    assert client.get("/api/graph/files/999999/map").json()["detail"] == "这份文件不在索引里了"
    assert client.get("/api/graph/trace", params={"node": "foo"}).status_code == 422
    assert (
        client.get("/api/graph/trace", params={"node": "m:nope"}).json()["detail"] == "会议不存在"
    )
    free = client.get("/api/graph/trace", params={"node": "m:m-free"})
    assert (free.status_code, free.json()["detail"]) == (409, "这场会没归项目")
    assert (
        client.get("/api/graph/trace", params={"node": "dec:dec-x"}).json()["detail"]
        == "这条决议已经不在了"
    )
    assert (
        client.get("/api/graph/trace", params={"node": "task:nope"}).json()["detail"]
        == "这条任务已经不在了"
    )
    assert client.get("/api/graph/trace", params={"node": f"file:{quote}"}).status_code == 200
    headers = write_headers(client)
    assert client.post(f"/api/graph/files/{quote}/map", json={}, headers=headers).status_code == 405
    assert (
        client.post(
            "/api/graph/trace", params={"node": "m:m"}, json={}, headers=headers
        ).status_code
        == 405
    )

    # 回答再撤销：星图的 ETag 变两次
    first = client.get("/api/graph/projects/p").headers["etag"]
    relation_id = db.query_one("SELECT id FROM relations WHERE kind = 'produced'")["id"]
    assert (
        client.post(
            f"/api/relations/{relation_id}/answer", json={"answer": "no"}, headers=headers
        ).status_code
        == 200
    )
    second = client.get("/api/graph/projects/p").headers["etag"]
    assert (
        client.post(f"/api/relations/{relation_id}/undo", json={}, headers=headers).status_code
        == 200
    )
    third = client.get("/api/graph/projects/p").headers["etag"]
    assert len({first, second, third}) == 3
    assert graph.GRAPH_API_VERSION == 4
