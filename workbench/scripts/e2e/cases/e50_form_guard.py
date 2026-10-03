"""需求新增页、修改页改了没存：浏览器后退、前进、手改地址栏，每次导航只问一次；选留下就留在原地，选离开才走"""

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
FIELD = 'input[placeholder="给这条需求起个名字"]'

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=800)
    c = Collector(page, "表单守卫")
    asked: list[str] = []
    answer = {"accept": False}

    def on_dialog(d):
        asked.append(d.message)
        d.accept() if answer["accept"] else d.dismiss()

    page.on("dialog", on_dialog)

    def step(label, hash_, field=None, asks=0):
        """asks 是这一步该多问几次"""
        state = page.evaluate(
            f"() => [location.hash, document.querySelector('{FIELD}')?.value ?? null]"
        )
        check(f"{label}：落在 {hash_}", state[0] == hash_, state)
        if field is not None:
            check(f"{label}：输入框里是「{field}」", state[1] == field, state)
        check(
            f"{label}：这一步问了 {asks} 次（累计 {len(asked)}）",
            len(asked) == step.total + asks,
            asked,
        )
        step.total += asks

    step.total = 0

    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(600)
    page.get_by_role("button", name="新建需求").click()
    page.wait_for_timeout(600)
    page.locator(FIELD).fill("守卫试探")
    step("新增页改了没存", "#requirements/new", "守卫试探")

    answer["accept"] = False
    page.go_back()
    page.wait_for_timeout(800)
    step("后退 → 选留下", "#requirements/new", "守卫试探", asks=1)
    page.go_back()
    page.wait_for_timeout(800)
    step("再后退一次 → 仍选留下", "#requirements/new", "守卫试探", asks=1)

    answer["accept"] = True
    page.go_back()
    page.wait_for_timeout(800)
    step("后退 → 选离开", "#requirements", asks=1)

    page.go_forward()
    page.wait_for_timeout(800)
    step("前进回新增页（空白表单，不问）", "#requirements/new", "")
    page.go_back()
    page.wait_for_timeout(800)
    step("没改过再后退（不问）", "#requirements")

    # 修改页
    page.evaluate("() => { location.hash = '#requirements/req-jd/edit' }")
    page.wait_for_timeout(1000)
    page.locator(FIELD).fill("京东仓改了一半")
    step("修改页改了没存", "#requirements/req-jd/edit", "京东仓改了一半")
    answer["accept"] = False
    page.go_back()
    page.wait_for_timeout(800)
    step("修改页后退 → 选留下", "#requirements/req-jd/edit", "京东仓改了一半", asks=1)
    answer["accept"] = True
    page.go_back()
    page.wait_for_timeout(800)
    step("修改页后退 → 选离开", "#requirements", asks=1)
    page.go_forward()
    page.wait_for_timeout(800)
    step("前进回修改页（不问）", "#requirements/req-jd/edit")

    # 手改地址栏：改了没存时把 hash 手改成别处
    page.locator(FIELD).fill("手改地址栏")
    answer["accept"] = False
    page.evaluate("() => { location.hash = '#tasks' }")
    page.wait_for_timeout(800)
    step("手改地址栏去 #tasks → 选留下", "#requirements/req-jd/edit", "手改地址栏", asks=1)
    check(
        "问的话是「当前需求仍有未保存修改。放弃这些修改并离开吗？」",
        set(asked) == {"当前需求仍有未保存修改。放弃这些修改并离开吗？"},
        set(asked),
    )
    done(c)
