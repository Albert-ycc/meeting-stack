"""列表 → 会议 → 回来：回到点下去那一刻的滚动位置（录音档案、需求详情、检索结果；浏览器后退和返回按钮各一遍）

Playwright 点击会先把目标滚进视口，所以以 mousedown 那一刻的 scrollTop 为准，不以点之前手动滚到的数为准。"""

from common import (
    BASE,
    check,
    done,
    hash_of,
    launch,
    new_page,
    scroll_to,
    scroll_y,
    settle,
    sync_playwright,
)

MARK = """() => { window.__at = null; addEventListener('mousedown', () => { window.__at = Math.round(document.scrollingElement.scrollTop) }, {capture: true, once: true}) }"""
TOLERANCE = 2


def back(page, how):
    if how == "browser-back":
        page.go_back()
    else:
        page.locator("section.detail-page").get_by_role("button", name="返回").first.click()
    settle(page, 1200)


def same(got, want) -> bool:
    return want is not None and abs(got - want) <= TOLERANCE


with sync_playwright() as p:
    b = launch(p)
    # 录音档案：两种窗口高度 × 点第几行 × 在详情里滚了多少 × 两种返回
    for h in (600, 800):
        for row in (2, 9, 13):
            for d in (0, 3000):
                for how in ("browser-back", "back-button"):
                    ctx, page = new_page(b, height=h)
                    page.goto(BASE + "/#library", wait_until="networkidle")
                    page.wait_for_timeout(500)
                    scroll_to(page, 600)
                    page.wait_for_timeout(300)
                    page.evaluate(MARK)
                    page.locator("button.archive-row").nth(row).click()
                    settle(page, 900)
                    at = page.evaluate("() => window.__at")
                    scroll_to(page, d)
                    page.wait_for_timeout(300)
                    back(page, how)
                    got = scroll_y(page)
                    check(
                        f"录音档案 窗口高 {h}、第 {row + 1} 行、详情里滚到 {d}、{how}：回到 {at}",
                        same(got, at),
                        got,
                    )
                    ctx.close()
    # 需求详情：点［打开会议］
    for start in (0, 500, 99999):
        for how in ("browser-back", "back-button"):
            ctx, page = new_page(b, height=700)
            page.goto(BASE + "/#requirements/req-jd", wait_until="networkidle")
            page.wait_for_timeout(600)
            scroll_to(page, start)
            page.wait_for_timeout(300)
            page.evaluate(MARK)
            page.get_by_role("button", name="打开会议").first.click()
            settle(page, 900)
            at = page.evaluate("() => window.__at")
            scroll_to(page, 400)
            page.wait_for_timeout(300)
            back(page, how)
            got = scroll_y(page)
            check(
                f"需求详情 起点 {start}、{how}：回到需求详情、位置 {at}",
                same(got, at) and hash_of(page) == "#requirements/req-jd",
                (got, hash_of(page)),
            )
            ctx.close()
    # 检索结果：点第 7 条的时间锚
    for how in ("browser-back", "back-button"):
        ctx, page = new_page(b, height=600)
        page.goto(BASE + "/#library", wait_until="networkidle")
        page.get_by_label("全局检索").fill("接口清单")
        page.get_by_label("全局检索").press("Enter")
        settle(page, 800)
        scroll_to(page, 900)
        page.wait_for_timeout(300)
        page.evaluate(MARK)
        page.locator("article.search-hit").nth(6).locator("button.time-anchor").first.click()
        settle(page, 900)
        at = page.evaluate("() => window.__at")
        back(page, how)
        got = scroll_y(page)
        hits = page.locator("article.search-hit").count()
        check(f"检索结果 {how}：回到 {at}，结果还在", same(got, at) and hits > 0, (got, hits))
        ctx.close()
    done()
