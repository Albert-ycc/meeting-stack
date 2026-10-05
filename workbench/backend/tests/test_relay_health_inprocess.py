"""relay 健康探测在进程内读，不再每 5 秒起一个 relayctl health 子进程。

原来的探测每次起 relayctl：解释器启动加编译 7 千行的 relay_control，实测约 58ms CPU/次，常驻约 1% 单核。
现在 RelayClient.health 直接调 relay 自己的 RelayControl.health。要守的是两件事：
1. 字段和含义不变：同一份库里，进程内读出来的和真 relayctl health --json 逐字段一致；
2. 探测里不再起子进程，relay 文件被换掉后也不用重启工作台才认。
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_workbench import relay_client as relay_client_module
from meeting_workbench.config import REPO_ROOT, Settings
from meeting_workbench.main import create_app
from meeting_workbench.relay_client import RelayClient, RelayUnavailable, load_relay_control

RELAY_QUICKSTART = REPO_ROOT / "relay" / "quickstart"
CONTROL_PATH = RELAY_QUICKSTART / "relay_control.py"
RELAYCTL = RELAY_QUICKSTART / "relayctl"
# 每次现算、不同次调用之间必然不同的字段：对比时去掉，年龄另外按容差比
VOLATILE_AGE_KEYS = ("heartbeat_age_seconds", "age_seconds")


def make_settings(tmp_path: Path, relay_repo: Path | None = None) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo or REPO_ROOT / "relay",
        relay_jobs_db=tmp_path / "relay" / "jobs.sqlite3",
        semantic_enabled=False,
    )


def relay_control(settings: Settings):
    module = load_relay_control(CONTROL_PATH)
    return module.RelayControl(
        settings.relay_jobs_db,
        archive_root=settings.archive_root,
        archive_lock_path=settings.data_dir / "archive.lock",
    )


def seed_healthy_workers(settings: Settings) -> None:
    control = relay_control(settings)
    control.heartbeat_worker("watchdog", mode="controlled", status="running")
    control.heartbeat_worker("control-worker", mode="controlled", status="idle")


def insert_job(settings: Settings, job_id: str, status: str, failure_stage: str | None = None):
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(settings.relay_jobs_db) as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, source_key, audio_path, status, failure_stage,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, f"key-{job_id}", f"/audio/{job_id}.m4a", status, failure_stage, now, now),
        )


def age_heartbeats(settings: Settings, seconds: float) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    with sqlite3.connect(settings.relay_jobs_db) as connection:
        connection.execute("UPDATE runtime_workers SET heartbeat_at = ?", (old,))


def relayctl_health(settings: Settings, tmp_path: Path) -> tuple[int, dict]:
    """真 relayctl health --json：工作台原来每 5 秒起的就是这个命令。环境和 RelayClient._run 一致。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(RELAYCTL), "health", "--json"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MEETING_RELAY_JOBS_DB": str(settings.relay_jobs_db),
            "MEETING_RELAY_ARCHIVE_ROOT": str(settings.archive_root),
            "MEETING_RELAY_CONTROL_ENABLED": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def split_ages(payload: dict) -> tuple[dict, list[float | None]]:
    """去掉每次现算的字段，返回 (其余内容, 各处年龄)。年龄的顺序固定：worker、再 workers 逐个。"""
    stripped = json.loads(json.dumps(payload))
    stripped.pop("checked_at")
    ages = [stripped["worker"].pop("heartbeat_age_seconds")]
    for item in stripped["workers"]:
        ages.append(item.pop("age_seconds"))
    return stripped, ages


def only_relay_health_subprocess_is_forbidden(monkeypatch):
    real_run = subprocess.run

    def guarded(arguments, *args, **kwargs):
        assert "health" not in arguments, "relay 健康探测不该再起 relayctl 子进程"
        return real_run(arguments, *args, **kwargs)

    monkeypatch.setattr(relay_client_module.subprocess, "run", guarded)


def test_health_reads_in_process_and_never_spawns_relayctl(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    seed_healthy_workers(settings)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("health 不该起任何 relayctl 子进程")

    monkeypatch.setattr(relay_client_module.subprocess, "run", forbidden)

    payload = RelayClient(settings).health()

    assert payload["status"] == "healthy"
    assert payload["mode"] == "controlled"
    assert payload["worker"]["state"] == "idle"
    assert payload["counts"] == {
        "queued": 0,
        "active": 0,
        "failed": 0,
        "pending_archive_failures": 0,
        "held_test_fixtures": 0,
    }
    assert [item["name"] for item in payload["workers"]] == ["control-worker", "watchdog"]


def scenario_idle(settings):
    seed_healthy_workers(settings)


def scenario_busy_with_queue(settings):
    seed_healthy_workers(settings)
    control = relay_control(settings)
    control.heartbeat_worker(
        "control-worker",
        mode="controlled",
        status="busy",
        current_job_id="job-0123456789abcdef",
        current_stage="transcribing",
    )
    insert_job(settings, "job-queued1", "queued")
    insert_job(settings, "job-queued2", "queued")
    insert_job(settings, "job-active1", "transcribing")
    insert_job(settings, "job-minutes1", "minutes_generating")


def scenario_failed_and_pending_archive(settings):
    seed_healthy_workers(settings)
    insert_job(settings, "job-failed1", "failed")
    insert_job(settings, "job-pending1", "failed", failure_stage="pending_archive")
    insert_job(settings, "job-pending2", "failed", failure_stage="pending_archive")


def scenario_degraded_worker(settings):
    seed_healthy_workers(settings)
    relay_control(settings).heartbeat_worker(
        "control-worker", mode="controlled", status="degraded", last_error="WorkerError"
    )


def scenario_stale_heartbeat(settings):
    seed_healthy_workers(settings)
    age_heartbeats(settings, 300)


def scenario_stopped_worker(settings):
    seed_healthy_workers(settings)
    relay_control(settings).stop_worker("control-worker")


def scenario_watchdog_only(settings):
    relay_control(settings).heartbeat_worker("watchdog", mode="controlled", status="running")


def scenario_empty_database(settings):
    relay_control(settings)


def scenario_database_missing(settings):
    pass


SCENARIOS = [
    (scenario_idle, "healthy"),
    (scenario_busy_with_queue, "healthy"),
    (scenario_failed_and_pending_archive, "degraded"),
    (scenario_degraded_worker, "degraded"),
    (scenario_stale_heartbeat, "unavailable"),
    (scenario_stopped_worker, "unavailable"),
    (scenario_watchdog_only, "unavailable"),
    (scenario_empty_database, "unavailable"),
    (scenario_database_missing, "unavailable"),
]


@pytest.mark.parametrize(
    ("scenario", "expected_status"), SCENARIOS, ids=[item[0].__name__ for item in SCENARIOS]
)
def test_in_process_health_matches_relayctl_health_field_by_field(
    tmp_path, scenario, expected_status
):
    settings = make_settings(tmp_path)
    scenario(settings)

    exit_code, cli_payload = relayctl_health(settings, tmp_path)
    in_process = RelayClient(settings).health()

    assert cli_payload["status"] == expected_status
    assert exit_code == {"healthy": 0, "degraded": 1, "unavailable": 2}[expected_status]
    cli_rest, cli_ages = split_ages(cli_payload)
    in_process_rest, in_process_ages = split_ages(in_process)
    assert in_process_rest == cli_rest
    assert len(in_process_ages) == len(cli_ages)
    for cli_age, own_age in zip(cli_ages, in_process_ages, strict=True):
        assert (cli_age is None) == (own_age is None)
        if cli_age is not None:
            # 两次读之间隔着起一个子进程的时间，年龄只会往大走
            assert 0 <= own_age - cli_age < 5


# ---------------------------------------------------------------- 加载与失败路径


def write_fake_control(relay_repo: Path, body: str) -> Path:
    path = relay_repo / "quickstart" / "relay_control.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


FAKE_CONTROL = """
class RelayControl:
    def __init__(self, db_path, *, archive_root=None, initialize=True):
        assert initialize is False, "探测只读，不能建表"

    def health(self, *, control_enabled=None):
        assert control_enabled is True
        return {payload}
"""


def fake_control_source(payload: dict) -> str:
    return FAKE_CONTROL.replace("{payload}", repr(payload))


def test_loaded_module_is_reused_until_the_file_changes(tmp_path):
    relay_repo = tmp_path / "meeting-relay"
    path = write_fake_control(
        relay_repo, fake_control_source({"status": "healthy", "marker": "first"})
    )
    client = RelayClient(make_settings(tmp_path, relay_repo))

    first = load_relay_control(path)
    assert load_relay_control(path) is first
    assert client.health()["marker"] == "first"

    # 升级 relay 后文件被换掉：原来每次起 relayctl 都读最新代码，这里不能变成要重启工作台才认
    write_fake_control(
        relay_repo, fake_control_source({"status": "degraded", "marker": "second-version"})
    )
    assert client.health()["marker"] == "second-version"
    assert load_relay_control(path) is not first


def test_health_tells_the_probe_when_relay_files_are_missing_without_leaking_paths(
    tmp_path, caplog
):
    settings = make_settings(tmp_path, tmp_path / "no-such-relay")

    with caplog.at_level("ERROR", logger="meeting_workbench.relay_client"):
        with pytest.raises(RelayUnavailable) as excinfo:
            RelayClient(settings).health()

    assert str(excinfo.value) == "中转程序找不到，详见服务日志"
    assert str(tmp_path) not in str(excinfo.value)
    assert str(tmp_path) in caplog.text


def test_health_turns_a_broken_relay_module_into_unavailable_not_a_crash(tmp_path, caplog):
    relay_repo = tmp_path / "meeting-relay"
    write_fake_control(relay_repo, "def broken(:\n")

    with caplog.at_level("ERROR", logger="meeting_workbench.relay_client"):
        with pytest.raises(RelayUnavailable) as excinfo:
            RelayClient(make_settings(tmp_path, relay_repo)).health()

    assert str(excinfo.value) == "relay 健康读取失败，详见服务日志"
    assert str(tmp_path) not in str(excinfo.value)


def test_health_rejects_a_status_the_workbench_does_not_know(tmp_path):
    relay_repo = tmp_path / "meeting-relay"
    write_fake_control(relay_repo, fake_control_source({"status": "on-fire"}))

    with pytest.raises(RelayUnavailable):
        RelayClient(make_settings(tmp_path, relay_repo)).health()


# ---------------------------------------------------------------- 接到探测循环和 /api/health


class ListlessRelay(RelayClient):
    """health 走真的进程内读取；需要处理清单的 list 不起子进程，这条用例只看健康探测。"""

    def list_jobs(self, **_kwargs):
        return []


def started_app(tmp_path):
    settings = make_settings(tmp_path)
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    return settings, create_app(settings, ListlessRelay(settings))


def relay_fields(client: TestClient) -> dict:
    body = client.get("/api/health").json()
    return {
        "relay": body["services"]["relay"],
        "relay_worker": body["services"]["relay_worker"],
        "worker_state": body["details"]["relay_worker"]["state"],
        "has_probed_at": bool(body["details"]["relay_worker"]["probed_at"]),
        "queued_jobs": body["counts"]["queued_jobs"],
    }


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_api_health_relay_fields_come_from_the_in_process_probe(tmp_path, monkeypatch):
    settings, app = started_app(tmp_path)
    seed_healthy_workers(settings)
    insert_job(settings, "job-queued1", "queued")
    insert_job(settings, "job-queued2", "queued")
    only_relay_health_subprocess_is_forbidden(monkeypatch)

    with TestClient(app) as client:
        fields = relay_fields(client)

    assert fields == {
        "relay": "healthy",
        "relay_worker": "healthy",
        "worker_state": "idle",
        "has_probed_at": True,
        "queued_jobs": 2,
    }


def test_api_health_follows_a_relay_state_change_within_one_probe_period(tmp_path, monkeypatch):
    """探测循环的间隔没变，每 5 秒一次；状态翻转最迟下一次探测就反映到 /api/health。"""
    settings, app = started_app(tmp_path)
    seed_healthy_workers(settings)
    only_relay_health_subprocess_is_forbidden(monkeypatch)

    with TestClient(app) as client:
        assert relay_fields(client)["relay_worker"] == "healthy"
        flipped_at = time.monotonic()
        age_heartbeats(settings, 300)

        assert wait_until(lambda: relay_fields(client)["relay_worker"] == "unavailable", 8)
        # 探测每 5 秒一次，加上这一次读的时间；不能比 5 秒的探测间隔再多出一整轮
        assert time.monotonic() - flipped_at < 7
        assert relay_fields(client)["relay"] == "unavailable"
