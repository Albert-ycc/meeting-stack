import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_speakers  # noqa: E402  (只能在 sys.path 调整之后导入)

from meeting_workbench.config import Settings
from meeting_workbench.db import utc_now


def _settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        relay_jobs_db=tmp_path / "relay-jobs.sqlite3",
    )


def _seed_meeting(db, meeting_id, texts, *, status="completed_unreviewed"):
    db.execute(
        """INSERT INTO meetings (id, title, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (meeting_id, meeting_id, status, utc_now(), utc_now()),
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


def _write_funasr_json(tmp_path, meeting_id, sentences):
    path = tmp_path / f"{meeting_id}.funasr.json"
    path.write_text(json.dumps({"sentence_info": sentences}), encoding="utf-8")
    return path


def test_human_review_reason_flags_draft_kind(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-120000"
    _seed_meeting(db, meeting_id, ["一句话"])
    db.execute(
        "UPDATE transcript_versions SET kind='draft' WHERE meeting_id=?", (meeting_id,)
    )

    reason = backfill_speakers._human_review_reason(db, meeting_id)

    assert reason == "current_version_kind_draft"


def test_human_review_reason_flags_draft_modified_status(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-130000"
    _seed_meeting(db, meeting_id, ["一句话"], status="draft_modified")

    reason = backfill_speakers._human_review_reason(db, meeting_id)

    assert reason == "status_draft_modified"


def test_human_review_reason_flags_open_external_source_change(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-140000"
    _seed_meeting(db, meeting_id, ["一句话"])
    db.conflicts.open(meeting_id, "external_source_change", source_signature="sig")

    reason = backfill_speakers._human_review_reason(db, meeting_id)

    assert reason == "open_external_source_change"


def test_human_review_reason_is_none_for_healthy_meeting(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-150000"
    _seed_meeting(db, meeting_id, ["一句话"])

    assert backfill_speakers._human_review_reason(db, meeting_id) is None


def test_process_meeting_dry_run_does_not_write(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-160000"
    version_id = _seed_meeting(db, meeting_id, ["第一句", "第二句"])
    json_path = _write_funasr_json(
        tmp_path,
        meeting_id,
        [
            {"start": 0, "end": 500, "spk": 0, "text": "第一句"},
            {"start": 1000, "end": 1500, "spk": 1, "text": "第二句"},
        ],
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    outcome = backfill_speakers._process_meeting(db, meeting_id, dry_run=True)

    assert outcome.outcome == "applied"
    assert outcome.updated_segments == 2
    segments = db.query_all(
        "SELECT speaker_label FROM segments WHERE version_id=?", (version_id,)
    )
    assert all(row["speaker_label"] is None for row in segments)
    assert db.query_all("SELECT 1 FROM speakers WHERE meeting_id=?", (meeting_id,)) == []
    assert db.query_all(
        "SELECT 1 FROM events WHERE meeting_id=? AND event_type='speaker_backfill'", (meeting_id,)
    ) == []


def test_process_meeting_writes_labels_and_versioned_event(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-170000"
    version_id = _seed_meeting(db, meeting_id, ["第一句", "第二句"])
    json_path = _write_funasr_json(
        tmp_path,
        meeting_id,
        [
            {"start": 0, "end": 500, "spk": 0, "text": "第一句"},
            {"start": 1000, "end": 1500, "spk": 1, "text": "第二句"},
        ],
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    outcome = backfill_speakers._process_meeting(db, meeting_id, dry_run=False)

    assert outcome.outcome == "applied"
    segments = db.query_all(
        "SELECT ordinal, speaker_label FROM segments WHERE version_id=? ORDER BY ordinal",
        (version_id,),
    )
    assert [row["speaker_label"] for row in segments] == ["SPEAKER_00", "SPEAKER_01"]
    event = db.query_one(
        "SELECT payload_json FROM events WHERE meeting_id=? AND event_type='speaker_backfill'",
        (meeting_id,),
    )
    payload = json.loads(event["payload_json"])
    assert payload["script_version"] == backfill_speakers.SCRIPT_VERSION
    assert payload["updated_segments"] == 2


def test_process_meeting_skips_rerun_when_already_done_with_current_script_version(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-180000"
    _seed_meeting(db, meeting_id, ["一句话"])
    json_path = _write_funasr_json(
        tmp_path, meeting_id, [{"start": 0, "end": 500, "spk": 0, "text": "一句话"}]
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    first = backfill_speakers._process_meeting(db, meeting_id, dry_run=False)
    assert first.outcome == "applied"

    second = backfill_speakers._process_meeting(db, meeting_id, dry_run=False)

    assert second.outcome == "already_done"


def test_process_meeting_forces_rerun_on_partial_coverage_even_with_prior_event(tmp_path):
    # 半场态（0 < 带标段数 < 总段数）必须重跑，不能被「事件已存在」的幂等跳过盖住——
    # 否则未来 parser 再修复一次，旧半场数据永远进不去。
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)
    meeting_id = "vm-20260101-190000"
    version_id = _seed_meeting(db, meeting_id, ["第一句", "第二句"])
    db.execute(
        "UPDATE segments SET speaker_label='SPEAKER_00' WHERE version_id=? AND ordinal=0",
        (version_id,),
    )
    db.add_event(
        "speaker_backfill",
        meeting_id=meeting_id,
        payload={"script_version": backfill_speakers.SCRIPT_VERSION},
    )
    json_path = _write_funasr_json(
        tmp_path,
        meeting_id,
        [
            {"start": 0, "end": 500, "spk": 0, "text": "第一句"},
            {"start": 1000, "end": 1500, "spk": 1, "text": "第二句"},
        ],
    )
    _register_funasr_artifact(db, meeting_id, json_path)

    outcome = backfill_speakers._process_meeting(db, meeting_id, dry_run=False)

    assert outcome.outcome == "applied"
    segments = db.query_all(
        "SELECT speaker_label FROM segments WHERE version_id=? ORDER BY ordinal", (version_id,)
    )
    assert [row["speaker_label"] for row in segments] == ["SPEAKER_00", "SPEAKER_01"]


def test_run_excludes_human_review_meetings_and_lists_hash_bound_minutes(tmp_path):
    settings = _settings(tmp_path)
    db = backfill_speakers._database(settings)

    healthy_id = "vm-20260101-200000"
    _seed_meeting(db, healthy_id, ["一句话"])
    _register_funasr_artifact(
        db,
        healthy_id,
        _write_funasr_json(
            tmp_path, healthy_id, [{"start": 0, "end": 500, "spk": 0, "text": "一句话"}]
        ),
    )

    draft_id = "vm-20260101-210000"
    _seed_meeting(db, draft_id, ["草稿会议"])
    db.execute("UPDATE transcript_versions SET kind='draft' WHERE meeting_id=?", (draft_id,))
    _register_funasr_artifact(
        db,
        draft_id,
        _write_funasr_json(
            tmp_path, draft_id, [{"start": 0, "end": 500, "spk": 0, "text": "草稿会议"}]
        ),
    )

    hash_bound_id = "vm-20260101-220000"
    _seed_meeting(db, hash_bound_id, ["绑定哈希的会议"])
    _register_funasr_artifact(
        db,
        hash_bound_id,
        _write_funasr_json(
            tmp_path, hash_bound_id, [{"start": 0, "end": 500, "spk": 0, "text": "绑定哈希的会议"}]
        ),
    )
    minutes_id = "mv-hash-bound"
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, kind, published, content_sha256,
            input_transcript_sha256, created_at)
           VALUES (?, ?, 1, 'x', 'generated', 0, 'c', ?, ?)""",
        (minutes_id, hash_bound_id, "a" * 64, utc_now()),
    )
    db.execute(
        "UPDATE meetings SET current_minutes_version_id=? WHERE id=?",
        (minutes_id, hash_bound_id),
    )

    report = backfill_speakers.run(settings, dry_run=True)

    outcomes_by_id = {outcome.meeting_id: outcome for outcome in report.outcomes}
    assert outcomes_by_id[healthy_id].outcome == "applied"
    assert outcomes_by_id[draft_id].outcome == "human_review"
    assert outcomes_by_id[hash_bound_id].outcome == "applied"
    assert report.hash_bound_current_minutes == [hash_bound_id]
    assert report.meetings_with_funasr_json == 3


def test_run_raises_when_relay_has_in_flight_job(tmp_path):
    settings = _settings(tmp_path)
    settings.relay_jobs_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.relay_jobs_db) as connection:
        connection.execute("CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO jobs VALUES ('job-1', 'transcribing')")

    with pytest.raises(RuntimeError, match="job-1"):
        backfill_speakers.run(settings, dry_run=True)
