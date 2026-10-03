"""展开一场会里的决议面板的 Esc：焦点在展开的会里时一次 Esc 只关面板、再按一次才回到关系图；点面板里的字（焦点在页面上）也能关面板"""

from common import (
    BASE,
    check,
    done,
    launch,
    sync_playwright,
    track,
)

ENGINES = ("chromium", "webkit", "firefox")
PANEL = "() => !!document.querySelector('.project-graph__panel')"
EXPANDED = "() => !!document.querySelector('[aria-label^=\"展开的会\"]')"


def open_panel(ctx):
    page = track(ctx.new_page())
    page.goto(BASE + "/#projects/project-yimi/graph", wait_until="networkidle")
    page.wait_for_timeout(1200)
    page.locator(".graph-viewport [data-node-id^='m:']").first.dblclick()
    page.wait_for_timeout(1500)
    page.locator(
        "[aria-label^='展开的会'] [aria-label^='决议：'], [aria-label^='展开的会'] [aria-label^='任务']"
    ).first.click()
    page.wait_for_timeout(900)
    return page


with sync_playwright() as p:
    b = launch(p)
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, timezone_id="Asia/Shanghai")
    ctx.set_default_timeout(10_000)

    page = open_panel(ctx)
    check("I1 点决议后：面板开了，展开的会还在", page.evaluate(PANEL) and page.evaluate(EXPANDED))
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check(
        "I1 第一次 Esc：只关面板，展开的会还在",
        not page.evaluate(PANEL) and page.evaluate(EXPANDED),
        (page.evaluate(PANEL), page.evaluate(EXPANDED)),
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    check(
        "I1 第二次 Esc：收起展开的会，回到关系图",
        not page.evaluate(EXPANDED),
        page.evaluate(EXPANDED),
    )
    page.close()

    page = open_panel(ctx)
    page.locator(
        ".project-graph__panel h2, .project-graph__panel h3, .project-graph__panel p"
    ).first.click()
    page.wait_for_timeout(200)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check(
        "I2 点面板里的字（焦点在页面上）按 Esc：面板关，展开的会还在",
        not page.evaluate(PANEL) and page.evaluate(EXPANDED),
        (page.evaluate(PANEL), page.evaluate(EXPANDED)),
    )
    page.close()
    done()
