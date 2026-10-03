"""词典里改了没存、点「删除这条」弹确认框再按 Esc：只取消确认框，编辑区里改了一半的内容不被一起丢掉"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    sync_playwright,
)

ENGINES = ("chromium", "webkit", "firefox")

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "词典 Esc")
    page.goto(BASE + "/#glossary", wait_until="networkidle")
    page.wait_for_timeout(1000)
    editor = page.get_by_label("正确写法", exact=True)
    original = editor.input_value()
    editor.fill(original + "改了一半")
    changed = editor.input_value()
    page.get_by_role("button", name="删除这条").click()
    page.wait_for_timeout(600)
    check("点「删除这条」：弹出确认框", page.locator("[role=alertdialog]").count() == 1)
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    check("按一次 Esc：确认框关了", page.locator("[role=alertdialog]").count() == 0)
    check(
        "按一次 Esc：编辑区改了一半的内容还在",
        page.get_by_label("正确写法", exact=True).input_value() == changed,
        page.get_by_label("正确写法", exact=True).input_value(),
    )
    done(c)
