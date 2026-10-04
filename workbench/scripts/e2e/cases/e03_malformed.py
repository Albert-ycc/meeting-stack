"""畸形地址：冷加载和已开页面里改地址栏两种进法，都不抛未捕获异常，之后侧栏还能用；缺失的会议、需求有人话提示"""

from common import (
    BASE,
    check,
    Collector,
    done,
    hash_of,
    launch,
    mid,
    nav,
    new_page,
    sync_playwright,
)

HASHES = [
    "#meetings/不存在的id",
    "#不存在的视图",
    "#meetings/",
    "#meetings",
    "#meetings/%E4",
    "#requirements/%ZZ",
    "#projects/%",
    "#glossary/project/%E4",
    "#requirements/claim/%E4",
    "#requirements/x%E4/edit",
    "#requirements/req-nope",
    "#requirements/req-nope/edit",
    "#requirements/claim/nope",
]
OPEN_ANYWAY = "#meetings/{id}@{suffix}"  # @ 后面的秒数解析不出来时当没有秒数，照样打开那场会


def snap(page):
    return page.evaluate(
        """() => ({hash: location.hash,
                  main: (document.querySelector('main') || document.body).innerText.slice(0, 120).replace(/\\n/g, ' | '),
                  rootEmpty: !(document.getElementById('root') || {}).childElementCount})"""
    )


with sync_playwright() as p:
    b = launch(p)
    meeting = mid("d3d3d3d3")
    for index, hash_ in enumerate(HASHES):
        ctx, page = new_page(b)
        c = Collector(page, f"冷加载 {hash_}")
        c.allow(r"^404 GET ")
        page.goto(BASE + "/" + hash_, wait_until="networkidle")
        page.wait_for_timeout(600)
        check(
            f"冷加载 {hash_}：没有页面异常、页面不是空的",
            not c.pageerrors and not snap(page)["rootEmpty"],
            c.pageerrors,
        )
        ctx.close()
    for index, hash_ in enumerate(HASHES):
        ctx, page = new_page(b)
        c = Collector(page, f"改地址栏 {hash_}")
        c.allow(r"^404 GET ")
        page.goto(BASE + "/#library", wait_until="networkidle")
        page.evaluate("h => { location.hash = h }", hash_)
        page.wait_for_timeout(700)
        check(f"已开着页面改成 {hash_}：没有页面异常", not c.pageerrors, c.pageerrors)
        nav(page, "词典管理")
        check(f"改成 {hash_} 之后侧栏还能用", hash_of(page) == "#glossary", hash_of(page))
        ctx.close()

    # 缺失的会议、需求：说人话，会议有返回入口，需求不给没用的［重试］
    ctx, page = new_page(b)
    c = Collector(page, "缺失的对象").allow(r"^404 GET ")
    page.goto(BASE + "/#meetings/不存在的id", wait_until="networkidle")
    page.wait_for_timeout(500)
    text = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
    check("会议不存在：写着「会议不存在」", "会议不存在" in text, text[:60])
    check("会议不存在：有返回入口", page.get_by_role("button", name="返回").count() >= 1)
    page.goto(BASE + "/#requirements/req-nope", wait_until="networkidle")
    page.wait_for_timeout(500)
    text = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
    check("需求不存在：写着「需求不存在或已删除」", "需求不存在或已删除" in text, text[:60])
    check("需求不存在：没有没用的［重试］", page.get_by_role("button", name="重试").count() == 0)
    for suffix in ("abc", "99999"):
        page.goto(BASE + "/#meetings/" + meeting + "@" + suffix, wait_until="networkidle")
        page.wait_for_timeout(800)
        check(
            f"#meetings/<编号>@{suffix}：照样打开那场会",
            page.locator("section.detail-page").count() == 1,
            hash_of(page),
        )
    ctx.close()
    done(c)
