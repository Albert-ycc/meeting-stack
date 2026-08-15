"""纪要写好通知卡构造（参考实现，卡片 2.0）。

三次触达的第二环：纪要生成后，把这场会定了什么直接摊在群里，而不是只报一句
「纪要已生成」再让人回电脑翻。群里的人多半刚散会、在手机上看，摘要和决议
当场就能判断这场会要不要马上跟进。

设计要点：
- 纪要卡把纪要压成手机一眼看得完的三块：一句话摘要 / 定了哪几件事 / 下一步。
- 抽不到就留空，绝不拿别处的内容凑数——卡片少一块可以，凑一块错的更糟。
- 同时兼容两套纪要模板：老六段式（「三、核心决议」+「### 决议 N」）与新模板
  （「一分钟摘要」+「决议」下的有序列表）。
- 措辞跟内容走：有决议段说「定了这几件事」，只有议题说「聊了这几件事」。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def format_duration_ms(ms: int | None) -> str:
    """时长写成人读的说法：87 分钟 → 1 小时 27 分钟。拿不到时长返回空串。"""
    if not ms or ms <= 0:
        return ""
    total = max(1, int(round(ms / 60000)))
    if total < 60:
        return f"{total} 分钟"
    hours, minutes = divmod(total, 60)
    return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"


def format_recording_date(value: str | None) -> str:
    """录音日期写成「8 月 6 日」；解析不了就原样返回（宁可丑也不丢信息）。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10]
    return f"{parsed.month} 月 {parsed.day} 日"


# 纪要模板不止一套：老六段式是「## 三、核心决议」+「### 决议 1 · xxx」，
# 新模板是「## 一分钟摘要」+「## 决议」下的有序列表。两套都要认。
_SECTION_START = re.compile(r"^##\s+", re.MULTILINE)
# 新模板叫「一分钟摘要」，老六段式没有摘要、用「会议背景」承担同一角色。
_SUMMARY_SECTION = re.compile(r"^##\s+.*(摘要|背景).*$", re.MULTILINE)
_DECISION_SECTION = re.compile(r"^##\s+.*(决议|结论|共识).*$", re.MULTILINE)
_SUBHEADING = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.、)]|[-*+])\s+(.+?)\s*$", re.MULTILINE)
_ANCHOR = re.compile(r"\s*`?\[\d{2}:\d{2}(?::\d{2})?[^\]]*\]`?")
_ITEM_PREFIX = re.compile(
    r"^(决议|结论|共识|议题|话题)\s*[一二三四五六七八九十百\d]*\s*[·:：、\-]?\s*"
)


def _section_body(text: str, header: re.Match[str] | None) -> str:
    """取某个 ## 段的正文（到下一个 ## 为止）。"""
    if header is None:
        return ""
    tail = _SECTION_START.search(text, header.end())
    return text[header.end(): tail.start() if tail else len(text)]


def _clean_item(raw: str) -> str:
    """条目清洗成人能读的一行：去时间锚点、去「决议 1 ·」这类编号前缀、去加粗。"""
    item = _ANCHOR.sub("", raw).strip()
    item = _ITEM_PREFIX.sub("", item)
    item = item.replace("**", "").strip(" 　`|-—·")
    return item


def _truncate(text: str, limit: int) -> str:
    """按句截断：宁可短一点，也不要断在半句话上。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for mark in ("。", "；", "！", "？", "…"):
        cut = head.rfind(mark)
        if cut >= limit // 2:
            return head[: cut + 1]
    return head.rstrip("，、 ") + "…"


def outline_minutes(markdown: str, *, item_limit: int = 3) -> dict[str, Any]:
    """把纪要压成手机上一眼看得完的三块：一句话摘要、定了什么、还剩多少条。

    抽不到就留空——卡片少一块，绝不拿别处的内容凑数。
    """
    text = markdown or ""
    summary = ""
    body = _section_body(text, _SUMMARY_SECTION.search(text))
    if body:
        paragraph = next(
            (
                line.strip()
                for line in body.strip().split("\n")
                if line.strip() and not line.strip().startswith(("#", "|", ">", "---"))
            ),
            "",
        )
        summary = _truncate(_ANCHOR.sub("", paragraph).replace("**", "").strip(), 200)

    decision_body = _section_body(text, _DECISION_SECTION.search(text))
    kind = "decision"
    raw_items = _SUBHEADING.findall(decision_body) or _LIST_ITEM.findall(decision_body)
    if not raw_items:
        # 没有决议段的纪要退回全文三级标题：那是议题不是定论，措辞要跟着换。
        kind = "topic"
        raw_items = _SUBHEADING.findall(text)

    items: list[str] = []
    for raw in raw_items:
        item = _clean_item(raw)
        if item and item not in items:
            items.append(item)
    return {
        "summary": summary,
        "kind": kind if items else "",
        "items": [_truncate(item, 56) for item in items[:item_limit]],
        "total": len(items),
    }


def build_minutes_ready_card(
    *,
    meeting_title: str,
    recording_date: str | None,
    duration_ms: int | None,
    markdown: str,
    will_extract_tasks: bool = True,
    open_url: str = "",
) -> dict[str, Any]:
    """纪要写好的通知卡：会议名 + 录音元信息 + 摘要 + 定了什么 + 下一步。

    群里的人多半刚散会、在手机上看，不该只收到一句「纪要已生成」再回电脑翻——
    所以把摘要和决议直接摊开，当场就能判断这场会要不要马上跟进。
    """
    # 会议标题来自纪要 H1，常带「· 会议纪要」后缀被截掉后留个孤零零的分隔符。
    title = (meeting_title or "").strip().rstrip("·-—|·").strip() or "未命名录音"
    meta = "，".join(
        part
        for part in (
            f"{format_recording_date(recording_date)}录的会"
            if format_recording_date(recording_date)
            else "",
            f"时长 {format_duration_ms(duration_ms)}" if duration_ms else "",
        )
        if part
    )
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": f"**{title}**"}]
    elements.append(
        {
            "tag": "markdown",
            "content": (f"{meta}，纪要已经写好归档。" if meta else "纪要已经写好归档。"),
        }
    )
    outline = outline_minutes(markdown)
    if outline["summary"]:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": outline["summary"]})
    if outline["items"]:
        elements.append({"tag": "hr"})
        lines = [f"{i}. {item}" for i, item in enumerate(outline["items"], 1)]
        rest = outline["total"] - len(outline["items"])
        if rest > 0:
            lines.append(f"…另有 {rest} 条写在纪要里。")
        lead = "这场会定了这几件事" if outline["kind"] == "decision" else "这场会聊了这几件事"
        elements.append({"tag": "markdown", "content": f"**{lead}**\n" + "\n".join(lines)})
    elements.append({"tag": "hr"})
    if will_extract_tasks:
        elements.append(
            {
                "tag": "markdown",
                "content": "接下来从纪要里挑会后任务，挑完发一张确认卡过来，逐条确认后才开工。",
            }
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": "任务抽取暂时没跑（LLM 没配好），纪要本身不受影响，在工作台里可以随时手动重抽。",
            }
        )
    if open_url:
        elements.append({"tag": "markdown", "content": f"[在工作台看全文]({open_url})"})
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "纪要写好了"},
        },
        "body": {"elements": elements},
    }
