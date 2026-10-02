"""第三期 3b：各格式的正文读取、读不了的五种原因、上限。直接调读取进程的 handle，不起子进程。"""

import codecs
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from meeting_workbench import extract_formats as formats
from meeting_workbench import extract_worker
from meeting_workbench.extract_worker import fit_answer, handle

from .material_fixtures import (
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
    write_bytes,
    write_zip,
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
