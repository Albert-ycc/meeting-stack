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

写盘量（2026-10-05 全量一轮逻辑写盘 20GiB，是本机最大的写盘来源）：自动生效的 _cheap_databases 让用例里
的库少写盘，查询语义不变，细节见它的文档。要看库文件、-wal / -shm 真实生命周期的用例标 real_database_files。
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
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

from meeting_workbench import db as db_module
from meeting_workbench.config import Settings
from meeting_workbench.db import Database

Settings.model_config["env_file"] = None
REAL_INITIALIZE = Database.initialize

pytest_plugins = ["pytester"]

REAL_AI_HOSTS = frozenset({"api.deepseek.com", "api.anthropic.com"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
NO_REAL_AI = "测试里不许调真的 AI"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "allow_local_llm: 放行发给本机假 AI 服务的请求（真 AI 的主机照样拦）"
    )
    config.addinivalue_line(
        "markers",
        "real_database_files: 用例要看库文件本身或 -wal / -shm 的真实生命周期（最后一个连接关掉时做检查点、"
        "删边车文件，直接读库文件的大小和修改时间），_cheap_databases 不给它常驻空闲连接",
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


def _clone_file(source: Path, destination: Path) -> None:
    """复制库文件。macOS 的 APFS 上用 clonefile：不拷数据块，改到哪页才写哪页，克隆本身几乎不产生写入；
    不支持的卷、别的系统退回普通复制（Linux 的文件系统支持 reflink 时同样不拷数据）。"""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(destination)
        if code not in (errno.ENOTSUP, errno.EXDEV):
            raise OSError(code, os.strerror(code), str(destination))
    shutil.copyfile(source, destination)


# 生产库是默认的 4096。WAL 里每次提交写的是改到的整页，页小了每次提交少写 3 到 4 倍（实测 1000 次单行
# 插入 21MB → 8.8MB），页大小只改存储的分页，语句的结果不变。
TEST_PAGE_SIZE = 1024


@pytest.fixture(scope="session")
def _schema_template():
    """整个会话只真的建一次库：建表、建触发器要提交三百多次，每次都往 -wal 追加整页，一个空库写盘约
    4.5MB（2026-10-05 实测，库文件本身不到 1MB），全量里有两千多个用例各建一遍。

    模板就是真 initialize() 在空库上跑完的结果。app_state 里的六行是建库那一刻的时间戳（links_since 等）
    和各自的默认值，留在模板里所有克隆都会带着会话开始的时间，所以清掉：克隆之后用例路径上照样会跑真
    initialize()，INSERT OR IGNORE 按当时的 utc_now() 把这六行原样补回来。

    返回 (模板路径, 模板建成时的 (SCHEMA, SCHEMA_VERSION))。"""
    with tempfile.TemporaryDirectory(prefix="meeting-workbench-schema-template-") as folder:
        template = Path(folder) / "template.sqlite3"
        # 页大小只能在库里还没有表的时候定；WAL 模式一并先落下，之后真 initialize() 看到的是一个已经
        # 是 WAL 的空库
        connection = sqlite3.connect(template)
        try:
            connection.execute(f"PRAGMA page_size={TEST_PAGE_SIZE}")
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        REAL_INITIALIZE(Database(template))
        connection = sqlite3.connect(template)
        try:
            connection.execute("DELETE FROM app_state")
            connection.commit()
            assert connection.execute("PRAGMA page_size").fetchone()[0] == TEST_PAGE_SIZE
        finally:
            connection.close()
        assert not Path(f"{template}-wal").exists(), "模板必须是单个文件"
        yield template, (db_module.SCHEMA, db_module.SCHEMA_VERSION)


@pytest.fixture(autouse=True)
def _cheap_databases(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """用例里的库少写盘，库里的内容和语句的结果跟原来一样。四处：

    1. 新库不再从零建表：Database.initialize() 碰到不存在的库文件时，先把模板克隆过去，再照常跑一遍真
       initialize()（建表语句都是 IF NOT EXISTS，剩下的迁移、补种子行照样执行）。
       只在表结构和版本号都还是模板建成时的样子时才克隆（用例改了 SCHEMA、SCHEMA_VERSION 就走原路）。
    2. 页大小 1024（生产是默认的 4096），理由见 TEST_PAGE_SIZE。
    3. 每条连接 temp_store=MEMORY：带触发器的 INSERT … ON CONFLICT 要开语句日志，默认放在磁盘临时文件里，
       一条语句写 5KB 上下（test_decisions_log 里建 16 万条 relations，光这一项写 800MB）；放内存里一个字节
       不落盘。用例里的库很小，内存不是问题。
    4. 给这个库常驻一个空闲连接，用例结束时关掉，和生产里 Database.held_open() 一样：
       用例里的 db.execute / db.query_one 每次都是用完就关的短连接，最后一个连接关掉时 SQLite 要把 -wal
       整个写进主文件再删掉 -wal / -shm，下一次再建，一条语句要写 130KB 左右（实测 200 条插入 26MB，
       常驻一个连接后 6.6MB）。

    标了 real_database_files 的用例不做第 4 项：它们看库文件本身或 -wal / -shm 的真实生命周期（最后一个
    连接关掉时做检查点、删边车文件）；第 1 到 3 项不改变这些行为，对谁都生效。"""
    if getattr(Database.initialize, "cheap_databases", False):
        # pytester 里嵌套跑的内层会话复制了这份 conftest，外层已经换过了，不再套第二层
        yield
        return

    template, template_schema = request.getfixturevalue("_schema_template")
    hold = request.node.get_closest_marker("real_database_files") is None
    held: dict[str, sqlite3.Connection] = {}
    lock = threading.Lock()
    real_connect = Database.connect

    def connect(self: Database) -> sqlite3.Connection:
        connection = real_connect(self)
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def hold_open(path: Path) -> None:
        key = os.path.realpath(path)
        with lock:
            if key not in held:
                # 线程池里建库、主线程收尾的用例有，所以不限线程
                connection = sqlite3.connect(path, check_same_thread=False)
                connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()
                held[key] = connection

    def initialize(self: Database, **kwargs) -> None:
        sidecars = ("", "-wal", "-shm", "-journal")
        fresh = not any(os.path.lexists(f"{self.path}{suffix}") for suffix in sidecars)
        if fresh and (db_module.SCHEMA, db_module.SCHEMA_VERSION) == template_schema:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _clone_file(template, self.path)
            if hold:
                # 克隆来的一定是好库，先常驻再跑 initialize：它里面的短连接就不是最后一个，关的时候不做检查点
                hold_open(self.path)
            REAL_INITIALIZE(self, **kwargs)
            return
        REAL_INITIALIZE(self, **kwargs)
        if hold:
            hold_open(self.path)

    initialize.cheap_databases = True
    monkeypatch.setattr(Database, "connect", connect)
    monkeypatch.setattr(Database, "initialize", initialize)
    yield
    for connection in held.values():
        try:
            connection.close()
        except sqlite3.Error:
            pass  # 用例把库文件删了或挪了：连接指着的是旧文件，关不干净不影响结果
