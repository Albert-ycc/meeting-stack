"""按这场会的词典看纪要（第一期 1d-2）。

relay 出纪要前按这场会挑词，把挑中的词写成 glossary-injection.json 随归档交回。导入后
这份回执存进 meeting_glossary_receipts，会议页据此说明「按哪个项目纠的错、用了几条」。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .db import Database, utc_now

logger = logging.getLogger(__name__)

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
