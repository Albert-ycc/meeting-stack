#!/usr/bin/env python3
"""存量说话人标签回填 CLI（一次性）。

对应方案 `docs/superpowers/plans/2026-08-18-speaker-diarization-enablement.md`
§3 改动 B：把 69 场已有非 degraded funasr_json 的会议，按 ordinal 对齐、逐段文本
严格相等校验后，就地把 speaker_label 写进 current 版本的 segments——不新建版本、
不碰逐字稿文本本身。复用改动 A 的同一个补标函数
`meeting_workbench.speaker_backfill.apply_speaker_labels_with_connection`，
不存在第二份实现。

用法：
    .venv/bin/python backend/scripts/backfill_speakers.py --dry-run
    .venv/bin/python backend/scripts/backfill_speakers.py

--dry-run 只解析、比对、出报告，不写库、不写事件、不落回滚清单之外的任何东西。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from meeting_workbench.archive_lock import ArchiveLock
from meeting_workbench.backup import BackupManager
from meeting_workbench.config import Settings
from meeting_workbench.db import Database
from meeting_workbench.speaker_backfill import apply_speaker_labels_with_connection


# 每次改动这份脚本改变了「怎么判定回填是否健康/成功」的口径时递增。
# 幂等判据必须带版本号——只看「有没有 label」会让未来 parser 再修复后的
# 旧数据进不去（半场态之外，全量误合并的旧结果也会被误判成"已完成"）。
SCRIPT_VERSION = 1
EVENT_TYPE = "speaker_backfill"

# relay_control.py 的完整在飞状态机；出现任意一个都视为「还有任务在飞」。
RELAY_IN_FLIGHT_STATUSES = (
    "queued",
    "stabilizing",
    "transcribing",
    "transcript_ready",
    "minutes_generating",
)


@dataclass
class MeetingOutcome:
    meeting_id: str
    outcome: str  # applied | already_done | skipped | human_review | error
    detail: str = ""
    updated_segments: int = 0
    speaker_count: int = 0


@dataclass
class BackfillReport:
    generated_at: str = ""
    dry_run: bool = True
    total_meetings: int = 0
    meetings_with_funasr_json: int = 0
    meetings_without_funasr_json: int = 0
    outcomes: list[MeetingOutcome] = field(default_factory=list)
    hash_bound_current_minutes: list[str] = field(default_factory=list)
    chunked_meetings: list[str] = field(default_factory=list)


def _database(settings: Settings) -> Database:
    db = Database(settings.database_path)
    db.initialize(before_migrate=lambda: BackupManager(db, settings).create())
    return db


def _relay_in_flight_jobs(settings: Settings) -> list[str]:
    if not settings.relay_jobs_db.is_file():
        return []
    uri = f"file:{settings.relay_jobs_db}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        placeholders = ",".join("?" for _ in RELAY_IN_FLIGHT_STATUSES)
        rows = connection.execute(
            f"SELECT job_id, status FROM jobs WHERE status IN ({placeholders})",
            RELAY_IN_FLIGHT_STATUSES,
        ).fetchall()
        return [f"{row[0]}:{row[1]}" for row in rows]
    finally:
        connection.close()


def _funasr_meeting_ids(db: Database) -> list[str]:
    return [
        row["meeting_id"]
        for row in db.query_all(
            "SELECT DISTINCT meeting_id FROM artifacts WHERE kind='funasr_json' ORDER BY meeting_id"
        )
    ]


def _human_review_reason(db: Database, meeting_id: str) -> str | None:
    """手改保护：kind='draft' 或 status='draft_modified' 或有 open external_source_change。

    评审裁定：hash 比对已废弃（6 场 source_path 缺失会假阳性），一条 SQL 判定即可。
    """
    meeting = db.query_one("SELECT status FROM meetings WHERE id=?", (meeting_id,))
    current = db.query_one(
        """SELECT tv.kind FROM transcript_versions tv
           JOIN meetings m ON m.current_transcript_version_id = tv.id
           WHERE m.id=?""",
        (meeting_id,),
    )
    reasons = []
    if not meeting or not current:
        reasons.append("no_current_transcript_version")
    elif current["kind"] == "draft":
        reasons.append("current_version_kind_draft")
    if meeting and meeting["status"] == "draft_modified":
        reasons.append("status_draft_modified")
    if db.conflicts.has(meeting_id, "external_source_change"):
        reasons.append("open_external_source_change")
    return ",".join(reasons) if reasons else None


def _coverage(db: Database, version_id: str) -> tuple[int, int]:
    row = db.query_one(
        "SELECT COUNT(*) AS total, COUNT(speaker_label) AS labeled FROM segments WHERE version_id=?",
        (version_id,),
    )
    return int(row["total"]), int(row["labeled"])


def _latest_backfill_event_version(db: Database, meeting_id: str) -> int | None:
    row = db.query_one(
        """SELECT payload_json FROM events
           WHERE meeting_id=? AND event_type=?
           ORDER BY id DESC LIMIT 1""",
        (meeting_id, EVENT_TYPE),
    )
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return None
    version = payload.get("script_version")
    return version if isinstance(version, int) else None


def _process_meeting(db: Database, meeting_id: str, *, dry_run: bool) -> MeetingOutcome:
    meeting = db.query_one(
        "SELECT current_transcript_version_id FROM meetings WHERE id=?", (meeting_id,)
    )
    version_id = meeting["current_transcript_version_id"] if meeting else None
    if not version_id:
        return MeetingOutcome(meeting_id, "skipped", detail="没有 current 逐字稿版本")

    total, labeled = _coverage(db, version_id)
    needs_rerun = 0 < labeled < total
    if not needs_rerun and total > 0 and labeled == total:
        done_version = _latest_backfill_event_version(db, meeting_id)
        if done_version == SCRIPT_VERSION:
            return MeetingOutcome(
                meeting_id, "already_done", detail=f"script_version={done_version} 已完成"
            )

    if dry_run:
        # dry-run 也要跑一次真实的 apply 逻辑才能知道会不会被健康断言挡住——
        # 用独立连接跑同一份实现，finally 里强制 rollback，绝不 commit、绝不持锁等待。
        probe_connection = db.connect()
        try:
            result = apply_speaker_labels_with_connection(probe_connection, meeting_id, version_id)
        finally:
            probe_connection.rollback()
            probe_connection.close()
        if result.applied:
            return MeetingOutcome(
                meeting_id,
                "applied",
                detail=f"dry-run 可回填（{result.source_path}）",
                updated_segments=result.updated_segments,
                speaker_count=result.speaker_count,
            )
        return MeetingOutcome(meeting_id, "skipped", detail=result.skip_reason or "未知原因")

    try:
        with db.transaction() as connection:
            result = apply_speaker_labels_with_connection(connection, meeting_id, version_id)
            if result.applied:
                db.add_event(
                    EVENT_TYPE,
                    meeting_id=meeting_id,
                    payload={
                        "script_version": SCRIPT_VERSION,
                        "updated_segments": result.updated_segments,
                        "speaker_count": result.speaker_count,
                        "source_path": result.source_path,
                    },
                    connection=connection,
                )
    except sqlite3.Error as error:
        return MeetingOutcome(meeting_id, "error", detail=str(error))

    if result.applied:
        return MeetingOutcome(
            meeting_id,
            "applied",
            detail=result.source_path or "",
            updated_segments=result.updated_segments,
            speaker_count=result.speaker_count,
        )
    return MeetingOutcome(meeting_id, "skipped", detail=result.skip_reason or "未知原因")


def run(settings: Settings, *, dry_run: bool) -> BackfillReport:
    in_flight = _relay_in_flight_jobs(settings)
    if in_flight:
        raise RuntimeError(f"relay 有在飞任务，拒绝执行：{in_flight}")

    db = _database(settings)
    report = BackfillReport(generated_at=datetime.now(UTC).isoformat(), dry_run=dry_run)
    report.total_meetings = int(db.query_one("SELECT COUNT(*) AS n FROM meetings")["n"])

    archive_lock = ArchiveLock(settings.database_path.parent / "archive.lock")
    with archive_lock:
        funasr_ids = _funasr_meeting_ids(db)
        report.meetings_with_funasr_json = len(funasr_ids)
        report.meetings_without_funasr_json = report.total_meetings - len(funasr_ids)

        for meeting_id in funasr_ids:
            reason = _human_review_reason(db, meeting_id)
            if reason:
                report.outcomes.append(
                    MeetingOutcome(meeting_id, "human_review", detail=reason)
                )
                continue
            report.outcomes.append(_process_meeting(db, meeting_id, dry_run=dry_run))

        report.hash_bound_current_minutes = [
            row["id"]
            for row in db.query_all(
                """SELECT m.id AS id FROM meetings m
                   JOIN minutes_versions mv ON mv.id = m.current_minutes_version_id
                   WHERE mv.input_transcript_sha256 IS NOT NULL
                     AND m.id IN (SELECT DISTINCT meeting_id FROM artifacts WHERE kind='funasr_json')
                   ORDER BY m.id"""
            )
        ]
    return report


def render_report_markdown(report: BackfillReport) -> str:
    lines = [
        "# 说话人标签存量回填报告",
        "",
        f"生成时间：{report.generated_at}",
        f"模式：{'dry-run（未写库）' if report.dry_run else '正式执行'}",
        f"脚本版本：{SCRIPT_VERSION}",
        "",
        "## 覆盖面",
        "",
        f"- 库内会议总数：{report.total_meetings}",
        f"- 有非 degraded funasr_json 的会议：{report.meetings_with_funasr_json}",
        f"- 无 funasr_json（whisper 老会议为主，本期无说话人）：{report.meetings_without_funasr_json}",
        "",
    ]

    by_outcome: dict[str, list[MeetingOutcome]] = {}
    for outcome in report.outcomes:
        by_outcome.setdefault(outcome.outcome, []).append(outcome)

    lines.append("## 结果分布")
    lines.append("")
    for key in ("applied", "already_done", "skipped", "human_review", "error"):
        lines.append(f"- {key}: {len(by_outcome.get(key, []))} 场")
    lines.append("")

    if by_outcome.get("applied"):
        lines.append("## 可回填 / 已回填清单")
        lines.append("")
        for outcome in by_outcome["applied"]:
            lines.append(
                f"- {outcome.meeting_id}：{outcome.updated_segments} 段，"
                f"{outcome.speaker_count} 个说话人（{outcome.detail}）"
            )
        lines.append("")

    if by_outcome.get("skipped"):
        lines.append("## 跳过清单（原因）")
        lines.append("")
        for outcome in by_outcome["skipped"]:
            lines.append(f"- {outcome.meeting_id}：{outcome.detail}")
        lines.append("")

    if by_outcome.get("human_review"):
        lines.append("## 人工确认清单（手改保护命中）")
        lines.append("")
        for outcome in by_outcome["human_review"]:
            lines.append(f"- {outcome.meeting_id}：{outcome.detail}")
        lines.append("")

    if by_outcome.get("error"):
        lines.append("## 异常清单")
        lines.append("")
        for outcome in by_outcome["error"]:
            lines.append(f"- {outcome.meeting_id}：{outcome.detail}")
        lines.append("")

    if report.hash_bound_current_minutes:
        lines.append("## 绑定 input_transcript_sha256 的会议（回填后发布会被拒，需先重生成纪要）")
        lines.append("")
        for meeting_id in report.hash_bound_current_minutes:
            lines.append(f"- {meeting_id}")
        lines.append("")

    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="只解析、比对、出报告，不写库"
    )
    parser.add_argument(
        "--report",
        default=None,
        help="报告输出路径，默认 docs/verification/backfill-speakers-<今天日期>.md",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    try:
        report = run(settings, dry_run=args.dry_run)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2

    markdown = render_report_markdown(report)
    if args.report:
        report_path = Path(args.report)
    else:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        report_path = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "verification"
            / f"backfill-speakers-{today}.md"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")

    applied = sum(1 for outcome in report.outcomes if outcome.outcome == "applied")
    errors = sum(1 for outcome in report.outcomes if outcome.outcome == "error")
    print(
        json.dumps(
            {
                "dry_run": report.dry_run,
                "total_meetings": report.total_meetings,
                "meetings_with_funasr_json": report.meetings_with_funasr_json,
                "applied": applied,
                "errors": errors,
                "report_path": str(report_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
