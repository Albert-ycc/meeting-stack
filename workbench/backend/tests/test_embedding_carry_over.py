"""写出新版本的段落时，文字没变的段沿用向量，语义索引只补真正变了的那几段。

界面上拆段、合段是本地操作，点「保存草稿」才写入：每次保存都是新版本、新段落 id。以前新版本的
段落一条向量都没有，改一个字保存，语义索引要把整场会（几千段）重新编码一遍。向量只由文字决定，
所以按文字对：新版本里文字和源版本某一段完全一样的段沿用它的向量，变了的、新增的留给语义索引补。
"""

import hashlib
from collections import Counter

import numpy as np

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter
from meeting_workbench.semantic import SemanticIndex
from meeting_workbench.service import MeetingService

from .helpers import seed_editable_meeting
from .test_importer import write_meeting

MEETING = "vm-20260102-101500"
MODEL = Settings().semantic_model  # 检索和语义索引用的模型名，「缺多少」的查询口径按它数


def _vector(text: str) -> bytes:
    """测试用的向量：由文字算出来，所以挂在哪段上都能验证它是不是这段自己文字的向量。"""
    return hashlib.sha256(text.encode("utf-8")).digest()


class CountingEmbedder:
    """记下编码过哪些文字，返回由文字决定的向量。"""

    def __init__(self):
        self.encoded: list[str] = []

    def encode(self, texts, **_kwargs):
        self.encoded.extend(texts)
        return np.asarray(
            [[byte + 1 for byte in hashlib.sha256(text.encode()).digest()[:8]] for text in texts],
            dtype=np.float32,
        )


def _item(index: int, text: str | None = None) -> dict:
    return {
        "id": f"seg-{index}",
        "start_ms": index * 1000,
        "end_ms": index * 1000 + 900,
        "speaker_label": "SPEAKER_00",
        "text": text if text is not None else f"第 {index} 句的正文",
    }


def _world(tmp_path, count):
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "workbench.sqlite3", semantic_enabled=True
    )
    db = Database(settings.database_path)
    db.initialize()
    archive = tmp_path / "archive"
    seed_editable_meeting(db, archive)
    version = db.query_one(
        "SELECT current_transcript_version_id AS v FROM meetings WHERE id=?", (MEETING,)
    )["v"]
    db.replace_segments(version, MEETING, [_item(i) for i in range(count)])
    embedder = CountingEmbedder()
    index = SemanticIndex(db, settings, embedder=embedder, busy_check=lambda: False)
    return db, MeetingService(db, archive_root=archive), index, embedder, version


def _missing(db, meeting_id=MEETING):
    """当前版本还缺多少向量：和 main.sync_index_substates 同一个查询口径（按会议 id 取）。"""
    row = db.query_one(
        """SELECT COUNT(s.id) AS segment_count,
                  SUM(CASE WHEN e.segment_id IS NULL THEN 1 ELSE 0 END) AS missing_count
             FROM meetings m
             LEFT JOIN segments s ON s.version_id=m.current_transcript_version_id
             LEFT JOIN embeddings e ON e.segment_id=s.id AND e.model=?
            WHERE m.id=?""",
        (MODEL, meeting_id),
    )
    return int(row["segment_count"]), int(row["missing_count"] or 0)


def _current_rows(db):
    return db.query_all(
        """SELECT s.* FROM segments s JOIN meetings m ON m.current_transcript_version_id=s.version_id
            WHERE m.id=? ORDER BY s.ordinal""",
        (MEETING,),
    )


def test_saving_one_changed_segment_leaves_only_that_segment_for_the_semantic_index(tmp_path):
    db, service, index, embedder, _version = _world(tmp_path, 1500)
    assert index.rebuild() == 1500 and _missing(db) == (1500, 0)
    embedder.encoded.clear()
    rows = _current_rows(db)
    rows[0]["text"] = "只改了第一段"

    service.save_segments(MEETING, rows)

    # 新版本立刻就有 1499 段带向量，要补的只剩改了的那 1 段
    assert _missing(db) == (1500, 1)
    assert index.rebuild() == 1
    assert embedder.encoded == ["只改了第一段"]
    assert _missing(db) == (1500, 0)


def test_a_copied_vector_always_belongs_to_the_text_of_its_own_segment(tmp_path):
    db, service, index, _embedder, _version = _world(tmp_path, 40)
    index.rebuild()
    rows = _current_rows(db)
    rows[3]["text"] = "同一个 id 但文字改了"  # 客户端传的还是原来那段的 id
    rows[7]["text"] = rows[8]["text"]  # 改成了别的段已有的文字
    del rows[10]  # 删掉一段
    rows.insert(12, _item(900, "新增的一段"))

    service.save_segments(MEETING, rows)

    current = _current_rows(db)
    by_text = Counter()
    for segment in current:
        embedding = db.query_one(
            "SELECT vector FROM embeddings WHERE segment_id=? AND model=?", (segment["id"], MODEL)
        )
        if embedding is None:
            by_text[segment["text"]] += 1
            continue
        expected = index.encode_texts([segment["text"]], background=False)[0]
        assert embedding["vector"] == expected.astype(np.float32).tobytes(), segment["text"]
    # 文字改了的、新增的没有向量；改成别的段已有文字的那段，用的是那段文字自己的向量
    assert by_text == Counter({"同一个 id 但文字改了": 1, "新增的一段": 1})


def test_first_edit_of_a_published_version_keeps_every_vector_and_a_split_costs_two(tmp_path):
    db, service, index, embedder, _version = _world(tmp_path, 300)
    index.rebuild()
    embedder.encoded.clear()

    draft = service.ensure_draft(MEETING)

    assert _missing(db) == (300, 0)
    rows = db.query_all("SELECT id FROM segments WHERE version_id=? ORDER BY ordinal", (draft,))
    service.split_segment(MEETING, rows[5]["id"], 3)
    assert _missing(db) == (301, 2)  # 拆开的两半是新文字
    service.merge_segments(MEETING, rows[20]["id"], rows[21]["id"])
    assert _missing(db) == (300, 3)
    assert index.rebuild() == 3 and len(embedder.encoded) == 3
    assert _missing(db) == (300, 0)


def test_rolled_back_draft_reuses_vectors_from_the_source_and_the_current_version(tmp_path):
    db, service, index, embedder, base = _world(tmp_path, 200)
    index.rebuild()
    rows = _current_rows(db)
    rows[0]["text"] = "保存后的第一段"
    service.save_segments(MEETING, rows)  # v2，第一段文字和 v1 不一样

    # 语义索引还没来得及清掉旧版本的向量：回滚到 v1，v1 自己的向量就在
    service.rollback_transcript(MEETING, base)
    assert _missing(db) == (200, 0)

    # 回到 v2，等语义索引补完、清掉所有非当前版本的向量，再回滚到 v1：
    # v1 自己的向量没了，当前版本里文字相同的 199 段的向量照样能用，只有 v1 的第一段（只存在于 v1）要补
    current = db.query_one(
        "SELECT id FROM transcript_versions WHERE meeting_id=? AND kind='draft' AND version_no=2",
        (MEETING,),
    )["id"]
    service.rollback_transcript(MEETING, current)
    index.rebuild()
    embedder.encoded.clear()
    assert _missing(db) == (200, 0)
    service.rollback_transcript(MEETING, base)
    assert _missing(db) == (200, 1)
    assert index.rebuild() == 1
    assert embedder.encoded == [_item(0)["text"]]


def _insert_vectors(db, segment_ids_and_texts, model=MODEL, created_at="2026-10-01T00:00:00Z"):
    with db.transaction() as connection:
        connection.executemany(
            "INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at) "
            "VALUES (?, ?, 8, ?, ?)",
            [(i, model, _vector(text), created_at) for i, text in segment_ids_and_texts],
        )


def test_reuse_copies_every_model_with_dimensions_and_created_at_unchanged(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('m', 't', 'draft_modified')")
    old = db.create_transcript_version("m", "funasr", published=True)
    db.replace_segments(old, "m", [_item(i) for i in range(5)])
    _insert_vectors(db, [(f"seg-{i}", _item(i)["text"]) for i in range(5)])
    _insert_vectors(db, [("seg-0", _item(0)["text"])], model="other-model", created_at="2026-09-01")
    new = db.create_transcript_version("m", "draft", published=False)
    db.replace_segments(
        new, "m", [{**_item(i), "id": f"new-{i}"} for i in range(5)] + [_item(50, "新文字")]
    )

    with db.transaction() as connection:
        copied = db.reuse_embeddings_with_connection(connection, new, [old])

    assert copied == 6
    rows = db.query_all(
        "SELECT segment_id, model, dimensions, vector, created_at FROM embeddings "
        "WHERE segment_id LIKE 'new-%' ORDER BY segment_id, model"
    )
    assert [(r["segment_id"], r["model"]) for r in rows] == [
        ("new-0", MODEL),
        ("new-0", "other-model"),
        ("new-1", MODEL),
        ("new-2", MODEL),
        ("new-3", MODEL),
        ("new-4", MODEL),
    ]
    first = next(r for r in rows if r["model"] == "other-model")
    assert (first["dimensions"], first["created_at"]) == (8, "2026-09-01")
    assert all(r["vector"] == _vector(_item(int(r["segment_id"][4:]))["text"]) for r in rows)
    # 新增的那段（文字没有对应）一条都没有
    assert db.query_one("SELECT 1 FROM embeddings WHERE segment_id='seg-50'") is None


def test_reuse_never_borrows_from_another_meeting_or_overwrites_an_existing_vector(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('m1', 't', 'draft_modified')")
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('m2', 't', 'draft_modified')")
    v1 = db.create_transcript_version("m1", "funasr", published=True)
    db.replace_segments(v1, "m1", [_item(1)])
    other = db.create_transcript_version("m2", "funasr", published=True)
    db.replace_segments(other, "m2", [{**_item(1), "id": "other-1"}])
    _insert_vectors(db, [("other-1", _item(1)["text"])])  # 别的会里有同样文字的向量
    new = db.create_transcript_version("m1", "draft", published=False)
    db.replace_segments(new, "m1", [{**_item(1), "id": "new-1"}])
    _insert_vectors(db, [("new-1", "这条已经有了")], created_at="keep-me")

    with db.transaction() as connection:
        assert db.reuse_embeddings_with_connection(connection, new, [v1]) == 0  # v1 没向量
        assert db.reuse_embeddings_with_connection(connection, new, []) == 0

    row = db.query_one("SELECT vector, created_at FROM embeddings WHERE segment_id='new-1'")
    assert row == {"vector": _vector("这条已经有了"), "created_at": "keep-me"}
    _insert_vectors(db, [("seg-1", _item(1)["text"])])
    with db.transaction() as connection:
        db.reuse_embeddings_with_connection(connection, new, [v1])
    # 已有的向量不覆盖
    assert db.query_one("SELECT vector FROM embeddings WHERE segment_id='new-1'")["vector"] == (
        _vector("这条已经有了")
    )


def _srt(lines: list[str]) -> str:
    return "\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{line}\n"
        for index, line in enumerate(lines, start=1)
    )


def test_importer_giving_a_meeting_a_new_current_version_reuses_the_vectors_of_unchanged_text(
    tmp_path,
):
    """归档里的 SRT 取代导入时的版本（或被外部改写）：文字没变的段不用语义索引再编码一遍。
    生产备份里 197 / 213 场会有不止一个 funasr 版本，抽样 80 场当前版本的文字 100% 能在别的版本里找到。"""
    archive = tmp_path / "archive"
    formal = write_meeting(archive, "正式会议", official=True, transcript_text="占位")
    meeting_id = "vm-20260101-120000"
    srt = formal / f"{meeting_id}.srt"
    lines = [f"这是第 {number} 句的正文" for number in range(1, 51)]
    srt.write_text(_srt(lines), encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        semantic_enabled=True,
    )
    db = Database(settings.database_path)
    db.initialize()
    embedder = CountingEmbedder()
    index = SemanticIndex(db, settings, embedder=embedder, busy_check=lambda: False)
    importer = ArchiveImporter(db, settings)
    importer.scan()
    first_version = db.query_one(
        "SELECT current_transcript_version_id AS v FROM meetings WHERE id=?", (meeting_id,)
    )["v"]
    assert index.rebuild() == 50
    embedder.encoded.clear()

    lines[9] = "这一句被外部改写了"
    srt.write_text(_srt(lines), encoding="utf-8")
    importer.scan()

    second_version = db.query_one(
        "SELECT current_transcript_version_id AS v FROM meetings WHERE id=?", (meeting_id,)
    )["v"]
    assert second_version != first_version
    assert _missing(db, meeting_id) == (50, 1)
    assert index.rebuild() == 1
    assert embedder.encoded == ["这一句被外部改写了"]
