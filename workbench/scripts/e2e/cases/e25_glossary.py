"""词典：新增、重名提示、空白名不能提交、编辑、删除（二次确认），删完不再去取这条术语"""

import re

from common import (
    BASE,
    check,
    Collector,
    dialogs,
    done,
    fetch_json,
    launch,
    new_page,
    notices,
    poll,
    sync_playwright,
)

MUTATES = True


def terms(page) -> list[str]:
    data = fetch_json(page, "/api/glossary/terms")
    items = data.get("items", data) if isinstance(data, dict) else data
    return [item["term"] for item in items]


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "词典")
    page.goto(BASE + "/#glossary", wait_until="networkidle")
    page.wait_for_timeout(600)

    page.get_by_role("button", name="新增术语").first.click()
    page.wait_for_timeout(400)
    page.get_by_label("正确写法").fill("测试术语E2E")
    page.get_by_label("添加错写").fill("测试树语")
    page.get_by_label("添加错写").press("Enter")
    page.get_by_role("button", name="加入词典").click()
    check("新增：术语进了词典", poll(lambda: "测试术语E2E" in terms(page)), terms(page))
    check(
        "新增：出了「已加入词典」的提示",
        any("已加入词典" in t for t in notices(page)),
        notices(page),
    )

    # 重名
    page.get_by_role("button", name="新增术语").first.click()
    page.wait_for_timeout(400)
    page.get_by_label("正确写法").fill("测试术语E2E")
    page.get_by_role("button", name="加入词典").click()
    page.wait_for_timeout(900)
    check(
        "重名：词典里还是只有一条",
        terms(page).count("测试术语E2E") == 1,
        terms(page).count("测试术语E2E"),
    )
    check(
        "重名：提示「已在 … 词典」，让把新错写加到那条",
        any("已在" in t and "词典" in t for t in notices(page)),
        notices(page),
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 空白名
    page.get_by_role("button", name="新增术语").first.click()
    page.wait_for_timeout(400)
    page.get_by_label("正确写法").fill("   ")
    check(
        "正确写法只有空格：［加入词典］点不了",
        not page.get_by_role("button", name="加入词典").is_enabled(),
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 编辑
    page.get_by_label("搜索术语").fill("测试术语")
    page.wait_for_timeout(500)
    page.locator("main [role=option]", has_text="测试术语E2E").first.click()
    page.wait_for_timeout(500)
    page.get_by_label("正确写法").fill("测试术语E2E改")
    page.get_by_role("button", name="保存修改").click()
    after = poll(lambda: terms(page), lambda v: "测试术语E2E改" in v)
    check(
        "编辑：改名后词典里是新名字、没有旧名字",
        "测试术语E2E改" in after and "测试术语E2E" not in after,
        after,
    )

    # 删除：二次确认，确认后不再去取这条术语
    c.bad_status.clear()
    page.get_by_role("button", name="删除这条").first.click()
    page.wait_for_timeout(600)
    check(
        "删除：弹出二次确认，写明删除后无法撤销",
        dialogs(page) == ["删除术语「测试术语E2E改」？"]
        and "删除后无法撤销" in page.locator("[role=alertdialog]").inner_text(),
        dialogs(page),
    )
    page.locator("[role=alertdialog]").get_by_role("button", name="删除").last.click()
    after = poll(lambda: terms(page), lambda v: "测试术语E2E改" not in v)
    check("删除：术语没了", "测试术语E2E改" not in after, after)
    page.wait_for_timeout(1000)
    check(
        "删除之后页面不再去取这条术语（没有 404）",
        not [s for s in c.bad_status if re.search(r"^404 GET .*/api/glossary/terms/", s)],
        c.bad_status,
    )
    done(c)
