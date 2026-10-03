"""检索：中文、特殊字符（" * ( - % 等）、很长的词，都不报错、不抛异常；普通词能搜到结果"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    sync_playwright,
)

WORDS = [
    "接口清单",
    "导出 excel",
    '"',
    "*",
    "(",
    ")",
    "-",
    "%",
    "_",
    "'",
    "\\",
    "AND",
    "NOT 导出",
    'excel"',
    "第 39 周",
    "进度*同步",
    "<b>",
    "标签",
    "a" * 300,
    "  ",
    "司美格鲁太",
    "NEAR(导出 excel)",
    "^",
    ":",
    "{}",
]
MUST_FIND = {"接口清单", "导出 excel"}

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "检索")
    page.goto(BASE + "/#library", wait_until="networkidle")
    page.wait_for_timeout(400)
    for word in WORDS:
        errors_before = len(c.unexpected_status()) + len(c.pageerrors)
        page.get_by_label("全局检索").fill(word)
        page.get_by_label("全局检索").press("Enter")
        page.wait_for_timeout(900)
        info = page.evaluate(
            """() => ({hits: document.querySelectorAll('article.search-hit').length,
                      alert: Array.from(document.querySelectorAll('main [role=alert], main .error')).map(e => e.innerText.replace(/\\n/g, ' ')).join(' / ').slice(0, 120)})"""
        )
        new_errors = (c.unexpected_status() + c.pageerrors)[errors_before:]
        label = repr(word[:20])
        check(f"检索 {label}：没有请求出错、没有页面异常", not new_errors, new_errors[:2])
        check(f"检索 {label}：页面上没有「失败」的提示", "失败" not in info["alert"], info["alert"])
        if word in MUST_FIND:
            check(f"检索 {label}：搜得到结果", info["hits"] > 0, info["hits"])
    done(c)
