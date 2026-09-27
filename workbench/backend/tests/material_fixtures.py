"""第三期 3b 测试用的样本文件：复合文档（老 Office）写入器，和各格式的最小样本。

都只用标准库现场生成，不往仓库里放二进制样本。
"""
from __future__ import annotations

import os
import struct
import zipfile
from pathlib import Path

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREE = 0xFFFFFFFF
END = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
SECTOR = 512
MINI = 64
CUTOFF = 4096


def build_cfb(streams: dict[str, bytes]) -> bytes:
    """最小的复合文档（v3，512 字节扇区）：小于 4096 字节的流放进迷你流，其余走普通扇区。"""
    per = SECTOR // 4
    sectors: list[bytes] = []
    fat: list[int] = []

    def alloc(data: bytes) -> int:
        if not data:
            return END
        count = -(-len(data) // SECTOR)
        start = len(sectors)
        for index in range(count):
            sectors.append(data[index * SECTOR : (index + 1) * SECTOR].ljust(SECTOR, b"\0"))
            fat.append(start + index + 1 if index < count - 1 else END)
        return start

    small = {name: data for name, data in streams.items() if len(data) < CUTOFF}
    mini = bytearray()
    minifat: list[int] = []
    mini_start: dict[str, int] = {}
    for name, data in small.items():
        count = max(1, -(-len(data) // MINI))
        start = len(mini) // MINI
        mini_start[name] = start
        mini += data.ljust(count * MINI, b"\0")
        minifat.extend(start + index + 1 if index < count - 1 else END for index in range(count))
    ministream_start = alloc(bytes(mini))
    minifat_bytes = struct.pack(f"<{len(minifat)}I", *minifat) if minifat else b""
    minifat_start = alloc(minifat_bytes)
    big_start = {name: alloc(data) for name, data in streams.items() if name not in small}

    entries = [("Root Entry", 5, len(mini))] + [(name, 2, len(data)) for name, data in streams.items()]
    directory = bytearray()
    for index, (name, kind, size) in enumerate(entries):
        encoded = (name + "\0").encode("utf-16-le")
        entry = bytearray(128)
        entry[: len(encoded)] = encoded
        struct.pack_into("<H", entry, 0x40, len(encoded))
        entry[0x42] = kind
        entry[0x43] = 1
        child = 1 if index == 0 and len(entries) > 1 else NOSTREAM
        right = index + 1 if 0 < index < len(entries) - 1 else NOSTREAM
        struct.pack_into("<III", entry, 0x44, NOSTREAM, right, child)
        if index == 0:
            start = ministream_start
        else:
            start = mini_start[name] if name in small else big_start[name]
        struct.pack_into("<I", entry, 0x74, start)
        struct.pack_into("<Q", entry, 0x78, size)
        directory += entry
    directory_start = alloc(bytes(directory))

    fat_count = 1
    while fat_count * per < len(sectors) + fat_count:
        fat_count += 1
    fat_start = len(sectors)
    fat.extend([FATSECT] * fat_count)
    fat.extend([FREE] * (fat_count * per - len(fat)))
    table = struct.pack(f"<{len(fat)}I", *fat)
    for index in range(fat_count):
        sectors.append(table[index * SECTOR : (index + 1) * SECTOR])

    header = bytearray(512)
    header[:8] = OLE_MAGIC
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 0x2C, fat_count)
    struct.pack_into("<I", header, 0x30, directory_start)
    struct.pack_into("<I", header, 0x38, CUTOFF)
    struct.pack_into("<II", header, 0x3C, minifat_start, -(-len(minifat_bytes) // SECTOR))
    struct.pack_into("<II", header, 0x44, END, 0)
    difat = [fat_start + index for index in range(fat_count)] + [FREE] * (109 - fat_count)
    struct.pack_into("<109I", header, 0x4C, *difat)
    return bytes(header) + b"".join(sectors)


# ---------------------------------------------------------------------- 老 Office


def record(kind: int, body: bytes = b"") -> bytes:
    return struct.pack("<HH", kind, len(body)) + body


def _short(text: str) -> bytes:
    return struct.pack("<BB", len(text), 1) + text.encode("utf-16-le")


def xl_string(text: str) -> bytes:
    return struct.pack("<HB", len(text), 1) + text.encode("utf-16-le")


def _bof(substream: int) -> bytes:
    return record(0x0809, struct.pack("<HH", 0x0600, substream) + b"\0" * 12)


def build_workbook(
    sheets: list[tuple[str, list[list[object]]]],
    *,
    encrypted: bool = False,
    split_sst_at: int | None = None,
    chart_in_first_sheet: bool = False,
) -> bytes:
    """BIFF8 的 Workbook 流：文字走共享字符串表（LABELSST），数字走 NUMBER。"""
    strings: list[str] = []
    for _name, rows in sheets:
        for row in rows:
            for value in row:
                if isinstance(value, str) and value not in strings:
                    strings.append(value)
    globals_ = _bof(0x0005)
    if encrypted:
        globals_ += record(0x002F, b"\x01\x00" + b"\0" * 52)
    for name, _rows in sheets:
        globals_ += record(0x0085, struct.pack("<IBB", 0, 0, 0) + _short(name))
    sst = struct.pack("<II", len(strings), len(strings)) + b"".join(xl_string(text) for text in strings)
    if split_sst_at is None:
        globals_ += record(0x00FC, sst)
    else:
        # 在某个字符串中间断开：CONTINUE 开头补一个标志字节
        globals_ += record(0x00FC, sst[:split_sst_at]) + record(0x003C, b"\x01" + sst[split_sst_at:])
    globals_ += record(0x000A)
    body = globals_
    for number, (_name, rows) in enumerate(sheets):
        body += _bof(0x0010)
        if chart_in_first_sheet and number == 0:
            body += _bof(0x0020) + record(0x00FD, struct.pack("<HHHI", 0, 0, 0, 0)) + record(0x000A)
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                if value is None:
                    continue
                if isinstance(value, str):
                    body += record(0x00FD, struct.pack("<HHHI", row_index, column, 0, strings.index(value)))
                else:
                    body += record(0x0203, struct.pack("<HHHd", row_index, column, 0, float(value)))
        body += record(0x000A)
    return body


def build_xls(path: Path, sheets: list[tuple[str, list[list[object]]]], **kwargs: object) -> Path:
    stream = build_workbook(sheets, **kwargs)  # type: ignore[arg-type]
    return write_bytes(path, build_cfb({"Workbook": stream}))


def _ppt_record(kind: int, body: bytes, *, version: int = 0, instance: int = 0) -> bytes:
    return struct.pack("<HHI", version | (instance << 4), kind, len(body)) + body


def build_ppt(path: Path, slides: list[list[str]], notes: list[str] = (), *, encrypted: bool = False) -> Path:
    slide_list = b"".join(
        _ppt_record(0x03F3, b"\0" * 20)
        + b"".join(_ppt_record(0x0FA0, text.encode("utf-16-le")) for text in texts)
        for texts in slides
    )
    body = _ppt_record(0x0FF0, slide_list, version=0x0F, instance=0)
    if notes:
        note_list = b"".join(
            _ppt_record(0x03F3, b"\0" * 20) + _ppt_record(0x0FA8, text.encode("latin-1")) for text in notes
        )
        body += _ppt_record(0x0FF0, note_list, version=0x0F, instance=2)
    stream = _ppt_record(0x03E8, body, version=0x0F)
    streams = {"PowerPoint Document": stream, "Current User": b"\0" * 32}
    if encrypted:
        streams["EncryptedSummary"] = b"\0" * 64
    return write_bytes(path, build_cfb(streams))


def build_doc(path: Path, *, encrypted: bool = False) -> Path:
    fib = bytearray(4096)
    struct.pack_into("<HH", fib, 0, 0xA5EC, 0x00C1)
    struct.pack_into("<H", fib, 0x0A, 0x0100 if encrypted else 0)
    return write_bytes(path, build_cfb({"WordDocument": bytes(fib), "1Table": b"\0" * 600}))


def build_encrypted_ooxml(path: Path) -> Path:
    return write_bytes(path, build_cfb({"EncryptionInfo": b"\x04\x00\x04\x00" + b"\0" * 200,
                                        "EncryptedPackage": b"\0" * 5000}))


# ---------------------------------------------------------------------- zip 包

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_zip(path: Path, members: dict[str, str | bytes], *, stored_first: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if stored_first:
            archive.writestr(zipfile.ZipInfo(stored_first), members[stored_first], zipfile.ZIP_STORED)
        for name, data in members.items():
            if name != stored_first:
                archive.writestr(name, data)
    return path


def _w_paragraphs(paragraphs: list[str]) -> str:
    return "".join(f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>' for text in paragraphs)


def _rels(items: dict[str, str]) -> str:
    body = "".join(f'<Relationship Id="{rid}" Type="x" Target="{target}"/>' for rid, target in items.items())
    return f'<Relationships xmlns="{REL}">{body}</Relationships>'


def build_docx(
    path: Path,
    paragraphs: list[str],
    *,
    header: str | None = None,
    footnote: str | None = None,
    comment: str | None = None,
) -> Path:
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": CT,
        "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{_w_paragraphs(paragraphs)}'
        "<w:p><w:r><w:t>甲</w:t><w:tab/><w:t>乙</w:t><w:br/><w:t>丙</w:t></w:r></w:p></w:body></w:document>",
    }
    if header:
        members["word/header1.xml"] = f'<w:hdr xmlns:w="{W}">{_w_paragraphs([header])}</w:hdr>'
    if footnote:
        members["word/footnotes.xml"] = f'<w:footnotes xmlns:w="{W}">{_w_paragraphs([footnote])}</w:footnotes>'
    if comment:
        members["word/comments.xml"] = f'<w:comments xmlns:w="{W}">{_w_paragraphs([comment])}</w:comments>'
    return write_zip(path, members)


def build_xlsx(path: Path, sheets: list[tuple[str, list[list[object]]]]) -> Path:
    shared: list[str] = []
    members: dict[str, str | bytes] = {"[Content_Types].xml": CT}
    sheet_entries = []
    rels = {}
    for number, (name, rows) in enumerate(sheets, start=1):
        sheet_entries.append(f'<sheet name="{name}" sheetId="{number}" r:id="rId{number}"/>')
        rels[f"rId{number}"] = f"worksheets/sheet{number}.xml"
        row_xml = []
        for row_index, row in enumerate(rows, start=1):
            cells = []
            for column, value in enumerate(row):
                if value is None:
                    continue
                ref = f"{chr(65 + column)}{row_index}"
                if isinstance(value, str):
                    if value not in shared:
                        shared.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        members[f"xl/worksheets/sheet{number}.xml"] = (
            f'<worksheet xmlns="{S}"><sheetData>{"".join(row_xml)}</sheetData></worksheet>'
        )
    members["xl/workbook.xml"] = (
        f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>{"".join(sheet_entries)}</sheets></workbook>'
    )
    members["xl/_rels/workbook.xml.rels"] = _rels(rels)
    members["xl/sharedStrings.xml"] = (
        f'<sst xmlns="{S}">' + "".join(f"<si><t>{text}</t></si>" for text in shared) + "</sst>"
    )
    return write_zip(path, members)


def _slide(texts: list[str]) -> str:
    paragraphs = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in texts)
    return (
        f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>{paragraphs}'
        "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )


def build_pptx(path: Path, slides: list[list[str]], notes: dict[int, str] | None = None) -> Path:
    """幻灯片故意按和文件名相反的顺序排（sldIdLst 决定顺序）。"""
    notes = notes or {}
    members: dict[str, str | bytes] = {"[Content_Types].xml": CT}
    ids = []
    rels = {}
    count = len(slides)
    for number, texts in enumerate(slides, start=1):
        file_number = count - number + 1
        members[f"ppt/slides/slide{file_number}.xml"] = _slide(texts)
        rels[f"rId{number}"] = f"slides/slide{file_number}.xml"
        ids.append(f'<p:sldId id="{255 + number}" r:id="rId{number}"/>')
        if number in notes:
            members[f"ppt/notesSlides/notesSlide{file_number}.xml"] = _slide([notes[number], str(number)])
            members[f"ppt/slides/_rels/slide{file_number}.xml.rels"] = _rels(
                {"rId9": f"../notesSlides/notesSlide{file_number}.xml"}
            )
    members["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{"".join(ids)}</p:sldIdLst></p:presentation>'
    )
    members["ppt/_rels/presentation.xml.rels"] = _rels(rels)
    return write_zip(path, members)


ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
)


def build_odt(path: Path, paragraphs: list[str], *, encrypted: bool = False, table: list[list[str]] = ()) -> Path:
    body = "".join(f"<text:p>{text}</text:p>" for text in paragraphs)
    if table:
        rows = "".join(
            "<table:table-row>"
            + "".join(f"<table:table-cell><text:p>{cell}</text:p></table:table-cell>" for cell in row)
            + "</table:table-row>"
            for row in table
        )
        body += f'<table:table table:name="报价">{rows}</table:table>'
    manifest = '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
    if encrypted:
        manifest += '<manifest:file-entry manifest:full-path="content.xml"><manifest:encryption-data/></manifest:file-entry>'
    manifest += "</manifest:manifest>"
    return write_zip(
        path,
        {
            "mimetype": "application/vnd.oasis.opendocument.text",
            "META-INF/manifest.xml": manifest,
            "content.xml": f"<office:document-content {ODF_NS}><office:body><office:text>{body}"
            "</office:text></office:body></office:document-content>",
        },
        stored_first="mimetype",
    )


def build_epub(path: Path, chapters: list[str]) -> Path:
    """spine 的顺序和文件名顺序相反。"""
    members: dict[str, str | bytes] = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
    }
    items = []
    spine = []
    for number, text in enumerate(chapters, start=1):
        name = f"ch{len(chapters) - number + 1}.xhtml"
        members[f"OEBPS/{name}"] = f"<html><body><p>{text}</p></body></html>"
        items.append(f'<item id="c{number}" href="{name}"/>')
        spine.append(f'<itemref idref="c{number}"/>')
    members["OEBPS/content.opf"] = (
        '<package xmlns="http://www.idpf.org/2007/opf"><manifest>' + "".join(items)
        + "</manifest><spine>" + "".join(spine) + "</spine></package>"
    )
    return write_zip(path, members, stored_first="mimetype")


def fake_textutil(folder: Path, *, output: str = "", exit_code: int = 0) -> Path:
    """替代 macOS 的 textutil：把参数记下来，按要求输出。"""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "textutil"
    log = folder / "textutil.args"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f"printf '%s' '{output}'\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    return script
