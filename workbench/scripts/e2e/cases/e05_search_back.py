"""检索结果 → 打开会议 → 浏览器后退 / ［← 返回检索结果］：检索结果还在（README 交互约定第一条）"""

from common import (
    BASE,
    check,
    done,
    launch,
    new_page,
    settle,
    sync_playwright,
)

STATE = """() => ({hash: location.hash, h1: (document.querySelector('main h1') || {}).innerText || null,
    detail: !!document.querySelector('section.detail-page'),
    hits: document.querySelectorAll('article.search-hit').length})"""

with sync_playwright() as p:
    b = launch(p)
    for start in ["#library", "", "#requirements", "#tasks"]:
        for how in ["browser-back", "back-button"]:
            for click in ["anchor", "title"]:
                label = f"从 {start or '工作台'} 检索，点{'时间锚' if click == 'anchor' else '标题'}打开，{'浏览器后退' if how == 'browser-back' else '点返回按钮'}"
                ctx, page = new_page(b, height=700)
                page.goto(BASE + "/" + start, wait_until="networkidle")
                page.get_by_label("全局检索").fill("接口清单")
                page.get_by_label("全局检索").press("Enter")
                settle(page, 800)
                before = page.evaluate(STATE)
                hit = page.locator("article.search-hit").first
                if click == "anchor":
                    hit.locator("button.time-anchor").first.click()
                else:
                    hit.locator("button, a").first.click()
                settle(page, 900)
                opened = page.evaluate(STATE)
                if how == "browser-back":
                    page.go_back()
                else:
                    page.locator("section.detail-page").get_by_role(
                        "button", name="返回"
                    ).first.click()
                settle(page, 900)
                after = page.evaluate(STATE)
                check(
                    f"{label}：先搜到了结果、再打开了会议",
                    before["hits"] > 0 and opened["detail"],
                    (before["hits"], opened["detail"]),
                )
                check(
                    f"{label}：回来检索结果还在，条数不变",
                    after["hits"] == before["hits"] and not after["detail"],
                    (before["hits"], after["hits"]),
                )
                ctx.close()
    done()
