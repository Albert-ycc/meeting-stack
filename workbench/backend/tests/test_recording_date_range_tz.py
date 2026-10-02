"""会议日期筛选（recording_date_range）按瞬时换算成本机日历日期再比，和列表上显示的日期同一套。

库里的录音日期混着 -07:00 和 +00:00（后者带小数秒）两种偏移，原来取前 10 位比，前 10 位不是同一套日历：
2026-05-20T06:06:31.96+00:00 在太平洋时区是 5 月 19 日 23 点，列表上显示 5/19，筛选却把它算在 5/20。
进程时区钉成太平洋、上海、UTC 各跑一遍；每个值该落在哪天，用 zoneinfo 独立算一遍对照，不是抄 SQLite 的结果。
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from meeting_workbench.tasks import recording_date_range

ZONES = ("America/Los_Angeles", "Asia/Shanghai", "UTC")


@pytest.fixture(params=ZONES)
def process_zone(request):
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


# 生产库里的两种写法（-07:00、带小数秒的 +00:00）加上跨日边界、结束日当天、没带时区的值
VALUES = {
    "pt-evening": "2026-09-30T23:30:00-07:00",
    "utc-just-after-midnight": "2026-10-01T00:10:00+00:00",
    "utc-fractional": "2026-05-20T06:06:31.960000+00:00",
    "utc-fractional-late": "2026-05-20T23:59:59.999999+00:00",
    "bj-morning": "2026-09-30T10:00:00+08:00",
    "bj-last-second": "2026-09-30T23:59:59+08:00",
    "bj-midnight": "2026-10-01T00:00:00+08:00",
    "pt-midnight": "2026-09-30T00:00:00-07:00",
    "pt-last-second": "2026-09-30T23:59:59-07:00",
    "zulu": "2026-09-30T20:00:00Z",
    # 没带时区的是本机时间（graph.local_day 同一规矩）：日期就是它自己的日期，不换算
    "naive": "2026-09-30T10:00:00",
    "date-only": "2026-09-30",
}


def expected_day(value: str, zone: str) -> date:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(ZoneInfo(zone)).date()


@pytest.fixture
def table():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE m(id TEXT PRIMARY KEY, recording_date TEXT)")
    connection.executemany("INSERT INTO m VALUES (?, ?)", list(VALUES.items()))
    connection.execute("INSERT INTO m VALUES ('no-date', NULL)")
    yield connection
    connection.close()


def ids(connection, date_from=None, date_to=None):
    clauses, params = recording_date_range("m.recording_date", date_from, date_to)
    where = " AND ".join(clauses) or "1"
    rows = connection.execute(f"SELECT m.id FROM m WHERE {where}", params).fetchall()
    return {row[0] for row in rows}


def days_in_play(zone):
    return sorted({expected_day(value, zone) for value in VALUES.values()})


def test_single_day_matches_the_local_calendar_day_of_each_instant(table, process_zone):
    for day in days_in_play(process_zone):
        wanted = {key for key, value in VALUES.items() if expected_day(value, process_zone) == day}
        assert ids(table, day.isoformat(), day.isoformat()) == wanted, (process_zone, day)


def test_the_end_day_is_included_and_the_next_day_is_not(table, process_zone):
    last = date(2026, 9, 30)
    through_last = {
        key for key, value in VALUES.items() if expected_day(value, process_zone) <= last
    }
    assert ids(table, None, last.isoformat()) == through_last
    from_next = {key for key, value in VALUES.items() if expected_day(value, process_zone) > last}
    assert ids(table, "2026-10-01", None) == from_next
    # 起止两头都含：从 5/20 到 9/30
    inside = {
        key
        for key, value in VALUES.items()
        if date(2026, 5, 20) <= expected_day(value, process_zone) <= last
    }
    assert ids(table, "2026-05-20", "2026-09-30") == inside


def test_fractional_seconds_and_plus_zero_offsets_are_converted_not_cut_at_ten_chars(
    table, process_zone
):
    # 5/20 06:06:31.96 UTC：太平洋是 5/19 23 点，上海和 UTC 是 5/20
    on_19 = ids(table, "2026-05-19", "2026-05-19")
    on_20 = ids(table, "2026-05-20", "2026-05-20")
    if process_zone == "America/Los_Angeles":
        assert "utc-fractional" in on_19 and "utc-fractional" not in on_20
    else:
        assert "utc-fractional" in on_20 and "utc-fractional" not in on_19
    # 5/20 23:59:59.999999 UTC：上海已经是 5/21
    on_21 = ids(table, "2026-05-21", "2026-05-21")
    assert ("utc-fractional-late" in on_21) == (process_zone == "Asia/Shanghai")


def test_no_filter_returns_everything_and_a_filter_leaves_out_meetings_without_a_date(
    table, process_zone
):
    assert ids(table) == set(VALUES) | {"no-date"}
    assert "no-date" not in ids(table, "2000-01-01", None)
    assert "no-date" not in ids(table, None, "2099-12-31")


def test_the_clause_is_one_comparison_per_given_end():
    assert recording_date_range("m.recording_date", None, None) == ([], [])
    clauses, params = recording_date_range("m.recording_date", "2026-09-01", "2026-09-30")
    assert len(clauses) == 2 and params == ["2026-09-01", "2026-09-30"]
