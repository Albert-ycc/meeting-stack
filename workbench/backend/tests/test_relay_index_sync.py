"""语义索引同步 relay 的 index 子状态：一轮同步起的 relayctl 子进程数不随会议数增长。

假 relayctl 记下每次调用的参数，按真 relayctl 的 list（含 --job-id、500 行上限）、status、
set-substate 契约回应，接在真的 RelayClient 上，从 POST /api/admin/semantic/rebuild 触发同步。
生产 169 场有任务号的会，原来每轮同步（刷新前后各一次）起 338 个 relayctl status 子进程，约 20 秒，
而扫描间隔只有 15 秒。
"""

import json
import stat
import sys
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_workbench import relay_client as relay_client_module
from meeting_workbench.config import Settings
from meeting_workbench.db import Database, utc_now
from meeting_workbench.main import create_app

FAKE_RELAYCTL = r"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
CALLS = HERE / "calls.jsonl"
LIST_CAP = 500  # 真 relayctl list 单次最多返回的行数

argv = sys.argv[1:]
with CALLS.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv, ensure_ascii=False) + "\n")
state = json.loads(STATE.read_text(encoding="utf-8"))
command = argv[0]


def values(flag):
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag and i + 1 < len(argv)]


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(2)


if command == "list":
    if values("--status"):
        fail("假 relayctl 没做 --status")
    rows = [{"job_id": key, **value} for key, value in state["jobs"].items()]
    rows.sort(key=lambda row: (row["updated_at"], row["job_id"]), reverse=True)
    wanted = values("--job-id")
    if wanted:
        rows = [row for row in rows if row["job_id"] in set(wanted)]
    else:
        limit = int((values("--limit") or ["100"])[0])
        rows = rows[: min(limit, LIST_CAP)]
    if state.get("list_fails"):
        fail("list 失败")
    print(json.dumps(rows, ensure_ascii=False))
elif command == "status":
    job = state["jobs"].get(argv[1])
    if job is None:
        fail("任务不存在")
    print(json.dumps({"job_id": argv[1], **job, "substates": {"index": {"status": job["index_status"]}}}))
elif command == "set-substate":
    job = state["jobs"].get(argv[1])
    if job is None:
        fail("任务不存在")
    if job.get("reject_set"):
        fail("迟到的 index 子状态")
    if int(values("--attempt")[0]) != job["current_attempt"]:
        fail("迟到的 index 子状态")
    state["clock"] += 1
    job["index_status"] = values("--status")[0]
    job["updated_at"] = "2099-01-01T00:00:%06d+00:00" % state["clock"]
    STATE.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({"job_id": argv[1], "substates": {"index": {"status": job["index_status"]}}}))
else:
    fail("假 relayctl 不认识 " + command)
"""


class FakeRelay:
    def __init__(self, repo: Path):
        self.repo = repo
        quickstart = repo / "quickstart"
        quickstart.mkdir(parents=True)
        (quickstart / "fake_relayctl.py").write_text(FAKE_RELAYCTL, encoding="utf-8")
        wrapper = quickstart / "relayctl"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{quickstart / "fake_relayctl.py"}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        self.state_path = quickstart / "state.json"
        self.calls_path = quickstart / "calls.jsonl"
        self.state = {"jobs": {}, "clock": 0}

    def add_job(self, job_id: str, index_status: str, *, attempt: int = 1, age: int = 0, **extra):
        self.state["jobs"][job_id] = {
            "status": "published",
            "current_attempt": attempt,
            "index_status": index_status,
            "updated_at": f"2026-10-01T00:{age // 60:02d}:{age % 60:02d}+00:00",
            **extra,
        }

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def load(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def index_status(self, job_id: str) -> str:
        return self.load()["jobs"][job_id]["index_status"]

    def break_externally(self, job_id: str, index_status: str) -> None:
        """模拟 relay 那边被别的动作改掉了（比如子状态重试把 index 重置成 pending）。"""
        state = self.load()
        state["jobs"][job_id]["index_status"] = index_status
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text("utf-8").splitlines()]

    def reset_calls(self) -> None:
        self.calls_path.unlink(missing_ok=True)

    def counts(self) -> Counter:
        return Counter(call[0] for call in self.calls())


def job_id_of(index: int) -> str:
    return f"job-{index:016x}"


def build(tmp_path, monkeypatch, *, ready: int, pending: int, empty: int = 0):
    """造 ready 场「向量齐全」、pending 场「还缺向量」、empty 场「没有逐字稿」的会，
    每场一个任务号；返回 (client, fake_relay, ready_ids, pending_ids, empty_ids, headers)。"""
    relay = FakeRelay(tmp_path / "relay")
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay.repo,
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
    )
    app = create_app(settings)
    monkeypatch.setattr(app.state.semantic, "rebuild", lambda **_kwargs: 0)
    db = Database(settings.database_path)
    ready_ids = [job_id_of(i) for i in range(ready)]
    pending_ids = [job_id_of(ready + i) for i in range(pending)]
    empty_ids = [job_id_of(ready + pending + i) for i in range(empty)]
    with db.transaction() as connection:
        for index, job_id in enumerate([*ready_ids, *pending_ids, *empty_ids]):
            meeting_id = f"vm-sync-{index:05d}"
            connection.execute(
                "INSERT INTO meetings(id, title, status, source_job_id) "
                "VALUES (?, ?, 'published', ?)",
                (meeting_id, f"第{index}场", job_id),
            )
            if job_id in empty_ids:
                continue
            version = Database.create_transcript_version_with_connection(
                connection, meeting_id, "funasr", published=True
            )
            segment_id = f"seg-{meeting_id}"
            Database.replace_segments_with_connection(
                connection,
                version,
                meeting_id,
                [{"id": segment_id, "start_ms": 0, "end_ms": 1000, "text": "测试"}],
            )
            if job_id in ready_ids:
                connection.execute(
                    "INSERT INTO embeddings(segment_id, model, dimensions, vector, created_at) "
                    "VALUES (?, ?, 3, ?, ?)",
                    (segment_id, settings.semantic_model, b"\x00" * 12, utc_now()),
                )
    client = TestClient(app)
    token = client.get("/api/bootstrap").json()["csrf_token"]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": token}
    return client, relay, ready_ids, pending_ids, empty_ids, headers


def sync_once(client, headers) -> None:
    response = client.post("/api/admin/semantic/rebuild", json={}, headers=headers)
    assert response.status_code == 200, response.text


def test_round_with_nothing_to_change_costs_one_list_not_one_status_per_meeting(
    tmp_path, monkeypatch
):
    client, relay, ready_ids, pending_ids, empty_ids, headers = build(
        tmp_path, monkeypatch, ready=60, pending=50, empty=10
    )
    for job_id in ready_ids:
        relay.add_job(job_id, "ready")
    for job_id in [*pending_ids, *empty_ids]:
        relay.add_job(job_id, "pending")
    relay.save()

    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 1})
    listed = relay.calls()[0]
    assert listed[:2] == ["list", "--json"]
    assert sorted(listed[i + 1] for i, item in enumerate(listed) if item == "--job-id") == sorted(
        [*ready_ids, *pending_ids, *empty_ids]
    )


def test_only_jobs_whose_index_status_differs_get_a_write(tmp_path, monkeypatch):
    client, relay, ready_ids, pending_ids, empty_ids, headers = build(
        tmp_path, monkeypatch, ready=40, pending=30, empty=3
    )
    wrong_ready = ready_ids[:4]
    wrong_pending = [pending_ids[0], empty_ids[0]]
    for job_id in ready_ids:
        relay.add_job(job_id, "absent" if job_id in wrong_ready else "ready", attempt=3)
    for job_id in [*pending_ids, *empty_ids]:
        relay.add_job(job_id, "ready" if job_id in wrong_pending else "pending", attempt=2)
    relay.save()

    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 1, "set-substate": 6})
    for job_id in wrong_ready:
        assert relay.index_status(job_id) == "ready"
    for job_id in wrong_pending:
        assert relay.index_status(job_id) == "pending"
    # 写的时候带上列表里读到的 attempt，relay 那边靠它挡迟到的回写
    sets = [call for call in relay.calls() if call[0] == "set-substate"]
    attempts = {call[1]: call[call.index("--attempt") + 1] for call in sets}
    assert attempts[ready_ids[0]] == "3"
    assert attempts[pending_ids[0]] == "2"
    assert all(call[call.index("--name") + 1] == "index" for call in sets)

    relay.reset_calls()
    sync_once(client, headers)
    assert relay.counts() == Counter({"list": 1})


def test_relay_side_change_is_corrected_on_the_next_round(tmp_path, monkeypatch):
    """记住「上次同步过」不行：relay 那边的子状态会被别的动作改掉，下一轮要能纠正回来。"""
    client, relay, ready_ids, pending_ids, _empty, headers = build(
        tmp_path, monkeypatch, ready=5, pending=5
    )
    for job_id in ready_ids:
        relay.add_job(job_id, "ready")
    for job_id in pending_ids:
        relay.add_job(job_id, "pending")
    relay.save()
    sync_once(client, headers)
    relay.reset_calls()

    relay.break_externally(ready_ids[2], "pending")
    relay.break_externally(pending_ids[1], "failed")
    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 1, "set-substate": 2})
    assert relay.index_status(ready_ids[2]) == "ready"
    assert relay.index_status(pending_ids[1]) == "pending"


def test_meetings_beyond_the_list_row_cap_are_still_synced(tmp_path, monkeypatch):
    """relayctl list 单次最多 500 行、按更新时间倒序：任务比 500 多时，较早的会只靠默认 list 取不到。"""
    client, relay, ready_ids, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=520, pending=0
    )
    for age, job_id in enumerate(ready_ids):
        relay.add_job(job_id, "ready", age=age)
    oldest = ready_ids[:3]
    for job_id in oldest:
        relay.state["jobs"][job_id]["index_status"] = "absent"
    relay.save()

    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 1, "set-substate": 3})
    assert [relay.index_status(job_id) for job_id in oldest] == ["ready"] * 3


def test_job_ids_are_asked_in_batches_so_one_call_never_has_an_unbounded_argv(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(relay_client_module, "JOB_IDS_PER_LIST", 40)
    client, relay, ready_ids, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=100, pending=0
    )
    for job_id in ready_ids:
        relay.add_job(job_id, "ready")
    relay.state["jobs"][ready_ids[99]]["index_status"] = "absent"
    relay.save()

    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 3, "set-substate": 1})
    asked = [
        [item for item in call if item.startswith("job-")]
        for call in relay.calls()
        if call[0] == "list"
    ]
    assert [len(ids) for ids in asked] == [40, 40, 20]
    assert relay.index_status(ready_ids[99]) == "ready"


def test_a_rejected_write_does_not_stop_the_other_jobs(tmp_path, monkeypatch):
    client, relay, ready_ids, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=4, pending=0
    )
    for job_id in ready_ids:
        relay.add_job(job_id, "absent")
    relay.state["jobs"][ready_ids[1]]["reject_set"] = True
    relay.save()

    sync_once(client, headers)

    assert [relay.index_status(job_id) for job_id in ready_ids] == [
        "ready",
        "absent",
        "ready",
        "ready",
    ]
    assert relay.counts() == Counter({"list": 1, "set-substate": 4})


def test_meetings_relay_does_not_know_are_skipped_quietly(tmp_path, monkeypatch):
    client, relay, ready_ids, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=3, pending=0
    )
    relay.add_job(ready_ids[0], "absent")
    relay.save()

    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 1, "set-substate": 1})
    assert relay.index_status(ready_ids[0]) == "ready"


def test_a_failing_list_makes_the_round_do_nothing_instead_of_falling_back_to_status(
    tmp_path, monkeypatch
):
    client, relay, ready_ids, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=6, pending=0
    )
    for job_id in ready_ids:
        relay.add_job(job_id, "absent")
    relay.state["list_fails"] = True
    relay.save()

    sync_once(client, headers)

    assert relay.counts() == Counter({"list": 1})


def test_missing_relayctl_does_not_break_the_rebuild_route(tmp_path, monkeypatch):
    client, relay, _ready, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=2, pending=0
    )
    (relay.repo / "quickstart" / "relayctl").unlink()

    sync_once(client, headers)


@pytest.mark.parametrize("bad", ["--help", "job-a b", ""])
def test_job_ids_that_cannot_exist_in_relay_are_not_sent(tmp_path, monkeypatch, bad):
    client, relay, ready_ids, _pending, _empty, headers = build(
        tmp_path, monkeypatch, ready=1, pending=0
    )
    relay.add_job(ready_ids[0], "absent")
    relay.save()
    app_db = Database(tmp_path / "data" / "db.sqlite3")
    app_db.execute(
        "INSERT INTO meetings(id, title, status, source_job_id) VALUES ('vm-odd', '怪任务号', 'published', ?)",
        (bad,),
    )

    sync_once(client, headers)

    sent = [item for call in relay.calls() for item in call]
    assert bad not in sent
    assert relay.index_status(ready_ids[0]) == "ready"
