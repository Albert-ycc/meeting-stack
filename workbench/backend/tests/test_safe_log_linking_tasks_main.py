"""AI 日志只记类型名和位置，补上第一批（test_safe_log.py）没收到的几处：

- project_linking：会议项目归类失败；
- tasks：抽需求候选失败、需求候选没存上；会后抽需求候选的 INFO 日志原来把「没出的原因」整个打出来
  （里面是 AI 给的需求名，写库出错时还有错误消息），re_extract 的 INFO 日志原来带了补充说明的前 40 个字；
- main.py：读材料文字的后台循环（材料内容、材料录音转写、材料向量、补材料全文表）外层的异常日志。
"""

from __future__ import annotations

import ast
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_workbench import main as main_module
from meeting_workbench import material_fts, requirement_candidates
from meeting_workbench.config import Settings
from meeting_workbench.project_linking import ProjectLinker
from meeting_workbench.tasks import TaskService

from .requirement_pool_world import meeting_id, seed_minutes
from .test_project_linking import make_db, make_project, seed_meeting
from .test_requirement_extraction import (
    CHASE_TASK,
    EXPORT,
    SCRIPT,
    extract_candidates,
    fake_ai,
    make_world,
    reply,
    scan,
)
from .test_safe_log import SECRET, explode_with_secret, only_type_and_place

# AI 给的需求名：同名出两条，第二条会被跳过，「没出的原因」里就带着这个名字
SECRET_TITLE = "机密需求名：甲方底价谈判口径"
DUPLICATED = {"title": SECRET_TITLE, "summary": "", "anchor_quote": ""}


# ---------------------------------------------------------------------- project_linking


def test_project_link_failure_logs_only_type_and_place(tmp_path, monkeypatch, caplog):
    db, settings = make_db(tmp_path)
    make_project(db, "云图科研用药")
    seed_meeting(db, "vm-0", "云图科研用药周会")
    linker = ProjectLinker(db, settings)
    monkeypatch.setattr(linker, "_link_one", explode_with_secret)

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.project_linking"):
        stats = linker.link_pending()

    assert stats["failed"] == 1
    assert "会议项目归类失败 link_id=" in caplog.text
    only_type_and_place(caplog)


# ---------------------------------------------------------------------- tasks


def test_manual_candidate_extraction_failure_logs_only_type_and_place(
    tmp_path, monkeypatch, caplog
):
    client, headers, db, _settings = make_world(tmp_path)
    seed_minutes(db, "cvm")
    monkeypatch.setattr(TaskService, "_call_llm", explode_with_secret)

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.tasks"):
        response = extract_candidates(client, headers, "cvm")

    assert response.json()["status"] == "failed"
    assert "抽需求候选失败 meeting=" in caplog.text
    only_type_and_place(caplog)


def test_candidate_save_failure_after_a_meeting_logs_only_type_and_place(
    tmp_path, monkeypatch, caplog
):
    _client, _headers, db, settings = make_world(tmp_path)
    seed_minutes(db, "cvm")
    fake_ai(monkeypatch, reply(SCRIPT, tasks=[CHASE_TASK]))
    monkeypatch.setattr(requirement_candidates, "save_extracted", explode_with_secret)

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.tasks"):
        scan(db, settings, "cvm")

    assert "需求候选没存上，任务照常 meeting=" in caplog.text
    only_type_and_place(caplog)


def test_candidate_info_logs_keep_counts_not_the_ai_names(tmp_path, monkeypatch, caplog):
    client, headers, db, settings = make_world(tmp_path)
    seed_minutes(db, "cvm")
    fake_ai(monkeypatch, reply(DUPLICATED, DUPLICATED, EXPORT))

    with caplog.at_level(logging.INFO, logger="meeting_workbench.tasks"):
        scan(db, settings, "cvm")
        after_meeting = caplog.text
        caplog.clear()
        assert extract_candidates(client, headers, "cvm").json()["status"] == "done"
        manual = caplog.text

    for text in (after_meeting, manual):
        assert "没出=1" in text
        assert SECRET_TITLE not in text and "甲方底价" not in text


def test_re_extract_info_log_has_the_length_of_the_supplement_not_its_words(
    tmp_path, monkeypatch, caplog
):
    client, headers, db, _settings = make_world(tmp_path)
    seed_minutes(db, "cvm")
    fake_ai(monkeypatch, reply(EXPORT))

    with caplog.at_level(logging.INFO, logger="meeting_workbench.tasks"):
        response = client.post(
            f"/api/meetings/{meeting_id('cvm')}/tasks/re-extract",
            json={"supplement": SECRET},
            headers=headers,
        )

    assert response.json() == {"status": "done"}
    assert f"补充说明 {len(SECRET)} 字" in caplog.text
    assert SECRET not in caplog.text and "底价" not in caplog.text


# ---------------------------------------------------------------------- main.py 读材料文字的循环

LOOPS = ("material_content_loop", "material_media_loop", "material_embed_loop", "material_fts_task")


def _loop_handlers(name: str) -> list[ast.ExceptHandler]:
    tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    loop = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    return [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "Exception"
    ]


@pytest.mark.parametrize("name", LOOPS)
def test_material_loops_log_only_type_and_place(name):
    """循环是 create_app 里的闭包、一开始还要等几秒，驱动不了，按源码查：外层 except Exception 里写的是
    logger.error(…, describe_error(error))，不是 logger.exception，也不带 exc_info。"""
    handlers = _loop_handlers(name)
    assert len(handlers) == 1, name
    handler = handlers[0]
    assert handler.name, "要写成 except Exception as error，才能把它交给 describe_error"
    calls = [node for node in ast.walk(handler) if isinstance(node, ast.Call)]
    logged = [
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "logger"
    ]
    assert [call.func.attr for call in logged] == ["error"], name
    described = [
        arg
        for arg in logged[0].args
        if isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Name)
        and arg.func.id == "describe_error"
        and [item.id for item in arg.args if isinstance(item, ast.Name)] == [handler.name]
    ]
    assert len(described) == 1, name
    assert not logged[0].keywords, name  # 没有 exc_info=


def test_material_fts_task_logs_only_type_and_place(tmp_path, monkeypatch, caplog):
    """补材料全文表这一个是启动时立刻跑的，可以真起一遍服务看日志（describe_error 的导入也在这里验到）。"""
    monkeypatch.setattr(material_fts, "rebuild_pending", lambda _db: True)
    monkeypatch.setattr(material_fts, "run_rebuild", explode_with_secret)
    (tmp_path / "archive").mkdir()
    (tmp_path / "staging").mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "workbench.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )

    with caplog.at_level(logging.ERROR, logger="meeting_workbench.main"):
        with TestClient(main_module.create_app(settings)):
            deadline = time.monotonic() + 10
            while "补材料全文表失败" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.05)

    assert "补材料全文表失败，下次启动接着补：RuntimeError @ " in caplog.text
    only_type_and_place(caplog)
