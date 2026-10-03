"""冒烟（接替原来的 browser_smoke.py）：复制文件夹路径、检索跳转并播放、波形拖动、吸顶播放条贴着顶栏、手机和横屏下只读"""

from common import (
    BASE,
    check,
    Collector,
    done,
    heading,
    launch,
    new_page,
    scroll_to,
    settle_scroll,
    sync_playwright,
)

WAIT_READY = (
    "() => document.querySelector('audio') && document.querySelector('audio').readyState >= 1"
)


def layout(page):
    """吸顶播放条和顶栏、波形的位置：顶栏下沿比播放条上沿低多少（负数是留了缝）、波形有没有被它盖住。

    滚到底以后要等播放条的位置不再变：刚起的服务上详情页渲染得慢，量早了播放条还没吸住，量出来的是它的自然位置。"""
    scroll_to(page, 10**6)
    settle_scroll(page)
    measure = """() => { const r = s => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return {y: b.y, h: b.height, bottom: b.bottom} };
          const topbar = r('header.topbar'), dock = r('section.audio-transport-dock'), wave = r('.waveform');
          return {overlap: Math.round(topbar.bottom - dock.y), dockHeight: dock.h, waveClear: wave.bottom <= dock.y + 1} }"""
    previous = None
    for _ in range(20):
        current = page.evaluate(measure)
        if current == previous:
            break
        previous = current
        page.wait_for_timeout(300)
    return current


def hugs_topbar(m, tolerance: float) -> bool:
    """播放条贴着顶栏的下沿：压住或留缝都不超过 tolerance 像素"""
    return abs(m["overlap"]) <= tolerance


with sync_playwright() as p:
    b = launch(p)

    # 宽屏：录音档案里复制文件夹路径的小按钮
    ctx, page = new_page(
        b, width=1900, height=1000, permissions=["clipboard-read", "clipboard-write"]
    )
    c = Collector(page, "宽屏复制路径")
    page.goto(BASE + "/#library", wait_until="networkidle")
    page.add_style_tag(
        content="*, *::before, *::after { animation: none !important; transition: none !important; }"
    )
    page.get_by_role("heading", name="会议录音档案").wait_for()
    page.wait_for_timeout(750)
    copy = page.locator("button.archive-path-copy:not(:disabled)").first
    row_box = copy.locator("xpath=..").bounding_box()
    copy_box = copy.bounding_box()
    check(
        "复制路径按钮是个不带字的小图标（宽高不超过 32）",
        copy.text_content() == "" and copy_box["width"] <= 32 and copy_box["height"] <= 32,
        copy_box,
    )
    check(
        "它在那一行的框里面，没有伸出去",
        copy_box["x"] + copy_box["width"] <= row_box["x"] + row_box["width"],
        (copy_box, row_box),
    )
    expected = copy.get_attribute("data-copy-path")
    copy.click()
    page.get_by_role("button", name="文件夹路径已复制").wait_for()
    check(
        "点了：按钮变成「文件夹路径已复制」，剪贴板里就是那条路径",
        page.evaluate("navigator.clipboard.readText()") == expected,
        expected,
    )
    c.expect_clean("宽屏复制路径")
    ctx.close()

    # 桌面：检索「播放验证词」→ 点时间锚 → 详情页开始放；拖波形；吸顶播放条
    ctx, page = new_page(b, width=1440, height=1000)
    c = Collector(page, "桌面")
    page.goto(BASE, wait_until="networkidle")
    check(
        "工作台打开，没有「只读访问」标记",
        heading(page) == "工作台" and page.get_by_text("只读访问", exact=True).count() == 0,
        heading(page),
    )
    page.get_by_label("全局检索").fill("播放验证词")
    page.get_by_role("button", name="检索").click()
    page.locator("article.search-hit").first.wait_for()
    page.locator("button.time-anchor").first.click()
    page.locator("section.detail-page").wait_for()
    page.wait_for_function("() => document.querySelector('audio')?.currentTime > 0", timeout=10_000)
    # 命中的可能是纪要里的那一句，详情页就开在纪要页签；转写的当前句要回到逐字稿页签看
    page.locator(".detail-tabs button").first.click()
    page.locator(".transcript-row.is-current").first.wait_for(timeout=10_000)
    check(
        "点时间锚：详情页打开，音频在放，逐字稿里当前句高亮",
        page.get_by_role("button", name="编辑逐字稿").is_visible(),
    )
    # 放着的时候页面会跟着当前句往下滚，切页签也会把当前句滚进视野，波形随之滚出视口：先暂停、等滚动停了，再把波形滚到视口里去拖
    page.evaluate("() => document.querySelector('audio').pause()")
    settle_scroll(page, 30)
    wave = page.locator('.waveform [part="wrapper"]')
    for _ in range(5):
        wave.evaluate("element => element.scrollIntoView({ block: 'center', behavior: 'instant' })")
        settle_scroll(page)
        box = wave.bounding_box()
        if box and 0 <= box["y"] and box["y"] + box["height"] <= 1000:
            break
    check("波形滚进了视口，能拖", box and 0 <= box["y"], box)
    page.mouse.move(box["x"] + box["width"] * 0.1, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.72, box["y"] + box["height"] / 2, steps=12)
    page.mouse.up()
    page.wait_for_timeout(500)
    state = page.locator("audio").evaluate(
        "audio => ({ currentTime: audio.currentTime, duration: audio.duration })"
    )
    check(
        "在波形上从 10% 拖到 72%：播放位置跟到了 60% 以后",
        state["duration"] > 0 and state["currentTime"] > state["duration"] * 0.6,
        state,
    )
    m = layout(page)
    check("滚到底：吸顶播放条贴着顶栏的下沿（误差 2 以内）", hugs_topbar(m, 2), m)
    check("吸顶播放条不高（小于 80），波形没被它盖住", m["dockHeight"] < 80 and m["waveClear"], m)
    page.locator(".detail-tabs button").nth(1).click()
    check(
        "会议纪要页签里有［写回会议文件夹］",
        page.get_by_role("button", name="写回会议文件夹").is_visible(),
    )
    c.expect_clean("桌面")
    ctx.close()

    # 手机：只读，没有编辑和发布入口，吸顶播放条照样贴着顶栏
    ctx, page = new_page(b, width=390, height=844)
    c = Collector(page, "手机")
    page.goto(BASE, wait_until="networkidle")
    check("手机上有「只读访问」标记", page.get_by_text("只读访问", exact=True).count() == 1)
    page.get_by_label("全局检索").fill("播放验证词")
    page.get_by_role("button", name="检索").click()
    page.locator("button.time-anchor").first.wait_for()
    page.locator("button.time-anchor").first.click()
    page.locator("section.detail-page").wait_for()
    check(
        "手机上的详情页有只读标记，没有［编辑逐字稿］",
        page.locator(".detail-page .read-only-chip").is_visible()
        and page.get_by_role("button", name="编辑逐字稿").count() == 0,
    )
    m = layout(page)
    # 手机宽度下播放条的 top 在 CSS 里写死 91px，顶栏的高度却跟着内容和字体变（本机实测顶栏 105px，播放条和顶栏差 7～9px，
    # 压住和留缝都见过），贴不到桌面那样的 2px：差不出十几像素、播放条不高、波形没被盖住就行
    check(
        "手机：吸顶播放条在顶栏下沿附近（差不到 20px），高度小于 110，波形没被盖住",
        hugs_topbar(m, 20) and m["dockHeight"] < 110 and m["waveClear"],
        m,
    )
    page.locator(".detail-tabs button").nth(1).click()
    check(
        "手机：纪要页签里没有［写回会议文件夹］",
        page.get_by_role("button", name="写回会议文件夹").count() == 0,
    )
    c.expect_clean("手机")
    ctx.close()

    # 横屏（触屏）：也是只读
    ctx, page = new_page(b, width=900, height=500, has_touch=True)
    page.goto(BASE, wait_until="networkidle")
    check("手机横屏：有「只读访问」标记", page.get_by_text("只读访问", exact=True).count() == 1)
    ctx.close()
    done()
