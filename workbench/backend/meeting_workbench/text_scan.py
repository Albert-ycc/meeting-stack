"""在逐字稿里数「叫法」出现几次（第二期 2b，2d 的文件名比对也用）。

- 比较前两边都做 NFKC、casefold、去空白和标点（和 light_key 同一套），保留每个字在原文里的位置。
- 一遍扫描：每个位置先试最长的叫法，命中就跳过这一段，不重叠；一次出现只记在最长的那个叫法上。
- 叫法开头或结尾是字母时，原文里它前后的字不能也是字母（「ai」不算「said」里的）。
- 每段逐字稿单独扫，不跨段拼接。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any

from .project_profile import MAX_ANCHORS, light_key


def _dropped(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith(("P", "S"))


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """折叠后的文本，和每个折叠后字符在原文里的下标。"""
    folded: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text or ""):
        for piece in unicodedata.normalize("NFKC", char).casefold():
            if _dropped(piece):
                continue
            folded.append(piece)
            positions.append(index)
    return "".join(folded), positions


def _letter(char: str) -> bool:
    folded = unicodedata.normalize("NFKC", char)
    return bool(folded) and folded[0].isascii() and folded[0].isalpha()


class FormScanner:
    """一组叫法的扫描器。forms 里不同写法折叠后相同的算同一个，记在第一个写法上。"""

    def __init__(self, forms: Iterable[str]):
        self.keys: dict[str, str] = {}
        for form in forms:
            key = light_key(form)
            if key and key not in self.keys:
                self.keys[key] = form
        self.lengths = sorted({len(key) for key in self.keys}, reverse=True)

    def _matches(self, text: str) -> list[tuple[str, int, int]]:
        """(叫法, 原文起点, 原文终点) 的列表。"""
        folded, positions = fold_with_map(text)
        found: list[tuple[str, int, int]] = []
        index = 0
        size = len(folded)
        while index < size:
            for length in self.lengths:
                if index + length > size:
                    continue
                piece = folded[index : index + length]
                form = self.keys.get(piece)
                if form is None:
                    continue
                start = positions[index]
                end = positions[index + length - 1]
                if _letter(piece[0]) and start > 0 and _letter(text[start - 1]):
                    continue
                if _letter(piece[-1]) and end + 1 < len(text) and _letter(text[end + 1]):
                    continue
                found.append((form, start, end + 1))
                index += length
                break
            else:
                index += 1
        return found

    def count_text(self, text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for form, _start, _end in self._matches(text):
            counts[form] = counts.get(form, 0) + 1
        return counts

    def scan_segments(self, segments: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """每个叫法：{count, first_ms, anchors_ms（最多 MAX_ANCHORS 段）}；没出现的不返回。"""
        result: dict[str, dict[str, Any]] = {}
        if not self.keys:
            return result
        for segment in segments:
            text = str(segment.get("text") or "")
            if not text:
                continue
            start_ms = int(segment.get("start_ms") or 0)
            for form, _start, _end in self._matches(text):
                entry = result.setdefault(form, {"count": 0, "first_ms": start_ms, "anchors_ms": []})
                entry["count"] += 1
                anchors = entry["anchors_ms"]
                if (not anchors or anchors[-1] != start_ms) and len(anchors) < MAX_ANCHORS:
                    anchors.append(start_ms)
        return result


def appears(form: str, *, segments: Iterable[dict[str, Any]], minutes: str = "") -> bool:
    """这个叫法在逐字稿或纪要里出现过没有。"""
    scanner = FormScanner([form])
    if scanner.count_text(minutes):
        return True
    return bool(scanner.scan_segments(segments))
