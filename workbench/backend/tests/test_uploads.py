import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.uploads import UploadError, UploadManager


def manager(tmp_path, **overrides):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        upload_chunk_bytes=4,
        **overrides,
    )
    return UploadManager(settings)


def test_incomplete_uploads_expire_and_can_be_cancelled(tmp_path):
    uploads = manager(tmp_path, upload_session_ttl_seconds=60)
    expired = uploads.start("expired.m4a", 4)
    cancellable = uploads.start("cancel.m4a", 4)
    metadata_path = uploads.sessions_root / expired.upload_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["updated_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    removed = uploads.cleanup_expired()
    uploads.cancel(cancellable.upload_id)

    assert removed == 1
    assert not (uploads.sessions_root / expired.upload_id).exists()
    assert not (uploads.sessions_root / cancellable.upload_id).exists()


def test_incomplete_upload_reservation_enforces_disk_quota(tmp_path):
    uploads = manager(tmp_path, max_incomplete_upload_bytes=10)
    uploads.start("first.m4a", 8)

    with pytest.raises(UploadError, match="占用已达到上限"):
        uploads.start("second.m4a", 4)


def test_saved_pending_enqueue_receipt_is_idempotent_and_not_garbage_collected(tmp_path):
    uploads = manager(tmp_path, upload_session_ttl_seconds=60)
    session = uploads.start("meeting.m4a", 4)
    uploads.write_chunk(session.upload_id, 0, base64.b64encode(b"data").decode())
    first = uploads.complete(session.upload_id)
    metadata_path = uploads.sessions_root / session.upload_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["updated_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    second = uploads.complete(session.upload_id)
    removed = uploads.cleanup_expired()

    assert second == first
    assert removed == 0
    assert first.read_bytes() == b"data"
    assert uploads.receipt(session.upload_id)["status"] == "saved_pending_enqueue"


def test_complete_recovers_after_destination_install_before_receipt(tmp_path):
    uploads = manager(tmp_path)
    session = uploads.start("meeting.m4a", 4)
    uploads.write_chunk(session.upload_id, 0, base64.b64encode(b"data").decode())
    original_atomic_text = uploads._atomic_text

    def fail_receipt_once(path, content):
        if path.name == "receipt.json":
            raise OSError("simulated crash before receipt")
        return original_atomic_text(path, content)

    with patch.object(uploads, "_atomic_text", side_effect=fail_receipt_once):
        with pytest.raises(OSError, match="simulated crash"):
            uploads.complete(session.upload_id)

    destination = uploads.destination_root / f"{session.upload_id}-meeting.m4a"
    session_dir = uploads.sessions_root / session.upload_id
    assert destination.read_bytes() == b"data"
    assert (session_dir / "00000000.part").is_file()
    assert not (session_dir / "receipt.json").exists()

    recovered = uploads.complete(session.upload_id)

    receipt = uploads.receipt(session.upload_id)
    assert recovered == destination
    assert receipt["size_bytes"] == 4
    assert receipt["sha256"] == "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7"
    assert not (session_dir / "00000000.part").exists()


def test_complete_recovers_existing_destination_without_chunks(tmp_path):
    uploads = manager(tmp_path)
    session = uploads.start("meeting.m4a", 4)
    destination = uploads.destination_root / f"{session.upload_id}-meeting.m4a"
    uploads.destination_root.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"data")

    recovered = uploads.complete(session.upload_id)

    assert recovered == destination
    assert uploads.receipt(session.upload_id)["sha256"]


def test_complete_never_overwrites_mismatched_existing_destination(tmp_path):
    uploads = manager(tmp_path)
    session = uploads.start("meeting.m4a", 4)
    uploads.write_chunk(session.upload_id, 0, base64.b64encode(b"data").decode())
    destination = uploads.destination_root / f"{session.upload_id}-meeting.m4a"
    uploads.destination_root.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"wrong-size")

    with pytest.raises(UploadError, match="目标文件"):
        uploads.complete(session.upload_id)

    assert destination.read_bytes() == b"wrong-size"


def test_concurrent_starts_reserve_quota_atomically(tmp_path):
    uploads = manager(tmp_path, max_incomplete_upload_bytes=10)
    original_reserved = uploads._reserved_incomplete_bytes

    def slow_reserved():
        reserved = original_reserved()
        time.sleep(0.05)
        return reserved

    with patch.object(uploads, "_reserved_incomplete_bytes", side_effect=slow_reserved):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(uploads.start, f"meeting-{index}.m4a", 8) for index in range(2)
            ]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except UploadError as error:
                    outcomes.append(error)

    assert sum(isinstance(item, UploadError) for item in outcomes) == 1
    assert len(list(uploads.sessions_root.glob("upload-*"))) == 1


def test_only_one_enqueue_owner_can_claim_completed_upload(tmp_path):
    uploads = manager(tmp_path)
    session = uploads.start("meeting.m4a", 4)
    uploads.write_chunk(session.upload_id, 0, base64.b64encode(b"data").decode())
    uploads.complete(session.upload_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda owner: uploads.claim_enqueue(session.upload_id, owner),
                ["owner-a", "owner-b"],
            )
        )

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == "enqueueing"
    assert winners[0]["enqueue_owner"] in {"owner-a", "owner-b"}


def test_expired_enqueue_claim_can_be_recovered_by_new_owner(tmp_path):
    uploads = manager(tmp_path)
    session = uploads.start("meeting.m4a", 4)
    uploads.write_chunk(session.upload_id, 0, base64.b64encode(b"data").decode())
    uploads.complete(session.upload_id)
    claimed = uploads.claim_enqueue(session.upload_id, "dead-owner")
    assert claimed is not None
    receipt_path = uploads.sessions_root / session.upload_id / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["enqueue_lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    uploads._atomic_text(receipt_path, json.dumps(receipt))

    recovered = uploads.claim_enqueue(session.upload_id, "new-owner")

    assert recovered is not None
    assert recovered["enqueue_owner"] == "new-owner"
