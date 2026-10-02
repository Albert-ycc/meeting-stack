"""第三期 3b：各格式的正文读取、读不了的五种原因、上限。直接调读取进程的 handle，不起子进程。"""

import codecs
import json
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from meeting_workbench import extract_formats as formats
from meeting_workbench import extract_worker
from meeting_workbench.extract_worker import fit_answer, handle

from .material_fixtures import (
    OLE_MAGIC,
    _bof,
    _ppt_record,
    _short,
    build_cfb,
    build_doc,
    build_docx,
    build_encrypted_ooxml,
    build_epub,
    build_odt,
    build_ppt,
    build_pptx,
    build_xls,
    build_xlsx,
    fake_textutil,
    record,
    write_bytes,
    write_zip,
    xl_string,
)


def read(path: Path, ext: str | None = None) -> dict:
    return handle(
        {
            "path": str(path),
            "ext": ext if ext is not None else path.suffix.lstrip("."),
            "layer": "text",
        }
    )


def texts(answer: dict) -> list[str]:
    return [block["text"] for block in answer["blocks"]]


def locs(answer: dict) -> list:
    return [block["loc"] for block in answer["blocks"]]


_CHILD = r"""
import json, resource, sys, time
from meeting_workbench.extract_worker import handle
start = time.monotonic()
answer = handle({"path": sys.argv[1], "ext": sys.argv[2], "layer": "text"})
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
answer.pop("blocks", None)
answer["seconds"] = time.monotonic() - start
# ru_maxrss：macOS 上是字节，Linux 上是 KB
answer["peak_mb"] = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
print(json.dumps(answer))
"""


def read_in_child(path: Path, timeout: float = 30) -> dict:
    """对抗样本放子进程里读：修坏了也只是这个子进程超时被杀，不会把跑用例的进程拖进 swap。"""
    root = str(Path(extract_worker.__file__).resolve().parent.parent)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(path), path.suffix.lstrip(".")],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONPATH": root},
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------- 纯文字


def test_plain_text_encodings(tmp_path):
    assert texts(read(write_bytes(tmp_path / "a.txt", "第一段\n\n第二段".encode()))) == [
        "第一段",
        "第二段",
    ]
    gbk = write_bytes(tmp_path / "gbk.txt", "中文 Windows 存的报价单".encode("gbk"))
    assert texts(read(gbk)) == ["中文 Windows 存的报价单"]
    utf16 = write_bytes(
        tmp_path / "u16.txt", codecs.BOM_UTF16_LE + "带 BOM 的文字".encode("utf-16-le")
    )
    assert texts(read(utf16)) == ["带 BOM 的文字"]
    sig = write_bytes(tmp_path / "sig.md", codecs.BOM_UTF8 + "# 标题".encode())
    assert texts(read(sig)) == ["# 标题"]
    broken = write_bytes(tmp_path / "broken.log", b"ok \xff\xfe\xfd end")
    answer = read(broken)
    assert answer["status"] == "ok" and "ok" in texts(answer)[0]
    code = write_bytes(
        tmp_path / "main.py", b"def main():\n    return '\xe6\x8a\xa5\xe4\xbb\xb7'\n"
    )
    assert "报价" in texts(read(code))[0]


def test_binary_with_text_extension_is_unsupported(tmp_path):
    answer = read(write_bytes(tmp_path / "data.txt", b"\x00\x01\x02binary\x00" * 10))
    assert answer["status"] == "unsupported"


def test_plain_text_is_capped_at_8mb(tmp_path, monkeypatch):
    monkeypatch.setattr(formats, "PLAIN_MAX_BYTES", 100)
    answer = read(write_bytes(tmp_path / "big.log", ("一行日志\n" * 100).encode()))
    assert answer["truncated"] is True
    assert answer["chars"] <= 100


def test_csv_tsv_and_tab_text_disguised_as_xls(tmp_path):
    rows = "\n".join(f"项目{i},金额,{i * 100}" for i in range(120))
    answer = read(write_bytes(tmp_path / "报价.csv", rows.encode()))
    assert len(answer["blocks"]) == 3  # 每 50 行一段
    assert answer["blocks"][0]["text"].splitlines()[1] == "项目1\t金额\t100"
    exported = write_bytes(tmp_path / "导出.xls", "姓名\t部门\n张三\t市场部\n".encode("gbk"))
    assert texts(read(exported)) == ["姓名\t部门\n张三\t市场部"]


def test_ipynb_takes_sources_and_text_outputs(tmp_path):
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["# 分析\n", "结论"]},
            {
                "cell_type": "code",
                "source": "print(1)",
                "outputs": [
                    {"output_type": "stream", "text": ["1\n"]},
                    {
                        "output_type": "display_data",
                        "data": {"image/png": "AAAA", "text/plain": ["<Figure>"]},
                    },
                ],
            },
        ]
    }
    answer = read(write_bytes(tmp_path / "a.ipynb", json.dumps(notebook).encode()))
    assert texts(answer) == ["# 分析\n结论", "print(1)", "1", "<Figure>"]


def test_html_skips_script_and_uses_meta_charset(tmp_path):
    page = (
        '<html><head><meta charset="gbk"><title>报价</title><style>p{}</style></head>'
        "<body><script>var secret=1</script><p>第一段</p><div>第二段</div></body></html>"
    ).encode("gbk")
    answer = read(write_bytes(tmp_path / "a.html", page))
    assert "secret" not in "".join(texts(answer))
    assert texts(answer) == ["报价", "第一段", "第二段"]


def test_html_long_blank_run_is_linear(tmp_path):
    """30 万个连续空格、中间没有换行：清理行尾空白的正则曾平方级回溯，卡到超时。"""
    path = write_bytes(
        tmp_path / "空格.html", b"<html><body><p>a" + b" " * 300_000 + b"b</p></body></html>"
    )
    answer = read_in_child(path)
    assert answer["status"] == "ok" and answer["seconds"] < 5


def test_html_trailing_blanks_before_newline_are_dropped():
    assert formats.html_to_text("<p>a \t\f\v\r\n  b  \n</p>c  ") == "\n\na\n  b\n\n\nc  "


def test_html_saved_as_doc_is_read_as_html(tmp_path):
    answer = read(
        write_bytes(
            tmp_path / "系统导出.doc",
            "<html><body><p>网上系统导出的文档</p></body></html>".encode(),
        )
    )
    assert texts(answer) == ["网上系统导出的文档"]


def test_eml_and_mht(tmp_path):
    message = (
        "MIME-Version: 1.0\nSubject: =?utf-8?b?5ZGo5Lya57qq6KaB?=\n"
        'Content-Type: multipart/alternative; boundary="b"\n\n'
        "--b\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n正文第一段\n\n正文第二段\n"
        "--b\nContent-Type: text/html; charset=utf-8\n\n<p>HTML 版本</p>\n--b--\n"
    )
    answer = read(write_bytes(tmp_path / "a.eml", message.encode()))
    assert texts(answer) == ["周会纪要", "正文第一段", "正文第二段"]
    assert locs(answer)[0] == "主题"
    mht = (
        'MIME-Version: 1.0\nContent-Type: multipart/related; boundary="x"\n\n'
        "--x\nContent-Type: text/html; charset=utf-8\n\n<html><body><p>网页存档</p></body></html>\n--x--\n"
    )
    assert texts(read(write_bytes(tmp_path / "a.mht", mht.encode()))) == ["网页存档"]


# ---------------------------------------------------------------------- 压缩包里的 XML


def test_docx_body_header_footnote_comment(tmp_path):
    path = build_docx(
        tmp_path / "方案.docx",
        ["第一段", "第二段"],
        header="页眉文字",
        footnote="脚注文字",
        comment="批注文字",
    )
    answer = read(path)
    assert answer["status"] == "ok"
    assert texts(answer) == ["第一段", "第二段", "甲\t乙\n丙", "页眉文字", "脚注文字", "批注文字"]
    assert locs(answer) == [None, None, None, "页眉", "脚注", "批注"]


def test_xlsx_sheets_cells_numbers_and_limits(tmp_path, monkeypatch):
    path = build_xlsx(
        tmp_path / "预算.xlsx",
        [
            ("预算", [["项目", "金额"], ["服务器", 12000], ["差旅", 3500.5]]),
            ("备注", [["说明", None, "第三列"]]),
        ],
    )
    answer = read(path)
    assert texts(answer) == ["项目\t金额\n服务器\t12000\n差旅\t3500.5", "说明\t\t第三列"]
    assert locs(answer) == ["表「预算」", "表「备注」"]
    monkeypatch.setattr(formats, "SHEET_ROWS", 2)
    answer = read(path)
    assert texts(answer)[0] == "项目\t金额\n服务器\t12000" and answer["truncated"] is True


def test_pptx_slide_order_and_notes(tmp_path):
    path = build_pptx(
        tmp_path / "汇报.pptx",
        [["封面", "云图AI"], ["进度"], ["下一步"]],
        notes={2: "讲到这里停一下"},
    )
    answer = read(path)
    assert texts(answer) == ["封面\n云图AI", "进度", "讲到这里停一下", "下一步"]
    assert locs(answer) == ["第 1 页", "第 2 页", "第 2 页 备注", "第 3 页"]
    assert answer["pages"] == 3


def test_odt_epub_and_their_password_marks(tmp_path):
    answer = read(build_odt(tmp_path / "a.odt", ["开放格式正文"], table=[["单价", "100"]]))
    assert texts(answer) == ["开放格式正文", "单价\t100"]
    assert read(build_odt(tmp_path / "b.odt", ["x"], encrypted=True))["status"] == "password"
    answer = read(build_epub(tmp_path / "书.epub", ["第一章", "第二章"]))
    assert texts(answer) == ["第一章", "第二章"]
    locked = write_zip(
        tmp_path / "锁.epub",
        {
            "mimetype": "application/epub+zip",
            "META-INF/container.xml": "<container/>",
            "META-INF/encryption.xml": '<encryption xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
            '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
            '<enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData></enc:EncryptedData>'
            "</encryption>",
        },
    )
    assert read(locked)["status"] == "password"


def test_zip_limits_declared_and_real_size(tmp_path, monkeypatch):
    path = build_docx(tmp_path / "大.docx", ["正文" * 50])
    monkeypatch.setattr(formats, "MEMBER_DECLARED_LIMIT", 50)
    answer = read(path)
    assert answer["truncated"] is True and answer["blocks"] == []
    monkeypatch.setattr(formats, "MEMBER_DECLARED_LIMIT", 10**9)
    monkeypatch.setattr(formats, "MEMBER_READ_LIMIT", 120)
    answer = read(path)
    assert answer["status"] == "ok" and answer["truncated"] is True


def test_broken_packages_are_corrupt_only_on_parse_failure(tmp_path):
    assert read(write_bytes(tmp_path / "坏.docx", b"PK\x03\x04 broken"))["status"] == "corrupt"
    assert (
        read(write_zip(tmp_path / "缺.docx", {"[Content_Types].xml": "<Types/>"}))["status"]
        == "corrupt"
    )
    bad_xml = write_zip(
        tmp_path / "xml.docx",
        {"[Content_Types].xml": "<Types/>", "word/document.xml": "<w:document><w:p>"},
    )
    assert read(bad_xml)["status"] == "corrupt"
    assert (
        read(write_zip(tmp_path / "其实是压缩包.txt", {"a.bin": b"x"}))["status"] == "unsupported"
    )


def test_unsupported_compression_method_is_unsupported_not_corrupt(tmp_path):
    """成员用了 zipfile 不支持的压缩方式（deflate64 = 9）：是「读不了这种格式」，不是文件坏了。"""
    path = build_docx(tmp_path / "deflate64.docx", ["正文"])
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        start = 0
        while (index := data.find(signature, start)) != -1:
            data[index + offset : index + offset + 2] = (9).to_bytes(2, "little")
            start = index + 4
    path.write_bytes(bytes(data))
    assert read(path)["status"] == "unsupported"


def test_iwork_is_unsupported(tmp_path):
    assert (
        read(write_zip(tmp_path / "总结.pages", {"Index/Document.iwa": b"x"}))["status"]
        == "unsupported"
    )
    package = tmp_path / "演示.key"
    package.mkdir()
    (package / "Index.zip").write_bytes(b"PK")
    assert read(package)["status"] == "unsupported"


def test_pdf_in_disguise_asks_to_change_layer(tmp_path):
    answer = read(write_bytes(tmp_path / "合同.doc", b"%PDF-1.7\n..."))
    assert answer == {
        "status": "relayer",
        "layer": "pdf",
        "extractor": "stdlib",
        "extractor_version": 1,
    }


# ---------------------------------------------------------------------- 老 Office


def test_xls_shared_strings_numbers_sheets_and_chart_substreams(tmp_path):
    sheets = [("预算", [["项目", "金额"], ["服务器", 12000]]), ("人员", [["张三", "市场部"]])]
    answer = read(build_xls(tmp_path / "预算.xls", sheets, chart_in_first_sheet=True))
    assert answer["status"] == "ok"
    assert texts(answer) == ["项目\t金额\n服务器\t12000", "张三\t市场部"]
    assert locs(answer) == ["表「预算」", "表「人员」"]
    assert read(build_xls(tmp_path / "锁.xls", sheets, encrypted=True))["status"] == "password"


def test_xls_sst_continue_split_mid_string(tmp_path):
    sheets = [("表", [["一段很长的共享字符串", "后一个"]])]
    # 8 字节表头 + 3 字节串头 + 4 个汉字处断开
    answer = read(build_xls(tmp_path / "续.xls", sheets, split_sst_at=8 + 3 + 8))
    assert texts(answer) == ["一段很长的共享字符串\t后一个"]


def test_et_saved_by_wps_reads_like_xls(tmp_path):
    answer = read(build_xls(tmp_path / "表.et", [("金山", [["单元格"]])]))
    assert texts(answer) == ["单元格"]


def test_ppt_slides_notes_and_password(tmp_path):
    answer = read(
        build_ppt(tmp_path / "旧.ppt", [["封面"], ["第二页", "要点"]], notes=["notes text"])
    )
    assert texts(answer) == ["封面", "第二页\n要点", "notes text"]
    assert locs(answer) == ["第 1 页", "第 2 页", "备注"]
    assert read(build_ppt(tmp_path / "锁.dps", [["x"]], encrypted=True))["status"] == "password"


def test_doc_goes_through_textutil_with_format(tmp_path, monkeypatch):
    script = fake_textutil(tmp_path / "bin", output="旧版 Word 正文\n\n第二段")
    monkeypatch.setenv("MEETING_WORKBENCH_TEXTUTIL", str(script))
    doc = build_doc(tmp_path / "旧.doc")
    answer = read(doc)
    assert texts(answer) == ["旧版 Word 正文", "第二段"]
    arguments = (tmp_path / "bin" / "textutil.args").read_text(encoding="utf-8")
    assert arguments.startswith("-format doc -convert txt -stdout -encoding UTF-8 ")
    # 改了扩展名的老 .doc：外面是复合文档、里面没有 EncryptedPackage，照 .doc 读
    renamed = build_doc(tmp_path / "改名.docx")
    assert texts(read(renamed)) == ["旧版 Word 正文", "第二段"]
    rtf = write_bytes(tmp_path / "a.rtf", b"{\\rtf1\\ansi hello}")
    assert read(rtf)["status"] == "ok"
    assert "-format rtf" in (tmp_path / "bin" / "textutil.args").read_text(encoding="utf-8")
    assert read(build_doc(tmp_path / "锁.doc", encrypted=True))["status"] == "password"


def test_textutil_empty_output_is_done_and_failure_is_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_WORKBENCH_TEXTUTIL", str(fake_textutil(tmp_path / "empty")))
    answer = read(build_doc(tmp_path / "扫描件.doc"))
    assert answer["status"] == "ok" and answer["blocks"] == []
    monkeypatch.setenv(
        "MEETING_WORKBENCH_TEXTUTIL", str(fake_textutil(tmp_path / "fail", exit_code=1))
    )
    assert read(build_doc(tmp_path / "坏.doc"))["status"] == "corrupt"


def test_textutil_missing_off_macos_is_waiting(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_WORKBENCH_TEXTUTIL", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    answer = read(build_doc(tmp_path / "旧.doc"))
    assert answer["status"] == "waiting" and answer["what"] == "textutil"


def test_textutil_timeout(tmp_path, monkeypatch):
    def slow(path, fmt):
        raise TimeoutError("textutil 超时")

    monkeypatch.setattr(formats, "run_textutil", slow)
    assert read(write_bytes(tmp_path / "a.rtf", b"{\\rtf1 x}"))["status"] == "timeout"


def test_encrypted_ooxml_and_unknown_cfb(tmp_path):
    assert read(build_encrypted_ooxml(tmp_path / "锁.xlsx"))["status"] == "password"
    assert (
        read(write_bytes(tmp_path / "x.ppt", build_cfb({"Other": b"x" * 100})))["status"]
        == "unsupported"
    )
    assert (
        read(write_bytes(tmp_path / "坏.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64))[
            "status"
        ]
        == "corrupt"
    )


def _cfb_header(*, difat_first: int, difat_count: int, fat_sectors: int = 1) -> bytearray:
    header = bytearray(512)
    header[:8] = OLE_MAGIC
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 0x2C, fat_sectors)
    struct.pack_into("<IIII", header, 0x30, 0xFFFFFFFE, 0, 0, 4096)
    struct.pack_into("<I", header, 0x3C, 0xFFFFFFFE)
    struct.pack_into("<II", header, 0x44, difat_first, difat_count)
    struct.pack_into("<109I", header, 0x4C, *([0xFFFFFFFF] * 109))
    return header


def _difat_sector(next_sector: int) -> bytes:
    return b"\xff" * 508 + struct.pack("<I", next_sector)


def test_cfb_difat_chain_loop_is_corrupt_fast(tmp_path):
    """DIFAT 链指回自己、文件头自报 40 亿个 DIFAT 扇区：1KB 的文件曾让读取进程几秒吃到上 GB。"""
    self_loop = _cfb_header(difat_first=0, difat_count=0xFFFFFFFF) + _difat_sector(0)
    answer = read_in_child(write_bytes(tmp_path / "自环.xls", bytes(self_loop)))
    assert answer["status"] == "corrupt" and answer["seconds"] < 5
    two_cycle = (
        _cfb_header(difat_first=0, difat_count=0xFFFFFFFF) + _difat_sector(1) + _difat_sector(0)
    )
    answer = read_in_child(write_bytes(tmp_path / "两步环.xls", bytes(two_cycle)))
    assert answer["status"] == "corrupt" and answer["seconds"] < 5


def test_cfb_repeated_fat_sector_is_corrupt(tmp_path):
    """DIFAT 里同一个扇区当 FAT 扇区列了 109 遍：FAT 会被拼成 109 份，文件越大放大越多。"""
    header = _cfb_header(difat_first=0xFFFFFFFE, difat_count=0, fat_sectors=109)
    struct.pack_into("<109I", header, 0x4C, *([0] * 109))
    fat = struct.pack("<128I", *([0xFFFFFFFD] + [0xFFFFFFFF] * 127))
    assert read(write_bytes(tmp_path / "重复.xls", bytes(header) + fat))["status"] == "corrupt"


def _patch_minifat_loop(data: bytearray, stream: str, size: int) -> None:
    """把 stream 的迷你扇区链最后一节指回第一节，并把它自报的大小改成 size。"""
    import io

    document = formats.CompoundFile(io.BytesIO(bytes(data)))
    start, _size, _kind = document.entries[stream]
    last = start
    while document.minifat[last] != 0xFFFFFFFE:
        last = document.minifat[last]
    first_minifat = struct.unpack_from("<I", data, 0x3C)[0]
    struct.pack_into("<I", data, (first_minifat + 1) * 512 + 4 * last, start)
    entry = data.find((stream + "\0").encode("utf-16-le"))
    struct.pack_into("<Q", data, entry + 0x78, size)


def test_cfb_minifat_loop_is_corrupt(tmp_path):
    """迷你扇区链绕圈、流自报的大小比链长：不再沿着环拼字节。"""
    sheets = [("表", [["一段文字", 1]])]
    data = bytearray(build_xls(tmp_path / "好.xls", sheets).read_bytes())
    _patch_minifat_loop(data, "Workbook", 4000)
    assert read(write_bytes(tmp_path / "环.xls", bytes(data)))["status"] == "corrupt"


def test_cfb_minifat_loop_does_not_walk_the_whole_table(tmp_path):
    """迷你扇区表很长、链是一节的环：曾沿环走满整张表（几百万节），拼出几百 MB 再截。"""
    path = build_xls(tmp_path / "好.xls", [("表", [["x"]])])
    with path.open("rb") as handle_:
        document = formats.CompoundFile(handle_)
    document.mini_stream = b"m" * 64
    document.minifat = [0] * 2_000_000
    document.entries["环"] = (0, 4000, 2)
    walked = 0
    chain = document._chain

    def counting(*args, **kwargs):
        nonlocal walked
        for sector in chain(*args, **kwargs):
            walked += 1
            yield sector

    document._chain = counting
    with pytest.raises(formats.CFBError):
        document.stream("环")
    assert walked <= 1
    document.entries["短"] = (0, 50, 2)
    walked = 0
    assert document.stream("短") == b"m" * 50
    assert walked == 1


def test_cfb_small_stream_reading_stops_at_declared_size(tmp_path):
    """读小流沿迷你扇区链拼字节，拼够自报的大小就停：环在大小之外的不影响读。"""
    sheets = [("表", [["一段文字", 1]])]
    path = build_xls(tmp_path / "好.xls", sheets)
    data = bytearray(path.read_bytes())
    import io

    size = formats.CompoundFile(io.BytesIO(bytes(data))).entries["Workbook"][1]
    _patch_minifat_loop(data, "Workbook", size)
    assert texts(read(write_bytes(tmp_path / "环在外.xls", bytes(data)))) == texts(read(path))


def test_xls_formula_text_beyond_200_columns_is_skipped_fast(tmp_path):
    """公式的文字结果落在第 60001 列：曾按最大列号把每行补成 6 万格，2 万行要跑好几分钟。"""
    stream = _bof(0x0005) + record(0x0085, struct.pack("<IBB", 0, 0, 0) + _short("表"))
    stream += record(0x000A) + _bof(0x0010)
    for row in range(20_000):
        formula = struct.pack("<HHH", row, 60_000, 0) + b"\0" * 6 + b"\xff\xff" + b"\0" * 6
        stream += record(0x0006, formula) + record(0x0207, xl_string("x"))
    stream += record(0x000A)
    path = write_bytes(tmp_path / "宽.xls", build_cfb({"Workbook": stream}))
    answer = read_in_child(path)
    assert answer["status"] == "ok" and answer["chars"] == 0 and answer["seconds"] < 10


def test_ppt_many_note_pieces_is_linear(tmp_path):
    """备注里几十万条一个字的文字记录：曾逐条拼接字符串，耗时随条数平方涨。"""
    pieces = _ppt_record(0x0FA8, b"a") * 400_000
    notes = _ppt_record(0x03F3, b"\0" * 20) + pieces
    body = _ppt_record(0x0FF0, notes, version=0x0F, instance=2)
    stream = _ppt_record(0x03E8, body, version=0x0F)
    path = write_bytes(
        tmp_path / "备注.ppt",
        build_cfb({"PowerPoint Document": stream, "Current User": b"\0" * 32}),
    )
    answer = read_in_child(path)
    assert answer["status"] == "ok" and answer["truncated"] is True and answer["seconds"] < 10


# ---------------------------------------------------------------------- 权限、IO、上限


@pytest.mark.skipif(os.geteuid() == 0, reason="root 读得了没有权限的文件")
def test_permission_error(tmp_path):
    path = write_bytes(tmp_path / "锁.txt", b"secret")
    path.chmod(0)
    try:
        assert read(path)["status"] == "permission"
    finally:
        path.chmod(0o644)


def test_permission_and_io_errors_are_reported_as_such(tmp_path, monkeypatch):
    path = write_bytes(tmp_path / "a.txt", b"x")

    def denied(*args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(formats, "read_plain", denied)
    assert read(path)["status"] == "permission"

    def eio(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(formats, "read_plain", eio)
    assert read(path)["status"] == "io_error"

    def zip_eio(*args, **kwargs):
        raise OSError(5, "EIO inside zipfile")

    docx = build_docx(tmp_path / "b.docx", ["x"])
    monkeypatch.setattr(zipfile, "ZipFile", zip_eio)
    assert read(docx)["status"] == "io_error"


def test_200k_char_cap_and_2mb_answer_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(formats, "MAX_CHARS", 30)
    paragraphs = "\n\n".join(f"第{i}段文字" for i in range(20))
    answer = read(write_bytes(tmp_path / "长.txt", paragraphs.encode()))
    assert answer["chars"] == 30 and answer["truncated"] is True
    big = {
        "status": "ok",
        "blocks": [{"loc": None, "text": "字" * 1000} for _ in range(10)],
        "chars": 10000,
        "truncated": False,
    }
    fitted = fit_answer(big, limit=5000)
    assert len(json.dumps(fitted, ensure_ascii=False).encode()) <= 5000
    assert fitted["truncated"] is True and fitted["chars"] == 1000 * len(fitted["blocks"])


def test_worker_module_runs_as_a_resident_process(tmp_path):
    """真的起一次读取进程：一行请求一行回答，print 不混进回答。"""
    from meeting_workbench.material_helpers import HelperProcess

    root = str(Path(extract_worker.__file__).resolve().parent.parent)
    helper = HelperProcess(
        "extract",
        [sys.executable, "-m", "meeting_workbench.extract_worker"],
        data_dir=tmp_path / "data",
        env={"PYTHONPATH": root},
        background=False,
    )
    try:
        doc = build_docx(tmp_path / "a.docx", ["子进程读到的正文"])
        answer = helper.request({"path": str(doc), "ext": "docx", "layer": "text"}, timeout=30)
        assert answer["status"] == "ok" and answer["blocks"][0]["text"] == "子进程读到的正文"
        answer = helper.request({"path": str(tmp_path / "不存在.txt"), "ext": "txt"}, timeout=30)
        assert answer["status"] == "io_error"
    finally:
        helper.close()
