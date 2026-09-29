"""4a：文件流水。material_files 上的两个触发器怎么记，file_events 的 classify、recent_added、
day_groups 和 400 天清理。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

from meeting_workbench import file_events
from meeting_workbench.db import Database, utc_now
from meeting_workbench.material_index import MaterialIndexer

from .helpers import count_reads
from .test_material_index import add_root, graph_rev, make, run_until_done, write

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


def today() -> str:
    return date.today().isoformat()


def events(db: Database) -> list[dict]:
    return db.query_all(
        """SELECT file_id, rel_path, dir_rel, kind, content_key, size, mtime_ns, day
             FROM material_file_events ORDER BY id"""
    )


def swept(db: Database, root_id: int) -> None:
    """装作这个根目录已经扫完过一整轮。"""
    db.execute(
        """INSERT INTO material_index_state(root_id, state, last_full_at) VALUES (?, 'done', ?)
           ON CONFLICT(root_id) DO UPDATE SET last_full_at = excluded.last_full_at""",
        (root_id, utc_now()),
    )


def add_file(
    db: Database,
    root_id: int,
    rel_path: str,
    *,
    size: int | None = 10,
    mtime_ns: int | None = 1_000_000_000_000_000_000,
    zone: str = "normal",
    content_key: str | None = None,
) -> int:
    name = rel_path.rpartition("/")[2]
    stem, _dot, ext = name.rpartition(".")
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size,
                                      mtime_ns, zone, seen_at, content_key, content_size,
                                      content_mtime_ns)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            root_id,
            rel_path,
            rel_path.rpartition("/")[0],
            name,
            stem,
            stem,
            ext.lower(),
            size,
            mtime_ns,
            zone,
            utc_now(),
            content_key,
            size if content_key else None,
            mtime_ns if content_key else None,
        ),
    )
    return int(db.query_one("SELECT id FROM material_files WHERE rel_path = ?", (rel_path,))["id"])


def setup(tmp_path) -> tuple[Database, int]:
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p', '云图AI', 'x')")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/x/云图AI', 'x')"
    )
    return db, 1


# ---------------------------------------------------------------------- 触发器


def test_first_full_sweep_records_nothing_then_new_files_are_added(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    write(root / "报价" / "报价单 v2.xlsx")
    write(root / "设计" / "app.js")
    add_root(db, root)
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    assert events(db) == []
    rev = graph_rev(db)

    write(root / "报价" / "报价单 v3.xlsx", "新的一版")
    write(root / "设计" / "main.js")
    MaterialIndexer(db, settings, clock=lambda: 0.0).run_round()

    assert [(row["rel_path"], row["kind"], row["content_key"]) for row in events(db)] == [
        ("报价/报价单 v3.xlsx", "added", None)
    ]
    assert events(db)[0]["day"] == today()
    assert graph_rev(db) == rev  # 流水不进关系图版本号


def test_moving_a_keynote_package_is_a_move_not_a_new_file(tmp_path):
    db, settings = make(tmp_path)
    root = tmp_path / "云图AI"
    (root / "设计" / "方案.key").mkdir(parents=True)
    write(root / "设计" / "方案.key" / "Index.zip")
    (root / "归档").mkdir()
    add_root(db, root)
    run_until_done(MaterialIndexer(db, settings, clock=lambda: 0.0))
    assert events(db) == []

    os.rename(root / "设计" / "方案.key", root / "归档" / "方案.key")
    MaterialIndexer(db, settings, clock=lambda: 0.0).run_round()

    rows = db.query_all("SELECT * FROM material_file_events ORDER BY id")
    assert sorted((row["rel_path"], row["kind"], row["size"]) for row in rows) == [
        ("归档/方案.key", "added", None),
        ("设计/方案.key", "gone", None),
    ]
    added = next(row for row in rows if row["kind"] == "added")
    with db.autocommit() as connection:
        assert file_events.classify(connection, rows)[added["id"]] == "moved"
        assert file_events.day_groups(connection, "p", today()) == []
        assert file_events.recent_added(connection, [1], "2000-01-01T00:00:00+00:00", None) == []


def test_root_path_change_skips_the_next_full_sweep(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    add_file(db, root_id, "报价单.xlsx")
    assert [row["kind"] for row in events(db)] == ["added"]
    db.execute("UPDATE project_material_roots SET path = '/y/云图AI' WHERE id = ?", (root_id,))
    assert db.query_one("SELECT last_full_at FROM material_index_state") == {"last_full_at": None}
    add_file(db, root_id, "排期表.xlsx")
    db.execute("UPDATE material_files SET gone_at = 'x' WHERE rel_path = '报价单.xlsx'")
    assert len(events(db)) == 1
    # 没有状态行（还没开始扫）也不记
    db.execute("DELETE FROM material_index_state")
    add_file(db, root_id, "合同.pdf")
    assert len(events(db)) == 1


def test_changed_keeps_one_row_per_file_per_day(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    old_mtime = 1_000_000_000_000_000_000
    file_id = add_file(db, root_id, "报价单.xlsx", content_key="q2:first", mtime_ns=old_mtime)
    db.execute("DELETE FROM material_file_events")

    db.execute(
        "UPDATE material_files SET size = 11, content_key = 'q2:second' WHERE id = ?", (file_id,)
    )
    db.execute(
        "UPDATE material_files SET size = 12, content_key = 'q2:third' WHERE id = ?", (file_id,)
    )

    assert events(db) == [
        {
            "file_id": file_id,
            "rel_path": "报价单.xlsx",
            "dir_rel": "",
            "kind": "changed",
            "content_key": "q2:first",
            "size": 12,
            "mtime_ns": old_mtime,
            "day": today(),
        }
    ]


def test_gone_and_back(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    file_id = add_file(db, root_id, "报价单.xlsx", size=10, mtime_ns=500, content_key="q2:a")
    db.execute(
        "UPDATE material_files SET gone_at = ?, size = 99, mtime_ns = 600 WHERE id = ?",
        (utc_now(), file_id),
    )
    db.execute(
        "UPDATE material_files SET gone_at = NULL, size = 20, mtime_ns = 700 WHERE id = ?",
        (file_id,),
    )

    assert [
        (row["kind"], row["content_key"], row["size"], row["mtime_ns"]) for row in events(db)
    ] == [
        ("added", None, 10, 500),
        ("gone", "q2:a", 10, 500),  # 带原来的大小、修改时间和内容标识
        ("added", None, 20, 700),  # 又出现了
    ]


def test_exfat_quarter_hour_shift_is_ignored_but_real_edits_are_recorded(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    mtime = 1_700_000_000_000_000_000
    file_id = add_file(db, root_id, "报价单.xlsx", size=10, mtime_ns=mtime)
    db.execute("DELETE FROM material_file_events")
    quarter = 900_000_000_000
    db.execute(
        "UPDATE material_files SET mtime_ns = ? WHERE id = ?", (mtime - 4 * quarter, file_id)
    )
    db.execute(
        "UPDATE material_files SET mtime_ns = ? WHERE id = ?", (mtime + 4 * quarter, file_id)
    )
    assert events(db) == []
    # 大小变了，或者不是整刻钟，都是真的编辑
    db.execute(
        "UPDATE material_files SET mtime_ns = ?, size = 11 WHERE id = ?",
        (mtime + 8 * quarter, file_id),
    )
    db.execute("UPDATE material_files SET mtime_ns = mtime_ns + 1 WHERE id = ?", (file_id,))
    assert [(row["kind"], row["size"]) for row in events(db)] == [("changed", 11)]


def test_filling_the_content_key_records_nothing(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    file_id = add_file(db, root_id, "报价单.xlsx")
    db.execute("DELETE FROM material_file_events")
    db.execute(
        """UPDATE material_files SET content_key = 'q2:a', content_size = size,
               content_mtime_ns = mtime_ns, content_error = NULL, content_checked_at = 'x'
            WHERE id = ?""",
        (file_id,),
    )
    db.execute("UPDATE material_files SET seen_at = 'y', name = name WHERE id = ?", (file_id,))
    assert events(db) == []


def test_only_normal_files_and_iwork_packages_are_recorded(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    add_file(db, root_id, "报价单.xlsx")
    add_file(db, root_id, "src/app.js", zone="code")
    add_file(db, root_id, "声档会议记录/周会.md", zone="cards")
    for ext in ("key", "pages", "numbers", "app", "photoslibrary"):
        add_file(db, root_id, f"方案.{ext}", zone="package", size=None)
    assert [row["rel_path"] for row in events(db)] == [
        "报价单.xlsx",
        "方案.key",
        "方案.pages",
        "方案.numbers",
    ]
    db.execute("DELETE FROM material_file_events")
    # 包的 size 是空的，修改时间变了照样记 changed；code 区（切分支会一下改几千个）不记
    db.execute("UPDATE material_files SET mtime_ns = mtime_ns + 7")
    assert sorted(row["rel_path"] for row in events(db)) == [
        "报价单.xlsx",
        "方案.key",
        "方案.numbers",
        "方案.pages",
    ]
    assert {row["size"] for row in events(db) if row["rel_path"].startswith("方案")} == {None}


def test_changed_day_follows_the_file_mtime(tmp_path):
    db, root_id = setup(tmp_path)
    swept(db, root_id)
    now = datetime.now()
    ids = [add_file(db, root_id, f"文件{index}.docx", mtime_ns=1) for index in range(4)]
    db.execute("DELETE FROM material_file_events")
    cases = {
        ids[0]: now - timedelta(days=1),  # 过去 2 天之内：记到 mtime 那天
        ids[1]: now - timedelta(days=10),  # 太早：记到今天（整轮才发现）
        ids[2]: now + timedelta(days=3),  # 未来：记到今天
        ids[3]: now + timedelta(minutes=1),  # 未来 5 分钟以内：mtime 那天
    }
    for file_id, moment in cases.items():
        db.execute(
            "UPDATE material_files SET mtime_ns = ? WHERE id = ?",
            (int(moment.timestamp()) * 1_000_000_000, file_id),
        )
    days = {row["file_id"]: row["day"] for row in events(db)}
    assert days == {
        ids[0]: (now - timedelta(days=1)).date().isoformat(),
        ids[1]: today(),
        ids[2]: today(),
        ids[3]: (now + timedelta(minutes=1)).date().isoformat(),
    }


# ---------------------------------------------------------------------- classify


def add_event(
    db: Database,
    file_id: int,
    kind: str,
    *,
    root_id: int = 1,
    rel_path: str | None = None,
    size: int | None = 10,
    mtime_ns: int | None = 500,
    content_key: str | None = None,
    day: str = "2026-09-27",
    at: str = "2026-09-27T06:00:00.000Z",
) -> dict:
    if rel_path is None:
        rel_path = db.query_one("SELECT rel_path FROM material_files WHERE id = ?", (file_id,))[
            "rel_path"
        ]
    db.execute(
        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key,
                                            size, mtime_ns, day, at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            root_id,
            file_id,
            rel_path,
            rel_path.rpartition("/")[0],
            kind,
            content_key,
            size,
            mtime_ns,
            day,
            at,
        ),
    )
    return db.query_one("SELECT * FROM material_file_events ORDER BY id DESC LIMIT 1")


def classify(db: Database, rows: list[dict], now: datetime = NOW) -> list[str]:
    with db.autocommit() as connection:
        labels = file_events.classify(connection, rows, now=now)
    return [labels[row["id"]] for row in rows]


def test_classify_moves_by_size_and_mtime_within_two_days(tmp_path):
    db, _root_id = setup(tmp_path)  # 没有状态行：触发器不记，流水由测试直接写
    old = add_file(db, 1, "旧/报价单.xlsx", size=10, mtime_ns=500)
    new = add_file(db, 1, "新/报价单改名.xlsx", size=10, mtime_ns=500)
    late_old = add_file(db, 1, "旧/合同.pdf", size=30, mtime_ns=900, content_key="q2:c1")
    late_new = add_file(db, 1, "新/合同终版.pdf", size=30, mtime_ns=900, content_key="q2:c2")
    again = add_file(db, 1, "排期.xlsx", size=40, mtime_ns=700)
    add_event(db, old, "gone", day="2026-09-25")
    add_event(db, late_old, "gone", size=30, mtime_ns=900, day="2026-09-24")  # 早了 3 天
    add_event(db, again, "gone", size=40, mtime_ns=700, day="2026-09-01")
    rows = [
        add_event(db, new, "added"),
        add_event(db, late_new, "added", size=30, mtime_ns=900),
        add_event(db, again, "added", size=40, mtime_ns=700),
        add_event(db, again, "changed", size=41, mtime_ns=701),
    ]
    assert classify(db, rows) == ["moved", "added", "back", "changed"]


def test_classify_moves_by_content_key_and_across_roots_of_the_same_project(tmp_path):
    db, _root_id = setup(tmp_path)
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/y/云图AI', 'x')"
    )
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '数据中台', 'x')")
    db.execute(
        "INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('q', '/z/数据中台', 'x')"
    )
    old = add_file(db, 1, "旧/报价单.xlsx", size=10, mtime_ns=500, content_key="q2:a")
    other = add_file(db, 3, "报价单.xlsx", size=40, mtime_ns=800, content_key="q2:b")
    moved = add_file(db, 2, "报价单.xlsx", size=11, mtime_ns=501, content_key="q2:a")
    elsewhere = add_file(db, 1, "新/报价单.xlsx", size=40, mtime_ns=800, content_key="q2:c")
    add_event(db, old, "gone", content_key="q2:a", day="2026-09-26")
    add_event(db, other, "gone", root_id=3, size=40, mtime_ns=800, content_key="q2:b")
    rows = [
        add_event(db, moved, "added", root_id=2, size=11, mtime_ns=501),
        # 别的项目里大小和修改时间相同的 gone 不算
        add_event(db, elsewhere, "added", size=40, mtime_ns=800),
    ]
    assert classify(db, rows) == ["moved", "added"]


def test_classify_copies_and_true_additions(tmp_path):
    db, _root_id = setup(tmp_path)
    source = add_file(db, 1, "报价单.xlsx", size=10, mtime_ns=500, content_key="q2:a")
    copy = add_file(db, 1, "备份/报价单.xlsx", size=10, mtime_ns=700, content_key="q2:a")
    fresh = add_file(db, 1, "新方案.docx", size=20, mtime_ns=800, content_key="q2:n")
    # 源文件后来删了，复制那一刻它还在
    db.execute(
        "UPDATE material_files SET gone_at = '2026-09-28T00:00:00+00:00' WHERE id = ?", (source,)
    )
    rows = [add_event(db, copy, "added", mtime_ns=700), add_event(db, fresh, "added", size=20)]
    assert classify(db, rows) == ["copied", "added"]
    # 源文件在复制之前就不见了：内容不在盘上，算新增
    db.execute(
        "UPDATE material_files SET gone_at = '2026-09-01T00:00:00+00:00' WHERE id = ?", (source,)
    )
    assert classify(db, rows[:1]) == ["added"]


def test_classify_waits_for_the_content_key_then_uses_name_and_size(tmp_path):
    db, _root_id = setup(tmp_path)
    add_file(db, 1, "资料/需求说明.docx", size=10, mtime_ns=100, content_key="q2:x")
    waiting = add_file(db, 1, "需求说明.docx", size=10, mtime_ns=900)
    stale = add_file(db, 1, "排期表.xlsx", size=10, mtime_ns=900, content_key="q2:old")
    db.execute("UPDATE material_files SET size = 11 WHERE id = ?", (stale,))  # 旧标识不算
    errored = add_file(db, 1, "损坏.pdf", size=10, mtime_ns=900)
    db.execute("UPDATE material_files SET content_error = 'corrupt' WHERE id = ?", (errored,))
    rows = [
        add_event(db, waiting, "added", mtime_ns=900, at="2026-09-27T07:30:00.000Z"),
        add_event(db, stale, "added", size=11, mtime_ns=900, at="2026-09-27T07:30:00.000Z"),
        add_event(db, errored, "added", mtime_ns=900, at="2026-09-27T07:30:00.000Z"),
    ]
    assert classify(db, rows) == ["pending", "pending", "added"]
    # 等过 60 分钟：同一个项目里有同名同大小的活文件，算复制来的
    assert classify(db, rows, now=NOW + timedelta(minutes=31)) == ["copied", "added", "added"]


def test_unkeyable_files_are_judged_by_name_and_size(tmp_path):
    db, _root_id = setup(tmp_path)
    gone_zip = add_file(db, 1, "旧/素材.zip", size=50, mtime_ns=100)
    moved_zip = add_file(db, 1, "新/素材.zip", size=50, mtime_ns=222)
    add_file(db, 1, "设计/首页原型.fig", size=70, mtime_ns=100)
    copied_fig = add_file(db, 1, "归档/首页原型.fig", size=70, mtime_ns=333)
    add_file(db, 1, "设计/方案.key", size=None, mtime_ns=100, zone="package")
    copied_key = add_file(db, 1, "归档/方案.key", size=None, mtime_ns=444, zone="package")
    resized = add_file(db, 1, "归档/首页原型 v2.fig", size=71, mtime_ns=555)
    add_event(db, gone_zip, "gone", size=50, mtime_ns=100, day="2026-09-26")
    rows = [
        add_event(db, moved_zip, "added", size=50, mtime_ns=222),
        add_event(db, copied_fig, "added", size=70, mtime_ns=333),
        add_event(db, copied_key, "added", size=None, mtime_ns=444),
        add_event(db, resized, "added", size=71, mtime_ns=555),
    ]
    # 算不出内容标识的文件不用等，刚出现就按名字加大小判断
    assert classify(db, rows, now=NOW - timedelta(hours=3)) == [
        "moved",
        "copied",
        "copied",
        "added",
    ]


# ---------------------------------------------------------------------- 读


def test_recent_added_returns_only_true_additions_in_the_window(tmp_path):
    db, _root_id = setup(tmp_path)
    moved_from = add_file(db, 1, "旧/报价单.xlsx", size=10, mtime_ns=500)
    moved_to = add_file(db, 1, "新/报价单.xlsx", size=10, mtime_ns=500)
    fresh = add_file(db, 1, "新/方案.docx", size=20, mtime_ns=600, content_key="q2:n")
    early = add_file(db, 1, "新/早.docx", size=30, mtime_ns=700, content_key="q2:e")
    add_event(db, moved_from, "gone")
    add_event(db, moved_to, "added")
    wanted = add_event(db, fresh, "added", size=20, mtime_ns=600, at="2026-09-27T07:00:00.000Z")
    add_event(
        db, early, "added", size=30, mtime_ns=700, day="2026-09-20", at="2026-09-20T07:00:00.000Z"
    )
    add_event(db, fresh, "changed", size=21, mtime_ns=601)

    with db.autocommit() as connection:
        found = file_events.recent_added(
            connection, [1], datetime(2026, 9, 26, tzinfo=UTC), "2026-09-28T00:00:00+00:00", now=NOW
        )
        assert [row["id"] for row in found] == [wanted["id"]]
        assert file_events.recent_added(connection, [], "2026-09-26T00:00:00Z", None) == []
        assert (
            file_events.recent_added(
                connection, [1], "2026-09-26T00:00:00Z", "2026-09-27T07:00:00Z", now=NOW
            )
            == []
        )


def test_day_groups_count_additions_and_changes_per_folder(tmp_path):
    db, _root_id = setup(tmp_path)
    moved_from = add_file(db, 1, "旧/报价单.xlsx", size=10, mtime_ns=500)
    moved_to = add_file(db, 1, "报价/报价单.xlsx", size=10, mtime_ns=500)
    add_event(db, moved_from, "gone")
    add_event(db, moved_to, "added", at="2026-09-27T06:00:00.000Z")
    names = []
    for index in range(4):
        file_id = add_file(
            db, 1, f"报价/附件{index}.docx", size=100 + index, content_key=f"q2:{index}"
        )
        add_event(db, file_id, "added", size=100 + index, at=f"2026-09-27T06:0{index}:00.000Z")
        names.append(f"附件{index}.docx")
    # 同一天新增过的文件不再算修改
    add_event(db, file_id, "changed", size=200, at="2026-09-27T06:30:00.000Z")
    edited = add_file(db, 1, "排期/排期表.xlsx", size=5, content_key="q2:plan")
    add_event(db, edited, "changed", size=6, at="2026-09-27T09:00:00.000Z")
    add_event(db, edited, "changed", size=7, day="2026-09-25", at="2026-09-25T09:00:00.000Z")
    add_event(db, edited, "gone", size=7, day="2026-09-26", at="2026-09-26T09:00:00.000Z")

    with db.autocommit() as connection:
        groups = file_events.day_groups(connection, "p", "2026-09-25", now=NOW)
        paged = file_events.day_groups(connection, "p", "2026-09-01", before="2026-09-26", now=NOW)
        assert file_events.day_groups(connection, "nobody", "2026-09-01") == []

    assert [(g["day"], g["dir_rel"], g["added"], g["changed"], g["names"]) for g in groups] == [
        ("2026-09-27", "排期", 0, 1, ["排期表.xlsx"]),
        ("2026-09-27", "报价", 4, 0, ["附件3.docx", "附件2.docx", "附件1.docx"]),
        ("2026-09-25", "排期", 0, 1, ["排期表.xlsx"]),
    ]
    assert groups[1]["root_id"] == 1 and groups[1]["at"] == "2026-09-27T06:03:00.000Z"
    assert len(groups[1]["file_ids"]) == 3
    assert [group["day"] for group in paged] == ["2026-09-25"]


def test_day_groups_statement_count_does_not_grow(tmp_path):
    """时间线有语句数上限：day_groups 是取流水一条加 classify 一条，和事件多少无关。"""
    db, _root_id = setup(tmp_path)

    def seed(start: int, count: int) -> None:
        with db.transaction() as connection:
            for index in range(start, start + count):
                for folder, kind in (("旧", "gone"), ("新", "added")):
                    rel_path = f"{folder}/文件{index}.zip"
                    file_id = connection.execute(
                        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key,
                                                      ext, size, mtime_ns, seen_at)
                           VALUES (1, ?, ?, ?, ?, ?, 'zip', ?, ?, 'x')""",
                        (
                            rel_path,
                            folder,
                            f"文件{index}.zip",
                            f"文件{index}",
                            f"文件{index}",
                            index,
                            index,
                        ),
                    ).lastrowid
                    connection.execute(
                        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind,
                                                            size, mtime_ns, day, at)
                           VALUES (1, ?, ?, ?, ?, ?, ?, '2026-09-27', '2026-09-27T06:00:00.000Z')""",
                        (file_id, rel_path, folder, kind, index, index),
                    )

    def reads() -> int:
        return count_reads(db, lambda c: file_events.day_groups(c, "p", "2026-09-01", now=NOW))

    seed(0, 5)
    small = reads()
    seed(5, 300)
    assert reads() == small == 2
    with db.autocommit() as connection:
        assert file_events.day_groups(connection, "p", "2026-09-01", now=NOW) == []


def test_prune_keeps_four_hundred_days(tmp_path):
    db, _root_id = setup(tmp_path)
    file_id = add_file(db, 1, "报价单.xlsx")
    # 400 天前是 2025-08-23，更早的删掉
    for day in ("2025-08-21", "2025-08-22", "2025-08-23", "2025-08-24", "2026-09-27"):
        add_event(db, file_id, "changed", day=day)
    with db.transaction() as connection:
        assert file_events.prune(connection, today=date(2026, 9, 27), limit=1) == 1
    with db.transaction() as connection:
        assert file_events.prune(connection, today=date(2026, 9, 27)) == 1
    with db.transaction() as connection:
        assert file_events.prune(connection, today=date(2026, 9, 27)) == 0
    assert [
        row["day"] for row in db.query_all("SELECT day FROM material_file_events ORDER BY day")
    ] == [
        "2025-08-23",
        "2025-08-24",
        "2026-09-27",
    ]
