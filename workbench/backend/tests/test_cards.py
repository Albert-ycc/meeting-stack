"""会议卡片（第一期 1c）：卡片规范、指纹、安全写盘、跟着会议和你在 Finder 里的动作变。"""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from meeting_workbench.cards import (
    CardWriter,
    content_fp,
    split_card,
)
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.service import MeetingService

from .test_project_linking import make_project, seed_meeting

MEETING = "vm-20260926-143000"
CARDS = "声档会议记录"
MINUTES = "# 初审规则沟通\n\n## 一分钟摘要\n\n阈值先按 0.8 执行 [00:12:34]。\n\n## 完整会议记录\n\n讨论了初审规则。\n"


def _env(tmp_path, *, archive_inside=False):
    disk = tmp_path / "disk"
    disk.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=(disk / "归档") if archive_inside else (tmp_path / "archive"),
        staging_root=tmp_path / "staging",
        semantic_enabled=False,
        material_browse_root=disk,
    )
    db = Database(settings.database_path)
    db.initialize()
    return db, settings, CardWriter(db, settings), disk


def _project(db, disk, name, *, folder=None):
    project_id = make_project(db, name)
    root = disk / (folder or name)
    root.mkdir(parents=True, exist_ok=True)
    _mount(db, project_id, root)
    return project_id, root


def _mount(db, project_id, path):
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, str(path), utc_now()),
    )


def _meeting(
    db,
    project_id,
    *,
    meeting_id=MEETING,
    title="初审规则沟通",
    markdown=MINUTES,
    when="2026-09-26T14:30:00",
    origin="ai",
):
    seed_meeting(
        db,
        meeting_id,
        title,
        markdown,
        segments=[(0, "大家好，开始吧"), (754_000, "阈值先按 0.8 执行"), (1_862_000, "下周上线")],
        project_id=project_id,
        origin=origin if project_id else None,
    )
    db.execute(
        "UPDATE meetings SET recording_date=?, duration_ms=2880000 WHERE id=?", (when, meeting_id)
    )
    return meeting_id


def _task(db, project_id, status, title, *, meeting_id=MEETING, anchor_ms=None, assignee="me"):
    now = utc_now()
    db.execute(
        """INSERT INTO tasks
           (id, title, status, origin, assignee, meeting_id, project_id, anchor_ms,
            status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?)""",
        (f"t-{uuid.uuid4().hex[:8]}", title, status, assignee, meeting_id, project_id, anchor_ms,
         now, now, now),
    )


def _card_row(db, meeting_id=MEETING):
    return db.query_one("SELECT * FROM meeting_cards WHERE meeting_id=?", (meeting_id,))


def _card_files(root):
    folder = root / CARDS
    return sorted(p.name for p in folder.glob("*.md") if p.name != "00 索引.md") if folder.is_dir() else []


def _retired(settings):
    folder = settings.data_dir / "card-retired"
    return sorted(p.name for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def _touch(db, meeting_id=MEETING):
    """模拟会议上又有了变化（这里改一下任务），让卡片变脏。"""
    db.execute("UPDATE meeting_cards SET dirty = dirty + 1 WHERE meeting_id=?", (meeting_id,))


# ---------------------------------------------------------------------- 规范


def test_card_is_written_to_the_spec(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    MeetingService(db).rename_speaker(MEETING, "SPEAKER_00", "张三")
    requirement_folder = root / "需求" / "初审规则"
    requirement_folder.mkdir(parents=True)
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, created_at, updated_at)
           VALUES ('r-1', ?, '初审规则 V2', 'P1', ?, ?)""",
        (project_id, utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES ('r-1', ?, ?)",
        (str(requirement_folder), utc_now()),
    )
    db.execute(
        "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES ('r-1', ?, ?)",
        (MEETING, utc_now()),
    )
    _task(db, project_id, "in_progress", "整理初审阈值对照表", anchor_ms=754_000)
    _task(db, project_id, "pending_confirm", "与李四确认 V2 上线时间", anchor_ms=1_862_000)
    _task(db, project_id, "done", "发会议通知")
    _task(db, project_id, "cancelled", "不做的事")

    stats = writer.reconcile()

    assert stats["written"] == 1
    card = root / CARDS / "260926 初审规则沟通.md"
    text = card.read_text(encoding="utf-8")
    assert text.startswith("---\nmeeting_id: vm-20260926-143000\ntitle: 初审规则沟通\ndate: 2026-09-26\n")
    assert 'start: "14:30"' in text
    assert "duration_min: 48" in text
    assert "project: 云图AI\nproject_source: ai\nparticipants: [张三]" in text
    assert "requirements:\n  - title: 初审规则 V2\n    folder: 需求/初审规则" in text
    assert "transcript: 逐字稿/260926 初审规则沟通.txt" in text
    assert "generated_by: shengdang" in text
    assert "> 项目归属：AI 自动判断（未人工确认）。" in text
    assert "\n# 初审规则沟通\n\n## 一分钟摘要\n\n阈值先按 0.8 执行 [00:12:34]。" in text
    url = re.search(r"^workbench_url: \"?([^\"\n]+)\"?$", text, re.M).group(1)
    assert url.endswith(f"/#meetings/{MEETING}")
    # 行动项的时间点链回声档，从那一刻开始播放
    assert (
        "## 行动项（声档实时状态）\n### 已确认 / 进行中\n"
        f"- 整理初审阈值对照表 · 我 · 进行中 · [00:12:34]({url}@754)\n"
        f"### 待你确认（AI 提取）\n- 与李四确认 V2 上线时间 · 我 · 待确认 · [00:31:02]({url}@1862)\n"
        "### 已完成\n- 发会议通知 · 我 · 已完成"
    ) in text
    assert "不做的事" not in text
    assert text.endswith("## 我的笔记（这一行以下不会被自动覆盖）\n")
    transcript = (root / CARDS / "逐字稿" / "260926 初审规则沟通.txt").read_text(encoding="utf-8")
    assert transcript.splitlines() == [
        "[00:00:00] 张三：大家好，开始吧",
        "[00:12:34] 张三：阈值先按 0.8 执行",
        "[00:31:02] 张三：下周上线",
    ]
    index = (root / CARDS / "00 索引.md").read_text(encoding="utf-8")
    assert "- 整理初审阈值对照表 · 我 · 进行中 · 来自 [初审规则沟通](<260926 初审规则沟通.md>)" in index
    assert "- 2026-09-26 14:30 · [初审规则沟通](<260926 初审规则沟通.md>) · AI 自动归属" in index
    # 只建了「声档会议记录/逐字稿」两层，没有留下临时文件，你自己的文件一个没动
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == [
        "声档会议记录",
        "声档会议记录/00 索引.md",
        "声档会议记录/260926 初审规则沟通.md",
        "声档会议记录/逐字稿",
        "声档会议记录/逐字稿/260926 初审规则沟通.txt",
        "需求",
        "需求/初审规则",
    ]
    row = _card_row(db)
    assert (row["state"], row["dirty"], row["project_id"]) == ("synced", 0, project_id)
    # 内容没变就不写
    assert writer.reconcile()["written"] == 0


def test_confirming_the_project_updates_the_card_header(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()

    db.execute("UPDATE meetings SET project_origin='manual' WHERE id=?", (MEETING,))
    assert _card_row(db)["dirty"] > 0
    writer.reconcile()

    text = (root / CARDS / "260926 初审规则沟通.md").read_text(encoding="utf-8")
    assert "project_source: manual" in text
    assert "> 项目归属：人工确认。" in text


def test_same_day_same_title_gets_the_start_time(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id, meeting_id="vm-20260919-100000", title="周会", when="2026-09-19T10:00:00")
    _meeting(db, project_id, meeting_id="vm-20260919-143000", title="周会", when="2026-09-19T14:30:00")

    writer.reconcile()

    assert _card_files(root) == ["260919 周会 1430.md", "260919 周会.md"]


def test_untitled_card_is_renamed_with_its_transcript_once_titled(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id, title="260926 未命名录音")
    writer.reconcile()
    assert _card_files(root) == ["260926 1430 会议.md"]
    card = root / CARDS / "260926 1430 会议.md"
    card.write_text(card.read_text(encoding="utf-8") + "我的补充\n", encoding="utf-8")

    db.execute("UPDATE meetings SET title='初审规则沟通' WHERE id=?", (MEETING,))
    writer.reconcile()

    assert _card_files(root) == ["260926 初审规则沟通.md"]
    text = (root / CARDS / "260926 初审规则沟通.md").read_text(encoding="utf-8")
    assert text.endswith("## 我的笔记（这一行以下不会被自动覆盖）\n我的补充\n")
    assert sorted(p.name for p in (root / CARDS / "逐字稿").iterdir()) == ["260926 初审规则沟通.txt"]
    assert _card_row(db)["rel_path"] == f"{CARDS}/260926 初审规则沟通.md"


# ---------------------------------------------------------------------- 跟着编辑变


def test_split_merge_and_speaker_rename_refresh_the_transcript_copy(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    transcript = root / CARDS / "逐字稿" / "260926 初审规则沟通.txt"
    service = MeetingService(db)

    def segment_ids():
        version = db.query_one("SELECT current_transcript_version_id AS v FROM meetings WHERE id=?", (MEETING,))["v"]
        return [row["id"] for row in db.query_all(
            "SELECT id FROM segments WHERE version_id=? ORDER BY ordinal", (version,))]

    before = transcript.read_text(encoding="utf-8")
    service.split_segment(MEETING, segment_ids()[0], 4)
    assert _card_row(db)["dirty"] > 0
    writer.reconcile()
    after_split = transcript.read_text(encoding="utf-8")
    assert after_split != before and len(after_split.splitlines()) == 4

    first, second = segment_ids()[:2]
    service.merge_segments(MEETING, first, second)
    assert _card_row(db)["dirty"] > 0
    writer.reconcile()
    after_merge = transcript.read_text(encoding="utf-8")
    assert len(after_merge.splitlines()) == 3

    service.rename_speaker(MEETING, "SPEAKER_00", "月总")
    assert _card_row(db)["dirty"] > 0
    writer.reconcile()
    assert transcript.read_text(encoding="utf-8").startswith("[00:00:00] 月总：")
    assert "participants: [月总]" in (root / CARDS / "260926 初审规则沟通.md").read_text(encoding="utf-8")


def test_editor_reformatting_is_not_treated_as_an_edit(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    MeetingService(db).rename_speaker(MEETING, "SPEAKER_00", "张三")
    _task(db, project_id, "confirmed", "整理对照表")
    writer.reconcile()
    card = root / CARDS / "260926 初审规则沟通.md"
    text = card.read_text(encoding="utf-8")

    # 换列表符号、YAML 改成块列表、属性换顺序、只在笔记区加一行
    reformatted = (
        text.replace("participants: [张三]", "participants:\n  - 张三")
        .replace("- 整理对照表", "* 整理对照表")
        .replace("date: 2026-09-26\n", "")
        .replace("generated_by: shengdang\n", "generated_by: shengdang\ndate: 2026-09-26\n")
        + "会后记得同步李四\n"
    )
    card.write_text(reformatted, encoding="utf-8")
    _task(db, project_id, "pending_confirm", "新提出的任务")
    writer.reconcile()

    assert _card_row(db)["state"] == "synced"
    updated = card.read_text(encoding="utf-8")
    assert "新提出的任务" in updated
    assert updated.endswith("## 我的笔记（这一行以下不会被自动覆盖）\n会后记得同步李四\n")

    # 改自动区一个字：变成你改过的，文件不再被写
    edited = updated.replace("讨论了初审规则", "讨论了初审规矩")
    card.write_text(edited, encoding="utf-8")
    _task(db, project_id, "pending_confirm", "又一个任务")
    writer.reconcile()
    assert _card_row(db)["state"] == "user_edited"
    assert card.read_text(encoding="utf-8") == edited
    with db.autocommit() as connection:
        status = writer.meeting_card(connection, MEETING)
    assert (status["state"], status["category"]) == ("user_edited", "stopped")

    # ［用最新纪要重写］：你的版本先进回收区，笔记带过去
    result = writer.rewrite(MEETING, "rewrite")
    assert result["state"] == "synced"
    rewritten = card.read_text(encoding="utf-8")
    assert "讨论了初审规则" in rewritten and "又一个任务" in rewritten
    assert rewritten.endswith("会后记得同步李四\n")
    assert _retired(_settings) == ["260926 初审规则沟通.md"]


def test_content_fp_ignores_whitespace_punctuation_and_key_order():
    base = "---\nmeeting_id: m\ntitle: 周会\nparticipants: [张三, 李四]\n---\n\n# 周会\n\n- 甲\n- 乙\n"
    same = "---\ntitle: 周会\nparticipants:\n- 张三\n- 李四\nmeeting_id: m\n---\n# 周会\n* 甲\n\n* 乙"
    assert content_fp(base) == content_fp(same)
    assert content_fp(base) != content_fp(base.replace("甲", "丙"))
    auto, notes = split_card("正文\n## 我的笔记（这一行以下不会被自动覆盖）\n想法\n")
    assert (auto, notes) == ("正文\n## 我的笔记（这一行以下不会被自动覆盖）", "想法\n")


# ---------------------------------------------------------------------- 写盘底线


def test_offline_disk_never_creates_directories(tmp_path):
    db, settings, writer, _disk = _env(tmp_path)
    settings.material_browse_root = Path("/Volumes")
    project_id = make_project(db, "云图AI")
    offline_root = Path("/Volumes/声档测试盘-不存在") / "项目" / "云图AI"
    _mount(db, project_id, offline_root)
    _meeting(db, project_id)

    writer.reconcile()
    writer.reconcile()

    assert not Path("/Volumes/声档测试盘-不存在").exists()
    row = _card_row(db)
    assert (row["state"], row["reason"]) == ("blocked", "root_offline")
    assert row["dirty"] > 0  # 插上盘后自动补写，所以一直留着重试
    with db.autocommit() as connection:
        status = writer.meeting_card(connection, MEETING)
    assert (status["reason"], status["category"]) == ("root_offline", "waiting")


def test_roots_inside_the_archive_are_refused(tmp_path):
    db, settings, writer, disk = _env(tmp_path, archive_inside=True)
    project_id = make_project(db, "云图AI")
    inside = settings.archive_root / "260926 初审规则沟通"
    inside.mkdir(parents=True)
    _mount(db, project_id, inside)
    _meeting(db, project_id)

    writer.reconcile()

    assert not (inside / CARDS).exists()
    row = _card_row(db)
    assert (row["state"], row["reason"]) == ("blocked", "root_in_archive")


def test_deleted_card_is_not_resurrected_but_follows_a_project_change(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    (root_a / CARDS / "260926 初审规则沟通.md").unlink()

    for _ in range(2):
        _touch(db)
        writer.reconcile()

    assert _card_files(root_a) == []
    assert _card_row(db)["state"] == "missing"
    with db.autocommit() as connection:
        assert writer.meeting_card(connection, MEETING)["state"] == "missing"
    assert writer.rewrite(MEETING, "regenerate")["state"] == "synced"
    (root_a / CARDS / "260926 初审规则沟通.md").unlink()
    _touch(db)
    writer.reconcile()

    db.execute("UPDATE meetings SET project_id=?, project_origin='manual' WHERE id=?", (project_b, MEETING))
    writer.reconcile()
    assert _card_files(root_b) == ["260926 初审规则沟通.md"]


def test_renamed_card_is_claimed_and_kept_up_to_date(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    (root / CARDS / "260926 初审规则沟通.md").rename(root / CARDS / "初审-我的版本.md")

    _task(db, project_id, "confirmed", "会后补的任务")
    writer.reconcile()

    assert _card_files(root) == ["初审-我的版本.md"]
    assert "会后补的任务" in (root / CARDS / "初审-我的版本.md").read_text(encoding="utf-8")
    assert _card_row(db)["rel_path"] == f"{CARDS}/初审-我的版本.md"
    # 你起的名字以后不再被改回去
    db.execute("UPDATE meetings SET title='初审规则沟通（定稿）' WHERE id=?", (MEETING,))
    writer.reconcile()
    assert _card_files(root) == ["初审-我的版本.md"]


def test_renamed_root_is_claimed_without_a_second_copy(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    renamed = disk / "云图AI（二期）"
    root.rename(renamed)
    card = renamed / CARDS / "260926 初审规则沟通.md"
    mtime = card.stat().st_mtime_ns

    # 找不到文件夹时不动，等你重新选
    _touch(db)
    writer.reconcile()
    assert _card_row(db)["reason"] == "root_missing"
    db.execute(
        "UPDATE project_material_roots SET path=? WHERE project_id=?", (str(renamed), project_id)
    )
    writer.reconcile()

    assert _card_files(renamed) == ["260926 初审规则沟通.md"]
    assert card.stat().st_mtime_ns == mtime
    row = _card_row(db)
    assert (row["state"], row["root_path"]) == ("synced", str(renamed))


def test_deleting_the_whole_folder_pauses_the_project_until_resumed(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    for path in sorted((root / CARDS).rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (root / CARDS).rmdir()

    _touch(db)
    _meeting(db, project_id, meeting_id="vm-20260927-090000", title="周会", when="2026-09-27T09:00:00")
    writer.reconcile()
    writer.reconcile()

    assert not (root / CARDS).exists()
    with db.autocommit() as connection:
        summary = writer.project_cards(connection, project_id)
    assert (summary["paused"], summary["waiting_reason"]) == (True, "paused")

    writer.resume_project(project_id)
    writer.reconcile()
    assert _card_files(root) == ["260926 初审规则沟通.md", "260927 周会.md"]


# ---------------------------------------------------------------------- 换项目


def test_moving_to_another_project_carries_notes_and_recycles_the_old_card(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    card_a = root_a / CARDS / "260926 初审规则沟通.md"
    card_a.write_text(card_a.read_text(encoding="utf-8") + "我的想法\n", encoding="utf-8")

    db.execute("UPDATE meetings SET project_id=?, project_origin='manual' WHERE id=?", (project_b, MEETING))
    result = writer.sync_meeting(MEETING)

    assert result["action"] == "moved"
    assert result["from"] == f"云图AI/{CARDS}/260926 初审规则沟通.md"
    assert result["to"] == f"数据中台/{CARDS}/260926 初审规则沟通.md"
    assert _card_files(root_a) == []
    moved = (root_b / CARDS / "260926 初审规则沟通.md").read_text(encoding="utf-8")
    assert "project: 数据中台" in moved and moved.endswith("我的想法\n")
    assert (root_b / CARDS / "逐字稿" / "260926 初审规则沟通.txt").is_file()
    assert _retired(settings) == ["260926 初审规则沟通.md", "260926 初审规则沟通.txt"]
    assert "初审规则沟通" not in (root_a / CARDS / "00 索引.md").read_text(encoding="utf-8")
    assert "初审规则沟通" in (root_b / CARDS / "00 索引.md").read_text(encoding="utf-8")


def test_an_edited_card_moves_to_the_new_project_as_it_is(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    card_a = root_a / CARDS / "260926 初审规则沟通.md"
    edited = card_a.read_text(encoding="utf-8").replace("讨论了初审规则", "我重写了这一段")
    card_a.write_text(edited, encoding="utf-8")

    db.execute("UPDATE meetings SET project_id=? WHERE id=?", (project_b, MEETING))
    writer.sync_meeting(MEETING)

    assert _card_files(root_a) == []
    assert (root_b / CARDS / "260926 初审规则沟通.md").read_text(encoding="utf-8") == edited
    assert _card_row(db)["state"] == "user_edited"


def test_unassigning_recycles_the_card_and_brings_notes_back_later(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    card = root / CARDS / "260926 初审规则沟通.md"
    card.write_text(card.read_text(encoding="utf-8") + "留着的笔记\n", encoding="utf-8")

    db.execute("UPDATE meetings SET project_id=NULL, project_origin='manual' WHERE id=?", (MEETING,))
    result = writer.sync_meeting(MEETING)
    assert result["action"] == "retired"
    assert _card_files(root) == []
    with db.autocommit() as connection:
        status = writer.meeting_card(connection, MEETING)
    assert (status["reason"], status["category"]) == ("waiting_project", "waiting")

    db.execute("UPDATE meetings SET project_id=? WHERE id=?", (project_id, MEETING))
    writer.sync_meeting(MEETING)
    assert card.read_text(encoding="utf-8").endswith("留着的笔记\n")


def test_concurrent_reassign_and_reconcile_leave_one_card(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    other = CardWriter(db, settings)
    db.execute("UPDATE meetings SET project_id=?, project_origin='manual' WHERE id=?", (project_b, MEETING))

    errors = []

    def run(action):
        try:
            action()
        except Exception as error:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            errors.append(error)

    threads = [
        threading.Thread(target=run, args=(lambda: writer.sync_meeting(MEETING),)),
        threading.Thread(target=run, args=(other.reconcile,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _card_files(root_a) == []
    assert _card_files(root_b) == ["260926 初审规则沟通.md"]
    assert _retired(settings).count("260926 初审规则沟通.md") <= 1


def test_deleted_meeting_card_goes_to_the_recycle_area(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()

    db.execute("DELETE FROM meetings WHERE id=?", (MEETING,))
    writer.reconcile()

    assert _card_files(root) == []
    assert "260926 初审规则沟通.md" in _retired(settings)
    assert _card_row(db) is None


# ---------------------------------------------------------------------- 历史会议、开关


def _make_historical(db):
    db.execute(
        "UPDATE app_state SET value=? WHERE key='cards_since'", ("2999-01-01T00:00:00+00:00",)
    )


def test_historical_meetings_wait_for_the_backfill_answer(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    no_folder = make_project(db, "没挂文件夹")
    _meeting(db, project_id)
    _meeting(db, no_folder, meeting_id="vm-20260925-100000", title="旧会", when="2026-09-25T10:00:00",
             origin="manual")
    _make_historical(db)

    writer.reconcile()
    assert _card_files(root) == []
    with db.autocommit() as connection:
        assert writer.meeting_card(connection, MEETING)["reason"] == "not_backfilled"
        banner = writer.backfill_banner(connection)
    assert banner["meetings"] == 1
    assert banner["projects"] == 1
    assert banner["ai_attributed"] == 1
    assert banner["no_folder_projects"] == 1
    assert banner["top"][0]["path"] == str(root / CARDS)

    writer.answer_backfill("later")
    with db.autocommit() as connection:
        assert writer.backfill_banner(connection) is None
    writer.answer_backfill("yes")
    writer.reconcile()
    assert _card_files(root) == ["260926 初审规则沟通.md"]


def test_backfilling_two_hundred_meetings_takes_at_most_five_rounds(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    for index in range(200):
        _meeting(
            db,
            project_id,
            meeting_id=f"vm-202609{index % 28 + 1:02d}-{index:06d}",
            title=f"周会 {index}",
            when=f"2026-09-{index % 28 + 1:02d}T10:{index % 60:02d}:00",
        )
    _make_historical(db)
    writer.answer_backfill("yes")

    rounds = 0
    while len(_card_files(root)) < 200:
        rounds += 1
        started = time.monotonic()
        writer.reconcile()
        assert time.monotonic() - started < 10
        assert rounds <= 5
    assert rounds <= 5


def test_retire_all_recycles_untouched_cards_and_lists_edited_ones(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    _meeting(db, project_id, meeting_id="vm-20260927-090000", title="周会", when="2026-09-27T09:00:00")
    writer.reconcile()
    edited = root / CARDS / "260927 周会.md"
    edited.write_text(edited.read_text(encoding="utf-8").replace("讨论了", "我改了"), encoding="utf-8")

    result = writer.retire_all()

    assert result["retired"] == 1
    assert [item["path"] for item in result["kept"]] == [str(edited)]
    assert _card_files(root) == ["260927 周会.md"]
    assert not (root / CARDS / "00 索引.md").exists()
    writer.reconcile()
    assert _card_files(root) == ["260927 周会.md"]
    with db.autocommit() as connection:
        assert writer.meeting_card(connection, MEETING)["reason"] == "disabled"

    writer.enable()
    writer.reconcile()
    assert _card_files(root) == ["260926 初审规则沟通.md", "260927 周会.md"]


def test_first_card_notice_and_dont_write(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    with db.autocommit() as connection:
        notices = writer.notices(connection)
    assert [(n["project_id"], n["path"]) for n in notices] == [(project_id, str(root / CARDS))]

    result = writer.pause_project(project_id)

    assert result["retired"] == 1
    assert _card_files(root) == []
    with db.autocommit() as connection:
        assert writer.notices(connection) == []
        assert writer.meeting_card(connection, MEETING)["reason"] == "paused"
    writer.reconcile()
    assert _card_files(root) == []
    writer.resume_project(project_id)
    writer.reconcile()
    assert _card_files(root) == ["260926 初审规则沟通.md"]


def test_no_root_waits_and_mounting_one_writes_the_backlog(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    project_id = make_project(db, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    assert (_card_row(db)["state"], _card_row(db)["reason"]) == ("blocked", "no_root")

    root = disk / "云图AI"
    root.mkdir()
    _mount(db, project_id, root)
    assert _card_row(db)["dirty"] > 0
    writer.reconcile()

    assert _card_files(root) == ["260926 初审规则沟通.md"]
    assert json.loads(_card_row(db)["written_fps"])
    assert os.listdir(root / CARDS)


def test_a_folder_shared_by_two_projects_only_gets_the_first_projects_cards(tmp_path):
    db, _settings, writer, disk = _env(tmp_path)
    first, root = _project(db, disk, "云图AI")
    second = make_project(db, "云图看板")
    _mount(db, second, root)
    _meeting(db, first)
    _meeting(db, second, meeting_id="vm-20260927-090000", title="看板评审", when="2026-09-27T09:00:00")

    writer.reconcile()

    assert _card_files(root) == ["260926 初审规则沟通.md"]
    row = _card_row(db, "vm-20260927-090000")
    assert (row["state"], row["reason"]) == ("blocked", "root_shared")
    with db.autocommit() as connection:
        assert writer.project_cards(connection, second)["waiting_reason"] == "root_shared"
        assert writer.meeting_card(connection, "vm-20260927-090000")["category"] == "stopped"
