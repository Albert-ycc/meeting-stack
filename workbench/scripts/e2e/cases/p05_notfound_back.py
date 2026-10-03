"""「会议不存在」页的［← 返回］：应用里打开的回到来处，冷加载直达的回到工作台"""

from common import (
    BASE,
    check,
    Collector,
    done,
    hash_of,
    heading,
    launch,
    new_page,
    settle,
    sync_playwright,
)

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "会议不存在").allow(r"^404 GET ")
    page.goto(BASE + "/#library", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.evaluate("() => { location.hash = '#meetings/nope' }")
    settle(page, 800)
    back = page.get_by_role("button", name="← 返回录音档案")
    check("在应用里打开不存在的会议：返回按钮写着来处「录音档案」", back.count() == 1)
    back.click()
    settle(page, 800)
    check("点返回：回到 #library", hash_of(page) == "#library", hash_of(page))
    ctx.close()
    ctx, page = new_page(b)
    c2 = Collector(page, "会议不存在-冷加载").allow(r"^404 GET ")
    page.goto(BASE + "/#meetings/nope", wait_until="networkidle")
    page.wait_for_timeout(500)
    back = page.get_by_role("button", name="← 返回工作台")
    check("冷加载直达不存在的会议：返回按钮写着「工作台」", back.count() == 1)
    back.click()
    settle(page, 800)
    check(
        "点返回：回到工作台",
        hash_of(page) == "" and heading(page) == "工作台",
        (hash_of(page), heading(page)),
    )
    done(c, c2)
