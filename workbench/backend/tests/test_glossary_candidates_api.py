"""第四期 4h：候选词的接口。GET 的键集合、看板的前 6 项、证据重新落位、记入/不是/撤销、404/409/422、
会议的 material_pairs。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from meeting_workbench import glossary, glossary_checkup
from meeting_workbench import glossary_mining as gm
from meeting_workbench.db import Database, utc_now

from .gm_world import NOW, mine, rows, terms, world
from .helpers import count_reads
from .test_search import add_meeting
from .test_tasks_api import make_client, write_headers

ITEM_KEYS = {
    "key",
    "term",
    "existing_term",
    "wrongs",
    "files",
    "spoken",
    "heard",
    "file_names",
    "file_quote",
}


@pytest.fixture
def api(tmp_path):
    client, settings = make_client(tmp_path)
    settings.links_enabled = True
    settings.glossary_mining_enabled = True
    db = Database(settings.database_path)
    world(tmp_path, db)
    mine(db)
    return client, settings, db, write_headers(client)


def snapshot_terms(settings):
    path = settings.data_dir / "glossary-snapshot.json"
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"terms": [], "projects": []}
    )


# ---------------------------------------------------------------------- 读


def test_get_items_have_a_fixed_key_set_and_no_scores(api):
    client, _settings, _db, _headers = api
    payload = client.get("/api/projects/p/glossary-candidates").json()
    assert payload["total"] == len(payload["items"]) == 2
    first, second = payload["items"]
    for item in payload["items"]:
        assert set(item) == ITEM_KEYS
    assert first["term"] == "司美格鲁肽" and first["wrongs"] == [
        {"text": "司美格鲁太", "meetings": 2}
    ]
    assert (
        first["heard"][0]["quote"].startswith("这次司美格鲁太")
        and first["heard"][0]["meeting"]["id"] == "m1"
    )
    assert first["file_quote"] is None  # 说过的词不给材料原话
    assert second["term"] == "驻场服务" and second["heard"] == []
    assert (
        second["file_quote"]["quote"].count("驻场服务") >= 1
        and len(second["file_quote"]["quote"]) <= 40 + 4
    )
    assert [entry["name"] for entry in second["file_names"]] == ["方案1.docx", "方案2.docx"]
    text = json.dumps(payload, ensure_ascii=False)
    for banned in ("score", "rank", "percent", "%"):
        assert banned not in text


def test_board_has_top_six_and_total_with_three_more_statements(api):
    client, _settings, db, _headers = api
    board = client.get("/api/projects/p/board").json()
    assert [item["term"] for item in board["glossary_candidates"]] == ["司美格鲁肽", "驻场服务"]
    assert board["glossary_candidate_total"] == 2
    assert count_reads(db, lambda c: gm.list_candidates(c, "p", limit=gm.SHOWN_ON_BOARD)) <= 3


def test_switched_off_hides_pending_words(api):
    client, settings, _db, _headers = api
    settings.glossary_mining_enabled = False
    assert client.get("/api/projects/p/glossary-candidates").json() == {"items": [], "total": 0}
    board = client.get("/api/projects/p/board").json()
    assert board["glossary_candidates"] == [] and board["glossary_candidate_total"] == 0


def test_all_projects_inbox_matches_the_project_lists(api):
    """词典页左栏的收件箱：每个有待认词的项目一组，内容和项目接口一致，待认多的在前。"""
    client, settings, _db, _headers = api
    inbox = client.get("/api/glossary/candidates").json()
    per_project = client.get("/api/projects/p/glossary-candidates").json()
    assert inbox["total"] == per_project["total"] == 2
    [group] = inbox["projects"]
    assert group["project_id"] == "p" and group["project_name"] and "project_color" in group
    assert group["items"] == per_project["items"] and group["total"] == per_project["total"]
    settings.glossary_mining_enabled = False
    assert client.get("/api/glossary/candidates").json() == {"projects": [], "total": 0}


def test_evidence_is_found_again_after_reread_and_retranscribe(api):
    client, _settings, db, _headers = api
    # 重读：片段 id 变了，(content_key, ordinal) 没变
    chunk = db.query_one("SELECT * FROM material_chunks WHERE content_key = 'k1'")
    db.execute("DELETE FROM material_chunks WHERE content_key = 'k1'")
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('k1', ?, ?)",
        (chunk["ordinal"], chunk["text"]),
    )
    # 重新转写：段落换了 id，时间点稍有出入
    version = db.create_transcript_version("m1", "funasr", published=True)
    db.replace_segments(
        version,
        "m1",
        [
            {"id": "m1-new-0", "ordinal": 0, "start_ms": 500, "end_ms": 900, "text": "开场白"},
            {
                "id": "m1-new-1",
                "ordinal": 1,
                "start_ms": 800,
                "end_ms": 1900,
                "text": "这次司美格鲁太的剂量先按",
            },
        ],
    )
    items = client.get("/api/projects/p/glossary-candidates").json()["items"]
    heard = items[0]["heard"]
    assert heard[0]["meeting"]["id"] == "m1" and heard[0]["start_ms"] == 800
    assert "司美格鲁太" in heard[0]["quote"]
    assert "驻场服务" in items[1]["file_quote"]["quote"]


def test_material_quote_falls_back_to_the_full_text_index_when_the_ordinal_moved(api):
    """重读后分段变了（ordinal 对不上）：在这份内容里用全文索引找一段含这个词的，语句数不变。"""
    client, _settings, db, _headers = api
    chunk = db.query_one("SELECT * FROM material_chunks WHERE content_key = 'k1'")
    for key in ("k1", "k2", "k3"):
        db.execute("DELETE FROM material_chunks WHERE content_key = ?", (key,))
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('k1', 0, '新加的封面页')"
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('k1', 1, ?)",
        (chunk["text"],),
    )
    item = next(
        item
        for item in client.get("/api/projects/p/glossary-candidates").json()["items"]
        if item["key"] == "驻场服务"
    )
    assert item["file_quote"] is not None and "驻场服务" in item["file_quote"]["quote"]
    assert count_reads(db, lambda c: gm.list_candidates(c, "p", limit=gm.SHOWN_ON_BOARD)) <= 3


# ---------------------------------------------------------------------- 记入


def test_accept_creates_a_material_term_and_rewrites_the_snapshot_once(api, monkeypatch):
    client, settings, db, headers = api
    calls = []
    original = glossary.rewrite_snapshot
    monkeypatch.setattr(
        glossary, "rewrite_snapshot", lambda *args: (calls.append(1), original(*args))[1]
    )

    result = client.post(
        "/api/projects/p/glossary-candidates/accept",
        json={"key": "司美格鲁肽", "not_wrong": []},
        headers=headers,
    ).json()

    assert calls == [1]
    assert result["created"] is True and result["already"] is False
    assert result["added_aliases"] == ["司美格鲁太"] and result["skipped_aliases"] == []
    assert result["text"] == "已记入『司美格鲁肽』，错写：司美格鲁太"
    assert result["undo_until"]
    term = db.query_one("SELECT * FROM glossary_terms WHERE term = '司美格鲁肽'")
    assert (
        term["source"],
        term["is_cue"],
        term["confirmed"],
        term["scope"],
        term["category"],
        term["project_id"],
    ) == ("material", 0, 1, "云图AI", "其他", "p")
    assert json.loads(term["aliases"]) == ["司美格鲁太"]
    assert {row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"} == {"accepted"}
    assert "司美格鲁肽" in {entry["term"] for entry in snapshot_terms(settings)["terms"]}
    again = client.post(
        "/api/projects/p/glossary-candidates/accept", json={"key": "司美格鲁肽"}, headers=headers
    )
    assert again.status_code == 409 and again.json()["detail"] == "这个词已经处理过了"


def test_accept_with_a_removed_wrong_marks_it_rejected(api):
    client, _settings, db, headers = api
    result = client.post(
        "/api/projects/p/glossary-candidates/accept",
        json={"key": "司美格鲁肽", "not_wrong": ["司美格鲁太"]},
        headers=headers,
    ).json()
    assert result["text"] == "已记入『司美格鲁肽』" and result["added_aliases"] == []
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "accepted", "司美格鲁太": "rejected"}


def test_accept_on_an_existing_term_appends_aliases(api):
    client, _settings, db, headers = api
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, project_id,
               created_at, updated_at) VALUES ('gt-1', '能耗看板', '["能耗看扳"]', '云图AI', '其他', 'manual', 1, 'p', ?, ?)""",
        (now, now),
    )
    add_meeting(
        db,
        "m7",
        date="2026-09-27T10:00:00",
        project_id="p",
        segments=["能耗看版上线", "能耗看版再看"],
    )
    mine(db, now=NOW + timedelta(hours=7))
    item = next(
        item
        for item in client.get("/api/projects/p/glossary-candidates").json()["items"]
        if item["term"] == "能耗看板"
    )
    assert item["existing_term"] == {"id": "gt-1", "term": "能耗看板"}
    result = client.post(
        "/api/projects/p/glossary-candidates/accept", json={"key": "能耗看板"}, headers=headers
    ).json()
    assert result["created"] is False and result["added_aliases"] == ["能耗看版"]
    assert result["text"] == "已把『能耗看版』记成『能耗看板』的错写"
    assert json.loads(
        db.query_one("SELECT aliases FROM glossary_terms WHERE id = 'gt-1'")["aliases"]
    ) == ["能耗看扳", "能耗看版"]


def test_accept_when_the_term_is_already_there(api):
    client, _settings, db, headers = api
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, created_at, updated_at)
           VALUES ('gt-2', '驻场服务', '[]', '通用', '其他', 'manual', 1, ?, ?)""",
        (now, now),
    )
    result = client.post(
        "/api/projects/p/glossary-candidates/accept", json={"key": "驻场服务"}, headers=headers
    ).json()
    assert result["already"] is True and result["text"] == "『驻场服务』已经在词典里了"
    assert (
        db.query_one("SELECT term_id FROM glossary_candidates WHERE term_key = '驻场服务'")[
            "term_id"
        ]
        == "gt-2"
    )


def test_accept_after_the_term_was_added_meanwhile_still_records_the_heard_wrong(api):
    """挖完以后、点［记入］以前，词在词典页被手动加上了（没写错写）：卡片上的「会上听成 司美格鲁太」
    照样记成那条词条的错写；撤销时把这次加的错写拿掉，行回到待认。"""
    _client, _settings, db, _headers = api
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, created_at, updated_at)
           VALUES ('gt-2', '司美格鲁肽', '[]', '通用', '药品', 'manual', 1, ?, ?)""",
        (now, now),
    )
    result = gm.accept(db, "p", "司美格鲁肽", [], now=NOW)
    assert result["already"] is False and result["added_aliases"] == ["司美格鲁太"]
    assert result["text"] == "已把『司美格鲁太』记成『司美格鲁肽』的错写"
    aliases = db.query_one("SELECT aliases FROM glossary_terms WHERE id = 'gt-2'")["aliases"]
    assert json.loads(aliases) == ["司美格鲁太"]
    gm.undo(db, "p", "司美格鲁肽", now=NOW + timedelta(seconds=5))
    aliases = db.query_one("SELECT aliases FROM glossary_terms WHERE id = 'gt-2'")["aliases"]
    assert json.loads(aliases) == []
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "pending", "司美格鲁太": "pending"}


def test_accept_skips_a_wrong_used_elsewhere(api):
    client, _settings, db, headers = api
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, created_at, updated_at)
           VALUES ('gt-3', '别的词', '["司美格鲁太"]', '通用', '其他', 'manual', 1, ?, ?)""",
        (now, now),
    )
    result = client.post(
        "/api/projects/p/glossary-candidates/accept", json={"key": "司美格鲁肽"}, headers=headers
    ).json()
    assert result["skipped_aliases"] == ["司美格鲁太"] and result["added_aliases"] == []
    assert result["text"] == "已记入『司美格鲁肽』；『司美格鲁太』已经用在别的词条上，没加成错写"


def test_invalid_term_is_422(api):
    client, _settings, db, headers = api
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, status, created_at, updated_at)
           VALUES ('p', '12345', '12345', 'pending', ?, ?)""",
        (now, now),
    )
    response = client.post(
        "/api/projects/p/glossary-candidates/accept", json={"key": "12345"}, headers=headers
    )
    assert response.status_code == 422 and response.json()["detail"] == "这个词不能记入词典"


def test_accept_leaves_dropped_wrongs_alone_and_undo_does_not_revive_them(api):
    """界面上没显示的（dropped）听错写法不跟着记成错写，行原样不动，撤销后也不回到 pending。"""
    client, _settings, db, headers = api
    db.execute("UPDATE segments SET text = '司美格鲁肽再看一下' WHERE text = '司美格鲁太再看一下'")
    add_meeting(db, "m3", date="2026-09-27T10:00:00", project_id="p", segments=["司美格鲁肽的剂量"])
    mine(db, now=NOW + timedelta(hours=7))
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "pending", "司美格鲁太": "dropped"}
    item = next(
        item
        for item in client.get("/api/projects/p/glossary-candidates").json()["items"]
        if item["key"] == "司美格鲁肽"
    )
    assert item["wrongs"] == []
    result = gm.accept(db, "p", "司美格鲁肽", [], now=NOW + timedelta(hours=7))
    assert result["text"] == "已记入『司美格鲁肽』" and result["added_aliases"] == []
    assert (
        json.loads(
            db.query_one("SELECT aliases FROM glossary_terms WHERE term = '司美格鲁肽'")["aliases"]
        )
        == []
    )
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "accepted", "司美格鲁太": "dropped"}
    gm.undo(db, "p", "司美格鲁肽", now=NOW + timedelta(hours=7, seconds=30))
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "pending", "司美格鲁太": "dropped"}
    # 不是也一样：只动 pending 的行
    gm.reject(db, "p", "司美格鲁肽", now=NOW + timedelta(hours=8))
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "rejected", "司美格鲁太": "dropped"}


def existing_term_world(db):
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, project_id,
               created_at, updated_at) VALUES ('gt-1', '能耗看板', '[]', '云图AI', '其他', 'manual', 1, 'p', ?, ?)""",
        (now, now),
    )
    add_meeting(
        db,
        "m7",
        date="2026-09-27T10:00:00",
        project_id="p",
        segments=["能耗看版上线", "能耗看版再看"],
    )
    mine(db, now=NOW + timedelta(hours=7))


def test_existing_term_with_every_wrong_removed_is_422_and_nothing_changes(api):
    client, _settings, db, headers = api
    existing_term_world(db)
    response = client.post(
        "/api/projects/p/glossary-candidates/accept",
        json={"key": "能耗看板", "not_wrong": ["能耗看版"]},
        headers=headers,
    )
    assert response.status_code == 422 and response.json()["detail"] == "至少留一个错写"
    assert {row["status"] for row in rows(db) if row["term_key"] == "能耗看板"} == {"pending"}
    assert (
        json.loads(db.query_one("SELECT aliases FROM glossary_terms WHERE id = 'gt-1'")["aliases"])
        == []
    )


def test_existing_term_whose_wrongs_are_all_taken_says_so_without_undo(api):
    client, _settings, db, headers = api
    existing_term_world(db)
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, scope, category, source, confirmed, created_at, updated_at)
           VALUES ('gt-9', '别的看板', '["能耗看版"]', '通用', '其他', 'manual', 1, ?, ?)""",
        (now, now),
    )
    response = client.post(
        "/api/projects/p/glossary-candidates/accept", json={"key": "能耗看板"}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "『能耗看版』已经用在别的词条上，没加成错写"
    assert {row["status"] for row in rows(db) if row["term_key"] == "能耗看板"} == {"pending"}
    assert (
        json.loads(db.query_one("SELECT aliases FROM glossary_terms WHERE id = 'gt-1'")["aliases"])
        == []
    )


def test_meeting_accept_records_only_the_shown_wrong(api, monkeypatch):
    """会议页一行只显示一个写法：只记这个写法，别的写法留着，在词典页记到『词』上。"""
    client, _settings, db, headers = api
    # 接口那一步也钉在 NOW：后面直接调 gm 用的是 NOW 往后的时刻，走真实时间的话过了 NOW+1 天先后就反了
    monkeypatch.setattr(gm, "_now", lambda now: now or NOW)
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, wrong, spoken, status, created_at, updated_at)
           VALUES ('p', '司美格鲁肽', '司美格鲁肽', '司美格鲁泰', 2, 'pending', ?, ?)""",
        (now, now),
    )
    result = client.post(
        "/api/projects/p/glossary-candidates/accept",
        json={"key": "司美格鲁肽", "only_wrong": "司美格鲁太"},
        headers=headers,
    ).json()
    assert result["added_aliases"] == ["司美格鲁太"]
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "accepted", "司美格鲁太": "accepted", "司美格鲁泰": "pending"}
    item = next(
        item
        for item in client.get("/api/projects/p/glossary-candidates").json()["items"]
        if item["key"] == "司美格鲁肽"
    )
    assert item["existing_term"]["term"] == "司美格鲁肽" and [
        w["text"] for w in item["wrongs"]
    ] == ["司美格鲁泰"]
    # 再记到已有词条上，撤销只撤这一次
    second = gm.accept(db, "p", "司美格鲁肽", [], now=NOW + timedelta(days=1))
    assert second["text"] == "已把『司美格鲁泰』记成『司美格鲁肽』的错写"
    gm.undo(db, "p", "司美格鲁肽", now=NOW + timedelta(days=1, seconds=5))
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "accepted", "司美格鲁太": "accepted", "司美格鲁泰": "pending"}
    assert json.loads(
        db.query_one("SELECT aliases FROM glossary_terms WHERE term = '司美格鲁肽'")["aliases"]
    ) == ["司美格鲁太"]
    # 在会议页点［不是］（这个写法），记入过的词本身不被冲掉
    gm.reject(db, "p", "司美格鲁肽", now=NOW + timedelta(days=1, seconds=10))
    statuses = {row["wrong"]: row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"}
    assert statuses == {"": "accepted", "司美格鲁太": "accepted", "司美格鲁泰": "rejected"}
    assert db.query_one(
        "SELECT term_id FROM glossary_candidates WHERE term_key = '司美格鲁肽' AND wrong = ''"
    )["term_id"]


# ---------------------------------------------------------------------- 不是


def test_reject_all_rows_and_never_again(api):
    client, _settings, db, headers = api
    result = client.post(
        "/api/projects/p/glossary-candidates/reject", json={"key": "司美格鲁肽"}, headers=headers
    ).json()
    assert result["text"] == "以后不再提『司美格鲁肽』" and result["undo_until"]
    assert {row["status"] for row in rows(db) if row["term_key"] == "司美格鲁肽"} == {"rejected"}
    mine(db, now=NOW + timedelta(hours=7))
    assert not any(term == "司美格鲁肽" for term, _wrong in terms(db))


# ---------------------------------------------------------------------- 撤销


def test_undo_within_600_seconds(api):
    _client, settings, db, _headers = api
    snapshot = settings.data_dir / "glossary-snapshot.json"
    gm.accept(db, "p", "司美格鲁肽", [], snapshot_path=snapshot, now=NOW)
    undone = gm.undo(
        db, "p", "司美格鲁肽", snapshot_path=snapshot, now=NOW + timedelta(seconds=599)
    )
    assert undone == {"status": "pending", "text": "已撤销，『司美格鲁肽』回到这里"}
    assert db.query_one("SELECT 1 AS x FROM glossary_terms WHERE term = '司美格鲁肽'") is None
    assert "司美格鲁肽" not in {entry["term"] for entry in snapshot_terms(settings)["terms"]}
    with pytest.raises(gm.CandidateError) as twice:
        gm.undo(db, "p", "司美格鲁肽", now=NOW + timedelta(seconds=600))
    assert (twice.value.status, twice.value.text) == (409, "已经撤销过了")
    gm.reject(db, "p", "驻场服务", now=NOW)
    assert gm.undo(db, "p", "驻场服务", now=NOW + timedelta(seconds=10))["status"] == "pending"


def test_undo_after_600_seconds_or_after_an_edit_is_409(api):
    _client, _settings, db, _headers = api
    gm.accept(db, "p", "司美格鲁肽", [], now=NOW)
    with pytest.raises(gm.CandidateError) as late:
        gm.undo(db, "p", "司美格鲁肽", now=NOW + timedelta(seconds=601))
    assert (late.value.status, late.value.text) == (409, "已超过撤销时间，请直接改回")
    gm.reject(db, "p", "驻场服务", now=NOW)
    gm.undo(db, "p", "驻场服务", now=NOW)
    gm.accept(db, "p", "驻场服务", [], now=NOW)
    db.execute("UPDATE glossary_terms SET updated_at = 'later' WHERE term = '驻场服务'")
    with pytest.raises(gm.CandidateError) as changed:
        gm.undo(db, "p", "驻场服务", now=NOW + timedelta(seconds=5))
    assert (changed.value.status, changed.value.text) == (409, "这个词后来改过了，请在词典里直接改")


# ---------------------------------------------------------------------- 404、CSRF


def test_not_found_and_csrf(api):
    client, _settings, _db, headers = api
    assert client.get("/api/projects/nope/glossary-candidates").json()["detail"] == "项目不存在"
    missing = client.post(
        "/api/projects/p/glossary-candidates/reject", json={"key": "没有这个词"}, headers=headers
    )
    assert missing.status_code == 404 and missing.json()["detail"] == "这个词已经不在了"
    unknown = client.post(
        "/api/projects/nope/glossary-candidates/reject", json={"key": "x"}, headers=headers
    )
    assert unknown.status_code == 404 and unknown.json()["detail"] == "项目不存在"
    assert (
        client.post(
            "/api/projects/p/glossary-candidates/reject", json={"key": "驻场服务"}
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------- 会议页


def test_meeting_material_pairs(api):
    client, _settings, db, _headers = api
    add_meeting(db, "m-free", date="2026-09-27T10:00:00", segments=["司美格鲁太没归项目"])
    for meeting_id in ("m1", "m2", "m-free"):
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind, published, created_at)
                   VALUES (?, ?, 1, '# 周会', '', 'generated', 0, ?)""",
                (f"mv-{meeting_id}", meeting_id, utc_now()),
            )
            connection.execute(
                "UPDATE meetings SET current_minutes_version_id = ? WHERE id = ?",
                (f"mv-{meeting_id}", meeting_id),
            )
        glossary_checkup.check_meeting(db, meeting_id)
    pairs = client.get("/api/meetings/m1/glossary").json()["glossary"]["material_pairs"]
    assert pairs == [
        {
            "key": "司美格鲁肽",
            "term": "司美格鲁肽",
            "wrong": "司美格鲁太",
            "start_ms": 0,
            "quote": "这次司美格鲁太的剂量先按",
            "project": {"id": "p", "name": "云图AI"},
        }
    ]
    assert client.get("/api/meetings/m1").json()["glossary"]["material_pairs"] == pairs
    assert client.get("/api/meetings/m-free/glossary").json()["glossary"]["material_pairs"] == []
