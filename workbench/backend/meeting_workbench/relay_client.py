from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Iterator

from .config import Settings
from .hotwords import normalize_hotwords

logger = logging.getLogger("meeting_workbench.relay_client")

# 项目提示走环境变量而不是 --project-hint：旧版 relayctl 不认识的参数会让入队直接失败，
# 不认识的环境变量则会被忽略，工作台和 relay 谁先升级都不影响出纪要。
PROJECT_HINT_ENV = "MEETING_RELAY_PROJECT_HINT"

# relay 入队时生成 "job-" 加 16 位小写十六进制（生产的任务号全是这个形状）。这里放宽到字母数字、
# 下划线、连字符，只为保证任务号进 relayctl 的 argv 后不会被当成选项、路径或带空白的串。
_JOB_ID_RE = re.compile(r"job-[0-9A-Za-z_-]{1,64}")

# 按任务号向 relayctl list 点名时一次最多带多少个。每个任务号在 argv 里占 30 来个字节，
# 1000 个约 30KB，离系统的参数总长上限（macOS 约 1MB）很远；生产现有 169 场会，一次就取完。
JOB_IDS_PER_LIST = 1000


class RelayUnavailable(RuntimeError):
    pass


# 进程内加载的 relay_control 模块，按文件路径存 ((mtime_ns, size), 模块)。
_control_modules: dict[Path, tuple[tuple[int, int], ModuleType]] = {}
_control_modules_lock = threading.Lock()


def load_relay_control(path: Path) -> ModuleType:
    """把 relay 的 relay_control.py 加载进本进程。

    文件的 (mtime, 大小) 变了（升级 relay 后换了文件）就重新加载：原来每次起 relayctl 读的都是磁盘上
    最新的代码，这里不能变成要重启工作台才认。不登记进 sys.modules，不动 sys.path。
    """
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _control_modules_lock:
        cached = _control_modules.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        spec = importlib.util.spec_from_file_location("relay_control", path)
        if spec is None or spec.loader is None:
            raise ImportError("relay_control 加载不了")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _control_modules[path] = (stamp, module)
        return module


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise RelayUnavailable("任务号格式不对")
    return job_id


class RelayClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _run(
        self,
        arguments: list[str],
        *,
        timeout: float = 30,
        allowed_returncodes: frozenset[int] = frozenset({0}),
        extra_env: dict[str, str] | None = None,
    ) -> str:
        executable = self.settings.relayctl_path
        if not executable.is_file():
            # 绝对路径只进日志：错误文案会原样回给浏览器，经 Tailscale 远程访问时不该露出本机目录
            logger.error("relayctl 不存在：%s", executable)
            raise RelayUnavailable("中转程序找不到，详见服务日志")
        environment = os.environ.copy()
        environment["MEETING_RELAY_JOBS_DB"] = str(self.settings.relay_jobs_db)
        environment["MEETING_RELAY_ARCHIVE_ROOT"] = str(self.settings.archive_root)
        # The workbench always talks to the controlled worker contract.  Do not
        # inherit an absent/legacy flag from launchd, tmux, or an SSH bootstrap.
        environment["MEETING_RELAY_CONTROL_ENABLED"] = "1"
        environment.pop(PROJECT_HINT_ENV, None)
        environment.update(extra_env or {})
        try:
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=self.settings.relay_repo,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.error("relayctl 调用失败：%s", error)
            raise RelayUnavailable("relayctl 调用失败，详见服务日志") from error
        if result.returncode not in allowed_returncodes:
            detail = result.stderr.strip() or result.stdout.strip() or "无输出"
            logger.error("relayctl 返回码 %s：%s", result.returncode, detail)
            raise RelayUnavailable("relayctl 返回失败，详见服务日志")
        return result.stdout.strip()

    def health(self) -> dict[str, Any]:
        """relay 的运行健康。探测循环每 5 秒调一次，所以在进程内调 relay 自己的 RelayControl.health：
        原来每次起一个 relayctl 子进程，解释器启动加编译 7 千行的 relay_control，实测约 57ms CPU/次。
        字段和含义由同一份代码保证，不在工作台这边另写一遍。

        control_enabled 恒为 True，和 _run 同一个理由：工作台只认受控 worker 的契约，不继承
        launchd、tmux、SSH 启动时可能缺的开关。"""
        module_path = self.settings.relay_repo / "quickstart" / "relay_control.py"
        if not module_path.is_file():
            # 绝对路径只进日志，理由同 _run
            logger.error("relay_control 不存在：%s", module_path)
            raise RelayUnavailable("中转程序找不到，详见服务日志")
        try:
            control = load_relay_control(module_path).RelayControl(
                self.settings.relay_jobs_db,
                archive_root=self.settings.archive_root,
                initialize=False,
            )
            payload = control.health(control_enabled=True)
        except Exception as error:
            logger.error("relay 健康读取失败：%s", error)
            raise RelayUnavailable("relay 健康读取失败，详见服务日志") from error
        if not isinstance(payload, dict) or payload.get("status") not in {
            "healthy",
            "degraded",
            "unavailable",
        }:
            raise RelayUnavailable("relay health 返回格式错误")
        return payload

    @staticmethod
    def _hint_env(project_hint: str | None) -> dict[str, str]:
        hint = " ".join((project_hint or "").split())[:200]
        return {PROJECT_HINT_ENV: hint} if hint else {}

    @staticmethod
    def _json(value: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise RelayUnavailable("relayctl 返回了无效 JSON") from error

    @contextmanager
    def _hotword_file(self, hotwords: list[str] | None) -> Iterator[Path | None]:
        normalized = normalize_hotwords(hotwords)
        if not normalized:
            yield None
            return
        root = self.settings.data_dir / "relay-inputs"
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise RelayUnavailable("Relay 私有输入目录不可用")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix="hotwords-", suffix=".txt", dir=root)
        path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(normalized) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            yield path
        finally:
            path.unlink(missing_ok=True)

    def enqueue(
        self,
        audio_path: str | Path,
        *,
        stage: str | None = None,
        transcript_path: str | Path | None = None,
        hotwords: list[str] | None = None,
        backend: str | None = None,
        project_hint: str | None = None,
    ) -> str:
        arguments = ["enqueue", str(Path(audio_path).expanduser())]
        if stage:
            arguments.extend(["--stage", stage])
        if backend:
            arguments.extend(["--backend", backend])
        if transcript_path:
            arguments.extend(["--transcript", str(Path(transcript_path).expanduser())])
        with self._hotword_file(hotwords) as hotword_path:
            if hotword_path:
                arguments.extend(["--hotwords", str(hotword_path)])
            job_id = self._run(arguments, extra_env=self._hint_env(project_hint)).strip()
        if not _JOB_ID_RE.fullmatch(job_id):
            raise RelayUnavailable("relayctl 未返回有效 job_id")
        return job_id

    def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
        job_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if job_ids is not None and not job_ids:
            return []
        arguments = ["list", "--json", "--limit", str(limit)]
        if status:
            arguments.extend(["--status", status])
        for job_id in job_ids or ():
            arguments.extend(["--job-id", check_job_id(job_id)])
        payload = self._json(self._run(arguments))
        if isinstance(payload, dict):
            payload = payload.get("jobs", [])
        if not isinstance(payload, list):
            raise RelayUnavailable("relayctl list 返回格式错误")
        return payload

    def jobs_by_id(self, job_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """按任务号一批取回任务摘要（含 current_attempt、index_status）。点名的任务不受 list
        500 行上限截断；格式不对的任务号不可能在 relay 里，当作查不到，不往 relayctl 传。"""
        wanted = [
            job_id
            for job_id in dict.fromkeys(job_ids)
            if isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id)
        ]
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), JOB_IDS_PER_LIST):
            for job in self.list_jobs(job_ids=wanted[start : start + JOB_IDS_PER_LIST]):
                if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
                    raise RelayUnavailable("relayctl list 返回格式错误")
                found[job["job_id"]] = job
        return found

    def status(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["status", check_job_id(job_id), "--json"]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl status 返回格式错误")
        return payload

    def retry(
        self,
        job_id: str,
        stage: str,
        *,
        transcript_path: str | Path | None = None,
        hotwords: list[str] | None = None,
        backend: str | None = None,
        project_hint: str | None = None,
    ) -> dict[str, Any]:
        arguments = ["retry", check_job_id(job_id), "--stage", stage]
        if transcript_path:
            arguments.extend(["--transcript", str(Path(transcript_path).expanduser())])
        if backend:
            arguments.extend(["--backend", backend])
        with self._hotword_file(hotwords) as hotword_path:
            if hotword_path:
                arguments.extend(["--hotwords", str(hotword_path)])
            payload = self._json(self._run(arguments, extra_env=self._hint_env(project_hint)))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl retry 返回格式错误")
        return payload

    def mark_draft_modified(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["mark-draft-modified", check_job_id(job_id)]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl 草稿状态回执格式错误")
        return payload

    def stop_after_stage(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["stop-after-stage", check_job_id(job_id)]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl stop 返回格式错误")
        return payload

    def cancel(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["cancel", check_job_id(job_id)]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl cancel 返回格式错误")
        return payload

    def mark_published(
        self, job_id: str, manifest_path: str | Path, meeting_id: str
    ) -> dict[str, Any]:
        payload = self._json(
            self._run(
                [
                    "mark-published",
                    check_job_id(job_id),
                    "--manifest",
                    str(Path(manifest_path)),
                    "--meeting-id",
                    meeting_id,
                ]
            )
        )
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl 发布回执返回格式错误")
        if (
            payload.get("job_id") != job_id
            or payload.get("meeting_id") != meeting_id
            or payload.get("status") != "published"
        ):
            raise RelayUnavailable("relayctl 发布回执与请求不一致")
        return payload

    def set_substate(
        self,
        job_id: str,
        name: str,
        status: str,
        error: str | None = None,
        *,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        check_job_id(job_id)
        if attempt is None:
            current = self.status(job_id).get("current_attempt")
            if isinstance(current, bool) or not isinstance(current, int):
                raise RelayUnavailable("relayctl status 缺少有效 current_attempt")
            attempt = current
        arguments = ["set-substate", job_id, "--name", name, "--status", status]
        if error:
            arguments.extend(["--error", error])
        arguments.extend(["--attempt", str(attempt)])
        payload = self._json(self._run(arguments))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl 子状态回执格式错误")
        return payload

    def retry_substate(self, job_id: str, name: str) -> dict[str, Any]:
        payload = self._json(self._run(["retry-substate", check_job_id(job_id), "--name", name]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl 子状态重试格式错误")
        return payload
