from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .db import Database
from .gold_schema import GoldSchemaError, validate_gold_sample


class GoldExportError(RuntimeError):
    pass


def export_gold_jsonl(
    db: Database, output_path: str | Path, *, meeting_id: str | None = None
) -> int:
    clauses = ["1=1"]
    params: list[str] = []
    if meeting_id is not None:
        clauses.append("meeting_id=?")
        params.append(meeting_id)
    rows = db.query_all(
        f"""SELECT * FROM asr_gold_samples
             WHERE {" AND ".join(clauses)}
             ORDER BY meeting_id, start_ms, end_ms, id""",
        params,
    )
    if not rows:
        raise GoldExportError("没有金标样本可导出")
    output = Path(output_path).expanduser()
    if output.exists() and output.is_symlink():
        raise GoldExportError("导出目标不能是符号链接")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise GoldExportError("导出目录不存在或不安全")
    lines: list[str] = []
    for row in rows:
        try:
            raw_payload = {
                "schema_version": 2,
                "id": row["id"],
                "reference": row["reference"],
                "entities": json.loads(row["entities_json"]),
                "numbers": json.loads(row["numbers_json"]),
                "tags": json.loads(row["tags_json"]),
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
            }
            if row.get("source_audio_sha256") is not None:
                raw_payload["source_audio_sha256"] = row["source_audio_sha256"]
            payload = validate_gold_sample(raw_payload)
        except (GoldSchemaError, json.JSONDecodeError, TypeError) as error:
            raise GoldExportError("金标样本无效，拒绝导出") from error
        if payload["source_audio_sha256"] is None:
            payload.pop("source_audio_sha256")
        lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return len(rows)
