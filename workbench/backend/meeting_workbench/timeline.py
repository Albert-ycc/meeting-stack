"""第四期 4c：项目时间线（GET /api/projects/{project_id}/timeline）。

按本机的「有动静的天」翻页：一页最多 days 天（1 到 31），新的天在前，同一天里按时间先后；next_before 是
下一页的起点。只读，最多 10 条语句（文件动静一页超过 500 条时 classify 每 500 条多一条）：
1. 项目、links_since、根目录和它们的收文件名状态；
2. 项目的会（本地日期在 Python 里按 graph.local_day 算）；
3. 任务的确认、完成和交付物（全项目，天在 SQL 里按本机时区算）；
4. 文件有动静的天（流水和记录开始前按修改时间的，只取这一页要的天数加一）；
5. 这一页的会的决议（最多 8 条，带「后来改了」）；
6. 这一页的会的录音；
7. [决议] 这一页的会关联的需求和项目的需求（放到需求 ▾ 用）；
8、9. 这一页的文件分组（file_events.day_groups 和它的 classify）；
10. 这一页记录开始前的文件（按修改时间）。

来源和规则（规格第 5 节「时间线的接口」）：
- 会议：日期、时间按 graph.local_day 的规则；最多 8 条还在的决议，每条带「后来改了」。
- 任务确认：confirmed 事件、以「→ confirmed」结尾的 status_changed、手动建的任务的 created_at，三者取最早；
  现在仍是已确认、进行中或完成的才列。任务完成：最后一次以「→ done」结尾的 status_changed，现在仍是完成的
  才列。同一天同一种有 3 条以上的合成一行。
- 交付物：「『任务名』的产出：文件名」，名字取交付物标题，或 deliverable_files.rel_path、url 的最后一段。
- 文件：file_events.day_groups（挪过来和回来了的不列、复制来的当新增、gone 永远不列），一天最多 8 组。
- 记录开始前：links_since（或根目录挂上的时间，取较晚的）之前按文件现在的修改时间归到那一天，只看分界
  之前 62 天；只记 zone 为 normal 的，和 package 里的 key、pages、numbers。
不列：需求状态的变化、AI 任务草稿、纪要改动、词条改动、挪动和删除。界面上不出现路径：文件夹只给最后一段。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from . import file_events
from .decisions import effective_requirement, pair_state_line, project_names
from .relation_read import AUDIO_ID_SQL

KINDS = ("all", "decisions", "tasks", "files")
DAYS_DEFAULT = 7
DAYS_MAX = 31
MEETING_DECISIONS = 8
TASKS_MERGE = 3
DAY_GROUPS = 8
PRELOG_DAYS = 62
PRELOG_NAMES = 3
WEEKDAYS = "一二三四五六日"

NO_ROOTS = "这个项目还没挂材料文件夹，时间线里只有会议和任务"
ROOT_OFFLINE = "资料盘未连接，插上后接着记文件的变化"
FIRST_PASS = "正在第一次收文件名，收完后开始记文件的新增和修改"
ATTACH_ROOT = {"kind": "attach_root", "label": "挂上文件夹"}
TIMELINE_SENTENCES = (NO_ROOTS, ROOT_OFFLINE, FIRST_PASS)

_FILE_ZONES = "(f.zone = 'normal' OR (f.zone = 'package' AND f.ext IN ('key', 'pages', 'numbers')))"
# 任务的确认时间（三种来源取最早，'~' 排在所有时间后面，当作没有）和最后一次完成时间
_TASKS_SQL = """
WITH tt AS (
    SELECT t.id, t.title, t.status,
           min(COALESCE((SELECT MIN(e.created_at) FROM task_events e
                          WHERE e.task_id = t.id
                            AND (e.kind = 'confirmed' OR (e.kind = 'status_changed' AND e.body LIKE '%→ confirmed'))),
                        '~'),
               CASE WHEN t.origin = 'manual' THEN t.created_at ELSE '~' END) AS confirmed_at,
           (SELECT MAX(e.created_at) FROM task_events e
             WHERE e.task_id = t.id AND e.kind = 'status_changed' AND e.body LIKE '%→ done') AS done_at
      FROM tasks t
     WHERE t.project_id = :pid AND t.status IN ('confirmed', 'in_progress', 'done')
)
SELECT 'confirmed' AS event, id, title, confirmed_at AS at, date(confirmed_at, 'localtime') AS day,
       NULL AS task_id, NULL AS task_title, NULL AS url, NULL AS rel_path
  FROM tt WHERE confirmed_at != '~'
UNION ALL
SELECT 'done', id, title, done_at, date(done_at, 'localtime'), NULL, NULL, NULL, NULL
  FROM tt WHERE status = 'done' AND done_at IS NOT NULL
UNION ALL
SELECT 'deliverable', CAST(d.id AS TEXT), d.title, d.created_at, date(d.created_at, 'localtime'), t.id, t.title,
       d.url, df.rel_path
  FROM deliverables d JOIN tasks t ON t.id = d.task_id
  LEFT JOIN deliverable_files df ON df.deliverable_id = d.id
 WHERE t.project_id = :pid"""


class TimelineNotFound(LookupError):
    pass


# ---------------------------------------------------------------------- 日期


def day_label(day: date, today: date) -> str:
    """「今天」「昨天」「9月24日 周三」，不是今年的「2025年12月30日 周二」。"""
    if day == today:
        return "今天"
    if day == today - timedelta(days=1):
        return "昨天"
    weekday = f"周{WEEKDAYS[day.weekday()]}"
    if day.year == today.year:
        return f"{day.month}月{day.day}日 {weekday}"
    return f"{day.year}年{day.month}月{day.day}日 {weekday}"


def _meeting_moment(recording_date: str | None, created_at: str | None) -> datetime | None:
    """会议的时刻（带时区）；只有日期时为 None。规则和 graph.local_day 一样。"""
    for raw, naive_is_utc in ((recording_date, False), (created_at, True)):
        if not raw:
            continue
        text = str(raw).strip()
        if len(text) <= 10:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC) if naive_is_utc else value.astimezone()
        return value
    return None


def _at(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment is not None else None


def _clock(moment: datetime | None) -> str | None:
    return moment.astimezone().strftime("%H:%M") if moment is not None else None


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _local_day(value: str | None) -> date | None:
    moment = _parse_utc(value)
    return moment.astimezone().date() if moment is not None else None


def _midnight_ns(day: date) -> int:
    return int(datetime.combine(day, time(0, 0)).astimezone().timestamp()) * 1_000_000_000


def _last_segment(value: str | None) -> str:
    return str(value or "").rstrip("/").rpartition("/")[2]


# ---------------------------------------------------------------------- 主函数


def project_timeline(
    connection: Any,
    project_id: str,
    *,
    before: date | None = None,
    days: int = DAYS_DEFAULT,
    kind: str = "all",
    now: datetime | None = None,
    worker: Any = None,
    settings: Any = None,
) -> dict[str, Any]:
    from .graph import local_day

    if kind not in KINDS:
        raise ValueError(f"不认识的筛选：{kind}")
    days = max(1, min(DAYS_MAX, int(days)))
    moment = now or datetime.now(UTC)
    today = moment.astimezone().date()
    stop = before or today + timedelta(days=1)
    want_meetings = kind in ("all", "decisions")
    want_tasks = kind in ("all", "tasks")
    want_files = kind in ("all", "files")

    project = connection.execute(
        """SELECT p.id, p.name, p.also_names,
                  (SELECT value FROM app_state WHERE key = 'links_since') AS links_since,
                  (SELECT json_group_array(json_object('id', r.id, 'created_at', r.created_at, 'state', s.state,
                                                       'last_full_at', s.last_full_at))
                     FROM project_material_roots r LEFT JOIN material_index_state s ON s.root_id = r.id
                    WHERE r.project_id = p.id) AS roots_json
             FROM projects p WHERE p.id = ?""",
        (project_id,),
    ).fetchone()
    if project is None:
        raise TimelineNotFound("项目不存在")
    roots = [root for root in json.loads(project["roots_json"] or "[]") if isinstance(root, dict)]
    since_day = _local_day(project["links_since"])
    # 每个根目录记录开始的那一天：links_since 和挂上的时间取较晚的
    boundaries: dict[int, date] = {}
    for root in roots:
        attached = _local_day(root.get("created_at"))
        candidates = [day for day in (since_day, attached) if day is not None]
        if candidates:
            boundaries[int(root["id"])] = max(candidates)
    file_log_since = min(boundaries.values()) if boundaries else None

    # ---------------------------------------------------------------- 候选的天
    meetings: list[dict[str, Any]] = []
    meeting_days: dict[date, list[dict[str, Any]]] = {}
    if want_meetings:
        for row in connection.execute(
            """SELECT m.id, m.title, m.recording_date, m.created_at, m.duration_ms, s.pair_state,
                      (SELECT COUNT(*) FROM decisions d WHERE d.meeting_id = m.id AND d.gone_at IS NULL) AS decision_count
                 FROM meetings m LEFT JOIN decision_scan s ON s.meeting_id = m.id
                WHERE m.project_id = ?""",
            (project_id,),
        ).fetchall():
            item = dict(row)
            item["day"] = local_day(item["recording_date"], item["created_at"])
            meetings.append(item)
            if kind == "decisions" and not item["decision_count"]:
                continue
            meeting_days.setdefault(item["day"], []).append(item)
    task_rows: list[dict[str, Any]] = []
    if want_tasks:
        task_rows = [
            dict(row) for row in connection.execute(_TASKS_SQL, {"pid": project_id}).fetchall() if row["day"]
        ]
    file_days: list[date] = []
    prelog_windows = _prelog_windows(roots, boundaries)
    if want_files and roots:
        file_days = _file_days(connection, project_id, prelog_windows, stop, days + 1)

    candidates = {day for day in meeting_days if day < stop}
    candidates.update(day for day in (date.fromisoformat(row["day"]) for row in task_rows) if day < stop)
    candidates.update(file_days)
    ordered = sorted(candidates, reverse=True)
    page = ordered[:days]
    next_before = page[-1].isoformat() if len(ordered) > days else None
    start = page[-1] if page else stop

    # ---------------------------------------------------------------- 这一页的内容
    items: dict[date, list[dict[str, Any]]] = {day: [] for day in page}
    more_dirs: dict[date, int] = {day: 0 for day in page}
    page_meetings = [meeting for day in page for meeting in meeting_days.get(day, [])]
    linked: dict[str, list[dict[str, Any]]] = {}
    requirements: list[dict[str, Any]] = []
    if page_meetings:
        ids = [meeting["id"] for meeting in page_meetings]
        marks = ", ".join("?" for _ in ids)
        decisions = _page_decisions(connection, marks, ids)
        audio = {
            row["id"]: row["audio_id"]
            for row in connection.execute(
                f"SELECT m.id, {AUDIO_ID_SQL.format(meeting='m.id')} AS audio_id FROM meetings m WHERE m.id IN ({marks})",
                ids,
            ).fetchall()
        }
        if kind == "decisions":
            for row in connection.execute(
                f"""SELECT NULL AS meeting_id, r.id, r.title, r.created_at FROM requirements r WHERE r.project_id = ?
                    UNION ALL
                    SELECT rm.meeting_id, r.id, r.title, r.created_at FROM requirement_meetings rm
                      JOIN requirements r ON r.id = rm.requirement_id
                     WHERE rm.meeting_id IN ({marks})
                    ORDER BY 4, 2""",
                [project_id, *ids],
            ).fetchall():
                entry = {"id": row["id"], "title": row["title"]}
                if row["meeting_id"] is None:
                    requirements.append(entry)
                else:
                    linked.setdefault(row["meeting_id"], []).append(entry)
        excluded = project_names(project["name"], project["also_names"])
        for meeting in page_meetings:
            _meeting_items(items[meeting["day"]], meeting, decisions.get(meeting["id"], []), audio.get(meeting["id"]),
                           kind=kind, linked=linked.get(meeting["id"], []), project_id=project_id, excluded=excluded)
    if want_tasks:
        _task_items(items, [row for row in task_rows if start <= date.fromisoformat(row["day"]) < stop])
    if want_files and roots and page:
        groups = file_events.day_groups(connection, project_id, start, before=stop, now=moment)
        prelog = _prelog_groups(connection, prelog_windows, start, stop)
        _file_items(items, more_dirs, groups, prelog)

    days_out = []
    for day in page:
        entries = items[day]
        if not entries:
            continue
        entries.sort(key=lambda entry: (entry.get("_sort") or "", entry.get("_seq", 0)))
        for entry in entries:
            entry.pop("_sort", None)
            entry.pop("_seq", None)
        days_out.append({"day": day.isoformat(), "label": day_label(day, today), "items": entries,
                         "more_dirs": more_dirs[day]})
    return {
        "kind": kind,
        "days": days_out,
        "requirements": requirements if kind == "decisions" else [],
        "next_before": next_before,
        "file_log_since": file_log_since.isoformat() if file_log_since else None,
        "state": _state(kind, roots, meetings, worker, settings),
    }


# ---------------------------------------------------------------------- 各种条目


def _page_decisions(connection: Any, marks: str, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """这一页的会还在的决议，每条带最新一条 shown 的「后来改了」（另一头也还在）。"""
    result: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        f"""SELECT d.id, d.meeting_id, d.text, d.detail, d.start_ms, d.end_ms, d.placement, d.requirement_id,
                   rq.project_id AS requirement_project, rq.title AS requirement_title,
                   (SELECT json_object('rec', lm.recording_date, 'created', lm.created_at, 'text', l.text)
                      FROM relations r
                      JOIN decisions l ON l.id = r.to_decision_id AND l.gone_at IS NULL
                      JOIN meetings lm ON lm.id = l.meeting_id
                     WHERE r.decision_id = d.id AND r.kind = 'later_changed' AND r.status = 'shown'
                     ORDER BY COALESCE(lm.recording_date, lm.created_at) DESC, l.start_ms DESC LIMIT 1) AS later_json
              FROM decisions d LEFT JOIN requirements rq ON rq.id = d.requirement_id
             WHERE d.meeting_id IN ({marks}) AND d.gone_at IS NULL
             ORDER BY d.meeting_id, d.ordinal, d.id""",
        list(ids),
    ).fetchall():
        result.setdefault(row["meeting_id"], []).append(dict(row))
    return result


def _later(raw: str | None) -> dict[str, Any] | None:
    from .graph import local_day

    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return {"date": local_day(data.get("rec"), data.get("created")).isoformat(), "text": data.get("text") or ""}


def _meeting_items(
    out: list[dict[str, Any]],
    meeting: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    audio_id: Any,
    *,
    kind: str,
    linked: Sequence[Mapping[str, Any]],
    project_id: str,
    excluded: Sequence[str],
) -> None:
    moment = _meeting_moment(meeting["recording_date"], meeting["created_at"])
    at, clock = _at(moment), _clock(moment)
    audio_url = f"/api/media/{audio_id}" if audio_id else None
    if kind == "decisions":
        titles = {item["id"]: item["title"] for item in linked}
        for index, row in enumerate(decisions):
            chosen, how = effective_requirement(row, linked, project_id, excluded)
            title = titles.get(chosen) or (row["requirement_title"] if chosen == row["requirement_id"] else None)
            out.append(
                {
                    "type": "decision",
                    "at": at,
                    "time": clock,
                    "decision": {"id": row["id"], "text": row["text"], "start_ms": row["start_ms"],
                                 "end_ms": row["end_ms"], "detail": row["detail"] or "",
                                 "later": _later(row["later_json"])},
                    "meeting": {"id": meeting["id"], "title": meeting["title"], "audio_url": audio_url},
                    "requirement": {"id": chosen, "title": title or "", "how": how} if chosen else None,
                    "how": how,
                    "linked_requirement_ids": [item["id"] for item in linked],
                    "_sort": at or "",
                    "_seq": index,
                }
            )
        return
    duration = meeting["duration_ms"]
    out.append(
        {
            "type": "meeting",
            "at": at,
            "time": clock,
            "meeting": {"id": meeting["id"], "title": meeting["title"],
                        "duration_sec": int(duration // 1000) if duration else None, "audio_url": audio_url},
            "decisions": [
                {"id": row["id"], "text": row["text"], "start_ms": row["start_ms"], "later": _later(row["later_json"])}
                for row in decisions[:MEETING_DECISIONS]
            ],
            "decisions_more": max(0, len(decisions) - MEETING_DECISIONS),
            "_sort": at or "",
        }
    )


def _deliverable_name(row: Mapping[str, Any]) -> str:
    return (row["title"] or "").strip() or _last_segment(row["rel_path"]) or _last_segment(row["url"])


def _task_items(items: dict[date, list[dict[str, Any]]], rows: Sequence[Mapping[str, Any]]) -> None:
    by_event: dict[tuple[date, str], list[Mapping[str, Any]]] = {}
    for row in sorted(rows, key=lambda row: (row["at"] or "", row["id"])):
        day = date.fromisoformat(row["day"])
        if day not in items:
            continue
        if row["event"] == "deliverable":
            moment = _parse_utc(row["at"])
            items[day].append(
                {
                    "type": "deliverable",
                    "at": _at(moment),
                    "time": _clock(moment),
                    "task": {"id": row["task_id"], "title": row["task_title"]},
                    "deliverable": {"id": int(row["id"]), "name": _deliverable_name(row)},
                    "_sort": _at(moment) or "",
                }
            )
            continue
        by_event.setdefault((day, row["event"]), []).append(row)
    for (day, event), group in by_event.items():
        # 同一天同一种有 3 条以上的合成一行
        chunks = [group] if len(group) >= TASKS_MERGE else [[row] for row in group]
        for chunk in chunks:
            moment = _parse_utc(chunk[0]["at"])
            shown = chunk[:TASKS_MERGE]
            items[day].append(
                {
                    "type": "tasks",
                    "event": event,
                    "at": _at(moment),
                    "time": _clock(moment),
                    "tasks": [{"id": row["id"], "title": row["title"]} for row in shown],
                    "more": len(chunk) - len(shown),
                    "_sort": _at(moment) or "",
                }
            )


# ---------------------------------------------------------------------- 文件


def _prelog_windows(roots: Sequence[Mapping[str, Any]], boundaries: Mapping[int, date]) -> list[tuple[int, date, date]]:
    """每个根目录记录开始前的窗口 (root_id, 起, 止)：分界之前 62 天。"""
    return [
        (root_id, boundary - timedelta(days=PRELOG_DAYS), boundary)
        for root_id, boundary in sorted(boundaries.items())
        if any(int(root["id"]) == root_id for root in roots)
    ]


def _window_sql(windows: Sequence[tuple[int, date, date]], start: date | None, stop: date) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    for root_id, low, high in windows:
        low = max(low, start) if start else low
        high = min(high, stop)
        if low >= high:
            continue
        parts.append("(f.root_id = ? AND f.mtime_ns >= ? AND f.mtime_ns < ?)")
        params += [root_id, _midnight_ns(low), _midnight_ns(high)]
    return " OR ".join(parts), params


def _file_days(
    connection: Any, project_id: str, windows: Sequence[tuple[int, date, date]], stop: date, limit: int
) -> list[date]:
    """有文件动静的天（新的在前，最多 limit 天）：流水里的 added、changed，和记录开始前按修改时间的。"""
    prelog, prelog_params = _window_sql(windows, None, stop)
    union = ""
    if prelog:
        union = f"""UNION SELECT date(f.mtime_ns / 1000000000, 'unixepoch', 'localtime') AS day
                      FROM material_files f
                     WHERE ({prelog}) AND f.gone_at IS NULL AND {_FILE_ZONES}"""
    rows = connection.execute(
        f"""SELECT day FROM (
                SELECT e.day AS day FROM project_material_roots r
                  JOIN material_file_events e ON e.root_id = r.id
                 WHERE r.project_id = ? AND e.kind IN ('added', 'changed') AND e.day < ?
                {union})
             WHERE day < ? GROUP BY day ORDER BY day DESC LIMIT ?""",
        [project_id, stop.isoformat(), *prelog_params, stop.isoformat(), limit],
    ).fetchall()
    return [date.fromisoformat(row["day"]) for row in rows if row["day"]]


def _prelog_groups(
    connection: Any, windows: Sequence[tuple[int, date, date]], start: date, stop: date
) -> list[dict[str, Any]]:
    where, params = _window_sql(windows, start, stop)
    if not where:
        return []
    groups: dict[tuple[date, int, str], dict[str, Any]] = {}
    for row in connection.execute(
        f"""SELECT f.id, f.root_id, f.dir_rel, f.name, f.mtime_ns FROM material_files f
             WHERE ({where}) AND f.gone_at IS NULL AND {_FILE_ZONES}
             ORDER BY f.mtime_ns DESC, f.id""",
        params,
    ).fetchall():
        moment = datetime.fromtimestamp(int(row["mtime_ns"]) / 1_000_000_000, UTC)
        key = (moment.astimezone().date(), int(row["root_id"]), str(row["dir_rel"]))
        group = groups.setdefault(
            key, {"day": key[0], "root_id": key[1], "dir_rel": key[2], "count": 0, "names": [], "at": moment}
        )
        group["count"] += 1
        if len(group["names"]) < PRELOG_NAMES:
            group["names"].append(row["name"])
    return list(groups.values())


def _file_items(
    items: dict[date, list[dict[str, Any]]],
    more_dirs: dict[date, int],
    groups: Sequence[Mapping[str, Any]],
    prelog: Sequence[Mapping[str, Any]],
) -> None:
    """一天最多 8 组：新增和修改多的在前，其余「另有 N 个文件夹有变化」。先按天挑出 8 组再拼条目。"""
    per_day: dict[date, list[tuple[int, bool, Mapping[str, Any]]]] = {}
    for group in groups:
        day = date.fromisoformat(str(group["day"]))
        if day in items:
            per_day.setdefault(day, []).append((int(group["added"]) + int(group["changed"]), False, group))
    for group in prelog:
        if group["day"] in items:
            per_day.setdefault(group["day"], []).append((int(group["count"]), True, group))
    for day, entries in per_day.items():
        entries.sort(key=lambda entry: -entry[0])
        more_dirs[day] = max(0, len(entries) - DAY_GROUPS)
        for _weight, is_prelog, group in entries[:DAY_GROUPS]:
            moment = group["at"] if is_prelog else _parse_utc(group["at"])
            item: dict[str, Any] = {
                "type": "files",
                "at": _at(moment),
                "time": _clock(moment),
                "root_id": group["root_id"],
                "folder": _last_segment(group["dir_rel"]),
                "added": 0 if is_prelog else group["added"],
                "changed": 0 if is_prelog else group["changed"],
                "names": list(group["names"]),
                "prelog": is_prelog,
                "_sort": _at(moment) or "",
            }
            if is_prelog:
                item["count"] = group["count"]
            items[day].append(item)


# ---------------------------------------------------------------------- 状态


def _state(
    kind: str,
    roots: Sequence[Mapping[str, Any]],
    meetings: Sequence[Mapping[str, Any]],
    worker: Any,
    settings: Any,
) -> dict[str, Any]:
    """{kind, reason, text, action}，和别的接口的状态对象同一个形状。［决议］用决议对比的状态句。"""
    if kind == "decisions":
        line = pair_state_line(worker, settings, [meeting["pair_state"] for meeting in meetings])
        return {"reason": None, **line}
    if kind == "tasks":
        return {"kind": "ok", "reason": None, "text": None, "action": None}
    if not roots:
        return {"kind": "stopped", "reason": "no_roots", "text": NO_ROOTS, "action": dict(ATTACH_ROOT)}
    if any(root.get("state") == "offline" for root in roots):
        return {"kind": "waiting", "reason": "offline", "text": ROOT_OFFLINE, "action": None}
    if any(not root.get("last_full_at") for root in roots):
        return {"kind": "waiting", "reason": "first_pass", "text": FIRST_PASS, "action": None}
    return {"kind": "ok", "reason": None, "text": None, "action": None}
