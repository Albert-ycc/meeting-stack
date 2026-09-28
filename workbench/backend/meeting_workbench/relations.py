"""第四期：关联表 relations 的写（4a）。

- 系统行是 origin 不是 manual、状态是 shown、suggested 或 cleared 的行（SYSTEM_ROW_SQL，只定义这一次）。
  循环只把系统行里的 shown、suggested 改成 cleared；L1 到 L5、H2、H3、clear_missing、离开项目的
  触发器和每日清理都只动系统行，你回答过的行（rejected、confirmed、resolved）和你手动建或换过的行
  （origin manual）一概不碰。
- 循环都只用 upsert_system 写：只在原来是系统状态、真有变化、这一轮开始读输入以后没被你改过时才改。
  条件不成立时 SQLite 不做 UPDATE，也就不触发版本号触发器：你刚回答的行，正在跑的那一轮改不回去；
  没变的行不让关系图缓存失效；行 id 一直不变，撤销和深链接都靠它。
- 不能用先删后插的写法（INSERT 加 OR REPLACE）：行 id 会变、回答会丢、两组版本号触发器都会触发
  （tests/test_relations.py 有一条 grep 测试）。
- 放宽的提到和 meeting_file_mentions 互相遮盖：同一 (会议, 项目, 词干) 已经有字面行（不管什么状态）
  时不写放宽的；relations 里被驳回的放宽提到，读的时候把同一词干的字面行也藏起来（relation_read）。
- 回答和撤销（POST /api/relations/{id}/answer、/undo）：prev_json 只记这次改了的列，600 秒内按它
  改回再清空；产出的［是］和它的撤销在同一个事务里登记、删掉交付物。
- 合并项目：repoint_project 在 UPDATE meetings 之前把关联、字面提到和候选词搬到目标项目。
- 4d：相关材料栏里还没连成线的一条点［不相关］时新建 origin manual 的 rejected 行（reject_related）；
  这种行撤销、改回相关时整行删掉，回到「没说过」；vector 行改回 shown。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .material_index import MATCH_ZONES
from .relation_read import live_file, serialize
from .tasks import UNDO_WINDOW_SECONDS, _delete_deliverable, _insert_deliverable

# 系统行：循环能动的行。你回答过的、你手动建或换过的都不是。
SYSTEM_ROW_SQL = "origin != 'manual' AND status IN ('shown', 'suggested', 'cleared')"

KINDS = ("mention", "related", "produced", "affects", "later_changed", "restated")
# 每类一出来的状态；restore 回到它
FIRST_STATUS = {
    "mention": "shown",
    "related": "shown",
    "produced": "suggested",
    "affects": "suggested",
    "later_changed": "shown",
    "restated": "shown",
}
# 每类能回答什么、回答后变成什么（restore 另算：只对 rejected、resolved，回到这一类的第一个状态）
ANSWERS: dict[str, dict[str, str]] = {
    "mention": {"no": "rejected", "pick": "shown"},
    "related": {"no": "rejected"},
    "produced": {"yes": "confirmed", "no": "rejected"},
    "affects": {"updated": "resolved", "no": "rejected"},
    "later_changed": {"no": "rejected"},
    "restated": {"no": "rejected"},
}
RESTORABLE = ("rejected", "resolved")
# 每类的起点列（is_rejected 按它找同一个起点）
FROM_COLUMN = {
    "mention": "meeting_id",
    "related": "meeting_id",
    "produced": "task_id",
    "affects": "decision_id",
    "later_changed": "decision_id",
    "restated": "decision_id",
}
FILE_KINDS = ("mention", "related", "produced", "affects")
# 回答能改的列（prev_json 只记这几列里这次改了的）
_ANSWER_COLUMNS = ("status", "origin", "file_id", "content_key", "root_id", "rel_path", "deliverable_id", "decided_at")
_SCOPE_COLUMNS = ("meeting_id", "task_id", "decision_id", "to_decision_id", "content_key")
_ZONES_SQL = ", ".join(f"'{zone}'" for zone in MATCH_ZONES)

GONE = "这条关联已经不在了"
HANDLED = "这条已经处理过了"
LEFT_PROJECT = "这条关联已经不在这个项目里了"
UNDO_EXPIRED = "已超过撤销时间，请直接改回"
UNDONE = "已经撤销过了"
WRONG_ANSWER = "这类关联不能这样回答"
# 和第二期 file_mentions.pick_mention_file 同一句，同一个按钮不出两种说法
WRONG_PICK = "只能换成这个项目文件夹里同名的另一份文件"
FILE_GONE = "这份文件已经不在了"
# 4d：相关材料栏［不相关］的错误
MEETING_GONE = "会议不存在"
NOT_INDEXED = "这份文件不在索引里了"
NO_PROJECT = "这场会没归项目"
OTHER_PROJECT = "这份文件不在这个项目的资料盘里"


class RelationError(ValueError):
    """回答和撤销的错误，status 是 HTTP 状态码，文字前端原样显示。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def canonical(obj: Any) -> str:
    """evidence_json 的固定写法（键排序、紧凑分隔符），比较字符串就知道变没变。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------- 循环的写


_UPSERT_SQL = """
INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, at_ms, task_id, decision_id,
                      to_decision_id, content_key, root_id, rel_path, stem_key, file_id, quote,
                      evidence_json, score, created_at, updated_at)
VALUES (:kind, :project_id, :ident, :status, :origin, :meeting_id, :at_ms, :task_id, :decision_id,
        :to_decision_id, :content_key, :root_id, :rel_path, :stem_key, :file_id, :quote,
        :evidence_json, :score, :now, :now)
ON CONFLICT(kind, project_id, ident) DO UPDATE SET
    status = excluded.status, at_ms = excluded.at_ms, content_key = excluded.content_key,
    root_id = excluded.root_id, rel_path = excluded.rel_path, stem_key = excluded.stem_key,
    file_id = excluded.file_id, quote = excluded.quote, evidence_json = excluded.evidence_json,
    score = excluded.score, updated_at = excluded.updated_at
WHERE relations.origin != 'manual'
  AND (relations.status IN ('shown', 'suggested')
       OR (relations.status = 'cleared' AND relations.kind != 'produced'))
  AND (relations.status IS NOT excluded.status OR relations.quote IS NOT excluded.quote
       OR relations.evidence_json IS NOT excluded.evidence_json
       OR relations.file_id IS NOT excluded.file_id OR relations.at_ms IS NOT excluded.at_ms
       OR round(relations.score, 2) IS NOT round(excluded.score, 2))
  AND relations.updated_at <= :since"""


def _params(row: dict[str, Any], now: str, since: str) -> dict[str, Any]:
    evidence = row.get("evidence_json")
    if evidence is None:
        evidence = canonical(row.get("evidence") or {})
    return {
        "kind": row["kind"],
        "project_id": row["project_id"],
        "ident": row["ident"],
        "status": row["status"],
        "origin": row["origin"],
        "meeting_id": row.get("meeting_id"),
        "at_ms": row.get("at_ms"),
        "task_id": row.get("task_id"),
        "decision_id": row.get("decision_id"),
        "to_decision_id": row.get("to_decision_id"),
        "content_key": row.get("content_key"),
        "root_id": row.get("root_id"),
        "rel_path": row.get("rel_path"),
        "stem_key": row.get("stem_key"),
        "file_id": row.get("file_id"),
        "quote": row.get("quote") or "",
        "evidence_json": evidence,
        "score": row.get("score"),
        "now": now,
        "since": since,
    }


def literal_mention_exists(connection: Any, meeting_id: str, project_id: str, stem_key: str) -> bool:
    """meeting_file_mentions 里同一 (会议, 项目, 词干) 已经有行（不管什么状态）。"""
    return (
        connection.execute(
            "SELECT 1 FROM meeting_file_mentions WHERE meeting_id = ? AND project_id = ? AND stem_key = ?",
            (meeting_id, project_id, stem_key),
        ).fetchone()
        is not None
    )


def upsert_system(connection: Any, rows: Iterable[dict[str, Any]], now: str, *, since: str) -> int:
    """循环写 relations 的唯一入口。since 是这一轮开始读输入的时刻：这一轮算的时候你回答、撤销或恢复
    过的行（updated_at 晚于 since）不碰。新行被同一起点、同一文件的 rejected 行挡住时不写；放宽的提到
    在同一词干已经有字面行时不写。返回真写了的行数（插入加改动）。"""
    written = 0
    for row in rows:
        kind = row["kind"]
        if kind == "mention" and row.get("stem_key") and literal_mention_exists(
            connection, row.get("meeting_id"), row["project_id"], row["stem_key"]
        ):
            continue
        if (
            kind in FILE_KINDS
            and row["status"] in ("shown", "suggested")
            and is_rejected(
                connection,
                kind,
                row["project_id"],
                row.get(FROM_COLUMN[kind]),
                row.get("content_key"),
                row.get("root_id"),
                row.get("rel_path"),
            )
        ):
            continue
        written += connection.execute(_UPSERT_SQL, _params(row, now, since)).rowcount
    return written


def _scope_sql(scope: dict[str, Any] | None) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    for column, value in (scope or {}).items():
        if column not in _SCOPE_COLUMNS:
            raise ValueError(f"不认识的范围：{column}")
        conditions.append(f"{column} = ?")
        params.append(value)
    return "".join(f" AND {condition}" for condition in conditions), params


def clear_missing(
    connection: Any,
    kind: str,
    project_id: str,
    scope: dict[str, Any] | None,
    keep: Iterable[str],
    now: str,
    *,
    since: str,
) -> int:
    """这个范围（scope：{meeting_id: …} 这类，None 是整个项目）里这次没再找到的系统行（shown、
    suggested）改成 cleared；只改 updated_at 不晚于 since 的行（这一轮算的时候你撤销过的行，回到
    shown 也不会因为不在 keep 里又被收回）。返回改了几行。"""
    where, params = _scope_sql(scope)
    kept = set(keep)
    ids = [
        int(row["id"])
        for row in connection.execute(
            f"""SELECT id, ident FROM relations
                 WHERE kind = ? AND project_id = ? AND status IN ('shown', 'suggested')
                   AND origin != 'manual' AND updated_at <= ?{where}""",
            [kind, project_id, since, *params],
        ).fetchall()
        if row["ident"] not in kept
    ]
    cleared = 0
    for start in range(0, len(ids), 400):
        part = ids[start : start + 400]
        cleared += connection.execute(
            f"""UPDATE relations SET status = 'cleared', updated_at = ?
                 WHERE id IN ({", ".join("?" for _ in part)}) AND status IN ('shown', 'suggested')
                   AND origin != 'manual' AND updated_at <= ?""",
            [now, *part, since],
        ).rowcount
    return cleared


def is_rejected(
    connection: Any,
    kind: str,
    project_id: str,
    from_end: Any,
    content_key: str | None,
    root_id: int | None,
    rel_path: str | None,
) -> bool:
    """起点相同、目标按 content_key 或 (root_id, rel_path) 对得上的 rejected 行，挡住新行。"""
    if from_end is None or (not content_key and (root_id is None or not rel_path)):
        return False
    column = FROM_COLUMN[kind]
    return (
        connection.execute(
            f"""SELECT 1 FROM relations
                 WHERE {column} = ? AND kind = ? AND project_id = ? AND status = 'rejected'
                   AND ((? IS NOT NULL AND content_key = ?)
                        OR (? IS NOT NULL AND root_id = ? AND rel_path = ?))
                 LIMIT 1""",
            (from_end, kind, project_id, content_key, content_key, root_id, root_id, rel_path),
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------- 回答和撤销


def _row(connection: Any, relation_id: int) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
    if row is None:
        raise RelationError(404, GONE)
    return dict(row)


def _left_project(connection: Any, row: dict[str, Any]) -> bool:
    """那场会、那条任务或决议所在的会已经不在这一行的项目里。"""
    ends = connection.execute(
        """SELECT (SELECT project_id FROM meetings WHERE id = :meeting) AS meeting_project,
                  (SELECT project_id FROM tasks WHERE id = :task) AS task_project,
                  (SELECT m.project_id FROM decisions d JOIN meetings m ON m.id = d.meeting_id
                    WHERE d.id = :decision) AS decision_project,
                  (SELECT m.project_id FROM decisions d JOIN meetings m ON m.id = d.meeting_id
                    WHERE d.id = :to_decision) AS to_decision_project""",
        {
            "meeting": row["meeting_id"],
            "task": row["task_id"],
            "decision": row["decision_id"],
            "to_decision": row["to_decision_id"],
        },
    ).fetchone()
    for column, key in (
        ("meeting_id", "meeting_project"),
        ("task_id", "task_project"),
        ("decision_id", "decision_project"),
        ("to_decision_id", "to_decision_project"),
    ):
        if row[column] is not None and ends[key] != row["project_id"]:
            return True
    return False


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def undo_until(decided_at: str | None) -> str | None:
    """撤销期的终点：decided_at 加 600 秒（tasks.UNDO_WINDOW_SECONDS），前端以它为准。"""
    if not decided_at:
        return None
    until = _parse(decided_at) + timedelta(seconds=UNDO_WINDOW_SECONDS)
    return until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick_target(connection: Any, row: dict[str, Any], file_id: Any) -> dict[str, Any]:
    """［换成这份］只能换成本项目里同一词干的活文件（和第二期的规矩一样）。"""
    try:
        target_id = int(file_id)
    except (TypeError, ValueError):
        raise RelationError(422, WRONG_PICK) from None
    target = connection.execute(
        f"""SELECT f.* FROM material_files f JOIN project_material_roots pr ON pr.id = f.root_id
             WHERE f.id = ? AND pr.project_id = ? AND f.stem_key = ? AND f.stem_key != ''
               AND f.gone_at IS NULL AND f.zone IN ({_ZONES_SQL})""",
        (target_id, row["project_id"], row["stem_key"] or ""),
    ).fetchone()
    if target is None:
        raise RelationError(422, WRONG_PICK)
    return dict(target)


def _task_deliverable(connection: Any, task_id: str, file: dict[str, Any], content_key: str | None) -> int | None:
    """这份文件已经是这条任务的交付物：按内容标识、按位置，旧的手填路径也算。"""
    found = connection.execute(
        """SELECT d.id FROM deliverables d
             LEFT JOIN deliverable_files df ON df.deliverable_id = d.id
             LEFT JOIN project_material_roots pr ON pr.id = ?
            WHERE d.task_id = ?
              AND ((df.content_key IS NOT NULL AND df.content_key IN (?, ?))
                   OR (df.root_id = ? AND df.rel_path = ?)
                   OR (d.kind = 'file' AND pr.path IS NOT NULL AND d.url = rtrim(pr.path, '/') || '/' || ?))
            ORDER BY d.id LIMIT 1""",
        (
            file["root_id"],
            task_id,
            content_key,
            file.get("content_key"),
            file["root_id"],
            file["rel_path"],
            file["rel_path"],
        ),
    ).fetchone()
    return int(found["id"]) if found is not None else None


def _file_of(connection: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    live = live_file(connection, row)
    if live is not None:
        return {"id": live["id"], "name": live["name"]}
    if row["file_id"] is None:
        return None
    found = connection.execute("SELECT id, name FROM material_files WHERE id = ?", (row["file_id"],)).fetchone()
    return {"id": found["id"], "name": found["name"]} if found is not None else None


def answer(connection: Any, relation_id: int, body: dict[str, Any], now: str) -> dict[str, Any]:
    """回答一条关联（在调用方的事务里）。body 是 {"answer": yes/no/updated/pick/restore, "file_id"}，
    file_id 只在 pick 时给。返回 {relation, undo_until, deliverable_id}。"""
    row = _row(connection, relation_id)
    kind = row["kind"]
    choice = body.get("answer")
    if choice != "restore" and choice not in ANSWERS.get(kind, {}):
        raise RelationError(422, WRONG_ANSWER)
    if _left_project(connection, row):
        raise RelationError(409, LEFT_PROJECT)
    if choice == "restore":
        if row["status"] not in RESTORABLE:
            raise RelationError(409, HANDLED)
        if _manual_related(row):
            # 4d：相关材料栏里你手动建的「不相关」行，改回相关时整行删掉，回到「没说过」
            return _drop_manual(connection, row, now)
    elif row["status"] != FIRST_STATUS[kind]:
        raise RelationError(409, HANDLED)

    changes: dict[str, Any] = {}
    remember: tuple[str, ...] = ()
    extra: dict[str, Any] = {}
    deliverable_id: int | None = None
    if choice == "restore":
        changes["status"] = FIRST_STATUS[kind]
    elif choice == "pick":
        target = _pick_target(connection, row, body.get("file_id"))
        changes.update(
            origin="manual",
            file_id=target["id"],
            content_key=target["content_key"],
            root_id=target["root_id"],
            rel_path=target["rel_path"],
        )
        # 换文件时连原来的文件一起记，撤销时整个换回来
        remember = ("origin", "file_id", "content_key", "root_id", "rel_path")
    elif choice == "yes":
        live = live_file(connection, row)
        if live is None:
            raise RelationError(422, FILE_GONE)
        content_key = row["content_key"] or live.get("content_key")
        deliverable_id = _task_deliverable(connection, row["task_id"], live, row["content_key"])
        extra["created_deliverable"] = deliverable_id is None
        if deliverable_id is None:
            deliverable_id = _insert_deliverable(
                connection,
                row["task_id"],
                name=live["name"],
                content_key=content_key,
                root_id=live["root_id"],
                rel_path=live["rel_path"],
                now=now,
            )
        changes.update(status=ANSWERS[kind][choice], deliverable_id=deliverable_id)
    else:
        changes["status"] = ANSWERS[kind][choice]
    changes["decided_at"] = now

    prev = {
        column: row[column]
        for column in _ANSWER_COLUMNS
        if column in changes and (column in remember or row[column] != changes[column])
    }
    prev.update(extra)
    assignments = ", ".join(f"{column} = ?" for column in changes)
    connection.execute(
        f"UPDATE relations SET {assignments}, prev_json = ?, updated_at = ? WHERE id = ?",
        [*changes.values(), canonical(prev), now, relation_id],
    )
    fresh = _row(connection, relation_id)
    return {
        "relation": serialize(fresh, _file_of(connection, fresh)),
        "undo_until": undo_until(now),
        "deliverable_id": deliverable_id,
    }


def undo(connection: Any, relation_id: int, now: str) -> dict[str, Any]:
    """600 秒内按 prev_json 改回，再清空它（第二次撤销回 409）。产出的［是］这次新登记了交付物时，
    在同一个事务里删掉它；交付物已经在任务抽屉里删了（deliverable_id 已被置空）时照样成功。
    返回 {relation, removed_deliverable_id}。"""
    row = _row(connection, relation_id)
    if _left_project(connection, row):
        raise RelationError(409, LEFT_PROJECT)
    if not row["prev_json"]:
        raise RelationError(409, UNDONE)
    if row["decided_at"] and _parse(now) > _parse(row["decided_at"]) + timedelta(seconds=UNDO_WINDOW_SECONDS):
        raise RelationError(409, UNDO_EXPIRED)
    if _manual_related(row):
        # 4d：你手动建的「不相关」行（栏里那一条还没连成线），撤销时整行删掉
        return {**_drop_manual(connection, row, now), "removed_deliverable_id": None}
    prev = json.loads(row["prev_json"])
    created = bool(prev.pop("created_deliverable", False))
    removed: int | None = None
    if created and row["deliverable_id"] is not None:
        _delete_deliverable(connection, row["task_id"], int(row["deliverable_id"]), now)
        removed = int(row["deliverable_id"])
    restore = {column: value for column, value in prev.items() if column in _ANSWER_COLUMNS}
    assignments = "".join(f"{column} = ?, " for column in restore)
    connection.execute(
        f"UPDATE relations SET {assignments}prev_json = NULL, updated_at = ? WHERE id = ?",
        [*restore.values(), now, relation_id],
    )
    fresh = _row(connection, relation_id)
    return {"relation": serialize(fresh, _file_of(connection, fresh)), "removed_deliverable_id": removed}


def _manual_related(row: dict[str, Any]) -> bool:
    return row["kind"] == "related" and row["origin"] == "manual"


def _drop_manual(connection: Any, row: dict[str, Any], now: str) -> dict[str, Any]:
    """删掉你手动建的相关行（撤销、改回相关都这样），返回它删之前的样子，状态写成 shown（回到栏里）。"""
    file = _file_of(connection, row)
    connection.execute("DELETE FROM relations WHERE id = ?", (row["id"],))
    gone = {**row, "status": "shown", "decided_at": None, "updated_at": now}
    return {"relation": serialize(gone, file), "undo_until": None, "deliverable_id": None, "deleted": True}


def reject_related(
    connection: Any, meeting_id: str, *, content_key: str, file_id: int, now: str
) -> dict[str, Any]:
    """相关材料栏的［不相关］（POST /api/meetings/{id}/related-materials/reject）：身份是（会，文件内容）。
    已经有行就按 answer(no) 办；没有行（这一条只在窗里，没连成线）就新建一行 origin manual、status
    rejected，同时记 root_id、rel_path（原地改过也挡）。"""
    meeting = connection.execute("SELECT id, project_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if meeting is None:
        raise RelationError(404, MEETING_GONE)
    if not meeting["project_id"]:
        raise RelationError(409, NO_PROJECT)
    file = connection.execute(
        """SELECT f.*, r.project_id FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
            WHERE f.id = ?""",
        (int(file_id),),
    ).fetchone()
    if file is None:
        raise RelationError(404, NOT_INDEXED)
    if file["project_id"] != meeting["project_id"]:
        raise RelationError(422, OTHER_PROJECT)
    project_id = str(meeting["project_id"])
    ident = f"{meeting_id}|{content_key}"
    existing = connection.execute(
        "SELECT * FROM relations WHERE kind = 'related' AND project_id = ? AND ident = ?", (project_id, ident)
    ).fetchone()
    if existing is not None:
        row = dict(existing)
        if row["status"] == "shown":
            return answer(connection, int(row["id"]), {"answer": "no"}, now)
        if row["status"] != "cleared":
            raise RelationError(409, HANDLED)
        # 收回过的系统行：改成 rejected，撤销时回到 cleared
        prev = {"status": "cleared", "decided_at": row["decided_at"]}
        connection.execute(
            """UPDATE relations SET status = 'rejected', decided_at = ?, prev_json = ?, updated_at = ?,
                   root_id = COALESCE(root_id, ?), rel_path = COALESCE(rel_path, ?) WHERE id = ?""",
            (now, canonical(prev), now, file["root_id"], file["rel_path"], row["id"]),
        )
        fresh = _row(connection, int(row["id"]))
        return {"relation": serialize(fresh, _file_of(connection, fresh)), "undo_until": undo_until(now),
                "deliverable_id": None}
    cursor = connection.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, content_key, root_id, rel_path,
               file_id, quote, evidence_json, prev_json, decided_at, created_at, updated_at)
           VALUES ('related', ?, ?, 'rejected', 'manual', ?, ?, ?, ?, ?, '', '{}', ?, ?, ?, ?)""",
        (
            project_id, ident, meeting_id, content_key, file["root_id"], file["rel_path"], file["id"],
            canonical({"created": True}), now, now, now,
        ),
    )
    fresh = _row(connection, int(cursor.lastrowid))
    return {"relation": serialize(fresh, _file_of(connection, fresh)), "undo_until": undo_until(now),
            "deliverable_id": None}


# ---------------------------------------------------------------------- 合并项目


def repoint_project(connection: Any, src: str, dst: str, now: str) -> dict[str, int]:
    """合并项目（project_names.merge_project 在 UPDATE meetings 之前调）：先把行搬到目标项目，后面
    会议改项目时两个离开项目的触发器都不删它们。你的回答优先于目标项目的系统行；两边都回答过同一条
    时留目标项目的，搬不过去的剩行删掉。"""
    connection.execute(
        f"""DELETE FROM relations
             WHERE project_id = ? AND {SYSTEM_ROW_SQL}
               AND EXISTS (SELECT 1 FROM relations s
                            WHERE s.project_id = ? AND s.kind = relations.kind AND s.ident = relations.ident)""",
        (dst, src),
    )
    moved = connection.execute(
        "UPDATE OR IGNORE relations SET project_id = ?, updated_at = ? WHERE project_id = ?", (dst, now, src)
    ).rowcount
    connection.execute("DELETE FROM relations WHERE project_id = ?", (src,))
    # 2d 的字面提到（连同「不是这份文件」和你换过的 picked=1）：以前 UPDATE meetings 先触发离开项目，
    # 有效行被删、剩下的驳回还挂在源项目名下，这张表没有外键，合并后就再也对不上
    mentions = connection.execute(
        "UPDATE OR IGNORE meeting_file_mentions SET project_id = ? WHERE project_id = ?", (dst, src)
    ).rowcount
    connection.execute("DELETE FROM meeting_file_mentions WHERE project_id = ?", (src,))
    # 挖出来的候选词：你在源项目点过的［记入］［不是］优先于目标项目里还在等的同一个词
    connection.execute(
        """DELETE FROM glossary_candidates
            WHERE project_id = ? AND status IN ('pending', 'dropped')
              AND EXISTS (SELECT 1 FROM glossary_candidates s
                           WHERE s.project_id = ? AND s.term_key = glossary_candidates.term_key
                             AND s.wrong = glossary_candidates.wrong AND s.status IN ('accepted', 'rejected'))""",
        (dst, src),
    )
    candidates = connection.execute(
        "UPDATE OR IGNORE glossary_candidates SET project_id = ?, updated_at = ? WHERE project_id = ?",
        (dst, now, src),
    ).rowcount
    return {"relations": moved, "mentions": mentions, "candidates": candidates}
