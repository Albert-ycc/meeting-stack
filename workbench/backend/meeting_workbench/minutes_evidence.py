from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any


MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_SRT_BYTES = 64 * 1024 * 1024
MINUTES_STRATEGIES = {"single_pass", "topic_hierarchical", "multi_stage"}
MINUTES_ITEM_KINDS = {
    "fact",
    "decision",
    "action",
    "risk",
    "open_question",
    "number",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ANCHOR_RE = re.compile(r"\[(\d{2}):(\d{2}):(\d{2})\]")
_SRT_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$"
)


class MinutesEvidenceError(ValueError):
    pass


def manifest_meeting_id_matches(manifest: dict[str, Any], meeting_id: str) -> bool:
    """判定 manifest 声明的 meeting_id 是否与目标会议一致。

    relay 写侧从不写这个字段——真实归档 manifest 用 vm-/fp- 前缀的内容派生
    身份代替显式声明，导入器与证据加载器都必须认这条口径，否则会把「manifest
    没写这个字段」误判成「身份不一致」。字段存在时按精确匹配，不做归一化。
    """
    declared = manifest.get("meeting_id")
    if declared is None:
        return meeting_id.startswith(("vm-", "fp-"))
    return declared == meeting_id


def relay_attempt_matches_minutes(
    relay_attempt: dict[str, Any],
    *,
    requested_stage: str | None,
    input_transcript_sha256: str | None,
) -> bool:
    """判定纪要版本与 relay attempts 表里的记录是否同源。

    relay 对全流程 attempt 记 requested_stage="discovered"，而 manifest 与 minutes_versions
    只在纪要重生成（minutes_generating）时才写这个字段；库里为 None 表示「没声明」，
    只能跳过、不能当成不一致，否则首轮生成的纪要永远拿不到可验证证据。
    """
    if relay_attempt.get("minutes_protocol_version") != 3:
        return False
    if requested_stage is not None and relay_attempt.get("requested_stage") != requested_stage:
        return False
    return relay_attempt.get("input_transcript_sha256") == input_transcript_sha256


def select_source_srt_entry(
    entries: dict[str, dict[str, Any]],
    *,
    expected_sha256: str,
    requested_stage: str | None,
) -> dict[str, Any] | None:
    """从 manifest 登记项里挑出纪要来源 SRT。

    首轮 attempt 会把同一份 SRT 同时写成 `vm-*.srt` 与 `input-transcript.srt`，哈希相同就是
    同一来源，不算「不唯一」；纪要重生成（minutes_generating）的来源必须是快照
    `input-transcript.srt`。没有匹配项时返回 None。
    """
    candidates = [
        entry
        for relative, entry in entries.items()
        if Path(relative).parent == Path(".")
        and Path(relative).suffix.casefold() == ".srt"
        and entry["sha256"] == expected_sha256
    ]
    snapshot = next(
        (entry for entry in candidates if entry["path"] == "input-transcript.srt"), None
    )
    if requested_stage == "minutes_generating":
        return snapshot
    return snapshot or (candidates[0] if candidates else None)


def is_manifest_exempt_path(relative: Path | str) -> bool:
    """whisper-ref/ 下的产物是异步子状态：manifest 落盘后 whisper.log 还在
    追加。写侧（relay_control 的归档校验）本来就跳过它，导入器与证据加载器
    的完整性比对必须同口径，否则长录音的归档目录会被永久判成无效 manifest。
    """
    parts = Path(relative).parts
    return bool(parts) and parts[0] == "whisper-ref"


def _number(value: Any) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise MinutesEvidenceError("纪要证据结构无效")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MinutesEvidenceError("纪要证据结构无效")
    return value


def _trusted_bytes(
    path: Path,
    *,
    expected_sha256: str | None,
    label: str,
    maximum_bytes: int = MAX_EVIDENCE_BYTES,
) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise MinutesEvidenceError(f"{label}不可用")
    if path.stat().st_size <= 0 or path.stat().st_size > maximum_bytes:
        raise MinutesEvidenceError(f"{label}大小无效")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MinutesEvidenceError(f"{label}不可用") from error
    if expected_sha256 is not None:
        if _SHA256_RE.fullmatch(expected_sha256) is None:
            raise MinutesEvidenceError(f"{label}缺少可信哈希")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise MinutesEvidenceError(f"{label}哈希不匹配")
    return raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MinutesEvidenceError("纪要 manifest 产物不可用") from error
    return digest.hexdigest()


def load_minutes_manifest(
    path: Path,
    *,
    expected_sha256: str,
    meeting_id: str,
    source_job_id: str,
    source_attempt: int,
    requested_stage: str | None,
    input_transcript_sha256: str | None,
) -> dict[str, Any]:
    if _SHA256_RE.fullmatch(expected_sha256 or "") is None:
        raise MinutesEvidenceError("纪要 manifest 缺少可信哈希")
    raw = _trusted_bytes(
        path,
        expected_sha256=expected_sha256,
        label="纪要 manifest",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MinutesEvidenceError("纪要 manifest 无效") from error
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or type(payload.get("minutes_protocol_version")) is not int
        or payload.get("minutes_protocol_version") != 3
        or not manifest_meeting_id_matches(payload, meeting_id)
        or payload.get("job_id") != source_job_id
        or type(payload.get("attempt")) is not int
        or payload.get("attempt") != source_attempt
        or payload.get("requested_stage") != requested_stage
        or payload.get("input_transcript_sha256") != input_transcript_sha256
    ):
        raise MinutesEvidenceError("纪要 manifest 来源不一致")
    raw_entries = payload.get("artifacts")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise MinutesEvidenceError("纪要 manifest 产物清单无效")
    root = path.parent
    entries: dict[str, dict[str, Any]] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise MinutesEvidenceError("纪要 manifest 产物清单无效")
        relative_value = entry.get("path")
        size_bytes = entry.get("bytes")
        sha256 = entry.get("sha256")
        if (
            not isinstance(relative_value, str)
            or not relative_value
            or "\\" in relative_value
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(sha256, str)
            or _SHA256_RE.fullmatch(sha256) is None
        ):
            raise MinutesEvidenceError("纪要 manifest 产物清单无效")
        relative_path = Path(relative_value)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.as_posix() != relative_value
            or relative_value in entries
        ):
            raise MinutesEvidenceError("纪要 manifest 产物路径无效")
        if is_manifest_exempt_path(relative_path):
            # whisper-ref/ 是异步子状态，落盘后仍在追加，不参与哈希校验。
            continue
        artifact_path = root / relative_path
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise MinutesEvidenceError("纪要 manifest 产物不可用")
        try:
            resolved = artifact_path.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise MinutesEvidenceError("纪要 manifest 产物路径无效") from error
        if artifact_path.stat().st_size != size_bytes:
            raise MinutesEvidenceError("纪要 manifest 产物大小不一致")
        if _sha256_file(artifact_path) != sha256:
            raise MinutesEvidenceError("纪要 manifest 产物哈希不一致")
        entries[relative_value] = {
            "path": relative_value,
            "bytes": size_bytes,
            "sha256": sha256,
        }
    try:
        actual_entries = list(root.rglob("*"))
    except OSError as error:
        raise MinutesEvidenceError("纪要 manifest 目录不可用") from error
    if any(candidate.is_symlink() for candidate in actual_entries):
        raise MinutesEvidenceError("纪要 manifest 目录包含符号链接")
    # 登记的产物已经在上面逐条校验过存在性与哈希；目录里多出未登记的文件
    # （用户笔记、事后纠错留下的旁路文件）不是证据链缺陷，不在这里拒收。
    return {"payload": payload, "entries": entries}


def load_relay_attempt(path: Path, *, job_id: str, attempt: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MinutesEvidenceError("暂无可验证证据：Relay attempt 不可用")
    try:
        resolved = path.resolve(strict=True)
        with closing(
            sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=2)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT requested_stage, input_transcript_sha256,
                          source_srt_sha256, minutes_plan_sha256,
                          minutes_protocol_version
                     FROM attempts WHERE job_id=? AND attempt_no=?""",
                (job_id, attempt),
            ).fetchone()
    except (OSError, sqlite3.Error) as error:
        raise MinutesEvidenceError("暂无可验证证据：Relay attempt 不可用") from error
    if row is None:
        raise MinutesEvidenceError("暂无可验证证据：Relay attempt 不存在")
    return dict(row)


def _seconds_from_srt_parts(parts: tuple[str, ...]) -> float | None:
    hours, minutes, seconds, milliseconds = (int(part) for part in parts)
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _parse_source_srt(path: Path, *, expected_sha256: str) -> list[dict[str, Any]]:
    raw = _trusted_bytes(
        path,
        expected_sha256=expected_sha256,
        label="纪要来源逐字稿",
        maximum_bytes=MAX_SOURCE_SRT_BYTES,
    )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise MinutesEvidenceError("纪要来源逐字稿不是有效 UTF-8") from error
    cues: list[dict[str, Any]] = []
    previous_end = 0.0
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    for sequence, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise MinutesEvidenceError("纪要来源逐字稿结构无效")
        timing = _SRT_TIME_RE.fullmatch(lines[1].strip())
        if timing is None:
            raise MinutesEvidenceError("纪要来源逐字稿时间无效")
        start = _seconds_from_srt_parts(tuple(timing.groups()[:4]))
        end = _seconds_from_srt_parts(tuple(timing.groups()[4:]))
        cue_text = "\n".join(lines[2:]).strip()
        if (
            start is None
            or end is None
            or start < previous_end
            or end <= start
            or not cue_text
        ):
            raise MinutesEvidenceError("纪要来源逐字稿结构无效")
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
        raise MinutesEvidenceError("纪要来源逐字稿为空")
    return cues


def _load_plan(
    path: Path,
    *,
    expected_sha256: str,
    expected_source_srt_sha256: str,
    expected_input_transcript_sha256: str | None,
    source_srt_path: Path,
) -> tuple[dict[str, Any], set[tuple[str, str, float, float]]]:
    raw = _trusted_bytes(
        path,
        expected_sha256=expected_sha256,
        label="纪要计划文件",
    )
    try:
        plan = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MinutesEvidenceError("纪要计划不是有效 UTF-8 JSON") from error
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or plan.get("minutes_protocol_version") != 3
        or not isinstance(plan.get("source_srt_sha256"), str)
        or _SHA256_RE.fullmatch(plan["source_srt_sha256"]) is None
        or not isinstance(plan.get("cue_count"), int)
        or isinstance(plan.get("cue_count"), bool)
        or plan["cue_count"] <= 0
        or not isinstance(plan.get("windows"), list)
        or not plan["windows"]
    ):
        raise MinutesEvidenceError("纪要计划协议无效")
    input_transcript_sha256 = plan.get("input_transcript_sha256")
    if input_transcript_sha256 is not None and (
        not isinstance(input_transcript_sha256, str)
        or _SHA256_RE.fullmatch(input_transcript_sha256) is None
    ):
        raise MinutesEvidenceError("纪要计划协议无效")
    if (
        plan["source_srt_sha256"] != expected_source_srt_sha256
        or input_transcript_sha256 != expected_input_transcript_sha256
    ):
        raise MinutesEvidenceError("纪要计划来源哈希不一致")
    total_duration = _number(plan.get("total_duration_sec"))
    if total_duration <= 0:
        raise MinutesEvidenceError("纪要计划时间无效")
    cue_bindings: set[tuple[str, str, float, float]] = set()
    planned_cues: list[dict[str, Any]] = []
    cue_indices: list[int] = []
    window_ids: set[str] = set()
    previous_window_end = 0.0
    for window in plan["windows"]:
        if not isinstance(window, dict) or not isinstance(window.get("cues"), list):
            raise MinutesEvidenceError("纪要计划窗口无效")
        window_id = _text(window.get("window_id"))
        start_sec = _number(window.get("start_sec"))
        end_sec = _number(window.get("end_sec"))
        if (
            window_id in window_ids
            or abs(float(start_sec) - previous_window_end) > 0.001
            or end_sec <= start_sec
            or end_sec > total_duration
            or not window["cues"]
            or (
                total_duration >= 480
                and not 480 <= float(end_sec) - float(start_sec) <= 720
            )
            or (
                total_duration < 480
                and (
                    len(plan["windows"]) != 1
                    or abs(float(start_sec)) > 0.001
                    or abs(float(end_sec) - float(total_duration)) > 0.001
                )
            )
        ):
            raise MinutesEvidenceError("纪要计划窗口无效")
        window_ids.add(window_id)
        previous_window_end = float(end_sec)
        previous_cue_end = float(start_sec)
        for cue in window["cues"]:
            if not isinstance(cue, dict):
                raise MinutesEvidenceError("纪要计划来源无效")
            cue_index = cue.get("cue_index")
            cue_start = _number(cue.get("source_start_sec"))
            cue_end = _number(cue.get("source_end_sec"))
            digest = cue.get("source_text_sha256")
            binding = (
                window_id,
                digest,
                float(cue_start),
                float(cue_end),
            ) if isinstance(digest, str) else None
            if (
                not isinstance(cue_index, int)
                or isinstance(cue_index, bool)
                or cue_start < start_sec
                or cue_start < previous_cue_end
                or cue_end <= cue_start
                or cue_end > end_sec
                or not isinstance(digest, str)
                or _SHA256_RE.fullmatch(digest) is None
                or binding in cue_bindings
            ):
                raise MinutesEvidenceError("纪要计划来源无效")
            cue_indices.append(cue_index)
            previous_cue_end = float(cue_end)
            cue_bindings.add((window_id, digest, float(cue_start), float(cue_end)))
            planned_cues.append(
                {
                    "cue_index": cue_index,
                    "source_start_sec": float(cue_start),
                    "source_end_sec": float(cue_end),
                    "source_text_sha256": digest,
                }
            )
    if (
        abs(previous_window_end - float(total_duration)) > 0.001
        or cue_indices != list(range(1, plan["cue_count"] + 1))
    ):
        raise MinutesEvidenceError("纪要计划覆盖无效")
    if _parse_source_srt(
        source_srt_path, expected_sha256=expected_source_srt_sha256
    ) != planned_cues:
        raise MinutesEvidenceError("纪要计划与来源逐字稿不一致")
    return plan, cue_bindings


def _anchor_seconds(anchor: str) -> int | None:
    match = _ANCHOR_RE.fullmatch(anchor)
    if match is None:
        return None
    hours, minutes, seconds = (int(value) for value in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def load_minutes_evidence(
    path: Path,
    *,
    expected_sha256: str,
    plan_path: Path,
    expected_plan_sha256: str | None,
    minutes_path: Path,
    expected_minutes_sha256: str | None,
    source_srt_path: Path,
    expected_source_srt_sha256: str,
    expected_input_transcript_sha256: str | None,
) -> dict[str, Any]:
    if not isinstance(expected_sha256, str) or _SHA256_RE.fullmatch(expected_sha256) is None:
        raise MinutesEvidenceError("纪要证据缺少可信哈希")
    raw = _trusted_bytes(
        path,
        expected_sha256=expected_sha256,
        label="纪要证据文件",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MinutesEvidenceError("纪要证据不是有效 UTF-8 JSON") from error
    if isinstance(payload, dict) and payload.get("minutes_protocol_version") != 3:
        raise MinutesEvidenceError("暂无可验证证据：仅支持 v3 证据协议")
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or type(payload.get("minutes_protocol_version")) is not int
        or payload.get("strategy") not in MINUTES_STRATEGIES
    ):
        raise MinutesEvidenceError("纪要证据协议无效")
    plan, plan_cue_bindings = _load_plan(
        plan_path,
        expected_sha256=expected_plan_sha256 or "",
        expected_source_srt_sha256=expected_source_srt_sha256,
        expected_input_transcript_sha256=expected_input_transcript_sha256,
        source_srt_path=source_srt_path,
    )
    minutes_raw = _trusted_bytes(
        minutes_path,
        expected_sha256=expected_minutes_sha256,
        label="对应纪要文件",
    )
    try:
        minutes_markdown = minutes_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MinutesEvidenceError("对应纪要不是有效 UTF-8 文本") from error
    coverage = payload.get("coverage")
    topics = payload.get("topics")
    if not isinstance(coverage, dict) or not isinstance(topics, list) or not topics:
        raise MinutesEvidenceError("纪要证据结构无效")
    clean_coverage: dict[str, int] = {}
    for key in ("total_items", "included_items", "omitted_items"):
        value = coverage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MinutesEvidenceError("纪要证据覆盖率无效")
        clean_coverage[key] = value

    clean_topics: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    topic_ids: set[str] = set()
    item_ids: set[str] = set()
    bound_cues: set[tuple[str, str, float, float]] = set()
    previous_topic_end = -1.0
    total = included = omitted = 0
    for topic in topics:
        if not isinstance(topic, dict) or not isinstance(topic.get("items"), list):
            raise MinutesEvidenceError("纪要证据议题无效")
        topic_id = _text(topic.get("topic_id"))
        title = _text(topic.get("title"))
        if topic_id in topic_ids or not topic["items"]:
            raise MinutesEvidenceError("纪要证据议题无效")
        topic_ids.add(topic_id)
        start_sec = _number(topic.get("start_sec"))
        end_sec = _number(topic.get("end_sec"))
        if (
            start_sec < 0
            or end_sec < start_sec
            or end_sec > plan["total_duration_sec"]
            or float(start_sec) < previous_topic_end
        ):
            raise MinutesEvidenceError("纪要证据议题时间无效")
        previous_topic_end = float(end_sec)
        clean_topic: dict[str, Any] = {
            "topic_id": topic_id,
            "title": title,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "items": [],
        }
        previous_item_end = float(start_sec)
        for item in topic["items"]:
            if not isinstance(item, dict):
                raise MinutesEvidenceError("纪要证据事项无效")
            item_id = _text(item.get("item_id"))
            kind = item.get("kind")
            text = _text(item.get("text"))
            status = item.get("status")
            if item_id in item_ids or kind not in MINUTES_ITEM_KINDS:
                raise MinutesEvidenceError("纪要证据事项无效")
            if status not in {"included", "omitted"}:
                raise MinutesEvidenceError("纪要证据事项状态无效")
            item_ids.add(item_id)
            source_start_sec = _number(item.get("source_start_sec"))
            source_end_sec = _number(item.get("source_end_sec"))
            if (
                source_start_sec < 0
                or source_end_sec < source_start_sec
                or float(source_start_sec) < float(start_sec)
                or float(source_end_sec) > float(end_sec)
                or float(source_start_sec) < previous_item_end
            ):
                raise MinutesEvidenceError("纪要证据事项时间无效")
            previous_item_end = float(source_end_sec)
            clean_item: dict[str, Any] = {
                "item_id": item_id,
                "kind": kind,
                "text": text,
                "status": status,
                "source_start_sec": source_start_sec,
                "source_end_sec": source_end_sec,
            }
            for key in ("owner", "deadline"):
                if isinstance(item.get(key), str):
                    clean_item[key] = item[key]
            if kind == "action" and (
                not isinstance(item.get("owner"), str)
                or not item["owner"].strip()
                or not isinstance(item.get("deadline"), str)
                or not item["deadline"].strip()
            ):
                raise MinutesEvidenceError("纪要证据行动项缺少负责人或截止时间")
            source_window_id = _text(item.get("source_window_id"))
            source_text_sha256 = item.get("source_text_sha256")
            if (
                not isinstance(source_text_sha256, str)
                or _SHA256_RE.fullmatch(source_text_sha256) is None
            ):
                raise MinutesEvidenceError("纪要证据事项缺少来源锚点")
            binding = (
                source_window_id,
                source_text_sha256,
                float(source_start_sec),
                float(source_end_sec),
            )
            if binding not in plan_cue_bindings or binding in bound_cues:
                raise MinutesEvidenceError("纪要证据事项与计划来源不一致")
            bound_cues.add(binding)
            clean_item.update(
                {
                    "source_window_id": source_window_id,
                    "source_text_sha256": source_text_sha256,
                }
            )
            if status == "included":
                minutes_anchor = item.get("minutes_anchor")
                anchor_seconds = (
                    _anchor_seconds(minutes_anchor)
                    if isinstance(minutes_anchor, str)
                    else None
                )
                if (
                    anchor_seconds is None
                    or abs(anchor_seconds - float(source_start_sec)) > 5
                    or minutes_anchor not in minutes_markdown
                ):
                    raise MinutesEvidenceError("纪要证据包含项缺少来源锚点")
                clean_item.update(
                    {
                        "minutes_anchor": minutes_anchor,
                    }
                )
                anchors.append(
                    {
                        "item_id": item_id,
                        "minutes_anchor": minutes_anchor,
                        "source_start_sec": source_start_sec,
                        "source_end_sec": source_end_sec,
                    }
                )
                included += 1
            else:
                omitted_reason = _text(item.get("omitted_reason"))
                clean_item["omitted_reason"] = omitted_reason
                omitted += 1
            clean_topic["items"].append(clean_item)
            total += 1
        clean_topics.append(clean_topic)
    if clean_coverage != {
        "total_items": total,
        "included_items": included,
        "omitted_items": omitted,
    }:
        raise MinutesEvidenceError("纪要证据覆盖率不一致")
    return {"coverage": clean_coverage, "topics": clean_topics, "anchors": anchors}
