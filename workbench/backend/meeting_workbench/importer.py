from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive_lock import ArchiveLock
from .config import Settings
from .db import Database, utc_now
from .parsers import (
    load_json_file,
    parse_funasr_json,
    parse_srt,
    parse_txt,
    parse_whisper_json,
)
from .rendering import (
    render_transcript_srt,
    render_transcript_txt,
    safe_imported_minutes_html,
)


AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".qta", ".mp4"}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | {
    ".srt",
    ".txt",
    ".json",
    ".md",
    ".html",
    ".htm",
    ".log",
    ".tsv",
    ".vtt",
}
VM_RE = re.compile(
    r"(?<![A-Za-z0-9])vm[-_]\d{8}[-_]\d{6}(?:[-_][0-9A-Fa-f]{8})?",
    re.IGNORECASE,
)
SAFE_MEETING_ID_RE = re.compile(r"^(?:vm|fp|legacy)-[a-z0-9][a-z0-9_-]{0,127}$")
UNTITLED_TITLE_SUFFIX = "未命名录音"


def input_transcript_name(manifest: dict[str, Any]) -> str:
    """纪要协议 v3 起，重生成的输入逐字稿改用 SRT。

    文件名必须跟 Relay 写盘时的判断保持一致，否则重生成出来的纪要
    会因为校验找不到输入稿而被整目录隔离。
    """
    version = manifest.get("minutes_protocol_version")
    if type(version) is int and version >= 3:
        return "input-transcript.srt"
    return "input-transcript.txt"


SUPPORT_DIRECTORIES = {".obsidian", "funasr-poc-260708", "待校对"}
DEGRADED_MARKERS = {
    "_hallucinated_backup",
    "_mlx-degraded",
    "mlx-first-run-degraded",
    "rerun",
    "rerun-openai",
}
NON_MINUTES_DOCUMENT_MARKERS = {
    "转写",
    "原文",
    "逐字稿",
    "字幕",
    "transcript",
    "transcription",
    "subtitle",
    "prompt",
    "提示词",
    "合同",
    "协议",
    "法务",
    "法律",
    "律师",
}
RECOVERABLE_SOURCE_ERRORS = (
    OSError,
    ValueError,
    UnicodeError,
    subprocess.SubprocessError,
    RecursionError,
)


@dataclass(slots=True)
class ScanReport:
    meetings_seen: int = 0
    meetings_created: int = 0
    versions_imported: int = 0
    artifacts_seen: int = 0
    conflicts: int = 0
    errors: int = 0


@dataclass(slots=True)
class SourceBundle:
    meeting_id: str
    directory: Path
    source_root: str
    priority: int
    files: list[Path]


def normalize_meeting_id(value: str) -> str:
    normalized = value.replace("_", "-").lower().strip()
    if not SAFE_MEETING_ID_RE.fullmatch(normalized):
        raise ValueError("invalid meeting_id")
    return normalized


def ids_in_text(value: str) -> set[str]:
    return {normalize_meeting_id(match.group(0)) for match in VM_RE.finditer(value)}


def is_degraded(path: Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    return any(marker.lower() in lowered for marker in DEGRADED_MARKERS)


def is_noise(path: Path) -> bool:
    internal = {".workbench-history"}
    return (
        path.is_symlink()
        or path.name.startswith("._")
        or path.name in {".DS_Store"}
        or "/.Trash" in str(path)
        or any(part in internal or part.startswith(".workbench-publish-") for part in path.parts)
    )


def source_signature(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=str):
        stat = path.stat()
        digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
    return digest.hexdigest()


def artifact_kind(path: Path) -> str:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        return "audio"
    if name == "workbench-manifest.json":
        return "manifest"
    if name == "minutes-evidence.json":
        return "minutes_evidence"
    if name == "minutes-plan.json":
        return "minutes_plan"
    if name == "prompt.txt":
        return "prompt"
    if name == "spk.txt" or name.endswith(".spk.txt"):
        return "speaker_map"
    if "whisper-ref" in parts:
        if path.suffix.lower() == ".json":
            return "whisper_json"
        if path.suffix.lower() == ".srt":
            return "whisper_srt"
        if path.suffix.lower() == ".txt":
            return "whisper_txt"
        if path.suffix.lower() == ".log":
            return "whisper_log"
        if path.suffix.lower() == ".tsv":
            return "whisper_tsv"
        if path.suffix.lower() == ".vtt":
            return "whisper_vtt"
        return "whisper_other"
    if "funasr" in name and path.suffix.lower() == ".json":
        return "funasr_json"
    if "funasr" in name and path.suffix.lower() == ".log":
        return "funasr_log"
    if ("whisper" in name or name.endswith(".raw.json")) and path.suffix.lower() == ".json":
        return "whisper_json"
    if path.suffix.lower() == ".srt":
        return "srt"
    if path.suffix.lower() == ".txt":
        return "txt"
    if path.suffix.lower() == ".md" or path.suffix == "":
        if "转写" in path.name or "原文" in path.name:
            return "transcript_md"
        if "纪要" in path.name:
            return "minutes_md"
        return "document_md"
    if path.suffix.lower() in {".html", ".htm"}:
        return "minutes_html"
    return "json" if path.suffix.lower() == ".json" else "other"


def topic_minutes_pair(files: list[Path]) -> tuple[Path, Path] | None:
    markdown_by_stem: dict[tuple[Path, str], Path] = {}
    html_by_stem: dict[tuple[Path, str], Path] = {}
    for path in files:
        if is_degraded(path):
            continue
        key = (path.parent, path.stem.casefold())
        if path.suffix.lower() == ".md" and artifact_kind(path) == "document_md":
            markdown_by_stem.setdefault(key, path)
        elif path.suffix.lower() in {".html", ".htm"}:
            html_by_stem.setdefault(key, path)
    for key in sorted(markdown_by_stem, key=lambda item: (str(item[0]), item[1])):
        html_path = html_by_stem.get(key)
        if not html_path:
            continue
        normalized_stem = re.sub(r"[\W_]+", "", key[1], flags=re.UNICODE)
        if any(marker.casefold() in normalized_stem for marker in NON_MINUTES_DOCUMENT_MARKERS):
            continue
        return markdown_by_stem[key], html_path
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_pcm_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
            digest.update(chunk)
        if process.wait(timeout=600) == 0 and digest.digest() != hashlib.sha256().digest():
            return digest.hexdigest()
    except (OSError, subprocess.SubprocessError):
        pass
    return sha256_file(path)


def extract_meeting_id(directory: Path, files: list[Path]) -> str | None:
    manifest = next((path for path in files if path.name == "workbench-manifest.json"), None)
    if manifest:
        try:
            value = load_json_file(manifest).get("meeting_id")
            if isinstance(value, str) and value.strip():
                return normalize_meeting_id(value.strip())
        except (OSError, ValueError, AttributeError):
            pass
    for candidate in [directory.name, *(path.stem for path in files)]:
        match = VM_RE.search(candidate)
        if match:
            return normalize_meeting_id(match.group(0))
    audio = next((path for path in files if path.suffix.lower() in AUDIO_EXTENSIONS), None)
    if audio:
        return f"fp-{normalized_pcm_fingerprint(audio)[:24]}"
    meaningful = [
        path
        for path in files
        if artifact_kind(path) in {"srt", "txt", "funasr_json", "whisper_json"}
    ]
    if meaningful:
        stable = hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:24]
        return f"legacy-{stable}"
    return None


class ArchiveImporter:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.archive_lock = ArchiveLock(self.db.path.parent / "archive.lock")

    def scan(self) -> ScanReport:
        with self.archive_lock:
            return self._scan_locked()

    def _scan_locked(self) -> ScanReport:
        report = ScanReport()
        archive_available = (
            self.settings.archive_root.is_dir() and not self.settings.archive_root.is_symlink()
        )
        bundles: list[SourceBundle] = []
        try:
            bundles.extend(
                self._discover_root(self.settings.staging_root, "staging", 10, report=report)
            )
        except RECOVERABLE_SOURCE_ERRORS:
            report.errors += 1
        try:
            bundles.extend(self._discover_archive(report=report))
        except RECOVERABLE_SOURCE_ERRORS:
            report.errors += 1
        grouped = self._coalesce_bundles(bundles, report=report)
        report.meetings_seen = len(grouped)
        all_bundles = bundles
        for meeting_id, meeting_bundles in grouped.items():
            try:
                existing = self.db.query_one(
                    "SELECT source_priority FROM meetings WHERE id=?", (meeting_id,)
                )
                if (
                    not archive_available
                    and existing
                    and int(existing["source_priority"] or 0) >= 100
                ):
                    continue
                self._import_meeting(meeting_id, meeting_bundles, report)
            except RECOVERABLE_SOURCE_ERRORS:
                report.errors += 1
        if report.errors == 0:
            self._cleanup_stale_artifacts(all_bundles)
        return report

    def _cleanup_stale_artifacts(self, bundles: list[SourceBundle]) -> None:
        discovered = {str(path) for bundle in bundles for path in bundle.files}
        available_roots = set()
        if self.settings.archive_root.is_dir():
            available_roots.update({"archive", "history", "draft"})
        if self.settings.staging_root.is_dir():
            available_roots.add("staging")
        if not available_roots:
            return
        rows = self.db.query_all(
            "SELECT id, path, source_root FROM artifacts WHERE source_root IN (%s)"
            % ",".join("?" for _ in available_roots),
            tuple(sorted(available_roots)),
        )
        stale = [row["id"] for row in rows if row["path"] not in discovered]
        if not stale:
            return
        with self.db.transaction() as connection:
            connection.executemany(
                "DELETE FROM artifacts WHERE id=?", [(value,) for value in stale]
            )

    def signature_for_meeting(self, meeting_id: str) -> str | None:
        with self.archive_lock:
            return self.signature_for_meeting_locked(meeting_id)

    def signature_for_meeting_locked(self, meeting_id: str) -> str | None:
        normalized = normalize_meeting_id(meeting_id)
        report = ScanReport()
        bundles = self._discover_root(self.settings.staging_root, "staging", 10, report=report)
        bundles.extend(self._discover_archive(report=report))
        grouped = self._coalesce_bundles(bundles, report=report)
        selected = grouped.get(normalized)
        if not selected:
            return None
        return source_signature([path for bundle in selected for path in bundle.files])

    def _discover_archive(self, *, report: ScanReport | None = None) -> list[SourceBundle]:
        root = self.settings.archive_root
        if not root.exists() or root.is_symlink():
            return []
        bundles: list[SourceBundle] = []
        try:
            bundles.extend(
                self._discover_workbench_drafts(root / ".workbench-drafts", report=report)
            )
        except RECOVERABLE_SOURCE_ERRORS:
            if report is not None:
                report.errors += 1
        try:
            bundles.extend(self._discover_pending_reviews(root / "待校对", report=report))
        except RECOVERABLE_SOURCE_ERRORS:
            if report is not None:
                report.errors += 1
        for child in sorted(root.iterdir()):
            try:
                if (
                    not child.is_dir()
                    or child.is_symlink()
                    or child.name in SUPPORT_DIRECTORIES
                    or child.name.startswith(".")
                ):
                    continue
                if child.name == "meeting-relay-历史产物":
                    bundles.extend(self._discover_root(child, "history", 1, report=report))
                    continue
                files = self._validated_source_files(child, report)
                manifest_path = child / "workbench-manifest.json"
                managed_manifest = self._validated_managed_unreviewed_manifest(
                    child,
                    manifest_path,
                    files,
                )
                if managed_manifest:
                    manifest_meeting_id = managed_manifest.get("meeting_id")
                    normalized_manifest_id = None
                    if manifest_meeting_id is not None:
                        if (
                            not isinstance(manifest_meeting_id, str)
                            or not manifest_meeting_id.strip()
                        ):
                            if report is not None:
                                report.errors += 1
                            continue
                        normalized_manifest_id = normalize_meeting_id(manifest_meeting_id)
                    discovered = self._partition_directory(
                        child,
                        files,
                        "draft",
                        min(99, 50 + managed_manifest["attempt"]),
                    )
                    if (
                        len(discovered) != 1
                        or (
                            normalized_manifest_id is not None
                            and discovered[0].meeting_id != normalized_manifest_id
                        )
                        or (
                            normalized_manifest_id is None
                            and not discovered[0].meeting_id.startswith("vm-")
                        )
                    ):
                        if report is not None:
                            report.errors += 1
                        continue
                    bundles.extend(discovered)
                    continue
                if self._looks_like_managed_unreviewed_manifest(manifest_path):
                    # 一级受管目录身份或 Relay 状态异常时必须隔离，不能回退成
                    # 普通 archive/100 并被误标为已发布。
                    if report is not None:
                        report.errors += 1
                    continue
                bundles.extend(self._partition_directory(child, files, "archive", 100))
            except RECOVERABLE_SOURCE_ERRORS:
                if report is not None:
                    report.errors += 1
        return bundles

    @staticmethod
    def _looks_like_managed_unreviewed_manifest(manifest_path: Path) -> bool:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return False
        try:
            manifest = load_json_file(manifest_path)
        except (OSError, ValueError, RecursionError):
            return False
        return bool(
            isinstance(manifest, dict)
            and manifest.get("schema_version") == 1
            and isinstance(manifest.get("job_id"), str)
            and type(manifest.get("attempt")) is int
            and manifest.get("status") != "published"
        )

    def _discover_pending_reviews(
        self, root: Path, *, report: ScanReport | None = None
    ) -> list[SourceBundle]:
        try:
            if not root.is_dir() or root.is_symlink():
                return []
        except RECOVERABLE_SOURCE_ERRORS:
            if report is not None:
                report.errors += 1
            return []
        bundles: list[SourceBundle] = []
        for directory in sorted(root.iterdir()):
            try:
                if (
                    not directory.is_dir()
                    or directory.is_symlink()
                    or directory.name.startswith(".")
                ):
                    continue
                files = self._validated_source_files(directory, report)
                manifest = self._validated_managed_unreviewed_manifest(
                    directory,
                    directory / "workbench-manifest.json",
                    files,
                )
                if not manifest:
                    continue
                manifest_meeting_id = manifest.get("meeting_id")
                normalized_manifest_id = None
                if manifest_meeting_id is not None:
                    if not isinstance(manifest_meeting_id, str) or not manifest_meeting_id.strip():
                        continue
                    normalized_manifest_id = normalize_meeting_id(manifest_meeting_id)
                discovered = self._partition_directory(
                    directory,
                    files,
                    "draft",
                    min(99, 50 + manifest["attempt"]),
                )
                if len(discovered) != 1:
                    continue
                discovered_id = discovered[0].meeting_id
                if normalized_manifest_id is not None:
                    if discovered_id != normalized_manifest_id:
                        continue
                elif not discovered_id.startswith("vm-"):
                    continue
                bundles.extend(discovered)
            except RECOVERABLE_SOURCE_ERRORS:
                if report is not None:
                    report.errors += 1
        return bundles

    def _discover_workbench_drafts(
        self, root: Path, *, report: ScanReport | None = None
    ) -> list[SourceBundle]:
        try:
            if not root.is_dir():
                return []
        except RECOVERABLE_SOURCE_ERRORS:
            if report is not None:
                report.errors += 1
            return []
        bundles: list[SourceBundle] = []
        job_dirs = []
        for path in sorted(root.iterdir()):
            try:
                if path.is_dir() and not path.is_symlink():
                    job_dirs.append(path)
            except RECOVERABLE_SOURCE_ERRORS:
                if report is not None:
                    report.errors += 1
        for job_dir in job_dirs:
            attempt_dirs = []
            for path in sorted(job_dir.iterdir()):
                try:
                    if path.is_dir() and not path.is_symlink():
                        attempt_dirs.append(path)
                except RECOVERABLE_SOURCE_ERRORS:
                    if report is not None:
                        report.errors += 1
            for attempt_dir in attempt_dirs:
                try:
                    manifest = attempt_dir / "workbench-manifest.json"
                    files = self._validated_source_files(attempt_dir, report)
                    match = re.search(r"attempt-(\d+)$", attempt_dir.name)
                    attempt_no = int(match.group(1)) if match else 0
                    if not self._validated_managed_unreviewed_manifest(
                        attempt_dir,
                        manifest,
                        files,
                        expected_job_id=job_dir.name,
                        expected_attempt=attempt_no,
                    ):
                        continue
                    bundles.extend(
                        self._partition_directory(
                            attempt_dir,
                            files,
                            "draft",
                            min(99, 50 + attempt_no),
                        )
                    )
                except RECOVERABLE_SOURCE_ERRORS:
                    if report is not None:
                        report.errors += 1
        return bundles

    def _validated_managed_unreviewed_manifest(
        self,
        directory: Path,
        manifest_path: Path,
        files: list[Path],
        *,
        expected_job_id: str | None = None,
        expected_attempt: int | None = None,
    ) -> dict[str, Any] | None:
        if (
            directory.is_symlink()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
            or not self.settings.relay_jobs_db.is_file()
        ):
            return None
        try:
            manifest = load_json_file(manifest_path)
        except (OSError, ValueError, RecursionError):
            return None
        schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
        job_id = manifest.get("job_id") if isinstance(manifest, dict) else None
        attempt_no = manifest.get("attempt") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or type(schema_version) is not int
            or schema_version != 1
            or not isinstance(job_id, str)
            or not job_id.strip()
            or type(attempt_no) is not int
            or attempt_no <= 0
            or (expected_job_id is not None and job_id != expected_job_id)
            or (expected_attempt is not None and attempt_no != expected_attempt)
        ):
            return None
        if self._is_committed_publish_manifest(directory, manifest):
            return None
        minutes_only = manifest.get("requested_stage") == "minutes_generating"
        input_transcript_sha256 = manifest.get("input_transcript_sha256")
        if minutes_only and (
            not isinstance(input_transcript_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", input_transcript_sha256)
        ):
            return None
        entries = manifest.get("artifacts")
        if not isinstance(entries, list) or not entries:
            return None
        listed: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            value = entry.get("path")
            if not isinstance(value, str):
                return None
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                return None
            candidate = directory / relative
            if candidate.is_symlink() or not candidate.is_file():
                return None
            expected_bytes = entry.get("bytes")
            expected_sha256 = entry.get("sha256")
            if (
                type(expected_bytes) is not int
                or expected_bytes < 0
                or not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or expected_bytes != candidate.stat().st_size
                or expected_sha256 != sha256_file(candidate)
            ):
                return None
            if not relative.parts or relative.parts[0] != "whisper-ref":
                listed.add(str(relative))
        physical = {
            str(path.relative_to(directory))
            for path in files
            if path.name != "workbench-manifest.json"
            and path.relative_to(directory).parts[0] != "whisper-ref"
        }
        if listed != physical:
            return None
        kinds = {artifact_kind(path) for path in files}
        required = (
            {"audio"}
            if minutes_only
            else {"audio", "srt", "txt", "speaker_map", "funasr_json", "funasr_log"}
        )
        if required - kinds:
            return None
        if minutes_only:
            input_transcript = directory / input_transcript_name(manifest)
            if (
                input_transcript.is_symlink()
                or not input_transcript.is_file()
                or sha256_file(input_transcript) != input_transcript_sha256
            ):
                return None
        if not topic_minutes_pair(files) and not ({"minutes_md", "minutes_html"} <= kinds):
            return None
        uri = f"file:{self.settings.relay_jobs_db.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            job_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            failure_stage_expression = (
                "failure_stage" if "failure_stage" in job_columns else "NULL AS failure_stage"
            )
            job = connection.execute(
                f"""SELECT status, current_attempt, archive_dir, {failure_stage_expression}
                      FROM jobs WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
        if (
            not job
            or job["status"] not in {"completed_unreviewed", "draft_modified"}
            or job["failure_stage"] == "pending_archive"
        ):
            return None
        try:
            archived = Path(job["archive_dir"]).expanduser().resolve()
        except (TypeError, OSError):
            return None
        if (
            type(job["current_attempt"]) is not int
            or job["current_attempt"] != attempt_no
            or archived != directory.resolve()
        ):
            return None
        return manifest

    def _is_committed_publish_manifest(self, directory: Path, manifest: dict[str, Any]) -> bool:
        if manifest.get("status") != "published":
            return False
        meeting_id = manifest.get("meeting_id")
        publish_token = manifest.get("publish_token")
        if not isinstance(meeting_id, str) or not isinstance(publish_token, str):
            return False
        meeting = self.db.query_one(
            "SELECT status, canonical_dir, source_priority FROM meetings WHERE id=?",
            (meeting_id,),
        )
        if (
            not meeting
            or meeting["status"] not in {"published", "draft_modified"}
            or int(meeting.get("source_priority") or 0) < 100
            or not meeting.get("canonical_dir")
            or Path(meeting["canonical_dir"]).resolve() != directory.resolve()
        ):
            return False
        for row in self.db.query_all(
            """SELECT payload_json FROM events
               WHERE meeting_id=? AND event_type='publish_files_committed'
               ORDER BY id DESC""",
            (meeting_id,),
        ):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("publish_token") == publish_token:
                return True
        return False

    def _discover_root(
        self,
        root: Path,
        source_root: str,
        priority: int,
        *,
        report: ScanReport | None = None,
    ) -> list[SourceBundle]:
        if not root.exists() or root.is_symlink():
            return []
        children = []
        for child in sorted(root.iterdir()):
            try:
                if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
                    children.append(child)
            except RECOVERABLE_SOURCE_ERRORS:
                if report is not None:
                    report.errors += 1
        if not children:
            children = [root]
        bundles: list[SourceBundle] = []
        for directory in children:
            try:
                files = self._validated_source_files(directory, report)
                bundles.extend(self._partition_directory(directory, files, source_root, priority))
            except RECOVERABLE_SOURCE_ERRORS:
                if report is not None:
                    report.errors += 1
        return bundles

    @staticmethod
    def _files_under(directory: Path) -> list[Path]:
        files = []
        for path in directory.rglob("*"):
            if not path.is_file() or is_noise(path):
                continue
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS and not (suffix == "" and "原文" in path.name):
                continue
            files.append(path)
        return sorted(files)

    def _validated_source_files(self, directory: Path, report: ScanReport | None) -> list[Path]:
        valid: list[Path] = []
        for path in self._files_under(directory):
            if path.suffix.lower() == ".json":
                try:
                    load_json_file(path)
                except RECOVERABLE_SOURCE_ERRORS:
                    if report is not None:
                        report.errors += 1
                    continue
            valid.append(path)
        return valid

    def _partition_directory(
        self,
        directory: Path,
        files: list[Path],
        source_root: str,
        priority: int,
    ) -> list[SourceBundle]:
        if not files:
            return []
        manifest_ids: set[str] = set()
        for manifest in (path for path in files if path.name == "workbench-manifest.json"):
            try:
                payload = load_json_file(manifest)
                if isinstance(payload, dict) and isinstance(payload.get("meeting_id"), str):
                    manifest_ids.add(normalize_meeting_id(payload["meeting_id"]))
            except (OSError, ValueError):
                pass
        directory_ids = ids_in_text(directory.name)
        filename_ids: dict[Path, set[str]] = {}
        for path in files:
            filename_ids[path] = ids_in_text(str(path.relative_to(directory)))
        explicit_ids = manifest_ids | directory_ids | set().union(*filename_ids.values())
        if explicit_ids:
            default_id = None
            for candidates in (manifest_ids, directory_ids, explicit_ids):
                if len(candidates) == 1:
                    default_id = next(iter(candidates))
                    break
            assigned: dict[str, list[Path]] = {meeting_id: [] for meeting_id in explicit_ids}
            for path, found in filename_ids.items():
                relevant = found & explicit_ids
                if len(relevant) == 1:
                    assigned[next(iter(relevant))].append(path)
                elif not relevant and default_id:
                    assigned[default_id].append(path)
            return [
                SourceBundle(meeting_id, directory, source_root, priority, sorted(group_files))
                for meeting_id, group_files in sorted(assigned.items())
                if group_files
            ]

        content_ids: dict[Path, set[str]] = {}
        for path in files:
            found: set[str] = set()
            if artifact_kind(path) in {"minutes_md", "transcript_md", "manifest"}:
                try:
                    if path.stat().st_size <= 5 * 1024 * 1024:
                        found = ids_in_text(path.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
            content_ids[path] = found
        all_ids = set().union(*content_ids.values())
        if all_ids:
            assigned: dict[str, list[Path]] = {meeting_id: [] for meeting_id in all_ids}
            for path, found in content_ids.items():
                relevant = found & all_ids
                if len(relevant) == 1:
                    assigned[next(iter(relevant))].append(path)
                elif len(all_ids) == 1:
                    assigned[next(iter(all_ids))].append(path)
            return [
                SourceBundle(meeting_id, directory, source_root, priority, sorted(group_files))
                for meeting_id, group_files in sorted(assigned.items())
                if group_files
            ]
        audio_files = [path for path in files if artifact_kind(path) == "audio"]
        if audio_files:
            by_fingerprint: dict[str, list[Path]] = {}
            for audio in audio_files:
                _sha256, pcm_sha256 = self._audio_fingerprints(audio)
                by_fingerprint.setdefault(pcm_sha256, []).append(audio)
            if len(by_fingerprint) == 1:
                fingerprint = next(iter(by_fingerprint))
                return [
                    SourceBundle(f"fp-{fingerprint[:24]}", directory, source_root, priority, files)
                ]
            bundles = []
            for fingerprint, audios in by_fingerprint.items():
                stems = {audio.stem.replace(".16k", "") for audio in audios}
                related = [
                    path
                    for path in files
                    if path in audios or any(stem and stem in path.stem for stem in stems)
                ]
                bundles.append(
                    SourceBundle(
                        f"fp-{fingerprint[:24]}", directory, source_root, priority, sorted(related)
                    )
                )
            return bundles
        meaningful = [
            path
            for path in files
            if artifact_kind(path) in {"srt", "txt", "transcript_md", "funasr_json", "whisper_json"}
        ]
        if meaningful:
            transcript_hash = self._transcript_hash(meaningful[0])
            fallback = (
                transcript_hash or hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
            )
            return [
                SourceBundle(f"legacy-{fallback[:24]}", directory, source_root, priority, files)
            ]
        return []

    def _coalesce_bundles(
        self, bundles: list[SourceBundle], *, report: ScanReport | None = None
    ) -> dict[str, list[SourceBundle]]:
        if not bundles:
            return {}
        explicit: list[tuple[SourceBundle, set[str]]] = []
        unidentified_audio: list[tuple[SourceBundle, set[str]]] = []
        legacy: list[SourceBundle] = []
        for bundle in bundles:
            try:
                audio_keys: set[str] = set()
                for audio in (path for path in bundle.files if artifact_kind(path) == "audio"):
                    sha, pcm = self._audio_fingerprints(audio)
                    audio_keys.update((f"sha:{sha}", f"pcm:{pcm}"))
                if bundle.meeting_id.startswith("vm-"):
                    explicit.append((bundle, audio_keys))
                elif audio_keys:
                    unidentified_audio.append((bundle, audio_keys))
                else:
                    legacy.append(bundle)
            except RECOVERABLE_SOURCE_ERRORS:
                if report is not None:
                    report.errors += 1
        grouped: dict[str, list[SourceBundle]] = {}

        def indexed_owners(bundle: SourceBundle) -> set[str]:
            paths = [str(path) for path in bundle.files]
            if not paths:
                return set()
            placeholders = ",".join("?" for _ in paths)
            return {
                str(row["meeting_id"])
                for row in self.db.query_all(
                    f"SELECT DISTINCT meeting_id FROM artifacts WHERE path IN ({placeholders})",
                    paths,
                )
            }

        explicit_owners: dict[str, set[str]] = {}
        for bundle, audio_keys in explicit:
            grouped.setdefault(bundle.meeting_id, []).append(bundle)
            for key in audio_keys:
                explicit_owners.setdefault(key, set()).add(bundle.meeting_id)
        for bundle, audio_keys in unidentified_audio:
            candidates = set().union(*(explicit_owners.get(key, set()) for key in audio_keys))
            if len(candidates) > 1:
                if report is not None:
                    report.errors += 1
                continue
            if candidates:
                meeting_id = next(iter(candidates))
            else:
                owners = indexed_owners(bundle)
                if len(owners) > 1:
                    if report is not None:
                        report.errors += 1
                    continue
                meeting_id = next(iter(owners)) if owners else bundle.meeting_id
            grouped.setdefault(meeting_id, []).append(bundle)
        for bundle in legacy:
            owners = indexed_owners(bundle)
            if len(owners) > 1:
                if report is not None:
                    report.errors += 1
                continue
            meeting_id = next(iter(owners)) if owners else bundle.meeting_id
            grouped.setdefault(meeting_id, []).append(bundle)
        return grouped

    def _audio_fingerprints(self, path: Path) -> tuple[str, str]:
        stat = path.stat()
        cached = self.db.query_one(
            "SELECT * FROM fingerprint_cache WHERE path = ? AND size_bytes = ? AND mtime_ns = ?",
            (str(path), stat.st_size, stat.st_mtime_ns),
        )
        if cached:
            return cached["sha256"], cached["pcm_sha256"] or cached["sha256"]
        sha = sha256_file(path)
        pcm = normalized_pcm_fingerprint(path)
        self.db.execute(
            """INSERT INTO fingerprint_cache(path, size_bytes, mtime_ns, sha256, pcm_sha256, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET size_bytes=excluded.size_bytes,
                 mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,
                 pcm_sha256=excluded.pcm_sha256, updated_at=excluded.updated_at""",
            (str(path), stat.st_size, stat.st_mtime_ns, sha, pcm, utc_now()),
        )
        return sha, pcm

    def _transcript_hash(self, path: Path) -> str | None:
        _, segments = self._parse_transcript(path)
        text = "".join(re.sub(r"\W+", "", segment.get("text", "")) for segment in segments)
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None

    def _import_meeting(
        self, meeting_id: str, bundles: list[SourceBundle], report: ScanReport
    ) -> None:
        bundles.sort(key=lambda bundle: (bundle.priority, len(bundle.files)), reverse=True)
        canonical = bundles[0]
        draft_bundles = sorted(
            (bundle for bundle in bundles if bundle.source_root == "draft"),
            key=lambda bundle: (bundle.priority, len(bundle.files)),
            reverse=True,
        )
        working_bundles = [
            *draft_bundles,
            *[bundle for bundle in bundles if bundle not in draft_bundles],
        ]
        minutes_only_drafts = [
            bundle for bundle in draft_bundles if self._is_minutes_only_bundle(bundle)
        ]
        transcript_bundles = [
            bundle for bundle in working_bundles if bundle not in minutes_only_drafts
        ]
        working_source = draft_bundles[0] if draft_bundles else canonical
        target_status = self._imported_status(working_source)
        source_job_id = self._manifest_value(working_source, "job_id") or self._manifest_value(
            canonical, "job_id"
        )
        all_files = [path for bundle in bundles for path in bundle.files]
        current_source_signature = source_signature(all_files)
        existing = self.db.query_one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        current_version = None
        current_minutes = None
        if existing and existing.get("current_transcript_version_id"):
            current_version = self.db.query_one(
                """SELECT tv.kind,
                          (SELECT COUNT(*) FROM segments s WHERE s.version_id=tv.id) AS segment_count
                     FROM transcript_versions tv WHERE tv.id=?""",
                (existing["current_transcript_version_id"],),
            )
        if existing and existing.get("current_minutes_version_id"):
            current_minutes = self.db.query_one(
                "SELECT kind FROM minutes_versions WHERE id=?",
                (existing["current_minutes_version_id"],),
            )
        transcript_is_draft = bool(current_version and current_version["kind"] == "draft")
        minutes_is_draft = bool(current_minutes and current_minutes["kind"] == "draft")
        if minutes_only_drafts:
            target_status = (
                "draft_modified"
                if transcript_is_draft or minutes_is_draft
                else "completed_unreviewed"
            )
        indexed_sources_changed = bool(
            existing and self._indexed_sources_changed(meeting_id, working_bundles)
        )
        if existing and self._is_safe_managed_whisper_refresh(
            meeting_id,
            existing,
            source_job_id,
            working_source,
            working_bundles,
            current_source_signature,
            indexed_sources_changed=indexed_sources_changed,
        ):
            self._apply_managed_whisper_refresh(
                meeting_id,
                existing,
                bundles,
                transcript_bundles,
                working_source,
                current_source_signature,
                source_job_id,
                report,
            )
            return
        if existing and self._is_pure_draft_relocation(
            meeting_id,
            existing,
            source_job_id,
            canonical,
            working_source,
            indexed_sources_changed=indexed_sources_changed,
        ):
            self._apply_draft_relocation(
                meeting_id,
                existing,
                bundles,
                working_source,
                current_source_signature,
                source_job_id,
                report,
            )
            return
        if (
            existing
            and existing["source_signature"] == current_source_signature
            and not indexed_sources_changed
        ):
            self._upsert_artifacts(meeting_id, bundles, report)
            # 源文件没变也要修掉历史遗留的编号标题：纪要一旦补上，签名会变，
            # 这里的占位会在完整路径里被真实标题覆盖。
            if str(existing["title"] or "").casefold() == meeting_id.casefold() and not (
                self._has_manual_title(meeting_id)
            ):
                self.db.execute(
                    "UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?",
                    (self._untitled_label(meeting_id), utc_now(), meeting_id),
                )
            # 历史记录里编号时间戳被当成 UTC 存过，会让日期整体偏一个时区。
            recorded_at = self._recording_date(meeting_id, canonical.directory)
            if recorded_at and recorded_at != existing["recording_date"]:
                self.db.execute(
                    "UPDATE meetings SET recording_date = ?, updated_at = ? WHERE id = ?",
                    (recorded_at, utc_now(), meeting_id),
                )
            if draft_bundles and existing["status"] != target_status:
                self.db.execute(
                    "UPDATE meetings SET status = ?, updated_at = ? WHERE id = ?",
                    (target_status, utc_now(), meeting_id),
                )
            elif existing["status"] == "completed_unreviewed" and target_status == "published":
                self.db.execute(
                    "UPDATE meetings SET status = 'published', updated_at = ? WHERE id = ?",
                    (utc_now(), meeting_id),
                )
            if source_job_id and not existing.get("source_job_id"):
                self.db.execute(
                    "UPDATE meetings SET source_job_id = ?, updated_at = ? WHERE id = ?",
                    (source_job_id, utc_now(), meeting_id),
                )
            if (
                not existing["current_transcript_version_id"]
                or not current_version
                or int(current_version.get("segment_count") or 0) == 0
            ):
                transcript_file = self._preferred_transcript(transcript_bundles)
                if transcript_file:
                    self._store_transcript(meeting_id, transcript_file, report)
            self._ensure_reference_transcript(meeting_id, transcript_bundles, report)
            if not existing["current_minutes_version_id"]:
                self._import_minutes(meeting_id, working_bundles)
            else:
                self._backfill_current_minutes_provenance(meeting_id, working_bundles)
            return
        if existing and minutes_only_drafts:
            if (transcript_is_draft and indexed_sources_changed) or self.db.conflicts.has(
                meeting_id, "external_source_change"
            ):
                self._capture_external_change(
                    meeting_id,
                    working_bundles,
                    transcript_bundles,
                    current_source_signature,
                    report,
                )
                return
            minutes_bundle = minutes_only_drafts[0]
            expected_hash = self._manifest_value(minutes_bundle, "input_transcript_sha256")
            current_hashes = self._current_transcript_snapshot_hashes(meeting_id)
            self._upsert_artifacts(meeting_id, bundles, report)
            if expected_hash and expected_hash in current_hashes:
                generated_minutes_id = self._import_minutes(
                    meeting_id,
                    [minutes_bundle],
                    kind="generated",
                    published=False,
                    make_current=not minutes_is_draft,
                )
                if minutes_is_draft:
                    self._open_external_change_conflict(
                        meeting_id,
                        current_source_signature,
                        report,
                        external_minutes_version_id=generated_minutes_id,
                        changed_resources=["minutes"],
                    )
                else:
                    self.db.execute(
                        """UPDATE meetings SET status=?, source_signature=?,
                           source_job_id=COALESCE(?, source_job_id), updated_at=?
                           WHERE id=?""",
                        (
                            target_status,
                            current_source_signature,
                            source_job_id,
                            utc_now(),
                            meeting_id,
                        ),
                    )
                    # 重生成的纪要写在新归档目录里，它才是这场会的规范位置；
                    # 标题也要跟着从纪要里取，否则会一直停在占位名上。
                    if int(existing["source_priority"] or 0) < minutes_bundle.priority:
                        regenerated_title = self._display_title(minutes_bundle, meeting_id)
                        keep_title = self._has_manual_title(meeting_id) or self.is_untitled(
                            regenerated_title, meeting_id
                        )
                        self.db.execute(
                            """UPDATE meetings SET canonical_dir=?, source_priority=?,
                               title=CASE WHEN ? THEN title ELSE ? END, updated_at=?
                               WHERE id=?""",
                            (
                                str(minutes_bundle.directory),
                                minutes_bundle.priority,
                                int(keep_title),
                                regenerated_title,
                                utc_now(),
                                meeting_id,
                            ),
                        )
                self.db.add_event(
                    "minutes_generation_imported",
                    meeting_id=meeting_id,
                    job_id=source_job_id,
                    payload={"input_transcript_sha256": expected_hash},
                )
            else:
                self._import_minutes(
                    meeting_id,
                    [minutes_bundle],
                    kind="stale_generated",
                    published=False,
                    make_current=False,
                )
                self.db.execute(
                    """UPDATE meetings SET source_signature=?,
                       source_job_id=COALESCE(?, source_job_id), updated_at=? WHERE id=?""",
                    (current_source_signature, source_job_id, utc_now(), meeting_id),
                )
                self.db.add_event(
                    "stale_minutes_generation_ignored",
                    meeting_id=meeting_id,
                    job_id=source_job_id,
                    payload={
                        "expected_sha256": expected_hash,
                        "current_sha256": sorted(current_hashes),
                    },
                )
            return
        if existing and (transcript_is_draft or minutes_is_draft):
            self._capture_external_change(
                meeting_id,
                working_bundles,
                transcript_bundles,
                current_source_signature,
                report,
            )
            return
        audio = self._preferred_file(bundles, {"audio"})
        audio_hash = sha256_file(audio) if audio else None
        recording_date = self._recording_date(meeting_id, canonical.directory)
        title = self._display_title(canonical, meeting_id)
        if existing and self._has_manual_title(meeting_id):
            title = existing["title"]
        with self.db.transaction() as connection:
            if not existing:
                connection.execute(
                    """INSERT INTO meetings
                       (id, title, recording_date, status, canonical_dir, source_priority,
                        source_signature, source_job_id, original_audio_sha256, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        meeting_id,
                        title,
                        recording_date,
                        target_status,
                        str(canonical.directory),
                        canonical.priority,
                        current_source_signature,
                        source_job_id,
                        audio_hash,
                        utc_now(),
                        utc_now(),
                    ),
                )
                report.meetings_created += 1
            else:
                connection.execute(
                    """UPDATE meetings SET title = ?, recording_date = ?, status = ?, canonical_dir = ?,
                       source_priority = ?, source_signature = ?,
                       source_job_id = COALESCE(?, source_job_id),
                       original_audio_sha256 = COALESCE(original_audio_sha256, ?), updated_at = ?
                       WHERE id = ?""",
                    (
                        title,
                        recording_date,
                        target_status,
                        str(canonical.directory),
                        canonical.priority,
                        current_source_signature,
                        source_job_id,
                        audio_hash,
                        utc_now(),
                        meeting_id,
                    ),
                )
        self._upsert_artifacts(meeting_id, bundles, report)
        transcript_file = self._preferred_transcript(transcript_bundles)
        if transcript_file:
            self._store_transcript(meeting_id, transcript_file, report)
        self._ensure_reference_transcript(meeting_id, transcript_bundles, report)
        self._import_minutes(meeting_id, working_bundles)
        self.db.add_event(
            "meeting_imported",
            meeting_id=meeting_id,
            payload={"canonical_dir": str(canonical.directory)},
        )

    def _is_safe_managed_whisper_refresh(
        self,
        meeting_id: str,
        existing: dict[str, Any],
        source_job_id: str | None,
        working_source: SourceBundle,
        bundles: list[SourceBundle],
        signature: str,
        *,
        indexed_sources_changed: bool,
    ) -> bool:
        if (
            working_source.source_root != "draft"
            or existing.get("status") not in {"completed_unreviewed", "draft_modified"}
            or int(existing.get("source_priority") or 0) >= 100
            or not source_job_id
            or existing.get("source_job_id") != source_job_id
            or (existing.get("source_signature") == signature and not indexed_sources_changed)
        ):
            return False
        manifest = self._manifest_payload(working_source)
        if (
            not manifest
            or manifest.get("job_id") != source_job_id
            or type(manifest.get("attempt")) is not int
        ):
            return False
        old_value = existing.get("canonical_dir")
        if not isinstance(old_value, str) or not old_value:
            return False
        old_directory = Path(old_value)
        new_directory = working_source.directory
        if not new_directory.is_dir():
            return False
        if old_directory != new_directory:
            if old_directory.exists():
                return False
            if not self._is_managed_draft_relocation_path(old_directory, new_directory):
                return False

        indexed_content, indexed_whisper = self._indexed_refresh_snapshots(
            meeting_id, old_directory
        )
        current_content, current_whisper = self._current_refresh_snapshots(bundles, new_directory)
        if indexed_content != current_content or indexed_whisper == current_whisper:
            return False
        return indexed_whisper.keys() <= current_whisper.keys()

    def _is_managed_draft_relocation_path(self, old_directory: Path, new_directory: Path) -> bool:
        archive_root = self.settings.archive_root
        try:
            old_directory.relative_to(archive_root / ".workbench-drafts")
            old_is_hidden = True
        except ValueError:
            old_is_hidden = False
        old_is_legacy = old_directory.parent == archive_root / "待校对"
        new_is_legacy = new_directory.parent == archive_root / "待校对"
        new_is_flat = (
            new_directory.parent == archive_root
            and not new_directory.name.startswith(".")
            and new_directory.name != "待校对"
        )
        return (old_is_hidden or old_is_legacy) and (new_is_legacy or new_is_flat)

    def _indexed_refresh_snapshots(
        self, meeting_id: str, managed_directory: Path
    ) -> tuple[list[tuple[str, str, str, str]], dict[tuple[str, str, str], str]]:
        content: list[tuple[str, str, str, str]] = []
        whisper: dict[tuple[str, str, str], str] = {}
        for row in self.db.query_all(
            """SELECT source_root, path, kind, sha256 FROM artifacts
               WHERE meeting_id=? AND source_root IN ('archive', 'draft', 'history', 'staging')""",
            (meeting_id,),
        ):
            source_root = str(row["source_root"])
            path = Path(row["path"])
            kind = str(row["kind"])
            logical_path = self._refresh_logical_path(path, source_root, managed_directory)
            digest = str(row.get("sha256") or "")
            key = (source_root, logical_path, kind)
            if source_root in {"draft", "staging"} and kind.startswith("whisper_"):
                whisper[key] = digest
            else:
                content.append((*key, digest))
        return sorted(content), whisper

    def _current_refresh_snapshots(
        self, bundles: list[SourceBundle], managed_directory: Path
    ) -> tuple[list[tuple[str, str, str, str]], dict[tuple[str, str, str], str]]:
        content: list[tuple[str, str, str, str]] = []
        whisper: dict[tuple[str, str, str], str] = {}
        for bundle in bundles:
            for path in bundle.files:
                kind = artifact_kind(path)
                logical_path = self._refresh_logical_path(
                    path, bundle.source_root, managed_directory
                )
                digest = sha256_file(path)
                key = (bundle.source_root, logical_path, kind)
                if bundle.source_root in {"draft", "staging"} and kind.startswith("whisper_"):
                    whisper[key] = digest
                else:
                    content.append((*key, digest))
        return sorted(content), whisper

    @staticmethod
    def _refresh_logical_path(path: Path, source_root: str, managed_directory: Path) -> str:
        if source_root == "draft":
            try:
                return f"managed/{path.relative_to(managed_directory)}"
            except ValueError:
                pass
        return str(path)

    def _apply_managed_whisper_refresh(
        self,
        meeting_id: str,
        existing: dict[str, Any],
        bundles: list[SourceBundle],
        transcript_bundles: list[SourceBundle],
        working_source: SourceBundle,
        signature: str,
        source_job_id: str | None,
        report: ScanReport,
    ) -> None:
        old_directory = Path(existing["canonical_dir"])
        relocated = old_directory != working_source.directory
        self._upsert_artifacts(meeting_id, bundles, report)
        self._ensure_reference_transcript(meeting_id, transcript_bundles, report)
        stale_artifact_ids = []
        if relocated:
            for row in self.db.query_all(
                "SELECT id, path FROM artifacts WHERE meeting_id=? AND source_root='draft'",
                (meeting_id,),
            ):
                try:
                    Path(row["path"]).relative_to(old_directory)
                except ValueError:
                    continue
                stale_artifact_ids.append(row["id"])
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE meetings SET canonical_dir=?, source_priority=?, source_signature=?,
                          source_job_id=COALESCE(?, source_job_id), updated_at=? WHERE id=?""",
                (
                    str(working_source.directory),
                    working_source.priority,
                    signature,
                    source_job_id,
                    utc_now(),
                    meeting_id,
                ),
            )
            if stale_artifact_ids:
                connection.executemany(
                    "DELETE FROM artifacts WHERE id=?",
                    [(artifact_id,) for artifact_id in stale_artifact_ids],
                )
            if relocated:
                relocation_payload = {
                    "from": str(old_directory),
                    "to": str(working_source.directory),
                    "job_id": source_job_id,
                }
                connection.execute(
                    """INSERT INTO events
                       (meeting_id, job_id, event_type, actor, payload_json, created_at)
                       VALUES (?, ?, 'artifact_source_relocated', 'system', ?, ?)""",
                    (
                        meeting_id,
                        source_job_id,
                        json.dumps(relocation_payload, ensure_ascii=False),
                        utc_now(),
                    ),
                )
            connection.execute(
                """INSERT INTO events
                   (meeting_id, job_id, event_type, actor, payload_json, created_at)
                   VALUES (?, ?, 'whisper_reference_refreshed', 'system', ?, ?)""",
                (
                    meeting_id,
                    source_job_id,
                    json.dumps({"source_signature": signature}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def _is_pure_draft_relocation(
        self,
        meeting_id: str,
        existing: dict[str, Any],
        source_job_id: str | None,
        canonical: SourceBundle,
        working_source: SourceBundle,
        *,
        indexed_sources_changed: bool,
    ) -> bool:
        if (
            indexed_sources_changed
            or canonical.source_root != "draft"
            or working_source.source_root != "draft"
            or canonical.directory != working_source.directory
            or existing.get("status") not in {"completed_unreviewed", "draft_modified"}
            or int(existing.get("source_priority") or 0) >= 100
            or not source_job_id
            or existing.get("source_job_id") != source_job_id
        ):
            return False
        old_value = existing.get("canonical_dir")
        if not isinstance(old_value, str) or not old_value:
            return False
        old_directory = Path(old_value)
        new_directory = working_source.directory
        if old_directory == new_directory or old_directory.exists() or not new_directory.is_dir():
            return False
        if not self._is_managed_draft_relocation_path(old_directory, new_directory):
            return False

        old_content = []
        for row in self.db.query_all(
            "SELECT kind, path, sha256 FROM artifacts WHERE meeting_id=? AND source_root='draft'",
            (meeting_id,),
        ):
            try:
                Path(row["path"]).relative_to(old_directory)
            except ValueError:
                continue
            if (
                row["kind"] == "manifest"
                or str(row["kind"]).startswith("whisper_")
                or not row.get("sha256")
            ):
                continue
            old_content.append((row["kind"], row["sha256"]))
        if not old_content:
            return False

        manifest = self._manifest_payload(working_source) or {}
        manifest_hashes = {
            entry["path"]: entry["sha256"]
            for entry in manifest.get("artifacts", [])
            if isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha256"), str)
        }
        new_content = []
        for path in working_source.files:
            kind = artifact_kind(path)
            if kind == "manifest" or kind.startswith("whisper_"):
                continue
            relative = str(path.relative_to(new_directory))
            new_content.append((kind, manifest_hashes.get(relative) or sha256_file(path)))
        return sorted(old_content) == sorted(new_content)

    def _apply_draft_relocation(
        self,
        meeting_id: str,
        existing: dict[str, Any],
        bundles: list[SourceBundle],
        working_source: SourceBundle,
        signature: str,
        source_job_id: str | None,
        report: ScanReport,
    ) -> None:
        old_directory = Path(existing["canonical_dir"])
        self._upsert_artifacts(meeting_id, bundles, report)
        stale_artifact_ids = []
        for row in self.db.query_all(
            "SELECT id, path FROM artifacts WHERE meeting_id=? AND source_root='draft'",
            (meeting_id,),
        ):
            try:
                Path(row["path"]).relative_to(old_directory)
            except ValueError:
                continue
            stale_artifact_ids.append(row["id"])
        payload = {
            "from": str(old_directory),
            "to": str(working_source.directory),
            "job_id": source_job_id,
        }
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE meetings SET canonical_dir=?, source_priority=?, source_signature=?,
                          source_job_id=COALESCE(?, source_job_id), updated_at=? WHERE id=?""",
                (
                    str(working_source.directory),
                    working_source.priority,
                    signature,
                    source_job_id,
                    utc_now(),
                    meeting_id,
                ),
            )
            if stale_artifact_ids:
                connection.executemany(
                    "DELETE FROM artifacts WHERE id=?",
                    [(artifact_id,) for artifact_id in stale_artifact_ids],
                )
            connection.execute(
                """INSERT INTO events
                   (meeting_id, job_id, event_type, actor, payload_json, created_at)
                   VALUES (?, ?, 'artifact_source_relocated', 'system', ?, ?)""",
                (meeting_id, source_job_id, json.dumps(payload, ensure_ascii=False), utc_now()),
            )

    def _capture_external_change(
        self,
        meeting_id: str,
        bundles: list[SourceBundle],
        transcript_bundles: list[SourceBundle],
        signature: str,
        report: ScanReport,
    ) -> None:
        transcript_id = None
        transcript_file = self._preferred_transcript(transcript_bundles)
        if transcript_file:
            transcript_id = self._store_transcript(
                meeting_id, transcript_file, report, make_current=False
            )
            if transcript_id and not self._transcript_version_changed(meeting_id, transcript_id):
                transcript_id = None
        self._ensure_reference_transcript(meeting_id, transcript_bundles, report)
        minutes_id = self._import_minutes(meeting_id, bundles, make_current=False)
        if minutes_id and not self._minutes_version_changed(meeting_id, minutes_id):
            minutes_id = None
        changed_resources = []
        if transcript_id:
            changed_resources.append("transcript")
        if minutes_id:
            changed_resources.append("minutes")
        if not changed_resources:
            changed_resources.append("artifacts")
        self._upsert_artifacts(meeting_id, bundles, report)
        self._open_external_change_conflict(
            meeting_id,
            signature,
            report,
            external_transcript_version_id=transcript_id,
            external_minutes_version_id=minutes_id,
            changed_resources=changed_resources,
        )

    def _open_external_change_conflict(
        self,
        meeting_id: str,
        signature: str,
        report: ScanReport,
        *,
        external_transcript_version_id: str | None = None,
        external_minutes_version_id: str | None = None,
        changed_resources: list[str],
    ) -> None:
        already_open = self.db.conflicts.has(meeting_id, "external_source_change")
        payload = {
            "changed_resources": changed_resources,
            "external_transcript_version_id": external_transcript_version_id,
            "external_minutes_version_id": external_minutes_version_id,
            "source_signature": signature,
        }
        self.db.conflicts.open(
            meeting_id,
            "external_source_change",
            source_signature=signature,
            payload=payload,
        )
        if not already_open:
            self.db.add_event("external_change_conflict", meeting_id=meeting_id, payload=payload)
            report.conflicts += 1

    def _transcript_version_changed(self, meeting_id: str, candidate_id: str) -> bool:
        current = self.db.query_one(
            """SELECT tv.id, tv.kind, tv.based_on_id
               FROM transcript_versions tv
               JOIN meetings m ON m.current_transcript_version_id=tv.id
               WHERE m.id=?""",
            (meeting_id,),
        )
        if not current:
            return True
        baseline_id = (
            current["based_on_id"]
            if current["kind"] == "draft" and current.get("based_on_id")
            else current["id"]
        )
        if candidate_id == baseline_id:
            return False
        return self._transcript_content_hash(candidate_id) != self._transcript_content_hash(
            baseline_id
        )

    def _transcript_content_hash(self, version_id: str) -> str | None:
        segments = self.db.query_all(
            """SELECT ordinal, start_ms, end_ms, speaker_label, speaker_name, text
               FROM segments WHERE version_id=? ORDER BY ordinal""",
            (version_id,),
        )
        if not segments:
            return None
        encoded = json.dumps(
            segments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _minutes_version_changed(self, meeting_id: str, candidate_id: str) -> bool:
        current = self.db.query_one(
            """SELECT mv.id, mv.kind, mv.based_on_id
               FROM minutes_versions mv
               JOIN meetings m ON m.current_minutes_version_id=mv.id
               WHERE m.id=?""",
            (meeting_id,),
        )
        if not current:
            return True
        baseline_id = (
            current["based_on_id"]
            if current["kind"] == "draft" and current.get("based_on_id")
            else current["id"]
        )
        if not baseline_id:
            return True
        candidate = self.db.query_one(
            "SELECT content_sha256, markdown FROM minutes_versions WHERE id=?",
            (candidate_id,),
        )
        baseline = self.db.query_one(
            "SELECT content_sha256, markdown FROM minutes_versions WHERE id=?",
            (baseline_id,),
        )
        if not candidate or not baseline:
            return True
        candidate_hash = (
            candidate.get("content_sha256")
            or hashlib.sha256(candidate["markdown"].encode("utf-8")).hexdigest()
        )
        baseline_hash = (
            baseline.get("content_sha256")
            or hashlib.sha256(baseline["markdown"].encode("utf-8")).hexdigest()
        )
        return candidate_hash != baseline_hash

    def _indexed_sources_changed(self, meeting_id: str, bundles: list[SourceBundle]) -> bool:
        current_paths: dict[str, Path] = {
            str(path): path
            for bundle in bundles
            if bundle.source_root in {"archive", "staging", "history"}
            for path in bundle.files
        }
        indexed = {
            row["path"]: row
            for row in self.db.query_all(
                """SELECT path, sha256, size_bytes, mtime_ns FROM artifacts
                   WHERE meeting_id=? AND source_root IN ('archive', 'staging', 'history')""",
                (meeting_id,),
            )
        }
        if set(current_paths) != set(indexed):
            return True
        for value, path in current_paths.items():
            row = indexed[value]
            stat = path.stat()
            metadata_matches = (
                row.get("size_bytes") == stat.st_size and row.get("mtime_ns") == stat.st_mtime_ns
            )
            if metadata_matches and path.suffix.lower() in AUDIO_EXTENSIONS:
                continue
            if not row.get("sha256") or sha256_file(path) != row["sha256"]:
                return True
        return False

    def _has_manual_title(self, meeting_id: str) -> bool:
        rows = self.db.query_all(
            """SELECT payload_json FROM events
               WHERE meeting_id=? AND event_type='meeting_metadata_updated' AND actor='user'
               ORDER BY id DESC""",
            (meeting_id,),
        )
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            fields = payload.get("fields") if isinstance(payload, dict) else None
            if isinstance(fields, list) and "title" in fields:
                return True
        return False

    @staticmethod
    def _imported_status(canonical: SourceBundle) -> str:
        manifest = next(
            (path for path in canonical.files if path.name == "workbench-manifest.json"), None
        )
        if manifest:
            try:
                payload = load_json_file(manifest)
                status = payload.get("status") if isinstance(payload, dict) else None
                if status == "published" and canonical.priority < 100:
                    return "completed_unreviewed"
                if status in {
                    "completed_unreviewed",
                    "draft_modified",
                    "published",
                    "failed",
                    "cancelled",
                    "interrupted",
                }:
                    return status
            except (OSError, ValueError):
                pass
        return "published" if canonical.priority >= 100 else "completed_unreviewed"

    @staticmethod
    def _manifest_value(canonical: SourceBundle, key: str) -> str | None:
        payload = ArchiveImporter._manifest_payload(canonical)
        value = payload.get(key) if payload else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _manifest_payload(bundle: SourceBundle) -> dict[str, Any] | None:
        manifest = next(
            (path for path in bundle.files if path.name == "workbench-manifest.json"), None
        )
        if not manifest:
            return None
        try:
            payload = load_json_file(manifest)
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _is_minutes_only_bundle(bundle: SourceBundle) -> bool:
        payload = ArchiveImporter._manifest_payload(bundle)
        return isinstance(payload, dict) and payload.get("requested_stage") == (
            "minutes_generating"
        )

    def _current_transcript_snapshot_hashes(self, meeting_id: str) -> set[str]:
        """当前逐字稿在各纪要协议下的快照哈希。

        v1/v2 把纯文本交给 Agent，v3 起改成 SRT。回收纪要时不知道 attempt 用的是
        哪一版，两个都算出来比对，避免把新生成的纪要误判成过期版本。
        """
        segments = self.db.query_all(
            """SELECT s.* FROM segments s JOIN meetings m
               ON m.current_transcript_version_id=s.version_id
               WHERE m.id=? ORDER BY s.ordinal""",
            (meeting_id,),
        )
        hashes: set[str] = set()
        rendered = render_transcript_txt(segments).rstrip()
        if rendered:
            hashes.add(hashlib.sha256((rendered + "\n").encode("utf-8")).hexdigest())
        srt = render_transcript_srt(segments)
        if srt:
            hashes.add(hashlib.sha256(srt.encode("utf-8")).hexdigest())
        return hashes

    def _store_transcript(
        self,
        meeting_id: str,
        transcript_file: Path,
        report: ScanReport,
        *,
        kind_override: str | None = None,
        make_current: bool = True,
    ) -> str | None:
        source_hash = sha256_file(transcript_file)
        parsed_kind, segments = self._parse_transcript(transcript_file)
        if not segments:
            return None
        existing = self.db.query_one(
            """SELECT id FROM transcript_versions
               WHERE meeting_id = ? AND source_path = ? AND source_sha256 = ?""",
            (meeting_id, str(transcript_file), source_hash),
        )
        repaired = False
        created = False
        with self.db.transaction() as connection:
            if existing:
                version_id = existing["id"]
                segment_count = connection.execute(
                    "SELECT COUNT(*) FROM segments WHERE version_id=?", (version_id,)
                ).fetchone()[0]
                if int(segment_count) == 0:
                    self.db.replace_segments_with_connection(
                        connection, version_id, meeting_id, segments
                    )
                    repaired = True
            else:
                version_id = self.db.create_transcript_version_with_connection(
                    connection,
                    meeting_id,
                    kind_override or parsed_kind,
                    published=make_current,
                    source_path=str(transcript_file),
                    source_sha256=source_hash,
                    make_current=False,
                )
                self.db.replace_segments_with_connection(
                    connection, version_id, meeting_id, segments
                )
                created = True
            if make_current:
                connection.execute(
                    "UPDATE meetings SET current_transcript_version_id = ?, updated_at = ? WHERE id = ?",
                    (version_id, utc_now(), meeting_id),
                )
            if repaired:
                connection.execute(
                    """INSERT INTO events
                       (meeting_id, event_type, actor, payload_json, created_at)
                       VALUES (?, 'transcript_version_repaired', 'system', ?, ?)""",
                    (
                        meeting_id,
                        json.dumps({"version_id": version_id}, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        if make_current:
            self._sync_speakers_and_duration(meeting_id, segments)
        if created:
            report.versions_imported += 1
        return version_id

    def _ensure_reference_transcript(
        self, meeting_id: str, bundles: list[SourceBundle], report: ScanReport
    ) -> None:
        reference = None
        for kinds in ({"whisper_srt"}, {"whisper_json"}, {"whisper_txt"}):
            reference = self._preferred_file(bundles, kinds)
            if reference:
                break
        if reference:
            self._store_transcript(
                meeting_id,
                reference,
                report,
                kind_override="whisper_reference",
                make_current=False,
            )

    def _upsert_artifacts(
        self, meeting_id: str, bundles: list[SourceBundle], report: ScanReport
    ) -> None:
        prepared = []
        for bundle in bundles:
            for path in bundle.files:
                stat = path.stat()
                existing = self.db.query_one(
                    "SELECT id, meeting_id, sha256, size_bytes, mtime_ns FROM artifacts WHERE path = ?",
                    (str(path),),
                )
                if (
                    existing
                    and existing["sha256"]
                    and existing["size_bytes"] == stat.st_size
                    and existing["mtime_ns"] == stat.st_mtime_ns
                    and path.suffix.lower() in AUDIO_EXTENSIONS
                ):
                    file_hash = existing["sha256"]
                else:
                    file_hash = sha256_file(path)
                prepared.append((bundle, path, stat, artifact_kind(path), file_hash, existing))
        with self.db.transaction() as connection:
            for bundle, path, stat, kind, file_hash, existing in prepared:
                connection.execute(
                    """INSERT INTO artifacts
                       (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
                       VALUES (?, ?, 'source', ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET
                         meeting_id=excluded.meeting_id, kind=excluded.kind,
                         source_root=excluded.source_root, sha256=excluded.sha256,
                         size_bytes=excluded.size_bytes,
                         mtime_ns=excluded.mtime_ns""",
                    (
                        meeting_id,
                        kind,
                        bundle.source_root,
                        str(path),
                        file_hash,
                        stat.st_size,
                        stat.st_mtime_ns,
                        utc_now(),
                    ),
                )
                if existing and existing["meeting_id"] != meeting_id:
                    connection.execute(
                        """INSERT INTO events
                           (meeting_id, event_type, actor, payload_json, created_at)
                           VALUES (?, 'artifact_identity_reassigned', 'system', ?, ?)""",
                        (
                            meeting_id,
                            json.dumps(
                                {
                                    "artifact_id": existing["id"],
                                    "path": str(path),
                                    "previous_meeting_id": existing["meeting_id"],
                                    "new_meeting_id": meeting_id,
                                },
                                ensure_ascii=False,
                            ),
                            utc_now(),
                        ),
                    )
                report.artifacts_seen += 1

    @staticmethod
    def _preferred_file(bundles: list[SourceBundle], kinds: set[str]) -> Path | None:
        degraded_fallback = None
        for bundle in bundles:
            for path in bundle.files:
                if artifact_kind(path) in kinds:
                    if not is_degraded(path):
                        return path
                    degraded_fallback = degraded_fallback or path
        return degraded_fallback

    def _preferred_transcript(self, bundles: list[SourceBundle]) -> Path | None:
        for kinds in ({"srt"}, {"funasr_json"}, {"txt"}, {"transcript_md"}, {"whisper_json"}):
            path = self._preferred_file(bundles, kinds)
            if path:
                return path
        return None

    def _import_minutes(
        self,
        meeting_id: str,
        bundles: list[SourceBundle],
        *,
        kind: str | None = None,
        published: bool | None = None,
        make_current: bool = True,
    ) -> str | None:
        markdown_path = None
        html_path = None
        selected_bundle = None
        for bundle in bundles:
            bundle_markdown = None
            bundle_html = None
            for path in bundle.files:
                if is_degraded(path) or "转写" in path.name or "原文" in path.name:
                    continue
                if artifact_kind(path) == "minutes_md" and (
                    path.name == "会议纪要.md" or "纪要" in path.name
                ):
                    bundle_markdown = bundle_markdown or path
                if artifact_kind(path) == "minutes_html" and "纪要" in path.name:
                    bundle_html = bundle_html or path
            if not bundle_markdown and not bundle_html:
                pair = topic_minutes_pair(bundle.files)
                if pair:
                    bundle_markdown, bundle_html = pair
            if bundle_markdown or bundle_html:
                markdown_path, html_path = bundle_markdown, bundle_html
                selected_bundle = bundle
                break
        if not markdown_path and not html_path:
            return None
        markdown = (
            markdown_path.read_text(encoding="utf-8-sig", errors="replace") if markdown_path else ""
        )
        raw_html = (
            html_path.read_text(encoding="utf-8-sig", errors="replace") if html_path else None
        )
        imported_html = safe_imported_minutes_html(markdown, raw_html)
        if published is None:
            published = bool(
                selected_bundle
                and selected_bundle.source_root == "archive"
                and selected_bundle.priority >= 100
            )
        if kind is None:
            kind = "imported" if published else "generated"
        provenance = self._manifest_payload(selected_bundle) if selected_bundle else None
        source_job_id = provenance.get("job_id") if provenance else None
        source_attempt = (
            provenance.get("attempt", provenance.get("source_attempt")) if provenance else None
        )
        requested_stage = provenance.get("requested_stage") if provenance else None
        input_transcript_sha256 = provenance.get("input_transcript_sha256") if provenance else None
        if not isinstance(source_job_id, str):
            source_job_id = None
        if not isinstance(source_attempt, int):
            source_attempt = None
        if not isinstance(requested_stage, str):
            requested_stage = None
        if not isinstance(input_transcript_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", input_transcript_sha256
        ):
            input_transcript_sha256 = None
        content_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        existing = self.db.query_one(
            """SELECT id FROM minutes_versions
               WHERE meeting_id=? AND content_sha256=? AND kind=?
                 AND source_job_id IS ? AND source_attempt IS ?
                 AND requested_stage IS ? AND input_transcript_sha256 IS ?
               ORDER BY version_no DESC LIMIT 1""",
            (
                meeting_id,
                content_sha256,
                kind,
                source_job_id,
                source_attempt,
                requested_stage,
                input_transcript_sha256,
            ),
        )
        if existing:
            if make_current:
                self.db.execute(
                    "UPDATE meetings SET current_minutes_version_id=?, updated_at=? WHERE id=?",
                    (existing["id"], utc_now(), meeting_id),
                )
            return str(existing["id"])
        minutes_id = f"mv-{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            meeting = connection.execute(
                "SELECT current_minutes_version_id FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            based_on_id = meeting["current_minutes_version_id"] if meeting else None
            version_no = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM minutes_versions WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO minutes_versions
                   (id, meeting_id, version_no, based_on_id, markdown, html, kind, published,
                    content_sha256,
                    source_job_id, source_attempt, requested_stage,
                    input_transcript_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    minutes_id,
                    meeting_id,
                    version_no,
                    based_on_id,
                    markdown,
                    imported_html,
                    kind,
                    int(published),
                    content_sha256,
                    source_job_id,
                    source_attempt,
                    requested_stage,
                    input_transcript_sha256,
                    utc_now(),
                ),
            )
            if make_current:
                connection.execute(
                    "UPDATE meetings SET current_minutes_version_id = ?, updated_at = ? WHERE id = ?",
                    (minutes_id, utc_now(), meeting_id),
                )
        return minutes_id

    def _backfill_current_minutes_provenance(
        self, meeting_id: str, bundles: list[SourceBundle]
    ) -> None:
        current = self.db.query_one(
            """SELECT mv.* FROM minutes_versions mv JOIN meetings m
               ON m.current_minutes_version_id = mv.id
               WHERE m.id = ?""",
            (meeting_id,),
        )
        if (
            not current
            or current.get("kind") not in {"generated", "imported", "stale_generated"}
            or (current.get("source_job_id") and isinstance(current.get("source_attempt"), int))
        ):
            return
        provenance = None
        for bundle in bundles:
            has_minutes = any(
                artifact_kind(path) in {"minutes_md", "minutes_html"} and not is_degraded(path)
                for path in bundle.files
            ) or topic_minutes_pair(bundle.files)
            if has_minutes:
                provenance = self._manifest_payload(bundle)
                if provenance:
                    break
        if not provenance:
            return
        source_job_id = provenance.get("job_id")
        source_attempt = provenance.get("attempt", provenance.get("source_attempt"))
        if not isinstance(source_job_id, str) or not isinstance(source_attempt, int):
            return
        requested_stage = provenance.get("requested_stage")
        input_digest = provenance.get("input_transcript_sha256")
        self.db.execute(
            """UPDATE minutes_versions
               SET source_job_id = COALESCE(source_job_id, ?),
                   source_attempt = COALESCE(source_attempt, ?),
                   requested_stage = COALESCE(requested_stage, ?),
                   input_transcript_sha256 = COALESCE(input_transcript_sha256, ?)
               WHERE id = ?""",
            (
                source_job_id,
                source_attempt,
                requested_stage if isinstance(requested_stage, str) else None,
                input_digest
                if isinstance(input_digest, str) and re.fullmatch(r"[0-9a-f]{64}", input_digest)
                else None,
                current["id"],
            ),
        )

    def _sync_speakers_and_duration(self, meeting_id: str, segments: list[dict[str, Any]]) -> None:
        duration_ms = max((int(segment.get("end_ms", 0)) for segment in segments), default=0)
        speakers: dict[str, str | None] = {}
        for segment in segments:
            if segment.get("speaker_label"):
                speakers[segment["speaker_label"]] = segment.get("speaker_name")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE meetings SET duration_ms = CASE WHEN COALESCE(duration_ms, 0) > ? THEN duration_ms ELSE ? END WHERE id = ?",
                (duration_ms, duration_ms, meeting_id),
            )
            for label, display_name in speakers.items():
                speaker_id = (
                    f"speaker-{hashlib.sha1(f'{meeting_id}:{label}'.encode()).hexdigest()[:20]}"
                )
                connection.execute(
                    """INSERT INTO speakers(id, meeting_id, label, display_name)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(meeting_id, label) DO UPDATE SET
                         display_name=COALESCE(excluded.display_name, speakers.display_name)""",
                    (speaker_id, meeting_id, label, display_name),
                )

    @staticmethod
    def _parse_transcript(path: Path) -> tuple[str, list[dict[str, Any]]]:
        kind = artifact_kind(path)
        if kind in {"srt", "whisper_srt"}:
            return "funasr", parse_srt(path)
        if kind == "funasr_json":
            return "funasr", parse_funasr_json(path)
        if kind == "whisper_json":
            return "whisper", parse_whisper_json(path)
        return "legacy_txt", parse_txt(path)

    @classmethod
    def _display_title(cls, bundle: SourceBundle, meeting_id: str) -> str:
        if bundle.source_root == "draft":
            minutes = next(
                (path for path in bundle.files if artifact_kind(path) == "minutes_md"), None
            )
            if not minutes:
                pair = topic_minutes_pair(bundle.files)
                minutes = pair[0] if pair else None
            if minutes:
                try:
                    heading = re.search(
                        r"(?m)^#\s+(.+?)\s*$",
                        minutes.read_text(encoding="utf-8-sig", errors="replace")[:20_000],
                    )
                    if heading:
                        title = re.sub(
                            r"^(?:会议纪要[：:]?\s*)|(?:会议纪要|纪要)$",
                            "",
                            heading.group(1).strip(),
                        ).strip()
                        if title:
                            return title
                except OSError:
                    pass
            return cls._untitled_label(meeting_id)
        name = bundle.directory.name.strip()
        if not name or name.casefold() == meeting_id.casefold():
            return cls._untitled_label(meeting_id)
        return name

    @staticmethod
    def _untitled_label(meeting_id: str) -> str:
        """纪要缺失时的占位标题。

        编号对人没有检索价值；退成“日期 + 未命名录音”至少能按时间线认出来，
        纪要补齐后下一轮扫描会自动换回真实标题。
        """
        match = re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", meeting_id)
        if not match:
            return UNTITLED_TITLE_SUFFIX
        return f"{match.group(1)[2:]}{match.group(2)}{match.group(3)} {UNTITLED_TITLE_SUFFIX}"

    @staticmethod
    def is_untitled(title: str, meeting_id: str) -> bool:
        stripped = (title or "").strip()
        if not stripped:
            return True
        if stripped.casefold() == meeting_id.casefold():
            return True
        return stripped.endswith(UNTITLED_TITLE_SUFFIX)

    @staticmethod
    def _recording_date(meeting_id: str, directory: Path) -> str | None:
        match = re.search(
            r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)[-_]?([0-2]\d)?([0-5]\d)?([0-5]\d)?", meeting_id
        )
        if match:
            values = match.groups(default="00")
            try:
                # 录音编号里的时间戳是录音当时的本机墙上时间，不是 UTC。
                # 标成 UTC 会让凌晨的会议在界面上退到前一天，和归档目录名对不上。
                return (
                    datetime(
                        int(values[0]),
                        int(values[1]),
                        int(values[2]),
                        int(values[3]),
                        int(values[4]),
                        int(values[5]),
                    )
                    .astimezone()
                    .isoformat()
                )
            except ValueError:
                pass
        try:
            return datetime.fromtimestamp(directory.stat().st_mtime, tz=UTC).isoformat()
        except OSError:
            return None
