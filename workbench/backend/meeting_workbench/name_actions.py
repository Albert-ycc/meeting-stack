"""「像是新项目 / 新需求」提示上的动作（第二期 2b）。

- name_candidates：候选名（文件夹 → 会上的叫法 → AI 起的名字 → 名字相近的文件夹）、同名的会、
  默认动作和文件夹动作。只查库和文件夹缓存，不读盘。
- add_spoken_also：从提示建成项目后，把会上说得最多的叫法记进也叫（够格才记，能撤销）。
- name_as_requirement：建成需求（一个事务：改归属、建需求或用已有的、关联同名的会、挂需求文件夹），
  10 分钟内能撤销。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .attribution import ATTRIBUTION_STATE_SQL, LATEST_LINK_JOIN, resolve_candidates
from .config import Settings
from .name_hints import (
    HintContext,
    json_names,
    load_undoable_event,
    merge_undo,
    record_event,
    restore_settlement,
    settle_project_name,
    settle_requirement_name,
    similar_title,
    strip_project_suffix,
    undo_until,
)
from .project_folders import CHECKING, folder_target, free_folders
from .project_names import (
    _dump_also,
    _match_kind,
    also_entries,
    sanitize_folder_name,
    validate_also_name,
)
from .project_profile import (
    GENERIC_FOLDER_NAMES,
    GENERIC_PROJECT_SPREAD,
    _spread_to_other_projects,
    light_key,
    norm_key,
)
from .project_linking import last_reassignment, reassign_meeting, undo_reassign
from .requirements import _validate_folder_paths, insert_requirement, link_meeting, merged_into
from .materials import ROOT_ONLINE
from .service import ConflictError, NotFoundError
from .text_scan import FormScanner

# 会上的叫法：3 字以上说过 1 次就算；2 字的要说过 2 次，而且只在没有 3 字以上的叫法达标时才用。
LONG_SPOKEN_LENGTH = 3
SHORT_SPOKEN_MIN_COUNT = 2
# 建成项目后记进也叫的叫法：3 字以上、说过 2 次以上。
SPOKEN_ALSO_MIN_COUNT = 2
# 「另有 N 场会也像是它」最多列几场。
MAX_SAME_NAME_MEETINGS = 20
REQUIREMENT_PRIORITY = "P2"

_ROW_SQL = f"""SELECT m.id, m.title, m.recording_date, m.created_at, m.project_id,
       m.current_transcript_version_id,
       pl.new_project_name, pl.new_requirement_name, pl.new_name_project_id, pl.new_name_spoken,
       pl.candidates_json, pl.evidence_json,
       {ATTRIBUTION_STATE_SQL} AS state
  FROM meetings m {LATEST_LINK_JOIN}"""


def _hint(context: HintContext, row: dict[str, Any]) -> dict[str, Any] | None:
    return context.hint(
        project_id=row["project_id"],
        state=row["state"],
        new_project_name=row["new_project_name"],
        new_requirement_name=row["new_requirement_name"],
        new_name_project_id=row["new_name_project_id"],
        new_name_spoken=row["new_name_spoken"],
    )


def _meeting(connection: Any, meeting_id: str) -> dict[str, Any]:
    row = connection.execute(f"{_ROW_SQL} WHERE m.id=?", (meeting_id,)).fetchone()
    if row is None:
        raise NotFoundError("会议不存在")
    return dict(row)


def _segments(connection: Any, version_id: str | None) -> list[dict[str, Any]]:
    if not version_id:
        return []
    return [
        dict(row)
        for row in connection.execute(
            "SELECT start_ms, text FROM segments WHERE version_id=? ORDER BY ordinal", (version_id,)
        ).fetchall()
    ]


def spoken_forms(connection: Any, name: str, spoken: list[str]) -> dict[str, str]:
    """要在逐字稿里数的叫法 → 这次出现记在哪个名字上。

    会上的叫法、AI 名字、AI 名字去掉「项目」，再加上词典里正写是这些词的错写（记在正写上）。
    """
    forms: dict[str, str] = {}
    for form in [*spoken, name, strip_project_suffix(name)]:
        text = (form or "").strip()
        if len(text) >= 2:
            forms.setdefault(text, text)
    targets = {light_key(form): form for form in forms}
    for row in connection.execute("SELECT term, aliases FROM glossary_terms").fetchall():
        target = targets.get(light_key(row["term"]))
        if target is None:
            continue
        for alias in json_names(row["aliases"]):
            if len(alias.strip()) >= 2:
                forms.setdefault(alias.strip(), target)
    return forms


def count_spoken(
    forms: dict[str, str], segments: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """一场会里每个名字说了几次：{名字: {count, first_ms, anchors_ms}}（错写记在正写上）。"""
    totals: dict[str, dict[str, Any]] = {}
    for form, entry in FormScanner(forms).scan_segments(segments).items():
        target = forms.get(form, form)
        bucket = totals.setdefault(
            target, {"count": 0, "first_ms": entry["first_ms"], "anchors_ms": []}
        )
        bucket["count"] += entry["count"]
        bucket["first_ms"] = min(bucket["first_ms"], entry["first_ms"])
        bucket["anchors_ms"] = sorted(set(bucket["anchors_ms"]) | set(entry["anchors_ms"]))[:5]
    return totals


def pick_spoken(totals: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """说得最多的叫法：3 字以上至少 1 次；2 字的至少 2 次，且只在没有 3 字以上的达标时才用。"""
    long = [
        (name, entry)
        for name, entry in totals.items()
        if len(light_key(name)) >= LONG_SPOKEN_LENGTH and entry["count"] >= 1
    ]
    if long:
        return max(long, key=lambda pair: (pair[1]["count"], len(pair[0])))
    short = [
        (name, entry)
        for name, entry in totals.items()
        if len(light_key(name)) == 2 and entry["count"] >= SHORT_SPOKEN_MIN_COUNT
    ]
    if short:
        return max(short, key=lambda pair: pair[1]["count"])
    return None


def _same_name_rows(
    connection: Any, context: HintContext, row: dict[str, Any], hint: dict[str, Any]
) -> list[dict[str, Any]]:
    """同名的会（含这一场，排第一）：项目提示是没归项目、名字 norm_key 相同的会；
    需求提示是同一项目里带同名提示的会。"""
    if hint["kind"] == "project":
        key = norm_key(hint["name"])
        rows = connection.execute(
            f"""{_ROW_SQL} WHERE m.project_id IS NULL AND COALESCE(pl.new_project_name, '') != ''
                ORDER BY COALESCE(m.recording_date, m.created_at) DESC"""
        ).fetchall()
        same = [
            dict(item)
            for item in rows
            if norm_key(item["new_project_name"]) == key
            and (found := _hint(context, dict(item))) is not None
            and found["kind"] == "project"
        ]
    else:
        key = light_key(hint["name"])
        rows = connection.execute(
            f"""{_ROW_SQL} WHERE m.project_id=? AND pl.new_name_project_id=?
                  AND COALESCE(pl.new_requirement_name, '') != ''
                ORDER BY COALESCE(m.recording_date, m.created_at) DESC""",
            (hint["project_id"], hint["project_id"]),
        ).fetchall()
        same = [
            dict(item)
            for item in rows
            if light_key(item["new_requirement_name"]) == key
            and (found := _hint(context, dict(item))) is not None
            and found["kind"] == "requirement"
        ]
    others = [item for item in same if item["id"] != row["id"]]
    return [row, *others][:MAX_SAME_NAME_MEETINGS]


def _requirement_projects(connection: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """没归项目时可以建成哪个项目的需求：AI 选的、线索命中的排前面，其余按名字。"""
    suggested: list[str] = [
        item["project_id"] for item in resolve_candidates(connection, row["candidates_json"], None)
    ]
    try:
        evidence = json.loads(row["evidence_json"] or "[]")
    except json.JSONDecodeError:
        evidence = []
    for entry in evidence if isinstance(evidence, list) else []:
        project_id = entry.get("project_id") if isinstance(entry, dict) else None
        if project_id and project_id not in suggested:
            suggested.append(project_id)
    projects = {
        item["id"]: dict(item)
        for item in connection.execute(
            "SELECT id, name, color FROM projects ORDER BY name"
        ).fetchall()
    }
    ordered = [pid for pid in suggested if pid in projects]
    ordered += [pid for pid in projects if pid not in ordered]
    return [
        {
            **{key: projects[pid][key] for key in ("id", "name", "color")},
            "suggested": pid in suggested,
        }
        for pid in ordered
    ]


def name_candidates(
    connection: Any, settings: Settings, cache: Any, meeting_id: str
) -> dict[str, Any]:
    """GET /api/meetings/{id}/name-candidates。"""
    row = _meeting(connection, meeting_id)
    context = HintContext(connection)
    hint = _hint(context, row)
    target = folder_target(connection, settings)
    result: dict[str, Any] = {
        "hint": hint,
        "candidates": [],
        "meetings": [],
        "default_action": None,
        "project": None,
        "requirement_projects": [],
        "folder": {"mode": "none", "reason": None},
        "folders_state": "ready",
        "create_parent": target["parent"],
        "create_parent_source": target["source"],
        "create_parent_state": None,
    }
    if hint is None:
        return result
    if target["parent"]:
        listing = cache.listing(target["parent"])
        result["create_parent_state"] = listing["state"] if listing else CHECKING

    candidates: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}

    def add(name: str, **evidence: Any) -> None:
        key = light_key(name)
        if not key:
            return
        candidate = by_key.get(key)
        if candidate is None:
            candidate = {
                "name": name,
                "folder_path": None,
                "spoken": None,
                "ai": False,
                "similar_folder_path": None,
            }
            by_key[key] = candidate
            candidates.append(candidate)
        for field, value in evidence.items():
            if value and not candidate.get(field):
                candidate[field] = value

    names = [hint["name"], *hint["spoken"]]
    exact_keys = {light_key(value) for value in [*names, strip_project_suffix(hint["name"])]}
    folders: list[dict[str, Any]] | None
    if hint["kind"] == "project":
        folders = free_folders(connection, settings, cache)
    else:
        folders = _requirement_folders(connection, cache, hint["project_id"])
    if folders is None:
        result["folders_state"] = CHECKING
        folders = []
    # ① 文件夹：只要同名的。
    for folder in folders:
        if light_key(folder["name"]) in exact_keys:
            add(folder["name"], folder_path=folder["path"])
    # ② 会上的叫法。
    forms = spoken_forms(connection, hint["name"], hint["spoken"])
    segments = _segments(connection, row["current_transcript_version_id"])
    picked = pick_spoken(count_spoken(forms, segments))
    if picked is not None:
        add(picked[0], spoken=picked[1])
    # ③ AI 起的名字。
    add(hint["name"], ai=True)
    # ④ 名字相近的文件夹：点它只填名字，文件夹动作仍是新建。
    for folder in folders:
        if light_key(folder["name"]) in by_key:
            continue
        similar = (
            _match_kind(folder["name"], names) is not None
            if hint["kind"] == "project"
            else any(similar_title(folder["name"], name) for name in names)
        )
        if similar:
            add(folder["name"], similar_folder_path=folder["path"])
    result["candidates"] = candidates

    for item in _same_name_rows(connection, context, row, hint):
        said = count_spoken(forms, _segments(connection, item["current_transcript_version_id"]))
        firsts = [entry["first_ms"] for entry in said.values()]
        result["meetings"].append(
            {
                "id": item["id"],
                "title": item["title"],
                "date": item["recording_date"] or (item["created_at"] or "")[:10],
                "said_ms": min(firsts) if firsts else None,
            }
        )

    first = candidates[0]
    if hint["kind"] == "requirement":
        result["default_action"] = "create_requirement"
        result["project"] = {"id": hint["project_id"], "name": hint["project_name"]}
        if first["folder_path"]:
            result["folder"] = {"mode": "mount", "path": first["folder_path"]}
    else:
        result["default_action"] = "create_project"
        result["requirement_projects"] = _requirement_projects(connection, row)
        if first["folder_path"]:
            result["folder"] = {"mode": "mount", "path": first["folder_path"]}
        elif target["parent"]:
            result["folder"] = {
                "mode": "create",
                "parent": target["parent"],
                "name": sanitize_folder_name(first["name"])[0],
                "parent_state": result["create_parent_state"],
                "inferred": target["source"] == "suggested",
            }
        else:
            result["folder"] = {
                "mode": "none",
                "reason": "还没有可参照的项目文件夹，这次先不建文件夹",
            }
    return result


def _requirement_folders(
    connection: Any, cache: Any, project_id: str
) -> list[dict[str, Any]] | None:
    """项目根目录下的一级子文件夹（需求文件夹只能是它们）；还没挂成需求文件夹的。缓存没好时 None。"""
    taken = {
        row["path"] for row in connection.execute("SELECT path FROM requirement_folders").fetchall()
    }
    folders: list[dict[str, Any]] = []
    for root in connection.execute(
        "SELECT path FROM project_material_roots WHERE project_id=? ORDER BY created_at, id",
        (project_id,),
    ).fetchall():
        entry = cache.get(root["path"])
        if entry is None:
            cache.refresh_in_background()
            return None
        if entry.get("state") != ROOT_ONLINE:
            continue
        for name in entry.get("child_dirs") or []:
            path = str(Path(root["path"]) / name)
            if path not in taken:
                folders.append({"name": name, "path": path})
    return folders


# ---------------------------------------------------------------------- 建成项目后记也叫


def add_spoken_also(
    connection: Any,
    *,
    project_id: str,
    final_name: str,
    source_name: str,
    meeting_ids: list[str],
) -> dict[str, Any] | None:
    """会上说得最多的叫法和最终名字不同、又够格时记进也叫（source=spoken），并记成「建成了项目」。

    够格：3 字以上、这几场会合计说过 2 次以上、不是通用名、在其他项目的会里出现不到 2 个项目、
    通过 validate_also_name。不够格就不加，建项目照常成功。返回 {name, event_id, undo_until}。
    """
    spoken: list[str] = []
    segment_lists: list[list[dict[str, Any]]] = []
    for meeting_id in meeting_ids:
        row = connection.execute(f"{_ROW_SQL} WHERE m.id=?", (meeting_id,)).fetchone()
        if row is None:
            continue
        spoken += [name for name in json_names(row["new_name_spoken"]) if name not in spoken]
        segment_lists.append(_segments(connection, row["current_transcript_version_id"]))
    forms = spoken_forms(connection, source_name, spoken)
    totals: dict[str, dict[str, Any]] = {}
    for segments in segment_lists:
        for name, entry in count_spoken(forms, segments).items():
            bucket = totals.setdefault(
                name, {"count": 0, "first_ms": entry["first_ms"], "anchors_ms": []}
            )
            bucket["count"] += entry["count"]
    picked = pick_spoken(totals)
    if picked is None:
        return None
    text, entry = picked
    project = connection.execute(
        "SELECT name, also_names FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if project is None:
        return None
    current = also_entries(project["also_names"])
    key = norm_key(text)
    if (
        key == norm_key(final_name)
        or any(norm_key(item["name"]) == key for item in current)
        or len(light_key(text)) < LONG_SPOKEN_LENGTH
        or entry["count"] < SPOKEN_ALSO_MIN_COUNT
        or text in GENERIC_FOLDER_NAMES
        or _spread_to_other_projects(connection, text, project_id) >= GENERIC_PROJECT_SPREAD
    ):
        return None
    try:
        text = validate_also_name(connection, text, project_id=project_id, project_name=final_name)
    except (ValueError, ConflictError):
        return None
    connection.execute(
        "UPDATE projects SET also_names=? WHERE id=?",
        (_dump_also([*current, {"name": text, "source": "spoken"}]), project_id),
    )
    undo = settle_project_name(connection, [text], "project", project_id)
    event_id, at = record_event(
        connection,
        "project_spoken_also_added",
        meeting_id=None,
        payload={"project_id": project_id, "name": text, "count": entry["count"], "undo": undo},
    )
    return {
        "name": text,
        "count": entry["count"],
        "event_id": event_id,
        "undo_until": undo_until(at),
    }


def undo_spoken_also(connection: Any, project_id: str, event_id: int) -> dict[str, Any]:
    """撤销记进也叫的会上叫法：从也叫里拿掉（还在的话），名字决定恢复原样。"""
    event = load_undoable_event(
        connection, event_id, ("project_spoken_also_added",), "project_spoken_also_undone"
    )
    payload = event["payload"]
    if payload.get("project_id") != project_id:
        raise NotFoundError("没有可以撤销的改动")
    project = connection.execute(
        "SELECT also_names FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if project is None:
        raise NotFoundError("项目不存在")
    current = also_entries(project["also_names"])
    kept = [
        item
        for item in current
        if not (item["name"] == payload.get("name") and item["source"] == "spoken")
    ]
    if kept != current:
        connection.execute(
            "UPDATE projects SET also_names=? WHERE id=?", (_dump_also(kept), project_id)
        )
    restore_settlement(connection, payload.get("undo") or {})
    record_event(
        connection, "project_spoken_also_undone", meeting_id=None, payload={"event_id": event_id}
    )
    return {"ok": True, "removed": kept != current}


# ---------------------------------------------------------------------- 建成需求


def name_as_requirement(
    connection: Any,
    *,
    meeting_id: str,
    title: str,
    project_id: str | None = None,
    folder_path: str | None = None,
) -> dict[str, Any]:
    """建成需求。调用方开事务。

    会议没归项目时 project_id 必填，同名的几场会先走 reassign_meeting（你归的）归到这个项目；
    会议已归项目时 project_id 可省，给了但不同就报错。项目里已有同名需求时不重复建，把会
    关联过去（existing=True）；否则按 P2 新建，把同名的会一起关联。folder_path 校验不过就不挂，
    需求照常建，folder_error 里说明。
    """
    text = (title or "").strip()
    if not text:
        raise ValueError("需求名不能为空")
    row = _meeting(connection, meeting_id)
    context = HintContext(connection)
    hint = _hint(context, row)
    if row["project_id"]:
        if project_id and project_id != row["project_id"]:
            raise ValueError("这场会已经归在别的项目，只能建在它所在的项目里")
        project_id = row["project_id"]
    elif not project_id:
        raise ValueError("要建在哪个项目？")
    project = connection.execute(
        "SELECT id, name FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if project is None:
        raise NotFoundError("项目不存在")
    rows = _same_name_rows(connection, context, row, hint) if hint else [row]

    reassigned: list[dict[str, Any]] = []
    for item in rows:
        if item["project_id"] is None:
            reassign_meeting(connection, item["id"], project_id, actor="user")
            last = last_reassignment(connection, item["id"])
            reassigned.append(
                {"meeting_id": item["id"], "event_id": last["event_id"] if last else None}
            )

    existing = next(
        (
            dict(requirement)
            for requirement in connection.execute(
                "SELECT id, title FROM requirements WHERE project_id=?", (project_id,)
            ).fetchall()
            if light_key(requirement["title"]) == light_key(text)
        ),
        None,
    )
    folder_attached: str | None = None
    folder_error: str | None = None
    if existing is not None:
        requirement_id = existing["id"]
        requirement_title = existing["title"]
    else:
        folders: list[str] = []
        if folder_path:
            try:
                _validate_folder_paths(connection, project_id, [folder_path])
            except ValueError as error:
                folder_error = str(error)
            else:
                folders = [folder_path]
        requirement_id = insert_requirement(
            connection,
            project_id=project_id,
            title=text,
            priority=REQUIREMENT_PRIORITY,
            folder_paths=folders,
        )
        requirement_title = text
        if folders:
            folder_attached = str(Path(folder_path or "").resolve(strict=False))
    linked = [
        item["id"]
        for item in rows
        if (item["project_id"] in (None, project_id))
        and link_meeting(connection, requirement_id, item["id"])
    ]

    undo_parts: list[dict[str, Any]] = []
    settled: set[str] = set()
    for name in [text, *([hint["name"]] if hint else [])]:
        key = light_key(name)
        if key and key not in settled:
            settled.add(key)
            undo_parts.append(
                settle_requirement_name(connection, project_id, name, "made", requirement_id)
            )
    if hint and hint["kind"] == "project":
        # 你把「像是新项目」的名字建成了需求：这个名字以后不再当新项目提示。
        undo_parts.append(settle_project_name(connection, [hint["name"]], "ignored"))
    event_id, at = record_event(
        connection,
        "name_made_requirement",
        meeting_id=meeting_id,
        payload={
            "requirement_id": requirement_id,
            "project_id": project_id,
            "title": requirement_title,
            "existing": existing is not None,
            "folder_attached": folder_attached,
            "linked_meeting_ids": linked,
            "reassigned": reassigned,
            "undo": merge_undo(*undo_parts),
        },
    )
    return {
        "requirement_id": requirement_id,
        "requirement_title": requirement_title,
        "project_id": project_id,
        "project_name": project["name"],
        "existing": existing is not None,
        "priority": REQUIREMENT_PRIORITY,
        "meetings_linked": len(linked),
        "meetings_assigned": len(reassigned),
        "meeting_ids": [item["id"] for item in rows],
        "folder_attached": folder_attached,
        "folder_error": folder_error,
        "event_id": event_id,
        "undo_until": undo_until(at),
    }


def _refuse_across_merges(connection: Any, requirement_id: str | None, *, since: str) -> None:
    """建成需求之后，那条需求被并走了、或者有需求并进了它：抛 ConflictError。只看建成需求之后的合并
    （链接到已有需求的，那条需求以前就并进来过的不算）。"""
    if not requirement_id:
        return
    gone = connection.execute(
        "SELECT title FROM requirement_merges WHERE requirement_id=?", (requirement_id,)
    ).fetchone()
    if gone is not None:
        into = merged_into(connection, requirement_id)
        where = f"已并入「{into['title']}」" if into else "已并入别的需求"
        raise ConflictError(f"「{gone['title']}」{where}，要撤销建成需求，先撤销那次合并")
    merged_in = connection.execute(
        """SELECT r.title FROM requirement_merges m JOIN requirements r ON r.id = m.into_requirement_id
            WHERE m.into_requirement_id=? AND julianday(m.merged_at) >= julianday(?)
            LIMIT 1""",
        (requirement_id, since),
    ).fetchone()
    if merged_in is not None:
        raise ConflictError(f"已有需求并入「{merged_in['title']}」，要撤销建成需求，先撤销那次合并")


def undo_name_as_requirement(connection: Any, meeting_id: str) -> dict[str, Any]:
    """10 分钟内撤销建成需求：解除关联；需求在那之后没有别的会、任务、文件夹时删掉，否则留着；
    改过归属的逐场撤销；名字和决定行恢复原样。调用方开事务。

    这期间那条需求被并进了别处、或者有别的需求并进了它（需求并需求，D7）时不撤销、什么都不改：解除关联、
    删需求会连带删掉合并带过来的原话和会，合并也就撤不回了。先撤销那次合并再撤销建成需求。"""
    latest = connection.execute(
        """SELECT id FROM events WHERE meeting_id=? AND event_type='name_made_requirement'
            ORDER BY id DESC LIMIT 1""",
        (meeting_id,),
    ).fetchone()
    if latest is None:
        raise NotFoundError("没有可以撤销的改动")
    event = load_undoable_event(
        connection, latest["id"], ("name_made_requirement",), "name_made_requirement_undone"
    )
    payload = event["payload"]
    requirement_id = payload.get("requirement_id")
    _refuse_across_merges(connection, requirement_id, since=event["at"])
    linked = [str(item) for item in payload.get("linked_meeting_ids") or []]
    if linked:
        connection.execute(
            f"""DELETE FROM requirement_meetings
                 WHERE requirement_id=? AND meeting_id IN ({", ".join("?" for _ in linked)})""",
            (requirement_id, *linked),
        )
    deleted = False
    if not payload.get("existing") and requirement_id:
        attached = payload.get("folder_attached")
        busy = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM requirement_meetings WHERE requirement_id=:id)
                 + (SELECT COUNT(*) FROM tasks WHERE requirement_id=:id)
                 + (SELECT COUNT(*) FROM requirement_folders
                     WHERE requirement_id=:id AND path IS NOT :attached) AS n""",
            {"id": requirement_id, "attached": attached},
        ).fetchone()["n"]
        if not busy:
            connection.execute(
                "DELETE FROM requirement_folders WHERE requirement_id=?", (requirement_id,)
            )
            deleted = bool(
                connection.execute(
                    "DELETE FROM requirements WHERE id=?", (requirement_id,)
                ).rowcount
            )
    restored_meetings = 0
    for item in payload.get("reassigned") or []:
        last = last_reassignment(connection, item.get("meeting_id"))
        if last is None or last["event_id"] != item.get("event_id"):
            continue
        try:
            undo_reassign(connection, item["meeting_id"])
        except ConflictError:
            continue
        restored_meetings += 1
    restore_settlement(connection, payload.get("undo") or {})
    record_event(
        connection,
        "name_made_requirement_undone",
        meeting_id=meeting_id,
        payload={"event_id": event["id"]},
    )
    return {
        "ok": True,
        "requirement_deleted": deleted,
        "meetings_restored": restored_meetings,
        "meeting_ids": [item.get("meeting_id") for item in payload.get("reassigned") or []],
    }
