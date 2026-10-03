"""v19 迁移：词干规则补了结尾的 (v2)、第三版、终稿，库里存的词干、会上提到、关联里的 stem_key 一起重算。

旧库用 v18 的规则原文建出来（真的按旧规则收文件名），再把版本号退回 18、重新 initialize。
"""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from meeting_workbench import db as db_module
from meeting_workbench import file_stems
from meeting_workbench.db import SCHEMA_VERSION, utc_now
from meeting_workbench.material_index import MaterialIndexer

from .test_file_mentions import add_file, setup
from .test_graph import add_meeting
from .test_material_index import add_root, make, run_until_done, write

_SEP = r"[\s_\-.·]*"
# v18 的 _TRAILING 原文
_OLD_TRAILING = re.compile(
    _SEP
    + r"(?:"
    + r"\(\d{1,3}\)"
    + r"|副本(?:\s*\d{1,3})?"
    + r"|(?<![a-z])copy(?:\s*\d{1,3})?"
    + r"|(?<![a-z])v\d{1,3}(?:\.\d{1,3}){0,2}"
    + r"|最终版|终版|定稿|修改版|修订版"
    + r"|(?<![a-z])final"
    + r")$",
    re.IGNORECASE,
)

BASE = "报价单.xlsx"
V2 = "报价单 (v2).xlsx"
THIRD = "报价单 第三版.docx"
FINAL = "报价单（终稿）.xlsx"
GONE = "归档/报价单 (v1).xlsx"
OTHER = "云图AI项目周报.pptx"


def as_v18(db):
    connection = sqlite3.connect(db.path)
    connection.execute("PRAGMA user_version=18")
    connection.commit()
    connection.close()


def stems(db):
    return {
        row["rel_path"]: (row["stem"], row["stem_key"])
        for row in db.query_all("SELECT rel_path, stem, stem_key FROM material_files")
    }


def old_database(tmp_path, monkeypatch):
    """按 v18 的规则收好一批文件名的库：db、root_id 和 {rel_path: file_id}。"""
    db, root_id = setup(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(file_stems, "_TRAILING", _OLD_TRAILING)
        ids = {name: add_file(db, root_id, name) for name in (BASE, V2, THIRD, FINAL, OTHER)}
        ids[GONE] = add_file(db, root_id, GONE, gone=True)
    return db, root_id, ids


def test_migration_regroups_stored_stems_and_leaves_the_rest(tmp_path, monkeypatch):
    db, _root_id, _ids = old_database(tmp_path, monkeypatch)
    before = stems(db)
    # 旧规则下带版本的几份各是一组，和「报价单」连不上
    assert {before[name][1] for name in (BASE, V2, THIRD, FINAL, GONE)} == {
        "报价单",
        "报价单v2",
        "报价单第三版",
        "报价单终稿",
        "报价单v1",
    }
    as_v18(db)

    db.initialize()

    after = stems(db)
    for name in (BASE, V2, THIRD, FINAL, GONE):
        assert after[name] == ("报价单", "报价单")
    assert after[OTHER] == before[OTHER] == ("云图AI项目周报", "云图ai项目周报")
    assert db.user_version() == SCHEMA_VERSION == 19


def test_words_the_old_rule_cut_in_half_are_regrouped(tmp_path, monkeypatch):
    """旧规则只认「定稿」，「审定稿」被削成「审」、「最终定稿」被削成「最终」，「送审稿」不认，都和原文档分成了
    别的组；迁移按整个词重算，一起并回「考核办法」。"""
    db, root_id = setup(tmp_path)
    names = ("考核办法.docx", "考核办法审定稿.docx", "考核办法最终定稿.docx", "考核办法送审稿.docx")
    with monkeypatch.context() as patch:
        patch.setattr(file_stems, "_TRAILING", _OLD_TRAILING)
        for name in names:
            add_file(db, root_id, name)
    before = stems(db)
    assert {before[name][1] for name in names} == {
        "考核办法",
        "考核办法审",
        "考核办法最终",
        "考核办法送审稿",
    }
    as_v18(db)

    db.initialize()

    after = stems(db)
    assert {after[name] for name in names} == {("考核办法", "考核办法")}


def test_running_it_again_changes_nothing(tmp_path, monkeypatch):
    db, _root_id, _ids = old_database(tmp_path, monkeypatch)
    as_v18(db)
    db.initialize()
    first = db.query_all("SELECT * FROM material_files ORDER BY id")

    db.initialize()
    with db.autocommit() as connection:
        stats = db_module._refresh_stems(connection)

    assert db.query_all("SELECT * FROM material_files ORDER BY id") == first
    assert stats == {"files": 0, "mentions": 0, "mentions_left": 0, "relations": 0}


def add_mention(db, meeting_id, key, file_id, *, status="active", picked=0, needle="n"):
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle,
               count, anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES (?, 'p', ?, ?, ?, 1, '[]', 0, 'transcript', ?, ?, ?)""",
        (meeting_id, key, file_id, needle, status, picked, utc_now()),
    )


def mention_rows(db, meeting_id):
    return {
        row["stem_key"]: (row["file_id"], row["status"], row["picked"])
        for row in db.query_all(
            "SELECT stem_key, file_id, status, picked FROM meeting_file_mentions WHERE meeting_id=?",
            (meeting_id,),
        )
    }


def test_mentions_follow_their_files_and_keep_what_the_user_decided(tmp_path, monkeypatch):
    db, _root_id, ids = old_database(tmp_path, monkeypatch)
    for meeting_id in ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8"):
        add_meeting(db, meeting_id, ago=1, project_id="p")
    base, v2, third, final = ids[BASE], ids[V2], ids[THIRD], ids[FINAL]
    # 各搬到「报价单」：系统写的、你说过不是这份的、你手动换过的
    add_mention(db, "m1", "报价单v2", v2)
    add_mention(db, "m2", "报价单第三版", third, status="rejected")
    add_mention(db, "m3", "报价单终稿", final, picked=1)
    # 新键上已经有系统写的行：你表过态的那一行替掉它
    add_mention(db, "m4", "报价单", base)
    add_mention(db, "m4", "报价单v2", v2, status="rejected")
    # 两边都是你表过态的：旧键上的这一行留在原处，不起作用
    add_mention(db, "m5", "报价单", base, status="rejected")
    add_mention(db, "m5", "报价单v2", v2, status="rejected")
    # 系统写的行搬到别的系统行上：不搬，下一轮比对会按新词干重新写
    add_mention(db, "m6", "报价单", base)
    add_mention(db, "m6", "报价单v2", v2)
    # 没连着文件的行、键和文件对不上的行不动
    add_mention(db, "m7", "报价单v2", None, status="rejected")
    add_mention(db, "m8", "别的词", v2)
    as_v18(db)

    db.initialize()

    assert mention_rows(db, "m1") == {"报价单": (v2, "active", 0)}
    assert mention_rows(db, "m2") == {"报价单": (third, "rejected", 0)}
    assert mention_rows(db, "m3") == {"报价单": (final, "active", 1)}
    assert mention_rows(db, "m4") == {"报价单": (v2, "rejected", 0)}
    assert mention_rows(db, "m5") == {
        "报价单": (base, "rejected", 0),
        "报价单v2": (v2, "rejected", 0),
    }
    assert mention_rows(db, "m6") == {"报价单": (base, "active", 0), "报价单v2": (v2, "active", 0)}
    assert mention_rows(db, "m7") == {"报价单v2": (None, "rejected", 0)}
    assert mention_rows(db, "m8") == {"别的词": (v2, "active", 0)}


def add_relation(
    db, kind, ident, key, *, file_id=None, root_id=None, rel_path=None, status="shown"
):
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, root_id, rel_path,
               stem_key, file_id, created_at, updated_at)
           VALUES (?, 'p', ?, ?, 'llm', 'm1', ?, ?, ?, ?, ?, ?)""",
        (kind, ident, status, root_id, rel_path, key, file_id, utc_now(), utc_now()),
    )


def relation_keys(db):
    return {
        row["ident"]: row["stem_key"]
        for row in db.query_all("SELECT ident, stem_key FROM relations")
    }


def test_relations_move_their_stem_key_but_not_their_ident(tmp_path, monkeypatch):
    db, root_id, ids = old_database(tmp_path, monkeypatch)
    add_meeting(db, "m1", ago=1, project_id="p")
    add_relation(db, "mention", "m1|报价单v2", "报价单v2", file_id=ids[V2])
    add_relation(
        db, "mention", "m1|报价单终稿", "报价单终稿", file_id=ids[FINAL], status="rejected"
    )
    # 文件行的 id 没记下来，靠 (root_id, rel_path) 找到文件
    add_relation(db, "affects", "a1", "报价单第三版", root_id=root_id, rel_path=THIRD)
    # 键和文件对不上、或者没有词干：不动
    add_relation(db, "affects", "a2", "别的词", file_id=ids[V2])
    add_relation(db, "related", "r1", None, file_id=ids[V2])
    as_v18(db)

    db.initialize()

    assert relation_keys(db) == {
        "m1|报价单v2": "报价单",
        "m1|报价单终稿": "报价单",
        "a1": "报价单",
        "a2": "别的词",
        "r1": None,
    }


def test_migration_leaves_the_index_digest_for_the_next_pass_to_notice(tmp_path, monkeypatch):
    """迁移不碰 stems_hash：下一轮扫完时按新词干算出的摘要对不上，stems_rev 自己加一，各会重新比对；
    迁移算出来的词干和文件名索引自己算的完全一致（整轮重读一遍，文件行一行都不用改）。"""
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    for name in (BASE, V2, THIRD, FINAL):
        write(root / name)
    add_root(db, root)
    with monkeypatch.context() as patch:
        patch.setattr(file_stems, "_TRAILING", _OLD_TRAILING)
        run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    old_state = db.query_one("SELECT stems_rev, stems_hash FROM material_index_state")
    assert old_state["stems_rev"] == 1
    assert {row["stem_key"] for row in db.query_all("SELECT stem_key FROM material_files")} == {
        "报价单",
        "报价单v2",
        "报价单第三版",
        "报价单终稿",
    }
    as_v18(db)

    db.initialize()

    assert {row["stem_key"] for row in db.query_all("SELECT stem_key FROM material_files")} == {
        "报价单"
    }
    state = db.query_one("SELECT stems_rev, stems_hash FROM material_index_state")
    assert (state["stems_rev"], state["stems_hash"]) == (1, old_state["stems_hash"])
    # 过了 24 小时的整轮重读：每个目录都重新列一遍，词干没变的文件行不写（seen_at 不变）
    before = db.query_all("SELECT * FROM material_files ORDER BY id")
    later = datetime.now(UTC) + timedelta(hours=25)
    MaterialIndexer(db, settings, clock=lambda: 0.0, now=lambda: later).run_round()
    assert db.query_all("SELECT * FROM material_files ORDER BY id") == before
    state = db.query_one("SELECT stems_rev, stems_hash FROM material_index_state")
    assert state["stems_rev"] == 2 and state["stems_hash"] != old_state["stems_hash"]
