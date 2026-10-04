"""待办：确认一条 → Toast［撤销］；整卡［全部确认］→ 撤销；页签和查询条件离开再回来、刷新后都保留"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    nav,
    new_page,
    notices,
    pending_tasks,
    poll,
    settle,
    sync_playwright,
)

MUTATES = True

UNDO = ".app-toast .app-toast__undo"


def tab_state(page):
    return page.evaluate(
        """() => [document.querySelector('main [role=tab][aria-selected=true]')?.innerText.replace(/\\n/g, ' '),
                  document.querySelector('input[placeholder="输入任务名称"]')?.value]"""
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "待办管理")
    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(400)
    page.locator("main [role=tab]", has_text="待确认").first.click()
    settle(page, 600)

    n0 = pending_tasks(page)
    check("有待确认的任务可点", n0 >= 4, n0)
    page.locator("main button.review-button").first.click()
    after_one = poll(lambda: pending_tasks(page), lambda v: v == n0 - 1)
    check("确认一条：待确认少 1", after_one == n0 - 1, (n0, after_one))
    check(
        "出了带［撤销］的提示",
        poll(lambda: page.locator(UNDO).count(), lambda v: v == 1) == 1,
        notices(page),
    )
    page.locator(UNDO).first.click()
    restored = poll(lambda: pending_tasks(page), lambda v: v == n0)
    check("点［撤销］：待确认回来", restored == n0, (n0, restored))

    # 整卡［全部确认］
    page.wait_for_timeout(500)
    page.locator("main").get_by_role("button", name="全部确认").first.click()
    page.wait_for_timeout(700)
    if page.locator("[role=dialog], [role=alertdialog]").count():
        page.locator("[role=dialog], [role=alertdialog]").get_by_role("button").filter(
            has_text="确认"
        ).last.click()
    after_all = poll(lambda: pending_tasks(page), lambda v: v <= n0 - 2)
    check("全部确认：这张卡上的草稿都确认了（至少 2 条）", after_all <= n0 - 2, (n0, after_all))
    check(
        "全部确认之后也有［撤销］",
        poll(lambda: page.locator(UNDO).count(), lambda v: v == 1) == 1,
        notices(page),
    )
    page.locator(UNDO).first.click()
    restored = poll(lambda: pending_tasks(page), lambda v: v == n0)
    check("撤销全部确认：待确认回到原来的数", restored == n0, (n0, restored))

    # 页签、查询条件：离开再回来、刷新后都在
    page.locator("main [role=tab]", has_text="未完成").first.click()
    settle(page, 500)
    page.get_by_placeholder("输入任务名称").fill("方案")
    page.get_by_role("button", name="查询").click()
    settle(page, 500)
    want = tab_state(page)
    check(
        "页签选在「未完成」、查询词是「方案」",
        want[0] is not None and "未完成" in want[0] and want[1] == "方案",
        want,
    )
    nav(page, "词典管理", 400)
    nav(page, "待办管理", 600)
    check("离开再回来：页签和查询词原样", tab_state(page) == want, tab_state(page))
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    check("刷新后：页签和查询词原样", tab_state(page) == want, tab_state(page))
    done(c)
