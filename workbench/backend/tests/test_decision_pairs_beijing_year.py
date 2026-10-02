"""决议对比提示词里的日期标签：「今年」和每场会的日期都按北京日历。

同一年的会不写年份、别的年份写「2026年12月30日」。「今年」原来是 (now).astimezone().year，本机时区的年份：
北京元旦 0～16 点，太平洋时区还是 12 月 31 日，今年被算成去年，元旦当天的会反而写出年份，去年的会不写年份。
会的日期也要用同一套日历比（北京的 1 月 1 日 01:00 在太平洋是 12 月 31 日 09:00），两边必须同一套。
进程时区钉成太平洋、上海、UTC 各跑一遍。
"""

from datetime import UTC, datetime

from meeting_workbench import decision_pairs

from .test_decision_pairs import NEW_TEXT, OLD_TEXT, ingest, make, meeting

# 北京 2027-01-01 03:00，太平洋 2026-12-31 11:00，UTC 2026-12-31 19:00
NEW_YEAR_MORNING = datetime(2026, 12, 31, 19, 0, tzinfo=UTC)


def world(tmp_path, *, recorded_prev, recorded_cur):
    db = make(tmp_path)
    meeting(db, "prev", OLD_TEXT, ago=1, title="周会甲")
    meeting(db, "cur", NEW_TEXT, ago=1, title="周会乙")
    db.execute("UPDATE meetings SET recording_date=? WHERE id='prev'", (recorded_prev,))
    db.execute("UPDATE meetings SET recording_date=? WHERE id='cur'", (recorded_cur,))
    ingest(db, now=NEW_YEAR_MORNING)
    db.execute("UPDATE decision_scan SET pair_state='done' WHERE meeting_id='prev'")
    return db


def test_year_is_the_beijing_year_on_new_years_morning(tmp_path, process_zone):
    db = world(
        tmp_path,
        recorded_prev="2026-12-30T10:00:00+08:00",
        recorded_cur="2027-01-01T01:00:00+08:00",
    )

    with db.autocommit() as connection:
        plan = decision_pairs.build_plan(connection, "cur", now=NEW_YEAR_MORNING)

    user = plan.user_message()
    # 「今年」是北京的 2027：元旦这场会不写年份，去年 12 月 30 日的写年份
    assert "n1 [1月1日 周会乙]" in user
    assert "e1 [2026年12月30日 周会甲]" in user


def test_a_meeting_on_the_last_day_of_the_old_year_in_beijing_keeps_its_year(
    tmp_path, process_zone
):
    db = world(
        tmp_path,
        recorded_prev="2026-12-31T23:30:00+08:00",
        recorded_cur="2027-01-01T00:30:00+08:00",
    )

    with db.autocommit() as connection:
        plan = decision_pairs.build_plan(connection, "cur", now=NEW_YEAR_MORNING)

    user = plan.user_message()
    assert "n1 [1月1日 周会乙]" in user
    assert "e1 [2026年12月31日 周会甲]" in user


def test_the_day_label_is_the_beijing_date_of_the_recording(tmp_path, process_zone):
    # 同一年里：太平洋 9 月 25 日 19:00 录的会，北京是 9 月 26 日，提示词里写 9 月 26 日
    now = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
    db = make(tmp_path)
    meeting(db, "prev", OLD_TEXT, ago=1, title="周会甲")
    meeting(db, "cur", NEW_TEXT, ago=1, title="周会乙")
    db.execute("UPDATE meetings SET recording_date='2026-09-25T19:00:00-07:00' WHERE id='prev'")
    db.execute("UPDATE meetings SET recording_date='2026-09-26T20:00:00-07:00' WHERE id='cur'")
    ingest(db, now=now)
    db.execute("UPDATE decision_scan SET pair_state='done' WHERE meeting_id='prev'")

    with db.autocommit() as connection:
        plan = decision_pairs.build_plan(connection, "cur", now=now)

    user = plan.user_message()
    assert "n1 [9月27日 周会乙]" in user
    assert "e1 [9月26日 周会甲]" in user
