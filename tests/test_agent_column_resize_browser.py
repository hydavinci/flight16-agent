import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.agent_helpers import make_agent_web
from tests.helpers import serve_app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in",
)


@pytest.fixture
def resize_page(tmp_path):
    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(5000)
        errors, mutations = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: mutations.append(request.method)
                if request.method not in ("GET", "HEAD", "OPTIONS") else None)
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            yield page
            assert errors == []
            assert mutations == []
            assert runtime.store.recent() == []
            assert calls == []
        finally:
            browser.close()


def drag_column(page, side, delta, *, release=True):
    handle = page.get_by_role("separator", name=f"调整{side}栏宽度", exact=True)
    expect(handle).to_be_visible()
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + delta, y, steps=6)
    if release:
        page.mouse.up()


def test_dragging_both_boundaries_resizes_live_and_preserves_drafts(resize_page):
    page = resize_page
    page.locator("#project-root").fill("/unsent/project")
    page.locator("#message").fill("Keep this draft.")
    page.locator("#evaluation-criteria").fill("Require RMSE <= 0.8 m.")
    drag_column(page, "左", 100, release=False)
    expect(page.locator(".sidebar")).to_have_css("width", "360px")
    expect(page.locator("main")).to_have_css("width", "740px")
    page.mouse.up()
    drag_column(page, "右", -100)
    expect(page.locator(".task-sidebar")).to_have_css("width", "440px")
    expect(page.locator("main")).to_have_css("width", "640px")
    expect(page.locator("#project-root")).to_have_value("/unsent/project")
    expect(page.locator("#message")).to_have_value("Keep this draft.")
    expect(page.locator("#evaluation-criteria")).to_have_value("Require RMSE <= 0.8 m.")
    expect(page.locator("#send")).to_be_in_viewport()


def test_saved_widths_survive_reload_and_responsive_transitions(resize_page):
    page = resize_page
    drag_column(page, "左", 100)
    drag_column(page, "右", -100)
    page.reload()
    expect(page.locator(".sidebar")).to_have_css("width", "360px")
    expect(page.locator(".task-sidebar")).to_have_css("width", "440px")
    page.set_viewport_size({"width": 1160, "height": 900})
    page.wait_for_function(
        "() => document.querySelector('main').getBoundingClientRect().width >= 480",
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("separator")).to_have_count(0)
    expect(page.locator("#resource-panel")).not_to_have_attribute("open", "")
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(page.locator(".sidebar")).to_have_css("width", "360px")
    expect(page.locator(".task-sidebar")).to_have_css("width", "440px")
    expect(page.get_by_role("separator")).to_have_count(2)
    page.reload()
    expect(page.locator(".task-sidebar")).to_have_css("width", "440px")


def test_drag_limits_keep_all_columns_usable(resize_page):
    page = resize_page
    drag_column(page, "左", -1000)
    expect(page.locator(".sidebar")).to_have_css("width", "200px")
    drag_column(page, "右", 1000)
    expect(page.locator(".task-sidebar")).to_have_css("width", "240px")
    drag_column(page, "左", 2000)
    expect(page.locator("main")).to_have_css("width", "480px")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator("#send")).to_be_in_viewport()
    page.reload()
    expect(page.locator("main")).to_have_css("width", "480px")


def test_keyboard_resize_reset_and_cancel_are_local(resize_page):
    page = resize_page
    left = page.get_by_role("separator", name="调整左栏宽度", exact=True)
    right = page.get_by_role("separator", name="调整右栏宽度", exact=True)
    expect(left).to_be_visible()
    left.focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator(".sidebar")).to_have_css("width", "270px")
    right.focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator(".task-sidebar")).to_have_css("width", "350px")
    before = page.evaluate("getComputedStyle(document.body).userSelect")
    drag_column(page, "左", 80, release=False)
    page.keyboard.press("Escape")
    page.mouse.up()
    expect(page.locator(".sidebar")).to_have_css("width", "270px")
    assert page.evaluate("getComputedStyle(document.body).userSelect") == before
    page.reload()
    expect(page.locator(".sidebar")).to_have_css("width", "270px")
    right.dblclick()
    expect(page.locator(".sidebar")).to_have_css("width", "260px")
    expect(page.locator(".task-sidebar")).to_have_css("width", "340px")
    left.focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    expect(page.locator(".sidebar")).to_have_css("width", "260px")
    page.reload()
    expect(page.locator(".sidebar")).to_have_css("width", "260px")


@pytest.mark.parametrize("value", ['{broken', "null", '{"left":"wide","right":340}'])
def test_invalid_width_preferences_do_not_break_startup(resize_page, value):
    page = resize_page
    page.evaluate(
        "(value) => localStorage.setItem('flight-agent-column-widths', value)", value,
    )
    page.reload()
    expect(page.locator("#send")).to_be_enabled()
    expect(page.get_by_role("separator")).to_have_count(2)
    expect(page.locator(".sidebar")).to_have_css("width", "260px")
    expect(page.locator(".task-sidebar")).to_have_css("width", "340px")
    drag_column(page, "左", 40)
    expect(page.locator(".sidebar")).to_have_css("width", "300px")


@pytest.mark.parametrize("method", ["getItem", "setItem"])
def test_unavailable_width_storage_keeps_resizing_usable(resize_page, method):
    page = resize_page
    page.add_init_script(f"""(function() {{
        const method = "{method}";
        const original = Storage.prototype[method];
        Storage.prototype[method] = function(key, ...args) {{
            if (key === "flight-agent-column-widths") {{
                throw new DOMException("Preference storage unavailable", "QuotaExceededError");
            }}
            return original.call(this, key, ...args);
        }};
    }})();""")
    page.reload()
    expect(page.locator("#send")).to_be_enabled()
    drag_column(page, "左", 40)
    expect(page.locator(".sidebar")).to_have_css("width", "300px")


@pytest.mark.parametrize("side,delta", [("左", 30), ("右", -30)])
def test_drag_after_typing_keeps_focus_on_a_short_grip_not_the_full_boundary(
    resize_page, side, delta,
):
    page = resize_page
    page.locator("#message").fill("Keyboard input before pointer resizing.")
    drag_column(page, side, delta)
    handle = page.get_by_role("separator", name=f"调整{side}栏宽度", exact=True)
    expect(handle).to_be_focused()
    expect(handle).to_have_css("outline-style", "none")
    assert handle.evaluate(
        "node => getComputedStyle(node, '::before').backgroundColor",
    ) == "rgba(0, 0, 0, 0)"
    handle.press("ArrowRight")
    grip = handle.evaluate("""node => {
        const style = getComputedStyle(node, "::after");
        return {height: parseFloat(style.height), outline: style.outlineStyle,
                opacity: style.opacity};
    }""")
    assert 20 <= grip["height"] <= 40
    assert grip["outline"] == "solid"
    assert grip["opacity"] == "1"
    expect(page.locator("#message")).to_have_value("Keyboard input before pointer resizing.")


def test_compact_headers_and_single_row_progress_leave_room_for_the_conversation(resize_page):
    page = resize_page
    header = page.locator(".chat-header").bounding_box()
    assert header["height"] <= 76
    assert float(page.locator("main h1").evaluate(
        "node => getComputedStyle(node).fontSize",
    ).removesuffix("px")) <= 18
    positions = page.locator("#task-progress li").evaluate_all(
        "items => items.map(item => item.getBoundingClientRect().top)",
    )
    assert max(positions) - min(positions) <= 1
    expect(page.locator("#send")).to_be_in_viewport()


def test_composer_keyboard_focus_has_a_visible_local_boundary(resize_page):
    page = resize_page
    page.get_by_label("任务需求", exact=True).focus()
    page.keyboard.press("a")
    box = page.locator(".composer-box")
    expect(box).to_have_css("outline-style", "solid")
    outline = box.evaluate("node => getComputedStyle(node).outlineWidth")
    assert float(outline.removesuffix("px")) >= 2
    assert box.bounding_box()["height"] < 200
    expect(page.locator("#message")).to_be_focused()


@pytest.mark.parametrize("width", [1440, 390])
def test_recent_conversations_start_collapsed_and_preserve_manual_state_on_resize(
    resize_page, width,
):
    page = resize_page
    page.set_viewport_size({"width": width, "height": 900})
    history = page.locator("#history-panel")
    expect(history).not_to_have_attribute("open", "")
    other_width = 390 if width == 1440 else 1440
    page.set_viewport_size({"width": other_width, "height": 900})
    expect(page.locator("#resource-panel")).to_have_js_property("open", other_width > 1100)
    expect(history).not_to_have_attribute("open", "")
    page.locator("#history-panel > summary").click()
    expect(history).to_have_attribute("open", "")
    page.set_viewport_size({"width": width, "height": 900})
    expect(page.locator("#resource-panel")).to_have_js_property("open", width > 1100)
    expect(history).to_have_attribute("open", "")
    page.locator("#history-panel > summary").click()
    page.reload()
    expect(page.locator("#send")).to_be_enabled()
    expect(history).not_to_have_attribute("open", "")


@pytest.mark.parametrize("width", [1440, 390])
def test_new_conversation_is_at_the_chat_header_right_edge_and_still_resets_drafts(
    resize_page, width,
):
    page = resize_page
    page.set_viewport_size({"width": width, "height": 900})
    button = page.get_by_role("button", name="新建会话", exact=True)
    expect(button).to_have_count(1)
    expect(page.locator(".chat-header #new-conversation")).to_have_count(1)
    expect(page.locator("#project-sidebar #new-conversation")).to_have_count(0)
    expect(button).to_be_in_viewport()
    header = page.locator(".chat-header").bounding_box()
    bounds = button.bounding_box()
    expect(page.locator("#conversation-status")).to_be_hidden()
    assert 0 <= header["x"] + header["width"] - bounds["x"] - bounds["width"] <= 32
    assert header["y"] <= bounds["y"] < bounds["y"] + bounds["height"] <= (
        header["y"] + header["height"]
    )
    page.locator("#message").fill("Unsent draft.")
    button.click()
    expect(page.locator("#message")).to_have_value("")
    expect(page.locator("#conversation-status")).to_have_text("新会话")
    expect(page.locator("#conversation-status")).to_be_hidden()
    expect(page.locator("#conversation-title")).to_have_text("工程对话")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
