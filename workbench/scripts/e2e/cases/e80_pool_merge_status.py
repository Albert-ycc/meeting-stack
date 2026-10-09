"""需求池：票根「⋯」不被裁、不进详情；并入其他需求能撤销、旧链接跳到主需求；标记完成一起关掉待办、撤销还原；已完成盖章写完成日；详情页重新打开"""

from datetime import datetime
from zoneinfo import ZoneInfo

from common import (
    BASE,
    check,
    Collector,
    dialogs,
    done,
    fetch_json,
    hash_of,
    heading,
    launch,
    new_page,
    notices,
    poll,
    settle,
    sync_playwright,
)

MUTATES = True

UNDO = ".app-toast .app-toast__undo"
OPEN = ("pending_confirm", "confirmed", "in_progress")


def poster(page, title: str):
    return page.get_by_role("article", name=f"需求：{title}")


def open_menu(page, title: str):
    poster(page, title).get_by_role("button", name=f"更多操作：{title}").click()
    page.wait_for_timeout(300)


def menu_items(page) -> list[str]:
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('[role=menuitem]')).map(e => e.innerText.trim())"
    )


def open_tasks(page, requirement_id: str) -> int:
    detail = fetch_json(page, f"/api/requirements/{requirement_id}")
    return sum(1 for task in detail["tasks"] if task["status"] in OPEN)


def status_code(page, path: str) -> int:
    return page.evaluate("async p => (await fetch(p)).status", path)


def wall(page, tab: str = "进行中") -> None:
    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.locator("main [role=tab]", has_text=tab).first.click()
    page.wait_for_timeout(800)


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, width=1440, height=900)
    c = Collector(page, "需求池并入与状态")
    # 被并掉的需求，旧链接打开时详情接口回 404（带 merged_into），和它并行取的「复制给 Claude Code」上下文也是 404，都是预期的
    c.allow(r"^404 GET /api/requirements/req-ht(/context)?$")

    # 1. 票根「⋯」：最右一列的菜单完整显示、不被下一排盖住，点开不进详情
    wall(page)
    titles = page.evaluate(
        "() => Array.from(document.querySelectorAll('article[aria-label^=\"需求：\"]')).map(e => e.getAttribute('aria-label').slice(3))"
    )
    rightmost = max(
        titles,
        key=lambda t: poster(page, t).bounding_box()["x"] if poster(page, t).bounding_box() else -1,
    )
    open_menu(page, rightmost)
    check("「⋯」点开：没进详情", hash_of(page) == "#requirements", hash_of(page))
    check("进行中的卡：菜单是 标记完成 / 搁置 / 并入其他需求…", menu_items(page) == ["标记完成", "搁置", "并入其他需求…"], menu_items(page))
    covered = page.evaluate(
        """() => {
          const items = Array.from(document.querySelectorAll('[role=menuitem]'));
          return items.map(item => {
            const r = item.getBoundingClientRect();
            const inView = r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
            const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return inView && (top === item || item.contains(top));
          });
        }"""
    )
    check(f"最右一列（{rightmost}）的菜单项都在视口里、最上层就是它", covered and all(covered), covered)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    check("Esc 收起菜单", not menu_items(page))

    # 2. 并入：赠药横跳拦截 → 京东科研仓对接，撤销后回到墙上
    before_tasks = len(fetch_json(page, "/api/requirements/req-jd")["tasks"])
    ht_tasks = len(fetch_json(page, "/api/requirements/req-ht")["tasks"])
    open_menu(page, "赠药横跳拦截")
    page.get_by_role("menuitem", name="并入其他需求…").click()
    page.wait_for_timeout(500)
    check("弹窗：把「赠药横跳拦截」并入", any("赠药横跳拦截" in d for d in dialogs(page)), dialogs(page))
    radios = page.evaluate(
        "() => Array.from(document.querySelectorAll('[role=radiogroup] [role=radio]')).map(e => e.innerText.replace(/\\n/g, ' '))"
    )
    check("只列同项目（医米）的其他需求，不列自己、不列别的项目", radios and all("赠药横跳拦截" not in r and "亲友积分" not in r for r in radios), radios)
    page.get_by_role("radio", name="京东科研仓对接").click()
    page.get_by_role("button", name="并入", exact=True).click()
    poll(lambda: notices(page), lambda n: any("已并入「京东科研仓对接」" in x for x in n))
    check("并入后提示「已并入『京东科研仓对接』」", any("已并入「京东科研仓对接」" in x for x in notices(page)), notices(page))
    check("这条从墙上消失", poll(lambda: poster(page, "赠药横跳拦截").count(), lambda n: n == 0) == 0)
    after = fetch_json(page, "/api/requirements/req-jd")
    check("待办带到主需求", len(after["tasks"]) == before_tasks + ht_tasks, (len(after["tasks"]), before_tasks, ht_tasks))
    page.locator(UNDO).first.click()
    check("撤销并入：回到墙上", poll(lambda: poster(page, "赠药横跳拦截").count(), lambda n: n == 1) == 1)
    check("撤销后主需求的待办数回到原样", len(fetch_json(page, "/api/requirements/req-jd")["tasks"]) == before_tasks)
    check("撤销后这条又能打开", status_code(page, "/api/requirements/req-ht") == 200)

    # 3. 再并一次不撤销：旧链接显示已并入，［去看］到主需求
    settle(page, 11000)  # 等上一条提示走掉
    open_menu(page, "赠药横跳拦截")
    page.get_by_role("menuitem", name="并入其他需求…").click()
    page.wait_for_timeout(400)
    page.get_by_role("radio", name="京东科研仓对接").click()
    page.get_by_role("button", name="并入", exact=True).click()
    poll(lambda: poster(page, "赠药横跳拦截").count(), lambda n: n == 0)
    page.goto(BASE + "/#requirements/req-ht", wait_until="networkidle")
    page.wait_for_timeout(800)
    text = page.locator("main").inner_text()
    check("旧链接：显示「这条需求已并入『京东科研仓对接』」", "这条需求已并入「京东科研仓对接」" in text, text[:200])
    page.get_by_role("button", name="去看").click()
    page.wait_for_timeout(800)
    check("［去看］到主需求", hash_of(page) == "#requirements/req-jd" and heading(page) == "京东科研仓对接", (hash_of(page), heading(page)))

    # 4. 标记完成：有没做完的待办 → 确认弹窗，默认一起关掉；撤销后待办回来
    wall(page)
    left = open_tasks(page, "req-jd")
    check("主需求名下有没做完的待办（造数）", left > 0, left)
    open_menu(page, "京东科研仓对接")
    page.get_by_role("menuitem", name="标记完成").click()
    page.wait_for_timeout(500)
    check(f"弹「还有 {left} 条待办没做完」", any(f"还有 {left} 条待办没做完" in d for d in dialogs(page)), dialogs(page))
    focused = page.evaluate("() => document.activeElement && document.activeElement.innerText")
    check("默认焦点在「一起关掉」", focused and "一起关掉" in focused, focused)
    page.keyboard.press("Enter")
    poll(lambda: poster(page, "京东科研仓对接").count(), lambda n: n == 0)
    check("完成后离开进行中墙", poster(page, "京东科研仓对接").count() == 0)
    check("待办一起关掉了", open_tasks(page, "req-jd") == 0, open_tasks(page, "req-jd"))
    check("提示带一起关掉的条数", any(f"一起关掉 {left} 条待办" in x for x in notices(page)), notices(page))
    page.locator(UNDO).first.click()
    check("撤销：卡片回到进行中墙", poll(lambda: poster(page, "京东科研仓对接").count(), lambda n: n == 1) == 1)
    check("撤销：待办回到没做完", poll(lambda: open_tasks(page, "req-jd"), lambda n: n == left) == left)

    # 5. 再完成一次，看已完成页签：盖章、写北京日历的完成日
    settle(page, 11000)
    open_menu(page, "京东科研仓对接")
    page.get_by_role("menuitem", name="标记完成").click()
    page.wait_for_timeout(400)
    page.get_by_role("button", name="一起关掉").click()
    poll(lambda: poster(page, "京东科研仓对接").count(), lambda n: n == 0)
    wall(page, "已完成")
    card = poster(page, "京东科研仓对接").inner_text()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m-%d")
    check("已完成页签：盖「已完成」章", "已完成" in card, card[:120])
    check(f"票根写「{today} 完成」", f"{today} 完成" in card, card[-80:])
    first = page.evaluate("() => (document.querySelector('article[aria-label^=\"需求：\"]') || {}).getAttribute('aria-label')")
    check("刚完成的排在已完成页签最前", first == "需求：京东科研仓对接", first)
    open_menu(page, "京东科研仓对接")
    check("已完成的卡：菜单是 重新打开 / 并入其他需求…", menu_items(page) == ["重新打开", "并入其他需求…"], menu_items(page))
    page.keyboard.press("Escape")

    # 6. 详情页：已完成的给［重新打开］，进行中的给［标记完成］和「⋯」里的搁置、并入
    page.goto(BASE + "/#requirements/req-five", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.get_by_role("button", name="重新打开").click()
    check("详情页重新打开：状态回到进行中", poll(lambda: fetch_json(page, "/api/requirements/req-five")["status"], lambda s: s == "active") == "active")
    page.goto(BASE + "/#requirements/req-jf", wait_until="networkidle")
    page.wait_for_timeout(800)
    check("进行中的详情页有［标记完成］", page.get_by_role("button", name="标记完成").count() == 1)
    page.get_by_role("button", name="更多操作", exact=True).click()
    page.wait_for_timeout(300)
    check("详情页「⋯」：搁置 / 并入其他需求…", menu_items(page) == ["搁置", "并入其他需求…"], menu_items(page))
    page.keyboard.press("Escape")

    b.close()
    done(c)
