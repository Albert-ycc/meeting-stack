import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from meeting_workbench.config import Settings
from meeting_workbench.db import ConflictStore, Database, utc_now
from meeting_workbench.importer import ArchiveImporter, ScanReport
from meeting_workbench.main import create_app
from meeting_workbench.service import MeetingService

from .helpers import seed_editable_meeting


def make_client(tmp_path, *, max_request_bytes=8 * 1024 * 1024):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        max_json_request_bytes=max_request_bytes,
        upload_chunk_bytes=1 if max_request_bytes < 8 * 1024 * 1024 else 4 * 1024 * 1024,
    )
    return TestClient(create_app(settings)), settings


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": "http://testserver"}


def test_write_endpoints_require_same_origin_csrf_and_json(tmp_path):
    client, _ = make_client(tmp_path)
    bootstrap = client.get("/api/bootstrap")
    token = bootstrap.json()["csrf_token"]

    assert client.post("/api/admin/scan", json={}).status_code == 403
    assert (
        client.post(
            "/api/admin/scan",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/scan",
            content="x",
            headers={
                "X-CSRF-Token": token,
                "Origin": "http://testserver",
                "Content-Type": "text/plain",
            },
        ).status_code
        == 415
    )
    response = client.post(
        "/api/admin/scan",
        json={},
        headers={"X-CSRF-Token": token, "Origin": "http://testserver"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("query", ["\x00", "\x1f", "\x7f"])
def test_search_rejects_forbidden_control_characters(tmp_path, query):
    client, _ = make_client(tmp_path)
    client = TestClient(client.app, raise_server_exceptions=False)

    response = client.get("/api/search", params={"q": query})

    assert response.status_code == 422


def test_chunked_write_body_is_limited_by_received_bytes_without_content_length(tmp_path):
    client, settings = make_client(tmp_path, max_request_bytes=32)
    app = client.app
    chunks = [b"x" * 20, b"y" * 13]
    receive_calls = 0
    sent = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls > len(chunks):
            raise AssertionError("middleware read beyond the supplied request body")
        return {
            "type": "http.request",
            "body": chunks[receive_calls - 1],
            "more_body": receive_calls < len(chunks),
        }

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/admin/scan",
        "raw_path": b"/api/admin/scan",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"origin", b"http://testserver"),
            (b"x-csrf-token", app.state.csrf_token.encode()),
            (
                b"cookie",
                f"{settings.csrf_cookie_name}={app.state.csrf_token}".encode(),
            ),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }

    asyncio.run(app(scope, receive, send))

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    assert receive_calls == 2


def test_media_endpoint_supports_byte_ranges(tmp_path):
    client, settings = make_client(tmp_path)
    audio = settings.archive_root / "meeting" / "audio.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"0123456789")
    db = Database(settings.database_path)
    db.execute(
        "INSERT INTO meetings(id, title, status) VALUES ('vm-range', 'Range', 'completed_unreviewed')"
    )
    artifact_id = db.execute(
        """INSERT INTO artifacts
           (meeting_id, kind, role, source_root, path, size_bytes, mtime_ns, created_at)
           VALUES ('vm-range', 'audio', 'source', 'archive', ?, 10, ?, ?)""",
        (str(audio), audio.stat().st_mtime_ns, utc_now()),
    )

    response = client.get(f"/api/media/{artifact_id}", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["accept-ranges"] == "bytes"


def test_loopback_binding_is_the_default():
    assert Settings().host == "127.0.0.1"
    assert Settings().port == 8765


def test_security_headers_block_external_runtime_resources(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.get("/api/health")

    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "connect-src 'self'" in policy
    assert "https:" not in policy
    assert response.headers["x-frame-options"] == "DENY"


def test_health_reports_recoverable_scan_errors_as_degraded(tmp_path):
    client, _ = make_client(tmp_path)
    client.app.state.last_scan = {"errors": 3}
    client.app.state.last_scan_error = None

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["services"]["scanner"] == "degraded"
    assert response.json()["counts"]["scan_errors"] == 3


def test_health_reports_fatal_scan_error_as_failed(tmp_path):
    client, _ = make_client(tmp_path)
    client.app.state.last_scan = {"errors": 2}
    client.app.state.last_scan_error = "sqlite failure"

    response = client.get("/api/health")

    assert response.json()["services"]["scanner"] == "failed"
    assert response.json()["counts"]["scan_errors"] == 2


def test_health_accepts_only_a_fresh_successful_backup_receipt(tmp_path):
    client, settings = make_client(tmp_path)
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    client.app.state.relay_health = {
        "status": "healthy",
        "mode": "controlled",
        "worker": {"state": "idle", "heartbeat_age_seconds": 0},
        "counts": {"queued": 0, "active": 0, "failed": 0},
    }
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    (settings.backup_dir / "last-backup.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "status": "healthy",
                "mirror_ok": True,
            }
        ),
        encoding="utf-8",
    )

    health = client.get("/api/health").json()

    assert health["status"] == "ok"
    assert health["services"]["backup"] == "healthy"
    assert health["details"]["backup"]["stale"] is False
    assert health["details"]["backup"]["age_seconds"] >= 0


def test_health_degrades_when_last_successful_backup_is_stale(tmp_path):
    client, settings = make_client(tmp_path)
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    client.app.state.relay_health = {
        "status": "healthy",
        "mode": "controlled",
        "worker": {"state": "idle", "heartbeat_age_seconds": 0},
        "counts": {"queued": 0, "active": 0, "failed": 0},
    }
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    (settings.backup_dir / "last-backup.json").write_text(
        json.dumps(
            {
                "created_at": (datetime.now(UTC) - timedelta(hours=31)).isoformat(),
                "status": "healthy",
                "mirror_ok": True,
            }
        ),
        encoding="utf-8",
    )

    health = client.get("/api/health").json()

    assert health["status"] == "degraded"
    assert health["services"]["backup"] == "degraded"
    assert health["details"]["backup"]["stale"] is True


def test_health_degrades_when_backup_receipt_reports_failure(tmp_path):
    client, settings = make_client(tmp_path)
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    client.app.state.relay_health = {
        "status": "healthy",
        "mode": "controlled",
        "worker": {"state": "idle", "heartbeat_age_seconds": 0},
        "counts": {"queued": 0, "active": 0, "failed": 0},
    }
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    (settings.backup_dir / "last-backup.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "status": "failed",
                "mirror_ok": True,
            }
        ),
        encoding="utf-8",
    )

    health = client.get("/api/health").json()

    assert health["status"] == "degraded"
    assert health["services"]["backup"] == "degraded"


def test_manual_scan_updates_recoverable_error_health_state(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path)
    monkeypatch.setattr(client.app.state.importer, "scan", lambda: ScanReport(errors=4))

    response = client.post("/api/admin/scan", json={}, headers=write_headers(client))

    assert response.status_code == 200
    health = client.get("/api/health").json()
    assert health["services"]["scanner"] == "degraded"
    assert health["counts"]["scan_errors"] == 4


def test_manual_scan_fatal_error_updates_health_state(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path)

    def fail_scan():
        raise sqlite3.OperationalError("simulated fatal scan")

    monkeypatch.setattr(client.app.state.importer, "scan", fail_scan)
    client = TestClient(client.app, raise_server_exceptions=False)

    response = client.post("/api/admin/scan", json={}, headers=write_headers(client))

    assert response.status_code == 500
    health = client.get("/api/health").json()
    assert health["services"]["scanner"] == "failed"


def test_untrusted_host_and_scheme_mismatch_are_rejected(tmp_path):
    client, _ = make_client(tmp_path)
    token = client.get("/api/bootstrap").json()["csrf_token"]

    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    rebound = client.post(
        "/api/projects",
        json={"name": "evil"},
        headers={
            "Host": "evil.example",
            "Origin": "http://evil.example",
            "X-CSRF-Token": token,
        },
    )
    assert rebound.status_code == 400
    mismatch = client.post(
        "/api/projects",
        json={"name": "mismatch"},
        headers={"Origin": "https://testserver", "X-CSRF-Token": token},
    )
    assert mismatch.status_code == 403


def test_segments_can_be_loaded_for_a_specific_transcript_version(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-a', 'A', 'published')")
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-b', 'B', 'published')")
    current = db.create_transcript_version("vm-a", "funasr", published=True)
    reference = db.create_transcript_version(
        "vm-a", "whisper_reference", published=False, make_current=False
    )
    other = db.create_transcript_version("vm-b", "whisper_reference", make_current=False)
    db.replace_segments(
        reference,
        "vm-a",
        [{"id": "ref", "start_ms": 1200, "end_ms": 1800, "text": "对照句"}],
    )

    response = client.get(f"/api/meetings/vm-a/transcript-versions/{reference}/segments")

    assert response.status_code == 200
    assert response.json()["items"][0]["text"] == "对照句"
    assert client.get(f"/api/meetings/vm-a/transcript-versions/{other}/segments").status_code == 404
    assert current != reference


def test_conflict_can_be_resolved_without_deadlocking_publish(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(db, settings.archive_root)
    importer = ArchiveImporter(db, settings)
    importer.scan()
    MeetingService(db).ensure_draft("vm-20260102-101500")
    (meeting_dir / "vm-20260102-101500.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n外部版本\n", encoding="utf-8"
    )
    assert importer.scan().conflicts == 1

    response = client.post(
        "/api/meetings/vm-20260102-101500/conflict/resolve",
        json={"action": "keep_draft"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["conflict"] == 0
    assert (
        db.query_one(
            """SELECT kind FROM transcript_versions tv JOIN meetings m
           ON m.current_transcript_version_id=tv.id WHERE m.id='vm-20260102-101500'"""
        )["kind"]
        == "draft"
    )


def test_accepting_external_conflict_preserves_draft_as_audit_version(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(db, settings.archive_root)
    importer = ArchiveImporter(db, settings)
    importer.scan()
    MeetingService(db).ensure_draft("vm-20260102-101500")
    (meeting_dir / "vm-20260102-101500.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n采用这个外部版本\n", encoding="utf-8"
    )
    importer.scan()

    response = client.post(
        "/api/meetings/vm-20260102-101500/conflict/resolve",
        json={"action": "accept_external"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["conflict"] == 0
    assert response.json()["segments"][0]["text"] == "采用这个外部版本"
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM transcript_versions WHERE kind='superseded_draft'"
        )["count"]
        == 1
    )


def test_accepting_external_transcript_recomputes_duration_and_speakers(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(db, settings.archive_root)
    importer = ArchiveImporter(db, settings)
    importer.scan()
    MeetingService(db).ensure_draft("vm-20260102-101500")
    (meeting_dir / "vm-20260102-101500.srt").write_text(
        "1\n00:00:00,100 --> 00:00:01,250\nSPEAKER_07: 外部版本\n",
        encoding="utf-8",
    )
    importer.scan()
    db.execute(
        "UPDATE meetings SET duration_ms = 99000 WHERE id = ?",
        ("vm-20260102-101500",),
    )
    db.execute("DELETE FROM speakers WHERE meeting_id = ?", ("vm-20260102-101500",))
    db.execute(
        """INSERT INTO speakers(id, meeting_id, label, display_name)
           VALUES ('speaker-stale', ?, 'SPEAKER_99', '旧说话人')""",
        ("vm-20260102-101500",),
    )

    response = client.post(
        "/api/meetings/vm-20260102-101500/conflict/resolve",
        json={"action": "accept_external"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["duration_ms"] == 1250
    assert [
        (speaker["label"], speaker["display_name"]) for speaker in response.json()["speakers"]
    ] == [("SPEAKER_07", None)]


def test_typed_external_conflict_is_exposed_and_resolved_by_id(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(db, settings.archive_root)
    importer = ArchiveImporter(db, settings)
    importer.scan()
    MeetingService(db).ensure_draft("vm-20260102-101500")
    (meeting_dir / "vm-20260102-101500.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n外部新版本\n", encoding="utf-8"
    )
    importer.scan()

    detail = client.get("/api/meetings/vm-20260102-101500").json()
    conflict = detail["conflicts"][0]
    response = client.post(
        f"/api/meetings/vm-20260102-101500/conflicts/{conflict['id']}/resolve",
        json={"action": "keep_draft"},
        headers=headers,
    )

    assert conflict["kind"] == "external_source_change"
    assert conflict["allowed_actions"] == [
        "keep_draft",
        "accept_external",
        "discard_draft",
    ]
    assert response.status_code == 200
    assert response.json()["conflict"] == 0


def test_non_external_conflict_cannot_be_cleared_by_generic_resolution(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)
    store = ConflictStore(db)
    external_id = store.open("vm-20260102-101500", "external_source_change")
    audio_id = store.open("vm-20260102-101500", "audio_integrity")

    response = client.post(
        f"/api/meetings/vm-20260102-101500/conflicts/{audio_id}/resolve",
        json={"action": "keep_draft"},
        headers=headers,
    )

    assert response.status_code == 409
    assert {item["id"] for item in store.list("vm-20260102-101500")} == {
        external_id,
        audio_id,
    }


def test_keep_draft_refreshes_changed_managed_artifact_before_resolving(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    meeting_dir, _audio, _version = seed_editable_meeting(db, settings.archive_root)
    importer = ArchiveImporter(db, settings)
    importer.scan()
    MeetingService(db).ensure_draft("vm-20260102-101500")
    source = meeting_dir / "vm-20260102-101500.funasr.json"
    source.write_text('{"changed": true}', encoding="utf-8")
    signature = importer.signature_for_meeting("vm-20260102-101500")
    conflict_id = db.conflicts.open(
        "vm-20260102-101500",
        "external_source_change",
        source_signature=signature,
        payload={
            "changed_resources": ["managed_sources"],
            "source_signature": signature,
        },
    )

    response = client.post(
        f"/api/meetings/vm-20260102-101500/conflicts/{conflict_id}/resolve",
        json={"action": "keep_draft"},
        headers=headers,
    )

    assert response.status_code == 200
    indexed = db.query_one("SELECT sha256 FROM artifacts WHERE path=?", (str(source),))
    assert indexed["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not db.conflicts.has("vm-20260102-101500")


def test_minutes_api_rejects_client_supplied_html(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)

    response = client.put(
        "/api/meetings/vm-20260102-101500/minutes",
        json={"markdown": "# 安全纪要", "html": "<script>fetch('//evil')</script>"},
        headers=headers,
    )

    assert response.status_code == 422


def test_transcript_save_rejects_stale_base_version_without_writes(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    _meeting_dir, _audio, current_version = seed_editable_meeting(db, settings.archive_root)
    before = db.query_one(
        "SELECT COUNT(*) AS count FROM transcript_versions WHERE meeting_id=?",
        ("vm-20260102-101500",),
    )["count"]

    response = client.put(
        "/api/meetings/vm-20260102-101500/transcript",
        json={
            "base_version_id": "tv-stale",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "text": "本地未保存内容",
                }
            ],
        },
        headers=headers,
    )

    assert current_version != "tv-stale"
    assert response.status_code == 409
    assert (
        db.query_one(
            "SELECT COUNT(*) AS count FROM transcript_versions WHERE meeting_id=?",
            ("vm-20260102-101500",),
        )["count"]
        == before
    )


def test_minutes_save_requires_matching_nullable_base_version(tmp_path):
    client, settings = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings.database_path)
    seed_editable_meeting(db, settings.archive_root)

    first = client.put(
        "/api/meetings/vm-20260102-101500/minutes",
        json={"base_version_id": None, "markdown": "# 第一版"},
        headers=headers,
    )
    stale = client.put(
        "/api/meetings/vm-20260102-101500/minutes",
        json={"base_version_id": None, "markdown": "# 不应覆盖"},
        headers=headers,
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    current = db.query_one(
        """SELECT mv.markdown FROM minutes_versions mv JOIN meetings m
           ON m.current_minutes_version_id=mv.id WHERE m.id=?""",
        ("vm-20260102-101500",),
    )
    assert current["markdown"] == "# 第一版"
