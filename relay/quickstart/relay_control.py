#!/usr/bin/env python3
"""meeting-relay 的小型任务控制层。

该模块只负责可靠台账、状态转换和归档回执校验；它不启动或强杀转写进程。
watchdog/工作台可以导入 ``RelayControl``，命令行则直接运行本文件或 relayctl。
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB_PATH = Path.home() / ".meeting-relay" / "workbench-jobs.sqlite3"
DEFAULT_ARCHIVE_ROOT = Path.home() / "MeetingArchive"
DEFAULT_ARCHIVE_LOCK_PATH = Path.home() / ".meeting-workbench" / "archive.lock"
DEFAULT_PRODUCTS_ROOT = Path.home() / "Movies" / "meeting-relay-products"
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav"}
MAX_INPUT_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_MINUTES_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_HOTWORD_PROMPT_BYTES = 64 * 1024
MAX_HOTWORD_TERMS = 20
CURRENT_MINUTES_PROTOCOL_VERSION = 3
_MINUTES_STRATEGIES = {"single_pass", "topic_hierarchical", "multi_stage"}
_MINUTES_ITEM_KINDS = {
    "fact",
    "decision",
    "action",
    "risk",
    "open_question",
    "number",
}
_MINUTES_ANCHOR_RE = re.compile(r"\[(\d{2}):(\d{2}):(\d{2})\]")
_SRT_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$"
)
_HOTWORD_TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿]{2,}")

PIPELINE_STATES = (
    "discovered",
    "stabilizing",
    "queued",
    "transcribing",
    "transcript_ready",
    "minutes_generating",
    "completed_unreviewed",
    "draft_modified",
    "published",
)
EXCEPTION_STATES = ("failed", "cancelled", "interrupted")
ALL_STATES = set(PIPELINE_STATES + EXCEPTION_STATES)
RETRYABLE_STAGES = {
    "stabilizing",
    "transcribing",
    "transcript_ready",
    "minutes_generating",
}
RETRYABLE_JOB_STATES = {
    "failed",
    "cancelled",
    "interrupted",
    "completed_unreviewed",
    "draft_modified",
    "published",
}
SUBSTATE_NAMES = {"whisper", "index"}
SUBSTATE_STATUSES = {"pending", "running", "ready", "failed", "absent"}
SUBSTATE_COLUMNS = {
    "whisper": (
        "whisper_status",
        "whisper_error",
        "whisper_updated_at",
        "whisper_attempt",
    ),
    "index": ("index_status", "index_error", "index_updated_at", "index_attempt"),
}

_ALLOWED_TRANSITIONS = {
    "discovered": {"stabilizing"},
    "stabilizing": {"queued"},
    "queued": {"transcribing"},
    "transcribing": {"transcript_ready"},
    "transcript_ready": {"minutes_generating"},
    "minutes_generating": {"completed_unreviewed"},
    "completed_unreviewed": {"draft_modified", "published"},
    "draft_modified": {"completed_unreviewed", "published"},
    "published": set(),
    "failed": set(),
    "cancelled": set(),
    "interrupted": set(),
}

_VM_ID_RE = re.compile(r"(vm-\d{8}-\d{6}-[A-Za-z0-9]+)", re.IGNORECASE)
_PIPELINE_STEM_SUFFIXES = (".16k", "-16k", "_16k")


def _canonical_media_stem(value: str) -> str:
    """只折叠已知转码后缀；不做模糊包含，避免不同录音被误认为同源。"""
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    changed = True
    while changed:
        changed = False
        for suffix in _PIPELINE_STEM_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
                break
    return normalized


def _is_archive_noise(path: Path, root: Path) -> bool:
    """Finder/AppleDouble 元数据不属于业务产物，但仍参与安全类型检查。"""
    return any(
        part.startswith("._") or part == ".DS_Store"
        for part in path.relative_to(root).parts
    )


class RelayControlError(RuntimeError):
    """控制层可预期错误。"""


class JobNotFoundError(RelayControlError):
    pass


class InvalidTransitionError(RelayControlError):
    pass


class ArchiveValidationReport:
    def __init__(
        self,
        archive_dir: Path,
        missing: Iterable[str],
        artifacts: Iterable[str],
    ):
        self.archive_dir = archive_dir
        self.missing = tuple(dict.fromkeys(missing))
        self.artifacts = tuple(sorted(set(artifacts)))

    @property
    def valid(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "archive_dir": str(self.archive_dir),
            "missing": list(self.missing),
            "artifacts": list(self.artifacts),
        }


class ArtifactValidationError(RelayControlError):
    def __init__(self, report: ArchiveValidationReport):
        self.report = report
        super().__init__("归档产物校验失败，缺失或无效: " + ", ".join(report.missing))


class PublishValidationError(RelayControlError):
    """发布回执没有通过文件归属或内容完整性校验。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_loads(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def _seconds_from_srt_parts(parts: tuple[str, ...]) -> float | None:
    values = tuple(int(part) for part in parts)
    hours, minutes, seconds, milliseconds = values
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _parse_srt_cues(srt_path: Path) -> list[dict[str, Any]]:
    if srt_path.is_symlink() or not srt_path.is_file() or srt_path.stat().st_size <= 0:
        raise RelayControlError("minutes_plan_srt")
    try:
        text = srt_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RelayControlError("minutes_plan_srt_utf8") from exc
    cues: list[dict[str, Any]] = []
    previous_end = 0.0
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for sequence, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise RelayControlError("minutes_plan_srt_structure")
        timing = _SRT_TIME_RE.fullmatch(lines[1].strip())
        if timing is None:
            raise RelayControlError("minutes_plan_srt_time")
        start = _seconds_from_srt_parts(tuple(timing.groups()[:4]))
        end = _seconds_from_srt_parts(tuple(timing.groups()[4:]))
        cue_text = "\n".join(lines[2:]).strip()
        if start is None or end is None or end <= start or not cue_text:
            raise RelayControlError("minutes_plan_srt_cue")
        # FunASR/whisper 的说话人分句会输出重叠时间戳（多人同时说话、分句
        # 边界取整），不是损坏文件。确定性钳制归一：起点钳到前一条终点；
        # 完全被覆盖的句子保留 10ms 最小时长，文本哈希照常进计划。建计划
        # 与 complete-minutes 复核共用本解析器，钳制结果两侧一致。
        if start < previous_end:
            start = previous_end
            if end <= start:
                end = start + 0.01
        cues.append(
            {
                "cue_index": sequence,
                "source_start_sec": start,
                "source_end_sec": end,
                "source_text_sha256": hashlib.sha256(
                    cue_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        previous_end = end
    if not cues:
        raise RelayControlError("minutes_plan_srt_empty")
    return cues


def _minimum_minutes_window_duration(
    total_duration_sec: float, window_count: int
) -> float:
    """通常至少 8 分钟；12–16 分钟的不可整除区间允许均衡到至少 6 分钟。"""
    if total_duration_sec >= 480 * window_count:
        return 480.0
    return 360.0


def create_minutes_plan(
    srt_path: str | Path,
    output_path: str | Path,
    *,
    total_duration_sec: float,
    input_transcript_sha256: str | None = None,
) -> dict[str, Any]:
    """从 SRT 生成确定性窗口计划；正文不进入计划，只保留来源哈希。"""
    source = Path(srt_path).expanduser()
    target = Path(output_path).expanduser()
    if (
        not isinstance(total_duration_sec, (int, float))
        or isinstance(total_duration_sec, bool)
        or total_duration_sec <= 0
    ):
        raise RelayControlError("minutes_plan_duration")
    cues = _parse_srt_cues(source)
    duration = max(float(total_duration_sec), float(cues[-1]["source_end_sec"]))
    if any(
        cue["source_end_sec"] - cue["source_start_sec"] > 720
        for cue in cues
    ):
        raise RelayControlError("minutes_plan_cue_too_long")

    if duration < 480:
        window_count = 1
    else:
        minimum = max(1, int((duration + 719) // 720))
        maximum = max(minimum, int(duration // 480))
        window_count = min(maximum, max(minimum, int(duration / 600 + 0.5)))
    window_count = min(window_count, len(cues))
    if duration >= 480 and duration / window_count > 720:
        raise RelayControlError("minutes_plan_window_duration")
    minimum_window_duration = _minimum_minutes_window_duration(
        duration, window_count
    )

    groups: list[list[dict[str, Any]]] = []
    cursor = 0
    for number in range(1, window_count + 1):
        remaining_windows = window_count - number
        if remaining_windows == 0:
            end_index = len(cues)
        else:
            target_time = duration * number / window_count
            end_index = cursor + 1
            latest = len(cues) - remaining_windows
            while (
                end_index < latest
                and cues[end_index]["source_end_sec"] <= target_time
            ):
                end_index += 1
            if end_index < latest:
                before = abs(cues[end_index - 1]["source_end_sec"] - target_time)
                after = abs(cues[end_index]["source_end_sec"] - target_time)
                if after < before:
                    end_index += 1
        groups.append(cues[cursor:end_index])
        cursor = end_index

    boundaries = [0.0]
    for index in range(len(groups) - 1):
        left_end = float(groups[index][-1]["source_end_sec"])
        right_start = float(groups[index + 1][0]["source_start_sec"])
        boundaries.append((left_end + right_start) / 2)
    boundaries.append(duration)
    windows: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        start = boundaries[index]
        end = boundaries[index + 1]
        if (
            duration >= 480
            and not minimum_window_duration <= end - start <= 720
        ):
            raise RelayControlError("minutes_plan_window_duration")
        windows.append(
            {
                "window_id": f"W{index + 1:03d}",
                "start_sec": start,
                "end_sec": end,
                "cues": group,
            }
        )

    plan = {
        "schema_version": 1,
        "minutes_protocol_version": CURRENT_MINUTES_PROTOCOL_VERSION,
        "source_srt_sha256": _sha256_file(source),
        "input_transcript_sha256": input_transcript_sha256,
        "total_duration_sec": duration,
        "cue_count": len(cues),
        "windows": windows,
    }
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise RelayControlError("minutes_plan_target")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RelayControlError("minutes_plan_target")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".minutes-plan-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(plan, destination, ensure_ascii=False, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return plan


def _minutes_plan_context(
    plan_path: Path | None,
    source_srt_path: Path | None = None,
    expected_input_transcript_sha256: str | None = None,
    expected_source_srt_sha256: str | None = None,
    expected_minutes_plan_sha256: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if (
        plan_path is None
        or plan_path.is_symlink()
        or not plan_path.is_file()
        or plan_path.stat().st_size <= 0
    ):
        return None, ["minutes_plan"]
    try:
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ["minutes_plan_json"]
    if not isinstance(plan, dict):
        return None, ["minutes_plan_schema"]
    windows = plan.get("windows")
    if (
        plan.get("schema_version") != 1
        or plan.get("minutes_protocol_version") != 3
        or not isinstance(plan.get("source_srt_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", plan["source_srt_sha256"]) is None
        or not isinstance(plan.get("cue_count"), int)
        or not isinstance(windows, list)
        or not windows
    ):
        return None, ["minutes_plan_schema"]
    errors: list[str] = []
    if (
        expected_minutes_plan_sha256 is not None
        and hashlib.sha256(plan_bytes).hexdigest() != expected_minutes_plan_sha256
    ):
        errors.append("minutes_plan_hash_mismatch")
    total_duration = plan.get("total_duration_sec")
    if (
        not isinstance(total_duration, (int, float))
        or isinstance(total_duration, bool)
        or total_duration <= 0
    ):
        errors.append("minutes_plan_window")
        total_duration = 0.0
    total_duration = float(total_duration)
    minimum_window_duration = (
        _minimum_minutes_window_duration(total_duration, len(windows))
        if windows and total_duration >= 480
        else 0.0
    )
    previous_end = 0.0
    cue_indices: list[int] = []
    window_ids: set[str] = set()
    for window_index, window in enumerate(windows):
        if not isinstance(window, dict):
            errors.append("minutes_plan_window")
            continue
        window_id = window.get("window_id")
        start = window.get("start_sec")
        end = window.get("end_sec")
        cues = window.get("cues")
        if (
            not isinstance(window_id, str)
            or not window_id
            or window_id in window_ids
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or abs(float(start) - previous_end) > 0.001
            or end <= start
            or (
                total_duration >= 480
                and not minimum_window_duration
                <= float(end) - float(start)
                <= 720
            )
            or (
                total_duration < 480
                and (
                    len(windows) != 1
                    or window_index != 0
                    or abs(float(start)) > 0.001
                    or abs(float(end) - total_duration) > 0.001
                )
            )
            or not isinstance(cues, list)
            or not cues
        ):
            errors.append("minutes_plan_window")
            continue
        window_ids.add(window_id)
        previous_end = float(end)
        cue_previous_end = float(start)
        for cue in cues:
            if not isinstance(cue, dict):
                errors.append("minutes_plan_cue")
                continue
            cue_index = cue.get("cue_index")
            cue_start = cue.get("source_start_sec")
            cue_end = cue.get("source_end_sec")
            digest = cue.get("source_text_sha256")
            if (
                not isinstance(cue_index, int)
                or not isinstance(cue_start, (int, float))
                or isinstance(cue_start, bool)
                or not isinstance(cue_end, (int, float))
                or isinstance(cue_end, bool)
                or cue_start < start
                or cue_start < cue_previous_end
                or cue_end <= cue_start
                or cue_end > end
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                errors.append("minutes_plan_cue")
                continue
            cue_indices.append(cue_index)
            cue_previous_end = float(cue_end)
    if windows and abs(previous_end - total_duration) > 0.001:
        errors.append("minutes_plan_window")
    if cue_indices != list(range(1, int(plan["cue_count"]) + 1)):
        errors.append("minutes_plan_coverage")
    if (
        expected_input_transcript_sha256 is not None
        and plan.get("input_transcript_sha256")
        != expected_input_transcript_sha256
    ):
        errors.append("minutes_plan_input_hash")
    if expected_source_srt_sha256 is not None and (
        plan.get("source_srt_sha256") != expected_source_srt_sha256
    ):
        errors.append("minutes_plan_source_mismatch")
    if source_srt_path is not None:
        try:
            actual_cues = _parse_srt_cues(source_srt_path)
            actual_hash = _sha256_file(source_srt_path)
        except RelayControlError:
            errors.append("minutes_plan_source_mismatch")
        else:
            planned_cues = [
                cue
                for window in windows
                if isinstance(window, dict)
                for cue in window.get("cues", [])
                if isinstance(cue, dict)
            ]
            if actual_hash != plan["source_srt_sha256"] or actual_cues != planned_cues:
                errors.append("minutes_plan_source_mismatch")
    return plan, list(dict.fromkeys(errors))


def _anchor_seconds(anchor: str) -> int | None:
    match = _MINUTES_ANCHOR_RE.fullmatch(anchor)
    if match is None:
        return None
    hours, minutes, seconds = (int(value) for value in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _minutes_evidence_errors(
    evidence_path: Path,
    minutes_path: Path,
    *,
    protocol_version: int = 2,
    plan_path: Path | None = None,
    ledger_root: Path | None = None,
    source_srt_path: Path | None = None,
    expected_input_transcript_sha256: str | None = None,
    expected_source_srt_sha256: str | None = None,
    expected_minutes_plan_sha256: str | None = None,
) -> list[str]:
    """校验纪要信息账本；只返回结构化错误码，不把会议正文写入日志。"""
    if (
        evidence_path.is_symlink()
        or not evidence_path.is_file()
        or evidence_path.stat().st_size <= 0
    ):
        return ["minutes_evidence"]
    if evidence_path.stat().st_size > MAX_MINUTES_EVIDENCE_BYTES:
        return ["minutes_evidence_size"]
    try:
        loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
        minutes_text = minutes_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["minutes_evidence_json"]
    if not isinstance(loaded, dict):
        return ["minutes_evidence_json"]

    errors: list[str] = []
    if (
        type(loaded.get("schema_version")) is not int
        or loaded.get("schema_version") != 1
        or type(loaded.get("minutes_protocol_version")) is not int
        or loaded.get("minutes_protocol_version") != protocol_version
        or loaded.get("strategy") not in _MINUTES_STRATEGIES
    ):
        errors.append("minutes_evidence_schema")

    topics = loaded.get("topics")
    if not isinstance(topics, list) or not topics:
        errors.append("minutes_evidence_topics")
        topics = []

    plan: dict[str, Any] | None = None
    plan_errors: list[str] = []
    cue_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    if protocol_version >= 3:
        plan, plan_errors = _minutes_plan_context(
            plan_path,
            source_srt_path=source_srt_path,
            expected_input_transcript_sha256=expected_input_transcript_sha256,
            expected_source_srt_sha256=expected_source_srt_sha256,
            expected_minutes_plan_sha256=expected_minutes_plan_sha256,
        )
        errors.extend(plan_errors)
        if plan_errors:
            plan = None
        if plan is not None:
            for window in plan["windows"]:
                for cue in window["cues"]:
                    cue_lookup[(window["window_id"], cue["source_text_sha256"])] = cue

    topic_ids: set[str] = set()
    item_ids: set[str] = set()
    total_items = 0
    included_items = 0
    omitted_items = 0
    evidence_records: dict[str, dict[str, Any]] = {}
    previous_topic_end = -1.0
    for topic in topics:
        if not isinstance(topic, dict):
            errors.append("minutes_evidence_topic")
            continue
        topic_id = topic.get("topic_id")
        title = topic.get("title")
        start_sec = topic.get("start_sec")
        end_sec = topic.get("end_sec")
        valid_times = (
            isinstance(start_sec, (int, float))
            and not isinstance(start_sec, bool)
            and isinstance(end_sec, (int, float))
            and not isinstance(end_sec, bool)
            and 0 <= start_sec <= end_sec
            and (
                protocol_version < 3
                or plan is None
                or end_sec <= plan["total_duration_sec"]
            )
        )
        if (
            not isinstance(topic_id, str)
            or not topic_id.strip()
            or topic_id in topic_ids
            or not isinstance(title, str)
            or not title.strip()
            or not valid_times
            or (protocol_version >= 3 and float(start_sec) < previous_topic_end)
        ):
            errors.append("minutes_evidence_topic")
            continue
        topic_ids.add(topic_id)
        previous_topic_end = float(end_sec)
        items = topic.get("items")
        if not isinstance(items, list) or not items:
            errors.append(f"minutes_evidence_items:{topic_id}")
            continue
        for item in items:
            if not isinstance(item, dict):
                errors.append("minutes_evidence_item")
                continue
            item_id = item.get("item_id")
            kind = item.get("kind")
            text = item.get("text")
            source_start_sec = item.get("source_start_sec")
            source_end_sec = item.get("source_end_sec")
            source_text_sha256 = item.get("source_text_sha256")
            source_window_id = item.get("source_window_id")
            status = item.get("status")
            if (
                not isinstance(item_id, str)
                or not item_id.strip()
                or item_id in item_ids
                or kind not in _MINUTES_ITEM_KINDS
                or not isinstance(text, str)
                or not text.strip()
                or not isinstance(source_start_sec, (int, float))
                or isinstance(source_start_sec, bool)
                or source_start_sec < 0
                or status not in {"included", "omitted"}
            ):
                errors.append("minutes_evidence_item")
                continue
            if protocol_version >= 3:
                cue = cue_lookup.get((source_window_id, source_text_sha256))
                if (
                    not isinstance(source_end_sec, (int, float))
                    or isinstance(source_end_sec, bool)
                    or source_end_sec < source_start_sec
                    or not isinstance(source_text_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", source_text_sha256) is None
                    or not isinstance(source_window_id, str)
                    or cue is None
                    or abs(float(cue["source_start_sec"]) - float(source_start_sec)) > 0.001
                    or abs(float(cue["source_end_sec"]) - float(source_end_sec)) > 0.001
                    or float(source_start_sec) < float(start_sec)
                    or float(source_end_sec) > float(end_sec)
                ):
                    errors.append(f"minutes_evidence_source:{item_id}")
                elif isinstance(item_id, str):
                    evidence_records[item_id] = {
                        "item_id": item_id,
                        "source_window_id": str(source_window_id),
                        "source_text_sha256": str(source_text_sha256),
                        "kind": kind,
                        "text": text,
                        "source_start_sec": float(source_start_sec),
                        "source_end_sec": float(source_end_sec),
                        "status": status,
                        "owner": item.get("owner"),
                        "deadline": item.get("deadline"),
                    }
            item_ids.add(item_id)
            total_items += 1
            if kind == "action" and (
                not isinstance(item.get("owner"), str)
                or not item["owner"].strip()
                or not isinstance(item.get("deadline"), str)
                or not item["deadline"].strip()
            ):
                errors.append(f"minutes_evidence_action:{item_id}")
            if status == "included":
                included_items += 1
                anchor = item.get("minutes_anchor")
                anchor_seconds = _anchor_seconds(anchor) if isinstance(anchor, str) else None
                if (
                    not isinstance(anchor, str)
                    or anchor_seconds is None
                    or abs(anchor_seconds - float(source_start_sec)) > 5
                    or anchor not in minutes_text
                ):
                    errors.append(f"minutes_evidence_anchor:{item_id}")
            else:
                omitted_items += 1
                reason = item.get("omitted_reason")
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(f"minutes_evidence_omission:{item_id}")

    coverage = loaded.get("coverage")
    if not isinstance(coverage, dict) or coverage != {
        "total_items": total_items,
        "included_items": included_items,
        "omitted_items": omitted_items,
    }:
        errors.append("minutes_evidence_coverage")
    if protocol_version >= 3 and loaded.get("strategy") == "multi_stage" and plan:
        ledger_records: dict[str, dict[str, Any]] = {}
        if ledger_root is None or ledger_root.is_symlink() or not ledger_root.is_dir():
            errors.append("minutes_ledger")
        else:
            for window in plan["windows"]:
                window_id = window["window_id"]
                ledger_path = ledger_root / f"{window_id}.json"
                if (
                    ledger_path.is_symlink()
                    or not ledger_path.is_file()
                    or ledger_path.stat().st_size <= 0
                ):
                    errors.append(f"minutes_ledger_missing:{window_id}")
                    continue
                try:
                    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    errors.append(f"minutes_ledger_json:{window_id}")
                    continue
                items = ledger.get("items") if isinstance(ledger, dict) else None
                if (
                    not isinstance(ledger, dict)
                    or ledger.get("schema_version") != 1
                    or ledger.get("minutes_protocol_version") != 3
                    or ledger.get("window_id") != window_id
                    or not isinstance(items, list)
                    or (
                        not items
                        and (
                            not isinstance(ledger.get("empty_reason"), str)
                            or not ledger["empty_reason"].strip()
                        )
                    )
                ):
                    errors.append(f"minutes_ledger_schema:{window_id}")
                    continue
                for ledger_item in items:
                    if not isinstance(ledger_item, dict):
                        errors.append(f"minutes_ledger_item:{window_id}")
                        continue
                    item_id = ledger_item.get("item_id")
                    kind = ledger_item.get("kind")
                    text = ledger_item.get("text")
                    start = ledger_item.get("source_start_sec")
                    end = ledger_item.get("source_end_sec")
                    digest = ledger_item.get("source_text_sha256")
                    cue = cue_lookup.get((window_id, digest))
                    if (
                        not isinstance(item_id, str)
                        or not item_id.strip()
                        or kind not in _MINUTES_ITEM_KINDS
                        or not isinstance(text, str)
                        or not text.strip()
                        or not isinstance(start, (int, float))
                        or isinstance(start, bool)
                        or not isinstance(end, (int, float))
                        or isinstance(end, bool)
                        or not isinstance(digest, str)
                        or cue is None
                        or abs(float(cue["source_start_sec"]) - float(start)) > 0.001
                        or abs(float(cue["source_end_sec"]) - float(end)) > 0.001
                    ):
                        errors.append(f"minutes_ledger_item:{window_id}")
                        continue
                    if item_id in ledger_records:
                        errors.append(f"minutes_ledger_item:{window_id}")
                        continue
                    ledger_records[item_id] = {
                        "item_id": item_id,
                        "source_window_id": window_id,
                        "source_text_sha256": digest,
                        "kind": kind,
                        "text": text,
                        "source_start_sec": float(start),
                        "source_end_sec": float(end),
                        "status": ledger_item.get("status"),
                        "owner": ledger_item.get("owner"),
                        "deadline": ledger_item.get("deadline"),
                    }
        for item_id, evidence_record in evidence_records.items():
            ledger_record = ledger_records.get(item_id)
            if ledger_record is None:
                errors.append(f"minutes_evidence_ledger:{item_id}")
                continue
            minimum_fields = (
                "source_window_id",
                "source_text_sha256",
                "kind",
                "text",
                "source_start_sec",
                "source_end_sec",
            )
            optional_fields = ("status", "owner", "deadline")
            if any(
                ledger_record[field] != evidence_record[field]
                for field in minimum_fields
            ) or any(
                ledger_record[field] is not None
                and ledger_record[field] != evidence_record[field]
                for field in optional_fields
            ):
                errors.append(f"minutes_ledger_mismatch:{item_id}")
        for item_id in ledger_records.keys() - evidence_records.keys():
            errors.append(f"minutes_ledger_unaccounted:{item_id}")
    return list(dict.fromkeys(errors))


def _pcm_fingerprint(audio: Path) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    process = subprocess.Popen(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(audio),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    digest = hashlib.sha256()
    decoded_bytes = 0
    assert process.stdout is not None
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        decoded_bytes += len(chunk)
        digest.update(chunk)
    process.stdout.close()
    return_code = process.wait()
    if return_code != 0 or decoded_bytes == 0:
        return None
    return digest.hexdigest()


def _source_key(audio: Path, *, normalized_pcm: bool = False) -> str:
    match = _VM_ID_RE.search(audio.stem)
    if match:
        return "vm:" + match.group(1).lower()
    if normalized_pcm:
        fingerprint = _pcm_fingerprint(audio)
        if fingerprint:
            return "pcm:" + fingerprint
        return "file:" + _sha256_file(audio)
    return "path:" + str(audio.resolve())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_input_transcript(path: str | Path) -> tuple[Path, int, str]:
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise RelayControlError("输入逐字稿必须是普通文件且不能是符号链接")
    resolved = source.resolve()
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_INPUT_TRANSCRIPT_BYTES:
        raise RelayControlError(
            f"输入逐字稿大小必须在 1 到 {MAX_INPUT_TRANSCRIPT_BYTES} 字节之间"
        )
    try:
        resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RelayControlError("输入逐字稿必须是 UTF-8 文本") from exc
    return resolved, size, _sha256_file(resolved)


def _process_start_token(pid: int) -> str | None:
    """用进程启动时间区分重启后复用的 PID；macOS 与 Linux 的 ps 均支持 lstart。"""
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started_at = " ".join(completed.stdout.split())
    if completed.returncode != 0 or not started_at:
        return None
    return hashlib.sha256(started_at.encode("utf-8")).hexdigest()[:12]


def make_worker_id(prefix: str = "worker", pid: int | None = None) -> str:
    if not re.fullmatch(r"[a-z]+", prefix):
        raise ValueError("worker prefix 必须是小写字母")
    process_id = int(pid or os.getpid())
    token = _process_start_token(process_id)
    return f"{prefix}-{process_id}-{token}" if token else f"{prefix}-{process_id}"


def _worker_process_is_alive(worker_id: str | None) -> bool:
    if not worker_id:
        return False
    match = re.fullmatch(
        r"(?:watchdog|relayctl|worker)-(\d+)"
        r"(?:-([0-9a-f]{12}))?(?:-[a-z0-9]+)?",
        worker_id,
    )
    if not match:
        return False
    pid = int(match.group(1))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 跨 UID 进程仍可能存在，但 PID 也可能已被复用；继续核对启动指纹。
        pass
    expected_start = match.group(2)
    if expected_start:
        actual_start = _process_start_token(pid)
        if actual_start is not None and actual_start != expected_start:
            return False
    return True


def _configured_volume_mount(path: Path) -> Path | None:
    try:
        relative = path.relative_to("/Volumes")
    except ValueError:
        return None
    if not relative.parts:
        return None
    return Path("/Volumes") / relative.parts[0]


def _validate_archive_root_for_write(archive_root: Path) -> None:
    """写入前验证归档根，避免掉盘后在 /Volumes 下造出幻影目录。"""
    if archive_root.is_symlink():
        raise RelayControlError("归档根不能是符号链接")
    try:
        root_exists = archive_root.is_dir()
    except OSError as exc:
        raise RelayControlError(f"无法访问归档根: {archive_root}") from exc
    if not root_exists:
        raise RelayControlError(f"归档根不存在或不是目录: {archive_root}")

    volume_mount = _configured_volume_mount(archive_root)
    if volume_mount is None:
        return
    try:
        if volume_mount.is_symlink() or not os.path.ismount(volume_mount):
            raise RelayControlError(f"外置卷未真实挂载: {volume_mount}")
        if volume_mount.stat().st_dev == Path("/Volumes").stat().st_dev:
            raise RelayControlError(f"外置卷设备号无效: {volume_mount}")
    except RelayControlError:
        raise
    except OSError as exc:
        raise RelayControlError(f"无法验证外置卷挂载: {volume_mount}") from exc


def inspect_whisper_ref(archive_dir: str | Path) -> dict[str, Any]:
    """只检查 Whisper 对照稿完整性；主链完成校验不调用它作为门槛。"""
    root = Path(archive_dir).expanduser()
    whisper_dir = root / "whisper-ref"
    transcript_suffixes = {
        "json": ".json",
        "tsv": ".tsv",
        "srt": ".srt",
        "txt": ".txt",
        "vtt": ".vtt",
    }
    required_names = {*transcript_suffixes, "log"}
    if whisper_dir.is_symlink():
        return {
            "status": "failed",
            "ready": False,
            "missing": ["symlink"],
            "files": [],
            "selected_stem": None,
        }
    try:
        exists = whisper_dir.is_dir()
    except OSError:
        exists = False
    if not exists:
        return {
            "status": "absent",
            "ready": False,
            "missing": sorted(required_names),
            "files": [],
            "selected_stem": None,
        }

    try:
        entries = [
            path
            for path in whisper_dir.rglob("*")
            if not any(
                part.startswith("._") or part == ".DS_Store"
                for part in path.relative_to(whisper_dir).parts
            )
        ]
        if any(path.is_symlink() for path in entries):
            raise PublishValidationError("whisper-ref 包含符号链接")
        files = [path for path in entries if path.is_file()]
        nonempty = [path for path in files if path.stat().st_size > 0]
    except (OSError, PublishValidationError):
        return {
            "status": "failed",
            "ready": False,
            "missing": ["io_or_symlink"],
            "files": [],
            "selected_stem": None,
        }

    by_stem: dict[str, dict[str, Path]] = {}
    for path in nonempty:
        for key, suffix in transcript_suffixes.items():
            if path.suffix.lower() == suffix:
                by_stem.setdefault(path.stem, {})[key] = path
                break
    complete_stems = sorted(
        stem
        for stem, members in by_stem.items()
        if set(members) == set(transcript_suffixes)
    )
    log_files = [path for path in nonempty if path.name.lower() == "whisper.log"]
    missing: list[str] = []
    if not complete_stems:
        available = (
            set().union(*(members.keys() for members in by_stem.values()))
            if by_stem
            else set()
        )
        missing.extend(sorted(set(transcript_suffixes) - available))
        if not missing:
            missing.append("coherent_stem")
    if not log_files:
        missing.append("log")

    if len(complete_stems) > 1:
        return {
            "status": "failed",
            "ready": False,
            "missing": ["ambiguous_stem"],
            "files": sorted(str(path.relative_to(root)) for path in files),
            "selected_stem": None,
        }

    if complete_stems:
        json_file = by_stem[complete_stems[0]]["json"]
        try:
            json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "status": "failed",
                "ready": False,
                "missing": ["json_valid"],
                "files": sorted(str(path.relative_to(root)) for path in files),
                "selected_stem": complete_stems[0],
            }
    ready = not missing
    return {
        "status": "ready" if ready else "running",
        "ready": ready,
        "missing": sorted(set(missing)),
        "files": sorted(str(path.relative_to(root)) for path in files),
        "selected_stem": complete_stems[0] if len(complete_stems) == 1 else None,
    }


class RelayControl:
    """SQLite-backed relay job service."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        archive_root: str | Path | None = None,
        auto_pending_archive: bool | None = None,
        archive_lock_path: str | Path | None = None,
        initialize: bool = True,
    ):
        configured = db_path or os.getenv("MEETING_RELAY_JOBS_DB") or DEFAULT_DB_PATH
        self.db_path = Path(configured).expanduser()
        configured_archive = (
            archive_root
            or os.getenv("MEETING_RELAY_ARCHIVE_ROOT")
            or DEFAULT_ARCHIVE_ROOT
        )
        self.archive_root = Path(
            os.path.abspath(str(Path(configured_archive).expanduser()))
        )
        self.auto_pending_archive = (
            True if auto_pending_archive is None else bool(auto_pending_archive)
        )
        self.archive_lock_path = Path(
            archive_lock_path or DEFAULT_ARCHIVE_LOCK_PATH
        ).expanduser()
        self.products_root = Path(
            os.getenv("MEETING_RELAY_PRODUCTS_ROOT", str(DEFAULT_PRODUCTS_ROOT))
        ).expanduser()
        self.hotword_prompt_dir = self.db_path.parent / "hotword-prompts"
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _hotword_prompt_payload(prompt_path: str | Path) -> tuple[bytes, str, int]:
        source = Path(prompt_path).expanduser()
        if source.is_symlink() or not source.is_file():
            raise RelayControlError("热词文件不存在或不是安全的普通文件")
        if source.stat().st_size <= 0 or source.stat().st_size > MAX_HOTWORD_PROMPT_BYTES:
            raise RelayControlError("热词文件必须大于 0 且不超过 64 KiB")
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise RelayControlError("热词文件必须是 UTF-8 文本") from error
        terms: list[str] = []
        seen: set[str] = set()
        for term in _HOTWORD_TOKEN_RE.findall(text):
            if term not in seen:
                seen.add(term)
                terms.append(term)
        if not terms:
            raise RelayControlError("热词文件没有可用词条")
        if len(terms) > MAX_HOTWORD_TERMS:
            raise RelayControlError("任务热词超过 20 个，请按本场会议缩小范围")

        payload = ("\n".join(terms) + "\n").encode("utf-8")
        return payload, hashlib.sha256(payload).hexdigest(), len(terms)

    def _snapshot_hotword_prompt(
        self,
        job_id: str,
        prompt_path: str | Path,
        *,
        snapshot_name: str | None = None,
    ) -> tuple[Path, str, int]:
        payload, digest, count = self._hotword_prompt_payload(prompt_path)

        self.hotword_prompt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.hotword_prompt_dir.is_symlink() or not self.hotword_prompt_dir.is_dir():
            raise RelayControlError("热词快照目录不安全")
        safe_name = snapshot_name or job_id
        if re.fullmatch(r"[A-Za-z0-9-]+", safe_name) is None:
            raise RelayControlError("热词快照名称无效")
        target = self.hotword_prompt_dir / f"{safe_name}.txt"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{job_id}-", suffix=".tmp", dir=self.hotword_prompt_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target.resolve(), digest, count

    def _attempt_draft_dir(self, job_id: str, attempt_no: int) -> Path:
        relative = Path(".workbench-drafts") / job_id / f"attempt-{attempt_no}"
        _validate_archive_root_for_write(self.archive_root)
        cursor = self.archive_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RelayControlError("attempt 草稿路径包含符号链接")
            cursor.mkdir(exist_ok=True)
            if not cursor.is_dir():
                raise RelayControlError("attempt 草稿路径不是目录")
        return cursor

    @contextmanager
    def _archive_lock(self):
        """与 meeting-workbench 共用同一把归档锁。"""
        self.archive_lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.archive_lock_path.is_symlink():
            raise RelayControlError("归档锁不能是符号链接")
        with self.archive_lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _expected_attempt_dir(self, job_id: str, attempt_no: int) -> Path:
        return (
            self.archive_root
            / ".workbench-drafts"
            / job_id
            / f"attempt-{attempt_no}"
        )

    @staticmethod
    def _manifest_metadata(archive_dir: Path) -> tuple[str, int, str | None]:
        manifest_path = archive_dir / "workbench-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RelayControlError("待校对归档缺少合法 manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RelayControlError("待校对归档 manifest 无效") from exc
        job_id = manifest.get("job_id") if isinstance(manifest, dict) else None
        attempt = manifest.get("attempt") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1
            or not isinstance(job_id, str)
            or type(attempt) is not int
        ):
            raise RelayControlError("待校对归档 manifest 缺少 job/attempt")
        status = manifest.get("status")
        return job_id, attempt, status if isinstance(status, str) else None

    @classmethod
    def _manifest_identity(cls, archive_dir: Path) -> tuple[str, int]:
        job_id, attempt, _status = cls._manifest_metadata(archive_dir)
        return job_id, attempt

    def _managed_unreviewed_archive(
        self,
        row: sqlite3.Row,
        archive_dir: str | Path | None = None,
    ) -> Path:
        value = archive_dir if archive_dir is not None else row["archive_dir"]
        if not value:
            raise RelayControlError("任务没有待校对归档目录")
        target = Path(os.path.abspath(str(Path(value).expanduser())))
        if target.is_symlink() or not target.is_dir():
            raise RelayControlError("待校对归档不存在或不安全")
        canonical_root = self.archive_root.resolve()
        canonical_target = target.resolve()
        try:
            relative = canonical_target.relative_to(canonical_root)
        except ValueError as exc:
            raise RelayControlError("待校对归档不在归档根目录") from exc
        cursor = canonical_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RelayControlError("待校对归档路径包含符号链接")
        expected_hidden = self._expected_attempt_dir(
            str(row["job_id"]), int(row["current_attempt"])
        )
        pending_root = self.archive_root / "待校对"
        is_hidden = target.resolve() == expected_hidden.resolve()
        is_legacy_visible = target.parent.resolve() == pending_root.resolve()
        is_flat_visible = (
            target.parent.resolve() == canonical_root
            and not target.name.startswith(".")
            and target.name != "待校对"
        )
        if not is_hidden and not is_legacy_visible and not is_flat_visible:
            raise RelayControlError("待校对归档不是受管当前 attempt 目录")
        manifest_job, manifest_attempt, manifest_status = self._manifest_metadata(target)
        if (
            manifest_job != row["job_id"]
            or manifest_attempt != int(row["current_attempt"])
            or manifest_status == "published"
        ):
            raise RelayControlError("待校对归档 manifest 与当前 attempt 不匹配")
        return canonical_target

    @staticmethod
    def _safe_pending_title(value: str) -> str:
        title = unicodedata.normalize("NFKC", value)
        title = re.sub(r"^(?:20)?\d{6}(?:[-_.\s]+)?", "", title)
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
        title = re.sub(r"\s+", " ", title).strip(" .")
        return title[:80].rstrip(" .") or "待校对会议"

    def _pending_archive_name(self, row: sqlite3.Row, source: Path) -> str:
        vm_match = _VM_ID_RE.search(Path(row["audio_path"]).stem)
        if vm_match is None:
            vm_match = next(
                (
                    _VM_ID_RE.search(path.stem)
                    for path in source.iterdir()
                    if path.is_file()
                ),
                None,
            )
        if vm_match:
            date_prefix = vm_match.group(1)[3:11]
            date_prefix = date_prefix[2:]
        else:
            try:
                date_prefix = datetime.fromtimestamp(
                    Path(row["audio_path"]).stat().st_mtime
                ).strftime("%y%m%d")
            except OSError:
                date_prefix = datetime.now().strftime("%y%m%d")
        md_stems = {
            path.stem
            for path in source.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and not path.name.startswith(".")
            and path.suffix.lower() == ".md"
        }
        html_stems = {
            path.stem
            for path in source.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and not path.name.startswith(".")
            and path.suffix.lower() == ".html"
        }
        common_stems = sorted(md_stems & html_stems)
        raw_title = common_stems[0] if common_stems else "待校对会议"
        return f"{date_prefix} {self._safe_pending_title(raw_title)}"

    def _pending_journal_path(self, job_id: str, attempt_no: int) -> Path:
        journal_root = self.archive_root / ".workbench-pending-journal"
        if journal_root.is_symlink():
            raise RelayControlError("待校对 journal 目录不安全")
        journal_root.mkdir(exist_ok=True)
        return journal_root / f"{job_id}-attempt-{attempt_no}.json"

    @staticmethod
    def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _move_pending_to_recovery(
        self,
        source: Path,
        *,
        job_id: str,
        attempt_no: int,
        category: str,
    ) -> Path:
        recovery_root = (
            self.archive_root / ".workbench-recovery" / category / job_id
        )
        if recovery_root.is_symlink():
            raise RelayControlError("归档恢复目录不安全")
        recovery_root.mkdir(parents=True, exist_ok=True)
        candidate = recovery_root / f"attempt-{attempt_no}-{source.name}"
        suffix = 2
        while candidate.exists():
            candidate = recovery_root / f"attempt-{attempt_no}-{source.name}-{suffix}"
            suffix += 1
        os.replace(source, candidate)
        return candidate

    def _visible_pending_for_job(
        self,
        job_id: str,
        *,
        attempt_no: int | None = None,
    ) -> list[Path]:
        pending_root = self.archive_root / "待校对"
        if pending_root.is_symlink():
            raise RelayControlError("待校对目录不能是符号链接")
        published_archive: Path | None = None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT published_archive_dir FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is not None and row["published_archive_dir"]:
            published_archive = Path(row["published_archive_dir"]).expanduser().resolve()
        matches: list[Path] = []
        candidates = [
            path
            for path in self.archive_root.iterdir()
            if not path.name.startswith(".") and path.name != "待校对"
        ]
        if pending_root.is_dir():
            candidates.extend(pending_root.iterdir())
        for candidate in sorted(candidates, key=lambda path: str(path)):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                manifest_job, manifest_attempt, manifest_status = (
                    self._manifest_metadata(candidate)
                )
            except RelayControlError:
                continue
            canonical_candidate = candidate.resolve()
            if (
                manifest_status != "published"
                and canonical_candidate != published_archive
                and manifest_job == job_id
                and (
                attempt_no is None or manifest_attempt == attempt_no
                )
            ):
                matches.append(canonical_candidate)
        return matches

    def _select_pending_target(self, row: sqlite3.Row, source: Path) -> Path:
        pending_root = self.archive_root
        base = pending_root / self._pending_archive_name(row, source)
        vm_match = _VM_ID_RE.search(Path(row["audio_path"]).stem)
        discriminator = vm_match.group(1) if vm_match else str(row["job_id"])
        vm_candidate = pending_root / (
            f"{base.name} {self._safe_pending_title(discriminator)}"
        )
        job_candidate = pending_root / f"{vm_candidate.name} {row['job_id']}"
        candidates = [base, vm_candidate, job_candidate]
        suffix = 2
        while True:
            if candidates:
                candidate = candidates.pop(0)
            else:
                candidate = pending_root / f"{job_candidate.name}-{suffix}"
                suffix += 1
            if not candidate.exists():
                return candidate
            try:
                candidate_job, candidate_attempt, candidate_status = (
                    self._manifest_metadata(candidate)
                )
            except RelayControlError:
                candidate_job = ""
                candidate_attempt = -1
                candidate_status = None
            published_archive = row["published_archive_dir"]
            is_published_archive = bool(
                published_archive
                and candidate.resolve()
                == Path(published_archive).expanduser().resolve()
            )
            if (
                candidate_job == row["job_id"]
                and candidate_attempt == int(row["current_attempt"])
                and candidate_status != "published"
                and not is_published_archive
            ):
                return candidate

    def _promote_pending_archive(self, job_id: str) -> dict[str, Any]:
        """在共享锁下将当前 hidden attempt 原子提升到待校对。"""
        with self._archive_lock():
            _validate_archive_root_for_write(self.archive_root)
            with self._connect() as connection:
                row = self._job_row(connection, job_id)
                archive_policy = self._archive_policy(connection, job_id)
            if row["status"] not in {"completed_unreviewed", "draft_modified"}:
                raise InvalidTransitionError(
                    f"待校对提升要求 completed_unreviewed/draft_modified，"
                    f"当前为 {row['status']}"
                )
            attempt_no = int(row["current_attempt"])
            expected_source = self._expected_attempt_dir(job_id, attempt_no)
            current_value = Path(
                os.path.abspath(str(Path(row["archive_dir"]).expanduser()))
            ) if row["archive_dir"] else None
            journal_path = self._pending_journal_path(job_id, attempt_no)
            journal: dict[str, Any] | None = None
            if journal_path.is_file() and not journal_path.is_symlink():
                try:
                    loaded = json.loads(journal_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        journal = loaded
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    journal = None

            source = expected_source
            if current_value is not None and current_value.is_dir():
                try:
                    managed = self._managed_unreviewed_archive(row, current_value)
                except RelayControlError:
                    managed = None
                if managed is None:
                    # 工作台先原地安装 published manifest、Relay 后确认回执时会有一个
                    # 很短的窗口；此时不得把正式目录重新搬迁或标成归档失败。
                    try:
                        current_job, current_attempt, current_status = (
                            self._manifest_metadata(current_value)
                        )
                    except RelayControlError:
                        current_job, current_attempt, current_status = "", -1, None
                    if (
                        current_value.parent.resolve() == self.archive_root.resolve()
                        and current_job == job_id
                        and current_attempt == attempt_no
                        and current_status == "published"
                    ):
                        journal_path.unlink(missing_ok=True)
                        self._clear_pending_archive_failure(job_id, attempt_no)
                        return self.status(job_id)
                    raise RelayControlError("任务 archive_dir 不是受管当前 attempt")
                if managed.parent == self.archive_root.resolve():
                    journal_path.unlink(missing_ok=True)
                    self._clear_pending_archive_failure(job_id, attempt_no)
                    return self.status(job_id)
                source = managed
            elif current_value is None:
                raise RelayControlError("任务 archive_dir 不是当前 hidden attempt")
            elif current_value.resolve() != expected_source.resolve():
                journal_source = None
                if (
                    journal is not None
                    and journal.get("job_id") == job_id
                    and journal.get("attempt") == attempt_no
                ):
                    journal_source = Path(
                        os.path.abspath(
                            str(
                                Path(
                                    str(journal.get("source", ""))
                                ).expanduser()
                            )
                        )
                    )
                if (
                    journal_source is None
                    or journal_source.resolve() != current_value.resolve()
                    or current_value.parent.resolve()
                    != (self.archive_root / "待校对").resolve()
                ):
                    raise RelayControlError("任务 archive_dir 不是当前 hidden attempt")
                # legacy 目录已完成 os.replace、DB CAS 尚未提交的恢复窗口。
                source = current_value

            if (
                archive_policy == "hidden_fixture"
                and source.resolve() == expected_source.resolve()
                and not journal_path.exists()
            ):
                self._managed_unreviewed_archive(row, source)
                self._clear_pending_archive_failure(job_id, attempt_no)
                return self.status(job_id)

            if journal is None:
                if source.is_dir():
                    self._managed_unreviewed_archive(row, source)
                    target = self._select_pending_target(row, source)
                else:
                    installed = self._visible_pending_for_job(
                        job_id, attempt_no=attempt_no
                    )
                    if len(installed) != 1:
                        raise RelayControlError("找不到可恢复的当前 attempt 待校对目录")
                    target = installed[0]
                journal = {
                    "schema_version": 1,
                    "job_id": job_id,
                    "attempt": attempt_no,
                    "source": str(source),
                    "target": str(target),
                    "state": "prepared",
                }
                self._write_json_atomically(journal_path, journal)
            else:
                if (
                    journal.get("job_id") != job_id
                    or journal.get("attempt") != attempt_no
                ):
                    raise RelayControlError("待校对 journal 与当前 attempt 不匹配")
                source = Path(
                    os.path.abspath(
                        str(Path(str(journal.get("source", ""))).expanduser())
                    )
                )
                source_is_hidden = source.resolve() == expected_source.resolve()
                source_is_legacy = (
                    source.parent.resolve()
                    == (self.archive_root / "待校对").resolve()
                )
                if not source_is_hidden and not source_is_legacy:
                    raise RelayControlError("待校对 journal 来源越界")
                target = Path(
                    os.path.abspath(
                        str(Path(str(journal.get("target", ""))).expanduser())
                    )
                )
                if target.parent.resolve() not in {
                    self.archive_root.resolve(),
                    (self.archive_root / "待校对").resolve(),
                }:
                    raise RelayControlError("待校对 journal 目标越界")

            if journal["state"] == "prepared":
                for old_pending in self._visible_pending_for_job(job_id):
                    try:
                        _old_job, old_attempt = self._manifest_identity(old_pending)
                    except RelayControlError:
                        continue
                    if (
                        old_attempt == attempt_no
                        and old_pending in {source.resolve(), target.resolve()}
                    ):
                        continue
                    self._move_pending_to_recovery(
                        old_pending,
                        job_id=job_id,
                        attempt_no=old_attempt,
                        category="replaced",
                    )
                journal["state"] = "old_moved"
                self._write_json_atomically(journal_path, journal)

            if journal["state"] == "old_moved":
                if source.is_dir():
                    if target.exists():
                        target_job, target_attempt = self._manifest_identity(target)
                        if target_job != job_id or target_attempt != attempt_no:
                            target = self._select_pending_target(row, source)
                            journal["target"] = str(target)
                            self._write_json_atomically(journal_path, journal)
                    if target.exists():
                        self._move_pending_to_recovery(
                            source,
                            job_id=job_id,
                            attempt_no=attempt_no,
                            category="duplicate",
                        )
                    else:
                        os.replace(source, target)
                target_job, target_attempt = self._manifest_identity(target)
                if target_job != job_id or target_attempt != attempt_no:
                    raise RelayControlError("新安装的待校对归档身份不匹配")
                journal["state"] = "new_installed"
                self._write_json_atomically(journal_path, journal)

            if journal["state"] == "new_installed":
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = self._job_row(connection, job_id)
                    if current["archive_dir"] == str(target.resolve()):
                        updated = None
                    else:
                        updated = connection.execute(
                            """
                            UPDATE jobs SET archive_dir = ?, updated_at = ?
                            WHERE job_id = ? AND current_attempt = ?
                              AND status IN ('completed_unreviewed', 'draft_modified')
                              AND archive_dir = ?
                            """,
                            (
                                str(target.resolve()),
                                _now(),
                                job_id,
                                attempt_no,
                                str(source.resolve()),
                            ),
                        )
                        if updated.rowcount != 1:
                            raise InvalidTransitionError("待校对提升 DB CAS 失败")
                        self._append_event(
                            connection,
                            job_id,
                            attempt_no,
                            "pending_archive_promoted",
                            current["status"],
                            current["status"],
                            stage="pending_archive",
                            payload={
                                "source_archive_dir": str(source),
                                "archive_dir": str(target.resolve()),
                            },
                        )
                    connection.execute(
                        """
                        UPDATE jobs SET failure_stage = NULL, last_error = NULL,
                            updated_at = ?
                        WHERE job_id = ? AND current_attempt = ?
                          AND status IN ('completed_unreviewed', 'draft_modified')
                          AND archive_dir = ? AND failure_stage = 'pending_archive'
                        """,
                        (_now(), job_id, attempt_no, str(target.resolve())),
                    )
                journal["state"] = "db_committed"
                self._write_json_atomically(journal_path, journal)

            if journal["state"] == "db_committed":
                journal_path.unlink(missing_ok=True)
            return self.status(job_id)

    def _record_pending_archive_failure(
        self,
        job_id: str,
        attempt_no: int,
        error: Exception,
    ) -> None:
        safe_error = f"{type(error).__name__}: {error}"[:512]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if (
                int(row["current_attempt"]) != attempt_no
                or row["status"] not in {"completed_unreviewed", "draft_modified"}
            ):
                return
            connection.execute(
                """
                UPDATE jobs SET failure_stage = 'pending_archive', last_error = ?,
                    updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND status IN ('completed_unreviewed', 'draft_modified')
                """,
                (safe_error, _now(), job_id, attempt_no),
            )
            self._append_event(
                connection,
                job_id,
                attempt_no,
                "pending_archive_failed",
                row["status"],
                row["status"],
                stage="pending_archive",
                payload={"error_type": type(error).__name__},
            )

    def _clear_pending_archive_failure(self, job_id: str, attempt_no: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if row["failure_stage"] != "pending_archive":
                return
            updated = connection.execute(
                """
                UPDATE jobs SET failure_stage = NULL, last_error = NULL,
                    updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND failure_stage = 'pending_archive'
                  AND status IN ('completed_unreviewed', 'draft_modified')
                """,
                (_now(), job_id, attempt_no),
            )
            if updated.rowcount == 1:
                self._append_event(
                    connection,
                    job_id,
                    attempt_no,
                    "pending_archive_recovered",
                    row["status"],
                    row["status"],
                    stage="pending_archive",
                )

    def migrate_pending_archives(
        self,
        job_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = list(dict.fromkeys(job_ids)) if job_ids is not None else None
        with self._connect() as connection:
            if selected is None:
                rows = connection.execute(
                    """
                    SELECT job_id FROM jobs
                    WHERE status IN ('completed_unreviewed', 'draft_modified')
                    ORDER BY updated_at, job_id
                    """
                ).fetchall()
                candidates = [str(row["job_id"]) for row in rows]
            else:
                candidates = selected
        summary: dict[str, Any] = {
            "ok": True,
            "selected": len(candidates),
            "promoted": [],
            "already_pending": [],
            "skipped": [],
            "errors": [],
        }

        def promote_to_flat(job_id: str) -> dict[str, Any]:
            promoted = self._promote_pending_archive(job_id)
            promoted_archive = Path(str(promoted["archive_dir"])).expanduser()
            if (
                promoted_archive.parent.resolve()
                == (self.archive_root / "待校对").resolve()
            ):
                # 兼容旧版本已经写下的二级 target journal；同一次显式迁移
                # 必须继续完成扁平化，不能把“仍在待校对”报告成最终成功。
                promoted = self._promote_pending_archive(job_id)
            return promoted

        for candidate in candidates:
            before: dict[str, Any] | None = None
            try:
                before = self.status(candidate)
                if before["status"] not in {"completed_unreviewed", "draft_modified"}:
                    summary["skipped"].append(
                        {"job_id": candidate, "reason": f"status:{before['status']}"}
                    )
                    continue
                archive_value = before.get("archive_dir")
                if not archive_value:
                    summary["skipped"].append(
                        {"job_id": candidate, "reason": "archive_dir:null"}
                    )
                    continue
                archive = Path(archive_value).expanduser()
                expected = self._expected_attempt_dir(
                    candidate, int(before["current_attempt"])
                )
                if archive.resolve() != expected.resolve():
                    recovery_journal = (
                        self.archive_root
                        / ".workbench-pending-journal"
                        / f"{candidate}-attempt-{int(before['current_attempt'])}.json"
                    )
                    if recovery_journal.is_file() and not recovery_journal.is_symlink():
                        promote_to_flat(candidate)
                        summary["promoted"].append(candidate)
                        continue
                    try:
                        with self._connect() as connection:
                            row = self._job_row(connection, candidate)
                        managed = self._managed_unreviewed_archive(row, archive)
                    except RelayControlError:
                        try:
                            manifest_job, manifest_attempt, manifest_status = (
                                self._manifest_metadata(archive)
                            )
                        except RelayControlError:
                            manifest_job, manifest_attempt, manifest_status = "", -1, None
                        if (
                            archive.parent.resolve() == self.archive_root.resolve()
                            and manifest_job == candidate
                            and manifest_attempt == int(before["current_attempt"])
                            and manifest_status == "published"
                        ):
                            summary["skipped"].append(
                                {"job_id": candidate, "reason": "awaiting_publish_ack"}
                            )
                            continue
                        summary["skipped"].append(
                            {"job_id": candidate, "reason": "archive_dir:not_managed"}
                        )
                        continue
                    promote_to_flat(candidate)
                    if managed.parent == self.archive_root.resolve():
                        summary["already_pending"].append(candidate)
                    else:
                        summary["promoted"].append(candidate)
                    continue
                promoted = promote_to_flat(candidate)
                promoted_archive = Path(str(promoted["archive_dir"])).expanduser()
                if (
                    promoted.get("archive_policy") == "hidden_fixture"
                    and promoted_archive.resolve() == expected.resolve()
                ):
                    summary["skipped"].append(
                        {"job_id": candidate, "reason": "policy:hidden_fixture"}
                    )
                else:
                    summary["promoted"].append(candidate)
            except Exception as exc:
                if before is not None and before["status"] in {
                    "completed_unreviewed",
                    "draft_modified",
                }:
                    self._record_pending_archive_failure(
                        candidate, int(before["current_attempt"]), exc
                    )
                summary["ok"] = False
                summary["errors"].append(
                    {
                        "job_id": candidate,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        return summary

    def _validated_whisper_install_paths(
        self,
        row: sqlite3.Row,
        source_root: str | Path,
        *,
        source_boundary: Path,
    ) -> tuple[Path, Path, str]:
        source = Path(os.path.abspath(str(Path(source_root).expanduser())))
        if source.is_symlink() or not source.is_dir():
            raise RelayControlError("Whisper 安装来源目录无效")
        try:
            source.resolve().relative_to(source_boundary.expanduser().resolve())
        except ValueError as exc:
            raise RelayControlError("Whisper 安装来源越界") from exc
        inspection = inspect_whisper_ref(source)
        if inspection.get("status") != "ready":
            raise RelayControlError("Whisper 安装产物不完整")

        target_root = self._managed_unreviewed_archive(row)
        if source.resolve() == target_root:
            raise RelayControlError("Whisper 安装来源不能是目标归档")
        if not row["audio_sha256"]:
            raise RelayControlError("Whisper 安装缺少原音频哈希")
        allowed_stems = {
            _canonical_media_stem(Path(row["audio_path"]).stem)
        }
        matched_original_audio = False
        for audio in target_root.iterdir():
            if (
                audio.is_file()
                and not audio.is_symlink()
                and audio.suffix.lower() in AUDIO_EXTENSIONS
                and _sha256_file(audio) == row["audio_sha256"]
            ):
                matched_original_audio = True
                allowed_stems.add(_canonical_media_stem(audio.stem))
        if not matched_original_audio:
            raise RelayControlError("Whisper 安装目标缺少匹配的原音频")
        selected_stem = _canonical_media_stem(
            str(inspection.get("selected_stem") or "")
        )
        if not selected_stem or selected_stem not in allowed_stems:
            raise RelayControlError("Whisper 安装产物与原音频不匹配")
        return source, target_root, selected_stem

    def _install_whisper_ref(
        self,
        row: sqlite3.Row,
        source_root: str | Path,
        *,
        source_boundary: Path,
    ) -> tuple[Path, Path | None]:
        source, target_root, selected_stem = self._validated_whisper_install_paths(
            row, source_root, source_boundary=source_boundary
        )
        recovery_root = self._whisper_install_recovery_dir(row)
        self._recover_whisper_install(row, recovery_root)
        recovery_root.mkdir(parents=True, exist_ok=False)
        stage_root = recovery_root / "stage"
        staged_whisper = stage_root / "whisper-ref"
        target_whisper = target_root / "whisper-ref"
        backup = recovery_root / "backup-whisper-ref"
        journal_path = recovery_root / "journal.json"
        journal = {
            "schema_version": 1,
            "job_id": str(row["job_id"]),
            "attempt": int(row["current_attempt"]),
            "target_archive_dir": str(target_root),
            "selected_stem": selected_stem,
            "had_previous": target_whisper.exists(),
            "phase": "prepared",
        }
        self._write_json_atomically(journal_path, journal)
        stage_root.mkdir()

        def record_phase(phase: str) -> None:
            journal["phase"] = phase
            self._write_json_atomically(journal_path, journal)

        try:
            shutil.copytree(source / "whisper-ref", staged_whisper)
            staged_inspection = inspect_whisper_ref(stage_root)
            if (
                staged_inspection.get("status") != "ready"
                or _canonical_media_stem(
                    str(staged_inspection.get("selected_stem") or "")
                )
                != selected_stem
            ):
                raise RelayControlError("Whisper 安装临时副本复验失败")
            record_phase("copied")
            if target_whisper.is_symlink():
                raise RelayControlError("Whisper 安装目标不安全")
            if target_whisper.exists():
                record_phase("old_moving")
                os.replace(target_whisper, backup)
                record_phase("old_moved")
            record_phase("new_installing")
            os.replace(staged_whisper, target_whisper)
            record_phase("new_installed")
            installed_inspection = inspect_whisper_ref(target_root)
            if (
                installed_inspection.get("status") != "ready"
                or _canonical_media_stem(
                    str(installed_inspection.get("selected_stem") or "")
                )
                != selected_stem
            ):
                raise RelayControlError("Whisper 安装后复验失败")
            return target_whisper, recovery_root
        except Exception:
            self._recover_whisper_install(row, recovery_root)
            raise

    def _whisper_install_recovery_dir(self, row: sqlite3.Row) -> Path:
        root = self.archive_root / ".workbench-recovery" / "whisper-install"
        cursor = self.archive_root
        for part in Path(".workbench-recovery/whisper-install").parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RelayControlError("Whisper 恢复目录不安全")
            cursor.mkdir(exist_ok=True)
        job_root = root / str(row["job_id"])
        if job_root.is_symlink():
            raise RelayControlError("Whisper job 恢复目录不安全")
        job_root.mkdir(exist_ok=True)
        return job_root / f"attempt-{int(row['current_attempt'])}"

    def _recover_whisper_install(
        self,
        row: sqlite3.Row,
        recovery_root: Path | None = None,
    ) -> None:
        operation = recovery_root or self._whisper_install_recovery_dir(row)
        if not operation.exists():
            return
        if operation.is_symlink() or not operation.is_dir():
            raise RelayControlError("Whisper 安装恢复状态不安全")
        journal_path = operation / "journal.json"
        if not journal_path.exists():
            if any(operation.iterdir()):
                raise RelayControlError("Whisper 安装恢复 journal 缺失")
            operation.rmdir()
            return
        if journal_path.is_symlink() or not journal_path.is_file():
            raise RelayControlError("Whisper 安装恢复 journal 不安全")
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RelayControlError("Whisper 安装恢复 journal 无效") from exc
        phase = journal.get("phase") if isinstance(journal, dict) else None
        if (
            not isinstance(journal, dict)
            or journal.get("schema_version") != 1
            or journal.get("job_id") != row["job_id"]
            or type(journal.get("attempt")) is not int
            or journal.get("attempt") != int(row["current_attempt"])
            or phase
            not in {
                "prepared",
                "copied",
                "old_moving",
                "old_moved",
                "new_installing",
                "new_installed",
            }
            or not isinstance(journal.get("had_previous"), bool)
        ):
            raise RelayControlError("Whisper 安装恢复 journal 身份无效")
        target_root = self._managed_unreviewed_archive(
            row, str(journal.get("target_archive_dir", ""))
        )
        if str(target_root) != str(journal.get("target_archive_dir")):
            raise RelayControlError("Whisper 安装恢复目标不匹配")
        target_whisper = target_root / "whisper-ref"
        backup = operation / "backup-whisper-ref"
        stage_root = operation / "stage"

        if row["whisper_status"] == "ready":
            inspection = inspect_whisper_ref(target_root)
            selected_stem = _canonical_media_stem(
                str(journal.get("selected_stem") or "")
            )
            if (
                inspection.get("status") != "ready"
                or _canonical_media_stem(
                    str(inspection.get("selected_stem") or "")
                )
                != selected_stem
            ):
                raise RelayControlError("Whisper 已提交安装无法完成复验")
            shutil.rmtree(operation)
            return

        if backup.exists():
            if backup.is_symlink() or not backup.is_dir():
                raise RelayControlError("Whisper 安装备份不安全")
            if target_whisper.exists():
                if target_whisper.is_symlink() or not target_whisper.is_dir():
                    raise RelayControlError("Whisper 安装回滚目标不安全")
                shutil.rmtree(target_whisper)
            os.replace(backup, target_whisper)
        elif phase in {"old_moved", "new_installing", "new_installed"}:
            if journal["had_previous"]:
                raise RelayControlError("Whisper 安装旧版本备份缺失")
            if target_whisper.exists():
                if target_whisper.is_symlink() or not target_whisper.is_dir():
                    raise RelayControlError("Whisper 安装回滚目标不安全")
                shutil.rmtree(target_whisper)
        if stage_root.exists() and (stage_root.is_symlink() or not stage_root.is_dir()):
            raise RelayControlError("Whisper 安装临时目录不安全")
        shutil.rmtree(operation)

    def _rollback_whisper_install(
        self,
        installed: Path | None,
        recovery_root: Path | None,
    ) -> None:
        del installed
        if recovery_root is None or not recovery_root.exists():
            return
        try:
            journal = json.loads(
                (recovery_root / "journal.json").read_text(encoding="utf-8")
            )
            job_id = journal.get("job_id")
            with self._connect() as connection:
                row = self._job_row(connection, str(job_id))
            self._recover_whisper_install(row, recovery_root)
        except Exception:
            # 恢复状态必须留给下轮 reconcile；不能在异常路径破坏唯一备份。
            return

    @staticmethod
    def _cleanup_whisper_backup(recovery_root: Path | None) -> None:
        if recovery_root is None:
            return
        try:
            shutil.rmtree(recovery_root, ignore_errors=True)
        except OSError:
            pass

    def _reconcile_product_whisper(
        self,
        job_id: str,
        product_archive: Path,
    ) -> bool:
        installed: Path | None = None
        backup: Path | None = None
        committed = False
        with self._archive_lock():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._job_row(connection, job_id)
                if (
                    row["status"] not in {"completed_unreviewed", "draft_modified"}
                    or row["whisper_status"] not in {"pending", "running"}
                    or int(row["whisper_attempt"]) != int(row["current_attempt"])
                ):
                    connection.rollback()
                    return False
                installed, backup = self._install_whisper_ref(
                    row,
                    product_archive,
                    source_boundary=self.products_root,
                )
                now = _now()
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET whisper_status = 'ready', whisper_error = NULL,
                        whisper_retry_requested = 0, whisper_worker_id = NULL,
                        whisper_claimed_at = NULL, whisper_updated_at = ?,
                        updated_at = ?
                    WHERE job_id = ? AND current_attempt = ?
                      AND whisper_attempt = ?
                      AND status IN ('completed_unreviewed', 'draft_modified')
                      AND whisper_status IN ('pending', 'running')
                      AND archive_dir = ?
                    """,
                    (
                        now,
                        now,
                        job_id,
                        row["current_attempt"],
                        row["whisper_attempt"],
                        row["archive_dir"],
                    ),
                )
                if updated.rowcount != 1:
                    raise InvalidTransitionError("Whisper 对账 CAS 失败")
                self._append_event(
                    connection,
                    job_id,
                    row["current_attempt"],
                    "whisper_reconciled",
                    row["whisper_status"],
                    "ready",
                    stage="whisper",
                    payload={"source": str(product_archive)},
                )
                connection.commit()
                committed = True
                self._cleanup_whisper_backup(backup)
                return True
            except Exception:
                if not committed:
                    connection.rollback()
                    self._rollback_whisper_install(installed, backup)
                raise
            finally:
                connection.close()

    def reconcile_pending_archives(self) -> dict[str, Any]:
        """提升遗留 hidden 归档，并回填已完成的产品 Whisper。"""
        summary = (
            self.migrate_pending_archives()
            if self.auto_pending_archive
            else {
                "ok": True,
                "selected": 0,
                "promoted": [],
                "already_pending": [],
                "skipped": [],
                "errors": [],
            }
        )
        summary["whisper_recoveries"] = []
        recovery_base = (
            self.archive_root / ".workbench-recovery" / "whisper-install"
        )
        if recovery_base.is_symlink():
            summary["ok"] = False
            summary["errors"].append(
                {
                    "stage": "whisper_recovery",
                    "error_type": "RelayControlError",
                    "error": "Whisper 恢复根不能是符号链接",
                }
            )
        elif recovery_base.is_dir():
            for operation in sorted(recovery_base.glob("*/attempt-*")):
                if operation.is_symlink() or not operation.is_dir():
                    continue
                job_id = operation.parent.name
                try:
                    with self._archive_lock():
                        with self._connect() as connection:
                            row = self._job_row(connection, job_id)
                        self._recover_whisper_install(row, operation)
                    summary["whisper_recoveries"].append(job_id)
                except Exception as exc:
                    summary["ok"] = False
                    summary["errors"].append(
                        {
                            "job_id": job_id,
                            "stage": "whisper_recovery",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
        summary["whisper_checked"] = []
        summary["whisper_ready"] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('completed_unreviewed', 'draft_modified')
                  AND whisper_status IN ('pending', 'running')
                ORDER BY updated_at, job_id
                """
            ).fetchall()
        for row in rows:
            job_id = str(row["job_id"])
            try:
                audio_stem = Path(row["audio_path"]).stem
                product_archive = self.products_root / audio_stem / audio_stem
                summary["whisper_checked"].append(job_id)
                if product_archive.is_symlink() or not product_archive.is_dir():
                    continue
                if self._reconcile_product_whisper(job_id, product_archive):
                    summary["whisper_ready"].append(job_id)
            except Exception as exc:
                summary["ok"] = False
                summary["errors"].append(
                    {
                        "job_id": job_id,
                        "stage": "whisper_reconcile",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        summary["published_pending_recovered"] = []
        with self._connect() as connection:
            published_rows = connection.execute(
                """
                SELECT job_id, current_attempt FROM jobs
                WHERE status = 'published'
                ORDER BY updated_at, job_id
                """
            ).fetchall()
        for row in published_rows:
            job_id = str(row["job_id"])
            try:
                recovered = self._recover_published_pending_sources(
                    job_id, int(row["current_attempt"])
                )
                if recovered:
                    summary["published_pending_recovered"].append(job_id)
            except Exception as exc:
                summary["ok"] = False
                summary["errors"].append(
                    {
                        "job_id": job_id,
                        "stage": "published_pending_cleanup",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        return summary

    def _snapshot_input_transcript(
        self,
        job_id: str,
        attempt_no: int,
        transcript_path: str | Path,
    ) -> tuple[Path, int, str]:
        source, size, digest = _validated_input_transcript(transcript_path)
        if source.suffix.lower() != ".srt":
            raise RelayControlError("v3 输入逐字稿必须是合法 SRT")
        try:
            _parse_srt_cues(source)
        except RelayControlError as exc:
            raise RelayControlError("v3 输入逐字稿必须是合法 SRT") from exc
        target_dir = self._attempt_draft_dir(job_id, attempt_no)
        target = target_dir / "input-transcript.srt"
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RelayControlError("输入逐字稿快照目标不安全")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".input-transcript-", suffix=".tmp", dir=target_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if temporary.stat().st_size != size or _sha256_file(temporary) != digest:
                raise RelayControlError("输入逐字稿快照复制校验失败")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target.resolve(), size, digest

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    source_key TEXT NOT NULL UNIQUE,
                    audio_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_attempt INTEGER NOT NULL DEFAULT 1,
                    retry_stage TEXT,
                    stop_after_stage INTEGER NOT NULL DEFAULT 0,
                    archive_dir TEXT,
                    last_error TEXT,
                    failure_stage TEXT,
                    audio_sha256 TEXT,
                    audio_size INTEGER,
                    deduplicated_to TEXT,
                    worker_id TEXT,
                    claimed_at TEXT,
                    codex_dispatched_at TEXT,
                    whisper_status TEXT NOT NULL DEFAULT 'pending',
                    whisper_error TEXT,
                    whisper_updated_at TEXT,
                    whisper_attempt INTEGER NOT NULL DEFAULT 1,
                    whisper_retry_requested INTEGER NOT NULL DEFAULT 0,
                    whisper_retry_generation INTEGER NOT NULL DEFAULT 0,
                    whisper_worker_id TEXT,
                    whisper_claimed_at TEXT,
                    index_status TEXT NOT NULL DEFAULT 'absent',
                    index_error TEXT,
                    index_updated_at TEXT,
                    index_attempt INTEGER NOT NULL DEFAULT 1,
                    meeting_id TEXT,
                    published_archive_dir TEXT,
                    published_manifest_path TEXT,
                    published_manifest_sha256 TEXT,
                    published_at TEXT,
                    hotword_prompt_path TEXT,
                    hotword_prompt_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    requested_stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_transcript_path TEXT,
                    input_transcript_sha256 TEXT,
                    input_transcript_bytes INTEGER,
                    source_srt_sha256 TEXT,
                    minutes_plan_sha256 TEXT,
                    minutes_protocol_version INTEGER NOT NULL DEFAULT 1,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(job_id, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    stage TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, relative_path)
                );

                CREATE TABLE IF NOT EXISTS job_archive_policies (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                    policy TEXT NOT NULL CHECK (policy IN ('hidden_fixture')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
                CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id, attempt_no);
                """
            )
            # 兼容已由早期适配层创建的本地 DB；迁移只增列，不改旧数据。
            job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            attempt_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
            }
            migration_columns = {
                "worker_id",
                "claimed_at",
                "audio_sha256",
                "audio_size",
                "deduplicated_to",
                "codex_dispatched_at",
                "whisper_status",
                "whisper_error",
                "whisper_updated_at",
                "whisper_attempt",
                "whisper_retry_requested",
                "whisper_retry_generation",
                "whisper_worker_id",
                "whisper_claimed_at",
                "index_status",
                "index_error",
                "index_updated_at",
                "index_attempt",
                "meeting_id",
                "published_archive_dir",
                "published_manifest_path",
                "published_manifest_sha256",
                "published_at",
                "hotword_prompt_path",
                "hotword_prompt_sha256",
            }
            attempt_migration_columns = {
                "input_transcript_path",
                "input_transcript_sha256",
                "input_transcript_bytes",
                "source_srt_sha256",
                "minutes_plan_sha256",
                "minutes_protocol_version",
            }
            if (
                migration_columns - job_columns
                or attempt_migration_columns - attempt_columns
            ):
                # 多进程首启时串行取得写锁，再读一次快照，避免重复 ALTER。
                connection.execute("BEGIN IMMEDIATE")
                job_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
                attempt_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
                }
            if "worker_id" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN worker_id TEXT")
            if "claimed_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN claimed_at TEXT")
            if "audio_sha256" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN audio_sha256 TEXT")
            if "audio_size" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN audio_size INTEGER")
            if "deduplicated_to" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN deduplicated_to TEXT")
            if "codex_dispatched_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN codex_dispatched_at TEXT")
            if "whisper_status" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN whisper_status TEXT NOT NULL DEFAULT 'pending'"
                )
            if "whisper_error" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN whisper_error TEXT")
            if "whisper_updated_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN whisper_updated_at TEXT")
            if "whisper_attempt" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN whisper_attempt INTEGER NOT NULL DEFAULT 1"
                )
                connection.execute(
                    "UPDATE jobs SET whisper_attempt = current_attempt"
                )
            if "whisper_retry_requested" not in job_columns:
                connection.execute(
                    """
                    ALTER TABLE jobs ADD COLUMN whisper_retry_requested
                    INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "whisper_retry_generation" not in job_columns:
                connection.execute(
                    """
                    ALTER TABLE jobs ADD COLUMN whisper_retry_generation
                    INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "whisper_worker_id" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN whisper_worker_id TEXT")
            if "whisper_claimed_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN whisper_claimed_at TEXT")
            if "index_status" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN index_status TEXT NOT NULL DEFAULT 'absent'"
                )
            if "index_error" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN index_error TEXT")
            if "index_updated_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN index_updated_at TEXT")
            if "index_attempt" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN index_attempt INTEGER NOT NULL DEFAULT 1"
                )
                connection.execute("UPDATE jobs SET index_attempt = current_attempt")
            if "meeting_id" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN meeting_id TEXT")
            if "published_archive_dir" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN published_archive_dir TEXT"
                )
                connection.execute(
                    """
                    UPDATE jobs SET published_archive_dir = archive_dir
                    WHERE status = 'published' AND archive_dir IS NOT NULL
                    """
                )

            if "published_manifest_path" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN published_manifest_path TEXT"
                )
            if "published_manifest_sha256" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN published_manifest_sha256 TEXT"
                )
            if "published_at" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN published_at TEXT")
            if "hotword_prompt_path" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN hotword_prompt_path TEXT")
            if "hotword_prompt_sha256" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN hotword_prompt_sha256 TEXT")
            if "input_transcript_path" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN input_transcript_path TEXT"
                )
            if "input_transcript_sha256" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN input_transcript_sha256 TEXT"
                )
            if "input_transcript_bytes" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN input_transcript_bytes INTEGER"
                )
            if "source_srt_sha256" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN source_srt_sha256 TEXT"
                )
            if "minutes_plan_sha256" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE attempts ADD COLUMN minutes_plan_sha256 TEXT"
                )
            if "minutes_protocol_version" not in attempt_columns:
                connection.execute(
                    """
                    ALTER TABLE attempts ADD COLUMN minutes_protocol_version
                    INTEGER NOT NULL DEFAULT 1
                    """
                )
            needs_published_backfill = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE status = 'published' AND published_archive_dir IS NULL
                  AND archive_dir IS NOT NULL
                LIMIT 1
                """
            ).fetchone()
            if needs_published_backfill:
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE jobs SET published_archive_dir = archive_dir
                    WHERE status = 'published' AND published_archive_dir IS NULL
                      AND archive_dir IS NOT NULL
                    """
                )

    @staticmethod
    def _ensure_runtime_workers_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_workers (
                name TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                current_job_id TEXT,
                current_stage TEXT,
                pid INTEGER NOT NULL,
                start_token TEXT,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                last_error TEXT
            )
            """
        )

    def heartbeat_worker(
        self,
        name: str,
        *,
        mode: str,
        status: str = "running",
        current_job_id: str | None = None,
        current_stage: str | None = None,
        pid: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise RelayControlError("runtime worker 名称无效")
        if mode not in {"controlled", "legacy"}:
            raise RelayControlError("runtime worker 模式无效")
        if status not in {"running", "idle", "busy", "degraded", "stopped"}:
            raise RelayControlError("runtime worker 状态无效")
        process_id = int(pid or os.getpid())
        start_token = _process_start_token(process_id)
        if last_error and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", last_error):
            last_error = "WorkerError"
        now = _now()
        with self._connect() as connection:
            self._ensure_runtime_workers_table(connection)
            connection.execute(
                """
                INSERT INTO runtime_workers (
                    name, mode, status, current_job_id, current_stage, pid,
                    start_token, started_at, heartbeat_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    mode = excluded.mode,
                    status = excluded.status,
                    current_job_id = excluded.current_job_id,
                    current_stage = excluded.current_stage,
                    pid = excluded.pid,
                    start_token = excluded.start_token,
                    started_at = CASE
                        WHEN runtime_workers.pid = excluded.pid
                         AND runtime_workers.start_token IS excluded.start_token
                        THEN runtime_workers.started_at
                        ELSE excluded.started_at
                    END,
                    heartbeat_at = excluded.heartbeat_at,
                    last_error = excluded.last_error
                """,
                (
                    name,
                    mode,
                    status,
                    current_job_id,
                    current_stage,
                    process_id,
                    start_token,
                    now,
                    now,
                    last_error,
                ),
            )
        return {"name": name, "mode": mode, "status": status, "heartbeat_at": now}

    def stop_worker(self, name: str) -> None:
        now = _now()
        with self._connect() as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'runtime_workers'
                """
            ).fetchone()
            if exists is None:
                return
            connection.execute(
                """
                UPDATE runtime_workers
                SET status = 'stopped', current_job_id = NULL,
                    current_stage = NULL, heartbeat_at = ?
                WHERE name = ?
                """,
                (now, name),
            )

    def health(
        self,
        *,
        stale_seconds: float = 30,
        now: datetime | None = None,
        control_enabled: bool | None = None,
    ) -> dict[str, Any]:
        if stale_seconds <= 0:
            raise RelayControlError("stale_seconds 必须大于 0")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if control_enabled is None:
            control_enabled = os.getenv("MEETING_RELAY_CONTROL_ENABLED") == "1"
        mode = "controlled" if control_enabled else "legacy"
        rows: list[sqlite3.Row] = []
        counts = {
            "queued": 0,
            "active": 0,
            "failed": 0,
            "pending_archive_failures": 0,
            "held_test_fixtures": 0,
        }
        database_available = False
        error_type: str | None = None
        try:
            if not self.db_path.is_file():
                raise FileNotFoundError
            uri = f"file:{self.db_path.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.row_factory = sqlite3.Row
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "runtime_workers" in tables:
                    rows = connection.execute(
                        "SELECT * FROM runtime_workers ORDER BY name"
                    ).fetchall()
                if "jobs" in tables:
                    job_columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(jobs)"
                        ).fetchall()
                    }
                    pending_archive_expression = (
                        "SUM(CASE WHEN failure_stage = 'pending_archive' "
                        "THEN 1 ELSE 0 END)"
                        if "failure_stage" in job_columns
                        else "0"
                    )
                    counts_row = connection.execute(
                        f"""
                        SELECT
                            SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                            SUM(CASE WHEN status IN (
                                'stabilizing', 'transcribing',
                                'transcript_ready', 'minutes_generating'
                            ) THEN 1 ELSE 0 END) AS active,
                            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                            {pending_archive_expression} AS pending_archive_failures
                        FROM jobs
                        """
                    ).fetchone()
                    counts = {
                        "queued": int(counts_row["queued"] or 0),
                        "active": int(counts_row["active"] or 0),
                        "failed": int(counts_row["failed"] or 0),
                        "pending_archive_failures": int(
                            counts_row["pending_archive_failures"] or 0
                        ),
                        "held_test_fixtures": 0,
                    }
                    if "job_archive_policies" in tables:
                        held_row = connection.execute(
                            """
                            SELECT COUNT(*) AS count
                            FROM job_archive_policies AS policy
                            JOIN jobs ON jobs.job_id = policy.job_id
                            WHERE policy.policy = 'hidden_fixture'
                              AND jobs.status IN (
                                  'completed_unreviewed', 'draft_modified'
                              )
                            """
                        ).fetchone()
                        counts["held_test_fixtures"] = int(
                            held_row["count"] or 0
                        )
                database_available = True
        except (FileNotFoundError, OSError, sqlite3.Error) as error:
            error_type = type(error).__name__

        workers: list[dict[str, Any]] = []
        by_name: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                heartbeat_at = datetime.fromisoformat(row["heartbeat_at"])
                age_seconds = max(
                    0.0, (current - heartbeat_at.astimezone(timezone.utc)).total_seconds()
                )
            except (TypeError, ValueError):
                age_seconds = stale_seconds + 1
            token = row["start_token"]
            process_worker_id = f"worker-{row['pid']}" + (f"-{token}" if token else "")
            worker_id = f"{row['name']}-{row['pid']}" + (f"-{token}" if token else "")
            process_alive = _worker_process_is_alive(process_worker_id)
            fresh = (
                row["status"] != "stopped"
                and age_seconds <= stale_seconds
                and process_alive
            )
            item = {
                "name": row["name"],
                "worker_id": worker_id,
                "mode": row["mode"],
                "status": row["status"],
                "current_job_id": row["current_job_id"],
                "current_stage": row["current_stage"],
                "heartbeat_at": row["heartbeat_at"],
                "age_seconds": round(age_seconds, 3),
                "fresh": fresh,
                "last_error": row["last_error"],
            }
            workers.append(item)
            by_name[row["name"]] = item

        if not database_available:
            runtime_status = "unavailable"
        elif control_enabled:
            required = [by_name.get("watchdog"), by_name.get("control-worker")]
            if all(item is not None and item["fresh"] for item in required):
                runtime_status = (
                    "degraded"
                    if counts["pending_archive_failures"]
                    or any(item["status"] == "degraded" for item in required if item)
                    else "healthy"
                )
            else:
                runtime_status = "unavailable"
        elif not workers:
            runtime_status = "degraded"
        else:
            watchdog = by_name.get("watchdog")
            runtime_status = "degraded" if watchdog and watchdog["fresh"] else "unavailable"

        selected_worker = by_name.get("control-worker") if control_enabled else by_name.get("watchdog")
        if selected_worker is None:
            worker = {
                "state": "absent",
                "worker_id": None,
                "heartbeat_at": None,
                "heartbeat_age_seconds": None,
                "current_job_id": None,
                "current_stage": None,
            }
        else:
            if selected_worker["status"] == "stopped":
                worker_state = "stopped"
            elif not selected_worker["fresh"]:
                worker_state = "stale"
            elif selected_worker["status"] == "busy":
                worker_state = "busy"
            elif selected_worker["status"] == "degraded":
                worker_state = "degraded"
            else:
                worker_state = "idle"
            worker = {
                "state": worker_state,
                "worker_id": selected_worker["worker_id"],
                "heartbeat_at": selected_worker["heartbeat_at"],
                "heartbeat_age_seconds": selected_worker["age_seconds"],
                "current_job_id": selected_worker["current_job_id"],
                "current_stage": selected_worker["current_stage"],
                "last_error": selected_worker["last_error"],
            }

        result = {
            "status": runtime_status,
            "mode": mode,
            "checked_at": current.isoformat(timespec="milliseconds"),
            "stale_after_seconds": stale_seconds,
            "worker": worker,
            "counts": counts,
            "workers": workers,
        }
        if error_type:
            result["error_type"] = error_type
        return result

    def _job_row(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFoundError(f"任务不存在: {job_id}")
        return row

    @staticmethod
    def _archive_policy(connection: sqlite3.Connection, job_id: str) -> str:
        row = connection.execute(
            "SELECT policy FROM job_archive_policies WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return str(row["policy"]) if row is not None else "visible"

    def set_archive_policy(self, job_id: str, policy: str) -> dict[str, Any]:
        """显式标记合成夹具；产物目录内容无权改变归档策略。"""
        if policy not in {"visible", "hidden_fixture"}:
            raise RelayControlError(f"未知归档策略: {policy}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            current = self._archive_policy(connection, job_id)
            if current != policy:
                if policy == "hidden_fixture":
                    if row["status"] == "published":
                        raise InvalidTransitionError("已发布任务不能标记为合成夹具")
                    if row["archive_dir"]:
                        archive = Path(row["archive_dir"]).expanduser()
                        expected = self._expected_attempt_dir(
                            job_id, int(row["current_attempt"])
                        )
                        if archive.resolve() != expected.resolve():
                            raise InvalidTransitionError(
                                "只有尚未归档或仍在 hidden attempt 的任务可标记为合成夹具"
                            )
                    now = _now()
                    connection.execute(
                        """
                        INSERT INTO job_archive_policies (
                            job_id, policy, created_at, updated_at
                        ) VALUES (?, 'hidden_fixture', ?, ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            policy = excluded.policy,
                            updated_at = excluded.updated_at
                        """,
                        (job_id, now, now),
                    )
                else:
                    connection.execute(
                        "DELETE FROM job_archive_policies WHERE job_id = ?",
                        (job_id,),
                    )
                self._append_event(
                    connection,
                    job_id,
                    int(row["current_attempt"]),
                    "archive_policy_changed",
                    row["status"],
                    row["status"],
                    stage="pending_archive",
                    payload={"from": current, "to": policy},
                )
        return self.status(job_id)

    @staticmethod
    def _assert_claim_identity(
        row: sqlite3.Row,
        *,
        expected_attempt: int | None,
        expected_worker: str | None,
        action: str,
    ) -> None:
        if (
            expected_attempt is not None
            and int(row["current_attempt"]) != int(expected_attempt)
        ):
            raise InvalidTransitionError(f"{action}的 attempt 已变化")
        if expected_worker is not None and row["worker_id"] != expected_worker:
            raise InvalidTransitionError(f"{action}的 worker 已变化")

    def _append_event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        attempt_no: int,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                job_id, attempt_no, event_type, from_status, to_status,
                stage, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                attempt_no,
                event_type,
                from_status,
                to_status,
                stage,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )

    def _transition(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        new_status: str,
        *,
        event_type: str = "status_changed",
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        expected_attempt: int | None = None,
        expected_worker: str | None = None,
    ) -> None:
        if new_status not in ALL_STATES:
            raise InvalidTransitionError(f"未知状态: {new_status}")
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        row = self._job_row(connection, job_id)
        self._assert_claim_identity(
            row,
            expected_attempt=expected_attempt,
            expected_worker=expected_worker,
            action="状态转换",
        )
        old_status = row["status"]
        if new_status not in _ALLOWED_TRANSITIONS[old_status]:
            raise InvalidTransitionError(f"非法状态转换: {old_status} -> {new_status}")

        now = _now()
        updated = connection.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?, last_error = NULL, failure_stage = NULL
            WHERE job_id = ? AND current_attempt = ? AND status = ?
              AND (? IS NULL OR worker_id = ?)
            """,
            (
                new_status,
                now,
                job_id,
                row["current_attempt"],
                old_status,
                expected_worker,
                expected_worker,
            ),
        )
        if updated.rowcount != 1:
            raise InvalidTransitionError("状态转换 CAS 失败")
        attempt_updated = connection.execute(
            """UPDATE attempts SET status = ?
               WHERE job_id = ? AND attempt_no = ? AND status = ?""",
            (new_status, job_id, row["current_attempt"], old_status),
        )
        if attempt_updated.rowcount != 1:
            raise InvalidTransitionError("attempt 状态转换 CAS 失败")
        if new_status in {"completed_unreviewed", "published", "cancelled", "interrupted"}:
            connection.execute(
                """
                UPDATE attempts SET finished_at = ?
                WHERE job_id = ? AND attempt_no = ?
                """,
                (now, job_id, row["current_attempt"]),
            )
        self._append_event(
            connection,
            job_id,
            row["current_attempt"],
            event_type,
            old_status,
            new_status,
            stage=stage or new_status,
            payload=payload,
        )

    def enqueue(
        self,
        audio: str | Path,
        *,
        compute_hash: bool = True,
        requested_stage: str | None = None,
        transcript_path: str | Path | None = None,
        hotword_prompt_path: str | Path | None = None,
    ) -> str:
        minutes_only = requested_stage == "minutes_generating"
        if requested_stage not in {None, "minutes_generating"}:
            raise RelayControlError("enqueue --stage 仅支持 minutes_generating")
        if minutes_only != (transcript_path is not None):
            raise RelayControlError(
                "minutes_generating enqueue 必须同时提供 --transcript"
            )
        _validate_archive_root_for_write(self.archive_root)
        audio_path = Path(audio).expanduser().resolve()
        if not audio_path.is_file():
            raise RelayControlError(f"音频不存在或不是文件: {audio_path}")
        if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise RelayControlError(f"不支持的音频格式: {audio_path.suffix}")

        source_key = _source_key(audio_path, normalized_pcm=compute_hash)
        audio_sha256 = _sha256_file(audio_path) if compute_hash else None
        audio_size = audio_path.stat().st_size if compute_hash else None
        incoming_hotwords: tuple[bytes, str, int] | None = None
        if hotword_prompt_path is not None:
            incoming_hotwords = self._hotword_prompt_payload(hotword_prompt_path)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT job_id, audio_sha256, hotword_prompt_path,
                          hotword_prompt_sha256
                     FROM jobs WHERE source_key = ?""",
                (source_key,),
            ).fetchone()
            if existing:
                if incoming_hotwords is not None:
                    _payload, incoming_digest, incoming_count = incoming_hotwords
                    existing_path = existing["hotword_prompt_path"]
                    try:
                        if not existing_path:
                            raise RelayControlError("existing hotword missing")
                        _existing_payload, existing_digest, existing_count = (
                            self._hotword_prompt_payload(existing_path)
                        )
                    except RelayControlError:
                        existing_digest = None
                        existing_count = -1
                    if (
                        not existing_path
                        or existing["hotword_prompt_sha256"] != incoming_digest
                        or existing_digest != incoming_digest
                        or existing_count != incoming_count
                    ):
                        raise InvalidTransitionError(
                            f"音频已有任务 {existing['job_id']}，不能在幂等入队时更换热词"
                        )
                if minutes_only:
                    raise InvalidTransitionError(
                        f"音频已有任务 {existing['job_id']}，请使用 retry --transcript"
                    )
                if audio_sha256 and not existing["audio_sha256"]:
                    connection.execute(
                        """
                        UPDATE jobs SET audio_sha256 = ?, audio_size = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (audio_sha256, audio_size, _now(), existing["job_id"]),
                    )
                return str(existing["job_id"])

            job_id = "job-" + uuid.uuid4().hex[:16]
            now = _now()
            snapshot_path = None
            snapshot_size = None
            snapshot_sha256 = None
            hotword_snapshot = None
            hotword_sha256 = None
            hotword_count = 0
            if minutes_only and transcript_path is not None:
                snapshot_path, snapshot_size, snapshot_sha256 = (
                    self._snapshot_input_transcript(job_id, 1, transcript_path)
                )
            if hotword_prompt_path is not None:
                hotword_snapshot, hotword_sha256, hotword_count = (
                    self._snapshot_hotword_prompt(job_id, hotword_prompt_path)
                )
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, source_key, audio_path, status, current_attempt,
                    retry_stage, audio_sha256, audio_size,
                    hotword_prompt_path, hotword_prompt_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'discovered', 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_key,
                    str(audio_path),
                    "minutes_generating" if minutes_only else None,
                    audio_sha256,
                    audio_size,
                    str(hotword_snapshot) if hotword_snapshot else None,
                    hotword_sha256,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO attempts (
                    job_id, attempt_no, requested_stage, status,
                    input_transcript_path, input_transcript_sha256,
                    input_transcript_bytes, minutes_protocol_version, started_at
                ) VALUES (?, 1, ?, 'discovered', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "minutes_generating" if minutes_only else "discovered",
                    str(snapshot_path) if snapshot_path else None,
                    snapshot_sha256,
                    snapshot_size,
                    CURRENT_MINUTES_PROTOCOL_VERSION,
                    now,
                ),
            )
            self._append_event(
                connection,
                job_id,
                1,
                "status_changed",
                None,
                "discovered",
                stage="discovered",
                payload={
                    "audio_path": str(audio_path),
                    "source_key": source_key,
                    "requested_stage": (
                        "minutes_generating" if minutes_only else "discovered"
                    ),
                    "input_transcript_sha256": snapshot_sha256,
                    "hotwords_configured": hotword_snapshot is not None,
                    "hotword_prompt_sha256": hotword_sha256,
                    "hotword_count": hotword_count,
                },
            )
            self._transition(connection, job_id, "stabilizing")
            self._transition(connection, job_id, "queued")
        return job_id

    def record_source_audio(
        self,
        job_id: str,
        audio: str | Path,
        *,
        expected_attempt: int | None = None,
        expected_worker: str | None = None,
    ) -> dict[str, Any]:
        """文件稳定后记录真源哈希，覆盖创建事件期间可能读到的半文件。"""
        audio_path = Path(audio).expanduser().resolve()
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise RelayControlError(f"无法记录源音频哈希: {audio_path}")
        digest = _sha256_file(audio_path)
        size = audio_path.stat().st_size
        stable_source_key = _source_key(audio_path, normalized_pcm=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            self._assert_claim_identity(
                row,
                expected_attempt=expected_attempt,
                expected_worker=expected_worker,
                action="源音频回写",
            )
            if row["audio_sha256"] and row["audio_sha256"] != digest:
                now = _now()
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed', failure_stage = 'source_audio_validation',
                        last_error = 'stable source audio hash changed', updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
                connection.execute(
                    """
                    UPDATE attempts
                    SET status = 'failed', error = 'stable source audio hash changed',
                        finished_at = ?
                    WHERE job_id = ? AND attempt_no = ?
                    """,
                    (now, job_id, row["current_attempt"]),
                )
                self._append_event(
                    connection,
                    job_id,
                    row["current_attempt"],
                    "source_audio_changed",
                    row["status"],
                    "failed",
                    stage=row["status"],
                    payload={"expected_sha256": row["audio_sha256"]},
                )
            else:
                duplicate = connection.execute(
                    """
                    SELECT job_id FROM jobs
                    WHERE source_key = ? AND job_id != ?
                    ORDER BY created_at LIMIT 1
                    """,
                    (stable_source_key, job_id),
                ).fetchone()
                if duplicate is not None:
                    now = _now()
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'cancelled', deduplicated_to = ?,
                            audio_sha256 = ?, audio_size = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (duplicate["job_id"], digest, size, now, job_id),
                    )
                    connection.execute(
                        """
                        UPDATE attempts SET status = 'cancelled', finished_at = ?
                        WHERE job_id = ? AND attempt_no = ?
                        """,
                        (now, job_id, row["current_attempt"]),
                    )
                    self._append_event(
                        connection,
                        job_id,
                        row["current_attempt"],
                        "source_deduplicated",
                        row["status"],
                        "cancelled",
                        stage=row["status"],
                        payload={"canonical_job_id": duplicate["job_id"]},
                    )
                else:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET source_key = ?, audio_sha256 = COALESCE(audio_sha256, ?),
                            audio_size = COALESCE(audio_size, ?), updated_at = ?
                        WHERE job_id = ?
                        """,
                        (stable_source_key, digest, size, _now(), job_id),
                    )
                    self._append_event(
                        connection,
                        job_id,
                        row["current_attempt"],
                        "source_audio_hashed",
                        row["status"],
                        row["status"],
                        stage=row["status"],
                        payload={"bytes": size, "sha256": digest},
                    )
        return self.status(job_id)

    def record_minutes_plan_source(
        self,
        job_id: str,
        *,
        attempt_no: int,
        source_srt_sha256: str,
        minutes_plan_sha256: str,
        expected_worker: str | None = None,
    ) -> dict[str, Any]:
        """派发纪要前固化实际 SRT 与 plan 哈希，后续不信任归档副本。"""
        if re.fullmatch(r"[0-9a-f]{64}", source_srt_sha256) is None:
            raise RelayControlError("source_srt_sha256 无效")
        if re.fullmatch(r"[0-9a-f]{64}", minutes_plan_sha256) is None:
            raise RelayControlError("minutes_plan_sha256 无效")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            self._assert_claim_identity(
                row,
                expected_attempt=attempt_no,
                expected_worker=expected_worker,
                action="纪要来源回写",
            )
            if row["status"] not in {"transcript_ready", "minutes_generating"}:
                raise InvalidTransitionError(
                    f"纪要来源回写要求 transcript_ready/minutes_generating，当前为 {row['status']}"
                )
            attempt = connection.execute(
                """
                SELECT requested_stage, input_transcript_sha256,
                       source_srt_sha256, minutes_plan_sha256
                FROM attempts WHERE job_id = ? AND attempt_no = ?
                """,
                (job_id, attempt_no),
            ).fetchone()
            if attempt is None:
                raise InvalidTransitionError("纪要来源 attempt 不存在")
            if (
                attempt["requested_stage"] == "minutes_generating"
                and attempt["input_transcript_sha256"] != source_srt_sha256
            ):
                raise InvalidTransitionError("纪要来源与入队逐字稿快照不一致")
            existing = attempt["source_srt_sha256"]
            existing_plan = attempt["minutes_plan_sha256"]
            if existing is not None and existing != source_srt_sha256:
                raise InvalidTransitionError("纪要来源 SRT 哈希已固化，禁止覆盖")
            if existing_plan is not None and existing_plan != minutes_plan_sha256:
                raise InvalidTransitionError("纪要 plan 哈希已固化，禁止覆盖")
            connection.execute(
                """
                UPDATE attempts
                SET source_srt_sha256 = ?, minutes_plan_sha256 = ?
                WHERE job_id = ? AND attempt_no = ?
                  AND (source_srt_sha256 IS NULL OR source_srt_sha256 = ?)
                  AND (minutes_plan_sha256 IS NULL OR minutes_plan_sha256 = ?)
                """,
                (
                    source_srt_sha256,
                    minutes_plan_sha256,
                    job_id,
                    attempt_no,
                    source_srt_sha256,
                    minutes_plan_sha256,
                ),
            )
            if existing is None or existing_plan is None:
                self._append_event(
                    connection,
                    job_id,
                    attempt_no,
                    "minutes_plan_source_recorded",
                    row["status"],
                    row["status"],
                    stage="minutes_generating",
                    payload={
                        "source_srt_sha256": source_srt_sha256,
                        "minutes_plan_sha256": minutes_plan_sha256,
                    },
                )
        return self.status(job_id)

    def prepare_attempt_draft(
        self,
        job_id: str,
        *,
        attempt_no: int,
        expected_worker: str | None = None,
    ) -> Path:
        """在控制层的可信归档根内安全准备当前 attempt 目录。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            self._assert_claim_identity(
                row,
                expected_attempt=attempt_no,
                expected_worker=expected_worker,
                action="准备 attempt 草稿目录",
            )
            attempt = connection.execute(
                """
                SELECT 1 FROM attempts
                WHERE job_id = ? AND attempt_no = ?
                """,
                (job_id, attempt_no),
            ).fetchone()
            if attempt is None:
                raise InvalidTransitionError("attempt 草稿目录对应记录不存在")
            return self._attempt_draft_dir(job_id, attempt_no)

    def record_stage(
        self,
        job_id: str,
        status: str,
        *,
        expected_attempt: int | None = None,
        expected_worker: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            self._assert_claim_identity(
                row,
                expected_attempt=expected_attempt,
                expected_worker=expected_worker,
                action="阶段回写",
            )
            if (
                row["status"] == "queued"
                and row["retry_stage"] == status
                and int(row["current_attempt"]) > 1
            ):
                now = _now()
                updated = connection.execute(
                    """
                    UPDATE jobs SET status = ?, updated_at = ?
                    WHERE job_id = ? AND current_attempt = ? AND status = 'queued'
                      AND (? IS NULL OR worker_id = ?)
                    """,
                    (
                        status,
                        now,
                        job_id,
                        row["current_attempt"],
                        expected_worker,
                        expected_worker,
                    ),
                )
                if updated.rowcount != 1:
                    raise InvalidTransitionError("重试阶段回写 CAS 失败")
                attempt_updated = connection.execute(
                    """
                    UPDATE attempts SET status = ?
                    WHERE job_id = ? AND attempt_no = ? AND status = 'queued'
                    """,
                    (status, job_id, row["current_attempt"]),
                )
                if attempt_updated.rowcount != 1:
                    raise InvalidTransitionError("重试 attempt 阶段回写 CAS 失败")
                self._append_event(
                    connection,
                    job_id,
                    row["current_attempt"],
                    "retry_stage_started",
                    "queued",
                    status,
                    stage=status,
                )
            else:
                self._transition(
                    connection,
                    job_id,
                    status,
                    expected_attempt=expected_attempt,
                    expected_worker=expected_worker,
                )
        return self.status(job_id)

    def record_substate(
        self,
        job_id: str,
        name: str,
        status: str,
        error: str | None = None,
        *,
        attempt_no: int | None = None,
    ) -> dict[str, Any]:
        if name not in SUBSTATE_NAMES:
            raise RelayControlError(f"未知子状态: {name}")
        if status not in SUBSTATE_STATUSES:
            raise RelayControlError(f"未知子状态值: {status}")
        status_column, error_column, updated_column, attempt_column = SUBSTATE_COLUMNS[name]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            target_attempt = (
                int(row["current_attempt"]) if attempt_no is None else int(attempt_no)
            )
            if target_attempt < 1:
                raise RelayControlError("子状态 attempt 必须大于等于 1")
            if target_attempt != int(row["current_attempt"]):
                raise InvalidTransitionError(
                    f"迟到的 {name} 子状态: callback={target_attempt}, "
                    f"current={row['current_attempt']}"
                )
            old_status = row[status_column]
            now = _now()
            updated = connection.execute(
                f"""
                UPDATE jobs
                SET {status_column} = ?, {error_column} = ?,
                    {updated_column} = ?, {attempt_column} = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                """,
                (status, error, now, target_attempt, now, job_id, target_attempt),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError(f"{name} 子状态写入时 attempt 已变化")
            if name == "whisper" and status != "pending":
                connection.execute(
                    """
                    UPDATE jobs
                    SET whisper_retry_requested = 0, whisper_worker_id = NULL,
                        whisper_claimed_at = NULL
                    WHERE job_id = ? AND current_attempt = ?
                    """,
                    (job_id, target_attempt),
                )
            self._append_event(
                connection,
                job_id,
                target_attempt,
                f"{name}_substate_changed",
                old_status,
                status,
                stage=name,
                payload={"error": error} if error else {},
            )
        return self.status(job_id)

    def retry_substate(self, job_id: str, name: str) -> dict[str, Any]:
        if name not in SUBSTATE_NAMES:
            raise RelayControlError(f"未知子状态: {name}")
        status_column, error_column, updated_column, attempt_column = SUBSTATE_COLUMNS[name]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if row[status_column] != "failed":
                raise InvalidTransitionError(
                    f"{name} 当前为 {row[status_column]}，只有 failed 可重试"
                )
            now = _now()
            if name == "whisper":
                if row["archive_dir"]:
                    try:
                        self._managed_unreviewed_archive(row)
                    except RelayControlError as exc:
                        raise InvalidTransitionError(
                            "Whisper 单独重试只允许写入未发布 attempt；"
                            "已发布会议请使用重新转写"
                        ) from exc
                connection.execute(
                    f"""
                    UPDATE jobs
                    SET {status_column} = 'pending', {error_column} = NULL,
                        {updated_column} = ?, {attempt_column} = ?,
                        whisper_retry_requested = 1,
                        whisper_retry_generation = whisper_retry_generation + 1,
                        whisper_worker_id = NULL, whisper_claimed_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND current_attempt = ?
                    """,
                    (
                        now,
                        row["current_attempt"],
                        now,
                        job_id,
                        row["current_attempt"],
                    ),
                )
                retry_generation = int(row["whisper_retry_generation"]) + 1
            else:
                connection.execute(
                    f"""
                    UPDATE jobs
                    SET {status_column} = 'pending', {error_column} = NULL,
                        {updated_column} = ?, {attempt_column} = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, row["current_attempt"], now, job_id),
                )
                retry_generation = None
            self._append_event(
                connection,
                job_id,
                row["current_attempt"],
                f"{name}_retry_requested",
                "failed",
                "pending",
                stage=name,
                payload=(
                    {"generation": retry_generation}
                    if retry_generation is not None
                    else {}
                ),
            )
        return self.status(job_id)

    def claim_whisper_retry(
        self,
        worker_id: str | None = None,
        *,
        job_id: str | None = None,
    ) -> dict[str, Any] | None:
        """原子领取一个已显式请求的 Whisper-only 重试。"""
        token = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            query = """
                SELECT * FROM jobs
                WHERE whisper_retry_requested = 1
                  AND whisper_status = 'pending'
                  AND whisper_attempt = current_attempt
                  AND archive_dir IS NOT NULL
                  AND status IN ('completed_unreviewed', 'draft_modified')
            """
            parameters: list[Any] = []
            if job_id is not None:
                query += " AND job_id = ?"
                parameters.append(job_id)
            query += " ORDER BY whisper_updated_at, updated_at, job_id LIMIT 1"
            row = connection.execute(query, parameters).fetchone()
            if row is None:
                return None

            canonical_target = self._managed_unreviewed_archive(row)

            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET whisper_status = 'running', whisper_retry_requested = 0,
                    whisper_worker_id = ?, whisper_claimed_at = ?,
                    whisper_updated_at = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND whisper_attempt = ? AND whisper_retry_generation = ?
                  AND whisper_status = 'pending' AND whisper_retry_requested = 1
                """,
                (
                    token,
                    now,
                    now,
                    now,
                    row["job_id"],
                    row["current_attempt"],
                    row["whisper_attempt"],
                    row["whisper_retry_generation"],
                ),
            )
            if updated.rowcount != 1:
                return None
            self._append_event(
                connection,
                row["job_id"],
                row["current_attempt"],
                "whisper_retry_claimed",
                "pending",
                "running",
                stage="whisper",
                payload={
                    "generation": row["whisper_retry_generation"],
                    "worker_id": token,
                },
            )
            return {
                "job_id": row["job_id"],
                "attempt": int(row["current_attempt"]),
                "generation": int(row["whisper_retry_generation"]),
                "status": "running",
                "worker_id": token,
                "audio_path": row["audio_path"],
                "audio_sha256": row["audio_sha256"],
                "source_archive_dir": row["archive_dir"],
                "published_archive_dir": row["published_archive_dir"],
                "target_archive_dir": str(canonical_target),
                "hotword_prompt_path": row["hotword_prompt_path"],
                "hotword_prompt_sha256": row["hotword_prompt_sha256"],
                "claimed_at": now,
            }

    def finish_whisper_retry(
        self,
        job_id: str,
        *,
        attempt_no: int,
        generation: int,
        worker_id: str,
        success: bool,
        error: str | None = None,
        artifact_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        with self._archive_lock():
            return self._finish_whisper_retry_locked(
                job_id,
                attempt_no=attempt_no,
                generation=generation,
                worker_id=worker_id,
                success=success,
                error=error,
                artifact_dir=artifact_dir,
            )

    def _finish_whisper_retry_locked(
        self,
        job_id: str,
        *,
        attempt_no: int,
        generation: int,
        worker_id: str,
        success: bool,
        error: str | None = None,
        artifact_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """校验并原子安装 Whisper 产物，再以 generation/worker 做 CAS 收口。"""
        target_status = "ready" if success else "failed"
        safe_error = None if success else (error or "whisper_retry_failed")[:256]
        connection = self._connect()
        backup: Path | None = None
        installed: Path | None = None
        committed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if (
                int(row["current_attempt"]) != attempt_no
                or int(row["whisper_attempt"]) != attempt_no
                or int(row["whisper_retry_generation"]) != generation
                or row["whisper_status"] != "running"
                or row["whisper_worker_id"] != worker_id
            ):
                raise InvalidTransitionError(
                    "Whisper 重试回执与当前 attempt/generation/worker 不一致"
                )

            if success:
                if artifact_dir is None:
                    raise ArtifactValidationError(
                        ArchiveValidationReport(
                            Path("."), ["whisper_retry_artifact_dir"], []
                        )
                    )
                staging_root = Path(
                    os.path.abspath(str(Path(artifact_dir).expanduser()))
                )
                installed, backup = self._install_whisper_ref(
                    row,
                    staging_root,
                    source_boundary=self.archive_root,
                )

            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET whisper_status = ?, whisper_error = ?,
                    whisper_retry_requested = 0, whisper_worker_id = NULL,
                    whisper_claimed_at = NULL, whisper_updated_at = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ? AND whisper_attempt = ?
                  AND whisper_retry_generation = ? AND whisper_status = 'running'
                  AND whisper_worker_id = ?
                """,
                (
                    target_status,
                    safe_error,
                    now,
                    now,
                    job_id,
                    attempt_no,
                    attempt_no,
                    generation,
                    worker_id,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError(
                    "Whisper 重试回执与当前 attempt/generation/worker 不一致"
                )
            self._append_event(
                connection,
                job_id,
                attempt_no,
                "whisper_retry_completed" if success else "whisper_retry_failed",
                row["whisper_status"],
                target_status,
                stage="whisper",
                payload={
                    "generation": generation,
                    **({"error": safe_error} if safe_error else {}),
                },
            )
            connection.commit()
            committed = True
            self._cleanup_whisper_backup(backup)
        except Exception:
            if not committed:
                connection.rollback()
                self._rollback_whisper_install(installed, backup)
            raise
        finally:
            connection.close()
        return self.status(job_id)

    def claim_next(self, worker_id: str | None = None) -> dict[str, Any] | None:
        """原子领取一个 queued attempt，并跳到它要求的起始阶段。"""
        token = worker_id or f"{make_worker_id('worker')}-{uuid.uuid4().hex[:8]}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE status IN ('stabilizing', 'transcribing',
                                 'transcript_ready', 'minutes_generating')
                LIMIT 1
                """
            ).fetchone()
            if active is not None:
                connection.commit()
                return None
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued' AND stop_after_stage = 0
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            start_stage = row["retry_stage"] or "transcribing"
            if start_stage not in RETRYABLE_STAGES:
                start_stage = "transcribing"
            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, worker_id = ?, claimed_at = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND status = 'queued' AND stop_after_stage = 0
                  AND worker_id IS NULL
                """,
                (
                    start_stage,
                    token,
                    now,
                    now,
                    row["job_id"],
                    row["current_attempt"],
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE attempts SET status = ?
                WHERE job_id = ? AND attempt_no = ?
                """,
                (start_stage, row["job_id"], row["current_attempt"]),
            )
            self._append_event(
                connection,
                row["job_id"],
                row["current_attempt"],
                "attempt_claimed",
                "queued",
                start_stage,
                stage=start_stage,
                payload={"worker_id": token},
            )
            attempt_row = connection.execute(
                """
                SELECT input_transcript_path, input_transcript_sha256,
                       input_transcript_bytes, source_srt_sha256,
                       minutes_plan_sha256,
                       minutes_protocol_version
                FROM attempts WHERE job_id = ? AND attempt_no = ?
                """,
                (row["job_id"], row["current_attempt"]),
            ).fetchone()
            connection.commit()
            return {
                "job_id": row["job_id"],
                "audio_path": row["audio_path"],
                "audio_sha256": row["audio_sha256"],
                "meeting_id": row["meeting_id"],
                "source_archive_dir": row["archive_dir"],
                "published_archive_dir": row["published_archive_dir"],
                "hotword_prompt_path": row["hotword_prompt_path"],
                "hotword_prompt_sha256": row["hotword_prompt_sha256"],
                "input_transcript_path": attempt_row["input_transcript_path"],
                "input_transcript_sha256": attempt_row["input_transcript_sha256"],
                "input_transcript_bytes": attempt_row["input_transcript_bytes"],
                "source_srt_sha256": attempt_row["source_srt_sha256"],
                "minutes_plan_sha256": attempt_row["minutes_plan_sha256"],
                "minutes_protocol_version": int(
                    attempt_row["minutes_protocol_version"]
                ),
                "attempt": row["current_attempt"],
                "start_stage": start_stage,
                "worker_id": token,
                "claimed_at": now,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_orphaned_claims(self) -> int:
        """watchdog 重启时把上个进程领取但未收口的 attempt 标为 interrupted。"""
        active_claimed_states = {
            "stabilizing",
            "transcribing",
            "transcript_ready",
            "minutes_generating",
        }
        recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE worker_id IS NOT NULL
                  AND status IN ('stabilizing', 'transcribing',
                                 'transcript_ready', 'minutes_generating')
                ORDER BY created_at
                """
            ).fetchall()
            for row in rows:
                if row["status"] not in active_claimed_states:
                    continue
                # minutes_generating 已移交给纪要 Agent，由 handoff lease 专门收口；
                # 避免 watchdog 重启瞬间把仍在执行的 Agent 错判为 orphan。
                if row["status"] == "minutes_generating" and row["codex_dispatched_at"]:
                    continue
                if _worker_process_is_alive(row["worker_id"]):
                    continue
                now = _now()
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'interrupted', worker_id = NULL, claimed_at = NULL,
                        stop_after_stage = 0, updated_at = ?
                    WHERE job_id = ? AND current_attempt = ? AND status = ?
                      AND worker_id = ?
                    """,
                    (
                        now,
                        row["job_id"],
                        row["current_attempt"],
                        row["status"],
                        row["worker_id"],
                    ),
                )
                if updated.rowcount != 1:
                    continue
                attempt_updated = connection.execute(
                    """
                    UPDATE attempts SET status = 'interrupted', finished_at = ?
                    WHERE job_id = ? AND attempt_no = ? AND status = ?
                    """,
                    (now, row["job_id"], row["current_attempt"], row["status"]),
                )
                if attempt_updated.rowcount != 1:
                    raise InvalidTransitionError("孤儿 attempt 恢复 CAS 失败")
                self._append_event(
                    connection,
                    row["job_id"],
                    row["current_attempt"],
                    "orphaned_claim_recovered",
                    row["status"],
                    "interrupted",
                    stage=row["status"],
                    payload={"previous_worker_id": row["worker_id"]},
                )
                recovered += 1
        return recovered

    def record_codex_dispatched(
        self,
        job_id: str,
        *,
        expected_attempt: int | None = None,
        expected_worker: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            self._assert_claim_identity(
                row,
                expected_attempt=expected_attempt,
                expected_worker=expected_worker,
                action="纪要 Agent 派发回写",
            )
            if row["status"] != "minutes_generating":
                raise InvalidTransitionError(
                    f"只有 minutes_generating 可记录 Agent handoff，当前为 {row['status']}"
                )
            dispatched_at = _now()
            updated = connection.execute(
                """
                UPDATE jobs SET codex_dispatched_at = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND (? IS NULL OR worker_id = ?)
                """,
                (
                    dispatched_at,
                    dispatched_at,
                    job_id,
                    row["current_attempt"],
                    expected_worker,
                    expected_worker,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("纪要 Agent 派发回写 CAS 失败")
            self._append_event(
                connection,
                job_id,
                row["current_attempt"],
                "codex_dispatched",
                "minutes_generating",
                "minutes_generating",
                stage="minutes_generating",
                payload={"dispatched_at": dispatched_at},
            )
        return self.status(job_id)

    def reconcile_codex_handoffs(self, grace_seconds: float = 30) -> int:
        """pane 已回到 shell 后，收口超过宽限期仍无回执的纪要 Agent attempt。"""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=max(0.0, float(grace_seconds))
        )
        cutoff_text = cutoff.isoformat(timespec="milliseconds")
        reconciled = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'minutes_generating'
                  AND codex_dispatched_at IS NOT NULL
                  AND codex_dispatched_at <= ?
                ORDER BY codex_dispatched_at
                """,
                (cutoff_text,),
            ).fetchall()
            for row in rows:
                now = _now()
                stopped = bool(row["stop_after_stage"])
                target = "interrupted" if stopped else "failed"
                failure_stage = None if stopped else "codex_callback"
                error = None if stopped else "Minutes agent exited without completion callback"
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, failure_stage = ?, last_error = ?,
                        stop_after_stage = 0, worker_id = NULL, claimed_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND current_attempt = ?
                      AND status = 'minutes_generating'
                      AND codex_dispatched_at = ? AND worker_id IS ?
                    """,
                    (
                        target,
                        failure_stage,
                        error,
                        now,
                        row["job_id"],
                        row["current_attempt"],
                        row["codex_dispatched_at"],
                        row["worker_id"],
                    ),
                )
                if updated.rowcount != 1:
                    continue
                attempt_updated = connection.execute(
                    """
                    UPDATE attempts
                    SET status = ?, error = ?, finished_at = ?
                    WHERE job_id = ? AND attempt_no = ?
                      AND status = 'minutes_generating'
                    """,
                    (
                        target,
                        error,
                        now,
                        row["job_id"],
                        row["current_attempt"],
                    ),
                )
                if attempt_updated.rowcount != 1:
                    raise InvalidTransitionError("纪要 Agent handoff attempt 对账 CAS 失败")
                self._append_event(
                    connection,
                    row["job_id"],
                    row["current_attempt"],
                    "codex_callback_missing",
                    "minutes_generating",
                    target,
                    stage="minutes_generating",
                    payload={"dispatched_at": row["codex_dispatched_at"]},
                )
                reconciled += 1
        return reconciled

    def fail(
        self,
        job_id: str,
        stage: str,
        error: str,
        *,
        expected_attempt: int | None = None,
        expected_status: str | None = None,
        expected_worker: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            old_status = row["status"]
            self._assert_claim_identity(
                row,
                expected_attempt=expected_attempt,
                expected_worker=expected_worker,
                action="失败回写",
            )
            if expected_status is not None and old_status != expected_status:
                raise InvalidTransitionError("失败回写的任务状态已变化")
            if old_status == "published":
                raise InvalidTransitionError("已发布任务不能标记失败")
            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', last_error = ?, failure_stage = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ? AND status = ?
                  AND (? IS NULL OR worker_id = ?)
                """,
                (
                    error,
                    stage,
                    now,
                    job_id,
                    row["current_attempt"],
                    old_status,
                    expected_worker,
                    expected_worker,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("失败状态 CAS 失败")
            attempt_updated = connection.execute(
                """
                UPDATE attempts
                SET status = 'failed', error = ?, finished_at = ?
                WHERE job_id = ? AND attempt_no = ? AND status = ?
                """,
                (error, now, job_id, row["current_attempt"], old_status),
            )
            if attempt_updated.rowcount != 1:
                raise InvalidTransitionError("attempt 失败状态 CAS 失败")
            self._append_event(
                connection,
                job_id,
                row["current_attempt"],
                "stage_failed",
                old_status,
                "failed",
                stage=stage,
                payload={"error": error},
            )
        return self.status(job_id)

    def retry(
        self,
        job_id: str,
        stage: str,
        transcript_path: str | Path | None = None,
        hotword_prompt_path: str | Path | None = None,
    ) -> int:
        if stage not in RETRYABLE_STAGES:
            raise RelayControlError(
                f"不可重试的阶段: {stage}；允许值: {', '.join(sorted(RETRYABLE_STAGES))}"
            )
        if stage == "minutes_generating" and transcript_path is None:
            raise RelayControlError(
                "minutes_generating retry 必须提供 --transcript 快照"
            )
        if stage != "minutes_generating" and transcript_path is not None:
            raise RelayControlError("--transcript 仅适用于 minutes_generating retry")
        _validate_archive_root_for_write(self.archive_root)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if row["deduplicated_to"]:
                raise InvalidTransitionError(
                    f"任务已去重到 {row['deduplicated_to']}，请重试规范任务"
                )
            if row["status"] not in RETRYABLE_JOB_STATES:
                raise InvalidTransitionError(
                    f"当前状态 {row['status']} 不允许重试；需为 "
                    "failed/cancelled/interrupted/completed_unreviewed/"
                    "draft_modified/published"
                )
            attempt_no = int(row["current_attempt"]) + 1
            now = _now()
            snapshot_path = None
            snapshot_size = None
            snapshot_sha256 = None
            replacement_hotword_path = None
            replacement_hotword_sha256 = None
            replacement_hotword_count = 0
            if transcript_path is not None:
                snapshot_path, snapshot_size, snapshot_sha256 = (
                    self._snapshot_input_transcript(job_id, attempt_no, transcript_path)
                )
            if hotword_prompt_path is not None:
                (
                    replacement_hotword_path,
                    replacement_hotword_sha256,
                    replacement_hotword_count,
                ) = self._snapshot_hotword_prompt(
                    job_id,
                    hotword_prompt_path,
                    snapshot_name=f"{job_id}-attempt-{attempt_no}",
                )
            published_snapshot = None
            if row["published_manifest_path"]:
                published_snapshot = {
                    "archive_dir": row["published_archive_dir"],
                    "manifest_path": row["published_manifest_path"],
                    "manifest_sha256": row["published_manifest_sha256"],
                    "meeting_id": row["meeting_id"],
                    "published_at": row["published_at"],
                }
            connection.execute(
                """
                INSERT INTO attempts (
                    job_id, attempt_no, requested_stage, status,
                    input_transcript_path, input_transcript_sha256,
                    input_transcript_bytes, minutes_protocol_version, started_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    attempt_no,
                    stage,
                    str(snapshot_path) if snapshot_path else None,
                    snapshot_sha256,
                    snapshot_size,
                    CURRENT_MINUTES_PROTOCOL_VERSION,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', current_attempt = ?, retry_stage = ?,
                    stop_after_stage = 0, last_error = NULL, failure_stage = NULL,
                    worker_id = NULL, claimed_at = NULL, codex_dispatched_at = NULL,
                    hotword_prompt_path = COALESCE(?, hotword_prompt_path),
                    hotword_prompt_sha256 = COALESCE(?, hotword_prompt_sha256),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    attempt_no,
                    stage,
                    str(replacement_hotword_path) if replacement_hotword_path else None,
                    replacement_hotword_sha256,
                    now,
                    job_id,
                ),
            )
            if stage in {"stabilizing", "transcribing"}:
                connection.execute(
                    """
                    UPDATE jobs
                    SET whisper_status = 'pending', whisper_error = NULL,
                        whisper_updated_at = ?, whisper_attempt = ?,
                        whisper_retry_requested = 0,
                        whisper_retry_generation = whisper_retry_generation + 1,
                        whisper_worker_id = NULL, whisper_claimed_at = NULL,
                        index_status = 'pending', index_error = NULL,
                        index_updated_at = ?, index_attempt = ?
                    WHERE job_id = ? AND current_attempt = ?
                    """,
                    (now, attempt_no, now, attempt_no, job_id, attempt_no),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET whisper_attempt = ?, index_attempt = ?,
                        whisper_retry_requested = 0,
                        whisper_retry_generation = whisper_retry_generation + 1,
                        whisper_worker_id = NULL, whisper_claimed_at = NULL,
                        whisper_status = CASE
                            WHEN whisper_status = 'running' THEN 'pending'
                            ELSE whisper_status
                        END
                    WHERE job_id = ? AND current_attempt = ?
                    """,
                    (attempt_no, attempt_no, job_id, attempt_no),
                )
            self._append_event(
                connection,
                job_id,
                attempt_no,
                "retry_requested",
                row["status"],
                "queued",
                stage=stage,
                payload={
                    "previous_attempt": row["current_attempt"],
                    "preserved_published_snapshot": published_snapshot,
                    "input_transcript_sha256": snapshot_sha256,
                    "hotwords_replaced": replacement_hotword_path is not None,
                    "hotword_prompt_sha256": replacement_hotword_sha256,
                    "hotword_count": replacement_hotword_count,
                },
            )
        return attempt_no

    def stop_after_stage(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if row["status"] in {
                "published",
                "completed_unreviewed",
                "draft_modified",
                "failed",
                "cancelled",
                "interrupted",
            }:
                raise InvalidTransitionError(
                    f"当前状态 {row['status']} 不能请求阶段后停止"
                )
            now = _now()
            if row["status"] == "queued":
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'interrupted', stop_after_stage = 0, updated_at = ?
                    WHERE job_id = ? AND current_attempt = ?
                      AND status = 'queued' AND worker_id IS NULL
                    """,
                    (now, job_id, row["current_attempt"]),
                )
                if updated.rowcount != 1:
                    raise InvalidTransitionError("停止 queued 任务 CAS 失败")
                attempt_updated = connection.execute(
                    """
                    UPDATE attempts SET status = 'interrupted', finished_at = ?
                    WHERE job_id = ? AND attempt_no = ? AND status = 'queued'
                    """,
                    (now, job_id, row["current_attempt"]),
                )
                if attempt_updated.rowcount != 1:
                    raise InvalidTransitionError("停止 queued attempt CAS 失败")
                self._append_event(
                    connection,
                    job_id,
                    row["current_attempt"],
                    "stopped_after_stage",
                    "queued",
                    "interrupted",
                    stage="queued",
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE jobs SET stop_after_stage = 1, updated_at = ?
                    WHERE job_id = ? AND current_attempt = ? AND status = ?
                    """,
                    (now, job_id, row["current_attempt"], row["status"]),
                )
                if updated.rowcount != 1:
                    raise InvalidTransitionError("阶段后停止请求 CAS 失败")
                self._append_event(
                    connection,
                    job_id,
                    row["current_attempt"],
                    "stop_after_stage_requested",
                    row["status"],
                    row["status"],
                    stage=row["status"],
                )
        return self.status(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """取消尚未运行的 attempt；运行中任务必须使用 stop-after-stage。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if row["status"] != "queued":
                raise InvalidTransitionError(
                    f"只有 queued 任务可取消，当前为 {row['status']}"
                )
            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', stop_after_stage = 0, updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND status = 'queued' AND worker_id IS NULL
                """,
                (now, job_id, row["current_attempt"]),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("取消 queued 任务 CAS 失败")
            attempt_updated = connection.execute(
                """
                UPDATE attempts SET status = 'cancelled', finished_at = ?
                WHERE job_id = ? AND attempt_no = ? AND status = 'queued'
                """,
                (now, job_id, row["current_attempt"]),
            )
            if attempt_updated.rowcount != 1:
                raise InvalidTransitionError("取消 queued attempt CAS 失败")
            self._append_event(
                connection,
                job_id,
                row["current_attempt"],
                "cancelled",
                "queued",
                "cancelled",
                stage="queued",
            )
        return self.status(job_id)

    def should_stop_after_stage(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = self._job_row(connection, job_id)
            return bool(row["stop_after_stage"])

    def interrupt_if_stop_requested(
        self,
        job_id: str,
        completed_stage: str,
        *,
        expected_attempt: int | None = None,
        expected_worker: str | None = None,
    ) -> bool:
        """产品语义：保留已完成的当前阶段产物，但任务仍以 interrupted 收口。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            self._assert_claim_identity(
                row,
                expected_attempt=expected_attempt,
                expected_worker=expected_worker,
                action="阶段后停止",
            )
            if not row["stop_after_stage"]:
                return False
            if row["status"] in {"published", "failed", "cancelled", "interrupted"}:
                return False
            old_status = row["status"]
            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', stop_after_stage = 0, updated_at = ?
                WHERE job_id = ? AND current_attempt = ? AND status = ?
                  AND stop_after_stage = 1
                  AND (? IS NULL OR worker_id = ?)
                """,
                (
                    now,
                    job_id,
                    row["current_attempt"],
                    old_status,
                    expected_worker,
                    expected_worker,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("阶段后停止 CAS 失败")
            attempt_updated = connection.execute(
                """
                UPDATE attempts SET status = 'interrupted', finished_at = ?
                WHERE job_id = ? AND attempt_no = ? AND status = ?
                """,
                (now, job_id, row["current_attempt"], old_status),
            )
            if attempt_updated.rowcount != 1:
                raise InvalidTransitionError("阶段后停止 attempt CAS 失败")
            self._append_event(
                connection,
                job_id,
                row["current_attempt"],
                "stopped_after_stage",
                old_status,
                "interrupted",
                stage=completed_stage,
            )
        return True

    def validate_archive(
        self,
        archive_dir: str | Path,
        *,
        job_id: str | None = None,
        attempt_no: int | None = None,
        requested_stage: str | None = None,
        input_transcript_sha256: str | None = None,
        source_srt_sha256: str | None = None,
        minutes_plan_sha256: str | None = None,
        minutes_protocol_version: int = 1,
    ) -> ArchiveValidationReport:
        # 保留词法路径，不对 attempt 目录 resolve；否则 symlink 到正式目录会
        # 让“实际路径相等”掩盖越界写入。
        root = Path(os.path.abspath(str(Path(archive_dir).expanduser())))
        if self.archive_root.is_symlink() or not self.archive_root.is_dir():
            return ArchiveValidationReport(root, ["archive_root"], [])
        if not root.is_dir():
            return ArchiveValidationReport(root, ["archive_dir"], [])
        try:
            relative_root = root.relative_to(self.archive_root)
        except ValueError:
            return ArchiveValidationReport(root, ["archive_outside_root"], [])

        archive_has_symlink = False
        cursor = self.archive_root
        for part in relative_root.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                archive_has_symlink = True
                break

        job = None
        expected_attempt = attempt_no
        if job_id is not None:
            with self._connect() as connection:
                job = connection.execute(
                    """
                    SELECT current_attempt, audio_sha256
                    FROM jobs WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
            if job is None:
                return ArchiveValidationReport(root, ["manifest_job_id"], [])
            if expected_attempt is None:
                expected_attempt = int(job["current_attempt"])
            with self._connect() as connection:
                attempt = connection.execute(
                    """
                    SELECT requested_stage, input_transcript_sha256,
                           source_srt_sha256, minutes_plan_sha256,
                           minutes_protocol_version
                    FROM attempts WHERE job_id = ? AND attempt_no = ?
                    """,
                    (job_id, expected_attempt),
                ).fetchone()
            if attempt is not None:
                if requested_stage is None:
                    requested_stage = attempt["requested_stage"]
                if input_transcript_sha256 is None:
                    input_transcript_sha256 = attempt["input_transcript_sha256"]
                if source_srt_sha256 is None:
                    source_srt_sha256 = attempt["source_srt_sha256"]
                if minutes_plan_sha256 is None:
                    minutes_plan_sha256 = attempt["minutes_plan_sha256"]
                if minutes_protocol_version == 1:
                    minutes_protocol_version = int(
                        attempt["minutes_protocol_version"]
                    )

        path_errors: list[str] = []
        if archive_has_symlink:
            path_errors.append("archive_symlink")
        if job_id is not None and expected_attempt is not None:
            expected_root = (
                self.archive_root
                / ".workbench-drafts"
                / job_id
                / f"attempt-{expected_attempt}"
            )
            if root != expected_root:
                path_errors.append("archive_not_current_attempt_draft")

        main_entries: list[Path] = []
        for top_level in root.iterdir():
            if top_level.name == "whisper-ref":
                continue
            main_entries.append(top_level)
            if top_level.is_dir() and not top_level.is_symlink():
                main_entries.extend(top_level.rglob("*"))
        optional_entries: list[Path] = []
        whisper_dir = root / "whisper-ref"
        try:
            if whisper_dir.is_symlink():
                optional_entries.append(whisper_dir)
            elif whisper_dir.is_dir():
                optional_entries.extend(whisper_dir.rglob("*"))
        except OSError:
            # 对照稿 I/O 异常由独立子状态收口，不污染主链归档校验。
            optional_entries = []
        all_entries = main_entries + optional_entries
        if any(
            path.is_symlink()
            for path in main_entries
        ):
            path_errors.append("artifact_symlink")
        if any(
            not path.is_symlink() and not path.is_file() and not path.is_dir()
            for path in main_entries
        ):
            path_errors.append("artifact_special_file")
        all_files = sorted(
            path
            for path in all_entries
            if path.is_file()
            and not path.is_symlink()
            and not _is_archive_noise(path, root)
        )
        artifacts = [str(path.relative_to(root)) for path in all_files]
        main_files = [
            path
            for path in all_files
            if path.relative_to(root).parts[0] != "whisper-ref"
        ]
        main_artifacts = [str(path.relative_to(root)) for path in main_files]
        direct = [path for path in main_files if path.parent == root]
        missing: list[str] = list(path_errors)
        minutes_only = (
            requested_stage == "minutes_generating"
            and input_transcript_sha256 is not None
        )
        required_paths: set[str] = set()
        empty_files = [
            path
            for path in main_files
            if path.name != "workbench-manifest.json" and path.stat().st_size == 0
        ]
        if empty_files:
            missing.append("empty_artifacts")
        invalid_json_files = []
        for path in main_files:
            if path.suffix.lower() != ".json" or path.name == "workbench-manifest.json":
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                invalid_json_files.append(path)
        if invalid_json_files:
            missing.append("invalid_json_artifacts")

        def require_one(key: str, candidates: list[Path]) -> None:
            if not candidates:
                missing.append(key)
            else:
                required_paths.add(str(candidates[0].relative_to(root)))

        require_one("audio", [p for p in direct if p.suffix.lower() in AUDIO_EXTENSIONS])
        source_srt_path: Path | None = None
        if minutes_only:
            input_file = root / (
                "input-transcript.srt"
                if minutes_protocol_version >= 3
                else "input-transcript.txt"
            )
            if (
                not input_file.is_file()
                or input_file.is_symlink()
                or _sha256_file(input_file) != input_transcript_sha256
            ):
                missing.append("input_transcript")
            else:
                required_paths.add(input_file.name)
                if input_file.suffix.lower() == ".srt":
                    source_srt_path = input_file
        else:
            require_one(
                "transcript_txt",
                [
                    p
                    for p in direct
                    if p.suffix.lower() == ".txt"
                    and not p.name.lower().endswith("spk.txt")
                    and p.name.lower() != "prompt.txt"
                    and p.name.lower() != "input-transcript.txt"
                ],
            )
            require_one(
                "transcript_srt", [p for p in direct if p.suffix.lower() == ".srt"]
            )
            source_srt_path = next(
                (p for p in direct if p.suffix.lower() == ".srt"), None
            )
            require_one(
                "spk.txt", [p for p in direct if p.name.lower().endswith("spk.txt")]
            )
            require_one(
                "funasr_json",
                [
                    p
                    for p in direct
                    if "funasr" in p.name.lower()
                    and p.suffix.lower() == ".json"
                ],
            )
            require_one(
                "funasr_log",
                [
                    p
                    for p in direct
                    if "funasr" in p.name.lower() and p.suffix.lower() == ".log"
                ],
            )

        md_by_stem = {p.stem: p for p in direct if p.suffix.lower() == ".md"}
        html_by_stem = {p.stem: p for p in direct if p.suffix.lower() == ".html"}
        minutes_stems = sorted(set(md_by_stem) & set(html_by_stem))
        minutes_md_path: Path | None = None
        if not minutes_stems:
            missing.append("minutes_md_html_pair")
        else:
            minutes_stem = minutes_stems[0]
            minutes_md_path = md_by_stem[minutes_stem]
            required_paths.add(str(minutes_md_path.relative_to(root)))
            required_paths.add(str(html_by_stem[minutes_stem].relative_to(root)))

        if minutes_protocol_version >= 2:
            evidence_path = root / "minutes-evidence.json"
            if minutes_md_path is None:
                missing.append("minutes_evidence")
            else:
                trusted_source_srt_sha256 = (
                    input_transcript_sha256 if minutes_only else source_srt_sha256
                )
                if (
                    minutes_protocol_version >= 3
                    and trusted_source_srt_sha256 is None
                    and job_id is not None
                ):
                    missing.append("minutes_plan_source_hash")
                if (
                    minutes_protocol_version >= 3
                    and minutes_plan_sha256 is None
                    and job_id is not None
                ):
                    missing.append("minutes_plan_hash")
                evidence_errors = _minutes_evidence_errors(
                    evidence_path,
                    minutes_md_path,
                    protocol_version=minutes_protocol_version,
                    plan_path=(
                        root / "minutes-plan.json"
                        if minutes_protocol_version >= 3
                        else None
                    ),
                    ledger_root=(
                        root / "minutes-ledger"
                        if minutes_protocol_version >= 3
                        else None
                    ),
                    source_srt_path=(
                        source_srt_path if minutes_protocol_version >= 3 else None
                    ),
                    expected_input_transcript_sha256=(
                        input_transcript_sha256 if minutes_protocol_version >= 3 else None
                    ),
                    expected_source_srt_sha256=(
                        trusted_source_srt_sha256
                        if minutes_protocol_version >= 3
                        else None
                    ),
                    expected_minutes_plan_sha256=(
                        minutes_plan_sha256
                        if minutes_protocol_version >= 3
                        else None
                    ),
                )
                missing.extend(evidence_errors)
                if not evidence_errors:
                    required_paths.add("minutes-evidence.json")
                    if minutes_protocol_version >= 3:
                        required_paths.add("minutes-plan.json")
                        try:
                            evidence_payload = json.loads(
                                evidence_path.read_text(encoding="utf-8")
                            )
                            if evidence_payload.get("strategy") == "multi_stage":
                                required_paths.update(
                                    str(path.relative_to(root))
                                    for path in sorted((root / "minutes-ledger").glob("*.json"))
                                )
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            pass

        manifest_path = root / "workbench-manifest.json"
        manifest: dict[str, Any] | None = None
        if not manifest_path.is_file():
            missing.append("workbench_manifest")
        else:
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
                else:
                    missing.append("manifest_json_object")
            except (OSError, json.JSONDecodeError):
                missing.append("manifest_json")

        if manifest is not None:
            if (
                type(manifest.get("schema_version")) is not int
                or manifest.get("schema_version") != 1
            ):
                missing.append("manifest_schema_version")
            if (
                minutes_protocol_version >= 2
                and manifest.get("minutes_protocol_version")
                != minutes_protocol_version
            ):
                missing.append("manifest_minutes_protocol_version")
            if job_id is not None and manifest.get("job_id") != job_id:
                missing.append("manifest_job_id")
            if expected_attempt is not None:
                manifest_attempt = manifest.get("attempt")
                if (
                    type(manifest_attempt) is not int
                    or manifest_attempt != expected_attempt
                ):
                    missing.append("manifest_attempt")
            if minutes_only:
                if manifest.get("requested_stage") != "minutes_generating":
                    missing.append("manifest_requested_stage")
                if manifest.get("input_transcript_sha256") != input_transcript_sha256:
                    missing.append("manifest_input_transcript_sha256")
            entries = manifest.get("artifacts")
            listed_paths: set[str] = set()
            invalid_list = not isinstance(entries, list) or not entries
            invalid_hashes = False
            if not invalid_list:
                for entry in entries:
                    value = entry.get("path") if isinstance(entry, dict) else entry
                    if not isinstance(value, str) or not value.strip():
                        invalid_list = True
                        continue
                    relative_value = Path(value)
                    if relative_value.is_absolute() or ".." in relative_value.parts:
                        invalid_list = True
                        continue
                    if relative_value.parts and relative_value.parts[0] == "whisper-ref":
                        # Whisper 是独立异步子状态，主链不依赖其文件或 manifest 哈希。
                        continue
                    candidate = Path(os.path.abspath(str(root / value)))
                    try:
                        candidate.relative_to(root)
                    except ValueError:
                        invalid_list = True
                        continue
                    if not candidate.is_file() or candidate.is_symlink():
                        invalid_list = True
                        continue
                    relative_path = str(candidate.relative_to(root))
                    listed_paths.add(relative_path)
                    if not isinstance(entry, dict):
                        invalid_hashes = True
                        continue
                    expected_size = entry.get("bytes")
                    expected_hash = entry.get("sha256")
                    if (
                        not isinstance(expected_size, int)
                        or expected_size != candidate.stat().st_size
                        or not isinstance(expected_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash.lower())
                        or expected_hash.lower() != _sha256_file(candidate)
                    ):
                        invalid_hashes = True
            if invalid_list:
                missing.append("manifest_artifacts")
            if invalid_hashes:
                missing.append("manifest_artifact_hashes")
            manifest_required_paths = set(main_artifacts) - {"workbench-manifest.json"}
            if manifest_required_paths != listed_paths or required_paths - listed_paths:
                missing.append("manifest_required_artifacts")

        if job_id is not None and job is not None:
            if not job["audio_sha256"]:
                missing.append("source_audio_hash")
            else:
                archived_audio = [
                    path for path in direct if path.suffix.lower() in AUDIO_EXTENSIONS
                ]
                if not any(
                    path.stat().st_size > 0
                    and _sha256_file(path) == job["audio_sha256"]
                    for path in archived_audio
                ):
                    missing.append("audio_hash_mismatch")

        # 只有在词法路径与整条路径链都通过无 symlink 校验后，才把 canonical
        # 路径写入台账；安全判断本身始终使用上面的 no-follow 词法路径。
        reported_root = root if archive_has_symlink else root.resolve()
        return ArchiveValidationReport(reported_root, missing, artifacts)

    def _validate_publish_manifest(
        self,
        row: sqlite3.Row,
        manifest_path: str | Path,
        meeting_id: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", meeting_id):
            raise PublishValidationError("meeting_id 无效")
        path = Path(os.path.abspath(str(Path(manifest_path).expanduser())))
        if path.name != "workbench-manifest.json" or path.is_symlink() or not path.is_file():
            raise PublishValidationError(
                "发布 manifest 不存在、命名错误或是符号链接"
            )
        publish_dir = path.parent
        if self.archive_root.is_symlink() or not self.archive_root.is_dir():
            raise PublishValidationError("归档根目录不可用或是符号链接")
        canonical_publish_dir = publish_dir.resolve()
        canonical_archive_root = self.archive_root.resolve()
        try:
            relative_publish_dir = canonical_publish_dir.relative_to(
                canonical_archive_root
            )
        except ValueError as exc:
            raise PublishValidationError("发布 manifest 不在归档根目录") from exc
        cursor = self.archive_root
        for part in relative_publish_dir.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise PublishValidationError("发布目录路径包含符号链接")

        current_archive_value = row["archive_dir"]
        if not current_archive_value:
            raise PublishValidationError("任务没有完成回执归档目录")
        current_archive = Path(
            os.path.abspath(str(Path(current_archive_value).expanduser()))
        )
        canonical_current_archive = current_archive.resolve()
        is_existing_published_directory = row["status"] == "published"
        if row["status"] == "draft_modified" and row["published_archive_dir"]:
            is_existing_published_directory = (
                Path(row["published_archive_dir"]).expanduser().resolve()
                == canonical_current_archive
            )
        if is_existing_published_directory:
            if canonical_publish_dir != canonical_current_archive:
                raise PublishValidationError("幂等发布 manifest 与已发布目录不一致")
        else:
            if canonical_publish_dir == canonical_current_archive:
                previous_published = row["published_archive_dir"]
                if (
                    previous_published
                    and Path(previous_published).expanduser().resolve()
                    != canonical_publish_dir
                ):
                    raise PublishValidationError(
                        "重处理发布必须回写任务原有正式归档目录"
                    )
                # 一级受管目录在发布前后物理路径不变；此时 manifest 已由工作台
                # 原地替换为 published，不能再用“未校对 manifest”校验器读取。
                try:
                    current_job, current_attempt = self._manifest_identity(
                        current_archive
                    )
                except RelayControlError as exc:
                    raise PublishValidationError(
                        "任务当前一级归档身份无效"
                    ) from exc
                if (
                    canonical_publish_dir.parent != canonical_archive_root
                    or current_job != row["job_id"]
                    or current_attempt != int(row["current_attempt"])
                ):
                    raise PublishValidationError(
                        "原地发布只允许任务自己的一级归档目录"
                    )
            else:
                try:
                    managed_current = self._managed_unreviewed_archive(
                        row, current_archive
                    )
                except RelayControlError as exc:
                    raise PublishValidationError(
                        "任务当前归档不是合法待校对目录"
                    ) from exc
                if (
                    canonical_current_archive != managed_current
                    or canonical_publish_dir.parent != canonical_archive_root
                ):
                    raise PublishValidationError(
                        "发布 manifest 不属于任务的正式归档目录"
                    )
            if canonical_publish_dir.parent != canonical_archive_root:
                raise PublishValidationError(
                    "发布 manifest 不属于任务的正式归档目录"
                )

        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublishValidationError("发布 manifest 不是有效 JSON") from exc
        if not isinstance(loaded, dict):
            raise PublishValidationError("发布 manifest 必须是 JSON 对象")
        if (
            type(loaded.get("schema_version")) is not int
            or loaded.get("schema_version") != 1
        ):
            raise PublishValidationError("发布 manifest schema_version 无效")
        if loaded.get("job_id") != row["job_id"]:
            raise PublishValidationError("发布 manifest job_id 不匹配")
        declared_attempt = loaded.get("attempt")
        if (
            not isinstance(declared_attempt, int)
            or isinstance(declared_attempt, bool)
            or declared_attempt != int(row["current_attempt"])
        ):
            raise PublishValidationError("发布 manifest attempt 不是当前 attempt")
        if loaded.get("meeting_id") != meeting_id:
            raise PublishValidationError("发布 manifest meeting_id 不匹配")
        if row["meeting_id"] and row["meeting_id"] != meeting_id:
            raise PublishValidationError("发布任务不能改绑其他 meeting_id")
        if loaded.get("status") != "published":
            raise PublishValidationError("发布 manifest status 不是 published")
        with self._connect() as connection:
            attempt_protocol = connection.execute(
                """
                SELECT requested_stage, input_transcript_sha256,
                       source_srt_sha256, minutes_plan_sha256, status,
                       minutes_protocol_version
                FROM attempts
                WHERE job_id = ? AND attempt_no = ?
                """,
                (row["job_id"], row["current_attempt"]),
            ).fetchone()
        minutes_protocol_version = (
            int(attempt_protocol["minutes_protocol_version"])
            if attempt_protocol is not None
            else 1
        )
        if (
            minutes_protocol_version >= 2
            and loaded.get("minutes_protocol_version") != minutes_protocol_version
        ):
            raise PublishValidationError("发布 manifest 纪要内容协议版本不匹配")

        declared_minutes_only = (
            loaded.get("requested_stage") == "minutes_generating"
            or loaded.get("input_transcript_sha256") is not None
        )
        minutes_only = False
        if declared_minutes_only:
            input_digest = loaded.get("input_transcript_sha256")
            if (
                loaded.get("requested_stage") != "minutes_generating"
                or not isinstance(input_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", input_digest)
            ):
                raise PublishValidationError("仅重生成纪要的来源声明无效")
            attempt = attempt_protocol
            if (
                attempt is None
                or attempt["requested_stage"] != "minutes_generating"
                or attempt["input_transcript_sha256"] != input_digest
                or attempt["status"] not in {"completed_unreviewed", "published"}
            ):
                raise PublishValidationError("仅重生成纪要的来源与当前 attempt 不一致")
            minutes_only = True

        try:
            all_entries = list(publish_dir.rglob("*"))
        except OSError as exc:
            raise PublishValidationError("无法读取发布目录") from exc
        if any(item.is_symlink() for item in all_entries):
            raise PublishValidationError("发布目录包含符号链接")
        if any(
            not item.is_file() and not item.is_dir()
            for item in all_entries
        ):
            raise PublishValidationError("发布目录包含特殊文件")
        non_history_entries = [
            item
            for item in all_entries
            if ".workbench-history" not in item.relative_to(publish_dir).parts
        ]
        relevant_entries = [
            item
            for item in non_history_entries
            if not _is_archive_noise(item, publish_dir)
        ]
        physical_files = {
            str(item.relative_to(publish_dir)): item
            for item in relevant_entries
            if item.is_file() and item != path
        }

        manifest_artifacts = loaded.get("artifacts")
        if not isinstance(manifest_artifacts, list) or not manifest_artifacts:
            raise PublishValidationError("发布 manifest 缺少 artifacts")
        listed_files: dict[str, Path] = {}
        listed_hashes: dict[str, str] = {}
        for entry in manifest_artifacts:
            if not isinstance(entry, dict):
                raise PublishValidationError("发布 artifact 条目格式无效")
            value = entry.get("path")
            if not isinstance(value, str) or not value.strip():
                raise PublishValidationError("发布 artifact 路径无效")
            relative_value = Path(value)
            if relative_value.is_absolute() or ".." in relative_value.parts:
                raise PublishValidationError("发布 artifact 路径越界")
            candidate = Path(os.path.abspath(str(publish_dir / relative_value)))
            try:
                candidate.relative_to(publish_dir)
            except ValueError as exc:
                raise PublishValidationError("发布 artifact 路径越界") from exc
            relative_path = str(candidate.relative_to(publish_dir))
            if relative_path in listed_files:
                raise PublishValidationError("发布 manifest artifact 路径重复")
            if not candidate.is_file() or candidate.is_symlink():
                raise PublishValidationError("发布 manifest 引用了缺失文件")
            expected_size = entry.get("bytes")
            expected_hash = entry.get("sha256")
            if (
                not isinstance(expected_size, int)
                or expected_size != candidate.stat().st_size
                or not isinstance(expected_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash.lower())
                or expected_hash.lower() != _sha256_file(candidate)
            ):
                raise PublishValidationError("发布 artifact 大小或哈希不一致")
            listed_files[relative_path] = candidate
            listed_hashes[relative_path] = expected_hash.lower()
        if set(listed_files) != set(physical_files):
            raise PublishValidationError("发布 manifest 未完整覆盖实际产物")

        direct = [
            candidate
            for candidate in listed_files.values()
            if candidate.parent == publish_dir
        ]
        required_checks = {
            "audio": any(path.suffix.lower() in AUDIO_EXTENSIONS for path in direct),
            "transcript_txt": any(
                path.suffix.lower() == ".txt"
                and not path.name.lower().endswith("spk.txt")
                and path.name.lower() != "prompt.txt"
                and path.name.lower() != "input-transcript.txt"
                for path in direct
            ),
            "transcript_srt": any(path.suffix.lower() == ".srt" for path in direct),
            "spk.txt": any(path.name.lower().endswith("spk.txt") for path in direct),
            "funasr_json": any(
                "funasr" in path.name.lower() and path.suffix.lower() == ".json"
                for path in direct
            ),
            "funasr_log": any(
                "funasr" in path.name.lower() and path.suffix.lower() == ".log"
                for path in direct
            ),
        }
        if minutes_only:
            for optional_source in (
                "transcript_txt",
                "transcript_srt",
                "spk.txt",
                "funasr_json",
                "funasr_log",
            ):
                required_checks.pop(optional_source)
        md_stems = {path.stem for path in direct if path.suffix.lower() == ".md"}
        html_stems = {path.stem for path in direct if path.suffix.lower() == ".html"}
        required_checks["minutes_md_html_pair"] = bool(md_stems & html_stems)
        missing_main = [name for name, present in required_checks.items() if not present]
        if missing_main:
            raise PublishValidationError(
                "发布主产物不完整: " + ", ".join(sorted(missing_main))
            )
        if minutes_protocol_version >= 2:
            minutes_stem = sorted(md_stems & html_stems)[0]
            minutes_markdown = next(
                path
                for path in direct
                if path.suffix.lower() == ".md" and path.stem == minutes_stem
            )
            trusted_source_srt_sha256 = (
                attempt_protocol["input_transcript_sha256"]
                if minutes_only
                else attempt_protocol["source_srt_sha256"]
            )
            if (
                minutes_protocol_version >= 3
                and trusted_source_srt_sha256 is None
            ):
                raise PublishValidationError("发布任务缺少可信逐字稿哈希")
            if (
                minutes_protocol_version >= 3
                and attempt_protocol["minutes_plan_sha256"] is None
            ):
                raise PublishValidationError("发布任务缺少可信纪要 plan 哈希")
            evidence_errors = _minutes_evidence_errors(
                publish_dir / "minutes-evidence.json",
                minutes_markdown,
                protocol_version=minutes_protocol_version,
                plan_path=(
                    publish_dir / "minutes-plan.json"
                    if minutes_protocol_version >= 3
                    else None
                ),
                ledger_root=(
                    publish_dir / "minutes-ledger"
                    if minutes_protocol_version >= 3
                    else None
                ),
                source_srt_path=(
                    (
                        publish_dir / "input-transcript.srt"
                        if minutes_only
                        else next(
                            (
                                path
                                for path in direct
                                if path.suffix.lower() == ".srt"
                            ),
                            publish_dir / "missing.srt",
                        )
                    )
                    if minutes_protocol_version >= 3
                    else None
                ),
                expected_input_transcript_sha256=(
                    attempt_protocol["input_transcript_sha256"]
                    if minutes_protocol_version >= 3 and minutes_only
                    else None
                ),
                expected_source_srt_sha256=(
                    trusted_source_srt_sha256
                    if minutes_protocol_version >= 3
                    else None
                ),
                expected_minutes_plan_sha256=(
                    attempt_protocol["minutes_plan_sha256"]
                    if minutes_protocol_version >= 3
                    else None
                ),
            )
            if evidence_errors:
                raise PublishValidationError(
                    "发布纪要证据账本无效: " + ", ".join(evidence_errors)
                )

        whisper = inspect_whisper_ref(publish_dir)
        if not minutes_only and whisper.get("status") != "ready":
            raise PublishValidationError("发布前 whisper-ref 尚未完整就绪")

        source_hash = row["audio_sha256"]
        original_audio = loaded.get("original_audio")
        if not source_hash or not isinstance(original_audio, dict):
            raise PublishValidationError("发布 manifest 缺少可核验的原音频")
        audio_value = original_audio.get("path")
        audio_hash = original_audio.get("sha256")
        if not isinstance(audio_value, str) or not audio_value:
            raise PublishValidationError("发布 manifest 原音频路径无效")
        audio_relative = Path(audio_value)
        if audio_relative.is_absolute() or ".." in audio_relative.parts:
            raise PublishValidationError("发布 manifest 原音频路径越界")
        published_audio = Path(os.path.abspath(str(publish_dir / audio_relative)))
        try:
            published_audio.relative_to(publish_dir)
        except ValueError as exc:
            raise PublishValidationError("发布 manifest 原音频路径越界") from exc
        audio_relative_path = str(published_audio.relative_to(publish_dir))
        if (
            not published_audio.is_file()
            or published_audio.is_symlink()
            or audio_hash != source_hash
            or _sha256_file(published_audio) != source_hash
            or listed_hashes.get(audio_relative_path) != source_hash
        ):
            raise PublishValidationError("发布前后原音频哈希不一致")
        if whisper.get("status") == "ready":
            whisper_stem = _canonical_media_stem(
                str(whisper.get("selected_stem") or "")
            )
            allowed_audio_stems = {
                _canonical_media_stem(Path(row["audio_path"]).stem),
                _canonical_media_stem(published_audio.stem),
            }
            if not whisper_stem or whisper_stem not in allowed_audio_stems:
                raise PublishValidationError("whisper-ref 与任务原音频不匹配")

        whisper_status = str(whisper.get("status") or "absent")
        whisper_error = (
            "whisper-ref incomplete or invalid"
            if whisper_status == "failed"
            else None
        )

        return {
            "archive_dir": str(publish_dir.resolve()),
            "archive_root": str(self.archive_root.resolve()),
            "manifest_path": str(path.resolve()),
            "manifest_sha256": _sha256_file(path),
            "artifact_hashes": dict(listed_hashes),
            "whisper_status": whisper_status,
            "whisper_error": whisper_error,
        }

    @staticmethod
    def _assert_publish_snapshot_unchanged(validation: dict[str, Any]) -> None:
        manifest = Path(validation["manifest_path"])
        if (
            not manifest.is_file()
            or manifest.is_symlink()
            or _sha256_file(manifest) != validation["manifest_sha256"]
        ):
            raise PublishValidationError("发布 manifest 在校验后发生变化")
        root = Path(validation["archive_dir"])
        archive_root = Path(validation["archive_root"])
        if archive_root.is_symlink() or not archive_root.is_dir():
            raise PublishValidationError("发布归档根目录在校验后不可用")
        try:
            relative_root = root.relative_to(archive_root)
        except ValueError as exc:
            raise PublishValidationError("发布目录在校验后越界") from exc
        cursor = archive_root
        for part in relative_root.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise PublishValidationError("发布目录在校验后出现符号链接")
        try:
            all_entries = list(root.rglob("*"))
        except OSError as exc:
            raise PublishValidationError("发布目录在校验后无法读取") from exc
        if any(item.is_symlink() for item in all_entries):
            raise PublishValidationError("发布目录在校验后出现符号链接")
        if any(
            not item.is_file() and not item.is_dir()
            for item in all_entries
        ):
            raise PublishValidationError("发布目录在校验后出现特殊文件")
        non_history_entries = [
            item
            for item in all_entries
            if ".workbench-history" not in item.relative_to(root).parts
        ]
        relevant_entries = [
            item
            for item in non_history_entries
            if not _is_archive_noise(item, root)
        ]
        physical_files = {
            str(item.relative_to(root))
            for item in relevant_entries
            if item.is_file() and item != manifest
        }
        if physical_files != set(validation["artifact_hashes"]):
            raise PublishValidationError("发布目录文件集合在校验后发生变化")
        for relative_path, expected_hash in validation["artifact_hashes"].items():
            candidate = root / relative_path
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or _sha256_file(candidate) != expected_hash
            ):
                raise PublishValidationError("发布产物在校验后发生变化")

    def _capture_completion_snapshot(
        self, report: ArchiveValidationReport
    ) -> dict[str, Any]:
        root = report.archive_dir
        manifest = root / "workbench-manifest.json"
        try:
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublishValidationError("完成回执 manifest 在校验后不可读") from exc
        entries = loaded.get("artifacts") if isinstance(loaded, dict) else None
        if not isinstance(entries, list):
            raise PublishValidationError("完成回执 manifest 在校验后无效")
        expected: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise PublishValidationError("完成回执 artifact 在校验后无效")
            relative = Path(entry["path"])
            if relative.parts and relative.parts[0] == "whisper-ref":
                continue
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise PublishValidationError("完成回执 artifact 哈希在校验后无效")
            expected[str(relative)] = digest
        snapshot = {
            "root": str(root),
            "archive_root": str(self.archive_root.resolve()),
            "manifest_sha256": _sha256_file(manifest),
            "artifact_hashes": expected,
        }
        self._assert_completion_snapshot_unchanged(snapshot)
        return snapshot

    @staticmethod
    def _assert_completion_snapshot_unchanged(snapshot: dict[str, Any]) -> None:
        root = Path(snapshot["root"])
        archive_root = Path(snapshot["archive_root"])
        if archive_root.is_symlink() or not archive_root.is_dir():
            raise PublishValidationError("完成回执归档根目录在校验后不可用")
        try:
            relative_root = root.relative_to(archive_root)
        except ValueError as exc:
            raise PublishValidationError("完成回执目录在校验后越界") from exc
        cursor = archive_root
        for part in relative_root.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise PublishValidationError("完成回执父路径在校验后出现符号链接")
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise PublishValidationError("完成回执目录在校验后不可用") from exc
        if not resolved_root.is_relative_to(archive_root.resolve()):
            raise PublishValidationError("完成回执目录在校验后越界")
        manifest = root / "workbench-manifest.json"
        if (
            root.is_symlink()
            or not root.is_dir()
            or manifest.is_symlink()
            or not manifest.is_file()
            or _sha256_file(manifest) != snapshot["manifest_sha256"]
        ):
            raise PublishValidationError("完成回执 manifest 在校验后发生变化")
        main_entries: list[Path] = []
        for top_level in root.iterdir():
            if top_level.name == "whisper-ref":
                continue
            main_entries.append(top_level)
            if top_level.is_dir() and not top_level.is_symlink():
                main_entries.extend(top_level.rglob("*"))
        if any(path.is_symlink() for path in main_entries):
            raise PublishValidationError("完成回执目录在校验后出现符号链接")
        if any(
            not path.is_file() and not path.is_dir()
            for path in main_entries
        ):
            raise PublishValidationError("完成回执目录在校验后出现特殊文件")
        physical = {
            str(path.relative_to(root)): path
            for path in main_entries
            if path.is_file()
            and path != manifest
            and not _is_archive_noise(path, root)
        }
        expected = snapshot["artifact_hashes"]
        if set(physical) != set(expected):
            raise PublishValidationError("完成回执文件集合在校验后发生变化")
        for relative_path, path in physical.items():
            if _sha256_file(path) != expected[relative_path]:
                raise PublishValidationError("完成回执产物在校验后发生变化")

    def mark_draft_modified(self, job_id: str) -> dict[str, Any]:
        allowed = {"completed_unreviewed", "published", "draft_modified"}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._job_row(connection, job_id)
            if row["status"] not in allowed:
                raise InvalidTransitionError(
                    "draft_modified 只接受 completed_unreviewed/published/"
                    f"draft_modified，当前为 {row['status']}"
                )
            now = _now()
            if row["status"] == "draft_modified":
                event_type = "draft_modified_acknowledged"
            else:
                updated = connection.execute(
                    """
                    UPDATE jobs SET status = 'draft_modified', updated_at = ?
                    WHERE job_id = ? AND status = ? AND current_attempt = ?
                    """,
                    (now, job_id, row["status"], row["current_attempt"]),
                )
                if updated.rowcount != 1:
                    raise InvalidTransitionError("draft_modified 状态 CAS 失败")
                event_type = "draft_modified"
            self._append_event(
                connection,
                job_id,
                row["current_attempt"],
                event_type,
                row["status"],
                "draft_modified",
                stage="draft_modified",
                payload={
                    "preserved_published_snapshot": (
                        {
                            "archive_dir": row["published_archive_dir"],
                            "manifest_path": row["published_manifest_path"],
                            "manifest_sha256": row["published_manifest_sha256"],
                            "meeting_id": row["meeting_id"],
                            "published_at": row["published_at"],
                        }
                        if row["published_manifest_path"]
                        else None
                    )
                },
            )
        return self.status(job_id)

    def _record_post_publish_validation_failure(
        self,
        job_id: str,
        attempt_no: int,
        validation: dict[str, Any],
        error: Exception,
    ) -> None:
        """仅撤回本次刚提交的 published 快照，不覆盖后续 attempt/发布。"""
        safe_error = f"post-commit publish validation failed: {type(error).__name__}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', failure_stage = 'publish_post_commit_validation',
                    last_error = ?, worker_id = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND current_attempt = ? AND status = 'published'
                  AND published_manifest_path = ?
                  AND published_manifest_sha256 = ?
                """,
                (
                    safe_error,
                    now,
                    job_id,
                    attempt_no,
                    validation["manifest_path"],
                    validation["manifest_sha256"],
                ),
            )
            if updated.rowcount != 1:
                return
            connection.execute(
                """
                UPDATE attempts
                SET status = 'failed', error = ?, finished_at = ?
                WHERE job_id = ? AND attempt_no = ?
                """,
                (safe_error, now, job_id, attempt_no),
            )
            self._append_event(
                connection,
                job_id,
                attempt_no,
                "publish_post_commit_validation_failed",
                "published",
                "failed",
                stage="publish_post_commit_validation",
                payload={"error_type": type(error).__name__},
            )

    def _assert_published_after_commit(
        self,
        job_id: str,
        attempt_no: int,
        validation: dict[str, Any],
    ) -> None:
        try:
            self._assert_publish_snapshot_unchanged(validation)
        except Exception as exc:
            self._record_post_publish_validation_failure(
                job_id, attempt_no, validation, exc
            )
            if isinstance(exc, PublishValidationError):
                raise
            raise PublishValidationError("发布提交后完整性复验失败") from exc

    def _record_pending_cleanup_event(
        self,
        job_id: str,
        attempt_no: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._append_event(
                    connection,
                    job_id,
                    attempt_no,
                    event_type,
                    "published",
                    "published",
                    stage="pending_archive_cleanup",
                    payload=payload,
                )
        except Exception:
            # 清理事件也是旁路，不得翻转已复验通过的发布结果。
            pass

    def _recover_published_pending_source(
        self,
        job_id: str,
        attempt_no: int,
        source_archive: str | Path | None,
    ) -> bool:
        if not source_archive:
            return False
        source = Path(os.path.abspath(str(Path(source_archive).expanduser())))
        with self._connect() as connection:
            row = self._job_row(connection, job_id)
        published_value = row["published_archive_dir"]
        if (
            published_value
            and source.resolve()
            == Path(published_value).expanduser().resolve()
        ):
            # 一级目录原地发布后，它就是正式真相源，绝不能作为旧待校对来源清理。
            return False
        pending_root = (self.archive_root / "待校对").resolve()
        if source.parent.resolve() not in {
            self.archive_root.resolve(),
            pending_root,
        }:
            return False
        try:
            with self._archive_lock():
                if not source.exists():
                    return False
                manifest_job, manifest_attempt, manifest_status = (
                    self._manifest_metadata(source)
                )
                if (
                    manifest_job != job_id
                    or manifest_attempt != attempt_no
                    or manifest_status == "published"
                ):
                    raise RelayControlError("发布后待校对来源身份不匹配")
                recovered = self._move_pending_to_recovery(
                    source,
                    job_id=job_id,
                    attempt_no=attempt_no,
                    category="published",
                )
            self._record_pending_cleanup_event(
                job_id,
                attempt_no,
                "published_pending_recovered",
                {
                    "source_archive_dir": str(source),
                    "recovery_archive_dir": str(recovered),
                },
            )
            return True
        except Exception as exc:
            self._record_pending_cleanup_event(
                job_id,
                attempt_no,
                "published_pending_cleanup_failed",
                {
                    "source_archive_dir": str(source),
                    "error_type": type(exc).__name__,
                },
            )
            return False

    def _recover_published_pending_sources(
        self,
        job_id: str,
        attempt_no: int,
    ) -> list[str]:
        sources = self._visible_pending_for_job(
            job_id, attempt_no=attempt_no
        )
        if not sources:
            return []
        with self._connect() as connection:
            row = self._job_row(connection, job_id)
        if row["status"] != "published" or int(row["current_attempt"]) != attempt_no:
            raise InvalidTransitionError("发布后待校对清理状态已变化")
        if (
            not row["published_manifest_path"]
            or not row["published_manifest_sha256"]
            or not row["meeting_id"]
        ):
            raise PublishValidationError("已发布快照缺少可复验身份")
        stored_manifest = Path(row["published_manifest_path"]).expanduser()
        try:
            manifest_relative = stored_manifest.resolve().relative_to(
                self.archive_root.resolve()
            )
        except ValueError as exc:
            raise PublishValidationError("已发布快照 manifest 越界") from exc
        validation_manifest = self.archive_root / manifest_relative
        validation = self._validate_publish_manifest(
            row, validation_manifest, row["meeting_id"]
        )
        if (
            validation["manifest_sha256"]
            != row["published_manifest_sha256"]
            or validation["archive_dir"] != row["published_archive_dir"]
        ):
            raise PublishValidationError("已发布快照与数据库身份不一致")
        self._assert_publish_snapshot_unchanged(validation)
        recovered: list[str] = []
        for source in sources:
            if self._recover_published_pending_source(
                job_id, attempt_no, source
            ):
                recovered.append(str(source))
        return recovered

    def mark_published(
        self,
        job_id: str,
        manifest_path: str | Path,
        meeting_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._job_row(connection, job_id)
            if row["status"] not in {
                "completed_unreviewed",
                "draft_modified",
                "published",
            }:
                raise InvalidTransitionError(
                    "发布回执只接受 completed_unreviewed/draft_modified/published，"
                    f"当前为 {row['status']}"
                )
            validation = self._validate_publish_manifest(row, manifest_path, meeting_id)
            snapshot_status = row["status"]
            snapshot_attempt = int(row["current_attempt"])
            snapshot_archive = row["archive_dir"]

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._job_row(connection, job_id)
            if current["status"] == "published":
                if (
                    current["meeting_id"] != meeting_id
                    or current["published_manifest_path"] != validation["manifest_path"]
                ):
                    raise InvalidTransitionError(
                        "已发布任务的 meeting 或 manifest 不一致"
                    )
                if current["published_manifest_sha256"] != validation["manifest_sha256"]:
                    raise PublishValidationError("已发布 manifest 已被改写，拒绝刷新发布快照")
                self._assert_publish_snapshot_unchanged(validation)
                connection.execute(
                    """
                    UPDATE jobs
                    SET published_archive_dir = ?, published_manifest_sha256 = ?,
                        whisper_status = ?, whisper_error = ?,
                        whisper_updated_at = ?, whisper_attempt = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'published'
                    """,
                    (
                        validation["archive_dir"],
                        validation["manifest_sha256"],
                        validation["whisper_status"],
                        validation["whisper_error"],
                        _now(),
                        current["current_attempt"],
                        _now(),
                        job_id,
                    ),
                )
                if current["whisper_status"] != validation["whisper_status"]:
                    self._append_event(
                        connection,
                        job_id,
                        current["current_attempt"],
                        "whisper_substate_changed",
                        current["whisper_status"],
                        validation["whisper_status"],
                        stage="whisper",
                        payload=(
                            {"error": validation["whisper_error"]}
                            if validation["whisper_error"]
                            else {}
                        ),
                    )
                self._append_event(
                    connection,
                    job_id,
                    current["current_attempt"],
                    "publish_ack_refreshed",
                    "published",
                    "published",
                    stage="published",
                    payload={
                        "meeting_id": meeting_id,
                        "manifest_path": validation["manifest_path"],
                    },
                )
                self._assert_publish_snapshot_unchanged(validation)
                connection.commit()
                self._assert_published_after_commit(
                    job_id, int(current["current_attempt"]), validation
                )
                self._recover_published_pending_sources(
                    job_id, int(current["current_attempt"])
                )
                return self.status(job_id)
            if (
                current["status"] != snapshot_status
                or int(current["current_attempt"]) != snapshot_attempt
                or current["archive_dir"] != snapshot_archive
            ):
                raise InvalidTransitionError("发布校验期间任务状态已变化")
            self._assert_publish_snapshot_unchanged(validation)
            now = _now()
            previous_published_snapshot = None
            if current["published_manifest_path"]:
                previous_published_snapshot = {
                    "archive_dir": current["published_archive_dir"],
                    "manifest_path": current["published_manifest_path"],
                    "manifest_sha256": current["published_manifest_sha256"],
                    "meeting_id": current["meeting_id"],
                    "published_at": current["published_at"],
                }
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'published', archive_dir = ?,
                    published_archive_dir = ?, meeting_id = ?,
                    published_manifest_path = ?, published_manifest_sha256 = ?,
                    published_at = ?, retry_stage = NULL, last_error = NULL,
                    failure_stage = NULL, whisper_status = ?,
                    whisper_error = ?, whisper_updated_at = ?,
                    whisper_attempt = ?, updated_at = ?
                WHERE job_id = ? AND current_attempt = ?
                  AND status IN ('completed_unreviewed', 'draft_modified')
                  AND archive_dir = ?
                """,
                (
                    validation["archive_dir"],
                    validation["archive_dir"],
                    meeting_id,
                    validation["manifest_path"],
                    validation["manifest_sha256"],
                    now,
                    validation["whisper_status"],
                    validation["whisper_error"],
                    now,
                    snapshot_attempt,
                    now,
                    job_id,
                    snapshot_attempt,
                    snapshot_archive,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("发布状态 CAS 失败")
            connection.execute(
                """
                UPDATE attempts SET status = 'published', finished_at = ?
                WHERE job_id = ? AND attempt_no = ?
                """,
                (now, job_id, snapshot_attempt),
            )
            self._append_event(
                connection,
                job_id,
                snapshot_attempt,
                "job_published",
                snapshot_status,
                "published",
                stage="published",
                payload={
                    "meeting_id": meeting_id,
                    "manifest_path": validation["manifest_path"],
                    "manifest_sha256": validation["manifest_sha256"],
                    "previous_archive_dir": snapshot_archive,
                    "previous_published_snapshot": previous_published_snapshot,
                },
            )
            if current["whisper_status"] != validation["whisper_status"]:
                self._append_event(
                    connection,
                    job_id,
                    snapshot_attempt,
                    "whisper_substate_changed",
                    current["whisper_status"],
                    validation["whisper_status"],
                    stage="whisper",
                    payload=(
                        {"error": validation["whisper_error"]}
                        if validation["whisper_error"]
                        else {}
                    ),
                )
            self._assert_publish_snapshot_unchanged(validation)
            connection.commit()
            self._assert_published_after_commit(
                job_id, snapshot_attempt, validation
            )
            self._recover_published_pending_source(
                job_id, snapshot_attempt, snapshot_archive
            )
        return self.status(job_id)

    def complete_minutes(
        self,
        job_id: str,
        archive_dir: str | Path,
        attempt_no: int | None = None,
    ) -> dict[str, Any]:
        replay_pending_promotion = False
        with self._connect() as connection:
            row = self._job_row(connection, job_id)
            current_attempt = int(row["current_attempt"])
            current_attempt_row = connection.execute(
                """
                SELECT requested_stage, input_transcript_path,
                       input_transcript_sha256, input_transcript_bytes,
                       source_srt_sha256, minutes_plan_sha256,
                       minutes_protocol_version
                FROM attempts WHERE job_id = ? AND attempt_no = ?
                """,
                (job_id, current_attempt),
            ).fetchone()
            callback_attempt = attempt_no
            manifest_path = Path(archive_dir).expanduser() / "workbench-manifest.json"
            if callback_attempt is None and manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if type(manifest.get("attempt")) is int:
                        callback_attempt = int(manifest["attempt"])
                except (OSError, json.JSONDecodeError, AttributeError):
                    pass
            if callback_attempt is None:
                callback_attempt = current_attempt
            if callback_attempt != current_attempt:
                raise InvalidTransitionError(
                    f"迟到的 attempt 回执: callback={callback_attempt}, current={current_attempt}"
                )
            recovery_stage = (
                str(row["failure_stage"])
                if row["status"] == "failed"
                and row["failure_stage"] in {"archive_validation", "codex_callback"}
                else None
            )
            if (
                self.auto_pending_archive
                and row["status"] in {"completed_unreviewed", "draft_modified"}
                and callback_attempt == current_attempt
            ):
                callback_archive = Path(
                    os.path.abspath(str(Path(archive_dir).expanduser()))
                )
                expected_callback = self._expected_attempt_dir(
                    job_id, current_attempt
                )
                if callback_archive.resolve() != expected_callback.resolve():
                    raise InvalidTransitionError(
                        "完成回执重放不是原 hidden attempt"
                    )
                replay_pending_promotion = True
            if row["status"] != "minutes_generating" and recovery_stage is None:
                if not replay_pending_promotion:
                    raise InvalidTransitionError(
                        f"完成回执要求 minutes_generating，当前为 {row['status']}"
                    )

        if replay_pending_promotion:
            try:
                return self._promote_pending_archive(job_id)
            except Exception as exc:
                self._record_pending_archive_failure(
                    job_id, callback_attempt, exc
                )
                raise

        report = self.validate_archive(
            archive_dir,
            job_id=job_id,
            attempt_no=callback_attempt,
            requested_stage=current_attempt_row["requested_stage"],
            input_transcript_sha256=current_attempt_row[
                "input_transcript_sha256"
            ],
            source_srt_sha256=current_attempt_row["source_srt_sha256"],
            minutes_plan_sha256=current_attempt_row["minutes_plan_sha256"],
            minutes_protocol_version=int(
                current_attempt_row["minutes_protocol_version"]
            ),
        )
        if not report.valid:
            self.fail(
                job_id,
                stage="archive_validation",
                error="missing: " + ", ".join(report.missing),
                expected_attempt=callback_attempt,
                expected_status=str(row["status"]),
            )
            raise ArtifactValidationError(report)
        completion_snapshot = self._capture_completion_snapshot(report)
        try:
            whisper_inspection = inspect_whisper_ref(report.archive_dir)
        except Exception:
            # Whisper 是旁路；即便外置盘 I/O 竞态导致探测异常，主回执仍要收口。
            whisper_inspection = {
                "status": "failed",
                "missing": ["inspection_error"],
            }
        if whisper_inspection.get("status") == "ready":
            allowed_source_stems = {
                _canonical_media_stem(Path(row["audio_path"]).stem)
            }
            for archived_audio in report.archive_dir.iterdir():
                if (
                    archived_audio.is_file()
                    and not archived_audio.is_symlink()
                    and archived_audio.suffix.lower() in AUDIO_EXTENSIONS
                    and row["audio_sha256"]
                    and _sha256_file(archived_audio) == row["audio_sha256"]
                ):
                    allowed_source_stems.add(
                        _canonical_media_stem(archived_audio.stem)
                    )
            whisper_stem = _canonical_media_stem(
                str(whisper_inspection.get("selected_stem") or "")
            )
            if whisper_stem not in allowed_source_stems:
                whisper_inspection = {
                    **whisper_inspection,
                    "status": "failed",
                    "ready": False,
                    "missing": ["source_stem_mismatch"],
                }

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._job_row(connection, job_id)
            if int(current["current_attempt"]) != callback_attempt:
                raise InvalidTransitionError("attempt 已在校验期间发生变化")
            self._assert_completion_snapshot_unchanged(completion_snapshot)
            if recovery_stage is not None:
                if (
                    current["status"] != "failed"
                    or current["failure_stage"] != recovery_stage
                ):
                    raise InvalidTransitionError(f"{recovery_stage} 状态已发生变化")
                now = _now()
                repaired = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'minutes_generating', last_error = NULL,
                        failure_stage = NULL, updated_at = ?
                    WHERE job_id = ? AND current_attempt = ?
                      AND status = 'failed' AND failure_stage = ?
                    """,
                    (now, job_id, callback_attempt, recovery_stage),
                )
                if repaired.rowcount != 1:
                    raise InvalidTransitionError(f"{recovery_stage} 修复 CAS 失败")
                repaired_attempt = connection.execute(
                    """
                    UPDATE attempts
                    SET status = 'minutes_generating', error = NULL, finished_at = NULL
                    WHERE job_id = ? AND attempt_no = ? AND status = 'failed'
                    """,
                    (job_id, callback_attempt),
                )
                if repaired_attempt.rowcount != 1:
                    raise InvalidTransitionError(
                        f"{recovery_stage} attempt 修复 CAS 失败"
                    )
                self._append_event(
                    connection,
                    job_id,
                    callback_attempt,
                    (
                        "archive_validation_repaired"
                        if recovery_stage == "archive_validation"
                        else "codex_callback_recovered"
                    ),
                    "failed",
                    "minutes_generating",
                    stage=recovery_stage,
                )
            self._transition(
                connection,
                job_id,
                "completed_unreviewed",
                event_type="minutes_completed",
                stage="minutes_generating",
                payload={
                    "archive_dir": str(report.archive_dir),
                    "artifacts": list(report.artifacts),
                },
            )
            archive_updated = connection.execute(
                """UPDATE jobs SET archive_dir = ?, retry_stage = NULL
                   WHERE job_id = ? AND current_attempt = ?
                     AND status = 'completed_unreviewed'""",
                (str(report.archive_dir), job_id, callback_attempt),
            )
            if archive_updated.rowcount != 1:
                raise InvalidTransitionError("完成归档状态 CAS 失败")
            now = _now()
            connection.executemany(
                """
                INSERT OR IGNORE INTO job_artifacts (job_id, relative_path, created_at)
                VALUES (?, ?, ?)
                """,
                [(job_id, path, now) for path in report.artifacts],
            )
            if whisper_inspection["status"] in {"running", "ready", "failed"}:
                old_whisper_status = current["whisper_status"]
                whisper_error = (
                    "whisper-ref incomplete or invalid"
                    if whisper_inspection["status"] == "failed"
                    else None
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET whisper_status = ?, whisper_error = ?,
                        whisper_updated_at = ?, whisper_attempt = ?, updated_at = ?
                    WHERE job_id = ? AND current_attempt = ?
                    """,
                    (
                        whisper_inspection["status"],
                        whisper_error,
                        now,
                        callback_attempt,
                        now,
                        job_id,
                        callback_attempt,
                    ),
                )
                self._append_event(
                    connection,
                    job_id,
                    callback_attempt,
                    "whisper_substate_changed",
                    old_whisper_status,
                    whisper_inspection["status"],
                    stage="whisper",
                    payload={"error": whisper_error} if whisper_error else {},
                )
            self._assert_completion_snapshot_unchanged(completion_snapshot)
        if self.auto_pending_archive:
            try:
                self._promote_pending_archive(job_id)
            except Exception as exc:
                self._record_pending_archive_failure(
                    job_id, callback_attempt, exc
                )
                raise
        # 产品语义：用户请求“当前阶段结束后停止”时保留已生成产物，但任务仍以
        # interrupted 收口，避免界面把主动停止误显示成完整流水线成功。
        if self.should_stop_after_stage(job_id):
            self.interrupt_if_stop_requested(job_id, "minutes_generating")
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._job_row(connection, job_id)
            archive_policy = self._archive_policy(connection, job_id)
            attempts = connection.execute(
                """
                SELECT attempt_no, requested_stage, status,
                       input_transcript_path, input_transcript_sha256,
                       input_transcript_bytes, source_srt_sha256,
                       minutes_plan_sha256,
                       minutes_protocol_version,
                       error, started_at, finished_at
                FROM attempts WHERE job_id = ? ORDER BY attempt_no
                """,
                (job_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT id, attempt_no, event_type, from_status, to_status, stage,
                       payload_json, created_at
                FROM events WHERE job_id = ? ORDER BY id
                """,
                (job_id,),
            ).fetchall()
            artifacts = connection.execute(
                """
                SELECT relative_path FROM job_artifacts
                WHERE job_id = ? ORDER BY relative_path
                """,
                (job_id,),
            ).fetchall()

        current_attempt_row = next(
            item for item in attempts if item["attempt_no"] == row["current_attempt"]
        )

        return {
            "job_id": row["job_id"],
            "audio_path": row["audio_path"],
            "status": row["status"],
            "degraded": row["failure_stage"] == "pending_archive",
            "current_attempt": row["current_attempt"],
            "requested_stage": current_attempt_row["requested_stage"],
            "input_transcript_path": current_attempt_row["input_transcript_path"],
            "input_transcript_sha256": current_attempt_row[
                "input_transcript_sha256"
            ],
            "input_transcript_bytes": current_attempt_row["input_transcript_bytes"],
            "source_srt_sha256": current_attempt_row["source_srt_sha256"],
            "minutes_plan_sha256": current_attempt_row["minutes_plan_sha256"],
            "minutes_protocol_version": int(
                current_attempt_row["minutes_protocol_version"]
            ),
            "hotwords_configured": bool(row["hotword_prompt_path"]),
            "hotword_prompt_sha256": row["hotword_prompt_sha256"],
            "retry_stage": row["retry_stage"],
            "stop_after_stage": bool(row["stop_after_stage"]),
            "archive_dir": row["archive_dir"],
            "archive_policy": archive_policy,
            "last_error": row["last_error"],
            "failure_stage": row["failure_stage"],
            "audio_sha256": row["audio_sha256"],
            "audio_size": row["audio_size"],
            "deduplicated_to": row["deduplicated_to"],
            "worker_id": row["worker_id"],
            "claimed_at": row["claimed_at"],
            "codex_dispatched_at": row["codex_dispatched_at"],
            "meeting_id": row["meeting_id"],
            "published_archive_dir": row["published_archive_dir"],
            "published_manifest_path": row["published_manifest_path"],
            "published_manifest_sha256": row["published_manifest_sha256"],
            "published_at": row["published_at"],
            "whisper_status": row["whisper_status"],
            "index_status": row["index_status"],
            "substates": {
                "whisper": {
                    "status": row["whisper_status"],
                    "error": row["whisper_error"],
                    "updated_at": row["whisper_updated_at"],
                    "attempt": row["whisper_attempt"],
                    "retry_requested": bool(row["whisper_retry_requested"]),
                    "retry_generation": row["whisper_retry_generation"],
                    "worker_id": row["whisper_worker_id"],
                    "claimed_at": row["whisper_claimed_at"],
                },
                "index": {
                    "status": row["index_status"],
                    "error": row["index_error"],
                    "updated_at": row["index_updated_at"],
                    "attempt": row["index_attempt"],
                },
            },
            "published_snapshot": (
                {
                    "archive_dir": row["published_archive_dir"],
                    "manifest_path": row["published_manifest_path"],
                    "manifest_sha256": row["published_manifest_sha256"],
                    "meeting_id": row["meeting_id"],
                    "published_at": row["published_at"],
                }
                if row["published_manifest_path"]
                else None
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "attempts": [dict(item) for item in attempts],
            "events": [
                {
                    **{key: item[key] for key in item.keys() if key != "payload_json"},
                    "payload": _json_loads(item["payload_json"]),
                }
                for item in events
            ],
            "artifacts": [item["relative_path"] for item in artifacts],
        }

    def list_jobs(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if status is not None and status not in ALL_STATES:
            raise RelayControlError(f"未知状态筛选: {status}")
        safe_limit = max(1, min(int(limit), 500))
        query = (
            "SELECT job_id, status, current_attempt, retry_stage, stop_after_stage, "
            "failure_stage, last_error, deduplicated_to, meeting_id, "
            "published_archive_dir, "
            "whisper_status, index_status, "
            "created_at, updated_at "
            "FROM jobs"
        )
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY updated_at DESC, job_id DESC LIMIT ?"
        parameters.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "job_id": row["job_id"],
                "status": row["status"],
                "current_attempt": row["current_attempt"],
                "retry_stage": row["retry_stage"],
                "stop_after_stage": bool(row["stop_after_stage"]),
                "failure_stage": row["failure_stage"],
                "last_error": row["last_error"],
                "deduplicated_to": row["deduplicated_to"],
                "meeting_id": row["meeting_id"],
                "published_archive_dir": row["published_archive_dir"],
                "whisper_status": row["whisper_status"],
                "index_status": row["index_status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]


def _service(db_path: str | Path | None = None) -> RelayControl:
    return RelayControl(db_path)


def enqueue(
    audio: str | Path,
    db_path: str | Path | None = None,
    *,
    compute_hash: bool = True,
    requested_stage: str | None = None,
    transcript_path: str | Path | None = None,
    hotword_prompt_path: str | Path | None = None,
) -> str:
    return _service(db_path).enqueue(
        audio,
        compute_hash=compute_hash,
        requested_stage=requested_stage,
        transcript_path=transcript_path,
        hotword_prompt_path=hotword_prompt_path,
    )


def retry(
    job_id: str,
    stage: str,
    db_path: str | Path | None = None,
    *,
    transcript_path: str | Path | None = None,
) -> int:
    return _service(db_path).retry(job_id, stage, transcript_path)


def claim_next(
    worker_id: str | None = None, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    return _service(db_path).claim_next(worker_id)


def claim_whisper_retry(
    worker_id: str | None = None,
    db_path: str | Path | None = None,
    *,
    job_id: str | None = None,
) -> dict[str, Any] | None:
    return _service(db_path).claim_whisper_retry(worker_id, job_id=job_id)


def finish_whisper_retry(
    job_id: str,
    *,
    attempt_no: int,
    generation: int,
    worker_id: str,
    success: bool,
    error: str | None = None,
    artifact_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return _service(db_path).finish_whisper_retry(
        job_id,
        attempt_no=attempt_no,
        generation=generation,
        worker_id=worker_id,
        success=success,
        error=error,
        artifact_dir=artifact_dir,
    )


def record_source_audio(
    job_id: str,
    audio: str | Path,
    db_path: str | Path | None = None,
    *,
    expected_attempt: int | None = None,
    expected_worker: str | None = None,
) -> dict[str, Any]:
    return _service(db_path).record_source_audio(
        job_id,
        audio,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def record_minutes_plan_source(
    job_id: str,
    *,
    attempt_no: int,
    source_srt_sha256: str,
    minutes_plan_sha256: str,
    expected_worker: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return _service(db_path).record_minutes_plan_source(
        job_id,
        attempt_no=attempt_no,
        source_srt_sha256=source_srt_sha256,
        minutes_plan_sha256=minutes_plan_sha256,
        expected_worker=expected_worker,
    )


def prepare_attempt_draft(
    job_id: str,
    *,
    attempt_no: int,
    expected_worker: str | None = None,
    db_path: str | Path | None = None,
) -> Path:
    return _service(db_path).prepare_attempt_draft(
        job_id,
        attempt_no=attempt_no,
        expected_worker=expected_worker,
    )


def recover_orphaned_claims(db_path: str | Path | None = None) -> int:
    return _service(db_path).recover_orphaned_claims()


def record_codex_dispatched(
    job_id: str,
    db_path: str | Path | None = None,
    *,
    expected_attempt: int | None = None,
    expected_worker: str | None = None,
) -> dict[str, Any]:
    return _service(db_path).record_codex_dispatched(
        job_id,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def reconcile_codex_handoffs(
    grace_seconds: float = 30, db_path: str | Path | None = None
) -> int:
    return _service(db_path).reconcile_codex_handoffs(grace_seconds)


def migrate_pending_archives(
    job_ids: Iterable[str] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return _service(db_path).migrate_pending_archives(job_ids)


def reconcile_pending_archives(
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return _service(db_path).reconcile_pending_archives()


def stop_after_stage(job_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    return _service(db_path).stop_after_stage(job_id)


def cancel(job_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    return _service(db_path).cancel(job_id)


def status(job_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    return _service(db_path).status(job_id)


def list_jobs(
    status_filter: str | None = None,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    return _service(db_path).list_jobs(status=status_filter, limit=limit)


def heartbeat_worker(
    name: str,
    *,
    mode: str,
    status: str = "running",
    current_job_id: str | None = None,
    current_stage: str | None = None,
    pid: int | None = None,
    last_error: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return _service(db_path).heartbeat_worker(
        name,
        mode=mode,
        status=status,
        current_job_id=current_job_id,
        current_stage=current_stage,
        pid=pid,
        last_error=last_error,
    )


def stop_worker(name: str, db_path: str | Path | None = None) -> None:
    _service(db_path).stop_worker(name)


def health(
    db_path: str | Path | None = None,
    *,
    stale_seconds: float = 30,
) -> dict[str, Any]:
    return _service(db_path).health(stale_seconds=stale_seconds)


def record_stage(
    job_id: str,
    stage: str,
    db_path: str | Path | None = None,
    *,
    expected_attempt: int | None = None,
    expected_worker: str | None = None,
) -> dict[str, Any]:
    return _service(db_path).record_stage(
        job_id,
        stage,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def record_substate(
    job_id: str,
    name: str,
    substate_status: str,
    error: str | None = None,
    db_path: str | Path | None = None,
    *,
    attempt_no: int | None = None,
) -> dict[str, Any]:
    return _service(db_path).record_substate(
        job_id, name, substate_status, error=error, attempt_no=attempt_no
    )


def retry_substate(
    job_id: str, name: str, db_path: str | Path | None = None
) -> dict[str, Any]:
    return _service(db_path).retry_substate(job_id, name)


def fail(
    job_id: str,
    stage: str,
    error: str,
    db_path: str | Path | None = None,
    *,
    expected_attempt: int | None = None,
    expected_status: str | None = None,
    expected_worker: str | None = None,
) -> dict[str, Any]:
    return _service(db_path).fail(
        job_id,
        stage,
        error,
        expected_attempt=expected_attempt,
        expected_status=expected_status,
        expected_worker=expected_worker,
    )


def interrupt_if_stop_requested(
    job_id: str,
    completed_stage: str,
    db_path: str | Path | None = None,
    *,
    expected_attempt: int | None = None,
    expected_worker: str | None = None,
) -> bool:
    return _service(db_path).interrupt_if_stop_requested(
        job_id,
        completed_stage,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def complete_minutes(
    job_id: str,
    archive_dir: str | Path,
    db_path: str | Path | None = None,
    attempt_no: int | None = None,
) -> dict[str, Any]:
    return _service(db_path).complete_minutes(job_id, archive_dir, attempt_no)


def mark_published(
    job_id: str,
    manifest_path: str | Path,
    meeting_id: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    return _service(db_path).mark_published(job_id, manifest_path, meeting_id)


def mark_draft_modified(
    job_id: str, db_path: str | Path | None = None
) -> dict[str, Any]:
    return _service(db_path).mark_draft_modified(job_id)


def _execute_claim(claim: dict[str, Any]) -> bool:
    try:
        from quickstart import relay_watchdog
    except ImportError:
        import relay_watchdog
    return bool(relay_watchdog.process_controlled_claim(claim))


def _execute_whisper_retry_claim(claim: dict[str, Any]) -> bool:
    try:
        from quickstart import relay_watchdog
    except ImportError:
        import relay_watchdog
    return bool(relay_watchdog.process_whisper_retry_claim(claim))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relayctl", description="meeting-relay 任务控制")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue_parser = subparsers.add_parser("enqueue", help="创建或复用音频任务")
    enqueue_parser.add_argument("audio")
    enqueue_parser.add_argument("--stage", choices=["minutes_generating"])
    enqueue_parser.add_argument("--transcript")
    enqueue_parser.add_argument(
        "--hotwords", help="本任务使用的 UTF-8 热词文件，最多 20 个词"
    )
    enqueue_parser.add_argument("--json", action="store_true", dest="as_json")

    retry_parser = subparsers.add_parser("retry", help="从失败阶段创建新 attempt")
    retry_parser.add_argument("job_id")
    retry_parser.add_argument("--stage", required=True, choices=sorted(RETRYABLE_STAGES))
    retry_parser.add_argument("--transcript")
    retry_parser.add_argument(
        "--hotwords", help="可选替换任务热词快照；不传则沿用原任务热词"
    )

    retry_substate_parser = subparsers.add_parser(
        "retry-substate", help="将失败的独立子状态重新置为 pending"
    )
    retry_substate_parser.add_argument("job_id")
    retry_substate_parser.add_argument("--name", required=True, choices=sorted(SUBSTATE_NAMES))

    set_substate_parser = subparsers.add_parser(
        "set-substate", help="由 Whisper/index worker 回写真实子状态"
    )
    set_substate_parser.add_argument("job_id")
    set_substate_parser.add_argument("--name", required=True, choices=sorted(SUBSTATE_NAMES))
    set_substate_parser.add_argument(
        "--status", required=True, choices=sorted(SUBSTATE_STATUSES)
    )
    set_substate_parser.add_argument("--error")
    set_substate_parser.add_argument("--attempt", type=int, required=True)

    stop_parser = subparsers.add_parser(
        "stop-after-stage", help="当前阶段完成后停止，不强杀运行进程"
    )
    stop_parser.add_argument("job_id")

    cancel_parser = subparsers.add_parser("cancel", help="取消仍在队列中的任务")
    cancel_parser.add_argument("job_id")

    status_parser = subparsers.add_parser("status", help="查看任务状态")
    status_parser.add_argument("job_id")
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    health_parser = subparsers.add_parser("health", help="查看 Relay worker 运行健康")
    health_parser.add_argument("--json", action="store_true", dest="as_json")

    complete_parser = subparsers.add_parser(
        "complete-minutes", help="提交纪要生成完成回执并严格校验归档"
    )
    complete_parser.add_argument("job_id")
    complete_parser.add_argument("--archive-dir", required=True)
    complete_parser.add_argument("--attempt", type=int)

    published_parser = subparsers.add_parser(
        "mark-published", help="校验正式发布 manifest 并幂等回写 published"
    )
    published_parser.add_argument("job_id")
    published_parser.add_argument("--manifest", required=True)
    published_parser.add_argument("--meeting-id", required=True)

    draft_parser = subparsers.add_parser(
        "mark-draft-modified", help="幂等回写工作稿已修改状态"
    )
    draft_parser.add_argument("job_id")

    run_parser = subparsers.add_parser(
        "run-next", help="原子领取并执行队列中的下一个 attempt"
    )
    run_parser.add_argument("--json", action="store_true", dest="as_json")

    run_whisper_parser = subparsers.add_parser(
        "run-whisper-retry", help="领取并执行指定任务的 Whisper-only 重试"
    )
    run_whisper_parser.add_argument("job_id")
    run_whisper_parser.add_argument("--json", action="store_true", dest="as_json")

    migrate_pending_parser = subparsers.add_parser(
        "migrate-pending", help="幂等提升遗留 hidden attempt 到待校对"
    )
    migrate_pending_parser.add_argument("job_id", nargs="*")
    migrate_pending_parser.add_argument("--json", action="store_true", dest="as_json")

    archive_policy_parser = subparsers.add_parser(
        "archive-policy", help="显式设置合成夹具的归档策略"
    )
    archive_policy_parser.add_argument("job_id")
    archive_policy_parser.add_argument(
        "--policy", required=True, choices=["visible", "hidden_fixture"]
    )
    archive_policy_parser.add_argument("--json", action="store_true", dest="as_json")

    list_parser = subparsers.add_parser("list", help="列出任务摘要，不返回正文")
    list_parser.add_argument("--status", choices=sorted(ALL_STATES))
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    check_whisper_parser = subparsers.add_parser(
        "check-whisper", help="发布前严格检查完整 whisper-ref"
    )
    check_whisper_parser.add_argument("--archive-dir", required=True)
    check_whisper_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    control = RelayControl(initialize=args.command != "health")
    try:
        if args.command == "enqueue":
            job_id = control.enqueue(
                args.audio,
                requested_stage=args.stage,
                transcript_path=args.transcript,
                hotword_prompt_path=args.hotwords,
            )
            if args.as_json:
                print(json.dumps(control.status(job_id), ensure_ascii=False))
            else:
                print(job_id)
        elif args.command == "retry":
            attempt = control.retry(
                args.job_id,
                args.stage,
                transcript_path=args.transcript,
                hotword_prompt_path=args.hotwords,
            )
            status_result = control.status(args.job_id)
            print(
                json.dumps(
                    {
                        "job_id": args.job_id,
                        "attempt": attempt,
                        "status": status_result["status"],
                        "requested_stage": status_result["requested_stage"],
                        "input_transcript_path": status_result[
                            "input_transcript_path"
                        ],
                        "input_transcript_sha256": status_result[
                            "input_transcript_sha256"
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "retry-substate":
            result = control.retry_substate(args.job_id, args.name)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "set-substate":
            result = control.record_substate(
                args.job_id,
                args.name,
                args.status,
                error=args.error,
                attempt_no=args.attempt,
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "stop-after-stage":
            result = control.stop_after_stage(args.job_id)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "cancel":
            result = control.cancel(args.job_id)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "status":
            result = control.status(args.job_id)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"{result['job_id']}  {result['status']}  "
                    f"attempt={result['current_attempt']}"
                )
        elif args.command == "health":
            result = control.health()
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(f"{result['status']}  mode={result['mode']}")
            return {"healthy": 0, "degraded": 1, "unavailable": 2}[result["status"]]
        elif args.command == "complete-minutes":
            result = control.complete_minutes(
                args.job_id, args.archive_dir, attempt_no=args.attempt
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "mark-published":
            result = control.mark_published(
                args.job_id, args.manifest, args.meeting_id
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "mark-draft-modified":
            result = control.mark_draft_modified(args.job_id)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "run-next":
            claim = control.claim_next(worker_id=make_worker_id("relayctl"))
            if claim is None:
                print("null" if args.as_json else "队列为空")
                return 0
            executed = _execute_claim(claim)
            payload = {**claim, "executed": executed}
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"{claim['job_id']}  start={claim['start_stage']}  "
                    f"executed={str(executed).lower()}"
                )
            return 0 if executed else 1
        elif args.command == "run-whisper-retry":
            claim = control.claim_whisper_retry(
                worker_id=f"{make_worker_id('relayctl')}-whisper",
                job_id=args.job_id,
            )
            if claim is None:
                print("null" if args.as_json else "没有可领取的 Whisper 重试")
                return 3
            executed = _execute_whisper_retry_claim(claim)
            payload = {**claim, "executed": executed}
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"{claim['job_id']}  generation={claim['generation']}  "
                    f"executed={str(executed).lower()}"
                )
            return 0 if executed else 1
        elif args.command == "migrate-pending":
            result = control.migrate_pending_archives(args.job_id or None)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"promoted={len(result['promoted'])} "
                    f"errors={len(result['errors'])}"
                )
            return 0 if result["ok"] else 1
        elif args.command == "archive-policy":
            result = control.set_archive_policy(args.job_id, args.policy)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(f"{result['job_id']}  archive_policy={result['archive_policy']}")
        elif args.command == "list":
            result = control.list_jobs(status=args.status, limit=args.limit)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                for item in result:
                    print(
                        f"{item['job_id']}  {item['status']}  "
                        f"attempt={item['current_attempt']}"
                    )
        elif args.command == "check-whisper":
            result = inspect_whisper_ref(args.archive_dir)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(result["status"])
            return 0 if result["status"] == "ready" else 3
    except RelayControlError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
