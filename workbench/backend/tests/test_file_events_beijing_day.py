"""文件流水的「北京日期」：产出建议里「会后 N 天新增」拿它和会的日期比，两边必须同一套日历（北京）。

流水的 day 列是触发器按本机日历写的，时间线按本机日历翻页、文件和会议任务要落在同一天，所以 day 不动；
「会后 N 天」那里按行里的瞬时（at、修改时间）重算成北京日期（file_events.beijing_day），规则和触发器写
day 时一样。进程时区钉成太平洋、上海、UTC 各跑一遍。
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from meeting_workbench import file_events

from .test_file_events import add_file, setup, swept
from .test_produced import NOW, ROOT, asked, evidence, put_file, requirement, task, watch, world

BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_date(moment: datetime) -> date:
    return moment.astimezone(BEIJING).date()


def test_added_and_gone_events_take_the_moment_they_were_recorded():
    # 北京 2027-01-01 03:00：太平洋还是 2026-12-31
    event = {"kind": "added", "at": "2026-12-31T19:00:00.000Z", "mtime_ns": None}
    assert file_events.beijing_day(event) == date(2027, 1, 1)
    assert file_events.beijing_day({**event, "kind": "gone"}) == date(2027, 1, 1)
    # 修改时间只对 changed 有用
    far = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
    assert file_events.beijing_day({**event, "mtime_ns": far}) == date(2027, 1, 1)


def test_changed_events_take_the_modification_day_when_it_is_recent_else_the_moment():
    # 记下这条修改的那一刻是北京 2026-09-27 23:58：往前往后差几分钟就跨到不同的日子，边界才测得出来
    at = datetime(2026, 9, 27, 15, 58, tzinfo=UTC)

    def changed(mtime: datetime | None):
        ns = None if mtime is None else int(mtime.timestamp()) * 1_000_000_000
        return {"kind": "changed", "at": "2026-09-27T15:58:00.000Z", "mtime_ns": ns}

    # 一天前改的：取修改时间那天（北京 9 月 26 日 23:58）
    assert file_events.beijing_day(changed(at - timedelta(hours=24))) == date(2026, 9, 26)
    # 两天的边界：差 47 小时取修改时间（北京 9 月 26 日 00:58），差 49 小时（修改时间是 9 月 25 日 22:58）
    # 不可信，取记下的那一刻（9 月 27 日）
    assert file_events.beijing_day(changed(at - timedelta(hours=47))) == date(2026, 9, 26)
    assert file_events.beijing_day(changed(at - timedelta(hours=49))) == date(2026, 9, 27)
    # 未来 5 分钟的边界：晚 4 分钟取修改时间（北京已是 9 月 28 日 00:02），晚 10 分钟当作时钟不准，取记下的那一刻
    assert file_events.beijing_day(changed(at + timedelta(minutes=4))) == date(2026, 9, 28)
    assert file_events.beijing_day(changed(at + timedelta(minutes=10))) == date(2026, 9, 27)
    # 没有修改时间
    assert file_events.beijing_day(changed(None)) == date(2026, 9, 27)


def test_the_python_rule_matches_what_the_trigger_wrote(tmp_path, process_zone):
    """触发器写 day 的规则是 SQL，beijing_day 是它的北京日历版；上海时区下触发器写的本机日期就是北京日期，
    每一行都要和 beijing_day 对上（added、gone、再出现的 added、各种修改时间的 changed）。几个修改时间的
    边界里，两天那条能在这里测出来（差一天以上日期必然不同）；5 分钟那条只有记下的那一刻靠近午夜时才
    分得出日期，交给上面的单测。"""
    db, root = setup(tmp_path)
    swept(db, root)
    now = int(time.time())
    mtimes = {
        "recent": now - 3600,
        "yesterday": now - 86_400,
        "inside": now - 172_800 + 120,
        "outside": now - 172_800 - 120,
        "old": now - 10 * 86_400,
        "soon": now + 120,
        "future": now + 3600,
        "same-day-twice": now - 1800,
    }
    for name, mtime in mtimes.items():
        file_id = add_file(db, root, f"{name}.xlsx")
        db.execute(
            "UPDATE material_files SET size = size + 1, mtime_ns = ? WHERE id = ?",
            (mtime * 1_000_000_000, file_id),
        )
        if name == "same-day-twice":
            db.execute("UPDATE material_files SET size = size + 1 WHERE id = ?", (file_id,))
        if name == "recent":
            stamp_now = datetime.now(UTC).isoformat()
            db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (stamp_now, file_id))
            db.execute("UPDATE material_files SET gone_at = NULL WHERE id = ?", (file_id,))
    rows = db.query_all("SELECT * FROM material_file_events ORDER BY id")
    assert {row["kind"] for row in rows} == {"added", "changed", "gone"}

    for row in rows:
        at = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
        recorded = int(at.timestamp())
        modified = row["mtime_ns"] // 1_000_000_000 if row["mtime_ns"] is not None else None
        if row["kind"] == "changed" and modified is not None:
            fresh = recorded - 172_800 <= modified <= recorded + 300
            instant = datetime.fromtimestamp(modified if fresh else recorded, UTC)
        else:
            instant = at
        assert file_events.beijing_day(row) == beijing_date(instant), (process_zone, row["kind"])
        if process_zone == "Asia/Shanghai":
            assert file_events.beijing_day(row) == date.fromisoformat(row["day"]), row["kind"]


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
