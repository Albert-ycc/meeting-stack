"""第四期 4g：项目内问答的计划、任务、名额和超时（都只在内存里，重启就没了，不建表）。

- prepare：在本机找原文（ask_retrieval.retrieve），从不调 AI；返回计划号和要发的内容。计划留 10 分钟。
- ask：按计划号把这几段原样发出去，立刻回 202 和任务号；页面每 1.5 秒轮询一次。任务在守护线程里跑，最多
  2 个同时调 AI（BoundedSemaphore），其余最多等 30 秒拿位置；同一个项目同时只有一个在回答的问题。不用
  ThreadPoolExecutor：解释器退出时会等它的线程。
- 每天上限 qa_daily_questions（4a 的 charge("qa")，和后台的 200 次分开算）：真要调 AI 时加一，［再问一次］
  也算；没配 AI、问答关着、到了上限、计划过期、同项目在答、选的来源为空时都在加一之前就回错误。请求没到
  服务器（连接被拒、DNS 失败）或没拿到位置时退回这一次。
- 任务到 qa_timeout_seconds + 5 秒还没回来就记成 timeout 停下，立刻放掉名额和项目锁；线程晚回来的结果丢掉。
- 回答不存库：不进任何提示词、索引、词典或卡片；任务留 30 分钟后就没了。
- 日志只记项目 id、各类来源的段数、耗时、结果代码和异常类型名；从不记问题、原文段落和回答。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .ask_retrieval import (
    NOTE_TEXTS,
    QA_SYSTEM,
    Plan,
    ProjectMissing,
    QuestionError,
    build_prompt,
    clean_question,
    logger,
    parse_answer,
    retrieve,
)
from .materials import volume_state

__all__ = ["QA_SYSTEM", "AskError", "AskRegistry", "AskService", "STOP_TEXTS"]

PLAN_TTL = 600
JOB_TTL = 1800
MAX_PLANS = 64
MAX_JOBS = 32
SLOTS = 2
SLOT_WAIT = 30.0
TIMEOUT_GRACE = 5.0
MAX_TOKENS = 900

WAITING_TEXT = "在等 AI 回答"
PROJECT_MISSING = "项目不存在"
PLAN_EXPIRED = "这次找到的原话过期了，请再问一次"
JOB_EXPIRED = "这次的回答过期了，请再问一次"
PROJECT_BUSY = "这个项目上一个问题还在回答"
NOTHING_TO_SEND = "这次没有可以发送的原话"
CAPPED = "今天问答的次数到上限了，明天再问"
NO_KEY = "没配置 AI，先列出找到的原话"
QA_OFF = "问答的 AI 回答已关闭，先列出找到的原话"

# 停了的八种原因：(字, ［再问一次］)。timeout 的秒数取设置
STOP_TEXTS: dict[str, tuple[str, bool]] = {
    "timeout": ("AI 没回（等了 {seconds} 秒），先列出找到的原话", True),
    "network": ("连不上 AI，先列出找到的原话", True),
    "rate_limited": ("AI 那边太忙，过一会儿再问", True),
    "server": ("AI 那边出错了，先列出找到的原话", True),
    "auth": ("AI 的 key 不对，先列出找到的原话", False),
    "bad_request": ("AI 不接受这次的请求，先列出找到的原话", False),
    "slots": ("AI 正在回答别的问题，过一会儿再问", True),
    "error": ("出了点问题，先列出找到的原话", True),
}
# llm.LLMError 的代码对到停了的原因：余额不足算 AI 那边出错；中途没了 key 算 key 不对
_REASONS = {
    "timeout": "timeout",
    "network": "network",
    "rate_limited": "rate_limited",
    "server": "server",
    "balance": "server",
    "auth": "auth",
    "no_key": "auth",
    "bad_request": "bad_request",
}
# 这几种代码虽然对到有［再问一次］的原因，再问也没用：不给按钮（余额不足，充值之前再问还是一样）
_NO_RETRY_CODES = frozenset({"balance"})


class AskError(Exception):
    """接口的错误：status 是 HTTP 状态，text 是给页面的一句话。"""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


@dataclass
class _Plan:
    project_id: str
    plan: Plan
    created: float
    llm: str


@dataclass
class _Job:
    id: str
    project_id: str
    plan: _Plan
    with_materials: bool
    created: float
    state: str = "waiting"
    call_started: float | None = None
    finished: float | None = None
    holds_slot: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _JobView:
    """任务在某一刻的样子（锁里复制出来的）。"""

    state: str
    payload: dict[str, Any]
    plan: Plan
    with_materials: bool


def _spawn_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="meeting-workbench-ask", daemon=True).start()


class AskRegistry:
    """计划和任务（只在内存里，按最近使用淘汰）。计划号和任务号都是 secrets.token_urlsafe(16)。"""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        plan_ttl: float = PLAN_TTL,
        job_ttl: float = JOB_TTL,
        max_plans: int = MAX_PLANS,
        max_jobs: int = MAX_JOBS,
        slots: int = SLOTS,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        *,
        slot_wait: float = SLOT_WAIT,
        call_timeout: float = 90.0 + TIMEOUT_GRACE,
    ):
        self.clock = clock
        self.plan_ttl = plan_ttl
        self.job_ttl = job_ttl
        self.max_plans = max_plans
        self.max_jobs = max_jobs
        self.spawn = spawn or _spawn_daemon
        self.slot_wait = slot_wait
        self.call_timeout = call_timeout
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(slots)
        self._plans: OrderedDict[str, _Plan] = OrderedDict()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._running: dict[str, str] = {}
        self.closed = False

    # ------------------------------------------------------------------ 计划

    def put_plan(self, project_id: str, plan: Plan, llm_state: str) -> str:
        plan_id = secrets.token_urlsafe(16)
        with self._lock:
            self._plans[plan_id] = _Plan(project_id, plan, self.clock(), llm_state)
            while len(self._plans) > self.max_plans:
                self._plans.popitem(last=False)
        return plan_id

    def get_plan(self, project_id: str, plan_id: str) -> _Plan | None:
        """别的项目的计划号当不存在；过期的删掉。"""
        with self._lock:
            entry = self._plans.get(plan_id)
            if entry is None:
                return None
            if self.clock() - entry.created > self.plan_ttl:
                del self._plans[plan_id]
                return None
            if entry.project_id != project_id:
                return None
            self._plans.move_to_end(plan_id)
            return entry

    # ------------------------------------------------------------------ 任务

    def _expire_locked(self, job: _Job) -> None:
        """在等的任务过了截止：记 timeout，立刻放掉名额和项目锁。"""
        if job.state != "waiting":
            return
        now = self.clock()
        started = job.call_started
        if started is not None:
            late = now - started > self.call_timeout
        else:
            late = now - job.created > self.slot_wait + self.call_timeout
        if late:
            self._finish_locked(job, "stopped", {"reason": "timeout"})

    def _finish_locked(self, job: _Job, state: str, payload: dict[str, Any]) -> bool:
        if job.state != "waiting":
            return False
        # 先写 payload 再写 state：锁外看到 state 变了时 payload 一定已经在了
        job.payload = payload
        job.state = state
        job.finished = self.clock()
        if job.holds_slot:
            job.holds_slot = False
            self._slots.release()
        if self._running.get(job.project_id) == job.id:
            del self._running[job.project_id]
        return True

    def start_job(self, entry: _Plan, with_materials: bool) -> _Job | None:
        """同一个项目已有在回答的问题时回 None（409）。"""
        with self._lock:
            current = self._running.get(entry.project_id)
            if current is not None and current in self._jobs:
                self._expire_locked(self._jobs[current])
            if entry.project_id in self._running:
                return None
            job = _Job(
                secrets.token_urlsafe(16), entry.project_id, entry, with_materials, self.clock()
            )
            self._jobs[job.id] = job
            self._running[entry.project_id] = job.id
            self._evict_locked()
            return job

    def project_busy(self, project_id: str) -> bool:
        with self._lock:
            current = self._running.get(project_id)
            if current is not None and current in self._jobs:
                self._expire_locked(self._jobs[current])
            return project_id in self._running

    def _evict_locked(self) -> None:
        now = self.clock()
        for job_id, job in list(self._jobs.items()):
            if job.finished is not None and now - job.finished > self.job_ttl:
                del self._jobs[job_id]
        # 超过上限时先淘汰最久没用的已完成任务；在答的不淘汰
        while len(self._jobs) > self.max_jobs:
            victim = next(
                (job_id for job_id, job in self._jobs.items() if job.state != "waiting"), None
            )
            if victim is None:
                break
            del self._jobs[victim]

    def get_job(self, job_id: str) -> _Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            self._expire_locked(job)
            if job.finished is not None and self.clock() - job.finished > self.job_ttl:
                del self._jobs[job_id]
                return None
            self._jobs.move_to_end(job_id)
            return job

    def view_job(self, job_id: str) -> _JobView | None:
        """轮询用：在锁里复制 (state, payload, plan, with_materials)，锁外组装返回时不会拿到半截。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            self._expire_locked(job)
            if job.finished is not None and self.clock() - job.finished > self.job_ttl:
                del self._jobs[job_id]
                return None
            self._jobs.move_to_end(job_id)
            return _JobView(job.state, dict(job.payload), job.plan.plan, job.with_materials)

    def acquire_slot(self, job: _Job) -> bool:
        """最多等 slot_wait 秒拿位置；拿到后记下调用开始的时间。任务已经停了（超时、关闭）时放回。"""
        if not self._slots.acquire(timeout=self.slot_wait):
            return False
        with self._lock:
            if job.state != "waiting" or self.closed:
                self._slots.release()
                return False
            job.holds_slot = True
            job.call_started = self.clock()
        return True

    def finish(self, job: _Job, state: str, payload: dict[str, Any]) -> bool:
        """线程晚回来（已经记了 timeout）时结果丢掉，回 False。"""
        with self._lock:
            if self.closed:
                return False
            return self._finish_locked(job, state, payload)

    def close(self) -> None:
        """lifespan 关闭时调：在跑的线程让它自己结束，结果丢掉。"""
        with self._lock:
            self.closed = True
            for job in self._jobs.values():
                if job.state == "waiting":
                    self._finish_locked(job, "stopped", {"reason": "error"})


class AskService:
    """接口背后的三件事：prepare、ask、job。worker 是 4a 的 LinksLLMWorker（charge/refund/usage_today）。"""

    def __init__(
        self,
        db: Any,
        settings: Any,
        *,
        worker: Any,
        semantic: Any | None = None,
        vectors: Any | None = None,
        busy: Callable[[], Any] = lambda: False,
        registry: AskRegistry | None = None,
        chat: Callable[..., llm.ChatReply] | None = None,
        state_of: Callable[[str], str] = volume_state,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.db = db
        self.settings = settings
        self.worker = worker
        self.semantic = semantic
        self.vectors = vectors
        self.busy = busy
        self.registry = registry or AskRegistry(
            call_timeout=float(settings.qa_timeout_seconds) + TIMEOUT_GRACE
        )
        self.chat = chat
        self.state_of = state_of
        self.clock = clock

    # ------------------------------------------------------------------ 状态

    def llm_state(self) -> str:
        """ok、no_key（key 读不出）、off（qa_daily_questions 为 0）、capped（今天到了上限）。"""
        if not llm.llm_ready(self.settings):
            return "no_key"
        limit = int(self.settings.qa_daily_questions)
        if limit <= 0:
            return "off"
        if int(self.worker.usage_today().get("qa", 0)) >= limit:
            return "capped"
        return "ok"

    # ------------------------------------------------------------------ prepare

    def prepare(self, project_id: str, question: str) -> dict[str, Any]:
        try:
            text = clean_question(question)
        except QuestionError as error:
            raise AskError(422, error.text) from None
        started = self.clock()
        connection = self.db.connect()
        try:
            plan = retrieve(
                connection,
                project_id,
                text,
                settings=self.settings,
                semantic=self.semantic,
                vectors=self.vectors,
                busy=self.busy,
                state_of=self.state_of,
            )
        except ProjectMissing:
            raise AskError(404, PROJECT_MISSING) from None
        finally:
            connection.close()
        state = self.llm_state()
        destination = llm.destination(self.settings)
        notes = list(plan.notes)
        if destination.local and state == "ok":
            notes.append("local_model")
        plan.notes = notes
        plan_id = self.registry.put_plan(project_id, plan, state)
        counts = plan.counts
        confirm = None
        if counts["materials"] > 0 and state == "ok":
            confirm = {
                "text": f"将发送 {counts['materials']} 段材料原文给 {destination.host}",
                "host": destination.host,
            }
        logger.info(
            "问答找原文：项目 %s，会议 %d 段、材料 %d 段，用时 %.2f 秒%s",
            project_id,
            counts["meetings"],
            counts["materials"],
            self.clock() - started,
            "（没找完）" if plan.partial else "",
        )
        return {
            "plan_id": plan_id,
            "expires_in": int(self.registry.plan_ttl),
            "question": plan.question,
            "counts": counts,
            "confirm": confirm,
            "local_model": destination.local,
            "llm": state,
            "highlight": plan.terms.highlight,
            "sources": plan.sources,
            "notes": _notes(notes),
            "unattributed_meetings": plan.unattributed,
        }

    # ------------------------------------------------------------------ ask

    def ask(self, project_id: str, plan_id: str, with_materials: bool) -> dict[str, Any]:
        """以下都在计数之前：404 计划、503 没 key、503 关着、404 带材料却没确认过、409 在答、422 没有可发的，
        最后 429（charge）。charge 之后起线程失败时退回这一次、放开项目锁，回 503。"""
        entry = self.registry.get_plan(project_id, plan_id)
        if entry is None:
            raise AskError(404, PLAN_EXPIRED)
        if not llm.llm_ready(self.settings):
            raise AskError(503, NO_KEY)
        if int(self.settings.qa_daily_questions) <= 0:
            raise AskError(503, QA_OFF)
        if with_materials and entry.llm != "ok":
            # prepare 时 AI 不能用，页面上没写「将发送 N 段材料原文给 …」那一行：不带着材料发，让它重新找一遍
            raise AskError(404, PLAN_EXPIRED)
        if self.registry.project_busy(project_id):
            raise AskError(409, PROJECT_BUSY)
        chosen = [
            source
            for source in entry.plan.sources
            if with_materials or source["kind"] != "material"
        ]
        if not chosen:
            raise AskError(422, NOTHING_TO_SEND)
        if not self.worker.charge("qa"):
            raise AskError(429, CAPPED)
        job = self.registry.start_job(entry, with_materials)
        if job is None:
            # 刚好同一个项目的另一问抢先开始：这一次不算
            self._refund()
            raise AskError(409, PROJECT_BUSY)
        try:
            self.registry.spawn(lambda: self._run(job))
        except Exception as error:  # noqa: BLE001  线程起不来：退回这一次、结束任务、放开项目锁和名额
            logger.warning("问答没起来：项目 %s，%s", job.project_id, type(error).__name__)
            self._refund()
            self.registry.finish(job, "stopped", {"reason": "error"})
            raise AskError(503, STOP_TEXTS["error"][0]) from None
        return {"job_id": job.id, "state": "waiting", "text": WAITING_TEXT}

    def _run(self, job: _Job) -> None:
        entry = job.plan
        plan = entry.plan
        counts = plan.counts
        sent_materials = counts["materials"] if job.with_materials else 0
        if not self.registry.acquire_slot(job):
            self._refund()
            self.registry.finish(job, "stopped", {"reason": "slots"})
            logger.info("问答没拿到位置：项目 %s，结果 slots", job.project_id)
            return
        started = self.clock()
        code = "error"
        payload: dict[str, Any] = {"reason": "error"}
        state = "stopped"
        try:
            prompt = build_prompt(plan, with_materials=job.with_materials)
            chat = self.chat or llm.chat
            reply = chat(
                self.settings,
                system=prompt.system,
                user=prompt.user,
                json_mode=False,
                max_tokens=MAX_TOKENS,
                timeout=float(self.settings.qa_timeout_seconds),
                retries=0,
                temperature=0.2,
            )
            parsed = parse_answer(reply.text, prompt.sent_ids, finish_reason=reply.finish_reason)
            state = "done"
            code = (
                "no_evidence" if parsed.no_evidence else ("found" if parsed.found else "not_found")
            )
            payload = {
                "answer": {
                    # 说找到了却没有有效出处的回答不显示
                    "text": "" if parsed.no_evidence else parsed.text,
                    "cited": list(parsed.cited),
                    "found": parsed.found,
                    "no_evidence": parsed.no_evidence,
                    "truncated": parsed.truncated,
                },
                "sent": {"meetings": counts["meetings"], "materials": sent_materials},
            }
        except llm.LLMError as error:
            code = _REASONS.get(error.code, "error")
            payload = {"reason": code}
            if error.code in _NO_RETRY_CODES:
                payload["retry"] = False
            if not error.sent:
                self._refund()
        except Exception as error:  # noqa: BLE001  任何意外都停下，只记类型名
            code = "error"
            payload = {"reason": "error"}
            logger.warning("问答出错：项目 %s，%s", job.project_id, type(error).__name__)
        kept = self.registry.finish(job, state, payload)
        logger.info(
            "问答回来：项目 %s，会议 %d 段、材料 %d 段，用时 %.1f 秒，结果 %s%s",
            job.project_id,
            counts["meetings"],
            sent_materials,
            self.clock() - started,
            code,
            "" if kept else "（已经停下，丢掉）",
        )

    def _refund(self) -> None:
        try:
            self.worker.refund("qa")
        except Exception as error:  # noqa: BLE001
            logger.warning("问答退回用量没成：%s", type(error).__name__)

    # ------------------------------------------------------------------ 轮询

    def job(self, job_id: str) -> dict[str, Any]:
        job = self.registry.view_job(job_id)
        if job is None:
            raise AskError(404, JOB_EXPIRED)
        if job.state == "waiting":
            return {"state": "waiting", "text": WAITING_TEXT}
        plan = job.plan
        sources = [
            {**source, "sent": job.with_materials or source["kind"] != "material"}
            for source in plan.sources
        ]
        if job.state == "done":
            return {
                "state": "done",
                **job.payload,
                "sources": sources,
                "notes": _notes(plan.notes),
                "local_model": llm.destination(self.settings).local,
            }
        reason = str(job.payload.get("reason") or "error")
        text, retry = STOP_TEXTS.get(reason, STOP_TEXTS["error"])
        return {
            "state": "stopped",
            "reason": reason,
            "text": text.format(seconds=_seconds(self.settings.qa_timeout_seconds)),
            "retry": retry and job.payload.get("retry", True) is not False,
            "sources": sources,
        }

    def close(self) -> None:
        self.registry.close()


def _seconds(value: float) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


def _notes(kinds: list[str]) -> list[dict[str, str]]:
    return [{"kind": kind, "text": NOTE_TEXTS[kind]} for kind in kinds if kind in NOTE_TEXTS]
