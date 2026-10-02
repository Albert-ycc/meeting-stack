"""日志里的异常：只留类型名和出错的位置，不带异常消息，也不带局部变量。

把会议内容、提示词交给 AI 的后台循环和任务，意料之外的异常消息里可能带着那段文字（解析出错的片段、
KeyError 的键、字符串拼接失败时的内容），logger.exception 会把消息连同整段 traceback 一起写进日志。
这里只留异常类型名和调用栈上最靠近出错处的几帧（文件:行:函数），够定位问题，不泄露会议内容。
qwen_shadow 的循环早先就只记类型名，这是同一个做法加上位置。用法：
    except Exception as error:
        logger.error("某一步失败：%s", describe_error(error))
（不要 logger.exception，也不要带 exc_info。）
"""

from __future__ import annotations

import traceback
from pathlib import Path

MAX_FRAMES = 3


def describe_error(error: BaseException, *, frames: int = MAX_FRAMES) -> str:
    """「类型名 @ 出错处 < 它的调用者 < …」，最多 frames 帧，从出错那一帧往外排。

    文件名只留最后两段（meeting_workbench/links_llm.py），行号和函数名取调用栈上的记录；
    不读源码行、不取局部变量，异常消息一个字也不碰。没有调用栈（异常对象从没抛出过）时位置写问号。
    """
    stack = traceback.extract_tb(error.__traceback__)
    places = [
        f"{'/'.join(Path(item.filename).parts[-2:])}:{item.lineno if item.lineno else '?'}:{item.name}"
        for item in reversed(stack[-frames:])
    ]
    return f"{type(error).__name__} @ {' < '.join(places) if places else '?'}"
