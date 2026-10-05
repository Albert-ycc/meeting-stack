"""全量测试的公共设置。

材料内容循环（第三期）会编译 Vision 程序、调 textutil、起读取进程。起完整 lifespan 的测试很多，
装机时在 Mac 上跑全量测试不该碰这些，所以默认关掉（pydantic-settings 会读环境变量）；要测循环的
测试自己传 material_content_enabled=True。

第四期（4a）的测试护栏，都在导入被测模块之前设好：
- 第四期的两个循环默认不启动，要测循环的测试自己传 links_enabled=True；
- AI 的 key 文件指到不存在的路径，要 key 的测试自己写临时 key 文件传进去；AI 地址指到
  127.0.0.1:9，万一漏进一个真 key 也连不到任何地方；
- 飞书全部清空，测试里不再读 .env；
- Host 白名单放行 TestClient 的默认主机名 testserver（生产默认值里没有它）；
- 自动生效的 _no_real_llm 拦下发给真 AI 的请求，teardown 时记下过就让测试失败。测本机假 AI 服务
  的测试标 allow_local_llm（只放行本机地址）；断言拦下了的测试自己把 fixture 的列表清空。
"""

from __future__ import annotations

import os
import time
import urllib.request
from urllib.parse import urlsplit

import pytest

os.environ["MEETING_WORKBENCH_MATERIAL_CONTENT_ENABLED"] = "0"
os.environ["MEETING_WORKBENCH_LINKS_ENABLED"] = "0"
os.environ["MEETING_WORKBENCH_LLM_API_KEY_FILE"] = "/nonexistent/meeting-workbench-test-key"
os.environ["MEETING_WORKBENCH_LLM_API_BASE"] = "http://127.0.0.1:9/v1"
os.environ["MEETING_WORKBENCH_LARK_WEBHOOK_URL"] = ""
os.environ["MEETING_WORKBENCH_LARK_CHAT_ID"] = ""
os.environ["MEETING_WORKBENCH_LARK_APP_ID"] = ""
os.environ["MEETING_WORKBENCH_LARK_APP_SECRET_FILE"] = "/nonexistent/meeting-workbench-lark-secret"
os.environ["MEETING_WORKBENCH_LARK_CLI_BIN"] = "/nonexistent/meeting-workbench-test-lark-cli"
os.environ["MEETING_WORKBENCH_ALLOWED_HOSTS"] = "testserver"
# 扫描空闲退避默认关：老用例把 scan_interval_seconds 设成零点几秒、等扫描循环转几圈，间隔被拉长就等不到了。
# 要测退避的用例（test_scan_idle_backoff.py）自己传 scan_idle_max_interval_seconds。
os.environ["MEETING_WORKBENCH_SCAN_IDLE_MAX_INTERVAL_SECONDS"] = "0"

from meeting_workbench.config import Settings

Settings.model_config["env_file"] = None

pytest_plugins = ["pytester"]

REAL_AI_HOSTS = frozenset({"api.deepseek.com", "api.anthropic.com"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
NO_REAL_AI = "测试里不许调真的 AI"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "allow_local_llm: 放行发给本机假 AI 服务的请求（真 AI 的主机照样拦）"
    )


def _ai_request(url: object) -> tuple[str, str] | None:
    """发给 AI 的请求回 (地址, 主机)，别的回 None。地址不带查询串。"""
    full_url = url.full_url if isinstance(url, urllib.request.Request) else str(url)
    parts = urlsplit(full_url)
    host = (parts.hostname or "").lower()
    if host in REAL_AI_HOSTS or parts.path.rstrip("/").endswith("/chat/completions"):
        return f"{parts.scheme}://{parts.netloc}{parts.path}", host
    return None


@pytest.fixture(autouse=True)
def _no_real_llm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """包一层 urllib.request.urlopen：发给真 AI 的请求先记下地址，再抛 AssertionError。调用方
    怎么捕获都没关系，teardown 时列表不为空就让测试失败。自己打桩 urlopen 的测试在自己的范围里
    覆盖它。"""
    blocked: list[str] = []
    real_urlopen = urllib.request.urlopen
    allow_local = request.node.get_closest_marker("allow_local_llm") is not None

    def guarded_urlopen(url, *args, **kwargs):
        found = _ai_request(url)
        if found is not None and not (allow_local and found[1] in LOCAL_HOSTS):
            blocked.append(found[0])
            raise AssertionError(NO_REAL_AI)
        return real_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)
    yield blocked
    if blocked:
        pytest.fail(f"{NO_REAL_AI}：{blocked[0]}")


MACHINE_PROGRAMS = frozenset(
    {"tesseract", "xcode-select", "xcrun", "swiftc", "sysctl", "ffmpeg", "ffprobe"}
)


@pytest.fixture(autouse=True)
def _no_machine_programs(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """本机装的认字、编译、音视频程序在测试里一律当没装：找程序（PATH、Homebrew 两个目录）找不到，真要起也起不来
    （抛 FileNotFoundError，和没装时一样）。不然结果随跑它的机器变，Mac 上还会真编译 Vision 程序。
    测试自己在临时目录里造的假程序照常能起（含 --basetemp 指到系统临时目录以外的时候）；要假程序的用例
    照旧自己传 which / run。"""
    import shutil
    import subprocess
    import tempfile

    from meeting_workbench import ocr_engines

    temp_roots = tuple(
        os.path.realpath(folder) + os.sep
        for folder in (tempfile.gettempdir(), tmp_path_factory.getbasetemp())
    )

    def machine_program(name: object) -> bool:
        text = os.fsdecode(name) if isinstance(name, str | bytes | os.PathLike) else ""
        if os.path.basename(text) not in MACHINE_PROGRAMS:
            return False
        return not os.path.realpath(text).startswith(temp_roots)

    real_which = shutil.which

    def guarded_which(name, *args, **kwargs):
        return None if machine_program(name) else real_which(name, *args, **kwargs)

    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            argv = [args] if isinstance(args, str | bytes | os.PathLike) else list(args)
            # 前四个看全：taskpolicy -b <程序>、nice -n 10 <程序>
            if any(machine_program(arg) for arg in argv[:4]):
                raise FileNotFoundError(f"测试里不起本机程序：{argv[0]}")
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(shutil, "which", guarded_which)
    monkeypatch.setattr(ocr_engines, "HOMEBREW_BINS", ())
    # 默认参数在定义时就绑了真的 shutil.which，换掉模块属性挡不住
    for function in (
        ocr_engines.find_tool,
        ocr_engines.probe_tools,
        ocr_engines.OcrEngines.__init__,
    ):
        monkeypatch.setitem(function.__kwdefaults__, "which", guarded_which)


@pytest.fixture(params=("America/Los_Angeles", "Asia/Shanghai", "UTC"))
def process_zone(request: pytest.FixtureRequest):
    """进程时区钉成太平洋、上海、UTC 各跑一遍（TZ 加 tzset，SQLite 的 'localtime' 也跟着变）。
    结果按「本机日历」或「北京日历」换算的用例要求它：生产在太平洋，开发常在 UTC，用户在北京。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()
