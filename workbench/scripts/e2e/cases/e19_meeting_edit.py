"""会议详情：编辑逐字稿 → 保存草稿后静默刷新（页面不卸载，逐字稿滚动、页签、播放进度都在），改动落库；切页签"""

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
EDIT_MARK = "【改过】"

STATE = """() => { const d = document.querySelector('section.detail-page'); const a = document.querySelector('audio'); const t = document.querySelector('.transcript-scroll');
  return {alive: !!(d && d.__mark === 42), transcript: t ? Math.round(t.scrollTop) : null, audio: a ? Math.round(a.currentTime * 10) / 10 : null,
          tab: (document.querySelector('.detail-tabs [aria-selected=true], .detail-tabs .active') || {}).innerText} }"""


def saved_segments(page):
    return page.evaluate(
        "async m => { const d = await (await fetch('/api/meetings/' + m)).json(); return {versions: d.transcript_versions.length, marked: d.segments.filter(s => s.text.includes('【改过】')).length} }",
        MEETING,
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "会议详情编辑")
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_timeout(900)
    page.evaluate(
        "() => { document.querySelector('section.detail-page').__mark = 42; document.querySelector('audio').currentTime = 40 }"
    )
    base = saved_segments(page)
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(500)
    scroll_to(page, 500)
    page.evaluate("() => { document.querySelector('.transcript-scroll').scrollTop = 600 }")
    page.wait_for_timeout(300)
    box = page.locator(".transcript-scroll textarea").nth(20)
    box.scroll_into_view_if_needed()
    box.click()
    page.keyboard.press("End")
    page.keyboard.type(EDIT_MARK)
    before = page.evaluate(STATE)
    page.get_by_role("button", name="保存草稿").click()
    page.wait_for_timeout(1500)
    after = page.evaluate(STATE)
    check("保存草稿：页面还是原来那一份，没卸载重建", after["alive"], after)
    check(
        "保存草稿：逐字稿滚动位置不变",
        abs(after["transcript"] - before["transcript"]) <= 2,
        (before["transcript"], after["transcript"]),
    )
    check(
        "保存草稿：播放进度不变",
        before["audio"] == after["audio"] == 40,
        (before["audio"], after["audio"]),
    )
    check("保存草稿：页签不变", after["tab"] == before["tab"], (before["tab"], after["tab"]))
    check(
        "保存草稿：出了「已保存」的提示",
        any("逐字稿已保存" in text for text in notices(page)),
        notices(page),
    )
    saved = saved_segments(page)
    check(
        "改动落了库：多一个逐字稿版本，带着标记的那段在",
        saved["versions"] == base["versions"] + 1 and saved["marked"] == 1,
        (base, saved),
    )

    for name in ("会议纪要", "本场任务", "逐字稿"):
        page.locator(".detail-tabs button", has_text=name).first.click()
        page.wait_for_timeout(500)
        selected = page.evaluate(STATE)["tab"] or ""
        check(f"切到「{name}」页签", selected.startswith(name), selected)
    done(c)
