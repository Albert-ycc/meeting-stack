"""关系图节点面板的 Esc：焦点在按钮上、在页面上（点线、点面板里的字）都能关；盖着取径器 / 图例时只关最上面一层；输入框里的 Esc 不关面板"""

from common import (
    BASE,
    check,
    dialogs,
    done,
    launch,
    sync_playwright,
    track,
)

ENGINES = ("chromium", "webkit", "firefox")
GRAPH = "/#projects/project-yimi/graph"
PANEL = "() => !!document.querySelector('.project-graph__panel')"
PANEL_TEXT = ".project-graph__panel h2, .project-graph__panel h3, .project-graph__panel p"


def fresh(ctx, url):
    page = track(ctx.new_page())
    page.goto(BASE + url, wait_until="networkidle")
    page.wait_for_timeout(1200)
    return page


def esc(page, wait=400):
    page.keyboard.press("Escape")
    page.wait_for_timeout(wait)


def panel(page) -> bool:
    return page.evaluate(PANEL)


with sync_playwright() as p:
    b = launch(p)
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, timezone_id="Asia/Shanghai")
    ctx.set_default_timeout(10_000)

    page = fresh(ctx, GRAPH)
    page.locator(".graph-viewport [data-node-id^='m:']").first.click()
    page.wait_for_timeout(700)
    check("A 点会议节点：面板开了", panel(page))
    esc(page)
    check("A 焦点在按钮上按 Esc：面板关", not panel(page))
    page.close()

    page = fresh(ctx, GRAPH)
    # 点线的正中间：取 path 上中点的屏幕坐标用真鼠标点（线是 SVG 的 path，不能聚焦，点完焦点在 body）
    point = page.evaluate(
        """() => { const el = document.querySelector('.graph-viewport svg [role=button]');
          const p = el.getPointAtLength(el.getTotalLength() / 2), m = el.getScreenCTM();
          return { x: p.x * m.a + p.y * m.c + m.e, y: p.x * m.b + p.y * m.d + m.f } }"""
    )
    page.mouse.click(point["x"], point["y"])
    page.wait_for_timeout(700)
    check("B 点线：面板开了", panel(page))
    esc(page)
    check("B 点线之后焦点在页面上，按 Esc：面板关", not panel(page))
    page.close()

    page = fresh(ctx, GRAPH)
    page.locator(".graph-viewport [data-node-id^='m:']").first.click()
    page.wait_for_timeout(700)
    page.locator(PANEL_TEXT).first.click()
    page.wait_for_timeout(200)
    esc(page)
    check("C 点面板里的字之后按 Esc：面板关", not panel(page))
    page.close()

    # D / E：「资料盘搬走的项目」的材料文件夹已经不在（造数就是这样），根目录是「找不到」，点它有［重新选…］
    for tag, enter_first in (("D", False), ("E", True)):
        page = fresh(ctx, "/#projects/project-moved/graph")
        page.wait_for_timeout(1500)
        page.locator(".graph-viewport [data-node-id^='root:']").first.click()
        page.wait_for_timeout(1000)
        page.get_by_role("button", name="重新选…").click()
        page.wait_for_timeout(900)
        check(
            f"{tag} 点［重新选…］：取径器开在面板上",
            len(dialogs(page)) == 1 and panel(page),
            dialogs(page),
        )
        if enter_first:
            # 进下一级：点「›」，那个按钮被换掉，焦点掉回页面
            page.locator("button.material-picker__enter").first.click()
            page.wait_for_timeout(800)
        esc(page, 500)
        check(
            f"{tag} 第一次 Esc：只关取径器，面板还在",
            dialogs(page) == [] and panel(page),
            (dialogs(page), panel(page)),
        )
        esc(page, 500)
        check(f"{tag} 第二次 Esc：面板关", not panel(page))
        page.close()

    page = fresh(ctx, GRAPH)
    page.locator(".graph-viewport [data-node-id^='m:']").first.click()
    page.wait_for_timeout(600)
    page.get_by_role("button", name="图例").click()
    page.wait_for_timeout(300)
    check("F 面板和［图例］都开着", panel(page) and len(dialogs(page)) >= 1, dialogs(page))
    esc(page)
    check(
        "F 第一次 Esc：先收图例，面板还在",
        dialogs(page) == [] and panel(page),
        (dialogs(page), panel(page)),
    )
    esc(page)
    check("F 第二次 Esc：面板关", not panel(page))
    page.close()

    page = fresh(ctx, GRAPH)
    page.locator(".graph-viewport [data-node-id^='m:']").first.click()
    page.wait_for_timeout(600)
    page.get_by_role("searchbox", name="在图上找").click()
    page.keyboard.type("需求")
    esc(page, 300)
    check("G 在「在图上找」的输入框里按 Esc：面板不关", panel(page))
    page.close()

    page = fresh(ctx, "/#graph")
    page.wait_for_timeout(800)
    # 点行星本身是进项目图；面板从行星上的琥珀数（在等你的）打开
    page.locator("[aria-label^='医米科研用药：'][aria-label$='件在等你，打开面板']").click()
    page.wait_for_timeout(900)
    page.locator(
        ".overview-panel h2, .overview-panel h3, .overview-panel p, .overview-panel header"
    ).first.click()
    page.wait_for_timeout(200)
    esc(page, 500)
    check(
        "H 总图点行星上的琥珀数、再点面板里的字、按 Esc：面板关",
        page.locator("aside.overview-panel").count() == 0,
        page.url,
    )
    page.close()
    done()
