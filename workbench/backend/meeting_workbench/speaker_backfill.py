from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


from .parsers import parse_funasr_json


GARBLED_CHARACTER = "�"


@dataclass(slots=True)
class BackfillResult:
    meeting_id: str
    version_id: str
    applied: bool = False
    source_path: str | None = None
    updated_segments: int = 0
    speaker_count: int = 0
    skip_reason: str | None = None


def _find_funasr_json_with_connection(
    connection: sqlite3.Connection, meeting_id: str
) -> Path | None:
    """挑该会议一个非 degraded 的 .funasr.json 源文件。

    59 场会议的同一份 funasr.json 在暂存与归档路径各挂一条 artifact 记录（sha256
    相同），按路径排序取第一个非 degraded 且文件仍在的即可，不需要逐条比较内容。
    延迟导入 is_degraded：避免本模块与 importer.py 在模块加载期互相引用。
    """
    from .importer import is_degraded

    rows = connection.execute(
        "SELECT path FROM artifacts WHERE meeting_id = ? AND kind = 'funasr_json' ORDER BY path",
        (meeting_id,),
    ).fetchall()
    for row in rows:
        candidate = Path(row[0])
        if not is_degraded(candidate) and candidate.is_file():
            return candidate
    return None


def apply_speaker_labels_with_connection(
    connection: sqlite3.Connection, meeting_id: str, version_id: str
) -> BackfillResult:
    """把该会议 funasr_json 里的说话人标签补进目标版本的 segments。

    存量回填（CLI）与新会议导入钩子共用同一份实现：定位非 degraded 的
    funasr_json → 解析 → 与目标版本 segments 按 ordinal 对齐 → 逐段文本严格
    相等校验 → 健康断言（spk 覆盖率 100%、零乱码字符）→ 写入。任何一步不满足
    就整场跳过，绝不带病写入、绝不写半场。

    只用外部传入的 connection 做查询与写入，不自己开事务——调用方（importer
    钩子或回填脚本）负责事务边界；本函数不得调用会自开事务的
    _sync_speakers_and_duration / sync_transcript_metadata_with_connection，
    否则在外层事务里会因为重复 BEGIN 而死锁。
    """
    result = BackfillResult(meeting_id=meeting_id, version_id=version_id)
    source_path = _find_funasr_json_with_connection(connection, meeting_id)
    if source_path is None:
        result.skip_reason = "没有非 degraded 的 funasr_json 源文件"
        return result
    result.source_path = str(source_path)

    try:
        parsed = parse_funasr_json(source_path)
    except (OSError, ValueError, UnicodeError) as error:
        result.skip_reason = f"解析 funasr_json 失败：{error}"
        return result

    db_segments = connection.execute(
        "SELECT id, text FROM segments WHERE version_id = ? ORDER BY ordinal",
        (version_id,),
    ).fetchall()
    if len(db_segments) != len(parsed):
        result.skip_reason = (
            f"段数不一致：库内 {len(db_segments)} 段，funasr_json 解析出 {len(parsed)} 段"
        )
        return result
    for db_row, parsed_row in zip(db_segments, parsed):
        if db_row["text"] != parsed_row["text"]:
            result.skip_reason = "逐段文本比对不严格相等，判定 funasr_json 与目标版本不同源"
            return result

    labels = [parsed_row["speaker_label"] for parsed_row in parsed]
    if not any(labels):
        result.skip_reason = "funasr_json 内没有任何说话人标签"
        return result
    if any(label is None for label in labels):
        result.skip_reason = "spk 覆盖率未达 100%，判定该场解析结果不健康（可能是半场态）"
        return result
    if any(GARBLED_CHARACTER in parsed_row["text"] for parsed_row in parsed):
        result.skip_reason = "解析文本内含乱码字符（U+FFFD），判定源文件编码不可信"
        return result

    for db_row, label in zip(db_segments, labels):
        connection.execute(
            "UPDATE segments SET speaker_label = ? WHERE id = ?", (label, db_row["id"])
        )

    distinct_labels = dict.fromkeys(labels)
    for label in distinct_labels:
        speaker_id = f"speaker-{hashlib.sha1(f'{meeting_id}:{label}'.encode()).hexdigest()[:20]}"
        # 照抄 importer.py `_sync_speakers_and_duration` 的保名 UPSERT 写法：
        # display_name 恒传 NULL，COALESCE 保住用户已经改过的名字不被清空。
        connection.execute(
            """INSERT INTO speakers(id, meeting_id, label, display_name)
               VALUES (?, ?, ?, NULL)
               ON CONFLICT(meeting_id, label) DO UPDATE SET
                 display_name=COALESCE(excluded.display_name, speakers.display_name)""",
            (speaker_id, meeting_id, label),
        )

    result.applied = True
    result.updated_segments = len(labels)
    result.speaker_count = len(distinct_labels)
    return result
