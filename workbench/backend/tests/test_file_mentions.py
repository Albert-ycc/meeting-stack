"""第二期 2d：会上提到文件名的比对、你的改动、会议简报里的文件。"""

import json
import os
import time
from datetime import datetime

import pytest

from meeting_workbench import file_mentions, graph
from meeting_workbench.db import utc_now
from meeting_workbench.file_stems import derive_stem, stem_key
from meeting_workbench.material_index import MaterialIndexer

from .test_graph import TODAY, add_meeting
from .test_material_index import add_root, make, run_until_done, write


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
        (
            root_id,
            rel,
            rel.rpartition("/")[0],
            name,
            stem,
            stem_key(stem),
            ns(day),
            zone,
            utc_now(),
            utc_now() if gone else None,
        ),
    )
    return db.query_one(
        "SELECT id FROM material_files WHERE root_id=? AND rel_path=?", (root_id, rel)
    )["id"]


def said(*texts):
    return [(index * 60_000, text) for index, text in enumerate(texts)]


def run(db):
    return file_mentions.match_pending(db, clock=lambda: 0.0)


def mentions(db, meeting_id="m"):
    return {
        row["stem_key"]: row
        for row in db.query_all(
            "SELECT * FROM meeting_file_mentions WHERE meeting_id=?", (meeting_id,)
        )
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
        db,
        "m",
        ago=1,
        project_id="p",
        segments=said(
            "报价单位要先确认", "报价单明天发", "数据看板系统下周上线", "看板系统说明也要改"
        ),
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
    add_meeting(
        db,
        "m2",
        ago=1,
        project_id="p",
        segments=said("排班先这样，排班下周再说", "看一下 Roadmap 吧"),
    )

    run(db)

    assert mentions(db) == {}
    assert {key: row["count"] for key, row in mentions(db, "m2").items()} == {
        "排班": 2,
        "roadmap": 1,
    }


def test_versions_prefer_the_full_name_then_the_meeting_date(tmp_path):
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价/报价单 v1.xlsx", day="2026-09-10")
    v2 = add_file(db, root_id, "报价/报价单 v2.xlsx", day="2026-09-18")
    v3 = add_file(db, root_id, "报价/报价单 v3.xlsx", day="2026-09-25")
    add_meeting(
        db, "said-v1", ago=1, project_id="p", segments=said("报价单 V1 那版不行", "报价单再改改")
    )
    add_meeting(db, "sep-20", ago=6, project_id="p", segments=said("报价单再改改"))
    add_meeting(db, "sep-05", ago=21, project_id="p", segments=said("报价单再改改"))
    add_meeting(db, "sep-26", ago=0, project_id="p", segments=said("报价单再改改"))

    run(db)

    assert mentions(db, "said-v1")["报价单"]["file_id"] == v1
    assert mentions(db, "said-v1")["报价单"]["count"] == 2
    assert mentions(db, "sep-20")["报价单"]["file_id"] == v2
    assert mentions(db, "sep-05")["报价单"]["file_id"] == v1  # 都晚于这场会：最早的一份
    assert mentions(db, "sep-26")["报价单"]["file_id"] == v3


def test_a_longer_version_number_is_not_that_version(tmp_path):
    db, root_id = setup(tmp_path)
    v3 = add_file(db, root_id, "报价单 v3.xlsx", day="2026-09-10")
    v1 = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-20")
    add_meeting(db, "v30", ago=1, project_id="p", segments=said("报价单 v30 发了"))
    add_meeting(db, "v3-1", ago=1, project_id="p", segments=said("报价单 V3.1 再看看"))
    add_meeting(db, "v3", ago=1, project_id="p", segments=said("报价单 v3，下周定"))

    run(db)

    assert mentions(db, "v30")["报价单"]["file_id"] == v1  # 按会议日期挑，不是 v3
    assert mentions(db, "v3-1")["报价单"]["file_id"] == v1
    assert mentions(db, "v3")["报价单"]["file_id"] == v3


def test_minutes_only_mentions_need_three_characters(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "需求说明书.docx")
    add_file(db, root_id, "排班.xlsx")
    add_meeting(
        db,
        "m",
        ago=1,
        project_id="p",
        segments=said("今天先到这"),
        minutes="## 待办\n- [00:12:34] 需求说明书要补一版，排班也看看，排班表\n",
    )

    run(db)

    row = mentions(db)["需求说明书"]
    assert (row["source"], row["count"], row["minutes_count"], row["first_ms"]) == (
        "minutes",
        0,
        1,
        754_000,
    )
    assert "排班" not in mentions(db)


def test_edits_moves_and_rejections(tmp_path):
    db, root_id = setup(tmp_path)
    old = add_file(db, root_id, "报价/报价单.xlsx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发"))
    run(db)
    assert mentions(db)["报价单"]["file_id"] == old
    assert run(db)["pending"] == 0

    # 改稿后重新比对
    version = db.query_one("SELECT current_transcript_version_id AS v FROM meetings WHERE id='m'")[
        "v"
    ]
    db.execute(
        "UPDATE segments SET text='报价单明天发，报价单要盖章' WHERE version_id=?", (version,)
    )
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
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),)
    )
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
        {
            "project_id": "p",
            "stem_key": "报价单",
            "status": "rejected",
            "file_id": rows[0]["file_id"],
        },
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


def test_your_pick_during_a_match_is_kept(tmp_path, monkeypatch):
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价单 v1.xlsx", day="2026-08-01")
    v2 = add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-01")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发"))
    run(db)
    assert mentions(db)["报价单"]["file_id"] == v2
    version = db.query_one("SELECT current_transcript_version_id AS id FROM meetings WHERE id='m'")[
        "id"
    ]
    db.execute(
        "UPDATE segments SET text='报价单明天发，报价单要盖章' WHERE version_id=?", (version,)
    )
    db.add_event("transcript_draft_saved", meeting_id="m")
    compute = file_mentions.compute_mentions

    def compute_then_pick(*args, **kwargs):
        result = compute(*args, **kwargs)
        file_mentions.pick_mention_file(db, "m", "报价单", v1)  # 比对算到一半，你点了［换成这份］
        return result

    monkeypatch.setattr(file_mentions, "compute_mentions", compute_then_pick)
    run(db)
    monkeypatch.setattr(file_mentions, "compute_mentions", compute)
    run(db)

    row = mentions(db)["报价单"]
    assert (row["file_id"], row["picked"]) == (v1, 1)


def test_a_file_moved_to_a_later_folder_keeps_its_mention(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "a" / "报价单.xlsx")
    (root / "z").mkdir()
    add_root(db, root)
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单明天发"))
    run(db)
    assert "报价单" in mentions(db)

    (root / "a" / "报价单.xlsx").rename(root / "z" / "报价单.xlsx")
    MaterialIndexer(
        db, settings, clock=lambda: 0.0, round_entries=2
    ).run_round()  # 读完 a/ 就停，z/ 还没读
    run(db)  # 这时比对：旧位置没了、新位置还没收进来
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    run(db)

    moved = db.query_one("SELECT id FROM material_files WHERE rel_path='z/报价单.xlsx'")["id"]
    assert mentions(db)["报价单"]["file_id"] == moved


def test_brief_lists_files_and_states(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "报价单.xlsx")
    add_file(db, root_id, "需求说明书.docx")
    add_meeting(
        db, "m", ago=1, project_id="p", segments=said("报价单明天发，报价单要盖章", "需求说明书")
    )
    add_meeting(db, "none", ago=1, segments=said("报价单"))

    def brief(meeting_id):
        with db.autocommit() as connection:
            return graph.meeting_brief(connection, meeting_id, attribution=None, card=None)

    assert brief("m")["files_state"] == "indexing"
    run(db)
    body = brief("m")
    assert body["files_state"] == "done"
    assert [(item["name"], item["count"]) for item in body["files"]] == [
        ("报价单.xlsx", 2),
        ("需求说明书.docx", 1),
    ]
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
            connection,
            v2,
            quotes=lambda meeting_id, starts: graph._segments_at(connection, meeting_id, starts),
        )

    assert body["file"]["name"] == "报价单 v2.xlsx"
    assert body["file"]["folder_path"].endswith("云图AI/报价")
    assert [item["id"] for item in body["siblings"]] == [v1]
    assert [(item["meeting_id"], item["count"], item["status"]) for item in body["meetings"]] == [
        ("m", 2, "active")
    ]
    assert body["meetings"][0]["quote"] == "先说报价单"


# ---------------------------------------------------------------------- 项目图


def test_project_graph_draws_mentioned_files_with_caps(tmp_path):
    db, root_id = setup(tmp_path)
    names = [f"模块{chr(0x4E00 + index)}说明" for index in range(16)]
    for name in names:
        add_file(db, root_id, f"说明/{name}.docx")
    add_file(db, root_id, "需求池总表.xlsx")
    # 6 场会，每场提到 4 个不同的文件（次数不同）；「需求池总表」每场都提到：通用，不占名额
    for meeting in range(6):
        texts = ["需求池总表先过一遍"]
        for offset in range(4):
            texts += [names[(meeting * 3 + offset) % 16]] * (4 - offset)
        add_meeting(
            db,
            f"m-{meeting}",
            ago=meeting + 1,
            project_id="p",
            origin="manual",
            segments=said(*texts),
        )
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
    assert graph.GRAPH_API_VERSION == 4

    # 简报：全部有效提到，通用的排最后
    with db.autocommit() as connection:
        brief = graph.meeting_brief(connection, "m-0", attribution=None, card=None)
    assert brief["files"][-1]["name"] == "需求池总表.xlsx" and brief["files"][-1]["generic"] is True
    assert len(brief["files"]) == 5


def test_more_files_fold_into_one_node(tmp_path):
    db, root_id = setup(tmp_path)
    names = [f"模块{chr(0x4E00 + index)}说明" for index in range(20)]
    for name in names:
        add_file(db, root_id, f"{name}.docx")
    for meeting in range(6):
        texts = [names[meeting * 3 + offset] for offset in range(3)]
        add_meeting(
            db,
            f"m-{meeting}",
            ago=meeting + 1,
            project_id="p",
            origin="manual",
            segments=said(*texts),
        )
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
    assert [(item["root_id"], item["state"], item["files"]) for item in status] == [
        (root_id, "done", 2)
    ]
    detail = client.get(f"/api/graph/files/{v2}").json()
    assert detail["meetings"][0]["meeting_id"] == "m"
    assert client.get("/api/graph/files/9999").status_code == 404

    base = "/api/meetings/m/file-mentions/报价单"
    assert (
        client.post(f"{base}/pick", json={"file_id": v1}, headers=headers).json()["file_id"] == v1
    )
    assert client.post(f"{base}/pick", json={"file_id": 9999}, headers=headers).status_code == 400
    assert client.post(f"{base}/reject", json={}, headers=headers).json()["status"] == "rejected"
    brief = client.get("/api/meetings/m/brief").json()
    assert brief["files"] == []
    assert client.post(f"{base}/restore", json={}, headers=headers).json()["status"] == "active"
    assert (
        client.post(
            "/api/meetings/m/file-mentions/没有/reject", json={}, headers=headers
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------- 先数再取（4a）


def mentioned_by(db, file_id, meetings):
    for index in range(meetings):
        meeting_id = f"m-{index:02d}"
        add_meeting(db, meeting_id, ago=index + 1, project_id="p")
        db.execute(
            """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count,
                   first_ms, anchors_json, minutes_count, source, status, picked, updated_at)
               VALUES (?, 'p', '报价单', ?, '报价单', 1, 0, '[0]', 0, 'transcript', 'active', 0, ?)""",
            (meeting_id, file_id, utc_now()),
        )


def test_file_panel_counts_more_than_forty_meetings(tmp_path):
    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    mentioned_by(db, file_id, 43)

    with db.autocommit() as connection:
        body = file_mentions.file_detail(connection, file_id, quotes=lambda meeting_id, starts: {})

    assert body["active_meetings"] == 43
    assert len(body["meetings"]) == 40 and body["meetings"][0]["meeting_id"] == "m-00"


def test_preview_counts_more_than_forty_meetings(tmp_path):
    from meeting_workbench import material_status

    db, root_id = setup(tmp_path)
    file_id = add_file(db, root_id, "报价单.xlsx")
    mentioned_by(db, file_id, 41)

    with db.autocommit() as connection:
        body = material_status.file_preview(connection, file_id, state_of=lambda _path: "offline")

    assert body["mentioned_meetings"] == 41
    assert len(body["mentions"]) == 40


# ---------------------------------------------------------------------- 4b 本机一层


def add_aliases(db, term, *, aliases=(), also=(), project_id="p", confirmed=1):
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, also, confirmed, project_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"t-{term}",
            term,
            json.dumps(list(aliases), ensure_ascii=False),
            json.dumps(list(also), ensure_ascii=False),
            confirmed,
            project_id,
            utc_now(),
            utc_now(),
        ),
    )


def test_aliases_and_also_are_needles_with_their_own_label(tmp_path):
    db, root_id = setup(tmp_path)
    quote = add_file(db, root_id, "报价单.xlsx")
    board = add_file(db, root_id, "能耗看板方案.pptx")
    add_aliases(db, "报价单", aliases=["报价表"])
    add_aliases(db, "能耗看板方案", also=["看板PPT"])
    add_meeting(
        db,
        "m",
        ago=1,
        project_id="p",
        segments=said("报价表发我一下", "报价表还要改", "看板PPT 明天讲"),
    )
    add_meeting(db, "both", ago=1, project_id="p", segments=said("报价表发我一下", "报价单还要改"))

    run(db)

    rows = mentions(db)
    assert (rows["报价单"]["file_id"], rows["报价单"]["needle"], rows["报价单"]["count"]) == (
        quote,
        "报价表",
        2,
    )
    assert (rows["能耗看板方案"]["file_id"], rows["能耗看板方案"]["needle"]) == (board, "看板PPT")
    assert (
        graph.mention_label({**rows["报价单"], "relation_id": None})
        == "会上说『报价表』2 次 · 00:00:00"
    )
    # 有别名也有本名：仍写词干
    assert mentions(db, "both")["报价单"]["needle"] == "报价单"
    assert mentions(db, "both")["报价单"]["count"] == 2


def test_two_char_alias_needs_two_mentions_and_unconfirmed_terms_do_not_count(tmp_path):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "设备采购清单.xlsx")
    add_file(db, root_id, "能耗看板方案.pptx")
    add_aliases(db, "设备采购清单", also=["采表"])
    add_aliases(db, "能耗看板方案", aliases=["看板方案"], confirmed=0)
    add_meeting(db, "once", ago=1, project_id="p", segments=said("采表发一下", "看板方案讲一下"))
    add_meeting(db, "twice", ago=1, project_id="p", segments=said("采表发一下", "采表还要改"))

    run(db)

    assert mentions(db, "once") == {}
    assert mentions(db, "twice")["设备采购清单"]["count"] == 2


def test_spoken_version_and_final_pick_the_file(tmp_path):
    db, root_id = setup(tmp_path)
    v2 = add_file(db, root_id, "报价/报价单 v2.xlsx", day="2026-09-10")
    v3 = add_file(db, root_id, "报价/报价单 v3.xlsx", day="2026-09-11")
    add_file(db, root_id, "报价/报价单 v4.xlsx", day="2026-09-20")
    draft = add_file(db, root_id, "方案/实施方案.docx", day="2026-09-20")
    final = add_file(db, root_id, "方案/实施方案 终版.docx", day="2026-09-10")
    add_meeting(db, "third", ago=1, project_id="p", segments=said("第三版报价单先发出去"))
    add_meeting(db, "v3", ago=1, project_id="p", segments=said("报价单 V3版 再看"))
    add_meeting(db, "ninth", ago=1, project_id="p", segments=said("第九版报价单"))
    add_meeting(db, "final", ago=1, project_id="p", segments=said("实施方案终版发群里"))
    add_meeting(db, "plain", ago=1, project_id="p", segments=said("实施方案发群里"))

    run(db)

    assert mentions(db, "third")["报价单"]["file_id"] == v3
    assert mentions(db, "v3")["报价单"]["file_id"] == v3
    # 组里没有第九版：按会议日期挑
    assert mentions(db, "ninth")["报价单"]["file_id"] == v3 + 1
    assert mentions(db, "final")["实施方案"]["file_id"] == final
    assert mentions(db, "plain")["实施方案"]["file_id"] == draft
    assert v2 < v3


def test_this_or_last_version_is_not_a_version_number(tmp_path):
    spoken = file_mentions.spoken_version
    for text in (
        "上一版报价单",
        "这一版",
        "下一版",
        "这两版",
        "那一版",
        "新一版",
        "前一版",
        "两版都看过",
        "改了三版",
    ):
        assert spoken(text) is None, text
    assert (
        spoken("第一版"),
        spoken("三版报价单"),
        spoken("版本二"),
        spoken("报价单V3版"),
        spoken("3.0版"),
    ) == (1, 3, 2, 3, 3)
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    v2 = add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-20")
    add_meeting(db, "prev", ago=1, project_id="p", segments=said("上一版报价单先发出去"))
    add_meeting(db, "this", ago=1, project_id="p", segments=said("这一版报价单再看看"))
    add_meeting(db, "both", ago=1, project_id="p", segments=said("报价单这两版都看过了"))
    run(db)
    # 不当成第 1 版、第 2 版：按会议日期挑开会前最新的
    assert {
        meeting: mentions(db, meeting)["报价单"]["file_id"] for meeting in ("prev", "this", "both")
    } == {"prev": v2, "this": v2, "both": v2}
    # L5 留下「上一版」的提示：按提示挑第二新的一份
    now = utc_now()
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, hints_json, created_at, updated_at)
           VALUES ('prev', 'v', 's', 'done', '{"报价单": {"rel": "previous"}}', ?, ?)""",
        (now, now),
    )
    db.execute("UPDATE meeting_file_scan SET dirty = dirty + 1 WHERE meeting_id = 'prev'")
    run(db)
    assert mentions(db, "prev")["报价单"]["file_id"] == v1


def test_hints_come_before_the_date_pick(tmp_path):
    db, root_id = setup(tmp_path)
    v1 = add_file(db, root_id, "报价单 v1.xlsx", day="2026-09-10")
    add_file(db, root_id, "报价单 v2.xlsx", day="2026-09-24")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单再看一下"))
    run(db)
    assert mentions(db)["报价单"]["file_id"] == v1 + 1
    now = utc_now()
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, hints_json, created_at, updated_at)
           VALUES ('m', 'v', 's', 'done', '{"报价单": {"version": 1}}', ?, ?)""",
        (now, now),
    )
    db.execute("UPDATE meeting_file_scan SET dirty = dirty + 1 WHERE meeting_id = 'm'")
    run(db)
    assert mentions(db)["报价单"]["file_id"] == v1
    # 提示换成「最新的」：挪回开会前最新的那份
    db.execute('UPDATE mention_extractions SET hints_json = \'{"报价单": {"rel": "latest"}}\'')
    db.execute("UPDATE meeting_file_scan SET dirty = dirty + 1 WHERE meeting_id = 'm'")
    run(db)
    assert mentions(db)["报价单"]["file_id"] == v1 + 1


def test_match_version_change_recompares_every_meeting(tmp_path, monkeypatch):
    db, root_id = setup(tmp_path)
    add_file(db, root_id, "报价单.xlsx")
    add_meeting(db, "m", ago=1, project_id="p", segments=said("报价单再看一下"))
    add_meeting(db, "m2", ago=2, project_id="p", segments=said("报价单再看一下"))
    assert run(db)["tried"] == 2
    assert run(db)["pending"] == 0
    assert file_mentions.MATCH_VERSION == "4b-1"
    monkeypatch.setattr(file_mentions, "MATCH_VERSION", "4b-2")
    assert run(db) == {"pending": 2, "tried": 2, "written": 2}


@pytest.mark.parametrize("zone", ["Asia/Shanghai", "America/Los_Angeles"])
def test_day_hints_follow_the_beijing_calendar_whatever_the_machine_zone(zone):
    """会在北京白天开，「昨天」「这周」是会上的日子：Mac 在太平洋时区时也得挑北京日历上的那一版。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:

        def at(text):
            return int(datetime.fromisoformat(text).timestamp() * 1_000_000_000)

        group = [
            {"id": 1, "name": "报价单.xlsx", "mtime_ns": at("2026-10-01T16:00:00+08:00")},
            {"id": 2, "name": "报价单.xlsx", "mtime_ns": at("2026-09-30T20:00:00+08:00")},
            {"id": 3, "name": "报价单.xlsx", "mtime_ns": at("2026-09-27T23:00:00+08:00")},
        ]
        meeting = file_mentions._meeting_ns("2026-10-02T10:00:00+08:00", None)
        picked = {
            rel: file_mentions.pick_by_hint(group, {"rel": rel}, meeting)["id"]
            for rel in ("yesterday", "this_week", "last_week")
        }
        assert picked == {"yesterday": 1, "this_week": 1, "last_week": 3}
        # 只有日期时取北京那天的最后一秒
        assert file_mentions._meeting_ns("2026-10-02", None) == at("2026-10-02T23:59:59+08:00")
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
