from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


TIMECODE_RE = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})")
# 箭头左边只有数字和时间码标点才算时间行；正文里写的「A --> B」不能被当成新的一条。
TIMING_LINE_RE = re.compile(r"^\s*[\d:,.]*\s*-->")
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 128
# 嵌套深度校验是纯 Python 逐字符扫描，一份 20MB 的转写 JSON 要 0.3～0.5 秒，而同一份文件
# 导入时会被加载好几次，后台扫描每轮还要把全部 JSON 再过一遍（生产上约 2100 份、校验约 1750 次，
# 占一轮 7.5 秒 CPU 里的 5.3 秒）。校验通过的文件按路径记下 (大小, mtime, ctime, inode)，
# 没变就不再扫；口径和导入器的 sha256 进程内记录一致（见 importer._file_sha256）：
# ctime 和 inode 能认出大小不变、mtime 被拨回原值的改写；刚写完不到 MEMO_SETTLE_NS 的文件不记，
# 时间戳精度粗的盘上同一个刻度里再写一次改不动 mtime。
# 只记通过的，最多记 _DEPTH_CHECKED_LIMIT 份，挤掉最久没用的。上限要大于一轮扫描碰到的 JSON 数：
# 原来是 512，一轮轮流碰两千多份，最久没用的总是下一个要用的，等于一次都没命中。
MEMO_SETTLE_NS = 3_000_000_000
_DEPTH_CHECKED_LIMIT = 16384
_depth_checked: OrderedDict[str, tuple[int, int, int, int]] = OrderedDict()
_depth_checked_lock = threading.Lock()
# 时间戳上限取 SRT 时间码能写出来的最大值 99:59:59,999：逐字稿要能按 SRT
# 导出、再导回来（rendering 写两位小时，上面的 TIMECODE_RE 也只认两位），
# 超过它的值不可能来自一场真实录音，只会是写坏或被改过的文件。
MAX_TIMESTAMP_MS = 100 * 3600 * 1000 - 1
SPEAKER_RE = re.compile(
    r"^\s*(?:\[?(?P<label>(?:SPEAKER|SPK|说话人)[ _-]?\d+)\]?\s*[:：-]?\s*)(?P<text>.*)$",
    re.IGNORECASE,
)


def _timecode_to_ms(value: str) -> int:
    match = TIMECODE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid SRT timecode: {value}")
    parts = {key: int(number) for key, number in match.groupdict().items()}
    return ((parts["h"] * 60 + parts["m"]) * 60 + parts["s"]) * 1000 + parts["ms"]


def _checked_ms(value: Any, scale: int = 1) -> float:
    """转写 JSON 里的时间换成毫秒：无穷大、NaN、越界的一律按坏文件处理。"""
    try:
        number = float(value) * scale
    except OverflowError as error:
        raise ValueError(f"invalid timestamp: {value!r}") from error
    if not math.isfinite(number) or abs(number) > MAX_TIMESTAMP_MS:
        raise ValueError(f"invalid timestamp: {value!r}")
    return number


def _speaker_and_text(text: str) -> tuple[str | None, str]:
    single_line = " ".join(line.strip() for line in text.splitlines() if line.strip())
    match = SPEAKER_RE.match(single_line)
    if not match:
        return None, single_line
    raw = match.group("label").upper().replace("说话人", "SPEAKER_").replace("SPK", "SPEAKER")
    raw = re.sub(r"[ -]+", "_", raw)
    if raw.startswith("SPEAKER") and not raw.startswith("SPEAKER_"):
        raw = raw.replace("SPEAKER", "SPEAKER_", 1)
    return raw, match.group("text").strip()


def _read_transcript_text(path: Path) -> str:
    """读逐字稿文本：UTF-8 优先，整份读不通再试 GB18030，都不行就按坏文件报错。

    只坏了零星几个字节的 UTF-8（比如写到一半被截断）仍按 UTF-8 读、坏处换成 �，
    不能因为一个字节就整份改用 GB18030 读成乱码。
    """
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("utf-8-sig", errors="replace")
        if content.count("\ufffd") > max(3, len(content) // 100):
            try:
                content = raw.decode("gb18030")
            except UnicodeDecodeError as error:
                raise ValueError(f"transcript is neither UTF-8 nor GB18030: {path.name}") from error
    return content.replace("\r\n", "\n").replace("\r", "\n")


def parse_srt(path: Path) -> list[dict[str, Any]]:
    lines = _read_transcript_text(path).split("\n")
    timing_rows = [index for index, line in enumerate(lines) if TIMING_LINE_RE.match(line)]
    segments: list[dict[str, Any]] = []
    # 按时间行切，不按空行切：两条字幕之间少一个空行时，下一条的序号和时间码
    # 不能被并进上一条的正文。时间行紧挨着的上一行是纯数字，就是它的序号。
    for position, row in enumerate(timing_rows):
        stop = timing_rows[position + 1] if position + 1 < len(timing_rows) else len(lines)
        if stop < len(lines) and stop - 1 > row and lines[stop - 1].strip().isdigit():
            stop -= 1
        start, end = (part.strip() for part in lines[row].split("-->", 1))
        end_parts = end.split()
        if not end_parts:
            raise ValueError(f"invalid SRT timing line: {lines[row].strip()}")
        label, text = _speaker_and_text("\n".join(lines[row + 1 : stop]))
        if not text:
            continue
        segments.append(
            {
                "ordinal": len(segments),
                "start_ms": _timecode_to_ms(start),
                "end_ms": _timecode_to_ms(end_parts[0]),
                "speaker_label": label,
                "speaker_name": None,
                "text": text,
            }
        )
    return segments


def _validate_json_depth(content: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in content:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError(f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")
        elif character in "]}":
            depth = max(0, depth - 1)


def _depth_already_checked(key: str, signature: tuple[int, int, int, int]) -> bool:
    with _depth_checked_lock:
        if _depth_checked.get(key) != signature:
            return False
        _depth_checked.move_to_end(key)
        return True


def _remember_depth_checked(key: str, signature: tuple[int, int, int, int]) -> None:
    with _depth_checked_lock:
        _depth_checked[key] = signature
        _depth_checked.move_to_end(key)
        while len(_depth_checked) > _DEPTH_CHECKED_LIMIT:
            _depth_checked.popitem(last=False)


def load_json_file(path: Path) -> Any:
    with path.open("rb") as handle:
        # 大小和修改时间取自打开的这个文件本身，和下面读到的内容是同一份
        status = os.fstat(handle.fileno())
        if status.st_size > MAX_JSON_BYTES:
            raise ValueError("JSON file exceeds 64 MiB")
        content_bytes = handle.read(MAX_JSON_BYTES + 1)
    if len(content_bytes) > MAX_JSON_BYTES:
        raise ValueError("JSON file exceeds 64 MiB")
    content = content_bytes.decode("utf-8-sig", errors="replace")
    key = os.path.abspath(path)
    signature = (status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino)
    if not _depth_already_checked(key, signature):
        _validate_json_depth(content)
        if time.time_ns() - max(status.st_mtime_ns, status.st_ctime_ns) >= MEMO_SETTLE_NS:
            _remember_depth_checked(key, signature)
    return json.loads(content)


def _load_json(path: Path) -> Any:
    return load_json_file(path)


def _find_segment_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            for item in payload:
                found = _find_segment_list(item, keys)
                if found:
                    return found
        return []
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise ValueError(f"{key} segment list contains a non-object item")
            return value
    for key in ("result", "data", "output"):
        if key in payload:
            found = _find_segment_list(payload[key], keys)
            if found:
                return found
    return []


def parse_whisper_json(path: Path) -> list[dict[str, Any]]:
    raw_segments = _find_segment_list(_load_json(path), ("segments",))
    segments = []
    for item in raw_segments:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        segments.append(
            {
                "ordinal": len(segments),
                "start_ms": round(_checked_ms(item.get("start", 0), 1000)),
                "end_ms": round(_checked_ms(item.get("end", item.get("start", 0)), 1000)),
                "speaker_label": None,
                "speaker_name": None,
                "text": text,
            }
        )
    return segments


def parse_funasr_json(path: Path) -> list[dict[str, Any]]:
    # 切块长会（.funasr.json 顶层是 [{chunk, offset_sec, result:{sentence_info}}, ...]）里
    # spk 是每块从 0 重编号的裸整数，不带块信息直转 SPEAKER_NN 会把不同块里的同号说话人
    # 静默合并成一个人。chunk_index 只在顶层列表长度 >1 时赋值，随递归原样下传，
    # 单块（含无 chunk 包装的旧格式）保持 chunk_index=None，label 不加前缀。
    def collect(
        payload: Any, inherited_offset_ms: int = 0, chunk_index: int | None = None
    ) -> list[tuple[dict[str, Any], int, int | None]]:
        if isinstance(payload, list):
            collected: list[tuple[dict[str, Any], int, int | None]] = []
            for item in payload:
                collected.extend(collect(item, inherited_offset_ms, chunk_index))
            return collected
        if not isinstance(payload, dict):
            return []
        offset_ms = inherited_offset_ms + round(
            _checked_ms(payload.get("offset_sec", 0) or 0, 1000)
        )
        for key in ("sentence_info", "sentences", "stamp_sents", "segments"):
            value = payload.get(key)
            if isinstance(value, list):
                if not all(isinstance(item, dict) for item in value):
                    raise ValueError(f"{key} segment list contains a non-object item")
                return [(item, offset_ms, chunk_index) for item in value]
        collected = []
        for key in ("result", "data", "output"):
            if key in payload:
                collected.extend(collect(payload[key], offset_ms, chunk_index))
        return collected

    top_level = _load_json(path)
    if isinstance(top_level, list) and len(top_level) > 1:
        raw_segments: list[tuple[dict[str, Any], int, int | None]] = []
        for index, block in enumerate(top_level):
            chunk_number = block.get("chunk") if isinstance(block, dict) else None
            if not isinstance(chunk_number, int):
                chunk_number = index
            raw_segments.extend(collect(block, 0, chunk_number + 1))
    else:
        raw_segments = collect(top_level)

    segments = []
    for item, offset_ms, chunk_index in raw_segments:
        text = str(item.get("text") or item.get("sentence") or "").strip()
        if not text:
            continue
        speaker = item.get("spk", item.get("speaker"))
        label = None
        if speaker is not None and str(speaker).strip() != "":
            value = str(speaker)
            if value.isdigit():
                label = f"SPEAKER_{int(value):02d}"
            else:
                label, _ = _speaker_and_text(f"{value} x")
            if label is not None and chunk_index is not None:
                label = f"C{chunk_index}_{label}"
        start = item.get("start", item.get("start_time", item.get("begin", 0)))
        end = item.get("end", item.get("end_time", item.get("finish", start)))
        start_ms = int(_checked_ms(offset_ms + int(_checked_ms(start or 0))))
        end_ms = int(_checked_ms(offset_ms + int(_checked_ms(end or start or 0))))
        segments.append(
            {
                "ordinal": len(segments),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_label": label,
                "speaker_name": None,
                "text": text,
            }
        )
    return segments


def parse_txt(path: Path) -> list[dict[str, Any]]:
    text = _read_transcript_text(path).strip()
    if not text:
        return []
    return [
        {
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": 0,
            "speaker_label": None,
            "speaker_name": None,
            "text": text,
        }
    ]
