from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import fcntl

from .config import Settings


class WaveformError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pixels_to_peaks(pixels: bytes, width: int, height: int) -> list[float]:
    if len(pixels) != width * height:
        raise WaveformError("FFmpeg 波形像素尺寸不匹配")
    center = (height - 1) / 2
    denominator = max(center, 1)
    peaks = []
    for x in range(width):
        lit = [y for y in range(height) if pixels[y * width + x] > 16]
        amplitude = max((abs(y - center) for y in lit), default=0) / denominator
        peaks.append(round(min(1.0, amplitude), 4))
    return peaks


class WaveformPeaks:
    def __init__(self, settings: Settings, *, width: int = 1200, height: int = 64):
        self.settings = settings
        self.width = width
        self.height = height

    def get(self, audio_path: Path, known_sha256: str | None = None) -> dict[str, Any]:
        audio_path = audio_path.resolve()
        fingerprint = known_sha256 or sha256_file(audio_path)
        self.settings.peaks_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.settings.peaks_dir / f"{fingerprint}-{self.width}.json"
        with self._generation_lock(cache_path):
            return self._get_or_create(audio_path, fingerprint, cache_path)

    def _get_or_create(
        self, audio_path: Path, fingerprint: str, cache_path: Path
    ) -> dict[str, Any]:
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if self._valid_cache(cached, fingerprint):
                    return cached
            except (OSError, ValueError):
                pass
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                pass
        duration = self._duration(audio_path)
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-filter_complex",
            f"aformat=channel_layouts=mono,showwavespic=s={self.width}x{self.height}:colors=white:draw=full",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise WaveformError(f"波形生成失败：{error}") from error
        if result.returncode != 0:
            raise WaveformError("FFmpeg 无法生成波形")
        payload = {
            "sha256": fingerprint,
            "duration_seconds": duration,
            "width": self.width,
            "peaks": pixels_to_peaks(result.stdout, self.width, self.height),
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{cache_path.name}.", dir=cache_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, cache_path)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return payload

    def _valid_cache(self, cached: Any, fingerprint: str) -> bool:
        if not isinstance(cached, dict):
            return False
        duration = cached.get("duration_seconds")
        width = cached.get("width")
        peaks = cached.get("peaks")
        if (
            cached.get("sha256") != fingerprint
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration <= 0
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width != self.width
            or not isinstance(peaks, list)
            or len(peaks) != self.width
        ):
            return False
        return all(
            not isinstance(peak, bool)
            and isinstance(peak, (int, float))
            and math.isfinite(float(peak))
            and 0 <= peak <= 1
            for peak in peaks
        )

    @staticmethod
    @contextmanager
    def _generation_lock(cache_path: Path) -> Iterator[None]:
        lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _duration(audio_path: Path) -> float:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            duration = float(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise WaveformError("无法读取音频时长") from error
        if result.returncode != 0 or duration <= 0:
            raise WaveformError("无法读取音频时长")
        return duration
