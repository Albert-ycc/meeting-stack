"""改逐字稿时的写入量和向量。

两件事：
1. 拆段、合段在编辑中的草稿上走 replace_segments_with_connection，以前先把整个版本的段落、
   全文索引行、向量全删光再原样写回，哪怕只动了一段（金标样本的 segment_id 外键被置空，
   语义索引要把整场会的向量重新编码一遍）。现在没动的段原地不动，只处理受影响的段。
2. 每次保存逐字稿都是界面「逐字稿版本」下拉里一个可回滚的新版本，所以整版写一份是必须的；
   但以前先复制 N 行再删掉重写，同一份内容写两遍。现在只写一遍。
"""

import random
import sqlite3
from collections import Counter

import pytest

from meeting_workbench.db import Database
from meeting_workbench.service import MeetingService

from .helpers import seed_editable_meeting

MEETING = "vm-20260102-101500"


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    return db


def _meeting_with_version(db, count, *, kind="draft"):
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, '编辑会', 'draft_modified')", (MEETING,)
    )
    version = db.create_transcript_version(MEETING, kind, published=False)
    db.replace_segments(version, MEETING, [_item(f"seg-{i}", i) for i in range(count)])
    return version


def _item(segment_id, index, text=None, *, label="SPEAKER_00", name=None):
    return {
        "id": segment_id,
        "start_ms": index * 1000,
        "end_ms": index * 1000 + 900,
        "speaker_label": label,
        "speaker_name": name,
        "text": text if text is not None else f"第 {segment_id} 句正文",
    }


def _rows(db, version):
    return [
        (
            r["id"],
            r["ordinal"],
            r["start_ms"],
            r["end_ms"],
            r["speaker_label"],
            r["speaker_name"],
            r["text"],
        )
        for r in db.query_all(
            "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (version,)
        )
    ]


def _expected_rows(items):
    return [
        (
            item["id"],
            ordinal,
            item["start_ms"],
            item["end_ms"],
            item.get("speaker_label"),
            item.get("speaker_name"),
            item["text"].strip(),
        )
        for ordinal, item in enumerate(items)
    ]


def _index_rows(db, version):
    return Counter(
        (r["segment_id"], r["text"])
        for r in db.query_all(
            "SELECT segment_id, text FROM segments_fts WHERE version_id=?", (version,)
        )
    )


def _add_vectors(db, ids):
    with db.transaction() as connection:
        connection.executemany(
            "INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at) "
            "VALUES (?, 'test-model', 4, ?, 'x')",
            [(segment_id, b"\x00" * 16) for segment_id in ids],
        )


def _vector_ids(db):
    return {r["segment_id"] for r in db.query_all("SELECT segment_id FROM embeddings")}


def _split(items, position, new_ids):
    first, second = dict(items[position]), dict(items[position])
    first.update(id=new_ids[0], text=items[position]["text"][:3])
    second.update(id=new_ids[1], text=items[position]["text"][3:])
    return [*items[:position], first, second, *items[position + 1 :]]


def test_replacing_a_few_segments_keeps_the_vectors_index_rows_and_gold_links_of_the_rest(tmp_path):
    db = _db(tmp_path)
    version = _meeting_with_version(db, 50)
    items = [_item(f"seg-{i}", i) for i in range(50)]
    _add_vectors(db, [item["id"] for item in items])
    for segment_id in ("seg-3", "seg-30"):
        db.execute(
            """INSERT INTO asr_gold_samples (id, meeting_id, segment_id, start_ms, end_ms,
               reference, created_at, updated_at) VALUES (?, ?, ?, 0, 1, '金标', 'x', 'x')""",
            (f"gold-{segment_id}", MEETING, segment_id),
        )

    after = _split(items, 3, ("new-a", "new-b"))  # 把 seg-3 拆成两段，后面的顺序号都往后挪一位
    db.replace_segments(version, MEETING, after)

    assert _rows(db, version) == _expected_rows(after)
    assert _index_rows(db, version) == Counter((i["id"], i["text"].strip()) for i in after)
    # 只有被拆掉的 seg-3 没了向量；新拆出的两段还没有；别的 49 段一份没动
    assert _vector_ids(db) == {f"seg-{i}" for i in range(50)} - {"seg-3"}
    gold = {
        r["id"]: r["segment_id"]
        for r in db.query_all("SELECT id, segment_id FROM asr_gold_samples")
    }
    assert gold == {"gold-seg-3": None, "gold-seg-30": "seg-30"}


def test_merging_two_segments_drops_only_their_vectors(tmp_path):
    db = _db(tmp_path)
    version = _meeting_with_version(db, 40)
    items = [_item(f"seg-{i}", i) for i in range(40)]
    _add_vectors(db, [item["id"] for item in items])
    merged = _item("merged", 10, text=items[10]["text"] + items[11]["text"])
    after = [*items[:10], merged, *items[12:]]

    db.replace_segments(version, MEETING, after)

    assert _rows(db, version) == _expected_rows(after)
    assert _vector_ids(db) == {f"seg-{i}" for i in range(40)} - {"seg-10", "seg-11"}


def test_a_changed_text_loses_its_vector_but_a_changed_speaker_or_time_keeps_it(tmp_path):
    db = _db(tmp_path)
    version = _meeting_with_version(db, 30)
    items = [_item(f"seg-{i}", i) for i in range(30)]
    _add_vectors(db, ["seg-5", "seg-6", "seg-7"])
    after = [dict(item) for item in items]
    after[5]["text"] = "改过的正文"
    after[6]["speaker_name"] = "张三"
    after[7]["start_ms"], after[7]["end_ms"] = 123, 456

    db.replace_segments(version, MEETING, after)

    assert _rows(db, version) == _expected_rows(after)
    assert _index_rows(db, version) == Counter((i["id"], i["text"]) for i in after)
    assert _vector_ids(db) == {"seg-6", "seg-7"}


def test_inserting_at_the_front_shifts_every_ordinal_without_tripping_the_unique_key(tmp_path):
    db = _db(tmp_path)
    version = _meeting_with_version(db, 300)
    items = [_item(f"seg-{i}", i) for i in range(300)]
    after = [_item("front", 0, text="新开头"), *items]

    db.replace_segments(version, MEETING, after)

    assert _rows(db, version) == _expected_rows(after)


def test_duplicate_ids_still_fail_and_leave_the_version_as_it_was(tmp_path):
    db = _db(tmp_path)
    version = _meeting_with_version(db, 5)
    before = _rows(db, version)
    dup = [_item("seg-0", 0), _item("seg-0", 1), *[_item(f"seg-{i}", i) for i in range(2, 5)]]

    with pytest.raises(sqlite3.IntegrityError):
        db.replace_segments(version, MEETING, dup)
    other = db.create_transcript_version(MEETING, "draft", published=False)
    with pytest.raises(sqlite3.IntegrityError):  # 别的版本里的段落 id
        db.replace_segments(other, MEETING, [_item("seg-1", 0)])

    assert _rows(db, version) == before


@pytest.mark.parametrize("seed", range(8))
def test_any_sequence_of_edits_ends_in_exactly_the_requested_list(tmp_path, seed):
    """随机拆、合、改字、删、插、调序、整批改写，每一步之后库里的段落和全文索引都要恰好是传进去的那份；
    向量只留给「文字没变的老段」。"""
    rng = random.Random(seed)
    db = _db(tmp_path)
    count = 0

    def fresh():
        nonlocal count
        count += 1
        return f"s{count}"

    items = [_item(fresh(), i) for i in range(rng.randint(4, 40))]
    version = _meeting_with_version(db, 0)
    db.replace_segments(version, MEETING, items)
    for _step in range(60):
        vectors = {i["id"] for i in items if rng.random() < 0.8}
        db.execute("DELETE FROM embeddings")
        _add_vectors(db, vectors)
        old_text = {i["id"]: i["text"].strip() for i in items}
        new = [dict(i) for i in items]
        op = rng.choice(
            ["retext", "split", "merge", "delete", "insert", "swap", "speaker", "rewrite"]
        )
        if op == "retext" and new:
            rng.choice(new)["text"] = f"改{rng.random():.6f}"
        elif op == "split" and new:
            position = rng.randrange(len(new))
            new = _split(new, position, (fresh(), fresh()))
        elif op == "merge" and len(new) > 1:
            position = rng.randrange(len(new) - 1)
            merged = dict(
                new[position], id=fresh(), text=new[position]["text"] + new[position + 1]["text"]
            )
            new[position : position + 2] = [merged]
        elif op == "delete" and len(new) > 2:
            del new[rng.randrange(len(new))]
        elif op == "insert":
            new.insert(rng.randint(0, len(new)), _item(fresh(), 99, text=f"插入{count}"))
        elif op == "swap" and len(new) > 1:
            a, b = rng.sample(range(len(new)), 2)
            new[a], new[b] = new[b], new[a]
        elif op == "speaker" and new:
            rng.choice(new)["speaker_name"] = f"人{rng.randint(1, 3)}"
        elif op == "rewrite":
            new = [dict(i, text=f"全改{rng.random():.6f}") for i in new]

        db.replace_segments(version, MEETING, new)

        assert _rows(db, version) == _expected_rows(new), (seed, op)
        assert _index_rows(db, version) == Counter((i["id"], i["text"].strip()) for i in new), (
            seed,
            op,
        )
        assert _vector_ids(db) == {
            i["id"]
            for i in new
            if i["id"] in vectors and old_text.get(i["id"]) == i["text"].strip()
        }, (seed, op)
        items = new


def _world_with_segments(tmp_path, count):
    db = _db(tmp_path)
    archive = tmp_path / "archive"
    seed_editable_meeting(db, archive)
    version = db.query_one(
        "SELECT current_transcript_version_id AS v FROM meetings WHERE id=?", (MEETING,)
    )["v"]
    db.replace_segments(version, MEETING, [_item(f"seg-{i}", i) for i in range(count)])
    return db, MeetingService(db, archive_root=archive)


def test_splitting_in_an_edit_draft_drops_the_vector_of_the_split_segment_only(tmp_path):
    db, service = _world_with_segments(tmp_path, 40)
    draft = service.ensure_draft(MEETING)
    ids = [
        r["id"]
        for r in db.query_all(
            "SELECT id FROM segments WHERE version_id=? ORDER BY ordinal", (draft,)
        )
    ]
    _add_vectors(db, ids)

    first, second = service.split_segment(MEETING, ids[7], 3)
    merged = service.merge_segments(MEETING, ids[20], ids[21])

    assert _vector_ids(db) == set(ids) - {ids[7], ids[20], ids[21]}
    assert (
        first not in _vector_ids(db)
        and second not in _vector_ids(db)
        and merged not in _vector_ids(db)
    )
    assert len(_rows(db, draft)) == 40 + 1 - 1  # 拆出一段、合掉一段


def _count_inserts(monkeypatch):
    counts = Counter()
    real_connect = Database.connect

    def connect(self):
        connection = real_connect(self)

        def trace(sql):
            head = sql.lstrip()[:40].upper()
            if head.startswith("INSERT INTO SEGMENTS_FTS"):
                counts["fts"] += 1
            elif head.startswith("INSERT INTO SEGMENTS"):
                counts["segments"] += 1

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(Database, "connect", connect)
    return counts


def _total(db, table):
    return db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


def test_each_save_is_one_visible_version_and_writes_its_rows_once(tmp_path, monkeypatch):
    db, service = _world_with_segments(tmp_path, 1500)
    counts = _count_inserts(monkeypatch)
    base = db.query_one(
        "SELECT current_transcript_version_id AS v FROM meetings WHERE id=?", (MEETING,)
    )["v"]
    saved = []

    for round_no in range(1, 4):
        rows = db.query_all(
            """SELECT s.* FROM segments s JOIN meetings m ON m.current_transcript_version_id=s.version_id
               WHERE m.id=? ORDER BY s.ordinal""",
            (MEETING,),
        )
        rows[0]["text"] = f"第 {round_no} 次保存"
        segments_before, index_before = _total(db, "segments"), _total(db, "segments_fts")
        counts.clear()

        saved.append(service.save_segments(MEETING, rows))

        # 整版一份写一遍：以前先复制再重写，每行写两遍（3000）
        assert (counts["segments"], counts["fts"]) == (1500, 1500)
        # 每次保存都是界面上一个新的可回滚版本，所以行数每次仍多 1500
        assert _total(db, "segments") - segments_before == 1500
        assert _total(db, "segments_fts") - index_before == 1500

    # 版本下拉里的内容：三次保存是三个草稿版本，一份没少，每份都是完整的 1500 段
    versions = db.query_all(
        """SELECT tv.id, tv.version_no, tv.kind, tv.based_on_id, tv.published, COUNT(s.id) AS n
             FROM transcript_versions tv LEFT JOIN segments s ON s.version_id=tv.id
            WHERE tv.meeting_id=? GROUP BY tv.id ORDER BY tv.version_no DESC""",
        (MEETING,),
    )
    assert [(v["version_no"], v["kind"], v["published"], v["n"]) for v in versions] == [
        (4, "draft", 0, 1500),
        (3, "draft", 0, 1500),
        (2, "draft", 0, 1500),
        (1, "funasr", 1, 1500),
    ]
    assert [v["id"] for v in versions] == [*reversed(saved), base]
    assert [v["based_on_id"] for v in versions] == [saved[1], saved[0], base, None]

    # 每个中间版本都还能回滚到，内容就是当时保存的那份
    rolled = service.rollback_transcript(MEETING, saved[0])
    first_text = db.query_one(
        "SELECT text FROM segments WHERE version_id=? AND ordinal=0", (rolled,)
    )["text"]
    assert first_text == "第 1 次保存"
