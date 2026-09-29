"""第四期 4h：需求页［复制给 Claude Code］的背景（GET /api/requirements/{id}/context）。
Markdown 的样子和绝对路径、cards_missing、paths 包含旧按钮的全部、材料原文不出现、404、语句数、不写盘。"""

from __future__ import annotations

import time
import warnings

from meeting_workbench import card_index

from .helpers import count_reads
from .test_cards_index_v2 import HISTORY, SENTINEL_CHUNK, WEEK_A, _link, _v2_world
from .test_cards import _env, _meeting, _project
from .test_graph_local import add_decision
from .test_tasks_api import make_client

EXPECTED = """# 初审规则 V2（云图AI · 需求 · P1 · 进行中）

> 声档生成的背景。纪要是完整的，先读纪要；原话在「逐字稿」里，时间戳是录音时间。这里不摘材料的内容，文件请直接打开看。

## 文件夹
- {root}/需求/初审规则

## 会议
- 2026-09-26 14:30 初审规则沟通：{root}/声档会议记录/260926 初审规则沟通.md
- 2026-09-24 09:00 别处的会：{old}/声档会议记录/260924 别处的会.md
- 2026-09-23 09:00 老会：/Volumes/资料盘/会议纪要与录音/260923 老会（归档文件夹，纪要不在项目文件夹里）
- 2026-09-20 10:00 周会：{root}/声档会议记录/260920 周会.md

## 定了什么
- 2026-09-26 阈值先按 0.8 执行（初审规则沟通 00:12:34）
- 2026-09-26 上线改到 10 月（初审规则沟通 00:31:02；这次改了 2026-09-20 定的『上线定在 9 月』）
- 2026-09-24 别处的决议（别处的会 00:00:01）
- 2026-09-23 没补写的决议（老会 00:00:01）
- 2026-09-20 周报按周五发（周会 00:05:10；2026-09-27 后来又提到）

## 行动项
- 整理初审阈值对照表 · 我 · 进行中
- 写一版方案 · 我 · 已确认

## 产出
- {root}/需求/初审规则/方案v1.docx（「写一版方案」的产出）
"""


def world(tmp_path):
    db, settings, writer, pid, root, old_root = _v2_world(tmp_path)
    # 补写答了「先不要」的会也关联上：卡片不在项目文件夹里，写归档文件夹
    _link(db, "r-1", HISTORY)
    db.execute(
        "UPDATE meetings SET canonical_dir = ? WHERE id = ?",
        ("/Volumes/资料盘/会议纪要与录音/260923 老会", HISTORY),
    )
    db.execute(
        "UPDATE meetings SET canonical_dir = ? WHERE id = ?",
        ("/Volumes/资料盘/会议纪要与录音/260920 周会", WEEK_A),
    )
    return db, settings, writer, root, old_root


def context(db, requirement_id="r-1"):
    with db.autocommit() as connection:
        return card_index.requirement_context(connection, requirement_id)


def legacy_paths(db, requirement_id="r-1"):
    """旧按钮（RequirementDetailPage 的 materialPaths）复制的：各场会的 canonical_dir 加需求文件夹。"""
    meetings = db.query_all(
        """SELECT m.canonical_dir FROM requirement_meetings rm JOIN meetings m ON m.id = rm.meeting_id
            WHERE rm.requirement_id = ?""",
        (requirement_id,),
    )
    folders = db.query_all(
        "SELECT path FROM requirement_folders WHERE requirement_id = ?", (requirement_id,)
    )
    return [row["canonical_dir"] for row in meetings if row["canonical_dir"]] + [
        row["path"] for row in folders
    ]


def test_markdown_uses_absolute_paths_and_writes_every_decision(tmp_path):
    db, _settings, _writer, root, old_root = world(tmp_path)

    result = context(db)

    assert result["markdown"] == EXPECTED.format(root=root, old=old_root)
    assert result["cards_missing"] == 1
    assert SENTINEL_CHUNK not in result["markdown"]
    for absent in (
        "相关的原话",
        "影响的原话",
        "蓝鲸七号原料",
        "还没确认的任务",
        "又说了一次",
        "%",
        "分数",
    ):
        assert absent not in result["markdown"]


def test_paths_include_the_old_material_list_plus_cards_and_deliverables(tmp_path):
    db, _settings, _writer, root, old_root = world(tmp_path)

    paths = context(db)["paths"]

    assert set(legacy_paths(db)) <= set(paths)
    assert f"{root}/声档会议记录/260926 初审规则沟通.md" in paths
    assert f"{old_root}/声档会议记录/260924 别处的会.md" in paths
    assert f"{root}/需求/初审规则/方案v1.docx" in paths
    assert len(paths) == len(set(paths))


def test_empty_sections_are_left_out(tmp_path):
    db, _settings, _writer, _root, _old_root = world(tmp_path)
    result = context(db, "r-0")
    assert result == {
        "markdown": "# 只有标题的需求（云图AI · 需求 · P0 · 进行中）\n\n"
        + card_index.CONTEXT_QUOTE
        + "\n",
        "paths": [],
        "cards_missing": 0,
    }
    done = context(db, "r-2")
    assert done["markdown"].startswith("# 北辰仓快递配送（云图AI · 需求 · P2 · 已完成）")


def test_statement_budget_and_no_disk_writes(tmp_path):
    db, _settings, _writer, root, _old_root = world(tmp_path)
    before = sorted((path, path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file())
    changes = db.query_one("SELECT value FROM app_state WHERE key = 'graph_rev'")
    assert (
        count_reads(db, lambda connection: card_index.requirement_context(connection, "r-1")) <= 8
    )
    assert (
        sorted((path, path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file())
        == before
    )
    assert db.query_one("SELECT value FROM app_state WHERE key = 'graph_rev'") == changes


def test_fifty_meetings_timing(tmp_path):
    """50 场会 30 毫秒以内（只提醒，测试机快慢不定；语句数才是硬的）。"""
    db, _settings, writer, disk = _env(tmp_path)
    pid, _root = _project(db, disk, "云图AI")
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES ('r-big', ?, '大需求', 'P1', 'active', 'x', 'x')""",
        (pid,),
    )
    for index in range(50):
        meeting_id = f"vm-202609{index % 28 + 1:02d}-{index:06d}"
        _meeting(
            db,
            pid,
            meeting_id=meeting_id,
            title=f"周会{index}",
            when=f"2026-09-{index % 28 + 1:02d}T09:00:00",
        )
        _link(db, "r-big", meeting_id)
        add_decision(db, f"d-{index}", meeting_id, f"第 {index} 条决议", 1000)
    writer.reconcile()
    assert (
        count_reads(db, lambda connection: card_index.requirement_context(connection, "r-big")) <= 8
    )
    started = time.perf_counter()
    result = context(db, "r-big")
    elapsed = time.perf_counter() - started
    assert result["markdown"].count("\n- 2026-09-") == 100 and result["cards_missing"] == 0
    if elapsed > 0.03:
        warnings.warn(f"需求背景用了 {elapsed * 1000:.0f} 毫秒", stacklevel=1)


def test_endpoint_and_404(tmp_path):
    client, settings = make_client(tmp_path)
    from meeting_workbench.db import Database

    db = Database(settings.database_path)
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES ('r', 'p', '初审规则 V2', 'P1', 'shelved', 'x', 'x')"""
    )
    payload = client.get("/api/requirements/r/context").json()
    assert set(payload) == {"markdown", "paths", "cards_missing"}
    assert payload["markdown"].startswith("# 初审规则 V2（云图AI · 需求 · P1 · 搁置）")
    missing = client.get("/api/requirements/nope/context")
    assert missing.status_code == 404 and missing.json()["detail"] == "需求不存在"
