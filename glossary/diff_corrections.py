# -*- coding: utf-8 -*-
"""错字更正提取与项目匹配（参考实现，标准库 only）。

两块逻辑，共同点是「宁可漏，不可误」：

1. diff 反写：用户编辑纪要时把错字改成正确写法，对比新旧 markdown 捕获「替换」操作，
   过滤出疑似错字更正，写进待确认队列（人工确认后才进词典）。过滤是质量命门，要同时
   守住两头——不漏明显错字、不误抓正常编辑。

2. 项目匹配：两层分域下，项目词只在「转写稿命中该项目强特征词」时注入。子串匹配里
   「代表/专家/看板」这类 2 字高频通用词在任何会议都出现，误当成项目信号会把无关项目词
   挤进注入表，所以匹配只用长度 ≥3 的强特征词。

详见 docs/glossary-correction.md。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# 错字更正几乎都发生在词级，只认 2-8 字的短片段
MIN_DIFF_LEN = 2
MAX_DIFF_LEN = 8
# 项目匹配的强特征词最短长度：过滤「代表/专家/看板」这类 2 字弱特征词
MIN_FEATURE_LEN = 3


def _is_candidate(text: str) -> bool:
    """2-8 字的中文/字母词，非纯数字、非纯标点。"""
    if not (MIN_DIFF_LEN <= len(text) <= MAX_DIFF_LEN):
        return False
    if re.fullmatch(r"\d+", text):
        return False
    return bool(re.search(r"[A-Za-z一-鿿]", text))


def _edit_distance(a: str, b: str) -> int:
    """同音/形近字判据用的编辑距离（简单实现，足够词级判断）。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def extract_corrections(
    old_text: str, new_text: str, known_terms: set[str]
) -> list[tuple[str, str]]:
    """从新旧文本提取疑似错字更正，返回 (错写, 正确写法) 列表。

    字符级 difflib 的固有粒度是词根：「树立协会 → 数理协会」会捕获成「树立 → 数理」
    而非整词，确认环节靠上下文兜底；整词粒度要分词器，留作后续。
    """
    matcher = difflib.SequenceMatcher(a=old_text, b=new_text)
    candidates: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        old_piece = unicodedata.normalize("NFKC", old_text[i1:i2]).strip()
        new_piece = unicodedata.normalize("NFKC", new_text[j1:j2]).strip()
        if not _is_candidate(old_piece) or not _is_candidate(new_piece):
            continue
        if old_piece == new_piece:
            continue
        # 判据：编辑距离很近（同音/形近），或新词已是已知正确写法（往权威写法改）
        if _edit_distance(old_piece, new_piece) <= 2 or new_piece in known_terms:
            candidates.append((old_piece, new_piece))
    return candidates


def match_project_scopes(transcript: str, terms: list[dict[str, Any]]) -> set[str]:
    """从转写稿反推这场会属于哪些项目 scope。

    命中规则：某 scope 下任一「长度 ≥3 的强特征词」出现在转写稿里，就判定命中该 scope。
    2 字弱特征词（代表/专家/看板）不参与匹配——误命中的代价（无关项目词挤占注入上限）
    大于漏命中（少纠几个错）。
    """
    hit: set[str] = set()
    for t in terms:
        term = str(t.get("term") or "").strip()
        scope = t.get("scope")
        if scope in (None, "通用"):
            continue
        if len(term) >= MIN_FEATURE_LEN and term in transcript:
            hit.add(scope)
    return hit
