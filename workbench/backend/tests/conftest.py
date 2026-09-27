"""全量测试的公共设置。

材料内容循环（第三期）会编译 Vision 程序、调 textutil、起读取进程。起完整 lifespan 的测试很多，
装机时在 Mac 上跑全量测试不该碰这些，所以默认关掉（pydantic-settings 会读环境变量）；要测循环的
测试自己传 material_content_enabled=True。
"""
from __future__ import annotations

import os

os.environ["MEETING_WORKBENCH_MATERIAL_CONTENT_ENABLED"] = "0"
