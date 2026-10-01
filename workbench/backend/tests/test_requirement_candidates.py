"""需求池改版 v17：需求候选的认领、合并、丢掉和撤销（R01）。

候选、会议、原话照《口径书》和生产库逐字稿（见 requirement_pool_world）；挂在候选上的任务用那场会
真实抽出的任务标题和锚点原话。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meeting_workbench.db import utc_now
from meeting_workbench.requirement_candidates import (
    insert_candidate,
    list_dropped,
    restore_candidate,
)
from meeting_workbench.service import ConflictError

from .requirement_pool_world import QUOTES, meeting_id, project_id
from .test_requirement_pool import (
    JD_SUMMARY,
    candidate,
    create,
    make_world,
    titles,
    wall,
)

EXPORT_SUMMARY = "预约审核页现在不能导出 Excel，运营要把科室会预约名单拉出来对表。"
RECEIPT_SUMMARY = (
    "京东妥投只靠配送员点击，医米拿不到患者签收凭证；月结上千万物流费不能只凭汇总表付款。"
)


def post(client, headers, path, body=None):
    return client.post(path, json=body or {}, headers=headers)


def task_on_candidate(db, task_id, title, anchor_ms, anchor_quote, candidate_id, **extra):
    """会后抽取挂在候选上的任务草稿（那场会真实抽出的任务）。"""
    now = utc_now()
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, assignee, meeting_id, project_id,
                             requirement_id, candidate_id, anchor_ms, anchor_quote,
                             status_changed_at, created_at, updated_at)
           VALUES (?, ?, 'pending_confirm', 'ai', 'me', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            title,
            meeting_id("cvm"),
            project_id("cvm"),
            extra.get("requirement_id"),
            candidate_id,
            anchor_ms,
            anchor_quote,
            now,
            now,
            now,
        ),
    )


# ---------------------------------------------------------------- 认领


def test_claim_builds_active_requirement_with_source_meeting_and_tasks(tmp_path):
    client, headers, db = make_world(tmp_path)
    other = create(client, headers, "cvm", "直播间运营六项修正", "P1", meeting_keys=("cvm",))
    candidate_id = candidate(db, "export", "科室会预约后台导出", summary=EXPORT_SUMMARY)
    task_on_candidate(
        db,
        "task-mobilize",
        "持续盯预约场次并在群里动员报名",
        487620,
        "我会同步的去实时的去看这个场次预约，然后尽量多次的在群里动员大家去去去报名。",
        candidate_id,
    )
    # 已经手动挂到别的需求上的任务，认领时不跟着走
    task_on_candidate(
        db,
        "task-script",
        "话术修改稿发执行群走默示确认",
        718230,
        "那个就直接呃甩到执行群里，然后说对应的同事检查一下。",
        candidate_id,
        requirement_id=other,
    )
    pool = wall(client, status="pending")
    assert pool["items"][0]["open_task_count"] == 1

    response = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/claim",
        {
            "title": "科室会预约名单导出",
            "summary": EXPORT_SUMMARY,
            "project_id": project_id("cvm"),
        },
    )

    assert response.status_code == 200, response.text
    detail = response.json()
    assert (detail["title"], detail["status"], detail["priority"]) == (
        "科室会预约名单导出",
        "active",
        "P2",
    )
    assert detail["summary"] == EXPORT_SUMMARY
    assert detail["source"]["kind"] == "origin"
    assert (detail["source"]["quote"], detail["source"]["anchor_ms"]) == QUOTES["export"][1:]
    assert [meeting["id"] for meeting in detail["meetings"]] == [meeting_id("cvm")]
    assert [task["id"] for task in detail["tasks"]] == ["task-mobilize"]
    moved = db.query_one(
        "SELECT requirement_id, project_id, candidate_id FROM tasks WHERE id='task-mobilize'"
    )
    assert moved == {
        "requirement_id": detail["id"],
        "project_id": project_id("cvm"),
        "candidate_id": None,
    }
    assert db.query_one("SELECT body FROM task_events WHERE task_id='task-mobilize'") == {
        "body": "挂到需求「科室会预约名单导出」"
    }
    assert db.query_one(
        "SELECT requirement_id, candidate_id FROM tasks WHERE id='task-script'"
    ) == {"requirement_id": other, "candidate_id": None}
    handled = client.get(f"/api/requirement-candidates/{candidate_id}").json()
    assert (handled["status"], handled["requirement_id"], handled["sources"]) == (
        "claimed",
        detail["id"],
        [],
    )
    pool = wall(client, status="all")
    assert pool["counts"]["pending"] == 0
    assert "科室会预约名单导出" in titles(pool) and "科室会预约后台导出" not in titles(pool)


def test_claim_with_same_title_returns_the_existing_requirement(tmp_path):
    client, headers, db = make_world(tmp_path)
    existing = create(client, headers, "yimi", "京东科研仓对接", "P0", summary=JD_SUMMARY)
    candidate_id = candidate(db, "receipt", "京东仓签收凭证", summary=RECEIPT_SUMMARY)

    response = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/claim",
        {"title": " 京东科研仓对接 ", "project_id": project_id("yimi"), "priority": "P0"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "该项目下已有同名需求",
        "existing": {"id": existing, "title": "京东科研仓对接", "status": "active"},
    }
    assert db.query_one("SELECT COUNT(*) AS n FROM requirements") == {"n": 1}
    assert db.query_one("SELECT status FROM requirement_candidates") == {"status": "pending"}
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM requirement_sources WHERE candidate_id=?", (candidate_id,)
    ) == {"n": 1}


def test_unassigned_candidate_needs_a_project_and_meeting_keeps_its_attribution(tmp_path):
    client, headers, db = make_world(tmp_path)
    candidate_id = candidate(db, "doctor", "医生资质 AI 审核规则")
    path = f"/api/requirement-candidates/{candidate_id}/claim"

    without = post(client, headers, path, {"title": "医生资质 AI 审核规则"})
    assert without.status_code == 400
    assert without.json()["detail"] == "候选还没归项目，认领前先选所属项目"

    claimed = post(
        client,
        headers,
        path,
        {"title": "医生资质 AI 审核规则", "project_id": project_id("huaxia"), "priority": "P1"},
    ).json()
    assert (claimed["project_name"], claimed["priority"]) == ("华夏基金会科普同行", "P1")
    # 所属项目和来源会议的归属不一致也能保存，不改会议归属（R04 异常与边界）
    assert db.query_one("SELECT project_id FROM meetings WHERE id=?", (meeting_id("doctor"),)) == {
        "project_id": None
    }


def test_candidate_follows_meeting_attribution_until_claimed(tmp_path):
    client, headers, db = make_world(tmp_path)
    candidate_id = candidate(db, "export", "科室会预约后台导出")
    db.execute(
        "UPDATE meetings SET project_id=? WHERE id=?", (project_id("huaxia"), meeting_id("cvm"))
    )
    assert wall(client, status="pending")["items"][0]["project_name"] == "华夏基金会科普同行"

    claimed = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/claim",
        {"title": "科室会预约后台导出", "project_id": project_id("huaxia")},
    ).json()
    db.execute(
        "UPDATE meetings SET project_id=? WHERE id=?", (project_id("cvm"), meeting_id("cvm"))
    )
    # 已认领的需求不再随会议归属变
    assert client.get(f"/api/requirements/{claimed['id']}").json()["project_name"] == (
        "华夏基金会科普同行"
    )


# ---------------------------------------------------------------- 合并


def test_merge_appends_the_quote_and_the_candidate_disappears(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(
        client, headers, "yimi", "京东科研仓对接", "P0", summary=JD_SUMMARY, source_key="inbound"
    )
    shelved = create(
        client, headers, "yimi", "EDC 系统选型", "P1", meeting_keys=("edc",), status="shelved"
    )
    create(client, headers, "yimi", "新患者注册：五个问题前置", "P2", status="done")
    create(client, headers, "hengrui", "亲友积分入口与导入字段", "P1", source_key="family")
    candidate_id = candidate(
        db, "receipt", "京东仓签收凭证", summary=RECEIPT_SUMMARY, similar=target
    )

    targets = client.get(f"/api/requirement-candidates/{candidate_id}/merge-targets").json()

    # S03：只列候选所属项目里进行中、已搁置的需求，AI 推荐的排第一；已完成的、别的项目的不列
    assert targets["project_id"] == project_id("yimi")
    assert [(item["title"], item["recommended"]) for item in targets["items"]] == [
        ("京东科研仓对接", True),
        ("EDC 系统选型", False),
    ]
    assert targets["items"][0]["meeting_title"] == "260916 医米京东科研仓系统对接"
    assert targets["items"][1]["meeting_title"] == "EDC 系统选型与产研对接决策"
    assert targets["items"][1]["id"] == shelved

    response = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    )

    assert response.status_code == 200, response.text
    detail = response.json()
    # R05：合并进来的原话和提出它的那句一起列，同一场会按时间锚先后；头部波形仍取提出它的那场
    assert [
        (source["kind"], source["anchor_ms"], source["via_candidate_title"])
        for source in detail["sources"]
    ] == [("origin", 825270, None), ("merged", 1909360, "京东仓签收凭证")]
    assert detail["sources"][1]["quote"] == QUOTES["receipt"][1]
    assert detail["source"]["anchor_ms"] == 825270
    assert [meeting["id"] for meeting in detail["meetings"]] == [meeting_id("jd")]
    assert detail["follow_up_count"] == 0
    assert (detail["title"], detail["summary"], detail["priority"]) == (
        "京东科研仓对接",
        JD_SUMMARY,
        "P0",
    )
    handled = client.get(f"/api/requirement-candidates/{candidate_id}").json()
    assert (handled["status"], handled["requirement_id"]) == ("merged", target)
    assert wall(client, status="pending")["items"] == []


def test_merge_only_into_open_requirements_of_the_same_project(tmp_path):
    client, headers, db = make_world(tmp_path)
    done = create(client, headers, "yimi", "新患者注册：五个问题前置", "P2", status="done")
    elsewhere = create(client, headers, "hengrui", "亲友积分入口与导入字段", "P1")
    candidate_id = candidate(db, "receipt", "京东仓签收凭证")
    path = f"/api/requirement-candidates/{candidate_id}/merge"

    assert post(client, headers, path, {"requirement_id": done}).status_code == 400
    assert post(client, headers, path, {"requirement_id": elsewhere}).status_code == 400
    assert post(client, headers, path, {"requirement_id": "requirement-missing"}).status_code == 404
    # 所属项目下没有可合并的需求：海报上不给［合并］
    item = wall(client, status="pending")["items"][0]
    assert (item["can_merge"], item["default_action"]) == (False, "claim")

    unassigned = candidate(db, "doctor", "医生资质 AI 审核规则")
    assert client.get(f"/api/requirement-candidates/{unassigned}/merge-targets").json() == {
        "project_id": None,
        "items": [],
    }
    response = post(
        client,
        headers,
        f"/api/requirement-candidates/{unassigned}/merge",
        {"requirement_id": elsewhere},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------- 撤销合并（R01-14）

# CVM 云讲堂那场会里另一句真实原话（同一场会抽出的任务「持续盯预约场次并在群里动员报名」的锚点）
MOBILIZE_QUOTE = "我会同步的去实时的去看这个场次预约，然后尽量多次的在群里动员大家去去去报名。"


def merged_minutes_ago(db, candidate_id, minutes):
    """把合并时间往前挪：撤销时限按服务端的当前时间算，用例不等真时间。"""
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    db.execute("UPDATE requirement_candidates SET merged_at=? WHERE id=?", (stamp, candidate_id))


def test_merge_can_be_undone_within_ten_minutes(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "cvm", "直播间运营六项修正", "P1")
    candidate_id = candidate(db, "export", "科室会预约后台导出", summary=EXPORT_SUMMARY)
    task_on_candidate(
        db, "task-mobilize", "持续盯预约场次并在群里动员报名", 487620, MOBILIZE_QUOTE, candidate_id
    )
    # 没归项目的任务合并后跟着需求进了 CVM 云讲堂，撤销时回到没归项目
    task_on_candidate(
        db,
        "task-script",
        "话术修改稿发执行群走默示确认",
        718230,
        "那个就直接呃甩到执行群里，然后说对应的同事检查一下。",
        candidate_id,
    )
    db.execute("UPDATE tasks SET project_id=NULL WHERE id='task-script'")

    merged = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    ).json()
    assert [meeting["id"] for meeting in merged["meetings"]] == [meeting_id("cvm")]
    assert sorted(task["id"] for task in merged["tasks"]) == ["task-mobilize", "task-script"]
    (source,) = merged["sources"]
    assert source["kind"] == "merged"
    assert source["undo_merge"]["candidate_id"] == candidate_id

    response = post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge")

    assert response.status_code == 200, response.text
    restored = response.json()
    assert (restored["status"], restored["requirement_id"]) == ("pending", None)
    assert [
        (s["kind"], s["quote"], s["anchor_ms"], s["via_candidate_title"])
        for s in restored["sources"]
    ] == [("origin", QUOTES["export"][1], QUOTES["export"][2], None)]
    detail = client.get(f"/api/requirements/{target}").json()
    # 这次合并新加的关联会议拆掉，原话、任务都退回候选
    assert (detail["sources"], detail["meetings"], detail["tasks"]) == ([], [], [])
    assert db.query_all(
        "SELECT id, requirement_id, candidate_id, project_id FROM tasks ORDER BY id"
    ) == [
        {
            "id": "task-mobilize",
            "requirement_id": None,
            "candidate_id": candidate_id,
            "project_id": project_id("cvm"),
        },
        {
            "id": "task-script",
            "requirement_id": None,
            "candidate_id": candidate_id,
            "project_id": None,
        },
    ]
    assert db.query_one(
        "SELECT body FROM task_events WHERE task_id='task-mobilize' ORDER BY id DESC LIMIT 1"
    ) == {"body": "撤销合并：回到候选「科室会预约后台导出」"}
    assert titles(wall(client, status="pending")) == ["科室会预约后台导出"]
    # 回到待认领以后可以照常再处理，撤销不能再撤一次
    again = post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge")
    assert again.status_code == 409


def test_merge_undo_closes_after_ten_minutes(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound")
    candidate_id = candidate(db, "receipt", "京东仓签收凭证", summary=RECEIPT_SUMMARY)
    post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    )
    merged_minutes_ago(db, candidate_id, 9)
    merged = client.get(f"/api/requirements/{target}").json()["sources"][1]
    assert merged["undo_merge"]["candidate_id"] == candidate_id

    merged_minutes_ago(db, candidate_id, 11)

    assert client.get(f"/api/requirements/{target}").json()["sources"][1]["undo_merge"] is None
    response = post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge")
    assert response.status_code == 409
    assert response.json()["detail"] == "合并超过 10 分钟，不能撤销了"
    # 提出它的那句不受影响，合并进来的原话和那场会的关联都还在
    detail = client.get(f"/api/requirements/{target}").json()
    assert [source["kind"] for source in detail["sources"]] == ["origin", "merged"]
    assert [meeting["id"] for meeting in detail["meetings"]] == [meeting_id("jd")]


def test_merge_undo_only_takes_back_what_that_merge_brought(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "cvm", "直播间运营六项修正", "P1")
    other = create(client, headers, "cvm", "科室会预约后台权限", "P1")
    first = candidate(db, "export", "科室会预约后台导出", summary=EXPORT_SUMMARY)
    task_on_candidate(
        db, "task-mobilize", "持续盯预约场次并在群里动员报名", 487620, MOBILIZE_QUOTE, first
    )
    with db.transaction() as connection:
        second = insert_candidate(
            connection,
            meeting_id=meeting_id("cvm"),
            title="科室会预约场次动员",
            summary="",
            quote=MOBILIZE_QUOTE,
            anchor_ms=487620,
        )
    for candidate_id in (first, second):
        post(
            client,
            headers,
            f"/api/requirement-candidates/{candidate_id}/merge",
            {"requirement_id": target},
        )
    # 合并后 10 分钟里任务被改挂到别的需求：撤销不把它拽回来
    client.patch("/api/tasks/task-mobilize", json={"requirement_id": other}, headers=headers)

    response = post(client, headers, f"/api/requirement-candidates/{first}/unmerge")

    assert response.status_code == 200, response.text
    detail = client.get(f"/api/requirements/{target}").json()
    # 同一场会后来又合并进一条：关联留着，那一条的原话也留着
    assert [meeting["id"] for meeting in detail["meetings"]] == [meeting_id("cvm")]
    assert [(s["quote"], s["via_candidate_title"]) for s in detail["sources"]] == [
        (MOBILIZE_QUOTE, "科室会预约场次动员")
    ]
    assert db.query_one(
        "SELECT requirement_id, candidate_id FROM tasks WHERE id='task-mobilize'"
    ) == {"requirement_id": other, "candidate_id": None}


def test_merge_undo_refused_when_its_quote_is_gone(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "cvm", "直播间运营六项修正", "P1")
    candidate_id = candidate(db, "export", "科室会预约后台导出")
    post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    )
    # 详情页把那场会移出关联：触发器连原话一起删了
    removed = client.delete(
        f"/api/requirements/{target}/meetings/{meeting_id('cvm')}",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert removed.status_code == 200, removed.text

    response = post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge")

    assert response.status_code == 409
    assert response.json()["detail"] == "合并进去的原话已经不在那条需求里了，不能撤销"
    assert client.get(f"/api/requirement-candidates/{candidate_id}").json()["status"] == "merged"


def test_default_action_falls_back_to_claim_when_the_similar_requirement_is_done(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound")
    candidate(db, "receipt", "京东仓签收凭证", similar=target)
    assert wall(client, status="pending")["items"][0]["default_action"] == "merge"

    client.patch(f"/api/requirements/{target}", json={"status": "done"}, headers=headers)

    item = wall(client, status="pending")["items"][0]
    assert (item["default_action"], item["similar_requirement"], item["can_merge"]) == (
        "claim",
        None,
        False,
    )


# ---------------------------------------------------------------- 丢掉 / 撤销


def test_drop_detaches_tasks_and_can_be_undone_within_30_days(tmp_path):
    client, headers, db = make_world(tmp_path)
    create(client, headers, "cvm", "直播间运营六项修正", "P1", meeting_keys=("cvm",))
    candidate_id = candidate(db, "export", "科室会预约后台导出", summary=EXPORT_SUMMARY)
    task_on_candidate(
        db,
        "task-mobilize",
        "持续盯预约场次并在群里动员报名",
        487620,
        "我会同步的去实时的去看这个场次预约，然后尽量多次的在群里动员大家去去去报名。",
        candidate_id,
    )

    dropped = post(client, headers, f"/api/requirement-candidates/{candidate_id}/drop").json()

    assert dropped["status"] == "dropped" and dropped["dropped_at"]
    assert db.query_one("SELECT candidate_id FROM tasks") == {"candidate_id": None}
    pool = wall(client, status="pending")
    assert (pool["items"], pool["dropped_count"]) == ([], 1)
    # 「已丢掉」不跟筛选走：筛别的项目、搜别的名字，链接上的数和弹层列的一样
    assert wall(client, status="pending", project_id=project_id("yimi"))["dropped_count"] == 1
    assert wall(client, status="pending", q="京东")["dropped_count"] == 1
    listing = client.get("/api/requirement-candidates/dropped").json()
    assert [item["title"] for item in listing["items"]] == ["科室会预约后台导出"]
    dropped_at = datetime.fromisoformat(dropped["dropped_at"])
    assert listing["items"][0]["restore_until"] == (dropped_at + timedelta(days=30)).isoformat()
    assert listing["undo_days"] == 30

    restored = post(client, headers, f"/api/requirement-candidates/{candidate_id}/restore").json()
    assert (restored["status"], restored["dropped_at"]) == ("pending", None)
    assert titles(wall(client, status="pending")) == ["科室会预约后台导出"]
    # 撤销不会把任务挂回来
    assert db.query_one("SELECT candidate_id FROM tasks") == {"candidate_id": None}


def test_restore_window_closes_after_30_days(tmp_path):
    client, headers, db = make_world(tmp_path)
    candidate_id = candidate(db, "hospital", "医院名单匹配规则")
    dropped = post(client, headers, f"/api/requirement-candidates/{candidate_id}/drop").json()
    dropped_at = datetime.fromisoformat(dropped["dropped_at"])

    # 时间都从丢掉的那一刻往后推，不看今天几点
    assert list_dropped(db, now=dropped_at + timedelta(days=29))["total"] == 1
    assert list_dropped(db, now=dropped_at + timedelta(days=31))["total"] == 0
    with pytest.raises(ConflictError, match="30 天"):
        restore_candidate(db, candidate_id, now=dropped_at + timedelta(days=31))
    assert restore_candidate(db, candidate_id, now=dropped_at + timedelta(days=29))["status"] == (
        "pending"
    )


def test_handled_candidates_cannot_be_handled_again(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "huaxia", "劳务签署证据链导出", "P0", meeting_keys=("huaxia",))
    candidate_id = candidate(db, "hospital", "医院名单匹配规则")
    claim = f"/api/requirement-candidates/{candidate_id}/claim"
    body = {"title": "医院名单匹配规则", "project_id": project_id("huaxia")}

    assert post(client, headers, claim, body).status_code == 200
    again = post(client, headers, claim, body)
    assert again.status_code == 409 and again.json()["detail"] == "这条候选已经认领了"
    merged = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    )
    assert merged.status_code == 409
    assert (
        post(client, headers, f"/api/requirement-candidates/{candidate_id}/drop").status_code == 409
    )
    restore = post(client, headers, f"/api/requirement-candidates/{candidate_id}/restore")
    assert restore.status_code == 409 and restore.json()["detail"] == "这条候选不在已丢掉里"
    assert client.get("/api/requirement-candidates/candidate-missing").status_code == 404


# ---------------------------------------------------------------- 建候选（抽取那一刀调）


def test_insert_candidate_keeps_ai_text_claimable(tmp_path):
    _client, _headers, db = make_world(tmp_path)
    with db.transaction() as connection:
        candidate_id = insert_candidate(
            connection,
            meeting_id=meeting_id("cvm"),
            title="  科室会预约后台导出 ",
            summary="说" * 80,
            quote=QUOTES["export"][1],
            anchor_ms=QUOTES["export"][2],
            similar_requirement_id="requirement-missing",
            extraction_id=7,
        )
    row = db.query_one("SELECT * FROM requirement_candidates WHERE id=?", (candidate_id,))
    assert (row["title"], row["name_key"], len(row["summary"])) == (
        "科室会预约后台导出",
        "科室会预约后台导出",
        70,
    )
    assert (row["similar_requirement_id"], row["extraction_id"], row["status"]) == (
        None,
        7,
        "pending",
    )
    with db.transaction() as connection, pytest.raises(ValueError):
        insert_candidate(connection, meeting_id=meeting_id("cvm"), title="  ")


def test_insert_candidate_tolerates_overlong_titles_and_bad_anchors(tmp_path):
    """AI 给的标题超长时截到 200（原样就能认领），时间锚超出录音或不是毫秒数时当没有，不让整场抽取失败。"""
    client, headers, db = make_world(tmp_path)
    with db.transaction() as connection:
        long_title = insert_candidate(
            connection, meeting_id=meeting_id("cvm"), title="导" * 280, anchor_ms=747000 * 10
        )
        odd_anchor = insert_candidate(
            connection,
            meeting_id=meeting_id("cvm"),
            title="科室会预约后台导出",
            quote=QUOTES["export"][1],
            anchor_ms="00:09:36",
        )
    assert (
        len(
            db.query_one("SELECT title FROM requirement_candidates WHERE id=?", (long_title,))[
                "title"
            ]
        )
        == 200
    )
    anchors = db.query_all("SELECT candidate_id, anchor_ms FROM requirement_sources ORDER BY id")
    assert anchors == [
        {"candidate_id": long_title, "anchor_ms": None},
        {"candidate_id": odd_anchor, "anchor_ms": None},
    ]
    claimed = post(
        client,
        headers,
        f"/api/requirement-candidates/{long_title}/claim",
        {"title": "导" * 200},
    )
    assert claimed.status_code == 200, claimed.text


# ---------------------------------------------------------------- 认领页改了项目（R01-7/8 与撞名）


def test_unassigned_candidate_can_merge_after_a_claim_conflict(tmp_path):
    """未归项目的候选在认领页选了华夏，撞上华夏已有的同名需求：改为合并到那条需求，会议归属不动。"""
    client, headers, db = make_world(tmp_path)
    existing = create(
        client, headers, "huaxia", "医生资质 AI 审核规则", "P1", meeting_keys=("huaxia",)
    )
    candidate_id = candidate(db, "doctor", "医生资质 AI 审核规则")

    conflict = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/claim",
        {"title": "医生资质 AI 审核规则", "project_id": project_id("huaxia")},
    )
    assert conflict.status_code == 409 and conflict.json()["existing"]["id"] == existing
    targets = client.get(
        f"/api/requirement-candidates/{candidate_id}/merge-targets",
        params={"project_id": project_id("huaxia")},
    ).json()
    assert [item["id"] for item in targets["items"]] == [existing]

    merged = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": existing, "project_id": project_id("huaxia")},
    )

    assert merged.status_code == 200, merged.text
    detail = merged.json()
    assert [
        (source["kind"], source["anchor_ms"], source["via_candidate_title"])
        for source in detail["sources"]
    ] == [("merged", QUOTES["doctor"][2], "医生资质 AI 审核规则")]
    assert {meeting["id"] for meeting in detail["meetings"]} == {
        meeting_id("huaxia"),
        meeting_id("doctor"),
    }
    assert db.query_one("SELECT project_id FROM meetings WHERE id=?", (meeting_id("doctor"),)) == {
        "project_id": None
    }


def test_merge_targets_follow_the_project_picked_on_the_claim_page(tmp_path):
    client, headers, db = make_world(tmp_path)
    create(client, headers, "cvm", "直播间运营六项修正", "P1", meeting_keys=("cvm",))
    same_name = create(
        client, headers, "huaxia", "科室会预约后台导出", "P2", meeting_keys=("huaxia",)
    )
    candidate_id = candidate(db, "export", "科室会预约后台导出")
    path = f"/api/requirement-candidates/{candidate_id}"

    by_meeting = client.get(f"{path}/merge-targets").json()
    assert [item["title"] for item in by_meeting["items"]] == ["直播间运营六项修正"]
    picked = client.get(f"{path}/merge-targets", params={"project_id": project_id("huaxia")}).json()
    assert (picked["project_id"], [item["id"] for item in picked["items"]]) == (
        project_id("huaxia"),
        [same_name],
    )
    missing = client.get(f"{path}/merge-targets", params={"project_id": "project-missing"})
    assert missing.status_code == 404
    # 不带项目时按会议归属（CVM），并进华夏的需求算跨项目
    assert post(client, headers, f"{path}/merge", {"requirement_id": same_name}).status_code == 400
    merged = post(
        client,
        headers,
        f"{path}/merge",
        {"requirement_id": same_name, "project_id": project_id("huaxia")},
    )
    assert merged.status_code == 200, merged.text
    assert db.query_one("SELECT project_id FROM meetings WHERE id=?", (meeting_id("cvm"),)) == {
        "project_id": project_id("cvm")
    }


def test_claim_defaults_to_the_candidates_own_project_and_summary(tmp_path):
    client, headers, db = make_world(tmp_path)
    export = candidate(db, "export", "科室会预约后台导出", summary=EXPORT_SUMMARY)
    hospital = candidate(
        db,
        "hospital",
        "医院名单匹配规则",
        summary="客户一次给上千家医院，能匹配上的告知成功，匹配不上的退回客户再确认。",
    )

    claimed = post(
        client,
        headers,
        f"/api/requirement-candidates/{export}/claim",
        {"title": "科室会预约后台导出"},
    ).json()
    assert (claimed["project_name"], claimed["summary"], claimed["priority"]) == (
        "CVM 云讲堂",
        EXPORT_SUMMARY,
        "P2",
    )
    cleared = post(
        client,
        headers,
        f"/api/requirement-candidates/{hospital}/claim",
        {"title": "医院名单匹配规则", "summary": ""},
    ).json()
    assert (cleared["project_name"], cleared["summary"]) == ("华夏基金会科普同行", "")
