"""第三期 3a：统一的「会议在转写」信号、材料子进程的共同规矩。"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from meeting_workbench import busy
from meeting_workbench.busy import BusySignal, transcribing_processes
from meeting_workbench.db import Database, utc_now
from meeting_workbench.material_helpers import (
    HelperCrashed,
    HelperProcess,
    HelperStopped,
    HelperTimeout,
    StopFlag,
    cleanup_leftovers,
)

LISTING = """\
  101   101 /Users/me/.venvs/funasr/bin/python /repo/transcribe/funasr_transcribe.py a.wav out 周会 prompt.txt
  102   102 tail -f /repo/out/funasr_transcribe.py.log
  103   103 /Users/me/Library/Python/3.9/bin/whisper a.wav --model turbo --output_dir /x/whisper-ref
  104   104 grep whisper-ref
  105   105 /Users/me/.venvs/mlx-qwen3-asr/bin/mlx-qwen3-asr a.wav
  106   106 /usr/bin/python3 -m meeting_workbench.main
  107   900 /Users/me/.venvs/funasr/bin/python funasr_transcribe.py 不该出现但属于自己的进程组
"""


def test_process_patterns_and_exclusions():
    found = transcribing_processes(LISTING, exclude_groups={900})
    assert [line.split()[0].rsplit("/", 1)[-1] for line in found] == [
        "python",
        "whisper",
        "mlx-qwen3-asr",
    ]
    assert transcribing_processes("  1 1 less funasr_transcribe.py\n") == []
    assert transcribing_processes("  1 1 /bin/zsh -c ls\n") == []


def make_db(tmp_path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    return db


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def test_each_source_triggers_busy(tmp_path):
    db = make_db(tmp_path)
    relay = {"worker": {"current_stage": None}}
    listing = {"value": ""}
    clock = Clock()
    signal = BusySignal(
        db, relay_state=lambda: relay, process_list=lambda: listing["value"], clock=clock
    )
    assert signal() is False

    relay["worker"]["current_stage"] = "transcribing"
    assert signal.reason() == "relay"
    relay["worker"]["current_stage"] = "minutes_generating"  # AI 写纪要不算
    assert signal() is False

    listing["value"] = "  5 5 /x/python funasr_transcribe.py a.wav\n"
    assert signal() is False  # 5 秒缓存
    clock.value += 5.1
    assert signal.reason() == "process"
    listing["value"] = ""
    clock.value += 5.1
    assert signal() is False

    db.execute(
        """INSERT INTO meetings(id, title, status, created_at, updated_at)
           VALUES ('m', '周会', 'completed_unreviewed', ?, ?)""",
        (utc_now(), utc_now()),
    )
    db.execute(
        """INSERT INTO asr_shadow_runs(id, meeting_id, engine, model, state, created_at, updated_at)
           VALUES ('r', 'm', 'qwen3-asr', 'Qwen/Qwen3-ASR-0.6B', 'running', ?, ?)""",
        (utc_now(), utc_now()),
    )
    clock.value += 5.1
    assert signal.reason() == "qwen"


def test_ps_failure_counts_as_busy_and_cli_version_needs_no_relay(tmp_path):
    db = make_db(tmp_path)

    def broken():
        raise subprocess.CalledProcessError(1, ["ps"])

    assert BusySignal(db, process_list=broken).reason() == "ps_failed"
    assert BusySignal(db, process_list=lambda: "").reason() is None


def test_own_material_groups_do_not_count(tmp_path):
    db = make_db(tmp_path)
    listing = "  77 77 /x/python funasr_transcribe.py a.wav\n"
    busy.register_own_group(77)
    try:
        assert BusySignal(db, process_list=lambda: listing)() is False
    finally:
        busy.unregister_own_group(77)
    assert BusySignal(db, process_list=lambda: listing)() is True


def test_semantic_index_default_uses_the_cli_signal(tmp_path):
    from types import SimpleNamespace

    from meeting_workbench.semantic import SemanticIndex

    db = make_db(tmp_path)
    index = SemanticIndex(db, SimpleNamespace(semantic_enabled=True, semantic_model="m"))
    assert isinstance(index.busy_check, BusySignal)
    assert index.busy_check.relay_state is None


# ---------------------------------------------------------------------- 子进程


ECHO = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {root!r})
    from meeting_workbench.material_helpers import serve_json_lines

    def handle(request):
        print("库里的 print 不该混进回答")
        if request.get("sleep"):
            time.sleep(request["sleep"])
        if request.get("stream"):
            def lines():
                for page in range(request["stream"]):
                    yield {{"page": page, "partial": True}}
                yield {{"done": True}}
            return lines()
        return {{"echo": request.get("text")}}

    serve_json_lines(handle)
    """
)


def echo_helper(tmp_path, **kwargs):
    script = tmp_path / "echo.py"
    root = os.path.dirname(os.path.dirname(os.path.abspath(busy.__file__)))
    script.write_text(ECHO.format(root=root), encoding="utf-8")
    return HelperProcess(
        "echo",
        [sys.executable, str(script)],
        data_dir=tmp_path / "data",
        background=False,
        **kwargs,
    )


def test_helper_round_trip_streaming_and_logs(tmp_path):
    helper = echo_helper(tmp_path)
    try:
        assert helper.request({"text": "你好"}, timeout=20)["echo"] == "你好"
        pages = []
        answer = helper.request({"stream": 3}, timeout=20, on_partial=pages.append)
        assert answer["done"] is True and [page["page"] for page in pages] == [0, 1, 2]
        log = (tmp_path / "data" / "logs" / "echo.log").read_text(encoding="utf-8")
        assert "库里的 print" in log
    finally:
        helper.close()


def test_helper_exits_when_stdin_closes(tmp_path):
    helper = echo_helper(tmp_path)
    helper.request({"text": "x"}, timeout=20)
    process = helper._process
    assert process is not None
    process.stdin.close()
    assert process.wait(timeout=10) == 0
    helper.close()


def test_helper_timeout_kills_the_group_and_restarts(tmp_path):
    helper = echo_helper(tmp_path)
    try:
        first_pid = helper.request({"text": "x"}, timeout=20) and helper.pid
        with pytest.raises(HelperTimeout):
            helper.request({"sleep": 30}, timeout=1.5)
        assert helper.pid is None
        assert helper.request({"text": "又好了"}, timeout=20)["echo"] == "又好了"
        assert helper.pid != first_pid
    finally:
        helper.close()


def test_helper_idle_timeout_and_memory_limit(tmp_path):
    helper = echo_helper(tmp_path, rss_of=lambda pid: 2 * 1024 * 1024 * 1024)
    try:
        with pytest.raises(HelperTimeout, match="内存"):
            helper.request({"sleep": 5}, timeout=20)
    finally:
        helper.close()
    helper = echo_helper(tmp_path)
    try:
        with pytest.raises(HelperTimeout):
            helper.request({"sleep": 30}, timeout=60, idle_timeout=1.5)
    finally:
        helper.close()


def test_helper_stop_flag_and_abort(tmp_path):
    stop = StopFlag()
    helper = echo_helper(tmp_path, stop=stop)
    try:
        with pytest.raises(HelperStopped) as info:
            helper.request({"sleep": 30}, timeout=60, abort=lambda: "busy", abort_every=0.2)
        assert info.value.reason == "busy"
        started = time.monotonic()
        import threading

        threading.Timer(0.5, stop.set).start()
        with pytest.raises(HelperStopped):
            helper.request({"sleep": 30}, timeout=60)
        assert time.monotonic() - started < 10
        with pytest.raises(HelperStopped):
            helper.request({"text": "x"}, timeout=5)
    finally:
        helper.close()


def test_helper_restarts_after_max_requests(tmp_path):
    helper = echo_helper(tmp_path, max_requests=2)
    try:
        helper.request({"text": "1"}, timeout=20)
        pid = helper.pid
        helper.request({"text": "2"}, timeout=20)
        assert helper.pid == pid
        helper.request({"text": "3"}, timeout=20)
        assert helper.pid != pid
    finally:
        helper.close()


def test_helper_crash_is_reported(tmp_path):
    script = tmp_path / "crash.py"
    script.write_text("import sys\nsys.stdin.readline()\nsys.exit(3)\n", encoding="utf-8")
    helper = HelperProcess(
        "crash", [sys.executable, str(script)], data_dir=tmp_path / "data", background=False
    )
    with pytest.raises(HelperCrashed):
        helper.request({"x": 1}, timeout=20)
    helper.close()


def test_cleanup_leftovers_kills_old_material_asr_and_removes_temp_files(tmp_path):
    db = make_db(tmp_path)
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "funasr_material.py"],
        start_new_session=True,
    )
    db.execute(
        """INSERT INTO material_contents(content_key, layer, created_at, updated_at)
           VALUES ('q2:a', 'media', 'x', 'x')"""
    )
    db.execute(
        "INSERT INTO material_media_jobs(content_key, done_ms, pid, updated_at) VALUES ('q2:a', 600000, ?, 'x')",
        (sleeper.pid,),
    )
    leftover = tmp_path / "data" / "material-asr" / "q2a-part3.wav"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"RIFF")
    (tmp_path / "data" / "material-ocr-tmp" / "job").mkdir(parents=True)
    listing = f"  {sleeper.pid} {sleeper.pid} {sys.executable} -c sleep funasr_material.py\n"
    result = cleanup_leftovers(db, tmp_path / "data", process_list=lambda: listing)
    assert result == {"killed": 1, "removed": 2}
    assert sleeper.wait(timeout=10) != 0
    job = db.query_one("SELECT done_ms, pid FROM material_media_jobs")
    assert job == {"done_ms": 600000, "pid": None}  # 进度保留
    assert not leftover.exists()
