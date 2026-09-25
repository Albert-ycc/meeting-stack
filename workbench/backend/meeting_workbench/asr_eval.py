from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .gold_schema import (
    GoldSchemaError,
    MAX_REFERENCE_CHARACTERS,
    normalize_number as _normalize_number,
    normalize_text as _normalize,
    validate_gold_sample,
)


MAX_GOLD_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_SAMPLE_CHARACTERS = MAX_REFERENCE_CHARACTERS
HALLUCINATION_LENGTH_RATIO = 1.5
HALLUCINATION_MIN_EXTRA_CHARS = 10
HALLUCINATION_MAX_ALIGNED_RATIO = 0.5
REPETITION_MIN_CONSECUTIVE = 4
REPETITION_MIN_UNIT_CHARS = 2
REPETITION_MAX_UNIT_CHARS = 12
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SAFE_ENGINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_NUMBER_SEMANTIC_CHARACTERS = frozenset(".+-%‰‱/:")
_CHINESE_NUMERAL_CHARACTERS = frozenset(
    "零〇一二两三四五六七八九十百千万亿兆点半壹贰貳叁參肆伍陆陸柒捌玖拾佰仟萬億"
)


class AsrEvaluationError(ValueError):
    pass


def _edit_distance(reference: str, hypothesis: str) -> int:
    if reference == hypothesis:
        return 0
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    if not hypothesis:
        return len(reference)
    previous = list(range(len(hypothesis) + 1))
    for row, reference_char in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_char in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_char != hypothesis_char),
                )
            )
        previous = current
    return previous[-1]


def _is_hallucination(
    reference: str,
    hypothesis: str,
    edit_distance: int,
) -> bool:
    if not hypothesis:
        return False
    extra_characters = len(hypothesis) - len(reference)
    length_ratio = len(hypothesis) / len(reference)
    aligned_ratio = max(0.0, 1.0 - edit_distance / len(hypothesis))
    return (
        extra_characters >= HALLUCINATION_MIN_EXTRA_CHARS
        and length_ratio >= HALLUCINATION_LENGTH_RATIO
        and aligned_ratio <= HALLUCINATION_MAX_ALIGNED_RATIO
    )


def _has_repetition(hypothesis: str) -> bool:
    maximum_unit = min(
        REPETITION_MAX_UNIT_CHARS,
        len(hypothesis) // REPETITION_MIN_CONSECUTIVE,
    )
    for unit_length in range(REPETITION_MIN_UNIT_CHARS, maximum_unit + 1):
        repeated_length = unit_length * REPETITION_MIN_CONSECUTIVE
        for start in range(len(hypothesis) - repeated_length + 1):
            unit = hypothesis[start : start + unit_length]
            if (
                hypothesis[start : start + repeated_length]
                == unit * REPETITION_MIN_CONSECUTIVE
            ):
                return True
    return False


def _load_gold(path: Path) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise AsrEvaluationError(f"金标文件不存在或不安全: {path}")
    if path.stat().st_size > MAX_GOLD_BYTES:
        raise AsrEvaluationError("金标文件超过 64 MiB")
    rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise AsrEvaluationError("无法读取 UTF-8 金标文件") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise AsrEvaluationError(f"金标第 {line_number} 行不是有效 JSON") from error
        try:
            sample = validate_gold_sample(row)
        except GoldSchemaError as error:
            raise AsrEvaluationError(f"金标第 {line_number} 行 {error}") from error
        sample_id = str(sample["id"])
        if sample_id in seen_ids:
            raise AsrEvaluationError(f"金标 id 重复: {sample_id}")
        seen_ids.add(sample_id)
        rows.append(sample)
    if not rows:
        raise AsrEvaluationError("金标文件没有样本")
    return rows


def _read_hypothesis(engine_dir: Path, sample_id: str) -> str | None:
    path = engine_dir / f"{sample_id}.txt"
    if path.is_symlink() or not path.is_file():
        return None
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise AsrEvaluationError(f"候选逐字稿超过 64 MiB: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise AsrEvaluationError(f"无法读取 UTF-8 候选逐字稿: {path.name}") from error


def _annotation_counts(
    values: list[str],
    hypothesis: str,
    vocabulary: set[str],
    *,
    require_numeric_boundaries: bool = False,
) -> tuple[int, int, int]:
    matches = _longest_non_overlapping_matches(
        hypothesis,
        vocabulary,
        require_numeric_boundaries=require_numeric_boundaries,
    )
    expected = Counter(values)
    hits = sum(min(count, matches[value]) for value, count in expected.items())
    return hits, len(values), sum(matches.values())


def _longest_non_overlapping_matches(
    hypothesis: str,
    vocabulary: set[str],
    *,
    require_numeric_boundaries: bool = False,
) -> Counter[str]:
    matches: Counter[str] = Counter()
    occupied = bytearray(len(hypothesis))
    terms_by_length: dict[int, set[str]] = {}
    for value in vocabulary:
        terms_by_length.setdefault(len(value), set()).add(value)

    for length in sorted(terms_by_length, reverse=True):
        terms = terms_by_length[length]
        start = 0
        last_start = len(hypothesis) - length
        while start <= last_start:
            value = hypothesis[start : start + length]
            if value not in terms:
                start += 1
                continue
            end = start + length
            chinese_number = any(
                character in _CHINESE_NUMERAL_CHARACTERS for character in value
            )
            if require_numeric_boundaries and (
                (
                    start > 0
                    and (
                        hypothesis[start - 1].isdigit()
                        or hypothesis[start - 1] in _NUMBER_SEMANTIC_CHARACTERS
                        or (
                            chinese_number
                            and hypothesis[start - 1] in _CHINESE_NUMERAL_CHARACTERS
                        )
                    )
                )
                or (
                    end < len(hypothesis)
                    and (
                        hypothesis[end].isdigit()
                        or hypothesis[end] in _NUMBER_SEMANTIC_CHARACTERS
                        or (
                            chinese_number
                            and hypothesis[end] in _CHINESE_NUMERAL_CHARACTERS
                        )
                    )
                )
            ):
                start += 1
                continue
            if occupied.find(b"\x01", start, end) != -1:
                start += 1
                continue
            occupied[start:end] = b"\x01" * length
            matches[value] += 1
            start = end
    return matches


def _summarize_samples(details: list[dict[str, object]]) -> dict[str, int | float | None]:
    reference_chars = sum(int(detail["reference_chars"]) for detail in details)
    edit_distance = sum(int(detail["edit_distance"]) for detail in details)
    entity_total = sum(int(detail["entity_total"]) for detail in details)
    entity_hits = sum(int(detail["entity_hits"]) for detail in details)
    entity_predictions = sum(int(detail["entity_predictions"]) for detail in details)
    number_total = sum(int(detail["number_total"]) for detail in details)
    number_hits = sum(int(detail["number_hits"]) for detail in details)
    number_predictions = sum(int(detail["number_predictions"]) for detail in details)
    return {
        "sample_count": len(details),
        "missing_samples": sum(bool(detail["missing"]) for detail in details),
        "reference_chars": reference_chars,
        "edit_distance": edit_distance,
        "cer": round(edit_distance / reference_chars, 6),
        "entity_total": entity_total,
        "entity_hits": entity_hits,
        "entity_recall": round(entity_hits / entity_total, 6) if entity_total else None,
        "entity_predictions": entity_predictions,
        "entity_precision": (
            round(entity_hits / entity_predictions, 6) if entity_predictions else None
        ),
        "number_total": number_total,
        "number_hits": number_hits,
        "number_recall": round(number_hits / number_total, 6) if number_total else None,
        "number_predictions": number_predictions,
        "number_precision": (
            round(number_hits / number_predictions, 6) if number_predictions else None
        ),
        "hallucination_samples": sum(
            bool(detail["hallucination"]) for detail in details
        ),
        "repetition_samples": sum(bool(detail["repetition"]) for detail in details),
    }


def evaluate_asr(
    gold_path: str | Path,
    engines: dict[str, str | Path],
) -> dict[str, object]:
    gold = _load_gold(Path(gold_path).expanduser())
    if not engines:
        raise AsrEvaluationError("至少提供一个 --engine NAME=DIR")
    entity_vocabulary = {
        _normalize(str(value)) for sample in gold for value in sample["entities"]
    }
    number_vocabulary = {
        _normalize_number(str(value))
        for sample in gold
        for value in sample["numbers"]
    }
    samples: list[dict[str, object]] = [
        {
            "id": sample["id"],
            "reference": sample["reference"],
            "entities": sample["entities"],
            "numbers": sample["numbers"],
            "tags": sample["tags"],
            "start_ms": sample["start_ms"],
            "end_ms": sample["end_ms"],
            "source_audio_sha256": sample["source_audio_sha256"],
            "engines": {},
        }
        for sample in gold
    ]
    results: dict[str, dict[str, object]] = {}
    for name, raw_directory in engines.items():
        if _SAFE_ENGINE.fullmatch(name) is None:
            raise AsrEvaluationError(f"引擎名称无效: {name}")
        directory = Path(raw_directory).expanduser()
        if directory.is_symlink() or not directory.is_dir():
            raise AsrEvaluationError(f"引擎目录不存在或不安全: {directory}")
        engine_details: list[dict[str, object]] = []
        for sample, output_sample in zip(gold, samples, strict=True):
            sample_id = str(sample["id"])
            reference = _normalize(str(sample["reference"]))
            hypothesis_text = _read_hypothesis(directory, sample_id)
            hypothesis = _normalize(hypothesis_text or "")
            number_hypothesis = _normalize_number(hypothesis_text or "")
            if len(reference) > MAX_SAMPLE_CHARACTERS or len(hypothesis) > MAX_SAMPLE_CHARACTERS:
                raise AsrEvaluationError(f"单样本超过 {MAX_SAMPLE_CHARACTERS} 字符上限")
            distance = _edit_distance(reference, hypothesis)
            entities = [_normalize(str(value)) for value in sample["entities"]]
            numbers = [_normalize_number(str(value)) for value in sample["numbers"]]
            entity_hits, entity_total, entity_predictions = _annotation_counts(
                entities, hypothesis, entity_vocabulary
            )
            number_hits, number_total, number_predictions = _annotation_counts(
                numbers,
                number_hypothesis,
                number_vocabulary,
                require_numeric_boundaries=True,
            )
            detail: dict[str, object] = {
                "hypothesis": hypothesis_text,
                "missing": hypothesis_text is None,
                "reference_chars": len(reference),
                "edit_distance": distance,
                "cer": round(distance / len(reference), 6),
                "entity_hits": entity_hits,
                "entity_total": entity_total,
                "entity_predictions": entity_predictions,
                "number_hits": number_hits,
                "number_total": number_total,
                "number_predictions": number_predictions,
                "hallucination": _is_hallucination(reference, hypothesis, distance),
                "repetition": _has_repetition(hypothesis),
            }
            engine_details.append(detail)
            sample_engines = output_sample["engines"]
            assert isinstance(sample_engines, dict)
            sample_engines[name] = detail

        summary: dict[str, object] = _summarize_samples(engine_details)
        tag_metrics: dict[str, dict[str, int | float | None]] = {}
        for tag in dict.fromkeys(
            str(tag) for sample in gold for tag in sample["tags"]
        ):
            tagged_details = [
                detail
                for sample, detail in zip(gold, engine_details, strict=True)
                if tag in sample["tags"]
            ]
            tag_metrics[tag] = _summarize_samples(tagged_details)
        summary["tag_metrics"] = tag_metrics
        results[name] = summary
    return {
        "schema_version": 2,
        "sample_count": len(gold),
        "engines": results,
        "samples": samples,
    }


def parse_engine_specs(values: list[str]) -> dict[str, Path]:
    engines: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise AsrEvaluationError("--engine 必须使用 NAME=DIR")
        name, raw_directory = value.split("=", 1)
        if not name or not raw_directory or name in engines:
            raise AsrEvaluationError(f"重复或无效的 --engine: {value}")
        engines[name] = Path(raw_directory)
    return engines


def run_qwen_shadow(
    audio_path: str | Path,
    output_dir: str | Path,
    *,
    model: str = "0.6B",
) -> Path:
    audio = Path(audio_path).expanduser()
    if audio.is_symlink() or not audio.is_file():
        raise AsrEvaluationError("影子转写音频不存在或不安全")
    if model != "0.6B":
        raise AsrEvaluationError("Qwen3-ASR 固定使用 0.6B")
    binary = shutil.which("mlx-qwen3-asr")
    if not binary:
        raise AsrEvaluationError("未安装 mlx-qwen3-asr，影子转写未执行")
    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise AsrEvaluationError("影子转写输出目录不能是符号链接")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise AsrEvaluationError("影子转写输出路径不是目录")
    environment = os.environ.copy()
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    environment["DO_NOT_TRACK"] = "1"
    command = [
        binary,
        str(audio),
        "--model",
        f"Qwen/Qwen3-ASR-{model}",
        "--language",
        "Chinese",
        "-f",
        "txt",
        "-o",
        str(output),
        "--quiet",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise AsrEvaluationError(
            "Qwen3-ASR 影子转写失败；请确认程序与模型已在本机离线缓存"
        )
    transcript = output / f"{audio.stem}.txt"
    if transcript.is_symlink() or not transcript.is_file() or transcript.stat().st_size == 0:
        raise AsrEvaluationError("Qwen3-ASR 未生成预期的 TXT 影子稿")
    return transcript
