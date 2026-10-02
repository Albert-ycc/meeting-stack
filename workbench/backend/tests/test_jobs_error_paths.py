"""转写录音页把 relay 的 last_error 当「失败原因」显示。GET /api/jobs 和 GET /api/jobs/{job_id} 回给浏览器前，
把失败原因（以及子状态的 error）里的绝对路径换成文件名：Tailscale 远程打开时，页面上不能出现外置盘、
归档目录、暂存目录的真实路径。完整文本还在 relay 的任务库和日志里。"""

from meeting_workbench.db import Database
from meeting_workbench.relay_client import RelayUnavailable

from .test_jobs_api import make_client

ARCHIVE_ERROR = (
    "PermissionError: [Errno 1] Operation not permitted: "
    "'/Volumes/外置中枢/会议纪要与录音/260901 医米京东科研仓对接/vm-20260901-100000-a1a1a1a1.m4a'"
)
ARCHIVE_ERROR_SHOWN = (
    "PermissionError: [Errno 1] Operation not permitted: 'vm-20260901-100000-a1a1a1a1.m4a'"
)
MOVE_ERROR = (
    "OSError: [Errno 18] Cross-device link: "
    "'/Users/albert/Movies/iphone-relay-products/260901 会议/vm.m4a' -> "
    "'/Volumes/外置中枢/会议纪要与录音/260901 会议/vm.m4a'"
)
MOVE_ERROR_SHOWN = "OSError: [Errno 18] Cross-device link: 'vm.m4a' -> 'vm.m4a'"


def listed(**fields):
    return {
        "job_id": "job-1",
        "status": "completed_unreviewed",
        "created_at": "2026-09-01T10:00:00+08:00",
        "updated_at": "2026-09-01T10:05:00+08:00",
        **fields,
    }


def test_list_shows_the_failure_reason_without_directories_and_keeps_the_rest(tmp_path):
    client, relay = make_client(tmp_path)
    relay.list_jobs = lambda *, status=None, limit=200: [
        listed(
            failure_stage="pending_archive",
            last_error=ARCHIVE_ERROR,
            published_archive_dir="/Volumes/外置中枢/会议纪要与录音/260901 医米京东科研仓对接",
        ),
        listed(
            job_id="job-2", status="failed", failure_stage="transcribing", last_error=MOVE_ERROR
        ),
        listed(
            job_id="job-3",
            status="failed",
            failure_stage="stabilizing",
            last_error="audio did not stabilize",
        ),
        listed(job_id="job-4"),
    ]

    items = client.get("/api/jobs").json()["items"]

    assert [item.get("last_error") for item in items] == [
        ARCHIVE_ERROR_SHOWN,
        MOVE_ERROR_SHOWN,
        "audio did not stabilize",
        None,
    ]
    assert items[0]["failure_stage"] == "pending_archive"
    # 页面没显示的字段不在这条的范围里，原样回
    assert (
        items[0]["published_archive_dir"]
        == "/Volumes/外置中枢/会议纪要与录音/260901 医米京东科研仓对接"
    )
    assert "外置中枢" not in str([item.get("last_error") for item in items])


def test_detail_redacts_last_error_and_substate_errors_but_not_other_fields(tmp_path):
    client, relay = make_client(tmp_path)
    Database(client.app.state.settings.database_path).execute(
        "INSERT INTO meetings(id, title, status, source_job_id) "
        "VALUES ('vm-1', '医米京东科研仓对接', 'completed_unreviewed', 'job-1')"
    )
    relay.status = lambda job_id: {
        "job_id": job_id,
        "status": "failed",
        "failure_stage": "transcribing",
        "last_error": MOVE_ERROR,
        "audio_path": "/Users/albert/Movies/iphone-relay-products/vm.m4a",
        "substates": {
            "whisper": {
                "status": "failed",
                "error": "failed to read /Users/albert/Movies/x/a b/vm.m4a",
            },
            "index": {"status": "ready", "error": None},
        },
        "attempts": [],
        "events": [],
    }

    job = client.get("/api/jobs/job-1").json()

    assert job["last_error"] == MOVE_ERROR_SHOWN
    assert job["substates"]["whisper"]["error"] == "failed to read vm.m4a"
    assert job["substates"]["index"] == {"status": "ready", "error": None}
    # 会议信息照常补上；前端没显示的 audio_path 不动
    assert (job["meeting_id"], job["meeting_title"]) == ("vm-1", "医米京东科研仓对接")
    assert job["audio_path"] == "/Users/albert/Movies/iphone-relay-products/vm.m4a"


def test_relay_unavailable_message_shown_on_the_page_has_no_directories_either(tmp_path):
    client, relay = make_client(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise RelayUnavailable(
            "relayctl 不存在：/Users/albert/workspace/meeting-stack/relay/quickstart/relayctl"
        )

    relay.list_jobs = unavailable
    relay.status = unavailable

    listing = client.get("/api/jobs")
    detail = client.get("/api/jobs/job-1")

    assert listing.status_code == 503 and detail.status_code == 503
    assert listing.json() == detail.json() == {"detail": "relayctl 不存在：relayctl"}


def test_the_relay_objects_are_not_changed_in_place(tmp_path):
    client, relay = make_client(tmp_path)
    original = listed(failure_stage="pending_archive", last_error=ARCHIVE_ERROR)
    relay.list_jobs = lambda *, status=None, limit=200: [original]

    client.get("/api/jobs")

    assert original["last_error"] == ARCHIVE_ERROR
