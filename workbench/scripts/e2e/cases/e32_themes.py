"""三套主题（深色、浅色、跟随系统）× 十二个页面：data-theme 写对，没有低于 3:1 的文字，没有主题不符的大色块

3:1 到 4.5:1 之间的小字还有几十处（--muted-2 之类），不在这里卡，只数个数打出来。"""

from common import (
    BASE,
    check,
    done,
    launch,
    mid,
    sync_playwright,
    track,
)

PAGES = {
    "工作台": "",
    "录音档案": "#library",
    "需求池": "#requirements",
    "需求详情": "#requirements/req-jd",
    "待办": "#tasks",
    "词典": "#glossary",
    "项目管理": "#projects",
    "项目详情": "#projects/project-yimi",
    "关系图": "#graph",
    "转写录音": "#jobs",
    "会议详情": "#meetings/{meeting}",
    "需求修改": "#requirements/req-jd/edit",
}
# (存的主题偏好, 浏览器的配色偏好, 该是什么主题)
COMBOS = [
    ("dark", "light", "dark"),
    ("light", "dark", "light"),
    ("system", "dark", "dark"),
    ("system", "light", "light"),
]
SCAN = r"""(mode) => {
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; };
  const lum = c => { const f = v => { v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4) }; return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b) };
  const bgOf = el => { let stack=[]; for (let e=el; e; e=e.parentElement) { const c=parse(getComputedStyle(e).backgroundColor); if (c && c.a>0) { stack.push(c); if (c.a>=0.99) break; } }
     let base = stack.length && stack[stack.length-1].a>=0.99 ? stack.pop() : parse(getComputedStyle(document.body).backgroundColor) || {r:255,g:255,b:255,a:1};
     while (stack.length) { const c=stack.pop(); base={r:c.r*c.a+base.r*(1-c.a), g:c.g*c.a+base.g*(1-c.a), b:c.b*c.a+base.b*(1-c.a), a:1}; } return base; };
  const low = [], soft = new Set(), seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (!el.childNodes.length) continue;
    const own = Array.from(el.childNodes).filter(n=>n.nodeType===3 && n.textContent.trim()).map(n=>n.textContent.trim()).join(' ');
    if (!own) continue;
    const r = el.getBoundingClientRect(); if (r.width<2||r.height<2||r.bottom<0||r.top>innerHeight) continue;
    const st = getComputedStyle(el); if (st.visibility==='hidden'||+st.opacity===0) continue;
    const fg = parse(st.color); if (!fg) continue; const bg=bgOf(el);
    const f = {r:fg.r*fg.a+bg.r*(1-fg.a), g:fg.g*fg.a+bg.g*(1-fg.a), b:fg.b*fg.a+bg.b*(1-fg.a)};
    const L1=lum(f), L2=lum(bg); const ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const size = parseFloat(st.fontSize), weight = parseInt(st.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const key = (el.className||'').toString().slice(0,40)+'|'+own.slice(0,16);
    if (ratio < 3) { if (!seen.has(key)) { seen.add(key); low.push({text: own.slice(0,24), ratio: +ratio.toFixed(2), cls: (el.className||'').toString().slice(0,40)}); } }
    else if (ratio < (large ? 3 : 4.5)) soft.add(key);
  }
  const blocks = [];
  for (const el of document.querySelectorAll('main *, aside *, header *')) {
    const r = el.getBoundingClientRect(); if (r.width*r.height < 20000 || r.bottom<0 || r.top>innerHeight) continue;
    const c = parse(getComputedStyle(el).backgroundColor); if (!c || c.a<0.9) continue;
    const L = lum(c); if ((mode==='light' && L<0.08) || (mode==='dark' && L>0.6)) blocks.push({cls:(el.className||'').toString().slice(0,50), bg: getComputedStyle(el).backgroundColor});
  }
  return {theme: document.documentElement.dataset.theme, low: low.sort((a,b)=>a.ratio-b.ratio).slice(0,6), soft: soft.size, blocks: blocks.slice(0,4)};
}"""

with sync_playwright() as p:
    b = launch(p)
    soft_total = 0
    for pref, scheme, want in COMBOS:
        ctx = b.new_context(
            viewport={"width": 1440, "height": 900},
            color_scheme=scheme,
            timezone_id="Asia/Shanghai",
        )
        ctx.set_default_timeout(10_000)
        ctx.add_init_script(
            f"try {{ localStorage.setItem('meeting-workbench:theme', '{pref}') }} catch (e) {{}}"
        )
        page = track(ctx.new_page())
        wrong_theme, low, blocks = [], [], []
        for name, hash_ in PAGES.items():
            page.goto(BASE + "/" + hash_.format(meeting=mid("a1a1a1a1")), wait_until="networkidle")
            page.add_style_tag(content="*{animation:none!important;transition:none!important}")
            page.wait_for_timeout(900)
            result = page.evaluate(SCAN, want)
            if result["theme"] != want:
                wrong_theme.append(f"{name}:{result['theme']}")
            low += [f"{name}「{x['text']}」{x['ratio']}:1" for x in result["low"]]
            blocks += [f"{name} {x['cls']} {x['bg']}" for x in result["blocks"]]
            soft_total += result["soft"]
        label = f"主题偏好 {pref}、浏览器配色 {scheme}"
        check(f"{label}：十二个页面的 data-theme 都是 {want}", not wrong_theme, wrong_theme[:4])
        check(f"{label}：没有低于 3:1 的文字", not low, low[:4])
        check(f"{label}：没有主题不符的大色块", not blocks, blocks[:3])
        ctx.close()
    print(f"（3:1 到 4.5:1 之间的小字，四套合计 {soft_total} 处，不卡）")
    done()
