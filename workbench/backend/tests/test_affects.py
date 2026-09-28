"""第四期 4e：可能过时（affects：H2 match_due、L3 auto_clear）。纯函数的探针、两条规则、不标的几种、
上限、增量和整场重配、自动收回、回答和撤销、转写时和全文表重建时不动。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from meeting_workbench import affects, decisions, relations
from meeting_workbench.db import Database, utc_now
from meeting_workbench.relations import RelationError

from .test_file_events import swept

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
ROOT = "/材料/云图资料"


def at(days: float = 0) -> datetime:
    return NOW + timedelta(days=days)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def world(tmp_path, *, links_since: datetime | None = None):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, 'x')",
        (ROOT,),
    )
    root_id = int(db.query_one("SELECT id FROM project_material_roots")["id"])
    swept(db, root_id)
    db.execute(
        "UPDATE app_state SET value = ? WHERE key = 'links_since'", (stamp(links_since or at(-10)),)
    )
    return SimpleNamespace(db=db, root=root_id)


def meeting(db, meeting_id: str, *texts: str, day: datetime | None = None) -> None:
    """一场归了项目的会，纪要的决议段就是 texts（第一条在 12:34）。"""
    moment = day or at(-2)
    items = "\n".join(
        f"- {text}" + (" [00:12:34]" if index == 0 else "") for index, text in enumerate(texts)
    )
    markdown = f"# 周会\n\n## 决议\n\n{items}\n"
    db.execute(
        """INSERT INTO meetings(id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES (?, ?, ?, 'completed_unreviewed', 'p', ?, ?)""",
        (
            meeting_id,
            f"会 {meeting_id}",
            moment.astimezone().date().isoformat(),
            utc_now(),
            utc_now(),
        ),
    )
    db.execute(
        """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES (?, ?, 1, ?, '', 'generated', 0, ?)""",
        (f"mv-{meeting_id}", meeting_id, markdown, utc_now()),
    )
    db.execute(
        "UPDATE meetings SET current_minutes_version_id = ? WHERE id = ?",
        (f"mv-{meeting_id}", meeting_id),
    )
    decisions.ingest_pending(db, now=NOW, max_seconds=60)


_KEYS = iter(range(1, 10_000))


def material(
    db,
    root_id: int,
    rel_path: str,
    *chunks: str,
    mtime: datetime | None = None,
    key: str | None = None,
    start_ms: int | None = None,
    meeting_id: str | None = None,
    fresh: bool = True,
) -> dict:
    """一份读好的文件：文件行、内容、片段。"""
    content_key = key or f"q2:{next(_KEYS):032d}"
    mtime_ns = ns(mtime or at(-20))
    name = rel_path.rpartition("/")[2]
    stem, _dot, ext = name.rpartition(".")
    if (
        db.query_one("SELECT 1 AS x FROM material_contents WHERE content_key = ?", (content_key,))
        is None
    ):
        db.execute(
            """INSERT INTO material_contents(content_key, layer, state, chars, chunks, meeting_id, created_at, updated_at)
               VALUES (?, 'text', 'done', 100, ?, ?, 'x', 'x')""",
            (content_key, len(chunks), meeting_id),
        )
        for ordinal, text in enumerate(chunks):
            db.execute(
                "INSERT INTO material_chunks(content_key, ordinal, loc, start_ms, text) VALUES (?, ?, ?, ?, ?)",
                (content_key, ordinal, f"第 {ordinal + 1} 页", start_ms, text),
            )
    existing = db.query_one(
        "SELECT id FROM material_files WHERE root_id = ? AND rel_path = ?", (root_id, rel_path)
    )
    if existing is None:
        db.execute(
            """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone,
                   seen_at, content_key, content_size, content_mtime_ns)
               VALUES (?, ?, ?, ?, ?, ?, ?, 100, ?, 'normal', 'x', ?, ?, ?)""",
            (
                root_id,
                rel_path,
                rel_path.rpartition("/")[0],
                name,
                stem,
                stem,
                ext,
                mtime_ns,
                content_key,
                100 if fresh else 99,
                mtime_ns,
            ),
        )
    else:
        db.execute(
            """UPDATE material_files SET content_key = ?, mtime_ns = ?, content_size = ?, content_mtime_ns = ?
                WHERE id = ?""",
            (content_key, mtime_ns, 100 if fresh else 99, mtime_ns, existing["id"]),
        )
    row = db.query_one(
        "SELECT * FROM material_files WHERE root_id = ? AND rel_path = ?", (root_id, rel_path)
    )
    return row


def run(db, *, now: datetime = NOW, busy=lambda: False, **kwargs):
    return affects.match_due(db, busy, 5.0, now=now, since=stamp(now), **kwargs)


def open_rows(db) -> list[dict]:
    return db.query_all(
        """SELECT r.*, f.name FROM relations r LEFT JOIN material_files f ON f.id = r.file_id
            WHERE r.kind = 'affects' AND r.status = 'suggested' ORDER BY r.id"""
    )


def names(db) -> list[str]:
    return sorted(row["name"] for row in open_rows(db))


# ---------------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    ("text", "kind", "number", "unit", "month", "day"),
    [
        ("5%", "percent", 5, "", None, None),
        ("5％", "percent", 5, "", None, None),
        ("百分之五", "percent", 5, "", None, None),
        ("5 个点", "percent", 5, "", None, None),
        ("12万", "money", 120_000, "", None, None),
        ("3000 元", "money", 3000, "", None, None),
        ("报价 1.2k", "money", 1200, "", None, None),
        ("¥1.2k", "money", 1200, "", None, None),
        ("3w 块", "money", 30_000, "", None, None),
        ("两万块", "money", 20_000, "", None, None),
        ("9月30日", "date", None, "", 9, 30),
        ("9/30", "date", None, "", 9, 30),
        ("2025/9/30", "date", None, "", 9, 30),
        ("2025年9月30日", "date", None, "", 9, 30),
        ("9.30号", "date", None, "", 9, 30),
        ("十月底", "month", None, "", 10, None),
        ("10 月份", "month", None, "", 10, None),
        ("3 人", "count", 3, "人", None, None),
        ("2 台", "count", 2, "台", None, None),
        ("五天", "count", 5, "天", None, None),
        ("0.8", "number", 0.8, "", None, None),
        ("120", "number", 120, "", None, None),
    ],
)
def test_values(text, kind, number, unit, month, day):
    (value,) = affects.values(text)
    assert (value.kind, value.unit, value.month, value.day) == (kind, unit, month, day)
    if number is not None:
        assert value.number == pytest.approx(number)


def test_values_skip_versions_and_single_digits():
    assert affects.values("报价单 v3 第 2 稿") == []
    # k、w 后面跟字母、或者前后都不像说钱的，不是金额
    assert affects.values("5 kg") == []
    assert affects.values("3w 用户") == []
    assert [value.kind for value in affects.values("1.2k")] == ["number"]
    # 月后面的中文数字不是日；阿拉伯数字后面跟单位的也不是
    assert [(value.kind, value.month, value.day) for value in affects.values("12 月两个版本")] == [
        ("month", 12, None),
        ("count", None, None),
    ]
    assert [value.kind for value in affects.values("12月3人到场")] == ["month", "count"]
    assert [(value.kind, value.month, value.day) for value in affects.values("十月十五日上线")] == [
        ("date", 10, 15)
    ]
    assert (
        affects.cn_number("三千五百") == 3500
        and affects.cn_number("十五") == 15
        and affects.cn_number("两万") == 20_000
    )


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("总价下调 5%", ["总价"]),
        ("阈值先按 0.8 执行", ["阈值"]),
        ("上线时间定在 10月15日", ["上线时间"]),
        ("驻场改成 2 人", ["驻场"]),
        ("预算控制在12万以内", ["预算"]),
        # 在「在」「按」「由」「从」处截断
        ("总价在原基础上下调 5%", ["总价"]),
        ("报价单总价在原基础上下调 5%，含税", ["报价单总价"]),
        ("驻场人员由 3 人改成 2 人", ["驻场人员"]),
        ("驻场从 3 人调到 2 人", ["驻场"]),
    ],
)
def test_decision_subject_probes(text, subject):
    assert affects.decision_subject(text) == subject


def test_decision_subject_adds_terms_and_skips_project_names():
    terms = [("能耗看板", ["能耗看板", "看板系统"]), ("驻场服务", ["驻场服务"]), ("排期", ["排期"])]
    assert affects.decision_subject("看板系统的总价下调 5%，排期不变", terms=terms) == [
        "总价",
        "能耗看板",
        "排期",
    ]
    assert affects.decision_subject("云图AI下调 5%", excluded=["云图AI"]) == []


def test_cancel_objects():
    assert affects.cancel_objects("取消驻场服务") == ["驻场服务"]
    assert affects.cancel_objects("短信通知换成企业微信") == ["短信通知"]
    assert affects.cancel_objects("不再做周报了") == ["做周报"]
    # 后面紧跟数值的「改成」是改数
    assert affects.cancel_objects("驻场改成 2 人") == []
    # 复合名词里的「取消」（「订单取消接口」「销售单取消」「采购单取消」）：D2 误报复现，不能把「共用」
    # 当成取消的对象
    assert (
        affects.cancel_objects("订单取消接口统一为一个，销售单取消与采购单取消共用，需要对。") == []
    )
    # 句中动词用法照常算：前面是别的词（本期、决定、这次）也不能丢
    assert affects.cancel_objects("也取消驻场服务") == ["驻场服务"]
    assert affects.cancel_objects("本期去掉资质核验模块") == ["资质核验模块"]
    assert affects.cancel_objects("会上决定取消驻场服务") == ["驻场服务"]
    assert affects.cancel_objects("这次不做医助端") == ["医助端"]


def test_rules():
    decided = affects.values("总价下调 5%")
    assert affects.value_hit("…总价在原基础上下调 3%，含税…", "总价", decided)
    assert not affects.value_hit("总价下调 5%，含税", "总价", decided)  # 文件已经改好了
    assert not affects.value_hit(
        "总价" + "这里是很长的一段说明文字" * 5 + "下调 3%", "总价", decided
    )  # 太远
    assert not affects.value_hit("驻场 3 台", "驻场", affects.values("驻场改成 2 人"))  # 单位不同
    assert affects.value_hit("方案含驻场服务 3 人", "驻场", affects.values("驻场改成 2 人"))
    assert affects.cancel_hit("方案含驻场服务 3 人", "驻场服务")
    assert affects.cancel_hit("发短信通知", "短信通知")


# ---------------------------------------------------------------------- H2


def test_value_rule_marks_an_old_file(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%")
    file = material(
        w.db, w.root, "报价/报价单 v3.xlsx", "封面", "报价说明：总价在原基础上下调 3%，含税"
    )
    result = run(w.db)
    assert result["written"] == 1
    (row,) = open_rows(w.db)
    decision_id = w.db.query_one("SELECT id FROM decisions")["id"]
    assert row["ident"] == f"{decision_id}|{file['content_key']}"
    assert (row["origin"], row["meeting_id"], row["at_ms"], row["quote"]) == (
        "rule",
        "m",
        754_000,
        "总价下调 5%",
    )
    assert json.loads(row["evidence_json"]) == {"rule": "value", "terms": ["总价"], "ordinal": 1}
    # 一场会配完：台账跟上，再跑一轮什么都不写
    scan = w.db.query_one(
        "SELECT affects_hash, section_hash, affects_chunk_mark FROM decision_scan WHERE meeting_id = 'm'"
    )
    assert scan["affects_hash"] == scan["section_hash"] and scan["affects_chunk_mark"] > 0
    assert run(w.db)["pending"] == 0


def test_too_common_term_subject_is_skipped(tmp_path):
    """D2 误报复现：决议里带一个全项目到处都是的已确认词条（EDC）当主语时，这个词条本身太泛，
    不能当证据——即使某几份文件里刚好有一个不一样的数，也不该因为这个词条而标过时；真正具体的
    主语（节点数量）不受影响，照样能标到对的文件。"""
    w = world(tmp_path)
    w.db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, also, confirmed, project_id, created_at, updated_at)
           VALUES ('t-edc', 'EDC', '[]', '[]', 1, 'p', 'x', 'x')"""
    )
    meeting(w.db, "m", "EDC 节点数量调整为 2 个")
    material(w.db, w.root, "方案/节点方案.docx", "节点数量为 5 个")
    for index in range(11):
        material(w.db, w.root, f"EDC/接口说明{index}.docx", f"EDC 模块处理 {index + 3} 个数据")
    run(w.db)
    assert names(w.db) == ["节点方案.docx"]


def test_cancel_rule_and_two_char_subjects(tmp_path, monkeypatch):
    w = world(tmp_path)
    meeting(w.db, "m", "取消驻场服务", "短信通知换成企业微信", "预算控制在12万以内")
    material(w.db, w.root, "方案/实施方案.docx", "方案含驻场服务 3 人")
    material(w.db, w.root, "方案/通知说明.docx", "上线以后发短信通知用户")
    material(w.db, w.root, "报价/预算表.xlsx", "项目预算 15 万元")
    run(w.db)
    rules = sorted(
        (row["name"], json.loads(row["evidence_json"])["rule"]) for row in open_rows(w.db)
    )
    assert rules == [
        ("实施方案.docx", "cancel"),
        ("通知说明.docx", "cancel"),
        ("预算表.xlsx", "value"),
    ]


def test_two_char_subjects_skip_large_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(affects, "SHORT_NEEDLE_CHUNKS", 0)
    w = world(tmp_path)
    meeting(w.db, "m", "预算控制在12万以内")
    material(w.db, w.root, "报价/预算表.xlsx", "项目预算 15 万元")
    run(w.db)
    assert open_rows(w.db) == []


@pytest.mark.parametrize(
    "case",
    ["newer", "stale_key", "recording", "meeting_material", "record", "too_old", "same_value"],
)
def test_files_that_are_not_marked(tmp_path, case):
    w = world(tmp_path)
    meeting(w.db, "m", "报价单总价下调 5%，下周一发给客户确认")
    decision_day = at(-2)
    if case == "newer":
        material(
            w.db, w.root, "报价/报价单.xlsx", "总价下调 3%", mtime=decision_day + timedelta(days=1)
        )
    elif case == "stale_key":
        material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%", fresh=False)
    elif case == "recording":
        material(w.db, w.root, "录音/沟通.m4a", "总价下调 3%", start_ms=5_000)
    elif case == "meeting_material":
        material(w.db, w.root, "录音/沟通.m4a", "总价下调 3%", meeting_id="m")
    elif case == "record":
        # 导出的纪要副本：有一段就是这条决议
        material(
            w.db,
            w.root,
            "纪要/周会纪要.docx",
            "报价单总价下调 5%，下周一发给客户确认",
            "旧稿里总价下调 3%",
        )
    elif case == "too_old":
        material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%", mtime=at(-400))
    else:
        material(w.db, w.root, "报价/报价单.xlsx", "总价下调 5%")
    run(w.db)
    assert open_rows(w.db) == []


def test_at_most_three_files_and_none_when_ten_or_more(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%", "驻场改成 2 人")
    for index in range(4):
        material(w.db, w.root, f"报价/报价单{index}.xlsx", "总价下调 3%", mtime=at(-20 + index))
    for index in range(10):
        material(w.db, w.root, f"驻场/排班{index}.xlsx", "驻场 3 人")
    run(w.db)
    # 每条决议最多 3 份，修改时间新的先；10 份以上说明主语太泛，一个都不写
    assert names(w.db) == ["报价单1.xlsx", "报价单2.xlsx", "报价单3.xlsx"]


def test_project_cap_waits_for_room_without_pushing_out(tmp_path):
    w = world(tmp_path)
    parties = ("甲方", "乙方", "丙方", "丁方", "戊方")
    meeting(w.db, "m", *(f"{party}总价下调 5%" for party in parties))
    subjects = [affects.decision_subject(f"{party}总价下调 5%")[0] for party in parties]
    assert subjects[0] == "甲方总价"
    for index, subject in enumerate(subjects):
        for copy in range(3):
            material(w.db, w.root, f"报价/{index}-{copy}.xlsx", f"报价里{subject}按原价下调 3%")
    run(w.db)
    rows = open_rows(w.db)
    assert len(rows) == 12
    scan = w.db.query_one(
        "SELECT affects_hash, section_hash FROM decision_scan WHERE meeting_id = 'm'"
    )
    assert scan["affects_hash"] == f"capped:{scan['section_hash']}"
    # 满了不再到期；有空位了整场重配，补上一个，已经在问的一个不少
    assert run(w.db)["pending"] == 0
    with w.db.transaction() as connection:
        relations.answer(connection, rows[0]["id"], {"answer": "updated"}, utc_now())
    run(w.db, now=at(0.01))
    after = open_rows(w.db)
    assert len(after) == 12
    assert {row["id"] for row in rows[1:]} <= {row["id"] for row in after}


def test_new_chunks_only_add_and_a_changed_section_rematches(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%")
    material(w.db, w.root, "报价/报价单 a.xlsx", "总价下调 3%")
    run(w.db)
    decision_id = w.db.query_one("SELECT id FROM decisions")["id"]
    # 一条这一轮不会再找到的行（比如它的片段那时还在）
    w.db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, decision_id, content_key,
               quote, created_at, updated_at)
           VALUES ('affects', 'p', ?, 'suggested', 'rule', 'm', ?, 'q2:gone', '总价下调 5%', ?, ?)""",
        (f"{decision_id}|q2:gone", decision_id, stamp(at(-1)), stamp(at(-1))),
    )
    material(w.db, w.root, "报价/报价单 b.xlsx", "总价下调 4%")
    result = run(w.db, now=at(0.01))
    assert result["pending"] == 1 and result["written"] == 1 and result["cleared"] == 0
    assert (
        w.db.query_one("SELECT status FROM relations WHERE content_key = 'q2:gone'")["status"]
        == "suggested"
    )
    # 决议段变了：整场重配，clear_missing 收回不再命中的
    w.db.execute("UPDATE decision_scan SET section_hash = 'changed' WHERE meeting_id = 'm'")
    result = run(w.db, now=at(0.02))
    assert result["cleared"] == 1
    assert (
        w.db.query_one("SELECT status FROM relations WHERE content_key = 'q2:gone'")["status"]
        == "cleared"
    )
    assert names(w.db) == ["报价单 a.xlsx", "报价单 b.xlsx"]


def test_time_window_follows_links_since(tmp_path):
    w = world(tmp_path, links_since=at(-10))
    meeting(w.db, "m-old", "总价下调 5%", day=at(-105))
    meeting(w.db, "m-ok", "排期改成 3 周", day=at(-95))
    material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%", mtime=at(-200))
    material(w.db, w.root, "排期/排期表.xlsx", "排期 4 周", mtime=at(-200))
    run(w.db)
    assert names(w.db) == ["排期表.xlsx"]


def test_busy_stops_and_fts_rebuild_skips(tmp_path):
    from meeting_workbench.deep_links import LinksWorker

    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%")
    material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    assert run(w.db, busy=lambda: True)["stopped"] == "busy"
    assert open_rows(w.db) == []
    w.db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES ('material_fts_rebuild', '{}', 'x')"
    )
    settings = SimpleNamespace(links_enabled=True, links_backfill_days=180, semantic_model="x")
    worker = LinksWorker(w.db, settings, now=lambda: NOW)
    assert worker.run_round()["phases"]["affects"] == "off"
    busy = LinksWorker(w.db, settings, now=lambda: NOW, busy=lambda: True)
    assert busy.run_round()["phases"]["affects"] == "busy"
    assert open_rows(w.db) == []


def test_decided_values_keep_only_the_new_value():
    assert [
        (value.kind, value.number) for value in affects.decided_values("驻场人员由 3 人改成 2 人")
    ] == [("count", 2)]
    assert [value.number for value in affects.decided_values("总价从 3% 调到 5%")] == [5]
    # 没有「由」「从」的两个数都是决议自己的
    assert len(affects.decided_values("甲方 3 人、乙方 2 人")) == 2


def test_from_old_value_to_new_value_marks_the_old_file(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "驻场人员由 3 人改成 2 人")
    material(w.db, w.root, "驻场/驻场安排.docx", "驻场人员 3 人")
    run(w.db)
    assert names(w.db) == ["驻场安排.docx"]


@pytest.mark.parametrize(
    ("decision", "passage"),
    [
        ("总价在原基础上下调 5%，含税", "总价在原基础上下调 3%，含税"),
        ("甲方总价下调 5%", "甲方总价下调 3%"),
    ],
)
def test_near_copy_with_the_old_value_is_still_marked(tmp_path, decision, passage):
    """字面和决议几乎一样、只是数不同的一段不是纪要副本，正是要标的旧文件。"""
    w = world(tmp_path)
    meeting(w.db, "m", decision)
    material(w.db, w.root, "报价/报价单 v3.xlsx", passage)
    run(w.db)
    assert names(w.db) == ["报价单 v3.xlsx"]


def test_real_copy_with_the_decided_value_is_not_marked(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "总价在原基础上下调 5%，含税")
    # 导出的纪要：一段就是这条决议（写着 5%），另一段是旧稿的说法
    material(
        w.db, w.root, "纪要/周会纪要.docx", "决议：总价在原基础上下调 5%，含税", "旧稿里总价下调 3%"
    )
    run(w.db)
    assert open_rows(w.db) == []


def _count_statements(db, monkeypatch) -> list[str]:
    seen: list[str] = []
    connect = db.connect

    def traced():
        connection = connect()
        # 全文索引自己的内部语句（-- 开头）不算
        connection.set_trace_callback(
            lambda statement: None if statement.startswith("--") else seen.append(statement)
        )
        return connection

    monkeypatch.setattr(db, "connect", traced)
    return seen


def _many_files(db, root_id: int, files: int, chunks: int, hit: str) -> None:
    filler = "这是一段很长的说明文字，用来凑长度。" * 6
    mtime = ns(at(-20))
    with db.transaction() as connection:
        for index in range(files):
            key = f"q2:many{index:028d}"
            connection.execute(
                """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
                   VALUES (?, 'text', 'done', 100, ?, 'x', 'x')""",
                (key, chunks),
            )
            connection.executemany(
                "INSERT INTO material_chunks(content_key, ordinal, loc, start_ms, text) VALUES (?, ?, '', NULL, ?)",
                [
                    (key, ordinal, hit if ordinal == chunks - 1 else f"{filler} 第{ordinal}段")
                    for ordinal in range(chunks)
                ],
            )
            connection.execute(
                """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns,
                       zone, seen_at, content_key, content_size, content_mtime_ns)
                   VALUES (?, ?, '资料', ?, ?, ?, 'docx', 100, ?, 'normal', 'x', ?, 100, ?)""",
                (
                    root_id,
                    f"资料/文件{index}.docx",
                    f"文件{index}.docx",
                    f"文件{index}",
                    f"文件{index}",
                    mtime,
                    key,
                    mtime,
                ),
            )


def test_record_check_reads_only_the_hit_passages(tmp_path, monkeypatch):
    """纪要副本只看找到主语的那几段，不回表读全文：语句数和文件有多少段无关。"""
    w = world(tmp_path)
    meeting(w.db, "m", "项目预算控制在 12万以内")
    _many_files(w.db, w.root, 5, 200, "项目预算 15万，其余照旧")
    seen = _count_statements(w.db, monkeypatch)
    run(w.db)
    assert len(names(w.db)) == 3
    assert not any("SELECT text FROM material_chunks" in statement for statement in seen)
    assert len(seen) < 45


def test_too_many_files_returns_early(tmp_path, monkeypatch):
    """主语太泛（10 份以上）时找到第 10 份就停，不做副本判断；300 份文件每份 200 段也很快。"""
    import time

    w = world(tmp_path)
    meeting(w.db, "m", "项目预算控制在 12万以内")
    _many_files(w.db, w.root, 60, 100, "项目预算 15万，其余照旧")
    seen = _count_statements(w.db, monkeypatch)
    started = time.monotonic()
    result = run(w.db)
    assert time.monotonic() - started < 1.0
    assert result["written"] == 0 and open_rows(w.db) == []
    assert not any("SELECT text FROM material_chunks" in statement for statement in seen)


def test_round_stops_as_budget_or_busy_and_keeps_the_ledger_whole(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m1", "总价下调 5%", day=at(-1))
    meeting(w.db, "m2", "驻场改成 2 人", "排期改成 3 周", day=at(-2))
    material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    material(w.db, w.root, "驻场/排班.xlsx", "驻场 3 人，排期 4 周")

    def ledger(meeting_id):
        return w.db.query_one(
            "SELECT affects_hash FROM decision_scan WHERE meeting_id = ?", (meeting_id,)
        )["affects_hash"]

    # 时间：第一场会不看时间，一定配完；第二场会一开始就没时间了，什么都不写、台账不动
    ticks = iter(range(0, 1000, 10))
    result = affects.match_due(
        w.db, lambda: False, 5.0, now=NOW, since=stamp(NOW), clock=lambda: next(ticks)
    )
    assert result["stopped"] == "budget" and result["tried"] == 1
    assert names(w.db) == ["报价单.xlsx"] and ledger("m1") is not None and ledger("m2") is None
    # 条数：第二场会要配 2 条，这一轮只剩 1 条，停在它前面（不白做半场）
    w.db.execute("UPDATE decision_scan SET affects_hash = NULL WHERE meeting_id = 'm1'")
    result = run(w.db, max_decisions=2)
    assert result["stopped"] == "budget" and result["tried"] == 1 and ledger("m2") is None
    # 忙：回 busy，不是 budget
    assert run(w.db, busy=lambda: True)["stopped"] == "busy"
    assert ledger("m2") is None
    # 下一轮它排第一，整场配完（第一场会不看条数），台账跟上
    result = run(w.db, max_decisions=1)
    assert result["stopped"] is None and result["tried"] == 2 and ledger("m2") is not None
    assert (
        sorted(set(names(w.db))) == sorted({"排班.xlsx", "报价单.xlsx"})
        and len(open_rows(w.db)) == 3
    )


# ---------------------------------------------------------------------- L3


def l3(db, now: datetime = NOW):
    return affects.auto_clear(db, now, stamp(now))


@pytest.mark.parametrize("case", ["edited", "new_key", "gone", "decision_gone", "superseded"])
def test_auto_clear(tmp_path, case):
    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%")
    file = material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    run(w.db)
    (row,) = open_rows(w.db)
    assert l3(w.db)["cleared"] == 0
    if case == "edited":
        w.db.execute(
            "UPDATE material_files SET mtime_ns = ? WHERE id = ?", (ns(at(-0.1)), file["id"])
        )
    elif case == "new_key":
        w.db.execute(
            "UPDATE material_files SET content_key = 'q2:other' WHERE id = ?", (file["id"],)
        )
    elif case == "gone":
        w.db.execute("UPDATE material_files SET gone_at = 'x' WHERE id = ?", (file["id"],))
    elif case == "decision_gone":
        w.db.execute("UPDATE decisions SET gone_at = 'x'")
    else:
        meeting(w.db, "m2", "总价下调 8%", day=at(-1))
        early = w.db.query_one("SELECT id FROM decisions WHERE meeting_id = 'm'")["id"]
        late = w.db.query_one("SELECT id FROM decisions WHERE meeting_id = 'm2'")["id"]
        w.db.execute(
            """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, decision_id, to_decision_id,
                   created_at, updated_at)
               VALUES ('later_changed', 'p', ?, 'shown', 'llm', 'm2', ?, ?, 'x', 'x')""",
            (f"{early}|{late}", early, late),
        )
    assert l3(w.db, now=at(0.01))["cleared"] == 1
    assert (
        w.db.query_one("SELECT status FROM relations WHERE id = ?", (row["id"],))["status"]
        == "cleared"
    )


def test_auto_clear_walks_500_rows_a_round(tmp_path):
    w = world(tmp_path)
    for index in range(3):
        w.db.execute(
            """INSERT INTO relations(kind, project_id, ident, status, origin, created_at, updated_at)
               VALUES ('affects', 'p', ?, 'suggested', 'rule', 'x', 'x')""",
            (f"d|{index}",),
        )
    first = affects.auto_clear(w.db, NOW, stamp(NOW), limit=2)
    assert first["seen"] == 2 and first["after"] > 0
    second = affects.auto_clear(w.db, NOW, stamp(NOW), limit=2, after=first["after"])
    assert second["seen"] == 1 and second["after"] == 0


# ---------------------------------------------------------------------- 回答


def answer(db, relation_id: int, choice: str, now: str | None = None):
    with db.transaction() as connection:
        return relations.answer(connection, relation_id, {"answer": choice}, now or utc_now())


def test_updated_holds_for_the_content_and_no_holds_for_the_path(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%")
    quote = material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    plan = material(w.db, w.root, "报价/方案.docx", "总价下调 4%")
    run(w.db)
    rows = {row["name"]: row for row in open_rows(w.db)}
    answer(w.db, rows["报价单.xlsx"]["id"], "updated")
    answer(w.db, rows["方案.docx"]["id"], "no")
    # 整场重配：同样的内容不再问；方案换了内容（路径没变）也不为这条决议再问
    material(w.db, w.root, "报价/方案.docx", "总价下调 2%", key="q2:" + "e" * 32)
    w.db.execute("UPDATE decision_scan SET section_hash = 'changed'")
    run(w.db, now=at(0.01))
    assert open_rows(w.db) == []
    statuses = {
        row["content_key"]: row["status"]
        for row in w.db.query_all("SELECT content_key, status FROM relations")
    }
    assert statuses == {quote["content_key"]: "resolved", plan["content_key"]: "rejected"}


def test_undo_after_600_seconds_and_restore(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", "总价下调 5%")
    material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    run(w.db)
    (row,) = open_rows(w.db)
    answered = datetime.now(UTC) - timedelta(seconds=700)
    answer(w.db, row["id"], "no", stamp(answered))
    with pytest.raises(RelationError) as late:
        with w.db.transaction() as connection:
            relations.undo(connection, row["id"], utc_now())
    assert (late.value.status, str(late.value)) == (409, "已超过撤销时间，请直接改回")
    # 接口本身照收 restore（界面上不给入口）
    result = answer(w.db, row["id"], "restore")
    assert result["relation"]["status"] == "suggested"
