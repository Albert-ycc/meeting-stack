"""需求层业务逻辑（260915 新增）：项目下的需求 CRUD、材料文件夹、会议关联、任务挂靠。

优先级 P0–P3 只挂在需求上；任务本身不设优先级，展示时从所属需求只读派生（见 tasks.py
`TaskService.task_summary`）。不做删除需求/删除项目——不要的需求用状态「已搁置」（D12，不镀金）。
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .db import Database, dedupe_preserve_order, escape_like_pattern, utc_now
from .materials import (
    CARDS_DIR_NAME,
    assert_no_hidden_segment,
    folder_stat,
    list_folder_files,
)
from .service import ConflictError, NotFoundError
from .tasks import OPEN_TASK_STATUSES, TaskService, resolve_requirement_and_project

REQUIREMENT_PRIORITIES = ("P0", "P1", "P2", "P3")
REQUIREMENT_STATUSES = ("active", "done", "shelved")
# 需求详情文件夹卡片的预览文件数
FOLDER_PREVIEW_LIMIT = 6
# 未完成任务在前，其余（已完成/已过期/已取消）在后，同组按创建时间倒序
_TASK_ORDER_SQL = """
    CASE WHEN t.status IN ({open_statuses}) THEN 0 ELSE 1 END,
    t.created_at DESC
"""


def _normalize_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValueError("需求标题不能为空")
    return title


def _assert_priority(priority: str) -> None:
    if priority not in REQUIREMENT_PRIORITIES:
        raise ValueError(f"优先级必须是 {'/'.join(REQUIREMENT_PRIORITIES)}")


def _assert_status(status: str) -> None:
    if status not in REQUIREMENT_STATUSES:
        raise ValueError(f"需求状态必须是 {'/'.join(REQUIREMENT_STATUSES)}")


def _requirement_row(connection: Any, requirement_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM requirements WHERE id=?", (requirement_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"需求不存在：{requirement_id}")
    return dict(row)


def _assert_project_exists(connection: Any, project_id: str) -> None:
    if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise NotFoundError(f"项目不存在：{project_id}")


def _validate_folder_paths(connection: Any, project_id: str, folder_paths: list[str]) -> None:
    """D5：材料文件夹必须是当前所属项目某个材料根目录的直接子文件夹，且存在。"""
    root_paths = {
        Path(row["path"]).resolve()
        for row in connection.execute(
            "SELECT path FROM project_material_roots WHERE project_id=?", (project_id,)
        ).fetchall()
    }
    for raw in folder_paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError(f"材料文件夹必须是绝对路径：{raw}")
        resolved = candidate.resolve(strict=False)
        assert_no_hidden_segment(resolved)
        if resolved.name == CARDS_DIR_NAME:
            raise ValueError("「声档会议记录」是声档写会议卡片的文件夹，不能当需求文件夹")
        if not resolved.is_dir():
            raise ValueError(f"材料文件夹不存在：{raw}")
        if resolved.parent not in root_paths:
            raise ValueError(f"材料文件夹必须是所属项目材料根目录的直接子文件夹：{raw}")


# ---------------------------------------------------------------- 列表 / 详情组装


def list_requirements(
    db: Database,
    *,
    project_id: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    statuses = [part.strip() for part in (status or "").split(",") if part.strip()]
    for value in statuses:
        if value not in REQUIREMENT_STATUSES:
            raise ValueError(f"未知需求状态：{value}")
    priorities = [part.strip() for part in (priority or "").split(",") if part.strip()]
    for value in priorities:
        if value not in REQUIREMENT_PRIORITIES:
            raise ValueError(f"未知优先级：{value}")

    clauses: list[str] = []
    params: list[Any] = []
    if project_id:
        clauses.append("r.project_id=?")
        params.append(project_id)
    if priorities:
        clauses.append(f"r.priority IN ({', '.join('?' for _ in priorities)})")
        params.extend(priorities)
    if q:
        clauses.append("r.title LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like_pattern(q)}%")
    # counts 不受状态筛选影响、受其余筛选影响：先按 status 之外的条件统计一遍。
    base_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    counts = {value: 0 for value in REQUIREMENT_STATUSES}
    for row in db.query_all(
        f"SELECT r.status, COUNT(*) AS n FROM requirements r {base_where} GROUP BY r.status",
        tuple(params),
    ):
        counts[row["status"]] = row["n"]

    if statuses:
        clauses.append(f"r.status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = db.query_one(f"SELECT COUNT(*) AS n FROM requirements r {where}", tuple(params))["n"]
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    rows = db.query_all(
        f"""SELECT r.*, p.name AS project_name, p.color AS project_color,
                   (SELECT COUNT(*) FROM tasks t WHERE t.requirement_id=r.id
                     AND t.status IN ({open_placeholders})) AS open_task_count,
                   (SELECT COUNT(*) FROM requirement_meetings rm
                     WHERE rm.requirement_id=r.id) AS meeting_count,
                   (SELECT MAX(m.recording_date) FROM requirement_meetings rm
                     JOIN meetings m ON m.id=rm.meeting_id
                    WHERE rm.requirement_id=r.id) AS latest_meeting_date,
                   (SELECT COUNT(*) FROM requirement_folders rf
                     WHERE rf.requirement_id=r.id) AS folder_count
              FROM requirements r JOIN projects p ON p.id=r.project_id
             {where}
             ORDER BY CASE r.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                      CASE WHEN latest_meeting_date IS NULL THEN 1 ELSE 0 END,
                      latest_meeting_date DESC,
                      r.created_at DESC
             LIMIT ? OFFSET ?""",
        (*OPEN_TASK_STATUSES, *params, limit, offset),
    )
    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts": {**counts, "all": sum(counts.values())},
    }


def _requirement_summary(db: Database, requirement: dict[str, Any]) -> dict[str, Any]:
    project = db.query_one(
        "SELECT name, color FROM projects WHERE id=?", (requirement["project_id"],)
    )
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    open_task_count = db.query_one(
        f"SELECT COUNT(*) AS n FROM tasks WHERE requirement_id=? AND status IN ({open_placeholders})",
        (requirement["id"], *OPEN_TASK_STATUSES),
    )["n"]
    meeting_stats = db.query_one(
        """SELECT COUNT(*) AS n, MAX(m.recording_date) AS latest
             FROM requirement_meetings rm JOIN meetings m ON m.id=rm.meeting_id
            WHERE rm.requirement_id=?""",
        (requirement["id"],),
    )
    folder_count = db.query_one(
        "SELECT COUNT(*) AS n FROM requirement_folders WHERE requirement_id=?",
        (requirement["id"],),
    )["n"]
    return {
        **requirement,
        "project_name": project["name"] if project else None,
        "project_color": project["color"] if project else None,
        "open_task_count": open_task_count,
        "meeting_count": meeting_stats["n"] or 0,
        "latest_meeting_date": meeting_stats["latest"],
        "folder_count": folder_count,
    }


def _folder_detail(folder_row: dict[str, Any]) -> dict[str, Any]:
    path = Path(folder_row["path"])
    stat = folder_stat(path)
    preview_files: list[dict[str, Any]] = []
    if stat["exists"]:
        preview_files = list_folder_files(path, limit=FOLDER_PREVIEW_LIMIT)["items"]
    return {
        "id": folder_row["id"],
        "name": path.name,
        "path": folder_row["path"],
        **stat,
        "preview_files": preview_files,
    }


def get_requirement(task_service: TaskService, requirement_id: str) -> dict[str, Any]:
    db = task_service.db
    row = db.query_one("SELECT * FROM requirements WHERE id=?", (requirement_id,))
    if row is None:
        raise NotFoundError(f"需求不存在：{requirement_id}")
    detail = _requirement_summary(db, row)
    folder_rows = db.query_all(
        "SELECT * FROM requirement_folders WHERE requirement_id=? ORDER BY created_at",
        (requirement_id,),
    )
    detail["folders"] = [_folder_detail(folder_row) for folder_row in folder_rows]
    detail["meetings"] = db.query_all(
        """SELECT m.id, m.title, m.recording_date, m.duration_ms, m.canonical_dir
             FROM requirement_meetings rm JOIN meetings m ON m.id=rm.meeting_id
            WHERE rm.requirement_id=?
            ORDER BY m.recording_date DESC""",
        (requirement_id,),
    )
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    task_rows = db.query_all(
        f"""SELECT t.* FROM tasks t WHERE t.requirement_id=?
             ORDER BY {_TASK_ORDER_SQL.format(open_statuses=open_placeholders)}""",
        (requirement_id, *OPEN_TASK_STATUSES),
    )
    detail["tasks"] = [task_service.task_summary(row) for row in task_rows]
    return detail


# ---------------------------------------------------------------- CRUD


def create_requirement(
    task_service: TaskService,
    *,
    project_id: str,
    title: str,
    priority: str,
    folder_paths: list[str] | None = None,
) -> dict[str, Any]:
    db = task_service.db
    title = _normalize_title(title)
    _assert_priority(priority)
    folder_paths = folder_paths or []
    requirement_id = f"requirement-{uuid.uuid4().hex}"
    now = utc_now()
    with db.transaction() as connection:
        _assert_project_exists(connection, project_id)
        if folder_paths:
            _validate_folder_paths(connection, project_id, folder_paths)
        try:
            connection.execute(
                """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (requirement_id, project_id, title, priority, now, now),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError("该项目下已有同名需求") from error
        for raw in folder_paths:
            connection.execute(
                "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, ?)",
                (requirement_id, str(Path(raw).resolve(strict=False)), now),
            )
    return get_requirement(task_service, requirement_id)


def update_requirement(
    task_service: TaskService,
    requirement_id: str,
    *,
    title: str | None = None,
    project_id: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    folder_paths: list[str] | None = None,
    folder_paths_given: bool = False,
) -> dict[str, Any]:
    db = task_service.db
    if priority is not None:
        _assert_priority(priority)
    if status is not None:
        _assert_status(status)
    now = utc_now()
    with db.transaction() as connection:
        requirement = _requirement_row(connection, requirement_id)
        project_changed = project_id is not None and project_id != requirement["project_id"]
        if project_changed:
            _assert_project_exists(connection, project_id)
            # D6：换了所属项目后，旧项目根目录下的文件夹不属于新项目，必须同批重选。
            if not folder_paths_given:
                raise ValueError("更换所属项目后要重新选择材料文件夹")
        target_project_id = project_id if project_changed else requirement["project_id"]
        target_folder_paths = folder_paths if folder_paths_given else None
        if target_folder_paths:
            _validate_folder_paths(connection, target_project_id, target_folder_paths)

        changes: list[str] = []
        values: list[Any] = []
        if title is not None:
            normalized_title = _normalize_title(title)
            if normalized_title != requirement["title"]:
                changes.append("title=?")
                values.append(normalized_title)
        if project_changed:
            changes.append("project_id=?")
            values.append(project_id)
        if priority is not None and priority != requirement["priority"]:
            changes.append("priority=?")
            values.append(priority)
        if status is not None and status != requirement["status"]:
            changes.append("status=?")
            values.append(status)
        if changes:
            changes.append("updated_at=?")
            values.append(now)
            values.append(requirement_id)
            try:
                connection.execute(
                    f"UPDATE requirements SET {', '.join(changes)} WHERE id=?", tuple(values)
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError("该项目下已有同名需求") from error

        if folder_paths_given:
            connection.execute(
                "DELETE FROM requirement_folders WHERE requirement_id=?", (requirement_id,)
            )
            for raw in target_folder_paths or []:
                connection.execute(
                    "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, ?)",
                    (requirement_id, str(Path(raw).resolve(strict=False)), now),
                )

        if project_changed:
            # D7：需求换项目，名下任务的项目在同一事务里跟着改。
            connection.execute(
                "UPDATE tasks SET project_id=?, updated_at=? WHERE requirement_id=?",
                (project_id, now, requirement_id),
            )
    return get_requirement(task_service, requirement_id)


def remove_folder(task_service: TaskService, requirement_id: str, folder_id: int) -> dict[str, Any]:
    db = task_service.db
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)
        removed = connection.execute(
            "DELETE FROM requirement_folders WHERE id=? AND requirement_id=?",
            (folder_id, requirement_id),
        ).rowcount
        if not removed:
            raise NotFoundError("材料文件夹不存在")
    return get_requirement(task_service, requirement_id)


def folder_files(
    db: Database, requirement_id: str, folder_id: int, *, limit: int = 2000, offset: int = 0
) -> dict[str, Any]:
    folder = db.query_one(
        "SELECT * FROM requirement_folders WHERE id=? AND requirement_id=?",
        (folder_id, requirement_id),
    )
    if folder is None:
        raise NotFoundError("材料文件夹不存在")
    payload = list_folder_files(Path(folder["path"]), limit=limit, offset=offset)
    return {"folder_id": folder_id, "path": folder["path"], **payload}


# ---------------------------------------------------------------- 会议关联


def _link_diff(
    connection: Any,
    *,
    existing: set[tuple[str, str]],
    target: list[tuple[str, str]],
) -> tuple[int, int]:
    """会议和需求的关联按差异增删：没变的那条保留原来的关联时间，不整组删了重插。"""
    wanted = set(target)
    removed = existing - wanted
    for requirement_id, meeting_id in removed:
        connection.execute(
            "DELETE FROM requirement_meetings WHERE requirement_id=? AND meeting_id=?",
            (requirement_id, meeting_id),
        )
    now = utc_now()
    added = 0
    for requirement_id, meeting_id in target:
        if (requirement_id, meeting_id) in existing:
            continue
        connection.execute(
            """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
               VALUES (?, ?, ?)""",
            (requirement_id, meeting_id, now),
        )
        added += 1
    return added, len(removed)


def sync_meeting_requirements(
    connection: Any, meeting_id: str, requirement_ids: list[str]
) -> tuple[int, int]:
    """会议页改「关联的需求」：调用方已校验需求存在并开好事务。"""
    existing = {
        (row["requirement_id"], meeting_id)
        for row in connection.execute(
            "SELECT requirement_id FROM requirement_meetings WHERE meeting_id=?", (meeting_id,)
        ).fetchall()
    }
    return _link_diff(
        connection,
        existing=existing,
        target=[(requirement_id, meeting_id) for requirement_id in requirement_ids],
    )


def set_meetings(task_service: TaskService, requirement_id: str, meeting_ids: list[str]) -> dict[str, Any]:
    db = task_service.db
    meeting_ids = dedupe_preserve_order(meeting_ids)
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)
        for meeting_id in meeting_ids:
            if connection.execute(
                "SELECT 1 FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone() is None:
                raise NotFoundError(f"会议不存在：{meeting_id}")
        existing = {
            (requirement_id, row["meeting_id"])
            for row in connection.execute(
                "SELECT meeting_id FROM requirement_meetings WHERE requirement_id=?",
                (requirement_id,),
            ).fetchall()
        }
        _link_diff(
            connection,
            existing=existing,
            target=[(requirement_id, meeting_id) for meeting_id in meeting_ids],
        )
    return get_requirement(task_service, requirement_id)


def add_meeting(task_service: TaskService, requirement_id: str, meeting_id: str) -> dict[str, Any]:
    """关系图面板里［＋ 关联一场会］/［＋ 关联需求］：只加这一条，已经关联过就什么都不做。"""
    db = task_service.db
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)
        if connection.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone() is None:
            raise NotFoundError(f"会议不存在：{meeting_id}")
        connection.execute(
            """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
               VALUES (?, ?, ?)""",
            (requirement_id, meeting_id, utc_now()),
        )
    return get_requirement(task_service, requirement_id)


def remove_meeting(task_service: TaskService, requirement_id: str, meeting_id: str) -> dict[str, Any]:
    db = task_service.db
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)
        connection.execute(
            "DELETE FROM requirement_meetings WHERE requirement_id=? AND meeting_id=?",
            (requirement_id, meeting_id),
        )
    return get_requirement(task_service, requirement_id)


# ---------------------------------------------------------------- 任务挂靠


def attach_tasks(task_service: TaskService, requirement_id: str, task_ids: list[str]) -> dict[str, Any]:
    """把已有任务挂到本需求（D7/D8）：任务项目跟着需求项目改，留痕写 task_events。

    D24：先去重，再一次性校验全部 task_ids 存在，任一不存在整批 404、不写入任何一条
    （校验与写入分两个阶段，不再边查边改）。
    """
    db = task_service.db
    task_ids = dedupe_preserve_order(task_ids)
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)  # 只为确认需求存在，404 交给这里判
        tasks: dict[str, dict[str, Any]] = {}
        for task_id in task_ids:
            task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task_row is None:
                raise NotFoundError(f"任务不存在：{task_id}")
            tasks[task_id] = dict(task_row)
        for task_id in task_ids:
            task = tasks[task_id]
            resolved_requirement_id, resolved_project_id, event_body = resolve_requirement_and_project(
                connection,
                task,
                requirement_id_given=True,
                requirement_id=requirement_id,
                project_id_given=False,
                project_id=None,
            )
            if resolved_requirement_id == task.get("requirement_id"):
                continue
            connection.execute(
                "UPDATE tasks SET requirement_id=?, project_id=?, updated_at=? WHERE id=?",
                (resolved_requirement_id, resolved_project_id, utc_now(), task_id),
            )
            if event_body:
                connection.execute(
                    "INSERT INTO task_events(task_id, kind, body, created_at) VALUES (?, 'requirement_changed', ?, ?)",
                    (task_id, event_body, utc_now()),
                )
    return get_requirement(task_service, requirement_id)


# ---------------------------------------------------------------- 项目详情页会议表格


def project_meetings(db: Database, project_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        """SELECT id, title, recording_date, duration_ms, canonical_dir
             FROM meetings WHERE project_id=?
            ORDER BY COALESCE(recording_date, created_at) DESC""",
        (project_id,),
    )
    requirement_rows = db.query_all(
        """SELECT rm.meeting_id, r.id, r.title, r.priority, r.status, r.project_id
             FROM requirement_meetings rm JOIN requirements r ON r.id=rm.requirement_id
             JOIN meetings m ON m.id=rm.meeting_id
            WHERE m.project_id=?""",
        (project_id,),
    )
    by_meeting: dict[str, list[dict[str, Any]]] = {}
    for row in requirement_rows:
        by_meeting.setdefault(row["meeting_id"], []).append(
            {
                "id": row["id"],
                "title": row["title"],
                "priority": row["priority"],
                "status": row["status"],
                "project_id": row["project_id"],
            }
        )
    for row in rows:
        row["requirements"] = by_meeting.get(row["id"], [])
    return rows
