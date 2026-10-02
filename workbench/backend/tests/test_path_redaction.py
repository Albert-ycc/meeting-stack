"""给浏览器看的错误文本里不带本机的目录结构：绝对路径只留文件名，别的字一个不动。
转写录音页把 relay 的 last_error 当「失败原因」显示，Tailscale 远程打开时这些文字会带出外置盘、
归档目录、暂存目录的真实路径。"""

import copy
import re

import pytest

from meeting_workbench.path_redaction import redact_job_paths, redact_paths

# (原文, 期望)：覆盖带引号的路径、中文、目录名里带空格、一段话里有多个路径、多行、被截断
CASES = [
    pytest.param("audio did not stabilize", "audio did not stabilize", id="没有路径的短文案"),
    pytest.param(
        "main_transcript_bundle_invalid:missing_funasr_json,missing_speaker_map",
        "main_transcript_bundle_invalid:missing_funasr_json,missing_speaker_map",
        id="稳定错误码",
    ),
    pytest.param(
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'/Volumes/外置中枢/会议纪要与录音/260901 医米京东科研仓对接/vm-20260901-100000-a1a1a1a1.m4a'",
        "FileNotFoundError: [Errno 2] No such file or directory: 'vm-20260901-100000-a1a1a1a1.m4a'",
        id="单引号包着的路径，目录名里有中文和空格",
    ),
    pytest.param(
        'OSError: [Errno 2] No such file or directory: "/Users/albert/Albert\'s Recordings/会议 1.m4a"',
        'OSError: [Errno 2] No such file or directory: "会议 1.m4a"',
        id="路径里有单引号时 Python 用双引号，文件名里有空格",
    ),
    pytest.param(
        "OSError: [Errno 18] Cross-device link: "
        "'/Users/albert/Movies/iphone-relay-products/260901 会议/vm.m4a' -> "
        "'/Volumes/外置中枢/会议纪要与录音/260901 会议/vm.m4a'",
        "OSError: [Errno 18] Cross-device link: 'vm.m4a' -> 'vm.m4a'",
        id="两个带引号的路径",
    ),
    pytest.param(
        "Command '['/Users/albert/.venvs/funasr/bin/python', "
        "'/Users/albert/workspace/meeting-stack/transcribe/funasr_transcribe.py', "
        "'/Users/albert/Movies/iphone-relay-products/260901 会议/vm.m4a']' returned non-zero exit status 1.",
        "Command '['python', 'funasr_transcribe.py', 'vm.m4a']' returned non-zero exit status 1.",
        id="子进程的参数列表",
    ),
    pytest.param(
        "No such file or directory: /Volumes/外置中枢/会议纪要与录音/会议.m4a",
        "No such file or directory: 会议.m4a",
        id="没有引号的路径",
    ),
    pytest.param(
        "cannot move /Users/albert/Movies/products/会议.m4a to /Volumes/外置中枢/会议纪要/会议.m4a: disk full",
        "cannot move 会议.m4a to 会议.m4a: disk full",
        id="一句话里两个没有引号的路径",
    ),
    pytest.param(
        "diff /Users/a/x.m4a /Users/b/y.m4a",
        "diff x.m4a y.m4a",
        id="两个路径中间只隔一个空格",
    ),
    pytest.param(
        "archive validation failed for /Volumes/外置中枢/会议纪要与录音/260905 EDC 系统选型/vm-1.m4a (missing srt)",
        "archive validation failed for vm-1.m4a (missing srt)",
        id="没有引号、目录名里有两个空格",
    ),
    pytest.param(
        "missing: /Volumes/外置中枢/会议/260901 医米 会议.m4a",
        "missing: 260901 医米 会议.m4a",
        id="没有引号、空格在文件名里：目录没了，文件名完整",
    ),
    pytest.param(
        "/Volumes/外置中枢/会议.m4a: Permission denied: x/y",
        "会议.m4a: Permission denied: x/y",
        id="路径后面跟冒号和原因，原因里的斜杠不是路径",
    ),
    pytest.param(
        "archive_dir=/Volumes/外置中枢/会议纪要与录音/260901会议",
        "archive_dir=260901会议",
        id="键=值",
    ),
    pytest.param(
        "missing:/Volumes/外置中枢/会议纪要与录音/会议.m4a",
        "missing:会议.m4a",
        id="冒号后面直接跟路径",
    ),
    pytest.param(
        "GET http://127.0.0.1:8765/api/jobs failed; see ./logs/web.log, ../a/b.m4a and transcribe/transcribe.sh",
        "GET http://127.0.0.1:8765/api/jobs failed; see ./logs/web.log, ../a/b.m4a and transcribe/transcribe.sh",
        id="带端口的网址、相对路径不动",
    ),
    pytest.param("确认/驳回 都不可用", "确认/驳回 都不可用", id="中文/中文（主线抽验出的误伤）"),
    pytest.param(
        "停止/取消后重试", "停止/取消后重试", id="中文/中文，后面紧跟文字（主线抽验出的误伤）"
    ),
    pytest.param("成功/失败 两种结果", "成功/失败 两种结果", id="中文/中文"),
    pytest.param("Retry/重试 按钮不可用", "Retry/重试 按钮不可用", id="English/中文"),
    pytest.param("确认/Reject 按钮不可用", "确认/Reject 按钮不可用", id="中文/English"),
    pytest.param("确认/驳回/撤销 三选一", "确认/驳回/撤销 三选一", id="三个中文选项"),
    pytest.param("已处理 3/5 个文件", "已处理 3/5 个文件", id="数字/数字"),
    pytest.param(
        "转写失败：/Volumes/外置中枢/会议纪要与录音/2026-10-01 周会.m4a 不存在",
        "转写失败：2026-10-01 周会.m4a 不存在",
        id="全角冒号后面的路径，文件名里有空格",
    ),
    pytest.param(
        "路径 '/private/tmp/x y/z.wav'",
        "路径 'z.wav'",
        id="中文后面空格再跟带引号的路径",
    ),
    pytest.param(
        "确认/驳回 失败：/Volumes/外置中枢/会议纪要与录音/会议.m4a 不存在",
        "确认/驳回 失败：会议.m4a 不存在",
        id="一句话里既有中文斜杠又有真路径：只换路径",
    ),
    pytest.param(
        "（/Volumes/外置中枢/会议纪要与录音/会议.m4a）读不了",
        "（会议.m4a）读不了",
        id="全角括号后面的路径",
    ),
    pytest.param(
        "Permission denied: ~/Movies/iphone-relay-products/会议.m4a",
        "Permission denied: 会议.m4a",
        id="家目录开头的路径",
    ),
    pytest.param(
        "cannot open file:///Users/albert/Movies/会议%20纪要.m4a",
        "cannot open 会议%20纪要.m4a",
        id="file:// 网址",
    ),
    pytest.param(
        "Traceback (most recent call last):\n"
        '  File "/Users/albert/workspace/meeting-stack/relay/quickstart/relay_watchdog.py", line 2066, in run\n'
        "    txt_path = transcribe(runtime_audio)\n"
        "FileNotFoundError: [Errno 2] No such file or directory: '/Users/albert/Movies/x/vm.m4a'",
        "Traceback (most recent call last):\n"
        '  File "relay_watchdog.py", line 2066, in run\n'
        "    txt_path = transcribe(runtime_audio)\n"
        "FileNotFoundError: [Errno 2] No such file or directory: 'vm.m4a'",
        id="多行的回溯",
    ),
    pytest.param(
        "[Errno 2] No such file or directory: '/Volumes/外置中枢/会议纪要与录音/260901 医米京东科",
        "[Errno 2] No such file or directory: '260901 医米京东科",
        id="被截断、没有收尾引号",
    ),
    pytest.param(
        "not a file: /Volumes/外置中枢/会议纪要与录音/",
        "not a file: 会议纪要与录音",
        id="目录：留最后一级",
    ),
    pytest.param(
        "Content-Type must be application/json; ratio 3/4 and/or 2 / 5; cwd is /",
        "Content-Type must be application/json; ratio 3/4 and/or 2 / 5; cwd is /",
        id="不是路径的斜杠不动",
    ),
    pytest.param(
        "Max retries exceeded with url https://api.deepseek.com/v1/chat/completions (timed out)",
        "Max retries exceeded with url https://api.deepseek.com/v1/chat/completions (timed out)",
        id="网址不动",
    ),
    pytest.param("", "", id="空串"),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_absolute_paths_keep_only_the_file_name(text, expected):
    assert redact_paths(text) == expected


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_redacting_twice_changes_nothing_more(text, expected):
    assert redact_paths(redact_paths(text)) == expected


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_no_directory_survives_and_no_absolute_path_start_is_left(text, expected):
    redacted = redact_paths(text)

    # 这几个目录名只在路径的中间出现过：不管哪种写法，都不能留下
    for directory in ("外置中枢", "iphone-relay-products", "/Users/albert", "/Volumes"):
        assert directory not in redacted
    # 也不剩任何以 / 或 ~/ 开头的路径
    assert not re.search(r"(?<![\w/.~])(?:~/|/)(?=[^\s/'\"])", redacted)


def test_job_errors_are_redacted_and_everything_else_is_left_alone():
    job = {
        "job_id": "job-1",
        "status": "failed",
        "failure_stage": "pending_archive",
        "last_error": "PermissionError: [Errno 1] Operation not permitted: '/Volumes/外置中枢/会议纪要/260901 会议/a.m4a'",
        "audio_path": "/Users/albert/Movies/iphone-relay-products/a.m4a",
        "published_archive_dir": "/Volumes/外置中枢/会议纪要与录音/260901 会议",
        "substates": {
            "whisper": {"status": "failed", "error": "failed to read /Users/albert/Movies/x/a.m4a"},
            "index": {"status": "ready", "error": None},
            "qwen": {"status": "unavailable", "error": "Qwen 离线执行程序不可用"},
        },
        "events": [{"payload": {"error": "x: '/Volumes/外置中枢/a.m4a'"}}],
    }
    before = copy.deepcopy(job)

    redacted = redact_job_paths(job)

    assert redacted["last_error"] == "PermissionError: [Errno 1] Operation not permitted: 'a.m4a'"
    assert redacted["substates"]["whisper"]["error"] == "failed to read a.m4a"
    assert redacted["substates"]["index"] == {"status": "ready", "error": None}
    assert redacted["substates"]["qwen"]["error"] == "Qwen 离线执行程序不可用"
    # 前端没显示的字段不动（回给浏览器的口径由调用方定）；也不改传进来的对象
    for untouched in (
        "job_id",
        "status",
        "failure_stage",
        "audio_path",
        "published_archive_dir",
        "events",
    ):
        assert redacted[untouched] == before[untouched]
    assert job == before


@pytest.mark.parametrize(
    "job",
    [
        {"job_id": "j"},
        {"job_id": "j", "last_error": None},
        {"job_id": "j", "last_error": 123},
        {"job_id": "j", "substates": None},
        {"job_id": "j", "substates": {"whisper": None, "index": "x", "qwen": {"status": "absent"}}},
    ],
)
def test_job_without_text_errors_or_with_odd_shapes_passes_through(job):
    assert redact_job_paths(job) == job
