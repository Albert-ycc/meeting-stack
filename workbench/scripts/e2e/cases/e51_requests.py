"""请求的超时、取消、非 JSON 错误体：读 30 秒、写 120 秒后给人话；换会议时中止还在路上的旧请求；代理回整页 502 只显示「请求失败（502）」

page.route 把请求按住不回；超时用 Playwright 的假时钟快进，不真等。"""

from common import (
    BASE,
    check,
    Collector,
    done,
    hold,
    launch,
    mid,
    new_page,
    release,
    sync_playwright,
)

MEETING_A = mid("a1a1a1a1")
MEETING_B = mid("a2a2a2a2")
BAD_PAGE = "<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body></html>"

with sync_playwright() as p:
    b = launch(p)

    # 读：会议详情读不回来，转圈，30 秒后给「服务没有响应」
    ctx, page = new_page(b, height=800)
    c = Collector(page, "读超时")
    held = hold(page, f"**/api/meetings/{MEETING_A}")
    page.clock.install()
    page.goto(BASE + f"/#meetings/{MEETING_A}", wait_until="domcontentloaded")
    page.wait_for_selector("text=正在读取本地档案", timeout=10_000)
    page.clock.fast_forward(31_000)
    page.wait_for_selector(".detail-error", timeout=10_000)
    message = page.locator(".detail-error span").inner_text()
    check(
        "读 30 秒没回：提示「服务没有响应，稍后重试」", message == "服务没有响应，稍后重试", message
    )
    release(held)
    ctx.close()

    # 取消：先开 A（按住不回），再改地址栏去 B：A 的请求被中止，B 正常出来，页面没有错误条
    ctx, page = new_page(b, height=800)
    c = Collector(page, "换会议取消旧请求")
    held = hold(page, f"**/api/meetings/{MEETING_A}")
    page.goto(BASE + f"/#meetings/{MEETING_A}", wait_until="domcontentloaded")
    page.wait_for_selector("text=正在读取本地档案", timeout=10_000)
    page.wait_for_timeout(500)
    page.evaluate("id => { location.hash = '#meetings/' + id }", MEETING_B)
    page.wait_for_selector("section.detail-page", timeout=10_000)
    page.wait_for_timeout(1500)
    check(
        "换会议：A 的请求被页面中止（ERR_ABORTED）",
        any(f"/api/meetings/{MEETING_A}" in line for line in c.aborted),
        c.aborted,
    )
    check(
        "换会议：B 正常出来，页面上没有错误条、没有「请求已取消」",
        page.locator(".detail-error").count() == 0
        and "请求已取消" not in page.locator("body").inner_text(),
    )
    c.expect_clean("换会议取消旧请求")
    release(held)
    ctx.close()

    # 写：新建任务的 POST 按住不回，120 秒后弹窗里写「服务没有响应，可能仍在处理」，按钮回到「创建任务」
    ctx, page = new_page(b, height=800)
    c = Collector(page, "写超时")
    held = hold(page, "**/api/tasks", only=lambda request: request.method == "POST")
    page.clock.install()
    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.get_by_role("button", name="＋ 新建任务").click()
    page.locator("[role=dialog] input, [role=dialog] textarea").first.fill("超时试探")
    page.get_by_role("button", name="创建任务").click()
    page.wait_for_selector("text=保存中…", timeout=5_000)
    page.clock.fast_forward(60_000)
    page.wait_for_timeout(500)
    check(
        "写请求 60 秒时：按钮仍是「保存中…」、弹窗里还没有错误",
        page.get_by_role("dialog").get_by_role("button", name="保存中…").count() == 1
        and page.locator("[role=dialog] [role=alert]").count() == 0,
    )
    page.clock.fast_forward(61_000)
    page.wait_for_selector("[role=dialog] [role=alert]", timeout=10_000)
    alert = page.locator("[role=dialog] [role=alert]").first.inner_text()
    check(
        "写请求 120 秒没回：提示「服务没有响应，可能仍在处理，稍后刷新确认」",
        alert == "服务没有响应，可能仍在处理，稍后刷新确认",
        alert,
    )
    check(
        "按钮回到「创建任务」，可以再点",
        page.get_by_role("dialog").locator("button:has-text('创建任务')").count() == 1,
    )
    release(held)
    ctx.close()

    # 代理回了一整页 502 的 HTML：界面上只该看到「请求失败（502）」，正文进 console
    ctx, page = new_page(b, height=800)
    c = Collector(page, "502 的 HTML 页")
    c.allow(r"^502 GET ")
    page.route(
        f"**/api/meetings/{MEETING_A}",
        lambda route: route.fulfill(status=502, content_type="text/html", body=BAD_PAGE),
    )
    page.goto(BASE + f"/#meetings/{MEETING_A}", wait_until="domcontentloaded")
    page.wait_for_selector(".detail-error", timeout=15_000)
    message = page.locator(".detail-error span").inner_text()
    check("502 的整页 HTML：界面只显示「请求失败（502）」", message == "请求失败（502）", message)
    check(
        "正文进了 console 而不是界面",
        any("502" in m and "Bad Gateway" in m for m in c.console),
        c.console[:2],
    )
    c.expect_clean("502 的 HTML 页")
    ctx.close()
    done()
