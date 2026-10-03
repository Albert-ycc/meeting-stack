import math
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


SCANNER_STALE_AFTER_SECONDS = 180.0
MAX_SCAN_INTERVAL_SECONDS = SCANNER_STALE_AFTER_SECONDS / 2

# 仓库根目录：config.py 位于 <repo>/workbench/backend/meeting_workbench/
REPO_ROOT = Path(__file__).resolve().parents[3]
# .env 按仓库位置找，跟从哪个目录启动无关：仓库根一份、workbench/ 下一份，两份都有时 workbench/ 的优先
ENV_FILES = (REPO_ROOT / ".env", REPO_ROOT / "workbench" / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEETING_WORKBENCH_",
        env_file=ENV_FILES,
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
    # 备份留几份：本机和外置盘镜像各留这么多（每份几百 MB，本机在系统盘上）。最小 1。
    backup_retention: int = 14
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
    # 通知卡片跳转用的本机地址；Tailnet 域名可配。它的主机名也会进 Host 白名单，
    # 配成 tailscale serve 的 https://<本机>.ts.net 后远程访问不用再配别的。
    public_base_url: str = "http://127.0.0.1:8765"
    # Host 白名单里额外放行的主机名（逗号分隔，精确匹配），回环地址和 public_base_url 的主机名不用写。
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # serve 的日志文件；留空 = 照旧写标准输出和标准错误。配了以后 uvicorn 的访问、错误日志和应用自己的日志
    # 都进这个文件，单个文件超过 log_max_bytes 就换新的，留 log_backup_count 份旧的（web.log.1 … web.log.N）。
    log_file: Path | None = None
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 5
    # 停滞督办阈值（天）；同一任务督办冷却（天）
    task_stall_after_days: float = 3.0
    # 待确认草稿放多少天没处理就自动归入「已过期」；<=0 关闭。
    task_draft_expire_days: float = 7.0
    task_stall_cooldown_days: float = 2.0
    # —— 材料内容（第三期）——
    # 材料的正文、图片文字、录音文字在后台慢慢读；关掉后只建文件名索引。
    material_content_enabled: bool = True
    # 材料录音转写用的 FunASR Python：本变量优先，没有时跟中转一样取
    # MEETING_RELAY_FUNASR_PYTHON，再没有用 ~/.venvs/funasr/bin/python。
    funasr_python: Path | None = None
    # 材料录音转写程序（和会议转写的程序放在一起，不改会议转写）
    material_transcriber: Path = Field(
        default_factory=lambda: REPO_ROOT / "transcribe/funasr_material.py"
    )
    # 一张图认字的超时；一份 PDF 多久没有新的一页算超时；每段录音的转写超时（实际取
    # 「音频长度 × 3」和它的较大者）。手工验收量过以后再定。
    material_image_timeout_s: float = 60.0
    material_pdf_idle_timeout_s: float = 60.0
    material_media_segment_timeout_s: float = 900.0
    # —— 深度关联（第四期）——
    # 关掉后第四期的两个循环都不启动；links_llm_enabled 关掉只跑本机的活，后台不调 AI。
    links_enabled: bool = True
    links_llm_enabled: bool = True
    # 补做多少天以内的旧会，0 表示只做新会
    links_backfill_days: int = 180
    # 后台 AI 调用每天的上限（所有后台调用共用），0 表示后台不调
    links_llm_daily_calls: int = 200
    # 问答每天的上限，0 表示问答关闭；一次问答的超时
    qa_daily_questions: int = 100
    qa_timeout_seconds: float = 90.0
    # 相关的最低门槛和比门槛多出的余量，都进台账的签名
    related_floor: float = 0.60
    related_margin: float = 0.05
    # 从材料里挖词
    glossary_mining_enabled: bool = True

    @field_validator("backup_retention", mode="before")
    @classmethod
    def _backup_retention_is_a_whole_number_of_at_least_one(cls, value: object) -> int:
        # 填错了要说人话：0 份会把刚生成的备份也轮转掉，非整数 pydantic 只会报英文的 int_parsing
        try:
            number = 0 if isinstance(value, bool) else int(str(value).strip())
        except ValueError:
            number = 0
        if number < 1:
            raise ValueError(
                f"MEETING_WORKBENCH_BACKUP_RETENTION 要写成不小于 1 的整数，比如 14，现在是 {value!r}"
            )
        return number

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [part for part in (item.strip() for item in value.split(",")) if part]
        return value

    @field_validator("log_file", mode="before")
    @classmethod
    def _blank_log_file_is_unset(cls, value: object) -> object:
        # .env 里写成 MEETING_WORKBENCH_LOG_FILE= 的意思是没配，不能当成当前目录
        return None if isinstance(value, str) and not value.strip() else value

    def trusted_hostnames(self) -> frozenset[str]:
        names = {"127.0.0.1", "localhost", "::1"}
        public_host = urlsplit(self.public_base_url).hostname
        if public_host:
            names.add(public_host.lower())
        names.update(host.strip().lower() for host in self.allowed_hosts if host.strip())
        return frozenset(names)

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
            "log_max_bytes": self.log_max_bytes,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.log_backup_count < 1:
            # 备份数为 0 时 RotatingFileHandler 永远不轮转，等于没限制
            raise ValueError("log_backup_count 至少为 1")
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
        if self.log_file is not None:
            self.log_file = self.log_file.expanduser()
        if self.qwen_model != "Qwen/Qwen3-ASR-0.6B":
            raise ValueError("qwen_model 固定为 Qwen/Qwen3-ASR-0.6B")
        if self.qwen_heartbeat_seconds >= self.qwen_lease_seconds:
            raise ValueError("qwen_heartbeat_seconds 必须小于 qwen_lease_seconds")
        if self.funasr_python is None:
            relay_python = os.environ.get("MEETING_RELAY_FUNASR_PYTHON", "").strip()
            self.funasr_python = (
                Path(relay_python) if relay_python else Path.home() / ".venvs/funasr/bin/python"
            )
        self.funasr_python = self.funasr_python.expanduser()
        self.material_transcriber = self.material_transcriber.expanduser()
        for name in (
            "material_image_timeout_s",
            "material_pdf_idle_timeout_s",
            "material_media_segment_timeout_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        for name in ("links_backfill_days", "links_llm_daily_calls", "qa_daily_questions"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能小于 0")
        if not math.isfinite(self.qa_timeout_seconds) or self.qa_timeout_seconds <= 0:
            raise ValueError("qa_timeout_seconds 必须大于 0")
        if not (math.isfinite(self.related_floor) and 0.3 <= self.related_floor <= 0.95):
            raise ValueError("related_floor 必须在 0.3 到 0.95 之间")
        if not (math.isfinite(self.related_margin) and 0 <= self.related_margin <= 0.3):
            raise ValueError("related_margin 必须在 0 到 0.3 之间")
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
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def audio_roots(self) -> tuple[Path, ...]:
        """服务肯播放、肯交给 relay 入队、肯交给影子转写的音频只能在这几个目录里。数据目录整个不放行：
        里面还有数据库、备份和各种缓存，只放行上传落盘的 uploads。"""
        return (self.archive_root, self.staging_root, self.uploads_dir)

    def in_audio_roots(self, path: Path) -> bool:
        """path 要先 resolve 过（符号链接展开之后才比）。三处用音频文件的地方都按这一条判断。"""
        return any(path.is_relative_to(root.resolve()) for root in self.audio_roots)

    @property
    def relayctl_path(self) -> Path:
        return self.relay_repo / "quickstart/relayctl"
