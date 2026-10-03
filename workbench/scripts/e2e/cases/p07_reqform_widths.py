"""需求新增页、修改页在 1440 / 1024 / 768 宽：表单和墙上预览不叠，没有横向溢出，滚到底时最后一个控件不被吸底操作栏盖住"""

from common import (
    BASE,
    check,
    done,
    launch,
    new_page,
    sync_playwright,
)

PAGES = {"修改页": "#requirements/req-jd/edit", "新增页": "#requirements/new"}
MEASURE = """() => {
  const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return {l: b.left, r: b.right, t: b.top, b: b.bottom} };
  const form = r('.form-card'), prev = r('.form-preview');
  const overlap = form && prev ? Math.max(0, Math.min(form.r, prev.r) - Math.max(form.l, prev.l)) * Math.max(0, Math.min(form.b, prev.b) - Math.max(form.t, prev.t)) : null;
  return {hasForm: !!form, overlap, scrollW: document.scrollingElement.scrollWidth, clientW: document.scrollingElement.clientWidth};
}"""
BOTTOM = """() => {
  document.scrollingElement.scrollTo(0, 1e6);
  const bar = document.querySelector('.form-actions').getBoundingClientRect();
  const ctrls = Array.from(document.querySelectorAll('.form-card input, .form-card textarea, .form-card button, .form-card [role=radio]')).filter((e) => e.getBoundingClientRect().height);
  const last = ctrls[ctrls.length - 1].getBoundingClientRect();
  return {barTop: Math.round(bar.top), lastBottom: Math.round(last.bottom), covered: last.bottom > bar.top};
}"""

with sync_playwright() as p:
    b = launch(p)
    for name, hash_ in PAGES.items():
        for width in (1440, 1024, 768):
            ctx, page = new_page(b, width=width, height=800)
            page.goto(BASE + "/" + hash_, wait_until="networkidle")
            page.wait_for_timeout(700)
            m = page.evaluate(MEASURE)
            check(f"{name} {width} 宽：表单和墙上预览不叠", m["hasForm"] and not m["overlap"], m)
            check(f"{name} {width} 宽：没有横向溢出", m["scrollW"] <= m["clientW"], m)
            page.wait_for_timeout(100)
            bottom = page.evaluate(BOTTOM)
            check(
                f"{name} {width} 宽：滚到底，最后一个控件没被吸底操作栏盖住",
                not bottom["covered"],
                bottom,
            )
            ctx.close()
    done()
