#!/usr/bin/env python3
"""声档的真浏览器回归：一条命令建隔离环境、造数、起服务、跑用例、汇总、清场。

    /usr/local/bin/python3 workbench/scripts/e2e/run.py                   # 全部用例，只跑 Chromium
    /usr/local/bin/python3 workbench/scripts/e2e/run.py e05 p02_list      # 按文件名的前缀或片段挑
    /usr/local/bin/python3 workbench/scripts/e2e/run.py --list            # 看有哪些用例
    /usr/local/bin/python3 workbench/scripts/e2e/run.py --browser chromium,webkit e52

前端要先 `cd workbench/frontend && npm run build`（后端直接服务 frontend/dist；dist 比 src 旧时这里会拦住，
加 --build 让它替你跑）。Playwright 要用装了 playwright 包的那个 Python（本机是 /usr/local/bin/python3），
后端用 workbench/.venv 里的 Python（在 worktree 里没有 .venv 时用 --backend-python 借主仓库的）。

整个过程只碰临时目录里的东西：数据目录、归档根、暂存根、HOME、中转仓库（指向空目录）、CSRF cookie 名都是新的，
飞书、AI、语义检索、深度关联全关，也不读 .env。服务端口不能是生产的 8765；停服务只按自己记下的 PID。
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKBENCH = HERE.parents[1]
CASES_DIR = HERE / "cases"
PRODUCTION_PORT = 8765
PORT_RANGE = range(8850, 8900)
MIN_FREE_BYTES = 2 * 1024**3
EXPECTED_MEETINGS = 16
ALL_ENGINES = ("chromium", "webkit", "firefox")
# 恢复快照时整目录换掉的几块；logs、home 不动
STATE_DIRS = ("data", "archive", "staging", "browse")
# 临时根目录里放一个记号，收场删整个目录之前认它，不认就不删
MARKER = ".workbench-e2e"


def refuse(message: str) -> None:
    raise SystemExit(f"拒绝：{message}")


def say(message: str = "") -> None:
    print(message, flush=True)


# ───────────────────────────── 用例清单 ─────────────────────────────


@dataclass
class Case:
    path: Path
    summary: str
    mutates: bool
    engines: tuple[str, ...]
    timeout: int

    @property
    def name(self) -> str:
        return self.path.stem


def parse_case(path: Path) -> Case:
    """从用例文件的文本里读说明和三个声明，不导入它（导入会连环境变量）"""
    text = path.read_text(encoding="utf-8")
    doc = re.match(r'\s*"""(.*?)"""', text, re.S)
    summary = doc.group(1).strip().splitlines()[0] if doc and doc.group(1).strip() else ""
    engines_match = re.search(r"^ENGINES\s*=\s*\(([^)]*)\)", text, re.M)
    engines = (
        tuple(re.findall(r"[a-z]+", engines_match.group(1))) if engines_match else ("chromium",)
    )
    timeout_match = re.search(r"^TIMEOUT\s*=\s*(\d+)", text, re.M)
    return Case(
        path=path,
        summary=summary,
        mutates=bool(re.search(r"^MUTATES\s*=\s*True\b", text, re.M)),
        engines=engines,
        timeout=int(timeout_match.group(1)) if timeout_match else 300,
    )


def discover_cases(selectors: list[str]) -> list[Case]:
    cases = [parse_case(path) for path in sorted(CASES_DIR.glob("*.py"))]
    if not selectors:
        return cases
    chosen = [c for c in cases if any(c.name.startswith(s) or s in c.name for s in selectors)]
    unmatched = [
        s for s in selectors if not any(c.name.startswith(s) or s in c.name for c in cases)
    ]
    if unmatched:
        refuse(f"没有叫 {'、'.join(unmatched)} 的用例（--list 看全部）")
    return chosen


# ───────────────────────────── 端口和进程 ─────────────────────────────


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def listening_pids(port: int) -> set[int]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    return {int(item) for item in out.split() if item.isdigit()}


def pick_port(requested: int | None) -> int:
    if requested is not None:
        if requested == PRODUCTION_PORT:
            refuse(f"端口 {PRODUCTION_PORT} 是生产实例在用的，不能拿来跑回归")
        if port_in_use(requested):
            refuse(
                f"端口 {requested} 已经有进程在监听（lsof -nP -iTCP:{requested} -sTCP:LISTEN 看是谁）"
            )
        return requested
    for port in PORT_RANGE:
        if port != PRODUCTION_PORT and not port_in_use(port):
            return port
    refuse(f"{PORT_RANGE.start}~{PORT_RANGE.stop - 1} 都被占了，用 --port 指一个空闲端口")
    raise AssertionError  # 不会走到


# ───────────────────────────── 隔离环境 ─────────────────────────────


@dataclass
class Sandbox:
    root: Path
    port: int
    ffmpeg: str
    dirs: dict[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (*STATE_DIRS, "home", "relay-none", "logs", "tmp", "report", "snapshot"):
            self.dirs[name] = self.root / name
            self.dirs[name].mkdir(parents=True, exist_ok=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def backend_env(self) -> dict[str, str]:
        """后端子进程的环境：不继承任何声档、中转的变量，路径全指到临时目录，外部服务全关"""
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                (
                    "MEETING_WORKBENCH_",
                    "MEETING_RELAY_",
                    "MallocStackLogging",
                    "PYTHON",
                    "VIRTUAL_ENV",
                )
            )
        }
        data = self.dirs["data"]
        nowhere = "/nonexistent"
        env.update(
            {
                "HOME": str(self.dirs["home"]),
                "TZ": "Asia/Shanghai",
                "PYTHONPATH": str(WORKBENCH / "backend"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "E2E_ROOT": str(self.root),
                "E2E_FFMPEG": self.ffmpeg,
                "MEETING_WORKBENCH_HOST": "127.0.0.1",
                "MEETING_WORKBENCH_PORT": str(self.port),
                "MEETING_WORKBENCH_DATA_DIR": str(data),
                "MEETING_WORKBENCH_DATABASE_PATH": str(data / "workbench.sqlite3"),
                "MEETING_WORKBENCH_ARCHIVE_ROOT": str(self.dirs["archive"]),
                "MEETING_WORKBENCH_STAGING_ROOT": str(self.dirs["staging"]),
                "MEETING_WORKBENCH_MATERIAL_BROWSE_ROOT": str(self.dirs["browse"]),
                "MEETING_WORKBENCH_RELAY_REPO": str(self.dirs["relay-none"]),
                "MEETING_WORKBENCH_RELAY_JOBS_DB": str(data / "relay-jobs.sqlite3"),
                "MEETING_WORKBENCH_SEMANTIC_ENABLED": "0",
                "MEETING_WORKBENCH_CSRF_COOKIE_NAME": "e2e_workbench_csrf",
                "MEETING_WORKBENCH_QWEN_BINARY": f"{nowhere}/qwen-asr",
                "MEETING_WORKBENCH_LLM_API_BASE": "http://127.0.0.1:9/v1",
                "MEETING_WORKBENCH_LLM_API_KEY_FILE": f"{nowhere}/llm-key",
                "MEETING_WORKBENCH_LARK_WEBHOOK_URL": "",
                "MEETING_WORKBENCH_LARK_CHAT_ID": "",
                "MEETING_WORKBENCH_LARK_APP_ID": "",
                "MEETING_WORKBENCH_LARK_APP_SECRET_FILE": f"{nowhere}/lark-secret",
                "MEETING_WORKBENCH_LARK_CLI_BIN": f"{nowhere}/lark-cli",
                "MEETING_WORKBENCH_LARK_TMUX_SOCKET": f"{nowhere}/tmux-socket",
                "MEETING_WORKBENCH_PUBLIC_BASE_URL": self.base_url,
                "MEETING_WORKBENCH_MATERIAL_CONTENT_ENABLED": "0",
                "MEETING_WORKBENCH_LINKS_ENABLED": "0",
                "MEETING_WORKBENCH_LINKS_LLM_ENABLED": "0",
                "MEETING_WORKBENCH_LINKS_LLM_DAILY_CALLS": "0",
                "MEETING_WORKBENCH_QA_DAILY_QUESTIONS": "0",
                "MEETING_WORKBENCH_GLOSSARY_MINING_ENABLED": "0",
                "MEETING_WORKBENCH_FUNASR_PYTHON": f"{nowhere}/funasr-python",
                "MEETING_WORKBENCH_MATERIAL_TRANSCRIBER": f"{nowhere}/funasr_material.py",
                "MEETING_WORKBENCH_TEXTUTIL": f"{nowhere}/textutil",
                "MEETING_WORKBENCH_LOG_FILE": str(self.dirs["logs"] / "web.log"),
                "MEETING_WORKBENCH_LOG_MAX_BYTES": str(5 * 1024 * 1024),
                "MEETING_WORKBENCH_LOG_BACKUP_COUNT": "1",
            }
        )
        return env

    def assert_isolated(self, env: dict[str, str]) -> None:
        """硬性检查：凡是会落盘的路径都在临时根目录里，端口不是生产的"""
        if int(env["MEETING_WORKBENCH_PORT"]) == PRODUCTION_PORT:
            refuse(f"端口是生产的 {PRODUCTION_PORT}")
        root = str(self.root.resolve())
        for key in (
            "HOME",
            "MEETING_WORKBENCH_DATA_DIR",
            "MEETING_WORKBENCH_DATABASE_PATH",
            "MEETING_WORKBENCH_ARCHIVE_ROOT",
            "MEETING_WORKBENCH_STAGING_ROOT",
            "MEETING_WORKBENCH_MATERIAL_BROWSE_ROOT",
            "MEETING_WORKBENCH_RELAY_REPO",
            "MEETING_WORKBENCH_RELAY_JOBS_DB",
            "MEETING_WORKBENCH_LOG_FILE",
        ):
            resolved = str(Path(env[key]).resolve())
            if resolved != root and not resolved.startswith(root + os.sep):
                refuse(f"{key}={resolved} 不在临时目录 {root} 里")


def check_workdir(root: Path) -> None:
    home = Path.home()
    forbidden = [
        home / ".meeting-workbench",
        home / ".meeting-relay",
        home / ".iphone-relay",
        home / "MeetingArchive",
        home / "Movies",
        Path("/Volumes"),
    ]
    resolved = root.resolve()
    for place in forbidden:
        if resolved == place or place in resolved.parents:
            refuse(f"临时目录 {resolved} 在 {place} 里，不能拿生产在用的位置当工作目录")


def snapshot_state(box: Sandbox) -> None:
    for name in STATE_DIRS:
        shutil.copytree(box.dirs[name], box.dirs["snapshot"] / name, symlinks=True)


def restore_state(box: Sandbox) -> None:
    for name in STATE_DIRS:
        shutil.rmtree(box.dirs[name])
        shutil.copytree(box.dirs["snapshot"] / name, box.dirs[name], symlinks=True)


# ───────────────────────────── 后端服务 ─────────────────────────────


def graph_rev(box: Sandbox) -> str | None:
    """隔离库里关系图的版本号（只读打开，服务开着也能读）"""
    try:
        connection = sqlite3.connect(
            f"file:{box.dirs['data'] / 'workbench.sqlite3'}?mode=ro", uri=True, timeout=5
        )
        try:
            row = connection.execute("SELECT value FROM app_state WHERE key='graph_rev'").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def wait_quiet(box: Sandbox, quiet_seconds: float = 20, timeout: float = 150) -> bool:
    """后台头几轮会写项目卡片，每写一张就给关系图的版本号加一，星图的 etag 跟着变。等它安静（超过一轮扫描的间隔
    没再变）再存快照，之后每次恢复都从安静的状态起，用例里「数据没变就回 304」才不会撞上后台的写。"""
    last, since = graph_rev(box), time.monotonic()
    deadline = since + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        current = graph_rev(box)
        if current != last:
            last, since = current, time.monotonic()
        elif time.monotonic() - since >= quiet_seconds:
            return True
    return False


def fetch_health(base_url: str) -> dict | None:
    try:
        with urllib.request.urlopen(base_url + "/api/health", timeout=3) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:  # 降级时可能回 5xx，服务照样是起来了
        try:
            return json.load(error)
        except ValueError:
            return {}
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Server:
    def __init__(self, box: Sandbox, backend_python: str):
        self.box = box
        self.backend_python = backend_python
        self.proc: subprocess.Popen | None = None
        self.log = box.dirs["report"] / "server.log"

    def start(self) -> None:
        env = self.box.backend_env()
        self.box.assert_isolated(env)
        if port_in_use(self.box.port):
            refuse(f"端口 {self.box.port} 被占了，起不了服务")
        with open(self.log, "ab") as log:
            self.proc = subprocess.Popen(
                [self.backend_python, str(HERE / "backend_entry.py"), "serve"],
                cwd=self.box.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (self.box.root / "server.pid").write_text(str(self.proc.pid), encoding="utf-8")
        deadline = time.monotonic() + 90
        health = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                tail = self.log.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise RuntimeError(f"服务起来就退出了（退出码 {self.proc.returncode}）：\n{tail}")
            health = fetch_health(self.box.base_url)
            if health is not None:
                break
            time.sleep(0.4)
        else:
            raise RuntimeError(f"90 秒内服务没有应答，日志 {self.log}")
        listeners = listening_pids(self.box.port)
        if listeners and listeners != {self.proc.pid}:
            raise RuntimeError(
                f"端口 {self.box.port} 上监听的是 {listeners}，不是自己起的 {self.proc.pid}"
            )
        meetings = (health.get("counts") or {}).get("meetings")
        if meetings != EXPECTED_MEETINGS:
            raise RuntimeError(
                f"实例里有 {meetings} 场会，造数应该是 {EXPECTED_MEETINGS} 场：它连的不是刚造数的那份库，停下"
            )

    def stop(self) -> None:
        """只停自己起的这个 PID；先核对端口上监听的是不是它"""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        listeners = listening_pids(self.box.port)
        if listeners and proc.pid not in listeners:
            say(
                f"  端口 {self.box.port} 上监听的是 {listeners}，和记下的 PID {proc.pid} 对不上，不按端口杀别人"
            )
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        deadline = time.monotonic() + 10
        while port_in_use(self.box.port) and time.monotonic() < deadline:
            time.sleep(0.2)

    def restart_on_snapshot(self) -> None:
        self.stop()
        restore_state(self.box)
        self.start()


# ───────────────────────────── 跑用例 ─────────────────────────────


@dataclass
class Result:
    name: str
    engine: str
    status: str  # 通过 / 失败 / 超时 / 跳过
    seconds: float = 0.0
    checks: int = 0  # 用例里记了多少条检查（check 打出来的「通过」「失败」行）
    failed_lines: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)


def case_env(box: Sandbox, engine: str, case_tmp: Path, shots: Path | None) -> dict[str, str]:
    """用例子进程的环境。浏览器要靠真 HOME 找 Playwright 装的浏览器；用例只经 HTTP 连隔离实例，不碰任何生产状态"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MEETING_WORKBENCH_", "MEETING_RELAY_", "PYTHON"))
    }
    env.update(
        {
            "PYTHONPATH": str(HERE),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "E2E_BASE_URL": box.base_url,
            "E2E_DATA_DIR": str(box.dirs["data"]),
            "E2E_ARCHIVE_ROOT": str(box.dirs["archive"]),
            "E2E_TMP_DIR": str(case_tmp),
            "E2E_SHOTS_DIR": str(shots or (box.dirs["report"] / "shots")),
            "E2E_SHOTS_ALL": "1" if shots else "0",
            "E2E_LOG_FILE": str(box.dirs["logs"] / "web.log"),
            "E2E_SEED_FILE": str(box.root / "seed.json"),
            "E2E_ENGINE": engine,
        }
    )
    return env


def run_case(
    case: Case, engine: str, box: Sandbox, playwright_python: str, shots: Path | None
) -> Result:
    report = box.dirs["report"]
    case_tmp = box.dirs["tmp"] / f"{case.name}-{engine}"
    case_tmp.mkdir(parents=True, exist_ok=True)
    env = case_env(box, engine, case_tmp, shots)
    log_path = report / f"{case.name}-{engine}.log"
    started = time.monotonic()
    status = "通过"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [playwright_python, str(case.path)],
            cwd=case_tmp,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=case.timeout)
            status = "通过" if code == 0 else "失败"
        except subprocess.TimeoutExpired:
            status = "超时"
            # 用例自己起的浏览器、驱动都在这个进程组里：按组停，不按名字找
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
    seconds = time.monotonic() - started
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return Result(
        name=case.name,
        engine=engine,
        status=status,
        seconds=seconds,
        checks=sum(1 for line in lines if line.startswith(("通过  ", "失败  "))),
        failed_lines=[line for line in lines if line.startswith("失败")],
        tail=lines[-14:],
    )


# ───────────────────────────── 前置检查 ─────────────────────────────


def newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for base in paths:
        if base.is_file():
            newest = max(newest, base.stat().st_mtime)
        elif base.is_dir():
            for file in base.rglob("*"):
                if file.is_file() and ".test." not in file.name and file.name != "test-setup.ts":
                    newest = max(newest, file.stat().st_mtime)
    return newest


def check_dist(build: bool) -> None:
    frontend = WORKBENCH / "frontend"
    index = frontend / "dist" / "index.html"
    sources = [
        frontend / "src",
        frontend / "public",
        frontend / "index.html",
        frontend / "vite.config.ts",
        frontend / "package.json",
    ]
    stale = not index.is_file() or newest_mtime(sources) > index.stat().st_mtime
    if not stale:
        return
    if build:
        say("构建前端：npm run build")
        if subprocess.run(["npm", "run", "build"], cwd=frontend).returncode != 0:
            refuse("npm run build 没成功")
        return
    reason = "还没构建" if not index.is_file() else "比 src 里的源码旧"
    refuse(
        f"frontend/dist {reason}，跑出来的是旧界面。先 `cd {frontend} && npm run build`，或者加 --build 让我来跑"
    )


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate
    refuse("找不到 ffmpeg：造数要用它生成测试音频（brew install ffmpeg）")
    raise AssertionError


def can_import(python: str, module: str, env_extra: dict[str, str] | None = None) -> bool:
    env = {**os.environ, **(env_extra or {})}
    return (
        subprocess.run(
            [python, "-c", f"import {module}"], capture_output=True, env=env, timeout=60
        ).returncode
        == 0
    )


def resolve_backend_python(given: str | None) -> str:
    candidates = [given] if given else [str(WORKBENCH / ".venv" / "bin" / "python")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            if not can_import(
                candidate, "meeting_workbench", {"PYTHONPATH": str(WORKBENCH / "backend")}
            ):
                refuse(f"{candidate} 导不进 meeting_workbench（要装好后端依赖的那个 Python）")
            return candidate
    refuse(
        "找不到后端的 Python：workbench/.venv 不存在。在 worktree 里请用 --backend-python 指向主仓库的 "
        "workbench/.venv/bin/python（导入的仍是当前目录的代码）"
    )
    raise AssertionError


def resolve_playwright_python(given: str | None) -> str:
    candidates = [given] if given else [sys.executable, "/usr/local/bin/python3"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and can_import(candidate, "playwright.sync_api"):
            return candidate
    refuse(
        "找不到装了 playwright 的 Python：用 --playwright-python 指定（本机是 /usr/local/bin/python3）"
    )
    raise AssertionError


# ───────────────────────────── 主流程 ─────────────────────────────


def seed(box: Sandbox, backend_python: str) -> None:
    env = box.backend_env()
    box.assert_isolated(env)
    steps = [
        ("摆归档", [backend_python, str(HERE / "seed_archive.py")]),
        ("导入归档", [backend_python, str(HERE / "backend_entry.py"), "scan"]),
        ("造库", [backend_python, str(HERE / "seed_db.py")]),
    ]
    for label, command in steps:
        started = time.monotonic()
        done = subprocess.run(command, cwd=box.root, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            refuse(
                f"造数「{label}」失败（退出码 {done.returncode}）：\n{done.stdout[-1200:]}\n{done.stderr[-1500:]}"
            )
        say(f"  造数：{label}（{time.monotonic() - started:.1f} 秒）")


def print_summary(results: list[Result], elapsed: float) -> None:
    say()
    say("=" * 72)
    width = max((len(r.name) for r in results), default=10)
    for r in results:
        engine = "" if r.engine == "chromium" else f" [{r.engine}]"
        say(f"{r.status}  {r.name:<{width}}{engine}  {r.seconds:6.1f}s  {r.checks:>3} 项检查")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("通过", "失败", "超时", "跳过")}
    say("-" * 72)
    say(
        f"共 {len(results)} 项：通过 {counts['通过']}，失败 {counts['失败']}，超时 {counts['超时']}，"
        f"跳过 {counts['跳过']}；共 {sum(r.checks for r in results)} 项检查；用时 {elapsed / 60:.1f} 分钟"
    )
    for r in results:
        if r.status in ("失败", "超时"):
            say()
            say(f"── {r.name} [{r.engine}] {r.status}")
            for line in r.failed_lines[:12]:
                say(f"   {line}")
            if not r.failed_lines:
                for line in r.tail:
                    say(f"   | {line}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="声档真浏览器回归：建隔离环境、造数、起服务、跑用例、清场",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("cases", nargs="*", help="用例文件名的前缀或片段，不给就跑全部")
    parser.add_argument("--list", action="store_true", help="列出用例和说明后退出")
    parser.add_argument(
        "--browser", default="chromium", help="chromium（默认）、webkit、firefox，逗号分隔"
    )
    parser.add_argument("--port", type=int, help="隔离实例的端口；不给就在 8850~8899 里挑空闲的")
    parser.add_argument("--backend-python", help="后端的 Python（默认 workbench/.venv/bin/python）")
    parser.add_argument(
        "--playwright-python",
        help="装了 playwright 的 Python（默认当前解释器或 /usr/local/bin/python3）",
    )
    parser.add_argument("--build", action="store_true", help="dist 过期时先跑 npm run build")
    parser.add_argument("--keep", action="store_true", help="跑完不删临时目录（含库、归档、日志）")
    parser.add_argument("--workdir", help="临时目录；默认在系统临时目录里新建")
    parser.add_argument(
        "--shots",
        help="截图存到这个目录（失败时自动拍的、用例里 shot() 拍的）；默认在失败现场的 report/shots 里",
    )
    parser.add_argument("--report", help="把汇总写成 JSON 文件")
    parser.add_argument("-x", "--fail-fast", action="store_true", help="第一个失败就停")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="只建环境、造数、起服务，停在那里等着手动跑用例（写新用例时用），Ctrl-C 收场",
    )
    args = parser.parse_args()

    selected = discover_cases(args.cases)
    if args.list:
        for case in discover_cases([]):
            flags = []
            if case.mutates:
                flags.append("改数据")
            if case.engines != ("chromium",):
                flags.append("/".join(case.engines))
            say(f"{case.name:<28} {case.summary}" + (f"  ({'，'.join(flags)})" if flags else ""))
        return 0

    engines = [item.strip() for item in args.browser.split(",") if item.strip()]
    bad = [e for e in engines if e not in ALL_ENGINES]
    if bad or not engines:
        refuse(f"--browser 只认 {'、'.join(ALL_ENGINES)}，收到 {args.browser!r}")

    backend_python = resolve_backend_python(args.backend_python)
    playwright_python = resolve_playwright_python(args.playwright_python)
    ffmpeg = find_ffmpeg()
    check_dist(args.build)
    port = pick_port(args.port)

    root = (
        Path(args.workdir).expanduser()
        if args.workdir
        else Path(tempfile.mkdtemp(prefix="workbench-e2e-"))
    )
    if args.workdir and root.exists() and any(root.iterdir()):
        refuse(f"--workdir {root} 不是空目录")
    check_workdir(root)
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if free < MIN_FREE_BYTES:
        refuse(
            f"{root} 所在的盘只剩 {free / 1024**3:.1f} GB，低于 {MIN_FREE_BYTES / 1024**3:.0f} GB，不跑"
        )

    box = Sandbox(root=root.resolve(), port=port, ffmpeg=ffmpeg)
    (box.root / MARKER).write_text("声档浏览器回归的临时目录，run.py 建的\n", encoding="utf-8")
    server = Server(box, backend_python)
    shots = Path(args.shots).expanduser() if args.shots else None
    results: list[Result] = []
    started = time.monotonic()

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    say(f"隔离环境 {box.root}")
    say(f"端口 {port}，后端 {backend_python}，Playwright {playwright_python}")
    try:
        seed(box, backend_python)
        server.start()
        say("  等后台头几轮写完项目卡片（关系图版本号安静下来）再存快照……")
        if not wait_quiet(box):
            say("  警告：150 秒内后台一直在写，关系图的 etag 可能不稳，涉及 304 的用例可能偶发失败")
        server.stop()
        snapshot_state(box)
        server.start()
        if args.serve:
            case_tmp = box.dirs["tmp"] / "manual"
            case_tmp.mkdir(parents=True, exist_ok=True)
            env = case_env(box, engines[0], case_tmp, shots)
            exports = "\n".join(
                f"export {key}={shlex.quote(value)}"
                for key, value in env.items()
                if key.startswith(("E2E_", "PYTHONPATH"))
            )
            (box.root / "env.sh").write_text(exports + "\n", encoding="utf-8")
            say(f"  服务已起（PID {server.proc.pid}）：{box.base_url}")
            say(
                f"  手动跑一个用例：. {box.root}/env.sh && {playwright_python} {CASES_DIR}/<用例>.py"
            )
            (box.root / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")
            say(
                f"  用例改了数据之后恢复到造数后的样子：kill -USR1 {os.getpid()}（停服务、换回快照、再起）"
            )
            say("  Ctrl-C 收场（停服务、删临时目录）")
            restore_wanted: list[bool] = []
            signal.signal(signal.SIGUSR1, lambda *_: restore_wanted.append(True))
            while True:
                time.sleep(1)
                if restore_wanted:
                    restore_wanted.clear()
                    server.restart_on_snapshot()
                    say("  已恢复到造数后的样子")
        say(
            f"  服务已起（PID {server.proc.pid}），实例里 {EXPECTED_MEETINGS} 场会，开始跑 {len(selected)} 个用例"
        )
        dirty = False
        total = len(selected) * len(engines)
        for engine in engines:
            for case in selected:
                index = len(results) + 1
                if engine not in case.engines:
                    results.append(Result(case.name, engine, "跳过"))
                    continue
                if dirty:
                    server.restart_on_snapshot()
                    dirty = False
                say(f"[{index:>2}/{total}] {case.name} ({engine}) …")
                result = run_case(case, engine, box, playwright_python, shots)
                results.append(result)
                say(f"        {result.status}  {result.seconds:.1f}s")
                dirty = case.mutates
                if args.fail_fast and result.status in ("失败", "超时"):
                    break
            else:
                continue
            break
    finally:
        server.stop()
        elapsed = time.monotonic() - started
        if results:
            print_summary(results, elapsed)
        failed = any(r.status in ("失败", "超时") for r in results)
        listeners = listening_pids(port)
        if listeners:
            say(f"警告：端口 {port} 上还有进程在监听 {listeners}，没有动它")
        report = box.dirs["report"]
        if args.report:
            Path(args.report).expanduser().write_text(
                json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        if args.keep:
            say(f"临时目录留着：{box.root}")
        elif failed:
            # 库、归档、快照占地方，删掉；留下服务日志、各用例的输出和失败截图
            for name in (*STATE_DIRS, "home", "tmp", "snapshot", "relay-none"):
                shutil.rmtree(box.dirs[name], ignore_errors=True)
            web_log = box.dirs["logs"] / "web.log"
            if web_log.exists():
                shutil.copy2(web_log, report / "web.log")
            shutil.rmtree(box.dirs["logs"], ignore_errors=True)
            say(f"失败现场（服务日志、各用例输出、失败截图）在 {report}，看完自己删")
        elif (box.root / MARKER).is_file():
            shutil.rmtree(box.root, ignore_errors=True)
    return 1 if any(r.status in ("失败", "超时") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
