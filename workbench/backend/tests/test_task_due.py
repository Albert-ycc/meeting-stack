"""项目页与待办改版·数据层（schema v18）：任务截止、确认时挂候选、撤销完成。

会议、纪要行动项、逐字稿和任务名都照生产库（261001 只读取出）。「发卡变更与一直拍流程改造沟通」录于
太平洋时间 09-22 晚上 20:30，在北京时间已经是 09-23 上午：开会日期按说话人的北京日历算，跟跑它的机器在
哪个时区无关，太平洋和北京两个时区各钉一遍。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta

import pytest

from meeting_workbench import tasks as tasks_module
from meeting_workbench.db import Database, utc_now
from meeting_workbench.task_due import extracted_due, meeting_date, prompt_rules
from meeting_workbench.tasks import TaskService

from .requirement_pool_world import meeting_id, project_id, seed_minutes, seed_world
from .test_requirement_extraction import (
    CHASE_TASK,
    EXPORT,
    LAUNCH,
    RECEIPT,
    SCRIPT_TASK,
    fake_ai,
    reply,
    scan,
)
from .test_tasks_api import make_client, write_headers
from .test_timeline import shanghai  # noqa: F401  北京时间下开会日期跨到第二天

CARD_MEETING = "vm-20260922-203016-464d0ff9"
CARD_PROJECT = "project-fabde688a6d54b34"
CARD_MINUTES = """# 发卡变更与一直拍流程改造沟通

## 四、行动项清单

| # | 事项 | Owner | 截止 | 音频锚点 |
|---|---|---|---|---|
| 1 | 与石总商量卡能否先走线上发放 | 待确认 | 待确认 | [00:02:08] |
| 2 | 「人身健康」功能开发完成 | 待确认 | 2026-09-23 | [00:06:20] |
| 3 | 与石总对接节点口径 | 待确认 | 待确认 | [00:10:37] |
| 4 | 明天晚上先上后台、同时撤下码，统一验证扫码 | 待确认 | 2026-09-23 | [00:11:36] |
"""
CARD_SEGMENTS = [
    (380190, "C1_SPEAKER_01", "也就是说明天能开发完差不多嘛，"),
    (382830, "C1_SPEAKER_01", "就是二十八号再测测。"),
    (695910, "C1_SPEAKER_00", "二十八号给他。"),
    (696890, "C1_SPEAKER_00", "这明天晚上把后台先上了吧，"),
]
# 那场会真实抽出的任务（标题、原话照库里）
BACKEND_TASK = {
    "title": "明天晚上先上后台并撤下码，统一验证扫码",
    "detail": "明天晚上先把后台上了，同时把码先撤掉，统一再验证扫码有没有问题。",
    "anchor_quote": "这明天晚上把后台先上了吧",
    "assignee_suggestion": "me",
}
HEALTH_TASK = {
    "title": "「人身健康」明天开发完、28号再测",
    "detail": "发卡方式改为一千个码、积分规则重写，明天（9月23日）开发完，28号再测，守住9月28日印刷节点。",
    "anchor_quote": "也就是说明天能开发完差不多嘛，",
    "assignee_suggestion": "ai",
}
SHI_TASK = {
    "title": "与石总商量卡能否先走线上发放",
    "anchor_quote": "",
    "assignee_suggestion": "me",
}


@pytest.fixture
def pacific():
    """声档实际跑的那台 Mac 的时区。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def seed_card_meeting(db: Database) -> None:
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id, name, color, created_at) VALUES (?, '黑卡小程序', '#2c8d83', ?)",
        (CARD_PROJECT, now),
    )
    db.execute(
        """INSERT INTO meetings
               (id, title, recording_date, duration_ms, status, project_id, created_at, updated_at)
           VALUES (?, '发卡变更与一直拍流程改造沟通', '2026-09-22T20:30:16-07:00', 1283730,
                   'published', ?, '2026-09-23T04:41:58.664167+00:00', ?)""",
        (CARD_MEETING, CARD_PROJECT, now),
    )
    version = db.create_transcript_version(CARD_MEETING, "funasr", published=True)
    db.replace_segments(
        version,
        CARD_MEETING,
        [
            {
                "id": f"{CARD_MEETING}-{start}",
                "ordinal": ordinal,
                "start_ms": start,
                "end_ms": start + 1000,
                "speaker_label": speaker,
                "text": text,
            }
            for ordinal, (start, speaker, text) in enumerate(CARD_SEGMENTS)
        ],
    )
    db.execute(
        """INSERT INTO minutes_versions
               (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-card-1', ?, 1, ?, '<p></p>', 'generated', 1, ?)""",
        (CARD_MEETING, CARD_MINUTES, now),
    )
    db.execute(
        "UPDATE meetings SET current_minutes_version_id='mv-card-1' WHERE id=?", (CARD_MEETING,)
    )


def make_card_world(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_card_meeting(db)
    return client, headers, db, settings


def run_extraction(db, settings, meeting=CARD_MEETING):
    db.execute(
        """INSERT INTO task_extractions(meeting_id, minutes_version_id, supplement, created_at)
           VALUES (?, (SELECT current_minutes_version_id FROM meetings WHERE id=?), '', ?)""",
        (meeting, meeting, utc_now()),
    )
    stats = TaskService(db, settings).extract_pending()
    assert stats["succeeded"] == 1, stats


def answer_by_meeting_date(prompt: str) -> str:
    """假模型照提示词里给的开会日期换算：「明天」加一天，「28 号」取开会当天或之后最近的 28 号。"""
    line = next(line for line in prompt.splitlines() if line.startswith("开会日期："))
    base = date.fromisoformat(line.removeprefix("开会日期：")[:10])
    the_28th = base.replace(day=28)
    return json.dumps(
        {
            "tasks": [
                {**BACKEND_TASK, "due_phrase": "明天晚上", "due_date": str(base + timedelta(1))},
                {**HEALTH_TASK, "due_phrase": "二十八号再测测", "due_date": str(the_28th)},
                {**SHI_TASK, "due_phrase": "待确认", "due_date": None},
            ]
        },
        ensure_ascii=False,
    )


def due_of(db):
    return {
        row["title"]: (row["due_date"], row["due_phrase"])
        for row in db.query_all("SELECT title, due_date, due_phrase FROM tasks")
    }


# ---------------------------------------------------------------- 会后抽取顺带抽截止（R07-2）


def test_extraction_converts_due_from_the_meeting_date(tmp_path, monkeypatch, pacific):
    """生产机在太平洋时区（还是 09-22 晚上），会上说话的人已是北京 09-23（周三）上午：「明天晚上」是 09-24，
    「二十八号」是 09-28；「待确认」落不到日子，归未定截止，原文说法照留。提示词里给出本周、下周每天的日子。"""
    client, headers, db, settings = make_card_world(tmp_path)
    prompts = fake_ai(monkeypatch, answer_by_meeting_date)

    run_extraction(db, settings)

    prompt = prompts[0]
    assert "开会日期：2026-09-23（周三）" in prompt
    assert "本周：2026-09-21（周一）" in prompt and "2026-09-27（周日）" in prompt
    assert "下周：2026-09-28（周一）" in prompt
    assert '"due_phrase":null,"due_date":null' in prompt
    assert due_of(db) == {
        "明天晚上先上后台并撤下码，统一验证扫码": ("2026-09-24", "明天晚上"),
        "「人身健康」明天开发完、28号再测": ("2026-09-28", "二十八号再测测"),
        "与石总商量卡能否先走线上发放": (None, "待确认"),
    }
    task = client.get("/api/tasks", params={"status": "pending_confirm"}).json()["items"][0]
    assert {"due_date", "due_phrase", "candidate_id", "candidate_title"} <= set(task)


def test_meeting_date_does_not_depend_on_the_machine(tmp_path, monkeypatch, shanghai):  # noqa: F811
    """同一场会换到北京时区的机器上抽：开会日期、换算结果都和太平洋那台一样。"""
    client, headers, db, settings = make_card_world(tmp_path)
    prompts = fake_ai(monkeypatch, answer_by_meeting_date)

    run_extraction(db, settings)

    assert "开会日期：2026-09-23（周三）" in prompts[0]
    assert due_of(db)["明天晚上先上后台并撤下码，统一验证扫码"] == ("2026-09-24", "明天晚上")


def test_pacific_sunday_night_is_a_beijing_monday(pacific):
    """「合伙人白名单」录于太平洋周日 09-13 晚上 23:01，逐字稿里说「今天几号人天十四号」：北京已是周一 09-14，
    「本周」是 09-14 到 09-20，不是太平洋那一周。录音时间写成 +00:00 的也换成北京日子。"""
    rules = prompt_rules(meeting_date({"recording_date": "2026-09-13T23:01:57-07:00"}))
    assert "开会日期：2026-09-14（周一）" in rules
    assert "本周：2026-09-14（周一）" in rules and "2026-09-20（周日）" in rules
    assert meeting_date({"recording_date": "2026-06-08T02:35:08+00:00"}) == date(2026, 6, 8)
    assert meeting_date(
        {"recording_date": None, "created_at": "2026-09-23T04:41:58+00:00"}
    ) == date(2026, 9, 23)


def test_due_the_model_got_wrong_falls_back_to_undated(tmp_path, monkeypatch, pacific):
    """AI 给的日子核不过——早于开会日、不是 YYYY-MM-DD、离开会超过一年、不是字符串——一律未定截止，
    原文说法照留，任务照常落库。"""
    client, headers, db, settings = make_card_world(tmp_path)
    fake_ai(
        monkeypatch,
        json.dumps(
            {
                "tasks": [
                    {**BACKEND_TASK, "due_phrase": "明天晚上", "due_date": "2026-09-21"},
                    {**HEALTH_TASK, "due_phrase": "二十八号再测测", "due_date": "20260928"},
                    {**SHI_TASK, "due_phrase": ["待确认"], "due_date": 20260923},
                ]
            },
            ensure_ascii=False,
        ),
    )

    run_extraction(db, settings)

    assert due_of(db) == {
        "明天晚上先上后台并撤下码，统一验证扫码": (None, "明天晚上"),
        "「人身健康」明天开发完、28号再测": (None, "二十八号再测测"),
        "与石总商量卡能否先走线上发放": (None, None),
    }


def test_surrogates_from_the_model_do_not_drop_tasks(tmp_path, monkeypatch, pacific):
    """模型回复里 \\ud800 这类转义解出来的孤立代理字符写不进库：去掉以后任务照常落库，不丢条。"""
    client, headers, db, settings = make_card_world(tmp_path)
    raw = json.dumps(
        {
            "tasks": [
                {**BACKEND_TASK, "due_phrase": "明天晚上", "due_date": "2026-09-24"},
                {**SHI_TASK, "due_phrase": "待确认", "due_date": None},
            ]
        },
        ensure_ascii=False,
    )
    raw = raw.replace('"明天晚上"', '"\\ud800明天晚上"').replace(
        '"与石总商量', '"与石总\\udfff商量'
    )
    fake_ai(monkeypatch, raw)

    run_extraction(db, settings)

    assert due_of(db) == {
        "明天晚上先上后台并撤下码，统一验证扫码": ("2026-09-24", "明天晚上"),
        "与石总商量卡能否先走线上发放": (None, "待确认"),
    }
    assert db.query_one("SELECT error FROM task_extractions") == {"error": None}


def test_extracted_due_bounds():
    base = date(2026, 9, 22)
    assert extracted_due({"due_date": "2026-09-22", "due_phrase": "今天"}, base) == (
        "2026-09-22",
        "今天",
    )
    assert extracted_due({"due_date": "2027-09-23"}, base) == ("2027-09-23", None)
    assert extracted_due({"due_date": "2027-09-24"}, base) == (None, None)
    assert extracted_due({"due_date": "2026-02-30"}, base) == (None, None)
    assert extracted_due({"due_date": "2026-09-23"}, None) == (None, None)
    # 原文说法只是核对用的依据：压成一行、最多 40 字
    phrase = extracted_due({"due_phrase": "  本周内\n（待确认）" + "啊" * 60}, base)[1]
    assert phrase.startswith("本周内 （待确认）") and len(phrase) == 40
    assert "due_date 一律写 null" in prompt_rules(None)


def test_extraction_never_backfills_existing_tasks(tmp_path, monkeypatch, pacific):
    """存量任务不回填：同一场会里原来就有的任务（原文带「明天」）重新抽取后截止仍是空的。"""
    client, headers, db, settings = make_card_world(tmp_path)
    db.execute(
        """INSERT INTO tasks(id, title, detail, status, origin, meeting_id, project_id,
                             anchor_quote, status_changed_at, created_at, updated_at)
           VALUES ('task-5172818d4e3f4190b1a7f46efad1b8ab', ?, ?, 'confirmed', 'ai', ?, ?, ?,
                   'x', 'x', 'x')""",
        (
            BACKEND_TASK["title"],
            BACKEND_TASK["detail"],
            CARD_MEETING,
            CARD_PROJECT,
            BACKEND_TASK["anchor_quote"],
        ),
    )
    fake_ai(monkeypatch, answer_by_meeting_date)

    run_extraction(db, settings)

    assert db.query_one(
        "SELECT due_date, due_phrase FROM tasks WHERE id='task-5172818d4e3f4190b1a7f46efad1b8ab'"
    ) == {"due_date": None, "due_phrase": None}


# ---------------------------------------------------------------- 手动填截止


def test_due_can_be_set_and_cleared_by_hand(tmp_path, monkeypatch, pacific):
    """新建、修改任务可填写或清空截止；手动改过截止，AI 抽到的原文说法一并清掉。"""
    client, headers, db, settings = make_card_world(tmp_path)
    fake_ai(monkeypatch, answer_by_meeting_date)
    run_extraction(db, settings)
    task_id = db.query_one("SELECT id FROM tasks WHERE title=?", (HEALTH_TASK["title"],))["id"]

    moved = client.patch(f"/api/tasks/{task_id}", json={"due_date": "2026-09-30"}, headers=headers)
    assert moved.status_code == 200, moved.text
    assert (moved.json()["due_date"], moved.json()["due_phrase"]) == ("2026-09-30", None)
    # 只改标题不动截止
    renamed = client.patch(
        f"/api/tasks/{task_id}", json={"title": "人身健康 28 号再测"}, headers=headers
    )
    assert renamed.json()["due_date"] == "2026-09-30"
    cleared = client.patch(f"/api/tasks/{task_id}", json={"due_date": None}, headers=headers)
    assert cleared.json()["due_date"] is None
    for bad in ("2026/09/30", "20260930", "2026-02-30", "明天", "0001-01-01", "9999-12-31"):
        response = client.patch(f"/api/tasks/{task_id}", json={"due_date": bad}, headers=headers)
        assert response.status_code == 400, (bad, response.text)

    created = client.post(
        "/api/tasks",
        json={"title": "复测发卡与核销链路", "project_id": CARD_PROJECT, "due_date": "2026-10-08"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    assert created.json()["due_date"] == "2026-10-08"
    # 确认时也能顺手改截止
    draft_id = db.query_one("SELECT id FROM tasks WHERE title=?", (BACKEND_TASK["title"],))["id"]
    confirmed = client.post(
        f"/api/tasks/{draft_id}/confirm", json={"due_date": "2026-09-24"}, headers=headers
    )
    assert (confirmed.json()["status"], confirmed.json()["due_date"]) == ("confirmed", "2026-09-24")


# ---------------------------------------------------------------- 确认时挂候选（R07-8、R07-14）


def make_cvm_world(tmp_path, monkeypatch):
    """CVM 云讲堂 09-29 那场会抽出候选「科室会预约后台导出」和两条任务（任务都没挂候选）。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_world(db)
    db.execute("UPDATE app_state SET value=? WHERE key='requirement_candidates_since'", (LAUNCH,))
    seed_minutes(db, "cvm")
    fake_ai(monkeypatch, reply(EXPORT, tasks=[SCRIPT_TASK, CHASE_TASK]))
    scan(db, settings, "cvm")
    candidate_id = db.query_one(
        "SELECT id FROM requirement_candidates WHERE title='科室会预约后台导出'"
    )["id"]
    tasks = {row["title"]: row["id"] for row in db.query_all("SELECT id, title FROM tasks")}
    return client, headers, db, candidate_id, tasks


def test_confirm_hangs_task_on_candidate_until_claimed(tmp_path, monkeypatch):
    """确认时挂到同场会的候选：任务带候选名；候选认领后任务正式挂上需求，候选清空。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase = tasks["催填节后三场信息"]

    response = client.post(
        f"/api/tasks/{chase}/confirm", json={"candidate_id": candidate_id}, headers=headers
    )

    assert response.status_code == 200, response.text
    task = response.json()
    assert (task["status"], task["candidate_id"], task["candidate_title"]) == (
        "confirmed",
        candidate_id,
        "科室会预约后台导出",
    )
    assert task["requirement_id"] is None
    kinds = [event["kind"] for event in task["events"]]
    # 撤销确认认「最后一条就是确认本身」：挂候选的留痕写在确认之前
    assert kinds[-2:] == ["requirement_changed", "confirmed"]
    assert task["events"][-2]["body"] == "挂到候选「科室会预约后台导出」"
    assert client.post(
        "/api/tasks/undo-review", json={"task_ids": [chase]}, headers=headers
    ).json()["reverted"] == [chase]

    client.post(f"/api/tasks/{chase}/confirm", json={"candidate_id": candidate_id}, headers=headers)
    claimed = client.post(
        f"/api/requirement-candidates/{candidate_id}/claim",
        json={"title": "科室会预约后台导出"},
        headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    after = client.get(f"/api/tasks/{chase}").json()
    assert (after["requirement_id"], after["candidate_id"], after["candidate_title"]) == (
        claimed.json()["id"],
        None,
        None,
    )


def test_candidate_scope_and_exclusivity(tmp_path, monkeypatch):
    """候选只限任务所属项目；挂需求和挂候选二选一；处理过的候选不能再挂；改了项目候选跟着移出。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    script = tasks["话术修改稿发执行群走默示确认"]
    requirement = client.post(
        "/api/requirements",
        json={"project_id": project_id("cvm"), "title": "直播间运营六项修正", "priority": "P1"},
        headers=headers,
    ).json()

    both = client.post(
        f"/api/tasks/{script}/confirm",
        json={"candidate_id": candidate_id, "requirement_id": requirement["id"]},
        headers=headers,
    )
    assert both.status_code == 400 and "同时" in both.json()["detail"]

    hung = client.patch(
        f"/api/tasks/{script}", json={"candidate_id": candidate_id}, headers=headers
    )
    assert hung.json()["candidate_id"] == candidate_id
    # 改挂需求：候选让位
    moved = client.patch(
        f"/api/tasks/{script}", json={"requirement_id": requirement["id"]}, headers=headers
    )
    assert (moved.json()["requirement_id"], moved.json()["candidate_id"]) == (
        requirement["id"],
        None,
    )
    # 改挂候选：原来的需求让位
    back = client.patch(
        f"/api/tasks/{script}", json={"candidate_id": candidate_id}, headers=headers
    ).json()
    assert (back["requirement_id"], back["candidate_id"]) == (None, candidate_id)
    assert [event["body"] for event in back["events"][-2:]] == [
        "移出需求「直播间运营六项修正」",
        "挂到候选「科室会预约后台导出」",
    ]
    # 选「不挂」：需求和候选都清空
    none = client.patch(
        f"/api/tasks/{script}", json={"requirement_id": None, "candidate_id": None}, headers=headers
    ).json()
    assert (none["requirement_id"], none["candidate_id"]) == (None, None)
    assert none["events"][-1]["body"] == "移出候选「科室会预约后台导出」"

    # 别的项目下的任务不能挂这条候选
    other = client.post(
        "/api/tasks",
        json={"title": "复测发卡与核销链路", "project_id": project_id("yimi")},
        headers=headers,
    ).json()
    outside = client.patch(
        f"/api/tasks/{other['id']}", json={"candidate_id": candidate_id}, headers=headers
    )
    assert outside.status_code == 400 and "所属项目" in outside.json()["detail"]
    # 没归项目的任务只能挂同一场会的候选
    loose = client.post(
        "/api/tasks", json={"title": "核查剪辑导出缺医院、科室字段问题"}, headers=headers
    ).json()
    stranger = client.patch(
        f"/api/tasks/{loose['id']}", json={"candidate_id": candidate_id}, headers=headers
    )
    assert stranger.status_code == 400 and "同一场会" in stranger.json()["detail"]

    # 改了项目：原来挂的候选不在新项目里，一并移出
    client.patch(f"/api/tasks/{script}", json={"candidate_id": candidate_id}, headers=headers)
    rehomed = client.patch(
        f"/api/tasks/{script}", json={"project_id": project_id("yimi")}, headers=headers
    ).json()
    assert (rehomed["project_id"], rehomed["candidate_id"]) == (project_id("yimi"), None)

    missing = client.patch(
        f"/api/tasks/{script}", json={"candidate_id": "candidate-nope"}, headers=headers
    )
    assert missing.status_code == 404
    client.post(f"/api/requirement-candidates/{candidate_id}/drop", json={}, headers=headers)
    chase = tasks["催填节后三场信息"]
    dropped = client.patch(
        f"/api/tasks/{chase}", json={"candidate_id": candidate_id}, headers=headers
    )
    assert dropped.status_code == 409


# ---------------------------------------------------------------- 撤销完成（R06-11、R07-12）


def complete(client, headers, task_id):
    response = client.post(f"/api/tasks/{task_id}/status", json={"status": "done"}, headers=headers)
    assert response.json()["status"] == "done", response.text


def undo_complete(client, headers, *task_ids):
    return client.post(
        "/api/tasks/undo-complete", json={"task_ids": list(task_ids)}, headers=headers
    ).json()


def test_undo_complete_restores_status_and_stall_clock(tmp_path, monkeypatch):
    """完成后 10 分钟内撤销：回到完成前的已确认或进行中，停滞计时恢复成完成前的时刻。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase, script = tasks["催填节后三场信息"], tasks["话术修改稿发执行群走默示确认"]
    for task_id in (chase, script):
        client.post(f"/api/tasks/{task_id}/confirm", json={}, headers=headers)
    client.post(f"/api/tasks/{script}/status", json={"status": "in_progress"}, headers=headers)
    before = {
        row["id"]: row["status_changed_at"]
        for row in db.query_all("SELECT id, status_changed_at FROM tasks")
    }
    complete(client, headers, chase)
    complete(client, headers, script)

    result = undo_complete(client, headers, chase, script, "task-nope")

    assert result["reverted"] == [chase, script]
    assert result["failed"] == [{"task_id": "task-nope", "error": "任务不存在"}]
    rows = {row["id"]: row for row in db.query_all("SELECT * FROM tasks")}
    assert (rows[chase]["status"], rows[script]["status"]) == ("confirmed", "in_progress")
    assert rows[chase]["status_changed_at"] == before[chase]
    assert rows[script]["status_changed_at"] == before[script]
    # 撤销过一次，再撤就不认了
    assert undo_complete(client, headers, chase)["failed"][0]["error"] == "只能撤销刚刚的完成"


def test_undo_complete_refuses_stale_or_touched_tasks(tmp_path, monkeypatch):
    """过了撤销窗口、完成后又加了备注、根本没完成的，都不能撤销。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase, script = tasks["催填节后三场信息"], tasks["话术修改稿发执行群走默示确认"]
    for task_id in (chase, script):
        client.post(f"/api/tasks/{task_id}/confirm", json={}, headers=headers)
    complete(client, headers, chase)
    client.post(f"/api/tasks/{chase}/comments", json={"body": "已在群里催过"}, headers=headers)

    assert undo_complete(client, headers, chase, script)["failed"] == [
        {"task_id": chase, "error": "只能撤销刚刚的完成"},
        {"task_id": script, "error": "只能撤销刚刚的完成"},
    ]

    complete(client, headers, script)
    monkeypatch.setattr(tasks_module, "UNDO_WINDOW_SECONDS", -1)
    assert undo_complete(client, headers, script)["reverted"] == []
    assert db.query_one("SELECT status FROM tasks WHERE id=?", (script,)) == {"status": "done"}


def test_undo_complete_twice_keeps_the_original_stall_clock(tmp_path, monkeypatch):
    """完成 → 撤销 → 再完成 → 再撤销：停滞计时两次都回到确认那一刻，不落到第一次撤销的时刻。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)
    confirmed_at = db.query_one("SELECT status_changed_at FROM tasks WHERE id=?", (chase,))

    for _round in range(2):
        complete(client, headers, chase)
        assert undo_complete(client, headers, chase)["reverted"] == [chase]
        assert db.query_one("SELECT status_changed_at FROM tasks WHERE id=?", (chase,)) == (
            confirmed_at
        )


# ---------------------------------------------------------------- 会议改归属时挂候选的任务


def test_moving_a_meeting_leaves_tasks_hung_on_other_meetings_candidates(tmp_path, monkeypatch):
    """医米「EDC 系统选型」那场会里的任务挂了京东那场会抽出的候选：EDC 会改归恒瑞时，任务留在医米、
    候选照挂（候选跟着京东那场会，不搬家）；同一场会抽出的候选挂着的任务照常随会走。"""
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_world(db)
    db.execute("UPDATE app_state SET value=? WHERE key='requirement_candidates_since'", (LAUNCH,))
    seed_minutes(db, "jd")
    fake_ai(monkeypatch, reply(RECEIPT))
    scan(db, settings, "jd")
    receipt = db.query_one("SELECT id FROM requirement_candidates WHERE title='京东仓签收凭证'")[
        "id"
    ]
    edc_task = client.post(
        "/api/tasks",
        json={"title": "确认产研能否派一人对接 EDC", "project_id": project_id("yimi")},
        headers=headers,
    ).json()["id"]
    db.execute("UPDATE tasks SET meeting_id=? WHERE id=?", (meeting_id("edc"), edc_task))
    client.patch(f"/api/tasks/{edc_task}", json={"candidate_id": receipt}, headers=headers)

    moved = client.patch(
        f"/api/meetings/{meeting_id('edc')}",
        json={"project_id": project_id("hengrui")},
        headers=headers,
    )

    assert moved.status_code == 200, moved.text
    assert db.query_one("SELECT project_id, candidate_id FROM tasks WHERE id=?", (edc_task,)) == {
        "project_id": project_id("yimi"),
        "candidate_id": receipt,
    }
