import os

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.agent_helpers import (
    criterion_data,
    delivery_draft,
    draft_data,
    make_agent_web,
    peak_current_draft,
    register_peak_current,
)
from tests.helpers import serve_app, tool_message
from tests.test_agent_evaluation import evaluation_plan

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1",
    reason="Browser tests are opt-in",
)


@pytest.mark.parametrize("width", [1280, 390])
def test_single_generic_entry_and_readonly_analysis_without_acceptance(tmp_path, width):
    app, runtime, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Analyze supplied arrays.", "plan": draft_data()},
        ],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.locator("#scenario")).to_have_count(0)
            expect(page.get_by_label("任务需求", exact=True)).to_be_visible()
            expect(page.locator("#baseline-heading")).to_have_text("执行方案")
            page.get_by_label("任务需求", exact=True).fill("Analyze supplied synthetic arrays.")
            page.locator("#send").click()
            expect(page.locator("#confirm")).to_be_visible()
            expect(page.locator("#confirm")).to_be_disabled()
            expect(page.locator("#baseline")).to_contain_text("offline_rmse")
            expect(page.locator("#trust-warning")).not_to_contain_text("工程初始化代码")
            assert not calls
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#evaluation")).to_contain_text("本任务未要求合格判定")
            expect(page.locator("#evaluation")).to_contain_text("RMSE")
            expect(page.locator("#evaluation")).to_contain_text("1.414")
            expect(page.locator("#evaluation")).not_to_contain_text("PASS")
            assert len(calls) == 1
            assert runtime.store.recent()[0].task.schema_version == "2.0"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_failed_check_and_missing_condition_remain_visible_with_sourced_criteria(tmp_path, width):
    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    saved = runtime.store.create()
    message = (
        'Require RMSE <= 1 m and check case unobserved; <img src=x onerror="window.unsafe=true">'
    )
    source = {"kind": "user_message", "reference": f"message:{saved.id}:0", "quote": message}
    plan = evaluation_plan()
    plan.pop("resources")
    plan["resource_ids"] = []
    plan["requirements"]["criteria"] = [
        criterion_data("limit", threshold=1, conditions={}, source=source),
        criterion_data("other-case", threshold=1, conditions={"case": "unobserved"}, source=source),
    ]
    plan["nodes"][1]["inputs"]["criteria"] = plan["requirements"]["criteria"]
    runtime.planner.model.responses = [
        tool_message(
            "propose_agent", {"action": "plan", "text": "Confirm criteria.", "plan": plan}
        ),
    ]
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(10000)
        try:
            page.goto(url)
            if width < 820:
                page.locator("#history-panel > summary").click()
            page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            page.get_by_label("任务需求", exact=True).fill(message)
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text(source["reference"])
            expect(page.locator("#baseline")).to_contain_text("unobserved")
            assert not calls
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#evaluation .verdict")).to_have_text("UNDETERMINED")
            expect(page.locator("#evaluation tbody tr").filter(has_text="FAIL")).to_have_count(1)
            expect(page.locator("#evaluation")).to_contain_text(source["quote"])
            expect(page.locator("#evaluation")).to_contain_text("unobserved")
            assert page.locator("#evaluation img, #baseline img").count() == 0
            assert page.evaluate("window.unsafe === undefined")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert len(calls) == 1
        finally:
            browser.close()


def test_capability_gap_is_displayed_without_execution_and_old_consent_is_reset(tmp_path):
    app, runtime, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Initial arrays.", "plan": draft_data()},
            {"action": "plan", "text": "Revised arrays.", "plan": draft_data()},
            {
                "action": "capability_gap",
                "text": "Cannot compute the requested response.",
                "gaps": [
                    {
                        "requirement_id": "frequency-response",
                        "missing_capability": "frequency response",
                        "missing_inputs": ["frequency grid"],
                        "limitation": "No registered adapter.",
                        "next_step": "Register an authorized capability.",
                    }
                ],
            },
        ],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(10000)
        try:
            page.goto(url)
            page.get_by_label("任务需求", exact=True).fill("Analyze supplied arrays.")
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text("v1")
            page.locator("#trusted").check()
            page.get_by_label("任务需求", exact=True).fill("Revise the requested arrays.")
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text("v2")
            expect(page.locator("#trusted")).not_to_be_checked()
            expect(page.locator("#confirm")).to_be_disabled()
            page.locator("#trusted").check()
            page.locator("#new-conversation").click()
            expect(page.locator("#trusted")).not_to_be_checked()
            page.get_by_label("任务需求", exact=True).fill("Compute a frequency response.")
            page.locator("#send").click()
            expect(page.locator("#messages")).to_contain_text("frequency grid")
            expect(page.locator("#messages")).to_contain_text("Register an authorized capability.")
            expect(page.locator("#baseline-card")).to_be_hidden()
            assert not calls
            assert all(
                not record.task or not record.task.baseline.confirmed
                for record in runtime.store.recent()
            )
        finally:
            browser.close()


def test_accepted_submission_survives_view_failure_without_duplicate_post(tmp_path):
    app, runtime, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Confirm arrays.", "plan": draft_data()},
        ],
    )
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(10000)
        pending = []
        failures = []

        def view_request(route):
            if not failures:
                failures.append(True)
                route.fulfill(status=503, json={"detail": "Controlled temporary view failure"})
            else:
                pending.append(route)
                page.evaluate("window.pendingProjection = true")

        page.route("**/api/conversations/*/view", view_request)
        try:
            page.goto(url)
            page.get_by_label("任务需求", exact=True).fill("Analyze supplied arrays.")
            page.locator("#send").click()
            expect(page.locator("#notice")).to_contain_text("已接收")
            expect(page.locator("#send")).to_be_disabled()
            with page.expect_request("**/api/conversations/*/view"):
                page.locator("#composer").evaluate("(form) => form.requestSubmit()")
            page.wait_for_function("() => window.pendingProjection === true")
            assert len(runtime.store.recent()) == 1
            assert len(pending) == 1
            pending.pop().continue_()
            expect(page.locator("#baseline")).to_contain_text("v1")
            expect(page.get_by_label("任务需求", exact=True)).to_have_value("")
            assert len(runtime.store.recent()) == 1
            assert not calls
        finally:
            for route in pending:
                route.abort()
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_reconfirms_sourced_acceptance_and_runs_another_registered_metric(tmp_path, width):
    from docx import Document

    app, runtime, rmse_calls, tools = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Confirm analysis and JSON.", "plan": delivery_draft()},
        ],
    )
    peak_calls = []
    register_peak_current(tools, peak_calls)
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(10000)
        try:
            page.goto(url)
            page.get_by_label("任务需求", exact=True).fill(
                "Analyze synthetic arrays [0,2] and [0,0]; JSON only, no acceptance.",
            )
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text("v1")
            assert not rmse_calls
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#evaluation")).to_contain_text("本任务未要求合格判定")
            assert len(rmse_calls) == 1
            original = runtime.store.recent()[0]
            message = "Require RMSE <= 0.8 m; deliver JSON and Word."
            source = {
                "kind": "user_message",
                "reference": f"message:{original.id}:{len(original.messages)}",
                "quote": message,
            }
            runtime.planner.model.responses = [
                tool_message(
                    "propose_agent",
                    {
                        "action": "plan",
                        "text": "Confirm sourced acceptance.",
                        "plan": delivery_draft(source=source),
                    },
                )
            ]
            page.get_by_label("任务需求", exact=True).fill(message)
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text("v2")
            expect(page.locator("#baseline")).to_contain_text(source["reference"])
            expect(page.locator("#trusted")).not_to_be_checked()
            expect(page.locator("#confirm")).to_be_disabled()
            assert len(rmse_calls) == 1
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#evaluation .verdict")).to_have_text("FAIL")
            expect(page.locator("#evaluation")).to_contain_text("0.8")
            expect(page.locator("#evaluation")).to_contain_text(source["quote"])
            assert len(rmse_calls) == 1
            assert (
                runtime.store.get(original.id).archived_tasks[0].baseline == original.task.baseline
            )
            with page.expect_download() as downloaded:
                page.locator("#artifacts .artifact-report").click()
            text = "\n".join(
                paragraph.text for paragraph in Document(downloaded.value.path()).paragraphs
            )
            assert "FAIL" in text
            assert source["reference"] in text
            assert message in text
            runtime.planner.model.responses = [
                tool_message(
                    "propose_agent",
                    {
                        "action": "plan",
                        "text": "Confirm peak current.",
                        "plan": peak_current_draft(),
                    },
                )
            ]
            page.locator("#new-conversation").click()
            page.get_by_label("任务需求", exact=True).fill(
                "Report the peak of synthetic current samples [1,-3,2] A, no acceptance.",
            )
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text("sample_peak")
            expect(page.locator("#baseline")).to_contain_text("peak_current")
            assert not peak_calls
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#evaluation")).to_contain_text("Peak current")
            metric = page.locator("#evaluation tbody tr").filter(has_text="Peak current")
            expect(metric.locator("td").nth(1)).to_have_text("3")
            expect(metric.locator("td").nth(2)).to_have_text("A")
            expect(page.locator("#evaluation")).to_contain_text("本任务未要求合格判定")
            assert len(peak_calls) == len(rmse_calls) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()
