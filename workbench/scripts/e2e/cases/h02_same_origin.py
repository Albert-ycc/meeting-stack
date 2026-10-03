"""本站页面自己的预览图（<img> 指向 /api/materials/files/N/thumb）、地址栏直接打开音频、同源链接下载式导航：都不被「跨站读取」的规则误伤"""

import sqlite3

from common import (
    BASE,
    check,
    DATA_DIR,
    done,
    launch,
    new_page,
    poll,
    sync_playwright,
)


def file_id(name: str) -> int:
    connection = sqlite3.connect(f"file:{DATA_DIR}/workbench.sqlite3?mode=ro", uri=True)
    try:
        row = poll(
            lambda: connection.execute(
                "SELECT id FROM material_files WHERE name=?", (name,)
            ).fetchone(),
            timeout=40,
        )
    finally:
        pass
    connection.close()
    return row[0]


def watch(page):
    pairs = []
    page.on(
        "response",
        lambda resp: (
            pairs.append((resp.status, resp.request)) if resp.request.url.startswith(BASE) else None
        ),
    )
    return pairs


def resolve(pairs):
    out = []
    for status, req in pairs:
        h = req.all_headers()
        out.append(
            {
                "status": status,
                "url": req.url.replace(BASE, ""),
                "type": req.resource_type,
                "site": h.get("sec-fetch-site"),
                "mode": h.get("sec-fetch-mode"),
                "dest": h.get("sec-fetch-dest"),
            }
        )
    return out


FID = file_id("示意图.png")
with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    pairs = watch(page)
    page.goto(BASE + "/#library", wait_until="networkidle")
    # 本站页面自己放一张预览图，和 MaterialPreview 的 <img src={preview.image_url}> 同一种请求
    result = page.evaluate(
        """fid => new Promise((resolve) => {
            const img = new Image();
            img.onload = () => resolve({ok: true, w: img.naturalWidth, h: img.naturalHeight});
            img.onerror = () => resolve({ok: false});
            img.src = `/api/materials/files/${fid}/thumb`;
        })""",
        FID,
    )
    thumb = [r for r in resolve(pairs) if r["url"].endswith("/thumb")]
    check("同源 <img> 预览图：取到了真图", result.get("ok") and result["w"] > 0, result)
    check(
        "同源 <img> 预览图：200、Sec-Fetch-Site 是 same-origin、目的地是 image",
        thumb
        and thumb[0]["status"] == 200
        and thumb[0]["site"] == "same-origin"
        and thumb[0]["dest"] == "image",
        thumb,
    )
    ctx.close()

    # 地址栏直接打开音频（Sec-Fetch-Site: none，整页导航）
    ctx, page = new_page(b)
    pairs = watch(page)
    try:
        page.goto(BASE + "/api/media/4", wait_until="commit")
    except Exception:  # noqa: BLE001  浏览器可能把它当下载或播放器页，goto 抛 ERR_ABORTED 也算收到了回答
        pass
    page.wait_for_timeout(1200)
    media = [r for r in resolve(pairs) if r["url"] == "/api/media/4"]
    check(
        "地址栏直接打开 /api/media/4：200 或 206，Sec-Fetch-Site 是 none",
        media and media[0]["status"] in (200, 206) and media[0]["site"] == "none",
        media[:2],
    )
    ctx.close()

    # 从本站页面里点一个同源链接去下载式地址：同源导航
    ctx, page = new_page(b)
    pairs = watch(page)
    page.goto(BASE + "/#library", wait_until="networkidle")
    page.evaluate(
        "() => { const a = document.createElement('a'); a.id = 'dl'; a.href = '/api/media/7'; a.textContent = 'dl'; document.body.appendChild(a); }"
    )
    try:
        with page.expect_navigation(timeout=4000):
            page.locator("#dl").click()
    except Exception:  # noqa: BLE001  下载式导航没有页面跳转，expect_navigation 超时也算点过了
        pass
    page.wait_for_timeout(1200)
    media = [r for r in resolve(pairs) if r["url"] == "/api/media/7"]
    check(
        "同源链接打开 /api/media/7：200 或 206，same-origin 的导航",
        media
        and media[0]["status"] in (200, 206)
        and media[0]["site"] == "same-origin"
        and media[0]["mode"] == "navigate",
        media[:2],
    )
    ctx.close()
    done()
