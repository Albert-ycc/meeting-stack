"""第四期 4a：决议入库。选段、拆条、时间点；id 跨纪要版本不变；L1 的快路径、对比排队、换项目；
简报和聚焦视图的 id 与当场解析；放到需求下和撤销。"""
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from meeting_workbench import decisions, graph
from meeting_workbench.db import utc_now

from .test_graph import add_meeting, add_project, add_requirement, make_db
from .test_notify import MINUTES_SIX_SECTION
from .test_tasks_api import write_headers

NOW = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)

TRICKY = """# 周会
## 会议结论与风险
- 风险：排期紧
## 未达成共识
- 预算
## 决议
1. **结论**：上线改到 10 月 [00:20:00 — 00:25:10]
   - 前提是测试环境 9/30 前到位
   - 市场部同步
   补充说明一行
2. 驻场排班改两班 `[01:02]`
2. 驻场排班改两班 `[01:02]`
3. （无）
4. 暂无
## 待办
- 月总：对接数理学会
"""

V1 = """# 周会

## 一分钟摘要
定了阈值。

## 决议
1. 司美格鲁太的对照组先按 0.8 执行 [00:12:34]
2. 驻场排班下周起改成两班 [00:20:00]
3. 导出按筛选范围全量导出 [00:30:00]
"""
# 词典「替换」改了错字，顺序也调了
V2_TYPO = """# 周会

## 一分钟摘要
定了阈值。

## 决议
1. 驻场排班下周起改成两班 [00:20:00]
2. 司美格鲁肽的对照组先按 0.8 执行 [00:12:34]
3. 导出按筛选范围全量导出 [00:30:00]
"""


def set_minutes(db, meeting_id, version_id, markdown, kind="draft"):
    db.execute(
        """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind, published,
                                        created_at)
           VALUES (?, ?, (SELECT COALESCE(MAX(version_no), 0) + 1 FROM minutes_versions
                           WHERE meeting_id = ?), ?, '', ?, 0, ?)""",
        (version_id, meeting_id, meeting_id, markdown, kind, utc_now()),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id = ? WHERE id = ?", (version_id, meeting_id))


def ingest(db, **kwargs):
    kwargs.setdefault("now", NOW)
    return decisions.ingest_pending(db, **kwargs)


def live(db, meeting_id):
    return [
        (row["id"], row["text"])
        for row in db.query_all(
            "SELECT id, text FROM decisions WHERE meeting_id = ? AND gone_at IS NULL ORDER BY ordinal",
            (meeting_id,),
        )
    ]


def scan(db, meeting_id):
    return db.query_one("SELECT * FROM decision_scan WHERE meeting_id = ?", (meeting_id,))


def graph_rev(db):
    with db.autocommit() as connection:
        return graph.graph_rev(connection)


def meeting_with_minutes(db, markdown=V1, *, meeting_id="m", project_id="p", ago=1):
    add_meeting(db, meeting_id, ago=ago, project_id=project_id, origin="manual")
    set_minutes(db, meeting_id, f"mv-{meeting_id}-1", markdown, kind="generated")


# ---------------------------------------------------------------------- 解析


def test_section_choice_prefers_decision_heading_and_skips_negative_ones():
    parsed = decisions.parse_decisions(TRICKY)

    # 「会议结论与风险」在前，照样让给「## 决议」；「未达成共识」跳过
    assert [item.text for item in parsed.items] == ["上线改到 10 月", "驻场排班改两班"]
    exact = decisions.parse_decisions("## 结论\n- 甲方案\n## 三、核心决议\n- 乙方案\n")
    assert [item.text for item in exact.items] == ["乙方案"]
    # 同一级取靠前的
    tie = decisions.parse_decisions("## 讨论结论\n- 甲方案\n## 会后共识\n- 乙方案\n")
    assert [item.text for item in tie.items] == ["甲方案"]
    assert decisions.parse_decisions("## 待定事项\n- 预算\n").note == "no_section"


def test_six_section_minutes_keep_the_range_end():
    parsed = decisions.parse_decisions(MINUTES_SIX_SECTION)

    first, second = parsed.items
    assert (first.text, first.start_ms, first.end_ms) == ("患者端支持手机号修改，设四重约束", 36_000, 319_000)
    assert first.detail == "正文。"
    assert (second.start_ms, second.end_ms) == (347_000, 418_000)


def test_sub_items_fold_into_detail_and_duplicates_and_none_are_dropped():
    parsed = decisions.parse_decisions(TRICKY)

    first, second = parsed.items
    assert first.detail == "前提是测试环境 9/30 前到位 市场部同步 补充说明一行"
    assert (first.start_ms, first.end_ms) == (1_200_000, 1_510_000)
    assert (second.text, second.start_ms) == ("驻场排班改两班", 62_000)
    assert len(parsed.items) == 2  # 重复的、（无）、暂无都丢掉
    assert parsed.note is None and len(parsed.section_hash) == 40


def test_topic_tag_prefix_is_stripped_but_tnm_staging_is_not():
    """会议材料里「T05　」「R07　」这类议题编号（字母+数字+全角空格）是格式噪声，要清掉；
    TNM 分期（T3、N1M0）后面接的是文字或半角空格，不会被这条规则误伤。"""
    tagged = decisions.parse_decisions(
        "## 决议\n1. T05　确定要把职称证校验加进去 [00:10:00]\n2. R07　亲友自购得积分 [00:20:00]\n"
    )
    assert [item.text for item in tagged.items] == ["确定要把职称证校验加进去", "亲友自购得积分"]
    staging = decisions.parse_decisions("## 决议\n1. T3N1M0 分期评估通过 [00:10:00]\n")
    assert [item.text for item in staging.items] == ["T3N1M0 分期评估通过"]


def test_notes_for_missing_and_empty_sections():
    assert decisions.parse_decisions("# 周会\n## 会议背景\n聊了\n### 议题一\n").note == "no_section"
    empty = decisions.parse_decisions("# 周会\n## 决议\n（无）\n")
    assert (empty.items, empty.note) == ([], "empty")
    assert decisions.parse_safely("").note == "no_minutes"


def test_table_sections_are_one_decision_per_row():
    """真实纪要里最常见的写法（v3 协议）：决议段是表格。"""
    parsed = decisions.parse_decisions(
        "# 周会\n## 决议\n\n| # | 决议 | 音频锚点 |\n|---|---|---|\n"
        "| 1 | 五个问题排在知情同意书之前 `[00:00:40]` | `[00:00:40]` |\n"
        "| 2 | **注册顺序**确认为：基本信息 → 五个问题 | `[00:00:45 — 00:01:00]` |\n"
        "| 3 | 无 | |\n"
        "\n## 行动项\n\n| # | 行动项 | Owner |\n|---|---|---|\n| 1 | 走查 | 甲 |\n"
    )
    assert [(item.text, item.start_ms, item.end_ms) for item in parsed.items] == [
        ("五个问题排在知情同意书之前", 40_000, None),
        ("注册顺序确认为：基本信息 → 五个问题", 45_000, 60_000),
    ]
    assert parsed.note is None
    # 表头叫法不一：编号 / 内容 / 锚点；议题列进 detail，时间点只在锚点列
    other = decisions.parse_decisions(
        "## 决议\n| 编号 | 议题 | 内容 | 锚点 |\n|:--:|---|---|---|\n| 一 | 报价 | 总价下调五个点 | [12:34] |\n"
    )
    assert [(item.text, item.detail, item.start_ms) for item in other.items] == [("总价下调五个点", "报价", 754_000)]
    # 认不出正文列的表格不硬拆
    assert decisions.parse_decisions("## 决议\n| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n").note == "empty"


def test_parse_anchor_reads_single_times_and_ranges():
    assert decisions.parse_anchor("[12:34]") == (754_000, None)
    assert decisions.parse_anchor("看这里 `[00:12:34]`") == (754_000, None)
    assert decisions.parse_anchor("[00:12:34 — 00:13:00]") == (754_000, 780_000)
    assert decisions.parse_anchor("[00:12:34-00:13:00]") == (754_000, 780_000)
    assert decisions.parse_anchor("[1:02:03 ~ 1:05:00]") == (3_723_000, 3_900_000)
    assert decisions.parse_anchor("[00:01:00 至 00:02:00]") == (60_000, 120_000)
    assert decisions.parse_anchor("[00:01:00到00:02:00]") == (60_000, 120_000)
    assert decisions.parse_anchor("没有时间点") == (None, None)
    # graph._anchor_ms 改走它
    assert graph._anchor_ms("[00:20:00 — 00:25:10]") == 1_200_000


def test_minutes_outline_uses_the_parser():
    outline = graph.minutes_outline(TRICKY, detail=True)

    assert [item["text"] for item in outline["decisions"]] == ["上线改到 10 月", "驻场排班改两班"]
    assert outline["decisions"][0]["end_ms"] == 1_510_000
    assert outline["decisions"][0]["id"] is None
    assert graph.minutes_outline("# 周会\n## 决议\n（无）\n")["decisions_note"] == "决议段是空的"


def test_carry_over_matches_typos_reorders_and_near_anchors():
    old_items = decisions.parse_decisions(V1).items
    old = [
        {"id": f"dec-{index}", "text_key": item.text_key, "start_ms": item.start_ms, "ordinal": index,
         "gone_at": None}
        for index, item in enumerate(old_items)
    ]
    new = decisions.parse_decisions(
        "## 决议\n1. 驻场排班改成三班 [00:20:02]\n2. 司美格鲁肽的对照组先按 0.8 执行 [00:12:34]\n"
        "3. 新增：周五复盘 [00:40:00]\n"
    ).items

    # 相似比不到 0.8，但时间点 5 秒以内、不低于 0.5 也能配上
    assert decisions.carry_over(old, new) == ["dec-1", "dec-0", None]


def test_decision_moment_adds_the_offset_to_the_meeting_time():
    row = {"recording_date": "2026-09-20T10:00:00+08:00", "created_at": "2026-09-21T00:00:00+00:00"}
    assert decisions.decision_moment(row, 60_000) == datetime(2026, 9, 20, 2, 1, tzinfo=UTC)
    assert decisions.decision_moment({"recording_date": None, "created_at": None}, 0) is None


# ---------------------------------------------------------------------- 入库


def test_typo_fix_and_reorder_keep_ids_and_do_not_queue_a_comparison(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)

    assert ingest(db)["written"] == 1
    before = dict((text[:4], decision_id) for decision_id, text in live(db, "m"))
    assert all(decision_id.startswith("dec-") and len(decision_id) == 20 for decision_id in before.values())
    assert scan(db, "m")["pair_state"] == "pending"  # 第一次入库算实质变化
    db.execute("UPDATE decision_scan SET pair_state = 'done' WHERE meeting_id = 'm'")

    set_minutes(db, "m", "mv-m-2", V2_TYPO)
    ingest(db)

    after = live(db, "m")
    assert [text for _id, text in after][:2] == ["驻场排班下周起改成两班", "司美格鲁肽的对照组先按 0.8 执行"]
    assert dict((text[:4], decision_id) for decision_id, text in after) == before
    assert scan(db, "m")["pair_state"] == "done"  # 改错字、调顺序不花一次对比
    assert scan(db, "m")["minutes_version_id"] == "mv-m-2"


def test_material_changes_queue_a_comparison(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)
    ingest(db)
    ids = [decision_id for decision_id, _text in live(db, "m")]
    db.execute("UPDATE decision_scan SET pair_state = 'failed', pair_attempts = 3 WHERE meeting_id = 'm'")

    set_minutes(db, "m", "mv-m-2", V1.replace("两班", "三班"))
    ingest(db)

    assert [decision_id for decision_id, _text in live(db, "m")] == ids  # 数值词变了，id 照样接回
    row = scan(db, "m")
    assert (row["pair_state"], row["pair_attempts"]) == ("pending", 0)

    db.execute("UPDATE decision_scan SET pair_state = 'done' WHERE meeting_id = 'm'")
    set_minutes(db, "m", "mv-m-3", V1.replace("两班", "三班") + "4. 新增：周五复盘 [00:40:00]\n")
    ingest(db)
    assert len(live(db, "m")) == 4
    assert scan(db, "m")["pair_state"] == "pending"


def test_rolled_back_minutes_revive_the_old_ids(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)
    ingest(db)
    ids = [decision_id for decision_id, _text in live(db, "m")]

    set_minutes(db, "m", "mv-m-2", V1.replace("3. 导出按筛选范围全量导出 [00:30:00]\n", ""))
    ingest(db)
    gone = db.query_one("SELECT gone_at FROM decisions WHERE id = ?", (ids[2],))
    assert gone["gone_at"] is not None  # 纪要里没了只记 gone_at，不删
    assert [decision_id for decision_id, _text in live(db, "m")] == ids[:2]

    set_minutes(db, "m", "mv-m-3", V1)
    ingest(db)
    assert [decision_id for decision_id, _text in live(db, "m")] == ids


def test_edits_outside_the_section_take_the_fast_path(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)
    ingest(db)
    stamps = db.query_all("SELECT id, updated_at FROM decisions ORDER BY id")

    set_minutes(db, "m", "mv-m-2", V1.replace("定了阈值。", "定了阈值，也聊了排班。"))
    rev = graph_rev(db)  # 换纪要版本本身会让 meetings 加一，只看入库这一步
    result = ingest(db)

    assert result["fast"] == 1
    assert graph_rev(db) == rev
    assert db.query_all("SELECT id, updated_at FROM decisions ORDER BY id") == stamps
    assert scan(db, "m")["minutes_version_id"] == "mv-m-2"
    # 台账跟上以后不再是待办，闲着的一轮什么都不写
    assert ingest(db) == {"pending": 0, "tried": 0, "written": 0, "fast": 0, "skipped": 0}
    assert graph_rev(db) == rev


def test_meetings_older_than_the_backfill_window_are_done_at_once(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db, meeting_id="old", ago=400)
    meeting_with_minutes(db, meeting_id="new", ago=3)

    ingest(db, backfill_days=180)

    old = scan(db, "old")
    rows = db.query_all("SELECT id, text FROM decisions WHERE meeting_id = 'old' AND gone_at IS NULL")
    assert old["pair_state"] == "done"
    assert old["pair_hash"] == decisions.pair_hash(rows)
    assert scan(db, "new")["pair_state"] == "pending"


def test_backfill_zero_means_only_meetings_after_links_since():
    since = "2026-09-01T00:00:00+00:00"
    assert decisions.backfill_cutoff(NOW, 0, since) == datetime(2026, 9, 1, tzinfo=UTC)
    assert decisions.backfill_cutoff(NOW, 30, since) == NOW - timedelta(days=30)


def test_meetings_without_a_project_are_not_queued(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    meeting_with_minutes(db, project_id=None)

    ingest(db)

    assert len(live(db, "m")) == 3
    assert scan(db, "m")["pair_state"] == "idle"


def test_removed_minutes_mark_every_decision_gone(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)
    ingest(db)

    db.execute("UPDATE meetings SET current_minutes_version_id = NULL WHERE id = 'm'")
    ingest(db)

    assert live(db, "m") == []
    assert db.query_one("SELECT COUNT(*) AS n FROM decisions WHERE meeting_id = 'm'")["n"] == 3
    assert scan(db, "m")["note"] == "no_minutes"


def _place(db, decision_id, placement, requirement_id):
    db.execute(
        "UPDATE decisions SET placement = ?, requirement_id = ?, placed_at = ? WHERE id = ?",
        (placement, requirement_id, utc_now(), decision_id),
    )


def test_project_change_resets_ai_placements_only(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_requirement(db, "r-p", "p", "白名单运营后台")
    meeting_with_minutes(db)
    ingest(db)
    first, second, third = (decision_id for decision_id, _text in live(db, "m"))
    _place(db, first, "ai", "r-p")
    _place(db, second, "picked", "r-p")
    _place(db, third, "none", None)
    db.execute("UPDATE decision_scan SET pair_state = 'done' WHERE meeting_id = 'm'")

    db.execute("UPDATE meetings SET project_id = 'q' WHERE id = 'm'")
    ingest(db)

    placements = {
        row["id"]: (row["placement"], row["requirement_id"])
        for row in db.query_all("SELECT id, placement, requirement_id FROM decisions")
    }
    assert placements == {first: (None, None), second: ("picked", "r-p"), third: ("none", None)}
    assert scan(db, "m")["pair_state"] == "pending"
    assert scan(db, "m")["project_id"] == "q"

    # 撤销改归属把会挪回来：你的 picked 还在
    db.execute("UPDATE meetings SET project_id = 'p' WHERE id = 'm'")
    ingest(db)
    row = db.query_one("SELECT placement, requirement_id FROM decisions WHERE id = ?", (second,))
    assert (row["placement"], row["requirement_id"]) == ("picked", "r-p")


def test_newest_meetings_come_first_within_the_round_budget(tmp_path):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    for ago in (1, 2, 3, 4):
        meeting_with_minutes(db, meeting_id=f"m{ago}", ago=ago)

    assert ingest(db, max_meetings=2)["tried"] == 2
    assert [row["meeting_id"] for row in db.query_all("SELECT meeting_id FROM decision_scan ORDER BY 1")] == [
        "m1", "m2",
    ]

    ticks = count(0.0, 0.6)  # 第一场之前 0.6 秒，第二场之前 1.2 秒，超过 1.0 秒就停
    result = ingest(db, clock=lambda: next(ticks))
    assert (result["pending"], result["tried"]) == (2, 1)
    assert scan(db, "m3") is not None and scan(db, "m4") is None


def test_version_change_during_parse_writes_nothing(tmp_path, monkeypatch):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)
    original = decisions.parse_decisions

    def racing(markdown):
        set_minutes(db, "m", "mv-m-2", V2_TYPO)
        return original(markdown)

    monkeypatch.setattr(decisions, "parse_decisions", racing)
    assert ingest(db)["skipped"] == 1
    assert live(db, "m") == [] and scan(db, "m") is None

    monkeypatch.setattr(decisions, "parse_decisions", original)
    ingest(db)
    assert scan(db, "m")["minutes_version_id"] == "mv-m-2"
    assert live(db, "m")[0][1] == "驻场排班下周起改成两班"


# ---------------------------------------------------------------------- 简报和聚焦


def test_brief_and_focus_parse_live_until_the_ledger_catches_up(tmp_path):
    client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db, TRICKY)

    # links 循环关着（测试环境）：当场解析，id 为 null
    brief = client.get("/api/meetings/m/brief").json()
    assert [item["id"] for item in brief["decisions"]] == [None, None]

    ingest(db)
    ids = [decision_id for decision_id, _text in live(db, "m")]
    brief = client.get("/api/meetings/m/brief").json()
    assert brief["decisions"][0] == {"id": ids[0], "text": "上线改到 10 月", "start_ms": 1_200_000, "later": None}
    focus = client.get("/api/graph/meetings/m").json()
    assert focus["decisions"][0] == {
        "id": ids[0], "text": "上线改到 10 月", "start_ms": 1_200_000, "end_ms": 1_510_000,
        "detail": "前提是测试环境 9/30 前到位 市场部同步 补充说明一行", "later": [], "earlier": [],
        # 4e：台账里的决议带在问的可能过时
        "stale": [],
    }
    assert [item["id"] for item in focus["decisions"]] == ids

    # 纪要改了、循环还没轮到：输出不等循环，按新纪要当场解析
    set_minutes(db, "m", "mv-m-2", TRICKY.replace("两班", "三班"))
    brief = client.get("/api/meetings/m/brief").json()
    assert [(item["id"], item["text"]) for item in brief["decisions"]] == [
        (None, "上线改到 10 月"), (None, "驻场排班改三班"),
    ]

    db.execute("UPDATE meetings SET current_minutes_version_id = NULL WHERE id = 'm'")
    ingest(db)
    focus = client.get("/api/graph/meetings/m").json()
    assert focus["decisions"] == [] and focus["decisions_note"] == "纪要还没写好"


def test_ledger_read_is_one_extra_select(tmp_path):
    from .helpers import count_reads

    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db)

    def reads():
        return count_reads(db, lambda connection: graph.meeting_focus(connection, "m"))

    lagging = reads()
    ingest(db)
    # 4c：台账跟上时多一条语句（决议连 relations 再连另一头的决议和会，填 later、earlier）
    assert reads() == lagging + 1


# ---------------------------------------------------------------------- 归需求


def test_placement_errors_and_undo(tmp_path):
    client, _settings, db = make_db(tmp_path)
    headers = write_headers(client)
    add_project(db, "p", "云图AI")
    add_project(db, "q", "数据中台")
    add_requirement(db, "r-p", "p", "白名单运营后台")
    add_requirement(db, "r-q", "q", "中台需求")
    meeting_with_minutes(db)
    ingest(db)
    first, second, _third = (decision_id for decision_id, _text in live(db, "m"))

    def post(decision_id, placement, requirement_id=None):
        return client.post(
            f"/api/decisions/{decision_id}/placement",
            json={"placement": placement, "requirement_id": requirement_id},
            headers=headers,
        )

    missing = post("dec-0000000000000000", "none")
    assert (missing.status_code, missing.json()["detail"]) == (404, "决议不存在")
    other = post(first, "picked", "r-q")
    assert (other.status_code, other.json()["detail"]) == (422, "只能放到同一个项目的需求里")
    assert post(first, "picked").status_code == 422

    placed = post(first, "picked", "r-p")
    body = placed.json()
    assert placed.status_code == 200
    assert (body["decision"]["placement"], body["decision"]["requirement_id"]) == ("picked", "r-p")
    assert body["undo"] == {"placement": None, "requirement_id": None}
    until = datetime.fromisoformat(body["undo_until"].replace("Z", "+00:00"))
    assert body["undo_until"].endswith("Z")
    assert timedelta(seconds=590) < until - datetime.now(UTC) <= timedelta(seconds=600)
    # 只改 placement，不把会挂到需求上
    assert db.query_one("SELECT COUNT(*) AS n FROM requirement_meetings")["n"] == 0

    undone = post(first, **body["undo"])
    assert undone.status_code == 200
    row = db.query_one("SELECT placement, requirement_id FROM decisions WHERE id = ?", (first,))
    assert (row["placement"], row["requirement_id"]) == (None, None)

    assert post(second, "none").json()["decision"]["placement"] == "none"
    db.execute("UPDATE decisions SET gone_at = ? WHERE id = ?", (utc_now(), second))
    gone = post(second, None)
    assert (gone.status_code, gone.json()["detail"]) == (409, "这条决议已经不在纪要里了")
    assert post(first, "bogus").status_code == 422


def test_undo_after_placing_an_ai_decision_restores_ai(tmp_path):
    client, _settings, db = make_db(tmp_path)
    headers = write_headers(client)
    add_project(db, "p", "云图AI")
    add_requirement(db, "r-1", "p", "白名单运营后台")
    add_requirement(db, "r-2", "p", "能耗看板")
    meeting_with_minutes(db)
    ingest(db)
    decision_id = live(db, "m")[0][0]
    _place(db, decision_id, "ai", "r-1")
    url = f"/api/decisions/{decision_id}/placement"

    body = client.post(url, json={"placement": "picked", "requirement_id": "r-2"}, headers=headers).json()
    assert body["undo"] == {"placement": "ai", "requirement_id": "r-1"}
    assert client.post(url, json=body["undo"], headers=headers).status_code == 200

    row = db.query_one("SELECT placement, requirement_id FROM decisions WHERE id = ?", (decision_id,))
    assert (row["placement"], row["requirement_id"]) == ("ai", "r-1")


@pytest.mark.parametrize("placement", ["picked", "ai"])
def test_placement_needs_a_project_for_requirements(tmp_path, placement):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    add_requirement(db, "r-p", "p", "白名单运营后台")
    meeting_with_minutes(db, project_id=None)
    ingest(db)
    decision_id = live(db, "m")[0][0]

    with db.transaction() as connection, pytest.raises(decisions.PlacementRejected):
        decisions.place(connection, decision_id, placement, "r-p")


def test_one_broken_meeting_does_not_stall_the_round(tmp_path, monkeypatch):
    _client, _settings, db = make_db(tmp_path)
    add_project(db, "p", "云图AI")
    meeting_with_minutes(db, meeting_id="bad", ago=1)
    meeting_with_minutes(db, meeting_id="good", ago=2)
    real = decisions.ingest_meeting

    def flaky(db_, row, **kwargs):
        if row["id"] == "bad":
            raise ValueError("这场会的数据有意料之外的形状")
        return real(db_, row, **kwargs)

    monkeypatch.setattr(decisions, "ingest_meeting", flaky)
    counts = ingest(db)
    # 出错的那场会跳过、记日志；同一轮里别的会照样入库，不让整轮（连带 L2 和清理）一直卡住
    assert counts["skipped"] >= 1 and counts["tried"] == 2
    assert [text for _id, text in live(db, "good")][:1] == ["司美格鲁太的对照组先按 0.8 执行"]
