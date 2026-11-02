import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.agent_helpers import make_agent_web
from tests.helpers import serve_app

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("RUN_BROWSER_TESTS") != "1",
        reason="Browser tests are opt-in",
    ),
]


def test_browser_assertions_allow_slow_ci_updates():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.set_content('<p id="status">waiting</p>')
            page.evaluate(
                """() => {
                    setTimeout(() => {
                        document.querySelector("#status").textContent = "ready";
                    }, 5500);
                }"""
            )
            expect(page.locator("#status")).to_have_text("ready")
        finally:
            browser.close()


def test_importing_bootstrap_alone_has_no_network_or_handler_side_effects(tmp_path):
    app, _, calls, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route(
            "**/static/agent.js",
            lambda route: route.fulfill(
                status=200,
                content_type="text/javascript",
                body="",
            ),
        )
        try:
            page.goto(url)
            result = page.evaluate(
                """async () => {
                    const requests = [];
                    const handlers = [];
                    const savedFetch = window.fetch;
                    const savedAdd = EventTarget.prototype.addEventListener;
                    window.fetch = (...args) => {
                        requests.push(args);
                        return savedFetch(...args);
                    };
                    EventTarget.prototype.addEventListener = function (...args) {
                        handlers.push(args[0]);
                        return savedAdd.apply(this, args);
                    };
                    try {
                        await import("/static/bootstrap.js?isolated");
                        return {requests: requests.length, handlers};
                    } finally {
                        window.fetch = savedFetch;
                        EventTarget.prototype.addEventListener = savedAdd;
                    }
                }"""
            )
            assert result == {"requests": 0, "handlers": []}
            assert calls == []
        finally:
            browser.close()


def test_native_imports_and_exported_state_transport_interfaces(tmp_path):
    app, _, calls, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors, loaded = [], set()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda result: loaded.add(urlsplit(result.url).path))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            result = page.evaluate(
                """async () => {
                    const {createState, active} = await import("/static/state.js");
                    const {createApi} = await import("/static/api.js");
                    const first = createState();
                    const second = createState();
                    first.current = {status: "queued"};
                    const transport = createApi(() => "interface-test-token");
                    const saved = window.fetch;
                    let captured;
                    window.fetch = async (path, options) => {
                        captured = {path, options};
                        return new Response(JSON.stringify({ok: true}), {
                            status: 200,
                            headers: {"Content-Type": "application/json"},
                        });
                    };
                    try {
                        const payload = await transport.api("/api/example", {
                            method: "POST",
                            body: {message: "literal <text>"},
                        });
                        return {
                            firstActive: active(first),
                            secondActive: active(second),
                            selection: second.selection,
                            pendingView: second.pendingView,
                            captured,
                            payload,
                        };
                    } finally {
                        window.fetch = saved;
                    }
                }"""
            )
            assert result["firstActive"] is True
            assert result["secondActive"] is False
            assert result["selection"] == 0
            assert result["pendingView"] is None
            assert result["captured"]["options"]["headers"]["X-Flight-Token"] == (
                "interface-test-token"
            )
            assert result["captured"]["options"]["method"] == "POST"
            assert result["captured"]["options"]["body"] == (
                '{"message":"literal <text>"}'
            )
            assert result["payload"] == {"ok": True}
            assert {
                "/static/" + name
                for name in (
                    "agent.js",
                    "api.js",
                    "state.js",
                    "render.js",
                    "task-evidence.js",
                    "resize-preferences.js",
                    "bootstrap.js",
                )
            } <= loaded
            assert calls == []
            assert errors == []
        finally:
            browser.close()


def test_late_selected_conversation_response_cannot_restore_old_selection(tmp_path):
    app, runtime, _, _ = make_agent_web(tmp_path, [])
    first = runtime.store.create()
    second = runtime.store.create()
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            page.locator("#history-panel").evaluate("(node) => node.open = true")
            held = []
            page.route(
                f"**/api/conversations/{first.id}/view",
                lambda route: held.append(route),
            )
            page.locator(f'button[data-id="{first.id}"]').click()
            expect(page.locator("#conversation-status")).to_have_text("正在加载")
            page.locator(f'button[data-id="{second.id}"]').click()
            expect(
                page.locator(f'button[data-id="{second.id}"]')
            ).to_have_attribute("aria-current", "true")
            page.wait_for_function(
                "(id) => localStorage.getItem('verification-conversation') === id",
                arg=second.id,
            )
            assert len(held) == 1
            held[0].continue_()
            page.wait_for_load_state("networkidle")
            expect(
                page.locator(f'button[data-id="{second.id}"]')
            ).to_have_attribute("aria-current", "true")
            assert (
                page.evaluate(
                    "localStorage.getItem('verification-conversation')"
                )
                == second.id
            )
            assert errors == []
        finally:
            browser.close()
