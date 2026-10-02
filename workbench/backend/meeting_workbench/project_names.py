"""项目名字与项目整理（第一期 1b-2）。

- 近似重名：新建项目时名字和已有项目的正式名或叫法归一化后相同，或者相近（names_similar：
  较短的一方 3 字以上，原样出现在较长一方里，或只是中间少了连着的一段字），就先问「是不是它」，
  带 force 才仍然新建。
- 也叫：2–20 字、不能纯数字、不能是通用词、一个叫法只能指向一个项目；改名时旧名
  自动进也叫（source=former）。
- 新项目建好后，在「没认出」的会里按名字找，命中的变成「待你选」，不调模型、不静默归属。
- 忽略名字、合并项目、删除空项目。
- 新建项目时可以直接挂上或新建同名文件夹（找同名文件夹在 project_folders，读资料盘缓存）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .db import utc_now
from .materials import (
    CARDS_DIR_NAME,
    ROOT_ONLINE,
    ROOT_VOLUME_OFFLINE,
    _protected_roots,
    _within,
    resolve_within,
    volume_state,
)
from .project_profile import (
    GENERIC_FOLDER_NAMES,
    Cue,
    _fts_phrase,
    count_cues,
    norm_key,
)
from .service import ConflictError, NotFoundError

ALSO_MIN_LENGTH = 2
ALSO_MAX_LENGTH = 20
ALSO_SOURCES = ("manual", "former", "merged", "spoken")
# 新项目回扫时，也叫要 3 字以上才拿来找（2 字的太容易撞车）。
RESCAN_MIN_ALSO_LENGTH = 3
# exFAT 不允许出现在文件名里的字符。
_EXFAT_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_HAS_WORD_CHAR = re.compile(r"[A-Za-z㐀-鿿]")


class SimilarProjectError(ConflictError):
    """近似重名：带上已有项目，前端问「是不是它？」。"""

    def __init__(self, message: str, suggestion: dict[str, Any]):
        super().__init__(message)
        self.suggestion = suggestion


# ---------------------------------------------------------------------- 也叫


def also_entries(raw: Any) -> list[dict[str, str]]:
    """projects.also_names 解析成 [{"name", "source"}]（兼容旧的纯字符串元素）。"""
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except json.JSONDecodeError:
        return []
    entries: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            name, source = item, "manual"
        elif isinstance(item, dict):
            name, source = item.get("name"), item.get("source") or "manual"
        else:
            continue
        if isinstance(name, str) and name.strip():
            entries.append(
                {"name": name.strip(), "source": source if source in ALSO_SOURCES else "manual"}
            )
    return entries


def _dump_also(entries: list[dict[str, str]]) -> str:
    return json.dumps(entries, ensure_ascii=False)


def _name_owner(
    connection: Any, key: str, *, exclude_project_id: str | None = None
) -> tuple[dict[str, Any], str] | None:
    """哪个项目的正式名或叫法归一化后等于 key；返回 (项目行, "name"|"also")。"""
    for row in connection.execute("SELECT id, name, also_names FROM projects").fetchall():
        if row["id"] == exclude_project_id:
            continue
        if norm_key(row["name"]) == key:
            return dict(row), "name"
        if any(norm_key(entry["name"]) == key for entry in also_entries(row["also_names"])):
            return dict(row), "also"
    return None


def validate_also_name(
    connection: Any, name: str, *, project_id: str | None, project_name: str
) -> str:
    text = (name or "").strip()
    if (
        not ALSO_MIN_LENGTH <= len(text) <= ALSO_MAX_LENGTH
        or text.isdigit()
        or not _HAS_WORD_CHAR.search(text)
    ):
        raise ValueError("叫法要 2–20 个字，不能是纯数字")
    if text in GENERIC_FOLDER_NAMES:
        raise ValueError(f"「{text}」太常见，不能当叫法")
    key = norm_key(text)
    owner = _name_owner(connection, key, exclude_project_id=project_id)
    if owner is not None:
        project, kind = owner
        if kind == "name":
            raise ConflictError(
                f"「{text}」已经是项目「{project['name']}」的名称，一个叫法只能指向一个项目"
            )
        raise ConflictError(
            f"「{text}」已经是「{project['name']}」的叫法，一个叫法只能指向一个项目"
        )
    return text


def merge_also_names(
    connection: Any,
    project_id: str,
    project_name: str,
    names: list[str],
    current: list[dict[str, str]],
) -> list[dict[str, str]]:
    """PATCH 传来的叫法列表 → 新的 also_names；已有的保留原来的 source，新加的校验后记 manual。"""
    by_key = {norm_key(entry["name"]): entry for entry in current}
    own_key = norm_key(project_name)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in names:
        text = (raw or "").strip()
        key = norm_key(text)
        if not text or key in seen or key == own_key:
            continue
        existing = by_key.get(key)
        if existing is not None and existing["name"] == text:
            result.append(existing)
        else:
            text = validate_also_name(
                connection, text, project_id=project_id, project_name=project_name
            )
            result.append({"name": text, "source": existing["source"] if existing else "manual"})
        seen.add(key)
    return result


def add_former_name(
    entries: list[dict[str, str]], old_name: str, new_name: str, *, source: str = "former"
) -> list[dict[str, str]]:
    """改名或合并：旧名进也叫；新名如果原来是一个叫法，就从也叫里拿掉。"""
    new_key = norm_key(new_name)
    result = [entry for entry in entries if norm_key(entry["name"]) != new_key]
    old_key = norm_key(old_name)
    if (
        old_key
        and old_key != new_key
        and all(norm_key(entry["name"]) != old_key for entry in result)
    ):
        result.append({"name": old_name, "source": source})
    return result


# ---------------------------------------------------------------------- 近似重名


def names_similar(key_a: str, key_b: str) -> bool:
    """两个名字（都是 norm_key）算不算「相近」：新建项目问「是不是它」、新项目回扫、同名文件夹三处共用。

    较短一方至少 3 个字，并且满足下面任一条：
    - 原样出现在较长一方里，多出来的字只在开头或结尾（「云图科研」和「云图科研用药」）；
    - 较长一方只是在较短一方中间多了一段连着的字，头尾都对得上（缩写、少写或多写一两个字：
      「云图用药」和「云图科研用药」）。
    以前只要求较短一方是较长一方的子序列，字散在几处也算：「智慧医院」会被当成「智慧医疗院区」
    的相近名（中间多了「疗」、结尾又多了「区」），只是多问一句，但问得太多。完全相同的是「同名」，
    不是相近，各处在这之前先判。"""
    short, long = sorted((key_a, key_b), key=len)
    if len(short) < 3 or short == long:
        return False
    if short in long:
        return True
    return any(
        long.startswith(short[:split]) and long.endswith(short[split:])
        for split in range(1, len(short))
    )


def find_similar_project(
    connection: Any, name: str, *, exclude_project_id: str | None = None
) -> dict[str, Any] | None:
    """近似重名：先找归一化后完全相同的，再找子序列相近的（较短一方 ≥3 字）。"""
    key = norm_key(name)
    if not key:
        return None
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT id, name, also_names FROM projects ORDER BY created_at, id"
        ).fetchall()
        if row["id"] != exclude_project_id
    ]
    for match in ("same", "similar"):
        for row in rows:
            also = also_entries(row["also_names"])
            for candidate in [row["name"], *(entry["name"] for entry in also)]:
                other = norm_key(candidate)
                if not other:
                    continue
                hit = other == key if match == "same" else names_similar(key, other)
                if hit:
                    return {
                        "id": row["id"],
                        "project_id": row["id"],
                        "name": row["name"],
                        "also_names": [entry["name"] for entry in also],
                        "matched": candidate,
                        "match": match,
                    }
    return None


def exact_project_suggestion(connection: Any, name: str) -> dict[str, Any] | None:
    """正式名完全相同的已有项目，格式和 find_similar_project 一样，另带 exact=True。"""
    row = connection.execute(
        "SELECT id, name, also_names FROM projects WHERE name=?", (name.strip(),)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "project_id": row["id"],
        "name": row["name"],
        "also_names": [entry["name"] for entry in also_entries(row["also_names"])],
        "matched": row["name"],
        "match": "same",
        "exact": True,
    }


def similar_project_message(suggestion: dict[str, Any]) -> str:
    also = suggestion.get("also_names") or []
    tail = f"（又称 {'、'.join(also[:3])}）" if also else ""
    return f"已有「{suggestion['name']}」{tail}，是不是它？"


# ---------------------------------------------------------------------- 新项目回扫


def rescan_unresolved_for_project(connection: Any, project_id: str) -> list[str]:
    """新项目建好后，在「没认出」的会里按它的名字和 3 字以上的叫法找；命中的改成待你选。

    查标题、纪要全文索引、当前逐字稿全文索引，外加 AI 之前提过的同名新项目。
    不调模型，不直接归属。返回改成待你选的会议 id。
    """
    project = connection.execute(
        "SELECT id, name, also_names FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if project is None:
        return []
    needles = [project["name"].strip()]
    needles += [
        entry["name"]
        for entry in also_entries(project["also_names"])
        if len(entry["name"]) >= RESCAN_MIN_ALSO_LENGTH
    ]
    needles = [needle for needle in dict.fromkeys(needles) if len(needle) >= 2]
    project_key = norm_key(project["name"])
    rows = connection.execute(
        """SELECT m.id, m.title, m.current_minutes_version_id, m.current_transcript_version_id,
                  pl.id AS link_id, pl.new_project_name
             FROM meetings m
             JOIN project_links pl ON pl.id = (
                  SELECT MAX(latest.id) FROM project_links latest WHERE latest.meeting_id = m.id)
            WHERE m.project_id IS NULL AND m.project_origin IS NULL
              AND pl.status = 'unresolved'"""
    ).fetchall()
    if not rows:
        return []
    candidates = {row["id"]: dict(row) for row in rows}

    prefiltered: set[str] = set()
    # AI 之前提过的新项目名和这个项目同名或相近：即使会里没原词也问一句。
    named: set[str] = set()
    for meeting in candidates.values():
        title = (meeting["title"] or "").casefold()
        if any(needle.casefold() in title for needle in needles):
            prefiltered.add(meeting["id"])
        name_key = norm_key(meeting["new_project_name"] or "")
        if name_key and project_key:
            if name_key == project_key or names_similar(name_key, project_key):
                named.add(meeting["id"])
    for needle in needles:
        if len(needle) >= 3:
            phrase = _fts_phrase(needle)
            for row in connection.execute(
                "SELECT DISTINCT meeting_id FROM minutes_fts WHERE minutes_fts MATCH ?", (phrase,)
            ).fetchall():
                prefiltered.add(row["meeting_id"])
            for row in connection.execute(
                """SELECT DISTINCT f.meeting_id FROM segments_fts f
                     JOIN meetings m ON m.id = f.meeting_id
                                    AND f.version_id = m.current_transcript_version_id
                    WHERE segments_fts MATCH ?""",
                (phrase,),
            ).fetchall():
                prefiltered.add(row["meeting_id"])
        else:
            # 两个字的名字全文索引（trigram）查不了，退回纪要原文 LIKE，下面再按 ≥2 次核对。
            for row in connection.execute(
                """SELECT m.id FROM meetings m
                     JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                    WHERE instr(mv.markdown, ?) > 0""",
                (needle,),
            ).fetchall():
                prefiltered.add(row["id"])

    cue_table = {
        project_id: [
            Cue(project_id, needle, "name" if index == 0 else "also")
            for index, needle in enumerate(needles)
        ]
    }
    flagged: list[str] = []
    now = utc_now()
    for meeting_id in sorted((prefiltered | named) & set(candidates)):
        meeting = candidates[meeting_id]
        minutes = connection.execute(
            "SELECT markdown FROM minutes_versions WHERE id=?",
            (meeting["current_minutes_version_id"],),
        ).fetchone()
        segments = (
            [
                dict(row)
                for row in connection.execute(
                    "SELECT start_ms, text FROM segments WHERE version_id=? ORDER BY ordinal",
                    (meeting["current_transcript_version_id"],),
                ).fetchall()
            ]
            if meeting["current_transcript_version_id"]
            else []
        )
        hits = count_cues(
            cue_table,
            title=meeting["title"] or "",
            segments=segments,
            minutes=minutes["markdown"] if minutes else "",
        ).get(project_id)
        if hits is None and meeting_id not in named:
            continue
        count = hits.count if hits else 0
        evidence = hits.evidence if hits else []
        reason = (
            f"新建了项目「{project['name']}」，这场会提到它 {count} 次"
            if count
            else f"新建了项目「{project['name']}」，AI 之前觉得这场会像新项目「{meeting['new_project_name']}」"
        )
        candidates_json = json.dumps(
            [
                {
                    "project_id": project_id,
                    "project_name": project["name"],
                    "count": count,
                    "llm": False,
                }
            ],
            ensure_ascii=False,
        )
        connection.execute(
            """UPDATE project_links
                  SET status='needs_review', method='new_project_rescan', project_id=NULL,
                      candidates_json=?, evidence_json=?, reason=?, new_project_name=NULL,
                      finished_at=?
                WHERE id=?""",
            (
                candidates_json,
                json.dumps(evidence, ensure_ascii=False),
                reason,
                now,
                meeting["link_id"],
            ),
        )
        connection.execute(
            """INSERT INTO events (meeting_id, job_id, event_type, actor, payload_json, created_at)
               VALUES (?, NULL, 'meeting_project_needs_review', 'system', ?, ?)""",
            (
                meeting_id,
                json.dumps(
                    {
                        "reason": reason,
                        "candidates": json.loads(candidates_json),
                        "trigger": "new_project",
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        flagged.append(meeting_id)
    return flagged


# ---------------------------------------------------------------------- 合并与删除


def _rewrite_project_ids_in_json(connection: Any, column: str, src: str, dst: str) -> None:
    # 项目 id 是随机串，直接替换 JSON 里的出现即可。
    connection.execute(
        f"UPDATE project_links SET {column}=replace({column}, ?, ?) WHERE instr({column}, ?) > 0",
        (json.dumps(src), json.dumps(dst), json.dumps(src)),
    )


def merge_project(connection: Any, src_id: str, dst_id: str) -> dict[str, Any]:
    """把 src 并进 dst：会议（保留来源）、任务、需求、词条、材料根目录、归属批次、名字决定
    都搬过去，src 的名字和叫法进 dst 的也叫，最后删掉 src。调用方负责开事务。"""
    if src_id == dst_id:
        raise ValueError("不能合并到自己")
    src = connection.execute("SELECT * FROM projects WHERE id=?", (src_id,)).fetchone()
    dst = connection.execute("SELECT * FROM projects WHERE id=?", (dst_id,)).fetchone()
    if src is None or dst is None:
        raise NotFoundError("项目不存在")
    now = utc_now()
    # v16：关联、字面提到和候选词先搬到 dst，会议改项目时两个离开项目的触发器就不删它们（你的回答、
    # 「不是这份文件」和换过的 picked=1 都留得住）。relations 经 tasks 引用本模块，这里晚一点再引。
    from .relations import repoint_project

    repoint_project(connection, src_id, dst_id, now)
    meetings = connection.execute(
        "UPDATE meetings SET project_id=?, updated_at=? WHERE project_id=?", (dst_id, now, src_id)
    ).rowcount
    tasks = connection.execute(
        "UPDATE tasks SET project_id=?, updated_at=? WHERE project_id=?", (dst_id, now, src_id)
    ).rowcount
    dst_titles = {
        row["title"].strip().casefold()
        for row in connection.execute(
            "SELECT title FROM requirements WHERE project_id=?", (dst_id,)
        ).fetchall()
    }
    renamed_requirements: list[dict[str, str]] = []
    for row in connection.execute(
        "SELECT id, title FROM requirements WHERE project_id=? ORDER BY created_at, id", (src_id,)
    ).fetchall():
        title = row["title"]
        if title.strip().casefold() in dst_titles:
            title = f"{row['title']}（原 {src['name']}）"
            renamed_requirements.append({"id": row["id"], "from": row["title"], "to": title})
        dst_titles.add(title.strip().casefold())
        connection.execute(
            "UPDATE requirements SET project_id=?, title=?, updated_at=? WHERE id=?",
            (dst_id, title, now, row["id"]),
        )
    terms = connection.execute(
        "UPDATE glossary_terms SET project_id=?, scope=?, updated_at=? WHERE project_id=?",
        (dst_id, dst["name"], now, src_id),
    ).rowcount
    dst_paths = {
        row["path"]
        for row in connection.execute(
            "SELECT path FROM project_material_roots WHERE project_id=?", (dst_id,)
        ).fetchall()
    }
    for row in connection.execute(
        "SELECT id, path FROM project_material_roots WHERE project_id=?", (src_id,)
    ).fetchall():
        if row["path"] in dst_paths:
            connection.execute("DELETE FROM project_material_roots WHERE id=?", (row["id"],))
        else:
            connection.execute(
                "UPDATE project_material_roots SET project_id=? WHERE id=?", (dst_id, row["id"])
            )
    connection.execute("UPDATE project_links SET project_id=? WHERE project_id=?", (dst_id, src_id))
    for column in ("candidates_json", "evidence_json"):
        _rewrite_project_ids_in_json(connection, column, src_id, dst_id)
    connection.execute("UPDATE name_decisions SET target_id=? WHERE target_id=?", (dst_id, src_id))
    also = also_entries(dst["also_names"])
    for entry in [{"name": src["name"], "source": "merged"}, *also_entries(src["also_names"])]:
        also = add_former_name(also, entry["name"], dst["name"], source=entry["source"])
    connection.execute(
        "UPDATE projects SET also_names=?, origin='manual' WHERE id=?", (_dump_also(also), dst_id)
    )
    connection.execute("DELETE FROM projects WHERE id=?", (src_id,))
    # v17：dst 没排座次时接过 src 的座次（需求池「我的方向」），删掉 src 之后再写，避开座次唯一索引。
    if src["seat"] is not None and dst["seat"] is None:
        connection.execute("UPDATE projects SET seat=? WHERE id=?", (src["seat"], dst_id))
    return {
        "meetings_moved": meetings,
        "tasks_moved": tasks,
        "terms_moved": terms,
        "renamed_requirements": renamed_requirements,
    }


def delete_empty_project(connection: Any, project_id: str) -> dict[str, Any]:
    """只允许删没有会议、没有需求的项目；它的任务变成未归项目，项目词回到公共词典。"""
    project = connection.execute(
        "SELECT id, name FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if project is None:
        raise NotFoundError("项目不存在")
    meetings = connection.execute(
        "SELECT COUNT(*) AS n FROM meetings WHERE project_id=?", (project_id,)
    ).fetchone()["n"]
    requirements = connection.execute(
        "SELECT COUNT(*) AS n FROM requirements WHERE project_id=?", (project_id,)
    ).fetchone()["n"]
    if meetings or requirements:
        parts = []
        if meetings:
            parts.append(f"{meetings} 场会")
        if requirements:
            parts.append(f"{requirements} 个需求")
        raise ConflictError(f"项目下还有{'、'.join(parts)}，请先合并到别的项目")
    now = utc_now()
    tasks = connection.execute(
        "UPDATE tasks SET project_id=NULL, updated_at=? WHERE project_id=?", (now, project_id)
    ).rowcount
    terms = connection.execute(
        "UPDATE glossary_terms SET project_id=NULL, scope='通用', updated_at=? WHERE project_id=?",
        (now, project_id),
    ).rowcount
    connection.execute("UPDATE project_links SET project_id=NULL WHERE project_id=?", (project_id,))
    # 「建成了这个项目」的名字决定一并删掉，这个名字以后还能被提出来。
    connection.execute(
        "DELETE FROM name_decisions WHERE decision='project' AND target_id=?", (project_id,)
    )
    connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"tasks_unassigned": tasks, "terms_to_public": terms}


# ---------------------------------------------------------------------- 同名文件夹


def sanitize_folder_name(name: str) -> tuple[str, list[str]]:
    """新建文件夹用的名字：exFAT 不认的字符换成 -，去掉结尾的点和空格；返回 (名字, 被换掉的字符)。"""
    replaced = sorted(set(_EXFAT_ILLEGAL.findall(name or "")))
    cleaned = _EXFAT_ILLEGAL.sub("-", (name or "").strip()).rstrip(". ")
    return cleaned, replaced


def _skipped_folder(path: Path, settings: Settings) -> bool:
    if path.name.startswith(".") or path.name == CARDS_DIR_NAME:
        return True
    if _within(path, Path.home() / "Library"):
        return True
    return any(_within(path, root) for root, _message in _protected_roots(settings))


def _match_kind(folder_name: str, names: list[str]) -> str | None:
    folder_key = norm_key(folder_name)
    if not folder_key:
        return None
    best: str | None = None
    for name in names:
        key = norm_key(name)
        if not key:
            continue
        if key == folder_key:
            return "exact"
        if names_similar(key, folder_key):
            best = "similar"
    return best


def create_project_folder(settings: Settings, parent_raw: str, name: str) -> tuple[str, str | None]:
    """在 parent 下新建项目文件夹（只建这一层）。返回 (文件夹路径, 没建成的原因)；
    父目录所在的盘没插时不建，原因里写明，项目照常新建。"""
    folder_name, _replaced = sanitize_folder_name(name)
    if not folder_name:
        raise ValueError("文件夹名不能为空")
    parent = resolve_within(settings.material_browse_root, parent_raw)
    target = parent / folder_name
    state = volume_state(parent)
    if state == ROOT_VOLUME_OFFLINE:
        return str(target), "资料盘未连接，插上后再建文件夹"
    if state != ROOT_ONLINE:
        raise ValueError("要放新文件夹的位置不存在")
    for protected, message in _protected_roots(settings):
        if _within(target, protected):
            raise ValueError(message)
    target.mkdir(parents=False, exist_ok=True)
    return str(target.resolve()), None
