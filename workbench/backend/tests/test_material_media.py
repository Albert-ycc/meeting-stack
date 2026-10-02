"""第三期 3d：材料里的录音和视频：会议录音查重、ffprobe、10 分钟一段、重叠去重、断点续转、让路、超时、
合成片段；还有 transcribe/funasr_material.py 自己的规矩。"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from meeting_workbench import material_media
from meeting_workbench.config import REPO_ROOT
from meeting_workbench.material_helpers import HelperCrashed, HelperStopped, HelperTimeout, StopFlag
from meeting_workbench.material_media import (
    MaterialMedia,
    classify_ffmpeg_error,
    merge_sentences,
    parse_probe,
    wav_seconds,
)
from meeting_workbench.materials import ROOT_ONLINE, ROOT_VOLUME_OFFLINE
from meeting_workbench.ocr_engines import Tools, media_missing

from .test_graph import add_meeting
from .test_material_content import contents, index, put, setup

SCRIPT = REPO_ROOT / "transcribe" / "funasr_material.py"


def fake_wav(path: Path, seconds: float) -> None:
    """只写文件头（声明的帧数就是长度），不写真的采样，测试不用写几十 MB。"""
    frames = int(seconds * 16000)
    data = frames * 2
    header = b"RIFF" + struct.pack("<I", 36 + data) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    header += b"data" + struct.pack("<I", data)
    path.write_bytes(header)


class FakeRun:
    """假的 ffprobe、ffmpeg：按文件名查时长（秒，None 是 N/A）和有没有音轨。"""

    def __init__(self, files):
        self.files = files
        self.calls = []
        self.probe_error = None
        self.cut_error = None

    def __call__(self, argv, *, timeout, stop=None, **kwargs):
        self.calls.append((list(argv), timeout))
        name = (
            Path(argv[-1]).name if argv[0] == "ffprobe" else Path(argv[argv.index("-i") + 1]).name
        )
        info = self.files[name]
        if argv[0] == "ffprobe":
            if self.probe_error is not None:
                if isinstance(self.probe_error, Exception):
                    raise self.probe_error
                return subprocess.CompletedProcess(argv, 1, b"", self.probe_error.encode())
            streams = [{"codec_type": "video"}] + (
                [{"codec_type": "audio"}] if info.get("audio", True) else []
            )
            duration = info.get("probe", info["seconds"])
            payload = {
                "streams": streams,
                "format": {"duration": "N/A" if duration is None else f"{duration:.6f}"},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload).encode(), b"")
        if self.cut_error is not None:
            return subprocess.CompletedProcess(argv, 1, b"", self.cut_error.encode())
        start = float(argv[argv.index("-ss") + 1])
        length = float(argv[argv.index("-t") + 1])
        fake_wav(Path(argv[-1]), max(0.0, min(length, info["seconds"] - start)))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class FakeTranscriber:
    """常驻转写程序的替身：每段回几句带时间的话；可以按次序抛错或回 ok:false。"""

    def __init__(self, *, plan=None, sentences=None, during=None):
        self.requests = []
        self.plan = list(plan or [])
        self.sentences = sentences or default_sentences
        self.during = during
        self.closed = False

    def start(self):
        return 4242

    def request(self, payload, *, timeout, abort=None, abort_every=None, **kwargs):
        self.requests.append((payload, timeout))
        if self.during is not None:
            self.during(len(self.requests))
        if abort is not None and abort():
            raise HelperStopped(abort())
        if self.plan:
            step = self.plan.pop(0)
            if isinstance(step, Exception):
                raise step
            if step == "fail":
                return {"ok": False, "error": "RuntimeError: 解码失败"}
        return {"ok": True, "sentences": self.sentences(payload["offset_ms"])}

    def close_if_idle(self):
        self.idle_checks = getattr(self, "idle_checks", 0) + 1
        return False

    def close(self):
        self.closed = True


def default_sentences(offset):
    sentences = [
        {
            "start_ms": offset + 1_000,
            "end_ms": offset + 20_000,
            "text": f"第{offset // 600_000 + 1}段开头讲报价。",
        },
        {"start_ms": offset + 20_000, "end_ms": offset + 45_000, "text": "接着讲交付时间。"},
        {"start_ms": offset + 598_000, "end_ms": offset + 601_500, "text": "重叠的一句。"},
    ]
    if offset:
        # 上一段多取的 2 秒里已经有这一句
        sentences.insert(
            0, {"start_ms": offset - 2_000, "end_ms": offset + 1_500, "text": "重叠的一句。"}
        )
    return sentences


def media_setup(tmp_path, files, *, busy=None, transcriber=None, tools=None, online=None):
    db, settings, root, root_id, content, indexer, now, state = setup(tmp_path, online=online)
    settings.material_media_segment_timeout_s = 900.0
    for name, info in files.items():
        put(root / "录音" / name, info.get("data", name.encode() * 10))
    index(indexer)
    content.run_round()
    run = FakeRun(files)
    transcriber = transcriber or FakeTranscriber()
    stop = StopFlag()
    media = MaterialMedia(
        db,
        settings,
        content,
        tools=tools
        or (
            lambda: Tools(
                ffmpeg="ffmpeg", ffprobe="ffprobe", funasr_python="python", media_script=str(SCRIPT)
            )
        ),
        busy_check=busy,
        stop=stop,
        state_of=lambda path: ROOT_ONLINE if state["online"] else ROOT_VOLUME_OFFLINE,
        helper_factory=lambda tools: transcriber,
        run=run,
    )
    return db, settings, root, media, run, transcriber, state


def only(db):
    rows = list(contents(db).values())
    assert len(rows) == 1
    return rows[0]


def chunks(db):
    return db.query_all("SELECT start_ms, end_ms, text FROM material_chunks ORDER BY ordinal")


def job(db):
    return db.query_one("SELECT * FROM material_media_jobs")


# ---------------------------------------------------------------------- 转写


def test_audio_is_cut_into_ten_minute_segments_and_merged_into_timed_chunks(tmp_path):
    db, settings, root, media, run, transcriber, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 1500}}
    )
    assert only(db)["layer"] == "media" and only(db)["state"] == "pending"
    stats = media.run_once()
    assert stats["work"] is True

    cuts = [argv for argv, _timeout in run.calls if argv[0] == "ffmpeg"]
    assert [argv[argv.index("-ss") + 1] for argv in cuts] == ["0.000", "600.000", "1200.000"]
    first = cuts[0]
    # 定位放在输入端，每段多取 2 秒，16k 单声道
    assert first.index("-ss") < first.index("-i") and first[first.index("-t") + 1] == "602"
    assert first[first.index("-ac") + 1] == "1" and first[first.index("-ar") + 1] == "16000"
    assert "-nostdin" in first and "-vn" in first
    assert (
        Path(first[-1]).parent == settings.data_dir / "material-asr" / Path(first[-1]).parent.name
    )
    assert [payload["offset_ms"] for payload, _timeout in transcriber.requests] == [
        0,
        600_000,
        1_200_000,
    ]
    # 每段超时取「音频长度 × 3」和 15 分钟的较大者
    assert [timeout for _payload, timeout in transcriber.requests] == [1806.0, 1806.0, 900.0]

    row = only(db)
    assert row["state"] == "done" and row["note"] is None and row["extractor"] == "funasr-material"
    assert (
        row["duration_ms"] == 1_500_000
        and row["sha256"] == hashlib.sha256("访谈.m4a".encode() * 10).hexdigest()
    )
    texts = [chunk["text"] for chunk in chunks(db)]
    assert "".join(texts).count("重叠的一句。") == 3  # 每段交界处的重复句去掉了，各段自己的留着
    assert all(len(text) <= 400 for text in texts)
    assert all(chunk["end_ms"] - chunk["start_ms"] <= 60_000 for chunk in chunks(db))
    assert chunks(db)[0]["start_ms"] == 1_000
    assert job(db) is None  # 转完断点行和片段同一个事务删掉
    assert not (settings.data_dir / "material-asr").exists() or not any(
        (settings.data_dir / "material-asr").iterdir()
    )
    hits = db.query_all(
        "SELECT c.start_ms FROM material_chunks_fts JOIN material_chunks c ON c.id = material_chunks_fts.rowid "
        "WHERE material_chunks_fts MATCH ?",
        ('"交付时间"',),
    )
    assert len(hits) == 3


def test_meeting_audio_is_recognized_by_hash_and_not_transcribed(tmp_path):
    data = b"meeting-audio" * 100
    db, _settings, _root, media, run, transcriber, _state = media_setup(
        tmp_path, {"周会.m4a": {"seconds": 1800, "data": data}}
    )
    add_meeting(db, "m1", ago=3)
    db.execute(
        "UPDATE meetings SET original_audio_sha256 = ? WHERE id = 'm1'",
        (hashlib.sha256(data).hexdigest(),),
    )
    media.run_once()
    row = only(db)
    assert row["state"] == "done" and row["note"] == "meeting_audio" and row["meeting_id"] == "m1"
    assert run.calls == [] and transcriber.requests == []
    assert chunks(db) == []


def test_meeting_audio_also_matches_artifact_hashes(tmp_path):
    data = b"exported" * 100
    db, _settings, _root, media, _run, transcriber, _state = media_setup(
        tmp_path, {"导出.wav": {"seconds": 60, "data": data}}
    )
    add_meeting(db, "m2", ago=1)
    db.execute(
        """INSERT INTO artifacts(meeting_id, kind, source_root, path, sha256, created_at)
           VALUES ('m2', 'audio', 'archive', '/x/导出.wav', ?, '2026-09-27T00:00:00Z')""",
        (hashlib.sha256(data).hexdigest(),),
    )
    media.run_once()
    assert only(db)["note"] == "meeting_audio" and only(db)["meeting_id"] == "m2"
    assert transcriber.requests == []


def test_unknown_duration_runs_until_an_empty_segment(tmp_path):
    db, _settings, _root, media, run, transcriber, _state = media_setup(
        tmp_path, {"裸流.aac": {"seconds": 1200, "probe": None}}
    )
    media.run_once()
    cuts = [argv[argv.index("-ss") + 1] for argv, _t in run.calls if argv[0] == "ffmpeg"]
    assert cuts == ["0.000", "600.000", "1200.000"]  # 第三段是空的：到结尾了
    assert len(transcriber.requests) == 2
    row = only(db)
    assert row["state"] == "done" and row["note"] is None and row["duration_ms"] == 1_200_000


def test_video_without_audio_track_is_no_speech(tmp_path):
    db, _settings, _root, media, run, transcriber, _state = media_setup(
        tmp_path, {"录屏.mov": {"seconds": 300, "audio": False}}
    )
    media.run_once()
    row = only(db)
    assert row["state"] == "done" and row["note"] == "no_speech"
    assert [argv[0] for argv, _t in run.calls] == ["ffprobe"] and transcriber.requests == []


def test_silence_is_no_speech(tmp_path):
    transcriber = FakeTranscriber(sentences=lambda offset: [])
    db, *_rest = media_setup(tmp_path, {"空.wav": {"seconds": 30}}, transcriber=transcriber)
    _rest[2].run_once()
    row = only(db)
    assert row["state"] == "done" and row["note"] == "no_speech" and row["chunks"] == 0


def test_only_the_first_six_hours_are_transcribed(tmp_path, monkeypatch):
    monkeypatch.setattr(material_media, "MAX_MS", 1_200_000)
    db, _settings, _root, media, _run, transcriber, _state = media_setup(
        tmp_path, {"长.mp3": {"seconds": 1500}}
    )
    media.run_once()
    assert len(transcriber.requests) == 2
    row = only(db)
    assert row["state"] == "done" and row["note"] == "truncated"


def test_probe_timeout_retries_later_and_bad_output_is_corrupt(tmp_path):
    db, _settings, _root, media, run, _transcriber, _state = media_setup(
        tmp_path, {"坏.m4a": {"seconds": 60}}
    )
    run.probe_error = HelperTimeout("ffprobe 超过 15 秒")
    media.run_once()
    row = only(db)
    assert (
        row["state"] == "pending" and row["reason"] == "timeout" and row["next_try_at"] is not None
    )
    assert run.calls[0][1] == 15.0

    db.execute("UPDATE material_contents SET next_try_at = NULL")
    run.probe_error = "Invalid data found when processing input"
    media.run_once()
    assert only(db)["state"] == "unreadable" and only(db)["reason"] == "corrupt"


def test_encrypted_media_is_unsupported(tmp_path):
    db, _settings, _root, media, run, _transcriber, _state = media_setup(
        tmp_path, {"加密.m4a": {"seconds": 60}}
    )
    run.probe_error = "[mov,mp4] stream 0: encrypted (DRM) stream"
    media.run_once()
    assert only(db)["state"] == "unreadable" and only(db)["reason"] == "unsupported"


def test_errors_with_the_disk_gone_write_nothing(tmp_path):
    db, _settings, _root, media, run, _transcriber, state = media_setup(
        tmp_path, {"盘.m4a": {"seconds": 60}}
    )
    run.probe_error = "Invalid data found when processing input"

    def unplug(*args, **kwargs):
        state["online"] = False
        return FakeRun.__call__(run, *args, **kwargs)

    media.run = unplug
    stats = media.run_once()
    assert stats["ended"] == "offline"
    assert only(db)["state"] == "pending"


# ---------------------------------------------------------------------- 让路、断点、重试


def test_busy_meeting_kills_the_transcriber_and_resumes_from_done_ms(tmp_path):
    busy = {"on": False}

    def during(count):
        if count == 2:
            busy["on"] = True  # 第二段转到一半，会议开始转写

    transcriber = FakeTranscriber(during=during)
    db, _settings, _root, media, _run, _t, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 1500}}, busy=lambda: busy["on"], transcriber=transcriber
    )
    stats = media.run_once()
    assert stats["ended"] == "busy" and media.progress["paused"] == "busy"
    saved = job(db)
    assert saved["done_ms"] == 600_000 and saved["pid"] == 4242
    assert len(json.loads(saved["parts"])) == 3
    assert only(db)["state"] == "pending" and only(db)["reason"] is None

    # 忙的时候这一轮不开工
    assert media.run_once()["ended"] == "busy" and len(transcriber.requests) == 2

    busy["on"] = False
    transcriber.during = None
    media.run_once()
    assert [payload["offset_ms"] for payload, _t in transcriber.requests] == [
        0,
        600_000,
        600_000,
        1_200_000,
    ]
    assert only(db)["state"] == "done" and job(db) is None
    assert media.progress["paused"] is None


def test_busy_is_checked_before_each_segment(tmp_path):
    busy = {"on": False}

    def sentences(offset):
        busy["on"] = True  # 第一段刚转完，会议开始转写
        return default_sentences(offset)

    transcriber = FakeTranscriber(sentences=sentences)
    db, _settings, _root, media, run, _t, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 1500}}, busy=lambda: busy["on"], transcriber=transcriber
    )
    assert media.run_once()["ended"] == "busy"
    assert len(transcriber.requests) == 1 and job(db)["done_ms"] == 600_000
    assert len([argv for argv, _t in run.calls if argv[0] == "ffmpeg"]) == 1  # 第二段没切


def test_a_failed_segment_is_retried_once(tmp_path):
    transcriber = FakeTranscriber(plan=[HelperCrashed("退出了")])
    db, *_rest = media_setup(tmp_path, {"短.m4a": {"seconds": 120}}, transcriber=transcriber)
    _rest[2].run_once()
    assert len(transcriber.requests) == 2 and only(db)["state"] == "done"


def test_two_timeouts_record_timeout_and_keep_progress_for_the_retry(tmp_path):
    transcriber = FakeTranscriber(plan=[None, HelperTimeout("超时"), HelperTimeout("超时")])
    transcriber.plan[0] = "ok"
    db, _settings, _root, media, _run, _t, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 1500}}, transcriber=transcriber
    )
    media.run_once()
    row = only(db)
    assert row["state"] == "pending" and row["reason"] == "timeout" and row["attempts"] == 1
    assert job(db)["done_ms"] == 600_000  # 一小时后从第二段接着转

    db.execute("UPDATE material_contents SET next_try_at = NULL")
    transcriber.plan = [HelperTimeout("超时"), HelperTimeout("超时")]
    media.run_once()
    row = only(db)
    assert row["state"] == "unreadable" and row["reason"] == "timeout"
    assert job(db) is None


def test_two_ok_false_answers_record_corrupt(tmp_path):
    transcriber = FakeTranscriber(plan=["fail", "fail"])
    db, *_rest = media_setup(tmp_path, {"坏.m4a": {"seconds": 120}}, transcriber=transcriber)
    _rest[2].run_once()
    assert only(db)["state"] == "unreadable" and only(db)["reason"] == "corrupt"
    assert job(db) is None


def test_file_changed_while_transcribing_drops_the_segment(tmp_path):
    holder = {}

    def during(count):
        target = holder["root"] / "录音" / "访谈.m4a"
        os.utime(target, (1_000_000_000, 1_000_000_000))

    transcriber = FakeTranscriber(during=during)
    db, _settings, root, media, _run, _t, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 1500}}, transcriber=transcriber
    )
    holder["root"] = root
    media.run_once()
    assert job(db)["done_ms"] == 0 and only(db)["state"] == "pending"
    assert chunks(db) == []


def test_missing_programs_mark_media_waiting(tmp_path):
    tools = Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", funasr_python=None, media_script=str(SCRIPT))
    db, _settings, _root, media, run, _t, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 60}}, tools=lambda: tools
    )
    media.run_once()
    row = only(db)
    assert row["state"] == "waiting" and row["note"] == "engine_missing" and run.calls == []
    assert media_missing(tools) == ["funasr"]
    assert media_missing(Tools(funasr_python="p", media_script="s")) == ["ffmpeg"]


def test_idle_transcriber_is_closed_between_rounds(tmp_path):
    db, _settings, _root, media, _run, transcriber, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 60}}
    )
    media.run_once()
    assert only(db)["state"] == "done"
    assert media.run_once()["work"] is False
    assert transcriber.idle_checks == 1  # 闲够 5 分钟由它自己关，把内存还给会议转写


def test_stop_ends_the_round_without_writing(tmp_path):
    db, _settings, _root, media, _run, transcriber, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 60}}
    )
    media.stop.set()
    assert media.run_once()["ended"] == "stopping"
    assert transcriber.requests == [] and only(db)["state"] == "pending"


# ---------------------------------------------------------------------- 小工具


def test_merge_sentences_into_30_to_60_second_chunks_under_400_chars():
    sentences = [
        {"start_ms": i * 10_000, "end_ms": i * 10_000 + 9_000, "text": f"第{i}句。"}
        for i in range(9)
    ]
    spans = merge_sentences(sentences)
    assert [(span["start_ms"], span["end_ms"]) for span in spans] == [
        (0, 39_000),
        (40_000, 79_000),
        (80_000, 89_000),
    ]
    assert spans[0]["text"] == "第0句。第1句。第2句。第3句。"

    wordy = [
        {"start_ms": i * 1_000, "end_ms": i * 1_000 + 900, "text": "字" * 150} for i in range(4)
    ]
    assert [len(span["text"]) for span in merge_sentences(wordy)] == [300, 300]

    long_gap = [
        {"start_ms": 0, "end_ms": 5_000, "text": "开头。"},
        {"start_ms": 70_000, "end_ms": 75_000, "text": "很久以后。"},
    ]
    assert len(merge_sentences(long_gap)) == 2

    english = [
        {"start_ms": 0, "end_ms": 1_000, "text": "Hello world."},
        {"start_ms": 1_000, "end_ms": 2_000, "text": "Next one"},
    ]
    assert merge_sentences(english)[0]["text"] == "Hello world. Next one"


def test_probe_parsing_and_error_classes(tmp_path):
    assert parse_probe(b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"61.5"}}') == (
        61_500,
        True,
    )
    assert parse_probe(b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"N/A"}}') == (
        None,
        True,
    )
    assert parse_probe(b'{"streams":[{"codec_type":"video"}],"format":{}}') == (None, False)
    for bad in (b"not json", b"[]"):
        try:
            parse_probe(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert classify_ffmpeg_error(b"x.m4a: Permission denied") == "permission"
    assert classify_ffmpeg_error("x.m4a: Input/output error") == "io_error"
    assert classify_ffmpeg_error("stream is encrypted") == "unsupported"
    assert classify_ffmpeg_error("moov atom not found") == "corrupt"

    wav = tmp_path / "a.wav"
    fake_wav(wav, 12.5)
    assert wav_seconds(wav) == 12.5
    fake_wav(wav, 0)
    assert wav_seconds(wav) == 0


def test_media_layer_readiness_needs_the_transcriber_script(tmp_path):
    from meeting_workbench.ocr_engines import probe_tools

    settings = type(
        "S", (), {"funasr_python": sys.executable, "material_transcriber": tmp_path / "none.py"}
    )()
    assert (
        probe_tools(settings, system="linux", which=lambda name: f"/usr/bin/{name}").media_script
        is None
    )
    settings.material_transcriber = SCRIPT
    tools = probe_tools(settings, system="linux", which=lambda name: f"/usr/bin/{name}")
    assert tools.media_script == str(SCRIPT) and media_missing(tools) == []


def test_default_transcriber_is_a_background_helper(tmp_path):
    db, settings, *_rest = setup(tmp_path)
    tools = Tools(
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        funasr_python="/venv/bin/python",
        media_script=str(SCRIPT),
    )
    stop = StopFlag()
    media = MaterialMedia(db, settings, _rest[2], tools=lambda: tools, stop=stop)
    helper = media.helper(tools)
    assert helper.argv == ["/venv/bin/python", str(SCRIPT)]
    assert helper.env == {"OMP_NUM_THREADS": "4"} and helper.idle_restart_seconds == 300.0
    assert helper.background is True
    media.close()
    assert media._helper is None


# ---------------------------------------------------------------------- 转写程序本身


FAKE_FUNASR = """
import sys
print("funasr 在 import 的时候乱打印")


class AutoModel:
    def __init__(self, **kwargs):
        print("加载模型", kwargs)
        sys.stderr.write("KWARGS " + repr(sorted(kwargs)) + "\\n")
        self.kwargs = kwargs

    def generate(self, input, **kwargs):
        print("generate 里也打印")
        sys.stderr.write("GENERATE " + repr(sorted(kwargs.items())) + "\\n")
        if "empty" in input:
            return [{"text": ""}]
        if "boom" in input:
            raise RuntimeError("解码失败")
        return [{"text": "你好。再见。", "sentence_info": [
            {"start": 100, "end": 900, "text": "你好。"},
            {"start": 1000, "end": 1800, "text": "再见。"},
        ]}]
"""

FAKE_TORCH = """
import os
import sys


def set_num_threads(n):
    sys.stderr.write(f"THREADS {n} OMP {os.environ.get('OMP_NUM_THREADS')}\\n")
"""


def test_transcriber_script_answers_only_on_stdout(tmp_path):
    fake = tmp_path / "fake"
    (fake / "funasr").mkdir(parents=True)
    (fake / "funasr" / "__init__.py").write_text(FAKE_FUNASR, encoding="utf-8")
    (fake / "torch").mkdir()
    (fake / "torch" / "__init__.py").write_text(FAKE_TORCH, encoding="utf-8")
    requests = "\n".join(
        json.dumps(item, ensure_ascii=False)
        for item in (
            {"id": "a", "wav": "/tmp/seg.wav", "offset_ms": 600_000},
            {"id": "b", "wav": "/tmp/empty.wav", "offset_ms": 0},
            {"id": "c", "wav": "/tmp/boom.wav", "offset_ms": 0},
        )
    )
    env = {**os.environ, "PYTHONPATH": str(fake), "OMP_NUM_THREADS": "16"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=requests + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    answers = [json.loads(line) for line in result.stdout.splitlines()]
    assert answers[0] == {
        "id": "a",
        "ok": True,
        "sentences": [
            {"start_ms": 600_100, "end_ms": 600_900, "text": "你好。"},
            {"start_ms": 601_000, "end_ms": 601_800, "text": "再见。"},
        ],
    }
    assert answers[1] == {"id": "b", "ok": True, "sentences": []}
    assert (
        answers[2]["id"] == "c" and answers[2]["ok"] is False and "解码失败" in answers[2]["error"]
    )
    # 库里的 print 都进了 stderr；不加载 cam++；每段显式要句子时间戳；4 个线程
    assert "乱打印" in result.stderr and "generate 里也打印" in result.stderr
    assert "spk_model" not in result.stderr and "'punc_model'" in result.stderr
    assert (
        "('sentence_timestamp', True)" in result.stderr and "('batch_size_s', 60)" in result.stderr
    )
    assert "THREADS 4 OMP 4" in result.stderr


@pytest.mark.parametrize("when", ["before", "during"])
def test_folder_swapped_for_a_symlink_to_outside_the_root_is_not_transcribed(tmp_path, when):
    """录音所在的那层目录被挪到根目录外、原位换成链接：录音本身一字没变，lstat 也看不出来，要按 realpath 核对。"""
    holder = {}

    def swap():
        folder = holder["root"] / "录音"
        outside = tmp_path / "根目录外" / "录音"
        outside.parent.mkdir(exist_ok=True)
        shutil.move(str(folder), str(outside))
        os.symlink(outside, folder)

    def during(count):
        if when == "during" and count == 1:
            swap()

    transcriber = FakeTranscriber(during=during)
    db, _settings, root, media, _run, _t, _state = media_setup(
        tmp_path, {"访谈.m4a": {"seconds": 1500}}, transcriber=transcriber
    )
    holder["root"] = root
    if when == "before":
        swap()
    media.run_once()
    assert len(transcriber.requests) == (0 if when == "before" else 1)
    assert chunks(db) == []
    assert only(db)["state"] == "pending"
