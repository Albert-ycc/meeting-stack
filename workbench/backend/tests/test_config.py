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
