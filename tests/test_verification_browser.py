import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.helpers import serve_app, tool_message
from tests.test_verification_runtime import explanation, proposal, runtime_fixture
from tests.test_web import web_module
from tests.verification_helpers import project_tree, verification_module

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in"
)


def test_browser_directory_conversation_confirmation_delivery_and_history(project, tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal(threshold=4)])
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.get_by_role("heading", name="目录验证 Agent")).to_be_visible()
            page.get_by_label("工程目录").fill(str(root))
            page.get_by_label("验证需求").fill("Perform pitch step verification.")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#baseline")).to_contain_text("v1", timeout=10000)
            expect(page.locator("#baseline")).to_contain_text("overshoot")
            expect(page.locator("#baseline")).to_contain_text("10")
            expect(
                page.locator("#baseline tbody tr").filter(has_text="overshoot").locator("td").first
            ).to_have_text("时域响应")
            expect(page.get_by_role("button", name="确认基线并执行")).to_be_disabled()
            assert not backend.calls
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            expect(page.locator("#evaluation")).to_contain_text("PASS")
            expect(page.locator("#results-directory")).to_contain_text("tasks")
            downloads = page.locator("#artifacts button")
            expect(downloads.first).to_be_visible()
            with page.expect_download() as download_info:
                page.locator("#artifacts button").filter(has_text=".docx").first.click()
            download = download_info.value
            assert download.suggested_filename.endswith(".docx")
            assert Path(download.path()).read_bytes().startswith(b"PK")
            page.reload()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=10000)
            expect(page.locator("#messages")).to_contain_text("Perform pitch step verification.")
            page.get_by_label("验证需求").fill("Change only the overshoot threshold to 4%.")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#baseline")).to_contain_text("v2", timeout=10000)
            expect(page.get_by_label("我已核对基线，并确认该工程代码可信")).not_to_be_checked()
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#evaluation")).to_contain_text("FAIL", timeout=15000)
            assert len(backend.calls) == 1
            page.get_by_role("button", name="新建会话", exact=True).click()
            expect(page.locator("#messages")).not_to_contain_text(
                "Perform pitch step verification."
            )
            page.locator("#conversation-history button").first.click()
            expect(page.locator("#evaluation")).to_contain_text("FAIL")
            assert errors == []
        finally:
            browser.close()


def test_browser_chat_directories_refresh_fields_without_resubmitting_stale_paths(
    project, tmp_path
):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    other_root = project_tree(tmp_path / "second source")
    output = tmp_path / "first results"
    other_output = tmp_path / "second results"
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            message = f'工程目录：“{root}”；输出目录：“{output}”。验证阶跃。'
            page.get_by_label("验证需求").fill(message)
            with page.expect_request("**/api/conversations") as posted:
                page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#baseline")).to_contain_text(str(output), timeout=10000)
            expect(page.locator("#baseline")).to_contain_text("Agent 所在机器")
            expect(page.get_by_label("工程目录", exact=True)).to_have_value(str(root))
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value(str(output))
            assert posted.value.post_data_json == {"message": message}
            expect(page.locator("#delivery-card")).to_be_hidden()
            assert not backend.calls
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            message = f'工程目录：“{other_root}”；输出目录：“{other_output}”。验证新工程。'
            page.get_by_label("验证需求").fill(message)
            with page.expect_request("**/api/conversations/*/messages") as posted:
                page.get_by_role("button", name="发送", exact=True).click()
            assert posted.value.post_data_json == {"message": message}
            expect(page.locator("#baseline")).to_contain_text("v2", timeout=10000)
            expect(page.locator("#baseline")).to_contain_text(str(other_output))
            expect(page.get_by_label("工程目录", exact=True)).to_have_value(str(other_root))
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value(str(other_output))
            expect(page.get_by_label("我已核对基线，并确认该工程代码可信")).not_to_be_checked()
            saved = runtime.store.recent()[0]
            assert saved.task.project.root == str(other_root)
            assert saved.task.result is None
            assert len(saved.archived_tasks) == 1
            assert len(backend.calls) == 1
        finally:
            browser.close()


def test_browser_output_selection_reset_and_reload_preserve_numerical_and_historical_evidence(
    project, tmp_path
):
    runtime, backend, _, root = runtime_fixture(
        tmp_path, [proposal(), proposal(threshold=4), proposal(threshold=4)]
    )
    first_output = tmp_path / "first results"
    second_output = tmp_path / "second results"
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            page.get_by_label("工程目录", exact=True).fill(str(root))
            page.get_by_label("输出根目录", exact=True).fill(str(first_output))
            page.get_by_label("验证需求").fill("Verify pitch step.")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#baseline")).to_contain_text(str(first_output), timeout=10000)
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            first = runtime.store.recent()[0]
            report = next(item for item in first.task.artifacts if item.role == "report")
            original_report = (Path(first.task.result_directory) / report.path).read_bytes()
            page.get_by_label("输出根目录", exact=True).fill(str(second_output))
            page.get_by_label("验证需求").fill("Change output and threshold to 4%.")
            with page.expect_request("**/api/conversations/*/messages") as posted:
                page.get_by_role("button", name="发送", exact=True).click()
            assert posted.value.post_data_json == {
                "message": "Change output and threshold to 4%.",
                "output_root": str(second_output),
            }
            expect(page.locator("#baseline")).to_contain_text("v2", timeout=10000)
            expect(page.locator("#baseline")).to_contain_text(str(second_output))
            expect(page.get_by_label("我已核对基线，并确认该工程代码可信")).not_to_be_checked()
            assert len(backend.calls) == 1
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            expect(page.locator("#evaluation")).to_contain_text("FAIL")
            page.reload()
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value(
                str(second_output), timeout=10000
            )
            expect(page.get_by_label("工程目录", exact=True)).to_have_value(str(root))
            with page.expect_download() as downloaded:
                page.locator("#artifacts button").filter(has_text="历史任务").filter(
                    has_text=".docx"
                ).first.click()
            assert Path(downloaded.value.path()).read_bytes() == original_report
            page.get_by_label("输出根目录", exact=True).fill("")
            page.get_by_label("验证需求").fill("Return to the default output directory.")
            with page.expect_request("**/api/conversations/*/messages") as posted:
                page.get_by_role("button", name="发送", exact=True).click()
            assert posted.value.post_data_json == {
                "message": "Return to the default output directory.", "output_root": ""
            }
            expect(page.locator("#baseline")).to_contain_text("v3", timeout=10000)
            expect(page.locator("#baseline")).to_contain_text(str(runtime.store.root))
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            assert len(backend.calls) == 1
            page.get_by_role("button", name="新建会话", exact=True).click()
            expect(page.get_by_label("工程目录", exact=True)).to_have_value("")
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value("")
            page.locator(f'#conversation-history button[data-id="{first.id}"]').click()
            expect(page.get_by_label("工程目录", exact=True)).to_have_value(str(root))
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value("")
            assert len(runtime.store.get(first.id).archived_tasks) == 2
            assert errors == []
        finally:
            browser.close()


def test_browser_unsent_directory_edits_survive_confirmation_and_polling(project, tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path)
    output = tmp_path / "selected results"
    planned = runtime.submit(None, "Verify pitch.", str(root), output_root=str(output))
    unsent_output = tmp_path / "unsent results"
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator(f'#conversation-history button[data-id="{planned.id}"]').click()
            expect(page.locator("#baseline")).to_contain_text(str(output))
            page.get_by_label("输出根目录", exact=True).fill(str(unsent_output))
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value(str(unsent_output))
            expect(page.locator("#results-directory")).to_contain_text(str(output))
            assert not unsent_output.exists()
        finally:
            browser.close()


def test_browser_recovery_plan_adoption_still_requires_fresh_baseline_consent(
    project, tmp_path, monkeypatch
):
    from tests.test_verification_replanning import failed_delivery

    runtime, backend, model, _, failed, deliver = failed_delivery(
        tmp_path, monkeypatch, proposal(threshold=4)
    )
    monkeypatch.setattr(verification_module("runtime"), "deliver_report", deliver)
    model.responses.append(explanation())
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator(f'#conversation-history button[data-id="{failed.id}"]').click()
            expect(page.locator("#conversation-status")).to_have_text("失败")
            expect(page.get_by_role("button", name="续接已有运行", exact=True)).to_be_visible()
            adopt = page.get_by_role("button", name="采纳恢复方案（不执行）", exact=True)
            expect(adopt).to_be_visible()
            expect(adopt).to_be_disabled()
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            adopt.click()
            expect(page.locator("#baseline")).to_contain_text("v2", timeout=10000)
            expect(page.get_by_label("我已核对基线，并确认该工程代码可信")).not_to_be_checked()
            expect(page.get_by_role("button", name="确认基线并执行")).to_be_disabled()
            assert runtime.store.get(failed.id).task.baseline.confirmed is False
            assert len(backend.calls) == 1
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            expect(page.locator("#evaluation")).to_contain_text("FAIL")
            assert len(backend.calls) == 1
        finally:
            browser.close()


def test_browser_interrupted_execution_can_resume_without_duplicate_submission(project, tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Resume the saved worker run."},
        ),
        explanation(),
    ]
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.get_by_label("工程目录").fill(str(root))
            page.get_by_label("验证需求").fill("Verify pitch step.")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#baseline")).to_contain_text("v1", timeout=10000)
            page.get_by_label("我已核对基线，并确认该工程代码可信").check()
            page.get_by_role("button", name="确认基线并执行").click()
            expect(page.get_by_role("button", name="续接已有运行", exact=True)).to_be_visible(
                timeout=15000
            )
            expect(page.locator("#notice")).to_contain_text("Controlled lost acknowledgement")
            page.get_by_role("button", name="续接已有运行", exact=True).click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            assert [resume for _, resume in backend.calls] == [False, True]
            assert backend.calls[0][0].id == backend.calls[1][0].id
        finally:
            browser.close()


def test_browser_renders_model_messages_as_text_and_keeps_failed_input(project, tmp_path):
    runtime, _, model, root = runtime_fixture(tmp_path)
    injection = '<img src=x onerror="window.unsafeRendered=true">'
    model.responses = [
        tool_message("propose_verification", {"action": "clarify", "question": injection})
    ]
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.get_by_label("工程目录").fill(str(root))
            page.get_by_label("验证需求").fill("Clarify the project.")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#messages")).to_contain_text(injection, timeout=10000)
            assert page.locator("#messages img").count() == 0
            assert page.evaluate("window.unsafeRendered === undefined")
            page.route(
                "**/api/conversations/*/messages",
                lambda route: route.fulfill(
                    status=409, json={"detail": "Controlled busy response"}
                ),
            )
            page.get_by_label("验证需求").fill("Keep this request on failure.")
            page.get_by_role("button", name="发送", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("Controlled busy response")
            expect(page.get_by_label("验证需求")).to_have_value("Keep this request on failure.")
        finally:
            browser.close()


def test_browser_rejects_mismatched_conversation_and_clears_old_evidence(project, tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path)
    first = runtime.submit(None, "First verification request.", str(root))
    first = runtime.confirm(first.id, 1, trusted=True)
    second = runtime.store.create()
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route(
            f"**/api/conversations/{second.id}",
            lambda route: route.fulfill(status=200, json=first.model_dump()),
        )
        try:
            page.goto(url)
            page.locator(f'#conversation-history button[data-id="{first.id}"]').click()
            expect(page.locator("#evaluation")).to_contain_text("PASS")
            page.locator(f'#conversation-history button[data-id="{second.id}"]').click()
            expect(page.locator("#notice")).to_contain_text("会话 ID 不匹配")
            expect(page.locator("#messages")).not_to_contain_text("First verification request.")
            expect(page.locator("#evaluation-card")).to_be_hidden()
            expect(page.locator("#delivery-card")).to_be_hidden()
            expect(page.locator("#confirm")).to_be_disabled()
        finally:
            browser.close()


def test_browser_discards_old_selection_response_after_starting_a_new_conversation(
    project, tmp_path
):
    runtime, _, _, root = runtime_fixture(tmp_path)
    saved = runtime.submit(None, "Previous conversation.", str(root))
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        delayed = []
        path = f"**/api/conversations/{saved.id}"
        page.route(path, lambda route: delayed.append(route), times=1)
        try:
            page.goto(url)
            with page.expect_request(path):
                page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
            expect(page.get_by_role("button", name="发送", exact=True)).to_be_disabled()
            page.get_by_role("button", name="新建会话", exact=True).click()
            page.get_by_label("验证需求").fill("New unsent request.")
            page.get_by_label("工程目录", exact=True).fill("/new/project")
            page.get_by_label("输出根目录", exact=True).fill("/new/output")
            with page.expect_response(path):
                delayed.pop().continue_()
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            expect(page.locator("#conversation-status")).to_have_text("新会话")
            expect(page.locator("#messages")).not_to_contain_text("Previous conversation.")
            expect(page.get_by_label("验证需求")).to_have_value("New unsent request.")
            expect(page.get_by_label("工程目录", exact=True)).to_have_value("/new/project")
            expect(page.get_by_label("输出根目录", exact=True)).to_have_value("/new/output")
            expect(page.locator("#baseline-card")).to_be_hidden()
        finally:
            for route in delayed:
                route.abort()
            browser.close()


@pytest.mark.parametrize("action", ["messages", "confirm", "poll"])
def test_browser_rejects_mismatched_updates_to_the_current_conversation(project, tmp_path, action):
    runtime, _, _, root = runtime_fixture(tmp_path)
    first = runtime.submit(None, "Current verification request.", str(root))
    second = runtime.store.create()
    with (
        serve_app(web_module().create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator(f'#conversation-history button[data-id="{first.id}"]').click()
            expect(page.locator("#baseline")).to_contain_text("v1")
            endpoint = f"**/api/conversations/{first.id}"
            if action == "poll":
                running = first.model_copy(update={"status": "running"})
                page.route(
                    f"{endpoint}/confirm",
                    lambda route: route.fulfill(status=202, json=running.model_dump()),
                )
            else:
                endpoint += f"/{action}"
            page.route(endpoint, lambda route: route.fulfill(status=200, json=second.model_dump()))
            if action == "messages":
                page.get_by_label("验证需求").fill("Keep this request.")
                page.get_by_role("button", name="发送", exact=True).click()
            else:
                page.get_by_label("我已核对基线，并确认该工程代码可信").check()
                page.get_by_role("button", name="确认基线并执行").click()
            expect(page.locator("#notice")).to_contain_text("会话 ID 不匹配")
            expect(page.locator("#messages")).to_contain_text("Current verification request.")
            expect(page.locator("#baseline")).to_contain_text("v1")
            if action == "messages":
                expect(page.get_by_label("验证需求")).to_have_value("Keep this request.")
        finally:
            browser.close()
