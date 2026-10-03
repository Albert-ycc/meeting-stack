"""盖层：会议页逐字稿选句 → 建成需求（盖在会议页上）→ 取消 / 浏览器后退：会议页不卸载重建，暂停时滚动和播放进度原样回来；有改动先确认"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    scroll_to,
    sync_playwright,
)

LEAVE_CONFIRM = "当前需求仍有未保存修改。放弃这些修改并离开吗？"
# 用 wav 的那场会：Playwright 自带的 Chromium 不一定解得了 m4a 里的 AAC，wav 一定放得出来
MEETING = mid("e2e2e2e2")

STATE = """() => { const d = document.querySelector('section.detail-page'); const a = document.querySelector('audio');
  const t = document.querySelector('.transcript-scroll');
  return {hash: location.hash, alive: !!(d && d.__mark === 42), detail: !!d, page: Math.round(document.scrollingElement.scrollTop),
          transcript: t ? Math.round(t.scrollTop) : null, paused: a ? a.paused : null, time: a ? Math.round(a.currentTime * 10) / 10 : null} }"""
PICK_RECT = """() => { const box = document.querySelector('.transcript-scroll').getBoundingClientRect();
  for (const p of document.querySelectorAll('.transcript-scroll p.segment-text')) { const r = p.getBoundingClientRect();
    if (r.top > Math.max(box.top, 80) + 10 && r.bottom < Math.min(box.bottom, innerHeight) - 10)
      return {x: r.left, y: r.top, width: r.width, height: r.height}; } return null }"""


def prepare(page, playing: bool):
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.evaluate("() => { document.querySelector('section.detail-page').__mark = 42 }")
    page.evaluate(
        "playing => { const a = document.querySelector('audio'); a.currentTime = 30; return a.play().then(() => { if (!playing) a.pause() }) }",
        playing,
    )
    page.wait_for_timeout(1200)
    scroll_to(page, 350)
    page.evaluate(
        "() => { const t = document.querySelector('.transcript-scroll'); if (t) t.scrollTop = 300 }"
    )
    page.wait_for_timeout(500)
    rect = page.evaluate(PICK_RECT)
    page.mouse.move(rect["x"] + 2, rect["y"] + rect["height"] / 2)
    page.mouse.down()
    page.mouse.move(
        rect["x"] + min(120, rect["width"] - 2), rect["y"] + rect["height"] / 2, steps=8
    )
    page.mouse.up()
    page.wait_for_timeout(400)
    return page.evaluate(STATE)


SCENARIOS = [
    # (放着还是暂停, 怎么关, 表单有没有改)
    (False, "取消按钮", True),
    (False, "浏览器后退", True),
    (False, "取消按钮", False),
    (True, "取消按钮", True),
    (True, "浏览器后退", True),
]

with sync_playwright() as p:
    b = launch(p)
    for playing, how, dirty in SCENARIOS:
        label = f"{'播放中' if playing else '暂停'}、表单{'改过' if dirty else '没改'}、{how}关盖层"
        ctx, page = new_page(b, height=800)
        c = Collector(page, label)
        natives: list[str] = []
        page.on("dialog", lambda d: (natives.append(d.message), d.accept()))
        before = prepare(page, playing)
        pick = page.get_by_role("button", name="建成需求")
        check(f"{label}：选句后出现［建成需求］", pick.count() == 1)
        pick.click()
        page.wait_for_timeout(900)
        opened = page.evaluate(STATE)
        check(
            f"{label}：盖层打开，地址是 #requirements/new，会议页没被卸载",
            opened["hash"] == "#requirements/new" and opened["alive"],
            opened,
        )
        check(
            f"{label}：打开盖层时音频停下，进度没丢",
            opened["paused"] and opened["time"] >= before["time"],
            (before["time"], opened["time"]),
        )
        field = page.get_by_label("需求名").first
        check(f"{label}：盖层里有「需求名」输入框", field.count() == 1)
        if dirty:
            field.fill("测试需求输入中")
        if how == "取消按钮":
            page.get_by_role("button", name="取消").first.click()
        else:
            page.go_back()
        page.wait_for_timeout(1200)
        after = page.evaluate(STATE)
        check(
            f"{label}：回到这场会，会议页还是原来那一份（没卸载重建）",
            after["hash"] == "#meetings/" + MEETING and after["alive"],
            after,
        )
        check(
            f"{label}：{'问了一次「放弃修改」' if dirty else '没改就不问'}",
            natives == ([LEAVE_CONFIRM] if dirty else []),
            natives,
        )
        check(
            f"{label}：播放进度没丢、也没自己接着放",
            abs(after["time"] - opened["time"]) <= 0.5 and after["paused"],
            (opened["time"], after["time"], after["paused"]),
        )
        if not playing:
            # 播放时逐字稿会跟着当前句滚，回来的位置和点之前对不上是跟随造成的，审核记作「待证实」，这里不卡；暂停时必须原样
            check(
                f"{label}：页面滚动、逐字稿滚动原样",
                abs(after["page"] - before["page"]) <= 2
                and abs(after["transcript"] - before["transcript"]) <= 2,
                (before["page"], after["page"], before["transcript"], after["transcript"]),
            )
        c.expect_clean(label)
        ctx.close()
    done()
