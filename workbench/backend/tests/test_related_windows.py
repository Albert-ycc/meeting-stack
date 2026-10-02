"""4d 切窗：只为有段落的时间开窗。正常逐字稿切出来的窗和原来逐个一致；时间戳写坏的一段不会让切窗卡死。"""

import random
import time

from meeting_workbench import related
from meeting_workbench.related import (
    MIN_WINDOW_CHARS,
    STEP_MS,
    WINDOW_CHARS,
    WINDOW_MS,
    Window,
    meaningful,
)


def reference_cut_windows(segments):
    """原来的实现（按时间轴枚举每个窗、每窗扫全部段），只当对照用。"""
    rows = sorted(
        ((int(start), str(text or "")) for start, text in segments), key=lambda item: item[0]
    )
    if not rows:
        return []
    last = rows[-1][0]
    windows = []
    for k in range(last // STEP_MS + 1):
        start = k * STEP_MS
        end = start + WINDOW_MS
        pieces = [
            (start_ms, text) for start_ms, text in rows if start <= start_ms < end and text.strip()
        ]
        if not pieces:
            continue
        parts = []
        buffer = ""
        for start_ms, text in pieces:
            if len(buffer) >= WINDOW_CHARS:
                break
            if buffer:
                buffer += "\n"
            begin = len(buffer)
            buffer += text
            parts.append((begin, min(len(buffer), WINDOW_CHARS), start_ms, text))
        buffer = buffer[:WINDOW_CHARS]
        parts = [part for part in parts if part[0] < len(buffer)]
        count = meaningful(buffer)
        if count < MIN_WINDOW_CHARS:
            continue
        windows.append(Window(start, end, buffer, count, parts))
    return windows


WORDS = [
    "初审规则",
    "报价单",
    "能耗看板",
    "接口",
    "下周",
    "嗯",
    "好的",
    "数据中台",
    "驻场排班",
    "OK",
]


def random_transcript(rng):
    segments = []
    at = rng.choice([0, 0, rng.randrange(0, 600_000)])
    for _ in range(rng.randrange(0, 400)):
        text = "".join(rng.choice(WORDS) for _ in range(rng.randrange(0, 30)))
        if rng.random() < 0.05:
            text = "   "
        segments.append((at, text))
        # 有紧挨着的、同一时刻的、也有隔了几分钟没人说话的
        at += rng.choice([0, 800, 3_000, 9_000, 45_000, 90_000, 240_000])
    rng.shuffle(segments)
    return segments


def test_windows_match_the_old_cut_on_random_transcripts():
    rng = random.Random(20261001)
    for _ in range(300):
        segments = random_transcript(rng)
        assert related.cut_windows(segments) == reference_cut_windows(segments)


def test_a_broken_timestamp_does_not_hang_the_cut():
    segments = [
        (index * 3000, "这是一段会议的发言内容用来测试切窗的速度好不好") for index in range(4000)
    ]
    normal = related.cut_windows(segments)
    broken = segments + [(36_000_000_000, "一段时间戳写坏了的发言" * 6)]

    started = time.perf_counter()
    windows = related.cut_windows(broken)

    assert time.perf_counter() - started < 2
    assert windows[: len(normal)] == normal
    assert [(window.start_ms, window.end_ms) for window in windows[len(normal) :]] == [
        (36_000_000_000 - STEP_MS, 36_000_000_000 + STEP_MS),
        (36_000_000_000, 36_000_000_000 + WINDOW_MS),
    ]
