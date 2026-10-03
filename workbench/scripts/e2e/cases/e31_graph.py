"""关系图：放大 / 缩小 / 复位、滚轮平移、拖拽、点岛开面板、时间窗、双击进项目图、点连线开面板、Esc 关面板"""

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

WORLD = """() => { const g = document.querySelector('.graph-world'); const m = g && g.style.transform.match(/translate\\(([-\\d.]+)px, ([-\\d.]+)px\\) scale\\(([\\d.]+)\\)/);
  return m ? {x: +m[1], y: +m[2], k: +m[3]} : null }"""
ISLAND = "main [aria-label^='项目：医米科研用药']"

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "关系图")
    world = lambda: page.evaluate(WORLD)  # noqa: E731
    page.goto(BASE + "/#graph", wait_until="networkidle")
    page.wait_for_timeout(1200)
    start = world()
    check("总图画出来了（有 .graph-world）", start is not None, start)
    page.get_by_role("button", name="放大").click()
    page.wait_for_timeout(400)
    zoomed_in = world()
    check("放大：缩放比例变大", zoomed_in["k"] > start["k"], (start["k"], zoomed_in["k"]))
    page.get_by_role("button", name="缩小").click()
    page.get_by_role("button", name="缩小").click()
    page.wait_for_timeout(400)
    zoomed_out = world()
    check(
        "缩小两下：比放大那一下小，也比开始小",
        zoomed_out["k"] < zoomed_in["k"] and zoomed_out["k"] < start["k"],
        zoomed_out["k"],
    )
    page.get_by_role("button", name="复位").click()
    page.wait_for_timeout(400)
    reset = world()
    check(
        "复位：回到开始的位置和比例",
        abs(reset["k"] - start["k"]) < 0.01
        and abs(reset["x"] - start["x"]) < 2
        and abs(reset["y"] - start["y"]) < 2,
        (start, reset),
    )
    page.mouse.move(800, 650)
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(400)
    waved = world()
    check(
        "滚轮是平移（设计如此）：位置变、比例不变，页面本身不滚",
        (waved["x"], waved["y"]) != (reset["x"], reset["y"])
        and abs(waved["k"] - reset["k"]) < 0.001
        and scroll_y(page) == 0,
        (reset, waved, scroll_y(page)),
    )
    page.get_by_role("button", name="复位").click()
    page.wait_for_timeout(400)
    page.mouse.move(800, 650)
    page.mouse.down()
    page.mouse.move(600, 550, steps=10)
    page.mouse.up()
    page.wait_for_timeout(300)
    dragged = world()
    check(
        "拖拽平移：往左上拖了 200×100，画布跟着挪（误差 30 以内）",
        abs((dragged["x"] - reset["x"]) + 200) < 30 and abs((dragged["y"] - reset["y"]) + 100) < 30,
        (reset, dragged),
    )
    page.get_by_role("button", name="复位").click()
    page.wait_for_timeout(400)

    page.locator(ISLAND).first.click()
    page.wait_for_timeout(1000)
    check(
        "点岛：地址带 sel=p:project-yimi，右边开出项目面板",
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
    page.locator(ISLAND).first.dblclick()
    page.wait_for_timeout(1800)
    check("双击岛：进项目图", hash_of(page) == "#projects/project-yimi/graph", hash_of(page))

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
