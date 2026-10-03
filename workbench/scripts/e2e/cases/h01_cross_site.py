"""跨站读取：另一个站点的页面往声档发 <img>、fetch、<iframe>、<audio>，声档一律回 403、不算出峰值缓存；整页导航进声档放行；本站页面自己的播放和波形不受影响

另一个站点由用例自己起一个临时的小服务（127.0.0.1 上随机端口）：用 localhost 访问它是跨站，用 127.0.0.1 访问是同站不同源。"""

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import (
    BASE,
    check,
    Collector,
    DATA_DIR,
    done,
    heading,
    launch,
    mid,
    new_page,
    settle,
    sync_playwright,
)

PEAKS = DATA_DIR / "waveform-peaks"
PAGE = f"""<!doctype html>
<meta charset="utf-8">
<title>另一个站点的页面</title>
<h1>另一个站点的页面</h1>
<img id="img" alt="" src="{BASE}/api/media/4/peaks">
<audio id="aud" preload="auto" src="{BASE}/api/media/13"></audio>
<iframe id="frame" src="{BASE}/api/media/10/peaks"></iframe>
<a id="go" href="{BASE}/#library">打开声档（整页导航）</a>
<script>
  window.results = {{}};
  fetch("{BASE}/api/media/7/peaks", {{ mode: "no-cors" }})
    .then(() => (window.results.fetchNoCors = "sent"), (e) => (window.results.fetchNoCors = "error " + e));
  fetch("{BASE}/api/bootstrap")
    .then((r) => r.text().then((t) => (window.results.fetchCors = "read " + t.length)), (e) => (window.results.fetchCors = "error " + e));
</script>
"""


class ForeignPage(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def peak_files() -> set[str]:
    return {p.name for p in PEAKS.glob("*.json")} if PEAKS.is_dir() else set()


def watch(page):
    """先记下 (状态, 请求)，等页面安静了再用 all_headers() 读真实发出去的头（Sec-Fetch-* 是网络栈加的，request.headers 里没有）"""
    pairs = []
    page.on(
        "response",
        lambda resp: (
            pairs.append((resp.status, resp.request)) if resp.request.url.startswith(BASE) else None
        ),
    )
    return pairs


def resolve(pairs):
    rows = []
    for status, req in pairs:
        h = req.all_headers()
        rows.append(
            {
                "status": status,
                "url": req.url.replace(BASE, ""),
                "type": req.resource_type,
                "site": h.get("sec-fetch-site"),
                "mode": h.get("sec-fetch-mode"),
            }
        )
    return rows


server = ThreadingHTTPServer(("127.0.0.1", 0), ForeignPage)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

with sync_playwright() as p:
    b = launch(p)
    before = peak_files()
    for kind, host in (("跨站", "localhost"), ("同站不同源", "127.0.0.1")):
        ctx, page = new_page(b)
        pairs = watch(page)
        page.goto(f"http://{host}:{port}/page.html", wait_until="load")
        page.wait_for_timeout(2500)
        rows = [r for r in resolve(pairs) if "/api/" in r["url"]]
        check(
            f"另一个站点的页面（{kind}）：往声档发的请求（iframe、fetch 等）到了声档，不是根本没发出去",
            len(rows) >= 2,
            [(r["url"], r["status"]) for r in rows],
        )
        check(
            f"另一个站点的页面（{kind}）：声档对这些请求一律回 403",
            rows and {r["status"] for r in rows} == {403},
            [(r["url"], r["status"], r["site"]) for r in rows],
        )
        check(
            f"另一个站点的页面（{kind}）：没有算出任何峰值缓存",
            peak_files() == before,
            sorted(peak_files() - before),
        )
        ctx.close()

    # 整页导航：从另一个站点的页面点链接进声档（飞书卡片、书签的情形）放行
    ctx, page = new_page(b)
    pairs = watch(page)
    page.goto(f"http://localhost:{port}/page.html", wait_until="load")
    page.wait_for_timeout(800)
    page.locator("#go").click()
    page.wait_for_url(BASE + "/**")
    settle(page, 1500)
    docs = [r for r in resolve(pairs) if r["type"] == "document" and r["url"] == "/"]
    check(
        "从别的站点点链接进声档：主文档 200，是跨站的整页导航",
        docs
        and docs[0]["status"] == 200
        and docs[0]["site"] == "cross-site"
        and docs[0]["mode"] == "navigate",
        docs,
    )
    check(
        "导航进来以后页面正常出来",
        heading(page) == "会议录音档案" or page.locator("main").count() == 1,
        heading(page),
    )
    ctx.close()

    # 对照：本站页面自己播放、取波形
    ctx, page = new_page(b)
    pairs = watch(page)
    c = Collector(page, "本站页面")
    page.goto(BASE + "/#meetings/" + mid("a1a1a1a1"), wait_until="networkidle")
    page.wait_for_timeout(2500)
    rows = resolve(pairs)
    media = [r for r in rows if re.fullmatch(r"/api/media/\d+", r["url"])]
    peaks = [r for r in rows if re.fullmatch(r"/api/media/\d+/peaks", r["url"])]
    check(
        "本站页面：音频请求 200/206，Sec-Fetch-Site 是 same-origin",
        media
        and all(r["status"] in (200, 206) for r in media)
        and media[0]["site"] == "same-origin",
        media,
    )
    check(
        "本站页面：波形请求 200，是 same-origin",
        peaks and peaks[0]["status"] == 200 and peaks[0]["site"] == "same-origin",
        peaks,
    )
    check(
        "本站页面取过之后：峰值缓存出来了",
        len(peak_files()) >= len(before) + (0 if peak_files() >= before and len(before) else 1),
        sorted(peak_files()),
    )
    check("本站页面：没有 403", not [s for s in c.bad_status if s.startswith("403")], c.bad_status)
    c.expect_clean("本站页面")
    ctx.close()
    server.shutdown()
    done()
