"""任务截止（项目页与待办改版 261001，R07-2）：会后 AI 抽任务时顺带抽截止，精确到日。

AI 从纪要的「截止：××」或任务原文里的时间说法抽，原文说法记进 due_phrase、换算出的日子记进 due_date；
「明天」「28 号」「下周三」这类相对说法以这场会的开会日期为基准换算。换算不靠 AI 心算星期：提示词里
直接给出开会日期是星期几、本周和下周每天的日子。AI 给的日子这里再核一遍，不合法、早于开会日期、
离开会超过一年的一律当作没抽到，归「未定截止」。

存量任务不回填，截止只对上线后新抽出的任务生效。

开会日期按会上说话人的日历（北京时间）取，不按本机（录音时间混着 -07:00 和 +00:00 两种写法，一律换成
北京时间再取日子）：声档跑在太平洋时区的 Mac 上，会议却集中在北京时间
白天开，生产库 212 场会里 137 场两边日期不同、20 场连周都不同（太平洋周日晚上是北京周一上午）。会上说的
「明天」「本周五」是北京日历上的，09-13 那场逐字稿原话就是「今天……十四号」（太平洋还是 13 号）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# AI 给的日子最远离开会多久，超过的当作换算错了
MAX_DUE_DAYS = 366
# 原文说法最多留多长：只是给人核对换算的依据，不是正文
MAX_PHRASE_CHARS = 40
# 会上说话人的时区：「明天」「本周」按这个日历换算
SPEAKER_TZ = ZoneInfo("Asia/Shanghai")
# 手动填的截止只收这个范围，挡住 0001-01-01、9999-12-31 这类手滑
MANUAL_DUE_RANGE = (date(2000, 1, 1), date(2099, 12, 31))
WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def speaker_date(value: str | None) -> date | None:
    """ISO 时间换成会上说话人日历（北京时间）的日期；不带时区的按本机时间理解再换。解析不了给 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(SPEAKER_TZ).date()


def meeting_date(meeting: dict[str, Any] | None) -> date | None:
    """开会日期：录音时间在说话人日历上的日子，没有录音时间时退回会议建档时间。"""
    if not meeting:
        return None
    return speaker_date(meeting.get("recording_date")) or speaker_date(meeting.get("created_at"))


def _day(value: date) -> str:
    return f"{value.isoformat()}（周{WEEKDAYS[value.weekday()]}）"


def prompt_rules(base: date | None) -> str:
    """抽取提示词里讲截止的一段。没有开会日期时不让 AI 换算，截止一律留空。"""
    if base is None:
        return (
            "- 截止：这场会没有开会日期，due_phrase 照抄原文里的时间说法（没有写 null），"
            "due_date 一律写 null。\n"
        )
    monday = base - timedelta(days=base.weekday())
    this_week = "、".join(_day(monday + timedelta(days=offset)) for offset in range(7))
    next_week = "、".join(_day(monday + timedelta(days=7 + offset)) for offset in range(7))
    return (
        "- 截止：从纪要里这条任务的「截止：××」或任务原文里的时间说法抽。due_phrase 逐字摘录那句时间说法，"
        "没有就写 null；due_date 以开会日期为基准换算成 YYYY-MM-DD："
        "「今天」「当天」是开会日期，「明天」加一天，「本周五」「周五前」是本周的周五，"
        "「下周三」是下周的周三，「28 号」是开会日期当天或之后最近的 28 号，「月底」是开会当月最后一天。"
        "「尽快」「抓紧」「待确认」这类落不到具体日子的，due_date 写 null，不要猜。\n"
        f"开会日期：{_day(base)}\n"
        f"本周：{this_week}\n"
        f"下周：{next_week}\n"
    )


def _phrase(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # 模型回复里 \ud800 这类转义解出来的孤立代理字符写不进 SQLite，会把整条任务拖垮
    text = " ".join("".join(c for c in value if not 0xD800 <= ord(c) <= 0xDFFF).split())
    return text[:MAX_PHRASE_CHARS] or None


def extracted_due(task: dict[str, Any], base: date | None) -> tuple[str | None, str | None]:
    """AI 给的一条任务 → (due_date, due_phrase)。日子核不过就只留原文说法、截止留空。"""
    phrase = _phrase(task.get("due_phrase"))
    raw = task.get("due_date")
    if base is None or not isinstance(raw, str):
        return None, phrase
    try:
        due = date.fromisoformat(raw.strip())
    except ValueError:
        return None, phrase
    # fromisoformat 也认 20260923、2026-W39 这类写法，只收 YYYY-MM-DD
    if raw.strip() != due.isoformat():
        return None, phrase
    if due < base or due > base + timedelta(days=MAX_DUE_DAYS):
        return None, phrase
    return due.isoformat(), phrase


def parse_due_input(value: str | None) -> str | None:
    """手动新建、修改任务时填的截止：空是清空（未定截止），否则必须是 YYYY-MM-DD。"""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        due = date.fromisoformat(text)
    except ValueError as error:
        raise ValueError("截止日期格式应为 YYYY-MM-DD") from error
    if text != due.isoformat():
        raise ValueError("截止日期格式应为 YYYY-MM-DD")
    if not MANUAL_DUE_RANGE[0] <= due <= MANUAL_DUE_RANGE[1]:
        raise ValueError("截止日期超出范围")
    return text
