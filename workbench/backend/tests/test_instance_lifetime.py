"""非正式实例（沙箱、回归、临时验证）的存活时限：到点自己退出并写日志；正式实例不受影响。"""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import meeting_workbench
from meeting_workbench import instance_lifetime
from meeting_workbench.config import Settings
from meeting_workbench.instance_lifetime import (
    OFFICIAL_PORT,
    is_official,
    lifetime_seconds,
    start_lifetime_guard,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def settings_for(data_dir: Path, port: int = OFFICIAL_PORT, **extra) -> Settings:
    return Settings(data_dir=data_dir, port=port, semantic_enabled=False, **extra)


def test_official_instance_is_the_home_data_dir_on_port_8765(fake_home):
    assert is_official(settings_for(fake_home / ".meeting-workbench", 8765))


def test_other_port_makes_it_unofficial_even_with_the_official_data_dir(fake_home):
    assert not is_official(settings_for(fake_home / ".meeting-workbench", 8766))


def test_other_data_dir_makes_it_unofficial_even_on_port_8765(fake_home, tmp_path):
    assert not is_official(settings_for(tmp_path / "sandbox", 8765))
    assert not is_official(settings_for(fake_home / ".meeting-workbench-2", 8765))


def test_official_data_dir_reached_through_a_symlink_is_still_official(fake_home, tmp_path):
    real = fake_home / ".meeting-workbench"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    assert is_official(settings_for(alias, 8765))


def test_official_instance_has_no_lifetime_whatever_the_setting_says(fake_home):
    official = settings_for(fake_home / ".meeting-workbench", instance_lifetime_seconds=1)

    assert lifetime_seconds(official) is None
    assert start_lifetime_guard(official) is None


def test_unofficial_instance_gets_four_hours_by_default(fake_home, tmp_path):
    assert lifetime_seconds(settings_for(tmp_path / "sandbox", 8799)) == 4 * 3600


def test_zero_means_unlimited_for_a_deliberate_long_running_second_instance(fake_home, tmp_path):
    second = settings_for(tmp_path / "second", 8767, instance_lifetime_seconds=0)

    assert lifetime_seconds(second) is None
    assert start_lifetime_guard(second) is None


def test_negative_lifetime_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="instance_lifetime_seconds"):
        Settings(data_dir=tmp_path, semantic_enabled=False, instance_lifetime_seconds=-1)


def test_guard_logs_then_terminates_on_time_and_hard_exits_if_still_alive(
    fake_home, tmp_path, caplog
):
    calls = []
    done = threading.Event()

    def hard_exit():
        calls.append("hard_exit")
        done.set()

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=instance_lifetime.logger.name):
        thread = start_lifetime_guard(
            settings_for(tmp_path / "sandbox", 8799),
            terminate=lambda: calls.append("terminate"),
            hard_exit=hard_exit,
            hard_exit_grace=0.2,
            lifetime=0.3,
        )
        assert thread is not None
        assert done.wait(5)

    assert calls == ["terminate", "hard_exit"]
    assert time.monotonic() - started >= 0.5
    messages = [record.getMessage() for record in caplog.records]
    assert any("非正式实例" in text and "存活时限" in text for text in messages)  # 起来时先说一声
    assert any("存活满" in text and "自动退出" in text for text in messages)  # 到点写日志
    assert any("直接结束进程" in text for text in messages)
    # 日志里要有能认出是谁的信息：数据目录和端口
    assert any("8799" in text and "sandbox" in text for text in messages)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_real_unofficial_server_process_exits_by_itself_and_leaves_a_log_line(tmp_path):
    """真起一个服务进程：存活时限到了它自己正常关停退出，web.log 里有原因。"""
    data_dir = tmp_path / "data"
    log_file = tmp_path / "web.log"
    package_root = Path(meeting_workbench.__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(package_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MEETING_WORKBENCH_DATA_DIR": str(data_dir),
        "MEETING_WORKBENCH_PORT": str(free_port()),
        "MEETING_WORKBENCH_ARCHIVE_ROOT": str(tmp_path / "archive"),
        "MEETING_WORKBENCH_STAGING_ROOT": str(tmp_path / "staging"),
        "MEETING_WORKBENCH_RELAY_JOBS_DB": str(tmp_path / "relay.sqlite3"),
        "MEETING_WORKBENCH_SEMANTIC_ENABLED": "0",
        "MEETING_WORKBENCH_LOG_FILE": str(log_file),
        "MEETING_WORKBENCH_INSTANCE_LIFETIME_SECONDS": "4",
    }
    # 不读仓库里的 .env：在生产 checkout 里跑这条用例也不能连上生产的飞书配置
    program = (
        "from meeting_workbench.config import Settings; "
        "Settings.model_config['env_file'] = None; "
        "from meeting_workbench.cli import main; "
        "raise SystemExit(main(['serve']))"
    )
    (tmp_path / "archive").mkdir()
    (tmp_path / "staging").mkdir()
    started = time.monotonic()

    process = subprocess.Popen(
        [sys.executable, "-c", program],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        pytest.fail(f"存活时限 4 秒的实例 60 秒了还没退出。输出：\n{output[-2000:]}")

    # uvicorn 走完正常的关停流程之后，会把收到的 SIGTERM 再对自己发一次，所以退出状态是被 15 号信号结束
    assert process.returncode in (0, -signal.SIGTERM), output[-2000:]
    assert time.monotonic() - started >= 4
    log = log_file.read_text(encoding="utf-8")
    assert "存活满" in log and "自动退出" in log
    assert str(data_dir) in log
    assert "Application shutdown complete" in log  # 是正常关停，不是被硬杀
