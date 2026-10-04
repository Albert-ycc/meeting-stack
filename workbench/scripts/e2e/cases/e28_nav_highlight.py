"""侧栏高亮：各个地址（含二级页）下亮的是哪个入口，冷加载和应用里改地址两种进法一致"""

from common import (
    active_nav,
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    sync_playwright,
)

# 地址 → 该亮的入口；会议详情亮哪个跟着来处走，只要求有且只有一个亮着
EXPECTED = {
    "#library": "录音档案",
    "#requirements": "需求池",
    "#requirements/req-jd": "需求池",
    "#requirements/req-jd/edit": "需求池",
    "#requirements/new": "需求池",
    "#tasks": "待办管理",
    "#glossary": "词典管理",
    "#projects": "项目管理",
    "#projects/project-yimi": "项目管理",
    "#projects/project-yimi/graph": "项目管理",
    "#graph": "关系图",
    "#jobs": "转写录音",
    "#meetings/{meeting}": None,
}
MEETING = mid("a1a1a1a1")

with sync_playwright() as p:
    b = launch(p)
    for mode in ("冷加载", "从项目管理页里改地址"):
        for hash_, want in EXPECTED.items():
            hash_ = hash_.format(meeting=MEETING)
            ctx, page = new_page(b)
            c = Collector(page, f"{mode} {hash_}")
            if mode == "冷加载":
                page.goto(BASE + "/" + hash_, wait_until="networkidle")
            else:
                page.goto(BASE + "/#projects", wait_until="networkidle")
                page.wait_for_timeout(300)
                page.evaluate("h => { location.hash = h }", hash_)
            page.wait_for_timeout(1200)
            got = active_nav(page)
            ok = got == [want] if want else len(got) == 1
            check(
                f"{mode} {hash_.replace(MEETING, '<会议>')}：侧栏亮{'「' + want + '」' if want else '且只亮一个'}",
                ok,
                got,
            )
            ctx.close()
    done()
