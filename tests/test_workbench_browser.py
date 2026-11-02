import json
import os
import re
import time
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


def browser_record(run_id, *, status="completed", artifacts=None):
    completed = status == "completed"
    return {
        "id": run_id,
        "request_sha256": "a" * 64,
        "status": status,
        "stage": status,
        "created_at": "2026-11-02T00:00:00Z",
        "updated_at": "2026-11-02T00:00:00Z",
        "filename": "analysis.m",
        "kind": "script",
        "matlab_version": "test-engine" if completed else None,
        "result": {"value": 6} if completed else None,
        "error": None,
        "log": "controlled browser log",
        "artifacts": artifacts or [],
    }


def install_workbench_api(page, handler):
    def route_api(route):
        request = route.request
        if request.url.endswith("/api/workbench/bootstrap"):
            route.fulfill(
                json={
                    "token": "browser-test-token",
                    "worker_configured": True,
                    "worker_error": None,
                }
            )
            return
        handler(route)

    page.route("**/api/workbench/**", route_api)


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


def test_file_and_parameter_byte_limits_accept_boundary_and_reject_one_over(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        requests = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                route.fulfill(json=[])
                return
            payload = request.post_data_json
            requests.append(payload)
            route.fulfill(status=202, json=browser_record(payload["id"]))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            page.locator("#trusted").check()
            page.set_input_files(
                "#file",
                {"name": "analysis.m", "mimeType": "text/plain", "buffer": b"x" * 1_000_000},
            )
            page.locator("#run-button").click()
            expect(page.locator("#execution-status")).to_have_text("执行完成")
            assert len(requests) == 1

            page.set_input_files(
                "#file",
                {
                    "name": "analysis.m",
                    "mimeType": "text/plain",
                    "buffer": b"x" * 1_000_001,
                },
            )
            page.locator("#run-button").click()
            expect(page.locator("#notice")).to_contain_text("1 MB")
            assert len(requests) == 1

            page.set_input_files("#file", source_file())
            page.locator("#parameters").fill(json.dumps({"x": "a" * 99_992}))
            previous_id = page.locator("#run-id").text_content()
            page.locator("#run-button").click()
            expect(page.locator("#run-id")).not_to_have_text(previous_id)
            assert len(requests) == 2

            page.locator("#parameters").fill(json.dumps({"x": "a" * 99_993}))
            page.locator("#run-button").click()
            expect(page.locator("#notice")).to_contain_text("100 KB")
            assert len(requests) == 2
        finally:
            browser.close()


def test_timeout_limits_and_generated_run_id_are_enforced(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """
            window.__cryptoCalls = 0;
            Object.defineProperty(crypto, "getRandomValues", {
              configurable: true,
              value: (array) => {
                const offset = window.__cryptoCalls * array.length;
                window.__cryptoCalls += 1;
                for (let index = 0; index < array.length; index += 1) {
                  array[index] = offset + index;
                }
                return array;
              },
            });
            """
        )
        requests = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                route.fulfill(json=[])
                return
            payload = request.post_data_json
            requests.append(payload)
            route.fulfill(status=202, json=browser_record(payload["id"]))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            page.set_input_files("#file", source_file())
            page.locator("#trusted").check()
            for value in ("0", "601", "1.5"):
                page.locator("#timeout").fill(value)
                page.locator("#run-button").click()
                expect(page.locator("#notice")).to_contain_text("1 到 600")
            assert requests == []
            page.evaluate("window.__cryptoCalls = 0")

            for value in ("1", "600"):
                page.locator("#timeout").fill(value)
                previous_id = page.locator("#run-id").text_content()
                page.locator("#run-button").click()
                expect(page.locator("#run-id")).not_to_have_text(previous_id)

            assert [request["timeout_seconds"] for request in requests] == [1, 600]
            assert [request["id"] for request in requests] == [
                "000102030405060708090a0b0c0d0e0f",
                "101112131415161718191a1b1c1d1e1f",
            ]
            assert all(re.fullmatch(r"[0-9a-f]{32}", request["id"]) for request in requests)
            assert page.evaluate("window.__cryptoCalls") == 2
        finally:
            browser.close()


def test_polling_recovers_from_transient_http_error_and_stops_at_terminal(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        run_id = []
        status_times = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                route.fulfill(json=[])
            elif request.method == "POST":
                run_id.append(request.post_data_json["id"])
                route.fulfill(status=202, json=browser_record(run_id[0], status="queued"))
            else:
                status_times.append(time.monotonic())
                if len(status_times) == 1:
                    route.fulfill(status=503, json={"detail": "temporary status failure"})
                elif len(status_times) == 2:
                    route.fulfill(json=browser_record(run_id[0], status="running"))
                else:
                    route.fulfill(json=browser_record(run_id[0]))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            submit(page)
            expect(page.locator("#notice")).to_contain_text("temporary status failure")
            expect(page.locator("#execution-status")).to_have_text("执行完成", timeout=5000)
            assert len(status_times) == 3
            assert all(
                later - earlier >= 0.7 for earlier, later in zip(status_times, status_times[1:])
            )
            page.wait_for_timeout(1000)
            assert len(status_times) == 3
        finally:
            browser.close()


def test_initial_history_failure_keeps_worker_available_and_submission_polling(
    tmp_path, monkeypatch
):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        run_id = []
        history_requests = []
        status_requests = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                history_requests.append(request)
                if len(history_requests) == 1:
                    route.fulfill(status=503, json={"detail": "initial history unavailable"})
                else:
                    route.fulfill(json=[])
            elif request.method == "POST":
                run_id.append(request.post_data_json["id"])
                route.fulfill(status=202, json=browser_record(run_id[0], status="queued"))
            else:
                status_requests.append(request)
                route.fulfill(json=browser_record(run_id[0]))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            expect(page.locator("#worker-status")).to_contain_text("已配置")
            expect(page.locator("#notice")).to_contain_text("initial history unavailable")
            expect(page.locator("#run-button")).to_be_enabled()
            submit(page)
            expect(page.locator("#execution-status")).to_have_text("执行完成", timeout=5000)
            assert len(run_id) == 1
            assert len(status_requests) == 1
        finally:
            browser.close()


def test_post_submit_history_failure_does_not_block_terminal_polling(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        run_id = []
        history_requests = []
        status_requests = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                history_requests.append(request)
                if len(history_requests) == 1:
                    route.fulfill(json=[])
                else:
                    route.fulfill(status=503, json={"detail": "post-submit history unavailable"})
            elif request.method == "POST":
                run_id.append(request.post_data_json["id"])
                route.fulfill(status=202, json=browser_record(run_id[0], status="queued"))
            else:
                status_requests.append(request)
                route.fulfill(json=browser_record(run_id[0]))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            submit(page)
            expect(page.locator("#notice")).to_contain_text("post-submit history unavailable")
            expect(page.locator("#execution-status")).to_have_text("执行完成", timeout=5000)
            assert len(run_id) == 1
            assert len(status_requests) == 1
        finally:
            browser.close()


def test_selecting_history_cancels_active_run_polling(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        active_id = []
        history_id = "3" * 32
        active_status_requests = []

        def handler(route):
            request = route.request
            if request.method == "POST":
                active_id.append(request.post_data_json["id"])
                route.fulfill(status=202, json=browser_record(active_id[0], status="queued"))
            elif request.url.endswith("/runs"):
                route.fulfill(json=[browser_record(history_id)])
            elif active_id and request.url.endswith("/" + active_id[0]):
                active_status_requests.append(request)
                route.fulfill(json=browser_record(active_id[0], status="running"))
            else:
                route.fulfill(json=browser_record(history_id))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            submit(page)
            expect(page.locator(f'#history button[data-id="{history_id}"]')).to_be_visible()
            page.locator(f'#history button[data-id="{history_id}"]').click()
            expect(page.locator("#run-id")).to_have_text(history_id)
            page.wait_for_timeout(1000)
            assert active_status_requests == []
        finally:
            browser.close()


def test_page_exit_event_cancels_active_run_polling(tmp_path, monkeypatch):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        run_id = []
        status_requests = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                route.fulfill(json=[])
            elif request.method == "POST":
                run_id.append(request.post_data_json["id"])
                route.fulfill(status=202, json=browser_record(run_id[0], status="queued"))
            else:
                status_requests.append(request)
                route.fulfill(json=browser_record(run_id[0], status="running"))

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            submit(page)
            expect(page.locator("#execution-status")).to_have_text("执行中")
            assert len(status_requests) == 1
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            page.wait_for_timeout(1000)
            assert len(status_requests) == 1
        finally:
            browser.close()


@pytest.mark.parametrize("failure", ["record", "artifact", "http"])
def test_malformed_and_http_responses_are_explicit_without_success_shape(
    tmp_path, monkeypatch, failure
):
    with (
        workbench_server(tmp_path, monkeypatch, configured=False) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        run_id = []

        def handler(route):
            request = route.request
            if request.method == "GET" and request.url.endswith("/runs"):
                route.fulfill(json=[])
            elif request.method == "POST" and failure == "http":
                route.fulfill(status=503, json={"detail": "controlled HTTP failure"})
            elif request.method == "POST" and failure == "record":
                route.fulfill(status=202, json={"id": "malformed"})
            elif request.method == "POST":
                run_id.append(request.post_data_json["id"])
                route.fulfill(
                    status=202,
                    json=browser_record(
                        run_id[0],
                        artifacts=[
                            {
                                "path": "figure-1.png",
                                "sha256": "b" * 64,
                                "size": 4,
                                "media_type": "image/png",
                                "role": "figure",
                            }
                        ],
                    ),
                )
            else:
                route.fulfill(headers={"content-type": "image/png"}, body=b"bad")

        install_workbench_api(page, handler)
        try:
            page.goto(url + "/workbench")
            submit(page)
            expected = {
                "record": "运行记录无效",
                "artifact": "清单不一致",
                "http": "controlled HTTP failure",
            }[failure]
            expect(page.locator("#notice")).to_contain_text(expected)
            expect(page.locator("#figures img")).to_have_count(0)
            if failure != "artifact":
                expect(page.locator("#result-data")).to_be_empty()
                expect(page.locator("#result-download")).to_be_disabled()
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
