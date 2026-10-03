"""弹窗叠着弹窗时按 Esc：只关最上层，再按才关下一层；危险确认框默认焦点在「取消」，回车只是取消"""

from common import (
    BASE,
    check,
    Collector,
    dialogs,
    done,
    ENGINE,
    launch,
    new_page,
    sync_playwright,
)

MUTATES = True
ENGINES = ("chromium", "webkit", "firefox")
TERM = "EDC"
# Safari（Playwright 的 WebKit）默认 Tab 不停在按钮上，要 Option+Tab，其余引擎 Tab 就够
TAB = "Alt+Tab" if ENGINE == "webkit" else "Tab"


def active_text(page):
    return page.evaluate(
        "() => { const a = document.activeElement; return a ? (a.getAttribute('aria-label') || a.innerText || a.tagName).trim().slice(0, 20) : null }"
    )


def nested(page, title, open_lower, open_upper, lower):
    """open_lower 打开下层，open_upper 在它上面再开一层；Esc 一次关最上面一层，再一次关下层"""
    open_lower()
    page.wait_for_timeout(600)
    check(f"{title}：下层打开", dialogs(page) == [lower], dialogs(page))
    open_upper()
    page.wait_for_timeout(700)
    check(
        f"{title}：叠上第二层", len(dialogs(page)) == 2 and dialogs(page)[0] == lower, dialogs(page)
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check(f"{title}：Esc 一次只关最上层", dialogs(page) == [lower], dialogs(page))
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check(f"{title}：Esc 再一次关下层", dialogs(page) == [], dialogs(page))


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "Esc")
    page.on("dialog", lambda d: d.dismiss())

    page.goto(BASE + "/#projects", wait_until="networkidle")
    page.wait_for_timeout(600)
    nested(
        page,
        "新建项目表单 + 取径器",
        lambda: page.get_by_role("button", name="＋ 新建项目").click(),
        lambda: page.get_by_role("button", name="＋ 选别的文件夹").click(),
        "新建项目",
    )

    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(800)
    nested(
        page,
        "任务抽屉 + 登记交付物",
        lambda: page.get_by_text("下周三前定方案", exact=False).first.click(),
        lambda: page.get_by_role("button", name="＋ 登记交付物").click(),
        "任务详情",
    )

    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    page.wait_for_timeout(1000)
    nested(
        page,
        "编辑项目表单 + 取径器",
        lambda: page.get_by_role("button", name="编辑项目").click(),
        lambda: page.get_by_role("button", name="＋ 添加目录").click(),
        "编辑项目",
    )

    # 危险确认框：默认焦点在取消，回车只是取消；Tab 一下到确认键
    page.goto(BASE + "/#glossary", wait_until="networkidle")
    page.wait_for_timeout(800)
    delete = page.get_by_role("button", name=f"删除『{TERM}』")
    delete.click()
    page.wait_for_timeout(700)
    check("删除术语：弹出确认框", dialogs(page) == [f"删除术语「{TERM}」？"], dialogs(page))
    check("确认框弹出时焦点在「取消」", active_text(page) == "取消", active_text(page))
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    check(
        "直接回车只是取消，确认框关了、术语还在",
        dialogs(page) == [] and delete.count() == 1,
        dialogs(page),
    )
    delete.click()
    page.wait_for_timeout(700)
    page.keyboard.press(TAB)
    check("Tab 一下到「删除」", active_text(page) == "删除", active_text(page))
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check("Esc 取消确认框，术语还在", dialogs(page) == [] and delete.count() == 1, dialogs(page))
    delete.click()
    page.wait_for_timeout(700)
    page.keyboard.press(TAB)
    page.keyboard.press("Enter")
    page.wait_for_timeout(900)
    check("Tab 到「删除」再回车：照常删掉", delete.count() == 0, delete.count())
    done(c)
