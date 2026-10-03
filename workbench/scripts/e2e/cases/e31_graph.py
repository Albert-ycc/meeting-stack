"""关系图：放大 / 缩小 / 复位、滚轮缩放、拖动转视角、点琥珀数开面板、时间窗、点行星进项目图、点连线开面板、Esc 关面板"""

from common import (
    BASE,
    check,
    Collector,
    done,
    hash_of,
    launch,
    new_page,
    scroll_y,
    sync_playwright,
)

# 星图左下角的视角读数：方位、仰角、缩放
VIEW = """() => { const r = document.querySelector('.star-readout');
  const m = r && r.textContent.match(/方位\\s*(\\d+)°\\s*仰角\\s*(\\d+)°\\s*缩放\\s*([\\d.]+)×/);
  return m ? {az: +m[1], el: +m[2], k: +m[3]} : null }"""
# 舞台下半部里一块空的画布（没有名字、读数块盖着），拖动从这里开始
EMPTY_SPOT = """() => { const c = document.querySelector('.star-canvas').getBoundingClientRect();
  for (let y = c.top + c.height * 0.62; y < c.bottom - 140; y += 17)
    for (let x = c.left + c.width * 0.3; x < c.right - c.width * 0.3; x += 23) {
      const hit = document.elementFromPoint(x, y);
      if (hit && hit.classList.contains('star-canvas')) return {x, y};
    }
  return null }"""
ISLAND = "main [aria-label^='项目：医米科研用药']"
# 行星上的琥珀数（在等你的）：种子里医米科研用药有待确认的任务
AMBER = "main [aria-label^='医米科研用药：'][aria-label$='件在等你，打开面板']"

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "关系图")
    view = lambda: page.evaluate(VIEW)  # noqa: E731
    page.goto(BASE + "/#graph", wait_until="networkidle")
    page.wait_for_timeout(1200)
    start = view()
    check(
        "总图画出来了：读数是打开时的视角（方位 0°、仰角 32°、缩放 1）",
        start == {"az": 0, "el": 32, "k": 1.0},
        start,
    )
    page.get_by_role("button", name="放大").click()
    page.wait_for_timeout(400)
    zoomed_in = view()
    check("放大：缩放变大", zoomed_in["k"] > start["k"], (start["k"], zoomed_in["k"]))
    page.get_by_role("button", name="缩小").click()
    page.get_by_role("button", name="缩小").click()
    page.wait_for_timeout(400)
    zoomed_out = view()
    check(
        "缩小两下：比放大那一下小，也比开始小",
        zoomed_out["k"] < zoomed_in["k"] and zoomed_out["k"] < start["k"],
        zoomed_out["k"],
    )
    page.get_by_role("button", name="复位").click()
    page.wait_for_timeout(800)
    reset = view()
    check("复位：回到打开时的视角", reset == start, (start, reset))
    page.mouse.move(800, 650)
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(400)
    waved = view()
    check(
        "滚轮是缩放（设计如此）：缩放变大、方位和仰角不变，页面本身不滚",
        waved["k"] > reset["k"]
        and (waved["az"], waved["el"]) == (reset["az"], reset["el"])
        and scroll_y(page) == 0,
        (reset, waved, scroll_y(page)),
    )
    page.get_by_role("button", name="复位").click()
    page.wait_for_timeout(800)
    spot = page.evaluate(EMPTY_SPOT)
    check("舞台下半部找得到一块空的画布", spot is not None, spot)
    page.mouse.move(spot["x"], spot["y"])
    page.mouse.down()
    page.mouse.move(spot["x"] + 200, spot["y"], steps=10)
    page.mouse.up()
    page.wait_for_timeout(1500)
    turned = view()
    check(
        "横拖转盘：往右拖了 200，方位角跟着转（加上松手后的惯性），仰角不变",
        turned["az"] != reset["az"] and turned["el"] == reset["el"],
        (reset, turned),
    )
    page.mouse.move(spot["x"], spot["y"])
    page.mouse.down()
    page.mouse.move(spot["x"], spot["y"] - 100, steps=10)
    page.mouse.up()
    page.wait_for_timeout(1500)
    tilted = view()
    check(
        "纵拖调仰角：往上拖，仰角变大（俯视得更多）", tilted["el"] > turned["el"], (turned, tilted)
    )
    page.locator(".overview-viewport").focus()
    page.keyboard.press("0")
    page.wait_for_timeout(800)
    check("按 0 复位", view() == start, view())

    page.locator(AMBER).first.click()
    page.wait_for_timeout(1000)
    check(
        "点行星上的琥珀数：地址带 sel=p:project-yimi，右边开出项目面板",
        "sel=p:project-yimi" in page.url
        and page.locator("aside.overview-panel").count() == 1
        and "医米科研用药" in page.locator("aside.overview-panel").inner_text(),
        page.url,
    )
    for window in ("7 天", "90 天", "全部", "28 天"):
        page.get_by_role("button", name=window, exact=True).click()
        page.wait_for_timeout(700)
        label = page.locator(ISLAND).first.get_attribute("aria-label")
        check(
            f"时间窗切到「{window}」：按钮按下、岛的名字里写着它",
            page.get_by_role("button", name=window, exact=True).get_attribute("aria-pressed")
            == "true"
            and window in label,
            label,
        )
    page.locator(ISLAND).first.click()
    page.wait_for_timeout(1800)
    check("单击行星：进项目图", hash_of(page) == "#projects/project-yimi/graph", hash_of(page))

    lines = page.locator("main svg [role=button], main svg [tabindex]")
    check("项目图里有连线可以点", lines.count() >= 2, lines.count())
    point = page.evaluate(
        """() => { const el = document.querySelectorAll('main svg [role=button], main svg [tabindex]')[1]; const p = el.getPointAtLength(el.getTotalLength() / 2), m = el.getScreenCTM();
          return { x: p.x * m.a + p.y * m.c + m.e, y: p.x * m.b + p.y * m.d + m.f } }"""
    )
    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(1000)
    check(
        "点一条线：地址带 sel=，开出面板",
        "sel=" in page.url and page.locator(".project-graph__panel").count() == 1,
        page.url,
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check(
        "按 Esc：面板关了，sel 也去掉了（点线后焦点在页面上也一样）",
        page.locator(".project-graph__panel").count() == 0 and "sel=" not in page.url,
        page.url,
    )
    page.go_back()
    page.wait_for_timeout(800)
    check("后退：退出项目图，回到总图", hash_of(page).startswith("#graph"), hash_of(page))
    done(c)
