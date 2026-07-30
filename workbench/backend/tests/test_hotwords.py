import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.hotwords import HotwordValidationError, hotword_audit, normalize_hotwords
from meeting_workbench.relay_client import RelayClient
from meeting_workbench.uploads import UploadManager


def settings(tmp_path: Path) -> Settings:
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
        upload_chunk_bytes=4,
        max_json_request_bytes=1024,
        max_json_upload_bytes=1024,
        max_incomplete_upload_bytes=1024,
    )


def test_hotwords_are_nfkc_trimmed_deduplicated_and_audited_without_plaintext():
    normalized = normalize_hotwords(["  ＡＣＭＥ  ", "ACME", "云图"])

    assert normalized == ["ACME", "云图"]
    audit = hotword_audit(normalized)
    assert audit["count"] == 2
    assert len(audit["sha256"]) == 64
    assert "ACME" not in json.dumps(audit, ensure_ascii=False)


def test_hotwords_deduplicate_by_nfkc_and_casefold_while_preserving_first_spelling():
    assert normalize_hotwords(["ACME", "acme", "ＡＣＭＥ", "云图"]) == ["ACME", "云图"]


@pytest.mark.parametrize(
    "values",
    [
        ["ok"] * 21,
        ["x" * 81],
        ["bad\nword"],
        ["\u007f"],
    ],
)
def test_hotwords_reject_limits_and_control_characters(values):
    with pytest.raises(HotwordValidationError):
        normalize_hotwords(values)


def test_relay_client_uses_private_ephemeral_hotword_file_for_enqueue_and_retry(
    tmp_path, monkeypatch
):
    calls = []

    def fake_run(arguments, **kwargs):
        forwarded = arguments[1:]
        hotword_path = Path(forwarded[forwarded.index("--hotwords") + 1])
        calls.append(
            {
                "arguments": forwarded,
                "content": hotword_path.read_text(encoding="utf-8"),
                "mode": hotword_path.stat().st_mode & 0o777,
                "path": hotword_path,
            }
        )
        payload = "job-created\n" if forwarded[0] == "enqueue" else '{"job_id":"job-created"}'
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)
    client = RelayClient(settings(tmp_path))

    client.enqueue(tmp_path / "audio.m4a", hotwords=[" ACME ", "云图"])
    client.retry("job-created", "transcribing", hotwords=["ACME"])

    assert calls[0]["content"] == "ACME\n云图\n"
    assert calls[1]["content"] == "ACME\n"
    assert all(call["mode"] == 0o600 for call in calls)
    assert all(not call["path"].exists() for call in calls)


def test_completed_upload_receipt_retains_hotwords_for_background_recovery(tmp_path):
    uploads = UploadManager(settings(tmp_path))
    session = uploads.start("meeting.m4a", 4, hotwords=[" ＡＣＭＥ ", "云图"])
    uploads.write_chunk(session.upload_id, 0, base64.b64encode(b"data").decode())
    uploads.complete(session.upload_id)

    pending = uploads.pending()

    assert pending[0]["hotwords"] == ["ACME", "云图"]
