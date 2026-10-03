"""Voice Memos 桥接的已处理清单：损坏时不能把历史录音全部重新搬进监听目录。

录音目录、监听目录、状态文件、HOME 全部指到临时目录，不读真实的语音备忘录容器。
"""

import importlib.util
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "quickstart" / "voicememos_bridge.py"


class VoiceMemosBridgeStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        (self.home / "Library" / "Logs").mkdir(parents=True)
        self.recordings = root / "recordings"
        self.recordings.mkdir()
        self.outbox = root / "outbox"
        self.outbox.mkdir()
        self.state_file = root / "state" / "vmbridge_seen.json"
        self.module = self._load()

    def tearDown(self):
        self._tmp.cleanup()

    def _load(self):
        env = {
            "HOME": str(self.home),
            "VM_RECORDINGS_DIR": str(self.recordings),
            "VM_OUTBOX": str(self.outbox),
            "VM_STATE_FILE": str(self.state_file),
            "VM_POKE_SEC": "0",
            "VM_POLL_SEC": "0",
        }
        # 模块顶层 basicConfig 会往根 logger 挂指向临时 HOME 的文件 handler：加载时先清空根 logger，
        # 让它真的挂上，加载完关掉再还原，不影响别的用例，也不留没关的文件
        root = logging.getLogger()
        handlers_before = list(root.handlers)
        root.handlers = []
        with patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location(
                "voicememos_bridge_under_test", BRIDGE_PATH
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            try:
                spec.loader.exec_module(module)
            finally:
                for handler in root.handlers:
                    handler.close()
                root.handlers = handlers_before
        return module

    def _record(self, name: str, data: bytes = b"audio") -> None:
        (self.recordings / name).write_bytes(data)

    def _run_main(self, polls: int, on_poll=None) -> None:
        """跑 main()，每轮轮询后的 sleep 处回调 on_poll(第几轮)，跑满 polls 轮停下。"""
        count = {"n": 0}

        def fake_sleep(_seconds):
            count["n"] += 1
            if on_poll is not None:
                on_poll(count["n"])
            if count["n"] >= polls:
                self.module._running = False

        with (
            patch.object(self.module.time, "sleep", side_effect=fake_sleep),
            patch.object(self.module.signal, "signal"),
        ):
            self.module._running = True
            self.module.main()

    def test_corrupt_state_is_treated_as_first_run_and_kept_as_evidence(self):
        history = ["20250101 100000-AAA.m4a", "20250102 100000-BBB.m4a", "20250103 100000-CCC.qta"]
        for name in history:
            self._record(name)
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text('["20250101 100000-AAA.m4a", ', encoding="utf-8")

        new_name = "20251001 090000-NEW.m4a"

        def add_new_recording(poll):
            if poll == 1:
                self._record(new_name)

        with self.assertLogs("vmbridge", level="ERROR") as logs:
            self._run_main(polls=4, on_poll=add_new_recording)

        # 历史录音一条都不搬，桥接起来之后的新录音照常搬
        self.assertEqual(
            ["vm-20251001-090000-NEW.m4a"], sorted(p.name for p in self.outbox.iterdir())
        )
        self.assertEqual(
            sorted(history + [new_name]), json.loads(self.state_file.read_text(encoding="utf-8"))
        )
        evidence = [
            p
            for p in self.state_file.parent.iterdir()
            if p.name.startswith(f"{self.state_file.name}.corrupt")
        ]
        self.assertEqual(1, len(evidence))
        self.assertEqual('["20250101 100000-AAA.m4a", ', evidence[0].read_text(encoding="utf-8"))
        self.assertTrue(any("损坏" in line for line in logs.output))

    def test_first_run_without_state_skips_history(self):
        self._record("20250101 100000-AAA.m4a")

        self._run_main(polls=3)

        self.assertEqual([], list(self.outbox.iterdir()))
        self.assertEqual(
            ["20250101 100000-AAA.m4a"], json.loads(self.state_file.read_text(encoding="utf-8"))
        )

    def test_existing_state_moves_only_unseen_recordings(self):
        self._record("20250101 100000-AAA.m4a")
        self._record("20250102 100000-BBB.m4a")
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text(json.dumps(["20250101 100000-AAA.m4a"]), encoding="utf-8")

        self._run_main(polls=3)

        self.assertEqual(["vm-20250102-100000-BBB.m4a"], [p.name for p in self.outbox.iterdir()])

    def test_save_seen_is_atomic(self):
        # 写到一半出错（断电、磁盘满）时，旧的清单必须原样保留，不能留下半截文件
        self.module.save_seen({"a.m4a"})

        with patch.object(self.module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.module.save_seen({"a.m4a", "b.m4a"})

        self.assertEqual(["a.m4a"], json.loads(self.state_file.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
