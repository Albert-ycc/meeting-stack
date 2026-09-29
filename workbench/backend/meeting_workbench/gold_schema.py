from __future__ import annotations

import re
import unicodedata
from typing import Any


GOLD_SCHEMA_VERSION = 2
MAX_REFERENCE_CHARACTERS = 4_000
MAX_TERMS_PER_FIELD = 100
MAX_TERM_CHARACTERS = 256
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[A-Fa-f0-9]{64}")
_NUMBER_SEMANTIC_CHARACTERS = frozenset(".+-%‰‱/:")


class GoldSchemaError(ValueError):
    pass


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith(("P", "Z"))
    )


def normalize_number(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("−", "-")
    return "".join(
        character
        for character in normalized
        if character in _NUMBER_SEMANTIC_CHARACTERS
        or (not character.isspace() and not unicodedata.category(character).startswith(("P", "Z")))
    )


def _terms(row: dict[str, Any], field: str) -> list[str]:
    values = row.get(field, [])
    if not isinstance(values, list) or len(values) > MAX_TERMS_PER_FIELD:
        raise GoldSchemaError(f"{field} 无效")
    canonical: list[str] = []
    seen: set[str] = set()
    normalizer = normalize_number if field == "numbers" else normalize_text
    for value in values:
        if not isinstance(value, str):
            raise GoldSchemaError(f"{field} 无效")
        term = unicodedata.normalize("NFKC", value).strip()
        normalized = normalizer(term)
        if not normalized or len(term) > MAX_TERM_CHARACTERS:
            raise GoldSchemaError(f"{field} 无效")
        if normalized in seen:
            raise GoldSchemaError(f"{field} 规范化后重复")
        seen.add(normalized)
        canonical.append(term)
    return canonical


def validate_gold_sample(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise GoldSchemaError("必须是对象")
    if "schema_version" in row and (
        type(row["schema_version"]) is not int or row["schema_version"] != GOLD_SCHEMA_VERSION
    ):
        raise GoldSchemaError("schema_version 无效")
    sample_id = row.get("id")
    if not isinstance(sample_id, str) or _SAFE_ID.fullmatch(sample_id) is None:
        raise GoldSchemaError("id 无效")
    reference = row.get("reference")
    if not isinstance(reference, str):
        raise GoldSchemaError("reference 为空")
    reference = unicodedata.normalize("NFKC", reference).strip()
    if not normalize_text(reference):
        raise GoldSchemaError("reference 为空")
    if len(reference) > MAX_REFERENCE_CHARACTERS:
        raise GoldSchemaError("单样本 reference 超过长度上限")

    has_start = "start_ms" in row
    has_end = "end_ms" in row
    if has_start != has_end:
        raise GoldSchemaError("start_ms/end_ms 必须同时提供")
    start_ms = row.get("start_ms")
    end_ms = row.get("end_ms")
    if has_start and (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
        or start_ms < 0
        or end_ms <= start_ms
    ):
        raise GoldSchemaError("时间范围无效")

    has_source_sha = "source_audio_sha256" in row
    source_sha = row.get("source_audio_sha256")
    if has_source_sha and (
        not isinstance(source_sha, str) or _SHA256.fullmatch(source_sha) is None
    ):
        raise GoldSchemaError("source_audio_sha256 无效")
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "id": sample_id,
        "reference": reference,
        "entities": _terms(row, "entities"),
        "numbers": _terms(row, "numbers"),
        "tags": _terms(row, "tags"),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "source_audio_sha256": source_sha.lower() if isinstance(source_sha, str) else None,
    }
