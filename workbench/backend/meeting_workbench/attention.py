"""需要处理的录音：失败或归档未完成的转写任务，以及被隔离的会议目录（260914 新增）。

relay 的阶段码和扫描器的隔离原因都是写给程序看的。这里统一翻成「发生了什么 / 下一步做什么」，
资料库「需要处理」和工作台提示共用这一份口径，不在前端各写一套。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MANIFEST_READ_LIMIT_BYTES = 1_000_000

TRANSCRIPTION_STAGES = frozenset({"discovered", "queued", "stabilizing", "transcribing"})
MINUTES_STAGES = frozenset(
    {"transcript_ready", "minutes_generating", "codex_callback", "archive_validation"}
)
ARCHIVE_STAGES = frozenset({"pending_archive"})
AUTO_RECOVERABLE_STAGES = frozenset({"codex_callback", "archive_validation"})
ATTENTION_KINDS = ("transcription", "minutes", "archive", "other")


def needs_attention(job: dict[str, Any]) -> bool:
    return job.get("status") == "failed" or job.get("failure_stage") in ARCHIVE_STAGES


def failure_kind(job: dict[str, Any]) -> str:
    stage = str(job.get("failure_stage") or "")
    if stage in ARCHIVE_STAGES:
        return "archive"
    if stage in TRANSCRIPTION_STAGES:
        return "transcription"
    if stage in MINUTES_STAGES:
        return "minutes"
    return "other"


def describe_job(
    job: dict[str, Any],
    *,
    meeting: dict[str, Any] | None = None,
    auto_recovery_left: bool = False,
) -> dict[str, Any]:
    kind = failure_kind(job)
    stage = job.get("failure_stage")
    if kind == "transcription":
        summary = "转写没有跑完"
        next_step = "到「转写录音」页对这条点「重新转写」；确认是坏录音就归档"
    elif kind == "minutes" and stage in AUTO_RECOVERABLE_STAGES and auto_recovery_left:
        summary = "纪要生成失败，系统会自动再试"
        next_step = "先不用管，20 分钟内会自动重新生成纪要"
    elif kind == "minutes":
        summary = "逐字稿已经好了，纪要没生成出来"
        next_step = "到「转写录音」页对这条点「重新生成纪要」"
    elif kind == "archive":
        # relay 不会定期自动重试这一步，界面上也没有按钮；补救只有 relayctl migrate-pending（260914 核实）。
        summary = "纪要已生成，放进会议文件夹时没完成"
        next_step = (
            f"确认外置盘在线后，在终端运行 relayctl migrate-pending {job.get('job_id') or ''} "
            "补放进会议文件夹；确认用不上就点「知道了，归档」"
        )
    else:
        summary = "处理中断"
        next_step = "到「转写录音」页查看这条的详情"
    return {
        "job_id": str(job.get("job_id") or job.get("id") or ""),
        "status": job.get("status"),
        "stage": stage,
        "kind": kind,
        "summary": summary,
        "next_step": next_step,
        "error": job.get("last_error"),
        "meeting_id": meeting.get("id") if meeting else None,
        "meeting_title": meeting.get("title") if meeting else None,
        "created_at": job.get("created_at"),
        "updated_at": str(job.get("updated_at") or ""),
    }


def manifest_job_id(directory: str | None) -> str | None:
    """隔离目录 manifest 里登记的 relay 任务号；读不到就返回 None。"""
    if not directory:
        return None
    path = Path(directory) / "workbench-manifest.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MANIFEST_READ_LIMIT_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job_id = payload.get("job_id") if isinstance(payload, dict) else None
    return job_id if isinstance(job_id, str) and job_id else None


def describe_quarantine(item: dict[str, Any]) -> dict[str, Any]:
    directory = str(item.get("directory") or "")
    reason = str(item.get("reason") or "")
    if "任务状态与预期不符" in reason or "归档未完成" in reason:
        next_step = "对应的转写任务没有正常收尾，处理完「转写录音」里的那条后会自动导入"
    elif "缺失" in reason:
        next_step = "会议文件夹里缺文件，补齐或重新生成后，下一轮扫描会自动导入"
    elif "音频" in reason:
        next_step = "原音频和登记的不一致，先确认音频没有被替换或改动"
    else:
        next_step = "检查这个会议文件夹，修好后下一轮扫描会自动导入"
    return {
        "directory": directory,
        "name": Path(directory).name or directory,
        "summary": "会议文件夹没能导入",
        "reason": reason,
        "next_step": next_step,
    }
