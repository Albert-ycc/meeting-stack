import hashlib
import json
import math
from pathlib import Path
import sqlite3

import pytest

from meeting_workbench.db import Database, utc_now

from .test_jobs_api import make_client


SOURCE_TEXT = "来源内容"
SOURCE_TEXT_SHA256 = hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest()
SOURCE_SRT = f"1\n00:00:10,000 --> 00:00:12,000\n{SOURCE_TEXT}\n"
SOURCE_SRT_SHA256 = hashlib.sha256(SOURCE_SRT.encode("utf-8")).hexdigest()
JOB_ID = "job-evidence"
ATTEMPT = 1
REQUESTED_STAGE = "minutes_generating"


def valid_plan(
    *,
    source_srt_sha256=SOURCE_SRT_SHA256,
    input_transcript_sha256=SOURCE_SRT_SHA256,
):
    return {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "source_srt_sha256": source_srt_sha256,
        "input_transcript_sha256": input_transcript_sha256,
        "total_duration_sec": 30,
        "cue_count": 1,
        "windows": [
            {
                "window_id": "W001",
                "start_sec": 0,
                "end_sec": 30,
                "cues": [
                    {
                        "cue_index": 1,
                        "source_start_sec": 10,
                        "source_end_sec": 12,
                        "source_text_sha256": SOURCE_TEXT_SHA256,
                    }
                ],
            }
        ],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _index_artifact(db, path, kind, *, source_root="archive", mtime_ns=None):
    stat = path.stat()
    db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, sha256, size_bytes,
            mtime_ns, created_at)
           VALUES ('vm-evidence', ?, 'source', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             meeting_id=excluded.meeting_id, kind=excluded.kind,
             source_root=excluded.source_root, sha256=excluded.sha256,
             size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns""",
        (
            kind,
            source_root,
            str(path),
            _sha256(path),
            stat.st_size,
            stat.st_mtime_ns if mtime_ns is None else mtime_ns,
            utc_now(),
        ),
    )


def _write_relay_attempt(
    relay_db: Path,
    *,
    source_srt_sha256: str,
    input_transcript_sha256: str | None,
    minutes_plan_sha256: str,
):
    with sqlite3.connect(relay_db) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                requested_stage TEXT NOT NULL,
                input_transcript_sha256 TEXT,
                source_srt_sha256 TEXT,
                minutes_plan_sha256 TEXT,
                minutes_protocol_version INTEGER NOT NULL,
                PRIMARY KEY(job_id, attempt_no)
            );
            """
        )
        connection.execute(
            """INSERT OR REPLACE INTO attempts
               (job_id, attempt_no, requested_stage, input_transcript_sha256,
                source_srt_sha256, minutes_plan_sha256, minutes_protocol_version)
               VALUES (?, ?, ?, ?, ?, ?, 3)""",
            (
                JOB_ID,
                ATTEMPT,
                REQUESTED_STAGE,
                input_transcript_sha256,
                source_srt_sha256,
                minutes_plan_sha256,
            ),
        )


def _refresh_manifest_and_indexes(db, meeting_dir: Path):
    manifest_path = meeting_dir / "workbench-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["artifacts"]:
        artifact_path = meeting_dir / entry["path"]
        entry["bytes"] = artifact_path.stat().st_size
        entry["sha256"] = _sha256(artifact_path)
        indexed = db.query_one("SELECT id FROM artifacts WHERE path=?", (str(artifact_path),))
        if indexed:
            stat = artifact_path.stat()
            db.execute(
                "UPDATE artifacts SET sha256=?, size_bytes=?, mtime_ns=? WHERE path=?",
                (_sha256(artifact_path), stat.st_size, stat.st_mtime_ns, str(artifact_path)),
            )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _index_artifact(db, manifest_path, "manifest")
    _write_relay_attempt(
        meeting_dir.parent.parent / "relay.sqlite3",
        source_srt_sha256=_sha256(meeting_dir / "input-transcript.srt"),
        input_transcript_sha256=_sha256(meeting_dir / "input-transcript.srt"),
        minutes_plan_sha256=_sha256(meeting_dir / "minutes-plan.json"),
    )


def add_evidence(
    db,
    root,
    *,
    content,
    mtime_ns=1,
    plan=None,
    minutes_markdown="# 纪要\n结论 [00:00:10]\n",
    include_plan=True,
    index_plan=True,
    source_srt=SOURCE_SRT,
):
    meeting_dir = root / "vm-evidence"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    path = meeting_dir / "minutes-evidence.json"
    path.write_text(content, encoding="utf-8")
    _index_artifact(db, path, "minutes_evidence", mtime_ns=mtime_ns)
    plan_path = meeting_dir / "minutes-plan.json"
    if include_plan:
        plan_path.write_text(json.dumps(plan or valid_plan(), ensure_ascii=False), encoding="utf-8")
        if index_plan:
            _index_artifact(db, plan_path, "minutes_plan")
    minutes_path = meeting_dir / "meeting.md"
    minutes_path.write_text(minutes_markdown, encoding="utf-8")
    _index_artifact(db, minutes_path, "document_md")
    minutes_html_path = meeting_dir / "meeting.html"
    minutes_html_path.write_text("<!doctype html>", encoding="utf-8")
    _index_artifact(db, minutes_html_path, "minutes_html")
    source_srt_path = meeting_dir / "input-transcript.srt"
    source_srt_path.write_text(source_srt, encoding="utf-8")
    _index_artifact(db, source_srt_path, "srt")

    content_sha256 = hashlib.sha256(minutes_markdown.encode("utf-8")).hexdigest()
    db.execute(
        """INSERT INTO minutes_versions
           (id, meeting_id, version_no, markdown, kind, published, content_sha256,
            source_job_id, source_attempt, requested_stage,
            input_transcript_sha256, created_at)
           VALUES ('mv-evidence', 'vm-evidence', 1, ?, 'generated', 1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             markdown=excluded.markdown, content_sha256=excluded.content_sha256,
             source_job_id=excluded.source_job_id,
             source_attempt=excluded.source_attempt,
             requested_stage=excluded.requested_stage,
             input_transcript_sha256=excluded.input_transcript_sha256""",
        (
            minutes_markdown,
            content_sha256,
            JOB_ID,
            ATTEMPT,
            REQUESTED_STAGE,
            _sha256(source_srt_path),
            utc_now(),
        ),
    )
    db.execute(
        "UPDATE meetings SET current_minutes_version_id='mv-evidence' WHERE id='vm-evidence'"
    )

    artifact_paths = [path, minutes_path, minutes_html_path, source_srt_path]
    if include_plan:
        artifact_paths.append(plan_path)
    manifest = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "meeting_id": "vm-evidence",
        "job_id": JOB_ID,
        "attempt": ATTEMPT,
        "requested_stage": REQUESTED_STAGE,
        "input_transcript_sha256": _sha256(source_srt_path),
        "artifacts": [
            {
                "path": artifact_path.name,
                "bytes": artifact_path.stat().st_size,
                "sha256": _sha256(artifact_path),
            }
            for artifact_path in artifact_paths
        ],
    }
    manifest_path = meeting_dir / "workbench-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _index_artifact(db, manifest_path, "manifest")
    if include_plan:
        _write_relay_attempt(
            root.parent / "relay.sqlite3",
            source_srt_sha256=_sha256(source_srt_path),
            input_transcript_sha256=_sha256(source_srt_path),
            minutes_plan_sha256=_sha256(plan_path),
        )
    return path


def valid_payload():
    return {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "strategy": "multi_stage",
        "secret_unknown": "must-not-leak",
        "coverage": {"total_items": 1, "included_items": 1, "omitted_items": 0},
        "topics": [
            {
                "topic_id": "T01",
                "title": "议题",
                "start_sec": 10,
                "end_sec": 20,
                "unknown": "omit",
                "items": [
                    {
                        "item_id": "D01",
                        "kind": "decision",
                        "text": "结论",
                        "source_start_sec": 10,
                        "source_end_sec": 12,
                        "source_window_id": "W001",
                        "source_text_sha256": SOURCE_TEXT_SHA256,
                        "minutes_anchor": "[00:00:10]",
                        "status": "included",
                        "unknown": "omit",
                    }
                ],
            }
        ],
    }


def test_minutes_evidence_api_returns_only_structured_allowlisted_fields(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )

    response = client.get("/api/meetings/vm-evidence/minutes-evidence")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"coverage", "topics", "anchors"}
    assert payload["topics"][0]["items"][0]["text"] == "结论"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "must-not-leak" not in serialized
    assert str(path) not in serialized


def test_minutes_evidence_api_reads_relay_attempt_without_writing_database(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    relay_db = client.app.state.settings.relay_jobs_db
    before = relay_db.stat().st_mtime_ns
    relay_db.chmod(0o444)

    try:
        response = client.get("/api/meetings/vm-evidence/minutes-evidence")
    finally:
        relay_db.chmod(0o644)

    assert response.status_code == 200
    assert relay_db.stat().st_mtime_ns == before


def test_minutes_evidence_api_uses_404_for_missing_and_409_for_bad_or_oversized(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 404

    path = add_evidence(db, client.app.state.settings.archive_root, content="{bad")
    bad = client.get("/api/meetings/vm-evidence/minutes-evidence")
    assert bad.status_code == 409
    assert str(path) not in bad.text

    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    oversized = client.get("/api/meetings/vm-evidence/minutes-evidence")
    assert oversized.status_code == 409


def test_minutes_evidence_api_rejects_non_finite_numbers_as_controlled_409(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    for value in (math.nan, math.inf, -math.inf):
        payload = valid_payload()
        payload["topics"][0]["start_sec"] = value
        path = add_evidence(
            db,
            client.app.state.settings.archive_root,
            content=json.dumps(payload, ensure_ascii=False, allow_nan=True),
            mtime_ns=10,
        )

        response = client.get("/api/meetings/vm-evidence/minutes-evidence")

        assert response.status_code == 409
        assert str(path) not in response.text
        db.execute("DELETE FROM artifacts WHERE meeting_id='vm-evidence'")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"schema_version": 2}),
        lambda payload: payload.update({"minutes_protocol_version": 2}),
        lambda payload: payload.update({"strategy": "unknown"}),
        lambda payload: payload["topics"][0].update({"start_sec": -1}),
        lambda payload: payload["topics"][0].update({"start_sec": 21, "end_sec": 20}),
        lambda payload: payload["topics"].append(
            {
                **payload["topics"][0],
                "topic_id": "T02",
                "start_sec": 19,
                "end_sec": 30,
            }
        ),
        lambda payload: payload["topics"].append(
            {**payload["topics"][0], "items": [], "start_sec": 21, "end_sec": 30}
        ),
        lambda payload: payload["topics"][0]["items"][0].update({"kind": "invented"}),
        lambda payload: payload["topics"][0]["items"][0].update({"source_start_sec": -1}),
        lambda payload: payload["topics"][0]["items"][0].update(
            {"source_start_sec": 13, "source_end_sec": 12}
        ),
        lambda payload: payload["topics"][0]["items"][0].update({"source_start_sec": 9}),
        lambda payload: payload["topics"][0]["items"][0].pop("source_window_id"),
        lambda payload: payload["topics"][0]["items"][0].pop("source_text_sha256"),
        lambda payload: payload["topics"][0]["items"][0].update(
            {"source_text_sha256": "not-a-sha"}
        ),
        lambda payload: payload["topics"][0]["items"][0].pop("minutes_anchor"),
        lambda payload: payload["topics"][0]["items"][0].update({"minutes_anchor": "10秒"}),
        lambda payload: payload["topics"][0]["items"][0].update(
            {"status": "omitted", "minutes_anchor": "", "omitted_reason": ""}
        ),
        lambda payload: payload["coverage"].update({"included_items": 0}),
    ],
)
def test_minutes_evidence_api_rejects_protocol_v3_pseudo_evidence(tmp_path, mutation):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    payload = valid_payload()
    mutation(payload)
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(payload, ensure_ascii=False),
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_duplicate_ids_and_out_of_order_items(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    payload = valid_payload()
    first = payload["topics"][0]["items"][0]
    payload["topics"][0]["items"] = [
        {**first, "source_start_sec": 15, "source_end_sec": 16},
        {**first, "source_start_sec": 12, "source_end_sec": 13},
    ]
    payload["coverage"] = {"total_items": 2, "included_items": 2, "omitted_items": 0}
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(payload, ensure_ascii=False),
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


@pytest.mark.parametrize("remove_hash", [False, True])
def test_minutes_evidence_api_rejects_artifact_hash_mismatch_or_missing_hash(tmp_path, remove_hash):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    if remove_hash:
        db.execute("UPDATE artifacts SET sha256=NULL WHERE meeting_id='vm-evidence'")
    else:
        swapped = valid_payload()
        swapped["topics"][0]["items"][0]["text"] = "被换包的另一份内容"
        path.write_text(json.dumps(swapped, ensure_ascii=False), encoding="utf-8")

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_requires_same_attempt_plan(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
        include_plan=False,
    )

    response = client.get("/api/meetings/vm-evidence/minutes-evidence")

    assert response.status_code == 409
    assert "暂无可验证证据" in response.text


def test_minutes_evidence_api_reports_v2_as_unverifiable(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    payload = valid_payload()
    payload["minutes_protocol_version"] = 2
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(payload, ensure_ascii=False),
    )

    response = client.get("/api/meetings/vm-evidence/minutes-evidence")

    assert response.status_code == 409
    assert "暂无可验证证据" in response.text


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["topics"][0]["items"][0].update({"source_window_id": "W999"}),
        lambda payload: payload["topics"][0]["items"][0].update({"source_text_sha256": "d" * 64}),
        lambda payload: payload["topics"][0]["items"][0].update({"source_start_sec": 10.5}),
        lambda payload: payload["topics"][0]["items"][0].update({"minutes_anchor": "[99:99:99]"}),
    ],
)
def test_minutes_evidence_api_rejects_items_not_bound_to_plan_or_minutes(tmp_path, mutation):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    payload = valid_payload()
    mutation(payload)
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(payload, ensure_ascii=False),
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_requires_unique_sequential_plan_cue_indices(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    plan = valid_plan()
    plan["windows"][0]["cues"].append(
        {
            "cue_index": 1,
            "source_start_sec": 13,
            "source_end_sec": 14,
            "source_text_sha256": "d" * 64,
        }
    )
    plan["cue_count"] = 2
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
        plan=plan,
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_allows_repeated_source_text_at_different_times(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    repeated_srt = (
        f"1\n00:00:10,000 --> 00:00:12,000\n{SOURCE_TEXT}\n\n"
        f"2\n00:00:13,000 --> 00:00:14,000\n{SOURCE_TEXT}\n"
    )
    repeated_srt_sha256 = hashlib.sha256(repeated_srt.encode("utf-8")).hexdigest()
    plan = valid_plan(
        source_srt_sha256=repeated_srt_sha256,
        input_transcript_sha256=repeated_srt_sha256,
    )
    plan["windows"][0]["cues"].append(
        {
            "cue_index": 2,
            "source_start_sec": 13,
            "source_end_sec": 14,
            "source_text_sha256": SOURCE_TEXT_SHA256,
        }
    )
    plan["cue_count"] = 2
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
        plan=plan,
        source_srt=repeated_srt,
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 200


def test_minutes_evidence_api_requires_action_owner_and_deadline(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    payload = valid_payload()
    payload["topics"][0]["items"][0]["kind"] = "action"
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(payload, ensure_ascii=False),
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_binds_omitted_items_to_plan(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    payload = valid_payload()
    item = payload["topics"][0]["items"][0]
    item.update(
        {
            "status": "omitted",
            "omitted_reason": "未形成结论",
            "source_text_sha256": "d" * 64,
        }
    )
    payload["coverage"] = {"total_items": 1, "included_items": 0, "omitted_items": 1}
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(payload, ensure_ascii=False),
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_requires_anchor_to_exist_in_corresponding_minutes(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
        minutes_markdown="# 纪要\n没有时间锚点\n",
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_unindexed_sibling_plan(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
        index_plan=False,
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_indexed_plan_hash_mismatch(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    evidence_path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    (evidence_path.parent / "minutes-plan.json").write_text("{}", encoding="utf-8")

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_indexed_minutes_hash_mismatch(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    evidence_path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    (evidence_path.parent / "meeting.md").write_text(
        "# 被换包的纪要\n[00:00:10]\n", encoding="utf-8"
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_unlisted_sidecar_minutes_candidates(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    evidence_path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    (evidence_path.parent / "000-fake.md").write_text(
        "# 旁置伪纪要\n[00:00:10]\n", encoding="utf-8"
    )
    (evidence_path.parent / "000-fake.html").write_text("<!doctype html>", encoding="utf-8")

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_long_single_window_for_long_recording(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    plan = valid_plan()
    plan["total_duration_sec"] = 3600
    plan["windows"][0]["end_sec"] = 3600
    add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
        plan=plan,
    )

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_rehashed_plan_not_matching_source_srt(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    evidence_path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    plan_path = evidence_path.parent / "minutes-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["windows"][0]["cues"][0]["source_text_sha256"] = "f" * 64
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["topics"][0]["items"][0]["source_text_sha256"] = "f" * 64
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    _refresh_manifest_and_indexes(db, evidence_path.parent)

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("meeting_id", "vm-other"),
        ("job_id", "job-other"),
        ("attempt", 2),
        ("requested_stage", "discovered"),
        ("input_transcript_sha256", "d" * 64),
    ],
)
def test_minutes_evidence_api_rejects_manifest_provenance_mismatch(tmp_path, field, value):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    evidence_path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    manifest_path = evidence_path.parent / "workbench-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _index_artifact(db, manifest_path, "manifest")

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409


def test_minutes_evidence_api_rejects_ambiguous_manifest_minutes_by_content_hash(tmp_path):
    client, _relay = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-evidence', '证据会', 'published')"
    )
    evidence_path = add_evidence(
        db,
        client.app.state.settings.archive_root,
        content=json.dumps(valid_payload(), ensure_ascii=False),
    )
    duplicate = evidence_path.parent / "duplicate.md"
    duplicate.write_bytes((evidence_path.parent / "meeting.md").read_bytes())
    _index_artifact(db, duplicate, "document_md")
    manifest_path = evidence_path.parent / "workbench-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].append(
        {
            "path": duplicate.name,
            "bytes": duplicate.stat().st_size,
            "sha256": _sha256(duplicate),
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _index_artifact(db, manifest_path, "manifest")

    assert client.get("/api/meetings/vm-evidence/minutes-evidence").status_code == 409
