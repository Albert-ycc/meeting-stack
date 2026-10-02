"""服务端执行「挂到需求」的范围（R07-14）：任务改挂、确认时挂的需求和候选，服务端认的和挂需求选择器
（/api/tasks/<id>/requirement-options）给的选项是同一条规矩，不再只靠前端的选项限制，直接调接口绕不过去。

- 新挂的需求必须是进行中的（待确认和已确认的任务都一样，选择器给的需求只有进行中的）；
- 候选只有待确认的任务、或没归项目的任务能挂（已确认且有项目的任务只挂需求）；
- 只拦「新挂」：任务现在挂着的（哪怕那条需求后来搁置了）原样保存不报错；
- 刚改过挂接（10 分钟内）时可以改回去：前端的［撤销］把原来的挂接写回去，原来的那条这期间可能已经搁置、
  或是确认时挂的候选；
- 需求详情页的「新建任务」「关联已有任务」（POST /api/tasks 带需求、POST /api/requirements/<id>/tasks）不拦：
  那个页面对已完成、已搁置的需求也给这两个按钮。
"""

from __future__ import annotations

from .requirement_pool_world import meeting_id
from .test_task_due import make_cvm_world
from .test_todo import create_requirement, options

LONG_AGO = "2026-01-01T00:00:00+00:00"


def make_world(tmp_path, monkeypatch):
    """CVM 云讲堂 09-29 那场会：两条待确认任务、一条待认领候选；再建进行中、已搁置、已完成的需求各一条。"""
    client, headers, db, candidate_id, tasks = make_cvm_world(tmp_path, monkeypatch)
    active = create_requirement(client, headers, "cvm", "直播间运营六项修正")
    shelved = create_requirement(client, headers, "cvm", "直播间画面比例缩到 95%")
    done = create_requirement(client, headers, "cvm", "白介素读法修正")
    client.patch(f"/api/requirements/{shelved}", json={"status": "shelved"}, headers=headers)
    client.patch(f"/api/requirements/{done}", json={"status": "done"}, headers=headers)
    return client, headers, db, candidate_id, tasks, active, shelved, done


def link(client, headers, task_id, **body):
    return client.patch(f"/api/tasks/{task_id}", json=body, headers=headers)


def forget_link_changes(db, task_id):
    """抹掉任务的挂接留痕：模拟「挂接很久没动过」，不在撤销窗口里。"""
    db.execute(
        "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='requirement_changed'",
        (LONG_AGO, task_id),
    )


def test_confirmed_task_only_moves_onto_active_requirements(tmp_path, monkeypatch):
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)

    on_shelved = link(client, headers, chase, requirement_id=shelved)
    on_done = link(client, headers, chase, requirement_id=done)

    assert on_shelved.status_code == 409
    assert (
        on_shelved.json()["detail"] == "需求「直播间画面比例缩到 95%」已搁置，只能挂到进行中的需求"
    )
    assert on_done.status_code == 409
    assert on_done.json()["detail"] == "需求「白介素读法修正」已完成，只能挂到进行中的需求"
    assert db.query_one("SELECT requirement_id FROM tasks WHERE id=?", (chase,)) == {
        "requirement_id": None
    }
    assert link(client, headers, chase, requirement_id=active).status_code == 200


def test_drafts_follow_the_same_requirement_scope_but_may_hang_on_candidates(tmp_path, monkeypatch):
    """待确认的任务照 requirement_options：需求只列进行中的，候选能挂。确认时挂需求同样要进行中的。"""
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]

    assert link(client, headers, script, requirement_id=shelved).status_code == 409
    assert link(client, headers, script, candidate_id=candidate_id).status_code == 200
    refused = client.post(
        f"/api/tasks/{chase}/confirm", json={"requirement_id": shelved}, headers=headers
    )
    assert refused.status_code == 409 and "已搁置" in refused.json()["detail"]
    assert db.query_one("SELECT status FROM tasks WHERE id=?", (chase,)) == {
        "status": "pending_confirm"
    }
    confirmed = client.post(
        f"/api/tasks/{chase}/confirm", json={"requirement_id": active}, headers=headers
    )
    assert confirmed.status_code == 200 and confirmed.json()["requirement_id"] == active


def test_confirmed_task_with_a_project_does_not_hang_on_candidates(tmp_path, monkeypatch):
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)

    refused = link(client, headers, chase, candidate_id=candidate_id)

    assert refused.status_code == 409
    assert refused.json()["detail"] == "已确认的任务不能再挂候选，只能挂到进行中的需求"
    assert db.query_one("SELECT candidate_id FROM tasks WHERE id=?", (chase,)) == {
        "candidate_id": None
    }
    # 没归项目的已确认任务可以挂同一场会的候选（选择器给的范围）
    db.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (chase,))
    assert link(client, headers, chase, candidate_id=candidate_id).status_code == 200


def test_keeping_the_current_link_is_not_a_new_link(tmp_path, monkeypatch):
    """任务现在挂着已搁置的需求：编辑弹窗保存时会把它原样再发一遍，不能因此报错。"""
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)
    db.execute("UPDATE tasks SET requirement_id=? WHERE id=?", (shelved, chase))

    kept = link(client, headers, chase, requirement_id=shelved, title="催填节后三场信息（改名）")

    assert kept.status_code == 200
    assert kept.json()["requirement_id"] == shelved


def test_server_accepts_exactly_what_the_picker_offers(tmp_path, monkeypatch):
    """同一个范围里的每个需求、候选：选择器列了的服务端收，没列的服务端拒（跨项目挂需求是另一回事，
    任务项目会跟着需求走，这里不比）。"""
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )
    script, chase = tasks["话术修改稿发执行群走默示确认"], tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)
    loose = client.post(
        "/api/tasks", json={"title": "核查剪辑导出缺医院、科室字段问题"}, headers=headers
    ).json()["id"]
    db.execute("UPDATE tasks SET meeting_id=? WHERE id=?", (meeting_id("cvm"), loose))
    # 没归项目的任务按来源会议划范围：把三条需求都关联到这场会
    for requirement_id in (active, shelved, done):
        client.post(
            f"/api/requirements/{requirement_id}/meetings/{meeting_id('cvm')}",
            json={},
            headers=headers,
        )
    targets = [("requirement_id", key) for key in (active, shelved, done)] + [
        ("candidate_id", candidate_id)
    ]

    checked = 0
    for task_id, label in (
        (script, "待确认有项目"),
        (chase, "已确认有项目"),
        (loose, "已确认没项目"),
    ):
        offered = {option["id"] for option in options(client, task_id)["options"]}
        for field, target in targets:
            # 每次都从「没挂过、挂接很久没动过」开始，不落进撤销窗口
            db.execute(
                "UPDATE tasks SET requirement_id=NULL, candidate_id=NULL WHERE id=?", (task_id,)
            )
            forget_link_changes(db, task_id)
            if label == "已确认没项目":
                # 挂上需求时任务项目会跟着需求走，每次都退回没归项目
                db.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (task_id,))
            accepted = link(client, headers, task_id, **{field: target}).status_code == 200
            assert accepted == (target in offered), (label, field, target, offered)
            checked += 1
    assert checked == 12


def test_a_link_change_can_be_put_back_right_after(tmp_path, monkeypatch):
    """前端挂接后的［撤销］把原来的写回去：原来挂的需求后来搁置了、原来挂的是确认时挂的候选，都要能写回去。
    不是刚改过的，同样的请求照样拦。"""
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )
    chase = tasks["催填节后三场信息"]
    client.post(f"/api/tasks/{chase}/confirm", json={}, headers=headers)

    # 候选：确认时挂上的、没有留痕的（AI 配好的草稿直接确认）
    db.execute("UPDATE tasks SET candidate_id=? WHERE id=?", (candidate_id, chase))
    assert link(client, headers, chase, requirement_id=active).status_code == 200
    assert link(client, headers, chase, candidate_id=candidate_id).status_code == 200
    assert db.query_one("SELECT candidate_id FROM tasks WHERE id=?", (chase,)) == {
        "candidate_id": candidate_id
    }

    # 已搁置的需求
    db.execute("UPDATE tasks SET candidate_id=NULL, requirement_id=? WHERE id=?", (shelved, chase))
    assert link(client, headers, chase, requirement_id=active).status_code == 200
    assert link(client, headers, chase, requirement_id=shelved).status_code == 200
    assert db.query_one("SELECT requirement_id FROM tasks WHERE id=?", (chase,)) == {
        "requirement_id": shelved
    }

    # 过了撤销窗口：同样的请求拦下
    assert link(client, headers, chase, requirement_id=active).status_code == 200
    forget_link_changes(db, chase)
    assert link(client, headers, chase, requirement_id=shelved).status_code == 409
    assert db.query_one("SELECT requirement_id FROM tasks WHERE id=?", (chase,)) == {
        "requirement_id": active
    }


def test_requirement_detail_buttons_still_work_on_closed_requirements(tmp_path, monkeypatch):
    """需求详情页对已搁置、已完成的需求也给「新建任务」「关联已有任务」，这两个入口不拦。"""
    client, headers, db, candidate_id, tasks, active, shelved, done = make_world(
        tmp_path, monkeypatch
    )

    created = client.post(
        "/api/tasks",
        json={"title": "补记一条已完成需求的收尾任务", "requirement_id": done},
        headers=headers,
    )
    attached = client.post(
        f"/api/requirements/{shelved}/tasks",
        json={"task_ids": [tasks["催填节后三场信息"]]},
        headers=headers,
    )

    assert created.status_code == 200 and created.json()["requirement_id"] == done
    assert attached.status_code == 200
    assert db.query_one(
        "SELECT requirement_id FROM tasks WHERE id=?", (tasks["催填节后三场信息"],)
    ) == {"requirement_id": shelved}
