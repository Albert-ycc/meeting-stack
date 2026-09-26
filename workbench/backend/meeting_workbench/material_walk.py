"""1f 材料盘点：给第三期（全量材料索引）摸底，只读，不写库、不写盘。

按第三期定下的规则走一遍项目材料文件夹：
- 只静默跳过系统影子文件（._*、.DS_Store、__MACOSX 以及卷根上的系统目录）；
- node_modules、.git 这类目录和声档自己写的「声档会议记录」只收文件名，单独计数；
- 其余文件按要做的事分四层：文档正文、图片文字、音视频转写、只收文件名；
- 读不了的只分五种原因：要密码、文件损坏、格式不支持、处理超时、没有权限。

内容检查都是「偷看」：PDF 读头尾各一段判断有没有文字层，Office 文件看是不是正常的 zip，
音视频在装了 ffprobe 时读时长。不做全文解析，也不算哈希。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .materials import CARDS_DIR_NAME

# —— 分层 ——
TEXT = "text"
IMAGE = "image"
MEDIA = "media"
NAME_ONLY = "name_only"
LAYER_LABELS = {
    TEXT: "文档正文",
    IMAGE: "图片文字",
    MEDIA: "音视频转写",
    NAME_ONLY: "只收文件名",
}

TEXT_EXTS = {
    "pdf", "doc", "docx", "docm", "dot", "dotx", "xls", "xlsx", "xlsm", "xlt", "csv", "tsv",
    "ppt", "pptx", "pps", "ppsx", "odt", "ods", "odp", "rtf", "txt", "md", "markdown",
    "html", "htm", "xml", "json", "yaml", "yml", "epub", "wps", "et", "dps", "key", "pages",
    "numbers", "log", "ini", "toml", "conf", "sql", "py", "js", "ts", "tsx", "jsx", "java",
    "go", "rs", "c", "h", "cpp", "hpp", "cs", "rb", "php", "sh", "css", "scss", "ipynb",
}
IMAGE_EXTS = {"png", "jpg", "jpeg", "heic", "heif", "gif", "bmp", "tif", "tiff", "webp"}
AUDIO_EXTS = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "wma", "amr", "aiff", "aif", "caf"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv", "3gp", "mts", "m2ts"}
OOXML_EXTS = {"docx", "docm", "dotx", "xlsx", "xlsm", "pptx", "ppsx"}
IWORK_EXTS = {"key", "pages", "numbers"}
# macOS 上以文件夹形式存在、在 Finder 里看起来是一个文件的「包」
PACKAGE_EXTS = IWORK_EXTS | {
    "app", "bundle", "framework", "photoslibrary", "rtfd", "xcodeproj", "xcworkspace",
    "fcpbundle", "logicx", "band", "imovielibrary", "lrlibrary", "sparsebundle",
}

# —— 跳过与只收文件名 ——
SYSTEM_NAMES = {
    ".DS_Store", "__MACOSX", ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", ".VolumeIcon.icns", ".apdisk", "Thumbs.db", "desktop.ini", "Icon\r",
}
NAME_ONLY_DIRS = {
    "node_modules", ".git", ".svn", ".hg", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    CARDS_DIR_NAME,
}

# —— 读不了的五种原因 ——
PASSWORD = "password"
CORRUPT = "corrupt"
UNSUPPORTED = "unsupported"
TIMEOUT = "timeout"
PERMISSION = "permission"
REASON_LABELS = {
    PASSWORD: "要密码",
    CORRUPT: "文件损坏",
    UNSUPPORTED: "格式不支持",
    TIMEOUT: "处理超时",
    PERMISSION: "没有权限",
}

PDF_WHOLE_LIMIT = 8 * 1024 * 1024
PDF_HEAD = 2 * 1024 * 1024
PDF_TAIL = 1024 * 1024
PDF_MAX_STREAMS = 60
SMALL_IMAGE_BYTES = 20 * 1024
MEDIA_PROBE_TIMEOUT = 15
SAMPLES_PER_REASON = 8
PROGRESS_EVERY = 20000

_STREAM_RE = re.compile(rb"stream\r?\n")
# 明文里只认 /Font（短标记在图片的二进制数据里会碰巧出现）；解开的流是文本，可以认文字操作符
_PDF_RAW_MARKERS = (b"/Font",)
_PDF_STREAM_MARKERS = (b"/Font", b" Tf", b"BT\n", b"BT\r", b"BT ")


def file_ext(name: str) -> str:
    _stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and _stem else ""


def layer_for(name: str) -> str:
    ext = file_ext(name)
    if ext in TEXT_EXTS:
        return TEXT
    if ext in IMAGE_EXTS:
        return IMAGE
    if ext in AUDIO_EXTS or ext in VIDEO_EXTS:
        return MEDIA
    return NAME_ONLY


def is_system_shadow(name: str) -> bool:
    return name.startswith("._") or name in SYSTEM_NAMES


# —— 内容偷看 ——


def _read_window(path: Path, size: int) -> bytes:
    with path.open("rb") as handle:
        if size <= PDF_WHOLE_LIMIT:
            return handle.read()
        head = handle.read(PDF_HEAD)
        handle.seek(max(size - PDF_TAIL, 0))
        return head + handle.read()


def peek_pdf(path: Path, size: int) -> tuple[str | None, str | None]:
    """返回 (问题原因, 文字层判断)。文字层判断：text 有文字层 / scanned 像扫描件 / unknown。

    新版 PDF 把字体字典压进对象流，所以明文里找不到时，再解开读到的前几十个 Flate 流里找。
    """
    data = _read_window(path, size)
    if b"%PDF" not in data[:1024]:
        return CORRUPT, None
    if b"/Encrypt" in data:
        return PASSWORD, None
    if any(marker in data for marker in _PDF_RAW_MARKERS):
        return None, "text"
    for index, match in enumerate(_STREAM_RE.finditer(data)):
        if index >= PDF_MAX_STREAMS:
            break
        end = data.find(b"endstream", match.end())
        if end == -1:
            break
        try:
            inflated = zlib.decompressobj().decompress(data[match.end():end], 4 * 1024 * 1024)
        except zlib.error:
            continue
        if any(marker in inflated for marker in _PDF_STREAM_MARKERS):
            return None, "text"
    if b"/Image" in data or b"/XObject" in data:
        return None, "scanned"
    return None, "unknown"


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def peek_ooxml(path: Path) -> str | None:
    with path.open("rb") as handle:
        head = handle.read(8)
    if head == _OLE_MAGIC:
        # 加密的 docx/xlsx/pptx 外面是一层 OLE 容器
        return PASSWORD
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return CORRUPT
    return None if "[Content_Types].xml" in names else CORRUPT


def peek_readable(path: Path) -> None:
    with path.open("rb") as handle:
        handle.read(16)


def ffprobe_duration(path: Path, ffprobe: str, timeout: int = MEDIA_PROBE_TIMEOUT) -> tuple[str | None, float | None]:
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return TIMEOUT, None
    except OSError:
        return None, None
    if result.returncode != 0:
        return CORRUPT, None
    try:
        return None, float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None, None


# —— 盘点 ——


class Walk:
    def __init__(self, *, probe_media: bool = True, progress: Callable[[int], None] | None = None):
        self.ffprobe = shutil.which("ffprobe") if probe_media else None
        self.probe_media = probe_media
        self.progress = progress
        self.layers = {
            key: {"count": 0, "bytes": 0} for key in (TEXT, IMAGE, MEDIA, NAME_ONLY)
        }
        self.layers[IMAGE]["small"] = 0
        self.layers[MEDIA].update({"audio": 0, "video": 0, "seconds": 0.0, "probed": 0})
        self.extensions: Counter[str] = Counter()
        self.pdf = Counter({"text": 0, "scanned": 0, "unknown": 0})
        self.unreadable: dict[str, dict[str, Any]] = {
            reason: {"count": 0, "samples": []} for reason in REASON_LABELS
        }
        self.name_only_dirs: dict[str, dict[str, int]] = {}
        self.skipped_system = 0
        self.symlinks = 0
        self.packages = 0
        self.files = 0
        self.bytes = 0
        self.largest: list[tuple[int, str]] = []
        self.duplicate_keys: Counter[tuple[str, int]] = Counter()
        self.roots: list[dict[str, Any]] = []

    # 读不了的记一笔
    def _unreadable(self, reason: str, path: Path, note: str | None = None) -> None:
        bucket = self.unreadable[reason]
        bucket["count"] += 1
        if len(bucket["samples"]) < SAMPLES_PER_REASON:
            bucket["samples"].append(str(path) if note is None else f"{path}（{note}）")

    def _tick(self) -> None:
        self.files += 1
        if self.progress and self.files % PROGRESS_EVERY == 0:
            self.progress(self.files)

    def _count(self, layer: str, name: str, size: int, path: Path) -> None:
        self._tick()
        self.bytes += size
        self.layers[layer]["count"] += 1
        self.layers[layer]["bytes"] += size
        self.extensions[file_ext(name) or "（无扩展名）"] += 1
        self.duplicate_keys[(name, size)] += 1
        if len(self.largest) < 10 or size > self.largest[-1][0]:
            self.largest.append((size, str(path)))
            self.largest.sort(key=lambda item: -item[0])
            del self.largest[10:]

    def _name_only_tree(self, path: Path, label: str) -> None:
        """只收文件名的目录：数文件、算大小，不看内容。"""
        bucket = self.name_only_dirs.setdefault(label, {"dirs": 0, "files": 0, "bytes": 0})
        bucket["dirs"] += 1
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [name for name in dirs if not is_system_shadow(name)]
            for name in files:
                if is_system_shadow(name):
                    continue
                try:
                    info = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                bucket["files"] += 1
                bucket["bytes"] += info.st_size
                self.bytes += info.st_size
                self._tick()

    def _package(self, path: Path, name: str) -> None:
        """在 Finder 里是一个文件的包：整体算一个，大小累加里面所有文件。"""
        self.packages += 1
        size = 0
        for root, _dirs, files in os.walk(path, followlinks=False):
            for file_name in files:
                try:
                    size += os.lstat(os.path.join(root, file_name)).st_size
                except OSError:
                    continue
        layer = TEXT if file_ext(name) in IWORK_EXTS else NAME_ONLY
        self._count(layer, name, size, path)
        if file_ext(name) in IWORK_EXTS:
            self._unreadable(UNSUPPORTED, path, "Keynote / Pages / Numbers 要另想办法取正文")

    def _file(self, path: Path, name: str, size: int) -> None:
        layer = layer_for(name)
        ext = file_ext(name)
        self._count(layer, name, size, path)
        try:
            if not os.access(path, os.R_OK):
                self._unreadable(PERMISSION, path)
                return
            if ext == "pdf":
                reason, text_layer = peek_pdf(path, size)
                if reason:
                    self._unreadable(reason, path)
                else:
                    self.pdf[text_layer or "unknown"] += 1
            elif ext in OOXML_EXTS:
                reason = peek_ooxml(path)
                if reason:
                    self._unreadable(reason, path)
            elif ext in IWORK_EXTS:
                self._unreadable(UNSUPPORTED, path, "Keynote / Pages / Numbers 要另想办法取正文")
            elif layer == IMAGE:
                peek_readable(path)
                if size < SMALL_IMAGE_BYTES:
                    self.layers[IMAGE]["small"] += 1
            elif layer == MEDIA:
                self.layers[MEDIA]["audio" if ext in AUDIO_EXTS else "video"] += 1
                if self.ffprobe:
                    reason, seconds = ffprobe_duration(path, self.ffprobe)
                    if reason:
                        self._unreadable(reason, path)
                    elif seconds is not None:
                        self.layers[MEDIA]["seconds"] += seconds
                        self.layers[MEDIA]["probed"] += 1
        except PermissionError:
            self._unreadable(PERMISSION, path)
        except OSError as error:
            self._unreadable(CORRUPT, path, error.strerror or type(error).__name__)

    def walk_root(self, root: Path) -> dict[str, int]:
        before_files, before_bytes = self.files, self.bytes
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda entry: entry.name)
            except PermissionError:
                self._unreadable(PERMISSION, directory, "文件夹打不开")
                continue
            except OSError as error:
                self._unreadable(CORRUPT, directory, error.strerror or "文件夹读不了")
                continue
            subdirs: list[Path] = []
            for entry in children:
                name = entry.name
                if is_system_shadow(name):
                    self.skipped_system += 1
                    continue
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        self.symlinks += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if name in NAME_ONLY_DIRS or name.startswith("."):
                            self._name_only_tree(path, name if name in NAME_ONLY_DIRS else "其他隐藏文件夹")
                        elif file_ext(name) in PACKAGE_EXTS:
                            self._package(path, name)
                        else:
                            subdirs.append(path)
                        continue
                    info = entry.stat(follow_symlinks=False)
                except PermissionError:
                    self._unreadable(PERMISSION, path)
                    continue
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                self._file(path, name, info.st_size)
            stack.extend(reversed(subdirs))
        return {"files": self.files - before_files, "bytes": self.bytes - before_bytes}

    def report(self, *, elapsed: float) -> dict[str, Any]:
        duplicate_groups = [(key, count) for key, count in self.duplicate_keys.items() if count > 1]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "roots": self.roots,
            "files": self.files,
            "bytes": self.bytes,
            "layers": self.layers,
            "pdf": dict(self.pdf),
            "unreadable": self.unreadable,
            "name_only_dirs": self.name_only_dirs,
            "skipped_system": self.skipped_system,
            "symlinks": self.symlinks,
            "packages": self.packages,
            "media_probe": "ffprobe" if self.ffprobe else ("off" if not self.probe_media else "missing"),
            "extensions": self.extensions.most_common(30),
            "largest": [{"path": path, "bytes": size} for size, path in self.largest],
            "duplicates": {
                "groups": len(duplicate_groups),
                "extra_files": sum(count - 1 for _key, count in duplicate_groups),
                "extra_bytes": sum(key[1] * (count - 1) for key, count in duplicate_groups),
            },
        }


def walk_materials(
    roots: Iterable[dict[str, Any]],
    *,
    probe_media: bool = True,
    progress: Callable[[int], None] | None = None,
    state_of: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """roots：[{path, project_id?, project_name?}]。盘没插、文件夹没了的只记下来不走。"""
    from .materials import ROOT_ONLINE, volume_state

    state_of = state_of or volume_state
    started = time.monotonic()
    walker = Walk(probe_media=probe_media, progress=progress)
    seen: set[str] = set()
    for root in roots:
        path = str(root["path"])
        entry = {
            "path": path,
            "project_id": root.get("project_id"),
            "project_name": root.get("project_name"),
            "state": state_of(path),
            "files": 0,
            "bytes": 0,
        }
        walker.roots.append(entry)
        real = os.path.realpath(path)
        # 嵌套挂载（A 挂了 /x，B 挂了 /x/y）只走外层一次，内层标出来
        nested_in = next((other for other in seen if real == other or real.startswith(other + os.sep)), None)
        if nested_in is not None:
            entry["state"] = "nested"
            entry["nested_in"] = nested_in
            continue
        if entry["state"] != ROOT_ONLINE:
            continue
        seen.add(real)
        entry.update(walker.walk_root(Path(path)))
    return walker.report(elapsed=time.monotonic() - started)


# —— 报告 ——


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def render_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    layers = report["layers"]
    lines.append(
        f"材料盘点（只读，没改任何东西）：{report['files']} 个文件，{human_bytes(report['bytes'])}，"
        f"用时 {report['elapsed_seconds']} 秒"
    )
    lines.append("")
    lines.append("文件夹：")
    labels = {
        "online": "",
        "volume_offline": "（盘没插，没走）",
        "missing": "（文件夹找不到，没走）",
        "nested": "（在另一个挂载的文件夹里面，已经一起算了）",
    }
    for root in report["roots"]:
        project = f"{root['project_name']}：" if root.get("project_name") else ""
        detail = labels.get(root["state"], f"（{root['state']}）")
        counted = f" {root['files']} 个，{human_bytes(root['bytes'])}" if root["state"] == "online" else ""
        lines.append(f"  {project}{root['path']}{detail}{counted}")
    lines.append("")
    lines.append("按第三期要做的事分：")
    for key in (TEXT, IMAGE, MEDIA, NAME_ONLY):
        layer = layers[key]
        extra = ""
        if key == IMAGE and layer["small"]:
            extra = f"，其中 {layer['small']} 张小于 20 KB（多半是图标）"
        if key == MEDIA:
            extra = f"（音频 {layer['audio']}、视频 {layer['video']}）"
            if report["media_probe"] == "ffprobe":
                extra += f"，读到时长的 {layer['probed']} 个共 {layer['seconds'] / 3600:.1f} 小时"
            elif report["media_probe"] == "missing":
                extra += "，没装 ffprobe，没算时长"
        lines.append(f"  {LAYER_LABELS[key]}：{layer['count']} 个，{human_bytes(layer['bytes'])}{extra}")
    pdf = report["pdf"]
    if sum(pdf.values()):
        lines.append(
            f"  PDF：有文字层 {pdf['text']} 个，像扫描件 {pdf['scanned']} 个（要走图片文字识别），"
            f"看不出来 {pdf['unknown']} 个"
        )
    if report["name_only_dirs"]:
        lines.append("")
        lines.append("只收文件名的文件夹：")
        for name, bucket in sorted(report["name_only_dirs"].items()):
            lines.append(f"  {name}：{bucket['dirs']} 个文件夹，{bucket['files']} 个文件，{human_bytes(bucket['bytes'])}")
    unreadable = report["unreadable"]
    if any(bucket["count"] for bucket in unreadable.values()):
        lines.append("")
        lines.append("读不了的（文件名照样能搜到）：")
        for reason, bucket in unreadable.items():
            if not bucket["count"]:
                continue
            lines.append(f"  {REASON_LABELS[reason]}：{bucket['count']} 个")
            for sample in bucket["samples"][:3]:
                lines.append(f"    例：{sample}")
    lines.append("")
    duplicates = report["duplicates"]
    lines.append(
        f"其他：跳过系统影子文件 {report['skipped_system']} 个，没跟随的链接 {report['symlinks']} 个，"
        f"按一个文件算的包 {report['packages']} 个；同名同大小的 {duplicates['groups']} 组"
        f"（多出来 {duplicates['extra_files']} 个，{human_bytes(duplicates['extra_bytes'])}）"
    )
    if report["extensions"]:
        top = "、".join(f"{ext} {count}" for ext, count in report["extensions"][:12])
        lines.append(f"最多的扩展名：{top}")
    if report["largest"]:
        lines.append("最大的几个文件：")
        for item in report["largest"][:5]:
            lines.append(f"  {human_bytes(item['bytes'])}  {item['path']}")
    return "\n".join(lines)


def stderr_progress(count: int) -> None:
    print(f"已走过 {count} 个文件…", file=sys.stderr, flush=True)
