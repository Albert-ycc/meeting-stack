"""待办（项目页与待办改版 261001，R07）：按截止分组的待办、挂需求的推荐与可选范围、待确认按会议的审核卡。

- 待办页签只收已确认、进行中的任务，按截止分五组：逾期、今天、本周（明天到本周日）、之后、未定截止。「今天」
  按北京日期（用户 261001 拍板）。筛选沿用任务池的那一套（list_tasks），计数和列表同一套判断。
- 挂需求：推荐项和可选范围都只在任务的范围里——任务所属项目；任务没归项目时是同一场会的候选和已关联这场会的
  需求。待确认的任务（确认时挂）能挂候选，已确认的只挂进行中需求（R07-14）。推荐顺序：AI 抽取时配好的候选
  （用户 261001 拍板）→ 已关联这场会的需求 → 同一场会抽出的候选。
- 审核卡：每场会一张，列这场会抽出的需求候选和 AI 任务（已过期的不列）。有待确认任务或待认领候选的是「待处理」，
  按会议时间由近到远；都处理完的整张置灰沉到底部，只留最近 7 天处理过的（用户 261001 拍板）。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .project_seats import MEETING_TIME_SQL
from .requirement_candidates import CANDIDATE_STATUSES, candidate_items, public_item
from .requirement_pool import project_order
from .requirements import REQUIREMENT_PRIORITIES
from .service import ConflictError, NotFoundError
from .task_due import beijing_today
from .tasks import TaskService, _validate_date_only

GROUPS = (
    ("overdue", "逾期"),
    ("today", "今天"),
    ("week", "本周"),
    ("later", "之后"),
    ("undated", "未定截止"),
)
TODO_STATUSES = ("confirmed", "in_progress")
# 待办一次全取出来分组：未完成的任务是几十条的量级
TODO_LIMIT = 1000
# 待确认的任务（确认时挂需求）才能挂候选
DRAFT_STATUSES = ("pending_confirm", "expired")
# 处理完的审核卡留多久
DONE_CARD_DAYS = 7
_PRIORITY_RANK = {priority: rank for rank, priority in enumerate(REQUIREMENT_PRIORITIES)}
logger = logging.getLogger("meeting_workbench.todo")


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def group_of(due: str | None, today: date) -> str:
    if not due:
        return "undated"
    try:
        day = date.fromisoformat(due)
    except ValueError:
        return "undated"
    if due != day.isoformat():
        # fromisoformat 也认 20261001、2026-W40-4，只收 YYYY-MM-DD（组内按原串排序）
        return "undated"
    if day < today:
        return "overdue"
    if day == today:
        return "today"
    if day <= today + timedelta(days=6 - today.weekday()):
        return "week"
    return "later"


def _todo_order(task: dict[str, Any]) -> tuple:
    """组内按截止由早到晚，再按来源会议时间由近到远；没有来源会议的排在同截止的最后，彼此按建的先后倒序。"""
    meeting_at = _timestamp(task.get("meeting_recording_date"))
    return (
        task.get("due_date") or "",
        meeting_at is None,
        -(meeting_at or 0.0),
        -(_timestamp(task.get("created_at")) or 0.0),
        task["id"],
    )


def list_todo(
    task_service: TaskService,
    *,
    project_id: str | None = None,
    requirement_id: str | None = None,
    assignee: str | None = None,
    meeting_date_from: str | None = None,
    meeting_date_to: str | None = None,
    q: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = task_service.list_tasks(
        status=",".join(TODO_STATUSES),
        project_id=project_id,
        requirement_id=requirement_id,
        assignee=assignee,
        meeting_date_from=meeting_date_from,
        meeting_date_to=meeting_date_to,
        q=q,
        limit=TODO_LIMIT,
    )
    today = beijing_today(now)
    with task_service.db.autocommit() as connection:
        _order, direction = project_order(connection)
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _label in GROUPS}
    for task in payload["items"]:
        grouped[group_of(task.get("due_date"), today)].append(task)
    return {
        "today": today.isoformat(),
        "week_end": (today + timedelta(days=6 - today.weekday())).isoformat(),
        "total": payload["total"],
        "groups": [
            {
                "key": key,
                "label": label,
                "count": len(grouped[key]),
                "items": sorted(grouped[key], key=_todo_order),
            }
            for key, label in GROUPS
        ],
        # 别的页签和「我的方向」条上的数字，口径同任务池（各页签按 project_counts 自己加）
        "counts": payload["counts"],
        "project_counts": payload["project_counts"],
        # 「我的方向」条的项目：已排座次的在前（seat 是名次），其后按最近会议排
        "projects": direction,
    }


# ---------------------------------------------------------------- 挂到需求（R07-8、R07-14、R06-9）


def _requirement_option(row: Any) -> dict[str, Any]:
    return {
        "kind": "requirement",
        "id": row["id"],
        "title": row["title"],
        "priority": row["priority"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "meeting_id": None,
    }


def _candidate_option(row: Any) -> dict[str, Any]:
    return {
        "kind": "candidate",
        "id": row["id"],
        "title": row["title"],
        "priority": None,
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "meeting_id": row["meeting_id"],
    }


def scope_options(connection: Any, task: dict[str, Any]) -> list[dict[str, Any]]:
    """任务能挂的需求和候选（不含推荐顺序）：需求按 P0→P3、同级按名字；候选在后，按抽出的先后倒序。"""
    project_id = task.get("project_id")
    meeting_id = task.get("meeting_id")
    with_candidates = task["status"] in DRAFT_STATUSES or project_id is None
    if project_id:
        requirements = connection.execute(
            """SELECT r.id, r.title, r.priority, r.project_id, p.name AS project_name
                 FROM requirements r JOIN projects p ON p.id = r.project_id
                WHERE r.project_id = ? AND r.status = 'active'""",
            (project_id,),
        ).fetchall()
        candidates = connection.execute(
            """SELECT c.id, c.title, c.meeting_id, c.created_at, m.project_id,
                      p.name AS project_name
                 FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
                 LEFT JOIN projects p ON p.id = m.project_id
                WHERE c.status = 'pending' AND m.project_id = ?""",
            (project_id,),
        ).fetchall()
    elif meeting_id:
        requirements = connection.execute(
            """SELECT r.id, r.title, r.priority, r.project_id, p.name AS project_name
                 FROM requirements r JOIN projects p ON p.id = r.project_id
                 JOIN requirement_meetings rm ON rm.requirement_id = r.id
                WHERE rm.meeting_id = ? AND r.status = 'active'""",
            (meeting_id,),
        ).fetchall()
        candidates = connection.execute(
            """SELECT c.id, c.title, c.meeting_id, c.created_at, m.project_id,
                      p.name AS project_name
                 FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
                 LEFT JOIN projects p ON p.id = m.project_id
                WHERE c.status = 'pending' AND c.meeting_id = ?""",
            (meeting_id,),
        ).fetchall()
    else:
        return []
    options = [
        _requirement_option(row)
        for row in sorted(
            requirements,
            key=lambda row: (_PRIORITY_RANK.get(row["priority"], 9), row["title"], row["id"]),
        )
    ]
    if with_candidates:
        options += [
            _candidate_option(row)
            for row in sorted(
                candidates, key=lambda row: (row["created_at"], row["id"]), reverse=True
            )
        ]
    return options


def _current_requirement(connection: Any, requirement_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT r.id, r.title, r.priority, r.project_id, p.name AS project_name
             FROM requirements r JOIN projects p ON p.id = r.project_id WHERE r.id = ?""",
        (requirement_id,),
    ).fetchone()
    return _requirement_option(row) if row else None


def recommend(
    connection: Any, task: dict[str, Any], options: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """推荐项，第一条是默认选中的；没有就只显示「不挂」。

    任务现在挂着的排第一：确认前用户手动挂过、或候选被合并后任务已随它挂上目标需求，默认就是它，不拿别的
    推荐去覆盖（R07 异常与边界「合并的改为推荐合并后的目标需求」）。现在挂着的需求不在可选范围里（比如已搁置）
    也照列。AI 抽取时配好、没人动过的候选标 paired，其余标 current。其后是已关联这场会的需求、同场会的候选。"""
    meeting_id = task.get("meeting_id")
    linked = (
        {
            row["requirement_id"]
            for row in connection.execute(
                "SELECT requirement_id FROM requirement_meetings WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchall()
        }
        if meeting_id
        else set()
    )
    picked: list[dict[str, Any]] = []

    def pick(option: dict[str, Any], reason: str) -> None:
        if all(item["id"] != option["id"] for item in picked):
            picked.append({**option, "reason": reason})

    if task.get("requirement_id"):
        current = next(
            (option for option in options if option["id"] == task["requirement_id"]), None
        ) or _current_requirement(connection, task["requirement_id"])
        if current is not None:
            pick(current, "current")
    for option in options:
        if option["kind"] == "candidate" and option["id"] == task.get("candidate_id"):
            touched = connection.execute(
                """SELECT 1 FROM task_events
                    WHERE task_id = ? AND kind = 'requirement_changed' LIMIT 1""",
                (task["id"],),
            ).fetchone()
            pick(option, "current" if touched else "paired")
    for option in options:
        if option["kind"] == "requirement" and option["id"] in linked:
            pick(option, "linked")
    for option in options:
        if option["kind"] == "candidate" and meeting_id and option["meeting_id"] == meeting_id:
            pick(option, "same_meeting")
    return picked


def _task_row(connection: Any, task_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"任务不存在：{task_id}")
    return dict(row)


def requirement_options(task_service: TaskService, task_id: str, q: str | None = None) -> dict:
    """挂到需求的选择器：推荐项（默认选第一条）、可选范围（按名字搜），以及任务现在挂的。"""
    needle = (q or "").strip().casefold()
    with task_service.db.autocommit() as connection:
        task = _task_row(connection, task_id)
        options = scope_options(connection, task)
        recommended = recommend(connection, task, options)
    return {
        "task_id": task_id,
        "can_link_candidates": task["status"] in DRAFT_STATUSES or not task.get("project_id"),
        "recommended": recommended,
        "default": recommended[0] if recommended else None,
        "options": [option for option in options if needle in option["title"].casefold()],
        "current": (
            {"kind": "requirement", "id": task["requirement_id"]}
            if task.get("requirement_id")
            else {"kind": "candidate", "id": task["candidate_id"]}
            if task.get("candidate_id")
            else None
        ),
    }


def _link_arguments(option: dict[str, Any] | None) -> dict[str, Any]:
    """推荐项 → 确认接口的参数；没有推荐就不动任务现在的挂接。"""
    if option is None:
        return {}
    if option["kind"] == "requirement":
        return {"requirement_id": option["id"], "requirement_id_given": True}
    return {"candidate_id": option["id"], "candidate_id_given": True}


# ---------------------------------------------------------------- 待确认按会议审核（R07-9～11、R07-16）


def _card_meeting_ids(connection: Any, cutoff: str) -> tuple[set[str], set[str]]:
    """(有待处理的会, 最近处理过的会)"""
    open_ids = {
        row["meeting_id"]
        for row in connection.execute(
            """SELECT meeting_id FROM tasks
                WHERE status = 'pending_confirm' AND meeting_id IS NOT NULL
               UNION
               SELECT meeting_id FROM requirement_candidates WHERE status = 'pending'"""
        ).fetchall()
    }
    recent_ids = {
        row["meeting_id"]
        for row in connection.execute(
            """SELECT t.meeting_id FROM task_events e JOIN tasks t ON t.id = e.task_id
                WHERE t.origin = 'ai' AND t.meeting_id IS NOT NULL
                  AND e.kind IN ('confirmed', 'rejected')
                  AND julianday(e.created_at) >= julianday(?)
               UNION
               SELECT meeting_id FROM requirement_candidates
                WHERE status <> 'pending' AND julianday(updated_at) >= julianday(?)""",
            (cutoff, cutoff),
        ).fetchall()
    }
    return open_ids, recent_ids


def review_cards(
    task_service: TaskService,
    *,
    project_id: str | None = None,
    meeting_date_from: str | None = None,
    meeting_date_to: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """待确认页签的审核卡。筛选只认会议这一层的（所属项目、会议日期，口径同任务池）。"""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=DONE_CARD_DAYS)).isoformat()
    cards: list[dict[str, Any]] = []
    with task_service.db.autocommit() as connection:
        open_ids, recent_ids = _card_meeting_ids(connection, cutoff)
        meeting_ids = sorted(open_ids | recent_ids)
        if not meeting_ids:
            return {"cards": [], "pending_task_count": 0, "pending_candidate_count": 0}
        clauses = [f"m.id IN ({', '.join('?' for _ in meeting_ids)})"]
        params: list[Any] = list(meeting_ids)
        # 所属项目可多选（逗号分隔），none 是没归项目的会，口径同任务池
        project_keys = [part.strip() for part in (project_id or "").split(",") if part.strip()]
        if project_keys:
            named = [key for key in project_keys if key != "none"]
            parts = ["m.project_id IS NULL"] if "none" in project_keys else []
            if named:
                parts.append(f"m.project_id IN ({', '.join('?' for _ in named)})")
                params.extend(named)
            clauses.append(f"({' OR '.join(parts)})")
        if meeting_date_from is not None:
            _validate_date_only(meeting_date_from)
            clauses.append("m.recording_date >= ?")
            params.append(meeting_date_from)
        if meeting_date_to is not None:
            _validate_date_only(meeting_date_to)
            clauses.append("m.recording_date <= ?")
            params.append(meeting_date_to)
        meetings = connection.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.duration_ms, m.project_id,
                       p.name AS project_name, p.color AS project_color,
                       julianday({MEETING_TIME_SQL}) AS jd
                  FROM meetings m LEFT JOIN projects p ON p.id = m.project_id
                 WHERE {" AND ".join(clauses)}""",
            tuple(params),
        ).fetchall()
        for meeting in meetings:
            task_rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM tasks
                        WHERE meeting_id = ? AND origin = 'ai' AND status <> 'expired'
                        ORDER BY created_at, id""",
                    (meeting["id"],),
                ).fetchall()
            ]
            candidate_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM requirement_candidates WHERE meeting_id = ?", (meeting["id"],)
                ).fetchall()
            ]
            candidates = sorted(
                candidate_items(
                    connection, statuses=CANDIDATE_STATUSES, candidate_ids=candidate_ids
                ),
                key=lambda item: (item["created_at"], item["id"]),
            )
            requirement_titles = (
                {
                    row["id"]: row["title"]
                    for row in connection.execute(
                        f"""SELECT id, title FROM requirements
                         WHERE id IN ({", ".join("?" for _ in candidates)})""",
                        tuple(item["requirement_id"] for item in candidates),
                    ).fetchall()
                }
                if candidates
                else {}
            )
            recommendations = {
                row["id"]: recommend(connection, row, scope_options(connection, row))
                for row in task_rows
                if row["status"] == "pending_confirm"
            }
            pending_tasks = len(recommendations)
            pending_candidates = sum(1 for item in candidates if item["status"] == "pending")
            if not task_rows and not candidates:
                continue
            cards.append(
                {
                    "meeting": {key: meeting[key] for key in meeting.keys() if key != "jd"},
                    "tasks": [
                        {
                            **task_service.task_summary(row),
                            "recommended": recommendations.get(row["id"], []),
                        }
                        for row in task_rows
                    ],
                    "candidates": [
                        {
                            **public_item(item),
                            "requirement_title": requirement_titles.get(item["requirement_id"]),
                        }
                        for item in candidates
                    ],
                    "pending_task_count": pending_tasks,
                    "pending_candidate_count": pending_candidates,
                    "done": pending_tasks == 0 and pending_candidates == 0,
                    "_jd": meeting["jd"] or 0.0,
                }
            )
    cards.sort(key=lambda card: (card["done"], -card["_jd"], card["meeting"]["id"]))
    return {
        "cards": [{key: value for key, value in card.items() if key != "_jd"} for card in cards],
        "pending_task_count": sum(card["pending_task_count"] for card in cards),
        "pending_candidate_count": sum(card["pending_candidate_count"] for card in cards),
    }


def confirm_all(task_service: TaskService, meeting_id: str) -> dict[str, Any]:
    """审核卡上的「全部确认」（R07-10）：这场会剩下的待确认任务一并确认，每条挂自己的默认推荐。
    候选不动；撤销用 undo-review（确认留痕写在最后一条，和逐条确认一样能撤）。"""
    with task_service.db.autocommit() as connection:
        if (
            connection.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            is None
        ):
            raise NotFoundError(f"会议不存在：{meeting_id}")
        draft_ids = [
            row["id"]
            for row in connection.execute(
                """SELECT id FROM tasks WHERE meeting_id = ? AND status = 'pending_confirm'
                    ORDER BY created_at, id""",
                (meeting_id,),
            ).fetchall()
        ]
    confirmed: list[str] = []
    failed: list[dict[str, Any]] = []
    linked: dict[str, dict[str, Any] | None] = {}
    chosen: dict[str, dict[str, Any] | None] = {}

    def link_for(connection: Any, task: dict[str, Any]) -> dict[str, Any]:
        """在确认的同一个事务里定挂哪条：已经挂着的不动，没挂的挂默认推荐。"""
        option = next(iter(recommend(connection, task, scope_options(connection, task))), None)
        chosen[task["id"]] = option
        if task.get("requirement_id") or task.get("candidate_id"):
            return {}
        return _link_arguments(option)

    for task_id in draft_ids:
        try:
            # 只从待确认起步：这期间被别处驳回、确认过的跳过，不算进确认、也不改它的挂接
            if task_service._confirm(task_id, only_pending=True, link_for=link_for):
                confirmed.append(task_id)
                linked[task_id] = chosen.get(task_id)
        except (ConflictError, NotFoundError, ValueError) as error:
            # 一条失败不牵连其余（比如推荐的候选刚被认领），原因回给前端
            failed.append({"task_id": task_id, "error": str(error)})
        except Exception:
            # 没想到的错也只算这一条失败：每条各自一个事务，前面的已经确认了，整个接口报错前端就不知道
            # 哪些确认上了
            logger.exception("全部确认时这条没确认上 meeting=%s task=%s", meeting_id, task_id)
            failed.append({"task_id": task_id, "error": "这条没确认上，稍后单独确认"})
    return {"confirmed": confirmed, "failed": failed, "linked": linked}
