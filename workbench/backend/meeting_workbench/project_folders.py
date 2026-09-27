"""第二期 2a：项目总文件夹。

- 项目总文件夹：你放项目文件夹的那个位置，存在 app_state（键见 folder_scan.PARENT_KEY）。新项目的文件夹
  放在这里；没设时给推荐（已挂根目录里最常见的父目录），推荐不会自动写入。
- 还没挂的文件夹：都从 RootsCache 的目录清单里出，请求里不读盘；「已挂」「容器」「被拒过」「受保护」
  在请求时拿库里当前的数据现算，写完立刻生效。
- 认领：总文件夹下还没挂的一级文件夹，逐行给默认动作（挂到已有项目 / 建成项目），整批处理。
- 盘不在时照常建项目：记一条待补建的文件夹，插上盘后后台补建并挂上。
- 文件夹改名后找回：根目录「找不到」时，按证据给候选，［是它］一起改掉嵌在里面的路径。
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .db import Database, utc_now
from .folder_scan import PARENT_KEY, browse_root, listing_targets, parent_setting
from .materials import (
    CARDS_DIR_NAME,
    ROOT_MISSING,
    ROOT_ONLINE,
    ROOT_VOLUME_OFFLINE,
    _protected_roots,
    _within,
    resolve_within,
    validate_new_root,
    volume_state,
)
from .project_names import _match_kind, _skipped_folder, also_entries, sanitize_folder_name
from .project_profile import GENERIC_FOLDER_NAMES, light_key, norm_key
from .service import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

CHECKING = "checking"
UNCLAIMED = "unclaimed"
RENAME = "rename"
# 默认动作的几种情况，前端按它写说明。
KIND_EXACT = "exact"  # 和一个还没挂文件夹的项目同名：挂到它（默认勾选）
KIND_EXACT_MOUNTED = "exact_mounted"  # 和一个已经挂了文件夹的项目同名：再挂这个？
KIND_SIMILAR = "similar"  # 名字相近：挂到它？
KIND_GENERIC = "generic"  # 通用名：看起来不是项目
KIND_NEW = "new"  # 和谁都不像：建成项目
_KIND_ORDER = {KIND_EXACT: 0, KIND_EXACT_MOUNTED: 1, KIND_SIMILAR: 2, KIND_NEW: 3, KIND_GENERIC: 4}
_GENERIC_KEYS = frozenset(light_key(name) for name in GENERIC_FOLDER_NAMES)
# 改名找回的证据强弱。
EVIDENCE_CARDS = 3
EVIDENCE_CHILDREN = 2
EVIDENCE_NAME = 1
MIN_CHILD_MATCHES = 3
_FORBIDDEN_PARENTS = ("/", "/Volumes", "/Users")


# 认领时新建项目用的颜色，和前端 ProjectFormModal 的色块一致，按项目个数轮着用。
PROJECT_COLORS = (
    "#3ecf8e",
    "#2c8d83",
    "#3f51b5",
    "#5090ff",
    "#7c3aed",
    "#831fa8",
    "#8c772c",
    "#f0783b",
)


def next_color(connection: Any) -> str:
    count = connection.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    return PROJECT_COLORS[count % len(PROJECT_COLORS)]


def _set_state(connection: Any, key: str, value: str) -> None:
    connection.execute(
        """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, utc_now()),
    )


# ---------------------------------------------------------------------- 还没挂的文件夹


class ClaimIndex:
    """请求时现算「这个文件夹还能不能算没挂」要用的库里数据，一次请求查一遍。"""

    def __init__(self, connection: Any, settings: Settings, cache: Any = None):
        self.settings = settings
        self.roots = [
            (Path(row["path"]), row["project_id"])
            for row in connection.execute(
                "SELECT path, project_id FROM project_material_roots"
            ).fetchall()
        ]
        self.root_paths = {str(path) for path, _project in self.roots}
        self.folders = {
            row["path"]
            for row in connection.execute("SELECT path FROM requirement_folders").fetchall()
        }
        self.declined = {
            row["path"]
            for row in connection.execute(
                "SELECT path FROM folder_declines WHERE kind=?", (UNCLAIMED,)
            ).fetchall()
        }
        # 有强证据是某个「找不到」的根目录改名后的样子：该走「是不是它」，不该算成没挂的。
        self.renamed: set[str] = set()
        if cache is not None:
            for root in missing_roots(connection, cache):
                for candidate in rename_candidates_for(connection, cache, root) or []:
                    if candidate["strength"] >= EVIDENCE_CHILDREN:
                        self.renamed.add(candidate["path"])

    def free(self, path: str | Path) -> bool:
        candidate = Path(path)
        text = str(candidate)
        if text in self.root_paths or text in self.folders or text in self.declined:
            return False
        if text in self.renamed:
            return False
        # 里面有已挂根目录的是个容器（按路径分段比，「云图AI」不会算成「云图AI二期」的上级）
        if any(root.is_relative_to(candidate) for root, _project in self.roots):
            return False
        return not _skipped_folder(candidate, self.settings)


def _listing_entries(
    cache: Any, targets: list[str], *, direct_only: bool = False
) -> list[dict[str, Any]] | None:
    """缓存里这些目录的文件夹；有一个还没读到就返回 None（前端显示「正在看…」并轮询）。"""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        listing = cache.listing(target)
        if listing is None:
            return None
        if listing["state"] != ROOT_ONLINE:
            continue
        for entry in listing["entries"]:
            if direct_only and Path(entry["path"]).parent != Path(target):
                continue
            if entry["path"] not in seen:
                seen.add(entry["path"])
                entries.append(entry)
    return entries


def free_folders(
    connection: Any, settings: Settings, cache: Any
) -> list[dict[str, Any]] | None:
    """新建项目弹窗、冷启动横幅用的「还没挂的文件夹」：项目总文件夹和已挂根目录的父目录各一层
    （都没有时浏览根往下两层）。缓存没好时返回 None，并在后台刷新一轮。"""
    targets, _depth = listing_targets(connection, settings)
    entries = _listing_entries(cache, targets)
    if entries is None:
        cache.refresh_in_background()
        return None
    index = ClaimIndex(connection, settings, cache)
    return [entry for entry in entries if index.free(entry["path"])]


# ---------------------------------------------------------------------- 项目总文件夹


def validate_parent(connection: Any, settings: Settings, raw: str) -> str:
    """写入前的校验；返回解析后的绝对路径。盘没插允许保存，路径不存在不允许。"""
    resolved = resolve_within(settings.material_browse_root, raw)
    if str(resolved) in _FORBIDDEN_PARENTS or resolved == Path.home().resolve():
        raise ValueError("不能用整个磁盘、/Volumes 或用户主目录，请选放项目文件夹的那一层")
    for protected, _message in _protected_roots(settings):
        try:
            protected_real = protected.expanduser().resolve()
        except OSError:
            continue
        if _within(resolved, protected) or protected_real.is_relative_to(resolved):
            raise ValueError("项目总文件夹不能是、也不能包含声档自己的归档、暂存或数据目录")
    problem = _inside_root(connection, resolved)
    if problem:
        raise ValueError(problem)
    state = volume_state(resolved)
    if state == ROOT_MISSING:
        raise ValueError("这个文件夹不存在")
    return str(resolved)


def _inside_root(connection: Any, path: Path) -> str | None:
    for row in connection.execute(
        """SELECT r.path, p.name FROM project_material_roots r
             JOIN projects p ON p.id = r.project_id ORDER BY r.created_at, r.id"""
    ).fetchall():
        root = Path(row["path"])
        if path == root:
            return f"这个文件夹已经是项目「{row['name']}」的材料文件夹"
        if path.is_relative_to(root):
            return f"这个文件夹在项目「{row['name']}」的材料文件夹里面"
    return None


def set_parent(db: Database, settings: Settings, raw: str | None) -> str | None:
    with db.transaction() as connection:
        if not raw:
            connection.execute("DELETE FROM app_state WHERE key=?", (PARENT_KEY,))
            return None
        path = validate_parent(connection, settings, raw)
        _set_state(connection, PARENT_KEY, path)
        return path


def suggested_parent(connection: Any, settings: Settings) -> dict[str, Any] | None:
    """已挂根目录里最常见的父目录（并列时取最近挂的）；一个根目录都没有时不推荐。"""
    base = browse_root(settings)
    counts: Counter[str] = Counter()
    order: list[str] = []
    total = 0
    for row in connection.execute(
        "SELECT path FROM project_material_roots ORDER BY created_at DESC, id DESC"
    ).fetchall():
        total += 1
        parent = Path(row["path"]).parent
        if parent == base or parent.is_relative_to(base):
            counts[str(parent)] += 1
            if str(parent) not in order:
                order.append(str(parent))
    if not counts:
        return None
    best = max(order, key=lambda parent: (counts[parent], -order.index(parent)))
    return {"path": best, "count": counts[best], "total": total}


def folder_target(connection: Any, settings: Settings) -> dict[str, Any]:
    """新项目的文件夹放哪：项目总文件夹 → 推荐位置 → 都没有就不建。"""
    parent = parent_setting(connection)
    if parent and _read_problem(connection, settings, parent) is None:
        return {"parent": parent, "source": "setting"}
    suggested = suggested_parent(connection, settings)
    if suggested:
        return {"parent": suggested["path"], "source": "suggested"}
    return {"parent": None, "source": None}


def _read_problem(connection: Any, settings: Settings, path: str) -> tuple[str, str] | None:
    """读的时候再按浏览根校验一次：浏览根换了、或者后来在它里面挂了根目录。"""
    try:
        resolve_within(settings.material_browse_root, path)
    except ValueError as error:
        return "invalid", str(error)
    problem = _inside_root(connection, Path(path))
    if problem:
        return "conflict", problem
    return None


def parent_status(connection: Any, settings: Settings, cache: Any) -> dict[str, Any]:
    """GET /api/settings/project-parent。"""
    path = parent_setting(connection)
    result: dict[str, Any] = {
        "path": path,
        "state": None,
        "reason": None,
        "suggested": None,
        "unclaimed": {"state": "unset", "folders": [], "total": 0},
    }
    if path is None:
        result["suggested"] = suggested_parent(connection, settings)
        return result
    problem = _read_problem(connection, settings, path)
    if problem is not None:
        result["state"], result["reason"] = problem
        result["unclaimed"]["state"] = problem[0]
        return result
    listing = cache.listing(path)
    if listing is None:
        cache.refresh_in_background()
        result["state"] = CHECKING
        result["unclaimed"]["state"] = CHECKING
        return result
    result["state"] = listing["state"]
    if listing["state"] != ROOT_ONLINE:
        result["unclaimed"]["state"] = listing["state"]
        return result
    folders = unclaimed_rows(connection, settings, cache, path)
    result["unclaimed"] = {"state": "ready", "folders": folders or [], "total": len(folders or [])}
    return result


def _project_rows(connection: Any) -> list[dict[str, Any]]:
    rows = []
    roots: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT project_id, path FROM project_material_roots ORDER BY created_at, id"
    ).fetchall():
        roots.setdefault(row["project_id"], []).append(row["path"])
    for row in connection.execute(
        "SELECT id, name, also_names FROM projects ORDER BY created_at, id"
    ).fetchall():
        names = [row["name"], *(entry["name"] for entry in also_entries(row["also_names"]))]
        rows.append(
            {
                "id": row["id"],
                "name": row["name"],
                "names": names,
                "light_keys": {light_key(name) for name in names if light_key(name)},
                "roots": roots.get(row["id"], []),
            }
        )
    return rows


def default_action(
    folder_name: str, projects: list[dict[str, Any]], taken: set[str]
) -> dict[str, Any]:
    """一行没挂的文件夹默认怎么处理。taken 是已经有一行默认勾选的项目（每个项目最多勾一行）。"""
    key = light_key(folder_name)
    if folder_name in GENERIC_FOLDER_NAMES or key in _GENERIC_KEYS or not key:
        return {"kind": KIND_GENERIC, "action": None, "project": None, "checked": False}
    exact = next((project for project in projects if key in project["light_keys"]), None)
    if exact is not None:
        if exact["roots"]:
            return {
                "kind": KIND_EXACT_MOUNTED,
                "action": "mount",
                "project": exact,
                "checked": False,
            }
        checked = exact["id"] not in taken
        if checked:
            taken.add(exact["id"])
        return {"kind": KIND_EXACT, "action": "mount", "project": exact, "checked": checked}
    folder_key = norm_key(folder_name)
    similar = next(
        (
            project
            for project in projects
            if _match_kind(folder_name, project["names"]) is not None
            or (folder_key and any(norm_key(name) == folder_key for name in project["names"]))
        ),
        None,
    )
    if similar is not None:
        return {"kind": KIND_SIMILAR, "action": "mount", "project": similar, "checked": False}
    return {"kind": KIND_NEW, "action": "create", "project": None, "checked": False}


def unclaimed_rows(
    connection: Any, settings: Settings, cache: Any, parent: str
) -> list[dict[str, Any]] | None:
    entries = _listing_entries(cache, [parent], direct_only=True)
    if entries is None:
        return None
    index = ClaimIndex(connection, settings, cache)
    projects = _project_rows(connection)
    taken: set[str] = set()
    rows = []
    free = sorted(
        (entry for entry in entries if index.free(entry["path"])),
        key=lambda entry: entry["name"],
    )
    for entry in free:
        default = default_action(entry["name"], projects, taken)
        project = default["project"]
        rows.append(
            {
                "path": entry["path"],
                "name": entry["name"],
                "modified_at": entry["mtime"],
                "kind": default["kind"],
                "action": default["action"],
                "project_id": project["id"] if project else None,
                "project_name": project["name"] if project else None,
                "project_roots": project["roots"] if project else [],
                "checked": default["checked"],
            }
        )
    rows.sort(key=lambda row: (_KIND_ORDER[row["kind"]], row["name"]))
    return rows


# ---------------------------------------------------------------------- 认领


def claim(
    db: Database,
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    create_project: Callable[..., dict[str, Any]],
    mount: Callable[[str, str], dict[str, Any]],
    after: Callable[[list[str]], dict[str, int]],
) -> dict[str, Any]:
    """整批处理勾选的文件夹：挂到已有项目或建成项目，逐项返回结果。

    create_project 不带 force（除非这一行你选了「仍然新建」），近似重名时这一行问「是不是它」。
    整批处理完后由 after 刷新一次快照、每个项目各补写一次卡片，返回补写张数。
    """
    from .project_names import SimilarProjectError

    results: list[dict[str, Any]] = []
    touched: list[str] = []
    created = mounted = 0
    flagged: list[str] = []
    for item in items:
        path = str(item.get("path") or "")
        action = item.get("action")
        result: dict[str, Any] = {"path": path, "action": action, "ok": False}
        try:
            if action == "mount":
                project_id = str(item.get("project_id") or "")
                if not project_id:
                    raise ValueError("要挂到哪个项目？")
                root = mount(project_id, path)
                result.update(
                    ok=True,
                    project_id=project_id,
                    project_name=_project_name(db, project_id),
                    nested=root.get("nested") or [],
                )
                mounted += 1
                touched.append(project_id)
            elif action == "create":
                name = Path(path).name
                detail = create_project(
                    name=name, material_roots=[path], force=bool(item.get("force"))
                )
                result.update(
                    ok=True,
                    project_id=detail["id"],
                    project_name=detail["name"],
                    nested=[],
                )
                created += 1
                touched.append(detail["id"])
                flagged.extend(detail.get("needs_review_meeting_ids") or [])
            else:
                raise ValueError("action 只能是 mount 或 create")
        except SimilarProjectError as error:
            result.update(error=str(error), suggestion=error.suggestion)
        except ConflictError as error:
            same = db.query_one("SELECT id, name FROM projects WHERE name=?", (Path(path).name,))
            result.update(error=str(error), suggestion=same)
        except (ValueError, NotFoundError) as error:
            result["error"] = str(error)
        results.append(result)
    written = after(list(dict.fromkeys(touched))) if touched else {}
    for result in results:
        if result["ok"]:
            result["cards_written"] = written.get(result["project_id"], 0)
    return {
        "items": results,
        "created": created,
        "mounted": mounted,
        "cards_written": sum(written.values()),
        "needs_review": len(set(flagged)),
    }


def _project_name(db: Database, project_id: str) -> str | None:
    row = db.query_one("SELECT name FROM projects WHERE id=?", (project_id,))
    return row["name"] if row else None


def decline(db: Database, path: str, *, kind: str = UNCLAIMED, scope: str = "") -> None:
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO folder_declines(kind, scope, path, decided_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(kind, scope, path) DO NOTHING""",
            (kind, scope, path, utc_now()),
        )


def undecline(db: Database, path: str, *, kind: str = UNCLAIMED, scope: str = "") -> bool:
    return bool(
        db.execute_rowcount(
            "DELETE FROM folder_declines WHERE kind=? AND scope=? AND path=?", (kind, scope, path)
        )
    )


# ---------------------------------------------------------------------- 盘不在时补建文件夹


def pending_path(parent: str, name: str | None, project_name: str) -> str:
    return str(Path(parent) / sanitize_folder_name(name or project_name)[0])


def pending_folders(connection: Any, project_id: str | None = None) -> dict[str, dict[str, Any]]:
    """项目详情和列表带出的 pending_folder：{path, parent, state, reason}。"""
    sql = """SELECT pf.project_id, pf.parent, pf.name, pf.state, pf.last_error, p.name AS project_name
               FROM pending_project_folders pf JOIN projects p ON p.id = pf.project_id"""
    params: tuple[Any, ...] = ()
    if project_id is not None:
        sql += " WHERE pf.project_id = ?"
        params = (project_id,)
    return {
        row["project_id"]: {
            "path": pending_path(row["parent"], row["name"], row["project_name"]),
            "parent": row["parent"],
            "state": row["state"],
            "reason": row["last_error"],
        }
        for row in connection.execute(sql, params).fetchall()
    }


def queue_pending_folder(
    connection: Any, project_id: str, parent: str, name: str | None, project_name: str
) -> None:
    """create_project 在同一个事务里调：name 和项目名相同时存 NULL，之后跟着项目改名。"""
    connection.execute(
        """INSERT INTO pending_project_folders(project_id, parent, name, state, created_at)
           VALUES (?, ?, ?, 'waiting', ?)
           ON CONFLICT(project_id) DO UPDATE
              SET parent=excluded.parent, name=excluded.name, state='waiting', last_error=NULL""",
        (project_id, parent, None if not name or name == project_name else name, utc_now()),
    )


class _Abandon(Exception):
    pass


class PendingFolders:
    """后台补建文件夹：每轮刷新资料盘状态之后跑一次。

    停下（stopped）的待办不再每轮重试，只在盘重新插上（这期间见过它掉盘）或你改了位置时再试。
    """

    def __init__(self, db: Database, settings: Settings, *, state_of=volume_state):
        self.db = db
        self.settings = settings
        self._state_of = state_of
        self._seen_offline: set[str] = set()
        self._lock = threading.Lock()

    def _stop(self, project_id: str, row: dict[str, Any], error: str) -> None:
        if row["state"] == "stopped" and row["last_error"] == error:
            return
        self.db.execute(
            """UPDATE pending_project_folders SET state='stopped', last_error=?
                WHERE project_id=? AND parent=? AND name IS ?""",
            (error, project_id, row["parent"], row["name"]),
        )

    def drain(self, *, project_ids: list[str] | None = None, force: bool = False) -> list[dict[str, Any]]:
        """返回这一轮建好并挂上的：[{project_id, project_name, path}]。"""
        with self._lock:
            rows = self.db.query_all(
                """SELECT pf.project_id, pf.parent, pf.name, pf.state, pf.last_error,
                          p.name AS project_name
                     FROM pending_project_folders pf JOIN projects p ON p.id = pf.project_id
                    ORDER BY pf.created_at"""
            )
            done: list[dict[str, Any]] = []
            for row in rows:
                project_id = row["project_id"]
                if project_ids is not None and project_id not in project_ids:
                    continue
                try:
                    created = self._drain_one(row, force=force)
                except Exception:  # noqa: BLE001 — 一个出错不影响别的
                    logger.exception("补建项目文件夹失败：%s", project_id)
                    continue
                if created is not None:
                    done.append(created)
            return done

    def _drain_one(self, row: dict[str, Any], *, force: bool) -> dict[str, Any] | None:
        project_id = row["project_id"]
        parent = Path(row["parent"])
        try:
            state = self._state_of(parent)
        except OSError:
            state = ROOT_MISSING
        if state == ROOT_VOLUME_OFFLINE:
            self._seen_offline.add(project_id)
            return None
        if row["state"] == "stopped" and not force and project_id not in self._seen_offline:
            return None
        self._seen_offline.discard(project_id)
        if state != ROOT_ONLINE:
            self._stop(project_id, row, "要放新文件夹的位置不存在了")
            return None
        target = Path(pending_path(row["parent"], row["name"], row["project_name"]))
        for protected, message in _protected_roots(self.settings):
            if _within(target, protected):
                self._stop(project_id, row, message)
                return None
        if target.exists() and not target.is_dir():
            self._stop(project_id, row, f"「{target.name}」是个文件，不是文件夹")
            return None
        existed = target.is_dir()
        try:
            target.mkdir(parents=False, exist_ok=True)
        except PermissionError:
            self._stop(project_id, row, "没有权限在这里建文件夹")
            return None
        except OSError as error:
            self._stop(project_id, row, f"建文件夹失败：{error.strerror or error}")
            return None
        resolved = str(target.resolve())
        stop_reason: str | None = None
        try:
            with self.db.transaction() as connection:
                current = connection.execute(
                    """SELECT pf.parent, pf.name, p.name AS project_name
                         FROM pending_project_folders pf JOIN projects p ON p.id = pf.project_id
                        WHERE pf.project_id = ?""",
                    (project_id,),
                ).fetchone()
                if current is None or (
                    current["parent"], current["name"], current["project_name"]
                ) != (row["parent"], row["name"], row["project_name"]):
                    raise _Abandon
                if connection.execute(
                    "SELECT 1 FROM project_material_roots WHERE project_id=?", (project_id,)
                ).fetchone():
                    connection.execute(
                        "DELETE FROM pending_project_folders WHERE project_id=?", (project_id,)
                    )
                    raise _Abandon
                owner = connection.execute(
                    """SELECT p.name FROM project_material_roots r
                         JOIN projects p ON p.id = r.project_id WHERE r.path = ?""",
                    (resolved,),
                ).fetchone()
                if owner is not None:
                    stop_reason = f"同名文件夹已挂在「{owner['name']}」项目下"
                    raise _Abandon
                # 插入根目录时触发器会删掉这条待办
                connection.execute(
                    """INSERT INTO project_material_roots(project_id, path, created_at)
                       VALUES (?, ?, ?)""",
                    (project_id, resolved, utc_now()),
                )
        except _Abandon:
            if not existed:
                with contextlib.suppress(OSError):
                    target.rmdir()
            if stop_reason is not None:
                self._stop(project_id, row, stop_reason)
            return None
        except Exception:
            if not existed:
                with contextlib.suppress(OSError):
                    target.rmdir()
            raise
        return {"project_id": project_id, "project_name": row["project_name"], "path": resolved}


def set_pending_parent(db: Database, settings: Settings, project_id: str, raw: str) -> None:
    """改位置：校验后写回 waiting，由调用方当场试一次。"""
    parent = resolve_within(settings.material_browse_root, raw)
    for protected, message in _protected_roots(settings):
        if _within(parent, protected):
            raise ValueError(message)
    if volume_state(parent) == ROOT_MISSING:
        raise ValueError("这个位置不存在")
    with db.transaction() as connection:
        changed = connection.execute(
            """UPDATE pending_project_folders SET parent=?, state='waiting', last_error=NULL
                WHERE project_id=?""",
            (str(parent), project_id),
        ).rowcount
    if not changed:
        raise NotFoundError("这个项目没有等着补建的文件夹")


def drop_pending(db: Database, project_id: str) -> None:
    if not db.execute_rowcount(
        "DELETE FROM pending_project_folders WHERE project_id=?", (project_id,)
    ):
        raise NotFoundError("这个项目没有等着补建的文件夹")


# ---------------------------------------------------------------------- 改名找回


def missing_roots(connection: Any, cache: Any) -> list[dict[str, Any]]:
    """缓存里状态是「找不到」（盘在、文件夹没了）的根目录。"""
    found = []
    for row in connection.execute(
        """SELECT r.id, r.project_id, r.path, p.name AS project_name, p.also_names
             FROM project_material_roots r JOIN projects p ON p.id = r.project_id"""
    ).fetchall():
        entry = cache.get(row["path"])
        if entry is not None and entry["state"] == ROOT_MISSING:
            found.append(dict(row))
    return found


def _comparable(names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name != CARDS_DIR_NAME and name not in GENERIC_FOLDER_NAMES and light_key(name)
    }


def rename_candidates_for(
    connection: Any, cache: Any, root: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """一个「找不到」的根目录的候选，按证据强弱排；缓存还没读到时返回 None。

    证据：里面有这个项目的会议卡片 > 子文件夹和指纹（没有指纹时和需求文件夹名）对上至少 3 个且超过一半
    > 名字相近。
    """
    probes = cache.probes(root["path"])
    if probes is None:
        return None
    declined = {
        row["path"]
        for row in connection.execute(
            "SELECT path FROM folder_declines WHERE kind=? AND scope=?", (RENAME, str(root["id"]))
        ).fetchall()
    }
    mounted = [
        Path(row["path"])
        for row in connection.execute("SELECT path FROM project_material_roots").fetchall()
    ]
    own_meetings = {
        row["id"]
        for row in connection.execute(
            "SELECT id FROM meetings WHERE project_id=?", (root["project_id"],)
        ).fetchall()
    }
    fingerprint_row = connection.execute(
        "SELECT child_names FROM root_fingerprints WHERE root_id=?", (root["id"],)
    ).fetchone()
    basis = "fingerprint"
    reference: set[str] = set()
    if fingerprint_row is not None:
        try:
            reference = _comparable(json.loads(fingerprint_row["child_names"]))
        except json.JSONDecodeError:
            reference = set()
    if not reference:
        basis = "requirements"
        old = Path(root["path"])
        names = []
        for row in connection.execute(
            """SELECT f.path FROM requirement_folders f JOIN requirements r ON r.id = f.requirement_id
                WHERE r.project_id = ?""",
            (root["project_id"],),
        ).fetchall():
            folder = Path(row["path"])
            if folder.is_relative_to(old) and folder != old:
                names.append(folder.relative_to(old).parts[0])
        reference = _comparable(names)
    similar_names = [
        Path(root["path"]).name,
        root.get("project_name") or "",
        *(entry["name"] for entry in also_entries(root.get("also_names"))),
    ]
    candidates = []
    for probe in probes:
        path = Path(probe["path"])
        if probe["path"] in declined or any(
            other == path or other.is_relative_to(path) for other in mounted
        ):
            continue
        own = [mid for mid in probe.get("meeting_ids") or [] if mid in own_meetings]
        children = _comparable(probe.get("child_names") or [])
        matched = len(reference & children)
        evidence: dict[str, Any]
        if own:
            evidence = {"kind": "cards", "count": len(own), "text": "里面有这个项目的会议卡片"}
            strength = EVIDENCE_CARDS
        elif matched >= MIN_CHILD_MATCHES and matched * 2 > max(len(reference), len(children)):
            evidence = {
                "kind": "children",
                "matched": matched,
                "total": len(children),
                "basis": basis,
                "text": f"里面 {len(children)} 个子文件夹有 {matched} 个对得上",
            }
            strength = EVIDENCE_CHILDREN
        elif _match_kind(probe["name"], [name for name in similar_names if name]):
            evidence = {"kind": "name", "text": "名字相近"}
            strength = EVIDENCE_NAME
        else:
            continue
        candidates.append(
            {
                "path": probe["path"],
                "name": probe["name"],
                "modified_at": probe["mtime"],
                "strength": strength,
                "evidence": evidence,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["strength"],
            item["evidence"].get("count", 0) + item["evidence"].get("matched", 0),
            item["modified_at"],
        ),
        reverse=True,
    )
    return candidates


def rename_candidates(
    connection: Any, cache: Any, project_id: str, root_id: int
) -> dict[str, Any]:
    root = connection.execute(
        """SELECT r.id, r.project_id, r.path, p.name AS project_name, p.also_names
             FROM project_material_roots r JOIN projects p ON p.id = r.project_id
            WHERE r.id = ? AND r.project_id = ?""",
        (root_id, project_id),
    ).fetchone()
    if root is None:
        raise NotFoundError("材料根目录不存在")
    entry = cache.get(root["path"])
    if entry is None:
        cache.refresh_in_background()
        return {"state": CHECKING, "candidates": [], "default_path": None}
    if entry["state"] != ROOT_MISSING:
        return {"state": entry["state"], "candidates": [], "default_path": None}
    candidates = rename_candidates_for(connection, cache, dict(root))
    if candidates is None:
        cache.refresh_in_background()
        return {"state": CHECKING, "candidates": [], "default_path": None}
    default_path = None
    if candidates and (
        len(candidates) == 1 or candidates[0]["strength"] > candidates[1]["strength"]
    ):
        default_path = candidates[0]["path"]
    return {"state": "ready", "candidates": candidates, "default_path": default_path}


_PREFIX_WHERE = "(path = ? OR substr(path, 1, length(?) + 1) = ? || '/')"


def repoint_material_root(
    db: Database, settings: Settings, project_id: str, root_id: int, raw_path: str
) -> dict[str, Any]:
    """根目录换到新路径（改名找回的［是它］，也是「重新选…」）。保留 root_id。

    原来的文件夹找不到了（被改名）时，嵌在里面的路径一起换前缀：别的项目的根目录、需求文件夹、待补建的位置、
    拒绝记录。比较用 substr，不用 LIKE / REPLACE（文件夹名里的 _ 和 % 会被当成通配符，兄弟文件夹
    「云图AI二期」也会被误改）。不改 meeting_cards：卡片模块在「根目录换了」时会按会议 id 在新位置认回卡片，
    认不到就重写；先改卡片表的话，选到一个没有卡片文件夹的新位置时整个项目会被当成「卡片文件夹被删了」而停写。
    """
    current = db.query_one(
        "SELECT path FROM project_material_roots WHERE id=? AND project_id=?", (root_id, project_id)
    )
    if current is None:
        raise NotFoundError("材料根目录不存在")
    carry = volume_state(current["path"]) == ROOT_MISSING
    moved_roots: list[dict[str, Any]] = []
    moved_folders = 0
    projects: set[str] = {project_id}
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT path FROM project_material_roots WHERE id=? AND project_id=?",
            (root_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("材料根目录不存在")
        old = row["path"]
        path, nested = validate_new_root(connection, settings, project_id, raw_path)
        if path == old:
            return {"path": path, "nested": nested, "moved_roots": [], "moved_folders": 0, "projects": []}
        prefix = (old, old, old)
        if carry:
            rows = connection.execute(
                f"""SELECT r.id, r.project_id, r.path, p.name AS project_name
                      FROM project_material_roots r JOIN projects p ON p.id = r.project_id
                     WHERE {_PREFIX_WHERE.replace('path', 'r.path')}""",
                prefix,
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT r.id, r.project_id, r.path, p.name AS project_name
                     FROM project_material_roots r JOIN projects p ON p.id = r.project_id
                    WHERE r.id = ?""",
                (root_id,),
            ).fetchall()
        for item in rows:
            new_path = path + item["path"][len(old):]
            try:
                connection.execute(
                    "UPDATE project_material_roots SET path=? WHERE id=?", (new_path, item["id"])
                )
            except Exception as error:  # sqlite3.IntegrityError：同一项目已挂过新路径
                raise ConflictError("新位置已经挂在这个项目下") from error
            projects.add(item["project_id"])
            if item["id"] != root_id:
                moved_roots.append(
                    {
                        "id": item["id"],
                        "project_id": item["project_id"],
                        "project_name": item["project_name"],
                        "old": item["path"],
                        "new": new_path,
                    }
                )
        if carry:
            for folder in connection.execute(
                f"""SELECT r.project_id FROM requirement_folders f
                      JOIN requirements r ON r.id = f.requirement_id
                     WHERE {_PREFIX_WHERE.replace('path', 'f.path')}""",
                prefix,
            ).fetchall():
                projects.add(folder["project_id"])
            moved_folders = connection.execute(
                f"""UPDATE OR IGNORE requirement_folders
                       SET path = ? || substr(path, length(?) + 1)
                     WHERE {_PREFIX_WHERE}""",
                (path, old, *prefix),
            ).rowcount
            connection.execute(
                f"""UPDATE pending_project_folders SET parent = ? || substr(parent, length(?) + 1)
                     WHERE {_PREFIX_WHERE.replace('path', 'parent')}""",
                (path, old, *prefix),
            )
            connection.execute(
                f"""UPDATE OR REPLACE folder_declines SET path = ? || substr(path, length(?) + 1)
                     WHERE {_PREFIX_WHERE}""",
                (path, old, *prefix),
            )
    return {
        "path": path,
        "nested": nested,
        "moved_roots": moved_roots,
        "moved_folders": moved_folders,
        "projects": sorted(projects),
    }


# ---------------------------------------------------------------------- 同名文件夹（读缓存）

MAX_RECENT_FOLDERS = 5


def folder_matches(
    connection: Any, settings: Settings, cache: Any, *, names: list[str]
) -> dict[str, Any]:
    """还没挂到项目的文件夹里，和 names 同名（exact）或相近（similar）的，外加最近修改的几个；
    新文件夹放哪（项目总文件夹 → 推荐位置 → 不建）。只读缓存，缓存没好时 state=checking。"""
    names = [name for name in names if name and name.strip()]
    target = folder_target(connection, settings)
    create_name, replaced = sanitize_folder_name(names[0]) if names else ("", [])
    result: dict[str, Any] = {
        "state": "ready",
        "matches": [],
        "recent": [],
        "create_parent": target["parent"],
        "create_parent_source": target["source"],
        "create_parent_state": None,
        "create_name": create_name,
        "create_replaced": replaced,
    }
    if target["parent"]:
        listing = cache.listing(target["parent"])
        result["create_parent_state"] = listing["state"] if listing else CHECKING
    folders = free_folders(connection, settings, cache)
    if folders is None:
        result["state"] = CHECKING
        return result

    def item(folder: dict[str, Any], match: str | None) -> dict[str, Any]:
        return {
            "path": folder["path"],
            "name": folder["name"],
            "match": match,
            "modified_at": folder["mtime"],
        }

    matches = []
    for folder in folders:
        match = _match_kind(folder["name"], names) if names else None
        if match:
            matches.append(item(folder, match))
    matches.sort(key=lambda entry: (entry["match"] != "exact", entry["name"]))
    result["matches"] = matches
    result["recent"] = [
        item(folder, None)
        for folder in sorted(folders, key=lambda folder: folder["mtime"], reverse=True)[
            :MAX_RECENT_FOLDERS
        ]
    ]
    return result


def unmounted_project_folders(
    connection: Any, settings: Settings, cache: Any
) -> list[dict[str, Any]] | None:
    """冷启动：还没挂文件夹的项目，各自找同名（默认勾选）或相近（默认不勾）的文件夹。
    一个项目只给最像的那一个文件夹（同名优先，再按名字排序）。缓存没好时返回 None。"""
    projects = connection.execute(
        """SELECT p.id, p.name, p.also_names FROM projects p
            WHERE NOT EXISTS (SELECT 1 FROM project_material_roots r WHERE r.project_id = p.id)
            ORDER BY p.name"""
    ).fetchall()
    if not projects:
        return []
    folders = free_folders(connection, settings, cache)
    if folders is None:
        return None
    items: list[dict[str, Any]] = []
    for project in projects:
        names = [project["name"], *(entry["name"] for entry in also_entries(project["also_names"]))]
        found = [
            (kind, folder)
            for folder in folders
            if (kind := _match_kind(folder["name"], names)) is not None
        ]
        if not found:
            continue
        found.sort(key=lambda pair: (pair[0] != "exact", pair[1]["name"]))
        kind, folder = found[0]
        items.append(
            {
                "project_id": project["id"],
                "project_name": project["name"],
                "path": folder["path"],
                "folder_name": folder["name"],
                "match": kind,
            }
        )
    return items
