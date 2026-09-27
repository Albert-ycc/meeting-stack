"""第三期 3b：服务这边的文档正文读取：切段、片段和状态同一个事务写、超时再试、换层、升级后重读。"""
from datetime import timedelta

from meeting_workbench import extract_worker
from meeting_workbench.extract_worker import TextExtractor, handle
from meeting_workbench.material_content import ExtractResult, chunk_blocks
from meeting_workbench.material_helpers import HelperStopped, HelperTimeout

from .material_fixtures import build_docx, build_xls, write_bytes
from .test_material_content import contents, index, put, setup


class InProcess:
    """不起子进程，直接调读取进程的 handle。"""

    def __init__(self, fail=None):
        self.requests = []
        self.fail = list(fail or [])

    def request(self, payload, *, timeout, **kwargs):
        self.requests.append((payload, timeout))
        if self.fail:
            raise self.fail.pop(0)
        return handle(payload)

    def close(self):
        pass


def extractor(tmp_path, helper=None):
    helper = helper or InProcess()
    return TextExtractor(tmp_path / "data", helper_factory=lambda: helper), helper


def chunks(db):
    return [
        (row["loc"], row["text"])
        for row in db.query_all("SELECT loc, text FROM material_chunks ORDER BY content_key, ordinal")
    ]


def fts(db, needle):
    return [
        row["text"]
        for row in db.query_all(
            "SELECT c.text FROM material_chunks_fts JOIN material_chunks c ON c.id = material_chunks_fts.rowid "
            "WHERE material_chunks_fts MATCH ?",
            (f'"{needle}"',),
        )
    ]


# ---------------------------------------------------------------------- 切段


def test_chunks_split_at_sentence_ends_and_merge_short_paragraphs_in_the_same_place():
    long = "。".join(f"第{i}句话讲的是报价和交付时间" for i in range(60)) + "。"
    result, cut = chunk_blocks([{"loc": None, "text": long}])
    assert not cut and all(len(text) <= 400 for _loc, text in result)
    assert all(text.endswith("。") for _loc, text in result[:-1])
    assert "".join(text for _loc, text in result) == long

    blocks = [
        {"loc": "第 1 页", "text": "标题"},
        {"loc": "第 1 页", "text": "要点一"},
        {"loc": "第 2 页", "text": "另一页"},
        {"loc": "第 2 页", "text": "字" * 399},
    ]
    result, _cut = chunk_blocks(blocks)
    assert result == [("第 1 页", "标题\n要点一"), ("第 2 页", "另一页"), ("第 2 页", "字" * 399)]

    no_breaks = "字" * 1000
    result, _cut = chunk_blocks([{"loc": None, "text": no_breaks}])
    assert [len(text) for _loc, text in result] == [400, 400, 200]


def test_chunks_stop_at_200k_chars():
    blocks = [{"loc": None, "text": "字" * 300} for _ in range(10)]
    result, cut = chunk_blocks(blocks, max_chars=1000)
    assert cut is True and sum(len(text) for _loc, text in result) == 1000


# ---------------------------------------------------------------------- 写入


def test_documents_are_read_chunked_and_searchable(tmp_path):
    text, _helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    put(root / "纪要.md", "第一段讲报价单的事\n\n第二段讲交付")
    build_docx(root / "方案.docx", ["方案正文里的验收标准"])
    put(root / "方案.docx", (root / "方案.docx").read_bytes())
    build_xls(root / "预算.xls", [("预算", [["服务器", 12000]])])
    put(root / "预算.xls", (root / "预算.xls").read_bytes())
    index(indexer)
    content.run_round()
    rows = contents(db)
    assert {row["state"] for row in rows.values()} == {"done"}
    assert {row["extractor"] for row in rows.values()} == {"stdlib"}
    assert {row["extractor_version"] for row in rows.values()} == {extract_worker.EXTRACTOR_VERSION}
    assert sorted(row["chunks"] for row in rows.values()) == [1, 1, 1]
    assert ("表「预算」", "服务器\t12000") in chunks(db)
    assert fts(db, "报价单") == ["第一段讲报价单的事\n第二段讲交付"]
    assert fts(db, "验收标准") == ["方案正文里的验收标准\n甲\t乙\n丙"]


def test_rereading_replaces_old_chunks_in_one_transaction(tmp_path):
    text, _helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    put(root / "纪要.md", "旧的正文内容")
    index(indexer)
    content.run_round()
    key = next(iter(contents(db)))
    db.execute("UPDATE material_contents SET state = 'pending' WHERE content_key = ?", (key,))
    content.run_round()
    assert chunks(db) == [(None, "旧的正文内容")]
    assert fts(db, "旧的正文") == ["旧的正文内容"]
    assert db.query_one("SELECT COUNT(*) AS n FROM material_chunks_fts")["n"] == 1


def test_truncated_answer_is_noted(tmp_path, monkeypatch):
    from meeting_workbench import extract_formats

    monkeypatch.setattr(extract_formats, "MAX_CHARS", 5)
    text, _helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    put(root / "长.txt", "一二三四五六七八九十")
    index(indexer)
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["note"], row["chars"]) == ("done", "truncated", 5)


# ---------------------------------------------------------------------- 读不了


def test_timeout_retries_once_with_120_seconds_then_gives_up(tmp_path):
    helper = InProcess(fail=[HelperTimeout("超时"), HelperTimeout("又超时")])
    text, _ = extractor(tmp_path, helper)
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path, extractors={"text": text})
    put(root / "卡住.txt", "x")
    index(indexer)
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["reason"], row["attempts"]) == ("pending", "timeout", 1)
    content.run_round()
    assert len(helper.requests) == 1  # 一小时内不再试
    now.value += timedelta(hours=1, minutes=1)
    content.run_round()
    assert [timeout for _payload, timeout in helper.requests] == [60.0, 120.0]
    row = next(iter(contents(db).values()))
    assert (row["state"], row["reason"]) == ("unreadable", "timeout")
    # 文件变了是另一份内容，照常读
    put(root / "卡住.txt", "改过了", when=1_700_000_000)
    index(indexer)
    content.run_round()
    assert {row["state"] for row in contents(db).values()} == {"unreadable", "done"}


def test_password_corrupt_and_waiting(tmp_path, monkeypatch):
    import sys

    monkeypatch.delenv("MEETING_WORKBENCH_TEXTUTIL", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    text, _helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    build_xls(root / "锁.xls", [("表", [["x"]])], encrypted=True)
    put(root / "锁.xls", (root / "锁.xls").read_bytes())
    put(root / "坏.docx", b"PK\x03\x04 broken")
    put(root / "说明.rtf", "{\\rtf1 x}")
    index(indexer)
    content.run_round()
    by_name = {
        row["name"]: row
        for row in db.query_all(
            "SELECT f.name, c.state, c.reason, c.note, c.extractor FROM material_files f "
            "JOIN material_contents c ON c.content_key = f.content_key"
        )
    }
    assert (by_name["锁.xls"]["state"], by_name["锁.xls"]["reason"]) == ("unreadable", "password")
    assert (by_name["坏.docx"]["state"], by_name["坏.docx"]["reason"]) == ("unreadable", "corrupt")
    assert (by_name["说明.rtf"]["state"], by_name["说明.rtf"]["note"]) == ("waiting", "engine_missing")
    assert by_name["锁.xls"]["extractor"] == "stdlib"


def test_pdf_with_a_doc_extension_moves_to_the_pdf_layer(tmp_path):
    text, helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    put(root / "合同.doc", b"%PDF-1.7\n1 0 obj\n")
    index(indexer)
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["layer"], row["state"]) == ("pdf", "pending")
    content.run_round()
    assert len(helper.requests) == 1  # 3b 还没有 PDF 读取器，不再拿文字读取器读


def test_permission_from_the_reader_goes_on_the_file_row(tmp_path):
    def denied(path, layer, row):
        return ExtractResult(status="permission")

    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": denied})
    put(root / "纪要.md", "x")
    index(indexer)
    content.run_round()
    assert db.query_one("SELECT content_error FROM material_files")["content_error"] == "permission"
    assert next(iter(contents(db).values()))["state"] == "pending"


def test_stopped_reader_writes_nothing_and_ends_the_round(tmp_path):
    helper = InProcess(fail=[HelperStopped("busy")])
    text, _ = extractor(tmp_path, helper)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    put(root / "纪要.md", "会议开始转写时读到一半")
    index(indexer)
    assert content.run_round()["ended"] == "busy"
    row = next(iter(contents(db).values()))
    assert (row["state"], row["attempts"]) == ("pending", 0)
    content.run_round()
    assert next(iter(contents(db).values()))["state"] == "done"


# ---------------------------------------------------------------------- 升级后重读


def test_newer_reader_rereads_old_results_when_idle_and_only_online_copies(tmp_path):
    text, helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, state = setup(tmp_path, extractors={"text": text})
    put(root / "纪要.md", "正文")
    put(root / "坏.docx", b"PK\x03\x04 broken")
    index(indexer)
    content.run_round()
    assert len(helper.requests) == 2
    content.run_round()
    assert len(helper.requests) == 2  # 同一版本不重读

    text.version = extract_worker.EXTRACTOR_VERSION + 1
    state["online"] = False
    content.run_round()
    assert len(helper.requests) == 2  # 盘不在：不把读好的变回等待读取
    assert {row["state"] for row in contents(db).values()} == {"done", "unreadable"}
    state["online"] = True
    content.run_round()
    assert len(helper.requests) == 4


def test_default_reader_runs_the_worker_module(tmp_path):
    text = TextExtractor(tmp_path / "data")
    try:
        doc = write_bytes(tmp_path / "a.md", "子进程".encode())
        result = text(doc, "text", {"ext": "md"})
        assert result.status == "ok" and result.blocks == [{"loc": None, "text": "子进程"}]
    finally:
        text.close()


def build_content_tree(root):
    """3b 的格式样本（和 1f 的 build_tree 分开，免得 1f 的计数跟着变）。"""
    from .material_fixtures import (
        build_doc,
        build_encrypted_ooxml,
        build_epub,
        build_odt,
        build_ppt,
        build_pptx,
        build_xlsx,
    )

    put(root / "gbk.txt", "中文 Windows 存的说明".encode("gbk"))
    build_docx(root / "方案.docx", ["方案正文"], header="页眉", comment="批注")
    build_xlsx(root / "预算.xlsx", [("预算", [["服务器", 1]]), ("人员", [["张三"]])])
    build_pptx(root / "汇报.pptx", [["封面"], ["进度"]], notes={2: "备注"})
    build_odt(root / "开放.odt", ["开放格式"])
    build_epub(root / "书.epub", ["第一章"])
    put(root / "网页.html", "<p>网页正文</p>")
    put(root / "邮件.eml", "Subject: 周会\n\n邮件正文\n")
    put(root / "笔记.ipynb", '{"cells": [{"source": "print(1)"}]}')
    put(root / "系统导出.doc", "<html><body>其实是网页</body></html>")
    put(root / "导出.xls", "姓名\t部门\n张三\t市场\n")
    build_xls(root / "老表.xls", [("表", [["老表格"]])])
    build_ppt(root / "老幻灯片.ppt", [["老幻灯片"]])
    build_encrypted_ooxml(root / "加密.docx")
    build_doc(root / "改了扩展名的老文档.docx")
    put(root / "坏.docx", b"PK\x03\x04 broken")
    put(root / "总结.pages", b"PK\x03\x04")
    put(root / "~$方案.docx", b"lock")
    put(root / "cache.pyc", b"\x00\x01")
    put(root / ".env", "SECRET=1")
    for path in root.rglob("*"):
        if path.is_file():
            put(path, path.read_bytes())


def test_content_tree_states(tmp_path, monkeypatch):
    script = __import__("tests.material_fixtures", fromlist=["fake_textutil"]).fake_textutil(
        tmp_path / "bin", output="老 Word 正文"
    )
    monkeypatch.setenv("MEETING_WORKBENCH_TEXTUTIL", str(script))
    text, _helper = extractor(tmp_path)
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path, extractors={"text": text})
    build_content_tree(root)
    index(indexer)
    for _ in range(3):
        content.run_round()
    rows = db.query_all(
        """SELECT f.name, f.content_error, c.state, c.reason FROM material_files f
             LEFT JOIN material_contents c ON c.content_key = f.content_key WHERE f.gone_at IS NULL"""
    )
    by_name = {row["name"]: (row["state"], row["reason"] or row["content_error"]) for row in rows}
    assert "~$方案.docx" not in by_name
    assert by_name["加密.docx"] == ("unreadable", "password")
    assert by_name["坏.docx"] == ("unreadable", "corrupt")
    assert by_name["总结.pages"] == (None, "unsupported")
    assert by_name["cache.pyc"] == (None, None) and by_name[".env"] == (None, None)
    done = {name for name, (state, _reason) in by_name.items() if state == "done"}
    assert done == {
        "gbk.txt", "方案.docx", "预算.xlsx", "汇报.pptx", "开放.odt", "书.epub", "网页.html", "邮件.eml",
        "笔记.ipynb", "系统导出.doc", "导出.xls", "老表.xls", "老幻灯片.ppt", "改了扩展名的老文档.docx",
    }
    for needle in ("中文 Windows", "方案正文", "页眉", "批注", "服务器", "张三", "备注", "开放格式", "第一章",
                   "网页正文", "邮件正文", "print(1)", "其实是网页", "市场", "老表格", "老幻灯片", "老 Word"):
        # 三个字以上走全文索引（按三个字一组切）；两个字的直接扫片段
        found = fts(db, needle) if len(needle) >= 3 else db.query_all(
            "SELECT 1 FROM material_chunks WHERE instr(text, ?) > 0", (needle,)
        )
        assert found, needle
