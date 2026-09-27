"""项目画像：认出一场会属于哪个项目用到的字面线索（第一期 1b）。

每个项目的线索来自四处：正式名、「也叫」、材料根目录的文件夹名、参与识别的项目词
（is_cue=1 的词条及其错写、也叫）。线索在会议标题、当前逐字稿、当前纪要里计次，
命中的段落时间点记进证据，界面上点一下就能跳到原话。

线索是纯字面匹配，不调模型：没配 LLM key 时靠它兜底，配了 key 时用来核对模型的结论
（模型说是 A，而 B 的线索明显更多，就不自动归属，交给用户选）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 文件夹名太常见时不能当线索（「资料」「项目」这类名字到处都是）。
GENERIC_FOLDER_NAMES = frozenset(
    {"资料", "项目", "文档", "归档", "材料", "工作", "新建文件夹", "备份", "下载", "会议", "方案"}
)
# 文件夹名和项目词如果在这么多个其他项目的已归属会议里出现过，就算泛词，不当线索。
GENERIC_PROJECT_SPREAD = 2
# 每条线索最多记几个命中段的时间点。
MAX_ANCHORS = 5

_TRAILING_SUFFIX = re.compile(r"(项目|[一二三四五六七八九十\d]+期|v\d+(?:\.\d+)*)$")


def norm_key(name: str) -> str:
    """名字归一化：NFKC、casefold、去空白标点，再去掉结尾的「项目」「一/二/三期」「v数字」。

    用于判断两个项目名是不是同一个（「云图AI项目」「云图 AI」「云图ai 二期」都归到「云图ai」）。
    """
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = "".join(
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith(("P", "S"))
    )
    previous = None
    while text and text != previous:
        previous = text
        stripped = _TRAILING_SUFFIX.sub("", text)
        text = stripped if stripped else text
    return text


def is_subsequence(short: str, long: str) -> bool:
    iterator = iter(long)
    return all(char in iterator for char in short)


@dataclass(frozen=True)
class Cue:
    project_id: str
    text: str
    kind: str  # name | also | folder | term
    term_id: str | None = None

    @property
    def min_count(self) -> int:
        # 两个字的名字容易撞车，一场会里出现 2 次以上才算数。
        return 2 if len(self.text) <= 2 else 1


@dataclass
class ProjectHits:
    project_id: str
    count: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)


def _json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def also_name_list(raw: Any) -> list[str]:
    """projects.also_names 的名字列表（元素是 {"name", "source"}，兼容纯字符串）。"""
    names: list[str] = []
    for item in _json_list(raw):
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _spread_to_other_projects(connection: Any, text: str, project_id: str) -> int:
    """这个词在多少个「其他项目」的已归属会议（标题或当前纪要）里出现过。"""
    projects: set[str] = set()
    if len(text) >= 3:
        for row in connection.execute(
            """SELECT DISTINCT m.project_id
                 FROM minutes_fts f JOIN meetings m ON m.id = f.meeting_id
                WHERE minutes_fts MATCH ? AND m.project_id IS NOT NULL AND m.project_id != ?""",
            (_fts_phrase(text), project_id),
        ).fetchall():
            projects.add(row["project_id"])
    for row in connection.execute(
        """SELECT DISTINCT project_id FROM meetings
            WHERE project_id IS NOT NULL AND project_id != ? AND instr(lower(title), lower(?)) > 0""",
        (project_id, text),
    ).fetchall():
        projects.add(row["project_id"])
    return len(projects)


def build_cue_table(connection: Any) -> dict[str, list[Cue]]:
    """算出每个项目的线索表。调用方可以在一轮批处理里复用结果。"""
    table: dict[str, list[Cue]] = {}
    seen: dict[str, set[str]] = {}

    def add(cue: Cue) -> None:
        key = cue.text.casefold()
        bucket = seen.setdefault(cue.project_id, set())
        if key in bucket:
            return
        bucket.add(key)
        table.setdefault(cue.project_id, []).append(cue)

    for row in connection.execute("SELECT id, name, also_names FROM projects").fetchall():
        table.setdefault(row["id"], [])
        seen.setdefault(row["id"], set())
        if len(row["name"].strip()) >= 2:
            add(Cue(row["id"], row["name"].strip(), "name"))
        for name in also_name_list(row["also_names"]):
            if len(name) >= 2:
                add(Cue(row["id"], name, "also"))

    candidates: list[Cue] = []
    for row in connection.execute(
        "SELECT project_id, path FROM project_material_roots ORDER BY created_at, id"
    ).fetchall():
        name = Path(row["path"]).name.strip()
        if len(name) >= 3 and name not in GENERIC_FOLDER_NAMES:
            candidates.append(Cue(row["project_id"], name, "folder"))
    for row in connection.execute(
        """SELECT id, term, aliases, also, project_id FROM glossary_terms
            WHERE project_id IS NOT NULL AND is_cue=1 AND confirmed=1"""
    ).fetchall():
        for text in (row["term"], *_json_list(row["aliases"]), *_json_list(row["also"])):
            if isinstance(text, str) and len(text.strip()) >= 3:
                candidates.append(Cue(row["project_id"], text.strip(), "term", row["id"]))

    spread_cache: dict[tuple[str, str], int] = {}
    for cue in candidates:
        if cue.project_id not in table:
            continue
        key = (cue.text.casefold(), cue.project_id)
        if key not in spread_cache:
            spread_cache[key] = _spread_to_other_projects(connection, cue.text, cue.project_id)
        if spread_cache[key] >= GENERIC_PROJECT_SPREAD:
            continue
        add(cue)
    return table


def count_cues(
    cue_table: dict[str, list[Cue]],
    *,
    title: str,
    segments: list[dict[str, Any]],
    minutes: str,
) -> dict[str, ProjectHits]:
    """在标题、逐字稿、纪要里给每条线索计次；只返回至少有一条线索达标的项目。"""
    folded_segments = [
        (int(segment.get("start_ms") or 0), str(segment.get("text") or "").casefold())
        for segment in segments
    ]
    folded_title = (title or "").casefold()
    folded_minutes = (minutes or "").casefold()
    results: dict[str, ProjectHits] = {}
    for project_id, cues in cue_table.items():
        for cue in cues:
            needle = cue.text.casefold()
            in_title = folded_title.count(needle)
            in_minutes = folded_minutes.count(needle)
            in_transcript = 0
            anchors: list[int] = []
            for start_ms, text in folded_segments:
                hits = text.count(needle)
                if hits:
                    in_transcript += hits
                    if len(anchors) < MAX_ANCHORS:
                        anchors.append(start_ms)
            total = in_title + in_transcript + in_minutes
            if total < cue.min_count:
                continue
            hits_for_project = results.setdefault(project_id, ProjectHits(project_id))
            hits_for_project.count += total
            entry: dict[str, Any] = {
                "kind": "literal",
                "project_id": project_id,
                "cue": cue.text,
                "source": cue.kind,
                "count": total,
                "anchors_ms": anchors,
                "where": {"title": in_title, "transcript": in_transcript, "minutes": in_minutes},
            }
            if cue.term_id:
                entry["term_id"] = cue.term_id
            hits_for_project.evidence.append(entry)
    for hits_for_project in results.values():
        hits_for_project.evidence.sort(key=lambda item: item["count"], reverse=True)
    return results
