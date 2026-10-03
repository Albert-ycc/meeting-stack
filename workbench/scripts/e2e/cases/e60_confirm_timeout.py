"""带 action 的确认框（项目页「移除材料根目录」）：写请求超时以后不再给「再执行一次」，确认键换成「关闭」；别的错误照旧可重试

超时用 Playwright 的假时钟快进 121 秒，不真等；DELETE 用 page.route 按住不回或回 409，不动实例里的数据。"""

import json

from common import (
    BASE,
    check,
    Collector,
    done,
    ENGINE,
    hold,
    launch,
    new_page,
    release,
    sync_playwright,
)

ENGINES = ("chromium", "webkit", "firefox")


def footer_buttons(page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('[role=alertdialog] .confirm-modal__footer button')).map(b => b.innerText.trim())"
    )


def focus_text(page):
    return page.evaluate(
        "() => (document.activeElement && document.activeElement.innerText || '').trim()"
    )


def open_confirm(page):
    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    page.wait_for_timeout(600)
    page.get_by_role("tab", name="材料").click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name="移除", exact=True).click()
    page.wait_for_selector("[role=alertdialog]", timeout=8000)
    page.wait_for_timeout(500)


def press_confirm(page):
    page.keyboard.press("Alt+Tab" if ENGINE == "webkit" else "Tab")
    check("Tab 一下到确认键「移除」", focus_text(page) == "移除", focus_text(page))
    page.keyboard.press("Enter")


with sync_playwright() as p:
    # 超时：DELETE 按住不回，假时钟快进过 120 秒
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "写请求超时")
    deletes = hold(
        page, "**/api/projects/*/material-roots/*", only=lambda request: request.method == "DELETE"
    )
    page.clock.install()
    open_confirm(page)
    check("弹出时焦点在「取消」上", focus_text(page) == "取消", focus_text(page))
    press_confirm(page)
    page.wait_for_timeout(600)
    check(
        "等待中按钮写着「取消」「处理中…」",
        footer_buttons(page) == ["取消", "处理中…"],
        footer_buttons(page),
    )
    page.clock.fast_forward(121_000)
    page.wait_for_selector("[role=alertdialog] [role=alert]", timeout=8000)
    page.wait_for_timeout(500)
    alert = page.locator("[role=alertdialog] [role=alert]").inner_text()
    check(
        "超时提示：服务没有响应，可能仍在处理，稍后刷新确认",
        alert == "服务没有响应，可能仍在处理，稍后刷新确认",
        alert,
    )
    check(
        "脚注里只剩一个按钮「关闭」（不给再执行一次）",
        footer_buttons(page) == ["关闭"],
        footer_buttons(page),
    )
    check("焦点落在「关闭」上", focus_text(page) == "关闭", focus_text(page))
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    check("回车关掉了确认框", page.locator("[role=alertdialog]").count() == 0)
    check("DELETE 一共只发过一次", len(deletes) == 1, deletes)
    c.expect_clean("写请求超时")
    release(deletes)
    b.close()

    # 别的错误：第一次回 409，确认键还在，再点一次成功才关
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "别的错误").allow(r"^409 DELETE ")
    calls: list[int] = []

    def answer(route):
        if route.request.method != "DELETE":
            return route.continue_()
        calls.append(1)
        if len(calls) == 1:
            return route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "这个目录正在被占用"}),
            )
        return route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"ok": True})
        )

    page.route("**/api/projects/*/material-roots/*", answer)
    open_confirm(page)
    press_confirm(page)
    page.wait_for_selector("[role=alertdialog] [role=alert]", timeout=8000)
    page.wait_for_timeout(400)
    check(
        "409：错误原文显示在确认框里",
        page.locator("[role=alertdialog] [role=alert]").inner_text() == "这个目录正在被占用",
    )
    check(
        "409：确认键还在，可以再点", footer_buttons(page) == ["取消", "移除"], footer_buttons(page)
    )
    page.get_by_role("alertdialog").get_by_role("button", name="移除").click()
    page.wait_for_timeout(900)
    check("再点一次成功，确认框关了", page.locator("[role=alertdialog]").count() == 0)
    check("DELETE 共两次", len(calls) == 2, calls)
    c.expect_clean("别的错误")
    b.close()
    done()
