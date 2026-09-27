"""项目材料目录：浏览、统计、项目材料根目录 CRUD（260915 新增）。

浏览与统计范围限制在 `MEETING_WORKBENCH_MATERIAL_BROWSE_ROOT` 之下（默认外置盘）；
外置盘是 exFAT，AppleDouble 影子文件量级达 23.7 万个，统计一律跳过隐藏项/黑名单目录、
不跟随符号链接、单文件夹最多数到 2000 个。接口全部走同步 def，由 FastAPI 丢进线程池，
不阻塞事件循环。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, utc_now
from .service import ConflictError, NotFoundError

# 单文件夹最多数到的文件数；超过即标 capped，界面显示「2000+」。
MAX_FOLDER_FILES = 2000
# 递归统计时跳过的目录/文件名。
_SKIP_NAMES = {"node_modules", "__MACOSX"}
# 声档往项目文件夹里写会议卡片的子目录。它是声档自己的产出，不算项目材料：
# 不进子文件夹统计、不能被选成需求文件夹。
CARDS_DIR_NAME = "声档会议记录"

# 材料根目录的三种状态。online：文件夹在；missing：盘在但文件夹没了（被改名或删掉）；
# volume_offline：路径在 /Volumes/ 下而那块盘没有真实挂载（拔掉了），这时不能当成「没了」。
ROOT_ONLINE = "online"
ROOT_MISSING = "missing"
ROOT_VOLUME_OFFLINE = "volume_offline"


def _is_skipped(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_NAMES


def _iso_mtime(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def assert_no_hidden_segment(path: Path) -> None:
    """D27：路径任一段以 `.` 开头即拒绝——浏览、挂根目录、需求文件夹三处共用这一份判定。"""
    for part in path.parts:
        if part.startswith("."):
            raise ValueError("不能选择隐藏目录")


def resolve_within(base: Path, raw: str | None) -> Path:
    """把用户传入的路径解析到 base 之下的真实路径；拒绝相对路径、`..`、隐藏目录（D27）
    与指向外部的符号链接（`Path.resolve()` 会把符号链接链路一路解到底，落在 base 外一律拒绝）。
    """
    base_real = base.resolve()
    if not raw:
        return base_real
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("路径必须是绝对路径")
    try:
        real = candidate.resolve(strict=False)
    except OSError as error:
        raise ValueError("路径无法解析") from error
    assert_no_hidden_segment(real)
    if real != base_real and not real.is_relative_to(base_real):
        raise ValueError("路径超出允许范围")
    return real


def _iter_files(path: Path, limit: int) -> tuple[list[tuple[Path, os.stat_result]], bool]:
    """按统一策略递归收集 path 下的文件：跳过隐藏项/黑名单目录、不跟随符号链接，
    最多收集 limit 个后立即停止扫描。file_count / modified_at / 文件清单三处消费者
    共用这一份遍历逻辑，避免策略各处实现出现漂移。
    """
    collected: list[tuple[Path, os.stat_result]] = []
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not _is_skipped(name))
        root_path = Path(root)
        for name in sorted(files):
            if _is_skipped(name):
                continue
            file_path = root_path / name
            try:
                if file_path.is_symlink():
                    continue
                stat_result = file_path.stat()
            except OSError:
                continue
            collected.append((file_path, stat_result))
            if len(collected) >= limit:
                return collected, True
    return collected, False


def folder_stat(path: Path, *, limit: int = MAX_FOLDER_FILES) -> dict[str, Any]:
    """单个文件夹的统计：递归文件数 + 其中最新文件的修改时间。"""
    if not path.is_dir():
        return {"exists": False, "file_count": 0, "file_count_capped": False, "modified_at": None}
    entries, capped = _iter_files(path, limit)
    latest = max((stat_result.st_mtime for _, stat_result in entries), default=None)
    return {
        "exists": True,
        "file_count": len(entries),
        "file_count_capped": capped,
        "modified_at": _iso_mtime(latest),
    }


def list_folder_files(path: Path, *, limit: int = MAX_FOLDER_FILES, offset: int = 0) -> dict[str, Any]:
    """递归列出文件夹内文件，按相对路径排序分页；capped 表示命中了 MAX_FOLDER_FILES 硬上限。"""
    if not path.is_dir():
        return {"exists": False, "total": 0, "capped": False, "items": []}
    entries, capped = _iter_files(path, MAX_FOLDER_FILES)
    items = [
        {
            "relative_path": str(file_path.relative_to(path)),
            "size_bytes": stat_result.st_size,
            "modified_at": _iso_mtime(stat_result.st_mtime),
        }
        for file_path, stat_result in entries
    ]
    items.sort(key=lambda item: item["relative_path"])
    return {"exists": True, "total": len(items), "capped": capped, "items": items[offset : offset + limit]}


def _project_subfolder_stats(root_path: Path) -> dict[str, Any]:
    if not root_path.is_dir():
        return {"exists": False, "folders": []}
    folders: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(root_path))
    except OSError:
        return {"exists": False, "folders": []}
    for entry in entries:
        if _is_skipped(entry.name) or entry.name == CARDS_DIR_NAME:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        sub_path = Path(entry.path)
        stat = folder_stat(sub_path)
        folders.append({"name": entry.name, "path": str(sub_path), **stat})
    folders.sort(key=lambda folder: folder["modified_at"] or "", reverse=True)
    return {"exists": True, "folders": folders}


# 项目材料根目录下一级子文件夹的统计结果按根目录真实路径缓存，TTL 内命中直接返回。
# exFAT 递归很慢，项目列表页/详情页短时间内会对同一根目录重复发起统计请求；缓存键＝
# root_path 的 resolve() 字符串，失效纯靠 TTL 过期——挂/删根目录、改需求文件夹都不
# 改变已存在文件夹自身的统计，不需要主动失效点。
_SUBFOLDER_STATS_CACHE_TTL_SECONDS = 30.0
_subfolder_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def project_subfolder_stats(root_path: Path) -> dict[str, Any]:
    key = str(root_path)
    now = time.monotonic()
    cached = _subfolder_stats_cache.get(key)
    if cached is not None and now - cached[0] < _SUBFOLDER_STATS_CACHE_TTL_SECONDS:
        return cached[1]
    result = _project_subfolder_stats(root_path)
    _subfolder_stats_cache[key] = (now, result)
    return result


def _breadcrumbs(base: Path, target: Path) -> list[dict[str, str]]:
    crumbs = [{"name": base.name or str(base), "path": str(base)}]
    if target == base:
        return crumbs
    current = base
    for part in target.relative_to(base).parts:
        current = current / part
        crumbs.append({"name": part, "path": str(current)})
    return crumbs


def browse_directory(base: Path, raw_path: str | None) -> dict[str, Any]:
    # base 本身可能是符号链接（合法运维配置：外置盘替代挂载路径、路径重定向）；
    # resolve_within 内部按 base.resolve() 穿透符号链接算越界，这里必须用同一个
    # 已解析的 base 拼 breadcrumbs/parent，否则「未解析的 base」与「已解析的 target」
    # 前缀对不上，Path.relative_to() 会抛未翻译的原始异常文本（ADV-M-06）。
    base_real = base.resolve()
    target = resolve_within(base, raw_path)
    if not target.is_dir():
        raise NotFoundError("目录不存在")
    dirs: list[dict[str, str]] = []
    for entry in os.scandir(target):
        if _is_skipped(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        dirs.append({"name": entry.name, "path": str(Path(entry.path))})
    dirs.sort(key=lambda item: item["name"])
    return {
        "base": str(base_real),
        "path": str(target),
        "parent": None if target == base_real else str(target.parent),
        "breadcrumbs": _breadcrumbs(base_real, target),
        "dirs": dirs,
    }


# ---------------------------------------------------------------- 项目材料根目录 CRUD


def _volume_mount(path: Path) -> Path | None:
    """/Volumes/<盘>/… 返回 /Volumes/<盘>；不在 /Volumes 下返回 None。"""
    try:
        relative = path.relative_to("/Volumes")
    except ValueError:
        return None
    if not relative.parts:
        return None
    return Path("/Volumes") / relative.parts[0]


def volume_state(path: str | Path) -> str:
    """材料根目录的三态，判据照搬 relay_control.py 的外置卷检查（ismount + st_dev）：
    /Volumes/<盘> 不是真实挂载点、或设备号和 /Volumes 本身相同（掉盘后留下的空目录），
    都算盘没插，而不是文件夹没了。"""
    candidate = Path(path)
    mount = _volume_mount(candidate)
    if mount is not None:
        try:
            if (
                mount.is_symlink()
                or not os.path.ismount(mount)
                or mount.stat().st_dev == Path("/Volumes").stat().st_dev
            ):
                return ROOT_VOLUME_OFFLINE
        except OSError:
            return ROOT_VOLUME_OFFLINE
    try:
        return ROOT_ONLINE if candidate.is_dir() else ROOT_MISSING
    except OSError:
        return ROOT_MISSING


def annotate_root(row: dict[str, Any]) -> dict[str, Any]:
    """根目录行补上 state（三态）和兼容旧调用方的 exists（= online）。"""
    state = volume_state(row["path"])
    row["state"] = state
    row["exists"] = state == ROOT_ONLINE
    return row


def _protected_roots(settings: Settings) -> list[tuple[Path, str]]:
    return [
        (settings.archive_root, "这是声档的会议归档目录，不能当项目文件夹"),
        (settings.staging_root, "这是声档的转写暂存目录，不能当项目文件夹"),
        (settings.data_dir, "这是声档的数据目录，不能当项目文件夹"),
    ]


def _within(path: Path, root: Path) -> bool:
    try:
        root_real = root.expanduser().resolve()
    except OSError:
        return False
    return path == root_real or path.is_relative_to(root_real)


def validate_new_root(
    connection: Any, settings: Settings, project_id: str, raw_path: str
) -> tuple[str, list[dict[str, Any]]]:
    """挂一个新的材料根目录前的全部校验；返回 (真实路径, 嵌套提示)。

    拒绝：浏览根之外 / 隐藏目录 / 不存在（盘没插时给专门的提示）/ 声档自己的归档、
    暂存、数据目录之内 / 已挂在别的项目下（409，带对方项目名）。嵌套允许，只返回提示。
    """
    resolved = resolve_within(settings.material_browse_root, raw_path)
    state = volume_state(resolved)
    if state == ROOT_VOLUME_OFFLINE:
        raise ValueError("资料盘未连接，插上后再选")
    if state != ROOT_ONLINE:
        raise ValueError("材料根目录不存在或不是文件夹")
    for protected, message in _protected_roots(settings):
        if _within(resolved, protected):
            raise ValueError(message)
    owner = connection.execute(
        """SELECT p.name FROM project_material_roots r JOIN projects p ON p.id = r.project_id
            WHERE r.path=? AND r.project_id != ?""",
        (str(resolved), project_id),
    ).fetchone()
    if owner is not None:
        raise ConflictError(f"这个文件夹已挂在「{owner['name']}」项目下")
    nested: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT r.path, r.project_id, p.name AS project_name
             FROM project_material_roots r JOIN projects p ON p.id = r.project_id
            WHERE r.project_id != ?""",
        (project_id,),
    ).fetchall():
        other = Path(row["path"])
        if other.is_relative_to(resolved) or resolved.is_relative_to(other):
            nested.append(dict(row))
    return str(resolved), nested


def list_material_roots(db: Database, project_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT * FROM project_material_roots WHERE project_id=? ORDER BY created_at, id",
        (project_id,),
    )
    return [annotate_root(row) for row in rows]


def add_material_root(
    db: Database, settings: Settings, project_id: str, raw_path: str
) -> dict[str, Any]:
    now = utc_now()
    with db.transaction() as connection:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise NotFoundError(f"项目不存在：{project_id}")
        path, nested = validate_new_root(connection, settings, project_id, raw_path)
        try:
            cursor = connection.execute(
                "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
                (project_id, path, now),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError("该材料根目录已经挂在这个项目下") from error
        root_id = cursor.lastrowid
    row = annotate_root(db.query_one("SELECT * FROM project_material_roots WHERE id=?", (root_id,)))
    row["nested"] = nested
    return row


def replace_material_root(
    db: Database, settings: Settings, project_id: str, root_id: int, raw_path: str
) -> dict[str, Any]:
    """原子替换一个根目录的路径：只改 path，保留 id 和 created_at。

    取代前端「先删后加」：第二步失败时不会把根目录弄丢，以后卡片按根目录 id 认领也不断。
    """
    with db.transaction() as connection:
        current = connection.execute(
            "SELECT * FROM project_material_roots WHERE id=? AND project_id=?",
            (root_id, project_id),
        ).fetchone()
        if current is None:
            raise NotFoundError("材料根目录不存在")
        path, nested = validate_new_root(connection, settings, project_id, raw_path)
        if path != current["path"]:
            try:
                connection.execute(
                    "UPDATE project_material_roots SET path=? WHERE id=?", (path, root_id)
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError("该材料根目录已经挂在这个项目下") from error
    row = annotate_root(db.query_one("SELECT * FROM project_material_roots WHERE id=?", (root_id,)))
    row["nested"] = nested
    return row


def remove_material_root(db: Database, project_id: str, root_id: int) -> None:
    removed = db.execute_rowcount(
        "DELETE FROM project_material_roots WHERE id=? AND project_id=?",
        (root_id, project_id),
    )
    if not removed:
        raise NotFoundError("材料根目录不存在")


def replace_material_roots(
    connection: Any, settings: Settings, project_id: str, raw_paths: list[str]
) -> bool:
    """把某项目的材料根目录列表改成 raw_paths；调用方负责开事务（供项目创建/更新复用）。

    按差异增删：原样保留的行不动（id、created_at 不变，也不重新校验——盘没插时改项目名
    不该因为根目录暂时看不到而失败），只删掉去掉的、只校验并插入新加的。返回是否有变化。
    """
    existing = {
        row["path"]: row["id"]
        for row in connection.execute(
            "SELECT id, path FROM project_material_roots WHERE project_id=?", (project_id,)
        ).fetchall()
    }
    wanted: list[str] = []
    for raw_path in raw_paths:
        if raw_path in existing:
            path = raw_path
        else:
            path, _nested = validate_new_root(connection, settings, project_id, raw_path)
        if path not in wanted:
            wanted.append(path)
    removed = [root_id for path, root_id in existing.items() if path not in wanted]
    added = [path for path in wanted if path not in existing]
    for root_id in removed:
        connection.execute("DELETE FROM project_material_roots WHERE id=?", (root_id,))
    now = utc_now()
    for path in added:
        connection.execute(
            "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
            (project_id, path, now),
        )
    return bool(removed or added)
