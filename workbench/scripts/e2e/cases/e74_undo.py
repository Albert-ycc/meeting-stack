"""关系图撤销失败以后这一步还在：撤销请求被掐断 → 失败提示 → ⌘Z 再撤成功；撤销在途时连按 ⌘Z、再点横幅［撤销］只发一次

会真的写隔离库（把一场会拖到别的项目再撤回来）。"""

import re

from common import (
    BASE,
    check,
    Collector,
    csrf_headers,
    done,
    fetch_json,
    hold,
    launch,
    new_page,
    sync_playwright,
)

MUTATES = True
ENGINES = ("chromium", "webkit", "firefox")
UNDO = re.compile(r"/api/meetings/[^/]+/project/undo")
NETWORK_DOWN = "连不上声档服务，检查它是否在运行"


def notice_text(page) -> str:
    return page.evaluate(
        "() => { const n = document.querySelector('.project-graph__notice'); return n ? n.innerText.replace(/\\s+/g, ' ').trim() : '' }"
    )


def notice_role(page):
    return page.evaluate(
        "() => { const n = document.querySelector('.project-graph__notice'); return n ? n.getAttribute('role') : null }"
    )


def meeting_titles(page) -> list[str]:
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.graph-viewport [data-node-id^=\"m:\"]')).map(e => e.getAttribute('aria-label'))"
    )


def drag_first_meeting_to(page, project_id: str) -> None:
    node = page.locator(".graph-viewport [data-node-id^='m:']").first
    box = node.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 40, cy + 40, steps=6)
    page.wait_for_timeout(300)
    dock = page.locator(f"[data-drop-project='{project_id}']").bounding_box()
    page.mouse.move(dock["x"] + dock["width"] / 2, dock["y"] + dock["height"] / 2, steps=10)
    page.wait_for_timeout(200)
    page.mouse.up()
    page.wait_for_timeout(1200)


def restore_moved(page) -> None:
    """把还处在「被移走」状态的会用服务端的撤销接口恢复（撤销期 10 分钟内一直可以）"""
    headers = csrf_headers(page)
    for item in fetch_json(page, "/api/graph/projects/project-yimi?window=all")["moved_out"]:
        page.request.post(
            f"{BASE}/api/meetings/{item['meeting_id']}/project/undo", headers=headers, data={}
        )
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1200)


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "关系图撤销").allow_failed(r"POST \S*/project/undo \(")
    undo_requests: list[str] = []
    page.on(
        "request",
        lambda r: (
            undo_requests.append(r.url) if UNDO.search(r.url) and r.method == "POST" else None
        ),
    )
    page.goto(BASE + "/#projects/project-yimi/graph", wait_until="networkidle")
    page.wait_for_timeout(1200)
    titles_before = meeting_titles(page)
    check("项目图里有会可拖", len(titles_before) >= 1, titles_before)

    # U1：撤销请求被掐断
    drag_first_meeting_to(page, "project-hengrui")
    check(
        "U1 把一场会拖到「恒瑞健康」：出了带［撤销］的横幅",
        "撤销" in notice_text(page),
        notice_text(page),
    )
    page.route(UNDO, lambda route: route.abort())
    page.locator(".project-graph__notice").get_by_role("button", name="撤销").click()
    page.wait_for_timeout(1500)
    check(
        "U1 撤销请求被掐断：失败提示是人话「连不上声档服务……」、role=alert、只发了一次",
        NETWORK_DOWN in notice_text(page)
        and notice_role(page) == "alert"
        and len(undo_requests) == 1,
        (notice_text(page), notice_role(page), len(undo_requests)),
    )
    page.unroute(UNDO)
    page.keyboard.press("ControlOrMeta+z")
    page.wait_for_timeout(1500)
    check(
        "U1 ⌘Z 再撤一次：这一步还在栈里，请求发了第二次并成功",
        len(undo_requests) == 2 and "已撤销刚才的改动" in notice_text(page),
        (len(undo_requests), notice_text(page)),
    )
    restore_moved(page)

    # U2：撤销在途（按住不放）时连按 ⌘Z、再点横幅［撤销］，只发一次
    undo_requests.clear()
    drag_first_meeting_to(page, "project-hengrui")
    held = hold(page, UNDO)
    page.locator(".project-graph__notice").get_by_role("button", name="撤销").click()
    page.wait_for_timeout(500)
    for _ in range(3):
        page.keyboard.press("ControlOrMeta+z")
        page.wait_for_timeout(120)
    banner_button = page.locator(".project-graph__notice").get_by_role("button", name="撤销")
    check(
        "U2 撤销在途、连按三次 ⌘Z：只发了一次，横幅上的［撤销］还在、置灰",
        len(undo_requests) == 1 and banner_button.count() == 1 and banner_button.is_disabled(),
        (len(undo_requests), banner_button.count()),
    )
    for route in held:
        route.continue_()
    held.clear()
    page.unroute(UNDO)
    page.wait_for_timeout(1500)
    check(
        "U2 放行以后：成功，请求数仍是 1",
        len(undo_requests) == 1 and "已撤销" in notice_text(page),
        (len(undo_requests), notice_text(page)),
    )
    restore_moved(page)
    check(
        "结束时会都回到了原来的项目",
        meeting_titles(page) == titles_before,
        (titles_before, meeting_titles(page)),
    )
    c.expect_clean("关系图撤销")
    done()
