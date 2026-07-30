from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "template", "svg", "iframe", "object"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "svg", "iframe", "object"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif not self.ignored_depth and tag.lower() in {"p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def imported_html_to_text(content: str) -> str:
    parser = _TextExtractor()
    parser.feed(content)
    parser.close()
    return parser.text()


def render_transcript_txt(segments: list[dict[str, Any]]) -> str:
    lines = []
    for segment in segments:
        speaker = segment.get("speaker_name") or segment.get("speaker_label")
        lines.append(f"{speaker}：{segment['text']}" if speaker else str(segment["text"]))
    return "\n".join(lines)


def format_srt_time(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_transcript_srt(segments: list[dict[str, Any]]) -> str | None:
    """纪要协议 v3 交给 Agent 的逐字稿格式。

    这份文本的哈希会写进 attempt manifest，回收纪要时要按同样的规则重算，
    所以生成端和校验端必须共用这一个实现。数据不合法时返回 None 由调用方决定报错方式。
    """
    if not segments:
        return None
    blocks: list[str] = []
    previous_start = -1
    for index, segment in enumerate(segments, start=1):
        start_ms = int(segment.get("start_ms", 0))
        end_ms = int(segment.get("end_ms", start_ms))
        text = str(segment.get("text") or "").strip()
        if not text or start_ms < previous_start or end_ms <= start_ms:
            return None
        previous_start = start_ms
        speaker = segment.get("speaker_name") or segment.get("speaker_label")
        rendered = f"{speaker}：{text}" if speaker else text
        blocks.append(
            f"{index}\n{format_srt_time(start_ms)} --> {format_srt_time(end_ms)}\n{rendered}"
        )
    return "\n\n".join(blocks) + "\n"


def render_safe_markdown(markdown: str) -> str:
    body: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def flush_paragraph() -> None:
        if paragraph:
            body.append(f"<p>{'<br>'.join(paragraph)}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^[-*+]\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            body.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
        elif bullet:
            flush_paragraph()
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{html.escape(bullet.group(1))}</li>")
        elif not line:
            flush_paragraph()
            close_list()
        else:
            close_list()
            paragraph.append(html.escape(line))
    flush_paragraph()
    close_list()
    rendered = "\n".join(body) or "<p></p>"
    return (
        """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
  <meta name="referrer" content="no-referrer">
  <title>会议纪要</title>
  <style>body{max-width:860px;margin:40px auto;padding:0 24px;color:#1d2925;font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}h1,h2,h3{line-height:1.35}li{margin:.35em 0}</style>
</head>
<body>
"""
        + rendered
        + "\n</body>\n</html>\n"
    )


def safe_imported_minutes_html(markdown: str, imported_html: str | None) -> str:
    if markdown.strip():
        return render_safe_markdown(markdown)
    return render_safe_markdown(imported_html_to_text(imported_html or ""))
