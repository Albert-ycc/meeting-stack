# -*- coding: utf-8 -*-
"""切块感知的说话人标签解析（参考实现，标准库 only）。

FunASR 对长音频切块转写后，`*.funasr.json` 顶层是块列表：

    [
      {"chunk": 0, "offset_sec": 0.0,    "result": {"sentence_info": [...]}},
      {"chunk": 1, "offset_sec": 2698.1, "result": {"sentence_info": [...]}},
    ]

每块 `sentence_info[].spk` 是从 0 重新编号的裸整数——块 0 的 spk0 和块 1 的 spk0
完全可能是不同的人。解析时若直接把数字转成 SPEAKER_NN，不同块的同号说话人会拿到
同一个标签，静默合并、无法逆转（详见 docs/speaker-diarization.md）。

本实现的原则：解析层保真，不合并。多块时标签带块前缀（C1_SPEAKER_00），
跨块身份归并留给声纹层。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def parse_chunked_funasr(path: Path) -> list[dict[str, Any]]:
    """解析 funasr json，返回带说话人标签的段列表。

    返回段结构：{"ordinal", "start_ms", "end_ms", "text", "speaker_label"}。
    单块（或无块包装的旧格式）标签为 SPEAKER_NN；多块为 C{n}_SPEAKER_NN。
    """
    top_level = json.loads(path.read_text(encoding="utf-8"))

    # (sentence, offset_ms, chunk_index) 三元组；chunk_index 仅多块时非 None
    raw: list[tuple[dict[str, Any], int, int | None]] = []
    if isinstance(top_level, list) and len(top_level) > 1:
        for index, block in enumerate(top_level):
            chunk_no = block.get("chunk") if isinstance(block, dict) else None
            if not isinstance(chunk_no, int):
                chunk_no = index
            offset_ms = round(float(block.get("offset_sec", 0) or 0) * 1000)
            for sentence in _sentences_of(block):
                raw.append((sentence, offset_ms, chunk_no + 1))
    else:
        blocks = top_level if isinstance(top_level, list) else [top_level]
        for block in blocks:
            offset_ms = round(float(block.get("offset_sec", 0) or 0) * 1000)
            for sentence in _sentences_of(block):
                raw.append((sentence, offset_ms, None))

    segments: list[dict[str, Any]] = []
    for sentence, offset_ms, chunk_index in raw:
        text = str(sentence.get("text") or "").strip()
        if not text:
            continue
        label = _speaker_label(sentence.get("spk"), chunk_index)
        start = int(float(sentence.get("start", 0) or 0))
        end = int(float(sentence.get("end", start) or start))
        segments.append(
            {
                "ordinal": len(segments),
                "start_ms": offset_ms + start,
                "end_ms": offset_ms + end,
                "text": text,
                "speaker_label": label,
            }
        )
    return segments


def _sentences_of(block: Any) -> list[dict[str, Any]]:
    """从一个块里取出 sentence_info 列表，兼容 result 包装层。"""
    if not isinstance(block, dict):
        return []
    payload = block.get("result", block)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    value = payload.get("sentence_info") if isinstance(payload, dict) else None
    if not isinstance(value, list):
        return []
    if not all(isinstance(item, dict) for item in value):
        raise ValueError("sentence_info 含非对象项，文件可能损坏")
    return value


def _speaker_label(spk: Any, chunk_index: int | None) -> str | None:
    if spk is None or str(spk).strip() == "":
        return None
    value = str(spk)
    label = f"SPEAKER_{int(value):02d}" if value.isdigit() else value
    # 多块时必须带块前缀：跨块的同号说话人未必同一人，入库抹平就再也拆不开了
    if chunk_index is not None:
        label = f"C{chunk_index}_{label}"
    return label
