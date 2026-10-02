"""项目页与待办改版·待办后端（R07）：按截止分组、挂需求的推荐与范围、待确认按会议的审核卡、全部确认。

任务 id、名字、执行方、时间锚照生产库（261001 只读取出）；截止是用例给的输入。「今天」钉在口径书示意数据的
2026-09-30（周三，北京日期），不取真实当前时间。
"""

from __future__ import annotations

from datetime import date

from meeting_workbench import todo
from meeting_workbench.db import Database, utc_now
from meeting_workbench.tasks import TaskService
from meeting_workbench.todo import group_of

from .requirement_pool_world import meeting_id, project_id, seed_world
from .test_task_due import CARD_MEETING, CARD_PROJECT, make_cvm_world, seed_card_meeting
from .test_tasks_api import make_client, write_headers

TODAY = date(2026, 9, 30)


def insert_task(
    db,
    task_id,
    title,
    *,
    status="confirmed",
    meeting,
    project,
    due=None,
    anchor_ms=None,
    assignee="me",
    created_at=None,
):
    now = created_at or utc_now()
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, assignee, meeting_id, project_id, anchor_ms,
                             due_date, status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, title, status, assignee, meeting, project, anchor_ms, due, now, now, now),
    )


def make_todo_world(tmp_path, monkeypatch):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_world(db)
    seed_card_meeting(db)
    monkeypatch.setattr(todo, "beijing_today", lambda now=None: TODAY)
    yimi, edc = project_id("yimi"), meeting_id("edc")
    cvm, cvm_meeting = project_id("cvm"), meeting_id("cvm")
    insert_task(
        db,
        "task-5172818d4e3f4190b1a7f46efad1b8ab",
        "明天晚上先上后台并撤下码，统一验证扫码",
        meeting=CARD_MEETING,
        project=CARD_PROJECT,
        due="2026-09-24",
        anchor_ms=696890,
    )
    insert_task(
        db,
        "task-36ee9fa9dda2428ba88b5b56234a8075",
        "「人身健康」明天开发完、28号再测",
        meeting=CARD_MEETING,
        project=CARD_PROJECT,
        due="2026-09-28",
        anchor_ms=380190,
        assignee="ai",
    )
    insert_task(
        db,
        "task-1c339237d4e24358bc723779ca1fb843",
        "「一直拍」15号版本先开发，开关默认关闭",
        meeting=CARD_MEETING,
        project=CARD_PROJECT,
        due="2026-09-30",
        anchor_ms=1240850,
        assignee="ai",
    )
    insert_task(
        db,
        "task-c68e743db0214a95887a207840b911c8",
        "与萌总同步 EDC 选型议题",
        status="in_progress",
        meeting=edc,
        project=yimi,
        due="2026-10-04",
        anchor_ms=59980,
    )
    insert_task(
        db,
        "task-8c980183bab647cbb253f380ed1c1c11",
        "确认产研能否派一人对接 EDC",
        meeting=edc,
        project=yimi,
        due="2026-10-05",
        anchor_ms=74730,
    )
    insert_task(
        db,
        "task-e8898a3468a34806a27ca418ec82358f",
        "安排与华谊的会",
        meeting=edc,
        project=yimi,
        anchor_ms=14610,
    )
    insert_task(
        db,
        "task-40e38c575b0a4a71b638be66e22631b4",
        "话术修改稿发执行群走默示确认",
        meeting=cvm_meeting,
        project=cvm,
        anchor_ms=718230,
    )
    # 不进待办的：已完成、待确认
    insert_task(
        db,
        "task-ba10ecb1d8344c5d821aaf9a2c115878",
        "催填节后三场信息",
        status="done",
        meeting=cvm_meeting,
        project=cvm,
        due="2026-09-29",
    )
    insert_task(
        db,
        "task-c0e4a87bea824645bdaf2d1fdd48d731",
        "持续盯预约场次并在群里动员报名",
        status="pending_confirm",
        meeting=cvm_meeting,
        project=cvm,
        anchor_ms=487620,
    )
    return client, headers, db


def titles(group):
    return [task["title"] for task in group["items"]]


# ---------------------------------------------------------------- 待办按截止分组（R07-4）


def test_group_boundaries_follow_the_beijing_week():
    """本周＝明天到本周日；周日那天「本周」是空的，下周一起算「之后」。"""
    wednesday = date(2026, 9, 30)
    assert [
        group_of(due, wednesday)
        for due in (
            None,
            "2026-09-29",
            "2026-09-30",
            "2026-10-01",
            "2026-10-04",
            "2026-10-05",
            "坏",
        )
    ] == ["undated", "overdue", "today", "week", "week", "later", "undated"]
    sunday = date(2026, 10, 4)
    assert [group_of(due, sunday) for due in ("2026-10-03", "2026-10-04", "2026-10-05")] == [
        "overdue",
        "today",
        "later",
    ]


def test_todo_groups_open_tasks_by_due(tmp_path, monkeypatch):
    """只收已确认、进行中；组内按截止由早到晚，未定截止按来源会议由近到远；空组照常给出。"""
    client, headers, db = make_todo_world(tmp_path, monkeypatch)

    payload = client.get("/api/todo").json()

    assert (payload["today"], payload["week_end"], payload["total"]) == (
        "2026-09-30",
        "2026-10-04",
        7,
    )
    groups = {group["key"]: group for group in payload["groups"]}
    assert [group["key"] for group in payload["groups"]] == [
        "overdue",
        "today",
        "week",
        "later",
        "undated",
    ]
    assert titles(groups["overdue"]) == [
        "明天晚上先上后台并撤下码，统一验证扫码",
        "「人身健康」明天开发完、28号再测",
    ]
    assert titles(groups["today"]) == ["「一直拍」15号版本先开发，开关默认关闭"]
    assert titles(groups["week"]) == ["与萌总同步 EDC 选型议题"]
    assert titles(groups["later"]) == ["确认产研能否派一人对接 EDC"]
    # CVM 那场会（09-29）比 EDC 那场（09-28）近
    assert titles(groups["undated"]) == ["话术修改稿发执行群走默示确认", "安排与华谊的会"]
    assert [group["count"] for group in payload["groups"]] == [2, 1, 1, 1, 2]
    task = groups["overdue"]["items"][0]
    assert (task["meeting_title"], task["anchor_ms"], task["project_name"]) == (
        "发卡变更与一直拍流程改造沟通",
        696890,
        "黑卡小程序",
    )
    # 别的页签的数字照任务池口径
    assert payload["counts"]["pending_confirm"] == 1 and payload["counts"]["done"] == 1


def test_todo_filters_like_the_task_pool(tmp_path, monkeypatch):
    """筛选沿用任务池：所属项目、执行方、任务名；空组照常显示 0；日期格式不对报 400。"""
    client, headers, db = make_todo_world(tmp_path, monkeypatch)

    yimi = client.get("/api/todo", params={"project_id": project_id("yimi")}).json()
    assert yimi["total"] == 3
    assert [group["count"] for group in yimi["groups"]] == [0, 0, 1, 1, 1]
    ai = client.get("/api/todo", params={"assignee": "ai"}).json()
    assert ai["total"] == 2
    several = client.get(
        "/api/todo", params={"project_id": f"{project_id('cvm')},{CARD_PROJECT}"}
    ).json()
    assert several["total"] == 4
    assert client.get("/api/todo", params={"project_id": "none"}).json()["total"] == 0
    assert [project["name"] for project in yimi["projects"]][:2] == ["CVM 云讲堂", "医米科研用药"]
    named = client.get("/api/todo", params={"q": "EDC"}).json()
    assert named["total"] == 2
    assert client.get("/api/todo", params={"meeting_date_from": "09-01"}).status_code == 400


# ---------------------------------------------------------------- 挂到需求的推荐与范围（R07-8、R07-14）


def link_meeting(client, headers, requirement_id, meeting):
    response = client.post(
        f"/api/requirements/{requirement_id}/meetings/{meeting}", json={}, headers=headers
    )
    assert response.status_code == 200, response.text


def create_requirement(client, headers, key, title, priority="P1"):
    response = client.post(
        "/api/requirements",
        json={"project_id": project_id(key), "title": title, "priority": priority},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def options(client, task_id, **params):
    response = client.get(f"/api/tasks/{task_id}/requirement-options", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_confirming_recommends_the_ai_pairing_then_linked_then_same_meeting(tmp_path, monkeypatch):
    """CVM 09-29 那场会：「直播间运营六项修正」已关联这场会，抽出候选「科室会预约后台导出」。
    AI 配了候选的任务默认选候选；没配的默认选已关联的需求；别的项目的需求不出现。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    link_meeting(client, headers, six, meeting_id("cvm"))
    create_requirement(client, headers, "yimi", "京东科研仓对接", "P0")
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    db.execute("UPDATE tasks SET candidate_id=? WHERE id=?", (candidate_id, script))

    paired = options(client, script)
    assert [(item["kind"], item["title"], item["reason"]) for item in paired["recommended"]] == [
        ("candidate", "科室会预约后台导出", "paired"),
        ("requirement", "直播间运营六项修正", "linked"),
    ]
    assert paired["default"]["id"] == candidate_id
    assert paired["current"] == {"kind": "candidate", "id": candidate_id}
    assert paired["can_link_candidates"] is True
    assert [item["title"] for item in paired["options"]] == [
        "直播间运营六项修正",
        "科室会预约后台导出",
    ]

    plain = options(client, chase)
    assert [item["reason"] for item in plain["recommended"]] == ["linked", "same_meeting"]
    assert plain["default"]["id"] == six
    assert [item["title"] for item in options(client, chase, q="导出")["options"]] == [
        "科室会预约后台导出"
    ]


def test_confirmed_tasks_only_link_active_requirements(tmp_path, monkeypatch):
    """已确认的任务在待办里「挂到需求」：只列所属项目的进行中需求，不列候选（R07-14）；
    没归项目的任务只列同一场会的候选和已关联这场会的需求。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    shelved = create_requirement(client, headers, "cvm", "直播间画面比例缩到 95%")
    client.patch(f"/api/requirements/{shelved}", json={"status": "shelved"}, headers=headers)
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)

    confirmed = options(client, chase)
    assert confirmed["can_link_candidates"] is False
    assert [item["id"] for item in confirmed["options"]] == [six]
    assert confirmed["recommended"] == [] and confirmed["default"] is None

    db.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (chase,))
    loose = options(client, chase)
    assert [item["title"] for item in loose["options"]] == ["科室会预约后台导出"]
    link_meeting(client, headers, six, meeting_id("cvm"))
    assert [item["title"] for item in options(client, chase)["options"]] == [
        "直播间运营六项修正",
        "科室会预约后台导出",
    ]


def test_handled_candidates_are_not_recommended(tmp_path, monkeypatch):
    """推荐的候选在确认前被丢掉：不再推荐；被合并：合并进去的需求关联了这场会，改推荐那条需求。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/requirement-candidates/{candidate_id}/drop", json={}, headers=headers)
    assert options(client, chase)["recommended"] == []

    client.post(f"/api/requirement-candidates/{candidate_id}/restore", json={}, headers=headers)
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    merged = client.post(
        f"/api/requirement-candidates/{candidate_id}/merge",
        json={"requirement_id": six},
        headers=headers,
    )
    assert merged.status_code == 200, merged.text
    recommended = options(client, chase)["recommended"]
    assert [(item["id"], item["reason"]) for item in recommended] == [(six, "linked")]


# ---------------------------------------------------------------- 待确认按会议审核（R07-9～11、16）


def cards(client, **params):
    response = client.get("/api/review-cards", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_review_cards_date_range_includes_the_end_day(tmp_path, monkeypatch):
    """审核卡按「会议日期 至 X」筛：X 当天的会也在（口径同任务池，按 recording_date 前 10 位的日期）。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    day = db.query_one("SELECT recording_date FROM meetings WHERE id=?", (meeting_id("cvm"),))[
        "recording_date"
    ][:10]

    payload = cards(client, meeting_date_from=day, meeting_date_to=day)

    assert [card["meeting"]["id"] for card in payload["cards"]] == [meeting_id("cvm")]


def test_review_card_lists_candidates_and_drafts_of_one_meeting(tmp_path, monkeypatch):
    """每场会一张卡：候选和待确认任务都在，每条任务带默认推荐；逐条确认后任务行留在卡里。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)

    payload = cards(client)

    assert (payload["pending_task_count"], payload["pending_candidate_count"]) == (2, 1)
    [card] = payload["cards"]
    assert card["meeting"]["title"] == "260929 云课堂直播运营问题对齐"
    assert [item["title"] for item in card["candidates"]] == ["科室会预约后台导出"]
    assert {task["title"]: task["recommended"][0]["title"] for task in card["tasks"]} == {
        "话术修改稿发执行群走默示确认": "科室会预约后台导出",
        "催填节后三场信息": "科室会预约后台导出",
    }
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={"candidate_id": candidate_id}, headers=headers)
    [card] = cards(client)["cards"]
    assert {task["title"]: task["status"] for task in card["tasks"]} == {
        "话术修改稿发执行群走默示确认": "pending_confirm",
        "催填节后三场信息": "confirmed",
    }
    confirmed = next(task for task in card["tasks"] if task["id"] == chase)
    assert (confirmed["candidate_title"], confirmed["recommended"]) == ("科室会预约后台导出", [])
    assert (card["pending_task_count"], card["done"]) == (1, False)


def test_confirm_all_links_each_default_and_can_be_undone(tmp_path, monkeypatch):
    """全部确认：每条按自己的默认推荐挂上；候选不受影响、卡还在；撤销沿用确认撤销。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    link_meeting(client, headers, six, meeting_id("cvm"))
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    db.execute("UPDATE tasks SET candidate_id=? WHERE id=?", (candidate_id, script))

    result = client.post(
        f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers
    ).json()

    assert sorted(result["confirmed"]) == sorted([script, chase]) and result["failed"] == []
    rows = {row["id"]: row for row in db.query_all("SELECT * FROM tasks")}
    assert (rows[script]["status"], rows[script]["candidate_id"]) == ("confirmed", candidate_id)
    assert (rows[chase]["status"], rows[chase]["requirement_id"]) == ("confirmed", six)
    [card] = cards(client)["cards"]
    assert (card["pending_task_count"], card["pending_candidate_count"], card["done"]) == (
        0,
        1,
        False,
    )
    undone = client.post(
        "/api/tasks/undo-review", json={"task_ids": result["confirmed"]}, headers=headers
    ).json()
    assert sorted(undone["reverted"]) == sorted([script, chase])
    assert cards(client)["pending_task_count"] == 2
    missing = client.post("/api/review-cards/vm-nope/confirm-all", json={}, headers=headers)
    assert missing.status_code == 404


def test_confirm_all_isolates_a_task_whose_pick_went_stale(tmp_path, monkeypatch):
    """推荐的候选在这期间被处理了：那一条失败、留在待确认，其余照常确认。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    stale = {"kind": "candidate", "id": candidate_id, "title": "科室会预约后台导出"}
    original = todo.recommend
    monkeypatch.setattr(
        todo,
        "recommend",
        lambda connection, task, opts: (
            [stale] if task["id"] == chase else original(connection, task, opts)
        ),
    )
    db.execute(
        "UPDATE requirement_candidates SET status='dropped', dropped_at=? WHERE id=?",
        (utc_now(), candidate_id),
    )

    result = client.post(
        f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers
    ).json()

    assert result["confirmed"] == [script]
    assert [item["task_id"] for item in result["failed"]] == [chase]
    assert db.query_one("SELECT status FROM tasks WHERE id=?", (chase,)) == {
        "status": "pending_confirm"
    }


def test_finished_cards_sink_and_leave_after_seven_days(tmp_path, monkeypatch):
    """候选和任务都处理完的卡置灰沉底（排在更早的待处理卡后面）；处理满 7 天的不再列；
    过期的任务从卡里移除，只剩过期任务的会不出卡。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    insert_task(
        db,
        "task-jd-draft",
        "京东仓入库单推送对接",
        status="pending_confirm",
        meeting=meeting_id("jd"),
        project=project_id("yimi"),
    )
    client.post(f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers)
    client.post(f"/api/requirement-candidates/{candidate_id}/drop", json={}, headers=headers)

    payload = cards(client)

    assert [(card["meeting"]["title"], card["done"]) for card in payload["cards"]] == [
        ("260916 医米京东科研仓系统对接", False),
        ("260929 云课堂直播运营问题对齐", True),
    ]
    assert [
        card["meeting"]["title"] for card in cards(client, project_id=project_id("cvm"))["cards"]
    ] == ["260929 云课堂直播运营问题对齐"]
    # 处理满 7 天：把保留天数调成负的，等同于处理时刻已在窗口之外
    monkeypatch.setattr(todo, "DONE_CARD_DAYS", -1)
    assert [card["meeting"]["title"] for card in cards(client)["cards"]] == [
        "260916 医米京东科研仓系统对接"
    ]
    monkeypatch.setattr(todo, "DONE_CARD_DAYS", 7)
    db.execute("UPDATE tasks SET status='expired' WHERE id='task-jd-draft'")
    assert [card["meeting"]["title"] for card in cards(client)["cards"]] == [
        "260929 云课堂直播运营问题对齐"
    ]
    assert client.get("/api/review-cards", params={"meeting_date_to": "坏"}).status_code == 400
    both = cards(client, project_id=f"{project_id('cvm')},{project_id('yimi')},none")
    assert len(both["cards"]) == 1
    assert cards(client, project_id="none")["cards"] == []


def test_current_link_comes_first_and_confirm_all_keeps_it(tmp_path, monkeypatch):
    """任务现在挂着的排第一、全部确认不改它：候选被合并后任务随它挂上的目标需求（即使这场会还关联着
    更高等级的需求），和确认前手动挂上的需求，都原样保留。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    screen = create_requirement(client, headers, "cvm", "直播画面比例缩到 95%", "P0")
    link_meeting(client, headers, screen, meeting_id("cvm"))
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正", "P2")
    db.execute("UPDATE tasks SET candidate_id=? WHERE id=?", (candidate_id, script))
    client.post(
        f"/api/requirement-candidates/{candidate_id}/merge",
        json={"requirement_id": six},
        headers=headers,
    )
    client.patch(f"/api/tasks/{chase}", json={"requirement_id": screen}, headers=headers)

    merged = options(client, script)
    assert (merged["default"]["id"], merged["default"]["reason"]) == (six, "current")
    assert options(client, chase)["default"]["reason"] == "current"

    result = client.post(
        f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers
    ).json()

    assert sorted(result["confirmed"]) == sorted([script, chase])
    assert result["linked"][script]["id"] == six
    rows = {row["id"]: row for row in db.query_all("SELECT * FROM tasks")}
    assert (rows[script]["status"], rows[script]["requirement_id"]) == ("confirmed", six)
    assert (rows[chase]["status"], rows[chase]["requirement_id"]) == ("confirmed", screen)


def test_confirm_all_leaves_tasks_someone_else_just_handled(tmp_path, monkeypatch):
    """全部确认跑到一半时：别处刚驳回的保持驳回；别处刚确认、选了不挂的不被改挂，那次确认照样能撤销。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    original = TaskService._confirm
    raced: list[str] = []

    def racing_confirm(self, task_id, **kwargs):
        if not raced:
            raced.append(task_id)
            client.post(f"/api/tasks/{chase}/reject", json={}, headers=headers)
            client.post(
                f"/api/tasks/{script}/confirm",
                json={"requirement_id": None, "candidate_id": None},
                headers=headers,
            )
        return original(self, task_id, **kwargs)

    monkeypatch.setattr(TaskService, "_confirm", racing_confirm)

    result = client.post(
        f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers
    ).json()

    assert result == {"confirmed": [], "failed": [], "linked": {}}
    rows = {row["id"]: row for row in db.query_all("SELECT * FROM tasks")}
    assert rows[chase]["status"] == "cancelled"
    assert (
        rows[script]["status"],
        rows[script]["candidate_id"],
        rows[script]["requirement_id"],
    ) == (
        "confirmed",
        None,
        None,
    )
    assert client.post(
        "/api/tasks/undo-review", json={"task_ids": [script]}, headers=headers
    ).json()["reverted"] == [script]


def test_due_in_odd_iso_shapes_is_undated():
    assert group_of("20261001", TODAY) == "undated"
    assert group_of("2026-W40-4", TODAY) == "undated"


def test_confirm_all_reports_unexpected_errors_per_task(tmp_path, monkeypatch):
    """推荐那步出了没想到的错：只算那一条失败，其余照常确认，接口不整个报错。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    original = todo.recommend

    def broken(connection, task, opts):
        if task["id"] == chase:
            raise KeyError("模拟推荐出错")
        return original(connection, task, opts)

    monkeypatch.setattr(todo, "recommend", broken)

    response = client.post(
        f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["confirmed"] == [script]
    assert [item["task_id"] for item in response.json()["failed"]] == [chase]
    assert db.query_one("SELECT status FROM tasks WHERE id=?", (chase,)) == {
        "status": "pending_confirm"
    }


def test_reject_only_takes_drafts(tmp_path, monkeypatch):
    """页面数据旧了：别处刚确认的任务点「驳回」不会被取消（409），已确认还是已确认；截止格式不对报中文。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)

    rejected = client.post(f"/api/tasks/{chase}/reject", json={}, headers=headers)

    assert rejected.status_code == 409
    assert db.query_one("SELECT status FROM tasks WHERE id=?", (chase,)) == {"status": "confirmed"}
    bad = client.patch(f"/api/tasks/{chase}", json={"due_date": "202610-01-15"}, headers=headers)
    assert bad.status_code == 400 and bad.json()["detail"] == "截止日期格式应为 YYYY-MM-DD"


def test_transition_errors_name_statuses_in_chinese(tmp_path, monkeypatch):
    """页面会原样显示后端的报错：状态写中文，不露 done、confirmed 这类内部名字。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)
    client.post(f"/api/tasks/{chase}/status", json={"status": "done"}, headers=headers)

    response = client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "任务已经是「已完成」，不能改成「已确认」，刷新后再看"


def test_undoing_a_confirm_takes_back_the_link_it_made(tmp_path, monkeypatch):
    """撤销确认时，确认时挂上的需求一起撤回（用户 261001 拍板）：AI 配好候选的那条回到挂候选，
    没挂过的回到不挂；全部确认的撤销也一样。确认前就挂着、确认没动它的，撤销后照样挂着。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    link_meeting(client, headers, six, meeting_id("cvm"))
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    db.execute("UPDATE tasks SET candidate_id=? WHERE id=?", (candidate_id, script))

    client.post(f"/api/tasks/{script}/confirm", json={"requirement_id": six}, headers=headers)
    client.post(f"/api/tasks/{script}/reject", json={}, headers=headers)  # 已确认的不能驳回，不影响
    undone = client.post(
        "/api/tasks/undo-review", json={"task_ids": [script]}, headers=headers
    ).json()
    assert undone["reverted"] == [script]
    row = db.query_one(
        "SELECT status, requirement_id, candidate_id, confirm_undo FROM tasks WHERE id=?", (script,)
    )
    assert row == {
        "status": "pending_confirm",
        "requirement_id": None,
        "candidate_id": candidate_id,
        "confirm_undo": None,
    }
    events = client.get(f"/api/tasks/{script}").json()["events"]
    assert [event["body"] for event in events[-2:]] == [
        "撤销确认：挂接回到确认前的样子",
        "撤销上一步，恢复为待确认",
    ]

    result = client.post(
        f"/api/review-cards/{meeting_id('cvm')}/confirm-all", json={}, headers=headers
    ).json()
    assert db.query_one("SELECT requirement_id FROM tasks WHERE id=?", (chase,)) == {
        "requirement_id": six
    }
    client.post("/api/tasks/undo-review", json={"task_ids": result["confirmed"]}, headers=headers)
    assert db.query_all("SELECT id, requirement_id, candidate_id FROM tasks ORDER BY id") == sorted(
        [
            {"id": chase, "requirement_id": None, "candidate_id": None},
            {"id": script, "requirement_id": None, "candidate_id": candidate_id},
        ],
        key=lambda row: row["id"],
    )


def test_undo_does_not_hang_back_on_a_handled_candidate(tmp_path, monkeypatch):
    """确认时从 AI 配好的候选改挂需求，候选随后被丢掉：撤销确认时需求撤回，但不挂回丢掉的候选。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    six = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    script = tasks["话术修改稿发执行群走默示确认"]
    db.execute("UPDATE tasks SET candidate_id=? WHERE id=?", (candidate_id, script))
    client.post(f"/api/tasks/{script}/confirm", json={"requirement_id": six}, headers=headers)
    client.post(f"/api/requirement-candidates/{candidate_id}/drop", json={}, headers=headers)

    client.post("/api/tasks/undo-review", json={"task_ids": [script]}, headers=headers)

    assert db.query_one(
        "SELECT status, requirement_id, candidate_id FROM tasks WHERE id=?", (script,)
    ) == {
        "status": "pending_confirm",
        "requirement_id": None,
        "candidate_id": None,
    }
