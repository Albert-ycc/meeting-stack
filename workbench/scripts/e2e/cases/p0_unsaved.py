"""会议页没保存的纪要和逐字稿：保存另一侧后还在、离开时被拦、跨手机断点（窗口缩到 700 再放回）也不丢"""

from common import (
    BASE,
    check,
    Collector,
    done,
    hash_of,
    launch,
    mid,
    nav,
    new_page,
    sync_playwright,
)

MUTATES = True
MARK_M = "【纪要未保存的一段】"
MARK_T = "【逐字稿未保存】"


def tab(page, name):
    page.locator(".detail-tabs button", has_text=name).first.click()
    page.wait_for_timeout(400)


def minutes_value(page):
    box = page.locator("textarea.minutes-editor")
    return box.input_value() if box.count() else None


def transcript_has(page, text):
    return page.evaluate(
        "t => Array.from(document.querySelectorAll('.transcript-scroll textarea')).some(x => x.value.includes(t))",
        text,
    )


def api_detail(page, meeting):
    return page.evaluate(
        """async m => { const d = await (await fetch('/api/meetings/' + m)).json(); const cur = (d.minutes_versions || []).find(v => v.id === d.current_minutes_version_id);
        return { minutes: cur ? cur.markdown : null, segs: d.segments.map(s => s.text).join('|') } }""",
        meeting,
    )


def open_meeting(b, meeting, name):
    ctx, page = new_page(b, height=900)
    c = Collector(page, name)
    natives: list[str] = []
    page.on("dialog", lambda d: (natives.append(d.message), d.dismiss()))
    page.goto(BASE + "/#meetings/" + meeting, wait_until="networkidle")
    page.wait_for_timeout(900)
    page.evaluate("() => { document.querySelector('section.detail-page').__mark = 42 }")
    return ctx, page, c, natives


def alive(page):
    return page.evaluate("() => document.querySelector('section.detail-page')?.__mark === 42")


with sync_playwright() as p:
    b = launch(p)

    # 1. 纪要改了没保存 → 保存逐字稿 → 纪要编辑器和改动还在；离开被拦；再保存纪要两边都落库
    meeting = mid("a1a1a1a1")
    ctx, page, c, natives = open_meeting(b, meeting, "保存逐字稿不丢纪要")
    tab(page, "会议纪要")
    page.get_by_role("button", name="编辑纪要").click()
    page.wait_for_timeout(300)
    page.locator("textarea.minutes-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(MARK_M)
    tab(page, "逐字稿")
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(400)
    page.locator(".transcript-scroll textarea").nth(2).click()
    page.keyboard.press("End")
    page.keyboard.type("【已保存的逐字稿改动】")
    page.get_by_role("button", name="保存草稿").click()
    page.wait_for_timeout(1800)
    tab(page, "会议纪要")
    check(
        "保存逐字稿后：纪要编辑器还开着，没保存的改动还在",
        MARK_M in (minutes_value(page) or ""),
        minutes_value(page) and minutes_value(page)[-30:],
    )
    nav(page, "词典管理", 500)
    check(
        "纪要还有没保存的改动时点侧栏：被拦下，还在这场会",
        any("未保存" in m for m in natives) and "#meetings/" in hash_of(page),
        (natives, hash_of(page)),
    )
    page.get_by_role("button", name="保存纪要草稿").click()
    page.wait_for_timeout(1800)
    detail = api_detail(page, meeting)
    check(
        "再保存纪要：纪要和逐字稿两边都落了库，没有出冲突面板",
        MARK_M in (detail["minutes"] or "")
        and "【已保存的逐字稿改动】" in detail["segs"]
        and page.locator("text=有更新版本").count() == 0,
        page.locator("text=有更新版本").count(),
    )
    check("整个过程页面没被卸载重建", alive(page))
    c.expect_clean("保存逐字稿不丢纪要")
    ctx.close()

    # 2. 逐字稿改了没保存 → 保存纪要 → 逐字稿编辑态和改动还在，库里逐字稿没有这处
    meeting = mid("d3d3d3d3")
    ctx, page, c, natives = open_meeting(b, meeting, "保存纪要不丢逐字稿")
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(400)
    page.locator(".transcript-scroll textarea").nth(1).click()
    page.keyboard.press("End")
    page.keyboard.type(MARK_T)
    tab(page, "会议纪要")
    page.get_by_role("button", name="编辑纪要").click()
    page.wait_for_timeout(300)
    page.locator("textarea.minutes-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type("【已保存的纪要改动】")
    page.get_by_role("button", name="保存纪要草稿").click()
    page.wait_for_timeout(1800)
    tab(page, "逐字稿")
    detail = api_detail(page, meeting)
    check("保存纪要后：逐字稿编辑态和没保存的改动还在", transcript_has(page, MARK_T))
    check(
        "纪要落了库，逐字稿的未保存改动没有被悄悄写进去",
        "【已保存的纪要改动】" in (detail["minutes"] or "") and MARK_T not in detail["segs"],
        detail["segs"][-40:],
    )
    check("整个过程页面没被卸载重建", alive(page))
    c.expect_clean("保存纪要不丢逐字稿")
    ctx.close()

    # 3. 两边都改了没保存，窗口缩到 700 再放回 1440：两边都还在
    ctx, page, c, natives = open_meeting(b, meeting, "跨手机断点不丢编辑")
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(400)
    page.locator(".transcript-scroll textarea").nth(1).click()
    page.keyboard.press("End")
    page.keyboard.type(MARK_T)
    tab(page, "会议纪要")
    page.get_by_role("button", name="编辑纪要").click()
    page.wait_for_timeout(300)
    page.locator("textarea.minutes-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(MARK_M)
    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_timeout(700)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(700)
    check("窗口缩到 700 再放回 1440：纪要的未保存改动还在", MARK_M in (minutes_value(page) or ""))
    tab(page, "逐字稿")
    check("窗口缩到 700 再放回 1440：逐字稿的未保存改动还在", transcript_has(page, MARK_T))
    check("整个过程页面没被卸载重建", alive(page))
    c.expect_clean("跨手机断点不丢编辑")
    ctx.close()
    done()
