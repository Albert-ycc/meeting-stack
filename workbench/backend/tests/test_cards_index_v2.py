"""00 索引.md（第四期 4h）：先修四个毛病，再是 v2 的几节。

四个毛病：全部撤下后下一轮又写回来；暂停的项目索引还在更新；根目录最后一张卡片走了以后索引冻住；
合并或移走根目录后留下孤儿。
"""

import json
import re
import time
import warnings
from pathlib import Path

import pytest

from meeting_workbench import card_index, cards
from meeting_workbench.cards import APP_FILE_NAMES, INDEX_PATHS_KEY, CardWriter
from meeting_workbench.db import utc_now
from meeting_workbench.project_names import merge_project

from .helpers import count_reads
from .test_cards import (
    CARDS,
    MEETING,
    _card_files,
    _env,
    _meeting,
    _mount,
    _needs_review,
    _project,
    _task,
)
from .test_file_mentions import add_file
from .test_graph_local import add_decision
from .test_project_linking import make_project
from .test_relation_read import literal
from .test_relations import keyed

INDEX = "00 索引.md"
WEEKLY = "vm-20260927-090000"


def _index(root):
    return root / CARDS / INDEX


def _retired_indexes(settings):
    folder = settings.data_dir / "card-retired"
    return (
        sorted(p.parent.name.rsplit("-", 1)[-1] + "/" + p.name for p in folder.rglob(INDEX))
        if folder.is_dir()
        else []
    )


def _known(db):
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (INDEX_PATHS_KEY,))
    return json.loads(row["value"]) if row else None


def _edit(path, old="讨论了", new="我改了"):
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _unmount(db, root):
    db.execute("DELETE FROM project_material_roots WHERE path=?", (str(root),))


# ---------------------------------------------------------------------- 四个毛病


def test_retire_all_takes_every_index_and_reconcile_writes_none(tmp_path):
    """毛病 1：全部撤下以后，留在原处的改过的卡片不再让索引下一轮又长出来。"""
    db, settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    _meeting(db, project_b, meeting_id=WEEKLY, title="周会", when="2026-09-27T09:00:00")
    writer.reconcile()
    assert _index(root_a).is_file() and _index(root_b).is_file()
    assert _known(db) == {str(_index(root_a)): project_a, str(_index(root_b)): project_b}
    _edit(root_a / CARDS / "260926 初审规则沟通.md")

    result = writer.retire_all()

    assert result["retired"] == 1  # 索引不计数
    assert _card_files(root_a) == ["260926 初审规则沟通.md"]
    assert not _index(root_a).exists() and not _index(root_b).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md", "index/00 索引.md"]
    assert _known(db) == {}
    _task(db, project_a, "in_progress", "关掉以后又加的任务")
    writer.reconcile()
    writer.reconcile()
    assert not _index(root_a).exists() and not _index(root_b).exists()

    writer.enable()
    writer.reconcile()
    assert "关掉以后又加的任务" in _index(root_a).read_text(encoding="utf-8")
    assert _index(root_b).is_file()


def test_indexes_written_before_the_upgrade_are_claimed(tmp_path):
    """升级前写的索引只记在内存里：第一次用到时认领，全部撤下照样收走。"""
    db, settings, _writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    CardWriter(db, settings).reconcile()
    db.execute("DELETE FROM app_state WHERE key=?", (INDEX_PATHS_KEY,))
    _edit(root / CARDS / "260926 初审规则沟通.md")

    CardWriter(db, settings).retire_all()

    assert not _index(root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    assert _known(db) == {}


def test_paused_project_index_is_retired_and_rewritten_on_resume(tmp_path):
    """毛病 2：暂停撤下索引（不计进 retired），暂停期间不更新，恢复时写回来。"""
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    _edit(root / CARDS / "260926 初审规则沟通.md")

    result = writer.pause_project(project_id)

    assert result["retired"] == 0  # 改过的卡片留在原处，索引不算数
    assert [item["meeting_id"] for item in result["kept"]] == [MEETING]
    assert not _index(root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    _task(db, project_id, "in_progress", "暂停时加的任务")
    writer.reconcile()
    writer.reconcile_project(project_id)
    assert not _index(root).exists()

    writer.resume_project(project_id)

    assert "暂停时加的任务" in _index(root).read_text(encoding="utf-8")
    assert _known(db) == {str(_index(root)): project_id}


def test_the_index_stays_live_after_the_last_card_leaves(tmp_path):
    """毛病 3：根目录最后一张卡片改走以后，后来加的任务照样写进它的索引。"""
    db, _settings, writer, disk = _env(tmp_path)
    project_a, root_a = _project(db, disk, "云图AI")
    project_b, root_b = _project(db, disk, "数据中台")
    _meeting(db, project_a)
    writer.reconcile()
    db.execute(
        "UPDATE meetings SET project_id=?, project_origin='manual' WHERE id=?", (project_b, MEETING)
    )
    writer.sync_meeting(MEETING)
    assert _card_files(root_a) == []

    _task(db, project_a, "in_progress", "后来加的任务", meeting_id=None)
    writer.reconcile()

    text = _index(root_a).read_text(encoding="utf-8")
    assert "- 后来加的任务 · 我 · 进行中" in text
    assert "这个项目还没有会议卡片。" in text
    assert "初审规则沟通" in _index(root_b).read_text(encoding="utf-8")


def test_a_shared_folder_gets_one_index_for_its_owner(tmp_path):
    """同一个文件夹挂在两个项目下：只有最早挂上的项目写索引；项目的第二个根目录不写。"""
    db, _settings, writer, disk = _env(tmp_path)
    first, root = _project(db, disk, "云图AI")
    second = make_project(db, "云图看板")
    _mount(db, second, root)
    second_root = disk / "看板"
    (second_root / CARDS).mkdir(parents=True)
    _mount(db, first, second_root)
    _meeting(db, first)
    _meeting(db, second, meeting_id=WEEKLY, title="看板评审", when="2026-09-27T09:00:00")

    writer.reconcile()

    assert _index(root).read_text(encoding="utf-8").startswith("---\nproject: 云图AI\n")
    assert not _index(second_root).exists()
    assert _known(db) == {str(_index(root)): first}


def test_moving_the_root_retires_the_old_index(tmp_path):
    """毛病 4：移走根目录以后，旧文件夹里的索引移进 card-retired/。"""
    db, settings, writer, disk = _env(tmp_path)
    project_id, old_root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    _edit(old_root / CARDS / "260926 初审规则沟通.md")  # 改过的卡片留在原处
    new_root = disk / "云图AI-新"
    new_root.mkdir()
    _unmount(db, old_root)
    _mount(db, project_id, new_root)

    writer.reconcile()

    assert not _index(old_root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    assert _known(db) == {str(_index(new_root)): project_id}


def test_merging_projects_retires_the_index_of_the_folder_left_behind(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    target, target_root = _project(db, disk, "数据中台")
    source, source_root = _project(db, disk, "云图AI")
    _meeting(db, source)
    _meeting(db, target, meeting_id=WEEKLY, title="周会", when="2026-09-27T09:00:00")
    writer.reconcile()
    assert _index(source_root).is_file()

    with db.transaction() as connection:
        merge_project(connection, source, target)
    writer.reconcile()

    assert not _index(source_root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    text = _index(target_root).read_text(encoding="utf-8")
    assert text.startswith("---\nproject: 数据中台\n") and 'project: ""' not in text
    assert _known(db) == {str(_index(target_root)): target}


@pytest.mark.parametrize("state", ["root_offline", "root_missing", "root_in_archive"])
def test_an_offline_root_keeps_its_index_until_it_comes_back(tmp_path, monkeypatch, state):
    """不在线、找不到（挂载掉线）、不让写的根目录：记录留着，回来后收走旧索引。"""
    db, settings, writer, disk = _env(tmp_path)
    project_id, old_root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    new_root = disk / "云图AI-新"
    new_root.mkdir()
    _unmount(db, old_root)
    _mount(db, project_id, new_root)
    original = writer._root_state
    monkeypatch.setattr(
        writer,
        "_root_state",
        lambda root, round_: state if root == str(old_root) else original(root, round_),
    )

    writer.reconcile()

    assert _index(old_root).is_file()
    assert str(_index(old_root)) in _known(db)
    assert _retired_indexes(settings) == []

    monkeypatch.undo()
    writer.reconcile()

    assert not _index(old_root).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]
    assert _known(db) == {str(_index(new_root)): project_id}


def test_hand_edits_are_overwritten_unless_the_marker_line_is_gone(tmp_path):
    db, settings, writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    index = _index(root)

    index.write_text(index.read_text(encoding="utf-8") + "\n我手改的一行\n", encoding="utf-8")
    _task(db, project_id, "in_progress", "第一条任务")
    writer.reconcile()
    assert "我手改的一行" not in index.read_text(encoding="utf-8")

    mine = index.read_text(encoding="utf-8").replace("generated_by: shengdang\n", "") + "我的索引\n"
    index.write_text(mine, encoding="utf-8")
    _task(db, project_id, "in_progress", "第二条任务")
    writer.reconcile()
    assert index.read_text(encoding="utf-8") == mine
    assert _known(db) == {}

    # 归你的索引：移走根目录、全部撤下都不碰
    other = disk / "别处"
    other.mkdir()
    _unmount(db, root)
    _mount(db, project_id, other)
    writer.reconcile()
    writer.retire_all()
    writer.reconcile()
    assert index.read_text(encoding="utf-8") == mine
    # 收走的只有新文件夹里声档写的那一份
    assert not _index(other).exists()
    assert _retired_indexes(settings) == ["index/00 索引.md"]


# ---------------------------------------------------------------------- v2 的样本

WEEK_A = "vm-20260920-100000"
REVIEW = "vm-20260925-110000"
ELSEWHERE = "vm-20260924-090000"
HISTORY = "vm-20260923-090000"
SENTINEL_CHUNK = "蓝鲸七号材料原文"


def _minutes(title, summary):
    return f"# {title}\n\n## 一分钟摘要\n\n{summary}\n\n## 完整会议记录\n\n讨论了。\n"


def _relation(db, kind, project_id, ident, *, status="shown", origin="rule", **cols):
    now = utc_now()
    names = ["kind", "project_id", "ident", "status", "origin", "created_at", "updated_at", *cols]
    values = [kind, project_id, ident, status, origin, now, now, *cols.values()]
    db.execute(
        f"INSERT INTO relations({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        values,
    )


def _requirement(
    db,
    requirement_id,
    project_id,
    title,
    priority,
    *,
    status="active",
    at="2026-09-01T00:00:00+00:00",
):
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (requirement_id, project_id, title, priority, status, at, at),
    )


def _link(db, requirement_id, meeting_id):
    db.execute(
        "INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, ?, ?)",
        (requirement_id, meeting_id, utc_now()),
    )


def _task_row(
    db, task_id, project_id, title, status, *, requirement_id=None, meeting_id=None, at=None
):
    now = at or utc_now()
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, assignee, meeting_id, project_id, requirement_id,
               status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, 'ai', 'me', ?, ?, ?, ?, ?, ?)""",
        (task_id, title, status, meeting_id, project_id, requirement_id, now, now, now),
    )


def _deliver(db, task_id, root_id, rel_path, key):
    deliverable_id = db.execute(
        "INSERT INTO deliverables(task_id, kind, url, title, created_at) VALUES (?, 'file', 'x', 'x', ?)",
        (task_id, utc_now()),
    )
    db.execute(
        "INSERT INTO deliverable_files(deliverable_id, content_key, root_id, rel_path) VALUES (?, ?, ?, ?)",
        (deliverable_id, key, root_id, rel_path),
    )


def _root_id(db, path):
    return db.query_one("SELECT id FROM project_material_roots WHERE path=?", (str(path),))["id"]


def _v2_world(tmp_path, *, reverse=False):
    """一个项目两个根目录。四场会写了卡片（9/20 周会、9/26 初审规则沟通、9/27 周会），另有待你选、
    补写答了「先不要」、卡片在别的根目录的三场会，它们的东西都不该出现。"""
    db, settings, writer, disk = _env(tmp_path)
    pid, root = _project(db, disk, "云图AI")
    old_root = disk / "云图-旧"
    old_root.mkdir()
    _mount(db, pid, old_root)
    order = (lambda items: list(reversed(items))) if reverse else list
    meetings = [
        (
            WEEK_A,
            "周会",
            "2026-09-20T10:00:00",
            "manual",
            _minutes("周会", "上线定在 9 月，周报按周五发。"),
        ),
        (
            MEETING,
            "初审规则沟通",
            "2026-09-26T14:30:00",
            "ai",
            _minutes(
                "初审规则沟通",
                "确定初审阈值先按 0.8 执行，上线改到 10 月，张三整理阈值对照表，"
                "另外讨论了驻场排班、快递配送的衔接和下个月的评审节奏，会后各自把材料补齐，"
                "下周三之前再对一次口径和上线清单，顺带确认评审人。",
            ),
        ),
        (WEEKLY, "周会", "2026-09-27T09:00:00", "manual", _minutes("周会", "周会改到周二。")),
        (REVIEW, "待选的会", "2026-09-25T11:00:00", "ai", _minutes("待选的会", "待选会议的摘要。")),
        (
            ELSEWHERE,
            "别处的会",
            "2026-09-24T09:00:00",
            "manual",
            _minutes("别处的会", "别处会议的摘要。"),
        ),
        (HISTORY, "老会", "2026-09-23T09:00:00", "manual", _minutes("老会", "老会议的摘要。")),
    ]
    for meeting_id, title, when, origin, markdown in order(meetings):
        _meeting(
            db, pid, meeting_id=meeting_id, title=title, when=when, origin=origin, markdown=markdown
        )
    _needs_review(db, REVIEW)
    db.execute(
        "UPDATE minutes_versions SET created_at='2000-01-01T00:00:00+00:00' WHERE meeting_id=?",
        (HISTORY,),
    )
    decisions = [
        ("d-a1", WEEK_A, "上线定在 9 月", 60_000),
        ("d-a2", WEEK_A, "周报按周五发", 310_000),
        ("d-b1", MEETING, "阈值先按 0.8 执行", 754_000),
        ("d-b2", MEETING, "上线改到 10 月", 1_862_000),
        ("d-c1", WEEKLY, "周报按周五发", 45_000),
        ("d-c2", WEEKLY, "下周起周会改到周二", 131_000),
        ("d-r1", REVIEW, "待选项目的决议", 1_000),
        ("d-e1", ELSEWHERE, "别处的决议", 1_000),
        ("d-h1", HISTORY, "没补写的决议", 1_000),
    ]
    # 同一场会里的序号跟着纪要走，不跟插入顺序：按会分组插
    by_meeting: dict[str, list] = {}
    for item in decisions:
        by_meeting.setdefault(item[1], []).append(item)
    for meeting_id in order(list(by_meeting)):
        for decision_id, _meeting_id, text, start_ms in by_meeting[meeting_id]:
            add_decision(db, decision_id, meeting_id, text, start_ms)
    db.execute("UPDATE decisions SET placement='none' WHERE id='d-c2'")
    _relation(
        db,
        "later_changed",
        pid,
        "d-a1|d-b2",
        origin="llm",
        meeting_id=MEETING,
        decision_id="d-a1",
        to_decision_id="d-b2",
    )
    _relation(
        db,
        "restated",
        pid,
        "d-a2|d-c1",
        meeting_id=WEEKLY,
        decision_id="d-a2",
        to_decision_id="d-c1",
    )
    _requirement(db, "r-1", pid, "初审规则 V2", "P1", at="2026-09-01T00:00:00+00:00")
    _requirement(db, "r-0", pid, "只有标题的需求", "P0", at="2026-09-05T00:00:00+00:00")
    _requirement(
        db, "r-2", pid, "北辰仓快递配送", "P2", status="done", at="2026-09-02T00:00:00+00:00"
    )
    folder = root / "需求" / "初审规则"
    folder.mkdir(parents=True)
    db.execute(
        "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES ('r-1', ?, ?)",
        (str(folder), utc_now()),
    )
    for meeting_id in order([WEEK_A, MEETING, ELSEWHERE]):
        _link(db, "r-1", meeting_id)
    _task_row(
        db,
        "t-1",
        pid,
        "整理初审阈值对照表",
        "in_progress",
        requirement_id="r-1",
        meeting_id=MEETING,
        at="2026-09-26T07:00:00+00:00",
    )
    _task_row(
        db,
        "t-2",
        pid,
        "写一版方案",
        "confirmed",
        requirement_id="r-1",
        meeting_id=MEETING,
        at="2026-09-26T07:01:00+00:00",
    )
    _task_row(
        db,
        "t-3",
        pid,
        "还没确认的任务",
        "pending_confirm",
        meeting_id=MEETING,
        at="2026-09-26T07:02:00+00:00",
    )
    root_id, old_id = _root_id(db, root), _root_id(db, old_root)
    files = [
        (root_id, "需求/初审规则/方案v1.docx", "k-plan"),
        (root_id, "报价单_v3.xlsx", "k-quote"),
        (old_root and old_id, "排期表.xlsx", "k-plan-sheet"),
        (root_id, "闲置文件甲.docx", "k-idle"),
        (root_id, "只在待选会上的文件.docx", "k-review"),
        (root_id, "说明.docx", "k-generic"),
    ]
    ids = {}
    for file_root, rel, key in order(files):
        ids[rel] = add_file(db, file_root, rel)
        keyed(db, ids[rel], key)
    # 同一内容在旧根目录里还有一份副本：算到本项目根目录里 id 最小的那份
    copy_id = add_file(db, old_id, "副本/报价单_v3.xlsx")
    keyed(db, copy_id, "k-quote")
    _deliver(db, "t-2", root_id, "需求/初审规则/方案v1.docx", "k-plan")
    literal(db, WEEK_A, "报价单", ids["报价单_v3.xlsx"], project_id=pid)
    _relation(
        db,
        "mention",
        pid,
        f"{MEETING}|报价单",
        origin="llm",
        meeting_id=MEETING,
        at_ms=60_000,
        stem_key="报价单",
        file_id=copy_id,
        content_key="k-quote",
        evidence_json=json.dumps({"phrase": "上周那版报价", "count": 1}),
    )
    literal(db, MEETING, "排期表", ids["排期表.xlsx"], project_id=pid)
    literal(db, WEEKLY, "排期表", ids["排期表.xlsx"], project_id=pid)
    literal(db, REVIEW, "只在待选会上的文件", ids["只在待选会上的文件.docx"], project_id=pid)
    literal(db, MEETING, "只在待选会上的文件", ids["只在待选会上的文件.docx"], project_id=pid)
    literal(db, MEETING, "说明", ids["说明.docx"], project_id=pid)
    literal(db, WEEKLY, "说明", ids["说明.docx"], project_id=pid)
    # 不该写进去的：相关、在问的产出和影响、没记入的词、材料原文
    _relation(
        db,
        "related",
        pid,
        f"{MEETING}|k-idle",
        origin="vector",
        meeting_id=MEETING,
        file_id=ids["闲置文件甲.docx"],
        content_key="k-idle",
        quote="相关的原话",
    )
    _relation(
        db,
        "produced",
        pid,
        "t-1|k-idle",
        status="suggested",
        task_id="t-1",
        file_id=ids["闲置文件甲.docx"],
        content_key="k-idle",
    )
    _relation(
        db,
        "affects",
        pid,
        "d-b1|k-idle",
        status="suggested",
        decision_id="d-b1",
        meeting_id=MEETING,
        file_id=ids["闲置文件甲.docx"],
        content_key="k-idle",
        quote="影响的原话",
    )
    now = utc_now()
    db.execute(
        """INSERT INTO glossary_candidates(project_id, term, term_key, files, spoken, created_at, updated_at)
           VALUES (?, '蓝鲸七号原料', '蓝鲸七号原料', 3, 0, ?, ?)""",
        (pid, now, now),
    )
    db.execute(
        """INSERT INTO material_contents(content_key, layer, state, created_at, updated_at)
           VALUES ('k-plan', 'text', 'done', ?, ?)""",
        (now, now),
    )
    db.execute(
        "INSERT INTO material_chunks(content_key, ordinal, text) VALUES ('k-plan', 0, ?)",
        (f"方案正文里有{SENTINEL_CHUNK}。",),
    )
    writer.reconcile()
    # 这场会的卡片记在别的根目录（比如老数据）：它不算这个根目录的会
    db.execute(
        "UPDATE meeting_cards SET root_path=? WHERE meeting_id=?", (str(old_root), ELSEWHERE)
    )
    return db, settings, writer, pid, root, old_root


EXPECTED_V2 = """---
project: 云图AI
generated_by: shengdang
---

> 本文件由声档自动维护，请勿手改。把这个项目文件夹交给 Claude Code 时，先让它读这一份。
> 这里只有会上说过的话、任务和文件位置，不摘材料的内容；文件内容请直接打开文件看。

# 云图AI · 会议记录索引

## 进行中的行动项

- 整理初审阈值对照表 · 我 · 进行中 · 来自 [初审规则沟通](<260926 初审规则沟通.md>)
- 写一版方案 · 我 · 已确认 · 来自 [初审规则沟通](<260926 初审规则沟通.md>)

## 需求

### 只有标题的需求
- 优先级：P0

### 初审规则 V2
- 优先级：P1
- 文件夹：`需求/初审规则`
- 会议：[周会](<260920 周会.md>)、[初审规则沟通](<260926 初审规则沟通.md>)（另有 1 场会的纪要不在这个文件夹）
- 定了什么：
  - 2026-09-26 阈值先按 0.8 执行 · [初审规则沟通](<260926 初审规则沟通.md>) 00:12:34
  - 2026-09-26 上线改到 10 月 · [初审规则沟通](<260926 初审规则沟通.md>) 00:31:02 · 这次改了 2026-09-20 定的『上线定在 9 月』
  - 2026-09-20 周报按周五发 · [周会](<260920 周会.md>) 00:05:10 · 2026-09-27 后来又提到
- 行动项：整理初审阈值对照表（进行中）、写一版方案（已确认）
- 产出：`需求/初审规则/方案v1.docx`（「写一版方案」的产出）

### 已完成或搁置
- 北辰仓快递配送（已完成）

## 其他决议（不属于哪个需求）

- 2026-09-27 下周起周会改到周二 · [周会](<260927 周会.md>) 00:02:11

## 关键文件

- `需求/初审规则/方案v1.docx` · 「写一版方案」的产出
- `报价单_v3.xlsx` · 在 2 场会上被提到
- `{old}/排期表.xlsx` · 在 2 场会上被提到

## 会议（按时间倒序）

- 2026-09-27 09:00 · [周会](<260927 周会.md>)
  - 摘要：周会改到周二。
- 2026-09-26 14:30 · [初审规则沟通](<260926 初审规则沟通.md>) · AI 自动归属
  - 摘要：确定初审阈值先按 0.8 执行，上线改到 10 月，张三整理阈值对照表，另外讨论了驻场排班、快递配送的衔接和下个月的评审节奏，会后各自把材料补齐，下周三之前再对一次口径和上线清单…
- 2026-09-20 10:00 · [周会](<260920 周会.md>)
  - 摘要：上线定在 9 月，周报按周五发。
"""


def _render(writer, pid, root):
    return writer.render_index(pid, str(root))


def test_v2_sections_line_by_line(tmp_path):
    db, _settings, writer, pid, root, old_root = _v2_world(tmp_path)

    text = _render(writer, pid, root)

    assert text.splitlines() == EXPECTED_V2.replace("{old}", str(old_root)).splitlines()
    # 待你选、补写答了「先不要」、卡片在别的根目录的会，一样都不出现
    for absent in ("待选", "别处", "老会", "没补写"):
        assert absent not in text
    # 盘上的那份就是这个（第一次 reconcile 之后又改了一张卡片的位置，签名变了会重渲）
    writer.reconcile()
    assert _index(root).read_text(encoding="utf-8") == text


def test_v2_leaves_out_what_is_not_settled(tmp_path):
    db, _settings, writer, pid, root, _old_root = _v2_world(tmp_path)

    text = _render(writer, pid, root)

    for absent in (
        "闲置文件甲",  # 既没被提到也不是交付物；它身上的相关、在问的产出和影响都不写
        "相关的原话",
        "影响的原话",
        "可能过时",
        "还没确认的任务",
        "蓝鲸七号原料",  # 没记入的词
        SENTINEL_CHUNK,  # 材料原文
        "说明.docx",  # 太泛的名字
        "只在待选会上的文件",  # 这个根目录的会里只被提到 1 场
        "副本/",  # 同一内容取本项目根目录里 id 最小的那份
        "又说了一次",
        "%",
        "分数",
    ):
        assert absent not in text
    assert "后来又提到" in text


def test_v2_is_deterministic(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    db, _settings, writer, pid, root, old_root = _v2_world(tmp_path / "a")
    first = _render(writer, pid, root)
    assert _render(writer, pid, root) == first
    db2, _settings2, writer2, pid2, root2, old_root2 = _v2_world(tmp_path / "b", reverse=True)
    shuffled = _render(writer2, pid2, root2)
    assert shuffled.replace(str(old_root2), "OLD") == first.replace(str(old_root), "OLD")
    # 新代码不用当前时间（_local_start 的 datetime.now() 兜底不在这条路上）
    source = Path(card_index.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\.now\(|\.today\(|time\.time\(|utc_now", source)


def test_v2_statement_budget(tmp_path):
    db, _settings, _writer, pid, root, _old_root = _v2_world(tmp_path)
    assert (
        count_reads(db, lambda connection: card_index.load_index_data(connection, pid, str(root)))
        <= 9
    )


def test_render_budget(tmp_path):
    """100 场会、300 条决议：一次渲染 150 毫秒以内（只提醒，测试机快慢不定；语句数才是硬的）。"""
    db, settings, writer, disk = _env(tmp_path)
    pid, root = _project(db, disk, "云图AI")
    for index in range(100):
        meeting_id = f"vm-202609{index % 28 + 1:02d}-{index:06d}"
        _meeting(
            db,
            pid,
            meeting_id=meeting_id,
            title=f"周会{index}",
            when=f"2026-09-{index % 28 + 1:02d}T{index % 10 + 8:02d}:00:00",
            origin="manual",
            markdown=_minutes(f"周会{index}", f"第 {index} 场会的摘要。"),
        )
        for number in range(3):
            add_decision(
                db,
                f"d-{index}-{number}",
                meeting_id,
                f"第 {index} 场的第 {number} 条决议",
                number * 1000,
            )
    writer.reconcile()
    assert (
        count_reads(db, lambda connection: card_index.load_index_data(connection, pid, str(root)))
        <= 9
    )
    started = time.perf_counter()
    text = _render(writer, pid, root)
    elapsed = time.perf_counter() - started
    assert text.count("  - 摘要：") == 10
    if elapsed > 0.15:
        warnings.warn(f"00 索引.md 渲染用了 {elapsed * 1000:.0f} 毫秒", stacklevel=1)


# ---------------------------------------------------------------------- 什么时候重渲


def _counting(monkeypatch, writer):
    calls = []
    original = writer.render_index

    def render(project_id, root):
        calls.append((project_id, root))
        return original(project_id, root)

    monkeypatch.setattr(writer, "render_index", render)
    return calls


def test_signature_gates_rendering(tmp_path, monkeypatch):
    db, settings, _writer, disk = _env(tmp_path)
    now = [1000.0]
    writer = CardWriter(db, settings, clock=lambda: now[0])
    project_id, root = _project(db, disk, "云图AI")
    _meeting(db, project_id)
    writer.reconcile()
    calls = _counting(monkeypatch, writer)
    writes = []
    original_write = writer._atomic_write
    monkeypatch.setattr(
        writer, "_atomic_write", lambda *args: (writes.append(args[2]), original_write(*args))[1]
    )

    writer.reconcile()
    assert calls == []  # 签名没变：不渲染

    db.execute("UPDATE app_state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'graph_rev'")
    writer.reconcile()
    assert len(calls) == 1 and writes == []  # 渲染了，内容一样，不写盘

    now[0] += 601
    writer.reconcile()
    assert len(calls) == 2 and writes == []  # 10 分钟强制渲染一次，照旧先比内容

    _task(db, project_id, "in_progress", "新任务")
    writer.reconcile()
    assert len(calls) == 3 and writes[-1:] == [INDEX]
    assert "新任务" in _index(root).read_text(encoding="utf-8")


def test_the_signature_parts(tmp_path):
    db, _settings, _writer, disk = _env(tmp_path)
    project_id, root = _project(db, disk, "云图AI")
    with db.autocommit() as connection:
        before = card_index.index_signature(connection, project_id, str(root))
    assert before.startswith(f"{card_index.INDEX_VERSION}:")
    db.execute(
        """INSERT INTO material_index_state(root_id, stems_rev) VALUES (?, 3)""",
        (_root_id(db, root),),
    )
    with db.autocommit() as connection:
        after = card_index.index_signature(connection, project_id, str(root))
    assert after != before and after.split(":")[2] == "3"


def test_app_file_names():
    assert APP_FILE_NAMES == {"00 索引.md"}
    assert cards.APP_FILE_NAMES is APP_FILE_NAMES
