"""需求并需求（261009，D4–D8）：把同一项目里重复的一条（「这条」）并进另一条（主需求）。

和候选合并同一套口径（requirement_candidates._hand_over / merge_candidate）：原话、关联会议、材料文件夹、
待办全部带过去，原话一律记成 merged、via_candidate_title 记这条的名字；会议、文件夹主需求上已有的不重复。
主需求的名字不变；说明、出处（origin 那句）主需求有就留、没有就接过这条的；等级取较高；只要有一边是
进行中，合并后就是进行中。这条搬空后删掉，requirement_merges 里留一行：以前的链接据此跟到主需求，
10 分钟内能按快照撤销。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .project_seats import MEETING_TIME_SQL
from .requirements import (
    MERGE_UNDO_MINUTES,
    REQUIREMENT_PRIORITIES,
    _requirement_row,
    drop_merged_meeting,
    get_requirement,
    settle_reopen,
    merged_into,
)
from .service import ConflictError, NotFoundError
from .tasks import OPEN_TASK_STATUSES, TaskService

# 弹窗里列的先后：进行中 → 已搁置 → 已完成（D4）
_STATUS_ORDER = {"active": 0, "shelved": 1, "done": 2}
_PRIORITY_RANK = {priority: rank for rank, priority in enumerate(REQUIREMENT_PRIORITIES)}


def _moving(connection: Any, requirement: dict[str, Any]) -> dict[str, Any]:
    """这条会带过去的东西（弹窗底部的预览）：待办、会、原话、文件夹各几个，和它的等级。"""
    placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    requirement_id = requirement["id"]
    row = connection.execute(
        f"""SELECT
               (SELECT COUNT(*) FROM tasks WHERE requirement_id=?
                  AND status IN ({placeholders})) AS open_task_count,
               (SELECT COUNT(*) FROM tasks WHERE requirement_id=?) AS task_count,
               (SELECT COUNT(*) FROM requirement_meetings WHERE requirement_id=?) AS meeting_count,
               (SELECT COUNT(*) FROM requirement_sources WHERE requirement_id=?) AS source_count,
               (SELECT COUNT(*) FROM requirement_folders WHERE requirement_id=?) AS folder_count""",
        (requirement_id, *OPEN_TASK_STATUSES, *([requirement_id] * 4)),
    ).fetchone()
    return {**dict(row), "priority": requirement["priority"]}


def merge_targets(db: Database, requirement_id: str, q: str | None = None) -> dict[str, Any]:
    """「把这条并入」弹窗（D4）：同一项目里除了它自己的需求，三种状态都列；按进行中 → 已搁置 → 已完成、
    再按等级、再按最近一场关联会议由近到远排。q 按名称筛，不分大小写。"""
    placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    with db.autocommit() as connection:
        requirement = _requirement_row(connection, requirement_id)
        moving = _moving(connection, requirement)
        rows = connection.execute(
            f"""SELECT r.id, r.title, r.status, r.priority,
                       (SELECT COUNT(*) FROM tasks t WHERE t.requirement_id = r.id
                          AND t.status IN ({placeholders})) AS open_task_count,
                       (SELECT COUNT(*) FROM requirement_meetings rm
                         WHERE rm.requirement_id = r.id) AS meeting_count,
                       (SELECT MAX(julianday({MEETING_TIME_SQL})) FROM requirement_meetings rm
                          JOIN meetings m ON m.id = rm.meeting_id
                         WHERE rm.requirement_id = r.id) AS latest_jd
                  FROM requirements r
                 WHERE r.project_id = ? AND r.id <> ?""",
            (*OPEN_TASK_STATUSES, requirement["project_id"], requirement_id),
        ).fetchall()
    needle = (q or "").strip().casefold()
    items = [dict(row) for row in rows if needle in row["title"].casefold()]
    items.sort(
        key=lambda item: (
            _STATUS_ORDER[item["status"]],
            _PRIORITY_RANK[item["priority"]],
            item["latest_jd"] is None,
            -(item["latest_jd"] or 0.0),
            item["title"],
            item["id"],
        )
    )
    for item in items:
        del item["latest_jd"]
    return {"moving": moving, "items": items}


def _rows(connection: Any, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _confirm_undo_pointing_at(connection: Any, requirement_id: str) -> list[tuple[str, dict]]:
    """确认前挂的是这条需求的任务（tasks.confirm_undo，撤销确认时照它挂回）：(任务 id, 解析出的 JSON)。"""
    found = []
    for row in connection.execute(
        "SELECT id, confirm_undo FROM tasks WHERE confirm_undo LIKE ?", (f"%{requirement_id}%",)
    ).fetchall():
        try:
            before = json.loads(row["confirm_undo"])
        except (TypeError, ValueError):
            continue
        if isinstance(before, dict) and before.get("requirement_id") == requirement_id:
            found.append((row["id"], before))
    return found


def _repoint_confirm_undo(connection: Any, task_ids: list[str], old: str, new: str) -> None:
    for task_id, before in _confirm_undo_pointing_at(connection, old):
        if task_id in task_ids:
            before["requirement_id"] = new
            connection.execute(
                "UPDATE tasks SET confirm_undo=? WHERE id=?",
                (json.dumps(before, ensure_ascii=False), task_id),
            )


def merge_requirement(
    task_service: TaskService,
    requirement_id: str,
    into_requirement_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把这条并进同项目的主需求（D5、D6），返回主需求详情，外加 merged_from（这条的 id、名字、撤销截止时间）。

    引用需求 id 的地方全部改指主需求：待办、关联会议、材料文件夹、来源、候选（认领进这条的记成已合并）、
    候选的相近需求、决议、需求名的决定、并进这条的合并记录、待办确认前的挂接。改指前的样子记进
    requirement_merges.undo，撤销时照它还原。会议页「建成需求」「不算新需求」事件里记的需求 id 不改：撤销
    建成需求时遇到合并会先拒绝（name_actions.undo_name_as_requirement），撤销不算新需求时按合并链读到现在那条。"""
    if requirement_id == into_requirement_id:
        raise ValueError("不能并给自己")
    moment = now or datetime.now(UTC)
    stamp = moment.isoformat()
    with task_service.db.transaction() as connection:
        this = _requirement_row(connection, requirement_id)
        found = connection.execute(
            "SELECT * FROM requirements WHERE id=?", (into_requirement_id,)
        ).fetchone()
        if found is None:
            into = merged_into(connection, into_requirement_id)
            if into is not None:
                raise NotFoundError(f"要并入的需求已并入「{into['title']}」")
            raise NotFoundError("要并入的需求不存在")
        main = dict(found)
        if main["project_id"] != this["project_id"]:
            raise ValueError("只能并入同一项目里的需求")

        meetings = _rows(
            connection,
            "SELECT meeting_id, created_at FROM requirement_meetings WHERE requirement_id=?",
            (requirement_id,),
        )
        folders = _rows(
            connection,
            "SELECT id, path, created_at FROM requirement_folders WHERE requirement_id=? ORDER BY id",
            (requirement_id,),
        )
        sources = _rows(
            connection,
            "SELECT * FROM requirement_sources WHERE requirement_id=? ORDER BY id",
            (requirement_id,),
        )
        tasks = _rows(
            connection,
            "SELECT id, project_id FROM tasks WHERE requirement_id=? ORDER BY id",
            (requirement_id,),
        )
        candidates = _rows(
            connection,
            """SELECT id, status FROM requirement_candidates
                WHERE requirement_id=? AND status IN ('claimed', 'merged') ORDER BY id""",
            (requirement_id,),
        )
        similar = [
            row["id"]
            for row in _rows(
                connection,
                "SELECT id FROM requirement_candidates WHERE similar_requirement_id=? ORDER BY id",
                (requirement_id,),
            )
        ]
        decisions = [
            row["id"]
            for row in _rows(
                connection,
                "SELECT id FROM decisions WHERE requirement_id=? ORDER BY id",
                (requirement_id,),
            )
        ]
        name_decisions = _rows(
            connection,
            """SELECT project_id, name_key FROM requirement_name_decisions
                WHERE requirement_id=? ORDER BY project_id, name_key""",
            (requirement_id,),
        )
        chained = [
            row["requirement_id"]
            for row in _rows(
                connection,
                """SELECT requirement_id FROM requirement_merges
                    WHERE into_requirement_id=? ORDER BY requirement_id""",
                (requirement_id,),
            )
        ]
        confirm_undo = [task_id for task_id, _ in _confirm_undo_pointing_at(connection, this["id"])]

        # 1. 来源先搬：删掉这条时级联删它的关联会议，requirement_sources_follow_unlink 会连带删掉
        #    同一需求、同一场会的来源。主需求没有出处时接过这条的 origin，其余一律记成合并进来的原话。
        main_has_origin = (
            connection.execute(
                "SELECT 1 FROM requirement_sources WHERE requirement_id=? AND kind='origin'",
                (into_requirement_id,),
            ).fetchone()
            is not None
        )
        took_origin = None
        for source in sources:
            if source["kind"] == "origin" and not main_has_origin:
                connection.execute(
                    "UPDATE requirement_sources SET requirement_id=? WHERE id=?",
                    (into_requirement_id, source["id"]),
                )
                took_origin = source["id"]
            else:
                connection.execute(
                    """UPDATE requirement_sources
                          SET requirement_id=?, kind='merged', via_candidate_title=?
                        WHERE id=?""",
                    (into_requirement_id, this["title"], source["id"]),
                )

        # 2. 关联会议、材料文件夹并过去，主需求上已有的不重复
        meetings_added = []
        for meeting in meetings:
            if connection.execute(
                """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
                   VALUES (?, ?, ?)""",
                (into_requirement_id, meeting["meeting_id"], stamp),
            ).rowcount:
                meetings_added.append(meeting["meeting_id"])
        folders_added = []
        for folder in folders:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO requirement_folders(requirement_id, path, created_at)
                   VALUES (?, ?, ?)""",
                (into_requirement_id, folder["path"], stamp),
            )
            if cursor.rowcount:
                folders_added.append(cursor.lastrowid)

        # 3. 待办改挂，待办动态写一行
        connection.execute(
            "UPDATE tasks SET requirement_id=?, project_id=?, updated_at=? WHERE requirement_id=?",
            (into_requirement_id, main["project_id"], stamp, requirement_id),
        )
        connection.executemany(
            """INSERT INTO task_events(task_id, kind, body, created_at)
               VALUES (?, 'requirement_changed', ?, ?)""",
            [
                (
                    task["id"],
                    f"需求「{this['title']}」并入「{main['title']}」，跟着挂到「{main['title']}」",
                    stamp,
                )
                for task in tasks
            ],
        )

        # 4. 别处指着这条的改指主需求
        connection.execute(
            """UPDATE requirement_candidates SET requirement_id=?, status='merged'
                WHERE requirement_id=? AND status IN ('claimed', 'merged')""",
            (into_requirement_id, requirement_id),
        )
        connection.execute(
            "UPDATE requirement_candidates SET similar_requirement_id=? WHERE similar_requirement_id=?",
            (into_requirement_id, requirement_id),
        )
        connection.execute(
            "UPDATE decisions SET requirement_id=? WHERE requirement_id=?",
            (into_requirement_id, requirement_id),
        )
        connection.execute(
            "UPDATE requirement_name_decisions SET requirement_id=? WHERE requirement_id=?",
            (into_requirement_id, requirement_id),
        )
        connection.execute(
            "UPDATE requirement_merges SET into_requirement_id=? WHERE into_requirement_id=?",
            (into_requirement_id, requirement_id),
        )
        _repoint_confirm_undo(connection, confirm_undo, requirement_id, into_requirement_id)

        # 5. 主需求：等级取较高，说明空着就接过这条的，有一边是进行中就是进行中
        after = {
            "priority": min(main["priority"], this["priority"], key=_PRIORITY_RANK.__getitem__),
            "summary": main["summary"] or this["summary"],
            "status": main["status"],
            "status_changed_at": main["status_changed_at"],
        }
        if this["status"] == "active" and main["status"] != "active":
            after["status"], after["status_changed_at"] = "active", stamp
        connection.execute(
            """UPDATE requirements
                  SET priority=?, summary=?, status=?, status_changed_at=?, updated_at=?
                WHERE id=?""",
            (
                after["priority"],
                after["summary"],
                after["status"],
                after["status_changed_at"],
                stamp,
                into_requirement_id,
            ),
        )

        # 6. 记下合并（撤销用的快照），删掉这条
        undo = {
            "into": into_requirement_id,
            "requirement": this,
            "meetings": meetings,
            "folders": folders,
            "sources": sources,
            "took_origin": took_origin,
            "meetings_added": meetings_added,
            "folders_added": folders_added,
            "tasks": tasks,
            "candidates": candidates,
            "similar_candidates": similar,
            "decisions": decisions,
            "name_decisions": name_decisions,
            "merges": chained,
            "confirm_undo_tasks": confirm_undo,
            "main_before": {key: main[key] for key in after},
            "main_after": after,
        }
        if after["status"] != main["status"]:
            # D6 把主需求重新打开了：撤销时按 settle_reopen 改回（并进来的都撤完才改回，和顺序无关）
            undo.update(
                reopened_from=main["status"],
                reopened_at=stamp,
                previous_status_changed_at=main["status_changed_at"],
            )
        connection.execute(
            """INSERT INTO requirement_merges
                   (requirement_id, title, project_id, into_requirement_id, merged_at, undo)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                requirement_id,
                this["title"],
                this["project_id"],
                into_requirement_id,
                stamp,
                json.dumps(undo, ensure_ascii=False),
            ),
        )
        connection.execute("DELETE FROM requirements WHERE id=?", (requirement_id,))
    detail = get_requirement(task_service, into_requirement_id, now=moment)
    detail["merged_from"] = {
        "id": requirement_id,
        "title": this["title"],
        "undo_until": (moment + timedelta(minutes=MERGE_UNDO_MINUTES)).isoformat(),
    }
    return detail


def _blocked_by_later_merge(connection: Any, requirement_id: str, main_id: str) -> str:
    """撤销不了的原因：主需求之后又被并进了别处（先撤销那一次），或者已经不在了。"""
    main_merge = connection.execute(
        "SELECT title FROM requirement_merges WHERE requirement_id=?", (main_id,)
    ).fetchone()
    final = merged_into(connection, requirement_id)
    if main_merge is not None and final is not None:
        return f"「{main_merge['title']}」之后又并入了「{final['title']}」，先撤销那次合并"
    return "并入的那条需求已经不在了，不能撤销"


def unmerge_requirement(
    task_service: TaskService, requirement_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """撤销需求并需求（D7）：10 分钟内，这条按快照原样回来（原来的 id、名字、等级、状态、说明、出处、关联会议、
    材料文件夹、原话），合并时改指主需求的东西指回来，主需求的等级、说明、状态退回合并前。

    这 10 分钟里别处改过的不动：已经改挂到别的需求的待办、被撤销合并拿回候选的原话、主需求上被手动改过的
    等级 / 说明 / 状态，都保持现状。主需求之后又被并进别处时不能撤销（先撤销那一次）。"""
    moment = now or datetime.now(UTC)
    stamp = moment.isoformat()
    with task_service.db.transaction() as connection:
        merge = connection.execute(
            "SELECT * FROM requirement_merges WHERE requirement_id=?", (requirement_id,)
        ).fetchone()
        if merge is None or not merge["undo"]:
            raise ConflictError("这条需求没有被合并，不用撤销")
        if moment - datetime.fromisoformat(merge["merged_at"]) > timedelta(
            minutes=MERGE_UNDO_MINUTES
        ):
            raise ConflictError(f"合并超过 {MERGE_UNDO_MINUTES} 分钟，不能撤销了")
        record = json.loads(merge["undo"])
        main_id = record["into"]
        main_row = connection.execute(
            "SELECT * FROM requirements WHERE id=?", (main_id,)
        ).fetchone()
        if main_row is None or merge["into_requirement_id"] != main_id:
            raise ConflictError(_blocked_by_later_merge(connection, requirement_id, main_id))
        main = dict(main_row)
        this = record["requirement"]
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (this["project_id"],)
            ).fetchone()
            is None
        ):
            raise ConflictError("这条需求原来的项目已经不在了，不能撤销")

        try:
            connection.execute(
                f"""INSERT INTO requirements ({", ".join(this)})
                    VALUES ({", ".join("?" for _ in this)})""",
                tuple(this.values()),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"项目里已经有同名的需求「{this['title']}」，不能撤销") from error

        # 关联会议、文件夹照快照放回（会议这期间被删了的不放）
        for meeting in record["meetings"]:
            connection.execute(
                """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
                   SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM meetings WHERE id=?)""",
                (
                    requirement_id,
                    meeting["meeting_id"],
                    meeting["created_at"],
                    meeting["meeting_id"],
                ),
            )
        for folder in record["folders"]:
            connection.execute(
                """INSERT OR IGNORE INTO requirement_folders(id, requirement_id, path, created_at)
                   VALUES (?, ?, ?, ?)""",
                (folder["id"], requirement_id, folder["path"], folder["created_at"]),
            )
        # 原话：还在主需求上的改回原样；主需求上被连带删掉的（移出了那场会、换了出处）按快照补回；
        # 已经不在主需求上、但行还在的（撤销候选合并拿回了候选）不动
        for source in record["sources"]:
            if connection.execute(
                """UPDATE requirement_sources SET requirement_id=?, kind=?, via_candidate_title=?
                    WHERE id=? AND requirement_id=?""",
                (
                    requirement_id,
                    source["kind"],
                    source["via_candidate_title"],
                    source["id"],
                    main_id,
                ),
            ).rowcount:
                continue
            gone = (
                connection.execute(
                    "SELECT 1 FROM requirement_sources WHERE id=?", (source["id"],)
                ).fetchone()
                is None
            )
            linked = connection.execute(
                "SELECT 1 FROM requirement_meetings WHERE requirement_id=? AND meeting_id=?",
                (requirement_id, source["meeting_id"]),
            ).fetchone()
            if gone and linked is not None:
                connection.execute(
                    f"""INSERT INTO requirement_sources ({", ".join(source)})
                        VALUES ({", ".join("?" for _ in source)})""",
                    tuple(source.values()),
                )
        # 合并时给主需求新加的关联会议拆掉——那场会还有别的原话留在主需求里（之后又并进来一条）的留着、
        # 记账交给那一条；新加的文件夹拿掉
        for meeting_id in record["meetings_added"]:
            drop_merged_meeting(connection, main_id, meeting_id, skip_merge=requirement_id)
        for folder_id in record["folders_added"]:
            connection.execute(
                "DELETE FROM requirement_folders WHERE id=? AND requirement_id=?",
                (folder_id, main_id),
            )
        # 待办：还挂在主需求上的挂回来，这期间改挂到别处的不动
        returned = set()
        for task in record["tasks"]:
            if connection.execute(
                """UPDATE tasks SET requirement_id=?, project_id=?, updated_at=?
                    WHERE id=? AND requirement_id=?""",
                (requirement_id, this["project_id"], stamp, task["id"], main_id),
            ).rowcount:
                returned.add(task["id"])
                connection.execute(
                    """INSERT INTO task_events(task_id, kind, body, created_at)
                       VALUES (?, 'requirement_changed', ?, ?)""",
                    (task["id"], f"撤销合并：回到需求「{this['title']}」", stamp),
                )
        # 主需求最近一次改状态时一起关掉了其中几条：记下它们现在回到了这条，撤销那次改状态时照样恢复
        # （改状态的撤销只恢复还挂在原处的待办，撤销合并退回的不算挪走）
        if returned and main["status_undo"]:
            status_record = json.loads(main["status_undo"])
            for task in status_record["tasks"]:
                if task["id"] in returned:
                    task["requirement_id"] = requirement_id
            connection.execute(
                "UPDATE requirements SET status_undo=? WHERE id=?",
                (json.dumps(status_record, ensure_ascii=False), main_id),
            )
        for candidate in record["candidates"]:
            connection.execute(
                """UPDATE requirement_candidates SET requirement_id=?, status=?
                    WHERE id=? AND requirement_id=? AND status='merged'""",
                (requirement_id, candidate["status"], candidate["id"], main_id),
            )
        for candidate_id in record["similar_candidates"]:
            connection.execute(
                """UPDATE requirement_candidates SET similar_requirement_id=?
                    WHERE id=? AND similar_requirement_id=?""",
                (requirement_id, candidate_id, main_id),
            )
        for decision_id in record["decisions"]:
            connection.execute(
                "UPDATE decisions SET requirement_id=? WHERE id=? AND requirement_id=?",
                (requirement_id, decision_id, main_id),
            )
        for item in record["name_decisions"]:
            connection.execute(
                """UPDATE requirement_name_decisions SET requirement_id=?
                    WHERE project_id=? AND name_key=? AND requirement_id=?""",
                (requirement_id, item["project_id"], item["name_key"], main_id),
            )
        for merged_id in record["merges"]:
            connection.execute(
                """UPDATE requirement_merges SET into_requirement_id=?
                    WHERE requirement_id=? AND into_requirement_id=?""",
                (requirement_id, merged_id, main_id),
            )
        _repoint_confirm_undo(connection, record["confirm_undo_tasks"], main_id, requirement_id)

        # 主需求：还是合并后那个样子的等级、说明退回合并前，这期间手动改过的留着；这次（或转交来的）重新打开
        # 按 settle_reopen 处理
        before, after = record["main_before"], record["main_after"]
        restored = {key: before[key] for key in ("priority", "summary") if main[key] == after[key]}
        connection.execute(
            f"""UPDATE requirements SET {"".join(f"{key}=?, " for key in restored)}updated_at=?
                 WHERE id=?""",
            (*restored.values(), stamp, main_id),
        )
        settle_reopen(connection, main_id, record, stamp, skip_merge=requirement_id)
        connection.execute(
            "DELETE FROM requirement_merges WHERE requirement_id=?", (requirement_id,)
        )
    return get_requirement(task_service, requirement_id, now=moment)
