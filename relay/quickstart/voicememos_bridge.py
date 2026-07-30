"""
Voice Memos → relay 流水线桥接（零成本抗中断录音入口）。

这是可选组件，只在 macOS + iPhone 上有意义。不用它的话，把音频用任何方式放进
监听目录即可，relay 一样处理。

iPhone 端：设置 → 动作按钮 → 语音备忘录。长按 Action Button 锁屏即录，
系统原生应用抗中断（来电自动暂停/恢复），后台录音不锁界面。
录音经 iCloud 同步落地 Mac 端 Voice Memos Group Container，本脚本轮询发现
新录音后复制到监听目录（统一 .m4a 后缀），交由 relay_watchdog 流水线
（转写 / 时长分流 / 纪要生成 / 归档 / 通知）接管。

⚠️ 必须经 `ssh localhost` 进程链启动，不能用 launchd 或 tmux 直起——
macOS TCC 会拦住 launchd 进程链（包括它自启的 tmux server）读取 Voice Memos 的
Group Container。sshd 通常在「完全磁盘访问」名单内，经 ssh localhost 启动的
进程链可继承该授权，实测放行。详见 docs/install.md 的权限章节。
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import logging
from pathlib import Path

RECORDINGS_DIR = Path(os.getenv(
    "VM_RECORDINGS_DIR",
    str(Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"),
))
OUTBOX     = Path(os.getenv("VM_OUTBOX", str(Path.home() / "Downloads")))
STATE_FILE = Path(os.getenv("VM_STATE_FILE", str(Path.home() / ".local/state/meeting-relay/vmbridge_seen.json")))
LOG_FILE   = Path.home() / "Library/Logs/meeting-relay-vmbridge.log"

POLL_SEC = int(os.getenv("VM_POLL_SEC", "15"))
# Voice Memos app 不在运行时 iCloud 可能不拉增量，定期隐藏唤醒保证同步活性；0 = 关闭
POKE_SEC = int(os.getenv("VM_POKE_SEC", "600"))

AUDIO_EXTS = {".m4a", ".qta"}  # .qta 是 Voice Memos 新容器，本质 MPEG-4，搬运时统一改 .m4a

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("vmbridge")

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def load_seen() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("state 文件损坏，重建基线：%s", e)
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8")


def list_recordings() -> dict:
    """返回 {文件名: (size, mtime)}，只看音频扩展名"""
    result = {}
    try:
        for p in RECORDINGS_DIR.iterdir():
            if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("."):
                try:
                    st = p.stat()
                    result[p.name] = (st.st_size, st.st_mtime)
                except FileNotFoundError:
                    continue
    except PermissionError:
        log.error("TCC 拒绝读取 %s —— 本脚本必须经 ssh localhost 链启动（见文件头注释）", RECORDINGS_DIR)
        raise
    return result


def poke_voice_memos():
    """Voice Memos 不在运行则隐藏唤醒，让它保持 iCloud 增量同步"""
    r = subprocess.run(["pgrep", "-x", "VoiceMemos"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["open", "-g", "-j", "-b", "com.apple.VoiceMemos"], capture_output=True)
        log.info("已隐藏唤醒 Voice Memos（保持 iCloud 同步活性）")


def transfer(name: str):
    src = RECORDINGS_DIR / name
    # 「20260528 203606-C168419A.qta」→「vm-20260528-203606-C168419A.m4a」
    stem = src.stem.replace(" ", "-")
    dst = OUTBOX / f"vm-{stem}.m4a"
    # 直接写最终文件名（对齐 server/audio.py）：watchdog 只接 on_created/on_modified，
    # 不接 on_moved，所以不能用 .part→rename 原子落位（rename 是 moved 事件会被漏掉）。
    # 半文件由 watchdog 的 wait_stable 兜底——和生产 HTTP 路径同款机制。
    shutil.copyfile(src, dst)
    log.info("已搬运：%s → %s（%.1f MB）", name, dst.name, src.stat().st_size / 1e6)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if not RECORDINGS_DIR.exists():
        log.error("Recordings 目录不存在：%s（Mac 端 Voice Memos iCloud 同步未开？）", RECORDINGS_DIR)
        sys.exit(1)

    seen = load_seen()
    first_run = not STATE_FILE.exists()
    snapshot = list_recordings()

    if first_run:
        # 首次启动：现存录音全部记为已处理，不回灌历史
        seen = set(snapshot)
        save_seen(seen)
        log.info("首次启动，基线 %d 个历史录音已跳过", len(seen))

    log.info("vmbridge 启动：监听 %s → %s（轮询 %ds，唤醒间隔 %ds）",
             RECORDINGS_DIR, OUTBOX, POLL_SEC, POKE_SEC)

    pending = {}  # 候选新文件 {name: (size, mtime)}，连续两轮一致才搬运
    last_poke = 0.0

    while _running:
        if POKE_SEC and time.time() - last_poke >= POKE_SEC:
            poke_voice_memos()
            last_poke = time.time()

        snapshot = list_recordings()
        for name, meta in snapshot.items():
            if name in seen:
                continue
            if pending.get(name) == meta and meta[0] > 0:
                try:
                    transfer(name)
                except Exception as e:
                    log.error("搬运失败（下轮重试）：%s — %s", name, e)
                    continue
                seen.add(name)
                save_seen(seen)
                pending.pop(name, None)
            else:
                if name not in pending:
                    log.info("发现新录音，等待稳定：%s", name)
                pending[name] = meta

        # 清理已从源目录消失的候选
        pending = {k: v for k, v in pending.items() if k in snapshot}

        time.sleep(POLL_SEC)

    log.info("vmbridge 退出")


if __name__ == "__main__":
    main()
