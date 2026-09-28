"""第四期 4e：问题块的读法（relation_read.file_questions、task_questions、decision_questions）和接口：
两种 RelationQuestion 的形状、白名单、stat_guard、parts=preview、任务的 suggestions、需求卡的 stale_files
（6 条语句）、展开一场会的 stale 和 asks。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from meeting_workbench import affects, decisions, materials, material_status, produced
from meeting_workbench.db import Database, utc_now

from .helpers import count_reads
from .test_file_events import swept
from .test_tasks_api import make_client, write_headers

HIDDEN = {"score", "prev_json", "root_id", "evidence_json"}


def walk_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in walk_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in walk_keys(item)}
    return set()


def world(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    now = datetime.now(UTC)
    root = tmp_path / "云图资料"
    (root / "报价").mkdir(parents=True)
    (root / "能耗看板").mkdir()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, 'x')",
        (str(root),),
    )
    root_id = int(db.query_one("SELECT id FROM project_material_roots")["id"])
    swept(db, root_id)
    # 一场三天前的会：定了「总价下调 5%」，派了一条任务，挂在需求「能耗看板」上
    day = (now - timedelta(days=3)).astimezone().date().isoformat()
    db.execute(
        """INSERT INTO meetings(id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES ('m', '报价沟通', ?, 'completed_unreviewed', 'p', ?, ?)""",
        (day, utc_now(), utc_now()),
    )
    db.execute(
        """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv', 'm', 1, '# 报价沟通\n\n## 决议\n\n- 总价下调 5% [00:12:34]\n', '', 'generated', 0, ?)""",
        (utc_now(),),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id = 'mv' WHERE id = 'm'")
    decisions.ingest_pending(db, now=now, max_seconds=60)
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES ('r', 'p', '能耗看板', 'P1', 'active', 'x', 'x')"""
    )
    db.execute(
        "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES ('r', ?, 'x')",
        (str(root / "能耗看板"),),
    )
    db.execute(
        "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES ('r', 'm', 'x')"
    )
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, meeting_id, project_id, requirement_id, status_changed_at,
                             created_at, updated_at)
           VALUES ('t', '写一版方案', 'in_progress', 'ai', 'm', 'p', 'r', 'x', 'x', 'x')"""
    )
    db.execute(
        "INSERT INTO task_events(task_id, kind, body, created_at) VALUES ('t', 'confirmed', '任务已确认', ?)",
        ((now - timedelta(days=1)).isoformat(),),
    )
    # 一份二十天前的报价单：还写着下调 3%
    quote = root / "报价" / "报价单 v3.xlsx"
    quote.write_text("表格内容", encoding="utf-8")
    old = (now - timedelta(days=20)).timestamp()
    os.utime(quote, (old, old))
    info = quote.stat()
    key = "q2:" + "3" * 32
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, chars, chunks, created_at, updated_at)
           VALUES (?, 'text', 'done', 40, 2, 'x', 'x')""",
        (key,),
    )
    for ordinal, text in enumerate(("封面：云图报价", "报价说明：总价在原基础上下调 3%，含税")):
        db.execute(
            "INSERT INTO material_chunks(content_key, ordinal, loc, text) VALUES (?, ?, ?, ?)",
            (key, ordinal, f"第 {ordinal + 1} 页", text),
        )
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone, seen_at,
               content_key, content_size, content_mtime_ns)
           VALUES (?, '报价/报价单 v3.xlsx', '报价', '报价单 v3.xlsx', '报价单 v3', '报价单v3', 'xlsx', ?, ?, 'normal',
                   'x', ?, ?, ?)""",
        (root_id, info.st_size, info.st_mtime_ns, key, info.st_size, info.st_mtime_ns),
    )
    quote_id = int(
        db.query_one("SELECT id FROM material_files WHERE name = '报价单 v3.xlsx'")["id"]
    )
    db.execute("DELETE FROM material_file_events")
    # 需求文件夹里今天新出现的一份（包，size 是空的）
    plan = root / "能耗看板" / "能耗看板方案.key"
    plan.mkdir()
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone, seen_at)
           VALUES (?, '能耗看板/能耗看板方案.key', '能耗看板', '能耗看板方案.key', '能耗看板方案', '能耗看板方案', 'key',
                   NULL, ?, 'package', 'x')""",
        (root_id, plan.stat().st_mtime_ns),
    )
    plan_id = int(db.query_one("SELECT id FROM material_files WHERE ext = 'key'")["id"])
    later = datetime.now(UTC) + timedelta(seconds=1)
    affects.match_due(db, lambda: False, 5.0, now=later)
    produced.watch(db, later, 5.0)
    return SimpleNamespace(
        client=client,
        settings=settings,
        db=db,
        root=root,
        quote=quote,
        quote_id=quote_id,
        plan_id=plan_id,
    )


def rev(db) -> int:
    return int(db.query_one("SELECT value FROM app_state WHERE key = 'graph_rev'")["value"])


def test_question_shapes_and_whitelist(tmp_path):
    w = world(tmp_path)
    affects_body = w.client.get(f"/api/graph/files/{w.quote_id}").json()
    (question,) = affects_body["questions"]
    decision = w.db.query_one("SELECT id FROM decisions")
    day = datetime.now(UTC) - timedelta(days=3)
    local = day.astimezone().date()
    assert question == {
        "relation_id": question["relation_id"],
        "kind": "affects",
        "text": f"可能过时：{local.month}/{local.day} 决议『总价下调 5%』",
        "decision": {
            "id": decision["id"],
            "text": "总价下调 5%",
            "date": local.isoformat(),
            "meeting_id": "m",
            "meeting_title": "报价沟通",
            "start_ms": 754_000,
            "audio_url": None,
        },
        "passage": {"loc": "第 2 页", "text": "报价说明：总价在原基础上下调 3%，含税"},
        "file": {"id": w.quote_id, "name": "报价单 v3.xlsx", "folder": "报价/"},
        "words": ["总价"],
        "answers": ["updated", "no"],
    }
    produced_body = w.client.get(f"/api/graph/files/{w.plan_id}").json()
    (ask,) = produced_body["questions"]
    assert ask == {
        "relation_id": ask["relation_id"],
        "kind": "produced",
        "text": "会后 3 天新增在『能耗看板/』",
        "ask": "是任务『写一版方案』的交付物吗？",
        "task": {"id": "t", "title": "写一版方案", "status": "in_progress"},
        "file": {"id": w.plan_id, "name": "能耗看板方案.key", "folder": "能耗看板/"},
        "words": ["能耗看板"],
        "answers": ["yes", "no"],
    }
    for body in (
        affects_body,
        produced_body,
        w.client.get(f"/api/materials/files/{w.quote_id}/preview").json(),
    ):
        assert not HIDDEN & walk_keys(body["questions"])
    # 材料文字只在 passage 里现读，不进表
    row = w.db.query_one("SELECT quote, evidence_json FROM relations WHERE kind = 'affects'")
    assert "含税" not in row["quote"] + row["evidence_json"]


def test_preview_has_questions_after_deliverables_but_not_with_parts_preview(tmp_path):
    w = world(tmp_path)
    full = w.client.get(f"/api/materials/files/{w.quote_id}/preview").json()
    keys = list(full)
    assert keys.index("questions") == keys.index("deliverables") + 1
    assert [item["kind"] for item in full["questions"]] == ["affects"]
    light = w.client.get(
        f"/api/materials/files/{w.quote_id}/preview", params={"parts": "preview"}
    ).json()
    assert "questions" not in light


def test_stat_guard_drops_affects_without_writing(tmp_path, monkeypatch):
    w = world(tmp_path)
    relation = w.db.query_one("SELECT id, status, updated_at FROM relations WHERE kind = 'affects'")
    before = rev(w.db)
    w.quote.write_text("改好了的表格内容，长一点", encoding="utf-8")

    assert w.client.get(f"/api/graph/files/{w.quote_id}").json()["questions"] == []
    assert w.client.get(f"/api/materials/files/{w.quote_id}/preview").json()["questions"] == []
    # 接口不写库：行仍是 suggested，版本号不动（下一次整轮以后 L3 收回）
    assert (
        w.db.query_one("SELECT id, status, updated_at FROM relations WHERE kind = 'affects'")
        == relation
    )
    assert rev(w.db) == before

    # 资料盘不在时不 stat，问题照常给
    monkeypatch.setattr(materials, "volume_state", lambda _path: materials.ROOT_VOLUME_OFFLINE)
    assert [
        item["kind"] for item in w.client.get(f"/api/graph/files/{w.quote_id}").json()["questions"]
    ] == ["affects"]
    with w.db.autocommit() as connection:
        preview = material_status.file_preview(
            connection, w.quote_id, state_of=lambda _path: materials.ROOT_VOLUME_OFFLINE
        )
    assert [item["kind"] for item in preview["questions"]] == ["affects"]


def test_stat_guard_is_quiet_when_nothing_changed(tmp_path):
    w = world(tmp_path)
    with w.db.autocommit() as connection:
        row = material_status.file_row(connection, w.quote_id)
    assert affects.stat_guard(row, materials.volume_state) is True
    assert affects.stat_guard(row, lambda _path: materials.ROOT_VOLUME_OFFLINE) is True
    w.quote.unlink()
    assert affects.stat_guard(row, materials.volume_state) is False


def test_task_detail_has_suggestions(tmp_path):
    w = world(tmp_path)
    body = w.client.get("/api/tasks/t").json()
    (item,) = body["suggestions"]
    assert item["kind"] == "produced" and item["file"]["id"] == w.plan_id
    assert item["text"] == "会后 3 天新增在『能耗看板/』"
    assert not HIDDEN & walk_keys(body["suggestions"])


def test_requirement_card_stale_files_in_six_statements(tmp_path):
    w = world(tmp_path)
    live = SimpleNamespace(links_enabled=True, links_llm_enabled=True, links_llm_daily_calls=200)
    with w.db.autocommit() as connection:
        body = decisions.requirement_log(connection, "r", settings=live)
    entry = body["meetings"][0]["decisions"][0]
    (stale,) = entry["stale_files"]
    # 需求卡用文件这一边的说法，决议本身就是那一行
    assert stale["text"] == "『报价单 v3』之后没改过，可能过时"
    assert (
        stale["kind"] == "affects"
        and stale["file"]["id"] == w.quote_id
        and stale["answers"] == ["updated", "no"]
    )
    reads = count_reads(
        w.db, lambda connection: decisions.requirement_log(connection, "r", settings=live)
    )
    assert reads == 6
    # links_enabled 关着时按纪要现读，没有标记（接口的默认设置在测试里是关着的）
    off = w.client.get("/api/requirements/r/decisions").json()
    assert off["meetings"][0]["decisions"][0]["stale_files"] == []


def test_meeting_focus_has_stale_and_asks(tmp_path):
    w = world(tmp_path)
    focus = w.client.get("/api/graph/meetings/m").json()
    affects_id = w.db.query_one("SELECT id FROM relations WHERE kind = 'affects'")["id"]
    produced_id = w.db.query_one("SELECT id FROM relations WHERE kind = 'produced'")["id"]
    assert focus["decisions"][0]["stale"] == [
        {"relation_id": affects_id, "file_id": w.quote_id, "name": "报价单 v3.xlsx"}
    ]
    (task,) = focus["tasks"]
    assert task["asks"] == [
        {"relation_id": produced_id, "file_id": w.plan_id, "name": "能耗看板方案.key", "ext": "key"}
    ]


def test_answer_endpoint_refuses_a_cancelled_task(tmp_path):
    w = world(tmp_path)
    headers = write_headers(w.client)
    produced_id = w.db.query_one("SELECT id FROM relations WHERE kind = 'produced'")["id"]
    w.db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = 't'")
    response = w.client.post(
        f"/api/relations/{produced_id}/answer", json={"answer": "yes"}, headers=headers
    )
    assert (
        response.status_code == 409
        and response.json()["detail"] == "这条任务已经取消了，先恢复任务再登记"
    )
    w.db.execute("UPDATE tasks SET status = 'in_progress' WHERE id = 't'")
    response = w.client.post(
        f"/api/relations/{produced_id}/answer", json={"answer": "yes"}, headers=headers
    )
    assert response.status_code == 200 and response.json()["deliverable_id"]
    # ［是］以后问题没了，交付物在任务抽屉里
    body = w.client.get("/api/tasks/t").json()
    assert body["suggestions"] == [] and body["deliverables"][0]["name"] == "能耗看板方案.key"
