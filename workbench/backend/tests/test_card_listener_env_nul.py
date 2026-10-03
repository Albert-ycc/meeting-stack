"""card_listener 读 .env：值里带 NUL 的那一行当没写，启动日志里说一句（只说哪个文件、哪个键，不记值）。

原来 python-dotenv 读出来的值原样写进 os.environ，带 NUL 时抛 ValueError: embedded null byte；这一步在模块顶层、
main() 的 try 之外，进程一启动就崩，日志一个字没有。relay 读同一份 .env 时早就跳过带 NUL 的行（relay_env.py）。"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from .test_card_listener import (
    CHAT_KEY,
    CONFIG_MISSING_EXIT,
    GOOD_ENV,
    LISTENER_KEYS,
    OWNER_KEY,
    load_listener,
    make_repo_copy,
    process_env,
    write_env,
)

BAD_CHAT = "oc_secret_bad\x00tail"


@pytest.fixture
def listener(monkeypatch, tmp_path):
    """在临时 HOME 下导入监听脚本（不是入口方式，不读 .env）。"""
    home = tmp_path / "home"
    (home / ".meeting-workbench" / "logs").mkdir(parents=True)
    return load_listener(monkeypatch, home, GOOD_ENV)


def test_a_value_with_nul_is_skipped_and_reported_by_file_and_key_only(listener, tmp_path):
    env_file = write_env(
        tmp_path / "workbench" / ".env", f"{CHAT_KEY}={BAD_CHAT}\n{OWNER_KEY}=ou_me\n"
    )
    environ: dict[str, str] = {}
    skipped: list[str] = []

    applied = listener._load_env_files((env_file,), environ, skipped)

    assert applied == [OWNER_KEY]
    assert environ == {OWNER_KEY: "ou_me"}
    assert skipped == [f"workbench/.env 的 {CHAT_KEY}"]


def test_the_nul_line_counts_as_not_written_so_the_other_file_still_applies(listener, tmp_path):
    root_env = write_env(tmp_path / ".env", f"{CHAT_KEY}=oc_good\n")
    workbench_env = write_env(tmp_path / "workbench" / ".env", f"{CHAT_KEY}={BAD_CHAT}\n")
    environ: dict[str, str] = {}

    listener._load_env_files((root_env, workbench_env), environ, [])

    assert environ == {CHAT_KEY: "oc_good"}


def test_the_real_process_environment_is_not_handed_a_nul(monkeypatch, listener, tmp_path):
    for key in LISTENER_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = write_env(tmp_path / ".env", f"{CHAT_KEY}={BAD_CHAT}\n{OWNER_KEY}=ou_me\n")

    listener._load_env_files((env_file,), os.environ, [])

    assert CHAT_KEY not in os.environ
    assert os.environ[OWNER_KEY] == "ou_me"


def run_script(tmp_path, *, root_env: str | None, workbench_env: str):
    """在临时拼的仓库目录里把监听脚本当入口起起来：起来了就停掉（只停这个子进程），返回退出码、标准错误、日志。"""
    repo = make_repo_copy(tmp_path)
    if root_env is not None:
        write_env(repo / ".env", root_env)
    write_env(repo / "workbench" / ".env", workbench_env)
    script_home = tmp_path / "script-home"
    script_home.mkdir()
    log_file = script_home / ".meeting-workbench" / "logs" / "card-listener.log"
    process = subprocess.Popen(
        [sys.executable, str(repo / "workbench" / "card_listener.py")],
        env=process_env(script_home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            if log_file.exists() and "card_listener 启动" in log_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
    finally:
        started = process.poll() is None
        if started:
            process.terminate()
        _, stderr = process.communicate(timeout=30)
    log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    return (None if started else process.returncode), stderr, log_text


def test_script_with_a_nul_in_the_only_chat_id_explains_instead_of_crashing(tmp_path):
    code, stderr, log_text = run_script(
        tmp_path, root_env=None, workbench_env=f"{CHAT_KEY}={BAD_CHAT}\n{OWNER_KEY}=ou_me\n"
    )

    assert code == CONFIG_MISSING_EXIT, stderr[-500:]
    assert "Traceback" not in stderr
    assert CHAT_KEY in stderr  # 缺什么照旧说清楚
    assert f"workbench/.env 的 {CHAT_KEY} 这一行带 NUL" in log_text
    assert "oc_secret_bad" not in log_text and "oc_secret_bad" not in stderr


def test_script_falls_back_to_the_other_file_and_still_logs_the_skipped_line(tmp_path):
    code, stderr, log_text = run_script(
        tmp_path,
        root_env=f"{CHAT_KEY}=oc_good\n",
        workbench_env=f"{CHAT_KEY}={BAD_CHAT}\n{OWNER_KEY}=ou_me\n",
    )

    assert code is None, stderr[-500:]  # 起来了
    assert "card_listener 启动" in log_text
    assert f"workbench/.env 的 {CHAT_KEY} 这一行带 NUL" in log_text
    assert "oc_secret_bad" not in log_text and "oc_secret_bad" not in stderr
