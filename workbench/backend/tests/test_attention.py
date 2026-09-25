"""「需要处理」：失败/归档未完成任务的说明、确认归档与健康判定（260914）。

背景：relay 里 5 个失败任务放了一个多月没有出口，3 个归档未完成让 relay 常驻 degraded，
健康灯永远是黄的；资料库筛「失败」是 0 场，隔离目录在界面上找不到任何提示。
"""
from fastapi.testclient import TestClient

from meeting_workbench.attention import (
    describe_job,
    describe_quarantine,
    failure_kind,
    needs_attention,
)
from meeting_workbench.config import Settings
from meeting_workbench.main import create_app
from meeting_workbench.relay_client import RelayUnavailable

from .test_tasks_api import write_headers


class AttentionRelay:
    def __init__(self):
        self.pending_archive_failures = 1
        self.jobs = [
            {
                "job_id": "job-transcribe",
                "status": "failed",
                "failure_stage": "transcribing",
                "last_error": "transcribe.sh failed",
                "created_at": "2026-08-01T04:07:19+00:00",
                "updated_at": "2026-08-06T15:54:52+00:00",
            },
            {
                "job_id": "job-archive",
                "status": "completed_unreviewed",
                "failure_stage": "pending_archive",
                "last_error": None,
                "created_at": "2026-09-10T01:00:00+00:00",
                "updated_at": "2026-09-10T02:00:00+00:00",
            },
            {
                "job_id": "job-ok",
                "status": "completed_unreviewed",
                "failure_stage": None,
                "created_at": "2026-09-11T01:00:00+00:00",
                "updated_at": "2026-09-11T02:00:00+00:00",
            },
        ]

    def health(self):
        return {
            "status": "degraded",
            "worker": {"state": "idle", "heartbeat_age_seconds": 1},
            "counts": {
                "queued": 0,
                "active": 0,
                "failed": 1,
                "pending_archive_failures": self.pending_archive_failures,
            },
        }

    def list_jobs(self, *, status=None, limit=200):
        return [dict(job) for job in self.jobs if status is None or job["status"] == status]

    def status(self, job_id):
        raise RelayUnavailable("no job")


def make_client(tmp_path, relay):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_jobs_db=tmp_path / "relay.sqlite3",
        semantic_enabled=False,
        lark_webhook_url="",
    )
    settings.archive_root.mkdir()
    settings.staging_root.mkdir()
    app = create_app(settings, relay)
    client = TestClient(app)
    app.state.relay_health = relay.health()
    return client, app


def test_needs_attention_and_kind_mapping():
    assert needs_attention({"status": "failed", "failure_stage": "transcribing"})
    assert needs_attention({"status": "completed_unreviewed", "failure_stage": "pending_archive"})
    assert not needs_attention({"status": "completed_unreviewed", "failure_stage": None})
    assert failure_kind({"failure_stage": "transcribing"}) == "transcription"
    assert failure_kind({"failure_stage": "transcript_ready"}) == "minutes"
    assert failure_kind({"failure_stage": "codex_callback"}) == "minutes"
    assert failure_kind({"failure_stage": "pending_archive"}) == "archive"
    assert failure_kind({"failure_stage": "something_new"}) == "other"


def test_minutes_failure_copy_depends_on_remaining_auto_recovery():
    job = {"job_id": "j", "status": "failed", "failure_stage": "codex_callback"}

    waiting = describe_job(job, auto_recovery_left=True)
    exhausted = describe_job(job, auto_recovery_left=False)

    assert "自动再试" in waiting["summary"]
    assert "重新生成纪要" in exhausted["next_step"]
    # 转写阶段失败不能说「纪要会自动重试」
    transcription = describe_job({"job_id": "t", "status": "failed", "failure_stage": "transcribing"})
    assert "重新转写" in transcription["next_step"]
    assert "自动" not in transcription["summary"]


def test_quarantine_next_step_follows_reason():
    missing = describe_quarantine(
        {"directory": "/archive/260914 明德会", "reason": "manifest 登记的纪要产物缺失"}
    )
    failed = describe_quarantine(
        {"directory": "/archive/260903 远山", "reason": "受管任务状态与预期不符：failed"}
    )

    assert missing["name"] == "260914 明德会"
    assert "缺文件" in missing["next_step"]
    assert "转写录音" in failed["next_step"]


def test_attention_lists_open_jobs_and_quarantined_directories(tmp_path):
    relay = AttentionRelay()
    client, app = make_client(tmp_path, relay)
    app.state.last_scan = {
        "quarantine_details": [
            {"directory": "/archive/260903 远山患者平台原型评审", "reason": "受管任务状态与预期不符：failed"}
        ]
    }

    payload = client.get("/api/attention").json()

    assert payload["jobs_available"] is True
    assert {job["job_id"] for job in payload["jobs"]} == {"job-transcribe", "job-archive"}
    kinds = {job["job_id"]: job["kind"] for job in payload["jobs"]}
    assert kinds == {"job-transcribe": "transcription", "job-archive": "archive"}
    assert payload["quarantined"][0]["name"] == "260903 远山患者平台原型评审"


def test_acknowledging_archive_failures_clears_relay_degraded(tmp_path):
    relay = AttentionRelay()
    client, app = make_client(tmp_path, relay)
    headers = write_headers(client)
    client.get("/api/attention")

    before = client.get("/api/health").json()
    assert before["services"]["relay"] == "degraded"
    assert before["counts"]["attention_jobs"] == 2
    assert before["counts"]["failed_jobs"] == 1
    assert before["details"]["attention"]["by_kind"]["archive"] == 1

    response = client.post("/api/jobs/job-archive/acknowledge", json={}, headers=headers)
    assert response.status_code == 200

    after = client.get("/api/health").json()
    assert after["services"]["relay"] == "healthy"
    assert after["services"]["relay_worker"] == "healthy"
    assert after["counts"]["attention_jobs"] == 1
    assert after["counts"]["acknowledged_jobs"] == 1
    assert {job["job_id"] for job in client.get("/api/attention").json()["jobs"]} == {"job-transcribe"}


def test_new_archive_failure_not_yet_in_snapshot_keeps_degraded(tmp_path):
    relay = AttentionRelay()
    client, app = make_client(tmp_path, relay)
    headers = write_headers(client)
    client.get("/api/attention")
    client.post("/api/jobs/job-archive/acknowledge", json={}, headers=headers)

    relay.pending_archive_failures = 2  # relay 已经多了一个，快照还没刷新
    app.state.relay_health = relay.health()

    assert client.get("/api/health").json()["services"]["relay"] == "degraded"


def test_acknowledged_job_reappears_after_it_changes(tmp_path):
    relay = AttentionRelay()
    client, app = make_client(tmp_path, relay)
    headers = write_headers(client)
    client.get("/api/attention")
    client.post("/api/jobs/job-transcribe/acknowledge", json={}, headers=headers)
    assert "job-transcribe" not in {job["job_id"] for job in client.get("/api/attention").json()["jobs"]}

    relay.jobs[0]["updated_at"] = "2026-09-14T09:00:00+00:00"  # 重试后又失败了
    app.state.attention_jobs = app.state.refresh_attention_jobs()

    assert "job-transcribe" in {job["job_id"] for job in client.get("/api/attention").json()["jobs"]}


def test_acknowledge_unknown_job_is_404(tmp_path):
    relay = AttentionRelay()
    client, _app = make_client(tmp_path, relay)
    headers = write_headers(client)

    response = client.post("/api/jobs/job-ok/acknowledge", json={}, headers=headers)

    assert response.status_code == 404


def test_health_without_snapshot_falls_back_to_relay_counts(tmp_path):
    relay = AttentionRelay()
    client, _app = make_client(tmp_path, relay)

    payload = client.get("/api/health").json()

    assert payload["counts"]["failed_jobs"] == 1
    assert payload["counts"]["attention_jobs"] is None
    assert payload["services"]["relay"] == "degraded"


def test_quarantine_of_a_listed_job_is_not_counted_twice(tmp_path):
    import json

    relay = AttentionRelay()
    client, app = make_client(tmp_path, relay)
    same_job = tmp_path / "archive" / "260801 转写失败那场"
    other_job = tmp_path / "archive" / "260912 另一场"
    for directory, job_id in ((same_job, "job-transcribe"), (other_job, "job-unrelated")):
        directory.mkdir(parents=True)
        (directory / "workbench-manifest.json").write_text(
            json.dumps({"schema_version": 1, "job_id": job_id, "attempt": 1}), encoding="utf-8"
        )
    app.state.last_scan = {
        "quarantine_details": [
            {"directory": str(same_job), "reason": "受管任务状态与预期不符：failed"},
            {"directory": str(other_job), "reason": "manifest 登记的纪要产物缺失"},
        ]
    }

    payload = client.get("/api/attention").json()

    assert [item["name"] for item in payload["quarantined"]] == ["260912 另一场"]


def test_acknowledging_unknown_job_does_not_hammer_relay(tmp_path):
    relay = AttentionRelay()
    calls = []
    original = relay.list_jobs

    def counting_list_jobs(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    relay.list_jobs = counting_list_jobs
    client, _app = make_client(tmp_path, relay)
    headers = write_headers(client)
    client.get("/api/attention")
    baseline = len(calls)

    statuses = [
        client.post(f"/api/jobs/forged-{index}/acknowledge", json={}, headers=headers).status_code
        for index in range(3)
    ]

    assert statuses == [404, 404, 404]
    assert len(calls) == baseline


def test_archive_failure_next_step_names_the_real_remedy():
    item = describe_job(
        {"job_id": "job-archive", "status": "completed_unreviewed", "failure_stage": "pending_archive"}
    )

    # 转写录音页没有「重试归档」按钮，下一步不能把人指到那里去
    assert "relayctl migrate-pending job-archive" in item["next_step"]
    assert "转写录音" not in item["next_step"]
