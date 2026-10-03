#!/usr/bin/env python3
"""飞书卡片回调监听进程（260804 新增）。

职责：以 应用机器人身份维持飞书长连接，接收声档任务确认卡的按钮回调
（card.action.trigger），把确认/驳回动作落到声档，并原地刷新卡片。

运行环境：必须在 GUI 会话的 tmux 里跑（keychain 已解锁），因为 lark-cli
的凭证只在 keychain。由 GUI 会话里的 tmux 会话拉起。用 workbench/.venv 的 Python 起：
.env 靠 python-dotenv 读，它随 pydantic-settings 装在这个 venv 里。

配置（环境变量，或写进 workbench/.env、仓库根的 .env；环境变量优先，两份 .env 都写了的以
workbench/.env 为准，和工作台读 .env 是同一套规则）：
  MEETING_WORKBENCH_LARK_CHAT_ID        任务确认卡所在的群，和声档发卡用的是同一项
  MEETING_WORKBENCH_LARK_OWNER_OPEN_ID  只处理这个人点的按钮
  MEETING_WORKBENCH_LARK_CLI_BIN        飞书 CLI 路径，不设就按 PATH 找 lark-cli
  MEETING_STACK_CARD_EVENTS_DIR         订阅子进程写事件文件的目录，不设用 ~/.meeting-workbench/card-events
前两项缺了就不启动：说明白缺什么，以退出码 78 退出。守护循环可以遇到 78 就停手，免得每 5 秒重启一次刷日志。

数据流：
  lark-cli event +subscribe --output-dir <events>  （子进程，相对路径要求）
    → card.action.trigger_*.json 落到 events 目录
    → 本进程 watch 到新文件 → 解析 action.value → 调声档 API 改任务状态
    → 调声档 API 取该抽取批次当前任务 → build_task_status_card 重建卡
    → lark-cli PATCH 原消息原地刷新 → 文件移到 processed/

日志（都在 ~/.meeting-workbench/logs/，都按大小轮转）：
  card-listener.log   本进程自己的
  card-subscribe.log  订阅子进程（lark-cli）的输出，本进程每分钟看一眼，超过上限就轮转
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterable, MutableMapping
from pathlib import Path

from dotenv import dotenv_values

# 加入声档后端包路径，复用卡片构造器
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "backend"))
from meeting_workbench.notify import build_task_status_card  # noqa: E402

# ------------------------------------------------------------------ 配置
CHAT_ID_ENV = "MEETING_WORKBENCH_LARK_CHAT_ID"
OWNER_OPEN_ID_ENV = "MEETING_WORKBENCH_LARK_OWNER_OPEN_ID"
LARK_CLI_ENV = "MEETING_WORKBENCH_LARK_CLI_BIN"
EVENTS_DIR_ENV = "MEETING_STACK_CARD_EVENTS_DIR"
# .env 的位置和工作台 config.py 的 ENV_FILES 是同一套（有用例钉着）：仓库根一份、workbench/ 下一份
ENV_FILES = (REPO.parent / ".env", REPO / ".env")
# 只从 .env 取监听进程自己用的这几项：.env 是工作台那套，里头还有 webhook、key 文件路径，不该白得一份
ENV_KEYS = (CHAT_ID_ENV, OWNER_OPEN_ID_ENV, LARK_CLI_ENV, EVENTS_DIR_ENV)
NO_CONFIG_EXIT_CODE = 78  # sysexits.h 的 EX_CONFIG


def _load_env_files(
    files: Iterable[Path] = ENV_FILES, environ: MutableMapping[str, str] = os.environ
) -> list[str]:
    """把 .env 里监听进程用的键补进 environ，返回补进去的键名（不含值）。

    已在环境变量里的值优先，.env 不覆盖（值为空串的也算已在环境里，和工作台读 Settings 一致）；
    .env 里的空值等于没写；两份 .env 都写了的项，后一份（workbench/）的优先。
    """
    merged: dict[str, str | None] = {}
    for path in files:
        merged.update(dotenv_values(path))
    applied = []
    for key in ENV_KEYS:
        value = merged.get(key)
        if value and key not in environ:
            environ[key] = value
            applied.append(key)
    return sorted(applied)


# 只在作为脚本起来时读 .env：被 import、被用例加载时不读，本机真实的 .env 带不进用例。
# 要赶在下面按环境变量取值之前。
_DOTENV_APPLIED: list[str] = []
if __name__ == "__main__":
    _DOTENV_APPLIED = _load_env_files()

OWNER_OPEN_ID = os.getenv(OWNER_OPEN_ID_ENV, "").strip()  # 只有这个用户的操作会被处理
CHAT_ID = os.getenv(CHAT_ID_ENV, "").strip()  # 任务跟进群
BASE = "http://127.0.0.1:8765"  # 声档
HOME = Path.home()
EVENTS_DIR = Path(
    os.getenv(EVENTS_DIR_ENV, "").strip() or HOME / ".meeting-workbench" / "card-events"
).expanduser()
PROCESSED_DIR = EVENTS_DIR / "processed"
SEEN_FILE = HOME / ".meeting-workbench" / "card-events-seen.json"
LOG_FILE = HOME / ".meeting-workbench" / "logs" / "card-listener.log"
SUBSCRIBE_LOG = HOME / ".meeting-workbench" / "logs" / "card-subscribe.log"
# 两份日志都按大小轮转：单个文件超过 LOG_MAX_BYTES 就换新的，只留 LOG_BACKUP_COUNT 份旧的（.1 最新）。
# 备份数不能为 0：RotatingFileHandler 的备份数为 0 时永远不轮转。
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LARK_CLI = os.getenv(LARK_CLI_ENV, "").strip() or "lark-cli"
SUPPORTED_ACTIONS = {"confirm", "reject"}
# 匹配订阅子进程的命令行，故意不锁参数顺序：加 --as bot 那次就因为写死了顺序险些漏判。
SUBSCRIBER_PATTERN = r"event \+subscribe.*card\.action\.trigger"
SUBSCRIBER_CHECK_SECONDS = 60


def _log_handler() -> logging.Handler:
    # 新机器上日志目录还不存在，打不开日志文件会让进程一启动就崩
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # delay：写第一条记录时才真正打开文件，被用例加载时不留一个空开着的句柄
    return logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )


logging.basicConfig(
    handlers=[_log_handler()],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("card_listener")


class ShengdangError(Exception):
    """声档 API 返回了错误响应（HTTP 4xx/5xx）。"""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LARK_CLI_NO_PROXY"] = "1"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)


def _cli(method: str, path: str, data: dict | None = None) -> dict:
    args = [LARK_CLI, "api", method, path, "--as", "bot"]
    if data is not None:
        args += ["--data", json.dumps(data, ensure_ascii=False)]
    r = _run(args)
    out = r.stdout
    brace = out.find("{")
    if brace < 0:
        return {"ok": False, "error": f"lark-cli 无 JSON 输出: {out[:200]}"}
    return json.loads(out[brace:])


def _send_text(text: str) -> dict:
    return _cli(
        "POST",
        "/open-apis/im/v1/messages",
        {
            "receive_id": CHAT_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )


def _update_card(message_id: str, card: dict) -> dict:
    return _cli(
        "PATCH",
        f"/open-apis/im/v1/messages/{message_id}",
        {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )


# ------------------------------------------------------------------ 声档 API 客户端（CSRF 双提交）
class ShengdangClient:
    def __init__(self) -> None:
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar)
        )
        self.token: str | None = None

    def _bootstrap(self) -> None:
        with self.opener.open(BASE + "/api/bootstrap", timeout=10) as resp:
            data = json.loads(resp.read())
        self.token = data["csrf_token"]

    def _request(
        self, method: str, path: str, body: dict | None = None, *, retried: bool = False
    ) -> dict:
        if self.token is None:
            self._bootstrap()
        headers = {"X-CSRF-Token": self.token, "Origin": BASE}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            BASE + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read())
            except Exception:
                payload = {"error": f"HTTP {error.code}"}
            # 声档每次启动都会重新随机生成 CSRF token（main.py 的 secrets.token_urlsafe），
            # 且不落盘。本进程常驻数周，只要声档重启过一次，缓存的 token 与 cookie 就永久
            # 失效，之后每次点按钮都被挡在 403，表现为「卡片点了没反应」。
            # 所以遇到 CSRF 类 403 必须重新握手一次再重试，不能把 token 当常量。
            if (
                error.code == 403
                and not retried
                and "CSRF" in str(payload.get("detail", ""))
            ):
                log.warning("CSRF 失效，重新握手后重试：%s %s", method, path)
                self.token = None
                self.cookiejar.clear()
                self._bootstrap()
                return self._request(method, path, body, retried=True)
            # 失败必须以异常形式往外抛，不能把错误体和成功体一起当 dict 返回。
            # 成功返回的是任务对象，它自带业务字段 detail（任务详情正文），而 FastAPI
            # 的报错体同样叫 detail——调用方靠 result.get("detail") 判失败时，凡是有详情
            # 的任务确认成功都会被判成失败，卡片不刷新还倒推一条「确认失败：<任务详情>」。
            raise ShengdangError(
                str(payload.get("detail") or payload.get("error") or f"HTTP {error.code}")
            )

    def list_tasks(self, extraction_id: int) -> list[dict]:
        data = self._request(
            "GET", f"/api/tasks?extraction_id={extraction_id}&limit=200"
        )
        return data.get("items", [])

    def confirm(self, task_id: str) -> dict:
        return self._request("POST", f"/api/tasks/{task_id}/confirm", {})

    def reject(self, task_id: str) -> dict:
        # 空 body 也要发 {}：不带 Content-Type 的写请求会被服务端挡在
        # 「写操作只接受 application/json」上，卡片按钮点了没反应。
        return self._request("POST", f"/api/tasks/{task_id}/reject", {})


# ------------------------------------------------------------------ 事件处理
def _load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def handle_event(client: ShengdangClient, event: dict) -> None:
    operator = event.get("event", {}).get("operator") or {}
    open_id = operator.get("open_id", "")
    # 缺 open_id 时认不出是不是本人，按非本人拒绝，不能放行；没配本人时「缺的 open_id」取到的空串会和
    # 空的本人相等，所以没配也一并拒绝
    if not OWNER_OPEN_ID or open_id != OWNER_OPEN_ID:
        log.warning("忽略非本人操作 open_id=%s", open_id or "<缺失>")
        return
    action = event.get("event", {}).get("action", {}).get("value", {})
    act = action.get("action")
    if act not in SUPPORTED_ACTIONS:
        log.warning("未知动作 %s", act)
        return
    message_id = event.get("event", {}).get("context", {}).get("open_message_id")
    task_id = action.get("task_id")
    extraction_id = action.get("extraction_id")
    if not task_id:
        return
    # 写失败时必须让用户看见。此前失败也照常重绘卡片，卡片长得跟点之前一模一样，
    # 用户只能得出「按钮坏了」，连排查线索都没有。
    try:
        result = client.confirm(task_id) if act == "confirm" else client.reject(task_id)
    except Exception as error:  # noqa: BLE001
        log.error("%s %s 失败：%s", act, task_id, error)
        label = "确认" if act == "confirm" else "驳回"
        _send_text(f"⚠️ 声档任务{label}失败：{error}\n（任务 {task_id}，卡片状态未变更）")
        return
    status = result.get("status") or result
    log.info("%s %s → %s", act, task_id, status)
    extraction_id = result.get("extraction_id") or extraction_id
    # 重建卡片并原地刷新（关键：绝不能重建出空卡把原卡「收起来」）
    if extraction_id and message_id:
        try:
            tasks = client.list_tasks(extraction_id)
            if not tasks:
                # 批次在库里无记录（样例卡/已被重抽覆盖）：不动原卡，保留任务细节供追溯，
                # 绝不刷成固定文案或空卡。
                log.warning("抽取 %s 无任务记录，保留原卡不刷新", extraction_id)
                return
            meeting_title = next(
                (t.get("meeting_title") or "" for t in tasks if t.get("meeting_title")),
                "会议",
            )
            all_done = all(t.get("status") != "pending_confirm" for t in tasks)
            card = build_task_status_card(
                meeting_title=meeting_title,
                tasks=tasks,
                all_done=all_done,
            )
            r = _update_card(message_id, card)
            log.info("卡片刷新 message_id=%s code=%s", message_id, r.get("code"))
        except Exception as error:  # noqa: BLE001
            log.error("卡片刷新失败：%s", error)


def process_file(client: ShengdangClient, path: Path, seen: set[str]) -> bool:
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        log.warning("解析事件文件失败 %s：%s", path, error)
        return False
    event_id = event.get("header", {}).get("event_id")
    if event_id in seen:
        return True
    try:
        handle_event(client, event)
    except Exception as error:  # noqa: BLE001
        log.error("处理事件失败 %s：%s", event_id, error)
    seen.add(event_id)
    _save_seen(seen)
    return True


# ------------------------------------------------------------------ 订阅子进程管理
def _start_subscriber() -> subprocess.Popen:
    # --output-dir 只接受相对路径：cd 到 events 目录再用 "."。
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    SUBSCRIBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LARK_CLI_NO_PROXY"] = "1"
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    # 子进程的输出必须留痕。原先 --quiet + stdout/stderr 全丢 DEVNULL，2026-08-15
    # 那次重启时 lark-cli 把 +subscribe 的身份 auto-detect 成 user 并直接拒绝执行
    # （config.json 里给 app 配了 users），进程一起就退，报错一个字都没留下，长连接
    # 静默消失两天，用户点卡片只看到飞书回的「回调目标服务当前未在线」。
    # with 打开：子进程已继承 fd，父进程这侧必须关掉，否则订阅反复重启会攒一堆 fd。
    with open(SUBSCRIBE_LOG, "a", buffering=1) as log_handle:
        return subprocess.Popen(
            [
                LARK_CLI,
                "event", "+subscribe",
                # 这条命令只认 bot 身份，不显式指定就走 auto-detect，配了 users 的 app
                # 会被判成 user 身份而报 validation 错误退出（退出码 2）。_cli() 里的
                # API 调用一直显式带 --as bot，唯独这里漏了。
                "--as", "bot",
                "--event-types", "card.action.trigger",
                "--output-dir", ".",
            ],
            cwd=str(EVENTS_DIR),
            env=env,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )


def _subscriber_alive() -> bool:
    try:
        return bool(
            subprocess.check_output(
                ["pgrep", "-f", SUBSCRIBER_PATTERN], text=True
            ).strip()
        )
    except subprocess.CalledProcessError:
        return False


def _ensure_subscriber() -> None:
    """没有存活的订阅进程就补一个。

    订阅子进程才是长连接本体，它一死飞书侧立刻变成「回调目标服务当前未在线」，
    而本进程和外层 while true 守护壳都照常活着，没人会去重启它——守护守错了对象。
    所以主循环必须周期性复查，不能只在启动时拉一次。
    """
    if _subscriber_alive():
        return
    proc = _start_subscriber()
    log.warning("订阅进程不在，已拉起 pid=%s（日志 %s）", proc.pid, SUBSCRIBE_LOG)


def _rotate_subscribe_log() -> None:
    """订阅子进程的输出文件超过 LOG_MAX_BYTES 就轮转：旧内容依次挪到 .1 .2 …，只留 LOG_BACKUP_COUNT 份。

    这个文件是 lark-cli 子进程一直以追加方式开着的，它能活过本进程的重启（start_new_session），
    改名没用：它会接着写改名后的那个文件。所以拷一份到 .1，再把原文件截成空，追加方式开着的写入方
    下一次写就落在新的文件尾。拷贝和截断之间写进来的几行会丢，重连日志不值得为这几行去加锁；
    拷贝没成功就不截断，不丢已有的内容。
    """
    try:
        if SUBSCRIBE_LOG.stat().st_size <= LOG_MAX_BYTES:
            return
        for number in range(LOG_BACKUP_COUNT - 1, 0, -1):
            older = SUBSCRIBE_LOG.with_name(f"{SUBSCRIBE_LOG.name}.{number}")
            if older.exists():
                os.replace(older, SUBSCRIBE_LOG.with_name(f"{SUBSCRIBE_LOG.name}.{number + 1}"))
        shutil.copyfile(SUBSCRIBE_LOG, SUBSCRIBE_LOG.with_name(f"{SUBSCRIBE_LOG.name}.1"))
        os.truncate(SUBSCRIBE_LOG, 0)
    except FileNotFoundError:
        return
    except OSError as error:
        log.warning("订阅日志轮转失败，下一轮再试：%s", error)


def _require_config() -> None:
    """本人和目标群缺了就不启动。缺了本人，所有人的点击都被当成别人忽略；缺了群，写失败的提示发不出去。
    带着缺的配置跑起来，用户只看到「卡片点了没反应」，谁也看不出是配置没写。"""
    hints = {
        CHAT_ID_ENV: "任务确认卡所在的群（oc_ 开头，和声档发卡用的是同一项）",
        OWNER_OPEN_ID_ENV: "只处理这个人点的按钮（ou_ 开头的 open_id）",
    }
    values = {CHAT_ID_ENV: CHAT_ID, OWNER_OPEN_ID_ENV: OWNER_OPEN_ID}
    missing = [name for name, value in values.items() if not value]
    if not missing:
        return
    lines = [
        f"card_listener 没有启动：缺少 {'、'.join(missing)}。",
        "写进 workbench/.env（或仓库根的 .env），或在启动命令里设成环境变量：",
        *(f"  {name}  {hints[name]}" for name in missing),
    ]
    log.error("缺少配置 %s，没有启动", "、".join(missing))
    print("\n".join(lines), file=sys.stderr)
    sys.exit(NO_CONFIG_EXIT_CODE)


def main() -> None:
    _require_config()
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    seen = _load_seen()
    client = ShengdangClient()
    if _DOTENV_APPLIED:
        log.info("配置从 .env 取了：%s（只列键名）", "、".join(_DOTENV_APPLIED))
    log.info("card_listener 启动，事件目录 %s", EVENTS_DIR)
    last_check = time.monotonic() - SUBSCRIBER_CHECK_SECONDS  # 首轮立即检查
    while True:
        try:
            # 长连接守护：首轮立即检查，之后每分钟复查一次；订阅日志顺带在这一轮轮转。
            if time.monotonic() - last_check >= SUBSCRIBER_CHECK_SECONDS:
                last_check = time.monotonic()
                _ensure_subscriber()
                _rotate_subscribe_log()
            for path in sorted(EVENTS_DIR.glob("card.action.trigger_*.json")):
                if process_file(client, path, seen):
                    dest = PROCESSED_DIR / path.name
                    try:
                        path.rename(dest)
                    except OSError:
                        pass
            time.sleep(2)
        except Exception as error:  # noqa: BLE001
            log.error("监听循环异常：%s", error)
            time.sleep(5)


if __name__ == "__main__":
    main()
