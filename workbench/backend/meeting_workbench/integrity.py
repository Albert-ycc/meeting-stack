from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .archive_lock import ArchiveLock
from .config import Settings
from .db import Database, utc_now


class AudioIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioIntegrityResult:
    checked: int
    issues: int
    missing: int
    mismatched: int
    events_added: int
    completed_at: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_priority(row: dict[str, Any]) -> tuple[int, int, str, int]:
    source_priority = {
        "archive": 0,
        "draft": 1,
        "staging": 2,
        "history": 3,
    }.get(row.get("source_root"), 4)
    matches_baseline = int(row.get("artifact_sha256") != row.get("original_audio_sha256"))
    return (
        source_priority,
        matches_baseline,
        str(row.get("path") or "").casefold(),
        int(row.get("artifact_id") or 0),
    )


class AudioIntegrityVerifier:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.archive_lock = ArchiveLock(self.db.path.parent / "archive.lock")

    def verify(self) -> AudioIntegrityResult:
        root = self.settings.archive_root
        if root.is_symlink() or not root.is_dir():
            raise AudioIntegrityError("正式归档根目录不可用")
        with self.archive_lock:
            if root.is_symlink() or not root.is_dir():
                raise AudioIntegrityError("正式归档根目录不可用")
            return self._verify_locked()

    def _verify_locked(self) -> AudioIntegrityResult:
        rows = self.db.query_all(
            """SELECT m.id AS meeting_id, m.original_audio_sha256,
                      m.canonical_dir, m.source_priority, m.status,
                      a.id AS artifact_id, a.path, a.source_root,
                      a.sha256 AS artifact_sha256
                 FROM meetings m
                 LEFT JOIN artifacts a ON a.meeting_id=m.id AND a.kind='audio'
                WHERE m.original_audio_sha256 IS NOT NULL
                ORDER BY m.id"""
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["meeting_id"], []).append(row)
        selected: dict[str, dict[str, Any]] = {}
        for meeting_id, candidates in grouped.items():
            meeting = candidates[0]
            formal = int(meeting.get("source_priority") or 0) >= 100
            if formal:
                canonical = Path(meeting["canonical_dir"]) if meeting.get("canonical_dir") else None
                archive_candidates = []
                for candidate in candidates:
                    if candidate.get("source_root") != "archive" or not candidate.get("path"):
                        continue
                    path = Path(candidate["path"])
                    try:
                        inside_canonical = canonical is not None and path.resolve().is_relative_to(
                            canonical.resolve()
                        )
                    except OSError:
                        inside_canonical = False
                    if inside_canonical:
                        archive_candidates.append(candidate)
                selected[meeting_id] = (
                    min(archive_candidates, key=_artifact_priority)
                    if archive_candidates
                    else {**meeting, "artifact_id": None, "path": None, "source_root": "archive"}
                )
            else:
                selected[meeting_id] = min(candidates, key=_artifact_priority)

        missing = 0
        mismatched = 0
        events_added = 0
        for meeting_id, row in selected.items():
            expected = str(row["original_audio_sha256"])
            path = Path(row["path"]) if row.get("path") else None
            if path is None or path.is_symlink() or not path.is_file():
                missing += 1
                issue = {
                    "reason": "missing",
                    "artifact_id": row.get("artifact_id"),
                    "expected_sha256": expected,
                    "actual_sha256": None,
                }
            else:
                try:
                    actual = _sha256_file(path)
                except FileNotFoundError:
                    missing += 1
                    issue = {
                        "reason": "missing",
                        "artifact_id": row.get("artifact_id"),
                        "expected_sha256": expected,
                        "actual_sha256": None,
                    }
                except OSError as error:
                    raise AudioIntegrityError(f"原音频读取失败：{meeting_id}") from error
                else:
                    if actual == expected:
                        resolved = self.db.conflicts.resolve_kind(
                            meeting_id, "audio_integrity", "verified"
                        )
                        if resolved:
                            self.db.add_event(
                                "audio_integrity_resolved",
                                meeting_id=meeting_id,
                                actor="system",
                                payload={"artifact_id": row.get("artifact_id")},
                            )
                        continue
                    mismatched += 1
                    issue = {
                        "reason": "hash_mismatch",
                        "artifact_id": row.get("artifact_id"),
                        "expected_sha256": expected,
                        "actual_sha256": actual,
                    }
            self.db.conflicts.open(
                meeting_id,
                "audio_integrity",
                payload=issue,
            )
            previous = self.db.query_one(
                """SELECT payload_json FROM events
                   WHERE meeting_id=? AND event_type='audio_integrity_conflict'
                   ORDER BY id DESC LIMIT 1""",
                (meeting_id,),
            )
            previous_payload = None
            if previous:
                try:
                    previous_payload = json.loads(previous["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    previous_payload = None
            if previous_payload != issue:
                self.db.add_event(
                    "audio_integrity_conflict",
                    meeting_id=meeting_id,
                    actor="system",
                    payload=issue,
                )
                events_added += 1

        completed_at = utc_now()
        result = AudioIntegrityResult(
            checked=len(selected),
            issues=missing + mismatched,
            missing=missing,
            mismatched=mismatched,
            events_added=events_added,
            completed_at=completed_at,
        )
        self.db.add_event(
            "audio_integrity_verified",
            actor="system",
            payload=asdict(result),
        )
        return result


def last_audio_integrity_result(db: Database) -> dict[str, Any] | None:
    row = db.query_one(
        """SELECT event_type, payload_json, created_at FROM events
           WHERE event_type IN ('audio_integrity_verified', 'audio_integrity_failed')
           ORDER BY id DESC LIMIT 1"""
    )
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if row["event_type"] == "audio_integrity_verified":
        payload.setdefault("status", "ok" if int(payload.get("issues") or 0) == 0 else "issues")
    payload.setdefault("completed_at", row["created_at"])
    return payload
