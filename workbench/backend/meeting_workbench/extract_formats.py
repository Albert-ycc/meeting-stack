"""各种文档格式的正文读取（第三期 3b），在读取进程（extract_worker）里跑，只用标准库。

每个读取函数往 Collector 里一块一块加文字（loc 是「第 3 页」「表『预算』」这类位置）。
读不了抛 Unreadable(原因)；PermissionError、其余 OSError 原样抛出，由读取进程分成 permission、
io_error。上限都在这里执行：读到 20 万字就停；压缩包成员声明超过 64MB 的不读、实际最多读 50MB；
表格每格 200 字、每行 200 列、每个表 2 万行。
"""

from __future__ import annotations

import codecs
import csv
import email
import email.policy
import io
import json
import os
import posixpath
import re
import struct
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET

MAX_CHARS = 200_000
PLAIN_MAX_BYTES = 8 * 1024 * 1024
MEMBER_DECLARED_LIMIT = 64 * 1024 * 1024
MEMBER_READ_LIMIT = 50 * 1024 * 1024
CELL_CHARS = 200
ROW_COLUMNS = 200
SHEET_ROWS = 20_000
CSV_BLOCK_ROWS = 50
TEXTUTIL_TIMEOUT = 45
STREAM_READ_LIMIT = 50 * 1024 * 1024

PASSWORD = "password"
CORRUPT = "corrupt"
UNSUPPORTED = "unsupported"

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class Unreadable(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


class Full(Exception):
    """收满 20 万字。"""


class EngineMissing(Exception):
    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class Collector:
    def __init__(self, max_chars: int | None = None):
        self.max_chars = MAX_CHARS if max_chars is None else max_chars
        self.blocks: list[dict[str, Any]] = []
        self.chars = 0
        self.truncated = False
        self.pages: int | None = None

    def add(self, text: str, loc: str | None = None) -> None:
        text = text.replace("\x00", "").strip()
        if not text:
            return
        room = self.max_chars - self.chars
        if room <= 0:
            self.truncated = True
            raise Full()
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self.blocks.append({"loc": loc, "text": text})
        self.chars += len(text)
        if self.truncated:
            raise Full()


# ---------------------------------------------------------------------- 认格式


def sniff(path: Path, ext: str) -> str:
    """看开头几个字节再信扩展名。返回：plain、csv、ipynb、email、html、ooxml、odf、epub、cfb、rtf、
    rtfd、pdf、iwork、binary。"""
    ext = ext.lower()
    if path.is_dir():
        return (
            "rtfd" if ext == "rtfd" else "iwork" if ext in {"key", "pages", "numbers"} else "binary"
        )
    with path.open("rb") as handle:
        head = handle.read(8192)
    stripped = head.lstrip(b"\xef\xbb\xbf").lstrip()
    lower = stripped[:512].lower()
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        if ext in {"key", "pages", "numbers"}:
            return "iwork"
        return "zip"
    if head.startswith(OLE_MAGIC):
        return "cfb"
    if stripped.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF8)):
        return _text_kind(ext)
    if lower.startswith((b"<html", b"<!doctype html", b"<?xml")) and ext in {
        "doc",
        "xls",
        "html",
        "htm",
        "wps",
        "et",
        "mht",
        "mhtml",
    }:
        if b"<html" in lower or b"<!doctype html" in lower:
            return "html"
    if lower.startswith((b"<html", b"<!doctype html")):
        return "html" if ext not in {"xml", "svg"} else _text_kind(ext)
    if lower.startswith(b"mime-version:") or (
        ext in {"mht", "mhtml", "eml"} and b"content-type:" in lower
    ):
        return "email"
    if b"\x00" in head:
        return "binary"
    if ext in {"xls", "xlt", "et"}:
        return "csv"  # 网上系统导出的「其实是制表符文本的 .xls」
    if ext in {
        "doc",
        "dot",
        "wps",
        "ppt",
        "pps",
        "dps",
        "docx",
        "xlsx",
        "pptx",
        "odt",
        "ods",
        "odp",
        "epub",
    }:
        return "plain"
    return _text_kind(ext)


def _text_kind(ext: str) -> str:
    if ext in {"csv", "tsv"}:
        return "csv"
    if ext == "ipynb":
        return "ipynb"
    if ext in {"eml", "mht", "mhtml"}:
        return "email"
    if ext in {"html", "htm"}:
        return "html"
    return "plain"


# ---------------------------------------------------------------------- 纯文字


def decode_bytes(data: bytes, *, cut: bool = False) -> str:
    """依次试 BOM、严格 utf-8、严格 gb18030（中文 Windows 存的 GBK）、utf-8 把乱码替换掉。
    cut=True 表示只读了前面一部分：末尾被截断的半个字不算乱码。"""
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return codecs.getincrementaldecoder(encoding)(errors="replace").decode(
                data, final=not cut
            )
    for encoding in ("utf-8", "gb18030"):
        try:
            return codecs.getincrementaldecoder(encoding)().decode(data, final=not cut)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_limited(path: Path, limit: int | None = None) -> tuple[bytes, bool]:
    limit = PLAIN_MAX_BYTES if limit is None else limit
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    return data[:limit], len(data) > limit


def _check_binary(data: bytes) -> None:
    head = data[:8192]
    if b"\x00" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise Unreadable(UNSUPPORTED, "扩展名是文字，内容是二进制")


def _split_paragraphs(text: str) -> Iterator[str]:
    for paragraph in re.split(r"\n\s*\n", text):
        if paragraph.strip():
            yield paragraph


def read_plain(path: Path, out: Collector) -> None:
    data, cut = _read_limited(path)
    _check_binary(data)
    if cut:
        out.truncated = True
    for paragraph in _split_paragraphs(decode_bytes(data, cut=cut)):
        out.add(paragraph)


def read_csv(path: Path, out: Collector, *, ext: str) -> None:
    data, cut = _read_limited(path)
    _check_binary(data)
    if cut:
        out.truncated = True
    text = decode_bytes(data, cut=cut)
    sample = text[:4096]
    delimiter = (
        "\t"
        if ext in {"tsv", "xls", "xlt", "et"} or sample.count("\t") > sample.count(",")
        else ","
    )
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[str] = []
    try:
        for row in reader:
            cells = [cell.strip()[:CELL_CHARS] for cell in row[:ROW_COLUMNS]]
            if any(cells):
                rows.append("\t".join(cells))
            if len(rows) >= CSV_BLOCK_ROWS:
                out.add("\n".join(rows))
                rows = []
    except csv.Error:
        pass
    if rows:
        out.add("\n".join(rows))


def read_ipynb(path: Path, out: Collector) -> None:
    data, cut = _read_limited(path)
    try:
        notebook = json.loads(decode_bytes(data))
    except ValueError as error:
        if cut:
            out.truncated = True
            return
        raise Unreadable(CORRUPT, "笔记本不是 JSON") from error
    for cell in notebook.get("cells", []) if isinstance(notebook, dict) else []:
        source = cell.get("source", "")
        out.add("".join(source) if isinstance(source, list) else str(source))
        for output in cell.get("outputs", []) or []:
            text = output.get("text")
            if text is None:
                text = (output.get("data") or {}).get("text/plain")
            if text:
                out.add("".join(text) if isinstance(text, list) else str(text))


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head"}
    BREAK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "table",
        "blockquote",
        "pre",
        "title",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            if tag == "head":
                return
            self._skip += 1
        if tag == "title":
            self._title = True
        if tag in self.BREAK:
            self.parts.append("\n\n" if tag != "br" else "\n")
        if tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and tag != "head" and self._skip:
            self._skip -= 1
        if tag == "title":
            self._title = False
            self.parts.append("\n\n")
        if tag in self.BREAK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"[ \t\r\f\v]+\n", "\n", "".join(self.parts))


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_\-]+)""", re.IGNORECASE)


def decode_html(data: bytes, *, cut: bool = False) -> str:
    match = _META_CHARSET.search(data[:4096])
    if match:
        encoding = match.group(1).decode("ascii", errors="ignore").lower()
        if encoding in {"gb2312", "gbk"}:
            encoding = "gb18030"
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            pass
    return decode_bytes(data, cut=cut)


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    return parser.text()


def read_html(path: Path, out: Collector) -> None:
    data, cut = _read_limited(path)
    if cut:
        out.truncated = True
    for paragraph in _split_paragraphs(html_to_text(decode_html(data, cut=cut))):
        out.add(paragraph)


def _fix_surrogates(text: str) -> str:
    """没声明编码的 8 位字节在标准库里会变成代理字符：按字节重新猜编码。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return decode_bytes(text.encode("utf-8", errors="surrogateescape"))


def _part_text(part: Any) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset()
    if charset:
        charset = "gb18030" if charset.lower() in {"gb2312", "gbk"} else charset
        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass
    return decode_bytes(payload)


def read_email(path: Path, out: Collector) -> None:
    data, cut = _read_limited(path)
    if cut:
        out.truncated = True
    message = email.message_from_bytes(data, policy=email.policy.default)
    subject = message.get("subject")
    if subject:
        out.add(_fix_surrogates(str(subject)), "主题")
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in {"text/plain", "text/html"}:
            continue
        (plain if kind == "text/plain" else html).append(_part_text(part))
    texts = plain or [html_to_text(item) for item in html]
    for text in texts:
        for paragraph in _split_paragraphs(text):
            out.add(paragraph)


# ---------------------------------------------------------------------- 压缩包里的 XML


class _LimitedReader(io.RawIOBase):
    """压缩包成员最多读 50MB，超过就当读完（记 truncated）。"""

    def __init__(self, inner: IO[bytes], limit: int):
        self.inner = inner
        self.remaining = limit
        self.hit = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self.remaining <= 0:
            self.hit = True
            return 0
        want = min(len(buffer), self.remaining)
        data = self.inner.read(want)
        buffer[: len(data)] = data
        self.remaining -= len(data)
        return len(data)


class Package:
    """OOXML、ODF、EPUB 这类 zip 包。"""

    def __init__(self, path: Path, out: Collector):
        self.out = out
        try:
            self.archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as error:
            raise Unreadable(CORRUPT, "压缩包打不开") from error
        self.names = {info.filename: info for info in self.archive.infolist()}

    def close(self) -> None:
        self.archive.close()

    def has(self, name: str) -> bool:
        return name in self.names

    def read(self, name: str) -> bytes | None:
        info = self.names.get(name)
        if info is None:
            return None
        if info.file_size > MEMBER_DECLARED_LIMIT:
            self.out.truncated = True
            return None
        with self.archive.open(info) as handle:
            reader = _LimitedReader(handle, MEMBER_READ_LIMIT)
            data = reader.read() if hasattr(reader, "read") else b""
            if reader.hit or len(data) >= MEMBER_READ_LIMIT:
                self.out.truncated = True
            return data

    def iterparse(
        self, name: str, events: tuple[str, ...] = ("end",)
    ) -> Iterator[tuple[str, ET.Element]]:
        info = self.names.get(name)
        if info is None:
            return
        if info.file_size > MEMBER_DECLARED_LIMIT:
            self.out.truncated = True
            return
        with self.archive.open(info) as handle:
            reader = _LimitedReader(handle, MEMBER_READ_LIMIT)
            stream = io.BufferedReader(reader)
            try:
                yield from ET.iterparse(stream, events=events)
            except ET.ParseError:
                if reader.hit:
                    self.out.truncated = True
                    return
                raise


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _rels(package: Package, part: str) -> dict[str, str]:
    """part 的关系：rId → 包里的路径。"""
    folder, _, name = part.rpartition("/")
    rels_name = f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"
    data = package.read(rels_name)
    if not data:
        return {}
    result: dict[str, str] = {}
    for element in ET.fromstring(data):
        target = element.get("Target") or ""
        if element.get("TargetMode") == "External":
            continue
        resolved = (
            posixpath.normpath(posixpath.join(folder, target))
            if not target.startswith("/")
            else target[1:]
        )
        result[element.get("Id") or ""] = resolved
    return result


def _word_paragraphs(package: Package, part: str) -> Iterator[str]:
    """docx 的一个部件里一段一段的文字：w:t 是字，w:tab 是制表符，w:br、w:cr 是换行。"""
    for _event, element in package.iterparse(part):
        if _local(element.tag) != "p":
            continue
        pieces: list[str] = []
        for child in element.iter():
            name = _local(child.tag)
            if name in {"t", "delText"} and child.text:
                if name == "t":
                    pieces.append(child.text)
            elif name == "tab":
                pieces.append("\t")
            elif name in {"br", "cr"}:
                pieces.append("\n")
        element.clear()
        text = "".join(pieces)
        if text.strip():
            yield text


def read_docx(package: Package, out: Collector) -> None:
    main = next(
        (name for name in ("word/document.xml", "word/document2.xml") if package.has(name)), None
    )
    if main is None:
        raise Unreadable(CORRUPT, "缺主文件")
    for text in _word_paragraphs(package, main):
        out.add(text)
    extras = sorted(
        (name for name in package.names if re.fullmatch(r"word/(header|footer)\d*\.xml", name)),
    )
    for name in extras:
        loc = "页眉" if "header" in name else "页脚"
        for text in _word_paragraphs(package, name):
            out.add(text, loc)
    for name, loc in (
        ("word/footnotes.xml", "脚注"),
        ("word/endnotes.xml", "尾注"),
        ("word/comments.xml", "批注"),
    ):
        if package.has(name):
            for text in _word_paragraphs(package, name):
                out.add(text, loc)


def format_number(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return ""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


class SheetWriter:
    """一个表：格子之间用制表符、行之间换行；每格 200 字、每行 200 列、每表 2 万行。"""

    def __init__(self, out: Collector, name: str):
        self.out = out
        self.loc = f"表「{name}」" if name else None
        self.lines: list[str] = []
        self.rows = 0
        self.chars = 0

    def row(self, cells: list[str]) -> bool:
        """加一行；到了 2 万行返回 False。"""
        if self.rows >= SHEET_ROWS:
            self.out.truncated = True
            return False
        cells = [
            cell.replace("\t", " ").replace("\n", " ").strip()[:CELL_CHARS]
            for cell in cells[:ROW_COLUMNS]
        ]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            return True
        line = "\t".join(cells)
        self.lines.append(line)
        self.rows += 1
        self.chars += len(line) + 1
        if self.chars >= 4000:
            self.flush()
        return True

    def flush(self) -> None:
        if self.lines:
            text = "\n".join(self.lines)
            self.lines = []
            self.chars = 0
            self.out.add(text, self.loc)


def _column_index(reference: str) -> int:
    letters = "".join(ch for ch in reference if ch.isalpha()).upper()
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - 64)
    return max(index - 1, 0)


def read_xlsx(package: Package, out: Collector) -> None:
    if not package.has("xl/workbook.xml"):
        raise Unreadable(CORRUPT, "缺主文件")
    shared: list[str] = []
    shared_chars = 0
    for _event, element in package.iterparse("xl/sharedStrings.xml"):
        if _local(element.tag) != "si":
            continue
        text = "".join(node.text or "" for node in element.iter() if _local(node.tag) == "t")
        if shared_chars < 4 * MAX_CHARS:
            shared.append(text[:CELL_CHARS])
            shared_chars += min(len(text), CELL_CHARS)
        else:
            shared.append("")
        element.clear()
    sheets: list[tuple[str, str]] = []
    rels = _rels(package, "xl/workbook.xml")
    workbook = package.read("xl/workbook.xml") or b""
    for element in ET.fromstring(workbook).iter():
        if _local(element.tag) == "sheet":
            rid = next((value for key, value in element.attrib.items() if _local(key) == "id"), "")
            target = rels.get(rid)
            if target:
                sheets.append((element.get("name") or "", target))
    for name, target in sheets:
        sheet = SheetWriter(out, name)
        stop = False
        for _event, element in package.iterparse(target):
            if stop:
                element.clear()
                continue
            if _local(element.tag) != "row":
                continue
            cells: dict[int, str] = {}
            for cell in element:
                if _local(cell.tag) != "c":
                    continue
                kind = cell.get("t")
                value = ""
                if kind == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.iter() if _local(node.tag) == "t"
                    )
                else:
                    raw = next((node.text for node in cell if _local(node.tag) == "v"), None)
                    if raw is None:
                        continue
                    if kind == "s":
                        try:
                            value = shared[int(raw)]
                        except (ValueError, IndexError):
                            value = ""
                    elif kind in {"str", "e"}:
                        value = raw
                    elif kind == "b":
                        value = "TRUE" if raw == "1" else "FALSE"
                    else:
                        try:
                            value = format_number(float(raw))
                        except ValueError:
                            value = raw
                column = _column_index(cell.get("r") or "") if cell.get("r") else len(cells)
                if column < ROW_COLUMNS:
                    cells[column] = value
            element.clear()
            if cells:
                row = [cells.get(index, "") for index in range(max(cells) + 1)]
                if not sheet.row(row):
                    stop = True
        sheet.flush()


def read_pptx(package: Package, out: Collector) -> None:
    if not package.has("ppt/presentation.xml"):
        raise Unreadable(CORRUPT, "缺主文件")
    rels = _rels(package, "ppt/presentation.xml")
    order: list[str] = []
    for element in ET.fromstring(package.read("ppt/presentation.xml") or b"<x/>").iter():
        if _local(element.tag) == "sldId":
            rid = next(
                (
                    value
                    for key, value in element.attrib.items()
                    if _local(key) == "id" and key != "id"
                ),
                "",
            )
            if rid in rels:
                order.append(rels[rid])
    if not order:
        order = sorted(
            (name for name in package.names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.findall(r"\d+", name)[-1]),
        )
    out.pages = len(order)
    for number, slide in enumerate(order, start=1):
        loc = f"第 {number} 页"
        lines = list(_drawing_paragraphs(package, slide))
        out.add("\n".join(lines), loc)
        notes = next(
            (target for target in _rels(package, slide).values() if "notesSlide" in target), None
        )
        if notes:
            note_lines = [
                line for line in _drawing_paragraphs(package, notes) if not line.strip().isdigit()
            ]
            out.add("\n".join(note_lines), f"{loc} 备注")


def _drawing_paragraphs(package: Package, part: str) -> Iterator[str]:
    for _event, element in package.iterparse(part):
        if _local(element.tag) != "p" or not element.tag.startswith(
            "{http://schemas.openxmlformats.org/drawingml"
        ):
            continue
        pieces = []
        for node in element.iter():
            name = _local(node.tag)
            if name == "t" and node.text:
                pieces.append(node.text)
            elif name == "br":
                pieces.append("\n")
        element.clear()
        text = "".join(pieces)
        if text.strip():
            yield text


ODF_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODF_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
ODF_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"


def _odf_text(element: ET.Element) -> str:
    pieces: list[str] = []

    def walk(node: ET.Element) -> None:
        if node.text:
            pieces.append(node.text)
        for child in node:
            name = _local(child.tag)
            if name == "tab":
                pieces.append("\t")
            elif name == "line-break":
                pieces.append("\n")
            elif name == "s":
                pieces.append(" " * int(child.get(f"{{{ODF_TEXT}}}c") or 1))
            walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(element)
    return "".join(pieces)


def read_odf(package: Package, out: Collector) -> None:
    manifest = package.read("META-INF/manifest.xml") or b""
    if b"encryption-data" in manifest:
        raise Unreadable(PASSWORD, "ODF 加密")
    if not package.has("content.xml"):
        raise Unreadable(CORRUPT, "缺主文件")
    page = 0
    sheet: SheetWriter | None = None
    depth_table = 0
    for event, element in package.iterparse("content.xml", events=("start", "end")):
        tag = element.tag
        if event == "start":
            if tag == f"{{{ODF_TABLE}}}table":
                depth_table += 1
                if depth_table == 1:
                    sheet = SheetWriter(out, element.get(f"{{{ODF_TABLE}}}name") or "")
            elif tag == f"{{{ODF_DRAW}}}page":
                page += 1
            continue
        if tag == f"{{{ODF_TABLE}}}table-row" and sheet is not None and depth_table == 1:
            cells: list[str] = []
            for cell in element:
                if _local(cell.tag) not in {"table-cell", "covered-table-cell"}:
                    continue
                text = "\n".join(_odf_text(p) for p in cell if _local(p.tag) in {"p", "h"})
                repeat = min(
                    int(cell.get(f"{{{ODF_TABLE}}}number-columns-repeated") or 1), ROW_COLUMNS
                )
                cells.extend([text] * (repeat if text else min(repeat, 1)))
            sheet.row(cells)
            element.clear()
        elif tag == f"{{{ODF_TABLE}}}table":
            depth_table -= 1
            if depth_table == 0 and sheet is not None:
                sheet.flush()
                sheet = None
            element.clear()
        elif tag in {f"{{{ODF_TEXT}}}p", f"{{{ODF_TEXT}}}h"} and depth_table == 0:
            out.add(_odf_text(element), f"第 {page} 页" if page else None)
            element.clear()
    if page:
        out.pages = page


def read_epub(package: Package, out: Collector) -> None:
    encryption = package.read("META-INF/encryption.xml")
    if encryption:
        protected = [
            element.get("URI") or ""
            for element in ET.fromstring(encryption).iter()
            if _local(element.tag) == "CipherReference"
        ]
        algorithms = [
            element.get("Algorithm") or ""
            for element in ET.fromstring(encryption).iter()
            if _local(element.tag) == "EncryptionMethod"
        ]
        font_only = all(
            "embedding" in algorithm or "font" in algorithm.lower() for algorithm in algorithms
        )
        if (
            any(uri.lower().endswith((".xhtml", ".html", ".htm", ".xml")) for uri in protected)
            and not font_only
        ):
            raise Unreadable(PASSWORD, "EPUB 加密")
    container = package.read("META-INF/container.xml")
    if not container:
        raise Unreadable(CORRUPT, "缺主文件")
    rootfile = next(
        (
            element.get("full-path")
            for element in ET.fromstring(container).iter()
            if _local(element.tag) == "rootfile"
        ),
        None,
    )
    opf = package.read(rootfile or "") if rootfile else None
    if not opf:
        raise Unreadable(CORRUPT, "缺主文件")
    folder = posixpath.dirname(rootfile or "")
    tree = ET.fromstring(opf)
    manifest = {
        element.get("id"): element.get("href")
        for element in tree.iter()
        if _local(element.tag) == "item"
    }
    spine = [element.get("idref") for element in tree.iter() if _local(element.tag) == "itemref"]
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        data = package.read(posixpath.normpath(posixpath.join(folder, href)))
        if data is None:
            continue
        for paragraph in _split_paragraphs(html_to_text(decode_html(data))):
            out.add(paragraph)


def read_zip(path: Path, out: Collector, *, ext: str) -> None:
    package = Package(path, out)
    try:
        names = package.names
        if "[Content_Types].xml" in names:
            if "word/document.xml" in names or "word/document2.xml" in names:
                read_docx(package, out)
            elif "xl/workbook.xml" in names:
                read_xlsx(package, out)
            elif "ppt/presentation.xml" in names:
                read_pptx(package, out)
            else:
                raise Unreadable(CORRUPT, "缺主文件")
            return
        mimetype = (package.read("mimetype") or b"").decode("ascii", errors="ignore").strip()
        if mimetype.startswith("application/vnd.oasis.opendocument") or ext in {
            "odt",
            "ods",
            "odp",
        }:
            read_odf(package, out)
            return
        if mimetype == "application/epub+zip" or ext == "epub":
            read_epub(package, out)
            return
        if ext in {
            "docx",
            "docm",
            "dotx",
            "xlsx",
            "xlsm",
            "xltx",
            "pptx",
            "pptm",
            "ppsx",
            "potx",
            "wps",
            "et",
            "dps",
        }:
            raise Unreadable(CORRUPT, "缺主文件")
        raise Unreadable(UNSUPPORTED, "不认识的压缩包")
    finally:
        package.close()


# ---------------------------------------------------------------------- 老 Office 复合文档


class CFBError(Exception):
    pass


class CompoundFile:
    """最小的复合文档（OLE2）读取：只按名字取根目录下的流。"""

    FREE = 0xFFFFFFFF
    END = 0xFFFFFFFE
    MAX_CHAIN = 1 << 22

    def __init__(self, handle: IO[bytes]):
        self.handle = handle
        handle.seek(0, os.SEEK_END)
        self.file_size = handle.tell()
        handle.seek(0)
        header = handle.read(512)
        if len(header) < 512 or header[:8] != OLE_MAGIC:
            raise CFBError("不是复合文档")
        self.sector_size = 1 << struct.unpack_from("<H", header, 0x1E)[0]
        self.mini_size = 1 << struct.unpack_from("<H", header, 0x20)[0]
        if self.sector_size not in (512, 4096) or self.mini_size != 64:
            raise CFBError("扇区大小不对")
        fat_sectors = struct.unpack_from("<I", header, 0x2C)[0]
        self.first_dir = struct.unpack_from("<I", header, 0x30)[0]
        self.cutoff = struct.unpack_from("<I", header, 0x38)[0]
        first_minifat = struct.unpack_from("<I", header, 0x3C)[0]
        difat_first = struct.unpack_from("<I", header, 0x44)[0]
        difat_count = struct.unpack_from("<I", header, 0x48)[0]
        # 文件里实际有几个扇区：文件头自报的个数、链长都不可信，沿链走以它为上界
        self.sector_count = self.file_size // self.sector_size
        difat = list(struct.unpack_from("<109I", header, 0x4C))
        next_difat = difat_first
        visited: set[int] = set()
        per = self.sector_size // 4
        while next_difat not in (self.END, self.FREE) and len(visited) <= difat_count:
            if next_difat in visited:
                raise CFBError("DIFAT 链绕圈")
            visited.add(next_difat)
            block = self._sector(next_difat)
            values = struct.unpack(f"<{per}I", block)
            difat.extend(values[:-1])
            next_difat = values[-1]
        fat_ids = [value for value in difat if value not in (self.FREE, self.END)][:fat_sectors]
        if len(set(fat_ids)) != len(fat_ids):
            raise CFBError("FAT 扇区重复")
        fat: list[int] = []
        for sector in fat_ids:
            fat.extend(struct.unpack(f"<{per}I", self._sector(sector)))
        self.fat = fat
        directory = self._chain_bytes(self.first_dir, None)
        self.entries: dict[str, tuple[int, int, int]] = {}
        root: tuple[int, int] | None = None
        for offset in range(0, len(directory) - 127, 128):
            entry = directory[offset : offset + 128]
            length = struct.unpack_from("<H", entry, 0x40)[0]
            kind = entry[0x42]
            if kind == 0 or length < 2 or length > 64:
                continue
            name = entry[: length - 2].decode("utf-16-le", errors="replace")
            start = struct.unpack_from("<I", entry, 0x74)[0]
            size = struct.unpack_from("<Q", entry, 0x78)[0]
            if self.sector_size == 512:
                size &= 0xFFFFFFFF
            if kind == 5:
                root = (start, size)
            elif kind == 2:
                self.entries.setdefault(name, (start, size, kind))
        self.mini_stream = b""
        self.minifat: list[int] = []
        if root is not None and root[1]:
            self.mini_stream = self._chain_bytes(root[0], root[1])
            if first_minifat not in (self.END, self.FREE):
                raw = self._chain_bytes(first_minifat, None)
                self.minifat = list(struct.unpack(f"<{len(raw) // 4}I", raw[: len(raw) // 4 * 4]))

    def _sector(self, index: int) -> bytes:
        offset = (index + 1) * self.sector_size
        if offset + self.sector_size > self.file_size + self.sector_size or index >= self.MAX_CHAIN:
            raise CFBError("扇区越界")
        self.handle.seek(offset)
        return self.handle.read(self.sector_size)

    def _chain(self, start: int, table: list[int], room: int) -> Iterator[int]:
        """沿扇区表走链。room 是实际装得下的扇区数：不绕圈的链不会比它长。"""
        current = start
        steps = 0
        limit = min(len(table), room)
        while current not in (self.END, self.FREE):
            if current >= len(table) or steps >= limit:
                raise CFBError("扇区链断了")
            yield current
            current = table[current]
            steps += 1

    def _chain_bytes(self, start: int, size: int | None) -> bytes:
        parts: list[bytes] = []
        total = 0
        limit = STREAM_READ_LIMIT if size is None else min(size, STREAM_READ_LIMIT)
        for sector in self._chain(start, self.fat, self.sector_count):
            parts.append(self._sector(sector))
            total += self.sector_size
            if total >= limit:
                break
        data = b"".join(parts)
        return data if size is None else data[: min(size, STREAM_READ_LIMIT)]

    def has(self, name: str) -> bool:
        return name in self.entries

    def stream(self, name: str) -> bytes:
        entry = self.entries.get(name)
        if entry is None:
            raise CFBError(f"缺 {name}")
        start, size, _kind = entry
        if size < self.cutoff:
            limit = min(size, STREAM_READ_LIMIT)
            room = -(-len(self.mini_stream) // self.mini_size)
            parts = []
            total = 0
            for sector in self._chain(start, self.minifat, room):
                offset = sector * self.mini_size
                parts.append(self.mini_stream[offset : offset + self.mini_size])
                total += self.mini_size
                if total >= limit:
                    break
            return b"".join(parts)[:limit]
        return self._chain_bytes(start, size)


def open_cfb(path: Path) -> tuple[CompoundFile, IO[bytes]]:
    handle = path.open("rb")
    try:
        return CompoundFile(handle), handle
    except CFBError as error:
        handle.close()
        raise Unreadable(CORRUPT, str(error)) from error
    except struct.error as error:
        handle.close()
        raise Unreadable(CORRUPT, "复合文档结构不对") from error


def cfb_kind(document: CompoundFile) -> str:
    if document.has("EncryptedPackage"):
        return "encrypted_ooxml"
    if document.has("WordDocument"):
        return "doc"
    if document.has("Workbook") or document.has("Book"):
        return "xls"
    if document.has("PowerPoint Document"):
        return "ppt"
    return "unknown"


def word_encrypted(document: CompoundFile) -> bool:
    fib = document.stream("WordDocument")[:12]
    if len(fib) < 12:
        raise Unreadable(CORRUPT, "Word 文件头不全")
    flags = struct.unpack_from("<H", fib, 0x0A)[0]
    return bool(flags & 0x0100)


class _Segments:
    """SST 和它后面的 CONTINUE：跨记录读字节；字符跨记录时新记录开头有一个标志字节。"""

    def __init__(self, parts: list[bytes]):
        self.parts = parts
        self.index = 0
        self.offset = 0

    def _ensure(self) -> None:
        while self.index < len(self.parts) and self.offset >= len(self.parts[self.index]):
            self.index += 1
            self.offset = 0

    def read(self, count: int) -> bytes:
        out = bytearray()
        while count > 0:
            self._ensure()
            if self.index >= len(self.parts):
                raise CFBError("SST 不全")
            part = self.parts[self.index]
            take = min(count, len(part) - self.offset)
            out += part[self.offset : self.offset + take]
            self.offset += take
            count -= take
        return bytes(out)

    def read_chars(self, count: int, high: bool) -> str:
        pieces: list[str] = []
        while count > 0:
            if self.index >= len(self.parts):
                raise CFBError("SST 不全")
            part = self.parts[self.index]
            width = 2 if high else 1
            available = (len(part) - self.offset) // width
            if available <= 0:
                # 字符串在记录边界断开：下一条 CONTINUE 开头是新的标志字节
                self.index += 1
                self.offset = 0
                if self.index >= len(self.parts):
                    raise CFBError("SST 不全")
                high = bool(self.parts[self.index][0] & 0x01)
                self.offset = 1
                continue
            take = min(count, available)
            raw = part[self.offset : self.offset + take * width]
            self.offset += take * width
            pieces.append(raw.decode("utf-16-le" if high else "latin-1", errors="replace"))
            count -= take
        return "".join(pieces)

    def at_record_start(self) -> bool:
        self._ensure()
        return self.offset == 0


def _parse_sst(parts: list[bytes]) -> list[str]:
    reader = _Segments(parts)
    _total, unique = struct.unpack("<II", reader.read(8))
    strings: list[str] = []
    for _ in range(unique):
        try:
            count = struct.unpack("<H", reader.read(2))[0]
            flags = reader.read(1)[0]
            runs = struct.unpack("<H", reader.read(2))[0] if flags & 0x08 else 0
            ext = struct.unpack("<I", reader.read(4))[0] if flags & 0x04 else 0
            text = reader.read_chars(count, bool(flags & 0x01))
            if runs:
                reader.read(4 * runs)
            if ext:
                reader.read(ext)
        except CFBError:
            break
        strings.append(text[:CELL_CHARS])
    return strings


def _short_string(data: bytes, offset: int) -> str:
    count = data[offset]
    high = data[offset + 1] & 0x01
    raw = data[offset + 2 : offset + 2 + count * (2 if high else 1)]
    return raw.decode("utf-16-le" if high else "latin-1", errors="replace")


def _unicode_string(data: bytes, offset: int) -> str:
    count = struct.unpack_from("<H", data, offset)[0]
    high = data[offset + 2] & 0x01
    raw = data[offset + 3 : offset + 3 + count * (2 if high else 1)]
    return raw.decode("utf-16-le" if high else "latin-1", errors="replace")


def _rk_value(raw: int) -> float:
    if raw & 0x02:
        value = float(raw >> 2 if not raw & 0x80000000 else (raw >> 2) - (1 << 30))
    else:
        value = struct.unpack("<d", struct.pack("<Q", (raw & 0xFFFFFFFC) << 32))[0]
    return value / 100 if raw & 0x01 else value


def read_xls(document: CompoundFile, out: Collector) -> None:
    """BIFF8：工作簿全局区的表名和共享字符串表，每个工作表的单元格。图表、宏表不读；工作表里嵌的
    图表有自己的 BOF…EOF，按层数跳过。"""
    name = "Workbook" if document.has("Workbook") else "Book"
    data = document.stream(name)
    position = 0
    sheet_names: list[str] = []
    sst: list[str] = []
    sst_parts: list[bytes] | None = None
    depth = 0
    sheet_index = -1
    in_sheet = False
    cells: dict[int, dict[int, str]] = {}
    pending_formula: tuple[int, int] | None = None

    def flush_sheet() -> None:
        sheet_name = sheet_names[sheet_index] if 0 <= sheet_index < len(sheet_names) else ""
        writer = SheetWriter(out, sheet_name)
        for row in sorted(cells):
            columns = cells[row]
            if not writer.row([columns.get(index, "") for index in range(max(columns) + 1)]):
                break
        writer.flush()

    while position + 4 <= len(data):
        kind, length = struct.unpack_from("<HH", data, position)
        body = data[position + 4 : position + 4 + length]
        position += 4 + length
        if sst_parts is not None:
            if kind == 0x003C:  # CONTINUE
                sst_parts.append(body)
                continue
            sst = _parse_sst(sst_parts)
            sst_parts = None
        if kind == 0x0809:  # BOF
            depth += 1
            if depth == 1:
                substream = struct.unpack_from("<H", body, 2)[0] if len(body) >= 4 else 0
                if substream != 0x0005:  # 不是工作簿全局区
                    sheet_index += 1
                    in_sheet = substream == 0x0010
                    cells = {}
            continue
        if kind == 0x000A:  # EOF
            if depth == 1 and in_sheet:
                flush_sheet()
                in_sheet = False
                cells = {}
            depth = max(depth - 1, 0)
            continue
        if depth == 1 and sheet_index < 0:
            if kind == 0x002F:  # FILEPASS
                raise Unreadable(PASSWORD, "Excel 加密")
            if kind == 0x0085 and len(body) >= 8:  # BOUNDSHEET
                try:
                    sheet_names.append(_short_string(body, 6))
                except IndexError:
                    sheet_names.append("")
            elif kind == 0x00FC:  # SST
                sst_parts = [body]
            continue
        if not (depth == 1 and in_sheet):
            continue
        if kind == 0x0207:  # STRING（前一个公式的文字结果）
            if pending_formula is not None and pending_formula[1] < ROW_COLUMNS:
                try:
                    text = _unicode_string(body, 0)[:CELL_CHARS]
                    cells.setdefault(pending_formula[0], {})[pending_formula[1]] = text
                except (IndexError, struct.error):
                    pass
            pending_formula = None
            continue
        if len(body) < 6:
            continue
        row, column = struct.unpack_from("<HH", body, 0)
        if row >= SHEET_ROWS:
            out.truncated = True
            continue
        value: str | None = None
        if kind == 0x00FD and len(body) >= 10:  # LABELSST
            index = struct.unpack_from("<I", body, 6)[0]
            value = sst[index] if index < len(sst) else ""
        elif kind == 0x0204:  # LABEL
            value = _unicode_string(body, 6)
        elif kind == 0x0203 and len(body) >= 14:  # NUMBER
            value = format_number(struct.unpack_from("<d", body, 6)[0])
        elif kind == 0x027E and len(body) >= 10:  # RK
            value = format_number(_rk_value(struct.unpack_from("<I", body, 6)[0]))
        elif kind == 0x00BD and len(body) >= 12:  # MULRK
            last = struct.unpack_from("<H", body, len(body) - 2)[0]
            for offset, col in zip(
                range(4, len(body) - 2, 6), range(column, last + 1), strict=False
            ):
                if col < ROW_COLUMNS:
                    raw = struct.unpack_from("<I", body, offset + 2)[0]
                    cells.setdefault(row, {})[col] = format_number(_rk_value(raw))
        elif kind == 0x0006 and len(body) >= 14:  # FORMULA
            result = body[6:14]
            if result[6:8] == b"\xff\xff":
                if result[0] == 0:
                    pending_formula = (row, column)
                elif result[0] == 1:
                    value = "TRUE" if result[2] else "FALSE"
            else:
                value = format_number(struct.unpack("<d", result)[0])
        if value is not None and column < ROW_COLUMNS:
            cells.setdefault(row, {})[column] = value[:CELL_CHARS]


def read_ppt(document: CompoundFile, out: Collector) -> None:
    if document.has("EncryptedSummary"):
        raise Unreadable(PASSWORD, "PowerPoint 加密")
    data = document.stream("PowerPoint Document")
    slides: list[list[str]] = []
    notes: list[list[str]] = []
    fallback: list[list[str]] = []

    def walk(start: int, end: int, context: str, depth: int) -> None:
        position = start
        while position + 8 <= end:
            ver_instance, kind, length = struct.unpack_from("<HHI", data, position)
            body_start = position + 8
            body_end = min(body_start + length, end)
            version = ver_instance & 0x000F
            instance = ver_instance >> 4
            if kind == 0x2F14:  # CryptSession10Container
                raise Unreadable(PASSWORD, "PowerPoint 加密")
            if version == 0x0F and depth < 16:
                if kind == 0x0FF0:  # SlideListWithText
                    inner = {0: "slides", 1: "masters", 2: "notes"}.get(instance, "other")
                    walk(body_start, body_end, inner, depth + 1)
                elif kind == 0x03EE:  # Slide
                    fallback.append([])
                    walk(body_start, body_end, "slide", depth + 1)
                else:
                    walk(body_start, body_end, context, depth + 1)
            else:
                if kind == 0x03F3 and context == "slides":  # SlidePersistAtom
                    slides.append([])
                elif kind == 0x03F3 and context == "notes":
                    notes.append([])
                elif kind in (0x0FA0, 0x0FA8):  # TextCharsAtom / TextBytesAtom
                    raw = data[body_start:body_end]
                    text = raw.decode(
                        "utf-16-le" if kind == 0x0FA0 else "latin-1", errors="replace"
                    )
                    text = text.replace("\r", "\n").replace("\x0b", "\n")
                    if context == "slides" and slides:
                        slides[-1].append(text)
                    elif context == "notes":
                        if not notes:
                            notes.append([])
                        notes[-1].append(text + "\n")
                    elif context == "slide" and fallback:
                        fallback[-1].append(text)
            position = body_start + length

    try:
        walk(0, len(data), "document", 0)
    except struct.error as error:
        raise Unreadable(CORRUPT, "PPT 结构不对") from error
    pages = slides if any(slides) else fallback
    out.pages = len(pages)
    for number, texts in enumerate(pages, start=1):
        out.add("\n".join(texts), f"第 {number} 页")
    for pieces in notes:
        out.add("".join(pieces), "备注")


def read_cfb(
    path: Path, out: Collector, *, ext: str, textutil: Callable[[Path, str], str] | None
) -> None:
    document, handle = open_cfb(path)
    try:
        kind = cfb_kind(document)
        if kind == "encrypted_ooxml":
            raise Unreadable(PASSWORD, "加密的 Office 文件")
        try:
            if kind == "doc":
                if word_encrypted(document):
                    raise Unreadable(PASSWORD, "Word 加密")
                handle.close()
                read_textutil(path, out, fmt="doc", textutil=textutil)
                return
            if kind == "xls":
                read_xls(document, out)
                return
            if kind == "ppt":
                read_ppt(document, out)
                return
        except CFBError as error:
            raise Unreadable(CORRUPT, str(error)) from error
        raise Unreadable(UNSUPPORTED, "不认识的复合文档")
    finally:
        handle.close()


# ---------------------------------------------------------------------- textutil


def textutil_command() -> str | None:
    override = os.environ.get("MEETING_WORKBENCH_TEXTUTIL", "").strip()
    if override:
        return override
    if sys.platform == "darwin":
        return "/usr/bin/textutil" if os.path.exists("/usr/bin/textutil") else "textutil"
    return None


def run_textutil(path: Path, fmt: str) -> str:
    command = textutil_command()
    if command is None:
        raise EngineMissing("textutil")
    try:
        result = subprocess.run(
            [
                command,
                "-format",
                fmt,
                "-convert",
                "txt",
                "-stdout",
                "-encoding",
                "UTF-8",
                str(path),
            ],
            capture_output=True,
            timeout=TEXTUTIL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("textutil 超时") from error
    except FileNotFoundError as error:
        raise EngineMissing("textutil") from error
    if result.returncode != 0:
        raise Unreadable(CORRUPT, "textutil 读不了")
    return result.stdout.decode("utf-8", errors="replace")


def read_textutil(
    path: Path, out: Collector, *, fmt: str, textutil: Callable[[Path, str], str] | None = None
) -> None:
    text = (textutil or run_textutil)(path, fmt)
    for paragraph in re.split(r"\n+", text):
        out.add(paragraph)
