import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.agent_helpers import draft_data, make_agent_web
from tests.helpers import serve_app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in",
)


@pytest.mark.parametrize("message,display", [
    (
        "[工程分析要求]\n  Analyze <original> arrays.\n[评价指标结束]\nKeep this literal.  \n"
        "[工程分析要求结束]\n\n[评价指标]\n Require RMSE <= 0.8 m. \n[评价指标结束]",
        "[工程分析要求]\n  Analyze <original> arrays.\n[评价指标结束]\nKeep this literal.  \n\n"
        "[评价指标]\n Require RMSE <= 0.8 m. ",
    ),
    (
        "[工程分析要求]\n  Analyze supplied arrays.  \n[工程分析要求结束]",
        "[工程分析要求]\n  Analyze supplied arrays.  ",
    ),
    (
        "Explain literal labels:\n[工程分析要求结束]\n[评价指标结束]",
        "Explain literal labels:\n[工程分析要求结束]\n[评价指标结束]",
    ),
    (
        "[工程分析要求]\nIncomplete example.\n[评价指标结束]",
        "[工程分析要求]\nIncomplete example.\n[评价指标结束]",
    ),
], ids=["analysis-and-criteria", "analysis-only", "literal-text", "incomplete-section"])
def test_chat_hides_only_structured_user_closing_labels_and_keeps_source_text(
    tmp_path, message, display,
):
    reply = "[工程分析要求]\nQuoted example.\n[工程分析要求结束]"
    app, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "answer", "text": reply}],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            page.locator("#message").fill(message)
            page.locator("#send").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            for reload in (False, True):
                if reload:
                    page.reload()
                    expect(page.locator("#conversation-status")).to_have_text("已完成")
                user_text = page.locator('#messages .message[data-role="user"] > span')
                assert user_text.text_content() == display
                assert page.locator(
                    '#messages .message[data-role="assistant"] > span',
                ).text_content() == reply
                expect(page.locator("#messages original")).to_have_count(0)
                assert runtime.store.recent()[0].messages[0].content == message
            assert calls == []
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("path,title", [
    ("/", "Flight Agent"), ("/workbench", "MATLAB 分析工作台"),
])
def test_browser_tab_title_and_local_favicon_render_correctly(tmp_path, path, title):
    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url + path)
            expect(page).to_have_title(title)
            icon = page.locator('head link[rel="icon"]')
            expect(icon).to_have_count(1)
            assert icon.evaluate("(link) => new URL(link.href).origin === location.origin")
            rendered = page.evaluate("""async () => {
                const image = new Image();
                image.src = document.querySelector('link[rel="icon"]').href;
                await image.decode();
                const canvas = document.createElement("canvas");
                canvas.width = canvas.height = 32;
                const context = canvas.getContext("2d");
                context.drawImage(image, 0, 0, 32, 32);
                const pixels = context.getImageData(0, 0, 32, 32).data;
                const colors = new Set();
                for (let i = 0; i < pixels.length; i += 4) {
                    if (pixels[i + 3] === 255) colors.add(pixels.slice(i, i + 3).join(","));
                }
                return {width: image.naturalWidth, height: image.naturalHeight,
                    colors: colors.size};
            }""")
            assert rendered["width"] == rendered["height"] == 32
            assert rendered["colors"] >= 2
            assert runtime.store.recent() == []
            assert calls == []
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_matlab_workbench_return_link_restores_the_selected_agent_conversation(tmp_path, width):
    app, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "plan", "text": "Confirm analysis.", "plan": draft_data()}],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(5000)
        errors, mutations = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            page.locator("#message").fill("Analyze supplied arrays.")
            page.locator("#send").click()
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            saved = runtime.store.recent()[0]
            page.on("request", lambda request: mutations.append(request.method)
                    if request.method not in ("GET", "HEAD", "OPTIONS") else None)
            page.locator("#new-conversation").click()
            if not page.locator("#history-panel").evaluate("(node) => node.open"):
                page.locator("#history-panel > summary").click()
            page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
            expect(page.locator("#messages")).to_contain_text("Analyze supplied arrays.")
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            page.locator(".environment > summary").click()
            page.get_by_role("link", name="MATLAB分析工作台", exact=True).click()
            expect(page).to_have_url(url + "/workbench")
            expect(page.get_by_role("heading", name="MATLAB 分析工作台")).to_be_visible()
            back = page.get_by_role("link", name="返回 Flight Agent", exact=True)
            expect(back).to_be_in_viewport()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            back.click()
            expect(page).to_have_url(url + "/")
            expect(page.locator("#messages")).to_contain_text("Analyze supplied arrays.")
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            expect(page.locator("#conversation-status")).to_be_visible()
            expect(page.locator("#history-panel")).not_to_have_attribute("open", "")
            expect(
                page.locator(f'#conversation-history button[data-id="{saved.id}"]'),
            ).to_have_attribute("aria-current", "true")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert len(runtime.store.recent()) == 1
            assert calls == []
            assert errors == []
            assert mutations == []
        finally:
            browser.close()


@pytest.mark.parametrize("width,height", [(1280, 900), (1440, 720), (1920, 1080)])
def test_desktop_configuration_flanks_chat_and_scrolling_keeps_composer_visible(
    tmp_path, width, height,
):
    app, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "plan", "text": "Confirm analysis.", "plan": draft_data()}],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            project = page.locator("#project-root").bounding_box()
            center = page.locator("main").bounding_box()
            criteria = page.locator("#evaluation-criteria").bounding_box()
            assert project["x"] + project["width"] <= center["x"]
            assert criteria["x"] >= center["x"] + center["width"]
            assert center["width"] > 500
            expect(page.locator("#send")).to_be_in_viewport()
            assert page.evaluate("""() => {
                const form = document.getElementById("composer");
                return ["resource-location", "project-root", "output-root", "evaluation-criteria"]
                    .every((id) => document.getElementById(id).form === form);
            }""")
            page.locator("#message").fill("Analyze supplied arrays.")
            page.locator("#send").click()
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            expect(page.locator("#confirm")).to_be_disabled()
            saved = runtime.store.recent()[0]
            saved.messages.extend(
                saved.messages[0].model_copy(update={"content": f"Recorded request {index}\n" * 8})
                for index in range(24)
            )
            runtime.store.save(saved)
            page.reload()
            expect(page.locator("#messages")).to_contain_text("Recorded request 23")
            expect(page.locator("#confirm")).to_be_disabled()
            assert page.locator("#baseline").evaluate(
                "(node) => !document.querySelector('main').contains(node)",
            )
            messages = page.locator("#messages")
            assert messages.evaluate("(node) => node.scrollHeight > node.clientHeight")
            before = page.locator("#composer").bounding_box()
            messages.evaluate("(node) => { node.scrollTop = 0; }")
            page.locator(".task-sidebar").evaluate(
                "(node) => { node.scrollTop = node.scrollHeight; }",
            )
            assert page.locator("#composer").bounding_box() == before
            expect(page.locator("#send")).to_be_in_viewport()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
            assert calls == []
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("width,height", [(390, 844), (768, 1024), (1024, 768)])
def test_compact_configuration_collapses_and_resizing_preserves_unsent_drafts(
    tmp_path, width, height,
):
    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            expect(page.locator("#resource-panel, #history-panel, #task-panel")).to_have_count(3)
            for panel in ("resource-panel", "history-panel", "task-panel"):
                assert page.locator(f"#{panel}").evaluate("(node) => !node.open")
            assert page.locator("main h1").bounding_box()["y"] < 300
            page.locator("#resource-panel > summary").click()
            page.locator("#project-root").fill("/unsent/project")
            page.locator("#output-root").fill("/unsent/output")
            page.locator("#resource-panel > summary").click()
            page.locator("#task-panel > summary").click()
            page.locator("#evaluation-criteria").fill("Require RMSE <= 0.8 m.")
            page.locator("#task-panel > summary").click()
            page.locator("#message").fill("Keep this unsent request.")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.set_viewport_size({"width": 1280, "height": 900})
            for panel in ("resource-panel", "task-panel"):
                expect(page.locator(f"#{panel}")).to_have_attribute("open", "")
            expect(page.locator("#history-panel")).not_to_have_attribute("open", "")
            expect(page.locator("#project-root")).to_have_value("/unsent/project")
            expect(page.locator("#output-root")).to_have_value("/unsent/output")
            expect(page.locator("#evaluation-criteria")).to_have_value("Require RMSE <= 0.8 m.")
            expect(page.locator("#message")).to_have_value("Keep this unsent request.")
            expect(page.locator("#send")).to_be_in_viewport()
            page.set_viewport_size({"width": width, "height": height})
            expect(page.locator("#resource-panel")).not_to_have_attribute("open", "")
            page.locator("#resource-panel > summary").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#project-root")).to_be_visible()
            expect(page.locator("#project-root")).to_have_value("/unsent/project")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert runtime.store.recent() == []
            assert calls == []
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_result_tables_remain_readable_and_keyboard_scroll_within_the_sidebar(tmp_path, width):
    app, _, calls, _ = make_agent_web(
        tmp_path, [{"action": "plan", "text": "Confirm analysis.", "plan": draft_data()}],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(5000)
        try:
            page.goto(url)
            if width <= 1100:
                page.locator("#task-panel > summary").click()
            page.locator("#message").fill("Analyze supplied arrays.")
            page.locator("#send").click()
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            page.locator("#evaluation details > summary").first.click()
            wrapper = page.locator("#evaluation .table-scroll").first
            assert wrapper.evaluate("(node) => node.scrollWidth > node.clientWidth")
            assert all(
                width >= 90 for width in wrapper.locator("th").evaluate_all(
                    "(cells) => cells.map((cell) => cell.getBoundingClientRect().width)",
                )
            )
            wrapper.focus()
            expect(wrapper).to_be_focused()
            page.keyboard.press("ArrowRight")
            page.wait_for_function(
                "() => document.querySelector('#evaluation .table-scroll').scrollLeft > 0",
            )
            expect(wrapper.locator("tbody")).to_contain_text("RMSE")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert len(calls) == 1
        finally:
            browser.close()


@pytest.mark.parametrize("observed,value", [([0.0, 0.0], "0"), ([0.0, 2.0], "1.41421")])
def test_analysis_metric_cards_show_zero_and_precision_without_inventing_a_verdict(
    tmp_path, observed, value,
):
    plan = draft_data()
    plan["nodes"][0]["inputs"]["observed"] = observed
    app, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "plan", "text": "Confirm analysis.", "plan": plan}],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(5000)
        try:
            page.goto(url)
            page.locator("#message").fill("Analyze supplied arrays without acceptance.")
            page.locator("#send").click()
            expect(page.locator("#confirm")).to_be_disabled()
            expect(page.locator("#baseline-details")).to_have_attribute("open", "")
            expect(page.locator("#conversation-title")).to_have_text("Synthetic RMSE")
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#baseline-details")).not_to_have_attribute("open", "")
            card = page.locator("#evaluation .metric-card")
            expect(card).to_have_count(1)
            expect(card).to_contain_text("RMSE")
            expect(card.locator(".metric-value")).to_have_text(value)
            expect(card.locator(".metric-unit")).to_have_text("m")
            expect(card).to_contain_text("有效")
            expect(page.locator("#evaluation .verdict")).to_have_count(0)
            full = page.get_by_text("观测与数值有效性", exact=True)
            page.get_by_text("完整指标与判定依据", exact=True).click()
            expect(full).to_be_visible()
            expect(page.locator("#evaluation tbody")).to_contain_text(
                "0" if value == "0" else "1.4142135623730951",
            )
            page.locator("#new-conversation").click()
            expect(page.locator("#conversation-title")).to_have_text("工程对话")
            assert len(calls) == 1
        finally:
            browser.close()


def test_long_history_titles_stay_compact_without_losing_the_full_title(tmp_path):
    plan = draft_data()
    plan["title"] = (
        "Analyze the nonlinear engineering response and verify the supplied acceptance criteria."
    )
    app, _, calls, _ = make_agent_web(
        tmp_path, [{"action": "plan", "text": "Confirm analysis.", "plan": plan}],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(5000)
        try:
            page.goto(url)
            page.locator("#message").fill("Analyze supplied arrays.")
            page.locator("#send").click()
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            page.locator("#history-panel > summary").click()
            button = page.locator("#conversation-history button").first
            expect(button).to_contain_text(plan["title"])
            title = button.locator(".history-title")
            assert title.bounding_box()["height"] <= 40
            expect(button).to_have_attribute("title", plan["title"])
            assert calls == []
        finally:
            browser.close()
