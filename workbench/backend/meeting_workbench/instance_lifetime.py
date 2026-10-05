"""非正式实例的存活时限：沙箱、回归、临时验证起的服务，到点自己退出并写日志。

2026-09-28 有个 8799 的沙箱实例没人收尾，挂了 5 天：每天白烧 CPU 和写盘，还每天零点真去调了几十次大模型。
这类实例都是人或 AI 会话手动起的（tmux 会话里 source 一份环境变量再 meeting-workbench serve），
没有统一的启动脚本可以挂清理，所以把期限放进服务自己。

正式实例 = 数据目录是 ~/.meeting-workbench 且端口是 8765；两项里有一项不是，就是非正式实例。
正式实例不看这里的任何设置，行为和以前完全一样。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)

OFFICIAL_PORT = 8765
# 发出退出信号后最多等这么久；还没退干净（比如某个线程卡住了）就直接结束进程
HARD_EXIT_GRACE_SECONDS = 30.0


def official_data_dir() -> Path:
    return Path.home() / ".meeting-workbench"


def is_official(settings: Settings) -> bool:
    return (
        settings.port == OFFICIAL_PORT
        and settings.data_dir.resolve() == official_data_dir().resolve()
    )


def lifetime_seconds(settings: Settings) -> float | None:
    """非正式实例的存活秒数；正式实例、或明确设成 0 不限时，返回 None。"""
    if is_official(settings):
        return None
    return settings.instance_lifetime_seconds or None


def _format_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:g} 小时"
    if seconds >= 60:
        return f"{seconds / 60:g} 分钟"
    return f"{seconds:g} 秒"


def sleep_until(deadline: float) -> None:
    """按墙钟睡到 deadline，分段睡：机器休眠醒来后墙钟已经过了点，下一段就直接结束。"""
    while (remaining := deadline - time.time()) > 0:
        time.sleep(min(remaining, 30.0))


def _send_sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _hard_exit() -> None:
    os._exit(0)


def start_lifetime_guard(
    settings: Settings,
    *,
    terminate: Callable[[], None] = _send_sigterm,
    hard_exit: Callable[[], None] = _hard_exit,
    hard_exit_grace: float = HARD_EXIT_GRACE_SECONDS,
    lifetime: float | None = None,
) -> threading.Thread | None:
    """非正式实例起一个守护线程，到点发 SIGTERM 让 uvicorn 走正常的关停流程；正式实例什么都不做。

    lifetime 只给测试传（覆盖设置里的时限）。"""
    seconds = lifetime if lifetime is not None else lifetime_seconds(settings)
    if seconds is None:
        return None
    where = f"数据目录 {settings.data_dir}，端口 {settings.port}"
    logger.warning(
        "这是非正式实例（%s），存活时限 %s，到点自动退出；要长期开着，设置 "
        "MEETING_WORKBENCH_INSTANCE_LIFETIME_SECONDS=0",
        where,
        _format_duration(seconds),
    )

    def guard() -> None:
        sleep_until(time.time() + seconds)
        logger.warning("非正式实例（%s）存活满 %s，自动退出。", where, _format_duration(seconds))
        terminate()
        sleep_until(time.time() + hard_exit_grace)
        logger.warning("退出信号发出 %s 秒还没退干净，直接结束进程", f"{hard_exit_grace:g}")
        hard_exit()

    thread = threading.Thread(target=guard, name="meeting-workbench-lifetime", daemon=True)
    thread.start()
    return thread
