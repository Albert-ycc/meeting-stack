from pathlib import Path

import pytest
from pydantic import ValidationError

from meeting_workbench.config import Settings


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"upload_chunk_bytes": 0}, "upload_chunk_bytes"),
        ({"scan_interval_seconds": 0}, "scan_interval_seconds"),
        ({"max_incomplete_upload_bytes": -1}, "max_incomplete_upload_bytes"),
        (
            {"upload_chunk_bytes": 7, "max_json_request_bytes": 8},
            "Base64",
        ),
    ],
)
def test_invalid_numeric_and_chunk_request_relationships_fail_at_startup(overrides, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        Settings(semantic_enabled=False, **overrides)


def test_scan_interval_must_leave_a_full_scan_window_before_stale_timeout():
    assert Settings(semantic_enabled=False, scan_interval_seconds=90).scan_interval_seconds == 90

    with pytest.raises((ValidationError, ValueError), match="scan_interval_seconds.*90"):
        Settings(semantic_enabled=False, scan_interval_seconds=90.001)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"links_backfill_days": -1}, "links_backfill_days"),
        ({"links_llm_daily_calls": -1}, "links_llm_daily_calls"),
        ({"qa_daily_questions": -1}, "qa_daily_questions"),
        ({"qa_timeout_seconds": 0}, "qa_timeout_seconds"),
        ({"related_floor": 0.29}, "related_floor"),
        ({"related_floor": 0.96}, "related_floor"),
        ({"related_margin": -0.01}, "related_margin"),
        ({"related_margin": 0.31}, "related_margin"),
    ],
)
def test_phase_four_settings_are_range_checked(overrides, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        Settings(semantic_enabled=False, **overrides)


def test_phase_four_settings_defaults_and_edges():
    settings = Settings(semantic_enabled=False)
    assert (settings.links_llm_enabled, settings.glossary_mining_enabled) == (True, True)
    assert (settings.links_backfill_days, settings.links_llm_daily_calls) == (180, 200)
    assert (settings.qa_daily_questions, settings.qa_timeout_seconds) == (100, 90)
    assert (settings.related_floor, settings.related_margin) == (0.60, 0.05)
    # 0 是合法的：只做新会、后台不调、问答关闭
    edge = Settings(
        semantic_enabled=False,
        links_backfill_days=0,
        links_llm_daily_calls=0,
        qa_daily_questions=0,
        related_floor=0.3,
        related_margin=0,
    )
    assert edge.links_backfill_days == edge.links_llm_daily_calls == edge.qa_daily_questions == 0
    top = Settings(semantic_enabled=False, related_floor=0.95, related_margin=0.3)
    assert (top.related_floor, top.related_margin) == (0.95, 0.3)


def test_allowed_hosts_reads_comma_separated_env(monkeypatch):
    monkeypatch.setenv("MEETING_WORKBENCH_ALLOWED_HOSTS", " A.example, b.example ,,")
    monkeypatch.setenv("MEETING_WORKBENCH_PUBLIC_BASE_URL", "https://Mac.Example.ts.net")
    settings = Settings(semantic_enabled=False)

    assert settings.trusted_hostnames() == {
        "127.0.0.1",
        "localhost",
        "::1",
        "mac.example.ts.net",
        "a.example",
        "b.example",
    }


def test_default_trusted_hostnames_are_loopback_only(monkeypatch):
    monkeypatch.delenv("MEETING_WORKBENCH_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MEETING_WORKBENCH_PUBLIC_BASE_URL", raising=False)

    assert Settings(semantic_enabled=False).trusted_hostnames() == {"127.0.0.1", "localhost", "::1"}


def test_env_files_are_located_from_the_repo_not_the_working_directory():
    from meeting_workbench import config

    repo = Path(config.__file__).resolve().parents[3]
    assert config.ENV_FILES == (repo / ".env", repo / "workbench" / ".env")
    # 测试里不许读任何 .env（conftest 置空了，借生产 venv 跑时也一样）
    assert Settings.model_config["env_file"] is None


def _env_settings(env_files):
    return Settings(_env_file=env_files, semantic_enabled=False)


def test_workbench_env_wins_over_repo_root_env_from_any_working_directory(tmp_path, monkeypatch):
    for name in ("PORT", "HOST", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(f"MEETING_WORKBENCH_{name}", raising=False)
    repo = tmp_path / "repo"
    (repo / "workbench").mkdir(parents=True)
    root_env = repo / ".env"
    workbench_env = repo / "workbench" / ".env"
    env_files = (root_env, workbench_env)

    # 生产现在的样子：只有 workbench/.env
    workbench_env.write_text("MEETING_WORKBENCH_PORT=2222\n", encoding="utf-8")
    assert _env_settings(env_files).port == 2222

    root_env.write_text(
        "MEETING_WORKBENCH_PORT=1111\nMEETING_WORKBENCH_PUBLIC_BASE_URL=https://mac.example.ts.net\n",
        encoding="utf-8",
    )
    for cwd in (tmp_path, repo / "workbench"):
        monkeypatch.chdir(cwd)
        settings = _env_settings(env_files)
        assert settings.port == 2222
        assert settings.public_base_url == "https://mac.example.ts.net"
