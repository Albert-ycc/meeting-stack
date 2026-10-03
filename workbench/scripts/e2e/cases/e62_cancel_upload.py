"""手工导入录音的［取消上传］：10MiB 的文件分 3 块，第 1 块的请求按住（模拟发到一半），这时点［取消上传］

看：这一块请求真被中止、没有第 2 块和 complete、发了 cancel 且 200、界面一句「已取消上传」没有失败提示、
导入按钮回来、服务端不留半截会话；然后同一个文件重新选、这回放行，整段传完。"""

from common import (
    BASE,
    check,
    Collector,
    DATA_DIR,
    done,
    hold,
    launch,
    new_page,
    release,
    sync_playwright,
    TMP,
)

MUTATES = True
FILE = TMP / "meeting-e2e.m4a"
FILE.write_bytes(b"\0" * (10 * 1024 * 1024))
SESSIONS = DATA_DIR / "uploads" / ".sessions"


def sessions() -> list[str]:
    # .quota.lock 是会话根目录里的锁文件，不是会话；会话目录都叫 upload-<id>
    return (
        sorted(p.name for p in SESSIONS.iterdir() if p.name.startswith("upload-"))
        if SESSIONS.exists()
        else []
    )


with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "取消上传")
    events: list[tuple] = []
    page.on(
        "request",
        lambda r: (
            events.append(("发出", r.method, r.url.replace(BASE, "")))
            if "/api/uploads" in r.url
            else None
        ),
    )
    page.on(
        "requestfinished",
        lambda r: (
            events.append(
                (
                    "完成",
                    r.method,
                    r.url.replace(BASE, ""),
                    r.response().status if r.response() else None,
                )
            )
            if "/api/uploads" in r.url
            else None
        ),
    )
    page.on(
        "requestfailed",
        lambda r: (
            events.append(("失败", r.method, r.url.replace(BASE, ""), r.failure))
            if "/api/uploads" in r.url
            else None
        ),
    )
    held = hold(
        page, "**/api/uploads/*/chunks/*", only=lambda request: request.url.endswith("/chunks/1")
    )
    before = sessions()
    page.goto(BASE + "/#jobs", wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.locator("input[type=file]").set_input_files(str(FILE))
    page.wait_for_function("() => /上传中/.test(document.body.innerText)", timeout=15000)
    page.wait_for_timeout(1500)
    check("第 1 块的请求在路上被按住了", len(held) == 1, len(held))
    cancel = page.get_by_role("button", name="取消上传")
    check("上传中出现［取消上传］", cancel.count() == 1)

    cancel.click()
    page.get_by_text("已取消上传").wait_for(timeout=10000)
    page.wait_for_timeout(500)
    chunk1_failed = [e for e in events if e[0] == "失败" and e[2].endswith("/chunks/1")]
    check(
        "第 1 块的请求被浏览器中止（ERR_ABORTED）",
        chunk1_failed and "ERR_ABORTED" in chunk1_failed[0][3],
        chunk1_failed,
    )
    check("没有发第 2 块", not any(e[2].endswith("/chunks/2") for e in events))
    check("没有发 complete", not any(e[2].endswith("/complete") for e in events))
    cancels = [e for e in events if e[0] == "完成" and e[2].endswith("/cancel")]
    check("发了 cancel 且回 200", cancels and cancels[0][3] == 200, cancels)
    # 隔离实例没有 relay，页面上本来就有一块「任务控制接口尚未接入」的 role=alert，和上传无关：只看操作提示条
    banners = page.locator(".action-banner")
    check(
        "操作提示条是一句成功样式的「已取消上传」，没有失败样式的",
        banners.count() == 1
        and "action-banner--success" in banners.first.get_attribute("class")
        and "已取消上传" in banners.first.inner_text()
        and page.locator(".action-banner--error").count() == 0,
        banners.first.inner_text() if banners.count() else "（没有提示条）",
    )
    check("导入按钮回来了、能点", page.get_by_role("button", name="＋ 手工导入录音").is_enabled())
    check(
        "取消按钮没了",
        page.get_by_role("button", name="取消上传", exact=True).count() == 0
        and page.get_by_role("button", name="正在取消…").count() == 0,
    )
    cancelled_id = cancels[0][2].split("/")[3] if cancels else None
    check(
        "服务端没留下半截会话：被取消的那个 id 不在，会话目录和取消前一样",
        cancelled_id not in sessions() and sessions() == before,
        (cancelled_id, sessions()),
    )

    # 取消以后同一个文件重新来：这回放行
    release(held)
    page.unroute("**/api/uploads/*/chunks/*")
    events.clear()
    page.locator("input[type=file]").set_input_files(str(FILE))
    page.wait_for_function(
        "() => /已保存|录音已安全保存/.test(document.body.innerText)", timeout=60000
    )
    page.wait_for_timeout(500)
    done_events = [e for e in events if e[0] == "完成" and e[2].endswith("/complete")]
    check(
        "重新选同一个文件：整段传完、complete 返回",
        done_events and done_events[0][3] in (200, 202),
        done_events,
    )
    check("三块都发了", sum(1 for e in events if e[0] == "完成" and "/chunks/" in e[2]) == 3)
    c.expect_clean("取消上传")
    done()
