import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from meeting_workbench.config import Settings
from meeting_workbench.waveform import WaveformError, WaveformPeaks, pixels_to_peaks


def test_waveform_pixels_are_reduced_to_normalized_column_peaks():
    width, height = 3, 5
    pixels = bytearray(width * height)
    pixels[2 * width + 0] = 255
    pixels[1 * width + 1] = 255
    pixels[0 * width + 2] = 255

    assert pixels_to_peaks(bytes(pixels), width, height) == [0.0, 0.5, 1.0]


def test_waveform_pixels_reject_wrong_frame_size():
    try:
        pixels_to_peaks(b"short", 3, 5)
    except WaveformError as error:
        assert "尺寸" in str(error)
    else:
        raise AssertionError("expected WaveformError")


def test_same_audio_hash_generates_waveform_only_once_when_requested_concurrently(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        semantic_enabled=False,
    )
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    waveforms = WaveformPeaks(settings, width=3, height=5)
    settings.peaks_dir.mkdir(parents=True)
    cache_path = settings.peaks_dir / f"{'a' * 64}-3.json"
    cache_path.write_text(
        json.dumps(
            {
                "sha256": "a" * 64,
                "duration_seconds": 1.0,
                "width": 3,
                "peaks": None,
            }
        ),
        encoding="utf-8",
    )
    duration_calls = 0
    duration_lock = threading.Lock()

    def slow_duration(_path):
        nonlocal duration_calls
        with duration_lock:
            duration_calls += 1
        time.sleep(0.1)
        return 1.0

    pixels = bytearray(15)
    pixels[2 * 3 + 0] = 255
    pixels[1 * 3 + 1] = 255
    pixels[0 * 3 + 2] = 255
    completed = subprocess.CompletedProcess([], 0, stdout=bytes(pixels), stderr=b"")
    with (
        patch.object(waveforms, "_duration", side_effect=slow_duration),
        patch("meeting_workbench.waveform.subprocess.run", return_value=completed),
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: waveforms.get(audio, "a" * 64), range(2)))

    assert duration_calls == 1
    assert results[0] == results[1]
    assert json.loads(cache_path.read_text(encoding="utf-8"))["peaks"] == [0.0, 0.5, 1.0]


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"duration_seconds": "not-a-number"},
        {"width": 3.0},
    ],
)
def test_waveform_cache_with_wrong_value_types_is_ignored_and_rebuilt(tmp_path, invalid_values):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "db.sqlite3",
        semantic_enabled=False,
    )
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    waveforms = WaveformPeaks(settings, width=3, height=5)
    settings.peaks_dir.mkdir(parents=True)
    cache_path = settings.peaks_dir / f"{'b' * 64}-3.json"
    cached = {
        "sha256": "b" * 64,
        "duration_seconds": 1.0,
        "width": 3,
        "peaks": [0.0, 0.5, 1.0],
    }
    cached.update(invalid_values)
    cache_path.write_text(json.dumps(cached), encoding="utf-8")
    pixels = bytearray(15)
    pixels[2 * 3 + 0] = 255
    pixels[1 * 3 + 1] = 255
    pixels[0 * 3 + 2] = 255
    completed = subprocess.CompletedProcess([], 0, stdout=bytes(pixels), stderr=b"")

    with (
        patch.object(waveforms, "_duration", return_value=2.5) as duration,
        patch("meeting_workbench.waveform.subprocess.run", return_value=completed),
    ):
        result = waveforms.get(audio, "b" * 64)

    assert result["duration_seconds"] == 2.5
    assert result["peaks"] == [0.0, 0.5, 1.0]
    duration.assert_called_once_with(audio.resolve())
