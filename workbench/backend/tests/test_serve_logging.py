"""serve 的日志：访问日志不带查询串、成功的轮询不记、可选写进自己管理的轮转文件。

uvicorn 的 logger 和 root logger 是进程全局的，这里的用例会真的去改它们，每条用例前后都要原样还回去。"""

import logging
import logging.handlers
import re
import sys

import pytest

from meeting_workbench import cli
from meeting_workbench.config import Settings
from meeting_workbench.serve_logging import (
    QUIET_POLL_PATHS,
    AccessLogFilter,
    configure_serve_logging,
)

LOGGER_NAMES = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
TIMESTAMP = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} "


@pytest.fixture(autouse=True)
def restore_logging():
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).filters[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in LOGGER_NAMES
    }
    yield
    for name, (handlers, filters, level, propagate, disabled) in saved.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        **overrides,
    )


def access_record(method, target, status) -> logging.LogRecord:
    """和 uvicorn 的协议实现发出的记录同一个形状：模板加五个参数。"""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", method, target, "1.1", status),
        None,
    )


def emit_access(method, target, status) -> None:
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", method, target, "1.1", status
    )


def flush_all() -> None:
    for name in LOGGER_NAMES:
        for handler in logging.getLogger(name).handlers:
            handler.flush()


# ---------------------------------------------------------------- 过滤器本身


def test_the_query_string_never_reaches_the_log_line():
    record = access_record(
        "GET", "/api/search?q=%E6%9C%BA%E5%AF%86%E9%A2%84%E7%AE%97&scope=all", 200
    )

    assert AccessLogFilter().filter(record) is True

    line = record.getMessage()
    assert line == '127.0.0.1:50000 - "GET /api/search HTTP/1.1" 200'
    assert "?" not in line and "scope" not in line


@pytest.mark.parametrize("path", sorted(QUIET_POLL_PATHS))
@pytest.mark.parametrize("status", [200, 204, 304])
def test_successful_polls_are_not_logged(path, status):
    assert AccessLogFilter().filter(access_record("GET", path, status)) is False
    # 带查询串的轮询（待确认任务的条数、待确认的词）也一样
    assert (
        AccessLogFilter().filter(access_record("GET", f"{path}?status=pending&limit=1", status))
        is False
    )


@pytest.mark.parametrize("path", sorted(QUIET_POLL_PATHS))
@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 422, 500, 503])
def test_failed_polls_are_still_logged(path, status):
    record = access_record("GET", f"{path}?x=1", status)

    assert AccessLogFilter().filter(record) is True
    assert record.getMessage() == f'127.0.0.1:50000 - "GET {path} HTTP/1.1" {status}'


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("POST", "/api/jobs"),
        ("DELETE", "/api/tasks"),
        ("GET", "/api/meetings?limit=50&offset=0"),
        ("GET", "/api/jobs/job-1"),
        ("GET", "/api/tasks/task-1"),
        ("GET", "/api/health/extra"),
        ("GET", "/api/media/1/peaks"),
        ("GET", "/"),
    ],
)
def test_everything_else_is_logged_with_the_query_stripped(method, target):
    record = access_record(method, target, 200)

    assert AccessLogFilter().filter(record) is True
    assert record.getMessage() == (
        f'127.0.0.1:50000 - "{method} {target.partition("?")[0]} HTTP/1.1" 200'
    )


def test_records_that_are_not_shaped_like_uvicorns_pass_through_untouched():
    odd = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "x ? y", (), None)
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "%s", ("a?b",), None)

    assert AccessLogFilter().filter(odd) is True and AccessLogFilter().filter(other) is True
    assert odd.getMessage() == "x ? y" and other.getMessage() == "a?b"


# ---------------------------------------------------------------- 配成日志文件


def test_one_rotating_file_collects_app_uvicorn_and_access_lines(tmp_path, capsys):
    log_file = tmp_path / "logs" / "web.log"
    configure_serve_logging(make_settings(tmp_path, log_file=log_file))

    logging.getLogger("meeting_workbench.relay_client").warning("应用自己的日志")
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    emit_access("GET", "/api/search?q=机密项目预算", 200)
    emit_access("GET", "/api/health", 200)
    emit_access("GET", "/api/health", 503)
    flush_all()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert all(re.match(TIMESTAMP, line) for line in lines), lines
    assert lines[0].endswith("WARNING meeting_workbench.relay_client 应用自己的日志")
    assert lines[1].endswith("INFO uvicorn.error Application startup complete.")
    assert lines[2].endswith('INFO uvicorn.access 127.0.0.1:50000 - "GET /api/search HTTP/1.1" 200')
    assert lines[3].endswith('INFO uvicorn.access 127.0.0.1:50000 - "GET /api/health HTTP/1.1" 503')
    assert "机密" not in log_file.read_text(encoding="utf-8")
    # 日志都进了文件，标准输出和标准错误里什么都没有
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_log_directory_is_created_when_missing(tmp_path):
    log_file = tmp_path / "does" / "not" / "exist" / "web.log"

    configure_serve_logging(make_settings(tmp_path, log_file=log_file))
    logging.getLogger("meeting_workbench.x").info("hello")
    flush_all()

    assert log_file.read_text(encoding="utf-8").endswith("hello\n")


def test_the_file_rotates_by_size_and_only_the_configured_backups_stay(tmp_path):
    log_file = tmp_path / "web.log"
    configure_serve_logging(
        make_settings(tmp_path, log_file=log_file, log_max_bytes=500, log_backup_count=2)
    )

    for index in range(80):
        logging.getLogger("meeting_workbench.x").info("第 %03d 行 %s", index, "字" * 20)
    flush_all()

    names = sorted(path.name for path in tmp_path.iterdir() if path.name.startswith("web.log"))
    assert names == ["web.log", "web.log.1", "web.log.2"]
    for name in names:
        # 单个文件不会超过上限太多：最多多出写进去的最后一行
        assert (tmp_path / name).stat().st_size <= 500 + 120, name
    # 留下的是最新的几行，最早的已经被挤掉
    kept = "".join((tmp_path / name).read_text(encoding="utf-8") for name in names)
    assert "第 079 行" in kept and "第 000 行" not in kept


def test_a_log_file_that_is_already_huge_is_rotated_out_on_the_first_line(tmp_path):
    """接手一份已经很大的旧 web.log：第一行写进来就把它整个换到 .1，当前文件从空的开始。"""
    log_file = tmp_path / "web.log"
    log_file.write_bytes(b"x" * 5000)
    configure_serve_logging(make_settings(tmp_path, log_file=log_file, log_max_bytes=1000))

    logging.getLogger("meeting_workbench.x").info("第一行")
    flush_all()

    assert (tmp_path / "web.log.1").stat().st_size == 5000
    assert log_file.read_text(encoding="utf-8").endswith("第一行\n")


# ---------------------------------------------------------------- 没配时和以前一样


def test_without_a_log_file_logs_stay_on_stderr_and_access_lines_on_stdout(tmp_path, capsys):
    configure_serve_logging(make_settings(tmp_path))

    logging.getLogger("meeting_workbench.relay_client").warning("应用自己的日志")
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    emit_access("GET", "/api/search?q=机密项目预算", 200)
    emit_access("GET", "/api/health", 200)
    emit_access("GET", "/api/health", 503)
    flush_all()

    captured = capsys.readouterr()
    assert re.search(
        TIMESTAMP + r"WARNING meeting_workbench\.relay_client 应用自己的日志$",
        captured.err,
        re.MULTILINE,
    )
    assert "INFO:     Application startup complete." in captured.err
    # uvicorn 自带的访问日志格式不变，只是查询串没了、成功的轮询没了
    assert captured.out.splitlines() == [
        'INFO:     127.0.0.1:50000 - "GET /api/search HTTP/1.1" 200 OK',
        'INFO:     127.0.0.1:50000 - "GET /api/health HTTP/1.1" 503 Service Unavailable',
    ]


def test_app_loggers_created_before_configuring_stay_enabled(tmp_path):
    early = logging.getLogger("meeting_workbench.created_before_configure")
    configure_serve_logging(make_settings(tmp_path, log_file=tmp_path / "web.log"))

    early.info("早就建好的 logger")
    flush_all()

    assert early.disabled is False
    assert "早就建好的 logger" in (tmp_path / "web.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 设置


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"log_max_bytes": 0}, "log_max_bytes"),
        ({"log_max_bytes": -1}, "log_max_bytes"),
        # 备份数为 0 时 RotatingFileHandler 永远不轮转，等于没限制
        ({"log_backup_count": 0}, "log_backup_count"),
    ],
)
def test_log_limits_must_actually_limit(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        make_settings(tmp_path, **overrides)


def test_log_settings_defaults_and_blank_or_tilde_values(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    assert settings.log_file is None
    assert (settings.log_max_bytes, settings.log_backup_count) == (20 * 1024 * 1024, 5)

    # .env 里写成空的等于没配
    monkeypatch.setenv("MEETING_WORKBENCH_LOG_FILE", "  ")
    assert make_settings(tmp_path).log_file is None

    monkeypatch.setenv("MEETING_WORKBENCH_LOG_FILE", "~/logs/web.log")
    configured = make_settings(tmp_path)
    assert configured.log_file is not None and "~" not in str(configured.log_file)
    assert configured.log_file.name == "web.log"


# ---------------------------------------------------------------- serve 的接线


def run_serve(monkeypatch, tmp_path, *, log_file):
    """跑 cli.main(["serve"])，但应用和 uvicorn 都换成记账的替身。"""
    for key, value in {
        "MEETING_WORKBENCH_DATA_DIR": tmp_path / "data",
        "MEETING_WORKBENCH_ARCHIVE_ROOT": tmp_path / "archive",
        "MEETING_WORKBENCH_STAGING_ROOT": tmp_path / "staging",
        "MEETING_WORKBENCH_RELAY_JOBS_DB": tmp_path / "relay.sqlite3",
        "MEETING_WORKBENCH_SEMANTIC_ENABLED": "0",
        "MEETING_WORKBENCH_PORT": "8899",
    }.items():
        monkeypatch.setenv(key, str(value))
    if log_file is None:
        monkeypatch.delenv("MEETING_WORKBENCH_LOG_FILE", raising=False)
    else:
        monkeypatch.setenv("MEETING_WORKBENCH_LOG_FILE", str(log_file))
    seen: dict[str, object] = {}

    def fake_create_app(settings):
        # 建应用的时候就要能打日志：这时 root 上的 handler 必须已经配好
        seen["handlers_while_building"] = list(logging.getLogger().handlers)
        return "the-app"

    def fake_run(app, **kwargs):
        seen["app"] = app
        seen["kwargs"] = kwargs

    import uvicorn

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli, "_raise_open_file_limit", lambda: None)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["serve"]) == 0
    return seen


def test_serve_with_a_log_file_writes_into_it_from_the_very_first_line(monkeypatch, tmp_path):
    log_file = tmp_path / "logs" / "web.log"

    seen = run_serve(monkeypatch, tmp_path, log_file=log_file)

    handlers = seen["handlers_while_building"]
    assert [type(handler) for handler in handlers] == [logging.handlers.RotatingFileHandler]
    assert handlers[0].baseFilename == str(log_file)
    assert (handlers[0].maxBytes, handlers[0].backupCount) == (20 * 1024 * 1024, 5)
    assert seen["app"] == "the-app"
    # 日志已经配好，不让 uvicorn 再套一份它自带的；其余参数照旧
    assert seen["kwargs"] == {
        "host": "127.0.0.1",
        "port": 8899,
        "access_log": True,
        "log_config": None,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
    }


def test_serve_without_a_log_file_keeps_logging_to_the_console(monkeypatch, tmp_path):
    seen = run_serve(monkeypatch, tmp_path, log_file=None)

    handlers = seen["handlers_while_building"]
    assert [type(handler) for handler in handlers] == [logging.StreamHandler]
    assert handlers[0].stream is sys.stderr
    assert seen["kwargs"]["log_config"] is None
