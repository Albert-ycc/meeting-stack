"""项目列表的卡片数字（项目页与待办改版 261001，R06-1、R06-2）。

每个项目：进行中需求的标题（最多 2 条，P0→P3、同级按最近会议由近到远）和条数、待认领的需求候选数、
近 12 周每周开了几场会（本周在最后）、最近一场会的标题、累计录音时长、最近一场会距今几周。
周按北京日历从周一算（会上说话人和用户的日历，见 task_due.BEIJING_TZ），和待办「今天」同一口径。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .project_seats import MEETING_TIME_SQL
from .task_due import beijing_today, speaker_date

RHYTHM_WEEKS = 12
TITLES_SHOWN = 2


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def card_stats(connection: Any, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{项目 id: 卡片数字}，只含有数字可给的项目；没出现的项目按全 0 处理。"""
    today = beijing_today(now)
    this_monday = _monday(today)
    stats: dict[str, dict[str, Any]] = {}

    def entry(project_id: str) -> dict[str, Any]:
        return stats.setdefault(project_id, empty_stats())

    for row in connection.execute(
        f"""SELECT r.project_id, r.title,
                   (SELECT MAX(julianday({MEETING_TIME_SQL}))
                      FROM requirement_meetings rm JOIN meetings m ON m.id = rm.meeting_id
                     WHERE rm.requirement_id = r.id) AS latest_jd
              FROM requirements r
             WHERE r.status = 'active'
             ORDER BY r.project_id, r.priority, latest_jd IS NULL, latest_jd DESC, r.created_at DESC"""
    ).fetchall():
        item = entry(row["project_id"])
        item["active_requirement_count"] += 1
        if len(item["active_requirement_titles"]) < TITLES_SHOWN:
            item["active_requirement_titles"].append(row["title"])
    for row in connection.execute(
        """SELECT m.project_id, COUNT(*) AS n FROM requirement_candidates c
             JOIN meetings m ON m.id = c.meeting_id
            WHERE c.status = 'pending' AND m.project_id IS NOT NULL
            GROUP BY m.project_id"""
    ).fetchall():
        entry(row["project_id"])["pending_candidate_count"] = row["n"]
    latest_jd: dict[str, float] = {}
    for row in connection.execute(
        f"""SELECT m.project_id, m.title, m.duration_ms, {MEETING_TIME_SQL} AS at,
                   julianday({MEETING_TIME_SQL}) AS jd
              FROM meetings m WHERE m.project_id IS NOT NULL"""
    ).fetchall():
        item = entry(row["project_id"])
        item["recording_ms"] += row["duration_ms"] or 0
        if row["jd"] is not None and row["jd"] > latest_jd.get(row["project_id"], float("-inf")):
            latest_jd[row["project_id"]] = row["jd"]
            item["latest_meeting_title"] = row["title"]
            held = speaker_date(row["at"])
            item["weeks_since_last_meeting"] = (
                max(0, (today - held).days) // 7 if held is not None else None
            )
        held = speaker_date(row["at"])
        if held is None:
            continue
        weeks_ago = (this_monday - _monday(held)).days // 7
        if 0 <= weeks_ago < RHYTHM_WEEKS:
            item["weekly_meetings"][RHYTHM_WEEKS - 1 - weeks_ago] += 1
    return stats


def empty_stats() -> dict[str, Any]:
    """没有需求、候选、会议的项目：卡片上全是 0。weeks_since_last_meeting 是最近一场会距今整周数
    （前端满 3 周写「已 N 周没有会议」），没有会议为 None。"""
    return {
        "active_requirement_titles": [],
        "active_requirement_count": 0,
        "pending_candidate_count": 0,
        "weekly_meetings": [0] * RHYTHM_WEEKS,
        "latest_meeting_title": None,
        "recording_ms": 0,
        "weeks_since_last_meeting": None,
    }
