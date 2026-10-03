"""文件流水的 day 是北京日历的日期（v19 起）：触发器不跟服务进程的时区走，时间线按它分天、翻页，产出建议里
「会后 N 天新增」拿它直接和会的日期（北京日历）比。进程时区钉成太平洋、上海、UTC 各跑一遍。
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .test_file_events import add_file, setup, swept
from .test_produced import NOW, ROOT, asked, evidence, put_file, requirement, task, watch, world

BEIJING = ZoneInfo("Asia/Shanghai")


def recorded_instant(row) -> datetime:
    """这条流水的 day 取哪一刻：added、gone 取记下它的那一刻（at）；changed 的修改时间落在「那一刻前 2 天到
    后 5 分钟」之间时取修改时间，否则也取那一刻。"""
    at = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
    recorded = int(at.timestamp())
    if row["kind"] == "changed" and row["mtime_ns"] is not None:
        modified = row["mtime_ns"] // 1_000_000_000
        if recorded - 172_800 <= modified <= recorded + 300:
            return datetime.fromtimestamp(modified, UTC)
    return at


def expected_day(row) -> date:
    """触发器该写的 day，按 zoneinfo 另算一遍。"""
    return recorded_instant(row).astimezone(BEIJING).date()


def test_the_trigger_writes_the_beijing_day_in_every_process_zone(tmp_path, process_zone):
    """added、gone、又出现的 added、各种修改时间的 changed（同一天改两次的只一行）：每一行的 day 都是北京日历上
    的那一天。修改时间里有过去一天每个整点的：不管几点跑，总有几个落在「北京已是第二天、太平洋和 UTC 还是
    前一天」的时段，按本机日历写的话这里必红。"""
    db, root = setup(tmp_path)
    swept(db, root)
    now = int(time.time())
    mtimes = {
        "inside": now - 172_800 + 120,
        "outside": now - 172_800 - 120,
        "old": now - 10 * 86_400,
        "soon": now + 120,
        "future": now + 3600,
        "same-day-twice": now - 1800,
        **{f"hour-{hours}": now - hours * 3600 for hours in range(1, 25)},
    }
    for name, mtime in mtimes.items():
        file_id = add_file(db, root, f"{name}.xlsx")
        db.execute(
            "UPDATE material_files SET size = size + 1, mtime_ns = ? WHERE id = ?",
            (mtime * 1_000_000_000, file_id),
        )
        if name == "same-day-twice":
            db.execute("UPDATE material_files SET size = size + 1 WHERE id = ?", (file_id,))
    back = add_file(db, root, "back.xlsx")
    db.execute(
        "UPDATE material_files SET gone_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), back)
    )
    db.execute("UPDATE material_files SET gone_at = NULL WHERE id = ?", (back,))
    rows = db.query_all("SELECT * FROM material_file_events ORDER BY id")
    assert {row["kind"] for row in rows} == {"added", "changed", "gone"}
    assert len([row for row in rows if row["rel_path"] == "same-day-twice.xlsx"]) == 2

    for row in rows:
        assert date.fromisoformat(row["day"]) == expected_day(row), (row["kind"], row["rel_path"])


def test_days_after_the_meeting_count_in_the_beijing_calendar(tmp_path, process_zone):
    """会录于北京 9 月 23 日 10:00（太平洋是 9 月 22 日 19:00），文件北京 9 月 26 日 18:00 出现
    （太平洋是 9 月 26 日 03:00）：北京日历是会后 3 天，两边都按太平洋算是 4 天。"""
    w = world(tmp_path)
    w.db.execute(
        """INSERT INTO meetings(id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES ('m', '会 m', '2026-09-23T10:00:00+08:00', 'completed_unreviewed', 'p', 'x', 'x')"""
    )
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    task(w.db, "t", "写方案", meeting_id="m", requirement_id="r")
    put_file(w.db, w.root, "能耗看板/方案.docx", datetime(2026, 9, 26, 10, 0, tzinfo=UTC))
    watch(w.db)

    [row] = asked(w.db)

    assert evidence(row)["ref"] == "meeting"
    assert evidence(row)["event_day"] == "2026-09-26"
    assert evidence(row)["days"] == 3


def test_days_after_the_confirmation_count_in_the_beijing_calendar(tmp_path, process_zone):
    """没有来源会议时拿任务确认的那一刻比：北京 9 月 23 日 10:00 确认，9 月 26 日 18:00 出现文件。"""
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    confirmed = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)
    task(
        w.db,
        "t",
        "写方案",
        requirement_id="r",
        events=(("confirmed", "任务已确认", (confirmed - NOW) / timedelta(days=1)),),
    )
    put_file(w.db, w.root, "能耗看板/方案.docx", datetime(2026, 9, 26, 10, 0, tzinfo=UTC))
    watch(w.db)

    [row] = asked(w.db)

    assert evidence(row)["ref"] == "confirm"
    assert evidence(row)["event_day"] == "2026-09-26"
    assert evidence(row)["days"] == 3
