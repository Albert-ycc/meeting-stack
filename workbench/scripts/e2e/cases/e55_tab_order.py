"""弹窗里连按 Tab / Shift+Tab：焦点不跑出弹窗（七个弹窗，各按 8~18 下再按回来）"""

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
DESCRIBE = """() => {
  const a = document.activeElement;
  if (!a) return null;
  const dialogs = Array.from(document.querySelectorAll('[role=dialog],[role=alertdialog]'));
  const top = dialogs[dialogs.length - 1] || null;
  const name = a.getAttribute('aria-label') || a.getAttribute('placeholder') || (a.innerText || '').trim().slice(0, 14) || a.getAttribute('name') || '';
  const type = a.tagName === 'INPUT' ? '[' + a.type + ']' : '';
  return { d: a.tagName.toLowerCase() + type + ':' + name.replace(/\\s+/g, ' '), inside: !!(top && top.contains(a)) };
}"""


def record(page, name, steps=14):
    escaped = []
    forward = []
    for direction in ("Tab", "Shift+Tab"):
        for i in range(steps):
            page.keyboard.press(direction)
            info = page.evaluate(DESCRIBE)
            if direction == "Tab":
                forward.append(info["d"] if info else "?")
            if info and not info["inside"]:
                escaped.append((direction, i, info["d"]))
    check(f"{name}：Tab、Shift+Tab 各按 {steps} 下，焦点没跑出弹窗", not escaped, escaped[:3])
    print("   正向：", " → ".join(forward[:8]))


def open_and_record(page, name, opener, wait=700, steps=14):
    opener()
    page.wait_for_timeout(wait)
    page.wait_for_selector("[role=dialog],[role=alertdialog]", timeout=8000)
    record(page, name, steps)
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    if page.locator("[role=dialog],[role=alertdialog]").count():
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "Tab 顺序")
    page.on("dialog", lambda d: d.dismiss())

    page.goto(BASE + "/#glossary", wait_until="networkidle")
    page.wait_for_timeout(800)
    open_and_record(
        page,
        "危险确认框（词典删除术语）",
        lambda: page.get_by_role("button", name="删除『EDC』").click(),
        steps=8,
    )

    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(800)
    open_and_record(
        page, "新建任务", lambda: page.get_by_role("button", name="＋ 新建任务").click()
    )

    page.goto(BASE + "/#projects", wait_until="networkidle")
    page.wait_for_timeout(800)
    open_and_record(
        page,
        "新建项目",
        lambda: page.get_by_role("button", name="＋ 新建项目").click(),
        wait=1500,
        steps=18,
    )

    page.get_by_role("button", name="＋ 新建项目").click()
    page.wait_for_timeout(1200)
    page.get_by_role("button", name="＋ 选别的文件夹").click()
    page.wait_for_timeout(900)
    record(page, "新建项目上的取径器", 12)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.locator("main [role=tab]", has_text="待认领").first.click()
    page.wait_for_timeout(800)
    open_and_record(
        page,
        "合并到已有需求",
        lambda: page.get_by_role("button", name="合并").first.click(),
        wait=900,
    )

    page.goto(BASE + "/#requirements/req-jd", wait_until="networkidle")
    page.wait_for_timeout(900)
    open_and_record(
        page,
        "关联已有任务",
        lambda: page.get_by_role("button", name="关联已有任务").click(),
        wait=900,
    )

    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(800)
    open_and_record(
        page,
        "任务抽屉",
        lambda: page.get_by_text("下周三前定方案", exact=False).first.click(),
        wait=900,
    )
    done(c)
