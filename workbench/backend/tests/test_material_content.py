"""第三期 3a：内容标识、材料内容循环、出错处理、没人引用的内容、按内容找回你选的文件。"""
import errno
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meeting_workbench import file_mentions, material_content
from meeting_workbench.db import utc_now
from meeting_workbench.material_content import ExtractResult, MaterialContent, compute_content_key
from meeting_workbench.material_helpers import StopFlag
from meeting_workbench.material_index import MaterialIndexer
from meeting_workbench.materials import ROOT_ONLINE, ROOT_VOLUME_OFFLINE

from .test_graph import add_meeting
from .test_material_index import add_root, make, run_until_done, write

OLD = time.time() - 3600


def aged(path: Path, when: float = OLD) -> Path:
    os.utime(path, (when, when))
    return path


def put(path: Path, data: bytes | str = "正文", when: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return aged(path, when)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class Now:
    def __init__(self):
        self.value = datetime(2026, 9, 27, 12, tzinfo=UTC)

    def __call__(self):
        return self.value


def setup(tmp_path, *, online=None, extractors=None, busy=None, stop=None):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    root.mkdir(exist_ok=True)
    root_id = add_root(db, root)
    state = {"online": True} if online is None else online
    now = Now()
    content = MaterialContent(
        db,
        settings,
        state_of=lambda path: ROOT_ONLINE if state["online"] else ROOT_VOLUME_OFFLINE,
        busy_check=busy,
        stop=stop,
        clock=Clock(),
        now=now,
        extractors=extractors,
    )
    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0)
    return db, settings, root, root_id, content, indexer, now, state


def index(indexer):
    indexer.db.execute("UPDATE material_index_state SET last_full_at=NULL")
    run_until_done(indexer)


def keys(db):
    return {
        row["rel_path"]: row["content_key"]
        for row in db.query_all("SELECT rel_path, content_key FROM material_files WHERE gone_at IS NULL")
    }


def contents(db):
    return {row["content_key"]: row for row in db.query_all("SELECT * FROM material_contents")}


# ---------------------------------------------------------------------- 内容标识


def test_small_files_hash_whole_content(tmp_path):
    a = put(tmp_path / "a.bmp", b"BM" + b"\0" * 5000 + b"A" + b"\0" * 5000)
    b = put(tmp_path / "b.bmp", b"BM" + b"\0" * 5000 + b"B" + b"\0" * 5000)
    c = put(tmp_path / "c.bmp", a.read_bytes())
    key_a, size = compute_content_key(a)
    assert key_a.startswith("q2:") and len(key_a) == 3 + 32 and size == a.stat().st_size
    assert key_a != compute_content_key(b)[0]
    assert key_a == compute_content_key(c)[0]
    # 超过 128KB 的 CSV 中间改一个数、长度不变：整份算（16MB 以内），标识变了
    rows = "\n".join(f"{index},{index * 3}" for index in range(30000))
    first = put(tmp_path / "d.csv", rows)
    key_d = compute_content_key(first)[0]
    put(tmp_path / "d.csv", rows.replace("15000,45000", "15000,45001"))
    assert compute_content_key(first)[0] != key_d


def test_large_files_sample_head_tail_and_middle_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(material_content, "WHOLE_HASH_LIMIT", 256 * 1024)
    size = 1024 * 1024
    data = bytearray(b"x" * size)
    base = put(tmp_path / "big.bin", bytes(data))
    key = compute_content_key(base)[0]
    # 中间第 4 块被取到的位置改一个字节：标识变
    span = size - 2 * material_content.EDGE_BYTES - material_content.SAMPLE_BYTES
    offset = material_content.EDGE_BYTES + span * 4 // (material_content.SAMPLE_BLOCKS + 1)
    data[offset] = ord("y")
    put(tmp_path / "big.bin", bytes(data))
    assert compute_content_key(base)[0] != key
    # 大小不同，标识不同
    put(tmp_path / "big2.bin", b"x" * (size + 1))
    assert compute_content_key(tmp_path / "big2.bin")[0] != key


def test_package_uses_main_file(tmp_path):
    package = tmp_path / "说明.rtfd"
    put(package / "TXT.rtf", r"{\rtf1 正文}")
    put(package / "图片.png", b"\x89PNG")
    assert compute_content_key(package)[0] == compute_content_key(package / "TXT.rtf")[0]


# ---------------------------------------------------------------------- 算标识


def test_same_content_in_two_roots_moves_renames_and_copies_is_read_once(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    other_root = tmp_path / "备份盘"
    other_root.mkdir()
    add_root(db, other_root)
    put(root / "方案/需求说明书.docx", "同一份内容")
    put(other_root / "需求说明书 副本.docx", "同一份内容")
    put(root / "报价单.xlsx", "另一份")
    index(indexer)
    content.run_round()
    found = keys(db)
    assert found["方案/需求说明书.docx"] == found["需求说明书 副本.docx"] != found["报价单.xlsx"]
    assert len(contents(db)) == 2
    assert {row["state"] for row in contents(db).values()} == {"pending"}

    # 挪位置、改名：新行得到同一个标识，不新建内容行
    (root / "方案/需求说明书.docx").rename(root / "归档说明.docx")
    index(indexer)
    content.run_round()
    assert keys(db)["归档说明.docx"] == found["方案/需求说明书.docx"]
    assert len(contents(db)) == 2


def test_changed_size_or_mtime_recomputes_the_key_and_only_new_content_is_reread(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    doc = put(root / "纪要.md", "第一版")
    index(indexer)
    content.run_round()
    first = keys(db)["纪要.md"]
    db.execute("UPDATE material_contents SET state='done' WHERE content_key=?", (first,))

    # 只改了修改时间（exFAT 换时区）：重算标识，标识没变就不重读
    aged(doc, OLD - 100)
    index(indexer)
    content.run_round()
    assert keys(db)["纪要.md"] == first
    assert contents(db)[first]["state"] == "done"

    # 内容变了：新标识、新的待读内容行
    put(doc, "第二版", OLD - 50)
    index(indexer)
    content.run_round()
    second = keys(db)["纪要.md"]
    assert second != first and contents(db)[second]["state"] == "pending"


def test_files_still_changing_are_skipped(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    fresh = put(root / "刚下载.pdf", "%PDF-1.4", when=time.time() - 30)
    put(root / "录屏.mov", b"\0" * 10, when=time.time() - 300)  # 音视频 10 分钟以内算还在变
    index(indexer)
    content.run_round()
    assert keys(db) == {"刚下载.pdf": None, "录屏.mov": None}
    # 行里的大小和实际不一样（还在拷贝）：顺手改成实时值，这一轮跳过
    fresh.write_bytes(b"%PDF-1.4 more bytes")
    aged(fresh)
    content.run_round()
    row = db.query_one("SELECT size, content_key FROM material_files WHERE name='刚下载.pdf'")
    assert row == {"size": fresh.stat().st_size, "content_key": None}
    content.run_round()
    assert keys(db)["刚下载.pdf"] is not None


def test_write_back_is_skipped_when_the_file_changes_while_hashing(tmp_path, monkeypatch):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    doc = put(root / "报价单.csv", "1,2")
    index(indexer)
    real = material_content.compute_content_key

    def edit_while_hashing(path, **kwargs):
        result = real(path, **kwargs)
        put(doc, "1,2,3", OLD + 10)
        return result

    monkeypatch.setattr(material_content, "compute_content_key", edit_while_hashing)
    content.run_round()
    assert keys(db)["报价单.csv"] is None and contents(db) == {}


def test_iwork_is_marked_unsupported_on_the_file_row(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    put(root / "汇报.key" / "Index.zip", b"PK")
    put(root / "预算.numbers", b"PK")
    index(indexer)
    content.run_round()
    rows = db.query_all("SELECT name, content_key, content_error FROM material_files ORDER BY name")
    assert rows == [
        {"name": "汇报.key", "content_key": None, "content_error": "unsupported"},
        {"name": "预算.numbers", "content_key": None, "content_error": "unsupported"},
    ]
    assert contents(db) == {}


def test_cards_and_names_only_files_get_no_content_row(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    put(root / "声档会议记录/2026-09-26 周会.md", "卡片")
    put(root / "源码.zip", b"PK")
    put(root / "依赖.lock", "x")
    index(indexer)
    content.run_round()
    assert set(keys(db).values()) == {None}
    assert contents(db) == {}


# ---------------------------------------------------------------------- 出错


def test_io_errors_retry_twice_before_corrupt(tmp_path, monkeypatch):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "扫描件.pdf", "%PDF")
    put(root / "另一份.pdf", "%PDF-2")
    index(indexer)
    real = material_content.compute_content_key
    failing = {"on": True}

    def eio(path, **kwargs):
        if failing["on"] and path.name == "扫描件.pdf":
            raise OSError(errno.EIO, "Input/output error")
        return real(path, **kwargs)

    monkeypatch.setattr(material_content, "compute_content_key", eio)

    def error():
        return db.query_one(
            "SELECT content_error, content_attempts FROM material_files WHERE name='扫描件.pdf'"
        )

    # 第一次：这一轮停下，不记读不了
    stats = content.run_round()
    assert stats["ended"] == "io"
    assert error() == {"content_error": "io", "content_attempts": 1}
    # 1 小时内不重试
    content.run_round()
    assert error() == {"content_error": "io", "content_attempts": 1}
    now.value += timedelta(hours=1, seconds=1)
    content.run_round()
    assert error() == {"content_error": "io", "content_attempts": 2}
    # 第二次后要等 24 小时
    now.value += timedelta(hours=2)
    content.run_round()
    assert error()["content_attempts"] == 2
    now.value += timedelta(hours=23)
    content.run_round()
    assert error() == {"content_error": "corrupt", "content_attempts": 3}


def test_io_error_then_success_is_not_corrupt(tmp_path, monkeypatch):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "扫描件.pdf", "%PDF")
    index(indexer)
    real = material_content.compute_content_key
    failing = {"on": True}

    def eio(path, **kwargs):
        if failing["on"]:
            raise OSError(errno.EIO, "Input/output error")
        return real(path, **kwargs)

    monkeypatch.setattr(material_content, "compute_content_key", eio)
    content.run_round()
    failing["on"] = False
    now.value += timedelta(hours=2)
    content.run_round()
    row = db.query_one("SELECT content_key, content_error FROM material_files")
    assert row["content_key"] and row["content_error"] is None


def test_errors_with_the_disk_gone_write_nothing(tmp_path, monkeypatch):
    online = {"online": True}
    db, settings, root, root_id, content, indexer, now, state = setup(tmp_path, online=online)
    put(root / "扫描件.pdf", "%PDF")
    index(indexer)

    def unplugged(path, **kwargs):
        online["online"] = False
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(material_content, "compute_content_key", unplugged)
    content.run_round()
    row = db.query_one("SELECT content_key, content_error, content_attempts FROM material_files")
    assert row == {"content_key": None, "content_error": None, "content_attempts": None}


def test_permission_is_recorded_on_the_file_row_and_retried_after_a_day(tmp_path, monkeypatch):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "合同.pdf", "%PDF")
    index(indexer)
    real = material_content.compute_content_key
    denied = {"on": True}

    def maybe_denied(path, **kwargs):
        if denied["on"]:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(path, **kwargs)

    monkeypatch.setattr(material_content, "compute_content_key", maybe_denied)
    assert content.run_round()["ended"] is None
    assert db.query_one("SELECT content_error FROM material_files")["content_error"] == "permission"
    denied["on"] = False
    content.run_round()
    assert db.query_one("SELECT content_error FROM material_files")["content_error"] == "permission"
    now.value += timedelta(hours=24, seconds=1)
    content.run_round()
    row = db.query_one("SELECT content_key, content_error FROM material_files")
    assert row["content_key"] and row["content_error"] is None


# ---------------------------------------------------------------------- 读内容


def fake_extractor(calls, *, result=None, during=None):
    def extract(path, layer, row):
        calls.append(path.name)
        if during is not None:
            during(path)
        return result or ExtractResult(status="ok", chars=4, extractor="fake", extractor_version=1)

    return extract


def test_extraction_result_is_dropped_when_the_file_changes_while_reading(tmp_path):
    calls: list[str] = []

    def edit(path):
        put(path, "改过了", OLD + 20)

    db, settings, root, root_id, content, indexer, _now, _state = setup(
        tmp_path, extractors={"text": fake_extractor(calls, during=edit)}
    )
    put(root / "纪要.md", "原来的")
    index(indexer)
    content.run_round()
    assert calls == ["纪要.md"]
    assert {row["state"] for row in contents(db).values()} == {"pending"}


def test_extraction_reads_each_content_once_and_marks_done(tmp_path):
    calls: list[str] = []
    db, settings, root, root_id, content, indexer, _now, _state = setup(
        tmp_path, extractors={"text": fake_extractor(calls)}
    )
    put(root / "a/纪要.md", "同一份")
    put(root / "b/纪要 副本.md", "同一份")
    index(indexer)
    content.run_round()
    assert len(calls) == 1
    assert [row["state"] for row in contents(db).values()] == ["done"]
    content.run_round()
    assert len(calls) == 1


def test_extraction_io_errors_count_towards_corrupt_on_the_file_row(tmp_path):
    calls: list[str] = []
    db, settings, root, root_id, content, indexer, now, _state = setup(
        tmp_path, extractors={"text": fake_extractor(calls, result=ExtractResult(status="io_error"))}
    )
    put(root / "纪要.md", "x")
    index(indexer)
    for hours in (0, 2, 25):
        now.value += timedelta(hours=hours)
        content.run_round()
    row = db.query_one("SELECT content_error, content_attempts FROM material_files")
    assert row == {"content_error": "corrupt", "content_attempts": 3}
    assert len(calls) == 3


def test_extraction_order_prefers_mentioned_then_requirement_folders_then_newest(tmp_path):
    calls: list[str] = []
    db, settings, root, root_id, content, indexer, _now, _state = setup(
        tmp_path, extractors={"text": fake_extractor(calls)}
    )
    put(root / "旧的.md", "1", OLD - 5000)
    put(root / "新的.md", "2", OLD - 10)
    put(root / "需求A/说明.md", "3", OLD - 9000)
    put(root / "被提到.md", "4", OLD - 20000)
    index(indexer)
    db.execute(
        "INSERT INTO requirements(id, project_id, title, priority, created_at, updated_at) VALUES ('r', 'p', '需求A', 'P1', ?, ?)",
        (utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES ('r', ?, ?)",
        (str(root / "需求A"), utc_now()),
    )
    add_meeting(db, "m", ago=1, project_id="p")
    file_id = db.query_one("SELECT id FROM material_files WHERE name='被提到.md'")["id"]
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, updated_at)
           VALUES ('m', 'p', '被提到', ?, '被提到', ?)""",
        (file_id, utc_now()),
    )
    content.run_round()
    assert calls == ["被提到.md", "说明.md", "新的.md", "旧的.md"]


def test_busy_and_stop_end_the_round(tmp_path):
    calls: list[str] = []
    busy = {"value": False}
    stop = StopFlag()
    db, settings, root, root_id, content, indexer, _now, _state = setup(
        tmp_path, extractors={"text": fake_extractor(calls)}, busy=lambda: busy["value"], stop=stop
    )
    for index_ in range(3):
        put(root / f"纪要{index_}.md", f"内容{index_}")
    index(indexer)
    busy["value"] = True
    assert content.run_round()["ended"] == "busy"
    assert calls == [] and set(keys(db).values()) == {None}
    assert content.progress["paused"] == "busy"

    busy["value"] = False
    stop.set()
    assert content.run_round()["ended"] == "stopping"
    stop.clear()

    # 处理完一个文件就查一次：会议开始转写，这一轮立刻结束
    def start_meeting(path):
        busy["value"] = True

    content.extractors["text"] = fake_extractor(calls, during=start_meeting)
    busy["value"] = False
    stats = content.run_round()
    assert stats["ended"] == "busy" and len(calls) <= 1


def test_database_busy_ends_the_round(tmp_path, monkeypatch):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    put(root / "纪要.md", "x")
    index(indexer)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(content, "_write_key", locked)
    assert content.run_round()["ended"] == "db_busy"


def test_progress_counts_pending_and_offline(tmp_path):
    online = {"online": True}
    db, settings, root, root_id, content, indexer, _now, state = setup(tmp_path, online=online)
    put(root / "纪要.md", "x")
    put(root / "图.png", b"\x89PNG")
    put(root / "源码.zip", b"PK")
    put(root / "预算.numbers", b"PK")
    index(indexer)
    content.run_round()
    roots = content.progress.pop("roots")
    assert content.progress == {"pending": 2, "offline_pending": 0, "paused": None}
    # 3g：每个根目录的计数（关系图项目面板用），文件名都算
    assert roots[root_id]["files"] == 4
    online["online"] = False
    content.run_round()
    content.progress.pop("roots")
    assert content.progress == {"pending": 2, "offline_pending": 2, "paused": None}


# ---------------------------------------------------------------------- 没人引用的内容


def test_orphaned_contents_are_deleted_after_thirty_days_only_when_everything_is_online(tmp_path):
    online = {"online": True}
    db, settings, root, root_id, content, indexer, now, state = setup(tmp_path, online=online)
    doc = put(root / "纪要.md", "x")
    index(indexer)
    content.run_round()
    key = keys(db)["纪要.md"]
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES (?, 0, '片段')", (key,)
    )
    doc.unlink()
    index(indexer)

    def orphan_pass():
        content._last_orphan_pass = None
        content.run_round()

    orphan_pass()
    assert contents(db)[key]["orphan_since"] is not None
    now.value += timedelta(days=31)
    online["online"] = False
    orphan_pass()
    assert key in contents(db)  # 有根目录离线时不删
    online["online"] = True
    db.execute("UPDATE material_index_state SET state='walking'")
    orphan_pass()
    assert key in contents(db)  # 文件名索引没扫完不删
    db.execute("UPDATE material_index_state SET state='done'")
    orphan_pass()
    assert key not in contents(db)
    # 片段跟着级联删掉，全文索引也同步删掉
    assert db.query_one("SELECT COUNT(*) AS n FROM material_chunks")["n"] == 0
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM material_chunks_fts WHERE material_chunks_fts MATCH '\"片段\"'"
    )["n"] == 0


def test_orphan_that_comes_back_before_deletion_is_kept(tmp_path):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    doc = put(root / "纪要.md", "x")
    index(indexer)
    content.run_round()
    key = keys(db)["纪要.md"]
    moved = tmp_path / "临时.md"
    doc.rename(moved)
    index(indexer)
    content._last_orphan_pass = None
    content.run_round()
    assert contents(db)[key]["orphan_since"] is not None
    moved.rename(root / "回来了.md")
    index(indexer)
    now.value += timedelta(days=31)
    content._last_orphan_pass = None
    content.run_round()  # 先算出标识（清掉 orphan_since），再清理
    assert key in contents(db) and contents(db)[key]["orphan_since"] is None


def test_removing_a_root_orphans_its_contents(tmp_path):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "纪要.md", "x")
    index(indexer)
    content.run_round()
    key = keys(db)["纪要.md"]
    db.execute("DELETE FROM project_material_roots WHERE id=?", (root_id,))
    content._last_orphan_pass = None
    content.run_round()
    now.value += timedelta(days=31)
    content._last_orphan_pass = None
    content.run_round()
    assert key not in contents(db)


def test_a_file_whose_content_row_was_cleaned_up_gets_a_new_pending_row(tmp_path):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "纪要.md", "x")
    index(indexer)
    content.run_round()
    key = keys(db)["纪要.md"]
    db.execute("DELETE FROM material_contents")
    content._last_orphan_pass = None
    content.run_round()
    assert contents(db)[key]["state"] == "pending" and contents(db)[key]["layer"] == "text"


# ---------------------------------------------------------------------- 按内容找回你选的文件


def run_mentions(db):
    return file_mentions.match_pending(db, clock=lambda: 0.0)


def mention(db):
    return db.query_one("SELECT file_id, picked FROM meeting_file_mentions WHERE meeting_id='m'")


def test_picked_file_follows_its_content_after_a_move(tmp_path):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "报价单 v1.xlsx", "第一版", OLD - 500)
    put(root / "报价单 v2.xlsx", "第二版", OLD - 100)
    (root / "归档").mkdir()
    index(indexer)
    add_meeting(db, "m", ago=1, project_id="p", segments=[(0, "报价单明天发")])
    run_mentions(db)
    v1 = db.query_one("SELECT id FROM material_files WHERE name='报价单 v1.xlsx'")["id"]
    file_mentions.pick_mention_file(db, "m", "报价单", v1)
    assert db.query_one("SELECT content_key FROM material_files WHERE id=?", (v1,))["content_key"]
    run_mentions(db)
    assert mention(db) == {"file_id": v1, "picked": 1}

    # 挪到归档文件夹：比对先于算标识，你选的这份原样保留
    (root / "报价单 v1.xlsx").rename(root / "归档" / "报价单 v1.xlsx")
    index(indexer)
    run_mentions(db)
    assert mention(db) == {"file_id": v1, "picked": 1}

    # 算出标识：同一份内容，换到新位置、仍算你选的
    content.run_round()
    run_mentions(db)
    moved = db.query_one(
        "SELECT id FROM material_files WHERE rel_path='归档/报价单 v1.xlsx' AND gone_at IS NULL"
    )["id"]
    assert mention(db) == {"file_id": moved, "picked": 1}


def test_picked_file_whose_stem_changed_falls_back_to_automatic(tmp_path):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "报价单 v1.xlsx", "第一版", OLD - 500)
    put(root / "报价单 v2.xlsx", "第二版", OLD - 100)
    index(indexer)
    add_meeting(db, "m", ago=1, project_id="p", segments=[(0, "报价单明天发")])
    run_mentions(db)
    v1 = db.query_one("SELECT id FROM material_files WHERE name='报价单 v1.xlsx'")["id"]
    v2 = db.query_one("SELECT id FROM material_files WHERE name='报价单 v2.xlsx'")["id"]
    file_mentions.pick_mention_file(db, "m", "报价单", v1)
    content.run_round()
    (root / "报价单 v1.xlsx").rename(root / "作废的旧价.xlsx")
    index(indexer)
    content.run_round()
    run_mentions(db)
    assert mention(db) == {"file_id": v2, "picked": 0}


# ---------------------------------------------------------------------- 文件名索引：一时读不了


def test_a_single_unreadable_entry_does_not_mark_anything_gone(tmp_path, monkeypatch):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "方案" / "需求说明书.docx")
    write(root / "方案" / "子目录" / "报价单.xlsx")
    write(root / "方案" / "排期.xlsx")
    add_root(db, root)
    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0)
    run_until_done(indexer)
    before = {row["rel_path"] for row in db.query_all("SELECT rel_path FROM material_files WHERE gone_at IS NULL")}

    from meeting_workbench import material_index

    real_scandir = os.scandir

    class Flaky:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name

        def is_symlink(self):
            return self._entry.is_symlink()

        def is_dir(self, follow_symlinks=True):
            if self.name in {"需求说明书.docx", "子目录"}:
                raise OSError(errno.EIO, "Input/output error")
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, follow_symlinks=True):
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class FlakyScandir:
        def __init__(self, path):
            self._inner = real_scandir(path)
            self._flaky = str(path).endswith("方案")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

        def __iter__(self):
            for entry in self._inner:
                yield Flaky(entry) if self._flaky else entry

    monkeypatch.setattr(material_index.os, "scandir", FlakyScandir)
    # 目录修改时间变了才会重读：加一个新文件
    write(root / "方案" / "新文件.md")
    db.execute("UPDATE material_index_state SET last_full_at=NULL")
    run_until_done(indexer)
    alive = {row["rel_path"] for row in db.query_all("SELECT rel_path FROM material_files WHERE gone_at IS NULL")}
    assert before | {"方案/新文件.md"} == alive
    assert db.query_one("SELECT mtime_ns FROM material_dirs WHERE dir_rel='方案'")["mtime_ns"] is None
    assert db.query_one("SELECT COUNT(*) AS n FROM material_dirs WHERE dir_rel='方案/子目录'")["n"] == 1

    # 下一轮（增量）一定重读这个目录
    monkeypatch.setattr(material_index.os, "scandir", real_scandir)
    indexer.run_round()
    assert db.query_one("SELECT mtime_ns FROM material_dirs WHERE dir_rel='方案'")["mtime_ns"] is not None


def test_a_single_unreadable_entry_with_the_disk_gone_stops_the_round(tmp_path, monkeypatch):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "方案" / "需求说明书.docx")
    add_root(db, root)
    online = {"value": True}
    indexer = MaterialIndexer(
        db, settings, clock=lambda: 0.0,
        state_of=lambda path: ROOT_ONLINE if online["value"] else ROOT_VOLUME_OFFLINE,
    )
    run_until_done(indexer)
    from meeting_workbench import material_index

    real_scandir = os.scandir

    class Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name

        def is_symlink(self):
            return False

        def is_dir(self, follow_symlinks=True):
            online["value"] = False
            raise OSError(errno.EIO, "Input/output error")

    class Scandir:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

        def __iter__(self):
            for entry in self._inner:
                yield Entry(entry)

    monkeypatch.setattr(material_index.os, "scandir", Scandir)
    db.execute("UPDATE material_index_state SET last_full_at=NULL")
    indexer.run_round()
    assert db.query_one("SELECT state FROM material_index_state")["state"] == "offline"
    assert db.query_one("SELECT COUNT(*) AS n FROM material_files WHERE gone_at IS NOT NULL")["n"] == 0


@pytest.mark.parametrize("missing", ["gone", "deleted"])
def test_key_file_now_skips_missing_rows(tmp_path, missing):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    put(root / "报价单.zip", b"PK")
    index(indexer)
    file_id = db.query_one("SELECT id FROM material_files")["id"]
    assert material_content.key_file_now(db, file_id)  # 只收文件名的也算，但不建内容行
    assert contents(db) == {}
    if missing == "gone":
        db.execute("UPDATE material_files SET gone_at=?", (utc_now(),))
    else:
        db.execute("DELETE FROM material_files")
    assert material_content.key_file_now(db, file_id) is None


# ---------------------------------------------------------------------- 服务里的循环


@pytest.mark.parametrize("enabled", [True, False])
def test_lifespan_cleans_up_before_the_loop_and_stops_on_shutdown(tmp_path, monkeypatch, enabled):
    from fastapi.testclient import TestClient

    from meeting_workbench import main as main_module
    from meeting_workbench.config import Settings

    calls: list[str] = []
    monkeypatch.setattr(main_module, "cleanup_leftovers", lambda db, data_dir: calls.append("cleanup"))
    (tmp_path / "archive").mkdir()
    (tmp_path / "staging").mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        material_content_enabled=enabled,
    )
    app = main_module.create_app(settings)
    with TestClient(app):
        assert calls == (["cleanup"] if enabled else [])
        assert app.state.material_stop.is_set() is False
    assert app.state.material_stop.is_set() is True


def test_conftest_turns_material_content_off_by_default():
    from meeting_workbench.config import Settings

    assert Settings().material_content_enabled is False
