"""纪要历史版本：会议详情只带当前版本的正文（20 个版本、每版约 30KB 的会，详情不随历史膨胀），历史版本只有元数据；选历史版本回滚后生成新版本"""

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    mid,
    new_page,
    sync_playwright,
)

MUTATES = True
MEETING = mid("e3e3e3e3")

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b, height=900)
    c = Collector(page, "纪要版本")
    page.on("dialog", lambda d: d.accept())
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if "/minutes-versions/" in r.url else None)
    page.goto(BASE + "/#meetings/" + MEETING, wait_until="networkidle")
    page.wait_for_selector("section.detail-page")
    page.locator(".detail-tabs button", has_text="会议纪要").first.click()
    page.wait_for_selector("aside.minutes-actions")
    page.wait_for_timeout(500)
    aside = page.locator("aside.minutes-actions")
    select = aside.locator("select")
    document = lambda: page.locator(".minutes-document .markdown-safe").inner_text()  # noqa: E731
    rollback = aside.get_by_role("button", name="回滚纪要版本")

    size = len(page.request.get(BASE + "/api/meetings/" + MEETING).body())
    check(
        "详情响应不随历史膨胀：20 版 × 30KB 的会，详情不到 150KB（只带当前版本的正文）",
        size < 150_000,
        size,
    )
    check("主文档是第 20 版", "第 20 版" in document())
    check(
        "版本下拉里有 21 项（20 个版本加一个空项）",
        select.locator("option").count() == 21,
        select.locator("option").count(),
    )
    check("页面上没有历史版本的预览", page.locator("section.minutes-version-preview").count() == 0)
    check("没选历史版本时［回滚纪要版本］置灰", not rollback.is_enabled())

    v7 = select.locator("option", has_text="v7 ·").first.get_attribute("value")
    select.select_option(v7)
    page.wait_for_timeout(600)
    check(
        "选 v7：不去取正文（没有按版本取的请求）、主文档仍是当前版本、［回滚纪要版本］亮起",
        requests == [] and "第 20 版" in document() and rollback.is_enabled(),
        requests,
    )

    rollback.click()
    page.wait_for_timeout(1500)
    check(
        "回滚到 v7：下拉当前项是新生成的 v21，一共 22 项",
        "v21" in select.evaluate("el => el.options[el.selectedIndex].text")
        and select.locator("option").count() == 22,
        select.evaluate("el => el.options[el.selectedIndex].text"),
    )
    check(
        "回滚到 v7：主文档是第 7 版的内容，［回滚纪要版本］又置灰",
        "第 7 版" in document() and not rollback.is_enabled(),
    )
    api = page.evaluate(
        """async m => { const d = await (await fetch('/api/meetings/' + m)).json();
          return { n: d.minutes_versions.length, withBody: d.minutes_versions.filter(v => 'markdown' in v).map(v => v.version_no),
                   withHtml: d.minutes_versions.filter(v => 'html' in v).map(v => v.version_no),
                   current: d.minutes_versions.find(v => v.id === d.current_minutes_version_id).version_no } }""",
        MEETING,
    )
    check(
        "接口里：共 21 个版本，只有当前的 v21 带正文和 html",
        api == {"n": 21, "withBody": [21], "withHtml": [21], "current": 21},
        api,
    )
    done(c)
