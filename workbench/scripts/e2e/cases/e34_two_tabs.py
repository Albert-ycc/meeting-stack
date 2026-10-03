"""双开两个标签页改同一场会的逐字稿：后保存的那个收到冲突提示，不会悄悄盖掉先保存的，自己的改动还留在本地"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    sync_playwright,
    track,
)

MUTATES = True
MEETING = mid("a4a4a4a4")
A_TEXT = "A 标签页改的第三段"
B_TEXT = "B 标签页改的第五段"


def segments(page):
    return page.evaluate(
        "async m => { const d = await (await fetch('/api/meetings/' + m)).json(); return {versions: d.transcript_versions.length, s2: d.segments[2].text, s4: d.segments[4].text} }",
        MEETING,
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, A = new_page(b)
    B = track(ctx.new_page())
    ca = Collector(A, "A 页")
    cb = Collector(B, "B 页").allow(r"^409 \w+ \S*/api/meetings/")
    for page in (A, B):
        page.on("dialog", lambda d: d.accept())
        page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
        page.wait_for_timeout(900)
        page.get_by_role("button", name="编辑逐字稿").click()
        page.wait_for_timeout(400)
    start = segments(A)
    A.locator(".transcript-scroll textarea").nth(2).fill(A_TEXT)
    B.locator(".transcript-scroll textarea").nth(4).fill(B_TEXT)
    A.get_by_role("button", name="保存草稿").click()
    A.wait_for_timeout(1200)
    after_a = segments(A)
    check(
        "A 先保存：成功，多一个版本，第三段是 A 改的",
        after_a["versions"] == start["versions"] + 1 and after_a["s2"] == A_TEXT,
        after_a,
    )
    track(B)
    B.get_by_role("button", name="保存草稿").click()
    B.wait_for_timeout(1200)
    after_b = segments(B)
    check(
        "B 后保存：没有保存进去（版本数不变、A 的第三段还在、B 的第五段没写入）",
        after_b["versions"] == after_a["versions"]
        and after_b["s2"] == A_TEXT
        and after_b["s4"] != B_TEXT,
        after_b,
    )
    text = B.locator("main").inner_text()
    check(
        "B 页上提示「保存时发现工作台上已有更新版本，你的修改还留在本地」",
        "已有更新版本" in text and "你的修改还留在本地" in text,
        text[:80],
    )
    check(
        "B 页给出两个选项：以我的内容覆盖最新版本 / 丢弃我的修改并加载最新",
        B.get_by_role("button", name="以我的内容覆盖最新版本").count() == 1
        and B.get_by_role("button", name="丢弃我的修改并加载最新").count() == 1,
    )
    check(
        "B 页的输入框里自己写的字还在",
        B.locator(".transcript-scroll textarea").nth(4).input_value() == B_TEXT,
    )
    check(
        "B 页的保存请求被服务端以 409 拒绝",
        any(s.startswith("409 ") for s in cb.bad_status),
        cb.bad_status,
    )
    done(ca, cb)
