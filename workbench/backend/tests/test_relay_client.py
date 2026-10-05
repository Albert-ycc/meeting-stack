import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.relay_client import RelayClient, RelayUnavailable


def test_relay_client_passes_explicit_transcript_snapshot_contract(tmp_path, monkeypatch):
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )
    transcript = tmp_path / "current.txt"
    transcript.write_text("当前编辑稿\n", encoding="utf-8")
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"audio")
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "enqueue":
            stdout = "job-created\n"
        else:
            stdout = json.dumps({"job_id": "job-created", "status": "draft_modified"})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)
    client = RelayClient(settings)

    assert (
        client.enqueue(audio, stage="minutes_generating", transcript_path=transcript)
        == "job-created"
    )
    client.retry("job-created", "minutes_generating", transcript_path=transcript)
    client.mark_draft_modified("job-created")

    assert calls[0][0][1:] == [
        "enqueue",
        str(audio),
        "--stage",
        "minutes_generating",
        "--transcript",
        str(transcript),
    ]
    assert calls[1][0][1:] == [
        "retry",
        "job-created",
        "--stage",
        "minutes_generating",
        "--transcript",
        str(transcript),
    ]
    assert calls[2][0][1:] == ["mark-draft-modified", "job-created"]
    assert calls[0][1]["env"]["MEETING_RELAY_JOBS_DB"] == str(settings.relay_jobs_db)


def test_relay_client_rejects_semantically_mismatched_publish_ack(tmp_path, monkeypatch):
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "job_id": "job-wrong",
                    "meeting_id": "vm-wrong",
                    "status": "completed_unreviewed",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)
    client = RelayClient(settings)

    with pytest.raises(RelayUnavailable, match="与请求不一致"):
        client.mark_published("job-right", tmp_path / "manifest.json", "vm-right")


def test_set_substate_reads_and_passes_the_current_attempt(tmp_path, monkeypatch):
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )
    calls = []

    def fake_run(arguments, **_kwargs):
        calls.append(arguments[1:])
        if arguments[1] == "status":
            payload = {"job_id": "job-current", "current_attempt": 4}
        else:
            payload = {"job_id": "job-current", "substates": {"index": {"status": "ready"}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)
    client = RelayClient(settings)

    client.set_substate("job-current", "index", "ready")

    assert calls == [
        ["status", "job-current", "--json"],
        [
            "set-substate",
            "job-current",
            "--name",
            "index",
            "--status",
            "ready",
            "--attempt",
            "4",
        ],
    ]


def test_remote_bootstrap_starts_web_in_controlled_relay_mode():
    root = Path(__file__).resolve().parents[2]

    script = (root / "scripts" / "remote-bootstrap.sh").read_text(encoding="utf-8")

    web_command = next(
        line for line in script.splitlines() if ".venv/bin/meeting-workbench serve" in line
    )
    assert "MEETING_RELAY_CONTROL_ENABLED=1" in web_command


def test_relayctl_stderr_stays_in_server_log_not_in_client_facing_error(
    tmp_path, monkeypatch, caplog
):
    """relayctl 失败时 stderr（可能带绝对路径/完整 traceback）只进日志，不回灌给客户端。"""
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )
    sensitive_stderr = (
        f'Traceback (most recent call last):\n  File "{tmp_path}/relayctl", line 12\n'
        "KeyError: 'job-secret-internal-path'"
    )

    monkeypatch.setattr(
        "meeting_workbench.relay_client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr=sensitive_stderr),
    )

    with caplog.at_level("ERROR", logger="meeting_workbench.relay_client"):
        with pytest.raises(RelayUnavailable) as excinfo:
            RelayClient(settings).status("job-abc")

    assert str(tmp_path) not in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)
    assert str(excinfo.value) == "relayctl 返回失败，详见服务日志"
    assert sensitive_stderr in caplog.text


def test_project_hint_travels_as_environment_not_as_argument(tmp_path, monkeypatch):
    """旧版 relayctl 不认识 --project-hint；走环境变量，旧版忽略即可，入队不会失败。"""
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"audio")
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs["env"]))
        stdout = (
            "job-created\n" if arguments[1] == "enqueue" else json.dumps({"job_id": "job-created"})
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)
    monkeypatch.setenv("MEETING_RELAY_PROJECT_HINT", "别处遗留的值")
    client = RelayClient(settings)

    client.enqueue(audio, project_hint="project-46fd3e04e3db")
    client.retry("job-created", "transcribing", project_hint=" p-yt ")
    client.retry("job-created", "transcribing")

    assert all("--project-hint" not in arguments for arguments, _env in calls)
    assert calls[0][1]["MEETING_RELAY_PROJECT_HINT"] == "project-46fd3e04e3db"
    assert calls[1][1]["MEETING_RELAY_PROJECT_HINT"] == "p-yt"
    assert "MEETING_RELAY_PROJECT_HINT" not in calls[2][1]


def _spy_client(tmp_path, monkeypatch, *, stdout="{}"):
    """relayctl 摆个空壳文件，subprocess.run 换成记账的假货；返回 (client, calls)。"""
    relay_repo = tmp_path / "meeting-relay"
    executable = relay_repo / "quickstart" / "relayctl"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )
    calls = []

    def fake_run(arguments, **_kwargs):
        calls.append(arguments[1:])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)
    return RelayClient(settings), calls


BAD_JOB_IDS = [
    "--transcript=/etc/passwd",
    "--help",
    "-h",
    "",
    "job-",
    "job-abc def",
    "job-abc\n",
    "job-../../etc/passwd",
    "job-abc;rm",
    "JOB-ABC",
    "job-" + "a" * 65,
]


@pytest.mark.parametrize("bad", BAD_JOB_IDS)
def test_every_job_id_method_rejects_a_malformed_id_before_spawning_relayctl(
    tmp_path, monkeypatch, bad
):
    """任务号会原样进 relayctl 的 argv，格式不对的一律在 RelayClient 里挡掉，连子进程都不起。"""
    client, calls = _spy_client(tmp_path, monkeypatch)
    manifest = tmp_path / "manifest.json"
    attempts = [
        lambda: client.status(bad),
        lambda: client.retry(bad, "minutes_generating"),
        lambda: client.mark_draft_modified(bad),
        lambda: client.stop_after_stage(bad),
        lambda: client.cancel(bad),
        lambda: client.mark_published(bad, manifest, "vm-1"),
        lambda: client.set_substate(bad, "index", "ready", attempt=1),
        lambda: client.set_substate(bad, "index", "ready"),
        lambda: client.retry_substate(bad, "index"),
    ]

    for attempt in attempts:
        with pytest.raises(RelayUnavailable, match="任务号格式不对"):
            attempt()

    assert calls == []


@pytest.mark.parametrize(
    "good",
    ["job-0123456789abcdef", "job-created", "job-a_b-C9", "job-" + "a" * 64],
)
def test_job_id_shapes_relay_really_issues_still_pass(tmp_path, monkeypatch, good):
    """relay 入队时生成 job- 加 16 位小写十六进制（生产 169 个任务号全是这个形状），不能被挡。"""
    client, calls = _spy_client(tmp_path, monkeypatch, stdout='{"job_id": "x"}')

    client.status(good)
    client.cancel(good)

    assert calls == [["status", good, "--json"], ["cancel", good]]


@pytest.mark.parametrize("bad", ["--help", "job-abc def", "", "/etc/passwd", "vm-20260101"])
def test_enqueue_rejects_a_malformed_job_id_coming_back_from_relayctl(tmp_path, monkeypatch, bad):
    """入队返回的任务号之后还会再传给 relayctl，来路上就要按同一规则把关。"""
    client, _calls = _spy_client(tmp_path, monkeypatch, stdout=bad)
    audio = tmp_path / "meeting.m4a"
    audio.write_bytes(b"audio")

    with pytest.raises(RelayUnavailable, match="relayctl 未返回有效 job_id"):
        client.enqueue(audio)


def test_missing_relayctl_error_names_no_path_but_the_log_does(tmp_path, caplog):
    """HTTP 响应里只给人话；relayctl 的绝对路径只进服务日志（Tailscale 远程访问会暴露本机目录结构）。"""
    relay_repo = tmp_path / "meeting-relay-仓库"
    settings = Settings(
        data_dir=tmp_path / "data",
        archive_root=tmp_path / "archive",
        staging_root=tmp_path / "staging",
        relay_repo=relay_repo,
        relay_jobs_db=tmp_path / "jobs.sqlite3",
        semantic_enabled=False,
    )

    with caplog.at_level("ERROR", logger="meeting_workbench.relay_client"):
        with pytest.raises(RelayUnavailable) as excinfo:
            RelayClient(settings).status("job-0123456789abcdef")

    assert str(excinfo.value) == "中转程序找不到，详见服务日志"
    assert str(tmp_path) not in str(excinfo.value)
    assert "relayctl" not in str(excinfo.value)
    assert str(settings.relayctl_path) in caplog.text
