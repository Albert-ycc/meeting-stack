import math
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


SCANNER_STALE_AFTER_SECONDS = 180.0
MAX_SCAN_INTERVAL_SECONDS = SCANNER_STALE_AFTER_SECONDS / 2

# 仓库根目录：config.py 位于 <repo>/workbench/backend/meeting_workbench/
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEETING_WORKBENCH_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".meeting-workbench")
    # 正式归档根：会议音频与产物的最终存放位置。可指向外置存储或 NAS 挂载点。
    archive_root: Path = Field(default_factory=lambda: Path.home() / "MeetingArchive")
    staging_root: Path = Field(
        default_factory=lambda: Path.home() / "Movies/meeting-relay-products"
    )
    # 项目 → 需求材料目录的可浏览范围（260915 新增）：浏览、挂根目录、选材料文件夹全部
    # 限制在它之下，避免服务在 Tailnet 可达时被拿来列任意目录。
    # 默认是用户主目录；项目材料放在外置存储时指向其挂载点。
    material_browse_root: Path = Field(default_factory=Path.home)
    database_path: Path | None = None
    # 默认指向本仓库内的 relay 组件；独立部署时用 MEETING_WORKBENCH_RELAY_REPO 覆盖。
    relay_repo: Path = Field(default_factory=lambda: REPO_ROOT / "relay")
    relay_jobs_db: Path = Field(
        default_factory=lambda: Path.home() / ".meeting-relay/workbench-jobs.sqlite3"
    )
    semantic_model: str = "BAAI/bge-small-zh-v1.5"
    semantic_enabled: bool = True
    csrf_cookie_name: str = "meeting_workbench_csrf"
    max_json_upload_bytes: int = 512 * 1024 * 1024
    max_json_request_bytes: int = 8 * 1024 * 1024
    upload_chunk_bytes: int = 4 * 1024 * 1024
    upload_session_ttl_seconds: int = 24 * 60 * 60
    max_incomplete_upload_bytes: int = 1024 * 1024 * 1024
    scan_interval_seconds: float = 15.0
    # Backups run every 24 hours.  Six hours of scheduling/runtime grace keeps
    # a small delay from paging while still detecting a dead daily session.
    backup_stale_after_seconds: int = 30 * 60 * 60
    qwen_binary: Path = Field(
        default_factory=lambda: Path.home() / ".venvs/mlx-qwen3-asr/bin/mlx-qwen3-asr"
    )
    qwen_model: str = "Qwen/Qwen3-ASR-0.6B"
    qwen_timeout_seconds: int = 4 * 60 * 60
    qwen_heartbeat_seconds: float = 10.0
    qwen_lease_seconds: float = 45.0
    # —— 任务抽取与飞书通知（260804 新增）——
    llm_api_base: str = "https://api.deepseek.com"
    llm_api_key_file: Path = Field(default_factory=lambda: Path.home() / ".config/ds/api-key")
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    # 飞书群自定义机器人 webhook；留空 = 通知整体关闭
    lark_webhook_url: str = ""
    # 应用机器人通道：非空时优先于 webhook，经本机 lark-cli 以 bot 身份发到该群，
    # 凭证完全复用 lark-cli 的 keychain，本服务不接触 app secret。
    lark_chat_id: str = ""
    lark_cli_bin: str = "lark-cli"
    # 自建应用直连通道（260827 新增）：配了 app_id 与 secret 文件时直接调开放
    # 平台 API 发卡片，不经 lark-cli、不依赖 GUI 会话 tmux 里的 keychain。多实例
    # 部署时各实例用各自的应用机器人走这条路，互不干扰。
    lark_app_id: str = ""
    lark_app_secret_file: Path | None = None
    # 声档服务跑在 ssh→tmux（keychain 锁定）里，lark-cli 调用必须经 GUI 会话的
    # tmux run-shell 转发；默认指向 GUI 会话里 tmux 的 cc 套接字。
    lark_tmux_socket: str = "~/.tmux-socket/cc"
    # 通知卡片跳转用的本机地址；Tailnet 域名可配
    public_base_url: str = "http://127.0.0.1:8765"
    # 停滞督办阈值（天）；同一任务督办冷却（天）
    task_stall_after_days: float = 3.0
    # 待确认草稿放多少天没处理就自动归入「已过期」；<=0 关闭。
    task_draft_expire_days: float = 7.0
    task_stall_cooldown_days: float = 2.0
    # 项目归属语义匹配相似度阈值（0~1）
    project_similarity_threshold: float = 0.62

    def model_post_init(self, __context: object) -> None:
        positive_values = {
            "max_json_upload_bytes": self.max_json_upload_bytes,
            "max_json_request_bytes": self.max_json_request_bytes,
            "upload_chunk_bytes": self.upload_chunk_bytes,
            "upload_session_ttl_seconds": self.upload_session_ttl_seconds,
            "max_incomplete_upload_bytes": self.max_incomplete_upload_bytes,
            "scan_interval_seconds": self.scan_interval_seconds,
            "backup_stale_after_seconds": self.backup_stale_after_seconds,
            "qwen_timeout_seconds": self.qwen_timeout_seconds,
            "qwen_heartbeat_seconds": self.qwen_heartbeat_seconds,
            "qwen_lease_seconds": self.qwen_lease_seconds,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if (
            not math.isfinite(self.scan_interval_seconds)
            or self.scan_interval_seconds > MAX_SCAN_INTERVAL_SECONDS
        ):
            raise ValueError(f"scan_interval_seconds 不能超过 {MAX_SCAN_INTERVAL_SECONDS:g} 秒")
        if self.upload_chunk_bytes > self.max_json_upload_bytes:
            raise ValueError("upload_chunk_bytes 不能超过 max_json_upload_bytes")
        if self.upload_chunk_bytes > self.max_incomplete_upload_bytes:
            raise ValueError("upload_chunk_bytes 不能超过 max_incomplete_upload_bytes")
        base64_bytes = ((self.upload_chunk_bytes + 2) // 3) * 4
        json_envelope_bytes = len('{"content_base64":""}'.encode("utf-8"))
        if base64_bytes + json_envelope_bytes > self.max_json_request_bytes:
            raise ValueError("upload_chunk_bytes 的 Base64 JSON 请求超过 max_json_request_bytes")
        self.data_dir = self.data_dir.expanduser()
        self.archive_root = self.archive_root.expanduser()
        self.staging_root = self.staging_root.expanduser()
        self.material_browse_root = self.material_browse_root.expanduser()
        self.relay_repo = self.relay_repo.expanduser()
        self.relay_jobs_db = self.relay_jobs_db.expanduser()
        self.qwen_binary = self.qwen_binary.expanduser()
        if self.lark_app_secret_file is not None:
            self.lark_app_secret_file = self.lark_app_secret_file.expanduser()
        if self.qwen_model != "Qwen/Qwen3-ASR-0.6B":
            raise ValueError("qwen_model 固定为 Qwen/Qwen3-ASR-0.6B")
        if self.qwen_heartbeat_seconds >= self.qwen_lease_seconds:
            raise ValueError("qwen_heartbeat_seconds 必须小于 qwen_lease_seconds")
        if self.database_path is None:
            self.database_path = self.data_dir / "workbench.sqlite3"
        else:
            self.database_path = self.database_path.expanduser()

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def peaks_dir(self) -> Path:
        return self.data_dir / "waveform-peaks"

    @property
    def relayctl_path(self) -> Path:
        return self.relay_repo / "quickstart/relayctl"
