"""飞书任务确认卡片构造（参考实现，卡片 2.0）。

设计要点：
- 每条任务一个灰底信息块：标题 / 建议·时间 / 📌 会上原话。
- 每个信息块下一组「✅确认 / 驳回」按钮，value 带 task_id 与 extraction_id。
- 双按钮并排用 column_set（卡片 2.0 不支持 button_group 与 action 容器）。
- 确认后重建状态卡：已确认条目保留全部细节、去掉按钮；待确认条目保留按钮。
- 不产任何外链（本地化部署，外部网络访问不到工作台）。
"""
from __future__ import annotations

from typing import Any


def _assignee_label(assignee: str | None) -> str:
    return "交给 AI" if assignee == "ai" else ("我来做" if assignee == "me" else "")


def _format_anchor_ms(ms: int | None) -> str:
    """毫秒时间锚点 → mm:ss。"""
    if not ms:
        return ""
    seconds = max(0, int(ms) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _task_buttons(task_id: str, extraction_id: int | None) -> dict[str, Any]:
    """确认/驳回 两个回调按钮并排（卡片 2.0 用 column_set）。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "vertical_align": "center",
                "elements": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 确认"},
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {
                                    "action": "confirm",
                                    "task_id": task_id,
                                    "extraction_id": extraction_id,
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "tag": "column",
                "width": "weighted",
                "vertical_align": "center",
                "elements": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "驳回"},
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {
                                    "action": "reject",
                                    "task_id": task_id,
                                    "extraction_id": extraction_id,
                                },
                            }
                        ],
                    }
                ],
            },
        ],
    }


def _task_text_block(task: dict[str, Any], index: int) -> dict[str, Any]:
    """单条任务灰底信息块：标题 + 建议/时间 + 会上原话（辅助决策）。"""
    meta: list[str] = []
    label = _assignee_label(task.get("assignee"))
    if label:
        meta.append(f"建议：{label}")
    anchor = _format_anchor_ms(task.get("anchor_ms"))
    if anchor:
        meta.append(f"时间：{anchor}")
    quote = (task.get("anchor_quote") or "").strip()
    inner: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{index}. {task['title']}**"}
    ]
    if meta:
        inner.append({"tag": "markdown", "content": "　".join(meta)})
    if quote:
        inner.append({"tag": "markdown", "content": f"**📌 会上原话**\n{quote}"})
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {"tag": "column", "width": "weighted", "vertical_align": "top", "elements": inner}
        ],
    }


def _state_line(task: dict[str, Any], index: int) -> dict[str, Any]:
    """已确认/已驳回条目：保留来源/建议/原话全部细节，便于追溯。"""
    state = {
        "confirmed": "✅ 已确认",
        "in_progress": "🔄 进行中",
        "done": "✅ 已完成",
        "cancelled": "✖ 已驳回",
    }.get(task.get("status"), task.get("status", ""))
    meta: list[str] = []
    label = _assignee_label(task.get("assignee"))
    if label:
        meta.append(f"建议：{label}")
    anchor = _format_anchor_ms(task.get("anchor_ms"))
    if anchor:
        meta.append(f"时间：{anchor}")
    inner: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{index}. {task['title']}**　{state}"}
    ]
    if meta:
        inner.append({"tag": "markdown", "content": "　".join(meta)})
    quote = (task.get("anchor_quote") or "").strip()
    if quote:
        inner.append({"tag": "markdown", "content": f"📌 会上原话：{quote}"})
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {"tag": "column", "width": "weighted", "vertical_align": "top", "elements": inner}
        ],
    }


def build_draft_card(*, meeting_title: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """会后任务确认卡：逐条信息块 + 确认/驳回按钮，无「全部确认」，无外链。"""
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**来源**：{meeting_title}"},
        {"tag": "hr"},
    ]
    shown = 0
    for index, task in enumerate(tasks, 1):
        shown += 1
        elements.append(_task_text_block(task, index))
        elements.append(_task_buttons(task["id"], task.get("extraction_id")))
        if shown >= 5:
            rest = len(tasks) - shown
            if rest > 0:
                elements.append({"tag": "markdown", "content": f"…及另外 {rest} 条。"})
            break
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": f"会后任务待确认 · {len(tasks)} 条"},
        },
        "body": {"elements": elements},
    }


def build_status_card(
    *, meeting_title: str, tasks: list[dict[str, Any]], all_done: bool = False
) -> dict[str, Any]:
    """确认/驳回后重建：逐条保留 来源/建议/原话，已确认的去掉按钮，待确认的保留按钮。"""
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**来源**：{meeting_title}"},
        {"tag": "hr"},
    ]
    for index, task in enumerate(tasks, 1):
        if task.get("status") == "pending_confirm":
            elements.append(_task_text_block(task, index))
            elements.append(_task_buttons(task["id"], task.get("extraction_id")))
        else:
            elements.append(_state_line(task, index))
    header = {
        "template": "green" if all_done else "turquoise",
        "title": {
            "tag": "plain_text",
            "content": f"会后任务 · {len(tasks)} 条" + ("，已全部确认 ✅" if all_done else ""),
        },
    }
    return {"schema": "2.0", "config": {"wide_screen_mode": True}, "header": header, "body": {"elements": elements}}
