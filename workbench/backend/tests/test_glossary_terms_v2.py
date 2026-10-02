"""1d-1b 词条：重名 409 与合并、也叫、分组排序、快照可选字段、项目页词条上限。"""

import threading
import time

import pytest

from meeting_workbench import glossary

from meeting_workbench.db import Database, utc_now
from meeting_workbench.glossary import (
    DuplicateTermError,
    GlossaryError,
    confirm_suggestion,
    create_term,
    list_scopes,
    merge_into_term,
    read_snapshot,
    update_term,
)

from .test_glossary import make_client, write_headers


def make_db(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    return db


def add_project(db, project_id, name):
    db.execute(
        "INSERT INTO projects (id, name, color, origin, created_at) VALUES (?, ?, '#667085', 'manual', ?)",
        (project_id, name, utc_now()),
    )


def add_meeting(db, meeting_id, project_id, recording_date):
    db.execute(
        """INSERT INTO meetings (id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES (?, '周会', ?, 'completed_unreviewed', ?, ?, ?)""",
        (meeting_id, recording_date, project_id, utc_now(), utc_now()),
    )


# —— 重名 ——


def test_duplicate_term_conflict_says_where_it_lives(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    public = create_term(db, term="随访", aliases=["随方", "随仿"])
    project = create_term(db, term="CRF", project_id="p-yt", scope="云图", also=["病例报告表"])

    with pytest.raises(DuplicateTermError) as public_error:
        create_term(db, term="随访", aliases=["岁访"])
    assert str(public_error.value) == "「随访」已在 公共 词典"
    assert public_error.value.conflict == {
        "term_id": public["id"],
        "term": "随访",
        "project_id": None,
        "project_name": None,
        "aliases": ["随方", "随仿"],
        "also": [],
    }

    with pytest.raises(DuplicateTermError) as project_error:
        update_term(db, public["id"], term="CRF")
    assert str(project_error.value) == "「CRF」已在 云图 项目"
    assert project_error.value.conflict["term_id"] == project["id"]
    assert project_error.value.conflict["also"] == ["病例报告表"]


def test_duplicate_term_api_returns_409_with_conflict_and_merge_makes_public(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    add_project(db, "p-yt", "云图")
    headers = write_headers(client)
    created = client.post(
        "/api/glossary/terms",
        json={"term": "CRF", "aliases": ["CFR"], "project_id": "p-yt"},
        headers=headers,
    ).json()

    duplicate = client.post(
        "/api/glossary/terms", json={"term": "CRF", "aliases": ["CRV"]}, headers=headers
    )
    assert duplicate.status_code == 409
    body = duplicate.json()
    assert body["detail"] == "「CRF」已在 云图 项目"
    assert body["conflict"]["term_id"] == created["id"]
    assert body["conflict"]["project_name"] == "云图"
    assert body["conflict"]["aliases"] == ["CFR"]

    # ［改成公共词并合并错写］
    merged = client.post(
        f"/api/glossary/terms/{created['id']}/merge",
        json={"aliases": ["CRV", "CFR"], "also": ["病例报告表"], "make_public": True},
        headers=headers,
    )
    assert merged.status_code == 200
    term = merged.json()
    assert (term["project_id"], term["scope"]) == (None, "通用")
    assert term["aliases"] == ["CFR", "CRV"]
    assert term["also"] == ["病例报告表"]
    snapshot = read_snapshot(settings.data_dir / "glossary-snapshot.json")
    assert snapshot["terms"] == [
        {
            "term": "CRF",
            "aliases": ["CFR", "CRV"],
            "scope": "通用",
            "category": "其他",
            "also": ["病例报告表"],
        }
    ]
    assert (
        client.post(
            "/api/glossary/terms/gt-missing/merge", json={"aliases": ["CRV"]}, headers=headers
        ).status_code
        == 404
    )

    renamed = client.post("/api/glossary/terms", json={"term": "随访"}, headers=headers).json()
    clash = client.put(
        f"/api/glossary/terms/{renamed['id']}", json={"term": "CRF"}, headers=headers
    )
    assert clash.status_code == 409
    assert clash.json()["conflict"]["project_id"] is None


def test_merge_keeps_project_unless_asked(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    term = create_term(db, term="初审规则", project_id="p-yt", scope="云图")
    # ［加到 云图 那条］
    merged = merge_into_term(db, term["id"], aliases=["出审规则", "初审规则"])
    assert (merged["project_id"], merged["scope"]) == ("p-yt", "云图")
    # 和正确写法相同的错写直接略过
    assert merged["aliases"] == ["出审规则"]


# —— 也叫 ——


def test_term_also_validation(tmp_path):
    db = make_db(tmp_path)
    create_term(db, term="数理协会", aliases=["树立协会"], also=["数理会"])
    for bad, message in (
        (["云"], "叫法要 2–20 个字"),
        (["20260926"], "叫法要 2–20 个字"),
        (["方案"], "「方案」太常见"),
        (["数理会"], "已经用在词条「数理协会」上了"),
        (["树立协会"], "已经用在词条「数理协会」上了"),
        (["数理协会"], "已经用在词条「数理协会」上了"),
    ):
        with pytest.raises(GlossaryError) as error:
            create_term(db, term="中华数理学会", also=bad)
        assert message in str(error.value)
    with pytest.raises(GlossaryError) as overlap:
        create_term(db, term="随访", aliases=["随方"], also=["随方"])
    assert "不能既是错写又是叫法" in str(overlap.value)

    created = create_term(db, term="病例报告表", also=["CRF", "病例报告表", "CRF"])
    # 和自己同名的叫法、重复的叫法略过
    assert created["also"] == ["CRF"]
    updated = update_term(db, created["id"], also=["CRF", "病例表"])
    assert updated["also"] == ["CRF", "病例表"]
    with pytest.raises(GlossaryError):
        update_term(db, created["id"], aliases=["病例表"])


def test_wrong_cannot_be_another_terms_spelling(tmp_path):
    """错写不能是别的词条的正确写法或叫法：不然纪要体检把正确的名字当「可能漏纠」，还会被自动改错。"""
    db = make_db(tmp_path)
    create_term(db, term="张三", category="人名", also=["三哥"])
    for bad in ("张三", "三哥"):
        with pytest.raises(GlossaryError) as error:
            create_term(db, term="张珊", aliases=[bad], category="人名")
        assert f"「{bad}」已经是词条「张三」的写法，不能当错写" == str(error.value)
    shan = create_term(db, term="张珊", aliases=["张山"], category="人名")
    with pytest.raises(GlossaryError):
        update_term(db, shan["id"], aliases=["张山", "张三"])
    with pytest.raises(GlossaryError):
        merge_into_term(db, shan["id"], aliases=["张三"])
    assert (
        db.query_one("SELECT aliases FROM glossary_terms WHERE id=?", (shan["id"],))["aliases"]
        == '["张山"]'
    )


def test_wrong_may_match_a_term_of_another_scope(tmp_path):
    """项目词只在自己项目的会里生效：公共词的错写撞上项目词照旧允许（在那个项目里避让），
    别的项目的词也不拦；项目词的错写撞上公共词、本项目的词要拦。"""
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图AI")
    add_project(db, "p-zt", "数据中台")
    create_term(db, term="树立", project_id="p-yt", scope="云图AI")
    assert create_term(db, term="数理", aliases=["树立"])["aliases"] == ["树立"]
    assert create_term(db, term="竖立", aliases=["树立"], project_id="p-zt", scope="数据中台")
    for project_id, scope, wrong in (("p-zt", "数据中台", "数理"), ("p-yt", "云图AI", "树立")):
        with pytest.raises(GlossaryError):
            create_term(db, term="述理", aliases=[wrong], project_id=project_id, scope=scope)


def test_old_wrong_that_clashes_does_not_block_other_edits(tmp_path):
    """词典里已经有撞名的旧错写（校验加上以前存的）：改别的字段、加别的错写、删错写都照样保存。"""
    db = make_db(tmp_path)
    create_term(db, term="张三", category="人名")
    shan = create_term(db, term="张珊", aliases=["张山"], category="人名")
    db.execute('UPDATE glossary_terms SET aliases=\'["张山", "张三"]\' WHERE id=?', (shan["id"],))
    assert update_term(db, shan["id"], category="其他")["category"] == "其他"
    assert update_term(db, shan["id"], also=["珊姐"])["also"] == ["珊姐"]
    assert update_term(db, shan["id"], term="张珊珊")["term"] == "张珊珊"
    assert update_term(db, shan["id"], aliases=["张山", "张三", "张杉"])["aliases"] == [
        "张山",
        "张三",
        "张杉",
    ]
    assert update_term(db, shan["id"], aliases=["张三"])["aliases"] == ["张三"]
    assert update_term(db, shan["id"], aliases=[])["aliases"] == []


def test_confirming_a_suggestion_whose_wrong_is_another_terms_spelling_is_refused(tmp_path):
    db = make_db(tmp_path)
    create_term(db, term="张三", category="人名")
    shan = create_term(db, term="张珊", category="人名")
    for suggestion_id, correct in (("gs-1", "张珊"), ("gs-2", "章珊")):
        db.execute(
            """INSERT INTO glossary_suggestions (id, wrong, correct, scope, status, created_at, updated_at)
               VALUES (?, '张三', ?, '通用', 'pending', ?, ?)""",
            (suggestion_id, correct, utc_now(), utc_now()),
        )
        with pytest.raises(GlossaryError):
            confirm_suggestion(db, suggestion_id)
        status = db.query_one(
            "SELECT status FROM glossary_suggestions WHERE id=?", (suggestion_id,)
        )
        assert status["status"] == "pending"
    assert (
        db.query_one("SELECT aliases FROM glossary_terms WHERE id=?", (shan["id"],))["aliases"]
        == "[]"
    )
    assert db.query_one("SELECT 1 AS x FROM glossary_terms WHERE term='章珊'") is None


def test_rename_racing_another_writer_is_a_duplicate_not_an_integrity_error(tmp_path, monkeypatch):
    """改名的重名检查通过以后、写库以前，别的请求抢先建了同名词条：应回和普通重名一样的
    DuplicateTermError（接口 409），不是撞 term UNIQUE 的 IntegrityError（500）。"""
    db = make_db(tmp_path)
    term = create_term(db, term="甲方")
    checked = threading.Event()
    real = glossary._raise_duplicate

    def slow_check(connection, text):
        real(connection, text)
        if threading.current_thread().name == "rename":
            checked.set()
            time.sleep(0.5)

    monkeypatch.setattr(glossary, "_raise_duplicate", slow_check)
    outcome = {}

    def rename():
        try:
            outcome["rename"] = update_term(db, term["id"], term="乙方")["term"]
        except Exception as error:  # noqa: BLE001
            outcome["rename"] = type(error).__name__

    thread = threading.Thread(target=rename, name="rename")
    thread.start()
    assert checked.wait(5)
    try:
        create_term(db, term="乙方")
        outcome["create"] = "ok"
    except Exception as error:  # noqa: BLE001
        outcome["create"] = type(error).__name__
    thread.join(10)
    # 改名先拿到写锁，建词条等它提交以后才查重
    assert outcome == {"rename": "乙方", "create": "DuplicateTermError"}


# —— 分组 ——


def test_scopes_list_public_then_all_projects_by_latest_meeting(tmp_path):
    db = make_db(tmp_path)
    for project_id, name in (
        ("p-a", "阿尔法"),
        ("p-b", "贝塔"),
        ("p-c", "C 项目"),
        ("p-d", "D 项目"),
    ):
        add_project(db, project_id, name)
    add_meeting(db, "m-1", "p-a", "2026-09-01")
    add_meeting(db, "m-2", "p-b", "2026-09-20")
    add_meeting(db, "m-3", "p-a", "2026-09-10")
    create_term(db, term="初审规则", project_id="p-a", scope="阿尔法")

    scopes = list_scopes(db)
    assert [(item["kind"], item["label"], item["count"]) for item in scopes] == [
        ("general", "公共", 0),
        ("project", "贝塔", 0),
        ("project", "阿尔法", 1),
        # 没开过会的按名字排在最后
        ("project", "C 项目", 0),
        ("project", "D 项目", 0),
    ]
    assert scopes[2]["last_meeting_at"] == "2026-09-10"

    # 旧分组还在（整理被撤销）时才出现桶
    create_term(db, term="样品发放", scope="启航")
    assert list_scopes(db)[-1] == {
        "kind": "bucket",
        "key": "启航",
        "label": "启航",
        "color": None,
        "count": 1,
    }


# —— 快照 ——


def test_snapshot_exports_optional_project_id_and_also(tmp_path):
    db = make_db(tmp_path)
    add_project(db, "p-yt", "云图")
    snapshot = tmp_path / "snap.json"
    create_term(db, term="数理协会", aliases=["树立协会"], snapshot_path=snapshot)
    create_term(
        db,
        term="初审规则",
        project_id="p-yt",
        scope="云图",
        also=["初审"],
        snapshot_path=snapshot,
    )
    payload = read_snapshot(snapshot)
    assert payload["schema_version"] == 1
    assert payload["terms"] == [
        {
            "term": "初审规则",
            "aliases": [],
            "scope": "云图",
            "category": "其他",
            "project_id": "p-yt",
            "also": ["初审"],
        },
        {"term": "数理协会", "aliases": ["树立协会"], "scope": "通用", "category": "其他"},
    ]


# —— 项目页 ——


def test_project_board_lists_up_to_fifty_terms_with_total(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    headers = write_headers(client)
    project_id = client.post(
        "/api/projects", json={"name": "云图", "color": "#2c8d83"}, headers=headers
    ).json()["id"]
    for index in range(55):
        create_term(db, term=f"项目词{index:02d}", project_id=project_id, scope="云图")
    create_term(db, term="数理协会")
    create_term(db, term="随访")

    board = client.get(f"/api/projects/{project_id}/board").json()
    assert board["glossary_count"] == 55
    assert len(board["glossary_terms"]) == 50
    assert board["glossary_terms"][0]["term"] == "项目词54"
    assert board["glossary_terms"][0]["also"] == []
    assert board["glossary_terms"][0]["is_cue"] is True
    assert board["public_glossary_count"] == 2
