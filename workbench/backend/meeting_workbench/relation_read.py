"""第四期：关联的读（4a）。显示用的读法都走这里；不建 SQL 视图（库里没有视图，回滚时也少一样 v15
可能碰到的东西）。

- 提到分两处存：字面的在 meeting_file_mentions（2d，它的 CHECK 改不了），放宽的在 relations
  （kind='mention'）。MENTION_UNION_SQL 把两处拼成一个样子，互相遮盖：字面行被同一 (会议, 项目, 词干)
  的放宽行盖住（你标了「不是这份文件」，或你［换成这份］过）；放宽行被任何字面行盖住（你换过的除外）。
  两半都按 m.project_id = 行的 project_id 连会议；活文件按来历规则找（LIVE_ID_SQL）。
- 「在 N 场会上被提到」先 COUNT(DISTINCT meeting_id)，再取列表，数目不受列表长度限制。
- 只看字面提到的内部读法（material_content 挑文件的顺序和 follow_content、
  material_index._dirty_meetings、file_mentions.generic_keys）不走这里。
- serialize 是白名单：score、prev_json、root_id 和原样的 evidence_json 永远不出接口。
- links_state：各页面的一句话状态 {kind, text, action}，kind 是 ok、waiting、stopped。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from .material_index import MATCH_ZONES

PARAM_BATCH = 400
MENTION_LIST_LIMIT = 40
REJECTED_LIST_LIMIT = 20
PASSAGE_CHARS = 60
QUOTE_CHARS = 40
_ZONES_SQL = ", ".join(f"'{zone}'" for zone in MATCH_ZONES)

# 关联行 r 现在指的活文件 id（来历规则，和 live_file 同一个顺序）：file_id 那行还活着（原地改过）；
# 否则同项目根目录里 content_key 相同的活文件（挪过，修改时间新的优先）；否则同一 (root_id, rel_path)。
# 都没有时退回 file_id（可能已经不见了，按上次认得的算），要活文件的读法自己再连 gone_at IS NULL。
LIVE_ID_SQL = """COALESCE(
    (SELECT f1.id FROM material_files f1 WHERE f1.id = r.file_id AND f1.gone_at IS NULL),
    (SELECT f2.id FROM material_files f2 JOIN project_material_roots pr ON pr.id = f2.root_id
      WHERE pr.project_id = r.project_id AND f2.content_key = r.content_key AND f2.gone_at IS NULL
      ORDER BY f2.mtime_ns DESC, f2.id LIMIT 1),
    (SELECT f3.id FROM material_files f3
      WHERE f3.root_id = r.root_id AND f3.rel_path = r.rel_path AND f3.gone_at IS NULL),
    r.file_id)"""

# 两种提到拼成一个样子（放进 WITH，外面按 project_id、meeting_id 或 file_id 筛，SQLite 会把条件推进
# 两半里走索引）。字面一半的 file_id 就是 meeting_file_mentions.file_id（文件不见了照样按上次认得的
# 算，和第二期一样）。遮盖检查按会议找（+x.project_id 不让它去扫整个项目的关联）。{literal}、{loose}
# 是两半各自多加的筛选（按文件数时字面一半按 file_id、放宽一半按项目走索引），见 mention_union。
_MENTION_UNION = f"""
SELECT 'literal' AS via, NULL AS relation_id, fm.meeting_id, fm.project_id, fm.stem_key,
       fm.file_id, fm.needle, fm.count, fm.first_ms, fm.anchors_json, fm.minutes_count, fm.source,
       fm.picked, '' AS quote, NULL AS phrase, NULL AS hint_via
  FROM meeting_file_mentions fm
  JOIN meetings m ON m.id = fm.meeting_id AND m.project_id = fm.project_id
 WHERE fm.status = 'active' AND {{literal}}
   AND NOT EXISTS (
       SELECT 1 FROM relations x
        WHERE x.meeting_id = fm.meeting_id AND x.kind = 'mention' AND +x.project_id = fm.project_id
          AND x.stem_key = fm.stem_key
          AND (x.status = 'rejected' OR (x.status = 'shown' AND x.origin = 'manual')))
UNION ALL
SELECT r.origin, r.id, r.meeting_id, r.project_id, r.stem_key,
       {LIVE_ID_SQL},
       COALESCE(json_extract(r.evidence_json, '$.phrase'), r.stem_key),
       MAX(1, COALESCE(json_array_length(r.evidence_json, '$.hits'), 0)), r.at_ms,
       CASE WHEN COALESCE(json_array_length(r.evidence_json, '$.hits'), 0) > 0
            THEN (SELECT json_group_array(json_extract(h.value, '$.at_ms'))
                    FROM json_each(r.evidence_json, '$.hits') h)
            WHEN r.at_ms IS NULL THEN '[]' ELSE json_array(r.at_ms) END,
       0, 'transcript', r.origin = 'manual', r.quote,
       json_extract(r.evidence_json, '$.phrase'), json_extract(r.evidence_json, '$.via')
  FROM relations r
  JOIN meetings m ON m.id = r.meeting_id AND m.project_id = r.project_id
 WHERE r.kind = 'mention' AND r.status = 'shown' AND {{loose}}
   AND (r.origin = 'manual' OR NOT EXISTS (
        SELECT 1 FROM meeting_file_mentions l
         WHERE l.meeting_id = r.meeting_id AND l.project_id = r.project_id
           AND l.stem_key = r.stem_key))"""


def mention_union(literal: str = "1", loose: str = "1") -> str:
    """MENTION_UNION_SQL 加上两半各自的筛选条件（参数按字面一半、放宽一半的顺序给）。"""
    return _MENTION_UNION.format(literal=literal, loose=loose)


MENTION_UNION_SQL = mention_union()

# 那场会的录音（和 material_status 的预览同一个挑法）
AUDIO_ID_SQL = """(SELECT a.id FROM artifacts a WHERE a.meeting_id = {meeting} AND a.kind = 'audio'
    ORDER BY CASE a.source_root
      WHEN 'archive' THEN 0 WHEN 'draft' THEN 1 WHEN 'staging' THEN 2 ELSE 3 END,
      a.role DESC, a.path LIMIT 1)"""

_FILE_PROJECT_SQL = """SELECT pr.project_id FROM material_files pf
    JOIN project_material_roots pr ON pr.id = pf.root_id WHERE pf.id IN ({marks})"""
_FILE_PROJECT = _FILE_PROJECT_SQL.format(marks="?")


def _file_union(marks: str) -> str:
    """按文件读的两种提到：字面一半按 file_id，放宽一半按文件所在的项目（活文件由来历规则定）。"""
    return mention_union(
        literal=f"fm.file_id IN ({marks})", loose=f"r.project_id IN ({_FILE_PROJECT_SQL.format(marks=marks)})"
    )


def _marks(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _batches(values: Sequence[Any], size: int = PARAM_BATCH) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _mention_item(row: Any) -> dict[str, Any]:
    return {
        "via": row["via"],
        "relation_id": row["relation_id"],
        "meeting_id": row["meeting_id"],
        "stem_key": row["stem_key"],
        "needle": row["needle"],
        "count": int(row["count"]),
        "first_ms": row["first_ms"],
        "anchors_json": row["anchors_json"],
        "minutes_count": int(row["minutes_count"]),
        "source": row["source"],
        "picked": bool(row["picked"]),
        "quote": row["quote"] or "",
        # 4b：放宽行才有（字面行为 None）；hint_via 是 stem、alias、time_hint 之一
        "phrase": row["phrase"],
        "hint_via": row["hint_via"],
    }


def loose_fields(row: dict[str, Any]) -> dict[str, Any]:
    """接口里给放宽行多出来的三项（字面行都是 None）：relation_id、phrase（会上的说法）、via。"""
    loose = row.get("relation_id") is not None
    return {
        "relation_id": row.get("relation_id") if loose else None,
        "phrase": (row.get("phrase") or row.get("needle")) if loose else None,
        "via": (row.get("hint_via") or "stem") if loose else None,
    }


def _one_per_file(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一场会连到同一份文件只留一行：字面的胜过放宽的，再比次数（两个词干可能对到同一份文件）。"""
    best: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (row["meeting_id"], row["file_id"])
        kept = best.get(key)
        rank = (row["via"] == "literal", int(row["count"]), int(row["minutes_count"]))
        if kept is None or rank > (kept["via"] == "literal", int(kept["count"]), int(kept["minutes_count"])):
            best[key] = row
    return [row for row in rows if best[(row["meeting_id"], row["file_id"])] is row]


# ---------------------------------------------------------------------- 提到


def meeting_mentions(connection: Any, meeting_id: str, project_id: str) -> list[dict[str, Any]]:
    """会议简报 files[] 的底：这场会在这个项目里的有效提到（字面和放宽的），连活文件。"""
    rows = connection.execute(
        f"""WITH u AS ({MENTION_UNION_SQL})
            SELECT u.*, f.id AS live_id, f.name, f.rel_path, f.root_id
              FROM u JOIN material_files f ON f.id = u.file_id AND f.gone_at IS NULL
             WHERE u.meeting_id = ? AND u.project_id = ? AND f.zone IN ({_ZONES_SQL})""",
        (meeting_id, project_id),
    ).fetchall()
    items = [
        {**_mention_item(row), "file_id": row["live_id"], "name": row["name"], "rel_path": row["rel_path"],
         "root_id": row["root_id"]}
        for row in rows
    ]
    return _one_per_file(items)


def project_edges(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """关系图查询 ⑫：本项目会上提到的文件，一条语句（以 WITH 开头）。4a 只有提到的两支，放宽的画成
    现有的 mentioned 线；4f 把在问的产出、影响和交付物加进同一条 UNION ALL，project_graph 始终 12 条。
    相关（kind='related'）永远不进来。"""
    rows = connection.execute(
        f"""WITH e AS ({MENTION_UNION_SQL})
            SELECT e.*, f.id AS live_id, f.name, f.ext, f.rel_path, f.root_id
              FROM e JOIN material_files f ON f.id = e.file_id AND f.gone_at IS NULL
             WHERE e.project_id = ?""",
        (project_id,),
    ).fetchall()
    items = [
        {**_mention_item(row), "file_id": row["live_id"], "name": row["name"], "ext": row["ext"],
         "rel_path": row["rel_path"], "root_id": row["root_id"]}
        for row in rows
    ]
    return _one_per_file(items)


def file_mention_counts(connection: Any, file_ids: Iterable[int]) -> dict[int, int]:
    """每份文件在几场会上被提到（字面和放宽的有效行，按会去重）。先数，不受列表长度限制。"""
    ids = sorted({int(file_id) for file_id in file_ids})
    counts: dict[int, int] = {}
    for part in _batches(ids):
        marks = _marks(part)
        for row in connection.execute(
            f"""WITH u AS ({_file_union(marks)})
                SELECT u.file_id, COUNT(DISTINCT u.meeting_id) AS n FROM u
                 WHERE u.file_id IN ({marks})
                 GROUP BY u.file_id""",
            [*part, *part, *part],
        ).fetchall():
            counts[int(row["file_id"])] = int(row["n"])
    return counts


def file_mention_meetings(
    connection: Any, file_id: int, limit: int = MENTION_LIST_LIMIT, *, audio: bool = False
) -> list[dict[str, Any]]:
    """提到这份文件的会，新的在前，最多 limit 场；数目用 file_mention_counts 另数。"""
    audio_sql = f", {AUDIO_ID_SQL.format(meeting='m.id')} AS audio_id" if audio else ""
    rows = connection.execute(
        f"""WITH u AS ({_file_union("?")})
            SELECT u.*, m.title, m.recording_date, m.created_at{audio_sql}
              FROM u JOIN meetings m ON m.id = u.meeting_id
             WHERE u.file_id = ?
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id, u.via != 'literal'
             LIMIT ?""",
        (file_id, file_id, file_id, int(limit)),
    ).fetchall()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["meeting_id"] in seen:
            continue
        seen.add(row["meeting_id"])
        item = {
            **_mention_item(row),
            "title": row["title"],
            "date": str(row["recording_date"] or row["created_at"] or "")[:10],
        }
        if audio:
            item["audio_id"] = row["audio_id"]
        result.append(item)
    return result


def rejected_file_mentions(connection: Any, file_id: int, limit: int = REJECTED_LIST_LIMIT) -> list[dict[str, Any]]:
    """你标过「不是这份文件」的字面提到（文件面板上给［撤销］）。"""
    return [
        {**dict(row), "date": str(row["recording_date"] or row["created_at"] or "")[:10]}
        for row in connection.execute(
            """SELECT fm.meeting_id, fm.stem_key, fm.needle, fm.count, fm.first_ms, fm.anchors_json,
                      fm.minutes_count, fm.source, fm.picked, m.title, m.recording_date, m.created_at
                 FROM meeting_file_mentions fm
                 JOIN meetings m ON m.id = fm.meeting_id AND m.project_id = fm.project_id
                WHERE fm.file_id = ? AND fm.status = 'rejected'
                ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id
                LIMIT ?""",
            (file_id, int(limit)),
        ).fetchall()
    ]


def rejected_loose_mentions(connection: Any, file_id: int, limit: int = REJECTED_LIST_LIMIT) -> list[dict[str, Any]]:
    """你标过「不是这份文件」的放宽提到（4b）：过了撤销期，文件面板上那一行的［撤销］发 restore。"""
    rows = connection.execute(
        f"""SELECT r.id AS relation_id, r.meeting_id, r.stem_key, r.at_ms, r.quote, r.evidence_json,
                   m.title, m.recording_date, m.created_at
              FROM relations r
              JOIN meetings m ON m.id = r.meeting_id AND m.project_id = r.project_id
             WHERE r.kind = 'mention' AND r.status = 'rejected' AND r.project_id IN ({_FILE_PROJECT})
               AND {LIVE_ID_SQL} = ?
             ORDER BY COALESCE(m.recording_date, m.created_at) DESC, m.id
             LIMIT ?""",
        (file_id, file_id, int(limit)),
    ).fetchall()
    result = []
    for row in rows:
        evidence = public_evidence(row["evidence_json"])
        hits = evidence.get("hits") or []
        result.append(
            {
                "relation_id": row["relation_id"],
                "meeting_id": row["meeting_id"],
                "stem_key": row["stem_key"],
                "needle": evidence.get("phrase") or row["stem_key"],
                "phrase": evidence.get("phrase") or row["stem_key"],
                "hint_via": evidence.get("via") or "stem",
                "count": max(1, len(hits)),
                "first_ms": row["at_ms"],
                "anchors_json": json.dumps([hit.get("at_ms") for hit in hits if isinstance(hit, dict)]),
                "minutes_count": 0,
                "source": "transcript",
                "picked": 0,
                "quote": row["quote"] or "",
                "title": row["title"],
                "recording_date": row["recording_date"],
                "created_at": row["created_at"],
                "date": str(row["recording_date"] or row["created_at"] or "")[:10],
            }
        )
    return result


# ---------------------------------------------------------------------- 来历和一跳


def live_file(connection: Any, row: Any) -> dict[str, Any] | None:
    """关联行现在指的活文件：file_id 那行还活着（原地改过）；否则同项目根目录里 content_key 相同的活
    文件（挪过）；否则同一 (root_id, rel_path)；都没有就算断了线，返回 None。相关只按内容找。挪了位置
    又改了内容的文件会断线，它的旧回答不再生效。"""
    by_content = row["kind"] == "related" if "kind" in row.keys() else False
    if row["file_id"] is not None:
        found = connection.execute(
            "SELECT * FROM material_files WHERE id = ? AND gone_at IS NULL", (row["file_id"],)
        ).fetchone()
        if found is not None and (not by_content or found["content_key"] == row["content_key"]):
            return dict(found)
    if row["content_key"]:
        found = connection.execute(
            """SELECT f.* FROM material_files f JOIN project_material_roots pr ON pr.id = f.root_id
                WHERE pr.project_id = ? AND f.content_key = ? AND f.gone_at IS NULL
                ORDER BY f.mtime_ns DESC, f.id LIMIT 1""",
            (row["project_id"], row["content_key"]),
        ).fetchone()
        if found is not None:
            return dict(found)
    if by_content or row["root_id"] is None or not row["rel_path"]:
        return None
    found = connection.execute(
        "SELECT * FROM material_files WHERE root_id = ? AND rel_path = ? AND gone_at IS NULL",
        (row["root_id"], row["rel_path"]),
    ).fetchone()
    return dict(found) if found is not None else None


EDGE_KINDS = frozenset({"mention", "deliverable", "affects_resolved", "later_changed", "restated"})


def _node(node: str) -> tuple[str, str]:
    prefix, _, value = node.partition(":")
    if prefix not in ("m", "file", "task", "dec") or not value:
        raise ValueError(f"认不出的节点：{node}")
    return prefix, value


def edges_of(connection: Any, node: str, kinds: Iterable[str]) -> list[dict[str, Any]]:
    """一个节点走一跳能到的线（4f 的来龙去脉用），每走一跳最多 2 条查询：提到和关联表一条，交付物
    一条。节点是 m:会议、file:文件、task:任务、dec:决议；kinds 取自 EDGE_KINDS。只给有原话或你回答
    过的线：字面和放宽的提到、你标过已更新的影响、后来改了和后来又提到、交付物。"""
    prefix, value = _node(node)
    wanted = set(kinds) & EDGE_KINDS
    file_id = int(value) if prefix == "file" else None
    parts: list[str] = []
    params: list[Any] = []
    if "mention" in wanted and prefix in ("m", "file"):
        where = "u.meeting_id = ?" if prefix == "m" else "u.file_id = ?"
        parts.append(
            f"""SELECT 'mention' AS kind, u.via, u.relation_id, u.meeting_id, NULL AS decision_id,
                       NULL AS to_decision_id, u.file_id, u.first_ms AS at_ms, u.quote, u.needle
                  FROM u JOIN material_files f ON f.id = u.file_id AND f.gone_at IS NULL WHERE {where}"""
        )
        params += [value] if prefix == "m" else [file_id]
    if "affects_resolved" in wanted and prefix in ("file", "dec"):
        where = (
            f"{LIVE_ID_SQL} = ? AND r.project_id IN ({_FILE_PROJECT})" if prefix == "file" else "r.decision_id = ?"
        )
        parts.append(
            f"""SELECT 'affects_resolved', r.origin, r.id, r.meeting_id, r.decision_id, NULL,
                       {LIVE_ID_SQL}, r.at_ms, r.quote, NULL
                  FROM relations r WHERE r.kind = 'affects' AND r.status = 'resolved' AND {where}"""
        )
        params += [file_id, file_id] if prefix == "file" else [value]
    pair_kinds = [kind for kind in ("later_changed", "restated") if kind in wanted]
    if pair_kinds and prefix == "dec":
        parts.append(
            f"""SELECT r.kind, r.origin, r.id, r.meeting_id, r.decision_id, r.to_decision_id, NULL, r.at_ms,
                       r.quote, NULL
                  FROM relations r
                 WHERE r.kind IN ({_marks(pair_kinds)}) AND r.status = 'shown'
                   AND (r.decision_id = ? OR r.to_decision_id = ?)"""
        )
        params += [*pair_kinds, value, value]
    edges: list[dict[str, Any]] = []
    if parts:
        union, union_params = (_file_union("?"), [file_id, file_id]) if prefix == "file" else (MENTION_UNION_SQL, [])
        sql = f"WITH u AS ({union}) " + " UNION ALL ".join(parts)
        for row in connection.execute(sql, union_params + params).fetchall():
            kind = row["kind"]
            if kind == "mention":
                ends = (f"m:{row['meeting_id']}", f"file:{row['file_id']}")
            elif kind == "affects_resolved":
                ends = (f"dec:{row['decision_id']}", f"file:{row['file_id']}")
            else:
                ends = (f"dec:{row['decision_id']}", f"dec:{row['to_decision_id']}")
            if row["file_id"] is None and kind in ("mention", "affects_resolved"):
                continue
            edges.append(
                {
                    "kind": kind,
                    "from": ends[0],
                    "to": ends[1],
                    "via": row["via"],
                    "relation_id": row["relation_id"],
                    "meeting_id": row["meeting_id"],
                    "decision_id": row["decision_id"],
                    "file_id": row["file_id"],
                    "at_ms": row["at_ms"],
                    "quote": row["quote"] or "",
                    "needle": row["needle"],
                }
            )
    if "deliverable" in wanted and prefix in ("task", "file"):
        if prefix == "task":
            where, args = "d.task_id = ?", [value]
        else:
            where = """EXISTS (SELECT 1 FROM material_files x WHERE x.id = ?
                         AND ((df.content_key IS NOT NULL AND df.content_key = x.content_key)
                              OR (df.root_id = x.root_id AND df.rel_path = x.rel_path)))"""
            args = [file_id]
        for row in connection.execute(
            f"""SELECT d.id AS deliverable_id, d.task_id, t.meeting_id, t.anchor_ms, t.anchor_quote,
                       COALESCE(
                         (SELECT f2.id FROM material_files f2 JOIN project_material_roots pr ON pr.id = f2.root_id
                           WHERE pr.project_id = t.project_id AND f2.content_key = df.content_key
                             AND f2.gone_at IS NULL ORDER BY f2.mtime_ns DESC, f2.id LIMIT 1),
                         (SELECT f3.id FROM material_files f3
                           WHERE f3.root_id = df.root_id AND f3.rel_path = df.rel_path AND f3.gone_at IS NULL))
                         AS file_id
                  FROM deliverables d
                  JOIN deliverable_files df ON df.deliverable_id = d.id
                  JOIN tasks t ON t.id = d.task_id
                 WHERE {where}""",
            args,
        ).fetchall():
            if row["file_id"] is None:
                continue
            edges.append(
                {
                    "kind": "deliverable",
                    "from": f"task:{row['task_id']}",
                    "to": f"file:{row['file_id']}",
                    "via": "user",
                    "deliverable_id": row["deliverable_id"],
                    "meeting_id": row["meeting_id"],
                    "file_id": row["file_id"],
                    "at_ms": row["anchor_ms"],
                    "quote": row["anchor_quote"] or "",
                }
            )
    return edges


# ---------------------------------------------------------------------- 白名单输出


# evidence_json 里能出接口的键；材料一端只给位置（段号、页码），不给文字
_EVIDENCE_KEYS = (
    "phrase", "hits", "via", "words", "windows", "meeting", "material", "passage", "terms", "rule",
    "folder", "days", "ref", "scope", "event_kind", "event_day", "decision_ms", "ordinal",
    "why_earlier", "why_later",
)
_HIDDEN_KEYS = frozenset({"score", "root_id", "prev_json", "text"})


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _HIDDEN_KEYS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def public_evidence(evidence_json: str | None) -> dict[str, Any]:
    try:
        raw = json.loads(evidence_json or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: _clean(raw[key]) for key in _EVIDENCE_KEYS if key in raw}


def serialize(row: Any, file: dict[str, Any] | None = None) -> dict[str, Any]:
    """关联行对外的样子（白名单）。file 是活文件 {id, name}，没给时按 file_id 写、名字为空。"""
    data = dict(row)
    if file is None and data.get("file_id") is not None:
        file = {"id": data["file_id"], "name": data.get("file_name") or ""}
    return {
        "id": data["id"],
        "kind": data["kind"],
        "status": data["status"],
        "by_you": data["origin"] == "manual" or data["status"] in ("rejected", "confirmed", "resolved"),
        "meeting_id": data.get("meeting_id"),
        "at_ms": data.get("at_ms"),
        "task_id": data.get("task_id"),
        "decision_id": data.get("decision_id"),
        "to_decision_id": data.get("to_decision_id"),
        "file": {"id": file["id"], "name": file.get("name") or ""} if file else None,
        "quote": data.get("quote") or "",
        "evidence": public_evidence(data.get("evidence_json")),
        "decided_at": data.get("decided_at"),
    }


# ---------------------------------------------------------------------- 在问的问题（产出、影响，4e 起有数据）


_QUESTION_SQL = f"""
SELECT r.id, r.kind, r.task_id, r.decision_id, r.meeting_id, r.at_ms, r.quote, r.evidence_json,
       r.content_key, f.id AS live_id, f.name AS file_name, f.dir_rel,
       t.title AS task_title, t.status AS task_status,
       d.text AS decision_text, d.start_ms AS decision_ms, d.meeting_id AS decision_meeting_id,
       dm.title AS decision_meeting_title, dm.recording_date AS decision_recording_date,
       dm.created_at AS decision_created_at, {AUDIO_ID_SQL.format(meeting='dm.id')} AS decision_audio_id,
       c.loc AS passage_loc, c.text AS passage_text
  FROM relations r
  JOIN material_files f ON f.id = {LIVE_ID_SQL} AND f.gone_at IS NULL
  LEFT JOIN tasks t ON t.id = r.task_id
  LEFT JOIN decisions d ON d.id = r.decision_id
  LEFT JOIN meetings dm ON dm.id = d.meeting_id
  LEFT JOIN material_chunks c ON c.content_key = r.content_key
       AND c.ordinal = json_extract(r.evidence_json, '$.ordinal')
 WHERE r.status = 'suggested' AND {{where}}
 ORDER BY r.kind = 'produced', r.created_at DESC, r.id DESC"""


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _month_day(recording_date: str | None, created_at: str | None) -> str:
    from .graph import local_day  # graph 引用本模块，这里晚一点再引

    day = local_day(recording_date, created_at)
    if day.year == datetime.now().astimezone().year:
        return f"{day.month}/{day.day}"
    return f"{day.year}/{day.month}/{day.day}"


def _passage(text: str | None, terms: list[str]) -> str:
    """材料片段在读的时候现取：主语前后最多 60 个字，只给页面，不进表、不进提示词。"""
    body = " ".join(str(text or "").split())
    if not body:
        return ""
    at = min((body.find(term) for term in terms if term and term in body), default=-1)
    if at < 0 or len(body) <= PASSAGE_CHARS:
        return _short(body, PASSAGE_CHARS)
    start = max(0, at - PASSAGE_CHARS // 3)
    piece = body[start : start + PASSAGE_CHARS]
    return ("…" if start else "") + piece + ("…" if start + PASSAGE_CHARS < len(body) else "")


def produced_evidence_text(evidence: dict[str, Any]) -> str:
    """产出的证据：「会后 3 天新增在『能耗看板/』」「任务确认后 3 天改过，文件名里也有『报价单』」。"""
    days = evidence.get("days")
    after_meeting = evidence.get("ref", "meeting") == "meeting"
    if days in (None, 0):
        prefix = "会后当天" if after_meeting else "确认当天"
    else:
        prefix = f"会后 {int(days)} 天" if after_meeting else f"任务确认后 {int(days)} 天"
    words = [str(word) for word in evidence.get("words") or [] if word]
    folder = evidence.get("folder")
    if evidence.get("event_kind") == "changed":
        return f"{prefix}改过，文件名里也有『{words[0]}』" if words else f"{prefix}改过"
    if folder:
        return f"{prefix}新增在『{folder}』"
    return f"{prefix}新增，文件名里也有『{words[0]}』" if words else f"{prefix}新增"


def _question(row: Any, *, for_requirement: bool = False) -> dict[str, Any]:
    from .relations import ANSWERS  # relations 引用本模块

    evidence = public_evidence(row["evidence_json"])
    folder_name = str(row["dir_rel"] or "").rstrip("/").rpartition("/")[2]
    item: dict[str, Any] = {
        "relation_id": row["id"],
        "kind": row["kind"],
        "file": {"id": row["live_id"], "name": row["file_name"], "folder": f"{folder_name}/" if folder_name else ""},
        "answers": list(ANSWERS[row["kind"]]),
    }
    if row["kind"] == "produced":
        item["text"] = produced_evidence_text(evidence)
        item["ask"] = f"是任务『{row['task_title'] or ''}』的交付物吗？"
        item["task"] = {"id": row["task_id"], "title": row["task_title"], "status": row["task_status"]}
        item["words"] = [str(word) for word in evidence.get("words") or []]
        return item
    decision_text = row["decision_text"] or row["quote"] or ""
    day = _month_day(row["decision_recording_date"], row["decision_created_at"])
    if for_requirement:
        stem = str(row["file_name"] or "").rpartition(".")[0] or str(row["file_name"] or "")
        item["text"] = f"{stem} 之后没改过，可能过时"
    else:
        item["text"] = f"可能过时：{day} 决议『{_short(decision_text, QUOTE_CHARS)}』"
    item["decision"] = {
        "id": row["decision_id"],
        "text": decision_text,
        "date": str(row["decision_recording_date"] or row["decision_created_at"] or "")[:10],
        "meeting_id": row["decision_meeting_id"],
        "meeting_title": row["decision_meeting_title"],
        "start_ms": row["decision_ms"],
        "audio_url": f"/api/media/{row['decision_audio_id']}" if row["decision_audio_id"] else None,
    }
    terms = [str(term) for term in evidence.get("terms") or []]
    item["passage"] = (
        {"loc": row["passage_loc"] or "", "text": _passage(row["passage_text"], terms)}
        if row["passage_text"] is not None
        else None
    )
    item["words"] = terms
    return item


def file_questions(connection: Any, file_id: int) -> list[dict[str, Any]]:
    """文件面板、预览抽屉：这份文件上在问的影响和产出（影响在前），一条语句。"""
    rows = connection.execute(
        _QUESTION_SQL.format(
            where=f"""r.kind IN ('affects', 'produced') AND f.id = ?
                      AND r.project_id IN ({_FILE_PROJECT})"""
        ),
        (file_id, file_id),
    ).fetchall()
    return [_question(row) for row in rows]


def task_questions(connection: Any, task_id: str) -> list[dict[str, Any]]:
    """任务抽屉：这条任务在问的产出，一条语句。"""
    rows = connection.execute(
        _QUESTION_SQL.format(where="r.kind = 'produced' AND r.task_id = ?"), (task_id,)
    ).fetchall()
    return [_question(row) for row in rows]


def decision_questions(connection: Any, decision_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """需求卡：每条决议在问的影响（文件这一边的说法），一条语句；按决议 id 分组。"""
    ids = list(dict.fromkeys(decision_ids))
    result: dict[str, list[dict[str, Any]]] = {decision_id: [] for decision_id in ids}
    if not ids:
        return result
    rows = connection.execute(
        _QUESTION_SQL.format(where=f"r.kind = 'affects' AND r.decision_id IN ({_marks(ids)})"), ids
    ).fetchall()
    for row in rows:
        result.setdefault(row["decision_id"], []).append(_question(row, for_requirement=True))
    return result


# ---------------------------------------------------------------------- 状态句


LINKS_OFF = "关联整理已关闭，在终端运行 meeting-workbench doctor 看原因"
SEMANTIC_OFF = "语义索引关着，找不了相关材料"
ROOT_OFFLINE = "资料盘未连接，插上后接着整理"
TRANSCRIBING = "会议在转写，关联先停一下，转完接着整理"
MATERIALS_READING = "材料还在读，读完才能找相关段落"
VECTORS_FILLING = "材料的向量还在补，补完才能找意思相近的段落"
LOOSE_QUEUED = "会上换了叫法的文件还在整理"
LLM_NO_KEY = "没配置 AI，会上换了叫法的文件先不整理"
LLM_BAD_KEY = "AI 的 key 不对，会上换了叫法的文件先不整理"
LLM_CAPPED = "今天的 AI 用量到上限了，明天接着整理"
LLM_BALANCE = "AI 账户余额不足，会上换了叫法的文件先不整理"
LLM_UNREACHABLE = "AI 连不上，过一会儿自动再试"
LOOSE_FAILED = "这场会的 AI 整理没做成"
PAIR_FAILED = "这场会的决议没对比成"
RELATED_EMPTY = "这场会没找到相关材料"
RETRY_ACTION = {"kind": "retry", "label": "现在重试"}
_LLM_TEXTS = {
    "no_key": LLM_NO_KEY,
    "auth": LLM_BAD_KEY,
    "capped": LLM_CAPPED,
    "balance": LLM_BALANCE,
    "failing": LLM_UNREACHABLE,
    "backoff": LLM_UNREACHABLE,
    "network": LLM_UNREACHABLE,
}


def waiting_text(count: int) -> str:
    return f"还有 {int(count)} 场会在整理关联"


def _state(kind: str, text: str = "", action: dict[str, str] | None = None) -> dict[str, Any]:
    return {"kind": kind, "text": text, "action": dict(action) if action else None}


def _snapshot(worker: Any) -> dict[str, Any]:
    if worker is None:
        return {}
    if isinstance(worker, dict):
        return worker
    snapshot = getattr(worker, "snapshot", None)
    value = snapshot() if callable(snapshot) else None
    return value if isinstance(value, dict) else {}


def links_state(worker: Any, settings: Any, surface: str, **ids: Any) -> dict[str, Any]:
    """一个页面的一句话状态 {kind, text, action}：kind 是 ok、waiting、stopped；action 只有
    {"kind": "retry", "label": "现在重试"}（POST /api/links/retry）。worker 给的是 details.links 的
    快照（或有 snapshot() 的 worker），不查库；这场会自己的情况由调用方从 ids 传进来：
    mention_state、pair_state（台账里的 state）、offline、materials_reading、waiting（场数）、empty。
    surface：mentions（放宽的提到）、related（相关）、decisions（决议对比），其余只看总开关和转写。"""
    if not getattr(settings, "links_enabled", True):
        return _state("stopped", LINKS_OFF)
    snap = _snapshot(worker)
    if snap.get("enabled") is False:
        return _state("stopped", LINKS_OFF)
    if surface == "related" and not (
        getattr(settings, "semantic_enabled", True) and getattr(settings, "material_content_enabled", True)
    ):
        return _state("stopped", SEMANTIC_OFF)
    if ids.get("offline"):
        return _state("stopped", ROOT_OFFLINE)
    if surface == "mentions" and ids.get("mention_state") == "failed":
        return _state("stopped", LOOSE_FAILED, RETRY_ACTION)
    if surface == "decisions" and ids.get("pair_state") == "failed":
        return _state("stopped", PAIR_FAILED, RETRY_ACTION)
    pending_llm = (surface == "mentions" and ids.get("mention_state") in ("pending", "running")) or (
        surface == "decisions" and ids.get("pair_state") in ("pending", "running")
    )
    if pending_llm and snap.get("llm") in _LLM_TEXTS:
        return _state("stopped", _LLM_TEXTS[str(snap["llm"])])
    if snap.get("paused") == "busy" and (pending_llm or ids.get("waiting") or surface == "related"):
        return _state("waiting", TRANSCRIBING)
    if surface == "related":
        if ids.get("materials_reading"):
            return _state("waiting", MATERIALS_READING)
        if (snap.get("phases") or {}).get("related") == "waiting":
            return _state("waiting", VECTORS_FILLING)
    if surface == "mentions" and ids.get("mention_state") in ("pending", "running"):
        return _state("waiting", LOOSE_QUEUED)
    if ids.get("waiting"):
        return _state("waiting", waiting_text(int(ids["waiting"])))
    if surface == "related" and ids.get("empty"):
        return _state("ok", RELATED_EMPTY)
    return _state("ok")
