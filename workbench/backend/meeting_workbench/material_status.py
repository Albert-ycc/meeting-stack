"""材料读了多少、为什么停、每个文件是什么状态（第三期 3e）。都只查库，不读盘。

- coverage：每个根目录一项，按文件数算（同一份内容放在两个地方算两个）。接口
  `GET /api/materials/coverage` 和命令 `meeting-workbench materials status` 共用；命令行读不到服务内存，
  paused 由调用方传。content_error=io 的算在还剩里（过一会儿还会再试）。
- unreadable_files：读不了的文件逐个列出，每页 100 个。
- file_preview：预览抽屉和关系图文件面板的数据。state.text 由 material_rules 的说法表生成，前端只显示它。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .material_rules import (
    CONTENT_EXTS,
    IMAGE_EXTS,
    IWORK_EXTS,
    LAYER_IMAGE,
    LAYER_MEDIA,
    LAYER_TEXT,
    MEDIA_EXTS,
    PDF_EXTS,
    PLAYABLE_TYPES,
    REASON_LABELS,
    STATE_TEXTS,
    TABLE_EXTS,
    TEXT_LAYER_EXTS,
    WAITING_TEXTS,
    meeting_audio_text,
    truncated_text,
    unreadable_text,
)
from . import relation_read
from .material_content import real_path_inside
from .materials import ROOT_ONLINE, volume_state

UNREADABLE_PAGE = 100
NOTE_KEYS = ("small_image", "no_text", "no_speech", "truncated", "meeting_audio")
FILE_ERRORS = ("permission", "corrupt", "unsupported")
PREVIEW_LINES = 40
PREVIEW_ROWS = 5
PREVIEW_SENTENCES = 20

_READABLE_SQL = ", ".join(f"'{ext}'" for ext in sorted(CONTENT_EXTS | IWORK_EXTS))


# ---------------------------------------------------------------------- 在等什么


def waiting_what(layer: str | None, engines: Any | None) -> str:
    """识别程序没装时缺的是哪个：vision、tesseract、textutil、ffmpeg、funasr。"""
    if layer == LAYER_TEXT:
        return "textutil"
    if layer == LAYER_MEDIA:
        from .ocr_engines import media_missing

        missing = media_missing(engines.tools()) if engines is not None else []
        return missing[0] if missing else "funasr"
    if layer == LAYER_IMAGE and engines is not None:
        setting = engines.setting()
        if setting == "tesseract":
            return "tesseract"
        if setting == "auto" and getattr(engines, "system", "darwin") != "darwin":
            return "tesseract"
    return "vision"


def waiting_hint(what: str, engines: Any | None) -> str:
    if what == "vision" and engines is not None and engines.build.failure():
        return STATE_TEXTS["vision_failed"]
    return WAITING_TEXTS.get(what, WAITING_TEXTS["funasr"])


# ---------------------------------------------------------------------- 覆盖率


def _empty_content(paused: str | None) -> dict[str, Any]:
    return {
        "total": 0,
        "done": 0,
        "pending": 0,
        "paused": paused,
        "waiting": [],
        "unreadable": {reason: 0 for reason in REASON_LABELS},
        "notes": {key: 0 for key in NOTE_KEYS},
        "names_only": {"cards": 0, "other": 0},
    }


def coverage(
    connection: Any,
    project_id: str | None = None,
    *,
    engines: Any | None = None,
    paused: str | None = None,
    state_of: Callable[[str], str] = volume_state,
) -> list[dict[str, Any]]:
    """每个根目录读了多少、为什么停、读不了的分类数。"""
    from .material_index import index_status

    roots = index_status(connection, project_id)
    if not roots:
        return []
    ids = [int(root["root_id"]) for root in roots]
    marks = ", ".join("?" for _ in ids)
    symlinks = {
        int(row["root_id"]): int(row["n"] or 0)
        for row in connection.execute(
            f"SELECT root_id, SUM(symlinks) AS n FROM material_dirs WHERE root_id IN ({marks}) GROUP BY root_id",
            ids,
        ).fetchall()
    }
    buckets = connection.execute(
        f"""SELECT f.root_id,
                   CASE WHEN f.zone = 'cards' THEN 'cards'
                        WHEN f.ext NOT IN ({_READABLE_SQL}) THEN 'other'
                        WHEN f.content_error = 'io' THEN 'pending'
                        WHEN f.content_error IS NOT NULL THEN 'unreadable'
                        WHEN c.content_key IS NULL THEN 'pending'
                        ELSE c.state END AS bucket,
                   CASE WHEN f.content_error IS NOT NULL AND f.content_error != 'io' THEN f.content_error
                        ELSE c.reason END AS reason,
                   c.note, c.layer, COUNT(*) AS n
              FROM material_files f
              LEFT JOIN material_contents c ON c.content_key = f.content_key
             WHERE f.gone_at IS NULL AND f.root_id IN ({marks})
             GROUP BY 1, 2, 3, 4, 5""",
        ids,
    ).fetchall()
    content = {root_id: _empty_content(paused) for root_id in ids}
    waiting: dict[int, dict[str, int]] = {root_id: {} for root_id in ids}
    for row in buckets:
        root_id = int(row["root_id"])
        count = int(row["n"])
        bucket = row["bucket"]
        entry = content[root_id]
        if bucket in {"cards", "other"}:
            entry["names_only"][bucket] += count
            continue
        entry["total"] += count
        if bucket == "done":
            entry["done"] += count
            if row["note"] in entry["notes"]:
                entry["notes"][row["note"]] += count
        elif bucket == "unreadable":
            reason = row["reason"] if row["reason"] in entry["unreadable"] else "corrupt"
            entry["unreadable"][reason] += count
        elif bucket == "waiting":
            what = waiting_what(row["layer"], engines)
            waiting[root_id][what] = waiting[root_id].get(what, 0) + count
        else:
            entry["pending"] += count
    result = []
    for root in roots:
        root_id = int(root["root_id"])
        entry = content[root_id]
        entry["waiting"] = [
            {"what": what, "files": files, "hint": waiting_hint(what, engines)}
            for what, files in sorted(waiting[root_id].items())
        ]
        if not entry["pending"]:
            entry["paused"] = None
        result.append(
            {
                "root_id": root_id,
                "project_id": root["project_id"],
                "path": root["path"],
                "state": root["state"],
                "online": state_of(str(root["path"])) == ROOT_ONLINE,
                "names": {
                    "files": root["files"],
                    "name_only_dirs": root["name_only_dirs"],
                    "symlinks": symlinks.get(root_id, 0),
                },
                "content": entry,
            }
        )
    return result


def root_counts(connection: Any) -> dict[int, dict[str, int]]:
    """每个根目录：files 活文件数（文件名都算）、done 内容读完的、unreadable 读不了的。关系图的项目面板用；
    材料内容循环每轮算一次放在内存里。分法同 coverage。"""
    errors = ", ".join(f"'{error}'" for error in FILE_ERRORS)
    rows = connection.execute(
        f"""SELECT f.root_id, COUNT(*) AS files,
                   SUM(CASE WHEN f.zone != 'cards' AND f.ext IN ({_READABLE_SQL})
                             AND f.content_error IS NULL AND c.state = 'done' THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN f.zone != 'cards' AND f.ext IN ({_READABLE_SQL})
                             AND (f.content_error IN ({errors})
                                  OR (f.content_error IS NULL AND c.state = 'unreadable')) THEN 1 ELSE 0 END)
                       AS unreadable
              FROM material_files f
              LEFT JOIN material_contents c ON c.content_key = f.content_key
             WHERE f.gone_at IS NULL
             GROUP BY f.root_id"""
    ).fetchall()
    return {
        int(row["root_id"]): {
            "files": int(row["files"] or 0),
            "done": int(row["done"] or 0),
            "unreadable": int(row["unreadable"] or 0),
        }
        for row in rows
    }


def _number(value: int) -> str:
    return f"{value:,}"


def render_status(roots: list[dict[str, Any]], project_names: dict[str, str] | None = None) -> str:
    """命令 materials status 的文字版：每个根目录一行。"""
    if not roots:
        return "还没有挂材料根目录。"
    lines = []
    for root in roots:
        content = root["content"]
        head = f"{root['path']}"
        if project_names and root["project_id"] in project_names:
            head = f"［{project_names[root['project_id']]}］{head}"
        parts = [
            f"文件名 {_number(root['names']['files'])} 个",
            f"已读 {_number(content['done'])} 个",
            f"还剩 {_number(content['pending'])} 个",
        ]
        unreadable = {reason: count for reason, count in content["unreadable"].items() if count}
        if unreadable:
            detail = "、".join(
                f"{REASON_LABELS[reason]} {count}" for reason, count in unreadable.items()
            )
            parts.append(f"读不了 {_number(sum(unreadable.values()))} 个（{detail}）")
        if content["waiting"]:
            parts.append(
                "在等："
                + "；".join(f"{item['what']} {item['files']} 个" for item in content["waiting"])
            )
        if not root["online"]:
            parts.append("资料盘未连接")
        lines.append(f"{head}\n  " + " · ".join(parts))
        for item in content["waiting"]:
            lines.append(f"  {item['hint']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- 读不了的列表


def unreadable_files(
    connection: Any,
    project_id: str,
    *,
    root_id: int | None = None,
    reason: str | None = None,
    offset: int = 0,
    limit: int = UNREADABLE_PAGE,
) -> dict[str, Any]:
    """读不了的文件，按根目录和相对路径排，每页 100 个。"""
    reason_sql = """CASE WHEN f.content_error IN ('permission', 'corrupt', 'unsupported') THEN f.content_error
                         ELSE c.reason END"""
    clauses = [
        "r.project_id = ?",
        "f.gone_at IS NULL",
        "f.zone != 'cards'",
        "(f.content_error IN ('permission', 'corrupt', 'unsupported') OR c.state = 'unreadable')",
    ]
    params: list[Any] = [project_id]
    if root_id is not None:
        clauses.append("f.root_id = ?")
        params.append(root_id)
    if reason is not None:
        clauses.append(f"{reason_sql} = ?")
        params.append(reason)
    where = " AND ".join(clauses)
    base = f"""FROM material_files f
               JOIN project_material_roots r ON r.id = f.root_id
               LEFT JOIN material_contents c ON c.content_key = f.content_key
              WHERE {where}"""
    total = int(connection.execute(f"SELECT COUNT(*) AS n {base}", params).fetchone()["n"])
    rows = connection.execute(
        f"""SELECT f.id, f.name, f.rel_path, f.root_id, r.path AS root_path,
                   {reason_sql} AS reason,
                   COALESCE(f.content_checked_at, c.updated_at) AS checked_at
              {base}
             ORDER BY f.root_id, f.rel_path
             LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()
    items = [
        {
            "file_id": row["id"],
            "name": row["name"],
            "rel_path": row["rel_path"],
            "path": f"{str(row['root_path']).rstrip('/')}/{row['rel_path']}",
            "root_id": row["root_id"],
            "reason": row["reason"] if row["reason"] in REASON_LABELS else "corrupt",
            "checked_at": row["checked_at"],
        }
        for row in rows
    ]
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {"items": items, "total": total, "next_offset": next_offset}


# ---------------------------------------------------------------------- 一个文件


def _iso_ns(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1e9, tz=UTC).isoformat()


def file_row(connection: Any, file_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT f.*, r.path AS root_path, r.project_id, p.name AS project_name
             FROM material_files f
             JOIN project_material_roots r ON r.id = f.root_id
             JOIN projects p ON p.id = r.project_id
            WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def file_state(
    connection: Any,
    row: dict[str, Any],
    content: dict[str, Any] | None,
    *,
    online: bool,
    paused: str | None,
    engines: Any | None = None,
) -> dict[str, Any]:
    """state：kind（done、pending、waiting、unreadable、names_only、gone）和给人看的那一句。"""
    state: dict[str, Any] = {
        "kind": "pending",
        "reason": None,
        "note": None,
        "what": None,
        "paused": None,
        "meeting": None,
        "text": "",
    }
    ext = str(row.get("ext") or "")
    error = row.get("content_error")
    if row.get("gone_at") is not None:
        state.update(kind="gone", text=STATE_TEXTS["gone"])
    elif row.get("zone") == "cards":
        state.update(kind="names_only", text=STATE_TEXTS["cards"])
    elif ext not in CONTENT_EXTS and ext not in IWORK_EXTS:
        state.update(kind="names_only", text=STATE_TEXTS["names_only"])
    elif error in FILE_ERRORS:
        state.update(kind="unreadable", reason=error, text=unreadable_text(error))
    elif content is None or error == "io" or content["state"] == "pending":
        state["kind"] = "pending"
        if not online:
            state["text"] = STATE_TEXTS["offline"]
        elif paused:
            state.update(paused=paused, text=STATE_TEXTS["paused"])
        elif error == "io" or (content is not None and content.get("reason") == "timeout"):
            state["text"] = STATE_TEXTS["retrying"]
        else:
            state["text"] = STATE_TEXTS["pending"]
    elif content["state"] == "waiting":
        what = waiting_what(content.get("layer"), engines)
        state.update(
            kind="waiting", what=what, note="engine_missing", text=waiting_hint(what, engines)
        )
    elif content["state"] == "unreadable":
        reason = content.get("reason") if content.get("reason") in REASON_LABELS else "corrupt"
        state.update(kind="unreadable", reason=reason, text=unreadable_text(reason))
    else:
        note = content.get("note")
        state.update(kind="done", note=note)
        if note == "meeting_audio" and content.get("meeting_id"):
            meeting = connection.execute(
                "SELECT id, title FROM meetings WHERE id = ?", (content["meeting_id"],)
            ).fetchone()
            if meeting is not None:
                state["meeting"] = {"id": meeting["id"], "title": meeting["title"]}
            state["text"] = meeting_audio_text(meeting["title"] if meeting is not None else None)
        elif note == "truncated":
            state["text"] = truncated_text(content.get("layer"), content.get("pages"))
        elif note in STATE_TEXTS:
            state["text"] = STATE_TEXTS[note]
        elif not online:
            state["text"] = STATE_TEXTS["offline"]
    return state


def _preview_kind(ext: str) -> str:
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in MEDIA_EXTS:
        return "media"
    if ext in TABLE_EXTS:
        return "table"
    if ext in TEXT_LAYER_EXTS:
        return "text"
    return "none"


def _first_lines(texts: list[str], limit: int = PREVIEW_LINES) -> tuple[list[str], bool]:
    lines: list[str] = []
    for text in texts:
        for line in str(text).split("\n"):
            if len(lines) >= limit:
                return lines, True
            lines.append(line)
    return lines, False


def _drop_adjacent_dupes(lines: list[str]) -> list[str]:
    """紧挨着完全一样的整行只留一份：PDF 文字层和认字结果叠出重复行时用（见 D8），
    只挡紧挨着的，隔开的重复（比如真的重复出现的表头）不动。"""
    result: list[str] = []
    for line in lines:
        if line.strip() and result and result[-1] == line:
            continue
        result.append(line)
    return result


def file_preview_block(
    connection: Any,
    row: dict[str, Any],
    content: dict[str, Any] | None,
    *,
    online: bool,
) -> dict[str, Any]:
    """preview：kind（text、table、image、pdf、media、none）和能画的东西。文字都从库里的片段取。"""
    file_id = int(row["id"])
    ext = str(row.get("ext") or "")
    gone = row.get("gone_at") is not None
    kind = "none" if gone or row.get("zone") == "cards" else _preview_kind(ext)
    preview: dict[str, Any] = {
        "kind": kind,
        "lines": [],
        "more": False,
        "rows": [],
        "sheet": None,
        "image_url": None,
        "page_url": None,
        "media_url": None,
        "playable": False,
        "duration_ms": None,
        "transcript": [],
    }
    if kind == "none":
        return preview
    reachable = online and not gone
    done = content is not None and content.get("state") == "done" and not row.get("content_error")
    chunks: list[dict[str, Any]] = []
    if done:
        chunks = [
            dict(item)
            for item in connection.execute(
                """SELECT loc, start_ms, end_ms, text FROM material_chunks
                    WHERE content_key = ? ORDER BY ordinal LIMIT ?""",
                (row["content_key"], PREVIEW_SENTENCES if kind == "media" else PREVIEW_LINES),
            ).fetchall()
        ]
    if kind == "image":
        preview["image_url"] = f"/api/materials/files/{file_id}/thumb" if reachable else None
        preview["lines"], preview["more"] = _first_lines([chunk["text"] for chunk in chunks])
    elif kind == "pdf":
        preview["page_url"] = f"/api/materials/files/{file_id}/page1" if reachable else None
        first_page = [chunk["text"] for chunk in chunks if chunk["loc"] in (None, "第 1 页")]
        lines, more = _first_lines(first_page)
        preview["lines"], preview["more"] = _drop_adjacent_dupes(lines), more
    elif kind == "media":
        playable = ext in PLAYABLE_TYPES
        preview["playable"] = playable
        preview["media_url"] = (
            f"/api/materials/files/{file_id}/media" if playable and reachable else None
        )
        preview["duration_ms"] = content.get("duration_ms") if content else None
        preview["transcript"] = [
            {"start_ms": chunk["start_ms"], "end_ms": chunk["end_ms"], "text": chunk["text"]}
            for chunk in chunks
        ]
    elif kind == "table":
        if chunks:
            sheet = chunks[0]["loc"]
            same_sheet = [chunk["text"] for chunk in chunks if chunk["loc"] == sheet]
            lines, more = _first_lines(same_sheet, PREVIEW_ROWS)
            preview["sheet"] = sheet
            preview["rows"] = [line.split("\t") for line in lines]
            preview["more"] = more
    else:
        preview["lines"], preview["more"] = _first_lines([chunk["text"] for chunk in chunks])
    return preview


def file_mentions_for_preview(
    connection: Any,
    file_id: int,
    *,
    quotes: Callable[[str, list[int]], dict[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """在哪几场会上被提到（有效的字面和放宽的提到，relation_read 读），新的 40 场，带那场会的录音地址。
    场数另用 relation_read.file_mention_counts 数（预览的 mentioned_meetings）。"""
    rows = relation_read.file_mention_meetings(connection, file_id, audio=True)
    result = []
    for row in rows:
        first_ms = row["first_ms"]
        quote = ""
        if row.get("relation_id") is not None and row.get("quote"):
            quote = row["quote"]  # 放宽行（4b）存着那句原话
        elif quotes is not None and row["source"] == "transcript" and first_ms is not None:
            quote = quotes(row["meeting_id"], [int(first_ms)]).get(int(first_ms), "")
        result.append(
            {
                "meeting_id": row["meeting_id"],
                "title": row["title"],
                "date": row["date"],
                "count": int(row["count"]),
                "first_ms": first_ms,
                "quote": quote,
                "audio_url": f"/api/media/{row['audio_id']}" if row["audio_id"] else None,
                # 4b：放宽行的 relation_id、说法和对上的方式（字面行都是 None）
                **relation_read.loose_fields(row),
            }
        )
    return result


def file_deliverables(connection: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """这个文件是哪些任务的交付物：按内容标识、按位置，旧的手填路径也算。"""
    path = f"{str(row['root_path']).rstrip('/')}/{row['rel_path']}"
    rows = connection.execute(
        """SELECT DISTINCT d.id AS deliverable_id, d.task_id, t.title, t.status
             FROM deliverables d
             JOIN tasks t ON t.id = d.task_id
             LEFT JOIN deliverable_files df ON df.deliverable_id = d.id
            WHERE (df.content_key IS NOT NULL AND df.content_key = ?)
               OR (df.root_id = ? AND df.rel_path = ?)
               OR (d.kind = 'file' AND d.url = ?)
            ORDER BY d.created_at DESC, d.id DESC""",
        (row.get("content_key"), row["root_id"], row["rel_path"], path),
    ).fetchall()
    return [dict(item) for item in rows]


def _content_of(connection: Any, row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("content_key"):
        return None
    found = connection.execute(
        "SELECT * FROM material_contents WHERE content_key = ?", (row["content_key"],)
    ).fetchone()
    return dict(found) if found is not None else None


def state_for_row(
    connection: Any,
    row: dict[str, Any],
    *,
    engines: Any | None = None,
    paused: str | None = None,
    state_of: Callable[[str], str] = volume_state,
) -> dict[str, Any]:
    """关系图文件面板（GET /api/graph/files/{id}）的 state，和预览的一样。"""
    online = state_of(str(row["root_path"])) == ROOT_ONLINE
    return file_state(
        connection, row, _content_of(connection, row), online=online, paused=paused, engines=engines
    )


def file_preview(
    connection: Any,
    file_id: int,
    *,
    engines: Any | None = None,
    paused: str | None = None,
    state_of: Callable[[str], str] = volume_state,
    quotes: Callable[[str, list[int]], dict[int, str]] | None = None,
    parts: str | None = None,
    can_reveal: bool = False,
    passage_key: str | None = None,
    passage_ordinal: int | None = None,
) -> dict[str, Any] | None:
    """GET /api/materials/files/{id}/preview。文件不在索引里回 None。4d：给了 passage_ordinal 时加 passage
    （parts=preview 时也加；不给时键不变）；完整结果另加 related_meetings 和 file.can_open。4e：完整结果
    另加 questions（在问的可能过时和产出），parts=preview 时不给。"""
    from . import related_read

    row = file_row(connection, file_id)
    if row is None:
        return None
    content = _content_of(connection, row)
    root_path = str(row["root_path"]).rstrip("/")
    online = state_of(str(row["root_path"])) == ROOT_ONLINE
    folder = f"{root_path}/{row['dir_rel']}" if row["dir_rel"] else root_path
    result: dict[str, Any] = {
        "file": {
            "id": row["id"],
            "name": row["name"],
            "ext": row["ext"],
            "rel_path": row["rel_path"],
            "root_id": row["root_id"],
            "folder_path": folder,
            "path": f"{root_path}/{row['rel_path']}",
            "size": row["size"],
            "modified_at": _iso_ns(row["mtime_ns"]),
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "root_online": online,
            "gone": row["gone_at"] is not None,
        },
        "state": file_state(
            connection, row, content, online=online, paused=paused, engines=engines
        ),
        "preview": file_preview_block(connection, row, content, online=online),
    }
    if passage_ordinal is not None:
        # 文件不见了就和 preview 一样不露正文：片段要 30 天才清，可能是顺着链接读进来的根目录外内容
        result["passage"] = (
            None
            if row["gone_at"] is not None
            else related_read.passage(connection, row, passage_key, passage_ordinal)
        )
    if parts == "preview":
        return result
    # 4d：［用本机应用打开］只在本机、白名单里的扩展名、文件没有不见时出现（前端只看这个字段）
    result["file"]["can_open"] = related_read.can_open(
        row["ext"], local=can_reveal, gone=row["gone_at"] is not None
    )
    result["related_meetings"] = related_read.related_meetings(connection, file_id)
    result["mentions"] = file_mentions_for_preview(connection, file_id, quotes=quotes)
    # 先数再取：列表最多 40 场，场数不受它限制（抽屉标题用它）
    result["mentioned_meetings"] = relation_read.file_mention_counts(connection, [file_id]).get(
        file_id, 0
    )
    result["deliverables"] = file_deliverables(connection, row)
    # 4e：在问的可能过时和产出，放在交付物后面；有在问的影响时 stat 一次，文件变了就先不给影响的问题
    from .affects import guarded_questions

    result["questions"] = guarded_questions(
        relation_read.file_questions(connection, file_id), row, state_of
    )
    result["can_reveal"] = can_reveal
    return result


PACKAGE_DIR_EXTS = frozenset({"key", "pages", "numbers"})


def resolve_file(
    connection: Any, file_id: int, *, state_of: Callable[[str], str] = volume_state
) -> tuple[str, Any]:
    """thumb、page1、media 读盘前的检查：只按 file_id 找根目录和相对路径，realpath 必须还在根目录里。
    返回 (状态, 路径或行)：ok、missing（没有这行或已不见）、offline（盘不在）、outside（指到根目录外）。"""
    row = file_row(connection, file_id)
    if row is None or row.get("gone_at") is not None:
        return "missing", None
    if state_of(str(row["root_path"])) != ROOT_ONLINE:
        return "offline", row
    target = real_path_inside(
        str(row["root_path"]), os.path.join(str(row["root_path"]), *str(row["rel_path"]).split("/"))
    )
    if target is None:
        return "outside", row
    # 4d：key、pages、numbers 可能是目录形式的包
    if not (
        os.path.isfile(target)
        or (str(row["ext"] or "").lower() in PACKAGE_DIR_EXTS and os.path.isdir(target))
    ):
        return "missing", row
    row["real_path"] = target
    return "ok", row
