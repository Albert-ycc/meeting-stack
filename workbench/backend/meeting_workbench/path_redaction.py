"""给浏览器看的错误文本里不带本机的目录结构：绝对路径只留文件名。

转写录音页把 relay 的 last_error 当「失败原因」显示。这些文字出自异常的原文（`[Errno 2] No such file or
directory: '/Volumes/…/会议.m4a'`、子进程的参数列表……），带着外置盘、归档目录、暂存目录的真实路径。
这里只改回给浏览器的文字，完整的原文还在 relay 的任务库和日志里。
"""

from __future__ import annotations

import re
from typing import Any

# 绝对路径的起点：/ 前面不能是文字（字母、数字、下划线、中文，\w 认 Unicode）或 / . ~。
# 网址 https://host/a、3/4、and/or、确认/驳回、Retry/重试、./a、../a 的斜杠都不是起点；
# 路径前面得是空白、引号、括号或标点（含全角冒号「：」和英文冒号 missing:/Volumes/…）。
# ~/ 开头的家目录路径也算；file:///… 网址整个换掉。
# 只换至少两级的路径（/Volumes/会议.m4a）：一级的（/tmp、「确认 /驳回」里的 /驳回）没有目录可藏，不动。
_START = re.compile(r"(?:file://|(?<![\w/.~])~?)/(?=[^\s/'\"])")
_NEXT_WORD = re.compile(r" +([^ \t\r\n'\"<>|]+)")
# 一个词里不会出现的字符：空白、引号、<>|
_WORD_BREAK = " \t\r\n'\"<>|"
# 词尾是这些标点，路径就到这里结束（「路径: 原因」「(见 路径)」）
_PATH_END_MARKS = ":;,)]}"
# 没有引号的路径，目录名里可能带空格（「260905 EDC 系统选型」）：往后最多看这几个词，
# 找到还带 / 的词，就当路径没完
_LOOKAHEAD = 4


def _levels(path: str) -> int:
    """路径有几级：按 / 切开，空的不算（/tmp/ 也是一级）。"""
    return len([part for part in path.split("/") if part])


def _quoted_end(text: str, start: int, quote: str) -> int:
    """引号包着的路径：到收尾的引号为止；被截断（没有收尾引号）的，到这一行结束。"""
    end = text.find(quote, start)
    newline = text.find("\n", start)
    if newline != -1 and (end == -1 or newline < end):
        return newline
    return len(text) if end == -1 else end


def _unquoted_end(text: str, start: int) -> int:
    """没有引号的路径到哪里结束：先读到空白为止；目录名里带空格时，下一个词里还有「/」（而且它自己
    不是新路径的开头）就接着读；读到的还只有一级（/驳回）时不往后读，它多半不是路径。没加引号、
    目录名里空格又多过 4 个词的，目录名的后半段会留下来；Python 异常文本里的路径都带引号，不受这个限制。"""
    position = start
    while True:
        while position < len(text) and text[position] not in _WORD_BREAK:
            position += 1
        if position >= len(text) or text[position] != " " or text[position - 1] in _PATH_END_MARKS:
            return position
        if _levels(text[start:position]) < 2:
            return position
        probe = position
        for _ in range(_LOOKAHEAD):
            word = _NEXT_WORD.match(text, probe)
            if word is None or word.group(1).startswith(("/", "~/")):
                return position
            if "/" in word.group(1):
                position = word.end()
                break
            probe = word.end()
        else:
            return position


def _file_name(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    return parts[-1] if parts else path


def redact_paths(text: str) -> str:
    """文字里至少两级的绝对路径（带不带引号都算）换成最后一级的名字，别的字一个不动。"""
    pieces: list[str] = []
    position = 0
    while match := _START.search(text, position):
        start = match.start()
        quote = text[start - 1] if start and text[start - 1] in "'\"" else None
        end = _quoted_end(text, match.end(), quote) if quote else _unquoted_end(text, match.end())
        if _levels(text[match.end() : end]) < 2:
            # 一级的（/tmp、「确认 /驳回」里的 /驳回）：没有目录可藏，按原样放过去
            pieces.append(text[position:end])
        else:
            pieces.append(text[position:start])
            pieces.append(_file_name(text[start:end]))
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def redact_job_paths(job: dict[str, Any]) -> dict[str, Any]:
    """回给浏览器的 relay 任务：失败原因（last_error）和各子状态的 error 里的路径只留文件名。
    这是转写录音页显示的两处文字；audio_path 一类页面不显示的字段不动。不改传进来的对象。"""
    redacted = dict(job)
    if isinstance(redacted.get("last_error"), str):
        redacted["last_error"] = redact_paths(redacted["last_error"])
    substates = redacted.get("substates")
    if isinstance(substates, dict):
        redacted["substates"] = {
            name: (
                {**state, "error": redact_paths(state["error"])}
                if isinstance(state, dict) and isinstance(state.get("error"), str)
                else state
            )
            for name, state in substates.items()
        }
    return redacted
