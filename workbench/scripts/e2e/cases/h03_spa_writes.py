"""页面自己发出的几种写请求都被后端收下：驳回和撤销驳回的请求体是 {}、手工导入录音的 complete 请求体是 {}、新建标签和项目用的颜色格式

后端对「没有请求体的写」和「颜色」收紧过，这里在真浏览器里点一遍，看页面发出去的格式没有被自己的后端拒掉。"""

import json
import struct
import time
import wave

from common import TMP, BASE, Collector, check, done, launch, nav, new_page, settle, sync_playwright

MUTATES = True
AUDIO = TMP / "upload-sample.wav"
with wave.open(str(AUDIO), "wb") as sample:
    sample.setnchannels(1)
    sample.setsampwidth(2)
    sample.setframerate(8000)
    sample.writeframes(struct.pack("<h", 0) * 8000 * 2)  # 2 秒静音

with sync_playwright() as p:
    b = launch(p)
    ctx, page = new_page(b)
    c = Collector(page, "页面发出的写请求")
    calls: list[tuple] = []
    page.on(
        "response",
        lambda r: (
            calls.append(
                (r.request.method, r.url.replace(BASE, ""), r.status, r.request.post_data or "")
            )
            if r.request.method != "GET"
            else None
        ),
    )
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(600)

    # 驳回、撤销驳回
    nav(page, "待办", 900)
    page.locator("main [role=tab]", has_text="待确认").first.click()
    page.wait_for_timeout(600)
    reject = page.locator("button[aria-label^='驳回「']").first
    check("待办页有可以驳回的审核卡", reject.count() == 1)
    reject.click()
    page.wait_for_timeout(900)
    rejects = [x for x in calls if x[1].endswith("/reject")]
    check(
        "驳回：POST …/reject 回 200，请求体是 {}",
        rejects and rejects[0][2] == 200 and rejects[0][3] == "{}",
        rejects,
    )
    page.get_by_role("button", name="撤销").first.click()
    page.wait_for_timeout(900)
    undos = [x for x in calls if "undo" in x[1]]
    check("撤销驳回：POST …/undo-review 回 200", undos and undos[0][2] == 200, calls)
    calls.clear()

    # 手工导入录音：start → chunks → complete
    nav(page, "转写录音", 800)
    page.locator("input[type=file]").set_input_files(str(AUDIO))
    page.wait_for_timeout(2500)
    complete = [x for x in calls if x[1].endswith("/complete")]
    check(
        "手工导入录音：complete 请求体是 {}，回 200 或 202",
        complete and complete[0][3] == "{}" and complete[0][2] in (200, 202),
        complete,
    )
    calls.clear()

    # 新建标签（<input type=color>）、新建项目（选一个色块）
    nav(page, "项目管理", 800)
    suffix = str(int(time.time()) % 100000)
    page.get_by_label("标签名称").fill("待跟进" + suffix)
    page.get_by_label("标签颜色").fill("#ff8800")
    page.get_by_role("button", name="新建标签").click()
    page.wait_for_timeout(700)
    tags = [x for x in calls if x[1] == "/api/tags"]
    check(
        "新建标签：颜色 #ff8800 被收下",
        tags and tags[0][2] == 200 and json.loads(tags[0][3]).get("color") == "#ff8800",
        tags,
    )
    page.get_by_role("button", name="＋ 新建项目").click()
    page.wait_for_timeout(500)
    dialog = page.get_by_role("dialog", name="新建项目")
    dialog.get_by_placeholder("例如：互联网医院").fill("颜色校验项目" + suffix)
    swatch = dialog.locator(".project-form-modal__swatch").nth(5)
    swatch_color = (swatch.get_attribute("aria-label") or "").split("#")[-1]
    swatch.click()
    dialog.get_by_role("button", name="创建", exact=True).click()
    page.wait_for_timeout(1200)
    projects = [x for x in calls if x[1] == "/api/projects" and x[0] == "POST"]
    check(
        "新建项目：选中的色块颜色被收下",
        projects
        and projects[0][2] == 200
        and json.loads(projects[0][3]).get("color", "").lstrip("#") == swatch_color,
        projects,
    )
    settle(page, 300)
    done(c)
