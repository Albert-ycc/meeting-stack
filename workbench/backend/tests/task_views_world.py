"""任务视图批量取数的回归世界：一批项目、需求、会议、候选、任务和事件，量大时拿来量语句条数和耗时。

覆盖的分支：任务各种状态、挂需求（进行中的、已搁置的）、挂候选（AI 配好的、人手动改挂过的）、没归项目的会、
没有来源会议的任务、停滞的和刚动过的、带评论的；候选各种状态、带相近需求。时间都从 NOW 往前推，用例把时钟钉在 NOW，
不取真实当前时间。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.project_profile import light_key

NOW = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)
PROJECT_COUNT = 4
# 任务状态按序号轮着来：待确认占大头，和生产里积压的样子一致
STATUS_CYCLE = (
    "pending_confirm",
    "pending_confirm",
    "confirmed",
    "in_progress",
    "done",
    "pending_confirm",
    "cancelled",
    "confirmed",
    "expired",
)
REQUIREMENT_CYCLE = (("active", "P0"), ("active", "P1"), ("done", "P2"), ("shelved", "P3"))
TOPICS = ("导出", "看板", "对账", "提醒", "权限", "入库", "回访", "审核")


DATA_STATEMENTS = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH")


def make_db(tmp_path: Path) -> tuple[Database, Settings]:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
    )
    db = Database(settings.database_path)
    db.initialize()
    return db, settings


@contextmanager
def counted(monkeypatch):
    """数这一段里执行的数据语句（SELECT、INSERT、UPDATE、DELETE）和开了几个连接。"""
    tally = {"statements": 0, "connections": 0}
    original = Database.connect

    def counting_connect(self) -> sqlite3.Connection:
        tally["connections"] += 1
        connection = original(self)

        def trace(statement: str) -> None:
            if statement.lstrip().upper().startswith(DATA_STATEMENTS):
                tally["statements"] += 1

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(Database, "connect", counting_connect)
    try:
        yield tally
    finally:
        monkeypatch.setattr(Database, "connect", original)


class FrozenClock(datetime):
    """顶替 tasks.datetime：now() 一直是 NOW，停滞天数才能和期望值逐位相等。"""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.astimezone().replace(tzinfo=None)


def ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def project_key(index: int) -> str:
    return f"project-w{index}"


def requirement_key(project: int, slot: int) -> str:
    return f"requirement-w{project}-{slot}"


def meeting_key(index: int) -> str:
    return f"meeting-w{index:03d}"


def build_world(db: Database, *, meetings: int = 8, tasks_per_meeting: int = 3) -> dict[str, Any]:
    """返回 {tasks: 任务总数, meetings: 会议 id 列表, projects: 项目 id 列表}。每 5 场会有 1 场没归项目；
    另有几条没来源会议的手动任务（项目页「未关联会议」那一组）。"""
    project_ids = [project_key(index) for index in range(PROJECT_COUNT)]
    meeting_ids = [meeting_key(index) for index in range(meetings)]

    def project_of(meeting_index: int) -> str | None:
        return None if meeting_index % 5 == 4 else project_ids[meeting_index % PROJECT_COUNT]

    with db.transaction() as connection:
        for index, project_id in enumerate(project_ids):
            connection.execute(
                "INSERT INTO projects(id, name, color, origin, created_at) VALUES (?, ?, ?, 'manual', ?)",
                (project_id, f"项目 {index}", f"#3f51b{index}", ago(days=60)),
            )
            for slot, (status, priority) in enumerate(REQUIREMENT_CYCLE):
                connection.execute(
                    """INSERT INTO requirements
                           (id, project_id, title, priority, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        requirement_key(index, slot),
                        project_id,
                        f"需求 {index}-{slot} {TOPICS[(index + slot) % len(TOPICS)]}",
                        priority,
                        status,
                        ago(days=50 - slot),
                        ago(days=40 - slot),
                    ),
                )
        for index, meeting_id in enumerate(meeting_ids):
            project_id = project_of(index)
            connection.execute(
                """INSERT INTO meetings
                       (id, title, recording_date, duration_ms, project_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    meeting_id,
                    f"会议 {index:03d} {TOPICS[index % len(TOPICS)]}",
                    ago(days=30 - index % 25, hours=index % 7).replace("+00:00", "+08:00"),
                    600000 + index * 1000,
                    project_id,
                    ago(days=31 - index % 25),
                    ago(days=31 - index % 25),
                ),
            )
            if project_id is not None and index % 2 == 0:
                # 偶数会关联所属项目的第一条进行中需求
                connection.execute(
                    "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
                    (requirement_key(index % PROJECT_COUNT, 0), meeting_id, ago(days=20)),
                )
            for slot in range(2):
                candidate_status = (
                    "pending"
                    if slot == 0
                    else ("pending", "claimed", "merged", "dropped")[index % 4]
                )
                title = f"候选 {index}-{slot} {TOPICS[(index + slot + 3) % len(TOPICS)]}"
                connection.execute(
                    """INSERT INTO requirement_candidates
                           (id, meeting_id, title, name_key, summary, similar_requirement_id, status,
                            requirement_id, dropped_at, project_id_seen, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"candidate-w{index:03d}-{slot}",
                        meeting_id,
                        title,
                        light_key(title),
                        f"说明 {index}-{slot}",
                        requirement_key(index % PROJECT_COUNT, 1)
                        if project_id is not None and index % 3 == 0
                        else None,
                        candidate_status,
                        requirement_key(index % PROJECT_COUNT, 0)
                        if candidate_status in ("claimed", "merged") and project_id is not None
                        else None,
                        ago(days=2) if candidate_status == "dropped" else None,
                        project_id,
                        ago(days=25 - index % 20, minutes=slot),
                        ago(days=25 - index % 20, minutes=slot),
                    ),
                )
                connection.execute(
                    """INSERT INTO requirement_sources
                           (candidate_id, kind, meeting_id, quote, anchor_ms, created_at)
                       VALUES (?, 'origin', ?, ?, ?, ?)""",
                    (
                        f"candidate-w{index:03d}-{slot}",
                        meeting_id,
                        f"原话 {index}-{slot}",
                        1000 * (slot + 1),
                        ago(days=25 - index % 20, minutes=slot),
                    ),
                )
        count = 0

        def insert_task(meeting_index: int | None, *, manual: bool = False) -> None:
            nonlocal count
            count += 1
            n = count
            status = STATUS_CYCLE[n % len(STATUS_CYCLE)]
            meeting_id = meeting_ids[meeting_index] if meeting_index is not None else None
            project_id = (
                project_of(meeting_index) if meeting_index is not None else project_ids[n % 2]
            )
            if n % 7 == 0 and project_id is not None:
                # 任务的项目和来源会议的项目不一定一致（手动改过项目）
                project_id = project_ids[(project_ids.index(project_id) + 1) % PROJECT_COUNT]
            project_index = project_ids.index(project_id) if project_id else None
            requirement_id = None
            if project_index is not None and n % 4 == 0:
                requirement_id = requirement_key(project_index, n % 2)
            elif project_index is not None and n % 11 == 0:
                requirement_id = requirement_key(project_index, 3)  # 现在挂着已搁置的需求
            candidate_id = None
            if meeting_index is not None and requirement_id is None and n % 3 == 1:
                candidate_id = f"candidate-w{meeting_index:03d}-0"
            # 完成的任务集中在最近两天里，晨报的「昨天完成」才有数可看
            changed = ago(hours=(n * 5) % (60 if status == "done" else 200) + 1)
            task_id = f"task-w{n:05d}"
            connection.execute(
                """INSERT INTO tasks
                       (id, title, detail, status, origin, assignee, meeting_id, project_id,
                        anchor_ms, anchor_quote, requirement_id, candidate_id, due_date, due_phrase,
                        status_changed_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    f"任务 {n:05d} {TOPICS[n % len(TOPICS)]}",
                    "" if n % 2 else f"补充说明 {n}",
                    status,
                    "manual" if manual else "ai",
                    "ai" if n % 2 else "me",
                    meeting_id,
                    project_id,
                    n * 1000 if meeting_id else None,
                    f"会上原话 {n}" if meeting_id else None,
                    requirement_id,
                    candidate_id,
                    f"2026-10-{n % 28 + 1:02d}" if n % 3 == 0 else None,
                    "月底前" if n % 3 == 0 else None,
                    changed,
                    (NOW - timedelta(days=30) + timedelta(seconds=n)).isoformat(),
                    changed,
                ),
            )
            created = (NOW - timedelta(days=30) + timedelta(seconds=n)).isoformat()
            events = [("created", "生成本条任务草稿", created)]
            if status != "pending_confirm" and status != "expired":
                events.append(("confirmed", "任务已确认", changed))
            if candidate_id and n % 6 == 1:
                events.append(("requirement_changed", "挂到候选", ago(hours=n % 50 + 1)))
            if n % 5 == 0:
                events.append(("comment", f"备注 {n}", ago(hours=n % 120 + 1)))
            if n % 10 == 0:
                events.append(("comment", f"又一条备注 {n}", ago(hours=n % 30 + 1)))
            if status == "done":
                events.append(("status_changed", "in_progress → done", changed))
            for kind, body, at in events:
                connection.execute(
                    "INSERT INTO task_events(task_id, kind, body, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, kind, body, at),
                )

        for meeting_index in range(meetings):
            for _ in range(tasks_per_meeting):
                insert_task(meeting_index)
        for _ in range(max(2, meetings // 4)):
            insert_task(None, manual=True)
    return {"tasks": count, "meetings": meeting_ids, "projects": project_ids}
