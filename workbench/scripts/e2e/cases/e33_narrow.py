"""窄窗口（1024、768 宽）：没有横向溢出，没有被别的元素盖住、滚一滚也点不到的控件"""

from common import (
    BASE,
    check,
    done,
    launch,
    mid,
    new_page,
    sync_playwright,
)

PAGES = {
    "工作台": "",
    "录音档案": "#library",
    "需求池": "#requirements",
    "需求池待认领": "#requirements",
    "需求详情": "#requirements/req-jd",
    "待办管理": "#tasks",
    "词典管理": "#glossary",
    "项目管理": "#projects",
    "项目详情": "#projects/project-yimi",
    "关系图": "#graph",
    "转写录音": "#jobs",
    "会议详情": "#meetings/{meeting}",
    "需求修改": "#requirements/req-jd/edit",
    "很长的检索词": None,
}
SCAN = r"""() => {
  const vw = document.documentElement.clientWidth;
  const over = [];
  for (const el of document.querySelectorAll('main *, header *, aside *')) {
    const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1) continue;
    if (r.right > vw + 1) { let p = el.parentElement, clipped = false; for (; p; p = p.parentElement) { const s = getComputedStyle(p); if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) { const pr = p.getBoundingClientRect(); if (pr.right <= vw + 1) { clipped = true; break; } } } if (!clipped) over.push((el.className||'').toString().slice(0,40) + '→' + Math.round(r.right)); }
  }
  // 控件被吸底栏这类东西盖住算不算问题，看滚一滚能不能点到：滚到视口中间还被盖着才算
  const candidates = [];
  for (const b of document.querySelectorAll('main button, header button, main a, main input, main select')) {
    const r = b.getBoundingClientRect(); if (r.width < 2 || r.height < 2 || r.top < 0 || r.bottom > innerHeight || r.left < 0 || r.right > vw) continue;
    if (getComputedStyle(b).visibility === 'hidden') continue;
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (hit && hit !== b && !b.contains(hit) && !hit.contains(b)) candidates.push(b);
  }
  const covered = [];
  for (const b of candidates) {
    b.scrollIntoView({block: 'center'});
    const r = b.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (hit && hit !== b && !b.contains(hit) && !hit.contains(b)) covered.push((b.innerText||b.getAttribute('aria-label')||b.placeholder||'').slice(0,16).replace(/\n/g,' ') + ' ← ' + hit.tagName + '.' + (hit.className||'').toString().slice(0,24));
  }
  window.scrollTo(0, 0);
  return {scrollW: document.scrollingElement.scrollWidth, clientW: vw, over: over.slice(0, 4), covered: covered.slice(0, 4)};
}"""

with sync_playwright() as p:
    b = launch(p)
    for width in (1024, 768):
        ctx, page = new_page(b, width=width, height=800)
        for name, hash_ in PAGES.items():
            if hash_ is None:
                page.goto(BASE + "/#library", wait_until="networkidle")
                page.get_by_label("全局检索").fill("a" * 200)
                page.get_by_label("全局检索").press("Enter")
            else:
                page.goto(
                    BASE + "/" + hash_.format(meeting=mid("d2d2d2d2")), wait_until="networkidle"
                )
                if name == "需求池待认领":
                    page.locator("main [role=tab]", has_text="待认领").first.click()
            page.wait_for_timeout(1000)
            page.add_style_tag(content="*{animation:none!important;transition:none!important}")
            r = page.evaluate(SCAN)
            problems = []
            if r["scrollW"] > r["clientW"]:
                problems.append(f"页面横向能滚：{r['scrollW']} > {r['clientW']}")
            problems += [f"超出视口 {x}" for x in r["over"]] + [f"被盖住 {x}" for x in r["covered"]]
            check(f"{width} 宽「{name}」：没有横向溢出、没有被盖住的控件", not problems, problems)
        ctx.close()
    done()
