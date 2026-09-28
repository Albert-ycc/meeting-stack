"""Vision 认字程序（第三期 3c）：Swift 源码、编译、常驻进程的命令。

程序用 macOS 自带的 Vision 认字、PDFKit 读 PDF 文字层、ImageIO 解码图片，编译到
data_dir/bin/sd-vision-<源码哈希前 12 位>。常驻时一行一个 JSON 请求、一行一个回答（照 3a 的共同规矩）：

- `{"cmd": "image", "path"}`：认一张图。回 `{status, width, height, pages, small, truncated, lines}`；
  lines 每行 `{t 文字, c 置信度, x, y, w, h（都是 0 到 1，原点在左上）, p 第几页}`。
- `{"cmd": "pdf_open", "path"}`：回 `{status, pages}`；status 是 ok、password、corrupt。
- `{"cmd": "pdf_text", "path", "page"}`：一页的文字层 `{status, text}`。
- `{"cmd": "pdf_ocr", "path", "page"}`：一页按 200dpi（长边最多 4096）渲染后认字，回 lines。
- `{"cmd": "pdf_render", "path", "page", "out"}`：一页渲染成 PNG 写到 out（给 tesseract 用）。

一次性的子命令给预览图用，不占常驻进程：`sd-vision thumb <path> <max_side> <out>`、
`sd-vision page1 <path> <max_side> <out>`，写 JPEG，成功退出码 0。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COMPILE_TIMEOUT = 300
COMPILE_RETRY = timedelta(days=1)

SWIFT_SOURCE = r"""
import CoreGraphics
import Foundation
import ImageIO
import PDFKit
import Vision

// MARK: - 输出

func emit(_ object: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(object),
          var data = try? JSONSerialization.data(withJSONObject: object, options: []) else {
        return
    }
    data.append(0x0A)
    FileHandle.standardOutput.write(data)
}

func log(_ message: String) {
    if let data = (message + "\n").data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
}

// MARK: - 认字

let minConfidence: Float = 0.3
let maxSide = 4096
let longRatio = 3.0
let maxPixels = 50_000_000
let maxTiffPages = 20

/// 认一张图。top、height 是这一块在整张图里的位置（0 到 1），回来的坐标换算到整张图上。
func recognize(_ image: CGImage, top: Double = 0, height: Double = 1, page: Int = 0) throws -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
    var lines: [[String: Any]] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        if candidate.confidence < minConfidence { continue }
        let box = observation.boundingBox
        lines.append([
            "t": candidate.string,
            "c": Double(candidate.confidence),
            "x": Double(box.minX),
            "y": top + (1.0 - Double(box.maxY)) * height,
            "w": Double(box.width),
            "h": Double(box.height) * height,
            "p": page,
        ])
    }
    return lines
}

// MARK: - 图片

/// 带方向的像素尺寸（竖拍的照片宽高对调）。
func orientedSize(_ source: CGImageSource, _ index: Int) -> (Int, Int) {
    guard let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any] else {
        return (0, 0)
    }
    let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue ?? 0
    let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue ?? 0
    let orientation = (properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
    return (orientation >= 5 && orientation <= 8) ? (height, width) : (width, height)
}

/// 用缩略图接口解码：带方向、长边不超过 limit。
func decode(_ source: CGImageSource, _ index: Int, limit: Int) -> CGImage? {
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: limit,
        kCGImageSourceShouldCacheImmediately: true,
    ]
    return CGImageSourceCreateThumbnailAtIndex(source, index, options as CFDictionary)
}

/// 聊天长截图：按原图长边解码，切成高约 1.5 倍宽的块，块间重叠 10%，分别认字。
func recognizeTall(_ image: CGImage, page: Int) throws -> ([[String: Any]], Bool) {
    let width = image.width
    var height = image.height
    var truncated = false
    if width * height > maxPixels {
        height = max(1, maxPixels / max(width, 1))
        truncated = true
    }
    let tile = max(1, Int(Double(width) * 1.5))
    let step = max(1, tile - tile / 10)
    var lines: [[String: Any]] = []
    var top = 0
    while top < height {
        let rows = min(tile, height - top)
        try autoreleasepool {
            if let piece = image.cropping(to: CGRect(x: 0, y: top, width: width, height: rows)) {
                lines += try recognize(
                    piece, top: Double(top) / Double(height), height: Double(rows) / Double(height), page: page)
            }
        }
        if top + rows >= height { break }
        top += step
    }
    return (lines, truncated)
}

func handleImage(_ path: String) -> [String: Any] {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        return ["status": "corrupt"]
    }
    let count = CGImageSourceGetCount(source)
    if count == 0 { return ["status": "corrupt"] }
    let (width, height) = orientedSize(source, 0)
    if width == 0 || height == 0 { return ["status": "corrupt"] }
    if min(width, height) < 80 || width * height < 60_000 {
        return ["status": "ok", "small": true, "width": width, "height": height, "pages": 1, "lines": []]
    }
    let type = (CGImageSourceGetType(source) as String?) ?? ""
    let multiPage = type == "public.tiff"
    let pages = multiPage ? min(count, maxTiffPages) : 1
    var truncated = multiPage && count > maxTiffPages
    var lines: [[String: Any]] = []
    do {
        for page in 0..<pages {
            try autoreleasepool {
                let (pageWidth, pageHeight) = orientedSize(source, page)
                let ratio = Double(max(pageWidth, pageHeight)) / Double(max(min(pageWidth, pageHeight), 1))
                if ratio > longRatio {
                    guard let image = decode(source, page, limit: max(pageWidth, pageHeight)) else { return }
                    let (found, cut) = try recognizeTall(image, page: page)
                    lines += found
                    truncated = truncated || cut
                } else {
                    guard let image = decode(source, page, limit: maxSide) else { return }
                    lines += try recognize(image, page: page)
                }
            }
        }
    } catch {
        return ["status": "error", "error": "\(error)"]
    }
    return [
        "status": "ok", "small": false, "width": width, "height": height, "pages": pages,
        "truncated": truncated, "lines": lines,
    ]
}

// MARK: - PDF

var openPath: String? = nil
var openDocument: PDFDocument? = nil

func openPDF(_ path: String) -> (PDFDocument?, String) {
    if openPath == path, let document = openDocument {
        return (document, "ok")
    }
    openPath = nil
    openDocument = nil
    guard let document = PDFDocument(url: URL(fileURLWithPath: path)) else {
        return (nil, "corrupt")
    }
    if document.isLocked && !document.unlock(withPassword: "") {
        return (nil, "password")
    }
    openPath = path
    openDocument = document
    return (document, "ok")
}

/// 按 dpi 渲染一页（长边最多 limit），白底；页面的旋转由 getDrawingTransform 处理。
func renderPage(_ page: PDFPage, dpi: Double = 200, limit: Int = maxSide) -> CGImage? {
    guard let pdfPage = page.pageRef else { return nil }
    let box = pdfPage.getBoxRect(.mediaBox)
    let quarter = ((Int(pdfPage.rotationAngle) % 360) + 360) % 360 / 90
    let pageWidth = Double(quarter % 2 == 1 ? box.height : box.width)
    let pageHeight = Double(quarter % 2 == 1 ? box.width : box.height)
    if pageWidth <= 0 || pageHeight <= 0 { return nil }
    var scale = dpi / 72.0
    let longest = max(pageWidth, pageHeight) * scale
    if longest > Double(limit) { scale *= Double(limit) / longest }
    let width = max(1, Int(pageWidth * scale))
    let height = max(1, Int(pageHeight * scale))
    guard let context = CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ) else { return nil }
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.scaleBy(x: CGFloat(scale), y: CGFloat(scale))
    let target = CGRect(x: 0, y: 0, width: pageWidth, height: pageHeight)
    context.concatenate(pdfPage.getDrawingTransform(.mediaBox, rect: target, rotate: 0, preserveAspectRatio: true))
    context.drawPDFPage(pdfPage)
    return context.makeImage()
}

func writeImage(_ image: CGImage, to path: String, type: String) -> Bool {
    let url = URL(fileURLWithPath: path)
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, type as CFString, 1, nil) else {
        return false
    }
    let options: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 0.8]
    CGImageDestinationAddImage(destination, image, options as CFDictionary)
    return CGImageDestinationFinalize(destination)
}

func handlePDF(_ command: String, _ request: [String: Any]) -> [String: Any] {
    let path = request["path"] as? String ?? ""
    let (opened, status) = openPDF(path)
    guard let document = opened else { return ["status": status] }
    if command == "pdf_open" {
        return ["status": "ok", "pages": document.pageCount]
    }
    let index = request["page"] as? Int ?? 0
    guard index >= 0, index < document.pageCount, let page = document.page(at: index) else {
        return ["status": "error", "error": "没有这一页"]
    }
    switch command {
    case "pdf_text":
        return ["status": "ok", "text": page.string ?? ""]
    case "pdf_ocr":
        guard let image = renderPage(page) else { return ["status": "error", "error": "渲染失败"] }
        do {
            let lines = try recognize(image, page: index)
            return ["status": "ok", "width": image.width, "height": image.height, "lines": lines]
        } catch {
            return ["status": "error", "error": "\(error)"]
        }
    case "pdf_render":
        let out = request["out"] as? String ?? ""
        guard let image = renderPage(page), writeImage(image, to: out, type: "public.png") else {
            return ["status": "error", "error": "渲染失败"]
        }
        return ["status": "ok", "width": image.width, "height": image.height]
    default:
        return ["status": "error", "error": "不认识的命令"]
    }
}

// MARK: - 预览图（一次性子命令）

func thumb(_ path: String, _ side: Int, _ out: String) -> Bool {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = decode(source, 0, limit: side) else { return false }
    return writeImage(image, to: out, type: "public.jpeg")
}

func firstPage(_ path: String, _ side: Int, _ out: String) -> Bool {
    let (opened, _) = openPDF(path)
    guard let document = opened, let page = document.page(at: 0),
          let image = renderPage(page, dpi: 144, limit: side) else { return false }
    return writeImage(image, to: out, type: "public.jpeg")
}

// MARK: - 主循环

func handle(_ request: [String: Any]) -> [String: Any] {
    let command = request["cmd"] as? String ?? ""
    if command == "image" {
        return handleImage(request["path"] as? String ?? "")
    }
    if command.hasPrefix("pdf_") {
        return handlePDF(command, request)
    }
    return ["status": "error", "error": "不认识的命令"]
}

let arguments = CommandLine.arguments
if arguments.count >= 5 && (arguments[1] == "thumb" || arguments[1] == "page1") {
    let side = Int(arguments[3]) ?? 480
    let ok = arguments[1] == "thumb" ? thumb(arguments[2], side, arguments[4]) : firstPage(arguments[2], side, arguments[4])
    exit(ok ? 0 : 1)
}

while let line = readLine(strippingNewline: true) {
    if line.isEmpty { continue }
    guard let data = line.data(using: .utf8),
          let request = (try? JSONSerialization.jsonObject(with: data, options: [])) as? [String: Any] else {
        log("不是 JSON 的请求：\(line.prefix(200))")
        continue
    }
    var answer = autoreleasepool { handle(request) }
    answer["id"] = request["id"] ?? NSNull()
    emit(answer)
}
"""


def source_hash(source: str = SWIFT_SOURCE) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def binary_path(data_dir: Path, source: str = SWIFT_SOURCE) -> Path:
    return Path(data_dir) / "bin" / f"sd-vision-{source_hash(source)}"


def _marker(binary: Path) -> Path:
    return binary.with_name(binary.name + ".failed")


Runner = Callable[..., subprocess.CompletedProcess[str]]


def find_swiftc(*, system: str = sys.platform, run: Runner = subprocess.run) -> str | None:
    """有没有 swiftc：xcode-select -p 退出码为 0，再用 xcrun --find swiftc 取路径。

    不用 which：每台 Mac 上都有 /usr/bin/swiftc，它是个占位程序，没装命令行工具时一调用就会在桌面上
    弹「安装开发者工具」的对话框。"""
    if system != "darwin":
        return None
    try:
        selected = run(["xcode-select", "-p"], capture_output=True, text=True, timeout=10, check=False)
        if selected.returncode != 0:
            return None
        found = run(["xcrun", "--find", "swiftc"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    path = (found.stdout or "").strip().splitlines()
    return path[-1] if found.returncode == 0 and path else None


def swiftc_version(swiftc: str, *, run: Runner = subprocess.run) -> str:
    try:
        result = run([swiftc, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    return lines[0] if lines else ""


class VisionBuild:
    """编译 Vision 程序：后台线程里做，最多 5 分钟；先写临时名再原子改名，源码一变就重编。
    编译失败后，源码哈希和 swiftc 版本都没变就不重编，一天最多重试一次。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        source: str = SWIFT_SOURCE,
        run: Runner = subprocess.run,
        now: Callable[[], datetime] | None = None,
        find: Callable[[], str | None] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.source = source
        self.run = run
        self.now = now or (lambda: datetime.now(UTC))
        self.find = find or (lambda: find_swiftc(run=run))
        self.binary = binary_path(self.data_dir, source)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.swiftc: str | None = None

    # ---------------------------------------------------------------- 状态

    def ready(self) -> bool:
        return self.binary.is_file() and os.access(self.binary, os.X_OK)

    def failure(self) -> dict[str, Any] | None:
        try:
            return json.loads(_marker(self.binary).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def compiling(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def describe(self) -> str:
        """doctor 和项目页用的一句话。"""
        if self.ready():
            return "能用"
        if self.compiling():
            return "正在编译"
        failure = self.failure()
        if failure:
            return f"编译失败：{failure.get('error') or '没有报错信息'}"
        if self.swiftc is None and self.find() is None:
            return "没有 swiftc（先在终端运行 xcode-select --install 装 Xcode 命令行工具）"
        return "还没编译"

    # ---------------------------------------------------------------- 编译

    def should_compile(self, swiftc: str) -> bool:
        if self.ready() or self.compiling():
            return False
        failure = self.failure()
        if not failure:
            return True
        if failure.get("swiftc_version") != swiftc_version(swiftc, run=self.run):
            return True
        try:
            failed_at = datetime.fromisoformat(str(failure.get("at")))
        except ValueError:
            return True
        return self.now() - failed_at >= COMPILE_RETRY

    def ensure(self, *, background: bool = True) -> bool:
        """已经编好返回 True；没编好、该编就开始编（默认在后台线程里），返回 False。"""
        if self.ready():
            return True
        swiftc = self.find()
        self.swiftc = swiftc
        if swiftc is None:
            return False
        with self._lock:
            if not self.should_compile(swiftc):
                return self.ready()
            if not background:
                return self.compile(swiftc)
            self._thread = threading.Thread(
                target=self.compile, args=(swiftc,), name="meeting-workbench-vision-build", daemon=True
            )
            self._thread.start()
        return False

    def compile(self, swiftc: str) -> bool:
        folder = self.binary.parent
        folder.mkdir(parents=True, exist_ok=True)
        source_file = folder / f"sd-vision-{source_hash(self.source)}.swift"
        temporary = folder / f".{self.binary.name}.{os.getpid()}.tmp"
        source_file.write_text(self.source, encoding="utf-8")
        started = time.monotonic()
        error = ""
        try:
            # 经 xcrun 调：它会带上 SDK 路径。直接调 xcrun --find 找到的 swiftc，在后台服务的干净环境里
            # 找不到 SDK，报 unable to load standard library（macOS 26 + 命令行工具实测）
            result = self.run(
                ["xcrun", "swiftc", "-O", "-o", str(temporary), str(source_file)],
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT, check=False,
            )
            if result.returncode == 0 and temporary.is_file():
                os.chmod(temporary, 0o755)
                os.replace(temporary, self.binary)
                _marker(self.binary).unlink(missing_ok=True)
                logger.info("Vision 程序编译好了，用了 %.0f 秒", time.monotonic() - started)
                return True
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            error = lines[-1] if lines else f"退出码 {result.returncode}"
        except subprocess.TimeoutExpired:
            error = "编译超过 5 分钟"
        except OSError as exc:
            error = str(exc)
        finally:
            temporary.unlink(missing_ok=True)
        logger.warning("Vision 程序编译失败：%s", error)
        _marker(self.binary).write_text(
            json.dumps(
                {"at": self.now().isoformat(), "swiftc_version": swiftc_version(swiftc, run=self.run),
                 "error": error[:300]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return False

    def wait(self, timeout: float) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
