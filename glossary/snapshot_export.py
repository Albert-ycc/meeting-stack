# -*- coding: utf-8 -*-
"""术语词典快照导出（参考实现，标准库 only）。

词典的事实源放在应用侧数据库，转写侧的派单进程需要这份词典来注入 prompt。两个独立
进程如果共享数据库，转写侧就得跟着应用的 schema 迁移一起改——这是跨进程契约漂移。
解法是中间放一个 JSON 快照文件：应用在词典任何变更后原子重写，转写侧只读这个文件、
格式不符就返回空列表不报错（详见 docs/glossary-correction.md）。

这里沉淀两个关键点：

1. 排序要服务注入上限，不是字母序。注入 prompt 有 50 条上限，字母序会让几乎不会写错
   的英文缩写（ADHD/CRF/EDC）排在前面，把真正容易写错的人名术语挤出注入表。
   排序 key 交给纠错价值：人名 > 含中文的词 > 纯英文缩写。

2. 原子写：临时文件 + os.replace，读方永远看不到写了一半的文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 1


def snapshot_sort_key(row: dict[str, Any]) -> tuple:
    """纠错价值排序：人名 > 含中文的词 > 纯英文缩写，同档按字典序。"""
    term = row["term"]
    is_person = row.get("category") == "人名"
    has_cjk = any("一" <= ch <= "鿿" for ch in term)
    return (0 if is_person else 1, 0 if has_cjk else 1, term)


def export_snapshot(terms: list[dict[str, Any]], snapshot_path: Path) -> dict[str, Any]:
    """把词条列表导出成快照文件（原子写）。

    每个词条形如：
        {"term": "数理协会", "aliases": ["树立协会"], "scope": "通用", "category": "机构"}

    只导出 confirmed 的词条；排序用 snapshot_sort_key，保证注入上限截断后剩下的是
    最需要纠错的词。
    """
    rows = [t for t in terms if t.get("confirmed")]
    rows.sort(key=snapshot_sort_key)

    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "terms": [
            {
                "term": row["term"],
                "aliases": row.get("aliases", []),
                "scope": row.get("scope", "通用"),
                "category": row.get("category", "其他"),
            }
            for row in rows
        ],
    }

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{snapshot_path.name}.", dir=snapshot_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, snapshot_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return payload


def load_general_terms(snapshot_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """转写侧读快照、只取「通用」词条（注入上限截断）。容错：读不到返回空列表。"""
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return []
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    return [
        t for t in terms
        if isinstance(t, dict) and t.get("scope") == "通用"
        and str(t.get("term") or "").strip()
    ][:limit]
