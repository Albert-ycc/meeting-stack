"""点时间 = 跳到那里并开始播放：详情页逐字稿的时间、检索结果的时间锚；带时间点的卡片链接冷打开只跳不放；放起来以后当前句跟着滚

音频用 wav 的那场会：Playwright 自带的 Chromium 不一定解得了 m4a 里的 AAC，wav 才稳妥地看得到 currentTime 往前走。"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    settle,
    sync_playwright,
)

ENGINES = ("chromium", "webkit")
MEETING = mid("e2e2e2e2")
AUDIO = "() => { const a = document.querySelector('audio'); return a ? { paused: a.paused, t: Math.round(a.currentTime * 10) / 10, ready: a.readyState } : null }"
WAIT_READY = (
    "() => document.querySelector('audio') && document.querySelector('audio').readyState >= 1"
)


def moving(page, wait: int = 1200):
    first = page.evaluate(AUDIO)
    page.wait_for_timeout(wait)
    second = page.evaluate(AUDIO)
    return first, second, bool(first and second and second["t"] > first["t"])


with sync_playwright() as p:
    b = launch(p)

    # 1. 详情页里点逐字稿的 01:00
    ctx, page = new_page(b, height=900)
    c = Collector(page, "点逐字稿的时间")
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_selector(".transcript-row")
    page.wait_for_function(WAIT_READY)
    opened = page.evaluate(AUDIO)
    check("没点任何东西时不自己放", opened["paused"] and opened["t"] == 0, opened)
    page.locator(".transcript-row .segment-time", has_text="01:00").first.click()
    first, second, advancing = moving(page)
    check(
        "点逐字稿的 01:00：跳到 60 秒并开始放，时间在走",
        not first["paused"] and first["t"] >= 59.5 and advancing,
        (first, second),
    )
    current = page.evaluate(
        "() => document.querySelector('.transcript-row.is-current .segment-time')?.innerText"
    )
    check("当前句标在 01:00 这一句上", current == "01:00", current)
    c.expect_clean("点逐字稿的时间")
    ctx.close()

    # 2. 从检索结果的时间锚打开会议
    ctx, page = new_page(b, height=900)
    c = Collector(page, "检索的时间锚")
    page.goto(BASE + "/", wait_until="networkidle")
    page.get_by_label("全局检索").fill("播放验证词")
    page.get_by_label("全局检索").press("Enter")
    settle(page, 800)
    hits = page.locator("article.search-hit")
    check(
        "检索「播放验证词」：命中那场会（两处）",
        hits.count() >= 1 and page.locator("article.search-hit button.time-anchor").count() >= 2,
        hits.count(),
    )
    hits.first.locator("button.time-anchor").first.click()
    page.wait_for_selector("section.detail-page")
    page.wait_for_function(WAIT_READY)
    page.wait_for_timeout(300)
    first, second, advancing = moving(page)
    check(
        "从检索的时间锚打开：从那个时间点开始放，时间在走",
        not first["paused"] and first["t"] >= 59 and advancing,
        (first, second),
    )
    c.expect_clean("检索的时间锚")
    ctx.close()

    # 3. 地址里带时间点（飞书会议卡片的 #meetings/<id>@<秒>）：全新页面直接进，没有用户手势，浏览器拒绝自动播放：停在那一秒
    ctx, page = new_page(b, height=900)
    c = Collector(page, "卡片链接冷打开")
    page.goto(BASE + "/#meetings/" + MEETING + "@125", wait_until="networkidle")
    page.wait_for_function(WAIT_READY)
    page.wait_for_timeout(500)
    first, second, advancing = moving(page)
    check(
        "卡片链接 @125 冷打开：停在 125 秒、没有被拒绝的自动播放弄出错误",
        abs(first["t"] - 125) < 1 and page.locator(".detail-error").count() == 0,
        (first, second),
    )
    c.expect_clean("卡片链接冷打开")
    ctx.close()

    # 4. 点了时间键（焦点停在键上）开始放以后，当前句跟着滚
    ctx, page = new_page(b, height=800)
    c = Collector(page, "放起来以后跟随")
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_selector(".transcript-row")
    page.wait_for_function(WAIT_READY)
    page.wait_for_timeout(500)
    index = page.evaluate(
        """() => { const box = document.querySelector('.transcript-scroll').getBoundingClientRect(); const rows = Array.from(document.querySelectorAll('.transcript-row'));
          let last = 0; rows.forEach((r, i) => { const b = r.getBoundingClientRect(); if (b.bottom <= box.bottom && b.top >= box.top) last = i; }); return last }"""
    )
    page.locator(".transcript-row").nth(index).locator(".segment-time").click()
    page.wait_for_timeout(12000)
    follow = page.evaluate(
        """() => { const box = document.querySelector('.transcript-scroll'); const br = box.getBoundingClientRect(); const cur = document.querySelector('.transcript-row.is-current');
          const r = cur && cur.getBoundingClientRect();
          return { inView: !!r && r.top >= br.top - 1 && r.bottom <= br.bottom + 1, scrollTop: Math.round(box.scrollTop), t: Math.round(document.querySelector('audio').currentTime * 10) / 10 } }"""
    )
    check(
        "点了可见范围里最靠下的一行的时间键，放 12 秒后：当前句在滚动框里，滚动框跟着往下滚了",
        follow["inView"] and follow["scrollTop"] > 0 and follow["t"] > 10,
        follow,
    )
    c.expect_clean("放起来以后跟随")
    ctx.close()
    done()
