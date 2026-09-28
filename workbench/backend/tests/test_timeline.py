"""第四期 4c：项目时间线（TZ=Asia/Shanghai）——按有动静的天翻页、会议和决议、任务确认和完成、交付物、
文件分组、记录开始前按修改时间、三种状态、语句数、时区。"""
import os
import time
from datetime import UTC, date, datetime

import pytest

from meeting_workbench import decisions, timeline
from meeting_workbench.db import Database

from .helpers import count_reads
from .test_decisions import set_minutes
from .test_graph import add_project, add_requirement

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)  # 本地 9 月 27 日 18:00


@pytest.fixture(autouse=True)
def shanghai():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def make(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    add_project(db, "p", "云图AI")
    db.execute("UPDATE app_state SET value = ? WHERE key = 'links_since'", ("2026-09-20T00:00:00+00:00",))
    return db


def meeting(db, meeting_id, recording_date, *, title=None, duration_ms=2_880_000, created_at="2026-09-01T00:00:00"):
    db.execute(
        """INSERT INTO meetings(id, title, recording_date, duration_ms, status, project_id, project_origin,
                                created_at, updated_at)
           VALUES (?, ?, ?, ?, 'completed_unreviewed', 'p', 'manual', ?, ?)""",
        (meeting_id, title or f"会{meeting_id}", recording_date, duration_ms, created_at, created_at),
    )


def task(db, task_id, status, *, origin="ai", created_at="2026-09-01T00:00:00+00:00", events=()):
    db.execute(
        """INSERT INTO tasks(id, title, status, origin, project_id, status_changed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'p', ?, ?, ?)""",
        (task_id, f"任务{task_id}", status, origin, created_at, created_at, created_at),
    )
    for kind, body, at in events:
        db.execute("INSERT INTO task_events(task_id, kind, body, created_at) VALUES (?, ?, ?, ?)",
                   (task_id, kind, body, at))


def build(db, **kwargs):
    kwargs.setdefault("now", NOW)
    with db.autocommit() as connection:
        return timeline.project_timeline(connection, "p", **kwargs)


def items(page, day):
    return next(entry["items"] for entry in page["days"] if entry["day"] == day)


# ---------------------------------------------------------------------- 翻页和会议


def test_pages_by_active_days(tmp_path):
    db = make(tmp_path)
    for meeting_id, when in (("a", "2026-09-27T14:30:00"), ("b", "2026-09-25T09:00:00"),
                             ("c", "2026-09-20T09:00:00"), ("d", "2026-09-10T09:00:00")):
        meeting(db, meeting_id, when)

    first = build(db, days=2)
    assert [(entry["day"], entry["label"]) for entry in first["days"]] == [
        ("2026-09-27", "今天"), ("2026-09-25", "9月25日 周五"),
    ]
    assert first["next_before"] == "2026-09-25"
    second = build(db, days=2, before=date(2026, 9, 25))
    assert [entry["day"] for entry in second["days"]] == ["2026-09-20", "2026-09-10"]
    assert second["next_before"] is None
    (item,) = items(first, "2026-09-27")
    assert item["type"] == "meeting" and item["time"] == "14:30"
    assert item["meeting"] == {"id": "a", "title": "会a", "duration_sec": 2880, "audio_url": None}
    # 昨天和不是今年的
    assert timeline.day_label(date(2026, 9, 26), date(2026, 9, 27)) == "昨天"
    assert timeline.day_label(date(2025, 12, 30), date(2026, 9, 27)) == "2025年12月30日 周二"


def test_days_are_clamped(tmp_path):
    db = make(tmp_path)
    for index in range(40):
        meeting(db, f"m{index}", f"2026-08-{(index % 28) + 1:02d}T09:00:00" if index < 28 else f"2026-07-{index - 27:02d}T09:00:00")
    assert len(build(db, days=0)["days"]) == 1
    assert len(build(db, days=99)["days"]) == 31


def test_timezones_of_meeting_dates(tmp_path):
    db = make(tmp_path)
    meeting(db, "utc", "2026-09-26T20:00:00+00:00")  # 本地 9 月 27 日 04:00
    meeting(db, "naive", "2026-09-26T20:00:00")  # 没带时区：按本机
    meeting(db, "created", None, created_at="2026-09-25T20:00:00")  # created_at 没带时区：按 UTC
    page = build(db)
    assert [item["meeting"]["id"] for item in items(page, "2026-09-27")] == ["utc"]
    assert items(page, "2026-09-27")[0]["time"] == "04:00"
    assert [item["time"] for item in items(page, "2026-09-26")] == ["04:00", "20:00"]


def test_meeting_carries_up_to_eight_decisions_with_later(tmp_path):
    db = make(tmp_path)
    lines = "".join(f"{index + 1}. 第{index + 1}条决定的内容\n" for index in range(10))
    meeting(db, "a", "2026-09-20T09:00:00")
    set_minutes(db, "a", "mv-a", f"# 周会\n\n## 决议\n{lines}", kind="generated")
    meeting(db, "b", "2026-09-25T09:00:00", title="周会乙")
    set_minutes(db, "b", "mv-b", "# 周会\n\n## 决议\n1. 第一条改成别的\n", kind="generated")
    decisions.ingest_pending(db, now=NOW)
    early = db.query_one("SELECT id FROM decisions WHERE meeting_id = 'a' AND ordinal = 0")["id"]
    late = db.query_one("SELECT id FROM decisions WHERE meeting_id = 'b'")["id"]
    db.execute(
        """INSERT INTO relations(kind, project_id, ident, status, origin, meeting_id, decision_id, to_decision_id,
                                 quote, evidence_json, created_at, updated_at)
           VALUES ('later_changed', 'p', ?, 'shown', 'llm', 'b', ?, ?, '改成别的', '{}', ?, ?)""",
        (f"{early}|{late}", early, late, NOW.isoformat(), NOW.isoformat()),
    )
    (item,) = items(build(db), "2026-09-20")
    assert len(item["decisions"]) == 8 and item["decisions_more"] == 2
    assert item["decisions"][0]["later"] == {"date": "2026-09-25", "text": "第一条改成别的"}
    assert item["decisions"][1]["later"] is None


# ---------------------------------------------------------------------- 任务和交付物


def test_confirmed_takes_the_earliest_of_three_sources_and_only_still_confirmed(tmp_path):
    db = make(tmp_path)
    task(db, "a", "confirmed", events=[
        ("status_changed", "pending_confirm → confirmed", "2026-09-22T02:00:00+00:00"),
        ("confirmed", "", "2026-09-20T02:00:00+00:00"),
    ])
    task(db, "manual", "in_progress", origin="manual", created_at="2026-09-21T02:00:00+00:00",
         events=[("created", "", "2026-09-21T02:00:00+00:00")])
    task(db, "gone", "cancelled", events=[("confirmed", "", "2026-09-21T03:00:00+00:00")])
    task(db, "draft", "pending_confirm")
    page = build(db, kind="tasks")
    assert [entry["day"] for entry in page["days"]] == ["2026-09-21", "2026-09-20"]
    (confirmed,) = items(page, "2026-09-20")
    assert (confirmed["type"], confirmed["event"], confirmed["time"]) == ("tasks", "confirmed", "10:00")
    assert confirmed["tasks"] == [{"id": "a", "title": "任务a"}]
    assert [item["tasks"][0]["id"] for item in items(page, "2026-09-21")] == ["manual"]


def test_done_only_when_still_done_and_three_merge(tmp_path):
    db = make(tmp_path)
    for index in range(4):
        task(db, f"d{index}", "done", events=[("status_changed", "confirmed → done", f"2026-09-23T0{index}:00:00+00:00")])
    task(db, "back", "in_progress", events=[("status_changed", "confirmed → done", "2026-09-23T05:00:00+00:00"),
                                            ("status_changed", "done → in_progress", "2026-09-24T05:00:00+00:00")])
    (item,) = [entry for entry in items(build(db, kind="tasks"), "2026-09-23") if entry["event"] == "done"]
    assert len(item["tasks"]) == 3 and item["more"] == 1


def test_deliverable_names(tmp_path):
    db = make(tmp_path)
    task(db, "t", "confirmed", events=[("confirmed", "", "2026-09-24T01:00:00+00:00")])
    db.execute("""INSERT INTO deliverables(task_id, kind, url, title, created_at)
                  VALUES ('t', 'file', '/盘/能耗看板/报价单_v3.xlsx', '', '2026-09-24T08:10:00+00:00')""")
    entries = items(build(db, kind="tasks"), "2026-09-24")
    (deliverable,) = [entry for entry in entries if entry["type"] == "deliverable"]
    assert deliverable["deliverable"]["name"] == "报价单_v3.xlsx" and deliverable["time"] == "16:10"
    assert deliverable["task"] == {"id": "t", "title": "任务t"}


# ---------------------------------------------------------------------- 文件


def root(db, *, last_full_at="2026-09-19T00:00:00+00:00", state="done", created_at="2026-09-01T00:00:00+00:00"):
    db.execute("INSERT INTO project_material_roots(project_id, path, created_at) VALUES ('p', '/盘/云图AI', ?)",
               (created_at,))
    root_id = db.query_one("SELECT MAX(id) AS id FROM project_material_roots")["id"]
    db.execute("INSERT INTO material_index_state(root_id, state, last_full_at) VALUES (?, ?, NULL)", (root_id, state))
    return root_id


def add_file(db, root_id, rel_path, *, size=10, mtime=None, zone="normal", gone_at=None):
    dir_rel, _, name = rel_path.rpartition("/")
    stem, _, ext = name.rpartition(".")
    mtime_ns = int((mtime or NOW).timestamp()) * 1_000_000_000
    db.execute(
        """INSERT INTO material_files(root_id, rel_path, dir_rel, name, stem, stem_key, ext, size, mtime_ns, zone,
                                      seen_at, gone_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (root_id, rel_path, dir_rel, name, stem, stem, ext, size, mtime_ns, zone, NOW.isoformat(), gone_at),
    )
    return db.query_one("SELECT id FROM material_files WHERE rel_path = ?", (rel_path,))["id"]


def event(db, root_id, file_id, rel_path, kind, day, *, size=10, mtime_ns=None, at=None):
    db.execute(
        """INSERT INTO material_file_events(root_id, file_id, rel_path, dir_rel, kind, content_key, size, mtime_ns,
                                            day, at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
        (root_id, file_id, rel_path, rel_path.rpartition("/")[0], kind, size, mtime_ns, day,
         at or f"{day}T08:00:00.000Z"),
    )


def finish_first_pass(db, root_id):
    db.execute("UPDATE material_index_state SET last_full_at = '2026-09-19T00:00:00+00:00' WHERE root_id = ?",
               (root_id,))


def test_file_groups_names_cap_and_moves(tmp_path):
    db = make(tmp_path)
    root_id = root(db)
    for index in range(10):
        folder = f"资料/能耗看板{index}"
        for number in range(1 if index else 5):
            path = f"{folder}/报价单{number}.xlsx"
            event(db, root_id, add_file(db, root_id, path, size=100 + index * 10 + number), path, "added", "2026-09-26")
    changed = "资料/能耗看板0/排期表.xlsx"
    event(db, root_id, add_file(db, root_id, changed, size=7), changed, "changed", "2026-09-26")
    # 挪过来的：同项目前后 2 天有大小和修改时间相同的 gone
    old = add_file(db, root_id, "旧/计划.docx", size=55, mtime=NOW, gone_at=NOW.isoformat())
    new = add_file(db, root_id, "新/计划.docx", size=55, mtime=NOW)
    mtime_ns = int(NOW.timestamp()) * 1_000_000_000
    event(db, root_id, old, "旧/计划.docx", "gone", "2026-09-26", size=55, mtime_ns=mtime_ns)
    event(db, root_id, new, "新/计划.docx", "added", "2026-09-26", size=55, mtime_ns=mtime_ns)
    finish_first_pass(db, root_id)

    page = build(db, kind="files")
    day = next(entry for entry in page["days"] if entry["day"] == "2026-09-26")
    assert len(day["items"]) == 8 and day["more_dirs"] == 2
    first = next(item for item in day["items"] if item["folder"] == "能耗看板0")
    assert (first["added"], first["changed"], len(first["names"])) == (5, 1, 3)
    assert all(item["folder"] != "新" for item in day["items"])
    # 界面上不出现路径：文件夹只给最后一段
    assert all("/" not in item["folder"] for item in day["items"])


def test_prelog_files_by_modification_time_and_code_zone_is_hidden(tmp_path):
    db = make(tmp_path)
    root_id = root(db)
    add_file(db, root_id, "能耗看板/方案.docx", mtime=datetime(2026, 9, 10, 4, 0, tzinfo=UTC))
    add_file(db, root_id, "能耗看板/清单.xlsx", mtime=datetime(2026, 9, 10, 5, 0, tzinfo=UTC))
    add_file(db, root_id, "代码/main.py", mtime=datetime(2026, 9, 10, 5, 0, tzinfo=UTC), zone="code")
    add_file(db, root_id, "很早/旧.docx", mtime=datetime(2026, 6, 1, 5, 0, tzinfo=UTC))  # 分界之前 62 天以外
    finish_first_pass(db, root_id)

    page = build(db, kind="files", days=5)
    assert page["file_log_since"] == "2026-09-20"
    assert [entry["day"] for entry in page["days"]] == ["2026-09-10"]
    (item,) = items(page, "2026-09-10")
    assert item["prelog"] is True and item["count"] == 2 and item["folder"] == "能耗看板"
    assert sorted(item["names"]) == ["方案.docx", "清单.xlsx"]


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("none", ("stopped", "no_roots", timeline.NO_ROOTS, "attach_root")),
        ("offline", ("waiting", "offline", timeline.ROOT_OFFLINE, None)),
        ("first", ("waiting", "first_pass", timeline.FIRST_PASS, None)),
        ("ok", ("ok", None, None, None)),
    ],
)
def test_states(tmp_path, setup, expected):
    db = make(tmp_path)
    if setup != "none":
        root_id = root(db, state="offline" if setup == "offline" else "done")
        if setup in ("ok", "offline"):
            finish_first_pass(db, root_id)
    state = build(db)["state"]
    assert (state["kind"], state["reason"], state["text"], (state["action"] or {}).get("kind")) == expected


# ---------------------------------------------------------------------- ［决议］


def test_decisions_filter_tags_requirements(tmp_path):
    db = make(tmp_path)
    add_requirement(db, "r1", "p", "初审规则 V2")
    add_requirement(db, "r2", "p", "驻场排班")
    meeting(db, "a", "2026-09-25T09:00:00")
    set_minutes(db, "a", "mv-a", "# 周会\n\n## 决议\n1. 驻场排班改成两班\n2. 下周起统一口径\n", kind="generated")
    meeting(db, "b", "2026-09-24T09:00:00")  # 没有决议的会不占一天
    for requirement_id in ("r1", "r2"):
        db.execute("INSERT INTO requirement_meetings(requirement_id, meeting_id, created_at) VALUES (?, 'a', ?)",
                   (requirement_id, NOW.isoformat()))
    decisions.ingest_pending(db, now=NOW)
    page = build(db, kind="decisions")
    assert [entry["day"] for entry in page["days"]] == ["2026-09-25"]
    first, second = items(page, "2026-09-25")
    assert first["type"] == "decision" and first["requirement"] == {"id": "r2", "title": "驻场排班", "how": "title"}
    assert second["requirement"] is None and second["how"] == "unplaced"
    assert second["linked_requirement_ids"] == ["r1", "r2"]
    assert [item["id"] for item in page["requirements"]] == ["r1", "r2"]
    assert page["state"]["kind"] in ("waiting", "stopped", "ok")


# ---------------------------------------------------------------------- 语句数


@pytest.mark.parametrize("count", [10, 200])
def test_timeline_is_at_most_ten_statements(tmp_path, count):
    db = make(tmp_path)
    root_id = root(db)
    for index in range(count):
        meeting(db, f"m{index:03d}", f"2026-{7 + index % 3:02d}-{index % 28 + 1:02d}T09:00:00")
        set_minutes(db, f"m{index:03d}", f"mv-{index}", f"# 周会\n\n## 决议\n1. 第{index}条\n", kind="generated")
        task(db, f"t{index}", "done", events=[("confirmed", "", f"2026-09-{index % 26 + 1:02d}T01:00:00+00:00"),
                                              ("status_changed", "confirmed → done",
                                               f"2026-09-{index % 26 + 1:02d}T05:00:00+00:00")])
    for index in range(30):
        path = f"能耗看板{index % 5}/文件{index}.docx"
        event(db, root_id, add_file(db, root_id, path, mtime=datetime(2026, 9, 5, tzinfo=UTC)), path, "added",
              f"2026-09-{index % 7 + 20:02d}")
    finish_first_pass(db, root_id)
    decisions.ingest_pending(db, now=NOW, max_meetings=count + 1, max_seconds=60)

    for kind in timeline.KINDS:
        reads = count_reads(db, lambda connection, kind=kind: timeline.project_timeline(
            connection, "p", kind=kind, now=NOW, days=31))
        assert reads <= 10, kind
    assert build(db, days=31)["days"]


# ---------------------------------------------------------------------- 接口


def test_endpoint(tmp_path):
    from .test_tasks_api import make_client

    client, settings = make_client(tmp_path)
    db = Database(settings.database_path)
    add_project(db, "p", "云图AI")
    assert client.get("/api/projects/p/timeline?days=99&kind=all").status_code == 200
    assert client.get("/api/projects/nope/timeline").json() == {"detail": "项目不存在"}
    assert client.get("/api/projects/p/timeline?before=9-27").status_code == 422
    assert client.get("/api/projects/p/timeline?kind=meetings").status_code == 422
