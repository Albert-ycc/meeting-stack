"""两份飞书卡片回调监听：task-notify/card_listener.py（README 让用户跑的参考实现）
和 workbench/card_listener.py。两份同样的行为放在一起测，防止修复只进其中一份。

全部在临时 HOME 下加载，不碰真实的 ~/.meeting-stack、~/.meeting-workbench，不调 lark-cli。
"""

import importlib.util
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
LISTENERS = {
    "task-notify": REPO_ROOT / "task-notify" / "card_listener.py",
    "workbench": REPO_ROOT / "workbench" / "card_listener.py",
}
DEFAULT_EVENTS_SUBDIR = {
    "task-notify": Path(".meeting-stack") / "card-events",
    "workbench": Path(".meeting-workbench") / "card-events",
}
EVENTS_ENV = "MEETING_STACK_CARD_EVENTS_DIR"


class _StopLoop(BaseException):
    """跳出 main() 的死循环；main 只吞 Exception，所以用 BaseException。"""


def load_listener(name: str, home: Path, env: dict[str, str] | None = None):
    path = LISTENERS[name]
    overrides = {"HOME": str(home), **(env or {})}
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    with patch.dict(os.environ, overrides):
        if EVENTS_ENV not in overrides:
            os.environ.pop(EVENTS_ENV, None)
        sys.path.insert(0, str(path.parent))
        try:
            spec = importlib.util.spec_from_file_location(f"card_listener_{name}", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(path.parent))
            # 模块顶层 basicConfig 往根 logger 挂了指向临时 HOME 的文件 handler，摘掉，免得影响别的用例
            for handler in root.handlers[:]:
                if handler not in handlers_before:
                    root.removeHandler(handler)
                    handler.close()
    return module


def card_event(operator: dict | None, *, action: str = "confirm") -> dict:
    event = {
        "action": {"value": {"action": action, "task_id": "t1", "extraction_id": 7}},
        "context": {"open_message_id": "om_1"},
    }
    if operator is not None:
        event["operator"] = operator
    return {"header": {"event_id": "e1"}, "event": event}


class FakeClient:
    def __init__(self, confirm_result=None, confirm_error: Exception | None = None):
        self.confirm_result = confirm_result
        self.confirm_error = confirm_error
        self.confirmed: list[str] = []

    def confirm(self, task_id):
        self.confirmed.append(task_id)
        if self.confirm_error is not None:
            raise self.confirm_error
        return self.confirm_result

    def reject(self, task_id):
        raise AssertionError("不该走到驳回")

    def list_tasks(self, extraction_id):
        return [{"id": "t1", "title": "发合同", "status": "confirmed", "meeting_title": "周会"}]


class CardListenerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # 日志目录先建好，让下面各条只测各自的行为；「日志目录不存在」单独由子进程那条测
        for subdir in (".meeting-stack", ".meeting-workbench"):
            (self.home / subdir / "logs").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _load_with_fakes(self, name: str):
        module = load_listener(name, self.home)
        sent: list[str] = []
        patched: list[str] = []
        module._send_text = lambda text: sent.append(text) or {}
        module._update_card = lambda message_id, card: patched.append(message_id) or {"code": 0}
        return module, sent, patched

    def test_fresh_home_import_creates_log_dir(self):
        # 新机器上日志目录还不存在，模块顶层的 logging.basicConfig 不能因此起不来。
        # 用子进程导入：同进程里根 logger 可能已被别的模块配置过，basicConfig 会变成空操作，测不出来。
        for name, path in LISTENERS.items():
            with self.subTest(listener=name):
                home = self.home / f"fresh-{name}"
                home.mkdir()
                code = (
                    "import importlib.util, sys\n"
                    f"sys.path.insert(0, {str(path.parent)!r})\n"
                    f"spec = importlib.util.spec_from_file_location('m', {str(path)!r})\n"
                    "module = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(module)\n"
                    "print(module.LOG_FILE.parent.is_dir())\n"
                )
                env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
                env.pop(EVENTS_ENV, None)
                result = subprocess.run(
                    [sys.executable, "-c", code], capture_output=True, text=True, env=env
                )
                self.assertEqual(0, result.returncode, result.stderr[-500:])
                self.assertEqual("True", result.stdout.strip())

    def test_confirm_success_with_task_detail_refreshes_card(self):
        # 确认成功返回的任务对象自带业务字段 detail（任务详情），不能被当成失败。
        for name in LISTENERS:
            with self.subTest(listener=name):
                module, sent, patched = self._load_with_fakes(name)
                client = FakeClient(
                    confirm_result={
                        "id": "t1",
                        "status": "confirmed",
                        "extraction_id": 7,
                        "detail": "周五前把合同条款发给法务",
                    }
                )
                module.handle_event(client, card_event({"open_id": module.OWNER_OPEN_ID}))
                self.assertEqual([], sent)
                self.assertEqual(["om_1"], patched)

    def test_workbench_http_error_raises_and_is_reported(self):
        for name in LISTENERS:
            with self.subTest(listener=name):
                module, sent, patched = self._load_with_fakes(name)
                client_cls = getattr(module, "WorkbenchClient", None) or module.ShengdangClient
                client = client_cls()
                client.token = "csrf"

                def fail(request, timeout=None):
                    raise urllib.error.HTTPError(
                        request.full_url, 404, "Not Found", {}, io.BytesIO('{"detail": "任务不存在"}'.encode())
                    )

                client.opener.open = fail
                with self.assertRaises(Exception) as caught:
                    client.confirm("t1")
                self.assertIn("任务不存在", str(caught.exception))

                module.handle_event(
                    FakeClient(confirm_error=caught.exception),
                    card_event({"open_id": module.OWNER_OPEN_ID}),
                )
                self.assertEqual(1, len(sent))
                self.assertIn("任务不存在", sent[0])
                self.assertEqual([], patched)

    def test_subscriber_runs_as_bot_and_keeps_its_output(self):
        for name in LISTENERS:
            with self.subTest(listener=name):
                module = load_listener(name, self.home)
                with patch.object(module.subprocess, "Popen") as popen:
                    module._start_subscriber()
                args = popen.call_args.args[0]
                kwargs = popen.call_args.kwargs
                self.assertIn("--as", args)
                self.assertEqual("bot", args[args.index("--as") + 1])
                self.assertIsNot(subprocess.DEVNULL, kwargs["stdout"])
                self.assertIsNot(subprocess.DEVNULL, kwargs["stderr"])

    def test_main_loop_rechecks_subscriber_periodically(self):
        # 长连接子进程断了要能被重新拉起：不能只在启动时检查一次。
        for name in LISTENERS:
            with self.subTest(listener=name):
                module = load_listener(name, self.home)
                clock = iter(range(0, 100_000, 61))
                sleeps = {"n": 0}

                def fake_sleep(seconds):
                    sleeps["n"] += 1
                    if sleeps["n"] >= 3:
                        raise _StopLoop

                checks: list[int] = []
                with (
                    patch.object(module.time, "monotonic", side_effect=lambda: next(clock)),
                    patch.object(module.time, "sleep", side_effect=fake_sleep),
                    patch.object(module, "_ensure_subscriber", side_effect=lambda: checks.append(1)),
                ):
                    with self.assertRaises(_StopLoop):
                        module.main()
                self.assertGreaterEqual(len(checks), 2)

    def test_event_without_operator_open_id_is_rejected(self):
        # 缺 operator.open_id 时无法确认是本人操作，必须拒绝，不能放行。
        for name in LISTENERS:
            with self.subTest(listener=name):
                for operator in (None, {}, {"open_id": ""}):
                    module, sent, patched = self._load_with_fakes(name)
                    client = FakeClient(confirm_result={"status": "confirmed", "extraction_id": 7})
                    module.handle_event(client, card_event(operator))
                    self.assertEqual([], client.confirmed, operator)
                    self.assertEqual([], patched)

    def test_owner_event_is_still_handled(self):
        for name in LISTENERS:
            with self.subTest(listener=name):
                module, sent, patched = self._load_with_fakes(name)
                client = FakeClient(confirm_result={"status": "confirmed", "extraction_id": 7})
                module.handle_event(client, card_event({"open_id": module.OWNER_OPEN_ID}))
                self.assertEqual(["t1"], client.confirmed)

    def test_events_dir_reads_env_and_keeps_default(self):
        for name in LISTENERS:
            with self.subTest(listener=name, case="default"):
                module = load_listener(name, self.home)
                self.assertEqual(self.home / DEFAULT_EVENTS_SUBDIR[name], module.EVENTS_DIR)
                self.assertEqual(module.EVENTS_DIR / "processed", module.PROCESSED_DIR)
            with self.subTest(listener=name, case="env"):
                custom = self.home / "custom events"
                module = load_listener(name, self.home, {EVENTS_ENV: str(custom)})
                self.assertEqual(custom, module.EVENTS_DIR)
                self.assertEqual(custom / "processed", module.PROCESSED_DIR)
            with self.subTest(listener=name, case="tilde"):
                module = load_listener(name, self.home, {EVENTS_ENV: "~/x-events"})
                self.assertEqual(self.home / "x-events", module.EVENTS_DIR)


if __name__ == "__main__":
    unittest.main()
