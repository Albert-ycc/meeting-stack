"""1d-2b：项目提示进任务表，出纪要前按这场会挑词，写回执并把对照表拼进 prompt。"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = REPO_ROOT / "quickstart" / "relay_control.py"
WATCHDOG_PATH = REPO_ROOT / "quickstart" / "relay_watchdog.py"
_RUNTIME_DB_ENV = "MEETING_RELAY_JOBS_DB"
_original_runtime_db = None
_runtime_db_tempdir = None


def setUpModule():
    global _original_runtime_db, _runtime_db_tempdir
    _original_runtime_db = os.environ.get(_RUNTIME_DB_ENV)
    _runtime_db_tempdir = tempfile.TemporaryDirectory()
    os.environ[_RUNTIME_DB_ENV] = str(Path(_runtime_db_tempdir.name) / "jobs.sqlite3")


def tearDownModule():
    if _original_runtime_db is None:
        os.environ.pop(_RUNTIME_DB_ENV, None)
    else:
        os.environ[_RUNTIME_DB_ENV] = _original_runtime_db
    if _runtime_db_tempdir is not None:
        _runtime_db_tempdir.cleanup()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SNAPSHOT = {
    "schema_version": 1,
    "updated_at": "2026-09-26T10:00:00+00:00",
    "terms": [
        {"term": "数理协会", "aliases": ["树立协会"], "scope": "云图AI", "category": "机构", "project_id": "p-yt"},
        {"term": "主数据", "aliases": ["珠数据"], "scope": "数据中台", "category": "术语", "project_id": "p-zt"},
        {"term": "随访", "aliases": ["随方"], "scope": "通用", "category": "术语"},
    ],
    "projects": [
        {"id": "p-yt", "name": "云图AI", "also": [], "cues": [{"text": "云图AI", "kind": "name"}]},
        {"id": "p-zt", "name": "数据中台", "also": [], "cues": [{"text": "数据中台", "kind": "name"}]},
    ],
}


class ProjectHintControlTests(unittest.TestCase):
    def setUp(self):
        self.module = _load("relay_control_hint", CONTROL_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control = self.module.RelayControl(self.root / "jobs.sqlite3", archive_root=self.root)
        self.audio = self.root / "vm-20260926-143000-ABC.m4a"
        self.audio.write_bytes(b"audio")

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_stores_hint_and_claim_exposes_it(self):
        job_id = self.control.enqueue(self.audio, project_hint="  p-yt ")
        self.assertEqual("p-yt", self.control.status(job_id)["project_hint"])
        claim = self.control.claim_next(worker_id="worker-hint")
        self.assertEqual("p-yt", claim["project_hint"])

    def test_idempotent_enqueue_follows_latest_hint_and_blank_keeps_it(self):
        job_id = self.control.enqueue(self.audio)
        self.assertIsNone(self.control.status(job_id)["project_hint"])
        self.assertEqual(job_id, self.control.enqueue(self.audio, project_hint="p-yt"))
        self.assertEqual(job_id, self.control.enqueue(self.audio, project_hint="   "))
        self.assertEqual("p-yt", self.control.status(job_id)["project_hint"])

    def test_retry_updates_hint_only_when_given(self):
        job_id = self.control.enqueue(self.audio, project_hint="p-yt")
        self.control.record_stage(job_id, "transcribing")
        self.control.fail(job_id, "transcribing", "engine failed")
        self.control.retry(job_id, "transcribing")
        self.assertEqual("p-yt", self.control.status(job_id)["project_hint"])
        self.control.record_stage(job_id, "transcribing")
        self.control.fail(job_id, "transcribing", "engine failed")
        self.control.retry(job_id, "transcribing", project_hint="p-zt")
        self.assertEqual("p-zt", self.control.status(job_id)["project_hint"])

    def test_hint_is_length_limited(self):
        with self.assertRaises(self.module.RelayControlError):
            self.control.enqueue(self.audio, project_hint="项" * 201)

    def test_cli_reads_flag_or_environment(self):
        database = self.root / "cli.sqlite3"
        env = {
            "MEETING_RELAY_JOBS_DB": str(database),
            "MEETING_RELAY_ARCHIVE_ROOT": str(self.root),
            self.module.PROJECT_HINT_ENV: "云图AI",
        }
        with patch.dict(os.environ, env):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, self.module.main(["enqueue", str(self.audio)]))
            job_id = output.getvalue().strip()
            control = self.module.RelayControl(database, archive_root=self.root)
            self.assertEqual("云图AI", control.status(job_id)["project_hint"])
            other = self.root / "vm-20260926-150000-DEF.m4a"
            other.write_bytes(b"other")
            output = io.StringIO()
            with redirect_stdout(output):
                self.module.main(["enqueue", str(other), "--project-hint", "p-zt"])
            self.assertEqual("p-zt", control.status(output.getvalue().strip())["project_hint"])

    def test_old_database_gains_the_column(self):
        import sqlite3

        database = self.root / "old.sqlite3"
        self.module.RelayControl(database, archive_root=self.root)
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN project_hint")
        control = self.module.RelayControl(database, archive_root=self.root)
        job_id = control.enqueue(self.audio, project_hint="p-yt")
        self.assertEqual("p-yt", control.status(job_id)["project_hint"])


class WatchdogGlossaryTests(unittest.TestCase):
    def setUp(self):
        self.module = _load("relay_watchdog_glossary", WATCHDOG_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.snapshot = self.root / "glossary-snapshot.json"
        self.snapshot.write_text(json.dumps(SNAPSHOT, ensure_ascii=False), encoding="utf-8")
        self.module.GLOSSARY_SNAPSHOT = self.snapshot
        self.transcript = self.root / "meeting.txt"
        self.transcript.write_text("随方的时候提到树立协会", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepare_writes_receipt_and_returns_table(self):
        receipt_dir = self.root / "attempt"
        receipt_dir.mkdir()
        table, written = self.module.prepare_glossary_injection(
            self.transcript, "p-yt", receipt_dir=receipt_dir, job_id="job-1", attempt_no=2
        )
        self.assertTrue(written)
        receipt = json.loads((receipt_dir / "glossary-injection.json").read_text(encoding="utf-8"))
        self.assertEqual(("job-1", 2), (receipt["job_id"], receipt["attempt"]))
        self.assertEqual("p-yt", receipt["project"]["id"])
        self.assertEqual(["数理协会", "随访"], [entry["term"] for entry in receipt["terms"]])
        self.assertIn("| 数理协会 | 树立协会 |  |", table)
        self.assertNotIn("主数据", table)

    def test_prepare_never_blocks_minutes(self):
        self.module.GLOSSARY_SNAPSHOT = self.root / "missing.json"
        table, written = self.module.prepare_glossary_injection(self.transcript, None)
        self.assertEqual(("", False), (table, written))
        self.module._glossary_injection = None
        self.module.GLOSSARY_INJECTION_PY = self.root / "nope.py"
        with self.assertLogs(self.module.log, level="WARNING"):
            table, written = self.module.prepare_glossary_injection(
                self.transcript, None, receipt_dir=self.root
            )
        self.assertEqual(("", False), (table, written))
        self.assertFalse((self.root / "glossary-injection.json").exists())

    def test_prompt_inlines_table_and_requires_receipt_in_manifest(self):
        prompt = self.module.build_meeting_prompt(
            Path("/tmp/audio.m4a"),
            "/tmp/a.txt",
            1200,
            job_id="job-1",
            glossary_table="## 本场术语对照表（以此为准）\n\n| 正确写法 |\n",
            glossary_receipt=True,
        )
        self.assertIn("## 本场术语对照表（以此为准）", prompt)
        self.assertLess(prompt.index("本场术语对照表"), prompt.index("## 归档规范"))
        self.assertIn("`glossary-injection.json`：只读", prompt)
        plain = self.module.build_meeting_prompt(Path("/tmp/audio.m4a"), "/tmp/a.txt", 1200, job_id="job-1")
        self.assertNotIn("本场术语对照表", plain)
        self.assertNotIn("glossary-injection.json", plain)

    def test_controlled_claim_uses_project_hint_and_writes_receipt(self):
        module = self.module
        module.ARCHIVE_ROOT = self.root
        database = self.root / "jobs.sqlite3"
        audio = self.root / "vm-20260926-143000-ABC.m4a"
        audio.write_bytes(b"audio")
        self.transcript.with_suffix(".srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n随方的时候提到树立协会\n", encoding="utf-8"
        )
        with patch.dict(
            os.environ,
            {"MEETING_RELAY_JOBS_DB": str(database), "MEETING_RELAY_ARCHIVE_ROOT": str(self.root)},
        ):
            control = module._relay_control_module().RelayControl(database, archive_root=self.root)
            job_id = control.enqueue(audio, project_hint="数据中台")
            claim = control.claim_next(worker_id="worker-glossary")
            attempt_dir = self.root / ".workbench-drafts" / job_id / "attempt-1"
            with patch.object(module, "_job_audio_path", return_value=audio), \
                patch.object(module, "wait_stable", return_value=True), \
                patch.object(module, "_control_record_source_audio"), \
                patch.object(module, "get_audio_duration_sec", return_value=601), \
                patch.object(module, "transcribe", return_value=str(self.transcript)), \
                patch.object(module, "_control_update_whisper_progress"), \
                patch.object(module, "_control_record_stage"), \
                patch.object(module, "_control_record_minutes_plan_source"), \
                patch.object(module, "_control_interrupt_if_requested", return_value=False), \
                patch.object(module, "dispatch_to_cc1", return_value=True) as dispatch, \
                patch.object(module, "_control_record_codex_dispatched"), \
                patch.object(module, "notify_lark"), \
                patch.object(module, "save_last_meeting"), \
                patch.object(module, "mark_processed"):
                result = module.process_controlled_claim(claim)
            receipt = json.loads((attempt_dir / "glossary-injection.json").read_text(encoding="utf-8"))

        self.assertTrue(result)
        self.assertEqual({"id": "p-zt", "name": "数据中台", "source": "hint", "score": None}, receipt["project"])
        self.assertEqual((job_id, 1), (receipt["job_id"], receipt["attempt"]))
        prompt = dispatch.call_args.args[0]
        self.assertIn("本场按「数据中台」项目挑了 2 条词", prompt)
        self.assertIn("| 主数据 | 珠数据 |  |", prompt)
        self.assertIn("`glossary-injection.json`：只读", prompt)


if __name__ == "__main__":
    unittest.main()
