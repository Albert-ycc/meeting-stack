"""目录部件里同一个目标被反复引用（workbook 的 sheet、presentation 的 sldId、epub 的 spine、幻灯片的备注页）：
那个部件只读一次、先出现的位置不变；正常文档（目标互不相同）的抽取结果和改前逐字相等。"""

from collections import Counter

import pytest

from meeting_workbench import extract_formats as formats

from .material_fixtures import (
    CT,
    P,
    R,
    S,
    _rels,
    _slide,
    build_epub,
    build_pptx,
    build_xlsx,
    write_zip,
)
from .test_extract_formats import locs, read, texts

REPEATS = 1000


@pytest.fixture
def part_reads(monkeypatch):
    """每个部件被打开几次：Package.iterparse 和 Package.read 各算一次。"""
    reads: Counter[str] = Counter()
    real_iterparse, real_read = formats.Package.iterparse, formats.Package.read

    def iterparse(self, name, *args, **kwargs):
        reads[name] += 1
        return real_iterparse(self, name, *args, **kwargs)

    def counting_read(self, name):
        reads[name] += 1
        return real_read(self, name)

    monkeypatch.setattr(formats.Package, "iterparse", iterparse)
    monkeypatch.setattr(formats.Package, "read", counting_read)
    return reads


def sheet_xml(rows: list[list[str]]) -> str:
    body = "".join(
        f'<row r="{number}">'
        + "".join(
            f'<c r="{chr(65 + column)}{number}" t="inlineStr"><is><t>{text}</t></is></c>'
            for column, text in enumerate(row)
        )
        + "</row>"
        for number, row in enumerate(rows, start=1)
    )
    return f'<worksheet xmlns="{S}"><sheetData>{body}</sheetData></worksheet>'


def workbook(sheets: list[tuple[str, str]], rels: dict[str, str], parts: dict[str, str]):
    """sheets 是 (表名, rId)，rels 是 rId → 目标，parts 是目标 → 表的 XML。"""
    entries = "".join(
        f'<sheet name="{name}" sheetId="{number}" r:id="{rid}"/>'
        for number, (name, rid) in enumerate(sheets, start=1)
    )
    return {
        "[Content_Types].xml": CT,
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>{entries}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": _rels(rels),
        **{f"xl/{target}": xml for target, xml in parts.items()},
    }


# ---------------------------------------------------------------------- xlsx


def test_many_sheets_pointing_at_one_rid_read_the_part_once(tmp_path, part_reads):
    members = workbook(
        [(f"s{index}", "rId1") for index in range(REPEATS)],
        {"rId1": "worksheets/sheet1.xml"},
        {"worksheets/sheet1.xml": sheet_xml([["项目", "金额"], ["服务器", "12000"]])},
    )

    answer = read(write_zip(tmp_path / "重复.xlsx", members))

    assert part_reads["xl/worksheets/sheet1.xml"] == 1
    assert answer["status"] == "ok"
    assert texts(answer) == ["项目\t金额\n服务器\t12000"] and locs(answer) == ["表「s0」"]


def test_many_rids_pointing_at_one_target_read_the_part_once(tmp_path, part_reads):
    members = workbook(
        [(f"s{index}", f"rId{index}") for index in range(REPEATS)],
        {f"rId{index}": "worksheets/sheet1.xml" for index in range(REPEATS)},
        {"worksheets/sheet1.xml": sheet_xml([["项目", "金额"]])},
    )

    answer = read(write_zip(tmp_path / "重复2.xlsx", members))

    assert part_reads["xl/worksheets/sheet1.xml"] == 1
    assert texts(answer) == ["项目\t金额"] and locs(answer) == ["表「s0」"]


def test_repeated_sheets_among_distinct_ones_keep_first_seen_order(tmp_path, part_reads):
    members = workbook(
        [("甲", "rId1"), ("乙", "rId2"), ("甲又来", "rId1"), ("丙", "rId3"), ("乙又来", "rId2")],
        {"rId1": "worksheets/a.xml", "rId2": "worksheets/b.xml", "rId3": "worksheets/c.xml"},
        {
            "worksheets/a.xml": sheet_xml([["甲表"]]),
            "worksheets/b.xml": sheet_xml([["乙表"]]),
            "worksheets/c.xml": sheet_xml([["丙表"]]),
        },
    )

    answer = read(write_zip(tmp_path / "穿插.xlsx", members))

    assert texts(answer) == ["甲表", "乙表", "丙表"]
    assert locs(answer) == ["表「甲」", "表「乙」", "表「丙」"]
    assert {name: part_reads[f"xl/worksheets/{name}.xml"] for name in "abc"} == {
        "a": 1,
        "b": 1,
        "c": 1,
    }


# ---------------------------------------------------------------------- pptx


def presentation(slide_rids: list[str], rels: dict[str, str], parts: dict[str, str]):
    ids = "".join(
        f'<p:sldId id="{256 + number}" r:id="{rid}"/>' for number, rid in enumerate(slide_rids)
    )
    return {
        "[Content_Types].xml": CT,
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": _rels(rels),
        **parts,
    }


def test_many_slide_ids_pointing_at_one_slide_read_it_once(tmp_path, part_reads):
    members = presentation(
        ["rId1"] * REPEATS,
        {"rId1": "slides/slide1.xml"},
        {"ppt/slides/slide1.xml": _slide(["封面", "云图AI"])},
    )

    answer = read(write_zip(tmp_path / "重复.pptx", members))

    assert part_reads["ppt/slides/slide1.xml"] == 1
    assert texts(answer) == ["封面\n云图AI"] and locs(answer) == ["第 1 页"]
    assert answer["pages"] == 1


def test_repeated_slides_among_distinct_ones_keep_first_seen_order(tmp_path, part_reads):
    members = presentation(
        ["rId1", "rId2", "rId1", "rId3", "rId2"],
        {"rId1": "slides/slide1.xml", "rId2": "slides/slide2.xml", "rId3": "slides/slide3.xml"},
        {
            "ppt/slides/slide1.xml": _slide(["第一页"]),
            "ppt/slides/slide2.xml": _slide(["第二页"]),
            "ppt/slides/slide3.xml": _slide(["第三页"]),
        },
    )

    answer = read(write_zip(tmp_path / "穿插.pptx", members))

    assert texts(answer) == ["第一页", "第二页", "第三页"]
    assert locs(answer) == ["第 1 页", "第 2 页", "第 3 页"]
    assert answer["pages"] == 3


def test_slides_sharing_one_notes_part_read_it_once(tmp_path, part_reads):
    slides = {f"rId{number}": f"slides/slide{number}.xml" for number in (1, 2, 3)}
    parts = {f"ppt/slides/slide{number}.xml": _slide([f"第{number}页"]) for number in (1, 2, 3)}
    parts["ppt/notesSlides/notesSlide1.xml"] = _slide(["共用备注", "9"])
    for number in (1, 2, 3):
        parts[f"ppt/slides/_rels/slide{number}.xml.rels"] = _rels(
            {"rId9": "../notesSlides/notesSlide1.xml"}
        )
    members = presentation(["rId1", "rId2", "rId3"], slides, parts)

    answer = read(write_zip(tmp_path / "共用备注.pptx", members))

    assert part_reads["ppt/notesSlides/notesSlide1.xml"] == 1
    assert texts(answer) == ["第1页", "共用备注", "第2页", "第3页"]
    assert locs(answer) == ["第 1 页", "第 1 页 备注", "第 2 页", "第 3 页"]


# ---------------------------------------------------------------------- epub


def epub(manifest: dict[str, str], spine: list[str], chapters: dict[str, str]):
    items = "".join(f'<item id="{item_id}" href="{href}"/>' for item_id, href in manifest.items())
    refs = "".join(f'<itemref idref="{item_id}"/>' for item_id in spine)
    return {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        "OEBPS/content.opf": f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}</manifest>'
        f"<spine>{refs}</spine></package>",
        **{
            f"OEBPS/{name}": f"<html><body><p>{text}</p></body></html>"
            for name, text in chapters.items()
        },
    }


def test_spine_pointing_at_one_chapter_over_and_over_reads_it_once(tmp_path, part_reads):
    members = epub({"c": "ch1.xhtml"}, ["c"] * REPEATS, {"ch1.xhtml": "唯一的一章"})

    answer = read(write_zip(tmp_path / "重复.epub", members, stored_first="mimetype"))

    assert part_reads["OEBPS/ch1.xhtml"] == 1
    assert texts(answer) == ["唯一的一章"]


def test_many_manifest_ids_pointing_at_one_href_read_it_once(tmp_path, part_reads):
    ids = [f"c{index}" for index in range(REPEATS)]
    members = epub({item_id: "ch1.xhtml" for item_id in ids}, ids, {"ch1.xhtml": "唯一的一章"})

    answer = read(write_zip(tmp_path / "重复2.epub", members, stored_first="mimetype"))

    assert part_reads["OEBPS/ch1.xhtml"] == 1
    assert texts(answer) == ["唯一的一章"]


def test_repeated_chapters_among_distinct_ones_keep_first_seen_order(tmp_path, part_reads):
    members = epub(
        {"a": "ch1.xhtml", "b": "ch2.xhtml", "c": "ch3.xhtml"},
        ["b", "a", "b", "c", "a"],
        {"ch1.xhtml": "甲章", "ch2.xhtml": "乙章", "ch3.xhtml": "丙章"},
    )

    answer = read(write_zip(tmp_path / "穿插.epub", members, stored_first="mimetype"))

    assert texts(answer) == ["乙章", "甲章", "丙章"]
    assert [part_reads[f"OEBPS/ch{number}.xhtml"] for number in (1, 2, 3)] == [1, 1, 1]


# ---------------------------------------------------------------------- 正常文档不变


def test_normal_documents_read_exactly_as_before(tmp_path):
    """目标互不相同的正常文档：下面的期望值是改前的实现读出来的。"""
    xlsx = build_xlsx(
        tmp_path / "预算.xlsx",
        [
            ("预算", [["项目", "金额"], ["服务器", 12000], ["差旅", 3500.5]]),
            ("备注", [["说明", None, "第三列"]]),
            ("空表", []),
        ],
    )
    pptx = build_pptx(
        tmp_path / "汇报.pptx",
        [["封面", "云图AI"], ["进度"], ["下一步"], ["附录", "补充"]],
        notes={2: "讲到这里停一下", 4: "备注二"},
    )
    book = build_epub(tmp_path / "书.epub", ["第一章", "第二章", "第三章", "第四章"])

    answers = {path.name: read(path) for path in (xlsx, pptx, book)}

    assert [(block["loc"], block["text"]) for block in answers["预算.xlsx"]["blocks"]] == [
        ("表「预算」", "项目\t金额\n服务器\t12000\n差旅\t3500.5"),
        ("表「备注」", "说明\t\t第三列"),
    ]
    assert [(block["loc"], block["text"]) for block in answers["汇报.pptx"]["blocks"]] == [
        ("第 1 页", "封面\n云图AI"),
        ("第 2 页", "进度"),
        ("第 2 页 备注", "讲到这里停一下"),
        ("第 3 页", "下一步"),
        ("第 4 页", "附录\n补充"),
        ("第 4 页 备注", "备注二"),
    ]
    assert answers["汇报.pptx"]["pages"] == 4
    assert [block["text"] for block in answers["书.epub"]["blocks"]] == [
        "第一章",
        "第二章",
        "第三章",
        "第四章",
    ]
    assert all(answer["status"] == "ok" for answer in answers.values())
