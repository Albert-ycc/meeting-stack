from __future__ import annotations

import hashlib
import json
import unicodedata


MAX_HOTWORDS = 20
MAX_HOTWORD_CHARACTERS = 80


class HotwordValidationError(ValueError):
    pass


def normalize_hotwords(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_HOTWORDS:
        raise HotwordValidationError(f"热词最多 {MAX_HOTWORDS} 个")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise HotwordValidationError("热词必须是字符串")
        term = unicodedata.normalize("NFKC", value).strip()
        if not term or len(term) > MAX_HOTWORD_CHARACTERS:
            raise HotwordValidationError("热词长度必须为 1-80 个字符")
        if any(unicodedata.category(character).startswith("C") for character in term):
            raise HotwordValidationError("热词不能包含控制字符")
        identity = term.casefold()
        if identity not in seen:
            seen.add(identity)
            normalized.append(term)
    return normalized


def hotword_audit(values: list[str]) -> dict[str, object]:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"count": len(values), "sha256": hashlib.sha256(encoded).hexdigest()}
