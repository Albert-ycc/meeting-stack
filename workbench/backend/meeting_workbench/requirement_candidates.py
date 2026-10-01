"""需求候选（需求池改版 260930，R01）：AI 从会议纪要里抽出来、还没认领的需求。

候选认领后才建成正式需求（进行中，优先级默认 P2）；合并＝把它的会和原话并进同项目一条进行中或已搁置的
需求，候选随即消失；丢掉的 30 天内能撤销，同一项目下同名候选以后不再提示（name_key 留着）。
挂在候选上的任务：认领后随需求走、合并后挂到目标需求、丢掉后回到未挂需求（撤销丢掉不会再挂回来）。
已经被手动挂到别的需求上的任务不跟着动。

候选不存项目，跟着来源会议当前的归属走；会议没归项目时是「未归项目」，认领时必须选定项目。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, utc_now
from .project_profile import light_key
from .project_seats import MEETING_TIME_SQL, seat_ranks
from .requirements import (
    QUOTE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    clean_source,
    follow_up_count,
    get_requirement,
    insert_requirement,
    load_sources,
    origin_of,
)
from .service import ConflictError, NotFoundError
from .tasks import OPEN_TASK_STATUSES, TaskService, resolve_requirement_and_project

CANDIDATE_STATUSES = ("pending", "claimed", "merged", "dropped")
CLAIM_DEFAULT_PRIORITY = "P2"
DROP_UNDO_DAYS = 30
# 合并只能并进同项目这两种状态的需求，不能并进已完成的（R01-8）。
MERGE_TARGET_STATUSES = ("active", "shelved")
_HANDLED = {
    "claimed": "这条候选已经认领了",
    "merged": "这条候选已经合并了",
    "dropped": "这条候选已经丢掉了",
}


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    """去掉只给排序用的下划线字段。"""
    return {key: value for key, value in item.items() if not key.startswith("_")}


# ---------------------------------------------------------------- 建候选（抽取那一刀调）


def insert_candidate(
    connection: Any,
    *,
    meeting_id: str,
    title: str,
    summary: str = "",
    quote: str = "",
    anchor_ms: int | None = None,
    extraction_id: int | None = None,
    similar_requirement_id: str | None = None,
) -> str:
    """在调用方的事务里建一条待认领候选和它提出时的那句原话，返回候选 id。

    AI 写的说明和原话超长时截断，保证候选原样就能认领；相近需求不存在时当没有。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("候选标题不能为空")
    source = clean_source(
        connection,
        {
            "meeting_id": meeting_id,
            "quote": (quote or "")[:QUOTE_MAX_CHARS],
            "anchor_ms": anchor_ms,
        },
    )
    if similar_requirement_id and (
        connection.execute(
            "SELECT 1 FROM requirements WHERE id=?", (similar_requirement_id,)
        ).fetchone()
        is None
    ):
        similar_requirement_id = None
    candidate_id = f"candidate-{uuid.uuid4().hex}"
    now = utc_now()
    connection.execute(
        """INSERT INTO requirement_candidates
               (id, meeting_id, extraction_id, title, name_key, summary, similar_requirement_id,
                status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (
            candidate_id,
            source["meeting_id"],
            extraction_id,
            title,
            light_key(title),
            (summary or "").strip()[:SUMMARY_MAX_CHARS],
            similar_requirement_id or None,
            now,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO requirement_sources
               (candidate_id, kind, meeting_id, quote, anchor_ms, created_at)
           VALUES (?, 'origin', ?, ?, ?, ?)""",
        (candidate_id, source["meeting_id"], source["quote"], source["anchor_ms"], now),
    )
    return candidate_id


# ---------------------------------------------------------------- 读


def candidate_items(
    connection: Any,
    *,
    statuses: tuple[str, ...],
    candidate_ids: list[str] | None = None,
    dropped_since: str | None = None,
) -> list[dict[str, Any]]:
    """候选在海报墙上的样子（kind="candidate"），字段和需求海报对齐；_latest_jd、_created_jd 只给排序用。

    默认动作：AI 判断的相近需求还在同项目、还能合并（进行中或已搁置）时是「合并」，否则「认领」。
    can_merge：所属项目下有可合并的需求才显示［合并］（未归项目的候选不能合并）。"""
    clauses = [f"c.status IN ({', '.join('?' for _ in statuses)})"]
    params: list[Any] = [*statuses]
    if candidate_ids is not None:
        if not candidate_ids:
            return []
        clauses.append(f"c.id IN ({', '.join('?' for _ in candidate_ids)})")
        params.extend(candidate_ids)
    if dropped_since is not None:
        clauses.append("julianday(c.dropped_at) >= julianday(?)")
        params.append(dropped_since)
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    rows = connection.execute(
        f"""SELECT c.*, m.project_id, p.name AS project_name, p.color AS project_color,
                   julianday(c.created_at) AS created_jd,
                   (SELECT COUNT(*) FROM tasks t
                     WHERE t.candidate_id = c.id AND t.requirement_id IS NULL
                       AND t.status IN ({open_placeholders})) AS open_task_count
              FROM requirement_candidates c
              JOIN meetings m ON m.id = c.meeting_id
              LEFT JOIN projects p ON p.id = m.project_id
             WHERE {" AND ".join(clauses)}""",
        (*OPEN_TASK_STATUSES, *params),
    ).fetchall()
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    sources = load_sources(connection, owner="candidate", ids=ids)
    latest = {
        row["candidate_id"]: row
        for row in connection.execute(
            f"""SELECT s.candidate_id, MAX(julianday({MEETING_TIME_SQL})) AS jd,
                       {MEETING_TIME_SQL} AS latest
                  FROM requirement_sources s JOIN meetings m ON m.id = s.meeting_id
                 WHERE s.candidate_id IN ({", ".join("?" for _ in ids)})
                 GROUP BY s.candidate_id""",
            tuple(ids),
        ).fetchall()
    }
    similar_ids = sorted({row["similar_requirement_id"] for row in rows} - {None})
    similar = (
        {
            row["id"]: dict(row)
            for row in connection.execute(
                f"""SELECT id, title, status, project_id FROM requirements
                     WHERE id IN ({", ".join("?" for _ in similar_ids)})""",
                tuple(similar_ids),
            ).fetchall()
        }
        if similar_ids
        else {}
    )
    mergeable_projects = {
        row["project_id"]
        for row in connection.execute(
            f"""SELECT DISTINCT project_id FROM requirements
                 WHERE status IN ({", ".join("?" for _ in MERGE_TARGET_STATUSES)})""",
            MERGE_TARGET_STATUSES,
        ).fetchall()
    }
    seats = seat_ranks(connection)
    items: list[dict[str, Any]] = []
    for row in rows:
        own_sources = sources.get(row["id"], [])
        origin = origin_of(own_sources)
        meeting_ids = {source["meeting_id"] for source in own_sources}
        project_id = row["project_id"]
        target = similar.get(row["similar_requirement_id"])
        mergeable_target = (
            target is not None
            and target["status"] in MERGE_TARGET_STATUSES
            and target["project_id"] == project_id
        )
        latest_row = latest.get(row["id"])
        items.append(
            {
                "kind": "candidate",
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "status": row["status"],
                "priority": None,
                "project_id": project_id,
                "project_name": row["project_name"],
                "project_color": row["project_color"],
                "project_seat": seats.get(project_id) if project_id else None,
                "open_task_count": row["open_task_count"],
                "meeting_count": len(meeting_ids),
                "folder_count": 0,
                "latest_meeting_date": latest_row["latest"] if latest_row else None,
                "source": origin,
                "follow_up_count": follow_up_count(meeting_ids, origin),
                "similar_requirement": (
                    {key: target[key] for key in ("id", "title", "status")}
                    if mergeable_target
                    else None
                ),
                "default_action": "merge" if mergeable_target else "claim",
                "can_merge": project_id is not None and project_id in mergeable_projects,
                "requirement_id": row["requirement_id"],
                "dropped_at": row["dropped_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "_latest_jd": latest_row["jd"] if latest_row else None,
                "_created_jd": row["created_jd"],
            }
        )
    return items


def get_candidate(db: Database, candidate_id: str) -> dict[str, Any]:
    """认领页（S02）用：海报上的字段，加上全部来源。认领、合并过的候选来源已经挂到需求上，
    requirement_id 指过去。"""
    with db.autocommit() as connection:
        items = candidate_items(
            connection, statuses=CANDIDATE_STATUSES, candidate_ids=[candidate_id]
        )
        if not items:
            raise NotFoundError(f"候选不存在：{candidate_id}")
        item = public_item(items[0])
        item["sources"] = load_sources(connection, owner="candidate", ids=[candidate_id]).get(
            candidate_id, []
        )
    return item


def list_dropped(db: Database, *, now: datetime | None = None) -> dict[str, Any]:
    """「已丢掉」：30 天内丢掉、还能撤销的候选，最近丢掉的在前。"""
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=DROP_UNDO_DAYS)).isoformat()
    with db.autocommit() as connection:
        items = candidate_items(connection, statuses=("dropped",), dropped_since=cutoff)
    items.sort(key=lambda item: (item["dropped_at"] or "", item["id"]), reverse=True)
    for item in items:
        item["restore_until"] = (
            datetime.fromisoformat(item["dropped_at"]) + timedelta(days=DROP_UNDO_DAYS)
        ).isoformat()
    return {
        "items": [public_item(item) for item in items],
        "total": len(items),
        "undo_days": DROP_UNDO_DAYS,
    }


def merge_targets(db: Database, candidate_id: str) -> dict[str, Any]:
    """合并弹层（S03）：候选所属项目里进行中、已搁置的需求，AI 判断的相近需求排第一、标 recommended。
    每条带提出它的那场会（没有来源时取最近一场关联会议）。不跨项目，未归项目的候选没有可选的。"""
    with db.autocommit() as connection:
        candidate = _candidate_row(connection, candidate_id)
        project_id = candidate["project_id"]
        if project_id is None:
            return {"project_id": None, "items": []}
        rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT r.id, r.title, r.status, r.priority,
                           (SELECT m.title FROM requirement_meetings rm
                              JOIN meetings m ON m.id = rm.meeting_id
                             WHERE rm.requirement_id = r.id
                             ORDER BY julianday({MEETING_TIME_SQL}) DESC LIMIT 1) AS latest_title,
                           (SELECT {MEETING_TIME_SQL} FROM requirement_meetings rm
                              JOIN meetings m ON m.id = rm.meeting_id
                             WHERE rm.requirement_id = r.id
                             ORDER BY julianday({MEETING_TIME_SQL}) DESC LIMIT 1) AS latest_date
                      FROM requirements r
                     WHERE r.project_id = ?
                       AND r.status IN ({", ".join("?" for _ in MERGE_TARGET_STATUSES)})""",
                (project_id, *MERGE_TARGET_STATUSES),
            ).fetchall()
        ]
        origins = load_sources(connection, owner="requirement", ids=[row["id"] for row in rows])
    recommended = candidate["similar_requirement_id"]
    items = []
    for row in rows:
        origin = origin_of(origins.get(row["id"], []))
        items.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "priority": row["priority"],
                "meeting_title": origin["meeting_title"] if origin else row["latest_title"],
                "recording_date": origin["recording_date"] if origin else row["latest_date"],
                "recommended": row["id"] == recommended,
            }
        )
    items.sort(
        key=lambda item: (
            not item["recommended"],
            MERGE_TARGET_STATUSES.index(item["status"]),
            item["priority"],
            item["title"],
        )
    )
    return {"project_id": project_id, "items": items}


# ---------------------------------------------------------------- 认领 / 合并 / 丢掉 / 撤销


def _candidate_row(connection: Any, candidate_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT c.*, m.project_id FROM requirement_candidates c
             JOIN meetings m ON m.id = c.meeting_id
            WHERE c.id = ?""",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"候选不存在：{candidate_id}")
    return dict(row)


def _pending_candidate(connection: Any, candidate_id: str) -> dict[str, Any]:
    candidate = _candidate_row(connection, candidate_id)
    if candidate["status"] != "pending":
        raise ConflictError(_HANDLED[candidate["status"]])
    return candidate


def _hand_over(
    connection: Any,
    candidate: dict[str, Any],
    requirement_id: str,
    *,
    merged: bool,
) -> None:
    """候选的来源、会议、任务交给需求，候选记成已认领 / 已合并。

    来源整行改挂：认领时提出它的那句仍是 origin；合并时一律变成 merged，记下合并自哪条候选。"""
    now = utc_now()
    candidate_id = candidate["id"]
    meeting_ids = [
        row["meeting_id"]
        for row in connection.execute(
            "SELECT DISTINCT meeting_id FROM requirement_sources WHERE candidate_id=?",
            (candidate_id,),
        ).fetchall()
    ]
    if merged:
        connection.execute(
            """UPDATE requirement_sources
                  SET requirement_id=?, candidate_id=NULL, kind='merged', via_candidate_title=?
                WHERE candidate_id=?""",
            (requirement_id, candidate["title"], candidate_id),
        )
    else:
        connection.execute(
            "UPDATE requirement_sources SET requirement_id=?, candidate_id=NULL WHERE candidate_id=?",
            (requirement_id, candidate_id),
        )
    for meeting_id in meeting_ids:
        connection.execute(
            """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
               VALUES (?, ?, ?)""",
            (requirement_id, meeting_id, now),
        )
    tasks = connection.execute(
        "SELECT * FROM tasks WHERE candidate_id=? AND requirement_id IS NULL", (candidate_id,)
    ).fetchall()
    for task in tasks:
        _requirement_id, project_id, event_body = resolve_requirement_and_project(
            connection,
            dict(task),
            requirement_id_given=True,
            requirement_id=requirement_id,
            project_id_given=False,
            project_id=None,
        )
        connection.execute(
            "UPDATE tasks SET requirement_id=?, project_id=?, updated_at=? WHERE id=?",
            (requirement_id, project_id, now, task["id"]),
        )
        if event_body:
            connection.execute(
                """INSERT INTO task_events(task_id, kind, body, created_at)
                   VALUES (?, 'requirement_changed', ?, ?)""",
                (task["id"], event_body, now),
            )
    connection.execute("UPDATE tasks SET candidate_id=NULL WHERE candidate_id=?", (candidate_id,))
    connection.execute(
        "UPDATE requirement_candidates SET status=?, requirement_id=?, updated_at=? WHERE id=?",
        ("merged" if merged else "claimed", requirement_id, now, candidate_id),
    )


def claim_candidate(
    task_service: TaskService,
    candidate_id: str,
    *,
    title: str,
    summary: str | None,
    project_id: str | None,
    priority: str = CLAIM_DEFAULT_PRIORITY,
    folder_paths: list[str] | None = None,
) -> dict[str, Any]:
    """认领（S02）：标题、说明、所属项目、优先级可改，建成进行中的需求，来源会议、原话、时间锚一并带入。

    同项目已有同名需求时不新建，抛 RequirementTitleConflict（带那条需求，前端提示改名或合并过去）。"""
    if not project_id:
        raise ValueError("候选还没归项目，认领前先选所属项目")
    with task_service.db.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        requirement_id = insert_requirement(
            connection,
            project_id=project_id,
            title=title,
            priority=priority,
            folder_paths=folder_paths,
            summary=summary,
        )
        _hand_over(connection, candidate, requirement_id, merged=False)
    return get_requirement(task_service, requirement_id)


def merge_candidate(
    task_service: TaskService, candidate_id: str, requirement_id: str
) -> dict[str, Any]:
    """合并（S03）：这场会关联到目标需求、原话追加到它的来源，候选消失。目标只能是候选所属项目里
    进行中或已搁置的需求。"""
    with task_service.db.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        if candidate["project_id"] is None:
            raise ValueError("候选还没归项目，不能合并；先认领并选所属项目")
        target = connection.execute(
            "SELECT id, project_id, status FROM requirements WHERE id=?", (requirement_id,)
        ).fetchone()
        if target is None:
            raise NotFoundError(f"需求不存在：{requirement_id}")
        if target["project_id"] != candidate["project_id"]:
            raise ValueError("只能合并到候选所属项目里的需求")
        if target["status"] not in MERGE_TARGET_STATUSES:
            raise ValueError("不能合并到已完成的需求")
        _hand_over(connection, candidate, requirement_id, merged=True)
        connection.execute(
            "UPDATE requirements SET updated_at=? WHERE id=?", (utc_now(), requirement_id)
        )
    return get_requirement(task_service, requirement_id)


def drop_candidate(
    db: Database, candidate_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """丢掉：候选移出墙面，挂在它上面的任务回到未挂需求。30 天内能撤销。"""
    stamp = (now or datetime.now(UTC)).isoformat()
    with db.transaction() as connection:
        _pending_candidate(connection, candidate_id)
        connection.execute(
            """UPDATE requirement_candidates SET status='dropped', dropped_at=?, updated_at=?
                WHERE id=?""",
            (stamp, stamp, candidate_id),
        )
        connection.execute(
            "UPDATE tasks SET candidate_id=NULL WHERE candidate_id=?", (candidate_id,)
        )
    return get_candidate(db, candidate_id)


def restore_candidate(
    db: Database, candidate_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """撤销丢掉：30 天内回到待认领。之前挂在它上面的任务不会再挂回来。"""
    moment = now or datetime.now(UTC)
    with db.transaction() as connection:
        candidate = _candidate_row(connection, candidate_id)
        if candidate["status"] != "dropped":
            raise ConflictError("这条候选不在已丢掉里")
        if moment - datetime.fromisoformat(candidate["dropped_at"]) > timedelta(
            days=DROP_UNDO_DAYS
        ):
            raise ConflictError(f"丢掉超过 {DROP_UNDO_DAYS} 天，不能撤销了")
        connection.execute(
            """UPDATE requirement_candidates SET status='pending', dropped_at=NULL, updated_at=?
                WHERE id=?""",
            (moment.isoformat(), candidate_id),
        )
    return get_candidate(db, candidate_id)
