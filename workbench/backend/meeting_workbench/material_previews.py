"""材料的预览图（第三期 3c，3e 的预览接口用）。

- 缓存在 data_dir/material-previews/<content_key>-<尺寸>.jpg，总量超过 500MB 时删最久没用的。
- 缓存没有时不走后台认字的常驻程序（它一次只做一件事，后台正在读一份扫描 PDF 时会一直等）：用同一个
  二进制的一次性子命令 `sd-vision thumb|page1 <path> <max_side> <out>`，请求里单独起进程，先写临时名
  再改名。没有 Vision 时图片用 `sips -Z <尺寸> -s format jpeg <原文件> --out <临时名>`；PDF 第一页没有
  Vision 就不出图。
- 15 秒超时，同一时刻最多 2 个在生成；超时抛 PreviewTimeout（接口回 503「预览图生成超时」）。
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from .material_helpers import HelperTimeout, run_background
from .vision_helper import VisionBuild

CACHE_LIMIT_BYTES = 500 * 1024 * 1024
TIMEOUT_SECONDS = 15.0
MAX_RUNNING = 2
SIZES = (480, 1600)

_slots = threading.BoundedSemaphore(MAX_RUNNING)
_prune_lock = threading.Lock()


class PreviewTimeout(Exception):
    """预览图生成超时（或同时在生成的太多，等不到）。"""


class PreviewUnavailable(Exception):
    """这台机器上出不了这种预览图（例如没有 Vision 时的 PDF 第一页）。"""


def cache_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "material-previews"


def cache_path(data_dir: Path, content_key: str, size: int) -> Path:
    safe = re.sub(r"[^0-9A-Za-z]", "_", content_key)
    return cache_dir(data_dir) / f"{safe}-{size}.jpg"


def prune_cache(data_dir: Path, limit: int = CACHE_LIMIT_BYTES) -> int:
    """总量超过上限时删最久没用的（按修改时间；取用时会 touch）。返回删了几个。"""
    folder = cache_dir(data_dir)
    with _prune_lock:
        try:
            entries = [
                (entry.stat().st_mtime, entry.stat().st_size, Path(entry.path))
                for entry in os.scandir(folder)
                if entry.is_file() and entry.name.endswith(".jpg")
            ]
        except FileNotFoundError:
            return 0
        total = sum(size for _mtime, size, _path in entries)
        removed = 0
        for _mtime, size, path in sorted(entries):
            if total <= limit:
                break
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed


def preview(
    data_dir: Path,
    content_key: str,
    path: Path,
    *,
    kind: str,
    size: int = SIZES[0],
    build: VisionBuild | None = None,
    sips: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Path:
    """拿一张预览图（kind 是 image 或 pdf）：有缓存直接用，没有就生成。"""
    target = cache_path(data_dir, content_key, size)
    if target.is_file():
        try:
            os.utime(target)
        except OSError:
            pass
        return target
    build = build or VisionBuild(Path(data_dir))
    if build.ready():
        argv = [str(build.binary), "thumb" if kind == "image" else "page1", str(path), str(size)]
    elif kind == "image" and sips:
        argv = [sips, "-Z", str(size), "-s", "format", "jpeg", str(path), "--out"]
    else:
        raise PreviewUnavailable(kind)
    if not _slots.acquire(timeout=timeout):
        raise PreviewTimeout("同时在生成的预览图太多")
    temporary = target.with_name(f".{target.name}.{threading.get_ident()}.tmp.jpg")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = run_background([*argv, str(temporary)], timeout=timeout, background=False)
        except HelperTimeout as error:
            raise PreviewTimeout("预览图生成超时") from error
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise PreviewUnavailable(kind)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        _slots.release()
    prune_cache(data_dir)
    return target
