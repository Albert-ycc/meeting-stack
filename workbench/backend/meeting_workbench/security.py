from __future__ import annotations

import re
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READ_METHODS = {"GET", "HEAD"}
# 浏览器替别的站点发请求时 Sec-Fetch-Site 才是这两个值；same-origin（本站页面）和 none（地址栏、书签）放行
FOREIGN_SITES = {"cross-site", "same-site"}
# iframe、embed、object 加载页面时 Sec-Fetch-Mode 也是 navigate，但页面看不到结果，服务照样会干活
EMBEDDED_DESTINATIONS = {"iframe", "frame", "embed", "object"}
# 预取（speculation rules、<link rel=prerender>）同样是 navigate、dest 是 document，只有 Sec-Purpose 说明没人在看
SPECULATIVE_PURPOSES = ("prefetch", "prerender")
# 请求体里数组、对象最多嵌套几层。前端和卡片监听发的最深的是保存逐字稿（{segments: [{…}]}，3 层）。
# 不挡的话，校验失败回 422 时 FastAPI 会把整个请求体逐层递归编码进响应，嵌到八九百层就撞上 Python 的
# 递归上限，异常出在异常处理里，落成 500 和一段 traceback；任意键都收的 dict 请求体还会原样交给处理函数
MAX_JSON_DEPTH = 64
# JSON 字符串（含转义）：写在字符串里的括号不算嵌套
_JSON_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)
_NOT_BRACKETS = bytes(byte for byte in range(256) if byte not in b"[]{}")


def json_too_deep(body: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
    """请求体里的数组、对象嵌套超过 limit 层。不解析，只数字符串外面的括号；不是合法 JSON 的交给后面的解析去报错。"""
    # 括号总共都不到 limit 个，就不可能嵌套超过 limit 层：几乎所有请求到这里就放行，4MB 的上传块也只多数两遍
    if body.count(b"[") + body.count(b"{") <= limit:
        return False
    depth = 0
    for byte in _JSON_STRING.sub(b"", body).translate(None, _NOT_BRACKETS):
        if byte in b"[{":
            depth += 1
            if depth > limit:
                return True
        else:
            depth -= 1
    return False


class WriteProtectionMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        cookie_name: str,
        token: str,
        max_request_bytes: int,
        allowed_hosts: frozenset[str],
    ):
        super().__init__(app)
        self.allowed_hosts = allowed_hosts
        self.cookie_name = cookie_name
        self.token = token
        self.max_request_bytes = max_request_bytes

    def _allowed_host(self, host: str | None) -> bool:
        # 只认精确的名字：后缀匹配（比如任意 *.ts.net）会让 DNS 重绑定的页面拿到同源身份
        if not host:
            return False
        try:
            hostname = urlsplit(f"//{host}").hostname
        except ValueError:
            return False
        return bool(hostname) and hostname.lower() in self.allowed_hosts

    @staticmethod
    def _foreign_subresource(request: Request) -> bool:
        """别的站点的页面用 <img>、fetch、<iframe> 之类发来的 GET：读不到结果，但 /api/media/N/peaks
        这类接口会真的起 ffmpeg，等于盲打本机资源。整页导航（飞书卡片、书签）、本站页面、
        curl 这类不带 Sec-Fetch-* 的客户端照常放行。"""
        site = request.headers.get("sec-fetch-site", "").strip().lower()
        if site not in FOREIGN_SITES:
            return False
        if request.headers.get("sec-fetch-mode", "").strip().lower() != "navigate":
            return True
        if request.headers.get("sec-fetch-dest", "").strip().lower() in EMBEDDED_DESTINATIONS:
            return True
        purpose = (
            f"{request.headers.get('sec-purpose', '')};{request.headers.get('purpose', '')}".lower()
        )
        return any(word in purpose for word in SPECULATIVE_PURPOSES)

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host")
        if not self._allowed_host(host):
            return self._secure(JSONResponse({"detail": "Host 不在允许列表"}, status_code=400))
        if request.method in READ_METHODS and self._foreign_subresource(request):
            return self._secure(JSONResponse({"detail": "跨站读取已拒绝"}, status_code=403))
        if request.method in WRITE_METHODS and request.url.path.startswith("/api/"):
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) < 0:
                        raise ValueError
                except ValueError:
                    return self._secure(
                        JSONResponse({"detail": "Content-Length 无效"}, status_code=400)
                    )
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return self._secure(
                    JSONResponse({"detail": "写操作只接受 application/json"}, status_code=415)
                )
            origin = request.headers.get("origin")
            parsed_origin = urlsplit(origin) if origin else None
            if (
                not parsed_origin
                or parsed_origin.scheme not in {"http", "https"}
                or parsed_origin.netloc.lower() != host.lower()
                or parsed_origin.scheme != request.url.scheme
            ):
                return self._secure(JSONResponse({"detail": "跨源写入已拒绝"}, status_code=403))
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return self._secure(JSONResponse({"detail": "跨站写入已拒绝"}, status_code=403))
            cookie_token = request.cookies.get(self.cookie_name)
            header_token = request.headers.get("x-csrf-token")
            if not cookie_token or not header_token:
                return self._secure(JSONResponse({"detail": "缺少 CSRF 校验"}, status_code=403))
            if not secrets.compare_digest(cookie_token, self.token) or not secrets.compare_digest(
                header_token, self.token
            ):
                return self._secure(JSONResponse({"detail": "CSRF 校验失败"}, status_code=403))
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > self.max_request_bytes:
                    return self._secure(JSONResponse({"detail": "请求体过大"}, status_code=413))
                body.extend(chunk)
            if json_too_deep(body):
                return self._secure(
                    JSONResponse(
                        {"detail": f"请求体嵌套过深（超过 {MAX_JSON_DEPTH} 层）"}, status_code=400
                    )
                )
            request._body = bytes(body)
        return self._secure(await call_next(request))

    @staticmethod
    def _secure(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            "connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
