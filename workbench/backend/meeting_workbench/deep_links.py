"""第四期的本机循环 links_loop（4a 搭骨架，4b 到 4h 往空位里填）。

- links_enabled 开着才启动；第一轮前等 20 秒，这一轮有活 10 秒后再来，没活 60 秒。prioritize(meeting_id)
  （会议页打开时调，内存里最多 64 个）同时唤醒循环，1 秒左右就开始下一轮。
- 每轮 asyncio.to_thread(worker.run_round)；遇到停止标记、预算用完或 database is locked 就结束这一轮。
- 轻活 L1 到 L5 只有 SQL 和字符串，不编码、不读盘，合计每轮不超过 3 秒，会议转写时照跑。各步的上限是
  各自的天花板，3 秒用完时后面的步留到下一轮。L1 排在 L3 和 H2 前面：同一轮里影响要看到刚入库的决议。
- 重活 H2 到 H4 合计每轮不超过 15 秒，会议转写时整段跳过。顺序：H3 里打开过、到期的会；H2；H3 其余
  两段；H4。每个单位之前、每批编码之间查忙信号（RoundContext.busy_now），看到忙最多再做完一批就停。
- app_state 里有 material_fts_rebuild（恢复备份后材料全文表还没补完）时，H2、H3 和 H4b 整段跳过，H4a 照做。
- 清理每 24 小时一次（links_housekeeping_at），每个事务最多 5,000 行，会议转写时照跑。
- 各步上一轮怎么结束的记在内存快照里（done、budget、busy、stopping、locked、off、waiting），健康检查
  从快照拼 details.links，请求时不查库。GET 接口从不写库。
- 4a 里 L1、L2 和清理是实的，4b 填了 L5，4d 填了 H3（related.RelatedPass）；L3（4e）、L4（4e）、H2（4e）、
  H4（4h）是空位。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import decisions, file_events, loose_mentions, related
from .db import Database
from .material_fts import REBUILD_KEY
from .relation_read import live_file
from .relations import FILE_KINDS

logger = logging.getLogger(__name__)

FIRST_DELAY_SECONDS = 20
WORK_DELAY_SECONDS = 10
IDLE_DELAY_SECONDS = 60
LIGHT_SECONDS = 3.0
HEAVY_SECONDS = 15.0
PRIORITY_LIMIT = 64
# L2：每轮最多看几行
RESOLVE_ROWS = 500
# 清理
HOUSEKEEPING_KEY = "links_housekeeping_at"
HOUSEKEEPING_EVERY = timedelta(hours=24)
HOUSEKEEPING_BATCH = 5_000
CLEARED_KEEP_DAYS = 90
DROPPED_KEEP_DAYS = 90
UNDO_KEEP = timedelta(days=1)

# 各步在 phases 里的名字
PHASE_DECISIONS = "decisions"  # L1
PHASE_RESOLVE = "resolve"  # L2
PHASE_STALE = "stale"  # L3 影响自动收回
PHASE_PRODUCED = "produced"  # L4 产出建议
PHASE_MENTIONS = "mentions"  # L5 放宽的提到
PHASE_AFFECTS = "affects"  # H2 影响匹配
PHASE_RELATED = "related"  # H3 相关
PHASE_TERMS = "terms"  # H4 挖词
PHASE_HOUSEKEEPING = "housekeeping"
PHASE_STATES = ("done", "budget", "busy", "stopping", "locked", "off", "waiting")
# 要查材料全文表的重活：material_fts_rebuild 在时整段跳过
FTS_PHASES = frozenset({PHASE_AFFECTS, PHASE_RELATED})
WAITING_KEYS = ("decisions", "mentions", "pairs", "related", "affects", "terms")
OPEN_KEYS = ("produced", "affects")
# 健康检查的 failed：AI 试满 3 次仍没做成的会数（4c「只计数，不报警」）
FAILED_KEYS = ("mentions", "pairs")


class RoundLocked(RuntimeError):
    """这一轮遇到 database is locked：结束这一轮，下一轮再来。"""


@dataclass
class RoundContext:
    """一轮里各步共用的东西。deadline 是这一段（轻活或重活）的截止时刻（clock 的读数）。"""

    db: Database
    settings: Any
    now: datetime
    since: str
    clock: Callable[[], float]
    deadline: float
    stopping: Callable[[], bool]
    busy_now: Callable[[], bool]
    semantic: Any = None
    vectors: Any = None
    fts_rebuilding: bool = False
    priorities: list[str] = field(default_factory=list)
    work: int = 0

    def remaining(self) -> float:
        return self.deadline - self.clock()


def _locked(error: BaseException) -> bool:
    return isinstance(error, sqlite3.OperationalError) and "database is locked" in str(error)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class LinksWorker:
    def __init__(
        self,
        db: Database,
        settings: Any,
        *,
        busy: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
        semantic: Any = None,
        vectors: Any = None,
        stop: Any | None = None,
        llm: Any = None,
    ):
        self.db = db
        self.settings = settings
        self.busy = busy
        self.clock = clock
        self.now = now or (lambda: datetime.now(UTC))
        self.today = today or (lambda: self.now().astimezone().date())
        self.semantic = semantic
        self.vectors = vectors
        self.stop = stop
        # links_llm.LinksLLMWorker：快照里带上 AI 的状态和当天用量
        self.llm = llm
        self._running = threading.Lock()
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._priorities: OrderedDict[str, None] = OrderedDict()
        self._resolve_cursor = 0
        # 4d：H3 的状态（检查过的词、背景样本、上次看到的矩阵）跨轮留在内存里
        self.related = related.RelatedPass()
        self._state: dict[str, Any] = {
            "paused": None,
            "last_round_at": None,
            "phases": {},
            "waiting": {key: 0 for key in WAITING_KEYS},
            "open": {key: 0 for key in OPEN_KEYS},
            "failed": {key: 0 for key in FAILED_KEYS},
        }

    # ------------------------------------------------------------------ 外面调的

    def prioritize(self, meeting_id: str) -> None:
        """会议页打开时调：记进内存（最多 64 个，新的在前），同时唤醒循环。"""
        with self._state_lock:
            self._priorities.pop(meeting_id, None)
            self._priorities[meeting_id] = None
            while len(self._priorities) > PRIORITY_LIMIT:
                self._priorities.popitem(last=False)
        self._wake.set()

    def priorities(self) -> list[str]:
        with self._state_lock:
            return list(reversed(self._priorities))

    def done_priority(self, meeting_id: str) -> None:
        """H3 算完一场打开过的会后调（4d）。"""
        with self._state_lock:
            self._priorities.pop(meeting_id, None)

    def wait_idle(self, timeout: float) -> bool:
        """服务关闭时等正在跑的这一轮返回。"""
        if self._running.acquire(timeout=timeout):
            self._running.release()
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        """健康检查的 details.links（links_enabled 关着时只有 {"enabled": false}）。不查库。"""
        if not self.settings.links_enabled:
            return {"enabled": False}
        with self._state_lock:
            state = {
                "enabled": True,
                "paused": self._state["paused"],
                "last_round_at": self._state["last_round_at"],
                "phases": dict(self._state["phases"]),
                "waiting": dict(self._state["waiting"]),
                "open": dict(self._state["open"]),
                "failed": dict(self._state["failed"]),
            }
        if self.llm is not None:
            state.update(self.llm.snapshot())
        else:
            state.update({"llm": "off", "calls_today": {"background": 0, "qa": 0}})
        return state

    # ------------------------------------------------------------------ 一轮

    def _stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def _busy_now(self) -> bool:
        if self.busy is None:
            return False
        try:
            return bool(self.busy())
        except Exception:  # noqa: BLE001  忙信号出错时当作忙，宁可多等
            return True

    def run_round(self) -> dict[str, Any]:
        """一轮：返回 {work, phases, paused}；work 为真时 10 秒后再来。"""
        with self._running:
            self._wake.clear()
            return self._run_round()

    def _run_round(self) -> dict[str, Any]:
        now = self.now()
        start = self.clock()
        ctx = RoundContext(
            db=self.db,
            settings=self.settings,
            now=now,
            since=_stamp(now),
            clock=self.clock,
            deadline=start + LIGHT_SECONDS,
            stopping=self._stopping,
            busy_now=self._busy_now,
            semantic=self.semantic,
            vectors=self.vectors,
            priorities=self.priorities(),
        )
        phases: dict[str, str] = {}
        busy = self._busy_now()
        light = (
            (PHASE_DECISIONS, self.l1_decisions),
            (PHASE_RESOLVE, self.l2_resolve),
            (PHASE_STALE, self.l3_stale_affects),
            (PHASE_PRODUCED, self.l4_produced),
            (PHASE_MENTIONS, self.l5_loose_mentions),
        )
        try:
            self._run_steps(ctx, light, phases)
            ctx.deadline = self.clock() + HEAVY_SECONDS
            ctx.fts_rebuilding = self._fts_rebuilding()
            heavy = (
                (PHASE_RELATED, self.h3_related_opened),
                (PHASE_AFFECTS, self.h2_affects),
                (PHASE_RELATED, self.h3_related_rest),
                (PHASE_TERMS, self.h4_terms),
            )
            if busy:
                for name, _step in heavy:
                    phases[name] = "busy"
            else:
                self._run_heavy(ctx, heavy, phases)
            if not self._stopping():
                phases[PHASE_HOUSEKEEPING] = self.housekeeping(ctx)
        except RoundLocked as locked:
            phases[str(locked)] = "locked"
            logger.info("关联整理这一轮遇到数据库被占用，下一轮再来")
        waiting, opened, failed = self._counts(ctx)
        with self._state_lock:
            self._state["paused"] = "busy" if busy else None
            self._state["last_round_at"] = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._state["phases"] = {name: status for name, status in phases.items() if name != PHASE_HOUSEKEEPING}
            if waiting is not None:
                self._state["waiting"] = waiting
            if opened is not None:
                self._state["open"] = opened
            if failed is not None:
                self._state["failed"] = failed
        return {"work": ctx.work > 0, "phases": phases, "paused": "busy" if busy else None}

    def _run_steps(self, ctx: RoundContext, steps: Any, phases: dict[str, str]) -> None:
        for name, step in steps:
            if self._stopping():
                phases.setdefault(name, "stopping")
                continue
            if ctx.remaining() <= 0:
                phases.setdefault(name, "budget")
                continue
            phases[name] = self._step(name, step, ctx)

    def _run_heavy(self, ctx: RoundContext, steps: Any, phases: dict[str, str]) -> None:
        for name, step in steps:
            if phases.get(name) in ("busy", "stopping", "locked"):
                continue
            if self._stopping():
                phases[name] = "stopping"
                continue
            if self._busy_now():
                phases[name] = "busy"
                continue
            if ctx.remaining() <= 0:
                phases[name] = "budget"
                continue
            if ctx.fts_rebuilding and name in FTS_PHASES:
                # 材料全文表还没补完：H2、H3 整段跳过（H4 只做不查全文表的 H4a，由它自己看 ctx）
                phases[name] = "off"
                continue
            status = self._step(name, step, ctx)
            # H3 分两段：前一段 budget、busy 时后一段不再盖掉
            if phases.get(name) in (None, "done", "off"):
                phases[name] = status
            elif status not in ("done", "off"):
                phases[name] = status

    def _step(self, name: str, step: Callable[[RoundContext], str], ctx: RoundContext) -> str:
        try:
            return step(ctx)
        except sqlite3.OperationalError as error:
            if _locked(error):
                raise RoundLocked(name) from error
            raise

    def _fts_rebuilding(self) -> bool:
        try:
            row = self.db.query_one("SELECT 1 AS yes FROM app_state WHERE key = ?", (REBUILD_KEY,))
        except sqlite3.OperationalError as error:
            if _locked(error):
                raise RoundLocked(PHASE_RELATED) from error
            raise
        return row is not None

    # ------------------------------------------------------------------ 轻活

    def l1_decisions(self, ctx: RoundContext) -> str:
        """L1 决议入库：每轮最多 100 场会或 1.0 秒（和这一段剩下的时间取小的）。"""
        counts = decisions.ingest_pending(
            ctx.db,
            clock=ctx.clock,
            now=ctx.now,
            backfill_days=int(ctx.settings.links_backfill_days),
            max_seconds=max(0.0, min(decisions.ROUND_SECONDS, ctx.remaining())),
        )
        if counts["tried"]:
            ctx.work += 1
        return "done" if counts["tried"] >= counts["pending"] else "budget"

    def l2_resolve(self, ctx: RoundContext) -> str:
        """L2 重找文件：file_id 为空或不是活文件、status 不是 cleared 的行，按 id 升序从内存游标往后
        取 500 行，取到底从头再来。找得到就只在变了时写 file_id；找不到时系统行（shown、suggested）
        改 cleared，你回答过的行和 manual 行这一轮什么都不写。相关的行只按 content_key 找。"""
        kinds = ", ".join(f"'{kind}'" for kind in FILE_KINDS)
        with ctx.db.autocommit() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""SELECT r.* FROM relations r
                          LEFT JOIN material_files f ON f.id = r.file_id AND f.gone_at IS NULL
                         WHERE r.id > ? AND r.kind IN ({kinds}) AND r.status != 'cleared'
                           AND (f.id IS NULL
                                OR (r.kind = 'related' AND f.content_key IS NOT r.content_key))
                         ORDER BY r.id LIMIT ?""",
                    (self._resolve_cursor, RESOLVE_ROWS),
                ).fetchall()
            ]
            moves: list[tuple[int, int]] = []
            clears: list[int] = []
            last = self._resolve_cursor
            out_of_time = False
            for index, row in enumerate(rows):
                if index % 50 == 0 and index and ctx.remaining() <= 0:
                    out_of_time = True
                    break
                last = int(row["id"])
                found = live_file(connection, row)
                if found is not None:
                    if found["id"] != row["file_id"]:
                        moves.append((int(found["id"]), last))
                elif row["origin"] != "manual" and row["status"] in ("shown", "suggested"):
                    clears.append(last)
        written = 0
        if moves or clears:
            with ctx.db.transaction() as connection:
                for file_id, relation_id in moves:
                    written += connection.execute(
                        "UPDATE relations SET file_id = ? WHERE id = ? AND file_id IS NOT ?",
                        (file_id, relation_id, file_id),
                    ).rowcount
                for relation_id in clears:
                    written += connection.execute(
                        """UPDATE relations SET status = 'cleared', updated_at = ?
                            WHERE id = ? AND status IN ('shown', 'suggested') AND origin != 'manual'
                              AND updated_at <= ?""",
                        (_stamp(ctx.now), relation_id, ctx.since),
                    ).rowcount
        if out_of_time:
            self._resolve_cursor = last
            ctx.work += 1
            return "budget"
        if len(rows) < RESOLVE_ROWS:
            self._resolve_cursor = 0  # 到底了，下一轮从头再来
        else:
            self._resolve_cursor = last
            ctx.work += 1
        if written:
            ctx.work += 1
        return "done"

    def l3_stale_affects(self, ctx: RoundContext) -> str:
        """L3 影响自动收回（4e 填）：待回答的影响，决议之后文件有了新的内容标识、决议没了或后来改了
        的改 cleared。每轮 500 行。"""
        return "off"

    def l4_produced(self, ctx: RoundContext) -> str:
        """L4 产出建议（4e 填）：符合条件的任务在窗口里真正新增的文件（file_events.recent_added），
        30 天没回答的收回。每轮 200 条任务或 1 秒。"""
        return "off"

    def l5_loose_mentions(self, ctx: RoundContext) -> str:
        """L5 放宽的提到（4b）：在本机把 AI 挑出的说法对到文件，写 mention 行和 hints_json
        （loose_mentions.resolve_due）。每轮 20 场会或 1.5 秒（和这一段剩下的时间取小的）；签名没变的会
        整轮不写。"""
        counts = loose_mentions.resolve_due(
            ctx.db,
            now=ctx.now,
            since=ctx.since,
            clock=ctx.clock,
            max_seconds=max(0.0, min(loose_mentions.ROUND_SECONDS, ctx.remaining())),
        )
        if counts["tried"]:
            ctx.work += 1
        return "done" if counts["tried"] >= counts["pending"] else "budget"

    # ------------------------------------------------------------------ 重活

    def h3_related_opened(self, ctx: RoundContext) -> str:
        """H3 前一段（4d 填）：打开过、到期的会（ctx.priorities，新的在前），一场最多 15 秒；算完调
        done_priority。材料向量矩阵还没建过（vectors.snapshot() 为 None）时回 waiting。
        材料全文表还没补完时框架整段跳过。"""
        return self.related.opened(ctx, self.done_priority)

    def h2_affects(self, ctx: RoundContext) -> str:
        """H2 影响匹配（4e 填）：只用全文索引或 instr 找共同的数字、日期、词；不用向量、不拿快照、不拿
        编码锁。20 条决议或 5 秒，每条决议之前看剩下的预算。材料全文表还没补完时框架整段跳过。"""
        return "off"

    def h3_related_rest(self, ctx: RoundContext) -> str:
        """H3 后两段（4d 填）：材料一侧的增量（新片段 4,096 段），再按会议新的在前（3 场会或 8 秒）。
        材料全文表还没补完时框架整段跳过。"""
        return self.related.rest(ctx)

    def h4_terms(self, ctx: RoundContext) -> str:
        """H4 挖词（4h 填）：先按每份内容挖种子（H4a，只看前 6 万字），再按项目汇总（H4b）。5 秒，内存
        峰值 30MB 以内。ctx.fts_rebuilding 为真时只做 H4a。"""
        return "off"

    # ------------------------------------------------------------------ 清理

    def housekeeping(self, ctx: RoundContext) -> str:
        """每 24 小时一次：文件流水留 400 天；cleared 的系统关联留 90 天（按 updated_at）；dropped 的
        候选词留 90 天；已经不配置的模型的窗口向量删掉；候选词的 undo_json 过一天清空。每个事务最多
        5,000 行，删满了再开一个事务接着删。rejected 的关联和候选词、不见了的决议永远留着。"""
        row = ctx.db.query_one("SELECT value FROM app_state WHERE key = ?", (HOUSEKEEPING_KEY,))
        if row is not None:
            try:
                last = datetime.fromisoformat(str(row["value"]))
            except ValueError:
                last = None
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if ctx.now - last < HOUSEKEEPING_EVERY:
                    return "done"
        today = ctx.now.astimezone().date()
        cleared_before = _stamp(ctx.now - timedelta(days=CLEARED_KEEP_DAYS))
        dropped_before = _stamp(ctx.now - timedelta(days=DROPPED_KEEP_DAYS))
        undo_before = _stamp(ctx.now - UNDO_KEEP)
        model = str(getattr(ctx.settings, "semantic_model", "") or "")
        jobs: tuple[Callable[[sqlite3.Connection], int], ...] = (
            lambda connection: file_events.prune(connection, today=today, limit=HOUSEKEEPING_BATCH),
            lambda connection: connection.execute(
                """DELETE FROM relations WHERE id IN (
                       SELECT id FROM relations
                        WHERE status = 'cleared' AND origin != 'manual' AND updated_at < ?
                        ORDER BY id LIMIT ?)""",
                (cleared_before, HOUSEKEEPING_BATCH),
            ).rowcount,
            lambda connection: connection.execute(
                """DELETE FROM glossary_candidates WHERE id IN (
                       SELECT id FROM glossary_candidates
                        WHERE status = 'dropped' AND updated_at < ? ORDER BY id LIMIT ?)""",
                (dropped_before, HOUSEKEEPING_BATCH),
            ).rowcount,
            lambda connection: connection.execute(
                """DELETE FROM meeting_windows WHERE rowid IN (
                       SELECT rowid FROM meeting_windows WHERE model != ? LIMIT ?)""",
                (model, HOUSEKEEPING_BATCH),
            ).rowcount,
            lambda connection: connection.execute(
                """UPDATE glossary_candidates SET undo_json = NULL WHERE id IN (
                       SELECT id FROM glossary_candidates
                        WHERE undo_json IS NOT NULL AND (decided_at IS NULL OR decided_at < ?)
                        ORDER BY id LIMIT ?)""",
                (undo_before, HOUSEKEEPING_BATCH),
            ).rowcount,
        )
        try:
            for job in jobs:
                while True:
                    if self._stopping():
                        return "stopping"
                    with ctx.db.transaction() as connection:
                        count = job(connection)
                    if count < HOUSEKEEPING_BATCH:
                        break
            with ctx.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                    (HOUSEKEEPING_KEY, _stamp(ctx.now), _stamp(ctx.now)),
                )
        except sqlite3.OperationalError as error:
            if _locked(error):
                raise RoundLocked(PHASE_HOUSEKEEPING) from error
            raise
        return "done"

    # ------------------------------------------------------------------ 快照里的计数

    def _counts(
        self, ctx: RoundContext
    ) -> tuple[dict[str, int] | None, dict[str, int] | None, dict[str, int] | None]:
        """每轮末尾数一次，健康检查和状态句从快照读。related、affects、terms 由 4d、4e、4h 接上。

        failed：AI 那两步试满 3 次仍没做成的会，只计数、不报警（不改 status，也不进状态句）。"""
        try:
            with ctx.db.autocommit() as connection:
                waiting = {key: 0 for key in WAITING_KEYS}
                waiting["decisions"] = decisions.pending_count(connection)
                waiting["mentions"] = connection.execute(
                    "SELECT COUNT(*) FROM mention_extractions WHERE state IN ('pending', 'running')"
                ).fetchone()[0]
                waiting["pairs"] = connection.execute(
                    "SELECT COUNT(*) FROM decision_scan WHERE pair_state IN ('pending', 'running')"
                ).fetchone()[0]
                # 4d：到期的会数（partial 的会只在 links status 里计数）
                if related.enabled(ctx.settings):
                    waiting["related"] = related.due_count(connection, ctx.settings)
                opened = {
                    kind: connection.execute(
                        "SELECT COUNT(*) FROM relations WHERE kind = ? AND status = 'suggested'", (kind,)
                    ).fetchone()[0]
                    for kind in OPEN_KEYS
                }
                failed = {
                    "mentions": connection.execute(
                        "SELECT COUNT(*) FROM mention_extractions WHERE state = 'failed'"
                    ).fetchone()[0],
                    "pairs": connection.execute(
                        "SELECT COUNT(*) FROM decision_scan WHERE pair_state = 'failed'"
                    ).fetchone()[0],
                }
        except sqlite3.OperationalError:
            return None, None, None
        return (
            {key: int(value) for key, value in waiting.items()},
            {key: int(value) for key, value in opened.items()},
            {key: int(value) for key, value in failed.items()},
        )


# ---------------------------------------------------------------------- asyncio 里的循环


def next_delay(stats: dict[str, Any] | None) -> float:
    """这一轮有活 10 秒后再来，没活（或这一轮出错）60 秒。"""
    return WORK_DELAY_SECONDS if stats and stats.get("work") else IDLE_DELAY_SECONDS


async def links_loop(
    worker: LinksWorker,
    stop: Any,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
    first_delay: float = FIRST_DELAY_SECONDS,
) -> None:
    """第一轮前等 20 秒；之后按 next_delay 等。等的时候按 0.5 秒一段看停止标记和 prioritize 的唤醒。"""
    await _wait(worker, stop, first_delay, sleep)
    while not stop.is_set():
        stats: dict[str, Any] | None = None
        try:
            stats = await asyncio.to_thread(worker.run_round)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("关联整理这一轮失败")
        await _wait(worker, stop, next_delay(stats), sleep)


async def _wait(worker: LinksWorker, stop: Any, seconds: float, sleep: Callable[[float], Any]) -> None:
    left = float(seconds)
    while left > 0 and not stop.is_set() and not worker._wake.is_set():
        step = min(0.5, left)
        await sleep(step)
        left -= step
