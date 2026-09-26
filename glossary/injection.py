# -*- coding: utf-8 -*-
"""按这场会挑词（标准库 only）：relay 出纪要前调用，工作台体检时复用同一套规则。

词典快照里有公共词和各项目的词。整份塞进 prompt 有两个问题：别的项目的词会诱导模型
把普通词「纠」成无关的专有名词；词多了还会超出 50 条注入上限。所以每场会只挑：

1. 先定项目。工作台知道这场会属于哪个项目时（重新生成纪要）直接用；不知道时（新录音）
   拿各项目的识别线索（名字、也叫、文件夹名、参与识别的项目词）在逐字稿里计次，只取
   得分最高且领先的一个。两个字的线索要出现 2 次才算数，和工作台认项目是同一条规矩。
   平分或得分不够就不选项目，只用公共词。
2. 再分三层挑词：A 层是错写出现在逐字稿里的，B 层是正确写法（或也叫）出现的，C 层是
   都没出现的。同一层里项目词在前，再按快照原来的纠错价值排序。50 条上限只截 C 层。
3. 公共词的某个错写如果正好是本项目的词条或也叫（比如公共词「数理」把「树立」当错写，
   而本项目恰好有个词条叫「树立」），这一场就不用这个错写，免得把项目的正确说法改掉。

结果同时是写给模型的对照表和留给工作台的回执（glossary-injection.json）。
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INJECTION_SCHEMA_VERSION = 1
INJECTION_FILENAME = "glossary-injection.json"
INJECTION_LIMIT = 50
PUBLIC_SCOPE = "通用"
# 选项目的最低得分：一条 3 字以上线索出现 2 次，或两条线索各出现 1 次。
MIN_PROJECT_SCORE = 2
RANKING_KEEP = 3


def fold(text: str) -> str:
    """比对用：NFKC + casefold，全角半角、大小写不影响命中。"""
    return unicodedata.normalize("NFKC", text or "").casefold()


def load_snapshot(path: Path | str) -> dict[str, Any] | None:
    """读快照；文件不在、不是 JSON、版本不对都返回 None，调用方照常出纪要。"""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    if not isinstance(data.get("terms"), list):
        return None
    return data


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _snapshot_terms(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    for raw in snapshot.get("terms") or []:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term") or "").strip()
        if not term:
            continue
        project_id = raw.get("project_id")
        terms.append(
            {
                "term": term,
                "aliases": _strings(raw.get("aliases")),
                "also": _strings(raw.get("also")),
                "category": raw.get("category") or "其他",
                "scope": raw.get("scope") or PUBLIC_SCOPE,
                "project_id": project_id if isinstance(project_id, str) and project_id else None,
            }
        )
    return terms


def _snapshot_projects(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for raw in snapshot.get("projects") or []:
        if not isinstance(raw, dict):
            continue
        project_id = raw.get("id")
        name = str(raw.get("name") or "").strip()
        if not isinstance(project_id, str) or not project_id or not name:
            continue
        cues: list[str] = []
        for cue in raw.get("cues") or []:
            text = cue.get("text") if isinstance(cue, dict) else cue
            if isinstance(text, str) and len(text.strip()) >= 2:
                cues.append(text.strip())
        projects.append(
            {"id": project_id, "name": name, "also": _strings(raw.get("also")), "cues": cues}
        )
    return projects


def score_projects(projects: list[dict[str, Any]], transcript: str) -> list[dict[str, Any]]:
    """各项目的线索在逐字稿里计次，只返回有得分的项目，高分在前。"""
    folded = fold(transcript)
    ranking: list[dict[str, Any]] = []
    for project in projects:
        score = 0
        matched: list[str] = []
        seen: set[str] = set()
        for cue in project["cues"]:
            needle = fold(cue)
            if needle in seen:
                continue
            seen.add(needle)
            count = folded.count(needle)
            # 两个字的名字容易撞车，一场会里出现 2 次以上才算数（同 project_profile.Cue）。
            if count < (2 if len(needle) <= 2 else 1):
                continue
            score += count
            matched.append(cue)
        if score:
            ranking.append(
                {"id": project["id"], "name": project["name"], "score": score, "cues": matched}
            )
    ranking.sort(key=lambda item: (-item["score"], item["name"]))
    return ranking


def _resolve_hint(
    hint: str, projects: list[dict[str, Any]], terms: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """项目提示可以是 id，也可以是名字或也叫（改名后旧提示照样认得）。"""
    text = hint.strip()
    for project in projects:
        if project["id"] == text:
            return project
    key = "".join(fold(text).split())
    for project in projects:
        names = [project["name"], *project["also"]]
        if any("".join(fold(name).split()) == key for name in names):
            return project
    if any(term["project_id"] == text for term in terms):
        # 快照里还没有这个项目的名字（旧快照），词条认得就照用。
        return {"id": text, "name": None, "also": [], "cues": []}
    return None


def select_injection(
    snapshot: dict[str, Any] | None,
    transcript: str,
    project_hint: str | None = None,
    *,
    limit: int = INJECTION_LIMIT,
) -> dict[str, Any]:
    """挑出这场会要交给模型的词，返回回执（可直接写成 glossary-injection.json）。"""
    receipt: dict[str, Any] = {
        "schema_version": INJECTION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_updated_at": None,
        "snapshot_missing": snapshot is None,
        "project_hint": project_hint or None,
        "project": None,
        "ranking": [],
        "limit": limit,
        "counts": {
            "selected": 0,
            "project": 0,
            "public": 0,
            "tier_a": 0,
            "tier_b": 0,
            "tier_c": 0,
            "tier_c_dropped": 0,
        },
        "dropped_aliases": [],
        "terms": [],
    }
    if snapshot is None:
        return receipt
    receipt["snapshot_updated_at"] = snapshot.get("updated_at")
    terms = _snapshot_terms(snapshot)
    projects = _snapshot_projects(snapshot)

    project: dict[str, Any] | None = None
    if project_hint and project_hint.strip():
        project = _resolve_hint(project_hint, projects, terms)
        if project is not None:
            receipt["project"] = {
                "id": project["id"],
                "name": project["name"],
                "source": "hint",
                "score": None,
            }
    else:
        ranking = score_projects(projects, transcript)
        receipt["ranking"] = [
            {"id": item["id"], "name": item["name"], "score": item["score"]}
            for item in ranking[:RANKING_KEEP]
        ]
        if ranking:
            top = ranking[0]
            leads = len(ranking) == 1 or top["score"] > ranking[1]["score"]
            if top["score"] >= MIN_PROJECT_SCORE and leads:
                project = next(item for item in projects if item["id"] == top["id"])
                receipt["project"] = {
                    "id": top["id"],
                    "name": top["name"],
                    "source": "transcript",
                    "score": top["score"],
                    "cues": top["cues"],
                }

    project_id = project["id"] if project else None
    project_terms = [term for term in terms if project_id and term["project_id"] == project_id]
    public_terms = [
        term for term in terms if term["project_id"] is None and term["scope"] == PUBLIC_SCOPE
    ]
    protected = {
        fold(text) for term in project_terms for text in (term["term"], *term["also"])
    }

    folded = fold(transcript)
    tiers: dict[str, list[dict[str, Any]]] = {"A": [], "B": [], "C": []}
    for term in [*project_terms, *public_terms]:
        aliases = term["aliases"]
        if term["project_id"] is None and protected:
            kept = [alias for alias in aliases if fold(alias) not in protected]
            for alias in aliases:
                if alias not in kept:
                    receipt["dropped_aliases"].append({"term": term["term"], "alias": alias})
            aliases = kept
        wrong_hits = sum(folded.count(fold(alias)) for alias in aliases)
        right_hits = sum(folded.count(fold(text)) for text in (term["term"], *term["also"]))
        tier = "A" if wrong_hits else "B" if right_hits else "C"
        entry: dict[str, Any] = {
            "term": term["term"],
            "aliases": aliases,
            "category": term["category"],
            "tier": tier,
            "transcript_hits": {"wrong": wrong_hits, "correct": right_hits},
        }
        if term["also"]:
            entry["also"] = term["also"]
        if term["project_id"]:
            entry["project_id"] = term["project_id"]
        tiers[tier].append(entry)

    room = max(0, limit - len(tiers["A"]) - len(tiers["B"]))
    selected = [*tiers["A"], *tiers["B"], *tiers["C"][:room]]
    receipt["terms"] = selected
    receipt["counts"] = {
        "selected": len(selected),
        "project": sum(1 for entry in selected if entry.get("project_id")),
        "public": sum(1 for entry in selected if not entry.get("project_id")),
        "tier_a": len(tiers["A"]),
        "tier_b": len(tiers["B"]),
        "tier_c": min(room, len(tiers["C"])),
        "tier_c_dropped": max(0, len(tiers["C"]) - room),
    }
    return receipt


def _cell(text: str) -> str:
    return text.replace("|", "／").replace("\n", " ")


def render_table(receipt: dict[str, Any]) -> str:
    """拼进 prompt 的「本场术语对照表」；一个词都没挑到时返回空串。"""
    terms = receipt.get("terms") or []
    if not terms:
        return ""
    project = receipt.get("project") or {}
    counts = receipt.get("counts") or {}
    if project.get("name"):
        basis = f"本场按「{project['name']}」项目挑了 {len(terms)} 条词（项目词 {counts.get('project', 0)} 条、公共词 {counts.get('public', 0)} 条）"
    else:
        basis = f"本场没有认出项目，只用了 {len(terms)} 条公共词"
    lines = [
        "## 本场术语对照表（以此为准）",
        "",
        f"{basis}。写纪要时，逐字稿里出现「常见错写」一列的写法，一律改成左边的正确写法；"
        "「也叫」是同一个东西的别称，照原话保留，不要改。表里没有的词不要按词典去猜。"
        "这张表已经是本场词典的全部，不要再读 glossary-snapshot.json。",
        "",
        "| 正确写法 | 常见错写 | 也叫 |",
        "|---|---|---|",
    ]
    for entry in terms:
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(entry["term"]),
                    _cell("、".join(entry.get("aliases") or [])),
                    _cell("、".join(entry.get("also") or [])),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_receipt(receipt: dict[str, Any], directory: Path | str) -> Path:
    """把回执写进 attempt 目录（临时文件 + replace，读方看不到半截文件）。"""
    target = Path(directory) / INJECTION_FILENAME
    temporary = target.with_name(f".{INJECTION_FILENAME}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return target
