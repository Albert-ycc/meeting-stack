from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .archive_lock import ArchiveLock
from .db import Database, utc_now
from .importer import (
    SUPPORTED_EXTENSIONS,
    artifact_kind,
    input_transcript_name,
    is_noise,
    normalize_meeting_id,
    source_signature,
)
from .parsers import load_json_file
from .rendering import render_safe_markdown, render_transcript_srt, render_transcript_txt
from .minutes_evidence import (
    MinutesEvidenceError,
    load_minutes_manifest,
    load_relay_attempt,
    relay_attempt_matches_minutes,
    select_source_srt_entry,
)
from .glossary import record_corrections_from_diff


_BASE_VERSION_UNSET = object()


class MeetingServiceError(RuntimeError):
    pass


class NotFoundError(MeetingServiceError):
    pass


class ConflictError(MeetingServiceError):
    pass


class PublishValidationError(MeetingServiceError):
    pass


@dataclass(frozen=True, slots=True)
class PublishResult:
    meeting_id: str
    transcript_version_id: str
    minutes_version_id: str | None
    archive_dir: str
    manifest_path: str


@dataclass(frozen=True, slots=True)
class AttemptPublishProvenance:
    public_metadata: dict[str, Any]
    source_directory: Path
    source_manifest_path: Path
    source_manifest_sha256: str
    files: dict[str, tuple[Path, str]]
    relay_attempt: dict[str, Any]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_srt_time(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    if source.is_symlink() or not source.is_file() or sha256_file(source) != expected_sha256:
        raise PublishValidationError("attempt 来源文件已变化，禁止发布")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        temporary_path = Path(temporary)
        if (
            sha256_file(temporary_path) != expected_sha256
            or sha256_file(source) != expected_sha256
        ):
            raise PublishValidationError("attempt 来源文件在复制期间发生变化，禁止发布")
        os.replace(temporary_path, destination)
        fsync_directory(destination.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_directory_tree(path: Path) -> None:
    """Remove a tree while tolerating ExFAT AppleDouble entries vanishing mid-walk."""

    def onexc(_function: Callable[..., Any], _target: str | Path, error: BaseException) -> None:
        if isinstance(error, FileNotFoundError):
            return
        raise error

    try:
        shutil.rmtree(path, onexc=onexc)
    except FileNotFoundError:
        pass


def _link_or_copy(source: str, destination: str) -> str:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        clonefile = getattr(libc, "clonefile", None)
        if clonefile is not None:
            clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            clonefile.restype = ctypes.c_int
            if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
                return destination
    return shutil.copy2(source, destination, follow_symlinks=False)


def atomic_swap_directories(left: Path, right: Path) -> None:
    """Swap two directories atomically on macOS, with a rollback fallback elsewhere."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        at_fdcwd = -2
        rename_swap = 0x00000002
        result = renameatx_np(
            at_fdcwd,
            os.fsencode(left),
            at_fdcwd,
            os.fsencode(right),
            rename_swap,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{left} <-> {right}")

    rollback = right.parent / f".{right.name}.workbench-rollback-{uuid.uuid4().hex}"
    os.replace(right, rollback)
    try:
        os.replace(left, right)
    except Exception:
        os.replace(rollback, right)
        raise
    os.replace(rollback, left)


class MeetingService:
    def __init__(
        self,
        db: Database,
        archive_root: str | Path | None = None,
        source_signature_resolver: Callable[[str], str | None] | None = None,
        relay_jobs_db: str | Path | None = None,
    ):
        self.db = db
        self.archive_root = Path(archive_root).expanduser() if archive_root else None
        self.source_signature_resolver = source_signature_resolver
        self.relay_jobs_db = Path(relay_jobs_db).expanduser() if relay_jobs_db else None
        self.archive_lock = ArchiveLock(self.db.path.parent / "archive.lock")
        with self.archive_lock:
            self._recover_publish_journals()

    def ensure_draft(self, meeting_id: str) -> str:
        with self.db.transaction() as connection:
            draft_id, based_on_id, _segment_id_map = self._ensure_draft_with_connection(
                connection, meeting_id
            )
        self._record_draft_created(meeting_id, draft_id, based_on_id)
        return draft_id

    def _ensure_draft_with_connection(
        self, connection: Any, meeting_id: str, *, force_new: bool = False
    ) -> tuple[str, str | None, dict[str, str]]:
        meeting = connection.execute(
            "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
        ).fetchone()
        if not meeting:
            raise NotFoundError("会议不存在")
        current_id = meeting["current_transcript_version_id"]
        if not current_id:
            raise PublishValidationError("会议没有可编辑逐字稿")
        current = connection.execute(
            "SELECT kind FROM transcript_versions WHERE id=?", (current_id,)
        ).fetchone()
        if current and current["kind"] == "draft" and not force_new:
            return str(current_id), None, {}
        rows = connection.execute(
            "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (current_id,)
        ).fetchall()
        draft_id = f"tv-{uuid.uuid4().hex}"
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM transcript_versions WHERE meeting_id=?",
            (meeting_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO transcript_versions
               (id, meeting_id, version_no, kind, based_on_id, published, created_at)
               VALUES (?, ?, ?, 'draft', ?, 0, ?)""",
            (draft_id, meeting_id, version_no, current_id, utc_now()),
        )
        copied = []
        segment_id_map: dict[str, str] = {}
        for source in rows:
            segment = dict(source)
            new_segment_id = f"seg-{uuid.uuid4().hex}"
            segment_id_map[str(segment["id"])] = new_segment_id
            segment["id"] = new_segment_id
            copied.append(segment)
        self.db.replace_segments_with_connection(connection, draft_id, meeting_id, copied)
        connection.execute(
            """UPDATE meetings SET current_transcript_version_id=?,
               status='draft_modified', updated_at=? WHERE id=?""",
            (draft_id, utc_now(), meeting_id),
        )
        return draft_id, str(current_id), segment_id_map

    def _record_draft_created(
        self, meeting_id: str, draft_id: str, based_on_id: str | None
    ) -> None:
        if based_on_id is None:
            return
        self.db.add_event(
            "transcript_draft_created",
            meeting_id=meeting_id,
            actor="user",
            payload={"version_id": draft_id, "based_on_id": based_on_id},
        )

    def save_segments(
        self,
        meeting_id: str,
        segments: list[dict[str, Any]],
        expected_base_version_id: str | None | object = _BASE_VERSION_UNSET,
    ) -> str:
        with self.db.transaction() as connection:
            meeting = connection.execute(
                "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            if not meeting:
                raise NotFoundError("会议不存在")
            if (
                expected_base_version_id is not _BASE_VERSION_UNSET
                and meeting["current_transcript_version_id"] != expected_base_version_id
            ):
                raise ConflictError("逐字稿版本已变化，请保留本地内容并重新加载")
            version_id, based_on_id, segment_id_map = self._ensure_draft_with_connection(
                connection, meeting_id, force_new=True
            )
            write_segments = [dict(segment) for segment in segments]
            if based_on_id is not None:
                for segment in write_segments:
                    old_id = str(segment.get("id") or "")
                    segment["id"] = segment_id_map.get(old_id, f"seg-{uuid.uuid4().hex}")
            self.db.replace_segments_with_connection(
                connection, version_id, meeting_id, write_segments
            )
            connection.execute(
                "UPDATE meetings SET status='draft_modified', updated_at=? WHERE id=?",
                (utc_now(), meeting_id),
            )
        self._record_draft_created(meeting_id, version_id, based_on_id)
        self.db.add_event(
            "transcript_draft_saved",
            meeting_id=meeting_id,
            actor="user",
            payload={"version_id": version_id, "segment_count": len(segments)},
        )
        return version_id

    def rename_speaker(self, meeting_id: str, label: str, display_name: str) -> int:
        speaker_id = f"speaker-{hashlib.sha1(f'{meeting_id}:{label}'.encode()).hexdigest()[:20]}"
        with self.db.transaction() as connection:
            version_id, based_on_id, _segment_id_map = self._ensure_draft_with_connection(
                connection, meeting_id
            )
            cursor = connection.execute(
                "UPDATE segments SET speaker_name = ? WHERE version_id = ? AND speaker_label = ?",
                (display_name.strip(), version_id, label),
            )
            connection.execute(
                """INSERT INTO speakers(id, meeting_id, label, display_name)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(meeting_id, label) DO UPDATE SET display_name=excluded.display_name""",
                (speaker_id, meeting_id, label, display_name.strip()),
            )
        self._record_draft_created(meeting_id, version_id, based_on_id)
        self.db.add_event(
            "speaker_renamed",
            meeting_id=meeting_id,
            actor="user",
            payload={"label": label, "display_name": display_name.strip()},
        )
        return cursor.rowcount

    def split_segment(
        self, meeting_id: str, segment_id: str, character_index: int
    ) -> tuple[str, str]:
        with self.db.transaction() as connection:
            version_id, based_on_id, segment_id_map = self._ensure_draft_with_connection(
                connection, meeting_id
            )
            segment_id = segment_id_map.get(segment_id, segment_id)
            segments = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal",
                    (version_id,),
                ).fetchall()
            ]
            index = next((i for i, row in enumerate(segments) if row["id"] == segment_id), None)
            if index is None:
                raise NotFoundError("当前草稿中不存在该段落")
            segment = segments[index]
            text = segment["text"]
            if character_index <= 0 or character_index >= len(text):
                raise MeetingServiceError("拆分位置必须位于文本中间")
            ratio = character_index / len(text)
            midpoint = int(segment["start_ms"] + (segment["end_ms"] - segment["start_ms"]) * ratio)
            first_id = f"seg-{uuid.uuid4().hex}"
            second_id = f"seg-{uuid.uuid4().hex}"
            common = {
                "speaker_label": segment["speaker_label"],
                "speaker_name": segment["speaker_name"],
            }
            replacement = [
                {
                    **common,
                    "id": first_id,
                    "start_ms": segment["start_ms"],
                    "end_ms": midpoint,
                    "text": text[:character_index].strip(),
                },
                {
                    **common,
                    "id": second_id,
                    "start_ms": midpoint,
                    "end_ms": segment["end_ms"],
                    "text": text[character_index:].strip(),
                },
            ]
            segments[index : index + 1] = replacement
            for ordinal, item in enumerate(segments):
                item["ordinal"] = ordinal
            self.db.replace_segments_with_connection(connection, version_id, meeting_id, segments)
        self._record_draft_created(meeting_id, version_id, based_on_id)
        self.db.add_event(
            "segment_split",
            meeting_id=meeting_id,
            actor="user",
            payload={"source_segment_id": segment_id, "new_segment_ids": [first_id, second_id]},
        )
        return first_id, second_id

    def merge_segments(self, meeting_id: str, first_id: str, second_id: str) -> str:
        with self.db.transaction() as connection:
            version_id, based_on_id, segment_id_map = self._ensure_draft_with_connection(
                connection, meeting_id
            )
            first_id = segment_id_map.get(first_id, first_id)
            second_id = segment_id_map.get(second_id, second_id)
            segments = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal",
                    (version_id,),
                ).fetchall()
            ]
            positions = {row["id"]: index for index, row in enumerate(segments)}
            if first_id not in positions or second_id not in positions:
                raise NotFoundError("当前草稿中不存在待合并段落")
            first_index, second_index = positions[first_id], positions[second_id]
            if second_index != first_index + 1:
                raise MeetingServiceError("只能合并相邻段落")
            first, second = segments[first_index], segments[second_index]
            merged_id = f"seg-{uuid.uuid4().hex}"
            separator = "" if first["text"][-1:] in "，。！？；：" else " "
            merged = {
                "id": merged_id,
                "start_ms": first["start_ms"],
                "end_ms": second["end_ms"],
                "speaker_label": first["speaker_label"]
                if first["speaker_label"] == second["speaker_label"]
                else None,
                "speaker_name": first["speaker_name"]
                if first["speaker_name"] == second["speaker_name"]
                else None,
                "text": f"{first['text']}{separator}{second['text']}",
            }
            segments[first_index : second_index + 1] = [merged]
            for ordinal, item in enumerate(segments):
                item["ordinal"] = ordinal
            self.db.replace_segments_with_connection(connection, version_id, meeting_id, segments)
        self._record_draft_created(meeting_id, version_id, based_on_id)
        self.db.add_event(
            "segments_merged",
            meeting_id=meeting_id,
            actor="user",
            payload={"source_segment_ids": [first_id, second_id], "new_segment_id": merged_id},
        )
        return merged_id

    def save_minutes(
        self,
        meeting_id: str,
        markdown: str,
        expected_base_version_id: str | None | object = _BASE_VERSION_UNSET,
    ) -> str:
        minutes_id, _corrections = self.save_minutes_detailed(
            meeting_id, markdown, expected_base_version_id
        )
        return minutes_id

    def save_minutes_detailed(
        self,
        meeting_id: str,
        markdown: str,
        expected_base_version_id: str | None | object = _BASE_VERSION_UNSET,
        *,
        glossary_snapshot_path: Path | str | None = None,
        capture_corrections: bool = True,
        actor: str = "user",
        event_payload: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """保存纪要草稿，同时返回这次编辑捕获到的错字更正建议（含直接记入的）。

        纪要体检按词典替换错写时 capture_corrections=False：这些改动本来就来自词典，
        不该再回头变成待确认的纠错建议。
        """
        minutes_id = f"mv-{uuid.uuid4().hex}"
        rendered_html = render_safe_markdown(markdown)
        content_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        old_markdown: str | None = None
        scope = "通用"
        with self.db.transaction() as connection:
            meeting = connection.execute(
                "SELECT current_minutes_version_id FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            if not meeting:
                raise NotFoundError("会议不存在")
            current_id = meeting["current_minutes_version_id"]
            if (
                expected_base_version_id is not _BASE_VERSION_UNSET
                and current_id != expected_base_version_id
            ):
                raise ConflictError("纪要版本已变化，请保留本地内容并重新加载")
            based_on = (
                connection.execute(
                    "SELECT * FROM minutes_versions WHERE id=?", (current_id,)
                ).fetchone()
                if current_id
                else None
            )
            old_markdown = based_on["markdown"] if based_on else None
            project_row = connection.execute(
                """SELECT p.name FROM projects p
                   JOIN meetings m ON m.project_id = p.id
                   WHERE m.id=?""",
                (meeting_id,),
            ).fetchone()
            scope = project_row["name"] if project_row and project_row["name"] else "通用"
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
                   VALUES (?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, ?, ?, ?, ?)""",
                (
                    minutes_id,
                    meeting_id,
                    version_no,
                    current_id,
                    markdown,
                    rendered_html,
                    content_sha256,
                    based_on["source_job_id"] if based_on else None,
                    based_on["source_attempt"] if based_on else None,
                    based_on["requested_stage"] if based_on else None,
                    based_on["input_transcript_sha256"] if based_on else None,
                    utc_now(),
                ),
            )
            connection.execute(
                """UPDATE meetings SET current_minutes_version_id = ?, status = 'draft_modified',
                   updated_at = ? WHERE id = ?""",
                (minutes_id, utc_now(), meeting_id),
            )
        self.db.add_event(
            "minutes_saved",
            meeting_id=meeting_id,
            actor=actor,
            payload={"version_id": minutes_id, **(event_payload or {})},
        )
        if not capture_corrections:
            return minutes_id, []
        # 编辑纪要时捕获疑似错字更正，写入待确认队列（不影响纪要保存本身）
        corrections = record_corrections_from_diff(
            self.db,
            old_markdown,
            markdown,
            meeting_id=meeting_id,
            scope=scope,
            snapshot_path=glossary_snapshot_path,
        )
        return minutes_id, corrections

    def rollback_transcript(self, meeting_id: str, version_id: str) -> str:
        with self.db.transaction() as connection:
            version = connection.execute(
                "SELECT id FROM transcript_versions WHERE id = ? AND meeting_id = ?",
                (version_id, meeting_id),
            ).fetchone()
            if not version:
                raise NotFoundError("逐字稿版本不存在")
            rows = connection.execute(
                "SELECT * FROM segments WHERE version_id=? ORDER BY ordinal", (version_id,)
            ).fetchall()
            draft_id = self.db.create_transcript_version_with_connection(
                connection,
                meeting_id,
                "draft",
                based_on_id=version_id,
                make_current=False,
            )
            copied = []
            for row in rows:
                segment = dict(row)
                segment["id"] = f"seg-{uuid.uuid4().hex}"
                copied.append(segment)
            self.db.replace_segments_with_connection(connection, draft_id, meeting_id, copied)
            connection.execute(
                """UPDATE meetings SET current_transcript_version_id = ?,
                   status = 'draft_modified', updated_at = ? WHERE id = ?""",
                (draft_id, utc_now(), meeting_id),
            )
            self.db.sync_transcript_metadata_with_connection(connection, meeting_id, draft_id)
        self.db.add_event(
            "transcript_rolled_back",
            meeting_id=meeting_id,
            actor="user",
            payload={"version_id": draft_id, "based_on_id": version_id},
        )
        return draft_id

    def rollback_minutes(self, meeting_id: str, version_id: str) -> str:
        rollback_id = f"mv-{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            version = connection.execute(
                "SELECT * FROM minutes_versions WHERE id = ? AND meeting_id = ?",
                (version_id, meeting_id),
            ).fetchone()
            if not version:
                raise NotFoundError("纪要版本不存在")
            version_no = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM minutes_versions WHERE meeting_id=?",
                (meeting_id,),
            ).fetchone()[0]
            content_sha256 = (
                version["content_sha256"]
                or hashlib.sha256(version["markdown"].encode("utf-8")).hexdigest()
            )
            connection.execute(
                """INSERT INTO minutes_versions
                   (id, meeting_id, version_no, based_on_id, markdown, html, kind, published,
                    content_sha256, source_job_id, source_attempt, requested_stage,
                    input_transcript_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, ?, ?, ?, ?)""",
                (
                    rollback_id,
                    meeting_id,
                    version_no,
                    version_id,
                    version["markdown"],
                    render_safe_markdown(version["markdown"]),
                    content_sha256,
                    version["source_job_id"],
                    version["source_attempt"],
                    version["requested_stage"],
                    version["input_transcript_sha256"],
                    utc_now(),
                ),
            )
            connection.execute(
                """UPDATE meetings SET current_minutes_version_id = ?,
                   status = 'draft_modified', updated_at = ? WHERE id = ?""",
                (rollback_id, utc_now(), meeting_id),
            )
        self.db.add_event(
            "minutes_rolled_back",
            meeting_id=meeting_id,
            actor="user",
            payload={"version_id": rollback_id, "based_on_id": version_id},
        )
        return rollback_id

    def publish(self, meeting_id: str) -> PublishResult:
        with self.archive_lock:
            return self._publish_locked(meeting_id)

    def _publish_locked(self, meeting_id: str) -> PublishResult:
        try:
            if normalize_meeting_id(meeting_id) != meeting_id:
                raise ValueError
        except ValueError as error:
            raise PublishValidationError("meeting_id 不安全，禁止发布") from error
        meeting = self._meeting(meeting_id)
        if meeting["conflict"]:
            raise ConflictError("外部文件已变化，请先解决冲突")
        version_id = meeting.get("current_transcript_version_id")
        if not version_id:
            raise PublishValidationError("缺少逐字稿")
        canonical_value = meeting.get("canonical_dir")
        if not canonical_value:
            raise PublishValidationError("缺少正式归档目录")
        canonical_dir = Path(canonical_value)
        promoting = int(meeting.get("source_priority") or 0) < 100
        publish_token = uuid.uuid4().hex
        installing_new_directory = promoting
        if promoting:
            if not self.archive_root:
                raise PublishValidationError("缺少正式归档根目录配置")
            if self.archive_root.is_symlink() or not self.archive_root.is_dir():
                raise PublishValidationError("正式归档根目录不可用或是符号链接")
            if canonical_dir.parent.resolve() == self.archive_root.resolve():
                archive_dir = self._validated_archive_directory(canonical_dir)
                installing_new_directory = False
                work_dir = (
                    archive_dir.parent
                    / f".{archive_dir.name}.workbench-publish-{publish_token}"
                )
            else:
                archive_dir = self._new_archive_directory(meeting)
                if archive_dir.parent.resolve() != self.archive_root.resolve():
                    raise PublishValidationError("正式归档目标路径越界")
                if archive_dir.exists():
                    raise ConflictError(f"正式归档目录已存在：{archive_dir.name}")
                work_dir = self.archive_root / f".workbench-publish-{publish_token}"
        else:
            archive_dir = self._validated_archive_directory(canonical_dir)
            work_dir = archive_dir.parent / f".{archive_dir.name}.workbench-publish-{publish_token}"
        artifacts = self.db.query_all("SELECT * FROM artifacts WHERE meeting_id = ?", (meeting_id,))
        minutes_id = meeting.get("current_minutes_version_id")
        minutes = (
            self.db.query_one("SELECT * FROM minutes_versions WHERE id = ?", (minutes_id,))
            if minutes_id
            else None
        )
        if not minutes:
            raise PublishValidationError("缺少会议纪要")
        managed_draft = any(row.get("source_root") == "draft" for row in artifacts)
        attempt_provenance = self._attempt_publish_provenance(artifacts, meeting, minutes)
        minutes_only_metadata = self._minutes_only_publish_metadata(
            artifacts, meeting, minutes, attempt_provenance
        )
        minutes_protocol_metadata = self._minutes_protocol_publish_metadata(
            artifacts, meeting, minutes
        )
        if attempt_provenance:
            minutes_protocol_metadata.update(attempt_provenance.public_metadata)
        if promoting and minutes_only_metadata:
            raise PublishValidationError("仅重生成纪要的 attempt 不能创建新的正式归档")
        copying_managed_sources = promoting or (managed_draft and not minutes_only_metadata)
        managed_source_root = "draft" if copying_managed_sources else "archive"
        managed_canonical_dir = (
            attempt_provenance.source_directory
            if copying_managed_sources and attempt_provenance
            else None if copying_managed_sources else archive_dir
        )
        self._validate_source_artifacts(
            artifacts,
            source_root=managed_source_root,
            meeting_id=meeting_id,
            canonical_dir=managed_canonical_dir,
            require_complete=copying_managed_sources,
        )
        expected_hash = meeting.get("original_audio_sha256")
        audio_row = self._source_audio(
            artifacts,
            expected_hash,
            required=promoting or bool(expected_hash),
            formal=not promoting,
            canonical_dir=archive_dir,
            meeting_id=meeting_id,
            preferred_directory=(
                attempt_provenance.source_directory
                if promoting and attempt_provenance
                else None
            ),
        )
        audio_path = Path(audio_row["path"]) if audio_row else None
        before_hash = sha256_file(audio_path) if audio_path else None
        if expected_hash and before_hash is not None and before_hash != expected_hash:
            raise ConflictError("原音频哈希已变化，禁止发布")
        segments = self._current_segments(meeting_id)
        if not segments:
            raise PublishValidationError("逐字稿为空")
        meeting_snapshot = self._publish_meeting_snapshot(meeting)
        minutes_snapshot = self._publish_minutes_snapshot(minutes)
        segments_snapshot = self._publish_segments_snapshot(segments)
        transcript_text = self._normalized_transcript_text(segments)
        minutes_input = (
            self._normalized_transcript_srt(segments)
            if attempt_provenance
            else transcript_text
        )
        if (
            minutes_only_metadata
            and hashlib.sha256(minutes_input.encode("utf-8")).hexdigest()
            != (minutes_only_metadata["input_transcript_sha256"])
        ):
            raise PublishValidationError("逐字稿已在纪要生成后变化，请重新生成纪要再发布")
        swapped = False
        final_outputs: list[Path] = []
        manifest_path = archive_dir / "workbench-manifest.json"
        published_signature = ""
        journal_path: Path | None = None
        backup_dir = archive_dir.parent / f".{archive_dir.name}.workbench-backup-{publish_token}"
        strategy = "install" if installing_new_directory else "rename_swap"
        try:
            if installing_new_directory:
                work_dir.mkdir(parents=True, exist_ok=False)
            else:
                shutil.copytree(
                    archive_dir,
                    work_dir,
                    copy_function=_link_or_copy,
                    symlinks=True,
                )
            if copying_managed_sources:
                if not installing_new_directory:
                    self._remove_previous_managed_sources(work_dir)
                self._copy_required_sources(artifacts, work_dir, attempt_provenance)
            elif minutes_only_metadata and attempt_provenance:
                self._remove_previous_attempt_sidecars(work_dir)
                self._copy_attempt_sidecars(attempt_provenance, work_dir)
            outputs = {
                work_dir / f"{meeting_id}.srt": self._render_srt(segments),
                work_dir / f"{meeting_id}.txt": transcript_text,
                work_dir / "spk.txt": self._render_speakers(segments),
                work_dir / "会议纪要.md": minutes["markdown"],
                work_dir / "会议纪要.html": render_safe_markdown(minutes["markdown"]),
            }
            self._validate_output_paths(work_dir, outputs)
            if not promoting:
                self._backup_existing(outputs.keys(), work_dir)
            for path, content in outputs.items():
                atomic_write_text(path, content.rstrip() + "\n")
            if audio_path and sha256_file(audio_path) != before_hash:
                raise ConflictError("发布过程中原音频发生变化")
            published_audio = self._published_audio_path(
                audio_path,
                archive_dir,
                work_dir,
                promoting=copying_managed_sources,
            )
            if published_audio:
                if (
                    published_audio.is_symlink()
                    or not published_audio.is_file()
                    or sha256_file(published_audio) != before_hash
                ):
                    raise ConflictError("正式归档中的原音频哈希不一致")
                original_audio = {
                    "path": str(published_audio.relative_to(work_dir)),
                    "sha256": before_hash,
                }
            elif audio_path:
                original_audio = {
                    "path": None,
                    "source_path": str(audio_path),
                    "sha256": before_hash,
                }
            else:
                original_audio = None
            manifest = {
                "schema_version": 1,
                "meeting_id": meeting_id,
                "job_id": meeting.get("source_job_id"),
                "attempt": minutes.get("source_attempt"),
                "status": "published",
                "publish_token": publish_token,
                "published_at": utc_now(),
                "transcript_version_id": version_id,
                "minutes_version_id": minutes_id,
                "original_audio": original_audio,
                "artifacts": self._manifest_artifacts(work_dir),
            }
            if minutes_only_metadata:
                manifest.update(minutes_only_metadata)
            manifest.update(minutes_protocol_metadata)
            work_manifest = work_dir / "workbench-manifest.json"
            atomic_write_text(
                work_manifest, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            )
            prepared_manifest_sha256 = sha256_file(work_manifest)
            prepared_artifacts = manifest["artifacts"]
            journal_path = self._write_publish_journal(
                publish_token=publish_token,
                meeting_id=meeting_id,
                transcript_version_id=version_id,
                minutes_version_id=minutes_id,
                archive_dir=archive_dir,
                work_dir=work_dir,
                backup_dir=backup_dir,
                promoting=installing_new_directory,
                strategy=strategy,
                expected_artifacts=prepared_artifacts,
                expected_manifest_sha256=prepared_manifest_sha256,
            )
            relative_outputs = [path.relative_to(work_dir) for path in outputs]
            self._validate_source_artifacts(
                artifacts,
                source_root=managed_source_root,
                meeting_id=meeting_id,
                canonical_dir=managed_canonical_dir,
                require_complete=copying_managed_sources,
            )
            if attempt_provenance:
                self._validate_attempt_provenance(attempt_provenance)
            with self.db.transaction() as connection:
                self._assert_publish_snapshot_current(
                    connection,
                    meeting_id,
                    meeting_snapshot,
                    minutes_snapshot,
                    segments_snapshot,
                )
                if self._manifest_artifacts(work_dir) != prepared_artifacts:
                    raise ConflictError("发布准备期间文件发生变化，请重新发布")
                if installing_new_directory:
                    os.replace(work_dir, archive_dir)
                    swapped = True
                else:
                    try:
                        atomic_swap_directories(work_dir, archive_dir)
                        swapped = True
                    except OSError as error:
                        if error.errno not in {errno.ENOTSUP, errno.EINVAL}:
                            raise
                        strategy = "staged"
                        self._update_publish_journal(
                            journal_path, strategy=strategy, phase="prepared"
                        )
                        if backup_dir.exists() or backup_dir.is_symlink():
                            raise ConflictError("发布备份目录已存在，无法安全降级")
                        os.replace(archive_dir, backup_dir)
                        swapped = True
                        fsync_directory(archive_dir.parent)
                        self._update_publish_journal(journal_path, phase="old_moved")
                        os.replace(work_dir, archive_dir)
                fsync_directory(archive_dir.parent)
                self._update_publish_journal(journal_path, phase="new_installed")
                final_outputs = [archive_dir / relative for relative in relative_outputs]
                published_signature = self._record_published_artifacts(
                    connection,
                    meeting_id,
                    final_outputs,
                    manifest_path,
                    archive_dir,
                    include_all=copying_managed_sources or bool(attempt_provenance),
                )
                self._assert_published_files_current(
                    archive_dir,
                    prepared_artifacts,
                    prepared_manifest_sha256,
                    audio_path,
                    before_hash,
                )
                connection.execute(
                    """INSERT INTO events
                       (meeting_id, event_type, actor, payload_json, created_at)
                       VALUES (?, 'publish_files_committed', 'system', ?, ?)""",
                    (
                        meeting_id,
                        json.dumps(
                            {
                                "publish_token": publish_token,
                                "transcript_version_id": version_id,
                                "minutes_version_id": minutes_id,
                            },
                            ensure_ascii=False,
                        ),
                        utc_now(),
                    ),
                )
                connection.execute(
                    """UPDATE transcript_versions
                       SET published = CASE WHEN id = ? THEN 1 ELSE 0 END,
                           kind = CASE WHEN id = ? THEN 'published_edit' ELSE kind END
                       WHERE meeting_id = ?""",
                    (version_id, version_id, meeting_id),
                )
                connection.execute(
                    """UPDATE minutes_versions
                       SET published = CASE WHEN id = ? THEN 1 ELSE 0 END,
                           kind = CASE WHEN id = ? THEN 'published_edit' ELSE kind END
                       WHERE meeting_id = ?""",
                    (minutes_id, minutes_id, meeting_id),
                )
                updated = connection.execute(
                    """UPDATE meetings SET status = 'published',
                       canonical_dir = ?, source_priority = 100, source_signature = ?,
                       updated_at = ?
                       WHERE id = ? AND current_transcript_version_id = ?
                         AND current_minutes_version_id = ?""",
                    (
                        str(archive_dir),
                        published_signature,
                        utc_now(),
                        meeting_id,
                        version_id,
                        minutes_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ConflictError("发布前草稿版本已变化，请重新发布")
                self._assert_published_files_current(
                    archive_dir,
                    prepared_artifacts,
                    prepared_manifest_sha256,
                    audio_path,
                    before_hash,
                )
        except Exception:
            if swapped:
                try:
                    self._rollback_uncommitted_publish(
                        strategy=strategy,
                        archive_dir=archive_dir,
                        work_dir=work_dir,
                        backup_dir=backup_dir,
                        promoting=installing_new_directory,
                    )
                    fsync_directory(archive_dir.parent)
                except Exception as rollback_error:
                    raise MeetingServiceError(
                        "发布失败，且正式目录自动回滚失败"
                    ) from rollback_error
            if work_dir.exists():
                remove_directory_tree(work_dir)
            if journal_path is not None:
                self._remove_publish_journal(journal_path)
            raise
        try:
            self._assert_published_files_current(
                archive_dir,
                prepared_artifacts,
                prepared_manifest_sha256,
                audio_path,
                before_hash,
            )
        except (ConflictError, OSError) as error:
            conflict_payload = {"publish_token": publish_token, "error": str(error)}
            self.db.conflicts.open(
                meeting_id,
                "publish_post_commit",
                payload=conflict_payload,
            )
            self.db.add_event(
                "publish_post_commit_conflict",
                meeting_id=meeting_id,
                actor="system",
                payload=conflict_payload,
            )
            raise ConflictError(
                "发布事务已提交，但正式文件随后发生变化；已保留恢复副本并标记冲突"
            ) from error
        self._remove_recovery_directory(work_dir)
        self._remove_recovery_directory(backup_dir)
        if journal_path is not None:
            self._remove_publish_journal(journal_path)
        if self.source_signature_resolver:
            try:
                resolved_signature = self.source_signature_resolver(meeting_id)
            except Exception:
                resolved_signature = None
            if resolved_signature and resolved_signature != published_signature:
                self.db.execute(
                    "UPDATE meetings SET source_signature = ?, updated_at = ? WHERE id = ?",
                    (resolved_signature, utc_now(), meeting_id),
                )
        self.db.add_event(
            "meeting_published",
            meeting_id=meeting_id,
            actor="user",
            payload={"transcript_version_id": version_id, "minutes_version_id": minutes_id},
        )
        return PublishResult(
            meeting_id, version_id, minutes_id, str(archive_dir), str(manifest_path)
        )

    def _write_publish_journal(
        self,
        *,
        publish_token: str,
        meeting_id: str,
        transcript_version_id: str,
        minutes_version_id: str,
        archive_dir: Path,
        work_dir: Path,
        backup_dir: Path,
        promoting: bool,
        strategy: str,
        expected_artifacts: list[dict[str, Any]],
        expected_manifest_sha256: str,
    ) -> Path:
        root = self.archive_root or archive_dir.parent
        journal = root / f".workbench-publish-journal-{publish_token}.json"
        payload = {
            "schema_version": 3,
            "publish_token": publish_token,
            "meeting_id": meeting_id,
            "transcript_version_id": transcript_version_id,
            "minutes_version_id": minutes_version_id,
            "archive_dir": str(archive_dir),
            "work_dir": str(work_dir),
            "backup_dir": str(backup_dir),
            "promoting": promoting,
            "strategy": strategy,
            "phase": "prepared",
            "expected_artifacts": expected_artifacts,
            "expected_manifest_sha256": expected_manifest_sha256,
        }
        atomic_write_text(journal, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        fsync_directory(root)
        return journal

    def _update_publish_journal(self, journal: Path, **changes: Any) -> None:
        payload = load_json_file(journal)
        if not isinstance(payload, dict) or payload.get("schema_version") != 3:
            raise MeetingServiceError("发布日志格式无效")
        payload.update(changes)
        atomic_write_text(journal, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        fsync_directory(journal.parent)

    @staticmethod
    def _remove_recovery_directory(directory: Path) -> None:
        if directory.is_symlink():
            raise MeetingServiceError(f"恢复目录不能是符号链接：{directory.name}")
        if directory.is_dir():
            remove_directory_tree(directory)
        elif directory.exists():
            raise MeetingServiceError(f"恢复路径不是目录：{directory.name}")

    def _rollback_uncommitted_publish(
        self,
        *,
        strategy: str,
        archive_dir: Path,
        work_dir: Path,
        backup_dir: Path,
        promoting: bool,
    ) -> None:
        if strategy == "staged":
            if backup_dir.is_symlink() or work_dir.is_symlink() or archive_dir.is_symlink():
                raise MeetingServiceError("发布恢复路径包含符号链接")
            if backup_dir.is_dir() and archive_dir.is_dir() and not work_dir.exists():
                os.replace(archive_dir, work_dir)
                os.replace(backup_dir, archive_dir)
                self._remove_recovery_directory(work_dir)
                return
            if backup_dir.is_dir() and not archive_dir.exists():
                os.replace(backup_dir, archive_dir)
                self._remove_recovery_directory(work_dir)
                return
            raise MeetingServiceError("staged 发布状态不唯一，拒绝自动回滚")
        if promoting or strategy == "install":
            if archive_dir.is_dir() and not work_dir.exists():
                os.replace(archive_dir, work_dir)
                self._remove_recovery_directory(work_dir)
                return
            raise MeetingServiceError("首次发布状态不唯一，拒绝自动回滚")
        if strategy == "rename_swap":
            if archive_dir.is_dir() and work_dir.is_dir():
                atomic_swap_directories(work_dir, archive_dir)
                self._remove_recovery_directory(work_dir)
                return
            raise MeetingServiceError("原子交换发布状态不唯一，拒绝自动回滚")
        raise MeetingServiceError(f"未知发布策略：{strategy}")

    def _remove_publish_journal(self, journal: Path) -> None:
        journal.unlink(missing_ok=True)
        if journal.parent.is_dir():
            fsync_directory(journal.parent)

    @staticmethod
    def _directory_publish_token(directory: Path) -> str | None:
        manifest = directory / "workbench-manifest.json"
        if directory.is_symlink() or not directory.is_dir() or manifest.is_symlink():
            return None
        try:
            payload = load_json_file(manifest)
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError):
            return None
        token = payload.get("publish_token") if isinstance(payload, dict) else None
        return token if isinstance(token, str) else None

    def _publish_commit_recorded(
        self,
        meeting_id: str,
        publish_token: str,
        transcript_version_id: str,
        minutes_version_id: str,
        archive_dir: Path,
        *,
        allow_legacy: bool = True,
    ) -> bool:
        rows = self.db.query_all(
            """SELECT payload_json FROM events
               WHERE meeting_id=? AND event_type='publish_files_committed'
               ORDER BY id DESC""",
            (meeting_id,),
        )
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("publish_token") == publish_token
                and payload.get("transcript_version_id") == transcript_version_id
                and payload.get("minutes_version_id") == minutes_version_id
            ):
                return True
        if not allow_legacy:
            return False
        legacy = self.db.query_one(
            """SELECT tv.published AS transcript_published,
                      mv.published AS minutes_published, m.canonical_dir
               FROM meetings m
               JOIN transcript_versions tv ON tv.id=? AND tv.meeting_id=m.id
               JOIN minutes_versions mv ON mv.id=? AND mv.meeting_id=m.id
               WHERE m.id=?""",
            (transcript_version_id, minutes_version_id, meeting_id),
        )
        return bool(
            legacy
            and legacy["transcript_published"]
            and legacy["minutes_published"]
            and Path(os.path.abspath(str(legacy.get("canonical_dir") or ""))) == archive_dir
        )

    def _recover_publish_journals(self) -> None:
        if (
            not self.archive_root
            or self.archive_root.is_symlink()
            or not self.archive_root.is_dir()
        ):
            return
        root = Path(os.path.abspath(str(self.archive_root)))
        for journal in sorted(root.glob(".workbench-publish-journal-*.json")):
            try:
                journal_mode = journal.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(journal_mode) or not stat.S_ISREG(journal_mode):
                continue
            try:
                payload = load_json_file(journal)
                if not isinstance(payload, dict):
                    continue
                schema_version = int(payload.get("schema_version", 1))
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, RecursionError):
                continue
            try:
                if schema_version in {1, 2}:
                    self._recover_legacy_publish_journal(root, journal, payload)
                elif schema_version == 3:
                    self._recover_schema_v3_publish_journal(root, journal, payload)
                else:
                    meeting_id = payload.get("meeting_id")
                    token = payload.get("publish_token")
                    if isinstance(meeting_id, str) and isinstance(token, str):
                        self._mark_publish_recovery_conflict(
                            meeting_id, token, "unsupported_journal_schema"
                        )
            except OSError:
                meeting_id = payload.get("meeting_id")
                token = payload.get("publish_token")
                if isinstance(meeting_id, str) and isinstance(token, str):
                    self._mark_publish_recovery_conflict(meeting_id, token, "recovery_io_error")
                raise

    def _validated_recovery_fields(
        self,
        root: Path,
        journal: Path,
        payload: dict[str, Any],
        *,
        require_backup: bool,
    ) -> tuple[str, str, str, str, Path, Path, Path | None, bool] | None:
        token = payload.get("publish_token")
        meeting_id = payload.get("meeting_id")
        transcript_version_id = payload.get("transcript_version_id")
        minutes_version_id = payload.get("minutes_version_id")
        archive_dir = Path(os.path.abspath(str(payload.get("archive_dir", ""))))
        work_dir = Path(os.path.abspath(str(payload.get("work_dir", ""))))
        backup_dir = (
            Path(os.path.abspath(str(payload.get("backup_dir", "")))) if require_backup else None
        )
        promoting = payload.get("promoting")
        paths = [archive_dir, work_dir, *([backup_dir] if backup_dir else [])]
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9a-f]{32}", token)
            or journal.name != f".workbench-publish-journal-{token}.json"
            or not isinstance(meeting_id, str)
            or not isinstance(transcript_version_id, str)
            or not isinstance(minutes_version_id, str)
            or not isinstance(promoting, bool)
            or any(path is None or path == root or path.parent != root for path in paths)
            or len({path for path in paths if path is not None}) != len(paths)
        ):
            return None
        return (
            token,
            meeting_id,
            transcript_version_id,
            minutes_version_id,
            archive_dir,
            work_dir,
            backup_dir,
            promoting,
        )

    @staticmethod
    def _recovery_path_is_safe(path: Path) -> bool:
        return not path.is_symlink() and (not path.exists() or path.is_dir())

    def _mark_publish_recovery_conflict(
        self, meeting_id: str, publish_token: str, reason: str
    ) -> None:
        meeting = self.db.query_one("SELECT id FROM meetings WHERE id=?", (meeting_id,))
        if not meeting:
            return
        payload = {"publish_token": publish_token, "reason": reason}
        self.db.conflicts.open(
            meeting_id,
            "publish_recovery",
            payload=payload,
        )
        self.db.add_event(
            "publish_recovery_conflict",
            meeting_id=meeting_id,
            actor="system",
            payload=payload,
        )

    def _finish_publish_recovery(
        self,
        root: Path,
        journal: Path,
        meeting_id: str,
        token: str,
        action: str,
    ) -> None:
        fsync_directory(root)
        self._remove_publish_journal(journal)
        if self.db.query_one("SELECT id FROM meetings WHERE id=?", (meeting_id,)):
            for kind in ("publish_recovery", "publish_post_commit"):
                for conflict in self.db.conflicts.list(meeting_id, kind=kind):
                    payload = conflict.get("payload")
                    if isinstance(payload, dict) and payload.get("publish_token") == token:
                        self.db.conflicts.resolve(conflict["id"], f"recovered:{action}")
            self.db.add_event(
                "publish_recovered_after_restart",
                meeting_id=meeting_id,
                actor="system",
                payload={"action": action, "publish_token": token},
            )

    def _recover_schema_v3_publish_journal(
        self, root: Path, journal: Path, payload: dict[str, Any]
    ) -> None:
        fields = self._validated_recovery_fields(root, journal, payload, require_backup=True)
        if fields is None:
            meeting_id = payload.get("meeting_id")
            token = payload.get("publish_token")
            if isinstance(meeting_id, str) and isinstance(token, str):
                self._mark_publish_recovery_conflict(meeting_id, token, "invalid_journal_fields")
            return
        (
            token,
            meeting_id,
            transcript_version_id,
            minutes_version_id,
            archive_dir,
            work_dir,
            backup_dir,
            promoting,
        ) = fields
        assert backup_dir is not None
        strategy = payload.get("strategy")
        phase = payload.get("phase")
        if strategy not in {"install", "rename_swap", "staged"} or phase not in {
            "prepared",
            "old_moved",
            "new_installed",
        }:
            self._mark_publish_recovery_conflict(meeting_id, token, "invalid_journal_state")
            return
        if not all(
            self._recovery_path_is_safe(path) for path in (archive_dir, work_dir, backup_dir)
        ):
            self._mark_publish_recovery_conflict(meeting_id, token, "unsafe_recovery_path")
            return

        committed = self._publish_commit_recorded(
            meeting_id,
            token,
            transcript_version_id,
            minutes_version_id,
            archive_dir,
            allow_legacy=False,
        )
        archive_is_new = self._directory_publish_token(archive_dir) == token
        work_is_new = self._directory_publish_token(work_dir) == token
        expected_artifacts = payload.get("expected_artifacts")
        expected_manifest_sha256 = payload.get("expected_manifest_sha256")

        if committed:
            expected_state_valid = bool(
                isinstance(expected_artifacts, list)
                and all(isinstance(item, dict) for item in expected_artifacts)
                and isinstance(expected_manifest_sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
            )
            if not archive_is_new or not expected_state_valid:
                self._mark_publish_recovery_conflict(meeting_id, token, "committed_state_ambiguous")
                return
            try:
                self._assert_published_files_current(
                    archive_dir,
                    expected_artifacts,
                    expected_manifest_sha256,
                    None,
                    None,
                )
            except (ConflictError, OSError):
                self._mark_publish_recovery_conflict(meeting_id, token, "committed_hash_mismatch")
                return
            self._remove_recovery_directory(work_dir)
            self._remove_recovery_directory(backup_dir)
            self._finish_publish_recovery(root, journal, meeting_id, token, "committed_cleanup")
            return

        action: str | None = None
        if strategy == "staged" or backup_dir.exists():
            if backup_dir.is_dir() and archive_is_new and not work_dir.exists():
                os.replace(archive_dir, work_dir)
                fsync_directory(root)
                os.replace(backup_dir, archive_dir)
                fsync_directory(root)
                self._remove_recovery_directory(work_dir)
                action = "staged_new_installed_rolled_back"
            elif backup_dir.is_dir() and not archive_dir.exists() and work_is_new:
                os.replace(backup_dir, archive_dir)
                fsync_directory(root)
                self._remove_recovery_directory(work_dir)
                action = "staged_old_moved_rolled_back"
            elif (
                not backup_dir.exists()
                and archive_dir.is_dir()
                and not archive_is_new
                and work_is_new
            ):
                self._remove_recovery_directory(work_dir)
                action = "staged_prepared_discarded"
            elif (
                not backup_dir.exists()
                and archive_dir.is_dir()
                and not archive_is_new
                and not work_dir.exists()
            ):
                action = "staged_already_rolled_back"
        elif strategy == "rename_swap":
            if archive_is_new and work_dir.is_dir() and not work_is_new:
                atomic_swap_directories(work_dir, archive_dir)
                self._remove_recovery_directory(work_dir)
                action = "rename_swap_rolled_back"
            elif archive_dir.is_dir() and not archive_is_new and work_is_new:
                self._remove_recovery_directory(work_dir)
                action = "rename_swap_prepared_discarded"
            elif archive_dir.is_dir() and not archive_is_new and not work_dir.exists():
                action = "rename_swap_already_rolled_back"
        elif strategy == "install" or promoting:
            if not archive_dir.exists() and work_is_new:
                self._remove_recovery_directory(work_dir)
                action = "install_prepared_discarded"
            elif archive_is_new and not work_dir.exists():
                os.replace(archive_dir, work_dir)
                fsync_directory(root)
                self._remove_recovery_directory(work_dir)
                action = "install_rolled_back"
            elif not archive_dir.exists() and not work_dir.exists():
                action = "install_already_rolled_back"

        if action is None:
            self._mark_publish_recovery_conflict(meeting_id, token, "uncommitted_state_ambiguous")
            return
        self._finish_publish_recovery(root, journal, meeting_id, token, action)

    def _recover_legacy_publish_journal(
        self, root: Path, journal: Path, payload: dict[str, Any]
    ) -> None:
        fields = self._validated_recovery_fields(root, journal, payload, require_backup=False)
        if fields is None:
            return
        (
            token,
            meeting_id,
            transcript_version_id,
            minutes_version_id,
            archive_dir,
            work_dir,
            _backup_dir,
            _promoting,
        ) = fields
        if not all(self._recovery_path_is_safe(path) for path in (archive_dir, work_dir)):
            return
        committed = self._publish_commit_recorded(
            meeting_id,
            token,
            transcript_version_id,
            minutes_version_id,
            archive_dir,
        )
        archive_is_new = self._directory_publish_token(archive_dir) == token
        work_is_new = self._directory_publish_token(work_dir) == token
        expected_artifacts = payload.get("expected_artifacts")
        expected_manifest_sha256 = payload.get("expected_manifest_sha256")
        action: str | None = None
        if committed:
            if (
                not archive_is_new
                or not isinstance(expected_artifacts, list)
                or not all(isinstance(item, dict) for item in expected_artifacts)
                or not isinstance(expected_manifest_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
            ):
                self._mark_publish_recovery_conflict(
                    meeting_id, token, "legacy_committed_ambiguous"
                )
                return
            try:
                self._assert_published_files_current(
                    archive_dir,
                    expected_artifacts,
                    expected_manifest_sha256,
                    None,
                    None,
                )
            except (ConflictError, OSError):
                self._mark_publish_recovery_conflict(meeting_id, token, "legacy_hash_mismatch")
                return
            self._remove_recovery_directory(work_dir)
            action = "legacy_committed_cleanup"
        elif archive_is_new and work_dir.is_dir():
            atomic_swap_directories(work_dir, archive_dir)
            self._remove_recovery_directory(work_dir)
            action = "legacy_rolled_back"
        elif archive_is_new and not work_dir.exists():
            os.replace(archive_dir, work_dir)
            self._remove_recovery_directory(work_dir)
            action = "legacy_install_rolled_back"
        elif work_is_new:
            self._remove_recovery_directory(work_dir)
            action = "legacy_prepared_discarded"
        elif not work_dir.exists():
            action = "legacy_already_rolled_back"
        if action:
            self._finish_publish_recovery(root, journal, meeting_id, token, action)

    @staticmethod
    def _publish_meeting_snapshot(meeting: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "title",
            "recording_date",
            "status",
            "canonical_dir",
            "source_priority",
            "source_job_id",
            "current_transcript_version_id",
            "current_minutes_version_id",
            "conflict",
            "original_audio_sha256",
        )
        return {key: meeting.get(key) for key in keys}

    @staticmethod
    def _publish_minutes_snapshot(minutes: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id",
            "markdown",
            "source_job_id",
            "source_attempt",
            "requested_stage",
            "input_transcript_sha256",
        )
        return {key: minutes.get(key) for key in keys}

    @staticmethod
    def _publish_segments_snapshot(segments: list[dict[str, Any]]) -> str:
        relevant = [
            {
                key: segment.get(key)
                for key in (
                    "id",
                    "ordinal",
                    "start_ms",
                    "end_ms",
                    "speaker_label",
                    "speaker_name",
                    "text",
                )
            }
            for segment in segments
        ]
        encoded = json.dumps(
            relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _assert_publish_snapshot_current(
        self,
        connection: Any,
        meeting_id: str,
        meeting_snapshot: dict[str, Any],
        minutes_snapshot: dict[str, Any],
        segments_snapshot: str,
    ) -> None:
        meeting_row = connection.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if not meeting_row:
            raise ConflictError("会议已被删除，无法发布")
        current_meeting = self._publish_meeting_snapshot(dict(meeting_row))
        if current_meeting != meeting_snapshot or current_meeting.get("conflict"):
            raise ConflictError("发布准备期间会议或草稿状态已变化，请重新发布")
        minutes_id = current_meeting.get("current_minutes_version_id")
        minutes_row = connection.execute(
            "SELECT * FROM minutes_versions WHERE id = ?", (minutes_id,)
        ).fetchone()
        if not minutes_row or self._publish_minutes_snapshot(dict(minutes_row)) != minutes_snapshot:
            raise ConflictError("发布准备期间纪要已变化，请重新发布")
        version_id = current_meeting.get("current_transcript_version_id")
        segment_rows = connection.execute(
            "SELECT * FROM segments WHERE version_id = ? ORDER BY ordinal", (version_id,)
        ).fetchall()
        current_segments = [dict(row) for row in segment_rows]
        if self._publish_segments_snapshot(current_segments) != segments_snapshot:
            raise ConflictError("发布准备期间逐字稿已变化，请重新发布")

    def _new_archive_directory(self, meeting: dict[str, Any]) -> Path:
        assert self.archive_root is not None
        recording_date = meeting.get("recording_date")
        try:
            recorded = (
                datetime.fromisoformat(recording_date) if recording_date else datetime.now(UTC)
            )
        except ValueError:
            recorded = datetime.now(UTC)
        date_code = recorded.astimezone().strftime("%y%m%d")
        title = re.sub(r"[\\/:\0]+", "-", str(meeting.get("title") or "会议录音")).strip()
        title = re.sub(r"^(?:20)?\d{6}\s*", "", title).strip(" .-") or "会议录音"
        return self.archive_root / f"{date_code} {title}"

    def _validated_archive_directory(self, candidate: Path) -> Path:
        if candidate.is_symlink() or not candidate.is_dir():
            raise PublishValidationError("正式归档目录不存在或是符号链接")
        resolved = candidate.resolve()
        if self.archive_root:
            root = self.archive_root.resolve()
            if not resolved.is_relative_to(root) or resolved == root:
                raise PublishValidationError("正式归档目录越界")
        return candidate

    @staticmethod
    def _validate_output_paths(work_dir: Path, outputs: dict[Path, str]) -> None:
        root = work_dir.resolve()
        for path in outputs:
            if path.parent.resolve() != root or path.is_symlink():
                raise PublishValidationError("发布产物路径越界")

    @staticmethod
    def _assert_published_files_current(
        archive_dir: Path,
        expected_artifacts: list[dict[str, Any]],
        expected_manifest_sha256: str,
        source_audio: Path | None,
        expected_audio_sha256: str | None,
    ) -> None:
        manifest = archive_dir / "workbench-manifest.json"
        if (
            manifest.is_symlink()
            or not manifest.is_file()
            or sha256_file(manifest) != expected_manifest_sha256
        ):
            raise ConflictError("发布 manifest 在目录切换后发生变化")
        for path in archive_dir.rglob("*"):
            if path.is_symlink():
                raise ConflictError("发布目录在目录切换后出现符号链接")
        if MeetingService._manifest_artifacts(archive_dir) != expected_artifacts:
            raise ConflictError("发布产物在目录切换后发生变化")
        if source_audio and expected_audio_sha256:
            if (
                source_audio.is_symlink()
                or not source_audio.is_file()
                or sha256_file(source_audio) != expected_audio_sha256
            ):
                raise ConflictError("发布过程中原音频发生变化")

    def _source_audio(
        self,
        artifacts: list[dict[str, Any]],
        expected_hash: str | None,
        *,
        required: bool,
        formal: bool,
        canonical_dir: Path,
        meeting_id: str,
        preferred_directory: Path | None = None,
    ) -> dict[str, Any] | None:
        ranks = {"archive": 4, "draft": 3, "staging": 2, "history": 1}
        canonical_root = Path(os.path.abspath(canonical_dir))
        preferred_root = (
            Path(os.path.abspath(preferred_directory)) if preferred_directory else None
        )
        candidates = [
            row
            for row in artifacts
            if row["kind"] == "audio"
            and (
                preferred_root is None
                or Path(os.path.abspath(row["path"])).is_relative_to(preferred_root)
            )
            and (
                not formal
                or (
                    row.get("source_root") == "archive"
                    and Path(os.path.abspath(row["path"])).is_relative_to(canonical_root)
                )
            )
            and Path(row["path"]).is_file()
            and not Path(row["path"]).is_symlink()
        ]
        if formal and expected_hash:
            checked = []
            for row in candidates:
                actual = sha256_file(Path(row["path"]))
                if actual == expected_hash:
                    return row
                checked.append((row, actual))
            issue = {
                "reason": "hash_mismatch" if checked else "missing",
                "artifact_id": checked[0][0].get("id") if checked else None,
                "expected_sha256": expected_hash,
                "actual_sha256": checked[0][1] if checked else None,
            }
            already_open = self.db.conflicts.has(meeting_id, "audio_integrity")
            self.db.conflicts.open(meeting_id, "audio_integrity", payload=issue)
            if not already_open:
                self.db.add_event(
                    "audio_integrity_conflict",
                    meeting_id=meeting_id,
                    actor="system",
                    payload=issue,
                )
            raise ConflictError("正式归档原音频缺失或哈希不一致，禁止发布")
        if not candidates:
            if required:
                raise PublishValidationError("缺少原音频")
            return None
        candidates.sort(key=lambda row: ranks.get(row.get("source_root"), 0), reverse=True)
        if expected_hash:
            for row in candidates:
                if (
                    row.get("sha256") == expected_hash
                    or sha256_file(Path(row["path"])) == expected_hash
                ):
                    return row
        return candidates[0]

    @staticmethod
    def _published_audio_path(
        audio_path: Path | None,
        archive_dir: Path,
        work_dir: Path,
        *,
        promoting: bool,
    ) -> Path | None:
        if not audio_path:
            return None
        if promoting:
            candidate = work_dir / audio_path.name
            return candidate if candidate.exists() else None
        try:
            relative = audio_path.resolve().relative_to(archive_dir.resolve())
        except ValueError:
            return None
        candidate = work_dir / relative
        return candidate if candidate.exists() else None

    @staticmethod
    def _copy_required_sources(
        artifacts: list[dict[str, Any]],
        destination: Path,
        provenance: AttemptPublishProvenance | None = None,
    ) -> None:
        ranks = {"draft": 4, "archive": 3, "staging": 2, "history": 1}
        required = {
            "audio",
            "funasr_json",
            "funasr_log",
            "minutes_evidence",
            "minutes_plan",
            "speaker_map",
            "whisper_json",
            "whisper_srt",
            "whisper_txt",
            "whisper_tsv",
            "whisper_vtt",
            "whisper_log",
        }
        selected: dict[str, dict[str, Any]] = {}
        for row in artifacts:
            kind = row["kind"]
            path = Path(row["path"])
            if provenance:
                try:
                    path.resolve(strict=True).relative_to(
                        provenance.source_directory.resolve(strict=True)
                    )
                except (OSError, ValueError):
                    continue
                if kind in {"minutes_evidence", "minutes_plan"}:
                    continue
            if kind not in required or not path.is_file() or path.is_symlink():
                continue
            current = selected.get(kind)
            if current is None or ranks.get(row["source_root"], 0) > ranks.get(
                current["source_root"], 0
            ):
                selected[kind] = row
        for kind, row in selected.items():
            source = Path(row["path"])
            target_dir = destination / "whisper-ref" if kind.startswith("whisper_") else destination
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_dir / source.name)
        if provenance:
            MeetingService._copy_attempt_sidecars(provenance, destination)
            return
        plans = [
            Path(row["path"])
            for row in artifacts
            if row.get("kind") == "minutes_plan"
            and Path(row["path"]).is_file()
            and not Path(row["path"]).is_symlink()
        ]
        if len(plans) != 1:
            return
        try:
            plan = load_json_file(plans[0])
        except (OSError, ValueError, RecursionError):
            return
        expected_source = plan.get("source_srt_sha256") if isinstance(plan, dict) else None
        if not isinstance(expected_source, str):
            return
        sources = [
            Path(row["path"])
            for row in artifacts
            if row.get("kind") == "srt"
            and Path(row["path"]).is_file()
            and not Path(row["path"]).is_symlink()
            and sha256_file(Path(row["path"])) == expected_source
        ]
        if len(sources) == 1:
            atomic_copy_verified(sources[0], destination / "input-transcript.srt", expected_source)

    @staticmethod
    def _copy_attempt_sidecars(
        provenance: AttemptPublishProvenance, destination: Path
    ) -> None:
        for target_name, (source, expected_sha256) in provenance.files.items():
            atomic_copy_verified(source, destination / target_name, expected_sha256)

    @staticmethod
    def _manifest_artifacts(root: Path) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name == "workbench-manifest.json":
                continue
            if (
                ".workbench-history" in path.parts
                or path.name.startswith("._")
                or path.name == ".DS_Store"
            ):
                continue
            entries.append(
                {
                    "path": str(path.relative_to(root)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        return entries

    def _meeting(self, meeting_id: str) -> dict[str, Any]:
        meeting = self.db.query_one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        if not meeting:
            raise NotFoundError("会议不存在")
        return meeting

    def _current_segments(self, meeting_id: str) -> list[dict[str, Any]]:
        return self.db.query_all(
            """SELECT s.* FROM segments s JOIN meetings m ON m.current_transcript_version_id = s.version_id
               WHERE m.id = ? ORDER BY s.ordinal""",
            (meeting_id,),
        )

    @staticmethod
    def _normalized_transcript_text(segments: list[dict[str, Any]]) -> str:
        rendered = render_transcript_txt(segments).rstrip()
        if not rendered:
            raise PublishValidationError("逐字稿为空")
        return rendered + "\n"

    def render_current_transcript(self, meeting_id: str) -> str:
        self._meeting(meeting_id)
        return self._normalized_transcript_text(self._current_segments(meeting_id))

    @staticmethod
    def _normalized_transcript_srt(segments: list[dict[str, Any]]) -> str:
        if not segments:
            raise PublishValidationError("逐字稿为空")
        rendered = render_transcript_srt(segments)
        if rendered is None:
            raise PublishValidationError("逐字稿为空、含空段落或时间顺序无效")
        return rendered

    def create_transcript_snapshot(
        self, meeting_id: str, directory: str | Path
    ) -> tuple[Path, str]:
        root = Path(directory).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise PublishValidationError("逐字稿快照目录不可用")
        safe_id = normalize_meeting_id(meeting_id)
        self._meeting(safe_id)
        content = self._normalized_transcript_srt(self._current_segments(safe_id))
        path = root / f"{safe_id}-{uuid.uuid4().hex}.srt"
        atomic_write_text(path, content)
        return path, hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _attempt_publish_provenance(
        self,
        artifacts: list[dict[str, Any]],
        meeting: dict[str, Any],
        minutes: dict[str, Any],
    ) -> AttemptPublishProvenance | None:
        meeting_id = meeting.get("id")
        job_id = minutes.get("source_job_id")
        attempt = minutes.get("source_attempt")
        if (
            not isinstance(meeting_id, str)
            or not isinstance(job_id, str)
            or not job_id
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
        ):
            return None
        candidates: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
        for row in artifacts:
            if row.get("kind") != "manifest" or row.get("source_root") not in {
                "draft",
                "archive",
            }:
                continue
            path = Path(row["path"])
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = load_json_file(path)
            except (OSError, ValueError, RecursionError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("job_id") == job_id
                and payload.get("attempt") == attempt
                and payload.get("minutes_protocol_version") == 3
            ):
                candidates.append((row, path, payload))
        if not candidates:
            return None
        if len(candidates) != 1:
            raise PublishValidationError("当前纪要对应的 v3 attempt manifest 不唯一")
        row, manifest_path, _payload = candidates[0]
        manifest_sha256 = row.get("sha256")
        if not isinstance(manifest_sha256, str):
            raise PublishValidationError("当前纪要的 attempt manifest 缺少可信哈希")
        if self.relay_jobs_db is None:
            raise PublishValidationError("缺少 Relay 数据库，无法核验 v3 attempt")
        try:
            manifest = load_minutes_manifest(
                manifest_path,
                expected_sha256=manifest_sha256,
                meeting_id=meeting_id,
                source_job_id=job_id,
                source_attempt=attempt,
                requested_stage=minutes.get("requested_stage"),
                input_transcript_sha256=minutes.get("input_transcript_sha256"),
            )
            relay_attempt = load_relay_attempt(
                self.relay_jobs_db, job_id=job_id, attempt=attempt
            )
        except MinutesEvidenceError as error:
            raise PublishValidationError(str(error)) from error
        payload = manifest["payload"]
        entries = manifest["entries"]
        if not relay_attempt_matches_minutes(
            relay_attempt,
            requested_stage=minutes.get("requested_stage"),
            input_transcript_sha256=minutes.get("input_transcript_sha256"),
        ):
            raise PublishValidationError("当前纪要与 Relay attempt 来源不一致")

        def trusted_hash(name: str) -> str:
            manifest_value = payload.get(name)
            relay_value = relay_attempt.get(name)
            if manifest_value is not None and manifest_value != relay_value:
                raise PublishValidationError("attempt manifest 与 Relay 哈希不一致")
            value = manifest_value or relay_value
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise PublishValidationError("Relay attempt 缺少可信产物哈希")
            return value

        plan_sha256 = trusted_hash("minutes_plan_sha256")
        source_srt_sha256 = trusted_hash("source_srt_sha256")
        plan_entry = entries.get("minutes-plan.json")
        evidence_entry = entries.get("minutes-evidence.json")
        source_entry = select_source_srt_entry(
            entries,
            expected_sha256=source_srt_sha256,
            requested_stage=minutes.get("requested_stage"),
        )
        if (
            plan_entry is None
            or plan_entry["sha256"] != plan_sha256
            or evidence_entry is None
            or source_entry is None
        ):
            raise PublishValidationError("v3 attempt 的证据、计划或来源 SRT 不完整")
        source_directory = manifest_path.parent
        files = {
            "minutes-evidence.json": (
                source_directory / evidence_entry["path"],
                evidence_entry["sha256"],
            ),
            "minutes-plan.json": (
                source_directory / plan_entry["path"],
                plan_entry["sha256"],
            ),
            "input-transcript.srt": (
                source_directory / source_entry["path"],
                source_entry["sha256"],
            ),
        }
        provenance = AttemptPublishProvenance(
            public_metadata={
                "minutes_protocol_version": 3,
                "requested_stage": minutes.get("requested_stage"),
                "input_transcript_sha256": minutes.get("input_transcript_sha256"),
                "source_srt_sha256": source_srt_sha256,
                "minutes_plan_sha256": plan_sha256,
            },
            source_directory=source_directory,
            source_manifest_path=manifest_path,
            source_manifest_sha256=manifest_sha256,
            files=files,
            relay_attempt=relay_attempt,
        )
        self._validate_attempt_provenance(provenance)
        return provenance

    def _validate_attempt_provenance(
        self, provenance: AttemptPublishProvenance
    ) -> None:
        if (
            provenance.source_manifest_path.is_symlink()
            or not provenance.source_manifest_path.is_file()
            or sha256_file(provenance.source_manifest_path)
            != provenance.source_manifest_sha256
        ):
            raise PublishValidationError("attempt manifest 已变化，禁止发布")
        for source, expected_sha256 in provenance.files.values():
            if (
                source.is_symlink()
                or not source.is_file()
                or sha256_file(source) != expected_sha256
            ):
                raise PublishValidationError("attempt 证据链文件已变化，禁止发布")
        if self.relay_jobs_db is None:
            raise PublishValidationError("缺少 Relay 数据库，无法核验 v3 attempt")
        job_id = provenance.source_manifest_path.parent.parent.name
        payload = load_json_file(provenance.source_manifest_path)
        if not isinstance(payload, dict):
            raise PublishValidationError("attempt manifest 无效")
        job_id = payload.get("job_id", job_id)
        attempt = payload.get("attempt")
        try:
            current = load_relay_attempt(self.relay_jobs_db, job_id=job_id, attempt=attempt)
        except MinutesEvidenceError as error:
            raise PublishValidationError(str(error)) from error
        if current != provenance.relay_attempt:
            raise PublishValidationError("Relay attempt 在发布期间发生变化")

    @staticmethod
    def _minutes_protocol_publish_metadata(
        artifacts: list[dict[str, Any]],
        meeting: dict[str, Any],
        minutes: dict[str, Any],
    ) -> dict[str, int]:
        job_id = meeting.get("source_job_id")
        source_job_id = minutes.get("source_job_id")
        source_attempt = minutes.get("source_attempt")
        if (
            not isinstance(job_id, str)
            or source_job_id != job_id
            or not isinstance(source_attempt, int)
        ):
            return {}
        candidates: list[tuple[int, int]] = []
        for row in artifacts:
            if row.get("kind") != "manifest" or row.get("source_root") not in {
                "draft",
                "archive",
            }:
                continue
            path = Path(row["path"])
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = load_json_file(path)
            except (OSError, ValueError, RecursionError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("job_id") != job_id
                or payload.get("attempt") != source_attempt
            ):
                continue
            protocol = payload.get("minutes_protocol_version")
            if type(protocol) is int and protocol >= 2:
                candidates.append((int(row.get("mtime_ns") or 0), protocol))
        if not candidates:
            return {}
        candidates.sort(reverse=True)
        return {"minutes_protocol_version": candidates[0][1]}

    @staticmethod
    def _minutes_only_publish_metadata(
        artifacts: list[dict[str, Any]],
        meeting: dict[str, Any],
        minutes: dict[str, Any],
        provenance: AttemptPublishProvenance | None = None,
    ) -> dict[str, str] | None:
        meeting_job_id = meeting.get("source_job_id")
        if not meeting_job_id:
            return None
        source_job_id = minutes.get("source_job_id")
        source_attempt = minutes.get("source_attempt")
        if source_job_id != meeting_job_id or not isinstance(source_attempt, int):
            raise PublishValidationError("当前纪要版本缺少可核验的 job/attempt 来源")
        manifests = [
            row
            for row in artifacts
            if row.get("kind") == "manifest" and row.get("source_root") in {"draft", "archive"}
        ]
        candidates: list[tuple[dict[str, Any], dict[str, Any], Path, int]] = []
        for row in manifests:
            path = Path(row["path"])
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = load_json_file(path)
            except (OSError, ValueError, RecursionError):
                continue
            if not isinstance(payload, dict) or payload.get("job_id") != meeting_job_id:
                continue
            attempt = payload.get("attempt", payload.get("source_attempt"))
            if not isinstance(attempt, int):
                continue
            candidates.append((row, payload, path, attempt))
        if not candidates:
            raise PublishValidationError("找不到当前纪要版本对应的 attempt manifest")
        latest_attempt = max(item[3] for item in candidates)
        if source_attempt != latest_attempt:
            raise PublishValidationError(
                f"当前纪要来自 attempt {source_attempt}，任务当前为 attempt {latest_attempt}"
            )
        exact = [item for item in candidates if item[3] == source_attempt]
        exact.sort(
            key=lambda item: (
                item[0].get("source_root") == "draft",
                int(item[0].get("mtime_ns") or 0),
            ),
            reverse=True,
        )
        requested_stage = minutes.get("requested_stage")
        if requested_stage != "minutes_generating":
            return None
        digest = minutes.get("input_transcript_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PublishValidationError("当前纪要版本缺少有效逐字稿快照哈希")
        if provenance:
            return {
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": digest,
            }
        for row, payload, path, _attempt in exact:
            if (
                payload.get("requested_stage") != "minutes_generating"
                or payload.get("input_transcript_sha256") != digest
            ):
                continue
            if row.get("source_root") == "draft":
                transcript_name = input_transcript_name(payload)
                input_path = path.parent / transcript_name
                if (
                    input_path.is_symlink()
                    or not input_path.is_file()
                    or sha256_file(input_path) != digest
                ):
                    continue
                entries = payload.get("artifacts")
                if not isinstance(entries, list):
                    continue
                listed = {
                    entry.get("path"): entry
                    for entry in entries
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str)
                }
                input_entry = listed.get(transcript_name)
                if not input_entry or input_entry.get("sha256") != digest:
                    continue
                stems = {
                    Path(name).stem for name in listed if Path(name).suffix.lower() == ".md"
                } & {Path(name).stem for name in listed if Path(name).suffix.lower() == ".html"}
                if not stems:
                    continue
            return {
                "requested_stage": "minutes_generating",
                "input_transcript_sha256": digest,
            }
        raise PublishValidationError("当前纪要版本的快照来源与 attempt manifest 不一致")

    def _validate_source_artifacts(
        self,
        artifacts: list[dict[str, Any]],
        *,
        source_root: str | None = None,
        meeting_id: str | None = None,
        canonical_dir: Path | None = None,
        require_complete: bool = True,
    ) -> None:
        required = {
            "audio",
            "funasr_json",
            "funasr_log",
            "speaker_map",
            "whisper_json",
            "whisper_txt",
            "whisper_srt",
            "whisper_tsv",
            "whisper_vtt",
            "whisper_log",
        }
        canonical_root = Path(os.path.abspath(canonical_dir)) if canonical_dir else None
        candidates = [
            row
            for row in artifacts
            if row.get("kind") in required
            and (source_root is None or row.get("source_root") == source_root)
            and (
                canonical_root is None
                or Path(os.path.abspath(row["path"])).is_relative_to(canonical_root)
            )
        ]
        changed = []
        signature_paths = []
        for row in candidates:
            path = Path(row["path"])
            if row.get("kind") == "audio":
                continue
            if path.is_symlink() or not path.is_file():
                changed.append(
                    {
                        "artifact_id": row.get("id"),
                        "kind": row.get("kind"),
                        "reason": "missing_or_symlink",
                    }
                )
            else:
                signature_paths.append(path)
                expected = row.get("sha256")
                if expected and sha256_file(path) != expected:
                    changed.append(
                        {
                            "artifact_id": row.get("id"),
                            "kind": row.get("kind"),
                            "reason": "hash_mismatch",
                        }
                    )
        if changed:
            signature = source_signature(signature_paths) if signature_paths else None
            payload = {
                "changed_resources": ["managed_sources"],
                "external_transcript_version_id": None,
                "external_minutes_version_id": None,
                "source_signature": signature,
                "artifacts": changed,
            }
            if meeting_id:
                already_open = self.db.conflicts.has(meeting_id, "external_source_change")
                self.db.conflicts.open(
                    meeting_id,
                    "external_source_change",
                    source_signature=signature,
                    payload=payload,
                )
                if not already_open:
                    self.db.add_event(
                        "external_change_conflict", meeting_id=meeting_id, payload=payload
                    )
            raise ConflictError("归档源文件已在扫描后发生变化，请重新扫描后再发布")
        if not require_complete:
            return
        kinds = {
            row["kind"]
            for row in candidates
            if Path(row["path"]).is_file()
            and not Path(row["path"]).is_symlink()
            and Path(row["path"]).stat().st_size > 0
        }
        missing = {"audio", "funasr_json", "funasr_log", "speaker_map"} - kinds
        required_whisper = {
            "whisper_json",
            "whisper_txt",
            "whisper_srt",
            "whisper_tsv",
            "whisper_vtt",
            "whisper_log",
        }
        missing_whisper = required_whisper - kinds
        if missing or missing_whisper:
            names = sorted(missing) + sorted(missing_whisper)
            raise PublishValidationError(f"归档产物不完整：{', '.join(names)}")

    @staticmethod
    def _remove_previous_managed_sources(work_dir: Path) -> None:
        whisper = work_dir / "whisper-ref"
        if whisper.exists():
            if whisper.is_symlink():
                raise PublishValidationError("旧 whisper-ref 是符号链接")
            remove_directory_tree(whisper)
        removable = {"funasr_json", "funasr_log", "speaker_map"}
        for path in work_dir.iterdir():
            if path.is_file() and not path.is_symlink() and artifact_kind(path) in removable:
                path.unlink()
        MeetingService._remove_previous_attempt_sidecars(work_dir)

    @staticmethod
    def _remove_previous_attempt_sidecars(work_dir: Path) -> None:
        for name in {
            "minutes-evidence.json",
            "minutes-plan.json",
            "input-transcript.srt",
            "input-transcript.txt",
        }:
            path = work_dir / name
            if path.is_symlink():
                raise PublishValidationError(f"旧 {name} 是符号链接")
            if path.is_file():
                path.unlink()

    @staticmethod
    def _render_srt(segments: list[dict[str, Any]]) -> str:
        blocks = []
        for index, segment in enumerate(segments, start=1):
            speaker = segment.get("speaker_name") or segment.get("speaker_label")
            prefix = f"[{speaker}] " if speaker else ""
            blocks.append(
                f"{index}\n{format_srt_time(segment['start_ms'])} --> {format_srt_time(segment['end_ms'])}\n"
                f"{prefix}{segment['text']}"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _render_txt(segments: list[dict[str, Any]]) -> str:
        return render_transcript_txt(segments)

    @staticmethod
    def _render_speakers(segments: list[dict[str, Any]]) -> str:
        speakers: dict[str, str] = {}
        for segment in segments:
            if segment.get("speaker_label"):
                speakers[segment["speaker_label"]] = segment.get("speaker_name") or ""
        return "\n".join(f"{label}\t{name}" for label, name in sorted(speakers.items()))

    @staticmethod
    def _backup_existing(paths: Any, archive_dir: Path) -> None:
        existing = [path for path in paths if path.exists()]
        manifest = archive_dir / "workbench-manifest.json"
        if manifest.exists():
            existing.append(manifest)
        if not existing:
            return
        history = (
            archive_dir / ".workbench-history" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        )
        history.mkdir(parents=True, exist_ok=False)
        for path in existing:
            shutil.copy2(path, history / path.name)

    def _record_published_artifacts(
        self,
        connection: Any,
        meeting_id: str,
        paths: Any,
        manifest_path: Path,
        archive_dir: Path,
        *,
        include_all: bool,
    ) -> str:
        if include_all:
            all_paths = [
                path
                for path in archive_dir.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and not is_noise(path)
                and path.suffix.lower() in SUPPORTED_EXTENSIONS
            ]
        else:
            all_paths = [*paths, manifest_path]
        for path in all_paths:
            stat = path.stat()
            kind = artifact_kind(path)
            role = "published" if path in paths or path == manifest_path else "source"
            connection.execute(
                """INSERT INTO artifacts
                   (meeting_id, kind, role, source_root, path, sha256, size_bytes, mtime_ns, created_at)
                   VALUES (?, ?, ?, 'archive', ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET meeting_id=excluded.meeting_id,
                     kind=excluded.kind, role=excluded.role, source_root='archive',
                     sha256=excluded.sha256, size_bytes=excluded.size_bytes,
                     mtime_ns=excluded.mtime_ns""",
                (
                    meeting_id,
                    kind,
                    role,
                    str(path),
                    sha256_file(path),
                    stat.st_size,
                    stat.st_mtime_ns,
                    utc_now(),
                ),
            )
        source_paths = []
        rows = connection.execute(
            "SELECT path FROM artifacts WHERE meeting_id = ?", (meeting_id,)
        ).fetchall()
        for row in rows:
            path = Path(row["path"])
            if (
                path.is_file()
                and not path.is_symlink()
                and not is_noise(path)
                and path.suffix.lower() in SUPPORTED_EXTENSIONS
            ):
                source_paths.append(path)
        return source_signature(source_paths)
