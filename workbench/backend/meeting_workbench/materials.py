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

from .db import Database, utc_now
from .service import ConflictError, NotFoundError

# 单文件夹最多数到的文件数；超过即标 capped，界面显示「2000+」。
MAX_FOLDER_FILES = 2000
# 递归统计时跳过的目录/文件名。
_SKIP_NAMES = {"node_modules", "__MACOSX"}


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
        if _is_skipped(entry.name):
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


def list_material_roots(db: Database, project_id: str) -> list[dict[str, Any]]:
    rows = db.query_all(
        "SELECT * FROM project_material_roots WHERE project_id=? ORDER BY created_at",
        (project_id,),
    )
    for row in rows:
        row["exists"] = Path(row["path"]).is_dir()
    return rows


def add_material_root(
    db: Database, browse_root: Path, project_id: str, raw_path: str
) -> dict[str, Any]:
    resolved = resolve_within(browse_root, raw_path)
    if not resolved.is_dir():
        raise ValueError("材料根目录不存在或不是文件夹")
    now = utc_now()
    with db.transaction() as connection:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise NotFoundError(f"项目不存在：{project_id}")
        try:
            cursor = connection.execute(
                "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
                (project_id, str(resolved), now),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError("该材料根目录已经挂在这个项目下") from error
        root_id = cursor.lastrowid
    row = db.query_one("SELECT * FROM project_material_roots WHERE id=?", (root_id,))
    row["exists"] = True
    return row


def remove_material_root(db: Database, project_id: str, root_id: int) -> None:
    removed = db.execute_rowcount(
        "DELETE FROM project_material_roots WHERE id=? AND project_id=?",
        (root_id, project_id),
    )
    if not removed:
        raise NotFoundError("材料根目录不存在")


def replace_material_roots(
    connection: Any, browse_root: Path, project_id: str, raw_paths: list[str]
) -> None:
    """整体替换某项目的材料根目录列表；调用方负责开事务（供项目创建/更新复用）。"""
    resolved_paths: list[str] = []
    for raw_path in raw_paths:
        resolved = resolve_within(browse_root, raw_path)
        if not resolved.is_dir():
            raise ValueError(f"材料根目录不存在或不是文件夹：{raw_path}")
        resolved_paths.append(str(resolved))
    connection.execute("DELETE FROM project_material_roots WHERE project_id=?", (project_id,))
    now = utc_now()
    for path in resolved_paths:
        connection.execute(
            "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
            (project_id, path, now),
        )
