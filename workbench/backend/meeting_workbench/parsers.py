from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


TIMECODE_RE = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})")
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 128
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


def parse_srt(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    segments: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start, end = (part.strip() for part in lines[timing_index].split("-->", 1))
        label, text = _speaker_and_text("\n".join(lines[timing_index + 1 :]))
        if not text:
            continue
        segments.append(
            {
                "ordinal": len(segments),
                "start_ms": _timecode_to_ms(start),
                "end_ms": _timecode_to_ms(end.split()[0]),
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


def load_json_file(path: Path) -> Any:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON file exceeds 64 MiB")
    with path.open("rb") as handle:
        content_bytes = handle.read(MAX_JSON_BYTES + 1)
    if len(content_bytes) > MAX_JSON_BYTES:
        raise ValueError("JSON file exceeds 64 MiB")
    content = content_bytes.decode("utf-8-sig", errors="replace")
    _validate_json_depth(content)
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
                "start_ms": round(float(item.get("start", 0)) * 1000),
                "end_ms": round(float(item.get("end", item.get("start", 0))) * 1000),
                "speaker_label": None,
                "speaker_name": None,
                "text": text,
            }
        )
    return segments


def parse_funasr_json(path: Path) -> list[dict[str, Any]]:
    def collect(payload: Any, inherited_offset_ms: int = 0) -> list[tuple[dict[str, Any], int]]:
        if isinstance(payload, list):
            collected: list[tuple[dict[str, Any], int]] = []
            for item in payload:
                collected.extend(collect(item, inherited_offset_ms))
            return collected
        if not isinstance(payload, dict):
            return []
        offset_ms = inherited_offset_ms + round(float(payload.get("offset_sec", 0) or 0) * 1000)
        for key in ("sentence_info", "sentences", "stamp_sents", "segments"):
            value = payload.get(key)
            if isinstance(value, list):
                if not all(isinstance(item, dict) for item in value):
                    raise ValueError(f"{key} segment list contains a non-object item")
                return [(item, offset_ms) for item in value]
        collected = []
        for key in ("result", "data", "output"):
            if key in payload:
                collected.extend(collect(payload[key], offset_ms))
        return collected

    raw_segments = collect(_load_json(path))
    segments = []
    for item, offset_ms in raw_segments:
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
        start = item.get("start", item.get("start_time", item.get("begin", 0)))
        end = item.get("end", item.get("end_time", item.get("finish", start)))
        segments.append(
            {
                "ordinal": len(segments),
                "start_ms": offset_ms + int(float(start or 0)),
                "end_ms": offset_ms + int(float(end or start or 0)),
                "speaker_label": label,
                "speaker_name": None,
                "text": text,
            }
        )
    return segments


def parse_txt(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
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
