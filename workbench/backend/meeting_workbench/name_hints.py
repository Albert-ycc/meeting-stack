"""「像是新项目 / 新需求」提示（第二期 2b）。

- 分类时过滤 AI 给的新需求名和会上的叫法（filter_spoken、accept_requirement_name）。
- name_hint：会议页、资料库列表、关系图、概览共用的判断，保证各处一致。
- 做了决定就清掉提示：settle_project_name、settle_requirement_name 返回撤销要用的东西，
  restore_settlement 照原样还回去（只在当前值仍为空时写回，决定行恢复原样而不是直接删）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import utc_now
from .project_names import also_entries
from .project_profile import is_subsequence, light_key, norm_key
from .service import ConflictError, NotFoundError
from .tasks import UNDO_WINDOW_SECONDS
from .text_scan import FormScanner, appears

SPOKEN_MIN_LENGTH = 2
SPOKEN_MAX_LENGTH = 20
MAX_SPOKEN = 3
REQUIREMENT_NAME_MAX_LENGTH = 40
# 新需求名（或它的叫法）在逐字稿里合计至少出现这么多次才提示。只数逐字稿：纪要会在
# 标题和正文里反复写主题，数纪要几乎每场都会过。
REQUIREMENT_MIN_SPOKEN = 2
_PROJECT_SUFFIX = re.compile(r"项目$")
# restore_settlement 只写回这两列。
_SETTLED_COLUMNS = ("new_project_name", "new_requirement_name")


def strip_project_suffix(name: str) -> str:
    stripped = _PROJECT_SUFFIX.sub("", (name or "").strip())
    return stripped or (name or "").strip()


def similar_title(a: str, b: str) -> bool:
    """需求标题相近：轻键相同，或较短一方 ≥3 字且是另一方的子序列（和 _match_kind 同一套，只是用轻键）。"""
    key_a, key_b = light_key(a), light_key(b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    short, long = sorted((key_a, key_b), key=len)
    return len(short) >= 3 and is_subsequence(short, long)


def json_names(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except json.JSONDecodeError:
        return []
    return (
        [item for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, list)
        else []
    )


# ---------------------------------------------------------------------- 分类时的过滤


def filter_spoken(raw: Any, *, segments: list[dict[str, Any]], minutes: str) -> list[str]:
    """AI 给的 spoken_names：2–20 字，并且在当前逐字稿或纪要里真的出现过，最多 3 个。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        text = item.strip()
        key = light_key(text)
        if not (SPOKEN_MIN_LENGTH <= len(text) <= SPOKEN_MAX_LENGTH) or not key or key in seen:
            continue
        if not appears(text, segments=segments, minutes=minutes):
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= MAX_SPOKEN:
            break
    return result


def _project_keys(row: dict[str, Any]) -> list[str]:
    return [row["name"], *(entry["name"] for entry in also_entries(row["also_names"]))]


def accept_requirement_name(
    connection: Any,
    name: Any,
    *,
    project_id: str,
    spoken: list[str],
    segments: list[dict[str, Any]],
) -> str | None:
    """AI 给的新需求名过了所有检查才留下，否则 None。"""
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text or len(text) > REQUIREMENT_NAME_MAX_LENGTH:
        return None
    key = light_key(strip_project_suffix(text))
    if not key:
        return None
    projects = connection.execute("SELECT id, name, also_names FROM projects").fetchall()
    own = next((row for row in projects if row["id"] == project_id), None)
    if own is None:
        return None
    # 就是这个项目自己的名字或也叫（「云图AI项目」≈「云图AI」）。
    if any(light_key(strip_project_suffix(value)) == key for value in _project_keys(own)):
        return None
    # 和这个项目任何状态的需求标题相近。
    for row in connection.execute(
        "SELECT title FROM requirements WHERE project_id=?", (project_id,)
    ).fetchall():
        if similar_title(text, row["title"]):
            return None
    # 其实是别的项目的名字或也叫。
    other_key = norm_key(text)
    for row in projects:
        if row["id"] != project_id and any(
            norm_key(value) == other_key for value in _project_keys(row)
        ):
            return None
    if (
        connection.execute(
            "SELECT 1 FROM requirement_name_decisions WHERE project_id=? AND name_key=?",
            (project_id, light_key(text)),
        ).fetchone()
        is not None
    ):
        return None
    counts = FormScanner([text, *spoken]).scan_segments(segments)
    if sum(entry["count"] for entry in counts.values()) < REQUIREMENT_MIN_SPOKEN:
        return None
    return text


# ---------------------------------------------------------------------- 共用的判断


class HintContext:
    """一次请求里判断多场会的提示：项目名、决定、需求标题只读一遍。

    关系图要把 SQL 条数固定住，传 projects（带 also_names）和 requirement_titles 进来就不再查库；
    这时不查名字决定（做决定时已经清掉了批次上的名字，分类时也会过滤掉）。"""

    def __init__(
        self,
        connection: Any,
        *,
        projects: Iterable[dict[str, Any]] | None = None,
        requirement_titles: dict[str, list[str]] | None = None,
    ):
        self.connection = connection
        self.preloaded = projects is not None
        rows = (
            [dict(row) for row in projects]
            if projects is not None
            else [
                dict(row)
                for row in connection.execute(
                    "SELECT id, name, also_names FROM projects"
                ).fetchall()
            ]
        )
        self.projects = {row["id"]: row for row in rows}
        self.taken_project_keys = {norm_key(value) for row in rows for value in _project_keys(row)}
        if not self.preloaded:
            self.taken_project_keys |= {
                row["norm_key"] for row in connection.execute("SELECT norm_key FROM name_decisions")
            }
        self._requirements: dict[str, list[str]] = dict(requirement_titles or {})
        self._decisions: dict[str, set[str]] = {}

    def _requirement_titles(self, project_id: str) -> list[str]:
        if project_id not in self._requirements and not self.preloaded:
            self._requirements[project_id] = [
                row["title"]
                for row in self.connection.execute(
                    "SELECT title FROM requirements WHERE project_id=?", (project_id,)
                ).fetchall()
            ]
        return self._requirements.get(project_id, [])

    def _decided(self, project_id: str) -> set[str]:
        if self.preloaded:
            return set()
        if project_id not in self._decisions:
            self._decisions[project_id] = {
                row["name_key"]
                for row in self.connection.execute(
                    "SELECT name_key FROM requirement_name_decisions WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            }
        return self._decisions[project_id]

    def hint(
        self,
        *,
        project_id: str | None,
        state: str | None,
        new_project_name: str | None,
        new_requirement_name: str | None,
        new_name_project_id: str | None,
        new_name_spoken: Any,
    ) -> dict[str, Any] | None:
        spoken = json_names(new_name_spoken)
        # 「待你选」但 AI 没选项目、提了新项目名：候选按钮下面加一行「也可能是一个新项目」。
        if new_project_name and state in ("new_project", "needs_review"):
            key = norm_key(new_project_name)
            if key and key not in self.taken_project_keys:
                return {"kind": "project", "name": new_project_name, "spoken": spoken}
            return None
        if (
            new_requirement_name
            and project_id
            and state in ("auto", "manual")
            and new_name_project_id == project_id
            and project_id in self.projects
        ):
            key = light_key(new_requirement_name)
            project = self.projects[project_id]
            if key in self._decided(project_id):
                return None
            own = {light_key(strip_project_suffix(value)) for value in _project_keys(project)}
            if light_key(strip_project_suffix(new_requirement_name)) in own:
                return None
            if any(
                similar_title(new_requirement_name, title)
                for title in self._requirement_titles(project_id)
            ):
                return None
            return {
                "kind": "requirement",
                "name": new_requirement_name,
                "spoken": spoken,
                "project_id": project_id,
                "project_name": project["name"],
            }
        return None


def name_hint(connection: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    """一场会的提示。row 要带 project_id、state、new_project_name、new_requirement_name、
    new_name_project_id、new_name_spoken。"""
    return HintContext(connection).hint(
        project_id=row.get("project_id"),
        state=row.get("state"),
        new_project_name=row.get("new_project_name"),
        new_requirement_name=row.get("new_requirement_name"),
        new_name_project_id=row.get("new_name_project_id"),
        new_name_spoken=row.get("new_name_spoken"),
    )


# ---------------------------------------------------------------------- 做了决定


def settle_project_name(
    connection: Any,
    names: Iterable[str],
    decision: str,
    target_id: str | None = None,
) -> dict[str, Any]:
    """记下项目名的决定（ignored / project），清掉所有 norm_key 相同的批次上的 new_project_name。

    已有 target 不同的「建成了项目」行不覆盖（那个名字已经指向别的项目）。
    返回撤销要用的东西：{"decisions": [...], "links": [{link_id, column, old_value}]}。
    """
    if decision not in ("ignored", "project"):
        raise ValueError("decision 只能是 ignored 或 project")
    pairs: dict[str, str] = {}
    for name in names:
        text = (name or "").strip()
        key = norm_key(text)
        if key and key not in pairs:
            pairs[key] = text
    undo: dict[str, Any] = {"decisions": [], "links": []}
    now = utc_now()
    for key, text in pairs.items():
        previous = connection.execute(
            "SELECT * FROM name_decisions WHERE norm_key=?", (key,)
        ).fetchone()
        if (
            previous is not None
            and previous["decision"] == "project"
            and previous["target_id"]
            and previous["target_id"] != target_id
        ):
            continue
        undo["decisions"].append(
            {
                "table": "name_decisions",
                "key": key,
                "previous": dict(previous) if previous else None,
            }
        )
        connection.execute(
            """INSERT INTO name_decisions(norm_key, name, decision, target_id, decided_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(norm_key) DO UPDATE SET
                   name=excluded.name, decision=excluded.decision, target_id=excluded.target_id,
                   decided_at=excluded.decided_at""",
            (key, text, decision, target_id, now),
        )
    if pairs:
        for row in connection.execute(
            """SELECT id, new_project_name FROM project_links
                WHERE COALESCE(new_project_name, '') != ''"""
        ).fetchall():
            if norm_key(row["new_project_name"]) in pairs:
                connection.execute(
                    "UPDATE project_links SET new_project_name=NULL WHERE id=?", (row["id"],)
                )
                undo["links"].append(
                    {
                        "link_id": row["id"],
                        "column": "new_project_name",
                        "old_value": row["new_project_name"],
                    }
                )
    return undo


def settle_requirement_name(
    connection: Any,
    project_id: str,
    name: str,
    decision: str,
    requirement_id: str | None = None,
) -> dict[str, Any]:
    """记下需求名的决定（made / ignored），只清同一项目、轻键相同的批次上的 new_requirement_name。
    「云图二期」和「云图三期」互不影响。"""
    if decision not in ("made", "ignored"):
        raise ValueError("decision 只能是 made 或 ignored")
    text = (name or "").strip()
    key = light_key(text)
    if not key:
        raise ValueError("名字不能为空")
    if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise NotFoundError("项目不存在")
    previous = connection.execute(
        "SELECT * FROM requirement_name_decisions WHERE project_id=? AND name_key=?",
        (project_id, key),
    ).fetchone()
    undo: dict[str, Any] = {
        "decisions": [
            {
                "table": "requirement_name_decisions",
                "project_id": project_id,
                "key": key,
                "previous": dict(previous) if previous else None,
            }
        ],
        "links": [],
    }
    connection.execute(
        """INSERT INTO requirement_name_decisions
               (project_id, name_key, name, decision, requirement_id, decided_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_id, name_key) DO UPDATE SET
               name=excluded.name, decision=excluded.decision,
               requirement_id=excluded.requirement_id, decided_at=excluded.decided_at""",
        (project_id, key, text, decision, requirement_id, utc_now()),
    )
    for row in connection.execute(
        """SELECT id, new_requirement_name FROM project_links
            WHERE new_name_project_id=? AND COALESCE(new_requirement_name, '') != ''""",
        (project_id,),
    ).fetchall():
        if light_key(row["new_requirement_name"]) == key:
            connection.execute(
                "UPDATE project_links SET new_requirement_name=NULL WHERE id=?", (row["id"],)
            )
            undo["links"].append(
                {
                    "link_id": row["id"],
                    "column": "new_requirement_name",
                    "old_value": row["new_requirement_name"],
                }
            )
    return undo


def merge_undo(*parts: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {"decisions": [], "links": []}
    for part in parts:
        if part:
            merged["decisions"].extend(part.get("decisions") or [])
            merged["links"].extend(part.get("links") or [])
    return merged


def restore_settlement(connection: Any, undo: dict[str, Any]) -> int:
    """照 settle_* 的返回值还原：批次上的名字只在当前仍为空时写回，决定行恢复原样。返回写回的批次数。"""
    restored = 0
    for link in undo.get("links") or []:
        column = link.get("column")
        if column not in _SETTLED_COLUMNS:
            continue
        restored += connection.execute(
            f"UPDATE project_links SET {column}=? WHERE id=? AND COALESCE({column}, '')=''",
            (link.get("old_value"), link.get("link_id")),
        ).rowcount
    for entry in reversed(undo.get("decisions") or []):
        previous = entry.get("previous")
        if entry.get("table") == "name_decisions":
            if previous is None:
                connection.execute("DELETE FROM name_decisions WHERE norm_key=?", (entry["key"],))
            else:
                connection.execute(
                    """INSERT OR REPLACE INTO name_decisions(norm_key, name, decision, target_id, decided_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        previous["norm_key"],
                        previous["name"],
                        previous["decision"],
                        previous["target_id"],
                        previous["decided_at"],
                    ),
                )
        elif entry.get("table") == "requirement_name_decisions":
            if previous is None:
                connection.execute(
                    "DELETE FROM requirement_name_decisions WHERE project_id=? AND name_key=?",
                    (entry["project_id"], entry["key"]),
                )
            elif (
                connection.execute(
                    "SELECT 1 FROM projects WHERE id=?", (previous["project_id"],)
                ).fetchone()
                is not None
            ):
                connection.execute(
                    """INSERT OR REPLACE INTO requirement_name_decisions
                           (project_id, name_key, name, decision, requirement_id, decided_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        previous["project_id"],
                        previous["name_key"],
                        previous["name"],
                        previous["decision"],
                        previous["requirement_id"],
                        previous["decided_at"],
                    ),
                )
    return restored


# ---------------------------------------------------------------------- 事件和撤销


def record_event(
    connection: Any, event_type: str, *, meeting_id: str | None, payload: dict[str, Any]
) -> tuple[int, str]:
    now = utc_now()
    cursor = connection.execute(
        """INSERT INTO events (meeting_id, job_id, event_type, actor, payload_json, created_at)
           VALUES (?, NULL, ?, 'user', ?, ?)""",
        (meeting_id, event_type, json.dumps(payload, ensure_ascii=False), now),
    )
    return int(cursor.lastrowid), now


def undo_until(at: str) -> str:
    return (datetime.fromisoformat(at) + timedelta(seconds=UNDO_WINDOW_SECONDS)).isoformat()


def load_undoable_event(
    connection: Any, event_id: int, event_types: tuple[str, ...], undone_type: str
) -> dict[str, Any]:
    """找到 10 分钟内、还没撤销过的事件；返回 {id, meeting_id, at, payload}。"""
    placeholders = ", ".join("?" for _ in event_types)
    row = connection.execute(
        f"""SELECT id, meeting_id, payload_json, created_at FROM events
             WHERE id=? AND event_type IN ({placeholders})""",
        (event_id, *event_types),
    ).fetchone()
    if row is None:
        raise NotFoundError("没有可以撤销的改动")
    if (
        connection.execute(
            """SELECT 1 FROM events WHERE event_type=? AND json_extract(payload_json, '$.event_id') = ?""",
            (undone_type, event_id),
        ).fetchone()
        is not None
    ):
        raise ConflictError("已经撤销过了")
    if datetime.now(UTC) > datetime.fromisoformat(row["created_at"]) + timedelta(
        seconds=UNDO_WINDOW_SECONDS
    ):
        raise ConflictError("已超过撤销时间，请直接改回")
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": row["id"],
        "meeting_id": row["meeting_id"],
        "at": row["created_at"],
        "payload": payload,
    }


def decide_name(
    connection: Any,
    *,
    kind: str,
    name: str,
    project_id: str | None = None,
    meeting_id: str | None = None,
) -> dict[str, Any]:
    """「不是新项目」「不算新需求」：记下决定、清掉提示，记一条可以撤销的事件。"""
    text = (name or "").strip()
    if not text:
        raise ValueError("名字不能为空")
    if kind == "requirement":
        if not project_id:
            raise ValueError("「不算新需求」要带上项目")
        undo = settle_requirement_name(connection, project_id, text, "ignored")
        key = light_key(text)
    elif kind == "project":
        undo = settle_project_name(connection, [text], "ignored")
        key = norm_key(text)
    else:
        raise ValueError("kind 只能是 project 或 requirement")
    if (
        meeting_id is not None
        and connection.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        is None
    ):
        meeting_id = None
    event_id, at = record_event(
        connection,
        "name_decision_made",
        meeting_id=meeting_id,
        payload={"kind": kind, "name": text, "project_id": project_id, "undo": undo},
    )
    return {
        "name": text,
        "norm_key": key,
        "kind": kind,
        "meetings_updated": len(undo["links"]),
        "event_id": event_id,
        "undo_until": undo_until(at),
    }


def undo_name_decision(connection: Any, event_id: int) -> dict[str, Any]:
    event = load_undoable_event(
        connection, event_id, ("name_decision_made",), "name_decision_undone"
    )
    restored = restore_settlement(connection, event["payload"].get("undo") or {})
    record_event(
        connection,
        "name_decision_undone",
        meeting_id=event["meeting_id"],
        payload={"event_id": event_id},
    )
    return {"ok": True, "meetings_restored": restored}
