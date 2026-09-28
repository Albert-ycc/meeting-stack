"""第四期 4c：L1 里的两步（规则版「后来又提到」、原话对不上的收回）、决议放在哪个需求下、需求页「决议」卡
（requirement_log）、简报和展开一场会带上的「后来改了」。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting_workbench import decisions, graph, relations
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.main import create_app

from .helpers import count_reads
from .test_decision_pairs import NEW_TEXT, NOW, OLD_TEXT, decision_id, ingest, make, meeting, minutes, pair_rows, scan
from .test_decisions import set_minutes
from .test_graph import add_project, add_requirement
from .test_tasks_api import FakeRelayClient

LIVE = SimpleNamespace(links_enabled=True, links_llm_enabled=True, links_llm_daily_calls=200)
SAME = "初审规则周五上线"


def link(db, requirement_id, meeting_id):
    db.execute("INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
               (requirement_id, meeting_id, NOW.isoformat()))


def changed_row(db, early, late, *, why_earlier="先按 0.8", why_later="改成 0.7", status="shown"):
    """直接写一行 AI 的「后来改了」（对比任务在 test_decision_pairs 里另测）。"""
    early_id, late_id = decision_id(db, early), decision_id(db, late)
    with db.transaction() as connection:
        relations.upsert_system(
            connection,
            [{
                "kind": "later_changed", "project_id": "p", "ident": f"{early_id}|{late_id}", "status": "shown",
                "origin": "llm", "meeting_id": late, "at_ms": 30_000, "decision_id": early_id,
                "to_decision_id": late_id, "quote": why_later,
                "evidence": {"why_earlier": why_earlier, "why_later": why_later},
            }],
            NOW.isoformat(),
            since=NOW.isoformat(),
        )
        if status != "shown":
            connection.execute("UPDATE relations SET status = ?, decided_at = ? WHERE kind = 'later_changed'",
                               (status, NOW.isoformat()))
    return db.query_one("SELECT * FROM relations WHERE kind = 'later_changed' ORDER BY id DESC LIMIT 1")


def world(tmp_path):
    """r1 初审规则 V2：旧会、新会只关联它；第三场关联 r1 和 r2（驻场排班）。"""
    db = make(tmp_path)
    add_requirement(db, "r1", "p", "初审规则 V2")
    add_requirement(db, "r2", "p", "驻场排班")
    meeting(db, "old", f"{OLD_TEXT} [00:12:34]", SAME, ago=10, title="周会甲")
    meeting(db, "new", f"{NEW_TEXT} [00:00:30]", SAME, ago=2, title="周会乙")
    meeting(db, "third", "驻场排班改成两班", "下周起统一口径", ago=1, title="周会丙")
    for requirement_id, meeting_id in (("r1", "old"), ("r1", "new"), ("r1", "third"), ("r2", "third")):
        link(db, requirement_id, meeting_id)
    ingest(db)
    return db


def log(db, requirement_id="r1", settings=LIVE):
    with db.autocommit() as connection:
        return decisions.requirement_log(connection, requirement_id, settings=settings)


# ---------------------------------------------------------------------- L1 的两步


def test_rule_restated_needs_no_ai_and_is_cleared_when_the_text_changes(tmp_path):
    db = world(tmp_path)

    (row,) = db.query_all("SELECT * FROM relations WHERE kind = 'restated'")
    assert (row["origin"], row["status"]) == ("rule", "shown")
    assert (row["decision_id"], row["to_decision_id"]) == (decision_id(db, "old", 1), decision_id(db, "new", 1))
    assert row["meeting_id"] == "new"

    # 同一天再跑一轮什么都不写
    before = db.query_one("SELECT updated_at FROM relations WHERE id = ?", (row["id"],))
    ingest(db)
    assert db.query_one("SELECT updated_at FROM relations WHERE id = ?", (row["id"],)) == before

    # 新会那一条改了说法：规则对不上了，收回
    set_minutes(db, "new", "mv-new-2", minutes(f"{NEW_TEXT} [00:00:30]", "初审规则下周五上线"), kind="generated")
    ingest(db, now=NOW + timedelta(minutes=1))
    assert db.query_one("SELECT status FROM relations WHERE id = ?", (row["id"],)) == {"status": "cleared"}


def test_short_identical_decisions_are_not_restated(tmp_path):
    db = make(tmp_path)
    meeting(db, "a", "照旧", ago=5)
    meeting(db, "b", "照旧", ago=2)
    ingest(db)
    assert db.query_all("SELECT * FROM relations") == []


def test_typo_keeps_pair_state_and_material_change_resets_it(tmp_path):
    db = world(tmp_path)
    db.execute("UPDATE decision_scan SET pair_state = 'done'")
    set_minutes(db, "old", "mv-old-2", minutes(f"{OLD_TEXT}  [00:12:34]", SAME), kind="draft")
    ingest(db, now=NOW + timedelta(minutes=1))
    assert scan(db, "old")["pair_state"] == "done"
    set_minutes(db, "old", "mv-old-3", minutes("阈值先按 0.9 执行 [00:12:34]", SAME), kind="generated")
    ingest(db, now=NOW + timedelta(minutes=2))
    assert scan(db, "old")["pair_state"] == "pending"


def test_quote_mismatch_clears_at_once_but_rejected_stays(tmp_path):
    db = world(tmp_path)
    shown = changed_row(db, "old", "new")
    # 旧会那一条实质改了，存的原话「先按 0.8」对不上了：立刻收回
    set_minutes(db, "old", "mv-old-2", minutes("阈值先按 0.9 执行 [00:12:34]", SAME), kind="generated")
    ingest(db, now=NOW + timedelta(minutes=1))
    assert db.query_one("SELECT status FROM relations WHERE id = ?", (shown["id"],)) == {"status": "cleared"}

    # 你标过［不是一回事］的：纪要实质改动以后仍是 rejected
    db2 = world(tmp_path / "second")
    rejected = changed_row(db2, "old", "new", status="rejected")
    set_minutes(db2, "old", "mv-old-2", minutes("阈值先按 0.9 执行 [00:12:34]", SAME), kind="generated")
    ingest(db2, now=NOW + timedelta(minutes=1))
    assert db2.query_one("SELECT status FROM relations WHERE id = ?", (rejected["id"],)) == {"status": "rejected"}


def test_gone_decision_clears_its_marks(tmp_path):
    db = world(tmp_path)
    shown = changed_row(db, "old", "new")
    set_minutes(db, "new", "mv-new-2", minutes(SAME), kind="generated")
    ingest(db, now=NOW + timedelta(minutes=1))
    assert db.query_one("SELECT status FROM relations WHERE id = ?", (shown["id"],)) == {"status": "cleared"}


def test_ai_restated_row_that_becomes_the_same_text_stays_shown(tmp_path):
    """AI 写的「后来又提到」，后来那条改成和前一条一字不差：规则接手这一行，仍显示后来又提到。"""
    db = make(tmp_path)
    add_requirement(db, "r1", "p", "初审规则 V2")
    meeting(db, "a", "阈值先按 0.8 执行 [00:12:34]", ago=10, title="周会甲")
    meeting(db, "b", "阈值还是按 0.8 执行 [00:00:30]", ago=2, title="周会乙")
    link(db, "r1", "a")
    link(db, "r1", "b")
    ingest(db)
    early_id, late_id = decision_id(db, "a"), decision_id(db, "b")
    with db.transaction() as connection:
        relations.upsert_system(
            connection,
            [{
                "kind": "restated", "project_id": "p", "ident": f"{early_id}|{late_id}", "status": "shown",
                "origin": "llm", "meeting_id": "b", "at_ms": 30_000, "decision_id": early_id,
                "to_decision_id": late_id, "quote": "还是按 0.8",
                "evidence": {"why_earlier": "先按 0.8", "why_later": "还是按 0.8"},
            }],
            NOW.isoformat(),
            since=NOW.isoformat(),
        )

    set_minutes(db, "b", "mv-b-2", minutes("阈值先按 0.8 执行 [00:00:30]"), kind="generated")
    ingest(db, now=NOW + timedelta(minutes=1))
    assert decision_id(db, "b") == late_id
    (row,) = pair_rows(db, "restated")
    assert row["status"] == "shown"

    # 再跑几轮也不会被收回
    ingest(db, now=NOW + timedelta(minutes=2))
    assert pair_rows(db, "restated")[0]["status"] == "shown"
    (group,) = log(db)["meetings"][-1]["decisions"]
    assert [item["meeting"]["id"] for item in group["restated"]] == ["b"]


# ---------------------------------------------------------------------- 放在哪个需求下


def test_automatic_placement_rules():
    only = [{"id": "r1", "title": "初审规则 V2"}]
    two = [{"id": "r1", "title": "平台登录改版"}, {"id": "r2", "title": "平台导出"}]
    plain = {"placement": None, "requirement_id": None, "text": "平台登录先上线", "detail": ""}
    assert decisions.effective_requirement(plain, [], "p") == (None, "project")
    assert decisions.effective_requirement(plain, only, "p") == ("r1", "only")
    assert decisions.effective_requirement(plain, two, "p") == ("r1", "title")
    # 共同片段落在项目名里不算（项目叫「平台登录」时「平台登」「台登录」都不算）
    assert decisions.effective_requirement(plain, two, "p", ["平台登录"]) == (None, "unplaced")
    # 停用词不算片段
    assert "则优化" not in decisions.title_fragments("初审规则优化")
    assert decisions.title_fragments("V2 需求") == set()
    # 两个需求名都对上：没归到
    both = [{"id": "r1", "title": "阈值规则"}, {"id": "r2", "title": "阈值规则二"}]
    assert decisions.effective_requirement({**plain, "text": "阈值规则改了"}, both, "p") == (None, "unplaced")


def test_manual_and_ai_placements():
    linked = [{"id": "r1", "title": "甲需求"}, {"id": "r2", "title": "乙需求"}]
    base = {"text": "照旧", "detail": ""}
    picked = {**base, "placement": "picked", "requirement_id": "r9", "requirement_project": "p"}
    assert decisions.effective_requirement(picked, linked, "p") == ("r9", "picked")
    # picked 的需求不在同一个项目：按自动
    assert decisions.effective_requirement({**picked, "requirement_project": "q"}, linked, "p") == (None, "unplaced")
    ai = {**base, "placement": "ai", "requirement_id": "r2", "requirement_project": "p"}
    assert decisions.effective_requirement(ai, linked, "p") == ("r2", "ai")
    # ai 放的需求不再关联这场会：按自动
    assert decisions.effective_requirement(ai, linked[:1], "p") == ("r1", "only")
    assert decisions.effective_requirement({**base, "placement": "none"}, linked, "p") == (None, "none")


# ---------------------------------------------------------------------- 「决议」卡


def test_requirement_log_structure(tmp_path):
    db = world(tmp_path)
    shown = changed_row(db, "old", "new")
    result = log(db)

    assert result["requirement"] == {"id": "r1", "title": "初审规则 V2"}
    assert result["counts"] == {"decisions": 4, "later_changed": 1, "unplaced": 1}
    assert [group["meeting"]["id"] for group in result["meetings"]] == ["third", "new", "old"]
    third, new, old = result["meetings"]
    # 第三场：「驻场排班」那条名字对上 r2，不在这张卡里；另一条没归到，折叠在 unplaced
    assert third["decisions"] == [] and [item["text"] for item in third["unplaced"]] == ["下周起统一口径"]
    assert third["unplaced"][0]["placement"] == {"how": "unplaced", "requirement_id": None}
    assert old["meeting"] == {"id": "old", "title": "周会甲", "date": "2026-09-16", "audio_url": None}
    first = old["decisions"][0]
    assert first["placement"] == {"how": "only", "requirement_id": "r1"}
    assert first["later"] == [{
        "relation_id": shown["id"], "decision_id": decision_id(db, "new"), "text": NEW_TEXT,
        "meeting": {"id": "new", "title": "周会乙", "date": "2026-09-24"}, "start_ms": 30_000,
        "quote": "改成 0.7", "audio_url": None,
    }]
    assert new["decisions"][0]["earlier"][0]["decision_id"] == decision_id(db, "old")
    assert first["stale_files"] == [] and first["dismissed"] == []
    # 规则版「后来又提到」挂在最早那条下面
    assert [item["meeting"]["id"] for item in old["decisions"][1]["restated"]] == ["new"]
    assert new["decisions"][1]["restated"] == []
    assert result["state"] == {"kind": "waiting", "text": decisions.PAIR_WAITING, "action": None}


def test_restated_groups_share_a_later_change(tmp_path):
    db = world(tmp_path)
    meeting(db, "fourth", "初审规则改到下周一上线", ago=0, title="周会丁")
    link(db, "r1", "fourth")
    ingest(db)
    # 新会那条「周五上线」后来改成「下周一」：整组（旧会、新会）都算后来改了
    changed_row(db, "new", "fourth", why_earlier="周五上线", why_later="下周一上线")
    db.execute("UPDATE relations SET decision_id = ?, to_decision_id = ?, ident = ? WHERE kind = 'later_changed'",
               (decision_id(db, "new", 1), decision_id(db, "fourth"),
                f"{decision_id(db, 'new', 1)}|{decision_id(db, 'fourth')}"))
    result = log(db)
    groups = {group["meeting"]["id"]: group for group in result["meetings"]}
    assert [ref["meeting"]["id"] for ref in groups["old"]["decisions"][1]["later"]] == ["fourth"]
    assert [ref["meeting"]["id"] for ref in groups["new"]["decisions"][1]["later"]] == ["fourth"]


def test_dismissed_lists_what_you_rejected(tmp_path):
    db = world(tmp_path)
    rejected = changed_row(db, "old", "new", status="rejected")
    old = log(db)["meetings"][2]
    first = old["decisions"][0]
    assert first["later"] == []
    assert first["dismissed"] == [{
        "relation_id": rejected["id"], "kind": "later_changed",
        "other": {"date": "2026-09-24", "meeting_title": "周会乙", "text": NEW_TEXT},
        "decided_at": NOW.isoformat(),
    }]
    assert log(db)["counts"]["later_changed"] == 0


def test_picked_decisions_join_the_card_and_none_leaves_it(tmp_path):
    db = world(tmp_path)
    with db.transaction() as connection:
        decisions.place(connection, decision_id(db, "third", 1), "picked", "r1")
        decisions.place(connection, decision_id(db, "old", 0), "none", None)
    result = log(db)
    groups = {group["meeting"]["id"]: group for group in result["meetings"]}
    assert [item["text"] for item in groups["third"]["decisions"]] == ["下周起统一口径"]
    assert groups["third"]["decisions"][0]["placement"]["how"] == "picked"
    assert [item["text"] for item in groups["old"]["decisions"]] == [SAME]


def test_lagging_ledger_reads_the_minutes_without_marks(tmp_path):
    db = world(tmp_path)
    changed_row(db, "old", "new")
    set_minutes(db, "old", "mv-old-2", minutes("阈值先按 0.9 执行 [00:12:34]", SAME), kind="generated")
    result = log(db)
    old = next(group for group in result["meetings"] if group["meeting"]["id"] == "old")
    assert [(item["id"], item["text"]) for item in old["decisions"]] == [(None, "阈值先按 0.9 执行"), (None, SAME)]
    assert all(not item["later"] and not item["restated"] for item in old["decisions"])

    # 关联整理关着：整张卡现读，状态句说明为什么不标
    off = log(db, settings=SimpleNamespace(links_enabled=False))
    assert all(item["id"] is None for group in off["meetings"] for item in group["decisions"])
    assert off["state"] == {"kind": "stopped", "text": decisions.PAIR_LINKS_OFF, "action": None}


def test_notes_for_meetings_without_a_section(tmp_path):
    db = make(tmp_path)
    add_requirement(db, "r1", "p", "初审规则 V2")
    meeting(db, "a", ago=3)
    set_minutes(db, "a", "mv-a-2", "# 周会\n\n## 摘要\n\n没定什么。\n", kind="generated")
    link(db, "r1", "a")
    ingest(db)
    (group,) = log(db)["meetings"]
    assert group["note"] == "这场纪要没有决议段" and group["decisions"] == []


@pytest.mark.parametrize(
    ("snapshot", "states", "expected"),
    [
        ({"enabled": True, "llm": "ok"}, ["done"], ("ok", None, None)),
        ({"enabled": True, "llm": "ok"}, ["pending"], ("waiting", decisions.PAIR_WAITING, None)),
        ({"enabled": True, "llm": "no_key"}, ["pending"], ("stopped", decisions.PAIR_NO_KEY, None)),
        ({"enabled": True, "llm": "auth"}, ["pending"], ("stopped", decisions.PAIR_BAD_KEY, None)),
        ({"enabled": True, "llm": "capped"}, ["running"], ("stopped", decisions.PAIR_CAPPED, None)),
        ({"enabled": True, "llm": "balance"}, ["pending"], ("stopped", decisions.PAIR_BALANCE, None)),
        ({"enabled": True, "llm": "backoff"}, ["pending"], ("stopped", decisions.PAIR_UNREACHABLE, None)),
        ({"enabled": True, "llm": "ok"}, ["failed", "pending"], ("stopped", decisions.PAIR_FAILED, "retry")),
        ({"enabled": True, "llm": "off"}, ["pending"], ("stopped", decisions.PAIR_LLM_OFF, None)),
        ({"enabled": False}, [], ("stopped", decisions.PAIR_LINKS_OFF, None)),
    ],
)
def test_state_sentences(snapshot, states, expected):
    line = decisions.pair_state_line(snapshot, LIVE, states)
    assert (line["kind"], line["text"], (line["action"] or {}).get("kind")) == expected


def _requirement_world(tmp_path, count):
    db = make(tmp_path)
    add_requirement(db, "r1", "p", "初审规则 V2")
    add_requirement(db, "r2", "p", "驻场排班")
    for index in range(count):
        meeting_id = f"m{index:03d}"
        meeting(db, meeting_id, f"阈值第{index}版按 0.{index} 执行", SAME, "下周起统一口径", ago=index % 300 + 1)
        link(db, "r1", meeting_id)
        if index % 3 == 0:
            link(db, "r2", meeting_id)
    decisions.ingest_pending(db, now=NOW, max_meetings=count + 1, max_seconds=60)
    return db


@pytest.mark.parametrize("count", [10, 200])
def test_requirement_log_is_at_most_six_statements(tmp_path, count):
    # 4e 起多一条：这些决议在问的可能过时（stale_files）
    db = _requirement_world(tmp_path, count)
    reads = count_reads(db, lambda connection: decisions.requirement_log(connection, "r1", settings=LIVE))
    assert reads <= 6
    assert log(db)["counts"]["decisions"] > 0


# ---------------------------------------------------------------------- 简报、展开一场会


def test_brief_and_focus_carry_later(tmp_path):
    db = world(tmp_path)
    shown = changed_row(db, "old", "new")
    with db.autocommit() as connection:
        brief = graph.meeting_brief(connection, "old", attribution=None, card=None)
        focus_old = graph.meeting_focus(connection, "old")
        focus_new = graph.meeting_focus(connection, "new")
    assert brief["decisions"][0]["later"] == {"date": "2026-09-24", "meeting_title": "周会乙", "text": NEW_TEXT}
    assert brief["decisions"][1]["later"] is None
    assert focus_old["decisions"][0]["later"] == [{
        "relation_id": shown["id"], "decision_id": decision_id(db, "new"), "text": NEW_TEXT,
        "meeting": {"id": "new", "title": "周会乙", "date": "2026-09-24"}, "start_ms": 30_000,
        "quote": "改成 0.7", "audio_url": None,
    }]
    assert focus_new["decisions"][0]["earlier"][0]["decision_id"] == decision_id(db, "old")


# ---------------------------------------------------------------------- 接口


def test_endpoint_and_404(tmp_path):
    key = tmp_path / "api-key"
    key.write_text("sk-test", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive", staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3", semantic_enabled=False, lark_webhook_url="",
        llm_api_key_file=key, material_browse_root=tmp_path / "browse", links_enabled=True,
    )
    client = TestClient(create_app(settings, FakeRelayClient()))
    db = Database(settings.database_path)
    add_project(db, "p", "云图AI")
    add_requirement(db, "r1", "p", "初审规则 V2")
    meeting(db, "old", OLD_TEXT, ago=3)
    link(db, "r1", "old")
    ingest(db)

    response = client.get("/api/requirements/r1/decisions")
    assert response.status_code == 200
    body = response.json()
    assert body["meetings"][0]["decisions"][0]["id"] == decision_id(db, "old")
    assert client.get("/api/requirements/nope/decisions").json() == {"detail": "需求不存在"}
    assert client.get("/api/requirements/nope/decisions").status_code == 404
