from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, utc_now
from .parsers import parse_srt


class QwenShadowError(RuntimeError):
    pass


class AudioIntegrityChanged(QwenShadowError):
    pass


class LeaseLost(QwenShadowError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class QwenShadowService:
    def __init__(self, db: Database, settings: Settings, relay: Any):
        self.db = db
        self.settings = settings
        self.relay = relay
        self.root = settings.data_dir / "qwen-shadow"
        self._flight = threading.Lock()
        self.owner_id = f"qwen-owner-{os.getpid()}-{uuid.uuid4().hex}"

    def _lease_values(self) -> tuple[str, str]:
        now = datetime.now(UTC)
        return now.isoformat(), (
            now + timedelta(seconds=self.settings.qwen_lease_seconds)
        ).isoformat()

    def _binary_available(self) -> bool:
        binary = self.settings.qwen_binary
        return binary.is_file() and not binary.is_symlink()

    def _audio(self, meeting_id: str) -> Path:
        artifact = self.db.query_one(
            """SELECT path FROM artifacts WHERE meeting_id=? AND kind='audio'
               ORDER BY CASE source_root
                 WHEN 'archive' THEN 0 WHEN 'draft' THEN 1 WHEN 'staging' THEN 2 ELSE 3 END,
                 id LIMIT 1""",
            (meeting_id,),
        )
        if not artifact:
            raise QwenShadowError("audio_missing")
        path = Path(artifact["path"])
        if path.is_symlink() or not path.is_file():
            raise QwenShadowError("audio_unsafe")
        resolved = path.resolve()
        allowed = (
            self.settings.archive_root.resolve(),
            self.settings.staging_root.resolve(),
            self.settings.data_dir.resolve(),
        )
        if not any(resolved.is_relative_to(root) for root in allowed):
            raise QwenShadowError("audio_outside_allowed_roots")
        return path

    def request(self, meeting_id: str, *, retry_of: str | None = None) -> dict[str, Any]:
        if not self.db.query_one("SELECT id FROM meetings WHERE id=?", (meeting_id,)):
            raise QwenShadowError("meeting_missing")
        audio = self._audio(meeting_id)
        audio_sha256 = _sha256(audio)
        run_id = f"shadow-{uuid.uuid4().hex}"
        now = utc_now()
        available = self._binary_available()
        state = "queued" if available else "unavailable"
        error = None if available else "Qwen 离线执行程序不可用"
        config = {"retry_of": retry_of} if retry_of else {}
        with self.db.transaction() as connection:
            active = connection.execute(
                """SELECT * FROM asr_shadow_runs
                    WHERE meeting_id=? AND state IN ('queued', 'running')
                    ORDER BY created_at, id LIMIT 1""",
                (meeting_id,),
            ).fetchone()
            if active is not None:
                return dict(active)
            connection.execute(
                """INSERT INTO asr_shadow_runs
                   (id, meeting_id, engine, model, state, audio_sha256, config_json,
                    error, created_at, updated_at, finished_at)
                   VALUES (?, ?, 'qwen3_asr', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    meeting_id,
                    self.settings.qwen_model,
                    state,
                    audio_sha256,
                    json.dumps(config, separators=(",", ":")),
                    error,
                    now,
                    now,
                    now if state == "unavailable" else None,
                ),
            )
            self.db.add_event(
                "qwen_shadow_requested",
                meeting_id=meeting_id,
                actor="user",
                payload={"run_id": run_id, "state": state, "retry": bool(retry_of)},
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM asr_shadow_runs WHERE id=?", (run_id,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def retry(self, meeting_id: str, run_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT id FROM asr_shadow_runs WHERE id=? AND meeting_id=?", (run_id, meeting_id)
        )
        if not row:
            raise QwenShadowError("run_missing")
        return self.request(meeting_id, retry_of=run_id)

    def recover_orphaned(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as connection:
            orphaned = connection.execute(
                """SELECT id, owner_id FROM asr_shadow_runs
                    WHERE state='running'
                      AND (owner_id IS NULL OR heartbeat_at IS NULL
                           OR lease_expires_at IS NULL OR lease_expires_at < ?)""",
                (now,),
            ).fetchall()
            cursor = connection.execute(
                """UPDATE asr_shadow_runs
                      SET state='queued', error='租约过期后重新排队', owner_id=NULL,
                          lease_expires_at=NULL, heartbeat_at=NULL,
                          updated_at=?, finished_at=NULL
                    WHERE state='running'
                      AND (owner_id IS NULL OR heartbeat_at IS NULL
                           OR lease_expires_at IS NULL OR lease_expires_at < ?)""",
                (utc_now(), now),
            )
            for row in orphaned:
                connection.execute(
                    "DELETE FROM runtime_leases WHERE name='qwen_shadow' AND run_id=?",
                    (row["id"],),
                )
            connection.execute(
                """DELETE FROM runtime_leases
                    WHERE name='qwen_shadow' AND lease_expires_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM asr_shadow_runs r
                           WHERE r.id=runtime_leases.run_id AND r.state='running'
                             AND r.owner_id=runtime_leases.owner_id
                             AND r.lease_expires_at >= ?
                      )""",
                (now, now),
            )
            return cursor.rowcount

    def _relay_is_transcribing(self) -> bool:
        try:
            return bool(self.relay.list_jobs(status="transcribing", limit=1))
        except Exception:
            return True

    def _claim_next(self) -> dict[str, Any] | None:
        heartbeat_at, lease_expires_at = self._lease_values()
        with self.db.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM runtime_leases WHERE name='qwen_shadow'"
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM asr_shadow_runs WHERE state='running' LIMIT 1"
                ).fetchone()
            ):
                return None
            candidate = connection.execute(
                """SELECT id FROM asr_shadow_runs WHERE state='queued'
                   ORDER BY created_at, id LIMIT 1"""
            ).fetchone()
            if not candidate:
                return None
            connection.execute(
                """INSERT INTO runtime_leases
                   (name, owner_id, run_id, heartbeat_at, lease_expires_at)
                   VALUES ('qwen_shadow', ?, ?, ?, ?)""",
                (self.owner_id, candidate["id"], heartbeat_at, lease_expires_at),
            )
            cursor = connection.execute(
                """UPDATE asr_shadow_runs
                      SET state='running', owner_id=?, heartbeat_at=?, lease_expires_at=?,
                          error=NULL, updated_at=?
                    WHERE id=? AND state='queued'""",
                (
                    self.owner_id,
                    heartbeat_at,
                    lease_expires_at,
                    heartbeat_at,
                    candidate["id"],
                ),
            )
            if cursor.rowcount != 1:
                connection.execute(
                    "DELETE FROM runtime_leases WHERE name='qwen_shadow' AND owner_id=?",
                    (self.owner_id,),
                )
                return None
            row = connection.execute(
                "SELECT * FROM asr_shadow_runs WHERE id=?", (candidate["id"],)
            ).fetchone()
            return dict(row)

    def _heartbeat(self, run_id: str) -> bool:
        heartbeat_at, lease_expires_at = self._lease_values()
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE asr_shadow_runs
                      SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                    WHERE id=? AND state='running' AND owner_id=?""",
                (heartbeat_at, lease_expires_at, heartbeat_at, run_id, self.owner_id),
            )
            worker = connection.execute(
                """UPDATE runtime_leases
                      SET heartbeat_at=?, lease_expires_at=?
                    WHERE name='qwen_shadow' AND run_id=? AND owner_id=?""",
                (heartbeat_at, lease_expires_at, run_id, self.owner_id),
            )
            if cursor.rowcount != 1 or worker.rowcount != 1:
                raise LeaseLost("lease_lost")
            return True

    def _heartbeat_loop(
        self, run_id: str, stop: threading.Event, lease_lost: threading.Event
    ) -> None:
        while not stop.wait(self.settings.qwen_heartbeat_seconds):
            try:
                if not self._heartbeat(run_id):
                    lease_lost.set()
                    return
            except Exception:
                lease_lost.set()
                return

    def _mark_failed(self, run_id: str, state: str, error: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE asr_shadow_runs
                      SET state=?, error=?, owner_id=NULL, lease_expires_at=NULL,
                          heartbeat_at=NULL, updated_at=?, finished_at=?
                    WHERE id=? AND state='running' AND owner_id=?""",
                (state, error, utc_now(), utc_now(), run_id, self.owner_id),
            )
            connection.execute(
                """DELETE FROM runtime_leases
                    WHERE name='qwen_shadow' AND run_id=? AND owner_id=?""",
                (run_id, self.owner_id),
            )

    def run_once(self) -> bool:
        if not self._flight.acquire(blocking=False):
            return False
        try:
            self.recover_orphaned()
            if self._relay_is_transcribing():
                return False
            run = self._claim_next()
            if not run:
                return False
            run_id = run["id"]
            if not self._binary_available():
                self._mark_failed(run_id, "unavailable", "Qwen 离线执行程序不可用")
                return True
            run_root = self.root / f"{run_id}-{self.owner_id}"
            try:
                if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
                    raise QwenShadowError("output_root_unsafe")
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if run_root.exists():
                    if run_root.is_symlink() or not run_root.is_dir():
                        raise QwenShadowError("output_run_unsafe")
                    shutil.rmtree(run_root)
                run_root.mkdir(mode=0o700)
                if not run_root.resolve().is_relative_to(self.root.resolve()):
                    raise QwenShadowError("output_outside_root")
                audio = self._audio(run["meeting_id"])
                before = _sha256(audio)
                if before != run["audio_sha256"]:
                    raise AudioIntegrityChanged("audio_changed_before_run")
                environment = os.environ.copy()
                environment.update(
                    {
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                        "HF_HUB_DISABLE_TELEMETRY": "1",
                        "DO_NOT_TRACK": "1",
                    }
                )
                command = [
                    str(self.settings.qwen_binary),
                    str(audio),
                    "--model",
                    self.settings.qwen_model,
                    "--timestamps",
                    "-f",
                    "all",
                    "--quiet",
                    "-o",
                    str(run_root),
                ]
                heartbeat_stop = threading.Event()
                lease_lost = threading.Event()
                heartbeat = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(run_id, heartbeat_stop, lease_lost),
                    name=f"qwen-heartbeat-{run_id}",
                    daemon=True,
                )
                heartbeat.start()
                try:
                    try:
                        result = subprocess.run(
                            command,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=self.settings.qwen_timeout_seconds,
                        )
                    finally:
                        after = _sha256(audio)
                        if after != before:
                            raise AudioIntegrityChanged("audio_changed_during_run")
                finally:
                    heartbeat_stop.set()
                    heartbeat.join(timeout=max(1.0, self.settings.qwen_heartbeat_seconds * 2))
                if lease_lost.is_set():
                    raise LeaseLost("lease_lost")
                if result.returncode != 0:
                    raise QwenShadowError("process_failed")
                candidates = list(run_root.glob("*.srt"))
                if (
                    len(candidates) != 1
                    or candidates[0].is_symlink()
                    or not candidates[0].is_file()
                    or not candidates[0].resolve().is_relative_to(run_root.resolve())
                    or candidates[0].stat().st_size <= 0
                    or candidates[0].stat().st_size > 64 * 1024 * 1024
                ):
                    raise QwenShadowError("srt_output_invalid")
                source_srt = candidates[0]
                source_srt.read_text(encoding="utf-8")
                segments = parse_srt(source_srt)
                if not segments or any(
                    int(segment["end_ms"]) <= int(segment["start_ms"]) for segment in segments
                ):
                    raise QwenShadowError("srt_output_empty")
                source_sha256 = _sha256(source_srt)
                metrics = {
                    "segment_count": len(segments),
                    "duration_ms": max(int(segment["end_ms"]) for segment in segments),
                }
                with self.db.transaction() as connection:
                    ownership = connection.execute(
                        """SELECT state, owner_id, lease_expires_at FROM asr_shadow_runs
                           WHERE id=?""",
                        (run_id,),
                    ).fetchone()
                    worker_lease = connection.execute(
                        """SELECT owner_id, run_id, lease_expires_at FROM runtime_leases
                            WHERE name='qwen_shadow'"""
                    ).fetchone()
                    if (
                        not ownership
                        or ownership["state"] != "running"
                        or ownership["owner_id"] != self.owner_id
                        or not ownership["lease_expires_at"]
                        or ownership["lease_expires_at"] < datetime.now(UTC).isoformat()
                        or not worker_lease
                        or worker_lease["owner_id"] != self.owner_id
                        or worker_lease["run_id"] != run_id
                        or worker_lease["lease_expires_at"] < datetime.now(UTC).isoformat()
                    ):
                        raise LeaseLost("lease_lost")
                    version_id = self.db.create_transcript_version_with_connection(
                        connection,
                        run["meeting_id"],
                        "qwen_reference",
                        source_sha256=source_sha256,
                        make_current=False,
                    )
                    self.db.replace_segments_with_connection(
                        connection, version_id, run["meeting_id"], segments
                    )
                    connection.execute(
                        """UPDATE asr_shadow_runs
                              SET state='ready', transcript_version_id=?, metrics_json=?,
                                  error=NULL, owner_id=NULL, lease_expires_at=NULL,
                                  heartbeat_at=NULL, updated_at=?, finished_at=?
                            WHERE id=? AND state='running' AND owner_id=?""",
                        (
                            version_id,
                            json.dumps(metrics, separators=(",", ":")),
                            utc_now(),
                            utc_now(),
                            run_id,
                            self.owner_id,
                        ),
                    )
                    self.db.add_event(
                        "qwen_shadow_ready",
                        meeting_id=run["meeting_id"],
                        actor="system",
                        payload={"run_id": run_id, **metrics},
                        connection=connection,
                    )
                    connection.execute(
                        """DELETE FROM runtime_leases
                            WHERE name='qwen_shadow' AND run_id=? AND owner_id=?""",
                        (run_id, self.owner_id),
                    )
            except AudioIntegrityChanged:
                self._mark_failed(run_id, "failed", "原音频完整性校验失败")
            except LeaseLost:
                pass
            except subprocess.TimeoutExpired:
                self._mark_failed(run_id, "failed", "Qwen 离线转写超时")
            except (OSError, sqlite3.Error, UnicodeError, ValueError, QwenShadowError):
                self._mark_failed(run_id, "failed", "Qwen 离线转写失败")
            finally:
                if run_root.exists() and not run_root.is_symlink():
                    shutil.rmtree(run_root, ignore_errors=True)
            return True
        finally:
            self._flight.release()
