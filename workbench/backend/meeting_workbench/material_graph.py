"""关系图里的文件（第三期 3g）。都只查库，不读盘（给交付物算内容标识那一处除外）。

- 最近改过的文件：每个根目录、每个需求文件夹最多 6 个候选（画布上已有的由前端去掉）。根目录取整棵
  子树，但不含「声档会议记录/」、不含需求文件夹里的（那些挂在需求文件夹上）。需求文件夹按根目录加
  文件夹名在库里查。
- 根目录的内容计数取材料内容循环每轮算好放在内存里的，没有时为 null。
- 散放文件、文件夹面板的文件行按 (根目录, 相对路径) 补上 file_id；声档会议记录里的文件单独一个接口。
- 需求文件夹的文件列表：根目录在线、文件名索引扫完时查库，否则由调用方照旧读盘。
- 交付物连到文件：先按内容标识找活文件（同一个根目录的优先），再按根目录加相对路径；以前手填路径的
  按字符串前缀对根目录路径匹配（不 realpath、不读盘）。在根目录里找过、没找到的记 gone；不在任何根目录下
  的路径没法找，不记 gone。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

from .material_index import MATCH_ZONES
from .material_rules import CONTENT_EXTS, IWORK_EXTS
from .material_status import FILE_ERRORS, _iso_ns
from .materials import MAX_FOLDER_FILES, ROOT_ONLINE, volume_state

RECENT_CANDIDATES = 6
CARDS_FILES = 20
_ZONES_SQL = ", ".join(f"'{zone}'" for zone in MATCH_ZONES)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def rel_under(root_path: str, path: str) -> str | None:
    """path 在 root_path 里时回相对路径（根目录本身回 ""）；按字符串前缀比，不 realpath、不读盘。"""
    root = str(root_path).rstrip("/")
    target = str(path).rstrip("/")
    if target == root:
        return ""
    if root and target.startswith(root + "/"):
        return target[len(root) + 1 :]
    return None


def file_kind(row: dict[str, Any]) -> str:
    """和预览的 state.kind 一样的分法（活文件）：done、pending、waiting、unreadable、names_only。"""
    ext = str(row.get("ext") or "")
    if row.get("zone") == "cards" or (ext not in CONTENT_EXTS and ext not in IWORK_EXTS):
        return "names_only"
    error = row.get("content_error")
    if error in FILE_ERRORS:
        return "unreadable"
    state = row.get("content_state")
    if error == "io" or state is None or state == "pending":
        return "pending"
    if state in ("waiting", "unreadable"):
        return str(state)
    return "done"


_RECENT_SQL = f"""SELECT f.id, f.name, f.ext, f.dir_rel, f.rel_path, f.mtime_ns, f.zone, f.content_error,
                         c.state AS content_state
                    FROM material_files f
                    LEFT JOIN material_contents c ON c.content_key = f.content_key
                   WHERE f.root_id = ? AND f.gone_at IS NULL AND f.zone IN ({_ZONES_SQL})"""


def recent_files(
    connection: Any,
    root_id: int,
    *,
    under: str | None = None,
    exclude: Iterable[str] = (),
    limit: int = RECENT_CANDIDATES,
) -> list[dict[str, Any]]:
    sql = _RECENT_SQL
    params: list[Any] = [root_id]
    if under:
        sql += " AND f.rel_path LIKE ? ESCAPE '\\'"
        params.append(f"{_escape_like(under)}/%")
    for prefix in exclude:
        if prefix:
            sql += " AND f.rel_path NOT LIKE ? ESCAPE '\\'"
            params.append(f"{_escape_like(prefix)}/%")
    sql += " ORDER BY f.mtime_ns DESC, f.id DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "file_id": row["id"],
            "name": row["name"],
            "ext": row["ext"],
            "dir_rel": row["dir_rel"],
            "mtime": _iso_ns(row["mtime_ns"]),
            "state": file_kind(dict(row)),
        }
        for row in connection.execute(sql, params).fetchall()
    ]


def decorate_roots(
    connection: Any,
    project_id: str,
    result: dict[str, Any],
    *,
    counts: dict[int, dict[str, int]] | None,
) -> dict[str, Any]:
    """给 GET /api/graph/projects/{id}/roots 的结果补上 recent_files、content、散放文件的 file_id。"""
    roots = {int(item["root_id"]): str(item["path"]) for item in result.get("roots", [])}
    # 需求文件夹落在哪个根目录下、相对路径是什么
    folder_rel: dict[int, tuple[int, str]] = {}
    for folder in result.get("folders", []):
        for root_id, root_path in roots.items():
            rel = rel_under(root_path, str(folder["path"]))
            if rel:
                folder_rel[int(folder["folder_id"])] = (root_id, rel)
                break
    for item in result.get("roots", []):
        root_id = int(item["root_id"])
        excluded = [rel for owner, rel in folder_rel.values() if owner == root_id]
        item["recent_files"] = recent_files(connection, root_id, exclude=excluded)
        found = (counts or {}).get(root_id) if counts is not None else None
        item["content"] = dict(found) if found is not None else None
    for folder in result.get("folders", []):
        located = folder_rel.get(int(folder["folder_id"]))
        # 需求文件夹在哪个根目录里（不在任何根目录里的为 null，也就没有最近的文件）
        folder["root_id"] = located[0] if located is not None else None
        folder["recent_files"] = (
            recent_files(connection, located[0], under=located[1]) if located is not None else []
        )
    loose = result.get("loose") or {}
    # 散放文件的条目来自根目录缓存：复制一份再补，不改缓存里的
    recent = [dict(entry) for entry in loose.get("recent") or []]
    if loose:
        loose["recent"] = recent
    if recent:
        wanted: dict[tuple[int, str], dict[str, Any]] = {}
        for entry in recent:
            for root_id, root_path in roots.items():
                rel = rel_under(root_path, str(entry.get("path") or ""))
                if rel and "/" not in rel:
                    wanted[(root_id, rel)] = entry
                    break
        ids = file_ids(connection, list(wanted))
        for key, entry in wanted.items():
            entry["file_id"] = ids.get(key)
        for entry in recent:
            entry.setdefault("file_id", None)
    return result


def file_ids(connection: Any, keys: list[tuple[int, str]]) -> dict[tuple[int, str], int]:
    """(根目录, 相对路径) → 活文件 id；库里还没有的不在结果里。"""
    found: dict[tuple[int, str], int] = {}
    by_root: dict[int, list[str]] = {}
    for root_id, rel in keys:
        by_root.setdefault(root_id, []).append(rel)
    for root_id, rels in by_root.items():
        for start in range(0, len(rels), 500):
            part = rels[start : start + 500]
            marks = ", ".join("?" for _ in part)
            for row in connection.execute(
                f"""SELECT id, rel_path FROM material_files
                     WHERE root_id = ? AND gone_at IS NULL AND rel_path IN ({marks})""",
                [root_id, *part],
            ).fetchall():
                found[(root_id, row["rel_path"])] = int(row["id"])
    return found


def decorate_expand(connection: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """文件夹面板的文件行按 (root_id, rel_path) 到库里补 file_id；库里还没有的为 null。"""
    root_id = int(payload["root_id"])
    base = str(payload.get("dir") or "")
    keys = {}
    for item in payload.get("files", []):
        rel = f"{base}/{item['name']}" if base else str(item["name"])
        keys[(root_id, rel)] = item
    ids = file_ids(connection, list(keys))
    for key, item in keys.items():
        item["file_id"] = ids.get(key)
    return payload


def cards_files(
    connection: Any, project_id: str, *, limit: int = CARDS_FILES
) -> list[dict[str, Any]]:
    """声档会议记录里的文件：只查库，zone=cards，按修改时间最多 20 个，给文件名、file_id 和位置（不给预览）。"""
    rows = connection.execute(
        """SELECT f.id, f.name, f.rel_path, f.root_id, f.mtime_ns FROM material_files f
             JOIN project_material_roots r ON r.id = f.root_id
            WHERE r.project_id = ? AND f.gone_at IS NULL AND f.zone = 'cards'
            ORDER BY f.mtime_ns DESC, f.id DESC LIMIT ?""",
        (project_id, limit),
    ).fetchall()
    return [
        {
            "file_id": row["id"],
            "name": row["name"],
            "rel_path": row["rel_path"],
            "root_id": row["root_id"],
            "mtime": _iso_ns(row["mtime_ns"]),
        }
        for row in rows
    ]


def folder_files_from_index(
    connection: Any,
    project_id: str,
    folder_path: str,
    *,
    limit: int,
    offset: int,
    state_of: Callable[[str], str] = volume_state,
) -> dict[str, Any] | None:
    """需求文件夹的文件列表：按（本项目，文件夹所在的根目录）查库。根目录不在线、文件名索引还没扫完时
    回 None，调用方照旧读盘。返回结构和读盘版一样（relative_path 去掉文件夹前缀，同样最多
    MAX_FOLDER_FILES 个），每项多一个 file_id。"""
    roots = connection.execute(
        """SELECT r.id, r.path, s.state FROM project_material_roots r
             LEFT JOIN material_index_state s ON s.root_id = r.id
            WHERE r.project_id = ? ORDER BY r.id""",
        (project_id,),
    ).fetchall()
    for root in roots:
        rel = rel_under(str(root["path"]), folder_path)
        if not rel:
            continue
        if root["state"] != "done" or state_of(str(root["path"])) != ROOT_ONLINE:
            return None
        rows = connection.execute(
            f"""SELECT id, rel_path, size, mtime_ns FROM material_files
                 WHERE root_id = ? AND gone_at IS NULL AND zone IN ({_ZONES_SQL})
                   AND rel_path LIKE ? ESCAPE '\\'
                 ORDER BY rel_path LIMIT ?""",
            (root["id"], f"{_escape_like(rel)}/%", MAX_FOLDER_FILES + 1),
        ).fetchall()
        capped = len(rows) > MAX_FOLDER_FILES
        items = [
            {
                "relative_path": str(row["rel_path"])[len(rel) + 1 :],
                "size_bytes": row["size"],
                "modified_at": _iso_ns(row["mtime_ns"]),
                "file_id": row["id"],
            }
            for row in rows[:MAX_FOLDER_FILES]
        ]
        return {
            "exists": True,
            "total": len(items),
            "capped": capped,
            "items": items[offset : offset + limit],
        }
    return None


# ---------------------------------------------------------------------- 交付物连到文件


def _live_by_content(
    connection: Any, content_key: str, prefer_root: int | None
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT id, name FROM material_files
            WHERE content_key = ? AND gone_at IS NULL AND zone != 'cards'
            ORDER BY (root_id = ?) DESC, mtime_ns DESC, id DESC LIMIT 1""",
        (content_key, prefer_root if prefer_root is not None else -1),
    ).fetchone()
    return dict(row) if row is not None else None


def _live_at(connection: Any, root_id: int, rel_path: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT id, name FROM material_files WHERE root_id = ? AND rel_path = ? AND gone_at IS NULL",
        (root_id, rel_path),
    ).fetchone()
    return dict(row) if row is not None else None


def deliverable_file(
    connection: Any, deliverable: dict[str, Any], *, roots: list[tuple[int, str]] | None = None
) -> dict[str, Any]:
    """file 类交付物指向哪个文件：{file_id, name, gone}。别的类型回 {}。"""
    if deliverable.get("kind") != "file":
        return {}
    link = connection.execute(
        "SELECT content_key, root_id, rel_path FROM deliverable_files WHERE deliverable_id = ?",
        (deliverable["id"],),
    ).fetchone()
    found = None
    # 没在资料根目录里找过的（以前手填的、不在任何根目录下的路径）不算找不到，前端照旧给路径和［复制路径］
    looked = link is not None
    if link is not None:
        if link["content_key"]:
            found = _live_by_content(connection, link["content_key"], link["root_id"])
        if found is None and link["root_id"] is not None and link["rel_path"]:
            found = _live_at(connection, int(link["root_id"]), str(link["rel_path"]))
    else:
        # 以前手填的路径：按字符串前缀对根目录路径匹配
        if roots is None:
            roots = [
                (int(row["id"]), str(row["path"]))
                for row in connection.execute(
                    "SELECT id, path FROM project_material_roots ORDER BY id"
                ).fetchall()
            ]
        for root_id, root_path in roots:
            rel = rel_under(root_path, str(deliverable.get("url") or ""))
            if rel:
                looked = True
                found = _live_at(connection, root_id, rel)
                if found is not None:
                    break
    if found is not None:
        return {"file_id": int(found["id"]), "name": found["name"], "gone": False}
    name = PurePosixPath(str(deliverable.get("url") or "")).name or str(
        deliverable.get("title") or ""
    )
    return {"file_id": None, "name": name, "gone": looked}


def decorate_deliverables(
    connection: Any, deliverables: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """file 类交付物补上 file_id、name、gone（展开一场会、任务详情用）。"""
    roots = None
    for item in deliverables:
        if item.get("kind") != "file":
            continue
        if roots is None:
            roots = [
                (int(row["id"]), str(row["path"]))
                for row in connection.execute(
                    "SELECT id, path FROM project_material_roots ORDER BY id"
                ).fetchall()
            ]
        item.update(deliverable_file(connection, item, roots=roots))
    return deliverables
