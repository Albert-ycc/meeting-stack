"""第四期 4g：问答的三个接口——prepare 从不调 AI、「将发送 N 段材料原文给 …」、三种 llm、错误和计数、任务状态、
没有分数类的键、提示词里没有文件名和位置、日志里没有问题和回答。

测「将发送 …」那一行的都显式传 llm_api_base，另建临时 key 文件（conftest 把 AI 地址指到 127.0.0.1:9）。
任务用 AskRegistry(spawn=…) 在当场跑完或先不跑；假的 chat 记下 (system, user) 和参数。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from meeting_workbench import asks
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.links_llm import read_usage
from meeting_workbench.llm import ChatReply, LLMError
from meeting_workbench.main import create_app

from .test_ask_retrieval import KEY_QUOTE, online, world
from .test_tasks_api import FakeRelayClient
from .test_timeline import shanghai  # noqa: F401  用例的会议时间按 +08:00 写，日期按北京时间断言

DEEPSEEK = "https://api.deepseek.com"
QUESTION = "驻场服务的报价单"
ANSWER = "报价按第三版，总价下调五个点[D1][M1]。"
FORBIDDEN_KEYS = {"score", "rank", "chunk_id", "segment_id"}


class FakeChat:
    def __init__(self, reply=ANSWER, finish="stop", error=None):
        self.calls: list[dict] = []
        self.reply = reply
        self.finish = finish
        self.error = error

    def __call__(self, settings, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ChatReply(self.reply, self.finish)


def make(tmp_path, *, base=DEEPSEEK, key=True, spawn="now", clock=None, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    key_file = tmp_path / "ds-key"
    if key:
        key_file.write_text("sk-test", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        lark_webhook_url="",
        llm_api_key_file=key_file,
        llm_api_base=base,
        material_browse_root=tmp_path / "browse",
        **overrides,
    )
    app = create_app(settings, FakeRelayClient())
    client = TestClient(app)
    world(tmp_path, Database(settings.database_path))
    pending: list = []
    if spawn == "now":
        run = lambda fn: fn()  # noqa: E731
    elif spawn == "later":
        run = pending.append
    else:
        run = spawn
    service = app.state.asks
    service.registry = asks.AskRegistry(clock=clock or (lambda: 0.0), spawn=run)
    service.chat = FakeChat()
    service.state_of = online
    service.busy = lambda: False
    token = client.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": token, "Origin": "http://testserver"}
    return client, app, headers, pending


def prepare(client, headers, question=QUESTION, project="p"):
    return client.post(
        f"/api/projects/{project}/ask/prepare", json={"question": question}, headers=headers
    )


def ask(client, headers, plan_id, with_materials=True, project="p"):
    return client.post(
        f"/api/projects/{project}/ask",
        json={"plan_id": plan_id, "with_materials": with_materials},
        headers=headers,
    )


def usage(app):
    with app.state.db.autocommit() as connection:
        return read_usage(connection, app.state.links_llm_worker._day())["qa"]


def keys_in(payload):
    found = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= keys_in(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= keys_in(item)
    return found


# ---------------------------------------------------------------------- prepare


def test_prepare_never_calls_ai_and_says_what_will_be_sent(tmp_path):
    client, app, headers, _ = make(tmp_path)
    response = prepare(client, headers)
    assert response.status_code == 200
    body = response.json()
    assert app.state.asks.chat.calls == []
    assert set(body) == {
        "plan_id",
        "expires_in",
        "question",
        "counts",
        "confirm",
        "local_model",
        "llm",
        "highlight",
        "sources",
        "notes",
        "unattributed_meetings",
    }
    materials = body["counts"]["materials"]
    assert materials > 0 and body["counts"]["meetings"] > 0
    assert body["confirm"] == {
        "text": f"将发送 {materials} 段材料原文给 api.deepseek.com",
        "host": "api.deepseek.com",
    }
    assert body["llm"] == "ok" and body["local_model"] is False and body["expires_in"] == 600
    assert body["question"] == QUESTION and "驻场服务" in body["highlight"]
    assert body["unattributed_meetings"] == 1
    assert usage(app) == 0


def test_only_meeting_sources_have_no_confirm(tmp_path):
    client, _app, headers, _ = make(tmp_path)
    body = prepare(client, headers, "单列").json()
    assert body["counts"]["materials"] == 0 and body["counts"]["meetings"] >= 1
    assert body["confirm"] is None


def test_local_model_still_gets_the_line(tmp_path):
    client, _app, headers, _ = make(tmp_path, base="http://127.0.0.1:11434/v1")
    body = prepare(client, headers).json()
    materials = body["counts"]["materials"]
    assert body["confirm"] == {
        "text": f"将发送 {materials} 段材料原文给 127.0.0.1",
        "host": "127.0.0.1",
    }
    assert body["local_model"] is True
    assert {"kind": "local_model", "text": "用本机模型回答"} in body["notes"]


def test_llm_states(tmp_path):
    client, _app, headers, _ = make(tmp_path / "a", key=False)
    body = prepare(client, headers).json()
    assert body["llm"] == "no_key" and body["confirm"] is None
    client, _app, headers, _ = make(tmp_path / "b", qa_daily_questions=0)
    assert prepare(client, headers).json()["llm"] == "off"
    assert client.get("/api/bootstrap").json()["ask_enabled"] is False
    client, app, headers, _ = make(tmp_path / "c", qa_daily_questions=1)
    assert client.get("/api/bootstrap").json()["ask_enabled"] is True
    assert app.state.links_llm_worker.charge("qa")
    body = prepare(client, headers).json()
    assert body["llm"] == "capped" and body["confirm"] is None


def test_prepare_validation(tmp_path):
    client, app, headers, _ = make(tmp_path)
    for question in ("报", "报" * 301):
        response = prepare(client, headers, question)
        assert response.status_code == 422
        assert response.json()["detail"] == "问题最少 2 个字，最多 300 个字"
    response = prepare(client, headers, "驻场\x00服务")
    assert response.status_code == 422 and response.json()["detail"] == "问题里有不能用的控制字符"
    ok = prepare(client, headers, "驻场服务\n的报价单")
    assert ok.status_code == 200 and ok.json()["question"] == "驻场服务 的报价单"
    missing = prepare(client, headers, project="nope")
    assert missing.status_code == 404 and missing.json()["detail"] == "项目不存在"
    # 问题从不进网址：GET 不受理（FastAPI 默认 405；打包过前端时根路径的静态挂载接走，回 404）
    assert client.get(f"/api/projects/p/ask/prepare?question={QUESTION}").status_code in (404, 405)
    assert app.state.asks.registry._plans.keys() == {ok.json()["plan_id"]}
    extra = client.post(
        "/api/projects/p/ask/prepare", json={"question": QUESTION, "x": 1}, headers=headers
    )
    assert extra.status_code == 422


def test_overlong_question_gets_the_spec_422_without_echoing_it(tmp_path):
    client, _app, headers, _ = make(tmp_path)
    question = "绝密问题原文丙" * 1000
    response = prepare(client, headers, question)
    assert response.status_code == 422
    assert response.json() == {"detail": "问题最少 2 个字，最多 300 个字"}
    assert "绝密问题原文丙" not in response.text


# ---------------------------------------------------------------------- ask


def test_ask_runs_and_counts(tmp_path):
    client, app, headers, _ = make(tmp_path)
    plan = prepare(client, headers).json()
    response = ask(client, headers, plan["plan_id"])
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "waiting" and body["text"] == "在等 AI 回答"
    assert usage(app) == 1
    job = client.get(f"/api/ask/{body['job_id']}").json()
    assert job["state"] == "done"
    assert job["answer"] == {
        "text": ANSWER,
        "cited": ["D1", "M1"],
        "found": True,
        "no_evidence": False,
        "truncated": False,
    }
    assert job["sent"] == plan["counts"]
    assert all(source["sent"] for source in job["sources"])
    assert len(job["sources"]) == len(plan["sources"])
    # ［再问一次］：同一个计划重发，也算一次
    again = ask(client, headers, plan["plan_id"])
    assert again.status_code == 202 and usage(app) == 2


def test_only_meetings_sends_no_material(tmp_path):
    client, app, headers, _ = make(tmp_path)
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"], with_materials=False).json()["job_id"]
    (call,) = app.state.asks.chat.calls
    assert 'kind="材料"' not in call["user"]
    for source in plan["sources"]:
        if source["kind"] == "material":
            assert source["text"] not in call["user"]
    job = client.get(f"/api/ask/{job_id}").json()
    # 回答里的 M1 没发过，删掉
    assert job["answer"]["cited"] == ["D1"] and "[M1]" not in job["answer"]["text"]
    assert job["sent"] == {"meetings": plan["counts"]["meetings"], "materials": 0}
    assert all(source["sent"] is (source["kind"] != "material") for source in job["sources"])


def test_ask_errors_before_counting(tmp_path):
    ticks = {"now": 0.0}
    client, app, headers, _ = make(
        tmp_path, spawn="later", clock=lambda: ticks["now"], qa_daily_questions=2
    )
    plan = prepare(client, headers).json()
    for bad in ("nope", ""):
        response = ask(client, headers, bad or "x")
        assert response.status_code == 404
        assert response.json()["detail"] == "这次找到的原话过期了，请再问一次"
    # 别的项目的计划号当不存在
    assert ask(client, headers, plan["plan_id"], project="q").status_code == 404
    # 选的来源是空的
    only_material = prepare(client, headers, "另算", project="q").json()
    assert only_material["counts"] == {"meetings": 0, "materials": 1}
    empty = ask(client, headers, only_material["plan_id"], with_materials=False, project="q")
    assert empty.status_code == 422 and empty.json()["detail"] == "这次没有可以发送的原话"
    assert usage(app) == 0
    # 在答时再问：409
    assert ask(client, headers, plan["plan_id"]).status_code == 202
    busy = ask(client, headers, plan["plan_id"])
    assert busy.status_code == 409 and busy.json()["detail"] == "这个项目上一个问题还在回答"
    assert usage(app) == 1
    # 过期（假时钟）
    ticks["now"] = 601.0
    assert ask(client, headers, plan["plan_id"]).status_code == 404
    # with_materials 必须明说
    missing = client.post("/api/projects/p/ask", json={"plan_id": plan["plan_id"]}, headers=headers)
    assert missing.status_code == 422


def test_ask_cap_and_503s(tmp_path):
    client, app, headers, _ = make(tmp_path, qa_daily_questions=1)
    plan = prepare(client, headers).json()
    assert ask(client, headers, plan["plan_id"]).status_code == 202
    capped = ask(client, headers, plan["plan_id"])
    assert (
        capped.status_code == 429 and capped.json()["detail"] == "今天问答的次数到上限了，明天再问"
    )
    assert usage(app) == 1

    client, app, headers, _ = make(tmp_path / "nokey", key=False)
    plan = prepare(client, headers).json()
    response = ask(client, headers, plan["plan_id"])
    assert response.status_code == 503
    assert response.json()["detail"] == "没配置 AI，先列出找到的原话"
    assert "/" not in response.json()["detail"] and usage(app) == 0

    client, app, headers, _ = make(tmp_path / "off", qa_daily_questions=0)
    plan = prepare(client, headers).json()
    response = ask(client, headers, plan["plan_id"])
    assert (
        response.status_code == 503
        and response.json()["detail"] == "问答的 AI 回答已关闭，先列出找到的原话"
    )
    assert "MEETING_WORKBENCH_" not in response.text and usage(app) == 0


def test_materials_need_the_confirm_line(tmp_path):
    """prepare 时 AI 不能用（页面没写「将发送」那一行）的计划，带材料发回 404，不调 AI、不计数；只用会议可以。"""
    client, app, headers, _ = make(tmp_path, qa_daily_questions=1)
    worker = app.state.links_llm_worker
    assert worker.charge("qa")
    plan = prepare(client, headers).json()
    assert plan["llm"] == "capped" and plan["confirm"] is None and plan["counts"]["materials"] > 0
    worker.refund("qa")
    response = ask(client, headers, plan["plan_id"], with_materials=True)
    assert (
        response.status_code == 404
        and response.json()["detail"] == "这次找到的原话过期了，请再问一次"
    )
    assert app.state.asks.chat.calls == [] and usage(app) == 0
    assert ask(client, headers, plan["plan_id"], with_materials=False).status_code == 202
    (call,) = app.state.asks.chat.calls
    assert 'kind="材料"' not in call["user"]


def test_spawn_failure_refunds_and_releases(tmp_path):
    client, app, headers, _ = make(tmp_path)

    def broken(_fn):
        raise RuntimeError("can't start new thread")

    registry = app.state.asks.registry
    registry.spawn = broken
    plan = prepare(client, headers).json()
    response = ask(client, headers, plan["plan_id"])
    assert (
        response.status_code == 503 and response.json()["detail"] == "出了点问题，先列出找到的原话"
    )
    assert usage(app) == 0
    assert not registry.project_busy("p") and registry._running == {}
    # 名额都还在
    for _ in range(2):
        assert registry._slots.acquire(timeout=0)
    for _ in range(2):
        registry._slots.release()
    registry.spawn = lambda fn: fn()
    assert ask(client, headers, plan["plan_id"]).status_code == 202 and usage(app) == 1


# ---------------------------------------------------------------------- 任务


def test_job_view_is_copied_under_the_lock(tmp_path):
    """轮询在锁里复制 (state, payload, …)：拿到的一份不会一半是 done、一半没有回答。"""
    client, app, headers, pending = make(tmp_path, spawn="later")
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    registry = app.state.asks.registry
    before = registry.view_job(job_id)
    assert before.state == "waiting" and before.payload == {}
    pending.pop()()
    assert before.state == "waiting" and before.payload == {}
    after = registry.view_job(job_id)
    assert after.state == "done" and "answer" in after.payload and after.with_materials is True
    body = client.get(f"/api/ask/{job_id}").json()
    assert body["state"] == "done" and body["answer"]["text"] == ANSWER


def test_job_waiting_then_done_then_expired(tmp_path):
    ticks = {"now": 0.0}
    client, _app, headers, pending = make(tmp_path, spawn="later", clock=lambda: ticks["now"])
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    assert client.get(f"/api/ask/{job_id}").json() == {"state": "waiting", "text": "在等 AI 回答"}
    pending.pop()()
    assert client.get(f"/api/ask/{job_id}").json()["state"] == "done"
    ticks["now"] = 1801.0 + 1
    gone = client.get(f"/api/ask/{job_id}")
    assert gone.status_code == 404 and gone.json()["detail"] == "这次的回答过期了，请再问一次"


def test_job_times_out_and_releases_the_project(tmp_path):
    ticks = {"now": 0.0}
    client, _app, headers, pending = make(tmp_path, spawn="later", clock=lambda: ticks["now"])
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    job = pending.pop()
    service = _app.state.asks
    # 线程拿到位置、开始调用以后 95 秒还没回来
    stuck = service.registry.get_job(job_id)
    assert service.registry.acquire_slot(stuck)
    ticks["now"] = 96.0
    body = client.get(f"/api/ask/{job_id}").json()
    assert body["state"] == "stopped" and body["reason"] == "timeout" and body["retry"] is True
    assert body["text"] == "AI 没回（等了 90 秒），先列出找到的原话"
    assert body["sources"]
    # 项目锁放开了，可以再问；晚回来的结果丢掉
    assert ask(client, headers, plan["plan_id"]).status_code == 202
    job()
    assert client.get(f"/api/ask/{job_id}").json()["reason"] == "timeout"


@pytest.mark.parametrize(
    ("code", "reason", "text", "retry"),
    [
        ("timeout", "timeout", "AI 没回（等了 90 秒），先列出找到的原话", True),
        ("network", "network", "连不上 AI，先列出找到的原话", True),
        ("rate_limited", "rate_limited", "AI 那边太忙，过一会儿再问", True),
        ("server", "server", "AI 那边出错了，先列出找到的原话", True),
        ("auth", "auth", "AI 的 key 不对，先列出找到的原话", False),
        ("bad_request", "bad_request", "AI 不接受这次的请求，先列出找到的原话", False),
        # 余额不足：再问也一样，不给［再问一次］
        ("balance", "server", "AI 那边出错了，先列出找到的原话", False),
    ],
)
def test_llm_errors_become_stopped_reasons(tmp_path, code, reason, text, retry):
    client, app, headers, _ = make(tmp_path)
    app.state.asks.chat = FakeChat(error=LLMError(code, status=500))
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    body = client.get(f"/api/ask/{job_id}").json()
    assert body == {
        "state": "stopped",
        "reason": reason,
        "text": text,
        "retry": retry,
        "sources": body["sources"],
    }
    assert len(body["sources"]) == len(plan["sources"])


def test_not_sent_errors_and_other_failures(tmp_path):
    client, app, headers, _ = make(tmp_path)
    app.state.asks.chat = FakeChat(error=LLMError("network", sent=False))
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    assert client.get(f"/api/ask/{job_id}").json()["reason"] == "network"
    # 请求没到服务器：退回这一次
    assert usage(app) == 0
    app.state.asks.chat = FakeChat(error=ValueError("boom"))
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    body = client.get(f"/api/ask/{job_id}").json()
    assert (
        body["reason"] == "error"
        and body["text"] == "出了点问题，先列出找到的原话"
        and body["retry"] is True
    )


def test_slots_and_answer_flags(tmp_path):
    client, app, headers, _ = make(tmp_path)
    registry = app.state.asks.registry
    registry.slot_wait = 0.01
    for _ in range(2):
        registry._slots.acquire()
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    body = client.get(f"/api/ask/{job_id}").json()
    assert body["reason"] == "slots" and body["text"] == "AI 正在回答别的问题，过一会儿再问"
    assert usage(app) == 0
    for _ in range(2):
        registry._slots.release()
    app.state.asks.chat = FakeChat(reply="说了个结论[Z1]")
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    answer = client.get(f"/api/ask/{job_id}").json()["answer"]
    assert answer["no_evidence"] is True and answer["text"] == ""
    app.state.asks.chat = FakeChat(reply="没找到", finish="length")
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    answer = client.get(f"/api/ask/{job_id}").json()["answer"]
    assert (
        answer["found"] is False and answer["truncated"] is True and answer["no_evidence"] is False
    )


# ---------------------------------------------------------------------- 键、提示词、日志


def test_no_score_like_keys_anywhere(tmp_path):
    client, _app, headers, _ = make(tmp_path)
    plan = prepare(client, headers).json()
    started = ask(client, headers, plan["plan_id"]).json()
    job = client.get(f"/api/ask/{started['job_id']}").json()
    for payload in (plan, started, job):
        assert not keys_in(payload) & FORBIDDEN_KEYS


def test_prompt_has_no_file_names_or_locations(tmp_path):
    client, app, headers, _ = make(tmp_path)
    plan = prepare(client, headers).json()
    ask(client, headers, plan["plan_id"])
    (call,) = app.state.asks.chat.calls
    assert call["system"] == asks.QA_SYSTEM
    assert call["json_mode"] is False and call["max_tokens"] == 900
    assert call["retries"] == 0 and call["timeout"] == 90
    user = call["user"]
    for source in plan["sources"]:
        if source["kind"] == "material":
            assert f'<source id="{source["id"]}" kind="材料">' in user
            assert source["name"] not in user
            if source["loc"]:
                assert source["loc"] not in user
    for leak in (
        " name=",
        " file=",
        " path=",
        " loc=",
        "云图资料",
        "共用说明",
        "报价单 v3.xlsx",
        "云图AI",
        KEY_QUOTE,
    ):
        assert leak not in user
    assert str(tmp_path) not in user


def test_logs_have_no_question_sources_or_answer(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client, _app, headers, _ = make(tmp_path)
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    client.get(f"/api/ask/{job_id}")
    assert "问答回来" in caplog.text
    for secret in (
        QUESTION,
        "驻场",
        "报价",
        ANSWER,
        "下调五个点",
        *[s["text"] for s in plan["sources"]],
    ):
        assert secret not in caplog.text


# ---------------------------------------------------------------------- 命令行


def test_cli_links_ask_reads_stdin_and_never_calls_ai(tmp_path, monkeypatch, capsys):
    import io

    from meeting_workbench import cli

    path = tmp_path / "workbench.sqlite3"
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(path))
    monkeypatch.setenv("MEETING_WORKBENCH_SEMANTIC_ENABLED", "0")
    monkeypatch.setenv("MEETING_WORKBENCH_LLM_API_BASE", DEEPSEEK)
    db = Database(path)
    db.initialize()
    world(tmp_path, db)
    stamp = path.stat().st_mtime_ns
    monkeypatch.setattr("sys.stdin", io.StringIO(QUESTION + "\n"))

    assert cli.main(["links", "ask", "--project", "p"]) == 0
    out = capsys.readouterr().out
    assert "找到会议里的" in out and "将发送" in out and "给 api.deepseek.com" in out
    assert "[D1] 决议 · 2026-09-21 初审规则沟通 · 12:34" in out
    assert "[M1] 材料 · 报价单 v3.xlsx · 表『预算』" in out
    assert path.stat().st_mtime_ns == stamp  # 只读
    # 问题不从参数进来：参数里没有问题的位置
    with pytest.raises(SystemExit):
        cli.main(["links", "ask", "--project", "p", QUESTION])
    monkeypatch.setattr("sys.stdin", io.StringIO("报"))
    assert cli.main(["links", "ask", "--project", "p"]) == 2


def test_cli_links_ask_loads_the_model_before_the_budget_starts(tmp_path, monkeypatch, capsys):
    """D13：命令行冷启动先加载模型，再开始计 2.5 秒的检索预算。"""
    import io

    from meeting_workbench import ask_retrieval, cli, semantic

    path = tmp_path / "workbench.sqlite3"
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(path))
    db = Database(path)
    db.initialize()
    world(tmp_path, db)
    order = []
    monkeypatch.setattr(semantic.SemanticIndex, "warm", lambda self: order.append("warm") or True)
    real_retrieve = ask_retrieval.retrieve
    monkeypatch.setattr(
        ask_retrieval,
        "retrieve",
        lambda *a, **kw: order.append("retrieve") or real_retrieve(*a, **kw),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(QUESTION + "\n"))

    assert cli.main(["links", "ask", "--project", "p"]) == 0
    assert order[:2] == ["warm", "retrieve"]


def test_malformed_key_file_refunds_and_says_the_key_is_wrong(tmp_path, caplog):
    """key 文件多了一行：请求没发出去，退回这一次；停了的原因是 key 不对；日志里没有 key。"""
    secret = "sk-FAKE-0123456789abcdef"
    client, app, headers, _ = make(tmp_path)
    app.state.settings.llm_api_key_file.write_text(f"{secret}\nold-key-xyz\n", encoding="utf-8")
    app.state.asks.chat = None
    caplog.set_level(logging.DEBUG)
    plan = prepare(client, headers).json()
    job_id = ask(client, headers, plan["plan_id"]).json()["job_id"]
    body = client.get(f"/api/ask/{job_id}").json()
    assert body["reason"] == "auth" and body["retry"] is False
    assert usage(app) == 0
    assert secret not in caplog.text
