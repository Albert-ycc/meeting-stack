"""第四期 4d：相关（related.py、related_read.py）：切窗、打分、共同词、副本和到处相关、连成相关、回答、重算和
增量、运行（忙、全文表在补、矩阵没建过、打开的会优先、GET 不写库）、接口和［用本机应用打开］的各种拒绝。

假编码器按字的二元组哈希给确定的向量：两段文字共有的二元组越多越像。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_workbench import related, related_read, relations
from meeting_workbench.db import Database, utc_now
from meeting_workbench.deep_links import LinksWorker
from meeting_workbench.main import create_app
from meeting_workbench.material_fts import REBUILD_KEY
from meeting_workbench.material_vectors import MaterialVectors
from meeting_workbench.materials import ROOT_ONLINE
from meeting_workbench.semantic import SemanticUnavailable

from .test_graph import add_meeting
from .test_links_loop import Clock, app_settings
from .test_material_search import add_file

MODEL = "bge-test"
DIM = 256
NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
KEY_A = "q2:" + "a" * 32
KEY_B = "q2:" + "b" * 32
KEY_C = "q2:" + "c" * 32
TOPIC_A = [
    "驻场服务的接口文档先按第三版来",
    "字段命名统一用小驼峰写法不要混用",
    "接口文档里驻场服务的报价要单列出来",
    "下周三之前把驻场服务的排期发给客户",
    "接口文档的错误码部分也要一起补齐",
    "驻场服务人员名单周五之前确认好",
    "测试环境的账号密码由运维单独发放",
    "上线窗口定在月底最后一个周六晚上",
    "回滚方案要写清楚每一步谁来负责",
    "监控告警的阈值先沿用现在的配置",
]
FILLER = [
    "食堂下个月开始增加一个面食窗口",
    "停车场东侧入口周末暂时封闭维修",
    "年会节目报名截止到这个月十五号",
    "新同事的电脑申请走行政的流程",
    "会议室预订系统升级以后要重新登录",
    "办公区绿植统一由物业公司来养护",
    "快递柜挪到了一楼大厅靠近电梯",
    "周五下午有一场消防安全演练",
]


def embed(text: str) -> np.ndarray:
    vector = np.zeros(DIM, dtype=np.float32)
    body = "".join(char for char in text if char.isalnum())
    for index in range(len(body) - 1):
        vector[int(hashlib.md5(body[index : index + 2].encode()).hexdigest()[:8], 16) % DIM] += 1
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


class FakeSemantic:
    """后台编码：记下每次编几条；missing 时抛 SemanticUnavailable。"""

    def __init__(self, on_encode=None):
        self.calls: list[int] = []
        self.missing = False
        self.on_encode = on_encode
        self.encode_lock = threading.Lock()

    def encode_texts(self, texts, *, background=True, batch_size=32):
        if self.missing:
            raise SemanticUnavailable("本地语义模型依赖尚未安装")
        self.calls.append(len(texts))
        if self.on_encode is not None:
            self.on_encode()
        return np.vstack([embed(text) for text in texts])


def settings(**overrides):
    values = {
        "links_enabled": True,
        "semantic_enabled": True,
        "material_content_enabled": True,
        "semantic_model": MODEL,
        "related_floor": 0.6,
        "related_margin": 0.05,
        "links_backfill_days": 180,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def segments_for(sentences, start=0, step=15_000):
    return [(start + index * step, text) for index, text in enumerate(sentences)]


def add_content(db, key, chunks, *, vectors=True):
    now = utc_now()
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
           VALUES (?, 'text', 'done', 100, ?, ?, ?)""",
        (key, len(chunks), now, now),
    )
    for ordinal, text in enumerate(chunks):
        db.execute(
            "INSERT INTO material_chunks(content_key, ordinal, loc, text) VALUES (?, ?, ?, ?)",
            (key, ordinal, f"第 {ordinal + 1} 节", text),
        )
    if vectors:
        embed_all(db)


def embed_all(db):
    for row in db.query_all(
        """SELECT c.id, c.text FROM material_chunks c WHERE NOT EXISTS (
               SELECT 1 FROM material_chunk_vectors v WHERE v.chunk_id = c.id AND v.model = ?) ORDER BY c.id""",
        (MODEL,),
    ):
        db.execute(
            "INSERT INTO material_chunk_vectors(chunk_id, model, vector, created_at) VALUES (?, ?, ?, ?)",
            (row["id"], MODEL, embed(row["text"]).astype(np.float16).tobytes(), utc_now()),
        )


def add_term(db, term, *, project_id="p", aliases=(), confirmed=1):
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, confirmed, project_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"t-{term}",
            term,
            json.dumps(list(aliases), ensure_ascii=False),
            confirmed,
            project_id,
            now,
            now,
        ),
    )


def build(tmp_path: Path, *, chunk_a=True, meeting=True):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    for project_id, name in (("p", "云图AI"), ("q", "别的项目")):
        db.execute(
            "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)",
            (project_id, name, utc_now()),
        )
    root_path = tmp_path / "云图资料"
    root_path.mkdir(exist_ok=True)
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
        (str(root_path), utc_now()),
    )
    root = db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'p'")["id"]
    add_term(db, "驻场服务")
    # 一份不在任何根目录里的内容：材料向量矩阵才建得起来（范围外，不会成为候选）
    add_content(db, "q2:" + "f" * 32, ["。".join(FILLER)])
    if chunk_a:
        add_content(db, KEY_A, ["。".join(TOPIC_A)])
        add_file(db, root, "需求/接口文档.docx", key=KEY_A, mtime=5)
    if meeting:
        add_meeting(db, "m", ago=1, project_id="p", segments=segments_for(TOPIC_A + FILLER))
    semantic = FakeSemantic()
    config = settings()
    vectors = MaterialVectors(db, config, semantic, clock=lambda: 0.0)
    vectors.refresh()
    return SimpleNamespace(
        db=db, root=root, root_path=root_path, semantic=semantic, settings=config, vectors=vectors
    )


def worker_for(w, **kwargs):
    return LinksWorker(
        w.db,
        w.settings,
        semantic=w.semantic,
        vectors=w.vectors,
        clock=kwargs.pop("clock", Clock()),
        now=lambda: NOW,
        **kwargs,
    )


def rev(db, key):
    row = db.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
    return int(row["value"]) if row else 0


def passages(db, meeting_id="m"):
    return db.query_all(
        "SELECT start_ms, rank, content_key, words, seg_ms FROM meeting_window_passages WHERE meeting_id = ? "
        "ORDER BY start_ms, rank",
        (meeting_id,),
    )


def related_rows(db):
    return db.query_all(
        "SELECT id, ident, status, origin, at_ms, quote, evidence_json, content_key, file_id FROM relations "
        "WHERE kind = 'related' ORDER BY id"
    )


# ---------------------------------------------------------------------- 切窗


def test_windows_are_90_seconds_every_45_and_short_ones_are_skipped():
    long_text = "驻场服务的接口文档先按第三版来，字段命名统一用小驼峰写法不要混用，" * 3
    windows = related.cut_windows(
        [(0, long_text), (50_000, long_text), (100_000, "嗯"), (200_000, "好的")]
    )
    assert [(window.start_ms, window.end_ms) for window in windows] == [
        (0, 90_000),
        (45_000, 135_000),
    ]
    # 窗 45 秒：50 秒和 100 秒两段；100 秒那一段只有「嗯」，不影响
    assert windows[1].text.startswith(long_text)
    huge = [(index * 1_000, "字" * 100) for index in range(10)]
    assert all(len(window.text) <= related.WINDOW_CHARS for window in related.cut_windows(huge))
    # 沉默和「嗯」「好」的窗不要
    assert related.cut_windows([(0, "嗯"), (10_000, "好的好的")]) == []
    assert related.text_sha("a", "文字") != related.text_sha("b", "文字")


def test_editing_one_segment_reencodes_at_most_two_windows(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    first = sum(w.semantic.calls)
    assert first == len(related.cut_windows(segments_for(TOPIC_A + FILLER)))
    # 原地改一段（和拆句、合并、保存草稿一样，由事件触发器记一笔要重算）
    db = w.db
    db.execute(
        "UPDATE segments SET text = '食堂的菜单这周换成了川菜为主' WHERE meeting_id = 'm' AND ordinal = 12"
    )
    db.add_event("transcript_draft_saved", meeting_id="m")
    assert (
        db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id = 'm'")["dirty"] == 1
    )
    w.semantic.calls.clear()
    worker_for(w).run_round()
    assert 1 <= sum(w.semantic.calls) <= 2
    assert (
        db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id = 'm'")["dirty"] == 0
    )


# ---------------------------------------------------------------------- 打分


def test_bar_is_floor_or_p95_plus_margin():
    rng = np.random.default_rng(1)
    windows = rng.normal(size=(3, 8)).astype(np.float32)
    windows /= np.linalg.norm(windows, axis=1, keepdims=True)
    sample = rng.normal(size=(300, 8)).astype(np.float32)
    sample /= np.linalg.norm(sample, axis=1, keepdims=True)
    bars = related.window_bars(windows, sample, 30, 0.6, 0.05)
    expected = np.maximum(0.6, np.percentile(windows @ sample.T, 95, axis=1) + 0.05)
    assert np.allclose(bars, expected)
    # 样本少（不到 200 段或 20 份内容）：floor + 0.04
    assert np.allclose(related.window_bars(windows, sample[:150], 30, 0.6, 0.05), 0.64)
    assert np.allclose(related.window_bars(windows, sample, 19, 0.6, 0.05), 0.64)


def test_background_sample_takes_at_most_four_per_content_and_is_stable():
    rows = [
        (content * 100 + ordinal, f"k{content}", ordinal)
        for content in range(1_500)
        for ordinal in range(9)
    ]
    picked = related.pick_sample(rows)
    assert len(picked) == related.SAMPLE_MAX
    per_content: dict[int, int] = {}
    for chunk_id in picked:
        per_content[chunk_id // 100] = per_content.get(chunk_id // 100, 0) + 1
    assert max(per_content.values()) <= 4
    assert related.pick_sample(list(reversed(rows))) == picked
    # 边读边只留每份内容最小的 4 个：和先全排序再取的结果一样
    by_content: dict[str, list[tuple[str, int]]] = {}
    for chunk_id, key, ordinal in rows:
        by_content.setdefault(key, []).append((related._sample_key(key, ordinal), chunk_id))
    reference = sorted(entry for items in by_content.values() for entry in sorted(items)[:4])
    assert picked == [chunk_id for _key, chunk_id in reference[: related.SAMPLE_MAX]]


def test_passage_quality_filter():
    assert related.good_passage(
        "驻场服务的接口文档先按第三版来，字段命名统一用小驼峰写法不要混用，错误码部分也要一起补齐"
    )
    assert not related.good_passage("太短的片段")
    assert not related.good_passage(
        "1,200 3,400 5,600 7,800 9,000 1,100 2,200 3,300 4,400 5,500 6,600 7,700"
    )


def test_one_meeting_gets_passages_links_and_only_pointers(tmp_path):
    w = build(tmp_path)
    graph_before = rev(w.db, "graph_rev")
    related_before = rev(w.db, "related_rev")
    result = worker_for(w).run_round()
    assert result["phases"]["related"] == "done"
    rows = passages(w.db)
    assert rows, "没找到片段"
    assert {row["content_key"] for row in rows} == {KEY_A}
    assert json.loads(rows[0]["words"]) == ["驻场服务", "接口文档"]
    # 每个窗每份内容只有一段
    assert len({(row["start_ms"], row["content_key"]) for row in rows}) == len(rows)
    [link] = related_rows(w.db)
    assert (link["ident"], link["status"], link["origin"]) == (f"m|{KEY_A}", "shown", "vector")
    evidence = json.loads(link["evidence_json"])
    assert set(evidence) == {"words", "windows", "meeting", "material"}
    assert set(evidence["material"]) == {"content_key", "ordinal", "loc"}
    assert evidence["windows"] >= 2 and "驻场服务" in link["quote"] and len(link["quote"]) <= 80
    # 只动 related_rev
    assert rev(w.db, "graph_rev") == graph_before and rev(w.db, "related_rev") > related_before
    scan = w.db.query_one("SELECT * FROM meeting_related_scan WHERE meeting_id = 'm'")
    assert scan["dirty"] == 0 and scan["sig"] and scan["scanned_at"] and scan["note"] is None
    # 没变的重算什么都不写
    before = (rev(w.db, "related_rev"), rev(w.db, "graph_rev"), related_rows(w.db))
    with w.db.transaction() as connection:
        related.mark_dirty(connection, meeting_id="m")
    worker_for(w).run_round()
    assert (rev(w.db, "related_rev"), rev(w.db, "graph_rev"), related_rows(w.db)) == before


def test_rows_outside_the_matrix_are_read_from_the_database(tmp_path):
    w = build(tmp_path, chunk_a=False)
    # 快照建好以后才有的片段（矩阵外）
    add_content(w.db, KEY_A, ["。".join(TOPIC_A)])
    add_file(w.db, w.root, "需求/接口文档.docx", key=KEY_A, mtime=5)
    assert KEY_A not in w.vectors.snapshot().code_of
    worker_for(w).run_round()
    assert {row["content_key"] for row in passages(w.db)} == {KEY_A}


def test_too_many_rows_outside_the_matrix_mark_partial(tmp_path, monkeypatch):
    w = build(tmp_path, chunk_a=False)
    add_content(w.db, KEY_A, ["。".join(TOPIC_A)])
    add_content(w.db, KEY_B, ["。".join(FILLER)])
    add_file(w.db, w.root, "需求/接口文档.docx", key=KEY_A, mtime=5)
    add_file(w.db, w.root, "杂事.docx", key=KEY_B, mtime=1)
    monkeypatch.setattr(related, "EXTRA_MAX", 1)
    worker_for(w).run_round()
    assert (
        w.db.query_one("SELECT partial FROM meeting_related_scan WHERE meeting_id = 'm'")["partial"]
        == 1
    )
    # 跳过的是最旧的内容，新的照样比
    assert {row["content_key"] for row in passages(w.db)} == {KEY_A}
    # 矩阵重建完成后 partial 的会 dirty 加一
    w.vectors._built_at = -10_000.0
    w.vectors.refresh()
    worker = worker_for(w)
    worker.related._seen_matrix = object()
    worker.run_round()
    # 新矩阵里两份都有了：这一轮重算，不再 partial
    assert w.db.query_one(
        "SELECT partial, dirty FROM meeting_related_scan WHERE meeting_id = 'm'"
    ) == {"partial": 0, "dirty": 0}


def _score_world(tmp_path):
    """KEY_A 在矩阵里；KEY_B、KEY_C 是快照之后才有的（矩阵外），KEY_C 最新。"""
    w = build(tmp_path)
    add_content(
        w.db,
        KEY_B,
        ["。".join(TOPIC_A[3:] + FILLER[:2]), "。".join(FILLER), "。".join(TOPIC_A[:5])],
    )
    add_content(w.db, KEY_C, ["。".join(TOPIC_A[::-1]), "。".join(FILLER[3:] + TOPIC_A[:2])])
    add_file(w.db, w.root, "排期/附录.docx", key=KEY_B, mtime=3)
    add_file(w.db, w.root, "排期/新版.docx", key=KEY_C, mtime=9)
    windows = np.vstack(
        [
            embed("。".join(TOPIC_A[index : index + 4] + FILLER[index : index + 2]))
            for index in range(5)
        ]
    )
    ctx = SimpleNamespace(
        settings=w.settings, stopping=lambda: False, busy_now=lambda: False, clock=lambda: 0.0
    )
    return w, windows.astype(np.float32), ctx


def _brute_top(connection, keys, windows):
    rows = connection.execute(
        f"""SELECT v.chunk_id, v.vector FROM material_chunk_vectors v JOIN material_chunks c ON c.id = v.chunk_id
             WHERE v.model = ? AND c.content_key IN ({", ".join("?" for _ in keys)}) ORDER BY v.chunk_id""",
        [MODEL, *keys],
    ).fetchall()
    ids = np.asarray([row[0] for row in rows], dtype=np.int64)
    matrix = np.vstack([np.frombuffer(row[1], dtype=np.float16).astype(np.float32) for row in rows])
    scores = windows @ matrix.T
    top = np.argsort(-scores, axis=1)[:, : related.TOP_SCORED]
    return (
        ids,
        np.take_along_axis(np.broadcast_to(ids, scores.shape), top, 1),
        np.take_along_axis(scores, top, 1),
    )


def test_score_reads_ids_as_arrays_and_matches_a_brute_force_top_six(tmp_path, monkeypatch):
    w, windows, ctx = _score_world(tmp_path)
    snap = w.vectors.snapshot()
    assert KEY_A in snap.code_of and KEY_B not in snap.code_of and KEY_C not in snap.code_of
    # 矩阵块也按 4,096 行转 float32：调小以后一块要分几段，结果不变
    monkeypatch.setattr(related, "EXTRA_BLOCK", 1)
    with w.db.autocommit() as connection:
        scope = related.load_scope(connection, "p")
        everything = related.scope_chunks(connection, sorted(scope.files), MODEL)
        assert everything.dtype.names == ("id", "key", "ordinal") and everything.shape[0] == 6
        scored = related.RelatedPass().score(ctx, connection, snap, scope, windows, 1e9)
        all_ids, top_ids, top_scores = _brute_top(connection, [KEY_A, KEY_B, KEY_C], windows)
    order = np.argsort(-scored.scores, axis=1, kind="stable")
    assert np.allclose(np.take_along_axis(scored.scores, order, 1), top_scores, atol=1e-6)
    assert [set(row) for row in scored.ids.tolist()] == [set(row) for row in top_ids.tolist()]
    assert scored.partial is False and scored.mark == int(all_ids.max())


def test_score_outside_the_matrix_takes_the_newest_contents_first(tmp_path, monkeypatch):
    w, windows, ctx = _score_world(tmp_path)
    monkeypatch.setattr(related, "EXTRA_MAX", 2)  # 矩阵外 5 段，只读得下 KEY_C 的 2 段
    read: list[list[int]] = []
    real = related._read_vectors

    def spy(connection, chunk_ids, model):
        read.append(list(chunk_ids))
        return real(connection, chunk_ids, model)

    monkeypatch.setattr(related, "_read_vectors", spy)
    with w.db.autocommit() as connection:
        scope = related.load_scope(connection, "p")
        scored = related.RelatedPass().score(
            ctx, connection, w.vectors.snapshot(), scope, windows, 1e9
        )
        newest = [
            row[0]
            for row in connection.execute(
                "SELECT v.chunk_id FROM material_chunk_vectors v JOIN material_chunks c ON c.id = v.chunk_id "
                "WHERE c.content_key = ? ORDER BY v.chunk_id",
                (KEY_C,),
            ).fetchall()
        ]
        _ids, top_ids, top_scores = _brute_top(connection, [KEY_A, KEY_C], windows)
    assert scored.partial is True and read[0] == newest
    assert [set(row) for row in scored.ids.tolist()] == [set(row) for row in top_ids.tolist()]


# ---------------------------------------------------------------------- 共同词


def _words(w, sentence):
    connection = w.db.connect()  # 测试结束随进程关
    checks = related.WordChecks()
    checks.load_cues(connection)
    scope = related.load_scope(connection, "p")
    words = related.Words(connection, scope, checks)
    [window] = related.cut_windows(
        [(0, sentence), (5_000, "补充一句这周的安排大家都清楚了没有问题就先这样吧谢谢各位")]
    )
    return lambda chunk_id, text: [
        found.word for found in words.shared(window, words.in_window(window), chunk_id, text)
    ]


def test_two_character_terms_aliases_stems_and_the_project_name(tmp_path):
    w = build(tmp_path)
    add_term(w.db, "驻场")
    add_term(w.db, "司美格鲁肽", aliases=["司美格鲁太"])
    root = w.root
    add_file(w.db, root, "报价单.xlsx", key=None)
    add_file(w.db, root, "需求.docx", key=None)  # 常用的两个字，不算
    add_file(w.db, root, "云图AI.docx", key=None)  # 项目名，不算
    sentence = "驻场这件事和司美格鲁太的剂量都要看一下，报价单和需求还有云图AI的排期也在这周一起定下来，大家回去准备一下，下周一再碰"
    shared = _words(w, sentence)
    text = "驻场人员说明：司美格鲁肽的剂量见附表，报价单按第三版，需求变更记录在云图AI的系统里，排期另行通知"
    found = shared(1, text)
    assert "驻场" in found and "司美格鲁肽" in found and "报价单" in found
    assert "需求" not in found and "云图AI" not in found and "司美格鲁太" not in found
    # 没有共同词：丢掉
    assert (
        shared(2, "完全不相干的一段话，讲的是食堂菜单和停车位，没有任何项目里的专门说法可以对上的")
        == []
    )


def test_dynamic_pieces_must_be_rare_and_local(tmp_path):
    w = build(tmp_path)
    sentence = "我们说一下能耗看板的上线时间，另外月度复盘报告也要交，大家有问题会后再单独找我，这周就先到这里吧，散会以后各自忙"
    text = "能耗看板计划在十月上线，月度复盘报告模板见附件，其余事项另行通知，以上内容请各位知悉并按时完成"
    shared = _words(w, sentence)
    assert "能耗看板" in shared(1, text)
    # 在别的项目的会里常见（至少 2 个别的项目）：不算
    for index, project in enumerate(("x", "y")):
        w.db.execute(
            "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)",
            (project, f"项目{index}", utc_now()),
        )
        add_meeting(w.db, f"o{index}", ago=3, project_id=project, title="能耗看板周会")
    shared = _words(w, sentence)
    assert "能耗看板" not in shared(1, text)
    # 在本项目的会里泛滥（至少 4 场、超过一半）：不算
    for index in range(4):
        add_meeting(
            w.db,
            f"n{index}",
            ago=4 + index,
            project_id="p",
            segments=[(0, "月度复盘报告今天过一遍")],
        )
    shared = _words(w, sentence)
    assert "月度复盘报告" not in shared(1, text)


def test_common_pieces_strip_fillers_and_limits():
    assert "能耗看板" in related.common_pieces("看一下能耗看板吧", "能耗看板的上线")
    assert related.common_pieces("123456", "123456") == []
    assert related.common_pieces("一二三四五六七八九十甲乙丙", "一二三四五六七八九十甲乙丙") == []


# ---------------------------------------------------------------------- 副本和到处相关


def test_copies_need_most_windows_and_a_high_average():
    tops = [("copy", 0.95)] * 4 + [("other", 0.9)] * 2
    assert related.copies_of(10, tops) == ["copy"]
    assert related.copies_of(20, tops) == []  # 30% 是 6 个窗
    assert related.copies_of(10, [("copy", 0.8)] * 5) == []


def test_exported_transcript_is_a_copy_and_not_linked(tmp_path):
    w = build(tmp_path, chunk_a=False)
    windows = related.cut_windows(segments_for(TOPIC_A + FILLER))
    add_content(w.db, KEY_C, [window.text + "驻场服务" for window in windows])
    add_file(w.db, w.root, "纪要-0921.docx", key=KEY_C, mtime=9)
    worker_for(w).run_round()
    scan = w.db.query_one("SELECT copies_json FROM meeting_related_scan WHERE meeting_id = 'm'")
    assert json.loads(scan["copies_json"]) == [KEY_C]
    assert related_rows(w.db) == []
    with w.db.autocommit() as connection:
        payload = related_read.panel(connection, "m", worker=None, settings=w.settings, local=True)
    assert payload["copies"] and payload["copies"][0]["name"] == "纪要-0921.docx"
    assert all(
        item["content_key"] != KEY_C for window in payload["windows"] for item in window["items"]
    )


def test_hub_content_is_hidden_and_not_linked(tmp_path):
    w = build(tmp_path)
    for index in range(6):
        add_meeting(
            w.db, f"h{index}", ago=2 + index, project_id="p", segments=segments_for(TOPIC_A)
        )
    worker = worker_for(w)
    for _ in range(4):
        worker.run_round()
    assert (
        w.db.query_one(
            "SELECT COUNT(*) AS n FROM meeting_related_scan WHERE scanned_at IS NOT NULL"
        )["n"]
        == 7
    )
    with w.db.autocommit() as connection:
        assert related.hub_keys(connection, "p", [KEY_A]) == {KEY_A}
        payload = related_read.panel(connection, "m", worker=None, settings=w.settings, local=True)
        edges = related_read.project_related(connection, "p", window="all", today=date(2026, 9, 28))
    assert payload["windows"] == [] and edges["edges"] == []


# ---------------------------------------------------------------------- 线


def test_link_thresholds_and_top_five():
    scope = related.Scope("p", "云图AI", [], [1], {}, {})
    rows = []
    for index in range(7):
        key = f"k{index}"
        scope.files[key] = {"id": index + 1, "root_id": 1, "rel_path": f"{key}.docx"}
        for window in range(2):
            rows.append(
                {
                    "start_ms": window * 45_000,
                    "content_key": key,
                    "ordinal": 0,
                    "score": 0.7 + index / 100,
                    "words": json.dumps(["驻场服务"]),
                    "seg_ms": 0,
                    "loc": None,
                }
            )
    scope.files["one"] = {"id": 99, "root_id": 1, "rel_path": "one.docx"}
    rows.append(
        {
            "start_ms": 0,
            "content_key": "one",
            "ordinal": 0,
            "score": 0.99,
            "words": json.dumps(["驻场"]),
            "seg_ms": 0,
            "loc": None,
        }
    )
    scope.files["two"] = {"id": 98, "root_id": 1, "rel_path": "two.docx"}
    rows.append(
        {
            "start_ms": 0,
            "content_key": "two",
            "ordinal": 0,
            "score": 0.5,
            "words": json.dumps(["驻场", "报价单"]),
            "seg_ms": 0,
            "loc": None,
        }
    )
    links = related.link_rows("m", scope, rows, skip=set(), segments={0: "驻场服务的事"})
    idents = [row["ident"] for row in links]
    assert len(links) == 5 and "m|one" not in idents
    assert idents[0] == "m|k6"  # 窗数一样时共同词多的、分高的在前
    links = related.link_rows("m", scope, rows[-2:], skip=set(), segments={})
    assert [row["ident"] for row in links] == ["m|two"]  # 1 个窗但 2 个共同词


def test_dead_content_is_cleared(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    assert related_rows(w.db)[0]["status"] == "shown"
    w.db.execute("UPDATE material_files SET gone_at = ? WHERE content_key = ?", (utc_now(), KEY_A))
    with w.db.transaction() as connection:
        related.mark_dirty(connection, meeting_id="m")
    worker_for(w).run_round()
    assert related_rows(w.db)[0]["status"] == "cleared"
    with w.db.autocommit() as connection:
        payload = related_read.panel(connection, "m", worker=None, settings=w.settings, local=True)
    assert payload["windows"] == []


# ---------------------------------------------------------------------- 回答


def _reject(db, content_key=KEY_A, file_id=None, meeting_id="m"):
    if file_id is None:
        file_id = db.query_one(
            "SELECT id FROM material_files WHERE content_key = ?", (content_key,)
        )["id"]
    with db.transaction() as connection:
        return relations.reject_related(
            connection, meeting_id, content_key=content_key, file_id=file_id, now=utc_now()
        )


def test_reject_without_a_row_creates_a_manual_row_and_undo_deletes_it(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    w.db.execute("DELETE FROM relations")  # 这一条只在窗里，没连成线
    result = _reject(w.db)
    row = w.db.query_one(
        "SELECT origin, status, root_id, rel_path FROM relations WHERE kind = 'related'"
    )
    assert row == {
        "origin": "manual",
        "status": "rejected",
        "root_id": w.root,
        "rel_path": "需求/接口文档.docx",
    }
    assert result["undo_until"]
    with w.db.autocommit() as connection:
        assert (
            related_read.panel(connection, "m", worker=None, settings=w.settings, local=True)[
                "windows"
            ]
            == []
        )
        assert (
            related_read.panel(connection, "m", worker=None, settings=w.settings, local=True)[
                "rejected"
            ]
            == 1
        )
    with w.db.transaction() as connection:
        relations.undo(connection, result["relation"]["id"], utc_now())
    assert related_rows(w.db) == []
    # 改回相关也整行删掉
    result = _reject(w.db)
    with w.db.transaction() as connection:
        relations.answer(connection, result["relation"]["id"], {"answer": "restore"}, utc_now())
    assert related_rows(w.db) == []


def test_reject_a_vector_row_and_restore_it(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    result = _reject(w.db)
    [row] = related_rows(w.db)
    assert (row["origin"], row["status"]) == ("vector", "rejected")
    with pytest.raises(relations.RelationError) as handled:
        _reject(w.db)
    assert (handled.value.status, str(handled.value)) == (409, "这条已经处理过了")
    with w.db.transaction() as connection:
        relations.answer(connection, result["relation"]["id"], {"answer": "restore"}, utc_now())
    assert related_rows(w.db)[0]["status"] == "shown"


def test_reject_errors(tmp_path):
    w = build(tmp_path)
    file_id = w.db.query_one("SELECT id FROM material_files WHERE content_key = ?", (KEY_A,))["id"]
    cases = []
    for meeting_id, key, target in (("nope", KEY_A, file_id), ("m", KEY_A, 9_999)):
        with pytest.raises(relations.RelationError) as error:
            _reject(w.db, key, target, meeting_id)
        cases.append((error.value.status, str(error.value)))
    other_root = w.db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('q', '/x', ?)",
        (utc_now(),),
    )
    assert other_root is not None
    q_root = w.db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'q'")["id"]
    other = add_file(w.db, q_root, "别的.docx", key=KEY_B)
    with pytest.raises(relations.RelationError) as error:
        _reject(w.db, KEY_B, other)
    cases.append((error.value.status, str(error.value)))
    add_meeting(w.db, "loose", ago=1)
    with pytest.raises(relations.RelationError) as error:
        _reject(w.db, KEY_A, file_id, "loose")
    cases.append((error.value.status, str(error.value)))
    assert cases == [
        (404, "会议不存在"),
        (404, "这份文件不在索引里了"),
        (422, "这份文件不在这个项目的资料盘里"),
        (409, "这场会没归项目"),
    ]


def test_a_rejection_blocks_by_content_or_by_place():
    rejected = ({KEY_A}, {(1, "旧/接口文档.docx")})
    moved = {"root_id": 1, "rel_path": "新/接口文档.docx"}
    changed = {"root_id": 1, "rel_path": "旧/接口文档.docx"}
    assert related.is_blocked(KEY_A, moved, rejected)  # 挪了位置，按内容仍挡
    assert related.is_blocked(KEY_B, changed, rejected)  # 原地改了内容，按路径仍挡
    assert not related.is_blocked(KEY_B, moved, rejected)  # 挪了又改的不挡


def test_content_related_meetings_skip_hubs_and_rejected_places(tmp_path, monkeypatch):
    w = build(tmp_path)
    worker_for(w).run_round()
    file_id = w.db.query_one("SELECT id FROM material_files WHERE content_key = ?", (KEY_A,))["id"]
    with w.db.autocommit() as connection:
        assert [
            row["meeting_id"] for row in related_read.related_meetings(connection, file_id)
        ] == ["m"]
        # 到处都相关的内容：和项目图谱一样不列
        monkeypatch.setattr(related, "HUB_MIN_MEETINGS", 1)
        assert related_read.related_meetings(connection, file_id) == []
    monkeypatch.setattr(related, "HUB_MIN_MEETINGS", 6)
    # 这场会里标过不相关的是这个位置上的旧内容：文件原地改过，按路径仍挡
    add_content(w.db, KEY_B, ["。".join(FILLER)])
    old = add_file(w.db, w.root, "旧/接口文档.docx", key=KEY_B)
    _reject(w.db, KEY_B, old)
    w.db.execute(
        "UPDATE material_files SET rel_path = '旧/挪走了.docx', gone_at = ? WHERE id = ?",
        (utc_now(), old),
    )
    w.db.execute("UPDATE material_files SET rel_path = '旧/接口文档.docx' WHERE id = ?", (file_id,))
    with w.db.autocommit() as connection:
        assert related_read.related_meetings(connection, file_id) == []


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": "http://testserver"}


def test_reject_endpoint_validates_the_body(tmp_path):
    client = TestClient(create_app(app_settings(tmp_path)))
    headers = write_headers(client)
    url = "/api/meetings/m/related-materials/reject"
    bad = client.post(url, json={"content_key": "../etc", "file_id": 1}, headers=headers)
    assert bad.status_code == 422
    extra = client.post(url, json={"content_key": KEY_A, "file_id": 1, "why": "x"}, headers=headers)
    assert extra.status_code == 422
    missing = client.post(url, json={"content_key": KEY_A, "file_id": 1}, headers=headers)
    assert missing.status_code == 404 and missing.json()["detail"] == "会议不存在"


# ---------------------------------------------------------------------- 重算


def test_edit_events_and_version_changes_mark_dirty_but_minutes_do_not(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()

    def dirty():
        return w.db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id = 'm'")[
            "dirty"
        ]

    for event in ("segment_split", "segments_merged", "transcript_draft_saved"):
        before = dirty()
        w.db.add_event(event, meeting_id="m")
        assert dirty() == before + 1
    before = dirty()
    w.db.add_event("minutes_saved", meeting_id="m")
    assert dirty() == before
    w.db.create_transcript_version("m", "funasr", published=True)
    assert dirty() == before + 1


def test_leaving_the_project_drops_passages_and_notes_no_project(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    windows = w.db.query_one("SELECT COUNT(*) AS n FROM meeting_windows")["n"]
    w.db.execute("UPDATE meetings SET project_id = NULL WHERE id = 'm'")
    worker_for(w).run_round()
    assert passages(w.db) == []
    assert w.db.query_one("SELECT COUNT(*) AS n FROM meeting_windows")["n"] == windows  # 窗留着
    assert w.db.query_one(
        "SELECT note, dirty FROM meeting_related_scan WHERE meeting_id = 'm'"
    ) == {"note": "no_project", "dirty": 0}


def test_confirming_a_term_changes_the_signature(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    with w.db.autocommit() as connection:
        assert not related.is_due(connection, w.settings, "m")
    add_term(w.db, "小驼峰")
    with w.db.autocommit() as connection:
        assert related.is_due(connection, w.settings, "m")
        # 改门槛也让签名变
        assert related.project_sigs(connection, w.settings) != related.project_sigs(
            connection, settings(related_floor=0.7)
        )
    w.semantic.calls.clear()
    worker_for(w).run_round()
    assert w.semantic.calls == []  # 文字没变不重新编码


def test_rereading_a_chunk_cascades_its_passages(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    assert passages(w.db)
    w.db.execute("DELETE FROM material_chunks WHERE content_key = ?", (KEY_A,))
    assert passages(w.db) == []


def test_new_chunks_go_through_the_increment_not_a_full_compute(tmp_path):
    w = build(tmp_path, chunk_a=False)
    worker_for(w).run_round()
    assert passages(w.db) == []
    scanned = w.db.query_one("SELECT scanned_at FROM meeting_related_scan WHERE meeting_id = 'm'")[
        "scanned_at"
    ]
    add_content(w.db, KEY_A, ["。".join(TOPIC_A)])
    add_file(w.db, w.root, "需求/接口文档.docx", key=KEY_A, mtime=5)
    w.semantic.calls.clear()
    worker_for(w).run_round()
    assert w.semantic.calls == []
    assert (
        w.db.query_one("SELECT scanned_at FROM meeting_related_scan WHERE meeting_id = 'm'")[
            "scanned_at"
        ]
        == scanned
    )
    assert {row["content_key"] for row in passages(w.db)} == {KEY_A}
    assert [row["ident"] for row in related_rows(w.db)] == [f"m|{KEY_A}"]
    mark = json.loads(
        w.db.query_one("SELECT value FROM app_state WHERE key = 'related_chunk_mark'")["value"]
    )
    top = w.db.query_one("SELECT MAX(chunk_id) AS n FROM material_chunk_vectors")["n"]
    assert mark == {"model": MODEL, "id": top}


class _SliceSpy:
    """记下每次和片段矩阵相乘的窗有多少行（numpy 遇到 __array_ufunc__ = None 会交给 __rmatmul__）。"""

    __array_ufunc__ = None

    def __init__(self, matrix, seen):
        self.matrix, self.seen = matrix, seen

    @property
    def T(self):  # noqa: N802
        return _SliceSpy(self.matrix.T, self.seen)

    def __rmatmul__(self, other):
        self.seen.append(other.shape[0])
        return other @ self.matrix


def test_increment_hits_scores_in_slices_like_the_dense_formula():
    rng = np.random.default_rng(7)
    count, chunks = 50, 9
    vectors = rng.standard_normal((count, 16)).astype(np.float16)
    cached = related.ProjectWindows(
        ["a", "b", "c"],
        rng.integers(0, 3, count).astype(np.int32),
        np.arange(count, dtype=np.int64) * 45_000,
        rng.uniform(-1, 1, count).astype(np.float32),
        vectors,
    )
    matrix = rng.standard_normal((chunks, 16)).astype(np.float32)
    chunk_ids = np.arange(100, 100 + chunks, dtype=np.int64)
    marks = np.asarray([0, 104, 1 << 62], dtype=np.int64)[cached.meeting]
    dense = vectors.astype(np.float32) @ matrix.T
    expected = np.nonzero((dense >= cached.bars[:, None]) & (chunk_ids[None, :] > marks[:, None]))
    for rows in (1, 4, 7, 50, 4_096):
        seen: list[int] = []
        windows, picked, scores = related.increment_hits(
            cached, marks, _SliceSpy(matrix, seen), chunk_ids, rows
        )
        assert max(seen) <= rows and sum(seen) == count
        assert windows.tolist() == expected[0].tolist() and picked.tolist() == expected[1].tolist()
        assert np.allclose(scores, dense[expected], atol=1e-5)  # 不同切片的 BLAS 路径只差浮点末位


def test_increment_reads_each_project_windows_once_a_round_and_matches(tmp_path, monkeypatch):
    def world(path, sliced):
        w = build(path, chunk_a=False)
        for index in range(3):
            add_meeting(
                w.db,
                f"x{index}",
                ago=2 + index,
                project_id="p",
                segments=segments_for(TOPIC_A[index:] + FILLER),
            )
        worker = worker_for(w)
        for _ in range(3):
            worker.run_round()
        add_content(
            w.db,
            KEY_A,
            ["。".join(TOPIC_A), "。".join(TOPIC_A[::-1]), "。".join(TOPIC_A[2:] + TOPIC_A[:2])],
        )
        add_content(w.db, KEY_B, ["。".join(TOPIC_A[4:] + FILLER[:3])])
        add_file(w.db, w.root, "需求/接口文档.docx", key=KEY_A, mtime=5)
        add_file(w.db, w.root, "排期/附录.docx", key=KEY_B, mtime=3)
        loads: list[str] = []
        real = related.load_project_windows

        def spy(connection, model, meeting_ids):
            loads.append(",".join(sorted(meeting_ids)))
            return real(connection, model, meeting_ids)

        with monkeypatch.context() as patch:
            patch.setattr(related, "load_project_windows", spy)
            if sliced:
                patch.setattr(related, "INCREMENT_BLOCK", 1)  # 4 个新片段分 4 块
                patch.setattr(related, "WINDOW_SLICE", 3)
                patch.setattr(related, "WINDOW_FETCH", 2)
            w.semantic.calls.clear()
            worker.run_round()
        assert w.semantic.calls == []  # 走的是增量
        return loads, [dict(row) for row in passages(w.db)], sorted(_fingerprint(w.db))

    plain_loads, plain_passages, plain_links = world(tmp_path / "plain", False)
    loads, sliced_passages, sliced_links = world(tmp_path / "sliced", True)
    assert loads == ["m,x0,x1,x2"] and plain_loads == loads  # 一轮一个项目只读一次
    assert {row["content_key"] for row in plain_passages} >= {KEY_A}
    assert (sliced_passages, sliced_links) == (plain_passages, plain_links)


def _fingerprint(db):
    return [
        (row["ident"], row["status"], row["at_ms"], json.loads(row["evidence_json"])["words"])
        for row in related_rows(db)
    ]


def test_restore_from_backup_fills_back_to_the_same_links(tmp_path):
    extra = ["排期表按周更新，驻场服务的接口文档也跟着改", "接口文档第二部分讲驻场服务的计费和结算"]

    def world(path):
        w = build(path)
        add_content(w.db, KEY_B, ["。".join(TOPIC_A[3:] + extra)])
        add_file(w.db, w.root, "排期/接口文档附录.docx", key=KEY_B, mtime=3)
        return w

    fresh = world(tmp_path / "fresh")
    worker_for(fresh).run_round()
    expected = _fingerprint(fresh.db)
    assert len(expected) >= 1

    restored = world(tmp_path / "restored")
    worker_for(restored).run_round()
    # 模拟恢复备份：清空材料向量和三张派生表，删掉 related_chunk_mark
    db = restored.db
    kept = db.query_all(
        "SELECT chunk_id, vector, created_at FROM material_chunk_vectors ORDER BY chunk_id"
    )
    for table in (
        "material_chunk_vectors",
        "meeting_windows",
        "meeting_window_passages",
        "meeting_related_scan",
    ):
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM app_state WHERE key = 'related_chunk_mark'")
    # 向量按片段 id 从小到大分批补，中间穿插 H3
    for row in kept:
        db.execute(
            "INSERT INTO material_chunk_vectors(chunk_id, model, vector, created_at) VALUES (?, ?, ?, ?)",
            (row["chunk_id"], MODEL, row["vector"], row["created_at"]),
        )
        restored.vectors._built_at = -10_000.0
        restored.vectors.refresh()
        worker_for(restored).run_round()
    assert _fingerprint(db) == expected


def test_a_new_model_name_resets_the_mark(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    db = w.db
    db.execute(
        "UPDATE app_state SET value = ? WHERE key = 'related_chunk_mark'",
        (json.dumps({"model": "old", "id": 0}),),
    )
    worker_for(w).run_round()
    mark = json.loads(
        db.query_one("SELECT value FROM app_state WHERE key = 'related_chunk_mark'")["value"]
    )
    assert (
        mark["model"] == MODEL
        and mark["id"] == db.query_one("SELECT MAX(chunk_id) AS n FROM material_chunk_vectors")["n"]
    )


# ---------------------------------------------------------------------- 运行


def test_busy_stops_within_one_batch_and_writes_no_half_result(tmp_path):
    busy = {"value": False}
    w = build(tmp_path, meeting=False)
    # 一场长会：40 多个窗
    add_meeting(w.db, "m", ago=1, project_id="p", segments=segments_for((TOPIC_A + FILLER) * 7))
    w.semantic.on_encode = lambda: busy.update(value=True)
    worker = worker_for(w, busy=lambda: busy["value"])
    result = worker.run_round()
    assert result["phases"]["related"] == "busy"
    assert w.semantic.calls == [16]  # 编完一批就停
    assert passages(w.db) == [] and related_rows(w.db) == []
    scan = w.db.query_one("SELECT scanned_at FROM meeting_related_scan WHERE meeting_id = 'm'")
    assert scan is None or scan["scanned_at"] is None
    # 编好的那一批窗先存下，下一轮不用再编
    assert w.db.query_one("SELECT COUNT(*) AS n FROM meeting_windows")["n"] == 16


def test_fts_rebuild_skips_h3(tmp_path):
    w = build(tmp_path)
    w.db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES (?, '1', ?)", (REBUILD_KEY, utc_now())
    )
    result = worker_for(w).run_round()
    assert result["phases"]["related"] == "off" and passages(w.db) == []


def test_no_matrix_yet_means_waiting_and_no_scan_rows(tmp_path):
    w = build(tmp_path)
    w.vectors._matrix = None
    worker = worker_for(w)
    result = worker.run_round()
    assert result["phases"]["related"] == "waiting"
    assert w.db.query_all("SELECT * FROM meeting_related_scan WHERE scanned_at IS NOT NULL") == []
    assert worker.snapshot()["phases"]["related"] == "waiting"


def test_missing_model_stops_and_the_panel_says_so(tmp_path):
    w = build(tmp_path)
    w.semantic.missing = True
    worker = worker_for(w)
    assert worker.run_round()["phases"]["related"] == "off"
    with w.db.autocommit() as connection:
        payload = related_read.panel(
            connection, "m", worker=worker, settings=w.settings, local=True
        )
    assert payload["state"] == {
        "kind": "stopped",
        "text": "本地语义模型没装好，找不了相关材料",
        "action": None,
    }


def test_opened_meeting_goes_first_and_wakes_the_loop(tmp_path):
    w = build(tmp_path)
    add_meeting(w.db, "newer", ago=0, project_id="p", segments=segments_for(FILLER + TOPIC_A))
    worker = worker_for(w)
    order: list[str] = []
    real = worker.related.compute

    def spy(ctx, snap, row, deadline):
        order.append(row["id"])
        return real(ctx, snap, row, deadline)

    worker.related.compute = spy
    worker.prioritize("m")
    assert worker._wake.is_set()
    worker.run_round()
    assert order[0] == "m" and "m" not in worker.priorities()


def _db_state(path):
    """另开一条连接看库：PRAGMA data_version 在别的连接提交过写入时就变；再加上各表行数和两个 rev。"""
    connection = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
        revs = connection.execute(
            "SELECT key, value FROM app_state WHERE key IN ('related_rev', 'graph_rev') ORDER BY key"
        ).fetchall()
        return connection, counts, revs
    except Exception:
        connection.close()
        raise


def test_get_endpoints_do_not_write(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    assert passages(w.db) and related_rows(w.db)
    file_id = w.db.query_one("SELECT id FROM material_files WHERE content_key = ?", (KEY_A,))["id"]
    with w.db.transaction() as connection:
        related.mark_dirty(connection, meeting_id="m")  # 到期：GET 栏时会顺手 prioritize
    config = app_settings(tmp_path / "app").model_copy(
        update={
            "links_enabled": True,
            "semantic_enabled": True,
            "material_content_enabled": True,
            "semantic_model": MODEL,
            "database_path": w.db.path,
        }
    )
    app = create_app(config)
    client = TestClient(app)
    watcher, counts, revs = _db_state(w.db.path)
    try:
        version = watcher.execute("PRAGMA data_version").fetchone()[0]
        replies = [
            client.get("/api/meetings/m"),
            client.get("/api/meetings/m/related-materials"),
            client.get("/api/meetings/m/related-materials/rejected"),
            client.get("/api/graph/projects/p/related?window=all"),
            client.get(f"/api/materials/files/{file_id}/preview"),
            client.get(
                f"/api/materials/files/{file_id}/preview?parts=preview&passage_key={KEY_A}&passage_ordinal=0"
            ),
        ]
        assert [reply.status_code for reply in replies] == [200] * len(replies)
        assert watcher.execute("PRAGMA data_version").fetchone()[0] == version
    finally:
        watcher.close()
    after, counts_after, revs_after = _db_state(w.db.path)
    after.close()
    assert (counts_after, revs_after) == (counts, revs)
    assert app.state.links_worker.priorities() == ["m"]


# ---------------------------------------------------------------------- 接口


def _no_score_keys(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in ("score", "bar", "rank", "relation_id"), key
            _no_score_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            _no_score_keys(item)


def test_panel_shape(tmp_path):
    w = build(tmp_path)
    worker_for(w).run_round()
    with w.db.autocommit() as connection:
        payload = related_read.panel(connection, "m", worker=None, settings=w.settings, local=True)
    assert payload["state"] == {"kind": "ok", "text": None, "action": None}
    assert set(payload) == {"state", "files", "copies", "windows", "rejected"}
    file = payload["files"][KEY_A]
    assert (
        file["name"] == "接口文档.docx" and file["can_open"] is True and file["root_online"] is True
    )
    window = payload["windows"][0]
    assert window["end_ms"] - window["start_ms"] == 90_000 and len(window["items"]) <= 2
    item = window["items"][0]
    assert set(item) == {"content_key", "ordinal", "loc", "start_ms", "text", "words", "at_ms"}
    assert len(item["text"]) <= 121 and item["words"][0] == "驻场服务"
    _no_score_keys(payload)
    with w.db.autocommit() as connection:
        remote = related_read.panel(connection, "m", worker=None, settings=w.settings, local=False)
    assert remote["files"][KEY_A]["can_open"] is False


def test_panel_state_sentences(tmp_path):
    w = build(tmp_path)
    busy_worker = SimpleNamespace(snapshot=lambda: {"paused": "busy"})

    def state(meeting_id="m", worker=None, config=None):
        with w.db.autocommit() as connection:
            return related_read.panel(
                connection,
                meeting_id,
                worker=worker,
                settings=config or w.settings,
                local=True,
                unread=related_read.UnreadCounts(),
            )["state"]

    assert state(config=settings(links_enabled=False))["text"] == "关联整理关着，找不了相关材料"
    assert state(config=settings(semantic_enabled=False))["text"] == "语义索引关着，找不了相关材料"
    assert (
        state(config=settings(material_content_enabled=False))["text"]
        == "材料正文读取关着，找不了相关材料"
    )
    assert state() == {"kind": "waiting", "text": "正在找相关材料", "action": None}
    assert state(worker=busy_worker)["text"] == "会议在转写，转完再找相关材料"
    add_meeting(w.db, "loose", ago=1, segments=[(0, "随便说说")])
    assert state("loose")["text"] == "这场会没归项目，相关材料只在项目文件夹里找"
    add_meeting(w.db, "bare", ago=1, project_id="p")
    assert state("bare") == {"kind": "stopped", "text": "还没有逐字稿", "action": None}
    assert state("bare", worker=busy_worker)["text"] == "这场会还在转写，转完再找相关材料"
    add_meeting(w.db, "q-m", ago=1, project_id="q", segments=[(0, "随便说说")])
    assert state("q-m") == {
        "kind": "stopped",
        "text": "这个项目还没挂材料文件夹",
        "action": {"kind": "open_project", "label": "去项目页", "project_id": "q"},
    }
    worker_for(w).run_round()
    assert state()["kind"] == "ok" and state()["text"] is None
    # 没读完的材料：有条目时写 N
    add_file(w.db, w.root, "待读.docx", key=None)
    assert state()["text"] == "这个项目还有 1 份材料没读完，读完的先列在这里"
    w.db.execute("DELETE FROM material_files WHERE name = '待读.docx'")
    w.db.execute("UPDATE meeting_related_scan SET partial = 1")
    assert state()["text"] == "这个项目材料太多，较早的一部分没有比对"
    w.db.execute("UPDATE meeting_related_scan SET partial = 0")
    w.db.execute("DELETE FROM meeting_window_passages")
    assert state() == {"kind": "ok", "text": "这场会没找到相关材料", "action": None}
    with w.db.autocommit() as connection:
        assert (
            related_read.panel(connection, "nope", worker=None, settings=w.settings, local=True)
            is None
        )


def test_graph_related_edges_etag_and_per_node_cap(tmp_path):
    config = app_settings(tmp_path)
    app = create_app(config)
    db = Database(config.database_path)
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/r', ?)",
        (utc_now(),),
    )
    root = db.query_one("SELECT id FROM project_material_roots")["id"]
    files = []
    for index in range(5):
        key = f"q2:{index:032x}"
        add_content(
            db,
            key,
            [f"第{index}份材料的正文，驻场服务的安排写在这里，另外还有很多别的内容凑够字数"],
            vectors=False,
        )
        files.append((key, add_file(db, root, f"f{index}.docx", key=key)))
    add_meeting(db, "m", ago=1, project_id="p")
    add_meeting(db, "old", ago=60, project_id="p")
    rows = []
    for meeting_id in ("m", "old"):
        for key, file_id in files:
            rows.append(
                {
                    "kind": "related",
                    "project_id": "p",
                    "ident": f"{meeting_id}|{key}",
                    "status": "shown",
                    "origin": "vector",
                    "meeting_id": meeting_id,
                    "at_ms": 1000,
                    "content_key": key,
                    "root_id": root,
                    "rel_path": "x",
                    "file_id": file_id,
                    "quote": "驻场服务的事",
                    "score": 0.7,
                    "evidence": {
                        "words": ["驻场服务"],
                        "windows": 2,
                        "meeting": {"at_ms": 1000, "quote": "q"},
                        "material": {"content_key": key, "ordinal": 0, "loc": None},
                    },
                }
            )
    with db.transaction() as connection:
        relations.upsert_system(connection, rows, utc_now(), since=utc_now())
    client = TestClient(app)
    reply = client.get("/api/graph/projects/p/related?window=28d")
    assert reply.status_code == 200
    body = reply.json()
    assert len(body["edges"]) == 3 and {edge["meeting_id"] for edge in body["edges"]} == {"m"}
    edge = body["edges"][0]
    assert edge["id"] == f"e:rel:{edge['relation_id']}" and edge["rank"] == 1
    assert len(edge["passage"]["text"]) <= 60 and str(edge["file_id"]) in body["files"]
    everything = client.get("/api/graph/projects/p/related?window=all").json()
    assert len(everything["edges"]) == 6  # 两场会各 3 条
    per_file: dict[int, int] = {}
    for edge in everything["edges"]:
        per_file[edge["file_id"]] = per_file.get(edge["file_id"], 0) + 1
    assert max(per_file.values()) <= 3
    again = client.get(
        "/api/graph/projects/p/related?window=28d", headers={"If-None-Match": reply.headers["etag"]}
    )
    assert again.status_code == 304
    assert client.get("/api/graph/projects/nope/related").status_code == 404


def test_preview_passage_fresh_stale_missing_and_other_files(tmp_path):
    w = build(tmp_path)
    db = w.db
    file_id = db.query_one("SELECT id FROM material_files WHERE content_key = ?", (KEY_A,))["id"]
    add_content(db, KEY_B, ["另一份材料的正文，讲的是别的事情，和这份文件没有来历上的关系"])
    with db.autocommit() as connection:
        row = dict(
            connection.execute("SELECT * FROM material_files WHERE id = ?", (file_id,)).fetchone()
        )
        fresh = related_read.passage(connection, row, KEY_A, 0)
        assert fresh["stale"] is False and fresh["loc"] == "第 1 节"
        assert related_read.passage(connection, row, KEY_A, 9) is None
        assert related_read.passage(connection, row, KEY_B, 0) is None  # 别的文件的 key
    # 文件原地改过：旧 key 在流水里
    db.execute(
        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key, day, at)
           VALUES (?, ?, ?, '需求', 'changed', ?, '2026-09-27', ?)""",
        (w.root, file_id, row["rel_path"], KEY_B, utc_now()),
    )
    with db.autocommit() as connection:
        assert related_read.passage(connection, row, KEY_B, 0)["stale"] is True


def test_preview_endpoint_keys_and_mentioned_counts(tmp_path):
    config = app_settings(tmp_path)
    app = create_app(config)
    db = Database(config.database_path)
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
        (str(tmp_path), utc_now()),
    )
    root = db.query_one("SELECT id FROM project_material_roots")["id"]
    add_content(db, KEY_A, ["。".join(TOPIC_A)], vectors=False)
    file_id = add_file(db, root, "接口文档.docx", key=KEY_A)
    client = TestClient(app)
    brief = client.get(f"/api/materials/files/{file_id}/preview?parts=preview").json()
    assert set(brief) == {"file", "state", "preview"}
    located = client.get(
        f"/api/materials/files/{file_id}/preview?parts=preview&passage_key={KEY_A}&passage_ordinal=0"
    ).json()
    assert located["passage"]["stale"] is False
    full = client.get(f"/api/materials/files/{file_id}/preview").json()
    assert (
        full["file"]["can_open"] is False and full["related_meetings"] == []
    )  # 测试客户端不是本机
    assert (
        client.get(f"/api/materials/files/{file_id}/preview?passage_ordinal=-1").status_code == 422
    )
    assert (
        client.get(
            f"/api/materials/files/{file_id}/preview?passage_key=bad&passage_ordinal=0"
        ).status_code
        == 422
    )
    counts = client.get(f"/api/materials/mentioned-counts?file_ids={file_id},9999")
    assert counts.json() == {"counts": {str(file_id): 0}}
    too_many = ",".join(str(index) for index in range(1, 202))
    assert client.get(f"/api/materials/mentioned-counts?file_ids={too_many}").status_code == 422
    assert client.get("/api/materials/mentioned-counts?file_ids=1,x").status_code == 422
    # ［用本机应用打开］：测试客户端不是本机，403
    reply = client.post(
        f"/api/materials/files/{file_id}/open", json={}, headers=write_headers(client)
    )
    assert (
        reply.status_code == 403 and reply.json()["detail"] == "只能在声档所在的这台电脑上打开文件"
    )


# ---------------------------------------------------------------------- ［用本机应用打开］


def _open_world(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    root_path = tmp_path / "root"
    root_path.mkdir()
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
        (str(root_path), utc_now()),
    )
    root = db.query_one("SELECT id FROM project_material_roots")["id"]
    return db, root, root_path


def _open(db, file_id, **kwargs):
    calls: list[list[str]] = []
    kwargs.setdefault("state_of", lambda path: ROOT_ONLINE)
    with db.autocommit() as connection:
        related_read.open_material(
            connection,
            file_id,
            local=kwargs.pop("local", True),
            run=lambda command, **_k: calls.append(command),
            command_for=lambda path: ["open", path],
            **kwargs,
        )
    return calls


def test_open_file_refusals_and_realpath(tmp_path):
    db, root, root_path = _open_world(tmp_path)
    (root_path / "方案.pdf").write_text("pdf", encoding="utf-8")
    (root_path / "run.command").write_text("rm -rf /", encoding="utf-8")
    (root_path / "演示.key").mkdir()
    (root_path / "演示.key" / "Index.zip").write_text("x", encoding="utf-8")
    pdf = add_file(db, root, "方案.pdf")
    script = add_file(db, root, "run.command")
    keynote = add_file(db, root, "演示.key")
    assert _open(db, pdf) == [["open", os.path.realpath(root_path / "方案.pdf")]]
    assert _open(db, keynote) == [["open", os.path.realpath(root_path / "演示.key")]]
    errors = []
    for file_id, kwargs in (
        (pdf, {"local": False}),
        (script, {}),
        (pdf, {"state_of": lambda path: "offline"}),
        (9_999, {}),
    ):
        with pytest.raises(related_read.RelatedError) as error:
            _open(db, file_id, **kwargs)
        errors.append(error.value.status)
    assert errors == [403, 415, 503, 404]


def test_a_pdf_row_swapped_for_a_symlink_to_a_command_is_refused(tmp_path):
    db, root, root_path = _open_world(tmp_path)
    (root_path / "run.command").write_text("echo hi", encoding="utf-8")
    (root_path / "报价.pdf").symlink_to(root_path / "run.command")
    pdf = add_file(db, root, "报价.pdf")
    calls: list[list[str]] = []
    with db.autocommit() as connection, pytest.raises(related_read.RelatedError) as error:
        related_read.open_material(
            connection,
            pdf,
            local=True,
            state_of=lambda path: ROOT_ONLINE,
            run=lambda command, **_k: calls.append(command),
            command_for=lambda path: ["open", path],
        )
    assert (
        error.value.status == 415
        and str(error.value) == "这种文件不在声档里直接打开，可以在访达中显示"
    )
    assert calls == []


# ---------------------------------------------------------------------- 命令行（开发工具）


def test_cli_links_related_three_ways(tmp_path, monkeypatch, capsys):
    from meeting_workbench import cli

    w = build(tmp_path)
    worker_for(w).run_round()
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(tmp_path / "workbench.sqlite3"))
    monkeypatch.setenv("MEETING_WORKBENCH_SEMANTIC_MODEL", MODEL)
    monkeypatch.setattr(cli, "_server_json", lambda *_args, **_kwargs: None)
    assert cli.main(["links", "related", "--meeting", "m", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    first = payload["windows"][0]
    assert first["bar"] == pytest.approx(0.64) and first["candidates"][0]["words"] == [
        "驻场服务",
        "接口文档",
    ]
    reasons = {item["reason"] for window in payload["windows"] for item in window["candidates"]}
    assert None in reasons and reasons & {"below_bar", "quality", "no_word"}
    assert cli.main(["links", "related", "--meeting", "m", "--at", "0:30"]) == 0
    assert "bar 0.640" in capsys.readouterr().out
    assert cli.main(["links", "related", "--project", "p", "--stats", "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["windows"] >= 2 and stats["links"] == {"shown": 1}
    dirty = w.db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id = 'm'")["dirty"]
    assert cli.main(["links", "related", "--rebuild", "--meeting", "m"]) == 0
    assert (
        w.db.query_one("SELECT dirty FROM meeting_related_scan WHERE meeting_id = 'm'")["dirty"]
        == dirty + 1
    )
    assert passages(w.db)  # 不当场算，也不删
