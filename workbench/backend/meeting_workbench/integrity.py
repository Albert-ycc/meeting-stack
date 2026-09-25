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


def _readable_path(row: dict[str, Any]) -> Path | None:
    """候选记录当前在磁盘上真实可读时返回其路径，否则返回 None。"""
    raw = row.get("path")
    if not raw:
        return None
    path = Path(raw)
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def _select_verifiable(
    candidates: list[dict[str, Any]], expected: str
) -> tuple[dict[str, Any] | None, str | None]:
    """在候选里按磁盘事实挑一条来判定，并附带已算出的实际哈希。

    索引里同一场会常常留着多条历史路径（中转目录清理、归档副本改名都会
    让旧记录失效）。巡检要回答的是「这场会的原音频还在不在、有没有被改过」，
    所以先找存活且哈希匹配的副本；没有匹配的再退到存活但哈希不符的（那才是
    真正的篡改）；全都不在磁盘上时才按原优先级选一条代表记录报缺失。
    """
    ordered = sorted(candidates, key=_artifact_priority)
    fallback: tuple[dict[str, Any], str] | None = None
    for candidate in ordered:
        path = _readable_path(candidate)
        if path is None:
            continue
        try:
            actual = _sha256_file(path)
        except FileNotFoundError:
            continue
        if actual == expected:
            return candidate, actual
        if fallback is None:
            fallback = (candidate, actual)
    if fallback is not None:
        return fallback
    return (ordered[0] if ordered else None), None


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
        selected: dict[str, tuple[dict[str, Any], str | None]] = {}
        for meeting_id, candidates in grouped.items():
            meeting = candidates[0]
            expected = str(meeting["original_audio_sha256"])
            formal = int(meeting.get("source_priority") or 0) >= 100
            if formal:
                # 正式归档是审计对象：只认 canonical_dir 里的副本，别处的拷贝
                # 不能顶替，否则归档丢文件就被悄悄掩盖过去了。
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
                pool = archive_candidates
                empty = {**meeting, "artifact_id": None, "path": None, "source_root": "archive"}
            else:
                pool = [candidate for candidate in candidates if candidate.get("path")]
                empty = {**meeting, "artifact_id": None, "path": None}
            try:
                chosen, actual = _select_verifiable(pool, expected)
            except OSError as error:
                raise AudioIntegrityError(f"原音频读取失败：{meeting_id}") from error
            selected[meeting_id] = (chosen if chosen is not None else empty, actual)

        missing = 0
        mismatched = 0
        events_added = 0
        for meeting_id, (row, actual) in selected.items():
            expected = str(row["original_audio_sha256"])
            if actual is None:
                missing += 1
                issue = {
                    "reason": "missing",
                    "artifact_id": row.get("artifact_id"),
                    "expected_sha256": expected,
                    "actual_sha256": None,
                }
            elif actual == expected:
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
            else:
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
