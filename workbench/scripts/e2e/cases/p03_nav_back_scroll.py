"""侧栏 A（滚到 400）→ B：B 从顶上开始；浏览器后退回 A 回到 400，前进回 B 又在顶上"""

from common import (
    BASE,
    check,
    done,
    launch,
    new_page,
    scroll_to,
    scroll_y,
    settle,
    sync_playwright,
)

NAV = [
    ("工作台", ""),
    ("录音档案", "#library"),
    ("需求池", "#requirements"),
    ("待办", "#tasks"),
    ("项目管理", "#projects"),
]

with sync_playwright() as p:
    b = launch(p)
    for a, a_hash in NAV:
        for t, _ in NAV:
            if a == t:
                continue
            ctx, page = new_page(b, height=600)
            page.goto(BASE + "/" + a_hash, wait_until="networkidle")
            page.wait_for_timeout(600)
            scroll_to(page, 400)
            page.wait_for_timeout(300)
            ya = scroll_y(page)
            page.locator("nav").get_by_role("button", name=t).first.click()
            settle(page, 700)
            yb = scroll_y(page)
            page.go_back()
            settle(page, 700)
            yback = scroll_y(page)
            page.go_forward()
            settle(page, 700)
            yfwd = scroll_y(page)
            check(
                f"{a}({ya}) → {t}：{t} 在顶上，后退回 {a} 是 {ya}，前进回 {t} 又在顶上",
                yb == 0 and yback == ya and yfwd == 0,
                (yb, yback, yfwd),
            )
            ctx.close()
    done()
