"""晨报统计的日历：北京日历，不随跑声档的那台 Mac 的本机时区（太平洋）。

「昨天完成」和「昨天自动归属」都按北京日历的昨天。完成原来写「今天完成」、按本机的今天算：晨报 09:00（北京时间）
发出时北京的今天才刚开始，按北京日历几乎恒为 0，所以换成最接近的「昨天完成」。
"""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest

from meeting_workbench import tasks as tasks_module
from meeting_workbench.db import utc_now
from meeting_workbench.notify import LarkNotifier
from meeting_workbench.task_due import BEIJING_TZ
from meeting_workbench.tasks import TaskService

from .task_views_world import NOW, FrozenClock, make_db
from .test_notify import local_tz  # noqa: F401  把本机时区钉成参数给的那个
from .test_project_linking import seed_meeting

# 时钟钉在 2026-10-02 12:00（北京时间）：昨天是北京的 10-01
BEIJING_NOW = NOW.astimezone(BEIJING_TZ)
TODAY_START = BEIJING_NOW.replace(hour=0, minute=0, second=0, microsecond=0)
YESTERDAY_START = TODAY_START - timedelta(days=1)
LOCAL_ZONES = ["America/Los_Angeles", "Asia/Shanghai", "UTC"]


@pytest.mark.parametrize("local_tz", LOCAL_ZONES, indirect=True)
@pytest.mark.usefixtures("local_tz")
def test_done_yesterday_is_the_beijing_calendar_day(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_module, "datetime", FrozenClock)
    db, settings = make_db(tmp_path)
    finished = {
        "前天最后一秒": YESTERDAY_START - timedelta(seconds=1),
        "昨天第一秒": YESTERDAY_START,
        "昨天最后一秒": TODAY_START - timedelta(seconds=1),
        "今天第一秒": TODAY_START,
        "今天上午": BEIJING_NOW - timedelta(hours=1),
    }
    for index, (title, moment) in enumerate(finished.items()):
        created = (NOW - timedelta(days=5) + timedelta(seconds=index)).isoformat()
        db.execute(
            """INSERT INTO tasks
                   (id, title, status, origin, assignee, status_changed_at, created_at, updated_at)
               VALUES (?, ?, 'done', 'ai', 'me', ?, ?, ?)""",
            (f"task-{index}", title, moment.astimezone(UTC).isoformat(), created, created),
        )

    stats = TaskService(db, settings)._digest_stats()

    # 点名的先后按任务建的先后倒序
    assert stats["done_yesterday"] == ["昨天最后一秒", "昨天第一秒"]
    assert "done_today" not in stats
    assert stats["total"] == 2


@pytest.mark.parametrize("local_tz", LOCAL_ZONES, indirect=True)
@pytest.mark.usefixtures("local_tz")
def test_auto_assigned_yesterday_is_the_beijing_calendar_day(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_module, "datetime", FrozenClock)
    db, settings = make_db(tmp_path)
    for meeting_id in ("m1", "m2", "m3", "m4", "m5", "m6", "m7"):
        seed_meeting(db, meeting_id, f"会 {meeting_id}")
    # 北京昨天是 [10-01 00:00, 10-02 00:00)，对应 UTC 的 09-30 16:00 到 10-01 16:00。这几个时刻按北京算 5 场，
    # 按太平洋的昨天算 4 场、按 UTC 的昨天算 4 场，三种本机时区下的答案都不一样才分得出日历用错
    assigned = {
        "m1": YESTERDAY_START,  # 昨天第一秒
        "m2": TODAY_START - timedelta(seconds=1),  # 昨天最后一秒
        "m3": TODAY_START,  # 今天第一秒，不算
        "m4": YESTERDAY_START - timedelta(seconds=1),  # 前天最后一秒，不算
        "m5": YESTERDAY_START + timedelta(hours=18),
        "m6": YESTERDAY_START + timedelta(hours=10),
        "m7": YESTERDAY_START + timedelta(hours=4),
    }
    for meeting_id, moment in assigned.items():
        db.execute(
            """INSERT INTO events(meeting_id, event_type, actor, payload_json, created_at)
               VALUES (?, 'meeting_project_auto_assigned', 'system', '{}', ?)""",
            (meeting_id, moment.astimezone(UTC).isoformat()),
        )
    db.execute(
        """INSERT INTO project_links(meeting_id, minutes_version_id, status, candidates_json, created_at)
           VALUES ('m1', 'mv-m1', 'needs_review', '[]', ?)""",
        (utc_now(),),
    )

    stats = TaskService(db, settings)._digest_stats()

    assert stats["auto_assigned_yesterday"] == 5
    assert stats["needs_review"] == 1


def test_digest_card_says_yesterday_done(tmp_path, monkeypatch):
    db, _settings = make_db(tmp_path)
    notifier = LarkNotifier(db, webhook_url="https://hook/", public_base_url="http://x")
    sent: list[str] = []
    monkeypatch.setattr(
        notifier, "_send", lambda kind, ref_key, title, text, **kwargs: sent.append(text) or True
    )

    assert notifier.daily_digest(
        {
            "total": 3,
            "pending": 1,
            "pending_sources": [],
            "stalled": 0,
            "stalled_titles": [],
            "stalled_days": [],
            "done_yesterday": ["整理报价单", "回访客户"],
            "auto_assigned_yesterday": 0,
            "needs_review": 0,
        }
    )

    assert "· 昨天完成 2 条：整理报价单。" in sent[0]
    assert "今天完成" not in sent[0]
