"""材料里的录音和视频（第三期 3d）：声档自己转，不走中转。

中转只收 m4a、mp3、wav，每个任务都会变成一场会、要写纪要、排在新会议前面，transcribe.sh 还会把输入
文件挪走，所以材料里的音视频由这里的循环转，写法照千问影子转写（进度、重启后接上）。

- 一次只转一个文件；第一轮前等 5 秒，没活时每 60 秒看一次。挑文件的顺序同 3a。
- 要有 ffmpeg、ffprobe、FunASR 的 Python 和 transcribe/funasr_material.py，缺哪个都记 waiting、
  note=engine_missing；装上以后由 3c 的定时检查改回 pending。
- 每个文件：
  1. 4GB 以内的算整份 sha256（每 256MB 查一次忙信号）：和会议原始录音一样的记 done、
     note=meeting_audio、meeting_id，不转写，免得会议和材料两边重复命中。
  2. ffprobe 取时长，15 秒超时记处理超时；拿不到时长（N/A，裸 aac 常见）不算损坏，按 10 分钟一段
     一直转到切出空的 wav 为止。没有音轨的记 done、note=no_speech。
  3. 按 10 分钟一段切，每段多取 2 秒，定位放在输入端；写到 data_dir/material-asr/<key 前 16 位>/，
     转完就删。最多转前 6 小时，多的 note=truncated。
  4. 每段交给常驻的转写程序；去掉和上一段重叠处重复的句子，追加进 material_media_jobs.parts，
     done_ms 前进。追加前查一次文件没变。
  5. 全部转完：句子合到 30 到 60 秒一段、满 400 字另起一段，写片段、state=done，同一个事务删掉
     断点行。一个字都没有记 note=no_speech。
- 让路：每段开始前、转写进行中每 3 秒查一次忙信号；忙了立刻杀掉转写程序的整个进程组，转好的段
  留着，下次从 done_ms 接着转。转写程序闲 5 分钟或每次让路后都退出，把内存还给会议转写。
- 超时：每段取「音频长度 × 3」和 material_media_segment_timeout_s 的较大者。超时或回 ok:false 都算
  这一段失败，同一段重试一次；两次都失败先查盘，盘在线时超时的记处理超时、ok:false 的记文件损坏。
- 不写中转的任何表，不占中转的队列；这个循环也不进健康检查。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import sqlite3
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import Database, utc_now
from .material_content import (
    CHUNK_CHARS,
    ExtractResult,
    MaterialContent,
    _EndRound,
    _locked,
    _RootOffline,
    _split_long,
    stat_signature,
)
from .material_helpers import HelperCrashed, HelperProcess, HelperStopped, HelperTimeout, StopFlag, run_background
from .material_rules import LAYER_MEDIA
from .materials import ROOT_ONLINE, volume_state
from .ocr_engines import Tools, media_missing

logger = logging.getLogger(__name__)

EXTRACTOR = "funasr-material"
EXTRACTOR_VERSION = 1

FIRST_DELAY_SECONDS = 5.0
IDLE_LOOP_SECONDS = 60.0
WORK_LOOP_SECONDS = 1.0

SEGMENT_MS = 600_000
SEGMENT_EXTRA_SECONDS = 2
MAX_MS = 6 * 3600 * 1000
HASH_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
HASH_CHECK_BYTES = 256 * 1024 * 1024
HASH_BLOCK = 1024 * 1024
PROBE_TIMEOUT_SECONDS = 15.0
CUT_TIMEOUT_SECONDS = 300.0
BUSY_POLL_SECONDS = 3.0
HELPER_IDLE_SECONDS = 300.0
SEGMENT_TRIES = 2

MERGE_MIN_MS = 30_000
MERGE_MAX_MS = 60_000


class _Failed(Exception):
    """这一步读不了：status 同 ExtractResult（timeout、corrupt、unsupported、io_error、permission）。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


# ---------------------------------------------------------------------- 小工具


def classify_ffmpeg_error(stderr: bytes | str) -> str:
    """ffmpeg、ffprobe 失败时按报错分：没有权限、IO 错、加密（格式不支持）、其余算文件损坏。"""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    lowered = text.lower()
    if "permission denied" in lowered:
        return "permission"
    if "input/output error" in lowered:
        return "io_error"
    if "encrypt" in lowered or "drm" in lowered:
        return "unsupported"
    return "corrupt"


def parse_probe(stdout: bytes | str) -> tuple[int | None, bool]:
    """ffprobe 的 JSON：(时长毫秒或 None, 有没有音轨)。解析不了抛 ValueError。"""
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("ffprobe 的输出不是对象")
    streams = data.get("streams") or []
    has_audio = any(isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams)
    raw = (data.get("format") or {}).get("duration")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None, has_audio
    if seconds != seconds or seconds <= 0:  # NaN 或 0：当作拿不到
        return None, has_audio
    return int(round(seconds * 1000)), has_audio


def wav_seconds(path: Path) -> float:
    """切出来的 wav 有多长；空的（定位过了结尾）是 0。"""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 16000
            return handle.getnframes() / rate
    except (wave.Error, EOFError, OSError):
        try:
            size = path.stat().st_size
        except OSError:
            return 0.0
        return max(size - 44, 0) / (16000 * 2)


def _joined(left: str, right: str) -> str:
    """接上一句：英文单词之间补一个空格，中文直接接。"""
    if left and right and left[-1].isascii() and not left[-1].isspace() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


def merge_sentences(
    sentences: list[dict[str, Any]],
    *,
    min_ms: int = MERGE_MIN_MS,
    max_ms: int = MERGE_MAX_MS,
    limit: int = CHUNK_CHARS,
) -> list[dict[str, Any]]:
    """句子合成片段：够 30 秒就收，不超过 60 秒，满 400 字也另起一段。"""
    spans: list[dict[str, Any]] = []
    text = ""
    start: int | None = None
    end = 0

    def flush() -> None:
        nonlocal text, start, end
        if text.strip() and start is not None:
            spans.append({"start_ms": start, "end_ms": max(end, start), "text": text.strip()})
        text, start, end = "", None, 0

    for sentence in sentences:
        body = str(sentence.get("text") or "").strip()
        if not body:
            continue
        s_start = int(sentence.get("start_ms") or 0)
        s_end = int(sentence.get("end_ms") or s_start)
        for piece in _split_long(body, limit) if len(body) > limit else [body]:
            if start is not None and (len(_joined(text, piece)) > limit or s_end - start > max_ms):
                flush()
            if start is None:
                start = s_start
            text = _joined(text, piece)
            end = max(end, s_end)
            if end - start >= min_ms:
                flush()
    flush()
    return spans


_overlap_rules: dict[str, Callable[..., bool] | None] = {}


def overlap_rule(script: str | None) -> Callable[..., bool] | None:
    """会议转写的 is_overlap_duplicate（和材料转写程序放在同一个目录）；拿不到就不去重。"""
    if not script:
        return None
    source = Path(script).with_name("funasr_transcribe.py")
    cache_key = str(source)
    if cache_key not in _overlap_rules:
        rule = None
        try:
            spec = importlib.util.spec_from_file_location("meeting_workbench_funasr_transcribe", source)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                rule = getattr(module, "is_overlap_duplicate", None)
        except Exception:  # noqa: BLE001
            logger.warning("读不到 %s 里的重叠去重，材料录音段落交界处可能有重复句", source)
        _overlap_rules[cache_key] = rule
    return _overlap_rules[cache_key]


def drop_overlap(
    fresh: list[dict[str, Any]], parts: list[dict[str, Any]], boundary_ms: int, rule: Callable[..., bool] | None
) -> list[dict[str, Any]]:
    """这一段开头和上一段多取的 2 秒重叠：文字完全相同的句子去掉。"""
    if rule is None or not parts or boundary_ms <= 0:
        return fresh
    existing = [{"start": p["start_ms"], "end": p["end_ms"], "text": p["text"]} for p in parts[-12:]]
    kept = []
    for sentence in fresh:
        candidate = {"start": sentence["start_ms"], "end": sentence["end_ms"], "text": sentence["text"]}
        if rule(candidate, existing, boundary_ms / 1000):
            continue
        kept.append(sentence)
    return kept


# ---------------------------------------------------------------------- 循环


class MaterialMedia:
    """材料录音和视频的转写循环。服务里只有一个，自己一个线程，一次一个文件。"""

    def __init__(
        self,
        db: Database,
        settings: Any,
        content: MaterialContent,
        *,
        tools: Callable[[], Tools],
        busy_check: Callable[[], bool] | None = None,
        stop: StopFlag | None = None,
        state_of: Callable[[str], str] = volume_state,
        helper_factory: Callable[[Tools], Any] | None = None,
        run: Callable[..., Any] = run_background,
    ):
        self.db = db
        self.settings = settings
        self.content = content
        self.tools = tools
        self.busy_check = busy_check
        self.stop = stop
        self.state_of = state_of
        self.data_dir = Path(settings.data_dir)
        self.run = run
        self._helper_factory = helper_factory or self._default_helper
        self._helper: Any = None
        self._running = threading.Lock()
        self.progress: dict[str, Any] = {"paused": None, "content_key": None, "done_ms": 0, "total_ms": None}

    # ---------------------------------------------------------------- 转写程序

    def _default_helper(self, tools: Tools) -> HelperProcess:
        return HelperProcess(
            "funasr-material",
            [str(tools.funasr_python), str(tools.media_script)],
            data_dir=self.data_dir,
            env={"OMP_NUM_THREADS": "4"},
            idle_restart_seconds=HELPER_IDLE_SECONDS,
            rss_limit_bytes=None,
            stop=self.stop,
        )

    def helper(self, tools: Tools) -> Any:
        if self._helper is None:
            self._helper = self._helper_factory(tools)
        return self._helper

    def close(self) -> None:
        helper, self._helper = self._helper, None
        if helper is not None:
            helper.close()
            if self.stop is not None:
                self.stop.unregister(helper)

    def wait_idle(self, timeout: float) -> bool:
        if self._running.acquire(timeout=timeout):
            self._running.release()
            return True
        return False

    # ---------------------------------------------------------------- 一轮

    def run_once(self) -> dict[str, Any]:
        """挑一个文件转（能转多少转多少）。返回 {work, ended}，决定下一轮隔多久。"""
        with self._running:
            stats: dict[str, Any] = {"work": False, "ended": None}
            if self._helper is not None:
                self._helper.close_if_idle()
            if self.stop is not None and self.stop.is_set():
                stats["ended"] = "stopping"
                return stats
            if self.busy_check is not None and self.busy_check():
                stats["ended"] = "busy"
                self.progress["paused"] = "busy"
                return stats
            self.progress["paused"] = None
            try:
                stats["work"] = self._run_once()
            except _EndRound as end:
                stats["ended"] = end.reason
                if end.reason == "busy":
                    self.progress["paused"] = "busy"
            except _RootOffline:
                stats["ended"] = "offline"
            except sqlite3.OperationalError as error:
                if not _locked(error):
                    raise
                stats["ended"] = "db_busy"
            finally:
                self.progress.update({"content_key": None, "done_ms": 0, "total_ms": None})
            return stats

    def _run_once(self) -> bool:
        roots = {int(row["id"]): row["path"] for row in self.db.query_all("SELECT id, path FROM project_material_roots")}
        online = {root_id: path for root_id, path in roots.items() if self.state_of(path) == ROOT_ONLINE}
        if not online:
            return False
        candidates = self.content.extract_candidates(online, limit=1, layers=[LAYER_MEDIA])
        if not candidates:
            return False
        tools = self.tools()
        if media_missing(tools):
            self._mark_waiting()
            return False
        row = candidates[0]
        self.process(row, online[int(row["root_id"])], tools)
        return True

    def _mark_waiting(self) -> int:
        """缺程序：等着的录音都记 waiting（装上后 3c 的定时检查改回 pending，断点行留着）。"""
        return self.db.execute_rowcount(
            """UPDATE material_contents SET state = 'waiting', note = 'engine_missing', updated_at = ?
                WHERE layer = 'media' AND state = 'pending'""",
            (utc_now(),),
        )

    def _checkpoint(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise _EndRound("stopping")
        if self.busy_check is not None and self.busy_check():
            raise _EndRound("busy")

    # ---------------------------------------------------------------- 一个文件

    def process(self, row: dict[str, Any], root_path: str, tools: Tools) -> bool:
        key = str(row["content_key"])
        path = Path(root_path).joinpath(*str(row["rel_path"]).split("/"))
        expected = (row["content_size"], row["content_mtime_ns"])
        try:
            if stat_signature(path) != expected:
                return False  # 下一轮先重算标识
        except FileNotFoundError:
            return False
        except OSError as error:
            self.content._on_os_error(row, root_path, error, keep_key=True)
            return False
        self.progress.update({"content_key": key, "done_ms": 0, "total_ms": None})
        try:
            result = self._transcribe_file(row, path, expected, tools)
        except _Failed as failed:
            result = self._result(failed.status)
        except FileNotFoundError:
            return False
        except OSError as error:
            self.content._on_os_error(row, root_path, error, keep_key=True)
            return False
        if result is None:
            return False
        return self.content.finish_extract(row, root_path, path, expected, result)

    def _result(self, status: str, **fields: Any) -> ExtractResult:
        return ExtractResult(status, extractor=EXTRACTOR, extractor_version=EXTRACTOR_VERSION, **fields)

    def _transcribe_file(
        self, row: dict[str, Any], path: Path, expected: tuple[int | None, int | None], tools: Tools
    ) -> ExtractResult | None:
        key = str(row["content_key"])
        job = self._job(key)
        if job is None:
            # 1. 是不是哪场会的录音
            known = self.db.query_one("SELECT sha256 FROM material_contents WHERE content_key = ?", (key,))
            digest = known["sha256"] if known else None
            if digest is None:
                digest = self._sha256(path, expected[0])
            if digest is not None:
                meeting_id = self._meeting_for(digest)
                if meeting_id is not None:
                    return self._result("ok", spans=[], note="meeting_audio", meeting_id=meeting_id, sha256=digest)
                self.db.execute(
                    "UPDATE material_contents SET sha256 = ? WHERE content_key = ? AND sha256 IS NULL", (digest, key)
                )
            # 2. 时长和音轨
            total_ms, has_audio = self._probe(path, tools)
            if not has_audio:
                return self._result("ok", spans=[], note="no_speech", duration_ms=total_ms)
            job = self._start_job(key, total_ms)
        return self._segments(row, path, expected, tools, job)

    def _segments(
        self,
        row: dict[str, Any],
        path: Path,
        expected: tuple[int | None, int | None],
        tools: Tools,
        job: dict[str, Any],
    ) -> ExtractResult | None:
        key = str(row["content_key"])
        done_ms = int(job["done_ms"] or 0)
        total_ms = int(job["total_ms"]) if job["total_ms"] is not None else None
        parts: list[dict[str, Any]] = json.loads(job["parts"] or "[]")
        limit_ms = MAX_MS if total_ms is None else min(total_ms, MAX_MS)
        heard_ms = done_ms
        rule = overlap_rule(tools.media_script)
        folder = self.data_dir / "material-asr" / _key16(key)
        reached_end = False
        self.progress.update({"done_ms": done_ms, "total_ms": total_ms})
        try:
            while done_ms < limit_ms:
                self._checkpoint()
                cut = self._cut(path, done_ms, folder, tools)
                if cut is None:
                    reached_end = True
                    break
                wav, seconds = cut
                try:
                    sentences = self._transcribe_segment(key, wav, done_ms, seconds, tools)
                finally:
                    wav.unlink(missing_ok=True)
                fresh = drop_overlap(_clean(sentences), parts, done_ms, rule)
                if stat_signature(path) != expected:
                    return None  # 转的途中文件被改了：下一轮先重算标识
                next_ms = done_ms + SEGMENT_MS
                if not self._save_progress(key, done_ms, next_ms, parts + fresh):
                    return None
                parts.extend(fresh)
                heard_ms = done_ms + int(seconds * 1000)
                done_ms = next_ms
                self.progress["done_ms"] = done_ms
                if seconds * 1000 < SEGMENT_MS:
                    reached_end = True  # 这一段没切满：到结尾了
                    break
        finally:
            try:
                folder.rmdir()
            except OSError:
                pass
        truncated = (total_ms is not None and total_ms > MAX_MS) or (total_ms is None and not reached_end)
        spans = merge_sentences(parts)
        return self._result(
            "ok",
            spans=spans,
            note=None if spans else "no_speech",
            truncated=truncated and bool(spans),
            duration_ms=total_ms if total_ms is not None else heard_ms,
        )

    # ---------------------------------------------------------------- 各步

    def _sha256(self, path: Path, size: int | None) -> str | None:
        if size is None or size > HASH_LIMIT_BYTES:
            return None
        digest = hashlib.sha256()
        since_check = 0
        with path.open("rb") as handle:
            while True:
                block = handle.read(HASH_BLOCK)
                if not block:
                    break
                digest.update(block)
                since_check += len(block)
                if since_check >= HASH_CHECK_BYTES:
                    since_check = 0
                    self._checkpoint()
        return digest.hexdigest()

    def _meeting_for(self, digest: str) -> str | None:
        row = self.db.query_one(
            """SELECT id FROM meetings WHERE original_audio_sha256 = ?
               UNION ALL
               SELECT meeting_id AS id FROM artifacts WHERE sha256 = ?
               LIMIT 1""",
            (digest, digest),
        )
        return str(row["id"]) if row else None

    def _probe(self, path: Path, tools: Tools) -> tuple[int | None, bool]:
        argv = [str(tools.ffprobe), "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path)]
        try:
            result = self.run(argv, timeout=PROBE_TIMEOUT_SECONDS, stop=self.stop)
        except HelperTimeout as error:
            raise _Failed("timeout") from error
        except HelperStopped as stopped:
            raise _EndRound(stopped.reason) from stopped
        if result.returncode != 0:
            raise _Failed(classify_ffmpeg_error(result.stderr))
        try:
            return parse_probe(result.stdout)
        except ValueError as error:
            raise _Failed("corrupt") from error

    def _cut(self, path: Path, start_ms: int, folder: Path, tools: Tools) -> tuple[Path, float] | None:
        """切一段 16k 单声道 wav（多取 2 秒）；切出来是空的就是过了结尾，返回 None。"""
        folder.mkdir(parents=True, exist_ok=True)
        wav = folder / f"{start_ms}.wav"
        argv = [
            str(tools.ffmpeg), "-nostdin", "-v", "error", "-y",
            "-ss", f"{start_ms / 1000:.3f}", "-t", str(SEGMENT_MS // 1000 + SEGMENT_EXTRA_SECONDS),
            "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(wav),
        ]
        try:
            result = self.run(argv, timeout=CUT_TIMEOUT_SECONDS, stop=self.stop)
        except HelperTimeout as error:
            wav.unlink(missing_ok=True)
            raise _Failed("timeout") from error
        except HelperStopped as stopped:
            wav.unlink(missing_ok=True)
            raise _EndRound(stopped.reason) from stopped
        if result.returncode != 0:
            wav.unlink(missing_ok=True)
            raise _Failed(classify_ffmpeg_error(result.stderr))
        seconds = wav_seconds(wav) if wav.is_file() else 0.0
        if seconds < 0.05:
            wav.unlink(missing_ok=True)
            return None
        return wav, seconds

    def _transcribe_segment(
        self, key: str, wav: Path, offset_ms: int, seconds: float, tools: Tools
    ) -> list[dict[str, Any]]:
        """一段交给常驻的转写程序；超时或 ok:false 同一段再试一次。"""
        timeout = max(seconds * 3, float(self.settings.material_media_segment_timeout_s))
        helper = self.helper(tools)
        failure = "timeout"
        for _attempt in range(SEGMENT_TRIES):
            self._checkpoint()
            try:
                pid = helper.start()
                if pid:
                    self.db.execute(
                        "UPDATE material_media_jobs SET pid = ?, updated_at = ? WHERE content_key = ?",
                        (pid, utc_now(), key),
                    )
                answer = helper.request(
                    {"wav": str(wav), "offset_ms": offset_ms},
                    timeout=timeout,
                    abort=self._abort_reason,
                    abort_every=BUSY_POLL_SECONDS,
                )
            except HelperStopped as stopped:
                # 会议开始转写或服务在关：进程组已经杀掉，转好的段留着，下次从 done_ms 接着转
                raise _EndRound(stopped.reason or "stopping") from stopped
            except (HelperTimeout, HelperCrashed) as error:
                logger.warning("材料录音转写这一段失败（%s）：%s", key, error)
                failure = "timeout"
                continue
            if answer.get("ok"):
                return list(answer.get("sentences") or [])
            logger.warning("材料录音转写这一段回了 ok:false（%s）：%.300s", key, answer.get("error"))
            failure = "corrupt"
        raise _Failed(failure)

    def _abort_reason(self) -> str | None:
        return "busy" if self.busy_check is not None and self.busy_check() else None

    # ---------------------------------------------------------------- 断点行

    def _job(self, key: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM material_media_jobs WHERE content_key = ?", (key,))

    def _start_job(self, key: str, total_ms: int | None) -> dict[str, Any]:
        self.db.execute(
            """INSERT OR IGNORE INTO material_media_jobs(content_key, done_ms, total_ms, parts, attempts, pid, updated_at)
               VALUES (?, 0, ?, '[]', 0, NULL, ?)""",
            (key, total_ms, utc_now()),
        )
        job = self._job(key)
        assert job is not None
        return job

    def _save_progress(self, key: str, done_ms: int, next_ms: int, parts: list[dict[str, Any]]) -> bool:
        """转好一段：追加句子、done_ms 前进（带条件，别的地方动过就不写）。"""
        count = self.db.execute_rowcount(
            """UPDATE material_media_jobs SET done_ms = ?, parts = ?, attempts = 0, updated_at = ?
                WHERE content_key = ? AND done_ms = ?""",
            (next_ms, json.dumps(parts, ensure_ascii=False), utc_now(), key, done_ms),
        )
        return count == 1


def _key16(key: str) -> str:
    return re.sub(r"[^0-9a-f]", "", key.split(":", 1)[-1])[:16] or "x"


def _clean(sentences: list[Any]) -> list[dict[str, Any]]:
    """转写程序回来的句子：只留有字的，时间取整。"""
    cleaned = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        text = str(sentence.get("text") or "").strip()
        if not text:
            continue
        try:
            start = int(sentence.get("start_ms") or 0)
            end = int(sentence.get("end_ms") or start)
        except (TypeError, ValueError):
            continue
        cleaned.append({"start_ms": start, "end_ms": max(end, start), "text": text})
    return cleaned

