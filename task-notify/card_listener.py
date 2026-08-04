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
import sys
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
LARK_CLI = "lark-cli"  # 飞书 CLI，凭证走 keychain
SUPPORTED_ACTIONS = {"confirm", "reject"}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("card_listener")


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

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
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
                return json.loads(error.read())
            except Exception:
                return {"error": f"HTTP {error.code}"}

    def list_tasks(self, extraction_id: int) -> list[dict]:
        data = self._request("GET", f"/api/tasks?extraction_id={extraction_id}&limit=200")
        return data.get("items", [])

    def confirm(self, task_id: str) -> dict:
        return self._request("POST", f"/api/tasks/{task_id}/confirm", {})

    def reject(self, task_id: str) -> dict:
        return self._request("POST", f"/api/tasks/{task_id}/reject")


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
    operator = event.get("event", {}).get("operator", {})
    if operator.get("open_id") and operator.get("open_id") != OWNER_OPEN_ID:
        log.warning("忽略非本人操作 open_id=%s", operator.get("open_id"))
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
    try:
        result = client.confirm(task_id) if act == "confirm" else client.reject(task_id)
    except Exception as error:  # noqa: BLE001
        result = {"error": str(error)}
    status = result.get("status") or result.get("error") or result.get("detail") or result
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
    env = dict(os.environ)
    env["LARK_CLI_NO_PROXY"] = "1"
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    return subprocess.Popen(
        [
            LARK_CLI,
            "event", "+subscribe",
            "--event-types", "card.action.trigger",
            "--output-dir", ".",
            "--quiet",
        ],
        cwd=str(EVENTS_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _ensure_subscriber() -> subprocess.Popen | None:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "event \\+subscribe --event-types card.action.trigger"],
            text=True,
        ).strip()
        if out:
            log.info("已有订阅进程 pid=%s", out)
            return None
    except subprocess.CalledProcessError:
        pass
    proc = _start_subscriber()
    log.info("已拉起订阅进程 pid=%s", proc.pid)
    return proc


def main() -> None:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    seen = _load_seen()
    client = WorkbenchClient()
    try:
        _ensure_subscriber()
    except Exception as error:  # noqa: BLE001
        log.error("拉起订阅失败：%s", error)
    log.info("card_listener 启动，事件目录 %s", EVENTS_DIR)
    while True:
        try:
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
