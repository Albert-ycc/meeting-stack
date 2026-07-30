from fastapi.testclient import TestClient

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.main import create_app

from .helpers import seed_editable_meeting


class NoopRelay:
    def list_jobs(self, *, status=None, limit=200):
        return []

    def health(self):
        return {"status": "healthy", "workers": []}


def make_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    return TestClient(create_app(settings, NoopRelay()))


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": "http://testserver"}


def seed_candidate(db, meeting_id):
    version = db.create_transcript_version(
        meeting_id, "whisper_reference", published=False, make_current=False
    )
    db.replace_segments(
        version,
        meeting_id,
        [
            {
                "id": "ref-a",
                "ordinal": 0,
                "start_ms": 1_050,
                "end_ms": 3_050,
                "speaker_label": "SPEAKER_00",
                "text": "第一段内容120万元 SCRM",
            },
            {
                "id": "ref-b",
                "ordinal": 1,
                "start_ms": 3_200,
                "end_ms": 5_000,
                "speaker_label": "SPEAKER_01",
                "text": "第二段内容",
            },
        ],
    )
    return version


def test_transcript_comparison_aligns_candidate_and_marks_risks(tmp_path):
    client = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    _directory, _audio, primary_version = seed_editable_meeting(
        db, client.app.state.settings.archive_root, meeting_id="vm-quality"
    )
    db.execute("UPDATE segments SET text='第一段内容125万元 CRM' WHERE id='seg-a'")
    candidate_version = seed_candidate(db, "vm-quality")

    response = client.get(
        f"/api/meetings/vm-quality/transcript-comparison?candidate_version_id={candidate_version}"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["primary_version_id"] == primary_version
    assert payload["candidate_version_id"] == candidate_version
    assert payload["items"][0]["risk_kinds"] == ["latin_term", "number"]


def test_gold_sample_is_idempotently_saved_from_a_meeting_segment(tmp_path):
    client = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-gold")
    headers = write_headers(client)

    first = client.post(
        "/api/meetings/vm-gold/asr-gold-samples",
        json={
            "segment_id": "seg-a",
            "reference": "人工校正后的第一段",
            "entities": ["云图"],
            "numbers": ["120"],
            "tags": ["数字", "术语"],
        },
        headers=headers,
    )
    second = client.post(
        "/api/meetings/vm-gold/asr-gold-samples",
        json={
            "segment_id": "seg-a",
            "reference": "人工再次校正的第一段",
            "entities": ["云图"],
            "numbers": ["120"],
            "tags": ["数字"],
        },
        headers=headers,
    )
    listed = client.get("/api/meetings/vm-gold/asr-gold-samples")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["reference"] == "人工再次校正的第一段"
    assert listed.json()["items"][0]["source_audio_sha256"]


def test_gold_sample_rejects_a_segment_from_another_meeting(tmp_path):
    client = make_client(tmp_path)
    db = Database(client.app.state.settings.database_path)
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-one")
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-two', '第二场会', 'published')")
    headers = write_headers(client)

    response = client.post(
        "/api/meetings/vm-two/asr-gold-samples",
        json={"segment_id": "seg-a", "reference": "越界样本"},
        headers=headers,
    )

    assert response.status_code == 404
