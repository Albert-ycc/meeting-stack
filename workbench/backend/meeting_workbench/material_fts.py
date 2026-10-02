"""材料全文表在恢复备份后的补建（第三期 3f）。

备份时材料全文表用 'delete-all' 清空（普通 DELETE 反而让备份变大），同一个事务里在副本的 app_state
写 material_fts_rebuild。用这份备份恢复后，服务启动时由一个单独的后台任务补回全文表：

- 开始时记下 max(material_chunks.id) 作为终点，写进这个键；每批补 id 在（上一批末尾, 终点]里的
  5000 个，一批一个事务，已补到的 id 也记在键里，重启后接着补。终点之后的新片段由触发器建索引。
- 补完之前材料内容循环和音视频循环一律不写、不删 material_chunks：在还没补进去的片段上执行删除会报
  database disk image is malformed，补到的时候再插一遍会重复索引。
- 补完跑一次 integrity-check（rank=1，连外部内容一起核对），不通过就 'rebuild'，然后删掉这个键。
- 不看 material_content_enabled，也不看忙信号（40 万段约半分钟）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from .db import Database, utc_now

logger = logging.getLogger(__name__)

REBUILD_KEY = "material_fts_rebuild"
BATCH = 5000


def rebuild_mark(connection: Any) -> dict[str, Any] | None:
    """键的值：{"end": 终点 id 或 None（还没开始）, "done": 已补到的 id}。没有这个键回 None。"""
    row = connection.execute("SELECT value FROM app_state WHERE key = ?", (REBUILD_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    end = value.get("end")
    done = value.get("done")
    return {
        "end": int(end) if isinstance(end, int) else None,
        "done": int(done) if isinstance(done, int) else 0,
    }


def rebuild_pending(db: Database) -> bool:
    """全文表还没补完：材料循环不动片段，搜索写「材料的全文索引在重建」。读库出错时当作还在补：材料循环
    宁可停一轮，也不能在补表期间删、写片段（会报 malformed、重复索引）。"""
    try:
        return (
            db.query_one("SELECT 1 AS pending FROM app_state WHERE key = ?", (REBUILD_KEY,))
            is not None
        )
    except sqlite3.OperationalError:
        return True


def mark_for_rebuild(connection: sqlite3.Connection) -> None:
    """备份副本里用：和 'delete-all' 在同一个事务里写这个键。"""
    connection.execute(
        """INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (REBUILD_KEY, json.dumps({"end": None, "done": 0}), utc_now()),
    )


def _write_mark(connection: sqlite3.Connection, end: int, done: int) -> None:
    connection.execute(
        "UPDATE app_state SET value = ?, updated_at = ? WHERE key = ?",
        (json.dumps({"end": end, "done": done}), utc_now(), REBUILD_KEY),
    )


def integrity_ok(connection: sqlite3.Connection) -> bool:
    """rank=1：连外部内容表一起核对（rank=0 只查索引自己）。doctor 也用它。"""
    try:
        connection.execute(
            "INSERT INTO material_chunks_fts(material_chunks_fts, rank) VALUES('integrity-check', 1)"
        )
    except sqlite3.DatabaseError:
        return False
    return True


def run_rebuild(
    db: Database,
    *,
    batch: int = BATCH,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """补全文表直到补完或被叫停。返回 {state: none|done|stopped, batches, rebuilt}。"""
    stats: dict[str, Any] = {"state": "none", "batches": 0, "rebuilt": False}
    with db.transaction() as connection:
        mark = rebuild_mark(connection)
        if mark is None:
            return stats
        if mark["end"] is None:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS end_id FROM material_chunks"
            ).fetchone()
            mark = {"end": int(row["end_id"]), "done": 0}
            _write_mark(connection, mark["end"], 0)
    end, done = mark["end"], mark["done"]
    while done < end:
        if should_stop is not None and should_stop():
            stats["state"] = "stopped"
            return stats
        with db.transaction() as connection:
            row = connection.execute(
                """SELECT MAX(id) AS last_id FROM (
                       SELECT id FROM material_chunks WHERE id > ? AND id <= ? ORDER BY id LIMIT ?
                   )""",
                (done, end, batch),
            ).fetchone()
            last = row["last_id"]
            if last is None:
                done = end
                _write_mark(connection, end, done)
                break
            connection.execute(
                """INSERT INTO material_chunks_fts(rowid, text)
                   SELECT id, text FROM material_chunks WHERE id > ? AND id <= ? ORDER BY id""",
                (done, int(last)),
            )
            done = int(last)
            _write_mark(connection, end, done)
        stats["batches"] += 1
    with db.transaction() as connection:
        if not integrity_ok(connection):
            logger.warning("材料全文表补完后核对不通过，整张重建")
            connection.execute(
                "INSERT INTO material_chunks_fts(material_chunks_fts) VALUES('rebuild')"
            )
            stats["rebuilt"] = True
        connection.execute("DELETE FROM app_state WHERE key = ?", (REBUILD_KEY,))
    stats["state"] = "done"
    return stats
