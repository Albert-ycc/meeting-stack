from pathlib import Path

from playwright.sync_api import Page, sync_playwright


BASE_URL = "http://127.0.0.1:8765"
OUTPUT = Path.home() / ".meeting-workbench/browser-artifacts"
OUTPUT.mkdir(parents=True, exist_ok=True)


def assert_no_browser_errors(
    errors: list[str], failed_requests: list[str], expected_evidence_statuses: list[int]
) -> None:
    remaining_errors = list(errors)
    for status in expected_evidence_statuses:
        marker = f"status of {status}"
        matching = next(
            (
                index
                for index, message in enumerate(remaining_errors)
                if "Failed to load resource" in message and marker in message
            ),
            None,
        )
        assert matching is not None, (
            f"expected minutes-evidence {status} console message was not observed"
        )
        remaining_errors.pop(matching)
    assert not remaining_errors, f"browser console errors: {remaining_errors}"
    assert not failed_requests, f"failed requests: {failed_requests}"


def collect_failures(page: Page) -> tuple[list[str], list[str], list[int]]:
    errors: list[str] = []
    failed_requests: list[str] = []
    expected_evidence_statuses: list[int] = []
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )

    def record_failed_request(request) -> None:
        reason = request.failure or "unknown"
        if "/api/media/" in request.url and "ERR_ABORTED" in reason:
            return
        failed_requests.append(f"{request.method} {request.url} ({reason})")

    page.on("requestfailed", record_failed_request)
    page.on(
        "response",
        lambda response: (
            expected_evidence_statuses.append(response.status)
            if "/minutes-evidence" in response.url and response.status in {404, 409}
            else None
        ),
    )
    return errors, failed_requests, expected_evidence_statuses


def wait_for_scroll_to_settle(page: Page) -> None:
    page.evaluate(
        """async () => {
          await new Promise((resolve) => {
            let previousX = window.scrollX;
            let previousY = window.scrollY;
            let stableFrames = 0;
            const check = () => {
              if (window.scrollX === previousX && window.scrollY === previousY) {
                stableFrames += 1;
              } else {
                previousX = window.scrollX;
                previousY = window.scrollY;
                stableFrames = 0;
              }
              // React can schedule transcript anchor scrolling after the first few quiet frames.
              if (stableFrames >= 12) resolve(undefined);
              else window.requestAnimationFrame(check);
            };
            window.requestAnimationFrame(check);
          });
        }"""
    )


cache = Path.home() / "Library/Caches/ms-playwright"
executables = sorted(cache.glob("chromium_headless_shell-*/**/chrome-headless-shell"))
assert executables, "Playwright Chromium is not installed"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, executable_path=executables[-1])

    wide = browser.new_page(viewport={"width": 1900, "height": 1000})
    wide_errors, wide_failed, wide_expected_evidence = collect_failures(wide)
    wide.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE_URL)
    wide.goto(BASE_URL, wait_until="networkidle")
    wide.add_style_tag(
        content="*, *::before, *::after { animation: none !important; transition: none !important; }"
    )
    wide.get_by_role("button", name="资料库").click()
    wide.get_by_role("heading", name="会议录音档案").wait_for()
    wide.wait_for_timeout(750)
    assert wide.get_by_text("文件夹", exact=True).count() == 0
    wide_copy = wide.locator("button.archive-path-copy:not(:disabled)").first
    assert wide_copy.text_content() == ""
    row_box = wide_copy.locator("xpath=..").bounding_box()
    copy_box = wide_copy.bounding_box()
    assert row_box is not None and copy_box is not None
    assert copy_box["width"] <= 32 and copy_box["height"] <= 32
    assert copy_box["x"] + copy_box["width"] <= row_box["x"] + row_box["width"]
    wide.screenshot(path=OUTPUT / "library-copy-rest.png", full_page=False)
    wide_copy.hover()
    wide.screenshot(path=OUTPUT / "library-copy-hover.png", full_page=False)
    wide_copy.click()
    wide.get_by_role("button", name="文件夹路径已复制").wait_for()
    wide.screenshot(path=OUTPUT / "library-copy-success.png", full_page=False)
    assert_no_browser_errors(wide_errors, wide_failed, wide_expected_evidence)
    wide.close()

    desktop = browser.new_page(viewport={"width": 1440, "height": 1000})
    desktop.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE_URL)
    desktop_errors, desktop_failed, desktop_expected_evidence = collect_failures(desktop)
    desktop.goto(BASE_URL, wait_until="networkidle")
    assert desktop.get_by_role("heading", name="本地会议处理台").is_visible()
    assert desktop.get_by_text("服务正常", exact=True).is_visible()
    meeting_count = desktop.evaluate(
        "async () => (await (await fetch('/api/health')).json()).counts.meetings"
    )
    assert meeting_count > 0

    desktop.get_by_role("button", name="资料库").click()
    desktop.get_by_role("heading", name="会议录音档案").wait_for()
    assert desktop.get_by_label("筛选参与人").count() == 0
    copy_path = desktop.locator("button.archive-path-copy:not(:disabled)").first
    expected_path = copy_path.get_attribute("data-copy-path")
    assert expected_path
    copy_path.click()
    copied_path = desktop.evaluate("navigator.clipboard.readText()")
    assert copied_path == expected_path
    page_size = 50
    assert desktop.locator("button.archive-row").count() == min(meeting_count, page_size)
    if meeting_count > page_size:
        desktop.get_by_role("button", name="下一页").click()
        expected_last_page = min(page_size, meeting_count - page_size)
        desktop.wait_for_function(
            "expected => document.querySelectorAll('button.archive-row').length === expected",
            arg=expected_last_page,
        )
        desktop.get_by_role("button", name="上一页").click()
        desktop.wait_for_function(
            "expected => document.querySelectorAll('button.archive-row').length === expected",
            arg=page_size,
        )

    desktop.get_by_label("全局检索").fill("需求")
    desktop.get_by_role("button", name="检索").click()
    desktop.locator("article.search-hit").first.wait_for()
    first_anchor = desktop.locator("button.time-anchor").first
    first_anchor.click()
    desktop.locator("section.detail-page").wait_for()
    desktop.locator("article.transcript-row[aria-current='true']").first.wait_for(timeout=10_000)
    desktop.wait_for_function(
        "() => document.querySelector('audio')?.currentTime > 0", timeout=10_000
    )
    current_time = desktop.locator("audio").evaluate("audio => audio.currentTime")
    assert current_time > 0, f"search anchor did not seek audio: {current_time}"
    assert desktop.get_by_role("button", name="编辑逐字稿").is_visible()

    waveform = desktop.locator('.waveform [part="wrapper"]')
    waveform.evaluate("element => element.scrollIntoView({ block: 'center', behavior: 'instant' })")
    wait_for_scroll_to_settle(desktop)
    waveform_box = waveform.bounding_box()
    assert waveform_box is not None
    desktop.mouse.move(waveform_box["x"] + waveform_box["width"] * 0.1, waveform_box["y"] + 30)
    desktop.mouse.down()
    desktop.mouse.move(
        waveform_box["x"] + waveform_box["width"] * 0.72,
        waveform_box["y"] + 30,
        steps=12,
    )
    desktop.mouse.up()
    desktop.wait_for_timeout(500)
    drag_state = desktop.locator("audio").evaluate(
        "audio => ({ currentTime: audio.currentTime, duration: audio.duration })"
    )
    assert (
        drag_state["duration"] > 0 and drag_state["currentTime"] > drag_state["duration"] * 0.6
    ), f"waveform drag failed: state={drag_state}, box={waveform_box}"
    dragged_time = desktop.locator("audio").evaluate("audio => audio.currentTime")
    desktop.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    wait_for_scroll_to_settle(desktop)
    waveform_box = waveform.bounding_box()
    topbar_box = desktop.locator("header.topbar").bounding_box()
    transport_box = desktop.locator("section.audio-transport-dock").bounding_box()
    assert waveform_box is not None
    assert topbar_box is not None and transport_box is not None
    assert abs(transport_box["y"] - (topbar_box["y"] + topbar_box["height"])) <= 2, (
        f"desktop transport={transport_box}, topbar={topbar_box}"
    )
    assert transport_box["height"] < 80
    assert waveform_box["y"] + waveform_box["height"] <= transport_box["y"] + 1
    desktop.screenshot(path=OUTPUT / "optimization-desktop-compact.png", full_page=False)

    whisper = desktop.get_by_role("button", name="Whisper 对照")
    whisper_enabled = whisper.is_enabled()
    comparison_count = 0
    if whisper_enabled:
        whisper.click()
        desktop.wait_for_function(
            """() => {
              const main = document.querySelector('section.editor-main');
              if (!main) return false;
              if (main.querySelectorAll('.comparison-row').length > 0) return true;
              const heading = main.querySelector(':scope > .comparison-empty h2');
              return Boolean(
                heading
                && (heading.textContent?.includes('正在读取')
                  || heading.textContent?.includes('暂无 Whisper 对照稿'))
              );
            }"""
        )
        initial_empty_heading = None
        if desktop.locator("section.editor-main .comparison-row").count() == 0:
            initial_empty_heading = desktop.locator(
                "section.editor-main > .comparison-empty h2"
            ).text_content()
        if initial_empty_heading and "暂无 Whisper 对照稿" not in initial_empty_heading:
            desktop.wait_for_function(
                """() => {
                  const main = document.querySelector('section.editor-main');
                  if (!main) return false;
                  if (main.querySelectorAll('.comparison-row').length > 0) return true;
                  const heading = main.querySelector(':scope > .comparison-empty h2');
                  return Boolean(heading && !heading.textContent?.includes('正在读取'));
                }"""
            )
        comparison_count = desktop.locator(".comparison-row").count()
        if comparison_count:
            assert desktop.get_by_label("Whisper 对照稿逐段对照").is_visible()
            assert desktop.locator(".comparison-row .risk-badge").count() >= comparison_count
            assert desktop.locator(".comparison-row .gold-trigger").count() == comparison_count
            desktop.locator(".comparison-panel__head").scroll_into_view_if_needed()
            desktop.screenshot(path=OUTPUT / "asr-comparison-desktop.png", full_page=False)
        else:
            empty_heading = desktop.locator(
                "section.editor-main > .comparison-empty h2"
            ).text_content()
            assert empty_heading and "Whisper" in empty_heading and "暂无" in empty_heading

    desktop.locator(".detail-tabs button").nth(1).click()
    assert desktop.get_by_role("button", name="发布正式版本").is_visible()

    desktop.screenshot(path=OUTPUT / "desktop-final.png", full_page=False)
    assert_no_browser_errors(desktop_errors, desktop_failed, desktop_expected_evidence)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile_errors, mobile_failed, mobile_expected_evidence = collect_failures(mobile)
    mobile.goto(BASE_URL, wait_until="networkidle")
    assert mobile.get_by_text("只读访问", exact=True).count() == 1
    assert mobile.get_by_role("button", name="任务", exact=True).count() == 0
    mobile.get_by_role("button", name="资料库").click()
    mobile.get_by_role("heading", name="会议录音档案").wait_for()
    mobile.get_by_label("全局检索").fill("需求")
    mobile.get_by_role("button", name="检索").click()
    mobile.locator("button.time-anchor").first.wait_for()
    mobile.locator("button.time-anchor").first.click()
    mobile.locator("section.detail-page").wait_for()
    assert mobile.locator(".detail-page .read-only-chip").is_visible()
    assert mobile.get_by_role("button", name="编辑逐字稿").count() == 0
    mobile_waveform = mobile.locator('.waveform [part="wrapper"]')
    mobile_waveform.evaluate(
        "element => element.scrollIntoView({ block: 'center', behavior: 'instant' })"
    )
    wait_for_scroll_to_settle(mobile)
    mobile.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    wait_for_scroll_to_settle(mobile)
    mobile_waveform_box = mobile_waveform.bounding_box()
    mobile_topbar_box = mobile.locator("header.topbar").bounding_box()
    mobile_transport_box = mobile.locator("section.audio-transport-dock").bounding_box()
    assert mobile_waveform_box is not None
    assert mobile_topbar_box is not None and mobile_transport_box is not None
    assert (
        abs(mobile_transport_box["y"] - (mobile_topbar_box["y"] + mobile_topbar_box["height"])) <= 2
    ), f"mobile transport={mobile_transport_box}, topbar={mobile_topbar_box}"
    assert mobile_transport_box["height"] < 110, (
        f"mobile transport too tall: {mobile_transport_box}"
    )
    assert mobile_waveform_box["y"] + mobile_waveform_box["height"] <= mobile_transport_box["y"] + 1
    mobile.screenshot(path=OUTPUT / "optimization-mobile-compact.png", full_page=False)
    mobile.locator(".detail-tabs button").nth(1).click()
    assert mobile.get_by_role("button", name="发布正式版本").count() == 0
    assert mobile.get_by_role("button", name="保存归档").count() == 0
    mobile.screenshot(path=OUTPUT / "mobile-final.png", full_page=False)
    assert_no_browser_errors(mobile_errors, mobile_failed, mobile_expected_evidence)

    landscape_context = browser.new_context(
        viewport={"width": 900, "height": 500},
        has_touch=True,
    )
    landscape = landscape_context.new_page()
    landscape.goto(BASE_URL, wait_until="networkidle")
    assert landscape.get_by_text("只读访问", exact=True).count() == 1
    assert landscape.get_by_role("button", name="任务", exact=True).count() == 0
    landscape_context.close()

    print(
        {
            "desktop_meetings": meeting_count,
            "desktop_seek_seconds": round(current_time, 3),
            "desktop_drag_seconds": round(dragged_time, 3),
            "whisper_enabled": whisper_enabled,
            "comparison_rows": comparison_count,
            "copied_path": copied_path,
            "desktop_transport_y": round(transport_box["y"], 1),
            "mobile_transport_y": round(mobile_transport_box["y"], 1),
            "mobile_read_only": True,
        }
    )
    browser.close()
