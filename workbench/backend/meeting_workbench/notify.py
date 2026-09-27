"""飞书通知通道（260804 新增）。

两种通道二选一（chat_id 优先）：
- 应用机器人：经本机 lark-cli 以 bot 身份发消息卡片到指定群，凭证复用
  lark-cli 的 keychain，本服务不接触 app secret；
- 群自定义机器人 webhook：直接 POST。

只做单向触达：卡片按钮是跳转链接，点开回声档 Web。所有发送过
notifications 台账幂等，失败只记日志不重试排队，靠扫描周期自然补发。

一场会在群里的三次触达（260806 补齐中间一环）：
  ① 转写完成 —— relay_watchdog 发，声明录音已经变成文字；
  ② 纪要写好 —— 本模块 minutes_ready，把这场会定了什么直接摊在群里；
  ③ 任务待确认 —— 本模块 task_draft，逐条确认/驳回。
每条通知都要答完三个问题：发生了什么、要不要我动手、动手去哪。
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from .db import Database, utc_now


def _format_anchor_ms(ms: int | None) -> str:
    """毫秒时间锚点 → mm:ss；无锚点返回空串。"""
    if not ms:
        return ""
    seconds = max(0, int(ms) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _assignee_label(assignee: str | None) -> str:
    return "交给 AI" if assignee == "ai" else ("我来做" if assignee == "me" else "")


_MARKDOWN_SPECIAL_CHARS = re.compile(r"([\\`*_\[\]()<>~])")


def _escape_markdown(text: str) -> str:
    """任务标题/会上原话来自会议内容，不受信任；转义掉会被解释成链接、@人等标记语法的字符，
    只留纯文本展示，防止「[点我](http://evil)」这类字符串在卡片里变成可点击链接。"""
    return _MARKDOWN_SPECIAL_CHARS.sub(r"\\\1", text)


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


# 飞书卡片请求体上限 30 KB（纯文本消息 150 KB）。正文分片阈值按 UTF-8 字节
# 卡到 12 KB，剩下的留给卡片结构与 JSON 转义；超过一片的纪要拆成多张卡按顺序
# 发全，绝不截断、也绝不用「点开看全文」的外链打发人——本机服务的地址在手机
# 上根本打不开。
_CARD_BODY_LIMIT_BYTES = 12000


def _split_oversized_block(block: str, limit_bytes: int) -> list[str]:
    """单个段落本身就超限时，退到按行切；一行还超限就按字符硬切。"""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in block.split("\n"):
        blob = len(line.encode("utf-8")) + 1
        while blob > limit_bytes:
            # 一行长到装不下（长表格行之类），只能硬切，保住「发得出去」。
            head = line
            while len(head.encode("utf-8")) > limit_bytes:
                head = head[: len(head) // 2]
            pieces.append(head)
            line = line[len(head) :]
            blob = len(line.encode("utf-8")) + 1
        if current and size + blob > limit_bytes:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += blob
    if current:
        pieces.append("\n".join(current))
    return [piece for piece in pieces if piece.strip()]


def split_markdown_for_cards(
    markdown: str, *, limit_bytes: int = _CARD_BODY_LIMIT_BYTES
) -> list[str]:
    """把纪要切成若干张卡装得下的片段，切点优先落在段落之间。

    短纪要原样返回一片——绝大多数会议都走这条路，多卡只是长会的兜底。
    """
    text = (markdown or "").strip("\n")
    if not text.strip():
        return []
    if len(text.encode("utf-8")) <= limit_bytes:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append("\n\n".join(current))
            current, size = [], 0

    for block in re.split(r"\n{2,}", text):
        if not block.strip():
            continue
        blob = len(block.encode("utf-8")) + 2
        if blob > limit_bytes:
            flush()
            chunks.extend(_split_oversized_block(block, limit_bytes))
            continue
        if current and size + blob > limit_bytes:
            flush()
        current.append(block)
        size += blob
    flush()
    return chunks


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


def _minutes_meta_line(recording_date: str | None, duration_ms: int | None) -> str:
    """录音日期 + 时长写成一句人话；两样都没有就只说纪要归档了。"""
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
    return f"{meta}，纪要已经写好归档。" if meta else "纪要已经写好归档。"


def _minutes_next_step(will_extract_tasks: bool) -> str:
    return (
        "接下来我从纪要里挑会后任务，挑完发一张确认卡过来，你点确认我才开工。"
        if will_extract_tasks
        else "任务抽取暂时没跑（LLM 没配好），纪要本身不受影响，在声档里可以随时手动重抽。"
    )


def build_minutes_ready_cards(
    *,
    meeting_title: str,
    recording_date: str | None,
    duration_ms: int | None,
    markdown: str,
    will_extract_tasks: bool = True,
) -> list[dict[str, Any]]:
    """纪要写好的通知卡：会议名 + 录音元信息 + 纪要全文 + 下一步。

    群里的人多半刚散会、在手机上看，纪要正文直接摊开——不摘要、不折成
    「点开看全文」的跳转，本机服务的地址在手机上打不开，链接等于没给。
    正文超过一张卡装得下的长度就顺序发多张，标题上标 n/N。
    """
    # 会议标题来自纪要 H1，常带「· 会议纪要」后缀被截掉后留个孤零零的分隔符。
    title = (meeting_title or "").strip().rstrip("·-—|·").strip() or "未命名录音"
    chunks = split_markdown_for_cards(markdown) or [""]
    total = len(chunks)
    cards: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        elements: list[dict[str, Any]] = []
        if index == 0:
            elements.append({"tag": "markdown", "content": f"**{title}**"})
            elements.append(
                {"tag": "markdown", "content": _minutes_meta_line(recording_date, duration_ms)}
            )
        if chunk:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": chunk})
        if index == total - 1:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": _minutes_next_step(will_extract_tasks)})
        header_title = "纪要写好了" if total == 1 else f"纪要写好了 · 全文 {index + 1}/{total}"
        cards.append(
            {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": header_title},
                },
                "body": {"elements": elements},
            }
        )
    return cards


def _task_buttons(task_id: str, extraction_id: int | None) -> dict[str, Any]:
    """确认/驳回 两个回调按钮并排（卡片 2.0 用 column_set，不支持 button_group）。

    value 同时带 extraction_id：任务已被重抽覆盖/删除时，listener 仍能按抽取
    批次重建卡片自愈，而不是死在 404 上。
    """
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
    """单条任务的灰底信息块：标题 + 建议/时间 + 会上原话（辅助决策）。"""
    meta: list[str] = []
    label = _assignee_label(task.get("assignee"))
    if label:
        meta.append(f"建议：{label}")
    anchor = _format_anchor_ms(task.get("anchor_ms"))
    if anchor:
        meta.append(f"时间：{anchor}")
    quote = (task.get("anchor_quote") or "").strip()
    inner: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{index}. {_escape_markdown(task['title'])}**"}
    ]
    if meta:
        inner.append({"tag": "markdown", "content": "　".join(meta)})
    if quote:
        inner.append(
            {"tag": "markdown", "content": f"**📌 会上原话**\n{_escape_markdown(quote)}"}
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {"tag": "column", "width": "weighted", "vertical_align": "top", "elements": inner}
        ],
    }


def _state_line(task: dict[str, Any], index: int) -> dict[str, Any]:
    """已确认/已驳回任务的状态展示：保留 来源/建议/原话 全部细节，便于追溯。"""
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
        {"tag": "markdown", "content": f"**{index}. {_escape_markdown(task['title'])}**　{state}"}
    ]
    if meta:
        inner.append({"tag": "markdown", "content": "　".join(meta)})
    quote = (task.get("anchor_quote") or "").strip()
    if quote:
        inner.append(
            {"tag": "markdown", "content": f"📌 会上原话：{_escape_markdown(quote)}"}
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {"tag": "column", "width": "weighted", "vertical_align": "top", "elements": inner}
        ],
    }


def _project_line(project_name: str | None) -> str:
    """任务卡上的只读项目行：任务跟着会议走，这里显示会议当前的项目。"""
    if project_name:
        return f"项目：{_escape_markdown(project_name)}"
    return "项目：还没定，到声档里选"


def build_task_draft_card(
    *,
    meeting_title: str,
    tasks: list[dict[str, Any]],
    interactive: bool = True,
    project_name: str | None = None,
) -> dict[str, Any]:
    """会后任务确认卡（卡片 2.0）：逐条任务灰底信息块 + 确认/驳回 按钮，不带「全部确认」，
    不放任何外链（本地化部署，外网访问不到）。

    回调按钮 value：{"action": "confirm|reject", "task_id":…, "extraction_id":…}

    interactive=False 时不放按钮：没有 card_listener 接管回调的实例（第二实例
    走自建应用直连，没有长连接订阅），按钮点下去不会有任何反应，不如不给。
    """
    source = meeting_title or "这场会"
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"从「{source}」里挑出 {len(tasks)} 件该跟进的事。\n"
                + (
                    "确认后我才开工，不该做的当场驳回。"
                    if interactive
                    else "到声档的任务池里确认，确认过我才开工。"
                )
            ),
        },
        {"tag": "markdown", "content": _project_line(project_name)},
        {"tag": "hr"},
    ]
    shown = 0
    for index, task in enumerate(tasks, 1):
        shown += 1
        elements.append(_task_text_block(task, index))
        if interactive:
            elements.append(_task_buttons(task["id"], task.get("extraction_id")))
        if shown >= 5:
            rest = len(tasks) - shown
            if rest > 0:
                elements.append(
                    {
                        "tag": "markdown",
                        "content": f"还有 {rest} 条没列进来，在声档的任务页一起处理。",
                    }
                )
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


def build_task_status_card(
    *,
    meeting_title: str,
    tasks: list[dict[str, Any]],
    all_done: bool = False,
    project_name: str | None = None,
) -> dict[str, Any]:
    """确认/驳回后的卡片重建：逐条保留 来源/建议/原话 全部细节（可追溯），
    已确认的显示状态无按钮，待确认的保留按钮。不放外链。

    project_name 没传时从任务上取（任务跟着会议走，同一批任务的项目相同）。"""
    pending = sum(1 for task in tasks if task.get("status") == "pending_confirm")
    lead = f"来自「{meeting_title or '这场会'}」"
    lead += "，都处理完了。" if all_done else f"，还剩 {pending} 条等你拿主意。"
    if project_name is None:
        project_name = next((task["project_name"] for task in tasks if task.get("project_name")), None)
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": lead},
        {"tag": "markdown", "content": _project_line(project_name)},
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


def _card(
    title: str,
    text: str,
    *,
    button_label: str | None = None,
    button_url: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": text}}
    ]
    if button_label and button_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": button_label},
                        "type": "primary",
                        "url": button_url,
                    }
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


class LarkNotifier:
    def __init__(
        self,
        db: Database,
        *,
        webhook_url: str,
        public_base_url: str,
        stall_cooldown_days: float = 2.0,
        chat_id: str = "",
        lark_cli_bin: str = "lark-cli",
        lark_tmux_socket: str = "~/.tmux-socket/cc",
        app_id: str = "",
        app_secret: str = "",
    ):
        self.db = db
        self.webhook_url = webhook_url
        self.chat_id = chat_id.strip()
        self.lark_cli_bin = lark_cli_bin
        self.tmux_socket = Path(lark_tmux_socket).expanduser()
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self._app_token = ""
        self._app_token_expires_at = 0.0
        self.public_base_url = public_base_url.rstrip("/")
        self.stall_cooldown_days = max(0.5, stall_cooldown_days)
        self._enabled = bool(self.chat_id) or self._validate_webhook(webhook_url)

    @property
    def direct_app(self) -> bool:
        """自建应用直连：配了 app_id + secret 就不再经 lark-cli 与 GUI tmux。"""
        return bool(self.app_id and self.app_secret and self.chat_id)

    @staticmethod
    def _validate_webhook(url: str) -> bool:
        """只接受 https 的飞书自定义机器人地址；其他值视为未配置，通知整体关闭。"""
        if not url:
            return False
        try:
            parsed = urllib_parse.urlsplit(url)
        except ValueError:
            return False
        return parsed.scheme == "https" and bool(parsed.hostname)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _sent(self, kind: str, ref_key: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM notifications WHERE kind=? AND ref_key=?",
            (kind, ref_key),
        )
        return row is not None

    def _record(self, kind: str, ref_key: str) -> None:
        self.db.execute(
            "INSERT INTO notifications(kind, ref_key, sent_at) VALUES (?, ?, ?)",
            (kind, ref_key, utc_now()),
        )

    def _post(self, payload: dict[str, Any]) -> bool:
        if self.direct_app:
            return self._post_via_app(payload["card"])
        if self.chat_id:
            return self._post_via_cli(payload)
        return self._post_via_webhook(payload)

    def _tenant_token(self) -> str:
        """自建应用的 tenant_access_token，按返回的有效期缓存，提前 5 分钟换新。"""
        if self._app_token and time.time() < self._app_token_expires_at:
            return self._app_token
        body = json.dumps(
            {"app_id": self.app_id, "app_secret": self.app_secret}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib_request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib_request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read(64 * 1024).decode("utf-8"))
        if result.get("code") != 0 or not result.get("tenant_access_token"):
            raise ValueError(f"tenant_access_token 获取失败：{result.get('msg')}")
        self._app_token = result["tenant_access_token"]
        self._app_token_expires_at = time.time() + max(60, int(result.get("expire", 7200)) - 300)
        return self._app_token

    def _post_via_app(self, card: dict[str, Any]) -> bool:
        """以自建应用身份直接调开放平台发卡片（第二实例走这条，无 keychain 依赖）。"""
        try:
            token = self._tenant_token()
            body = json.dumps(
                {
                    "receive_id": self.chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib_request.Request(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                data=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {token}",
                },
            )
            with urllib_request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read(64 * 1024).decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError):
            self._app_token = ""  # 凭证可能已失效，下次重新换
            return False
        return isinstance(result, dict) and result.get("code") == 0

    def _post_via_cli(self, payload: dict[str, Any]) -> bool:
        """经 lark-cli 以应用机器人身份发卡片；im/v1/messages 的 content
        是卡片本体的 JSON 字符串，不带外层 msg_type 包装。"""
        data = json.dumps(
            {
                "receive_id": self.chat_id,
                "msg_type": "interactive",
                "content": json.dumps(payload["card"], ensure_ascii=False),
            },
            ensure_ascii=False,
        )
        try:
            result = subprocess.run(
                [
                    self.lark_cli_bin, "api", "POST", "/open-apis/im/v1/messages",
                    "--as", "bot",
                    "--params", '{"receive_id_type":"chat_id"}',
                    "--data", data,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        # 输出可能混有 WARN 等前导行，从第一个花括号起解析。
        out = result.stdout.strip()
        brace = out.find("{")
        if brace < 0:
            return False
        try:
            parsed = json.loads(out[brace:])
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, dict) and parsed.get("code") == 0

    def _post_via_webhook(self, payload: dict[str, Any]) -> bool:
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib_request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib_request.urlopen(request, timeout=10) as response:
                body = response.read(64 * 1024)
            # 飞书自定义机器人业务失败时 HTTP 仍是 200，需校验响应里的 code。
            try:
                result = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return False
            if isinstance(result, dict) and result.get("code") not in (0, None):
                return False
            return True
        except (HTTPError, URLError, OSError, ValueError):
            return False

    def _send(
        self,
        kind: str,
        ref_key: str,
        title: str,
        text: str,
        *,
        button_label: str | None = None,
        button_url: str | None = None,
    ) -> bool:
        if not self.enabled or self._sent(kind, ref_key):
            return False
        if self._post(_card(title, text, button_label=button_label, button_url=button_url)):
            self._record(kind, ref_key)
            return True
        return False

    def _send_payloads(self, kind: str, ref_key: str, payloads: list[dict[str, Any]]) -> bool:
        """webhook 通道的多条顺序发送：首条成功就记账，后续失败只丢续篇不重刷。"""
        if not self.enabled or self._sent(kind, ref_key) or not payloads:
            return False
        first_ok = False
        for index, payload in enumerate(payloads):
            if self._post(payload):
                if index == 0:
                    first_ok = True
                    self._record(kind, ref_key)
                continue
            if index == 0:
                return False
            break
        return first_ok

    def minutes_ready(
        self,
        minutes_version_id: str,
        *,
        meeting_title: str,
        recording_date: str | None = None,
        duration_ms: int | None = None,
        markdown: str = "",
        will_extract_tasks: bool = True,
    ) -> bool:
        """纪要写好的通知（三次触达的第二环），正文全文直发。

        ref_key 用纪要版本 id：同一份纪要只惊动群里一次，用户在声档改纪要
        产生新版本才会再发，重新抽取任务不会重复打扰。纪要太长时拆成多张卡
        顺序发完，只要第一张发出去就记账——宁可漏掉续篇，也不能整篇重刷。
        """
        if not self.chat_id:
            # webhook 通道：卡片 1.0 纯文本，同样是全文，同样不给跳转链接。
            title = (meeting_title or "").strip().rstrip("·-—|·").strip() or "未命名录音"
            chunks = split_markdown_for_cards(markdown) or [""]
            payloads = []
            for index, chunk in enumerate(chunks):
                lines = []
                if index == 0:
                    lines.append(f"**{title}**")
                    lines.append(_minutes_meta_line(recording_date, duration_ms))
                if chunk:
                    lines.extend(["", chunk])
                if index == len(chunks) - 1:
                    lines.extend(["", _minutes_next_step(will_extract_tasks)])
                card_title = (
                    "纪要写好了"
                    if len(chunks) == 1
                    else f"纪要写好了 · 全文 {index + 1}/{len(chunks)}"
                )
                payloads.append(_card(card_title, "\n".join(lines)))
            return self._send_payloads("minutes", minutes_version_id, payloads)
        cards = build_minutes_ready_cards(
            meeting_title=meeting_title,
            recording_date=recording_date,
            duration_ms=duration_ms,
            markdown=markdown,
            will_extract_tasks=will_extract_tasks,
        )
        return self._send_cards(cards, kind="minutes", ref_key=minutes_version_id)

    def task_draft(
        self,
        extraction_id: int,
        *,
        meeting_title: str,
        tasks: list[dict[str, Any]],
        project_name: str | None = None,
    ) -> bool:
        """会后任务确认卡。优先 应用机器人身份发卡片2.0（带回调按钮，可在飞书直接
        确认）；未配 应用机器人群时退回 群 webhook 机器人 的简单通知。"""
        if not self.chat_id:
            lines = [
                f"从「{meeting_title or '这场会'}」里挑出 {len(tasks)} 件该跟进的事，"
                "确认后我才开工。",
                _project_line(project_name),
                "",
            ]
            lines.extend(f"{i}. {t['title']}" for i, t in enumerate(tasks, 1))
            return self._send(
                "draft",
                str(extraction_id),
                f"会后任务待确认 · {len(tasks)} 条",
                "\n".join(lines),
                button_label="去声档确认",
                button_url=f"{self.public_base_url}/#tasks",
            )
        card = build_task_draft_card(
            meeting_title=meeting_title,
            tasks=tasks,
            interactive=not self.direct_app,
            project_name=project_name,
        )
        return self._send_app_card(card, kind="draft", ref_key=str(extraction_id))

    # ------------------------------------------------------------------ 应用机器人通道

    def _tmux_run(self, command: str) -> subprocess.CompletedProcess:
        """在 GUI 会话的 tmux 里执行命令（keychain 已解锁），声档服务进程本身在
        ssh→tmux（keychain 锁定）里跑，lark-cli 必须这样转发。"""
        return subprocess.run(
            ["tmux", "-S", str(self.tmux_socket), "run-shell", "-b", command],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _send_cards(self, cards: list[dict], *, kind: str, ref_key: str) -> bool:
        """多张卡顺序发同一条通知（长纪要的续篇）：首张成功即记账，避免重刷。"""
        if not cards or self._sent(kind, ref_key):
            return False
        if not self._send_app_card(cards[0], kind=kind, ref_key=ref_key):
            return False
        for card in cards[1:]:
            if not self._post_card(card):
                break
        return True

    def _post_card(self, card: dict) -> bool:
        """把一张卡片 2.0 发出去，不碰幂等台账。直连优先，否则经 lark-cli。"""
        if self.direct_app:
            return self._post_via_app(card)
        return self._post_app_card(card)

    def _send_app_card(self, card: dict, *, kind: str, ref_key: str) -> bool:
        """发一张交互卡片并记账（幂等）；直连可用时不经 lark-cli。"""
        if self._sent(kind, ref_key):
            return False
        if self.direct_app:
            if not self._post_via_app(card):
                return False
            self._record(kind, ref_key)
            return True
        return self._send_app_card_via_cli(card, kind=kind, ref_key=ref_key)

    def _post_app_card(self, card: dict) -> bool:
        """lark-cli 通道的纯发送（不记账），供续篇复用。"""
        return self._send_app_card_via_cli(card, kind="", ref_key="")

    def _send_app_card_via_cli(self, card: dict, *, kind: str, ref_key: str) -> bool:
        """以 应用机器人 身份经 lark-cli 发交互卡片到群；结果写日志文件轮询确认成功才记账。

        kind 为空表示只发不记账（长纪要的续篇走这条）。
        """
        log_dir = Path.home() / ".meeting-workbench" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        slug = f"{kind}-{ref_key}" if kind else f"seq-{int(time.time() * 1000)}"
        log_path = log_dir / f"card-send-{slug}.log"
        if log_path.exists():
            log_path.unlink()
        payload = json.dumps(
            {
                "receive_id": self.chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            ensure_ascii=False,
        )
        inner = (
            "export LARK_CLI_NO_PROXY=1; "
            f"PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin "
            f"{shlex.quote(self.lark_cli_bin)} api POST /open-apis/im/v1/messages "
            "--as bot --params '{\"receive_id_type\":\"chat_id\"}' "
            f"--data {shlex.quote(payload)} > {shlex.quote(str(log_path))} 2>&1"
        )
        try:
            result = self._tmux_run(inner)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        for _ in range(20):
            if not log_path.exists():
                time.sleep(0.5)
                continue
            text = log_path.read_text(errors="ignore")
            brace = text.find("{")
            if brace < 0:
                time.sleep(0.5)
                continue
            try:
                parsed = json.loads(text[brace:])
            except json.JSONDecodeError:
                time.sleep(0.5)
                continue
            if parsed.get("code") == 0:
                if kind:
                    self._record(kind, ref_key)
                return True
            return False  # 可解析但 code 非 0：发送失败，不记账
        return False

    def stall_reminder(self, task: dict[str, Any]) -> bool:
        text = (
            f"「{task['title']}」已停滞 {task['stall_days']:.0f} 天，"
            f"最近一次动态是 {task['stall_since'][:10]}。"
        )
        chips = []
        if task.get("project_name"):
            chips.append(task["project_name"])
        chips.append("我来做" if task.get("assignee") == "me" else "交给 AI")
        if chips:
            text += f"（{' · '.join(chips)}）"
        text += "\n可以回一句进展、转给 AI 执行，或直接取消任务。"
        # 按冷却天数分桶：同一任务在同一个冷却桶内只提醒一次。
        bucket = int(datetime.now(UTC).timestamp() // (self.stall_cooldown_days * 86400))
        return self._send(
            "stall",
            f"{task['id']}:{bucket}",
            "任务停滞提醒",
            text,
            button_label="打开任务",
            button_url=f"{self.public_base_url}/#tasks",
        )

    def daily_digest(self, stats: dict[str, Any]) -> bool:
        auto = int(stats.get("auto_assigned_yesterday") or 0)
        review = int(stats.get("needs_review") or 0)
        if int(stats.get("total") or 0) == 0 and not auto and not review:
            return False
        parts: list[str] = []
        if int(stats.get("total") or 0):
            parts.append(f"· 待确认 {stats['pending']} 条" + (
                f"，都来自「{stats['pending_sources'][0]}」" if stats["pending_sources"] else ""
            ) + "，确认后 AI 才会开工。")
        if stats["stalled"]:
            parts.append(f"· 停滞点名：{stats['stalled_titles'][0]}（{stats['stalled_days'][0]} 天没动了）。")
        if stats["done_today"]:
            parts.append(f"· 今天完成 {len(stats['done_today'])} 条：{stats['done_today'][0]}。")
        if auto or review:
            line = f"· 昨天自动归属 {auto} 场" if auto else "· 昨天没有自动归属的会"
            line += f"，{review} 场等你选项目。" if review else "。"
            parts.append(line)
        text = "\n".join(parts)
        return self._send(
            "digest",
            utc_now()[:10],
            f"今日任务晨报 · {utc_now()[:10]}",
            text,
            button_label="打开声档",
            button_url=f"{self.public_base_url}/#tasks",
        )
