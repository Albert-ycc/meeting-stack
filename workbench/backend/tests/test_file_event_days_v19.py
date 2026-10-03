"""v19：文件流水的 day 从本机日历换成北京日历。

两个触发器删掉、按北京日历重建；存量的 day 按行里记下的那一刻重算（规则和触发器一样：added、gone 取 at，
changed 的修改时间可信时取修改时间），算不出来的保持原样；本机日历里分在两天、北京日历落在同一天的 changed
并成一行（同一个文件同一天只一行）；再跑一遍什么都不变。旧库按 v18 的触发器原文建（date(…, 'localtime')），
再把版本号退回 18、重新 initialize。
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

from meeting_workbench import db as db_module
from meeting_workbench.db import SCHEMA_VERSION

from .test_file_events import add_file, setup, swept
from .test_file_events_beijing_day import expected_day, recorded_instant
from .test_stems_migration_v19 import as_v18

# v18 的两个触发器原文：day 按本机日历
V18_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS material_file_events_insert
AFTER INSERT ON material_files
WHEN (NEW.zone = 'normal' OR (NEW.zone = 'package' AND NEW.ext IN ('key', 'pages', 'numbers')))
    AND NEW.gone_at IS NULL
    AND EXISTS (SELECT 1 FROM material_index_state s
                 WHERE s.root_id = NEW.root_id AND s.last_full_at IS NOT NULL)
BEGIN
    INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                     size, mtime_ns, day, at)
    VALUES (NEW.root_id, NEW.id, NEW.rel_path, NEW.dir_rel, 'added', NULL, NEW.size, NEW.mtime_ns,
            date('now', 'localtime'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END""",
    """CREATE TRIGGER IF NOT EXISTS material_file_events_update
AFTER UPDATE OF size, mtime_ns, gone_at ON material_files
WHEN (NEW.zone = 'normal' OR (NEW.zone = 'package' AND NEW.ext IN ('key', 'pages', 'numbers')))
    AND (NEW.size IS NOT OLD.size OR NEW.mtime_ns IS NOT OLD.mtime_ns
         OR NEW.gone_at IS NOT OLD.gone_at)
    AND EXISTS (SELECT 1 FROM material_index_state s
                 WHERE s.root_id = NEW.root_id AND s.last_full_at IS NOT NULL)
BEGIN
    INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                     size, mtime_ns, day, at)
    SELECT NEW.root_id, NEW.id, NEW.rel_path, NEW.dir_rel,
           CASE WHEN NEW.gone_at IS NOT NULL THEN 'gone' ELSE 'added' END,
           CASE WHEN NEW.gone_at IS NOT NULL THEN OLD.content_key END,
           CASE WHEN NEW.gone_at IS NOT NULL THEN OLD.size ELSE NEW.size END,
           CASE WHEN NEW.gone_at IS NOT NULL THEN OLD.mtime_ns ELSE NEW.mtime_ns END,
           date('now', 'localtime'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE (OLD.gone_at IS NULL) != (NEW.gone_at IS NULL);
    INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                     size, mtime_ns, day, at)
    SELECT NEW.root_id, NEW.id, NEW.rel_path, NEW.dir_rel, 'changed', OLD.content_key,
           NEW.size, NEW.mtime_ns,
           CASE WHEN NEW.mtime_ns IS NOT NULL
                 AND NEW.mtime_ns / 1000000000
                     BETWEEN CAST(strftime('%s', 'now') AS INTEGER) - 172800
                         AND CAST(strftime('%s', 'now') AS INTEGER) + 300
                THEN date(NEW.mtime_ns / 1000000000, 'unixepoch', 'localtime')
                ELSE date('now', 'localtime') END,
           strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
     WHERE OLD.gone_at IS NULL AND NEW.gone_at IS NULL
       AND NOT (NEW.size IS OLD.size AND NEW.mtime_ns IS NOT NULL AND OLD.mtime_ns IS NOT NULL
                AND (NEW.mtime_ns - OLD.mtime_ns) % 900000000000 = 0)
    ON CONFLICT(file_id, day) WHERE kind = 'changed'
    DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, at = excluded.at;
END""",
)


def old_database(tmp_path):
    """装上 v18 触发器的库（根目录已扫完过一整轮）：db 和 root_id。"""
    db, root = setup(tmp_path)
    with db.autocommit() as connection:
        connection.execute("DROP TRIGGER material_file_events_insert")
        connection.execute("DROP TRIGGER material_file_events_update")
        for trigger in V18_TRIGGERS:
            connection.execute(trigger)
    swept(db, root)
    return db, root


def local_day(row) -> date:
    """v18 触发器写的 day：同一个瞬时，本机日历。"""
    return recorded_instant(row).astimezone().date()


def raw(db, file_id, kind, day, at, *, mtime=None, size=10, content_key=None):
    """直接写一行流水（v18 时按本机日历写下的样子）。"""
    db.execute(
        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                            size, mtime_ns, day, at)
           VALUES (1, ?, ?, '', ?, ?, ?, ?, ?, ?)""",
        (
            file_id,
            f"文件{file_id}.xlsx",
            kind,
            content_key,
            size,
            None if mtime is None else int(mtime.timestamp()) * 1_000_000_000,
            day,
            at,
        ),
    )
    return db.query_one("SELECT id FROM material_file_events ORDER BY id DESC LIMIT 1")["id"]


def days(db) -> dict[int, str]:
    return {
        row["id"]: row["day"] for row in db.query_all("SELECT id, day FROM material_file_events")
    }


def triggers(db) -> dict[str, str]:
    return {
        row["name"]: row["sql"]
        for row in db.query_all(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'material_file_events_%'"
        )
    }


def test_old_triggers_are_replaced_and_stored_days_move_to_beijing(tmp_path, process_zone):
    """旧触发器在这个进程时区下按本机日历写了一批流水（修改时间铺满过去一天的每个整点，总有几行本机和北京
    不是同一天）；迁移以后每一行都是北京日期，新记的也是。"""
    db, root = old_database(tmp_path)
    now = int(time.time())
    for hours in range(25):
        file_id = add_file(db, root, f"h{hours}.xlsx")
        db.execute(
            "UPDATE material_files SET size = size + 1, mtime_ns = ? WHERE id = ?",
            ((now - hours * 3600) * 1_000_000_000, file_id),
        )
    back = add_file(db, root, "back.xlsx")
    db.execute(
        "UPDATE material_files SET gone_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), back)
    )
    db.execute("UPDATE material_files SET gone_at = NULL WHERE id = ?", (back,))
    before = db.query_all("SELECT * FROM material_file_events ORDER BY id")
    assert {row["kind"] for row in before} == {"added", "changed", "gone"}
    assert all(date.fromisoformat(row["day"]) == local_day(row) for row in before)
    as_v18(db)

    db.initialize()

    assert db.user_version() == SCHEMA_VERSION == 19
    assert set(triggers(db)) == {"material_file_events_insert", "material_file_events_update"}
    for sql in triggers(db).values():
        assert "localtime" not in sql and "'+8 hours'" in sql
    after = db.query_all("SELECT * FROM material_file_events ORDER BY id")
    assert [row["id"] for row in after] == [row["id"] for row in before]
    for row in after:
        assert date.fromisoformat(row["day"]) == expected_day(row), (row["kind"], row["rel_path"])
    fresh = add_file(db, root, "迁移以后.xlsx")
    (row,) = db.query_all("SELECT * FROM material_file_events WHERE file_id = ?", (fresh,))
    assert date.fromisoformat(row["day"]) == expected_day(row)


def test_stored_days_are_recomputed_from_the_recorded_moment(tmp_path, process_zone):
    """手写的旧行（太平洋时区下写的本机日期）：元旦凌晨记下的 added、gone，changed 的两天和 5 分钟两条边界
    （记下的那一刻是北京 9 月 27 日 23:58，差几分钟日子才不同），没有修改时间的，记下的时刻解析不了的。"""
    db, _root = setup(tmp_path)
    new_year = "2026-12-31T19:00:00.000Z"  # 北京 2027-01-01 03:00；太平洋还是 12 月 31 日
    at = datetime(2026, 9, 27, 15, 58, tzinfo=UTC)
    stamp = "2026-09-27T15:58:00.000Z"
    rows = {
        raw(db, 1, "added", "2026-12-31", new_year): "2027-01-01",
        raw(db, 2, "gone", "2026-12-31", new_year, mtime=at - timedelta(days=30)): "2027-01-01",
        # 一天前改的：修改时间那天（北京 9 月 26 日 23:58，太平洋也是 26 日）
        raw(db, 3, "changed", "2026-09-26", stamp, mtime=at - timedelta(hours=24)): "2026-09-26",
        # 差 47 小时：取修改时间（北京 9 月 26 日 00:58，太平洋 25 日）
        raw(db, 4, "changed", "2026-09-25", stamp, mtime=at - timedelta(hours=47)): "2026-09-26",
        # 差 49 小时：修改时间不可信，取记下的那一刻
        raw(db, 5, "changed", "2026-09-27", stamp, mtime=at - timedelta(hours=49)): "2026-09-27",
        # 晚 4 分钟：取修改时间（北京已是 9 月 28 日 00:02）；晚 10 分钟：当作时钟不准，取记下的那一刻
        raw(db, 6, "changed", "2026-09-27", stamp, mtime=at + timedelta(minutes=4)): "2026-09-28",
        raw(db, 7, "changed", "2026-09-27", stamp, mtime=at + timedelta(minutes=10)): "2026-09-27",
        raw(db, 8, "changed", "2026-09-27", stamp): "2026-09-27",
        # 算不出来：保持原样
        raw(db, 9, "added", "2026-09-01", "不是时间"): "2026-09-01",
    }
    as_v18(db)

    db.initialize()

    assert days(db) == rows


def test_changed_rows_landing_on_the_same_beijing_day_are_merged(tmp_path):
    """同一个文件北京 9 月 30 日 14:51 和 16:06 各改了一次：太平洋分在 29、30 两天两行，北京是同一天，并成
    一行——留先记的那行和它的旧标识（当天第一次修改前的），大小、修改时间、记下的那一刻取最后一次的。
    另一个文件两行往后挪一天（28→29、29→30）：不并，前一行先挪也不撞唯一索引。"""
    db, _root = setup(tmp_path)
    first = raw(
        db,
        613,
        "changed",
        "2026-09-29",
        "2026-09-30T06:51:11.405Z",
        mtime=datetime(2026, 9, 30, 6, 50, 49, tzinfo=UTC),
        size=10074,
    )
    added = raw(db, 613, "added", "2026-09-29", "2026-09-30T06:40:00.000Z")
    second = raw(
        db,
        613,
        "changed",
        "2026-09-30",
        "2026-09-30T08:06:34.903Z",
        mtime=datetime(2026, 9, 30, 8, 5, 44, tzinfo=UTC),
        size=13267,
        content_key="q2:second",
    )
    chain = [
        raw(db, 7, "changed", "2026-09-28", "2026-09-28T20:00:00.000Z"),
        raw(db, 7, "changed", "2026-09-29", "2026-09-29T20:00:00.000Z"),
    ]

    with db.autocommit() as connection:
        stats = db_module._rebase_file_event_days(connection)

    assert stats == {"days": 4, "merged": 1}
    assert days(db) == {
        first: "2026-09-30",
        added: "2026-09-30",
        chain[0]: "2026-09-29",
        chain[1]: "2026-09-30",
    }
    merged = db.query_one("SELECT * FROM material_file_events WHERE id = ?", (first,))
    assert (merged["content_key"], merged["size"], merged["at"]) == (
        None,
        13267,
        "2026-09-30T08:06:34.903Z",
    )
    assert (
        merged["mtime_ns"] == int(datetime(2026, 9, 30, 8, 5, 44, tzinfo=UTC).timestamp()) * 10**9
    )
    assert second not in days(db)


def test_running_it_again_changes_nothing(tmp_path):
    db, root = old_database(tmp_path)
    for index in range(5):
        file_id = add_file(db, root, f"文件{index}.xlsx")
        db.execute("UPDATE material_files SET size = size + 1 WHERE id = ?", (file_id,))
    raw(db, 99, "changed", "2026-09-29", "2026-09-30T06:51:11.405Z")
    as_v18(db)
    db.initialize()
    rows = db.query_all("SELECT * FROM material_file_events ORDER BY id")
    schema = db.query_all("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")

    with db.autocommit() as connection:
        stats = db_module._rebase_file_event_days(connection)
    db.initialize()

    assert stats == {"days": 0, "merged": 0}
    assert db.query_all("SELECT * FROM material_file_events ORDER BY id") == rows
    assert db.query_all("SELECT type, name, sql FROM sqlite_master ORDER BY type, name") == schema
