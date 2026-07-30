import importlib.util
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
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


def load_watchdog_module():
    spec = importlib.util.spec_from_file_location("relay_watchdog", WATCHDOG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_main_transcript_bundle(transcript: Path, text: str = "主稿已完成") -> None:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(text, encoding="utf-8")
    transcript.with_suffix(".srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n主稿已完成\n", encoding="utf-8"
    )
    transcript.with_name(f"{transcript.stem}.spk.txt").write_text(
        "speaker 0: 主稿已完成", encoding="utf-8"
    )
    transcript.with_name(f"{transcript.stem}.funasr.json").write_text(
        "{}", encoding="utf-8"
    )
    transcript.with_name("funasr.log").write_text(
        "ok", encoding="utf-8"
    )


class RelayDispatchTests(unittest.TestCase):
    def test_transcribe_uses_file_backed_logs_so_background_whisper_cannot_hold_pipe(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module.PRODUCTS_DIR = root / "products"
            module.DEFAULT_PROMPT_FILE = root / "missing-prompt.txt"
            audio = root / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            script = root / "transcribe.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                self.assertNotIn("capture_output", kwargs)
                self.assertEqual(subprocess.STDOUT, kwargs["stderr"])
                self.assertGreaterEqual(kwargs["stdout"].fileno(), 0)
                transcript = (
                    module.PRODUCTS_DIR
                    / audio.stem
                    / audio.stem
                    / f"{audio.stem}.txt"
                )
                write_main_transcript_bundle(transcript)
                kwargs["stdout"].write("FunASR complete\n")
                kwargs["stdout"].flush()
                return subprocess.CompletedProcess(command, 0)

            with patch.object(module.subprocess, "run", side_effect=fake_run):
                result = module.transcribe(audio, script=script)

        self.assertTrue(result and result.endswith(f"{audio.stem}.txt"))

    def test_transcribe_uses_explicit_job_hotwords_instead_of_global_default(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module.PRODUCTS_DIR = root / "products"
            module.DEFAULT_PROMPT_FILE = root / "default.txt"
            module.DEFAULT_PROMPT_FILE.write_text("全局词", encoding="utf-8")
            hotwords = root / "job-hotwords.txt"
            hotwords.write_text("云图\nACME\n", encoding="utf-8")
            audio = root / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            script = root / "transcribe.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                prompt = module.PRODUCTS_DIR / audio.stem / "prompt.txt"
                self.assertEqual("云图\nACME\n", prompt.read_text(encoding="utf-8"))
                transcript = (
                    module.PRODUCTS_DIR
                    / audio.stem
                    / audio.stem
                    / f"{audio.stem}.txt"
                )
                write_main_transcript_bundle(transcript)
                return subprocess.CompletedProcess(command, 0)

            with patch.object(module.subprocess, "run", side_effect=fake_run):
                result = module.transcribe(audio, script=script, prompt_file=hotwords)

        self.assertTrue(result and result.endswith(f"{audio.stem}.txt"))

    def test_controlled_transcribe_polls_heartbeat_without_terminate_or_kill(self):
        module = load_watchdog_module()
        heartbeats = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module.PRODUCTS_DIR = root / "products"
            module.DEFAULT_PROMPT_FILE = root / "missing-prompt.txt"
            audio = root / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            script = root / "transcribe.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            transcript = (
                module.PRODUCTS_DIR / audio.stem / audio.stem / f"{audio.stem}.txt"
            )

            class FakeProcess:
                returncode = None

                def __init__(self):
                    self.polls = 0

                def poll(self):
                    self.polls += 1
                    if self.polls < 3:
                        return None
                    write_main_transcript_bundle(transcript)
                    self.returncode = 0
                    return 0

                def terminate(self):
                    raise AssertionError("running transcription must not be terminated")

                def kill(self):
                    raise AssertionError("running transcription must not be killed")

            with patch.object(module.subprocess, "Popen", return_value=FakeProcess()), patch.object(
                module.time, "sleep", return_value=None
            ):
                result = module.transcribe(
                    audio,
                    script=script,
                    heartbeat=lambda: heartbeats.append("beat"),
                    heartbeat_interval=0,
                    poll_interval=0,
                )

        self.assertTrue(result and result.endswith(f"{audio.stem}.txt"))
        self.assertGreaterEqual(len(heartbeats), 2)

    def test_codex_agent_skips_broken_binary_candidate(self):
        module = load_watchdog_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            broken = tmp / "codex-broken"
            broken.symlink_to(tmp / "missing-codex")
            working = tmp / "codex-working"
            working.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            working.chmod(0o755)

            module.CODEX_BIN = str(broken)
            module.CODEX_FALLBACKS = (str(broken), str(working))
            with patch.dict(os.environ, {
                "MEETING_RELAY_AGENT": "codex",
                "MEETING_RELAY_CODEX_BIN": "",
            }), patch.object(module.shutil, "which", return_value=None):
                command = module.build_agent_shell_command(tmp / "prompt.md")

        self.assertIn(str(working), command)
        self.assertNotIn(str(broken), command)

    def test_codex_agent_resolves_binary_symlink_to_real_target(self):
        module = load_watchdog_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            target = tmp / "app" / "codex"
            target.parent.mkdir()
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            link = tmp / "bin" / "codex"
            link.parent.mkdir()
            link.symlink_to(target)

            module.CODEX_BIN = ""
            module.CODEX_FALLBACKS = (str(link),)
            with patch.dict(os.environ, {
                "MEETING_RELAY_AGENT": "codex",
                "MEETING_RELAY_CODEX_BIN": "",
            }), patch.object(module.shutil, "which", return_value=None):
                command = module.build_agent_shell_command(tmp / "prompt.md")

        self.assertIn(str(target), command)
        self.assertNotIn(str(link), command)

    def test_codex_agent_uses_codex_exec_in_tmux_shell_pane(self):
        module = load_watchdog_module()
        calls = []

        def fake_tmux(*args):
            calls.append(args)
            if args[0] == "display-message":
                return subprocess.CompletedProcess(args, 0, stdout="zsh\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            module.PROMPTS_DIR = Path(tmpdir)
            module._tmux = fake_tmux
            with patch.dict(os.environ, {"MEETING_RELAY_AGENT": "codex"}):
                self.assertTrue(module.dispatch_to_cc1("整理这段录音", kind="测试"))

        send_key_calls = [args for args in calls if args and args[0] == "send-keys"]
        self.assertEqual(1, len(send_key_calls))
        command = send_key_calls[0][3]
        self.assertIn("codex exec", command)
        self.assertNotIn("claude --print", command)

    def test_default_agent_is_claude(self):
        module = load_watchdog_module()

        self.assertEqual("claude", module.DEFAULT_AGENT)

    def test_claude_agent_keeps_legacy_print_dispatch(self):
        module = load_watchdog_module()
        calls = []

        def fake_tmux(*args):
            calls.append(args)
            if args[0] == "display-message":
                return subprocess.CompletedProcess(args, 0, stdout="zsh\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            module.PROMPTS_DIR = Path(tmpdir)
            module._tmux = fake_tmux
            claude = Path(tmpdir) / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            claude.chmod(0o755)
            module.CLAUDE_BIN = ""
            module.CLAUDE_FALLBACKS = (str(claude),)
            with patch.dict(os.environ, {
                "MEETING_RELAY_AGENT": "claude",
                "MEETING_RELAY_CLAUDE_BIN": "",
            }), patch.object(module.shutil, "which", return_value=None):
                self.assertTrue(module.dispatch_to_cc1("整理这段录音", kind="测试"))

        send_key_calls = [args for args in calls if args and args[0] == "send-keys"]
        self.assertEqual(1, len(send_key_calls))
        command = send_key_calls[0][3]
        self.assertIn(f"{claude} --print", command)
        self.assertNotIn("codex exec", command)

    def test_dispatch_returns_false_when_tmux_send_keys_fails(self):
        module = load_watchdog_module()

        def fake_tmux(*args):
            if args[0] == "display-message":
                return subprocess.CompletedProcess(args, 0, stdout="zsh\n", stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="send failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            module.PROMPTS_DIR = Path(tmpdir)
            module._tmux = fake_tmux
            with patch.dict(os.environ, {"MEETING_RELAY_AGENT": "claude"}):
                dispatched = module.dispatch_to_cc1("整理这段录音", kind="测试")

        self.assertFalse(dispatched)

    def test_dispatch_returns_false_when_codex_pane_is_busy(self):
        module = load_watchdog_module()

        def fake_tmux(*args):
            if args[0] == "display-message":
                return subprocess.CompletedProcess(args, 0, stdout="codex\n", stderr="")
            raise AssertionError("busy pane must not receive natural-language send-keys")

        with tempfile.TemporaryDirectory() as tmpdir:
            module.PROMPTS_DIR = Path(tmpdir)
            module._tmux = fake_tmux
            dispatched = module.dispatch_to_cc1("整理这段录音", kind="测试")

        self.assertFalse(dispatched)

    def test_agent_pane_is_busy_while_foreground_codex_child_runs(self):
        module = load_watchdog_module()

        def fake_tmux(*args):
            if args[-1] == "#{pane_current_command}":
                return subprocess.CompletedProcess(
                    args, 0, stdout="zsh\n", stderr=""
                )
            if args[-1] == "#{pane_tty}":
                return subprocess.CompletedProcess(
                    args, 0, stdout="/dev/ttys000\n", stderr=""
                )
            raise AssertionError(f"unexpected tmux call: {args}")

        ps_output = (
            "Ss   -zsh\n"
            "S+   /Applications/ChatGPT.app/Contents/Resources/codex\n"
        )
        with patch.object(module, "_tmux", side_effect=fake_tmux), patch.object(
            module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["ps"], 0, stdout=ps_output, stderr=""
            ),
        ):
            available = module._agent_pane_available()

        self.assertFalse(available)

    def test_agent_pane_is_available_when_shell_is_foreground(self):
        module = load_watchdog_module()

        def fake_tmux(*args):
            value = "zsh\n" if args[-1] == "#{pane_current_command}" else "/dev/ttys000\n"
            return subprocess.CompletedProcess(args, 0, stdout=value, stderr="")

        with patch.object(module, "_tmux", side_effect=fake_tmux), patch.object(
            module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["ps"], 0, stdout="Ss+  -zsh\n", stderr=""
            ),
        ):
            available = module._agent_pane_available()

        self.assertTrue(available)


class LarkNotifyTests(unittest.TestCase):
    def test_notify_lark_skips_when_user_id_not_configured(self):
        """未配置 RELAY_LARK_USER_ID 时静默跳过，不影响主流程。"""
        module = load_watchdog_module()
        calls = []

        def fake_tmux(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            module.LARK_LOG_FILE = Path(tmpdir) / "lark.log"
            module.LARK_USER_ID = ""
            module._tmux = fake_tmux
            module.notify_lark("测试标题", "测试正文")

        self.assertEqual([], calls)

    def test_notify_lark_schedules_bot_send_via_tmux(self):
        module = load_watchdog_module()
        calls = []

        def fake_tmux(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmpdir:
            module.LARK_LOG_FILE = Path(tmpdir) / "lark.log"
            module.LARK_USER_ID = "ou_test_user_id"
            module._tmux = fake_tmux
            module.notify_lark("测试标题", "测试正文")

        self.assertEqual(1, len(calls))
        self.assertEqual("run-shell", calls[0][0])
        self.assertEqual("-b", calls[0][1])
        command = calls[0][2]
        self.assertIn("lark-cli im +messages-send", command)
        self.assertIn("--as bot", command)
        self.assertIn("--user-id", command)
        self.assertNotIn("--as user", command)


class WorkbenchControlCompatibilityTests(unittest.TestCase):
    def _make_audio_and_transcript(self, root: Path):
        audio = root / "vm-20260710-120000-ABC.m4a"
        audio.write_bytes(b"audio")
        transcript = root / "transcript.txt"
        transcript.write_text("这是不能发到飞书的逐字稿正文", encoding="utf-8")
        return audio, transcript

    def test_default_flag_keeps_legacy_path_without_control_enqueue(self):
        module = load_watchdog_module()
        notifications = []
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, transcript = self._make_audio_and_transcript(Path(tmpdir))
            with patch.dict(os.environ, {}, clear=False), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "get_audio_duration_sec", return_value=30), \
                    patch.object(module, "transcribe", return_value=str(transcript)), \
                    patch.object(module, "dispatch_to_cc1", return_value=True), \
                    patch.object(module, "notify_lark", side_effect=lambda title, body: notifications.append((title, body))), \
                    patch.object(module, "mark_processed"), \
                    patch.object(module, "_control_enqueue", side_effect=AssertionError):
                os.environ.pop("MEETING_RELAY_CONTROL_ENABLED", None)
                module.handle_audio(audio)

        notification_text = "\n".join("\n".join(item) for item in notifications)
        self.assertNotIn(audio.name, notification_text)
        self.assertNotIn("这是不能发到飞书的逐字稿正文", notification_text)

    def test_legacy_meeting_prompt_does_not_request_ima_sync(self):
        module = load_watchdog_module()

        prompt = module.build_meeting_prompt(Path("/tmp/audio.m4a"), "/tmp/a.txt", 601)

        self.assertNotIn("IMA", prompt)

    def test_meeting_prompt_uses_single_pass_only_for_short_meetings(self):
        module = load_watchdog_module()

        prompt = module.build_meeting_prompt(
            Path("/tmp/audio.m4a"), "/tmp/a.txt", 12 * 60
        )

        self.assertIn("单轮结构化提取", prompt)
        self.assertNotIn("多阶段递归合并", prompt)

    def test_meeting_prompt_requires_topic_ledger_for_medium_meetings(self):
        module = load_watchdog_module()

        prompt = module.build_meeting_prompt(
            Path("/tmp/audio.m4a"), "/tmp/a.txt", 30 * 60
        )

        self.assertIn("分议题结构化提取", prompt)
        self.assertIn("minutes-evidence.json", prompt)
        self.assertIn("一分钟摘要", prompt)
        self.assertIn("完整会议记录", prompt)

    def test_meeting_prompt_requires_multi_stage_merge_for_long_meetings(self):
        module = load_watchdog_module()

        prompt = module.build_meeting_prompt(
            Path("/tmp/audio.m4a"), "/tmp/a.txt", 90 * 60
        )

        self.assertIn("多阶段递归合并", prompt)
        self.assertIn("不得直接从整篇逐字稿一次生成最终纪要", prompt)

    def test_legacy_failure_notification_does_not_expose_filename_or_path(self):
        module = load_watchdog_module()
        notifications = []
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, _ = self._make_audio_and_transcript(Path(tmpdir))
            with patch.dict(os.environ, {}, clear=False), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", return_value=None), \
                    patch.object(module, "notify_lark", side_effect=lambda title, body: notifications.append((title, body))), \
                    patch.object(module, "mark_processed"):
                os.environ.pop("MEETING_RELAY_CONTROL_ENABLED", None)
                module.handle_audio(audio)

        notification_text = "\n".join("\n".join(item) for item in notifications)
        self.assertIn("failed", notification_text)
        self.assertNotIn(audio.name, notification_text)
        self.assertNotIn(str(module.PRODUCTS_DIR), notification_text)

    def test_enabled_flag_records_stages_and_adds_completion_callback(self):
        module = load_watchdog_module()
        recorded = []
        prompts = []
        notifications = []
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, transcript = self._make_audio_and_transcript(Path(tmpdir))

            def fake_record(job_id, status, **kwargs):
                recorded.append((job_id, status, kwargs))

            def fake_dispatch(prompt, kind):
                prompts.append(prompt)
                return True

            with patch.dict(os.environ, {"MEETING_RELAY_CONTROL_ENABLED": "1"}), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", return_value=str(transcript)), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "_control_update_whisper_progress") as whisper_progress, \
                    patch.object(module, "_control_record_stage", side_effect=fake_record), \
                    patch.object(module, "_control_interrupt_if_requested", return_value=False), \
                    patch.object(module, "dispatch_to_cc1", side_effect=fake_dispatch), \
                    patch.object(module, "_control_record_codex_dispatched"), \
                    patch.object(module, "notify_lark", side_effect=lambda title, body: notifications.append((title, body))), \
                    patch.object(module, "save_last_meeting"), \
                    patch.object(module, "mark_processed"):
                module.process_controlled_claim({
                    "job_id": "job-test-123",
                    "audio_path": str(audio),
                    "attempt": 4,
                    "worker_id": "worker-main-4",
                    "start_stage": "transcribing",
                })

        self.assertEqual(
            ["transcript_ready", "minutes_generating"],
            [status for _, status, _ in recorded],
        )
        self.assertTrue(
            all(
                kwargs
                == {"expected_attempt": 4, "expected_worker": "worker-main-4"}
                for _, _, kwargs in recorded
            )
        )
        self.assertEqual(1, len(prompts))
        self.assertIn("job-test-123", prompts[0])
        self.assertIn("complete-minutes", prompts[0])
        self.assertIn("自动提升为归档根下可见的一级会议目录", prompts[0])
        self.assertIn("不再创建「待校对」分层", prompts[0])
        self.assertIn("Whisper 对照稿是独立异步子状态", prompts[0])
        self.assertIn("不得阻塞", prompts[0])
        self.assertIn("._*", prompts[0])
        self.assertNotIn("IMA", prompts[0])
        whisper_progress.assert_called_once()
        notification_text = "\n".join("\n".join(item) for item in notifications)
        self.assertIn("job-test-123", notification_text)
        self.assertNotIn(audio.name, notification_text)
        self.assertNotIn("这是不能发到飞书的逐字稿正文", notification_text)

    def test_enabled_flag_routes_short_audio_through_receipted_workbench_flow(self):
        module = load_watchdog_module()
        prompts = []
        recorded = []
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, transcript = self._make_audio_and_transcript(Path(tmpdir))
            with patch.dict(os.environ, {"MEETING_RELAY_CONTROL_ENABLED": "1"}), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "get_audio_duration_sec", return_value=30), \
                    patch.object(module, "transcribe", return_value=str(transcript)), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "_control_update_whisper_progress"), \
                    patch.object(module, "_control_record_stage", side_effect=lambda _, status, **kwargs: recorded.append((status, kwargs))), \
                    patch.object(module, "_control_interrupt_if_requested", return_value=False), \
                    patch.object(module, "dispatch_to_cc1", side_effect=lambda prompt, kind: prompts.append((prompt, kind)) or True), \
                    patch.object(module, "_control_record_codex_dispatched"), \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "save_last_meeting"), \
                    patch.object(module, "mark_processed"):
                module.process_controlled_claim({
                    "job_id": "job-short",
                    "audio_path": str(audio),
                    "attempt": 1,
                    "worker_id": "worker-short",
                    "start_stage": "transcribing",
                })

        self.assertEqual(
            ["transcript_ready", "minutes_generating"],
            [status for status, _ in recorded],
        )
        self.assertTrue(
            all(
                kwargs
                == {"expected_attempt": 1, "expected_worker": "worker-short"}
                for _, kwargs in recorded
            )
        )
        self.assertEqual("会议录音", prompts[0][1])
        self.assertIn("job-short", prompts[0][0])
        self.assertIn("complete-minutes", prompts[0][0])

    def test_controlled_failure_is_bound_to_claim_attempt_and_worker(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, _ = self._make_audio_and_transcript(Path(tmpdir))
            with patch.object(module, "wait_stable", return_value=False), \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim({
                    "job_id": "job-stale-safe",
                    "audio_path": str(audio),
                    "attempt": 7,
                    "worker_id": "worker-attempt-7",
                    "start_stage": "transcribing",
                })

        self.assertFalse(result)
        fail.assert_called_once_with(
            "job-stale-safe",
            "stabilizing",
            "audio did not stabilize",
            expected_attempt=7,
            expected_worker="worker-attempt-7",
        )

    def test_enabled_flag_stops_only_after_running_transcription_returns(self):
        module = load_watchdog_module()
        calls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, transcript = self._make_audio_and_transcript(Path(tmpdir))

            def fake_transcribe(*args, **kwargs):
                calls.append("transcribe_finished")
                return str(transcript)

            def fake_interrupt(job_id, stage, **kwargs):
                calls.append(f"stop_checked_after_{stage}")
                self.assertEqual(3, kwargs["expected_attempt"])
                self.assertEqual("worker-stop", kwargs["expected_worker"])
                return True

            with patch.dict(os.environ, {"MEETING_RELAY_CONTROL_ENABLED": "1"}), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", side_effect=fake_transcribe), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "_control_update_whisper_progress"), \
                    patch.object(module, "_control_record_stage"), \
                    patch.object(module, "_control_interrupt_if_requested", side_effect=fake_interrupt), \
                    patch.object(module, "dispatch_to_cc1") as dispatch, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim({
                    "job_id": "job-stop",
                    "audio_path": str(audio),
                    "attempt": 3,
                    "worker_id": "worker-stop",
                    "start_stage": "transcribing",
                })

        # 停止是一次已确认的协作式中断；watchdog 不应把同一源文件自动重跑。
        self.assertTrue(result)
        self.assertEqual(
            ["transcribe_finished", "stop_checked_after_transcribing"], calls
        )
        dispatch.assert_not_called()

    def test_whisper_progress_keeps_missing_background_output_nonblocking(self):
        module = load_watchdog_module()
        with patch.object(module, "_relay_control_module") as control_module:
            control = control_module.return_value
            control.inspect_whisper_ref.return_value = {
                "status": "absent",
                "missing": ["json", "txt"],
            }

            result = module._control_update_whisper_progress(
                "job-whisper",
                Path("/tmp/work"),
                async_expected=True,
                attempt_no=2,
            )

        self.assertIs(result, control.record_substate.return_value)
        control.record_substate.assert_called_once_with(
            "job-whisper", "whisper", "running", error=None, attempt_no=2
        )

    def test_file_event_only_enqueues_when_control_mode_is_enabled(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, _ = self._make_audio_and_transcript(Path(tmpdir))
            handler = module.AudioHandler()
            handler._processed_log.clear()
            with patch.dict(os.environ, {"MEETING_RELAY_CONTROL_ENABLED": "1"}), \
                    patch.object(module, "_control_enqueue", return_value="job-queued") as enqueue, \
                    patch.object(module, "handle_audio", side_effect=AssertionError) as legacy:
                handler._handle(audio)

        enqueue.assert_called_once_with(audio)
        legacy.assert_not_called()

    def test_minutes_retry_uses_claimed_snapshot_without_transcribing(self):
        module = load_watchdog_module()
        prompts = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            audio, _ = self._make_audio_and_transcript(root)
            nested = root / audio.stem / audio.stem
            nested.mkdir(parents=True)
            existing = nested / f"{audio.stem}.txt"
            existing.write_text("复用的 FunASR 主稿", encoding="utf-8")
            snapshot = root / "input-transcript.txt"
            snapshot.write_text("工作台编辑后的逐字稿", encoding="utf-8")
            with patch.object(module, "PRODUCTS_DIR", root), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", side_effect=AssertionError) as transcribe, \
                    patch.object(module, "dispatch_to_cc1", side_effect=lambda prompt, kind: prompts.append(prompt) or True), \
                    patch.object(module, "_control_record_codex_dispatched"), \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "save_last_meeting"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim({
                    "job_id": "job-retry-minutes",
                    "audio_path": str(audio),
                    "attempt": 2,
                    "worker_id": "worker-retry-minutes",
                    "start_stage": "minutes_generating",
                    "input_transcript_path": str(snapshot),
                    "input_transcript_sha256": hashlib.sha256(
                        snapshot.read_bytes()
                    ).hexdigest(),
                })

        self.assertTrue(result)
        transcribe.assert_not_called()
        fail.assert_not_called()
        self.assertIn("job-retry-minutes", prompts[0])
        self.assertIn(str(snapshot), prompts[0])
        self.assertNotIn(str(existing), prompts[0])
        self.assertIn(".workbench-drafts/job-retry-minutes/attempt-2", prompts[0])
        self.assertIn("禁止覆盖", prompts[0])

    def test_published_minutes_retry_uses_snapshot_and_formal_audio(self):
        module = load_watchdog_module()
        prompts = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing_source = root / "removed" / "legacy-session.m4a"
            published = root / "260710 正式会议"
            published.mkdir()
            published_audio = published / missing_source.name
            published_audio.write_bytes(b"audio")
            published_transcript = published / "fp-0123456789abcdef01234567.txt"
            published_transcript.write_text("正式主稿", encoding="utf-8")
            products = root / "products"
            stale_dir = products / missing_source.stem / missing_source.stem
            stale_dir.mkdir(parents=True)
            stale_transcript = stale_dir / f"{missing_source.stem}.txt"
            stale_transcript.write_text("过期原始主稿", encoding="utf-8")
            snapshot = root / "input-transcript.txt"
            snapshot.write_text("工作台发布后继续编辑的逐字稿", encoding="utf-8")
            with patch.object(module, "PRODUCTS_DIR", products), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", side_effect=AssertionError) as transcribe, \
                    patch.object(module, "dispatch_to_cc1", side_effect=lambda prompt, kind: prompts.append(prompt) or True), \
                    patch.object(module, "_control_record_codex_dispatched"), \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "save_last_meeting"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim({
                    "job_id": "job-published-retry",
                    "audio_path": str(missing_source),
                    "attempt": 2,
                    "worker_id": "worker-published-retry",
                    "start_stage": "minutes_generating",
                    "meeting_id": "fp-0123456789abcdef01234567",
                    "published_archive_dir": str(published),
                    "input_transcript_path": str(snapshot),
                    "input_transcript_sha256": hashlib.sha256(
                        snapshot.read_bytes()
                    ).hexdigest(),
                })

        self.assertTrue(result)
        transcribe.assert_not_called()
        fail.assert_not_called()
        self.assertIn(str(published_audio), prompts[0])
        self.assertIn(str(snapshot), prompts[0])
        self.assertNotIn(str(published_transcript), prompts[0])
        self.assertNotIn(str(stale_transcript), prompts[0])
        self.assertIn(".workbench-drafts/job-published-retry/attempt-2", prompts[0])

    def test_minutes_retry_ignores_archive_transcripts_in_favor_of_snapshot(self):
        module = load_watchdog_module()
        prompts = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "removed" / "session.m4a"
            attempt_two = root / ".workbench-drafts" / "job-three" / "attempt-2"
            attempt_two.mkdir(parents=True)
            latest = attempt_two / "fp-current.txt"
            latest.write_text("NEW TRANSCRIPT", encoding="utf-8")
            latest_audio = attempt_two / source.name
            latest_audio.write_bytes(b"audio")
            published = root / "260710 正式会议"
            published.mkdir()
            old = published / "fp-current.txt"
            old.write_text("OLD PUBLISHED TRANSCRIPT", encoding="utf-8")
            (published / source.name).write_bytes(b"audio")
            products = root / "products"
            stale_dir = products / source.stem / source.stem
            stale_dir.mkdir(parents=True)
            stale = stale_dir / f"{source.stem}.txt"
            stale.write_text("STALE PRODUCT TRANSCRIPT", encoding="utf-8")
            snapshot = root / "input-transcript.txt"
            snapshot.write_text("EXPLICIT WORKBENCH SNAPSHOT", encoding="utf-8")
            with patch.object(module, "PRODUCTS_DIR", products), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", side_effect=AssertionError), \
                    patch.object(module, "dispatch_to_cc1", side_effect=lambda prompt, kind: prompts.append(prompt) or True), \
                    patch.object(module, "_control_record_codex_dispatched"), \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "save_last_meeting"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim({
                    "job_id": "job-three",
                    "audio_path": str(source),
                    "attempt": 3,
                    "worker_id": "worker-three",
                    "start_stage": "minutes_generating",
                    "meeting_id": "fp-current",
                    "source_archive_dir": str(attempt_two),
                    "published_archive_dir": str(published),
                    "input_transcript_path": str(snapshot),
                    "input_transcript_sha256": hashlib.sha256(
                        snapshot.read_bytes()
                    ).hexdigest(),
                })

        self.assertTrue(result)
        fail.assert_not_called()
        self.assertIn(str(snapshot), prompts[0])
        self.assertNotIn(str(latest), prompts[0])
        self.assertNotIn(str(old), prompts[0])
        self.assertNotIn(str(stale), prompts[0])
        self.assertIn(".workbench-drafts/job-three/attempt-3", prompts[0])

    def test_worker_once_claims_and_processes_one_attempt(self):
        module = load_watchdog_module()
        claim = {
            "job_id": "job-worker",
            "audio_path": "/tmp/audio.m4a",
            "start_stage": "transcribing",
        }
        with patch.object(module, "_agent_pane_available", return_value=True), \
                patch.object(module, "_control_reconcile_pending_archives", return_value={"ok": True}) as reconcile_pending, \
                patch.object(module, "_control_reconcile_codex_handoffs", return_value=0), \
                patch.object(module, "_control_recover_orphaned_claims", return_value=0), \
                patch.object(module, "_control_claim_next", return_value=claim) as claim_next, \
                patch.object(module, "process_controlled_claim", return_value=True) as process:
            worked = module.run_control_worker_once()

        self.assertTrue(worked)
        reconcile_pending.assert_called_once_with()
        claim_next.assert_called_once()
        process.assert_called_once_with(claim)

    def test_worker_throttles_pending_reconcile_without_delaying_claim_polling(self):
        module = load_watchdog_module()
        module.PENDING_RECONCILE_INTERVAL_SEC = 30.0
        with patch.object(
            module.time, "monotonic", side_effect=[100.0, 101.0, 131.0]
        ), patch.object(
            module,
            "_control_reconcile_pending_archives",
            return_value={"ok": True},
        ) as reconcile_pending, patch.object(
            module, "_agent_pane_available", return_value=True
        ), patch.object(
            module, "_control_reconcile_codex_handoffs", return_value=0
        ), patch.object(
            module, "_control_recover_orphaned_claims", return_value=0
        ), patch.object(
            module, "_control_claim_next", return_value=None
        ) as claim_next:
            results = [module.run_control_worker_once() for _ in range(3)]

        self.assertEqual([False, False, False], results)
        self.assertEqual(2, reconcile_pending.call_count)
        self.assertEqual(3, claim_next.call_count)

    def test_pending_reconcile_interval_is_configurable_and_positive(self):
        with patch.dict(
            os.environ,
            {"MEETING_RELAY_PENDING_RECONCILE_INTERVAL": "7.5"},
        ):
            configured = load_watchdog_module()
        self.assertEqual(7.5, configured.PENDING_RECONCILE_INTERVAL_SEC)

        with patch.dict(
            os.environ,
            {"MEETING_RELAY_PENDING_RECONCILE_INTERVAL": "0"},
        ):
            with self.assertRaisesRegex(ValueError, "greater than zero"):
                load_watchdog_module()

    def test_runtime_heartbeat_integration_accepts_controlled_mode(self):
        module = load_watchdog_module()

        module._control_runtime_heartbeat(
            "watchdog", mode="controlled", status="running"
        )
        module._control_runtime_heartbeat(
            "control-worker", mode="controlled", status="idle"
        )

        control_module = module._relay_control_module()
        control = control_module.RelayControl(Path(os.environ["MEETING_RELAY_JOBS_DB"]))
        health = control.health(control_enabled=True)
        self.assertEqual("healthy", health["status"])
        self.assertEqual("controlled", health["mode"])
        self.assertEqual("idle", health["worker"]["state"])

    def test_runtime_guard_marks_watchdog_degraded_and_exits_when_observer_dies(self):
        module = load_watchdog_module()
        observer = unittest.mock.MagicMock()
        observer.is_alive.return_value = False
        worker = unittest.mock.MagicMock()
        worker.is_alive.return_value = True

        with patch.object(module, "_best_effort_runtime_heartbeat") as heartbeat:
            with self.assertRaisesRegex(RuntimeError, "observer"):
                module._ensure_runtime_components_alive(
                    observer, worker, mode="controlled"
                )

        heartbeat.assert_called_once_with(
            "watchdog",
            mode="controlled",
            status="degraded",
            last_error="observer_stopped",
        )

    def test_runtime_guard_marks_watchdog_degraded_and_exits_when_control_worker_dies(self):
        module = load_watchdog_module()
        observer = unittest.mock.MagicMock()
        observer.is_alive.return_value = True
        worker = unittest.mock.MagicMock()
        worker.is_alive.return_value = False

        with patch.object(module, "_best_effort_runtime_heartbeat") as heartbeat:
            with self.assertRaisesRegex(RuntimeError, "control-worker"):
                module._ensure_runtime_components_alive(
                    observer, worker, mode="controlled"
                )

        heartbeat.assert_called_once_with(
            "watchdog",
            mode="controlled",
            status="degraded",
            last_error="control_worker_stopped",
        )

    def test_blocking_stage_renews_heartbeat_past_thirty_seconds_without_sleep(self):
        module = load_watchdog_module()
        heartbeats = []
        alive = iter([True, True, False])

        class FakeThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

            def is_alive(self):
                return next(alive)

            def join(self, timeout=None):
                self.timeout = timeout

        with patch.object(module.threading, "Thread", FakeThread), patch.object(
            module.time, "monotonic", side_effect=[0.0, 31.0]
        ):
            result = module._run_with_heartbeat(
                lambda: "done",
                heartbeat=lambda: heartbeats.append("beat"),
                heartbeat_interval=10,
                poll_interval=0,
            )

        self.assertEqual("done", result)
        self.assertEqual(["beat", "beat"], heartbeats)

    def test_pretranscription_blocking_stages_report_current_job_and_stage(self):
        module = load_watchdog_module()
        recorded_heartbeats = []
        with tempfile.TemporaryDirectory() as tmpdir:
            audio, transcript = self._make_audio_and_transcript(Path(tmpdir))

            def run_guarded(operation, *, heartbeat, **_kwargs):
                heartbeat()
                return operation()

            with patch.object(module, "_run_with_heartbeat", side_effect=run_guarded), \
                    patch.object(module, "_best_effort_runtime_heartbeat", side_effect=lambda name, **kwargs: recorded_heartbeats.append((name, kwargs))), \
                    patch.object(module, "_job_audio_path", return_value=audio), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", return_value=str(transcript)), \
                    patch.object(module, "_control_update_whisper_progress"), \
                    patch.object(module, "_control_record_stage"), \
                    patch.object(module, "_control_record_minutes_plan_source"), \
                    patch.object(module, "_control_interrupt_if_requested", return_value=False), \
                    patch.object(module, "dispatch_to_cc1", return_value=True), \
                    patch.object(module, "_control_record_codex_dispatched"), \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "save_last_meeting"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim(
                    {
                        "job_id": "job-long-preflight",
                        "audio_path": str(audio),
                        "attempt": 1,
                        "worker_id": "worker-long-preflight",
                        "start_stage": "stabilizing",
                    }
                )

        self.assertTrue(result)
        pretranscription = recorded_heartbeats[:4]
        self.assertEqual(4, len(pretranscription))
        self.assertTrue(
            all(name == "control-worker" for name, _kwargs in pretranscription)
        )
        self.assertTrue(
            all(
                kwargs["current_job_id"] == "job-long-preflight"
                and kwargs["status"] == "busy"
                for _name, kwargs in pretranscription
            )
        )
        self.assertEqual(
            ["stabilizing", "stabilizing", "stabilizing", "transcribing"],
            [kwargs["current_stage"] for _name, kwargs in pretranscription],
        )

    def test_control_worker_logs_failure_backs_off_and_continues(self):
        module = load_watchdog_module()
        stop_event = unittest.mock.MagicMock()
        stop_event.is_set.side_effect = [False, False, True]
        calls = 0

        def run_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary database error")
            return False

        with patch.object(module, "run_control_worker_once", side_effect=run_once), patch.object(
            module, "_control_runtime_heartbeat"
        ) as heartbeat, patch.object(module, "_control_runtime_stop"):
            module.run_control_worker(stop_event)

        self.assertEqual(2, calls)
        self.assertTrue(any(call.kwargs.get("status") == "degraded" for call in heartbeat.mock_calls))
        stop_event.wait.assert_called()

    def test_whisper_retry_runner_invokes_only_whisper_and_submits_staging(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / ".workbench-drafts" / "job-whisper" / "attempt-1"
            target.mkdir(parents=True)
            audio = target / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            claim = {
                "job_id": "job-whisper",
                "attempt": 1,
                "generation": 3,
                "worker_id": "worker-123456",
                "audio_path": str(audio),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "source_archive_dir": str(target),
                "target_archive_dir": str(target),
            }

            def fake_run(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                for suffix in ("json", "tsv", "srt", "txt", "vtt"):
                    (output_dir / f"{audio.stem}.{suffix}").write_text(
                        "{}" if suffix == "json" else "whisper", encoding="utf-8"
                    )
                kwargs["stdout"].write("ok")
                kwargs["stdout"].flush()
                return subprocess.CompletedProcess(command, 0)

            with patch.object(module, "_resolve_whisper_bin", return_value="/fake/whisper"), \
                    patch.object(module, "_validated_whisper_model_dir", return_value=root), \
                    patch.object(module.subprocess, "run", side_effect=fake_run) as run, \
                    patch.object(module, "transcribe", side_effect=AssertionError) as transcribe, \
                    patch.object(module, "_control_finish_whisper_retry") as finish:
                result = module.process_whisper_retry_claim(claim)

        self.assertTrue(result)
        transcribe.assert_not_called()
        command = run.call_args.args[0]
        self.assertIn("--model", command)
        self.assertIn("turbo", command)
        self.assertIn("--model_dir", command)
        self.assertTrue(finish.call_args.kwargs["success"])
        self.assertIsNotNone(finish.call_args.kwargs["artifact_dir"])

    def test_whisper_retry_uses_job_hotword_snapshot_not_global_prompt(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / ".workbench-drafts" / "job-whisper" / "attempt-1"
            target.mkdir(parents=True)
            audio = target / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            hotwords = root / "job-hotwords.txt"
            hotwords.write_text("云图\nACME\n", encoding="utf-8")
            claim = {
                "job_id": "job-whisper",
                "attempt": 1,
                "generation": 3,
                "worker_id": "worker-123456",
                "audio_path": str(audio),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "source_archive_dir": str(target),
                "target_archive_dir": str(target),
                "hotword_prompt_path": str(hotwords),
                "hotword_prompt_sha256": hashlib.sha256(
                    hotwords.read_bytes()
                ).hexdigest(),
            }

            def fake_run(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                for suffix in ("json", "tsv", "srt", "txt", "vtt"):
                    (output_dir / f"{audio.stem}.{suffix}").write_text(
                        "{}" if suffix == "json" else "whisper", encoding="utf-8"
                    )
                return subprocess.CompletedProcess(command, 0)

            with patch.object(module, "_resolve_whisper_bin", return_value="/fake/whisper"), \
                    patch.object(module, "_validated_whisper_model_dir", return_value=root), \
                    patch.object(module.subprocess, "run", side_effect=fake_run) as run, \
                    patch.object(module, "_control_finish_whisper_retry"):
                self.assertTrue(module.process_whisper_retry_claim(claim))

        command = run.call_args.args[0]
        self.assertEqual(
            "云图\nACME\n", command[command.index("--initial_prompt") + 1]
        )

    def test_controlled_claim_fails_closed_when_ffprobe_has_no_duration(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            audio = Path(tmpdir) / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            with patch.object(module, "_job_audio_path", return_value=audio), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "get_audio_duration_sec", return_value=None), \
                    patch.object(module, "transcribe") as transcribe, \
                    patch.object(module, "dispatch_to_cc1") as dispatch, \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim(
                    {
                        "job_id": "job-no-duration",
                        "audio_path": str(audio),
                        "attempt": 1,
                        "worker_id": "worker-no-duration",
                        "start_stage": "transcribing",
                    }
                )

        self.assertFalse(result)
        transcribe.assert_not_called()
        dispatch.assert_not_called()
        fail.assert_called_once_with(
            "job-no-duration",
            "transcribing",
            "audio_duration_unavailable",
            expected_attempt=1,
            expected_worker="worker-no-duration",
        )

    def test_main_transcript_bundle_rejects_missing_and_invalid_utf8_or_json(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            txt = root / "meeting.txt"
            txt.write_text("逐字稿", encoding="utf-8")
            (root / "meeting.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8"
            )
            (root / "meeting.spk.txt").write_bytes(b"\xff")
            (root / "meeting.funasr.json").write_text("not-json", encoding="utf-8")

            errors = module._main_transcript_bundle_errors(txt)

        self.assertIn("main_transcript_spk_utf8", errors)
        self.assertIn("main_transcript_funasr_json", errors)
        self.assertIn("main_transcript_funasr_log", errors)

    def test_main_transcript_bundle_accepts_canonical_funasr_log(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "meeting.txt"
            write_main_transcript_bundle(transcript)

            errors = module._main_transcript_bundle_errors(transcript)

        self.assertEqual([], errors)

    def test_main_transcript_bundle_accepts_legacy_stemmed_funasr_log(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "meeting.txt"
            write_main_transcript_bundle(transcript)
            transcript.with_name("funasr.log").rename(
                transcript.with_name("meeting.funasr.log")
            )

            errors = module._main_transcript_bundle_errors(transcript)

        self.assertEqual([], errors)

    def test_controlled_claim_does_not_dispatch_when_main_bundle_is_rejected(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module.PRODUCTS_DIR = root / "products"
            audio = root / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            transcript = (
                module.PRODUCTS_DIR
                / audio.stem
                / audio.stem
                / f"{audio.stem}.txt"
            )
            write_main_transcript_bundle(transcript)
            transcript.with_name("funasr.log").unlink()
            bundle_error_type = getattr(
                module, "MainTranscriptBundleError", RuntimeError
            )
            bundle_error = bundle_error_type(
                module._main_transcript_bundle_errors(transcript)
            )
            with patch.object(module, "_job_audio_path", return_value=audio), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", side_effect=bundle_error), \
                    patch.object(module, "dispatch_to_cc1") as dispatch, \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim(
                    {
                        "job_id": "job-bad-bundle",
                        "audio_path": str(audio),
                        "attempt": 1,
                        "worker_id": "worker-bad-bundle",
                        "start_stage": "transcribing",
                        "minutes_protocol_version": 3,
                    }
                )

        self.assertFalse(result)
        dispatch.assert_not_called()
        fail.assert_called_once_with(
            "job-bad-bundle",
            "transcribing",
            "main_transcript_bundle_invalid:main_transcript_funasr_log",
            expected_attempt=1,
            expected_worker="worker-bad-bundle",
        )

    def test_v3_claim_without_srt_fails_before_codex_dispatch(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module.ARCHIVE_ROOT = root
            audio = root / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            transcript = root / "meeting.txt"
            transcript.write_text("只有 TXT，没有 SRT", encoding="utf-8")
            (root / ".workbench-drafts" / "job-no-srt" / "attempt-1").mkdir(
                parents=True
            )
            with patch.object(module, "_job_audio_path", return_value=audio), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", return_value=str(transcript)), \
                    patch.object(module, "_control_update_whisper_progress"), \
                    patch.object(module, "_control_record_stage"), \
                    patch.object(module, "_control_record_minutes_plan_source"), \
                    patch.object(module, "_control_interrupt_if_requested", return_value=False), \
                    patch.object(module, "dispatch_to_cc1") as dispatch, \
                    patch.object(module, "_control_fail") as fail, \
                    patch.object(module, "notify_lark"), \
                    patch.object(module, "mark_processed"):
                result = module.process_controlled_claim(
                    {
                        "job_id": "job-no-srt",
                        "audio_path": str(audio),
                        "attempt": 1,
                        "worker_id": "worker-no-srt",
                        "start_stage": "transcribing",
                        "minutes_protocol_version": 3,
                    }
                )

        self.assertFalse(result)
        dispatch.assert_not_called()
        self.assertEqual("minutes_plan_invalid", fail.call_args.args[2])

    def test_v3_claim_writes_minutes_plan_before_codex_dispatch(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            module.ARCHIVE_ROOT = root
            database = root / "jobs.sqlite3"
            audio = root / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            transcript = root / "meeting.txt"
            transcript.write_text("有 SRT 的主稿", encoding="utf-8")
            transcript.with_suffix(".srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n有 SRT 的主稿\n",
                encoding="utf-8",
            )
            attempt_dir = (
                root / ".workbench-drafts"
            )
            with patch.dict(
                os.environ,
                {
                    "MEETING_RELAY_JOBS_DB": str(database),
                    "MEETING_RELAY_ARCHIVE_ROOT": str(root),
                },
            ):
                control_module = module._relay_control_module()
                control = control_module.RelayControl(
                    database, archive_root=root
                )
                job_id = control.enqueue(audio)
                claim = control.claim_next(worker_id="worker-with-srt")
                attempt_dir = attempt_dir / job_id / "attempt-1"
                self.assertFalse(attempt_dir.exists())
                with patch.object(module, "_job_audio_path", return_value=audio), \
                    patch.object(module, "wait_stable", return_value=True), \
                    patch.object(module, "_control_record_source_audio"), \
                    patch.object(module, "get_audio_duration_sec", return_value=601), \
                    patch.object(module, "transcribe", return_value=str(transcript)), \
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
            plan = json.loads(
                (attempt_dir / "minutes-plan.json").read_text(encoding="utf-8")
            )

        self.assertTrue(result)
        self.assertEqual(1, plan["cue_count"])
        self.assertIn("minutes-plan.json", dispatch.call_args.args[0])

    def test_whisper_retry_runner_records_process_failure_without_ready(self):
        module = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / ".workbench-drafts" / "job-whisper" / "attempt-1"
            target.mkdir(parents=True)
            audio = target / "vm-20260710-120000-ABC.m4a"
            audio.write_bytes(b"audio")
            claim = {
                "job_id": "job-whisper",
                "attempt": 1,
                "generation": 3,
                "worker_id": "worker-123456",
                "audio_path": str(audio),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "source_archive_dir": str(target),
                "target_archive_dir": str(target),
            }
            with patch.object(module, "_resolve_whisper_bin", return_value="/fake/whisper"), \
                    patch.object(module, "_validated_whisper_model_dir", return_value=root), \
                    patch.object(
                        module.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 1),
                    ), patch.object(module, "_control_finish_whisper_retry") as finish:
                result = module.process_whisper_retry_claim(claim)

        self.assertFalse(result)
        self.assertFalse(finish.call_args.kwargs["success"])
        self.assertEqual("whisper_process_failed", finish.call_args.kwargs["error"])

    def test_worker_leaves_queue_unclaimed_while_codex_pane_is_busy(self):
        module = load_watchdog_module()
        with patch.object(module, "_agent_pane_available", return_value=False), \
                patch.object(module, "_control_claim_next") as claim_next:
            worked = module.run_control_worker_once()

        self.assertFalse(worked)
        claim_next.assert_not_called()


if __name__ == "__main__":
    unittest.main()
