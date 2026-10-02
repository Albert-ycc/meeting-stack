"""飞书任务确认卡片回调监听（参考实现）。

职责：以应用机器人身份维持飞书长连接，接收任务确认卡片的按钮回调
（card.action.trigger），把确认/驳回动作落到工作台，并原地刷新卡片。

关键点：
- 长连接（WebSocket）接收回调：本地进程主动连出，无公网端口暴露，适合本地化部署。
- 卡片回调是应用级事件：纯自定义机器人收不到，必须由应用机器人发卡。
- 回调里 operator.open_id 校验操作者身份；action.value 携带 task_id/extraction_id。
- 确认/驳回后按抽取批次重建卡片：已确认条目保留细节去按钮，待确认条目保留按钮。
- 批次在库里无记录（旧卡/样例）时保留原卡不塌缩。

运行环境：macOS 上建议在 GUI 会话的 tmux 里跑（lark-cli 凭证走 keychain，
非交互会话 keychain 锁定拿不到凭证）。
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from cards import build_status_card  # 与本目录 cards.py 同放

# ------------------------------------------------------------------ 配置（部署时按需修改）
OWNER_OPEN_ID = "ou_<你的 open_id>"  # 只有这个用户的操作会被处理
CHAT_ID = "oc_<目标群 chat_id>"
WORKBENCH_BASE = "http://127.0.0.1:8765"  # 工作台地址
EVENTS_DIR = Path.home() / ".meeting-stack" / "card-events"
PROCESSED_DIR = EVENTS_DIR / "processed"
SEEN_FILE = Path.home() / ".meeting-stack" / "card-events-seen.json"
LOG_FILE = Path.home() / ".meeting-stack" / "logs" / "card-listener.log"
SUBSCRIBE_LOG = LOG_FILE.parent / "card-subscribe.log"
LARK_CLI = "lark-cli"  # 飞书 CLI，凭证走 keychain
SUPPORTED_ACTIONS = {"confirm", "reject"}
# 匹配订阅子进程的命令行，不锁参数顺序（加了 --as bot，写死顺序会漏判）
SUBSCRIBER_PATTERN = r"event \+subscribe.*card\.action\.trigger"
SUBSCRIBER_CHECK_SECONDS = 60

# 新机器上日志目录还不存在，basicConfig 打不开日志文件会让进程一启动就崩
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("card_listener")


class WorkbenchError(Exception):
    """工作台 API 返回了错误响应（HTTP 4xx/5xx）。"""


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


def _send_card(card: dict) -> dict:
    return _cli(
        "POST",
        "/open-apis/im/v1/messages",
        {
            "receive_id": CHAT_ID,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )


def _send_text(text: str) -> dict:
    """发一条纯文本消息，用于把写失败显性化（不让用户以为按钮坏了）。"""
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
    """原地更新一张已发的交互卡片。"""
    return _cli(
        "PATCH",
        f"/open-apis/im/v1/messages/{message_id}",
        {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )


# ------------------------------------------------------------------ 工作台 API 客户端（CSRF 双提交）
class WorkbenchClient:
    def __init__(self) -> None:
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar)
        )
        self.token: str | None = None

    def _bootstrap(self) -> None:
        with self.opener.open(WORKBENCH_BASE + "/api/bootstrap", timeout=10) as resp:
            data = json.loads(resp.read())
        self.token = data["csrf_token"]

    def _request(
        self, method: str, path: str, body: dict | None = None, *, retried: bool = False
    ) -> dict:
        if self.token is None:
            self._bootstrap()
        headers = {"X-CSRF-Token": self.token, "Origin": WORKBENCH_BASE}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            WORKBENCH_BASE + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read())
            except Exception:
                payload = {"error": f"HTTP {error.code}"}
            # 工作台每次启动重新随机生成 CSRF token 且不落盘。本进程常驻，
            # 只要工作台重启过一次，缓存的 token 就永久失效，之后每次点按钮
            # 都被挡在 403，表现为「卡片点了没反应」。遇到 CSRF 类 403 必须
            # 重新握手一次再重试，不能把 token 当常量。
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
            # 失败必须抛异常，不能和成功体一样当 dict 返回：成功返回的任务对象自带业务字段
            # detail（任务详情），和 FastAPI 报错体的 detail 同名，靠它判失败会把确认成功判成失败。
            raise WorkbenchError(
                str(payload.get("detail") or payload.get("error") or f"HTTP {error.code}")
            )

    def list_tasks(self, extraction_id: int) -> list[dict]:
        data = self._request("GET", f"/api/tasks?extraction_id={extraction_id}&limit=200")
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


def handle_event(client: WorkbenchClient, event: dict) -> None:
    operator = event.get("event", {}).get("operator") or {}
    open_id = operator.get("open_id", "")
    # 缺 open_id 时认不出是不是本人，按非本人拒绝，不能放行
    if open_id != OWNER_OPEN_ID:
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
        _send_text(f"⚠️ 任务{label}失败：{error}\n（任务 {task_id}，卡片状态未变更）")
        return
    status = result.get("status") or result
    log.info("%s %s → %s", act, task_id, status)
    extraction_id = result.get("extraction_id") or extraction_id
    # 重建卡片并原地刷新（关键：绝不能重建出空卡把原卡「收起来」）
    if extraction_id and message_id:
        try:
            tasks = client.list_tasks(extraction_id)
            if not tasks:
                # 批次在库里无记录（旧卡/已被重抽覆盖）：保留原卡细节供追溯
                log.warning("抽取 %s 无任务记录，保留原卡不刷新", extraction_id)
                return
            meeting_title = next(
                (t.get("meeting_title") or "" for t in tasks if t.get("meeting_title")),
                "会议",
            )
            all_done = all(t.get("status") != "pending_confirm" for t in tasks)
            card = build_status_card(meeting_title=meeting_title, tasks=tasks, all_done=all_done)
            r = _update_card(message_id, card)
            log.info("卡片刷新 message_id=%s code=%s", message_id, r.get("code"))
        except Exception as error:  # noqa: BLE001
            log.error("卡片刷新失败：%s", error)


def process_file(client: WorkbenchClient, path: Path, seen: set[str]) -> bool:
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
    # --output-dir 只接受相对路径：cd 到 events 目录再用 "."
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    SUBSCRIBE_LOG.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LARK_CLI_NO_PROXY"] = "1"
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    # 子进程输出必须留痕：丢进 DEVNULL 时订阅一起就退，报错一个字都留不下。
    # with 打开：子进程已继承 fd，父进程这侧要关掉，否则反复重启会攒 fd。
    with open(SUBSCRIBE_LOG, "a", buffering=1) as log_handle:
        return subprocess.Popen(
            [
                LARK_CLI,
                "event", "+subscribe",
                # 这条命令只认 bot 身份；不显式指定就走 auto-detect，配了 users 的 app
                # 会被判成 user 身份，以退出码 2 立即退出。
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

    订阅子进程才是长连接本体，它一死飞书侧就是「回调目标服务当前未在线」，
    本进程却照常活着。所以主循环要周期性复查，不能只在启动时拉一次。
    """
    if _subscriber_alive():
        return
    proc = _start_subscriber()
    log.warning("订阅进程不在，已拉起 pid=%s（日志 %s）", proc.pid, SUBSCRIBE_LOG)


def main() -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    seen = _load_seen()
    client = WorkbenchClient()
    log.info("card_listener 启动，事件目录 %s", EVENTS_DIR)
    last_check = time.monotonic() - SUBSCRIBER_CHECK_SECONDS  # 首轮立即检查
    while True:
        try:
            if time.monotonic() - last_check >= SUBSCRIBER_CHECK_SECONDS:
                last_check = time.monotonic()
                _ensure_subscriber()
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
