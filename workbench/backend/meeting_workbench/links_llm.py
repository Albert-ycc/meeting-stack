"""第四期的 AI 循环 links_llm_loop（4a 搭框架、上限和计数，4b、4c 往里放两种调用）。

- 启用：links_enabled、links_llm_enabled 都开，llm_ready 为真，当天后台用量没到上限时才调；第一轮前
  等 30 秒，调过一次 5 秒后再来，没活 60 秒；每次循环最多调一次。
- 调用在守护线程里跑（不用 asyncio.to_thread 的默认线程池），服务关闭时不等它，它的认领下次回收。
- 顺序（TASK_ORDER）：最近 7 天的会的放宽提到（4b）；等着对比、pair_after 已到的会（4c）；再按会议
  新的在前补 links_backfill_days 天内的放宽提到（4b）。4a 里 tasks 是空的：LLMTask 是它们的接口。
- 认领超过 10 分钟的收回成 pending，不算一次失败（mention_extractions、decision_scan 两处）。
- 每次 tick 先 seed：有 seed(db, now) 的任务给该做的会建 pending 行（4b 在 AI 这一层关着、当天上限为 0
  时不建，免得状态句一直写「还在整理」）。
- 失败按 LLMError 的代码分三种：
  - auth、no_key、balance：整个循环停下，不给任何会加次数，等 key 文件的修改时间变了或点［现在重试］；
  - network、timeout、server、rate_limited：整个循环退避 1 分钟、5 分钟、30 分钟，之后每 30 分钟试一次，
    不加这场会的次数；
  - 拿到 200 但内容不合格，或 bad_request：由那一步给这场会加一次（4b 的 attempts、4c 的
    pair_attempts），3 次记 failed。
- 用量在调用之前记（charge）；请求没到服务器（连接被拒、DNS 失败）时 refund 退回；超时照算。
  app_state['links_llm_usage'] = {"day": 本地日期, "background": N, "qa": M}，后台看
  links_llm_daily_calls，问答看 qa_daily_questions，两个上限分开算，换了一天从零开始。
- 连续 3 次 auth 或网络错误（30 分钟内）时，健康检查里的 llm 记 failing。
- 不看忙信号：它只等网络，不和 FunASR 抢 CPU。发出去的只有逐字稿和决议原文，不发材料原文和文件名。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from .db import Database, utc_now
from .llm import LLMError, llm_ready
from .safe_log import describe_error

logger = logging.getLogger(__name__)

USAGE_KEY = "links_llm_usage"
USAGE_KINDS = ("background", "qa")
FIRST_DELAY_SECONDS = 30
CALLED_DELAY_SECONDS = 5
IDLE_DELAY_SECONDS = 60
CLAIM_TIMEOUT_SECONDS = 10 * 60
# 网络一类错误的整体退避：1 分钟、5 分钟、30 分钟，之后每 30 分钟
BACKOFF_SECONDS = (60, 300, 1800)
PAUSE_CODES = frozenset({"auth", "no_key", "balance"})
BACKOFF_CODES = frozenset({"network", "timeout", "server", "rate_limited"})
# 算 failing 的错误：auth 和网络错误，30 分钟内连续 3 次
FAILING_CODES = frozenset({"auth", "network", "timeout"})
FAILING_COUNT = 3
FAILING_WINDOW_SECONDS = 30 * 60
# 4b、4c 往 tasks 里放的顺序
TASK_ORDER = ("mentions_recent", "pairs", "mentions_backfill")


class LLMTask(Protocol):
    """links_llm_loop 里的一种调用（4b 的放宽提到、4c 的决议对比）。

    - claim(db, now)：在短事务里认领下一个单位（state 改 running、写认领时间，用 claim_stamp），
      没活回 None；
    - run(job)：在事务外调 llm.chat、写结果；内容合格回 True，内容不合格（JSON 不对、条目全被丢掉、
      被截断）回 False；llm.chat 的 LLMError 原样抛出；
    - release(db, job)：把认领放回 pending，不加次数（到了上限、整体停下或退避时）；
    - fail(db, job, code)：内容不合格或 bad_request：这场会加一次，3 次记 failed。
    """

    name: str

    def claim(self, db: Database, now: datetime) -> Any | None: ...

    def run(self, job: Any) -> bool: ...

    def release(self, db: Database, job: Any) -> None: ...

    def fail(self, db: Database, job: Any, code: str) -> None: ...


def claim_stamp(moment: datetime) -> str:
    """认领时间的写法（UTC isoformat），回收时按字符串比。"""
    return moment.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------- 当天用量（库里）


def _local_day(moment: datetime | date) -> str:
    if isinstance(moment, datetime):
        return moment.astimezone().date().isoformat()
    return moment.isoformat()


def read_usage(connection: sqlite3.Connection, day: str) -> dict[str, Any]:
    """当天用量 {"day", "background", "qa"}；记的是别的一天（或没记过）时从零开始。"""
    row = connection.execute("SELECT value FROM app_state WHERE key = ?", (USAGE_KEY,)).fetchone()
    usage: dict[str, Any] = {"day": day, "background": 0, "qa": 0}
    if row is None:
        return usage
    try:
        stored = json.loads(row["value"] if isinstance(row, sqlite3.Row) else row[0])
    except (TypeError, ValueError):
        return usage
    if not isinstance(stored, dict) or stored.get("day") != day:
        return usage
    for kind in USAGE_KINDS:
        try:
            usage[kind] = max(0, int(stored.get(kind) or 0))
        except (TypeError, ValueError):
            usage[kind] = 0
    return usage


def _write_usage(connection: sqlite3.Connection, usage: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (USAGE_KEY, json.dumps(usage, ensure_ascii=False, sort_keys=True), utc_now()),
    )


# ---------------------------------------------------------------------- 认领回收和重试（库里）


def recover_claims(connection: sqlite3.Connection, now: datetime) -> int:
    """认领超过 10 分钟的收回成 pending，不算一次失败（4b 的段进度和已抽出的说法都留着）。"""
    cutoff = claim_stamp(now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS))
    stamp = utc_now()
    count = connection.execute(
        """UPDATE mention_extractions SET state = 'pending', claimed_at = NULL, updated_at = ?
            WHERE state = 'running' AND (claimed_at IS NULL OR claimed_at < ?)""",
        (stamp, cutoff),
    ).rowcount
    count += connection.execute(
        """UPDATE decision_scan SET pair_state = 'pending', pair_claimed_at = NULL, updated_at = ?
            WHERE pair_state = 'running' AND (pair_claimed_at IS NULL OR pair_claimed_at < ?)""",
        (stamp, cutoff),
    ).rowcount
    return count


def requeue_failed(connection: sqlite3.Connection) -> int:
    """［现在重试］：mention_extractions 和 decision_scan.pair_state 里 failed 的行放回 pending、次数清零。"""
    stamp = utc_now()
    count = connection.execute(
        """UPDATE mention_extractions SET state = 'pending', attempts = 0, error = NULL, claimed_at = NULL,
                  updated_at = ?
            WHERE state = 'failed'""",
        (stamp,),
    ).rowcount
    count += connection.execute(
        """UPDATE decision_scan SET pair_state = 'pending', pair_attempts = 0, pair_after = NULL,
                  pair_error = NULL, pair_claimed_at = NULL, updated_at = ?
            WHERE pair_state = 'failed'""",
        (stamp,),
    ).rowcount
    return count


def seed(db: Database, tasks: Sequence[Any], now: datetime) -> int:
    """links_llm.seed()：让各任务给该做的会建 pending 行（有 seed(db, now) 的任务才建，4b 的
    loose_mentions.seed 在 AI 这一层关着、当天上限为 0 时不建）。建行出错只记日志，不耽误这次调用。"""
    created = 0
    for task in tasks:
        seeder = getattr(task, "seed", None)
        if not callable(seeder):
            continue
        try:
            created += int(seeder(db, now) or 0)
        except sqlite3.Error:
            logger.warning("AI 循环建行失败：%s", getattr(task, "name", "?"))
    return created


def ordered(tasks: Sequence[LLMTask]) -> list[LLMTask]:
    """按 TASK_ORDER 排（不认识的名字排在最后）。"""
    rank = {name: index for index, name in enumerate(TASK_ORDER)}
    return sorted(tasks, key=lambda task: rank.get(getattr(task, "name", ""), len(rank)))


# ---------------------------------------------------------------------- 循环


class LinksLLMWorker:
    def __init__(
        self,
        db: Database,
        settings: Any,
        *,
        tasks: Sequence[LLMTask] = (),
        stop: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.tasks = list(tasks)
        self.stop = stop
        self.clock = clock
        self.now = now or (lambda: datetime.now(UTC))
        self.today = today or (lambda: self.now().astimezone().date())
        self._lock = threading.Lock()
        # 整体停下（auth、no_key、balance）和那时 key 文件的修改时间
        self._paused: str | None = None
        self._paused_key_mtime: int | None = None
        # 整体退避
        self._backoff_level = 0
        self._backoff_until: float | None = None
        self._failures: deque[float] = deque(maxlen=FAILING_COUNT)
        self._usage: dict[str, Any] = {"day": None, "background": 0, "qa": 0}
        self._ready = False
        self._last_tick_at: str | None = None

    # ------------------------------------------------------------------ 用量

    def _limit(self, kind: str) -> int:
        if kind == "background":
            return int(self.settings.links_llm_daily_calls)
        if kind == "qa":
            return int(self.settings.qa_daily_questions)
        raise ValueError(f"不认识的用量：{kind}")

    def _day(self) -> str:
        return _local_day(self.today())

    def charge(self, kind: str) -> bool:
        """在一个 BEGIN IMMEDIATE 里查上限并加一；到了上限返回假、不加（调用方就不调）。"""
        limit = self._limit(kind)
        if limit <= 0:
            return False
        day = self._day()
        with self.db.transaction() as connection:
            usage = read_usage(connection, day)
            if usage[kind] >= limit:
                self._remember(usage)
                return False
            usage[kind] += 1
            _write_usage(connection, usage)
        self._remember(usage)
        return True

    def refund(self, kind: str) -> None:
        """请求没到服务器（连接被拒、DNS 失败）时退回这一次；换了一天的不退。"""
        self._limit(kind)
        day = self._day()
        with self.db.transaction() as connection:
            usage = read_usage(connection, day)
            if usage[kind] <= 0:
                return
            usage[kind] -= 1
            _write_usage(connection, usage)
        self._remember(usage)

    def usage_today(self) -> dict[str, Any]:
        with self.db.autocommit() as connection:
            usage = read_usage(connection, self._day())
        self._remember(usage)
        return usage

    def _remember(self, usage: dict[str, Any]) -> None:
        with self._lock:
            self._usage = dict(usage)

    # ------------------------------------------------------------------ 停下和退避

    def _key_mtime(self) -> int | None:
        try:
            return self.settings.llm_api_key_file.expanduser().stat().st_mtime_ns
        except OSError:
            return None

    def clear_pause(self) -> None:
        """［现在重试］：清掉整体停下和退避。"""
        with self._lock:
            self._paused = None
            self._paused_key_mtime = None
            self._backoff_level = 0
            self._backoff_until = None
            self._failures.clear()

    def _pause(self, code: str) -> None:
        with self._lock:
            self._paused = code
            self._paused_key_mtime = self._key_mtime()

    def _back_off(self) -> None:
        with self._lock:
            step = BACKOFF_SECONDS[min(self._backoff_level, len(BACKOFF_SECONDS) - 1)]
            self._backoff_level += 1
            self._backoff_until = self.clock() + step

    def _record(self, code: str | None) -> None:
        """记一次结果：成功清零；auth、网络错误记下时刻（算 failing）。"""
        with self._lock:
            if code is None:
                self._backoff_level = 0
                self._backoff_until = None
                self._failures.clear()
            elif code in FAILING_CODES:
                self._failures.append(self.clock())

    def _failing(self) -> bool:
        return (
            len(self._failures) >= FAILING_COUNT
            and self._failures[-1] - self._failures[0] <= FAILING_WINDOW_SECONDS
            and self.clock() - self._failures[0] <= FAILING_WINDOW_SECONDS
        )

    def _still_paused(self) -> bool:
        """整体停下时：key 文件的修改时间变了就恢复。"""
        with self._lock:
            if self._paused is None:
                return False
            if self._key_mtime() != self._paused_key_mtime:
                self._paused = None
                self._paused_key_mtime = None
                return False
            return True

    def _backing_off(self) -> bool:
        with self._lock:
            return self._backoff_until is not None and self.clock() < self._backoff_until

    # ------------------------------------------------------------------ 一次

    def enabled(self) -> bool:
        return bool(self.settings.links_enabled and self.settings.links_llm_enabled)

    def refresh_state(self) -> None:
        """开头读一次当天用量和 key，健康检查在第一次调用前也有数。"""
        self._ready = llm_ready(self.settings)
        try:
            self.usage_today()
        except sqlite3.Error:
            logger.warning("读 AI 用量失败")

    def tick(self) -> dict[str, Any]:
        """一次循环：最多调一次。返回 {called, state}，state 说明没调的原因或调的结果。"""
        self._last_tick_at = utc_now()
        if not self.enabled():
            return {"called": False, "state": "off"}
        if self.stop is not None and self.stop.is_set():
            return {"called": False, "state": "stopping"}
        now = self.now()
        with self.db.transaction() as connection:
            recover_claims(connection, now)
        # 建行（4b 的 seed）在 key、上限、退避之前：没配置 AI、到了上限时这场会也有一行，状态句才说得出在等什么
        seed(self.db, self.tasks, now)
        self._ready = llm_ready(self.settings)
        if self._still_paused():
            return {"called": False, "state": "paused"}
        if not self._ready:
            return {"called": False, "state": "no_key"}
        if self._backing_off():
            return {"called": False, "state": "backoff"}
        usage = self.usage_today()
        if usage["background"] >= self._limit("background"):
            return {"called": False, "state": "capped"}
        for task in self.tasks:
            if self.stop is not None and self.stop.is_set():
                return {"called": False, "state": "stopping"}
            job = task.claim(self.db, now)
            if job is None:
                continue
            return self._call(task, job)
        return {"called": False, "state": "idle"}

    def _call(self, task: LLMTask, job: Any) -> dict[str, Any]:
        if not self.charge("background"):
            task.release(self.db, job)
            return {"called": False, "state": "capped"}
        try:
            good = task.run(job)
        except LLMError as error:
            code = error.code
            if not error.sent:
                self.refund("background")
            self._record(code)
            if code in PAUSE_CODES:
                task.release(self.db, job)
                self._pause(code)
                logger.warning("AI 循环停下：%s", code)
            elif code in BACKOFF_CODES:
                task.release(self.db, job)
                self._back_off()
                logger.warning("AI 循环退避：%s", code)
            else:  # bad_request：这场会加一次
                task.fail(self.db, job, code)
            return {"called": True, "state": code}
        except Exception:
            # 意料之外的错误（多半是任务自己的 bug）：放回这份活并整体退避，不让它卡在认领状态、
            # 也不在 60 秒后接着耗当天的用量；异常照样抛出，循环外层记日志
            task.release(self.db, job)
            self._back_off()
            raise
        self._record(None)
        if not good:
            task.fail(self.db, job, "invalid")
            return {"called": True, "state": "invalid"}
        return {"called": True, "state": "ok"}

    # ------------------------------------------------------------------ 状态

    def status(self) -> str:
        """健康检查的 llm：ok、no_key、off、capped、balance、failing；另有 auth（key 不对，停下）和
        backoff（在退避，还不到 failing），relation_read.links_state 认得这两种。不查库。"""
        if not self.enabled() or int(self.settings.links_llm_daily_calls) <= 0:
            return "off"
        with self._lock:
            paused = self._paused
            usage = dict(self._usage)
        if paused == "balance":
            return "balance"
        if paused == "auth":
            return "auth"
        if paused == "no_key" or not self._ready:
            return "no_key"
        if self._failing():
            return "failing"
        if self._backing_off():
            return "backoff"
        if usage.get("day") == self._day() and int(usage.get("background") or 0) >= self._limit(
            "background"
        ):
            return "capped"
        return "ok"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            usage = dict(self._usage)
        same_day = usage.get("day") == self._day()
        return {
            "llm": self.status(),
            "calls_today": {
                kind: int(usage.get(kind) or 0) if same_day else 0 for kind in USAGE_KINDS
            },
        }


# ---------------------------------------------------------------------- asyncio 里的循环


async def run_in_daemon(fn: Callable[[], Any]) -> Any:
    """在守护线程里跑 fn，等它的结果；被取消时不等线程（服务关闭时不等正在进行的 AI 调用）。"""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def deliver(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():
            setter(value)

    def target() -> None:
        try:
            result = fn()
        except BaseException as error:  # noqa: BLE001  交回事件循环那边再抛
            try:
                loop.call_soon_threadsafe(deliver, future.set_exception, error)
            except RuntimeError:
                pass
            return
        try:
            loop.call_soon_threadsafe(deliver, future.set_result, result)
        except RuntimeError:  # 事件循环已经关了
            pass

    threading.Thread(target=target, name="meeting-workbench-links-llm", daemon=True).start()
    return await future


async def links_llm_loop(
    worker: LinksLLMWorker,
    stop: Any,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
    first_delay: float = FIRST_DELAY_SECONDS,
) -> None:
    """第一轮前等 30 秒；调过一次 5 秒后再来，没活 60 秒。"""
    # 外层异常只记类型名和位置（safe_log）：任务里意料之外的异常消息可能带着会议原文
    try:
        await run_in_daemon(worker.refresh_state)
    except Exception as error:  # noqa: BLE001
        logger.error("读 AI 循环的状态失败：%s", describe_error(error))
    await _wait(stop, first_delay, sleep)
    while not stop.is_set():
        result: dict[str, Any] = {}
        try:
            result = await run_in_daemon(worker.tick)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.error("AI 循环这一次失败：%s", describe_error(error))
        await _wait(
            stop, CALLED_DELAY_SECONDS if result.get("called") else IDLE_DELAY_SECONDS, sleep
        )


async def _wait(stop: Any, seconds: float, sleep: Callable[[float], Any]) -> None:
    """按 1 秒一段等，停止标记置上就不再等。"""
    left = float(seconds)
    while left > 0 and not stop.is_set():
        step = min(1.0, left)
        await sleep(step)
        left -= step
