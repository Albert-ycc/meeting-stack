"""项目图每 30 秒对一次数据：页面带着上一次的 etag 去，JS 直接看到 304，不再每次拿到一份新解析的图（Playwright 假时钟快进，不真等）"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    sync_playwright,
)

ENGINES = ("chromium", "webkit", "firefox")
INIT = """
(() => {
  window.__star = [];
  const orig = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const res = await orig(input, init);
    if (/\\/api\\/graph\\/projects\\/[^/?]+(\\?|$)/.test(url)) {
      const h = (init && init.headers) || {};
      window.__star.push({ status: res.status, manualIfNoneMatch: !!h['If-None-Match'] });
    }
    return res;
  };
})();
"""

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    ctx.add_init_script(INIT)
    c = Collector(page, "项目图刷新")
    page.clock.install()
    page.goto(BASE + "/#projects/project-yimi/graph", wait_until="networkidle")
    page.wait_for_timeout(1500)
    first = page.evaluate("window.__star")
    check("首次画出：星图请求 200", first and first[0]["status"] == 200, first)
    page.evaluate("window.__star.length = 0")
    for i in range(1, 4):
        page.clock.run_for(30_000)
        page.wait_for_timeout(900)
        seen = page.evaluate("window.__star")
        page.evaluate("window.__star.length = 0")
        check(
            f"第 {i} 次 30 秒刷新：页面自己带了 If-None-Match，JS 看到的是 304",
            len(seen) >= 1 and all(s["status"] == 304 and s["manualIfNoneMatch"] for s in seen),
            seen,
        )
    c.expect_clean("项目图刷新")
    done()
