"""统一的「会议在转写」信号（第三期 3a）。

满足任一就算忙：
1. 中转状态（relay_health，每 5 秒刷新）里 worker.current_stage 是 stabilizing 或 transcribing；
2. 进程列表里有中转起的 FunASR（funasr_transcribe.py）、会后在后台跑的 Whisper 参考转写（带
   whisper-ref 的 whisper）、千问影子转写（mlx-qwen3-asr）。声档自己起的材料转写进程组不算，
   tail、less、grep 这类看日志的进程也不算；
3. 库里有 state=running 的千问影子转写。

进程列表用一次 `ps -Ao pid=,pgid=,args=` 取，结果缓存 5 秒；ps 失败时算忙，宁可多等也不和会议抢。
AI 写纪要（minutes_generating）不算忙，那时不占 CPU 转写。

两个版本：服务里用的完整版本（传 relay_state，三个来源都看）；不传 relay_state 的版本只看进程
列表和库，是 SemanticIndex 的默认值，命令行 semantic-index 用它。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from .db import Database

CACHE_SECONDS = 5.0
BUSY_STAGES = frozenset({"stabilizing", "transcribing"})
# 看日志、翻文件的进程：命令行里带着转写程序的名字，但它们不是转写。
VIEWER_COMMANDS = frozenset(
    {"tail", "less", "more", "grep", "egrep", "fgrep", "rg", "cat", "head", "vi", "vim", "nano",
     "open", "pgrep", "pkill", "ps", "lsof", "watch", "sed", "awk"}
)

REASON_RELAY = "relay"
REASON_PROCESS = "process"
REASON_QWEN = "qwen"
REASON_PS_FAILED = "ps_failed"

_own_lock = threading.Lock()
_own_groups: set[int] = set()


def register_own_group(pgid: int) -> None:
    """声档自己起的材料转写、读取、认字进程组：不算会议在转写。"""
    with _own_lock:
        _own_groups.add(int(pgid))


def unregister_own_group(pgid: int) -> None:
    with _own_lock:
        _own_groups.discard(int(pgid))


def own_groups() -> set[int]:
    with _own_lock:
        return set(_own_groups)


def read_process_list() -> str:
    result = subprocess.run(
        ["ps", "-Ao", "pid=,pgid=,args="],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    return result.stdout


def _command_name(args: str) -> str:
    try:
        first = shlex.split(args)[0] if args.strip() else ""
    except ValueError:
        first = args.split(" ", 1)[0]
    return os.path.basename(first)


def transcribing_processes(listing: str, *, exclude_groups: set[int] | None = None) -> list[str]:
    """进程列表里正在转写会议的进程（命令行），排除自己的进程组和看日志的进程。"""
    exclude = exclude_groups or set()
    own_pid = os.getpid()
    found: list[str] = []
    for line in listing.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        args = parts[2]
        if pid == own_pid or pgid in exclude:
            continue
        if _command_name(args) in VIEWER_COMMANDS:
            continue
        if "funasr_transcribe.py" in args:
            found.append(args)
        elif "mlx-qwen3-asr" in args:
            found.append(args)
        elif _is_whisper_ref(args):
            found.append(args)
    return found


def _is_whisper_ref(args: str) -> bool:
    """会后的 Whisper 参考转写：whisper 程序，输出目录是 whisper-ref。"""
    return "whisper-ref" in args and "whisper" in args.replace("whisper-ref", "")


class BusySignal:
    """调用它得到「会议是不是在转写」；reason() 说明是哪一种。"""

    def __init__(
        self,
        db: Database | None,
        *,
        relay_state: Callable[[], dict[str, Any] | None] | None = None,
        process_list: Callable[[], str] = read_process_list,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
    ):
        self.db = db
        self.relay_state = relay_state
        self.process_list = process_list
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached_at: float | None = None
        self._cached: str | None = None

    def __call__(self) -> bool:
        return self.reason() is not None

    def reason(self) -> str | None:
        relay = self._relay_reason()
        if relay is not None:
            return relay
        with self._lock:
            now = self.clock()
            if self._cached_at is not None and now - self._cached_at < self.cache_seconds:
                return self._cached
        verdict = self._process_reason() or self._qwen_reason()
        with self._lock:
            self._cached_at = self.clock()
            self._cached = verdict
        return verdict

    def _relay_reason(self) -> str | None:
        if self.relay_state is None:
            return None
        try:
            state = self.relay_state() or {}
        except Exception:  # noqa: BLE001
            return None
        worker = state.get("worker") if isinstance(state, dict) else None
        stage = worker.get("current_stage") if isinstance(worker, dict) else None
        return REASON_RELAY if stage in BUSY_STAGES else None

    def _process_reason(self) -> str | None:
        try:
            listing = self.process_list()
        except (OSError, subprocess.SubprocessError, ValueError):
            return REASON_PS_FAILED
        return REASON_PROCESS if transcribing_processes(listing, exclude_groups=own_groups()) else None

    def _qwen_reason(self) -> str | None:
        if self.db is None:
            return None
        try:
            row = self.db.query_one("SELECT 1 AS busy FROM asr_shadow_runs WHERE state='running' LIMIT 1")
        except Exception:  # noqa: BLE001
            return None
        return REASON_QWEN if row else None
