"""快速连点、操作进行中切页：确认和丢掉只发一次请求；保存逐字稿后立刻切页，保存照样完成；连换检索词，最后显示的是最后一个"""

from common import (
    BASE,
    check,
    Collector,
    done,
    fetch_json,
    hash_of,
    heading,
    launch,
    mid,
    nav,
    new_page,
    sync_playwright,
)

MUTATES = True
MEETING = mid("b1b1b1b1")

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "快速操作")
    page.on("dialog", lambda d: d.accept())
    writes: list[str] = []
    page.on(
        "request",
        lambda r: (
            writes.append(f"{r.method} {r.url.replace(BASE, '')}") if r.method != "GET" else None
        ),
    )

    # 1. 待办「确认」连点
    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(400)
    page.locator("main [role=tab]", has_text="待确认").first.click()
    page.wait_for_timeout(600)
    button = page.locator("main button.review-button").first
    button.click()
    try:
        button.click(force=True, no_wait_after=True, timeout=2000)
    except Exception:  # noqa: BLE001  第一下之后按钮可能已经没了，没了也说明没发第二次
        pass
    page.wait_for_timeout(1200)
    confirms = [w for w in writes if w.endswith("/confirm")]
    check("待办［确认］连点两下：只发了一次确认请求", len(confirms) == 1, confirms)
    writes.clear()

    # 2. 需求池「丢掉」双击
    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(400)
    page.locator("main [role=tab]", has_text="待认领").first.click()
    page.wait_for_timeout(600)
    before = fetch_json(page, "/api/requirement-pool?status=pending&limit=1")["total"]
    page.get_by_role("button", name="丢掉").first.dblclick()
    page.wait_for_timeout(1200)
    drops = [w for w in writes if w.endswith("/drop")]
    after = fetch_json(page, "/api/requirement-pool?status=pending&limit=1")["total"]
    check(
        "需求池［丢掉］双击：只发了一次 drop，候选只少 1",
        len(drops) == 1 and after == before - 1,
        (drops, before, after),
    )
    writes.clear()

    # 3. 保存逐字稿后立刻切页
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_timeout(800)
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(300)
    page.locator(".transcript-scroll textarea").nth(1).fill("保存中就切页")
    page.get_by_role("button", name="保存草稿").click()
    nav(page, "词典管理", 1500)
    saved = page.evaluate(
        "async m => (await (await fetch('/api/meetings/' + m)).json()).segments[1].text", MEETING
    )
    check(
        "保存后立刻点侧栏切页：切过去了、保存的请求发出并完成、内容落了库",
        hash_of(page) == "#glossary"
        and any(w.startswith("PUT ") and "/transcript" in w for w in writes)
        and saved == "保存中就切页",
        (hash_of(page), writes, saved),
    )

    # 4. 检索连换四个词：最后显示的是最后一个词的结果
    page.goto(BASE + "/#library", wait_until="networkidle")
    for word in ("接口清单", "导出", "司美格鲁肽", "冷链"):
        page.get_by_label("全局检索").fill(word)
        page.get_by_label("全局检索").press("Enter")
    page.wait_for_timeout(1500)
    title = heading(page) or ""
    check("连换检索词：结果页的标题是最后一个词「冷链」", "冷链" in title, title)
    done(c)
