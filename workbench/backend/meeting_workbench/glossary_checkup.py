"""按这场会的词典看纪要（第一期 1d-2）。

relay 出纪要前按这场会挑词，把挑中的词写成 glossary-injection.json 随归档交回。导入后
这份回执存进 meeting_glossary_receipts，会议页据此说明「按哪个项目纠的错、用了几条」。

纪要体检是一条写死的字符串规则，不调模型：
- 已纠正：错写出现在逐字稿里、正确写法出现在纪要里、错写没出现在纪要里；
- 可能漏纠：纪要里还留着错写。
错写正好落在某个正确写法（或也叫）里面时不算，也不替换（「云图」之于「云图AI」）。

体检用的词典：有回执就按回执里的项目，没有就按会议当前的项目，再没有就只用公共词；
用户也可以指定按哪个项目查。项目词和公共词一起查，公共词的错写撞上本项目的词条或也叫时
不用（同 glossary/injection.py）。

漏纠只在这一版纪要是 relay 刚按本场词典出的、会还没人看过（completed_unreviewed）时
自动改过来，并且只在这一版第一次体检时改；其余情况只给按钮。改过的都能撤销。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .db import Database, utc_now

logger = logging.getLogger(__name__)

PUBLIC_SCOPE = "通用"
# 每轮扫描最多体检这么多场会；升级后第一次会把旧会慢慢查一遍。
CHECK_BATCH = 20
# relay 出的纪要版本（不是手改的草稿）。
GENERATED_KINDS = ("generated", "stale_generated", "imported")
_AUTO = object()

RECEIPT_KIND = "glossary_injection"
# 回执就是一张 50 条上下的词表，远小于这个上限；超过的当坏文件跳过。
MAX_RECEIPT_BYTES = 2 * 1024 * 1024


def _load_receipt(path: Path) -> tuple[dict[str, Any], str] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
            return None
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    if not isinstance(payload.get("terms"), list):
        return None
    return payload, hashlib.sha256(raw).hexdigest()


def ingest_receipts(db: Database) -> int:
    """把新索引到的 glossary-injection.json 存成回执；同一份内容只存一次。"""
    rows = db.query_all(
        """SELECT a.meeting_id, a.path, a.sha256 FROM artifacts a
            WHERE a.kind = ?
              AND NOT EXISTS (
                  SELECT 1 FROM meeting_glossary_receipts r
                   WHERE r.meeting_id = a.meeting_id AND r.sha256 = a.sha256
              )
            ORDER BY a.id""",
        (RECEIPT_KIND,),
    )
    stored = 0
    for row in rows:
        loaded = _load_receipt(Path(row["path"]))
        if loaded is None:
            continue
        payload, digest = loaded
        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        attempt = payload.get("attempt")
        with db.transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO meeting_glossary_receipts
                       (meeting_id, job_id, attempt, sha256, project_id, project_name,
                        project_source, term_count, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["meeting_id"],
                    payload.get("job_id") if isinstance(payload.get("job_id"), str) else None,
                    attempt if isinstance(attempt, int) else None,
                    digest,
                    project.get("id") if isinstance(project.get("id"), str) else None,
                    project.get("name") if isinstance(project.get("name"), str) else None,
                    project.get("source") if isinstance(project.get("source"), str) else None,
                    len(payload["terms"]),
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )
            stored += max(0, cursor.rowcount)
    return stored


def receipt_for_minutes(connection: Any, meeting_id: str, minutes: dict[str, Any] | None) -> dict[str, Any] | None:
    """这一版纪要出的时候用的回执：按纪要记着的 relay 任务和 attempt 对；手改的草稿沿用来源版本的。"""
    if not minutes or not minutes.get("source_job_id"):
        return None
    row = connection.execute(
        """SELECT * FROM meeting_glossary_receipts
            WHERE meeting_id = ? AND job_id = ? AND attempt IS ?
            ORDER BY id DESC LIMIT 1""",
        (meeting_id, minutes["source_job_id"], minutes.get("source_attempt")),
    ).fetchone()
    if row is None:
        return None
    receipt = dict(row)
    receipt["payload"] = json.loads(receipt["payload"])
    return receipt


def summarize_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """给会议页的摘要：按哪个项目、怎么定的、用了几条（项目词 / 公共词各几条）。"""
    if receipt is None:
        return None
    payload = receipt["payload"]
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    return {
        "id": receipt["id"],
        "job_id": receipt["job_id"],
        "attempt": receipt["attempt"],
        "project_id": receipt["project_id"],
        "project_name": receipt["project_name"],
        "project_source": receipt["project_source"],
        "term_count": receipt["term_count"],
        "project_terms": int(counts.get("project") or 0),
        "public_terms": int(counts.get("public") or 0),
        "snapshot_missing": bool(payload.get("snapshot_missing")),
        "generated_at": payload.get("generated_at"),
    }


# —— 纪要体检 ——


def dictionary_terms(connection: Any, project_id: str | None) -> list[dict[str, Any]]:
    """体检用的词：项目词在前，然后是公共词；公共词的错写撞上本项目词条或也叫时去掉。"""
    rows = connection.execute(
        """SELECT id, term, aliases, also, project_id FROM glossary_terms
            WHERE confirmed = 1
              AND ((project_id IS NULL AND scope = ?) OR (? IS NOT NULL AND project_id = ?))
            ORDER BY CASE WHEN project_id IS NULL THEN 1 ELSE 0 END, term""",
        (PUBLIC_SCOPE, project_id, project_id),
    ).fetchall()
    terms = [
        {
            "term_id": row["id"],
            "term": row["term"],
            "aliases": [a for a in json.loads(row["aliases"] or "[]") if isinstance(a, str) and a],
            "also": [a for a in json.loads(row["also"] or "[]") if isinstance(a, str) and a],
            "project_id": row["project_id"],
        }
        for row in rows
    ]
    protected = {
        text for term in terms if term["project_id"] for text in (term["term"], *term["also"])
    }
    for term in terms:
        if term["project_id"] is None and protected:
            term["aliases"] = [alias for alias in term["aliases"] if alias not in protected]
    return terms


def _shield_forms(terms: list[dict[str, Any]]) -> list[str]:
    """会「罩住」短错写的写法：所有正确写法、也叫，以及别的（更长的）错写。

    短错写落在正确写法里面的不算错（「云图」之于「云图AI」）；落在更长的错写里面的，
    由那条长错写来报和替换，不重复算（「树立」之于「树立协会」）。
    """
    return sorted(
        {text for term in terms for text in (term["term"], *term["also"], *term["aliases"])},
        key=len,
        reverse=True,
    )


def _outside_positions(text: str, needle: str, shield_forms: list[str]) -> list[int]:
    """needle 在 text 里的出现位置，去掉整个落在某个更长写法里面的那些。"""
    covering = [form for form in shield_forms if needle in form and form != needle]
    shields: list[tuple[int, int]] = []
    for form in covering:
        start = text.find(form)
        while start != -1:
            shields.append((start, start + len(form)))
            start = text.find(form, start + 1)
    positions: list[int] = []
    start = text.find(needle)
    while start != -1:
        end = start + len(needle)
        if not any(left <= start and end <= right for left, right in shields):
            positions.append(start)
        start = text.find(needle, end)
    return positions


def compute_hits(terms: list[dict[str, Any]], transcript: str, minutes: str) -> dict[str, list[dict[str, Any]]]:
    forms = _shield_forms(terms)
    corrected: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in terms:
        for wrong in term["aliases"]:
            if wrong in seen or wrong == term["term"]:
                continue
            seen.add(wrong)
            in_minutes = len(_outside_positions(minutes, wrong, forms))
            in_transcript = len(_outside_positions(transcript, wrong, forms))
            entry = {
                "term": term["term"],
                "wrong": wrong,
                "term_project_id": term["project_id"],
                "transcript_count": in_transcript,
                "minutes_count": in_minutes,
            }
            if in_minutes:
                missed.append(entry)
            elif in_transcript and term["term"] in minutes:
                entry["minutes_count"] = minutes.count(term["term"])
                corrected.append(entry)
    return {"corrected": corrected, "missed": missed}


def replace_missed(markdown: str, terms: list[dict[str, Any]], pairs: list[tuple[str, str]]) -> tuple[str, int]:
    """把纪要里的错写换成正确写法；长的错写先换，落在正确写法里面的不动。"""
    forms = _shield_forms(terms)
    text = markdown
    total = 0
    for wrong, correct in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
        positions = _outside_positions(text, wrong, forms)
        for start in reversed(positions):
            text = text[:start] + correct + text[start + len(wrong):]
        total += len(positions)
    return text, total


def _check_row(connection: Any, meeting_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM meeting_glossary_checks WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()
    return dict(row) if row else None


def check_meeting(db: Database, meeting_id: str, *, project_id: Any = _AUTO) -> dict[str, Any] | None:
    """按词典查当前这一版纪要，结果写进 meeting_glossary_checks / meeting_glossary_hits。

    project_id 不传：沿用你上次指定的项目；没指定过就按回执、会议当前项目、公共词的顺序。
    传 None 表示回到默认，传项目 id 表示按这个项目查。
    """
    with db.transaction() as connection:
        meeting = connection.execute(
            """SELECT id, project_id, current_minutes_version_id, current_transcript_version_id
                 FROM meetings WHERE id = ?""",
            (meeting_id,),
        ).fetchone()
        if meeting is None:
            return None
        minutes = (
            connection.execute(
                "SELECT * FROM minutes_versions WHERE id = ?",
                (meeting["current_minutes_version_id"],),
            ).fetchone()
            if meeting["current_minutes_version_id"]
            else None
        )
        previous = _check_row(connection, meeting_id)
        if minutes is None:
            connection.execute("DELETE FROM meeting_glossary_hits WHERE meeting_id = ?", (meeting_id,))
            connection.execute("DELETE FROM meeting_glossary_checks WHERE meeting_id = ?", (meeting_id,))
            return None
        minutes = dict(minutes)
        receipt = receipt_for_minutes(connection, meeting_id, minutes)
        if project_id is _AUTO and previous and previous["basis"] == "chosen":
            project_id = previous["project_id"]
        if project_id is not _AUTO and project_id is not None:
            basis, chosen_project = "chosen", project_id
        elif receipt is not None:
            basis, chosen_project = "receipt", receipt["project_id"]
        elif meeting["project_id"]:
            basis, chosen_project = "meeting", meeting["project_id"]
        else:
            basis, chosen_project = "public", None
        terms = dictionary_terms(connection, chosen_project)
        transcript = "\n".join(
            row["text"] or ""
            for row in connection.execute(
                "SELECT text FROM segments WHERE version_id = ? ORDER BY ordinal",
                (meeting["current_transcript_version_id"],),
            ).fetchall()
        )
        hits = compute_hits(terms, transcript, minutes["markdown"])
        now = utc_now()
        keep_applied = bool(previous) and previous.get("applied_version_id") == minutes["id"]
        connection.execute(
            """INSERT INTO meeting_glossary_checks
                   (meeting_id, minutes_version_id, project_id, basis, receipt_id,
                    applied_version_id, applied_from_version_id, applied_by, applied_count,
                    applied_at, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(meeting_id) DO UPDATE SET
                   minutes_version_id=excluded.minutes_version_id,
                   project_id=excluded.project_id, basis=excluded.basis,
                   receipt_id=excluded.receipt_id,
                   applied_version_id=excluded.applied_version_id,
                   applied_from_version_id=excluded.applied_from_version_id,
                   applied_by=excluded.applied_by, applied_count=excluded.applied_count,
                   applied_at=excluded.applied_at, checked_at=excluded.checked_at""",
            (
                meeting_id,
                minutes["id"],
                chosen_project,
                basis,
                receipt["id"] if receipt else None,
                previous["applied_version_id"] if keep_applied else None,
                previous["applied_from_version_id"] if keep_applied else None,
                previous["applied_by"] if keep_applied else None,
                previous["applied_count"] if keep_applied else 0,
                previous["applied_at"] if keep_applied else None,
                now,
            ),
        )
        connection.execute("DELETE FROM meeting_glossary_hits WHERE meeting_id = ?", (meeting_id,))
        for kind in ("corrected", "missed"):
            for entry in hits[kind]:
                connection.execute(
                    """INSERT INTO meeting_glossary_hits
                           (meeting_id, minutes_version_id, kind, term, wrong, term_project_id,
                            transcript_count, minutes_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        meeting_id,
                        minutes["id"],
                        kind,
                        entry["term"],
                        entry["wrong"],
                        entry["term_project_id"],
                        entry["transcript_count"],
                        entry["minutes_count"],
                        now,
                    ),
                )
    return {
        "basis": basis,
        "project_id": chosen_project,
        "receipt": receipt,
        "minutes": minutes,
        "first_check": previous is None or previous.get("minutes_version_id") != minutes["id"],
        **hits,
    }


def _auto_eligible(db: Database, meeting_id: str, result: dict[str, Any]) -> bool:
    if not result["missed"] or not result["first_check"] or result["receipt"] is None:
        return False
    if result["basis"] != "receipt" or result["minutes"]["kind"] not in GENERATED_KINDS:
        return False
    row = db.query_one("SELECT status FROM meetings WHERE id = ?", (meeting_id,))
    return bool(row) and row["status"] == "completed_unreviewed"


def apply_missed(
    db: Database,
    service: Any,
    meeting_id: str,
    *,
    expected_version_id: str,
    actor: str = "user",
) -> dict[str, Any]:
    """把体检发现的漏纠错写改过来，存成一版新纪要；返回新版本和改了几处。"""
    from .service import ConflictError, NotFoundError

    check = db.query_one("SELECT * FROM meeting_glossary_checks WHERE meeting_id = ?", (meeting_id,))
    meeting = db.query_one("SELECT current_minutes_version_id FROM meetings WHERE id = ?", (meeting_id,))
    if meeting is None:
        raise NotFoundError("会议不存在")
    if meeting["current_minutes_version_id"] != expected_version_id or check is None or (
        check["minutes_version_id"] != expected_version_id
    ):
        raise ConflictError("纪要已经变了，请刷新后再改")
    missed = db.query_all(
        "SELECT term, wrong FROM meeting_glossary_hits WHERE meeting_id = ? AND kind = 'missed'",
        (meeting_id,),
    )
    if not missed:
        raise ConflictError("没有要改的错写")
    minutes = db.query_one("SELECT markdown FROM minutes_versions WHERE id = ?", (expected_version_id,))
    with db.autocommit() as connection:
        terms = dictionary_terms(connection, check["project_id"])
    markdown, count = replace_missed(
        minutes["markdown"], terms, [(row["wrong"], row["term"]) for row in missed]
    )
    if count == 0:
        raise ConflictError("没有要改的错写")
    version_id, _ = service.save_minutes_detailed(
        meeting_id,
        markdown,
        expected_version_id,
        capture_corrections=False,
        actor="system" if actor == "auto" else actor,
        event_payload={"glossary_checkup": True, "replaced": count},
    )
    now = utc_now()
    db.execute(
        """UPDATE meeting_glossary_checks
              SET applied_version_id = ?, applied_from_version_id = ?, applied_by = ?,
                  applied_count = ?, applied_at = ?
            WHERE meeting_id = ?""",
        (version_id, expected_version_id, actor, count, now, meeting_id),
    )
    db.add_event(
        "minutes_glossary_applied",
        meeting_id=meeting_id,
        actor="system" if actor == "auto" else actor,
        payload={
            "version_id": version_id,
            "based_on_id": expected_version_id,
            "replaced": count,
            "pairs": [[row["wrong"], row["term"]] for row in missed],
            "auto": actor == "auto",
        },
    )
    check_meeting(db, meeting_id)
    return {"version_id": version_id, "replaced": count}


def undo_applied(db: Database, service: Any, meeting_id: str) -> dict[str, Any]:
    """撤销体检替换：纪要回到替换前那一版（以新版本的方式，历史都留着）。"""
    from .service import ConflictError

    check = db.query_one("SELECT * FROM meeting_glossary_checks WHERE meeting_id = ?", (meeting_id,))
    meeting = db.query_one("SELECT current_minutes_version_id FROM meetings WHERE id = ?", (meeting_id,))
    if (
        check is None
        or meeting is None
        or not check["applied_version_id"]
        or meeting["current_minutes_version_id"] != check["applied_version_id"]
    ):
        raise ConflictError("纪要在替换之后又改过，不能直接撤销；可以在历史版本里回退")
    version_id = service.rollback_minutes(meeting_id, check["applied_from_version_id"])
    db.execute(
        """UPDATE meeting_glossary_checks
              SET applied_version_id = NULL, applied_from_version_id = NULL, applied_by = NULL,
                  applied_count = 0, applied_at = NULL
            WHERE meeting_id = ?""",
        (meeting_id,),
    )
    db.add_event(
        "minutes_glossary_undone",
        meeting_id=meeting_id,
        actor="user",
        payload={"version_id": version_id, "undone_version_id": check["applied_version_id"]},
    )
    check_meeting(db, meeting_id)
    return {"version_id": version_id}


def run_pending(
    db: Database,
    service: Any,
    *,
    limit: int = CHECK_BATCH,
    on_minutes_changed: Any = None,
) -> dict[str, int]:
    """扫描循环里调用：收回执，体检纪要版本变了、词典变了或回执刚到的会，符合条件的自动改。"""
    stats = {"receipts": ingest_receipts(db), "checked": 0, "auto_applied": 0}
    rows = db.query_all(
        """SELECT m.id FROM meetings m
             JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
             LEFT JOIN meeting_glossary_checks c ON c.meeting_id = m.id
            WHERE c.meeting_id IS NULL
               OR c.minutes_version_id IS NOT m.current_minutes_version_id
               OR c.checked_at < COALESCE((SELECT MAX(updated_at) FROM glossary_terms), '')
               OR (c.receipt_id IS NULL AND c.basis != 'chosen' AND EXISTS (
                      SELECT 1 FROM meeting_glossary_receipts r
                       WHERE r.meeting_id = m.id AND r.job_id = mv.source_job_id
                         AND r.attempt IS mv.source_attempt))
            ORDER BY m.updated_at DESC
            LIMIT ?""",
        (limit,),
    )
    for row in rows:
        meeting_id = row["id"]
        try:
            result = check_meeting(db, meeting_id)
            stats["checked"] += 1
            if result is None or not _auto_eligible(db, meeting_id, result):
                continue
            apply_missed(
                db, service, meeting_id, expected_version_id=result["minutes"]["id"], actor="auto"
            )
            stats["auto_applied"] += 1
            if on_minutes_changed is not None:
                on_minutes_changed(meeting_id)
        except Exception:
            logger.exception("纪要体检失败 meeting_id=%s", meeting_id)
    return stats


def meeting_glossary(db: Database, meeting_id: str) -> dict[str, Any] | None:
    """会议页「词典」小节要的全部东西。还没体检过（或没有纪要）时返回 None。"""
    with db.autocommit() as connection:
        check = _check_row(connection, meeting_id)
        meeting = connection.execute(
            """SELECT m.project_id, m.current_minutes_version_id, p.name AS project_name,
                      p.color AS project_color
                 FROM meetings m LEFT JOIN projects p ON p.id = m.project_id
                WHERE m.id = ?""",
            (meeting_id,),
        ).fetchone()
        if check is None or meeting is None:
            return None
        receipt = None
        if check["receipt_id"]:
            row = connection.execute(
                "SELECT * FROM meeting_glossary_receipts WHERE id = ?", (check["receipt_id"],)
            ).fetchone()
            if row is not None:
                receipt = dict(row)
                receipt["payload"] = json.loads(receipt["payload"])
        project = None
        if check["project_id"]:
            row = connection.execute(
                "SELECT id, name, color FROM projects WHERE id = ?", (check["project_id"],)
            ).fetchone()
            project = (
                {"id": row["id"], "name": row["name"], "color": row["color"]}
                if row
                else {"id": check["project_id"], "name": receipt["project_name"] if receipt else None, "color": None}
            )
        hits = connection.execute(
            """SELECT kind, term, wrong, term_project_id, transcript_count, minutes_count
                 FROM meeting_glossary_hits WHERE meeting_id = ? ORDER BY id""",
            (meeting_id,),
        ).fetchall()
    current = meeting["current_minutes_version_id"]
    meeting_project = (
        {"id": meeting["project_id"], "name": meeting["project_name"], "color": meeting["project_color"]}
        if meeting["project_id"]
        else None
    )
    applied = None
    if check["applied_version_id"]:
        applied = {
            "by": check["applied_by"],
            "count": check["applied_count"],
            "at": check["applied_at"],
            "can_undo": check["applied_version_id"] == current,
        }
    return {
        "basis": check["basis"],
        "project": project,
        "meeting_project": meeting_project,
        "mismatch": bool(meeting_project) and (project or {}).get("id") != meeting_project["id"],
        "receipt": summarize_receipt(receipt),
        "minutes_version_id": check["minutes_version_id"],
        "stale": check["minutes_version_id"] != current,
        "checked_at": check["checked_at"],
        "corrected": [dict(row) for row in hits if row["kind"] == "corrected"],
        "missed": [dict(row) for row in hits if row["kind"] == "missed"],
        "applied": applied,
    }
