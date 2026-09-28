from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
from typing import Any, Iterable


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+")
_LATIN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+-]*")


def _tokens(pattern: re.Pattern[str], text: str) -> Counter[str]:
    return Counter(match.casefold() for match in pattern.findall(text))


def _overlap_ms(first: dict[str, Any], second: dict[str, Any]) -> int:
    return max(
        0,
        min(int(first["end_ms"]), int(second["end_ms"]))
        - max(int(first["start_ms"]), int(second["start_ms"])),
    )


def align_transcript_segments(
    primary: Iterable[dict[str, Any]],
    candidate: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidate_rows = list(candidate)
    aligned: list[dict[str, Any]] = []
    for source in primary:
        matches = [row for row in candidate_rows if _overlap_ms(source, row) > 0]
        matches.sort(key=lambda row: (int(row["start_ms"]), str(row["id"])))
        candidate_text = "".join(str(row.get("text", "")) for row in matches)
        primary_text = str(source.get("text", ""))
        if not matches:
            risks = ["missing_candidate"]
            similarity = 0.0
        else:
            risks = []
            if _tokens(_LATIN_TERM_RE, primary_text) != _tokens(_LATIN_TERM_RE, candidate_text):
                risks.append("latin_term")
            if _tokens(_NUMBER_RE, primary_text) != _tokens(_NUMBER_RE, candidate_text):
                risks.append("number")
            similarity = SequenceMatcher(
                None, primary_text.casefold(), candidate_text.casefold(), autojunk=False
            ).ratio()
            if not risks and similarity < 0.72:
                risks.append("text")
        aligned.append(
            {
                "primary_segment_id": str(source["id"]),
                "candidate_segment_ids": [str(row["id"]) for row in matches],
                "start_ms": int(source["start_ms"]),
                "end_ms": int(source["end_ms"]),
                "primary_text": primary_text,
                "candidate_text": candidate_text,
                "risk_kinds": risks,
                "similarity": round(similarity, 6),
            }
        )
    return aligned
