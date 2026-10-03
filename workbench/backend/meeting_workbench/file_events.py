"""文件新增、修改、不见的流水（第四期 4a）：读的一边。

material_files 上的两个触发器写 material_file_events（规则写在 db.py 里：第一次整轮不记、只记
normal 区和 key、pages、numbers 包、changed 同一天一行、exFAT 整刻钟不记；day 是北京日历的日期）。这里是
时间线（4c）和产出建议（4e）共用的一处规则：
- classify：给每条 added 分类。moved 挪过来的、copied 复制来的、back 又出现的、pending 还在等内容
  标识的，其余是真正的新增；
- recent_added：给产出建议，只返回真正的新增；
- day_groups：给时间线，按天、根目录、文件夹分组，数新增和修改，带最多 3 个文件名；
- prune：流水留 400 天，每天一次的清理调它，每个事务最多 5,000 行。

大小一律用 IS 比：key、pages、numbers 包的 size 是空的，用 = 永远配不上。时间线有语句数上限，
classify 是一条语句（事件超过 500 条时每 500 条一条），day_groups、recent_added 各再加一条。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .material_rules import CONTENT_EXTS

ADDED = "added"
CHANGED = "changed"
GONE = "gone"
MOVED = "moved"
COPIED = "copied"
BACK = "back"
PENDING = "pending"

# 挪位置配对：前后 2 天内的 gone
MOVE_WINDOW_DAYS = 2
# 该有内容标识的文件最多等 60 分钟，之后按文件名加大小判断
PENDING_WAIT = timedelta(minutes=60)
# 时间线每组最多列几个文件名
GROUP_NAMES = 3
KEEP_DAYS = 400
PRUNE_BATCH = 5000
# 一条语句里 IN 的个数上限（SQLite 的参数个数有上限）
_IN_CHUNK = 500

# 内容循环只给 normal 区的正文、PDF、图片、音视频算标识；key、pages、numbers 记 unsupported
_KEYED_EXTS_SQL = ", ".join(f"'{ext}'" for ext in sorted(CONTENT_EXTS))
_WINDOW = (
    f"BETWEEN date(ev.day, '-{MOVE_WINDOW_DAYS} days') AND date(ev.day, '+{MOVE_WINDOW_DAYS} days')"
)
# 同一个项目里别的文件行的 gone。按大小比的两条走 (root_id, size, mtime_ns) 索引（+g.day 让它不去
# 选按天的索引）；按内容标识比的先按标识找文件行，再按 file_id 找它的 gone。
_GONE_SAME_SIZE = f"""
                SELECT 1 FROM project_material_roots gr
                  JOIN material_file_events g ON g.root_id = gr.id
                 WHERE gr.project_id = ev.project_id AND g.size IS ev.size AND g.kind = 'gone'
                   AND g.file_id != ev.file_id AND +g.day {_WINDOW}"""
_LIVE_AT_EVENT = "(o.gone_at IS NULL OR julianday(o.gone_at) > julianday(ev.at))"
# 按顺序判断，第一条成立的就是它的类别。ev.key 是文件现在的内容标识（大小或修改时间变了、还没
# 重算的旧标识不算）；ev.keyed 是这份文件该不该有标识。
_CLASSIFY_SQL = f"""
WITH ev AS (
    SELECT e.id, e.file_id, e.size, e.mtime_ns, e.day, e.at, r.project_id, f.name, f.stem_key,
           CASE WHEN f.content_key IS NOT NULL AND f.content_size IS f.size
                     AND f.content_mtime_ns IS f.mtime_ns THEN f.content_key END AS key,
           (f.zone = 'normal' AND f.ext IN ({_KEYED_EXTS_SQL}) AND f.content_error IS NULL) AS keyed
      FROM material_file_events e
      JOIN project_material_roots r ON r.id = e.root_id
      LEFT JOIN material_files f ON f.id = e.file_id
     WHERE e.id IN ({{marks}}) AND e.kind = 'added'
)
SELECT ev.id,
       CASE
         WHEN EXISTS (SELECT 1 FROM material_file_events b
                       WHERE b.file_id = ev.file_id AND b.kind = 'gone' AND b.id < ev.id)
           THEN '{BACK}'
         WHEN ev.mtime_ns IS NOT NULL AND EXISTS ({_GONE_SAME_SIZE}
                   AND g.mtime_ns = ev.mtime_ns)
           THEN '{MOVED}'
         WHEN ev.key IS NOT NULL AND EXISTS (
                SELECT 1 FROM material_files gf
                  JOIN project_material_roots gr ON gr.id = gf.root_id
                  JOIN material_file_events g ON g.file_id = gf.id
                 WHERE gf.content_key = ev.key AND gf.id != ev.file_id
                   AND gr.project_id = ev.project_id AND g.kind = 'gone'
                   AND g.content_key = ev.key AND g.day {_WINDOW})
           THEN '{MOVED}'
         WHEN ev.key IS NOT NULL AND EXISTS (
                SELECT 1 FROM material_files o
                 WHERE o.content_key = ev.key AND o.id < ev.file_id AND {_LIVE_AT_EVENT})
           THEN '{COPIED}'
         WHEN ev.key IS NOT NULL THEN '{ADDED}'
         WHEN ev.keyed AND julianday(?) - julianday(ev.at) < ? THEN '{PENDING}'
         WHEN EXISTS ({_GONE_SAME_SIZE}
                   AND EXISTS (SELECT 1 FROM material_files gf
                                WHERE gf.id = g.file_id AND gf.name = ev.name))
           THEN '{MOVED}'
         WHEN ev.stem_key != '' AND EXISTS (
                SELECT 1 FROM material_files o
                  JOIN project_material_roots orr ON orr.id = o.root_id
                 WHERE o.stem_key = ev.stem_key AND o.name = ev.name AND o.size IS ev.size
                   AND o.id < ev.file_id AND orr.project_id = ev.project_id AND {_LIVE_AT_EVENT})
           THEN '{COPIED}'
         ELSE '{ADDED}'
       END AS label
  FROM ev"""


def _chunks(values: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), _IN_CHUNK):
        yield values[start : start + _IN_CHUNK]


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def at_text(value: datetime | str) -> str:
    """转成触发器写 at 的格式（UTC，毫秒，结尾 Z），比较字符串才对得上。"""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _day_text(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def _name(rel_path: str) -> str:
    return str(rel_path).rpartition("/")[2]


def classify(
    conn: sqlite3.Connection, events: Iterable[Mapping[str, Any]], *, now: datetime | None = None
) -> dict[int, str]:
    """给每条 added 分类，返回 {事件 id: 类别}；changed、gone 原样回它们的 kind。

    按顺序判断：
    - back：同一个文件行之前有过 gone（不见了、又出现了）；
    - moved：同一个项目的根目录里，前后 2 天内有别的文件行的 gone，大小相同（IS）且修改时间相同，
      或者内容标识相同；
    - copied：这个内容标识之前已经有活文件（别的、更早建的文件行，那一刻还没不见）；
    - pending：该有内容标识的文件还没算出来，最多等 60 分钟；
    - 算不出标识的文件（包、压缩包这类）和等过 60 分钟的，按文件名加大小（IS）判断 moved 和
      copied（只看同一个项目）；
    - 其余是 added（真正的新增）。
    时间线不显示 moved 和 back，copied、pending 当新增；产出建议把 moved、copied、back 都排除，
    pending 等它分出来。
    """
    moment = at_text(now or datetime.now(UTC))
    labels: dict[int, str] = {}
    added: list[int] = []
    for event in events:
        labels[int(event["id"])] = str(event["kind"])
        if event["kind"] == ADDED:
            added.append(int(event["id"]))
    wait_days = PENDING_WAIT / timedelta(days=1)
    for part in _chunks(sorted(added)):
        for row in conn.execute(
            _CLASSIFY_SQL.format(marks=_marks(part)), (*part, moment, wait_days)
        ).fetchall():
            labels[int(row[0])] = str(row[1])
    return labels


def recent_added(
    conn: sqlite3.Connection,
    root_ids: Iterable[int],
    since: datetime | str,
    until: datetime | str | None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """给产出建议（L4）：这些根目录里 since <= at < until（until 为空时不限）的 added，只返回
    classify 判成真正新增的，按时间先后。挪过来的、复制来的、又出现的都不算，还在等内容标识的
    等它分出来。"""
    ids = sorted({int(root_id) for root_id in root_ids})
    if not ids:
        return []
    bounds = [at_text(since)]
    until_sql = ""
    if until is not None:
        bounds.append(at_text(until))
        until_sql = " AND at < ?"
    events: list[dict[str, Any]] = []
    for part in _chunks(ids):
        events.extend(
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM material_file_events
                     WHERE root_id IN ({_marks(part)}) AND at >= ?{until_sql} AND kind = 'added'""",
                (*part, *bounds),
            ).fetchall()
        )
    labels = classify(conn, events, now=now)
    kept = [event for event in events if labels[int(event["id"])] == ADDED]
    kept.sort(key=lambda event: (event["at"], event["id"]))
    return kept


def day_groups(
    conn: sqlite3.Connection,
    project_id: str,
    since: date | str,
    *,
    before: date | str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """给时间线（4c）：这个项目 day >= since（给了 before 时还要 day < before）的文件动静，按
    (day, root_id, dir_rel) 分组。

    每组：{"day", "root_id", "dir_rel", "added", "changed", "names", "file_ids", "at"}。
    added 数真正的新增、复制来的和还在等内容标识的；挪过来的、又出现的不列，gone 永远不列；同一天
    新增过的文件不再算修改。names 最多 3 个文件名（新增的在前，新的在前），file_ids 和它一一对应；
    at 是这组最后一次动静。新的天在前，同一天里最近有动静的组在前。
    """
    params: list[Any] = [project_id, _day_text(since)]
    before_sql = ""
    if before is not None:
        params.append(_day_text(before))
        before_sql = " AND e.day < ?"
    events = [
        dict(row)
        for row in conn.execute(
            f"""SELECT e.* FROM project_material_roots r
                  JOIN material_file_events e ON e.root_id = r.id
                 WHERE r.project_id = ? AND e.day >= ?{before_sql}
                   AND e.kind IN ('added', 'changed')""",
            params,
        ).fetchall()
    ]
    labels = classify(conn, events, now=now)
    events.sort(key=lambda event: (event["at"], event["id"]), reverse=True)
    shown_as_added = (ADDED, COPIED, PENDING)
    added_days = {
        (int(event["file_id"]), event["day"])
        for event in events
        if labels[int(event["id"])] in shown_as_added
    }
    groups: dict[tuple[str, int, str], dict[str, Any]] = {}
    changed_names: dict[tuple[str, int, str], list[tuple[str, int]]] = {}
    counted: set[tuple[int, str, str]] = set()
    for event in events:
        file_id = int(event["file_id"])
        label = labels[int(event["id"])]
        if label in shown_as_added:
            kind = ADDED
        elif label == CHANGED and (file_id, event["day"]) not in added_days:
            kind = CHANGED
        else:
            continue
        if (file_id, event["day"], kind) in counted:
            continue
        counted.add((file_id, event["day"], kind))
        key = (str(event["day"]), int(event["root_id"]), str(event["dir_rel"]))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "day": key[0],
                "root_id": key[1],
                "dir_rel": key[2],
                "added": 0,
                "changed": 0,
                "names": [],
                "file_ids": [],
                "at": event["at"],
            }
        group[kind] += 1
        if kind == CHANGED:
            changed_names.setdefault(key, []).append((_name(event["rel_path"]), file_id))
        elif len(group["names"]) < GROUP_NAMES:
            group["names"].append(_name(event["rel_path"]))
            group["file_ids"].append(file_id)
    for key, group in groups.items():
        for name, file_id in changed_names.get(key, [])[: GROUP_NAMES - len(group["names"])]:
            group["names"].append(name)
            group["file_ids"].append(file_id)
    return sorted(groups.values(), key=lambda group: (group["day"], group["at"]), reverse=True)


def prune(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    keep_days: int = KEEP_DAYS,
    limit: int = PRUNE_BATCH,
) -> int:
    """删掉 day 早于 keep_days 天前的流水，一次最多 limit 行。调用方每个事务调一次，删满了就再开
    一个事务接着删。today 是本机日期。返回删了几行。"""
    cutoff = ((today or datetime.now().astimezone().date()) - timedelta(days=keep_days)).isoformat()
    return conn.execute(
        """DELETE FROM material_file_events
            WHERE id IN (SELECT id FROM material_file_events WHERE day < ? ORDER BY id LIMIT ?)""",
        (cutoff, limit),
    ).rowcount
