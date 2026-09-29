"""第三期 3f：材料进搜索、按范围的「意思相近」、材料向量循环和内存矩阵、备份清全文表和恢复后补建。"""

import json
import shutil
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_workbench import cli
from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.material_content import MaterialContent
from meeting_workbench.material_fts import (
    REBUILD_KEY,
    integrity_ok,
    rebuild_mark,
    rebuild_pending,
    run_rebuild,
)
from meeting_workbench.material_media import MaterialMedia
from meeting_workbench.material_search import CHUNK_FETCH, material_search
from meeting_workbench.material_vectors import MaterialVectors
from meeting_workbench.materials import ROOT_ONLINE, ROOT_VOLUME_OFFLINE
from meeting_workbench.semantic import SemanticIndex

from .test_search import add_meeting, add_project, make_app

NS = 1_000_000_000


# ---------------------------------------------------------------------- 造数据


def add_root(db, project_id, path):
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, str(path), utc_now()),
    )
    return db.query_one("SELECT id FROM project_material_roots WHERE path = ?", (str(path),))["id"]


def add_file(db, root_id, rel_path, *, key=None, mtime=1, zone="normal", error=None, gone=False):
    name = rel_path.rsplit("/", 1)[-1]
    stem, _, ext = name.rpartition(".")
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone,
                                      seen_at, gone_at, content_key, content_error)
           VALUES (?, ?, ?, ?, ?, ?, ?, 10, ?, ?, ?, ?, ?, ?)""",
        (
            root_id,
            rel_path,
            rel_path.rsplit("/", 1)[0] if "/" in rel_path else "",
            name,
            stem or name,
            (stem or name).lower(),
            ext.lower() if stem else "",
            mtime * NS,
            zone,
            utc_now(),
            utc_now() if gone else None,
            key,
            error,
        ),
    )
    return db.query_one(
        "SELECT id FROM material_files WHERE root_id = ? AND rel_path = ?", (root_id, rel_path)
    )["id"]


def add_content(db, key, chunks=(), *, layer="text", state="done", reason=None):
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, reason, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (key, layer, state, reason, utc_now(), utc_now()),
    )
    for ordinal, chunk in enumerate(chunks):
        loc, start_ms, text = chunk if isinstance(chunk, tuple) else (None, None, chunk)
        db.execute(
            "INSERT INTO material_chunks(content_key, ordinal, loc, start_ms, text) VALUES (?, ?, ?, ?, ?)",
            (key, ordinal, loc, start_ms, text),
        )


def chunk_ids(db, key):
    return [
        row["id"]
        for row in db.query_all(
            "SELECT id FROM material_chunks WHERE content_key = ? ORDER BY ordinal", (key,)
        )
    ]


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    add_project(db, "p", "云图AI")
    add_project(db, "q", "别的项目")
    root = add_root(db, "p", tmp_path / "云图AI")
    other = add_root(db, "q", tmp_path / "别的")
    return SimpleNamespace(db=db, root=root, other=other, tmp=tmp_path)


def search(db, words, scope=None, **kwargs):
    return material_search(
        db, words, scope=scope, state_of=kwargs.pop("state_of", lambda path: ROOT_ONLINE), **kwargs
    )


# ---------------------------------------------------------------------- 原词命中


def test_body_hits_one_row_per_content_with_copies_and_two_quotes(world):
    db = world.db
    add_content(
        db,
        "k-plan",
        [
            ("第 1 页", None, "报价单下周发给客户"),
            ("第 2 页", None, "开场白"),
            ("第 3 页", None, "报价单要盖章"),
            ("第 4 页", None, "报价单附件"),
        ],
        layer="pdf",
    )
    add_file(db, world.root, "方案/方案.pdf", key="k-plan", mtime=5)
    newest = add_file(db, world.root, "备份/方案.pdf", key="k-plan", mtime=9)
    add_content(db, "k-audio", [(None, 92_000, "报价单的事明天说")], layer="media")
    add_file(db, world.root, "访谈.m4a", key="k-audio", mtime=7)

    [plan, audio] = search(db, ["报价单"])["items"]

    assert plan["file_id"] == newest and plan["copies"] == 1 and plan["name"] == "方案.pdf"
    assert [(hit["kind"], hit["loc"]) for hit in plan["hits"]] == [
        ("pdf", "第 1 页"),
        ("pdf", "第 3 页"),
    ]
    assert plan["hits"][0]["matched"] == "报价单" and plan["more_hits"] == 1
    assert plan["project_name"] == "云图AI" and plan["project_color"] == "#123456"
    assert plan["path"].endswith("云图AI/备份/方案.pdf") and plan["folder_path"].endswith(
        "云图AI/备份"
    )
    assert audio["hits"][0]["kind"] == "media" and audio["hits"][0]["start_ms"] == 92_000
    assert audio["playable"] is True and plan["playable"] is False
    assert plan["name_hit"] is False and plan["state_text"] is None


def test_file_names_match_and_cards_and_gone_files_do_not(world):
    db = world.db
    add_file(db, world.root, "报价单.zip", mtime=3)
    add_file(db, world.root, "声档会议记录/0926 报价单.md", zone="cards", mtime=4)
    add_file(db, world.root, "旧报价单.docx", mtime=5, gone=True)
    add_content(db, "k-quote", ["和报价无关的正文"])
    add_file(db, world.root, "报价单-v2.docx", key="k-quote", mtime=6)
    add_file(db, world.root, "副本.docx", key="k-quote", mtime=8)

    rows = search(db, ["报价单"])["items"]

    assert [row["name"] for row in rows] == ["报价单-v2.docx", "报价单.zip"]
    # 同一份内容里文件名命中的那个当代表，另一个算副本
    assert rows[0]["name_hit"] is True and rows[0]["copies"] == 1 and rows[0]["hits"] == []


def test_two_character_words_scan_the_project_but_only_names_across_projects(world):
    db = world.db
    add_content(db, "k-a", ["随访安排在周三"])
    add_file(db, world.root, "纪要.txt", key="k-a", mtime=3)
    add_file(db, world.root, "随访表.xlsx", mtime=2)

    scoped = search(db, ["随访"], scope="p")["items"]
    everywhere = search(db, ["随访"])["items"]

    assert [row["name"] for row in scoped] == ["纪要.txt", "随访表.xlsx"]
    assert scoped[0]["hits"][0]["text"] == "随访安排在周三"
    assert [row["name"] for row in everywhere] == ["随访表.xlsx"]


def test_scope_limits_to_the_project_roots_and_unattributed_meetings_get_no_materials(world):
    db = world.db
    add_content(db, "k-a", ["云图的报价单"])
    add_content(db, "k-b", ["别的报价单"])
    add_file(db, world.root, "a.txt", key="k-a")
    add_file(db, world.other, "b.txt", key="k-b")

    assert [row["name"] for row in search(db, ["报价单"], scope="p")["items"]] == ["a.txt"]
    assert {row["name"] for row in search(db, ["报价单"])["items"]} == {"a.txt", "b.txt"}
    assert search(db, ["报价单"], scope="none")["items"] == []


def test_newest_first_and_at_most_the_limit(world):
    db = world.db
    for index in range(25):
        add_content(db, f"k-{index}", [f"第 {index} 份报价单"])
        add_file(db, world.root, f"{index:02d}.txt", key=f"k-{index}", mtime=index + 1)

    rows = search(db, ["报价单"])["items"]

    assert len(rows) == 20
    assert [row["name"] for row in rows[:3]] == ["24.txt", "23.txt", "22.txt"]


def test_state_text_for_unreadable_offline_and_mentions(world):
    db = world.db
    add_content(db, "k-locked", [], layer="pdf", state="unreadable", reason="password")
    locked = add_file(db, world.root, "报价单-加密.pdf", key="k-locked", mtime=3)
    add_file(db, world.root, "报价单-无权限.docx", error="permission", mtime=2)
    add_content(db, "k-off", ["报价单在盘上"])
    add_file(db, world.other, "盘上.txt", key="k-off", mtime=1)
    add_meeting(db, "m1", date="2026-09-20", project_id="p")
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, needle, file_id, count, first_ms,
                                             anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('m1', 'p', 'x', 'x', ?, 1, 0, '[]', 0, 'transcript', 'active', 0, ?)""",
        (locked, utc_now()),
    )
    offline_path = str(world.tmp / "别的")

    rows = search(
        db,
        ["报价单"],
        state_of=lambda path: ROOT_VOLUME_OFFLINE if path == offline_path else ROOT_ONLINE,
    )["items"]
    by_name = {row["name"]: row for row in rows}

    assert by_name["报价单-加密.pdf"]["state_text"] == "读不了：要密码"
    assert by_name["报价单-加密.pdf"]["mentioned_meetings"] == 1
    assert by_name["报价单-无权限.docx"]["state_text"] == "读不了：没有权限"
    assert (
        by_name["盘上.txt"]["state_text"] == "资料盘未连接"
        and by_name["盘上.txt"]["root_online"] is False
    )


def test_budget_runs_out_and_returns_what_was_found_as_partial(world, monkeypatch):
    from meeting_workbench import material_search as module

    monkeypatch.setattr(module, "PROGRESS_OPS", 1)
    db = world.db
    add_content(db, "k-a", ["报价单"])
    add_file(db, world.root, "报价单.txt", key="k-a")
    ticks = iter([0.0] + [10.0] * 10_000)

    result = search(db, ["报价单"], clock=lambda: next(ticks))

    assert result["partial"] is True


def test_hitting_the_fetch_cap_counts_as_partial(world, monkeypatch):
    db = world.db
    from meeting_workbench import material_search as module

    monkeypatch.setattr(module, "CHUNK_FETCH", 2)
    for index in range(3):
        add_content(db, f"k-{index}", ["报价单"])
        add_file(db, world.root, f"{index}.txt", key=f"k-{index}")
    assert search(db, ["报价单"])["partial"] is True
    assert CHUNK_FETCH > 2


# ---------------------------------------------------------------------- 接口


def test_search_query_is_capped_at_200_characters_with_a_chinese_message(tmp_path):
    app, _db = make_app(tmp_path)
    client = TestClient(app)
    too_long = client.get("/api/search", params={"q": "报" * 201})
    assert too_long.status_code == 422 and too_long.json()["detail"] == "搜索词最多 200 个字"
    assert client.get("/api/search", params={"q": "报" * 200}).status_code == 200


def test_search_endpoint_returns_materials_and_state(tmp_path):
    app, db = make_app(tmp_path)
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图AI")
    add_content(db, "k-a", ["报价单下周发"])
    add_file(db, root, "a.txt", key="k-a")
    add_meeting(db, "vm-1", date="2026-09-26", segments=["开场"])
    client = TestClient(app)

    body = client.get("/api/search", params={"q": "报价单"}).json()
    assert [row["name"] for row in body["materials"]] == ["a.txt"]
    assert body["material_state"] == {"pending": 0, "rebuilding": False, "partial": False}
    assert body["material_similar"] == []
    scoped = client.get("/api/search", params={"q": "报价单", "project_id": "p"}).json()
    assert [row["name"] for row in scoped["materials"]] == ["a.txt"]
    assert (
        client.get("/api/search", params={"q": "报价单", "project_id": "none"}).json()["materials"]
        == []
    )

    db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES (?, '{}', ?)",
        (REBUILD_KEY, utc_now()),
    )
    assert (
        client.get("/api/search", params={"q": "报价单"}).json()["material_state"]["rebuilding"]
        is True
    )


# ---------------------------------------------------------------------- 意思相近


def put_vector(db, chunk_id, values, model):
    vector = np.asarray(values, dtype=np.float32)
    vector = vector / np.linalg.norm(vector)
    db.execute(
        "INSERT INTO material_chunk_vectors(chunk_id, model, vector, created_at) VALUES (?, ?, ?, ?)",
        (chunk_id, model, vector.astype(np.float16).tobytes(), utc_now()),
    )


def test_meeting_similar_filters_by_project_before_taking_the_top_60(tmp_path):
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "db.sqlite3", semantic_enabled=True
    )
    db = Database(settings.database_path)
    db.initialize()
    add_project(db, "p", "小项目")
    add_meeting(db, "vm-small", date="2026-09-01", project_id="p", segments=["小项目的话"])
    add_meeting(
        db, "vm-big", date="2026-09-02", segments=[f"别的话 {index}" for index in range(70)]
    )
    for row in db.query_all("SELECT id, meeting_id FROM segments"):
        vector = np.array(
            [0.8, 0.6, 0, 0] if row["meeting_id"] == "vm-small" else [1, 0, 0, 0], dtype=np.float32
        )
        db.execute(
            "INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at) VALUES (?, ?, 4, ?, ?)",
            (row["id"], settings.semantic_model, vector.tobytes(), utc_now()),
        )
    semantic = SemanticIndex(db, settings, busy_check=lambda: False)
    query = np.array([1, 0, 0, 0], dtype=np.float32)

    assert [row["segment_id"] for row in semantic.search_vector(query, scope="p", limit=60)] == [
        "vm-small-s0"
    ]
    assert "vm-small-s0" not in [
        row["segment_id"] for row in semantic.search_vector(query, limit=60)
    ]
    assert all(
        row["project_id"] is None for row in semantic.search_vector(query, scope="none", limit=60)
    )


def test_material_similar_in_scope_above_threshold_one_row_per_content(tmp_path, monkeypatch):
    app, db = make_app(tmp_path, semantic_enabled=True)
    model = app.state.settings.semantic_model
    add_project(db, "p", "云图AI")
    add_project(db, "q", "别的")
    root = add_root(db, "p", tmp_path / "云图AI")
    other = add_root(db, "q", tmp_path / "别的")
    add_content(db, "k-close", ["交付时间往后挪", "交付节奏"])
    add_content(db, "k-far", ["午饭吃什么"])
    add_content(db, "k-listed", ["报价单原文"])
    add_content(db, "k-other", ["别的项目的交付"])
    add_file(db, root, "近.txt", key="k-close")
    add_file(db, root, "远.txt", key="k-far")
    add_file(db, root, "报价单.txt", key="k-listed")
    add_file(db, other, "他.txt", key="k-other")
    for chunk in chunk_ids(db, "k-close"):
        put_vector(db, chunk, [1, 0.1, 0, 0], model)
    put_vector(db, chunk_ids(db, "k-far")[0], [0, 0, 1, 0], model)
    put_vector(db, chunk_ids(db, "k-listed")[0], [1, 0, 0, 0], model)
    put_vector(db, chunk_ids(db, "k-other")[0], [1, 0, 0, 0], model)
    semantic = app.state.semantic
    monkeypatch.setattr(semantic, "busy_check", lambda: False)
    monkeypatch.setattr(
        semantic, "encode_query", lambda query: np.array([1, 0, 0, 0], dtype=np.float32)
    )
    monkeypatch.setattr(semantic, "search_vector", lambda vector, *, scope=None, limit=20: [])
    client = TestClient(app)

    body = client.get("/api/search", params={"q": "报价单", "project_id": "p"}).json()

    assert [row["name"] for row in body["materials"]] == ["报价单.txt"]
    [close] = body["material_similar"]
    assert (
        close["name"] == "近.txt"
        and close["score"] > 0.9
        and close["hits"][0]["text"] == "交付时间往后挪"
    )
    everywhere = client.get("/api/search", params={"q": "报价单"}).json()
    assert {row["name"] for row in everywhere["material_similar"]} == {"近.txt", "他.txt"}
    assert (
        client.get("/api/search", params={"q": "报价单", "project_id": "none"}).json()[
            "material_similar"
        ]
        == []
    )

    monkeypatch.setattr(semantic, "busy_check", lambda: True)
    busy = client.get("/api/search", params={"q": "报价单"}).json()
    assert busy["similar"] == [] and busy["material_similar"] == []
    assert busy["semantic_unavailable"] == "正在转写，意思相近的结果等转写完再搜"


def test_deleted_chunks_are_dropped_and_forgotten(tmp_path, monkeypatch):
    app, db = make_app(tmp_path, semantic_enabled=True)
    model = app.state.settings.semantic_model
    add_project(db, "p", "云图AI")
    root = add_root(db, "p", tmp_path / "云图AI")
    add_content(db, "k-a", ["交付时间"])
    add_file(db, root, "a.txt", key="k-a")
    [chunk] = chunk_ids(db, "k-a")
    put_vector(db, chunk, [1, 0, 0, 0], model)
    vectors = app.state.material_vectors
    vectors.refresh()
    # 重读：旧片段删掉（向量跟着级联删），新片段还没算向量
    db.execute("DELETE FROM material_chunks WHERE id = ?", (chunk,))
    add_content(db, "k-b", ["交付节奏"])
    add_file(db, root, "b.txt", key="k-b")
    semantic = app.state.semantic
    monkeypatch.setattr(semantic, "busy_check", lambda: False)
    monkeypatch.setattr(
        semantic, "encode_query", lambda query: np.array([1, 0, 0, 0], dtype=np.float32)
    )
    monkeypatch.setattr(semantic, "search_vector", lambda vector, *, scope=None, limit=20: [])
    monkeypatch.setattr(vectors, "refresh", lambda: vectors._matrix)

    body = TestClient(app).get("/api/search", params={"q": "不相干"}).json()

    assert body["material_similar"] == []
    assert vectors._matrix.invalid == 1


# ---------------------------------------------------------------------- 向量循环


class FakeEncoder:
    def __init__(self, on_encode=None):
        self.calls = []
        self.on_encode = on_encode

    def encode_texts(self, texts, *, background=False):
        self.calls.append(len(texts))
        self.background = background
        if self.on_encode is not None:
            self.on_encode()
        return np.tile(np.array([1, 0, 0, 0], dtype=np.float32), (len(texts), 1))


def vector_settings(enabled=True):
    return SimpleNamespace(semantic_enabled=enabled, semantic_model="bge-test")


def test_embed_round_goes_in_batches_of_64_and_stores_float16(world):
    db = world.db
    add_content(db, "k-a", [f"第 {index} 段" for index in range(130)])
    encoder = FakeEncoder()
    vectors = MaterialVectors(db, vector_settings(), encoder)

    stats = vectors.embed_round()

    assert encoder.calls == [64, 64, 2] and stats == {"embedded": 130, "ended": "done"}
    row = db.query_one("SELECT vector FROM material_chunk_vectors LIMIT 1")
    assert len(row["vector"]) == 4 * 2
    assert vectors.embed_round() == {"embedded": 0, "ended": "done"} and encoder.calls == [
        64,
        64,
        2,
    ]


def test_embed_round_yields_between_batches_and_skips_when_disabled(world):
    db = world.db
    add_content(db, "k-a", [f"第 {index} 段" for index in range(100)])
    busy = {"value": False}
    encoder = FakeEncoder(on_encode=lambda: busy.update(value=True))
    vectors = MaterialVectors(db, vector_settings(), encoder, busy_check=lambda: busy["value"])

    assert vectors.embed_round() == {"embedded": 64, "ended": "busy"}
    assert MaterialVectors(db, vector_settings(False), encoder).embed_round()["ended"] == "disabled"


def test_chunk_deleted_while_encoding_gets_no_vector(world):
    db = world.db
    add_content(db, "k-a", ["一段", "两段"])
    [first, second] = chunk_ids(db, "k-a")
    encoder = FakeEncoder(
        on_encode=lambda: db.execute("DELETE FROM material_chunks WHERE id = ?", (first,))
    )
    MaterialVectors(db, vector_settings(), encoder).embed_round()

    stored = [
        row["chunk_id"] for row in db.query_all("SELECT chunk_id FROM material_chunk_vectors")
    ]
    assert stored == [second]


# ---------------------------------------------------------------------- 内存矩阵


def matrix_world(world, count, *, model="bge-test"):
    db = world.db
    for index in range(count):
        add_content(db, f"k-{index}", [f"段 {index}"])
        add_file(db, world.root, f"{index}.txt", key=f"k-{index}", mtime=index + 1)
        put_vector(db, chunk_ids(db, f"k-{index}")[0], [1, index / count, 0, 0], model)
    return db


def test_matrix_adds_new_chunks_incrementally_and_rebuilds_after_an_hour(world):
    db = matrix_world(world, 3)
    clock = {"now": 0.0}
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), clock=lambda: clock["now"])
    first = vectors.refresh()
    assert first.n == 3
    add_content(db, "k-new", ["新段"])
    put_vector(db, chunk_ids(db, "k-new")[0], [0, 1, 0, 0], "bge-test")
    assert vectors.refresh() is first and first.n == 4
    clock["now"] = 3600.0
    assert vectors.refresh() is not first


def test_forgotten_chunks_are_skipped_and_many_trigger_a_rebuild(world):
    db = matrix_world(world, 10)
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), clock=lambda: 0.0)
    query = np.array([1, 0, 0, 0], dtype=np.float32)
    top = vectors.search(query, allowed=None, fetch=3)
    vectors.forget([top[0][0]])
    assert top[0][0] not in [
        chunk_id for chunk_id, _ in vectors.search(query, allowed=None, fetch=3)
    ]
    before = vectors._matrix
    vectors.forget([chunk_id for chunk_id, _ in vectors.search(query, allowed=None, fetch=3)])
    vectors.search(query, allowed=None, fetch=3)
    assert vectors._matrix is not before and vectors._matrix.invalid == 0


def test_scope_filter_and_block_scoring_agree_with_one_block(world):
    db = matrix_world(world, 40)
    query = np.array([0.2, 1, 0, 0], dtype=np.float32)
    whole = MaterialVectors(db, vector_settings(), FakeEncoder()).search(
        query, allowed=None, fetch=5
    )
    blocks = MaterialVectors(db, vector_settings(), FakeEncoder(), block_bytes=1).search(
        query, allowed=None, fetch=5
    )
    assert [chunk_id for chunk_id, _ in whole] == [chunk_id for chunk_id, _ in blocks]
    allowed = {"k-3", "k-4"}
    scoped = MaterialVectors(db, vector_settings(), FakeEncoder()).search(
        query, allowed=allowed, fetch=5
    )
    assert {chunk_ids(db, key)[0] for key in allowed} == {chunk_id for chunk_id, _ in scoped}


def test_matrix_cap_keeps_the_most_recently_modified_files(world, caplog):
    db = matrix_world(world, 10)
    vectors = MaterialVectors(db, vector_settings(), FakeEncoder(), max_rows=4)
    matrix = vectors.refresh()
    kept = {chunk_id for chunk_id in matrix.ids[: matrix.n].tolist()}
    assert kept == {chunk_ids(db, f"k-{index}")[0] for index in (6, 7, 8, 9)}
    assert "少放了 6 个" in caplog.text


# ---------------------------------------------------------------------- 备份和恢复后补全文表


def backup_world(tmp_path, chunks=200):
    archive = tmp_path / "archive"
    archive.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=archive,
    )
    db = Database(settings.database_path)
    db.initialize()
    add_content(
        db,
        "k-big",
        [
            f"第 {index} 段：报价单、交付时间、验收标准都在这一段里写着" * 3
            for index in range(chunks)
        ],
    )
    return settings, db


def test_backup_empties_the_fts_table_and_marks_it_for_rebuild(tmp_path):
    settings, db = backup_world(tmp_path)

    result = BackupManager(db, settings).create()

    with sqlite3.connect(result.local_path) as copy:
        assert copy.execute("SELECT COUNT(*) FROM material_chunks").fetchone()[0] == 200
        assert (
            copy.execute(
                "SELECT COUNT(*) FROM material_chunks_fts WHERE material_chunks_fts MATCH '\"报价单\"'"
            ).fetchone()[0]
            == 0
        )
        value = copy.execute(
            "SELECT value FROM app_state WHERE key = ?", (REBUILD_KEY,)
        ).fetchone()[0]
        assert json.loads(value) == {"end": None, "done": 0}
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    receipt = json.loads((settings.backup_dir / "last-backup.json").read_text())
    assert "material_chunks_fts" in receipt["derived_tables"]
    assert not rebuild_pending(db)


def test_backup_without_the_fts_table_gets_no_mark(tmp_path):
    settings, db = backup_world(tmp_path, chunks=2)
    for statement in (
        "DROP TRIGGER material_chunks_fts_insert",
        "DROP TRIGGER material_chunks_fts_delete",
        "DROP TRIGGER material_chunks_fts_update",
        "DROP TABLE material_chunks_fts",
    ):
        db.execute(statement)

    result = BackupManager(db, settings).create()

    with sqlite3.connect(result.local_path) as copy:
        assert (
            copy.execute("SELECT 1 FROM app_state WHERE key = ?", (REBUILD_KEY,)).fetchone() is None
        )
    receipt = json.loads((settings.backup_dir / "last-backup.json").read_text())
    assert receipt["derived_tables"] == [
        "embeddings",
        "material_chunk_vectors",
        "meeting_windows",
        "meeting_window_passages",
        "meeting_related_scan",
    ]


def test_backup_is_smaller_than_one_that_keeps_the_fts_table(tmp_path):
    settings, db = backup_world(tmp_path, chunks=2000)
    result = BackupManager(db, settings).create()
    kept = tmp_path / "kept.sqlite3"
    source = db.connect()
    target = sqlite3.connect(kept)
    try:
        source.backup(target)
        target.execute("DELETE FROM embeddings")
        target.execute("DELETE FROM material_chunk_vectors")
        target.commit()
        target.execute("VACUUM")
    finally:
        target.close()
        source.close()
    assert result.local_path.stat().st_size < kept.stat().st_size


def restored(tmp_path, result):
    target = tmp_path / "restored.sqlite3"
    shutil.copyfile(result.local_path, target)
    db = Database(target)
    db.initialize()
    return db


def test_restore_rebuilds_in_batches_resumes_after_restart_and_checks_integrity(tmp_path):
    settings, db = backup_world(tmp_path, chunks=12)
    db = restored(tmp_path, BackupManager(db, settings).create())
    calls = {"n": 0}

    def stop_after_one():
        calls["n"] += 1
        return calls["n"] > 1

    first = run_rebuild(db, batch=5, should_stop=stop_after_one)
    with db.transaction() as connection:
        mark = rebuild_mark(connection)
    assert first["state"] == "stopped" and mark["done"] == 5 and mark["end"] == 12
    assert rebuild_pending(db)

    second = run_rebuild(db, batch=5)

    assert second == {"state": "done", "batches": 2, "rebuilt": False}
    assert not rebuild_pending(db)
    with db.transaction() as connection:
        assert integrity_ok(connection)
    assert (
        len(material_search(db, ["报价单"], state_of=lambda path: ROOT_ONLINE)["items"]) == 0
    )  # 没有文件行
    hits = db.query_all(
        "SELECT rowid FROM material_chunks_fts WHERE material_chunks_fts MATCH '\"报价单\"'"
    )
    assert len(hits) == 12


def test_rebuild_falls_back_to_a_full_rebuild_when_the_check_fails(tmp_path):
    settings, db = backup_world(tmp_path, chunks=4)
    db = restored(tmp_path, BackupManager(db, settings).create())
    # 终点记错了一个：最后一段没补进去，核对不通过
    db.execute(
        "UPDATE app_state SET value = ? WHERE key = ?",
        (json.dumps({"end": 3, "done": 0}), REBUILD_KEY),
    )

    stats = run_rebuild(db, batch=10)

    assert stats["state"] == "done" and stats["rebuilt"] is True
    with db.transaction() as connection:
        assert integrity_ok(connection)


def test_material_loops_leave_chunks_alone_until_the_rebuild_finishes(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    settings = SimpleNamespace(
        data_dir=tmp_path / "data", archive_root=tmp_path, staging_root=tmp_path
    )
    extracted = []

    class Extractor:
        def extract(self, *args, **kwargs):
            extracted.append(args)
            raise AssertionError("不该读")

    content = MaterialContent(
        db,
        settings,
        state_of=lambda path: ROOT_ONLINE,
        extractors={"text": Extractor()},
        fts_rebuilding=lambda: True,
    )
    orphan_calls = []
    content.orphan_pass = lambda **kwargs: orphan_calls.append(kwargs)
    add_project(db, "p", "云图AI")
    add_root(db, "p", tmp_path / "云图AI")

    stats = content.run_round()

    assert stats["ended"] == "fts_rebuild" and extracted == [] and orphan_calls == []
    media = MaterialMedia(db, settings, content, tools=lambda: None)
    assert media.run_once()["ended"] == "fts_rebuild"


def test_doctor_reports_the_material_fts_check(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
    )
    db = Database(settings.database_path)
    db.initialize()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    cli.main(["doctor"])
    assert json.loads(capsys.readouterr().out)["material_fts"] == "ok"
    db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES (?, '{}', ?)",
        (REBUILD_KEY, utc_now()),
    )
    cli.main(["doctor"])
    assert json.loads(capsys.readouterr().out)["material_fts"] == "rebuilding"


def test_search_is_fast_enough_for_the_budget(world):
    db = world.db
    for index in range(200):
        add_content(db, f"k-{index}", [f"第 {index} 份材料，写着报价单和交付" for _ in range(5)])
        add_file(db, world.root, f"{index}.txt", key=f"k-{index}", mtime=index)
    started = time.monotonic()
    result = search(db, ["报价单"])
    assert (
        time.monotonic() - started < 1.5
        and len(result["items"]) == 20
        and result["partial"] is False
    )
