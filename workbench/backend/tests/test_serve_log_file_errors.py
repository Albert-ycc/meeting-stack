"""MEETING_WORKBENCH_LOG_FILE 指的地方写不了：serve 照旧起不来（fail-fast），但标准错误里是一句人话——哪项配置、
指到了什么、怎么改——不是 dictConfig 的「Unable to configure handler 'file'」加一大段 traceback。"""

import os

import pytest

from meeting_workbench import cli

from .test_serve_logging import restore_logging  # noqa: F401  serve 会改进程全局的 logger，用完还原

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目录权限限制")


def serve_with_log_file(monkeypatch, tmp_path, log_file):
    for key, value in {
        "MEETING_WORKBENCH_DATA_DIR": tmp_path / "data",
        "MEETING_WORKBENCH_ARCHIVE_ROOT": tmp_path / "archive",
        "MEETING_WORKBENCH_STAGING_ROOT": tmp_path / "staging",
        "MEETING_WORKBENCH_RELAY_JOBS_DB": tmp_path / "relay.sqlite3",
        "MEETING_WORKBENCH_SEMANTIC_ENABLED": "0",
        "MEETING_WORKBENCH_PORT": "8899",
        "MEETING_WORKBENCH_LOG_FILE": log_file,
    }.items():
        monkeypatch.setenv(key, str(value))
    started: list[str] = []

    import uvicorn

    monkeypatch.setattr(cli, "create_app", lambda settings: started.append("app"))
    monkeypatch.setattr(cli, "_raise_open_file_limit", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.append("uvicorn"))
    return cli.main(["serve"]), started


def assert_refused_in_plain_words(capsys, code, started, log_file, *words):
    stderr = capsys.readouterr().err
    assert code == 2
    assert started == []  # 应用没建、服务没起
    assert "Traceback" not in stderr and "Unable to configure handler" not in stderr
    assert "MEETING_WORKBENCH_LOG_FILE" in stderr and str(log_file) in stderr
    for word in words:
        assert word in stderr
    return stderr


def test_log_file_pointing_at_a_directory(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    code, started = serve_with_log_file(monkeypatch, tmp_path, log_dir)

    assert_refused_in_plain_words(capsys, code, started, log_dir, "是一个目录", "web.log")


@needs_permissions
def test_log_file_in_a_directory_the_user_cannot_write(monkeypatch, tmp_path, capsys):
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        code, started = serve_with_log_file(monkeypatch, tmp_path, read_only / "web.log")
    finally:
        read_only.chmod(0o700)

    assert_refused_in_plain_words(capsys, code, started, read_only / "web.log", "没有权限")


@needs_permissions
def test_log_directory_that_cannot_be_created(monkeypatch, tmp_path, capsys):
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        code, started = serve_with_log_file(monkeypatch, tmp_path, read_only / "logs" / "web.log")
    finally:
        read_only.chmod(0o700)

    assert_refused_in_plain_words(capsys, code, started, read_only / "logs" / "web.log", "没有权限")


def test_log_file_under_something_that_is_a_file(monkeypatch, tmp_path, capsys):
    a_file = tmp_path / "a_file"
    a_file.write_text("x", encoding="utf-8")

    code, started = serve_with_log_file(monkeypatch, tmp_path, a_file / "web.log")

    assert_refused_in_plain_words(capsys, code, started, a_file / "web.log", "不是目录")
