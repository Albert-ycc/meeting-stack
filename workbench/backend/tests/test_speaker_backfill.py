import json

from meeting_workbench.db import Database, utc_now
from meeting_workbench.speaker_backfill import apply_speaker_labels_with_connection


def _seed_meeting_with_segments(db, meeting_id, texts):
    db.execute(
        """INSERT INTO meetings (id, title, status, created_at, updated_at)
           VALUES (?, ?, 'completed_unreviewed', ?, ?)""",
        (meeting_id, meeting_id, utc_now(), utc_now()),
    )
    version_id = db.create_transcript_version(meeting_id, "funasr", published=True)
    db.replace_segments(
        version_id,
        meeting_id,
        [
            {"ordinal": index, "start_ms": index * 1000, "end_ms": index * 1000 + 500, "text": text}
            for index, text in enumerate(texts)
        ],
    )
    return version_id


def _register_funasr_artifact(db, meeting_id, path):
    stat = path.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES (?, 'funasr_json', 'source', 'archive', ?, ?, ?, ?)""",
        (meeting_id, str(path), stat.st_size, stat.st_mtime_ns, utc_now()),
    )


def test_apply_speaker_labels_updates_segments_and_speakers(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_id = "vm-20260101-120000"
    version_id = _seed_meeting_with_segments(db, meeting_id, ["第一句", "第二句"])
    json_path = tmp_path / f"{meeting_id}.funasr.json"
    json_path.write_text(
        json.dumps(
            {
                "sentence_info": [
                    {"start": 0, "end": 500, "spk": 0, "text": "第一句"},
                    {"start": 1000, "end": 1500, "spk": 1, "text": "第二句"},
                ]
            }
        ),
        encoding="utf-8",
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    with db.transaction() as connection:
        result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)

    assert result.applied
    assert result.updated_segments == 2
    assert result.speaker_count == 2
    segments = db.query_all(
        "SELECT ordinal, speaker_label FROM segments WHERE version_id=? ORDER BY ordinal",
        (version_id,),
    )
    assert [row["speaker_label"] for row in segments] == ["SPEAKER_00", "SPEAKER_01"]
    speakers = {
        row["label"] for row in db.query_all("SELECT label FROM speakers WHERE meeting_id=?", (meeting_id,))
    }
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}


def test_apply_speaker_labels_skips_on_text_mismatch(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_id = "vm-20260101-130000"
    version_id = _seed_meeting_with_segments(db, meeting_id, ["真实文本"])
    json_path = tmp_path / f"{meeting_id}.funasr.json"
    json_path.write_text(
        json.dumps({"sentence_info": [{"start": 0, "end": 500, "spk": 0, "text": "不同文本"}]}),
        encoding="utf-8",
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    with db.transaction() as connection:
        result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)

    assert not result.applied
    assert "文本" in result.skip_reason
    segments = db.query_all("SELECT speaker_label FROM segments WHERE version_id=?", (version_id,))
    assert all(row["speaker_label"] is None for row in segments)


def test_apply_speaker_labels_skips_on_segment_count_mismatch(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_id = "vm-20260101-140000"
    version_id = _seed_meeting_with_segments(db, meeting_id, ["只有一句"])
    json_path = tmp_path / f"{meeting_id}.funasr.json"
    json_path.write_text(
        json.dumps(
            {
                "sentence_info": [
                    {"start": 0, "end": 500, "spk": 0, "text": "只有一句"},
                    {"start": 600, "end": 900, "spk": 1, "text": "多出来的一句"},
                ]
            }
        ),
        encoding="utf-8",
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    with db.transaction() as connection:
        result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)

    assert not result.applied
    assert "段数不一致" in result.skip_reason


def test_apply_speaker_labels_skips_on_partial_spk_coverage(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_id = "vm-20260101-150000"
    version_id = _seed_meeting_with_segments(db, meeting_id, ["有标签的句子", "没标签的句子"])
    json_path = tmp_path / f"{meeting_id}.funasr.json"
    json_path.write_text(
        json.dumps(
            {
                "sentence_info": [
                    {"start": 0, "end": 500, "spk": 0, "text": "有标签的句子"},
                    {"start": 600, "end": 900, "text": "没标签的句子"},
                ]
            }
        ),
        encoding="utf-8",
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    with db.transaction() as connection:
        result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)

    assert not result.applied
    assert "覆盖率" in result.skip_reason


def test_apply_speaker_labels_skips_when_only_degraded_json_available(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_id = "vm-20260101-160000"
    version_id = _seed_meeting_with_segments(db, meeting_id, ["一句话"])
    degraded_dir = tmp_path / "rerun"
    degraded_dir.mkdir()
    json_path = degraded_dir / f"{meeting_id}.funasr.json"
    json_path.write_text(
        json.dumps({"sentence_info": [{"start": 0, "end": 500, "spk": 0, "text": "一句话"}]}),
        encoding="utf-8",
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    with db.transaction() as connection:
        result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)

    assert not result.applied
    assert result.skip_reason == "没有非 degraded 的 funasr_json 源文件"


def test_apply_speaker_labels_preserves_user_renamed_speaker_on_rerun(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    meeting_id = "vm-20260101-170000"
    version_id = _seed_meeting_with_segments(db, meeting_id, ["一句话"])
    json_path = tmp_path / f"{meeting_id}.funasr.json"
    json_path.write_text(
        json.dumps({"sentence_info": [{"start": 0, "end": 500, "spk": 0, "text": "一句话"}]}),
        encoding="utf-8",
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    with db.transaction() as connection:
        apply_speaker_labels_with_connection(connection, meeting_id, version_id)
    db.execute(
        "UPDATE speakers SET display_name=? WHERE meeting_id=? AND label=?",
        ("张三", meeting_id, "SPEAKER_00"),
    )

    # 幂等重跑（例如脚本或钩子被再次触发）不能把用户已经改好的名字冲掉。
    with db.transaction() as connection:
        result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)

    assert result.applied
    speaker = db.query_one(
        "SELECT display_name FROM speakers WHERE meeting_id=? AND label=?",
        (meeting_id, "SPEAKER_00"),
    )
    assert speaker["display_name"] == "张三"
