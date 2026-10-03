"""页面轮询不记日志：两个标签页开着转写录音页放 13 秒，成功的轮询（health、attention、tasks、glossary/suggestions）不进 web.log，失败的照记；日志里没有查询串"""

import re
from collections import Counter

from common import (
    BASE,
    check,
    done,
    launch,
    LOG_FILE,
    nav,
    new_page,
    sync_playwright,
    track,
)

LOG = LOG_FILE
QUIET = ("/api/health", "/api/attention", "/api/tasks", "/api/glossary/suggestions")
before = len(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else 0

with sync_playwright() as p:
    b = launch(p)
    ctx, first = new_page(b)
    pages = [first, track(ctx.new_page())]
    seen: list[tuple] = []
    for index, page in enumerate(pages):
        page.on(
            "response",
            lambda r, i=index: (
                seen.append((i, r.status, r.url.replace(BASE, ""))) if "/api/" in r.url else None
            ),
        )
        page.goto(BASE + "/", wait_until="networkidle")
        nav(page, "转写录音", 300)
    pages[0].wait_for_timeout(13000)
    b.close()

lines = LOG.read_text(encoding="utf-8").splitlines()[before:]
access = [line for line in lines if "uvicorn.access" in line]
entries = []
for line in access:
    match = re.search(r'"(?:GET|POST|PUT|PATCH|DELETE) (\S+) HTTP[^"]*" (\d+)', line)
    if match:
        entries.append((match.group(1), match.group(2)))
polls = [
    s
    for s in seen
    if s[2].split("?")[0]
    in ("/api/health", "/api/attention", "/api/jobs", "/api/tasks", "/api/glossary/suggestions")
]
print(
    "浏览器发出的轮询类请求：",
    len(polls),
    "；web.log 新增的访问日志按（路径 状态）计数：",
    dict(Counter(entries)),
)
check("两个标签页真的在轮询（13 秒里发出了不少请求）", len(polls) >= 10, len(polls))
check(
    "日志里的访问记录都没有查询串",
    all("?" not in path for path, _ in entries),
    [path for path, _ in entries if "?" in path][:3],
)
check(
    "成功的轮询没有进日志",
    not [(path, status) for path, status in entries if path in QUIET and status == "200"],
    [e for e in entries if e[0] in QUIET][:4],
)
check("失败的轮询照记：/api/jobs 503 在日志里", ("/api/jobs", "503") in entries, entries[:6])
done()
