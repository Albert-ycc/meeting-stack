"""第二期 2d：会上提到文件名的比对、你的改动、会议简报里的文件。"""
import json
from datetime import datetime

from meeting_workbench import file_mentions, graph
from meeting_workbench.db import utc_now
from meeting_workbench.file_stems import derive_stem, stem_key

from .test_graph import TODAY, add_meeting
from .test_material_index import add_root, make


def ns(day):
    return int(datetime.fromisoformat(f"{day}T12:00:00").astimezone().timestamp() * 1_000_000_000)


def setup(tmp_path, project_id="p", folder="云图AI"):
    db, settings = make(tmp_path)
    root_id = add_root(db, tmp_path / folder, project_id=project_id)
    db.execute(
        "INSERT INTO material_index_state(root_id, state, stems_rev, stems_hash) VALUES (?, 'done', 1, 'h')",
        (root_id,),
    )
    return db, root_id


def add_file(db, root_id, rel, *, day="2026-09-01", zone="normal", gone=False):
    name = rel.rpartition("/")[2]
    stem = derive_stem(name)
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns,
               zone, seen_at, gone_at)
           VALUES (?, ?, ?, ?, ?, ?, '', 1, ?, ?, ?, ?)""",
        (root_id, rel, rel.rpartition("/")[0], name, stem, stem_key(stem), ns(day), zone, utc_now(),
         utc_now() if gone else None),
    )
    return db.query_one("SELECT id FROM material_files WHERE root_id=? AND rel_path=?", (root_id, rel))["id"]


def said(*texts):
    return [(index * 60_000, text) for index, text in enumerate(texts)]


def run(db):
    return file_mentions.match_pending(db, clock=lambda: 0.0)


def mentions(db, meeting_id="m"):
    return {
        row["stem_key"]: row
        for row in db.query_all("SELECT * FROM meeting_file_mentions WHERE meeting_id=?", (meeting_id,))
    }


def add_term(db, term, project_id=None):
    db.execute(
        """INSERT INTO glossary_terms(id, term, project_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (f"t-{term}", term, project_id, utc_now(), utc_now()),
    )


def test_counts_only_what_is_not_inside_longer_words(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "报价/报价单.xlsx")
    add_file(db, root_id, "看板系统说明.docx")
    add_meeting(
        db, "m", ago=1, project_id="p",
        segments=said("报价单位要先确认", "报价单明天发", "数据看板系统下周上线", "看板系统说明也要改"),
    )
    add_meeting(db, "m2", ago=1, project_id="p", segments=said("报价单位要先确认"))
    add_term(db, "数据看板系统", "p")

    run(db)
    # 没有分词器：词典里没有「报价单位」时它也算「报价单」
    assert mentions(db)["报价单"]["count"] == 2
    assert mentions(db)["看板系统说明"]["count"] == 1
    assert mentions(db, "m2")["报价单"]["count"] == 1

    add_term(db, "报价单位")
    run(db)
    assert mentions(db)["报价单"]["count"] == 1
    assert "报价单" not in mentions(db, "m2")


def test_two_char_stems_need_two_mentions_and_letters_need_boundaries(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "排班.xlsx")
    add_file(db, root_id, "Roadmap.pptx")
    add_file(db, root_id, "云图AI项目周报.pptx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("排班先这样", "roadmaps 都看了"))
    add_meeting(db, "m2", ago=1, project_id="p", segments=said("排班先这样，排班下周再说", "看一下 Roadmap 吧"))

    run(db)

    assert mentions(db) == {}
    assert {key: row["count"] for key, row in mentions(db, "m2").items()} == {"排班": 2, "roadmap": 1}


def test_versions_prefer_the_full_name_then_the_meeting_date(tmp_path):
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价/报价单 v1.xlsx", day="2026-09-10")
    v2 = add_file(db, root_id, "报价/报价单 v2.xlsx", day="2026-09-18")
    v3 = add_file(db, root_id, "报价/报价单 v3.xlsx", day="2026-09-25")
    add_meeting(db, "said-v1", ago=1, project_id="p", segments=said("报价单 V1 那版不行", "报价单再改改"))
    add_meeting(db, "sep-20", ago=6, project_id="p", segments=said("报价单再改改"))
    add_meeting(db, "sep-05", ago=21, project_id="p", segments=said("报价单再改改"))
    add_meeting(db, "sep-26", ago=0, project_id="p", segments=said("报价单再改改"))

    run(db)

    assert mentions(db, "said-v1")["报价单"]["file_id"] == v1
    assert mentions(db, "said-v1")["报价单"]["count"] == 2
    assert mentions(db, "sep-20")["报价单"]["file_id"] == v2
    assert mentions(db, "sep-05")["报价单"]["file_id"] == v1  # 都晚于这场会：最早的一份
    assert mentions(db, "sep-26")["报价单"]["file_id"] == v3


def test_minutes_only_mentions_need_three_characters(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "需求说明书.docx")
    add_file(db, root_id, "排班.xlsx")
    add_meeting(
        db, "m", ago=1, project_id="p", segments=said("今天先到这"),
        minutes="## 待办\n- [00:12:34] 需求说明书要补一版，排班也看看，排班表\n",
    )

    run(db)

    row = mentions(db)["需求说明书"]
    assert (row["source"], row["count"], row["minutes_count"], row["first_ms"]) == ("minutes", 0, 1, 754_000)
    assert "排班" not in mentions(db)


def test_edits_moves_and_rejections(tmp_path):
    db, root_id = setup(tmp_path)
    old = add_file(db, root_id, "报价/报价单.xlsx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发"))
    run(db)
    assert mentions(db)["报价单"]["file_id"] == old
    assert run(db)["pending"] == 0

    # 改稿后重新比对
    version = db.query_one("SELECT current_transcript_version_id AS v FROM meetings WHERE id='m'")["v"]
    db.execute("UPDATE segments SET text='报价单明天发，报价单要盖章' WHERE version_id=?", (version,))
    db.add_event("transcript_draft_saved", meeting_id="m")
    run(db)
    assert mentions(db)["报价单"]["count"] == 2

    # 文件挪了位置：旧的不见了、新的入库，提到换到新文件
    db.execute("UPDATE material_files SET gone_at=? WHERE id=?", (utc_now(), old))
    db.execute("UPDATE meeting_file_scan SET dirty = dirty + 1 WHERE meeting_id='m'")
    new = add_file(db, root_id, "归档/报价单.xlsx")
    run(db)
    assert mentions(db)["报价单"]["file_id"] == new

    # 不是这份文件：以后比对也不改回来
    file_mentions.reject_mention(db, "m", "报价单")
    db.add_event("transcript_draft_saved", meeting_id="m")
    run(db)
    assert mentions(db)["报价单"]["status"] == "rejected"
    # 撤销：改回有效，重新比对
    file_mentions.restore_mention(db, "m", "报价单")
    run(db)
    assert mentions(db)["报价单"]["status"] == "active"

    # 换成这份：以后不再自动改
    other = add_file(db, root_id, "报价单 v2.xlsx", day="2026-08-01")
    file_mentions.pick_mention_file(db, "m", "报价单", other)
    db.add_event("transcript_draft_saved", meeting_id="m")
    run(db)
    assert mentions(db)["报价单"]["file_id"] == other and mentions(db)["报价单"]["picked"] == 1


def test_rejections_only_block_the_original_project(tmp_path):
    db, root_id = setup(tmp_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),))
    other_root = add_root(db, tmp_path / "数据中台", project_id="q")
    db.execute(
        "INSERT INTO material_index_state(root_id, state, stems_rev, stems_hash) VALUES (?, 'done', 1, 'h')",
        (other_root,),
    )
    add_file(db, root_id, "报价单.xlsx")
    add_file(db, root_id, "权限中心说明.docx")
    q_file = add_file(db, other_root, "报价单.xlsx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发", "权限中心说明看了"))
    run(db)
    file_mentions.reject_mention(db, "m", "报价单")

    db.execute("UPDATE meetings SET project_id='q' WHERE id='m'")
    # 旧项目的有效提到立刻去掉，「不是这份文件」留着
    assert {(row["project_id"], key, row["status"]) for key, row in mentions(db).items()} == {
        ("p", "报价单", "rejected")
    }
    run(db)
    rows = db.query_all(
        "SELECT project_id, stem_key, status, file_id FROM meeting_file_mentions WHERE meeting_id='m' ORDER BY project_id"
    )
    assert rows == [
        {"project_id": "p", "stem_key": "报价单", "status": "rejected", "file_id": rows[0]["file_id"]},
        {"project_id": "q", "stem_key": "报价单", "status": "active", "file_id": q_file},
    ]


def test_changed_while_matching_is_left_for_the_next_round(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "报价单.xlsx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发"))
    with db.autocommit() as connection:
        row = file_mentions._pending(connection)[0]
    db.add_event("transcript_draft_saved", meeting_id="m")  # 比对期间改了稿（先有了一行）
    assert file_mentions.match_meeting(db, row, {}) is False
    assert mentions(db) == {}
    assert run(db)["written"] == 1


def test_brief_lists_files_and_states(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "报价单.xlsx")
    add_file(db, root_id, "需求说明书.docx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发，报价单要盖章", "需求说明书"))
    add_meeting(db, "none", ago=1, segments=said("报价单"))

    def brief(meeting_id):
        with db.autocommit() as connection:
            return graph.meeting_brief(connection, meeting_id, attribution=None, card=None)

    assert brief("m")["files_state"] == "indexing"
    run(db)
    body = brief("m")
    assert body["files_state"] == "done"
    assert [(item["name"], item["count"]) for item in body["files"]] == [("报价单.xlsx", 2), ("需求说明书.docx", 1)]
    assert "files_note" not in body
    assert len(json.dumps(body, ensure_ascii=False).encode()) < 20_000
    assert brief("none")["files_state"] == "no_project"
    db.execute("UPDATE material_index_state SET state='offline'")
    assert brief("m")["files_state"] == "offline"
    db.execute("DELETE FROM project_material_roots")
    assert brief("m")["files_state"] == "no_root"


def test_file_detail_lists_meetings_and_same_named_files(tmp_path):
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价/报价单 v1.xlsx", day="2026-09-10")
    v2 = add_file(db, root_id, "报价/报价单 v2.xlsx", day="2026-09-18")
    add_meeting(db, "m", ago=6, project_id="p", segments=said("先说报价单", "报价单再改改"))
    run(db)

    with db.autocommit() as connection:
        body = file_mentions.file_detail(
            connection, v2, quotes=lambda meeting_id, starts: graph._segments_at(connection, meeting_id, starts)
        )

    assert body["file"]["name"] == "报价单 v2.xlsx"
    assert body["file"]["folder_path"].endswith("云图AI/报价")
    assert [item["id"] for item in body["siblings"]] == [v1]
    assert [(item["meeting_id"], item["count"], item["status"]) for item in body["meetings"]] == [("m", 2, "active")]
    assert body["meetings"][0]["quote"] == "先说报价单"


# ---------------------------------------------------------------------- 项目图


def test_project_graph_draws_mentioned_files_with_caps(tmp_path):
    db, root_id = setup(tmp_path)
    names = [f"模块{chr(0x4e00 + index)}说明" for index in range(16)]
    for name in names:
        add_file(db, root_id, f"说明/{name}.docx")
    add_file(db, root_id, "需求池总表.xlsx")
    # 6 场会，每场提到 4 个不同的文件（次数不同）；「需求池总表」每场都提到：通用，不占名额
    for meeting in range(6):
        texts = ["需求池总表先过一遍"]
        for offset in range(4):
            texts += [names[(meeting * 3 + offset) % 16]] * (4 - offset)
        add_meeting(db, f"m-{meeting}", ago=meeting + 1, project_id="p", origin="manual", segments=said(*texts))
    run(db)

    with db.autocommit() as connection:
        body = graph.project_graph(connection, "p", today=TODAY)

    edges = [edge for edge in body["edges"] if edge["kind"] == "mentioned"]
    per_meeting = {}
    for edge in edges:
        per_meeting.setdefault(edge["meeting_id"], []).append(edge)
    assert per_meeting and all(len(items) <= 3 for items in per_meeting.values())
    assert len(body["files"]) <= 12
    assert all(item["name"] != "需求池总表.xlsx" for item in body["files"])
    shown = {item["id"] for item in body["files"]}
    assert all(edge["to"] in shown for edge in edges)
    edge = next(edge for edge in edges if edge["meeting_id"] == "m-0" and edge["count"] == 4)
    assert edge["label"] == f"会上说『{names[0]}』4 次 · 00:01:00"
    # 每场前 3 个里有几场共用的文件：节点比连线少
    assert len(shown) < len(edges)
    assert graph.GRAPH_API_VERSION == 3

    # 简报：全部有效提到，通用的排最后
    with db.autocommit() as connection:
        brief = graph.meeting_brief(connection, "m-0", attribution=None, card=None)
    assert brief["files"][-1]["name"] == "需求池总表.xlsx" and brief["files"][-1]["generic"] is True
    assert len(brief["files"]) == 5


def test_more_files_fold_into_one_node(tmp_path):
    db, root_id = setup(tmp_path)
    names = [f"模块{chr(0x4e00 + index)}说明" for index in range(20)]
    for name in names:
        add_file(db, root_id, f"{name}.docx")
    for meeting in range(6):
        texts = [names[meeting * 3 + offset] for offset in range(3)]
        add_meeting(db, f"m-{meeting}", ago=meeting + 1, project_id="p", origin="manual", segments=said(*texts))
    run(db)

    with db.autocommit() as connection:
        body = graph.project_graph(connection, "p", today=TODAY)

    assert len(body["files"]) == 12
    assert body["files_more"]["count"] == 6


# ---------------------------------------------------------------------- 接口


def test_file_endpoints(tmp_path):
    from meeting_workbench.db import Database

    from .test_tasks_api import make_client, write_headers

    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    root_id = add_root(db, tmp_path / "云图AI")
    db.execute(
        "INSERT INTO material_index_state(root_id, state, stems_rev, stems_hash, files) VALUES (?, 'done', 1, 'h', 2)",
        (root_id,),
    )
    v1 = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    v2 = add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-18")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单再改改"))
    run(db)

    status = client.get("/api/materials/index-status?project_id=p").json()["roots"]
    assert [(item["root_id"], item["state"], item["files"]) for item in status] == [(root_id, "done", 2)]
    detail = client.get(f"/api/graph/files/{v2}").json()
    assert detail["meetings"][0]["meeting_id"] == "m"
    assert client.get("/api/graph/files/9999").status_code == 404

    base = "/api/meetings/m/file-mentions/报价单"
    assert client.post(f"{base}/pick", json={"file_id": v1}, headers=headers).json()["file_id"] == v1
    assert client.post(f"{base}/pick", json={"file_id": 9999}, headers=headers).status_code == 400
    assert client.post(f"{base}/reject", json={}, headers=headers).json()["status"] == "rejected"
    brief = client.get("/api/meetings/m/brief").json()
    assert brief["files"] == []
    assert client.post(f"{base}/restore", json={}, headers=headers).json()["status"] == "active"
    assert client.post("/api/meetings/m/file-mentions/没有/reject", json={}, headers=headers).status_code == 404
