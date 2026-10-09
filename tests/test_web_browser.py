import json
import os

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from tests.helpers import serve_app
from tests.test_web import web_module
from tests.test_worker import TOKEN


def test_web_page_and_assets_exist(project):
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_upload_result_figure_and_undetermined(project, fake_engine, monkeypatch):
    from flight_agent.worker import create_worker_app

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    with serve_app(create_worker_app(project, TOKEN)) as remote:
        monkeypatch.setenv("MATLAB_WORKER_URL", remote)
        with (
            serve_app(web_module().create_web_app(project)) as url,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(url)
                expect(page.get_by_role("heading", name="MATLAB 分析工作台")).to_be_visible()
                page.locator("#file").set_input_files(
                    {
                        "name": "calculation.m",
                        "mimeType": "text/plain",
                        "buffer": b"result = struct('value', params.value * 2);",
                    }
                )
                page.locator("#parameters").fill(json.dumps({"value": 3}))
                page.locator("#trusted").check()
                page.get_by_role("button", name="运行 MATLAB", exact=True).click()
                expect(page.locator("#execution-status")).to_have_text("执行完成", timeout=15000)
                expect(page.locator("#result-data")).to_contain_text('"value": 6')
                expect(page.locator("#execution-log")).to_contain_text("upload log")
                expect(page.locator("#figures img")).to_have_count(1)
                assert page.locator("#figures img").evaluate("(img) => img.naturalWidth") == 2
                page.get_by_role("button", name="Agent 分析", exact=True).click()
                expect(page.locator("#agent-status")).to_have_text(
                    "无法按当前规则判定", timeout=15000
                )
                expect(page.locator("#result-data")).to_contain_text('"value": 6')
                assert errors == []
            finally:
                browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_shows_unconfigured_worker_and_rejects_bad_json(project, monkeypatch):
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            expect(page.locator("#worker-state")).to_contain_text("未配置")
            page.locator("#file").set_input_files(
                {
                    "name": "calculation.m",
                    "mimeType": "text/plain",
                    "buffer": b"result=1;",
                }
            )
            page.locator("#parameters").fill("{bad")
            page.locator("#trusted").check()
            page.get_by_role("button", name="运行 MATLAB", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("JSON")
            page.locator("#parameters").fill("{}")
            page.get_by_role("button", name="运行 MATLAB", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("MATLAB_WORKER")
            expect(page.locator("#execution-status")).not_to_have_text("执行完成")
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_submission_snapshots_file_and_kind_before_reading(project):
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        captured = []

        def capture(route):
            captured.append(route.request.post_data_json)
            route.fulfill(status=503, json={"detail": "Captured test request"})

        page.route("**/api/jobs", capture)
        try:
            page.goto(url)
            page.evaluate("""() => {
                const original = File.prototype.arrayBuffer;
                File.prototype.arrayBuffer = function() {
                    if (this.name !== 'first.m') return original.call(this);
                    return new Promise(resolve => {
                        window.releaseRead = () => original.call(this).then(resolve);
                    });
                };
            }""")
            page.locator("#file").set_input_files(
                {
                    "name": "first.m",
                    "mimeType": "text/plain",
                    "buffer": b"result=1;",
                }
            )
            page.locator("#parameters").fill('{"value":9007199254740993}')
            page.locator("#trusted").check()
            page.get_by_role("button", name="运行 MATLAB", exact=True).click()
            page.wait_for_function("typeof window.releaseRead === 'function'")
            page.locator("#file").set_input_files(
                {
                    "name": "second.m",
                    "mimeType": "text/plain",
                    "buffer": b"result=2;",
                }
            )
            page.locator("#kind").select_option("function")
            page.locator("#parameters").fill('{"value":2}')
            page.evaluate("window.releaseRead()")
            expect(page.locator("#notice")).to_contain_text("Captured test request")
            assert captured[-1]["filename"] == "first.m"
            assert captured[-1]["source"] == "result=1;"
            assert captured[-1]["kind"] == "script"
            assert captured[-1]["parameters"] == {"value": 9007199254740993}
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_submits_raw_parameter_numbers(project):
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        captured = []

        def capture(route):
            if route.request.method == "POST":
                captured.append(json.loads(route.request.post_data))
                route.fulfill(status=503, json={"detail": "Captured test request"})
            else:
                route.continue_()

        page.route("**/api/jobs", capture)
        try:
            page.goto(url)
            page.locator("#file").set_input_files(
                {"name": "calculation.m", "mimeType": "text/plain", "buffer": b"result=params;"}
            )
            page.locator("#parameters").fill(
                ' \n{"value":9007199254740993,"nested":[-9007199254740993,'
                '9007199254740991,1e300,null,true],'
                '"text":"中文 \\"parameters\\":{}, \\"trusted\\":false"}\n '
            )
            page.locator("#trusted").check()
            page.locator("#run-button").click()
            expect(page.locator("#notice")).to_contain_text("Captured test request")
            assert captured[-1]["parameters"] == {
                "value": 9007199254740993,
                "nested": [-9007199254740993, 9007199254740991, 1e300, None, True],
                "text": '中文 "parameters":{}, "trusted":false',
            }
            assert captured[-1]["trusted"] is True
        finally:
            browser.close()


def completed_image_jobs(project):
    from PIL import Image

    from flight_agent.jobs import JobStore
    from flight_agent.upload_models import UploadRequest

    store = JobStore(project / "runs" / "web")
    records = []
    for name in ("first.m", "second.m"):
        record = store.create(
            UploadRequest(
                filename=name,
                source="result=1;",
                parameters={},
                kind="script",
                trusted=True,
            )
        )
        Image.new("RGB", (2, 2)).save(store.directory(record.id) / "figure-1.png")
        store.update(
            record.id,
            status="completed",
            result=1,
            matlab_version="test-engine",
            figures=["figure-1.png"],
        )
        records.append(record)
    return records


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_displays_and_downloads_exact_result_numbers(project):
    from flight_agent.jobs import JobStore

    records = completed_image_jobs(project)
    result = {"value": 9007199254740993, "nested": [-9007199254740993, "中文", None]}
    JobStore(project / "runs" / "web").update(records[0].id, result=result)
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator("#history button").filter(has_text="first.m").click()
            expect(page.locator("#result-data")).to_contain_text('"value": 9007199254740993')
            with page.expect_download() as pending:
                page.locator("#download-result").click()
            download = pending.value
            assert download.suggested_filename == "result.json"
            path = project / "downloaded.json"
            download.save_as(path)
            assert json.loads(path.read_text("utf-8")) == result
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_discards_superseded_result_response(project):
    records = completed_image_jobs(project)
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        delayed = []
        page.route(f"**/api/jobs/{records[0].id}/result", lambda route: delayed.append(route))
        try:
            page.goto(url)
            with page.expect_request(f"**/api/jobs/{records[0].id}/result"):
                page.locator("#history button").filter(has_text="first.m").click()
            expect(page.locator("#download-result")).to_be_disabled()
            page.locator("#history button").filter(has_text="second.m").click()
            expect(page.locator("#job-meta")).to_contain_text("second.m")
            expect(page.locator("#download-result")).to_be_enabled()
            with page.expect_response(f"**/api/jobs/{records[0].id}/result") as response:
                delayed[0].fulfill(status=200, body='{"stale":9007199254740993}')
            response.value.body()
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            expect(page.locator("#job-meta")).to_contain_text("second.m")
            expect(page.locator("#result-data")).to_have_text("1")
            with page.expect_download() as pending:
                page.locator("#download-result").click()
            path = project / "downloaded.json"
            pending.value.save_as(path)
            assert json.loads(path.read_text()) == 1
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_retries_image_after_fetch_failure(project):
    records = completed_image_jobs(project)
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        attempts = []

        def image_route(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.fulfill(status=503, json={"detail": "Temporary image failure"})
            else:
                route.continue_()

        page.route(f"**/api/jobs/{records[0].id}/artifacts/*", image_route)
        try:
            page.goto(url)
            page.locator("#history button").filter(has_text="first.m").click()
            expect(page.locator("#notice")).to_contain_text("Temporary image failure")
            page.locator("#history button").filter(has_text="first.m").click()
            expect(page.locator("#figures img")).to_have_count(1)
            assert len(attempts) == 2
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_discards_old_image_request_after_reselect(project):
    records = completed_image_jobs(project)
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        delayed = []

        def image_route(route):
            if not delayed:
                delayed.append(route)
            else:
                route.continue_()

        page.route(f"**/api/jobs/{records[0].id}/artifacts/*", image_route)
        try:
            page.goto(url)
            page.locator("#history button").filter(has_text="first.m").click()
            expect(page.locator("#execution-status")).to_have_text("执行完成")
            page.locator("#history button").filter(has_text="second.m").click()
            expect(page.locator("#job-meta")).to_contain_text("second.m")
            page.locator("#history button").filter(has_text="first.m").click()
            expect(page.locator("#figures img")).to_have_count(1)
            assert delayed
            with page.expect_response(
                lambda response: records[0].id in response.url and "artifacts" in response.url
            ):
                delayed[0].continue_()
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            assert page.locator("#figures img").count() == 1
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
@pytest.mark.parametrize(
    "parameters",
    ['{"value":1e400}', '{"nested":{"value":-1e400}}', '{"values":[1,1e400]}'],
)
def test_browser_rejects_nonfinite_numbers_without_changing_them_to_null(project, parameters):
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        submitted = []

        def capture(route):
            if route.request.method == "POST":
                submitted.append(route.request.post_data_json)
                route.fulfill(status=503, json={"detail": "Captured test request"})
            else:
                route.continue_()

        page.route("**/api/jobs", capture)
        try:
            page.goto(url)
            page.locator("#file").set_input_files(
                {"name": "calculation.m", "mimeType": "text/plain", "buffer": b"result=params;"}
            )
            page.locator("#parameters").fill(parameters)
            page.locator("#trusted").check()
            page.locator("#run-button").click()
            expect(page.locator("#notice")).to_contain_text("有限数值")
            assert submitted == []
            expect(page.locator("#run-button")).to_be_enabled()

            valid = {"value": 1e300, "empty": None, "items": [0, -2.5, True, "1e400"]}
            page.locator("#parameters").fill(json.dumps(valid))
            page.locator("#run-button").click()
            expect(page.locator("#notice")).to_contain_text("Captured test request")
            assert submitted[-1]["parameters"] == valid
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
@pytest.mark.parametrize("stage", ["source", "parameters"])
@pytest.mark.parametrize("newer_input", ["file", "example", "parameters", "kind"])
def test_browser_stale_example_cannot_overwrite_newer_input(project, stage, newer_input):
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        delayed = []
        old_parameters = {"mass_kg": 10}
        source = "function result = web_metrics(params)\nresult=params;\nend"

        def source_route(route):
            if stage == "source":
                delayed.append(route)
            else:
                route.fulfill(status=200, body=source, content_type="text/plain")

        def parameters_route(route):
            if stage == "parameters":
                delayed.append(route)
            else:
                route.fulfill(status=200, json=old_parameters)

        page.route("**/api/examples/web_metrics.m", source_route)
        page.route("**/api/examples/parameters.json", parameters_route)
        page.route(
            "**/api/examples/web_script.m",
            lambda route: route.fulfill(status=200, body="result=params;"),
        )
        try:
            page.goto(url)
            page.locator("#file").set_input_files(
                {"name": "first.m", "mimeType": "text/plain", "buffer": b"result=params;"}
            )
            page.locator("#parameters").fill('{"value":1}')
            page.locator("#example").select_option("web_metrics.m")
            delayed_path = "web_metrics.m" if stage == "source" else "parameters.json"
            with page.expect_request(f"**/api/examples/{delayed_path}"):
                page.locator("#load-example").click()

            expected_filename = "first.m"
            expected_parameters = {"value": 1}
            expected_kind = "script"
            if newer_input == "file":
                page.locator("#file").set_input_files(
                    {"name": "newest.m", "mimeType": "text/plain", "buffer": b"result=2;"}
                )
                page.locator("#parameters").fill('{"value":2}')
                page.locator("#kind").select_option("script")
                expected_filename = "newest.m"
                expected_parameters = {"value": 2}
            elif newer_input == "example":
                page.locator("#example").select_option("web_script.m")
                page.locator("#load-example").click()
                expect(page.locator("#filename")).to_contain_text("web_script.m")
                expected_filename = "web_script.m"
                expected_parameters = {"frequency_hz": 1, "amplitude": 1, "samples": 200}
            elif newer_input == "parameters":
                page.locator("#parameters").fill('{"value":9}')
                expected_parameters = {"value": 9}
            else:
                page.locator("#kind").select_option("function")
                expected_kind = "function"

            page.locator("#trusted").check()
            assert delayed, "An older example response must still be pending"
            with page.expect_response(f"**/api/examples/{delayed_path}") as response:
                if stage == "source":
                    delayed[0].fulfill(status=200, body=source, content_type="text/plain")
                else:
                    delayed[0].fulfill(status=200, json=old_parameters)
            response.value.body()
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            expect(page.locator("#filename")).to_contain_text(expected_filename)
            assert json.loads(page.locator("#parameters").input_value()) == expected_parameters
            expect(page.locator("#kind")).to_have_value(expected_kind)
            expect(page.locator("#trusted")).to_be_checked()
        finally:
            browser.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in")
def test_browser_ignores_errors_from_superseded_example_requests(project):
    with serve_app(web_module().create_web_app(project)) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        delayed = []
        page.route("**/api/examples/web_script.m", lambda route: delayed.append(route))
        try:
            page.goto(url)
            with page.expect_request("**/api/examples/web_script.m"):
                page.locator("#load-example").click()
            page.locator("#file").set_input_files(
                {"name": "newest.m", "mimeType": "text/plain", "buffer": b"result=2;"}
            )
            assert delayed
            with page.expect_response("**/api/examples/web_script.m") as response:
                delayed[0].fulfill(status=503, json={"detail": "Obsolete example failure"})
            response.value.body()
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            expect(page.locator("#notice")).to_be_hidden()
            expect(page.locator("#filename")).to_contain_text("newest.m")
        finally:
            browser.close()
