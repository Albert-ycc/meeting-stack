"""空态和中转不可用：空项目、需求池和待办搜不到、转写录音页不露本机路径、工作台转写卡片不谎报「没有正在处理」"""

import re

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    sync_playwright,
)

LEAK = re.compile(
    r"/Volumes|/Users/|/private/|外置中枢|iphone-relay|relay-none|meeting-stack|workbench-e2e"
)
TEXT = "() => (document.querySelector('main') || document.body).innerText.replace(/\\n+/g, ' | ')"

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "空态")
    page.goto(BASE + "/#projects/project-empty", wait_until="networkidle")
    page.wait_for_timeout(1500)
    text = page.evaluate(TEXT)
    check(
        "空项目：写着「0 场会」「本项目没有进行中的需求」「没有未挂需求的任务」",
        all(s in text for s in ("0 场会", "本项目没有进行中的需求", "没有未挂需求的任务")),
        text[:160],
    )

    page.goto(BASE + "/#requirements", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.get_by_placeholder("搜需求名称").fill("不存在的需求zzz")
    page.wait_for_timeout(900)
    text = page.evaluate(TEXT)
    check(
        "需求池搜不到：写着「没有符合筛选条件的需求」，有［清空筛选］",
        "没有符合筛选条件的需求" in text
        and page.locator("main button", has_text="清空筛选").count() >= 1,
        text[-80:],
    )
    page.get_by_placeholder("搜需求名称").fill("")

    page.goto(BASE + "/#tasks", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.get_by_placeholder("输入任务名称").fill("zzz不存在")
    page.get_by_role("button", name="查询").click()
    page.wait_for_timeout(800)
    text = page.evaluate(TEXT)
    check(
        "待办查不到：各栏写着「没有逾期的任务」「之后没有到期的任务」，任务名一个都不在",
        "没有逾期的任务" in text
        and "之后没有到期的任务" in text
        and "京东科研仓" not in text.split("任务名称")[-1],
        text[-120:],
    )
    page.get_by_role("button", name="重置").click()
    page.wait_for_timeout(500)

    page.goto(BASE + "/#jobs", wait_until="networkidle")
    page.wait_for_timeout(1200)
    text = page.evaluate(TEXT)
    check(
        "转写录音（中转不可用）：说「中转程序找不到，详见服务日志」",
        "中转程序找不到，详见服务日志" in text,
        text[-80:],
    )
    body = page.evaluate("() => document.body.innerText")
    check(
        "转写录音：页面文字里没有本机目录（/Volumes、/Users、/private、外置中枢……）",
        not LEAK.search(body),
        LEAK.findall(body),
    )

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(2500)
    cards = page.evaluate(
        "() => Array.from(document.querySelectorAll('.metric-strip > *')).map(e => e.innerText.replace(/\\n/g, ' '))"
    )
    jobs = next((card for card in cards if card.startswith("进行中转写")), "")
    check(
        "工作台转写卡片：中转不可用时写「转写状态读取失败」，不说「没有正在处理的录音」",
        "转写状态读取失败" in jobs and "没有正在处理" not in jobs,
        jobs,
    )
    done(c)
