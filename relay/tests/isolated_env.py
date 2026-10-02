"""relay 用例的公共隔离：HOME 指到临时目录，摘掉从调用方 shell 继承来的 relay 配置。

relay 的默认路径（~/.meeting-relay 的状态目录、~/.meeting-workbench 的词典快照和归档锁、
~/MeetingArchive、~/Movies/meeting-relay-products、~/Downloads）全按 HOME 算，模块加载时就定死。
用例在真机上跑时，不隔离就会读到生产的 processed.txt、词典快照；调用方 shell 里若导出过
MEETING_RELAY_STATE_DIR 之类，更是直接指到生产目录。

在每个会加载 relay 模块的测试文件的 setUpModule 里最先调用 isolate_environment，模块结束时自动还原
（addModuleCleanup）。relay 模块都是用例里现加载的，加载时读到的已经是隔离后的 HOME。
"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

# relay 自己读的配置：前缀 MEETING_RELAY_、RELAY_（含 RELAY_LARK_USER_ID、RELAY_DISABLE_DUAL）和
# 转写脚本读的 TRANSCRIBE_ENGINE。用例要用哪个自己设。
SCRUBBED_PREFIXES = ("MEETING_RELAY_", "RELAY_")
SCRUBBED_NAMES = frozenset({"TRANSCRIBE_ENGINE"})


def isolate_environment(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(SCRUBBED_PREFIXES) and name not in SCRUBBED_NAMES
    }
    patcher = patch.dict(os.environ, {**inherited, "HOME": str(home)}, clear=True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
