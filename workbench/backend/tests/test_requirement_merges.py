"""需求并需求 + 卡片改状态（261009）：D4–D16 的后端部分。

项目、会议和原话照 requirement_pool_world（生产库原样抄的医米会议）；用户说「这仨咋重复了」的场景：医米下
「京东科研仓对接」和从同一场会认领出来的「京东仓签收凭证」说的是一件事，要并起来。

撤销时限按服务端的当前时间算：用例把库里记下的时间往前挪，不等真时间、也不取决于今天几点。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from meeting_workbench.db import Database, utc_now
from meeting_workbench.project_profile import light_key

from .requirement_pool_world import PROJECTS, QUOTES, meeting_id, project_id
from .test_requirement_candidates import RECEIPT_SUMMARY, post
from .test_requirement_pool import JD_SUMMARY, candidate, create, make_world, titles, wall
from .test_timeline import shanghai  # noqa: F401  撤销时限、完成时间排序都和时间有关，本机时区钉成北京时间

CHANGED = "2026-10-08T02:00:00+00:00"


def task_on(db, task_id, title, requirement_id, status, *, project_key="yimi", changed_at=CHANGED):
    """挂在需求上的一条待办（状态、状态时间照给的写）。"""
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, assignee, meeting_id, project_id,
                             requirement_id, status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', 'me', NULL, ?, ?, ?, ?, ?)""",
        (
            task_id,
            title,
            status,
            project_id(project_key),
            requirement_id,
            changed_at,
            CHANGED,
            CHANGED,
        ),
    )


def task_row(db, task_id):
    return db.query_one(
        "SELECT status, status_changed_at, requirement_id, project_id FROM tasks WHERE id=?",
        (task_id,),
    )


def events(db, task_id):
    return [
        (row["kind"], row["body"])
        for row in db.query_all(
            "SELECT kind, body FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


def patch(client, headers, requirement_id, body):
    return client.patch(f"/api/requirements/{requirement_id}", json=body, headers=headers)


def material_root(db, tmp_path, key):
    root = (tmp_path / "材料" / PROJECTS[key][1]).resolve()
    root.mkdir(parents=True)
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id(key), str(root), utc_now()),
    )
    return root


def folder(root, name):
    path = root / name
    path.mkdir(exist_ok=True)
    return str(path)


def snapshot(db):
    """合并、撤销会动到的表；updated_at 另看（主需求的会刷新）。"""

    def without_updated(rows):
        return [{key: value for key, value in row.items() if key != "updated_at"} for row in rows]

    return {
        "requirements": without_updated(db.query_all("SELECT * FROM requirements ORDER BY id")),
        "requirement_meetings": db.query_all(
            "SELECT * FROM requirement_meetings ORDER BY requirement_id, meeting_id"
        ),
        "requirement_folders": db.query_all("SELECT * FROM requirement_folders ORDER BY id"),
        "requirement_sources": db.query_all("SELECT * FROM requirement_sources ORDER BY id"),
        "tasks": without_updated(db.query_all("SELECT * FROM tasks ORDER BY id")),
        "requirement_candidates": db.query_all("SELECT * FROM requirement_candidates ORDER BY id"),
        "decisions": db.query_all("SELECT * FROM decisions ORDER BY id"),
        "requirement_name_decisions": db.query_all(
            "SELECT * FROM requirement_name_decisions ORDER BY project_id, name_key"
        ),
        "requirement_merges": db.query_all("SELECT * FROM requirement_merges"),
    }


def merge(client, headers, requirement_id, into_id):
    return post(
        client,
        headers,
        f"/api/requirements/{requirement_id}/merge",
        {"into_requirement_id": into_id},
    )


def merged_minutes_ago(db, requirement_id, minutes):
    row = db.query_one(
        "SELECT merged_at FROM requirement_merges WHERE requirement_id=?", (requirement_id,)
    )
    stamp = (datetime.fromisoformat(row["merged_at"]) - timedelta(minutes=minutes)).isoformat()
    db.execute(
        "UPDATE requirement_merges SET merged_at=? WHERE requirement_id=?", (stamp, requirement_id)
    )


def status_changed_minutes_ago(db, requirement_id, minutes):
    """把最近一次改状态整体往前挪：需求的状态时间、撤销记录里的时刻、这次一起关掉的待办的状态时间。"""
    row = db.query_one(
        "SELECT status_changed_at, status_undo FROM requirements WHERE id=?", (requirement_id,)
    )
    record = json.loads(row["status_undo"])
    old = record["at"]
    new = (datetime.fromisoformat(old) - timedelta(minutes=minutes)).isoformat()
    record["at"] = new
    db.execute(
        "UPDATE requirements SET status_changed_at=?, status_undo=? WHERE id=?",
        (new, json.dumps(record), requirement_id),
    )
    db.execute("UPDATE tasks SET status_changed_at=? WHERE status_changed_at=?", (new, old))


def close_to_now(stamp, *, plus_minutes=0):
    moment = datetime.fromisoformat(stamp)
    expected = datetime.now(UTC) + timedelta(minutes=plus_minutes)
    return abs((moment - expected).total_seconds()) < 60


def jd_pair(client, headers, db, tmp_path):
    """医米下重复的两条：主需求「京东科研仓对接」（P1、没写说明、出处是入库单那句、关联京东那场会、两个文件夹）；
    这条「京东仓签收凭证」（从京东那场会的候选认领出来，P0、有说明、出处是签收凭证那句，另关联横跳那场会，
    三条待办、两个文件夹其中一个和主需求同名，一条决议、一条需求名决定、一条把它当相近需求的候选）。"""
    root = material_root(db, tmp_path, "yimi")
    shared = folder(root, "京东科研仓对接材料")
    own = folder(root, "签收凭证样张")
    main = create(client, headers, "yimi", "京东科研仓对接", "P1", source_key="inbound")
    assert patch(client, headers, main, {"folder_paths": [shared]}).status_code == 200
    claimed_from = candidate(db, "receipt", "京东仓签收凭证", summary=RECEIPT_SUMMARY)
    claimed = post(
        client,
        headers,
        f"/api/requirement-candidates/{claimed_from}/claim",
        {"title": "京东仓签收凭证", "project_id": project_id("yimi"), "priority": "P0"},
    )
    assert claimed.status_code == 200, claimed.text
    this = claimed.json()["id"]
    assert patch(client, headers, this, {"folder_paths": [shared, own]}).status_code == 200
    linked = post(client, headers, f"/api/requirements/{this}/meetings/{meeting_id('hengtiao')}")
    assert linked.status_code == 200, linked.text
    task_on(db, "task-sign", "和京东确认妥投拍照字段", this, "confirmed")
    task_on(db, "task-bill", "月结对账单按签收凭证核", this, "in_progress")
    task_on(db, "task-ask", "问清随货通行单谁来传", this, "done")
    now = utc_now()
    db.execute(
        """INSERT INTO decisions(id, meeting_id, ordinal, text, text_key, placement, requirement_id,
                                 placed_at, created_at, updated_at)
           VALUES ('dec-0123456789abcdef', ?, 0, '签收凭证按患者手持身份证跟药盒拍照', '签收凭证',
                   'picked', ?, ?, ?, ?)""",
        (meeting_id("jd"), this, now, now, now),
    )
    db.execute(
        """INSERT INTO requirement_name_decisions(project_id, name_key, name, decision, requirement_id,
                                                  decided_at)
           VALUES (?, ?, '京东仓签收凭证', 'made', ?, ?)""",
        (project_id("yimi"), light_key("京东仓签收凭证"), this, now),
    )
    similar = candidate(db, "inbound", "京东入库单推送", similar=this)
    return main, this, claimed_from, similar


# ---------------------------------------------------------------- 3. merge-targets（D4）


def test_merge_targets_list_the_same_project_by_status_priority_and_latest_meeting(tmp_path):
    client, headers, db = make_world(tmp_path)
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2", source_key="receipt")
    task_on(db, "task-open", "和京东确认妥投拍照字段", this, "confirmed")
    task_on(db, "task-done", "问清随货通行单谁来传", this, "done")
    # 进行中三条：P0 一条没关联会议；P1 两条按最近会议由近到远（京东 09-16 在横跳 09-10 前）
    edc = create(client, headers, "yimi", "EDC 系统选型", "P1", meeting_keys=("hengtiao",))
    jd = create(client, headers, "yimi", "京东科研仓对接", "P1", source_key="inbound")
    urgent = create(client, headers, "yimi", "赠药横跳拦截", "P0")
    shelved = create(client, headers, "yimi", "处方流转药房白名单", "P0", status="shelved")
    done = create(
        client,
        headers,
        "yimi",
        "新患者注册：五个问题前置",
        "P0",
        meeting_keys=("five",),
        status="done",
    )
    create(client, headers, "hengrui", "亲友积分入口与导入字段", "P1")
    task_on(db, "task-jd", "推入库单接口", jd, "in_progress")

    response = client.get(f"/api/requirements/{this}/merge-targets")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["moving"] == {
        "open_task_count": 1,
        "task_count": 2,
        "meeting_count": 1,
        "source_count": 1,
        "folder_count": 0,
        "priority": "P2",
    }
    assert [item["id"] for item in payload["items"]] == [urgent, jd, edc, shelved, done]
    assert payload["items"][1] == {
        "id": jd,
        "title": "京东科研仓对接",
        "status": "active",
        "priority": "P1",
        "open_task_count": 1,
        "meeting_count": 1,
    }
    assert payload["items"][4]["status"] == "done"
    # 按名称筛，不分大小写
    filtered = client.get(f"/api/requirements/{this}/merge-targets", params={"q": "edc"}).json()
    assert titles(filtered) == ["EDC 系统选型"]
    assert client.get("/api/requirements/requirement-missing/merge-targets").status_code == 404


# ---------------------------------------------------------------- 4. merge（D5、D6、D7）


def test_merge_carries_everything_over_and_the_requirement_leaves_the_wall(tmp_path):
    client, headers, db = make_world(tmp_path)
    main, this, claimed_from, similar = jd_pair(client, headers, db, tmp_path)
    assert len(wall(client, status="active")["items"]) == 2

    response = merge(client, headers, this, main)

    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["id"] == main
    assert detail["merged_from"]["id"] == this
    assert detail["merged_from"]["title"] == "京东仓签收凭证"
    assert close_to_now(detail["merged_from"]["undo_until"], plus_minutes=10)
    # D6：名字用主需求的，说明主需求空着就接过这条的，等级取较高，两边都进行中
    assert (detail["title"], detail["summary"], detail["priority"], detail["status"]) == (
        "京东科研仓对接",
        RECEIPT_SUMMARY,
        "P0",
        "active",
    )
    # D5、D6：两边都有出处，主需求的留着；这条的原话记成合并进来的，来源卡上显示「合并自候选『这条』」
    assert [
        (source["kind"], source["quote"], source["via_candidate_title"])
        for source in detail["sources"]
    ] == [
        ("origin", QUOTES["inbound"][1], None),
        ("merged", QUOTES["receipt"][1], "京东仓签收凭证"),
    ]
    # 会、文件夹已在主需求上的不重复
    assert sorted(meeting["id"] for meeting in detail["meetings"]) == sorted(
        [meeting_id("jd"), meeting_id("hengtiao")]
    )
    assert sorted(item["name"] for item in detail["folders"]) == [
        "京东科研仓对接材料",
        "签收凭证样张",
    ]
    assert {task["id"] for task in detail["tasks"]} == {"task-sign", "task-bill", "task-ask"}
    assert detail["open_task_count"] == 2
    for task_id in ("task-sign", "task-bill", "task-ask"):
        assert task_row(db, task_id)["requirement_id"] == main
        assert events(db, task_id)[-1] == (
            "requirement_changed",
            "需求「京东仓签收凭证」并入「京东科研仓对接」，跟着挂到「京东科研仓对接」",
        )
    # 别处指着这条的都改指主需求：认领进这条的候选记成已合并
    assert db.query_one(
        "SELECT status, requirement_id FROM requirement_candidates WHERE id=?", (claimed_from,)
    ) == {"status": "merged", "requirement_id": main}
    assert db.query_one(
        "SELECT similar_requirement_id FROM requirement_candidates WHERE id=?", (similar,)
    ) == {"similar_requirement_id": main}
    assert db.query_one("SELECT requirement_id FROM decisions") == {"requirement_id": main}
    assert db.query_one("SELECT requirement_id FROM requirement_name_decisions") == {
        "requirement_id": main
    }
    # D7：这条从墙上消失
    assert db.query_one("SELECT 1 AS n FROM requirements WHERE id=?", (this,)) is None
    assert titles(wall(client, status="active")) == ["京东科研仓对接"]
    # D8：以前的链接打开它，说并进了哪条
    gone = client.get(f"/api/requirements/{this}")
    assert gone.status_code == 404
    assert gone.json() == {
        "detail": "这条需求已并入「京东科研仓对接」",
        "merged_into": {"id": main, "title": "京东科研仓对接"},
    }
    assert client.get("/api/requirements/requirement-missing").json() == {
        "detail": "需求不存在：requirement-missing"
    }


def test_merge_takes_over_the_origin_when_the_main_requirement_has_none(tmp_path):
    client, headers, db = make_world(tmp_path)
    main = create(
        client, headers, "yimi", "京东科研仓对接", "P0", summary=JD_SUMMARY, meeting_keys=("jd",)
    )
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2", source_key="receipt")

    detail = merge(client, headers, this, main).json()

    # 出处主需求没有就接过这条的 origin；说明主需求有就留主需求的；等级 P0 比 P2 高
    assert detail["source"]["kind"] == "origin"
    assert detail["source"]["quote"] == QUOTES["receipt"][1]
    assert detail["source"]["via_candidate_title"] is None
    assert [source["kind"] for source in detail["sources"]] == ["origin"]
    assert (detail["summary"], detail["priority"]) == (JD_SUMMARY, "P0")

    restored = post(client, headers, f"/api/requirements/{this}/unmerge")

    assert restored.status_code == 200, restored.text
    assert restored.json()["source"]["quote"] == QUOTES["receipt"][1]
    assert client.get(f"/api/requirements/{main}").json()["source"] is None


@pytest.mark.parametrize(
    ("this_status", "main_status", "merged_status"),
    [
        ("active", "done", "active"),
        ("active", "shelved", "active"),
        ("shelved", "active", "active"),
        ("shelved", "done", "done"),
        ("done", "shelved", "shelved"),
    ],
)
def test_merged_requirement_is_active_when_either_side_is(
    tmp_path, this_status, main_status, merged_status
):
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P3", status=main_status)
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2", status=this_status)
    before = db.query_one("SELECT status_changed_at FROM requirements WHERE id=?", (main,))

    detail = merge(client, headers, this, main).json()

    assert (detail["status"], detail["priority"]) == (merged_status, "P2")
    if merged_status == main_status:
        assert detail["status_changed_at"] == before["status_changed_at"]
    else:
        # 已完成的主需求因为这条进行中被重新打开：状态时间是这次合并
        assert close_to_now(detail["status_changed_at"])
        assert detail["status_undo_until"] is None


def test_merge_refuses_itself_other_projects_and_missing_requirements(tmp_path):
    client, headers, db = make_world(tmp_path)
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2")
    elsewhere = create(client, headers, "hengrui", "亲友积分入口与导入字段", "P1")

    itself = merge(client, headers, this, this)
    across = merge(client, headers, this, elsewhere)
    missing = merge(client, headers, this, "requirement-missing")
    unknown = merge(client, headers, "requirement-missing", this)

    assert (itself.status_code, itself.json()["detail"]) == (400, "不能并给自己")
    assert (across.status_code, across.json()["detail"]) == (400, "只能并入同一项目里的需求")
    assert (missing.status_code, missing.json()["detail"]) == (404, "要并入的需求不存在")
    assert unknown.status_code == 404
    assert db.query_one("SELECT COUNT(*) AS n FROM requirements") == {"n": 2}
    assert db.query_one("SELECT COUNT(*) AS n FROM requirement_merges") == {"n": 0}


# ---------------------------------------------------------------- 5. unmerge（D7）


def test_unmerge_within_ten_minutes_puts_every_table_back(tmp_path):
    client, headers, db = make_world(tmp_path)
    main, this, _claimed_from, _similar = jd_pair(client, headers, db, tmp_path)
    before = snapshot(db)
    this_updated = db.query_one("SELECT updated_at FROM requirements WHERE id=?", (this,))
    assert merge(client, headers, this, main).status_code == 200

    response = post(client, headers, f"/api/requirements/{this}/unmerge")

    assert response.status_code == 200, response.text
    detail = response.json()
    assert (detail["id"], detail["title"], detail["priority"]) == (this, "京东仓签收凭证", "P0")
    assert snapshot(db) == before
    assert db.query_one("SELECT updated_at FROM requirements WHERE id=?", (this,)) == this_updated
    assert events(db, "task-sign")[-1] == (
        "requirement_changed",
        "撤销合并：回到需求「京东仓签收凭证」",
    )
    assert len(wall(client, status="active")["items"]) == 2
    assert client.get(f"/api/requirements/{this}").status_code == 200
    # 撤销过一次，再撤销没有可撤销的
    again = post(client, headers, f"/api/requirements/{this}/unmerge")
    assert (again.status_code, again.json()["detail"]) == (409, "这条需求没有被合并，不用撤销")


def test_unmerge_after_ten_minutes_is_refused(tmp_path):
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P1")
    this = create(client, headers, "yimi", "京东仓签收凭证", "P0")
    assert merge(client, headers, this, main).status_code == 200
    merged_minutes_ago(db, this, 11)

    response = post(client, headers, f"/api/requirements/{this}/unmerge")

    assert (response.status_code, response.json()["detail"]) == (
        409,
        "合并超过 10 分钟，不能撤销了",
    )
    # 过了时限，以前的链接照样跟到主需求
    assert client.get(f"/api/requirements/{this}").json()["merged_into"]["id"] == main


def test_unmerge_keeps_what_was_changed_elsewhere_in_the_meantime(tmp_path):
    """撤销时主需求已被改过：手动改过的等级留着，没改过的状态退回；改挂到别处的待办不动；
    主需求上被连带删掉的这条的原话按快照补回给这条。"""
    client, headers, db = make_world(tmp_path)
    main = create(
        client, headers, "yimi", "京东科研仓对接", "P3", source_key="inbound", status="done"
    )
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2", meeting_keys=("hengtiao",))
    other = create(client, headers, "yimi", "赠药横跳拦截", "P0")
    db.execute(
        """INSERT INTO requirement_sources(requirement_id, kind, meeting_id, quote, anchor_ms,
                                           via_candidate_title, created_at)
           VALUES (?, 'origin', ?, '', NULL, NULL, ?)""",
        (this, meeting_id("hengtiao"), utc_now()),
    )
    task_on(db, "task-sign", "和京东确认妥投拍照字段", this, "confirmed")
    task_on(db, "task-bill", "月结对账单按签收凭证核", this, "in_progress")
    done_before = db.query_one("SELECT status_changed_at FROM requirements WHERE id=?", (main,))
    detail = merge(client, headers, this, main).json()
    assert (detail["status"], detail["priority"]) == ("active", "P2")
    # 这 10 分钟里：主需求等级手动改成 P1；一条待办改挂到别的需求；横跳那场会从主需求上移出（原话跟着删）
    assert patch(client, headers, main, {"priority": "P1"}).status_code == 200
    moved = client.patch("/api/tasks/task-bill", json={"requirement_id": other}, headers=headers)
    assert moved.status_code == 200, moved.text
    removed = client.delete(
        f"/api/requirements/{main}/meetings/{meeting_id('hengtiao')}",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert removed.status_code == 200, removed.text

    restored = post(client, headers, f"/api/requirements/{this}/unmerge").json()

    main_after = client.get(f"/api/requirements/{main}").json()
    assert (main_after["priority"], main_after["status"]) == ("P1", "done")
    assert main_after["status_changed_at"] == done_before["status_changed_at"]
    assert [meeting["id"] for meeting in main_after["meetings"]] == [meeting_id("jd")]
    assert task_row(db, "task-sign")["requirement_id"] == this
    assert task_row(db, "task-bill")["requirement_id"] == other
    assert [meeting["id"] for meeting in restored["meetings"]] == [meeting_id("hengtiao")]
    assert [(source["kind"], source["meeting_id"]) for source in restored["sources"]] == [
        ("origin", meeting_id("hengtiao"))
    ]
    assert [task["id"] for task in restored["tasks"]] == ["task-sign"]


def test_unmerge_refuses_a_title_taken_in_the_meantime(tmp_path):
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P1")
    this = create(client, headers, "yimi", "京东仓签收凭证", "P0")
    assert merge(client, headers, this, main).status_code == 200
    create(client, headers, "yimi", "京东仓签收凭证", "P2")

    response = post(client, headers, f"/api/requirements/{this}/unmerge")

    assert response.status_code == 409
    assert response.json()["detail"] == "项目里已经有同名的需求「京东仓签收凭证」，不能撤销"
    assert client.get(f"/api/requirements/{this}").status_code == 404


# ---------------------------------------------------------------- 6. 链式合并（D8）


def test_old_links_follow_a_chain_of_merges_to_the_last_one(tmp_path):
    client, headers, db = make_world(tmp_path)
    first = create(client, headers, "yimi", "京东仓签收凭证", "P2")
    second = create(client, headers, "yimi", "京东科研仓对接", "P1")
    last = create(client, headers, "yimi", "京东科研仓二期", "P0")
    assert merge(client, headers, first, second).status_code == 200
    assert merge(client, headers, second, last).status_code == 200

    for gone in (first, second):
        response = client.get(f"/api/requirements/{gone}")
        assert response.status_code == 404
        assert response.json()["merged_into"] == {"id": last, "title": "京东科研仓二期"}
    # 中间那条之后又被并走了：先撤销后一次，才能撤销前一次
    blocked = post(client, headers, f"/api/requirements/{first}/unmerge")
    assert blocked.status_code == 409
    assert (
        blocked.json()["detail"]
        == "「京东科研仓对接」之后又并入了「京东科研仓二期」，先撤销那次合并"
    )

    assert post(client, headers, f"/api/requirements/{second}/unmerge").status_code == 200
    assert client.get(f"/api/requirements/{first}").json()["merged_into"] == {
        "id": second,
        "title": "京东科研仓对接",
    }
    assert post(client, headers, f"/api/requirements/{first}/unmerge").status_code == 200
    assert {item["title"] for item in wall(client, status="active")["items"]} == {
        "京东仓签收凭证",
        "京东科研仓对接",
        "京东科研仓二期",
    }


# ---------------------------------------------------------------- 会议页「建成需求」「不算新需求」遇上合并


def name_as_requirement(client, headers, meeting_key, title):
    response = client.post(
        f"/api/meetings/{meeting_id(meeting_key)}/name-as-requirement",
        json={"title": title},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def ignore_requirement_name(client, headers, name):
    response = post(
        client,
        headers,
        "/api/project-names/ignore",
        {"name": name, "kind": "requirement", "project_id": project_id("yimi")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def event_payload(db, event_id):
    row = db.query_one("SELECT payload_json FROM events WHERE id=?", (event_id,))
    return json.loads(row["payload_json"])


def test_undoing_name_as_requirement_is_refused_while_its_requirement_is_merged_away(tmp_path):
    """会议页把 EDC 那场会建成需求「EDC 系统选型」，随后并进已有的「EDC 产研对接」；10 分钟内撤销「建成需求」
    不动任何数据，提示先撤销那次合并。撤销合并以后再撤销建成需求，照原样删掉那次建出来的需求。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "EDC 产研对接", "P1")
    before_name = snapshot(db)
    made = name_as_requirement(client, headers, "edc", "EDC 系统选型")
    assert made["existing"] is False
    assert merge(client, headers, made["requirement_id"], main).status_code == 200
    merged = snapshot(db)
    payload = event_payload(db, made["event_id"])

    refused = post(client, headers, f"/api/meetings/{meeting_id('edc')}/name-as-requirement/undo")

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "「EDC 系统选型」已并入「EDC 产研对接」，要撤销建成需求，先撤销那次合并"
    )
    assert snapshot(db) == merged
    assert event_payload(db, made["event_id"]) == payload
    assert (payload["requirement_id"], payload["existing"]) == (made["requirement_id"], False)

    assert (
        post(client, headers, f"/api/requirements/{made['requirement_id']}/unmerge").status_code
        == 200
    )
    undone = post(client, headers, f"/api/meetings/{meeting_id('edc')}/name-as-requirement/undo")

    assert undone.status_code == 200, undone.text
    assert undone.json()["requirement_deleted"] is True
    assert snapshot(db) == before_name


def test_undoing_name_as_requirement_is_refused_after_another_requirement_merged_into_it(
    tmp_path,
):
    """反过来：会议页在京东那场会建成「京东科研仓二期」，再把早就挂着这场会、出处也在这场会的「京东科研仓对接」
    并进它。撤销建成需求要解除这场会、删掉那条，会连带删掉并进来的原话和出处：拒绝，什么都不改。"""
    client, headers, db = make_world(tmp_path)
    existing = create(client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound")
    before_name = snapshot(db)
    made = name_as_requirement(client, headers, "jd", "京东科研仓二期")
    assert merge(client, headers, existing, made["requirement_id"]).status_code == 200
    merged = snapshot(db)

    refused = post(client, headers, f"/api/meetings/{meeting_id('jd')}/name-as-requirement/undo")

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "已有需求并入「京东科研仓二期」，要撤销建成需求，先撤销那次合并"
    )
    assert snapshot(db) == merged

    assert post(client, headers, f"/api/requirements/{existing}/unmerge").status_code == 200
    undone = post(client, headers, f"/api/meetings/{meeting_id('jd')}/name-as-requirement/undo")

    assert undone.status_code == 200, undone.text
    assert undone.json()["requirement_deleted"] is True
    assert snapshot(db) == before_name
    assert (
        client.get(f"/api/requirements/{existing}").json()["source"]["quote"]
        == QUOTES["inbound"][1]
    )


def test_merges_from_before_the_name_action_do_not_block_its_undo(tmp_path):
    """「建成需求」撞名关联到已有的需求（existing），那条需求以前就并进来过别的：撤销照常，只解除这场会。"""
    client, headers, db = make_world(tmp_path)
    existing = create(client, headers, "yimi", "EDC 产研对接", "P1")
    older = create(client, headers, "yimi", "EDC 选型旧稿", "P2")
    assert merge(client, headers, older, existing).status_code == 200
    made = name_as_requirement(client, headers, "edc", "EDC 产研对接")
    assert (made["existing"], made["requirement_id"]) == (True, existing)

    undone = post(client, headers, f"/api/meetings/{meeting_id('edc')}/name-as-requirement/undo")

    assert undone.status_code == 200, undone.text
    assert undone.json()["requirement_deleted"] is False
    assert client.get(f"/api/requirements/{existing}").json()["meetings"] == []


def test_undoing_an_ignored_requirement_name_after_a_merge_restores_it_onto_the_main_requirement(
    tmp_path,
):
    """「不算新需求」的撤销记录里是原来那行「建成了这条」；这条之后被并走，撤销时顺着合并记录写成主需求，
    事件本身不改。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "EDC 产研对接", "P1")
    made = name_as_requirement(client, headers, "edc", "EDC 系统选型")
    ignored = ignore_requirement_name(client, headers, "EDC 系统选型")
    payload = event_payload(db, ignored["event_id"])
    assert payload["undo"]["decisions"][0]["previous"]["requirement_id"] == made["requirement_id"]
    assert merge(client, headers, made["requirement_id"], main).status_code == 200
    assert event_payload(db, ignored["event_id"]) == payload

    undone = post(client, headers, "/api/name-decisions/undo", {"event_id": ignored["event_id"]})

    assert undone.status_code == 200, undone.text
    assert db.query_one(
        "SELECT decision, requirement_id FROM requirement_name_decisions WHERE name_key=?",
        (light_key("EDC 系统选型"),),
    ) == {"decision": "made", "requirement_id": main}
    assert event_payload(db, ignored["event_id"]) == payload


# ---------------------------------------------------------------- 1、2. 改状态（D9、D10、D11）


def test_mark_done_without_open_tasks_records_the_time_and_leaves_the_active_wall(tmp_path):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0")
    task_on(db, "task-done", "推入库单接口", requirement_id, "done")

    response = patch(client, headers, requirement_id, {"status": "done", "close_open_tasks": True})

    assert response.status_code == 200, response.text
    detail = response.json()
    assert (detail["status"], detail["closed_task_count"]) == ("done", 0)
    assert close_to_now(detail["status_changed_at"])
    assert close_to_now(detail["status_undo_until"], plus_minutes=10)
    assert "status_undo" not in detail
    # 需求列表接口和详情一样，不带撤销记录的原始 JSON
    listed = client.get("/api/requirements", params={"project_id": project_id("yimi")}).json()
    assert [(item["id"], item["status_changed_at"]) for item in listed["items"]] == [
        (requirement_id, detail["status_changed_at"])
    ]
    assert "status_undo" not in listed["items"][0]
    assert task_row(db, "task-done")["status"] == "done"
    assert wall(client, status="active")["items"] == []
    (item,) = wall(client, status="done")["items"]
    assert (item["status_changed_at"], item["task_count"], item["open_task_count"]) == (
        detail["status_changed_at"],
        1,
        0,
    )


@pytest.mark.parametrize(("status", "action"), [("done", "标记完成"), ("shelved", "搁置")])
def test_close_open_tasks_cancels_only_unfinished_ones(tmp_path, status, action):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0")
    task_on(db, "task-draft", "补京东接口文档", requirement_id, "pending_confirm")
    task_on(db, "task-confirmed", "和京东确认妥投拍照字段", requirement_id, "confirmed")
    task_on(db, "task-doing", "推入库单接口", requirement_id, "in_progress")
    task_on(db, "task-done", "问清随货通行单谁来传", requirement_id, "done")
    task_on(db, "task-cancelled", "约京东仓现场看", requirement_id, "cancelled")

    detail = patch(
        client, headers, requirement_id, {"status": status, "close_open_tasks": True}
    ).json()

    assert (detail["status"], detail["closed_task_count"], detail["open_task_count"]) == (
        status,
        3,
        0,
    )
    for task_id in ("task-draft", "task-confirmed", "task-doing"):
        assert task_row(db, task_id)["status"] == "cancelled"
        assert task_row(db, task_id)["status_changed_at"] == detail["status_changed_at"]
        assert events(db, task_id) == [
            ("status_changed", f"需求「京东科研仓对接」{action}，一起关掉")
        ]
    for task_id, kept in (("task-done", "done"), ("task-cancelled", "cancelled")):
        assert task_row(db, task_id) == {
            "status": kept,
            "status_changed_at": CHANGED,
            "requirement_id": requirement_id,
            "project_id": project_id("yimi"),
        }
        assert events(db, task_id) == []


def test_open_tasks_stay_unless_asked(tmp_path):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0")
    task_on(db, "task-confirmed", "和京东确认妥投拍照字段", requirement_id, "confirmed")

    shelved = patch(client, headers, requirement_id, {"status": "shelved"}).json()
    reopened = patch(
        client, headers, requirement_id, {"status": "active", "close_open_tasks": True}
    ).json()

    assert (shelved["closed_task_count"], shelved["open_task_count"]) == (0, 1)
    # 重新打开时 close_open_tasks 不起作用
    assert (reopened["status"], reopened["closed_task_count"], reopened["open_task_count"]) == (
        "active",
        0,
        1,
    )
    assert task_row(db, "task-confirmed")["status"] == "confirmed"


def test_editing_other_fields_does_not_touch_the_status_time(tmp_path):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0", status="done")
    before = db.query_one(
        "SELECT status_changed_at, status_undo FROM requirements WHERE id=?", (requirement_id,)
    )

    detail = patch(
        client, headers, requirement_id, {"title": "京东科研仓接口对接", "status": "done"}
    ).json()

    assert detail["title"] == "京东科研仓接口对接"
    assert (
        db.query_one(
            "SELECT status_changed_at, status_undo FROM requirements WHERE id=?",
            (requirement_id,),
        )
        == before
    )


def test_status_undo_restores_the_requirement_and_only_untouched_tasks(tmp_path):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0")
    created = db.query_one(
        "SELECT status_changed_at FROM requirements WHERE id=?", (requirement_id,)
    )
    task_on(db, "task-confirmed", "和京东确认妥投拍照字段", requirement_id, "confirmed")
    task_on(db, "task-doing", "推入库单接口", requirement_id, "in_progress")
    task_on(db, "task-draft", "补京东接口文档", requirement_id, "pending_confirm")
    assert (
        patch(client, headers, requirement_id, {"status": "done", "close_open_tasks": True}).json()[
            "closed_task_count"
        ]
        == 3
    )
    # 这 10 分钟里一条在待办页被重新确认了
    revived = post(client, headers, "/api/tasks/task-draft/status", {"status": "confirmed"})
    assert revived.status_code == 200, revived.text

    response = post(client, headers, f"/api/requirements/{requirement_id}/status-undo")

    assert response.status_code == 200, response.text
    detail = response.json()
    assert (detail["status"], detail["status_changed_at"], detail["status_undo_until"]) == (
        "active",
        created["status_changed_at"],
        None,
    )
    assert task_row(db, "task-confirmed")["status"] == "confirmed"
    assert task_row(db, "task-doing")["status"] == "in_progress"
    for task_id in ("task-confirmed", "task-doing"):
        assert task_row(db, task_id)["status_changed_at"] == CHANGED
    assert events(db, "task-confirmed")[-1] == (
        "reverted",
        "撤销需求「京东科研仓对接」标记完成，恢复为已确认",
    )
    assert events(db, "task-doing")[-1] == (
        "reverted",
        "撤销需求「京东科研仓对接」标记完成，恢复为进行中",
    )
    # 被别处改过的那条不动
    assert task_row(db, "task-draft")["status"] == "confirmed"
    assert events(db, "task-draft")[-1][0] == "status_changed"
    assert titles(wall(client, status="active")) == ["京东科研仓对接"]
    again = post(client, headers, f"/api/requirements/{requirement_id}/status-undo")
    assert (again.status_code, again.json()["detail"]) == (409, "没有可以撤销的状态变更")


def test_status_undo_after_ten_minutes_is_refused(tmp_path):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0")
    task_on(db, "task-confirmed", "和京东确认妥投拍照字段", requirement_id, "confirmed")
    patch(client, headers, requirement_id, {"status": "shelved", "close_open_tasks": True})
    status_changed_minutes_ago(db, requirement_id, 11)

    response = post(client, headers, f"/api/requirements/{requirement_id}/status-undo")

    assert (response.status_code, response.json()["detail"]) == (
        409,
        "改状态超过 10 分钟，不能撤销了",
    )
    assert client.get(f"/api/requirements/{requirement_id}").json()["status_undo_until"] is None
    assert task_row(db, "task-confirmed")["status"] == "cancelled"


def test_status_undo_is_refused_once_the_status_moved_on(tmp_path):
    """标记完成以后，一条进行中的需求并了进来、把它重新打开：那次「标记完成」不能再撤销。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P0")
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2")
    patch(client, headers, main, {"status": "done"})
    assert merge(client, headers, this, main).json()["status"] == "active"

    response = post(client, headers, f"/api/requirements/{main}/status-undo")

    assert (response.status_code, response.json()["detail"]) == (
        409,
        "需求状态之后又改过了，不能撤销",
    )
    # 撤销那次合并，状态回到合并前，「标记完成」又能撤销了
    assert post(client, headers, f"/api/requirements/{this}/unmerge").status_code == 200
    undone = post(client, headers, f"/api/requirements/{main}/status-undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["status"] == "active"


# ---------------------------------------------------------------- 7. 海报墙（D12、D14、D15）


def test_done_tab_sorts_by_completion_time_newest_first(tmp_path):
    client, headers, db = make_world(tmp_path)
    jd = create(client, headers, "yimi", "京东科研仓对接", "P0", status="done")
    family = create(client, headers, "hengrui", "亲友积分入口与导入字段", "P1", status="done")
    five = create(client, headers, "yimi", "新患者注册：五个问题前置", "P2", status="done")
    old = create(client, headers, "cvm", "直播间运营六项修正", "P3", status="done")
    for requirement_id, changed in (
        (jd, "2026-09-20T03:00:00+00:00"),
        (family, "2026-10-02T03:00:00+00:00"),
        (five, "2026-10-05T03:00:00+00:00"),
    ):
        db.execute(
            "UPDATE requirements SET status_changed_at=? WHERE id=?", (changed, requirement_id)
        )
    # 上线前就完成了、没有状态时间的老数据，取 updated_at
    db.execute(
        "UPDATE requirements SET status_changed_at=NULL, updated_at=? WHERE id=?",
        ("2026-09-25T03:00:00+00:00", old),
    )
    task_on(db, "task-a", "推入库单接口", jd, "done")
    task_on(db, "task-b", "约京东仓现场看", jd, "cancelled")
    task_on(db, "task-c", "补京东接口文档", jd, "expired")

    payload = wall(client, status="done")

    assert [item["id"] for item in payload["items"]] == [five, family, old, jd]
    assert [item["status_changed_at"] for item in payload["items"]] == [
        "2026-10-05T03:00:00+00:00",
        "2026-10-02T03:00:00+00:00",
        "2026-09-25T03:00:00+00:00",
        "2026-09-20T03:00:00+00:00",
    ]
    # 待办总数不算已取消、已过期的
    assert payload["items"][3]["task_count"] == 1
    assert client.get(f"/api/requirements/{old}").json()["status_changed_at"] == (
        "2026-09-25T03:00:00+00:00"
    )
    # 别的页签排序不变：项目先后（没排座次的按项目最近一场会，CVM 09-29 最近）、项目内按等级
    assert [item["id"] for item in wall(client, status="all")["items"]] == [old, jd, five, family]


# ---------------------------------------------------------------- 8. 候选并进已完成的需求（D13）


def test_candidate_merged_into_a_done_requirement_reopens_it_and_undo_closes_it_again(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(
        client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound", status="done"
    )
    done_at = db.query_one("SELECT status_changed_at FROM requirements WHERE id=?", (target,))
    candidate_id = candidate(db, "receipt", "京东仓签收凭证", summary=RECEIPT_SUMMARY)
    targets = client.get(f"/api/requirement-candidates/{candidate_id}/merge-targets").json()
    assert [(item["id"], item["status"]) for item in targets["items"]] == [(target, "done")]

    response = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    )

    assert response.status_code == 200, response.text
    detail = response.json()
    assert (detail["status"], detail["reopened"]) == ("active", True)
    assert close_to_now(detail["status_changed_at"])
    assert titles(wall(client, status="active")) == ["京东科研仓对接"]

    undone = post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge")

    assert undone.status_code == 200, undone.text
    after = client.get(f"/api/requirements/{target}").json()
    assert (after["status"], after["status_changed_at"]) == ("done", done_at["status_changed_at"])


def test_candidate_unmerge_leaves_a_status_changed_elsewhere(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", status="done")
    candidate_id = candidate(db, "receipt", "京东仓签收凭证")
    merged = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    ).json()
    assert merged["reopened"] is True
    patch(client, headers, target, {"status": "shelved"})

    assert (
        post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge").status_code
        == 200
    )

    assert client.get(f"/api/requirements/{target}").json()["status"] == "shelved"


def test_candidate_merged_into_an_open_requirement_is_not_reopened(tmp_path):
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", status="shelved")
    candidate_id = candidate(db, "receipt", "京东仓签收凭证")

    detail = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": target},
    ).json()

    assert (detail["status"], detail["reopened"]) == ("shelved", False)


# ---------------------------------------------------------------- v19 → v20 迁移


def test_version_twenty_migration_adds_columns_table_and_freezes_status_time(tmp_path):
    database_path = tmp_path / "workbench.sqlite3"
    db = Database(database_path)
    db.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX idx_requirement_merges_into")
        connection.execute("DROP TABLE requirement_merges")
        connection.execute("ALTER TABLE requirements DROP COLUMN status_changed_at")
        connection.execute("ALTER TABLE requirements DROP COLUMN status_undo")
        connection.execute(
            "INSERT INTO projects(id, name, created_at) VALUES ('p', '医米科研用药', 'x')"
        )
        connection.execute(
            """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
               VALUES ('r-done', 'p', '新患者注册：五个问题前置', 'P2', 'done',
                       '2026-09-21T03:00:00+00:00', '2026-09-28T03:00:00+00:00')"""
        )
        connection.execute("PRAGMA user_version=19")
    backups: list[int] = []

    db.initialize(before_migrate=lambda: backups.append(db.user_version()))

    assert backups == [19]
    assert db.user_version() == 20
    assert db.query_one("SELECT status_changed_at, status_undo FROM requirements") == {
        "status_changed_at": "2026-09-28T03:00:00+00:00",
        "status_undo": None,
    }
    assert db.query_all("SELECT * FROM requirement_merges") == []
    # 再启动一次不再改：之后改名刷新了 updated_at，状态时间不跟着动
    db.execute("UPDATE requirements SET updated_at='2026-10-09T03:00:00+00:00'")
    db.initialize()
    assert db.query_one("SELECT status_changed_at FROM requirements") == {
        "status_changed_at": "2026-09-28T03:00:00+00:00"
    }


# ---------------------------------------------------------------- 第二轮审查（B2–B6）


@pytest.mark.parametrize("order", ["first_merged_first", "last_merged_first"])
def test_unmerging_two_merges_that_share_a_meeting_in_either_order_restores_everything(
    tmp_path, order
):
    """B2：「京东科研仓对接」并进「赠药横跳拦截」时给它新加了京东那场会；「京东仓签收凭证」的原话也在京东
    那场会，后并进来。先撤前一次时这场会因为还有后一次的原话留着，记账交给后一次；正序、倒序撤完都和合并前一样。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "赠药横跳拦截", "P0", meeting_keys=("hengtiao",))
    first = create(client, headers, "yimi", "京东科研仓对接", "P1", source_key="inbound")
    second = create(client, headers, "yimi", "京东仓签收凭证", "P2", source_key="receipt")
    before = snapshot(db)
    assert merge(client, headers, first, main).status_code == 200
    assert merge(client, headers, second, main).status_code == 200

    undo_order = [first, second] if order == "first_merged_first" else [second, first]
    for requirement_id in undo_order:
        response = post(client, headers, f"/api/requirements/{requirement_id}/unmerge")
        assert response.status_code == 200, response.text

    assert snapshot(db) == before


def test_unmerging_a_candidate_hands_a_shared_meeting_to_the_later_requirement_merge(tmp_path):
    """B2 的候选那一边：候选并进来时新加了京东那场会，之后并进来的需求在这场会也有原话；先撤候选，再撤需求合并。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "赠药横跳拦截", "P0", meeting_keys=("hengtiao",))
    later = create(client, headers, "yimi", "京东科研仓对接", "P1", source_key="inbound")
    candidate_id = candidate(db, "receipt", "京东仓签收凭证")
    before = snapshot(db)
    merged = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": main},
    )
    assert merged.status_code == 200, merged.text
    assert merge(client, headers, later, main).status_code == 200

    assert (
        post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge").status_code
        == 200
    )
    assert meeting_id("jd") in [
        m["id"] for m in client.get(f"/api/requirements/{main}").json()["meetings"]
    ]
    assert post(client, headers, f"/api/requirements/{later}/unmerge").status_code == 200

    after = snapshot(db)
    # 候选合并、撤销本来就刷新候选的 updated_at
    for state in (before, after):
        for row in state["requirement_candidates"]:
            row.pop("updated_at")
    assert after == before


def test_undo_marks_point_at_the_merge_that_brought_each_quote(tmp_path):
    """B3：候选「签收凭证拍照」「京东仓签收凭证」先后并进「京东仓签收凭证」，再把这条并进「京东科研仓对接」。
    主需求上，这条搬过来的原话都打需求合并的标记（第二个候选名字和这条相同，两种都符合也取需求合并）；
    直接并进主需求、名字还在原话上的候选打候选标记；主需求自己的出处不打。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound")
    this = create(client, headers, "yimi", "京东仓签收凭证", "P2", meeting_keys=("jd",))
    photo = candidate(db, "receipt", "签收凭证拍照")
    same_name = candidate(db, "inbound", "京东仓签收凭证")
    direct = candidate(db, "export", "科室会预约后台导出")
    db.execute(
        "UPDATE meetings SET project_id=? WHERE id=?", (project_id("yimi"), meeting_id("cvm"))
    )
    for candidate_id, target in ((photo, this), (same_name, this), (direct, main)):
        response = post(
            client,
            headers,
            f"/api/requirement-candidates/{candidate_id}/merge",
            {"requirement_id": target},
        )
        assert response.status_code == 200, response.text
    marks = {
        source["via_candidate_title"]: source["undo_merge"]
        for source in client.get(f"/api/requirements/{this}").json()["sources"]
    }
    assert {title: mark["kind"] for title, mark in marks.items()} == {
        "签收凭证拍照": "candidate",
        "京东仓签收凭证": "candidate",
    }
    assert marks["签收凭证拍照"]["candidate_id"] == photo

    detail = merge(client, headers, this, main).json()

    by_quote = {
        (source["kind"], source["quote"]): source["undo_merge"] for source in detail["sources"]
    }
    assert by_quote[("origin", QUOTES["inbound"][1])] is None
    moved = [
        mark
        for (kind, quote), mark in by_quote.items()
        if kind == "merged" and quote != QUOTES["export"][1]
    ]
    assert len(moved) == 2
    for mark in moved:
        assert {key: mark[key] for key in ("kind", "requirement_id", "title")} == {
            "kind": "requirement",
            "requirement_id": this,
            "title": "京东仓签收凭证",
        }
        assert close_to_now(mark["until"], plus_minutes=10)
    assert {
        key: by_quote[("merged", QUOTES["export"][1])][key] for key in ("kind", "candidate_id")
    } == {
        "kind": "candidate",
        "candidate_id": direct,
    }


def test_requirement_endpoints_on_a_merged_away_id_point_to_where_it_went(tmp_path):
    """B4：被并掉的 id 打需求的其余接口，一律 404 带上并进去的那条；撤销合并照常能用。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P0")
    gone = create(client, headers, "yimi", "京东仓签收凭证", "P2", meeting_keys=("jd",))
    assert merge(client, headers, gone, main).status_code == 200
    expected = {
        "detail": "这条需求已并入「京东科研仓对接」",
        "merged_into": {"id": main, "title": "京东科研仓对接"},
    }
    body_headers = {**headers, "Content-Type": "application/json"}
    base = f"/api/requirements/{gone}"
    calls = [
        client.get(base),
        client.patch(base, json={"priority": "P1"}, headers=headers),
        client.post(f"{base}/status-undo", json={}, headers=headers),
        client.get(f"{base}/merge-targets"),
        client.post(f"{base}/merge", json={"into_requirement_id": main}, headers=headers),
        client.get(f"{base}/context"),
        client.get(f"{base}/decisions"),
        client.put(f"{base}/meetings", json={"meeting_ids": []}, headers=headers),
        client.post(f"{base}/meetings/{meeting_id('jd')}", json={}, headers=headers),
        client.delete(f"{base}/meetings/{meeting_id('jd')}", headers=body_headers),
        client.post(f"{base}/tasks", json={"task_ids": []}, headers=headers),
        client.get(f"{base}/folders/1/files"),
        client.delete(f"{base}/folders/1", headers=body_headers),
    ]
    for response in calls:
        assert (response.status_code, response.json()) == (404, expected), response.request.url
    # 从来没有过的 id 照旧
    assert client.get("/api/requirements/requirement-missing/context").json() == {
        "detail": "需求不存在"
    }
    assert post(client, headers, f"{base}/unmerge").status_code == 200


def test_status_undo_leaves_tasks_moved_to_another_requirement(tmp_path):
    """B5：一起关掉以后，一条待办被改挂到别的需求；撤销只恢复还挂在这条需求上的。"""
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0")
    other = create(client, headers, "yimi", "赠药横跳拦截", "P1")
    task_on(db, "task-stay", "和京东确认妥投拍照字段", requirement_id, "confirmed")
    task_on(db, "task-move", "推入库单接口", requirement_id, "in_progress")
    patch(client, headers, requirement_id, {"status": "done", "close_open_tasks": True})
    moved = client.patch("/api/tasks/task-move", json={"requirement_id": other}, headers=headers)
    assert moved.status_code == 200, moved.text

    assert (
        post(client, headers, f"/api/requirements/{requirement_id}/status-undo").status_code == 200
    )

    assert task_row(db, "task-stay")["status"] == "confirmed"
    assert task_row(db, "task-move")["status"] == "cancelled"
    assert task_row(db, "task-move")["requirement_id"] == other


def test_status_undo_still_restores_tasks_that_an_unmerge_sent_back(tmp_path):
    """B5 的另一面：这条并进主需求，主需求标记完成、一起关掉（含并过来的待办）；撤销合并把待办退回这条，
    再撤销主需求的完成——退回这条的待办照样恢复（撤销合并不算被别处挪走）。"""
    client, headers, db = make_world(tmp_path)
    main = create(client, headers, "yimi", "京东科研仓对接", "P0")
    this = create(client, headers, "yimi", "京东仓签收凭证", "P1")
    task_on(db, "task-main", "推入库单接口", main, "in_progress")
    task_on(db, "task-this", "和京东确认妥投拍照字段", this, "confirmed")
    before = snapshot(db)
    assert merge(client, headers, this, main).status_code == 200
    closed = patch(client, headers, main, {"status": "done", "close_open_tasks": True}).json()
    assert closed["closed_task_count"] == 2
    assert post(client, headers, f"/api/requirements/{this}/unmerge").status_code == 200
    assert task_row(db, "task-this") == {
        "status": "cancelled",
        "status_changed_at": closed["status_changed_at"],
        "requirement_id": this,
        "project_id": project_id("yimi"),
    }

    assert post(client, headers, f"/api/requirements/{main}/status-undo").status_code == 200

    after = snapshot(db)
    for state in (before, after):
        for row in state["requirements"]:
            row.pop("status_undo")
    assert after == before


def merge_candidate(client, headers, candidate_id, requirement_id):
    response = post(
        client,
        headers,
        f"/api/requirement-candidates/{candidate_id}/merge",
        {"requirement_id": requirement_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def unmerge_candidate(client, headers, candidate_id):
    response = post(client, headers, f"/api/requirement-candidates/{candidate_id}/unmerge")
    assert response.status_code == 200, response.text


def status_of(client, requirement_id):
    return client.get(f"/api/requirements/{requirement_id}").json()["status"]


def test_candidate_unmerge_keeps_it_open_when_more_was_merged_in_after_the_reopen(tmp_path):
    """B6：两条候选先后并进已完成的需求（第一条把它重新打开）。先撤第一条：之后还并进来过第二条，不改回已完成；
    第二条没打开过它，撤了也不动。"""
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", status="done")
    first = candidate(db, "receipt", "京东仓签收凭证")
    second = candidate(db, "inbound", "京东入库单推送")
    assert merge_candidate(client, headers, first, target)["reopened"] is True
    assert merge_candidate(client, headers, second, target)["reopened"] is False

    unmerge_candidate(client, headers, first)
    assert status_of(client, target) == "active"
    unmerge_candidate(client, headers, second)
    assert status_of(client, target) == "active"


def test_candidate_unmerge_closes_it_again_once_later_merges_are_undone(tmp_path):
    """B6 倒序：先撤后并进来的那条，再撤打开它的那条，回到已完成。"""
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", status="done")
    first = candidate(db, "receipt", "京东仓签收凭证")
    second = candidate(db, "inbound", "京东入库单推送")
    merge_candidate(client, headers, first, target)
    merge_candidate(client, headers, second, target)

    unmerge_candidate(client, headers, second)
    unmerge_candidate(client, headers, first)

    assert status_of(client, target) == "done"


def test_candidate_unmerge_keeps_it_open_after_a_requirement_was_merged_in(tmp_path):
    """B6：候选把已完成的需求重新打开以后，又有一条需求并了进来：撤销候选合并不改回已完成。"""
    client, headers, db = make_world(tmp_path)
    target = create(client, headers, "yimi", "京东科研仓对接", "P0", status="done")
    later = create(client, headers, "yimi", "京东科研仓二期", "P1", status="shelved")
    first = candidate(db, "receipt", "京东仓签收凭证")
    merge_candidate(client, headers, first, target)
    assert merge(client, headers, later, target).status_code == 200

    unmerge_candidate(client, headers, first)

    assert status_of(client, target) == "active"
