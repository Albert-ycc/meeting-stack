"""第二期 2d：文件名变词干、后台文件名索引。"""

import os
from types import SimpleNamespace

import pytest

from meeting_workbench.db import Database, utc_now
from meeting_workbench.file_stems import (
    STEM_NO,
    STEM_TWICE,
    STEM_YES,
    derive_stem,
    stem_key,
    stem_usability,
)
from meeting_workbench.material_index import MaterialIndexer, index_status
from meeting_workbench.materials import ROOT_ONLINE, ROOT_VOLUME_OFFLINE


@pytest.mark.parametrize(
    ("name", "stem", "usable"),
    [
        ("260926 报价单 v3 终版.xlsx", "报价单", STEM_YES),
        ("01_需求说明书(1).docx", "需求说明书", STEM_YES),
        ("IMG_2031.JPG", "", STEM_NO),
        ("云图AI项目周报-0926.pptx", "云图AI项目周报", STEM_YES),
        ("README.md", "README", STEM_NO),
        ("合同.pdf", "合同", STEM_NO),
        ("20260926报价单.xlsx", "报价单", STEM_YES),
        ("报价单20260926", "报价单", STEM_YES),
        ("报价单（1）", "报价单", STEM_YES),
        ("报价单 副本 2.xlsx", "报价单", STEM_YES),
        ("CRM_接口文档.docx", "CRM_接口文档", STEM_YES),
        ("Roadmap.pptx", "Roadmap", STEM_YES),
        ("2026.xlsx", "2026", STEM_NO),
        ("index.js", "index", STEM_NO),
        ("【定稿】数据看板需求_2026-09-26_final.docx", "数据看板需求", STEM_YES),
        ("报价单_v1.2.xlsx", "报价单", STEM_YES),
        ("2026年9月26日 权限中心.key", "权限中心", STEM_YES),
        ("排班.xlsx", "排班", STEM_TWICE),
        ("周报.docx", "周报", STEM_NO),
        ("dev3.md", "dev3", STEM_YES),
    ],
)
def test_derive_stem(name, stem, usable):
    assert derive_stem(name) == stem
    assert stem_usability(stem_key(stem)) == usable


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("260931 报价单.xlsx", "260931 报价单"),
        ("报价单_0230.xlsx", "报价单_0230"),
        ("报价单_0229.xlsx", "报价单"),
        ("2026-02-30 报价单.xlsx", "2026-02-30 报价单"),
        ("230229 纪要整理.docx", "230229 纪要整理"),
    ],
)
def test_impossible_dates_are_not_stripped(name, stem):
    assert derive_stem(name) == stem


# ---------------------------------------------------------------------- 索引


def make(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    settings = SimpleNamespace(
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        data_dir=tmp_path / "data",
    )
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', ?)", (utc_now(),))
    return db, settings


def add_root(db, path, project_id="p"):
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES (?, ?, ?)",
        (project_id, str(path), utc_now()),
    )
    return db.query_one("SELECT id FROM project_material_roots WHERE path=?", (str(path),))["id"]


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def files(db, root_id=None, *, gone=False):
    where = "gone_at IS NOT NULL" if gone else "gone_at IS NULL"
    sql = f"SELECT rel_path, zone, stem FROM material_files WHERE {where}"
    params = ()
    if root_id is not None:
        sql += " AND root_id=?"
        params = (root_id,)
    return {row["rel_path"]: (row["zone"], row["stem"]) for row in db.query_all(sql, params)}


def run_until_done(indexer, rounds=50):
    for _ in range(rounds):
        indexer.run_round()
        states = {
            row["state"] for row in indexer.db.query_all("SELECT state FROM material_index_state")
        }
        if states == {"done"}:
            return
    raise AssertionError("没扫完")


def graph_rev(db):
    row = db.query_one("SELECT value FROM app_state WHERE key='graph_rev'")
    return int(row["value"]) if row else 0


def test_skip_rules_and_zones(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "报价单 v3.xlsx")
    write(root / "._报价单 v3.xlsx")
    write(root / ".DS_Store")
    write(root / "~$报价单.xlsx")
    write(root / "设计" / "首页原型.fig")
    write(root / "设计" / "app.js")
    write(root / "声档会议记录" / "2026-09-26 周会.md")
    for index in range(3):
        write(root / "node_modules" / f"pkg{index}" / "index.js")
    write(root / ".git" / "HEAD")
    (root / "权限中心.key").mkdir()
    write(root / "权限中心.key" / "Data" / "inner.png")
    os.symlink(root / "设计", root / "链接")
    settings.data_dir = root / "内部数据"
    write(root / "内部数据" / "workbench.sqlite3")
    root_id = add_root(db, root)

    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))

    assert files(db) == {
        "报价单 v3.xlsx": ("normal", "报价单"),
        "设计/首页原型.fig": ("normal", "首页原型"),
        "设计/app.js": ("code", "app"),
        "声档会议记录/2026-09-26 周会.md": ("cards", "周会"),
        "权限中心.key": ("package", "权限中心"),
    }
    name_only = {
        row["dir_rel"]: row["child_count"]
        for row in db.query_all(
            "SELECT dir_rel, child_count FROM material_dirs WHERE zone='name_only'"
        )
    }
    assert name_only == {"node_modules": 3, ".git": 1}
    status = index_status(db.connect())[0]
    assert status["root_id"] == root_id
    assert (status["state"], status["files"], status["name_only_dirs"]) == ("done", 5, 2)


def test_resumes_from_cursor_and_keeps_rows_quiet_when_nothing_changed(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    for folder in range(4):
        for index in range(5):
            write(root / f"文件夹{folder}" / f"方案说明{folder}{index}.docx")
    add_root(db, root)
    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0, round_entries=8)

    indexer.run_round()
    state = db.query_one("SELECT state, cursor FROM material_index_state")
    assert state["state"] == "walking" and state["cursor"]
    run_until_done(indexer)
    assert len(files(db)) == 20
    first = db.query_one("SELECT stems_rev, last_full_at FROM material_index_state")
    assert first["stems_rev"] == 1 and first["last_full_at"]

    # 再扫两轮：什么都没变，文件行、目录行、关系图版本号都不动
    before_rev = graph_rev(db)
    before_dirs = db.query_all("SELECT dir_rel, listed_at FROM material_dirs ORDER BY dir_rel")
    before_files = db.query_all("SELECT * FROM material_files ORDER BY id")
    fast = MaterialIndexer(db, settings, clock=lambda: 0.0)
    fast.run_round()
    fast.run_round()
    assert db.query_all("SELECT * FROM material_files ORDER BY id") == before_files
    assert (
        db.query_all("SELECT dir_rel, listed_at FROM material_dirs ORDER BY dir_rel") == before_dirs
    )
    assert files(db, gone=True) == {}
    assert graph_rev(db) == before_rev
    assert db.query_one("SELECT stems_rev FROM material_index_state")["stems_rev"] == 1


def test_changes_are_judged_per_directory(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "报价" / "报价单 v1.xlsx")
    write(root / "报价" / "报价单 v2.xlsx")
    write(root / "设计" / "旧稿" / "首页原型.fig")
    write(root / "设计" / "旧稿" / "更旧" / "登录页原型.fig")
    add_root(db, root)
    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0)
    run_until_done(indexer)

    (root / "报价" / "报价单 v1.xlsx").unlink()
    write(root / "报价" / "报价单 v3.xlsx")
    for path in sorted((root / "设计" / "旧稿").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (root / "设计" / "旧稿").rmdir()
    os.utime(root / "报价", ns=(1, 2_000_000_000))  # 修改时间变了才重读
    indexer.run_round()

    assert set(files(db)) == {"报价/报价单 v2.xlsx", "报价/报价单 v3.xlsx"}
    assert set(files(db, gone=True)) == {
        "报价/报价单 v1.xlsx",
        "设计/旧稿/首页原型.fig",
        "设计/旧稿/更旧/登录页原型.fig",
    }
    assert (
        db.query_one("SELECT COUNT(*) AS n FROM material_dirs WHERE dir_rel LIKE '设计/旧稿%'")["n"]
        == 0
    )
    gone_at = db.query_one(
        "SELECT gone_at FROM material_files WHERE rel_path='报价/报价单 v1.xlsx'"
    )
    indexer.run_round()
    # 不见了只写一次
    assert (
        db.query_one("SELECT gone_at FROM material_files WHERE rel_path='报价/报价单 v1.xlsx'")
        == gone_at
    )
    # 回来了清掉 gone_at
    write(root / "报价" / "报价单 v1.xlsx")
    os.utime(root / "报价", ns=(1, 3_000_000_000))
    indexer.run_round()
    assert "报价/报价单 v1.xlsx" in files(db)


def test_unplugged_mid_walk_keeps_rows_and_cursor(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    for folder in range(3):
        for index in range(4):
            write(root / f"文件夹{folder}" / f"方案说明{folder}{index}.docx")
    add_root(db, root)
    online = {"value": True}
    indexer = MaterialIndexer(
        db,
        settings,
        clock=lambda: 0.0,
        state_of=lambda path: ROOT_ONLINE if online["value"] else ROOT_VOLUME_OFFLINE,
    )
    run_until_done(indexer)
    count = len(files(db))

    # 拔盘：文件夹整个不见了，但盘的状态是「没插」
    moved = tmp_path / "别处"
    root.rename(moved)
    online["value"] = False
    indexer.run_round()
    state = db.query_one("SELECT state FROM material_index_state")
    assert state["state"] == "offline"
    assert len(files(db)) == count and files(db, gone=True) == {}

    # 插回来，接着扫：什么都没丢
    moved.rename(root)
    online["value"] = True
    run_until_done(indexer)
    assert len(files(db)) == count and files(db, gone=True) == {}


def test_unplugged_while_reading_a_subfolder_stops_without_marking_gone(tmp_path, monkeypatch):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    for folder in range(3):
        write(root / f"文件夹{folder}" / f"方案说明{folder}.docx")
    add_root(db, root)
    online = {"value": True}
    indexer = MaterialIndexer(
        db,
        settings,
        clock=lambda: 0.0,
        state_of=lambda path: ROOT_ONLINE if online["value"] else ROOT_VOLUME_OFFLINE,
    )
    run_until_done(indexer)
    before = files(db)

    # 整轮重读时读到「文件夹1」盘掉了：读失败，先看盘，盘不在就停
    from meeting_workbench import material_index

    real_scandir = os.scandir

    def failing(path):
        if str(path).endswith("文件夹1"):
            online["value"] = False
            raise OSError(5, "Input/output error")
        return real_scandir(path)

    monkeypatch.setattr(material_index.os, "scandir", failing)
    db.execute("UPDATE material_index_state SET last_full_at=NULL")
    cursor_before = db.query_one("SELECT cursor FROM material_index_state")["cursor"]
    indexer.run_round()
    state = db.query_one("SELECT state, cursor FROM material_index_state")
    assert state["state"] == "offline"
    assert state["cursor"] == cursor_before
    assert files(db) == before and files(db, gone=True) == {}

    # 盘在、只是这个子目录读不了：只跳过这棵子树
    online["value"] = True

    def unreadable(path):
        if str(path).endswith("文件夹1"):
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(material_index.os, "scandir", unreadable)
    run_until_done(indexer)
    assert files(db) == before and files(db, gone=True) == {}


def test_nested_roots_are_walked_once(tmp_path):
    db, settings = make(tmp_path)
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),)
    )
    outer = tmp_path / "资料"
    inner = outer / "数据中台"
    write(outer / "总体规划.docx")
    write(inner / "接口清单说明.xlsx")
    outer_id = add_root(db, outer)
    inner_id = add_root(db, inner, project_id="q")

    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))

    assert set(files(db, outer_id)) == {"总体规划.docx"}
    assert set(files(db, inner_id)) == {"接口清单说明.xlsx"}


def test_root_path_change_restarts_with_a_full_pass(tmp_path):
    db, settings = make(tmp_path)
    old = tmp_path / "云图AI"
    write(old / "报价单.xlsx")
    root_id = add_root(db, old)
    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0)
    run_until_done(indexer)

    new = tmp_path / "云图AI-新"
    write(new / "报价单.xlsx")
    write(new / "权限中心说明.docx")
    db.execute("UPDATE project_material_roots SET path=? WHERE id=?", (str(new), root_id))
    assert db.query_one("SELECT state, last_full_at FROM material_index_state") == {
        "state": "pending",
        "last_full_at": None,
    }
    run_until_done(indexer)
    assert set(files(db)) == {"报价单.xlsx", "权限中心说明.docx"}
    assert db.query_one("SELECT stems_rev FROM material_index_state")["stems_rev"] == 2


def test_nested_root_added_later_takes_its_files_from_the_outer_root(tmp_path):
    db, settings = make(tmp_path)
    db.execute(
        "INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', ?)", (utc_now(),)
    )
    outer = tmp_path / "资料"
    write(outer / "总体规划.docx")
    write(outer / "数据中台" / "接口清单说明.xlsx")
    outer_id = add_root(db, outer)
    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0)
    run_until_done(indexer)
    assert set(files(db, outer_id)) == {"总体规划.docx", "数据中台/接口清单说明.xlsx"}

    inner_id = add_root(db, outer / "数据中台", project_id="q")  # 磁盘上什么都没变
    run_until_done(indexer)

    assert set(files(db, outer_id)) == {"总体规划.docx"}
    assert set(files(db, inner_id)) == {"接口清单说明.xlsx"}


def test_moving_the_root_mid_round_does_not_write_back_the_old_cursor(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    for name in "abcdef":
        write(root / name / f"{name}文件说明.docx")
    root_id = add_root(db, root)
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    other = tmp_path / "新位置"
    write(other / "x" / "新的报价单.xlsx")

    indexer = MaterialIndexer(db, settings, clock=lambda: 0.0, round_entries=4)
    visit = indexer._visit
    calls = []

    def visit_then_move(root_id_, base, dir_rel, *, full):
        calls.append(dir_rel)
        if len(calls) == 2:  # 两个文件夹之间，你在项目页「重新选…」了位置
            db.execute(
                "UPDATE project_material_roots SET path=? WHERE id=?", (str(other), root_id_)
            )
        return visit(root_id_, base, dir_rel, full=full)

    indexer._visit = visit_then_move
    indexer.run_round()

    assert (
        db.query_one("SELECT cursor FROM material_index_state WHERE root_id=?", (root_id,))[
            "cursor"
        ]
        is None
    )
    indexer._visit = visit
    run_until_done(indexer)
    assert set(files(db, root_id)) == {"x/新的报价单.xlsx"}
