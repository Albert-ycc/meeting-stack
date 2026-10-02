"""项目详情（项目页与待办改版 261001，R06-3～13）：「需求与任务」「录音」两个标签页的数据。

需求与任务：进行中需求各带一张任务面板（已确认、进行中、已完成，未完成在前、刚完成的在末尾），需求的先后
同需求池（P0→P3、同级按最近会议由近到远，海报本身由前端取需求池接口）；已完成、已搁置的需求收在折叠区；
「没挂需求的任务」是本项目没挂需求的已确认、进行中任务（挂在待认领候选上的也在这里，带候选名）。
录音：本项目会议按录音时间倒序，每场带关联的需求和这场会抽出的任务数。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .project_seats import MEETING_TIME_SQL
from .requirement_candidates import reconcile_moved
from .requirements import project_meetings
from .service import NotFoundError
from .task_due import BEIJING_TZ
from .tasks import TaskService
from .todo import group_of, todo_order

PANEL_STATUSES = ("confirmed", "in_progress", "done")
UNLINKED_STATUSES = ("confirmed", "in_progress")
_PRIORITY_SQL = "CASE r.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END"


def _open_order(task: dict[str, Any]) -> tuple:
    """不分组的列表里未完成任务的先后：有截止的按截止由早到晚，没截止（或截止不合规）的沉在最后，
    同截止再按来源会议由近到远——和待办页逐组看下来的顺序一致。"""
    undated = group_of(task.get("due_date"), date.max) == "undated"
    return (undated, todo_order(task))


def _panel_order(task: dict[str, Any]) -> tuple:
    """未完成的在前，完成的在后、按完成先后（刚完成的在最末）。"""
    if task["status"] == "done":
        return (1, _timestamp(task.get("status_changed_at")), task["id"])
    return (0, _open_order(task))


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.timestamp()


def project_work(task_service: TaskService, project_id: str) -> dict[str, Any]:
    db = task_service.db
    if db.query_one("SELECT 1 AS ok FROM projects WHERE id=?", (project_id,)) is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    # 会议事后改了归属：候选按新项目重核去重，横幅和需求池的待认领数同一口径
    reconcile_moved(db)
    requirements = db.query_all(
        f"""SELECT r.id, r.title, r.status, r.priority,
                   (SELECT MAX(julianday({MEETING_TIME_SQL}))
                      FROM requirement_meetings rm JOIN meetings m ON m.id = rm.meeting_id
                     WHERE rm.requirement_id = r.id) AS latest_jd
              FROM requirements r
             WHERE r.project_id = ?
             ORDER BY {_PRIORITY_SQL}, latest_jd IS NULL, latest_jd DESC, r.created_at DESC""",
        (project_id,),
    )
    placeholders = ", ".join("?" for _ in PANEL_STATUSES)
    panel_rows = db.query_all(
        f"""SELECT t.* FROM tasks t JOIN requirements r ON r.id = t.requirement_id
             WHERE r.project_id = ? AND t.status IN ({placeholders})""",
        (project_id, *PANEL_STATUSES),
    )
    unlinked_rows = db.query_all(
        f"""SELECT * FROM tasks
             WHERE project_id = ? AND requirement_id IS NULL
               AND status IN ({", ".join("?" for _ in UNLINKED_STATUSES)})""",
        (project_id, *UNLINKED_STATUSES),
    )
    # 面板上的和没挂需求的任务一起取展示字段：语句条数不随任务数涨（原来逐条 task_summary，每条 5 次查询）
    summaries = task_service.task_summaries([*panel_rows, *unlinked_rows])
    tasks_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries[: len(panel_rows)]:
        tasks_by_requirement.setdefault(summary["requirement_id"], []).append(summary)
    active: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for requirement in requirements:
        tasks = sorted(tasks_by_requirement.get(requirement["id"], []), key=_panel_order)
        open_count = sum(1 for task in tasks if task["status"] != "done")
        base = {
            "id": requirement["id"],
            "title": requirement["title"],
            "status": requirement["status"],
            "priority": requirement["priority"],
            "task_count": len(tasks),
            "open_task_count": open_count,
        }
        if requirement["status"] == "active":
            active.append({**base, "tasks": tasks})
        else:
            closed.append({**base, "all_done": bool(tasks) and open_count == 0})
    unlinked = sorted(summaries[len(panel_rows) :], key=_open_order)
    pending_candidates = db.query_all(
        """SELECT c.id, c.title FROM requirement_candidates c
             JOIN meetings m ON m.id = c.meeting_id
            WHERE c.status = 'pending' AND m.project_id = ?
            ORDER BY c.created_at DESC, c.id""",
        (project_id,),
    )
    return {
        "project_id": project_id,
        "requirements": active,
        "closed_requirements": closed,
        "unlinked_tasks": unlinked,
        "pending_candidates": pending_candidates,
    }


def project_recordings(task_service: TaskService, project_id: str) -> list[dict[str, Any]]:
    """录音标签页：现有的项目会议列表（带关联需求）按录音时刻倒序（录音时间混着 -07:00、+00:00 两种写法，
    按真实时刻排，不按字符串），加这场会抽出、还留着的任务数（已过期、已取消的不算）。"""
    if task_service.db.query_one("SELECT 1 AS ok FROM projects WHERE id=?", (project_id,)) is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    meetings = project_meetings(task_service.db, project_id)
    counts = {
        row["meeting_id"]: row["n"]
        for row in task_service.db.query_all(
            """SELECT t.meeting_id, COUNT(*) AS n FROM tasks t
                 JOIN meetings m ON m.id = t.meeting_id
                WHERE m.project_id = ? AND t.origin = 'ai'
                  AND t.status NOT IN ('expired', 'cancelled')
                GROUP BY t.meeting_id""",
            (project_id,),
        )
    }
    for meeting in meetings:
        meeting["task_count"] = counts.get(meeting["id"], 0)
    meetings.sort(key=lambda meeting: _timestamp(meeting.get("recording_date")), reverse=True)
    return meetings
