"""侧栏切视图：新页面从顶上开始，不带着上一个视图的滚动位置"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    nav,
    new_page,
    scroll_to,
    scroll_y,
    sync_playwright,
)

VIEWS = ["工作台", "录音档案", "需求池", "待办", "词典", "项目管理"]

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=600)
    c = Collector(page, "切视图滚动")
    page.goto(BASE + "/#library", wait_until="networkidle")
    carried = []
    scrolled_views = set()
    for a in VIEWS:
        for z in VIEWS:
            if a == z:
                continue
            nav(page, a)
            scroll_to(page, 99999)
            page.wait_for_timeout(250)
            ya = scroll_y(page)
            nav(page, z, 600)
            yz = scroll_y(page)
            if ya > 50:
                scrolled_views.add(a)
            if yz > 5:
                carried.append(f"{a}(滚到 {ya}) → {z}: {yz}")
    check(
        "有几个视图确实能滚（不然这条检查没意义）", len(scrolled_views) >= 4, sorted(scrolled_views)
    )
    check("切到任何视图都从顶上开始", not carried, carried[:6])
    done(c)
