"""会上提到文件名（第二期 2d）：把逐字稿和本项目文件夹里的文件名比对，连成「会上提到」。

- 要比对的会：已归项目，并且改过稿（dirty 不为 0）、还没比对过、或 stems_sig 变了（项目各根目录
  的 stems_rev、项目名和也叫、根目录位置、词条的最后修改时间合起来的摘要）。每轮最多 20 场或 5 秒。
- 只和这场会所在项目的根目录里、还在的、zone 是 normal 或 package 的文件比。
- 逐字稿每段按 stem_key 同一套规则折叠，一遍扫完：最长优先、不重叠；落在更长的项目名、也叫、词条里的
  出现不算（它们作为「挡板」一起扫）；字母词前后不能是字母。次数只数逐字稿；只在纪要里写到的，词干要
  3 字以上才连，source=minutes。
- 一个词干对应多份文件时一场会只连一份：说出了带版本的全名就连那份；否则连修改时间不晚于这场会的
  最新一份；都晚于这场会就连最早的一份。你手动换过的（picked）不再自动改；文件不在了先按内容标识
  在同名文件里找回（3a），同名文件的标识还没算完就原样保留，找不回才换。
- rejected（不是这份文件）永远不会被改回 active，只挡同一场会、同一个项目。
- 通用词干（被本项目一半以上、至少 4 场会提到）照样存，读的时候标 generic。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, time as day_time
from typing import Any, Callable

from .db import Database, utc_now
from .file_stems import STEM_NO, STEM_TWICE, STEM_YES, stem_usability
from .material_content import key_file_now
from .material_index import MATCH_ZONES, STATE_MISSING, STATE_OFFLINE
from .project_names import also_entries
from .project_profile import MAX_ANCHORS, light_key, norm_key
from .text_scan import FormScanner

MATCH_VERSION = "2d-1"
PER_MEETING = 20
GENERIC_MIN_MEETINGS = 4
MINUTES_MIN_CHARS = 3
ROUND_MEETINGS = 20
ROUND_SECONDS = 5.0
_VERSION_TAG = re.compile(r"(?<![a-z])v\d{1,3}(?:\.\d{1,3}){0,2}(?![\d.]*\d)", re.IGNORECASE)
_HHMMSS = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")
_ZONES_SQL = ", ".join(f"'{zone}'" for zone in MATCH_ZONES)


class MentionError(ValueError):
    pass


class MentionNotFound(LookupError):
    pass


# ---------------------------------------------------------------------- 摘要


def project_sigs(connection: Any) -> dict[str, str]:
    """每个项目的 stems_sig（固定 3 条查询）。"""
    roots: dict[str, list[str]] = {}
    for row in connection.execute(
        """SELECT r.project_id, r.id, r.path, COALESCE(s.stems_rev, 0) AS rev
             FROM project_material_roots r LEFT JOIN material_index_state s ON s.root_id = r.id
            ORDER BY r.id"""
    ).fetchall():
        roots.setdefault(row["project_id"], []).append(f"{row['id']}:{row['rev']}:{row['path']}")
    terms: dict[str | None, str] = {}
    for row in connection.execute(
        "SELECT project_id, COUNT(*) AS n, MAX(updated_at) AS at FROM glossary_terms GROUP BY project_id"
    ).fetchall():
        terms[row["project_id"]] = f"{row['n']}:{row['at']}"
    sigs: dict[str, str] = {}
    for row in connection.execute("SELECT id, name, also_names FROM projects").fetchall():
        digest = hashlib.sha1()
        for part in (
            MATCH_VERSION,
            row["name"] or "",
            row["also_names"] or "",
            "|".join(roots.get(row["id"], [])),
            terms.get(row["id"], ""),
            terms.get(None, ""),
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        sigs[row["id"]] = digest.hexdigest()[:20]
    return sigs


# ---------------------------------------------------------------------- 项目上下文


def version_tag(name: str) -> str | None:
    """文件名里的版本号（v3、V1.2），全名比对用。"""
    base = name.rpartition(".")[0] or name
    found = _VERSION_TAG.findall(base)
    return light_key(found[-1]) if found else None


class ProjectContext:
    """一个项目的词干、挡板和扫描器，一轮里同项目的会共用。"""

    def __init__(self, connection: Any, project_id: str):
        project = connection.execute(
            "SELECT id, name, also_names FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        self.project_id = project_id
        names = [project["name"]] if project else []
        names += [entry["name"] for entry in also_entries(project["also_names"] if project else None)]
        root_rows = connection.execute(
            "SELECT id, path FROM project_material_roots WHERE project_id = ?", (project_id,)
        ).fetchall()
        root_names = [str(row["path"]).rstrip("/").rpartition("/")[2] for row in root_rows]
        excluded = {norm_key(name) for name in [*names, *root_names] if name}
        excluded.discard("")
        files = [
            dict(row)
            for row in connection.execute(
                f"""SELECT f.id, f.root_id, f.rel_path, f.name, f.stem, f.stem_key, f.mtime_ns, f.size,
                           f.content_key, f.content_size, f.content_mtime_ns
                      FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                     WHERE r.project_id = ? AND f.gone_at IS NULL AND f.stem_key != ''
                       AND f.zone IN ({_ZONES_SQL})""",
                (project_id,),
            ).fetchall()
        ]
        self.groups: dict[str, list[dict[str, Any]]] = {}
        self.usability: dict[str, str] = {}
        for row in files:
            key = row["stem_key"]
            if key not in self.usability:
                usable = stem_usability(key)
                if usable != STEM_NO and norm_key(key) in excluded:
                    usable = STEM_NO
                self.usability[key] = usable
            if self.usability[key] == STEM_NO:
                continue
            self.groups.setdefault(key, []).append(row)
        # 针：词干本身；一个词干有多份文件时，再加「词干 + 版本号」的全名
        self.needles: dict[str, tuple[str, int | None]] = {}
        for key, group in self.groups.items():
            self.needles[key] = (key, None)
            if len(group) > 1:
                for row in group:
                    tag = version_tag(row["name"])
                    if tag:
                        self.needles.setdefault(key + tag, (key, row["id"]))
        # 挡板：项目名、也叫、词条（本项目和通用的）。落在它们里面的出现不算。
        blockers = [*names]
        for row in connection.execute(
            "SELECT term FROM glossary_terms WHERE project_id = ? OR project_id IS NULL", (project_id,)
        ).fetchall():
            blockers.append(row["term"])
        forms = list(self.needles)
        forms += [light_key(text) for text in blockers if light_key(text) and light_key(text) not in self.needles]
        self.scanner = FormScanner(forms) if self.needles else None
        self.files_by_id = {row["id"]: row for group in self.groups.values() for row in group}

    def follow_picked(self, key: str, content_key: str | None) -> tuple[dict[str, Any] | None, bool]:
        """你手动换过的文件不见了：在同一词干组的活文件里按内容标识找。返回 (找到的文件, 要不要等)。

        组里还有没算出标识（或标识过时）的活文件时要等：这条提到原样保留，不退回自动选。"""
        if not content_key:
            return None, False
        waiting = False
        for row in sorted(self.groups.get(key, []), key=lambda item: item["id"]):
            fresh = row["content_size"] == row["size"] and row["content_mtime_ns"] == row["mtime_ns"]
            if row["content_key"] is None or not fresh:
                waiting = True
                continue
            if row["content_key"] == content_key:
                return row, False
        return None, waiting


# ---------------------------------------------------------------------- 一场会


def _meeting_ns(recording_date: str | None, created_at: str | None) -> int | None:
    """会议时间（纳秒）：只有日期时取那天结束。"""
    for raw in (recording_date, created_at):
        if not raw:
            continue
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            if len(text) == 10:
                moment = datetime.combine(datetime.fromisoformat(text).date(), day_time(23, 59, 59))
            else:
                moment = datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return int(moment.timestamp() * 1_000_000_000)
    return None


def _line_anchor(line: str) -> int | None:
    match = _HHMMSS.search(line)
    if match is None:
        return None
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is None:
        return (int(first) * 60 + int(second)) * 1000
    return ((int(first) * 60 + int(second)) * 60 + int(third)) * 1000


def _continues_number(text: str, end: int) -> bool:
    following = text[end : end + 2]
    return following[:1].isdigit() or (following[:1] == "." and following[1:2].isdigit())


def _pick_by_date(group: list[dict[str, Any]], meeting_ns: int | None) -> dict[str, Any]:
    def mtime(row: dict[str, Any]) -> int:
        return int(row["mtime_ns"] or 0)

    if meeting_ns is not None:
        before = [row for row in group if mtime(row) <= meeting_ns]
        if before:
            return max(before, key=lambda row: (mtime(row), row["id"]))
        return min(group, key=lambda row: (mtime(row), row["id"]))
    return max(group, key=lambda row: (mtime(row), row["id"]))


def compute_mentions(
    context: ProjectContext,
    *,
    segments: list[dict[str, Any]],
    minutes: str,
    meeting_ns: int | None,
    existing: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """这场会在本项目的提到：{stem_key: 行}。existing 是本项目已有的行（按 stem_key）。"""
    if context.scanner is None:
        return {}
    spoken: dict[str, dict[str, Any]] = {}
    for segment in segments:
        text = str(segment.get("text") or "")
        if not text:
            continue
        start_ms = int(segment.get("start_ms") or 0)
        for needle, _start, _end in context.scanner.matches(text):
            target = context.needles.get(needle)
            if target is None:
                continue  # 挡板
            key, file_id = target
            if file_id is not None and _continues_number(text, _end):
                file_id = None  # 说的是「报价单 v30」，不是 v3 那一份
            entry = spoken.setdefault(key, {"count": 0, "first_ms": start_ms, "anchors": [], "versions": Counter()})
            entry["count"] += 1
            if (not entry["anchors"] or entry["anchors"][-1] != start_ms) and len(entry["anchors"]) < MAX_ANCHORS:
                entry["anchors"].append(start_ms)
            if file_id is not None:
                entry["versions"][file_id] += 1
    written: dict[str, dict[str, Any]] = {}
    for line in (minutes or "").splitlines():
        if not line.strip():
            continue
        for needle, _start, _end in context.scanner.matches(line):
            target = context.needles.get(needle)
            if target is None:
                continue
            key, file_id = target
            if file_id is not None and _continues_number(line, _end):
                file_id = None
            entry = written.setdefault(key, {"count": 0, "first_ms": _line_anchor(line), "versions": Counter()})
            entry["count"] += 1
            if file_id is not None:
                entry["versions"][file_id] += 1

    result: dict[str, dict[str, Any]] = {}
    for key in set(spoken) | set(written):
        usable = context.usability.get(key, STEM_NO)
        said = spoken.get(key)
        wrote = written.get(key)
        if said is not None and (usable == STEM_YES or (usable == STEM_TWICE and said["count"] >= 2)):
            source, count, first_ms, anchors = "transcript", said["count"], said["first_ms"], said["anchors"]
            versions = said["versions"]
        elif said is None and wrote is not None and usable == STEM_YES and len(key) >= MINUTES_MIN_CHARS:
            source, count, first_ms = "minutes", 0, wrote["first_ms"]
            anchors = [first_ms] if first_ms is not None else []
            versions = wrote["versions"]
        else:
            continue
        previous = existing.get(key)
        if previous is not None and previous["status"] == "rejected":
            continue
        group = context.groups[key]
        picked = 0
        followed: dict[str, Any] | None = None
        waiting = False
        if previous is not None and previous["picked"] and previous["file_id"] not in context.files_by_id:
            followed, waiting = context.follow_picked(key, previous.get("picked_key"))
        if waiting:
            # 还有没算出标识的同名文件：原样保留你选的那份，等内容循环算完标识再比
            result[key] = {
                "stem_key": key,
                "file_id": previous["file_id"],
                "needle": previous["needle"],
                "count": count,
                "first_ms": first_ms,
                "anchors_json": json.dumps(anchors),
                "minutes_count": wrote["count"] if wrote else 0,
                "source": source,
                "picked": 1,
            }
            continue
        if previous is not None and previous["picked"] and previous["file_id"] in context.files_by_id:
            chosen = context.files_by_id[previous["file_id"]]
            picked = 1
        elif followed is not None:
            chosen = followed  # 挪了位置、改了文件夹：按内容找回，仍算你选的
            picked = 1
        elif versions:
            chosen = max(
                (context.files_by_id[file_id] for file_id in versions),
                key=lambda row: (versions[row["id"]], int(row["mtime_ns"] or 0), row["id"]),
            )
        else:
            chosen = _pick_by_date(group, meeting_ns)
        result[key] = {
            "stem_key": key,
            "file_id": chosen["id"],
            "needle": chosen["stem"],
            "count": count,
            "first_ms": first_ms,
            "anchors_json": json.dumps(anchors),
            "minutes_count": wrote["count"] if wrote else 0,
            "source": source,
            "picked": picked,
        }
    ranked = sorted(result.values(), key=lambda row: (-row["count"], -row["minutes_count"], row["stem_key"]))
    return {row["stem_key"]: row for row in ranked[:PER_MEETING]}


_COMPARED = ("file_id", "needle", "count", "first_ms", "anchors_json", "minutes_count", "source", "picked")


def _write_mentions(
    connection: Any,
    meeting_id: str,
    project_id: str,
    result: dict[str, dict[str, Any]],
    existing_all: list[dict[str, Any]],
) -> None:
    now = utc_now()
    for row in existing_all:
        if row["status"] != "active":
            continue
        if row["project_id"] != project_id or row["stem_key"] not in result:
            connection.execute(
                "DELETE FROM meeting_file_mentions WHERE meeting_id = ? AND project_id = ? AND stem_key = ?",
                (meeting_id, row["project_id"], row["stem_key"]),
            )
    current = {row["stem_key"]: row for row in existing_all if row["project_id"] == project_id}
    for key, item in result.items():
        previous = current.get(key)
        if previous is None:
            connection.execute(
                """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count,
                       first_ms, anchors_json, minutes_count, source, status, picked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (meeting_id, project_id, key, item["file_id"], item["needle"], item["count"], item["first_ms"],
                 item["anchors_json"], item["minutes_count"], item["source"], item["picked"], now),
            )
            continue
        if previous["status"] != "active":
            continue
        # 全都没变就不写（AFTER UPDATE 触发器对没变化的更新也会让关系图版本号加一）
        if all(previous[column] == item[column] for column in _COMPARED):
            continue
        connection.execute(
            """UPDATE meeting_file_mentions SET file_id = ?, needle = ?, count = ?, first_ms = ?,
                   anchors_json = ?, minutes_count = ?, source = ?, picked = ?, updated_at = ?
             WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
            (item["file_id"], item["needle"], item["count"], item["first_ms"], item["anchors_json"],
             item["minutes_count"], item["source"], item["picked"], now, meeting_id, project_id, key),
        )


def _pending(connection: Any) -> list[dict[str, Any]]:
    sigs = project_sigs(connection)
    rows = connection.execute(
        """SELECT m.id, m.project_id, m.recording_date, m.created_at,
                  m.current_transcript_version_id AS transcript_id,
                  m.current_minutes_version_id AS minutes_id,
                  s.dirty, s.stems_sig
             FROM meetings m LEFT JOIN meeting_file_scan s ON s.meeting_id = m.id
            WHERE m.project_id IS NOT NULL"""
    ).fetchall()
    todo = []
    for row in rows:
        sig = sigs.get(row["project_id"])
        if sig is None:
            continue
        if row["dirty"] is None or row["dirty"] or row["stems_sig"] != sig:
            todo.append({**dict(row), "sig": sig})
    # 最近的会先比对
    todo.sort(key=lambda row: (row["recording_date"] or row["created_at"] or "", row["id"]), reverse=True)
    return todo


def match_meeting(db: Database, row: dict[str, Any], contexts: dict[str, ProjectContext]) -> bool:
    """比对一场会；比对期间会被改过（dirty 变了）就不写，留给下一轮。返回是否写了。"""
    meeting_id = row["id"]
    project_id = row["project_id"]
    with db.autocommit() as connection:
        context = contexts.get(project_id)
        if context is None:
            context = contexts[project_id] = ProjectContext(connection, project_id)
        segments = (
            [
                dict(item)
                for item in connection.execute(
                    "SELECT start_ms, text FROM segments WHERE version_id = ? ORDER BY ordinal",
                    (row["transcript_id"],),
                ).fetchall()
            ]
            if row["transcript_id"]
            else []
        )
        minutes_row = (
            connection.execute("SELECT markdown FROM minutes_versions WHERE id = ?", (row["minutes_id"],)).fetchone()
            if row["minutes_id"]
            else None
        )
        existing_all = [
            dict(item)
            for item in connection.execute(
                """SELECT fm.*, pf.content_key AS picked_key
                     FROM meeting_file_mentions fm LEFT JOIN material_files pf ON pf.id = fm.file_id
                    WHERE fm.meeting_id = ?""",
                (meeting_id,),
            ).fetchall()
        ]
    existing = {item["stem_key"]: item for item in existing_all if item["project_id"] == project_id}
    result = compute_mentions(
        context,
        segments=segments,
        minutes=minutes_row["markdown"] if minutes_row else "",
        meeting_ns=_meeting_ns(row["recording_date"], row["created_at"]),
        existing=existing,
    )
    with db.transaction() as connection:
        if row["dirty"] is None:
            claimed = connection.execute(
                """INSERT INTO meeting_file_scan(meeting_id, stems_sig, dirty, scanned_at)
                   VALUES (?, ?, 0, ?) ON CONFLICT(meeting_id) DO NOTHING""",
                (meeting_id, row["sig"], utc_now()),
            ).rowcount
        else:
            claimed = connection.execute(
                """UPDATE meeting_file_scan SET stems_sig = ?, dirty = 0, scanned_at = ?
                    WHERE meeting_id = ? AND dirty = ?""",
                (row["sig"], utc_now(), meeting_id, row["dirty"]),
            ).rowcount
        if not claimed:
            return False
        still = connection.execute("SELECT project_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if still is None or still["project_id"] != project_id:
            return False
        _write_mentions(connection, meeting_id, project_id, result, existing_all)
    return True


def match_pending(
    db: Database,
    *,
    clock: Callable[[], float] = time.monotonic,
    max_meetings: int = ROUND_MEETINGS,
    max_seconds: float = ROUND_SECONDS,
) -> dict[str, int]:
    """扫描循环里的一段：比对要比对的会，每轮最多 max_meetings 场或 max_seconds 秒。"""
    with db.autocommit() as connection:
        todo = _pending(connection)
    deadline = clock() + max_seconds
    contexts: dict[str, ProjectContext] = {}
    written = 0
    tried = 0
    for row in todo[:max_meetings]:
        if clock() >= deadline:
            break
        tried += 1
        written += int(match_meeting(db, row, contexts))
    return {"pending": len(todo), "tried": tried, "written": written}


# ---------------------------------------------------------------------- 读


def generic_keys(connection: Any, project_id: str) -> set[str]:
    """被本项目一半以上、至少 4 场会提到的词干。"""
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM meetings WHERE project_id = ?", (project_id,)
    ).fetchone()["n"]
    return {
        row["stem_key"]
        for row in connection.execute(
            """SELECT fm.stem_key, COUNT(DISTINCT fm.meeting_id) AS n
                 FROM meeting_file_mentions fm
                 JOIN meetings m ON m.id = fm.meeting_id AND m.project_id = fm.project_id
                 JOIN material_files f ON f.id = fm.file_id AND f.gone_at IS NULL
                WHERE fm.project_id = ? AND fm.status = 'active'
                GROUP BY fm.stem_key""",
            (project_id,),
        ).fetchall()
        if is_generic(int(row["n"]), int(total))
    }


def is_generic(meetings: int, total: int) -> bool:
    return meetings >= GENERIC_MIN_MEETINGS and meetings * 2 > total


def files_state(connection: Any, meeting_id: str, project_id: str | None) -> str:
    """done / indexing / offline / no_project / no_root。"""
    if not project_id:
        return "no_project"
    roots = connection.execute(
        """SELECT COALESCE(s.state, 'pending') AS state, s.stems_hash IS NOT NULL AS indexed_once
             FROM project_material_roots r LEFT JOIN material_index_state s ON s.root_id = r.id
            WHERE r.project_id = ?""",
        (project_id,),
    ).fetchall()
    if not roots:
        return "no_root"
    if any(row["state"] in (STATE_OFFLINE, STATE_MISSING) for row in roots):
        return "offline"
    if any(not row["indexed_once"] for row in roots):
        return "indexing"
    scan = connection.execute(
        "SELECT dirty, stems_sig FROM meeting_file_scan WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    if scan is None or scan["dirty"] or scan["stems_sig"] != project_sigs(connection).get(project_id):
        return "indexing"
    return "done"


def meeting_files(connection: Any, meeting_id: str, project_id: str | None) -> dict[str, Any]:
    """会议简报的 files[] 和 files_state：这场会的全部有效提到，按次数排，通用的排最后。"""
    state = files_state(connection, meeting_id, project_id)
    if not project_id:
        return {"files": [], "files_state": state}
    generic = generic_keys(connection, project_id)
    rows = connection.execute(
        f"""SELECT fm.stem_key, fm.needle, fm.count, fm.first_ms, fm.minutes_count, fm.source, fm.picked,
                   f.id AS file_id, f.name, f.rel_path, f.root_id
              FROM meeting_file_mentions fm
              JOIN material_files f ON f.id = fm.file_id AND f.gone_at IS NULL
             WHERE fm.meeting_id = ? AND fm.project_id = ? AND fm.status = 'active'
               AND f.zone IN ({_ZONES_SQL})""",
        (meeting_id, project_id),
    ).fetchall()
    files = [
        {
            "file_id": row["file_id"],
            "name": row["name"],
            "rel_path": row["rel_path"],
            "root_id": row["root_id"],
            "stem_key": row["stem_key"],
            "needle": row["needle"],
            "count": int(row["count"]),
            "minutes_count": int(row["minutes_count"]),
            "first_ms": row["first_ms"],
            "source": row["source"],
            "picked": bool(row["picked"]),
            "generic": row["stem_key"] in generic,
        }
        for row in rows
    ]
    files.sort(key=lambda item: (item["generic"], -item["count"], -item["minutes_count"], item["name"]))
    return {"files": files[:PER_MEETING], "files_state": state}


def file_detail(connection: Any, file_id: int, *, quotes: Callable[[str, list[int]], dict[int, str]]) -> dict[str, Any]:
    """GET /api/graph/files/{id}：文件信息、同名的其他文件、在哪几场会上被提到。只查库。"""
    row = connection.execute(
        """SELECT f.*, r.path AS root_path, r.project_id, p.name AS project_name
             FROM material_files f
             JOIN project_material_roots r ON r.id = f.root_id
             JOIN projects p ON p.id = r.project_id
            WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise MentionNotFound("文件不在索引里（可能已经挪走或删掉了）")
    root_path = str(row["root_path"]).rstrip("/")
    dir_path = f"{root_path}/{row['dir_rel']}" if row["dir_rel"] else root_path
    siblings = [
        {
            "id": item["id"],
            "name": item["name"],
            "rel_path": item["rel_path"],
            "root_id": item["root_id"],
            "modified_at": _iso_ns(item["mtime_ns"]),
        }
        for item in connection.execute(
            f"""SELECT f.id, f.name, f.rel_path, f.root_id, f.mtime_ns
                  FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                 WHERE r.project_id = ? AND f.stem_key = ? AND f.id != ? AND f.stem_key != ''
                   AND f.gone_at IS NULL AND f.zone IN ({_ZONES_SQL})
                 ORDER BY f.mtime_ns DESC, f.id DESC LIMIT 20""",
            (row["project_id"], row["stem_key"], file_id),
        ).fetchall()
    ]
    mention_rows = connection.execute(
        """SELECT fm.meeting_id, fm.stem_key, fm.needle, fm.count, fm.first_ms, fm.anchors_json,
                  fm.minutes_count, fm.source, fm.status, fm.picked,
                  m.title, m.recording_date, m.created_at
             FROM meeting_file_mentions fm
             JOIN meetings m ON m.id = fm.meeting_id AND m.project_id = fm.project_id
            WHERE fm.file_id = ?
            ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id
            LIMIT 40""",
        (file_id,),
    ).fetchall()
    meetings = []
    for item in mention_rows:
        first_ms = item["first_ms"]
        quote = ""
        if item["source"] == "transcript" and first_ms is not None:
            quote = quotes(item["meeting_id"], [int(first_ms)]).get(int(first_ms), "")
        meetings.append(
            {
                "meeting_id": item["meeting_id"],
                "title": item["title"],
                "date": str(item["recording_date"] or item["created_at"] or "")[:10],
                "stem_key": item["stem_key"],
                "needle": item["needle"],
                "count": int(item["count"]),
                "minutes_count": int(item["minutes_count"]),
                "first_ms": first_ms,
                "anchors_ms": json.loads(item["anchors_json"] or "[]"),
                "source": item["source"],
                "status": item["status"],
                "picked": bool(item["picked"]),
                "quote": quote,
            }
        )
    return {
        "file": {
            "id": row["id"],
            "name": row["name"],
            "ext": row["ext"],
            "stem": row["stem"],
            "stem_key": row["stem_key"],
            "rel_path": row["rel_path"],
            "root_id": row["root_id"],
            "root_path": row["root_path"],
            "folder_path": dir_path,
            "path": f"{root_path}/{row['rel_path']}",
            "size": row["size"],
            "modified_at": _iso_ns(row["mtime_ns"]),
            "zone": row["zone"],
            "gone": row["gone_at"] is not None,
            "project_id": row["project_id"],
            "project_name": row["project_name"],
        },
        "siblings": siblings,
        "meetings": meetings,
        "active_meetings": sum(1 for item in meetings if item["status"] == "active"),
    }


def _iso_ns(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1_000_000_000).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------- 你的改动


def _mention_row(connection: Any, meeting_id: str, stem_key: str) -> dict[str, Any]:
    meeting = connection.execute("SELECT project_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if meeting is None:
        raise MentionNotFound("会议不存在")
    if not meeting["project_id"]:
        raise MentionError("这场会还没归项目")
    row = connection.execute(
        "SELECT * FROM meeting_file_mentions WHERE meeting_id = ? AND project_id = ? AND stem_key = ?",
        (meeting_id, meeting["project_id"], stem_key),
    ).fetchone()
    if row is None:
        raise MentionNotFound("这场会没有提到这个文件名")
    return dict(row)


def _result(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "meeting_id": row["meeting_id"],
        "project_id": row["project_id"],
        "stem_key": row["stem_key"],
        "file_id": row["file_id"],
        "status": row["status"],
        "picked": bool(row["picked"]),
        **extra,
    }


def _invalidate_scan(connection: Any, meeting_id: str) -> None:
    """你改了这场会的提到：正在进行的比对写不进来（dirty 变了），下一轮按你的改动重新比对。"""
    connection.execute(
        """INSERT INTO meeting_file_scan(meeting_id, dirty) VALUES (?, 1)
           ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1""",
        (meeting_id,),
    )


def reject_mention(db: Database, meeting_id: str, stem_key: str) -> dict[str, Any]:
    """「不是这份文件」：只挡这场会、这个项目；以后比对也不会改回来。"""
    with db.transaction() as connection:
        row = _mention_row(connection, meeting_id, stem_key)
        if row["status"] != "rejected":
            connection.execute(
                """UPDATE meeting_file_mentions SET status = 'rejected', updated_at = ?
                    WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
                (utc_now(), meeting_id, row["project_id"], stem_key),
            )
            _invalidate_scan(connection, meeting_id)
        row["status"] = "rejected"
    return _result(row)


def restore_mention(db: Database, meeting_id: str, stem_key: str) -> dict[str, Any]:
    """撤销「不是这份文件」：改回有效，这场会下一轮重新比对（次数、文件按现在的算）。"""
    with db.transaction() as connection:
        row = _mention_row(connection, meeting_id, stem_key)
        if row["status"] != "active":
            connection.execute(
                """UPDATE meeting_file_mentions SET status = 'active', updated_at = ?
                    WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
                (utc_now(), meeting_id, row["project_id"], stem_key),
            )
            _invalidate_scan(connection, meeting_id)
        row["status"] = "active"
    return _result(row)


def pick_mention_file(db: Database, meeting_id: str, stem_key: str, file_id: int) -> dict[str, Any]:
    """［换成这份］：换成同名的另一份文件，以后比对不再自动改（文件不在了才换）。"""
    with db.transaction() as connection:
        row = _mention_row(connection, meeting_id, stem_key)
        target = connection.execute(
            f"""SELECT f.id FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                 WHERE f.id = ? AND r.project_id = ? AND f.stem_key = ? AND f.gone_at IS NULL
                   AND f.zone IN ({_ZONES_SQL})""",
            (file_id, row["project_id"], stem_key),
        ).fetchone()
        if target is None:
            raise MentionError("只能换成这个项目文件夹里同名的另一份文件")
        connection.execute(
            """UPDATE meeting_file_mentions SET file_id = ?, picked = 1, status = 'active', updated_at = ?
                WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
            (file_id, utc_now(), meeting_id, row["project_id"], stem_key),
        )
        _invalidate_scan(connection, meeting_id)
        row.update(file_id=file_id, picked=1, status="active")
    # 当场算好内容标识：以后文件挪了位置，按内容找回你选的这份（盘不在、读不了就算了）
    try:
        key_file_now(db, file_id)
    except Exception:  # noqa: BLE001
        pass
    return _result(row)

