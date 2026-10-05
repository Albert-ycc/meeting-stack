"""后台扫描的空闲退避：间隔怎么拉长、什么会把它打断、退避期间新文件出现得有多快。"""

import asyncio
import functools
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from meeting_workbench import main as main_module
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.importer import ArchiveImporter, ScanReport
from meeting_workbench.main import create_app
from meeting_workbench.tasks import TaskService
from meeting_workbench.scan_pacing import (
    API,
    CHANGED,
    RELAY,
    TIMER,
    IdleBackoff,
    ScanPacer,
    did_work,
    iter_source_files,
    seconds_until_daily,
    source_fingerprint,
)

BEIJING = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------- 间隔怎么算


def test_backoff_keeps_base_interval_during_grace_then_doubles_up_to_cap():
    backoff = IdleBackoff(base=15, cap=600)
    seen = []
    for _ in range(10):
        backoff.record(active=False)
        seen.append(backoff.interval)
    # 前两轮仍是原节奏，之后 30、60、120、240、480，最后封顶在 600
    assert seen == [15, 15, 30, 60, 120, 240, 480, 600, 600, 600]


def test_backoff_resets_to_base_when_a_round_does_work():
    backoff = IdleBackoff(base=15, cap=600)
    for _ in range(8):
        backoff.record(active=False)
    assert backoff.interval == 600
    assert backoff.backed_off

    backoff.record(active=True)

    assert backoff.interval == 15
    assert not backoff.backed_off


def test_backoff_never_exceeds_cap_however_long_it_stays_idle():
    backoff = IdleBackoff(base=15, cap=600)
    for _ in range(100_000):
        backoff.record(active=False)
    assert backoff.interval == 600


@pytest.mark.parametrize("cap", [0, 15, 10])
def test_backoff_is_off_when_cap_is_not_above_base(cap):
    backoff = IdleBackoff(base=15, cap=cap)
    for _ in range(20):
        backoff.record(active=False)
    assert backoff.interval == 15
    assert not ScanPacer(backoff).enabled


def test_did_work_counts_only_work_done_not_backlog_or_flags():
    assert did_work(3)
    assert not did_work(0)
    assert did_work({"written": 2, "pending": 9}, "written")
    # 积压数、被跳过的数不算做了事：一个永远处理不掉的条目不能让循环永远退不了
    assert not did_work({"written": 0, "pending": 9, "skipped_no_key": 1}, "written")
    assert not did_work({"done": True}, "reevaluated")
    assert not did_work(None, "written")
    assert not did_work([1, 2], "written")


def test_scan_report_counts_only_imports_as_work_not_standing_errors():
    assert not ScanReport(errors=4, quarantined=2, stale_cleanup_skipped=1).imported_anything
    assert ScanReport(versions_imported=1).imported_anything
    assert ScanReport(meetings_created=1).imported_anything
    assert ScanReport(conflicts=1).imported_anything


def test_seconds_until_daily_is_always_strictly_in_the_future():
    before = datetime(2026, 10, 5, 8, 59, 0, tzinfo=BEIJING)
    exactly = datetime(2026, 10, 5, 9, 0, 0, tzinfo=BEIJING)
    after = datetime(2026, 10, 5, 9, 0, 1, tzinfo=BEIJING)

    assert seconds_until_daily(9, 0, BEIJING, now=before) == 60
    assert seconds_until_daily(9, 0, BEIJING, now=exactly) == 24 * 3600
    assert seconds_until_daily(9, 0, BEIJING, now=after) == 24 * 3600 - 1
    # 传进来的时间在别的时区也按目标时区算
    assert seconds_until_daily(9, 0, BEIJING, now=before.astimezone(ZoneInfo("UTC"))) == 60


# ---------------------------------------------------------------------- 归档指纹


def make_tree(tmp_path):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    meeting = archive / "260701 周会"
    meeting.mkdir(parents=True)
    (meeting / "vm-20260701-100000.m4a").write_bytes(b"audio")
    (meeting / "vm-20260701-100000.srt").write_text("1\n", encoding="utf-8")
    (meeting / "meeting.md").write_text("# 纪要", encoding="utf-8")
    staging.mkdir()
    (staging / "vm-20260702-100000").mkdir()
    (staging / "vm-20260702-100000" / "vm-20260702-100000.txt").write_text("hi", encoding="utf-8")
    return archive, staging


def test_fingerprint_is_stable_when_nothing_changes(tmp_path):
    archive, staging = make_tree(tmp_path)

    assert source_fingerprint((staging, archive)) == source_fingerprint((staging, archive))


def test_fingerprint_changes_when_a_source_file_is_added_resized_touched_or_removed(tmp_path):
    archive, staging = make_tree(tmp_path)
    roots = (staging, archive)
    seen = [source_fingerprint(roots)]

    new_file = archive / "260701 周会" / "extra.json"
    new_file.write_text("{}", encoding="utf-8")
    seen.append(source_fingerprint(roots))

    new_file.write_text('{"a": 1}', encoding="utf-8")  # 大小变了
    seen.append(source_fingerprint(roots))

    stat = new_file.stat()
    os.utime(new_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # 只有修改时间变了
    seen.append(source_fingerprint(roots))

    new_file.unlink()
    seen.append(source_fingerprint(roots))
    assert seen[-1] == seen[0]  # 删回去就回到起点

    # 每一步都和前一步不同（最后一步回到起点，和第一步相同，所以只比相邻的）
    assert all(left != right for left, right in zip(seen, seen[1:], strict=False))


def test_fingerprint_sees_a_root_appearing_and_disappearing(tmp_path):
    archive, staging = make_tree(tmp_path)
    with_archive = source_fingerprint((staging, archive))

    gone = source_fingerprint((staging, tmp_path / "unmounted"))

    assert gone != with_archive
    assert gone[1] == (False, 0, 0)


def test_fingerprint_ignores_what_the_importer_never_reads(tmp_path):
    archive, staging = make_tree(tmp_path)
    roots = (staging, archive)
    before = source_fingerprint(roots)
    meeting = archive / "260701 周会"

    (meeting / ".DS_Store").write_bytes(b"x")
    (meeting / "._meeting.md").write_bytes(b"x")  # AppleDouble
    (meeting / "cover.png").write_bytes(b"x")  # 不认的类型
    (meeting / ".workbench-history").mkdir()
    (meeting / ".workbench-history" / "old.json").write_text("{}", encoding="utf-8")
    (meeting / ".workbench-publish-abc").mkdir()
    (meeting / ".workbench-publish-abc" / "half.md").write_text("x", encoding="utf-8")
    (archive / ".obsidian").mkdir()  # 编辑器的工作区文件常被改
    (archive / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (archive / "funasr-poc-260708").mkdir()
    (archive / "funasr-poc-260708" / "poc.json").write_text("{}", encoding="utf-8")
    (meeting / "link.md").symlink_to(meeting / "meeting.md")

    assert source_fingerprint(roots) == before


def test_fingerprint_covers_drafts_and_pending_review_folders(tmp_path):
    archive, staging = make_tree(tmp_path)
    roots = (staging, archive)
    before = source_fingerprint(roots)

    draft = archive / ".workbench-drafts" / "job-1" / "attempt-1"
    draft.mkdir(parents=True)
    (draft / "meeting.md").write_text("# 草稿", encoding="utf-8")
    after_draft = source_fingerprint(roots)
    pending = archive / "待校对" / "260710 待看"
    pending.mkdir(parents=True)
    (pending / "meeting.md").write_text("# 待校对", encoding="utf-8")

    assert after_draft != before
    assert source_fingerprint(roots) != after_draft


def test_fingerprint_covers_every_file_the_importer_discovers(tmp_path):
    """指纹的范围只许比发现阶段大：发现阶段认的文件漏了一个，退避期间那个文件变了就没人知道。"""
    archive, staging = make_tree(tmp_path)
    draft = archive / ".workbench-drafts" / "job-1" / "attempt-1"
    draft.mkdir(parents=True)
    for name in ("vm-20260703-100000.m4a", "vm-20260703-100000.srt", "meeting.md"):
        (draft / name).write_bytes(b"x")
    pending = archive / "待校对" / "260710 待看"
    pending.mkdir(parents=True)
    (pending / "meeting.md").write_text("# 待校对", encoding="utf-8")
    (pending / "原文").write_text("x", encoding="utf-8")  # 无扩展名的「原文」也认
    history = archive / "meeting-relay-历史产物"
    (history / "vm-20260601-100000").mkdir(parents=True)
    (history / "vm-20260601-100000" / "vm-20260601-100000.txt").write_text("h", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )
    db = Database(settings.database_path)
    db.initialize()
    importer = ArchiveImporter(db, settings)
    report = ScanReport()
    discovered = importer._discover_root(staging, "staging", 10, report=report)
    discovered += importer._discover_archive(report=report)
    importer_files = {str(path) for bundle in discovered for path in bundle.files}
    assert importer_files  # 夹具确实造出了东西

    fingerprinted = {entry.path for entry in iter_source_files((staging, archive))}

    assert importer_files <= fingerprinted


def test_fingerprint_of_a_realistic_archive_is_cheap(tmp_path):
    archive = tmp_path / "archive"
    for index in range(150):
        meeting = archive / f"2606{index:02d} 会议{index}"
        meeting.mkdir(parents=True)
        for name in ("a.m4a", "a.srt", "a.txt", "a.json", "meeting.md"):
            (meeting / name).write_bytes(b"x")
    started = time.process_time()

    source_fingerprint((tmp_path / "none", archive))

    # 750 个文件：每隔 15 秒做一次，几十毫秒才不算空转；这里只拦数量级的退化
    assert time.process_time() - started < 0.5


# ---------------------------------------------------------------------- 节拍器


def pacer_for(**overrides):
    params = {"base": 0.03, "cap": 3.0, "grace_rounds": 0, "factor": 1000.0}
    backoff = IdleBackoff(**{**params, **{k: v for k, v in overrides.items() if k in params}})
    kwargs = {k: v for k, v in overrides.items() if k not in params}
    return ScanPacer(backoff, **kwargs)


async def backed_off(pacer):
    """跑一轮没做事的轮次，让间隔直接拉到上限。"""
    await pacer.snapshot()
    pacer.finish_round(active=False)
    assert pacer.backoff.interval == pacer.backoff.cap


def test_disabled_pacer_sleeps_the_base_interval_and_ignores_wakes():
    async def scenario():
        pacer = ScanPacer(IdleBackoff(base=0.05, cap=0))
        pacer.wake(API)
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started

    reason, elapsed = asyncio.run(scenario())

    assert reason == TIMER
    assert elapsed >= 0.045


def test_backed_off_wait_runs_to_the_cap_when_nothing_happens():
    async def scenario():
        pacer = pacer_for(cap=0.4, probe=lambda: "same")
        await backed_off(pacer)
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started

    reason, elapsed = asyncio.run(scenario())

    assert reason == TIMER
    assert 0.35 <= elapsed < 1.5


def test_api_write_wakes_a_backed_off_pacer_at_once_without_resetting_the_backoff():
    async def scenario():
        pacer = pacer_for(cap=5.0, probe=lambda: "same")
        await backed_off(pacer)
        await asyncio.sleep(0.05)  # 离上一轮结束已经超过最小间隔
        asyncio.get_running_loop().call_later(0.1, pacer.wake, API)
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started, pacer.backoff.interval

    reason, elapsed, interval = asyncio.run(scenario())

    assert reason == API
    assert elapsed < 1.0  # 不用等满 5 秒
    # 页面写操作只说「也许有后续」，这一轮真做了事才回到原节奏
    assert interval == 5.0


def test_relay_state_change_wakes_a_backed_off_pacer_and_resets_the_backoff():
    async def scenario():
        pacer = pacer_for(cap=5.0, probe=lambda: "same")
        await backed_off(pacer)
        await asyncio.sleep(0.05)
        asyncio.get_running_loop().call_later(0.1, pacer.wake, RELAY)
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started, pacer.backoff.interval

    reason, elapsed, interval = asyncio.run(scenario())

    assert reason == RELAY
    assert elapsed < 1.0
    assert interval == 0.03


def test_archive_change_is_noticed_within_one_base_interval_and_resets_the_backoff():
    state = {"fingerprint": "v1"}

    async def scenario():
        pacer = pacer_for(cap=5.0, probe=lambda: state["fingerprint"])
        await backed_off(pacer)
        asyncio.get_running_loop().call_later(0.2, state.__setitem__, "fingerprint", "v2")
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started, pacer.backoff.interval

    reason, elapsed, interval = asyncio.run(scenario())

    assert reason == CHANGED
    # 变化发生在 0.2 秒时，探测间隔是原间隔 0.03 秒：马上就能看到，远没到 5 秒的上限
    assert 0.18 <= elapsed < 1.0
    assert interval == 0.03


def test_change_during_a_round_is_caught_by_the_next_probe():
    """指纹在轮次开头取：这一轮读盘期间文件又变了，下一次探测就要发现。"""
    state = {"fingerprint": "v1"}

    async def scenario():
        pacer = pacer_for(cap=5.0, probe=lambda: state["fingerprint"])
        await pacer.snapshot()  # 轮次开头看到 v1
        state["fingerprint"] = "v2"  # 轮次进行中又变了
        pacer.finish_round(active=False)
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started

    reason, elapsed = asyncio.run(scenario())

    assert reason == CHANGED
    assert elapsed < 0.5


def test_unreadable_fingerprint_is_not_treated_as_a_change():
    def broken():
        raise OSError("盘掉了")

    async def scenario():
        pacer = pacer_for(cap=0.3, probe=broken)
        await pacer.snapshot()
        pacer.finish_round(active=False)
        return await pacer.wait()

    assert asyncio.run(scenario()) == TIMER


def test_wake_never_makes_two_rounds_closer_than_the_base_interval():
    async def scenario():
        pacer = pacer_for(base=0.2, cap=5.0, probe=lambda: "same")
        await backed_off(pacer)
        pacer.wake(API)  # 轮次进行中就有写操作进来了
        finished = time.monotonic()
        pacer._last_end = finished
        reason = await pacer.wait()
        return reason, time.monotonic() - finished

    reason, gap = asyncio.run(scenario())

    assert reason == API
    assert gap >= 0.18


def test_daily_deadline_cuts_a_long_backoff_short():
    async def scenario():
        pacer = pacer_for(cap=5.0, probe=lambda: "same", seconds_to_deadline=lambda: 0.2)
        await backed_off(pacer)
        started = time.monotonic()
        reason = await pacer.wait()
        return reason, time.monotonic() - started

    reason, elapsed = asyncio.run(scenario())

    assert reason == TIMER
    assert 0.15 <= elapsed < 1.5  # 点上醒来，不是等满 5 秒


def test_probe_is_not_run_while_the_pacer_is_not_backed_off():
    calls = []

    async def scenario():
        pacer = pacer_for(cap=3.0, grace_rounds=5, probe=lambda: calls.append(1) or "same")
        pacer.finish_round(active=False)
        assert not pacer.backoff.backed_off
        await pacer.wait()

    asyncio.run(scenario())

    assert calls == []


# ---------------------------------------------------------------------- 整个应用


class RelayStub:
    def __init__(self):
        self.counts = {"queued": 0, "active": 0, "failed": 0}
        self.broken = False

    def list_jobs(self, *, status=None, limit=200):
        return []

    def health(self):
        if self.broken:
            raise RuntimeError("relayctl 起不来")
        return {
            "status": "healthy",
            "mode": "controlled",
            "worker": {"state": "idle", "heartbeat_age_seconds": 0},
            "counts": dict(self.counts),
        }


def backoff_app(tmp_path, monkeypatch, relay=None, cap=4.0):
    # 一轮没做事就直接拉到上限，用例不用等好几圈
    monkeypatch.setattr(
        main_module, "IdleBackoff", functools.partial(IdleBackoff, grace_rounds=0, factor=1e6)
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        scan_interval_seconds=0.05,
        scan_idle_max_interval_seconds=cap,
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    app = create_app(settings, relay or RelayStub())
    scans = []
    real_scan = app.state.importer.scan

    def counting_scan(*args, **kwargs):
        scans.append(time.monotonic())
        return real_scan(*args, **kwargs)

    app.state.importer.scan = counting_scan
    return app, settings, scans


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


def scanner_interval(client):
    return client.get("/api/health").json()["details"]["scanner"]["interval_seconds"]


def settle(client, scans, cap=4.0):
    """等到扫描退避到上限，并且在上限这一档睡下（最近一轮扫描已经结束）。"""
    assert wait_until(lambda: scanner_interval(client) == cap)
    count = len(scans)
    time.sleep(0.4)
    assert len(scans) == count, "退避到上限之后不该再有扫描"
    return count


def add_probe_post(app, path, endpoint):
    app.post(path)(endpoint)
    app.router.routes.insert(0, app.router.routes.pop())


def write_headers(client):
    token = client.get("/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": "http://testserver"}


def test_idle_app_backs_off_to_the_cap_and_stops_scanning(tmp_path, monkeypatch):
    app, _, scans = backoff_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        assert wait_until(lambda: scanner_interval(client) == 4.0)
        count = len(scans)
        time.sleep(0.5)

        # 间隔拉到了 4 秒：半秒里不该有新的一轮（原来 0.05 秒一轮，会有十来轮）
        assert len(scans) == count
        # 退避着也不能被当成扫描停滞
        assert client.get("/api/health").json()["services"]["scanner"] == "healthy"


def test_rounds_that_import_something_keep_the_base_pace(tmp_path, monkeypatch):
    app, _, scans = backoff_app(tmp_path, monkeypatch)
    app.state.importer.scan = lambda *a, **k: (
        scans.append(time.monotonic()) or ScanReport(versions_imported=1)
    )

    with TestClient(app) as client:
        assert wait_until(lambda: len(scans) >= 6, timeout=4.0)

        # 每一轮都导入了新东西：间隔始终是原来的 0.05 秒，没有被拉长
        assert scanner_interval(client) == 0.05


def test_rounds_where_a_later_phase_did_work_keep_the_base_pace(tmp_path, monkeypatch):
    calls = []

    def busy_extraction(self, **kwargs):
        calls.append(time.monotonic())
        return {"started": 2, "succeeded": 2, "failed": 0, "skipped_no_key": 0}

    monkeypatch.setattr(TaskService, "extract_pending", busy_extraction)
    app, _, scans = backoff_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        assert wait_until(lambda: len(calls) >= 6, timeout=4.0)

        assert scanner_interval(client) == 0.05


def test_a_failing_scan_is_retried_at_the_base_pace_not_backed_off(tmp_path, monkeypatch):
    app, _, scans = backoff_app(tmp_path, monkeypatch)

    def failing_scan(*args, **kwargs):
        scans.append(time.monotonic())
        raise OSError("盘读不出来")

    app.state.importer.scan = failing_scan

    with TestClient(app) as client:
        assert wait_until(lambda: len(scans) >= 6, timeout=4.0)

        assert scanner_interval(client) == 0.05


def test_new_recording_in_the_archive_appears_while_backed_off_far_sooner_than_the_cap(
    tmp_path, monkeypatch
):
    app, settings, scans = backoff_app(tmp_path, monkeypatch)
    meeting_id = "vm-20260710-150000-abcd1234"
    meeting_dir = settings.archive_root / "260710 退避中落盘"

    with TestClient(app) as client:
        settle(client, scans)
        started = time.monotonic()
        meeting_dir.mkdir()
        (meeting_dir / f"{meeting_id}.m4a").write_bytes(b"audio")
        (meeting_dir / f"{meeting_id}.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n新录音\n", encoding="utf-8"
        )
        (meeting_dir / "meeting.md").write_text("# 新录音纪要", encoding="utf-8")

        items = wait_until(lambda: client.get("/api/meetings").json()["items"], timeout=3.5)
        elapsed = time.monotonic() - started

        assert [item["id"] for item in items] == [meeting_id]
        # 探测间隔是原来的 0.05 秒；退避到 4 秒的话要等满 4 秒才看得到
        assert elapsed < 2.0


def test_page_write_wakes_a_backed_off_scan_loop(tmp_path, monkeypatch):
    app, _, scans = backoff_app(tmp_path, monkeypatch)

    async def touch():
        return {"ok": True}

    add_probe_post(app, "/api/test-write", touch)

    with TestClient(app) as client:
        count = settle(client, scans)
        started = time.monotonic()

        response = client.post("/api/test-write", json={}, headers=write_headers(client))
        assert response.status_code == 200

        assert wait_until(lambda: len(scans) > count, timeout=3.0)
        assert time.monotonic() - started < 2.0


def test_rejected_page_write_does_not_wake_the_scan_loop(tmp_path, monkeypatch):
    app, _, scans = backoff_app(tmp_path, monkeypatch)

    async def touch():
        return {"ok": True}

    add_probe_post(app, "/api/test-write", touch)

    with TestClient(app) as client:
        count = settle(client, scans)

        # 缺 CSRF 校验：被安全中间件挡下，没有写成，不该惊动扫描
        response = client.post("/api/test-write", json={})
        assert response.status_code == 403
        time.sleep(0.5)

        assert len(scans) == count


def test_relay_job_state_change_wakes_a_backed_off_scan_loop(tmp_path, monkeypatch):
    relay = RelayStub()
    app, _, scans = backoff_app(tmp_path, monkeypatch, relay)

    with TestClient(app) as client:
        count = settle(client, scans)
        started = time.monotonic()

        relay.counts["active"] = 1  # 一个任务开始转写
        assert wait_until(lambda: len(scans) > count, timeout=8.0)

        # relay 健康探测 5 秒一次：最多等到下一次探测，比 4 秒的退避上限之外再加一轮要快得多
        assert time.monotonic() - started < 7.0


def test_relay_probe_blip_does_not_wake_a_backed_off_scan_loop(tmp_path, monkeypatch):
    relay = RelayStub()
    app, _, scans = backoff_app(tmp_path, monkeypatch, relay, cap=60.0)

    with TestClient(app) as client:
        count = settle(client, scans, cap=60.0)

        relay.broken = True  # 一次探测超时：状态变成 unavailable，任务其实没动
        assert wait_until(
            lambda: client.get("/api/health").json()["services"]["relay"] == "unavailable",
            timeout=8.0,
        )
        relay.broken = False
        assert wait_until(
            lambda: client.get("/api/health").json()["services"]["relay"] == "healthy",
            timeout=8.0,
        )
        time.sleep(0.5)

        assert len(scans) == count


def test_health_scanner_stale_line_widens_with_the_backoff_interval(tmp_path, monkeypatch):
    app, settings, scans = backoff_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        settle(client, scans)
        state = app.state.scanner_state
        # 上一轮是 100 秒前：原来 180 秒的线会判停滞；退避到 600 秒的间隔里这是正常的
        state["interval_seconds"] = 600.0
        state["last_completed_monotonic"] = time.monotonic() - 400
        assert client.get("/api/health").json()["services"]["scanner"] == "healthy"

        # 间隔回到 4 秒，同样的 400 秒前就是真停滞了
        state["interval_seconds"] = 4.0
        assert client.get("/api/health").json()["services"]["scanner"] == "failed"


def test_default_settings_back_off_to_ten_minutes():
    # conftest 把测试里的默认值关了；这里直接看字段默认值
    assert Settings.model_fields["scan_idle_max_interval_seconds"].default == 600.0


def test_settings_reject_a_negative_or_infinite_backoff_cap():
    with pytest.raises(ValueError, match="scan_idle_max_interval_seconds"):
        Settings(semantic_enabled=False, scan_idle_max_interval_seconds=-1)
    with pytest.raises(ValueError, match="scan_idle_max_interval_seconds"):
        Settings(semantic_enabled=False, scan_idle_max_interval_seconds=float("inf"))


def test_health_scanner_section_stays_json_serializable(tmp_path, monkeypatch):
    app, _, _ = backoff_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        payload = client.get("/api/health").json()

        json.dumps(payload)
        assert isinstance(payload["details"]["scanner"]["interval_seconds"], int | float)


def test_fingerprint_is_independent_of_directory_listing_order(tmp_path):
    archive, staging = make_tree(tmp_path)
    first = source_fingerprint((staging, archive))
    for index in range(30):
        (archive / "260701 周会" / f"n{index}.txt").write_text(str(index), encoding="utf-8")
    for index in reversed(range(30)):
        (archive / "260701 周会" / f"n{index}.txt").unlink()

    assert source_fingerprint((staging, archive)) == first
