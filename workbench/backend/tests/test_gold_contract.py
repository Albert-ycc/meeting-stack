import json
import sqlite3

import pytest

from meeting_workbench.asr_eval import evaluate_asr
from meeting_workbench.db import Database, utc_now
from meeting_workbench.gold_export import GoldExportError, export_gold_jsonl

from .helpers import seed_editable_meeting
from .test_quality_api import make_client, write_headers


def test_gold_save_export_and_evaluate_share_one_roundtrip_contract(tmp_path):
    client = make_client(tmp_path)
    db = client.app.state.db
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-roundtrip")
    response = client.post(
        "/api/meetings/vm-roundtrip/asr-gold-samples",
        json={
            "segment_id": "seg-a",
            "reference": "  ＡＣＭＥ 金额 １２０ 万元  ",
            "entities": [" ＡＣＭＥ "],
            "numbers": ["１２０"],
            "tags": [" 术语 "],
        },
        headers=write_headers(client),
    )
    assert response.status_code == 200
    output = tmp_path / "gold.jsonl"

    assert export_gold_jsonl(db, output, meeting_id="vm-roundtrip") == 1
    exported = json.loads(output.read_text(encoding="utf-8"))
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / f"{exported['id']}.txt").write_text(exported["reference"], encoding="utf-8")

    report = evaluate_asr(output, {"candidate": engine})

    assert exported["reference"] == "ACME 金额 120 万元"
    assert exported["entities"] == ["ACME"]
    assert exported["numbers"] == ["120"]
    assert exported["tags"] == ["术语"]
    assert report["engines"]["candidate"]["cer"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"reference": "！！！", "entities": [], "numbers": [], "tags": []},
        {"reference": "有效", "entities": ["ACME", "acme"], "numbers": [], "tags": []},
        {"reference": "有效", "entities": [], "numbers": ["１２０", "120"], "tags": []},
        {"reference": "有效", "entities": [], "numbers": [], "tags": ["Medical", "ｍｅｄｉｃａｌ"]},
    ],
)
def test_gold_save_rejects_normalized_empty_or_duplicate_annotations(tmp_path, body):
    client = make_client(tmp_path)
    seed_editable_meeting(
        client.app.state.db, client.app.state.settings.archive_root, meeting_id="vm-invalid"
    )

    response = client.post(
        "/api/meetings/vm-invalid/asr-gold-samples",
        json={"segment_id": "seg-a", **body},
        headers=write_headers(client),
    )

    assert response.status_code == 422
    assert client.app.state.db.query_one("SELECT COUNT(*) AS count FROM asr_gold_samples")[
        "count"
    ] == 0


def test_gold_save_rejects_non_positive_source_duration(tmp_path):
    client = make_client(tmp_path)
    db = client.app.state.db
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-duration")
    db.execute("UPDATE segments SET end_ms=start_ms WHERE id='seg-a'")

    response = client.post(
        "/api/meetings/vm-duration/asr-gold-samples",
        json={"segment_id": "seg-a", "reference": "有效"},
        headers=write_headers(client),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("reference", "end_ms", "entities_json", "numbers_json", "tags_json"),
    [
        ("！！！", 100, "[]", "[]", "[]"),
        ("有效", 0, "[]", "[]", "[]"),
        ("有效", 100, '["ACME","acme"]', "[]", "[]"),
        ("有效", 100, "[]", "not-json", "[]"),
        ("有效", 100, "[]", "[]", '["Medical","ｍｅｄｉｃａｌ"]'),
    ],
)
def test_gold_export_rejects_corrupted_database_rows(
    tmp_path, reference, end_ms, entities_json, numbers_json, tags_json
):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-bad', '坏金标', 'published')")
    db.execute(
        """INSERT INTO asr_gold_samples
           (id, meeting_id, start_ms, end_ms, reference, entities_json, numbers_json,
            tags_json, created_at, updated_at)
           VALUES ('gold-bad', 'vm-bad', 0, ?, ?, ?, ?, ?, ?, ?)""",
        (
            end_ms,
            reference,
            entities_json,
            numbers_json,
            tags_json,
            utc_now(),
            utc_now(),
        ),
    )

    with pytest.raises(GoldExportError, match="金标样本无效"):
        export_gold_jsonl(db, tmp_path / "bad.jsonl")
    assert not (tmp_path / "bad.jsonl").exists()


def test_gold_save_event_failure_rolls_back_sample(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    db = client.app.state.db
    seed_editable_meeting(db, client.app.state.settings.archive_root, meeting_id="vm-event")
    original_add_event = db.add_event

    def fail_event(event_type, **kwargs):
        if event_type == "asr_gold_sample_saved":
            raise sqlite3.OperationalError("event failed")
        return original_add_event(event_type, **kwargs)

    monkeypatch.setattr(db, "add_event", fail_event)
    with pytest.raises(sqlite3.OperationalError):
        client.post(
            "/api/meetings/vm-event/asr-gold-samples",
            json={"segment_id": "seg-a", "reference": "有效金标"},
            headers=write_headers(client),
        )

    assert db.query_one("SELECT COUNT(*) AS count FROM asr_gold_samples")["count"] == 0
