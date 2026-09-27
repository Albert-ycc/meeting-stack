"""第二期 2a：给「还没挂的文件夹」读盘的几个小函数。只在 RootsCache 的后台线程里调用。

- 列目录：只读一层，只收文件夹，跳过隐藏、系统和声档自己的文件夹，不跟随替身。
- 读候选：文件夹改名后找回时，看候选文件夹里的一级子文件夹名，和它「声档会议记录/」里
  最多 5 张卡片开头的 meeting_id。
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .material_walk import NAME_ONLY_DIRS, SYSTEM_NAMES
from .materials import CARDS_DIR_NAME

# 系统和回收站一类的文件夹：不会是项目。
SKIP_FOLDER_NAMES = frozenset(
    {
        *SYSTEM_NAMES,
        *NAME_ONLY_DIRS,
        CARDS_DIR_NAME,
        "$RECYCLE.BIN",
        "System Volume Information",
        "RECYCLER",
        "lost+found",
        "Applications",
        "System",
        "Users",
        "Backups.backupdb",
    }
)
# 指纹最多记多少个子文件夹名。
MAX_CHILD_NAMES = 200
# 改名找回：父目录里最多看多少个候选、每个候选最多读几张卡片。
MAX_PROBE_CANDIDATES = 20
MAX_PROBE_CARDS = 5


def skipped_name(name: str) -> bool:
    return name.startswith((".", "~$")) or name in SKIP_FOLDER_NAMES


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, UTC).isoformat()


def list_child_dirs(path: str | Path) -> list[dict[str, Any]]:
    """path 下一层的文件夹：{name, path, mtime}，按名字排。读不了抛 OSError。"""
    base = Path(path)
    library = Path.home() / "Library"
    found: list[dict[str, Any]] = []
    with os.scandir(base) as iterator:
        for entry in iterator:
            name = entry.name
            if skipped_name(name):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            child = base / name
            if child == library:
                continue
            found.append({"name": name, "path": str(child), "mtime": _iso(stat.st_mtime)})
    found.sort(key=lambda item: item["name"])
    return found


def child_names(path: str | Path) -> list[str]:
    """一级子文件夹名（指纹用），最多 MAX_CHILD_NAMES 个。"""
    return [item["name"] for item in list_child_dirs(path)][:MAX_CHILD_NAMES]


def probe_candidate(path: str | Path) -> dict[str, Any]:
    """改名找回的候选：一级子文件夹名，和卡片文件夹里读到的 meeting_id。
    不读「00 索引.md」和「逐字稿/」。读不了的部分当作空。"""
    # cards 牵出归属模块，放到这里导入，免得和 tasks → project_folders 绕成环。
    from .cards import INDEX_NAME, _read_meeting_id

    base = Path(path)
    try:
        names = child_names(base)
    except OSError:
        names = []
    meeting_ids: list[str] = []
    cards = base / CARDS_DIR_NAME
    try:
        if cards.is_dir() and not cards.is_symlink():
            with os.scandir(cards) as iterator:
                files = sorted(
                    entry.name
                    for entry in iterator
                    if entry.name.endswith(".md")
                    and not entry.name.startswith(".")
                    and entry.name != INDEX_NAME
                    and entry.is_file(follow_symlinks=False)
                )
            for name in files[:MAX_PROBE_CARDS]:
                meeting_id = _read_meeting_id(cards / name)
                if meeting_id:
                    meeting_ids.append(meeting_id)
    except OSError:
        pass
    return {"child_names": names, "meeting_ids": meeting_ids}


# ---------------------------------------------------------------------- 列哪些目录

# app_state 里项目总文件夹的键：解析后的绝对路径。
PARENT_KEY = "project_parent_folder"


def parent_setting(connection: Any) -> str | None:
    row = connection.execute("SELECT value FROM app_state WHERE key=?", (PARENT_KEY,)).fetchone()
    return row["value"] if row and row["value"] else None


def browse_root(settings: Any) -> Path:
    return settings.material_browse_root.expanduser().resolve()


def listing_targets(connection: Any, settings: Any) -> tuple[list[str], int]:
    """要列的目录和深度：项目总文件夹、全部已挂根目录的父目录（在浏览根之内的）各看一层；
    一个都没有时从浏览根往下看两层。"""
    base = browse_root(settings)
    targets: list[str] = []
    parent = parent_setting(connection)
    if parent:
        targets.append(parent)
    for row in connection.execute(
        "SELECT path FROM project_material_roots ORDER BY created_at DESC, id DESC"
    ).fetchall():
        folder = Path(row["path"]).parent
        if (folder == base or folder.is_relative_to(base)) and str(folder) not in targets:
            targets.append(str(folder))
    if targets:
        return targets, 1
    return [str(base)], 2
