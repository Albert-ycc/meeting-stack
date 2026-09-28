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
