"""第四期 4a：关联表的写（系统行、upsert_system、clear_missing、遮盖、合并项目）和回答、撤销。"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meeting_workbench import project_names, relations
from meeting_workbench.db import Database, utc_now
from meeting_workbench.relation_read import live_file
from meeting_workbench.tasks import _delete_deliverable, _insert_deliverable

from .test_file_mentions import add_file
from .test_graph import add_meeting, add_task
from .test_material_index import add_root, make

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


def at(seconds=0):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def setup(tmp_path):
    db, _settings = make(tmp_path)
    root_id = add_root(db, tmp_path / "云图AI")
    add_meeting(db, "m", ago=1, project_id="p")
    return db, root_id


def keyed(db, file_id, key):
    db.execute("UPDATE material_files SET content_key = ? WHERE id = ?", (key, file_id))


def rev(db, key="graph_rev"):
    row = db.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
    return int(row["value"]) if row else 0


def get(db, relation_id):
    return db.query_one("SELECT * FROM relations WHERE id = ?", (relation_id,))


def system_row(**overrides):
    row = {
        "kind": "mention",
        "project_id": "p",
        "ident": "m|报价单",
        "status": "shown",
        "origin": "llm",
        "meeting_id": "m",
        "at_ms": 60_000,
        "stem_key": "报价单",
        "quote": "上周那版报价单再对一下",
        "evidence": {"phrase": "上周那版报价单", "via": "time_hint"},
        "score": 0.8123,
    }
    row.update(overrides)
    return row


def upsert(db, rows, *, now=None, since=None):
    with db.transaction() as connection:
        return relations.upsert_system(connection, rows, now or at(), since=since or at())


def ident_id(db, ident, project_id="p"):
    return db.query_one(
        "SELECT id FROM relations WHERE ident = ? AND project_id = ?", (ident, project_id)
    )["id"]


def answer(db, relation_id, body, now=None):
    with db.transaction() as connection:
        return relations.answer(connection, relation_id, body, now or at(10))


def undo(db, relation_id, now=None):
    with db.transaction() as connection:
        return relations.undo(connection, relation_id, now or at(20))


def error_of(fn, *args, **kwargs):
    with pytest.raises(relations.RelationError) as caught:
        fn(*args, **kwargs)
    return caught.value.status, str(caught.value)


def produced_setup(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "能耗看板/能耗看板方案.key")
    keyed(db, file_id, "k-plan")
    add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    upsert(
        db,
        [
            system_row(
                kind="produced",
                ident="t|k-plan",
                status="suggested",
                origin="rule",
                meeting_id=None,
                task_id="t",
                stem_key=None,
                at_ms=None,
                quote="",
                file_id=file_id,
                content_key="k-plan",
                root_id=root_id,
                rel_path="能耗看板/能耗看板方案.key",
                evidence={
                    "event_kind": "added",
                    "folder": "能耗看板/",
                    "days": 3,
                    "ref": "meeting",
                },
            )
        ],
    )
    return db, root_id, file_id, ident_id(db, "t|k-plan")


# ---------------------------------------------------------------------- 循环的写


def test_unchanged_upserts_fire_nothing_and_related_has_its_own_revision(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    row = system_row(file_id=file_id)
    assert upsert(db, [row]) == 1
    relation_id = ident_id(db, "m|报价单")
    graph0, related0 = rev(db), rev(db, "related_rev")

    # 同样的内容、分数只在两位小数以后变：不写，版本号不动，行 id 不变
    assert upsert(db, [row], now=at(5), since=at(5)) == 0
    assert upsert(db, [{**row, "score": 0.8149}], now=at(6), since=at(6)) == 0
    # evidence 用 canonical 写：键的顺序不同也是同一串
    assert (
        upsert(
            db, [{**row, "evidence": {"via": "time_hint", "phrase": "上周那版报价单"}}], since=at(7)
        )
        == 0
    )
    assert rev(db) == graph0
    assert upsert(db, [{**row, "score": 0.9}], now=at(8), since=at(8)) == 1
    assert rev(db) == graph0 + 1 and ident_id(db, "m|报价单") == relation_id
    assert get(db, relation_id)["evidence_json"] == relations.canonical(row["evidence"])

    related = system_row(
        kind="related", ident="m|k1", origin="vector", stem_key=None, content_key="k1"
    )
    upsert(db, [related], since=at(9))
    assert rev(db) == graph0 + 1 and rev(db, "related_rev") == related0 + 1
    assert upsert(db, [related], since=at(9)) == 0 and rev(db, "related_rev") == related0 + 1


def test_a_running_round_cannot_take_back_your_answer(tmp_path):
    """一轮在 T0 开始读输入；它算的时候你回答了（再撤销也一样），它写回来时什么都不改。"""
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    upsert(db, [system_row(file_id=file_id)], now=at(-60), since=at(-60))
    relation_id = ident_id(db, "m|报价单")
    since = at(0)  # 这一轮开始读输入

    answer(db, relation_id, {"answer": "no"}, now=at(1))
    before = rev(db)
    stale = system_row(file_id=file_id, quote="这一轮算出来的另一句", score=0.95)
    assert upsert(db, [stale], now=at(2), since=since) == 0
    assert get(db, relation_id)["status"] == "rejected" and rev(db) == before

    # 撤销以后回到 shown，updated_at 晚于这一轮的 since：这一轮既不改它，也不因为不在 keep 里收回它
    undo(db, relation_id, now=at(3))
    assert get(db, relation_id)["status"] == "shown"
    assert upsert(db, [stale], now=at(4), since=since) == 0
    with db.transaction() as connection:
        assert (
            relations.clear_missing(
                connection, "mention", "p", {"meeting_id": "m"}, [], at(4), since=since
            )
            == 0
        )
    assert (
        get(db, relation_id)["status"] == "shown"
        and get(db, relation_id)["quote"] == system_row()["quote"]
    )

    # 下一轮（since 晚于你的撤销）照常收回
    with db.transaction() as connection:
        assert (
            relations.clear_missing(
                connection, "mention", "p", {"meeting_id": "m"}, [], at(9), since=at(8)
            )
            == 1
        )
    assert get(db, relation_id)["status"] == "cleared"


def test_answer_and_round_race_on_the_same_row(tmp_path):
    """真的两条连接：一轮读完输入后，你的回答先提交；这一轮的写在它后面，不覆盖你的回答。"""
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    upsert(db, [system_row(file_id=file_id)], now=at(-60), since=at(-60))
    relation_id = ident_id(db, "m|报价单")
    round_since = utc_now()
    loop = db.connect()
    try:
        # 这一轮读了输入（还在算）
        assert (
            loop.execute("SELECT status FROM relations WHERE id = ?", (relation_id,)).fetchone()[0]
            == "shown"
        )
        answer(db, relation_id, {"answer": "no"}, now=utc_now())
        loop.execute("BEGIN IMMEDIATE")
        written = relations.upsert_system(
            loop, [system_row(file_id=file_id, quote="算完的另一句")], utc_now(), since=round_since
        )
        loop.commit()
    finally:
        loop.close()
    assert written == 0
    assert get(db, relation_id)["status"] == "rejected"


def test_system_rows_only(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    upsert(db, [system_row(file_id=file_id)], now=at(-60), since=at(-60))
    relation_id = ident_id(db, "m|报价单")
    # 你手动换过的行（origin manual）：循环不改、不收回
    db.execute("UPDATE relations SET origin = 'manual' WHERE id = ?", (relation_id,))
    assert upsert(db, [system_row(file_id=None, status="cleared")], since=at(5)) == 0
    with db.transaction() as connection:
        assert (
            relations.clear_missing(connection, "mention", "p", None, [], at(5), since=at(5)) == 0
        )
    assert get(db, relation_id)["status"] == "shown" and get(db, relation_id)["file_id"] == file_id
    assert (
        relations.SYSTEM_ROW_SQL
        == "origin != 'manual' AND status IN ('shown', 'suggested', 'cleared')"
    )


def test_cleared_produced_is_never_raised_again(tmp_path):
    db, _root_id, file_id, relation_id = produced_setup(tmp_path)
    with db.transaction() as connection:
        relations.clear_missing(
            connection, "produced", "p", {"task_id": "t"}, [], at(5), since=at(5)
        )
    assert get(db, relation_id)["status"] == "cleared"
    again = system_row(
        kind="produced",
        ident="t|k-plan",
        status="suggested",
        origin="rule",
        meeting_id=None,
        task_id="t",
        stem_key=None,
        quote="",
        file_id=file_id,
        content_key="k-plan",
        evidence={"days": 4},
    )
    assert upsert(db, [again], since=at(9)) == 0
    assert get(db, relation_id)["status"] == "cleared"

    # 别的类收回以后，条件又成立时会再出现
    add_file(db, _root_id, "报价单.xlsx")
    upsert(db, [system_row()], since=at(9))
    mention_id = ident_id(db, "m|报价单")
    with db.transaction() as connection:
        relations.clear_missing(
            connection, "mention", "p", {"meeting_id": "m"}, [], at(10), since=at(10)
        )
    assert get(db, mention_id)["status"] == "cleared"
    assert upsert(db, [system_row(quote="又说到了")], now=at(11), since=at(11)) == 1
    assert get(db, mention_id)["status"] == "shown"


def test_rejections_block_new_rows_for_the_same_file(tmp_path):
    db, root_id, file_id, relation_id = produced_setup(tmp_path)
    answer(db, relation_id, {"answer": "no"})
    # 文件改过、有了新的内容标识：同一条任务、同一个位置仍然挡住
    changed = system_row(
        kind="produced",
        ident="t|k-plan-2",
        status="suggested",
        origin="rule",
        meeting_id=None,
        task_id="t",
        stem_key=None,
        file_id=file_id,
        content_key="k-plan-2",
        root_id=root_id,
        rel_path="能耗看板/能耗看板方案.key",
    )
    assert upsert(db, [changed], since=at(30)) == 0
    with db.autocommit() as connection:
        assert relations.is_rejected(
            connection, "produced", "p", "t", "k-other", root_id, "能耗看板/能耗看板方案.key"
        )
        assert relations.is_rejected(connection, "produced", "p", "t", "k-plan", None, None)
        assert not relations.is_rejected(connection, "produced", "p", "t2", "k-plan", root_id, "x")
    add_task(db, "t2", meeting_id="m", project_id="p", status="confirmed")
    assert upsert(db, [{**changed, "ident": "t2|k-plan-2", "task_id": "t2"}], since=at(30)) == 1


def test_loosened_mentions_step_aside_for_literal_ones(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
               anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('m', 'p', '报价单', ?, '报价单', 1, 0, '[0]', 0, 'transcript', 'rejected', 0, ?)""",
        (file_id, utc_now()),
    )
    # 同一 (会议, 项目, 词干) 已经有字面行（驳回的也算）：不写放宽的
    assert upsert(db, [system_row(file_id=file_id)]) == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM relations")["n"] == 0


def test_insert_or_replace_is_banned():
    package = Path(relations.__file__).resolve().parent
    banned = re.compile(
        r"\bOR\s+REPLACE\s+(INTO\s+)?relations\b|\bREPLACE\s+INTO\s+relations\b", re.IGNORECASE
    )
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(package.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if banned.search(line)
    ]
    assert offenders == []


# ---------------------------------------------------------------------- 回答


def _row_for(db, root_id, kind):
    file_id = add_file(db, root_id, f"{kind}/{kind}材料.docx")
    keyed(db, file_id, f"k-{kind}")
    stem = db.query_one("SELECT stem_key FROM material_files WHERE id = ?", (file_id,))["stem_key"]
    if kind in ("affects", "later_changed", "restated"):
        for decision_id in ("dec-a", "dec-b"):
            db.execute(
                """INSERT OR IGNORE INTO decisions(id, meeting_id, ordinal, text, text_key, created_at, updated_at)
                   VALUES (?, 'm', 0, '总价下调 5%', '总价下调5', ?, ?)""",
                (decision_id, utc_now(), utc_now()),
            )
    if kind == "produced":
        add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    base = {
        "mention": {"ident": f"m|{stem}", "stem_key": stem},
        "related": {"ident": "m|k-related", "origin": "vector", "stem_key": None},
        "produced": {
            "ident": "t|k-produced",
            "status": "suggested",
            "origin": "rule",
            "meeting_id": None,
            "task_id": "t",
            "stem_key": None,
        },
        "affects": {
            "ident": "dec-a|k-affects",
            "status": "suggested",
            "origin": "rule",
            "decision_id": "dec-a",
            "stem_key": None,
        },
        "later_changed": {
            "ident": "dec-a|dec-b",
            "origin": "rule",
            "decision_id": "dec-a",
            "to_decision_id": "dec-b",
            "stem_key": None,
        },
        "restated": {
            "ident": "dec-a|dec-b",
            "origin": "llm",
            "decision_id": "dec-a",
            "to_decision_id": "dec-b",
            "stem_key": None,
        },
    }[kind]
    target = (
        {}
        if kind in ("later_changed", "restated")
        else {
            "file_id": file_id,
            "content_key": f"k-{kind}",
            "root_id": root_id,
            "rel_path": f"{kind}/{kind}材料.docx",
        }
    )
    upsert(db, [system_row(kind=kind, **{**base, **target})], now=at(-60), since=at(-60))
    return ident_id(db, base["ident"]), file_id


@pytest.mark.parametrize("kind", relations.KINDS)
@pytest.mark.parametrize("choice", ["yes", "no", "updated", "pick", "restore"])
def test_every_kind_and_answer(tmp_path, kind, choice):
    db, root_id = setup(tmp_path)
    relation_id, file_id = _row_for(db, root_id, kind)
    body = {"answer": choice}
    if choice == "pick":
        body["file_id"] = add_file(db, root_id, f"另一处/{kind}材料.docx")
    if choice == "restore":
        # 刚出来的行不能 restore
        assert error_of(answer, db, relation_id, body) == (409, "这条已经处理过了")
        return
    if choice not in relations.ANSWERS[kind]:
        assert error_of(answer, db, relation_id, body) == (422, "这类关联不能这样回答")
        assert get(db, relation_id)["status"] == relations.FIRST_STATUS[kind]
        return
    result = answer(db, relation_id, body)
    relation = result["relation"]
    assert relation["status"] == relations.ANSWERS[kind][choice]
    assert relation["by_you"] is True and relation["decided_at"] == at(10)
    assert result["undo_until"] == "2026-09-27T08:10:10Z"
    assert not {"score", "prev_json", "root_id", "evidence_json", "origin"} & set(relation)
    if relation["status"] != relations.FIRST_STATUS[kind]:
        assert error_of(answer, db, relation_id, body) == (409, "这条已经处理过了")
    # 撤销改回第一个状态
    assert undo(db, relation_id)["relation"]["status"] == relations.FIRST_STATUS[kind]
    if relations.ANSWERS[kind][choice] in relations.RESTORABLE:
        answer(db, relation_id, body, now=at(30))
        restored = answer(db, relation_id, {"answer": "restore"}, now=at(40))
        assert restored["relation"]["status"] == relations.FIRST_STATUS[kind]
        # 撤销恢复：回到你刚才的回答
        assert (
            undo(db, relation_id, now=at(50))["relation"]["status"]
            == relations.ANSWERS[kind][choice]
        )


def test_conflicts_and_gone(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    upsert(db, [system_row(file_id=file_id)], now=at(-60), since=at(-60))
    relation_id = ident_id(db, "m|报价单")
    assert error_of(answer, db, 999, {"answer": "no"}) == (404, "这条关联已经不在了")
    assert error_of(undo, db, 999) == (404, "这条关联已经不在了")
    assert error_of(undo, db, relation_id) == (409, "已经撤销过了")
    assert error_of(answer, db, relation_id, {"answer": "maybe"}) == (422, "这类关联不能这样回答")

    # 循环刚收回
    with db.transaction() as connection:
        relations.clear_missing(
            connection, "mention", "p", {"meeting_id": "m"}, [], at(0), since=at(0)
        )
    assert error_of(answer, db, relation_id, {"answer": "no"}) == (409, "这条已经处理过了")

    # 回答、撤销、再撤销
    upsert(db, [system_row(file_id=file_id, quote="又说到了")], now=at(1), since=at(1))
    answer(db, relation_id, {"answer": "no"})
    undo(db, relation_id)
    assert error_of(undo, db, relation_id) == (409, "已经撤销过了")

    # 超过 600 秒
    answer(db, relation_id, {"answer": "no"}, now=at(100))
    assert error_of(undo, db, relation_id, now=at(701)) == (409, "已超过撤销时间，请直接改回")
    assert undo(db, relation_id, now=at(699))["relation"]["status"] == "shown"

    # 那场会已经离开这个项目：你标过的行留着，但不能在这个项目里再改
    answer(db, relation_id, {"answer": "no"}, now=at(800))
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),)
    )
    db.execute("UPDATE meetings SET project_id = 'q' WHERE id = 'm'")
    assert error_of(undo, db, relation_id, now=at(810)) == (409, "这条关联已经不在这个项目里了")
    assert error_of(answer, db, relation_id, {"answer": "restore"}, now=at(810)) == (
        409,
        "这条关联已经不在这个项目里了",
    )


def test_pick_and_its_undo(tmp_path):
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价/报价单 v1.xlsx")
    v2 = add_file(db, root_id, "报价/报价单 v2.xlsx")
    other = add_file(db, root_id, "需求说明书.docx")
    keyed(db, v1, "k-v1")
    keyed(db, v2, "k-v2")
    upsert(
        db,
        [
            system_row(
                file_id=v1, content_key="k-v1", root_id=root_id, rel_path="报价/报价单 v1.xlsx"
            )
        ],
        now=at(-60),
        since=at(-60),
    )
    relation_id = ident_id(db, "m|报价单")
    wrong = "只能换成这个项目文件夹里同名的另一份文件"
    assert error_of(answer, db, relation_id, {"answer": "pick", "file_id": other}) == (422, wrong)
    assert error_of(answer, db, relation_id, {"answer": "pick"}) == (422, wrong)
    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), v2))
    assert error_of(answer, db, relation_id, {"answer": "pick", "file_id": v2}) == (422, wrong)
    db.execute("UPDATE material_files SET gone_at = NULL WHERE id = ?", (v2,))

    result = answer(db, relation_id, {"answer": "pick", "file_id": v2})
    assert result["relation"]["status"] == "shown" and result["relation"]["file"] == {
        "id": v2,
        "name": "报价单 v2.xlsx",
    }
    row = get(db, relation_id)
    assert (row["origin"], row["file_id"], row["content_key"], row["rel_path"]) == (
        "manual",
        v2,
        "k-v2",
        "报价/报价单 v2.xlsx",
    )
    prev = json.loads(row["prev_json"])
    assert prev["file_id"] == v1 and prev["origin"] == "llm" and prev["content_key"] == "k-v1"
    # 换过的行循环不再改
    assert upsert(db, [system_row(file_id=v1)], since=at(30)) == 0

    back = undo(db, relation_id)
    row = get(db, relation_id)
    assert back["relation"]["file"]["id"] == v1
    assert (row["origin"], row["file_id"], row["content_key"], row["prev_json"]) == (
        "llm",
        v1,
        "k-v1",
        None,
    )


def test_yes_registers_a_deliverable_and_undo_removes_it(tmp_path):
    db, root_id, file_id, relation_id = produced_setup(tmp_path)
    before = rev(db)
    result = answer(db, relation_id, {"answer": "yes"})
    deliverable_id = result["deliverable_id"]
    assert result["relation"]["status"] == "confirmed" and deliverable_id
    deliverable = db.query_one("SELECT * FROM deliverables WHERE id = ?", (deliverable_id,))
    assert deliverable["kind"] == "file" and deliverable["title"] == "能耗看板方案.key"
    assert deliverable["url"] == f"{tmp_path / '云图AI'}/能耗看板/能耗看板方案.key"
    assert db.query_one(
        "SELECT * FROM deliverable_files WHERE deliverable_id = ?", (deliverable_id,)
    )["content_key"] == ("k-plan")
    events = [
        row["kind"]
        for row in db.query_all("SELECT kind FROM task_events WHERE task_id = 't' ORDER BY id")
    ]
    assert events == ["deliverable_added"]
    row = get(db, relation_id)
    assert (
        row["deliverable_id"] == deliverable_id
        and json.loads(row["prev_json"])["created_deliverable"] is True
    )
    assert rev(db) > before  # 交付物、关联都进 graph_rev

    removed = undo(db, relation_id)
    assert removed["removed_deliverable_id"] == deliverable_id
    assert removed["relation"]["status"] == "suggested"
    assert db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM deliverable_files")["n"] == 0
    events = [
        row["kind"]
        for row in db.query_all("SELECT kind FROM task_events WHERE task_id = 't' ORDER BY id")
    ]
    assert events == ["deliverable_added", "deliverable_removed"]
    assert get(db, relation_id)["deliverable_id"] is None


def test_yes_on_an_existing_deliverable_links_it_only(tmp_path):
    db, root_id, file_id, relation_id = produced_setup(tmp_path)
    with db.transaction() as connection:
        existing = _insert_deliverable(
            connection,
            "t",
            name="能耗看板方案.key",
            content_key="k-plan",
            root_id=root_id,
            rel_path="能耗看板/能耗看板方案.key",
            now=utc_now(),
        )
    result = answer(db, relation_id, {"answer": "yes"})
    assert result["deliverable_id"] == existing
    assert db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 1
    assert json.loads(get(db, relation_id)["prev_json"])["created_deliverable"] is False
    removed = undo(db, relation_id)
    assert removed["removed_deliverable_id"] is None
    assert db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 1
    assert (
        get(db, relation_id)["deliverable_id"] is None
        and get(db, relation_id)["status"] == "suggested"
    )


def test_undo_after_the_deliverable_was_removed_in_the_drawer(tmp_path):
    db, _root_id, _file_id, relation_id = produced_setup(tmp_path)
    deliverable_id = answer(db, relation_id, {"answer": "yes"})["deliverable_id"]
    with db.transaction() as connection:
        _delete_deliverable(connection, "t", deliverable_id, utc_now())
    assert get(db, relation_id)["deliverable_id"] is None  # 外键置空，行仍是 confirmed
    assert get(db, relation_id)["status"] == "confirmed"
    result = undo(db, relation_id)
    assert result["removed_deliverable_id"] is None and result["relation"]["status"] == "suggested"


def test_undo_rolls_back_when_removing_the_deliverable_fails(tmp_path, monkeypatch):
    db, _root_id, _file_id, relation_id = produced_setup(tmp_path)
    answer(db, relation_id, {"answer": "yes"})

    def broken(*_args, **_kwargs):
        raise RuntimeError("删不掉")

    monkeypatch.setattr(relations, "_delete_deliverable", broken)
    with pytest.raises(RuntimeError):
        undo(db, relation_id)
    row = get(db, relation_id)
    assert (
        row["status"] == "confirmed"
        and row["prev_json"] is not None
        and row["deliverable_id"] is not None
    )


def test_yes_on_a_file_that_is_gone(tmp_path):
    db, _root_id, file_id, relation_id = produced_setup(tmp_path)
    db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), file_id))
    assert error_of(answer, db, relation_id, {"answer": "yes"}) == (422, "这份文件已经不在了")
    assert db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 0


# ---------------------------------------------------------------------- 离开项目、合并、删根目录


def _decision(db, decision_id, meeting_id):
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, created_at, updated_at)
           VALUES (?, ?, 0, '阈值改成 0.7', '阈值改成07', ?, ?)""",
        (decision_id, meeting_id, utc_now(), utc_now()),
    )


def test_leaving_the_project_drops_only_system_rows(tmp_path):
    db, root_id = setup(tmp_path)
    add_meeting(db, "m2", ago=3, project_id="p")
    _decision(db, "dec-m", "m")
    _decision(db, "dec-m2", "m2")
    file_id = add_file(db, root_id, "报价单.xlsx")
    upsert(
        db,
        [
            system_row(file_id=file_id),
            system_row(ident="m|需求说明书", stem_key="需求说明书"),
            system_row(ident="m|周报", stem_key="周报"),
            system_row(
                kind="related", ident="m|k1", origin="vector", stem_key=None, content_key="k1"
            ),
            # 两条决议之间的标记：前一条或后一条在这场会里
            system_row(
                kind="later_changed",
                ident="dec-m|dec-m2",
                origin="rule",
                meeting_id=None,
                decision_id="dec-m",
                to_decision_id="dec-m2",
                stem_key=None,
            ),
            system_row(
                kind="restated",
                ident="dec-m2|dec-m",
                origin="llm",
                meeting_id=None,
                decision_id="dec-m2",
                to_decision_id="dec-m",
                stem_key=None,
            ),
            system_row(
                kind="restated", ident="m2|other", origin="llm", meeting_id="m2", stem_key=None
            ),
        ],
        now=at(-60),
        since=at(-60),
    )
    answer(db, ident_id(db, "m|需求说明书"), {"answer": "no"})
    db.execute("UPDATE relations SET origin = 'manual' WHERE ident = 'm|周报'")
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),)
    )

    db.execute("UPDATE meetings SET project_id = 'q' WHERE id = 'm'")
    left = {
        row["ident"]: row["status"] for row in db.query_all("SELECT ident, status FROM relations")
    }
    assert left == {"m|需求说明书": "rejected", "m|周报": "shown", "m2|other": "shown"}

    # 撤销改归属把会挪回来：你的回答照样生效
    db.execute("UPDATE meetings SET project_id = 'p' WHERE id = 'm'")
    assert get(db, ident_id(db, "m|需求说明书"))["status"] == "rejected"


def test_merge_keeps_answers_and_the_2d_rejections(tmp_path):
    db, root_id = setup(tmp_path)
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),)
    )
    add_root(db, tmp_path / "数据中台", project_id="q")
    file_id = add_file(db, root_id, "报价单.xlsx")
    picked_id = add_file(db, root_id, "需求说明书 v2.docx")
    add_meeting(db, "mq", ago=2, project_id="q")
    now = utc_now()
    # 2d：一条「不是这份文件」，一条你换过的 picked=1
    for stem, file, status, picked in (
        ("周报", file_id, "rejected", 0),
        ("需求说明书", picked_id, "active", 1),
    ):
        db.execute(
            """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count,
                   first_ms, anchors_json, minutes_count, source, status, picked, updated_at)
               VALUES ('m', 'p', ?, ?, ?, 1, 0, '[0]', 0, 'transcript', ?, ?, ?)""",
            (stem, file, stem, status, picked, now),
        )
    upsert(
        db,
        [
            system_row(file_id=file_id),
            system_row(ident="m|方案", stem_key="方案"),
            system_row(ident="m|会议纪要", stem_key="会议纪要"),
            system_row(project_id="q", ident="m|报价单", quote="目标项目的系统行"),
            system_row(project_id="q", ident="m|会议纪要", quote="目标项目也回答过"),
        ],
        now=at(-60),
        since=at(-60),
    )
    answer(db, ident_id(db, "m|报价单"), {"answer": "no"})
    db.execute("UPDATE relations SET origin = 'manual' WHERE ident = 'm|方案' AND project_id = 'p'")
    # 两边都回答过同一条：留目标项目的
    answer(db, ident_id(db, "m|会议纪要", "p"), {"answer": "no"}, now=at(12))
    db.execute(
        "UPDATE relations SET status = 'rejected', decided_at = ? WHERE ident = 'm|会议纪要' AND project_id = 'q'",
        (at(11),),
    )
    # 候选词：源项目点过［不是］，目标项目还有一条同名的待认词
    for project_id, status in (("p", "rejected"), ("q", "pending")):
        db.execute(
            """INSERT INTO glossary_candidates(project_id, term, term_key, wrong, status, created_at, updated_at)
               VALUES (?, '能耗看板', '能耗看板', '', ?, ?, ?)""",
            (project_id, status, now, now),
        )

    with db.transaction() as connection:
        project_names.merge_project(connection, "p", "q")

    rows = {
        (row["ident"], row["project_id"]): row for row in db.query_all("SELECT * FROM relations")
    }
    assert rows[("m|报价单", "q")]["status"] == "rejected"  # 源项目的回答胜过目标项目的系统行
    assert (
        rows[("m|方案", "q")]["origin"] == "manual" and rows[("m|方案", "q")]["status"] == "shown"
    )
    assert rows[("m|会议纪要", "q")]["decided_at"] == at(11)
    assert all(project == "q" for _ident, project in rows)
    mentions = {row["stem_key"]: row for row in db.query_all("SELECT * FROM meeting_file_mentions")}
    assert mentions["周报"]["project_id"] == "q" and mentions["周报"]["status"] == "rejected"
    assert mentions["需求说明书"]["project_id"] == "q" and mentions["需求说明书"]["picked"] == 1
    assert db.query_all("SELECT project_id, status FROM glossary_candidates") == [
        {"project_id": "q", "status": "rejected"}
    ]


def test_removing_a_root_clears_the_file_cache(tmp_path):
    db, root_id = setup(tmp_path)
    copy_root = add_root(db, tmp_path / "云图AI 备份")
    file_id = add_file(db, root_id, "报价单.xlsx")
    copy_id = add_file(db, copy_root, "发客户/报价单.xlsx")
    keyed(db, file_id, "k-quote")
    keyed(db, copy_id, "k-quote")
    upsert(
        db,
        [
            system_row(
                file_id=file_id, content_key="k-quote", root_id=root_id, rel_path="报价单.xlsx"
            ),
            system_row(
                kind="related",
                ident="m|k-quote",
                origin="vector",
                stem_key=None,
                file_id=file_id,
                content_key="k-quote",
                root_id=root_id,
                rel_path="报价单.xlsx",
            ),
        ],
    )
    manual_id = ident_id(db, "m|报价单")
    db.execute("UPDATE relations SET origin = 'manual' WHERE id = ?", (manual_id,))
    db.execute("DELETE FROM project_material_roots WHERE id = ?", (root_id,))
    rows = db.query_all("SELECT kind, file_id, origin, status FROM relations ORDER BY id")
    assert [row["file_id"] for row in rows] == [None, None]
    assert all(row["status"] == "shown" for row in rows)
    # 按内容标识在同项目别的根目录里找回（L2 就是这样重找的）
    with db.autocommit() as connection:
        for row in connection.execute("SELECT * FROM relations").fetchall():
            assert live_file(connection, row)["id"] == copy_id
    db.execute("DELETE FROM project_material_roots WHERE id = ?", (copy_root,))
    with db.autocommit() as connection:
        assert all(
            live_file(connection, row) is None
            for row in connection.execute("SELECT * FROM relations")
        )


# ---------------------------------------------------------------------- 接口


def test_answer_and_undo_endpoints(tmp_path):
    from .test_tasks_api import make_client, write_headers

    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    root_id = add_root(db, tmp_path / "云图AI")
    file_id = add_file(db, root_id, "报价单.xlsx")
    add_meeting(db, "m", ago=1, project_id="p")
    upsert(db, [system_row(file_id=file_id)], now=at(-60), since=at(-60))
    relation_id = ident_id(db, "m|报价单")

    response = client.post(
        f"/api/relations/{relation_id}/answer", json={"answer": "no"}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["relation"]["status"] == "rejected" and body["deliverable_id"] is None
    assert body["undo_until"].endswith("Z")
    assert set(body["relation"]) == {
        "id",
        "kind",
        "status",
        "by_you",
        "meeting_id",
        "at_ms",
        "task_id",
        "decision_id",
        "to_decision_id",
        "file",
        "quote",
        "evidence",
        "decided_at",
    }
    assert body["relation"]["evidence"] == {"phrase": "上周那版报价单", "via": "time_hint"}
    again = client.post(
        f"/api/relations/{relation_id}/answer", json={"answer": "no"}, headers=headers
    )
    assert (again.status_code, again.json()["detail"]) == (409, "这条已经处理过了")
    wrong = client.post(
        f"/api/relations/{relation_id}/answer", json={"answer": "yes"}, headers=headers
    )
    assert (wrong.status_code, wrong.json()["detail"]) == (422, "这类关联不能这样回答")

    undone = client.post(f"/api/relations/{relation_id}/undo", json={}, headers=headers)
    assert undone.status_code == 200 and undone.json()["relation"]["status"] == "shown"
    assert undone.json()["removed_deliverable_id"] is None
    twice = client.post(f"/api/relations/{relation_id}/undo", json={}, headers=headers)
    assert (twice.status_code, twice.json()["detail"]) == (409, "已经撤销过了")
    missing = client.post("/api/relations/999/answer", json={"answer": "no"}, headers=headers)
    assert (missing.status_code, missing.json()["detail"]) == (404, "这条关联已经不在了")
    # 写入校验：没有 CSRF 头不收
    assert (
        client.post(f"/api/relations/{relation_id}/answer", json={"answer": "no"}).status_code
        == 403
    )
