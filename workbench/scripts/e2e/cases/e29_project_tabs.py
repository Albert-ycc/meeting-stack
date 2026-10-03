"""项目详情：顶部页签（需求与任务 / 录音 / 材料）按项目记住，和关系图页签配合；新建项目、同名提示"""

from common import (
    BASE,
    check,
    Collector,
    dialogs,
    done,
    fetch_json,
    hash_of,
    launch,
    nav,
    new_page,
    notices,
    sync_playwright,
    tabs_selected,
)

MUTATES = True
STORAGE_KEY = "meeting-workbench:view:project.project-yimi.tab"

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "项目页签").allow(r"^409 POST \S*/api/projects$")
    page.on("dialog", lambda d: d.accept())

    def tab() -> str:
        return " / ".join(tabs_selected(page))

    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    page.wait_for_timeout(1500)
    check("全新进入：停在「需求与任务」", "需求与任务" in tab(), tab())
    page.get_by_role("tab", name="录音").click()
    page.wait_for_timeout(600)
    check("点「录音」", "录音" in tab(), tab())
    page.get_by_role("tab", name="关系图").click()
    page.wait_for_timeout(1500)
    check(
        "点「关系图」：地址带 /graph",
        "关系图" in tab() and hash_of(page).endswith("/graph"),
        (tab(), hash_of(page)),
    )
    page.go_back()
    page.wait_for_timeout(1500)
    check("浏览器后退（回清单）：回到记着的「录音」，不是固定的第一个", "录音" in tab(), tab())
    page.go_forward()
    page.wait_for_timeout(1500)
    check("前进：回到关系图", "关系图" in tab(), tab())
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    check("在关系图地址上刷新：停在关系图，不被记着的「录音」顶掉", "关系图" in tab(), tab())
    page.get_by_role("tab", name="材料").click()
    page.wait_for_timeout(1000)
    check("从关系图直接点「材料」", "材料" in tab(), tab())
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    check("在材料上刷新：还在材料", "材料" in tab(), tab())
    nav(page, "词典", 600)
    nav(page, "项目管理", 800)
    page.get_by_role("button", name="进入医米科研用药").click()
    page.wait_for_timeout(1500)
    check("离开再从列表进同一个项目：还是材料", "材料" in tab(), tab())
    nav(page, "项目管理", 800)
    page.get_by_role("button", name="进入恒瑞健康").click()
    page.wait_for_timeout(1500)
    check("另一个项目各记各的：恒瑞健康先看需求与任务", "需求与任务" in tab(), tab())
    page.evaluate("k => sessionStorage.setItem(k, JSON.stringify('graph'))", STORAGE_KEY)
    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1500)
    check("存着的页签值不合法（graph）：当没存，回到需求与任务", "需求与任务" in tab(), tab())

    # 新建项目：创建后直接进新项目；同名给提示、不重复创建
    nav(page, "项目管理", 800)
    name = "E2E 新项目 <i>x</i>"
    page.get_by_role("button", name="＋ 新建项目").click()
    page.wait_for_timeout(800)
    dialog = page.get_by_role("dialog", name="新建项目")
    dialog.get_by_placeholder("例如：互联网医院").fill(name)
    dialog.get_by_role("button", name="创建", exact=True).click()
    page.wait_for_timeout(1500)
    names = [project["name"] for project in fetch_json(page, "/api/projects")]
    check("新建项目：库里有了，名字里的尖括号原样", names.count(name) == 1, names)
    check(
        "新建项目：创建后直接进新项目，标题是按文字显示的",
        hash_of(page).startswith("#projects/")
        and name in page.locator("main").inner_text()
        and page.locator("main i").count() == 0,
        hash_of(page),
    )
    nav(page, "项目管理", 800)
    page.get_by_role("button", name="＋ 新建项目").click()
    page.wait_for_timeout(800)
    dialog = page.get_by_role("dialog", name="新建项目")
    dialog.get_by_placeholder("例如：互联网医院").fill(name)
    dialog.get_by_role("button", name="创建", exact=True).click()
    page.wait_for_timeout(1200)
    check(
        "同名再建：弹窗不关，提示「已有……是不是它？」",
        dialogs(page) == ["新建项目"]
        and any("已有" in t and "是不是它" in t for t in notices(page)),
        (dialogs(page), notices(page)),
    )
    names = [project["name"] for project in fetch_json(page, "/api/projects")]
    check("同名再建：没有建出第二个", names.count(name) == 1, names)
    done(c)
