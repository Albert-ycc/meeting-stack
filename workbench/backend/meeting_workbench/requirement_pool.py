"""需求池海报墙（需求池改版 260930，R02/R03）：正式需求和待认领候选拼成一面墙。

排序：项目座次 → 项目内 P0→P3（候选没有等级，排在 P3 之后）→ 最近会议时间由近到远；没有关联会议的
排在同等级最后，彼此按创建时间由近到远。没排座次的项目在已排的后面、按项目最近会议时间排，项目下没有
会议的再往后；「未归项目」（只有候选会是）永远最后。

筛选：状态页签（pending / active / done / shelved / all）、项目（多选，unassigned 是未归项目）、
优先级（多选；候选没有等级，不受它影响）、需求名称。页签计数随项目、优先级、名称变，不随所选页签变；
「我的方向」条上每个项目的数只随页签变。条数是几十条的量级，全取出来在这里筛和排，计数和列表同一套判断。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .project_seats import MEETING_TIME_SQL, project_latest_meetings, seat_ranks
from .requirement_candidates import DROP_UNDO_DAYS, candidate_items, public_item
from .requirements import REQUIREMENT_PRIORITIES, follow_up_count, load_sources, origin_of
from .tasks import OPEN_TASK_STATUSES

POOL_STATUSES = ("pending", "active", "done", "shelved")
UNASSIGNED = "unassigned"
_PRIORITY_RANK = {priority: rank for rank, priority in enumerate(REQUIREMENT_PRIORITIES)}
_UNASSIGNED_GROUP = (3, 0, 0.0, "")


def requirement_items(connection: Any) -> list[dict[str, Any]]:
    """全部正式需求在海报墙上的样子（kind="requirement"），字段和候选海报对齐。"""
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    rows = connection.execute(
        f"""SELECT r.*, p.name AS project_name, p.color AS project_color,
                   julianday(r.created_at) AS created_jd,
                   (SELECT COUNT(*) FROM tasks t WHERE t.requirement_id = r.id
                       AND t.status IN ({open_placeholders})) AS open_task_count,
                   (SELECT COUNT(*) FROM requirement_folders rf
                     WHERE rf.requirement_id = r.id) AS folder_count
              FROM requirements r JOIN projects p ON p.id = r.project_id""",
        OPEN_TASK_STATUSES,
    ).fetchall()
    if not rows:
        return []
    meetings: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        f"""SELECT rm.requirement_id, rm.meeting_id, julianday({MEETING_TIME_SQL}) AS jd,
                   {MEETING_TIME_SQL} AS at
              FROM requirement_meetings rm JOIN meetings m ON m.id = rm.meeting_id"""
    ).fetchall():
        entry = meetings.setdefault(row["requirement_id"], {"ids": set(), "jd": None, "at": None})
        entry["ids"].add(row["meeting_id"])
        if row["jd"] is not None and (entry["jd"] is None or row["jd"] > entry["jd"]):
            entry["jd"], entry["at"] = row["jd"], row["at"]
    sources = load_sources(connection, owner="requirement", ids=[row["id"] for row in rows])
    seats = seat_ranks(connection)
    items: list[dict[str, Any]] = []
    for row in rows:
        linked = meetings.get(row["id"], {"ids": set(), "jd": None, "at": None})
        origin = origin_of(sources.get(row["id"], []))
        items.append(
            {
                "kind": "requirement",
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "status": row["status"],
                "priority": row["priority"],
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "project_color": row["project_color"],
                "project_seat": seats.get(row["project_id"]),
                "open_task_count": row["open_task_count"],
                "meeting_count": len(linked["ids"]),
                "folder_count": row["folder_count"],
                "latest_meeting_date": linked["at"],
                "source": origin,
                "follow_up_count": follow_up_count(len(linked["ids"])),
                "similar_requirement": None,
                "default_action": None,
                "can_merge": False,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "_latest_jd": linked["jd"],
                "_created_jd": row["created_jd"],
            }
        )
    return items


def project_order(connection: Any) -> tuple[dict[str | None, tuple], list[dict[str, Any]]]:
    """项目分组的先后（R03-5/6），和「我的方向」条要的项目列表（同一顺序，seat 是名次、未排为 None）。"""
    projects = [
        dict(row) for row in connection.execute("SELECT id, name, color FROM projects").fetchall()
    ]
    seats = seat_ranks(connection)
    latest = project_latest_meetings(connection)

    def group(project: dict[str, Any]) -> tuple:
        if project["id"] in seats:
            return (0, seats[project["id"]], 0.0, "")
        if project["id"] in latest:
            return (1, 0, -latest[project["id"]]["jd"], project["name"])
        return (2, 0, 0.0, project["name"])

    order: dict[str | None, tuple] = {project["id"]: group(project) for project in projects}
    order[None] = _UNASSIGNED_GROUP
    direction = [
        {
            "id": project["id"],
            "name": project["name"],
            "color": project["color"],
            "seat": seats.get(project["id"]),
            "latest_meeting_date": (latest.get(project["id"]) or {}).get("date"),
        }
        for project in sorted(projects, key=lambda project: order[project["id"]])
    ]
    return order, direction


def _sort_key(item: dict[str, Any], order: dict[str | None, tuple]) -> tuple:
    latest = item["_latest_jd"]
    return (
        order.get(item["project_id"], _UNASSIGNED_GROUP),
        _PRIORITY_RANK.get(item["priority"], len(_PRIORITY_RANK)),
        latest is None,
        -(latest or 0.0),
        -(item["_created_jd"] or 0.0),
        item["id"],
    )


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def list_pool(
    db: Database,
    *,
    status: str = "active",
    project_id: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """海报墙一页。project_id、priority 都是逗号分隔的多选；project_id 里的 unassigned 是未归项目。"""
    if status != "all" and status not in POOL_STATUSES:
        raise ValueError(f"未知需求状态：{status}")
    priorities = _split(priority)
    for value in priorities:
        if value not in REQUIREMENT_PRIORITIES:
            raise ValueError(f"未知优先级：{value}")
    project_ids = set(_split(project_id))
    needle = (q or "").strip().casefold()
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=DROP_UNDO_DAYS)).isoformat()
    with db.autocommit() as connection:
        order, direction = project_order(connection)
        items = requirement_items(connection) + candidate_items(connection, statuses=("pending",))
        dropped = candidate_items(connection, statuses=("dropped",), dropped_since=cutoff)

    def project_ok(item: dict[str, Any]) -> bool:
        if not project_ids:
            return True
        if item["project_id"] is None:
            return UNASSIGNED in project_ids
        return item["project_id"] in project_ids

    def priority_ok(item: dict[str, Any]) -> bool:
        return not priorities or item["kind"] == "candidate" or item["priority"] in priorities

    def name_ok(item: dict[str, Any]) -> bool:
        return not needle or needle in item["title"].casefold()

    def in_tab(item: dict[str, Any]) -> bool:
        return status == "all" or item["status"] == status

    matched = [item for item in items if project_ok(item) and priority_ok(item) and name_ok(item)]
    counts = {value: 0 for value in POOL_STATUSES}
    for item in matched:
        counts[item["status"]] += 1
    selected = sorted(
        (item for item in matched if in_tab(item)), key=lambda item: _sort_key(item, order)
    )
    per_project = Counter(item["project_id"] for item in items if in_tab(item))
    for project in direction:
        project["count"] = per_project.get(project["id"], 0)
    return {
        "items": [public_item(item) for item in selected[offset : offset + limit]],
        "total": len(selected),
        "limit": limit,
        "offset": offset,
        "status": status,
        "counts": {**counts, "all": sum(counts.values())},
        "projects": direction,
        "unassigned_count": per_project.get(None, 0),
        "dropped_count": sum(1 for item in dropped if project_ok(item) and name_ok(item)),
    }
