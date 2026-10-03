"""项目页「问这个项目」：把材料原文发给 AI 前的确认，默认焦点在「只用会议回答」，回车不会把材料原文发出去

prepare、ask、askJob 三个接口用 page.route 假一份，只看界面，不连真 AI。"""

import json

from common import (
    BASE,
    check,
    Collector,
    done,
    ENGINE,
    launch,
    mid,
    new_page,
    sync_playwright,
)

ENGINES = ("chromium", "webkit", "firefox")


def plan(meetings: int):
    sources = []
    if meetings:
        sources.append(
            {"id": "T1", "kind": "meeting", "meeting_id": mid("a1a1a1a1"), "title": "医米京东科研仓对接", "date": "2026-09-01",
             "start_ms": 12000, "end_ms": 20000, "audio_url": None, "speaker": "张三", "text": "入库单要从系统里推过去", "quote": "入库单要从系统里推过去"}
        )  # fmt: skip
    sources.append(
        {"id": "M1", "kind": "material", "file_id": 1, "name": "报价单.xlsx", "content_key": "k", "ordinal": 1, "loc": "表『预算』", "start_ms": None,
         "playable": False, "root_online": True, "text": "预算表里的总价", "quote": "预算表里的总价"}
    )  # fmt: skip
    return {
        "plan_id": "plan-e2e", "expires_in": 600, "question": "报价最后定了多少？", "counts": {"meetings": meetings, "materials": 1},
        "confirm": {"text": "将发送 1 段材料原文给 api.deepseek.com", "host": "api.deepseek.com"}, "local_model": False, "llm": "ok",
        "highlight": [], "sources": sources, "notes": [], "unattributed_meetings": 0,
    }  # fmt: skip


def active(page):
    return page.evaluate(
        "() => { const a = document.activeElement; return a ? (a.innerText || a.getAttribute('aria-label') || a.tagName).trim() : null }"
    )


def scenario(p, name, meetings, steps):
    b = launch(p)
    ctx, page = new_page(b, height=900)
    c = Collector(page, name)
    asks: list[dict] = []
    page.route(
        "**/api/projects/*/ask/prepare",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(plan(meetings))
        ),
    )

    def on_ask(route):
        asks.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps({"job_id": "j-e2e", "state": "waiting", "text": ""}),
        )

    page.route("**/api/projects/*/ask", on_ask)
    page.route(
        "**/api/ask/j-e2e",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "state": "stopped",
                    "reason": "e2e",
                    "text": "测试到此为止",
                    "retry": False,
                    "sources": [],
                }
            ),
        ),
    )
    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.get_by_role("tab", name="材料").click()
    page.wait_for_timeout(800)
    box = page.get_by_label("问题", exact=True)
    box.fill("报价最后定了多少？")
    box.press("Enter")
    page.wait_for_selector(".ask-confirm", timeout=8000)
    page.wait_for_timeout(500)
    steps(page, asks)
    c.expect_clean(name)
    b.close()


with sync_playwright() as p:

    def with_meetings(page, asks):
        check(
            "有会议段落：弹出确认后焦点在「只用会议回答」",
            active(page) == "只用会议回答",
            active(page),
        )
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        check(
            "有会议段落：直接回车发出去的是不带材料原文的（with_materials=false）",
            [a.get("with_materials") for a in asks] == [False],
            asks,
        )

    scenario(p, "有会议段落-直接回车", 2, with_meetings)

    def tab_back(page, asks):
        check(
            "键盘回到发送：弹出确认后焦点在「只用会议回答」",
            active(page) == "只用会议回答",
            active(page),
        )
        # 问答卡不是弹窗；Safari 默认 Tab 不停在按钮上，要 Option+Tab，其余引擎 Shift+Tab 就够
        page.keyboard.press("Alt+Shift+Tab" if ENGINE == "webkit" else "Shift+Tab")
        check("键盘回到发送：Shift+Tab 回到「发送」", active(page) == "发送", active(page))
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        check(
            "键盘回到发送：回车发出去的带材料原文（with_materials=true）",
            [a.get("with_materials") for a in asks] == [True],
            asks,
        )

    scenario(p, "有会议段落-键盘回到发送", 2, tab_back)

    def materials_only(page, asks):
        check(
            "只有材料段落：弹出确认后焦点在「看看是哪几段」",
            active(page).split("\n")[0].split(" ")[0] == "看看是哪几段",
            active(page),
        )
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        check(
            "只有材料段落：回车展开要发的原文",
            page.locator(".ask-confirm").inner_text().count("预算表里的总价") >= 1,
        )
        check("只有材料段落：回车什么也没发", asks == [], asks)

    scenario(p, "只有材料段落-直接回车", 0, materials_only)
    done()
