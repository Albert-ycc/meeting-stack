#!/usr/bin/env python3
"""飞书卡片回调监听进程（260804 新增）。

职责：以 应用机器人身份维持飞书长连接，接收声档任务确认卡的按钮回调
（card.action.trigger），把确认/驳回动作落到声档，并原地刷新卡片。

运行环境：必须在 GUI 会话的 tmux 里跑（keychain 已解锁），因为 lark-cli
的凭证只在 keychain。由 GUI 会话里的 tmux 会话拉起。

数据流：
  lark-cli event +subscribe --output-dir <events>  （子进程，相对路径要求）
    → card.action.trigger_*.json 落到 events 目录
    → 本进程 watch 到新文件 → 解析 action.value → 调声档 API 改任务状态
    → 调声档 API 取该抽取批次当前任务 → build_task_status_card 重建卡
    → lark-cli PATCH 原消息原地刷新 → 文件移到 processed/
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# 加入声档后端包路径，复用卡片构造器
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "backend"))
from meeting_workbench.notify import build_task_status_card  # noqa: E402

# ------------------------------------------------------------------ 配置
OWNER_OPEN_ID = "ou_<你的 open_id>"  # 只有这个用户的操作会被处理
CHAT_ID = "oc_<目标群 chat_id>"  # 任务跟进群
BASE = "http://127.0.0.1:8765"  # 声档
HOME = Path.home()
EVENTS_DIR = HOME / ".meeting-workbench" / "card-events"
PROCESSED_DIR = EVENTS_DIR / "processed"
SEEN_FILE = HOME / ".meeting-workbench" / "card-events-seen.json"
LOG_FILE = HOME / ".meeting-workbench" / "logs" / "card-listener.log"
SUBSCRIBE_LOG = HOME / ".meeting-workbench" / "logs" / "card-subscribe.log"
LARK_CLI = "/usr/local/bin/lark-cli"
SUPPORTED_ACTIONS = {"confirm", "reject"}
# 匹配订阅子进程的命令行，故意不锁参数顺序：加 --as bot 那次就因为写死了顺序险些漏判。
SUBSCRIBER_PATTERN = r"event \+subscribe.*card\.action\.trigger"
SUBSCRIBER_CHECK_SECONDS = 60

logging.basicConfig(
    filename=LOG_FILE,
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

    def batch_confirm(self, task_ids: list[str]) -> dict:
        return self._request("POST", "/api/tasks/batch-confirm", {"task_ids": task_ids})


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
    operator = event.get("event", {}).get("operator", {})
    open_id = operator.get("open_id", "")
    if open_id and open_id != OWNER_OPEN_ID:
        log.warning("忽略非本人操作 open_id=%s", open_id)
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


def main() -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    seen = _load_seen()
    client = ShengdangClient()
    log.info("card_listener 启动，事件目录 %s", EVENTS_DIR)
    last_check = time.monotonic() - SUBSCRIBER_CHECK_SECONDS  # 首轮立即检查
    while True:
        try:
            # 长连接守护：首轮立即检查，之后每分钟复查一次。
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
