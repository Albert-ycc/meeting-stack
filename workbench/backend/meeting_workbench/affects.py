"""第四期 4e：可能过时（links_loop 的 H2 affects.match_due、L3 affects.auto_clear）。

决议定下以后一直没改过、里面还写着旧数字或已经取消的东西的文件，标「可能过时」。只用 SQL、字符串和
材料全文索引：不调 AI、不用向量、不拿编码锁，材料文字和文件名都不离开这台 Mac。材料一端只存段号，
relations.quote 是决议原文。

- 纯函数：values(text) 认六种数值（百分数、金额、日期、月份、数量、普通数，中文数字到「千」）；
  decided_values(text) 是决议自己定下的值（「由 A 改成 B」只留 B）；decision_subject(text) 取主语
  （紧挨着第一个数值前面的那段，在「在」「按」「由」「从」处截断，加上决议里出现的已确认词条，最多
  3 个）；cancel_objects(text) 取「取消 X」「X 换成…」里的 X（「订单取消接口」「采购单取消」这类复合名词
  里的「取消」不算）。项目名、它的也叫和根目录名永远不当主语。
- 找片段：3 个字以上用 material_chunks_fts MATCH；2 个字的只在本项目片段不超过 10 万段时用 instr，
  一场会的 2 字主语合成一条语句一起查。字面上这个主语在项目材料里超过 10 份文件都有（「EDC」「定位」
  这类到处都是的词）时太泛，不能当证据，这个主语直接跳过。
- 两条规则：数值规则（片段里有主语，主语前后 40 个字以内有同类、值不同的数；决议自己的值已经在
  里面就跳过）；取消规则（片段里原样有 X）。
- 文件：活的、normal 区、内容标识新鲜、不是录音和会议材料、修改时间早于决议且在一年以内、不是这条
  决议的记录（找到主语或 X 的那几段里，没有哪一段包含决议 60% 以上的 4 字片段；数值规则还要那一段
  写着决议自己的值）。
- 上限：每条决议最多 3 份，10 份以上一个都不写；每个项目同时最多 12 个在问，不挤掉已有的，多出来的等
  空位（台账里记 capped:，项目里在问的少于 12 个时再整场重配）。
- 排程：决议段变了（affects_hash 不等于 section_hash）整场重配，再 clear_missing；只有新片段时只配
  新片段、只加不收。一场会配完在同一个事务里写 affects_hash 和 affects_chunk_mark；没配完（忙、到了
  条数或时间）的会什么都不写，下一轮整场再来。
- L3：在问的影响，文件断了线、决议之后改过、决议没了或后来改了的改 cleared。
- stat_guard：文件面板和预览在有在问的影响时 stat 一次文件，变了就先不给这几个问题（不写库）。
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .decisions import decision_moment, project_names, superseded_sql, text_key
from .relation_read import live_file
from .relations import canonical, clear_missing, is_rejected, upsert_system
from .search import _fts_phrase

ROUND_DECISIONS = 20
ROUND_SECONDS = 5.0
L3_ROWS = 500
NEAR_CHARS = 40
FILES_PER_DECISION = 3
TOO_MANY_FILES = 10
PROJECT_OPEN_CAP = 12
# 2 个字的主语只在本项目片段合计不超过这么多时用 instr（和 4g 的短针同一个门槛）
SHORT_NEEDLE_CHUNKS = 100_000
SHORT_PER_DECISION = 2
RECORD_RATIO = 0.6
LOOKBACK = timedelta(days=90)
YEAR = timedelta(days=365)
SUBJECT_CHARS = 6
CAPPED = "capped:"

RULE_VALUE = "value"
RULE_CANCEL = "cancel"

# ---------------------------------------------------------------------- 数值

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN = "零〇一二两三四五六七八九十百千"
# 前面紧挨着字母的数（v3 这种版本号）不算
_NUM = r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?"
_UNITS = "人|台|天|个|套|次|周|页|张|家|项|条|名|位|小时|份|轮|场|期|批"
_MONEY_SUFFIX = r"(?:元|块钱|块)"

KIND_PERCENT = "percent"
KIND_MONEY = "money"
KIND_DATE = "date"
KIND_MONTH = "month"
KIND_COUNT = "count"
KIND_NUMBER = "number"
# 能对上的类型（第 7 节的表）
COMPATIBLE = {
    KIND_PERCENT: frozenset({KIND_PERCENT, KIND_MONEY, KIND_NUMBER}),
    KIND_MONEY: frozenset({KIND_MONEY, KIND_PERCENT}),
    KIND_DATE: frozenset({KIND_DATE, KIND_MONTH}),
    KIND_MONTH: frozenset({KIND_DATE, KIND_MONTH}),
    KIND_COUNT: frozenset({KIND_COUNT}),
    KIND_NUMBER: frozenset({KIND_NUMBER, KIND_PERCENT}),
}


@dataclass(frozen=True)
class Value:
    """一个数值：kind 六种之一；number 是归一后的数（日期、月份用 month、day）；unit 是数量的单位。
    start、end 是在 NFKC 以后的文字里的位置。"""

    kind: str
    number: float | None
    start: int
    end: int
    unit: str = ""
    month: int | None = None
    day: int | None = None


def cn_number(text: str) -> float | None:
    """中文数字到「千」（加上「万」作乘数）：五 → 5，十五 → 15，三千五百 → 3500，两万 → 20000。"""
    text = text.strip()
    if not text or any(char not in _CN_DIGITS and char not in _CN_UNITS and char != "万" for char in text):
        return None
    if "万" in text:
        head, _sep, tail = text.partition("万")
        high = cn_number(head) if head else 1
        low = cn_number(tail) if tail else 0
        return None if high is None or low is None else high * 10_000 + low
    total = 0
    digit: int | None = None
    for char in text:
        if char in _CN_DIGITS:
            if digit is not None and not (digit == 0 or _CN_DIGITS[char] == 0):
                # 「一二」这种连着写的数字不当成一个数
                return None
            digit = _CN_DIGITS[char]
        else:
            total += (digit if digit is not None else 1) * _CN_UNITS[char]
            digit = None
    return float(total + (digit or 0))


def _number(text: str) -> float | None:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return cn_number(text)


_MONEY_SCALE = {"万": 10_000, "千": 1_000, "k": 1_000, "w": 10_000}
# k、w 后面紧跟字母的（kg、km、kw、web）不是金额；没有「元」「块」时要前面有 ¥、￥ 或说钱的词才算
_MONEY_CONTEXT = re.compile(r"[¥￥$]|价|预算|费|金额|成本|款|报价|合同额|工资|薪|收入|营收")
_MONEY_CONTEXT_CHARS = 8
# 日期前面可以带年份：2025/9/30、2025年9月30日
_YEAR = r"(?:\d{4}\s*[/年]\s*)?"
# 「12 月 3 人」「12 月 5%」里月后面的数不是日
_NOT_DAY = rf"(?!\s*(?:[.\x25]|{_UNITS}|万|千|元|块|k|w))"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_PERCENT, re.compile(rf"百分之\s*([{_CN}\d.]+)")),
    (KIND_PERCENT, re.compile(rf"({_NUM}|[{_CN}]+)\s*(?:\x25|个点)")),
    # 月后面是阿拉伯数字的日可以不写「日」「号」；是中文数字的日要写（「12 月两个版本」不是 12/2）
    (KIND_DATE, re.compile(rf"(?<![\d.]){_YEAR}(\d{{1,2}}|[{_CN}]{{1,3}})\s*月\s*(\d{{1,2}})(?!\d)(?:\s*[日号]|{_NOT_DAY})")),
    (KIND_DATE, re.compile(rf"(?<![\d.]){_YEAR}(\d{{1,2}}|[{_CN}]{{1,3}})\s*月\s*([{_CN}]{{1,3}})\s*[日号]")),
    (KIND_DATE, re.compile(r"(?<![\d./])(?:\d{4}/)?(\d{1,2})/(\d{1,2})(?![\d/])")),
    (KIND_DATE, re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\s*[日号]")),
    (KIND_MONTH, re.compile(rf"(?<![\d.]){_YEAR}(\d{{1,2}}|[{_CN}]{{1,3}})\s*月(?:份|底|初|中旬|上旬|下旬|中|末)?")),
    (KIND_MONEY, re.compile(rf"({_NUM}|[{_CN}]+)\s*(万|千)\s*{_MONEY_SUFFIX}?")),
    (KIND_MONEY, re.compile(rf"({_NUM})\s*(k|w)(?![A-Za-z])\s*({_MONEY_SUFFIX})?", re.IGNORECASE)),
    (KIND_MONEY, re.compile(rf"({_NUM}|[{_CN}]+)\s*{_MONEY_SUFFIX}")),
    (KIND_COUNT, re.compile(rf"({_NUM}|[{_CN}]+)\s*({_UNITS})")),
    (KIND_NUMBER, re.compile(r"(?<![A-Za-z0-9_.])(\d+\.\d+|\d{2,})(?![\d.]?\d)")),
)


def _normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def values(text: str) -> list[Value]:
    """文字里的数值（按出现的先后）。百分数（5%、百分之五、5 个点）归一成数；金额（12万、3000 元、
    1.2k、两万块）归一成元；日期（9月30日、9/30、9.30号）归一成月日；月份（十月底、10 月份）归一成
    月；数量（3 人、2 台、5 天）是数加单位；普通数只认小数或两位以上的数，版本号（v3）里的不算。
    text 先做 NFKC，位置按 NFKC 以后的文字算。"""
    body = _normal(text)
    taken: list[tuple[int, int]] = []
    found: list[Value] = []

    def free(start: int, end: int) -> bool:
        return all(end <= left or start >= right for left, right in taken)

    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(body):
            start, end = match.span()
            if not free(start, end):
                continue
            value = _value(kind, match, start, end)
            if value is None:
                continue
            taken.append((start, end))
            found.append(value)
    return sorted(found, key=lambda item: item.start)


def _value(kind: str, match: re.Match[str], start: int, end: int) -> Value | None:
    if kind == KIND_DATE:
        month, day = _number(match.group(1)), _number(match.group(2))
        if month is None or day is None or not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return Value(kind, None, start, end, month=int(month), day=int(day))
    if kind == KIND_MONTH:
        month = _number(match.group(1))
        if month is None or not (1 <= month <= 12) or month != int(month):
            return None
        return Value(kind, None, start, end, month=int(month))
    number = _number(match.group(1))
    if number is None:
        return None
    if kind == KIND_PERCENT:
        return Value(kind, number, start, end)
    if kind == KIND_MONEY:
        scale = match.group(2).lower() if match.lastindex and match.lastindex >= 2 and match.group(2) else ""
        if scale in ("k", "w") and not match.group(3):
            # 1.2k、3w 只在前面有 ¥、￥ 或说钱的词时算金额（「3w 用户」不是）
            if not _MONEY_CONTEXT.search(match.string[max(0, start - _MONEY_CONTEXT_CHARS) : start]):
                return None
        return Value(kind, number * _MONEY_SCALE.get(scale, 1), start, end)
    if kind == KIND_COUNT:
        return Value(kind, number, start, end, unit=match.group(2))
    return Value(kind, number, start, end)


def compatible(left: Value, right: Value) -> bool:
    """决议的数值 left 和文件里的 right 能不能比（第 7 节的表；数量要同单位）。"""
    if right.kind not in COMPATIBLE[left.kind]:
        return False
    return left.kind != KIND_COUNT or left.unit == right.unit


def same(left: Value, right: Value) -> bool:
    """能比的两个数值是不是同一个值（日期和月份比月）。"""
    if left.kind in (KIND_DATE, KIND_MONTH) or right.kind in (KIND_DATE, KIND_MONTH):
        if left.kind == KIND_DATE and right.kind == KIND_DATE:
            return (left.month, left.day) == (right.month, right.day)
        return left.month == right.month
    if left.number is None or right.number is None:
        return False
    return abs(left.number - right.number) < 1e-9


# ---------------------------------------------------------------------- 主语和取消的说法

# 结尾的动词和副词（去掉，长的先试）
_TRAILING_VERBS = tuple(
    sorted(
        (
            "下调", "上调", "调整为", "调整到", "调整成", "调到", "调为", "调成", "改为", "改成", "改到", "定在",
            "定为", "定成", "控制在", "维持在", "保持在", "不超过", "不低于", "不少于", "不高于", "不多于", "按照",
            "降到", "降至", "降为", "提到", "提高到", "提高", "降低", "增加到", "增加", "减少到", "减少", "减到",
            "设为", "设置为", "设成", "大约", "约为", "暂定", "暂时", "统一", "先", "暂", "按", "为", "是", "到",
            "在", "约", "共", "再", "都", "也", "就", "还", "仍", "需", "要", "需要", "应", "应该", "必须", "只", "最多", "最少", "至少", "上限", "下限",
            "由", "从",
        ),
        key=len,
        reverse=True,
    )
)
# 主语在这几个字处截断：「总价在原基础上下调 5%」的主语是「总价」
_CUT_WORDS = ("在", "按", "由", "从")
# 「由 A 改成 B」「从 A 调到 B」：A 是原来的值，只有 B 是决议定下的
_FROM_WORDS = ("由", "从")
_CHANGE_VERBS = tuple(
    sorted(
        (
            "改成", "改为", "改到", "调到", "调为", "调成", "调整为", "调整到", "调整成", "降到", "降至", "降为",
            "降低到", "升到", "升至", "升为", "提到", "提高到", "提升到", "增加到", "减少到", "减到",
        ),
        key=len,
        reverse=True,
    )
)
_LEADING_WORDS = ("的", "把", "将", "对", "就", "并", "且", "和", "与", "及", "由", "从", "让", "给", "请", "原来的", "原")
_CANCEL_BEFORE = re.compile(r"(取消|不再|去掉|砍掉|停用|下线|不做)(?:掉|了)?\s*")
_CANCEL_AFTER = re.compile(r"(换成|替换为|替换成|改用|改成)")
_CANCEL_STOP = re.compile(r"(?:和|与|以及|及|或)")
_TRAILING_PARTICLES = ("了", "的", "吧", "呢", "啊")
# 「取消」当名词用时夹在复合词里：「订单取消接口」（后面直接跟着接口、按钮这类名词）、「采购单取消」
# （前面是「单」）。这两种不当动词处理；「本期去掉 X」「会上决定取消 X」照常算
_CANCEL_NOUN_HEADS = (
    "接口", "按钮", "功能", "流程", "原因", "状态", "操作", "记录", "规则", "逻辑", "入口", "权限", "页面", "时间",
)


def _run_char(char: str) -> bool:
    return ("一" <= char <= "鿿") or (char.isascii() and char.isalnum())


def _run_before(body: str, at: int) -> str:
    """紧挨着 at 前面的那段汉字或字母数字（中间的空白跳过）。"""
    end = at
    while end > 0 and body[end - 1].isspace():
        end -= 1
    start = end
    while start > 0 and _run_char(body[start - 1]):
        start -= 1
    return body[start:end]


def _run_after(body: str, at: int) -> str:
    start = at
    while start < len(body) and body[start].isspace():
        start += 1
    end = start
    while end < len(body) and _run_char(body[end]):
        end += 1
    return body[start:end]


def _strip_trailing(word: str) -> str:
    changed = True
    while changed and word:
        changed = False
        for verb in _TRAILING_VERBS:
            if word.endswith(verb) and len(word) > len(verb):
                word = word[: -len(verb)]
                changed = True
                break
    return word


def _cut(word: str) -> str:
    """在「在」「按」「由」「从」处截断，留前面那一截（前面不到 2 个字时不截）。"""
    for index, char in enumerate(word):
        if index >= 2 and char in _CUT_WORDS:
            return word[:index]
    return word


def decided_values(text: str) -> list[Value]:
    """决议自己定下的值：「由 A 改成 B」「从 A 调到 B」里只留 B，A 是原来的值。"""
    body = _normal(text)
    found = values(body)
    kept: list[Value] = []
    for index, value in enumerate(found):
        before = body[: value.start].rstrip()
        after = body[value.end :].lstrip()
        if index + 1 < len(found) and before.endswith(_FROM_WORDS) and after.startswith(_CHANGE_VERBS):
            continue
        kept.append(value)
    return kept


def _strip_leading(word: str) -> str:
    changed = True
    while changed and word:
        changed = False
        for lead in sorted(_LEADING_WORDS, key=len, reverse=True):
            if word.startswith(lead) and len(word) > len(lead):
                word = word[len(lead) :]
                changed = True
                break
    return word


def _banned(word: str, excluded: Sequence[str]) -> bool:
    folded = _normal(word).casefold()
    return any(folded in _normal(name).casefold() for name in excluded if name)


def decision_subject(
    text: str,
    *,
    terms: Sequence[tuple[str, Sequence[str]]] = (),
    excluded: Sequence[str] = (),
) -> list[str]:
    """主语，最多 3 个：1. 紧挨着第一个数值前面的那段汉字或字母数字，去掉结尾的动词、副词（包括「由」
    「从」），在「在」「按」「由」「从」处截断，去掉开头的虚字（有「的」时取它后面那一截），留最后 6 个字以内；2. 决议里出现的已确认词条（terms 是 [(本名, [本名, 别名, 也叫…])]），按本名记，
    最多 2 个。项目名、它的也叫和根目录名（excluded）永远不当主语。"""
    body = _normal(text)
    subjects: list[str] = []
    found = values(body)
    if found:
        # 「看板系统的总价」取「的」后面那一截
        word = _strip_trailing(_cut(_strip_trailing(_run_before(body, found[0].start)))).rpartition("的")[2]
        word = _strip_leading(word)[-SUBJECT_CHARS:]
        word = _strip_leading(word)
        if len(word) >= 2 and not _banned(word, excluded):
            subjects.append(word)
    picked = 0
    folded = body.casefold()
    for term, names in terms:
        if picked >= 2 or len(subjects) >= 3:
            break
        if term in subjects or _banned(term, excluded):
            continue
        if any(len(name) >= 2 and _normal(name).casefold() in folded for name in names):
            subjects.append(term)
            picked += 1
    return subjects[:3]


def _object(word: str) -> str | None:
    word = _CANCEL_STOP.split(word, maxsplit=1)[0]
    while word and word.endswith(_TRAILING_PARTICLES):
        word = word[:-1]
    word = _strip_leading(word)[:12]
    return word if 2 <= len(word) <= 12 else None


def _cancel_is_verb(body: str, start: int, end: int) -> bool:
    """body[start:end] 处的触发词是不是动词用法：前面紧贴「单」（「采购单取消」）或后面紧跟接口、按钮这类
    名词（「订单取消接口」）时是复合名词的一截，不算。"""
    before = _run_before(body, start)
    return not before.endswith("单") and not body[end:].startswith(_CANCEL_NOUN_HEADS)


def cancel_objects(text: str, *, excluded: Sequence[str] = ()) -> list[str]:
    """取消的说法里旧的那个东西 X（2 到 12 个字）：「取消」「不再」「去掉」「砍掉」「停用」「下线」「不做」
    加 X，或 X 加「换成」「替换为」「改用」「改成」（后面紧跟数值的「改成」是改数，不算）。「订单取消
    接口」「采购单取消」这类复合名词里的「取消」不当动词处理。"""
    body = _normal(text)
    found: list[str] = []
    for match in _CANCEL_BEFORE.finditer(body):
        if not _cancel_is_verb(body, match.start(), match.end()):
            continue
        word = _object(_run_after(body, match.end()))
        if word:
            found.append(word)
    numbers = values(body)
    for match in _CANCEL_AFTER.finditer(body):
        after = match.end()
        while after < len(body) and body[after].isspace():
            after += 1
        if match.group(1) == "改成" and any(value.start == after for value in numbers):
            continue
        run = _run_before(body, match.start())
        word = _object(run[-12:]) if run else None
        if word:
            found.append(word)
    result: list[str] = []
    for word in found:
        if word not in result and not _banned(word, excluded):
            result.append(word)
    return result


# ---------------------------------------------------------------------- 两条规则


def value_hit(chunk_text: str, subject: str, decided: Sequence[Value]) -> bool:
    """数值规则：片段里有主语，主语前后 40 个字以内有一个和决议的数值类型对得上、值又不同的数。
    决议自己的值已经在这 40 个字里时不算（文件已经改好了）。"""
    body = _normal(chunk_text)
    if not subject or subject not in body or not decided:
        return False
    found = values(body)
    hit = False
    at = body.find(subject)
    while at >= 0:
        low, high = at - NEAR_CHARS, at + len(subject) + NEAR_CHARS
        near = [value for value in found if value.end > low and value.start < high]
        for mine in decided:
            for theirs in near:
                if not compatible(mine, theirs):
                    continue
                if same(mine, theirs):
                    return False
                hit = True
        at = body.find(subject, at + 1)
    return hit


def cancel_hit(chunk_text: str, word: str) -> bool:
    """取消规则：片段里原样有 X。"""
    return bool(word) and word in _normal(chunk_text)


def _shingles(text: str) -> set[str]:
    key = text_key(text)
    return {key[index : index + 4] for index in range(len(key) - 3)}


def is_record(decision_text: str, chunk_texts: Iterable[str], decided: Sequence[Value] = ()) -> bool:
    """这份文件是不是这条决议的记录（导出的纪要副本）：有一段包含决议 60% 以上的 4 字片段。decided 不为
    空时（数值规则）这一段还要写着决议自己的值才算：『总价在原基础上下调 3%，含税』对决议『总价在原基础
    上下调 5%，含税』字面几乎一样，却正是要标的旧文件。"""
    mine = _shingles(decision_text)
    if not mine:
        return False
    for text in chunk_texts:
        theirs = _shingles(text)
        if len(mine & theirs) < RECORD_RATIO * len(mine):
            continue
        if not decided or _has_value(text, decided):
            return True
    return False


def _has_value(text: str, decided: Sequence[Value]) -> bool:
    found = values(text)
    return any(compatible(mine, theirs) and same(mine, theirs) for mine in decided for theirs in found)


# ---------------------------------------------------------------------- H2 的排程


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def floor_of(now: datetime, links_since: str | None) -> datetime:
    """决议时刻的下限：links_since 前 90 天，也不早于 365 天前。"""
    since = _parse(links_since) or now
    return max(since - LOOKBACK, now - YEAR)


_PROJECT_FILES = """SELECT f.content_key FROM material_files f
    JOIN project_material_roots pr ON pr.id = f.root_id
   WHERE pr.project_id = ? AND f.gone_at IS NULL AND f.content_key IS NOT NULL"""

_MEETINGS_SQL = """
SELECT s.meeting_id, s.section_hash, s.affects_hash, s.affects_chunk_mark, m.project_id,
       m.recording_date, m.created_at
  FROM decision_scan s JOIN meetings m ON m.id = s.meeting_id
 WHERE m.project_id IS NOT NULL AND s.section_hash IS NOT NULL
   AND s.minutes_version_id IS m.current_minutes_version_id
   AND EXISTS (SELECT 1 FROM project_material_roots pr
                 JOIN material_index_state st ON st.root_id = pr.id
                WHERE pr.project_id = m.project_id AND st.last_full_at IS NOT NULL)
 ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id"""


def _project_marks(connection: Any, project_ids: Iterable[str]) -> dict[str, int]:
    """每个项目现在最大的片段 id（片段 id 只增不复用）。"""
    marks: dict[str, int] = {}
    for project_id in sorted(set(project_ids)):
        row = connection.execute(
            f"""SELECT MAX(c.id) FROM material_chunks c
                 WHERE c.content_key IN ({_PROJECT_FILES})""",
            (project_id,),
        ).fetchone()
        marks[project_id] = int(row[0] or 0)
    return marks


def _open_counts(connection: Any) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """SELECT project_id, COUNT(*) FROM relations
                WHERE kind = 'affects' AND status = 'suggested' GROUP BY project_id"""
        ).fetchall()
    }


def due_meetings(connection: Any, now: datetime) -> list[dict[str, Any]]:
    """到期的会（新的在前）：决议段变了（affects_hash 不等于 section_hash），或项目里有新片段。
    记过 capped: 的会，项目里在问的少于 12 个时才再到期。会议日期早于时间窗的不看。每条带 full（整场
    重配）和 mark（项目现在最大的片段 id）。"""
    rows = [dict(row) for row in connection.execute(_MEETINGS_SQL).fetchall()]
    if not rows:
        return []
    since = connection.execute("SELECT value FROM app_state WHERE key = 'links_since'").fetchone()
    floor = floor_of(now, since[0] if since else None)
    marks = _project_marks(connection, (row["project_id"] for row in rows))
    opened = _open_counts(connection)
    due: list[dict[str, Any]] = []
    for row in rows:
        moment = decision_moment(row, None)
        # 只有日期的会按那天结束算；再多留一天给当天很晚的时间点
        if moment is None or moment + timedelta(days=1) < floor:
            continue
        project_id = str(row["project_id"])
        full = row["affects_hash"] != row["section_hash"]
        if full and row["affects_hash"] == f"{CAPPED}{row['section_hash']}":
            full = opened.get(project_id, 0) < PROJECT_OPEN_CAP
        fresh = marks.get(project_id, 0) > int(row["affects_chunk_mark"] or 0)
        if full or fresh:
            due.append({**row, "full": full, "mark": marks.get(project_id, 0), "floor": floor})
    return due


def due_count(connection: Any, now: datetime) -> int:
    """健康检查的 waiting.affects：H2 到期的会数。"""
    return len(due_meetings(connection, now))


def _project_context(connection: Any, project_id: str) -> dict[str, Any]:
    project = connection.execute("SELECT name, also_names FROM projects WHERE id = ?", (project_id,)).fetchone()
    roots = [str(row[0]) for row in connection.execute(
        "SELECT path FROM project_material_roots WHERE project_id = ?", (project_id,)
    ).fetchall()]
    excluded = [
        *(project_names(project["name"], project["also_names"]) if project is not None else []),
        *(path.rstrip("/").rpartition("/")[2] for path in roots),
    ]
    terms: list[tuple[str, list[str]]] = []
    for row in connection.execute(
        """SELECT term, aliases, also FROM glossary_terms
            WHERE confirmed = 1 AND (project_id = ? OR project_id IS NULL)
            ORDER BY project_id IS NULL, length(term) DESC, term""",
        (project_id,),
    ).fetchall():
        names = [str(row["term"])]
        for column in ("aliases", "also"):
            try:
                extra = json.loads(row[column] or "[]")
            except ValueError:
                extra = []
            if isinstance(extra, list):
                names.extend(str(item) for item in extra if isinstance(item, str) and item)
        terms.append((str(row["term"]), names))
    chunk_total = connection.execute(
        f"SELECT COUNT(*) FROM material_chunks c WHERE c.content_key IN ({_PROJECT_FILES})", (project_id,)
    ).fetchone()[0]
    return {"excluded": excluded, "terms": terms, "short_ok": int(chunk_total) <= SHORT_NEEDLE_CHUNKS}


# 片段连文件：活的、normal 区、内容标识新鲜、不是录音、不是会议材料、在这个项目的根目录里
_CHUNK_COLUMNS = """c.id AS chunk_id, c.content_key, c.ordinal, c.text, f.id AS file_id, f.name, f.root_id,
       f.rel_path, f.stem_key, f.mtime_ns"""
_CHUNK_JOIN = """JOIN material_contents mc ON mc.content_key = c.content_key AND mc.meeting_id IS NULL
  JOIN material_files f ON f.content_key = c.content_key AND f.gone_at IS NULL AND f.zone = 'normal'
       AND f.content_size = f.size AND f.content_mtime_ns = f.mtime_ns
  JOIN project_material_roots pr ON pr.id = f.root_id AND pr.project_id = :pid"""


def _match_chunks(connection: Any, project_id: str, needle: str, after: int, upto: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            f"""SELECT {_CHUNK_COLUMNS}
                  FROM material_chunks_fts x
                  JOIN material_chunks c ON c.id = x.rowid
                  {_CHUNK_JOIN}
                 WHERE material_chunks_fts MATCH :q AND x.rowid > :after AND x.rowid <= :upto
                   AND c.start_ms IS NULL""",
            {"pid": project_id, "q": _fts_phrase(needle), "after": after, "upto": upto},
        ).fetchall()
    ]


def _short_chunks(
    connection: Any, project_id: str, needles: Sequence[str], after: int, upto: int
) -> dict[str, list[dict[str, Any]]]:
    """2 个字的针合成一条语句一起查（instr），按针分组。"""
    result: dict[str, list[dict[str, Any]]] = {needle: [] for needle in needles}
    if not needles:
        return result
    params: dict[str, Any] = {"pid": project_id, "after": after, "upto": upto}
    ors = []
    for index, needle in enumerate(needles):
        params[f"n{index}"] = needle
        ors.append(f"instr(c.text, :n{index}) > 0")
    for row in connection.execute(
        f"""SELECT {_CHUNK_COLUMNS} FROM material_chunks c {_CHUNK_JOIN}
             WHERE c.id > :after AND c.id <= :upto AND c.start_ms IS NULL AND ({" OR ".join(ors)})""",
        params,
    ).fetchall():
        item = dict(row)
        body = _normal(item["text"])
        for needle in needles:
            if needle in body:
                result[needle].append(item)
    return result


@dataclass
class _Hit:
    rule: str
    term: str
    chunk: dict[str, Any]
    named: bool

    def order(self) -> tuple:
        """取消规则先于数值规则；决议里直接说到文件名的先；主语长的先；修改时间新的先；段号小的先。"""
        return (
            self.rule != RULE_CANCEL,
            not self.named,
            -len(self.term),
            -int(self.chunk["mtime_ns"] or 0),
            int(self.chunk["ordinal"]),
        )


def match_decision(
    connection: Any,
    decision: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    project_id: str,
    moment: datetime,
    after: int,
    upto: int,
    short_cache: Mapping[str, list[dict[str, Any]]] | None = None,
) -> list[_Hit] | None:
    """一条决议的候选文件（每份内容一段最好的），按顺序；10 份以上时回 None（主语太泛，一个都不写）。
    after、upto 是片段 id 的范围（只配新片段时 after 是台账里的记号）。

    纪要副本只看这份文件里找到主语或 X 的那几段（找片段时已经拿到了文字，不回表读全文）：副本里那一段
    就是这条决议，一定写着主语或 X。候选一到 10 份就回 None，不再往下找，也不做副本判断。"""
    text = str(decision["text"] or "")
    excluded = context["excluded"]
    decided = decided_values(text)
    subjects = decision_subject(text, terms=context["terms"], excluded=excluded) if decided else []
    cancels = cancel_objects(text, excluded=excluded)
    needles: list[tuple[str, str]] = [(RULE_CANCEL, word) for word in cancels] + [
        (RULE_VALUE, word) for word in subjects
    ]
    if not needles:
        return []
    moment_ns = _ns(moment)
    floor_ns = _ns(moment - YEAR)
    folded_text = _normal(text).casefold()
    best: dict[str, _Hit] = {}
    # 每份内容找到主语或 X 的那几段（按片段 id 去重），纪要副本只在这几段里看
    found: dict[str, dict[int, str]] = {}
    shorts = 0
    for rule, needle in needles:
        if len(needle) >= 3:
            chunks = _match_chunks(connection, project_id, needle, after, upto)
        elif context["short_ok"] and shorts < SHORT_PER_DECISION:
            shorts += 1
            cached = (short_cache or {}).get(needle)
            chunks = cached if cached is not None else _short_chunks(connection, project_id, [needle], after, upto)[needle]
        else:
            continue
        if rule == RULE_VALUE and len({chunk["content_key"] for chunk in chunks}) > TOO_MANY_FILES:
            # 主语在项目材料里到处都是（「EDC」「定位」这类广泛出现的词），不止决议自己说的这份，
            # 太泛不能当证据：这条决议的字面主语没抓准，跳过这个主语，不进一步判断片段
            continue
        for chunk in chunks:
            mtime = int(chunk["mtime_ns"] or 0)
            if not (floor_ns <= mtime < moment_ns):
                continue
            content_key = str(chunk["content_key"])
            found.setdefault(content_key, {})[int(chunk["chunk_id"])] = str(chunk["text"] or "")
            if rule == RULE_CANCEL:
                if not cancel_hit(chunk["text"], needle):
                    continue
            elif not value_hit(chunk["text"], needle, decided):
                continue
            stem = str(chunk["name"]).rpartition(".")[0] or str(chunk["name"])
            hit = _Hit(rule, needle, chunk, bool(stem) and _normal(stem).casefold() in folded_text)
            kept = best.get(content_key)
            if kept is None or hit.order() < kept.order():
                best[content_key] = hit
                if len(best) >= TOO_MANY_FILES:
                    return None
    # 这条决议的记录（导出的纪要副本）不算；数值规则要那一段还写着决议自己的值（见 is_record）
    for content_key, hit in list(best.items()):
        if is_record(text, found[content_key].values(), decided if hit.rule == RULE_VALUE else ()):
            best.pop(content_key)
    return sorted(best.values(), key=lambda hit: hit.order())[:FILES_PER_DECISION]


def _row_for(decision: Mapping[str, Any], meeting_id: str, project_id: str, hit: _Hit) -> dict[str, Any]:
    chunk = hit.chunk
    return {
        "kind": "affects",
        "project_id": project_id,
        "ident": f"{decision['id']}|{chunk['content_key']}",
        "status": "suggested",
        "origin": "rule",
        "meeting_id": meeting_id,
        "at_ms": decision["start_ms"],
        "decision_id": decision["id"],
        "file_id": int(chunk["file_id"]),
        "content_key": chunk["content_key"],
        "root_id": int(chunk["root_id"]),
        "rel_path": chunk["rel_path"],
        "stem_key": chunk["stem_key"],
        # 决议原文；材料一端只存段号
        "quote": str(decision["text"] or ""),
        "evidence_json": canonical({"rule": hit.rule, "terms": [hit.term], "ordinal": int(chunk["ordinal"])}),
        "score": (2.0 if hit.rule == RULE_CANCEL else 1.0) + len(hit.term) / 100,
    }


STOP_BUSY = "busy"
STOP_BUDGET = "budget"


def match_due(
    db: Any,
    busy: Callable[[], bool],
    budget: float = ROUND_SECONDS,
    *,
    now: datetime | None = None,
    since: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_decisions: int | None = None,
) -> dict[str, Any]:
    """H2 一轮：到期的会新的在前，每条决议之前看忙信号和剩下的预算；每轮最多 max_decisions 条决议或
    budget 秒。一场会配完在同一个事务里写关联行和台账。返回 {tried, pending, written, cleared, stopped}；
    stopped 是 busy（忙信号）、budget（到了条数或时间）或 None，健康信息里的 phases.affects 照它写。

    每轮第一场会不看条数和时间，一定配完（只看忙信号）：台账按场会记（affects_hash、affects_chunk_mark），
    没有「配到第几条」的位置，停在一场会中间只能下一轮整场重来；第一场会也守时间的话，一场配不完 5 秒的
    会每轮都从头再来，永远到不了台账。修掉回表读全文以后，一条决议最坏约 0.3 秒（6 万段），一场会通常
    几条决议，第一场会超出预算有限。之后的会：开始前看它要配的决议数，放不进这一轮剩下的条数就停在它
    前面（不白做半场）；每条决议之前看时间，超了就停，这场会什么都不写、台账不动，下一轮它排第一整场
    重配。这样增量标记只在整场配完时前进，不会漏掉没配的决议。"""
    moment = now or datetime.now(UTC)
    stamp = since or _stamp(moment)
    started = clock()
    most = ROUND_DECISIONS if max_decisions is None else max_decisions
    with db.autocommit() as connection:
        due = due_meetings(connection, moment)
    tried = 0
    done = 0
    written = 0
    cleared = 0
    stopped: str | None = None

    def out_of_time() -> bool:
        return clock() - started >= budget

    for meeting in due:
        first = done == 0
        if not first and (tried >= most or out_of_time()):
            stopped = STOP_BUDGET
            break
        outcome = _match_meeting(
            db, meeting, moment, stamp, busy,
            out_of_time=None if first else out_of_time,
            room=None if first else most - tried,
        )
        if isinstance(outcome, str):
            stopped = outcome
            break
        done += 1
        tried += outcome["tried"]
        written += outcome["written"]
        cleared += outcome["cleared"]
    return {"tried": tried, "pending": len(due), "written": written, "cleared": cleared, "stopped": stopped}


def _match_meeting(
    db: Any,
    meeting: Mapping[str, Any],
    moment: datetime,
    since: str,
    busy: Callable[[], bool],
    *,
    out_of_time: Callable[[], bool] | None = None,
    room: int | None = None,
) -> dict[str, int] | str:
    """一场会：整场重配（full）或只配新片段。忙了回 STOP_BUSY；要配的决议比 room 多、或配到一半
    out_of_time 为真回 STOP_BUDGET。这两种都什么都不写、台账不动，这场会下一轮从头再来。"""
    meeting_id = str(meeting["meeting_id"])
    project_id = str(meeting["project_id"])
    full = bool(meeting["full"])
    upto = int(meeting["mark"])
    after = 0 if full else int(meeting["affects_chunk_mark"] or 0)
    floor = meeting["floor"]
    decisions_sql = f"""SELECT d.id, d.text, d.start_ms FROM decisions d
                         WHERE d.meeting_id = ? AND d.gone_at IS NULL AND NOT {superseded_sql('d')}
                         ORDER BY d.ordinal, d.id"""
    with db.autocommit() as connection:
        decisions: list[tuple[dict[str, Any], datetime]] = []
        for row in connection.execute(decisions_sql, (meeting_id,)).fetchall():
            decided_at = decision_moment(meeting, row["start_ms"])
            if decided_at is None or decided_at < floor or decided_at < moment - YEAR:
                continue
            decisions.append((dict(row), decided_at))
        if room is not None and len(decisions) > room:
            return STOP_BUDGET
        if busy():
            return STOP_BUSY
        context = _project_context(connection, project_id)
        # 这场会的 2 字主语合成一条语句一起查
        shorts: list[str] = []
        if context["short_ok"]:
            for decision, _decided_at in decisions:
                text = str(decision["text"] or "")
                words = cancel_objects(text, excluded=context["excluded"])
                if decided_values(text):
                    words += decision_subject(text, terms=context["terms"], excluded=context["excluded"])
                shorts += [word for word in words if len(word) == 2 and word not in shorts]
        cache = _short_chunks(connection, project_id, shorts, after, upto) if shorts else {}
        rows: list[dict[str, Any]] = []
        tried = 0
        for decision, decided_at in decisions:
            if busy():
                return STOP_BUSY
            if out_of_time is not None and out_of_time():
                return STOP_BUDGET
            tried += 1
            hits = match_decision(
                connection, decision, context, project_id=project_id, moment=decided_at, after=after, upto=upto,
                short_cache=cache,
            )
            for hit in hits or []:
                rows.append(_row_for(decision, meeting_id, project_id, hit))
    written = 0
    cleared = 0
    capped = False
    with db.transaction() as connection:
        statuses = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT ident, status, origin FROM relations WHERE kind = 'affects' AND project_id = ?",
                (project_id,),
            ).fetchall()
        }
        existing = {ident for ident, (status, _origin) in statuses.items() if status == "suggested"}
        opened = len(existing)
        per_decision: dict[str, int] = {}
        if not full:
            for row in connection.execute(
                """SELECT decision_id, COUNT(*) FROM relations
                    WHERE kind = 'affects' AND meeting_id = ? AND status = 'suggested' GROUP BY decision_id""",
                (meeting_id,),
            ).fetchall():
                per_decision[str(row[0])] = int(row[1])
        keep: list[str] = []
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if row["ident"] in existing:
                keep.append(row["ident"])
                continue
            # 你回答过的（已更新、不相关）和按路径标过不相关的：不写，也不占空位
            status, origin = statuses.get(row["ident"], ("", ""))
            if (status and status != "cleared") or origin == "manual" or is_rejected(
                connection, "affects", project_id, row["decision_id"], row["content_key"], row["root_id"], row["rel_path"]
            ):
                continue
            decision_id = str(row["decision_id"])
            if per_decision.get(decision_id, 0) >= FILES_PER_DECISION:
                continue
            if opened >= PROJECT_OPEN_CAP:
                capped = True
                continue
            opened += 1
            per_decision[decision_id] = per_decision.get(decision_id, 0) + 1
            keep.append(row["ident"])
            fresh.append(row)
        stamp = _stamp(moment)
        if fresh:
            written = upsert_system(connection, fresh, stamp, since=since)
        if full:
            cleared = clear_missing(connection, "affects", project_id, {"meeting_id": meeting_id}, keep, stamp, since=since)
        # 这次或之前整场重配时有等空位的：记 capped:，项目里在问的少于 12 个时再整场重配
        waiting = capped or (not full and str(meeting["affects_hash"] or "").startswith(CAPPED))
        mark_hash = f"{CAPPED}{meeting['section_hash']}" if waiting else meeting["section_hash"]
        connection.execute(
            """UPDATE decision_scan SET affects_hash = ?, affects_chunk_mark = ?
                WHERE meeting_id = ? AND (affects_hash IS NOT ? OR affects_chunk_mark IS NOT ?)""",
            (mark_hash, upto, meeting_id, mark_hash, upto),
        )
    return {"tried": max(tried, 1), "written": written, "cleared": cleared}


# ---------------------------------------------------------------------- L3 和 stat


def auto_clear(db: Any, now: datetime, since: str, *, limit: int | None = None, after: int = 0) -> dict[str, int]:
    """L3：在问的影响按 id 从 after 往后看 limit 行，改 cleared 的：文件断了线；文件现在的修改时间不早于
    决议时刻，或内容标识和这一行的不同（决议之后改过了）；决议没了或后来改了。返回 {seen, cleared,
    after}：after 是下一轮从哪一行之后接着看（到底了回 0）。"""
    limit = L3_ROWS if limit is None else limit
    with db.autocommit() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT r.*, d.start_ms AS decision_ms, d.gone_at AS decision_gone,
                           m.recording_date, m.created_at AS meeting_created_at,
                           CASE WHEN d.id IS NOT NULL AND {superseded_sql('d')} THEN 1 ELSE 0 END AS superseded
                      FROM relations r
                      LEFT JOIN decisions d ON d.id = r.decision_id
                      LEFT JOIN meetings m ON m.id = d.meeting_id
                     WHERE r.kind = 'affects' AND r.status = 'suggested' AND r.origin != 'manual' AND r.id > ?
                     ORDER BY r.id LIMIT ?""",
                (after, limit),
            ).fetchall()
        ]
        stale: list[int] = []
        for row in rows:
            if _stale(connection, row):
                stale.append(int(row["id"]))
    cleared = 0
    if stale:
        with db.transaction() as connection:
            for relation_id in stale:
                cleared += connection.execute(
                    """UPDATE relations SET status = 'cleared', updated_at = ?
                        WHERE id = ? AND status = 'suggested' AND origin != 'manual' AND updated_at <= ?""",
                    (_stamp(now), relation_id, since),
                ).rowcount
    next_after = int(rows[-1]["id"]) if len(rows) >= limit else 0
    return {"seen": len(rows), "cleared": cleared, "after": next_after}


def _stale(connection: Any, row: Mapping[str, Any]) -> bool:
    if row["decision_gone"] is not None or row["superseded"]:
        return True
    if row["recording_date"] is None and row["meeting_created_at"] is None:
        return True
    live = live_file(connection, row)
    if live is None or live.get("content_key") != row["content_key"]:
        return True
    moment = decision_moment(
        {"recording_date": row["recording_date"], "created_at": row["meeting_created_at"]}, row["decision_ms"]
    )
    return moment is None or int(live.get("mtime_ns") or 0) >= _ns(moment)


def stat_guard(row: Mapping[str, Any], state_of: Callable[[str], str]) -> bool:
    """文件面板和预览在这份文件有在问的影响时调：stat 一次，大小或修改时间和索引里的不同（原地改过、
    还没等到整轮）时回 False，调用方把影响的问题从响应里拿掉；不写库。资料盘没插时不 stat，回 True。"""
    from .materials import ROOT_ONLINE

    root = str(row.get("root_path") or "")
    if not root or state_of(root) != ROOT_ONLINE:
        return True
    try:
        info = os.stat(os.path.join(root, *str(row["rel_path"]).split("/")))
    except OSError:
        return False
    return info.st_size == row.get("size") and info.st_mtime_ns == row.get("mtime_ns")


def guarded_questions(questions: list[dict[str, Any]], row: Mapping[str, Any] | None, state_of: Callable[[str], str]) -> list[dict[str, Any]]:
    """有在问的影响时 stat_guard 一次；文件变了就先不给影响的问题（产出的照给）。"""
    if row is None or not any(item["kind"] == "affects" for item in questions):
        return questions
    if stat_guard(row, state_of):
        return questions
    return [item for item in questions if item["kind"] != "affects"]
