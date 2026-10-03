"""relay 读工作台那套 .env：watchdog、relayctl 作为命令启动时读，被 import、被用例调 main() 时不读。

加载规则本身（relay_env.load_env_files）用纯函数测；两个入口（relayctl 和作为脚本起的
relay_watchdog.py）在临时拼出来的仓库目录里真跑一遍，看 .env 里的值有没有生效。
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# tests/ 没有 __init__，按文件路径跑单个文件时同目录的公共模块不在 sys.path 上
sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolated_env import isolate_environment

QUICKSTART = Path(__file__).resolve().parents[1] / "quickstart"
ENTRY_FILES = ("relayctl", "relay_control.py", "relay_env.py", "relay_watchdog.py")
_tempdir = None


def setUpModule():
    global _tempdir
    _tempdir = tempfile.TemporaryDirectory()
    isolate_environment(Path(_tempdir.name) / "home")


def tearDownModule():
    if _tempdir is not None:
        _tempdir.cleanup()


def load_env_module():
    spec = importlib.util.spec_from_file_location(
        "relay_env_under_test", QUICKSTART / "relay_env.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LoadEnvFilesTests(unittest.TestCase):
    def setUp(self):
        self.module = load_env_module()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _load(self, *files: Path, environ: dict | None = None):
        environ = {} if environ is None else environ
        applied = self.module.load_env_files(files, environ)
        return applied, environ

    def test_plain_assignments_are_loaded_and_comments_blank_and_malformed_lines_are_ignored(self):
        env_file = self._write(
            ".env",
            "# 注释\n\nMEETING_RELAY_AGENT=codex\nMEETING_RELAY_TMUX_SESSION = agent2\n"
            "没有等号的一行\n9BAD=1\nMEETING_RELAY_BAD KEY=1\n# MEETING_RELAY_WATCH_DIR=/不生效\n",
        )

        applied, environ = self._load(env_file)

        self.assertEqual(
            {"MEETING_RELAY_AGENT": "codex", "MEETING_RELAY_TMUX_SESSION": "agent2"}, environ
        )
        self.assertEqual(["MEETING_RELAY_AGENT", "MEETING_RELAY_TMUX_SESSION"], applied)

    def test_export_prefix_quotes_and_inline_comments(self):
        env_file = self._write(
            ".env",
            "export MEETING_RELAY_AGENT=claude\n"
            'MEETING_RELAY_TMUX_SESSION="my agent"  # 带空格要加引号\n'
            "MEETING_RELAY_LLM_BACKEND='claude'\n"
            "MEETING_RELAY_CLAUDE_MODEL=opus # 行尾注释\n"
            "MEETING_RELAY_DEEPSEEK_MODEL=a#b\n"
            'MEETING_RELAY_LARK_CHAT_ID="unclosed\n',
        )

        _applied, environ = self._load(env_file)

        self.assertEqual("claude", environ["MEETING_RELAY_AGENT"])
        self.assertEqual("my agent", environ["MEETING_RELAY_TMUX_SESSION"])
        self.assertEqual("claude", environ["MEETING_RELAY_LLM_BACKEND"])
        self.assertEqual("opus", environ["MEETING_RELAY_CLAUDE_MODEL"])
        self.assertEqual("a#b", environ["MEETING_RELAY_DEEPSEEK_MODEL"])
        self.assertNotIn("MEETING_RELAY_LARK_CHAT_ID", environ)

    def test_leading_tilde_is_expanded_like_source_did_but_not_inside_quotes(self):
        """原来文档让 source ./.env，不加引号的 ~/ 会被 shell 展开；转写脚本（bash）拿到环境变量里的 ~ 不会再展开。"""
        env_file = self._write(
            ".env",
            "MEETING_RELAY_ARCHIVE_ROOT=~/MeetingArchive\n"
            "MEETING_RELAY_FUNASR_PYTHON=~\n"
            'MEETING_RELAY_WHISPER_BIN="~/quoted"\n'
            "MEETING_RELAY_TMUX_SOCKET=/abs/~/not-leading\n",
        )

        _applied, environ = self._load(env_file)

        home = os.path.expanduser("~")
        self.assertEqual(f"{home}/MeetingArchive", environ["MEETING_RELAY_ARCHIVE_ROOT"])
        self.assertEqual(home, environ["MEETING_RELAY_FUNASR_PYTHON"])
        self.assertEqual("~/quoted", environ["MEETING_RELAY_WHISPER_BIN"])
        self.assertEqual("/abs/~/not-leading", environ["MEETING_RELAY_TMUX_SOCKET"])

    def test_empty_values_are_not_loaded(self):
        """空值不导出：MEETING_RELAY_POLL_INTERVAL= 这种导成空串，float("") 会让 watchdog 一启动就抛。"""
        env_file = self._write(".env", "MEETING_RELAY_POLL_INTERVAL=\nMEETING_RELAY_AGENT=''\n")

        applied, environ = self._load(env_file)

        self.assertEqual([], applied)
        self.assertEqual({}, environ)

    def test_workbench_dir_file_beats_repo_root_file_and_later_lines_beat_earlier_ones(self):
        root_file = self._write(
            ".env", "MEETING_RELAY_AGENT=codex\nMEETING_RELAY_TMUX_SESSION=root\n"
        )
        workbench_file = self._write(
            "workbench/.env", "MEETING_RELAY_AGENT=claude\nMEETING_RELAY_AGENT=claude2\n"
        )

        _applied, environ = self._load(root_file, workbench_file)

        self.assertEqual("claude2", environ["MEETING_RELAY_AGENT"])
        self.assertEqual("root", environ["MEETING_RELAY_TMUX_SESSION"])

    def test_values_already_in_the_environment_win_even_when_empty(self):
        env_file = self._write(
            ".env", "MEETING_RELAY_AGENT=codex\nMEETING_RELAY_TMUX_SESSION=from-file\n"
        )

        applied, environ = self._load(
            env_file, environ={"MEETING_RELAY_AGENT": "claude", "MEETING_RELAY_TMUX_SESSION": ""}
        )

        self.assertEqual(
            {"MEETING_RELAY_AGENT": "claude", "MEETING_RELAY_TMUX_SESSION": ""}, environ
        )
        self.assertEqual([], applied)

    def test_only_keys_relay_reads_are_loaded(self):
        """.env 是工作台那套，里头有飞书 webhook、AI key 文件路径这些；relay 和它起的子进程不该白得一份。"""
        env_file = self._write(
            ".env",
            "MEETING_WORKBENCH_LARK_WEBHOOK_URL=https://example.invalid/hook\n"
            "MEETING_WORKBENCH_ARCHIVE_ROOT=/a\nDEEPSEEK_API_KEY=sk-x\nPATH=/evil\nHOME=/evil\n"
            "RELAY_LARK_USER_ID=ou_abc\nRELAY_DISABLE_DUAL=1\nTRANSCRIBE_ENGINE=observe\n"
            "MEETING_RELAY_WATCH_DIR=/inbox\nXMEETING_RELAY_AGENT=x\n",
        )

        applied, environ = self._load(env_file)

        self.assertEqual(
            [
                "MEETING_RELAY_WATCH_DIR",
                "RELAY_DISABLE_DUAL",
                "RELAY_LARK_USER_ID",
                "TRANSCRIBE_ENGINE",
            ],
            applied,
        )
        self.assertEqual(set(applied), set(environ))

    def test_a_line_with_a_nul_byte_is_ignored_instead_of_crashing_the_start(self):
        env_file = self._write(
            ".env", "MEETING_RELAY_AGENT=cla\x00ude\nMEETING_RELAY_TMUX_SESSION=ok\n"
        )

        applied, environ = self._load(env_file)

        self.assertEqual(["MEETING_RELAY_TMUX_SESSION"], applied)
        self.assertEqual({"MEETING_RELAY_TMUX_SESSION": "ok"}, environ)

    def test_per_call_project_hint_is_never_taken_from_a_file(self):
        """项目提示是工作台每次入队、重试时按场次传的，还特意先摘掉继承来的；文件里的静态值不能把它带回来。"""
        env_file = self._write(
            ".env", "MEETING_RELAY_PROJECT_HINT=p-old\nMEETING_RELAY_AGENT=claude\n"
        )

        applied, environ = self._load(env_file)

        self.assertEqual(["MEETING_RELAY_AGENT"], applied)
        self.assertNotIn("MEETING_RELAY_PROJECT_HINT", environ)

    def test_missing_and_unreadable_files_are_skipped_without_echoing_any_value(self):
        directory_instead_of_file = self.root / "workbench" / ".env"
        directory_instead_of_file.mkdir(parents=True)
        undecodable = self._write("bad.env", "")
        undecodable.write_bytes(b"MEETING_RELAY_AGENT=\xff\xfe-secret-value\n")
        good = self._write(".env", "MEETING_RELAY_TMUX_SESSION=ok\n")
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            applied, environ = self._load(
                self.root / "does-not-exist.env", directory_instead_of_file, undecodable, good
            )

        self.assertEqual(["MEETING_RELAY_TMUX_SESSION"], applied)
        self.assertEqual({"MEETING_RELAY_TMUX_SESSION": "ok"}, environ)
        self.assertIn("workbench", stderr.getvalue())
        self.assertNotIn("secret-value", stderr.getvalue())
        self.assertNotIn("does-not-exist", stderr.getvalue())

    def test_default_files_are_the_same_two_the_workbench_reads_with_workbench_last(self):
        module = self.module
        repo_root = QUICKSTART.parents[1]

        self.assertEqual((repo_root / ".env", repo_root / "workbench" / ".env"), module.ENV_FILES)


class EntryPointTests(unittest.TestCase):
    """在临时拼出来的仓库目录里真跑 relayctl 和 relay_watchdog.py：.env 的位置按脚本自己所在的仓库算。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        self.repo = self.root / "repo"
        quickstart = self.repo / "relay" / "quickstart"
        quickstart.mkdir(parents=True)
        for name in ENTRY_FILES:
            shutil.copy2(QUICKSTART / name, quickstart / name)
        self.quickstart = quickstart
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _env(self, **extra) -> dict:
        """干净的进程环境：只有 PATH 和临时 HOME，调用方 shell 里的任何 MEETING_RELAY_* 都不带进来。"""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "PYTHONDONTWRITEBYTECODE": "1",
            **extra,
        }

    def _write_env_files(self, root_text: str | None = None, workbench_text: str | None = None):
        if root_text is not None:
            (self.repo / ".env").write_text(root_text, encoding="utf-8")
        if workbench_text is not None:
            (self.repo / "workbench").mkdir(exist_ok=True)
            (self.repo / "workbench" / ".env").write_text(workbench_text, encoding="utf-8")

    def _relayctl(self, *arguments: str, env: dict | None = None):
        return subprocess.run(
            [sys.executable, str(self.quickstart / "relayctl"), *arguments],
            env=self._env() if env is None else env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_relayctl_uses_the_jobs_db_named_in_the_env_file(self):
        database = self.root / "from-env-file.sqlite3"
        self._write_env_files(
            workbench_text=f"MEETING_RELAY_JOBS_DB={database}\nMEETING_RELAY_ARCHIVE_ROOT={self.root}\n"
        )

        result = self._relayctl("list", "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout))
        self.assertTrue(database.is_file())

    def test_relayctl_prefers_process_environment_and_workbench_file_over_root_file(self):
        from_root = self.root / "from-root.sqlite3"
        from_workbench = self.root / "from-workbench.sqlite3"
        from_process = self.root / "from-process.sqlite3"
        self._write_env_files(
            root_text=f"MEETING_RELAY_JOBS_DB={from_root}\nMEETING_RELAY_ARCHIVE_ROOT={self.root}\n",
            workbench_text=f"MEETING_RELAY_JOBS_DB={from_workbench}\n",
        )

        self.assertEqual(0, self._relayctl("list", "--json").returncode)
        self.assertEqual(
            0,
            self._relayctl(
                "list", "--json", env=self._env(MEETING_RELAY_JOBS_DB=str(from_process))
            ).returncode,
        )

        self.assertTrue(from_workbench.is_file())
        self.assertTrue(from_process.is_file())
        self.assertFalse(from_root.exists())

    def test_relayctl_health_mode_follows_control_flag_from_the_env_file(self):
        database = self.root / "jobs.sqlite3"
        base = f"MEETING_RELAY_JOBS_DB={database}\nMEETING_RELAY_ARCHIVE_ROOT={self.root}\n"
        self._write_env_files(workbench_text=base)
        legacy = json.loads(self._relayctl("health", "--json").stdout)
        self._write_env_files(workbench_text=base + "MEETING_RELAY_CONTROL_ENABLED=1\n")
        controlled = json.loads(self._relayctl("health", "--json").stdout)

        self.assertEqual("legacy", legacy["mode"])
        self.assertEqual("controlled", controlled["mode"])

    def test_watchdog_started_as_a_script_reads_the_env_files_before_building_its_config(self):
        missing_inbox = self.root / "no-such-inbox"
        self._write_env_files(workbench_text=f"MEETING_RELAY_WATCH_DIR={missing_inbox}\n")

        result = subprocess.run(
            [sys.executable, str(self.quickstart / "relay_watchdog.py")],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn(f"INBOX 目录不存在：{missing_inbox}", result.stderr)

    def test_watchdog_started_with_dash_m_from_the_relay_dir_reads_the_env_files_too(self):
        """模块里 _relay_control_module 两种入口都兼容，加载 .env 也不能只认脚本方式。"""
        missing_inbox = self.root / "no-such-inbox"
        self._write_env_files(workbench_text=f"MEETING_RELAY_WATCH_DIR={missing_inbox}\n")

        result = subprocess.run(
            [sys.executable, "-m", "quickstart.relay_watchdog"],
            cwd=self.repo / "relay",
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn(f"INBOX 目录不存在：{missing_inbox}", result.stderr)

    def test_watchdog_process_environment_beats_the_env_file(self):
        from_file = self.root / "inbox-from-file"
        from_process = self.root / "inbox-from-process"
        self._write_env_files(workbench_text=f"MEETING_RELAY_WATCH_DIR={from_file}\n")

        result = subprocess.run(
            [sys.executable, str(self.quickstart / "relay_watchdog.py")],
            env=self._env(MEETING_RELAY_WATCH_DIR=str(from_process)),
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn(f"INBOX 目录不存在：{from_process}", result.stderr)
        self.assertNotIn(str(from_file), result.stderr)

    def _load_without_running(self, name: str):
        spec = importlib.util.spec_from_file_location(
            f"{name}_probe", self.quickstart / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_importing_the_watchdog_module_does_not_read_env_files(self):
        """用例、别的模块 import relay_watchdog 时不读 .env：生产 checkout 里有真 .env，不能带进用例。"""
        self._write_env_files(workbench_text=f"MEETING_RELAY_WATCH_DIR={self.root / 'from-file'}\n")

        module = self._load_without_running("relay_watchdog")

        self.assertNotEqual(self.root / "from-file", module.INBOX)

    def test_calling_relay_control_main_does_not_read_env_files(self):
        """main(argv) 被用例直接调：只有 relayctl 脚本这个入口才读 .env。"""
        database = self.root / "jobs.sqlite3"
        self._write_env_files(workbench_text="MEETING_RELAY_CONTROL_ENABLED=1\n")
        module = self._load_without_running("relay_control")
        previous = {name: os.environ.pop(name, None) for name in ("MEETING_RELAY_CONTROL_ENABLED",)}
        os.environ["MEETING_RELAY_JOBS_DB"] = str(database)
        os.environ["MEETING_RELAY_ARCHIVE_ROOT"] = str(self.root)
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                module.main(["health", "--json"])
        finally:
            os.environ.pop("MEETING_RELAY_JOBS_DB", None)
            os.environ.pop("MEETING_RELAY_ARCHIVE_ROOT", None)
            for name, value in previous.items():
                if value is not None:
                    os.environ[name] = value

        self.assertEqual("legacy", json.loads(output.getvalue())["mode"])
        self.assertNotIn("MEETING_RELAY_CONTROL_ENABLED", os.environ)


if __name__ == "__main__":
    unittest.main()
