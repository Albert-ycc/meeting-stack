"""后台扫描循环的节奏。

扫描一轮要把整个归档重读一遍（生产上约 8 秒 CPU），空闲时每 15 秒来一轮，白烧近三成单核。
这里让连续几轮没有变化之后，两轮之间的间隔逐步拉长到上限；一有变化来源就立刻醒来。变化来源有四类：

- 归档根、暂存根里导入会读的那批文件变了（新录音落盘、relay 写出逐字稿和纪要、外部改了文件）。
  这类变化发生在别的进程里，只能自己看：退避期间每隔原来的扫描间隔做一次只看目录和文件
  大小、修改时间的指纹（生产上约 40 毫秒），指纹变了就醒来，所以新录音出现在界面上的延迟
  和退避之前一样；
- 页面上的写操作（发布、改纪要、改归属、上传、手动刷新……），写完立刻醒来；
- relay 任务状态变了（排队、转写中、失败的数量或正在跑的任务变化），由健康探测的循环通知；
- 到点要做的事（每日晨报），退避期间也在点上醒来。

两轮之间的最小间隔始终是原来的 scan_interval_seconds：唤醒不会让扫描更密，只会让退避期间的第一轮
不用再等满长间隔。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .importer import (
    SUPPORT_DIRECTORIES,
    is_noise_directory,
    is_noise_name,
    is_source_file_name,
    root_available,
)

logger = logging.getLogger(__name__)

# 起因
TIMER = "timer"
CHANGED = "changed"  # 归档文件指纹变了
RELAY = "relay"  # relay 任务状态变了
API = "api"  # 页面上有写操作

# 变化是真的发生了（不是「也许有」）：醒来之后退避回到原来的节奏
_RESETTING = frozenset({CHANGED, RELAY})

_MAX_QUIET_ROUNDS = 64  # 计数封顶，免得 factor ** steps 溢出


@dataclass(slots=True)
class IdleBackoff:
    """连续没有变化的轮数 → 下一轮之前睡多久。"""

    base: float
    cap: float
    # 前几轮仍按原节奏：刚导入完的后续处理（抽任务、写卡片）往往要多一两轮才收尾
    grace_rounds: int = 2
    factor: float = 2.0
    quiet_rounds: int = 0

    @property
    def interval(self) -> float:
        if self.cap <= self.base:
            return self.base
        steps = self.quiet_rounds - self.grace_rounds
        if steps <= 0:
            return self.base
        return min(self.cap, self.base * self.factor**steps)

    @property
    def backed_off(self) -> bool:
        return self.interval > self.base

    def record(self, *, active: bool) -> None:
        self.quiet_rounds = 0 if active else min(self.quiet_rounds + 1, _MAX_QUIET_ROUNDS)

    def reset(self) -> None:
        self.quiet_rounds = 0


def did_work(result: object, *keys: str) -> bool:
    """一段处理（返回数字或统计字典）这一轮有没有真的做了事。

    只认「做了」的计数：待处理的积压数、被跳过的数、始终为真的 done 标志都不算，
    否则一个永远处理不掉的条目会让循环永远退不了。"""
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result > 0
    if isinstance(result, dict):
        return any(
            isinstance(value, bool | int | float) and bool(value)
            for value in (result.get(key) for key in keys)
        )
    return False


# ---------------------------------------------------------------------- 归档指纹

_HASH_MASK = (1 << 64) - 1


def iter_source_files(roots: Iterable[Path]) -> Iterator[os.DirEntry[str]]:
    """导入会读的文件：归档根、暂存根里认得的类型，不含符号链接、系统垃圾和声档内部目录。

    范围只许比发现阶段（importer._discover_archive / _discover_root）大不许小：漏一个，
    那个文件变了退避期间就没人知道。多出来的（比如暂存根根目录直接放的文件）只会多醒一次。
    """
    for root in roots:
        if not root_available(root):
            continue
        yield from _walk(str(root), top_level=True)


def _walk(directory: str, *, top_level: bool) -> Iterator[os.DirEntry[str]]:
    stack = [(directory, top_level)]
    while stack:
        current, at_top = stack.pop()
        try:
            with os.scandir(current) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if is_noise_directory(name):
                        continue
                    # 根下面一层：点开头的目录和辅助目录发现阶段不进（.obsidian 之类里的 json 常被
                    # 编辑器改），只有草稿区和待校对是例外
                    if at_top and (name.startswith(".") or name in SUPPORT_DIRECTORIES):
                        if name not in {".workbench-drafts", "待校对"}:
                            continue
                    stack.append((entry.path, False))
                    continue
                if is_noise_name(name) or not is_source_file_name(name):
                    continue
            except OSError:
                continue
            yield entry


def source_fingerprint(roots: Iterable[Path]) -> tuple[Hashable, ...]:
    """每个根一项：(能不能用, 文件数, 路径+大小+修改时间的无序和)。读不了的文件当没有。"""
    result: list[Hashable] = []
    for root in roots:
        if not root_available(root):
            result.append((False, 0, 0))
            continue
        count = 0
        total = 0
        for entry in iter_source_files([root]):
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            count += 1
            total = (total + hash((entry.path, stat.st_size, stat.st_mtime_ns))) & _HASH_MASK
        result.append((True, count, total))
    return tuple(result)


# ---------------------------------------------------------------------- 到点要做的事


def seconds_until_daily(
    hour: int, minute: int, tz: tzinfo, *, now: datetime | None = None
) -> float:
    """离下一个「每天 hour:minute（tz 时区）」还有多少秒，严格在未来。"""
    current = (now or datetime.now(tz)).astimezone(tz)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target = (target + timedelta(days=1)).replace(hour=hour, minute=minute)
    return (target - current).total_seconds()


# ---------------------------------------------------------------------- 节奏


class ScanPacer:
    """扫描循环的节拍器：`await wait()` 睡到下一轮该开始，轮次开头 `await snapshot()`，
    收尾 `finish_round(active=...)`。所有方法都在事件循环线程里调用。

    退避没开（上限不大于原间隔）时，wait() 就是原来的睡一个固定间隔，其它方法都是空操作。"""

    def __init__(
        self,
        backoff: IdleBackoff,
        *,
        probe: Callable[[], Hashable] | None = None,
        seconds_to_deadline: Callable[[], float | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.backoff = backoff
        self._probe = probe
        self._seconds_to_deadline = seconds_to_deadline
        self._clock = clock
        self._event = asyncio.Event()
        self._wakes: list[str] = []
        self._baseline: Hashable | None = None
        self._last_end = clock()

    @property
    def enabled(self) -> bool:
        return self.backoff.cap > self.backoff.base

    def wake(self, reason: str) -> None:
        """有变化来源了：下一次 wait() 马上返回（仍守两轮之间的最小间隔）。"""
        if not self.enabled:
            return
        self._wakes.append(reason)
        self._event.set()

    async def snapshot(self) -> None:
        """轮次开头记下归档指纹。要在读盘之前取：这一轮读的过程中又变了的，下一次探测就能看出来。"""
        if self.enabled and self._probe is not None:
            self._baseline = await self._probe_safely()

    def finish_round(self, *, active: bool) -> None:
        self.backoff.record(active=active)
        self._last_end = self._clock()

    async def wait(self) -> str:
        base = self.backoff.base
        if not self.enabled:
            await asyncio.sleep(base)
            return TIMER
        delay = self.backoff.interval
        hint = self._seconds_to_deadline() if self._seconds_to_deadline else None
        if hint is not None:
            # 到点要做的事不能被长间隔拖后：点上多留 1 秒，免得醒早了还在分钟边上
            delay = min(delay, max(hint + 1.0, 0.0))
        deadline = self._clock() + delay
        probing = delay > base and self._probe is not None
        reason = TIMER
        while True:
            if self._wakes:
                reason = self._take_wake()
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                reason = TIMER
                break
            step = min(remaining, base) if probing else remaining
            try:
                await asyncio.wait_for(self._event.wait(), step)
            except TimeoutError:
                if probing and await self._changed():
                    reason = CHANGED
                    break
        if reason in _RESETTING:
            self.backoff.reset()
        # 两轮之间至少隔一个原间隔
        pause = self._last_end + base - self._clock()
        if pause > 0:
            await asyncio.sleep(pause)
        return reason

    def _take_wake(self) -> str:
        reasons = self._wakes
        self._wakes = []
        self._event.clear()
        for reason in (RELAY, API):
            if reason in reasons:
                return reason
        return reasons[0]

    async def _changed(self) -> bool:
        current = await self._probe_safely()
        return current is not None and self._baseline is not None and current != self._baseline

    async def _probe_safely(self) -> Hashable | None:
        assert self._probe is not None
        try:
            return await asyncio.to_thread(self._probe)
        except Exception:
            # 指纹是个提示，读不出来就当这次没变化；超时兜底（最长间隔）仍会按时开下一轮
            logger.warning("归档指纹读取失败", exc_info=True)
            return None
