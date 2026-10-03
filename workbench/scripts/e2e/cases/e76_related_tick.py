"""每 30 秒星图和相关线各带各自的 etag，谁变了只换谁（Playwright 假时钟快进 30 秒）

隔离实例里关联整理和语义索引是关着的，［相关］不会出现：这里在页面这一侧改 /api/bootstrap 的返回让页面以为它们开着；
相关线的接口仍是真后端的（库里没有相关线，回空的），R2 起换成一份带新 etag 的 200 模拟后台重算。"""

import json

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    sync_playwright,
)

# 用例里把相关线接口换成假的 304，WebKit 的 route.fulfill 不让回 304，所以只跑另两个
ENGINES = ("chromium", "firefox")
KEY = "meeting-workbench:graph:lines.project-yimi"
INIT = f"""
(() => {{
  window.localStorage.setItem({json.dumps(KEY)}, JSON.stringify({{ mention: true, related: true }}));
  window.__calls = [];
  const orig = window.fetch.bind(window);
  window.fetch = async (input, init) => {{
    const url = typeof input === 'string' ? input : input.url;
    const res = await orig(input, init);
    const h = (init && init.headers) || {{}};
    let kind = null;
    if (/\\/api\\/graph\\/projects\\/[^/?]+\\/related/.test(url)) kind = 'related';
    else if (/\\/api\\/graph\\/projects\\/[^/?]+(\\?|$)/.test(url)) kind = 'star';
    if (kind) window.__calls.push({{ kind, status: res.status, manual: !!h['If-None-Match'] }});
    return res;
  }};
}})();
"""
RELATED_LINE = "[aria-label='连线：共同词：报价单、驻场']"
FAKE_ETAG = 'W/"related-fake-2"'

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    ctx.add_init_script(INIT)
    c = Collector(page, "相关线刷新")

    def bootstrap(route):
        response = route.fetch()
        body = response.json()
        body["links_enabled"] = True
        body["semantic_enabled"] = True
        body["llm_configured"] = False
        route.fulfill(response=response, json=body)

    page.route("**/api/bootstrap", bootstrap)
    meeting_id = page.request.get(BASE + "/api/graph/projects/project-yimi?window=28d").json()[
        "meetings"
    ][0]["meeting_id"]
    fake = {"on": False, "served": 0}
    payload = {
        "rev": 2,
        "files": {"9": {"file_id": 9, "name": "接口文档.docx", "ext": "docx", "rel_path": "接口文档.docx", "root_id": 1}},
        "edges": [
            {
                "id": "e:rel:5", "relation_id": 5, "meeting_id": meeting_id, "file_id": 9, "rank": 1,
                "words": ["报价单", "驻场"], "at_ms": 1000, "quote": "报价单再看一下",
                "passage": {"content_key": "k", "ordinal": 0, "loc": "第 1 页", "text": "报价单的驻场部分"},
            }
        ],
    }  # fmt: skip

    def related(route):
        if not fake["on"]:
            route.continue_()
        elif route.request.headers.get("if-none-match") == FAKE_ETAG:
            route.fulfill(status=304, headers={"ETag": FAKE_ETAG})
        else:
            fake["served"] += 1
            route.fulfill(
                status=200,
                headers={"ETag": FAKE_ETAG, "Content-Type": "application/json"},
                body=json.dumps(payload),
            )

    page.route("**/api/graph/projects/project-yimi/related*", related)
    page.clock.install()
    page.goto(BASE + "/#projects/project-yimi/graph", wait_until="networkidle")
    page.wait_for_timeout(1500)

    def calls():
        value = page.evaluate("window.__calls")
        page.evaluate("window.__calls.length = 0")
        return value

    def revalidated(seen, kind) -> bool:
        mine = [s for s in seen if s["kind"] == kind]
        return len(mine) >= 1 and all(s["status"] == 304 and s["manual"] for s in mine)

    calls()
    for i in range(1, 4):
        page.clock.run_for(30_000)
        page.wait_for_timeout(900)
        seen = calls()
        check(
            f"R1 第 {i} 次 30 秒：星图和相关线都是页面自己带 etag 去对的，都回 304",
            revalidated(seen, "star") and revalidated(seen, "related"),
            seen,
        )

    fake["on"] = True
    page.clock.run_for(30_000)
    page.wait_for_timeout(1200)
    seen = calls()
    check(
        "R2 相关线换成新 etag 的 200（星图没变）：星图仍 304，相关线收到 200",
        revalidated(seen, "star")
        and any(s["kind"] == "related" and s["status"] == 200 for s in seen)
        and fake["served"] == 1,
        (seen, fake),
    )
    check("R2 新的相关线画出来了", page.locator(RELATED_LINE).count() > 0)
    page.clock.run_for(30_000)
    page.wait_for_timeout(900)
    seen = calls()
    check(
        "R2 再过 30 秒：相关线带新 etag 回 304，星图也 304",
        revalidated(seen, "star") and revalidated(seen, "related"),
        seen,
    )
    c.expect_clean("相关线刷新")
    done()
