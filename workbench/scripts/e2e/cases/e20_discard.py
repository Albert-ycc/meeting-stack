"""没保存的逐字稿改动：退出编辑不丢（再进来还在）；离开会议时问一次，点取消就留下、点确定才走"""

from common import (
    BASE,
    check,
    Collector,
    dialogs,
    done,
    hash_of,
    launch,
    mid,
    nav,
    new_page,
    sync_playwright,
)

LEAVE_CONFIRM = "当前会议仍有未保存修改。放弃这些修改并离开吗？"
MEETING = mid("a2a2a2a2")

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "未保存的逐字稿")
    natives: list[str] = []
    answer = {"accept": False}
    page.on(
        "dialog",
        lambda d: (natives.append(d.message), d.accept() if answer["accept"] else d.dismiss()),
    )
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_timeout(900)
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(500)
    box = page.locator(".transcript-scroll textarea").nth(3)
    original = box.input_value()
    box.click()
    page.keyboard.press("End")
    page.keyboard.type("XYZ未保存")
    edited = box.input_value()
    page.get_by_role("button", name="退出编辑").click()
    page.wait_for_timeout(600)
    check(
        "退出编辑：不弹确认（只是切回阅读）",
        natives == [] and dialogs(page) == [],
        (natives, dialogs(page)),
    )
    shown = page.evaluate(
        "() => document.querySelectorAll('.transcript-scroll p.segment-text')[3]?.innerText"
    )
    check("退出编辑之后阅读视图里还是改过的字，没丢", shown == edited, (shown, edited))
    page.get_by_role("button", name="编辑逐字稿").click()
    page.wait_for_timeout(500)
    check(
        "再进编辑：改动还在",
        page.locator(".transcript-scroll textarea").nth(3).input_value() == edited,
    )
    check("改动和原文确实不一样（防这条检查空转）", edited != original)

    # 离开：有未保存改动先问一次
    nav(page, "词典", 800)
    check(f"点侧栏离开：问一次「{LEAVE_CONFIRM}」", natives == [LEAVE_CONFIRM], natives)
    check(
        "点了取消：还留在这场会，改动还在",
        hash_of(page) == "#meetings/" + MEETING
        and page.locator(".transcript-scroll textarea").nth(3).input_value() == edited,
        hash_of(page),
    )
    answer["accept"] = True
    natives.clear()
    nav(page, "词典", 800)
    check(
        "点了确定：离开到词典",
        hash_of(page) == "#glossary" and natives == [LEAVE_CONFIRM],
        (hash_of(page), natives),
    )
    done(c)
