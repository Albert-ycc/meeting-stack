"""改说话人名（应用到全部段落）、回滚到所选版本：静默刷新（不卸载、逐字稿滚动保留），名字按文字显示，回滚生成新版本"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    notices,
    scroll_to,
    sync_playwright,
)

MUTATES = True
MEETING = mid("a1a1a1a1")
NAME = "张经理<script>"

STATE = """() => { const d = document.querySelector('section.detail-page'); const t = document.querySelector('.transcript-scroll');
  return {alive: !!(d && d.__mark === 42), transcript: t ? Math.round(t.scrollTop) : null,
          speakers: Array.from(new Set(Array.from(document.querySelectorAll('.segment-speaker')).map(e => e.innerText)))} }"""


def version_count(page) -> int:
    return page.evaluate(
        "async m => (await (await fetch('/api/meetings/' + m)).json()).transcript_versions.length",
        MEETING,
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "改说话人和回滚")
    page.on("dialog", lambda d: d.accept())
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_timeout(900)
    page.evaluate("() => { document.querySelector('section.detail-page').__mark = 42 }")
    scroll_to(page, 400)
    page.evaluate("() => { document.querySelector('.transcript-scroll').scrollTop = 800 }")
    page.wait_for_timeout(300)
    page.locator(".detail-tabs button", has_text="逐字稿").first.click()
    before = page.evaluate(STATE)
    versions_before = version_count(page)
    inspector = page.locator("aside.edit-inspector")
    inspector.locator("select").nth(1).select_option("SPEAKER_00")
    inspector.get_by_placeholder("新的显示名称").fill(NAME)
    inspector.get_by_role("button", name="应用到全部段落").click()
    page.wait_for_timeout(1500)
    renamed = page.evaluate(STATE)
    check("改说话人名：页面没卸载重建", renamed["alive"], renamed)
    # 名字变长、行高会跟着变，滚动位置允许差不到一行；要抓的是回到顶上或跳走
    check(
        "改说话人名：逐字稿滚动位置基本不变",
        abs(renamed["transcript"] - before["transcript"]) <= 40,
        (before["transcript"], renamed["transcript"]),
    )
    check(
        "新名字立刻反映到逐字稿里，而且是按文字显示的（尖括号原样）",
        NAME in renamed["speakers"] and page.locator(".segment-speaker script").count() == 0,
        renamed["speakers"],
    )
    after_rename = version_count(page)

    versions = inspector.locator("select").nth(2)
    options = versions.locator("option").all_inner_texts()
    check("版本下拉里有可以回滚的历史版本", len(options) >= 2, options)
    versions.select_option(index=len(options) - 1)
    inspector.get_by_role("button", name="回滚到所选版本").click()
    page.wait_for_timeout(1500)
    check(
        "回滚：出了「已回滚逐字稿工作版本」的提示",
        any("已回滚逐字稿工作版本" in text for text in notices(page)),
        notices(page),
    )
    rolled = page.evaluate(STATE)
    check("回滚：页面没卸载重建", rolled["alive"], rolled)
    check(
        "回滚：生成了一个新版本，版本下拉跟上",
        version_count(page) == after_rename + 1
        and inspector.locator("select").nth(2).locator("option").count() == len(options) + 1,
        (after_rename, version_count(page)),
    )
    done(c)
