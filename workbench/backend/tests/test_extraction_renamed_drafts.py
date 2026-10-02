"""重抽任务时认得出用户改过名的草稿：AI 又抽到原来的名字，不另出一条。

没有加列：AI 抽出草稿时（created 事件）和重抽原地更新它时（regenerated 事件），把当时的名字记进事件文字
（「AI 从会后纪要生成本条任务草稿：「做看板」」）。_existing_drafts 把动过的草稿的这些名字也算作它的名字，
AI 再抽到的名字对得上就是同一件事，照「有人动过的草稿留着那条」的规矩不另出一条。
"""

from __future__ import annotations

from meeting_workbench.db import Database, utc_now
from meeting_workbench.tasks import TaskService

from .helpers import seed_editable_meeting
from .test_tasks_api import MEETING, ai_tasks, drafts, make_client, re_extract, seed_minutes
from .test_tasks_api import write_headers

CREATED = "AI 从会后纪要生成本条任务草稿"
REGENERATED = "AI 重新抽取，草稿按这次的结果更新"


def make_meeting(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(client, settings)
    return client, headers, db, settings


def event_bodies(db, task_id):
    return [
        (row["kind"], row["body"])
        for row in db.query_all(
            "SELECT kind, body FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


def rename(client, headers, task_id, title):
    response = client.patch(f"/api/tasks/{task_id}", json={"title": title}, headers=headers)
    assert response.status_code == 200, response.text


def test_the_ai_title_is_recorded_in_the_draft_events(tmp_path, monkeypatch):
    client, headers, db, settings = make_meeting(tmp_path)
    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: ai_tasks("做看板"))
    re_extract(client, headers)
    task_id = drafts(db)["做看板"]["id"]
    assert event_bodies(db, task_id) == [("created", f"{CREATED}：「做看板」")]

    # 没人动过的草稿重抽时原地更新（名字只差写法），事件里记这次的名字
    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: ai_tasks("做看板 "))
    re_extract(client, headers)

    assert event_bodies(db, task_id) == [
        ("created", f"{CREATED}：「做看板」"),
        ("regenerated", f"{REGENERATED}：「做看板」"),
    ]


def test_a_renamed_draft_is_recognized_when_the_ai_extracts_the_original_name(
    tmp_path, monkeypatch
):
    client, headers, db, settings = make_meeting(tmp_path)
    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: ai_tasks("做看板", "写周报"))
    re_extract(client, headers)
    first = drafts(db)
    rename(client, headers, first["做看板"]["id"], "做运营看板")
    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: ai_tasks("做看板", "约评审"))

    for _round in range(2):
        assert re_extract(client, headers)["status"] == "done"

        second = drafts(db)
        # AI 又抽到原名「做看板」：认得出是改成「做运营看板」的那一条，不另出一条；其余照旧对账
        assert set(second) == {"做运营看板", "约评审"}
        assert second["做运营看板"]["id"] == first["做看板"]["id"]
    note = db.query_one("SELECT error FROM task_extractions WHERE meeting_id=?", (MEETING,))[
        "error"
    ]
    assert "「做看板」这场会已有人动过的同名草稿，留着那条" in note


def test_the_scan_path_recognizes_a_renamed_draft_too(tmp_path, monkeypatch):
    """纪要重新生成后扫描再抽走的也是 _extract_one，同一套对账。"""
    client, headers, db, settings = make_meeting(tmp_path)
    db.execute(
        """INSERT INTO task_extractions(meeting_id, minutes_version_id, supplement, created_at)
           VALUES (?, 'mv-1', '', ?)""",
        (MEETING, utc_now()),
    )
    service = TaskService(db, settings)
    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: ai_tasks("做看板"))
    assert service.extract_pending()["succeeded"] == 1
    rename(client, headers, drafts(db)["做看板"]["id"], "做运营看板")
    db.execute(
        """INSERT INTO minutes_versions (id, meeting_id, version_no, markdown, html, kind,
                                         published, created_at)
           VALUES ('mv-regen', ?, 2, '# 摘要\n重新生成。', '<p></p>', 'stale_generated', 1, ?)""",
        (MEETING, utc_now()),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-regen' WHERE id=?", (MEETING,))

    assert service.extract_pending()["succeeded"] == 1

    assert set(drafts(db)) == {"做运营看板"}


def test_drafts_created_before_the_names_were_recorded_are_not_recognized(tmp_path, monkeypatch):
    """升级前抽出的草稿事件里没记名字，改过名的认不出，和以前一样；这类草稿最多留 task_draft_expire_days
    （7 天）就过期。想认出它们得补记名字（或加一列），这条用例钉住这个边界。"""
    client, headers, db, settings = make_meeting(tmp_path)
    monkeypatch.setattr(TaskService, "_call_llm", lambda self, prompt: ai_tasks("做看板"))
    re_extract(client, headers)
    task_id = drafts(db)["做看板"]["id"]
    db.execute(
        "UPDATE task_events SET body=? WHERE task_id=? AND kind='created'", (CREATED, task_id)
    )
    rename(client, headers, task_id, "做运营看板")

    re_extract(client, headers)

    assert set(drafts(db)) == {"做运营看板", "做看板"}
