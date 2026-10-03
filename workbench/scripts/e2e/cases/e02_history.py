"""地址与历史：侧栏连点后退、前进逐级对称；会议详情的后退、前进和［← 返回］；每个视图刷新后原地恢复"""

from common import (
    active_nav,
    BASE,
    check,
    Collector,
    done,
    hash_of,
    launch,
    mid,
    nav,
    new_page,
    settle,
    sync_playwright,
)

SEQ = [
    ("录音档案", "#library"),
    ("需求池", "#requirements"),
    ("词典", "#glossary"),
    ("项目管理", "#projects"),
]
# 刷新后该亮哪个入口
RELOAD = [
    ("#library", "录音档案"),
    ("#requirements", "需求池"),
    ("#tasks", "待办"),
    ("#glossary", "词典"),
    ("#projects", "项目管理"),
    ("#graph", "关系图"),
    ("#jobs", "转写录音"),
]


def detail_open(page) -> bool:
    return page.locator("section.detail-page").count() > 0


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "历史")
    page.goto(BASE, wait_until="networkidle")
    for label, want in SEQ:
        nav(page, label)
        check(f"点「{label}」→ {want}", hash_of(page) == want, hash_of(page))
    back_expected = ["#glossary", "#requirements", "#library", ""]
    for want in back_expected:
        page.go_back()
        settle(page, 500)
        check(f"后退一步 → {want or '（工作台）'}", hash_of(page) == want, hash_of(page))
    for _, want in SEQ:
        page.go_forward()
        settle(page, 500)
        check(f"前进一步 → {want}", hash_of(page) == want, hash_of(page))

    nav(page, "待办")
    check("点「待办」→ #tasks", hash_of(page) == "#tasks", hash_of(page))

    # 录音档案 → 打开会议 → 后退 / 前进 / ［← 返回］
    nav(page, "录音档案")
    page.locator("button.archive-row").nth(2).click()
    settle(page, 800)
    opened = hash_of(page)
    check(
        "打开会议：地址是 #meetings/<编号>，详情页出来了",
        opened.startswith("#meetings/") and detail_open(page),
        opened,
    )
    page.go_back()
    settle(page, 600)
    check(
        "后退：回到 #library，详情页收起",
        hash_of(page) == "#library" and not detail_open(page),
        hash_of(page),
    )
    page.go_forward()
    settle(page, 600)
    check("前进：回到刚才那场会", hash_of(page) == opened and detail_open(page), hash_of(page))
    back_button = page.locator("section.detail-page").get_by_role("button", name="返回").first
    check(
        "详情页的返回按钮写明回哪里",
        "返回录音档案" in back_button.inner_text(),
        back_button.inner_text(),
    )
    back_button.click()
    settle(page, 500)
    check(
        "点［← 返回录音档案］：回到 #library",
        hash_of(page) == "#library" and not detail_open(page),
        hash_of(page),
    )

    # 刷新恢复：每个视图刷新后还在原地、侧栏高亮不变
    for hash_, label in RELOAD:
        page.goto(BASE + "/" + hash_, wait_until="networkidle")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(500)
        check(
            f"{hash_} 刷新后原地不动，侧栏亮「{label}」",
            hash_of(page) == hash_ and active_nav(page) == [label],
            (hash_of(page), active_nav(page)),
        )
    meeting = mid("a1a1a1a1")
    page.goto(BASE + "/#meetings/" + meeting, wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(800)
    check(
        "会议详情刷新后还在这场会",
        hash_of(page) == "#meetings/" + meeting and detail_open(page),
        hash_of(page),
    )
    done(c)
