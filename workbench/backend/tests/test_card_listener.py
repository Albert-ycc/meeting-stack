"""飞书卡片回调监听 workbench/card_listener.py：配置读法、只认本人、日志轮转。

全部在临时 HOME 下加载，不碰真实的 ~/.meeting-workbench，不调 lark-cli，不连飞书。
.env 只在脚本作为入口起来时才读（和 relay 一致），所以「读 .env」「缺配置退出」这类要走入口的用例，
在临时拼出来的仓库目录里起子进程跑，读不到本机真实的 .env。
"""

from __future__ import annotations

import importlib.util
import io
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
import types
import urllib.error
from pathlib import Path

import pytest

from meeting_workbench import config
from meeting_workbench.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
LISTENER = REPO_ROOT / "workbench" / "card_listener.py"

CHAT_KEY = "MEETING_WORKBENCH_LARK_CHAT_ID"
OWNER_KEY = "MEETING_WORKBENCH_LARK_OWNER_OPEN_ID"
CLI_KEY = "MEETING_WORKBENCH_LARK_CLI_BIN"
EVENTS_KEY = "MEETING_STACK_CARD_EVENTS_DIR"
LISTENER_KEYS = (CHAT_KEY, OWNER_KEY, CLI_KEY, EVENTS_KEY)
CONFIG_MISSING_EXIT = 78
GOOD_ENV = {CHAT_KEY: "oc_test_group", OWNER_KEY: "ou_test_owner"}


class StopLoop(BaseException):
    """跳出 main() 的死循环；main 只吞 Exception，所以用 BaseException。"""


def one_pass_clock():
    """主循环里的 time：走完一圈就跳出去。凡是会真调 main() 的用例都套上，实现写错了也只是失败，不会死循环卡住整轮。"""

    def stop(seconds):
        raise StopLoop

    return types.SimpleNamespace(monotonic=lambda: 0.0, sleep=stop)


def load_listener(monkeypatch, home: Path, env: dict[str, str] | None = None):
    """在临时 HOME 下导入监听脚本。不是以入口方式导入，所以不读 .env；监听进程用的几项环境变量先清掉再按 env 设。"""
    monkeypatch.setenv("HOME", str(home))
    for key in LISTENER_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    # 脚本自己会往 sys.path 最前面插后端目录，拷一份让 monkeypatch 在用例结束时还原
    monkeypatch.setattr(sys, "path", list(sys.path))
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    spec = importlib.util.spec_from_file_location(f"card_listener_{time.monotonic_ns()}", LISTENER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    try:
        spec.loader.exec_module(module)
    finally:
        # 模块顶层的 basicConfig 在根 logger 还没有处理器时会往上挂一个指向临时 HOME 的，摘掉，免得影响别的用例
        for handler in root.handlers[:]:
            if handler not in handlers_before:
                root.removeHandler(handler)
                handler.close()
    return module


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".meeting-workbench" / "logs").mkdir(parents=True)
    return path


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    """轮转用例自己的日志目录：和 home 分开，列目录时只看得到日志文件。"""
    path = tmp_path / "rotation"
    path.mkdir()
    return path


@pytest.fixture
def listener(monkeypatch, home):
    return load_listener(monkeypatch, home, GOOD_ENV)


@pytest.fixture
def faked(listener):
    """把发飞书的两个出口换成记录器：用例里没有任何东西会走到 lark-cli。"""
    sent: list[str] = []
    patched: list[str] = []
    listener._send_text = lambda text: sent.append(text) or {}
    listener._update_card = lambda message_id, card: patched.append(message_id) or {"code": 0}
    return listener, sent, patched


def card_event(operator: dict | None, *, action: str = "confirm") -> dict:
    event = {
        "action": {"value": {"action": action, "task_id": "t1", "extraction_id": 7}},
        "context": {"open_message_id": "om_1"},
    }
    if operator is not None:
        event["operator"] = operator
    return {"header": {"event_id": "e1"}, "event": event}


class FakeClient:
    def __init__(self, confirm_result=None, confirm_error: Exception | None = None):
        self.confirm_result = confirm_result
        self.confirm_error = confirm_error
        self.confirmed: list[str] = []

    def confirm(self, task_id):
        self.confirmed.append(task_id)
        if self.confirm_error is not None:
            raise self.confirm_error
        return self.confirm_result

    def reject(self, task_id):
        raise AssertionError("不该走到驳回")

    def list_tasks(self, extraction_id):
        return [{"id": "t1", "title": "发合同", "status": "confirmed", "meeting_title": "周会"}]


# —— 配置：ID 和飞书 CLI 路径不再写在源码里 ——


def test_owner_chat_and_cli_come_from_the_environment(monkeypatch, home):
    module = load_listener(
        monkeypatch,
        home,
        {CHAT_KEY: " oc_group ", OWNER_KEY: "ou_me", CLI_KEY: "/opt/bin/lark-cli"},
    )
    assert (module.CHAT_ID, module.OWNER_OPEN_ID, module.LARK_CLI) == (
        "oc_group",
        "ou_me",
        "/opt/bin/lark-cli",
    )


def test_cli_defaults_to_the_name_found_on_path(monkeypatch, home):
    module = load_listener(monkeypatch, home, GOOD_ENV)
    assert module.LARK_CLI == "lark-cli"


def test_source_carries_no_ids_to_edit():
    # 要改源码才能配置，生产就只能跑一份改过源码的副本；ID 不许再写回源码里
    source = LISTENER.read_text(encoding="utf-8")
    assert re.search(r"[\"']o[uc]_", source) is None


def test_failure_notice_goes_to_the_configured_chat(listener, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(listener, "_cli", lambda *args: calls.append(args) or {"code": 0})
    listener._send_text("出事了")
    method, path, data = calls[0]
    assert (method, path, data["receive_id"]) == (
        "POST",
        "/open-apis/im/v1/messages",
        "oc_test_group",
    )


def test_events_dir_reads_env_and_keeps_default(monkeypatch, home):
    module = load_listener(monkeypatch, home, GOOD_ENV)
    assert module.EVENTS_DIR == home / ".meeting-workbench" / "card-events"
    assert module.PROCESSED_DIR == module.EVENTS_DIR / "processed"
    custom = home / "custom events"
    module = load_listener(monkeypatch, home, {**GOOD_ENV, EVENTS_KEY: str(custom)})
    assert module.EVENTS_DIR == custom
    assert module.PROCESSED_DIR == custom / "processed"
    module = load_listener(monkeypatch, home, {**GOOD_ENV, EVENTS_KEY: "~/x-events"})
    assert module.EVENTS_DIR == home / "x-events"


# —— 没配齐就报人话退出，不能带着空 ID 或占位符跑起来 ——


@pytest.mark.parametrize(
    ("env", "missing", "present"),
    [
        ({}, (CHAT_KEY, OWNER_KEY), ()),
        ({CHAT_KEY: "oc_group"}, (OWNER_KEY,), (CHAT_KEY,)),
        ({OWNER_KEY: "ou_me"}, (CHAT_KEY,), (OWNER_KEY,)),
        # 只有空白也算没配
        ({CHAT_KEY: "  ", OWNER_KEY: "\t"}, (CHAT_KEY, OWNER_KEY), ()),
    ],
)
def test_main_refuses_to_start_without_owner_and_chat(
    monkeypatch, home, capsys, env, missing, present
):
    module = load_listener(monkeypatch, home, env)
    started: list[str] = []
    monkeypatch.setattr(module, "time", one_pass_clock())
    monkeypatch.setattr(module, "_ensure_subscriber", lambda: started.append("subscriber"))

    with pytest.raises(SystemExit) as stopped:
        module.main()

    assert stopped.value.code == CONFIG_MISSING_EXIT
    message = capsys.readouterr().err
    for key in missing:
        assert key in message
    for key in present:
        assert key not in message
    assert ".env" in message  # 告诉人写到哪
    assert started == []
    assert not module.EVENTS_DIR.exists()  # 什么都没动


def test_main_starts_once_both_are_configured(monkeypatch, listener):
    monkeypatch.setattr(listener, "time", one_pass_clock())
    monkeypatch.setattr(listener, "_ensure_subscriber", lambda: None)
    monkeypatch.setattr(listener, "_rotate_subscribe_log", lambda: None)
    with pytest.raises(StopLoop):
        listener.main()
    assert listener.EVENTS_DIR.is_dir()


def make_repo_copy(tmp_path: Path) -> Path:
    """临时拼一个仓库目录：监听脚本拷过去，后端目录链过去。.env 放哪份由用例定，本机真实的 .env 读不到。"""
    repo = tmp_path / "repo"
    (repo / "workbench").mkdir(parents=True)
    shutil.copyfile(LISTENER, repo / "workbench" / "card_listener.py")
    (repo / "workbench" / "backend").symlink_to(REPO_ROOT / "workbench" / "backend")
    return repo


def process_env(home: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEETING_")}
    # 万一起到了订阅子进程，也只会找一个不存在的程序
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **{CLI_KEY: "/nonexistent/lark-cli"})
    env.update(extra)
    return env


def test_fresh_home_import_creates_log_dir(tmp_path):
    # 新机器上日志目录还不存在，模块顶层配日志时不能因此起不来。
    # 用子进程导入：同进程里根 logger 已被 pytest 配置过，basicConfig 会变成空操作，测不出来。
    home = tmp_path / "fresh-home"
    home.mkdir()
    code = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('m', {str(LISTENER)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(module.LOG_FILE.parent.is_dir())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=process_env(home),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip() == "True"


def test_script_without_config_exits_with_a_readable_message(tmp_path):
    repo = make_repo_copy(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [sys.executable, str(repo / "workbench" / "card_listener.py")],
        env=process_env(home),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == CONFIG_MISSING_EXIT, result.stderr[-500:]
    assert CHAT_KEY in result.stderr and OWNER_KEY in result.stderr
    assert "Traceback" not in result.stderr


def test_script_reads_both_dotenv_files_and_logs_only_key_names(tmp_path):
    repo = make_repo_copy(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / ".env").write_text(f"{CHAT_KEY}=oc_secret_chat_from_root\n", encoding="utf-8")
    (repo / "workbench" / ".env").write_text(f"{OWNER_KEY}=ou_secret_owner\n", encoding="utf-8")
    log_file = home / ".meeting-workbench" / "logs" / "card-listener.log"

    process = subprocess.Popen(
        [sys.executable, str(repo / "workbench" / "card_listener.py")],
        env=process_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            if log_file.exists() and "card_listener 启动" in log_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.1)
        assert process.poll() is None, "进程提前退出了"
        text = log_file.read_text(encoding="utf-8")
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=30)
    assert "card_listener 启动" in text
    # 日志里只留从 .env 取了哪几项，不留值
    assert CHAT_KEY in text and OWNER_KEY in text
    assert "oc_secret_chat_from_root" not in text and "ou_secret_owner" not in text
    assert "oc_secret_chat_from_root" not in stderr and "ou_secret_owner" not in stderr


# —— .env：位置和读法跟工作台一致，环境变量优先 ——


def write_env(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_dotenv_locations_are_the_same_as_the_workbench_settings(listener):
    assert listener.ENV_FILES == config.ENV_FILES


def test_dotenv_fills_only_the_listeners_keys_and_never_overrides_the_environment(
    listener, tmp_path
):
    root_env = write_env(
        tmp_path / ".env",
        f"{CHAT_KEY}=oc_from_root\n{OWNER_KEY}=ou_from_root\n{EVENTS_KEY}=/root/events\n",
    )
    workbench_env = write_env(
        tmp_path / "workbench" / ".env",
        f"{OWNER_KEY}=ou_from_workbench\n"
        "MEETING_WORKBENCH_LARK_WEBHOOK_URL=https://example.invalid/hook\n"
        "MEETING_WORKBENCH_LLM_API_KEY_FILE=/somewhere/key\n",
    )
    environ = {EVENTS_KEY: "/from/environment"}

    applied = listener._load_env_files((root_env, workbench_env), environ)

    assert environ == {
        EVENTS_KEY: "/from/environment",  # 已在环境变量里的不覆盖
        CHAT_KEY: "oc_from_root",
        OWNER_KEY: "ou_from_workbench",  # 两份都写了，workbench/ 下的优先
    }
    assert applied == [CHAT_KEY, OWNER_KEY]  # webhook、key 文件路径是工作台的，监听进程不取


def test_dotenv_empty_values_count_as_not_set_and_a_blank_environment_variable_still_wins(
    listener, tmp_path
):
    env_file = write_env(tmp_path / ".env", f"{CHAT_KEY}=\n{OWNER_KEY}=ou_me\n")
    environ = {OWNER_KEY: ""}  # 环境变量里设成空串：显式留空，和工作台读 Settings 时一样压过 .env
    listener._load_env_files((env_file,), environ)
    assert environ == {OWNER_KEY: ""}


def test_missing_dotenv_files_are_fine(listener, tmp_path):
    environ: dict[str, str] = {}
    assert listener._load_env_files((tmp_path / "no.env", tmp_path / "x" / ".env"), environ) == []
    assert environ == {}


@pytest.mark.parametrize(
    "line",
    [
        f"{CHAT_KEY}=oc_plain",
        f"export {CHAT_KEY}=oc_exported",
        f'{CHAT_KEY}="oc_double"',
        f"{CHAT_KEY}='oc_single'",
        f"{CHAT_KEY}=oc_trailing   # 行尾注释",
        f"  {CHAT_KEY} = oc_spaced  ",
    ],
)
def test_chat_id_is_read_like_the_workbench_does(monkeypatch, listener, tmp_path, line):
    """同一份 .env，监听进程和工作台读出来的群要是同一个，否则卡发到一个群、失败提示发到另一个群。"""
    env_file = write_env(tmp_path / ".env", line + "\n")
    monkeypatch.delenv(CHAT_KEY, raising=False)  # conftest 把它设成了空串，环境变量会压过 .env
    expected = Settings(_env_file=(env_file,)).lark_chat_id
    assert expected.startswith("oc_")
    environ: dict[str, str] = {}
    listener._load_env_files((env_file,), environ)
    assert environ[CHAT_KEY] == expected


def test_importing_the_module_does_not_read_dotenv_files(monkeypatch, home):
    # 本机真实的 .env 带不进用例；只有作为入口起来时才读
    import dotenv

    def boom(*args, **kwargs):
        raise AssertionError("导入时不该读 .env")

    monkeypatch.setattr(dotenv, "dotenv_values", boom)
    load_listener(monkeypatch, home, GOOD_ENV)


# —— 只认本人：缺 operator.open_id、对不上，一律拒绝 ——


@pytest.mark.parametrize(
    "operator",
    [None, {}, {"open_id": ""}, {"open_id": None}, {"open_id": "ou_someone_else"}],
)
def test_event_from_anyone_but_the_owner_is_rejected(faked, operator):
    module, sent, patched = faked
    client = FakeClient(confirm_result={"status": "confirmed", "extraction_id": 7})
    module.handle_event(client, card_event(operator))
    assert client.confirmed == []
    assert patched == [] and sent == []


@pytest.mark.parametrize("operator", [None, {}, {"open_id": ""}])
def test_nothing_matches_when_the_owner_is_not_configured(faked, operator):
    # 空字符串和「缺 open_id 时取到的空字符串」相等：没配本人时不能因此放行
    module, sent, patched = faked
    module.OWNER_OPEN_ID = ""
    client = FakeClient(confirm_result={"status": "confirmed", "extraction_id": 7})
    module.handle_event(client, card_event(operator))
    assert client.confirmed == []


def test_owner_event_is_still_handled(faked):
    module, sent, patched = faked
    client = FakeClient(confirm_result={"status": "confirmed", "extraction_id": 7})
    module.handle_event(client, card_event({"open_id": module.OWNER_OPEN_ID}))
    assert client.confirmed == ["t1"]
    assert patched == ["om_1"]


def test_confirm_success_with_task_detail_refreshes_card(faked):
    # 确认成功返回的任务对象自带业务字段 detail（任务详情），不能被当成失败。
    module, sent, patched = faked
    client = FakeClient(
        confirm_result={
            "id": "t1",
            "status": "confirmed",
            "extraction_id": 7,
            "detail": "周五前把合同条款发给法务",
        }
    )
    module.handle_event(client, card_event({"open_id": module.OWNER_OPEN_ID}))
    assert sent == []
    assert patched == ["om_1"]


def test_workbench_http_error_raises_and_is_reported(faked):
    module, sent, patched = faked
    client = module.ShengdangClient()
    client.token = "csrf"

    def fail(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, io.BytesIO('{"detail": "任务不存在"}'.encode())
        )

    client.opener.open = fail
    with pytest.raises(Exception, match="任务不存在") as caught:
        client.confirm("t1")

    module.handle_event(
        FakeClient(confirm_error=caught.value), card_event({"open_id": module.OWNER_OPEN_ID})
    )
    assert len(sent) == 1
    assert "任务不存在" in sent[0]
    assert patched == []


# —— 订阅子进程 ——


def test_subscriber_runs_as_bot_and_keeps_its_output(monkeypatch, listener):
    popen_calls: list[tuple] = []
    monkeypatch.setattr(
        listener,
        "subprocess",
        types.SimpleNamespace(
            Popen=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    listener._start_subscriber()
    [(args, kwargs)] = popen_calls
    command = args[0]
    assert command[command.index("--as") + 1] == "bot"
    assert kwargs["stdout"] is not subprocess.DEVNULL
    assert kwargs["stderr"] is not subprocess.DEVNULL


def test_main_loop_rechecks_subscriber_and_rotates_its_log_periodically(monkeypatch, listener):
    # 长连接子进程断了要能被重新拉起：不能只在启动时检查一次；它的日志也要跟着周期性轮转
    clock = iter(range(0, 100_000, 61))
    sleeps = {"n": 0}

    def fake_sleep(seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise StopLoop

    checks: list[str] = []
    monkeypatch.setattr(
        listener, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=fake_sleep)
    )
    monkeypatch.setattr(listener, "_ensure_subscriber", lambda: checks.append("ensure"))
    monkeypatch.setattr(listener, "_rotate_subscribe_log", lambda: checks.append("rotate"))
    with pytest.raises(StopLoop):
        listener.main()
    # 每一轮都是先查订阅进程、再轮转：轮转出了岔子也不会挡在守护前面
    assert len(checks) >= 4
    assert checks == ["ensure", "rotate"] * (len(checks) // 2)


# —— 日志轮转 ——


def test_own_log_is_size_bounded_rotating_file(listener):
    assert listener.LOG_MAX_BYTES > 0
    # 备份数为 0 时 RotatingFileHandler 永远不轮转，等于没限制
    assert listener.LOG_BACKUP_COUNT >= 1
    handler = listener._log_handler()
    try:
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert Path(handler.baseFilename) == listener.LOG_FILE
        assert (handler.maxBytes, handler.backupCount) == (
            listener.LOG_MAX_BYTES,
            listener.LOG_BACKUP_COUNT,
        )
    finally:
        handler.close()


def test_own_log_really_rotates(monkeypatch, listener, tmp_path):
    monkeypatch.setattr(listener, "LOG_FILE", tmp_path / "logs" / "card-listener.log")
    monkeypatch.setattr(listener, "LOG_MAX_BYTES", 400)
    handler = listener._log_handler()
    probe = logging.Logger("card-listener-rotation-probe")
    probe.addHandler(handler)
    try:
        for index in range(200):
            probe.warning("第 %03d 条 %s", index, "x" * 50)
    finally:
        handler.close()
    names = sorted(path.name for path in (tmp_path / "logs").iterdir())
    expected = ["card-listener.log"] + [
        f"card-listener.log.{n}" for n in range(1, listener.LOG_BACKUP_COUNT + 1)
    ]
    assert names == sorted(expected)
    for path in (tmp_path / "logs").iterdir():
        assert path.stat().st_size <= 400 + 120  # 一条记录的余量


def test_subscribe_log_rotates_in_place_while_a_writer_keeps_it_open(
    monkeypatch, listener, logs_dir
):
    # 订阅子进程一直以追加方式开着这个文件、能活过监听进程的重启：改名没用（它接着写改名后的文件），
    # 要拷一份再截断原文件，追加方式开着的写入方下一次写就落到新的文件尾
    log_path = logs_dir / "card-subscribe.log"
    monkeypatch.setattr(listener, "SUBSCRIBE_LOG", log_path)
    monkeypatch.setattr(listener, "LOG_MAX_BYTES", 100)
    with open(
        log_path, "a", buffering=1
    ) as writer:  # 和 Popen(stdout=...) 交给子进程的是同一种句柄
        for index in range(10):
            writer.write(f"old line {index:02d} ..............\n")
        listener._rotate_subscribe_log()
        writer.write("new line\n")
    assert log_path.read_text(encoding="utf-8") == "new line\n"
    kept = (logs_dir / "card-subscribe.log.1").read_text(encoding="utf-8")
    assert kept.count("old line") == 10


def test_subscribe_log_rotates_under_a_real_child_process(monkeypatch, listener, logs_dir):
    log_path = logs_dir / "card-subscribe.log"
    stop_file = logs_dir / "stop"
    monkeypatch.setattr(listener, "SUBSCRIBE_LOG", log_path)
    monkeypatch.setattr(listener, "LOG_MAX_BYTES", 300)
    child_code = (
        "import sys, time, pathlib\n"
        f"stop = pathlib.Path({str(stop_file)!r})\n"
        "i = 0\n"
        "while not stop.exists() and i < 2000:\n"
        "    print(f'line {i:04d} ' + 'x' * 30, flush=True)\n"
        "    i += 1\n"
        "    time.sleep(0.01)\n"
    )
    with open(log_path, "a", buffering=1) as handle:  # 和监听进程交给订阅子进程的一样
        child = subprocess.Popen(
            [sys.executable, "-c", child_code], stdout=handle, stderr=handle, start_new_session=True
        )
    try:
        deadline = time.monotonic() + 30
        while log_path.stat().st_size <= 300 and time.monotonic() < deadline:
            time.sleep(0.02)
        listener._rotate_subscribe_log()
        backup = (logs_dir / "card-subscribe.log.1").read_text(encoding="utf-8")
        last_before = max(int(m) for m in re.findall(r"line (\d{4})", backup))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            fresh = [
                int(m) for m in re.findall(r"line (\d{4})", log_path.read_text(encoding="utf-8"))
            ]
            if fresh and max(fresh) > last_before + 3:
                break
            time.sleep(0.02)
    finally:
        stop_file.write_text("1")
        child.wait(timeout=30)
    live = log_path.read_text(encoding="utf-8")
    first_live = min(int(m) for m in re.findall(r"line (\d{4})", live))
    # 子进程轮转之后接着往原路径写，没有写进改名后的文件，也没有写到旧偏移留下一段空洞
    assert first_live >= last_before - 1
    assert "\x00" not in live


def test_subscribe_log_keeps_only_the_configured_backups_newest_first(
    monkeypatch, listener, logs_dir
):
    log_path = logs_dir / "card-subscribe.log"
    monkeypatch.setattr(listener, "SUBSCRIBE_LOG", log_path)
    monkeypatch.setattr(listener, "LOG_MAX_BYTES", 10)
    monkeypatch.setattr(listener, "LOG_BACKUP_COUNT", 2)
    for number in range(1, 5):
        log_path.write_text(f"round {number}\n" + "x" * 20, encoding="utf-8")
        listener._rotate_subscribe_log()
    assert sorted(path.name for path in logs_dir.iterdir()) == [
        "card-subscribe.log",
        "card-subscribe.log.1",
        "card-subscribe.log.2",
    ]
    assert (logs_dir / "card-subscribe.log.1").read_text(encoding="utf-8").startswith("round 4")
    assert (logs_dir / "card-subscribe.log.2").read_text(encoding="utf-8").startswith("round 3")
    assert log_path.read_text(encoding="utf-8") == ""


def test_small_or_missing_subscribe_log_is_left_alone(monkeypatch, listener, logs_dir):
    log_path = logs_dir / "card-subscribe.log"
    monkeypatch.setattr(listener, "SUBSCRIBE_LOG", log_path)
    monkeypatch.setattr(listener, "LOG_MAX_BYTES", 100)
    listener._rotate_subscribe_log()  # 文件还不存在
    assert list(logs_dir.iterdir()) == []
    log_path.write_text("y" * 100, encoding="utf-8")  # 正好到上限不算超
    listener._rotate_subscribe_log()
    assert [path.name for path in logs_dir.iterdir()] == ["card-subscribe.log"]
    assert log_path.read_text(encoding="utf-8") == "y" * 100


def test_rotation_failure_keeps_the_data_and_does_not_raise(
    monkeypatch, listener, logs_dir, caplog
):
    log_path = logs_dir / "card-subscribe.log"
    monkeypatch.setattr(listener, "SUBSCRIBE_LOG", log_path)
    monkeypatch.setattr(listener, "LOG_MAX_BYTES", 100)
    log_path.write_text("z" * 200, encoding="utf-8")

    def disk_full(source, destination):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(listener, "shutil", types.SimpleNamespace(copyfile=disk_full))
    with caplog.at_level(logging.WARNING, logger="card_listener"):
        listener._rotate_subscribe_log()
    # 拷贝没成功就不能截断，不然这段日志就没了
    assert log_path.read_text(encoding="utf-8") == "z" * 200
    assert "轮转" in caplog.text
