"""会上提到文件名（第二期 2d）：把逐字稿和本项目文件夹里的文件名比对，连成「会上提到」。

- 要比对的会：已归项目，并且改过稿（dirty 不为 0）、还没比对过、或 stems_sig 变了（项目各根目录
  的 stems_rev、项目名和也叫、根目录位置、词条的最后修改时间合起来的摘要）。每轮最多 20 场或 5 秒。
- 只和这场会所在项目的根目录里、还在的、zone 是 normal 或 package 的文件比。
- 逐字稿每段按 stem_key 同一套规则折叠，一遍扫完：最长优先、不重叠；落在更长的项目名、也叫、词条里的
  出现不算（它们作为「挡板」一起扫）；字母词前后不能是字母。次数只数逐字稿；只在纪要里写到的，词干要
  3 字以上才连，source=minutes。
- 一个词干对应多份文件时一场会只连一份：说出了带版本的全名就连那份；否则连修改时间不晚于这场会的
  最新一份；都晚于这场会就连最早的一份。你手动换过的（picked）不再自动改；文件不在了先按内容标识
  在同名文件里找回（3a），同名文件的标识还没算完就原样保留，找不回才换。
- rejected（不是这份文件）永远不会被改回 active，只挡同一场会、同一个项目。
- 通用词干（被本项目一半以上、至少 4 场会提到）照样存，读的时候标 generic。
- 4b（MATCH_VERSION 4b-1）：已确认词条（本项目的或通用的）的本名等于某个能用的词干时，它的 aliases、
  also 也当针，指向这个词干（能不能用按这个叫法本身算，两个字的要说两次）；这一行全是别名命中时
  needle 写说得最多的那个叫法。词干命中前后 8 个字以内的口头版本号（「第三版」「V3版」「3.0版」，
  中文数字到二十）和全名针并列计入 versions（组里有这一版才算）；「终版」「定稿」、final 在组里恰好
  一份文件名带这类字样时选那一份。都没有时先看 L5 写的 hints_json（pick_by_hint），再用 _pick_by_date。
- 4b-2：文件名里的版本号认 vN、v1.2 和「第 N 版 / 第 N 稿」（和 file_stems 结尾去掉的两种写法同一份，
  并进同一个词干的几份文件靠它分出来），口头的「第 N 稿」同「第 N 版」，「终稿」「最终稿」和「终版」一样
  当终版；v1.2 以前被当成 12，现在是 (1, 2)，v3.0 才算「第三版」。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from datetime import date as date_type, datetime, time as day_time, timedelta
from typing import Any, Callable

from . import relation_read
from .db import Database, utc_now
from .file_stems import ORDINAL, STEM_NO, STEM_TWICE, STEM_YES, V_TAG, stem_usability
from .material_content import key_file_now
from .material_index import MATCH_ZONES, STATE_MISSING, STATE_OFFLINE
from .project_names import also_entries
from .project_profile import MAX_ANCHORS, light_key, norm_key
from .task_due import BEIJING_TZ
from .text_scan import FormScanner

MATCH_VERSION = "4b-2"
PER_MEETING = 20
GENERIC_MIN_MEETINGS = 4
MINUTES_MIN_CHARS = 3
ROUND_MEETINGS = 20
ROUND_SECONDS = 5.0
# 文件名里的版本号：vN、v1.2（后面不能再跟数字，v30 不是 v3）和「第 N 版 / 第 N 稿」，写法和 file_stems 结尾
# 去掉的同一份（V_TAG、ORDINAL）。
_VERSION_TAG = re.compile(rf"{V_TAG}(?![\d.]*\d)|{ORDINAL}", re.IGNORECASE)
_HHMMSS = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")
_ZONES_SQL = ", ".join(f"'{zone}'" for zone in MATCH_ZONES)
# 4b：词干命中前后看几个字找口头版本号和「终版」
NEAR_CHARS = 8
_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_NUM = r"[一二两三四五六七八九十]{1,3}|\d{1,2}"
# 「第三版」「第三稿」「第3版」「三版」「V3版」「版本三」「3.0版」。光杆「N版」最容易误判：「上一版」「这一版」
# 「下一版」「新一版」「这两版」「改了三版」说的不是第几版，所以前面是这些字（或数字本身的一部分）时不认；
# 光杆的「一版」「两版」多半是在数版数（「出一版」「做了两版」），也不认（「第一版」「版本一」照认）。
# 「第一稿件」是另一个词，不是第一稿。
_BARE_NOT_AFTER = "上下这那前后新旧同每哪几各某本此该头首末好多了过出一二两三四五六七八九十"
_ORAL_VERSION = re.compile(
    rf"第\s*(?P<a>{_NUM})\s*(?:版|稿(?!件))"
    rf"|(?P<e>\d{{1,2}})\.0\s*版"
    rf"|[vV]\s*(?P<c>\d{{1,2}})\s*版"
    rf"|版本\s*(?P<d>{_NUM})"
    rf"|(?<![{_BARE_NOT_AFTER}\d.vV第])(?![一两]\s*版)(?P<b>{_NUM})\s*版(?!本)"
)
# 终版这一类：词干结尾去掉的修饰里，意思是「最后一版」的几种（审定稿里带着「定稿」，也算）
_FINAL = re.compile(r"最终版|最终稿|终版|终稿|定稿|(?<![a-z])final(?![a-z])", re.IGNORECASE)
# L5 留给 2d 的时间提示（hints_json 里的 rel），和 loose_mentions 的 when.rel 同一组
HINT_RELS = ("last_week", "this_week", "yesterday", "today", "last_meeting", "latest", "previous")


class MentionError(ValueError):
    pass


class MentionNotFound(LookupError):
    pass


# ---------------------------------------------------------------------- 摘要


def project_sigs(connection: Any) -> dict[str, str]:
    """每个项目的 stems_sig（固定 3 条查询）。"""
    roots: dict[str, list[str]] = {}
    for row in connection.execute(
        """SELECT r.project_id, r.id, r.path, COALESCE(s.stems_rev, 0) AS rev
             FROM project_material_roots r LEFT JOIN material_index_state s ON s.root_id = r.id
            ORDER BY r.id"""
    ).fetchall():
        roots.setdefault(row["project_id"], []).append(f"{row['id']}:{row['rev']}:{row['path']}")
    terms: dict[str | None, str] = {}
    for row in connection.execute(
        "SELECT project_id, COUNT(*) AS n, MAX(updated_at) AS at FROM glossary_terms GROUP BY project_id"
    ).fetchall():
        terms[row["project_id"]] = f"{row['n']}:{row['at']}"
    sigs: dict[str, str] = {}
    for row in connection.execute("SELECT id, name, also_names FROM projects").fetchall():
        digest = hashlib.sha1()
        for part in (
            MATCH_VERSION,
            row["name"] or "",
            row["also_names"] or "",
            "|".join(roots.get(row["id"], [])),
            terms.get(row["id"], ""),
            terms.get(None, ""),
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        sigs[row["id"]] = digest.hexdigest()[:20]
    return sigs


# ---------------------------------------------------------------------- 项目上下文


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return (
        [str(item) for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, list)
        else []
    )


def _version_marker(name: str) -> str | None:
    """文件名（去掉扩展名）里最后一个版本号的原样写法；先做 NFKC，全角的「Ｖ２」「第３版」和半角一样。"""
    text = unicodedata.normalize("NFKC", name)
    base = text.rpartition(".")[0] or text
    found = _VERSION_TAG.findall(base)
    return found[-1] if found else None


def version_tag(name: str) -> str | None:
    """文件名里的版本号（v3、V1.2、第三版、第 3 稿），折成和逐字稿同一套写法（light_key），全名比对用。"""
    marker = _version_marker(name)
    return light_key(marker) if marker else None


def cn_number(text: str) -> int | None:
    """「三」「十二」「二十」「3」→ 数字；认不出或超出 1 到 20 时回 None。"""
    text = (text or "").strip()
    if text.isdigit():
        value = int(text)
    elif text == "十":
        value = 10
    elif len(text) == 1 and text in _CN_DIGITS:
        value = _CN_DIGITS[text]
    elif len(text) == 2 and text[0] == "十" and text[1] in _CN_DIGITS:
        value = 10 + _CN_DIGITS[text[1]]
    elif len(text) == 2 and text[1] == "十" and text[0] in _CN_DIGITS:
        value = _CN_DIGITS[text[0]] * 10
    else:
        return None
    return value if 1 <= value <= 20 else None


def spoken_version(text: str) -> int | None:
    """一段话里的口头版本号（「第三版」「第三稿」「V3版」「版本三」「3.0版」），没有回 None。"""
    for match in _ORAL_VERSION.finditer(text or ""):
        raw = next((value for value in match.groupdict().values() if value), "")
        number = cn_number(raw)
        if number is not None:
            return number
    return None


def _near(text: str, start: int, end: int) -> tuple[str, str]:
    return text[max(0, start - NEAR_CHARS) : start], text[end : end + NEAR_CHARS]


def near_version(text: str, start: int, end: int) -> int | None:
    """词干命中前后 8 个字以内的口头版本号（后面的先看）。"""
    before, after = _near(text, start, end)
    found = spoken_version(after)
    return found if found is not None else spoken_version(before)


def near_final(text: str, start: int, end: int) -> bool:
    before, after = _near(text, start, end)
    return bool(_FINAL.search(before) or _FINAL.search(after))


def version_number(name: str) -> tuple[int, ...] | None:
    """文件名里的版本号拆成数字：v3 → (3,)，V1.2 → (1, 2)，第三版 → (3,)，第 12 稿 → (12,)。
    第几版超过 20 的认不出（口头版本号也只到 20），回 None。从原样写法拆，不从 light_key 折过的 tag 拆：
    折的时候把小数点去掉了，V1.2 会变成 12。"""
    marker = _version_marker(name)
    if not marker:
        return None
    if marker[0] in "vV":
        return tuple(int(part) for part in marker[1:].split("."))
    number = cn_number(re.sub(r"[\s第版稿]", "", marker))
    return None if number is None else (number,)


def is_version(name: str, number: int) -> bool:
    """文件名的版本号就是口头说的那一版：v3、v3.0、v3.0.0 都算第三版。"""
    parts = version_number(name)
    return bool(parts) and parts[0] == number and all(part == 0 for part in parts[1:])


def final_files(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in group if _FINAL.search(row["name"] or "")]


def _mtime(row: dict[str, Any]) -> int:
    return int(row["mtime_ns"] or 0)


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(rows, key=lambda row: (_mtime(row), row["id"])) if rows else None


def _local(ns: int) -> datetime:
    """按会上说话人的日历（北京时间）看这个时刻：「昨天」「这周」是会上的日子，不随跑声档的 Mac 的时区变。"""
    return datetime.fromtimestamp(ns / 1_000_000_000, BEIJING_TZ)


def _day_start_ns(day: date_type) -> int:
    return int(datetime.combine(day, day_time(0, 0), BEIJING_TZ).timestamp() * 1_000_000_000)


def pick_by_hint(
    group: list[dict[str, Any]],
    hint: dict[str, Any] | None,
    meeting_ns: int | None,
    previous_ns: int | None = None,
) -> dict[str, Any] | None:
    """按提示在词干组里挑一份（4b，2d 和 L5 同一套）：{"version": 3} 挑那一版；{"rel": …} 以会议当天
    （北京日历，一周从周一开始）为准：上周是上一个周一到周日之间修改时间最新的；这周是本周到开会时为止
    最新的；昨天、今天是那一天里最新的（今天要早于开会）；上次开会是不晚于同项目上一场会的最新一份；
    最新是不晚于开会的最新一份；上一版是不晚于开会的第二新一份。找不到回 None（调用方再用 _pick_by_date）。"""
    if not hint or not group:
        return None
    number = hint.get("version")
    if isinstance(number, int) and not isinstance(number, bool):
        return _latest([row for row in group if is_version(row["name"] or "", number)])
    rel = hint.get("rel")
    if rel not in HINT_RELS:
        return None
    if rel == "last_meeting":
        if previous_ns is None:
            return None
        return _latest([row for row in group if _mtime(row) <= previous_ns])
    if meeting_ns is None:
        return None
    before = [row for row in group if _mtime(row) <= meeting_ns]
    if rel == "latest":
        return _latest(before)
    if rel == "previous":
        ranked = sorted(before, key=lambda row: (_mtime(row), row["id"]), reverse=True)
        return ranked[1] if len(ranked) > 1 else None
    day = _local(meeting_ns).date()
    if rel == "today":
        low, high = _day_start_ns(day), meeting_ns
    elif rel == "yesterday":
        low, high = _day_start_ns(day - timedelta(days=1)), _day_start_ns(day) - 1
    else:
        monday = day - timedelta(days=day.weekday())
        if rel == "this_week":
            low, high = _day_start_ns(monday), meeting_ns
        else:  # last_week
            low, high = _day_start_ns(monday - timedelta(days=7)), _day_start_ns(monday) - 1
    return _latest([row for row in group if low <= _mtime(row) <= high])


def previous_meeting_ns(
    connection: Any, meeting_id: str, project_id: str, meeting_ns: int | None
) -> int | None:
    """同项目上一场会的时间（「上次开会那版」用）。"""
    if meeting_ns is None:
        return None
    best: int | None = None
    for row in connection.execute(
        "SELECT id, recording_date, created_at FROM meetings WHERE project_id = ? AND id != ?",
        (project_id, meeting_id),
    ).fetchall():
        ns = _meeting_ns(row["recording_date"], row["created_at"])
        if ns is not None and ns < meeting_ns and (best is None or ns > best):
            best = ns
    return best


class ProjectContext:
    """一个项目的词干、挡板和扫描器，一轮里同项目的会共用。"""

    def __init__(self, connection: Any, project_id: str):
        project = connection.execute(
            "SELECT id, name, also_names FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        self.project_id = project_id
        names = [project["name"]] if project else []
        names += [
            entry["name"] for entry in also_entries(project["also_names"] if project else None)
        ]
        root_rows = connection.execute(
            "SELECT id, path FROM project_material_roots WHERE project_id = ?", (project_id,)
        ).fetchall()
        root_names = [str(row["path"]).rstrip("/").rpartition("/")[2] for row in root_rows]
        excluded = {norm_key(name) for name in [*names, *root_names] if name}
        excluded.discard("")
        files = [
            dict(row)
            for row in connection.execute(
                f"""SELECT f.id, f.root_id, f.rel_path, f.name, f.stem, f.stem_key, f.mtime_ns, f.size,
                           f.content_key, f.content_size, f.content_mtime_ns
                      FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                     WHERE r.project_id = ? AND f.gone_at IS NULL AND f.stem_key != ''
                       AND f.zone IN ({_ZONES_SQL})""",
                (project_id,),
            ).fetchall()
        ]
        self.groups: dict[str, list[dict[str, Any]]] = {}
        self.usability: dict[str, str] = {}
        for row in files:
            key = row["stem_key"]
            if key not in self.usability:
                usable = stem_usability(key)
                if usable != STEM_NO and norm_key(key) in excluded:
                    usable = STEM_NO
                self.usability[key] = usable
            if self.usability[key] == STEM_NO:
                continue
            self.groups.setdefault(key, []).append(row)
        # 针：词干本身；一个词干有多份文件时，再加「词干 + 版本号」的全名
        self.needles: dict[str, tuple[str, int | None]] = {}
        for key, group in self.groups.items():
            self.needles[key] = (key, None)
            if len(group) > 1:
                for row in group:
                    tag = version_tag(row["name"])
                    if tag:
                        self.needles.setdefault(key + tag, (key, row["id"]))
        # 挡板：项目名、也叫、词条（本项目和通用的）。落在它们里面的出现不算。
        blockers = [*names]
        terms = [
            dict(row)
            for row in connection.execute(
                """SELECT term, aliases, also, confirmed FROM glossary_terms
                    WHERE project_id = ? OR project_id IS NULL ORDER BY term""",
                (project_id,),
            ).fetchall()
        ]
        # 4b：已确认词条的本名等于某个能用的词干时，别名（aliases、also）也当针：{叫法的键: (词干, 原样写法, 能用性)}；
        # term_forms 是全部叫法到词条本名的键（L5 的 T3 用）
        self.aliases: dict[str, tuple[str, str, str]] = {}
        self.term_forms: dict[str, str] = {}
        for row in terms:
            blockers.append(row["term"])
            if not row["confirmed"]:
                continue
            term_key = light_key(row["term"])
            forms_of_term = [row["term"], *_json_list(row["aliases"]), *_json_list(row["also"])]
            for form in forms_of_term:
                form_key = light_key(form)
                if form_key:
                    self.term_forms.setdefault(form_key, term_key)
            if term_key not in self.groups:
                continue
            for form in forms_of_term[1:]:
                form_key = light_key(form)
                if not form_key or form_key in self.needles or form_key in self.aliases:
                    continue
                if norm_key(form_key) in excluded:
                    continue
                usable = stem_usability(form_key)
                if usable == STEM_NO:
                    continue
                self.aliases[form_key] = (term_key, form, usable)
        forms = [*self.needles, *self.aliases]
        forms += [
            light_key(text)
            for text in blockers
            if light_key(text)
            and light_key(text) not in self.needles
            and light_key(text) not in self.aliases
        ]
        self.scanner = FormScanner(forms) if self.needles else None
        self.files_by_id = {row["id"]: row for group in self.groups.values() for row in group}

    def follow_picked(
        self, key: str, content_key: str | None
    ) -> tuple[dict[str, Any] | None, bool]:
        """你手动换过的文件不见了：在同一词干组的活文件里按内容标识找。返回 (找到的文件, 要不要等)。

        组里还有没算出标识（或标识过时）的活文件时要等：这条提到原样保留，不退回自动选。"""
        if not content_key:
            return None, False
        waiting = False
        for row in sorted(self.groups.get(key, []), key=lambda item: item["id"]):
            fresh = (
                row["content_size"] == row["size"] and row["content_mtime_ns"] == row["mtime_ns"]
            )
            if row["content_key"] is None or not fresh:
                waiting = True
                continue
            if row["content_key"] == content_key:
                return row, False
        return None, waiting


# ---------------------------------------------------------------------- 一场会


def _meeting_ns(recording_date: str | None, created_at: str | None) -> int | None:
    """会议时间（纳秒）：只有日期时取那天（北京日历）结束；不带时区的时间按本机时间理解。"""
    for raw in (recording_date, created_at):
        if not raw:
            continue
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            if len(text) == 10:
                moment = datetime.combine(
                    datetime.fromisoformat(text).date(), day_time(23, 59, 59), BEIJING_TZ
                )
            else:
                moment = datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return int(moment.timestamp() * 1_000_000_000)
    return None


def _line_anchor(line: str) -> int | None:
    match = _HHMMSS.search(line)
    if match is None:
        return None
    first, second, third = match.group(1), match.group(2), match.group(3)
    if third is None:
        return (int(first) * 60 + int(second)) * 1000
    return ((int(first) * 60 + int(second)) * 60 + int(third)) * 1000


def _continues_number(text: str, end: int) -> bool:
    following = text[end : end + 2]
    return following[:1].isdigit() or (following[:1] == "." and following[1:2].isdigit())


def _pick_by_date(group: list[dict[str, Any]], meeting_ns: int | None) -> dict[str, Any]:
    def mtime(row: dict[str, Any]) -> int:
        return int(row["mtime_ns"] or 0)

    if meeting_ns is not None:
        before = [row for row in group if mtime(row) <= meeting_ns]
        if before:
            return max(before, key=lambda row: (mtime(row), row["id"]))
        return min(group, key=lambda row: (mtime(row), row["id"]))
    return max(group, key=lambda row: (mtime(row), row["id"]))


def compute_mentions(
    context: ProjectContext,
    *,
    segments: list[dict[str, Any]],
    minutes: str,
    meeting_ns: int | None,
    existing: dict[str, dict[str, Any]],
    hints: dict[str, dict[str, Any]] | None = None,
    previous_ns: int | None = None,
) -> dict[str, dict[str, Any]]:
    """这场会在本项目的提到：{stem_key: 行}。existing 是本项目已有的行（按 stem_key）；hints 是 L5 写的
    {stem_key: {"version": 3} 或 {"rel": "last_week"}}，没有版本依据时先按它挑；previous_ns 是同项目
    上一场会的时间（「上次开会」的提示用）。"""
    if context.scanner is None:
        return {}
    hints = hints or {}
    # 每个词干的每一处命中：(start_ms, 别名的原样写法或 None, 全名针指向的文件, 口头版本号, 带不带终版)
    hits: dict[str, list[tuple[int, str | None, int | None, int | None, bool]]] = {}
    for segment in segments:
        text = str(segment.get("text") or "")
        if not text:
            continue
        start_ms = int(segment.get("start_ms") or 0)
        for needle, _start, _end in context.scanner.matches(text):
            target = context.needles.get(needle)
            alias = context.aliases.get(needle) if target is None else None
            if target is None and alias is None:
                continue  # 挡板
            if target is not None:
                key, file_id = target
                form = None
            else:
                key, form, _usable = alias
                file_id = None
            if file_id is not None and _continues_number(text, _end):
                file_id = None  # 说的是「报价单 v30」，不是 v3 那一份
            hits.setdefault(key, []).append(
                (
                    start_ms,
                    form,
                    file_id,
                    near_version(text, _start, _end),
                    near_final(text, _start, _end),
                )
            )
    spoken: dict[str, dict[str, Any]] = {}
    for key, found in hits.items():
        usable = context.usability.get(key, STEM_NO)
        stem_hits = [hit for hit in found if hit[1] is None]
        alias_counts = Counter(hit[1] for hit in found if hit[1] is not None)
        stem_ok = usable == STEM_YES or (usable == STEM_TWICE and len(stem_hits) >= 2)
        # 两个字的别名要说到两次才连（能不能用按这个叫法本身算）
        forms_ok = {
            form
            for form, times in alias_counts.items()
            if context.aliases[light_key(form)][2] == STEM_YES
            or (context.aliases[light_key(form)][2] == STEM_TWICE and times >= 2)
        }
        counted = [
            hit
            for hit in found
            if (hit[1] is None and stem_ok) or (hit[1] is not None and hit[1] in forms_ok)
        ]
        if not counted:
            continue
        anchors: list[int] = []
        versions: Counter[int] = Counter()
        group = context.groups[key]
        for start_ms, _form, file_id, oral, _final in counted:
            if (not anchors or anchors[-1] != start_ms) and len(anchors) < MAX_ANCHORS:
                anchors.append(start_ms)
            if file_id is not None:
                versions[file_id] += 1
            elif oral is not None:
                for row in group:
                    if is_version(row["name"] or "", oral):
                        versions[row["id"]] += 1
        aliases_said = Counter(hit[1] for hit in counted if hit[1] is not None)
        spoken[key] = {
            "count": len(counted),
            "first_ms": counted[0][0],
            "anchors": anchors,
            "versions": versions,
            "finals": sum(1 for hit in counted if hit[4]),
            # 这一行的命中全来自别名时，线上的字写说得最多的那个叫法
            "alias": aliases_said.most_common(1)[0][0]
            if aliases_said and not any(hit[1] is None for hit in counted)
            else None,
        }
    written: dict[str, dict[str, Any]] = {}
    for line in (minutes or "").splitlines():
        if not line.strip():
            continue
        for needle, _start, _end in context.scanner.matches(line):
            target = context.needles.get(needle)
            if target is None:
                continue  # 挡板和别名（纪要里只认词干本身）
            key, file_id = target
            if file_id is not None and _continues_number(line, _end):
                file_id = None
            entry = written.setdefault(
                key, {"count": 0, "first_ms": _line_anchor(line), "versions": Counter()}
            )
            entry["count"] += 1
            if file_id is not None:
                entry["versions"][file_id] += 1

    result: dict[str, dict[str, Any]] = {}
    for key in set(spoken) | set(written):
        usable = context.usability.get(key, STEM_NO)
        said = spoken.get(key)
        wrote = written.get(key)
        finals = 0
        alias_needle = None
        if said is not None:
            source, count, first_ms, anchors = (
                "transcript",
                said["count"],
                said["first_ms"],
                said["anchors"],
            )
            versions = said["versions"]
            finals = said["finals"]
            alias_needle = said["alias"]
        elif wrote is not None and usable == STEM_YES and len(key) >= MINUTES_MIN_CHARS:
            source, count, first_ms = "minutes", 0, wrote["first_ms"]
            anchors = [first_ms] if first_ms is not None else []
            versions = wrote["versions"]
        else:
            continue
        previous = existing.get(key)
        if previous is not None and previous["status"] == "rejected":
            continue
        group = context.groups[key]
        picked = 0
        followed: dict[str, Any] | None = None
        waiting = False
        if (
            previous is not None
            and previous["picked"]
            and previous["file_id"] not in context.files_by_id
        ):
            followed, waiting = context.follow_picked(key, previous.get("picked_key"))
        if waiting:
            # 还有没算出标识的同名文件：原样保留你选的那份，等内容循环算完标识再比
            result[key] = {
                "stem_key": key,
                "file_id": previous["file_id"],
                "needle": previous["needle"],
                "count": count,
                "first_ms": first_ms,
                "anchors_json": json.dumps(anchors),
                "minutes_count": wrote["count"] if wrote else 0,
                "source": source,
                "picked": 1,
            }
            continue
        finals_in_group = final_files(group) if finals else []
        hinted = None
        if (
            previous is not None
            and previous["picked"]
            and previous["file_id"] in context.files_by_id
        ):
            chosen = context.files_by_id[previous["file_id"]]
            picked = 1
        elif followed is not None:
            chosen = followed  # 挪了位置、改了文件夹：按内容找回，仍算你选的
            picked = 1
        elif versions:
            chosen = max(
                (context.files_by_id[file_id] for file_id in versions),
                key=lambda row: (versions[row["id"]], int(row["mtime_ns"] or 0), row["id"]),
            )
        elif len(finals_in_group) == 1:
            chosen = finals_in_group[0]  # 说了「终版」，组里恰好一份文件名带终版这类字样
        elif (hinted := pick_by_hint(group, hints.get(key), meeting_ns, previous_ns)) is not None:
            chosen = hinted  # 大模型一层留下的提示（「上周那版」「第 3 版」）
        else:
            chosen = _pick_by_date(group, meeting_ns)
        result[key] = {
            "stem_key": key,
            "file_id": chosen["id"],
            "needle": alias_needle or chosen["stem"],
            "count": count,
            "first_ms": first_ms,
            "anchors_json": json.dumps(anchors),
            "minutes_count": wrote["count"] if wrote else 0,
            "source": source,
            "picked": picked,
        }
    ranked = sorted(
        result.values(), key=lambda row: (-row["count"], -row["minutes_count"], row["stem_key"])
    )
    return {row["stem_key"]: row for row in ranked[:PER_MEETING]}


_COMPARED = (
    "file_id",
    "needle",
    "count",
    "first_ms",
    "anchors_json",
    "minutes_count",
    "source",
    "picked",
)


def _write_mentions(
    connection: Any,
    meeting_id: str,
    project_id: str,
    result: dict[str, dict[str, Any]],
    existing_all: list[dict[str, Any]],
) -> None:
    now = utc_now()
    for row in existing_all:
        if row["status"] != "active":
            continue
        if row["project_id"] != project_id or row["stem_key"] not in result:
            connection.execute(
                "DELETE FROM meeting_file_mentions WHERE meeting_id = ? AND project_id = ? AND stem_key = ?",
                (meeting_id, row["project_id"], row["stem_key"]),
            )
    current = {row["stem_key"]: row for row in existing_all if row["project_id"] == project_id}
    for key, item in result.items():
        previous = current.get(key)
        if previous is None:
            connection.execute(
                """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, file_id, needle, count,
                       first_ms, anchors_json, minutes_count, source, status, picked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    meeting_id,
                    project_id,
                    key,
                    item["file_id"],
                    item["needle"],
                    item["count"],
                    item["first_ms"],
                    item["anchors_json"],
                    item["minutes_count"],
                    item["source"],
                    item["picked"],
                    now,
                ),
            )
            continue
        if previous["status"] != "active":
            continue
        # 全都没变就不写（AFTER UPDATE 触发器对没变化的更新也会让关系图版本号加一）
        if all(previous[column] == item[column] for column in _COMPARED):
            continue
        connection.execute(
            """UPDATE meeting_file_mentions SET file_id = ?, needle = ?, count = ?, first_ms = ?,
                   anchors_json = ?, minutes_count = ?, source = ?, picked = ?, updated_at = ?
             WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
            (
                item["file_id"],
                item["needle"],
                item["count"],
                item["first_ms"],
                item["anchors_json"],
                item["minutes_count"],
                item["source"],
                item["picked"],
                now,
                meeting_id,
                project_id,
                key,
            ),
        )


def _pending(connection: Any) -> list[dict[str, Any]]:
    sigs = project_sigs(connection)
    rows = connection.execute(
        """SELECT m.id, m.project_id, m.recording_date, m.created_at,
                  m.current_transcript_version_id AS transcript_id,
                  m.current_minutes_version_id AS minutes_id,
                  s.dirty, s.stems_sig
             FROM meetings m LEFT JOIN meeting_file_scan s ON s.meeting_id = m.id
            WHERE m.project_id IS NOT NULL"""
    ).fetchall()
    todo = []
    for row in rows:
        sig = sigs.get(row["project_id"])
        if sig is None:
            continue
        if row["dirty"] is None or row["dirty"] or row["stems_sig"] != sig:
            todo.append({**dict(row), "sig": sig})
    # 最近的会先比对
    todo.sort(
        key=lambda row: (row["recording_date"] or row["created_at"] or "", row["id"]), reverse=True
    )
    return todo


def read_hints(connection: Any, meeting_id: str) -> dict[str, dict[str, Any]]:
    """mention_extractions.hints_json：{stem_key: {"version": 3} 或 {"rel": "last_week"}}。"""
    row = connection.execute(
        "SELECT hints_json FROM mention_extractions WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row["hints_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): hint for key, hint in value.items() if isinstance(hint, dict)}


def match_meeting(db: Database, row: dict[str, Any], contexts: dict[str, ProjectContext]) -> bool:
    """比对一场会；比对期间会被改过（dirty 变了）就不写，留给下一轮。返回是否写了。"""
    meeting_id = row["id"]
    project_id = row["project_id"]
    with db.autocommit() as connection:
        context = contexts.get(project_id)
        if context is None:
            context = contexts[project_id] = ProjectContext(connection, project_id)
        segments = (
            [
                dict(item)
                for item in connection.execute(
                    "SELECT start_ms, text FROM segments WHERE version_id = ? ORDER BY ordinal",
                    (row["transcript_id"],),
                ).fetchall()
            ]
            if row["transcript_id"]
            else []
        )
        minutes_row = (
            connection.execute(
                "SELECT markdown FROM minutes_versions WHERE id = ?", (row["minutes_id"],)
            ).fetchone()
            if row["minutes_id"]
            else None
        )
        existing_all = [
            dict(item)
            for item in connection.execute(
                """SELECT fm.*, pf.content_key AS picked_key
                     FROM meeting_file_mentions fm LEFT JOIN material_files pf ON pf.id = fm.file_id
                    WHERE fm.meeting_id = ?""",
                (meeting_id,),
            ).fetchall()
        ]
        # 4b：L5 留下的提示（「上周那版」「第 3 版」），没有版本依据时先按它挑
        hints = read_hints(connection, meeting_id)
        meeting_ns = _meeting_ns(row["recording_date"], row["created_at"])
        previous_ns = (
            previous_meeting_ns(connection, meeting_id, project_id, meeting_ns)
            if any(hint.get("rel") == "last_meeting" for hint in hints.values())
            else None
        )
    existing = {item["stem_key"]: item for item in existing_all if item["project_id"] == project_id}
    result = compute_mentions(
        context,
        segments=segments,
        minutes=minutes_row["markdown"] if minutes_row else "",
        meeting_ns=meeting_ns,
        existing=existing,
        hints=hints,
        previous_ns=previous_ns,
    )
    with db.transaction() as connection:
        if row["dirty"] is None:
            claimed = connection.execute(
                """INSERT INTO meeting_file_scan(meeting_id, stems_sig, dirty, scanned_at)
                   VALUES (?, ?, 0, ?) ON CONFLICT(meeting_id) DO NOTHING""",
                (meeting_id, row["sig"], utc_now()),
            ).rowcount
        else:
            claimed = connection.execute(
                """UPDATE meeting_file_scan SET stems_sig = ?, dirty = 0, scanned_at = ?
                    WHERE meeting_id = ? AND dirty = ?""",
                (row["sig"], utc_now(), meeting_id, row["dirty"]),
            ).rowcount
        if not claimed:
            return False
        still = connection.execute(
            "SELECT project_id FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if still is None or still["project_id"] != project_id:
            return False
        _write_mentions(connection, meeting_id, project_id, result, existing_all)
    return True


def match_pending(
    db: Database,
    *,
    clock: Callable[[], float] = time.monotonic,
    max_meetings: int = ROUND_MEETINGS,
    max_seconds: float = ROUND_SECONDS,
) -> dict[str, int]:
    """扫描循环里的一段：比对要比对的会，每轮最多 max_meetings 场或 max_seconds 秒。"""
    with db.autocommit() as connection:
        todo = _pending(connection)
    deadline = clock() + max_seconds
    contexts: dict[str, ProjectContext] = {}
    written = 0
    tried = 0
    for row in todo[:max_meetings]:
        if clock() >= deadline:
            break
        tried += 1
        written += int(match_meeting(db, row, contexts))
    return {"pending": len(todo), "tried": tried, "written": written}


# ---------------------------------------------------------------------- 读


def generic_keys(connection: Any, project_id: str) -> set[str]:
    """被本项目一半以上、至少 4 场会提到的词干。"""
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM meetings WHERE project_id = ?", (project_id,)
    ).fetchone()["n"]
    return {
        row["stem_key"]
        for row in connection.execute(
            """SELECT fm.stem_key, COUNT(DISTINCT fm.meeting_id) AS n
                 FROM meeting_file_mentions fm
                 JOIN meetings m ON m.id = fm.meeting_id AND m.project_id = fm.project_id
                 JOIN material_files f ON f.id = fm.file_id AND f.gone_at IS NULL
                WHERE fm.project_id = ? AND fm.status = 'active'
                GROUP BY fm.stem_key""",
            (project_id,),
        ).fetchall()
        if is_generic(int(row["n"]), int(total))
    }


def is_generic(meetings: int, total: int) -> bool:
    return meetings >= GENERIC_MIN_MEETINGS and meetings * 2 > total


def files_state(connection: Any, meeting_id: str, project_id: str | None) -> str:
    """done / indexing / offline / no_project / no_root。"""
    if not project_id:
        return "no_project"
    roots = connection.execute(
        """SELECT COALESCE(s.state, 'pending') AS state, s.stems_hash IS NOT NULL AS indexed_once
             FROM project_material_roots r LEFT JOIN material_index_state s ON s.root_id = r.id
            WHERE r.project_id = ?""",
        (project_id,),
    ).fetchall()
    if not roots:
        return "no_root"
    if any(row["state"] in (STATE_OFFLINE, STATE_MISSING) for row in roots):
        return "offline"
    if any(not row["indexed_once"] for row in roots):
        return "indexing"
    scan = connection.execute(
        "SELECT dirty, stems_sig FROM meeting_file_scan WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    if (
        scan is None
        or scan["dirty"]
        or scan["stems_sig"] != project_sigs(connection).get(project_id)
    ):
        return "indexing"
    return "done"


def meeting_files(connection: Any, meeting_id: str, project_id: str | None) -> dict[str, Any]:
    """会议简报的 files[] 和 files_state：这场会的全部有效提到，按次数排，通用的排最后。"""
    state = files_state(connection, meeting_id, project_id)
    if not project_id:
        return {"files": [], "files_state": state}
    generic = generic_keys(connection, project_id)
    # v16：字面和放宽的提到一起读（relation_read 管两处互相遮盖）
    rows = relation_read.meeting_mentions(connection, meeting_id, project_id)
    files = [
        {
            "file_id": row["file_id"],
            "name": row["name"],
            "rel_path": row["rel_path"],
            "root_id": row["root_id"],
            "stem_key": row["stem_key"],
            "needle": row["needle"],
            "count": int(row["count"]),
            "minutes_count": int(row["minutes_count"]),
            "first_ms": row["first_ms"],
            "source": row["source"],
            "picked": bool(row["picked"]),
            "generic": row["stem_key"] in generic,
            # 4b：放宽行的 relation_id、说法和对上的方式（字面行都是 None）
            **relation_read.loose_fields(row),
        }
        for row in rows
    ]
    files.sort(
        key=lambda item: (item["generic"], -item["count"], -item["minutes_count"], item["name"])
    )
    return {"files": files[:PER_MEETING], "files_state": state}


def file_detail(
    connection: Any, file_id: int, *, quotes: Callable[[str, list[int]], dict[int, str]]
) -> dict[str, Any]:
    """GET /api/graph/files/{id}：文件信息、同名的其他文件、在哪几场会上被提到。只查库。"""
    from . import related_read  # related_read 引 graph，graph 引本模块

    row = connection.execute(
        """SELECT f.*, r.path AS root_path, r.project_id, p.name AS project_name
             FROM material_files f
             JOIN project_material_roots r ON r.id = f.root_id
             JOIN projects p ON p.id = r.project_id
            WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise MentionNotFound("文件不在索引里（可能已经挪走或删掉了）")
    root_path = str(row["root_path"]).rstrip("/")
    dir_path = f"{root_path}/{row['dir_rel']}" if row["dir_rel"] else root_path
    siblings = [
        {
            "id": item["id"],
            "name": item["name"],
            "rel_path": item["rel_path"],
            "root_id": item["root_id"],
            "modified_at": _iso_ns(item["mtime_ns"]),
        }
        for item in connection.execute(
            f"""SELECT f.id, f.name, f.rel_path, f.root_id, f.mtime_ns
                  FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                 WHERE r.project_id = ? AND f.stem_key = ? AND f.id != ? AND f.stem_key != ''
                   AND f.gone_at IS NULL AND f.zone IN ({_ZONES_SQL})
                 ORDER BY f.mtime_ns DESC, f.id DESC LIMIT 20""",
            (row["project_id"], row["stem_key"], file_id),
        ).fetchall()
    ]
    # v16：先数再取。有效的提到（字面和放宽的）列新的 40 场，你标过「不是这份文件」的另列；
    # 「在 N 场会上被提到」的 N 另数，不受列表长度限制
    # 4b：标过「不是这份文件」的放宽行和字面行进同一个列表
    mention_rows = (
        [
            {**item, "status": "active"}
            for item in relation_read.file_mention_meetings(connection, file_id)
        ]
        + [
            {**item, "status": "rejected"}
            for item in relation_read.rejected_file_mentions(connection, file_id)
        ]
        + [
            {**item, "status": "rejected"}
            for item in relation_read.rejected_loose_mentions(connection, file_id)
        ]
    )
    meetings = []
    for item in mention_rows:
        first_ms = item["first_ms"]
        quote = ""
        if item.get("relation_id") is not None and item.get("quote"):
            quote = item["quote"]  # 放宽行存着那句原话
        elif item["source"] == "transcript" and first_ms is not None:
            quote = quotes(item["meeting_id"], [int(first_ms)]).get(int(first_ms), "")
        meetings.append(
            {
                "meeting_id": item["meeting_id"],
                "title": item["title"],
                "date": item["date"],
                "stem_key": item["stem_key"],
                "needle": item["needle"],
                "count": int(item["count"]),
                "minutes_count": int(item["minutes_count"]),
                "first_ms": first_ms,
                "anchors_ms": json.loads(item["anchors_json"] or "[]"),
                "source": item["source"],
                "status": item["status"],
                "picked": bool(item["picked"]),
                "quote": quote,
                **relation_read.loose_fields(item),
            }
        )
    return {
        "file": {
            "id": row["id"],
            "name": row["name"],
            "ext": row["ext"],
            "stem": row["stem"],
            "stem_key": row["stem_key"],
            "rel_path": row["rel_path"],
            "root_id": row["root_id"],
            "root_path": row["root_path"],
            "folder_path": dir_path,
            "path": f"{root_path}/{row['rel_path']}",
            "size": row["size"],
            "modified_at": _iso_ns(row["mtime_ns"]),
            "zone": row["zone"],
            "gone": row["gone_at"] is not None,
            "project_id": row["project_id"],
            "project_name": row["project_name"],
        },
        "siblings": siblings,
        "meetings": meetings,
        "active_meetings": relation_read.file_mention_counts(connection, [file_id]).get(file_id, 0),
        # 4d：「内容相关的会」最多 5 条（不算进上面的 N）
        "related_meetings": related_read.related_meetings(connection, file_id),
    }


def _iso_ns(value: int | None) -> str | None:
    if not value:
        return None
    return (
        datetime.fromtimestamp(int(value) / 1_000_000_000)
        .astimezone()
        .isoformat(timespec="seconds")
    )


# ---------------------------------------------------------------------- 你的改动


def _mention_row(connection: Any, meeting_id: str, stem_key: str) -> dict[str, Any]:
    meeting = connection.execute(
        "SELECT project_id FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        raise MentionNotFound("会议不存在")
    if not meeting["project_id"]:
        raise MentionError("这场会还没归项目")
    row = connection.execute(
        "SELECT * FROM meeting_file_mentions WHERE meeting_id = ? AND project_id = ? AND stem_key = ?",
        (meeting_id, meeting["project_id"], stem_key),
    ).fetchone()
    if row is None:
        raise MentionNotFound("这场会没有提到这个文件名")
    return dict(row)


def _result(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "meeting_id": row["meeting_id"],
        "project_id": row["project_id"],
        "stem_key": row["stem_key"],
        "file_id": row["file_id"],
        "status": row["status"],
        "picked": bool(row["picked"]),
        **extra,
    }


def _invalidate_scan(connection: Any, meeting_id: str) -> None:
    """你改了这场会的提到：正在进行的比对写不进来（dirty 变了），下一轮按你的改动重新比对。"""
    connection.execute(
        """INSERT INTO meeting_file_scan(meeting_id, dirty) VALUES (?, 1)
           ON CONFLICT(meeting_id) DO UPDATE SET dirty = dirty + 1""",
        (meeting_id,),
    )


def reject_mention(db: Database, meeting_id: str, stem_key: str) -> dict[str, Any]:
    """「不是这份文件」：只挡这场会、这个项目；以后比对也不会改回来。"""
    with db.transaction() as connection:
        row = _mention_row(connection, meeting_id, stem_key)
        if row["status"] != "rejected":
            connection.execute(
                """UPDATE meeting_file_mentions SET status = 'rejected', updated_at = ?
                    WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
                (utc_now(), meeting_id, row["project_id"], stem_key),
            )
            _invalidate_scan(connection, meeting_id)
        row["status"] = "rejected"
    return _result(row)


def restore_mention(db: Database, meeting_id: str, stem_key: str) -> dict[str, Any]:
    """撤销「不是这份文件」：改回有效，这场会下一轮重新比对（次数、文件按现在的算）。"""
    with db.transaction() as connection:
        row = _mention_row(connection, meeting_id, stem_key)
        if row["status"] != "active":
            connection.execute(
                """UPDATE meeting_file_mentions SET status = 'active', updated_at = ?
                    WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
                (utc_now(), meeting_id, row["project_id"], stem_key),
            )
            _invalidate_scan(connection, meeting_id)
        row["status"] = "active"
    return _result(row)


def pick_mention_file(db: Database, meeting_id: str, stem_key: str, file_id: int) -> dict[str, Any]:
    """［换成这份］：换成同名的另一份文件，以后比对不再自动改（文件不在了才换）。"""
    with db.transaction() as connection:
        row = _mention_row(connection, meeting_id, stem_key)
        target = connection.execute(
            f"""SELECT f.id FROM material_files f JOIN project_material_roots r ON r.id = f.root_id
                 WHERE f.id = ? AND r.project_id = ? AND f.stem_key = ? AND f.gone_at IS NULL
                   AND f.zone IN ({_ZONES_SQL})""",
            (file_id, row["project_id"], stem_key),
        ).fetchone()
        if target is None:
            raise MentionError("只能换成这个项目文件夹里同名的另一份文件")
        connection.execute(
            """UPDATE meeting_file_mentions SET file_id = ?, picked = 1, status = 'active', updated_at = ?
                WHERE meeting_id = ? AND project_id = ? AND stem_key = ?""",
            (file_id, utc_now(), meeting_id, row["project_id"], stem_key),
        )
        _invalidate_scan(connection, meeting_id)
        row.update(file_id=file_id, picked=1, status="active")
    # 当场算好内容标识：以后文件挪了位置，按内容找回你选的这份（盘不在、读不了就算了）
    try:
        key_file_now(db, file_id)
    except Exception:  # noqa: BLE001
        pass
    return _result(row)
