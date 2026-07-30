"""
Relay watchdog：监听 ~/Downloads 新音频 → 转写 → 按时长分流派给 tmux cc1。

链路全景：
  录音来源（iPhone 语音备忘录经 voicememos_bridge.py，或任何手动放入的音频）
  → 落到监听目录（默认 ~/Downloads，vm-*.m4a 等）
  → 本脚本检测 → transcribe.sh 转写（FunASR 主稿 + openai-whisper turbo 对照稿）
  → 短录音(<10min)：转写文本作为即时指令派给 AI Agent
  → 长录音(≥10min)：派 AI Agent 生成会议纪要，归档规范固化在派单 prompt 里
  → 可选通知（未配置则跳过）

几条来自实跑的设计约束，改动前请先理解：
  1. 长录音必须用带温度回退的 ASR。无温度回退的实现在长音频上会整段幻觉，
     曾把一段 90 分钟录音转成完全虚构的内容而无任何报错。
  2. 归档命名规范固化进派单 prompt（统一「YYMMDD 主题」6 位日期、音频必归档），
     不靠 Agent 自由发挥，否则同一批录音会出现多种目录格式混杂。
  3. 同场会连续分段检测：与上一场派单间隔 < 45 分钟时，prompt 要求 Agent 先检查
     是否应并入上一场的文件夹，避免一段录音一个文件夹。

⚠️ macOS TCC 权限：读监听目录、写外置存储的授权跟随启动进程链。用 launchd 直接拉起
   往往被 TCC 拦住，实测经 ssh localhost 链（sshd 通常已在完全磁盘访问名单内）继承放行。
   详见 docs/install.md 的权限章节。
"""
import json
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ssh localhost 链只给裸 PATH（/usr/bin:/bin:/usr/sbin:/sbin），ffprobe 和
# whisper 解码用的 ffmpeg 通常装在 Homebrew 里，这里自愈补全；子进程（含
# transcribe.sh）继承 os.environ，一处补全全链路生效。
# 缺了它的典型症状：长录音在 ffprobe 探测时长处抛 FileNotFoundError 后静默搁浅。
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

# 仓库根：本文件位于 <repo>/relay/quickstart/
REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    """读环境变量里的路径，支持 ~ 展开；未设置则用默认值。"""
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else default


# ── 配置 ─────────────────────────────────────────────────────────────────────
INBOX                  = _env_path("MEETING_RELAY_WATCH_DIR", Path.home() / "Downloads")
PRODUCTS_DIR           = _env_path(
    "MEETING_RELAY_PRODUCTS_ROOT", Path.home() / "Movies" / "meeting-relay-products"
)
STATE_DIR              = _env_path("MEETING_RELAY_STATE_DIR", Path.home() / ".meeting-relay")
PROCESSED_LOG          = STATE_DIR / "processed.txt"
PROMPTS_DIR            = STATE_DIR / "prompts"
LAST_MEETING_FILE      = STATE_DIR / "last_meeting.json"
DEFAULT_PROMPT_FILE    = STATE_DIR / "prompt-default.txt"   # 转写词典模板（可选）
ARCHIVE_ROOT           = _env_path("MEETING_RELAY_ARCHIVE_ROOT", Path.home() / "MeetingArchive")
# 转写脚本默认取仓库内的实现；单独部署时用 MEETING_RELAY_TRANSCRIBE_SH 指向别处。
TRANSCRIBE_SH          = _env_path(
    "MEETING_RELAY_TRANSCRIBE_SH", REPO_ROOT / "transcribe" / "transcribe.sh"
)
TRANSCRIBE_DUAL_SH     = _env_path(
    "MEETING_RELAY_TRANSCRIBE_DUAL_SH", REPO_ROOT / "transcribe" / "transcribe-dual.sh"
)
# 派单目标：Agent 跑在哪个 tmux socket 的哪个 session 里。
TMUX_SOCKET            = _env_path("MEETING_RELAY_TMUX_SOCKET", Path.home() / ".tmux-socket" / "cc")
TMUX_SESSION           = os.getenv("MEETING_RELAY_TMUX_SESSION", "agent")
DEFAULT_AGENT          = "claude"
CLAUDE_BIN             = os.getenv("MEETING_RELAY_CLAUDE_BIN", "")
CLAUDE_FALLBACKS       = (
    str(Path.home() / ".local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)
CODEX_BIN              = os.getenv("MEETING_RELAY_CODEX_BIN", "")
CODEX_FALLBACKS        = (
    str(Path.home() / ".local/bin/codex"),
    "/Applications/Codex.app/Contents/Resources/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)
# 可选的飞书（Lark）通知：留空则整个通知环节静默跳过，不影响转写与归档主流程。
# 需要通知时设置为自己的 open_id，并确保 lark-cli 已登录。
LARK_USER_ID           = os.getenv("RELAY_LARK_USER_ID", "")
LARK_LOG_FILE          = Path.home() / "Library" / "Logs" / "meeting-relay-notify.log"
LARK_NOTIFY_PATH       = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
AUDIO_EXTS             = {".m4a", ".mp3", ".wav"}
STABLE_SECS            = 5
CONTROL_POLL_INTERVAL_SEC = float(os.getenv("MEETING_RELAY_POLL_INTERVAL", "2"))


def _positive_interval_from_env(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number greater than zero") from exc
    if not 0 < value < float("inf"):
        raise ValueError(f"{name} must be greater than zero")
    return value


PENDING_RECONCILE_INTERVAL_SEC = _positive_interval_from_env(
    "MEETING_RELAY_PENDING_RECONCILE_INTERVAL", "30"
)
CODEX_CALLBACK_GRACE_SEC = float(os.getenv("MEETING_RELAY_CODEX_CALLBACK_GRACE", "30"))
RUNTIME_HEARTBEAT_INTERVAL_SEC = 10.0
CONTROL_ERROR_BACKOFF_MAX_SEC = 30.0
MAX_INPUT_TRANSCRIPT_BYTES = 64 * 1024 * 1024
CURRENT_MINUTES_PROTOCOL_VERSION = 3
WHISPER_BIN = os.getenv("MEETING_RELAY_WHISPER_BIN", "")
WHISPER_FALLBACKS = (
    str(Path.home() / "Library" / "Python" / "3.9" / "bin" / "whisper"),
)
WHISPER_MODEL_DIR = Path(
    os.getenv("MEETING_RELAY_WHISPER_MODEL_DIR", str(Path.home() / ".cache" / "whisper"))
).expanduser()
WHISPER_MODEL_FILE = "large-v3-turbo.pt"
WHISPER_MODEL_SHA256 = "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a"
_WHISPER_MODEL_CACHE: tuple[int, int, str] | None = None

# 时长阈值：超过此值走会议纪要分支
LONG_AUDIO_THRESHOLD_SEC = 600  # 10 分钟

# 同场会连续分段判定：与上一场会议派单间隔小于此值时提示 cc1 合并归档
SAME_MEETING_GAP_SEC = 45 * 60

# 紧急回退开关（出问题时一行 export 即可关掉双轨路径）
DISABLE_DUAL = os.getenv("RELAY_DISABLE_DUAL") == "1"
# ──────────────────────────────────────────────────────────────────────────────

STARTUP_EPOCH = time.time()
_next_pending_reconcile_at = 0.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def control_enabled() -> bool:
    """默认关闭，完整预发布验收后再显式切换。"""
    return os.getenv("MEETING_RELAY_CONTROL_ENABLED") == "1"


def _relay_control_module():
    # 兼容「python quickstart/relay_watchdog.py」和从仓库根目录导入测试两种入口。
    try:
        from quickstart import relay_control
    except ImportError:
        import relay_control
    return relay_control


def _control_enqueue(audio: Path) -> str:
    # 文件创建事件不能做大音频哈希；worker 等稳定后再记录真源哈希。
    return _relay_control_module().enqueue(audio, compute_hash=False)


def _control_claim_next() -> dict | None:
    relay_control = _relay_control_module()
    return relay_control.claim_next(worker_id=relay_control.make_worker_id("watchdog"))


def _control_recover_orphaned_claims() -> int:
    return _relay_control_module().recover_orphaned_claims()


def _control_reconcile_codex_handoffs() -> int:
    return _relay_control_module().reconcile_codex_handoffs(
        grace_seconds=CODEX_CALLBACK_GRACE_SEC
    )


def _control_reconcile_pending_archives() -> dict:
    return _relay_control_module().reconcile_pending_archives()


def _control_runtime_heartbeat(
    name: str,
    *,
    mode: str,
    status: str,
    current_job_id: str | None = None,
    current_stage: str | None = None,
    last_error: str | None = None,
):
    return _relay_control_module().heartbeat_worker(
        name,
        mode=mode,
        status=status,
        current_job_id=current_job_id,
        current_stage=current_stage,
        last_error=last_error,
    )


def _best_effort_runtime_heartbeat(name: str, **kwargs):
    try:
        return _control_runtime_heartbeat(name, **kwargs)
    except Exception as exc:
        log.warning("Relay runtime 心跳写入失败：%s", type(exc).__name__)
        return None


def _run_with_heartbeat(
    operation,
    *,
    heartbeat,
    heartbeat_interval: float = RUNTIME_HEARTBEAT_INTERVAL_SEC,
    poll_interval: float = 1.0,
):
    """Run a blocking preflight operation while the worker lease stays fresh."""
    outcome = {}

    def run():
        try:
            outcome["value"] = operation()
        except BaseException as exc:
            outcome["error"] = exc

    runner = threading.Thread(
        target=run,
        name="relay-heartbeating-operation",
        daemon=True,
    )
    runner.start()
    next_heartbeat = 0.0
    while runner.is_alive():
        now = time.monotonic()
        if now >= next_heartbeat:
            heartbeat()
            next_heartbeat = now + max(0.0, heartbeat_interval)
        runner.join(timeout=max(0.0, poll_interval))
    runner.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _ensure_runtime_components_alive(observer, worker, *, mode: str) -> None:
    failures = []
    if not observer.is_alive():
        failures.append(("observer", "observer_stopped"))
    if mode == "controlled" and (worker is None or not worker.is_alive()):
        failures.append(("control-worker", "control_worker_stopped"))
    if not failures:
        return
    _best_effort_runtime_heartbeat(
        "watchdog",
        mode=mode,
        status="degraded",
        last_error=",".join(error for _name, error in failures),
    )
    raise RuntimeError(
        "Relay runtime component stopped: " + ", ".join(name for name, _error in failures)
    )


def _control_runtime_stop(name: str) -> None:
    try:
        _relay_control_module().stop_worker(name)
    except Exception as exc:
        log.warning("Relay runtime 停止状态写入失败：%s", type(exc).__name__)


def _control_record_stage(
    job_id: str,
    stage: str,
    *,
    expected_attempt: int,
    expected_worker: str,
):
    return _relay_control_module().record_stage(
        job_id,
        stage,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def _control_record_substate(
    job_id: str,
    name: str,
    status: str,
    error: str | None = None,
    attempt_no: int | None = None,
):
    return _relay_control_module().record_substate(
        job_id,
        name,
        status,
        error=error,
        attempt_no=attempt_no,
    )


def _control_update_whisper_progress(
    job_id: str,
    products_subdir: Path,
    *,
    async_expected: bool,
    attempt_no: int,
):
    try:
        inspection = _relay_control_module().inspect_whisper_ref(products_subdir)
        status = str(inspection["status"])
        if status == "absent" and async_expected:
            status = "running"
        error = None
        if status == "failed":
            error = ", ".join(str(item) for item in inspection.get("missing", []))
        return _control_record_substate(
            job_id,
            "whisper",
            status,
            error=error,
            attempt_no=attempt_no,
        )
    except Exception as exc:
        # 对照稿是旁路，探测或台账写入异常不能反过来阻塞 FunASR 主链。
        log.warning("Whisper 子状态更新失败（不阻塞主链）: %s", type(exc).__name__)
        return None


def _control_record_source_audio(
    job_id: str,
    audio: Path,
    *,
    expected_attempt: int,
    expected_worker: str,
):
    return _relay_control_module().record_source_audio(
        job_id,
        audio,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def _control_record_minutes_plan_source(
    job_id: str,
    *,
    attempt_no: int,
    source_srt_sha256: str,
    minutes_plan_sha256: str,
    expected_worker: str | None = None,
) -> dict:
    return _relay_control_module().record_minutes_plan_source(
        job_id,
        attempt_no=attempt_no,
        source_srt_sha256=source_srt_sha256,
        minutes_plan_sha256=minutes_plan_sha256,
        expected_worker=expected_worker,
    )


def _control_prepare_attempt_dir(
    job_id: str,
    *,
    attempt_no: int,
    expected_worker: str,
) -> Path:
    return Path(
        _relay_control_module().prepare_attempt_draft(
            job_id,
            attempt_no=attempt_no,
            expected_worker=expected_worker,
        )
    )


def _control_record_codex_dispatched(
    job_id: str,
    *,
    expected_attempt: int,
    expected_worker: str,
):
    return _relay_control_module().record_codex_dispatched(
        job_id,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def _control_fail(
    job_id: str,
    stage: str,
    error: str,
    *,
    expected_attempt: int,
    expected_worker: str,
):
    return _relay_control_module().fail(
        job_id,
        stage,
        error,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


def _control_finish_whisper_retry(
    job_id: str,
    *,
    attempt_no: int,
    generation: int,
    worker_id: str,
    success: bool,
    error: str | None = None,
    artifact_dir: str | Path | None = None,
):
    return _relay_control_module().finish_whisper_retry(
        job_id,
        attempt_no=attempt_no,
        generation=generation,
        worker_id=worker_id,
        success=success,
        error=error,
        artifact_dir=artifact_dir,
    )


def _control_interrupt_if_requested(
    job_id: str,
    completed_stage: str,
    *,
    expected_attempt: int,
    expected_worker: str,
) -> bool:
    return _relay_control_module().interrupt_if_stop_requested(
        job_id,
        completed_stage,
        expected_attempt=expected_attempt,
        expected_worker=expected_worker,
    )


# ── 状态 ─────────────────────────────────────────────────────────────────────

def load_processed() -> set:
    if PROCESSED_LOG.exists():
        return set(PROCESSED_LOG.read_text(encoding="utf-8").splitlines())
    return set()


def mark_processed(name: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PROCESSED_LOG.open("a", encoding="utf-8") as f:
        f.write(name + "\n")


def load_last_meeting() -> dict | None:
    if LAST_MEETING_FILE.exists():
        try:
            return json.loads(LAST_MEETING_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_last_meeting(audio_name: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_MEETING_FILE.write_text(json.dumps(
        {"audio": audio_name, "dispatched_at": time.time(),
         "dispatched_at_human": time.strftime("%Y-%m-%d %H:%M")},
        ensure_ascii=False), encoding="utf-8")


# ── 工具 ─────────────────────────────────────────────────────────────────────

def wait_stable(path: Path, timeout: int = 900) -> bool:
    """等文件大小连续 STABLE_SECS 秒不变（iCloud/拷贝进行中的半文件兜底）"""
    deadline = time.time() + timeout
    last_size = -1
    stable_since = None
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size and size > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= STABLE_SECS:
                return True
        else:
            stable_since = None
            last_size = size
        time.sleep(1)
    return False


def get_audio_duration_sec(audio: Path) -> float | None:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return None


class MainTranscriptBundleError(RuntimeError):
    """主转写交接包不完整；消息只包含稳定错误码。"""

    def __init__(self, errors: list[str]):
        self.errors = tuple(dict.fromkeys(error for error in errors if error))
        if not self.errors:
            raise ValueError("main transcript bundle errors must not be empty")
        super().__init__(
            "main_transcript_bundle_invalid:" + ",".join(self.errors)
        )


def transcribe(
    audio: Path,
    script: Path | None = None,
    *,
    prompt_file: Path | None = None,
    prompt_sha256: str | None = None,
    heartbeat=None,
    heartbeat_interval: float = RUNTIME_HEARTBEAT_INTERVAL_SEC,
    poll_interval: float = 1.0,
) -> str | None:
    """
    跑转写脚本。输入输出契约：输入音频文件，产物落
    PRODUCTS_DIR/<stem>/<stem>/<stem>.txt（transcribe.sh 会在音频所在目录
    再建一层同名工作目录）。返回 txt 路径，失败返回 None。
    """
    if script is None:
        script = TRANSCRIBE_SH
    if not script.exists():
        log.error("转写脚本不存在: %s", script)
        return None

    products_subdir = PRODUCTS_DIR / audio.stem
    products_subdir.mkdir(parents=True, exist_ok=True)

    work_audio = products_subdir / audio.name
    if not work_audio.exists():
        shutil.copyfile(audio, work_audio)

    # 词典：显式任务快照优先；没有时才沿用可选的全局模板。
    # transcribe.sh 会读取音频同级 prompt.txt。
    prompt_dst = products_subdir / "prompt.txt"
    selected_prompt = prompt_file
    if selected_prompt is None and DEFAULT_PROMPT_FILE.exists() and not prompt_dst.exists():
        selected_prompt = DEFAULT_PROMPT_FILE
    if selected_prompt is not None:
        if selected_prompt.is_symlink() or not selected_prompt.is_file():
            log.error("任务热词快照不存在或不安全")
            return None
        prompt_payload = selected_prompt.read_bytes()
        if (
            prompt_sha256 is not None
            and hashlib.sha256(prompt_payload).hexdigest() != prompt_sha256
        ):
            log.error("任务热词快照哈希不匹配")
            return None
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".prompt-", suffix=".tmp", dir=products_subdir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(prompt_payload)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, prompt_dst)
        finally:
            temporary.unlink(missing_ok=True)

    log.info("转写中（%s）：%s", script.name, audio.name)
    # transcribe.sh 会把 Whisper 对照稿留在后台。若这里使用 PIPE/capture_output，
    # 后台子进程会继续持有管道，communicate() 仍会等待 Whisper，等价于阻塞主链。
    # 文件型日志没有 EOF 等待语义，FunASR 主脚本退出后即可继续派发纪要。
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as process_log:
        command = ["bash", str(script), str(work_audio)]
        if heartbeat is None:
            return_code = subprocess.run(
                command,
                stdout=process_log,
                stderr=subprocess.STDOUT,
                text=True,
            ).returncode
        else:
            process = subprocess.Popen(
                command,
                stdout=process_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            next_heartbeat = 0.0
            while True:
                return_code = process.poll()
                now = time.monotonic()
                if now >= next_heartbeat:
                    heartbeat()
                    next_heartbeat = now + max(0.0, heartbeat_interval)
                if return_code is not None:
                    break
                time.sleep(max(0.0, poll_interval))
        process_log.flush()
        process_log.seek(0)
        process_output = process_log.read()
    txt = products_subdir / audio.stem / f"{audio.stem}.txt"
    if return_code != 0 or not txt.exists():
        log.error("转写失败 rc=%s：%s", return_code, process_output[-500:])
        return None
    bundle_errors = _main_transcript_bundle_errors(txt)
    if bundle_errors:
        log.error("主转写产物校验失败: %s", ",".join(bundle_errors))
        raise MainTranscriptBundleError(bundle_errors)
    return str(txt)


def _main_transcript_bundle_errors(txt_path: str | Path) -> list[str]:
    """校验 FunASR 主链交接包；错误码不含路径或正文。"""
    txt = Path(txt_path).expanduser()
    root = txt.parent
    stem = txt.stem
    canonical_funasr_log = root / "funasr.log"
    funasr_log = (
        canonical_funasr_log
        if canonical_funasr_log.exists() or canonical_funasr_log.is_symlink()
        else root / f"{stem}.funasr.log"
    )
    candidates = {
        "txt": txt,
        "srt": root / f"{stem}.srt",
        "spk": root / f"{stem}.spk.txt",
        "funasr_json": root / f"{stem}.funasr.json",
        "funasr_log": funasr_log,
    }
    errors: list[str] = []
    for name, path in candidates.items():
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"main_transcript_{name}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            errors.append(f"main_transcript_{name}_utf8")
            continue
        if not text.strip():
            errors.append(f"main_transcript_{name}")
            continue
        if name == "funasr_json":
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                errors.append("main_transcript_funasr_json")
                continue
            if not isinstance(loaded, (dict, list)):
                errors.append("main_transcript_funasr_json")
    return list(dict.fromkeys(errors))


# ── 派单 prompt ───────────────────────────────────────────────────────────────

def build_command_prompt(transcript: str) -> str:
    return (
        "# [Relay 短录音 · 即时指令]\n\n"
        "这是用户通过 iPhone 语音备忘录口述的指令，已转写如下。"
        "请直接理解并执行，执行完成后简要汇报结果：\n\n"
        f"> {transcript}\n"
    )


def build_meeting_prompt(
    audio_path: Path,
    txt_path: str,
    duration_sec: float,
    job_id: str | None = None,
    attempt_no: int = 1,
    requested_stage: str | None = None,
    input_transcript_sha256: str | None = None,
    minutes_protocol_version: int = CURRENT_MINUTES_PROTOCOL_VERSION,
) -> str:
    duration_min = duration_sec / 60
    yymmdd = time.strftime("%y%m%d")

    if duration_min <= 15:
        summary_strategy = (
            "- 本场采用 `single_pass`：先完成单轮结构化提取，再写纪要；"
            "即使议题少，也不得跳过信息账本。\n"
        )
    elif duration_min <= 45:
        summary_strategy = (
            "- 本场采用 `topic_hierarchical`：先按语义转场和时间轴切分议题，"
            "分议题结构化提取，再从信息账本合成纪要。\n"
        )
    else:
        summary_strategy = (
            "- 本场采用 `multi_stage`：分议题提取后先局部合并，再多阶段递归合并"
            "为全局信息账本；不得直接从整篇逐字稿一次生成最终纪要。"
            "必须按 `minutes-plan.json` 的每个窗口生成 "
            "`minutes-ledger/<window_id>.json`；无重要事项也必须写明 `empty_reason`。\n"
        )

    protocol_v3_requirement = ""
    if minutes_protocol_version >= 3:
        protocol_v3_requirement = (
            "- Relay 已在草稿目录生成只读来源计划 `minutes-plan.json`；不得修改。"
            "每个 evidence/ledger item 必须携带 `source_end_sec`、"
            "`source_text_sha256`、`source_window_id`，并严格引用计划中的同一 cue。\n"
        )

    # 同场会连续分段检测
    merge_section = ""
    last = load_last_meeting()
    if last and time.time() - last.get("dispatched_at", 0) < SAME_MEETING_GAP_SEC:
        merge_section = (
            f"\n## ⚠️ 疑似同场会议的连续分段\n\n"
            f"上一段会议录音 `{last['audio']}` 在 **{last['dispatched_at_human']}**"
            f"（不到 45 分钟前）刚派发过。中途停录再续录的会议很常见。\n"
            f"归档前必须先 `ls ~/MeetingArchive/ | grep {yymmdd}` "
            f"查看今天已有的文件夹：如果本段与上一段是同一场会（主题相同/内容延续），"
            f"**并入已有文件夹**——本段音频、转写、字幕拷进去（文件名加 -part2 等后缀区分），"
            f"纪要在已有 md 基础上补充合并后重新生成 HTML，**不要另建文件夹**。\n"
        )

    if job_id:
        # 工作台稿件先落隐藏草稿区，完成回执后提升为归档根一级目录；发布在
        # 同一目录原子换入规范产物。重跑绝不覆盖已有正式文件或旧版本。
        merge_section = ""
        archive_location = (
            f"~/MeetingArchive/.workbench-drafts/"
            f"{job_id}/attempt-{attempt_no}/"
        )
        minutes_only = (
            requested_stage == "minutes_generating"
            and input_transcript_sha256 is not None
        )
        if minutes_only:
            artifact_requirement = (
                "- 本 attempt 仅重生成纪要：必须保留既有 `input-transcript.srt`，"
                "并补齐原音频、同名纪要 `.md/.html`、`minutes-plan.json`、"
                "需要时的 `minutes-ledger/`、`minutes-evidence.json` 和 "
                "`workbench-manifest.json`；"
                "不要伪造新的 SRT/FunASR/spk 产物\n"
            )
            manifest_mode_fields = (
                f',"requested_stage":"minutes_generating",'
                f'"input_transcript_sha256":"{input_transcript_sha256}"'
            )
        else:
            artifact_requirement = (
                "- 文件夹里必须齐全：原音频、FunASR 主稿 `.txt/.srt/*.spk.txt/*.json` 与 "
                "`funasr.log`、同名纪要 `.md/.html`、`minutes-plan.json`、"
                "需要时的 `minutes-ledger/`、`minutes-evidence.json` 和 "
                "`workbench-manifest.json`\n"
                "- Whisper 对照稿是独立异步子状态：缺失或生成中不得等待，也不得阻塞 "
                "`complete-minutes`。如果已经完整，可一并保留 `whisper-ref/`；正式发布前"
                "工作台会严格校验 json/tsv/srt/txt/vtt/log 全部齐全\n"
            )
            manifest_mode_fields = ""
        relayctl = Path(__file__).with_name("relayctl").resolve()
        completion_section = (
            f"\n## 工作台任务回执（强制）\n\n"
            f"- `job_id`：`{job_id}`。不得省略、替换或另建任务。\n"
            f"- `workbench-manifest.json` 至少包含 "
            f"`{{\"schema_version\":1,\"job_id\":\"{job_id}\","
            f"\"attempt\":{attempt_no},\"minutes_protocol_version\":"
            f"{minutes_protocol_version}{manifest_mode_fields},"
            f"\"artifacts\":[{{\"path\":\"相对路径\",\"bytes\":123,"
            f"\"sha256\":\"64位十六进制\"}}]}}`；artifacts 必须逐一列出除 manifest "
            f"自身外的全部主链文件，bytes 和 sha256 必须与文件一致；仍在生成的 "
            f"`whisper-ref/` 不属于本次完成回执的前置条件；AppleDouble `._*` 与 "
            f"`.DS_Store` 是噪音，不得写入 artifacts。\n"
            f"- 草稿目录固定为 `{archive_location}`；**禁止覆盖、改名或删除任何正式目录中的旧文件**。\n"
            f"- 所有归档文件写完并自检后，最后执行：\n\n"
            f"  `{relayctl} complete-minutes {job_id} --attempt {attempt_no} "
            f"--archive-dir \"{archive_location}\"`\n\n"
            f"- `complete-minutes` 成功后 Relay 会自动提升为归档根下可见的一级会议"
            f"目录；校对状态只在工作台显示，不再创建「待校对」分层。不要手工搬移"
            f"或重命名 hidden attempt。\n"
            f"- 回调失败时按错误补齐文件并走阶段重试；没有成功回执，不得宣称任务完成。\n"
        )
        cleanup_requirement = (
            "- 不删除或修改 Downloads、产物目录或正式归档中的原音频；发布后由工作台统一清理冗余。\n"
        )
    else:
        archive_location = (
            f"~/MeetingArchive/{yymmdd} <会议主题>/"
        )
        artifact_requirement = (
            "- 文件夹里必须齐 **5 类文件**：原音频 `.m4a`、转写 `.txt`、字幕 `.srt`、"
            "纪要 `.md`、纪要 `.html`——**m4a 必须一并拷入归档文件夹**，别留在产物目录就算完\n"
        )
        completion_section = ""
        cleanup_requirement = (
            "- 归档完成后删除 ~/Downloads 里的同名原始 m4a（产物目录和归档目录各有一份，"
            "Downloads 那份是冗余）\n"
        )

    return (
        f"# [Relay 长录音 · 会议纪要场景]\n\n"
        f"这段录音 **{duration_min:.1f} 分钟**，已经转写完成，请使用 "
        f"`claude-skill-meeting-minutes`（Claude 旧入口名：`meeting-minutes`）"
        f"走完整流程：\n\n"
        f"- 原音频：`{audio_path}`\n"
        f"- 转写文本：`{txt_path}`\n\n"
        f"## 流程要求\n\n"
        f"1. 同时读取纯文本、SRT 和可用的 `*.spk.txt`/FunASR JSON；SRT 是时间真相源，"
        f"说话人标签只用于区分观点，不得虚构真实姓名\n"
        f"2. 先抽取议题和证据信息账本，再写最终纪要；纪要信息量随有效议题、决议、"
        f"行动项、风险和开放问题增长，不按固定字数压缩\n"
        f"{summary_strategy}"
        f"{protocol_v3_requirement}"
        f"3. 输出同一份 Markdown 中的两个阅读层：开头是精炼的「一分钟摘要」，"
        f"随后是按议题展开的「完整会议记录」，并保留背景、决议、待办、风险和下一步\n"
        f"4. 每个决议、行动项、风险、开放问题和关键数字都必须带 `[HH:MM:SS]` "
        f"音频锚；行动项必须显式写 Owner 与截止时间，无法确认时写「待确认」\n"
        f"5. 在归档目录生成 `minutes-evidence.json`：顶层必须包含 "
        f"`schema_version=1`、`minutes_protocol_version={minutes_protocol_version}`、"
        f"`strategy`、`topics[]` 和 `coverage`。每个 topic 包含 "
        f"`topic_id/title/start_sec/end_sec/items[]`；每个 item 包含 "
        f"`item_id/kind/text/source_start_sec/status`。`kind` 仅允许 "
        f"`fact/decision/action/risk/open_question/number`；included 项必须填写出现在"
        f"正文中的 `minutes_anchor`，omitted 项必须填写 `omitted_reason`；action 还必须"
        f"填写 `owner/deadline`。coverage 的 total/included/omitted 必须与 items 实际"
        f"数量一致；协议 v3 的 item 还必须包含 `source_end_sec/source_text_sha256/"
        f"source_window_id`，所有来源必须能在对应窗口 ledger 和 plan 中双重核验\n"
        f"6. pandoc 转 HTML，纪要 md/html 都按会议主题命名（不要用「会议纪要」泛称）\n"
        f"7. 原始产物（转写/纪要）保留在原音频所在的产物目录（留作原始档案）\n"
        f"{merge_section}"
        f"\n## 归档规范（固定，不要自由发挥）\n\n"
        f"归档目录：`{archive_location}`\n\n"
        f"- 日期一律 **6 位 `{yymmdd}`**（不要写成 20{yymmdd} 8 位，不要省成 4 位）\n"
        f"- 会议主题从纪要内容提炼，≤ 14 个字，业务词优先（如「云图AI初审规则沟通」）\n"
        f"{artifact_requirement}"
        f"{cleanup_requirement}"
        f"{completion_section}"
    )


# ── 派单与通知 ─────────────────────────────────────────────────────────────────

def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-S", str(TMUX_SOCKET), *args],
        capture_output=True, text=True,
    )


def resolve_codex_bin() -> str:
    configured = os.getenv("MEETING_RELAY_CODEX_BIN", CODEX_BIN).strip()
    if configured:
        resolved = Path(configured).resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
        raise FileNotFoundError(
            f"MEETING_RELAY_CODEX_BIN 不可执行或不存在: {configured}"
        )

    candidates = (shutil.which("codex"), *CODEX_FALLBACKS)
    for candidate in candidates:
        if not candidate:
            continue
        resolved = Path(candidate).resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    raise FileNotFoundError(
        "找不到可执行的 Codex CLI；请安装 Codex/ChatGPT App，或设置 "
        "MEETING_RELAY_CODEX_BIN"
    )


def resolve_claude_bin() -> str:
    configured = os.getenv("MEETING_RELAY_CLAUDE_BIN", CLAUDE_BIN).strip()
    if configured:
        resolved = Path(configured).resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
        raise FileNotFoundError(
            f"MEETING_RELAY_CLAUDE_BIN 不可执行或不存在: {configured}"
        )

    candidates = (shutil.which("claude"), *CLAUDE_FALLBACKS)
    for candidate in candidates:
        if not candidate:
            continue
        resolved = Path(candidate).resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    raise FileNotFoundError(
        "找不到可执行的 Claude Code CLI；请安装 Claude Code，或设置 "
        "MEETING_RELAY_CLAUDE_BIN"
    )


def build_agent_shell_command(prompt_file: Path) -> str:
    agent = os.getenv("MEETING_RELAY_AGENT", DEFAULT_AGENT).strip().lower()
    quoted_prompt = shlex.quote(str(prompt_file))
    if agent not in {"claude", "codex"}:
        log.warning("未知 MEETING_RELAY_AGENT=%s，按 %s 处理", agent, DEFAULT_AGENT)
        agent = DEFAULT_AGENT
    if agent == "claude":
        claude_bin = shlex.quote(resolve_claude_bin())
        return f"cat {quoted_prompt} | {claude_bin} --print"

    args = [
        resolve_codex_bin(),
        "exec",
        "--cd", str(Path.home() / "workspace" / "meeting-relay"),
        "--add-dir", str(ARCHIVE_ROOT),
        "--add-dir", str(PRODUCTS_DIR),
        "--add-dir", str(INBOX),
        "--add-dir", str(STATE_DIR),
        "--sandbox", "danger-full-access",
        "-c", "approval_policy=\"never\"",
        "-",
    ]
    return f"cat {quoted_prompt} | " + " ".join(shlex.quote(arg) for arg in args)


def dispatch_to_cc1(prompt: str, kind: str):
    """把 prompt 落盘为文件，向 cc1 面板派发。

    cc1 常态是普通 zsh，严禁 send-keys 自然语言——会被 zsh 当命令解析。
    姿势是把「cat 文件管给 Agent CLI」整体作为 shell 命令发过去，
    非交互跑完自动退回 zsh。默认走 Claude Code，可用
    `MEETING_RELAY_AGENT=codex` 回切 Codex。"""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = PROMPTS_DIR / f"relay-{os.urandom(4).hex()}.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    r = _tmux("display-message", "-p", "-t", TMUX_SESSION, "#{pane_current_command}")
    pane_cmd = (r.stdout or "").strip()
    if r.returncode != 0:
        log.error("tmux cc1 会话不存在，派单失败（prompt 保留在 %s）", prompt_file)
        return False

    if pane_cmd not in ("zsh", "bash", "sh"):
        log.warning(
            "tmux cc1 正忙（pane=%s），本次不派单，prompt 保留在 %s",
            pane_cmd,
            prompt_file,
        )
        return False

    sent = _tmux(
        "send-keys",
        "-t",
        TMUX_SESSION,
        build_agent_shell_command(prompt_file),
        "Enter",
    )

    if sent.returncode != 0:
        log.error("tmux 派单失败（prompt 保留在 %s）：%s", prompt_file, sent.stderr)
        return False

    log.info("已派 %s 给 cc1（prompt: %s，pane=%s）", kind, prompt_file.name, pane_cmd)
    return True


def _agent_pane_available() -> bool:
    command_result = _tmux(
        "display-message", "-p", "-t", TMUX_SESSION, "#{pane_current_command}"
    )
    if command_result.returncode != 0:
        return False
    shells = {"zsh", "bash", "sh"}
    if (command_result.stdout or "").strip() not in shells:
        return False

    tty_result = _tmux(
        "display-message", "-p", "-t", TMUX_SESSION, "#{pane_tty}"
    )
    tty = (tty_result.stdout or "").strip()
    if tty_result.returncode != 0 or not tty.startswith("/dev/"):
        return False
    try:
        processes = subprocess.run(
            ["ps", "-t", tty.removeprefix("/dev/"), "-o", "stat=,comm="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if processes.returncode != 0:
        return False

    foreground_found = False
    for line in processes.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or "+" not in parts[0]:
            continue
        foreground_found = True
        executable = Path(parts[1].split(maxsplit=1)[0]).name.lstrip("-")
        if executable not in shells:
            return False
    return foreground_found


def notify_lark(title: str, body: str):
    if not LARK_USER_ID:
        log.debug("未配置 RELAY_LARK_USER_ID，跳过飞书通知")
        return
    msg = f"**{title}**\n\n{body}"
    LARK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "lark-cli", "im", "+messages-send",
        "--as", "bot",
        "--user-id", LARK_USER_ID,
        "--markdown", msg,
    ]
    command = (
        f"PATH={shlex.quote(LARK_NOTIFY_PATH)} "
        + " ".join(shlex.quote(arg) for arg in args)
        + f" >> {shlex.quote(str(LARK_LOG_FILE))} 2>&1"
    )

    # relay_watchdog 经 ssh localhost 启动以继承 TCC 权限；这个环境读不到
    # macOS keychain。把 bot 通知交给 tmux server 的普通 shell 执行，避免
    # 退回 user 身份导致消息变成“自己发给自己”而不提醒。
    r = _tmux("run-shell", "-b", command)
    if r.returncode != 0:
        log.warning("飞书通知调度失败（不影响主流程）：%s", (r.stderr or r.stdout)[:200])
    else:
        log.info("飞书通知已交给 tmux/bot 发送（日志：%s）", LARK_LOG_FILE)


def notify_workbench_status(job_id: str, status: str, duration_min: float | None = None):
    """工作台通知只发状态元数据，不携带标题、正文、文件名或客户信息。"""
    duration_line = (
        f"\n时长：`{duration_min:.1f} 分钟`" if duration_min is not None else ""
    )
    notify_lark(
        "[Relay] 工作台任务状态",
        f"任务：`{job_id}`\n状态：`{status}`{duration_line}",
    )


def notify_relay_status(status: str, duration_min: float | None = None):
    """回滚路径同样遵守隐私边界，不向飞书发送标题、路径或正文。"""
    duration_line = (
        f"\n时长：`{duration_min:.1f} 分钟`" if duration_min is not None else ""
    )
    notify_lark("[Relay] 状态更新", f"状态：`{status}`{duration_line}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def handle_audio(audio: Path):
    """未启用控制层时的原同步路径；保留作一键回滚。"""
    if not wait_stable(audio):
        log.warning("文件不稳定，跳过：%s", audio.name)
        return

    try:
        if audio.stat().st_mtime < STARTUP_EPOCH:
            mark_processed(audio.name)
            return
    except FileNotFoundError:
        return

    duration = get_audio_duration_sec(audio)
    if duration is None:
        log.warning("拿不到时长，按短录音处理：%s", audio.name)
        duration = 0

    duration_min = duration / 60
    is_dual = "-dual-" in audio.name

    if duration >= LONG_AUDIO_THRESHOLD_SEC:
        if is_dual and not DISABLE_DUAL and TRANSCRIBE_DUAL_SH.exists():
            script = TRANSCRIBE_DUAL_SH
            branch = "长录音 · MacBook 双轨"
        else:
            script = TRANSCRIBE_SH
            branch = "长录音 · 单轨 FunASR + Whisper 双跑"
    else:
        script = TRANSCRIBE_SH
        branch = "短录音 · 即时指令"

    log.info("录音 %s（%.1f 分钟，dual=%s）→ %s", audio.name, duration_min, is_dual, branch)

    try:
        txt_path = transcribe(audio, script=script)
    except MainTranscriptBundleError:
        txt_path = None
    if not txt_path:
        notify_relay_status("failed", duration_min)
        mark_processed(audio.name)
        return

    transcript = Path(txt_path).read_text(encoding="utf-8").strip()

    if duration >= LONG_AUDIO_THRESHOLD_SEC:
        products_subdir = PRODUCTS_DIR / audio.stem / audio.stem
        prompt = build_meeting_prompt(products_subdir / audio.name, txt_path, duration)
        if not dispatch_to_cc1(prompt, kind="会议录音"):
            notify_relay_status("failed", duration_min)
            return False
        save_last_meeting(audio.name)
        notify_relay_status("minutes_generating", duration_min)
    else:
        prompt = build_command_prompt(transcript)
        if not dispatch_to_cc1(prompt, kind="即时指令"):
            notify_relay_status("failed", duration_min)
            return False
        notify_relay_status("dispatched", duration_min)

    mark_processed(audio.name)
    log.info("处理完成：%s", audio.name)
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _job_audio_path(
    audio: Path,
    source_archive_dir: str | Path | None = None,
    published_archive_dir: str | Path | None = None,
    expected_sha256: str | None = None,
) -> Path:
    candidates: list[Path] = []
    archive_candidates: list[Path] = []
    for value in (source_archive_dir, published_archive_dir):
        if not value:
            continue
        archive = Path(value).expanduser()
        if archive not in archive_candidates:
            archive_candidates.append(archive)
    for published in archive_candidates:
        candidates.append(published / audio.name)
        if published.is_dir() and not published.is_symlink():
            candidates.extend(
                sorted(
                    path
                    for path in published.iterdir()
                    if path.is_file() and path.suffix.lower() in AUDIO_EXTS
                )
            )
    candidates.extend(
        [
            audio,
            PRODUCTS_DIR / audio.stem / audio.name,
            PRODUCTS_DIR / audio.stem / audio.stem / audio.name,
        ]
    )
    for candidate in candidates:
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if expected_sha256 and _file_sha256(candidate) != expected_sha256:
            continue
        return candidate
    return audio


def _existing_transcript_path(
    audio: Path,
    source_archive_dir: str | Path | None = None,
    published_archive_dir: str | Path | None = None,
    meeting_id: str | None = None,
) -> Path:
    product_transcript = PRODUCTS_DIR / audio.stem / audio.stem / f"{audio.stem}.txt"
    candidates: list[Path] = []
    archive_candidates: list[Path] = []
    for value in (source_archive_dir, published_archive_dir):
        if not value:
            continue
        archive = Path(value).expanduser()
        if archive not in archive_candidates:
            archive_candidates.append(archive)
    for published in archive_candidates:
        if meeting_id:
            candidates.append(published / f"{meeting_id}.txt")
        candidates.append(published / f"{audio.stem}.txt")
        if published.is_dir():
            candidates.extend(
                sorted(
                    path
                    for path in published.glob("*.txt")
                    if not path.name.lower().endswith("spk.txt")
                    and path.name.lower() != "prompt.txt"
                )
            )
    candidates.append(product_transcript)
    return next((candidate for candidate in candidates if candidate.is_file()), product_transcript)


def _claimed_input_transcript(claim: dict) -> Path | None:
    value = claim.get("input_transcript_path")
    expected_hash = claim.get("input_transcript_sha256")
    if not value or not expected_hash:
        return None
    path = Path(str(value)).expanduser()
    if path.is_symlink() or not path.is_file():
        return None
    size = path.stat().st_size
    if size <= 0 or size > MAX_INPUT_TRANSCRIPT_BYTES:
        return None
    if _file_sha256(path) != expected_hash:
        return None
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return path


def _resolve_whisper_bin() -> str:
    candidates = [
        os.getenv("MEETING_RELAY_WHISPER_BIN", "").strip(),
        WHISPER_BIN.strip(),
        *WHISPER_FALLBACKS,
        shutil.which("whisper") or "",
    ]
    for value in candidates:
        if not value:
            continue
        candidate = Path(value).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise FileNotFoundError("找不到可执行的 openai-whisper CLI")


def _validated_whisper_model_dir() -> Path:
    """预先校验固定本地模型，避免 Whisper CLI 在缓存异常时自动外联下载。"""
    global _WHISPER_MODEL_CACHE
    model = WHISPER_MODEL_DIR / WHISPER_MODEL_FILE
    if model.is_symlink() or not model.is_file():
        raise FileNotFoundError("本地 Whisper turbo 模型不存在")
    stat = model.stat()
    cache_key = (stat.st_size, stat.st_mtime_ns)
    if _WHISPER_MODEL_CACHE is None or _WHISPER_MODEL_CACHE[:2] != cache_key:
        digest = _file_sha256(model)
        if digest != WHISPER_MODEL_SHA256:
            raise RuntimeError("本地 Whisper turbo 模型哈希无效")
        _WHISPER_MODEL_CACHE = (*cache_key, digest)
    return WHISPER_MODEL_DIR.resolve()


def process_whisper_retry_claim(claim: dict) -> bool:
    """只运行 Whisper 对照稿，产物由控制层校验并原子安装。"""
    job_id = str(claim["job_id"])
    attempt_no = int(claim["attempt"])
    generation = int(claim["generation"])
    worker_id = str(claim["worker_id"])
    target = Path(str(claim["target_archive_dir"])).expanduser()
    staging: Path | None = None
    try:
        audio = _job_audio_path(
            Path(str(claim["audio_path"])).expanduser(),
            claim.get("source_archive_dir"),
            claim.get("published_archive_dir"),
            str(claim.get("audio_sha256") or "") or None,
        )
        expected_hash = str(claim.get("audio_sha256") or "")
        if (
            not audio.is_file()
            or audio.is_symlink()
            or not expected_hash
            or _file_sha256(audio) != expected_hash
        ):
            raise RuntimeError("source_audio_invalid")
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".whisper-retry-{generation}-",
                dir=target.parent,
            )
        )
        output_dir = staging / "whisper-ref"
        output_dir.mkdir()
        command = [
            _resolve_whisper_bin(),
            str(audio),
            "--model",
            "turbo",
            "--model_dir",
            str(_validated_whisper_model_dir()),
            "--language",
            "zh",
            "--output_format",
            "all",
            "--output_dir",
            str(output_dir),
            "--verbose",
            "False",
        ]
        prompt_path_value = claim.get("hotword_prompt_path")
        prompt_sha256 = claim.get("hotword_prompt_sha256")
        selected_prompt = (
            Path(str(prompt_path_value)).expanduser()
            if prompt_path_value
            else DEFAULT_PROMPT_FILE
        )
        if selected_prompt.is_file() and not selected_prompt.is_symlink():
            prompt_payload = selected_prompt.read_bytes()
            if prompt_path_value and (
                not isinstance(prompt_sha256, str)
                or hashlib.sha256(prompt_payload).hexdigest() != prompt_sha256
            ):
                raise RuntimeError("hotword_prompt_invalid")
            try:
                prompt = prompt_payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("hotword_prompt_invalid") from exc
            if prompt:
                command.extend(["--initial_prompt", prompt])
        with (output_dir / "whisper.log").open("w", encoding="utf-8") as log_file:
            offline_env = os.environ.copy()
            offline_env.update(
                {
                    "HTTP_PROXY": "http://127.0.0.1:9",
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9",
                    "NO_PROXY": "",
                }
            )
            completed = subprocess.run(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=offline_env,
            )
        if completed.returncode != 0:
            _control_finish_whisper_retry(
                job_id,
                attempt_no=attempt_no,
                generation=generation,
                worker_id=worker_id,
                success=False,
                error="whisper_process_failed",
            )
            return False
        _control_finish_whisper_retry(
            job_id,
            attempt_no=attempt_no,
            generation=generation,
            worker_id=worker_id,
            success=True,
            artifact_dir=staging,
        )
        return True
    except Exception as exc:
        log.error("Whisper-only 重试失败：%s", type(exc).__name__)
        try:
            _control_finish_whisper_retry(
                job_id,
                attempt_no=attempt_no,
                generation=generation,
                worker_id=worker_id,
                success=False,
                error="whisper_retry_exception",
            )
        except Exception:
            log.error("Whisper-only 失败状态 CAS 未生效：%s", job_id)
        return False
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def process_controlled_claim(claim: dict) -> bool:
    """执行一个已原子领取的 attempt；所有异常都回写 failed。"""
    job_id = str(claim["job_id"])
    attempt_no = int(claim["attempt"])
    worker_id = str(claim["worker_id"])
    if not worker_id:
        raise ValueError("controlled claim missing worker_id")
    audio = Path(str(claim["audio_path"])).expanduser()
    source_archive_dir = claim.get("source_archive_dir")
    published_archive_dir = claim.get("published_archive_dir")
    meeting_id = claim.get("meeting_id")
    start_stage = str(claim.get("start_stage") or "transcribing")
    duration_min: float | None = None
    current_stage = start_stage

    def run_blocking(operation, *, stage: str):
        return _run_with_heartbeat(
            operation,
            heartbeat=lambda: _best_effort_runtime_heartbeat(
                "control-worker",
                mode="controlled",
                status="busy",
                current_job_id=job_id,
                current_stage=stage,
            ),
        )

    try:
        needs_transcription = start_stage in {"stabilizing", "transcribing"}
        runtime_audio = run_blocking(
            lambda: _job_audio_path(
                audio,
                source_archive_dir,
                published_archive_dir,
                str(claim.get("audio_sha256") or "") or None,
            ),
            stage=current_stage,
        )

        if needs_transcription:
            if not run_blocking(lambda: wait_stable(runtime_audio), stage=current_stage):
                _control_fail(
                    job_id,
                    "stabilizing",
                    "audio did not stabilize",
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                )
                notify_workbench_status(job_id, "failed")
                return False
            source_status = run_blocking(
                lambda: _control_record_source_audio(
                    job_id,
                    runtime_audio,
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                ),
                stage=current_stage,
            )
            if (
                isinstance(source_status, dict)
                and source_status.get("status") == "cancelled"
                and source_status.get("deduplicated_to")
            ):
                notify_workbench_status(job_id, "deduplicated")
                mark_processed(audio.name)
                return True
            if isinstance(source_status, dict) and source_status.get("status") == "failed":
                notify_workbench_status(job_id, "failed")
                mark_processed(audio.name)
                return False
            if start_stage == "stabilizing":
                if _control_interrupt_if_requested(
                    job_id,
                    "stabilizing",
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                ):
                    notify_workbench_status(job_id, "interrupted")
                    return True
                _control_record_stage(
                    job_id,
                    "queued",
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                )
                _control_record_stage(
                    job_id,
                    "transcribing",
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                )
            current_stage = "transcribing"

        duration = run_blocking(
            lambda: get_audio_duration_sec(runtime_audio), stage=current_stage
        )
        if duration is None:
            _control_fail(
                job_id,
                current_stage,
                "audio_duration_unavailable",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            )
            notify_workbench_status(job_id, "failed")
            return False
        duration_min = duration / 60

        if needs_transcription:
            is_dual = "-dual-" in audio.name
            if is_dual and not DISABLE_DUAL and TRANSCRIBE_DUAL_SH.exists():
                script = TRANSCRIBE_DUAL_SH
                branch = "工作台录音 · MacBook 双轨"
            else:
                script = TRANSCRIBE_SH
                branch = "工作台录音 · FunASR + Whisper 双跑"
            log.info("任务 %s（%.1f 分钟）→ %s", job_id, duration_min, branch)
            transcription_error = None
            try:
                txt_path = transcribe(
                    runtime_audio,
                    script=script,
                    prompt_file=(
                        Path(str(claim["hotword_prompt_path"]))
                        if claim.get("hotword_prompt_path")
                        else None
                    ),
                    prompt_sha256=(
                        str(claim["hotword_prompt_sha256"])
                        if claim.get("hotword_prompt_sha256")
                        else None
                    ),
                    heartbeat=lambda: _best_effort_runtime_heartbeat(
                        "control-worker",
                        mode="controlled",
                        status="busy",
                        current_job_id=job_id,
                        current_stage="transcribing",
                    ),
                )
            except MainTranscriptBundleError as exc:
                txt_path = None
                transcription_error = str(exc)
            if not txt_path:
                _control_fail(
                    job_id,
                    "transcribing",
                    transcription_error or f"{script.name} failed",
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                )
                notify_workbench_status(job_id, "failed", duration_min)
                mark_processed(audio.name)
                return False
            products_subdir = PRODUCTS_DIR / audio.stem / audio.stem
            _control_update_whisper_progress(
                job_id,
                products_subdir,
                async_expected=(
                    script == TRANSCRIBE_SH
                    and os.getenv("TRANSCRIBE_ENGINE", "observe") == "observe"
                ),
                attempt_no=int(claim.get("attempt") or 1),
            )
            _control_record_stage(
                job_id,
                "transcript_ready",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            )
            current_stage = "transcript_ready"
            if _control_interrupt_if_requested(
                job_id,
                "transcribing",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            ):
                notify_workbench_status(job_id, "interrupted", duration_min)
                mark_processed(audio.name)
                return True
        else:
            if start_stage == "minutes_generating":
                input_transcript = _claimed_input_transcript(claim)
                txt_path = str(input_transcript) if input_transcript else ""
            else:
                txt_path = str(
                    _existing_transcript_path(
                        audio,
                        source_archive_dir,
                        published_archive_dir,
                        str(meeting_id) if meeting_id else None,
                    )
                )
            if not Path(txt_path).is_file():
                failure = (
                    "input transcript snapshot missing or invalid"
                    if start_stage == "minutes_generating"
                    else "existing transcript missing"
                )
                _control_fail(
                    job_id,
                    start_stage,
                    failure,
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                )
                notify_workbench_status(job_id, "failed", duration_min)
                return False

        if start_stage == "transcript_ready":
            if _control_interrupt_if_requested(
                job_id,
                "transcript_ready",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            ):
                notify_workbench_status(job_id, "interrupted", duration_min)
                return True

        minutes_protocol_version = int(
            claim.get("minutes_protocol_version")
            or 2
        )
        if minutes_protocol_version >= 3:
            source_srt = Path(txt_path)
            if source_srt.suffix.lower() != ".srt":
                source_srt = source_srt.with_suffix(".srt")
            try:
                attempt_dir = _control_prepare_attempt_dir(
                    job_id,
                    attempt_no=attempt_no,
                    expected_worker=worker_id,
                )
                plan_path = attempt_dir / "minutes-plan.json"
                plan = _relay_control_module().create_minutes_plan(
                    source_srt,
                    plan_path,
                    total_duration_sec=duration,
                    input_transcript_sha256=(
                        str(claim.get("input_transcript_sha256"))
                        if claim.get("input_transcript_sha256")
                        else None
                    ),
                )
                _control_record_minutes_plan_source(
                    job_id,
                    attempt_no=attempt_no,
                    source_srt_sha256=str(plan["source_srt_sha256"]),
                    minutes_plan_sha256=hashlib.sha256(
                        plan_path.read_bytes()
                    ).hexdigest(),
                    expected_worker=worker_id,
                )
            except Exception:
                _control_fail(
                    job_id,
                    current_stage,
                    "minutes_plan_invalid",
                    expected_attempt=attempt_no,
                    expected_worker=worker_id,
                )
                notify_workbench_status(job_id, "failed", duration_min)
                return False

        if start_stage != "minutes_generating":
            _control_record_stage(
                job_id,
                "minutes_generating",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            )
        current_stage = "minutes_generating"

        products_subdir = PRODUCTS_DIR / audio.stem / audio.stem
        prompt_audio_path = products_subdir / audio.name
        if not prompt_audio_path.is_file():
            prompt_audio_path = runtime_audio
        prompt = build_meeting_prompt(
            prompt_audio_path,
            txt_path,
            duration,
            job_id=job_id,
            attempt_no=attempt_no,
            requested_stage=start_stage,
            input_transcript_sha256=(
                str(claim.get("input_transcript_sha256"))
                if claim.get("input_transcript_sha256")
                else None
            ),
            minutes_protocol_version=minutes_protocol_version,
        )
        if not dispatch_to_cc1(prompt, kind="会议录音"):
            _control_fail(
                job_id,
                "minutes_generating",
                "Agent dispatch failed",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            )
            notify_workbench_status(job_id, "failed", duration_min)
            mark_processed(audio.name)
            return False

        _control_record_codex_dispatched(
            job_id,
            expected_attempt=attempt_no,
            expected_worker=worker_id,
        )
        save_last_meeting(audio.name)
        notify_workbench_status(job_id, "minutes_generating", duration_min)
        mark_processed(audio.name)
        log.info("工作台任务已派 Claude Code，等待完成回执：%s", job_id)
        return True
    except Exception as exc:
        log.exception("工作台任务执行异常：%s", job_id)
        failed_written = False
        try:
            _control_fail(
                job_id,
                current_stage,
                f"worker exception: {type(exc).__name__}",
                expected_attempt=attempt_no,
                expected_worker=worker_id,
            )
            failed_written = True
        except Exception:
            log.exception("工作台任务失败状态回写异常：%s", job_id)
        if failed_written:
            notify_workbench_status(job_id, "failed", duration_min)
        return False


def run_control_worker_once() -> bool:
    global _next_pending_reconcile_at
    now = time.monotonic()
    if now >= _next_pending_reconcile_at:
        _next_pending_reconcile_at = now + PENDING_RECONCILE_INTERVAL_SEC
        try:
            pending_summary = _control_reconcile_pending_archives()
            if pending_summary.get("errors"):
                log.warning(
                    "待校对对账有 %d 个任务失败，已隔离并继续",
                    len(pending_summary["errors"]),
                )
        except Exception:
            # 对账是修复旁路，不得因单轮异常阻断主 worker 领取任务。
            log.exception("待校对对账异常，本轮继续")
    if not _agent_pane_available():
        return False
    reconciled = _control_reconcile_codex_handoffs()
    if reconciled:
        log.error("%d 个纪要生成 attempt 未回调，已失败收口", reconciled)
    recovered = _control_recover_orphaned_claims()
    if recovered:
        log.warning("已将 %d 个上次中断的 attempt 标为 interrupted", recovered)
    claim = _control_claim_next()
    if claim is None:
        return False
    process_controlled_claim(claim)
    return True


def run_control_worker(stop_event: threading.Event):
    error_backoff = max(CONTROL_POLL_INTERVAL_SEC, 0.1)
    next_heartbeat = 0.0
    try:
        while not stop_event.is_set():
            try:
                now = time.monotonic()
                if now >= next_heartbeat:
                    _control_runtime_heartbeat(
                        "control-worker", mode="controlled", status="idle"
                    )
                    next_heartbeat = now + RUNTIME_HEARTBEAT_INTERVAL_SEC
                worked = run_control_worker_once()
                error_backoff = max(CONTROL_POLL_INTERVAL_SEC, 0.1)
                if not worked:
                    stop_event.wait(CONTROL_POLL_INTERVAL_SEC)
            except Exception as exc:
                log.exception("工作台 control worker 单轮异常，退避后继续")
                try:
                    _control_runtime_heartbeat(
                        "control-worker",
                        mode="controlled",
                        status="degraded",
                        last_error=type(exc).__name__,
                    )
                except Exception as heartbeat_error:
                    log.warning(
                        "control worker 降级心跳写入失败：%s",
                        type(heartbeat_error).__name__,
                    )
                stop_event.wait(error_backoff)
                error_backoff = min(error_backoff * 2, CONTROL_ERROR_BACKOFF_MAX_SEC)
                next_heartbeat = 0.0
    finally:
        _control_runtime_stop("control-worker")


class AudioHandler(FileSystemEventHandler):
    def __init__(self):
        self._processing: set[str] = set()
        self._processed_log = load_processed()

    def _should_process(self, path: Path) -> bool:
        if path.suffix.lower() not in AUDIO_EXTS:
            return False
        if path.name.startswith("."):
            return False
        if "meeting-relay-products" in str(path):
            return False
        if path.name in self._processed_log:
            return False
        if str(path) in self._processing:
            return False
        return True

    def _handle(self, path: Path):
        key = str(path)
        self._processing.add(key)
        try:
            if control_enabled():
                job_id = _control_enqueue(path)
                log.info("工作台任务已入队：%s", job_id)
                handled = True
            else:
                handled = handle_audio(path)
            if handled is not False:
                self._processed_log.add(path.name)
        except Exception:
            log.exception("处理出错：%s", path.name)
        finally:
            self._processing.discard(key)

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if self._should_process(path):
            self._handle(path)

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_process(path):
            return
        try:
            # 只接刚落地的文件，避免历史文件的 metadata 触碰引发重处理
            if time.time() - path.stat().st_mtime > 60:
                return
        except FileNotFoundError:
            return
        self._handle(path)


if __name__ == "__main__":
    if not INBOX.exists():
        log.error("INBOX 目录不存在：%s", INBOX)
        sys.exit(1)

    PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Relay watchdog v2（FunASR + Whisper 双跑 · 归档规范固化 · 同场会合并） 启动")
    configured_agent = os.getenv("MEETING_RELAY_AGENT", DEFAULT_AGENT).strip().lower()
    if configured_agent not in {"claude", "codex"}:
        log.warning(
            "未知 MEETING_RELAY_AGENT=%s，按 %s 处理",
            configured_agent,
            DEFAULT_AGENT,
        )
        configured_agent = DEFAULT_AGENT
    log.info("派工 Agent：%s（MEETING_RELAY_AGENT，codex 为回滚通道）", configured_agent)
    if configured_agent == "claude":
        try:
            log.info("Claude Code CLI：%s [✓]", resolve_claude_bin())
        except FileNotFoundError as exc:
            log.error("%s", exc)
            sys.exit(1)
    else:
        try:
            log.info("Codex CLI：%s [✓]", resolve_codex_bin())
        except FileNotFoundError as exc:
            log.error("%s", exc)
            sys.exit(1)
    log.info("监听目录：%s", INBOX)
    log.info("长录音阈值：%d 秒（%.1f 分钟）", LONG_AUDIO_THRESHOLD_SEC, LONG_AUDIO_THRESHOLD_SEC / 60)
    log.info("转写脚本：%s [%s]", TRANSCRIBE_SH, "✓" if TRANSCRIBE_SH.exists() else "✗")
    log.info("  双轨路径：%s [%s, DISABLE_DUAL=%s]",
             TRANSCRIBE_DUAL_SH, "✓" if TRANSCRIBE_DUAL_SH.exists() else "✗", DISABLE_DUAL)
    log.info("启动时刻：%s", time.strftime("%H:%M:%S", time.localtime(STARTUP_EPOCH)))

    observer = Observer()
    observer.schedule(AudioHandler(), str(INBOX), recursive=False)
    observer.start()

    worker_stop = threading.Event()
    worker = None
    if control_enabled():
        worker = threading.Thread(
            target=run_control_worker,
            args=(worker_stop,),
            name="relay-control-worker",
            daemon=True,
        )
        worker.start()
        log.info("工作台控制层：已启用（轮询 %.1f 秒）", CONTROL_POLL_INTERVAL_SEC)
    else:
        log.info("工作台控制层：关闭（旧同步路径）")

    watchdog_mode = "controlled" if control_enabled() else "legacy"
    next_watchdog_heartbeat = 0.0
    try:
        while True:
            _ensure_runtime_components_alive(observer, worker, mode=watchdog_mode)
            now = time.monotonic()
            if now >= next_watchdog_heartbeat:
                _best_effort_runtime_heartbeat(
                    "watchdog", mode=watchdog_mode, status="running"
                )
                next_watchdog_heartbeat = now + RUNTIME_HEARTBEAT_INTERVAL_SEC
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        worker_stop.set()
        if worker is not None:
            worker.join(timeout=10)
        observer.join()
        _control_runtime_stop("watchdog")
