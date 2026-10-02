"""第三期 3c：认字引擎的选择和探测、Vision 程序的编译、图片和 PDF 读取、tesseract、预览图、doctor。

Vision 程序只能在 Mac 上编译运行，这里都用假的常驻进程和假的 swiftc、tesseract、sips。
"""

import json
import os
import signal
import struct
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meeting_workbench import cli, material_previews, ocr_engines, ocr_trial, vision_helper
from meeting_workbench.config import Settings
from meeting_workbench.material_helpers import HelperCrashed, HelperTimeout
from meeting_workbench.ocr_engines import (
    ImageExtractor,
    OcrEngines,
    PdfExtractor,
    arrange_lines,
    image_size,
    is_small,
    needs_ocr,
    tidy_ocr_text,
)
from meeting_workbench.vision_helper import SWIFT_SOURCE, VisionBuild, find_swiftc

from .test_material_content import contents, index, put, setup


@pytest.fixture(autouse=True)
def no_homebrew_tools(monkeypatch):
    """程序在不在全由各用例的假 which 决定；不让本机 Homebrew 里真装着的 tesseract 混进来。"""
    monkeypatch.setattr(ocr_engines, "HOMEBREW_BINS", ())


def png(width: int, height: int, *, padding: int = 0) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"\0" * (4 + padding)
    )


class FakeBuild:
    def __init__(self, data_dir, *, ready=True, compiling=False):
        self._ready = ready
        self._compiling = compiling
        self.binary = Path(data_dir) / "bin" / "sd-vision-test"
        self.ensured = 0

    def ready(self):
        return self._ready

    def compiling(self):
        return self._compiling

    def ensure(self, *, background=True):
        self.ensured += 1
        return self._ready

    def describe(self):
        return "能用" if self._ready else "还没编译"


class FakeVision:
    """假的 Vision 常驻进程：按命令回脚本里的答案。"""

    def __init__(self, *, pages=None, lines=None, image=None, fail_at=None, on_request=None):
        self.pages = pages or {}
        self.lines = lines or {}
        self.image = image
        self.fail_at = fail_at
        self.on_request = on_request
        self.requests = []
        self.killed = 0

    def request(self, payload, *, timeout, **kwargs):
        self.requests.append((payload, timeout))
        if self.on_request:
            self.on_request(payload)
        command = payload["cmd"]
        key = (command, payload.get("page"))
        if self.fail_at is not None and key == self.fail_at:
            raise HelperTimeout("假的超时")
        if command == "image":
            return self.image or {"status": "ok", "small": False, "pages": 1, "lines": []}
        if command == "pdf_open":
            if isinstance(self.pages, str):
                return {"status": self.pages}
            return {"status": "ok", "pages": len(self.pages)}
        if command == "pdf_text":
            return {"status": "ok", "text": self.pages[payload["page"]]}
        if command == "pdf_ocr":
            return {"status": "ok", "lines": self.lines.get(payload["page"], [])}
        if command == "pdf_render":
            Path(payload["out"]).write_bytes(png(1700, 2200))
            return {"status": "ok"}
        raise AssertionError(command)

    def kill(self):
        self.killed += 1

    def close(self):
        pass


def fake_bin(folder: Path, name: str, body: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def tesseract_bin(folder: Path, output: str) -> str:
    log = folder / "tesseract.args"
    return fake_bin(
        folder,
        "tesseract",
        f'if [ "$1" = "--list-langs" ]; then printf "List of available languages:\\nchi_sim\\neng\\n"; exit 0; fi\n'
        f'if [ "$1" = "--version" ]; then echo "tesseract 5.4.1"; exit 0; fi\n'
        f'echo "$@ OMP=$OMP_THREAD_LIMIT" >> "{log}"\n'
        f"printf '{output}'\n",
    )


def sips_bin(folder: Path) -> str:
    # 把 --out 后面的路径写成一张够大的 PNG
    return fake_bin(
        folder,
        "sips",
        'while [ $# -gt 0 ]; do if [ "$1" = "--out" ]; then out="$2"; fi; shift; done\n'
        "printf '\\211PNG\\r\\n\\032\\n\\000\\000\\000\\rIHDR\\000\\000\\003\\000\\000\\000\\002\\000' > \"$out\"\n",
    )


def make_engines(
    db, settings, tmp_path, *, tools=(), build=None, vision=None, busy=None, system="darwin"
):
    folder = tmp_path / "fakebin"
    found = {}
    for name in tools:
        if name == "tesseract":
            found[name] = tesseract_bin(
                folder, "数 理 协 会 的 报 告\\n第二行 English words\\n\\n第二段"
            )
        elif name == "sips":
            found[name] = sips_bin(folder)
        else:
            found[name] = fake_bin(folder, name, "exit 0\n")

    def which(name):
        return found.get(name)

    def run(argv, **kwargs):
        if argv[0] == "xcode-select":
            return completed(argv, 2)
        return subprocess.run(argv, **kwargs)

    vision = vision or FakeVision()
    engines = OcrEngines(
        db,
        settings,
        system=system,
        run=run,
        which=which,
        busy_check=busy,
        build=build or FakeBuild(settings.data_dir, ready=False),
        helper_factory=lambda binary: vision,
    )
    return engines, vision


def extractors(engines):
    return {"image": ImageExtractor(engines), "pdf": PdfExtractor(engines)}


# ---------------------------------------------------------------------- Swift 源码和编译


def test_swift_source_keeps_the_recognition_settings():
    assert '"zh-Hans", "en-US"' in SWIFT_SOURCE
    assert "VNRecognizeTextRequest" in SWIFT_SOURCE
    assert ".accurate" in SWIFT_SOURCE and "usesLanguageCorrection = true" in SWIFT_SOURCE
    assert "autoreleasepool" in SWIFT_SOURCE and 'unlock(withPassword: "")' in SWIFT_SOURCE


def test_find_swiftc_asks_xcode_select_before_touching_swiftc():
    calls = []

    def missing(argv, **kwargs):
        calls.append(argv[0])
        return completed(argv, 2)

    assert find_swiftc(system="darwin", run=missing) is None
    assert calls == ["xcode-select"]  # 没装命令行工具时不碰 swiftc（会弹安装对话框）

    def present(argv, **kwargs):
        calls.append(argv[0])
        return completed(
            argv,
            0,
            "/Library/Developer/CommandLineTools/usr/bin/swiftc\n" if argv[0] == "xcrun" else "/x",
        )

    assert (
        find_swiftc(system="darwin", run=present)
        == "/Library/Developer/CommandLineTools/usr/bin/swiftc"
    )
    calls.clear()
    assert find_swiftc(system="linux", run=present) is None and calls == []


class Compiler:
    def __init__(self, *, ok=True, version="Apple Swift version 6.0"):
        self.ok = ok
        self.version = version
        self.compiles = 0

    def __call__(self, argv, **kwargs):
        if argv[-1] == "--version":
            return completed(argv, 0, self.version + "\n")
        self.compiles += 1
        if not self.ok:
            return completed(
                argv,
                1,
                "",
                "vision.swift:3:8: warning\nvision.swift:9:1: error: no such module 'Vision'\n",
            )
        Path(argv[argv.index("-o") + 1]).write_bytes(b"binary")
        return completed(argv, 0)


def test_compile_timeout_kills_the_whole_process_group(tmp_path):
    """编译超时：xcrun 起的 swift-frontend 是孙进程，只杀直接子进程的话它会留着接着占 CPU。
    用假编译器（先起一个孙进程，再自己卡住）验证整组被杀。"""
    pidfile = tmp_path / "grandchild.pid"
    compiler = fake_bin(
        tmp_path / "fakebin", "fake-compiler", f'sleep 300 &\necho $! > "{pidfile}"\nsleep 300\n'
    )
    assert VisionBuild(tmp_path, find=lambda: None).compile_run is vision_helper.run_in_group
    grandchild = None
    try:
        # 超时给 5 秒：机器忙时 sh 要一会儿才起得来孙进程、写下 PID（1 秒在全量跑时不够）
        with pytest.raises(subprocess.TimeoutExpired):
            vision_helper.run_in_group([compiler], timeout=5, capture_output=True, text=True)
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"孙进程 {grandchild} 超时后还活着")
    finally:
        if grandchild is not None:
            try:
                os.kill(grandchild, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_vision_build_compiles_once_and_backs_off_after_failure(tmp_path):
    now = {"value": datetime(2026, 9, 27, tzinfo=UTC)}
    compiler = Compiler(ok=False)
    build = VisionBuild(
        tmp_path, run=compiler, now=lambda: now["value"], find=lambda: "/usr/bin/swiftc"
    )
    assert build.ensure(background=False) is False
    assert build.describe() == "编译失败：vision.swift:9:1: error: no such module 'Vision'"
    assert (
        build.ensure(background=False) is False and compiler.compiles == 1
    )  # 源码和版本都没变：不重编
    now["value"] += timedelta(hours=25)
    build.ensure(background=False)
    assert compiler.compiles == 2  # 一天后再试一次
    compiler.version = "Apple Swift version 6.1"
    build.ensure(background=False)
    assert compiler.compiles == 3  # swiftc 升级了：再试
    compiler.ok = True
    compiler.version = "Apple Swift version 6.2"
    assert build.ensure(background=False) is True
    assert (
        build.ready() and build.binary.name.startswith("sd-vision-") and build.describe() == "能用"
    )
    assert not list(build.binary.parent.glob(".*.tmp"))
    assert build.ensure(background=False) is True and compiler.compiles == 4
    other = VisionBuild(
        tmp_path, source=SWIFT_SOURCE + "\n// 改过", run=compiler, find=lambda: "/usr/bin/swiftc"
    )
    assert other.binary != build.binary and not other.ready()  # 源码一变就重编


def test_vision_build_runs_in_the_background(tmp_path):
    gate = threading.Event()
    compiler = Compiler()

    def slow(argv, **kwargs):
        if argv[-1] != "--version":
            gate.wait(5)
        return compiler(argv, **kwargs)

    build = VisionBuild(tmp_path, run=slow, find=lambda: "/usr/bin/swiftc")
    assert build.ensure() is False and build.compiling() and build.describe() == "正在编译"
    gate.set()
    build.wait(5)
    assert build.ready()


def test_no_swiftc_is_described(tmp_path):
    build = VisionBuild(tmp_path, find=lambda: None)
    assert build.ensure(background=False) is False
    assert build.describe().startswith("没有 swiftc")


# ---------------------------------------------------------------------- 引擎选择


def test_engine_selection_and_switching(tmp_path):
    db, settings, *_rest = setup(tmp_path)
    engines, _vision = make_engines(db, settings, tmp_path)
    assert engines.setting() == "auto" and engines.image_engine() is None
    engines, _vision = make_engines(db, settings, tmp_path, tools=("tesseract",))
    assert engines.image_engine() == "tesseract"
    ready = FakeBuild(settings.data_dir, ready=True)
    engines, _vision = make_engines(db, settings, tmp_path, tools=("tesseract",), build=ready)
    assert engines.image_engine() == "vision"
    engines.set_engine("tesseract")
    assert engines.image_engine() == "tesseract"
    engines.set_engine("off")
    assert engines.image_engine() == "off"
    engines, _vision = make_engines(db, settings, tmp_path, tools=("tesseract",))
    engines.set_engine("vision")
    assert engines.image_engine() is None  # 选了 Vision 但还没编好
    with pytest.raises(ValueError):
        engines.set_engine("paddle")


def test_tesseract_without_chinese_does_not_count(tmp_path):
    db, settings, *_rest = setup(tmp_path)
    folder = tmp_path / "fakebin"
    english = fake_bin(
        folder, "tesseract", 'if [ "$1" = "--list-langs" ]; then printf "eng\\n"; fi\n'
    )
    engines = OcrEngines(
        db,
        settings,
        system="darwin",
        run=lambda argv, **kw: (
            subprocess.run(argv, **kw) if argv[0] != "xcode-select" else completed(argv, 2)
        ),
        which=lambda name: english if name == "tesseract" else None,
        build=FakeBuild(settings.data_dir, ready=False),
    )
    assert engines.tools().tesseract and not engines.tools().tesseract_chinese
    assert engines.image_engine() is None


# ---------------------------------------------------------------------- 图片


def test_images_with_vision_small_images_and_no_text(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    vision = FakeVision(
        image={
            "status": "ok",
            "pages": 1,
            "lines": [
                {"t": "白板上写的 第一行", "c": 0.9, "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.05},
                {"t": "第二行", "c": 0.9, "x": 0.1, "y": 0.16, "w": 0.3, "h": 0.05},
            ],
        }
    )
    engines, _ = make_engines(
        db, settings, tmp_path, build=FakeBuild(settings.data_dir), vision=vision
    )
    content.extractors.update(extractors(engines))
    put(root / "白板.png", png(1600, 1200, padding=2000))
    put(root / "一行字.png", png(600, 200, padding=15_000))  # 15KB 的文字截图照样认
    put(root / "图标.png", png(64, 64))
    index(indexer)
    content.run_round()
    by_name = {
        row["name"]: row
        for row in db.query_all(
            "SELECT f.name, c.state, c.note, c.chunks, c.extractor FROM material_files f "
            "JOIN material_contents c ON c.content_key = f.content_key"
        )
    }
    assert (by_name["图标.png"]["state"], by_name["图标.png"]["note"]) == ("done", "small_image")
    assert by_name["白板.png"]["extractor"] == "vision" and by_name["白板.png"]["chunks"] == 1
    assert len(vision.requests) == 2  # 小图没送去认
    texts = [row["text"] for row in db.query_all("SELECT text FROM material_chunks")]
    assert texts == ["白板上写的第一行第二行"] * 2  # 白板和一行字的截图各一段

    vision.image = {"status": "ok", "pages": 1, "lines": []}
    put(root / "空白.png", png(1600, 1200, padding=3000))
    index(indexer)
    content.run_round()
    row = db.query_one(
        "SELECT c.state, c.note FROM material_files f JOIN material_contents c "
        "ON c.content_key = f.content_key WHERE f.name = '空白.png'"
    )
    assert (row["state"], row["note"]) == ("done", "no_text")


def test_tesseract_leaves_the_materials_untouched(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    engines, _ = make_engines(db, settings, tmp_path, tools=("tesseract", "sips"))
    content.extractors.update(extractors(engines))
    photo = put(root / "现场.heic", b"\x00\x00\x00\x18ftypheic" + b"\0" * 5000)
    before = (photo.read_bytes(), photo.stat().st_mtime_ns)
    index(indexer)
    listing = sorted(path.name for path in root.iterdir())
    content.run_round()
    assert (photo.read_bytes(), photo.stat().st_mtime_ns) == before
    assert sorted(path.name for path in root.iterdir()) == listing
    assert list((settings.data_dir / "material-ocr-tmp").iterdir()) == []
    arguments = (tmp_path / "fakebin" / "tesseract.args").read_text(encoding="utf-8")
    assert "stdout -l chi_sim+eng -c preserve_interword_spaces=1 OMP=1" in arguments
    assert str(photo) not in arguments  # HEIC 先转成 data_dir 里的 PNG
    texts = [
        row["text"] for row in db.query_all("SELECT text FROM material_chunks ORDER BY ordinal")
    ]
    assert texts == ["数理协会的报告第二行 English words\n第二段"]


def test_missing_engines_mean_waiting_and_come_back_when_installed(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    engines, _ = make_engines(db, settings, tmp_path)
    clock = {"value": 0.0}
    engines.clock = lambda: clock["value"]
    content.extractors.update(extractors(engines))
    content.before_round = engines.refresh
    put(root / "白板.png", png(1600, 1200))
    put(root / "扫描件.pdf", b"%PDF-1.7\n")
    index(indexer)
    content.run_round()
    states = {
        row["layer"]: (row["state"], row["note"], row["reason"]) for row in contents(db).values()
    }
    assert states == {
        "image": ("waiting", "engine_missing", None),
        "pdf": ("waiting", "engine_missing", None),
    }

    # 装上了 tesseract：10 分钟内不重新看
    engines.which = lambda name: (
        tesseract_bin(tmp_path / "fakebin", "认出来的字") if name == "tesseract" else None
    )
    clock["value"] = 300
    content.run_round()
    assert contents(db)[next(iter(contents(db)))]["state"] == "waiting"
    clock["value"] = 700
    content.run_round()
    states = {row["layer"]: row["state"] for row in contents(db).values()}
    assert states == {"image": "done", "pdf": "waiting"}  # PDF 要等 Vision 程序


def test_compiling_holds_back_images_and_pdfs(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    build = FakeBuild(settings.data_dir, ready=False, compiling=True)
    engines, vision = make_engines(db, settings, tmp_path, tools=("tesseract",), build=build)
    content.extractors.update(extractors(engines))
    put(root / "白板.png", png(1600, 1200))
    put(root / "合同.pdf", b"%PDF-1.7\n")
    index(indexer)
    content.run_round()
    assert {row["state"] for row in contents(db).values()} == {"pending"}


def test_switching_back_from_off_rereads_skipped_images(tmp_path):
    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path)
    engines, vision = make_engines(db, settings, tmp_path, build=FakeBuild(settings.data_dir))
    content.extractors.update(extractors(engines))
    engines.set_engine("off")
    put(root / "白板.png", png(1600, 1200))
    index(indexer)
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["extractor"]) == ("done", "off") and vision.requests == []
    assert engines.set_engine("auto") == {"reread": 1}
    content.run_round()
    assert next(iter(contents(db).values()))["extractor"] == "vision" and len(vision.requests) == 1
    assert engines.set_engine("tesseract") == {"reread": 0}  # 已经认过的不重认


# ---------------------------------------------------------------------- PDF


def pdf_setup(tmp_path, *, pages, busy=None, **vision_kwargs):
    db, settings, root, root_id, content, indexer, now, _state = setup(tmp_path)
    vision = FakeVision(pages=pages, **vision_kwargs)
    engines, _ = make_engines(
        db,
        settings,
        tmp_path,
        tools=("tesseract",),
        build=FakeBuild(settings.data_dir),
        vision=vision,
        busy=busy,
    )
    content.extractors.update(extractors(engines))
    put(root / "合同.pdf", b"%PDF-1.7\n" + b"0" * 100)
    index(indexer)
    return db, settings, content, engines, vision, now


LONG = "本合同由甲乙双方在平等自愿的基础上签订，约定交付时间和验收标准。"


def chunk_rows(db):
    return [
        (row["loc"], row["text"])
        for row in db.query_all("SELECT loc, text FROM material_chunks ORDER BY ordinal")
    ]


def test_pdf_text_layer_is_read_whatever_the_engine(tmp_path):
    db, settings, content, engines, vision, _now = pdf_setup(tmp_path, pages=[LONG, ""])
    engines.set_engine("off")
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["extractor"], row["pages"]) == ("done", "pdfkit+off", 2)
    assert chunk_rows(db) == [("第 1 页", LONG)]
    assert [payload["cmd"] for payload, _t in vision.requests] == [
        "pdf_open",
        "pdf_text",
        "pdf_text",
    ]


def test_pdf_same_path_replaced_is_reopened_not_read_from_the_cache(tmp_path):
    """同名覆盖成新版后再读：每份 PDF 读之前都先发 pdf_open，Vision 程序收到 pdf_open 一律重开，
    别的命令按路径加文件身份（inode、大小、修改时间）认缓存。Swift 那边的行为要在 Mac 上真编译才测得到，
    这里钉住两头的约定。"""
    db, settings, content, engines, vision, _now = pdf_setup(tmp_path, pages=[LONG])
    path = tmp_path / "方案.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"0" * 100)
    extractor = PdfExtractor(engines)
    extractor(path, "pdf", {"content_key": "q2:aa", "reason": None})
    vision.pages = [LONG, LONG, LONG]
    path.unlink()
    path.write_bytes(b"%PDF-1.7\n" + b"1" * 300)
    second = extractor(path, "pdf", {"content_key": "q2:bb", "reason": None})
    assert second.pages == 3
    commands = [(payload["cmd"], payload["path"]) for payload, _t in vision.requests]
    assert commands == [
        ("pdf_open", str(path)),
        ("pdf_text", str(path)),
        ("pdf_open", str(path)),
        ("pdf_text", str(path)),
        ("pdf_text", str(path)),
        ("pdf_text", str(path)),
    ]
    assert 'openPDF(path, reuse: command != "pdf_open")' in SWIFT_SOURCE
    assert "openPath == path, stamp != nil, openStamp == stamp" in SWIFT_SOURCE
    assert "info.st_ino" in SWIFT_SOURCE and "info.st_size" in SWIFT_SOURCE


def test_pdf_scanned_pages_use_vision_or_tesseract(tmp_path):
    lines = {1: [{"t": "扫 描 页 上 的 字", "x": 0.1, "y": 0.1, "h": 0.03, "p": 1}]}
    db, settings, content, engines, vision, _now = pdf_setup(
        tmp_path, pages=[LONG, ""], lines=lines
    )
    content.run_round()
    assert chunk_rows(db) == [("第 1 页", LONG), ("第 2 页", "扫描页上的字")]
    assert next(iter(contents(db).values()))["extractor"] == "pdfkit+vision"

    db2, settings2, content2, engines2, vision2, _ = pdf_setup(tmp_path / "t", pages=[LONG, ""])
    engines2.set_engine("tesseract")
    content2.run_round()
    assert chunk_rows(db2)[1] == ("第 2 页", "数理协会的报告第二行 English words\n\n第二段")
    render = [payload for payload, _t in vision2.requests if payload["cmd"] == "pdf_render"]
    assert len(render) == 1 and not Path(render[0]["out"]).exists()


def test_pdf_busy_midway_kills_and_starts_over(tmp_path):
    busy = {"value": False}

    def after_first_page(payload):
        if payload["cmd"] == "pdf_text" and payload["page"] == 0:
            busy["value"] = True

    db, settings, content, engines, vision, _now = pdf_setup(
        tmp_path, pages=[LONG, LONG, LONG], busy=lambda: busy["value"], on_request=after_first_page
    )
    content.busy_check = None  # 只看读取器里的检查
    stats = content.run_round()
    assert stats["ended"] == "busy" and vision.killed == 1
    row = next(iter(contents(db).values()))
    assert (row["state"], row["attempts"]) == ("pending", 0) and chunk_rows(db) == []
    busy["value"] = False
    vision.on_request = None
    content.run_round()
    assert len(chunk_rows(db)) == 3  # 从头读


def test_pdf_timeout_midway_keeps_the_earlier_pages(tmp_path):
    db, settings, content, engines, vision, _now = pdf_setup(
        tmp_path, pages=[LONG, LONG, LONG], fail_at=("pdf_text", 2)
    )
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["note"]) == ("done", "truncated")
    assert [loc for loc, _text in chunk_rows(db)] == ["第 1 页", "第 2 页"]


def test_pdf_timeout_at_the_start_retries_later(tmp_path):
    db, settings, content, engines, vision, now = pdf_setup(
        tmp_path, pages=[LONG], fail_at=("pdf_open", None)
    )
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["reason"]) == ("pending", "timeout")
    now.value += timedelta(hours=2)
    vision.fail_at = None
    content.run_round()
    assert vision.requests[-2][1] == 120.0  # 再试时用 120 秒
    assert next(iter(contents(db).values()))["state"] == "done"


def test_helper_crashing_on_a_file_is_corrupt_not_timeout(tmp_path):
    """认字程序在一份坏文件上崩了（进程退出）：第一次照超时给一次重试，再崩记 corrupt（文件损坏），
    不记「处理超时」。"""

    def crash(payload):
        raise HelperCrashed("vision 退出了（退出码 -11）")

    db, settings, content, engines, vision, now = pdf_setup(
        tmp_path, pages=[LONG], on_request=crash
    )
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["reason"]) == ("pending", "timeout")
    now.value += timedelta(hours=2)
    content.run_round()
    row = next(iter(contents(db).values()))
    assert (row["state"], row["reason"]) == ("unreadable", "corrupt")

    image = tmp_path / "坏图.png"
    image.write_bytes(png(2000, 2000))
    engines.set_engine("vision")
    extractor = ImageExtractor(engines)
    first = extractor(image, "image", {"content_key": "q2:ab", "reason": None, "attempts": 0})
    again = extractor(image, "image", {"content_key": "q2:ab", "reason": "timeout", "attempts": 1})
    assert (first.status, again.status) == ("timeout", "corrupt")


def test_pdf_password_corrupt_and_not_really_a_pdf(tmp_path):
    db, settings, content, engines, vision, _now = pdf_setup(tmp_path, pages="password")
    content.run_round()
    assert next(iter(contents(db).values()))["reason"] == "password"

    db, settings, root, root_id, content, indexer, _now, _state = setup(tmp_path / "b")
    engines, _ = make_engines(db, settings, tmp_path, build=FakeBuild(settings.data_dir))
    content.extractors.update(extractors(engines))
    from .material_fixtures import build_docx

    build_docx(root / "其实是 Word.pdf", ["x"])
    put(root / "其实是 Word.pdf", (root / "其实是 Word.pdf").read_bytes())
    index(indexer)
    content.run_round()
    assert next(iter(contents(db).values()))["layer"] == "text"


# ---------------------------------------------------------------------- 整理


def test_arrange_lines_sorts_rows_removes_overlap_duplicates_and_splits_paragraphs():
    lines = [
        {"t": "第二段", "x": 0.1, "y": 0.50, "h": 0.03},
        {"t": "右边", "x": 0.6, "y": 0.101, "h": 0.03},
        {"t": "左边", "x": 0.1, "y": 0.10, "h": 0.03},
        {"t": "下一行", "x": 0.1, "y": 0.14, "h": 0.03},
        {"t": "下一行", "x": 0.1, "y": 0.145, "h": 0.03},  # 长截图块间重叠处认了两次
        {"t": "第二页", "x": 0.1, "y": 0.1, "h": 0.03, "p": 1},
    ]
    assert arrange_lines(lines) == [(0, "左边右边下一行"), (0, "第二段"), (1, "第二页")]


def test_tidy_text_and_text_layer_rules():
    assert tidy_ocr_text("数 理 协 会 2026 年 会 议") == "数理协会 2026 年会议"
    assert tidy_ocr_text("第一行汉字\n接着写\nEnglish\nline") == "第一行汉字接着写\nEnglish\nline"
    assert needs_ocr("短") and needs_ocr("" * 30 + "正常的字" * 5)
    assert not needs_ocr("这一页的文字层足够长，超过二十个字，没有乱码，可以直接用。")


def test_image_sizes_from_headers(tmp_path):
    def size(data):
        path = tmp_path / "x"
        path.write_bytes(data)
        return image_size(path)

    assert size(png(600, 200)) == (600, 200)
    assert size(b"GIF89a" + struct.pack("<HH", 320, 240) + b"\0" * 10) == (320, 240)
    assert size(b"BM" + b"\0" * 16 + struct.pack("<ii", 800, -600) + b"\0" * 30) == (800, 600)
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xe1"
        + struct.pack(">H", 8)
        + b"Exif\0\0"
        + b"\xff\xc0"
        + struct.pack(">HBHH", 17, 8, 1080, 1920)
        + b"\0" * 20
    )
    assert size(jpeg) == (1920, 1080)
    webp = (
        b"RIFF"
        + b"\0" * 4
        + b"WEBPVP8X"
        + b"\0" * 8
        + (1023).to_bytes(3, "little")
        + (767).to_bytes(3, "little")
    )
    assert size(webp) == (1024, 768)
    tiff = (
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", 2)
        + struct.pack("<HHII", 256, 3, 1, 2000)
        + struct.pack("<HHII", 257, 4, 1, 3000)
        + b"\0" * 8
    )
    assert size(tiff) == (2000, 3000)
    assert size(b"\x00\x00\x00\x18ftypheic") is None
    assert is_small(64, 64) and is_small(1000, 50) and not is_small(600, 200)


# ---------------------------------------------------------------------- 预览图


def test_preview_uses_one_shot_command_and_caches(tmp_path):
    build = FakeBuild(tmp_path / "data")
    log = tmp_path / "thumb.log"
    build.binary.parent.mkdir(parents=True)
    build.binary.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\nprintf JPEG > "$4"\n', encoding="utf-8"
    )
    os.chmod(build.binary, 0o755)
    image = put(tmp_path / "白板.png", png(1600, 1200))
    first = material_previews.preview(tmp_path / "data", "q2:abc", image, kind="image", build=build)
    assert first.read_bytes() == b"JPEG" and first.name == "q2_abc-480.jpg"
    second = material_previews.preview(
        tmp_path / "data", "q2:abc", image, kind="image", build=build
    )
    assert second == first and log.read_text(encoding="utf-8").count("thumb") == 1
    material_previews.preview(
        tmp_path / "data", "q2:def", image, kind="pdf", build=build, size=1600
    )
    assert f"page1 {image} 1600" in log.read_text(encoding="utf-8")
    assert not list((tmp_path / "data" / "material-previews").glob(".*"))


def test_preview_without_vision_uses_sips_for_images_only(tmp_path):
    build = FakeBuild(tmp_path / "data", ready=False)
    sips = fake_bin(
        tmp_path / "bin",
        "sips",
        'while [ $# -gt 0 ]; do if [ "$1" = "--out" ]; then out="$2"; fi; shift; done\n'
        'printf JPEG > "$out"\n',
    )
    image = put(tmp_path / "白板.heic", b"heic")
    assert (
        material_previews.preview(
            tmp_path / "data", "q2:a", image, kind="image", build=build, sips=sips
        ).read_bytes()
        == b"JPEG"
    )
    with pytest.raises(material_previews.PreviewUnavailable):
        material_previews.preview(
            tmp_path / "data", "q2:b", image, kind="pdf", build=build, sips=sips
        )


def test_preview_timeout_and_concurrency(tmp_path, monkeypatch):
    build = FakeBuild(tmp_path / "data")
    build.binary.parent.mkdir(parents=True)
    build.binary.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    os.chmod(build.binary, 0o755)
    image = put(tmp_path / "a.png", png(100, 100))
    started = time.monotonic()
    with pytest.raises(material_previews.PreviewTimeout):
        material_previews.preview(
            tmp_path / "data", "q2:a", image, kind="image", build=build, timeout=1
        )
    assert time.monotonic() - started < 10
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(material_previews, "_slots", slots)
    slots.acquire()
    slots.acquire()
    with pytest.raises(material_previews.PreviewTimeout):
        material_previews.preview(
            tmp_path / "data", "q2:b", image, kind="image", build=build, timeout=0.2
        )


def test_preview_cache_is_pruned_oldest_first(tmp_path):
    folder = material_previews.cache_dir(tmp_path)
    folder.mkdir(parents=True)
    for index_ in range(5):
        path = folder / f"q2_{index_}-480.jpg"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1000 + index_, 1000 + index_))
    assert material_previews.prune_cache(tmp_path, limit=250) == 3
    assert sorted(path.name for path in folder.iterdir()) == ["q2_3-480.jpg", "q2_4-480.jpg"]


# ---------------------------------------------------------------------- doctor 和试跑报告


def test_doctor_reports_material_tools_without_failing(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    archive.mkdir()
    staging.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cli.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    materials = payload["materials"]
    assert set(materials) >= {
        "vision",
        "tesseract",
        "textutil",
        "ffmpeg",
        "funasr",
        "chip",
        "macos",
    }


def test_ocr_engine_command_switches_without_restart(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "archive"
    staging = tmp_path / "staging"
    archive.mkdir()
    staging.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=archive,
        staging_root=staging,
        semantic_enabled=False,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["materials", "ocr-engine", "off"]) == 0
    out = capsys.readouterr().out
    assert "设置：off" in out and "不认字" in out
    # 测试里本机的 swiftc、tesseract 一律当没装（conftest 挡着），不会真去编译 Vision 程序
    assert "tesseract：没装" in out and "要等 Vision 程序编译好" in out
    assert not (tmp_path / "data" / "bin").exists()
    assert cli.main(["materials", "ocr-engine"]) == 0
    assert "设置：off" in capsys.readouterr().out


def test_ocr_trial_report_has_machine_and_pixels(tmp_path):
    report = {
        "generated_at": "2026-09-27T12:00:00+00:00",
        "images": [{"path": "/x/a.png", "bytes": 30000, "pixels": [1170, 2532], "engines": {}}],
        "summary": {},
        "machine": {"macos": "15.1", "chip": "Apple M2", "tesseract": "tesseract 5.4.1"},
    }
    text = ocr_trial.render_markdown(report, [])
    assert "macOS 15.1，Apple M2，tesseract tesseract 5.4.1" in text and "1170×2532" in text
