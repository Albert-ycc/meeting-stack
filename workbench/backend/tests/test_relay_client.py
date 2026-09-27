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


def test_health_accepts_degraded_exit_code_and_uses_short_timeout(tmp_path, monkeypatch):
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
    payload = {
        "status": "degraded",
        "mode": "legacy",
        "worker": {"state": "absent", "heartbeat_age_seconds": None},
        "counts": {"queued": 1, "active": 0, "failed": 0},
    }

    monkeypatch.delenv("MEETING_RELAY_CONTROL_ENABLED", raising=False)

    def fake_run(arguments, **kwargs):
        calls.append(
            (
                arguments[1:],
                kwargs["timeout"],
                kwargs["env"].get("MEETING_RELAY_CONTROL_ENABLED"),
            )
        )
        return SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("meeting_workbench.relay_client.subprocess.run", fake_run)

    result = RelayClient(settings).health()

    assert result == payload
    assert calls == [(["health", "--json"], 2, "1")]


def test_remote_bootstrap_starts_web_in_controlled_relay_mode():
    root = Path(__file__).resolve().parents[2]

    script = (root / "scripts" / "remote-bootstrap.sh").read_text(encoding="utf-8")

    web_command = next(
        line for line in script.splitlines() if ".venv/bin/meeting-workbench serve" in line
    )
    assert "MEETING_RELAY_CONTROL_ENABLED=1" in web_command


def test_health_preserves_unavailable_json_from_exit_code_two(tmp_path, monkeypatch):
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
    payload = {
        "status": "unavailable",
        "mode": "controlled",
        "worker": {"state": "stale", "heartbeat_age_seconds": 31},
        "counts": {"queued": 1, "active": 1, "failed": 0},
    }

    monkeypatch.setattr(
        "meeting_workbench.relay_client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout=json.dumps(payload), stderr=""
        ),
    )

    assert RelayClient(settings).health() == payload


def test_relayctl_stderr_stays_in_server_log_not_in_client_facing_error(tmp_path, monkeypatch, caplog):
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
        f"Traceback (most recent call last):\n  File \"{tmp_path}/relayctl\", line 12\n"
        "KeyError: 'job-secret-internal-path'"
    )

    monkeypatch.setattr(
        "meeting_workbench.relay_client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr=sensitive_stderr
        ),
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
        stdout = "job-created\n" if arguments[1] == "enqueue" else json.dumps({"job_id": "job-created"})
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
