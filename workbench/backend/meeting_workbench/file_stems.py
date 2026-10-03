"""文件名怎么变成「会上会说的词」（第二期 2d）。

「260926 报价单 v3 终版.xlsx」会上只会说「报价单」：去掉扩展名、副本编号、版本号（括号里的也算）、
第 N 版、终版、终稿这类修饰，去掉日期和开头的编号，剩下的叫词干。词干的比较键 stem_key 和 light_key
同一套（NFKC、大小写不敏感、去空白和标点），逐字稿也按同一套折叠后比对。

规则一变，库里存的 stem、stem_key 要跟着重算（db.py 的 v19 迁移做了一次）：新旧两套规则的键并存，
同一份「报价单」就会在库里分成两组。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from .project_profile import light_key

# 版本号的两种写法：vN、v1.2、v1.2.3；第 N 版、第 N 稿（阿拉伯数字或中文数字）。词干结尾去掉的和
# file_mentions 在同一个词干的几份文件里按版本号挑文件认的是同一份，改了这里两边一起变。
V_TAG = r"(?<![a-z])v\d{1,3}(?:\.\d{1,3}){0,2}"
ORDINAL = r"第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*[版稿]"
# 结尾的修饰：反复去，直到没有变化。前面可以隔着空格、下划线、横线、点。
_SEP = r"[\s_\-.·]*"
# 修饰本身，一个词整个去掉：短的词不能只削掉尾巴、把长词剩一截在词干里（「最终稿」只削「终稿」剩「最」，
# 「审定稿」只削「定稿」剩「审」，「最终定稿」剩「最终」），所以带前缀的长词写成一条，或者写在短词前面。
_MODIFIER = (
    r"副本(?:\s*\d{1,3})?"
    r"|(?<![a-z])copy(?:\s*\d{1,3})?"
    rf"|{V_TAG}"
    rf"|{ORDINAL}"
    # 定稿、审定稿、送审稿、修订稿、修改稿，前面可以再带「最终」「最后」
    r"|(?:最终|最后)?(?:审定|送审|修订|修改|定)稿"
    r"|(?:最终|最后)?(?:修订|修改)版"
    r"|最终版|最终稿|终版|终稿|初稿"
    r"|(?<![a-z])final"
)
_TRAILING = re.compile(
    # 一串分隔符只从它的第一个字符开始试：sub 逐个位置往后找，不加这一句，一长串点或间隔号里
    # 每个位置都要把后面的整串再吞一遍（二次增长，8000 个间隔号要 3 秒）。结果和不加完全一样。
    r"(?<![\s_\-.·])"
    + _SEP
    + r"(?:"
    + r"\(\d{1,3}\)"  # (1)、（2）经 NFKC 后都是半角
    # 括号里只有一个修饰才去：「(v2)」「（终稿）」去，「(终稿评审)」「(v2版本说明)」不去。
    # 全角的（）［］经 NFKC 后是半角，【】不会变，要单独写。
    + r"|[(\[【]\s*(?:"
    + _MODIFIER
    + r")\s*[)\]】]"
    + r"|"
    + _MODIFIER
    + r")$",
    re.IGNORECASE,
)

# 日期：长的先去，前后不能紧挨数字；不存在的日子（260931、0230）不当日期。
_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?!\d)"),
    re.compile(
        r"(?<!\d)(?P<y>(?:19|20)\d{2})[-._](?P<m>0?[1-9]|1[0-2])[-._](?P<d>0?[1-9]|[12]\d|3[01])(?!\d)"
    ),
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>\d{2})(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)"),
)
# MMDD 只在前后是 - _ 或空格（或者开头结尾）时去，并且至少有一边是分隔符。
_MMDD = re.compile(r"(?:(?<=[\s_\-])|^)(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?=[\s_\-]|$)")

# 时刻（截图名里常见）：10.12.33、下午3.12.33
_TIME = re.compile(r"(?<!\d)(?:上午|下午)?\d{1,2}[.:]\d{2}(?:[.:]\d{2})?(?!\d)")
# 相机、截图、聊天软件自动起的名字：会上不会这么说，整个不用。
_MACHINE_NAME = re.compile(
    r"^(?:img|dsc|dscn|dcim|pxl|vid|mvimg|wechatimg|mmexport|screenshot|screen\s*shot|"
    r"屏幕快照|屏幕截图|截屏|微信图片)(?![a-z])",
    re.IGNORECASE,
)

_LEADING_NUMBER = re.compile(r"^\d{1,3}(?:[_\-.、)]|\s)+")
_LEADING_BRACKET = re.compile(r"^(?:【[^】]*】|\[[^\]]*\])\s*")
_EDGE_SEPARATORS = re.compile(r"^[\s_\-.·]+|[\s_\-.·]+$")
_INNER_SEPARATORS = re.compile(r"([\s_\-])[\s_\-]+")

# 停用词：整个 stem_key 等于这些时不用。
STOPWORDS = frozenset(
    light_key(word)
    for word in (
        "新建",
        "未命名",
        "文档",
        "表格",
        "图片",
        "截图",
        "屏幕快照",
        "微信图片",
        "录音",
        "会议记录",
        "会议纪要",
        "纪要",
        "方案",
        "报告",
        "合同",
        "报价",
        "发票",
        "附件",
        "备份",
        "草稿",
        "目录",
        "说明",
        "汇总",
        "总结",
        "untitled",
        "image",
        "img",
        "screenshot",
        "photo",
        "video",
        "test",
        "demo",
        "data",
        "index",
        "main",
        "config",
        "readme",
        "draft",
        "backup",
        "new",
    )
)
# 2 个汉字的词干：会上要说 2 次以上，并且不在这张常用词表里。
COMMON_TWO_CHAR = frozenset(
    (
        "需求",
        "设计",
        "排期",
        "测试",
        "进度",
        "预算",
        "周报",
        "日报",
        "月报",
        "会议",
        "问题",
        "数据",
        "接口",
        "上线",
        "方案",
        "报告",
        "合同",
        "报价",
        "发票",
        "附件",
        "备份",
        "草稿",
        "目录",
        "说明",
        "汇总",
        "总结",
        "文档",
        "表格",
        "图片",
        "截图",
        "录音",
        "纪要",
        "计划",
        "项目",
        "产品",
        "运营",
        "市场",
        "销售",
        "财务",
        "人事",
        "行政",
        "法务",
        "技术",
        "开发",
        "部署",
        "发布",
        "版本",
        "迭代",
        "复盘",
        "评审",
        "验收",
        "交付",
        "培训",
        "资料",
        "素材",
        "模板",
        "流程",
        "规范",
        "标准",
        "制度",
        "指标",
        "目标",
        "任务",
        "进展",
        "风险",
        "成本",
        "费用",
        "采购",
        "供应",
        "客户",
        "用户",
        "功能",
        "模块",
        "页面",
        "原型",
        "视觉",
        "交互",
        "海报",
        "介绍",
        "简介",
        "清单",
        "列表",
        "明细",
        "统计",
        "分析",
        "调研",
        "访谈",
    )
)

# 可用程度：no 不用；twice 要在逐字稿里出现 2 次以上；yes 出现 1 次就算。
STEM_NO = "no"
STEM_TWICE = "twice"
STEM_YES = "yes"


def _han(char: str) -> bool:
    return "一" <= char <= "鿿" or "㐀" <= char <= "䶿" or "豈" <= char <= "﫿"


def _strip_extension(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and ext and len(ext) <= 8 and ext.isascii() and ext.isalnum():
        return stem
    return name


def _strip_trailing(text: str) -> str:
    previous = None
    while text and text != previous:
        previous = text
        stripped = _TRAILING.sub("", text)
        text = stripped if stripped.strip(" _-.·") else text
    return text


def _real_date(match: re.Match[str]) -> str:
    year = match.groupdict().get("y")
    # 两位年份按 20xx 算；没有年份（MMDD）按闰年算，0229 也算日期
    year_number = 2000 if year is None else int(year) + (2000 if len(year) == 2 else 0)
    try:
        date(year_number, int(match["m"]), int(match["d"]))
    except ValueError:
        return match[0]
    return " "


def _strip_dates(text: str) -> str:
    for pattern in _DATE_PATTERNS:
        text = pattern.sub(_real_date, text)
    text = _TIME.sub(" ", text)
    return _MMDD.sub(_real_date, text)


def _strip_leading(text: str) -> str:
    previous = None
    while text and text != previous:
        previous = text
        text = _LEADING_BRACKET.sub("", text)
        stripped = _LEADING_NUMBER.sub("", text)
        text = stripped if stripped else text
    return text


def _tidy(text: str) -> str:
    text = _INNER_SEPARATORS.sub(r"\1", text)
    return _EDGE_SEPARATORS.sub("", text)


def derive_stem(name: str, *, strip_extension: bool = True) -> str:
    """文件名 → 词干（显示用的原样写法）。去不出东西时返回空串。"""
    text = unicodedata.normalize("NFKC", name or "").strip()
    if _MACHINE_NAME.match(text):
        return ""
    if strip_extension:
        text = _strip_extension(text)
    for _ in range(3):
        before = text
        text = _tidy(_strip_trailing(_tidy(text)))
        text = _tidy(_strip_dates(text))
        text = _tidy(_strip_leading(text))
        text = _tidy(_strip_trailing(text))
        if text == before:
            break
    return text


def stem_key(stem: str) -> str:
    return light_key(stem)


def stem_usability(key: str) -> str:
    """按规格第 6、7 条判断一个 stem_key 能不能拿去比对（项目名、也叫那条在比对时另外判断）。"""
    if not key or key in STOPWORDS:
        return STEM_NO
    han = sum(1 for char in key if _han(char))
    digits = sum(1 for char in key if char.isdigit())
    letters = len(key) - han - digits
    if han == len(key):
        if han >= 3:
            return STEM_YES
        if han == 2 and key not in COMMON_TWO_CHAR:
            return STEM_TWICE
        return STEM_NO
    if han == 0:
        # 含字母的 4 个以上字母数字；纯数字不算
        return STEM_YES if letters >= 1 and len(key) >= 4 else STEM_NO
    # 汉字夹字母：至少 3 个字符、至少 1 个汉字；只夹数字时按汉字个数算
    if letters >= 1:
        return STEM_YES if len(key) >= 3 else STEM_NO
    return STEM_YES if han >= 3 else STEM_NO
