"""第四期 4e：产出建议（produced.watch，L4）。窗口起点、范围 A 和 B、共同词、哪些事件算、跳过和上限、
收回、［是］的登记和撤销、证据的字。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from meeting_workbench import produced, relations
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.file_events import at_text
from meeting_workbench.relation_read import produced_evidence_text, task_questions
from meeting_workbench.relations import RelationError
from meeting_workbench.tasks import TaskService, _delete_deliverable, _insert_deliverable

from .test_file_events import swept

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
ROOT = "/材料/能源平台资料"


def at(days: float = 0, minutes: float = 0) -> datetime:
    return NOW + timedelta(days=days, minutes=minutes)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def world(tmp_path, *, links_since: datetime | None = None):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    db.execute(
        "INSERT INTO projects(id, name, also_names, created_at) VALUES ('p', '云图AI', '[\"云图平台\"]', 'x')"
    )
    db.execute("INSERT INTO projects(id, name, created_at) VALUES ('q', '别的项目', 'x')")
    db.execute("INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', ?, 'x')", (ROOT,))
    root_id = int(db.query_one("SELECT id FROM project_material_roots WHERE path = ?", (ROOT,))["id"])
    swept(db, root_id)
    db.execute(
        "UPDATE app_state SET value = ? WHERE key = 'links_since'", (stamp(links_since or at(-60)),)
    )
    return SimpleNamespace(db=db, root=root_id)


def meeting(db, meeting_id: str, day: datetime) -> None:
    db.execute(
        """INSERT INTO meetings(id, title, recording_date, status, project_id, created_at, updated_at)
           VALUES (?, ?, ?, 'completed_unreviewed', 'p', ?, ?)""",
        (meeting_id, f"会 {meeting_id}", day.astimezone().date().isoformat(), utc_now(), utc_now()),
    )


def requirement(db, requirement_id: str, title: str, *folders: str) -> None:
    db.execute(
        """INSERT INTO requirements(id, project_id, title, priority, status, created_at, updated_at)
           VALUES (?, 'p', ?, 'P1', 'active', 'x', 'x')""",
        (requirement_id, title),
    )
    for folder in folders:
        db.execute(
            "INSERT INTO requirement_folders(requirement_id, path, created_at) VALUES (?, ?, 'x')",
            (requirement_id, folder),
        )


def task(
    db,
    task_id: str,
    title: str,
    *,
    status: str = "confirmed",
    origin: str = "ai",
    meeting_id: str | None = None,
    requirement_id: str | None = None,
    project_id: str = "p",
    events=(("confirmed", "任务已确认", -3),),
) -> None:
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, meeting_id, project_id, requirement_id,
                             status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'x', 'x', 'x')""",
        (task_id, title, status, origin, meeting_id, project_id, requirement_id),
    )
    for kind, body, days in events:
        db.execute(
            "INSERT INTO task_events(task_id, kind, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, body, stamp(at(days))),
        )


def put_file(
    db,
    root_id: int,
    rel_path: str,
    when: datetime,
    *,
    size: int | None = 10,
    mtime_ns: int = 1_700_000_000_000_000_000,
    zone: str = "normal",
    content_key: str | None = "auto",
) -> int:
    """建一行文件（触发器记一条 added），再把这条流水挪到 when。content_key 为 auto 时按路径给一个。"""
    name = rel_path.rpartition("/")[2]
    stem, _dot, ext = name.rpartition(".")
    key = f"q2:{abs(hash(rel_path)) % 10**32:032d}" if content_key == "auto" else content_key
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone,
               seen_at, content_key, content_size, content_mtime_ns)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (root_id, rel_path, rel_path.rpartition("/")[0], name, stem or name, stem or name, ext.lower(), size,
         mtime_ns, zone, utc_now(), key, size if key else None, mtime_ns if key else None),
    )
    file_id = int(db.query_one("SELECT id FROM material_files WHERE root_id = ? AND rel_path = ?", (root_id, rel_path))["id"])
    move_events(db, file_id, when)
    return file_id


def move_events(db, file_id: int, when: datetime, kind: str | None = None) -> None:
    where = "file_id = ?" + (" AND kind = ?" if kind else "")
    params = (at_text(when), when.astimezone().date().isoformat(), file_id, *((kind,) if kind else ()))
    db.execute(f"UPDATE material_file_events SET at = ?, day = ? WHERE {where}", params)


def change(db, file_id: int, when: datetime) -> None:
    """原地改过：大小和修改时间都变了（内容标识跟着重算过）。"""
    db.execute(
        """UPDATE material_files SET size = size + 5, mtime_ns = mtime_ns + 7,
               content_size = size + 5, content_mtime_ns = mtime_ns + 7 WHERE id = ?""",
        (file_id,),
    )
    move_events(db, file_id, when, "changed")


def old_file(db, root_id: int, rel_path: str, **kwargs) -> int:
    """一份早就有的文件：去掉它的 added。"""
    file_id = put_file(db, root_id, rel_path, at(-100), **kwargs)
    db.execute("DELETE FROM material_file_events WHERE file_id = ?", (file_id,))
    return file_id


def watch(db, now: datetime = NOW, **kwargs):
    return produced.watch(db, now, 5.0, since=stamp(now), **kwargs)


def asked(db, task_id: str | None = None) -> list[dict]:
    sql = """SELECT r.*, f.name FROM relations r LEFT JOIN material_files f ON f.id = r.file_id
              WHERE r.kind = 'produced' AND r.status = 'suggested'"""
    params: tuple = ()
    if task_id:
        sql += " AND r.task_id = ?"
        params = (task_id,)
    return db.query_all(sql + " ORDER BY r.id", params)


def names(db, task_id: str | None = None) -> list[str]:
    return sorted(row["name"] for row in asked(db, task_id))


def evidence(row: dict) -> dict:
    return json.loads(row["evidence_json"])


# ---------------------------------------------------------------------- 窗口起点


def test_window_starts_at_the_last_entry_into_confirmed(tmp_path):
    w = world(tmp_path)
    # 抽出的任务：AI 建的 created 不算，从确认那一刻起
    task(w.db, "t-ai", "整理报价单明细", events=(("created", "AI 从会后纪要生成本条任务草稿", -5), ("confirmed", "任务已确认", -3)))
    put_file(w.db, w.root, "报价/报价单明细 旧.xlsx", at(-4))
    put_file(w.db, w.root, "报价/报价单明细 v2.xlsx", at(-2))
    # 手动建的任务：取 created
    task(w.db, "t-manual", "写一版能耗看板方案", origin="manual", events=(("created", "手动创建任务", -3),))
    put_file(w.db, w.root, "看板/能耗看板方案.docx", at(-1))
    # 取消后再确认：从再确认那一刻起
    task(w.db, "t-again", "排期总表梳理", events=(
        ("confirmed", "任务已确认", -10), ("rejected", "任务被驳回并取消", -8),
        ("status_changed", "cancelled → confirmed", -2),
    ))
    put_file(w.db, w.root, "排期/排期总表 初稿.xlsx", at(-5))
    put_file(w.db, w.root, "排期/排期总表 定稿.xlsx", at(-1))

    watch(w.db)

    assert names(w.db, "t-ai") == ["报价单明细 v2.xlsx"]
    assert names(w.db, "t-manual") == ["能耗看板方案.docx"]
    assert names(w.db, "t-again") == ["排期总表 定稿.xlsx"]
    assert evidence(asked(w.db, "t-manual")[0])["ref"] == "confirm"


def test_moving_back_from_in_progress_does_not_reopen_the_window(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细", status="confirmed", events=(
        ("confirmed", "任务已确认", -20), ("status_changed", "confirmed → in_progress", -18),
        ("status_changed", "in_progress → confirmed", -1),
    ))
    put_file(w.db, w.root, "报价/报价单明细 v9.xlsx", at(-0.5))
    watch(w.db)
    assert asked(w.db) == []


def test_attaching_a_requirement_later_reopens_the_window(tmp_path):
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    task(w.db, "t", "写方案", requirement_id="r", events=(
        ("confirmed", "任务已确认", -30), ("requirement_changed", "挂到需求「能耗看板」", -2),
    ))
    put_file(w.db, w.root, "能耗看板/随手记.docx", at(-1))
    watch(w.db)
    row = asked(w.db, "t")[0]
    assert evidence(row)["scope"] == "folder" and evidence(row)["folder"] == "能耗看板/"


def test_reverted_confirmation_and_ai_created_event_do_not_count(tmp_path):
    w = world(tmp_path)
    # 确认被撤销：回到待确认，自然不在里面
    task(w.db, "t-reverted", "整理报价单明细", status="pending_confirm", events=(
        ("confirmed", "任务已确认", -3), ("reverted", "撤销上一步，恢复为待确认", -3),
    ))
    # AI 建的 created 不算起点：只剩 links_since（60 天前），窗口早关了
    task(w.db, "t-ai", "整理报价单明细", events=(("created", "AI 从会后纪要生成本条任务草稿", -3),))
    put_file(w.db, w.root, "报价/报价单明细 v2.xlsx", at(-1))
    watch(w.db)
    assert asked(w.db) == []


def test_links_since_is_a_floor(tmp_path):
    # 升级以前的文件没有流水，不补：起点不早于 links_since
    w = world(tmp_path, links_since=at(-1))
    task(w.db, "t", "整理报价单明细", events=(("confirmed", "任务已确认", -5),))
    put_file(w.db, w.root, "报价/报价单明细 v1.xlsx", at(-2))
    put_file(w.db, w.root, "报价/报价单明细 v2.xlsx", at(-0.5))
    watch(w.db)
    assert names(w.db) == ["报价单明细 v2.xlsx"]


# ---------------------------------------------------------------------- 范围和共同词


def test_folder_scope_needs_no_shared_word_but_the_root_itself_does(tmp_path):
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    requirement(w.db, "r-root", "排班", ROOT)
    task(w.db, "t", "写一版方案", requirement_id="r")
    task(w.db, "t-root", "写一版说明", requirement_id="r-root")
    put_file(w.db, w.root, "能耗看板/草图/随手记.docx", at(-1))
    put_file(w.db, w.root, "别处/随手记2.docx", at(-1))
    watch(w.db)
    assert names(w.db, "t") == ["随手记.docx"]
    # 文件夹就是根目录本身：按范围 B，名字对不上不问
    assert names(w.db, "t-root") == []


@pytest.mark.parametrize(
    ("title", "rel_path"),
    [
        ("修改最终版本文件", "文档/最终版本文件.docx"),  # 只由泛词拼成
        ("给云图AI写介绍", "介绍/云图AI.docx"),  # 项目名
        ("云图平台接入", "接入/云图平台.docx"),  # 项目的也叫
        ("能源平台资料整理", "整理/能源平台资料汇编.docx"),  # 根目录名
        ("测试接口", "接口/接口.docx"),  # 两个字的词
        ("写需求文档", "需求/需求文档.docx"),  # 只由常见两字词拼成
    ],
)
def test_generic_words_do_not_count(tmp_path, title, rel_path):
    w = world(tmp_path)
    task(w.db, "t", title)
    put_file(w.db, w.root, rel_path, at(-1))
    watch(w.db)
    assert asked(w.db) == []


def test_shared_word_in_name_or_only_in_folder(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    task(w.db, "t2", "能耗看板方案")
    put_file(w.db, w.root, "报价/报价单明细-v2.xlsx", at(-1))
    put_file(w.db, w.root, "新建/能耗看板/草图.docx", at(-1))
    watch(w.db)
    by_name = {row["name"]: evidence(row) for row in asked(w.db)}
    assert by_name["报价单明细-v2.xlsx"]["words"] == ["报价单明细"]
    assert by_name["报价单明细-v2.xlsx"]["folder"] is None and by_name["报价单明细-v2.xlsx"]["scope"] == "word"
    # 词只在文件夹名里：写有这个词的那一层文件夹
    assert by_name["草图.docx"]["folder"] == "能耗看板/" and by_name["草图.docx"]["words"] == ["能耗看板"]


def test_shared_word_rules():
    assert produced.shared_word("整理报价单明细", "报价单明细-v2") == "报价单明细"
    assert produced.shared_word("Dashboard 改版", "dashboard_final") == "dashboard"
    assert produced.shared_word("v3报价", "V3报价单") == "v3报价"
    assert produced.shared_word("写api", "api文档") is None  # 字母数字不到 4 个、混排不到 3 个
    assert produced.shared_word("整理会议纪要", "会议纪要") is None  # 停用词
    assert produced.shared_word("写一版方案", "方案") is None


# ---------------------------------------------------------------------- 哪些事件算


def test_moved_copied_and_back_do_not_count(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    # 挪过来的：另一份文件前一天不见了，大小和修改时间都一样
    gone = old_file(w.db, w.root, "旧/报价单明细 a.xlsx", content_key=None, mtime_ns=111)
    w.db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), gone))
    move_events(w.db, gone, at(-1.2), "gone")
    put_file(w.db, w.root, "报价/报价单明细 a.xlsx", at(-1), content_key=None, mtime_ns=111)
    # 复制来的：同一份内容早就有一份活的
    old_file(w.db, w.root, "旧/报价单明细 b.xlsx", content_key="q2:" + "b" * 32)
    put_file(w.db, w.root, "报价/报价单明细 b 副本.xlsx", at(-1), content_key="q2:" + "b" * 32)
    # 又出现的：不见了又回来
    back = old_file(w.db, w.root, "报价/报价单明细 c.xlsx")
    w.db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), back))
    w.db.execute("UPDATE material_files SET gone_at = NULL WHERE id = ?", (back,))
    move_events(w.db, back, at(-1))
    watch(w.db)
    assert asked(w.db) == []


def test_pending_waits_sixty_minutes_then_goes_by_name_and_size(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    put_file(w.db, w.root, "报价/报价单明细 新.xlsx", at(minutes=-30), content_key=None)
    watch(w.db)
    assert asked(w.db) == []
    watch(w.db, now=at(minutes=45))
    assert names(w.db) == ["报价单明细 新.xlsx"]
    row = asked(w.db)[0]
    assert row["content_key"] is None and row["ident"] == f"t|p:{w.root}:报价/报价单明细 新.xlsx"


@pytest.mark.parametrize(
    ("rel_path", "size", "zone"),
    [
        ("报价/报价单明细.tmp", 10, "normal"),
        ("报价/._报价单明细.xlsx", 10, "normal"),
        ("报价/~$报价单明细.xlsx", 10, "normal"),
        ("报价/报价单明细.xlsx.crdownload", 10, "normal"),
        ("报价/报价单明细 空.xlsx", 0, "normal"),
        ("报价/报价单明细.app", None, "package"),
    ],
)
def test_temporary_shadow_empty_and_other_packages_do_not_count(tmp_path, rel_path, size, zone):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    put_file(w.db, w.root, rel_path, at(-1), size=size, zone=zone, content_key=None)
    watch(w.db)
    assert asked(w.db) == []


def test_keynote_package_without_size_counts(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    file_id = put_file(w.db, w.root, "报价/报价单明细.key", at(-1), size=None, zone="package", content_key=None)
    watch(w.db)
    row = asked(w.db)[0]
    assert row["file_id"] == file_id and row["ident"] == f"t|p:{w.root}:报价/报价单明细.key"


def test_bursts_need_a_shared_word_even_in_the_folder(tmp_path):
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    task(w.db, "t", "写方案", requirement_id="r")
    for index in range(24):
        put_file(w.db, w.root, f"能耗看板/解压/图{index:02d}.png", at(-1, minutes=index * 0.2))
    put_file(w.db, w.root, "能耗看板/解压/能耗看板说明.docx", at(-1, minutes=5))
    watch(w.db)
    assert names(w.db) == ["能耗看板说明.docx"]


def test_changed_files_need_a_shared_word(tmp_path):
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    task(w.db, "t", "写方案", requirement_id="r")
    task(w.db, "t2", "整理报价单明细")
    plain = old_file(w.db, w.root, "能耗看板/随手记.docx")
    quote = old_file(w.db, w.root, "报价/报价单明细.xlsx")
    change(w.db, plain, at(-1))
    change(w.db, quote, at(-1))
    watch(w.db)
    assert names(w.db) == ["报价单明细.xlsx"]
    assert evidence(asked(w.db)[0])["event_kind"] == "changed"


# ---------------------------------------------------------------------- 跳过和上限


def test_caps_per_task_requirement_and_project(tmp_path):
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    for index in range(3):
        task(w.db, f"tr{index}", f"写第{index}版", requirement_id="r")
    for index in range(7):
        put_file(w.db, w.root, f"能耗看板/稿{index}.docx", at(-1, minutes=index))
    for index in range(3):
        task(w.db, f"tw{index}", f"整理报价单明细{index}号")
        for copy in range(3):
            put_file(w.db, w.root, f"报价/报价单明细{index}号-{copy}.xlsx", at(-1, minutes=copy))

    watch(w.db)

    rows = asked(w.db)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["task_id"]] = counts.get(row["task_id"], 0) + 1
    assert len(rows) == 8  # 每个项目 8 个
    assert max(counts.values()) == 2  # 每条任务 2 个
    assert sum(counts.get(f"tr{index}", 0) for index in range(3)) <= 5  # 每个需求 5 个
    # 一份文件只给一条任务
    assert len({row["content_key"] for row in rows}) == 8
    # 上限只挡新增：再跑一轮不多也不少，已经在问的不被挤掉
    before = [row["id"] for row in rows]
    watch(w.db)
    assert [row["id"] for row in asked(w.db)] == before


def test_a_file_in_two_windows_goes_to_the_stronger_match(tmp_path):
    w = world(tmp_path)
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    task(w.db, "t-word", "能耗看板说明")
    task(w.db, "t-folder", "写方案", requirement_id="r")
    put_file(w.db, w.root, "能耗看板/能耗看板说明.docx", at(-1))
    watch(w.db)
    assert [(row["task_id"], row["name"]) for row in asked(w.db)] == [("t-folder", "能耗看板说明.docx")]


def test_deliverables_rejections_and_expired_rows_are_skipped(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    task(w.db, "t-other", "别的事")
    done = put_file(w.db, w.root, "报价/报价单明细 已交.xlsx", at(-1))
    with w.db.transaction() as connection:
        _insert_deliverable(connection, "t-other", name="x", content_key=None, root_id=w.root,
                            rel_path="报价/报价单明细 已交.xlsx", now=utc_now())
    said_no = put_file(w.db, w.root, "报价/报价单明细 不是.xlsx", at(-1))
    key = w.db.query_one("SELECT content_key FROM material_files WHERE id = ?", (said_no,))["content_key"]
    w.db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, task_id, content_key, file_id, created_at, updated_at)
           VALUES ('produced', 'p', ?, 'rejected', 'rule', 't-other', ?, ?, 'x', 'x')""",
        (f"t-other|{key}", key, said_no),
    )
    assert done
    watch(w.db)
    assert asked(w.db) == []


def test_thirty_days_without_an_answer_clears_and_never_asks_again(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细", status="done")
    put_file(w.db, w.root, "报价/报价单明细.xlsx", at(-1))
    # done 的任务不再问新文件
    watch(w.db)
    assert asked(w.db) == []
    w.db.execute("UPDATE tasks SET status = 'confirmed' WHERE id = 't'")
    watch(w.db)
    row = asked(w.db)[0]
    # 做完的时候正好认交付物：done 以后照样留着
    w.db.execute("UPDATE tasks SET status = 'done' WHERE id = 't'")
    watch(w.db, now=at(29))
    assert asked(w.db)[0]["id"] == row["id"]
    w.db.execute("UPDATE tasks SET status = 'confirmed' WHERE id = 't'")
    watch(w.db, now=at(31))
    assert w.db.query_one("SELECT status FROM relations WHERE id = ?", (row["id"],))["status"] == "cleared"
    watch(w.db, now=at(1))
    assert asked(w.db) == []


@pytest.mark.parametrize("change_task", ["cancelled", "expired", "pending_confirm", "moved", "deliverable"])
def test_rows_are_cleared_when_the_task_no_longer_qualifies(tmp_path, change_task):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    file_id = put_file(w.db, w.root, "报价/报价单明细.xlsx", at(-1))
    watch(w.db)
    row = asked(w.db)[0]
    if change_task == "moved":
        w.db.execute("UPDATE tasks SET project_id = 'q' WHERE id = 't'")
    elif change_task == "deliverable":
        # 文件从别的路（任务抽屉）成了交付物
        with w.db.transaction() as connection:
            _insert_deliverable(connection, "t", name="报价单明细.xlsx", content_key=row["content_key"], root_id=w.root,
                                rel_path="报价/报价单明细.xlsx", now=utc_now())
    else:
        w.db.execute("UPDATE tasks SET status = ? WHERE id = 't'", (change_task,))
    watch(w.db, now=at(0, minutes=1))
    assert w.db.query_one("SELECT status FROM relations WHERE id = ?", (row["id"],))["status"] == "cleared"
    assert file_id


def test_idle_round_writes_nothing(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    put_file(w.db, w.root, "报价/报价单明细.xlsx", at(-1))
    watch(w.db)
    rev = w.db.query_one("SELECT value FROM app_state WHERE key = 'graph_rev'")["value"]
    result = watch(w.db, now=at(0, minutes=5))
    assert result["written"] == 0 and result["cleared"] == 0
    assert w.db.query_one("SELECT value FROM app_state WHERE key = 'graph_rev'")["value"] == rev


def test_round_limit_by_task_count(tmp_path):
    w = world(tmp_path)
    w.db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p2', '第二个', 'x')")
    w.db.execute("INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p2', '/材料/第二个', 'x')")
    swept(w.db, int(w.db.query_one("SELECT id FROM project_material_roots WHERE project_id = 'p2'")["id"]))
    task(w.db, "t1", "整理报价单明细")
    task(w.db, "t2", "整理报价单明细", project_id="p2")
    first = watch(w.db, max_tasks=1)
    assert first["tried"] == 1 and first["after"] == "p"
    second = watch(w.db, max_tasks=1, after=first["after"])
    assert second["tried"] == 1 and second["after"] == ""


# ---------------------------------------------------------------------- 回答


def answer_setup(tmp_path):
    w = world(tmp_path)
    task(w.db, "t", "整理报价单明细")
    file_id = put_file(w.db, w.root, "报价/报价单明细.xlsx", at(-1))
    watch(w.db)
    return w, file_id, int(asked(w.db)[0]["id"])


def answer(db, relation_id: int, choice: str):
    with db.transaction() as connection:
        return relations.answer(connection, relation_id, {"answer": choice}, utc_now())


def undo(db, relation_id: int):
    with db.transaction() as connection:
        return relations.undo(connection, relation_id, utc_now())


def test_yes_registers_in_one_transaction_and_undo_removes_it(tmp_path):
    w, _file_id, relation_id = answer_setup(tmp_path)
    result = answer(w.db, relation_id, "yes")
    deliverable = w.db.query_one("SELECT * FROM deliverables WHERE id = ?", (result["deliverable_id"],))
    assert deliverable["url"] == f"{ROOT}/报价/报价单明细.xlsx" and deliverable["kind"] == "file"
    assert w.db.query_one("SELECT deliverable_id FROM relations WHERE id = ?", (relation_id,))["deliverable_id"] == deliverable["id"]
    # 任务没被改成完成
    assert w.db.query_one("SELECT status FROM tasks WHERE id = 't'")["status"] == "confirmed"
    undone = undo(w.db, relation_id)
    assert undone["removed_deliverable_id"] == deliverable["id"]
    assert w.db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 0
    assert w.db.query_one("SELECT status FROM relations WHERE id = ?", (relation_id,))["status"] == "suggested"


def test_yes_on_an_existing_deliverable_of_this_task_links_only(tmp_path):
    w, _file_id, relation_id = answer_setup(tmp_path)
    key = w.db.query_one("SELECT content_key FROM relations WHERE id = ?", (relation_id,))["content_key"]
    with w.db.transaction() as connection:
        existing = _insert_deliverable(connection, "t", name="报价单明细.xlsx", content_key=key, root_id=w.root,
                                       rel_path="报价/报价单明细.xlsx", now=utc_now())
    assert answer(w.db, relation_id, "yes")["deliverable_id"] == existing
    undo(w.db, relation_id)
    assert w.db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 1


def test_undo_rolls_back_when_removing_fails_and_survives_a_drawer_removal(tmp_path, monkeypatch):
    w, _file_id, relation_id = answer_setup(tmp_path)
    deliverable_id = answer(w.db, relation_id, "yes")["deliverable_id"]

    def broken(*_args, **_kwargs):
        raise RuntimeError("删不掉")

    monkeypatch.setattr(relations, "_delete_deliverable", broken)
    with pytest.raises(RuntimeError):
        undo(w.db, relation_id)
    row = w.db.query_one("SELECT status, deliverable_id FROM relations WHERE id = ?", (relation_id,))
    assert row == {"status": "confirmed", "deliverable_id": deliverable_id}
    monkeypatch.undo()
    # 交付物已经在任务抽屉里删掉：撤销照样成功，只改回 suggested
    with w.db.transaction() as connection:
        _delete_deliverable(connection, "t", deliverable_id, utc_now())
    result = undo(w.db, relation_id)
    assert result["removed_deliverable_id"] is None and result["relation"]["status"] == "suggested"


def test_yes_on_a_cancelled_task_or_a_gone_file(tmp_path):
    w, file_id, relation_id = answer_setup(tmp_path)
    w.db.execute("UPDATE tasks SET status = 'cancelled' WHERE id = 't'")
    with pytest.raises(RelationError) as cancelled:
        answer(w.db, relation_id, "yes")
    assert (cancelled.value.status, str(cancelled.value)) == (409, "这条任务已经取消了，先恢复任务再登记")
    w.db.execute("UPDATE tasks SET status = 'in_progress' WHERE id = 't'")
    w.db.execute("UPDATE material_files SET gone_at = ? WHERE id = ?", (utc_now(), file_id))
    with pytest.raises(RelationError) as gone:
        answer(w.db, relation_id, "yes")
    assert (gone.value.status, str(gone.value)) == (422, "这份文件已经不在了")
    assert w.db.query_one("SELECT COUNT(*) AS n FROM deliverables")["n"] == 0


def test_no_is_never_asked_again(tmp_path):
    w, _file_id, relation_id = answer_setup(tmp_path)
    answer(w.db, relation_id, "no")
    watch(w.db, now=at(0, minutes=5))
    assert asked(w.db) == []
    assert w.db.query_one("SELECT status FROM relations WHERE id = ?", (relation_id,))["status"] == "rejected"


def test_after_yes_the_confirmation_can_no_longer_be_undone(tmp_path):
    w = world(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "workbench.sqlite3",
        archive_root=tmp_path / "archive", staging_root=tmp_path / "staging",
    )
    service = TaskService(w.db, settings)
    task(w.db, "t", "整理报价单明细", status="pending_confirm", events=())
    service.confirm_task("t")
    confirmed_at = w.db.query_one("SELECT created_at FROM task_events WHERE task_id = 't' AND kind = 'confirmed'")
    put_file(w.db, w.root, "报价/报价单明细.xlsx", datetime.fromisoformat(confirmed_at["created_at"]) + timedelta(seconds=1))
    now = datetime.now(UTC) + timedelta(seconds=5)
    produced.watch(w.db, now, 5.0, since=stamp(now))
    relation_id = int(asked(w.db)[0]["id"])
    answer(w.db, relation_id, "yes")
    result = service.undo_review(["t"])
    assert result["reverted"] == [] and result["failed"][0]["error"] == "只能撤销刚刚的确认或驳回"


def test_questions_for_the_task_drawer(tmp_path):
    w, _file_id, relation_id = answer_setup(tmp_path)
    with w.db.autocommit() as connection:
        questions = task_questions(connection, "t")
    assert [item["relation_id"] for item in questions] == [relation_id]
    assert questions[0]["ask"] == "是任务『整理报价单明细』的交付物吗？"


# ---------------------------------------------------------------------- 证据的字


@pytest.mark.parametrize(
    ("evidence_json", "text"),
    [
        ({"event_kind": "added", "scope": "folder", "folder": "能耗看板/", "words": [], "ref": "meeting", "days": 3},
         "会后 3 天新增在『能耗看板/』"),
        ({"event_kind": "added", "scope": "word", "folder": None, "words": ["能耗看板"], "ref": "meeting", "days": 3},
         "会后 3 天新增，文件名里也有『能耗看板』"),
        ({"event_kind": "added", "scope": "word", "folder": "能耗看板/", "words": ["能耗看板"], "ref": "meeting", "days": 3},
         "会后 3 天新增在『能耗看板/』"),
        ({"event_kind": "changed", "scope": "word", "folder": None, "words": ["报价单"], "ref": "meeting", "days": 3},
         "会后 3 天改过，文件名里也有『报价单』"),
        ({"event_kind": "added", "scope": "folder", "folder": "能耗看板/", "words": [], "ref": "confirm", "days": 3},
         "任务确认后 3 天新增在『能耗看板/』"),
        ({"event_kind": "added", "scope": "folder", "folder": "能耗看板/", "words": [], "ref": "meeting", "days": 0},
         "会后当天新增在『能耗看板/』"),
        ({"event_kind": "added", "scope": "folder", "folder": "能耗看板/", "words": [], "ref": "confirm", "days": 0},
         "确认当天新增在『能耗看板/』"),
    ],
)
def test_evidence_texts(evidence_json, text):
    assert produced_evidence_text(evidence_json) == text


def test_evidence_counts_days_after_the_meeting(tmp_path):
    w = world(tmp_path)
    meeting(w.db, "m", at(-4))
    requirement(w.db, "r", "能耗看板", f"{ROOT}/能耗看板")
    task(w.db, "t", "写方案", meeting_id="m", requirement_id="r")
    put_file(w.db, w.root, "能耗看板/方案.docx", at(-1))
    watch(w.db)
    row = asked(w.db)[0]
    assert evidence(row) == {
        "event_id": evidence(row)["event_id"],
        "event_kind": "added",
        "event_day": at(-1).astimezone().date().isoformat(),
        "scope": "folder",
        "folder": "能耗看板/",
        "words": [],
        "ref": "meeting",
        "days": 3,
    }
    assert produced_evidence_text(evidence(row)) == "会后 3 天新增在『能耗看板/』"
    assert row["meeting_id"] is None and row["quote"] == ""
