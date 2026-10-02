"""任务视图（任务池、待办、项目详情任务面板、项目看板、需求详情、审核卡）的批量取数。

原来每条任务各开 5 次连接、查 5 次（任务池 limit=1000 约 4500 条语句，890 条任务量下约 5.4 秒），审核卡每场会、每条
任务还各查一遍挂需求的范围。现在每类关联一条 IN 查询取齐，语句条数不随任务数、会议数涨。

两道回归：
- 输出和改动前的实现逐字段一致：tests/golden/task_views.json 是改前的实现在 task_views_world 这份数据上的输出
  （时钟钉在 NOW）。有意改了输出的字段，用 `python -m tests.test_task_views_batch`（在 workbench/backend 下）重新生成。
- 语句条数、连接数在 3 倍会议、9 倍任务下基本不变（只有有没有这类数据才查的分支会差一两条）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meeting_workbench import project_work as project_work_module
from meeting_workbench import tasks as tasks_module
from meeting_workbench import todo
from meeting_workbench.requirements import get_requirement
from meeting_workbench.tasks import TaskService

from .task_views_world import (
    NOW,
    FrozenClock,
    build_world,
    counted,
    make_db,
    project_key,
    requirement_key,
)

GOLDEN = Path(__file__).parent / "golden" / "task_views.json"


def make_service(tmp_path: Path) -> TaskService:
    db, settings = make_db(tmp_path)
    return TaskService(db, settings)


def views(service: TaskService) -> dict[str, Any]:
    """每个视图一个无参调用；键是它在 golden 里的名字。"""
    calls: dict[str, Any] = {
        "list_tasks": lambda: service.list_tasks(limit=1000),
        "list_tasks_filtered": lambda: service.list_tasks(
            status="pending_confirm,confirmed", project_id=project_key(0), limit=1000
        ),
        "todo": lambda: todo.list_todo(service, now=NOW),
        "review_cards": lambda: todo.review_cards(service, now=NOW),
        "review_cards_project": lambda: todo.review_cards(
            service, project_id=project_key(1), now=NOW
        ),
        "task_detail": lambda: service.get_task("task-w00007"),
    }
    for index in range(4):
        calls[f"project_board_{index}"] = lambda index=index: service.project_board(
            project_key(index)
        )
        calls[f"project_work_{index}"] = lambda index=index: project_work_module.project_work(
            service, project_key(index)
        )
        calls[f"requirement_{index}"] = lambda index=index: get_requirement(
            service, requirement_key(index, 0), now=NOW
        )
    return calls


def snapshot(service: TaskService) -> dict[str, Any]:
    return json.loads(
        json.dumps({name: call() for name, call in views(service).items()}, ensure_ascii=False)
    )


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(tasks_module, "datetime", FrozenClock)


def test_views_match_the_output_before_batching(tmp_path, frozen_clock):
    service = make_service(tmp_path)
    build_world(service.db, meetings=8, tasks_per_meeting=3)

    assert snapshot(service) == json.loads(GOLDEN.read_text(encoding="utf-8"))


def counts(tmp_path: Path, monkeypatch, *, meetings: int, tasks_per_meeting: int) -> dict[str, Any]:
    service = make_service(tmp_path)
    build_world(service.db, meetings=meetings, tasks_per_meeting=tasks_per_meeting)
    result = {}
    for name, call in views(service).items():
        with counted(monkeypatch) as tally:
            call()
        result[name] = (tally["statements"], tally["connections"])
    return result


def test_statement_count_does_not_grow_with_tasks_or_meetings(tmp_path, monkeypatch, frozen_clock):
    small = counts(tmp_path / "small", monkeypatch, meetings=10, tasks_per_meeting=4)
    large = counts(tmp_path / "large", monkeypatch, meetings=30, tasks_per_meeting=12)

    for name, (statements, connections) in large.items():
        small_statements, small_connections = small[name]
        # 有没有这类数据才查的分支（比如这批任务里有没有挂候选的）会让语句多一两条，和任务数、会议数无关；
        # 改前同样的跳变是几百条（任务池一次约 4500 条）
        assert statements <= small_statements + 4, name
        assert connections <= small_connections + 2, name
        assert statements < 40 and connections < 20, name


if __name__ == "__main__":
    import tempfile

    tasks_module.datetime = FrozenClock  # type: ignore[misc]
    with tempfile.TemporaryDirectory() as work:
        generated = make_service(Path(work))
        build_world(generated.db, meetings=8, tasks_per_meeting=3)
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(
            json.dumps(
                snapshot(generated), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            encoding="utf-8",
        )
    print(f"已写入 {GOLDEN}（{GOLDEN.stat().st_size} 字节）")
