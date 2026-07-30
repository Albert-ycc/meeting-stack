from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .hotwords import normalize_hotwords


class RelayUnavailable(RuntimeError):
    pass


class RelayClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _run(
        self,
        arguments: list[str],
        *,
        timeout: float = 30,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> str:
        executable = self.settings.relayctl_path
        if not executable.is_file():
            raise RelayUnavailable(f"relayctl 不存在：{executable}")
        environment = os.environ.copy()
        environment["MEETING_RELAY_JOBS_DB"] = str(self.settings.relay_jobs_db)
        environment["MEETING_RELAY_ARCHIVE_ROOT"] = str(self.settings.archive_root)
        # The workbench always talks to the controlled worker contract.  Do not
        # inherit an absent/legacy flag from launchd, tmux, or an SSH bootstrap.
        environment["MEETING_RELAY_CONTROL_ENABLED"] = "1"
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
            raise RelayUnavailable(f"relayctl 调用失败：{error}") from error
        if result.returncode not in allowed_returncodes:
            message = result.stderr.strip() or result.stdout.strip() or "relayctl 返回失败"
            raise RelayUnavailable(message)
        return result.stdout.strip()

    def health(self) -> dict[str, Any]:
        payload = self._json(
            self._run(
                ["health", "--json"],
                timeout=2,
                allowed_returncodes=frozenset({0, 1, 2}),
            )
        )
        if not isinstance(payload, dict) or payload.get("status") not in {
            "healthy",
            "degraded",
            "unavailable",
        }:
            raise RelayUnavailable("relayctl health 返回格式错误")
        return payload

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
    ) -> str:
        arguments = ["enqueue", str(Path(audio_path).expanduser())]
        if stage:
            arguments.extend(["--stage", stage])
        if transcript_path:
            arguments.extend(["--transcript", str(Path(transcript_path).expanduser())])
        with self._hotword_file(hotwords) as hotword_path:
            if hotword_path:
                arguments.extend(["--hotwords", str(hotword_path)])
            job_id = self._run(arguments).strip()
        if not job_id.startswith("job-"):
            raise RelayUnavailable("relayctl 未返回有效 job_id")
        return job_id

    def list_jobs(self, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        arguments = ["list", "--json", "--limit", str(limit)]
        if status:
            arguments.extend(["--status", status])
        payload = self._json(self._run(arguments))
        if isinstance(payload, dict):
            payload = payload.get("jobs", [])
        if not isinstance(payload, list):
            raise RelayUnavailable("relayctl list 返回格式错误")
        return payload

    def status(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["status", job_id, "--json"]))
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
    ) -> dict[str, Any]:
        arguments = ["retry", job_id, "--stage", stage]
        if transcript_path:
            arguments.extend(["--transcript", str(Path(transcript_path).expanduser())])
        with self._hotword_file(hotwords) as hotword_path:
            if hotword_path:
                arguments.extend(["--hotwords", str(hotword_path)])
            payload = self._json(self._run(arguments))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl retry 返回格式错误")
        return payload

    def mark_draft_modified(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["mark-draft-modified", job_id]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl 草稿状态回执格式错误")
        return payload

    def stop_after_stage(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["stop-after-stage", job_id]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl stop 返回格式错误")
        return payload

    def cancel(self, job_id: str) -> dict[str, Any]:
        payload = self._json(self._run(["cancel", job_id]))
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
                    job_id,
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
        payload = self._json(self._run(["retry-substate", job_id, "--name", name]))
        if not isinstance(payload, dict):
            raise RelayUnavailable("relayctl 子状态重试格式错误")
        return payload
