import importlib.util
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = REPO_ROOT / "quickstart" / "relay_control.py"
_RUNTIME_DB_ENV = "MEETING_RELAY_JOBS_DB"
_original_runtime_db = None
_runtime_db_tempdir = None


def setUpModule():
    global _original_runtime_db, _runtime_db_tempdir
    _original_runtime_db = os.environ.get(_RUNTIME_DB_ENV)
    _runtime_db_tempdir = tempfile.TemporaryDirectory()
    os.environ[_RUNTIME_DB_ENV] = str(Path(_runtime_db_tempdir.name) / "default-jobs.sqlite3")


def tearDownModule():
    if _original_runtime_db is None:
        os.environ.pop(_RUNTIME_DB_ENV, None)
    else:
        os.environ[_RUNTIME_DB_ENV] = _original_runtime_db
    if _runtime_db_tempdir is not None:
        _runtime_db_tempdir.cleanup()


def load_control_module():
    spec = importlib.util.spec_from_file_location("relay_control_under_test", CONTROL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_complete_archive(
    root: Path,
    job_id: str,
    attempt: int = 1,
    *,
    include_whisper: bool = True,
    stem: str = "vm-20260710-120000-ABC",
    audio_suffix: str = ".m4a",
    whisper_stem: str | None = None,
    include_minutes_evidence: bool = True,
    minutes_protocol_version: int = 3,
) -> Path:
    archive = root / ".workbench-drafts" / job_id / f"attempt-{attempt}"
    archive.mkdir(parents=True, exist_ok=True)
    files = {
        f"{stem}{audio_suffix}": b"audio",
        f"{stem}.txt": "逐字稿",
        f"{stem}.srt": "1\n00:00:01,000 --> 00:00:02,000\n测试",
        f"{stem}.spk.txt": "speaker 0: 张三",
        f"{stem}.funasr.json": "{}",
        f"{stem}.funasr.log": "ok",
        "测试会议.md": "# 会议纪要\n\n## 一分钟摘要\n测试结论 [00:00:01]",
        "测试会议.html": "<h1>会议纪要</h1>",
    }
    for relative, content in files.items():
        target = archive / relative
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    if include_minutes_evidence:
        (archive / "minutes-evidence.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "minutes_protocol_version": minutes_protocol_version,
                    "strategy": "topic_hierarchical",
                    "topics": [
                        {
                            "topic_id": "T01",
                            "title": "测试议题",
                            "start_sec": 0,
                            "end_sec": 2 if minutes_protocol_version >= 3 else 60,
                            "items": [
                                {
                                    "item_id": "D01",
                                    "kind": "decision",
                                    "text": "测试结论",
                                    "source_start_sec": 1,
                                    **(
                                        {
                                            "source_end_sec": 2,
                                            "source_text_sha256": hashlib.sha256(
                                                "测试".encode("utf-8")
                                            ).hexdigest(),
                                            "source_window_id": "W001",
                                        }
                                        if minutes_protocol_version >= 3
                                        else {}
                                    ),
                                    "minutes_anchor": "[00:00:01]",
                                    "status": "included",
                                }
                            ],
                        }
                    ],
                    "coverage": {
                        "total_items": 1,
                        "included_items": 1,
                        "omitted_items": 0,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if minutes_protocol_version >= 3:
        source_srt = archive / f"{stem}.srt"
        (archive / "minutes-plan.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "minutes_protocol_version": 3,
                    "source_srt_sha256": hashlib.sha256(
                        source_srt.read_bytes()
                    ).hexdigest(),
                    "input_transcript_sha256": None,
                    "total_duration_sec": 2,
                    "cue_count": 1,
                    "windows": [
                        {
                            "window_id": "W001",
                            "start_sec": 0,
                            "end_sec": 2,
                            "cues": [
                                {
                                    "cue_index": 1,
                                    "source_start_sec": 1,
                                    "source_end_sec": 2,
                                    "source_text_sha256": hashlib.sha256(
                                        "测试".encode("utf-8")
                                    ).hexdigest(),
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if include_whisper:
        reference_stem = whisper_stem or stem
        whisper = archive / "whisper-ref"
        whisper.mkdir()
        for suffix in ("json", "tsv", "srt", "txt", "vtt"):
            (whisper / f"{reference_stem}.{suffix}").write_text(
                "{}" if suffix == "json" else "whisper", encoding="utf-8"
            )
        (whisper / "whisper.log").write_text("ok", encoding="utf-8")

    artifact_paths = sorted(
        str(path.relative_to(archive))
        for path in archive.rglob("*")
        if path.is_file()
    )
    manifest = {
        "schema_version": 1,
        "minutes_protocol_version": minutes_protocol_version,
        "job_id": job_id,
        "attempt": attempt,
        "artifacts": [
                    {
                        "path": path,
                        "bytes": (archive / path).stat().st_size,
                        "sha256": hashlib.sha256((archive / path).read_bytes()).hexdigest(),
                    }
                    for path in artifact_paths
                ],
    }
    input_snapshot = archive / "input-transcript.txt"
    if input_snapshot.is_file():
        manifest["requested_stage"] = "minutes_generating"
        manifest["input_transcript_sha256"] = hashlib.sha256(
            input_snapshot.read_bytes()
        ).hexdigest()
    (archive / "workbench-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    if minutes_protocol_version >= 3:
        source_digest = hashlib.sha256(
            (archive / f"{stem}.srt").read_bytes()
        ).hexdigest()
        plan_digest = hashlib.sha256(
            (archive / "minutes-plan.json").read_bytes()
        ).hexdigest()
        for database_path in root.glob("*.sqlite3"):
            try:
                with sqlite3.connect(database_path) as connection:
                    connection.execute(
                        """
                        UPDATE attempts
                        SET source_srt_sha256 = ?, minutes_plan_sha256 = ?
                        WHERE job_id = ? AND attempt_no = ?
                        """,
                        (source_digest, plan_digest, job_id, attempt),
                    )
            except sqlite3.Error:
                continue
    return archive


def create_published_archive(
    root: Path,
    draft: Path,
    job_id: str,
    meeting_id: str = "vm-20260710-120000-ABC",
    directory_name: str = "260710 工作台发布测试",
) -> Path:
    published = root / directory_name
    shutil.copytree(draft, published)
    manifest_path = published / "workbench-manifest.json"
    draft_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = draft_manifest["attempt"]
    audio = next(
        path
        for path in published.iterdir()
        if path.suffix.lower() in {".m4a", ".mp3", ".wav"}
    )
    artifact_paths = sorted(
        str(path.relative_to(published))
        for path in published.rglob("*")
        if path.is_file()
        and path.name != "workbench-manifest.json"
        and ".workbench-history" not in path.parts
        and not path.name.startswith("._")
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": draft_manifest.get(
                    "minutes_protocol_version", 1
                ),
                "job_id": job_id,
                "attempt": attempt,
                "meeting_id": meeting_id,
                "status": "published",
                "original_audio": {
                    "path": audio.name,
                    "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                },
                "artifacts": [
                    {
                        "path": path,
                        "bytes": (published / path).stat().st_size,
                        "sha256": hashlib.sha256(
                            (published / path).read_bytes()
                        ).hexdigest(),
                    }
                    for path in artifact_paths
                ],
                **(
                    {
                        "requested_stage": draft_manifest["requested_stage"],
                        "input_transcript_sha256": draft_manifest[
                            "input_transcript_sha256"
                        ],
                    }
                    if draft_manifest.get("requested_stage") == "minutes_generating"
                    else {}
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest_path


def rewrite_archive_as_published(
    archive: Path,
    job_id: str,
    meeting_id: str = "vm-20260710-120000-ABC",
) -> Path:
    manifest_path = archive / "workbench-manifest.json"
    draft_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    attempt = draft_manifest["attempt"]
    audio = next(
        path
        for path in archive.iterdir()
        if path.suffix.lower() in {".m4a", ".mp3", ".wav"}
    )
    artifact_paths = sorted(
        str(path.relative_to(archive))
        for path in archive.rglob("*")
        if path.is_file()
        and path.name != "workbench-manifest.json"
        and ".workbench-history" not in path.parts
        and not path.name.startswith("._")
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": draft_manifest.get(
                    "minutes_protocol_version", 1
                ),
                "job_id": job_id,
                "attempt": attempt,
                "meeting_id": meeting_id,
                "status": "published",
                "original_audio": {
                    "path": audio.name,
                    "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                },
                "artifacts": [
                    {
                        "path": relative,
                        "bytes": (archive / relative).stat().st_size,
                        "sha256": hashlib.sha256(
                            (archive / relative).read_bytes()
                        ).hexdigest(),
                    }
                    for relative in artifact_paths
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest_path


def create_minutes_only_archive(
    root: Path,
    job_id: str,
    attempt: int,
    *,
    audio_name: str = "vm-20260710-120000-ABC.m4a",
) -> Path:
    archive = root / ".workbench-drafts" / job_id / f"attempt-{attempt}"
    input_snapshot = archive / "input-transcript.srt"
    assert input_snapshot.is_file()
    (archive / audio_name).write_bytes(b"audio")
    (archive / "测试会议.md").write_text(
        "# 新纪要\n\n## 一分钟摘要\n测试结论 [00:00:01]", encoding="utf-8"
    )
    (archive / "测试会议.html").write_text("<h1>新纪要</h1>", encoding="utf-8")
    (archive / "minutes-evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "strategy": "single_pass",
                "topics": [
                    {
                        "topic_id": "T01",
                        "title": "测试议题",
                        "start_sec": 0,
                        "end_sec": 2,
                        "items": [
                            {
                                "item_id": "D01",
                                "kind": "decision",
                                "text": "测试结论",
                                "source_start_sec": 1,
                                "source_end_sec": 2,
                                "source_text_sha256": hashlib.sha256(
                                    "当前工作稿".encode("utf-8")
                                ).hexdigest(),
                                "source_window_id": "W001",
                                "minutes_anchor": "[00:00:01]",
                                "status": "included",
                            }
                        ],
                    }
                ],
                "coverage": {
                    "total_items": 1,
                    "included_items": 1,
                    "omitted_items": 0,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (archive / "minutes-plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "source_srt_sha256": hashlib.sha256(
                    input_snapshot.read_bytes()
                ).hexdigest(),
                "input_transcript_sha256": hashlib.sha256(
                    input_snapshot.read_bytes()
                ).hexdigest(),
                "total_duration_sec": 2,
                "cue_count": 1,
                "windows": [
                    {
                        "window_id": "W001",
                        "start_sec": 0,
                        "end_sec": 2,
                        "cues": [
                            {
                                "cue_index": 1,
                                "source_start_sec": 1,
                                "source_end_sec": 2,
                                "source_text_sha256": hashlib.sha256(
                                    "当前工作稿".encode("utf-8")
                                ).hexdigest(),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_paths = sorted(
        str(path.relative_to(archive))
        for path in archive.rglob("*")
        if path.is_file() and path.name != "workbench-manifest.json"
    )
    manifest = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "job_id": job_id,
        "attempt": attempt,
        "requested_stage": "minutes_generating",
        "input_transcript_sha256": hashlib.sha256(
            input_snapshot.read_bytes()
        ).hexdigest(),
        "artifacts": [
            {
                "path": path,
                "bytes": (archive / path).stat().st_size,
                "sha256": hashlib.sha256((archive / path).read_bytes()).hexdigest(),
            }
            for path in artifact_paths
        ],
    }
    (archive / "workbench-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    plan_digest = hashlib.sha256(
        (archive / "minutes-plan.json").read_bytes()
    ).hexdigest()
    for database_path in root.glob("*.sqlite3"):
        try:
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE attempts SET minutes_plan_sha256 = ?
                    WHERE job_id = ? AND attempt_no = ?
                    """,
                    (plan_digest, job_id, attempt),
                )
        except sqlite3.Error:
            continue
    return archive


def create_minutes_only_published_archive(
    root: Path,
    job_id: str,
    input_transcript_sha256: str,
    *,
    attempt: int = 1,
    meeting_id: str = "legacy-meeting",
    directory_name: str = "260710 历史会议修订",
) -> Path:
    published = root / directory_name
    published.mkdir()
    input_source = (
        root / ".workbench-drafts" / job_id / f"attempt-{attempt}" / "input-transcript.srt"
    )
    if not input_source.is_file():
        input_source = root / "current-transcript.srt"
    input_text = input_source.read_text(encoding="utf-8")
    files = {
        "legacy-audio.m4a": b"audio",
        "input-transcript.srt": input_text,
        f"{meeting_id}.txt": "人工校订逐字稿",
        f"{meeting_id}.srt": "1\n00:00:00,000 --> 00:00:01,000\n人工校订逐字稿",
        "spk.txt": "spk0\t",
        "会议纪要.md": "# 历史会议新纪要\n\n测试结论 [00:00:01]",
        "会议纪要.html": "<h1>历史会议新纪要</h1>",
        "minutes-evidence.json": json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "strategy": "single_pass",
                "topics": [
                    {
                        "topic_id": "T01",
                        "title": "测试议题",
                        "start_sec": 0,
                        "end_sec": 2,
                        "items": [
                            {
                                "item_id": "D01",
                                "kind": "decision",
                                "text": "测试结论",
                                "source_start_sec": 1,
                                "source_end_sec": 2,
                                "source_text_sha256": hashlib.sha256(
                                    "当前工作稿".encode("utf-8")
                                ).hexdigest(),
                                "source_window_id": "W001",
                                "minutes_anchor": "[00:00:01]",
                                "status": "included",
                            }
                        ],
                    }
                ],
                "coverage": {
                    "total_items": 1,
                    "included_items": 1,
                    "omitted_items": 0,
                },
            },
            ensure_ascii=False,
        ),
    }
    for name, content in files.items():
        path = published / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    input_srt = published / "input-transcript.srt"
    (published / "minutes-plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "minutes_protocol_version": 3,
                "source_srt_sha256": hashlib.sha256(input_srt.read_bytes()).hexdigest(),
                "input_transcript_sha256": input_transcript_sha256,
                "total_duration_sec": 2,
                "cue_count": 1,
                "windows": [
                    {
                        "window_id": "W001",
                        "start_sec": 0,
                        "end_sec": 2,
                        "cues": [
                            {
                                "cue_index": 1,
                                "source_start_sec": 1,
                                "source_end_sec": 2,
                                "source_text_sha256": hashlib.sha256(
                                    "当前工作稿".encode("utf-8")
                                ).hexdigest(),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    artifact_paths = sorted(path.name for path in published.iterdir() if path.is_file())
    manifest = {
        "schema_version": 1,
        "minutes_protocol_version": 3,
        "job_id": job_id,
        "attempt": attempt,
        "meeting_id": meeting_id,
        "status": "published",
        "requested_stage": "minutes_generating",
        "input_transcript_sha256": input_transcript_sha256,
        "original_audio": {
            "path": "legacy-audio.m4a",
            "sha256": hashlib.sha256(b"audio").hexdigest(),
        },
        "artifacts": [
            {
                "path": name,
                "bytes": (published / name).stat().st_size,
                "sha256": hashlib.sha256((published / name).read_bytes()).hexdigest(),
            }
            for name in artifact_paths
        ],
    }
    manifest_path = published / "workbench-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def create_whisper_staging(root: Path, stem: str = "vm-20260710-120000-ABC") -> Path:
    staging = root / f".workbench-whisper-stage-{stem}"
    whisper = staging / "whisper-ref"
    whisper.mkdir(parents=True)
    for suffix in ("json", "tsv", "srt", "txt", "vtt"):
        (whisper / f"{stem}.{suffix}").write_text(
            "{}" if suffix == "json" else "whisper", encoding="utf-8"
        )
    (whisper / "whisper.log").write_text("ok", encoding="utf-8")
    return staging


def refresh_published_manifest(manifest_path: Path) -> None:
    published = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_paths = sorted(
        str(path.relative_to(published))
        for path in published.rglob("*")
        if path.is_file()
        and path.name != "workbench-manifest.json"
        and ".workbench-history" not in path.parts
        and not path.name.startswith("._")
    )
    manifest["artifacts"] = [
        {
            "path": path,
            "bytes": (published / path).stat().st_size,
            "sha256": hashlib.sha256((published / path).read_bytes()).hexdigest(),
        }
        for path in artifact_paths
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )


def archive_artifact_hashes(archive: Path) -> dict[str, str]:
    return {
        str(path.relative_to(archive)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(archive.rglob("*"))
        if path.is_file() and path.name != "workbench-manifest.json"
    }


class RelayControlTests(unittest.TestCase):
    def setUp(self):
        self.module = load_control_module()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "jobs.sqlite3"
        self.audio = self.root / "vm-20260710-120000-ABC.m4a"
        self.audio.write_bytes(b"audio")
        self.input_transcript = self.root / "current-transcript.srt"
        self.input_transcript.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n当前工作稿\n", encoding="utf-8"
        )
        try:
            self.control = self.module.RelayControl(
                self.db_path,
                archive_root=self.root,
                auto_pending_archive=False,
            )
        except TypeError as error:
            if "auto_pending_archive" not in str(error):
                raise
            self.control = self.module.RelayControl(
                self.db_path, archive_root=self.root
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def _pending_control(
        self,
        *,
        db_path: Path | None = None,
        strict_interface: bool = False,
    ):
        target_db = db_path or self.db_path
        try:
            return self.module.RelayControl(
                target_db,
                archive_root=self.root,
                auto_pending_archive=True,
            )
        except TypeError as error:
            if "auto_pending_archive" not in str(error):
                raise
            if strict_interface:
                self.fail(
                    "RelayControl 缺少 auto_pending_archive 回滚开关接口: "
                    f"{error}"
                )
            control = self.module.RelayControl(target_db, archive_root=self.root)
            control.auto_pending_archive = True
            return control

    @staticmethod
    def _advance_to_minutes(control, audio: Path) -> str:
        job_id = control.enqueue(audio)
        control.record_stage(job_id, "transcribing")
        control.record_stage(job_id, "transcript_ready")
        control.record_stage(job_id, "minutes_generating")
        return job_id

    def test_enqueue_is_idempotent_and_records_initial_state_chain(self):
        first = self.control.enqueue(self.audio)
        second = self.control.enqueue(self.audio)

        self.assertEqual(first, second)
        status = self.control.status(first)
        self.assertEqual("queued", status["status"])
        self.assertEqual(1, status["current_attempt"])
        self.assertEqual(
            ["discovered", "stabilizing", "queued"],
            [
                event["to_status"]
                for event in status["events"]
                if event["event_type"] == "status_changed"
            ],
        )

    def test_idempotent_enqueue_accepts_same_hotwords_after_caller_receipt_crash(self):
        first_hotwords = self.root / "first-hotwords.txt"
        retry_hotwords = self.root / "retry-hotwords.txt"
        changed_hotwords = self.root / "changed-hotwords.txt"
        first_hotwords.write_text("云图\nACME\n", encoding="utf-8")
        retry_hotwords.write_text("云图\nACME\n", encoding="utf-8")
        changed_hotwords.write_text("另一个词\n", encoding="utf-8")

        first = self.control.enqueue(self.audio, hotword_prompt_path=first_hotwords)
        recovered = self.control.enqueue(self.audio, hotword_prompt_path=retry_hotwords)

        self.assertEqual(first, recovered)
        with self.assertRaisesRegex(
            self.module.InvalidTransitionError, "不能在幂等入队时更换热词"
        ):
            self.control.enqueue(self.audio, hotword_prompt_path=changed_hotwords)

    def test_new_attempt_uses_minutes_protocol_v3_and_claim_exposes_it(self):
        job_id = self.control.enqueue(self.audio)

        claim = self.control.claim_next(worker_id="worker-protocol-v2")
        status = self.control.status(job_id)

        self.assertEqual(3, claim["minutes_protocol_version"])
        self.assertEqual(3, status["minutes_protocol_version"])

    def test_enqueue_snapshots_job_hotwords_and_claim_exposes_only_snapshot_path(self):
        hotwords = self.root / "project-hotwords.txt"
        hotwords.write_text("云图\nACME\n云图\n", encoding="utf-8")

        job_id = self.control.enqueue(self.audio, hotword_prompt_path=hotwords)
        hotwords.write_text("来源后来被修改", encoding="utf-8")
        claim = self.control.claim_next(worker_id="worker-hotwords")
        status = self.control.status(job_id)

        snapshot = Path(claim["hotword_prompt_path"])
        self.assertNotEqual(hotwords, snapshot)
        self.assertEqual("云图\nACME\n", snapshot.read_text(encoding="utf-8"))
        self.assertTrue(status["hotwords_configured"])
        self.assertNotIn("hotword_prompt", status)

    def test_runtime_health_requires_fresh_watchdog_and_control_worker(self):
        queued = self.control.enqueue(self.audio)
        failed_audio = self.root / "vm-20260710-130000-FAILED.m4a"
        failed_audio.write_bytes(b"failed")
        failed = self.control.enqueue(failed_audio)
        self.control.record_stage(failed, "transcribing")
        self.control.fail(failed, "transcribing", "engine failed")
        self.control.heartbeat_worker(
            "watchdog", mode="controlled", status="running", pid=os.getpid()
        )
        self.control.heartbeat_worker(
            "control-worker",
            mode="controlled",
            status="busy",
            current_job_id=queued,
            current_stage="transcribing",
            pid=os.getpid(),
        )

        result = self.control.health(control_enabled=True)

        self.assertEqual("healthy", result["status"])
        self.assertEqual("controlled", result["mode"])
        self.assertEqual("busy", result["worker"]["state"])
        self.assertEqual(queued, result["worker"]["current_job_id"])
        self.assertEqual("transcribing", result["worker"]["current_stage"])
        self.assertTrue(result["worker"]["worker_id"].startswith("control-worker-"))
        self.assertIsNotNone(result["worker"]["heartbeat_at"])
        self.assertEqual(
            {
                "queued": 1,
                "active": 0,
                "failed": 1,
                "pending_archive_failures": 0,
                "held_test_fixtures": 0,
            },
            result["counts"],
        )
        self.assertEqual(
            ["control-worker", "watchdog"],
            sorted(worker["name"] for worker in result["workers"]),
        )
        self.assertTrue(all(worker["fresh"] for worker in result["workers"]))
        self.assertNotIn("audio_path", json.dumps(result))

    def test_runtime_worker_table_is_created_only_by_first_heartbeat(self):
        lazy_db = self.root / "lazy-runtime.sqlite3"
        control = self.module.RelayControl(lazy_db, archive_root=self.root)
        control.enqueue(self.audio)

        with sqlite3.connect(f"file:{lazy_db}?mode=ro", uri=True) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runtime_workers'"
            ).fetchone()[0]

        control.heartbeat_worker(
            "watchdog", mode="controlled", status="running", pid=os.getpid()
        )

        with sqlite3.connect(f"file:{lazy_db}?mode=ro", uri=True) as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runtime_workers'"
            ).fetchone()[0]
        self.assertEqual(0, before)
        self.assertEqual(1, after)

    def test_runtime_health_marks_stopped_worker_unavailable(self):
        self.control.heartbeat_worker(
            "watchdog", mode="controlled", status="running", pid=os.getpid()
        )
        self.control.heartbeat_worker(
            "control-worker", mode="controlled", status="idle", pid=os.getpid()
        )
        self.control.stop_worker("control-worker")

        result = self.control.health(control_enabled=True)

        self.assertEqual("unavailable", result["status"])
        worker = next(item for item in result["workers"] if item["name"] == "control-worker")
        self.assertFalse(worker["fresh"])

    def test_runtime_health_reports_legacy_mode_as_degraded(self):
        self.control.heartbeat_worker(
            "watchdog", mode="legacy", status="running", pid=os.getpid()
        )

        result = self.control.health(control_enabled=False)

        self.assertEqual("degraded", result["status"])
        self.assertEqual("legacy", result["mode"])

    def test_runtime_health_rejects_reused_pid_start_token(self):
        self.control.heartbeat_worker(
            "watchdog", mode="controlled", status="running", pid=os.getpid()
        )
        self.control.heartbeat_worker(
            "control-worker", mode="controlled", status="idle", pid=os.getpid()
        )

        with patch.object(self.module, "_process_start_token", return_value="ffffffffffff"):
            result = self.control.health(control_enabled=True)

        self.assertEqual("unavailable", result["status"])
        self.assertTrue(all(not worker["fresh"] for worker in result["workers"]))

    def test_cli_health_returns_one_for_legacy_and_two_when_control_worker_missing(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
                "MEETING_RELAY_CONTROL_ENABLED": "0",
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                legacy_exit = self.module.main(["health", "--json"])
        self.assertEqual(1, legacy_exit)
        self.assertEqual("legacy", json.loads(output.getvalue())["mode"])

        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
                "MEETING_RELAY_CONTROL_ENABLED": "1",
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                unavailable_exit = self.module.main(["health", "--json"])
        self.assertEqual(2, unavailable_exit)
        self.assertEqual("unavailable", json.loads(output.getvalue())["status"])

    def test_cli_health_is_read_only_and_does_not_migrate_legacy_database(self):
        legacy_db = self.root / "legacy-health.sqlite3"
        with sqlite3.connect(legacy_db) as connection:
            connection.execute("CREATE TABLE jobs (status TEXT NOT NULL)")
            connection.execute("INSERT INTO jobs(status) VALUES ('queued')")
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(legacy_db),
                "MEETING_RELAY_CONTROL_ENABLED": "0",
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(["health", "--json"])

        with sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True) as connection:
            runtime_table_count = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runtime_workers'"
            ).fetchone()[0]
        self.assertEqual(1, exit_code)
        self.assertEqual(0, runtime_table_count)
        self.assertEqual(1, json.loads(output.getvalue())["counts"]["queued"])

    def test_existing_database_migrates_substate_attempt_and_publish_columns(self):
        job_id = self.control.enqueue(self.audio)
        with sqlite3.connect(self.db_path) as connection:
            existing_job_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(jobs)")
            }
            for column in (
                "whisper_attempt",
                "whisper_retry_requested",
                "whisper_retry_generation",
                "whisper_worker_id",
                "whisper_claimed_at",
                "index_attempt",
                "meeting_id",
                "published_archive_dir",
                "published_manifest_path",
                "published_manifest_sha256",
                "published_at",
            ):
                if column in existing_job_columns:
                    connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
            for column in (
                "input_transcript_path",
                "input_transcript_sha256",
                "input_transcript_bytes",
            ):
                connection.execute(f"ALTER TABLE attempts DROP COLUMN {column}")

        def open_migrated_control(_index):
            return self.module.RelayControl(self.db_path, archive_root=self.root)

        with ThreadPoolExecutor(max_workers=8) as executor:
            migrated_controls = list(executor.map(open_migrated_control, range(8)))
        migrated = migrated_controls[0]
        status = migrated.status(job_id)

        self.assertEqual(1, status["substates"]["whisper"]["attempt"])
        self.assertEqual(1, status["substates"]["index"]["attempt"])
        self.assertIsNone(status["meeting_id"])
        self.assertIsNone(status["published_archive_dir"])
        self.assertIsNone(status["published_manifest_path"])
        self.assertFalse(status["substates"]["whisper"]["retry_requested"])
        self.assertEqual(0, status["substates"]["whisper"]["retry_generation"])
        self.assertIsNone(status["input_transcript_path"])

    def test_published_archive_is_backfilled_when_old_database_adds_column(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        published = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN published_archive_dir")

        migrated = self.module.RelayControl(self.db_path, archive_root=self.root)

        self.assertEqual(
            published["archive_dir"], migrated.status(job_id)["published_archive_dir"]
        )

    def test_claim_next_atomically_claims_one_queued_attempt(self):
        first = self.control.enqueue(self.audio)

        claim = self.control.claim_next(worker_id="worker-test")
        second_claim = self.control.claim_next(worker_id="worker-other")

        self.assertEqual(first, claim["job_id"])
        self.assertEqual("transcribing", claim["start_stage"])
        self.assertEqual("worker-test", claim["worker_id"])
        self.assertIsNone(second_claim)
        self.assertEqual("transcribing", self.control.status(first)["status"])

    def test_stale_worker_cannot_advance_or_fail_a_new_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.claim_next(worker_id="worker-old")
        self.control.fail(job_id, "transcribing", "replace attempt")
        self.control.retry(job_id, "transcribing")
        self.control.claim_next(worker_id="worker-new")

        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.record_stage(
                job_id,
                "transcript_ready",
                expected_attempt=1,
                expected_worker="worker-old",
            )
        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.fail(
                job_id,
                "transcribing",
                "late failure",
                expected_attempt=1,
                expected_worker="worker-old",
            )

        current = self.control.status(job_id)
        self.assertEqual(2, current["current_attempt"])
        self.assertEqual("transcribing", current["status"])
        self.assertEqual("worker-new", current["worker_id"])

    def test_cancel_and_claim_race_never_cancels_a_claimed_attempt(self):
        job_id = self.control.enqueue(self.audio)
        row_read = threading.Event()
        release_cancel = threading.Event()
        claim_finished = threading.Event()
        original_job_row = self.control._job_row

        def pause_cancel_after_read(connection, requested_job_id):
            row = original_job_row(connection, requested_job_id)
            if threading.current_thread().name.startswith("cancel-race"):
                row_read.set()
                release_cancel.wait(timeout=2)
            return row

        def claim():
            try:
                return self.control.claim_next(worker_id="race-worker")
            finally:
                claim_finished.set()

        with patch.object(
            self.control, "_job_row", side_effect=pause_cancel_after_read
        ), ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="cancel-race"
        ) as executor:
            cancel_future = executor.submit(self.control.cancel, job_id)
            self.assertTrue(row_read.wait(timeout=2))
            claim_future = executor.submit(claim)
            claim_finished.wait(timeout=0.5)
            release_cancel.set()
            cancel_future.result(timeout=2)
            claimed = claim_future.result(timeout=2)

        current = self.control.status(job_id)
        if claimed is not None:
            self.assertNotEqual("cancelled", current["status"])
            self.assertEqual("race-worker", current["worker_id"])
        else:
            self.assertEqual("cancelled", current["status"])

    def test_stop_and_claim_race_never_interrupts_a_claimed_stage(self):
        job_id = self.control.enqueue(self.audio)
        row_read = threading.Event()
        release_stop = threading.Event()
        claim_finished = threading.Event()
        original_job_row = self.control._job_row

        def pause_stop_after_read(connection, requested_job_id):
            row = original_job_row(connection, requested_job_id)
            if threading.current_thread().name.startswith("stop-race"):
                row_read.set()
                release_stop.wait(timeout=2)
            return row

        def claim():
            try:
                return self.control.claim_next(worker_id="race-worker")
            finally:
                claim_finished.set()

        with patch.object(
            self.control, "_job_row", side_effect=pause_stop_after_read
        ), ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stop-race"
        ) as executor:
            stop_future = executor.submit(self.control.stop_after_stage, job_id)
            self.assertTrue(row_read.wait(timeout=2))
            claim_future = executor.submit(claim)
            claim_finished.wait(timeout=0.5)
            release_stop.set()
            stop_future.result(timeout=2)
            claimed = claim_future.result(timeout=2)

        current = self.control.status(job_id)
        if claimed is not None:
            self.assertEqual("transcribing", current["status"])
            self.assertTrue(current["stop_after_stage"])
            self.assertEqual("race-worker", current["worker_id"])
        else:
            self.assertEqual("interrupted", current["status"])

    def test_single_pipeline_does_not_claim_second_job_while_one_is_active(self):
        first = self.control.enqueue(self.audio)
        second_audio = self.root / "vm-20260710-130000-DEF.m4a"
        second_audio.write_bytes(b"audio")
        second = self.control.enqueue(second_audio)
        first_claim = self.control.claim_next(worker_id=f"watchdog-{os.getpid()}")
        active_id = first_claim["job_id"]
        queued_id = second if active_id == first else first

        blocked = self.control.claim_next(worker_id="another-worker")
        self.control.fail(active_id, "transcribing", "test failure")
        next_claim = self.control.claim_next(worker_id="another-worker")

        self.assertIsNone(blocked)
        self.assertEqual(queued_id, next_claim["job_id"])

    def test_missing_codex_callback_fails_handoff_and_unblocks_queue(self):
        first = self.control.enqueue(self.audio)
        second_audio = self.root / "vm-20260710-140000-GHI.m4a"
        second_audio.write_bytes(b"second")
        second = self.control.enqueue(second_audio)
        first_claim = self.control.claim_next(worker_id=f"watchdog-{os.getpid()}")
        active_id = first_claim["job_id"]
        queued_id = second if active_id == first else first
        self.control.record_stage(active_id, "transcript_ready")
        self.control.record_stage(active_id, "minutes_generating")
        self.control.record_codex_dispatched(active_id)

        reconciled = self.control.reconcile_codex_handoffs(grace_seconds=0)
        next_claim = self.control.claim_next(worker_id="next-worker")

        self.assertEqual(1, reconciled)
        failed = self.control.status(active_id)
        self.assertEqual("failed", failed["status"])
        self.assertEqual("codex_callback", failed["failure_stage"])
        self.assertEqual(queued_id, next_claim["job_id"])

    def test_missing_codex_callback_honours_pending_stop_request(self):
        job_id = self.control.enqueue(self.audio)
        self.control.claim_next(worker_id=f"watchdog-{os.getpid()}")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        self.control.record_codex_dispatched(job_id)
        self.control.stop_after_stage(job_id)

        self.control.reconcile_codex_handoffs(grace_seconds=0)

        self.assertEqual("interrupted", self.control.status(job_id)["status"])

    def test_restart_recovery_marks_claimed_attempt_interrupted_for_retry(self):
        job_id = self.control.enqueue(self.audio)
        self.control.claim_next(worker_id="dead-worker")

        recovered = self.control.recover_orphaned_claims()

        self.assertEqual(1, recovered)
        self.assertEqual("interrupted", self.control.status(job_id)["status"])
        self.assertEqual(2, self.control.retry(job_id, "transcribing"))

    def test_restart_recovery_does_not_interrupt_a_live_worker_pid(self):
        job_id = self.control.enqueue(self.audio)
        self.control.claim_next(worker_id=f"watchdog-{os.getpid()}")

        recovered = self.control.recover_orphaned_claims()

        self.assertEqual(0, recovered)
        self.assertEqual("transcribing", self.control.status(job_id)["status"])

    def test_restart_recovery_rejects_reused_pid_with_different_start_token(self):
        job_id = self.control.enqueue(self.audio)
        self.control.claim_next(
            worker_id=f"watchdog-{os.getpid()}-000000000000"
        )

        with patch.object(
            self.module,
            "_process_start_token",
            return_value="111111111111",
        ):
            recovered = self.control.recover_orphaned_claims()

        self.assertEqual(1, recovered)
        self.assertEqual("interrupted", self.control.status(job_id)["status"])

    def test_pid_permission_error_still_rejects_mismatched_start_token(self):
        worker_id = "watchdog-424242-000000000000"

        with patch.object(self.module.os, "kill", side_effect=PermissionError), \
                patch.object(
                    self.module,
                    "_process_start_token",
                    return_value="111111111111",
                ):
            alive = self.module._worker_process_is_alive(worker_id)

        self.assertFalse(alive)

    def test_pid_permission_error_is_conservatively_alive_when_token_unreadable(self):
        worker_id = "watchdog-424242-000000000000"

        with patch.object(self.module.os, "kill", side_effect=PermissionError), \
                patch.object(self.module, "_process_start_token", return_value=None):
            alive = self.module._worker_process_is_alive(worker_id)

        self.assertTrue(alive)

    def test_orphan_recovery_cannot_overwrite_a_newly_retried_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.claim_next(worker_id="dead-worker")
        worker_checked = threading.Event()
        release_recovery = threading.Event()

        def pause_dead_worker_check(_worker_id):
            worker_checked.set()
            release_recovery.wait(timeout=2)
            return False

        def retry_and_claim():
            self.control.fail(job_id, "transcribing", "old worker failed")
            self.control.retry(job_id, "transcribing")
            return self.control.claim_next(worker_id="worker-new")

        with patch.object(
            self.module,
            "_worker_process_is_alive",
            side_effect=pause_dead_worker_check,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            recovery = executor.submit(self.control.recover_orphaned_claims)
            self.assertTrue(worker_checked.wait(timeout=2))
            retry = executor.submit(retry_and_claim)
            release_recovery.set()
            recovery.result(timeout=2)
            claim = retry.result(timeout=2)

        current = self.control.status(job_id)
        self.assertEqual(2, current["current_attempt"])
        self.assertEqual("transcribing", current["status"])
        self.assertEqual("worker-new", current["worker_id"])
        self.assertEqual(2, claim["attempt"])

    def test_vm_id_deduplicates_same_recording_at_different_paths(self):
        first = self.control.enqueue(self.audio)
        moved = self.root / "archive" / self.audio.name
        moved.parent.mkdir()
        moved.write_bytes(b"another copy")

        self.assertEqual(first, self.control.enqueue(moved))

    def test_non_vm_audio_uses_normalized_pcm_fingerprint_when_stable(self):
        first = self.root / "meeting-a.wav"
        with wave.open(str(first), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes((b"\x00\x00\x01\x00\xff\xff") * 800)
        second = self.root / "renamed-copy.wav"
        second.write_bytes(first.read_bytes() + b"container metadata ignored by decoder")

        first_job = self.control.enqueue(first)
        second_job = self.control.enqueue(second)

        self.assertEqual(first_job, second_job)

    def test_provisional_non_vm_jobs_merge_after_worker_pcm_fingerprint(self):
        first = self.root / "event-a.wav"
        with wave.open(str(first), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes((b"\x00\x00\x01\x00\xff\xff") * 800)
        second = self.root / "event-b.wav"
        second.write_bytes(first.read_bytes() + b"different container tail")
        first_job = self.control.enqueue(first, compute_hash=False)
        second_job = self.control.enqueue(second, compute_hash=False)
        self.assertNotEqual(first_job, second_job)

        self.control.record_source_audio(first_job, first)
        duplicate = self.control.record_source_audio(second_job, second)

        self.assertEqual("cancelled", duplicate["status"])
        self.assertEqual(first_job, duplicate["deduplicated_to"])

    def test_concurrent_enqueue_returns_one_job_without_integrity_errors(self):
        with ThreadPoolExecutor(max_workers=12) as executor:
            job_ids = list(executor.map(lambda _: self.control.enqueue(self.audio), range(24)))

        self.assertEqual(1, len(set(job_ids)))

    def test_worker_can_replace_provisional_hash_after_file_stabilizes(self):
        job_id = self.control.enqueue(self.audio, compute_hash=False)
        self.assertIsNone(self.control.status(job_id)["audio_sha256"])
        self.audio.write_bytes(b"stable final audio")

        status = self.control.record_source_audio(job_id, self.audio)

        self.assertEqual(
            hashlib.sha256(b"stable final audio").hexdigest(),
            status["audio_sha256"],
        )

    def test_state_machine_accepts_linear_progress_and_rejects_skips(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.assertEqual("transcript_ready", self.control.status(job_id)["status"])

        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.record_stage(job_id, "completed_unreviewed")

    def test_retry_creates_attempt_and_queues_requested_stage(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.fail(job_id, stage="transcribing", error="FunASR failed")

        attempt = self.control.retry(job_id, "transcribing")

        status = self.control.status(job_id)
        self.assertEqual(2, attempt)
        self.assertEqual("queued", status["status"])
        self.assertEqual(2, status["current_attempt"])
        self.assertEqual("transcribing", status["retry_stage"])
        self.assertEqual("queued", status["attempts"][-1]["status"])
        self.assertEqual("transcribing", status["attempts"][-1]["requested_stage"])

    def test_retry_can_resume_directly_at_requested_later_stage(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        self.control.fail(job_id, stage="minutes_generating", error="Codex failed")
        self.control.retry(
            job_id, "minutes_generating", transcript_path=self.input_transcript
        )

        self.control.record_stage(job_id, "minutes_generating")

        self.assertEqual("minutes_generating", self.control.status(job_id)["status"])

    def test_enqueue_minutes_only_snapshots_transcript_and_completes_minimal_archive(self):
        expected_text = self.input_transcript.read_text(encoding="utf-8")
        job_id = self.control.enqueue(
            self.audio,
            requested_stage="minutes_generating",
            transcript_path=self.input_transcript,
        )
        self.input_transcript.write_text("来源文件后来被修改", encoding="utf-8")

        claim = self.control.claim_next(worker_id="worker-minutes-only")
        snapshot = Path(claim["input_transcript_path"])
        archive = create_minutes_only_archive(self.root, job_id, attempt=1)
        completed = self.control.complete_minutes(job_id, archive, attempt_no=1)

        self.assertEqual("minutes_generating", claim["start_stage"])
        self.assertEqual(expected_text, snapshot.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(expected_text.encode()).hexdigest(),
            claim["input_transcript_sha256"],
        )
        self.assertEqual("completed_unreviewed", completed["status"])
        self.assertNotIn("transcript_srt", completed["artifacts"])

    def test_minutes_retry_requires_explicit_transcript_snapshot(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.fail(job_id, "transcribing", "retry")

        with self.assertRaises(self.module.RelayControlError):
            self.control.retry(job_id, "minutes_generating")

    def test_minutes_only_manifest_binds_requested_stage_and_snapshot_hash(self):
        job_id = self.control.enqueue(
            self.audio,
            requested_stage="minutes_generating",
            transcript_path=self.input_transcript,
        )
        self.control.claim_next(worker_id="worker-minutes-only")
        archive = create_minutes_only_archive(self.root, job_id, attempt=1)
        manifest_path = archive / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["requested_stage"] = "transcribing"
        manifest["input_transcript_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(self.module.ArtifactValidationError) as caught:
            self.control.complete_minutes(job_id, archive, attempt_no=1)

        self.assertIn("manifest_requested_stage", caught.exception.report.missing)
        self.assertIn(
            "manifest_input_transcript_sha256", caught.exception.report.missing
        )

    def test_input_snapshot_never_satisfies_full_transcript_requirement(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        transcript = archive / "vm-20260710-120000-ABC.txt"
        transcript.rename(archive / "input-transcript.txt")
        refresh_published_manifest(archive / "workbench-manifest.json")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("transcript_txt", report.missing)

    def test_transcript_snapshot_rejects_symlink_input(self):
        outside = self.root / "outside-transcript.txt"
        outside.write_text("外部稿件", encoding="utf-8")
        linked = self.root / "linked-transcript.txt"
        linked.symlink_to(outside)

        with self.assertRaises(self.module.RelayControlError):
            self.control.enqueue(
                self.audio,
                requested_stage="minutes_generating",
                transcript_path=linked,
            )

    def test_enqueue_rejects_missing_archive_root_without_creating_it(self):
        source_root = self.root / "source"
        source_root.mkdir()
        audio = source_root / "vm-20260710-130000-DEF.m4a"
        audio.write_bytes(b"audio")
        archive_root = self.root / "detached-archive"
        control = self.module.RelayControl(
            self.root / "detached.sqlite3", archive_root=archive_root
        )

        with self.assertRaises(self.module.RelayControlError):
            control.enqueue(audio)

        self.assertFalse(archive_root.exists())

    def test_retry_rejects_disappeared_archive_root_without_recreating_it(self):
        source_root = self.root / "source"
        source_root.mkdir()
        audio = source_root / "vm-20260710-130000-DEF.m4a"
        audio.write_bytes(b"audio")
        archive_root = self.root / "detachable-archive"
        archive_root.mkdir()
        control = self.module.RelayControl(
            self.root / "detached.sqlite3", archive_root=archive_root
        )
        job_id = control.enqueue(audio)
        control.claim_next(worker_id="worker-test")
        control.fail(job_id, "transcribing", "test failure")
        archive_root.rmdir()

        with self.assertRaises(self.module.RelayControlError):
            control.retry(job_id, "transcribing")

        self.assertFalse(archive_root.exists())

    def test_enqueue_rejects_symlink_archive_root(self):
        source_root = self.root / "source"
        source_root.mkdir()
        audio = source_root / "vm-20260710-130000-DEF.m4a"
        audio.write_bytes(b"audio")
        real_archive = self.root / "real-archive"
        real_archive.mkdir()
        linked_archive = self.root / "linked-archive"
        linked_archive.symlink_to(real_archive, target_is_directory=True)
        control = self.module.RelayControl(
            self.root / "linked.sqlite3", archive_root=linked_archive
        )

        with self.assertRaises(self.module.RelayControlError):
            control.enqueue(audio)

    def test_volumes_archive_requires_a_real_mountpoint(self):
        archive_root = Path("/Volumes/Detached/meetings")
        control = self.module.RelayControl(
            self.root / "volume.sqlite3", archive_root=archive_root
        )

        with patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(self.module.os.path, "ismount", return_value=False):
            with self.assertRaises(self.module.RelayControlError):
                control.enqueue(self.audio, compute_hash=False)

    def test_volumes_archive_requires_a_distinct_mounted_device(self):
        archive_root = Path("/Volumes/Detached/meetings")
        control = self.module.RelayControl(
            self.root / "device.sqlite3", archive_root=archive_root
        )
        original_stat = os.stat

        def matching_volume_stat(path, *args, **kwargs):
            if Path(path) in {Path("/Volumes"), Path("/Volumes/Detached")}:
                return os.stat_result((0, 0, 7, 0, 0, 0, 0, 0, 0, 0))
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(self.module.os.path, "ismount", return_value=True), \
                patch.object(self.module.os, "stat", side_effect=matching_volume_stat):
            with self.assertRaises(self.module.RelayControlError):
                control.enqueue(self.audio, compute_hash=False)

    def test_volumes_archive_accepts_a_distinct_real_mount(self):
        archive_root = Path("/Volumes/Attached/meetings")
        control = self.module.RelayControl(
            self.root / "mounted.sqlite3", archive_root=archive_root
        )
        original_stat = os.stat

        def distinct_volume_stat(path, *args, **kwargs):
            if Path(path) == Path("/Volumes"):
                return os.stat_result((0, 0, 7, 0, 0, 0, 0, 0, 0, 0))
            if Path(path) == Path("/Volumes/Attached"):
                return os.stat_result((0, 0, 8, 0, 0, 0, 0, 0, 0, 0))
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(self.module.os.path, "ismount", return_value=True), \
                patch.object(self.module.os, "stat", side_effect=distinct_volume_stat):
            job_id = control.enqueue(self.audio, compute_hash=False)

        self.assertEqual("queued", control.status(job_id)["status"])

    def test_claim_next_uses_retry_stage_without_replaying_earlier_stages(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        self.control.fail(job_id, stage="minutes_generating", error="Codex failed")
        self.control.retry(
            job_id, "minutes_generating", transcript_path=self.input_transcript
        )

        claim = self.control.claim_next(worker_id="worker-retry")

        self.assertEqual("minutes_generating", claim["start_stage"])
        self.assertEqual("minutes_generating", self.control.status(job_id)["status"])

    def test_retry_requires_failed_cancelled_or_interrupted_job(self):
        job_id = self.control.enqueue(self.audio)

        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.retry(job_id, "transcribing")

    def test_completed_unreviewed_can_create_a_non_published_reprocess_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, archive)

        attempt = self.control.retry(
            job_id, "minutes_generating", transcript_path=self.input_transcript
        )

        status = self.control.status(job_id)
        self.assertEqual(2, attempt)
        self.assertEqual("queued", status["status"])
        self.assertEqual(str(archive.resolve()), status["archive_dir"])

    def test_published_job_can_regenerate_minutes_and_publish_a_new_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft_one = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft_one)
        manifest_one = create_published_archive(self.root, draft_one, job_id)
        first_publish = self.control.mark_published(
            job_id, manifest_one, "vm-20260710-120000-ABC"
        )

        attempt = self.control.retry(
            job_id, "minutes_generating", transcript_path=self.input_transcript
        )
        queued = self.control.status(job_id)
        claim = self.control.claim_next(worker_id="worker-republish")
        draft_two = create_minutes_only_archive(self.root, job_id, attempt=2)
        completed = self.control.complete_minutes(job_id, draft_two, attempt_no=2)
        manifest_two = create_published_archive(
            self.root,
            draft_two,
            job_id,
            directory_name="260710 工作台发布测试-v2",
        )
        second_publish = self.control.mark_published(
            job_id, manifest_two, "vm-20260710-120000-ABC"
        )

        self.assertEqual(2, attempt)
        self.assertEqual("queued", queued["status"])
        self.assertEqual(first_publish["archive_dir"], queued["published_archive_dir"])
        self.assertEqual(
            first_publish["published_manifest_sha256"],
            queued["published_manifest_sha256"],
        )
        self.assertEqual("minutes_generating", claim["start_stage"])
        self.assertEqual(first_publish["archive_dir"], claim["published_archive_dir"])
        self.assertEqual("completed_unreviewed", completed["status"])
        self.assertEqual(str(draft_two.resolve()), completed["archive_dir"])
        self.assertEqual(first_publish["archive_dir"], completed["published_archive_dir"])
        self.assertEqual("published", second_publish["status"])
        self.assertEqual(str(manifest_two.parent.resolve()), second_publish["archive_dir"])
        published_events = [
            event
            for event in second_publish["events"]
            if event["event_type"] == "job_published"
        ]
        self.assertEqual(2, len(published_events))
        self.assertEqual(
            first_publish["published_manifest_sha256"],
            published_events[1]["payload"]["previous_published_snapshot"][
                "manifest_sha256"
            ],
        )

    def test_new_attempt_cannot_be_published_with_an_old_attempt_manifest(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft_one = create_complete_archive(self.root, job_id, attempt=1)
        self.control.complete_minutes(job_id, draft_one, attempt_no=1)
        old_manifest = create_published_archive(self.root, draft_one, job_id)
        self.control.mark_published(
            job_id, old_manifest, "vm-20260710-120000-ABC"
        )

        self.control.retry(
            job_id, "minutes_generating", transcript_path=self.input_transcript
        )
        self.control.claim_next(worker_id="worker-current-attempt")
        draft_two = create_minutes_only_archive(self.root, job_id, attempt=2)
        self.control.complete_minutes(job_id, draft_two, attempt_no=2)

        with self.assertRaises(self.module.PublishValidationError) as caught:
            self.control.mark_published(
                job_id, old_manifest, "vm-20260710-120000-ABC"
            )

        self.assertIn("attempt", str(caught.exception))
        current = self.control.status(job_id)
        self.assertEqual(2, current["current_attempt"])
        self.assertEqual("completed_unreviewed", current["status"])
        self.assertEqual(str(draft_two.resolve()), current["archive_dir"])

    def test_published_job_can_start_a_fresh_retranscription_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_substate(job_id, "whisper", "ready", attempt_no=1)
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        published = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        attempt = self.control.retry(job_id, "transcribing")
        status = self.control.status(job_id)

        self.assertEqual(2, attempt)
        self.assertEqual("queued", status["status"])
        self.assertEqual("pending", status["substates"]["whisper"]["status"])
        self.assertEqual(published["archive_dir"], status["published_archive_dir"])
        self.assertEqual(
            published["published_manifest_sha256"],
            status["published_manifest_sha256"],
        )
        claim_two = self.control.claim_next(worker_id="worker-attempt-two")
        self.assertEqual("transcribing", claim_two["start_stage"])
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft_two = create_complete_archive(self.root, job_id, attempt=2)
        self.control.complete_minutes(job_id, draft_two, attempt_no=2)
        transcript_two = draft_two / "vm-20260710-120000-ABC.srt"

        attempt_three = self.control.retry(
            job_id, "minutes_generating", transcript_path=transcript_two
        )
        claim_three = self.control.claim_next(worker_id="worker-attempt-three")

        self.assertEqual(3, attempt_three)
        self.assertEqual(str(draft_two.resolve()), claim_three["source_archive_dir"])
        self.assertEqual(published["archive_dir"], claim_three["published_archive_dir"])

    def test_stop_after_stage_preserves_completed_stage_then_ends_interrupted(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")

        self.control.stop_after_stage(job_id)

        self.assertEqual("transcribing", self.control.status(job_id)["status"])
        self.assertTrue(self.control.should_stop_after_stage(job_id))
        self.assertTrue(
            self.control.interrupt_if_stop_requested(job_id, "transcribing")
        )
        self.assertEqual("interrupted", self.control.status(job_id)["status"])

    def test_stop_after_stage_interrupts_queued_job_before_it_runs(self):
        job_id = self.control.enqueue(self.audio)

        result = self.control.stop_after_stage(job_id)

        self.assertEqual("interrupted", result["status"])
        self.assertIsNone(self.control.claim_next(worker_id="worker-test"))

    def test_cancel_only_cancels_a_queued_attempt(self):
        job_id = self.control.enqueue(self.audio)

        self.control.cancel(job_id)

        self.assertEqual("cancelled", self.control.status(job_id)["status"])
        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.cancel(job_id)

    def test_complete_minutes_validates_and_records_artifacts(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual(str(archive.resolve()), result["archive_dir"])
        self.assertIn("workbench-manifest.json", result["artifacts"])
        self.assertIn(
            "whisper-ref/vm-20260710-120000-ABC.vtt", result["artifacts"]
        )
        self.assertEqual("ready", result["substates"]["whisper"]["status"])

    def test_protocol_v2_completion_requires_minutes_evidence(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        archive = create_complete_archive(
            self.root, job_id, include_minutes_evidence=False
        )

        with self.assertRaises(self.module.ArtifactValidationError) as caught:
            self.control.complete_minutes(job_id, archive)

        self.assertIn("minutes_evidence", caught.exception.report.missing)

    def test_existing_protocol_v2_attempt_still_completes_after_v3_upgrade(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        with self.control._connect() as connection:
            connection.execute(
                "UPDATE attempts SET minutes_protocol_version = 2 "
                "WHERE job_id = ? AND attempt_no = 1",
                (job_id,),
            )
        archive = create_complete_archive(
            self.root,
            job_id,
            minutes_protocol_version=2,
        )

        completed = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", completed["status"])
        self.assertEqual(2, completed["minutes_protocol_version"])

    def test_protocol_v3_rejects_joint_srt_and_plan_tampering_against_attempt_hash(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        archive = create_complete_archive(self.root, job_id)
        srt = archive / "vm-20260710-120000-ABC.srt"
        original_hash = hashlib.sha256(srt.read_bytes()).hexdigest()
        self.control.record_minutes_plan_source(
            job_id,
            attempt_no=1,
            source_srt_sha256=original_hash,
            minutes_plan_sha256=hashlib.sha256(
                (archive / "minutes-plan.json").read_bytes()
            ).hexdigest(),
        )
        srt.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n篡改\n", encoding="utf-8"
        )
        tampered_text_hash = hashlib.sha256("篡改".encode("utf-8")).hexdigest()
        plan_path = archive / "minutes-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["source_srt_sha256"] = hashlib.sha256(srt.read_bytes()).hexdigest()
        plan["windows"][0]["cues"][0]["source_text_sha256"] = tampered_text_hash
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        evidence_path = archive / "minutes-evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["topics"][0]["items"][0][
            "source_text_sha256"
        ] = tampered_text_hash
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        refresh_published_manifest(archive / "workbench-manifest.json")

        with self.assertRaises(self.module.ArtifactValidationError) as caught:
            self.control.complete_minutes(job_id, archive)

        self.assertIn("minutes_plan_source_mismatch", caught.exception.report.missing)

    def test_protocol_v3_rejects_plan_duration_and_boundary_tampering(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        archive = create_complete_archive(self.root, job_id)
        srt = archive / "vm-20260710-120000-ABC.srt"
        plan_path = archive / "minutes-plan.json"
        self.control.record_minutes_plan_source(
            job_id,
            attempt_no=1,
            source_srt_sha256=hashlib.sha256(srt.read_bytes()).hexdigest(),
            minutes_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["total_duration_sec"] = 3
        plan["windows"][0]["end_sec"] = 3
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        refresh_published_manifest(archive / "workbench-manifest.json")

        with self.assertRaises(self.module.ArtifactValidationError) as caught:
            self.control.complete_minutes(job_id, archive)

        self.assertIn("minutes_plan_hash_mismatch", caught.exception.report.missing)

    def test_protocol_v2_completion_rejects_anchor_missing_from_minutes(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        archive = create_complete_archive(self.root, job_id)
        minutes_path = archive / "测试会议.md"
        minutes_path.write_text("# 会议纪要\n\n没有时间锚", encoding="utf-8")
        refresh_published_manifest(archive / "workbench-manifest.json")

        with self.assertRaises(self.module.ArtifactValidationError) as caught:
            self.control.complete_minutes(job_id, archive)

        self.assertIn(
            "minutes_evidence_anchor:D01", caught.exception.report.missing
        )

    def test_protocol_v2_publish_rejects_stale_minutes_evidence_anchor(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest_path = create_published_archive(self.root, draft, job_id)
        published = manifest_path.parent
        (published / "测试会议.md").write_text(
            "# 会议纪要\n\n发布稿删除了时间锚", encoding="utf-8"
        )
        refresh_published_manifest(manifest_path)

        with self.assertRaises(self.module.PublishValidationError) as caught:
            self.control.mark_published(
                job_id, manifest_path, "vm-20260710-120000-ABC"
            )

        self.assertIn("minutes_evidence_anchor:D01", str(caught.exception))

    def test_complete_minutes_moves_attempt_to_visible_pending_archive(self):
        control = self._pending_control(strict_interface=True)
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        before_hashes = archive_artifact_hashes(hidden)
        before_manifest = json.loads(
            (hidden / "workbench-manifest.json").read_text(encoding="utf-8")
        )

        result = control.complete_minutes(job_id, hidden, attempt_no=1)

        pending = Path(result["archive_dir"])
        self.assertEqual(
            (self.root / "260710 测试会议").resolve(),
            pending.resolve(),
        )
        self.assertTrue(pending.is_dir())
        self.assertFalse(hidden.exists())
        self.assertEqual(before_hashes, archive_artifact_hashes(pending))
        after_manifest = json.loads(
            (pending / "workbench-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            (before_manifest["schema_version"], before_manifest["job_id"], before_manifest["attempt"]),
            (after_manifest["schema_version"], after_manifest["job_id"], after_manifest["attempt"]),
        )

    def test_pending_title_ignores_appledouble_sidecar_documents(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        (hidden / "._260710 测试会议.md").write_bytes(b"appledouble")
        (hidden / "._260710 测试会议.html").write_bytes(b"appledouble")

        result = control.complete_minutes(job_id, hidden, attempt_no=1)

        self.assertEqual(
            (self.root / "260710 测试会议").resolve(),
            Path(result["archive_dir"]).resolve(),
        )

    def test_complete_minutes_replayed_old_callback_is_idempotent(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        first = control.complete_minutes(job_id, hidden, attempt_no=1)

        try:
            second = control.complete_minutes(job_id, hidden, attempt_no=1)
        except Exception as error:  # desired replay contract is a successful no-op
            self.fail(f"相同完成回执重放应幂等，实际抛出 {type(error).__name__}: {error}")

        self.assertEqual(first["archive_dir"], second["archive_dir"])
        self.assertEqual(
            1,
            len(
                [
                    event
                    for event in second["events"]
                    if event["event_type"] == "minutes_completed"
                ]
            ),
        )
        pending_dirs = control._visible_pending_for_job(job_id, attempt_no=1)
        self.assertEqual([Path(first["archive_dir"]).resolve()], pending_dirs)

    def test_same_date_and_title_never_merge_different_jobs(self):
        control = self._pending_control()
        second_audio = self.root / "vm-20260710-120001-DEF.m4a"
        second_audio.write_bytes(b"audio")
        first_job = self._advance_to_minutes(control, self.audio)
        first_hidden = create_complete_archive(
            self.root, first_job, stem=self.audio.stem
        )
        first = control.complete_minutes(first_job, first_hidden, attempt_no=1)
        second_job = self._advance_to_minutes(control, second_audio)
        second_hidden = create_complete_archive(
            self.root, second_job, stem=second_audio.stem
        )
        second = control.complete_minutes(second_job, second_hidden, attempt_no=1)

        first_pending = Path(first["archive_dir"])
        second_pending = Path(second["archive_dir"])
        self.assertEqual(self.root.resolve(), first_pending.parent.resolve())
        self.assertEqual(self.root.resolve(), second_pending.parent.resolve())
        self.assertNotEqual(first_pending, second_pending)
        self.assertEqual(
            first_job,
            json.loads(
                (first_pending / "workbench-manifest.json").read_text(encoding="utf-8")
            )["job_id"],
        )
        self.assertEqual(
            second_job,
            json.loads(
                (second_pending / "workbench-manifest.json").read_text(encoding="utf-8")
            )["job_id"],
        )

    def test_whisper_retry_installs_into_visible_pending_archive(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        control.record_substate(job_id, "whisper", "failed", error="engine")
        control.retry_substate(job_id, "whisper")
        claim = control.claim_whisper_retry(worker_id="worker-pending-whisper")
        staging = create_whisper_staging(self.root, stem=self.audio.stem)

        finished = control.finish_whisper_retry(
            job_id,
            attempt_no=claim["attempt"],
            generation=claim["generation"],
            worker_id=claim["worker_id"],
            success=True,
            artifact_dir=staging,
        )

        pending = Path(completed["archive_dir"])
        self.assertEqual(self.root.resolve(), pending.parent.resolve())
        self.assertEqual(str(pending.resolve()), claim["target_archive_dir"])
        self.assertEqual("ready", finished["substates"]["whisper"]["status"])
        self.assertTrue((pending / "whisper-ref" / f"{self.audio.stem}.json").is_file())
        self.assertFalse(hidden.exists())

    def test_migrate_pending_archives_is_scoped_and_idempotent(self):
        eligible_job = self._advance_to_minutes(self.control, self.audio)
        eligible_hidden = create_complete_archive(self.root, eligible_job)
        self.control.complete_minutes(eligible_job, eligible_hidden, attempt_no=1)
        expected_hashes = archive_artifact_hashes(eligible_hidden)

        unselected_audio = self.root / "vm-20260710-120002-UNSELECTED.m4a"
        unselected_audio.write_bytes(b"audio")
        unselected_job = self._advance_to_minutes(self.control, unselected_audio)
        unselected_hidden = create_complete_archive(
            self.root, unselected_job, stem=unselected_audio.stem
        )
        self.control.complete_minutes(unselected_job, unselected_hidden, attempt_no=1)

        queued_audio = self.root / "vm-20260710-120003-QUEUED.m4a"
        queued_audio.write_bytes(b"audio")
        queued_job = self.control.enqueue(queued_audio)
        queued_hidden = create_complete_archive(
            self.root, queued_job, stem=queued_audio.stem
        )

        migrate = getattr(self.control, "migrate_pending_archives", None)
        self.assertIsNotNone(migrate, "RelayControl 缺少 migrate_pending_archives")
        migrate(job_ids=[eligible_job, queued_job])
        first_status = self.control.status(eligible_job)
        first_pending = Path(first_status["archive_dir"])
        migrate(job_ids=[eligible_job, queued_job])
        second_status = self.control.status(eligible_job)

        self.assertEqual(first_status["archive_dir"], second_status["archive_dir"])
        self.assertEqual(self.root.resolve(), first_pending.parent.resolve())
        self.assertEqual(expected_hashes, archive_artifact_hashes(first_pending))
        self.assertFalse(eligible_hidden.exists())
        self.assertTrue(unselected_hidden.is_dir())
        self.assertEqual(
            str(unselected_hidden.resolve()),
            self.control.status(unselected_job)["archive_dir"],
        )
        self.assertTrue(queued_hidden.is_dir())
        self.assertIsNone(self.control.status(queued_job)["archive_dir"])

    def test_migrate_legacy_pending_directory_flattens_it_to_archive_root(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = self.control.complete_minutes(job_id, hidden, attempt_no=1)
        legacy = self.root / "待校对" / "260710 测试会议"
        legacy.parent.mkdir()
        os.replace(Path(completed["archive_dir"]), legacy)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE jobs SET archive_dir=? WHERE job_id=?",
                (str(legacy.resolve()), job_id),
            )

        summary = self._pending_control().migrate_pending_archives([job_id])
        current = self.control.status(job_id)
        flattened = Path(current["archive_dir"])

        self.assertTrue(summary["ok"], summary)
        self.assertIn(job_id, summary["promoted"])
        self.assertEqual(self.root.resolve(), flattened.parent.resolve())
        self.assertTrue(flattened.is_dir())
        self.assertFalse(legacy.exists())

    def test_legacy_flatten_recovers_after_install_before_database_cas(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = self.control.complete_minutes(job_id, hidden, attempt_no=1)
        legacy = self.root / "待校对" / "260710 测试会议"
        legacy.parent.mkdir()
        os.replace(Path(completed["archive_dir"]), legacy)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE jobs SET archive_dir=? WHERE job_id=?",
                (str(legacy.resolve()), job_id),
            )
        control = self._pending_control()
        original_write = control._write_json_atomically

        def interrupt_after_install(path, payload):
            original_write(path, payload)
            if payload.get("state") == "new_installed":
                raise KeyboardInterrupt("installed before DB CAS")

        with patch.object(
            control,
            "_write_json_atomically",
            side_effect=interrupt_after_install,
        ):
            with self.assertRaises(KeyboardInterrupt):
                control.migrate_pending_archives([job_id])

        installed = control._visible_pending_for_job(job_id, attempt_no=1)
        self.assertEqual(1, len(installed))
        self.assertEqual(self.root.resolve(), installed[0].parent.resolve())
        self.assertFalse(legacy.exists())
        self.assertEqual(
            str(legacy.resolve()), self.control.status(job_id)["archive_dir"]
        )

        recovered = self._pending_control().migrate_pending_archives([job_id])

        self.assertTrue(recovered["ok"], recovered)
        self.assertIn(job_id, recovered["promoted"])
        self.assertEqual(
            str(installed[0]), self.control.status(job_id)["archive_dir"]
        )
        self.assertFalse(
            (
                self.root
                / ".workbench-pending-journal"
                / f"{job_id}-attempt-1.json"
            ).exists()
        )

    def test_migration_ignores_flat_directory_during_publish_ack_window(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        flat = Path(completed["archive_dir"])
        manifest_path = flat / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "published"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

        summary = control.migrate_pending_archives([job_id])

        self.assertTrue(summary["ok"], summary)
        self.assertIn(
            {"job_id": job_id, "reason": "awaiting_publish_ack"},
            summary["skipped"],
        )
        self.assertTrue(flat.is_dir())
        self.assertFalse(
            (self.root / ".workbench-recovery" / "replaced" / job_id).exists()
        )

    def test_database_fixture_policy_prevents_pending_promotion(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        control.set_archive_policy(job_id, "hidden_fixture")
        hidden = create_complete_archive(self.root, job_id)

        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        reconciled = control.reconcile_pending_archives()

        self.assertEqual(str(hidden.resolve()), completed["archive_dir"])
        self.assertEqual("hidden_fixture", completed["archive_policy"])
        self.assertTrue(hidden.is_dir())
        self.assertFalse((self.root / "待校对").exists())
        self.assertNotIn(job_id, reconciled["promoted"])
        self.assertIn(
            {"job_id": job_id, "reason": "policy:hidden_fixture"},
            reconciled["skipped"],
        )
        self.assertEqual(
            1, control.health(control_enabled=True)["counts"]["held_test_fixtures"]
        )

    def test_archive_contents_cannot_opt_out_of_pending_promotion(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        marker = hidden / ".workbench-keep-hidden"
        marker.write_text("ordinary artifact\n", encoding="utf-8")
        manifest_path = hidden / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"].append(
            {
                "path": marker.name,
                "bytes": marker.stat().st_size,
                "sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )

        completed = control.complete_minutes(job_id, hidden, attempt_no=1)

        self.assertEqual("visible", completed["archive_policy"])
        self.assertEqual(
            self.root.resolve(),
            Path(completed["archive_dir"]).parent.resolve(),
        )

    def test_pending_recovery_journal_wins_over_fixture_policy(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        self.control.set_archive_policy(job_id, "hidden_fixture")
        hidden = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, hidden, attempt_no=1)
        target = self.root / "待校对" / "260710 测试会议"
        target.parent.mkdir(exist_ok=True)
        journal = (
            self.root
            / ".workbench-pending-journal"
            / f"{job_id}-attempt-1.json"
        )
        journal.parent.mkdir(exist_ok=True)
        journal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "minutes_protocol_version": 3,
                    "job_id": job_id,
                    "attempt": 1,
                    "source": str(hidden),
                    "target": str(target),
                    "state": "prepared",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        migrated = self._pending_control().migrate_pending_archives([job_id])

        self.assertEqual([job_id], migrated["promoted"], migrated)
        self.assertEqual(
            (self.root / target.name).resolve(),
            Path(self.control.status(job_id)["archive_dir"]).resolve(),
        )
        self.assertFalse(target.exists())

    def test_reconcile_pending_archives_promotes_and_backfills_product_whisper(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        self.control.complete_minutes(job_id, hidden, attempt_no=1)
        self.control.record_substate(job_id, "whisper", "running")
        products_root = self.root / "products"
        product_archive = products_root / self.audio.stem / self.audio.stem
        whisper_source = create_whisper_staging(
            self.root, stem=self.audio.stem
        ) / "whisper-ref"
        product_archive.mkdir(parents=True)
        shutil.copytree(whisper_source, product_archive / "whisper-ref")
        control = self._pending_control()
        control.products_root = products_root

        reconcile = getattr(control, "reconcile_pending_archives", None)
        self.assertIsNotNone(reconcile, "RelayControl 缺少 reconcile_pending_archives")
        summary = reconcile()
        current = control.status(job_id)
        pending = Path(current["archive_dir"])

        self.assertTrue(summary["ok"], summary)
        self.assertIn(job_id, summary["promoted"])
        self.assertIn(job_id, summary["whisper_ready"])
        self.assertEqual("ready", current["substates"]["whisper"]["status"])
        self.assertEqual(
            self.root.resolve(),
            pending.parent.resolve(),
        )
        for name in (
            f"{self.audio.stem}.json",
            f"{self.audio.stem}.tsv",
            f"{self.audio.stem}.srt",
            f"{self.audio.stem}.txt",
            f"{self.audio.stem}.vtt",
            "whisper.log",
        ):
            self.assertTrue((pending / "whisper-ref" / name).is_file(), name)
        self.assertFalse(hidden.exists())

    def test_whisper_reconcile_recovers_copy_interrupted_outside_visible_pending(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        control.record_substate(job_id, "whisper", "running")
        products_root = self.root / "products"
        product_archive = products_root / self.audio.stem / self.audio.stem
        product_archive.mkdir(parents=True)
        source = create_whisper_staging(
            self.root, stem=self.audio.stem
        ) / "whisper-ref"
        shutil.copytree(source, product_archive / "whisper-ref")
        control.products_root = products_root
        recovery = (
            self.root
            / ".workbench-recovery"
            / "whisper-install"
            / job_id
            / "attempt-1"
        )

        def copy_then_interrupt(source_dir, target_dir, *args, **kwargs):
            target = Path(target_dir)
            target.mkdir(parents=True)
            first = next(Path(source_dir).iterdir())
            shutil.copy2(first, target / first.name)
            raise KeyboardInterrupt("copy interrupted")

        with patch.object(
            self.module.shutil, "copytree", side_effect=copy_then_interrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                control._reconcile_product_whisper(job_id, product_archive)

        self.assertTrue((recovery / "journal.json").is_file())
        self.assertFalse(
            any(path.name.startswith(".whisper") for path in pending.iterdir())
        )

        summary = control.reconcile_pending_archives()

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(
            "ready", control.status(job_id)["substates"]["whisper"]["status"]
        )
        self.assertFalse(recovery.exists())
        self.assertFalse(
            any(path.name.startswith(".whisper") for path in pending.iterdir())
        )

    def test_whisper_reconcile_recovers_old_moved_install_journal(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        control.record_substate(job_id, "whisper", "running")
        products_root = self.root / "products"
        product_archive = products_root / self.audio.stem / self.audio.stem
        product_archive.mkdir(parents=True)
        source = create_whisper_staging(
            self.root, stem=self.audio.stem
        ) / "whisper-ref"
        shutil.copytree(source, product_archive / "whisper-ref")
        control.products_root = products_root
        recovery = (
            self.root
            / ".workbench-recovery"
            / "whisper-install"
            / job_id
            / "attempt-1"
        )
        real_replace = self.module.os.replace

        def interrupt_before_new_install(source_path, target_path):
            source_candidate = Path(source_path)
            target_candidate = Path(target_path)
            if (
                target_candidate == pending / "whisper-ref"
                and source_candidate != pending / "whisper-ref"
            ):
                raise KeyboardInterrupt("new install interrupted")
            return real_replace(source_path, target_path)

        with patch.object(
            self.module.os, "replace", side_effect=interrupt_before_new_install
        ):
            with self.assertRaises(KeyboardInterrupt):
                control._reconcile_product_whisper(job_id, product_archive)

        journal = json.loads(
            (recovery / "journal.json").read_text(encoding="utf-8")
        )
        self.assertEqual("new_installing", journal["phase"])
        self.assertFalse((pending / "whisper-ref").exists())
        self.assertTrue((recovery / "backup-whisper-ref").is_dir())

        summary = control.reconcile_pending_archives()

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(
            "ready", control.status(job_id)["substates"]["whisper"]["status"]
        )
        self.assertEqual(
            "ready", self.module.inspect_whisper_ref(pending)["status"]
        )
        self.assertFalse(recovery.exists())

    def test_whisper_reconcile_recovers_new_installed_before_database_commit(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        control.record_substate(job_id, "whisper", "running")
        products_root = self.root / "products"
        product_archive = products_root / self.audio.stem / self.audio.stem
        product_archive.mkdir(parents=True)
        source = create_whisper_staging(
            self.root, stem=self.audio.stem
        ) / "whisper-ref"
        shutil.copytree(source, product_archive / "whisper-ref")
        control.products_root = products_root
        recovery = (
            self.root
            / ".workbench-recovery"
            / "whisper-install"
            / job_id
            / "attempt-1"
        )

        with patch.object(
            control, "_append_event", side_effect=KeyboardInterrupt("db interrupted")
        ):
            with self.assertRaises(KeyboardInterrupt):
                control._reconcile_product_whisper(job_id, product_archive)

        journal = json.loads(
            (recovery / "journal.json").read_text(encoding="utf-8")
        )
        self.assertEqual("new_installed", journal["phase"])
        self.assertEqual(
            "running", control.status(job_id)["substates"]["whisper"]["status"]
        )

        summary = control.reconcile_pending_archives()

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(
            "ready", control.status(job_id)["substates"]["whisper"]["status"]
        )
        self.assertEqual(
            "ready", self.module.inspect_whisper_ref(pending)["status"]
        )
        self.assertFalse(recovery.exists())

    def test_reconcile_respects_auto_pending_archive_rollback_switch(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, hidden, attempt_no=1)

        reconciled = self.control.reconcile_pending_archives()

        self.assertTrue(hidden.is_dir())
        self.assertEqual(
            str(hidden.resolve()), self.control.status(job_id)["archive_dir"]
        )
        self.assertNotIn(job_id, reconciled["promoted"])
        migrated = self.control.migrate_pending_archives([job_id])
        self.assertEqual([job_id], migrated["promoted"])

    def test_old_moved_journal_reselects_target_occupied_by_another_job(self):
        occupying_audio = self.root / "vm-20260710-120001-BBB.m4a"
        occupying_audio.write_bytes(b"audio")
        pending_control = self._pending_control()
        occupying_job = self._advance_to_minutes(
            pending_control, occupying_audio
        )
        occupying_hidden = create_complete_archive(
            self.root, occupying_job, stem=occupying_audio.stem
        )
        occupying_status = pending_control.complete_minutes(
            occupying_job, occupying_hidden, attempt_no=1
        )
        occupied_target = Path(occupying_status["archive_dir"])
        occupied_hashes = archive_artifact_hashes(occupied_target)

        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, hidden, attempt_no=1)
        journal = (
            self.root
            / ".workbench-pending-journal"
            / f"{job_id}-attempt-1.json"
        )
        journal.parent.mkdir(exist_ok=True)
        journal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "attempt": 1,
                    "source": str(hidden),
                    "target": str(occupied_target),
                    "state": "old_moved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        summary = pending_control.reconcile_pending_archives()
        current = pending_control.status(job_id)
        recovered = Path(current["archive_dir"])

        self.assertTrue(summary["ok"], summary)
        self.assertIn(job_id, summary["promoted"])
        self.assertEqual(self.root.resolve(), recovered.parent.resolve())
        self.assertNotEqual(occupied_target.resolve(), recovered.resolve())
        self.assertIn(self.audio.stem, recovered.name)
        self.assertEqual(occupied_hashes, archive_artifact_hashes(occupied_target))
        self.assertEqual(
            occupying_job,
            json.loads(
                (occupied_target / "workbench-manifest.json").read_text(
                    encoding="utf-8"
                )
            )["job_id"],
        )
        self.assertFalse(hidden.exists())
        self.assertFalse(journal.exists())

    def test_complete_minutes_rejects_boolean_and_float_manifest_versions(self):
        cases = (
            ("schema-bool", "schema_version", True),
            ("attempt-bool", "attempt", True),
            ("attempt-float", "attempt", 1.0),
        )
        for index, (label, field, invalid_value) in enumerate(cases, start=1):
            with self.subTest(label=label):
                audio = self.root / f"vm-20260710-13000{index}-TYPE{index}.m4a"
                audio.write_bytes(b"audio")
                control = self.module.RelayControl(
                    self.root / f"{label}.sqlite3",
                    archive_root=self.root,
                    auto_pending_archive=False,
                )
                job_id = self._advance_to_minutes(control, audio)
                archive = create_complete_archive(
                    self.root, job_id, stem=audio.stem
                )
                manifest_path = archive / "workbench-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[field] = invalid_value
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
                )

                with self.assertRaises(self.module.ArtifactValidationError):
                    control.complete_minutes(job_id, archive)

                current = control.status(job_id)
                self.assertEqual("failed", current["status"])
                self.assertIsNone(current["archive_dir"])
                self.assertFalse((self.root / "待校对").exists())

    def test_visible_pending_can_publish_then_moves_source_to_recovery(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        manifest = create_published_archive(self.root, pending, job_id)

        published = control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        self.assertEqual("published", published["status"])
        self.assertFalse(pending.exists())
        recovered = list(
            (
                self.root
                / ".workbench-recovery"
                / "published"
                / job_id
            ).glob("attempt-1-*")
        )
        self.assertEqual(1, len(recovered))

    def test_first_publish_ack_can_reuse_top_level_managed_directory(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        working = Path(completed["archive_dir"])
        self.assertEqual(self.root.resolve(), working.parent.resolve())
        manifest_path = working / "workbench-manifest.json"
        audio = next(
            path
            for path in working.iterdir()
            if path.suffix.lower() in {".m4a", ".mp3", ".wav"}
        )
        artifact_paths = sorted(
            str(path.relative_to(working))
            for path in working.rglob("*")
            if path.is_file()
            and path.name != "workbench-manifest.json"
            and ".workbench-history" not in path.parts
            and not path.name.startswith("._")
        )
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "minutes_protocol_version": 3,
                    "job_id": job_id,
                    "attempt": 1,
                    "meeting_id": "vm-20260710-120000-ABC",
                    "status": "published",
                    "original_audio": {
                        "path": audio.name,
                        "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                    },
                    "artifacts": [
                        {
                            "path": relative,
                            "bytes": (working / relative).stat().st_size,
                            "sha256": hashlib.sha256(
                                (working / relative).read_bytes()
                            ).hexdigest(),
                        }
                        for relative in artifact_paths
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        published = control.mark_published(
            job_id, manifest_path, "vm-20260710-120000-ABC"
        )

        self.assertEqual("published", published["status"])
        self.assertEqual(str(working.resolve()), published["archive_dir"])
        self.assertTrue(working.is_dir())
        self.assertEqual([], list((self.root / ".workbench-recovery" / "published").glob("**/*")))

    def test_reprocess_cannot_publish_a_second_top_level_directory(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        first_hidden = create_complete_archive(self.root, job_id, attempt=1)
        first_completed = control.complete_minutes(
            job_id, first_hidden, attempt_no=1
        )
        first_directory = Path(first_completed["archive_dir"])
        first_manifest = rewrite_archive_as_published(first_directory, job_id)
        control.mark_published(
            job_id, first_manifest, "vm-20260710-120000-ABC"
        )

        attempt = control.retry(
            job_id,
            "minutes_generating",
            transcript_path=self.input_transcript,
        )
        control.claim_next(worker_id="worker-reprocess-flat")
        second_hidden = create_minutes_only_archive(
            self.root, job_id, attempt=attempt
        )
        second_completed = control.complete_minutes(
            job_id, second_hidden, attempt_no=attempt
        )
        second_directory = Path(second_completed["archive_dir"])
        second_manifest = rewrite_archive_as_published(second_directory, job_id)

        with self.assertRaises(self.module.PublishValidationError):
            control.mark_published(
                job_id, second_manifest, "vm-20260710-120000-ABC"
            )

        current = control.status(job_id)
        self.assertTrue(first_directory.is_dir())
        self.assertTrue(second_directory.is_dir())
        self.assertEqual(str(first_directory.resolve()), current["published_archive_dir"])
        self.assertEqual(str(second_directory.resolve()), current["archive_dir"])
        self.assertEqual("completed_unreviewed", current["status"])

    def test_published_replay_recovers_pending_left_after_commit_crash(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        manifest = create_published_archive(self.root, pending, job_id)

        with patch.object(
            control,
            "_recover_published_pending_source",
            side_effect=KeyboardInterrupt("cleanup interrupted"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                control.mark_published(
                    job_id, manifest, "vm-20260710-120000-ABC"
                )

        committed = control.status(job_id)
        published_snapshot = committed["published_snapshot"].copy()
        self.assertEqual("published", committed["status"])
        self.assertTrue(pending.is_dir())

        replayed = control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        self.assertEqual(published_snapshot, replayed["published_snapshot"])
        self.assertFalse(pending.exists())
        recovered = list(
            (
                self.root
                / ".workbench-recovery"
                / "published"
                / job_id
            ).glob("attempt-1-*")
        )
        self.assertEqual(1, len(recovered))

    def test_periodic_reconcile_recovers_published_pending_left_after_commit_crash(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        manifest = create_published_archive(self.root, pending, job_id)

        with patch.object(
            control,
            "_recover_published_pending_source",
            side_effect=KeyboardInterrupt("cleanup interrupted"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                control.mark_published(
                    job_id, manifest, "vm-20260710-120000-ABC"
                )
        published_snapshot = control.status(job_id)["published_snapshot"].copy()

        summary = control.reconcile_pending_archives()
        reconciled = control.status(job_id)

        self.assertTrue(summary["ok"], summary)
        self.assertIn(job_id, summary["published_pending_recovered"])
        self.assertEqual(published_snapshot, reconciled["published_snapshot"])
        self.assertFalse(pending.exists())

    def test_periodic_reconcile_preserves_pending_when_published_snapshot_is_damaged(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        manifest = create_published_archive(self.root, pending, job_id)

        with patch.object(
            control,
            "_recover_published_pending_source",
            side_effect=KeyboardInterrupt("cleanup interrupted"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                control.mark_published(
                    job_id, manifest, "vm-20260710-120000-ABC"
                )
        stored_snapshot = control.status(job_id)["published_snapshot"].copy()
        manifest.write_text("{}", encoding="utf-8")

        summary = control.reconcile_pending_archives()
        current = control.status(job_id)

        self.assertFalse(summary["ok"], summary)
        self.assertTrue(
            any(
                item.get("stage") == "published_pending_cleanup"
                for item in summary["errors"]
            ),
            summary,
        )
        self.assertTrue(pending.is_dir())
        self.assertEqual(stored_snapshot, current["published_snapshot"])

    def test_pending_promotion_failure_is_degraded_and_replay_recovers_once(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)

        with patch.object(
            control,
            "_promote_pending_archive",
            side_effect=OSError("pending volume write failed"),
        ):
            with self.assertRaises(OSError):
                control.complete_minutes(job_id, hidden, attempt_no=1)

        degraded = control.status(job_id)
        self.assertEqual("completed_unreviewed", degraded["status"])
        self.assertTrue(degraded["degraded"])
        self.assertEqual("pending_archive", degraded["failure_stage"])
        self.assertIn("pending volume write failed", degraded["last_error"])
        health = control.health(control_enabled=True)
        self.assertEqual(1, health["counts"]["pending_archive_failures"])
        self.assertNotEqual("healthy", health["status"])

        recovered = control.complete_minutes(job_id, hidden, attempt_no=1)

        self.assertEqual("completed_unreviewed", recovered["status"])
        self.assertFalse(recovered["degraded"])
        self.assertIsNone(recovered["failure_stage"])
        self.assertEqual(
            self.root.resolve(),
            Path(recovered["archive_dir"]).parent.resolve(),
        )
        self.assertEqual(
            1,
            len(
                [
                    event
                    for event in recovered["events"]
                    if event["event_type"] == "minutes_completed"
                ]
            ),
        )

    def test_background_pending_promotion_failure_is_degraded_and_reconcile_recovers(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, hidden, attempt_no=1)
        control = self._pending_control()

        with patch.object(
            control,
            "_promote_pending_archive",
            side_effect=OSError("background promotion failed"),
        ):
            failed_summary = control.reconcile_pending_archives()

        degraded = control.status(job_id)
        self.assertFalse(failed_summary["ok"], failed_summary)
        self.assertTrue(degraded["degraded"])
        self.assertEqual("pending_archive", degraded["failure_stage"])

        recovered_summary = control.reconcile_pending_archives()
        recovered = control.status(job_id)

        self.assertTrue(recovered_summary["ok"], recovered_summary)
        self.assertIn(job_id, recovered_summary["promoted"])
        self.assertFalse(recovered["degraded"])
        self.assertIsNone(recovered["failure_stage"])
        control._record_pending_archive_failure(
            job_id, 1, OSError("cleanup after db commit failed")
        )
        self.assertTrue(control.status(job_id)["degraded"])

        already_pending = control.reconcile_pending_archives()

        self.assertTrue(already_pending["ok"], already_pending)
        self.assertIn(job_id, already_pending["already_pending"])
        self.assertFalse(control.status(job_id)["degraded"])

    def test_failed_publish_ack_preserves_visible_pending(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        pending = Path(completed["archive_dir"])
        manifest = create_published_archive(self.root, pending, job_id)

        with self.assertRaises(self.module.PublishValidationError):
            control.mark_published(job_id, manifest, "different-meeting")

        self.assertTrue(pending.is_dir())
        self.assertEqual(
            str(pending.resolve()), control.status(job_id)["archive_dir"]
        )

    def test_complete_minutes_rejects_main_artifact_changed_after_validation(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)
        original_validate = self.control.validate_archive

        def validate_then_mutate(*args, **kwargs):
            report = original_validate(*args, **kwargs)
            (archive / "测试会议.md").write_text("# 校验后改写", encoding="utf-8")
            return report

        with patch.object(
            self.control, "validate_archive", side_effect=validate_then_mutate
        ):
            with self.assertRaises(self.module.PublishValidationError):
                self.control.complete_minutes(job_id, archive)

        self.assertEqual("minutes_generating", self.control.status(job_id)["status"])

    def test_stale_invalid_callback_cannot_fail_a_new_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        invalid = self.root / ".workbench-drafts" / job_id / "attempt-1"
        invalid.mkdir(parents=True, exist_ok=True)
        original_validate = self.control.validate_archive

        def validate_then_retry(*args, **kwargs):
            report = original_validate(*args, **kwargs)
            self.control.fail(job_id, "minutes_generating", "simulated timeout")
            self.control.retry(job_id, "transcribing")
            return report

        with patch.object(
            self.control, "validate_archive", side_effect=validate_then_retry
        ):
            with self.assertRaises(self.module.InvalidTransitionError):
                self.control.complete_minutes(job_id, invalid, attempt_no=1)

        current = self.control.status(job_id)
        self.assertEqual(2, current["current_attempt"])
        self.assertEqual("queued", current["status"])
        self.assertEqual("queued", current["attempts"][-1]["status"])

    def test_complete_minutes_rejects_parent_replaced_by_external_symlink(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)
        original_capture = self.control._capture_completion_snapshot
        outside = self.root.parent / f"outside-{job_id}"
        displaced = archive.parent.with_name(f"{job_id}-displaced")

        def capture_then_replace(report):
            snapshot = original_capture(report)
            shutil.copytree(archive.parent, outside)
            archive.parent.rename(displaced)
            archive.parent.symlink_to(outside, target_is_directory=True)
            return snapshot

        try:
            with patch.object(
                self.control,
                "_capture_completion_snapshot",
                side_effect=capture_then_replace,
            ):
                with self.assertRaises(self.module.PublishValidationError):
                    self.control.complete_minutes(job_id, archive)
            self.assertEqual("minutes_generating", self.control.status(job_id)["status"])
        finally:
            if archive.parent.is_symlink():
                archive.parent.unlink()
            if displaced.exists():
                displaced.rename(archive.parent)
            shutil.rmtree(outside, ignore_errors=True)

    def test_complete_minutes_does_not_block_when_whisper_is_absent(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual("pending", result["substates"]["whisper"]["status"])

    def test_partial_whisper_files_do_not_block_main_completion(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        whisper = archive / "whisper-ref"
        whisper.mkdir()
        (whisper / "partial.json").write_text("", encoding="utf-8")

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual("running", result["substates"]["whisper"]["status"])

    def test_wrong_whisper_stem_is_failed_substate_without_blocking_completion(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)
        whisper = archive / "whisper-ref"
        for suffix in ("json", "tsv", "srt", "txt", "vtt"):
            original = next(whisper.glob(f"*.{suffix}"))
            original.rename(whisper / f"vm-20990101-000000-WRONG.{suffix}")
        refresh_published_manifest(archive / "workbench-manifest.json")

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual("failed", result["substates"]["whisper"]["status"])

    def test_whisper_and_index_substates_are_visible_and_retryable(self):
        job_id = self.control.enqueue(self.audio)
        initial = self.control.status(job_id)
        self.assertEqual("pending", initial["substates"]["whisper"]["status"])
        self.assertEqual("absent", initial["substates"]["index"]["status"])

        self.control.record_substate(job_id, "whisper", "running")
        self.control.record_substate(
            job_id, "whisper", "failed", error="reference engine failed"
        )
        retried = self.control.retry_substate(job_id, "whisper")
        self.control.record_substate(job_id, "index", "running")
        ready = self.control.record_substate(job_id, "index", "ready")

        self.assertEqual("pending", retried["substates"]["whisper"]["status"])
        self.assertIsNone(retried["substates"]["whisper"]["error"])
        self.assertTrue(retried["substates"]["whisper"]["retry_requested"])
        self.assertEqual("ready", ready["substates"]["index"]["status"])

    def test_substate_rejects_zero_attempt_instead_of_falling_back_to_current(self):
        job_id = self.control.enqueue(self.audio)

        with self.assertRaises(self.module.RelayControlError):
            self.control.record_substate(job_id, "index", "ready", attempt_no=0)

        self.assertEqual(
            "absent", self.control.status(job_id)["substates"]["index"]["status"]
        )

    def test_whisper_retry_claim_is_single_consumer_and_generation_cas_completes(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        self.control.complete_minutes(job_id, archive)
        self.control.record_substate(job_id, "whisper", "failed", error="engine")
        requested = self.control.retry_substate(job_id, "whisper")

        claim = self.control.claim_whisper_retry(worker_id="worker-123456")
        second_claim = self.control.claim_whisper_retry(worker_id="worker-654321")
        staging = create_whisper_staging(self.root)
        completed = self.control.finish_whisper_retry(
            job_id,
            attempt_no=claim["attempt"],
            generation=claim["generation"],
            worker_id=claim["worker_id"],
            success=True,
            artifact_dir=staging,
        )

        self.assertEqual(1, requested["substates"]["whisper"]["retry_generation"])
        self.assertTrue(requested["substates"]["whisper"]["retry_requested"])
        self.assertEqual(str(archive.resolve()), claim["target_archive_dir"])
        self.assertEqual("running", claim["status"])
        self.assertIsNone(second_claim)
        self.assertEqual("ready", completed["substates"]["whisper"]["status"])
        self.assertFalse(completed["substates"]["whisper"]["retry_requested"])

    def test_whisper_retry_rejects_stale_generation_after_new_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        self.control.complete_minutes(job_id, archive)
        self.control.record_substate(job_id, "whisper", "failed", error="engine")
        self.control.retry_substate(job_id, "whisper")
        claim = self.control.claim_whisper_retry(worker_id="worker-123456")
        self.control.retry(job_id, "transcribing")
        staging = create_whisper_staging(self.root)

        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.finish_whisper_retry(
                job_id,
                attempt_no=claim["attempt"],
                generation=claim["generation"],
                worker_id=claim["worker_id"],
                success=True,
                artifact_dir=staging,
            )

        status = self.control.status(job_id)
        self.assertEqual(2, status["current_attempt"])
        self.assertEqual("pending", status["substates"]["whisper"]["status"])

    def test_whisper_retry_failure_is_explicit_and_retryable_again(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        self.control.complete_minutes(job_id, archive)
        self.control.record_substate(job_id, "whisper", "failed", error="engine")
        self.control.retry_substate(job_id, "whisper")
        claim = self.control.claim_whisper_retry(worker_id="worker-123456")

        failed = self.control.finish_whisper_retry(
            job_id,
            attempt_no=claim["attempt"],
            generation=claim["generation"],
            worker_id=claim["worker_id"],
            success=False,
            error="whisper_process_failed",
        )
        retried = self.control.retry_substate(job_id, "whisper")

        self.assertEqual("failed", failed["substates"]["whisper"]["status"])
        self.assertEqual(
            "whisper_process_failed", failed["substates"]["whisper"]["error"]
        )
        self.assertEqual(2, retried["substates"]["whisper"]["retry_generation"])

    def test_whisper_retry_cannot_report_ready_without_validated_artifacts(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        self.control.complete_minutes(job_id, archive)
        self.control.record_substate(job_id, "whisper", "failed", error="engine")
        self.control.retry_substate(job_id, "whisper")
        claim = self.control.claim_whisper_retry(worker_id="worker-123456")

        with self.assertRaises(self.module.RelayControlError):
            self.control.finish_whisper_retry(
                job_id,
                attempt_no=claim["attempt"],
                generation=claim["generation"],
                worker_id=claim["worker_id"],
                success=True,
            )

        self.assertEqual(
            "running", self.control.status(job_id)["substates"]["whisper"]["status"]
        )

    def test_whisper_backup_cleanup_failure_cannot_undo_committed_ready_state(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, archive)
        self.control.record_substate(job_id, "whisper", "failed", error="engine")
        self.control.retry_substate(job_id, "whisper")
        claim = self.control.claim_whisper_retry(worker_id="worker-123456")
        staging = create_whisper_staging(self.root)

        with patch.object(self.module.shutil, "rmtree", side_effect=OSError("cleanup")):
            completed = self.control.finish_whisper_retry(
                job_id,
                attempt_no=claim["attempt"],
                generation=claim["generation"],
                worker_id=claim["worker_id"],
                success=True,
                artifact_dir=staging,
            )

        self.assertEqual("ready", completed["substates"]["whisper"]["status"])
        self.assertEqual(
            "ready", self.module.inspect_whisper_ref(archive)["status"]
        )

    def test_retranscription_resets_substates_and_rejects_stale_worker_callback(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_substate(job_id, "whisper", "ready", attempt_no=1)
        self.control.record_substate(job_id, "index", "ready", attempt_no=1)
        self.control.fail(job_id, "transcribing", "retry")

        attempt = self.control.retry(job_id, "transcribing")

        status = self.control.status(job_id)
        self.assertEqual(2, attempt)
        self.assertEqual("pending", status["substates"]["whisper"]["status"])
        self.assertEqual("pending", status["substates"]["index"]["status"])
        self.assertEqual(2, status["substates"]["whisper"]["attempt"])
        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.record_substate(
                job_id, "whisper", "failed", attempt_no=1
            )
        self.assertEqual(
            "pending", self.control.status(job_id)["substates"]["whisper"]["status"]
        )

    def test_publish_whisper_inspection_stays_strict(self):
        job_id = self.control.enqueue(self.audio)
        incomplete = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        complete = create_complete_archive(
            self.root, "job-whisper-complete", include_whisper=True
        )

        incomplete_result = self.module.inspect_whisper_ref(incomplete)
        complete_result = self.module.inspect_whisper_ref(complete)

        self.assertNotEqual("ready", incomplete_result["status"])
        self.assertEqual("ready", complete_result["status"])

    def test_whisper_inspection_rejects_malformed_json_and_mixed_stems(self):
        malformed = create_complete_archive(self.root, "job-malformed")
        whisper = malformed / "whisper-ref"
        next(whisper.glob("*.json")).write_text("not-json", encoding="utf-8")
        mixed = create_complete_archive(self.root, "job-mixed")
        mixed_whisper = mixed / "whisper-ref"
        (mixed_whisper / "vm-20260710-120000-ABC.tsv").rename(
            mixed_whisper / "another.tsv"
        )

        self.assertEqual("failed", self.module.inspect_whisper_ref(malformed)["status"])
        self.assertNotEqual("ready", self.module.inspect_whisper_ref(mixed)["status"])

    def test_whisper_inspection_ignores_appledouble_sidecars(self):
        archive = create_complete_archive(self.root, "job-appledouble")
        whisper = archive / "whisper-ref"
        for path in list(whisper.iterdir()):
            (whisper / f"._{path.name}").write_bytes(b"appledouble")

        result = self.module.inspect_whisper_ref(archive)

        self.assertEqual("ready", result["status"])
        self.assertFalse(any("/._" in path for path in result["files"]))

    def test_whisper_inspection_error_does_not_block_main_completion(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id, include_whisper=False)

        with patch.object(
            self.module, "inspect_whisper_ref", side_effect=OSError("disk race")
        ):
            result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual("failed", result["substates"]["whisper"]["status"])

    def test_unsafe_whisper_symlink_is_failed_substate_not_main_chain_failure(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id, include_whisper=False)
        outside = self.root / "outside-whisper"
        outside.mkdir()
        (archive / "whisper-ref").symlink_to(outside, target_is_directory=True)

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual("failed", result["substates"]["whisper"]["status"])

    def test_mark_published_validates_manifest_and_is_idempotent(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)

        first = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )
        second = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        self.assertEqual("published", first["status"])
        self.assertEqual("vm-20260710-120000-ABC", first["meeting_id"])
        self.assertEqual(str(manifest.parent.resolve()), first["archive_dir"])
        self.assertEqual(str(manifest.resolve()), first["published_manifest_path"])
        self.assertEqual(first["published_manifest_sha256"], second["published_manifest_sha256"])
        published_events = [
            event for event in second["events"] if event["event_type"] == "job_published"
        ]
        self.assertEqual(1, len(published_events))

    def test_published_idempotent_ack_rejects_rewritten_manifest(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        published = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )
        original_sha = published["published_manifest_sha256"]
        (manifest.parent / "测试会议.md").write_text("# 发布后被改写", encoding="utf-8")
        refresh_published_manifest(manifest)

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

        current = self.control.status(job_id)
        self.assertEqual("published", current["status"])
        self.assertEqual(original_sha, current["published_manifest_sha256"])

    def test_publish_snapshot_rejects_late_added_symlink(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        with self.control._connect() as connection:
            row = self.control._job_row(connection, job_id)
        validation = self.control._validate_publish_manifest(
            row, manifest, "vm-20260710-120000-ABC"
        )
        (manifest.parent / "late-link").symlink_to(manifest.parent / "测试会议.md")

        with self.assertRaises(self.module.PublishValidationError):
            self.control._assert_publish_snapshot_unchanged(validation)

    def test_mark_published_rechecks_files_immediately_before_commit(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        original_assert = self.control._assert_publish_snapshot_unchanged
        calls = 0

        def mutate_after_first_assert(validation):
            nonlocal calls
            original_assert(validation)
            calls += 1
            if calls == 1:
                (manifest.parent / "测试会议.md").write_text(
                    "# commit 前被改写", encoding="utf-8"
                )

        with patch.object(
            self.control,
            "_assert_publish_snapshot_unchanged",
            side_effect=mutate_after_first_assert,
        ):
            with self.assertRaises(self.module.PublishValidationError):
                self.control.mark_published(
                    job_id, manifest, "vm-20260710-120000-ABC"
                )

        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_mark_published_fails_closed_when_files_change_after_commit(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        original_assert = self.control._assert_publish_snapshot_unchanged
        calls = 0

        def mutate_after_precommit_assert(validation):
            nonlocal calls
            original_assert(validation)
            calls += 1
            if calls == 2:
                (manifest.parent / "测试会议.md").write_text(
                    "# SQLite 提交后被改写", encoding="utf-8"
                )

        with patch.object(
            self.control,
            "_assert_publish_snapshot_unchanged",
            side_effect=mutate_after_precommit_assert,
        ):
            with self.assertRaises(self.module.PublishValidationError):
                self.control.mark_published(
                    job_id, manifest, "vm-20260710-120000-ABC"
                )

        current = self.control.status(job_id)
        self.assertEqual("failed", current["status"])
        self.assertEqual("publish_post_commit_validation", current["failure_stage"])

    def test_mark_published_rejects_symlink_inside_history(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        history = manifest.parent / ".workbench-history"
        history.mkdir()
        (history / "old.md").write_text("# 历史版本", encoding="utf-8")
        (history / "outside-link").symlink_to(manifest.parent / "测试会议.md")

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

    def test_mark_published_rejects_special_file_inside_history(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        history = manifest.parent / ".workbench-history"
        history.mkdir()
        os.mkfifo(history / "unexpected.fifo")

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

    def test_mark_published_ignores_ds_store_but_keeps_it_out_of_manifest(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        (manifest.parent / ".DS_Store").write_bytes(b"finder metadata")
        nested = manifest.parent / "metadata"
        nested.mkdir()
        (nested / ".DS_Store").write_bytes(b"nested finder metadata")

        published = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        self.assertEqual("published", published["status"])
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertFalse(
            any(".DS_Store" in item["path"] for item in loaded["artifacts"])
        )

    def test_mark_published_rejects_hidden_appledouble_symlink(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        (manifest.parent / "._outside").symlink_to(manifest.parent / "测试会议.md")

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_mark_published_rejects_tampered_artifact_without_state_change(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        (manifest.parent / "测试会议.md").write_text("被篡改", encoding="utf-8")

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_mark_published_requires_complete_whisper_reference(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id, include_whisper=False)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)

        with self.assertRaises(self.module.PublishValidationError) as caught:
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

        self.assertIn("whisper", str(caught.exception).lower())
        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_minutes_only_publish_allows_historical_archive_without_engine_sources(self):
        job_id = self.control.enqueue(
            self.audio,
            requested_stage="minutes_generating",
            transcript_path=self.input_transcript,
        )
        claim = self.control.claim_next(worker_id="worker-historical-minutes")
        draft = create_minutes_only_archive(self.root, job_id, attempt=1)
        self.control.complete_minutes(job_id, draft, attempt_no=1)
        manifest = create_minutes_only_published_archive(
            self.root,
            job_id,
            claim["input_transcript_sha256"],
        )

        published = self.control.mark_published(job_id, manifest, "legacy-meeting")

        self.assertEqual("published", published["status"])
        self.assertEqual("legacy-meeting", published["meeting_id"])

    def test_minutes_only_publish_requires_current_attempt_snapshot_provenance(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_minutes_only_published_archive(
            self.root,
            job_id,
            hashlib.sha256(self.input_transcript.read_bytes()).hexdigest(),
            directory_name="260710 伪造历史会议修订",
        )

        with self.assertRaises(self.module.PublishValidationError) as caught:
            self.control.mark_published(job_id, manifest, "legacy-meeting")

        self.assertIn("来源", str(caught.exception))

    def test_mark_published_rejects_complete_whisper_from_another_recording(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        whisper = manifest.parent / "whisper-ref"
        for suffix in ("json", "tsv", "srt", "txt", "vtt"):
            original = next(whisper.glob(f"*.{suffix}"))
            original.rename(whisper / f"vm-20990101-000000-WRONG.{suffix}")
        refresh_published_manifest(manifest)

        with self.assertRaises(self.module.PublishValidationError) as caught:
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

        self.assertIn("原音频", str(caught.exception))
        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_mark_published_accepts_fingerprint_meeting_id_for_matching_audio(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(
            self.root,
            draft,
            job_id,
            meeting_id="fp-0123456789abcdef01234567",
        )

        result = self.control.mark_published(
            job_id, manifest, "fp-0123456789abcdef01234567"
        )

        self.assertEqual("published", result["status"])
        self.assertEqual("fp-0123456789abcdef01234567", result["meeting_id"])

    def test_mark_published_allows_known_16k_pipeline_suffix(self):
        audio = self.root / "客户讨论.16k.wav"
        audio.write_bytes(b"audio")
        job_id = self.control.enqueue(audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(
            self.root,
            job_id,
            stem="客户讨论.16k",
            audio_suffix=".wav",
            whisper_stem="客户讨论",
        )
        completed = self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(
            self.root,
            draft,
            job_id,
            meeting_id="legacy-customer-session",
        )

        result = self.control.mark_published(
            job_id, manifest, "legacy-customer-session"
        )

        self.assertEqual("ready", completed["substates"]["whisper"]["status"])
        self.assertEqual("published", result["status"])

    def test_completion_accepts_whisper_bound_to_hash_matching_renamed_audio(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root,
            job_id,
            stem="客户会谈归档",
            audio_suffix=".wav",
            whisper_stem="客户会谈归档",
        )

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("ready", result["substates"]["whisper"]["status"])

    def test_mark_published_closes_late_whisper_substate_as_ready(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id, include_whisper=False)
        completed = self.control.complete_minutes(job_id, draft)
        reference = create_complete_archive(self.root, "job-reference-whisper")
        shutil.copytree(reference / "whisper-ref", draft / "whisper-ref")
        manifest = create_published_archive(self.root, draft, job_id)

        published = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        self.assertEqual("pending", completed["substates"]["whisper"]["status"])
        self.assertEqual("ready", published["substates"]["whisper"]["status"])
        self.assertEqual(
            published["current_attempt"],
            published["substates"]["whisper"]["attempt"],
        )

    def test_mark_published_rejects_manifest_outside_archive_root(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        outside_root = self.root.parent / f"outside-{job_id}"
        manifest = create_published_archive(outside_root, draft, job_id)

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, manifest, "vm-20260710-120000-ABC"
            )

        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_mark_published_never_promotes_the_hidden_draft_in_place(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        published_manifest = create_published_archive(self.root, draft, job_id)
        draft_manifest = draft / "workbench-manifest.json"
        shutil.copy2(published_manifest, draft_manifest)

        with self.assertRaises(self.module.PublishValidationError):
            self.control.mark_published(
                job_id, draft_manifest, "vm-20260710-120000-ABC"
            )

        self.assertEqual("completed_unreviewed", self.control.status(job_id)["status"])

    def test_mark_draft_modified_is_idempotent_and_preserves_publish_snapshot(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        draft = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, draft)
        manifest = create_published_archive(self.root, draft, job_id)
        published = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        first = self.control.mark_draft_modified(job_id)
        second = self.control.mark_draft_modified(job_id)
        republished = self.control.mark_published(
            job_id, manifest, "vm-20260710-120000-ABC"
        )

        self.assertEqual("draft_modified", first["status"])
        self.assertEqual("draft_modified", second["status"])
        self.assertEqual(
            published["published_manifest_sha256"],
            second["published_manifest_sha256"],
        )
        transitions = [
            event
            for event in second["events"]
            if event["event_type"] == "draft_modified"
        ]
        self.assertEqual(1, len(transitions))
        self.assertEqual("published", republished["status"])

    def test_published_draft_rejects_whisper_only_retry_without_new_attempt(self):
        control = self._pending_control()
        job_id = self._advance_to_minutes(control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        completed = control.complete_minutes(job_id, hidden, attempt_no=1)
        archive = Path(completed["archive_dir"])
        manifest = rewrite_archive_as_published(archive, job_id)
        control.mark_published(job_id, manifest, "vm-20260710-120000-ABC")
        control.mark_draft_modified(job_id)
        control.record_substate(
            job_id, "whisper", "failed", attempt_no=1, error="engine"
        )

        with self.assertRaises(self.module.InvalidTransitionError):
            control.retry_substate(job_id, "whisper")

        whisper = control.status(job_id)["substates"]["whisper"]
        self.assertEqual("failed", whisper["status"])
        self.assertFalse(whisper["retry_requested"])

    def test_mark_draft_modified_accepts_unreviewed_and_rejects_active_job(self):
        active_job = self.control.enqueue(self.audio)
        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.mark_draft_modified(active_job)

        second_audio = self.root / "vm-20260710-130000-DEF.m4a"
        second_audio.write_bytes(b"audio")
        completed_job = self.control.enqueue(second_audio)
        self.control.record_stage(completed_job, "transcribing")
        self.control.record_stage(completed_job, "transcript_ready")
        self.control.record_stage(completed_job, "minutes_generating")
        archive = create_complete_archive(
            self.root, completed_job, stem="vm-20260710-130000-DEF"
        )
        result = self.control.complete_minutes(completed_job, archive)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual(
            "draft_modified", self.control.mark_draft_modified(completed_job)["status"]
        )

    def test_stop_after_stage_keeps_minutes_artifacts_but_terminal_is_interrupted(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        self.control.stop_after_stage(job_id)
        archive = create_complete_archive(self.root, job_id)

        result = self.control.complete_minutes(job_id, archive)

        self.assertEqual("interrupted", result["status"])
        self.assertIn("workbench-manifest.json", result["artifacts"])

    def test_complete_minutes_fails_closed_when_artifacts_are_missing(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = self.root / "incomplete"
        archive.mkdir()
        (archive / "some.txt").write_text("not enough", encoding="utf-8")

        with self.assertRaises(self.module.ArtifactValidationError) as caught:
            self.control.complete_minutes(job_id, archive)

        self.assertIn("audio", str(caught.exception))
        status = self.control.status(job_id)
        self.assertEqual("failed", status["status"])
        self.assertEqual("archive_validation", status["failure_stage"])

    def test_completion_callback_can_recover_after_artifacts_are_repaired(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        incomplete = self.root / "incomplete"
        incomplete.mkdir()

        with self.assertRaises(self.module.ArtifactValidationError):
            self.control.complete_minutes(job_id, incomplete)

        complete = create_complete_archive(self.root, job_id)
        result = self.control.complete_minutes(job_id, complete)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual(1, result["current_attempt"])

    def test_late_completion_callback_recovers_same_attempt_after_handoff_timeout(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        self.control.record_codex_dispatched(job_id)
        self.control.reconcile_codex_handoffs(grace_seconds=0)
        archive = create_complete_archive(self.root, job_id)

        result = self.control.complete_minutes(job_id, archive, attempt_no=1)

        self.assertEqual("completed_unreviewed", result["status"])
        self.assertEqual(1, result["current_attempt"])
        self.assertIn(
            "codex_callback_recovered",
            [event["event_type"] for event in result["events"]],
        )

    def test_manifest_must_match_job_and_list_required_files(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, "job-wrong")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("manifest_job_id", report.missing)

    def test_manifest_schema_version_is_required(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        manifest_path = archive / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = 99
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("manifest_schema_version", report.missing)

    def test_archive_audio_must_match_enqueued_source_hash(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        archived_audio = next(archive.glob("*.m4a"))
        archived_audio.write_bytes(b"different audio")
        manifest_path = archive / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == archived_audio.name:
                artifact["bytes"] = archived_audio.stat().st_size
                artifact["sha256"] = hashlib.sha256(archived_audio.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("audio_hash_mismatch", report.missing)

    def test_source_hash_is_frozen_after_first_stable_capture(self):
        job_id = self.control.enqueue(self.audio)
        original_hash = self.control.status(job_id)["audio_sha256"]
        self.audio.write_bytes(b"changed audio")

        result = self.control.record_source_audio(job_id, self.audio)

        self.assertEqual("failed", result["status"])
        self.assertEqual("source_audio_validation", result["failure_stage"])
        self.assertEqual(original_hash, result["audio_sha256"])

    def test_completion_requires_source_hash_even_with_consistent_manifest(self):
        job_id = self.control.enqueue(self.audio, compute_hash=False)
        archive = create_complete_archive(self.root, job_id)

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("source_audio_hash", report.missing)

    def test_empty_required_artifact_is_rejected(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        transcript = next(
            path
            for path in archive.glob("*.txt")
            if not path.name.endswith("spk.txt")
        )
        transcript.write_bytes(b"")
        manifest_path = archive / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == transcript.name:
                artifact["bytes"] = 0
                artifact["sha256"] = hashlib.sha256(b"").hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("empty_artifacts", report.missing)

    def test_required_json_artifact_must_parse(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        funasr = next(archive.glob("*.funasr.json"))
        funasr.write_text("not-json", encoding="utf-8")
        manifest_path = archive / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == funasr.name:
                artifact["bytes"] = funasr.stat().st_size
                artifact["sha256"] = hashlib.sha256(funasr.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("invalid_json_artifacts", report.missing)

    def test_manifest_must_cover_every_physical_artifact(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        (archive / "extra.bin").write_bytes(b"extra")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("manifest_required_artifacts", report.missing)

    def test_manifest_rejects_absolute_paths_and_bad_hashes(self):
        job_id = self.control.enqueue(self.audio)
        archive = create_complete_archive(self.root, job_id)
        manifest_path = archive / "workbench-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"][0]["path"] = str(
            (archive / manifest["artifacts"][0]["path"]).resolve()
        )
        manifest["artifacts"][1]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.control.validate_archive(archive, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("manifest_artifacts", report.missing)
        self.assertIn("manifest_artifact_hashes", report.missing)

    def test_completion_rejects_directory_outside_canonical_archive_root(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            report = self.control.validate_archive(outside_dir, job_id="job-any")

        self.assertFalse(report.valid)
        self.assertIn("archive_outside_root", report.missing)

    def test_completion_rejects_formal_directory_outside_attempt_draft(self):
        job_id = self.control.enqueue(self.audio)
        formal = self.root / "260710 正式会议"
        formal.mkdir()

        report = self.control.validate_archive(formal, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("archive_not_current_attempt_draft", report.missing)

    def test_completion_rejects_attempt_directory_symlinked_to_formal_archive(self):
        job_id = self.control.enqueue(self.audio)
        draft = create_complete_archive(self.root, job_id)
        formal = self.root / "260710 正式会议"
        draft.rename(formal)
        draft.symlink_to(formal, target_is_directory=True)

        report = self.control.validate_archive(draft, job_id=job_id)

        self.assertFalse(report.valid)
        self.assertIn("archive_symlink", report.missing)

    def test_stale_attempt_callback_cannot_complete_new_attempt(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        self.control.fail(job_id, "minutes_generating", "Codex interrupted")
        self.control.retry(
            job_id, "minutes_generating", transcript_path=self.input_transcript
        )
        self.control.claim_next(worker_id="worker-attempt-2")
        stale_archive = create_complete_archive(self.root, job_id, attempt=1)

        with self.assertRaises(self.module.InvalidTransitionError):
            self.control.complete_minutes(job_id, stale_archive, attempt_no=1)

        self.assertEqual("minutes_generating", self.control.status(job_id)["status"])

    def test_cli_status_json_is_machine_readable(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(["enqueue", str(self.audio)])
            self.assertEqual(0, exit_code)
            job_id = output.getvalue().strip()

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(["status", job_id, "--json"])

        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual(job_id, payload["job_id"])
        self.assertEqual("queued", payload["status"])

    def test_cli_set_substate_requires_explicit_attempt(self):
        stderr = io.StringIO()

        with redirect_stdout(io.StringIO()), patch("sys.stderr", stderr), \
                self.assertRaises(SystemExit) as caught:
            self.module.build_parser().parse_args(
                [
                    "set-substate",
                    "job-test",
                    "--name",
                    "index",
                    "--status",
                    "ready",
                ]
            )

        self.assertEqual(2, caught.exception.code)
        self.assertIn("--attempt", stderr.getvalue())

        parsed = self.module.build_parser().parse_args(
            [
                "set-substate",
                "job-test",
                "--name",
                "index",
                "--status",
                "ready",
                "--attempt",
                "3",
            ]
        )
        self.assertEqual(3, parsed.attempt)

    def test_cli_minutes_enqueue_and_retry_return_snapshot_contract(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(
                    [
                        "enqueue",
                        str(self.audio),
                        "--stage",
                        "minutes_generating",
                        "--transcript",
                        str(self.input_transcript),
                        "--json",
                    ]
                )
            enqueued = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("minutes_generating", enqueued["requested_stage"])
            self.assertTrue(enqueued["input_transcript_sha256"])
            self.assertTrue(Path(enqueued["input_transcript_path"]).is_file())

            control = self.module.RelayControl(self.db_path, archive_root=self.root)
            control.cancel(enqueued["job_id"])
            replacement = self.root / "replacement.srt"
            replacement.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n修订稿\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(
                    [
                        "retry",
                        enqueued["job_id"],
                        "--stage",
                        "minutes_generating",
                        "--transcript",
                        str(replacement),
                    ]
                )

        retried = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual(2, retried["attempt"])
        self.assertEqual("minutes_generating", retried["requested_stage"])
        self.assertEqual(
            hashlib.sha256(replacement.read_bytes()).hexdigest(),
            retried["input_transcript_sha256"],
        )

    def test_cli_mark_draft_modified_returns_machine_readable_status(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, archive)
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(["mark-draft-modified", job_id])

        self.assertEqual(0, exit_code)
        self.assertEqual("draft_modified", json.loads(output.getvalue())["status"])

    def test_cli_run_next_claims_and_executes_one_job(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            job_id = self.module.enqueue(self.audio)
            output = io.StringIO()
            with patch.object(self.module, "_execute_claim", return_value=True) as execute, \
                    redirect_stdout(output):
                exit_code = self.module.main(["run-next", "--json"])

        self.assertEqual(0, exit_code)
        execute.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(job_id, payload["job_id"])
        self.assertEqual("transcribing", payload["start_stage"])

    def test_cli_run_whisper_retry_claims_only_requested_job(self):
        job_id = self.control.enqueue(self.audio)
        self.control.record_stage(job_id, "transcribing")
        self.control.record_stage(job_id, "transcript_ready")
        self.control.record_stage(job_id, "minutes_generating")
        archive = create_complete_archive(
            self.root, job_id, include_whisper=False
        )
        self.control.complete_minutes(job_id, archive)
        self.control.record_substate(job_id, "whisper", "failed", error="engine")
        self.control.retry_substate(job_id, "whisper")
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            output = io.StringIO()
            with patch.object(
                self.module, "_execute_whisper_retry_claim", return_value=True
            ) as execute, redirect_stdout(output):
                exit_code = self.module.main(
                    ["run-whisper-retry", job_id, "--json"]
                )

        self.assertEqual(0, exit_code)
        execute.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(job_id, payload["job_id"])
        self.assertEqual(1, payload["generation"])
        self.assertTrue(payload["executed"])

    def test_list_jobs_filters_status_without_exposing_event_payloads(self):
        queued_id = self.control.enqueue(self.audio)
        failed_audio = self.root / "vm-20260710-130000-DEF.m4a"
        failed_audio.write_bytes(b"audio two")
        failed_id = self.control.enqueue(failed_audio)
        self.control.record_stage(failed_id, "transcribing")
        self.control.fail(failed_id, "transcribing", "FunASR failed")

        failed = self.control.list_jobs(status="failed", limit=10)

        self.assertEqual([failed_id], [item["job_id"] for item in failed])
        self.assertEqual("transcribing", failed[0]["failure_stage"])
        self.assertEqual("FunASR failed", failed[0]["last_error"])
        self.assertNotIn("events", failed[0])
        self.assertNotIn("audio_path", failed[0])
        self.assertEqual(queued_id, self.control.list_jobs(status="queued")[0]["job_id"])

    def test_cli_list_json_uses_read_only_service_contract(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            job_id = self.module.enqueue(self.audio)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(["list", "--json", "--status", "queued"])

        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual([job_id], [item["job_id"] for item in payload])

    def test_cli_retry_substate_creates_pending_retry_request(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            job_id = self.module.enqueue(self.audio)
            self.module.record_substate(
                job_id, "whisper", "failed", error="whisper failed"
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(
                    ["retry-substate", job_id, "--name", "whisper"]
                )

        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("pending", payload["substates"]["whisper"]["status"])

    def test_cli_migrate_pending_returns_json_and_error_exit(self):
        job_id = self._advance_to_minutes(self.control, self.audio)
        hidden = create_complete_archive(self.root, job_id)
        self.control.complete_minutes(job_id, hidden, attempt_no=1)
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(
                    ["migrate-pending", job_id, "--json"]
                )
            missing_output = io.StringIO()
            with redirect_stdout(missing_output):
                missing_exit = self.module.main(
                    ["migrate-pending", "job-does-not-exist", "--json"]
                )

        payload = json.loads(output.getvalue())
        missing_payload = json.loads(missing_output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual([job_id], payload["promoted"])
        self.assertEqual(1, missing_exit)
        self.assertFalse(missing_payload["ok"])
        self.assertEqual("job-does-not-exist", missing_payload["errors"][0]["job_id"])

    def test_cli_archive_policy_is_explicit_and_audited(self):
        job_id = self.control.enqueue(self.audio)
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(
                    [
                        "archive-policy",
                        job_id,
                        "--policy",
                        "hidden_fixture",
                        "--json",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("hidden_fixture", payload["archive_policy"])
        self.assertEqual(
            "archive_policy_changed", payload["events"][-1]["event_type"]
        )

    def test_cli_mark_published_returns_machine_readable_published_status(self):
        with patch.dict(
            os.environ,
            {
                "MEETING_RELAY_JOBS_DB": str(self.db_path),
                "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            },
        ):
            job_id = self.module.enqueue(self.audio)
            self.module.record_stage(job_id, "transcribing")
            self.module.record_stage(job_id, "transcript_ready")
            self.module.record_stage(job_id, "minutes_generating")
            draft = create_complete_archive(self.root, job_id)
            completed = self.module.complete_minutes(job_id, draft)
            manifest = create_published_archive(
                self.root, Path(completed["archive_dir"]), job_id
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(
                    [
                        "mark-published",
                        job_id,
                        "--manifest",
                        str(manifest),
                        "--meeting-id",
                        "vm-20260710-120000-ABC",
                    ]
                )

        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("published", payload["status"])
        self.assertEqual("vm-20260710-120000-ABC", payload["meeting_id"])


if __name__ == "__main__":
    unittest.main()
