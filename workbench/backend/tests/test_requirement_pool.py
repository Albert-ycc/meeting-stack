"""需求池改版 v17：海报墙排序与三级筛选（R02）、项目座次（R03）、需求的说明和来源（R04/R05）。

墙上的数据照《口径书》：座次 1 医米科研用药、2 恒瑞健康、3 华夏基金会科普同行、4 CVM 云讲堂、5 安心四季，
九条进行中、一条已完成、一条已搁置、三条待认领候选。会议和原话见 requirement_pool_world。
"""

from __future__ import annotations

from meeting_workbench.db import Database, utc_now
from meeting_workbench.requirement_candidates import insert_candidate

from .requirement_pool_world import QUOTES, meeting_id, project_id, seed_world, source
from .test_tasks_api import make_client, write_headers

JD_SUMMARY = (
    "把京东科研仓当作一个药房接进医米：采购单入库、销售单、订单取消、物流轨迹四类接口必须对上，"
    "签收凭证怎么拿还悬着。"
)


def make_world(tmp_path, *, projects=None):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    if projects is None:
        seed_world(db)
    else:
        seed_world(db, projects=projects)
    return client, headers, db


def create(
    client,
    headers,
    project_key,
    title,
    priority,
    *,
    summary="",
    source_key=None,
    meeting_keys=(),
    status=None,
):
    body = {
        "project_id": project_id(project_key),
        "title": title,
        "priority": priority,
        "summary": summary,
    }
    if source_key:
        body["source"] = source(source_key)
    response = client.post("/api/requirements", json=body, headers=headers)
    assert response.status_code == 200, response.text
    requirement_id = response.json()["id"]
    for key in meeting_keys:
        linked = client.post(
            f"/api/requirements/{requirement_id}/meetings/{meeting_id(key)}",
            json={},
            headers=headers,
        )
        assert linked.status_code == 200, linked.text
    if status:
        changed = client.patch(
            f"/api/requirements/{requirement_id}", json={"status": status}, headers=headers
        )
        assert changed.status_code == 200, changed.text
    return requirement_id


def candidate(db, quote_key, title, *, summary="", similar=None):
    meeting_key, quote, anchor_ms = QUOTES[quote_key]
    with db.transaction() as connection:
        return insert_candidate(
            connection,
            meeting_id=meeting_id(meeting_key),
            title=title,
            summary=summary,
            quote=quote,
            anchor_ms=anchor_ms,
            similar_requirement_id=similar,
        )


def seat(client, headers, *keys):
    response = client.put(
        "/api/project-seats",
        json={"project_ids": [project_id(key) for key in keys]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def build_wall(client, headers, db):
    ids = {
        "jd": create(
            client,
            headers,
            "yimi",
            "京东科研仓对接",
            "P0",
            summary=JD_SUMMARY,
            source_key="inbound",
        ),
        "hengtiao": create(
            client,
            headers,
            "yimi",
            "赠药横跳拦截",
            "P0",
            summary="同一周期两个赠药项目并存时只能进一个；在组期间重复申请在提交环节直接拦，"
            "存量重复在审核端弹窗、只给驳回。",
            meeting_keys=("hengtiao",),
        ),
        "edc": create(
            client, headers, "yimi", "EDC 系统选型", "P1", meeting_keys=("edc",), status="shelved"
        ),
        "five": create(
            client,
            headers,
            "yimi",
            "新患者注册：五个问题前置",
            "P2",
            meeting_keys=("five",),
            status="done",
        ),
        "family": create(
            client, headers, "hengrui", "亲友积分入口与导入字段", "P1", source_key="family"
        ),
        "blackcard": create(
            client, headers, "hengrui", "黑卡注销后的分享限制", "P2", meeting_keys=("blackcard",)
        ),
        "huaxia": create(
            client, headers, "huaxia", "劳务签署证据链导出", "P0", meeting_keys=("huaxia",)
        ),
        "cvm": create(client, headers, "cvm", "直播间运营六项修正", "P1", meeting_keys=("cvm",)),
        "aitan": create(
            client, headers, "anxin", "艾坦联合艾瑞卡超期出组", "P1", meeting_keys=("aitan",)
        ),
        "copy": create(
            client, headers, "copy", "项目复制：名单一键转移", "P2", meeting_keys=("copy",)
        ),
        "oral": create(client, headers, "oral", "到店领取药房白名单", "P2", meeting_keys=("oral",)),
    }
    ids["c_export"] = candidate(
        db,
        "export",
        "科室会预约后台导出",
        summary="预约审核页现在不能导出 Excel，运营要把科室会预约名单拉出来对表。",
    )
    ids["c_receipt"] = candidate(
        db,
        "receipt",
        "京东仓签收凭证",
        summary="京东妥投只靠配送员点击，医米拿不到患者签收凭证；月结上千万物流费不能只凭汇总表付款。",
        similar=ids["jd"],
    )
    ids["c_hospital"] = candidate(
        db,
        "hospital",
        "医院名单匹配规则",
        summary="客户一次给上千家医院，能匹配上的告知成功，匹配不上的退回客户再确认。",
    )
    seat(client, headers, "yimi", "hengrui", "huaxia", "cvm", "anxin")
    return ids


def wall(client, **params):
    response = client.get("/api/requirement-pool", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def titles(payload):
    return [item["title"] for item in payload["items"]]


# ---------------------------------------------------------------- R02 / R03 排序与计数


def test_active_wall_sorts_by_seat_then_priority_then_latest_meeting(tmp_path):
    client, headers, db = make_world(tmp_path)
    build_wall(client, headers, db)

    payload = wall(client, status="active")

    # S05：医米两条 P0 按最近会议由近到远（09-16 在 09-10 前）；未排座次的两个项目按项目最近会议排
    assert titles(payload) == [
        "京东科研仓对接",
        "赠药横跳拦截",
        "亲友积分入口与导入字段",
        "黑卡注销后的分享限制",
        "劳务签署证据链导出",
        "直播间运营六项修正",
        "艾坦联合艾瑞卡超期出组",
        "项目复制：名单一键转移",
        "到店领取药房白名单",
    ]
    assert payload["counts"] == {"pending": 3, "active": 9, "done": 1, "shelved": 1, "all": 14}
    assert payload["total"] == 9
    first = payload["items"][0]
    assert first["kind"] == "requirement"
    assert first["summary"] == JD_SUMMARY
    assert (first["project_name"], first["project_seat"], first["priority"]) == (
        "医米科研用药",
        1,
        "P0",
    )
    assert first["source"]["meeting_title"] == "260916 医米京东科研仓系统对接"
    assert first["source"]["recording_date"] == "2026-09-16T19:01:50-07:00"
    assert first["source"]["duration_ms"] == 3033387
    assert (first["source"]["quote"], first["source"]["anchor_ms"]) == QUOTES["inbound"][1:]
    assert (first["meeting_count"], first["follow_up_count"]) == (1, 0)
    assert first["latest_meeting_date"] == "2026-09-16T19:01:50-07:00"
    # 没有来源的需求海报上不显示来源录音
    assert payload["items"][1]["source"] is None
    assert not any(key.startswith("_") for key in first)


def test_direction_bar_lists_seated_then_unseated_by_latest_meeting_with_tab_counts(tmp_path):
    client, headers, db = make_world(tmp_path)
    build_wall(client, headers, db)

    payload = wall(client, status="active")

    bar = [(project["name"], project["seat"], project["count"]) for project in payload["projects"]]
    assert bar == [
        ("医米科研用药", 1, 2),
        ("恒瑞健康", 2, 2),
        ("华夏基金会科普同行", 3, 1),
        ("CVM 云讲堂", 4, 1),
        ("安心四季", 5, 1),
        ("项目复制与名单一键转移", None, 1),
        ("口服药到店领取配置方案", None, 1),
        ("寻呼随访项目", None, 0),
    ]
    assert payload["unassigned_count"] == 0
    # S01：待认领页签下，条上的数换成各项目的候选数
    pending_bar = {
        project["name"]: project["count"] for project in wall(client, status="pending")["projects"]
    }
    assert pending_bar["医米科研用药"] == 1 and pending_bar["恒瑞健康"] == 0
    assert pending_bar["CVM 云讲堂"] == 1


def test_project_and_priority_filters_match_s06_counts(tmp_path):
    client, headers, db = make_world(tmp_path)
    build_wall(client, headers, db)

    payload = wall(
        client,
        status="active",
        project_id=f"{project_id('yimi')},{project_id('hengrui')}",
        priority="P0,P1",
    )

    assert titles(payload) == ["京东科研仓对接", "赠药横跳拦截", "亲友积分入口与导入字段"]
    # 候选没有等级，不受优先级条件影响；页签计数随项目和优先级变
    assert payload["counts"] == {"pending": 1, "active": 3, "done": 0, "shelved": 1, "all": 5}
    # 「我的方向」条上的数只随页签变，不随项目、优先级变
    bar = {project["name"]: project["count"] for project in payload["projects"]}
    assert bar["恒瑞健康"] == 2 and bar["华夏基金会科普同行"] == 1


def test_pending_tab_sorts_candidates_by_seat_with_default_actions(tmp_path):
    client, headers, db = make_world(tmp_path)
    ids = build_wall(client, headers, db)

    payload = wall(client, status="pending")

    assert titles(payload) == ["京东仓签收凭证", "医院名单匹配规则", "科室会预约后台导出"]
    receipt, hospital, export = payload["items"]
    assert receipt["kind"] == "candidate" and receipt["priority"] is None
    assert receipt["default_action"] == "merge"
    assert receipt["similar_requirement"] == {
        "id": ids["jd"],
        "title": "京东科研仓对接",
        "status": "active",
    }
    assert (export["default_action"], export["similar_requirement"]) == ("claim", None)
    assert all(item["can_merge"] for item in payload["items"])
    assert (export["project_name"], export["project_seat"]) == ("CVM 云讲堂", 4)
    assert export["source"]["meeting_title"] == "260929 云课堂直播运营问题对齐"
    assert (export["source"]["quote"], export["source"]["anchor_ms"]) == QUOTES["export"][1:]
    assert export["latest_meeting_date"] == "2026-09-29T19:26:37-07:00"
    assert (hospital["meeting_count"], hospital["open_task_count"]) == (1, 0)


def test_all_tab_puts_candidates_after_p3_inside_their_project(tmp_path):
    client, headers, db = make_world(tmp_path)
    build_wall(client, headers, db)

    payload = wall(client, status="all")

    assert titles(payload) == [
        "京东科研仓对接",
        "赠药横跳拦截",
        "EDC 系统选型",
        "新患者注册：五个问题前置",
        "京东仓签收凭证",
        "亲友积分入口与导入字段",
        "黑卡注销后的分享限制",
        "劳务签署证据链导出",
        "医院名单匹配规则",
        "直播间运营六项修正",
        "科室会预约后台导出",
        "艾坦联合艾瑞卡超期出组",
        "项目复制：名单一键转移",
        "到店领取药房白名单",
    ]
    assert payload["total"] == payload["counts"]["all"] == 14
    statuses = {item["title"]: item["status"] for item in payload["items"]}
    assert statuses["EDC 系统选型"] == "shelved" and statuses["京东仓签收凭证"] == "pending"


def test_name_search_and_bad_filters(tmp_path):
    client, headers, db = make_world(tmp_path)
    build_wall(client, headers, db)

    payload = wall(client, status="pending", q="签收")
    assert titles(payload) == ["京东仓签收凭证"]
    assert payload["counts"] == {"pending": 1, "active": 0, "done": 0, "shelved": 0, "all": 1}
    assert titles(wall(client, status="active", q="excel")) == []
    assert client.get("/api/requirement-pool", params={"status": "bogus"}).status_code == 400
    assert client.get("/api/requirement-pool", params={"priority": "P9"}).status_code == 400


def test_unseated_projects_follow_latest_meeting_and_unassigned_comes_last(tmp_path):
    """只给医米排座次：其余项目按项目最近会议由近到远，没有会议的项目再往后，未归项目永远最后。"""
    client, headers, db = make_world(tmp_path)
    create(client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound")
    create(client, headers, "huaxia", "劳务签署证据链导出", "P0", meeting_keys=("huaxia",))
    create(client, headers, "cvm", "直播间运营六项修正", "P3", meeting_keys=("cvm",))
    create(client, headers, "pager", "寻呼随访上线配置", "P0")
    candidate(db, "doctor", "医生资质 AI 审核规则")
    seat(client, headers, "yimi")

    payload = wall(client, status="all")

    assert titles(payload) == [
        "京东科研仓对接",
        "直播间运营六项修正",
        "劳务签署证据链导出",
        "寻呼随访上线配置",
        "医生资质 AI 审核规则",
    ]
    unassigned = payload["items"][-1]
    assert (unassigned["project_id"], unassigned["project_name"]) == (None, None)
    assert (unassigned["can_merge"], unassigned["default_action"]) == (False, "claim")
    assert payload["unassigned_count"] == 1
    assert [project["name"] for project in payload["projects"]] == [
        "医米科研用药",
        "CVM 云讲堂",
        "恒瑞健康",
        "安心四季",
        "项目复制与名单一键转移",
        "口服药到店领取配置方案",
        "华夏基金会科普同行",
        "寻呼随访项目",
    ]
    only_unassigned = wall(client, status="all", project_id="unassigned")
    assert titles(only_unassigned) == ["医生资质 AI 审核规则"]


def test_latest_meeting_compares_instants_not_strings(tmp_path):
    """录音时间混着 -07:00 和 +00:00：-07:00 的 09-16 19:01 是 UTC 09-17 02:01，比 +00:00 的 09-17 01:00
    晚；按字符串比会排反。两场对照会只为比时区，不带原话。"""
    client, headers, db = make_world(tmp_path)
    for probe_id, title, recorded in (
        ("tz-pacific", "按太平洋时间记的会", "2026-09-16T19:01:50-07:00"),
        ("tz-utc", "按 UTC 记的会", "2026-09-17T01:00:00+00:00"),
    ):
        db.execute(
            """INSERT INTO meetings(id, title, recording_date, duration_ms, status, project_id,
                                    created_at, updated_at)
               VALUES (?, ?, ?, 60000, 'published', ?, ?, ?)""",
            (probe_id, title, recorded, project_id("pager"), utc_now(), utc_now()),
        )
    for title, probe_id in (("挂 UTC 那场", "tz-utc"), ("挂太平洋时间那场", "tz-pacific")):
        requirement_id = create(client, headers, "pager", title, "P1")
        client.post(
            f"/api/requirements/{requirement_id}/meetings/{probe_id}", json={}, headers=headers
        )

    payload = wall(client, status="active")

    assert titles(payload) == ["挂太平洋时间那场", "挂 UTC 那场"]
    assert payload["items"][0]["latest_meeting_date"] == "2026-09-16T19:01:50-07:00"
    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert projects[project_id("pager")]["latest_meeting_date"] == "2026-09-16T19:01:50-07:00"


# ---------------------------------------------------------------- 座次


def test_project_seats_reorder_unseat_and_stay_dense(tmp_path):
    client, headers, db = make_world(tmp_path)

    saved = seat(client, headers, "yimi", "hengrui", "huaxia")
    assert saved["seats"] == [
        {"project_id": project_id("yimi"), "seat": 1},
        {"project_id": project_id("hengrui"), "seat": 2},
        {"project_id": project_id("huaxia"), "seat": 3},
    ]
    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert projects[project_id("hengrui")]["seat"] == 2
    assert projects[project_id("cvm")]["seat"] is None
    assert projects[project_id("cvm")]["latest_meeting_date"] == "2026-09-29T19:26:37-07:00"

    # 拖动：恒瑞放到第 1 位，华夏移出座次
    seat(client, headers, "hengrui", "yimi")
    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert (projects[project_id("hengrui")]["seat"], projects[project_id("yimi")]["seat"]) == (1, 2)
    assert projects[project_id("huaxia")]["seat"] is None

    # 排在前面的项目没了，后面的名次补上，不留空位
    seat(client, headers, "pager", "hengrui", "yimi")
    assert (
        client.delete(
            f"/api/projects/{project_id('pager')}",
            headers={**headers, "Content-Type": "application/json"},
        ).status_code
        == 200
    )
    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert (projects[project_id("hengrui")]["seat"], projects[project_id("yimi")]["seat"]) == (1, 2)

    duplicated = client.put(
        "/api/project-seats",
        json={"project_ids": [project_id("yimi"), project_id("yimi")]},
        headers=headers,
    )
    assert duplicated.status_code == 400
    missing = client.put(
        "/api/project-seats", json={"project_ids": ["project-missing"]}, headers=headers
    )
    assert missing.status_code == 404
    seat(client, headers)
    assert all(project["seat"] is None for project in client.get("/api/projects").json())


# ---------------------------------------------------------------- 说明和来源（R04 / R05）


def test_create_with_summary_and_source_links_the_meeting(tmp_path):
    client, headers, db = make_world(tmp_path)
    # 画波形用归档里的录音（和录音档案列表同一个取法）
    db.execute(
        """INSERT INTO artifacts(meeting_id, kind, role, source_root, path, created_at)
           VALUES (?, 'audio', 'source', 'staging', '/staging/a.m4a', 'x')""",
        (meeting_id("cvm"),),
    )
    db.execute(
        """INSERT INTO artifacts(meeting_id, kind, role, source_root, path, created_at)
           VALUES (?, 'audio', 'source', 'archive', '/archive/a.m4a', 'x')""",
        (meeting_id("cvm"),),
    )
    archive_audio = db.query_one("SELECT id FROM artifacts WHERE source_root='archive'")["id"]

    response = client.post(
        "/api/requirements",
        json={
            "project_id": project_id("cvm"),
            "title": "科室会预约后台导出",
            "priority": "P2",
            "summary": "  预约审核页现在不能导出 Excel，运营要把科室会预约名单拉出来对表。  ",
            "source": source("export"),
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["summary"] == "预约审核页现在不能导出 Excel，运营要把科室会预约名单拉出来对表。"
    assert detail["status"] == "active"
    expected_source = {
        "kind": "origin",
        "meeting_id": meeting_id("cvm"),
        "meeting_title": "260929 云课堂直播运营问题对齐",
        "recording_date": "2026-09-29T19:26:37-07:00",
        "duration_ms": 747000,
        "audio_artifact_id": archive_audio,
        "quote": "那我有办法导出 excel 吗？是没有办法，",
        "anchor_ms": 576900,
        "via_candidate_title": None,
    }
    assert {key: detail["source"][key] for key in expected_source} == expected_source
    assert detail["sources"] == [detail["source"]]
    assert [meeting["id"] for meeting in detail["meetings"]] == [meeting_id("cvm")]
    assert (detail["follow_up_count"], detail["project_seat"]) == (0, None)


def test_create_rejects_bad_summary_and_source(tmp_path):
    client, headers, db = make_world(tmp_path)
    base = {"project_id": project_id("cvm"), "title": "科室会预约后台导出", "priority": "P2"}

    too_long = client.post(
        "/api/requirements", json={**base, "summary": "说" * 71}, headers=headers
    )
    assert too_long.status_code == 400 and "70" in too_long.json()["detail"]
    missing_meeting = client.post(
        "/api/requirements",
        json={**base, "source": {"meeting_id": "vm-missing", "quote": "x"}},
        headers=headers,
    )
    assert missing_meeting.status_code == 404
    long_quote = client.post(
        "/api/requirements",
        json={**base, "source": {"meeting_id": meeting_id("cvm"), "quote": "话" * 1001}},
        headers=headers,
    )
    assert long_quote.status_code == 400
    negative = client.post(
        "/api/requirements",
        json={**base, "source": {"meeting_id": meeting_id("cvm"), "anchor_ms": -1}},
        headers=headers,
    )
    assert negative.status_code == 422
    # 只选了会、没挑原话也能建
    meeting_only = client.post(
        "/api/requirements",
        json={**base, "source": {"meeting_id": meeting_id("cvm")}},
        headers=headers,
    ).json()
    assert (meeting_only["source"]["quote"], meeting_only["source"]["anchor_ms"]) == ("", None)
    assert db.query_one("SELECT COUNT(*) AS n FROM requirements") == {"n": 1}


def test_titles_drop_zero_width_characters(tmp_path):
    """需求名里的零宽字符（从飞书、微信复制常带）不算字：绕不过同项目不重名，只有零宽字符等于没写。"""
    client, headers, db = make_world(tmp_path)
    create(client, headers, "yimi", "赠药横跳拦截", "P0")

    twin = client.post(
        "/api/requirements",
        json={"project_id": project_id("yimi"), "title": "赠药横跳拦截\u200b", "priority": "P1"},
        headers=headers,
    )
    assert twin.status_code == 409
    blank = client.post(
        "/api/requirements",
        json={"project_id": project_id("yimi"), "title": "\u200b\u2060", "priority": "P1"},
        headers=headers,
    )
    assert blank.status_code == 400
    cleaned = client.post(
        "/api/requirements",
        json={
            "project_id": project_id("cvm"),
            "title": "\ufeff科室会预约后台导出\u200d",
            "priority": "P2",
        },
        headers=headers,
    )
    assert cleaned.status_code == 200 and cleaned.json()["title"] == "科室会预约后台导出"
    with db.transaction() as connection:
        candidate_id = insert_candidate(
            connection, meeting_id=meeting_id("cvm"), title="科室会预约\u200b后台导出 "
        )
    assert db.query_one(
        "SELECT title, name_key FROM requirement_candidates WHERE id=?", (candidate_id,)
    ) == {"title": "科室会预约后台导出", "name_key": "科室会预约后台导出"}


def test_update_replaces_or_clears_source_and_keeps_linked_meetings(tmp_path):
    client, headers, db = make_world(tmp_path)
    requirement_id = create(client, headers, "yimi", "京东科研仓对接", "P0", source_key="inbound")

    replaced = client.patch(
        f"/api/requirements/{requirement_id}",
        json={"source": source("export"), "summary": JD_SUMMARY},
        headers=headers,
    ).json()
    assert replaced["source"]["anchor_ms"] == 576900
    assert replaced["summary"] == JD_SUMMARY
    # 换来源只加关联会议、不删原来的：两场会，跟进 1 场
    assert {meeting["id"] for meeting in replaced["meetings"]} == {
        meeting_id("jd"),
        meeting_id("cvm"),
    }
    assert replaced["follow_up_count"] == 1

    untouched = client.patch(
        f"/api/requirements/{requirement_id}", json={"priority": "P1"}, headers=headers
    ).json()
    assert untouched["source"]["anchor_ms"] == 576900

    cleared = client.patch(
        f"/api/requirements/{requirement_id}", json={"source": None, "summary": ""}, headers=headers
    ).json()
    assert (cleared["source"], cleared["sources"], cleared["summary"]) == (None, [], "")
    assert len(cleared["meetings"]) == 2

    # 状态只能在进行中、已完成、已搁置之间切，不能改回待认领
    back_to_pending = client.patch(
        f"/api/requirements/{requirement_id}", json={"status": "pending"}, headers=headers
    )
    assert back_to_pending.status_code == 400


def test_candidates_never_reach_requirement_counts_or_old_list(tmp_path):
    """五-2：候选不算正式需求，项目的需求计数和老的需求列表（任务、会议页的下拉）都见不到候选。"""
    client, headers, db = make_world(tmp_path)
    build_wall(client, headers, db)

    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert projects[project_id("yimi")]["requirement_counts"] == {
        "active": 2,
        "done": 1,
        "shelved": 1,
        "all": 4,
    }
    assert projects[project_id("cvm")]["requirement_counts"]["all"] == 1
    old_list = client.get("/api/requirements", params={"limit": 200}).json()
    assert old_list["total"] == 11
    assert set(old_list["counts"]) == {"active", "done", "shelved", "all"}
    assert "京东仓签收凭证" not in {item["title"] for item in old_list["items"]}


def test_source_anchor_past_the_recording_end_is_pulled_back(tmp_path):
    """这场会录了 747000 毫秒。逐字稿最后几句的开始时间可能比录音时长晚一点（转写和录音各算各的）：
    选了这样的句子照样能存，时间锚截到录音末尾，不然用户存不进来、也没处可改（审查 B3）。"""
    client, headers, db = make_world(tmp_path)
    base = {"project_id": project_id("cvm"), "title": "科室会预约后台导出", "priority": "P2"}

    beyond = client.post(
        "/api/requirements",
        json={
            **base,
            "summary": None,
            "source": {"meeting_id": meeting_id("cvm"), "anchor_ms": 747001},
        },
        headers=headers,
    )
    assert beyond.status_code == 200, beyond.text
    assert (beyond.json()["source"]["anchor_ms"], beyond.json()["summary"]) == (747000, "")


def test_quote_needs_at_least_one_word(tmp_path):
    """纯标点、全空白的句子不能当原话（审查 B2）：存进去会在详情页显示成「时间签＋空引号」。"""
    client, headers, db = make_world(tmp_path)
    base = {"project_id": project_id("cvm"), "title": "科室会预约后台导出", "priority": "P2"}

    for quote in ("，。？", "……", " ！ "):
        response = client.post(
            "/api/requirements",
            json={**base, "source": {"meeting_id": meeting_id("cvm"), "quote": quote}},
            headers=headers,
        )
        assert response.status_code == 400, quote
        assert response.json()["detail"] == "原话里得有字，纯标点的句子不能当原话"
    excel = client.post(
        "/api/requirements",
        json={**base, "source": {"meeting_id": meeting_id("cvm"), "quote": "excel？"}},
        headers=headers,
    )
    assert excel.status_code == 200, excel.text


def test_unlinking_the_source_meeting_drops_its_quote(tmp_path):
    """来源一定在关联会议里：详情页移除、整组替换（会议页改关联同一条路）把提出它的那场会移出后，
    来源跟着去掉；跟进场数＝关联会议数减 1。"""
    client, headers, db = make_world(tmp_path)
    json_headers = {**headers, "Content-Type": "application/json"}
    requirement_id = create(
        client,
        headers,
        "yimi",
        "京东科研仓对接",
        "P0",
        source_key="inbound",
        meeting_keys=("hengtiao", "edc"),
    )
    detail = client.get(f"/api/requirements/{requirement_id}").json()
    assert (len(detail["meetings"]), detail["follow_up_count"]) == (3, 2)

    removed = client.delete(
        f"/api/requirements/{requirement_id}/meetings/{meeting_id('jd')}", headers=json_headers
    ).json()

    assert (removed["source"], removed["sources"], removed["follow_up_count"]) == (None, [], 1)
    poster = wall(client, status="active")["items"][0]
    assert (poster["source"], poster["meeting_count"], poster["follow_up_count"]) == (None, 2, 1)

    other = create(client, headers, "yimi", "京东仓签收凭证", "P1", source_key="receipt")
    replaced = client.put(
        f"/api/requirements/{other}/meetings",
        json={"meeting_ids": [meeting_id("edc")]},
        headers=headers,
    ).json()
    assert (replaced["source"], [meeting["id"] for meeting in replaced["meetings"]]) == (
        None,
        [meeting_id("edc")],
    )


def test_project_merge_hands_the_seat_to_an_unseated_target(tmp_path):
    client, headers, db = make_world(tmp_path)
    seat(client, headers, "yimi", "hengrui", "huaxia")

    # 恒瑞（第 2 位）并进还没排座次的寻呼随访项目：寻呼接过第 2 位
    merged = client.post(
        f"/api/projects/{project_id('hengrui')}/merge-into/{project_id('pager')}",
        json={},
        headers=headers,
    )
    assert merged.status_code == 200, merged.text
    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert projects[project_id("pager")]["seat"] == 2

    # 两个都排了座次：目标留自己的，名次照样连续
    client.post(
        f"/api/projects/{project_id('yimi')}/merge-into/{project_id('huaxia')}",
        json={},
        headers=headers,
    )
    projects = {project["id"]: project for project in client.get("/api/projects").json()}
    assert (projects[project_id("pager")]["seat"], projects[project_id("huaxia")]["seat"]) == (1, 2)


def test_copy_for_claude_code_carries_summary_and_quotes(tmp_path):
    """R02-9、R05-5：［接下］和［复制给 Claude Code］复制的背景里有说明、会上原话和时间锚，
    提出它的那句和合并进来的都列上，按会议时间、时间锚先后。"""
    client, headers, db = make_world(tmp_path)
    requirement_id = create(
        client, headers, "yimi", "京东科研仓对接", "P0", summary=JD_SUMMARY, source_key="inbound"
    )
    candidate_id = candidate(db, "receipt", "京东仓签收凭证")
    client.post(
        f"/api/requirement-candidates/{candidate_id}/merge",
        json={"requirement_id": requirement_id},
        headers=headers,
    )

    markdown = client.get(f"/api/requirements/{requirement_id}/context").json()["markdown"]

    head = markdown.split("\n## 会议")[0]
    assert head.endswith(
        "\n## 说明\n"
        f"{JD_SUMMARY}\n"
        "\n## 会上原话\n"
        f"- 00:13:45 「{QUOTES['inbound'][1]}」（260916 医米京东科研仓系统对接 · 提出）\n"
        f"- 00:31:49 「{QUOTES['receipt'][1]}」"
        "（260916 医米京东科研仓系统对接 · 合并自候选「京东仓签收凭证」）\n"
    )
