"""需求池：详情票根三个数点了滚到对应卡；从需求详情开会再回来；候选丢掉、合并各自能撤销；认领进表单、后退回来"""

import re

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
    scroll_to,
    scroll_y,
    settle,
    sync_playwright,
)

MUTATES = True

UNDO = ".app-toast .app-toast__undo"


def candidates(page) -> int:
    return fetch_json(page, "/api/requirement-pool?status=pending&limit=1")["total"]


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=700)
    c = Collector(page, "需求池")
    page.goto(BASE + "/#requirements/req-jd", wait_until="networkidle")
    page.wait_for_timeout(800)
    for label in ("1 待办", "2 会议", "0 材料"):
        scroll_to(page, 0)
        page.wait_for_timeout(200)
        page.get_by_role("button", name=label).first.click()
        page.wait_for_timeout(1200)
        check(f"票根「{label}」：点了滚到对应的卡", scroll_y(page) > 100, scroll_y(page))

    # 从需求详情打开会议 → 返回按钮写着来处 → 后退回需求详情
    scroll_to(page, 500)
    page.wait_for_timeout(300)
    page.get_by_role("button", name="打开会议").first.click()
    settle(page, 1000)
    back_label = page.evaluate(
        "() => (Array.from(document.querySelectorAll('section.detail-page button')).find(b => b.innerText.includes('返回')) || {}).innerText"
    )
    check(
        "会议页的返回按钮写着「返回需求详情」",
        back_label and "返回需求详情" in back_label,
        back_label,
    )
    page.go_back()
    settle(page, 1000)
    check(
        "后退：回到这条需求的详情",
        hash_of(page) == "#requirements/req-jd" and heading(page) == "京东科研仓对接",
        (hash_of(page), heading(page)),
    )
    # 需求详情里的原话：点了在原地用小播放器放，不跳走（MiniPlayer 的设计）
    quote = page.get_by_role("button", name=re.compile(r"从 \d\d:\d\d 听这条")).first
    check("需求详情里有［从 xx:xx 听这条］", quote.count() == 1)
    quote.click()
    page.wait_for_timeout(1000)
    check(
        "点它：不跳走，原地出现播放器",
        hash_of(page) == "#requirements/req-jd" and page.locator("audio").count() == 1,
        hash_of(page),
    )

    # 候选：丢掉、合并，各自撤销
    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.locator("main [role=tab]", has_text="待认领").first.click()
    settle(page, 600)
    n0 = candidates(page)
    check("待认领里有候选", n0 >= 3, n0)
    page.get_by_role("button", name="丢掉").first.click()
    after_drop = poll(lambda: candidates(page), lambda v: v == n0 - 1)
    check("丢掉一条：候选少 1", after_drop == n0 - 1, (n0, after_drop))
    check(
        "出了带［撤销］的提示",
        poll(lambda: page.locator(UNDO).count(), lambda v: v == 1) == 1,
        notices(page),
    )
    page.locator(UNDO).first.click()
    restored = poll(lambda: candidates(page), lambda v: v == n0)
    check("撤销丢掉：候选回来", restored == n0, (n0, restored))

    page.get_by_role("button", name="合并").first.click()
    page.wait_for_timeout(900)
    dialog = page.locator("[role=dialog]")
    check("点［合并］：弹出「合并到已有需求」", dialogs(page) == ["合并到已有需求"], dialogs(page))
    dialog.locator("label").first.click()
    page.wait_for_timeout(300)
    dialog.get_by_role("button", name="合并", exact=True).click()
    after_merge = poll(lambda: candidates(page), lambda v: v == n0 - 1)
    check("合并进一条需求：候选少 1", after_merge == n0 - 1, (n0, after_merge))
    check(
        "合并之后也有［撤销］",
        poll(lambda: page.locator(UNDO).count(), lambda v: v == 1) == 1,
        notices(page),
    )
    page.locator(UNDO).first.click()
    restored = poll(lambda: candidates(page), lambda v: v == n0)
    check("撤销合并：候选回来", restored == n0, (n0, restored))

    # 认领：进表单，后退回来
    page.locator("main button").filter(has_text=re.compile(r"^认领")).first.click()
    settle(page, 900)
    check("点［认领 →］：进认领表单", "#requirements/claim/" in hash_of(page), hash_of(page))
    page.go_back()
    settle(page, 700)
    check("后退：回到需求池", hash_of(page) == "#requirements", hash_of(page))
    done(c)
