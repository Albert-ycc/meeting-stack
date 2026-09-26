"""术语词典库：CRUD、快照导出、diff 反写过滤、API 与 save_minutes 挂钩测试。"""
from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.glossary import (
    _edit_distance,
    _punct_only,
    add_suggestion,
    confirm_suggestion,
    create_term,
    delete_term,
    extract_corrections,
    get_term,
    list_suggestions,
    list_terms,
    read_snapshot,
    reject_suggestion,
    rewrite_snapshot,
    update_term,
)
from meeting_workbench.main import create_app
from meeting_workbench.relay_client import RelayUnavailable

from .helpers import seed_editable_meeting


class FakeRelayClient:
    def __init__(self):
        pass

    def health(self):
        return {"status": "healthy", "worker": {"state": "idle"}, "counts": {}}

    def list_jobs(self, *, status=None, limit=200):
        return []

    def status(self, job_id):
        raise RelayUnavailable("no job")

    def set_substate(self, job_id, name, target, attempt=1):
        return {"job_id": job_id, "substates": {name: {"status": target}}}


def make_client(tmp_path):
    # extract_pending 的无 key 短路检查真实文件，放一个假 key 避免误读
    key_file = tmp_path / "api-key"
    key_file.write_text("test-key", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        llm_api_key_file=key_file,
    )
    return TestClient(create_app(settings, FakeRelayClient())), settings


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {
        "X-CSRF-Token": token,
        "Origin": "http://testserver",
        # 无 body 的 POST/DELETE 也要求 application/json，统一带上
        "Content-Type": "application/json",
    }


def seed_minutes(db, meeting_id="vm-20260102-101500", markdown="# 摘要\n会上确定成立树立协会。"):
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-1', ?, 1, ?, '<p></p>', 'generated', 1, ?)""",
        (meeting_id, markdown, utc_now()),
    )
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-1' WHERE id=?", (meeting_id,))


# —— extract_corrections：diff 过滤是核心质量点 ——


def test_edit_distance_basics():
    assert _edit_distance("树立", "数理") == 2
    assert _edit_distance("树立协会", "树立协会") == 0
    assert _edit_distance("树立协绘", "数理协会") == 3
    assert _edit_distance("abc", "abd") == 1


def test_punct_only_helper():
    assert _punct_only("树立、", "树立.") is True
    assert _punct_only("树立", "数理") is False


def test_extract_corrections_captures_homophone_fix():
    assert extract_corrections("树立协会", "数理协会", set()) == [("树立", "数理")]


def test_extract_corrections_captures_fix_inside_sentence():
    old = "会议确定成立树立协会，下周三执行。"
    new = "会议确定成立数理协会，下周三执行。"
    assert extract_corrections(old, new, set()) == [("树立", "数理")]


def test_extract_corrections_captures_fix_when_new_side_has_known_term():
    # 用户在往已知权威写法改（数理协会是权威词），即使被字符级 diff 拆碎也捕获
    old = "我们确定树立协绘方案"
    new = "我们确定数理协会方案"
    assert extract_corrections(old, new, {"数理协会"}) == [("树立", "数理")]


def test_extract_corrections_excludes_known_to_known_rewrite():
    # 两个都是权威写法 = 内容实质变更（把数理协会整体改成数学学会），不是错字更正
    old = "会议确定成立数理协会"
    new = "会议确定成立数学学会"
    assert extract_corrections(old, new, {"数理协会", "数学学会"}) == []


def test_extract_corrections_excludes_whole_sentence_rewrite():
    old = "我们决定把整个项目的执行节奏重新调整一遍。"
    new = "战略方向调整后，各条线的推进顺序重新排了。"
    assert extract_corrections(old, new, set()) == []


def test_extract_corrections_excludes_single_char_and_digits():
    assert extract_corrections("由我负责", "由你负责", set()) == []
    assert extract_corrections("方案编号100", "方案编号200", set()) == []


def test_extract_corrections_excludes_punct_only_change():
    assert extract_corrections("确立·协会", "确立、协会", set()) == []


def test_extract_corrections_dedupes_repeated_pairs():
    old = "树立协会和树立协会的工作"
    new = "数理协会和数理协会的工作"
    assert extract_corrections(old, new, set()) == [("树立", "数理")]


# —— glossary_terms CRUD ——


def test_terms_crud_roundtrip(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    created = create_term(
        db,
        term="数理协会",
        aliases=["树立协会", "树立协绘"],
        scope="通用",
        category="机构",
    )
    assert created["term"] == "数理协会"
    assert created["aliases"] == ["树立协会", "树立协绘"]
    assert created["scope"] == "通用"
    assert created["category"] == "机构"
    assert created["confirmed"] == 1
    assert created["source"] == "manual"

    updated = update_term(db, created["id"], aliases=["树立协会"], category="术语")
    assert updated["aliases"] == ["树立协会"]
    assert updated["category"] == "术语"
    assert updated["term"] == "数理协会"

    assert [t["term"] for t in list_terms(db)] == ["数理协会"]
    assert delete_term(db, created["id"]) is True
    assert get_term(db, created["id"]) is None


def test_duplicate_term_rejected(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    create_term(db, term="数理协会")
    try:
        create_term(db, term="数理协会")
    except ValueError:
        pass
    else:
        raise AssertionError("重复术语应被拒绝")


# —— 快照导出 ——


def test_snapshot_exports_only_confirmed_terms(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    create_term(db, term="数理协会", aliases=["树立协会"], category="机构")
    create_term(db, term="未确认词", confirmed=False)
    snapshot = tmp_path / "glossary-snapshot.json"
    rewrite_snapshot(db, snapshot)

    payload = read_snapshot(snapshot)
    assert payload["schema_version"] == 1
    assert payload["updated_at"] is not None
    assert payload["terms"] == [
        {
            "term": "数理协会",
            "aliases": ["树立协会"],
            "scope": "通用",
            "category": "机构",
        }
    ]


def test_snapshot_read_missing_file_returns_empty(tmp_path):
    assert read_snapshot(tmp_path / "glossary-snapshot.json") == {
        "schema_version": 1,
        "updated_at": None,
        "terms": [],
    }


def test_snapshot_rewrites_after_change(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    snapshot = tmp_path / "glossary-snapshot.json"
    create_term(db, term="数理协会", snapshot_path=snapshot)
    first = read_snapshot(snapshot)
    create_term(db, term="云图", category="术语", snapshot_path=snapshot)
    second = read_snapshot(snapshot)
    assert [t["term"] for t in first["terms"]] == ["数理协会"]
    assert [t["term"] for t in second["terms"]] == ["云图", "数理协会"]


# —— 待确认队列 ——


def test_suggestion_confirm_backfills_term(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    assert add_suggestion(db, wrong="树立", correct="数理")
    rows = list_suggestions(db, status="pending")
    assert len(rows) == 1
    suggestion_id = rows[0]["id"]

    snapshot = tmp_path / "glossary-snapshot.json"
    result = confirm_suggestion(db, suggestion_id, snapshot_path=snapshot)
    assert result["created"] is True
    assert (result["wrong"], result["correct"]) == ("树立", "数理")
    # 确认后反写进词典：term=数理, alias=树立
    terms = list_terms(db)
    assert [(t["term"], t["aliases"]) for t in terms] == [("数理", ["树立"])]
    assert list_suggestions(db, status="confirmed")[0]["id"] == suggestion_id
    assert [t["term"] for t in read_snapshot(snapshot)["terms"]] == ["数理"]
    # 已处理的不再重复确认
    assert confirm_suggestion(db, suggestion_id) is None


def test_suggestion_reject_keeps_out_of_dictionary(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    add_suggestion(db, wrong="树立", correct="数理")
    suggestion_id = list_suggestions(db, status="pending")[0]["id"]
    assert reject_suggestion(db, suggestion_id) is True
    assert list_terms(db) == []
    assert reject_suggestion(db, suggestion_id) is False


def test_add_suggestion_dedupes(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    assert add_suggestion(db, wrong="树立", correct="数理")
    assert add_suggestion(db, wrong="树立", correct="数理") is None
    # 映射已入词典（aliases 已含 wrong）则不再排队
    create_term(db, term="协会", aliases=["协力"])
    assert add_suggestion(db, wrong="协力", correct="协会") is None
    # 扩词前的 2 字那对已在词典里，扩出来的整词也不再排队
    assert (
        add_suggestion(
            db, wrong="协力小组", correct="协会小组", alt_wrong="协力", alt_correct="协会"
        )
        is None
    )


# —— API 层（CSRF + JSON 约定） ——


def test_glossary_terms_api_crud(tmp_path):
    client, _settings = make_client(tmp_path)

    created = client.post(
        "/api/glossary/terms",
        json={"term": "数理协会", "aliases": ["树立协会"], "category": "机构"},
        headers=write_headers(client),
    )
    assert created.status_code == 200
    body = created.json()
    assert body["term"] == "数理协会"
    term_id = body["id"]

    listing = client.get("/api/glossary/terms").json()
    assert [t["term"] for t in listing] == ["数理协会"]

    updated = client.put(
        f"/api/glossary/terms/{term_id}",
        json={"aliases": ["树立协会", "树立协绘"]},
        headers=write_headers(client),
    )
    assert updated.status_code == 200
    assert updated.json()["aliases"] == ["树立协会", "树立协绘"]

    deleted = client.delete(
        f"/api/glossary/terms/{term_id}", headers=write_headers(client)
    )
    assert deleted.status_code == 200
    assert client.get("/api/glossary/terms").json() == []


def test_glossary_terms_api_rejects_write_without_csrf(tmp_path):
    client, _settings = make_client(tmp_path)
    response = client.post(
        "/api/glossary/terms",
        json={"term": "数理协会"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 403


def test_glossary_suggestions_api_confirm(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    add_suggestion(db, wrong="树立", correct="数理")

    pending = client.get("/api/glossary/suggestions").json()
    assert len(pending) == 1
    suggestion_id = pending[0]["id"]

    confirmed = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/confirm",
        headers=write_headers(client),
    )
    assert confirmed.status_code == 200
    assert [t["term"] for t in client.get("/api/glossary/terms").json()] == ["数理"]

    rejected = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/confirm",
        headers=write_headers(client),
    )
    assert rejected.status_code == 404

    snapshot = client.get("/api/glossary/snapshot").json()
    assert [t["term"] for t in snapshot["terms"]] == ["数理"]
    assert snapshot["schema_version"] == 1


def test_glossary_suggestions_api_reject(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    add_suggestion(db, wrong="树立", correct="数理")
    suggestion_id = client.get("/api/glossary/suggestions").json()[0]["id"]

    response = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/reject",
        headers=write_headers(client),
    )
    assert response.status_code == 200
    assert client.get("/api/glossary/suggestions").json()[0]["status"] == "rejected"
    assert client.get("/api/glossary/suggestions", params={"status": "pending"}).json() == []
    assert client.get("/api/glossary/terms").json() == []


def test_glossary_terms_api_project_id_derives_scope_and_ignores_request_scope(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO projects (id, name, color, origin, created_at) VALUES (?, ?, ?, 'manual', ?)",
        ("proj-mdt", "MDT", "#667085", utc_now()),
    )

    created = client.post(
        "/api/glossary/terms",
        json={"term": "多学科会诊", "scope": "别的桶", "project_id": "proj-mdt"},
        headers=write_headers(client),
    )
    assert created.status_code == 200
    term_id = created.json()["id"]

    listing = client.get("/api/glossary/terms").json()
    row = next(t for t in listing if t["id"] == term_id)
    assert row["project_id"] == "proj-mdt"
    assert row["project_name"] == "MDT"
    assert row["scope"] == "MDT"  # 请求里的 "别的桶" 被忽略

    # 改绑到不存在的项目 → 404
    missing = client.put(
        f"/api/glossary/terms/{term_id}",
        json={"project_id": "proj-does-not-exist"},
        headers=write_headers(client),
    )
    assert missing.status_code == 404

    # 解绑：project_id 显式传 null，回落到给定 scope
    unbound = client.put(
        f"/api/glossary/terms/{term_id}",
        json={"project_id": None, "scope": "临时桶"},
        headers=write_headers(client),
    )
    assert unbound.status_code == 200
    assert unbound.json()["project_id"] is None
    assert unbound.json()["scope"] == "临时桶"


def test_glossary_create_term_api_project_id_not_found(tmp_path):
    client, _settings = make_client(tmp_path)
    response = client.post(
        "/api/glossary/terms",
        json={"term": "数理协会", "project_id": "proj-does-not-exist"},
        headers=write_headers(client),
    )
    assert response.status_code == 404


def test_glossary_update_term_ignores_bare_scope_when_project_bound(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO projects (id, name, color, origin, created_at) VALUES (?, ?, ?, 'manual', ?)",
        ("proj-mdt", "MDT", "#667085", utc_now()),
    )
    term_id = client.post(
        "/api/glossary/terms",
        json={"term": "多学科会诊", "project_id": "proj-mdt"},
        headers=write_headers(client),
    ).json()["id"]

    # 已挂项目的术语不接受脱离 project_id 单独改 scope
    updated = client.put(
        f"/api/glossary/terms/{term_id}",
        json={"scope": "别的桶"},
        headers=write_headers(client),
    )
    assert updated.status_code == 200
    assert updated.json()["scope"] == "MDT"


def test_glossary_terms_api_project_id_none_filters_unassigned(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO projects (id, name, color, origin, created_at) VALUES (?, ?, ?, 'manual', ?)",
        ("proj-mdt", "MDT", "#667085", utc_now()),
    )
    client.post(
        "/api/glossary/terms",
        json={"term": "多学科会诊", "project_id": "proj-mdt"},
        headers=write_headers(client),
    )
    client.post(
        "/api/glossary/terms", json={"term": "数理协会"}, headers=write_headers(client)
    )

    unassigned = client.get("/api/glossary/terms", params={"project_id": "none"}).json()
    assert [t["term"] for t in unassigned] == ["数理协会"]

    scoped = client.get("/api/glossary/terms", params={"project_id": "proj-mdt"}).json()
    assert [t["term"] for t in scoped] == ["多学科会诊"]


def test_glossary_scopes_api_groups_general_project_and_bucket(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO projects (id, name, color, origin, created_at) VALUES (?, ?, ?, 'manual', ?)",
        ("proj-mdt", "MDT", "#2c8d83", utc_now()),
    )
    client.post(
        "/api/glossary/terms",
        json={"term": "多学科会诊", "project_id": "proj-mdt"},
        headers=write_headers(client),
    )
    client.post(
        "/api/glossary/terms", json={"term": "数理协会"}, headers=write_headers(client)
    )
    client.post(
        "/api/glossary/terms",
        json={"term": "样品发放", "scope": "启航"},
        headers=write_headers(client),
    )

    scopes = client.get("/api/glossary/scopes").json()
    assert scopes == [
        {"kind": "general", "key": "通用", "label": "公共", "color": None, "count": 1},
        {
            "kind": "project",
            "key": "proj-mdt",
            "label": "MDT",
            "color": "#2c8d83",
            "count": 1,
            "last_meeting_at": None,
        },
        # 旧分组桶只在库里还有没挂项目、也不是公共的词条时出现
        {"kind": "bucket", "key": "启航", "label": "启航", "color": None, "count": 1},
    ]


def test_glossary_snapshot_api_empty_before_any_write(tmp_path):
    client, _settings = make_client(tmp_path)
    payload = client.get("/api/glossary/snapshot").json()
    assert payload == {"schema_version": 1, "updated_at": None, "terms": []}


# —— save_minutes 挂钩：编辑纪要时 diff 反写 ——


def test_save_minutes_hooks_diff_into_suggestions(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    seed_minutes(db, markdown="会上确定成立树立协会，下周三执行。")

    saved = client.put(
        "/api/meetings/vm-20260102-101500/minutes",
        json={
            "markdown": "会上确定成立数理协会，下周三执行。",
            "base_version_id": "mv-1",
        },
        headers=write_headers(client),
    )
    assert saved.status_code == 200

    # 2 字片段沿后文扩成整词，原来的 2 字那对留作「只记 2 字」
    assert [
        (row["wrong"], row["correct"], row["alt_wrong"], row["alt_correct"], row["auto_recorded"])
        for row in saved.json()["corrections"]
    ] == [("树立协会", "数理协会", "树立", "数理", False)]
    pending = client.get("/api/glossary/suggestions", params={"status": "pending"}).json()
    assert [(row["wrong"], row["correct"]) for row in pending] == [("树立协会", "数理协会")]
    assert pending[0]["meeting_id"] == "vm-20260102-101500"
    assert pending[0]["meeting_title"] == "需求复盘会"
    # 会议没归项目：默认记到公共
    assert pending[0]["target_project_id"] is None
    assert "数理协会" in pending[0]["context"]


def test_save_minutes_suggestion_scope_follows_project(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    db.execute(
        "INSERT INTO projects (id, name, color, origin, created_at) VALUES (?, ?, ?, 'manual', ?)",
        ("proj-1", "云图", "#667085", utc_now()),
    )
    db.execute(
        "UPDATE meetings SET project_id='proj-1' WHERE id='vm-20260102-101500'"
    )
    seed_minutes(db, markdown="会上确定成立树立协会，下周三执行。")

    saved = client.put(
        "/api/meetings/vm-20260102-101500/minutes",
        json={
            "markdown": "会上确定成立数理协会，下周三执行。",
            "base_version_id": "mv-1",
        },
        headers=write_headers(client),
    )
    assert saved.status_code == 200

    pending = client.get("/api/glossary/suggestions").json()
    assert [(row["wrong"], row["correct"]) for row in pending] == [("树立协会", "数理协会")]
    assert pending[0]["scope"] == "云图"
    assert (pending[0]["target_project_id"], pending[0]["target_project_name"]) == (
        "proj-1",
        "云图",
    )


def test_save_minutes_first_save_does_not_create_suggestions(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)

    saved = client.put(
        "/api/meetings/vm-20260102-101500/minutes",
        json={"markdown": "会上确定成立树立协会。", "base_version_id": None},
        headers=write_headers(client),
    )
    assert saved.status_code == 200
    assert client.get("/api/glossary/suggestions").json() == []
