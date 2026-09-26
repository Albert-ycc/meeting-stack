"""1d-1a 纠错词的落点：确认时记到哪、撤销确认、恢复驳回、2 字扩整词、保存纪要直接记入。"""
import json

from meeting_workbench.db import Database, utc_now
from meeting_workbench.glossary import (
    add_suggestion,
    confirm_suggestion,
    create_term,
    extract_correction_candidates,
    get_term,
    list_scopes,
    list_suggestions,
    list_terms,
    read_snapshot,
    record_corrections_from_diff,
    reject_suggestion,
    restore_suggestion,
    undo_confirm_suggestion,
)

from .helpers import seed_editable_meeting
from .test_glossary import make_client, seed_minutes, write_headers

MEETING = "vm-20260102-101500"


def make_db(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    return db


def add_project(db, project_id, name, also=None):
    db.execute(
        """INSERT INTO projects (id, name, color, origin, also_names, created_at)
           VALUES (?, ?, '#667085', 'manual', ?, ?)""",
        (project_id, name, json.dumps(also or [], ensure_ascii=False), utc_now()),
    )


def add_meeting(db, meeting_id=MEETING, project_id=None):
    db.execute(
        """INSERT INTO meetings (id, title, status, project_id, created_at, updated_at)
           VALUES (?, '周会', 'completed_unreviewed', ?, ?, ?)""",
        (meeting_id, project_id, utc_now(), utc_now()),
    )


# —— 2 字扩整词 ——


def test_two_char_fragment_expands_along_following_text():
    old = "会上确定成立树立协会，下周三执行。"
    new = "会上确定成立数理协会，下周三执行。"
    assert extract_correction_candidates(old, new, set()) == [
        {"wrong": "树立协会", "correct": "数理协会", "alt_wrong": "树立", "alt_correct": "数理"}
    ]


def test_expansion_stops_at_punctuation_particles_and_six_chars():
    # 右边紧跟标点、左边是「去」：扩不出来，保持 2 字
    assert extract_correction_candidates("我们去树立，好", "我们去数理，好", set()) == [
        {"wrong": "树立", "correct": "数理", "alt_wrong": None, "alt_correct": None}
    ]
    # 遇到「的」停
    assert extract_correction_candidates("树立协会的事", "数理协会的事", set())[0]["wrong"] == "树立协会"
    # 最长 6 字
    assert extract_correction_candidates("树立协会章程草案", "数理协会章程草案", set())[0][
        "wrong"
    ] == "树立协会章程"


def test_expansion_goes_left_when_right_side_is_punctuation():
    candidates = extract_correction_candidates("决定成立数据终态。", "决定成立数据中台。", set())
    # 右边是句号，向左只补 2 个字
    assert candidates == [
        {"wrong": "数据终态", "correct": "数据中台", "alt_wrong": "终态", "alt_correct": "中台"}
    ]


def test_fix_inside_known_term_expands_to_that_term_even_for_one_char():
    assert extract_correction_candidates("患者随方两周。", "患者随访两周。", {"随访"}) == [
        {"wrong": "随方", "correct": "随访", "alt_wrong": None, "alt_correct": None}
    ]
    # 没有已知词兜底的单字改动仍然不算（由我负责 → 由你负责）
    assert extract_correction_candidates("由我负责", "由你负责", {"随访"}) == []
    assert extract_correction_candidates("患者随方两周。", "患者随访两周。", set()) == []


# —— 确认时记到哪 ——


def test_confirm_defaults_to_meeting_project_at_confirm_time(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    add_project(db, "p-sj", "数据中台")
    add_meeting(db, project_id="p-yt")
    suggestion_id = add_suggestion(db, wrong="树立协会", correct="数理协会", scope="云图", meeting_id=MEETING)
    # 建议排进来之后会议被改到了数据中台：按确认那一刻的项目记
    db.execute("UPDATE meetings SET project_id='p-sj' WHERE id=?", (MEETING,))

    result = confirm_suggestion(db, suggestion_id)
    assert result["created"] is True
    term = result["term"]
    assert (term["project_id"], term["scope"], term["project_name"]) == ("p-sj", "数据中台", "数据中台")
    assert term["aliases"] == ["树立协会"]
    row = list_suggestions(db, status="confirmed")[0]
    assert (row["confirmed_term_id"], row["confirmed_wrong"]) == (term["id"], "树立协会")


def test_confirm_without_project_or_public_target_goes_public(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    add_meeting(db)
    first = add_suggestion(db, wrong="树立", correct="数理", meeting_id=MEETING)
    assert confirm_suggestion(db, first)["term"]["project_id"] is None

    db.execute("UPDATE meetings SET project_id='p-yt' WHERE id=?", (MEETING,))
    second = add_suggestion(db, wrong="岳总", correct="月总", meeting_id=MEETING)
    term = confirm_suggestion(db, second, target="public")["term"]
    assert (term["project_id"], term["scope"]) == (None, "通用")


def test_confirm_into_named_project_and_unknown_project(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    add_meeting(db)
    suggestion_id = add_suggestion(db, wrong="树立", correct="数理", meeting_id=MEETING)
    try:
        confirm_suggestion(db, suggestion_id, target="p-missing")
    except ValueError as error:
        assert "项目不存在" in str(error)
    else:
        raise AssertionError("未知项目应当报错")
    # 报错时建议保持待确认
    assert list_suggestions(db, status="pending")[0]["id"] == suggestion_id
    term = confirm_suggestion(db, suggestion_id, target="p-yt")["term"]
    assert (term["project_id"], term["scope"]) == ("p-yt", "云图")


def test_confirm_adds_to_existing_term_without_moving_it(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    add_meeting(db, project_id="p-yt")
    existing = create_term(db, term="数理协会", aliases=["数立协会"])
    suggestion_id = add_suggestion(db, wrong="树立协会", correct="数理协会", meeting_id=MEETING)
    listed = list_suggestions(db, status="pending")[0]
    assert (listed["existing_term_id"], listed["existing_term_project_id"]) == (existing["id"], None)

    result = confirm_suggestion(db, suggestion_id)
    assert result["created"] is False
    assert result["term"]["id"] == existing["id"]
    # 公共词条留在公共，只是多了一个错写
    assert result["term"]["project_id"] is None
    assert result["term"]["aliases"] == ["数立协会", "树立协会"]


def test_confirm_short_records_the_two_char_pair(tmp_path):
    db = make_db(tmp_path)
    add_meeting(db)
    suggestion_id = add_suggestion(
        db, wrong="树立协会", correct="数理协会", meeting_id=MEETING, alt_wrong="树立", alt_correct="数理"
    )
    result = confirm_suggestion(db, suggestion_id, short=True)
    assert (result["wrong"], result["correct"]) == ("树立", "数理")
    assert [(t["term"], t["aliases"]) for t in list_terms(db)] == [("数理", ["树立"])]


def test_project_suggestion_lands_in_project_and_follows_rename(tmp_path):
    """验收：项目会议的建议确认后 project_id 不为空，list_scopes 里没有和项目同名的桶；
    项目改名后词条 scope 跟着变，旧名进了项目的也叫。"""
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    add_project(db, "p-yt", "云图")
    db.execute("UPDATE meetings SET project_id='p-yt' WHERE id=?", (MEETING,))
    seed_minutes(db, markdown="会上确定成立树立协会，下周三执行。")
    saved = client.put(
        f"/api/meetings/{MEETING}/minutes",
        json={"markdown": "会上确定成立数理协会，下周三执行。", "base_version_id": "mv-1"},
        headers=write_headers(client),
    ).json()
    suggestion_id = saved["corrections"][0]["id"]
    assert saved["corrections"][0]["target_project_name"] == "云图"

    confirmed = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/confirm", headers=write_headers(client)
    )
    assert confirmed.status_code == 200
    term = confirmed.json()["term"]
    assert term["project_id"] == "p-yt"
    assert confirmed.json()["suggestion"]["status"] == "confirmed"
    scopes = client.get("/api/glossary/scopes").json()
    assert not [scope for scope in scopes if scope["kind"] == "bucket"]
    assert [scope["key"] for scope in scopes if scope["kind"] == "project"] == ["p-yt"]

    renamed = client.patch(
        "/api/projects/p-yt", json={"name": "云图科研"}, headers=write_headers(client)
    )
    assert renamed.status_code == 200
    assert "云图" in [entry["name"] for entry in renamed.json()["also_names"]]
    assert get_term(db, term["id"])["scope"] == "云图科研"
    snapshot = read_snapshot(settings.data_dir / "glossary-snapshot.json")
    assert [(t["term"], t["scope"]) for t in snapshot["terms"]] == [("数理协会", "云图科研")]


# —— 撤销确认、恢复驳回 ——


def test_undo_confirm_deletes_auto_term_and_returns_to_pending(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    add_meeting(db, project_id="p-yt")
    snapshot = tmp_path / "snap.json"
    suggestion_id = add_suggestion(db, wrong="树立协会", correct="数理协会", meeting_id=MEETING)
    confirm_suggestion(db, suggestion_id, snapshot_path=snapshot)
    assert [t["term"] for t in read_snapshot(snapshot)["terms"]] == ["数理协会"]

    assert undo_confirm_suggestion(db, suggestion_id, snapshot_path=snapshot) is True
    assert list_terms(db) == []
    assert read_snapshot(snapshot)["terms"] == []
    row = list_suggestions(db, status="pending")[0]
    assert (row["id"], row["confirmed_term_id"], row["confirmed_wrong"]) == (suggestion_id, None, None)
    # 撤销过的可以换个落点再确认
    assert confirm_suggestion(db, suggestion_id, target="public")["term"]["project_id"] is None
    # 待确认的不能撤销
    assert undo_confirm_suggestion(db, "gs-missing") is False


def test_undo_confirm_keeps_existing_term_and_its_other_aliases(tmp_path):
    db = make_db(tmp_path)
    add_meeting(db)
    existing = create_term(db, term="数理协会", aliases=["数立协会"])
    suggestion_id = add_suggestion(db, wrong="树立协会", correct="数理协会", meeting_id=MEETING)
    confirm_suggestion(db, suggestion_id)
    assert undo_confirm_suggestion(db, suggestion_id) is True
    assert get_term(db, existing["id"])["aliases"] == ["数立协会"]


def test_undo_confirm_keeps_auto_term_that_gained_other_aliases(tmp_path):
    db = make_db(tmp_path)
    add_meeting(db)
    first = add_suggestion(db, wrong="树立", correct="数理", meeting_id=MEETING)
    second = add_suggestion(db, wrong="数立", correct="数理", meeting_id=MEETING)
    confirm_suggestion(db, first)
    confirm_suggestion(db, second)
    assert undo_confirm_suggestion(db, first) is True
    assert [(t["term"], t["aliases"]) for t in list_terms(db)] == [("数理", ["数立"])]


def test_undo_confirm_legacy_row_without_landing(tmp_path):
    """升级前确认的建议没有记落点：按正确写法找词条拿掉错写。"""
    db = make_db(tmp_path)
    create_term(db, term="数理", aliases=["树立"], source="auto")
    db.execute(
        """INSERT INTO glossary_suggestions (id, wrong, correct, scope, status, created_at, updated_at)
           VALUES ('gs-old', '树立', '数理', '通用', 'confirmed', ?, ?)""",
        (utc_now(), utc_now()),
    )
    assert undo_confirm_suggestion(db, "gs-old") is True
    assert list_terms(db) == []


def test_rejected_suggestion_can_be_restored_and_confirmed(tmp_path):
    """验收：驳回后 restore，状态回到 pending，可以再次确认。"""
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    suggestion_id = add_suggestion(db, wrong="树立", correct="数理")
    headers = write_headers(client)
    assert client.post(f"/api/glossary/suggestions/{suggestion_id}/reject", headers=headers).status_code == 200
    # 待确认的不能恢复，已驳回的能
    restored = client.post(f"/api/glossary/suggestions/{suggestion_id}/restore", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["suggestion"]["status"] == "pending"
    again = client.post(f"/api/glossary/suggestions/{suggestion_id}/restore", headers=headers)
    assert again.status_code == 404
    confirmed = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/confirm",
        json={"target": "public"},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["created"] is True
    undone = client.post(f"/api/glossary/suggestions/{suggestion_id}/undo", headers=headers)
    assert undone.status_code == 200
    assert undone.json()["suggestion"]["status"] == "pending"
    assert client.get("/api/glossary/terms").json() == []
    assert client.post(f"/api/glossary/suggestions/{suggestion_id}/undo", headers=headers).status_code == 404


def test_confirm_api_rejects_unknown_target_and_extra_fields(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    suggestion_id = add_suggestion(db, wrong="树立", correct="数理")
    headers = write_headers(client)
    missing = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/confirm", json={"target": "p-x"}, headers=headers
    )
    assert missing.status_code == 404
    extra = client.post(
        f"/api/glossary/suggestions/{suggestion_id}/confirm", json={"scope": "x"}, headers=headers
    )
    assert extra.status_code == 422
    assert reject_suggestion(db, suggestion_id) is True
    assert restore_suggestion(db, suggestion_id) is True


# —— 保存纪要时直接记入 ——


def test_save_auto_records_when_correct_is_known_and_meeting_has_project(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    add_project(db, "p-yt", "云图")
    db.execute("UPDATE meetings SET project_id='p-yt' WHERE id=?", (MEETING,))
    term = create_term(db, term="数理协会", project_id="p-yt", scope="云图")
    seed_minutes(db, markdown="会上确定成立树立协会，患者随方两周。")
    create_term(db, term="随访")

    saved = client.put(
        f"/api/meetings/{MEETING}/minutes",
        json={"markdown": "会上确定成立数理协会，患者随访两周。", "base_version_id": "mv-1"},
        headers=write_headers(client),
    )
    assert saved.status_code == 200
    corrections = saved.json()["corrections"]
    assert sorted((row["wrong"], row["correct"], row["status"], row["auto_recorded"]) for row in corrections) == [
        ("树立协会", "数理协会", "confirmed", True),
        ("随方", "随访", "confirmed", True),
    ]
    assert get_term(db, term["id"])["aliases"] == ["树立协会"]
    snapshot = read_snapshot(settings.data_dir / "glossary-snapshot.json")
    assert {t["term"]: t["aliases"] for t in snapshot["terms"]} == {
        "数理协会": ["树立协会"],
        "随访": ["随方"],
    }
    # 「已记入 · 撤销」
    auto_id = next(row["id"] for row in corrections if row["correct"] == "数理协会")
    assert client.post(f"/api/glossary/suggestions/{auto_id}/undo", headers=write_headers(client)).status_code == 200
    assert get_term(db, term["id"])["aliases"] == []


def test_save_does_not_auto_record_without_project_or_when_wrong_is_a_known_name(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图", also=[{"name": "树立协会", "source": "manual"}])
    add_meeting(db)
    create_term(db, term="数理协会")
    create_term(db, term="月总")
    db.execute("UPDATE glossary_terms SET also=? WHERE term='月总'", (json.dumps(["岳总"], ensure_ascii=False),))

    # 会议没归项目：只排队
    rows = record_corrections_from_diff(
        db, "成立树立协会。", "成立数理协会。", meeting_id=MEETING
    )
    assert [(row["wrong"], row["status"], row["auto_recorded"]) for row in rows] == [
        ("树立协会", "pending", False)
    ]

    # 有项目，但错写是某个项目的叫法 / 某条词的也叫：也只排队
    db.execute("UPDATE glossary_suggestions SET status='rejected'")
    db.execute("DELETE FROM glossary_suggestions")
    db.execute("UPDATE meetings SET project_id='p-yt' WHERE id=?", (MEETING,))
    rows = record_corrections_from_diff(
        db, "成立树立协会，岳总说。", "成立数理协会，月总说。", meeting_id=MEETING
    )
    assert sorted((row["wrong"], row["auto_recorded"]) for row in rows) == [
        ("岳总", False),
        ("树立协会", False),
    ]
    assert get_term(db, list_terms(db, scope="通用")[0]["id"])["aliases"] == []


def test_record_returns_nothing_for_duplicates(tmp_path):
    db = make_db(tmp_path)
    add_meeting(db)
    first = record_corrections_from_diff(db, "成立树立协会。", "成立数理协会。", meeting_id=MEETING)
    assert len(first) == 1
    assert record_corrections_from_diff(db, "成立树立协会。", "成立数理协会。", meeting_id=MEETING) == []
    # 公共分组总在，哪怕一个词也没有
    assert list_scopes(db) == [
        {"kind": "general", "key": "通用", "label": "公共", "color": None, "count": 0}
    ]
