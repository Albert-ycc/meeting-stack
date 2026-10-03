"""第四期的 AI 调用（4a）：放宽的提到（4b）、决议对比（4c）、问答（4g）共用。

- chat：一次对话请求。timeout 是每一次尝试从发出到读完的截止时间（time.monotonic）：连上以后分块读，每块之前把
  socket 超时设成剩下的时间，每块之后看时钟，过了就关掉连接、记 timeout。只靠 urlopen 的 timeout
  不够：它只管每一次读，DeepSeek 忙时对不流式的请求一直发空行保活，最长约 30 分钟。开头的空白行照读、
  不算回答。
- 错误按代码分：400、422 是 bad_request，401、403 是 auth，402（账户余额不足）是 balance，这几种和
  no_key 从不重试；429 是 rate_limited，5xx 和拿到 200 却没有内容是 server，连不上是 network，超时是
  timeout，等 5 秒重试，最多 retries 次。LLMError.sent 说请求有没有到服务器（连接被拒、DNS 失败时为
  假，调用方用它退回当天用量）。
- 日志只记错误代码、HTTP 状态和用时，不记提示词和回答。
- 必须用 urllib.request.urlopen（按模块属性取），测试护栏（tests/conftest.py 的 _no_real_llm）才拦得到。
- tasks.call_llm 不动，任务抽取和项目归属照旧用它。
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .config import Settings

logger = logging.getLogger(__name__)

CODES = ("bad_request", "auth", "balance", "rate_limited", "server", "network", "timeout", "no_key")
# 从不重试的几种：请求本身不对、key 不对、余额不足、没有 key
NEVER_RETRY = frozenset({"bad_request", "auth", "balance", "no_key"})
RETRY_WAIT_SECONDS = 5.0
READ_BLOCK = 8192
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LLMError(RuntimeError):
    """code 是 CODES 之一；status 是 HTTP 状态（没有时为 None）；sent 为假表示请求没到服务器。"""

    def __init__(self, code: str, *, status: int | None = None, sent: bool = True):
        super().__init__(code)
        self.code = code
        self.status = status
        self.sent = sent


@dataclass(frozen=True)
class ChatReply:
    text: str
    # 原样带回：length 表示被截断，调用方看得出来
    finish_reason: str | None


@dataclass(frozen=True)
class Destination:
    host: str
    # 本机地址（127.0.0.1、localhost、::1）：只用来在问答的回答下面写「用本机模型回答」
    local: bool


def read_key(settings: Settings) -> str:
    """每次调用都读 key 文件；没有、读不了或为空时回空串。记事本、TextEdit 存出来的开头 BOM 去掉。"""
    try:
        text = settings.llm_api_key_file.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text.strip().removeprefix("\ufeff").strip()


def _key_fits_header(key: str) -> bool:
    """key 只能是一段可见 ASCII：多一行、中间有空格、带中文时 http.client 拼头会抛错，错误文字里就是 key。"""
    return all("!" <= char <= "~" for char in key)


def llm_ready(settings: Settings) -> bool:
    """key 文件在、读得出不为空的 key。"""
    key_file = settings.llm_api_key_file.expanduser()
    return key_file.is_file() and bool(read_key(settings))


def destination(settings: Settings) -> Destination:
    host = (urlsplit(settings.llm_api_base).hostname or "").lower()
    return Destination(host=host, local=host in LOCAL_HOSTS)


def neutralise(text: str, limit: int) -> str:
    """放进提示词之前处理一段原文：NFKC、控制字符和格式字符换成空格（换行留着）、< > 换成全角
    ＜ ＞，再截到 limit 个字。原文里的「</transcript>」关不掉提示词里的标签。"""
    normal = unicodedata.normalize("NFKC", text or "")
    cleaned = "".join(
        char if char == "\n" or unicodedata.category(char) not in ("Cc", "Cf") else " "
        for char in normal
    )
    cleaned = cleaned.replace("<", "＜").replace(">", "＞")
    return cleaned[: max(0, int(limit))]


def _status_code(status: int) -> str:
    if status in (400, 422):
        return "bad_request"
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "balance"
    if status == 429:
        return "rate_limited"
    # 5xx，和别的少见状态（地址配错时的 404 之类）：整体退避，不算这场会的次数
    return "server"


def _not_sent(error: BaseException) -> bool:
    """连接被拒、DNS 失败：请求没到服务器。"""
    reason = getattr(error, "reason", error)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _is_timeout(error: BaseException) -> bool:
    reason = getattr(error, "reason", error)
    return isinstance(reason, (TimeoutError, socket.timeout)) or isinstance(
        error, (TimeoutError, socket.timeout)
    )


def _set_socket_timeout(response: Any, seconds: float) -> None:
    """把这次连接的 socket 超时设成剩下的时间（取不到 socket 时就不设，照样按时钟判断）。"""
    try:
        response.fp.raw._sock.settimeout(max(0.05, seconds))
    except (AttributeError, OSError, ValueError):
        pass


def _read_all(response: Any, deadline: float, clock: Callable[[], float]) -> bytes:
    # read1 最多只读一次底层（一个分块、一次收包）就返回，空行保活时也能按时钟停下；
    # read(8192) 会一直等到凑够 8192 字节
    read = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise LLMError("timeout")
        _set_socket_timeout(response, remaining)
        try:
            block = read(READ_BLOCK)
        except (TimeoutError, socket.timeout) as error:
            raise LLMError("timeout") from error
        except (OSError, http.client.HTTPException) as error:
            # 分块响应读到一半断开（IncompleteRead）属于 HTTPException，不是 OSError
            raise LLMError("network") from error
        if not block:
            return b"".join(chunks)
        chunks.append(block)
        if clock() >= deadline:
            raise LLMError("timeout")


def _parse(body: bytes) -> ChatReply:
    text = body.decode("utf-8", "replace").strip()
    if not text:
        raise LLMError("server", status=200)
    try:
        payload = json.loads(text)
        choice = payload["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise LLMError("server", status=200) from error
    if not isinstance(content, str) or not content.strip():
        raise LLMError("server", status=200)
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    return ChatReply(text=content, finish_reason=finish if isinstance(finish, str) else None)


def _once(request: urllib.request.Request, timeout: float, clock: Callable[[], float]) -> ChatReply:
    deadline = clock() + timeout
    header_refused = False
    try:
        response = urllib.request.urlopen(request, timeout=max(0.05, timeout))
    except urllib.error.HTTPError as error:
        status = int(error.code)
        error.close()
        raise LLMError(_status_code(status), status=status) from None
    except (urllib.error.URLError, OSError) as error:
        if _is_timeout(error):
            raise LLMError("timeout") from None
        raise LLMError("network", sent=not _not_sent(error)) from None
    except http.client.HTTPException:
        # 服务器回了不像 HTTP 的东西（BadStatusLine、LineTooLong 等）：请求已经到了
        raise LLMError("network") from None
    except ValueError:
        # http.client 拼头时拒收（头里有换行、非 ASCII）：请求没发出。原异常的文字里是整个
        # Authorization 头，在 except 外面再抛，__context__ 里才不挂着它
        header_refused = True
    if header_refused:
        raise LLMError("auth", sent=False)
    try:
        body = _read_all(response, deadline, clock)
    finally:
        try:
            response.close()
        except Exception:
            pass
    return _parse(body)


def chat(
    settings: Settings,
    *,
    system: str,
    user: str,
    json_mode: bool,
    max_tokens: int,
    timeout: float = 60.0,
    retries: int = 1,
    temperature: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ChatReply:
    """发一次对话请求，返回 ChatReply(text, finish_reason)；失败抛 LLMError。

    timeout 是每一次尝试从发出到读完的截止时间；可重试的错误等 5 秒再试，最多 retries 次。
    json_mode=True 时才带 response_format（有的本机兼容服务器不认这个字段）。
    """
    key = read_key(settings)
    if not key:
        logger.warning("AI 调用没发出：no_key")
        raise LLMError("no_key", sent=False)
    if not _key_fits_header(key):
        logger.warning("AI 调用没发出：auth（key 文件里有换行、空格或非 ASCII 字符）")
        raise LLMError("auth", sent=False)
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        f"{settings.llm_api_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    attempts = max(0, int(retries)) + 1
    # 只要有一次请求到过服务器（比如先超时、重试时连接被拒），这次调用就可能已经计费，不退用量
    any_sent = False
    for attempt in range(attempts):
        started = clock()
        try:
            reply = _once(request, timeout, clock)
        except LLMError as error:
            any_sent = any_sent or error.sent
            logger.warning(
                "AI 调用失败：%s（HTTP %s，用时 %.1f 秒）",
                error.code,
                error.status,
                clock() - started,
            )
            if error.code in NEVER_RETRY or attempt + 1 >= attempts:
                error.sent = any_sent
                raise
            sleep(RETRY_WAIT_SECONDS)
            continue
        logger.info("AI 调用完成：HTTP 200，用时 %.1f 秒", clock() - started)
        return reply
    raise LLMError("server")  # 到不了这里：最后一次失败已经抛出
