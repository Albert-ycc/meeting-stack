from meeting_workbench.db import Database
from meeting_workbench.config import Settings
from meeting_workbench.main import create_app
from fastapi.testclient import TestClient


def test_trigram_fts_returns_chinese_sentence_and_anchor(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, ?, ?)",
        ("vm-123", "项目复盘会", "completed_unreviewed"),
    )
    version_id = db.create_transcript_version("vm-123", "funasr", published=True)
    db.replace_segments(
        version_id,
        "vm-123",
        [
            {
                "id": "segment-1",
                "ordinal": 0,
                "start_ms": 12_340,
                "end_ms": 18_000,
                "speaker_label": "SPEAKER_00",
                "speaker_name": "张三",
                "text": "下一步先把随访方案发给研发确认。",
            }
        ],
    )

    results = db.exact_search("随访方案")

    assert results[0]["meeting_id"] == "vm-123"
    assert results[0]["segment_id"] == "segment-1"
    assert results[0]["start_ms"] == 12_340
    assert "随访方案" in results[0]["text"]


def test_search_uses_latest_working_version_only(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES (?, ?, ?)",
        ("vm-456", "版本会", "draft_modified"),
    )
    old_version = db.create_transcript_version("vm-456", "funasr", published=True)
    db.replace_segments(
        old_version,
        "vm-456",
        [{"id": "old", "ordinal": 0, "start_ms": 0, "end_ms": 1000, "text": "历史版本关键词"}],
    )
    draft = db.create_transcript_version("vm-456", "draft", published=False)
    db.replace_segments(
        draft,
        "vm-456",
        [{"id": "new", "ordinal": 0, "start_ms": 0, "end_ms": 1000, "text": "当前工作版本关键词"}],
    )

    assert db.exact_search("当前工作版本")
    assert db.exact_search("历史版本") == []


def test_exact_search_matches_title_and_returns_a_current_segment_anchor(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-title', '云图规则评审会', 'published')"
    )
    version = db.create_transcript_version("vm-title", "funasr", published=True)
    db.replace_segments(
        version,
        "vm-title",
        [{"id": "seg-title", "start_ms": 2300, "end_ms": 4100, "text": "正文没有标题词"}],
    )

    result = db.exact_search("云图规则")

    assert result == [
        {
            "segment_id": "seg-title",
            "meeting_id": "vm-title",
            "title": "云图规则评审会",
            "canonical_dir": None,
            "recording_date": None,
            "match_kind": "title",
            "start_ms": 2300,
            "end_ms": 4100,
            "speaker_name": None,
            "speaker_label": None,
            "text": "正文没有标题词",
        }
    ]


def test_exact_search_marks_segment_matches(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-segment', '普通会议', 'published')"
    )
    version = db.create_transcript_version("vm-segment", "funasr", published=True)
    db.replace_segments(
        version,
        "vm-segment",
        [{"id": "seg-match", "start_ms": 1200, "end_ms": 2300, "text": "正文关键词"}],
    )

    result = db.exact_search("正文关键词")

    assert result[0]["match_kind"] == "segment"


def test_meeting_list_returns_filtered_total_and_project_counts(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    app = create_app(settings)
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO projects(id, name, color, created_at) VALUES ('project-a', '项目A', '#123456', CURRENT_TIMESTAMP)"
    )
    for index in range(3):
        db.execute(
            "INSERT INTO meetings(id, title, status, project_id) VALUES (?, ?, 'published', ?)",
            (f"vm-page-{index}", f"分页会议 {index}", "project-a" if index < 2 else None),
        )
    client = TestClient(app)

    page = client.get("/api/meetings", params={"limit": 1, "offset": 1, "q": "分页"}).json()
    projects = client.get("/api/projects").json()

    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert projects[0]["meeting_count"] == 2


def test_done_status_filter_covers_every_completed_state(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    app = create_app(settings)
    db = Database(settings.database_path)
    for index, status in enumerate(
        ["completed_unreviewed", "draft_modified", "published", "failed"]
    ):
        db.execute(
            "INSERT INTO meetings(id, title, status) VALUES (?, ?, ?)",
            (f"vm-state-{index}", f"状态会议 {index}", status),
        )
    client = TestClient(app)

    done = client.get("/api/meetings", params={"status": "done"}).json()
    failed = client.get("/api/meetings", params={"status": "failed"}).json()
    legacy = client.get("/api/meetings", params={"status": "published"}).json()

    assert done["total"] == 3
    assert failed["total"] == 1
    assert legacy["total"] == 1


def test_invalid_source_end_time_is_clamped_to_start(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-time', '时间修复', 'completed_unreviewed')"
    )
    version = db.create_transcript_version("vm-time", "funasr", published=True)

    db.replace_segments(
        version,
        "vm-time",
        [{"id": "bad-time", "start_ms": 5000, "end_ms": 4000, "text": "时间异常"}],
    )

    row = db.query_one("SELECT start_ms, end_ms FROM segments WHERE id='bad-time'")
    assert row == {"start_ms": 5000, "end_ms": 5000}


def test_meeting_text_filters_treat_like_metacharacters_as_literals(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    app = create_app(settings)
    db = Database(settings.database_path)
    for meeting_id, title, speaker in (
        ("vm-percent", "完成 100%", "产品%经理"),
        ("vm-underscore", "alpha_beta", "研发_一组"),
        ("vm-backslash", r"C:\meeting", r"客户\代表"),
        ("vm-plain", "普通会议", "普通参会人"),
    ):
        db.execute(
            "INSERT INTO meetings(id, title, status) VALUES (?, ?, 'published')",
            (meeting_id, title),
        )
        db.execute(
            "INSERT INTO speakers(id, meeting_id, label, display_name) VALUES (?, ?, 'SPEAKER_00', ?)",
            (f"speaker-{meeting_id}", meeting_id, speaker),
        )
    client = TestClient(app)

    for query, expected in (("%", "vm-percent"), ("_", "vm-underscore"), ("\\", "vm-backslash")):
        q_ids = {
            item["id"] for item in client.get("/api/meetings", params={"q": query}).json()["items"]
        }
        participant_ids = {
            item["id"]
            for item in client.get("/api/meetings", params={"participant": query}).json()["items"]
        }

        assert q_ids == {expected}
        assert participant_ids == {expected}
