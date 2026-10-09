"""需求候选（需求池改版 260930，R01）：AI 从会议纪要里抽出来、还没认领的需求。

候选认领后才建成正式需求（进行中，优先级默认 P2）；合并＝把它的会和原话并进同项目一条需求（进行中、
已搁置、已完成都行，并进已完成的那条重新打开，D13），候选随即消失；丢掉的 30 天内能撤销，同一项目下
同名候选以后不再提示（name_key 留着）。
挂在候选上的任务：认领后随需求走、合并后挂到目标需求、丢掉后回到未挂需求（撤销丢掉时，这期间没被挂到别处的挂回来）。
已经被手动挂到别的需求上的任务不跟着动。

候选不存项目，跟着来源会议当前的归属走；会议没归项目时是「未归项目」，认领时必须选定项目。
认领页上可以改项目：撞名后「改为合并到那条需求」、合并弹层列的需求，都按认领页上选的项目来
（project_id 参数），不改会议归属。
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, utc_now
from .llm import neutralise
from .project_profile import light_key
from .project_seats import MEETING_TIME_SQL, seat_ranks
from .requirements import (
    MERGE_UNDO_MINUTES,
    QUOTE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    anchor_in_recording,
    clean_source,
    clean_title,
    drop_merged_meeting,
    settle_reopen,
    follow_up_count,
    get_requirement,
    insert_requirement,
    load_sources,
    origin_of,
)
from .service import ConflictError, NotFoundError
from .tasks import OPEN_TASK_STATUSES, TaskService, resolve_requirement_and_project

CANDIDATE_STATUSES = ("pending", "claimed", "merged", "dropped")
CLAIM_DEFAULT_PRIORITY = "P2"
DROP_UNDO_DAYS = 30
# 合并能并进同项目这三种状态的需求；并进已完成的，那条需求重新打开为进行中（D13，原先 R01-8 不让并进
# 已完成的）。
MERGE_TARGET_STATUSES = ("active", "shelved", "done")
# AI 判断相近、同名默认合并，只对照进行中、已搁置的需求：已完成的不在抽取对照清单里，候选默认动作不会是
# 「合并到一条已完成的需求」（并进已完成的要用户在合并弹层里自己选，或认领撞名后改为合并）。
SIMILAR_TARGET_STATUSES = ("active", "shelved")
_HANDLED = {
    "claimed": "这条候选已经认领了",
    "merged": "这条候选已经合并了",
    "dropped": "这条候选已经丢掉了",
}


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    """去掉只给排序用的下划线字段。"""
    return {key: value for key, value in item.items() if not key.startswith("_")}


# ---------------------------------------------------------------- 建候选（抽取那一刀调）


def insert_candidate(
    connection: Any,
    *,
    meeting_id: str,
    title: str,
    summary: str = "",
    quote: str = "",
    anchor_ms: int | None = None,
    extraction_id: int | None = None,
    similar_requirement_id: str | None = None,
) -> str:
    """在调用方的事务里建一条待认领候选和它提出时的那句原话，返回候选 id。

    AI 给的东西不因为一处不合规就让整场抽取失败：标题、说明、原话超长时截断（保证候选原样就能
    认领），时间锚不是录音里的时间点时当没有，相近需求不存在时当没有。"""
    title = clean_title(title)[:TITLE_MAX_CHARS].strip()
    if not title:
        raise ValueError("候选标题不能为空")
    meeting = connection.execute(
        "SELECT duration_ms FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if meeting is not None and not anchor_in_recording(anchor_ms, meeting["duration_ms"]):
        anchor_ms = None
    source = clean_source(
        connection,
        {
            "meeting_id": meeting_id,
            "quote": (quote or "")[:QUOTE_MAX_CHARS],
            "anchor_ms": anchor_ms,
        },
    )
    if similar_requirement_id and (
        connection.execute(
            "SELECT 1 FROM requirements WHERE id=?", (similar_requirement_id,)
        ).fetchone()
        is None
    ):
        similar_requirement_id = None
    candidate_id = f"candidate-{uuid.uuid4().hex}"
    now = utc_now()
    # 记下抽出时会议的归属：会议事后改了项目，墙面取数时据此按新项目重核去重（reconcile_moved）
    project = connection.execute(
        "SELECT project_id FROM meetings WHERE id=?", (source["meeting_id"],)
    ).fetchone()
    connection.execute(
        """INSERT INTO requirement_candidates
               (id, meeting_id, extraction_id, title, name_key, summary, similar_requirement_id,
                status, project_id_seen, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (
            candidate_id,
            source["meeting_id"],
            extraction_id,
            title,
            light_key(title),
            (summary or "").strip()[:SUMMARY_MAX_CHARS],
            similar_requirement_id or None,
            project["project_id"] if project else None,
            now,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO requirement_sources
               (candidate_id, kind, meeting_id, quote, anchor_ms, created_at)
           VALUES (?, 'origin', ?, ?, ?, ?)""",
        (candidate_id, source["meeting_id"], source["quote"], source["anchor_ms"], now),
    )
    return candidate_id


# ---------------------------------------------------------------- 会后抽取（R01-2～6、9）

# 每场会一次抽取最多出 5 条候选（R01-5）
MAX_CANDIDATES_PER_MEETING = 5
# 提示词里列给 AI 对照的同项目需求、待认领候选，各取最近改过的这么多条
CONTEXT_LIMIT = 40
# 原话去掉标点、空白后至少这么多个字才拿去逐字稿里找：「嗯」「的」到处都有，找到了也不是那句
QUOTE_MIN_CHARS = 4
_CONTEXT_STATUS = {"active": "进行中", "shelved": "已搁置"}
# AI 偶尔把对照清单的编号当成需求名
_CODE_TITLE = re.compile(r"^[RC]\d+$", re.IGNORECASE)
# 说明超过 70 字时在这些句读处截断（截出来不短于 40 字时）
_SENTENCE_ENDS = "。；！？;!?"


def candidates_wanted(connection: Any, batch_created_at: str | None, meeting_id: str) -> bool:
    """这一批抽取要不要出候选：候选上线时刻（迁移写下的 requirement_candidates_since）以后建的批次，
    并且这场会是上线以后才进声档的。R01-4「上线前的历史会议不自动回填」按会算：历史会议的纪要上线后
    重新导入、重新生成，或者点［重新抽取］，都只抽任务；要候选在会议详情手动点［抽需求候选］。
    没有这个键时一律不出：宁可漏抽，不轰炸。"""
    if not batch_created_at:
        return False
    row = connection.execute(
        """SELECT julianday(?) >= julianday(s.value)
                  AND julianday(m.created_at) >= julianday(s.value) AS wanted
             FROM app_state s JOIN meetings m ON m.id = ?
            WHERE s.key = 'requirement_candidates_since'""",
        (batch_created_at, meeting_id),
    ).fetchone()
    return bool(row and row["wanted"])


def _replaceable(connection: Any, meeting_id: str) -> list[dict[str, Any]]:
    """重抽时换掉的候选（R01-5）：这场会提出的、还没处理的，并且没有别的会并进来的原话。
    并进过别的会原话的留着，不然那几场会的原话会跟着没了。"""
    return [
        dict(row)
        for row in connection.execute(
            """SELECT c.id, c.name_key FROM requirement_candidates c
                WHERE c.meeting_id = ? AND c.status = 'pending'
                  AND NOT EXISTS (SELECT 1 FROM requirement_sources s
                                   WHERE s.candidate_id = c.id AND s.meeting_id <> c.meeting_id)
                ORDER BY c.created_at, c.id""",
            (meeting_id,),
        ).fetchall()
    ]


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _text(value: Any) -> str:
    """AI 给的文字字段：不是字符串的当没给（列表、对象、数字不拿 repr 当名字）；去掉孤立的代理字符
    （JSON 里收得下、写库会报错），并成一行。"""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(char for char in value if not 0xD800 <= ord(char) <= 0xDFFF).split())


def _summary(value: Any) -> str:
    """说明最多 70 字（R01-3 要 40～70 字）：长了在 40 字以后最近的句末截断，找不到再硬截。"""
    text = _text(value)
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    head = text[:SUMMARY_MAX_CHARS]
    cut = max(head.rfind(mark) for mark in _SENTENCE_ENDS)
    return head[: cut + 1] if cut + 1 >= 40 else head


def _listed(title: str) -> str:
    """列进对照清单的名字：一行，尖括号换成全角，名字里的「</existing_requirements>」关不掉标签。"""
    return _one_line(neutralise(title, TITLE_MAX_CHARS))


def _plain(text: str) -> str:
    """比对原话用：NFKC、不分大小写，只留字和数字（标点、空白、全半角差别都不算）。"""
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", text).casefold()
        if unicodedata.category(char)[0] in "LN"
    )


def _transcript_index(connection: Any, meeting_id: str) -> tuple[str, list[int]]:
    """这场会当前逐字稿连成一串（_plain 过的字），和每个字所在那句的开始时间。"""
    chars: list[str] = []
    starts: list[int] = []
    for row in connection.execute(
        """SELECT start_ms, text FROM segments
            WHERE version_id = (SELECT current_transcript_version_id FROM meetings WHERE id = ?)
            ORDER BY start_ms""",
        (meeting_id,),
    ).fetchall():
        plain = _plain(row["text"] or "")
        chars.append(plain)
        starts.extend([row["start_ms"]] * len(plain))
    return "".join(chars), starts


def _quote_anchor(index: tuple[str, list[int]], quote: str) -> int | None:
    """原话整句要在逐字稿里找得到（可以跨几句，标点和全半角不算），时间锚取它开头那句的开始时间。
    只对上开头、后半句是编的，或者短得到处都有的，都不算原话（R01-3「会上原话和时间锚」）。"""
    needle = _plain(quote)
    if len(needle) < QUOTE_MIN_CHARS:
        return None
    text, starts = index
    position = text.find(needle)
    return starts[position] if position >= 0 else None


def extraction_context(connection: Any, meeting_id: str) -> dict[str, Any]:
    """抽候选时给 AI 对照的（R01-6）：会议所属项目里进行中、已搁置的需求和其他待认领候选，
    编号 R1…、C1…（refs 记编号对应的是哪一条）。这场会重抽时要换掉的候选不列；会议没归项目时不对照。"""
    meeting = connection.execute(
        "SELECT project_id FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    project_id = meeting["project_id"] if meeting else None
    context: dict[str, Any] = {"project_id": project_id, "refs": {}, "lines": []}
    if project_id is None:
        return context
    requirements = connection.execute(
        f"""SELECT id, title, status FROM requirements
             WHERE project_id = ? AND status IN ({", ".join("?" for _ in SIMILAR_TARGET_STATUSES)})
             ORDER BY julianday(updated_at) DESC, id LIMIT ?""",
        (project_id, *SIMILAR_TARGET_STATUSES, CONTEXT_LIMIT),
    ).fetchall()
    replaceable = {row["id"] for row in _replaceable(connection, meeting_id)}
    candidates = [
        row
        for row in connection.execute(
            """SELECT c.id, c.title FROM requirement_candidates c
                 JOIN meetings m ON m.id = c.meeting_id
                WHERE c.status = 'pending' AND m.project_id = ?
                ORDER BY julianday(c.updated_at) DESC, c.id LIMIT ?""",
            (project_id, CONTEXT_LIMIT + len(replaceable)),
        ).fetchall()
        if row["id"] not in replaceable
    ][:CONTEXT_LIMIT]
    for index, row in enumerate(requirements, 1):
        code = f"R{index}"
        context["refs"][code] = ("requirement", row["id"])
        context["lines"].append(
            f"{code} {_listed(row['title'])}（{_CONTEXT_STATUS[row['status']]}）"
        )
    for index, row in enumerate(candidates, 1):
        code = f"C{index}"
        context["refs"][code] = ("candidate", row["id"])
        context["lines"].append(f"{code} {_listed(row['title'])}（待认领）")
    return context


def prompt_rules(context: dict[str, Any], *, with_tasks: bool) -> str:
    """提示词里抽候选的规则和对照清单。和任务同一次抽取时（R01-2），任务可以挂到候选上。"""
    if context["project_id"] is None:
        listing = "（这场会没归项目，不用对照，same_as 一律填 null）"
    else:
        listing = (
            "\n".join(context["lines"]) or "（这个项目还没有进行中、已搁置的需求和待认领的候选）"
        )
    task_link = (
        "- tasks 里的任务是在为某条需求候选做事时，requirement_no 填那条候选的 no，否则填 null。\n"
        if with_tasks
        else ""
    )
    return (
        "抽「需求候选」，放进 requirements：\n"
        "- 需求＝要交付的一块功能或改造（要写 PRD、画原型的那种）；任务＝某个人要去做的一个动作"
        "（发材料、问口径、约会）。同一件事只放一边。\n"
        "- 只抽会上提出、要交付的功能或改造；背景介绍、已经上线的功能、只抱怨不说要改什么的不抽。"
        "宁缺勿滥，没有就返回空列表，不要编造。\n"
        f"- 最多 {MAX_CANDIDATES_PER_MEETING} 条，按重要性从高到低排列。\n"
        "- title 是需求名：十来个字的名词短语，说清要做什么（如「科室会预约后台导出」），不写成句子。\n"
        "- summary 是说明：40～70 个字，说清谁在哪个环节遇到什么问题、要做成什么样，"
        "只写纪要和逐字稿里有的。\n"
        "- anchor_quote 必须是逐字稿里提出这件事的原句摘录（短、可回听定位）。\n"
        "- same_as：<existing_requirements> 里说的是同一件事、或明显相近（同一件事的一部分、换了个说法）"
        "的，填它的编号（如 R2、C1），没有填 null。\n"
        f"{task_link}"
        "<existing_requirements>\n"
        f"{listing}\n"
        "</existing_requirements>\n"
    )


def item_no(value: Any) -> int | None:
    """AI 写的候选序号：1、"1"、"#1"、全角数字都认；①、² 这类不是十进制数字的不认。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = value.strip().lstrip("#").strip()
        if digits.isdecimal():
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def _same_as(
    connection: Any, code: Any, refs: dict[str, tuple[str, str]], project_id: str | None
) -> tuple[str, str] | None:
    """AI 填的 same_as 编号：对照清单里有、并且现在还在同项目、还能并进去的才算。"""
    if project_id is None or not isinstance(code, str):
        return None
    ref = refs.get(code.strip().upper())
    if ref is None:
        return None
    kind, ref_id = ref
    if kind == "requirement":
        row = connection.execute(
            f"""SELECT 1 FROM requirements
                 WHERE id = ? AND project_id = ?
                   AND status IN ({", ".join("?" for _ in SIMILAR_TARGET_STATUSES)})""",
            (ref_id, project_id, *SIMILAR_TARGET_STATUSES),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT 1 FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
                WHERE c.id = ? AND c.status = 'pending' AND m.project_id = ?""",
            (ref_id, project_id),
        ).fetchone()
    return (kind, ref_id) if row else None


def _requirement_keys(connection: Any, project_id: str | None) -> dict[str, str]:
    """同项目进行中、已搁置的需求，名字（light_key）→ id；名字相同的取先建的。"""
    keys: dict[str, str] = {}
    if project_id is None:
        return keys
    for row in connection.execute(
        f"""SELECT id, title FROM requirements
             WHERE project_id = ?
               AND status IN ({", ".join("?" for _ in SIMILAR_TARGET_STATUSES)})
             ORDER BY created_at, id""",
        (project_id, *SIMILAR_TARGET_STATUSES),
    ).fetchall():
        keys.setdefault(light_key(row["title"]), row["id"])
    return keys


def _pending_same_name(
    connection: Any, key: str, project_id: str | None, exclude: set[str]
) -> str | None:
    """同项目里同名（light_key 相同）的待认领候选，先建的那条。"""
    if project_id is None:
        return None
    for row in connection.execute(
        """SELECT c.id FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
            WHERE c.status = 'pending' AND c.name_key = ? AND m.project_id = ?
            ORDER BY c.created_at, c.id""",
        (key, project_id),
    ).fetchall():
        if row["id"] not in exclude:
            return row["id"]
    return None


def _dropped_before(connection: Any, key: str, meeting_id: str, project_id: str | None) -> bool:
    """同项目丢掉过同名候选的不再提示（R01-9）。项目按丢掉时那场会的归属算（会议后来改了归属，
    丢掉的名字不跟着搬家）；会议没归项目时只看这场会自己丢掉过的。"""
    return (
        connection.execute(
            """SELECT 1 FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
                WHERE c.status = 'dropped' AND c.name_key = ?
                  AND (c.meeting_id = ?
                       OR (? IS NOT NULL AND COALESCE(c.dropped_project_id, m.project_id) = ?))
                LIMIT 1""",
            (key, meeting_id, project_id, project_id),
        ).fetchone()
        is not None
    )


def _handled_before(connection: Any, key: str, meeting_id: str) -> bool:
    """这场会出过、已经认领或合并的同名候选，以及这场会已经关联着的同名需求：再抽一遍不再冒出来
    （R01-5「已认领、已合并的不动」）。名字不同的相近需求照常出候选，默认合并过去。"""
    if (
        connection.execute(
            """SELECT 1 FROM requirement_candidates
                WHERE meeting_id = ? AND name_key = ? AND status IN ('claimed', 'merged')
                LIMIT 1""",
            (meeting_id, key),
        ).fetchone()
        is not None
    ):
        return True
    return any(
        light_key(row["title"]) == key
        for row in connection.execute(
            """SELECT r.title FROM requirements r
                 JOIN requirement_meetings rm ON rm.requirement_id = r.id
                WHERE rm.meeting_id = ?""",
            (meeting_id,),
        ).fetchall()
    )


def _clean_anchor(connection: Any, meeting_id: str, anchor_ms: int | None) -> int | None:
    meeting = connection.execute(
        "SELECT duration_ms FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if meeting is not None and not anchor_in_recording(anchor_ms, meeting["duration_ms"]):
        return None
    return anchor_ms


def _merge_into_candidate(
    connection: Any, candidate_id: str, meeting_id: str, quote: str, anchor_ms: int | None
) -> bool:
    """不另出一条：这场会和原话并进那条待认领候选（R01-6）。这场会已经在它的来源里时不重复加，返回 False。"""
    if (
        connection.execute(
            "SELECT 1 FROM requirement_sources WHERE candidate_id=? AND meeting_id=?",
            (candidate_id, meeting_id),
        ).fetchone()
        is not None
    ):
        return False
    source = clean_source(
        connection,
        {
            "meeting_id": meeting_id,
            "quote": quote,
            "anchor_ms": _clean_anchor(connection, meeting_id, anchor_ms),
        },
    )
    now = utc_now()
    connection.execute(
        """INSERT INTO requirement_sources
               (candidate_id, kind, meeting_id, quote, anchor_ms, created_at)
           VALUES (?, 'merged', ?, ?, ?, ?)""",
        (candidate_id, source["meeting_id"], source["quote"], source["anchor_ms"], now),
    )
    connection.execute(
        "UPDATE requirement_candidates SET updated_at=? WHERE id=?", (now, candidate_id)
    )
    return True


def _refresh_candidate(
    connection: Any,
    candidate_id: str,
    *,
    meeting_id: str,
    project_id: str | None,
    title: str,
    summary: str,
    quote: str,
    anchor_ms: int | None,
    similar_requirement_id: str | None,
    extraction_id: int | None,
) -> None:
    """重抽又抽到这场会同名的候选：原地换成这次的名字、说明、原话和相近需求，id 不变——挂着的任务、
    开着的认领页都还认得它（R01-5 只替换还没处理的候选）。"""
    source = clean_source(
        connection,
        {
            "meeting_id": meeting_id,
            "quote": quote,
            "anchor_ms": _clean_anchor(connection, meeting_id, anchor_ms),
        },
    )
    now = utc_now()
    connection.execute(
        """UPDATE requirement_candidates
              SET title=?, name_key=?, summary=?, similar_requirement_id=?,
                  extraction_id=COALESCE(?, extraction_id), project_id_seen=?, updated_at=?
            WHERE id=?""",
        (
            title,
            light_key(title),
            summary,
            similar_requirement_id,
            extraction_id,
            project_id,
            now,
            candidate_id,
        ),
    )
    connection.execute(
        """UPDATE requirement_sources SET quote=?, anchor_ms=?
            WHERE candidate_id=? AND kind='origin'""",
        (source["quote"], source["anchor_ms"], candidate_id),
    )


def save_extracted(
    connection: Any,
    *,
    meeting_id: str,
    items: list[dict[str, Any]],
    context: dict[str, Any],
    extraction_id: int | None = None,
) -> dict[str, Any]:
    """在调用方的事务里把 AI 抽出的候选落库。返回 by_no（AI 的序号 → 候选 id，同一次抽出的任务按它
    挂上去）、created、updated（重抽原地更新的）、merged（并进的已有候选）、removed（撤下的条数）、
    skipped（没出的和原因）。

    - 这场会还没处理的候选：这次又抽到同名的原地更新，没抽到的撤下；已认领、已合并、已丢掉的不动（R01-5）。
    - 和同项目进行中、已搁置的需求相近：建候选，默认动作是合并到那一条（R01-6）。
    - 和同项目别的待认领候选相近：不另出一条，把这场会和原话并进那条（R01-6）。
    - AI 没认出、但名字完全相同的，照相近处理。
    - 同项目丢掉过的同名候选不再提示（R01-9）；这场会出过、认领或合并了的同名候选，和这场会已经关联着的
      同名需求，也不再出。
    - 每场会一次最多 5 条（R01-5）；原话要在逐字稿里整句找得到，找不到的只留来源会议。
    - AI 给的哪一条出了错（字段类型不对、写库失败）只跳过那一条，不牵连别的、更不牵连同一次抽出的任务。"""
    meeting = connection.execute(
        "SELECT project_id FROM meetings WHERE id=?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        raise NotFoundError(f"会议不存在：{meeting_id}")
    project_id = meeting["project_id"]
    # 抽取期间会议改了归属：AI 对照的是旧项目的清单，编号作废，只按名字兜底
    refs = context["refs"] if project_id == context["project_id"] else {}
    requirement_keys = _requirement_keys(connection, project_id)
    replaceable: dict[str, str] = {}
    for row in _replaceable(connection, meeting_id):
        replaceable.setdefault(row["name_key"], row["id"])
    replaceable_ids = set(replaceable.values())
    transcript = _transcript_index(connection, meeting_id)

    result: dict[str, Any] = {
        "by_no": {},
        "created": [],
        "updated": [],
        "merged": [],
        "removed": 0,
        "skipped": [],
    }
    seen: dict[str, str] = {}
    kept: set[str] = set()
    for index, item in enumerate(items, 1):
        no = item_no(item.get("no")) or index
        title = clean_title(_text(item.get("title")))[:TITLE_MAX_CHARS].strip()
        key = light_key(title)
        if not key or _CODE_TITLE.match(title):
            result["skipped"].append(f"第 {index} 条没有需求名")
            continue
        if key in seen:
            # 同一次抽出两条同名的：只出一条，挂在第二条上的任务挂到第一条上
            result["by_no"][no] = seen[key]
            result["skipped"].append(f"「{title}」和这次抽出的另一条同名")
            continue
        if len(result["created"]) + len(result["updated"]) + len(result["merged"]) >= (
            MAX_CANDIDATES_PER_MEETING
        ):
            result["skipped"].append(f"「{title}」超过每场会 {MAX_CANDIDATES_PER_MEETING} 条")
            continue
        if _dropped_before(connection, key, meeting_id, project_id):
            result["skipped"].append(f"「{title}」同项目丢掉过同名的候选")
            continue
        if _handled_before(connection, key, meeting_id):
            result["skipped"].append(f"「{title}」这场会已经出过，认领或合并了")
            continue
        quote = _text(item.get("anchor_quote"))[:QUOTE_MAX_CHARS]
        anchor_ms = _quote_anchor(transcript, quote)
        if anchor_ms is None:
            quote = ""
        connection.execute("SAVEPOINT candidate_item")
        try:
            target = _same_as(connection, item.get("same_as"), refs, project_id)
            similar_id = target[1] if target is not None and target[0] == "requirement" else None
            if key in replaceable:
                candidate_id = replaceable[key]
                _refresh_candidate(
                    connection,
                    candidate_id,
                    meeting_id=meeting_id,
                    project_id=project_id,
                    title=title,
                    summary=_summary(item.get("summary")),
                    quote=quote,
                    anchor_ms=anchor_ms,
                    similar_requirement_id=similar_id or requirement_keys.get(key),
                    extraction_id=extraction_id,
                )
                kept.add(candidate_id)
                result["updated"].append(candidate_id)
            else:
                if target is None:
                    pending = _pending_same_name(connection, key, project_id, replaceable_ids)
                    if pending is not None:
                        target = ("candidate", pending)
                    elif key in requirement_keys:
                        target = ("requirement", requirement_keys[key])
                if target is not None and target[0] == "candidate":
                    candidate_id = target[1]
                    if _merge_into_candidate(
                        connection, candidate_id, meeting_id, quote, anchor_ms
                    ):
                        result["merged"].append(candidate_id)
                else:
                    candidate_id = insert_candidate(
                        connection,
                        meeting_id=meeting_id,
                        title=title,
                        summary=_summary(item.get("summary")),
                        quote=quote,
                        anchor_ms=anchor_ms,
                        extraction_id=extraction_id,
                        similar_requirement_id=target[1] if target is not None else None,
                    )
                    result["created"].append(candidate_id)
        except Exception as error:
            connection.execute("ROLLBACK TO candidate_item")
            connection.execute("RELEASE candidate_item")
            result["skipped"].append(f"「{title}」没存上：{error}")
            continue
        connection.execute("RELEASE candidate_item")
        seen[key] = candidate_id
        result["by_no"][no] = candidate_id
    for candidate_id in replaceable_ids - kept:
        # 有人挂过任务的不撤：已确认的、别的会的任务挂在上面（项目页与待办改版 R07-8），撤下会让任务的挂接
        # 悄悄没了。这场会自己抽出的草稿挂着的不算，那是 AI 上次的配对，照旧撤下；但用户手动改挂过的
        # 草稿（有 requirement_changed 事件，同 todo.recommend 区分 paired / current）算有人挂过。
        if connection.execute(
            """SELECT 1 FROM tasks t JOIN requirement_candidates c ON c.id = t.candidate_id
                WHERE t.candidate_id = ?
                  AND NOT (t.meeting_id IS c.meeting_id
                           AND t.status IN ('pending_confirm', 'expired')
                           AND NOT EXISTS (SELECT 1 FROM task_events e
                                            WHERE e.task_id = t.id
                                              AND e.kind = 'requirement_changed'))""",
            (candidate_id,),
        ).fetchone():
            continue
        connection.execute("DELETE FROM requirement_candidates WHERE id=?", (candidate_id,))
        result["removed"] += 1
    return result


def _absorb(connection: Any, candidate_id: str, into_id: str) -> None:
    """一条待认领候选并进同项目同名的另一条：来源（这场会已经在那条里的不重复）和挂着的任务都过去，
    这条删掉。"""
    for source in connection.execute(
        "SELECT id, meeting_id FROM requirement_sources WHERE candidate_id=?", (candidate_id,)
    ).fetchall():
        if (
            connection.execute(
                "SELECT 1 FROM requirement_sources WHERE candidate_id=? AND meeting_id=?",
                (into_id, source["meeting_id"]),
            ).fetchone()
            is None
        ):
            connection.execute(
                "UPDATE requirement_sources SET candidate_id=?, kind='merged' WHERE id=?",
                (into_id, source["id"]),
            )
    connection.execute(
        "UPDATE tasks SET candidate_id=? WHERE candidate_id=?", (into_id, candidate_id)
    )
    connection.execute("DELETE FROM requirement_candidates WHERE id=?", (candidate_id,))
    connection.execute(
        "UPDATE requirement_candidates SET updated_at=? WHERE id=?", (utc_now(), into_id)
    )


_MOVED_SQL = """FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
               WHERE c.status = 'pending' AND m.project_id IS NOT c.project_id_seen"""


def reconcile_moved(db: Database) -> int:
    """会议抽完候选以后才归项目、改项目（手动改、AI 判断、合并项目）：按新项目把去重和「同项目丢掉过的
    不再提示」再核一遍（R01-6、R01-9）——同项目丢掉过同名的，这条也算丢掉（30 天内能撤销）；同项目已有
    同名的待认领候选，并进那条；同项目有同名的进行中、已搁置需求，默认合并到它。墙面取数、会后抽取前各跑
    一次，没有要核的就不开写事务。返回核过的条数。"""
    with db.autocommit() as connection:
        if connection.execute(f"SELECT 1 {_MOVED_SQL} LIMIT 1").fetchone() is None:
            return 0
    count = 0
    with db.transaction() as connection:
        rows = connection.execute(
            f"""SELECT c.id, c.name_key, c.similar_requirement_id, m.project_id
                  {_MOVED_SQL}
                 ORDER BY c.created_at, c.id"""
        ).fetchall()
        for row in rows:
            count += 1
            candidate_id = row["id"]
            project_id = row["project_id"]
            now = utc_now()
            if project_id is not None:
                dropped = connection.execute(
                    """SELECT 1 FROM requirement_candidates c JOIN meetings m ON m.id = c.meeting_id
                        WHERE c.status = 'dropped' AND c.name_key = ? AND c.id <> ?
                          AND COALESCE(c.dropped_project_id, m.project_id) = ?
                        LIMIT 1""",
                    (row["name_key"], candidate_id, project_id),
                ).fetchone()
                if dropped is not None:
                    connection.execute(
                        """UPDATE requirement_candidates
                              SET status='dropped', dropped_at=?, dropped_project_id=?,
                                  project_id_seen=?, updated_at=?
                            WHERE id=?""",
                        (now, project_id, project_id, now, candidate_id),
                    )
                    connection.execute(
                        "UPDATE tasks SET candidate_id=NULL WHERE candidate_id=?", (candidate_id,)
                    )
                    continue
                other = _pending_same_name(connection, row["name_key"], project_id, {candidate_id})
                if other is not None:
                    _absorb(connection, candidate_id, other)
                    continue
                similar = _requirement_keys(connection, project_id).get(row["name_key"])
                if similar is not None:
                    connection.execute(
                        "UPDATE requirement_candidates SET similar_requirement_id=? WHERE id=?",
                        (similar, candidate_id),
                    )
            connection.execute(
                "UPDATE requirement_candidates SET project_id_seen=?, updated_at=? WHERE id=?",
                (project_id, now, candidate_id),
            )
    return count


# ---------------------------------------------------------------- 读


def candidate_items(
    connection: Any,
    *,
    statuses: tuple[str, ...],
    candidate_ids: list[str] | None = None,
    dropped_since: str | None = None,
) -> list[dict[str, Any]]:
    """候选在海报墙上的样子（kind="candidate"），字段和需求海报对齐；_latest_jd、_created_jd 只给排序用。

    默认动作：AI 判断的相近需求还在同项目、还是进行中或已搁置时是「合并」，否则「认领」。
    can_merge：所属项目下有可合并的需求（三种状态都算）才显示［合并］（未归项目的候选不能合并）。"""
    clauses = [f"c.status IN ({', '.join('?' for _ in statuses)})"]
    params: list[Any] = [*statuses]
    if candidate_ids is not None:
        if not candidate_ids:
            return []
        clauses.append(f"c.id IN ({', '.join('?' for _ in candidate_ids)})")
        params.extend(candidate_ids)
    if dropped_since is not None:
        clauses.append("julianday(c.dropped_at) >= julianday(?)")
        params.append(dropped_since)
    open_placeholders = ", ".join("?" for _ in OPEN_TASK_STATUSES)
    rows = connection.execute(
        f"""SELECT c.*, m.project_id, p.name AS project_name, p.color AS project_color,
                   julianday(c.created_at) AS created_jd,
                   (SELECT COUNT(*) FROM tasks t
                     WHERE t.candidate_id = c.id AND t.requirement_id IS NULL
                       AND t.status IN ({open_placeholders})) AS open_task_count
              FROM requirement_candidates c
              JOIN meetings m ON m.id = c.meeting_id
              LEFT JOIN projects p ON p.id = m.project_id
             WHERE {" AND ".join(clauses)}""",
        (*OPEN_TASK_STATUSES, *params),
    ).fetchall()
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    sources = load_sources(connection, owner="candidate", ids=ids)
    latest = {
        row["candidate_id"]: row
        for row in connection.execute(
            f"""SELECT s.candidate_id, MAX(julianday({MEETING_TIME_SQL})) AS jd,
                       {MEETING_TIME_SQL} AS latest
                  FROM requirement_sources s JOIN meetings m ON m.id = s.meeting_id
                 WHERE s.candidate_id IN ({", ".join("?" for _ in ids)})
                 GROUP BY s.candidate_id""",
            tuple(ids),
        ).fetchall()
    }
    similar_ids = sorted({row["similar_requirement_id"] for row in rows} - {None})
    similar = (
        {
            row["id"]: dict(row)
            for row in connection.execute(
                f"""SELECT id, title, status, project_id FROM requirements
                     WHERE id IN ({", ".join("?" for _ in similar_ids)})""",
                tuple(similar_ids),
            ).fetchall()
        }
        if similar_ids
        else {}
    )
    mergeable_projects = {
        row["project_id"]
        for row in connection.execute(
            f"""SELECT DISTINCT project_id FROM requirements
                 WHERE status IN ({", ".join("?" for _ in MERGE_TARGET_STATUSES)})""",
            MERGE_TARGET_STATUSES,
        ).fetchall()
    }
    seats = seat_ranks(connection)
    items: list[dict[str, Any]] = []
    for row in rows:
        own_sources = sources.get(row["id"], [])
        origin = origin_of(own_sources)
        meeting_ids = {source["meeting_id"] for source in own_sources}
        project_id = row["project_id"]
        target = similar.get(row["similar_requirement_id"])
        mergeable_target = (
            target is not None
            and target["status"] in SIMILAR_TARGET_STATUSES
            and target["project_id"] == project_id
        )
        latest_row = latest.get(row["id"])
        items.append(
            {
                "kind": "candidate",
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "status": row["status"],
                "priority": None,
                "project_id": project_id,
                "project_name": row["project_name"],
                "project_color": row["project_color"],
                "project_seat": seats.get(project_id) if project_id else None,
                "open_task_count": row["open_task_count"],
                "meeting_count": len(meeting_ids),
                "folder_count": 0,
                "latest_meeting_date": latest_row["latest"] if latest_row else None,
                "source": origin,
                "follow_up_count": follow_up_count(len(meeting_ids)),
                "similar_requirement": (
                    {key: target[key] for key in ("id", "title", "status")}
                    if mergeable_target
                    else None
                ),
                "default_action": "merge" if mergeable_target else "claim",
                "can_merge": project_id is not None and project_id in mergeable_projects,
                "requirement_id": row["requirement_id"],
                "dropped_at": row["dropped_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "_latest_jd": latest_row["jd"] if latest_row else None,
                "_created_jd": row["created_jd"],
            }
        )
    return items


def get_candidate(db: Database, candidate_id: str) -> dict[str, Any]:
    """认领页（S02）用：海报上的字段，加上全部来源。认领、合并过的候选来源已经挂到需求上，
    requirement_id 指过去。"""
    with db.autocommit() as connection:
        items = candidate_items(
            connection, statuses=CANDIDATE_STATUSES, candidate_ids=[candidate_id]
        )
        if not items:
            raise NotFoundError(f"候选不存在：{candidate_id}")
        item = public_item(items[0])
        item["sources"] = load_sources(connection, owner="candidate", ids=[candidate_id]).get(
            candidate_id, []
        )
    return item


def list_dropped(db: Database, *, now: datetime | None = None) -> dict[str, Any]:
    """「已丢掉」：30 天内丢掉、还能撤销的候选，最近丢掉的在前。"""
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=DROP_UNDO_DAYS)).isoformat()
    with db.autocommit() as connection:
        items = candidate_items(connection, statuses=("dropped",), dropped_since=cutoff)
    items.sort(key=lambda item: (item["dropped_at"] or "", item["id"]), reverse=True)
    for item in items:
        item["restore_until"] = (
            datetime.fromisoformat(item["dropped_at"]) + timedelta(days=DROP_UNDO_DAYS)
        ).isoformat()
    return {
        "items": [public_item(item) for item in items],
        "total": len(items),
        "undo_days": DROP_UNDO_DAYS,
    }


def merge_targets(db: Database, candidate_id: str, project_id: str | None = None) -> dict[str, Any]:
    """合并弹层（S03）：候选所属项目里进行中、已搁置、已完成的需求（D13），AI 判断的相近需求排第一、
    标 recommended，其余按进行中 → 已搁置 → 已完成。
    每条带提出它的那场会（没有来源时取最近一场关联会议）。不跨项目；project_id 是认领页上改选的项目
    （候选改了所属项目后按新项目列，R01-8），不传时取来源会议的归属，未归项目的候选没有可选的。"""
    with db.autocommit() as connection:
        candidate = _candidate_row(connection, candidate_id)
        project_id = _merge_project(connection, candidate, project_id)
        if project_id is None:
            return {"project_id": None, "items": []}
        rows = [
            dict(row)
            for row in connection.execute(
                f"""SELECT r.id, r.title, r.status, r.priority,
                           (SELECT m.title FROM requirement_meetings rm
                              JOIN meetings m ON m.id = rm.meeting_id
                             WHERE rm.requirement_id = r.id
                             ORDER BY julianday({MEETING_TIME_SQL}) DESC LIMIT 1) AS latest_title,
                           (SELECT {MEETING_TIME_SQL} FROM requirement_meetings rm
                              JOIN meetings m ON m.id = rm.meeting_id
                             WHERE rm.requirement_id = r.id
                             ORDER BY julianday({MEETING_TIME_SQL}) DESC LIMIT 1) AS latest_date
                      FROM requirements r
                     WHERE r.project_id = ?
                       AND r.status IN ({", ".join("?" for _ in MERGE_TARGET_STATUSES)})""",
                (project_id, *MERGE_TARGET_STATUSES),
            ).fetchall()
        ]
        origins = load_sources(connection, owner="requirement", ids=[row["id"] for row in rows])
    recommended = candidate["similar_requirement_id"]
    items = []
    for row in rows:
        origin = origin_of(origins.get(row["id"], []))
        items.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "priority": row["priority"],
                "meeting_title": origin["meeting_title"] if origin else row["latest_title"],
                "recording_date": origin["recording_date"] if origin else row["latest_date"],
                "recommended": row["id"] == recommended,
            }
        )
    items.sort(
        key=lambda item: (
            not item["recommended"],
            MERGE_TARGET_STATUSES.index(item["status"]),
            item["priority"],
            item["title"],
        )
    )
    return {"project_id": project_id, "items": items}


# ---------------------------------------------------------------- 认领 / 合并 / 丢掉 / 撤销


def _candidate_row(connection: Any, candidate_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT c.*, m.project_id FROM requirement_candidates c
             JOIN meetings m ON m.id = c.meeting_id
            WHERE c.id = ?""",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"候选不存在：{candidate_id}")
    return dict(row)


def _merge_project(
    connection: Any, candidate: dict[str, Any], project_id: str | None
) -> str | None:
    """合并按哪个项目来：认领页上选的项目优先，不传时取来源会议当前的归属。"""
    if not project_id:
        return candidate["project_id"]
    if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project_id


def _pending_candidate(connection: Any, candidate_id: str) -> dict[str, Any]:
    candidate = _candidate_row(connection, candidate_id)
    if candidate["status"] != "pending":
        raise ConflictError(_HANDLED[candidate["status"]])
    return candidate


def _hand_over(
    connection: Any,
    candidate: dict[str, Any],
    requirement_id: str,
    *,
    merged: bool,
) -> None:
    """候选的来源、会议、任务交给需求，候选记成已认领 / 已合并。

    来源整行改挂：认领时提出它的那句仍是 origin；合并时一律变成 merged，记下合并自哪条候选。"""
    now = utc_now()
    candidate_id = candidate["id"]
    meeting_ids = [
        row["meeting_id"]
        for row in connection.execute(
            "SELECT DISTINCT meeting_id FROM requirement_sources WHERE candidate_id=?",
            (candidate_id,),
        ).fetchall()
    ]
    if merged:
        connection.execute(
            """UPDATE requirement_sources
                  SET requirement_id=?, candidate_id=NULL, kind='merged', via_candidate_title=?
                WHERE candidate_id=?""",
            (requirement_id, candidate["title"], candidate_id),
        )
    else:
        connection.execute(
            "UPDATE requirement_sources SET requirement_id=?, candidate_id=NULL WHERE candidate_id=?",
            (requirement_id, candidate_id),
        )
    for meeting_id in meeting_ids:
        connection.execute(
            """INSERT OR IGNORE INTO requirement_meetings(requirement_id, meeting_id, created_at)
               VALUES (?, ?, ?)""",
            (requirement_id, meeting_id, now),
        )
    tasks = connection.execute(
        "SELECT * FROM tasks WHERE candidate_id=? AND requirement_id IS NULL", (candidate_id,)
    ).fetchall()
    # 只接手候选范围里的任务：任务所属项目就是候选所在的项目，任务没归项目时是同一场会的。候选的项目跟着
    # 它那场会走，挂上以后两边谁改了项目都可能越界；越界的移出候选并留痕，不随认领被拉进别的项目。
    in_scope = []
    for task in tasks:
        if (
            task["project_id"] == candidate["project_id"]
            if task["project_id"] is not None
            else task["meeting_id"] == candidate["meeting_id"]
        ):
            in_scope.append(task)
            continue
        connection.execute(
            """INSERT INTO task_events(task_id, kind, body, created_at)
               VALUES (?, 'requirement_changed', ?, ?)""",
            (task["id"], f"移出候选「{candidate['title']}」：不在候选所在的项目里", now),
        )
    for task in in_scope:
        _requirement_id, project_id, event_body = resolve_requirement_and_project(
            connection,
            dict(task),
            requirement_id_given=True,
            requirement_id=requirement_id,
            project_id_given=False,
            project_id=None,
        )
        connection.execute(
            "UPDATE tasks SET requirement_id=?, project_id=?, updated_at=? WHERE id=?",
            (requirement_id, project_id, now, task["id"]),
        )
        if event_body:
            connection.execute(
                """INSERT INTO task_events(task_id, kind, body, created_at)
                   VALUES (?, 'requirement_changed', ?, ?)""",
                (task["id"], event_body, now),
            )
    connection.execute("UPDATE tasks SET candidate_id=NULL WHERE candidate_id=?", (candidate_id,))
    connection.execute(
        "UPDATE requirement_candidates SET status=?, requirement_id=?, updated_at=? WHERE id=?",
        ("merged" if merged else "claimed", requirement_id, now, candidate_id),
    )


def claim_candidate(
    task_service: TaskService,
    candidate_id: str,
    *,
    title: str,
    summary: str | None = None,
    project_id: str | None = None,
    priority: str = CLAIM_DEFAULT_PRIORITY,
    folder_paths: list[str] | None = None,
) -> dict[str, Any]:
    """认领（S02）：标题、说明、所属项目、优先级可改，建成进行中的需求，来源会议、原话、时间锚一并带入。
    project_id、summary 不传时取候选自己的（项目随来源会议归属、AI 写的说明）；summary 传空串是清空。

    同项目已有同名需求时不新建，抛 RequirementTitleConflict（带那条需求，前端提示改名或合并过去）。"""
    with task_service.db.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        project_id = project_id or candidate["project_id"]
        if not project_id:
            raise ValueError("候选还没归项目，认领前先选所属项目")
        requirement_id = insert_requirement(
            connection,
            project_id=project_id,
            title=title,
            priority=priority,
            folder_paths=folder_paths,
            summary=candidate["summary"] if summary is None else summary,
        )
        _hand_over(connection, candidate, requirement_id, merged=False)
    return get_requirement(task_service, requirement_id)


def _merge_before(connection: Any, candidate_id: str, requirement_id: str) -> dict[str, Any]:
    """合并之前记下候选的原话、目标需求已有的关联会议、候选上的任务（和它们的项目）。"""
    return {
        "sources": [
            dict(row)
            for row in connection.execute(
                """SELECT id, kind, meeting_id, via_candidate_title FROM requirement_sources
                    WHERE candidate_id=? ORDER BY id""",
                (candidate_id,),
            ).fetchall()
        ],
        "linked": {
            row["meeting_id"]
            for row in connection.execute(
                "SELECT meeting_id FROM requirement_meetings WHERE requirement_id=?",
                (requirement_id,),
            ).fetchall()
        },
        "tasks": {
            row["id"]: row["project_id"]
            for row in connection.execute(
                "SELECT id, project_id FROM tasks WHERE candidate_id=? AND requirement_id IS NULL",
                (candidate_id,),
            ).fetchall()
        },
    }


def _merge_record(connection: Any, before: dict[str, Any], requirement_id: str) -> str:
    """这次合并实际带进需求的东西：原话原来的样子、新加的关联会议、真挂过去的任务和它们原来的项目。
    撤销合并只退回这些——交接时没跟过去的任务（不在候选范围里的）不会被撤销挂回候选。"""
    moved = [
        row["id"]
        for row in connection.execute(
            f"""SELECT id FROM tasks
                 WHERE requirement_id=? AND id IN ({", ".join("?" for _ in before["tasks"])})
                 ORDER BY id""",
            (requirement_id, *before["tasks"]),
        ).fetchall()
    ]
    return json.dumps(
        {
            "sources": [
                {"id": s["id"], "kind": s["kind"], "via_candidate_title": s["via_candidate_title"]}
                for s in before["sources"]
            ],
            "meetings": sorted({s["meeting_id"] for s in before["sources"]} - before["linked"]),
            "tasks": [{"id": task_id, "project_id": before["tasks"][task_id]} for task_id in moved],
        },
        ensure_ascii=False,
    )


def merge_candidate(
    task_service: TaskService,
    candidate_id: str,
    requirement_id: str,
    *,
    project_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """合并（S03，或认领撞名后「改为合并到那条需求」）：这场会关联到目标需求、原话追加到它的来源，
    候选消失。目标是所属项目里的需求，三种状态都行；并进已完成的需求，那条重新打开为进行中（D13），
    返回的详情里 reopened 为 true。所属项目是认领页上选的 project_id，不传时取来源会议的归属。
    不改会议归属。合并带进去的东西记在候选上，10 分钟内能撤销。"""
    with task_service.db.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        project_id = _merge_project(connection, candidate, project_id)
        if project_id is None:
            raise ValueError("候选还没归项目，不能合并；先在认领页选所属项目")
        target = connection.execute(
            "SELECT id, project_id, status, status_changed_at FROM requirements WHERE id=?",
            (requirement_id,),
        ).fetchone()
        if target is None:
            raise NotFoundError(f"需求不存在：{requirement_id}")
        if target["project_id"] != project_id:
            raise ValueError("只能合并到所属项目里的需求")
        before = _merge_before(connection, candidate_id, requirement_id)
        _hand_over(connection, candidate, requirement_id, merged=True)
        stamp = (now or datetime.now(UTC)).isoformat()
        record = json.loads(_merge_record(connection, before, requirement_id))
        reopened = target["status"] == "done"
        if reopened:
            # D13：之后的会又提到已完成的需求，并进去就重新打开；撤销这次合并时（状态没被别处再改过）回到已完成
            record.update(
                reopened_from="done",
                reopened_at=stamp,
                previous_status_changed_at=target["status_changed_at"],
            )
            connection.execute(
                "UPDATE requirements SET status='active', status_changed_at=? WHERE id=?",
                (stamp, requirement_id),
            )
        connection.execute(
            "UPDATE requirement_candidates SET merged_at=?, merge_undo=? WHERE id=?",
            (stamp, json.dumps(record, ensure_ascii=False), candidate_id),
        )
        connection.execute(
            "UPDATE requirements SET updated_at=? WHERE id=?", (stamp, requirement_id)
        )
    return {**get_requirement(task_service, requirement_id, now=now), "reopened": reopened}


def unmerge_candidate(
    task_service: TaskService, candidate_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """撤销合并（R01-14）：合并后 10 分钟内，候选回到待认领，这次合并带进需求的原话、关联会议、任务按
    合并时记下的原样退回。原话已经不在那条需求里（比如那场会被移出了关联、原话跟着删了）时不能撤销；
    任务在这 10 分钟里被改挂到别处的不动。合并时把已完成的需求重新打开了的（D13，或别的合并转交过来的），
    按 settle_reopen：并进来的都撤完时回到已完成，期间状态被别处改过的不动。"""
    moment = now or datetime.now(UTC)
    stamp = moment.isoformat()
    with task_service.db.transaction() as connection:
        candidate = _candidate_row(connection, candidate_id)
        if candidate["status"] != "merged" or not candidate["merge_undo"]:
            raise ConflictError("这条候选没有合并，不用撤销")
        if moment - datetime.fromisoformat(candidate["merged_at"]) > timedelta(
            minutes=MERGE_UNDO_MINUTES
        ):
            raise ConflictError(f"合并超过 {MERGE_UNDO_MINUTES} 分钟，不能撤销了")
        record = json.loads(candidate["merge_undo"])
        requirement_id = candidate["requirement_id"]
        restored = 0
        for source in record["sources"]:
            restored += connection.execute(
                """UPDATE requirement_sources
                      SET requirement_id=NULL, candidate_id=?, kind=?, via_candidate_title=?
                    WHERE id=? AND requirement_id=?""",
                (
                    candidate_id,
                    source["kind"],
                    source["via_candidate_title"],
                    source["id"],
                    requirement_id,
                ),
            ).rowcount
        if record["sources"] and not restored:
            raise ConflictError("合并进去的原话已经不在那条需求里了，不能撤销")
        # 只拆这次合并新加的关联，而且那场会已经没有别的原话留在需求里（同一场会后来又合并进一条的，
        # 关联留着、记账交给那一条；拆关联的触发器会连带删掉这场会的原话）
        for meeting_id in record["meetings"]:
            drop_merged_meeting(connection, requirement_id, meeting_id, skip_candidate=candidate_id)
        # 这 10 分钟里被改挂到别处的任务不动，记下几条，提示里说一声（第二轮审查建议 7）
        kept = 0
        for task in record["tasks"]:
            if not connection.execute(
                """UPDATE tasks SET requirement_id=NULL, candidate_id=?, project_id=?, updated_at=?
                    WHERE id=? AND requirement_id=?""",
                (candidate_id, task["project_id"], stamp, task["id"], requirement_id),
            ).rowcount:
                kept += 1
            else:
                connection.execute(
                    """INSERT INTO task_events(task_id, kind, body, created_at)
                       VALUES (?, 'requirement_changed', ?, ?)""",
                    (task["id"], f"撤销合并：回到候选「{candidate['title']}」", stamp),
                )
        settle_reopen(connection, requirement_id, record, stamp, skip_candidate=candidate_id)
        connection.execute(
            """UPDATE requirement_candidates
                  SET status='pending', requirement_id=NULL, merged_at=NULL, merge_undo=NULL,
                      updated_at=?
                WHERE id=?""",
            (stamp, candidate_id),
        )
    return {**get_candidate(task_service.db, candidate_id), "kept_task_count": kept}


def drop_candidate(
    db: Database, candidate_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """丢掉：候选移出墙面，挂在它上面的任务回到未挂需求（记下是哪几条，撤销时挂回去）。30 天内能撤销。"""
    stamp = (now or datetime.now(UTC)).isoformat()
    with db.transaction() as connection:
        candidate = _pending_candidate(connection, candidate_id)
        task_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM tasks WHERE candidate_id=? ORDER BY id", (candidate_id,)
            ).fetchall()
        ]
        # 「同项目丢掉过的不再提示」按丢掉时的项目算：会议后来改了归属，丢掉的名字不跟着搬家
        connection.execute(
            """UPDATE requirement_candidates
                  SET status='dropped', dropped_at=?, dropped_project_id=?, drop_undo=?, updated_at=?
                WHERE id=?""",
            (
                stamp,
                candidate["project_id"],
                json.dumps(task_ids) if task_ids else None,
                stamp,
                candidate_id,
            ),
        )
        connection.execute(
            "UPDATE tasks SET candidate_id=NULL WHERE candidate_id=?", (candidate_id,)
        )
        connection.executemany(
            """INSERT INTO task_events(task_id, kind, body, created_at)
               VALUES (?, 'requirement_changed', ?, ?)""",
            [
                (task_id, f"移出候选「{candidate['title']}」：候选丢掉了", stamp)
                for task_id in task_ids
            ],
        )
    return get_candidate(db, candidate_id)


def restore_candidate(
    db: Database, candidate_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """撤销丢掉：30 天内回到待认领，丢掉时摘下来的任务挂回去，回到丢掉之前的样子（和撤销合并一致）。
    这期间已经被挂到别的需求或候选上的任务不动。

    同项目这期间又有了同名的待认领候选时不撤回（同项目同名的待认领候选只该有一条）：并进那条做不到可逆，
    也会丢掉这条自己的说明和同一场会的第二句原话，所以只提示，这条原样留在「已丢掉」里。"""
    moment = now or datetime.now(UTC)
    stamp = moment.isoformat()
    with db.transaction() as connection:
        candidate = _candidate_row(connection, candidate_id)
        if candidate["status"] != "dropped":
            raise ConflictError("这条候选不在已丢掉里")
        if moment - datetime.fromisoformat(candidate["dropped_at"]) > timedelta(
            days=DROP_UNDO_DAYS
        ):
            raise ConflictError(f"丢掉超过 {DROP_UNDO_DAYS} 天，不能撤销了")
        same_name = _pending_same_name(
            connection, candidate["name_key"], candidate["project_id"], {candidate_id}
        )
        if same_name is not None:
            title = connection.execute(
                "SELECT title FROM requirement_candidates WHERE id=?", (same_name,)
            ).fetchone()["title"]
            raise ConflictError(
                f"同项目里已经有一条同名的待认领候选「{title}」，先认领、合并或丢掉那一条，再撤销这条"
            )
        for task_id in json.loads(candidate["drop_undo"] or "[]"):
            if connection.execute(
                """UPDATE tasks SET candidate_id=?, updated_at=?
                    WHERE id=? AND candidate_id IS NULL AND requirement_id IS NULL""",
                (candidate_id, stamp, task_id),
            ).rowcount:
                connection.execute(
                    """INSERT INTO task_events(task_id, kind, body, created_at)
                       VALUES (?, 'requirement_changed', ?, ?)""",
                    (task_id, f"撤销丢掉：回到候选「{candidate['title']}」", stamp),
                )
        connection.execute(
            """UPDATE requirement_candidates
                  SET status='pending', dropped_at=NULL, dropped_project_id=NULL, drop_undo=NULL,
                      updated_at=?
                WHERE id=?""",
            (stamp, candidate_id),
        )
    return get_candidate(db, candidate_id)
