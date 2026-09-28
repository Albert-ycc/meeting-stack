"""从材料里挖项目专属的词（第四期 4h，H4），先在项目页、词典页、会议页等你认。

- 在 deep_links 的本机循环里跑（重活的最后一个 H4），links_enabled 和 glossary_mining_enabled 都开才跑；
  关掉 glossary_mining_enabled 时不再挖，已有的待认词也不显示，行留着。会议转写时整段跳过，每挖一份
  内容、每做一个项目之前各查一次忙信号。
- H4a（seed_round）：每份材料内容按前 6 万字挖「种子」，存 glossary_mining_seeds；只读片段、不查全文表，
  材料全文表还没补完（material_fts_rebuild）时照做。
- H4b（project_pass）：签名变了并且过了 6 小时（新项目立刻），在写事务外算完十步，再开 BEGIN IMMEDIATE
  重算一次签名，没变才只 upsert 变了的行。
- 只挖文字层和 PDF 层读完的内容；图片认出来的字和录音转出来的字不挖（认错的词会重复出现，被当成
  「正确写法」提给你）；代码、数据和字幕文件不挖。文件名只当证据。
- 候选词和种子只在本机：不进词典快照、项目线索、会议全文索引，不发给任何大模型，也不写进卡片和索引。
  点了［记入］才进 glossary_terms（source='material'、is_cue=0），进了词典才随纪要生成交给 AI 纠错。
- evidence_json 只存位置和次数，不存材料原文；材料原话在读的时候现取，只给页面。
- 不算分数：排序按「有听错写法的、说过的、没说过的」三档，档里按次数；接口里没有分数、名次、百分比。
"""
from __future__ import annotations

import bisect
import hashlib
import io
import json
import re
import time
import unicodedata
from array import array
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import glossary
from .db import Database
from .file_stems import COMMON_TWO_CHAR, STOPWORDS
from .material_fts import REBUILD_KEY
from .material_rules import PLAIN_TEXT_EXTS
from .project_profile import GENERIC_FOLDER_NAMES, GENERIC_PROJECT_SPREAD, light_key, norm_key

MINER_VERSION = 1
MINE_CHARS = 60_000
SEEDS_HAN = 18
SEEDS_LATIN = 6
MIN_IN_CONTENT = 2
MIN_DF_UNSPOKEN = 3
OTHER_PROJECTS_DROP = GENERIC_PROJECT_SPREAD
PENDING_CAP = 30
SHOWN_ON_BOARD = 6
PASS_INTERVAL_S = 6 * 3600
PAIR_MIN_HEARD = 2
PAIR_BASE_LEN = (4, 8)
TRANSCRIPT_CHARS_CAP = 4_000_000
AGG_LIMIT = 2_000
POOL_CAP = 150
POOL_QUERY_CAP = 400  # 第 3 步最多查几次（留满 POOL_CAP 个之前）
SPREAD_CAP = 80
PAIR_BASES_CAP = 60
SUB_BASE_MIN = 5  # 长词的头尾几个字也拿来找听错的写法（见第 9 步）
SUB_BASES_CAP = 60
STABILITY_RATIO = 0.5
CLOSED_RATIO = 0.9
ROUND_SECONDS = 5.0
SEED_BATCH = 200
SEED_SHARE_S = 4.0
PROJECTS_PER_ROUND = 3
RECENT_DAYS = 30
HAN_LEN = (3, 8)  # 规格是 3 到 6 字；放到 8 字，见 extract_seeds 里的说明
LATIN_LEN = (3, 20)
NAME_SEG_LEN = (3, 8)
QUOTE_SIDE = 20  # 材料原话：词前后各 20 字
HEARD_SIDE = 10  # 会上的原话：前后各 10 字
EVIDENCE_KEEP = 3
UNDO_WINDOW_S = 600

# 代码、数据和字幕文件不挖（字幕多半是语音识别出来的）
MINING_SKIP_EXTS = PLAIN_TEXT_EXTS - {"txt", "md", "markdown", "csv", "tsv", "tex", "eml", "mht", "mhtml"}
_SKIP_SQL = ", ".join(f"'{ext}'" for ext in sorted(MINING_SKIP_EXTS))

# 当标点用的套话（换成空格，词不会跨过它们）
COMMON_PHRASES = (
    "综上所述", "总的来说", "总而言之", "一方面", "另一方面", "也就是说", "换句话说", "进一步", "情况下",
    "在此基础上", "与此同时", "由此可见", "除此之外", "不仅如此", "即便如此", "尽管如此", "一般来说",
    "通常情况", "具体来说", "简单来说", "事实上", "实际上", "基本上", "原则上", "总体上", "整体上",
    "根据以上", "如下所示", "如上所述", "如图所示", "如表所示", "详见附件", "以下简称", "有关规定",
    "相关规定", "有关部门", "相关部门", "进行了", "开展了", "完成了", "的情况", "的基础上", "的要求",
    "的问题", "的工作", "的时候", "的方式", "的过程中", "过程中", "之一", "等方面", "等工作", "为了",
    "以便于", "是否需要", "需要注意", "请注意", "注意事项", "特此说明", "如有疑问", "谢谢大家",
)
# 常用词：全由它们拼成的片段不要；3 字片段两头是其中的二字词也不要
COMMON_WORDS = frozenset(STOPWORDS | COMMON_TWO_CHAR) | frozenset(
    (
        "我们", "你们", "他们", "大家", "自己", "需要", "进行", "可以", "这个", "那个", "一个", "没有",
        "就是", "还是", "如果", "但是", "所以", "已经", "现在", "然后", "什么", "怎么", "这样", "那么",
        "时候", "这些", "那些", "一些", "可能", "应该", "以及", "或者", "而且", "并且", "通过", "对于",
        "关于", "其中", "目前", "以后", "之前", "之后", "今天", "明天", "一下", "比较", "非常", "主要",
        "相关", "具体", "方面", "情况", "工作", "部分", "内容", "要求", "处理", "完成", "提供", "使用",
        "包括", "根据", "按照", "进一步", "同时", "此外", "另外", "以上", "以下", "为了", "由于", "因此",
        "不是", "还有", "一样", "这里", "那里", "所有", "每个", "其他", "有关", "时间", "地方", "问题",
    )
)
_NAME_EXTRA = frozenset(("说明", "文档", "表格", "汇总", "清单", "明细", "初稿", "终稿", "定稿", "附件", "模板"))
LATIN_STOP = frozenset(
    word.casefold()
    for word in (
        "PDF", "DOC", "DOCX", "XLS", "XLSX", "PPT", "PPTX", "TXT", "CSV", "TSV", "URL", "URI", "HTTP",
        "HTTPS", "WWW", "HTML", "HTM", "JSON", "XML", "YAML", "PNG", "JPG", "JPEG", "GIF", "BMP", "SVG",
        "ZIP", "RAR", "MP3", "MP4", "WAV", "MOV", "COM", "NET", "ORG", "WPS", "UTF", "UTF-8", "ASCII",
        "GBK", "TODO", "FAQ", "PS", "NO", "YES", "THE", "AND", "FOR", "WITH", "FROM", "PAGE", "TEL",
        "FAX", "EMAIL", "MAIL", "EXCEL", "WORD", "WECHAT", "APP", "IOS", "MAC", "WIN", "PC", "OK",
    )
)
_HAN = "一-鿿"
_HAN_RUN = re.compile(f"[{_HAN}]{{{HAN_LEN[0]},}}")
_HAN_ONLY = re.compile(f"[{_HAN}]+")
_LATIN_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*(?![A-Za-z0-9])")
_LATIN_SHAPE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.\-_]*[A-Za-z0-9])?")
_HEX = re.compile(r"[0-9a-fA-F]{6,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-?[0-9a-fA-F]{4}){3}-?[0-9a-fA-F]{12}")
_VERSION = re.compile(r"[vV]?\d+(?:\.\d+)+|[vV]\d+")
_PHRASES = re.compile("|".join(re.escape(item) for item in sorted(COMMON_PHRASES, key=len, reverse=True)))

# 回答的说法（进用词测试）
PROJECT_MISSING = "项目不存在"
TERM_GONE = "这个词已经不在了"
ALREADY_DONE = "这个词已经处理过了"
UNDO_EXPIRED = "已超过撤销时间，请直接改回"
UNDO_TWICE = "已经撤销过了"
TERM_CHANGED = "这个词后来改过了，请在词典里直接改"
TERM_INVALID = "这个词不能记入词典"
ACCEPTED_TEXT = "已记入『{term}』"
ACCEPTED_WRONGS_TEXT = "已记入『{term}』，错写：{wrongs}"
APPENDED_TEXT = "已把『{wrongs}』记成『{term}』的错写"
ALREADY_TEXT = "『{term}』已经在词典里了"
SKIPPED_TEXT = "已记入『{term}』；『{skipped}』已经用在别的词条上，没加成错写"
SKIPPED_TAIL = "；『{skipped}』已经用在别的词条上，没加成错写"
NOTHING_ADDED_TEXT = "『{skipped}』已经用在别的词条上，没加成错写"
KEEP_ONE_WRONG = "至少留一个错写"
REJECTED_TEXT = "以后不再提『{term}』"
UNDONE_TEXT = "已撤销，『{term}』回到这里"


class CandidateError(Exception):
    """接口层转成 HTTP 错误：status 和给人看的一句话。"""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


def enabled(settings: Any) -> bool:
    """两个开关都开才挖、才显示待认词。"""
    return bool(getattr(settings, "links_enabled", False)) and bool(
        getattr(settings, "glossary_mining_enabled", False)
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _is_han(text: str) -> bool:
    return bool(text) and _HAN_ONLY.fullmatch(text) is not None


# ---------------------------------------------------------------------- 纯函数


def _all_common(text: str) -> bool:
    """整段都能切成常用词（2 到 4 字）。"""
    reach = [False] * (len(text) + 1)
    reach[0] = True
    for start in range(len(text)):
        if not reach[start]:
            continue
        for size in (2, 3, 4):
            if start + size <= len(text) and text[start : start + size] in COMMON_WORDS:
                reach[start + size] = True
    return reach[len(text)]


# 对规格的补充：除了 glossary._EXPAND_STOP_CHARS，这几个介词、指代字打头结尾的片段也不要（由北辰科研仓、
# 由项目组统一配置、该系统）
_SEED_EDGE_STOPS = frozenset("由将于该此各每")


def _han_ok(fragment: str) -> bool:
    if fragment[0] in glossary._EXPAND_STOP_CHARS or fragment[-1] in glossary._EXPAND_STOP_CHARS:
        return False
    if fragment[0] in _SEED_EDGE_STOPS or fragment[-1] in _SEED_EDGE_STOPS:
        return False
    if _all_common(fragment):
        return False
    if len(fragment) == 3 and (fragment[:2] in COMMON_WORDS or fragment[1:] in COMMON_WORDS):
        return False
    return True


def latin_ok(token: str) -> bool:
    """字母词：3 到 20 个字符，全大写、驼峰或字母加数字（GLP-1、CRF、ESG、OpenAI）。
    不要 PDF、URL 这类、十六进制串和 uuid、版本号（v1.2）、普通小写词。"""
    if not LATIN_LEN[0] <= len(token) <= LATIN_LEN[1] or not _LATIN_SHAPE.fullmatch(token):
        return False
    letters = [char for char in token if char.isalpha()]
    if not letters:
        return False
    if token.casefold() in LATIN_STOP:
        return False
    if _UUID.fullmatch(token) or _VERSION.fullmatch(token):
        return False
    if _HEX.fullmatch(token) and any(char.isdigit() for char in token):
        return False
    has_digit = any(char.isdigit() for char in token)
    upper = [char for char in letters if char.isupper()]
    if len(upper) == len(letters):
        return len(letters) >= 2 or has_digit
    if upper and any(char.isupper() for char in token[1:]):
        return True  # 驼峰：OpenAI、iPhone
    return has_digit and len(letters) >= 2  # 小写加数字：gpt4；普通小写词不要


_MIXED = "\x00"  # 左邻（右邻）不止一种


def _stuck_sides(runs: Sequence[str], kept: dict[str, int]) -> set[str]:
    """左邻或右邻总是同一个汉字的片段（这份内容里出现至少 2 次，kept 里的都是）。一串汉字的头尾是边界
    （前后是标点、数字、字母或套话），的、了这类虚字也算边界，边界不算「同一个字」。每个片段只记见过的第一个邻字或「不止一种」。"""
    left: dict[str, str] = {}
    right: dict[str, str] = {}
    sizes = sorted({len(fragment) for fragment in kept})
    stops = glossary._EXPAND_STOP_CHARS
    for run in runs:
        length = len(run)
        for size in sizes:
            for start in range(length - size + 1):
                fragment = run[start : start + size]
                if fragment not in kept:
                    continue
                before = run[start - 1] if start else ""
                after = run[start + size] if start + size < length else ""
                # 的、了、和这类虚字也当边界：「北辰科研仓的」两次都跟「的」不说明它是半截
                if before in stops:
                    before = ""
                if after in stops:
                    after = ""
                seen = left.get(fragment)
                if seen is None:
                    left[fragment] = before
                elif seen != before:
                    left[fragment] = _MIXED
                seen = right.get(fragment)
                if seen is None:
                    right[fragment] = after
                elif seen != after:
                    right[fragment] = _MIXED
    return {
        fragment
        for fragment in kept
        if left.get(fragment, _MIXED) not in ("", _MIXED) or right.get(fragment, _MIXED) not in ("", _MIXED)
    }


def extract_seeds(text: str) -> list[tuple[str, int]]:
    """一份内容的种子：最多 18 个汉字词和 6 个字母词，带在这份里出现的次数。纯函数。"""
    text = unicodedata.normalize("NFKC", text[:MINE_CHARS])
    text = _PHRASES.sub(" ", text)
    runs = [match.group(0) for match in _HAN_RUN.finditer(text)]
    # 逐级数：长一个字的片段只在两截都够次数的位置上数，内存峰值只有 3 字那一级
    first: Counter[str] = Counter(run[start : start + 3] for run in runs for start in range(len(run) - 2))
    level = {fragment: count for fragment, count in first.items() if count >= MIN_IN_CONTENT}
    del first
    frequent = dict(level)
    for size in range(HAN_LEN[0] + 1, HAN_LEN[1] + 1):
        grown: Counter[str] = Counter()
        for run in runs:
            for start in range(len(run) - size + 1):
                if run[start : start + size - 1] in level and run[start + 1 : start + size] in level:
                    grown[run[start : start + size]] += 1
        level = {fragment: count for fragment, count in grown.items() if count >= MIN_IN_CONTENT}
        del grown
        frequent.update(level)
    kept = {fragment: count for fragment, count in frequent.items() if _han_ok(fragment)}
    del frequent, level
    # 对规格算法的补充（复查后加的）：规格只说「3 到 6 字、被更长且次数相同的片段包住的不要」。在真实风格的
    # 材料上，一个词后面总跟着同一串字（司美格鲁肽注射液、受试者用药记录、云图科研用药平台）时，3 到 6 字的
    # 滑窗全都够次数，真词被一截截包住去掉，留下的是「受试者用药记」「格鲁肽注射液」这类半截。所以：
    # 1. 种子最长放到 8 字（和 PAIR_BASE_LEN 的上限一致），整串能留下来；
    # 2. 左右邻字：一个片段在这份内容里的左邻（或右邻）总是同一个汉字，就是更长的词的一截，丢掉；
    #    邻字是标点、套话或一串汉字的头尾时算「边界」，不算同一个字；
    # 3. 被包住只认两端都「封闭」的长片段（左右邻字都不止一种，或是边界），半截包不住别的片段。
    stuck = _stuck_sides(runs, kept)
    del runs
    halves = {fragment for fragment in kept if fragment in stuck}
    by_len = sorted((fragment for fragment in kept if fragment not in halves), key=len, reverse=True)
    covered: set[str] = set()
    for long in by_len:
        if len(long) <= HAN_LEN[0]:
            continue
        count = kept[long]
        for size in range(HAN_LEN[0], len(long)):
            for start in range(0, len(long) - size + 1):
                inner = long[start : start + size]
                if kept.get(inner) == count:
                    covered.add(inner)
    han = sorted(
        ((fragment, count) for fragment, count in kept.items() if fragment not in covered and fragment not in halves),
        key=lambda item: (-item[1] * len(item[0]), item[0]),
    )[:SEEDS_HAN]
    latin_counts: Counter[str] = Counter(
        match.group(0) for match in _LATIN_TOKEN.finditer(text) if latin_ok(match.group(0))
    )
    latin = sorted(
        ((token, count) for token, count in latin_counts.items() if count >= MIN_IN_CONTENT),
        key=lambda item: (-item[1], item[0]),
    )[:SEEDS_LATIN]
    return han + latin


def name_segments(stem: str) -> list[str]:
    """文件名切出来的汉字段（3 到 8 字），两头的常用词去掉。只当证据，不直接进候选池。"""
    stem = unicodedata.normalize("NFKC", stem or "")
    segments: list[str] = []
    for run in _HAN_ONLY.findall(stem):
        changed = True
        while changed and run:
            changed = False
            for word in sorted(COMMON_WORDS | _NAME_EXTRA, key=len, reverse=True):
                if len(run) - len(word) >= NAME_SEG_LEN[0] and run.startswith(word):
                    run, changed = run[len(word) :], True
                if len(run) - len(word) >= NAME_SEG_LEN[0] and run.endswith(word):
                    run, changed = run[: -len(word)], True
        if NAME_SEG_LEN[0] <= len(run) <= NAME_SEG_LEN[1] and not _all_common(run) and run not in segments:
            segments.append(run)
    return segments


@dataclass
class Pair:
    base: str
    wrong: str
    heard: int
    positions: list[int] = field(default_factory=list)


def _occurrences(haystack: str, needle: str) -> Iterable[int]:
    start = haystack.find(needle)
    while start != -1:
        yield start
        start = haystack.find(needle, start + 1)


def find_pairs(
    bases: Sequence[str], transcript: str, material_has: Callable[[str], bool]
) -> list[Pair]:
    """会上可能听错的写法：只给 4 到 8 字的汉字词找，和原词只差一个字、听到至少 2 次、任何材料里都没有。

    对每个位置 i，拿 i 左右两边较长的那段（至少 2 字）在逐字稿里找，再核对整段只在 i 这个字不一样。
    互相包含的写法只留听到多的那个。"""
    found: dict[str, Pair] = {}
    for base in bases:
        size = len(base)
        if not PAIR_BASE_LEN[0] <= size <= PAIR_BASE_LEN[1] or not _is_han(base):
            continue
        per_base: dict[str, list[int]] = {}
        for index in range(size):
            left, right = base[:index], base[index + 1 :]
            piece, offset = (left, 0) if len(left) >= len(right) else (right, index + 1)
            if len(piece) < 2:
                continue
            for position in _occurrences(transcript, piece):
                start = position - offset
                if start < 0 or start + size > len(transcript):
                    continue
                candidate = transcript[start : start + size]
                if candidate == base or candidate[:index] != base[:index] or candidate[index + 1 :] != base[index + 1 :]:
                    continue
                if not _is_han(candidate[index]):
                    continue
                spots = per_base.setdefault(candidate, [])
                if start not in spots:
                    spots.append(start)
        for wrong, spots in per_base.items():
            if len(spots) < PAIR_MIN_HEARD or wrong in found:
                continue
            if material_has(wrong):
                continue
            found[wrong] = Pair(base=base, wrong=wrong, heard=len(spots), positions=sorted(spots))
    ordered = sorted(found.values(), key=lambda pair: (-pair.heard, pair.wrong))
    kept: list[Pair] = []
    for pair in ordered:
        if any(pair.wrong in other.wrong or other.wrong in pair.wrong for other in kept):
            continue
        kept.append(pair)
    return kept


# ---------------------------------------------------------------------- 挑内容


def _mineable_files(alias: str = "f") -> str:
    """活文件、normal 区、扩展名不在跳过表里。"""
    return (
        f"{alias}.gone_at IS NULL AND {alias}.zone = 'normal' "
        f"AND lower({alias}.ext) NOT IN ({_SKIP_SQL})"
    )


def _project_keys_sql() -> str:
    """这个项目活着的、能挖的内容（参数 :pid）。"""
    return f"""SELECT DISTINCT f.content_key FROM material_files f
                 JOIN project_material_roots r ON r.id = f.root_id
                 JOIN material_contents c ON c.content_key = f.content_key
                WHERE r.project_id = :pid AND {_mineable_files()}
                  AND c.layer IN ('text', 'pdf') AND c.state = 'done'"""


def _source_sig(row: Any) -> str:
    return f"{row['extractor'] or ''}:{row['extractor_version'] or 0}:{row['chars'] or 0}:{row['chunks'] or 0}"


_DUE_SEEDS = f"""
  FROM material_contents c
  LEFT JOIN glossary_mining_seeds s ON s.content_key = c.content_key
 WHERE c.layer IN ('text', 'pdf') AND c.state = 'done'
   AND (s.content_key IS NULL OR s.miner != {MINER_VERSION}
        OR s.source_sig != (COALESCE(c.extractor, '') || ':' || COALESCE(c.extractor_version, 0) || ':'
                            || COALESCE(c.chars, 0) || ':' || COALESCE(c.chunks, 0)))
   AND EXISTS (SELECT 1 FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                WHERE f.content_key = c.content_key AND {_mineable_files()})"""


def seeds_due(connection: Any) -> int:
    """还要挖种子的内容数（健康检查的 waiting.terms）。"""
    return int(connection.execute(f"SELECT COUNT(*) {_DUE_SEEDS}").fetchone()[0])


def read_text(connection: Any, content_key: str) -> str:
    """按 ordinal 读片段，读满 6 万字为止。"""
    parts: list[str] = []
    total = 0
    for row in connection.execute(
        "SELECT text FROM material_chunks WHERE content_key = ? ORDER BY ordinal", (content_key,)
    ):
        text = row["text"] or ""
        parts.append(text)
        total += len(text)
        if total >= MINE_CHARS:
            break
    return "\n".join(parts)[:MINE_CHARS]


def seed_round(
    conn: Any,
    deadline: float,
    busy: Callable[[], bool],
    *,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
) -> int:
    """H4a：没有种子、miner 旧了或 source_sig 对不上的内容，最近 30 天有会的项目的先挖，再按
    updated_at 从新到旧，每次挑 200 份。每份之前查忙信号和时间。三样都没变就不写。返回挖了几份。"""
    moment = now or datetime.now(UTC)
    recent = _stamp(moment - timedelta(days=RECENT_DAYS))
    rows = conn.execute(
        f"""SELECT c.content_key, c.extractor, c.extractor_version, c.chars, c.chunks,
                   s.miner, s.source_sig, s.terms_json,
                   EXISTS (SELECT 1 FROM material_files f2
                             JOIN project_material_roots r2 ON r2.id = f2.root_id
                             JOIN meetings m ON m.project_id = r2.project_id
                            WHERE f2.content_key = c.content_key AND f2.gone_at IS NULL
                              AND COALESCE(m.recording_date, m.created_at) >= ?) AS recent
            {_DUE_SEEDS}
             ORDER BY recent DESC, c.updated_at DESC, c.content_key LIMIT ?""",
        (recent, SEED_BATCH),
    ).fetchall()
    done = 0
    for row in rows:
        if clock() >= deadline or busy():
            break
        terms = extract_seeds(read_text(conn, row["content_key"]))
        terms_json = json.dumps([[term, count] for term, count in terms], ensure_ascii=False)
        sig = _source_sig(row)
        if row["miner"] == MINER_VERSION and row["source_sig"] == sig and row["terms_json"] == terms_json:
            continue
        with conn:
            conn.execute(
                """INSERT INTO glossary_mining_seeds(content_key, miner, source_sig, terms_json, mined_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(content_key) DO UPDATE SET miner = excluded.miner,
                       source_sig = excluded.source_sig, terms_json = excluded.terms_json,
                       mined_at = excluded.mined_at""",
                (row["content_key"], MINER_VERSION, sig, terms_json, _stamp(moment)),
            )
        done += 1
    return done


# ---------------------------------------------------------------------- 签名


def signatures(conn: Any, only: str | None = None) -> dict[str, str]:
    """每个项目的签名：挖词程序版本、file_mentions.project_sigs（词干版本、项目名和也叫、根目录、词典
    最后一次改动）、种子数和最新 mined_at、会议数和当前逐字稿版本、答过的词数。"""
    from .file_mentions import project_sigs

    base = project_sigs(conn)
    where = "AND r.project_id = :only" if only else ""
    params = {"only": only, "miner": MINER_VERSION}
    seeds = {
        row["project_id"]: f"{row['n']}:{row['at']}"
        for row in conn.execute(
            f"""SELECT r.project_id, COUNT(DISTINCT s.content_key) AS n, MAX(s.mined_at) AS at
                  FROM project_material_roots r
                  JOIN material_files f ON f.root_id = r.id
                  JOIN glossary_mining_seeds s ON s.content_key = f.content_key AND s.miner = :miner
                 WHERE {_mineable_files()} {where}
                 GROUP BY r.project_id""",
            params,
        ).fetchall()
    }
    meetings: dict[str, list[str]] = {}
    for row in conn.execute(
        f"""SELECT project_id, id, COALESCE(current_transcript_version_id, '') AS tv FROM meetings
             WHERE project_id IS NOT NULL {'AND project_id = :only' if only else ''} ORDER BY id""",
        params,
    ).fetchall():
        meetings.setdefault(row["project_id"], []).append(f"{row['id']}={row['tv']}")
    answered = {
        row["project_id"]: row["n"]
        for row in conn.execute(
            f"""SELECT project_id, COUNT(*) AS n FROM glossary_candidates
                 WHERE status IN ('accepted', 'rejected') {'AND project_id = :only' if only else ''}
                 GROUP BY project_id""",
            params,
        ).fetchall()
    }
    result: dict[str, str] = {}
    for project_id, sig in base.items():
        if only and project_id != only:
            continue
        ids = meetings.get(project_id, [])
        digest = hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:16]
        result[project_id] = (
            f"{MINER_VERSION}|{sig}|{seeds.get(project_id, '0:')}|{len(ids)}:{digest}|{answered.get(project_id, 0)}"
        )
    return result


def due_projects(conn: Any, now: datetime) -> list[tuple[str, str]]:
    """（项目，签名）：签名变了并且离上次过了 6 小时，还没做过的立刻；上次做得最早的先。"""
    sigs = signatures(conn)
    scans = {
        row["project_id"]: (row["sig"], row["mined_at"])
        for row in conn.execute("SELECT project_id, sig, mined_at FROM glossary_mining_scan").fetchall()
    }
    cutoff = now - timedelta(seconds=PASS_INTERVAL_S)
    due: list[tuple[str, str, str]] = []
    for project_id, sig in sigs.items():
        scan = scans.get(project_id)
        if scan is None:
            due.append(("", project_id, sig))
            continue
        if scan[0] == sig:
            continue
        try:
            last = datetime.fromisoformat(str(scan[1]))
        except ValueError:
            last = None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is None or last <= cutoff:
            due.append((str(scan[1]), project_id, sig))
    due.sort()
    return [(project_id, sig) for _at, project_id, sig in due]


# ---------------------------------------------------------------------- H4b


@dataclass
class Transcript:
    """一个项目当前逐字稿拼起来的文字，和位置到（会议，段落开始时间）的对照。

    对照表用 array 存（每段 8 字节的开始位置、4 字节的会议下标、8 字节的开始时间），会议 id 每场只存一次；
    400 万字、十几万段时常驻约 10MB，用 list 和元组要 30MB。"""

    text: str = ""
    starts: array = field(default_factory=lambda: array("q"))
    meeting_at: array = field(default_factory=lambda: array("l"))
    start_ms: array = field(default_factory=lambda: array("q"))
    meetings: list[str] = field(default_factory=list)
    meeting_starts: list[int] = field(default_factory=list)  # 每场会从第几个字开始（一场一个数）

    def where(self, position: int) -> tuple[str, int]:
        index = max(0, bisect.bisect_right(self.starts, position) - 1)
        return self.meetings[self.meeting_at[index]], self.start_ms[index]

    def meeting_count(self, positions: Iterable[int]) -> int:
        """这些位置落在几场会里（只查每场会的开头，不查十几万段的对照表）。"""
        return len({bisect.bisect_right(self.meeting_starts, position) for position in positions})


def load_transcript(conn: Any, project_id: str) -> Transcript:
    """当前逐字稿按会议、段落拼起来，最多 400 万字。边读边写进 StringIO，不先攒一个段落列表。"""
    result = Transcript()
    buffer = io.StringIO()
    total = 0
    for row in conn.execute(
        """SELECT s.meeting_id, s.start_ms, s.text FROM meetings m
             JOIN segments s ON s.version_id = m.current_transcript_version_id
            WHERE m.project_id = ? ORDER BY m.id, s.ordinal""",
        (project_id,),
    ):
        text = unicodedata.normalize("NFKC", row["text"] or "")
        if total + len(text) + 1 > TRANSCRIPT_CHARS_CAP:
            break
        if total:
            buffer.write("\n")
            total += 1
        if not result.meetings or result.meetings[-1] != row["meeting_id"]:
            result.meetings.append(row["meeting_id"])
            result.meeting_starts.append(total)
        result.starts.append(total)
        result.meeting_at.append(len(result.meetings) - 1)
        result.start_ms.append(int(row["start_ms"] or 0))
        buffer.write(text)
        total += len(text)
    result.text = buffer.getvalue()
    buffer.close()
    return result


@dataclass
class Found:
    """算出来的一项（折叠后的一个词）：词本身一行（wrong 为空），每个听错的写法另一行。"""

    term: str
    key: str
    df: int = 0
    total: int = 0
    names: int = 0
    spoken: int = 0
    meetings: int = 0
    existing: bool = False
    text: list[dict[str, Any]] = field(default_factory=list)
    name_keys: list[str] = field(default_factory=list)
    heard: list[dict[str, Any]] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)
    pair_heard: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pair_meetings: dict[str, int] = field(default_factory=dict)


def rank_key(has_pairs: bool, heard: int, spoken: int, files: int, names: int, term: str) -> tuple:
    """有听错写法的在前（按听到次数），再是说过的（按次数），再是没说过的（按文件数加文件名次数）。"""
    if has_pairs:
        return (0, -heard, term)
    if spoken:
        return (1, -spoken, term)
    return (2, -(files + names), term)


@dataclass
class PassStats:
    project_id: str
    sig: str = ""
    items: list[Found] = field(default_factory=list)
    pending: int = 0
    dropped: int = 0
    written: int = 0
    stale: bool = False
    abandoned: str | None = None  # busy 或 budget：这一遍放弃了，什么都没写
    queries: Counter = field(default_factory=Counter)


def _known_names(conn: Any, project_id: str) -> tuple[set[str], list[str], set[str], set[str]]:
    """不提的词：(light_key 集合, 用来查「被包含」的汉字名字, 听错写法不能撞的名字, 本项目答过的 key)。"""
    keys: set[str] = set()
    han_names: list[str] = []
    term_names: set[str] = set()

    def add(name: Any) -> None:
        text = unicodedata.normalize("NFKC", str(name or "")).strip()
        if not text:
            return
        keys.add(light_key(text))
        if _HAN_ONLY.search(text):
            han_names.append(text)

    for row in conn.execute("SELECT term, aliases, also FROM glossary_terms").fetchall():
        names = [row["term"], *json.loads(row["aliases"] or "[]"), *json.loads(row["also"] or "[]")]
        for name in names:
            add(name)
            term_names.add(str(name))
    from .project_profile import also_name_list

    for row in conn.execute("SELECT name, also_names FROM projects").fetchall():
        for name in (row["name"], *also_name_list(row["also_names"])):
            add(name)
            term_names.add(str(name))
    for row in conn.execute("SELECT path FROM project_material_roots").fetchall():
        add(str(row["path"]).rstrip("/").rsplit("/", 1)[-1])
    for name in (*GENERIC_FOLDER_NAMES, *STOPWORDS, *COMMON_TWO_CHAR):
        keys.add(light_key(name))
    for row in conn.execute("SELECT name, decision FROM name_decisions").fetchall():
        if row["decision"] == "ignored":
            add(row["name"])
        term_names.add(str(row["name"]))
    for row in conn.execute(
        "SELECT correct FROM glossary_suggestions WHERE status IN ('pending', 'confirmed')"
    ).fetchall():
        add(row["correct"])
    answered = {
        row["term_key"]
        for row in conn.execute(
            """SELECT DISTINCT term_key FROM glossary_candidates
                WHERE project_id = ? AND status IN ('accepted', 'rejected')""",
            (project_id,),
        ).fetchall()
    }
    return keys, han_names, term_names, answered


def _decided_keys(conn: Any) -> set[str]:
    return {row["norm_key"] for row in conn.execute("SELECT norm_key FROM name_decisions").fetchall()}


class PassAbandoned(Exception):
    """忙了（开始转写）或过了截止时间：这一遍放弃，什么都不写（也不写 scan 行），下一轮再做。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _fill_keys(conn: Any, project_id: str) -> None:
    """第 1 步的临时表：这个项目活着的、能挖的内容放进 temp 库的 _gm_keys，汇总和取证据都连它，
    不再每个词重算一遍。只写 temp 库，不碰主库的锁；写 temp 表会让 sqlite3 隐式开事务，填完马上提交，
    后面的只读查询不挂在事务里，也不和之后的 BEGIN IMMEDIATE 冲突。"""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gm_keys(content_key TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM temp._gm_keys")
    conn.execute(f"INSERT OR IGNORE INTO temp._gm_keys(content_key) {_project_keys_sql()}", {"pid": project_id})
    if conn.in_transaction:
        conn.commit()


def _drop_keys(conn: Any) -> None:
    conn.execute("DROP TABLE IF EXISTS temp._gm_keys")
    if conn.in_transaction:
        conn.commit()


def compute(
    conn: Any,
    project_id: str,
    *,
    seeds: dict[str, list[tuple[str, int]]] | None = None,
    busy: Callable[[], bool] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PassStats:
    """第 1 到 10 步，只读（只写 temp 库的 _gm_keys）。seeds 给了就用它（命令行 --dry-run 当场挖的），
    不读种子表。在第 3、6、7、9 步之前（第 3 步里每查 50 个词也）查一次忙信号和截止时间，忙了或超了
    抛 PassAbandoned。"""

    def checkpoint() -> None:
        if busy is not None and busy():
            raise PassAbandoned("busy")
        if deadline is not None and clock() >= deadline:
            raise PassAbandoned("budget")

    _fill_keys(conn, project_id)
    try:
        return _compute(conn, project_id, seeds, checkpoint)
    finally:
        _drop_keys(conn)


def _compute(
    conn: Any,
    project_id: str,
    seeds: dict[str, list[tuple[str, int]]] | None,
    checkpoint: Callable[[], None],
) -> PassStats:
    stats = PassStats(project_id=project_id)
    q = stats.queries
    params = {"pid": project_id, "miner": MINER_VERSION}
    # 1. 汇总
    if seeds is None:
        q["agg"] += 1
        agg = [
            (row["term"], int(row["df"]), int(row["n"] or 0))
            for row in conn.execute(
                f"""SELECT json_extract(j.value, '$[0]') AS term, COUNT(*) AS df,
                           SUM(json_extract(j.value, '$[1]')) AS n
                      FROM glossary_mining_seeds s
                      JOIN temp._gm_keys k ON k.content_key = s.content_key, json_each(s.terms_json) j
                     WHERE s.miner = :miner
                     GROUP BY 1 ORDER BY df DESC, n DESC, term LIMIT {AGG_LIMIT}""",
                params,
            ).fetchall()
            if row["term"]
        ]
    else:
        df: Counter[str] = Counter()
        total: Counter[str] = Counter()
        for terms in seeds.values():
            for term, count in terms:
                df[term] += 1
                total[term] += count
        agg = sorted(((term, df[term], total[term]) for term in df), key=lambda item: (-item[1], -item[2], item[0]))
        agg = agg[:AGG_LIMIT]
    files = conn.execute(
        """SELECT f.stem, f.name, f.content_key FROM material_files f
             JOIN project_material_roots r ON r.id = f.root_id
            WHERE r.project_id = ? AND f.gone_at IS NULL AND f.zone = 'normal'
            ORDER BY f.id""",
        (project_id,),
    ).fetchall()
    segment_names: Counter[str] = Counter()
    for row in files:
        for segment in name_segments(row["stem"] or row["name"] or ""):
            segment_names[segment] += 1
    # 2. 候选池：正文 df 至少 2；或者文件名段出现至少 2 次（正文有或会上说过，第 6 步后核对）
    pool: dict[str, list[int]] = {}
    body = {term: (d, n) for term, d, n in agg}
    for term, d, n in agg:
        if d >= 2:
            pool[term] = [d, n]
    for segment, count in segment_names.items():
        if count >= 2 and segment not in pool:
            d, n = body.get(segment, (0, 0))
            pool[segment] = [d, n]
    # 对规格算法的补充：池里有一个多一个字、次数有它 0.9 以上的词，短的只是那个词的一截，丢掉
    # （种子是一份一份挖的，各份里截出来的半截在项目这一层还会碰上整词，这里再兜一次）
    longer_total: dict[str, int] = {}
    for term, (_d, n) in pool.items():
        if len(term) > HAN_LEN[0]:
            for part in (term[1:], term[:-1]):
                longer_total[part] = max(longer_total.get(part, 0), n)
    for term in list(pool):
        n = pool[term][1]
        if n > 0 and longer_total.get(term, 0) >= CLOSED_RATIO * n:
            pool.pop(term)
    keys_known, han_known, term_names, answered = _known_names(conn, project_id)

    def excluded(term: str) -> bool:
        key = light_key(term)
        if not key or key in keys_known or key in answered:
            return True
        if _is_han(term) and any(term in name for name in han_known):
            return True
        try:
            glossary.validate_term_text(term, what="术语")
        except glossary.GlossaryError:
            return True
        return False

    def name_count(term: str) -> int:
        return sum(1 for row in files if term in (row["stem"] or row["name"] or ""))

    ranked_pool = sorted(
        (term for term in pool if not excluded(term)),
        key=lambda term: (-(pool[term][0] + segment_names.get(term, 0)), term),
    )
    # 3. 别的项目的材料：按排好的顺序逐个查，留满 150 个（POOL_CAP）或查满 400 次（POOL_QUERY_CAP）为止
    checkpoint()
    found: dict[str, Found] = {}
    for term in ranked_pool:
        if len(found) >= POOL_CAP or q["spread_materials"] >= POOL_QUERY_CAP:
            break
        if q["spread_materials"] and q["spread_materials"] % 50 == 0:
            checkpoint()
        q["spread_materials"] += 1
        others = conn.execute(
            """SELECT DISTINCT r.project_id FROM material_chunks_fts
                 JOIN material_chunks c ON c.id = material_chunks_fts.rowid
                 JOIN material_files f ON f.content_key = c.content_key AND f.gone_at IS NULL
                 JOIN project_material_roots r ON r.id = f.root_id
                WHERE material_chunks_fts MATCH ? AND r.project_id != ? LIMIT ?""",
            (_fts_phrase(term), project_id, OTHER_PROJECTS_DROP),
        ).fetchall()
        if len(others) < OTHER_PROJECTS_DROP:
            found[term] = Found(term=term, key=light_key(term), df=pool[term][0], total=pool[term][1])
    chunk_counts: dict[str, int] = {}

    def chunks_with(text: str) -> int:
        if text not in chunk_counts:
            q["boundary"] += 1
            chunk_counts[text] = int(
                conn.execute(
                    """SELECT COUNT(*) FROM (SELECT 1 FROM material_chunks_fts
                        WHERE material_chunks_fts MATCH ? LIMIT 100000)""",
                    (_fts_phrase(text),),
                ).fetchone()[0]
            )
        return chunk_counts[text]

    # 4. 边界：4 字以上的汉字词，去掉头一个字或尾一个字后的片段在全部材料里的次数是它的 2 倍以上
    for term in list(found):
        if len(term) < 4 or not _is_han(term):
            continue
        own = chunks_with(term)
        if any(own <= STABILITY_RATIO * chunks_with(part) for part in (term[1:], term[:-1])):
            found.pop(term)
    # 5. 包含：更长的、留下的词包住它，次数又有它的 0.9 以上
    for term in sorted(found, key=len):
        mine = found[term].total
        if any(
            other != term and term in other and found[other].total >= CLOSED_RATIO * mine
            for other in found
        ):
            found.pop(term)
    # 6. 会上说了几次
    checkpoint()
    transcript = load_transcript(conn, project_id)
    for term, item in found.items():
        places = list(_occurrences(transcript.text, term))
        item.spoken = len(places)
        item.meetings = transcript.meeting_count(places)
        item.heard = _heard(transcript, places)
        item.names = name_count(term)
    # 7. 别的项目的会
    checkpoint()
    spoken = sorted((term for term in found if found[term].spoken), key=lambda term: (-found[term].spoken, term))
    for term in spoken[:SPREAD_CAP]:
        q["spread_meetings"] += 1
        others = conn.execute(
            """SELECT DISTINCT m.project_id FROM segments_fts
                 JOIN meetings m ON m.id = segments_fts.meeting_id
                  AND m.current_transcript_version_id = segments_fts.version_id
                WHERE segments_fts MATCH ? AND m.project_id IS NOT NULL AND m.project_id != ? LIMIT ?""",
            (_fts_phrase(term), project_id, OTHER_PROJECTS_DROP),
        ).fetchall()
        if len(others) >= OTHER_PROJECTS_DROP:
            found.pop(term)
    # 8. 留下：说过，或正文 df 至少 3；只有文件名的，正文里要有或会上说过
    for term in list(found):
        item = found[term]
        if not (item.spoken or item.df >= MIN_DF_UNSPOKEN):
            found.pop(term)
    # 9. 听错的写法
    checkpoint()
    bases: list[str] = [
        term
        for term in sorted(found, key=lambda term: (-found[term].spoken, term))
        if _is_han(term) and PAIR_BASE_LEN[0] <= len(term) <= PAIR_BASE_LEN[1]
    ]
    for row in conn.execute(
        """SELECT term FROM glossary_terms WHERE project_id = ? OR scope = ? ORDER BY term""",
        (project_id, glossary.PUBLIC_SCOPE),
    ).fetchall():
        term = unicodedata.normalize("NFKC", row["term"])
        if _is_han(term) and PAIR_BASE_LEN[0] <= len(term) <= PAIR_BASE_LEN[1] and term not in found:
            bases.append(term)
    bases = bases[:PAIR_BASES_CAP]
    # 对规格算法的补充：留下的长词（7 字以上）的头几个字、尾几个字（至少 5 字，剩下至少 2 字）也拿来找。
    # 材料里「司美格鲁肽」总跟着「注射液」，种子只留得下整串；会上说的却是「司美格鲁太」。只有听到了
    # 这样的写法（至少 2 次、任何材料里都没有，换的不是的、了这类虚字），这一截才作为一个词提出来。
    parents: dict[str, str] = {}
    for term in sorted(found, key=lambda term: (-found[term].df, term)):
        if not _is_han(term) or len(term) < SUB_BASE_MIN + 2:
            continue
        for size in range(min(len(term) - 2, PAIR_BASE_LEN[1]), SUB_BASE_MIN - 1, -1):
            for sub in (term[:size], term[-size:]):
                if sub in parents or sub in found or sub in bases or not _han_ok(sub) or excluded(sub):
                    continue
                parents[sub] = term
    subs = list(parents)[:SUB_BASES_CAP]
    decided = _decided_keys(conn)
    material_cache: dict[str, bool] = {}

    def material_has(text: str) -> bool:
        if text not in material_cache:
            q["pair_material"] += 1
            material_cache[text] = (
                conn.execute(
                    "SELECT 1 FROM material_chunks_fts WHERE material_chunks_fts MATCH ? LIMIT 1",
                    (_fts_phrase(text),),
                ).fetchone()
                is not None
            )
        return material_cache[text]

    q["pair_bases"] = len(bases)
    q["pair_sub_bases"] = len(subs)
    sub_set = set(subs)
    for pair in find_pairs([*bases, *subs], transcript.text, material_has):
        wrong = pair.wrong
        if not glossary.MIN_DIFF_LEN <= len(wrong) <= glossary.MAX_DIFF_LEN:
            continue
        if wrong in term_names or norm_key(wrong) in decided or light_key(wrong) in answered:
            continue
        item = found.get(pair.base)
        if item is None and pair.base in sub_set:
            if _changed_char(pair.base, wrong) in glossary._EXPAND_STOP_CHARS:
                continue
            parent = found[parents[pair.base]]
            places = list(_occurrences(transcript.text, pair.base))
            item = found.setdefault(
                pair.base,
                Found(
                    term=pair.base,
                    key=light_key(pair.base),
                    df=parent.df,
                    total=parent.total,
                    names=name_count(pair.base),
                    spoken=len(places),
                    meetings=transcript.meeting_count(places),
                    heard=_heard(transcript, places),
                ),
            )
        elif item is None:
            key = light_key(pair.base)
            if key in answered:
                continue
            item = found.setdefault(pair.base, Found(term=pair.base, key=key, existing=True))
        item.pairs.append(pair)
        item.pair_heard[wrong] = _heard(transcript, pair.positions)
        item.pair_meetings[wrong] = transcript.meeting_count(pair.positions)
    for item in found.values():
        item.pairs = item.pairs[:EVIDENCE_KEEP]
    del transcript
    # 10. 排序
    ordered = sorted(
        found.values(),
        key=lambda item: rank_key(
            bool(item.pairs), sum(pair.heard for pair in item.pairs), item.spoken, item.df, item.names, item.term
        ),
    )
    # 前 30 个的证据：含这个词的片段位置（FTS 按 rowid 走，碰到 3 个就停）、文件名里有它的内容
    for item in ordered[:PENDING_CAP]:
        if item.existing:
            continue
        q["evidence"] += 1
        item.text = [
            {"k": row["content_key"], "o": int(row["ordinal"])}
            for row in conn.execute(
                f"""SELECT c.content_key, c.ordinal FROM material_chunks_fts
                      JOIN material_chunks c ON c.id = material_chunks_fts.rowid
                     WHERE material_chunks_fts MATCH ?
                       AND c.content_key IN (SELECT content_key FROM temp._gm_keys)
                     ORDER BY material_chunks_fts.rowid LIMIT {EVIDENCE_KEEP}""",
                (_fts_phrase(item.term),),
            ).fetchall()
        ]
        item.name_keys = sorted(
            {row["content_key"] for row in files if row["content_key"] and item.term in (row["stem"] or row["name"] or "")}
        )[:EVIDENCE_KEEP]
    stats.items = ordered
    return stats


def _changed_char(base: str, wrong: str) -> str:
    """听错的写法里换掉的那个字。"""
    return next((char for char, other in zip(wrong, base, strict=False) if char != other), "")


def _heard(transcript: Transcript, positions: Iterable[int]) -> list[dict[str, Any]]:
    heard: list[dict[str, Any]] = []
    for position in positions:
        meeting_id, start_ms = transcript.where(position)
        entry = {"m": meeting_id, "t": start_ms}
        if entry not in heard:
            heard.append(entry)
        if len(heard) >= EVIDENCE_KEEP:
            break
    return heard


def _rows_of(stats: PassStats) -> dict[tuple[str, str], dict[str, Any]]:
    """算出来的项 → 要写的行（按 (term_key, wrong)）。前 30 项 pending，其余 dropped。"""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for rank, item in enumerate(stats.items):
        status = "pending" if rank < PENDING_CAP else "dropped"
        if not item.existing:
            rows[(item.key, "")] = {
                "term": item.term,
                "files": item.df,
                "spoken": item.spoken,
                "status": status,
                "evidence_json": json.dumps(
                    {
                        "text": item.text,
                        "names": item.name_keys,
                        "heard": item.heard,
                        "n": {"names": item.names, "meetings": item.meetings},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        for pair in item.pairs:
            rows[(item.key, pair.wrong)] = {
                "term": item.term,
                "files": 0,
                "spoken": pair.heard,
                "status": status,
                "evidence_json": json.dumps(
                    {
                        "text": [],
                        "names": [],
                        "heard": item.pair_heard.get(pair.wrong, []),
                        "n": {"names": 0, "meetings": item.pair_meetings.get(pair.wrong, 0)},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
    return rows


def forget_deleted_terms(conn: Any, now: datetime | None = None) -> int:
    """记入的词后来被你在词典里删了：改成 rejected，不再提。只在真有这种行时才写。"""
    stale = conn.execute(
        """SELECT id FROM glossary_candidates
            WHERE status = 'accepted' AND term_id IS NOT NULL
              AND term_id NOT IN (SELECT id FROM glossary_terms)"""
    ).fetchall()
    if not stale:
        return 0
    moment = _stamp(now or datetime.now(UTC))
    with conn:
        return conn.execute(
            f"""UPDATE glossary_candidates SET status = 'rejected', undo_json = NULL, updated_at = ?
                 WHERE id IN ({', '.join(str(int(row['id'])) for row in stale)}) AND status = 'accepted'""",
            (moment,),
        ).rowcount


_UPSERT = """INSERT INTO glossary_candidates(project_id, term, term_key, wrong, files, spoken, evidence_json,
                 status, created_at, updated_at)
             VALUES (:pid, :term, :key, :wrong, :files, :spoken, :evidence_json, :status, :now, :now)
             ON CONFLICT(project_id, term_key, wrong) DO UPDATE SET
                 term = excluded.term, files = excluded.files, spoken = excluded.spoken,
                 evidence_json = excluded.evidence_json, status = excluded.status,
                 updated_at = excluded.updated_at
              WHERE glossary_candidates.status IN ('pending', 'dropped')
                AND (glossary_candidates.term IS NOT excluded.term
                     OR glossary_candidates.files IS NOT excluded.files
                     OR glossary_candidates.spoken IS NOT excluded.spoken
                     OR glossary_candidates.evidence_json IS NOT excluded.evidence_json
                     OR glossary_candidates.status IS NOT excluded.status)"""


def project_pass(
    conn: Any,
    project_id: str,
    now: datetime,
    *,
    busy: Callable[[], bool] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PassStats:
    """H4b：一个项目一遍。先在写事务外算完；再 BEGIN IMMEDIATE，重算签名，和算之前不同就丢掉结果
    留给下一轮，相同就只写变了的行（没有变化时一条 UPDATE 都不发）。算的中途忙了或过了 deadline，
    这一遍放弃，不写 scan 行，下一轮再做（stats.abandoned 是 busy 或 budget）。"""
    forget_deleted_terms(conn, now)
    before = signatures(conn, only=project_id).get(project_id)
    if before is None:
        return PassStats(project_id=project_id, stale=True)
    try:
        stats = compute(conn, project_id, busy=busy, deadline=deadline, clock=clock)
    except PassAbandoned as abandoned:
        return PassStats(project_id=project_id, sig=before, abandoned=abandoned.reason)
    stats.sig = before
    rows = _rows_of(stats)
    moment = _stamp(now)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if signatures(conn, only=project_id).get(project_id) != before:
            conn.rollback()
            stats.stale = True
            return stats
        current = {
            (row["term_key"], row["wrong"]): dict(row)
            for row in conn.execute(
                """SELECT term_key, wrong, term, files, spoken, evidence_json, status
                     FROM glossary_candidates WHERE project_id = ?""",
                (project_id,),
            ).fetchall()
        }
        for (key, wrong), row in rows.items():
            old = current.get((key, wrong))
            if old is not None and (
                old["status"] not in ("pending", "dropped")
                or all(old[name] == row[name] for name in ("term", "files", "spoken", "evidence_json", "status"))
            ):
                continue
            conn.execute(_UPSERT, {"pid": project_id, "key": key, "wrong": wrong, "now": moment, **row})
            stats.written += 1
        gone = [
            (key, wrong)
            for (key, wrong), old in current.items()
            if old["status"] == "pending" and (key, wrong) not in rows
        ]
        for key, wrong in gone:
            conn.execute(
                """UPDATE glossary_candidates SET status = 'dropped', updated_at = ?
                    WHERE project_id = ? AND term_key = ? AND wrong = ? AND status = 'pending'""",
                (moment, project_id, key, wrong),
            )
            stats.written += 1
        scan = conn.execute(
            "SELECT sig FROM glossary_mining_scan WHERE project_id = ?", (project_id,)
        ).fetchone()
        if scan is None or scan["sig"] != before or stats.written:
            conn.execute(
                """INSERT INTO glossary_mining_scan(project_id, sig, mined_at) VALUES (?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET sig = excluded.sig, mined_at = excluded.mined_at""",
                (project_id, before, moment),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    stats.pending = sum(1 for row in rows.values() if row["status"] == "pending")
    stats.dropped = sum(1 for row in rows.values() if row["status"] == "dropped")
    return stats


def mine_round(
    db: Database,
    busy: Callable[[], bool],
    *,
    clock: Callable[[], float] = time.monotonic,
    budget_s: float = ROUND_SECONDS,
    now: datetime | None = None,
    fts_rebuilding: bool | None = None,
) -> dict[str, Any]:
    """H4 的入口（links_loop 调）：先挖种子（最多 4 秒），剩下的给项目汇总；有到期的项目时每轮至少做一个，
    最多 3 个。返回 {seeded, projects, stopped}，stopped 是 None、busy 或 budget。
    database is locked 往外抛，由循环结束这一轮。"""
    moment = now or datetime.now(UTC)
    start = clock()
    deadline = start + budget_s
    result: dict[str, Any] = {"seeded": 0, "projects": 0, "stopped": None}
    if busy():
        result["stopped"] = "busy"
        return result
    with db.autocommit() as conn:
        if fts_rebuilding is None:
            fts_rebuilding = (
                conn.execute("SELECT 1 FROM app_state WHERE key = ?", (REBUILD_KEY,)).fetchone() is not None
            )
        result["seeded"] = seed_round(
            conn, min(deadline, start + SEED_SHARE_S), busy, clock=clock, now=moment
        )
        if busy():
            result["stopped"] = "busy"
            return result
        if fts_rebuilding:
            # 材料全文表还没补完：第 3、4 步和听错写法都要查它，H4b 整段跳过
            return result
        due = due_projects(conn, moment)
    for index, (project_id, _sig) in enumerate(due[:PROJECTS_PER_ROUND]):
        if busy():
            result["stopped"] = "busy"
            break
        if index and clock() >= deadline:
            result["stopped"] = "budget"
            break
        # 有到期的项目时每轮至少做一个：第一个项目从它开始算至少有一整轮的时间（5 秒），之后的按这一轮的截止
        pass_deadline = max(deadline, clock() + budget_s) if index == 0 else deadline
        with db.autocommit() as conn:
            stats = project_pass(conn, project_id, moment, busy=busy, deadline=pass_deadline, clock=clock)
        if stats.abandoned:
            result["stopped"] = stats.abandoned
            break
        result["projects"] += 1
    if result["stopped"] is None and (
        len(due) > PROJECTS_PER_ROUND or clock() >= deadline
    ):
        result["stopped"] = "budget"
    return result


# ---------------------------------------------------------------------- 读


def _load_rows(conn: Any, project_id: str) -> list[dict[str, Any]]:
    """这个项目待认的行，顺带同样写法的词条（existing_term）。一条语句。"""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT g.term, g.term_key, g.wrong, g.files, g.spoken, g.evidence_json,
                      t.id AS existing_id, t.term AS existing_term
                 FROM glossary_candidates g LEFT JOIN glossary_terms t ON t.term = g.term
                WHERE g.project_id = ? AND g.status = 'pending' ORDER BY g.term_key, g.wrong""",
            (project_id,),
        ).fetchall()
    ]


def _fold(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """行按 term_key 折成项并排序（和汇总时同一个排法）。"""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        evidence = json.loads(row["evidence_json"] or "{}")
        group = groups.setdefault(
            row["term_key"],
            {
                "key": row["term_key"],
                "term": row["term"],
                "base": None,
                "wrongs": [],
                "existing_term": (
                    {"id": row["existing_id"], "term": row["existing_term"]} if row["existing_id"] else None
                ),
            },
        )
        if row["wrong"]:
            group["wrongs"].append({**row, "evidence": evidence})
        else:
            group["base"] = {**row, "evidence": evidence}
            group["term"] = row["term"]
    items = list(groups.values())
    for item in items:
        item["wrongs"].sort(key=lambda row: (-int(row["spoken"]), row["wrong"]))
        item["wrongs"] = item["wrongs"][:EVIDENCE_KEEP]
    items.sort(
        key=lambda item: rank_key(
            bool(item["wrongs"]),
            sum(int(row["spoken"]) for row in item["wrongs"]),
            int((item["base"] or {}).get("spoken") or 0),
            int((item["base"] or {}).get("files") or 0),
            int(((item["base"] or {}).get("evidence") or {}).get("n", {}).get("names") or 0),
            item["term"],
        )
    )
    return items


def _snippet(text: str, needle: str, side: int) -> str:
    index = text.find(needle)
    if index < 0:
        return ""
    return " ".join(text[max(0, index - side) : index + len(needle) + side].split())


def serialize(conn: Any, project_id: str, items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """折好的项 → 接口的样子。现取原话、会名和文件名（2 条语句）；键的集合固定，没有分数。"""
    from .decisions import _audio_url, _day
    from .relation_read import AUDIO_ID_SQL

    heard_refs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for item in items:
        if item["wrongs"]:
            refs = [(row["wrong"], ref) for row in item["wrongs"] for ref in row["evidence"].get("heard", [])]
        else:
            refs = [(item["term"], ref) for ref in ((item["base"] or {}).get("evidence") or {}).get("heard", [])]
        heard_refs.append((item, [{"needle": needle, **ref} for needle, ref in refs]))
    meeting_ids = sorted({ref["m"] for _item, refs in heard_refs for ref in refs})
    needles = sorted({ref["needle"] for _item, refs in heard_refs for ref in refs})
    segments: dict[str, list[dict[str, Any]]] = {}
    meetings: dict[str, dict[str, Any]] = {}
    if meeting_ids and needles:
        marks = ", ".join("?" for _ in meeting_ids)
        wanted = " OR ".join("instr(s.text, ?) > 0" for _ in needles)
        for row in conn.execute(
            f"""SELECT m.id, m.title, m.recording_date, m.created_at, {AUDIO_ID_SQL.format(meeting='m.id')} AS audio,
                       s.start_ms, s.text
                  FROM meetings m JOIN segments s ON s.version_id = m.current_transcript_version_id
                 WHERE m.id IN ({marks}) AND ({wanted}) ORDER BY m.id, s.start_ms""",
            [*meeting_ids, *needles],
        ).fetchall():
            meetings[row["id"]] = {
                "id": row["id"],
                "title": row["title"],
                "date": _day(row["recording_date"], row["created_at"]),
                "audio_url": _audio_url(row["audio"]),
            }
            segments.setdefault(row["id"], []).append({"start_ms": int(row["start_ms"]), "text": row["text"] or ""})
    # 文件名和材料原话
    content_keys: set[str] = set()
    chunk_refs: dict[tuple[str, int], str] = {}
    for item in items:
        evidence = (item["base"] or {}).get("evidence") or {}
        for ref in evidence.get("text", []):
            content_keys.add(ref["k"])
            if not evidence.get("heard"):
                chunk_refs.setdefault((ref["k"], int(ref["o"])), item["term"])
        content_keys.update(evidence.get("names", []))
    names: dict[str, dict[str, Any]] = {}
    chunks: dict[tuple[str, int], str] = {}
    if content_keys:
        key_marks = ", ".join("?" for _ in content_keys)
        head = ""
        chunk_sql = ""
        chunk_params: list[Any] = []
        if chunk_refs:
            # 按 (content_key, ordinal) 取；重读后 ordinal 对不上（或那一段里已经没有这个词）时，在这份内容里
            # 用全文索引找一段含这个词的。都在同一条语句里（COALESCE 取到第一个就不再算后面的）
            head = "WITH refs(k, o, t, p) AS (VALUES " + ", ".join("(?, ?, ?, ?)" for _ in chunk_refs) + ") "
            for (key, ordinal), term in sorted(chunk_refs.items()):
                chunk_params += [key, ordinal, term, _fts_phrase(term)]
            chunk_sql = """ UNION ALL SELECT 'q', refs.k, refs.o, NULL, COALESCE(
                    (SELECT c.text FROM material_chunks c
                      WHERE c.content_key = refs.k AND c.ordinal = refs.o AND instr(c.text, refs.t) > 0),
                    (SELECT c.text FROM material_chunks_fts JOIN material_chunks c ON c.id = material_chunks_fts.rowid
                      WHERE material_chunks_fts MATCH refs.p AND c.content_key = refs.k LIMIT 1))
                  FROM refs"""
        for row in conn.execute(
            f"""{head}SELECT 'n' AS kind, f.content_key, f.id, f.name, NULL AS text FROM material_files f
                  JOIN project_material_roots r ON r.id = f.root_id
                 WHERE r.project_id = ? AND f.gone_at IS NULL AND f.zone = 'normal'
                   AND f.content_key IN ({key_marks}){chunk_sql}""",
            [*chunk_params, project_id, *sorted(content_keys)],
        ).fetchall():
            if row["kind"] == "n":
                current = names.get(row["content_key"])
                if current is None or int(row["id"]) < current["file_id"]:
                    names[row["content_key"]] = {"file_id": int(row["id"]), "name": row["name"]}
            else:
                chunks[(row["content_key"], int(row["id"]))] = row["text"] or ""
    result: list[dict[str, Any]] = []
    for item, refs in heard_refs:
        base = item["base"] or {}
        evidence = base.get("evidence") or {}
        heard: list[dict[str, Any]] = []
        for ref in refs:
            if len(heard) >= 2:
                break
            candidates = [seg for seg in segments.get(ref["m"], []) if ref["needle"] in seg["text"]]
            if not candidates or ref["m"] not in meetings:
                continue
            before = [seg for seg in candidates if seg["start_ms"] <= int(ref["t"])]
            segment = before[-1] if before else candidates[0]
            meeting = meetings[ref["m"]]
            heard.append(
                {
                    "meeting": {"id": meeting["id"], "title": meeting["title"], "date": meeting["date"]},
                    "start_ms": segment["start_ms"],
                    "quote": _snippet(segment["text"], ref["needle"], HEARD_SIDE),
                    "audio_url": meeting["audio_url"],
                }
            )
        file_names: list[dict[str, Any]] = []
        for key in [ref["k"] for ref in evidence.get("text", [])] + list(evidence.get("names", [])):
            entry = names.get(key)
            if entry is not None and entry not in file_names:
                file_names.append(entry)
            if len(file_names) >= 2:
                break
        file_quote = None
        if not heard:
            for ref in evidence.get("text", []):
                text = chunks.get((ref["k"], int(ref["o"])), "")
                quote = _snippet(text, item["term"], QUOTE_SIDE)
                entry = names.get(ref["k"])
                if quote and entry is not None:
                    file_quote = {"file_id": entry["file_id"], "quote": quote}
                    break
        result.append(
            {
                "key": item["key"],
                "term": item["term"],
                "existing_term": item.get("existing_term"),
                "wrongs": [
                    {"text": row["wrong"], "meetings": int((row["evidence"].get("n") or {}).get("meetings") or 0)}
                    for row in item["wrongs"]
                ],
                "files": int(base.get("files") or 0),
                "spoken": int(base.get("spoken") or 0),
                "heard": heard,
                "file_names": file_names,
                "file_quote": file_quote,
            }
        )
    return result


def list_candidates(conn: Any, project_id: str, *, limit: int | None = None) -> dict[str, Any]:
    """{items, total}：这个项目待认的词，最多 30 项（limit 更小时取前几项）。"""
    items = _fold(_load_rows(conn, project_id))
    total = min(len(items), PENDING_CAP)
    items = items[: min(limit or PENDING_CAP, PENDING_CAP)]
    return {"items": serialize(conn, project_id, items), "total": total}


def meeting_pairs(conn: Any, meeting_id: str, *, limit: int = 2) -> list[dict[str, Any]]:
    """会议页词典小节：这场会所属项目里待认的听错写法，并且写法出现在这场会当前逐字稿里（最多 2 个）。"""
    meeting = conn.execute(
        """SELECT m.project_id, m.current_transcript_version_id AS tv, p.name
             FROM meetings m LEFT JOIN projects p ON p.id = m.project_id WHERE m.id = ?""",
        (meeting_id,),
    ).fetchone()
    if meeting is None or not meeting["project_id"] or not meeting["tv"]:
        return []
    rows = conn.execute(
        """SELECT g.term, g.term_key, g.wrong, g.spoken,
                  (SELECT s.start_ms || char(9) || s.text FROM segments s
                    WHERE s.version_id = ? AND instr(s.text, g.wrong) > 0 ORDER BY s.ordinal LIMIT 1) AS hit
             FROM glossary_candidates g
            WHERE g.project_id = ? AND g.status = 'pending' AND g.wrong != ''
            ORDER BY g.spoken DESC, g.wrong""",
        (meeting["tv"], meeting["project_id"]),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        if not row["hit"]:
            continue
        start, _tab, text = str(row["hit"]).partition("\t")
        result.append(
            {
                "key": row["term_key"],
                "term": row["term"],
                "wrong": row["wrong"],
                "start_ms": int(start),
                "quote": _snippet(text, row["wrong"], HEARD_SIDE),
                "project": {"id": meeting["project_id"], "name": meeting["name"]},
            }
        )
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------- 写：记入、不是、撤销


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _undo_until(decided: datetime) -> str:
    return _stamp(decided + timedelta(seconds=UNDO_WINDOW_S))


def _item_rows(connection: Any, project_id: str, key: str) -> list[dict[str, Any]]:
    if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise CandidateError(404, PROJECT_MISSING)
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM glossary_candidates WHERE project_id = ? AND term_key = ? ORDER BY wrong",
            (project_id, key),
        ).fetchall()
    ]
    if not rows or all(row["status"] == "dropped" for row in rows):
        raise CandidateError(404, TERM_GONE)
    return rows


def _wrong_taken(connection: Any, wrong: str, own_term_id: str | None) -> bool:
    """这个写法已经是别的词条的写法、错写或也叫，或是某个项目的名字、也叫。"""
    from .project_names import also_entries

    for row in connection.execute("SELECT id, term, aliases, also FROM glossary_terms").fetchall():
        if row["id"] == own_term_id:
            if row["term"] == wrong or wrong in json.loads(row["also"] or "[]"):
                return True
            continue
        if wrong == row["term"] or wrong in json.loads(row["aliases"] or "[]") or wrong in json.loads(row["also"] or "[]"):
            return True
    key = norm_key(wrong)
    for row in connection.execute("SELECT name, also_names FROM projects").fetchall():
        if norm_key(row["name"]) == key or any(norm_key(entry["name"]) == key for entry in also_entries(row["also_names"])):
            return True
    return False


def accept(
    db: Database,
    project_id: str,
    key: str,
    not_wrong: Sequence[str] = (),
    *,
    only_wrong: str | None = None,
    snapshot_path: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """［记入］：一个事务里做完，再调一次 rewrite_snapshot。

    只动这一项里 pending 的行：界面上看得到的只有 pending 的写法，dropped 的听错写法你没见过，
    不能跟着记成错写，行也原样不动（撤销时也就不会被带回 pending）。only_wrong 给了（会议页，
    一行只显示一个写法）时只记这个写法和词本身，别的写法留着等你在词典页回答。"""
    moment = _now(now)
    stamp = _stamp(moment)
    refused = set(not_wrong)
    with db.transaction() as connection:
        rows = _item_rows(connection, project_id, key)
        pending = [row for row in rows if row["status"] == "pending"]
        if only_wrong is not None:
            pending = [row for row in pending if row["wrong"] in ("", only_wrong)]
        if not pending:
            raise CandidateError(409, ALREADY_DONE)
        base = next((row for row in pending if row["wrong"] == ""), None)
        named = base or next((row for row in rows if row["wrong"] == ""), None) or pending[0]
        term_text = named["term"]
        try:
            term_text = glossary.validate_term_text(term_text, what="术语")
        except glossary.GlossaryError:
            raise CandidateError(422, TERM_INVALID) from None
        project = connection.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
        wrongs = [row["wrong"] for row in pending if row["wrong"] and row["wrong"] not in refused]
        existing = connection.execute("SELECT * FROM glossary_terms WHERE term = ?", (term_text,)).fetchone()
        created = False
        already = False
        added: list[str] = []
        skipped: list[str] = []
        if existing is not None and base is not None:
            already = True
            term_id = existing["id"]
        else:
            if existing is not None and not wrongs:
                # 记到已有词条，写法却全被去掉了：什么都加不上，不记、不给［撤销］
                raise CandidateError(422, KEEP_ONE_WRONG)
            own = existing["id"] if existing is not None else None
            usable: list[str] = []
            for wrong in wrongs:
                try:
                    glossary.normalize_aliases([wrong])
                except glossary.GlossaryError:
                    skipped.append(wrong)
                    continue
                if wrong == term_text or _wrong_taken(connection, wrong, own):
                    skipped.append(wrong)
                else:
                    usable.append(wrong)
            if existing is not None:
                if not usable:
                    # 写法全都已经用在别的词条上：如实说，行留着（可以点［不是］）
                    raise CandidateError(409, NOTHING_ADDED_TEXT.format(skipped="』『".join(skipped)))
                term_id = existing["id"]
                added = glossary._append_aliases(connection, term_id, usable, now=stamp)
            else:
                term_id = glossary._insert_term(
                    connection,
                    term=term_text,
                    aliases=usable,
                    scope=project["name"],
                    category="其他",
                    source="material",
                    confirmed=True,
                    project_id=project_id,
                    is_cue=False,
                    also=[],
                    now=stamp,
                )
                created = True
                added = usable
        term_row = connection.execute("SELECT * FROM glossary_terms WHERE id = ?", (term_id,)).fetchone()
        undo_json = json.dumps(
            {"created": created, "aliases": added, "term_updated_at": term_row["updated_at"]}, ensure_ascii=False
        )
        for row in pending:
            status = "rejected" if row["wrong"] in refused and row["wrong"] else "accepted"
            connection.execute(
                """UPDATE glossary_candidates SET status = ?, term_id = ?, undo_json = ?, decided_at = ?,
                          updated_at = ? WHERE id = ? AND status = 'pending'""",
                (status, term_id, undo_json, stamp, stamp, row["id"]),
            )
        term = {
            "id": term_row["id"],
            "term": term_row["term"],
            "aliases": json.loads(term_row["aliases"] or "[]"),
            "is_cue": bool(term_row["is_cue"]),
        }
    if snapshot_path is not None and not already:
        glossary.rewrite_snapshot(db, snapshot_path)
    if already:
        text = ALREADY_TEXT.format(term=term_text)
    elif not created:
        text = APPENDED_TEXT.format(wrongs="』『".join(added), term=term_text)
        if skipped:
            text += SKIPPED_TAIL.format(skipped="』『".join(skipped))
    elif skipped:
        text = SKIPPED_TEXT.format(term=term_text, skipped="』『".join(skipped))
    elif added:
        text = ACCEPTED_WRONGS_TEXT.format(term=term_text, wrongs="、".join(added))
    else:
        text = ACCEPTED_TEXT.format(term=term_text)
    return {
        "term": term,
        "created": created,
        "added_aliases": added,
        "skipped_aliases": skipped,
        "already": already,
        "text": text,
        "undo_until": _undo_until(moment),
    }


def reject(db: Database, project_id: str, key: str, *, now: datetime | None = None) -> dict[str, Any]:
    """［不是］：这个项目里这个 key 待认的行记 rejected，以后在这个项目里不再提（key 进了「答过的」，
    汇总不再提它）。dropped 的和更早回答过的行不动，撤销时不会被带回 pending，记入过的也不会被冲掉。"""
    moment = _now(now)
    stamp = _stamp(moment)
    with db.transaction() as connection:
        rows = _item_rows(connection, project_id, key)
        if not any(row["status"] == "pending" for row in rows):
            raise CandidateError(409, ALREADY_DONE)
        term = next((row["term"] for row in rows if row["wrong"] == ""), rows[0]["term"])
        connection.execute(
            """UPDATE glossary_candidates SET status = 'rejected', term_id = NULL,
                      undo_json = '{"rejected": true}', decided_at = ?, updated_at = ?
                WHERE project_id = ? AND term_key = ? AND status = 'pending'""",
            (stamp, stamp, project_id, key),
        )
    return {"text": REJECTED_TEXT.format(term=term), "undo_until": _undo_until(moment)}


def undo(
    db: Database,
    project_id: str,
    key: str,
    *,
    snapshot_path: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """600 秒内撤销记入或不是：行回到 pending；记入时新建的词条没改过就删掉，这次加的错写拿掉。"""
    moment = _now(now)
    stamp = _stamp(moment)
    touched_terms = False
    with db.transaction() as connection:
        rows = _item_rows(connection, project_id, key)
        if all(row["status"] in ("pending", "dropped") for row in rows):
            raise CandidateError(409, UNDO_TWICE)
        decided = [row for row in rows if row["decided_at"] and row["undo_json"]]
        if not decided:
            raise CandidateError(409, UNDO_EXPIRED)
        latest = max(row["decided_at"] for row in decided)
        at = datetime.fromisoformat(latest)
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if moment - at > timedelta(seconds=UNDO_WINDOW_S):
            raise CandidateError(409, UNDO_EXPIRED)
        term = next((row["term"] for row in rows if row["wrong"] == ""), rows[0]["term"])
        # 只撤最后一次回答动过的行（同一个 decided_at）；dropped 的和更早回答过的行不动
        accepted = next(
            (row for row in rows if row["status"] == "accepted" and row["decided_at"] == latest), None
        )
        if accepted is not None and accepted["term_id"]:
            info = json.loads(accepted["undo_json"] or "{}")
            term_row = connection.execute(
                "SELECT * FROM glossary_terms WHERE id = ?", (accepted["term_id"],)
            ).fetchone()
            if term_row is not None and (info.get("created") or info.get("aliases")):
                if term_row["updated_at"] != info.get("term_updated_at"):
                    raise CandidateError(409, TERM_CHANGED)
                if info.get("created"):
                    connection.execute("DELETE FROM glossary_terms WHERE id = ?", (term_row["id"],))
                else:
                    added = set(info.get("aliases") or [])
                    aliases = [alias for alias in json.loads(term_row["aliases"] or "[]") if alias not in added]
                    connection.execute(
                        "UPDATE glossary_terms SET aliases = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(aliases, ensure_ascii=False), stamp, term_row["id"]),
                    )
                touched_terms = True
        connection.execute(
            """UPDATE glossary_candidates SET status = 'pending', term_id = NULL, undo_json = NULL,
                      decided_at = NULL, updated_at = ?
                WHERE project_id = ? AND term_key = ? AND status IN ('accepted', 'rejected')
                  AND decided_at = ?""",
            (stamp, project_id, key, latest),
        )
    if touched_terms and snapshot_path is not None:
        glossary.rewrite_snapshot(db, snapshot_path)
    return {"status": "pending", "text": UNDONE_TEXT.format(term=term)}


# ---------------------------------------------------------------------- 命令行


def dry_run(conn: Any, project_id: str) -> PassStats:
    """links words --dry-run：当场给这个项目的内容挖种子，算一遍，什么都不写。"""
    seeds = {
        row["content_key"]: extract_seeds(read_text(conn, row["content_key"]))
        for row in conn.execute(_project_keys_sql(), {"pid": project_id}).fetchall()
    }
    return compute(conn, project_id, seeds=seeds)


def describe(item: Found) -> str:
    """命令行一行：「司美格鲁肽（会上听成司美格鲁太）· 听到 133 次」「驻场服务 · 15 个文件 · 会上 238 次」。"""
    if item.pairs:
        wrongs = "、".join(pair.wrong for pair in item.pairs)
        return f"{item.term}（会上听成{wrongs}）· 听到 {sum(pair.heard for pair in item.pairs)} 次"
    parts = [item.term]
    if item.df:
        parts.append(f"{item.df} 个文件")
    if item.names:
        parts.append(f"文件名里 {item.names} 次")
    if item.spoken:
        parts.append(f"会上 {item.spoken} 次")
    return " · ".join(parts)

