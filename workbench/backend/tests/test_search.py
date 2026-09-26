"""1e 检索：纪要可搜、词典同义展开、项目范围和没归项目的会、原词命中和意思相近的分开列。"""
import json

from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.main import create_app
from meeting_workbench.search import expand_query, literal_search, minutes_line_hits
from meeting_workbench.semantic import SemanticUnavailable


def make_app(tmp_path, *, semantic_enabled=False):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=semantic_enabled,
        llm_api_key_file=tmp_path / "missing-key",
    )
    app = create_app(settings)
    return app, Database(settings.database_path)


def add_project(db, project_id, name):
    db.execute(
        "INSERT INTO projects(id, name, color, created_at) VALUES (?, ?, '#123456', ?)",
        (project_id, name, utc_now()),
    )


def add_meeting(db, meeting_id, *, date, project_id=None, title="周会", segments=(), minutes=None):
    db.execute(
        """INSERT INTO meetings(id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES (?, ?, ?, 'completed_unreviewed', ?, ?, ?)""",
        (meeting_id, title, date, project_id, utc_now(), utc_now()),
    )
    version = db.create_transcript_version(meeting_id, "funasr", published=True)
    db.replace_segments(
        version,
        meeting_id,
        [
            {"id": f"{meeting_id}-s{index}", "ordinal": index, "start_ms": index * 1000,
             "end_ms": index * 1000 + 900, "text": text}
            for index, text in enumerate(segments or ["开场"])
        ],
    )
    if minutes is not None:
        db.execute(
            """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind, published, created_at)
               VALUES (?, ?, 1, ?, '', 'generated', 0, ?)""",
            (f"mv-{meeting_id}", meeting_id, minutes, utc_now()),
        )
        db.execute(
            "UPDATE meetings SET current_minutes_version_id=? WHERE id=?",
            (f"mv-{meeting_id}", meeting_id),
        )


def add_term(db, term, aliases=(), *, also=(), project_id=None, scope="通用"):
    db.execute(
        """INSERT INTO glossary_terms(id, term, aliases, also, scope, project_id, confirmed, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (f"t-{term}", term, json.dumps(list(aliases), ensure_ascii=False),
         json.dumps(list(also), ensure_ascii=False), scope, project_id, utc_now(), utc_now()),
    )


def test_words_only_in_minutes_are_found_with_the_nearest_timestamp(tmp_path):
    app, db = make_app(tmp_path)
    add_meeting(
        db, "vm-1", date="2026-09-26", segments=["大家好", "下周上线"],
        minutes="# 初审规则\n\n## 一分钟摘要\n\n- 阈值先按 0.8 执行 [00:12:34]，由月总牵头 [00:20:00]\n- 月总负责对接\n",
    )

    body = TestClient(app).get("/api/search", params={"q": "月总"}).json()

    minutes = [item for item in body["items"] if item["match_kind"] == "minutes"]
    assert [item["text"] for item in minutes] == ["阈值先按 0.8 执行，由月总牵头", "月总负责对接"]
    assert [item["start_ms"] for item in minutes] == [20 * 60 * 1000, None]
    assert minutes[0]["segment_id"] is None
    assert minutes[0]["matched"] == "月总"
    assert not [item for item in body["items"] if item["match_kind"] == "segment"]


def test_minutes_hits_follow_the_current_version_and_long_lines_are_trimmed(tmp_path):
    _app, db = make_app(tmp_path)
    long_line = "前情" * 60 + "随访方案要改" + "后话" * 60
    add_meeting(db, "vm-1", date="2026-09-26", minutes=f"## 记录\n\n{long_line}\n")
    db.execute(
        """INSERT INTO minutes_versions(id, meeting_id, version_no, markdown, html, kind, published, created_at)
           VALUES ('mv-new', 'vm-1', 2, '## 记录\n\n已经删了', '', 'draft', 0, ?)""",
        (utc_now(),),
    )
    [hit] = minutes_line_hits(long_line, "随访方案")
    assert "随访方案要改" in hit["text"]
    assert hit["text"].startswith("…") and hit["text"].endswith("…")
    assert len(hit["text"]) <= 122

    assert literal_search(db, ["随访方案"])[0]["match_kind"] == "minutes"
    db.execute("UPDATE meetings SET current_minutes_version_id='mv-new' WHERE id='vm-1'")
    assert literal_search(db, ["随访方案"]) == []


def test_search_expands_to_recorded_wrong_spellings_but_not_two_character_ones(tmp_path):
    app, db = make_app(tmp_path)
    add_term(db, "数理协会", ["树立协会"], also=["数协"])
    add_term(db, "随访", ["随方"])
    add_meeting(db, "vm-1", date="2026-09-25", segments=["会上说要成立树立协会"])
    add_meeting(db, "vm-2", date="2026-09-26", segments=["随方的时候注意", "随访两周后复核"])
    client = TestClient(app)

    body = client.get("/api/search", params={"q": "数理协会"}).json()
    assert body["expanded"] == ["树立协会"]
    assert body["expand_hints"] == ["数协"]
    assert [(item["segment_id"], item["matched"]) for item in body["items"]] == [("vm-1-s0", "树立协会")]

    short = client.get("/api/search", params={"q": "随访"}).json()
    assert short["expanded"] == []
    assert short["expand_hints"] == ["随方"]
    assert [item["segment_id"] for item in short["items"]] == ["vm-2-s1"]

    wrong = client.get("/api/search", params={"q": "树立协会"}).json()
    assert wrong["expanded"] == ["数理协会"]


def test_expansion_replaces_the_term_inside_a_longer_query(tmp_path):
    _app, db = make_app(tmp_path)
    add_term(db, "数理协会", ["树立协会"])
    add_term(db, "数理", ["树立"])

    result = expand_query(db, "树立协会的会员")

    # 长的写法优先：不会把「树立」单独换成「数理」
    assert result == {"expanded": ["数理协会的会员"], "hints": []}
    # 2 字的写法只在整个搜索词就是它时才换
    assert expand_query(db, "树立标杆") == {"expanded": [], "hints": []}
    assert expand_query(db, "树立") == {"expanded": [], "hints": ["数理"]}


def test_project_scope_uses_that_projects_terms_and_counts_unattributed_hits(tmp_path):
    app, db = make_app(tmp_path)
    add_project(db, "p-yt", "云图AI")
    add_project(db, "p-zt", "数据中台")
    add_term(db, "主数据平台", ["珠数据平台"], project_id="p-zt", scope="数据中台")
    add_meeting(db, "vm-yt", date="2026-09-24", project_id="p-yt", segments=["主数据平台的接口"])
    add_meeting(db, "vm-zt", date="2026-09-25", project_id="p-zt", segments=["珠数据平台上线"])
    add_meeting(db, "vm-none", date="2026-09-26", segments=["主数据平台排期", "主数据平台验收"])
    client = TestClient(app)

    in_zt = client.get("/api/search", params={"q": "主数据平台", "project_id": "p-zt"}).json()
    assert [item["meeting_id"] for item in in_zt["items"]] == ["vm-zt"]
    assert in_zt["expanded"] == ["珠数据平台"]
    assert in_zt["unattributed_hits"] == 2
    assert in_zt["items"][0]["project_name"] == "数据中台"

    in_yt = client.get("/api/search", params={"q": "主数据平台", "project_id": "p-yt"}).json()
    assert in_yt["expanded"] == []
    assert [item["meeting_id"] for item in in_yt["items"]] == ["vm-yt"]

    unattributed = client.get("/api/search", params={"q": "主数据平台", "project_id": "none"}).json()
    assert {item["meeting_id"] for item in unattributed["items"]} == {"vm-none"}
    assert "unattributed_hits" not in unattributed

    everything = client.get("/api/search", params={"q": "主数据平台"}).json()
    assert [item["meeting_id"] for item in everything["items"]] == ["vm-none", "vm-none", "vm-zt", "vm-yt"]
    assert "unattributed_hits" not in everything

    assert client.get("/api/search", params={"q": "主数据", "project_id": "p-gone"}).status_code == 404


def test_one_meeting_cannot_flood_the_results(tmp_path):
    app, db = make_app(tmp_path)
    add_meeting(
        db, "vm-busy", date="2026-09-26", title="灰度方案评审",
        segments=[f"灰度方案第 {index} 条" for index in range(8)],
        minutes="\n".join(f"- 灰度方案要点 {index}" for index in range(5)),
    )
    add_meeting(db, "vm-old", date="2026-09-01", segments=["灰度方案回顾"])

    items = TestClient(app).get("/api/search", params={"q": "灰度方案", "mode": "exact"}).json()["items"]

    assert [(item["meeting_id"], item["match_kind"]) for item in items] == [
        ("vm-busy", "title"),
        ("vm-busy", "minutes"),
        ("vm-busy", "minutes"),
        ("vm-busy", "segment"),
        ("vm-busy", "segment"),
        ("vm-busy", "segment"),
        ("vm-old", "segment"),
    ]


def test_hybrid_lists_similar_segments_separately_and_skips_what_is_already_listed(tmp_path, monkeypatch):
    app, db = make_app(tmp_path, semantic_enabled=True)
    add_project(db, "p-yt", "云图AI")
    add_meeting(db, "vm-1", date="2026-09-26", project_id="p-yt", segments=["随访方案发给研发", "复诊安排"])
    add_meeting(db, "vm-2", date="2026-09-25", segments=["回访计划"])
    semantic = app.state.semantic
    monkeypatch.setattr(semantic, "busy_check", lambda: False)

    def fake_search(query, limit):
        return [
            {"segment_id": "vm-1-s0", "meeting_id": "vm-1", "project_id": "p-yt", "score": 0.9, "text": "随访方案发给研发"},
            {"segment_id": "vm-1-s1", "meeting_id": "vm-1", "project_id": "p-yt", "score": 0.7, "text": "复诊安排"},
            {"segment_id": "vm-2-s0", "meeting_id": "vm-2", "project_id": None, "score": 0.6, "text": "回访计划"},
            {"segment_id": "vm-x", "meeting_id": "vm-2", "project_id": None, "score": 0.2, "text": "无关"},
        ]

    monkeypatch.setattr(semantic, "search", fake_search)
    client = TestClient(app)

    body = client.get("/api/search", params={"q": "随访方案"}).json()
    assert [item["segment_id"] for item in body["items"]] == ["vm-1-s0"]
    assert [item["segment_id"] for item in body["similar"]] == ["vm-1-s1", "vm-2-s0"]
    assert "semantic_unavailable" not in body

    scoped = client.get("/api/search", params={"q": "随访方案", "project_id": "p-yt"}).json()
    assert [item["segment_id"] for item in scoped["similar"]] == ["vm-1-s1"]

    exact = client.get("/api/search", params={"q": "随访方案", "mode": "exact"}).json()
    assert "similar" not in exact


def test_search_still_answers_while_transcribing_or_without_the_model(tmp_path, monkeypatch):
    app, db = make_app(tmp_path, semantic_enabled=True)
    add_meeting(db, "vm-1", date="2026-09-26", segments=["随访方案发给研发"])
    semantic = app.state.semantic
    calls = []
    monkeypatch.setattr(semantic, "busy_check", lambda: True)
    monkeypatch.setattr(semantic, "search", lambda query, limit: calls.append(query) or [])
    client = TestClient(app)

    busy = client.get("/api/search", params={"q": "随访方案"})
    assert busy.status_code == 200
    assert busy.json()["semantic_unavailable"] == "正在转写，意思相近的结果等转写完再搜"
    assert [item["segment_id"] for item in busy.json()["items"]] == ["vm-1-s0"]
    assert calls == []

    def missing_model(query, limit):
        raise SemanticUnavailable("本地语义模型依赖尚未安装")

    monkeypatch.setattr(semantic, "busy_check", lambda: False)
    monkeypatch.setattr(semantic, "search", missing_model)
    unavailable = client.get("/api/search", params={"q": "随访方案"}).json()
    assert unavailable["semantic_unavailable"] == "本地语义模型依赖尚未安装"
    assert unavailable["similar"] == []


def test_semantic_off_means_no_similar_section_and_no_warning(tmp_path):
    app, db = make_app(tmp_path)
    add_meeting(db, "vm-1", date="2026-09-26", segments=["随访方案发给研发"])

    body = TestClient(app).get("/api/search", params={"q": "随访方案"}).json()

    assert body["mode"] == "hybrid"
    assert body["similar"] == []
    assert "semantic_unavailable" not in body
