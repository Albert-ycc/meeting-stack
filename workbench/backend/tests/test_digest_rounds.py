"""晨报和停滞提醒的每一轮扫描（TaskService.run_notifications）。

- 09:00 以后晨报当天发过了，之后每一轮扫描不再算统计：统计要过一遍全部任务，原来每轮约 7.8 秒（890 条任务），
  算完才在发送那一步发现今天发过。
- 停滞、晨报统计不再逐条 task_summary（每条开 5 次连接）：只取要用的几列，评论时间一条 SQL 取齐。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meeting_workbench import task_due
from meeting_workbench import tasks as tasks_module
from meeting_workbench.notify import LarkNotifier
from meeting_workbench.tasks import TaskService

from .task_views_world import FrozenClock, build_world, counted, make_db
from .test_timeline import shanghai  # noqa: F401  按本机日算的统计，本机时区钉成北京时间


@pytest.fixture
def frozen_clock(monkeypatch):
    """时钟钉在 2026-10-02 12:00（北京时间），晨报已经到点。"""
    monkeypatch.setattr(tasks_module, "datetime", FrozenClock)
    monkeypatch.setattr(task_due, "datetime", FrozenClock)


def make_service(
    tmp_path: Path, *, notifying: bool = True
) -> tuple[TaskService, LarkNotifier, list]:
    db, settings = make_db(tmp_path)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    posts: list[Any] = []
    notifier._post = lambda payload: posts.append(payload) or True  # type: ignore[method-assign]
    return TaskService(db, settings, notifier=notifier if notifying else None), notifier, posts


def test_rounds_after_the_digest_went_out_skip_the_stats(tmp_path, monkeypatch, frozen_clock):
    service, notifier, posts = make_service(tmp_path)
    build_world(service.db, meetings=8, tasks_per_meeting=3)
    computed: list[int] = []
    original = service._digest_stats
    monkeypatch.setattr(service, "_digest_stats", lambda: computed.append(1) or original())

    first = service.run_notifications()

    assert first["digest_sent"] is True and computed == [1]
    for _ in range(3):
        assert service.run_notifications()["digest_sent"] is False
    assert computed == [1]
    assert notifier.digest_sent_today()
    digests = [p for p in posts if "晨报" in p["card"]["header"]["title"]["content"]]
    assert len(digests) == 1


def test_stats_are_still_computed_while_the_digest_has_not_gone_out(
    tmp_path, monkeypatch, frozen_clock
):
    """没发出去（发送失败）的那天，下一轮照常重新算、重新发。"""
    service, notifier, posts = make_service(tmp_path)
    build_world(service.db, meetings=8, tasks_per_meeting=3)
    monkeypatch.setattr(notifier, "_post", lambda payload: False)
    computed: list[int] = []
    original = service._digest_stats
    monkeypatch.setattr(service, "_digest_stats", lambda: computed.append(1) or original())

    assert service.run_notifications()["digest_sent"] is False
    assert service.run_notifications()["digest_sent"] is False

    assert computed == [1, 1]
    assert not notifier.digest_sent_today()


def test_stall_and_digest_stats_match_the_per_task_implementation(tmp_path, frozen_clock):
    """期望值是逐条 task_summary 的旧实现在这份数据上的输出（时钟钉死、本机时区北京时间）。"""
    service, _notifier, _posts = make_service(tmp_path, notifying=False)
    build_world(service.db, meetings=8, tasks_per_meeting=3)

    assert [
        [t["id"], t["title"], t["stall_days"], t["stall_since"], t["project_name"], t["assignee"]]
        for t in service._stall_candidates()
    ] == [
        ["task-w00016", "任务 00016 导出", 3.375, "2026-09-28T19:00:00+00:00", "项目 1", "me"],
        [
            "task-w00021",
            "任务 00021 入库",
            4.416666666666667,
            "2026-09-27T18:00:00+00:00",
            "项目 3",
            "ai",
        ],
    ]
    assert service._digest_stats() == {
        "auto_assigned_yesterday": 0,
        "done_today": ["任务 00013 入库"],
        "in_progress": 9,
        "needs_review": 0,
        "pending": 8,
        "pending_sources": [
            "会议 000 导出",
            "会议 001 看板",
            "会议 002 对账",
            "会议 003 提醒",
            "会议 004 权限",
            "会议 005 入库",
            "会议 006 回访",
            "会议 007 审核",
        ],
        "stalled": 2,
        "stalled_days": [4, 3],
        "stalled_titles": ["任务 00021 入库", "任务 00016 导出"],
        "total": 18,
    }


def test_stall_and_digest_statements_do_not_grow_with_tasks(tmp_path, monkeypatch, frozen_clock):
    def tally(root: Path, meetings: int, tasks_per_meeting: int) -> dict[str, tuple[int, int]]:
        service, _notifier, _posts = make_service(root, notifying=False)
        build_world(service.db, meetings=meetings, tasks_per_meeting=tasks_per_meeting)
        result = {}
        for name, call in (("stall", service._stall_candidates), ("digest", service._digest_stats)):
            with counted(monkeypatch) as counts:
                call()
            result[name] = (counts["statements"], counts["connections"])
        return result

    small = tally(tmp_path / "small", 10, 4)
    large = tally(tmp_path / "large", 30, 12)

    assert large == small
    assert small["stall"][0] <= 3 and small["digest"][0] <= 12
