"""侧栏「待办」角标：在待办页、工作台确认一条之后跟着变，切视图、刷新后都和接口一致"""

from common import (
    badge_number,
    BASE,
    check,
    Collector,
    done,
    launch,
    nav,
    new_page,
    pending_tasks,
    poll,
    sidebar_badge,
    sync_playwright,
)

MUTATES = True


def badge(page):
    return badge_number(sidebar_badge(page))


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "角标")
    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.locator("main [role=tab]", has_text="待确认").first.click()
    page.wait_for_timeout(500)
    start = pending_tasks(page)
    check("开始时角标和待确认的数一致", badge(page) == start, (badge(page), start))
    page.locator("main button.review-button").first.click()
    check(
        "在待办页确认一条：角标跟着少 1（3 秒内）",
        poll(lambda: badge(page), lambda v: v == start - 1, timeout=3) == start - 1,
        (badge(page), start - 1),
    )
    nav(page, "录音档案", 1000)
    check(
        "切到别的视图：角标还是少 1 的那个数",
        badge(page) == start - 1 == pending_tasks(page),
        (badge(page), pending_tasks(page)),
    )
    nav(page, "工作台", 1500)
    page.locator("main button.todo-item__confirm").first.click()
    check(
        "在工作台确认一条：角标又少 1，和接口一致",
        poll(lambda: badge(page), lambda v: v == start - 2, timeout=4)
        == start - 2
        == pending_tasks(page),
        (badge(page), pending_tasks(page)),
    )
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(800)
    check(
        "刷新后：角标和接口一致",
        badge(page) == pending_tasks(page),
        (badge(page), pending_tasks(page)),
    )
    done(c)
