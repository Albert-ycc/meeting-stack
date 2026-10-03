"""项目详情：冷加载、从列表点［进入 →］、在详情里刷新，都不停在「正在读取本地档案」"""

import re

from common import (
    BASE,
    check,
    Collector,
    done,
    launch,
    new_page,
    poll,
    sync_playwright,
)

LOADING = "正在读取本地档案"
STATE = """() => ({hash: location.hash, loading: (document.querySelector('main') || document.body).innerText.includes('正在读取本地档案'),
  tabs: document.querySelectorAll('main [role=tab]').length})"""

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "项目详情冷加载")
    page.goto(BASE + "/#projects/project-yimi", wait_until="networkidle")
    state = page.evaluate(STATE)
    state = poll(
        lambda: page.evaluate(STATE), lambda s: not s["loading"] and s["tabs"] >= 4, timeout=8
    )
    check(
        "冷加载 #projects/<id>：8 秒内读完，四个页签都在",
        not state["loading"] and state["tabs"] >= 4,
        state,
    )
    ctx.close()

    ctx, page = new_page(b)
    c2 = Collector(page, "项目列表进入")
    page.goto(BASE + "/#projects", wait_until="networkidle")
    page.wait_for_timeout(500)
    page.get_by_role("button", name=re.compile(r"^进入")).first.click()
    state = poll(
        lambda: page.evaluate(STATE),
        lambda s: not s["loading"] and s["tabs"] >= 4 and "#projects/" in s["hash"],
        timeout=8,
    )
    check(
        "从列表点［进入 →］：进到项目详情并读完",
        not state["loading"] and state["tabs"] >= 4 and "#projects/" in state["hash"],
        state,
    )
    page.reload(wait_until="networkidle")
    state = poll(
        lambda: page.evaluate(STATE), lambda s: not s["loading"] and s["tabs"] >= 4, timeout=8
    )
    check(
        "在详情里刷新：读完，还在这个项目",
        not state["loading"] and state["tabs"] >= 4 and "#projects/" in state["hash"],
        state,
    )
    done(c, c2)
