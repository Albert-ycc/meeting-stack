"""项目时间线按北京日历：分组、翻页和记录开始前的窗口、每条显示的日期和时刻，都不随服务进程的时区变。

同一份数据在进程时区钉成太平洋、上海、UTC 时各跑一遍（process_zone），分出来的日子、标题、时刻必须一样。
生产的 Mac 在太平洋，会在北京白天开：太平洋前一天晚上就是北京的第二天。
"""

from datetime import UTC, date, datetime

from meeting_workbench import decisions

from .test_decisions import set_minutes
from .test_timeline import (
    add_file,
    build,
    event,
    finish_first_pass,
    items,
    make,
    meeting,
    root,
    task,
)

# 北京 2026-10-01 01:00（周四）；太平洋还是 9 月 30 日 10:00，UTC 9 月 30 日 17:00
NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)


def page(db, **kwargs):
    return build(db, now=NOW, **kwargs)


def shape(result):
    """每一天：(日子, 标题, [(类型, 时刻)])。"""
    return [
        (entry["day"], entry["label"], [(item["type"], item["time"]) for item in entry["items"]])
        for entry in result["days"]
    ]


def test_beijing_midnight_splits_the_days(tmp_path, process_zone):
    """北京 23:59 确认、00:01 完成的任务，23:30 和 00:30 开的会：在北京日历上分属两天。"""
    db = make(tmp_path)
    meeting(db, "late", "2026-09-30T23:30:00+08:00")
    meeting(db, "early", "2026-10-01T00:30:00+08:00")
    task(db, "a", "confirmed", events=[("confirmed", "", "2026-09-30T15:59:00+00:00")])
    task(
        db,
        "b",
        "done",
        events=[("status_changed", "confirmed → done", "2026-09-30T16:01:00+00:00")],
    )

    assert shape(page(db)) == [
        ("2026-10-01", "今天", [("tasks", "00:01"), ("meeting", "00:30")]),
        ("2026-09-30", "昨天", [("meeting", "23:30"), ("tasks", "23:59")]),
    ]


def test_a_pacific_evening_is_the_next_beijing_day(tmp_path, process_zone):
    """太平洋 9 月 29 日 19:00 录的会（带 -07:00）是北京 9 月 30 日 10:00；交付物、文件同理按北京日期。"""
    db = make(tmp_path)
    meeting(db, "m", "2026-09-29T19:00:00-07:00")
    task(db, "t", "confirmed", events=[("confirmed", "", "2026-09-28T01:00:00+00:00")])
    db.execute(
        """INSERT INTO deliverables(task_id, kind, url, title, created_at)
           VALUES ('t', 'file', '/盘/能耗看板/报价单_v3.xlsx', '', '2026-09-29T20:10:00+00:00')"""
    )
    root_id = root(db)
    path = "能耗看板/方案.docx"
    # 文件流水的 day 是触发器按北京日历写的（太平洋 9 月 29 日 13:20）
    event(
        db,
        root_id,
        add_file(db, root_id, path),
        path,
        "added",
        "2026-09-30",
        at="2026-09-29T20:20:00.000Z",
    )
    finish_first_pass(db, root_id)

    result = page(db)

    assert [entry["day"] for entry in result["days"]] == ["2026-09-30", "2026-09-28"]
    assert [(item["type"], item["time"]) for item in items(result, "2026-09-30")] == [
        ("deliverable", "04:10"),
        ("files", "04:20"),
        ("meeting", "10:00"),
    ]
    assert [(item["type"], item["time"]) for item in items(result, "2026-09-28")] == [
        ("tasks", "09:00")
    ]


def test_pages_and_windows_follow_beijing_days(tmp_path, process_zone):
    """翻页的起点、记录开始的那一天、记录开始前按修改时间的窗口，边界都是北京的 0 点。"""
    db = make(tmp_path)
    # 第四期从北京 9 月 20 日 01:00 开始记（太平洋还是 9 月 19 日）
    db.execute(
        "UPDATE app_state SET value = ? WHERE key = 'links_since'", ("2026-09-19T17:00:00+00:00",)
    )
    meeting(db, "late", "2026-09-30T23:30:00+08:00")
    task(
        db,
        "b",
        "done",
        events=[("status_changed", "confirmed → done", "2026-09-30T16:01:00+00:00")],
    )
    root_id = root(db)
    # 北京 9 月 19 日 00:30（太平洋 9 月 18 日）改的：记录开始前，归到北京 9 月 19 日
    add_file(db, root_id, "能耗看板/方案.docx", mtime=datetime(2026, 9, 18, 16, 30, tzinfo=UTC))
    # 北京 9 月 20 日 00:30（太平洋 9 月 19 日）改的：已经是记录开始那天，不再按修改时间列
    add_file(db, root_id, "能耗看板/清单.xlsx", mtime=datetime(2026, 9, 19, 16, 30, tzinfo=UTC))
    finish_first_pass(db, root_id)

    first = page(db, days=1)
    assert shape(first) == [("2026-10-01", "今天", [("tasks", "00:01")])]
    assert first["next_before"] == "2026-10-01"
    assert first["file_log_since"] == "2026-09-20"

    second = page(db, days=1, before=date(2026, 10, 1))
    assert shape(second) == [("2026-09-30", "昨天", [("meeting", "23:30")])]

    third = page(db, days=1, before=date(2026, 9, 30))
    assert [entry["day"] for entry in third["days"]] == ["2026-09-19"]
    (prelog,) = items(third, "2026-09-19")
    assert prelog["prelog"] is True and prelog["names"] == ["方案.docx"]
    assert third["next_before"] is None


def test_later_changed_date_is_the_beijing_day(tmp_path, process_zone):
    """「后来改了」带的日子也按北京日历：后一场会在北京 10 月 1 日 00:30 开（太平洋还是 9 月 30 日）。"""
    db = make(tmp_path)
    meeting(db, "a", "2026-09-30T10:00:00+08:00")
    set_minutes(db, "a", "mv-a", "# 周会\n\n## 决议\n1. 阈值先按 0.8 执行\n", kind="generated")
    meeting(db, "b", "2026-10-01T00:30:00+08:00")
    set_minutes(db, "b", "mv-b", "# 周会\n\n## 决议\n1. 阈值改成 0.7\n", kind="generated")
    decisions.ingest_pending(db, now=NOW)
    early = db.query_one("SELECT id FROM decisions WHERE meeting_id = 'a'")["id"]
    late = db.query_one("SELECT id FROM decisions WHERE meeting_id = 'b'")["id"]
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, decision_id, to_decision_id,
                                 quote, evidence_json, created_at, updated_at)
           VALUES ('later_changed', 'p', ?, 'shown', 'llm', 'b', ?, ?, '改成 0.7', '{}', ?, ?)""",
        (f"{early}|{late}", early, late, NOW.isoformat(), NOW.isoformat()),
    )

    for kind in ("all", "decisions"):
        (item,) = items(page(db, kind=kind), "2026-09-30")
        later = item["decisions"][0]["later"] if kind == "all" else item["decision"]["later"]
        assert later == {"date": "2026-10-01", "text": "阈值改成 0.7"}, kind
