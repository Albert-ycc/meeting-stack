"""图片文字和 PDF（第三期 3c）：认字引擎的选择、探测，图片和 PDF 的读取器。

- PDF 的文字层一直由 Vision 程序里的 PDFKit 读，和选哪套认字引擎无关；只有没有 swiftc 或编译失败时
  PDF 才记 waiting、note=engine_missing。
- 认字引擎只决定图片和扫描页用哪套：app_state.ocr_engine，默认 auto：Vision 程序能用就用 Vision，
  否则 tesseract（装了中文语言包才算能用），都没有就记「识别程序没装」。off 时只读 PDF 的文字层，
  图片记 done、不认字；换回别的引擎时这些再读一遍，已经认过的不重认。
- 每 10 分钟看一次各个程序有没有新装上，有了就把对应层的 waiting 改回 pending。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import extract_formats
from .db import Database, utc_now
from .material_content import ExtractResult
from .material_helpers import (
    HelperCrashed,
    HelperProcess,
    HelperStopped,
    HelperTimeout,
    StopFlag,
    run_background,
)
from .material_rules import LAYER_IMAGE, LAYER_MEDIA, LAYER_PDF, LAYER_TEXT, file_ext
from .vision_helper import VisionBuild, find_swiftc

logger = logging.getLogger(__name__)

AUTO = "auto"
VISION = "vision"
TESSERACT = "tesseract"
OFF = "off"
ENGINES = (AUTO, VISION, TESSERACT, OFF)
ENGINE_KEY = "ocr_engine"

CHECK_SECONDS = 600.0
FIRST_TIMEOUT = 60.0
RETRY_TIMEOUT = 120.0
PDF_TOTAL_SECONDS = 1800.0
PDF_MAX_PAGES = 300
MIN_SIDE = 80
MIN_PIXELS = 60_000
TEXT_LAYER_MIN_CHARS = 20
TEXT_LAYER_BAD_RATIO = 0.3
IMAGE_VERSION = 1
PDF_VERSION = 2
SIPS_EXTS = frozenset({"heic", "heif", "webp"})
HOMEBREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")
VISION_RSS_LIMIT = 2 * 1024 * 1024 * 1024


# ---------------------------------------------------------------------- 程序在不在


def find_tool(name: str, *, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """先按 PATH 找，再看 Homebrew 的两个位置（后台服务的 PATH 里常常没有它们）。"""
    found = which(name)
    if found:
        return found
    for folder in HOMEBREW_BINS:
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass
class Tools:
    swiftc: str | None = None
    tesseract: str | None = None
    tesseract_chinese: bool = False
    tesseract_version: str = ""
    textutil: str | None = None
    sips: str | None = None
    ffmpeg: str | None = None
    ffprobe: str | None = None
    funasr_python: str | None = None
    media_script: str | None = None


def probe_tools(
    settings: Any,
    *,
    system: str = sys.platform,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> Tools:
    tools = Tools()
    tools.swiftc = find_swiftc(system=system, run=run)
    tesseract = find_tool("tesseract", which=which)
    if tesseract:
        tools.tesseract = tesseract
        try:
            listed = run(
                [tesseract, "--list-langs"], capture_output=True, text=True, timeout=30, check=False
            )
            tools.tesseract_chinese = "chi_sim" in (listed.stdout or "").split()
            version = run(
                [tesseract, "--version"], capture_output=True, text=True, timeout=30, check=False
            )
            lines = ((version.stdout or "") + (version.stderr or "")).strip().splitlines()
            tools.tesseract_version = lines[0] if lines else ""
        except (OSError, subprocess.SubprocessError):
            tools.tesseract_chinese = False
    override = os.environ.get("MEETING_WORKBENCH_TEXTUTIL", "").strip()
    tools.textutil = override or ("textutil" if system == "darwin" else None)
    tools.sips = find_tool("sips", which=which) if system == "darwin" else None
    tools.ffmpeg = find_tool("ffmpeg", which=which)
    tools.ffprobe = find_tool("ffprobe", which=which)
    funasr = getattr(settings, "funasr_python", None)
    if funasr and os.path.isfile(funasr) and os.access(funasr, os.X_OK):
        tools.funasr_python = str(funasr)
    script = getattr(settings, "material_transcriber", None)
    if script and os.path.isfile(script):
        tools.media_script = str(script)
    return tools


def media_missing(tools: Tools) -> list[str]:
    """材料录音转写缺什么（ffmpeg 或 funasr；项目页按它写怎么装）。"""
    missing = []
    if not (tools.ffmpeg and tools.ffprobe):
        missing.append("ffmpeg")
    if not (tools.funasr_python and tools.media_script):
        missing.append("funasr")
    return missing


def machine_info(
    *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> dict[str, str]:
    """macOS 版本和芯片型号，给 doctor 和试跑报告用。"""
    info = {"macos": platform.mac_ver()[0] or platform.platform(), "chip": platform.machine()}
    if sys.platform == "darwin":
        try:
            result = run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                info["chip"] = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return info


# ---------------------------------------------------------------------- 图片尺寸


def image_size(path: Path) -> tuple[int, int] | None:
    """只读文件头拿像素尺寸：PNG、JPEG、GIF、BMP、WebP、TIFF。读不出来（例如 HEIC）返回 None。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(512 * 1024)
    except OSError:
        return None
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head.startswith(b"BM"):
            width, height = struct.unpack("<ii", head[18:26])
            return abs(width), abs(height)
        if head.startswith(b"\xff\xd8"):
            return _jpeg_size(head)
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return _webp_size(head)
        if head[:4] in (b"II*\x00", b"MM\x00*"):
            return _tiff_size(head)
    except (struct.error, IndexError):
        return None
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    position = 2
    while position + 9 < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            position += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack(">H", data[position + 2 : position + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            return width, height
        position += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


def _tiff_size(data: bytes) -> tuple[int, int] | None:
    order = "<" if data[:2] == b"II" else ">"
    offset = struct.unpack(order + "I", data[4:8])[0]
    count = struct.unpack(order + "H", data[offset : offset + 2])[0]
    values: dict[int, int] = {}
    for index in range(count):
        entry = data[offset + 2 + index * 12 : offset + 14 + index * 12]
        tag, kind = struct.unpack(order + "HH", entry[:4])
        if tag in (256, 257):
            values[tag] = struct.unpack(
                order + ("H" if kind == 3 else "I"), entry[8 : 10 if kind == 3 else 12]
            )[0]
    if 256 in values and 257 in values:
        return values[256], values[257]
    return None


def is_small(width: int, height: int) -> bool:
    """小图不认：短边小于 80 像素，或总像素少于 6 万。不按文件大小判断（一行字的小截图只有几 KB）。"""
    return min(width, height) < MIN_SIDE or width * height < MIN_PIXELS


# ---------------------------------------------------------------------- 认字结果的整理

_HAN = r"[㐀-䶿一-鿿豈-﫿　-〿＀-￯]"
_HAN_SPACE = re.compile(rf"(?<={_HAN})[ \t　]+(?={_HAN})")
_HAN_BREAK = re.compile(rf"(?<={_HAN})\n(?={_HAN})")


def tidy_ocr_text(text: str) -> str:
    """两个汉字之间的空格去掉（tesseract 会输出「数 理 协 会」，全文索引按三个字一组切，空格会把词
    切断）；同一段里前后都是汉字的换行直接连上。"""
    text = _HAN_SPACE.sub("", text.replace("\r", ""))
    return _HAN_BREAK.sub("", text).strip()


def ocr_paragraphs(text: str) -> list[str]:
    return [tidy_ocr_text(part) for part in re.split(r"\n\s*\n", text) if part.strip()]


def arrange_lines(lines: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Vision 回的行：按页、从上到下、从左到右排好，合成段落；长截图块间重叠处的重复行去掉。
    返回 [(页, 段落)]。"""
    items = []
    for line in lines:
        text = str(line.get("t") or "").strip()
        if not text:
            continue
        items.append(
            {
                "t": text,
                "p": int(line.get("p") or 0),
                "x": float(line.get("x") or 0),
                "y": float(line.get("y") or 0),
                "h": max(float(line.get("h") or 0), 1e-6),
            }
        )
    items.sort(key=lambda item: (item["p"], item["y"], item["x"]))
    kept: list[dict[str, Any]] = []
    for item in items:
        duplicate = any(
            other["p"] == item["p"]
            and other["t"] == item["t"]
            and abs(other["y"] - item["y"]) <= max(other["h"], item["h"])
            for other in kept[-30:]
        )
        if not duplicate:
            kept.append(item)
    rows: list[list[dict[str, Any]]] = []
    for item in kept:
        if rows:
            last = rows[-1]
            center = sum(other["y"] + other["h"] / 2 for other in last) / len(last)
            height = min(min(other["h"] for other in last), item["h"])
            if last[0]["p"] == item["p"] and abs(item["y"] + item["h"] / 2 - center) < height * 0.5:
                last.append(item)
                continue
        rows.append([item])
    heights = sorted(item["h"] for item in kept) or [0.0]
    median = heights[len(heights) // 2]
    paragraphs: list[tuple[int, list[str]]] = []
    previous_bottom: float | None = None
    previous_page: int | None = None
    for row in rows:
        row.sort(key=lambda item: item["x"])
        text = " ".join(item["t"] for item in row)
        top = min(item["y"] for item in row)
        bottom = max(item["y"] + item["h"] for item in row)
        page = row[0]["p"]
        new_paragraph = (
            not paragraphs
            or page != previous_page
            or (previous_bottom is not None and top - previous_bottom > median * 0.8)
        )
        if new_paragraph:
            paragraphs.append((page, [text]))
        else:
            paragraphs[-1][1].append(text)
        previous_bottom = bottom
        previous_page = page
    result = []
    for page, texts in paragraphs:
        text = tidy_ocr_text("\n".join(texts))
        if text:
            result.append((page, text))
    return result


def needs_ocr(text: str) -> bool:
    """PDF 一页的文字层：去掉空白后少于 20 个字，或私用区字符和替换符超过 30%（很多国内 PDF 的子集字体
    没有 ToUnicode，文字层是乱码），就渲染后认字。"""
    chars = [char for char in text if not char.isspace()]
    if len(chars) < TEXT_LAYER_MIN_CHARS:
        return True
    bad = sum(1 for char in chars if "" <= char <= "" or char == "�")
    return bad / len(chars) > TEXT_LAYER_BAD_RATIO


# ---------------------------------------------------------------------- 引擎


def _read_head(path: Path) -> tuple[bytes, str | None]:
    """先自己打开一次：没有权限、IO 错在这里分出来（认字程序只会说读不了）。"""
    try:
        with path.open("rb") as handle:
            return handle.read(16), None
    except PermissionError:
        return b"", "permission"
    except OSError:
        return b"", "io_error"


def _key16(row: dict[str, Any]) -> str:
    key = str(row.get("content_key") or "")
    return re.sub(r"[^0-9a-f]", "", key.split(":", 1)[-1])[:16] or "x"


class OcrEngines:
    """认字引擎的选择、探测、Vision 常驻进程。服务里只有一个，图片和 PDF 两个读取器共用。"""

    def __init__(
        self,
        db: Database,
        settings: Any,
        *,
        stop: StopFlag | None = None,
        busy_check: Callable[[], bool] | None = None,
        system: str = sys.platform,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        clock: Callable[[], float] = time.monotonic,
        build: VisionBuild | None = None,
        helper_factory: Callable[[Path], Any] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.stop = stop
        self.busy_check = busy_check
        self.system = system
        self.run = run
        self.which = which
        self.clock = clock
        self.build = build or VisionBuild(
            self.data_dir, run=run, find=lambda: find_swiftc(system=system, run=run)
        )
        self._helper_factory = helper_factory
        self._helper: Any = None
        self._helper_binary: Path | None = None
        self._tools: Tools | None = None
        self._checked_at: float | None = None

    # ---------------------------------------------------------------- 探测

    def tools(self) -> Tools:
        if self._tools is None:
            self._tools = probe_tools(
                self.settings, system=self.system, run=self.run, which=self.which
            )
        return self._tools

    def refresh(self, *, force: bool = False) -> dict[str, int]:
        """每 10 分钟最多一次：重新看各个程序，该编译就在后台编译 Vision 程序，装上了的把 waiting 改回
        pending。每轮材料循环开始时调；force 用于命令行切换引擎。"""
        now = self.clock()
        if not force and self._checked_at is not None and now - self._checked_at < CHECK_SECONDS:
            return {}
        self._checked_at = now
        self._tools = probe_tools(self.settings, system=self.system, run=self.run, which=self.which)
        self.build.ensure(background=not force)
        return self.requeue_waiting()

    def layer_ready(self, layer: str) -> bool:
        tools = self.tools()
        if layer == LAYER_TEXT:
            return tools.textutil is not None
        if layer == LAYER_PDF:
            return self.build.ready()
        if layer == LAYER_IMAGE:
            return self.image_engine() is not None
        if layer == LAYER_MEDIA:
            return not media_missing(tools)
        return False

    def requeue_waiting(self) -> dict[str, int]:
        """装上了：把对应层的 waiting 改回 pending，「装好后自动接着读」靠的就是这个。"""
        moved: dict[str, int] = {}
        for layer in (LAYER_TEXT, LAYER_PDF, LAYER_IMAGE, LAYER_MEDIA):
            if not self.layer_ready(layer):
                continue
            count = self.db.execute_rowcount(
                """UPDATE material_contents SET state = 'pending', note = NULL, next_try_at = NULL, updated_at = ?
                    WHERE state = 'waiting' AND layer = ?""",
                (utc_now(), layer),
            )
            if count:
                moved[layer] = count
        return moved

    # ---------------------------------------------------------------- 选择

    def setting(self) -> str:
        row = self.db.query_one("SELECT value FROM app_state WHERE key = ?", (ENGINE_KEY,))
        value = str(row["value"]) if row and row["value"] else AUTO
        return value if value in ENGINES else AUTO

    def image_engine(self) -> str | None:
        """图片和扫描页用哪套：vision、tesseract、off，或 None（都没装）。"""
        setting = self.setting()
        if setting == OFF:
            return OFF
        tools = self.tools()
        tesseract = bool(tools.tesseract and tools.tesseract_chinese)
        vision = self.build.ready()
        if setting == VISION:
            return VISION if vision else None
        if setting == TESSERACT:
            return TESSERACT if tesseract else None
        if vision:
            return VISION
        return TESSERACT if tesseract else None

    def compiling(self) -> bool:
        return self.build.compiling()

    def set_engine(self, engine: str) -> dict[str, int]:
        """命令 materials ocr-engine：写 app_state，不用重启。已经认过的不重认；off 时跳过的图片和扫描页，
        换回别的引擎时再读一遍。"""
        if engine not in ENGINES:
            raise ValueError(f"不认识的引擎：{engine}")
        before = self.setting()
        stamp = utc_now()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (ENGINE_KEY, engine, stamp),
            )
            reread = 0
            if before == OFF and engine != OFF:
                reread = connection.execute(
                    """UPDATE material_contents SET state = 'pending', attempts = 0, next_try_at = NULL,
                              updated_at = ?
                        WHERE state = 'done' AND ((layer = 'image' AND extractor = 'off')
                                                  OR (layer = 'pdf' AND extractor = 'pdfkit+off'))""",
                    (stamp,),
                ).rowcount
        return {"reread": reread}

    # ---------------------------------------------------------------- Vision 常驻进程

    def vision(self) -> Any:
        binary = self.build.binary
        if self._helper is not None and self._helper_binary == binary:
            return self._helper
        if self._helper is not None:
            self._helper.close()
        if self._helper_factory is not None:
            self._helper = self._helper_factory(binary)
        else:
            # Vision 认 4096 像素的大图、切长截图时内存比读文档大，上限放到 2GB
            self._helper = HelperProcess(
                "vision",
                [str(binary)],
                data_dir=self.data_dir,
                stop=self.stop,
                rss_limit_bytes=VISION_RSS_LIMIT,
            )
        self._helper_binary = binary
        return self._helper

    def close(self) -> None:
        if self._helper is not None:
            self._helper.close()
            self._helper = None

    # ---------------------------------------------------------------- tesseract

    def tesseract_text(self, path: Path, *, timeout: float) -> str:
        """tesseract <图> stdout：OMP_THREAD_LIMIT=1、后台优先级，结果从 stdout 取，不让它写文件。"""
        tools = self.tools()
        assert tools.tesseract is not None
        result = run_background(
            [
                tools.tesseract,
                str(path),
                "stdout",
                "-l",
                "chi_sim+eng",
                "-c",
                "preserve_interword_spaces=1",
            ],
            timeout=timeout,
            stop=self.stop,
            env={"OMP_THREAD_LIMIT": "1"},
        )
        if result.returncode != 0:
            raise extract_formats.Unreadable(
                extract_formats.CORRUPT,
                (result.stderr or b"").decode("utf-8", errors="replace")[-200:],
            )
        return result.stdout.decode("utf-8", errors="replace")

    def temp_dir(self) -> Path:
        folder = self.data_dir / "material-ocr-tmp"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def to_png(self, path: Path, row: dict[str, Any], *, timeout: float) -> Path:
        """HEIC、WebP 先用 sips 转成 PNG，写到 data_dir（不在资料盘上留东西）。"""
        tools = self.tools()
        if not tools.sips:
            raise extract_formats.Unreadable(extract_formats.UNSUPPORTED, "没有 sips")
        out = self.temp_dir() / f"{_key16(row)}.png"
        result = run_background(
            [tools.sips, "-s", "format", "png", "-Z", "4096", str(path), "--out", str(out)],
            timeout=timeout,
            stop=self.stop,
        )
        if result.returncode != 0 or not out.is_file():
            out.unlink(missing_ok=True)
            raise extract_formats.Unreadable(extract_formats.CORRUPT, "sips 转不了")
        return out


def _timeout_for(row: dict[str, Any]) -> float:
    return RETRY_TIMEOUT if row.get("reason") == "timeout" else FIRST_TIMEOUT


class ImageExtractor:
    """图片文字：MaterialContent 的 extractors["image"]。"""

    version = IMAGE_VERSION

    def __init__(self, engines: OcrEngines):
        self.engines = engines

    def available(self) -> bool:
        """Vision 正在编译时这一层先不读（auto 不该趁这一两分钟用 tesseract 认掉一批）。"""
        return not (self.engines.compiling() and self.engines.setting() in (AUTO, VISION))

    def __call__(self, path: Path, layer: str, row: dict[str, Any]) -> ExtractResult:
        head, problem = _read_head(path)
        if problem:
            return ExtractResult(status=problem)
        if head.startswith(b"%PDF"):
            return ExtractResult(status="relayer", layer=LAYER_PDF)
        engine = self.engines.image_engine()
        if engine is None:
            return ExtractResult(status="waiting", what="ocr")
        if engine == OFF:
            return ExtractResult(status="ok", extractor=OFF, extractor_version=self.version)
        size = image_size(path)
        if size and is_small(*size):
            return ExtractResult(
                status="ok", note="small_image", extractor=engine, extractor_version=self.version
            )
        started = time.monotonic()
        try:
            if engine == VISION:
                result = self._vision(path, row)
            else:
                result = self._tesseract(path, row)
        except (HelperTimeout, HelperCrashed) as error:
            logger.info("认字超时：%s（%s）", path, error)
            return ExtractResult(status="timeout", extractor=engine, extractor_version=self.version)
        except extract_formats.Unreadable as error:
            return ExtractResult(
                status=error.reason, extractor=engine, extractor_version=self.version
            )
        result.extractor = engine
        result.extractor_version = self.version
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if result.status == "ok" and not result.note and not result.blocks:
            result.note = "no_text"
        return result

    def _vision(self, path: Path, row: dict[str, Any]) -> ExtractResult:
        answer = self.engines.vision().request(
            {"cmd": "image", "path": str(path)}, timeout=_timeout_for(row)
        )
        status = answer.get("status")
        if status != "ok":
            if status == "error":
                logger.warning("Vision 认不了 %s：%s", path, answer.get("error"))
            return ExtractResult(status="corrupt")
        if answer.get("small"):
            return ExtractResult(status="ok", note="small_image")
        pages = int(answer.get("pages") or 1)
        blocks = [
            {"loc": f"第 {page + 1} 页" if pages > 1 else None, "text": text}
            for page, text in arrange_lines(answer.get("lines") or [])
        ]
        return ExtractResult(
            status="ok",
            blocks=blocks,
            pages=pages if pages > 1 else None,
            truncated=bool(answer.get("truncated")),
        )

    def _tesseract(self, path: Path, row: dict[str, Any]) -> ExtractResult:
        timeout = _timeout_for(row)
        source = path
        converted: Path | None = None
        try:
            if file_ext(path.name) in SIPS_EXTS or image_size(path) is None:
                converted = self.engines.to_png(path, row, timeout=timeout)
                source = converted
                size = image_size(converted)
                if size and is_small(*size):
                    return ExtractResult(status="ok", note="small_image")
            text = self.engines.tesseract_text(source, timeout=timeout)
        finally:
            if converted is not None:
                converted.unlink(missing_ok=True)
        return ExtractResult(
            status="ok", blocks=[{"loc": None, "text": part} for part in ocr_paragraphs(text)]
        )


class PdfExtractor:
    """PDF：MaterialContent 的 extractors["pdf"]。文字层由 PDFKit 读；字少或乱码的页按选的引擎认字。
    一页一个请求，每页 60 秒；每页之间查一次忙信号，忙了杀掉程序、这份下次从头读，不记超时；
    中途超时的，已经读到的页照样写入，note=truncated。整份最多 30 分钟、300 页。"""

    version = PDF_VERSION

    def __init__(self, engines: OcrEngines, *, clock: Callable[[], float] = time.monotonic):
        self.engines = engines
        self.clock = clock

    def available(self) -> bool:
        return not self.engines.compiling()

    def __call__(self, path: Path, layer: str, row: dict[str, Any]) -> ExtractResult:
        head, problem = _read_head(path)
        if problem:
            return ExtractResult(status=problem)
        if not head.startswith(b"%PDF") and b"%PDF-" not in head:
            kind = extract_formats.sniff(path, "pdf")
            if kind in {"zip", "cfb", "html", "rtf", "email"}:
                return ExtractResult(status="relayer", layer=LAYER_TEXT)
            if image_size(path) is not None:
                return ExtractResult(status="relayer", layer=LAYER_IMAGE)
            return ExtractResult(status="corrupt")
        if not self.engines.build.ready():
            return ExtractResult(status="waiting", what="vision")
        engine = self.engines.image_engine()
        mode = engine or OFF
        extractor = f"pdfkit+{mode}"
        helper = self.engines.vision()
        timeout = _timeout_for(row)
        started = self.clock()
        try:
            opened = helper.request({"cmd": "pdf_open", "path": str(path)}, timeout=timeout)
        except (HelperTimeout, HelperCrashed):
            return ExtractResult(
                status="timeout", extractor=extractor, extractor_version=self.version
            )
        if opened.get("status") in {"password", "corrupt"}:
            return ExtractResult(
                status=str(opened["status"]), extractor=extractor, extractor_version=self.version
            )
        if opened.get("status") != "ok":
            return ExtractResult(
                status="corrupt", extractor=extractor, extractor_version=self.version
            )
        pages = int(opened.get("pages") or 0)
        truncated = pages > PDF_MAX_PAGES
        blocks: list[dict[str, Any]] = []
        for index in range(min(pages, PDF_MAX_PAGES)):
            if self.engines.busy_check is not None and self.engines.busy_check():
                helper.kill()  # 把内存还给会议转写
                raise HelperStopped("busy")
            if self.clock() - started > PDF_TOTAL_SECONDS:
                truncated = True
                break
            try:
                text = self._page(helper, path, row, index, mode, timeout)
            except (HelperTimeout, HelperCrashed):
                if not blocks:
                    return ExtractResult(
                        status="timeout", extractor=extractor, extractor_version=self.version
                    )
                truncated = True  # 只读了前 N 页
                break
            if text.strip():
                blocks.append({"loc": f"第 {index + 1} 页", "text": text})
        return ExtractResult(
            status="ok",
            blocks=blocks,
            pages=pages,
            truncated=truncated,
            duration_ms=int((self.clock() - started) * 1000),
            extractor=extractor,
            extractor_version=self.version,
        )

    def _page(
        self, helper: Any, path: Path, row: dict[str, Any], index: int, mode: str, timeout: float
    ) -> str:
        answer = helper.request(
            {"cmd": "pdf_text", "path": str(path), "page": index}, timeout=timeout
        )
        text = str(answer.get("text") or "") if answer.get("status") == "ok" else ""
        if not needs_ocr(text) or mode not in (VISION, TESSERACT):
            return text.strip()
        if mode == VISION:
            answer = helper.request(
                {"cmd": "pdf_ocr", "path": str(path), "page": index}, timeout=timeout
            )
            recognized = "\n\n".join(
                part for _page, part in arrange_lines(answer.get("lines") or [])
            )
        else:
            out = self.engines.temp_dir() / f"{_key16(row)}-p{index + 1}.png"
            try:
                answer = helper.request(
                    {"cmd": "pdf_render", "path": str(path), "page": index, "out": str(out)},
                    timeout=timeout,
                )
                recognized = ""
                if answer.get("status") == "ok" and out.is_file():
                    try:
                        recognized = "\n\n".join(
                            ocr_paragraphs(self.engines.tesseract_text(out, timeout=timeout))
                        )
                    except extract_formats.Unreadable:
                        recognized = ""
            finally:
                out.unlink(missing_ok=True)
        return recognized.strip() or text.strip()


# ---------------------------------------------------------------------- doctor


def tools_report(settings: Any, *, build: VisionBuild | None = None) -> dict[str, Any]:
    """doctor 的新项：只报告，不进 required，缺了也不让 doctor 失败。不在前台等编译。"""
    tools = probe_tools(settings)
    build = build or VisionBuild(Path(settings.data_dir))
    tesseract = "没装（brew install tesseract tesseract-lang）"
    if tools.tesseract:
        tesseract = (
            f"能用（{tools.tesseract_version}）"
            if tools.tesseract_chinese
            else "没有中文语言包（brew install tesseract-lang）"
        )
    return {
        "vision": build.describe() if sys.platform == "darwin" else "只能在 Mac 上用",
        "tesseract": tesseract,
        "textutil": "能用" if tools.textutil else "没有（只有 Mac 上有）",
        "ffmpeg": "能用" if tools.ffmpeg and tools.ffprobe else "没装（brew install ffmpeg）",
        "funasr": (
            "没找到 FunASR 的 Python"
            if not tools.funasr_python
            else "能用"
            if tools.media_script
            else "没找到材料转写程序 transcribe/funasr_material.py"
        ),
        **machine_info(),
    }
