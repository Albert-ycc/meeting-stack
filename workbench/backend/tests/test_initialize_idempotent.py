"""initialize 反复执行（服务每次启动、每条命令行命令）不能去动没变的数据。

关系图用 app_state 里的 graph_rev 做缓存版本号，会议表上的触发器每更新一行就加一。
以前 initialize 里有一条不带条件的 UPDATE meetings SET conflict=...，300 场会的库每启动一次
graph_rev 就加 300，备份、巡检这些命令行命令每跑一次都白白让关系图缓存失效一遍。
"""

import hashlib

from meeting_workbench.db import Database, utc_now

from .helpers import seed_editable_meeting


def _revision(db: Database, key: str) -> int:
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (key,))
    return int(row["value"]) if row else 0


def _digest(db: Database) -> str:
    """整库每张表的全部内容；initialize 要是悄悄改了哪一行，这个值就变。"""
    digest = hashlib.sha256()
    with db.autocommit() as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for row in names:
            digest.update(row["name"].encode())
            for values in connection.execute(f'SELECT * FROM "{row["name"]}"').fetchall():
                digest.update(repr(tuple(values)).encode())
    return digest.hexdigest()


def _seed_meetings(db: Database, count: int) -> None:
    now = utc_now()
    for index in range(count):
        db.execute(
            "INSERT INTO meetings(id, title, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            (f"vm-bulk-{index}", "批量会", "published", now, now),
        )


def test_initialize_twice_changes_nothing_in_a_settled_database(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    seed_editable_meeting(db, tmp_path / "archive")
    _seed_meetings(db, 300)
    db.conflicts.open("vm-bulk-1", "audio_integrity", payload={"reason": "hash_mismatch"})

    db.initialize()
    settled = _digest(db)
    graph_rev = _revision(db, "graph_rev")
    related_rev = _revision(db, "related_rev")

    db.initialize()

    assert _revision(db, "graph_rev") == graph_rev
    assert _revision(db, "related_rev") == related_rev
    assert _digest(db) == settled


def test_initialize_repairs_only_the_conflict_flags_that_are_wrong(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    _seed_meetings(db, 5)
    db.conflicts.open("vm-bulk-0", "audio_integrity", payload={})
    db.conflicts.open("vm-bulk-1", "audio_integrity", payload={})
    # 标记和冲突表对不上的两场：0 号有未处理冲突却被清成 0，2 号没有冲突却被标成 1。
    db.execute("UPDATE meetings SET conflict=0 WHERE id='vm-bulk-0'")
    db.execute("UPDATE meetings SET conflict=1 WHERE id='vm-bulk-2'")
    graph_rev = _revision(db, "graph_rev")

    db.initialize()

    flags = {
        row["id"]: row["conflict"] for row in db.query_all("SELECT id, conflict FROM meetings")
    }
    assert flags == {
        "vm-bulk-0": 1,
        "vm-bulk-1": 1,
        "vm-bulk-2": 0,
        "vm-bulk-3": 0,
        "vm-bulk-4": 0,
    }
    # 只有改了的这两行触发了版本号，另外三场没被碰
    assert _revision(db, "graph_rev") == graph_rev + 2
