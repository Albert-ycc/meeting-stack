"""第四期 4d：相关的读（会议页的相关材料栏、标过不相关的、关系图的相关线、预览定位、内容相关的会）和
［用本机应用打开］。

- GET 都只读，不写库。片段在请求时从 material_chunks 现读：栏里最多 120 字（search._snippet 的截法），
  关系图悬停最多 60 字，预览定位最多 400 字。relations 和派生表里只存指针（content_key、ordinal）和共同词。
- 分数、门槛、名次和 relation_id 都不出栏的接口（关系图的边带 relation_id 和只表示顺序的 rank）。
- 读的时候过滤：你标过不相关的（按 content_key，或按 (root_id, rel_path)）、这场会自己的副本、到处都相关的
  内容、没有活文件的内容。
- 栏的状态一句话、最多一个按钮（kind 是 ok、waiting、stopped）；项目里没读完的材料数和项目页覆盖率同一套
  数（没读完的文件加上已读但向量还在补的），每个项目缓存 30 秒。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from . import related
from .graph import WINDOWS, local_day
from .material_rules import PLAYABLE_TYPES
from .material_status import _READABLE_SQL
from .materials import ROOT_ONLINE, volume_state
from .relation_read import AUDIO_ID_SQL, _passage, _short
from .search import _snippet

PANEL_WINDOWS = 400
PANEL_ITEMS = 2
GRAPH_EDGES_PER_NODE = 3
PREVIEW_PASSAGE_CHARS = 400
RELATED_MEETINGS = 5
UNREAD_CACHE_SECONDS = 30.0
MENTIONED_IDS_MAX = 200

# ［用本机应用打开］只开文档、图片和音视频；代码、网页和脚本一律不开，材料里的文件永远不会被执行
OPEN_EXTS = frozenset(
    {
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "key",
        "pages",
        "numbers",
        "rtf",
        "txt",
        "md",
        "csv",
        "png",
        "jpg",
        "jpeg",
        "heic",
        "gif",
        "webp",
        "mp3",
        "m4a",
        "wav",
        "mp4",
        "mov",
    }
)
# 可能是目录形式的包
PACKAGE_EXTS = frozenset({"key", "pages", "numbers"})

# 栏的状态句（第 6 节「栏的状态」）
EMPTY = "这场会没找到相关材料"
PARTIAL = "这个项目材料太多，较早的一部分没有比对"
FINDING = "正在找相关材料"
THIS_TRANSCRIBING = "这场会还在转写，转完再找相关材料"
OTHER_TRANSCRIBING = "会议在转写，转完再找相关材料"
NO_PROJECT = "这场会没归项目，相关材料只在项目文件夹里找"
NO_ROOTS = "这个项目还没挂材料文件夹"
NO_TRANSCRIPT = "还没有逐字稿"
NO_MODEL = "本地语义模型没装好，找不了相关材料"
SEMANTIC_OFF = "语义索引关着，找不了相关材料"
CONTENT_OFF = "材料正文读取关着，找不了相关材料"
LINKS_OFF = "关联整理关着，找不了相关材料"
READING_SOME = "这个项目的材料还没读完，读完会接着找"
OPEN_PROJECT_LABEL = "去项目页"
OFFLINE_TEXT = "资料盘未连接"
PANEL_SENTENCES = (
    EMPTY,
    PARTIAL,
    FINDING,
    THIS_TRANSCRIBING,
    OTHER_TRANSCRIBING,
    NO_PROJECT,
    NO_ROOTS,
    NO_TRANSCRIPT,
    NO_MODEL,
    SEMANTIC_OFF,
    CONTENT_OFF,
    LINKS_OFF,
    READING_SOME,
)

# ［用本机应用打开］的错误
OPEN_REMOTE = "只能在声档所在的这台电脑上打开文件"
OPEN_REFUSED = "这种文件不在声档里直接打开，可以在访达中显示"
OPEN_OFFLINE = "资料盘未连接"
OPEN_MISSING = "找不到这个文件了"
OPEN_OUTSIDE = "这个文件指到了材料根目录外面"
OPEN_NO_APP = "这台电脑上找不到能打开文件的程序"
OPEN_FAILED = "打开文件失败"


def reading_text(count: int) -> str:
    return f"这个项目还有 {int(count)} 份材料没读完，读完的先列在这里"


class RelatedError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _state(
    kind: str, text: str | None = None, action: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"kind": kind, "text": text, "action": action}


# ---------------------------------------------------------------------- 没读完的材料数（缓存 30 秒）


class UnreadCounts:
    """项目里没读完的材料份数：没读完的文件加上已读但向量还在补的。每个项目缓存 30 秒。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, int]] = {}

    def get(self, connection: Any, project_id: str, model: str) -> int:
        key = (project_id, model)
        now = self.clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < UNREAD_CACHE_SECONDS:
                return cached[1]
        count = unread_count(connection, project_id, model)
        with self._lock:
            self._cache[key] = (now, count)
        return count


def unread_count(connection: Any, project_id: str, model: str) -> int:
    row = connection.execute(
        f"""SELECT COUNT(*) FROM material_files f
              JOIN project_material_roots r ON r.id = f.root_id
              LEFT JOIN material_contents c ON c.content_key = f.content_key
             WHERE r.project_id = ? AND f.gone_at IS NULL AND f.zone != 'cards' AND f.ext IN ({_READABLE_SQL})
               AND (f.content_error IS NULL OR f.content_error = 'io')
               AND (c.content_key IS NULL OR c.state = 'pending'
                    OR (c.state = 'done' AND EXISTS (
                        SELECT 1 FROM material_chunks k WHERE k.content_key = c.content_key
                           AND NOT EXISTS (SELECT 1 FROM material_chunk_vectors v
                                            WHERE v.chunk_id = k.id AND v.model = ?))))""",
        (project_id, model),
    ).fetchone()
    return int(row[0])


UNREAD = UnreadCounts()


# ---------------------------------------------------------------------- 会议页的栏


def _files_for(connection: Any, project_id: str, keys: list[str]) -> dict[str, dict[str, Any]]:
    """这些内容在项目里的代表文件（修改时间最新的活文件）。"""
    result: dict[str, dict[str, Any]] = {}
    for part in related._batches(sorted(set(keys))):
        for row in connection.execute(
            f"""SELECT f.id, f.content_key, f.name, f.ext, f.root_id, f.rel_path, f.mtime_ns, r.path AS root_path
                  FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                 WHERE r.project_id = ? AND f.content_key IN ({related._marks(part)}) AND {related._LIVE_FILE}""",
            [project_id, *part],
        ).fetchall():
            key = str(row["content_key"])
            current = result.get(key)
            if current is None or (int(row["mtime_ns"] or 0), int(row["id"])) > (
                int(current["mtime_ns"] or 0),
                int(current["id"]),
            ):
                result[key] = dict(row)
    return result


def can_open(ext: str | None, *, local: bool, gone: bool = False) -> bool:
    """［用本机应用打开］出不出：本机请求、扩展名在白名单里、文件没有不见。前端只看这个字段。"""
    return bool(local and not gone and str(ext or "").lower() in OPEN_EXTS)


def _file_info(row: dict[str, Any], *, local: bool, online: bool) -> dict[str, Any]:
    ext = str(row["ext"] or "")
    return {
        "file_id": int(row["id"]),
        "name": row["name"],
        "ext": ext,
        "root_online": online,
        "playable": ext in PLAYABLE_TYPES,
        "can_open": can_open(ext, local=local),
        "state_text": "" if online else OFFLINE_TEXT,
    }


def panel(
    connection: Any,
    meeting_id: str,
    *,
    worker: Any,
    settings: Any,
    local: bool,
    state_of: Callable[[str], str] = volume_state,
    unread: UnreadCounts = UNREAD,
) -> dict[str, Any] | None:
    """GET /api/meetings/{id}/related-materials。会议不存在回 None。只读。"""
    meeting = connection.execute(
        "SELECT id, project_id, current_transcript_version_id AS version_id FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if meeting is None:
        return None
    result: dict[str, Any] = {
        "state": _state("ok"),
        "files": {},
        "copies": [],
        "windows": [],
        "rejected": 0,
    }
    snap = _snapshot(worker)
    busy = snap.get("paused") == "busy"
    stopped = _stopped(settings, worker)
    if stopped is not None:
        result["state"] = stopped
        return result
    project_id = meeting["project_id"]
    if not project_id:
        result["state"] = _state("stopped", NO_PROJECT)
        return result
    roots = connection.execute(
        "SELECT COUNT(*) FROM project_material_roots WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if not roots:
        result["state"] = _state(
            "stopped",
            NO_ROOTS,
            {"kind": "open_project", "label": OPEN_PROJECT_LABEL, "project_id": project_id},
        )
        return result
    if not meeting["version_id"]:
        result["state"] = (
            _state("waiting", THIS_TRANSCRIBING) if busy else _state("stopped", NO_TRANSCRIPT)
        )
        return result
    scan = connection.execute(
        "SELECT copies_json, partial, scanned_at FROM meeting_related_scan WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()
    passages = connection.execute(
        """SELECT p.start_ms, p.rank, p.content_key, p.ordinal, p.words, p.seg_ms, c.loc, c.start_ms AS chunk_ms,
                  c.text
             FROM meeting_window_passages p JOIN material_chunks c ON c.id = p.chunk_id
            WHERE p.meeting_id = ? ORDER BY p.start_ms, p.rank""",
        (meeting_id,),
    ).fetchall()
    keys = sorted({str(row["content_key"]) for row in passages})
    copies = related._json_list(scan["copies_json"]) if scan is not None else []
    files = _files_for(connection, project_id, [*keys, *copies])
    rejected = related.rejected_filter(connection, meeting_id, project_id)
    result["rejected"] = int(
        connection.execute(
            """SELECT COUNT(*) FROM relations
                WHERE kind = 'related' AND meeting_id = ? AND project_id = ? AND status = 'rejected'""",
            (meeting_id, project_id),
        ).fetchone()[0]
    )
    hubs = related.hub_keys(connection, project_id, keys)
    online: dict[int, bool] = {}

    def is_online(row: dict[str, Any]) -> bool:
        root_id = int(row["root_id"])
        if root_id not in online:
            online[root_id] = state_of(str(row["root_path"])) == ROOT_ONLINE
        return online[root_id]

    windows: dict[int, list[dict[str, Any]]] = {}
    used: set[str] = set()
    for row in passages:
        key = str(row["content_key"])
        file = files.get(key)
        if file is None or key in copies or key in hubs or related.is_blocked(key, file, rejected):
            continue
        items = windows.setdefault(int(row["start_ms"]), [])
        if len(items) >= PANEL_ITEMS or any(item["content_key"] == key for item in items):
            continue
        words = json.loads(row["words"] or "[]")
        items.append(
            {
                "content_key": key,
                "ordinal": int(row["ordinal"]),
                "loc": row["loc"],
                "start_ms": row["chunk_ms"],
                "text": _snippet(str(row["text"] or ""), words[0] if words else ""),
                "words": words,
                "at_ms": int(row["seg_ms"]),
            }
        )
        used.add(key)
    ordered = [(start, items) for start, items in sorted(windows.items()) if items][:PANEL_WINDOWS]
    result["windows"] = [
        {"start_ms": start, "end_ms": start + related.WINDOW_MS, "items": items}
        for start, items in ordered
    ]
    kept = {item["content_key"] for _start, items in ordered for item in items}
    result["files"] = {
        key: _file_info(files[key], local=local, online=is_online(files[key]))
        for key in sorted(kept)
    }
    result["copies"] = [
        {"file_id": int(files[key]["id"]), "name": files[key]["name"]}
        for key in copies
        if key in files
    ]
    computed = scan is not None and scan["scanned_at"] is not None
    unread_now = unread.get(connection, project_id, related.model_of(settings))
    if ordered:
        if unread_now:
            result["state"] = _state("waiting", reading_text(unread_now))
        elif scan is not None and scan["partial"]:
            result["state"] = _state("ok", PARTIAL)
        return result
    if not computed:
        if busy:
            result["state"] = _state("waiting", OTHER_TRANSCRIBING)
        elif unread_now:
            result["state"] = _state("waiting", READING_SOME)
        else:
            result["state"] = _state("waiting", FINDING)
        return result
    if unread_now:
        result["state"] = _state("waiting", READING_SOME)
    elif scan["partial"]:
        result["state"] = _state("ok", PARTIAL)
    else:
        result["state"] = _state("ok", EMPTY)
    return result


def _snapshot(worker: Any) -> dict[str, Any]:
    snapshot = getattr(worker, "snapshot", None)
    value = snapshot() if callable(snapshot) else None
    return value if isinstance(value, dict) else {}


def _stopped(settings: Any, worker: Any) -> dict[str, Any] | None:
    if not getattr(settings, "links_enabled", False):
        return _state("stopped", LINKS_OFF)
    if not getattr(settings, "semantic_enabled", False):
        return _state("stopped", SEMANTIC_OFF)
    if not getattr(settings, "material_content_enabled", False):
        return _state("stopped", CONTENT_OFF)
    if getattr(getattr(worker, "related", None), "model_missing", False):
        return _state("stopped", NO_MODEL)
    return None


def rejected_items(connection: Any, meeting_id: str) -> dict[str, Any] | None:
    """GET /api/meetings/{id}/related-materials/rejected：［改回相关］用 4a 的 restore。"""
    meeting = connection.execute(
        "SELECT id, project_id FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        return None
    rows = connection.execute(
        """SELECT r.id, r.content_key, r.rel_path, r.decided_at, r.updated_at, f.name AS file_name,
                  (SELECT g.name FROM material_files g JOIN project_material_roots pr ON pr.id = g.root_id
                    WHERE pr.project_id = r.project_id AND g.content_key = r.content_key AND g.gone_at IS NULL
                    ORDER BY g.mtime_ns DESC, g.id LIMIT 1) AS live_name
             FROM relations r LEFT JOIN material_files f ON f.id = r.file_id
            WHERE r.kind = 'related' AND r.meeting_id = ? AND r.project_id IS ? AND r.status = 'rejected'
            ORDER BY r.decided_at DESC, r.id DESC""",
        (meeting_id, meeting["project_id"]),
    ).fetchall()
    return {
        "items": [
            {
                "relation_id": int(row["id"]),
                "name": row["live_name"]
                or row["file_name"]
                or str(row["rel_path"] or "").rpartition("/")[2],
                "decided_at": row["decided_at"] or row["updated_at"],
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------- 关系图的相关线（4f 画）


def related_rev(connection: Any) -> int:
    row = connection.execute("SELECT value FROM app_state WHERE key = 'related_rev'").fetchone()
    try:
        return int(row[0]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def related_etag(connection: Any, window: str, today: date) -> str:
    return f'W/"related-{related_rev(connection)}-{window}-{today.isoformat()}"'


class ProjectNotFound(LookupError):
    pass


def project_related(
    connection: Any, project_id: str, *, window: str, today: date
) -> dict[str, Any]:
    """GET /api/graph/projects/{id}/related：按窗口筛会，再按确定的贪心每个节点最多 3 条；rank 只是顺序；
    边 id 用 e:rel:<relation_id>；没有活文件的跳过；files 列出边里用到的每份文件。"""
    from .relation_read import LIVE_ID_SQL

    if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise ProjectNotFound("项目不存在")
    rows = connection.execute(
        f"""SELECT r.id, r.meeting_id, r.at_ms, r.quote, r.evidence_json, r.score, r.content_key,
                   m.recording_date, m.created_at, f.id AS file_id, f.name, f.ext, f.rel_path, f.root_id
              FROM relations r
              JOIN meetings m ON m.id = r.meeting_id AND m.project_id = r.project_id
              JOIN material_files f ON f.id = {LIVE_ID_SQL} AND f.gone_at IS NULL
             WHERE r.project_id = ? AND r.kind = 'related' AND r.status = 'shown'""",
        (project_id,),
    ).fetchall()
    days = WINDOWS.get(window)
    if days is not None:
        cutoff = today - timedelta(days=days)
        rows = [
            row for row in rows if local_day(row["recording_date"], row["created_at"]) >= cutoff
        ]
    hubs = related.hub_keys(connection, project_id, [str(row["content_key"]) for row in rows])
    ranked = []
    for row in rows:
        if row["content_key"] in hubs:
            continue
        evidence = related._json_obj(row["evidence_json"])
        words = [str(word) for word in evidence.get("words") or []]
        ranked.append(
            (
                -int(evidence.get("windows") or 0),
                -len(words),
                -float(row["score"] or 0),
                int(row["id"]),
                row,
                evidence,
                words,
            )
        )
    ranked.sort(key=lambda item: item[:4])
    per_meeting: dict[str, int] = {}
    per_file: dict[int, int] = {}
    chosen = []
    for *_order, row, evidence, words in ranked:
        meeting, file_id = str(row["meeting_id"]), int(row["file_id"])
        if (
            per_meeting.get(meeting, 0) >= GRAPH_EDGES_PER_NODE
            or per_file.get(file_id, 0) >= GRAPH_EDGES_PER_NODE
        ):
            continue
        per_meeting[meeting] = per_meeting.get(meeting, 0) + 1
        per_file[file_id] = per_file.get(file_id, 0) + 1
        chosen.append((row, evidence, words))
    material = [
        (
            str(ev.get("material", {}).get("content_key") or row["content_key"]),
            ev.get("material", {}).get("ordinal"),
        )
        for row, ev, _w in chosen
    ]
    chunks = _chunks_at(
        connection, [(key, ordinal) for key, ordinal in material if isinstance(ordinal, int)]
    )
    edges = []
    files: dict[str, dict[str, Any]] = {}
    for rank, ((row, evidence, words), (key, ordinal)) in enumerate(
        zip(chosen, material, strict=True), start=1
    ):
        chunk = chunks.get((key, ordinal)) if isinstance(ordinal, int) else None
        edges.append(
            {
                "id": f"e:rel:{row['id']}",
                "relation_id": int(row["id"]),
                "meeting_id": row["meeting_id"],
                "file_id": int(row["file_id"]),
                "rank": rank,
                "words": words,
                "at_ms": row["at_ms"],
                "quote": row["quote"] or "",
                "passage": {
                    "content_key": key,
                    "ordinal": ordinal,
                    "loc": (chunk or {}).get("loc") or evidence.get("material", {}).get("loc"),
                    "text": _passage((chunk or {}).get("text"), words) if chunk else "",
                },
            }
        )
        files[str(row["file_id"])] = {
            "file_id": int(row["file_id"]),
            "name": row["name"],
            "ext": row["ext"],
            "rel_path": row["rel_path"],
            "root_id": int(row["root_id"]),
        }
    return {"rev": related_rev(connection), "files": files, "edges": edges}


def _chunks_at(
    connection: Any, anchors: list[tuple[str, int]]
) -> dict[tuple[str, int], dict[str, Any]]:
    found: dict[tuple[str, int], dict[str, Any]] = {}
    unique = sorted(set(anchors))
    for part in related._batches(unique, 200):
        values = ", ".join("(?, ?)" for _ in part)
        params = [value for pair in part for value in pair]
        for row in connection.execute(
            f"""SELECT content_key, ordinal, loc, start_ms, text FROM material_chunks
                 WHERE (content_key, ordinal) IN (VALUES {values})""",
            params,
        ).fetchall():
            found[(str(row["content_key"]), int(row["ordinal"]))] = dict(row)
    return found


# ---------------------------------------------------------------------- 预览定位和内容相关的会


def passage(
    connection: Any, row: dict[str, Any], content_key: str | None, ordinal: int
) -> dict[str, Any] | None:
    """预览定位到那一段：这个 key 是文件现在的 → stale false；属于这份文件的来历（relations 或文件流水里
    有这份文件的 root_id、rel_path）并且片段还在 → stale true；否则 None。"""
    key = content_key or row.get("content_key")
    if not key:
        return None
    chunk = connection.execute(
        "SELECT loc, start_ms, text FROM material_chunks WHERE content_key = ? AND ordinal = ?",
        (key, int(ordinal)),
    ).fetchone()
    if chunk is None:
        return None
    if key != row.get("content_key"):
        history = connection.execute(
            """SELECT 1 WHERE EXISTS (SELECT 1 FROM relations r
                                       WHERE r.content_key = :key AND r.root_id = :root AND r.rel_path = :rel)
                          OR EXISTS (SELECT 1 FROM material_file_events e
                                      WHERE e.content_key = :key
                                        AND (e.file_id = :file OR (e.root_id = :root AND e.rel_path = :rel)))""",
            {"key": key, "root": row["root_id"], "rel": row["rel_path"], "file": row["id"]},
        ).fetchone()
        if history is None:
            return None
    return {
        "loc": chunk["loc"],
        "start_ms": chunk["start_ms"],
        "text": _short(str(chunk["text"] or ""), PREVIEW_PASSAGE_CHARS),
        "stale": key != row.get("content_key"),
    }


def related_meetings(
    connection: Any, file_id: int, limit: int = RELATED_MEETINGS
) -> list[dict[str, Any]]:
    """文件面板、预览抽屉的「内容相关的会」：按内容找（同内容的副本上也看得到），新的在前，最多 5 条。
    和项目图谱一样去掉到处都相关的内容（hub_keys）；和栏一样挡掉那场会里你标过不相关的（按内容或按这份
    文件的位置，原地改过也挡）。"""
    file = connection.execute(
        """SELECT f.content_key, f.root_id, f.rel_path, pr.project_id FROM material_files f
             JOIN project_material_roots pr ON pr.id = f.root_id WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if file is None or not file["content_key"]:
        return []
    project_id, key = str(file["project_id"]), str(file["content_key"])
    if key in related.hub_keys(connection, project_id, [key]):
        return []
    place = {"root_id": file["root_id"], "rel_path": file["rel_path"]}
    found = connection.execute(
        f"""SELECT r.id, r.meeting_id, r.at_ms, r.quote, r.evidence_json, m.title, m.recording_date, m.created_at,
                   {AUDIO_ID_SQL.format(meeting="m.id")} AS audio_id
              FROM relations r
              JOIN meetings m ON m.id = r.meeting_id AND m.project_id = r.project_id
             WHERE r.project_id = ? AND r.content_key = ? AND r.kind = 'related' AND r.status = 'shown'
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id""",
        (project_id, key),
    )
    rows = []
    for row in found:
        if related.is_blocked(
            key, place, related.rejected_filter(connection, str(row["meeting_id"]), project_id)
        ):
            continue
        rows.append(row)
        if len(rows) >= int(limit):
            break
    return [
        {
            "meeting_id": row["meeting_id"],
            "title": row["title"],
            "date": str(row["recording_date"] or row["created_at"] or "")[:10],
            "at_ms": row["at_ms"],
            "quote": row["quote"] or "",
            "words": [
                str(word) for word in related._json_obj(row["evidence_json"]).get("words") or []
            ],
            "audio_url": f"/api/media/{row['audio_id']}" if row["audio_id"] else None,
            "relation_id": int(row["id"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------- 「在 N 场会上被提到」的批量接口


def parse_file_ids(raw: str | None) -> list[int]:
    """逗号分隔的整数 id，最多 200 个；不是整数或超过 200 个抛 ValueError。"""
    parts = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if len(parts) > MENTIONED_IDS_MAX:
        raise ValueError("一次最多 200 个文件")
    ids = []
    for part in parts:
        if not part.isdigit():
            raise ValueError("文件 id 要是整数")
        ids.append(int(part))
    return ids


def mentioned_counts(connection: Any, file_ids: list[int]) -> dict[str, int]:
    """{file_id: 场数}；不认识或已不见的 id 不返回。"""
    from .relation_read import file_mention_counts

    ids = sorted(set(file_ids))
    live: list[int] = []
    for part in related._batches(ids):
        live += [
            int(row[0])
            for row in connection.execute(
                f"SELECT id FROM material_files WHERE id IN ({related._marks(part)}) AND gone_at IS NULL",
                list(part),
            ).fetchall()
        ]
    counts = file_mention_counts(connection, live)
    return {str(file_id): int(counts.get(file_id, 0)) for file_id in live}


# ---------------------------------------------------------------------- ［用本机应用打开］


def open_material(
    connection: Any,
    file_id: int,
    *,
    local: bool,
    state_of: Callable[[str], str] = volume_state,
    run: Callable[..., Any] | None = None,
    command_for: Callable[[str], list[str] | None] | None = None,
) -> None:
    """POST /api/materials/files/{id}/open：只在本机；扩展名白名单；realpath 还在根目录里、扩展名对得上；
    key、pages、numbers 可以是目录。传给打开程序的是 realpath，5 秒超时，不等应用退出。"""
    from .graph import open_command
    from .material_status import file_row, resolve_file

    if not local:
        raise RelatedError(403, OPEN_REMOTE)
    row = file_row(connection, file_id)
    if row is None or row.get("gone_at") is not None:
        raise RelatedError(404, OPEN_MISSING)
    ext = str(row["ext"] or "").lower()
    if ext not in OPEN_EXTS:
        raise RelatedError(415, OPEN_REFUSED)
    status, resolved = resolve_file(connection, file_id, state_of=state_of)
    if status == "offline":
        raise RelatedError(503, OPEN_OFFLINE)
    if status == "outside":
        raise RelatedError(403, OPEN_OUTSIDE)
    if status != "ok":
        raise RelatedError(404, OPEN_MISSING)
    real = str(resolved["real_path"])
    real_ext = os.path.splitext(real.rstrip("/"))[1].lstrip(".").lower()
    if real_ext != ext or real_ext not in OPEN_EXTS:
        raise RelatedError(415, OPEN_REFUSED)
    if os.path.isdir(real) and real_ext not in PACKAGE_EXTS:
        raise RelatedError(415, OPEN_REFUSED)
    command = (command_for or open_command)(real)
    if command is None:
        raise RelatedError(503, OPEN_NO_APP)
    try:
        (run or subprocess.run)(
            command, check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return  # 不等应用退出
    except OSError as error:
        raise RelatedError(503, OPEN_NO_APP) from error
