#!/usr/bin/env python3
"""纪要归档本地预检：调用 complete-minutes 用的同一个入口 RelayControl.validate_archive，
参数也取自同一处（任务库里这次 attempt 的来源哈希、协议版本等）。

用法：
    python3 precheck_minutes.py "<归档目录>" --job-id <job_id> --attempt <n>

不给 --job-id/--attempt 时从归档目录的 workbench-manifest.json 读。需要和 relayctl 一样
带 MEETING_RELAY_JOBS_DB / MEETING_RELAY_ARCHIVE_ROOT 环境变量。

退出码 0 = 校验通过（打印 NONE），1 = 有错（逐行打印错误码），2 = 用法或参数错。

存在的意义：派单 agent（Claude 或 DeepSeek）写完归档后必须先本地过一遍，
别拿 complete-minutes 当试跑——那是闸门，不是校验器，失败要整个 attempt 重来。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import relay_control as rc  # noqa: E402


def _manifest_identity(root: Path) -> tuple[str | None, int | None]:
    try:
        manifest = json.loads((root / "workbench-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(manifest, dict):
        return None, None
    job_id = manifest.get("job_id")
    attempt = manifest.get("attempt")
    return (
        job_id if isinstance(job_id, str) else None,
        attempt if type(attempt) is int else None,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="precheck_minutes.py")
    parser.add_argument("archive_dir")
    parser.add_argument("--job-id")
    parser.add_argument("--attempt", type=int)
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2

    root = Path(args.archive_dir)
    if not root.is_dir():
        print(f"归档目录不存在：{root}", file=sys.stderr)
        return 2

    manifest_job, manifest_attempt = _manifest_identity(root)
    job_id = args.job_id or manifest_job
    attempt_no = args.attempt if args.attempt is not None else manifest_attempt
    if not job_id or attempt_no is None:
        print("workbench_manifest_identity")
        return 1

    report = rc.RelayControl().precheck_archive(
        root, job_id=job_id, attempt_no=attempt_no
    )
    if report.valid:
        print("NONE")
        return 0
    for err in report.missing:
        print(err)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
