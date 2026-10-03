"""回滚以后版本下拉跟上（接口已经有新版本），逐字稿显示回到所选版本；刷新后一致"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    sync_playwright,
)

MUTATES = True
MEETING = mid("a3a3a3a3")
MARK = "【改过】"


def ui(page):
    inspector = page.locator("aside.edit-inspector")
    return {
        "options": inspector.locator("select").nth(2).locator("option").count(),
        "marked": page.evaluate(
            "m => Array.from(document.querySelectorAll('.transcript-scroll p.segment-text')).filter(p => p.innerText.includes(m)).length",
            MARK,
        ),
    }


def api(page):
    return page.evaluate(
        "async m => { const d = await (await fetch('/api/meetings/' + m)).json(); return {versions: d.transcript_versions.length, marked: d.segments.filter(s => s.text.includes('【改过】')).length} }",
        MEETING,
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "回滚版本")
    page.on("dialog", lambda d: d.accept())
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_timeout(900)
    start = {"ui": ui(page), "api": api(page)}
    check(
        "开始时没有带标记的段落", start["ui"]["marked"] == 0 and start["api"]["marked"] == 0, start
    )
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(400)
    box = page.locator(".transcript-scroll textarea").nth(2)
    box.click()
    page.keyboard.press("End")
    page.keyboard.type(MARK)
    page.get_by_role("button", name="保存草稿").click()
    page.wait_for_timeout(1500)
    saved = {"ui": ui(page), "api": api(page)}
    check(
        "保存后：界面和接口都有带标记的段落，版本多了一个",
        saved["ui"]["marked"] == 1
        and saved["api"]["marked"] == 1
        and saved["api"]["versions"] == start["api"]["versions"] + 1
        and saved["ui"]["options"] == saved["api"]["versions"],
        saved,
    )
    inspector = page.locator("aside.edit-inspector")
    versions = inspector.locator("select").nth(2)
    versions.select_option(index=versions.locator("option").count() - 1)
    inspector.get_by_role("button", name="回滚到所选版本").click()
    page.wait_for_timeout(2500)
    rolled = {"ui": ui(page), "api": api(page)}
    check(
        "回滚后：标记没了（回到所选的最早版本）",
        rolled["ui"]["marked"] == 0 and rolled["api"]["marked"] == 0,
        rolled,
    )
    check(
        "回滚后：版本又多一个，下拉和接口对得上",
        rolled["api"]["versions"] == saved["api"]["versions"] + 1
        and rolled["ui"]["options"] == rolled["api"]["versions"],
        rolled,
    )
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(900)
    check("刷新后界面和回滚后一致", ui(page) == rolled["ui"], (ui(page), rolled["ui"]))
    done(c)
