"""上传在 App 里跑，切到别的页面再回来（页面实例是新的）照样能取消；取消以后立刻再传一个，按钮不停在「正在取消…」"""

import re

from common import (
    BASE,
    check,
    Collector,
    DATA_DIR,
    done,
    hold,
    launch,
    nav,
    new_page,
    release,
    sync_playwright,
    TMP,
)

MUTATES = True
FILE = TMP / "meeting-e2e.m4a"
FILE.write_bytes(b"\0" * (10 * 1024 * 1024))
SESSIONS = DATA_DIR / "uploads" / ".sessions"


def sessions() -> list[str]:
    return (
        sorted(p.name for p in SESSIONS.iterdir() if p.name.startswith("upload-"))
        if SESSIONS.exists()
        else []
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "换页后取消上传")
    events: list[tuple] = []
    page.on(
        "request",
        lambda r: (
            events.append(("发出", r.method, r.url.replace(BASE, "")))
            if "/api/uploads" in r.url
            else None
        ),
    )
    page.on(
        "requestfinished",
        lambda r: (
            events.append(
                (
                    "完成",
                    r.method,
                    r.url.replace(BASE, ""),
                    r.response().status if r.response() else None,
                )
            )
            if "/api/uploads" in r.url
            else None
        ),
    )
    page.on(
        "requestfailed",
        lambda r: (
            events.append(("失败", r.method, r.url.replace(BASE, ""), r.failure))
            if "/api/uploads" in r.url
            else None
        ),
    )
    held = hold(
        page, "**/api/uploads/*/chunks/*", only=lambda request: request.url.endswith("/chunks/1")
    )
    before = sessions()
    page.goto(BASE + "/#jobs", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.locator("input[type=file]").set_input_files(str(FILE))
    page.wait_for_function("() => /上传中/.test(document.body.innerText)", timeout=15000)
    page.wait_for_timeout(1200)

    nav(page, "录音档案", 800)
    nav(page, "转写录音", 800)
    cancel = page.get_by_role("button", name="取消上传")
    check(
        "换页再回来，还在传、还有［取消上传］",
        cancel.count() == 1
        and page.get_by_role("button", name=re.compile(r"^上传中 \d+%")).count() == 1,
    )
    cancel.click()
    page.get_by_role("button", name="＋ 手工导入录音").wait_for(timeout=10000)
    page.wait_for_timeout(500)
    chunk1_failed = [e for e in events if e[0] == "失败" and e[2].endswith("/chunks/1")]
    check("这一块请求被中止", chunk1_failed and "ERR_ABORTED" in chunk1_failed[0][3], chunk1_failed)
    check(
        "发了 cancel 且 200，没有 complete",
        any(e[0] == "完成" and e[2].endswith("/cancel") and e[3] == 200 for e in events)
        and not any(e[2].endswith("/complete") for e in events),
    )
    # 新页面实例里没有「已取消上传」提示条（提示是原来那个页面实例的），但不能有失败样式的提示
    check("没有失败样式的提示", page.locator(".action-banner--error").count() == 0)
    check("服务端没留下半截会话", sessions() == before, sessions())

    # 取消以后立刻再传一个（这回也按住第 1 块）：取消按钮是新的，不停在上一次的「正在取消…」
    release(held)
    page.locator("input[type=file]").set_input_files(str(FILE))
    page.wait_for_function("() => /上传中/.test(document.body.innerText)", timeout=15000)
    page.wait_for_timeout(1000)
    again = page.get_by_role("button", name="取消上传")
    check(
        "再传一个：按钮是「取消上传」且能点（没卡在「正在取消…」）",
        again.count() == 1 and again.is_enabled(),
    )
    again.click()
    page.get_by_role("button", name="＋ 手工导入录音").wait_for(timeout=10000)
    check("第二次也取消干净", sessions() == before, sessions())
    release(held)
    c.expect_clean("换页后取消上传")
    done()
