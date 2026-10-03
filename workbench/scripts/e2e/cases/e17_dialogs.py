"""弹窗：焦点进入、Tab 不出框、背景不滚、Esc 关、关后焦点回触发按钮；点背景：关联会议 / 关联任务 / 选文件夹会关，合并、选来源、新建任务不关；盖层里再开弹窗按 Esc 只关最上面一层"""

from common import (
    BASE,
    check,
    Collector,
    done,
    hash_of,
    launch,
    new_page,
    scroll_to,
    scroll_y,
    settle,
    sync_playwright,
)

DIALOG = """() => { const d = document.querySelector('[role=dialog]'); const a = document.activeElement;
  return {open: !!d, focusInside: !!(d && d.contains(a))} }"""
ACTIVE = "() => document.activeElement && (document.activeElement.innerText || document.activeElement.tagName).trim().slice(0, 30)"


def dialog_open(page) -> bool:
    return page.evaluate(DIALOG)["open"]


def tab_escapes(page, presses: int = 25) -> list[int]:
    escaped = []
    for i in range(presses):
        page.keyboard.press("Tab")
        if not page.evaluate(
            "() => { const d = document.querySelector('[role=dialog]'); return !!(d && d.contains(document.activeElement)) }"
        ):
            escaped.append(i)
    return escaped


def probe(page, name: str, opener, trigger: str, backdrop_closes: bool, fill=None) -> None:
    """opener 打开弹窗；trigger 是触发按钮的文字（关了以后焦点该回到它）"""
    opener()
    page.wait_for_timeout(600)
    opened = page.evaluate(DIALOG)
    check(f"{name}：打开了，焦点在弹窗里", opened["open"] and opened["focusInside"], opened)
    check(f"{name}：连按 25 次 Tab，焦点不出弹窗", tab_escapes(page) == [], tab_escapes(page))
    scroll_to(page, 0)
    page.mouse.move(700, 450)
    page.mouse.wheel(0, 600)
    page.wait_for_timeout(300)
    check(f"{name}：弹窗开着时背景不滚", scroll_y(page) == 0, scroll_y(page))
    if fill:
        fill()
    page.mouse.click(5, 300)
    page.wait_for_timeout(400)
    closed_by_backdrop = not dialog_open(page)
    check(
        f"{name}：点背景{'关' if backdrop_closes else '不关'}",
        closed_by_backdrop == backdrop_closes,
        f"点背景后{'关了' if closed_by_backdrop else '还开着'}",
    )
    if dialog_open(page):
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    check(f"{name}：Esc 能关", not dialog_open(page))
    focus = page.evaluate(ACTIVE)
    check(f"{name}：关了以后焦点回到「{trigger}」", focus and trigger in focus, focus)


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=700)
    c = Collector(page, "弹窗")
    page.on("dialog", lambda d: d.dismiss())

    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.locator("main [role=tab]", has_text="待认领").first.click()
    settle(page, 600)
    probe(
        page,
        "合并到已有需求",
        lambda: page.get_by_role("button", name="合并").first.click(),
        "合并",
        False,
    )

    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(500)
    probe(
        page,
        "新建任务（空表单）",
        lambda: page.get_by_role("button", name="＋ 新建任务").click(),
        "新建任务",
        False,
    )
    probe(
        page,
        "新建任务（写了一半）",
        lambda: page.get_by_role("button", name="＋ 新建任务").click(),
        "新建任务",
        False,
        fill=lambda: page.locator("[role=dialog] input, [role=dialog] textarea").first.fill(
            "写了一半"
        ),
    )

    page.goto(BASE + "/#requirements/req-jd", wait_until="networkidle")
    page.wait_for_timeout(700)
    probe(
        page,
        "关联会议",
        lambda: page.get_by_role("button", name="＋ 关联会议").click(),
        "关联会议",
        True,
    )
    probe(
        page,
        "关联已有任务",
        lambda: page.get_by_role("button", name="关联已有任务").click(),
        "关联已有任务",
        True,
    )
    probe(
        page,
        "选择文件夹",
        lambda: page.get_by_role("button", name="＋ 选择文件夹").click(),
        "选择文件夹",
        True,
    )

    # 盖层（需求修改页）里再开弹窗：Esc 只关最上面一层，页面还在
    page.goto(BASE + "/#requirements/req-jd/edit", wait_until="networkidle")
    page.wait_for_timeout(700)
    probe(
        page,
        "选来源会议",
        lambda: page.get_by_role("button", name="＋ 选来源会议，再挑会上原话").click(),
        "选来源会议",
        False,
    )
    page.get_by_role("button", name="＋ 选来源会议，再挑会上原话").click()
    page.wait_for_timeout(600)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    check(
        "修改页上开着选来源弹窗，按一次 Esc：只关弹窗，还在修改页",
        not dialog_open(page) and hash_of(page) == "#requirements/req-jd/edit",
        (dialog_open(page), hash_of(page)),
    )
    done(c)
