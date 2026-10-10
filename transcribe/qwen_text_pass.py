#!/usr/bin/env python3
"""Qwen3 文字精转：FunASR 出完主稿后，用 Qwen3-ASR 带本场词典把文字重转一遍。

由 funasr_transcribe.py 在同一进程里调用，只用标准库；Qwen3-ASR 走它自己 venv 里的命令行。

分工（改动前先读）：
  - FunASR（paraformer + cam++）照旧跑第一遍：切块、说话人、挑词用的初稿，Qwen 出问题时用它的原文兜底。
  - 拿 FunASR 初稿按 glossary/injection.py 挑本场词典（和出纪要同一套规则：先认项目，再挑最多 50 条），
    作为 --context 交给 Qwen3-ASR。FunASR 的热词在转写前就要定，那时还不知道是哪个项目的会，
    只能用通用模板，项目专名进不去；第二遍才有初稿可以认项目。
  - 句子按 Qwen 的标点切，起止取它的字级时间戳；说话人取时间上重叠最多的那句 FunASR 的 spk，
    所以切块会议的说话人编号规则（c1-spk0 / c2-spk0）不变。
  - 每个 Qwen 分段（约 30 秒）单独体检：被截断、连续重复、字数和同一时间窗的 FunASR 差太多、
    冒出一串 FunASR 里没有的词典词（Qwen 偶尔把 context 原样吐出来），这一段就退回 FunASR 原文。
    字级时间戳和文字对不上时整场退回 FunASR。
  - 开关：TRANSCRIBE_TEXT_ENGINE=paraformer 关掉这一遍；MEETING_RELAY_QWEN_ASR_BIN 指定命令行位置。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

CUT_PUNCT = set("，。？！；,.?!;")  # 句子在这些标点后断开，和 FunASR 的分句粒度相当
# 按幻觉处理的连续重复：单字连着 8 次以上，或 2–8 字的片段连着 5 遍以上（去掉标点后看）。
# 「对对对，对对」「谢谢谢谢，谢谢」「可以可以可以可以」这类正常口语不算。
REPEAT_RE = re.compile(r"(.)\1{7,}|(.{2,8}?)\2{4,}")
LEAKED_TERM_LIMIT = 5  # 一个分段里冒出这么多条 FunASR 没写到的词典词，判为 context 外溢
MIN_TIMEOUT_SEC = 900
TIMEOUT_PER_AUDIO_SEC = 1.5  # 实测 0.6B 带字级时间戳约 0.26 倍音频时长，留足余量

REPO_ROOT = Path(__file__).resolve().parents[1]
INJECTION_PY = REPO_ROOT / "glossary" / "injection.py"
DEFAULT_QWEN_BIN = Path.home() / ".venvs" / "mlx-qwen3-asr" / "bin" / "mlx-qwen3-asr"
DEFAULT_SNAPSHOT = Path.home() / ".meeting-workbench" / "glossary-snapshot.json"


class QwenPassError(RuntimeError):
    """这一遍做不成，整场用 FunASR 原文。"""


def enabled():
    return os.environ.get("TRANSCRIBE_TEXT_ENGINE", "qwen3").strip().lower() != "paraformer"


def qwen_bin():
    return Path(os.environ.get("MEETING_RELAY_QWEN_ASR_BIN") or DEFAULT_QWEN_BIN).expanduser()


def _content_chars(text):
    return [ch for ch in str(text) if ch.isalnum()]


def select_context(transcript, snapshot_path=None):
    """按 FunASR 初稿挑本场词典，返回（词列表, 认出的项目名）。快照读不到时返回空列表。"""
    spec = importlib.util.spec_from_file_location("meeting_glossary_injection", INJECTION_PY)
    if spec is None or spec.loader is None:
        raise QwenPassError(f"找不到挑词模块 {INJECTION_PY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = snapshot_path or os.environ.get("MEETING_RELAY_GLOSSARY_SNAPSHOT") or DEFAULT_SNAPSHOT
    receipt = module.select_injection(module.load_snapshot(path), transcript, None)
    terms = []
    for entry in receipt.get("terms") or []:
        for text in (entry.get("term"), *(entry.get("also") or [])):
            text = str(text or "").strip()
            if text and text not in terms:
                terms.append(text)
    project = (receipt.get("project") or {}).get("name")
    return terms, project


def run_qwen(wav, context_terms, workdir, timeout_sec):
    """跑 Qwen3-ASR 命令行（带字级时间戳），返回它的 JSON。"""
    binary = qwen_bin()
    if not binary.is_file():
        raise QwenPassError(f"没装 Qwen3-ASR（{binary}）")
    command = [
        str(binary),
        str(wav),
        "--language",
        "Chinese",
        "--output-dir",
        str(workdir),
        "--output-format",
        "json",
        "--timestamps",
        "--no-progress",
    ]
    if context_terms:
        command += ["--context", " ".join(context_terms)]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_sec, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise QwenPassError(f"Qwen3-ASR 超过 {timeout_sec:.0f} 秒没跑完") from error
    output = Path(workdir) / f"{Path(wav).stem}.json"
    if result.returncode != 0 or not output.is_file():
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise QwenPassError(f"Qwen3-ASR 退出码 {result.returncode}：{' / '.join(tail)}")
    try:
        return json.loads(output.read_text(encoding="utf-8"))
    except ValueError as error:
        raise QwenPassError("Qwen3-ASR 的 JSON 读不出来") from error


def split_sentences(qwen):
    """按标点切句，起止取字级时间戳。返回 [{start, end, text, qwen_chunk}]（毫秒）。

    时间戳只覆盖字母和汉字（标点、引号不带时间戳），多字 token（OK、EBU）按字数消费。
    文字和时间戳逐字对不上就抛 QwenPassError——对齐器出错时宁可整场不用。
    """
    tokens = [item for item in qwen.get("segments") or [] if str(item.get("text", "")).strip()]
    sentences, index, used = [], 0, 0
    for chunk_no, chunk in enumerate(qwen.get("chunks") or []):
        current, leading = None, ""
        for ch in str(chunk.get("text") or ""):
            if ch.isspace():
                continue
            if not ch.isalnum():
                if current is not None:
                    current["text"] += ch
                    if ch in CUT_PUNCT:
                        sentences.append(current)
                        current = None
                elif ch not in CUT_PUNCT:
                    leading += ch  # 句首的引号、括号，等下一个字来了一起放进新句
                continue
            if index >= len(tokens):
                raise QwenPassError("字级时间戳比文字少")
            token = tokens[index]
            token_chars = _content_chars(token["text"])
            if used >= len(token_chars) or token_chars[used].casefold() != ch.casefold():
                raise QwenPassError(f"字级时间戳和文字对不上（第 {index} 个时间戳）")
            start_ms = round(float(token["start"]) * 1000)
            end_ms = round(float(token["end"]) * 1000)
            if current is None:
                current = {
                    "start": start_ms,
                    "end": end_ms,
                    "text": leading,
                    "qwen_chunk": chunk_no,
                }
                leading = ""
            current["text"] += ch
            current["end"] = max(current["end"], end_ms)
            used += 1
            if used >= len(token_chars):
                index, used = index + 1, 0
        if current is not None:
            sentences.append(current)
    if index != len(tokens):
        raise QwenPassError("字级时间戳比文字多")
    for sentence in sentences:
        # 起止相同的句子会被纪要计划判为损坏，至少留 10 毫秒
        sentence["end"] = max(sentence["end"], sentence["start"] + 10)
    return sentences


def chunk_problem(chunk, qwen_text, funasr_text, context_terms):
    """一个 Qwen 分段该不该退回 FunASR；该退回时返回原因。"""
    if chunk.get("truncated"):
        return "截断"
    q_chars = "".join(_content_chars(qwen_text))
    f_count = len(_content_chars(funasr_text))
    if REPEAT_RE.search(q_chars):
        return "连续重复"
    if f_count >= 15:
        if len(q_chars) < 0.5 * f_count or len(q_chars) > 1.6 * f_count + 10:
            return f"字数偏差（{len(q_chars)} 对 {f_count}）"
    elif len(q_chars) > f_count + 30:
        return f"字数偏差（{len(q_chars)} 对 {f_count}）"
    leaked = [
        term
        for term in context_terms
        if len(term) >= 2 and term in qwen_text and term not in funasr_text
    ]
    if len(leaked) >= LEAKED_TERM_LIMIT:
        return f"词典外溢（{'、'.join(leaked[:LEAKED_TERM_LIMIT])}…）"
    return None


def _chunk_of(time_ms, chunk_starts_ms):
    chunk = 0
    for i, start in enumerate(chunk_starts_ms):
        if time_ms >= start:
            chunk = i
    return chunk


def _speaker_source(sentence, funasr_sentences, chunk):
    """时间重叠最多的 FunASR 句；没有重叠时取最近的一句（同块优先）。"""
    same_chunk = [item for item in funasr_sentences if item["chunk"] == chunk] or funasr_sentences
    best, best_overlap = None, 0
    for item in same_chunk:
        overlap = min(sentence["end"], item["end"]) - max(sentence["start"], item["start"])
        if overlap > best_overlap:
            best, best_overlap = item, overlap
    if best is not None:
        return best
    middle = (sentence["start"] + sentence["end"]) / 2
    return min(same_chunk, key=lambda item: abs((item["start"] + item["end"]) / 2 - middle))


def merge(funasr_sentences, qwen, chunk_starts_ms, context_terms):
    """把 Qwen 的句子换进来，体检不过的分段保留 FunASR 原句。返回（句子列表, 统计）。

    funasr_sentences 的每一项要带 start / end（毫秒，整场时间）、text、spk（输出用的 c1-spk0 这类）、
    spk_raw（funasr.json 里的原始编号）、chunk（属于 FunASR 第几块）。
    """
    if not funasr_sentences:
        raise QwenPassError("FunASR 没有句子，没法对说话人")
    qwen_sentences = split_sentences(qwen)
    chunks = qwen.get("chunks") or []
    windows = [
        (round(float(chunk.get("start", 0)) * 1000), round(float(chunk.get("end", 0)) * 1000))
        for chunk in chunks
    ]

    def window_of(item):
        middle = (item["start"] + item["end"]) / 2
        for i, (start, end) in enumerate(windows):
            if start <= middle < end or (i == len(windows) - 1 and middle >= start):
                return i
        return None

    funasr_by_window = {}
    for item in funasr_sentences:
        funasr_by_window.setdefault(window_of(item), []).append(item)
    qwen_by_window = {}
    for item in qwen_sentences:
        qwen_by_window.setdefault(item["qwen_chunk"], []).append(item)

    merged, rejected = [], []
    for i, chunk in enumerate(chunks):
        funasr_here = funasr_by_window.get(i, [])
        qwen_here = qwen_by_window.get(i, [])
        problem = chunk_problem(
            chunk,
            "".join(item["text"] for item in qwen_here),
            "".join(item["text"] for item in funasr_here),
            context_terms,
        )
        if problem:
            rejected.append((i, problem))
            merged.extend(dict(item) for item in funasr_here)
            continue
        for item in qwen_here:
            chunk_index = _chunk_of((item["start"] + item["end"]) / 2, chunk_starts_ms)
            source = _speaker_source(item, funasr_sentences, chunk_index)
            prefix = f"c{chunk_index + 1}-" if len(chunk_starts_ms) > 1 else ""
            merged.append(
                {
                    "start": item["start"],
                    "end": item["end"],
                    "text": item["text"],
                    "spk": f"{prefix}spk{source['spk_raw'] if source['spk_raw'] is not None else '?'}",
                    "spk_raw": source["spk_raw"],
                    "chunk": chunk_index,
                }
            )
    # 落在所有 Qwen 分段之外的 FunASR 句子原样保留
    merged.extend(dict(item) for item in funasr_by_window.get(None, []))
    merged = [item for item in merged if item["text"].strip()]
    merged.sort(key=lambda item: (item["chunk"], item["start"], item["end"]))
    stats = {
        "qwen_chunks": len(chunks),
        "rejected": rejected,
        "sentences": len(merged),
    }
    return merged, stats


def _duration_sec(audio):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(audio),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def refine(audio, funasr_sentences, chunk_starts_ms, tmpdir, log, runner=run_qwen):
    """整套第二遍。做不成就返回 None，调用方照用 FunASR 原文；原因写进日志。"""
    if not enabled():
        log("Qwen3 文字精转已关闭（TRANSCRIBE_TEXT_ENGINE=paraformer），用 FunASR 原文")
        return None
    try:
        transcript = "\n".join(item["text"] for item in funasr_sentences)
        terms, project = select_context(transcript)
        log(
            f"Qwen3 文字精转：项目={project or '未认出'}，本场词典 {len(terms)} 词：{' '.join(terms)}"
        )
        workdir = Path(tmpdir) / "qwen3"
        workdir.mkdir(parents=True, exist_ok=True)
        wav = workdir / "full.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
        )
        timeout = max(MIN_TIMEOUT_SEC, TIMEOUT_PER_AUDIO_SEC * _duration_sec(audio))
        qwen = runner(wav, terms, workdir, timeout)
        merged, stats = merge(funasr_sentences, qwen, chunk_starts_ms, terms)
    except Exception as error:
        # 第二遍是锦上添花：Qwen 的 JSON 长得不对、挑词模块出错，都不能挡住 FunASR 主稿交付
        log(f"WARN: Qwen3 文字精转没做成，用 FunASR 原文：{type(error).__name__}: {error}")
        return None
    for chunk_no, reason in stats["rejected"]:
        log(f"Qwen3 分段 {chunk_no + 1} 退回 FunASR 原文：{reason}")
    log(
        f"Qwen3 文字精转完成：{stats['qwen_chunks']} 段里用了 "
        f"{stats['qwen_chunks'] - len(stats['rejected'])} 段，共 {stats['sentences']} 句"
    )
    return merged
