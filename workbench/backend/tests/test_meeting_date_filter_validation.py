"""会议列表 /api/meetings 的 date_from/date_to 和任务池走同一道校验：不是 YYYY-MM-DD 的回 400，不再拿去和
会议日期做字符串比较、静默回空，被误读成「这段时间真没会」。

数字只认 ASCII 的 0-9：年份写成阿拉伯-印度数字、全角数字的，原来连任务池的校验也能过（Python 的 \\d 认
所有文字的数字），和会议日期一比恒假，结果同样静默为空。"""

import pytest

from meeting_workbench.db import Database, utc_now

from .test_tasks_api import make_client
from .test_timeline import shanghai  # noqa: F401  会议日期按本机日历比，本机时区钉成北京时间

MEETINGS = {
    "vm-jan": "2026-01-15T10:00:00+08:00",
    "vm-jun": "2026-06-15T10:00:00+08:00",
    "vm-dec": "2026-12-15T10:00:00+08:00",
}
BAD_DATES = [
    pytest.param("abc", id="不是日期"),
    pytest.param("2026/09/01", id="分隔符不对"),
    pytest.param("2026-02-30", id="格式对但没有这一天"),
    pytest.param("٢٠٢٦-٠١-٠١", id="阿拉伯-印度数字"),
    pytest.param("２０２６-０１-０１", id="全角数字"),
    pytest.param("٢٠٢٦-01-01", id="年份是阿拉伯-印度数字"),
    pytest.param("２０２６-01-01", id="年份是全角数字"),
    pytest.param("२०२६-01-01", id="年份是天城文数字"),
    pytest.param("2026-01-01\n", id="结尾带换行"),
]


@pytest.fixture
def client(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    for meeting_id, recording_date in MEETINGS.items():
        db.execute(
            """INSERT INTO meetings(id, title, recording_date, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (meeting_id, meeting_id, recording_date, utc_now(), utc_now()),
        )
    return client


def meeting_ids(client, **params) -> set[str]:
    response = client.get("/api/meetings", params=params)
    assert response.status_code == 200
    return {item["id"] for item in response.json()["items"]}


@pytest.mark.parametrize("value", BAD_DATES)
@pytest.mark.parametrize("name", ["date_from", "date_to"])
def test_meeting_list_rejects_a_date_that_is_not_yyyy_mm_dd(client, name, value):
    response = client.get("/api/meetings", params={name: value})

    assert response.status_code == 400
    assert response.json()["detail"] == "日期格式应为 YYYY-MM-DD"


@pytest.mark.parametrize("value", BAD_DATES)
def test_task_pool_rejects_the_same_dates(client, value):
    response = client.get("/api/tasks", params={"meeting_date_from": value})

    assert response.status_code == 400
    assert response.json()["detail"] == "日期格式应为 YYYY-MM-DD"


def test_meeting_list_still_filters_by_the_dates_the_page_sends(client):
    # 录音档案页的 <input type="date"> 发的都是 YYYY-MM-DD，没填的不发
    assert meeting_ids(client) == set(MEETINGS)
    assert meeting_ids(client, date_from="2026-03-01", date_to="2026-12-31") == {"vm-jun", "vm-dec"}
    assert meeting_ids(client, date_to="2026-06-15") == {"vm-jan", "vm-jun"}
    assert meeting_ids(client, date_from="2026-12-15") == {"vm-dec"}


def test_start_after_end_is_an_empty_result_as_in_the_task_pool(client):
    meetings = client.get(
        "/api/meetings", params={"date_from": "2026-12-31", "date_to": "2026-01-01"}
    )
    tasks = client.get(
        "/api/tasks", params={"meeting_date_from": "2026-12-31", "meeting_date_to": "2026-01-01"}
    )

    assert (meetings.status_code, meetings.json()["total"]) == (200, 0)
    assert (tasks.status_code, tasks.json()["total"]) == (200, 0)
