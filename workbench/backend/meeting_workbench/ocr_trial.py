"""1f 图片文字识别试跑：从项目材料里挑几十张图，分别用 macOS 自带的 Vision 和 tesseract 识别，
比较用时和识别出来的字，给第三期选引擎用。只读材料，结果写到声档数据目录下的 ocr-trial/。

Vision 走一段很短的 Swift 程序（第一次用 swiftc 编译，需要 Xcode 命令行工具）；
tesseract 需要 brew install tesseract tesseract-lang（中文要 chi_sim 语言包）。
哪个没装就只跑另一个，并说明怎么装。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .material_walk import (
    IMAGE_EXTS,
    NAME_ONLY_DIRS,
    PACKAGE_EXTS,
    SMALL_IMAGE_BYTES,
    file_ext,
    human_bytes,
    is_system_shadow,
)

DEFAULT_LIMIT = 20
ENGINE_TIMEOUT = 60
TEXT_PREVIEW_CHARS = 800
VISION = "vision"
TESSERACT = "tesseract"
ENGINE_LABELS = {VISION: "Vision（macOS 自带）", TESSERACT: "tesseract"}

SWIFT_SOURCE = r"""
import Foundation
import ImageIO
import Vision

struct Output: Codable {
    let text: String
    let seconds: Double
    let error: String?
}

func recognize(_ path: String) -> Output {
    let start = Date()
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        return Output(text: "", seconds: Date().timeIntervalSince(start), error: "读不了这张图")
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return Output(text: "", seconds: Date().timeIntervalSince(start), error: "\(error)")
    }
    let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
    let lines = observations.compactMap { $0.topCandidates(1).first?.string }
    return Output(text: lines.joined(separator: "\n"), seconds: Date().timeIntervalSince(start), error: nil)
}

let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let output = recognize(path)
if let data = try? JSONEncoder().encode(output), let line = String(data: data, encoding: .utf8) {
    print(line)
}
"""

Engine = Callable[[Path], dict[str, Any]]


# —— 挑图 ——


def collect_images(roots: Iterable[Path]) -> list[tuple[Path, int]]:
    """材料文件夹里的图片（跳过系统影子文件、只收文件名的目录、包和小于 20 KB 的小图）。"""
    found: list[tuple[Path, int]] = []
    for root in roots:
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs
                if not is_system_shadow(name)
                and name not in NAME_ONLY_DIRS
                and not name.startswith(".")
                and file_ext(name) not in PACKAGE_EXTS
            )
            for name in sorted(files):
                if is_system_shadow(name) or file_ext(name) not in IMAGE_EXTS:
                    continue
                path = Path(current) / name
                try:
                    if path.is_symlink():
                        continue
                    size = path.stat().st_size
                except OSError:
                    continue
                if size >= SMALL_IMAGE_BYTES:
                    found.append((path, size))
    return found


def pick_images(images: list[tuple[Path, int]], roots: list[Path], limit: int = DEFAULT_LIMIT) -> list[Path]:
    """尽量分散：按所在的一级文件夹分组轮流挑，同组里按路径均匀取。"""
    groups: OrderedDict[str, list[Path]] = OrderedDict()
    for path, _size in sorted(images, key=lambda item: str(item[0])):
        root = next((base for base in roots if path.is_relative_to(base)), None)
        relative = path.relative_to(root) if root else path
        key = f"{root}::{relative.parts[0] if len(relative.parts) > 1 else ''}"
        groups.setdefault(key, []).append(path)
    buckets = list(groups.values())
    quotas = [0] * len(buckets)
    remaining = min(limit, sum(len(bucket) for bucket in buckets))
    while remaining:
        for index, bucket in enumerate(buckets):
            if remaining and quotas[index] < len(bucket):
                quotas[index] += 1
                remaining -= 1
    picked: list[Path] = []
    for bucket, quota in zip(buckets, quotas, strict=True):
        if not quota:
            continue
        step = len(bucket) / quota
        picked.extend(bucket[int(index * step)] for index in range(quota))
    return picked


# —— 引擎 ——


def vision_engine(workdir: Path, *, system: str | None = None) -> tuple[Engine | None, str | None]:
    """返回 (引擎, 不能用的原因)。"""
    if (system or platform.system()) != "Darwin":
        return None, "Vision 只能在 Mac 上跑"
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None, "没找到 swiftc：先在终端运行 xcode-select --install 装 Xcode 命令行工具"
    source = workdir / "vision_ocr.swift"
    binary = workdir / "vision-ocr"
    source.write_text(SWIFT_SOURCE, encoding="utf-8")
    try:
        result = subprocess.run(
            [swiftc, "-O", "-o", str(binary), str(source)],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "编译 Vision 小程序超时"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        return None, f"编译 Vision 小程序失败：{detail[0]}"

    def run(path: Path) -> dict[str, Any]:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [str(binary), str(path)], capture_output=True, text=True,
                timeout=ENGINE_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"text": "", "seconds": ENGINE_TIMEOUT, "error": "处理超时"}
        try:
            output = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {
                "text": "",
                "seconds": round(time.monotonic() - started, 2),
                "error": (completed.stderr.strip() or "没有输出")[:200],
            }
        return {
            "text": output.get("text") or "",
            "seconds": round(float(output.get("seconds") or 0), 2),
            "error": output.get("error"),
        }

    return run, None


def tesseract_engine(workdir: Path) -> tuple[Engine | None, str | None, str | None]:
    """返回 (引擎, 不能用的原因, 提醒)。"""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return None, "没找到 tesseract：brew install tesseract tesseract-lang", None
    try:
        listed = subprocess.run(
            [tesseract, "--list-langs"], capture_output=True, text=True, timeout=30, check=False
        )
        languages = set(listed.stdout.split())
    except (OSError, subprocess.SubprocessError):
        languages = set()
    note = None
    if "chi_sim" in languages:
        language = "chi_sim+eng" if "eng" in languages else "chi_sim"
    else:
        language = "eng"
        note = "tesseract 没装中文语言包，只能认英文：brew install tesseract-lang"
    sips = shutil.which("sips")

    def run(path: Path) -> dict[str, Any]:
        source = path
        if file_ext(path.name) in {"heic", "heif"}:
            if not sips:
                return {"text": "", "seconds": 0.0, "error": "tesseract 读不了 HEIC"}
            source = workdir / f"heic-{abs(hash(str(path)))}.png"
            try:
                subprocess.run(
                    [sips, "-s", "format", "png", str(path), "--out", str(source)],
                    capture_output=True, timeout=ENGINE_TIMEOUT, check=True,
                )
            except (subprocess.SubprocessError, OSError):
                return {"text": "", "seconds": 0.0, "error": "HEIC 转 PNG 失败"}
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [tesseract, str(source), "stdout", "-l", language],
                capture_output=True, text=True, timeout=ENGINE_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"text": "", "seconds": ENGINE_TIMEOUT, "error": "处理超时"}
        finally:
            if source != path:
                source.unlink(missing_ok=True)
        seconds = round(time.monotonic() - started, 2)
        if completed.returncode != 0:
            return {"text": "", "seconds": seconds, "error": (completed.stderr.strip() or "识别失败")[:200]}
        return {"text": completed.stdout.strip(), "seconds": seconds, "error": None}

    return run, None, note


# —— 试跑 ——


def _chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def run_trial(images: list[Path], engines: dict[str, Engine], *, progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, path in enumerate(images, start=1):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        row: dict[str, Any] = {"path": str(path), "bytes": size, "engines": {}}
        for name, engine in engines.items():
            outcome = engine(path)
            outcome["chars"] = _chars(outcome.get("text") or "")
            row["engines"][name] = outcome
        results.append(row)
        if progress:
            progress(index, len(images))
    summary: dict[str, Any] = {}
    for name in engines:
        outcomes = [row["engines"][name] for row in results]
        succeeded = [outcome for outcome in outcomes if not outcome.get("error")]
        summary[name] = {
            "images": len(outcomes),
            "succeeded": len(succeeded),
            "avg_seconds": round(sum(item["seconds"] for item in succeeded) / len(succeeded), 2) if succeeded else None,
            "chars": sum(item["chars"] for item in succeeded),
            "empty": sum(1 for item in succeeded if item["chars"] == 0),
        }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "images": results,
        "summary": summary,
    }


def render_markdown(report: dict[str, Any], notes: list[str]) -> str:
    lines = ["# 图片文字识别试跑", ""]
    lines.append(f"{len(report['images'])} 张图，{report['generated_at'][:19].replace('T', ' ')}（UTC）。")
    for note in notes:
        lines.append(f"- {note}")
    lines += ["", "| 引擎 | 认出来的 | 平均每张 | 总字数 | 一个字也没认出 |", "| --- | --- | --- | --- | --- |"]
    for name, item in report["summary"].items():
        average = f"{item['avg_seconds']} 秒" if item["avg_seconds"] is not None else "—"
        lines.append(
            f"| {ENGINE_LABELS.get(name, name)} | {item['succeeded']}/{item['images']} | {average} | "
            f"{item['chars']} | {item['empty']} |"
        )
    for index, row in enumerate(report["images"], start=1):
        lines += ["", f"## {index}. {row['path']}（{human_bytes(row['bytes'])}）"]
        for name, outcome in row["engines"].items():
            label = ENGINE_LABELS.get(name, name)
            if outcome.get("error"):
                lines.append(f"**{label}**：没认成，{outcome['error']}")
                continue
            lines.append(f"**{label}**：{outcome['seconds']} 秒，{outcome['chars']} 个字")
            text = outcome.get("text") or ""
            if text:
                preview = text if len(text) <= TEXT_PREVIEW_CHARS else text[:TEXT_PREVIEW_CHARS] + "…"
                lines += ["", "```text", preview.replace("```", "ʼʼʼ"), "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def render_summary(report: dict[str, Any], out_dir: Path) -> str:
    lines = [f"试跑了 {len(report['images'])} 张图："]
    for name, item in report["summary"].items():
        average = f"平均每张 {item['avg_seconds']} 秒" if item["avg_seconds"] is not None else "没有认成的"
        lines.append(
            f"  {ENGINE_LABELS.get(name, name)}：认出 {item['succeeded']}/{item['images']} 张，{average}，"
            f"共 {item['chars']} 个字"
        )
    lines.append(f"对照结果：{out_dir / '结果.md'}")
    return "\n".join(lines)


def default_out_dir(data_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return data_dir / "ocr-trial" / stamp


def prepare_engines(
    wanted: list[str], workdir: Path, *, system: str | None = None
) -> tuple[dict[str, Engine], list[str]]:
    engines: dict[str, Engine] = {}
    notes: list[str] = []
    if VISION in wanted:
        engine, reason = vision_engine(workdir, system=system)
        if engine:
            engines[VISION] = engine
        else:
            notes.append(f"Vision 没跑：{reason}")
    if TESSERACT in wanted:
        engine, reason, note = tesseract_engine(workdir)
        if engine:
            engines[TESSERACT] = engine
        else:
            notes.append(f"tesseract 没跑：{reason}")
        if note:
            notes.append(note)
    return engines, notes


def scratch_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="meeting-workbench-ocr-"))
