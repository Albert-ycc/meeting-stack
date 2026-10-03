"""撤销被服务端拒绝（409）时，界面把后端文案摆出来（红色失败提示）：待办页和项目详情「需求与任务」里「挂到需求」的撤销

撤销那次的 PATCH 用 page.route 换成真的 409 响应（后端为了撤销放行，正常点不出 409）。"""

import json

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    notices,
    sync_playwright,
)

MUTATES = True
REJECT = "需求「旧需求」已搁置，只能挂到进行中的需求"


def reject_next_patch(page):
    """从现在起，下一个 PATCH /api/tasks/<id> 回 409"""
    state = {"hit": 0}

    def handler(route):
        if route.request.method == "PATCH" and "/api/tasks/" in route.request.url:
            state["hit"] += 1
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": REJECT}, ensure_ascii=False),
            )
        else:
            route.continue_()

    page.route("**/api/tasks/*", handler)
    return state


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "撤销被拒")

    # —— 待办页 ——
    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.locator("main [role=tab]", has_text="未完成").first.click()
    page.wait_for_timeout(600)
    row = page.locator("main li", has=page.get_by_role("button", name="挂到需求")).first
    row.get_by_role("button", name="挂到需求").first.click()
    page.wait_for_timeout(500)
    page.get_by_role("dialog").get_by_role("option").first.click()
    page.wait_for_timeout(900)
    toast = page.locator(".app-toast")
    check(
        "挂上以后出了带［撤销］的提示",
        toast.get_by_role("button", name="撤销").count() == 1,
        notices(page),
    )
    state = reject_next_patch(page)
    toast.get_by_role("button", name="撤销").click()
    page.wait_for_timeout(900)
    error = page.locator(".app-toast--error")
    check(
        "待办页：撤销被拒绝，红色失败提示里是后端文案",
        error.count() == 1 and REJECT in error.inner_text(),
        error.inner_text() if error.count() else "（没有提示）",
    )
    check("待办页：撤销确实发出了请求并被拒", state["hit"] == 1, state)
    page.unroute("**/api/tasks/*")

    # —— 项目详情的「需求与任务」 ——
    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    page.wait_for_timeout(1500)
    unlinked = page.locator(".work-unlinked li").first
    unlinked.get_by_role("button", name="挂到需求").first.click()
    page.wait_for_timeout(500)
    page.get_by_role("dialog").get_by_role("option").first.click()
    page.wait_for_timeout(900)
    toast = page.locator(".app-toast")
    state = reject_next_patch(page)
    toast.get_by_role("button", name="撤销").click()
    page.wait_for_timeout(900)
    error = page.locator(".app-toast--error")
    check(
        "项目详情：撤销被拒绝，红色失败提示里是后端文案",
        error.count() == 1 and REJECT in error.inner_text(),
        error.inner_text() if error.count() else "（没有提示）",
    )
    check("项目详情：撤销确实发出了请求并被拒", state["hit"] == 1, state)
    c.allow(r"^409 PATCH ")
    done(c)
