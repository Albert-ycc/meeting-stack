"""异步取数的视图：A 滚到 y → 侧栏去 B → 浏览器后退回 A（回到 y）→ 前进、再后退（还是 y）"""

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

VIEWS = {
    "工作台": "",
    "录音档案": "#library",
    "需求池": "#requirements",
    "待办管理": "#tasks",
    "项目管理": "#projects",
    "项目详情": "#projects/project-yimi",
    "项目关系图": "#projects/project-yimi/graph",
    "词典管理": "#glossary",
    "需求详情": "#requirements/req-jd",
}
MAX = "() => document.scrollingElement.scrollHeight - innerHeight"

with sync_playwright() as p:
    b = launch(p)
    for name, hash_ in VIEWS.items():
        ctx, page = new_page(b, height=600)
        page.goto(BASE + "/" + hash_, wait_until="networkidle")
        page.wait_for_timeout(1000)
        want = min(400, page.evaluate(MAX))
        scroll_to(page, want)
        page.wait_for_timeout(300)
        at = scroll_y(page)
        page.locator("nav").get_by_role("button", name="转写录音").first.click()
        settle(page, 700)
        page.go_back()
        settle(page, 900)
        back = scroll_y(page)
        page.go_forward()
        settle(page, 700)
        page.go_back()
        settle(page, 900)
        back_again = scroll_y(page)
        check(
            f"{name}：滚到 {at} → 去转写录音 → 后退回到 {at}，再前进后退还是 {at}",
            back == at and back_again == at,
            (back, back_again),
        )
        ctx.close()
    done()
