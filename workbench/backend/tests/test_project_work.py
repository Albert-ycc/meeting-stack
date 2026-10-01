"""项目页与待办改版·项目详情（R06-3～13）：需求与任务、录音两个标签页的数据。

需求名、任务名照口径书 P02 医米科研用药的示意（任务名都是库里真实任务），会议照生产库。
"""

from __future__ import annotations

from .requirement_pool_world import meeting_id, project_id
from .test_task_due import RECEIPT, make_jd_world
from .test_todo import create_requirement, insert_task, link_meeting


def work(client, key="yimi"):
    response = client.get(f"/api/projects/{project_id(key)}/work")
    assert response.status_code == 200, response.text
    return response.json()


def hang(db, task_id, requirement_id):
    db.execute("UPDATE tasks SET requirement_id=? WHERE id=?", (requirement_id, task_id))


def make_yimi(tmp_path, monkeypatch):
    """医米：京东科研仓对接（P0，关联 09-16 京东那场）、赠药横跳拦截（P0，关联 09-10 横跳那场），
    已完成的「新患者注册：五个问题前置」、已搁置的「EDC 系统选型」；EDC 那场会的三条跟进事项没挂需求，
    其中一条挂在京东那场会的待认领候选「京东仓签收凭证」上。"""
    client, headers, db, settings, receipt, edc_task = make_jd_world(tmp_path, monkeypatch)
    yimi = project_id("yimi")
    jd = create_requirement(client, headers, "yimi", "京东科研仓对接", "P0")
    link_meeting(client, headers, jd, meeting_id("jd"))
    hengtiao = create_requirement(client, headers, "yimi", "赠药横跳拦截", "P0")
    link_meeting(client, headers, hengtiao, meeting_id("hengtiao"))
    five = create_requirement(client, headers, "yimi", "新患者注册：五个问题前置", "P2")
    edc = create_requirement(client, headers, "yimi", "EDC 系统选型", "P1")
    client.patch(f"/api/requirements/{five}", json={"status": "done"}, headers=headers)
    client.patch(f"/api/requirements/{edc}", json={"status": "shelved"}, headers=headers)
    rows = [
        (
            "t-jd-1",
            "梳理京东开放平台需对接的接口清单",
            "done",
            "jd",
            jd,
            "2026-09-17T01:00:00+00:00",
        ),
        (
            "t-jd-2",
            "与合作方协商落实采购单号告知物流司机",
            "done",
            "jd",
            jd,
            "2026-09-17T02:00:00+00:00",
        ),
        ("t-ht-1", "把跨政策横跳卡控作为需求立项并修复", "confirmed", "hengtiao", hengtiao, None),
        (
            "t-ht-2",
            "排查现网是否已有同一药品不能重复申领能力，若无则9月完成",
            "in_progress",
            "hengtiao",
            hengtiao,
            None,
        ),
        (
            "t-ht-3",
            "就艾瑞康、艾瑞妮项目是否与济佰世同步给崔成回复",
            "done",
            "hengtiao",
            hengtiao,
            "2026-09-11T01:00:00+00:00",
        ),
        ("t-five-1", "新患者注册页五个问题前置改版", "done", "five", five, None),
        ("t-edc-2", "安排与华谊的会", "confirmed", "edc", None, None),
        ("t-edc-3", "与萌总同步 EDC 选型议题", "pending_confirm", "edc", None, None),
        ("t-edc-4", "确认 EDC 供应商报价", "done", "edc", None, None),
    ]
    for task_id, title, status, meeting_key, requirement_id, changed in rows:
        insert_task(
            db, task_id, title, status=status, meeting=meeting_id(meeting_key), project=yimi
        )
        if requirement_id:
            hang(db, task_id, requirement_id)
        if changed:
            db.execute("UPDATE tasks SET status_changed_at=? WHERE id=?", (changed, task_id))
    return (
        client,
        headers,
        db,
        {"jd": jd, "hengtiao": hengtiao, "five": five, "edc": edc},
        receipt,
        edc_task,
    )


def test_requirements_carry_their_task_panels(tmp_path, monkeypatch):
    """进行中需求按 P0→P3、同级最近会议在前；面板里未完成的在前，完成的按完成先后排在末尾。"""
    client, headers, db, ids, receipt, edc_task = make_yimi(tmp_path, monkeypatch)

    payload = work(client)

    assert [item["title"] for item in payload["requirements"]] == ["京东科研仓对接", "赠药横跳拦截"]
    jd, hengtiao = payload["requirements"]
    assert [task["title"] for task in jd["tasks"]] == [
        "梳理京东开放平台需对接的接口清单",
        "与合作方协商落实采购单号告知物流司机",
    ]
    assert (jd["task_count"], jd["open_task_count"]) == (2, 0)
    # 两条未完成的截止、来源会议都一样，后建的在前（同待办组内顺序）
    assert [task["title"] for task in hengtiao["tasks"]] == [
        "排查现网是否已有同一药品不能重复申领能力，若无则9月完成",
        "把跨政策横跳卡控作为需求立项并修复",
        "就艾瑞康、艾瑞妮项目是否与济佰世同步给崔成回复",
    ]
    assert hengtiao["open_task_count"] == 2
    assert [
        (item["title"], item["status"], item["task_count"], item["all_done"])
        for item in payload["closed_requirements"]
    ] == [
        ("EDC 系统选型", "shelved", 0, False),
        ("新患者注册：五个问题前置", "done", 1, True),
    ]


def test_unlinked_tasks_and_pending_candidates(tmp_path, monkeypatch):
    """没挂需求的任务只列已确认、进行中的（挂在候选上的带候选名）；待认领候选给横幅用。"""
    client, headers, db, ids, receipt, edc_task = make_yimi(tmp_path, monkeypatch)

    payload = work(client)

    unlinked = {task["title"]: task for task in payload["unlinked_tasks"]}
    assert set(unlinked) == {"确认产研能否派一人对接 EDC", "安排与华谊的会"}
    assert unlinked["确认产研能否派一人对接 EDC"]["candidate_title"] == RECEIPT["title"]
    assert payload["pending_candidates"] == [{"id": receipt, "title": "京东仓签收凭证"}]

    client.patch(f"/api/tasks/{edc_task}", json={"requirement_id": ids["jd"]}, headers=headers)
    moved = work(client)
    assert "确认产研能否派一人对接 EDC" in [
        task["title"] for task in moved["requirements"][0]["tasks"]
    ]
    assert len(moved["unlinked_tasks"]) == 1
    assert client.get("/api/projects/project-nope/work").status_code == 404


def test_recordings_list_meetings_with_task_counts(tmp_path, monkeypatch):
    """录音：按录音时间倒序，每场带关联需求和这场会抽出的任务数（手动建的不算）。"""
    client, headers, db, ids, receipt, edc_task = make_yimi(tmp_path, monkeypatch)

    response = client.get(f"/api/projects/{project_id('yimi')}/recordings")

    assert response.status_code == 200, response.text
    rows = {row["title"]: row for row in response.json()}
    assert [row["title"] for row in response.json()] == [
        "EDC 系统选型与产研对接决策",
        "新患者注册流程五个问题前置",
        "260916 医米京东科研仓系统对接",
        "医米赠药横跳拦截规则",
    ]
    assert rows["EDC 系统选型与产研对接决策"]["task_count"] == 3
    assert rows["医米赠药横跳拦截规则"]["task_count"] == 3
    assert [item["title"] for item in rows["260916 医米京东科研仓系统对接"]["requirements"]] == [
        "京东科研仓对接"
    ]
    assert client.get("/api/projects/project-nope/recordings").status_code == 404


def test_undated_tasks_sink_and_recordings_count_kept_tasks(tmp_path, monkeypatch):
    """面板和没挂需求区里未定截止的排在有截止的后面；录音的任务数不算已过期、已取消的。"""
    client, headers, db, ids, receipt, edc_task = make_yimi(tmp_path, monkeypatch)
    db.execute("UPDATE tasks SET due_date='2026-10-08' WHERE id='t-ht-1'")
    db.execute("UPDATE tasks SET due_date='2026-09-25' WHERE id='t-edc-2'")
    db.execute("UPDATE tasks SET status='expired' WHERE id='t-edc-3'")

    payload = work(client)

    hengtiao = payload["requirements"][1]
    assert [task["id"] for task in hengtiao["tasks"]] == ["t-ht-1", "t-ht-2", "t-ht-3"]
    assert [task["title"] for task in payload["unlinked_tasks"]][0] == "安排与华谊的会"
    rows = {
        row["title"]: row
        for row in client.get(f"/api/projects/{project_id('yimi')}/recordings").json()
    }
    # EDC 那场三条：一条已确认、一条刚过期、一条已完成 → 留着的 2 条
    assert rows["EDC 系统选型与产研对接决策"]["task_count"] == 2


def test_recordings_sort_by_real_time_across_offsets(tmp_path, monkeypatch):
    """录音时间混着 -07:00 和 +00:00：按真实时刻倒序，不按字符串。"""
    client, headers, db, ids, receipt, edc_task = make_yimi(tmp_path, monkeypatch)
    # 北京 10-01 00:30 和北京 09-30 23:00：字符串比较会把后者排前面
    db.execute(
        "UPDATE meetings SET recording_date='2026-09-30T09:30:00-07:00' WHERE id=?",
        (meeting_id("edc"),),
    )
    db.execute(
        "UPDATE meetings SET recording_date='2026-09-30T15:00:00+00:00' WHERE id=?",
        (meeting_id("five"),),
    )

    titles = [
        row["title"] for row in client.get(f"/api/projects/{project_id('yimi')}/recordings").json()
    ]

    assert titles[:2] == ["EDC 系统选型与产研对接决策", "新患者注册流程五个问题前置"]


def test_moved_meeting_candidates_are_counted_once(tmp_path, monkeypatch):
    """候选那场会改归医米、医米里已有同名待认领候选：项目卡片和详情横幅只算一条，和需求池同一口径。"""
    client, headers, db, ids, receipt, edc_task = make_yimi(tmp_path, monkeypatch)
    db.execute(
        """INSERT INTO requirement_candidates(id, meeting_id, title, name_key, status,
                                              project_id_seen, created_at, updated_at)
           VALUES ('c-dup', ?, '京东仓签收凭证', (SELECT name_key FROM requirement_candidates WHERE id=?),
                   'pending', ?, 'x', 'x')""",
        (meeting_id("cvm"), receipt, project_id("cvm")),
    )
    db.execute(
        "UPDATE meetings SET project_id=? WHERE id=?", (project_id("yimi"), meeting_id("cvm"))
    )

    banner = work(client)["pending_candidates"]
    cards = {item["id"]: item for item in client.get("/api/projects").json()}

    assert [item["title"] for item in banner] == ["京东仓签收凭证"]
    assert cards[project_id("yimi")]["pending_candidate_count"] == 1
