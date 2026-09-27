"""第三期 3e：覆盖率、materials status、读不了的列表、首页标签、文件状态的说法、预览数据、读盘的三个接口。"""
import json
import os
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from meeting_workbench import cli
from meeting_workbench.db import Database, utc_now
from meeting_workbench.material_content import ExtractResult, MaterialContent, pending_counts, store_result
from meeting_workbench.material_index import MaterialIndexer
from meeting_workbench.material_rules import STATE_TEXTS, WAITING_TEXTS
from meeting_workbench.material_status import (
    coverage,
    file_preview,
    file_row,
    file_state,
    render_status,
    unreadable_files,
)
from meeting_workbench.materials import ROOT_ONLINE, ROOT_VOLUME_OFFLINE

from .test_graph import add_meeting, add_task
from .test_material_content import index, put, setup
from .test_material_index import run_until_done
from .test_tasks_api import make_client


def build_root(root):
    """一个根目录里各种状态都有一份。"""
    put(root / "方案.docx", b"docx-1")
    put(root / "副本" / "方案.docx", b"docx-1")  # 同一份内容放在两个地方
    put(root / "报价.xlsx", b"xlsx")
    put(root / "白板.png", b"png-bytes")
    put(root / "访谈.m4a", b"audio")
    put(root / "扫描.pdf", b"%PDF-1.4 scanned")
    put(root / "汇报.key", b"keynote")
    put(root / "日志.txt", b"log")
    put(root / "包.zip", b"zip")
    put(root / "声档会议记录" / "0926 周会.md", "卡片")
    put(root / "node_modules" / "left-pad" / "index.js", "x")
    (root.parent / "别处").mkdir(exist_ok=True)
    os.symlink(root.parent / "别处", root / "链接")


def key_of(db, rel_path):
    return db.query_one("SELECT content_key FROM material_files WHERE rel_path = ?", (rel_path,))["content_key"]


def file_id(db, rel_path):
    return db.query_one("SELECT id FROM material_files WHERE rel_path = ?", (rel_path,))["id"]


def store(db, rel_path, result):
    with db.transaction() as connection:
        store_result(connection, key_of(db, rel_path), result, attempts=0, now=datetime.now(UTC))


def fill_states(db):
    """把各份内容放进各种状态：读完的（文档、表格、录音）、在等识别程序的、要密码的、一时读不了的。"""
    store(db, "方案.docx", ExtractResult("ok", blocks=[{"loc": None, "text": "第一行\n第二行"}], extractor="stdlib"))
    table = "\n".join(f"项目\t{index}" for index in range(8))
    store(db, "报价.xlsx", ExtractResult("ok", blocks=[{"loc": "表「预算」", "text": table}], extractor="stdlib"))
    store(
        db,
        "访谈.m4a",
        ExtractResult(
            "ok",
            spans=[{"start_ms": 1_000, "end_ms": 31_000, "text": "先讲报价。"},
                   {"start_ms": 31_000, "end_ms": 62_000, "text": "再讲交付。"}],
            duration_ms=62_000,
            extractor="funasr-material",
        ),
    )
    store(db, "白板.png", ExtractResult("waiting", note="engine_missing"))
    store(db, "扫描.pdf", ExtractResult("password", extractor="pdfkit+vision"))
    db.execute("UPDATE material_files SET content_error = 'io', content_attempts = 1 WHERE rel_path = '日志.txt'")


def seeded(tmp_path):
    db, settings, root, root_id, content, indexer, now, state = setup(tmp_path)
    build_root(root)
    index(indexer)
    content.run_round()
    fill_states(db)
    return db, root, root_id, content, state


# ---------------------------------------------------------------------- 覆盖率


def test_coverage_counts_files_not_contents(tmp_path):
    db, root, root_id, _content, _state = seeded(tmp_path)
    with db.autocommit() as connection:
        [item] = coverage(connection, "p")
    assert item["root_id"] == root_id and item["state"] == "done" and item["online"] is True
    assert item["names"] == {"files": 10, "name_only_dirs": 1, "symlinks": 1}
    content = item["content"]
    # 两份一样的 docx 算两个；日志.txt 一时读不了算在还剩里；汇报.key 在文件行上记了格式不支持
    assert content["total"] == 8 and content["done"] == 4 and content["pending"] == 1
    assert content["unreadable"] == {"password": 1, "corrupt": 0, "unsupported": 1, "timeout": 0, "permission": 0}
    assert content["waiting"] == [{"what": "vision", "files": 1, "hint": WAITING_TEXTS["vision"]}]
    assert content["names_only"] == {"cards": 1, "other": 1}
    assert content["paused"] is None


def test_coverage_waiting_names_what_is_missing(tmp_path):
    db, _root, _root_id, _content, _state = seeded(tmp_path)
    store(db, "访谈.m4a", ExtractResult("waiting"))
    engines = SimpleNamespace(
        tools=lambda: SimpleNamespace(ffmpeg=None, ffprobe=None, funasr_python="py", media_script="s"),
        setting=lambda: "auto",
        system="linux",
        build=SimpleNamespace(failure=lambda: None),
    )
    with db.autocommit() as connection:
        [item] = coverage(connection, "p", engines=engines, paused="busy")
    waiting = {entry["what"]: entry for entry in item["content"]["waiting"]}
    assert waiting["ffmpeg"]["hint"] == WAITING_TEXTS["ffmpeg"]
    assert waiting["tesseract"]["files"] == 1  # 不在 Mac 上时图片等的是 tesseract
    assert item["content"]["paused"] == "busy"

    engines.system = "darwin"
    engines.build = SimpleNamespace(failure=lambda: {"error": "swiftc 报错"})
    with db.autocommit() as connection:
        [item] = coverage(connection, "p", engines=engines)
    vision = next(entry for entry in item["content"]["waiting"] if entry["what"] == "vision")
    assert vision["hint"] == STATE_TEXTS["vision_failed"]


def test_iwork_files_do_not_keep_the_home_tag_going(tmp_path):
    db, settings, root, _root_id, content, indexer, _now, _state = setup(tmp_path)
    put(root / "汇报.key", b"keynote")
    put(root / "预算.numbers", b"numbers")
    index(indexer)
    content.run_round()
    assert pending_counts(db)["pending"] == 0


def test_render_status_one_line_per_root(tmp_path):
    db, root, _root_id, _content, _state = seeded(tmp_path)
    with db.autocommit() as connection:
        text = render_status(coverage(connection, "p"), {"p": "云图AI"})
    assert f"［云图AI］{root}" in text
    assert "文件名 10 个 · 已读 4 个 · 还剩 1 个 · 读不了 2 个（要密码 1、格式不支持 1） · 在等：vision 1 个" in text
    assert WAITING_TEXTS["vision"] in text


def test_cli_materials_status_reads_the_database(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MEETING_WORKBENCH_DATA_DIR", str(data_dir))
    monkeypatch.delenv("MEETING_WORKBENCH_DATABASE_PATH", raising=False)
    db, root, _root_id, _content, _state = seeded(tmp_path)  # 库在 tmp_path/workbench.sqlite3
    monkeypatch.setenv("MEETING_WORKBENCH_DATABASE_PATH", str(tmp_path / "workbench.sqlite3"))
    stamp = (tmp_path / "workbench.sqlite3").stat().st_mtime_ns

    assert cli.main(["materials", "status", "--project", "云图ai"]) == 0
    out = capsys.readouterr().out
    assert "已读 4 个" in out and "读不了 2 个" in out
    assert cli.main(["materials", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["roots"][0]["content"]["done"] == 4
    assert (tmp_path / "workbench.sqlite3").stat().st_mtime_ns == stamp  # 只读
    with pytest.raises(SystemExit):
        cli.main(["materials", "status", "--project", "不存在的项目"])


# ---------------------------------------------------------------------- 读不了的列表


def test_unreadable_list_filters_by_root_and_reason_and_pages(tmp_path):
    db, root, root_id, _content, _state = seeded(tmp_path)
    other = tmp_path / "第二个盘"
    put(other / "坏.docx", b"bad")
    db.execute("INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
               (str(other), utc_now()))
    run_until_done(MaterialIndexer(db, SimpleNamespace(archive_root=tmp_path / "a", staging_root=tmp_path / "s",
                                                        data_dir=tmp_path / "data"), clock=lambda: 0.0))
    db.execute("UPDATE material_files SET content_error = 'corrupt' WHERE name = '坏.docx'")
    with db.autocommit() as connection:
        everything = unreadable_files(connection, "p")
        assert everything["total"] == 3 and everything["next_offset"] is None
        assert {item["reason"] for item in everything["items"]} == {"password", "unsupported", "corrupt"}
        only_first = unreadable_files(connection, "p", root_id=root_id)
        assert {item["name"] for item in only_first["items"]} == {"扫描.pdf", "汇报.key"}
        page = unreadable_files(connection, "p", root_id=root_id, limit=1)
        assert page["total"] == 2 and page["next_offset"] == 1 and len(page["items"]) == 1
        rest = unreadable_files(connection, "p", root_id=root_id, offset=1, limit=1)
        assert rest["next_offset"] is None
        assert unreadable_files(connection, "p", reason="password")["items"][0]["path"] == f"{root}/扫描.pdf"


# ---------------------------------------------------------------------- 文件状态的说法


def state_for(db, rel_path, *, online=True, paused=None, engines=None):
    with db.autocommit() as connection:
        row = file_row(connection, file_id(db, rel_path))
        found = connection.execute("SELECT * FROM material_contents WHERE content_key = ?",
                                   (row["content_key"],)).fetchone()
        return file_state(connection, row, dict(found) if found else None, online=online, paused=paused,
                          engines=engines)


def test_every_state_sentence(tmp_path):
    db, _root, _root_id, _content, _state = seeded(tmp_path)
    put(_root / "新.docx", b"new")
    assert state_for(db, "方案.docx") == {
        "kind": "done", "reason": None, "note": None, "what": None, "paused": None, "meeting": None, "text": "",
    }
    assert state_for(db, "方案.docx", online=False)["text"] == "资料盘未连接，先看上次读到的"
    db.execute("UPDATE material_contents SET state = 'pending' WHERE content_key = ?", (key_of(db, "方案.docx"),))
    assert state_for(db, "方案.docx")["text"] == "还没读到内容，读完后这里能预览"
    assert state_for(db, "方案.docx", paused="busy")["text"] == "转写会议时先停，转写完接着读"
    assert state_for(db, "方案.docx", online=False)["text"] == "资料盘未连接，先看上次读到的"
    assert state_for(db, "日志.txt")["text"] == "上次没读出来，过一会儿再试"
    assert state_for(db, "白板.png")["text"] == (
        "要先装 Xcode 命令行工具才能读 PDF、认图片里的字：在终端运行 xcode-select --install"
    )
    assert state_for(db, "扫描.pdf")["text"] == "读不了：要密码。文件名照样能搜到"
    assert state_for(db, "汇报.key")["text"] == "读不了：格式不支持。文件名照样能搜到"
    assert state_for(db, "包.zip") == {**state_for(db, "包.zip"), "kind": "names_only", "text": "这种文件只收文件名"}
    assert state_for(db, "声档会议记录/0926 周会.md")["text"] == "声档写的会议卡片，只收文件名"

    for note, text in (("small_image", "图太小，没认字"), ("no_text", "图里没认出字"), ("no_speech", "没听到说话声")):
        db.execute("UPDATE material_contents SET state = 'done', note = ? WHERE content_key = ?",
                   (note, key_of(db, "白板.png")))
        assert state_for(db, "白板.png")["text"] == text
    for rel, layer_text in (("方案.docx", "只收了前 20 万字"), ("访谈.m4a", "只转了前 6 小时"),
                            ("白板.png", "只收了前面一部分")):
        db.execute("UPDATE material_contents SET state = 'done', note = 'truncated' WHERE content_key = ?",
                   (key_of(db, rel),))
        assert state_for(db, rel)["text"] == layer_text
    db.execute("UPDATE material_contents SET state = 'done', note = 'truncated', pages = 300 WHERE content_key = ?",
               (key_of(db, "扫描.pdf"),))
    assert state_for(db, "扫描.pdf")["text"] == "只读了前 300 页"

    add_meeting(db, "m1", ago=1, title="云图周会")
    db.execute("UPDATE material_contents SET note = 'meeting_audio', meeting_id = 'm1' WHERE content_key = ?",
               (key_of(db, "访谈.m4a"),))
    state = state_for(db, "访谈.m4a")
    assert state["text"] == "这是会议『云图周会』的录音，内容看会议" and state["meeting"] == {"id": "m1", "title": "云图周会"}

    db.execute("UPDATE material_files SET gone_at = ? WHERE rel_path = '包.zip'", (utc_now(),))
    assert state_for(db, "包.zip")["kind"] == "gone"
    assert state_for(db, "包.zip")["text"] == "找不到这个文件了，可能已经删掉或挪走了"


# ---------------------------------------------------------------------- 预览数据


def preview_of(db, rel_path, **kwargs):
    with db.autocommit() as connection:
        return file_preview(connection, file_id(db, rel_path), **kwargs)


def test_preview_kinds(tmp_path):
    db, root, _root_id, _content, _state = seeded(tmp_path)
    text = preview_of(db, "方案.docx")
    assert text["preview"]["kind"] == "text" and text["preview"]["lines"] == ["第一行", "第二行"]
    assert text["file"]["path"] == f"{root}/方案.docx" and text["file"]["folder_path"] == str(root)
    assert text["file"]["root_online"] is True and text["file"]["gone"] is False

    table = preview_of(db, "报价.xlsx")["preview"]
    assert table["kind"] == "table" and table["sheet"] == "表「预算」"
    assert table["rows"] == [["项目", str(index)] for index in range(5)] and table["more"] is True

    image = preview_of(db, "白板.png")
    assert image["preview"]["kind"] == "image"
    assert image["preview"]["image_url"] == f"/api/materials/files/{image['file']['id']}/thumb"

    pdf = preview_of(db, "扫描.pdf")
    assert pdf["preview"]["kind"] == "pdf" and pdf["preview"]["page_url"].endswith("/page1")

    media = preview_of(db, "访谈.m4a")["preview"]
    assert media["kind"] == "media" and media["playable"] is True and media["media_url"].endswith("/media")
    assert media["duration_ms"] == 62_000
    assert media["transcript"] == [
        {"start_ms": 1_000, "end_ms": 31_000, "text": "先讲报价。"},
        {"start_ms": 31_000, "end_ms": 62_000, "text": "再讲交付。"},
    ]

    assert preview_of(db, "包.zip")["preview"]["kind"] == "none"
    db.execute("UPDATE material_files SET gone_at = ? WHERE rel_path = '方案.docx'", (utc_now(),))
    gone = preview_of(db, "方案.docx")
    assert gone["file"]["gone"] is True and gone["preview"]["kind"] == "none" and gone["state"]["kind"] == "gone"

    offline = preview_of(db, "白板.png", state_of=lambda path: ROOT_VOLUME_OFFLINE)
    assert offline["file"]["root_online"] is False and offline["preview"]["image_url"] is None


def test_preview_mentions_deliverables_and_parts(tmp_path):
    db, root, _root_id, _content, _state = seeded(tmp_path)
    add_meeting(db, "m1", ago=2, project_id="p", title="报价会", segments=[(90_000, "说到报价单")])
    db.execute(
        """INSERT INTO artifacts(meeting_id, kind, source_root, path, sha256, created_at)
           VALUES ('m1', 'audio', 'archive', '/archive/m1.m4a', 'x', ?)""",
        (utc_now(),),
    )
    audio_id = db.query_one("SELECT id FROM artifacts WHERE meeting_id = 'm1'")["id"]
    target = file_id(db, "报价.xlsx")
    db.execute(
        """INSERT INTO meeting_file_mentions(meeting_id, project_id, stem_key, needle, file_id, count, first_ms,
                                             anchors_json, minutes_count, source, status, picked, updated_at)
           VALUES ('m1', 'p', '报价', '报价', ?, 2, 90000, '[90000]', 0, 'transcript', 'active', 0, ?)""",
        (target, utc_now()),
    )
    add_task(db, "t1", meeting_id="m1", project_id="p")
    db.execute("INSERT INTO deliverables(task_id, kind, url, title, created_at) VALUES ('t1', 'file', ?, '', ?)",
               (f"{root}/报价.xlsx", utc_now()))
    result = preview_of(db, "报价.xlsx", quotes=lambda meeting_id, starts: {90_000: "说到报价单"}, can_reveal=True)
    [mention] = result["mentions"]
    assert mention["title"] == "报价会" and mention["count"] == 2 and mention["quote"] == "说到报价单"
    assert mention["audio_url"] == f"/api/media/{audio_id}"
    assert [item["task_id"] for item in result["deliverables"]] == ["t1"]
    assert result["can_reveal"] is True
    only = preview_of(db, "报价.xlsx", parts="preview")
    assert set(only) == {"file", "state", "preview"}


# ---------------------------------------------------------------------- 接口


@pytest.fixture
def api(tmp_path):
    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    root = tmp_path / "云图AI"
    root.mkdir()
    build_root(root)
    put(root / "录屏.mkv", b"video")
    db.execute("INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, ?)",
               (str(root), utc_now()))
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    MaterialContent(db, settings, state_of=lambda path: ROOT_ONLINE).run_round()
    fill_states(db)
    return SimpleNamespace(client=client, settings=settings, db=db, root=root)


def test_coverage_and_unreadable_endpoints(api):
    coverage_payload = api.client.get("/api/materials/coverage", params={"project_id": "p"}).json()
    [item] = coverage_payload["roots"]
    assert item["content"]["done"] == 4 and item["content"]["unreadable"]["password"] == 1
    listing = api.client.get("/api/materials/unreadable", params={"project_id": "p", "reason": "password"}).json()
    assert [entry["name"] for entry in listing["items"]] == ["扫描.pdf"] and listing["next_offset"] is None
    assert api.client.get("/api/materials/unreadable", params={"project_id": "p", "reason": "x"}).status_code == 422


def test_preview_endpoint(api):
    target = file_id(api.db, "方案.docx")
    payload = api.client.get(f"/api/materials/files/{target}/preview").json()
    assert payload["preview"]["lines"] == ["第一行", "第二行"] and payload["can_reveal"] is False
    assert "mentions" in payload and "deliverables" in payload
    assert set(api.client.get(f"/api/materials/files/{target}/preview?parts=preview").json()) == {
        "file", "state", "preview",
    }
    assert api.client.get("/api/materials/files/999999/preview").status_code == 404


def fake_vision(api):
    """假的 Vision 程序：thumb、page1 都写一个小 JPEG。"""
    binary = api.client.app.state.ocr.build.binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text('#!/bin/sh\nprintf "\\377\\330\\377fake" > "$4"\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)


def test_thumb_page1_and_media_check_paths(api, monkeypatch):
    fake_vision(api)

    def no_resident(*args, **kwargs):
        raise AssertionError("预览图不走后台认字的常驻程序")

    monkeypatch.setattr(api.client.app.state.ocr, "vision", no_resident)
    image = file_id(api.db, "白板.png")
    response = api.client.get(f"/api/materials/files/{image}/thumb")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8")
    pdf = file_id(api.db, "扫描.pdf")
    assert api.client.get(f"/api/materials/files/{pdf}/page1").status_code == 200
    assert api.client.get(f"/api/materials/files/{pdf}/thumb").status_code == 415
    assert api.client.get(f"/api/materials/files/{image}/page1").status_code == 415

    audio = file_id(api.db, "访谈.m4a")
    media = api.client.get(f"/api/materials/files/{audio}/media")
    assert media.status_code == 200 and media.content == b"audio" and media.headers["content-type"] == "audio/mp4"
    ranged = api.client.get(f"/api/materials/files/{audio}/media", headers={"Range": "bytes=1-2"})
    assert ranged.status_code == 206 and ranged.content == b"ud"
    assert api.client.get(f"/api/materials/files/{file_id(api.db, '录屏.mkv')}/media").status_code == 415

    # 改过的 file_id、不见了的行、换成指到根目录外的符号链接
    assert api.client.get("/api/materials/files/999999/media").status_code == 404
    api.db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), image))
    assert api.client.get(f"/api/materials/files/{image}/thumb").status_code == 404
    outside = api.root.parent / "外面.m4a"
    outside.write_bytes(b"secret")
    (api.root / "访谈.m4a").unlink()
    os.symlink(outside, api.root / "访谈.m4a")
    assert api.client.get(f"/api/materials/files/{audio}/media").status_code == 403

    # 盘不在
    api.root.rename(api.root.parent / "拔掉了")
    response = api.client.get(f"/api/materials/files/{pdf}/page1")
    assert response.status_code == 503 and response.json()["detail"] == "资料盘未连接"


def test_thumb_times_out_quickly_when_the_generator_hangs(api, monkeypatch):
    binary = api.client.app.state.ocr.build.binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    from meeting_workbench import material_previews

    image = file_id(api.db, "白板.png")
    original = material_previews.preview

    def quick(*args, **kwargs):
        kwargs["timeout"] = 0.5
        return original(*args, **kwargs)

    monkeypatch.setattr(material_previews, "preview", quick)
    response = api.client.get(f"/api/materials/files/{image}/thumb")
    assert response.status_code == 503 and response.json()["detail"] == "预览图生成超时"


def test_health_has_the_material_count_without_touching_services(api):
    before = api.client.get("/api/health").json()
    assert before["counts"]["material_pending"] == 0
    assert before["details"]["materials"] == {"pending": 0, "paused": None, "offline_pending": 0}
    app = api.client.app
    app.state.material_content.progress = {"pending": 210, "offline_pending": 0, "paused": "busy"}
    after = api.client.get("/api/health").json()
    assert after["counts"]["material_pending"] == 210
    assert after["details"]["materials"] == {"pending": 210, "paused": "busy", "offline_pending": 0}
    assert after["status"] == before["status"] and after["services"] == before["services"]
    assert "materials" not in after["services"]


def test_bootstrap_can_reveal_only_on_this_machine(api):
    assert api.client.get("/api/bootstrap").json()["can_reveal"] is False
    local = TestClient(api.client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert local.get("/api/bootstrap").json()["can_reveal"] is True
