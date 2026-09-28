"""第四期 4a：关联的读（relation_read）：两处提到互相遮盖、先数再取、⑫ 仍是一条语句、白名单输出、
来历规则、一跳的线、在问的问题和状态句。"""
from types import SimpleNamespace

from meeting_workbench import graph, relation_read
from meeting_workbench.db import utc_now

from .helpers import count_reads
from .test_copy_vocabulary import STATE_SENTENCES_4A
from .test_file_mentions import add_file
from .test_graph import TODAY, add_meeting, add_task
from .test_relations import get, ident_id, keyed, setup, system_row, upsert


def literal(db, meeting_id, stem, file_id, *, status="active", project_id="p", count=1, picked=0):
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
               anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 60000, '[60000]', 0, 'transcript', ?, ?, ?)""",
        (meeting_id, project_id, stem, file_id, stem, count, status, picked, utc_now()),
    )


def loose(db, meeting_id, stem, file_id, *, status="shown", origin="llm", phrase="上周那版", at_ms=754_000):
    """直接写一行放宽的提到（upsert_system 在有字面行时不写，遮盖测试要绕过它）。"""
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, at_ms, stem_key, file_id,
               quote, evidence_json, created_at, updated_at)
           VALUES ('mention', 'p', ?, ?, ?, ?, ?, ?, ?, '上周那版再对一下', ?, ?, ?)""",
        (f"{meeting_id}|{stem}", status, origin, meeting_id, at_ms, stem, file_id,
         f'{{"phrase":"{phrase}"}}', utc_now(), utc_now()),
    )
    return ident_id(db, f"{meeting_id}|{stem}")


def read(db, fn, *args, **kwargs):
    with db.autocommit() as connection:
        return fn(connection, *args, **kwargs)


def shown(db, meeting_id="m"):
    return [(row["via"], row["file_id"]) for row in read(db, relation_read.meeting_mentions, meeting_id, "p")]


def test_a_rejected_or_picked_loose_row_hides_the_literal_one(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    other = add_file(db, root_id, "报价/报价单 v2.xlsx")
    literal(db, "m", "报价单", quote)
    assert shown(db) == [("literal", quote)]
    assert read(db, relation_read.file_mention_counts, [quote]) == {quote: 1}

    relation_id = loose(db, "m", "报价单", quote, status="rejected")
    assert shown(db) == []
    assert read(db, relation_read.project_edges, "p") == []
    assert read(db, relation_read.file_mention_counts, [quote]) == {}

    # 你［换成这份］过的放宽行（仍是 shown，origin manual）也盖住字面行
    db.execute("UPDATE relations SET status = 'shown', origin = 'manual', file_id = ? WHERE id = ?", (other, relation_id))
    assert shown(db) == [("manual", other)]
    [edge] = read(db, relation_read.project_edges, "p")
    assert (edge["file_id"], edge["needle"], edge["count"], edge["first_ms"], edge["picked"]) == (
        other, "上周那版", 1, 754_000, True
    )
    assert edge["anchors_json"] == "[754000]"


def test_any_literal_row_hides_the_loose_one(tmp_path):
    db, root_id = setup(tmp_path)
    plan = add_file(db, root_id, "方案.docx")
    loose(db, "m", "方案", plan)
    assert shown(db) == [("llm", plan)]
    assert read(db, relation_read.file_mention_counts, [plan]) == {plan: 1}
    # 字面行哪怕是驳回的，也盖住同一词干的放宽行
    literal(db, "m", "方案", plan, status="rejected")
    assert shown(db) == []
    assert read(db, relation_read.file_mention_counts, [plan]) == {}
    db.execute("DELETE FROM meeting_file_mentions")
    assert shown(db) == [("llm", plan)]
    # 会已经不在这个项目里：不算
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),))
    db.execute("UPDATE relations SET origin = 'manual'")  # 离开项目的触发器留着你换过的行
    db.execute("UPDATE meetings SET project_id = 'q' WHERE id = 'm'")
    assert read(db, relation_read.project_edges, "p") == []


def test_counts_come_before_the_forty_row_list(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    for index in range(41):
        add_meeting(db, f"m-{index:02d}", ago=index + 2, project_id="p")
        literal(db, f"m-{index:02d}", "报价单", quote)
    loose(db, "m", "报价单单", quote)
    assert read(db, relation_read.file_mention_counts, [quote]) == {quote: 42}
    listed = read(db, relation_read.file_mention_meetings, quote)
    assert len(listed) == 40 and listed[0]["meeting_id"] == "m"  # 新的在前
    assert listed[0]["via"] == "llm" and listed[1]["via"] == "literal"


def test_project_graph_is_still_twelve_statements_with_every_kind(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    plan = add_file(db, root_id, "方案.docx")
    keyed(db, plan, "k-plan")
    add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, created_at, updated_at)
           VALUES ('dec-a', 'm', 0, '总价下调 5%', '总价下调5', ?, ?), ('dec-b', 'm', 1, '总价下调 3%', '总价下调3', ?, ?)""",
        (utc_now(), utc_now(), utc_now(), utc_now()),
    )
    literal(db, "m", "报价单", quote, count=3)
    upsert(
        db,
        [
            system_row(ident="m|方案", stem_key="方案", file_id=plan),
            system_row(kind="related", ident="m|k-plan", origin="vector", stem_key=None, file_id=plan,
                       content_key="k-plan"),
            system_row(kind="produced", ident="t|k-plan", status="suggested", origin="rule", meeting_id=None,
                       task_id="t", stem_key=None, file_id=plan, content_key="k-plan"),
            system_row(kind="affects", ident="dec-a|k-plan", status="suggested", origin="rule",
                       decision_id="dec-a", stem_key=None, file_id=plan, content_key="k-plan"),
            system_row(kind="later_changed", ident="dec-a|dec-b", origin="rule", decision_id="dec-a",
                       to_decision_id="dec-b", stem_key=None),
            system_row(kind="restated", ident="dec-b|dec-a", origin="llm", decision_id="dec-b",
                       to_decision_id="dec-a", stem_key=None),
        ],
    )
    assert count_reads(db, lambda connection: graph.project_graph(connection, "p", today=TODAY)) == 12
    with db.autocommit() as connection:
        body = graph.project_graph(connection, "p", today=TODAY)
    edges = {edge["to"]: edge for edge in body["edges"] if edge["kind"] == "mentioned"}
    assert edges[f"file:{quote}"]["label"] == "会上说『报价单』3 次 · 00:01:00"
    assert edges[f"file:{plan}"]["needle"] == "上周那版报价单"
    assert {edge["kind"] for edge in body["edges"]} >= {"mentioned"}
    assert "related" not in {edge["kind"] for edge in body["edges"]}


def test_serialize_is_a_whitelist(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    upsert(
        db,
        [
            system_row(
                file_id=file_id, root_id=root_id, rel_path="报价单.xlsx", score=0.93,
                evidence={"phrase": "上周那版", "material": {"content_key": "k", "ordinal": 3, "root_id": 9,
                                                              "text": "材料原文"}, "score": 1, "secret": "x"},
            )
        ],
    )
    relation_id = ident_id(db, "m|报价单")
    db.execute("UPDATE relations SET prev_json = '{\"status\":\"shown\"}' WHERE id = ?", (relation_id,))
    out = relation_read.serialize(get(db, relation_id))
    flat = repr(out)
    for banned in ("score", "prev_json", "root_id", "evidence_json", "材料原文", "secret"):
        assert banned not in flat
    assert out["evidence"] == {"phrase": "上周那版", "material": {"content_key": "k", "ordinal": 3}}
    assert out["file"] == {"id": file_id, "name": ""} and out["by_you"] is False


def test_live_file_follows_the_lineage(tmp_path):
    db, root_id = setup(tmp_path)
    old = add_file(db, root_id, "报价/报价单.xlsx", day="2026-09-01")
    keyed(db, old, "k-quote")
    row = {"kind": "mention", "project_id": "p", "file_id": old, "content_key": "k-quote", "root_id": root_id,
           "rel_path": "报价/报价单.xlsx"}
    assert read(db, relation_read.live_file, row)["id"] == old
    # 挪过：同项目里同内容的活文件，修改时间新的优先
    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), old))
    older = add_file(db, root_id, "归档/报价单.xlsx", day="2026-08-01")
    newer = add_file(db, root_id, "发客户/报价单.xlsx", day="2026-09-20")
    keyed(db, older, "k-quote")
    keyed(db, newer, "k-quote")
    assert read(db, relation_read.live_file, row)["id"] == newer
    # 同一位置换了一份新内容：按 (root_id, rel_path) 找回；相关只按内容找
    db.execute("UPDATE material_files SET gone_at = ? WHERE id IN (?, ?)", (utc_now(), older, newer))
    db.execute("DELETE FROM material_files WHERE id = ?", (old,))
    again = add_file(db, root_id, "报价/报价单.xlsx")
    assert read(db, relation_read.live_file, {**row, "file_id": None})["id"] == again
    assert read(db, relation_read.live_file, {**row, "file_id": None, "kind": "related"}) is None
    assert read(db, relation_read.live_file, {**row, "file_id": None, "rel_path": "别处.xlsx"}) is None


def test_edges_of_takes_at_most_two_statements(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    keyed(db, quote, "k-quote")
    plan = add_file(db, root_id, "方案.docx")
    literal(db, "m", "报价单", quote)
    loose(db, "m", "方案", plan)
    add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    deliverable_id = db.execute(
        "INSERT INTO deliverables(task_id, kind, url, title, created_at) VALUES ('t', 'file', 'x', '报价单', ?)",
        (utc_now(),),
    )
    db.execute(
        "INSERT INTO deliverable_files(deliverable_id, content_key, root_id, rel_path) VALUES (?, 'k-quote', ?, ?)",
        (deliverable_id, root_id, "报价单.xlsx"),
    )
    kinds = relation_read.EDGE_KINDS
    by_meeting = read(db, relation_read.edges_of, "m:m", kinds)
    assert {(edge["kind"], edge["to"], edge["via"]) for edge in by_meeting} == {
        ("mention", f"file:{quote}", "literal"), ("mention", f"file:{plan}", "llm")
    }
    by_file = read(db, relation_read.edges_of, f"file:{quote}", kinds)
    assert {(edge["kind"], edge["from"]) for edge in by_file} == {("mention", "m:m"), ("deliverable", "task:t")}
    assert [edge["to"] for edge in read(db, relation_read.edges_of, "task:t", kinds)] == [f"file:{quote}"]
    for node in ("m:m", f"file:{quote}", "task:t", "dec:dec-x"):
        assert count_reads(db, lambda connection, node=node: relation_read.edges_of(connection, node, kinds)) <= 2


def test_questions_read_in_one_statement(tmp_path):
    db, root_id = setup(tmp_path)
    plan = add_file(db, root_id, "能耗看板/能耗看板方案.docx")
    keyed(db, plan, "k-plan")
    now = utc_now()
    db.execute(
        "INSERT INTO material_contents(content_key, layer, state, created_at, updated_at) VALUES ('k-plan', 'text', 'done', ?, ?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, loc, text) VALUES ('k-plan', 14, '第 2 页', ?)",
        ("前面的话" * 20 + "总价在原基础上下调 3%，含税" + "后面的话" * 20,),
    )
    add_task(db, "t", meeting_id="m", project_id="p", status="in_progress")
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, start_ms, created_at, updated_at)
           VALUES ('dec-a', 'm', 0, '总价下调 5%', '总价下调5', 754000, ?, ?)""",
        (now, now),
    )
    upsert(
        db,
        [
            system_row(kind="produced", ident="t|k-plan", status="suggested", origin="rule", meeting_id=None,
                       task_id="t", stem_key=None, quote="", file_id=plan, content_key="k-plan",
                       evidence={"event_kind": "added", "scope": "folder", "folder": "能耗看板/",
                                 "words": ["能耗看板"], "ref": "meeting", "days": 3}),
            system_row(kind="affects", ident="dec-a|k-plan", status="suggested", origin="rule",
                       decision_id="dec-a", stem_key=None, quote="总价下调 5%", file_id=plan,
                       content_key="k-plan", evidence={"rule": "value", "terms": ["总价"], "ordinal": 14}),
        ],
    )
    questions = read(db, relation_read.file_questions, plan)
    assert [item["kind"] for item in questions] == ["affects", "produced"]
    affects, produced = questions
    assert affects["text"].startswith("可能过时：") and affects["text"].endswith("决议『总价下调 5%』")
    assert affects["answers"] == ["updated", "no"] and affects["decision"]["start_ms"] == 754_000
    assert affects["passage"]["loc"] == "第 2 页" and "总价" in affects["passage"]["text"]
    assert len(affects["passage"]["text"].strip("…")) <= relation_read.PASSAGE_CHARS
    assert produced["text"] == "会后 3 天新增在『能耗看板/』"
    assert produced["ask"] == "是任务『任务 t』的交付物吗？"
    assert produced["file"] == {"id": plan, "name": "能耗看板方案.docx", "folder": "能耗看板/"}
    assert produced["answers"] == ["yes", "no"] and produced["words"] == ["能耗看板"]
    for item in questions:
        assert "score" not in repr(item) and "root_id" not in repr(item)
    assert [item["relation_id"] for item in read(db, relation_read.task_questions, "t")] == [produced["relation_id"]]
    grouped = read(db, relation_read.decision_questions, ["dec-a", "dec-z"])
    assert grouped["dec-z"] == [] and grouped["dec-a"][0]["text"] == "『能耗看板方案』之后没改过，可能过时"
    assert count_reads(db, lambda connection: relation_read.file_questions(connection, plan)) == 1


def test_produced_evidence_sentences():
    text = relation_read.produced_evidence_text
    assert text({"event_kind": "added", "folder": "能耗看板/", "days": 0, "ref": "meeting"}) == "会后当天新增在『能耗看板/』"
    assert text({"event_kind": "added", "words": ["能耗看板"], "days": 3, "ref": "meeting"}) == (
        "会后 3 天新增，文件名里也有『能耗看板』"
    )
    assert text({"event_kind": "changed", "words": ["报价单"], "days": 3, "ref": "meeting"}) == (
        "会后 3 天改过，文件名里也有『报价单』"
    )
    assert text({"event_kind": "added", "folder": "能耗看板/", "days": 3, "ref": "confirm"}) == (
        "任务确认后 3 天新增在『能耗看板/』"
    )
    assert text({"event_kind": "added", "folder": "能耗看板/", "days": 0, "ref": "confirm"}) == "确认当天新增在『能耗看板/』"


def test_links_state_sentences():
    on = SimpleNamespace(links_enabled=True, semantic_enabled=True, material_content_enabled=True)
    off = SimpleNamespace(links_enabled=False)
    state = relation_read.links_state
    seen = [
        state(None, off, "mentions"),
        state({"enabled": True}, SimpleNamespace(links_enabled=True, semantic_enabled=False), "related"),
        state({}, on, "related", offline=True),
        state({}, on, "mentions", mention_state="failed"),
        state({}, on, "decisions", pair_state="failed"),
        state({"llm": "no_key"}, on, "mentions", mention_state="pending"),
        state({"llm": "auth"}, on, "mentions", mention_state="pending"),
        state({"llm": "capped"}, on, "mentions", mention_state="pending"),
        state({"llm": "balance"}, on, "mentions", mention_state="pending"),
        state({"llm": "failing"}, on, "mentions", mention_state="pending"),
        state({"paused": "busy"}, on, "related"),
        state({}, on, "related", materials_reading=True),
        state({"phases": {"related": "waiting"}}, on, "related"),
        state({"llm": "ok"}, on, "mentions", mention_state="pending"),
        state({}, on, "timeline", waiting=3),
        state({}, on, "related", empty=True),
    ]
    assert {item["text"] for item in seen} == set(STATE_SENTENCES_4A)
    for item in seen:
        assert set(item) == {"kind", "text", "action"} and item["kind"] in ("ok", "waiting", "stopped")
    assert seen[3]["action"] == {"kind": "retry", "label": "现在重试"} and seen[4]["action"] == seen[3]["action"]
    assert sum(1 for item in seen if item["action"]) == 2
    assert state(None, on, "mentions") == {"kind": "ok", "text": "", "action": None}

    class Worker:
        def snapshot(self):
            return {"enabled": False}

    assert state(Worker(), on, "mentions")["text"] == relation_read.LINKS_OFF
    # 会议在转写时，已经整理完的页面不写这句
    assert state({"paused": "busy"}, on, "mentions", mention_state="done")["kind"] == "ok"
    assert state({"paused": "busy"}, on, "mentions", mention_state="pending")["text"] == relation_read.TRANSCRIBING


def test_loose_edges_reuse_the_mentioned_line_and_its_labels(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    plan = add_file(db, root_id, "能耗看板方案.pptx")
    upsert(
        db,
        [
            system_row(file_id=quote, evidence={"phrase": "上周那版报价单", "via": "time_hint",
                                                  "hits": [{"at_ms": 754_000, "quote": "上周那版报价单"}]},
                       at_ms=754_000),
            system_row(ident="m|能耗看板方案", stem_key="能耗看板方案", file_id=plan, at_ms=60_000,
                       evidence={"phrase": "能耗看板那个PPT", "via": "stem",
                                 "hits": [{"at_ms": 60_000, "quote": "a"}, {"at_ms": 90_000, "quote": "b"}]}),
        ],
    )
    assert count_reads(db, lambda connection: graph.project_graph(connection, "p", today=TODAY)) == 12
    with db.autocommit() as connection:
        body = graph.project_graph(connection, "p", today=TODAY)
    edges = {edge["to"]: edge for edge in body["edges"] if edge["kind"] == "mentioned"}
    assert edges[f"file:{quote}"]["id"] == f"e:file:{quote}:m"
    assert edges[f"file:{quote}"]["label"] == "会上说『上周那版报价单』· 00:12:34"
    assert edges[f"file:{plan}"]["label"] == "会上说『能耗看板那个PPT』等 2 处 · 00:01:00"
    assert edges[f"file:{plan}"]["anchors_ms"] == [60_000, 90_000]
    assert graph.GRAPH_API_VERSION == 3
    # 简报、文件面板多出的三项：放宽行才有
    with db.autocommit() as connection:
        from meeting_workbench import file_mentions

        files = {item["file_id"]: item for item in file_mentions.meeting_files(connection, "m", "p")["files"]}
        detail = file_mentions.file_detail(connection, plan, quotes=lambda meeting_id, starts: {})
    assert (files[quote]["relation_id"], files[quote]["phrase"], files[quote]["via"]) == (
        ident_id(db, "m|报价单"), "上周那版报价单", "time_hint"
    )
    assert files[plan]["count"] == 2 and files[plan]["source"] == "transcript"
    [row] = detail["meetings"]
    assert (row["relation_id"], row["phrase"], row["via"], row["status"], row["quote"]) == (
        ident_id(db, "m|能耗看板方案"), "能耗看板那个PPT", "stem", "active", "上周那版报价单再对一下"
    )
    # 字面行三项都是 None
    literal(db, "m", "报价单", quote)
    with db.autocommit() as connection:
        files = {item["file_id"]: item for item in file_mentions.meeting_files(connection, "m", "p")["files"]}
    assert (files[quote]["relation_id"], files[quote]["phrase"], files[quote]["via"]) == (None, None, None)


def test_rejected_loose_rows_join_the_rejected_list(tmp_path):
    from meeting_workbench import file_mentions

    db, root_id = setup(tmp_path)
    plan = add_file(db, root_id, "能耗看板方案.pptx")
    relation_id = loose(db, "m", "能耗看板方案", plan, status="rejected", phrase="能耗看板那个PPT")
    with db.autocommit() as connection:
        detail = file_mentions.file_detail(connection, plan, quotes=lambda meeting_id, starts: {})
    assert detail["active_meetings"] == 0
    [row] = detail["meetings"]
    assert (row["status"], row["relation_id"], row["phrase"], row["title"]) == (
        "rejected", relation_id, "能耗看板那个PPT", "会 m"
    )


def test_same_content_copies_count_once_and_rejected_rows_do_not_count(tmp_path):
    """4d：「在 N 场会上被提到」算上本项目同内容的活副本，每场会只算一次；rejected 不算、也不占前 40 行。"""
    db, root_id = setup(tmp_path)
    first = add_file(db, root_id, "报价单.xlsx")
    copy = add_file(db, root_id, "备份/报价单.xlsx")
    gone = add_file(db, root_id, "旧/报价单.xlsx", gone=True)
    for file_id in (first, copy, gone):
        keyed(db, file_id, "k-quote")
    for index in range(41):
        add_meeting(db, f"c-{index:02d}", ago=index + 2, project_id="p")
        literal(db, f"c-{index:02d}", "报价单", first if index % 2 else copy)
    # 同一场会两份副本都被提到：只算一次
    literal(db, "m", "报价单", first)
    db.execute("UPDATE meeting_file_mentions SET file_id = ? WHERE meeting_id = 'c-00'", (copy,))
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
               anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('c-00', 'p', '报价', ?, '报价', 1, 0, '[0]', 0, 'transcript', 'active', 0, ?)""",
        (first, utc_now()),
    )
    # 只在已经不见的副本上被提到、以及标过「不是这份文件」的，都不算
    add_meeting(db, "gone-only", ago=60, project_id="p")
    literal(db, "gone-only", "报价单", gone)
    for index in range(3):
        add_meeting(db, f"r-{index}", ago=0, project_id="p")
        literal(db, f"r-{index}", "报价单", first, status="rejected")
    counts = read(db, relation_read.file_mention_counts, [first, copy])
    assert counts == {first: 42, copy: 42}
    listed = read(db, relation_read.file_mention_meetings, first)
    assert len(listed) == 22 and not any(row["meeting_id"].startswith("r-") for row in listed)
    detail = read(db, file_detail_of, first)
    assert detail["active_meetings"] == 42
    assert [row["status"] for row in detail["meetings"]].count("rejected") == 3
    assert len(detail["meetings"]) == 22 + 3


def file_detail_of(connection, file_id):
    from meeting_workbench.file_mentions import file_detail

    return file_detail(connection, file_id, quotes=lambda meeting_id, starts: {})
