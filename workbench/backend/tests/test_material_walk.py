"""1f 材料盘点与图片文字识别试跑：只读、分层统计、读不了的五种原因、挑图和对照报告。"""
import json
import os
import zipfile
import zlib
from pathlib import Path

import pytest

from meeting_workbench import cli, material_walk, ocr_trial
from meeting_workbench.db import Database, utc_now
from meeting_workbench.material_walk import render_report, walk_materials


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def docx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")


def build_tree(root: Path) -> None:
    write(root / "合同" / "报价.pdf", b"%PDF-1.4\n1 0 obj << /Type /Font /BaseFont /Song >> endobj\n")
    write(root / "合同" / "扫描件.pdf", b"%PDF-1.4\n1 0 obj << /XObject << /Im0 2 0 R >> /Subtype /Image >>\n")
    write(root / "合同" / "加密.pdf", b"%PDF-1.6\ntrailer << /Encrypt 5 0 R >>\n")
    write(root / "合同" / "坏.pdf", b"not a pdf at all")
    packed = zlib.compress(b"<< /Type /Font /Subtype /Type0 >>")
    write(root / "合同" / "新版.pdf", b"%PDF-1.7\n3 0 obj << /Type /ObjStm /Filter /FlateDecode >>\nstream\n" + packed + b"\nendstream\n")
    docx(root / "文档" / "方案.docx")
    write(root / "文档" / "加密.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
    write(root / "文档" / "坏.docx", b"PK broken")
    write(root / "文档" / "总结.pages" / "Index.zip", b"x" * 100)
    write(root / "文档" / "总结.pages" / "preview.jpg", b"y" * 50)
    write(root / "文档" / "纪要.md", "# 纪要")
    write(root / "图片" / "白板.png", b"\x89PNG" + b"0" * 30_000)
    write(root / "图片" / "icon.png", b"\x89PNG" + b"0" * 100)
    write(root / "备份" / "白板.png", b"\x89PNG" + b"0" * 30_000)
    write(root / "录音" / "访谈.m4a", b"a" * 2000)
    write(root / "录音" / "演示.mov", b"v" * 3000)
    write(root / "压缩包.zip", b"PK")
    write(root / "._报价.pdf", b"shadow")
    write(root / ".DS_Store", b"ds")
    write(root / "__MACOSX" / "x", b"mac")
    write(root / "前端" / "node_modules" / "a" / "b.js", "js")
    write(root / "前端" / ".git" / "HEAD", "ref")
    write(root / "声档会议记录" / "260926 周会.md", "卡片")
    os.symlink(root / "文档", root / "快捷方式")


def test_walk_counts_layers_skips_system_files_and_keeps_name_only_dirs_apart(tmp_path):
    root = tmp_path / "云图AI"
    build_tree(root)

    report = walk_materials([{"path": str(root), "project_name": "云图AI"}], probe_media=False)

    layers = report["layers"]
    # 文档：5 个 PDF、3 个 docx、1 个 pages 包、1 个 md
    assert layers["text"]["count"] == 10
    assert (layers["image"]["count"], layers["image"]["small"]) == (3, 1)
    assert (layers["media"]["count"], layers["media"]["audio"], layers["media"]["video"]) == (2, 1, 1)
    assert layers["name_only"]["count"] == 1
    assert report["pdf"] == {"text": 2, "scanned": 1, "unknown": 0}
    unreadable = {reason: bucket["count"] for reason, bucket in report["unreadable"].items()}
    assert unreadable == {"password": 2, "corrupt": 2, "unsupported": 1, "timeout": 0, "permission": 0}
    assert report["skipped_system"] == 3
    assert report["symlinks"] == 1
    assert report["packages"] == 1
    assert {name: bucket["files"] for name, bucket in report["name_only_dirs"].items()} == {
        "node_modules": 1, ".git": 1, "声档会议记录": 1,
    }
    assert report["duplicates"]["groups"] == 1
    # 总数也算上只收文件名的那 3 个
    assert report["roots"][0]["files"] == report["files"] == 19
    assert report["media_probe"] == "off"


def test_walk_reads_media_duration_when_ffprobe_is_there(tmp_path, monkeypatch):
    root = tmp_path / "材料"
    write(root / "访谈.m4a", b"a" * 10)
    write(root / "演示.mov", b"v" * 10)
    monkeypatch.setattr(material_walk.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        material_walk,
        "ffprobe_duration",
        lambda path, ffprobe: (None, 5400.0) if path.suffix == ".m4a" else (material_walk.TIMEOUT, None),
    )

    report = walk_materials([{"path": str(root)}])

    assert report["layers"]["media"]["seconds"] == 5400.0
    assert report["layers"]["media"]["probed"] == 1
    assert report["unreadable"]["timeout"]["count"] == 1
    assert "读到时长的 1 个共 1.5 小时" in render_report(report)


def test_offline_and_nested_roots_are_listed_but_not_walked(tmp_path):
    root = tmp_path / "云图AI"
    write(root / "合同" / "报价.pdf", b"%PDF-1.4 /Font")

    def state_of(path):
        return "volume_offline" if path.startswith("/Volumes/") else material_walk_state(path)

    from meeting_workbench.materials import volume_state as material_walk_state

    report = walk_materials(
        [
            {"path": str(root), "project_name": "云图AI"},
            {"path": str(root / "合同"), "project_name": "云图合同"},
            {"path": "/Volumes/资料盘/数据中台", "project_name": "数据中台"},
        ],
        state_of=state_of,
    )

    assert [item["state"] for item in report["roots"]] == ["online", "nested", "volume_offline"]
    assert report["files"] == 1
    text = render_report(report)
    assert "数据中台：/Volumes/资料盘/数据中台（盘没插，没走）" in text
    assert "已经一起算了" in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受文件权限限制")
def test_files_without_permission_are_counted(tmp_path):
    root = tmp_path / "材料"
    locked = write(root / "工资.xlsx", b"PK")
    locked.chmod(0)
    try:
        report = walk_materials([{"path": str(root)}], probe_media=False)
    finally:
        locked.chmod(0o644)
    assert report["unreadable"]["permission"]["count"] == 1


def test_walk_does_not_touch_the_tree(tmp_path):
    root = tmp_path / "云图AI"
    build_tree(root)
    before = sorted((str(path), path.lstat().st_mtime_ns) for path in root.rglob("*"))

    walk_materials([{"path": str(root)}], probe_media=False)

    assert sorted((str(path), path.lstat().st_mtime_ns) for path in root.rglob("*")) == before


# —— 命令行 ——


def settings_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(data_dir))
    monkeypatch.delenv("MEETING_WORKBENCH_DATABASE_PATH", raising=False)
    return data_dir


def test_cli_walk_needs_dry_run_and_reads_roots_from_the_database(tmp_path, monkeypatch, capsys):
    data_dir = settings_env(tmp_path, monkeypatch)
    root = tmp_path / "云图AI"
    build_tree(root)
    db = Database(data_dir / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO projects(id, name, color, created_at) VALUES ('p-yt', '云图AI', '#123456', ?)", (utc_now(),)
    )
    db.execute(
        """INSERT INTO project_material_roots(project_id, path, created_at)
           VALUES ('p-yt', ?, ?)""",
        (str(root), utc_now()),
    )
    db_file = data_dir / "workbench.sqlite3"
    stamp = db_file.stat().st_mtime_ns

    assert cli.main(["materials", "walk", "--project", "云图AI"]) == 2
    assert "请加 --dry-run" in capsys.readouterr().err

    out = tmp_path / "walk.json"
    assert cli.main(["materials", "walk", "--dry-run", "--project", "云图ai", "--no-probe", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"云图AI：{root}" in printed
    assert "读不了的（文件名照样能搜到）" in printed
    assert json.loads(out.read_text(encoding="utf-8"))["files"] == 19
    assert db_file.stat().st_mtime_ns == stamp

    with pytest.raises(SystemExit, match="没有叫「数据中台」的项目。现有项目：云图AI"):
        cli.main(["materials", "walk", "--dry-run", "--project", "数据中台"])


def test_cli_walk_with_root_skips_the_database(tmp_path, monkeypatch, capsys):
    settings_env(tmp_path, monkeypatch)
    root = tmp_path / "随便"
    write(root / "a.txt", "hi")

    assert cli.main(["materials", "walk", "--dry-run", "--root", str(root)]) == 0
    assert "1 个文件" in capsys.readouterr().out
    assert not (tmp_path / "data" / "workbench.sqlite3").exists()


# —— 图片文字识别试跑 ——


def test_collect_and_pick_images_spread_across_folders(tmp_path):
    root = tmp_path / "材料"
    for folder, count in (("合同", 6), ("白板", 3), ("截图", 1)):
        for index in range(count):
            write(root / folder / f"{index:02d}.png", b"0" * 25_000)
    write(root / "白板" / "icon.png", b"0" * 100)
    write(root / "._00.png", b"0" * 25_000)
    write(root / "node_modules" / "x.png", b"0" * 25_000)
    write(root / "声档会议记录" / "图.png", b"0" * 25_000)

    images = ocr_trial.collect_images([root])
    assert len(images) == 10

    picked = ocr_trial.pick_images(images, [root], limit=5)
    folders = [path.parent.name for path in picked]
    assert sorted(folders) == sorted(["合同", "合同", "白板", "白板", "截图"])
    assert ocr_trial.pick_images(images, [root], limit=5) == picked
    assert len(ocr_trial.pick_images(images, [root], limit=50)) == 10


def test_engines_explain_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_trial.shutil, "which", lambda name: None)
    engines, notes = ocr_trial.prepare_engines(["vision", "tesseract"], tmp_path, system="Linux")
    assert engines == {}
    assert notes == [
        "Vision 没跑：Vision 只能在 Mac 上跑",
        "tesseract 没跑：没找到 tesseract：brew install tesseract tesseract-lang",
    ]
    engines, notes = ocr_trial.prepare_engines(["vision"], tmp_path, system="Darwin")
    assert notes == ["Vision 没跑：没找到 swiftc：先在终端运行 xcode-select --install 装 Xcode 命令行工具"]


def test_trial_report_compares_engines_side_by_side(tmp_path):
    images = [write(tmp_path / "a.png", b"0" * 30_000), write(tmp_path / "b.heic", b"0" * 40_000)]
    engines = {
        "vision": lambda path: {"text": "数理协会 成立", "seconds": 0.4, "error": None},
        "tesseract": lambda path: (
            {"text": "", "seconds": 0.0, "error": "tesseract 读不了 HEIC"}
            if path.suffix == ".heic"
            else {"text": "数理 协会", "seconds": 1.2, "error": None}
        ),
    }

    report = ocr_trial.run_trial(images, engines)

    assert report["summary"]["vision"] == {"images": 2, "succeeded": 2, "avg_seconds": 0.4, "chars": 12, "empty": 0}
    assert report["summary"]["tesseract"]["succeeded"] == 1
    markdown = ocr_trial.render_markdown(report, ["tesseract 没装中文语言包，只能认英文：brew install tesseract-lang"])
    assert "| Vision（macOS 自带） | 2/2 | 0.4 秒 | 12 | 0 |" in markdown
    assert "**tesseract**：没认成，tesseract 读不了 HEIC" in markdown
    assert "```text\n数理协会 成立\n```" in markdown


def test_cli_ocr_trial_writes_results_into_the_data_dir(tmp_path, monkeypatch, capsys):
    data_dir = settings_env(tmp_path, monkeypatch)
    root = tmp_path / "材料"
    write(root / "白板" / "01.png", b"0" * 30_000)
    monkeypatch.setattr(
        ocr_trial,
        "prepare_engines",
        lambda wanted, workdir: ({"vision": lambda path: {"text": "你好", "seconds": 0.3, "error": None}}, []),
    )

    assert cli.main(["materials", "ocr-trial", "--root", str(root)]) == 0

    [result_dir] = list((data_dir / "ocr-trial").iterdir())
    assert "你好" in (result_dir / "结果.md").read_text(encoding="utf-8")
    assert json.loads((result_dir / "result.json").read_text(encoding="utf-8"))["summary"]["vision"]["chars"] == 2
    assert "Vision（macOS 自带）：认出 1/1 张" in capsys.readouterr().out


def test_swift_source_asks_for_chinese_and_english(tmp_path):
    assert '"zh-Hans", "en-US"' in ocr_trial.SWIFT_SOURCE
    assert "VNRecognizeTextRequest" in ocr_trial.SWIFT_SOURCE
