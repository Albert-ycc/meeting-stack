"""1d-1b 词条：重名 409 与合并、也叫、分组排序、快照可选字段、项目页词条上限。"""
import pytest

from meeting_workbench.db import Database, utc_now
from meeting_workbench.glossary import (
    DuplicateTermError,
    GlossaryError,
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
    assert client.post(
        "/api/glossary/terms/gt-missing/merge", json={"aliases": ["CRV"]}, headers=headers
    ).status_code == 404

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


# —— 分组 ——


def test_scopes_list_public_then_all_projects_by_latest_meeting(tmp_path):
    db = make_db(tmp_path)
    for project_id, name in (("p-a", "阿尔法"), ("p-b", "贝塔"), ("p-c", "C 项目"), ("p-d", "D 项目")):
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
