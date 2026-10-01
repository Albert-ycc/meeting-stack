"""需求层业务逻辑（260915 新增）：项目下的需求 CRUD、材料文件夹、会议关联、任务挂靠。

优先级 P0–P3 只挂在需求上；任务本身不设优先级，展示时从所属需求只读派生（见 tasks.py
`TaskService.task_summary`）。不做删除需求/删除项目——不要的需求用状态「已搁置」（D12，不镀金）。

v17（需求池改版 260930）：需求多了说明和来源。来源＝提出它的会议、会上原话和时间锚（origin），
另有从候选合并进来的原话（merged），都在 requirement_sources。
"""

from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import Database, dedupe_preserve_order, escape_like_pattern, utc_now
from .materials import (
    CARDS_DIR_NAME,
    assert_no_hidden_segment,
    folder_stat,
    list_folder_files,
    volume_state,
)
from .project_seats import MEETING_TIME_SQL, seat_ranks
from .service import ConflictError, NotFoundError
from .tasks import OPEN_TASK_STATUSES, TaskService, resolve_requirement_and_project

REQUIREMENT_PRIORITIES = ("P0", "P1", "P2", "P3")
REQUIREMENT_STATUSES = ("active", "done", "shelved")
# 需求详情文件夹卡片的预览文件数
FOLDER_PREVIEW_LIMIT = 6
# 需求名最多 200 字（和接口一致）；说明是一两句话，最多 70 字；会上原话在逐字稿里一次可能选中好几句，
# 最多 1000 字。
TITLE_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 70
QUOTE_MAX_CHARS = 1000
# 未完成任务在前，其余（已完成/已过期/已取消）在后，同组按创建时间倒序
_TASK_ORDER_SQL = """
    CASE WHEN t.status IN ({open_statuses}) THEN 0 ELSE 1 END,
    t.created_at DESC
"""
# 一场会画波形用的录音：归档里的优先（和录音档案列表同一个取法）。
AUDIO_ARTIFACT_SQL = """(SELECT a.id FROM artifacts a WHERE a.meeting_id = m.id AND a.kind = 'audio'
    ORDER BY CASE a.source_root WHEN 'archive' THEN 0 ELSE 1 END LIMIT 1)"""


class RequirementTitleConflict(ConflictError):
    """同一项目下已有同名需求。existing 是那条需求（id、title、status），认领候选撞名时前端据此提示改名或合并。"""

    def __init__(self, existing: dict[str, Any] | None):
        super().__init__("该项目下已有同名需求")
        self.existing = existing


def clean_title(title: str | None) -> str:
    """需求名去掉零宽字符这类看不见的格式字符（从飞书、微信复制常带），再去首尾空白：
    不然「赠药横跳拦截」后面多一个零宽空格就能绕过同项目不重名，只粘一个零宽空格能建出看不见名字的需求。"""
    return "".join(char for char in (title or "") if unicodedata.category(char) != "Cf").strip()


def _normalize_title(title: str) -> str:
    title = clean_title(title)
    if not title:
        raise ValueError("需求标题不能为空")
    return title


def normalize_summary(summary: str | None) -> str:
    summary = (summary or "").strip()
    if len(summary) > SUMMARY_MAX_CHARS:
        raise ValueError(f"说明最多 {SUMMARY_MAX_CHARS} 字")
    return summary


def title_conflict(
    task_service: TaskService, *, project_id: str, title: str, exclude_id: str | None = None
) -> dict[str, Any] | None:
    """新增、修改页边填边查重（R04-9）：同项目里和它重名的那条需求，没有返回 None。
    名字的比较和保存时的唯一约束一致：去掉零宽字符和首尾空白，ASCII 不分大小写。"""
    title = clean_title(title)
    if not title or not project_id:
        return None
    row = task_service.db.query_one(
        """SELECT id, title, status FROM requirements
            WHERE project_id=? AND lower(trim(title))=lower(trim(?)) AND id IS NOT ?""",
        (project_id, title, exclude_id),
    )
    return dict(row) if row else None


def _title_conflict(connection: Any, project_id: str, title: str) -> RequirementTitleConflict:
    row = connection.execute(
        """SELECT id, title, status FROM requirements
            WHERE project_id=? AND lower(trim(title))=lower(trim(?))""",
        (project_id, title),
    ).fetchone()
    return RequirementTitleConflict(dict(row) if row else None)


def _assert_priority(priority: str) -> None:
    if priority not in REQUIREMENT_PRIORITIES:
        raise ValueError(f"优先级必须是 {'/'.join(REQUIREMENT_PRIORITIES)}")


def _assert_status(status: str) -> None:
    if status not in REQUIREMENT_STATUSES:
        raise ValueError(f"需求状态必须是 {'/'.join(REQUIREMENT_STATUSES)}")


def _requirement_row(connection: Any, requirement_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,)).fetchone()
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


# ---------------------------------------------------------------- 来源（v17）


def load_sources(connection: Any, *, owner: str, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """一批需求（owner="requirement"）或候选（owner="candidate"）的全部来源，按会议时间先后、
    同一场会按时间锚排。每条带会议名、录音时间、时长和画波形用的录音 id。"""
    column = {"requirement": "requirement_id", "candidate": "candidate_id"}[owner]
    if not ids:
        return {}
    rows = connection.execute(
        f"""SELECT s.id, s.{column} AS owner_id, s.kind, s.meeting_id, s.quote, s.anchor_ms,
                   s.via_candidate_title, m.title AS meeting_title, m.recording_date,
                   m.duration_ms, {AUDIO_ARTIFACT_SQL} AS audio_artifact_id
              FROM requirement_sources s JOIN meetings m ON m.id = s.meeting_id
             WHERE s.{column} IN ({", ".join("?" for _ in ids)})
             ORDER BY julianday({MEETING_TIME_SQL}), COALESCE(s.anchor_ms, 0), s.id""",
        tuple(ids),
    ).fetchall()
    sources: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        sources.setdefault(item.pop("owner_id"), []).append(item)
    return sources


def origin_of(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """提出它的那句；没有来源（新建时留空）时为 None。"""
    return next((source for source in sources if source["kind"] == "origin"), None)


def anchor_in_recording(anchor_ms: Any, duration_ms: int | None) -> bool:
    """时间锚是不小于 0 的整数毫秒；知道录音时长时不超过时长（超出的锚点画不出标记、也跳不过去）。"""
    if isinstance(anchor_ms, bool) or not isinstance(anchor_ms, int) or anchor_ms < 0:
        return False
    return not duration_ms or anchor_ms <= duration_ms


def clean_source(connection: Any, source: dict[str, Any]) -> dict[str, Any]:
    """来源入参：会议必填且存在；原话和时间锚可空（只选了会、没挑原话）。"""
    meeting_id = str(source.get("meeting_id") or "").strip()
    if not meeting_id:
        raise ValueError("来源要先选会议")
    meeting = connection.execute(
        "SELECT duration_ms FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        raise NotFoundError(f"会议不存在：{meeting_id}")
    quote = str(source.get("quote") or "").strip()
    if len(quote) > QUOTE_MAX_CHARS:
        raise ValueError(f"原话最多 {QUOTE_MAX_CHARS} 字")
    anchor_ms = source.get("anchor_ms")
    if anchor_ms is not None and not anchor_in_recording(anchor_ms, meeting["duration_ms"]):
        raise ValueError("时间锚要落在这场会的录音里")
    return {"meeting_id": meeting_id, "quote": quote, "anchor_ms": anchor_ms}


def set_origin_source(connection: Any, requirement_id: str, source: dict[str, Any] | None) -> None:
    """在调用方的事务里换掉需求「提出它的那句」，source 为 None 时清空。选定的会议同时加进
    关联会议（R04-4）；换掉或清空来源不动关联会议，合并进来的原话也不动。"""
    clean = clean_source(connection, source) if source is not None else None
    connection.execute(
        "DELETE FROM requirement_sources WHERE requirement_id=? AND kind='origin'",
        (requirement_id,),
    )
    if clean is None:
        return
    now = utc_now()
    connection.execute(
        """INSERT INTO requirement_sources
               (requirement_id, kind, meeting_id, quote, anchor_ms, created_at)
           VALUES (?, 'origin', ?, ?, ?, ?)""",
        (requirement_id, clean["meeting_id"], clean["quote"], clean["anchor_ms"], now),
    )
    connection.execute(
        """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
           VALUES (?, ?, ?)""",
        (requirement_id, clean["meeting_id"], now),
    )


def follow_up_count(meeting_count: int) -> int:
    """「之后又跟进了几场」＝关联会议数减 1（R02-4）。来源会议一定在关联会议里：选定来源时同时关联，
    移出关联时它的原话跟着去掉（db 里的 requirement_sources_follow_unlink 触发器）。"""
    return max(meeting_count - 1, 0)


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
    # 录音时间混着两种时区写法，最晚的一场按儒略日比（SQLite 的 MAX 聚合里裸列取自最大那一行）。
    meeting_stats = db.query_one(
        f"""SELECT COUNT(*) AS n, MAX(julianday({MEETING_TIME_SQL})) AS jd,
                   {MEETING_TIME_SQL} AS latest
             FROM requirement_meetings rm JOIN meetings m ON m.id=rm.meeting_id
            WHERE rm.requirement_id=?""",
        (requirement["id"],),
    )
    folder_count = db.query_one(
        "SELECT COUNT(*) AS n FROM requirement_folders WHERE requirement_id=?",
        (requirement["id"],),
    )["n"]
    with db.autocommit() as connection:
        seats = seat_ranks(connection)
    return {
        **requirement,
        "project_name": project["name"] if project else None,
        "project_color": project["color"] if project else None,
        "project_seat": seats.get(requirement["project_id"]),
        "open_task_count": open_task_count,
        "meeting_count": meeting_stats["n"] or 0,
        "latest_meeting_date": meeting_stats["latest"],
        "folder_count": folder_count,
    }


def _folder_detail(
    folder_row: dict[str, Any], *, db: Database | None = None, project_id: str | None = None
) -> dict[str, Any]:
    path = Path(folder_row["path"])
    stat = folder_stat(path)
    preview_files: list[dict[str, Any]] | None = None
    # 4d：前 6 个先试查库（根目录在线、文件名收完时），行里带 file_id，小签用；不行再照旧读盘
    if db is not None and project_id:
        from .material_graph import folder_files_from_index

        with db.autocommit() as connection:
            indexed = folder_files_from_index(
                connection,
                project_id,
                str(folder_row["path"]),
                limit=FOLDER_PREVIEW_LIMIT,
                offset=0,
            )
        if indexed is not None:
            preview_files = indexed["items"]
    if preview_files is None:
        preview_files = []
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
    detail["folders"] = [
        _folder_detail(folder_row, db=db, project_id=row.get("project_id"))
        for folder_row in folder_rows
    ]
    detail["meetings"] = db.query_all(
        """SELECT m.id, m.title, m.recording_date, m.duration_ms, m.canonical_dir
             FROM requirement_meetings rm JOIN meetings m ON m.id=rm.meeting_id
            WHERE rm.requirement_id=?
            ORDER BY m.recording_date DESC""",
        (requirement_id,),
    )
    with db.autocommit() as connection:
        sources = load_sources(connection, owner="requirement", ids=[requirement_id]).get(
            requirement_id, []
        )
    # R05：合并进来的原话和提出它的那句一起列，按会议时间先后；头部波形取提出它的那场会。
    detail["sources"] = sources
    detail["source"] = origin_of(sources)
    detail["follow_up_count"] = follow_up_count(len(detail["meetings"]))
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    task_rows = db.query_all(
        f"""SELECT t.* FROM tasks t WHERE t.requirement_id=?
             ORDER BY {_TASK_ORDER_SQL.format(open_statuses=open_placeholders)}""",
        (requirement_id, *OPEN_TASK_STATUSES),
    )
    detail["tasks"] = [task_service.task_summary(row) for row in task_rows]
    return detail


# ---------------------------------------------------------------- CRUD


def insert_requirement(
    connection: Any,
    *,
    project_id: str,
    title: str,
    priority: str,
    folder_paths: list[str] | None = None,
    summary: str | None = None,
    source: dict[str, Any] | None = None,
) -> str:
    """在调用方的事务里建需求，返回需求 id（建成需求的提示要和改归属、关联会放进同一个事务）。

    source 是提出它的会议、原话和时间锚（逐字稿选句建、需求池新建时选了来源会议），可空。"""
    title = _normalize_title(title)
    _assert_priority(priority)
    summary = normalize_summary(summary)
    folder_paths = folder_paths or []
    requirement_id = f"requirement-{uuid.uuid4().hex}"
    now = utc_now()
    _assert_project_exists(connection, project_id)
    if folder_paths:
        _validate_folder_paths(connection, project_id, folder_paths)
    try:
        connection.execute(
            """INSERT INTO requirements
                   (id, project_id, title, summary, priority, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (requirement_id, project_id, title, summary, priority, now, now),
        )
    except sqlite3.IntegrityError as error:
        raise _title_conflict(connection, project_id, title) from error
    for raw in folder_paths:
        connection.execute(
            "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, ?)",
            (requirement_id, str(Path(raw).resolve(strict=False)), now),
        )
    if source is not None:
        set_origin_source(connection, requirement_id, source)
    return requirement_id


def create_requirement(
    task_service: TaskService,
    *,
    project_id: str,
    title: str,
    priority: str,
    folder_paths: list[str] | None = None,
    summary: str | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with task_service.db.transaction() as connection:
        requirement_id = insert_requirement(
            connection,
            project_id=project_id,
            title=title,
            priority=priority,
            folder_paths=folder_paths,
            summary=summary,
            source=source,
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
    summary: str | None = None,
    source: dict[str, Any] | None = None,
    source_given: bool = False,
) -> dict[str, Any]:
    """source_given 时换掉提出它的那句（source 为 None 是清空）；状态只在进行中、已完成、
    已搁置之间切换，不能改回待认领（R04-5，待认领的是候选，不在需求表里）。"""
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
        if summary is not None:
            normalized_summary = normalize_summary(summary)
            if normalized_summary != requirement["summary"]:
                changes.append("summary=?")
                values.append(normalized_summary)
        if project_changed:
            changes.append("project_id=?")
            values.append(project_id)
        if priority is not None and priority != requirement["priority"]:
            changes.append("priority=?")
            values.append(priority)
        if status is not None and status != requirement["status"]:
            changes.append("status=?")
            values.append(status)
        if changes or source_given:
            changes.append("updated_at=?")
            values.append(now)
            values.append(requirement_id)
            try:
                connection.execute(
                    f"UPDATE requirements SET {', '.join(changes)} WHERE id=?", tuple(values)
                )
            except sqlite3.IntegrityError as error:
                raise _title_conflict(
                    connection, target_project_id, title or requirement["title"]
                ) from error

        if source_given:
            set_origin_source(connection, requirement_id, source)

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
    db: Database,
    requirement_id: str,
    folder_id: int,
    *,
    limit: int = 2000,
    offset: int = 0,
    state_of: Callable[[str], str] = volume_state,
) -> dict[str, Any]:
    from .material_graph import folder_files_from_index

    folder = db.query_one(
        """SELECT f.*, r.project_id FROM requirement_folders f
             JOIN requirements r ON r.id = f.requirement_id
            WHERE f.id=? AND f.requirement_id=?""",
        (folder_id, requirement_id),
    )
    if folder is None:
        raise NotFoundError("材料文件夹不存在")
    # 3g：根目录在线、文件名索引扫完时查库，否则照旧读盘
    with db.autocommit() as connection:
        payload = folder_files_from_index(
            connection,
            str(folder["project_id"]),
            str(folder["path"]),
            limit=limit,
            offset=offset,
            state_of=state_of,
        )
    if payload is None:
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


def set_meetings(
    task_service: TaskService, requirement_id: str, meeting_ids: list[str]
) -> dict[str, Any]:
    db = task_service.db
    meeting_ids = dedupe_preserve_order(meeting_ids)
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)
        for meeting_id in meeting_ids:
            if (
                connection.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone()
                is None
            ):
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


def link_meeting(connection: Any, requirement_id: str, meeting_id: str) -> bool:
    """在调用方的事务里关联一场会；已经关联过返回 False。"""
    _requirement_row(connection, requirement_id)
    if connection.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone() is None:
        raise NotFoundError(f"会议不存在：{meeting_id}")
    return bool(
        connection.execute(
            """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
               VALUES (?, ?, ?)""",
            (requirement_id, meeting_id, utc_now()),
        ).rowcount
    )


def add_meeting(task_service: TaskService, requirement_id: str, meeting_id: str) -> dict[str, Any]:
    """关系图面板里［＋ 关联一场会］/［＋ 关联需求］：只加这一条，已经关联过就什么都不做。"""
    with task_service.db.transaction() as connection:
        link_meeting(connection, requirement_id, meeting_id)
    return get_requirement(task_service, requirement_id)


def remove_meeting(
    task_service: TaskService, requirement_id: str, meeting_id: str
) -> dict[str, Any]:
    db = task_service.db
    with db.transaction() as connection:
        _requirement_row(connection, requirement_id)
        connection.execute(
            "DELETE FROM requirement_meetings WHERE requirement_id=? AND meeting_id=?",
            (requirement_id, meeting_id),
        )
    return get_requirement(task_service, requirement_id)


# ---------------------------------------------------------------- 任务挂靠


def attach_tasks(
    task_service: TaskService, requirement_id: str, task_ids: list[str]
) -> dict[str, Any]:
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
            resolved_requirement_id, resolved_project_id, event_body = (
                resolve_requirement_and_project(
                    connection,
                    task,
                    requirement_id_given=True,
                    requirement_id=requirement_id,
                    project_id_given=False,
                    project_id=None,
                )
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
