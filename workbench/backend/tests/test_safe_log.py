"""把会议内容、提示词交给 AI 的后台循环和任务：意料之外的异常，日志里只有类型名和出错位置，
没有异常消息（里面可能带着会议原文、提示词）、没有整段 traceback、没有局部变量。

每一处都造一个消息里带「机密文字」的异常，断言日志里有类型名和出错的函数、文件、行号，没有那段文字。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading

import pytest

from meeting_workbench import cold_start, decisions, deep_links, glossary_checkup, links_llm
from meeting_workbench.llm import LLMError
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.safe_log import MAX_FRAMES, describe_error

from .test_ask_api import FakeChat, ask, make as make_ask_world, prepare
from .test_cold_start import _link, _weak
from .test_graph import add_meeting
from .test_links_loop import MINUTES, NOW, Clock, fake_heavy, worker
from .test_material_index import make
from .test_project_linking import make_db, make_project, seed_meeting

SECRET = "机密文字：甲方报价单的底价是 38.8 万"


def explode_with_secret(*_args, **_kwargs):
    raise RuntimeError(SECRET)


def explode_sqlite_with_secret(*_args, **_kwargs):
    raise sqlite3.OperationalError(SECRET)


def only_type_and_place(caplog, *, type_name="RuntimeError", function="explode_with_secret"):
    assert SECRET not in caplog.text
    assert "底价" not in caplog.text and "38.8" not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        # 不带 exc_info：整段 traceback（连同消息和源码行）不会被日志处理器格式化出来
        assert record.exc_info is None and record.exc_text is None
    assert f"{type_name} @ " in caplog.text
    assert f":{function}" in caplog.text
    assert "test_safe_log.py:" in caplog.text


async def no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------- 小函数本身


def test_describe_error_keeps_type_and_places_but_not_the_message_or_locals():
    def inner():
        private = SECRET  # noqa: F841  局部变量里的内容也不能出现
        raise ValueError(SECRET)

    def outer():
        inner()

    try:
        outer()
    except ValueError as error:
        brief = describe_error(error)

    assert brief.startswith("ValueError @ tests/test_safe_log.py:")
    assert ":inner" in brief and ":outer" in brief
    assert SECRET not in brief and "底价" not in brief


def test_describe_error_lists_at_most_a_few_frames_innermost_first():
    def recurse(depth):
        if depth == 0:
            raise KeyError(SECRET)
        recurse(depth - 1)

    try:
        recurse(10)
    except KeyError as error:
        brief = describe_error(error)

    assert brief.count(" < ") == MAX_FRAMES - 1
    assert brief.count(":recurse") == MAX_FRAMES
    assert SECRET not in brief


def test_describe_error_ignores_the_cause_and_context_messages():
    try:
        try:
            raise ValueError(SECRET)
        except ValueError as cause:
            raise RuntimeError("看不出内容的包装") from cause
    except RuntimeError as error:
        brief = describe_error(error)

    assert brief.startswith("RuntimeError @ ")
    assert SECRET not in brief


def test_describe_error_for_an_exception_that_was_never_raised():
    assert describe_error(RuntimeError(SECRET)) == "RuntimeError @ ?"


# ---------------------------------------------------------------------- 各个 AI 循环和任务


def test_links_llm_loop_logs_only_type_and_place(caplog):
    stop = threading.Event()

    class Worker:
        def refresh_state(self):
            explode_with_secret()

        def tick(self):
            stop.set()
            explode_with_secret()

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.links_llm"):
        asyncio.run(links_llm.links_llm_loop(Worker(), stop, sleep=no_sleep, first_delay=0))

    # 开头读状态那一处、这一次循环那一处，各一条
    assert caplog.text.count("RuntimeError @") == 2
    only_type_and_place(caplog)


def test_deep_links_steps_and_rounds_log_only_type_and_place(tmp_path, caplog):
    db, _ = make(tmp_path)
    w = worker(db, clock=Clock())
    fake_heavy(w, [], h2_affects=explode_with_secret, h4_terms=explode_sqlite_with_secret)
    with caplog.at_level(logging.ERROR, logger="meeting_workbench.deep_links"):
        result = w.run_round()
    assert result["phases"]["affects"] == "error" and result["phases"]["terms"] == "error"
    # 一般的错误和不是「被占用」的数据库错误各一条；步骤名还在消息里，方便知道是哪一步
    assert caplog.text.count("RuntimeError @") == 1 and caplog.text.count("OperationalError @") == 1
    assert "affects" in caplog.text and "terms" in caplog.text
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert record.exc_info is None and record.exc_text is None

    caplog.clear()
    stop = threading.Event()

    def broken_round():
        stop.set()
        explode_with_secret()

    w.run_round = broken_round
    with caplog.at_level(logging.ERROR, logger="meeting_workbench.deep_links"):
        asyncio.run(deep_links.links_loop(w, stop, sleep=no_sleep, first_delay=0))
    assert "关联整理这一轮失败" in caplog.text
    only_type_and_place(caplog)


def test_decision_ingest_and_parse_log_only_type_and_place(tmp_path, monkeypatch, caplog):
    db, _ = make(tmp_path)
    add_meeting(db, "m0", ago=1, project_id="p", minutes=MINUTES)
    monkeypatch.setattr(decisions, "ingest_meeting", explode_with_secret)
    with caplog.at_level(logging.ERROR, logger="meeting_workbench.decisions"):
        counts = decisions.ingest_pending(db, clock=Clock(), now=NOW)
    assert counts["skipped"] == 1
    assert "决议入库跳过一场会：m0" in caplog.text
    only_type_and_place(caplog)

    caplog.clear()
    monkeypatch.setattr(decisions, "parse_decisions", explode_with_secret)
    with caplog.at_level(logging.WARNING, logger="meeting_workbench.decisions"):
        parsed = decisions.parse_safely(MINUTES)
    assert parsed.items == []
    assert "决议段解析出错" in caplog.text
    only_type_and_place(caplog)


def test_minutes_checkup_failure_logs_only_type_and_place(tmp_path, monkeypatch, caplog):
    db, _ = make(tmp_path)
    add_meeting(db, "m0", ago=1, project_id="p", minutes=MINUTES)
    monkeypatch.setattr(glossary_checkup, "check_meeting", explode_with_secret)

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.glossary_checkup"):
        stats = glossary_checkup.run_pending(db, None)

    assert stats["checked"] == 0
    assert "纪要体检失败 meeting_id=m0" in caplog.text
    only_type_and_place(caplog)


def test_cold_start_reevaluation_failure_logs_only_type_and_place(tmp_path, monkeypatch, caplog):
    db, settings = make_db(tmp_path)
    project_id = make_project(db, "云图科研用药")
    seed_meeting(db, "vm-0", "云图科研用药周会")
    _weak(db, "vm-0", project_id)
    linker = ProjectLinker(db, settings)
    monkeypatch.setattr(linker, "_classify", explode_with_secret)

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.cold_start"):
        cold_start.run(db, settings, linker)

    assert _link(db, "vm-0")["attempts"] == 1
    assert "弱归属复评失败 meeting_id=vm-0" in caplog.text
    only_type_and_place(caplog)


def test_ask_failures_log_only_type_and_place(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client, app, headers, _pending = make_ask_world(tmp_path)

    # 任务里意料之外的错误
    app.state.asks.chat = explode_with_secret
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    assert client.get(f"/api/ask/{job_id}").json()["reason"] == "error"
    assert "问答出错：项目 p，RuntimeError @ " in caplog.text
    only_type_and_place(caplog)

    # 线程起不来
    caplog.clear()
    app.state.asks.registry.spawn = explode_with_secret
    assert ask(client, headers, plan["plan_id"]).status_code == 503
    assert "问答没起来：项目 p，RuntimeError @ " in caplog.text
    only_type_and_place(caplog)

    # 请求没发出去、退回用量时又出错
    caplog.clear()
    app.state.asks.registry.spawn = lambda fn: fn()
    app.state.asks.chat = FakeChat(error=LLMError("network", sent=False))
    app.state.asks.worker.refund = explode_with_secret
    ask(client, headers, plan["plan_id"])
    assert "问答退回用量没成：RuntimeError @ " in caplog.text
    only_type_and_place(caplog)


@pytest.mark.parametrize(
    "module",
    [links_llm, deep_links, decisions, glossary_checkup, cold_start],
    ids=lambda m: m.__name__,
)
def test_ai_modules_have_no_logger_exception_left(module):
    """这几个模块里不再有 logger.exception（它会把异常消息和整段 traceback 写进日志）。"""
    source = open(module.__file__, encoding="utf-8").read()
    assert "logger.exception(" not in source and "exc_info" not in source
