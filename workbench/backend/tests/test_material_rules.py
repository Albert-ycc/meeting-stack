"""第三期 3b：哪些文件读什么、哪些名字静默跳过；文件名索引、关系图浏览、文件夹统计用同一套判断。"""

from meeting_workbench import graph, materials
from meeting_workbench.material_index import MaterialIndexer
from meeting_workbench.material_rules import (
    LAYER_IMAGE,
    LAYER_MEDIA,
    LAYER_PDF,
    LAYER_TEXT,
    LAYER_UNSUPPORTED,
    hidden_in_browse,
    layer_for,
    silent_skip,
)

from .test_material_index import add_root, files, make, run_until_done, write


def test_layers_by_extension():
    assert layer_for("方案.DOCX") == LAYER_TEXT
    assert layer_for("main.swift") == LAYER_TEXT
    assert layer_for("旧表.et") == LAYER_TEXT
    assert layer_for("合同.pdf") == LAYER_PDF
    assert layer_for("白板.HEIC") == LAYER_IMAGE
    assert layer_for("访谈.m4a") == LAYER_MEDIA
    assert layer_for("汇报.key") == LAYER_UNSUPPORTED
    for name in (
        "a.pyc",
        "lib.so",
        "app.dll",
        "secrets.env",
        "package.lock",
        "bundle.js.map",
        "包.zip",
        "字体.ttf",
    ):
        assert layer_for(name) is None, name


def test_silent_skip_and_hidden_in_browse():
    for name in (
        "._报价.pdf",
        ".DS_Store",
        "__MACOSX",
        "Thumbs.db",
        "~$方案.docx",
        ".~lock.报价.xlsx#",
    ):
        assert silent_skip(name) and hidden_in_browse(name), name
    assert not silent_skip(".gitignore") and hidden_in_browse(".gitignore")
    assert not silent_skip("方案.docx") and not hidden_in_browse("方案.docx")


def test_index_browse_and_folder_stats_share_the_same_rules(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "方案.docx")
    write(root / "~$方案.docx")
    write(root / ".~lock.报价.xlsx#")
    write(root / "Thumbs.db")
    write(root / ".gitignore")
    add_root(db, root)
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    names = set(files(db))
    assert (
        "~$方案.docx" not in names and ".~lock.报价.xlsx#" not in names and "Thumbs.db" not in names
    )
    assert "方案.docx" in names

    listing = graph.expand_folder({"id": 1, "project_id": "p", "path": str(root)})
    assert [item["name"] for item in listing["files"]] == ["方案.docx"]
    assert materials.folder_stat(root)["file_count"] == 1
