"""材料读取用的子进程的共同规矩（第三期 3a）：3b 的读取进程、3c 的认字程序、3d 的转写程序都用它。

- 外部程序只读原文件，输出一律写到 data_dir 或 stdout，不在资料盘上留任何东西。
- JSON 一行一个请求、一行一个回答。stdout 只走回答；stderr 写到 data_dir/logs/<程序名>.log，
  不接 PIPE（接了没人读，满 64KB 子进程就卡住）。不是 JSON 或 id 对不上的行丢掉并记日志。
  回答里带 "partial": true 的是中间行（例如 PDF 一页一页地回），最后一行不带。
- 子进程 stdin 读到 EOF 立刻退出，服务没了它们也不会留着。
- macOS 上用 taskpolicy -b 启动（后台优先级），别的系统用 nice 10；都 start_new_session，超时
  杀整个进程组。
- 可以给一个内存上限：每秒看一次 RSS，超过就杀掉，按处理超时算。
- 每 200 个请求或闲 5 分钟重开一次。
- 服务关闭时先置停止标记，再杀掉这些程序；等回答时每秒查一次停止标记和中止条件。
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

from . import busy

logger = logging.getLogger(__name__)

MAX_REQUESTS = 200
IDLE_RESTART_SECONDS = 300.0
RSS_LIMIT_BYTES = 1024 * 1024 * 1024
LOG_ROTATE_BYTES = 5 * 1024 * 1024
POLL_SECONDS = 1.0


class HelperError(RuntimeError):
    pass


class HelperTimeout(HelperError):
    """超时或内存超限：进程组已经杀掉，下次请求会重开。"""


class HelperStopped(HelperError):
    """服务在关闭（停止标记），或中止条件成立（例如会议开始转写）：进程组已经杀掉。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class HelperCrashed(HelperError):
    """子进程自己退出了（stdout 读到 EOF）。"""


def background_prefix() -> list[str]:
    if sys.platform == "darwin" and shutil.which("taskpolicy"):
        return ["taskpolicy", "-b"]
    if shutil.which("nice"):
        return ["nice", "-n", "10"]
    return []


def log_path(data_dir: Path, name: str) -> Path:
    path = Path(data_dir) / "logs" / f"{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_ROTATE_BYTES:
            path.replace(path.with_suffix(".log.1"))
    except FileNotFoundError:
        pass
    return path


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def process_rss_bytes(pid: int) -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    return int(text) * 1024 if text.isdigit() else None


class StopFlag:
    """服务关闭时置上；所有材料循环和子进程都看它。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._helpers: list[HelperProcess] = []

    def is_set(self) -> bool:
        return self._event.is_set()

    def register(self, helper: Any) -> None:
        """登记一个有 kill() 的东西：常驻进程，或者 run_background 的一次性进程。"""
        with self._lock:
            self._helpers.append(helper)

    def unregister(self, helper: Any) -> None:
        with self._lock:
            if helper in self._helpers:
                self._helpers.remove(helper)

    def set(self) -> None:
        """置停止标记并杀掉所有登记过的子进程。"""
        self._event.set()
        with self._lock:
            helpers = list(self._helpers)
        for helper in helpers:
            helper.kill()

    def clear(self) -> None:
        self._event.clear()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)


class _OneShot:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        kill_group(self.process.pid)


def run_background(
    argv: list[str],
    *,
    timeout: float,
    stop: "StopFlag | None" = None,
    env: dict[str, str] | None = None,
    background: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """一次性的外部程序（tesseract、sips、ffprobe 这类）：后台优先级、自己的进程组，超时杀整个组、
    抛 HelperTimeout；服务关闭时一起杀掉，抛 HelperStopped。stdout、stderr 都收回来。"""
    if stop is not None and stop.is_set():
        raise HelperStopped("stopping")
    merged = dict(os.environ)
    merged.update(env or {})
    process = subprocess.Popen(
        [*(background_prefix() if background else []), *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged,
        start_new_session=True,
    )
    handle = _OneShot(process)
    if stop is not None:
        stop.register(handle)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            kill_group(process.pid)
            process.communicate()
            raise HelperTimeout(f"{Path(argv[0]).name} 超过 {timeout:g} 秒") from error
    finally:
        if stop is not None:
            stop.unregister(handle)
    if handle.killed:
        raise HelperStopped("stopping")
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class HelperProcess:
    """常驻 JSON 行子进程。线程安全：同一时刻只处理一个请求。"""

    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        data_dir: Path,
        env: dict[str, str] | None = None,
        background: bool = True,
        max_requests: int = MAX_REQUESTS,
        idle_restart_seconds: float = IDLE_RESTART_SECONDS,
        rss_limit_bytes: int | None = RSS_LIMIT_BYTES,
        stop: StopFlag | None = None,
        clock: Callable[[], float] = time.monotonic,
        rss_of: Callable[[int], int | None] = process_rss_bytes,
    ):
        self.name = name
        self.argv = list(argv)
        self.data_dir = Path(data_dir)
        self.env = env
        self.background = background
        self.max_requests = max_requests
        self.idle_restart_seconds = idle_restart_seconds
        self.rss_limit_bytes = rss_limit_bytes
        self.stop = stop
        self.clock = clock
        self.rss_of = rss_of
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] | None = None
        self._stderr: TextIO | None = None
        self._served = 0
        self._last_used = clock()
        self._ids = itertools.count(1)
        if stop is not None:
            stop.register(self)

    # ------------------------------------------------------------------ 进程

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def _start(self) -> subprocess.Popen[str]:
        argv = [*(background_prefix() if self.background else []), *self.argv]
        self._stderr = log_path(self.data_dir, self.name).open("a", encoding="utf-8")
        env = dict(os.environ)
        env.update(self.env or {})
        env.setdefault("PYTHONUNBUFFERED", "1")
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        busy.register_own_group(process.pid)
        lines: queue.Queue[str | None] = queue.Queue()

        def pump(stream: TextIO) -> None:
            try:
                for line in stream:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        threading.Thread(
            target=pump, args=(process.stdout,), name=f"{self.name}-stdout", daemon=True
        ).start()
        self._process = process
        self._lines = lines
        self._served = 0
        self._last_used = self.clock()
        return process

    def _ensure(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            if (
                self._served >= self.max_requests
                or self.clock() - self._last_used >= self.idle_restart_seconds
            ):
                self._close_locked()
            else:
                return process
        elif process is not None:
            self._close_locked()
        return self._start()

    def _close_locked(self, *, graceful: bool = True) -> None:
        process = self._process
        self._process = None
        self._lines = None
        if process is not None:
            if graceful and process.poll() is None:
                try:
                    if process.stdin:
                        process.stdin.close()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                kill_group(process.pid)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            busy.unregister_own_group(process.pid)
            for stream in (process.stdin, process.stdout):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        if self._stderr is not None:
            try:
                self._stderr.close()
            except OSError:
                pass
            self._stderr = None

    def start(self) -> int:
        """先把进程开起来（不发请求），返回进程号：材料转写把它记进断点行，重启后好清理。"""
        with self._lock:
            if self.stop is not None and self.stop.is_set():
                raise HelperStopped("stopping")
            return self._ensure().pid

    def kill(self) -> None:
        """立刻杀掉进程组（不拿锁：正在等回答的线程会看到 EOF 或停止标记）。"""
        process = self._process
        if process is not None and process.poll() is None:
            kill_group(process.pid)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def close_if_idle(self) -> bool:
        """闲够了就关掉（下次请求再开）。"""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if (
                self._process is not None
                and self.clock() - self._last_used >= self.idle_restart_seconds
            ):
                self._close_locked()
                return True
            return False
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ 请求

    def request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float,
        idle_timeout: float | None = None,
        abort: Callable[[], str | None] | None = None,
        abort_every: float = POLL_SECONDS,
        on_partial: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """发一个请求，等最后一行回答。

        timeout 是整个请求的上限；idle_timeout 是多久没有任何回答行算超时（流式回答用）。
        abort 返回非空就杀掉进程组、抛 HelperStopped（例如会议开始转写）。"""
        with self._lock:
            if self.stop is not None and self.stop.is_set():
                raise HelperStopped("stopping")
            process = self._ensure()
            lines = self._lines
            assert lines is not None
            request_id = f"{self.name}-{next(self._ids)}"
            message = json.dumps({**payload, "id": request_id}, ensure_ascii=False)
            try:
                assert process.stdin is not None
                process.stdin.write(message + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                self._close_locked(graceful=False)
                raise HelperCrashed(f"{self.name} 写不进去：{error}") from error
            self._served += 1
            started = self.clock()
            last_line = started
            last_abort = started
            last_rss = started
            try:
                while True:
                    now = self.clock()
                    if self.stop is not None and self.stop.is_set():
                        raise HelperStopped("stopping")
                    if abort is not None and now - last_abort >= abort_every:
                        last_abort = now
                        reason = abort()
                        if reason:
                            raise HelperStopped(reason)
                    if now - started >= timeout:
                        raise HelperTimeout(f"{self.name} 超过 {timeout:g} 秒")
                    if idle_timeout is not None and now - last_line >= idle_timeout:
                        raise HelperTimeout(f"{self.name} {idle_timeout:g} 秒没有新的回答")
                    if self.rss_limit_bytes is not None and now - last_rss >= POLL_SECONDS:
                        last_rss = now
                        rss = self.rss_of(process.pid)
                        if rss is not None and rss > self.rss_limit_bytes:
                            raise HelperTimeout(f"{self.name} 内存超过上限")
                    try:
                        line = lines.get(timeout=min(POLL_SECONDS, max(0.05, abort_every)))
                    except queue.Empty:
                        continue
                    if line is None:
                        if self.stop is not None and self.stop.is_set():
                            raise HelperStopped("stopping")
                        raise HelperCrashed(f"{self.name} 退出了（退出码 {process.poll()}）")
                    try:
                        answer = json.loads(line)
                    except ValueError:
                        logger.warning("%s 输出了不是 JSON 的一行，丢掉：%.200s", self.name, line)
                        continue
                    if not isinstance(answer, dict) or answer.get("id") != request_id:
                        logger.warning("%s 回答的 id 对不上，丢掉：%.200s", self.name, line)
                        continue
                    last_line = self.clock()
                    if answer.get("partial"):
                        if on_partial is not None:
                            on_partial(answer)
                        continue
                    self._last_used = self.clock()
                    return answer
            except BaseException:
                self._close_locked(graceful=False)
                raise


# ---------------------------------------------------------------------- 子进程这一侧


def serve_json_lines(
    handle: Callable[[dict[str, Any]], Iterator[dict[str, Any]] | dict[str, Any]],
) -> None:
    """子进程的主循环：stdin 一行一个请求，回答写到原来的 stdout；读到 EOF 就退出。

    先把 fd 1 指到 stderr，免得库里的 print 混进回答。handle 可以返回一个回答，或者逐行产出
    （中间行自己带 partial=true）。"""
    answers = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        request_id = request.get("id")
        try:
            result = handle(request)
            items = [result] if isinstance(result, dict) else result
            for item in items:
                answers.write(json.dumps({**item, "id": request_id}, ensure_ascii=False) + "\n")
                answers.flush()
        except Exception as error:
            answers.write(
                json.dumps(
                    {
                        "id": request_id,
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                + "\n"
            )
            answers.flush()


# ---------------------------------------------------------------------- 重启清理


def cleanup_leftovers(
    db: Any,
    data_dir: Path,
    *,
    process_list: Callable[[], str] = busy.read_process_list,
) -> dict[str, int]:
    """服务重启时、材料循环启动前：杀掉上次留下的材料转写进程（进度保留），删掉临时目录里的残留。"""
    killed = 0
    try:
        rows = db.query_all(
            "SELECT content_key, pid FROM material_media_jobs WHERE pid IS NOT NULL"
        )
    except Exception:
        rows = []
    if rows:
        try:
            listing = process_list()
        except (OSError, subprocess.SubprocessError):
            listing = ""
        alive: dict[int, str] = {}
        for line in listing.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                alive[int(parts[0])] = parts[2]
        for row in rows:
            pid = int(row["pid"])
            if "funasr_material.py" in alive.get(pid, ""):
                kill_group(pid)
                killed += 1
        db.execute("UPDATE material_media_jobs SET pid = NULL WHERE pid IS NOT NULL")
    removed = 0
    for name in ("material-asr", "material-ocr-tmp"):
        folder = Path(data_dir) / name
        if folder.is_dir():
            for child in folder.iterdir():
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    removed += 1
                except OSError:
                    logger.warning("清理临时文件失败：%s", child)
    return {"killed": killed, "removed": removed}
