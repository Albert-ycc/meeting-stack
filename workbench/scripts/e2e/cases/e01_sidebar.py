"""侧栏八个入口逐个进：地址、标题、高亮都对，页面没有异常"""

from common import (
    active_nav,
    BASE,
    check,
    Collector,
    done,
    hash_of,
    launch,
    nav,
    new_page,
    sync_playwright,
)

# 侧栏文字 → (地址 hash, 第一个标题)
ENTRIES = [
    ("工作台", "", "工作台"),
    ("录音档案", "#library", "会议录音档案"),
    ("需求池", "#requirements", "需求池"),
    ("待办", "#tasks", "待办"),
    ("词典", "#glossary", "词典"),
    ("项目管理", "#projects", "项目"),
    ("关系图", "#graph", "全部项目"),
    ("转写录音", "#jobs", "录音流水线"),
]
FIRST_HEADING = "() => (document.querySelector('main h1, main h2') || {}).innerText || null"

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "侧栏")
    page.goto(BASE, wait_until="networkidle")
    labels = page.evaluate(
        "() => Array.from(document.querySelectorAll('aside .rail-link')).map(e => e.innerText.split('\\n')[0].trim())"
    )
    check("侧栏正好这八个入口，顺序不变", labels == [e[0] for e in ENTRIES], labels)
    for label, want_hash, want_heading in ENTRIES:
        nav(page, label, 700)
        got = page.evaluate(FIRST_HEADING)
        check(f"{label}：地址是 {want_hash or '（空）'}", hash_of(page) == want_hash, hash_of(page))
        check(f"{label}：标题是「{want_heading}」", got == want_heading, got)
        check(f"{label}：侧栏只有它亮着", active_nav(page) == [label], active_nav(page))
    done(c)
