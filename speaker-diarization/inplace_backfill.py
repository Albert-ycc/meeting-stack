# -*- coding: utf-8 -*-
"""就地补标的骨架（参考实现）：给已入库的逐字稿补 speaker_label 字段。

适用前提：逐字稿文本来源（如 SRT）与说话人来源（funasr json）出自同一次转写，
段落一一对应。此时不要新建版本重导——那会换掉所有段 id，把以段 id 为主键的
派生数据（语义向量、外部引用）全部炸掉。就地 UPDATE 一个此前恒为 NULL 的字段，
血缘半径为零，回滚就是 SET NULL。

四条纪律（每条背后都是一个真实翻车场景，见 docs/speaker-diarization.md）：
1. 逐段 text 严格相等才写——对齐靠校验，不靠假设；
2. 一场一事务，不分批 commit——半场状态只可能来自分批；
3. speakers 表只用 UPSERT 保名语义——DELETE 重建会清掉用户已标的真名；
4. 幂等靠带版本号的事件台账——「已经有数据就跳过」会让未来的修复版永远进不去。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

SCRIPT_VERSION = 1


@dataclass
class BackfillResult:
    applied: bool
    reason: str = ""
    labeled_segments: int = 0
    speakers: int = 0


def apply_speaker_labels(
    connection: sqlite3.Connection,
    meeting_id: str,
    version_id: str,
    parsed_segments: list[dict[str, Any]],
) -> BackfillResult:
    """在调用方已开启的事务里，为一个版本的 segments 补 speaker_label。

    parsed_segments 来自 chunk_aware_labels.parse_chunked_funasr()。
    本函数不开事务、不提交——事务边界归调用方（一场会一个事务）。
    """
    rows = connection.execute(
        "SELECT id, text FROM segments WHERE version_id = ? ORDER BY ordinal",
        (version_id,),
    ).fetchall()

    # 纪律 1：段数与逐段文本严格相等才写，任何不一致整场跳过
    if len(rows) != len(parsed_segments):
        return BackfillResult(False, f"段数不一致 {len(rows)} != {len(parsed_segments)}")
    for (_, db_text), parsed in zip(rows, parsed_segments):
        if db_text != parsed["text"]:
            return BackfillResult(False, "逐段文本不一致，来源可能不同批")

    # 健康断言：说话人覆盖率必须 100%（正常转写产物如此），乱码字符必须为零
    labels = [p["speaker_label"] for p in parsed_segments]
    if any(label is None for label in labels):
        return BackfillResult(False, "说话人覆盖率不足 100%，源文件可疑")
    if any("�" in p["text"] for p in parsed_segments):
        return BackfillResult(False, "文本含替换字符，源文件编码可疑")

    for (segment_id, _), parsed in zip(rows, parsed_segments):
        connection.execute(
            "UPDATE segments SET speaker_label = ? WHERE id = ?",
            (parsed["speaker_label"], segment_id),
        )

    # 纪律 3：UPSERT 保名——display_name 只在为空时更新，绝不清掉用户已标的真名
    distinct = sorted(set(labels))
    for label in distinct:
        connection.execute(
            """INSERT INTO speakers(meeting_id, label, display_name)
               VALUES (?, ?, NULL)
               ON CONFLICT(meeting_id, label) DO NOTHING""",
            (meeting_id, label),
        )

    # 纪律 4：事件台账带版本号，幂等与「重跑要不要重做」都看它，不看数据本身
    connection.execute(
        """INSERT INTO events(meeting_id, event_type, payload)
           VALUES (?, 'speaker_backfill', json_object('script_version', ?))""",
        (meeting_id, SCRIPT_VERSION),
    )
    return BackfillResult(True, labeled_segments=len(rows), speakers=len(distinct))


def is_hand_edited(connection: sqlite3.Connection, meeting_id: str) -> bool:
    """手改判定：编辑操作必然产生 kind='draft' 版本（既有机制），一条 SQL 即精确信号。

    不要用「重新解析源文件算内容 hash 比对」——源文件可能已被清理（算不出 hash
    会误判成手改），且有确定性标记时启发式只会更差。
    """
    row = connection.execute(
        "SELECT COUNT(*) FROM transcript_versions WHERE meeting_id = ? AND kind = 'draft'",
        (meeting_id,),
    ).fetchone()
    return row[0] > 0
