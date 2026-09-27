#!/usr/bin/env python3
"""纪要归档本地预检：跑的是 complete-minutes 用的同一套 _minutes_evidence_errors。

用法：
    python3 precheck_minutes.py "<归档目录>"

退出码 0 = 校验通过（打印 NONE），1 = 有错（逐行打印错误码）。

存在的意义：派单 agent（Claude 或 DeepSeek）写完 evidence 后必须先本地过一遍，
别拿 complete-minutes 当试跑——那是闸门，不是校验器，失败要整个 attempt 重来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import relay_control as rc  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python3 precheck_minutes.py \"<归档目录>\"", file=sys.stderr)
        return 2

    root = Path(argv[1])
    if not root.is_dir():
        print(f"归档目录不存在：{root}", file=sys.stderr)
        return 2

    evidence = root / "minutes-evidence.json"
    if not evidence.is_file():
        print("minutes-evidence.json 缺失", file=sys.stderr)
        return 1

    # AppleDouble `._*` 在 exFAT 外置盘上遍地都是，别把它当纪要正文
    candidates = sorted(
        p for p in root.glob("*.md") if not p.name.startswith("._")
    )
    if not candidates:
        print("纪要 .md 缺失", file=sys.stderr)
        return 1
    if len(candidates) > 1:
        print(f"归档目录有多份 .md，取第一份校验：{[p.name for p in candidates]}",
              file=sys.stderr)
    minutes = candidates[0]

    try:
        protocol = int(json.loads(evidence.read_text(encoding="utf-8"))
                       .get("minutes_protocol_version", 2))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        print("minutes_evidence_unreadable")
        return 1

    plan = root / "minutes-plan.json"
    ledger = root / "minutes-ledger"
    srt = root / "input-transcript.srt"

    errors = rc._minutes_evidence_errors(
        evidence,
        minutes,
        protocol_version=protocol,
        plan_path=plan if protocol >= 3 and plan.is_file() else None,
        ledger_root=ledger if ledger.is_dir() else None,
        source_srt_path=srt if srt.is_file() else None,
    )

    if not errors:
        print("NONE")
        return 0
    for err in errors:
        print(err)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
