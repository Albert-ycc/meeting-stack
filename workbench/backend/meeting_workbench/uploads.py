from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import fcntl

from .config import Settings
from .hotwords import HotwordValidationError, normalize_hotwords


ALLOWED_UPLOAD_EXTENSIONS = {".m4a", ".mp3", ".wav"}
UPLOAD_ID_RE = re.compile(r"^upload-[0-9a-f]{32}$")
UPLOAD_ENQUEUE_LEASE_SECONDS = 60


class UploadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UploadSession:
    upload_id: str
    chunk_bytes: int
    chunk_count: int


class UploadManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions_root = settings.data_dir / "uploads" / ".sessions"
        self.destination_root = settings.data_dir / "uploads"

    def start(
        self, filename: str, size_bytes: int, *, hotwords: list[str] | None = None
    ) -> UploadSession:
        self.cleanup_expired()
        try:
            normalized_hotwords = normalize_hotwords(hotwords)
        except HotwordValidationError as error:
            raise UploadError(str(error)) from error
        safe_name = Path(filename).name
        if (
            not safe_name
            or safe_name.startswith(".")
            or Path(safe_name).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS
        ):
            raise UploadError("仅支持 m4a、mp3、wav 录音")
        if size_bytes <= 0 or size_bytes > self.settings.max_json_upload_bytes:
            raise UploadError("录音大小无效或超过限制")
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        with self._quota_lock():
            if (
                self._reserved_incomplete_bytes() + size_bytes
                > self.settings.max_incomplete_upload_bytes
            ):
                raise UploadError("未完成上传占用已达到上限，请稍后重试或取消旧上传")
            upload_id = f"upload-{uuid.uuid4().hex}"
            session_dir = self.sessions_root / upload_id
            session_dir.mkdir(parents=True, exist_ok=False)
            chunk_count = (
                size_bytes + self.settings.upload_chunk_bytes - 1
            ) // self.settings.upload_chunk_bytes
            metadata = {
                "upload_id": upload_id,
                "filename": safe_name,
                "size_bytes": size_bytes,
                "chunk_bytes": self.settings.upload_chunk_bytes,
                "chunk_count": chunk_count,
                "hotwords": normalized_hotwords,
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self._atomic_text(
                session_dir / "metadata.json", json.dumps(metadata, ensure_ascii=False)
            )
        return UploadSession(upload_id, self.settings.upload_chunk_bytes, chunk_count)

    def write_chunk(self, upload_id: str, index: int, content_base64: str) -> dict[str, int | str]:
        try:
            payload = base64.b64decode(content_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise UploadError("Base64 分块无效") from error
        session_dir, _metadata = self._session(upload_id)
        with self._session_lock(session_dir):
            session_dir, metadata = self._session(upload_id)
            chunk_count = int(metadata["chunk_count"])
            if index < 0 or index >= chunk_count:
                raise UploadError("分块序号越界")
            expected = int(metadata["chunk_bytes"])
            if len(payload) > expected:
                raise UploadError("分块超过限制")
            if index < chunk_count - 1 and len(payload) != expected:
                raise UploadError("非末尾分块大小不完整")
            path = session_dir / f"{index:08d}.part"
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=session_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
            except Exception:
                Path(temporary_name).unlink(missing_ok=True)
                raise
            metadata["updated_at"] = datetime.now(UTC).isoformat()
            self._atomic_text(
                session_dir / "metadata.json", json.dumps(metadata, ensure_ascii=False)
            )
        return {
            "upload_id": upload_id,
            "index": index,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def complete(self, upload_id: str) -> Path:
        session_dir, _metadata = self._session(upload_id)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        with self._quota_lock(), self._session_lock(session_dir):
            session_dir, metadata = self._session(upload_id)
            receipt_path = session_dir / "receipt.json"
            self.destination_root.mkdir(parents=True, exist_ok=True)
            destination = self.destination_root / f"{upload_id}-{metadata['filename']}"
            expected_size = int(metadata["size_bytes"])

            if receipt_path.is_file() and not receipt_path.is_symlink():
                receipt = self._validated_receipt(receipt_path, destination, expected_size)
                if not receipt.get("sha256"):
                    receipt["sha256"] = self._sha256(destination)
                    self._atomic_text(receipt_path, json.dumps(receipt, ensure_ascii=False))
                self._cleanup_parts(session_dir, int(metadata["chunk_count"]))
                return destination
            if receipt_path.exists():
                raise UploadError("上传完成回执损坏")

            if destination.exists():
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or destination.stat().st_size != expected_size
                ):
                    raise UploadError("上传目标文件存在但与会话不匹配")
            else:
                chunks = [
                    session_dir / f"{index:08d}.part"
                    for index in range(int(metadata["chunk_count"]))
                ]
                if any(not path.is_file() or path.is_symlink() for path in chunks):
                    raise UploadError("上传分块尚未齐全")
                if sum(path.stat().st_size for path in chunks) != expected_size:
                    raise UploadError("上传总大小不一致")
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.", dir=self.destination_root
                )
                try:
                    with os.fdopen(descriptor, "wb") as target:
                        for chunk in chunks:
                            with chunk.open("rb") as source:
                                shutil.copyfileobj(source, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary_name, destination)
                    self._fsync_file(destination)
                    self._fsync_directory(self.destination_root)
                except Exception:
                    Path(temporary_name).unlink(missing_ok=True)
                    raise

            receipt = {
                "upload_id": upload_id,
                "path": str(destination),
                "size_bytes": expected_size,
                "sha256": self._sha256(destination),
                "status": "saved_pending_enqueue",
                "job_id": None,
                "hotwords": list(metadata.get("hotwords") or []),
            }
            self._atomic_text(receipt_path, json.dumps(receipt, ensure_ascii=False))
            self._cleanup_parts(session_dir, int(metadata["chunk_count"]))
            return destination

    def claim_enqueue(
        self,
        upload_id: str,
        owner_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, object] | None:
        if not owner_id or len(owner_id) > 200:
            raise UploadError("入队 owner 无效")
        session_dir, metadata = self._session(upload_id)
        receipt_path = session_dir / "receipt.json"
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_lock(session_dir):
            destination = self.destination_root / f"{upload_id}-{metadata['filename']}"
            receipt = self._validated_receipt(
                receipt_path, destination, int(metadata["size_bytes"])
            )
            status = receipt.get("status")
            if status == "queued":
                return receipt
            if status == "enqueueing":
                try:
                    lease_expires_at = datetime.fromisoformat(
                        str(receipt["enqueue_lease_expires_at"])
                    )
                    if lease_expires_at.tzinfo is None:
                        lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
                except (KeyError, TypeError, ValueError):
                    lease_expires_at = datetime.min.replace(tzinfo=UTC)
                if lease_expires_at.astimezone(UTC) > current:
                    return None
            elif status != "saved_pending_enqueue":
                raise UploadError("上传入队回执状态无效")
            receipt.update(
                {
                    "status": "enqueueing",
                    "enqueue_owner": owner_id,
                    "enqueue_started_at": current.isoformat(),
                    "enqueue_lease_expires_at": (
                        current + timedelta(seconds=UPLOAD_ENQUEUE_LEASE_SECONDS)
                    ).isoformat(),
                }
            )
            self._atomic_text(receipt_path, json.dumps(receipt, ensure_ascii=False))
            return receipt

    def release_enqueue(self, upload_id: str, owner_id: str) -> None:
        session_dir, _metadata = self._session(upload_id)
        receipt_path = session_dir / "receipt.json"
        with self._session_lock(session_dir):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise UploadError("上传完成回执损坏") from error
            if not isinstance(receipt, dict):
                raise UploadError("上传完成回执损坏")
            if (
                receipt.get("status") != "enqueueing"
                or receipt.get("enqueue_owner") != owner_id
            ):
                return
            receipt["status"] = "saved_pending_enqueue"
            for key in ("enqueue_owner", "enqueue_started_at", "enqueue_lease_expires_at"):
                receipt.pop(key, None)
            self._atomic_text(receipt_path, json.dumps(receipt, ensure_ascii=False))

    def mark_enqueued(
        self, upload_id: str, job_id: str, *, owner_id: str | None = None
    ) -> dict[str, object]:
        session_dir, _metadata = self._session(upload_id)
        receipt_path = session_dir / "receipt.json"
        with self._session_lock(session_dir):
            if not receipt_path.is_file() or receipt_path.is_symlink():
                raise UploadError("上传尚未完成")
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise UploadError("上传完成回执损坏") from error
            if not isinstance(receipt, dict):
                raise UploadError("上传完成回执损坏")
            if receipt.get("status") == "queued":
                if receipt.get("job_id") != job_id:
                    raise UploadError("上传已绑定其他 Relay 任务")
                return receipt
            if owner_id is not None and (
                receipt.get("status") != "enqueueing"
                or receipt.get("enqueue_owner") != owner_id
            ):
                raise UploadError("上传入队所有权已变化")
            receipt.update({"status": "queued", "job_id": job_id})
            for key in ("enqueue_owner", "enqueue_started_at", "enqueue_lease_expires_at"):
                receipt.pop(key, None)
            self._atomic_text(receipt_path, json.dumps(receipt, ensure_ascii=False))
            return receipt

    def receipt(self, upload_id: str) -> dict[str, object]:
        session_dir, _metadata = self._session(upload_id)
        path = session_dir / "receipt.json"
        if not path.is_file() or path.is_symlink():
            raise UploadError("上传尚未完成")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UploadError("上传完成回执损坏") from error
        if not isinstance(payload, dict):
            raise UploadError("上传完成回执损坏")
        return payload

    def pending(self) -> list[dict[str, object]]:
        if not self.sessions_root.is_dir():
            return []
        pending: list[dict[str, object]] = []
        for session_dir in self.sessions_root.iterdir():
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            receipt = session_dir / "receipt.json"
            try:
                payload = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("status") in {"saved_pending_enqueue", "enqueueing"}
                and isinstance(payload.get("path"), str)
                and Path(payload["path"]).is_file()
            ):
                try:
                    payload["hotwords"] = normalize_hotwords(payload.get("hotwords"))
                except HotwordValidationError:
                    continue
                pending.append(payload)
        return pending

    def cancel(self, upload_id: str) -> None:
        session_dir, _metadata = self._session(upload_id)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        with self._quota_lock(), self._session_lock(session_dir):
            if (session_dir / "receipt.json").exists():
                raise UploadError("录音已完成保存，不能作为未完成上传取消")
            shutil.rmtree(session_dir)

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        if not self.sessions_root.is_dir():
            return 0
        with self._quota_lock():
            return self._cleanup_expired_locked(now=now)

    def _cleanup_expired_locked(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        removed = 0
        for session_dir in list(self.sessions_root.iterdir()):
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            with self._session_lock(session_dir):
                metadata_path = session_dir / "metadata.json"
                receipt_path = session_dir / "receipt.json"
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    updated_at = datetime.fromisoformat(str(metadata["updated_at"]))
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    updated_at = datetime.fromtimestamp(session_dir.stat().st_mtime, tz=UTC)
                age = (current - updated_at.astimezone(UTC)).total_seconds()
                if age <= self.settings.upload_session_ttl_seconds:
                    continue
                if receipt_path.is_file():
                    try:
                        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        receipt = None
                    if (
                        isinstance(receipt, dict)
                        and receipt.get("status") in {"saved_pending_enqueue", "enqueueing"}
                    ):
                        continue
                shutil.rmtree(session_dir)
                removed += 1
        return removed

    def _reserved_incomplete_bytes(self) -> int:
        if not self.sessions_root.is_dir():
            return 0
        reserved = 0
        for session_dir in self.sessions_root.iterdir():
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            if (session_dir / "receipt.json").is_file():
                continue
            try:
                metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
                reserved += int(metadata["size_bytes"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return reserved

    def _session(self, upload_id: str) -> tuple[Path, dict[str, object]]:
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            raise UploadError("upload_id 无效")
        session_dir = self.sessions_root / upload_id
        metadata_path = session_dir / "metadata.json"
        if session_dir.is_symlink() or metadata_path.is_symlink() or not metadata_path.is_file():
            raise UploadError("上传会话不存在")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UploadError("上传会话损坏") from error
        if not isinstance(metadata, dict) or metadata.get("upload_id") != upload_id:
            raise UploadError("上传会话不匹配")
        return session_dir, metadata

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            UploadManager._fsync_file(path)
            UploadManager._fsync_directory(path.parent)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    @contextmanager
    def _quota_lock(self) -> Iterator[None]:
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        with (self.sessions_root / ".quota.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    @contextmanager
    def _session_lock(session_dir: Path) -> Iterator[None]:
        with (session_dir / ".session.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validated_receipt(
        receipt_path: Path, destination: Path, expected_size: int
    ) -> dict[str, object]:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_destination = Path(str(receipt["path"]))
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise UploadError("上传完成回执损坏") from error
        if (
            not isinstance(receipt, dict)
            or receipt_destination != destination
            or destination.is_symlink()
            or not destination.is_file()
            or destination.stat().st_size != expected_size
            or int(receipt.get("size_bytes", -1)) != expected_size
        ):
            raise UploadError("上传完成回执损坏")
        expected_hash = receipt.get("sha256")
        if expected_hash and expected_hash != UploadManager._sha256(destination):
            raise UploadError("上传完成回执损坏")
        return receipt

    @staticmethod
    def _cleanup_parts(session_dir: Path, chunk_count: int) -> None:
        for index in range(chunk_count):
            (session_dir / f"{index:08d}.part").unlink(missing_ok=True)
        (session_dir / ".completing").unlink(missing_ok=True)
        UploadManager._fsync_directory(session_dir)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
