"""第四期 4a：links_loop（deep_links.LinksWorker）的骨架：节奏、轻活和重活的预算、转写时让路、
database is locked、L1、L2、清理；编码锁和材料向量快照；健康检查、bootstrap 和 links 命令。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_workbench import affects, cli, deep_links, file_events, produced, relations
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.deep_links import LinksWorker
from meeting_workbench.main import create_app
from meeting_workbench.material_vectors import MaterialVectors
from meeting_workbench.semantic import SemanticIndex
from meeting_workbench.tasks import _insert_deliverable

from .test_file_mentions import add_file
from .test_graph import add_meeting, add_task
from .test_material_index import add_root, make
from .test_material_search import (
    FakeEncoder,
    add_content,
    add_project,
    chunk_ids,
    matrix_world,
    put_vector,
    vector_settings,
)
from .test_material_search import add_root as add_material_root

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


@pytest.fixture
def world(tmp_path):
    """和 test_material_search 的 world 一样：两个项目各挂一个根目录。"""
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    add_project(db, "p", "云图AI")
    add_project(db, "q", "别的项目")
    root = add_material_root(db, "p", tmp_path / "云图AI")
    other = add_material_root(db, "q", tmp_path / "别的")
    return SimpleNamespace(db=db, root=root, other=other, tmp=tmp_path)
MINUTES = "# 周会\n\n## 决议\n\n- 报价单按第三版发出 [00:12:34]\n- 排期表下周定稿\n"
MINUTES_EARLIER = "# 周会\n\n## 决议\n\n- 报价单按第四版发出 [00:05:00]\n- 排期表下周定稿\n"


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def loop_settings(**overrides):
    values = {"links_enabled": True, "links_backfill_days": 180, "semantic_model": "bge-test"}
    values.update(overrides)
    return SimpleNamespace(**values)


def worker(db, *, now=NOW, settings=None, **kwargs):
    moment = {"value": now}
    built = LinksWorker(db, settings or loop_settings(), now=lambda: moment["value"], **kwargs)
    built.moment = moment
    return built


def at(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def rev(db, key="graph_rev"):
    row = db.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
    return int(row["value"]) if row else 0


def recorder(calls, name, status="done", effect=None):
    def step(ctx):
        calls.append(name)
        if effect is not None:
            effect(ctx)
        return status

    return step


def fake_heavy(w, calls, **effects):
    for attr in ("h3_related_opened", "h2_affects", "h3_related_rest", "h4_terms"):
        setattr(w, attr, recorder(calls, attr, effect=effects.get(attr)))


# ---------------------------------------------------------------------- 闲着的一轮


def idle_world(tmp_path):
    db, _ = make(tmp_path)
    root_id = add_root(db, tmp_path / "云图AI")
    quote_id = add_file(db, root_id, "报价单.xlsx")
    plan_id = add_file(db, root_id, "方案.docx")
    gone_id = add_file(db, root_id, "旧稿.docx", gone=True)
    db.execute("UPDATE material_files SET content_key = 'k-quote' WHERE id = ?", (quote_id,))
    db.execute("UPDATE material_files SET content_key = 'k-plan' WHERE id = ?", (plan_id,))
    add_meeting(db, "m", ago=1, project_id="p", minutes=MINUTES)
    # 4c：前一场会定了第四版、排期表同一句话（规则版「后来又提到」），对比行在第一轮以后写
    add_meeting(db, "m2", ago=2, project_id="p", minutes=MINUTES_EARLIER)
    add_task(db, "t", meeting_id="m", project_id="p", status="confirmed")
    with db.transaction() as connection:
        _insert_deliverable(
            connection, "t", name="方案.docx", content_key="k-plan", root_id=root_id, rel_path="方案.docx", now=at(-7200)
        )
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
               anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('m', 'p', '方案', ?, '方案', 1, 0, '[0]', 0, 'transcript', 'active', 0, ?)""",
        (plan_id, at(-7200)),
    )
    rows = [
        {"kind": "mention", "project_id": "p", "ident": "m|报价单", "status": "shown", "origin": "llm",
         "meeting_id": "m", "at_ms": 60_000, "stem_key": "报价单", "file_id": quote_id, "content_key": "k-quote",
         "root_id": root_id, "rel_path": "报价单.xlsx", "quote": "上周那版报价单", "evidence": {"phrase": "上周那版"}},
        {"kind": "related", "project_id": "p", "ident": "m|k-quote", "status": "shown", "origin": "vector",
         "meeting_id": "m", "at_ms": 90_000, "file_id": quote_id, "content_key": "k-quote", "root_id": root_id,
         "rel_path": "报价单.xlsx", "score": 0.71, "evidence": {"words": ["报价"]}},
        {"kind": "produced", "project_id": "p", "ident": "t|k-quote", "status": "suggested", "origin": "rule",
         "task_id": "t", "file_id": quote_id, "content_key": "k-quote", "root_id": root_id,
         "rel_path": "报价单.xlsx", "evidence": {"words": ["报价单"]}},
        {"kind": "mention", "project_id": "p", "ident": "m2|旧稿", "status": "cleared", "origin": "llm",
         "meeting_id": "m2", "stem_key": "旧稿", "file_id": gone_id, "content_key": "k-old", "root_id": root_id,
         "rel_path": "旧稿.docx"},
    ]
    with db.transaction() as connection:
        relations.upsert_system(connection, rows, at(-3600), since=at(-3600))
    # 一条你驳回过、文件已经不在的：L2 每轮都看到它，但从不写
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, stem_key, content_key, root_id,
               rel_path, created_at, updated_at, decided_at)
           VALUES ('mention', 'p', 'm2|草稿', 'rejected', 'llm', 'm2', '草稿', 'k-draft', ?, '草稿.docx', ?, ?, ?)""",
        (root_id, at(-3600), at(-3600), at(-3600)),
    )
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, files, spoken, status, created_at, updated_at)
           VALUES ('p', '能耗看板', '能耗看板', 3, 1, 'pending', ?, ?)""",
        (at(-3600), at(-3600)),
    )
    # 4b：一场抽完了放宽提到的会，L5 前两轮写出放宽行和给字面行的提示，之后签名不变就不再写
    add_meeting(db, "m3", ago=1, project_id="p", segments=[(60_000, "上周那版报价单再看一下"), (120_000, "方案也改")])
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count, first_ms,
               anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('m3', 'p', '方案', ?, '方案', 1, 0, '[0]', 0, 'transcript', 'active', 0, ?)""",
        (plan_id, at(-7200)),
    )
    phrases = [
        {"at_ms": 60_000, "quote": "上周那版报价单再看一下", "phrase": "上周那版报价单", "core": "报价单", "aka": [],
         "kind": "表格", "when": {"rel": "last_week", "version": None}},
    ]
    # 4d：一份读完、算好向量的材料（假编码器按文字哈希给向量）和一场会上说到它的会，H3 前两轮算出相关行
    from .test_related import TOPIC_A, add_content, segments_for

    add_content(db, "q2:" + "a" * 32, ["。".join(TOPIC_A)])
    doc_id = add_file(db, root_id, "接口文档.docx")
    db.execute("UPDATE material_files SET content_key = ? WHERE id = ?", ("q2:" + "a" * 32, doc_id))
    add_meeting(db, "m4", ago=1, project_id="p", segments=segments_for(TOPIC_A))
    version = db.query_one("SELECT current_transcript_version_id AS v FROM meetings WHERE id = 'm3'")["v"]
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, parts, parts_done, phrases_json,
               created_at, updated_at)
           VALUES ('m3', ?, 'sha', 'done', 1, 1, ?, ?, ?)""",
        (version, json.dumps(phrases, ensure_ascii=False), at(-3600), at(-3600)),
    )
    # 4e：根目录收完过一轮；一条昨天确认的任务和它确认以后新增的一份名字对得上的文件（L4 写产出）；
    # 一场定了「总价下调 5%」的会和一份月初就有、还写着下调 3% 的文件（H2 写影响）
    from .test_file_events import swept

    db.execute("UPDATE app_state SET value = ? WHERE key = 'links_since'", (at(-30 * 86400),))
    swept(db, root_id)
    add_task(db, "t-new", meeting_id=None, project_id="p", status="confirmed")
    db.execute("UPDATE tasks SET title = '整理报价单明细' WHERE id = 't-new'")
    db.execute(
        "INSERT INTO task_events(task_id, kind, body, created_at) VALUES ('t-new', 'confirmed', '任务已确认', ?)",
        (at(-86400),),
    )
    detail_id = add_file(db, root_id, "报价单明细 v2.xlsx")
    db.execute(
        "UPDATE material_file_events SET at = ?, day = ? WHERE file_id = ?",
        (file_events.at_text(NOW - timedelta(hours=1)), (NOW - timedelta(hours=1)).astimezone().date().isoformat(),
         detail_id),
    )
    add_meeting(db, "m5", ago=1, project_id="p", minutes="# 周会\n\n## 决议\n\n- 总价下调 5% [00:01:00]\n")
    price_key = "q2:" + "c" * 32
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
           VALUES (?, 'text', 'done', 20, 1, 'x', 'x')""",
        (price_key,),
    )
    db.execute("INSERT INTO material_chunks(content_key, ordinal, text) VALUES (?, 0, '报价说明：总价下调 3%')", (price_key,))
    price_id = add_file(db, root_id, "报价/总价说明.docx")
    db.execute(
        """UPDATE material_files SET content_key = ?, content_size = size, content_mtime_ns = mtime_ns WHERE id = ?""",
        (price_key, price_id),
    )
    # 4h：三份文字材料里各说两次「驻场服务」，H4 前两轮挖出种子、汇总出一个候选词，之后签名不变就不再写
    for index in range(3):
        key = "q2:" + str(index) * 32
        db.execute(
            """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
               VALUES (?, 'text', 'done', 30, 1, 'x', 'x')""",
            (key,),
        )
        db.execute(
            "INSERT INTO material_chunks(content_key, ordinal, text) VALUES (?, 0, ?)",
            (key, f"驻场服务按月结算，第{index}条。驻场服务另计，第{index + 5}条。"),
        )
        file_id = add_file(db, root_id, f"驻场/合同{index}.docx")
        db.execute("UPDATE material_files SET content_key = ?, ext = 'docx' WHERE id = ?", (key, file_id))
    db.execute("DELETE FROM material_file_events WHERE file_id != ?", (detail_id,))
    return db


def add_pair_rows(db):
    """4c：一条 AI 写的「后来改了」（前一场「第四版」到这一场「第三版」）。"""
    early = db.query_one("SELECT id FROM decisions WHERE meeting_id = 'm2' AND ordinal = 0")["id"]
    late = db.query_one("SELECT id FROM decisions WHERE meeting_id = 'm' AND ordinal = 0")["id"]
    with db.transaction() as connection:
        relations.upsert_system(
            connection,
            [{"kind": "later_changed", "project_id": "p", "ident": f"{early}|{late}", "status": "shown",
              "origin": "llm", "meeting_id": "m", "at_ms": 754_000, "decision_id": early, "to_decision_id": late,
              "quote": "按第三版", "evidence": {"why_earlier": "按第四版", "why_later": "按第三版"}}],
            at(-3600),
            since=at(-3600),
        )


def fingerprint(db):
    return {
        "graph_rev": rev(db),
        "related_rev": rev(db, "related_rev"),
        "relations": db.query_all("SELECT id, status, file_id, updated_at FROM relations ORDER BY id"),
        "decisions": db.query_all("SELECT id, text, updated_at FROM decisions ORDER BY id"),
        "candidates": db.query_all("SELECT id, status, updated_at FROM glossary_candidates ORDER BY id"),
        "seeds": db.query_all("SELECT * FROM glossary_mining_seeds ORDER BY content_key"),
        "mining_scan": db.query_all("SELECT * FROM glossary_mining_scan ORDER BY project_id"),
        "extractions": db.query_all("SELECT meeting_id, hints_json, resolved_sig, updated_at FROM mention_extractions"),
        "related": db.query_all("SELECT * FROM meeting_window_passages ORDER BY meeting_id, start_ms, rank"),
        "related_scan": db.query_all("SELECT * FROM meeting_related_scan ORDER BY meeting_id"),
    }


def test_idle_round_leaves_revisions_alone(tmp_path):
    from .test_related import FakeSemantic

    db = idle_world(tmp_path)
    config = loop_settings(semantic_enabled=True, material_content_enabled=True, glossary_mining_enabled=True)
    semantic = FakeSemantic()
    vectors = MaterialVectors(db, config, semantic, clock=lambda: 0.0)
    vectors.refresh()
    w = worker(db, clock=Clock(), settings=config, semantic=semantic, vectors=vectors)
    first = w.run_round()
    add_pair_rows(db)
    w.run_round()
    stable = fingerprint(db)
    assert len(stable["decisions"]) == 5  # 样本真的入库了（4e 加了一场定了总价的会）
    # 4c：规则版「后来又提到」和 AI 的「后来改了」都在
    assert db.query_one("SELECT origin, status FROM relations WHERE kind = 'restated'") == {
        "origin": "rule", "status": "shown"
    }
    assert db.query_one("SELECT status FROM relations WHERE kind = 'later_changed'") == {"status": "shown"}
    # 放宽的提到真的写出来了（L5）
    assert db.query_one("SELECT status, origin FROM relations WHERE ident = 'm3|报价单'") == {
        "status": "shown", "origin": "llm"
    }
    assert stable["extractions"][0]["resolved_sig"]
    # 4d：相关真的算出来了（H3）
    assert db.query_one("SELECT status, origin FROM relations WHERE ident = ?", ("m4|q2:" + "a" * 32,)) == {
        "status": "shown", "origin": "vector"
    }
    assert first["phases"]["decisions"] == "done"
    # 4e：L4 写出了产出，H2 写出了影响（第三轮下面不能再写）
    assert db.query_one("SELECT status, origin FROM relations WHERE kind = 'produced' AND task_id = 't-new'") == {
        "status": "suggested", "origin": "rule"
    }
    assert db.query_one("SELECT status, quote FROM relations WHERE kind = 'affects'") == {
        "status": "suggested", "quote": "总价下调 5%"
    }
    assert w.snapshot()["open"] == {"produced": 2, "affects": 1} and w.snapshot()["waiting"]["affects"] == 0
    # 4h：H4 挖出了词（手放的「能耗看板」证据没了，记 dropped）
    words = {row["term"]: row["status"] for row in db.query_all("SELECT term, status FROM glossary_candidates")}
    assert words["能耗看板"] == "dropped" and words["驻场服务"] == "pending"
    assert w.snapshot()["waiting"]["terms"] == 0

    w.moment["value"] = NOW + timedelta(hours=3)  # 同一天
    third = w.run_round()

    assert fingerprint(db) == stable
    assert third["work"] is False


# ---------------------------------------------------------------------- 预算、转写、locked


def test_light_budget_leaves_later_steps_for_the_next_round(tmp_path):
    db, _ = make(tmp_path)
    clock = Clock()
    w = worker(db, clock=clock)
    calls: list[str] = []
    w.l1_decisions = recorder(calls, "l1", effect=lambda ctx: setattr(clock, "value", clock.value + 3.5))
    w.l2_resolve = recorder(calls, "l2")
    fake_heavy(w, calls, h2_affects=lambda ctx: setattr(clock, "value", clock.value + 15.5))

    phases = w.run_round()["phases"]

    assert phases["decisions"] == "done"
    assert [phases[name] for name in ("resolve", "stale", "produced", "mentions")] == ["budget"] * 4
    # 重活另有 15 秒：打开过的会的 H3 排在 H2 前面；H2 用完 15 秒后 H3 其余和 H4 留到下一轮
    assert calls == ["l1", "h3_related_opened", "h2_affects"]
    assert (phases["affects"], phases["related"], phases["terms"]) == ("done", "budget", "budget")


def test_light_steps_run_while_transcribing_and_heavy_steps_wait(tmp_path):
    db, _ = make(tmp_path)
    add_meeting(db, "m", ago=1, project_id="p", minutes=MINUTES)
    w = worker(db, clock=Clock(), busy=lambda: True)
    calls: list[str] = []
    fake_heavy(w, calls)

    result = w.run_round()

    assert calls == []
    assert result["phases"]["decisions"] == "done" and len(db.query_all("SELECT id FROM decisions")) == 2
    assert {result["phases"][name] for name in ("affects", "related", "terms")} == {"busy"}
    snap = w.snapshot()
    assert snap["paused"] == "busy" and snap["phases"]["related"] == "busy"


def test_heavy_steps_stop_once_transcribing_starts(tmp_path):
    db, _ = make(tmp_path)
    state = {"busy": False}
    w = worker(db, clock=Clock(), busy=lambda: state["busy"])
    calls: list[str] = []
    fake_heavy(w, calls, h3_related_opened=lambda ctx: state.update(busy=True))

    phases = w.run_round()["phases"]

    assert calls == ["h3_related_opened"]
    assert (phases["affects"], phases["related"], phases["terms"]) == ("busy", "busy", "busy")


def test_database_is_locked_ends_the_round(tmp_path):
    db, _ = make(tmp_path)
    w = worker(db, clock=Clock())
    calls: list[str] = []
    fake_heavy(w, calls)

    def locked(ctx):
        raise sqlite3.OperationalError("database is locked")

    w.l2_resolve = locked
    result = w.run_round()

    assert result["phases"] == {"decisions": "done", "resolve": "locked"}
    assert calls == []
    assert db.query_one("SELECT 1 AS x FROM app_state WHERE key = 'links_housekeeping_at'") is None
    assert w.snapshot()["phases"]["resolve"] == "locked"


def test_one_broken_step_does_not_stop_the_rest(tmp_path, caplog):
    db, _ = make(tmp_path)
    w = worker(db, clock=Clock())
    calls: list[str] = []

    def broken(ctx):
        raise ValueError("坏数据")

    fake_heavy(w, calls, h2_affects=broken)
    with caplog.at_level("ERROR", logger="meeting_workbench.deep_links"):
        result = w.run_round()

    assert result["phases"]["affects"] == "error"
    assert "h4_terms" in calls
    assert db.query_one("SELECT 1 AS x FROM app_state WHERE key = 'links_housekeeping_at'") is not None
    snap = w.snapshot()
    assert snap["phases"]["affects"] == "error" and snap["last_round_at"] is not None
    assert "affects" in caplog.text


def test_material_fts_rebuild_skips_h2_and_h3(tmp_path):
    db, _ = make(tmp_path)
    db.execute("INSERT INTO app_state(key, value, updated_at) VALUES ('material_fts_rebuild', '{}', ?)", (utc_now(),))
    w = worker(db, clock=Clock())
    calls: list[str] = []
    seen: list[bool] = []
    fake_heavy(w, calls, h4_terms=lambda ctx: seen.append(ctx.fts_rebuilding))

    phases = w.run_round()["phases"]

    assert calls == ["h4_terms"] and seen == [True]
    assert (phases["affects"], phases["related"]) == ("off", "off")


def test_stop_flag_ends_the_round(tmp_path):
    db, _ = make(tmp_path)
    stop = threading.Event()
    w = worker(db, clock=Clock(), stop=stop)
    w.l1_decisions = recorder([], "l1", effect=lambda ctx: stop.set())
    phases = w.run_round()["phases"]
    assert phases["resolve"] == "stopping" and phases["terms"] == "stopping"


# ---------------------------------------------------------------------- 4e：L3、L4、H2


def _e4_world(tmp_path):
    from .test_affects import material, meeting
    from .test_affects import world as affects_world
    from .test_produced import put_file, task

    w = affects_world(tmp_path)
    w.material, w.meeting, w.put_file, w.task = material, meeting, put_file, task
    return w


def _e4_settings():
    return loop_settings(links_backfill_days=180)


def test_l3_and_l4_run_while_transcribing(tmp_path):
    w = _e4_world(tmp_path)
    w.task(w.db, "t", "整理报价单明细")
    w.put_file(w.db, w.root, "报价/报价单明细 v2.xlsx", NOW - timedelta(days=1))
    w.db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, created_at, updated_at)
           VALUES ('affects', 'p', 'dec-gone|k', 'suggested', 'rule', ?, ?)""",
        (at(-3600), at(-3600)),
    )
    calls: list[str] = []
    busy = worker(w.db, clock=Clock(), busy=lambda: True, settings=_e4_settings())
    fake_heavy(busy, calls)
    result = busy.run_round()
    assert calls == []
    assert (result["phases"]["stale"], result["phases"]["produced"]) == ("done", "done")
    assert w.db.query_one("SELECT status FROM relations WHERE ident = 'dec-gone|k'")["status"] == "cleared"
    assert w.db.query_one("SELECT status FROM relations WHERE kind = 'produced'")["status"] == "suggested"


def test_l3_and_l4_per_round_limits(tmp_path, monkeypatch):
    w = _e4_world(tmp_path)
    monkeypatch.setattr(produced, "ROUND_TASKS", 1)
    monkeypatch.setattr(affects, "L3_ROWS", 1)
    w.db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p2', '第二个', 'x')")
    w.db.execute("INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p2', '/材料/第二个', 'x')")
    second_root = int(w.db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'p2'")["id"])
    from .test_file_events import swept

    swept(w.db, second_root)
    w.task(w.db, "t1", "整理报价单明细")
    w.task(w.db, "t2", "整理排期总表", project_id="p2")
    w.put_file(w.db, w.root, "报价/报价单明细 v2.xlsx", NOW - timedelta(days=1))
    w.put_file(w.db, second_root, "排期/排期总表 v2.xlsx", NOW - timedelta(days=1))
    # 两行决议已经没了的影响（文件还在，L2 不动它们）
    file = w.material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    w.db.execute("DELETE FROM material_file_events WHERE file_id = ?", (file["id"],))
    for index in range(2):
        w.db.execute(
            """INSERT INTO relations(kind, project_id, ident, status, origin, file_id, content_key, root_id, rel_path,
                   created_at, updated_at)
               VALUES ('affects', 'p', ?, 'suggested', 'rule', ?, ?, ?, ?, ?, ?)""",
            (f"dec-gone|{index}", file["id"], file["content_key"], file["root_id"], file["rel_path"], at(-3600),
             at(-3600)),
        )
    runner = worker(w.db, clock=Clock(), settings=_e4_settings())
    fake_heavy(runner, [])
    first = runner.run_round()["phases"]
    # 每轮一条任务（按项目整批）、一行影响
    assert first["produced"] == "budget"
    assert w.db.query_one("SELECT COUNT(*) AS n FROM relations WHERE kind = 'produced'")["n"] == 1
    assert w.db.query_one("SELECT COUNT(*) AS n FROM relations WHERE status = 'cleared'")["n"] == 1
    runner.moment["value"] = NOW + timedelta(minutes=1)
    second = runner.run_round()["phases"]
    assert second["produced"] == "done"
    assert w.db.query_one("SELECT COUNT(*) AS n FROM relations WHERE kind = 'produced'")["n"] == 2
    assert w.db.query_one("SELECT COUNT(*) AS n FROM relations WHERE status = 'cleared'")["n"] == 2


def test_h2_stops_within_a_batch_once_transcribing_starts(tmp_path):
    w = _e4_world(tmp_path)
    w.meeting(w.db, "m1", "总价下调 5%", day=NOW - timedelta(days=1))
    w.meeting(w.db, "m2", "驻场改成 2 人", day=NOW - timedelta(days=2))
    w.material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    w.material(w.db, w.root, "驻场/排班.xlsx", "驻场 3 人")

    def busy():
        # 第一场会配完（台账写上）以后开始转写
        return w.db.query_one("SELECT COUNT(*) AS n FROM decision_scan WHERE affects_hash IS NOT NULL")["n"] >= 1

    runner = worker(w.db, clock=Clock(), busy=busy, settings=_e4_settings())
    phases = runner.run_round()["phases"]
    assert phases["affects"] == "busy"
    rows = w.db.query_all("SELECT meeting_id FROM relations WHERE kind = 'affects'")
    assert rows == [{"meeting_id": "m1"}]
    assert w.db.query_one("SELECT affects_hash FROM decision_scan WHERE meeting_id = 'm2'")["affects_hash"] is None


def test_h2_reports_budget_not_busy_when_the_round_is_full(tmp_path, monkeypatch):
    """H2 到了每轮的条数停下时，健康信息里写 budget（不是 busy）；下一轮接着把剩下的会配完。"""
    monkeypatch.setattr(affects, "ROUND_DECISIONS", 1)
    w = _e4_world(tmp_path)
    w.meeting(w.db, "m1", "总价下调 5%", day=NOW - timedelta(days=1))
    w.meeting(w.db, "m2", "驻场改成 2 人", day=NOW - timedelta(days=2))
    w.material(w.db, w.root, "报价/报价单.xlsx", "总价下调 3%")
    w.material(w.db, w.root, "驻场/排班.xlsx", "驻场 3 人")
    runner = worker(w.db, clock=Clock(), settings=_e4_settings())
    assert runner.run_round()["phases"]["affects"] == "budget"
    assert runner.snapshot()["phases"]["affects"] == "budget"
    assert w.db.query_one("SELECT affects_hash FROM decision_scan WHERE meeting_id = 'm2'")["affects_hash"] is None
    runner.moment["value"] = NOW + timedelta(minutes=1)
    assert runner.run_round()["phases"]["affects"] == "done"
    assert w.db.query_one("SELECT COUNT(*) AS n FROM relations WHERE kind = 'affects'")["n"] == 2


# ---------------------------------------------------------------------- 节奏和打开过的会


def test_loop_cadence_and_wake_up(tmp_path):
    db, _ = make(tmp_path)
    w = worker(db, clock=Clock())
    stop = threading.Event()
    rounds = iter([{"work": True}, {"work": False}, {"work": False}])
    slept: list[float] = []
    marks: list[int] = []

    def run_round():
        w._wake.clear()
        marks.append(len(slept))
        result = next(rounds, None)
        if result is None or len(marks) == 3:
            stop.set()
            return {"work": False}
        return result

    w.run_round = run_round

    async def sleep(seconds):
        slept.append(seconds)
        # 第二轮之后没活要等 60 秒，这时会议页打开了：1 秒左右就开始下一轮
        if len(marks) == 2 and len(slept) - marks[1] == 2:
            w.prioritize("m")

    asyncio.run(deep_links.links_loop(w, stop, sleep=sleep))

    # 第一轮前等 20 秒，有活 10 秒，没活本该 60 秒但被唤醒（都按 0.5 秒一段等）
    assert marks == [40, 60, 62]
    assert set(slept) == {0.5}


def test_prioritize_keeps_at_most_64_newest_first(tmp_path):
    db, _ = make(tmp_path)
    w = worker(db)
    for index in range(70):
        w.prioritize(f"m{index}")
    w.prioritize("m10")
    order = w.priorities()
    assert len(order) == 64 and order[0] == "m10" and order[1] == "m69" and "m5" not in order
    assert w._wake.is_set()
    w.done_priority("m10")
    assert "m10" not in w.priorities()


def test_prioritize_wakes_only_when_the_meeting_is_new_to_the_queue(tmp_path):
    # 栏在 waiting 时每 15 秒重取：已经排着的会只挪到最前，不再唤醒循环
    db, _ = make(tmp_path)
    w = worker(db)
    w.prioritize("m1")
    w.prioritize("m2")
    assert w._wake.is_set()
    w._wake.clear()
    w.prioritize("m1")
    assert not w._wake.is_set() and w.priorities() == ["m1", "m2"]
    # 算完出了队列，再打开就又是新的
    w.done_priority("m1")
    w.prioritize("m1")
    assert w._wake.is_set()


def test_next_delay():
    assert deep_links.next_delay({"work": True}) == 10
    assert deep_links.next_delay({"work": False}) == 60
    assert deep_links.next_delay(None) == 60


# ---------------------------------------------------------------------- L1、L2


def test_l1_ingests_decisions_and_counts_waiting(tmp_path):
    db, _ = make(tmp_path)
    for index in range(3):
        add_meeting(db, f"m{index}", ago=index + 1, project_id="p", minutes=MINUTES)
    w = worker(db, clock=Clock())
    assert w.snapshot()["waiting"]["decisions"] == 0  # 还没跑过
    result = w.run_round()
    assert result["work"] is True and result["phases"]["decisions"] == "done"
    assert len(db.query_all("SELECT id FROM decisions")) == 6
    assert w.snapshot()["waiting"]["decisions"] == 0
    assert w.run_round()["work"] is False


def test_failed_ai_steps_are_counted_but_not_waiting(tmp_path):
    db, _ = make(tmp_path)
    for index in range(2):
        add_meeting(db, f"m{index}", ago=index + 1, project_id="p", minutes=MINUTES)
    w = worker(db, clock=Clock())
    w.run_round()
    db.execute("UPDATE decision_scan SET pair_state = 'failed' WHERE meeting_id = 'm0'")
    w.run_round()
    snap = w.snapshot()
    # 对比试满 3 次仍没做成：只计数，不算「在等」，也不改健康检查的 status
    waiting_rows = db.query_one(
        "SELECT COUNT(*) AS n FROM decision_scan WHERE pair_state IN ('pending', 'running')"
    )["n"]
    assert snap["failed"]["pairs"] == 1 and snap["waiting"]["pairs"] == waiting_rows


def test_l2_finds_a_moved_file_behind_600_rejected_rows(tmp_path):
    db, _ = make(tmp_path)
    root_id = add_root(db, tmp_path / "云图AI")
    add_meeting(db, "m", ago=1, project_id="p")
    old_id = add_file(db, root_id, "报价单.xlsx", gone=True)
    new_id = add_file(db, root_id, "发客户/报价单.xlsx")
    db.execute("UPDATE material_files SET content_key = 'k-quote' WHERE id IN (?, ?)", (old_id, new_id))
    stamp = at(-3600)
    with db.transaction() as connection:
        connection.executemany(
            """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, stem_key, content_key,
                   root_id, rel_path, created_at, updated_at, decided_at)
               VALUES ('mention', 'p', ?, 'rejected', 'llm', 'm', ?, ?, ?, ?, ?, ?, ?)""",
            [(f"m|旧{index}", f"旧{index}", f"k-gone-{index}", root_id, f"旧{index}.docx", stamp, stamp, stamp)
             for index in range(600)],
        )
        relations.upsert_system(
            connection,
            [{"kind": "mention", "project_id": "p", "ident": "m|报价单", "status": "shown", "origin": "llm",
              "meeting_id": "m", "stem_key": "报价单", "file_id": old_id, "content_key": "k-quote",
              "root_id": root_id, "rel_path": "报价单.xlsx"}],
            stamp,
            since=stamp,
        )
    moved = db.query_one("SELECT id FROM relations WHERE ident = 'm|报价单'")["id"]
    before = db.query_all("SELECT id, status, file_id, updated_at FROM relations WHERE status = 'rejected'")
    w = worker(db, clock=Clock())

    first = w.run_round()
    assert first["work"] is True  # 还没看完
    w.run_round()

    assert db.query_one("SELECT file_id, status FROM relations WHERE id = ?", (moved,)) == {
        "file_id": new_id, "status": "shown",
    }
    assert db.query_all("SELECT id, status, file_id, updated_at FROM relations WHERE status = 'rejected'") == before


def test_l2_clears_system_rows_and_leaves_answers_and_manual_rows(tmp_path):
    db, _ = make(tmp_path)
    root_id = add_root(db, tmp_path / "云图AI")
    add_meeting(db, "m", ago=1, project_id="p")
    gone_id = add_file(db, root_id, "旧稿.docx", gone=True)
    other_id = add_file(db, root_id, "别的.docx")
    db.execute("UPDATE material_files SET content_key = 'k-other' WHERE id = ?", (other_id,))
    stamp = at(-3600)
    base = {"project_id": "p", "meeting_id": "m", "root_id": root_id, "rel_path": "旧稿.docx", "file_id": gone_id}
    with db.transaction() as connection:
        relations.upsert_system(
            connection,
            [
                {**base, "kind": "mention", "ident": "m|旧稿", "status": "shown", "origin": "llm", "stem_key": "旧稿",
                 "content_key": "k-old"},
                {**base, "kind": "mention", "ident": "m|手动", "status": "shown", "origin": "llm", "stem_key": "手动",
                 "content_key": "k-old2"},
                # 相关只按内容标识找：同一路径上有别的内容的活文件也算断了线
                {**base, "kind": "related", "ident": "m|k-old3", "status": "shown", "origin": "vector",
                 "content_key": "k-old3", "rel_path": "别的.docx", "file_id": None, "score": 0.7},
            ],
            stamp,
            since=stamp,
        )
    db.execute("UPDATE relations SET origin = 'manual' WHERE ident = 'm|手动'")
    w = worker(db, clock=Clock())
    w.run_round()
    rows = {row["ident"]: row for row in db.query_all("SELECT * FROM relations")}
    assert rows["m|旧稿"]["status"] == "cleared" and rows["m|旧稿"]["updated_at"] == NOW.isoformat()
    assert rows["m|手动"]["status"] == "shown" and rows["m|手动"]["updated_at"] == stamp
    assert rows["m|k-old3"]["status"] == "cleared"


def test_l2_never_undoes_an_answer_made_during_the_round(tmp_path):
    """这一轮开始以后你撤销或回答过的行（updated_at 晚于这一轮的 since），L2 不改。"""
    db, _ = make(tmp_path)
    root_id = add_root(db, tmp_path / "云图AI")
    add_meeting(db, "m", ago=1, project_id="p")
    gone_id = add_file(db, root_id, "旧稿.docx", gone=True)
    later = at(30)
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, stem_key, content_key, root_id,
               rel_path, file_id, created_at, updated_at)
           VALUES ('mention', 'p', 'm|旧稿', 'shown', 'llm', 'm', '旧稿', 'k-old', ?, '旧稿.docx', ?, ?, ?)""",
        (root_id, gone_id, later, later),
    )
    worker(db, clock=Clock()).run_round()
    assert db.query_one("SELECT status, updated_at FROM relations") == {"status": "shown", "updated_at": later}


# ---------------------------------------------------------------------- 清理


def test_housekeeping_once_a_day_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(deep_links, "HOUSEKEEPING_BATCH", 2)
    db, _ = make(tmp_path)
    root_id = add_root(db, tmp_path / "云图AI")
    add_meeting(db, "m", ago=1, project_id="p")

    def event(day):
        db.execute(
            """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, day, at)
               VALUES (?, 1, 'a.txt', '', 'added', ?, ?)""",
            (root_id, day, utc_now()),
        )

    for _ in range(5):
        event("2025-08-01")  # 400 天以前
    event("2025-09-01")

    def relation(ident, status, origin, days):
        stamp = (NOW - timedelta(days=days)).isoformat()
        db.execute(
            """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, created_at, updated_at)
               VALUES ('mention', 'p', ?, ?, ?, 'm', ?, ?)""",
            (ident, status, origin, stamp, stamp),
        )

    for index in range(3):
        relation(f"m|老{index}", "cleared", "llm", 100)
    relation("m|新", "cleared", "llm", 10)
    relation("m|驳回", "rejected", "llm", 400)
    relation("m|手动", "cleared", "manual", 400)

    def candidate(term, status, days, *, undo=None, decided_days=None):
        stamp = (NOW - timedelta(days=days)).isoformat()
        decided = (NOW - timedelta(days=decided_days)).isoformat() if decided_days is not None else None
        db.execute(
            """INSERT INTO glossary_candidates(project_id, term, term_key, status, undo_json, decided_at,
                   created_at, updated_at) VALUES ('p', ?, ?, ?, ?, ?, ?, ?)""",
            (term, term, status, undo, decided, stamp, stamp),
        )

    candidate("老词", "dropped", 100)
    candidate("新词", "dropped", 10)
    candidate("不要", "rejected", 400)
    candidate("记入早", "accepted", 3, undo='{"term_id": "t1"}', decided_days=2)
    candidate("记入晚", "accepted", 0, undo='{"term_id": "t2"}', decided_days=0.01)
    for model in ("old-model", "bge-test"):
        db.execute(
            """INSERT INTO meeting_windows(meeting_id, model, start_ms, end_ms, text_sha, chars, bar, vector)
               VALUES ('m', ?, 0, 90000, 's', 10, 0.6, x'00')""",
            (model,),
        )
    w = worker(db, clock=Clock())

    w.run_round()

    assert [row["day"] for row in db.query_all("SELECT day FROM material_file_events")] == ["2025-09-01"]
    assert sorted(row["ident"] for row in db.query_all("SELECT ident FROM relations")) == ["m|手动", "m|新", "m|驳回"]
    assert {row["term"]: row["undo_json"] for row in db.query_all("SELECT term, undo_json FROM glossary_candidates")} == {
        "新词": None, "不要": None, "记入早": None, "记入晚": '{"term_id": "t2"}',
    }
    assert [row["model"] for row in db.query_all("SELECT model FROM meeting_windows")] == ["bge-test"]
    assert db.query_one("SELECT value FROM app_state WHERE key = 'links_housekeeping_at'")["value"] == NOW.isoformat()

    # 24 小时以内不再清理；过了 24 小时再来
    event("2025-08-02")
    w.moment["value"] = NOW + timedelta(hours=23)
    w.run_round()
    assert len(db.query_all("SELECT id FROM material_file_events")) == 2
    w.moment["value"] = NOW + timedelta(hours=25)
    w.run_round()
    assert len(db.query_all("SELECT id FROM material_file_events")) == 1


# ---------------------------------------------------------------------- 编码锁和向量快照


class CountingEmbedder:
    def __init__(self):
        self.batches: list[int] = []

    def encode(self, texts, **_kwargs):
        self.batches.append(len(texts))
        return np.tile(np.array([1, 0, 0, 0], dtype=np.float32), (len(texts), 1))


def semantic_index(tmp_path):
    db, _ = make(tmp_path)
    settings = SimpleNamespace(semantic_enabled=True, semantic_model="bge-test")
    embedder = CountingEmbedder()
    return db, SemanticIndex(db, settings, embedder=embedder, busy_check=lambda: False), embedder


def test_background_encoding_takes_the_lock_every_32(tmp_path):
    _db, index, embedder = semantic_index(tmp_path)
    vectors = index.encode_texts([f"第 {n} 段" for n in range(70)], background=True)
    assert vectors.shape == (70, 4) and embedder.batches == [32, 32, 6]
    # 用户搜索的 encode_query 不拿锁：后台拿着锁时照样能算
    with index.encode_lock:
        assert index.encode_query("报价单").shape == (4,)


def test_encode_lock_makes_embed_round_take_turns(world):
    db = world.db
    add_content(db, "k-a", [f"第 {n} 段" for n in range(40)])
    embedder = CountingEmbedder()
    index = SemanticIndex(db, vector_settings(), embedder=embedder, busy_check=lambda: False)
    vectors = MaterialVectors(db, vector_settings(), index)
    index.encode_lock.acquire()
    done = threading.Event()
    thread = threading.Thread(target=lambda: (vectors.embed_round(), done.set()))
    thread.start()
    try:
        assert not done.wait(0.3)  # 别的后台编码拿着锁时 embed_round 等着
        assert embedder.batches == []
    finally:
        index.encode_lock.release()
    assert done.wait(5)
    thread.join()
    assert embedder.batches == [32, 8]  # 一轮 64 段的批次里再按 32 条拿锁


def test_semantic_rebuild_encodes_in_locked_batches(tmp_path):
    db, index, embedder = semantic_index(tmp_path)
    db.execute("INSERT INTO meetings(id, title) VALUES ('m', '会')")
    version = db.create_transcript_version("m", "funasr", published=True)
    db.replace_segments(
        version, "m",
        [{"id": f"s{n}", "ordinal": n, "start_ms": n, "end_ms": n + 1, "text": f"第 {n} 句"} for n in range(40)],
    )
    assert index.rebuild() == 40
    assert embedder.batches == [32, 8]


def test_snapshot_does_not_refresh(world, monkeypatch):
    db = matrix_world(world, 3)
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), clock=lambda: 0.0)
    monkeypatch.setattr(vectors, "refresh", lambda: pytest.fail("snapshot 不该刷新"))
    assert vectors.snapshot() is None
    monkeypatch.undo()
    vectors.refresh()
    snap = vectors.snapshot()
    assert snap.n == 3 and snap.dim == 4 and sorted(snap.ids[: snap.n].tolist()) == sorted(
        chunk_ids(db, f"k-{n}")[0] for n in range(3)
    )


def test_snapshot_returns_the_old_matrix_while_a_rebuild_is_stuck(world, monkeypatch):
    db = matrix_world(world, 3)
    clock = {"now": 0.0}
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), clock=lambda: clock["now"])
    old = vectors.refresh()
    clock["now"] = 3600.0  # 到了整份重建的时候
    entered = threading.Event()
    release = threading.Event()
    real_rebuild = vectors._rebuild

    def stuck():
        entered.set()
        release.wait(5)
        return real_rebuild()

    monkeypatch.setattr(vectors, "_rebuild", stuck)
    thread = threading.Thread(target=vectors.refresh)
    thread.start()
    try:
        assert entered.wait(5)
        started = time.perf_counter()
        snap = vectors.snapshot()
        assert time.perf_counter() - started < 0.01
        assert snap.vectors is old.vectors and snap.n == 3
        # 另一个调用方不等重建，照用旧矩阵
        assert vectors.refresh() is old
    finally:
        release.set()
        thread.join()
    assert vectors._matrix is not old and vectors.snapshot().n == 3


def test_new_rows_after_a_snapshot_stay_outside_it(world):
    db = matrix_world(world, 3)
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), clock=lambda: 0.0)
    vectors.refresh()
    snap = vectors.snapshot()
    add_content(db, "k-new", ["新段"])
    put_vector(db, chunk_ids(db, "k-new")[0], [0, 1, 0, 0], "bge-test")
    vectors.refresh()
    assert snap.n == 3 and vectors.snapshot().n == 4


def test_embed_loop_does_not_refresh_while_transcribing(world, monkeypatch):
    db = matrix_world(world, 2)
    busy = {"value": True}
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), busy_check=lambda: busy["value"])
    refreshed: list[int] = []
    monkeypatch.setattr(vectors, "refresh", lambda: refreshed.append(1))
    assert vectors.background_round()["refreshed"] is False and refreshed == []
    busy["value"] = False
    assert vectors.background_round()["refreshed"] is True and refreshed == [1]


# ---------------------------------------------------------------------- 健康检查、bootstrap、命令行


def app_settings(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "api-key"
    key.write_text("sk-test", encoding="utf-8")
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        llm_api_key_file=key,
        material_browse_root=tmp_path / "browse",
        **overrides,
    )


def test_health_has_links_details_without_changing_services(tmp_path):
    from .test_tasks_api import FakeRelayClient

    on = TestClient(create_app(app_settings(tmp_path / "on", links_enabled=True), FakeRelayClient()))
    off = TestClient(create_app(app_settings(tmp_path / "off"), FakeRelayClient()))
    health_on = on.get("/api/health").json()
    health_off = off.get("/api/health").json()

    assert health_off["details"]["links"] == {"enabled": False}
    links = health_on["details"]["links"]
    assert links["enabled"] is True and links["phases"] == {} and links["last_round_at"] is None
    assert set(links) == {
        "enabled", "paused", "last_round_at", "phases", "waiting", "open", "failed", "llm", "calls_today",
    }
    assert links["failed"] == {"mentions": 0, "pairs": 0}
    assert set(links["waiting"]) == {"decisions", "mentions", "pairs", "related", "affects", "terms"}
    assert set(links["open"]) == {"produced", "affects"}
    assert links["calls_today"] == {"background": 0, "qa": 0}
    assert health_on["services"] == health_off["services"] and health_on["status"] == health_off["status"]
    assert "links" not in health_on["services"]


def test_links_snapshot_does_not_touch_the_database(tmp_path):
    db, _ = make(tmp_path)
    w = worker(db, clock=Clock())
    w.run_round()
    w.db = None  # 快照只看内存
    snap = w.snapshot()
    assert snap["enabled"] is True and snap["last_round_at"] == "2026-09-27T08:00:00Z"
    assert snap["phases"]["decisions"] == "done"


def test_bootstrap_reports_links_flags(tmp_path):
    from .test_tasks_api import FakeRelayClient

    client = TestClient(create_app(app_settings(tmp_path), FakeRelayClient()))
    payload = client.get("/api/bootstrap").json()
    assert payload["llm_configured"] is True and payload["links_enabled"] is False
    other = app_settings(tmp_path / "second", links_enabled=True)
    other.llm_api_key_file.unlink()
    payload = TestClient(create_app(other, FakeRelayClient())).get("/api/bootstrap").json()
    assert payload["llm_configured"] is False and payload["links_enabled"] is True


def cli_world(tmp_path, monkeypatch):
    path = tmp_path / "workbench.sqlite3"
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(path))
    monkeypatch.setattr(cli, "_server_json", lambda *_args, **_kwargs: None)
    db = Database(path)
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    add_meeting(db, "m", ago=1, project_id="p", minutes=MINUTES)
    add_meeting(db, "m2", ago=2, project_id="p")
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, attempts, created_at, updated_at)
           VALUES ('m2', 'v', 's', 'failed', 3, ?, ?)""",
        (utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES ('links_llm_usage', ?, ?)",
        (json.dumps({"day": datetime.now().date().isoformat(), "background": 7, "qa": 2}), utc_now()),
    )
    return db, path


def test_cli_links_status_reads_the_database(tmp_path, monkeypatch, capsys):
    _db, path = cli_world(tmp_path, monkeypatch)
    stamp = path.stat().st_mtime_ns

    assert cli.main(["links", "status"]) == 0
    out = capsys.readouterr().out
    assert "服务没开" in out
    assert "决议入库 2 场" in out and "没做成 1 场" in out
    assert "后台 7 / 0 次" not in out and "后台 7 / " in out and "问答 2 / " in out
    assert cli.main(["links", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["waiting"]["decisions"] == 2 and payload["failed"]["mentions"] == 1
    assert payload["calls_today"] == {"background": 7, "qa": 2} and payload["server"] is False
    assert path.stat().st_mtime_ns == stamp  # 只读


def test_cli_links_decisions_and_retry(tmp_path, monkeypatch, capsys):
    db, _path = cli_world(tmp_path, monkeypatch)

    assert cli.main(["links", "decisions", "--meeting", "m"]) == 0
    out = capsys.readouterr().out
    assert "当场解析" in out and "[00:12:34]" in out and "报价单按第三版发出" in out
    LinksWorker(db, loop_settings(), clock=Clock(), now=lambda: NOW).run_round()
    assert cli.main(["links", "decisions", "--meeting", "m", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "table" and [item["id"][:4] for item in payload["decisions"]] == ["dec-", "dec-"]
    # 4c：对比时会发的提示词，只打印，不发送（测试护栏拦着真的 AI 请求）
    assert payload["prompt"]["user"].startswith("<this>\nn1 [") and "报价单按第三版发出" in payload["prompt"]["user"]
    assert cli.main(["links", "decisions", "--meeting", "m"]) == 0
    out = capsys.readouterr().out
    assert "---- system ----" in out and "<others>" in out and "不发送" in out
    assert cli.main(["links", "decisions", "--meeting", "m2"]) == 0
    assert "no_minutes" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["links", "decisions", "--meeting", "不存在"])

    assert cli.main(["links", "retry"]) == 0
    assert "已放回 1 场" in capsys.readouterr().out
    assert db.query_one("SELECT state, attempts FROM mention_extractions") == {"state": "pending", "attempts": 0}


def test_doctor_reports_links_without_requiring_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(tmp_path / "workbench.sqlite3"))
    monkeypatch.setenv("MEETING_WORKBENCH_ARCHIVE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEETING_WORKBENCH_STAGING_ROOT", str(tmp_path))
    monkeypatch.setenv("MEETING_WORKBENCH_SEMANTIC_ENABLED", "0")
    code = cli.main(["doctor"])
    report = json.loads(capsys.readouterr().out)
    links = report["links"]
    assert code == 0  # 没 key、关着都不影响退出码
    assert links["enabled"] is False and links["key_ready"] is False
    assert links["key_file"] == "/nonexistent/meeting-workbench-test-key"
    assert links["host"] == "127.0.0.1" and "MEETING_WORKBENCH_LINKS_ENABLED" in links["note"]
