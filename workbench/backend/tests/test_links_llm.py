"""第四期 4a：links_llm_loop 的框架（启用条件、每次最多一个调用、认领回收、错误三类、退避、停下和恢复、
当天用量）和 POST /api/links/retry。4b、4c 的两种调用在这里用假的 LLMTask 代替。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from meeting_workbench import links_llm, loose_mentions
from meeting_workbench.db import Database, utc_now
from meeting_workbench.links_llm import LinksLLMWorker, claim_stamp
from meeting_workbench.llm import LLMError

from .test_file_mentions import setup as fm_setup
from .test_graph import add_meeting
from .test_loose_mentions import NOW as LOOSE_NOW
from .test_loose_mentions import FakeChat, talk
from .test_loose_mentions import settings as loose_settings
from .test_material_index import make

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


class FakeTask:
    """假的一种调用：jobs 是待办单位；run 按 outcomes 依次回 True、False 或抛 LLMError。"""

    def __init__(self, name="mentions_recent", jobs=None, outcomes=None):
        self.name = name
        self.jobs = list(jobs if jobs is not None else ["m1"])
        self.outcomes = list(outcomes or [True])
        self.claimed: list = []
        self.ran: list = []
        self.released: list = []
        self.failed: list = []

    def claim(self, db, now):
        if not self.jobs:
            return None
        job = self.jobs.pop(0)
        self.claimed.append(job)
        return job

    def run(self, job):
        self.ran.append(job)
        outcome = self.outcomes[min(len(self.ran) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def release(self, db, job):
        self.released.append(job)
        self.jobs.insert(0, job)

    def fail(self, db, job, code):
        self.failed.append((job, code))


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def settings(tmp_path, *, key=True, **overrides):
    key_file = tmp_path / "api-key"
    if key:
        key_file.write_text("sk-test", encoding="utf-8")
    values = {
        "links_enabled": True,
        "links_llm_enabled": True,
        "links_llm_daily_calls": 200,
        "qa_daily_questions": 100,
        "llm_api_key_file": key_file,
        "llm_api_base": "http://127.0.0.1:9/v1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def worker(tmp_path, tasks=(), *, today=date(2026, 9, 27), clock=None, **overrides):
    db, _ = make(tmp_path)
    day = {"value": today}
    built = LinksLLMWorker(
        db,
        settings(
            tmp_path,
            **{k: v for k, v in overrides.items() if k != "key"},
            key=overrides.get("key", True),
        ),
        tasks=tasks,
        clock=clock or Clock(),
        now=lambda: NOW,
        today=lambda: day["value"],
    )
    built.day = day
    return built


def usage(db):
    row = db.query_one("SELECT value FROM app_state WHERE key = 'links_llm_usage'")
    return json.loads(row["value"]) if row else None


def test_no_call_without_key_when_off_or_capped(tmp_path):
    task = FakeTask()
    assert worker(tmp_path / "a", [task], key=False).tick()["state"] == "no_key"
    assert worker(tmp_path / "b", [task], links_llm_enabled=False).tick()["state"] == "off"
    assert worker(tmp_path / "c", [task], links_enabled=False).tick()["state"] == "off"
    assert worker(tmp_path / "d", [task], links_llm_daily_calls=0).tick()["state"] == "capped"
    assert task.claimed == [] and task.ran == []


def test_cap_and_new_day(tmp_path):
    task = FakeTask(jobs=["m1", "m2", "m3"])
    w = worker(tmp_path, [task], links_llm_daily_calls=2)
    assert [w.tick()["called"] for _ in range(3)] == [True, True, False]
    assert usage(w.db) == {"day": "2026-09-27", "background": 2, "qa": 0}
    assert task.ran == ["m1", "m2"] and w.status() == "capped"
    # 换了一天从零开始
    w.day["value"] = date(2026, 9, 28)
    assert w.tick()["called"] is True
    assert usage(w.db) == {"day": "2026-09-28", "background": 1, "qa": 0}


def test_at_most_one_call_per_tick_in_task_order(tmp_path):
    first = FakeTask("mentions_recent", jobs=[])
    pairs = FakeTask("pairs", jobs=["p1"])
    backfill = FakeTask("mentions_backfill", jobs=["b1"])
    w = worker(tmp_path, [first, pairs, backfill])
    assert w.tick() == {"called": True, "state": "ok"}
    assert (pairs.ran, backfill.claimed) == (["p1"], [])
    assert w.tick()["called"] is True and backfill.ran == ["b1"]
    assert w.tick() == {"called": False, "state": "idle"}


def test_stale_claims_come_back_without_counting(tmp_path):
    w = worker(tmp_path)
    db = w.db
    for meeting_id in ("old", "fresh"):
        add_meeting(db, meeting_id, ago=1, project_id="p")
    old = claim_stamp(NOW - timedelta(minutes=11))
    fresh = claim_stamp(NOW - timedelta(minutes=5))
    for meeting_id, stamp in (("old", old), ("fresh", fresh)):
        db.execute(
            """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, attempts, parts_done,
                   claimed_at, created_at, updated_at)
               VALUES (?, 'v', 's', 'running', 1, 2, ?, ?, ?)""",
            (meeting_id, stamp, utc_now(), utc_now()),
        )
        db.execute(
            """INSERT INTO decision_scan(meeting_id, pair_state, pair_attempts, pair_claimed_at, updated_at)
               VALUES (?, 'running', 2, ?, ?)""",
            (meeting_id, stamp, utc_now()),
        )

    w.tick()

    extractions = {
        row["meeting_id"]: row for row in db.query_all("SELECT * FROM mention_extractions")
    }
    assert extractions["old"]["state"] == "pending" and extractions["old"]["claimed_at"] is None
    assert (extractions["old"]["attempts"], extractions["old"]["parts_done"]) == (1, 2)
    assert extractions["fresh"]["state"] == "running"
    scans = {row["meeting_id"]: row for row in db.query_all("SELECT * FROM decision_scan")}
    assert scans["old"]["pair_state"] == "pending" and scans["old"]["pair_attempts"] == 2
    assert scans["fresh"]["pair_state"] == "running"


def test_bad_content_and_bad_request_count_against_the_meeting(tmp_path):
    task = FakeTask(
        jobs=["m1", "m1", "m1"], outcomes=[False, LLMError("bad_request", status=400), False]
    )
    w = worker(tmp_path, [task])
    assert [w.tick()["state"] for _ in range(3)] == ["invalid", "bad_request", "invalid"]
    assert task.failed == [("m1", "invalid"), ("m1", "bad_request"), ("m1", "invalid")]
    assert task.released == [] and w.status() == "ok"
    assert usage(w.db)["background"] == 3


def test_network_outage_backs_off_without_counting_the_meeting(tmp_path):
    clock = Clock()
    task = FakeTask(jobs=["m1"], outcomes=[LLMError("network", sent=True)])
    w = worker(tmp_path, [task], clock=clock)
    calls: list[float] = []
    # 模拟断网 75 分钟，每 5 秒循环一次
    while clock.value <= 75 * 60:
        if w.tick()["called"]:
            calls.append(clock.value)
        clock.value += 5
    # 退避 1 分钟、5 分钟、30 分钟，之后每 30 分钟：每个台阶最多一次调用
    assert calls == [0, 60, 360, 2160, 3960]
    assert task.failed == [] and len(task.released) == len(calls)
    assert w.status() == "backoff"
    # 30 分钟内连续 3 次才算 failing
    clock2 = Clock()
    w2 = worker(tmp_path / "second", [FakeTask(outcomes=[LLMError("timeout")])], clock=clock2)
    for moment in (0, 60, 360):
        clock2.value = moment
        assert w2.tick()["called"] is True
    assert w2.status() == "failing" and w2.snapshot()["llm"] == "failing"
    # 成功一次就清零
    w2.tasks[0].outcomes = [True]
    clock2.value = 360 + 1800
    assert w2.tick()["state"] == "ok" and w2.status() == "ok"


def test_balance_stops_until_the_key_file_changes(tmp_path):
    task = FakeTask(jobs=["m1"], outcomes=[LLMError("balance", status=402)])
    w = worker(tmp_path, [task])
    assert w.tick() == {"called": True, "state": "balance"}
    assert w.status() == "balance" and task.failed == [] and task.released == ["m1"]
    for _ in range(3):
        assert w.tick() == {"called": False, "state": "paused"}
    key = w.settings.llm_api_key_file
    stat = key.stat()
    os.utime(key, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    task.outcomes = [True]
    assert w.tick() == {"called": True, "state": "ok"}


def test_auth_stops_and_retry_clears_it(tmp_path):
    task = FakeTask(jobs=["m1"], outcomes=[LLMError("auth", status=401), True])
    w = worker(tmp_path, [task])
    assert w.tick()["state"] == "auth" and w.status() == "auth"
    assert w.tick()["state"] == "paused"
    w.clear_pause()
    assert w.tick() == {"called": True, "state": "ok"}


def test_refund_when_the_request_never_left(tmp_path):
    task = FakeTask(
        jobs=["m1", "m2", "m3"],
        outcomes=[
            LLMError("network", sent=False),
            LLMError("no_key", sent=False),
            LLMError("timeout", sent=True),
        ],
    )
    w = worker(tmp_path, [task])
    w.tick()
    assert usage(w.db)["background"] == 0
    w.clear_pause()
    w.tick()
    assert usage(w.db)["background"] == 0 and w.status() == "no_key"
    w.clear_pause()
    w.tick()
    # 超时照算：服务器可能已经算完、计了费
    assert usage(w.db)["background"] == 1


def test_two_concurrent_charges_at_99_of_100(tmp_path):
    w = worker(tmp_path, qa_daily_questions=100)
    w.db.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES ('links_llm_usage', ?, ?)",
        (json.dumps({"day": "2026-09-27", "background": 0, "qa": 99}), utc_now()),
    )
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def go():
        barrier.wait()
        results.append(w.charge("qa"))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert usage(w.db) == {"day": "2026-09-27", "background": 0, "qa": 100}
    # 两个上限分开算
    assert w.charge("background") is True
    assert w.usage_today() == {"day": "2026-09-27", "background": 1, "qa": 100}
    assert w.snapshot()["calls_today"] == {"background": 1, "qa": 100}


def test_qa_off_when_limit_is_zero(tmp_path):
    w = worker(tmp_path, qa_daily_questions=0)
    assert w.charge("qa") is False and usage(w.db) is None


def test_loop_cadence(tmp_path):
    w = worker(tmp_path)
    results = iter([{"called": True}, {"called": False}])
    stop = threading.Event()
    slept: list[float] = []

    def tick():
        try:
            return next(results)
        except StopIteration:
            stop.set()
            return {"called": False}

    w.tick = tick

    async def sleep(seconds):
        slept.append(seconds)

    asyncio.run(links_llm.links_llm_loop(w, stop, sleep=sleep))
    # 第一轮前 30 秒，调过 5 秒，没活 60 秒（按 1 秒一段等）
    assert slept[:30] == [1.0] * 30
    assert len(slept) == 30 + 5 + 60


def test_retry_endpoint_requeues_and_clears_the_pause(tmp_path):
    from .test_tasks_api import make_client, write_headers

    client, settings_ = make_client(tmp_path)
    headers = write_headers(client)
    db = Database(settings_.database_path)
    for meeting_id in ("m1", "m2", "m3"):
        add_meeting(db, meeting_id, ago=1)
    db.execute(
        """INSERT INTO mention_extractions(meeting_id, version_id, text_sha, state, attempts, error,
               created_at, updated_at) VALUES ('m1', 'v', 's', 'failed', 3, 'invalid', ?, ?)""",
        (utc_now(), utc_now()),
    )
    db.execute(
        """INSERT INTO decision_scan(meeting_id, pair_state, pair_attempts, pair_after, pair_error, updated_at)
           VALUES ('m2', 'failed', 3, '2026-09-27T09:00:00+00:00', 'invalid', ?),
                  ('m3', 'done', 0, NULL, NULL, ?)""",
        (utc_now(), utc_now()),
    )
    llm_worker = client.app.state.links_llm_worker
    llm_worker._pause("auth")

    response = client.post("/api/links/retry", json={}, headers=headers)

    assert response.status_code == 200 and response.json() == {"requeued": 2}
    assert db.query_one("SELECT state, attempts, error FROM mention_extractions") == {
        "state": "pending",
        "attempts": 0,
        "error": None,
    }
    assert db.query_all(
        "SELECT meeting_id, pair_state, pair_attempts, pair_after FROM decision_scan ORDER BY 1"
    ) == [
        {"meeting_id": "m2", "pair_state": "pending", "pair_attempts": 0, "pair_after": None},
        {"meeting_id": "m3", "pair_state": "done", "pair_attempts": 0, "pair_after": None},
    ]
    assert llm_worker._paused is None
    # 写入校验照旧：不带 CSRF 不行
    assert client.post("/api/links/retry", json={}).status_code == 403


def test_status_values(tmp_path):
    w = worker(tmp_path)
    w.refresh_state()
    assert w.status() == "ok"
    assert worker(tmp_path / "x", links_llm_enabled=False).status() == "off"
    nokey = worker(tmp_path / "y", key=False)
    nokey.refresh_state()
    assert nokey.status() == "no_key"


@pytest.mark.parametrize("code", ["server", "rate_limited", "timeout"])
def test_every_backoff_code_releases_the_job(tmp_path, code):
    task = FakeTask(outcomes=[LLMError(code)])
    w = worker(tmp_path, [task])
    assert w.tick()["state"] == code
    assert task.released == ["m1"] and task.failed == [] and w.status() == "backoff"


def test_unexpected_error_in_a_task_releases_the_job_and_backs_off(tmp_path):
    task = FakeTask(jobs=["m1"], outcomes=[RuntimeError("任务自己的 bug")])
    w = worker(tmp_path, [task])
    with pytest.raises(RuntimeError):
        w.tick()
    # 这份活放回去了，不会卡在认领状态；循环整体退避，不在 60 秒后接着耗当天的用量
    assert task.released == ["m1"] and task.failed == []
    assert w.tick() == {"called": False, "state": "backoff"}


# ---------------------------------------------------------------------- 4b：放宽的提到放进这个循环


def test_loose_mentions_seed_only_qualified_meetings(tmp_path):
    db, _root = fm_setup(tmp_path)
    add_meeting(db, "good", ago=1, project_id="p", segments=talk())
    add_meeting(db, "short", ago=1, project_id="p", segments=[(0, "报价单再看一下")])
    add_meeting(db, "old", ago=200, project_id="p", segments=talk())
    add_meeting(db, "loose", ago=1, segments=talk())
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '别的项目', ?)", (utc_now(),)
    )
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('q', '/没收完', ?)",
        (utc_now(),),
    )
    add_meeting(db, "unindexed", ago=1, project_id="q", segments=talk())

    assert loose_mentions.seed(db, loose_settings(tmp_path), LOOSE_NOW) == 1
    assert [
        row["meeting_id"] for row in db.query_all("SELECT meeting_id FROM mention_extractions")
    ] == ["good"]
    # 已经有行的不再建；300 天回补的设置能补到老会
    assert loose_mentions.seed(db, loose_settings(tmp_path), LOOSE_NOW) == 0
    assert (
        loose_mentions.seed(db, loose_settings(tmp_path, links_backfill_days=300), LOOSE_NOW) == 1
    )
    # 0 表示只做新会：以 links_since 为界
    db.execute("DELETE FROM mention_extractions")
    db.execute("UPDATE app_state SET value = '2026-09-25T00:00:00+00:00' WHERE key = 'links_since'")
    assert loose_mentions.seed(db, loose_settings(tmp_path, links_backfill_days=0), LOOSE_NOW) == 1


def test_order_recent_then_pairs_then_backfill_and_one_shared_cap(tmp_path):
    db, _root = fm_setup(tmp_path)
    add_meeting(db, "recent", ago=1, project_id="p", segments=talk())
    add_meeting(db, "older", ago=30, project_id="p", segments=talk())
    chat = FakeChat({"refs": []})
    cfg = loose_settings(tmp_path, links_llm_daily_calls=3)
    pairs = FakeTask(name="pairs", jobs=["d1"])
    tasks = links_llm.ordered(
        [
            loose_mentions.LooseMentionTask(cfg, loose_mentions.TASK_BACKFILL, chat=chat),
            pairs,
            loose_mentions.LooseMentionTask(cfg, loose_mentions.TASK_RECENT, chat=chat),
        ]
    )
    assert [task.name for task in tasks] == list(links_llm.TASK_ORDER)
    worker = LinksLLMWorker(
        db, cfg, tasks=tasks, now=lambda: LOOSE_NOW, today=lambda: date(2026, 9, 26)
    )
    states = [
        db.query_one("SELECT state FROM mention_extractions WHERE meeting_id = ?", (m,))
        for m in ("recent", "older")
    ]
    assert states == [None, None]
    assert worker.tick()["called"] is True
    assert db.query_one("SELECT state FROM mention_extractions WHERE meeting_id = 'recent'") == {
        "state": "done"
    }
    assert worker.tick()["called"] is True and pairs.ran == ["d1"]
    assert worker.tick()["called"] is True
    assert db.query_one("SELECT state FROM mention_extractions WHERE meeting_id = 'older'") == {
        "state": "done"
    }
    # 三种一起用每天的上限
    pairs.jobs.append("d2")
    assert worker.tick()["state"] == "capped" and pairs.ran == ["d1"]
    assert usage(db)["background"] == 3


def test_malformed_key_file_pauses_as_auth_without_charging_or_logging_the_key(tmp_path, caplog):
    """key 文件多了一行：请求没发出去，不扣用量；循环按 key 不对停下（不是退避）；日志里没有 key。"""
    from meeting_workbench import llm

    secret = "sk-FAKE-0123456789abcdef"

    class ChatTask(FakeTask):
        def run(self, job):
            self.ran.append(job)
            llm.chat(self.settings, system="s", user="u", json_mode=True, max_tokens=10, retries=0)
            return True

    task = ChatTask(jobs=["m1"])
    w = worker(tmp_path, [task])
    task.settings = w.settings
    w.settings.llm_api_key_file.write_text(f"{secret}\nold-key-xyz\n", encoding="utf-8")
    caplog.set_level("DEBUG")
    for _ in range(3):
        w.tick()
    assert task.ran == ["m1"] and task.released == ["m1"]
    assert usage(w.db)["background"] == 0
    assert w.status() == "auth"
    assert secret not in caplog.text
