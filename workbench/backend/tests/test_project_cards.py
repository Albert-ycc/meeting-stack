"""项目页与待办改版·项目列表卡片（R06-2）：进行中需求标题、待认领数、近 12 周会议节奏、最近一场会、录音时长。

项目、会议、录音时间和时长照生产库（requirement_pool_world）。录音时间是太平洋时间，周按北京日历从周一算：
「EDC 系统选型」录于 09-28 18:37（太平洋）＝北京 09-29，落在「今天」2026-09-30 所在的这一周。
"""

from __future__ import annotations

from datetime import date

from meeting_workbench import project_cards

from .requirement_pool_world import project_id
from .test_task_due import make_cvm_world
from .test_todo import create_requirement


def projects_by_id(client):
    response = client.get("/api/projects")
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


def test_project_cards_count_requirements_candidates_and_meetings(tmp_path, monkeypatch):
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    monkeypatch.setattr(project_cards, "beijing_today", lambda now=None: date(2026, 9, 30))
    create_requirement(client, headers, "yimi", "赠药横跳拦截", "P1")
    create_requirement(client, headers, "yimi", "京东科研仓对接", "P0")
    create_requirement(client, headers, "yimi", "新患者注册：五个问题前置", "P2")

    cards = projects_by_id(client)

    yimi = cards[project_id("yimi")]
    assert yimi["active_requirement_titles"] == ["京东科研仓对接", "赠药横跳拦截"]
    assert yimi["active_requirement_count"] == 3
    # 横跳（北京 09-11）、京东（09-17）、五个问题（09-21）、EDC（09-29）各落一周，最后一格是本周
    assert yimi["weekly_meetings"] == [0] * 8 + [1, 1, 1, 1]
    assert (yimi["latest_meeting_title"], yimi["weeks_since_last_meeting"]) == (
        "EDC 系统选型与产研对接决策",
        0,
    )
    assert yimi["recording_ms"] == 3033387 + 194525 + 113475 + 78655
    hengrui = cards[project_id("hengrui")]
    # 亲友积分、黑卡注销两场都在太平洋 09-21 晚上＝北京 09-22，同一周
    assert hengrui["weekly_meetings"][10] == 2 and sum(hengrui["weekly_meetings"]) == 2
    assert hengrui["latest_meeting_title"] == "黑卡分享注销与积分限制口径"
    cvm = cards[project_id("cvm")]
    assert (cvm["pending_candidate_count"], cvm["active_requirement_titles"]) == (1, [])
    pager = cards[project_id("pager")]
    assert pager["weekly_meetings"] == [0] * 12 and pager["weeks_since_last_meeting"] is None
    assert pager["latest_meeting_title"] is None and pager["recording_ms"] == 0


def test_quiet_projects_and_rhythm_slide_with_the_week(tmp_path, monkeypatch):
    """三周后再看：华夏（北京 09-14 开的最后一场）已 5 周没有会议，节奏条整体往前挪。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    monkeypatch.setattr(project_cards, "beijing_today", lambda now=None: date(2026, 10, 20))

    cards = projects_by_id(client)

    huaxia = cards[project_id("huaxia")]
    # 按自然周：09-14 那周到 10-19 那周隔 5 周，节奏条最后 5 格是空的
    assert huaxia["weeks_since_last_meeting"] == 5

    assert huaxia["weekly_meetings"] == [0] * 6 + [1] + [0] * 5
    # 处理过的候选不算待认领
    client.post(f"/api/requirement-candidates/{candidate_id}/drop", json={}, headers=headers)
    assert projects_by_id(client)[project_id("cvm")]["pending_candidate_count"] == 0


def test_weeks_without_meetings_count_calendar_weeks(tmp_path, monkeypatch):
    """恒瑞最近一场在北京 09-22（周二）；今天 10-05（周一）：隔了 09-28、10-05 两个周一，按自然周是 2，
    节奏条最后两格是空的（滚动 7 天取整只有 13 天＝1 周，和节奏条对不上）。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    monkeypatch.setattr(project_cards, "beijing_today", lambda now=None: date(2026, 10, 5))

    hengrui = projects_by_id(client)[project_id("hengrui")]

    assert hengrui["weeks_since_last_meeting"] == 2
    assert hengrui["weekly_meetings"][-3:] == [2, 0, 0]
