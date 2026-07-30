from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import secrets
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .backup import BackupManager
from .db import ConflictStore, Database, utc_now
from .importer import SAFE_MEETING_ID_RE, ArchiveImporter
from .security import WriteProtectionMiddleware
from .relay_client import RelayClient, RelayUnavailable
from .semantic import SemanticBusy, SemanticIndex, SemanticPaused, SemanticUnavailable
from .service import (
    ConflictError,
    MeetingService,
    MeetingServiceError,
    NotFoundError,
    PublishValidationError,
)
from .uploads import UploadError, UploadManager
from .waveform import WaveformError, WaveformPeaks
from .quality import align_transcript_segments
from .hotwords import hotword_audit, normalize_hotwords
from .gold_schema import GoldSchemaError, validate_gold_sample
from .minutes_evidence import (
    MinutesEvidenceError,
    load_minutes_evidence,
    load_minutes_manifest,
    load_relay_attempt,
)
from .qwen_shadow import QwenShadowError, QwenShadowService


# 界面上的会议状态只保留“已完成”和“失败”。历史库里三种完成态语义不同，
# 但对使用者是同一件事：录音已经转写好、能听能搜。
MEETING_DONE_STATUSES = ("completed_unreviewed", "draft_modified", "published")
MEETING_STATUS_GROUPS: dict[str, tuple[str, ...]] = {"done": MEETING_DONE_STATUSES}
# 纪要阶段自动兜底：这些失败停在“逐字稿已好、纪要没生成”，重派一次即可救回。
RECOVERABLE_MINUTES_FAILURE_STAGES = frozenset({"codex_callback", "archive_validation"})
MINUTES_AUTO_RECOVERY_MAX_ATTEMPTS = 2
MINUTES_AUTO_RECOVERY_COOLDOWN_SECONDS = 20 * 60


class HotwordsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hotwords: list[str] = Field(default_factory=list)

    @field_validator("hotwords")
    @classmethod
    def validate_hotwords(cls, values: list[str]) -> list[str]:
        return normalize_hotwords(values)


class SegmentInput(BaseModel):
    id: str | None = None
    ordinal: int | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_label: str | None = None
    speaker_name: str | None = None
    text: str


class TranscriptDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version_id: str | None
    segments: list[SegmentInput]


class SpeakerRenameInput(BaseModel):
    label: str
    display_name: str


class SplitInput(BaseModel):
    segment_id: str
    character_index: int = Field(gt=0)


class MergeInput(BaseModel):
    first_segment_id: str
    second_segment_id: str


class MinutesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version_id: str | None
    markdown: str


class RollbackInput(BaseModel):
    version_id: str


class ProjectInput(BaseModel):
    name: str
    color: str = "#667085"


class TagInput(BaseModel):
    name: str
    color: str = "#667085"


class MeetingMetadataInput(BaseModel):
    title: str | None = None
    project_id: str | None = None
    tag_ids: list[str] | None = None


class UploadStartInput(HotwordsModel):
    filename: str
    size_bytes: int = Field(gt=0)


class UploadChunkInput(BaseModel):
    content_base64: str


class JobEnqueueInput(HotwordsModel):
    audio_path: str


class GoldSampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=20_000)
    entities: list[str] = Field(default_factory=list, max_length=100)
    numbers: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=100)


class JobRetryInput(HotwordsModel):
    stage: Literal["stabilizing", "transcribing", "transcript_ready", "minutes_generating"]


class ConflictResolutionInput(BaseModel):
    action: Literal["keep_draft", "accept_external", "discard_draft"]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _has_forbidden_control_character(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _serialize_shadow_run(row: dict[str, Any]) -> dict[str, Any]:
    try:
        metrics = json.loads(row.get("metrics_json") or "null")
    except (TypeError, json.JSONDecodeError):
        metrics = None
    return {
        "id": row["id"],
        "meeting_id": row["meeting_id"],
        "engine": row["engine"],
        "model": row["model"],
        "state": row["state"],
        "transcript_version_id": row.get("transcript_version_id"),
        "audio_sha256": row.get("audio_sha256"),
        "metrics": metrics if isinstance(metrics, dict) else None,
        "error": row.get("error"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row.get("finished_at"),
    }


def _meeting_detail(db: Database, meeting_id: str) -> dict[str, Any] | None:
    meeting = db.query_one(
        """SELECT m.*, p.name AS project_name, p.color AS project_color
             FROM meetings m LEFT JOIN projects p ON p.id = m.project_id WHERE m.id = ?""",
        (meeting_id,),
    )
    if not meeting:
        return None
    meeting["artifacts"] = db.query_all(
        """SELECT * FROM artifacts WHERE meeting_id = ?
           ORDER BY CASE source_root
             WHEN 'archive' THEN 0 WHEN 'draft' THEN 1 WHEN 'staging' THEN 2 ELSE 3 END,
             role DESC, kind, path""",
        (meeting_id,),
    )
    meeting["segments"] = (
        db.query_all(
            """SELECT s.* FROM segments s WHERE s.version_id = ? ORDER BY s.ordinal""",
            (meeting["current_transcript_version_id"],),
        )
        if meeting["current_transcript_version_id"]
        else []
    )
    meeting["transcript_versions"] = db.query_all(
        """SELECT tv.*, COUNT(s.id) AS segment_count FROM transcript_versions tv
             LEFT JOIN segments s ON s.version_id = tv.id WHERE tv.meeting_id = ?
             GROUP BY tv.id ORDER BY tv.version_no DESC""",
        (meeting_id,),
    )
    meeting["minutes_versions"] = db.query_all(
        "SELECT * FROM minutes_versions WHERE meeting_id = ? ORDER BY version_no DESC",
        (meeting_id,),
    )
    meeting["tags"] = db.query_all(
        """SELECT t.* FROM tags t JOIN meeting_tags mt ON mt.tag_id = t.id
             WHERE mt.meeting_id = ? ORDER BY t.name""",
        (meeting_id,),
    )
    meeting["speakers"] = db.query_all(
        "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY label", (meeting_id,)
    )
    meeting["events"] = db.query_all(
        "SELECT * FROM events WHERE meeting_id = ? ORDER BY id DESC LIMIT 100", (meeting_id,)
    )
    meeting["asr_shadow_runs"] = [
        _serialize_shadow_run(row)
        for row in db.query_all(
            """SELECT * FROM asr_shadow_runs WHERE meeting_id=?
               ORDER BY created_at DESC, id DESC""",
            (meeting_id,),
        )
    ]
    conflicts = ConflictStore(db).list(meeting_id)
    meeting["conflicts"] = [
        {
            "id": conflict["id"],
            "meeting_id": conflict["meeting_id"],
            "kind": conflict["kind"],
            "status": conflict["status"],
            "source_signature": conflict.get("source_signature"),
            "payload": conflict.get("payload", {}),
            "resolution": conflict.get("resolution"),
            "created_at": conflict["created_at"],
            "updated_at": conflict["updated_at"],
            "resolved_at": conflict.get("resolved_at"),
            "allowed_actions": (
                ["keep_draft", "accept_external", "discard_draft"]
                if conflict["kind"] == "external_source_change"
                else []
            ),
            "detail": conflict.get("payload", {}),
        }
        for conflict in conflicts
    ]
    meeting["conflict"] = int(bool(conflicts))
    return meeting


def create_app(
    settings: Settings | None = None, relay_client: RelayClient | None = None
) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    assert settings.database_path is not None
    db = Database(settings.database_path)
    db.initialize(before_migrate=lambda: BackupManager(db, settings).create())
    importer = ArchiveImporter(db, settings)
    service = MeetingService(
        db,
        archive_root=settings.archive_root,
        source_signature_resolver=importer.signature_for_meeting_locked,
        relay_jobs_db=settings.relay_jobs_db,
    )
    semantic = SemanticIndex(db, settings)
    waveforms = WaveformPeaks(settings)
    relay = relay_client or RelayClient(settings)
    uploads = UploadManager(settings)
    qwen = QwenShadowService(db, settings, relay)
    csrf_token = secrets.token_urlsafe(32)
    scan_lock = asyncio.Lock()

    def new_scanner_state() -> dict[str, Any]:
        return {
            "loop_alive": False,
            "in_progress": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_completed_monotonic": None,
            "consecutive_failures": 0,
            "last_error_type": None,
        }

    def new_semantic_state(status: str = "disabled") -> dict[str, Any]:
        return {
            "status": status,
            "last_completed_at": None,
            "consecutive_failures": 0,
            "last_error_type": None,
        }

    def new_qwen_worker_state() -> dict[str, Any]:
        return {
            "loop_alive": False,
            "in_progress": False,
            "last_started_at": None,
            "last_completed_at": None,
            "last_started_monotonic": None,
            "last_completed_monotonic": None,
            "consecutive_failures": 0,
            "last_error_type": None,
        }

    async def run_scan() -> Any:
        async with scan_lock:
            scanner_state = app.state.scanner_state
            scanner_state["in_progress"] = True
            scanner_state["last_started_at"] = utc_now()
            try:
                report = await asyncio.to_thread(importer.scan)
            except Exception as error:
                app.state.last_scan_error = str(error)
                scanner_state["consecutive_failures"] += 1
                scanner_state["last_error_type"] = type(error).__name__
                raise
            finally:
                scanner_state["in_progress"] = False
            app.state.last_scan = {
                name: getattr(report, name) for name in report.__dataclass_fields__
            }
            app.state.last_scan_error = None
            scanner_state["last_completed_at"] = utc_now()
            scanner_state["last_completed_monotonic"] = asyncio.get_running_loop().time()
            scanner_state["consecutive_failures"] = 0
            scanner_state["last_error_type"] = None
            return report

    def record_scanner_phase_errors(errors: list[Exception], previous_failures: int) -> None:
        if not errors:
            return
        scanner_state = app.state.scanner_state
        scanner_state["consecutive_failures"] = max(
            int(scanner_state["consecutive_failures"]), previous_failures + 1
        )
        scanner_state["last_error_type"] = type(errors[-1]).__name__
        last_scan = dict(getattr(app.state, "last_scan", {}) or {})
        last_scan["errors"] = int(last_scan.get("errors") or 0) + len(errors)
        app.state.last_scan = last_scan

    def sync_index_substates() -> None:
        if not hasattr(relay, "set_substate"):
            return
        rows = db.query_all(
            """SELECT m.source_job_id AS job_id, COUNT(s.id) AS segment_count,
                      SUM(CASE WHEN e.segment_id IS NULL THEN 1 ELSE 0 END) AS missing_count
                 FROM meetings m
                 LEFT JOIN segments s ON s.version_id=m.current_transcript_version_id
                 LEFT JOIN embeddings e ON e.segment_id=s.id AND e.model=?
                WHERE m.source_job_id IS NOT NULL
                GROUP BY m.source_job_id""",
            (settings.semantic_model,),
        )
        for row in rows:
            target = (
                "ready"
                if int(row["segment_count"] or 0) > 0 and int(row["missing_count"] or 0) == 0
                else "pending"
            )
            try:
                job = relay.status(row["job_id"])
                current = job.get("index_status") or job.get("substates", {}).get("index", {}).get(
                    "status"
                )
                if current != target:
                    current_attempt = job.get("current_attempt")
                    if isinstance(current_attempt, bool) or not isinstance(current_attempt, int):
                        raise RelayUnavailable("relayctl status 缺少有效 current_attempt")
                    relay.set_substate(row["job_id"], "index", target, attempt=current_attempt)
            except RelayUnavailable:
                continue

    async def refresh_semantic_index() -> int:
        if not settings.semantic_enabled:
            return 0
        semantic_state = app.state.semantic_details
        semantic_state["status"] = "rebuilding"
        await asyncio.to_thread(sync_index_substates)
        indexed = await asyncio.to_thread(semantic.rebuild)
        await asyncio.to_thread(sync_index_substates)
        semantic_state["status"] = "ready"
        semantic_state["last_completed_at"] = utc_now()
        semantic_state["consecutive_failures"] = 0
        semantic_state["last_error_type"] = None
        app.state.semantic_status = "ready"
        return indexed

    async def probe_relay() -> dict[str, Any]:
        def read_health() -> dict[str, Any]:
            if hasattr(relay, "health"):
                payload = relay.health()
                if isinstance(payload, dict):
                    return payload
                raise RelayUnavailable("relayctl health 返回格式错误")
            jobs = relay.list_jobs(limit=500)
            return {
                "status": "degraded",
                "mode": "legacy",
                "worker": {"state": "absent", "heartbeat_age_seconds": None},
                "counts": {
                    "queued": sum(job.get("status") == "queued" for job in jobs),
                    "active": sum(
                        job.get("status")
                        in {
                            "stabilizing",
                            "transcribing",
                            "transcript_ready",
                            "minutes_generating",
                        }
                        for job in jobs
                    ),
                    "failed": sum(job.get("status") == "failed" for job in jobs),
                },
            }

        try:
            payload = await asyncio.wait_for(asyncio.to_thread(read_health), timeout=2)
        except Exception as error:
            payload = {
                "status": "unavailable",
                "mode": "unknown",
                "worker": {"state": "absent", "heartbeat_age_seconds": None},
                "counts": {"queued": 0, "active": 0, "failed": 0},
                "error_type": type(error).__name__,
            }
        payload["probed_at"] = utc_now()
        return payload

    def enqueue_pending_uploads() -> int:
        recovered = 0
        for receipt in uploads.pending():
            upload_id = str(receipt["upload_id"])
            owner_id = f"upload-enqueue-{secrets.token_hex(16)}"
            try:
                claimed = uploads.claim_enqueue(upload_id, owner_id)
                if claimed is None or claimed.get("status") == "queued":
                    continue
                hotwords = list(claimed.get("hotwords") or [])
                job_id = relay.enqueue(str(claimed["path"]), hotwords=hotwords)
                uploads.mark_enqueued(upload_id, job_id, owner_id=owner_id)
            except (RelayUnavailable, UploadError):
                try:
                    uploads.release_enqueue(upload_id, owner_id)
                except UploadError:
                    pass
                continue
            db.add_event(
                "audio_upload_enqueue_recovered",
                job_id=job_id,
                actor="system",
                payload={"upload_id": upload_id, "hotwords": hotword_audit(hotwords)},
            )
            recovered += 1
        return recovered

    async def scan_loop(application: FastAPI) -> None:
        application.state.scanner_state["loop_alive"] = True
        semantic_retry_delay = 15.0
        semantic_retry_at = 0.0
        try:
            while True:
                await asyncio.sleep(settings.scan_interval_seconds)
                previous_failures = int(application.state.scanner_state["consecutive_failures"])
                phase_errors: list[Exception] = []
                try:
                    await asyncio.to_thread(uploads.cleanup_expired)
                except Exception as error:
                    phase_errors.append(error)
                try:
                    await asyncio.to_thread(enqueue_pending_uploads)
                except Exception as error:
                    phase_errors.append(error)
                try:
                    await run_scan()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    record_scanner_phase_errors(phase_errors, previous_failures)
                    continue
                try:
                    await asyncio.to_thread(recover_stalled_minutes)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # 兜底是旁路，失败只记账，不影响扫描与语义索引。
                    phase_errors.append(error)
                record_scanner_phase_errors(phase_errors, previous_failures)
                if not settings.semantic_enabled:
                    continue
                now = asyncio.get_running_loop().time()
                if now < semantic_retry_at:
                    continue
                try:
                    await refresh_semantic_index()
                    semantic_retry_delay = 15.0
                    semantic_retry_at = 0.0
                except asyncio.CancelledError:
                    raise
                except SemanticBusy:
                    application.state.semantic_details["status"] = "ready"
                    application.state.semantic_status = "ready"
                except SemanticPaused:
                    details = application.state.semantic_details
                    details["status"] = "paused"
                    details["consecutive_failures"] = 0
                    details["last_error_type"] = None
                    application.state.semantic_status = "paused"
                    semantic_retry_delay = 15.0
                    semantic_retry_at = now + semantic_retry_delay
                except Exception as error:
                    details = application.state.semantic_details
                    details["status"] = (
                        "unavailable" if isinstance(error, SemanticUnavailable) else "degraded"
                    )
                    details["consecutive_failures"] += 1
                    details["last_error_type"] = type(error).__name__
                    application.state.semantic_status = details["status"]
                    semantic_retry_at = now + semantic_retry_delay
                    semantic_retry_delay = min(semantic_retry_delay * 2, 600.0)
        finally:
            application.state.scanner_state["loop_alive"] = False

    async def relay_health_loop(application: FastAPI) -> None:
        while True:
            application.state.relay_health = await probe_relay()
            await asyncio.sleep(5)

    async def qwen_shadow_loop(application: FastAPI) -> None:
        state = application.state.qwen_worker_state
        state["loop_alive"] = True
        try:
            while True:
                state["in_progress"] = True
                state["last_started_at"] = utc_now()
                state["last_started_monotonic"] = asyncio.get_running_loop().time()
                try:
                    await asyncio.to_thread(qwen.run_once)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    state["consecutive_failures"] += 1
                    state["last_error_type"] = type(error).__name__
                else:
                    state["last_completed_at"] = utc_now()
                    state["last_completed_monotonic"] = asyncio.get_running_loop().time()
                    state["consecutive_failures"] = 0
                    state["last_error_type"] = None
                finally:
                    state["in_progress"] = False
                await asyncio.sleep(settings.scan_interval_seconds)
        finally:
            state["loop_alive"] = False

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.lifespan_active = True
        application.state.scanner_state = new_scanner_state()
        application.state.semantic_details = new_semantic_state()
        application.state.qwen_worker_state = new_qwen_worker_state()
        application.state.semantic_status = "disabled"
        application.state.relay_health = await probe_relay()
        await asyncio.to_thread(qwen.recover_orphaned)
        startup_phase_errors: list[Exception] = []
        try:
            await asyncio.to_thread(uploads.cleanup_expired)
        except Exception as error:
            startup_phase_errors.append(error)
        try:
            await asyncio.to_thread(enqueue_pending_uploads)
        except Exception as error:
            startup_phase_errors.append(error)
        try:
            await run_scan()
        except Exception:
            record_scanner_phase_errors(startup_phase_errors, 0)
        else:
            record_scanner_phase_errors(startup_phase_errors, 0)
        if settings.semantic_enabled:
            try:
                await asyncio.to_thread(semantic.warm)
                application.state.semantic_status = "ready"
                application.state.semantic_details["status"] = "ready"
                await refresh_semantic_index()
            except Exception as error:
                status = (
                    "paused"
                    if isinstance(error, SemanticPaused)
                    else "unavailable"
                    if isinstance(error, SemanticUnavailable)
                    else "degraded"
                )
                application.state.semantic_status = status
                application.state.semantic_details["status"] = status
                application.state.semantic_details["consecutive_failures"] = (
                    0 if isinstance(error, SemanticPaused) else 1
                )
                application.state.semantic_details["last_error_type"] = (
                    None if isinstance(error, SemanticPaused) else type(error).__name__
                )
        application.state.scanner_state["loop_alive"] = True
        scanner = asyncio.create_task(scan_loop(application), name="meeting-workbench-scan")
        relay_probe = asyncio.create_task(
            relay_health_loop(application), name="meeting-workbench-relay-health"
        )
        qwen_worker = asyncio.create_task(
            qwen_shadow_loop(application), name="meeting-workbench-qwen-shadow"
        )
        try:
            yield
        finally:
            scanner.cancel()
            relay_probe.cancel()
            qwen_worker.cancel()
            with suppress(asyncio.CancelledError):
                await scanner
            with suppress(asyncio.CancelledError):
                await relay_probe
            with suppress(asyncio.CancelledError):
                await qwen_worker
            application.state.lifespan_active = False

    app = FastAPI(title="本地会议录音工作台", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.service = service
    app.state.importer = importer
    app.state.semantic = semantic
    app.state.waveforms = waveforms
    app.state.relay = relay
    app.state.uploads = uploads
    app.state.qwen = qwen
    app.state.csrf_token = csrf_token
    app.state.scanner_state = new_scanner_state()
    app.state.semantic_details = new_semantic_state()
    app.state.qwen_worker_state = new_qwen_worker_state()
    app.state.relay_health = {
        "status": "unavailable",
        "mode": "unknown",
        "worker": {"state": "absent", "heartbeat_age_seconds": None},
        "counts": {"queued": 0, "active": 0, "failed": 0},
        "probed_at": None,
    }
    app.state.lifespan_active = False
    app.add_middleware(
        WriteProtectionMiddleware,
        cookie_name=settings.csrf_cookie_name,
        token=csrf_token,
        max_request_bytes=settings.max_json_request_bytes,
    )

    def checked_audio_artifact(artifact_id: int) -> tuple[dict[str, Any], Path]:
        artifact = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not artifact or artifact["kind"] != "audio":
            raise HTTPException(404, "音频不存在")
        path = Path(artifact["path"]).expanduser().resolve()
        allowed_roots = [
            settings.archive_root.resolve(),
            settings.staging_root.resolve(),
            settings.data_dir.resolve(),
        ]
        if not any(path.is_relative_to(root) for root in allowed_roots) or not path.is_file():
            raise HTTPException(404, "音频不可访问")
        return artifact, path

    def attach_meeting(job: dict[str, Any]) -> dict[str, Any]:
        if not job.get("job_id"):
            return job
        meeting = db.query_one(
            "SELECT id, status FROM meetings WHERE source_job_id=? LIMIT 1", (job["job_id"],)
        )
        if meeting:
            job = {**job, "meeting_id": meeting["id"]}
            latest_shadow = db.query_one(
                """SELECT state, updated_at, error FROM asr_shadow_runs
                   WHERE meeting_id=? ORDER BY created_at DESC, id DESC LIMIT 1""",
                (meeting["id"],),
            )
            if latest_shadow:
                substates = dict(job.get("substates") or {})
                substates["qwen"] = {
                    "status": latest_shadow["state"],
                    "updated_at": latest_shadow["updated_at"],
                    "error": latest_shadow.get("error"),
                }
                job = {**job, "substates": substates}
            if meeting["status"] == "draft_modified" and job.get("status") in {
                "completed_unreviewed",
                "published",
            }:
                job = {
                    **job,
                    "relay_status": job.get("status"),
                    "status": "draft_modified",
                }
        return job

    def notify_relay_draft_modified(meeting_id: str) -> None:
        meeting = db.query_one("SELECT source_job_id FROM meetings WHERE id=?", (meeting_id,))
        job_id = meeting.get("source_job_id") if meeting else None
        if not job_id or not hasattr(relay, "mark_draft_modified"):
            return
        try:
            relay.mark_draft_modified(job_id)
        except RelayUnavailable as error:
            db.add_event(
                "relay_draft_modified_sync_failed",
                meeting_id=meeting_id,
                job_id=job_id,
                actor="system",
                payload={"error": str(error)},
            )

    @app.exception_handler(MeetingServiceError)
    async def meeting_service_error(_request: Request, error: MeetingServiceError):
        if isinstance(error, NotFoundError):
            status = 404
        elif isinstance(error, (ConflictError, PublishValidationError)):
            status = 409
        else:
            status = 400
        return JSONResponse({"detail": str(error)}, status_code=status)

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        response = JSONResponse(
            {
                "csrf_token": csrf_token,
                "mobile_read_only": True,
                "semantic_enabled": settings.semantic_enabled,
            }
        )
        response.set_cookie(
            settings.csrf_cookie_name,
            csrf_token,
            httponly=False,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.get("/api/health")
    def health():
        database_ok = db.query_one("SELECT 1 AS ok") is not None
        archive_ok = settings.archive_root.is_dir() and not settings.archive_root.is_symlink()
        staging_ok = settings.staging_root.is_dir() and not settings.staging_root.is_symlink()
        last_scan = getattr(app.state, "last_scan", {}) or {}
        scan_errors = int(last_scan.get("errors") or 0)
        fatal_scan_error = getattr(app.state, "last_scan_error", None)
        scanner_details = dict(getattr(app.state, "scanner_state", new_scanner_state()))
        stale_scan = bool(
            app.state.lifespan_active
            and scanner_details.get("last_completed_monotonic") is not None
            and time.monotonic() - float(scanner_details["last_completed_monotonic"]) > 180
        )
        scanner_failed = bool(
            fatal_scan_error
            or stale_scan
            or (app.state.lifespan_active and not scanner_details.get("loop_alive"))
        )
        scanner_status = "failed" if scanner_failed else "degraded" if scan_errors else "healthy"
        relay_health = dict(getattr(app.state, "relay_health", {}) or {})
        relay_status = str(relay_health.get("status") or "unavailable")
        worker = dict(relay_health.get("worker") or {})
        worker_state = str(worker.get("state") or "absent")
        relay_worker_status = (
            "healthy"
            if relay_status == "healthy" and worker_state in {"idle", "busy"}
            else "unavailable"
            if relay_status == "unavailable" or worker_state in {"absent", "stopped"}
            else "degraded"
        )
        relay_counts = dict(relay_health.get("counts") or {})
        qwen_details = dict(getattr(app.state, "qwen_worker_state", new_qwen_worker_state()))
        qwen_counts = (
            db.query_one(
                """SELECT SUM(state='queued') AS queued_count,
                      SUM(state='running') AS running_count,
                      SUM(state='running' AND (
                          owner_id IS NULL OR heartbeat_at IS NULL
                          OR lease_expires_at IS NULL
                      )) AS orphaned_running_count,
                      MIN(CASE WHEN state='queued' THEN created_at END) AS oldest_queued_at
                 FROM asr_shadow_runs"""
            )
            or {}
        )
        queued_count = int(qwen_counts.get("queued_count") or 0)
        running_count = int(qwen_counts.get("running_count") or 0)
        orphaned_running_count = int(qwen_counts.get("orphaned_running_count") or 0)
        oldest_queued_at = qwen_counts.get("oldest_queued_at")
        queued_age_seconds: float | None = None
        if oldest_queued_at:
            try:
                queued_at = datetime.fromisoformat(str(oldest_queued_at))
                if queued_at.tzinfo is None:
                    queued_at = queued_at.replace(tzinfo=UTC)
                queued_age_seconds = max(
                    0.0,
                    (datetime.now(UTC) - queued_at.astimezone(UTC)).total_seconds(),
                )
            except (TypeError, ValueError):
                queued_age_seconds = None
        queued_stale = bool(queued_age_seconds is not None and queued_age_seconds > 180)
        cycle_stale = False
        monotonic_now = time.monotonic()
        if app.state.lifespan_active:
            if not qwen_details.get("loop_alive"):
                cycle_stale = True
            elif qwen_details.get("in_progress") and running_count == 0:
                started = qwen_details.get("last_started_monotonic")
                cycle_stale = bool(started is not None and monotonic_now - float(started) > 180)
            elif not qwen_details.get("in_progress"):
                completed = qwen_details.get("last_completed_monotonic")
                cycle_stale = bool(completed is not None and monotonic_now - float(completed) > 180)
        qwen_failures = int(qwen_details.get("consecutive_failures") or 0)
        qwen_worker_status = (
            "failed"
            if cycle_stale or qwen_failures >= 3
            else "degraded"
            if qwen_failures or queued_stale or orphaned_running_count
            else "healthy"
        )
        backup_details: dict[str, Any] = {
            "last_success_at": None,
            "mirror_ok": None,
            "age_seconds": None,
            "stale": None,
            "receipt_status": None,
        }
        backup_status = "unknown"
        backup_receipt = settings.backup_dir / "last-backup.json"
        if backup_receipt.is_file() and not backup_receipt.is_symlink():
            try:
                payload = json.loads(backup_receipt.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    completed_at = payload.get("created_at") or payload.get("completed_at")
                    completed = datetime.fromisoformat(str(completed_at))
                    if completed.tzinfo is None:
                        completed = completed.replace(tzinfo=UTC)
                    age_seconds = max(
                        0.0,
                        (datetime.now(UTC) - completed.astimezone(UTC)).total_seconds(),
                    )
                    stale = age_seconds > settings.backup_stale_after_seconds
                    backup_details = {
                        "last_success_at": completed_at,
                        "mirror_ok": payload.get("mirror_ok"),
                        "age_seconds": round(age_seconds, 3),
                        "stale": stale,
                        "receipt_status": payload.get("status"),
                    }
                    backup_status = (
                        "healthy"
                        if payload.get("status") == "healthy"
                        and payload.get("mirror_ok") is True
                        and not stale
                        else "degraded"
                    )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                backup_status = "degraded"
        scanner_details.pop("last_completed_monotonic", None)
        return {
            "status": "ok"
            if database_ok
            and archive_ok
            and staging_ok
            and relay_status == "healthy"
            and relay_worker_status == "healthy"
            and scanner_status == "healthy"
            and qwen_worker_status == "healthy"
            and backup_status == "healthy"
            else "degraded",
            "services": {
                "database": "healthy" if database_ok else "failed",
                "archive": "healthy" if archive_ok else "unavailable",
                "staging": "healthy" if staging_ok else "unavailable",
                "semantic": getattr(
                    app.state,
                    "semantic_status",
                    "enabled" if settings.semantic_enabled else "disabled",
                ),
                "relay": relay_status,
                "relay_worker": relay_worker_status,
                "qwen_worker": qwen_worker_status,
                "scanner": scanner_status,
                "backup": backup_status,
            },
            "counts": {
                "meetings": db.query_one("SELECT COUNT(*) AS count FROM meetings")["count"],
                "unreviewed": db.query_one(
                    "SELECT COUNT(*) AS count FROM meetings WHERE status = 'completed_unreviewed'"
                )["count"],
                "failed_jobs": int(relay_counts.get("failed") or 0),
                "queued_jobs": int(relay_counts.get("queued") or 0),
                "scan_errors": scan_errors,
            },
            "details": {
                "scanner": scanner_details,
                "semantic": dict(getattr(app.state, "semantic_details", new_semantic_state())),
                "relay_worker": {
                    "state": worker_state,
                    "heartbeat_age_seconds": worker.get("heartbeat_age_seconds"),
                    "probed_at": relay_health.get("probed_at"),
                },
                "qwen_worker": {
                    "loop_alive": bool(qwen_details.get("loop_alive")),
                    "in_progress": bool(qwen_details.get("in_progress")),
                    "last_started_at": qwen_details.get("last_started_at"),
                    "last_completed_at": qwen_details.get("last_completed_at"),
                    "consecutive_failures": qwen_failures,
                    "last_error_type": qwen_details.get("last_error_type"),
                    "stale": cycle_stale,
                    "queued_count": queued_count,
                    "running_count": running_count,
                    "orphaned_running_count": orphaned_running_count,
                    "queued_stale": queued_stale,
                    "oldest_queued_age_seconds": (
                        round(queued_age_seconds, 3) if queued_age_seconds is not None else None
                    ),
                },
                "backup": backup_details,
            },
        }

    @app.get("/api/meetings")
    def list_meetings(
        q: str | None = None,
        project_id: str | None = None,
        tag_id: str | None = None,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        participant: str | None = None,
        min_duration_ms: int | None = None,
        max_duration_ms: int | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        joins = ["LEFT JOIN projects p ON p.id = m.project_id"]
        clauses = ["1=1"]
        params: list[Any] = []
        if tag_id:
            joins.append("JOIN meeting_tags mt_filter ON mt_filter.meeting_id = m.id")
            clauses.append("mt_filter.tag_id = ?")
            params.append(tag_id)
        if status is not None:
            # 界面只区分“已完成 / 失败”；历史库里三种完成态都算已完成。
            grouped = MEETING_STATUS_GROUPS.get(status)
            if grouped:
                clauses.append(f"m.status IN ({', '.join('?' for _ in grouped)})")
                params.extend(grouped)
            else:
                clauses.append("m.status = ?")
                params.append(status)
        for value, clause in (
            (project_id, "m.project_id = ?"),
            (date_from, "m.recording_date >= ?"),
            (date_to, "m.recording_date <= ?"),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        if min_duration_ms is not None:
            clauses.append("m.duration_ms >= ?")
            params.append(min_duration_ms)
        if max_duration_ms is not None:
            clauses.append("m.duration_ms <= ?")
            params.append(max_duration_ms)
        if participant:
            clauses.append(
                """EXISTS (SELECT 1 FROM speakers sp WHERE sp.meeting_id = m.id
                   AND sp.display_name LIKE ? ESCAPE '\\')"""
            )
            params.append(f"%{_escape_like(participant)}%")
        if q:
            clauses.append(
                """(m.title LIKE ? ESCAPE '\\' OR EXISTS (
                    SELECT 1 FROM segments s WHERE s.meeting_id = m.id
                    AND s.version_id = m.current_transcript_version_id
                    AND s.text LIKE ? ESCAPE '\\'))"""
            )
            escaped_query = _escape_like(q)
            params.extend((f"%{escaped_query}%", f"%{escaped_query}%"))
        count_sql = f"""
            SELECT COUNT(DISTINCT m.id) AS count
              FROM meetings m {" ".join(joins)}
             WHERE {" AND ".join(clauses)}
        """
        total = int(db.query_one(count_sql, params)["count"])
        sql = f"""
            SELECT m.*, p.name AS project_name, p.color AS project_color,
                   (SELECT a.id FROM artifacts a WHERE a.meeting_id = m.id AND a.kind = 'audio'
                    ORDER BY CASE a.source_root WHEN 'archive' THEN 0 ELSE 1 END LIMIT 1) AS audio_artifact_id,
                   (SELECT COUNT(*) FROM segments s WHERE s.version_id = m.current_transcript_version_id) AS segment_count
              FROM meetings m {" ".join(joins)}
             WHERE {" AND ".join(clauses)}
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC
             LIMIT ? OFFSET ?
        """
        params.extend((limit, offset))
        rows = db.query_all(sql, params)
        for row in rows:
            row["tags"] = db.query_all(
                """SELECT t.* FROM tags t JOIN meeting_tags mt ON mt.tag_id=t.id
                   WHERE mt.meeting_id=? ORDER BY t.name""",
                (row["id"],),
            )
        return {"items": rows, "limit": limit, "offset": offset, "total": total}

    @app.get("/api/meetings/{meeting_id}")
    def get_meeting(meeting_id: str):
        detail = _meeting_detail(db, meeting_id)
        if not detail:
            raise HTTPException(404, "会议不存在")
        return detail

    @app.get("/api/meetings/{meeting_id}/minutes-evidence")
    def minutes_evidence(meeting_id: str):
        current = db.query_one(
            """SELECT m.id AS existing_meeting_id, mv.* FROM meetings m
                 LEFT JOIN minutes_versions mv ON mv.id=m.current_minutes_version_id
                WHERE m.id=?""",
            (meeting_id,),
        )
        if not current:
            raise HTTPException(404, "会议不存在")
        if current.get("id") is None:
            raise HTTPException(404, "该会议暂无可验证证据")
        source_job_id = current.get("source_job_id")
        source_attempt = current.get("source_attempt")
        content_sha256 = current.get("content_sha256")
        current_markdown = current.get("markdown")
        if (
            not isinstance(source_job_id, str)
            or not source_job_id
            or not isinstance(source_attempt, int)
            or isinstance(source_attempt, bool)
            or not isinstance(content_sha256, str)
            or not isinstance(current_markdown, str)
            or hashlib.sha256(current_markdown.encode("utf-8")).hexdigest() != content_sha256
        ):
            raise HTTPException(409, "该会议当前纪要缺少可验证来源")
        try:
            archive_root = settings.archive_root.resolve()
            manifest_candidates = []
            for artifact in db.query_all(
                """SELECT path, sha256, source_root FROM artifacts
                    WHERE meeting_id=? AND kind='manifest'
                      AND source_root IN ('archive', 'draft')""",
                (meeting_id,),
            ):
                manifest_path = Path(artifact["path"])
                try:
                    resolved = manifest_path.resolve(strict=True)
                    if (
                        manifest_path.is_symlink()
                        or manifest_path.name != "workbench-manifest.json"
                        or not resolved.is_relative_to(archive_root)
                        or manifest_path.stat().st_size > 2 * 1024 * 1024
                    ):
                        continue
                    peek = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(peek, dict)
                    and peek.get("job_id") == source_job_id
                    and peek.get("attempt") == source_attempt
                ):
                    manifest_candidates.append((artifact, manifest_path))
            if len(manifest_candidates) != 1:
                raise MinutesEvidenceError("暂无可验证证据：manifest 关联不唯一")
            manifest_artifact, manifest_path = manifest_candidates[0]
            manifest = load_minutes_manifest(
                manifest_path,
                expected_sha256=manifest_artifact.get("sha256") or "",
                meeting_id=meeting_id,
                source_job_id=source_job_id,
                source_attempt=source_attempt,
                requested_stage=current.get("requested_stage"),
                input_transcript_sha256=current.get("input_transcript_sha256"),
            )
            manifest_payload = manifest["payload"]
            entries = manifest["entries"]
            relay_attempt = load_relay_attempt(
                settings.relay_jobs_db,
                job_id=source_job_id,
                attempt=source_attempt,
            )
            if (
                relay_attempt.get("minutes_protocol_version") != 3
                or relay_attempt.get("requested_stage") != current.get("requested_stage")
                or relay_attempt.get("input_transcript_sha256")
                != current.get("input_transcript_sha256")
            ):
                raise MinutesEvidenceError("纪要版本与 Relay attempt 不一致")

            def trusted_attempt_hash(name: str) -> str:
                manifest_value = manifest_payload.get(name)
                relay_value = relay_attempt.get(name)
                if manifest_value is not None and manifest_value != relay_value:
                    raise MinutesEvidenceError("纪要 manifest 与 Relay attempt 哈希不一致")
                value = manifest_value or relay_value
                if not isinstance(value, str) or len(value) != 64:
                    raise MinutesEvidenceError("纪要 attempt 缺少可信哈希")
                return value

            expected_plan_sha256 = trusted_attempt_hash("minutes_plan_sha256")
            expected_source_srt_sha256 = trusted_attempt_hash("source_srt_sha256")

            evidence_entry = entries.get("minutes-evidence.json")
            plan_entry = entries.get("minutes-plan.json")
            minutes_entries = [
                entry
                for relative, entry in entries.items()
                if Path(relative).parent == Path(".")
                and Path(relative).suffix.casefold() == ".md"
                and entry["sha256"] == content_sha256
            ]
            source_entries = [
                entry
                for relative, entry in entries.items()
                if Path(relative).parent == Path(".")
                and Path(relative).suffix.casefold() == ".srt"
                and entry["sha256"] == expected_source_srt_sha256
            ]
            if (
                evidence_entry is None
                or plan_entry is None
                or plan_entry["sha256"] != expected_plan_sha256
                or len(minutes_entries) != 1
                or len(source_entries) != 1
                or (
                    current.get("requested_stage") == "minutes_generating"
                    and source_entries[0]["path"] != "input-transcript.srt"
                )
            ):
                raise MinutesEvidenceError("暂无可验证证据：manifest 关联产物不唯一")

            source_root = manifest_artifact["source_root"]

            def indexed_artifact(entry: dict[str, Any], kinds: set[str]) -> Path:
                artifact_path = manifest_path.parent / entry["path"]
                indexed = db.query_one(
                    """SELECT meeting_id, kind, source_root, sha256, size_bytes
                         FROM artifacts WHERE path=?""",
                    (str(artifact_path),),
                )
                if (
                    not indexed
                    or indexed["meeting_id"] != meeting_id
                    or indexed["kind"] not in kinds
                    or indexed["source_root"] != source_root
                    or indexed.get("sha256") != entry["sha256"]
                    or indexed.get("size_bytes") != entry["bytes"]
                ):
                    raise MinutesEvidenceError("纪要 manifest 产物未被精确索引")
                return artifact_path

            evidence_path = indexed_artifact(evidence_entry, {"minutes_evidence"})
            plan_path = indexed_artifact(plan_entry, {"minutes_plan"})
            minutes_path = indexed_artifact(minutes_entries[0], {"minutes_md", "document_md"})
            source_srt_path = indexed_artifact(source_entries[0], {"srt", "whisper_srt"})
            return load_minutes_evidence(
                evidence_path,
                expected_sha256=evidence_entry["sha256"],
                plan_path=plan_path,
                expected_plan_sha256=expected_plan_sha256,
                minutes_path=minutes_path,
                expected_minutes_sha256=content_sha256,
                source_srt_path=source_srt_path,
                expected_source_srt_sha256=expected_source_srt_sha256,
                expected_input_transcript_sha256=current.get("input_transcript_sha256"),
            )
        except (OSError, MinutesEvidenceError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/meetings/{meeting_id}/asr-shadow/qwen", status_code=202)
    def request_qwen_shadow(meeting_id: str, _body: dict[str, Any]):
        try:
            return _serialize_shadow_run(qwen.request(meeting_id))
        except QwenShadowError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/meetings/{meeting_id}/asr-shadow/qwen/{run_id}/retry", status_code=202)
    def retry_qwen_shadow(meeting_id: str, run_id: str, _body: dict[str, Any]):
        try:
            return _serialize_shadow_run(qwen.retry(meeting_id, run_id))
        except QwenShadowError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/meetings/{meeting_id}/transcript-versions/{version_id}/segments")
    def transcript_version_segments(meeting_id: str, version_id: str):
        version = db.query_one(
            """SELECT * FROM transcript_versions
               WHERE id = ? AND meeting_id = ?""",
            (version_id, meeting_id),
        )
        if not version:
            raise HTTPException(404, "逐字稿版本不存在")
        return {
            "version": version,
            "items": db.query_all(
                "SELECT * FROM segments WHERE version_id = ? ORDER BY ordinal",
                (version_id,),
            ),
        }

    @app.get("/api/meetings/{meeting_id}/transcript-comparison")
    def transcript_comparison(meeting_id: str, candidate_version_id: str):
        meeting = db.query_one(
            "SELECT current_transcript_version_id FROM meetings WHERE id=?",
            (meeting_id,),
        )
        if not meeting:
            raise HTTPException(404, "会议不存在")
        primary_version_id = meeting["current_transcript_version_id"]
        if not primary_version_id:
            raise HTTPException(409, "会议尚无当前逐字稿")
        candidate_version = db.query_one(
            "SELECT id, kind FROM transcript_versions WHERE id=? AND meeting_id=?",
            (candidate_version_id, meeting_id),
        )
        if not candidate_version:
            raise HTTPException(404, "候选逐字稿版本不存在")
        primary = db.query_all(
            "SELECT id, start_ms, end_ms, text FROM segments WHERE version_id=? ORDER BY ordinal",
            (primary_version_id,),
        )
        candidate = db.query_all(
            "SELECT id, start_ms, end_ms, text FROM segments WHERE version_id=? ORDER BY ordinal",
            (candidate_version_id,),
        )
        return {
            "primary_version_id": primary_version_id,
            "candidate_version_id": candidate_version_id,
            "candidate_kind": candidate_version["kind"],
            "items": align_transcript_segments(primary, candidate),
        }

    def serialize_gold_sample(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "meeting_id": row["meeting_id"],
            "segment_id": row.get("segment_id"),
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "reference": row["reference"],
            "entities": json.loads(row["entities_json"]),
            "numbers": json.loads(row["numbers_json"]),
            "tags": json.loads(row["tags_json"]),
            "source_audio_sha256": row.get("source_audio_sha256"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @app.get("/api/meetings/{meeting_id}/asr-gold-samples")
    def list_asr_gold_samples(meeting_id: str):
        if not db.query_one("SELECT id FROM meetings WHERE id=?", (meeting_id,)):
            raise HTTPException(404, "会议不存在")
        rows = db.query_all(
            "SELECT * FROM asr_gold_samples WHERE meeting_id=? ORDER BY start_ms, id",
            (meeting_id,),
        )
        return {"items": [serialize_gold_sample(row) for row in rows]}

    @app.post("/api/meetings/{meeting_id}/asr-gold-samples")
    def save_asr_gold_sample(meeting_id: str, body: GoldSampleInput):
        source = db.query_one(
            """SELECT s.id, s.start_ms, s.end_ms, m.original_audio_sha256
                 FROM segments s JOIN meetings m ON m.id=s.meeting_id
                WHERE s.id=? AND s.meeting_id=?""",
            (body.segment_id, meeting_id),
        )
        if not source:
            raise HTTPException(404, "会议或逐字稿段落不存在")
        now = utc_now()
        with db.transaction() as connection:
            existing = connection.execute(
                "SELECT id, created_at FROM asr_gold_samples WHERE meeting_id=? AND segment_id=?",
                (meeting_id, body.segment_id),
            ).fetchone()
            sample_id = existing["id"] if existing else f"gold-{secrets.token_hex(16)}"
            created_at = existing["created_at"] if existing else now
            try:
                sample = validate_gold_sample(
                    {
                        "schema_version": 2,
                        "id": sample_id,
                        "reference": body.reference,
                        "entities": body.entities,
                        "numbers": body.numbers,
                        "tags": body.tags,
                        "start_ms": source["start_ms"],
                        "end_ms": source["end_ms"],
                        **(
                            {"source_audio_sha256": source["original_audio_sha256"]}
                            if source.get("original_audio_sha256") is not None
                            else {}
                        ),
                    }
                )
            except GoldSchemaError as error:
                raise HTTPException(422, str(error)) from error
            entities_json = json.dumps(
                sample["entities"], ensure_ascii=False, separators=(",", ":")
            )
            numbers_json = json.dumps(sample["numbers"], ensure_ascii=False, separators=(",", ":"))
            tags_json = json.dumps(sample["tags"], ensure_ascii=False, separators=(",", ":"))
            if existing:
                connection.execute(
                    """UPDATE asr_gold_samples
                          SET start_ms=?, end_ms=?, reference=?, entities_json=?, numbers_json=?,
                              tags_json=?, source_audio_sha256=?, updated_at=?
                        WHERE id=?""",
                    (
                        source["start_ms"],
                        source["end_ms"],
                        sample["reference"],
                        entities_json,
                        numbers_json,
                        tags_json,
                        source.get("original_audio_sha256"),
                        now,
                        sample_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO asr_gold_samples
                       (id, meeting_id, segment_id, start_ms, end_ms, reference,
                        entities_json, numbers_json, tags_json, source_audio_sha256,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sample_id,
                        meeting_id,
                        body.segment_id,
                        source["start_ms"],
                        source["end_ms"],
                        sample["reference"],
                        entities_json,
                        numbers_json,
                        tags_json,
                        source.get("original_audio_sha256"),
                        created_at,
                        now,
                    ),
                )
            db.add_event(
                "asr_gold_sample_saved",
                meeting_id=meeting_id,
                actor="user",
                payload={
                    "sample_id": sample_id,
                    "segment_id": body.segment_id,
                    "start_ms": source["start_ms"],
                    "end_ms": source["end_ms"],
                },
                connection=connection,
            )
            saved_row = connection.execute(
                "SELECT * FROM asr_gold_samples WHERE id=?", (sample_id,)
            ).fetchone()
            saved = dict(saved_row) if saved_row else None
        assert saved is not None
        return serialize_gold_sample(saved)

    @app.get("/api/search")
    def search(
        q: str,
        mode: Literal["exact", "semantic"] = "exact",
        limit: int = Query(30, ge=1, le=100),
    ):
        if _has_forbidden_control_character(q):
            raise HTTPException(422, "搜索词包含禁止控制字符")
        if mode == "exact":
            return {"mode": mode, "items": db.exact_search(q, limit=limit)}
        try:
            return {"mode": mode, "items": semantic.search(q, limit=limit)}
        except SemanticBusy as error:
            raise HTTPException(409, str(error)) from error
        except SemanticPaused as error:
            raise HTTPException(409, str(error)) from error
        except SemanticUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/api/media/{artifact_id}")
    def media(artifact_id: int):
        _artifact, path = checked_audio_artifact(artifact_id)
        return FileResponse(path, filename=path.name, content_disposition_type="inline")

    @app.get("/api/media/{artifact_id}/peaks")
    def media_peaks(artifact_id: int):
        artifact, path = checked_audio_artifact(artifact_id)
        try:
            return waveforms.get(path, artifact.get("sha256"))
        except WaveformError as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/api/jobs")
    def list_jobs(
        status: str | None = None,
        limit: int = Query(200, ge=1, le=500),
    ):
        try:
            return {
                "items": [
                    attach_meeting(job) for job in relay.list_jobs(status=status, limit=limit)
                ]
            }
        except RelayUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return attach_meeting(relay.status(job_id))
        except RelayUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.post("/api/jobs/enqueue")
    def enqueue_job(body: JobEnqueueInput):
        try:
            job_id = relay.enqueue(body.audio_path, hotwords=body.hotwords)
            db.add_event(
                "job_enqueued",
                job_id=job_id,
                actor="user",
                payload={"hotwords": hotword_audit(body.hotwords)},
            )
            return {"job_id": job_id, "status": "queued"}
        except RelayUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, body: JobRetryInput):
        try:
            result = relay.retry(job_id, body.stage, hotwords=body.hotwords)
            db.add_event(
                "job_retry_requested",
                job_id=job_id,
                actor="user",
                payload={"stage": body.stage, "hotwords": hotword_audit(body.hotwords)},
            )
            return result
        except RelayUnavailable as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/jobs/{job_id}/stop-after-stage")
    def stop_job_after_stage(job_id: str, _body: dict[str, Any]):
        try:
            result = relay.stop_after_stage(job_id)
            db.add_event("job_stop_after_stage_requested", job_id=job_id, actor="user")
            return result
        except RelayUnavailable as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, _body: dict[str, Any]):
        try:
            result = relay.cancel(job_id)
            db.add_event("job_cancelled", job_id=job_id, actor="user")
            return result
        except RelayUnavailable as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/jobs/{job_id}/substates/{name}/retry")
    def retry_job_substate(job_id: str, name: Literal["whisper", "index"], _body: dict[str, Any]):
        try:
            result = relay.retry_substate(job_id, name)
            db.add_event(
                "job_substate_retry_requested",
                job_id=job_id,
                actor="user",
                payload={"name": name},
            )
            return result
        except (RelayUnavailable, AttributeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/admin/scan")
    async def scan(_body: dict[str, Any]):
        report = await run_scan()
        payload = {name: getattr(report, name) for name in report.__dataclass_fields__}
        db.add_event("archive_scan_requested", actor="user", payload=payload)
        return payload

    @app.post("/api/admin/semantic/rebuild")
    def semantic_rebuild(_body: dict[str, Any]):
        try:
            indexed = semantic.rebuild(force=True)
            sync_index_substates()
            db.add_event("semantic_rebuild_requested", actor="user", payload={"indexed": indexed})
            return {"indexed": indexed}
        except SemanticBusy as error:
            raise HTTPException(409, str(error)) from error
        except SemanticPaused as error:
            raise HTTPException(409, str(error)) from error
        except SemanticUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.post("/api/admin/backup")
    def create_backup(_body: dict[str, Any]):
        result = BackupManager(db, settings).create()
        db.add_event("database_backup_created", actor="user")
        return {
            "local_path": str(result.local_path),
            "mirror_path": str(result.mirror_path) if result.mirror_path else None,
            "status": result.status,
            "local_sha256": result.local_sha256,
            "mirror_sha256": result.mirror_sha256,
            "mirror_error": result.mirror_error,
        }

    @app.put("/api/meetings/{meeting_id}/transcript")
    def save_transcript(meeting_id: str, body: TranscriptDraftInput):
        version_id = service.save_segments(
            meeting_id,
            [segment.model_dump(exclude_none=True) for segment in body.segments],
            expected_base_version_id=body.base_version_id,
        )
        notify_relay_draft_modified(meeting_id)
        return {"version_id": version_id}

    @app.post("/api/meetings/{meeting_id}/speakers/rename")
    def rename_speaker(meeting_id: str, body: SpeakerRenameInput):
        updated = service.rename_speaker(meeting_id, body.label, body.display_name)
        notify_relay_draft_modified(meeting_id)
        return {"updated": updated}

    @app.post("/api/meetings/{meeting_id}/segments/split")
    def split_segment(meeting_id: str, body: SplitInput):
        segment_ids = service.split_segment(meeting_id, body.segment_id, body.character_index)
        notify_relay_draft_modified(meeting_id)
        return {"segment_ids": segment_ids}

    @app.post("/api/meetings/{meeting_id}/segments/merge")
    def merge_segments(meeting_id: str, body: MergeInput):
        segment_id = service.merge_segments(
            meeting_id, body.first_segment_id, body.second_segment_id
        )
        notify_relay_draft_modified(meeting_id)
        return {"segment_id": segment_id}

    @app.put("/api/meetings/{meeting_id}/minutes")
    def save_minutes(meeting_id: str, body: MinutesInput):
        version_id = service.save_minutes(
            meeting_id,
            body.markdown,
            expected_base_version_id=body.base_version_id,
        )
        notify_relay_draft_modified(meeting_id)
        return {"version_id": version_id}

    def preferred_audio_path(meeting_id: str) -> Path | None:
        artifact = db.query_one(
            """SELECT path FROM artifacts WHERE meeting_id = ? AND kind = 'audio'
               ORDER BY CASE source_root
                 WHEN 'archive' THEN 0 WHEN 'draft' THEN 1 WHEN 'staging' THEN 2 ELSE 3 END
               LIMIT 1""",
            (meeting_id,),
        )
        if not artifact:
            return None
        path = Path(artifact["path"])
        return path if path.is_file() and not path.is_symlink() else None

    def ensure_relay_job(
        meeting_id: str, hotwords: list[str] | None = None
    ) -> tuple[dict[str, Any], bool]:
        meeting = db.query_one("SELECT id, source_job_id FROM meetings WHERE id = ?", (meeting_id,))
        if not meeting:
            raise HTTPException(404, "会议不存在")
        if meeting["source_job_id"]:
            try:
                return relay.status(meeting["source_job_id"]), False
            except RelayUnavailable as error:
                raise HTTPException(409, str(error)) from error
        audio = preferred_audio_path(meeting_id)
        if not audio:
            raise HTTPException(409, "该历史会议没有可用原音频，无法重新处理")
        try:
            job_id = relay.enqueue(audio, hotwords=hotwords)
            status = relay.status(job_id)
        except RelayUnavailable as error:
            raise HTTPException(409, str(error)) from error
        db.execute(
            "UPDATE meetings SET source_job_id=?, updated_at=? WHERE id=?",
            (job_id, utc_now(), meeting_id),
        )
        return status, True

    def request_minutes_regeneration(
        meeting_id: str,
        *,
        actor: str = "user",
        event_type: str = "minutes_regeneration_requested",
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meeting = db.query_one("SELECT id, source_job_id FROM meetings WHERE id=?", (meeting_id,))
        if not meeting:
            raise HTTPException(404, "会议不存在")
        audio = None
        if not meeting.get("source_job_id"):
            audio = preferred_audio_path(meeting_id)
            if not audio:
                raise HTTPException(409, "该历史会议没有可用原音频，无法重新处理")
        snapshot_path, snapshot_sha256 = service.create_transcript_snapshot(
            meeting_id, settings.data_dir / "relay-inputs"
        )
        try:
            if meeting.get("source_job_id"):
                job_id = meeting["source_job_id"]
                try:
                    status = relay.status(job_id)
                except RelayUnavailable as error:
                    raise HTTPException(409, str(error)) from error
                current = status.get("status")
                if current not in {
                    "failed",
                    "cancelled",
                    "interrupted",
                    "completed_unreviewed",
                    "draft_modified",
                    "published",
                }:
                    raise HTTPException(
                        409, f"任务正在处理（{current or 'unknown'}），暂不能重生成"
                    )
                try:
                    result = relay.retry(
                        job_id,
                        "minutes_generating",
                        transcript_path=snapshot_path,
                    )
                except RelayUnavailable as error:
                    raise HTTPException(409, str(error)) from error
            else:
                assert audio is not None
                try:
                    job_id = relay.enqueue(
                        audio,
                        stage="minutes_generating",
                        transcript_path=snapshot_path,
                    )
                except RelayUnavailable as error:
                    raise HTTPException(409, str(error)) from error
                db.execute(
                    "UPDATE meetings SET source_job_id=?, updated_at=? WHERE id=?",
                    (job_id, utc_now(), meeting_id),
                )
                result = {"job_id": job_id, "status": "queued"}
        finally:
            snapshot_path.unlink(missing_ok=True)
        db.add_event(
            event_type,
            meeting_id=meeting_id,
            job_id=job_id,
            actor=actor,
            payload={"input_transcript_sha256": snapshot_sha256, **(extra_payload or {})},
        )
        return {"status": "queued", "meeting_id": meeting_id, **result}

    @app.post("/api/meetings/{meeting_id}/minutes/regenerate")
    def regenerate_minutes(meeting_id: str, _body: dict[str, Any]):
        return request_minutes_regeneration(meeting_id)

    def find_meeting_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
        """把 relay 任务对回工作台会议。

        纪要没生成的任务从来没写过 manifest，`source_job_id` 也就一直是空的。
        这时用录音文件名兜底：它与会议编号同源，命中后顺手把关联补上。
        """
        select = """SELECT m.id, m.source_job_id,
                           (SELECT COUNT(*) FROM segments s
                             WHERE s.version_id = m.current_transcript_version_id)
                             AS segment_count,
                           (SELECT COUNT(*) FROM minutes_versions mv
                             WHERE mv.meeting_id = m.id) AS minutes_count
                      FROM meetings m WHERE {condition} LIMIT 1"""
        job_id = str(job.get("job_id") or job.get("id") or "")
        meeting = db.query_one(select.format(condition="m.source_job_id = ?"), (job_id,))
        if meeting:
            return meeting
        audio_path = job.get("audio_path")
        if not audio_path and job_id:
            # 任务列表不返回录音路径，只有单个任务详情里才有。
            try:
                audio_path = relay.status(job_id).get("audio_path")
            except (RelayUnavailable, AttributeError):
                return None
        if not audio_path:
            return None
        candidate = Path(str(audio_path)).stem.strip().lower()
        if not SAFE_MEETING_ID_RE.match(candidate):
            return None
        meeting = db.query_one(select.format(condition="m.id = ?"), (candidate,))
        if not meeting or meeting.get("source_job_id"):
            return None
        db.execute(
            "UPDATE meetings SET source_job_id = ?, updated_at = ? WHERE id = ? "
            "AND source_job_id IS NULL",
            (job_id, utc_now(), candidate),
        )
        return meeting

    def recover_stalled_minutes() -> int:
        """自动救回“逐字稿已好、纪要没生成”的任务。

        纪要 Agent 掉线时 relay 只会把任务收口成 failed，不会重派；没有纪要就没有
        标题，会议在资料库里只剩一串编号。这里按有限次数和冷却窗口自动重派一次，
        既不放着不管，也不会对同一个任务无限重试。
        """
        try:
            jobs = relay.list_jobs(status="failed", limit=200)
        except (RelayUnavailable, AttributeError):
            return 0
        recovered = 0
        for job in jobs:
            job_id = job.get("job_id") or job.get("id")
            if not job_id or job.get("failure_stage") not in RECOVERABLE_MINUTES_FAILURE_STAGES:
                continue
            meeting = find_meeting_for_job(job)
            if not meeting or int(meeting["segment_count"] or 0) <= 0:
                continue
            if int(meeting["minutes_count"] or 0) > 0:
                continue
            history = db.query_one(
                """SELECT COUNT(*) AS count, MAX(created_at) AS last_at FROM events
                    WHERE job_id = ? AND event_type = 'minutes_auto_recovery_requested'""",
                (job_id,),
            )
            attempts = int(history["count"] or 0) if history else 0
            if attempts >= MINUTES_AUTO_RECOVERY_MAX_ATTEMPTS:
                continue
            last_at = history.get("last_at") if history else None
            if last_at:
                try:
                    elapsed = (datetime.now(UTC) - datetime.fromisoformat(last_at)).total_seconds()
                except ValueError:
                    elapsed = MINUTES_AUTO_RECOVERY_COOLDOWN_SECONDS
                if elapsed < MINUTES_AUTO_RECOVERY_COOLDOWN_SECONDS:
                    continue
            try:
                request_minutes_regeneration(
                    meeting["id"],
                    actor="system",
                    event_type="minutes_auto_recovery_requested",
                    extra_payload={
                        "failure_stage": job.get("failure_stage"),
                        "attempt": attempts + 1,
                    },
                )
            except (HTTPException, MeetingServiceError, RelayUnavailable, OSError):
                continue
            recovered += 1
        return recovered

    app.state.recover_stalled_minutes = recover_stalled_minutes

    @app.post("/api/meetings/{meeting_id}/retranscribe")
    def retranscribe(meeting_id: str, body: HotwordsModel):
        status, created = ensure_relay_job(meeting_id, body.hotwords)
        job_id = status["job_id"]
        current = status.get("status")
        if created or current in {
            "discovered",
            "stabilizing",
            "queued",
            "transcribing",
            "transcript_ready",
            "minutes_generating",
        }:
            result = {"job_id": job_id, "status": current or "queued"}
        else:
            try:
                result = relay.retry(job_id, "transcribing", hotwords=body.hotwords)
            except RelayUnavailable as error:
                raise HTTPException(409, str(error)) from error
        db.add_event(
            "retranscription_requested",
            meeting_id=meeting_id,
            job_id=job_id,
            actor="user",
            payload={"hotwords": hotword_audit(body.hotwords)},
        )
        return {"status": "queued", "meeting_id": meeting_id, **result}

    @app.post("/api/meetings/{meeting_id}/rollback/transcript")
    def rollback_transcript(meeting_id: str, body: RollbackInput):
        service.rollback_transcript(meeting_id, body.version_id)
        notify_relay_draft_modified(meeting_id)
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/rollback/minutes")
    def rollback_minutes(meeting_id: str, body: RollbackInput):
        service.rollback_minutes(meeting_id, body.version_id)
        notify_relay_draft_modified(meeting_id)
        return {"ok": True}

    @app.post("/api/meetings/{meeting_id}/publish")
    def publish(meeting_id: str, _body: dict[str, Any]):
        result = asdict(service.publish(meeting_id))
        meeting = db.query_one("SELECT source_job_id FROM meetings WHERE id=?", (meeting_id,))
        job_id = meeting.get("source_job_id") if meeting else None
        if job_id:
            try:
                relay.mark_published(job_id, result["manifest_path"], meeting_id)
                published_signature = importer.signature_for_meeting(meeting_id)
                if published_signature:
                    db.execute(
                        "UPDATE meetings SET source_signature=?, updated_at=? WHERE id=?",
                        (published_signature, utc_now(), meeting_id),
                    )
            except RelayUnavailable as error:
                db.execute(
                    "UPDATE meetings SET status='draft_modified', updated_at=? WHERE id=?",
                    (utc_now(), meeting_id),
                )
                db.add_event(
                    "relay_publish_ack_failed",
                    meeting_id=meeting_id,
                    job_id=job_id,
                    actor="system",
                    payload={"error": str(error)},
                )
                raise HTTPException(
                    503,
                    "文件已安全生成，但任务台账发布回执失败；已标记为待恢复，未显示成功",
                ) from error
        return result

    async def resolve_external_conflict(
        meeting_id: str,
        conflict_id: str,
        action: Literal["keep_draft", "accept_external", "discard_draft"],
    ) -> dict[str, Any]:
        meeting = db.query_one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
        if not meeting:
            raise HTTPException(404, "会议不存在")
        store = ConflictStore(db)
        conflict = next(
            (item for item in store.list(meeting_id) if item["id"] == conflict_id),
            None,
        )
        if not conflict:
            raise HTTPException(409, "指定冲突已解决或不存在")
        if conflict["kind"] != "external_source_change":
            raise HTTPException(409, "该冲突只能由对应的复验或恢复流程关闭")
        payload = conflict.get("payload") if isinstance(conflict.get("payload"), dict) else {}
        changed_before_resolution = payload.get("changed_resources")
        if isinstance(changed_before_resolution, list) and "managed_sources" in (
            changed_before_resolution
        ):
            await run_scan()
            conflict = next(
                (item for item in store.list(meeting_id) if item["id"] == conflict_id),
                None,
            )
            if not conflict:
                raise HTTPException(409, "重新扫描后冲突状态已变化，请刷新页面")
            payload = conflict.get("payload") if isinstance(conflict.get("payload"), dict) else {}
        if action == "keep_draft":
            signature = importer.signature_for_meeting(meeting_id)
            if not signature:
                raise HTTPException(409, "外部来源已不可访问，暂不能确认保留草稿")
            expected_signature = conflict.get("source_signature") or payload.get("source_signature")
            if expected_signature and signature != expected_signature:
                await run_scan()
                raise HTTPException(409, "外部来源再次发生变化，请重新检查冲突")
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE meetings SET source_signature=?, updated_at=? WHERE id=?",
                    (signature, utc_now(), meeting_id),
                )
                store.resolve(conflict_id, action, connection=connection)
        else:
            changed = payload.get("changed_resources")
            changed_resources = set(changed) if isinstance(changed, list) else {"transcript"}
            replacement_kind = (
                "discarded_draft" if action == "discard_draft" else "superseded_draft"
            )
            with db.transaction() as connection:
                pointer_updates: dict[str, str | None] = {}
                if "transcript" in changed_resources:
                    current = connection.execute(
                        "SELECT * FROM transcript_versions WHERE id=?",
                        (meeting.get("current_transcript_version_id"),),
                    ).fetchone()
                    target_id = payload.get("external_transcript_version_id")
                    target = (
                        connection.execute(
                            "SELECT id FROM transcript_versions WHERE id=? AND meeting_id=?",
                            (target_id, meeting_id),
                        ).fetchone()
                        if isinstance(target_id, str)
                        else None
                    )
                    if target is None and current is not None:
                        target_id = current["based_on_id"]
                    if current is not None and current["kind"] == "draft":
                        connection.execute(
                            "UPDATE transcript_versions SET kind=? WHERE id=?",
                            (replacement_kind, current["id"]),
                        )
                    pointer_updates["current_transcript_version_id"] = target_id
                if "minutes" in changed_resources:
                    current_minutes = connection.execute(
                        "SELECT * FROM minutes_versions WHERE id=?",
                        (meeting.get("current_minutes_version_id"),),
                    ).fetchone()
                    target_minutes_id = payload.get("external_minutes_version_id")
                    target_minutes = (
                        connection.execute(
                            "SELECT id FROM minutes_versions WHERE id=? AND meeting_id=?",
                            (target_minutes_id, meeting_id),
                        ).fetchone()
                        if isinstance(target_minutes_id, str)
                        else None
                    )
                    if target_minutes is None and current_minutes is not None:
                        target_minutes_id = current_minutes["based_on_id"]
                    if current_minutes is not None and current_minutes["kind"] == "draft":
                        connection.execute(
                            "UPDATE minutes_versions SET kind=? WHERE id=?",
                            (replacement_kind, current_minutes["id"]),
                        )
                    pointer_updates["current_minutes_version_id"] = target_minutes_id
                assignments = [f"{name}=?" for name in pointer_updates]
                values = list(pointer_updates.values())
                assignments.extend(("source_signature=?", "updated_at=?"))
                values.extend(
                    (
                        conflict.get("source_signature") or payload.get("source_signature"),
                        utc_now(),
                        meeting_id,
                    )
                )
                connection.execute(
                    f"UPDATE meetings SET {', '.join(assignments)} WHERE id=?",
                    values,
                )
                if "current_transcript_version_id" in pointer_updates:
                    db.sync_transcript_metadata_with_connection(
                        connection,
                        meeting_id,
                        pointer_updates["current_transcript_version_id"],
                    )
                store.resolve(conflict_id, action, connection=connection)
        db.add_event(
            "external_conflict_resolved",
            meeting_id=meeting_id,
            actor="user",
            payload={"action": action, "conflict_id": conflict_id},
        )
        detail = _meeting_detail(db, meeting_id)
        assert detail is not None
        return detail

    @app.post("/api/meetings/{meeting_id}/conflicts/{conflict_id}/resolve")
    async def resolve_conflict_by_id(
        meeting_id: str, conflict_id: str, body: ConflictResolutionInput
    ):
        return await resolve_external_conflict(meeting_id, conflict_id, body.action)

    @app.post("/api/meetings/{meeting_id}/conflict/resolve")
    async def resolve_conflict(meeting_id: str, body: ConflictResolutionInput):
        conflicts = ConflictStore(db).list(meeting_id, kind="external_source_change")
        if len(conflicts) != 1:
            raise HTTPException(409, "当前会议没有唯一的外部来源冲突")
        return await resolve_external_conflict(meeting_id, conflicts[0]["id"], body.action)

    @app.patch("/api/meetings/{meeting_id}")
    def update_meeting(meeting_id: str, body: MeetingMetadataInput):
        if not db.query_one("SELECT id FROM meetings WHERE id = ?", (meeting_id,)):
            raise HTTPException(404, "会议不存在")
        changed_fields = []
        event_payload: dict[str, Any] = {}
        with db.transaction() as connection:
            if body.title is not None:
                normalized_title = body.title.strip()
                connection.execute(
                    "UPDATE meetings SET title=?, updated_at=? WHERE id=?",
                    (normalized_title, utc_now(), meeting_id),
                )
                changed_fields.append("title")
                event_payload["title"] = normalized_title
            if body.project_id is not None:
                connection.execute(
                    "UPDATE meetings SET project_id=?, updated_at=? WHERE id=?",
                    (body.project_id or None, utc_now(), meeting_id),
                )
                changed_fields.append("project_id")
            if body.tag_ids is not None:
                connection.execute("DELETE FROM meeting_tags WHERE meeting_id=?", (meeting_id,))
                for tag_id in body.tag_ids:
                    connection.execute(
                        "INSERT INTO meeting_tags(meeting_id, tag_id) VALUES (?, ?)",
                        (meeting_id, tag_id),
                    )
                changed_fields.append("tag_ids")
        event_payload["fields"] = changed_fields
        db.add_event(
            "meeting_metadata_updated",
            meeting_id=meeting_id,
            actor="user",
            payload=event_payload,
        )
        return _meeting_detail(db, meeting_id)

    @app.get("/api/projects")
    def projects():
        return db.query_all(
            """SELECT p.*, COUNT(m.id) AS meeting_count
                 FROM projects p LEFT JOIN meetings m ON m.project_id=p.id
                GROUP BY p.id ORDER BY p.name"""
        )

    @app.post("/api/projects")
    def create_project(body: ProjectInput):
        project_id = f"project-{secrets.token_hex(8)}"
        db.execute(
            "INSERT INTO projects(id, name, color, created_at) VALUES (?, ?, ?, ?)",
            (project_id, body.name.strip(), body.color, utc_now()),
        )
        db.add_event("project_created", actor="user", payload={"project_id": project_id})
        project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        assert project is not None
        project["meeting_count"] = 0
        return project

    @app.get("/api/tags")
    def tags():
        return db.query_all("SELECT * FROM tags ORDER BY name")

    @app.post("/api/tags")
    def create_tag(body: TagInput):
        tag_id = f"tag-{secrets.token_hex(8)}"
        db.execute(
            "INSERT INTO tags(id, name, color, created_at) VALUES (?, ?, ?, ?)",
            (tag_id, body.name.strip(), body.color, utc_now()),
        )
        db.add_event("tag_created", actor="user", payload={"tag_id": tag_id})
        return db.query_one("SELECT * FROM tags WHERE id = ?", (tag_id,))

    @app.post("/api/uploads/start")
    def start_upload(body: UploadStartInput):
        try:
            session = uploads.start(body.filename, body.size_bytes, hotwords=body.hotwords)
        except UploadError as error:
            raise HTTPException(400, str(error)) from error
        db.add_event(
            "audio_upload_started",
            actor="user",
            payload={
                "upload_id": session.upload_id,
                "size_bytes": body.size_bytes,
                "hotwords": hotword_audit(body.hotwords),
            },
        )
        return asdict(session)

    @app.put("/api/uploads/{upload_id}/chunks/{index}")
    def upload_chunk(upload_id: str, index: int, body: UploadChunkInput):
        try:
            result = uploads.write_chunk(upload_id, index, body.content_base64)
        except UploadError as error:
            raise HTTPException(409, str(error)) from error
        db.add_event(
            "audio_upload_chunk_stored",
            actor="user",
            payload={"upload_id": upload_id, "index": index, "bytes": result["bytes"]},
        )
        return result

    @app.post("/api/uploads/{upload_id}/complete")
    def complete_upload(upload_id: str, _body: dict[str, Any]):
        try:
            destination = uploads.complete(upload_id)
        except UploadError as error:
            raise HTTPException(409, str(error)) from error
        owner_id = f"upload-enqueue-{secrets.token_hex(16)}"
        try:
            receipt = uploads.claim_enqueue(upload_id, owner_id)
        except UploadError as error:
            raise HTTPException(409, str(error)) from error
        if receipt is None:
            return JSONResponse(
                {
                    "upload_id": upload_id,
                    "path": str(destination),
                    "size_bytes": destination.stat().st_size,
                    "status": "enqueueing",
                    "job_id": None,
                    "detail": "录音正在入队，无需重复提交",
                },
                status_code=202,
            )
        if receipt.get("status") == "queued":
            return {
                "path": str(destination),
                "size_bytes": destination.stat().st_size,
                "status": "queued",
                "job_id": receipt.get("job_id"),
            }
        hotwords = list(receipt.get("hotwords") or [])
        try:
            job_id = relay.enqueue(destination, hotwords=hotwords)
        except RelayUnavailable as error:
            try:
                uploads.release_enqueue(upload_id, owner_id)
            except UploadError:
                pass
            db.add_event(
                "audio_upload_enqueue_failed",
                actor="user",
                payload={
                    "upload_id": upload_id,
                    "error_type": type(error).__name__,
                    "hotwords": hotword_audit(hotwords),
                },
            )
            return JSONResponse(
                {
                    "upload_id": upload_id,
                    "path": str(destination),
                    "size_bytes": destination.stat().st_size,
                    "status": "saved_pending_enqueue",
                    "job_id": None,
                    "detail": "音频已安全保存在本机，将由后台自动重试入队",
                },
                status_code=202,
            )
        uploads.mark_enqueued(upload_id, job_id, owner_id=owner_id)
        db.add_event(
            "audio_uploaded",
            job_id=job_id,
            actor="user",
            payload={
                "upload_id": upload_id,
                "size_bytes": destination.stat().st_size,
                "hotwords": hotword_audit(hotwords),
            },
        )
        return {
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
            "status": "queued",
            "job_id": job_id,
        }

    @app.post("/api/uploads/{upload_id}/cancel")
    def cancel_upload(upload_id: str, _body: dict[str, Any]):
        try:
            uploads.cancel(upload_id)
        except UploadError as error:
            raise HTTPException(409, str(error)) from error
        db.add_event(
            "audio_upload_cancelled",
            actor="user",
            payload={"upload_id": upload_id},
        )
        return {"ok": True, "upload_id": upload_id}

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app
