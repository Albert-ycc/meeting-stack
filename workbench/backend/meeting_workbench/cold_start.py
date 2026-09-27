"""冷启动整理（v13 第一期 1b-4）：换成新归属规则后要补做的几件事。都是幂等的，做完记在 app_state 里不再做。

- 弱归属复评：旧规则里靠「内容相近」「任务多数」归进项目的会，用新规则重判一遍。结论一样只补证据；
  不一样就改成待你选（原项目排第一个候选），会议本身先不动。
- 旧分组：词典里没挂项目的自由分组。和项目同名的直接挂上；其余按规则整理（分组名是某个项目名的一部分就归
  那个项目，否则归公共），词典页留一行说明，可以撤销。
- 同名文件夹：还没挂文件夹的项目，工作台问一次要不要挂上同名文件夹；这里给候选，并记下「不挂」和「稍后」。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database, utc_now
from .glossary import rewrite_snapshot
from .project_linking import ProjectLinker
from .project_names import also_entries, unmounted_project_folders
from .project_profile import norm_key

logger = logging.getLogger(__name__)

WEAK_METHODS = ("semantic", "task_majority")
WEAK_METHOD_LABELS = {"semantic": "内容相近", "task_majority": "任务多数"}
# 复评要调 LLM，每轮扫描只做几场，免得升级后第一轮扫描卡太久。
REEVAL_PER_ROUND = 5
PUBLIC_SCOPE = "通用"
GROUPS_EVENT = "glossary_groups_organized"
GROUPS_UNDONE_EVENT = "glossary_groups_organize_undone"
FOLDER_SNOOZE_DAYS = 3


def _state(connection: Any, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(connection: Any, key: str, value: str) -> None:
    connection.execute(
        """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, utc_now()),
    )


# ---------------------------------------------------------------------- 入口


def run(db: Database, settings: Settings, linker: ProjectLinker) -> dict[str, Any]:
    """扫描循环每轮调一次；全部做完后只剩一次 app_state 查询。"""
    stats: dict[str, Any] = {"groups": None, "reevaluated": 0, "done": False}
    with db.autocommit() as connection:
        if _state(connection, "cold_start_done") == "1":
            stats["done"] = True
            return stats
    with db.transaction() as connection:
        if _state(connection, "legacy_groups_done") != "1":
            stats["groups"] = organize_legacy_groups(db, connection)
            _set_state(connection, "legacy_groups_done", "1")
    if stats["groups"] and stats["groups"]["changed_terms"]:
        rewrite_snapshot(db, settings.data_dir / "glossary-snapshot.json")
    reeval = reevaluate_weak(db, linker, limit=REEVAL_PER_ROUND)
    stats["reevaluated"] = reeval["processed"]
    if reeval["remaining"] == 0:
        with db.transaction() as connection:
            _set_state(connection, "reeval_weak_ai", "done")
            _set_state(connection, "cold_start_done", "1")
        stats["done"] = True
    return stats


# ---------------------------------------------------------------------- 弱归属复评


_WEAK_SQL = """FROM meetings m
               JOIN project_links pl
                 ON pl.id = (SELECT MAX(id) FROM project_links WHERE meeting_id = m.id)
              WHERE m.project_origin = 'ai' AND m.project_id IS NOT NULL
                AND pl.status = 'done' AND pl.method IN ('semantic', 'task_majority')"""


def reevaluate_weak(db: Database, linker: ProjectLinker, *, limit: int) -> dict[str, int]:
    rows = db.query_all(
        f"""SELECT m.id AS meeting_id, m.title, m.project_id, m.current_minutes_version_id,
                   pl.id AS link_id, pl.method, pl.attempts
              {_WEAK_SQL}
             ORDER BY pl.id LIMIT ?""",
        (limit,),
    )
    processed = 0
    if rows:
        cue_table = linker.cue_table()
        names = {row["id"]: row["name"] for row in db.query_all("SELECT id, name FROM projects")}
        for row in rows:
            try:
                if _reevaluate_one(db, linker, row, cue_table=cue_table, names=names):
                    processed += 1
            except Exception:
                logger.exception("弱归属复评失败 meeting_id=%s", row["meeting_id"])
                db.execute(
                    "UPDATE project_links SET attempts=COALESCE(attempts, 0)+1 WHERE id=?",
                    (row["link_id"],),
                )
    remaining = db.query_one(f"SELECT COUNT(*) AS count {_WEAK_SQL}")["count"]
    return {"processed": processed, "remaining": int(remaining)}


def _reevaluate_one(
    db: Database,
    linker: ProjectLinker,
    row: dict[str, Any],
    *,
    cue_table: dict[str, Any],
    names: dict[str, str],
) -> bool:
    now = utc_now()
    minutes = None
    if row["current_minutes_version_id"]:
        minutes = db.query_one(
            "SELECT markdown FROM minutes_versions WHERE id=?", (row["current_minutes_version_id"],)
        )
    if minutes is None:
        # 没有纪要就没法重判：保持原样，只是不再算进待复评。
        db.execute("UPDATE project_links SET method='reeval_kept' WHERE id=?", (row["link_id"],))
        return True
    result = linker._classify(
        meeting_id=row["meeting_id"],
        title=row["title"] or "",
        minutes_markdown=minutes["markdown"],
        attempts=int(row["attempts"] or 0),
        cue_table=cue_table,
    )
    if result["decision"] == "retry":
        # LLM 一时调不通：下一轮再试；连着失败几次后 _classify 会退回只看字面线索。
        db.execute(
            "UPDATE project_links SET attempts=COALESCE(attempts, 0)+1 WHERE id=?", (row["link_id"],)
        )
        return False
    original = row["project_id"]
    evidence_json = json.dumps(result["evidence"], ensure_ascii=False)
    if result["decision"] == "auto" and result["project_id"] == original:
        db.execute(
            """UPDATE project_links SET method=?, evidence_json=?, reason=?, raw_response=?
                WHERE id=?""",
            (result["method"], evidence_json, result["reason"], result["raw_response"], row["link_id"]),
        )
        return True

    literal = result.get("literal") or {}
    llm_pick = next(
        (entry.get("project_id") for entry in result["evidence"] if entry.get("kind") == "llm"), None
    )
    candidates = [
        {
            "project_id": original,
            "project_name": names.get(original),
            "count": literal.get(original, 0),
            "llm": llm_pick == original,
        }
    ]
    others = (
        [{"project_id": result["project_id"], "project_name": names.get(result["project_id"]),
          "count": literal.get(result["project_id"], 0), "llm": llm_pick == result["project_id"]}]
        if result["decision"] == "auto"
        else result["candidates"]
    )
    for candidate in others:
        if candidate["project_id"] != original and len(candidates) < 2:
            candidates.append(candidate)
    label = WEAK_METHOD_LABELS.get(row["method"], "旧规则")
    reason = f"原来按「{label}」归到这里，按新规则拿不准，请你确认"
    if result["reason"]:
        reason += f"（{result['reason']}）"
    with db.transaction() as connection:
        connection.execute(
            """UPDATE project_links
                  SET status='needs_review', method='reeval', candidates_json=?, evidence_json=?,
                      reason=?, raw_response=?, finished_at=?
                WHERE id=?""",
            (
                json.dumps(candidates, ensure_ascii=False), evidence_json, reason,
                result["raw_response"], now, row["link_id"],
            ),
        )
        db.add_event(
            "meeting_project_needs_review",
            meeting_id=row["meeting_id"],
            actor="system",
            payload={"reason": reason, "candidates": candidates, "reevaluated_from": row["method"]},
            connection=connection,
        )
    return True


# ---------------------------------------------------------------------- 旧分组


def organize_legacy_groups(db: Database, connection: Any) -> dict[str, Any]:
    """没挂项目、也不是「通用」的术语分组：同名挂项目（不打扰），其余按规则整理并留一条可撤销的记录。"""
    rows = connection.execute(
        f"""SELECT id, scope FROM glossary_terms
             WHERE project_id IS NULL AND TRIM(scope) NOT IN ('', '{PUBLIC_SCOPE}')
             ORDER BY scope, id"""
    ).fetchall()
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row["scope"], []).append(row["id"])
    projects = [
        {
            "id": row["id"],
            "name": row["name"],
            "key": norm_key(row["name"]),
            "also": [norm_key(entry["name"]) for entry in also_entries(row["also_names"])],
        }
        for row in connection.execute("SELECT id, name, also_names FROM projects ORDER BY name")
    ]
    silent = 0
    organized: list[dict[str, Any]] = []
    for scope, term_ids in groups.items():
        key = norm_key(scope)
        exact = [project for project in projects if key and project["key"] == key]
        target = exact[0] if exact else None
        if target is None and len(key) >= 2:
            contained = [
                project
                for project in projects
                if key in project["key"] or any(key in also for also in project["also"] if also)
            ]
            target = contained[0] if len(contained) == 1 else None
        placeholders = ", ".join("?" for _ in term_ids)
        now = utc_now()
        if target is not None:
            connection.execute(
                f"UPDATE glossary_terms SET project_id=?, scope=?, updated_at=? WHERE id IN ({placeholders})",
                (target["id"], target["name"], now, *term_ids),
            )
        else:
            connection.execute(
                f"UPDATE glossary_terms SET scope=?, updated_at=? WHERE id IN ({placeholders})",
                (PUBLIC_SCOPE, now, *term_ids),
            )
        if exact:
            silent += 1
            continue
        organized.append(
            {
                "scope": scope,
                "count": len(term_ids),
                "term_ids": term_ids,
                "project_id": target["id"] if target else None,
                "project_name": target["name"] if target else None,
            }
        )
    if organized:
        db.add_event(GROUPS_EVENT, actor="system", payload={"groups": organized}, connection=connection)
    return {
        "silent": silent,
        "organized": len(organized),
        "changed_terms": sum(len(ids) for ids in groups.values()),
    }


def _latest_groups_event(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT id, payload_json, created_at FROM events WHERE event_type=? ORDER BY id DESC LIMIT 1",
        (GROUPS_EVENT,),
    ).fetchone()
    if row is None:
        return None
    undone = connection.execute(
        """SELECT 1 FROM events WHERE event_type=?
              AND json_extract(payload_json, '$.event_id') = ?""",
        (GROUPS_UNDONE_EVENT, row["id"]),
    ).fetchone()
    return {
        "event_id": row["id"],
        "at": row["created_at"],
        "groups": json.loads(row["payload_json"] or "{}").get("groups", []),
        "undone": undone is not None,
    }


def legacy_groups_summary(connection: Any) -> dict[str, Any] | None:
    """词典页那一行「已自动整理 N 个旧分组［查看/撤销］」要的数据；没整理过或已收起时为 None。"""
    event = _latest_groups_event(connection)
    if event is None:
        return None
    if _state(connection, "legacy_groups_dismissed") == str(event["event_id"]):
        return None
    return {
        "event_id": event["event_id"],
        "at": event["at"],
        "undone": event["undone"],
        "groups": [
            {key: group[key] for key in ("scope", "count", "project_id", "project_name")}
            for group in event["groups"]
        ],
    }


def undo_legacy_groups(db: Database, connection: Any) -> dict[str, Any]:
    """撤销自动整理：整理后又被人改过的词条不动。"""
    event = _latest_groups_event(connection)
    if event is None or event["undone"]:
        raise ValueError("没有可以撤销的整理")
    restored = 0
    now = utc_now()
    for group in event["groups"]:
        for term_id in group["term_ids"]:
            if group["project_id"]:
                changed = connection.execute(
                    """UPDATE glossary_terms SET project_id=NULL, scope=?, updated_at=?
                        WHERE id=? AND project_id=?""",
                    (group["scope"], now, term_id, group["project_id"]),
                ).rowcount
            else:
                changed = connection.execute(
                    """UPDATE glossary_terms SET scope=?, updated_at=?
                        WHERE id=? AND project_id IS NULL AND scope=?""",
                    (group["scope"], now, term_id, PUBLIC_SCOPE),
                ).rowcount
            restored += changed
    db.add_event(
        GROUPS_UNDONE_EVENT,
        actor="user",
        payload={"event_id": event["event_id"], "restored": restored},
        connection=connection,
    )
    return {"restored": restored}


def dismiss_legacy_groups(connection: Any) -> None:
    event = _latest_groups_event(connection)
    if event is not None:
        _set_state(connection, "legacy_groups_dismissed", str(event["event_id"]))


# ---------------------------------------------------------------------- 同名文件夹


def _declined(connection: Any) -> set[str]:
    raw = _state(connection, "folder_suggestions_declined")
    try:
        value = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        value = []
    return {str(item) for item in value if item}


def folder_suggestions(connection: Any, settings: Settings) -> dict[str, Any]:
    """工作台横幅：还没挂文件夹的项目各自的同名/相近文件夹；说过不挂的项目不再出现。"""
    snoozed_until = _state(connection, "folder_suggestions_snoozed_until")
    if snoozed_until and snoozed_until > datetime.now(UTC).isoformat():
        return {"items": [], "snoozed_until": snoozed_until}
    declined = _declined(connection)
    items = [
        item for item in unmounted_project_folders(connection, settings)
        if item["project_id"] not in declined
    ]
    return {"items": items, "snoozed_until": None}


def decline_folder_suggestions(connection: Any, project_ids: list[str]) -> None:
    declined = _declined(connection) | {str(item) for item in project_ids if item}
    _set_state(connection, "folder_suggestions_declined", json.dumps(sorted(declined)))


def snooze_folder_suggestions(connection: Any) -> str:
    until = (datetime.now(UTC) + timedelta(days=FOLDER_SNOOZE_DAYS)).isoformat()
    _set_state(connection, "folder_suggestions_snoozed_until", until)
    return until
