"""检索只认当前逐字稿，标题命中带当前逐字稿第一段当锚点。

这两条以前由 Database.exact_search 的用例守着。那个方法生产里没有调用方（检索走
search.literal_search），连同用例删掉后，改在真正的检索上钉住。
"""

from meeting_workbench.db import Database
from meeting_workbench.search import literal_search


def test_segment_hits_come_from_the_current_transcript_version_only(tmp_path):
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

    assert [item["segment_id"] for item in literal_search(db, ["当前工作版本"])] == ["new"]
    assert literal_search(db, ["历史版本"]) == []


def test_title_hit_carries_the_first_segment_of_the_current_transcript(tmp_path):
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

    [hit] = literal_search(db, ["云图规则"])

    assert hit["match_kind"] == "title"
    assert hit["meeting_id"] == "vm-title"
    assert hit["title"] == "云图规则评审会"
    assert (hit["segment_id"], hit["start_ms"], hit["end_ms"], hit["text"]) == (
        "seg-title",
        2300,
        4100,
        "正文没有标题词",
    )
