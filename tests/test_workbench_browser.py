import os
from contextlib import contextmanager

import pytest
from playwright.sync_api import expect, sync_playwright

from flight_agent.web import create_web_app
from tests.helpers import serve_app
from tests.test_workbench_web import configured_worker, write_examples

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("RUN_BROWSER_TESTS") != "1",
        reason="Browser tests are opt-in",
    ),
]


@contextmanager
def workbench_server(tmp_path, monkeypatch, *, configured=True):
    write_examples(tmp_path)
    if configured:
        with configured_worker(tmp_path, monkeypatch):
            with serve_app(create_web_app(tmp_path)) as url:
                yield url
    else:
        monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
        monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
        with serve_app(create_web_app(tmp_path)) as url:
            yield url


def source_file(kind="script", *, invalid_utf8=False):
    if invalid_utf8:
        return {"name": "analysis.m", "mimeType": "text/plain", "buffer": b"\xff"}
    source = (
        b"result = struct('value', params.value * 2); plot(1, 1);"
        if kind == "script"
        else (
            b"function result = analysis(params)\n"
            b"result = struct('value', params.value * 2); plot(1, 1);\nend"
        )
    )
    return {"name": "analysis.m", "mimeType": "text/plain", "buffer": source}


def submit(page, kind="script"):
    page.set_input_files("#file", source_file(kind))
    page.locator("#kind").select_option(kind)
    page.locator("#parameters").fill('{"value": 3}')
    page.locator("#trusted").check()
    page.locator("#run-button").click()


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("kind", ["script", "function"])
def test_workbench_runs_file_and_renders_strict_outputs(
    tmp_path, fake_engine, monkeypatch, width, kind
):
    with workbench_server(tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(10000)
        posts = []
        page.on(
            "request",
            lambda request: (
                posts.append(request)
                if request.method == "POST" and request.url.endswith("/api/workbench/runs")
                else None
            ),
        )
        try:
            page.goto(url + "/workbench")
            expect(page).to_have_title("MATLAB 分析工作台")
            expect(page.locator("#run-button")).to_be_enabled()
            submit(page, kind)
            expect(page.locator("#execution-status")).to_have_text("执行完成")
            expect(page.locator("#result-data")).to_contain_text('"value": 6')
            expect(page.locator("#matlab-version")).to_have_text("test-engine")
            expect(page.locator("#figures img")).to_have_count(1)
            expect(page.locator("#execution-log")).to_contain_text("workbench log")
            expect(page.locator("#history")).to_contain_text("analysis.m")
            expect(page.locator("#result-download")).to_be_enabled()
            assert len(posts) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


def test_bootstrap_retains_input_and_unconfigured_worker_stays_disabled(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route(
            "**/api/workbench/bootstrap",
            lambda route: (page.wait_for_timeout(100), route.continue_())[-1],
        )
        try:
            page.goto(url + "/workbench", wait_until="domcontentloaded")
            page.locator("#parameters").fill('{"preserved": true}')
            expect(page.locator("#worker-status")).to_contain_text("不可用")
            expect(page.locator("#parameters")).to_have_value('{"preserved": true}')
            expect(page.locator("#run-button")).to_be_disabled()
            expect(page.locator("#notice")).to_contain_text("MATLAB_WORKER")
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("invalid-json", "JSON"),
        ("untrusted", "可信"),
        ("invalid-utf8", "UTF-8"),
    ],
)
def test_browser_validation_is_explicit_and_never_submits(
    tmp_path, fake_engine, monkeypatch, setup, message
):
    with workbench_server(tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        posts = []
        page.on(
            "request",
            lambda request: (
                posts.append(request)
                if request.method == "POST" and request.url.endswith("/api/workbench/runs")
                else None
            ),
        )
        try:
            page.goto(url + "/workbench")
            page.set_input_files("#file", source_file(invalid_utf8=setup == "invalid-utf8"))
            page.locator("#parameters").fill(
                '{"value":' if setup == "invalid-json" else '{"value": 3}'
            )
            if setup != "untrusted":
                page.locator("#trusted").check()
            page.locator("#run-button").click()
            expect(page.locator("#notice")).to_contain_text(message)
            assert posts == []
        finally:
            browser.close()


def test_failed_run_has_error_without_success_shaped_output(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("TEST_ENGINE_MODE", "execution_error")
    with workbench_server(tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url + "/workbench")
            submit(page)
            expect(page.locator("#execution-status")).to_have_text("执行失败")
            expect(page.locator("#notice")).to_contain_text("original MATLAB solver failure")
            expect(page.locator("#result-data")).to_be_empty()
            expect(page.locator("#figures img")).to_have_count(0)
            expect(page.locator("#result-download")).to_be_disabled()
        finally:
            browser.close()


def test_history_reload_selection_download_and_figure_url_cleanup(
    tmp_path, fake_engine, monkeypatch
):
    with workbench_server(tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """
            window.__createdUrls = [];
            window.__revokedUrls = [];
            const create = URL.createObjectURL.bind(URL);
            const revoke = URL.revokeObjectURL.bind(URL);
            URL.createObjectURL = (blob) => {
              const value = create(blob);
              window.__createdUrls.push(value);
              return value;
            };
            URL.revokeObjectURL = (value) => {
              window.__revokedUrls.push(value);
              revoke(value);
            };
            """
        )
        try:
            page.goto(url + "/workbench")
            submit(page)
            expect(page.locator("#execution-status")).to_have_text("执行完成")
            first_id = page.locator("#run-id").text_content()
            submit(page)
            expect(page.locator("#execution-status")).to_have_text("执行完成")
            expect(page.locator("#history button")).to_have_count(2)
            with page.expect_download() as download:
                page.locator("#result-download").click()
            assert download.value.suggested_filename.endswith("-result.json")
            page.reload()
            expect(page.locator("#history button")).to_have_count(2)
            page.locator("#history button").first.click()
            expect(page.locator("#figures img")).to_have_count(1)
            page.locator(f'#history button[data-id="{first_id}"]').click()
            expect(page.locator("#run-id")).to_have_text(first_id)
            expect(page.locator("#figures img")).to_have_count(1)
            counts = page.evaluate(
                "() => ({created: __createdUrls.length, revoked: __revokedUrls.length})"
            )
            assert counts["created"] >= 2
            assert counts["revoked"] >= 1
        finally:
            browser.close()
