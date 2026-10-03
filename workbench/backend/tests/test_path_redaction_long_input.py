"""path_redaction 对很长的输入：处理时间随长度线性增长，入口再封一个宽松的长度上限。

原来「一条没加引号的两级路径，后面跟一串带斜杠的词」每读一个词就把读过的整段切出来数一次级数，1MB 跑不完
一分钟；很多带引号的路径、全文没有换行时，每个路径都要往后找换行找到文末。失败原因是给人看的，几 KB 足够，
超出的截掉并标明。口径（中文斜杠、两级路径、引号）由 test_path_redaction.py 钉着，这里只管长度。"""

import re
import time

import pytest

from meeting_workbench import path_redaction
from meeting_workbench.path_redaction import redact_paths

MEGABYTE = 1_000_000
# 改之前最坏的形状 1MB 要跑十分钟上下，线性的在一秒以内；上限给得很宽，慢机器、满负载也不会误报
SECONDS = 15
OMITTED = "\n…（以下省略）"
# 以 / 或 ~/ 开头、至少两级的路径（和 test_path_redaction.py 同一个判据）
TWO_LEVEL_PATH = re.compile(r"(?<![\w/.~])(?:~/|/)[^\s/'\"]+/[^\s'\"]")

WORST_SHAPES = [
    pytest.param(
        "/Volumes/a/b" + " c/d" * (MEGABYTE // 4), "d", id="没加引号的路径后面一串带斜杠的词"
    ),
    pytest.param(
        "'/Volumes/a/b' " * (MEGABYTE // 15),
        "'b' " * (MEGABYTE // 15),
        id="很多带引号的路径、没有换行",
    ),
]


def timed(text: str) -> tuple[str, float]:
    started = time.perf_counter()
    redacted = redact_paths(text)
    return redacted, time.perf_counter() - started


@pytest.mark.parametrize(("text", "expected"), WORST_SHAPES)
def test_the_algorithm_alone_is_linear_on_a_megabyte_of_the_worst_shapes(
    monkeypatch, text, expected
):
    monkeypatch.setattr(path_redaction, "_MAX_CHARS", len(text))  # 放开上限，量算法本身

    redacted, seconds = timed(text)

    assert redacted == expected
    assert seconds < SECONDS


@pytest.mark.parametrize(("text", "expected"), WORST_SHAPES)
def test_a_megabyte_is_cut_to_a_few_kilobytes_and_says_so(text, expected):
    redacted, seconds = timed(text)

    assert seconds < SECONDS
    assert len(redacted) <= path_redaction._MAX_CHARS
    assert redacted.endswith(OMITTED)
    assert not TWO_LEVEL_PATH.search(redacted)
    # 再过一遍不再变：截出来的结果本身不超过上限
    assert redact_paths(redacted) == redacted


def test_a_path_cut_in_half_by_the_limit_still_loses_its_directories():
    path = "'/Volumes/外置中枢/会议纪要与录音/260901 医米会议/vm.m4a'"
    keep = path_redaction._MAX_CHARS - len(OMITTED)
    # 截断点落在「会议纪要与录音」中间
    text = "x" * (keep - len("'/Volumes/外置中枢/会议纪")) + path + " 后面还有很多" * 100

    redacted = redact_paths(text)

    assert redacted.endswith("'会议纪" + OMITTED)
    assert "Volumes" not in redacted and "外置中枢" not in redacted
    assert redact_paths(redacted) == redacted


@pytest.mark.parametrize("tail", ["/Volumes/", "'/Volumes/"])
def test_a_one_level_path_left_at_the_cut_does_not_swallow_the_mark(tail):
    # 截断点前面剩下一级的 /Volumes/：省略的字要另起一行，不能被当成它的下一级
    keep = path_redaction._MAX_CHARS - len(OMITTED)
    text = "x" * (keep - len(tail) - 1) + " " + tail + "外置中枢/会议.m4a" * 10

    redacted = redact_paths(text)

    assert redacted.endswith(tail + OMITTED)
    assert redact_paths(redacted) == redacted


def test_text_within_the_limit_is_not_cut():
    text = "x" * (path_redaction._MAX_CHARS - 40) + " '/Volumes/外置中枢/会议.m4a'"

    assert redact_paths(text) == "x" * (path_redaction._MAX_CHARS - 40) + " '会议.m4a'"
