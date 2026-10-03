"""浏览器实点用例的公共件。

用例由 run.py 逐个起成子进程，地址和目录从环境变量来。直接 python 跑一个用例会因为缺环境变量退出：
没有 run.py 建的隔离环境，用例就没有可以安全连的实例。
"""

import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright  # noqa: F401  用例从这里取

PRODUCTION_PORT = 8765


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"缺环境变量 {name}：用例要由 workbench/scripts/e2e/run.py 起，它会建隔离环境并设好这些变量"
        )
    return value


BASE = _need("E2E_BASE_URL").rstrip("/")
_parts = urlsplit(BASE)
if _parts.hostname != "127.0.0.1" or _parts.port in (None, PRODUCTION_PORT):
    sys.exit(
        f"拒绝：E2E_BASE_URL 必须是 127.0.0.1 上的隔离实例，且不能是生产的 {PRODUCTION_PORT}，现在是 {BASE}"
    )
DATA_DIR = Path(_need("E2E_DATA_DIR"))
ARCHIVE_ROOT = Path(_need("E2E_ARCHIVE_ROOT"))
TMP = Path(_need("E2E_TMP_DIR"))
SHOTS = Path(_need("E2E_SHOTS_DIR"))
LOG_FILE = Path(_need("E2E_LOG_FILE"))
ENGINE = os.environ.get("E2E_ENGINE", "chromium")
SEED = json.loads(Path(_need("E2E_SEED_FILE")).read_text(encoding="utf-8"))
CASE = Path(sys.argv[0]).stem
# 不算问题的响应：隔离实例的中转仓库是个空目录，每页都有一条 /api/jobs 的 503；纪要依据在逐字稿改过之后
# 按设计回 404 / 409（页面据此提示「依据已过期」）
ENV_NOISE_STATUS = (r"^503 GET \S*/api/jobs\b", r"^(404|409) GET \S*/minutes-evidence\b")

FAILURES: list[str] = []
_last_page: Page | None = None


def mid(suffix: str) -> str:
    """造数的会议编号按今天往前推，写死会过期；按后缀（a1a1a1a1 这种）取"""
    return SEED["meetings"][suffix]["id"]


def check(label: str, ok: object, detail: object = "") -> bool:
    """记一条检查；失败时顺手把最近用的那个页面截一张图（只留前三张）"""
    passed = bool(ok)
    text = str(detail).replace("\n", " ")
    suffix = f"  [{text[:200]}]" if text else ""
    print(f"{'通过' if passed else '失败'}  {label}{suffix}", flush=True)
    if not passed:
        FAILURES.append(label)
        if len(FAILURES) <= 3 and _last_page is not None:
            try:
                SHOTS.mkdir(parents=True, exist_ok=True)
                _last_page.screenshot(path=str(SHOTS / f"{CASE}-fail-{len(FAILURES)}.png"))
            except Exception:  # noqa: BLE001  页面可能已经关了
                pass
    return passed


def poll(fn: Callable[[], object], want: Callable[[object], bool] = bool, timeout: float = 5.0):
    """反复取 fn()，直到 want(值) 成立或超时；返回最后一次取到的值。界面的结果是异步出来的，一次取值会误判"""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if want(value) or time.monotonic() >= deadline:
            return value
        time.sleep(0.1)


class Collector:
    """记一个页面上的 console 错误、未捕获异常、失败请求和 4xx/5xx 响应"""

    def __init__(self, page: Page, name: str):
        self.name = name
        self.console: list[str] = []
        self.errors: list[str] = []
        self.pageerrors: list[str] = []
        self.failed: list[str] = []
        self.aborted: list[str] = []
        self.bad_status: list[str] = []
        self.allow_status: list[str] = list(ENV_NOISE_STATUS)
        self.allow_failed_patterns: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.pageerrors.append(str(e)))
        page.on("requestfailed", self._failed)
        page.on("response", self._response)

    def _console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            self.console.append(f"[{msg.type}] {msg.text}")
        # 资源加载失败那条 console 错误由 bad_status 另算，这里只留页面自己 console.error 的
        if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
            self.errors.append(msg.text)

    def _failed(self, req) -> None:
        reason = req.failure or "?"
        # 页面自己取消的请求（换页时中止还在路上的读、超时放弃等待、播放器换了音频源）不是网络出了问题；
        # 三个引擎的说法不一样：Chromium ERR_ABORTED、WebKit cancelled、Firefox NS_BINDING_ABORTED
        if any(word in reason for word in ("ERR_ABORTED", "cancelled", "NS_BINDING_ABORTED")):
            self.aborted.append(f"{req.method} {req.url}")
            return
        self.failed.append(f"{req.method} {req.url} ({reason})")

    def _response(self, resp) -> None:
        if resp.status >= 400:
            self.bad_status.append(f"{resp.status} {resp.request.method} {resp.url}")

    def allow(self, *patterns: str) -> "Collector":
        """放行某类预期内的失败响应，写成对 '状态 方法 地址' 的正则，例如 r'^409 PUT '"""
        self.allow_status.extend(patterns)
        return self

    def allow_failed(self, *patterns: str) -> "Collector":
        """放行故意掐断的请求（page.route 里 abort 的），写成对 '方法 地址 (原因)' 的正则"""
        self.allow_failed_patterns.extend(patterns)
        return self

    def unexpected_failed(self) -> list[str]:
        return [
            line
            for line in self.failed
            if not any(
                re.search(pattern, line.replace(BASE, "")) for pattern in self.allow_failed_patterns
            )
        ]

    def unexpected_status(self) -> list[str]:
        return [
            line
            for line in self.bad_status
            if not any(re.search(pattern, line.replace(BASE, "")) for pattern in self.allow_status)
        ]

    def expect_clean(self, label: str | None = None) -> None:
        name = label or self.name
        check(f"{name}：没有未捕获的页面异常", not self.pageerrors, self.pageerrors[:3])
        check(f"{name}：页面没有自己 console.error", not self.errors, self.errors[:3])
        check(f"{name}：没有失败的请求", not self.unexpected_failed(), self.unexpected_failed()[:3])
        check(
            f"{name}：没有预料之外的 4xx/5xx",
            not self.unexpected_status(),
            self.unexpected_status()[:3],
        )


def done(*collectors: Collector) -> None:
    for collector in collectors:
        collector.expect_clean()
    print("总结：" + ("全部通过" if not FAILURES else f"{len(FAILURES)} 项失败"), flush=True)
    sys.exit(1 if FAILURES else 0)


def hold(page: Page, pattern: str, only: Callable[[object], bool] = lambda request: True) -> list:
    """把匹配的请求按住不回应（模拟后端卡住），其余照常放行；返回被按住的 route，用完调 release"""
    held: list = []

    def handler(route) -> None:
        if only(route.request):
            held.append(route)
        else:
            route.continue_()

    page.route(pattern, handler)
    return held


def release(held: list) -> None:
    """把按住的请求中止掉。不收的话，页面关闭时 Playwright 会为这些悬着的 route 打一段 CancelledError 的回溯"""
    for route in held:
        try:
            route.abort()
        except Exception:  # noqa: BLE001  页面或连接已经关了
            pass
    held.clear()


def shot(page: Page, name: str, full: bool = False) -> None:
    """只在 run.py 带 --shots 时才留图；失败时的截图由 check 自己拍"""
    if os.environ.get("E2E_SHOTS_ALL") == "1":
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{CASE}-{name}.png"), full_page=full)


def launch(p, **kw):
    """E2E_ENGINE=chromium|webkit|firefox，run.py 的 --browser 设；默认只跑 chromium"""
    return getattr(p, ENGINE).launch(headless=True, **kw)


def new_page(browser, width: int = 1440, height: int = 900, **kw):
    # 时区钉成北京：造数的会议日期、「今天」「昨天」都按北京日历算，不跟着开发机走
    kw.setdefault("timezone_id", "Asia/Shanghai")
    ctx = browser.new_context(viewport={"width": width, "height": height}, **kw)
    ctx.set_default_timeout(10_000)
    page = ctx.new_page()
    return ctx, track(page)


def track(page: Page) -> Page:
    """让 check 失败时截这个页面；new_page 建的会自动记，自己用 context.new_page 建的要调一下"""
    global _last_page
    _last_page = page
    return page


def settle(page: Page, ms: int = 400) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(ms)


def scroll_y(page: Page) -> int:
    return page.evaluate(
        "() => Math.round((document.scrollingElement || document.documentElement).scrollTop)"
    )


def settle_scroll(page: Page, frames: int = 12) -> None:
    """等页面滚动停下来（连续 frames 帧位置不变）：播放时「跟随当前句」会接着把页面滚走，量坐标前要等它停"""
    page.evaluate(
        """async frames => {
          await new Promise(resolve => {
            let x = window.scrollX, y = window.scrollY, stable = 0;
            const check = () => {
              if (window.scrollX === x && window.scrollY === y) stable += 1;
              else { x = window.scrollX; y = window.scrollY; stable = 0; }
              if (stable >= frames) resolve(undefined); else window.requestAnimationFrame(check);
            };
            window.requestAnimationFrame(check);
          });
        }""",
        frames,
    )


def scroll_to(page: Page, y: int) -> None:
    page.evaluate("y => (document.scrollingElement || document.documentElement).scrollTo(0, y)", y)


def hash_of(page: Page) -> str:
    return page.evaluate("() => location.hash")


def nav(page: Page, label: str, wait: int = 500) -> None:
    """点侧栏的入口（待办的按钮名带角标数字，按包含匹配）"""
    page.locator("aside, nav").get_by_role("button", name=label).first.click()
    settle(page, wait)


def active_nav(page: Page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('aside button.rail-link'))
          .filter(b => b.getAttribute('aria-current') || /active|current|selected/.test(b.className))
          .map(b => b.innerText.split('\\n')[0].trim())"""
    )


def heading(page: Page) -> str | None:
    return page.evaluate("() => (document.querySelector('main h1') || {}).innerText || null")


def fetch_json(page: Page, path: str):
    return page.evaluate("async p => (await fetch(p)).json()", path)


def pending_tasks(page: Page) -> int:
    return fetch_json(page, "/api/tasks?status=pending_confirm&limit=1")["total"]


def notices(page: Page) -> list[str]:
    """页面上的提示条和 Toast 文字"""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('[role=status], [role=alert]'))
          .map(e => e.innerText.replace(/\\n/g, ' ')).filter(Boolean)"""
    )


def dialogs(page: Page) -> list[str]:
    """页面里开着的弹窗（role=dialog / alertdialog）的名字，下面的在前"""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('[role=dialog], [role=alertdialog]'))
          .map(d => d.getAttribute('aria-label')
            || (document.getElementById(d.getAttribute('aria-labelledby')) || {}).innerText || '?')"""
    )


def tabs_selected(page: Page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('main [role=tab][aria-selected=true]'))
          .map(e => e.innerText.replace(/\\n/g, ' '))"""
    )


def sidebar_badge(page: Page) -> str | None:
    return page.evaluate(
        "() => document.querySelector('aside .rail-link--badge')?.innerText.replace(/\\n/g, ' ') ?? null"
    )


def badge_number(text: str | None) -> int | None:
    match = re.search(r"(\d+)\s*$", text or "")
    return int(match.group(1)) if match else None


def csrf_headers(page: Page) -> dict[str, str]:
    token = page.request.get(BASE + "/api/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": BASE}


def any_of(values: Iterable[object]) -> bool:
    return any(bool(v) for v in values)
