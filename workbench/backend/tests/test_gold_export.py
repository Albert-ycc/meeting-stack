import json

from meeting_workbench import cli
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now


def test_asr_export_gold_writes_deterministic_schema_v2_jsonl_without_printing_body(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    db = Database(settings.database_path)
    db.initialize()
    db.execute("INSERT INTO meetings(id, title, status) VALUES ('vm-gold', '金标会', 'published')")
    db.execute(
        """INSERT INTO asr_gold_samples
           (id, meeting_id, segment_id, start_ms, end_ms, reference, entities_json,
            numbers_json, tags_json, source_audio_sha256, created_at, updated_at)
           VALUES ('gold-b', 'vm-gold', NULL, 200, 300, '第二条正文', '[\"云图\"]',
                   '[\"120\"]', '[\"medical\"]', NULL, ?, ?),
                  ('gold-a', 'vm-gold', NULL, 0, 100, '第一条正文', '[]', '[]', '[]', NULL, ?, ?)""",
        (utc_now(), utc_now(), utc_now(), utc_now()),
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    output = tmp_path / "gold.jsonl"

    result = cli.main(["asr-export-gold", "--output", str(output), "--meeting-id", "vm-gold"])

    assert result == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in rows] == ["gold-a", "gold-b"]
    assert rows[0]["schema_version"] == 2
    assert rows[1]["entities"] == ["云图"]
    stdout = capsys.readouterr().out
    assert "第一条正文" not in stdout and "第二条正文" not in stdout
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_asr_export_gold_returns_nonzero_for_empty_selection(tmp_path, monkeypatch, capsys):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
    )
    Database(settings.database_path).initialize()
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    result = cli.main(["asr-export-gold", "--output", str(tmp_path / "empty.jsonl")])

    assert result != 0
    assert "没有金标样本" in capsys.readouterr().err
