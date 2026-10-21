import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from tests.helpers import tool_message
from tests.test_general_orchestration import register_mass, shell
from tests.test_general_runtime import proposal
from tests.test_verification_runtime import proposal as verification_proposal
from tests.test_verification_runtime import runtime_fixture
from tests.test_web import web_module

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in"
)


@contextmanager
def browser_page(project, runtime, *, width=1280):
    with (
        TestClient(web_module().create_web_app(
            project, verification_runtime=runtime,
        ), base_url="http://127.0.0.1") as client,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            response = client.request(
                request.method, request.url, headers=request.headers, content=request.post_data,
            )
            route.fulfill(
                status=response.status_code, headers=dict(response.headers), body=response.content,
            )

        page.route("http://127.0.0.1/**", route_request)
        try:
            page.goto("http://127.0.0.1")
            yield page
            assert errors == []
        finally:
            browser.close()


def test_browser_auto_answers_knowledge_without_requesting_a_directory(project, tmp_path):
    runtime, _ = shell(tmp_path, [tool_message("plan_intent", {
        "action": "answer", "text": "增益裕度表示距离临界稳定的增益余量。",
    })])
    with browser_page(project, runtime) as page:
        expect(page.locator("#scenario")).to_have_value("auto")
        page.locator("#message").fill("增益裕度是什么？")
        page.locator("#send").click()
        expect(page.locator("#conversation-status")).to_have_text("已完成")
        expect(page.locator("#messages")).to_contain_text("距离临界稳定")
        expect(page.locator("#baseline-card")).not_to_be_visible()
        expect(page.locator("#project-root")).to_have_value("")
        expect(page.locator("#scenario")).to_be_enabled()
        assert runtime.store.recent()[0].task is None


def test_browser_explicit_d1_preserves_confirmation_history_and_numerical_reuse(project, tmp_path):
    runtime, backend, _, root = runtime_fixture(
        tmp_path, [verification_proposal(), verification_proposal(threshold=4)],
    )
    with browser_page(project, runtime) as page:
        page.locator("#scenario").select_option("d1")
        page.get_by_label("工程目录", exact=True).fill(str(root))
        page.get_by_label("验证需求").fill("Verify synthetic pitch step.")
        page.locator("#send").click()
        expect(page.locator("#baseline")).to_contain_text("v1")
        expect(page.locator("#confirm")).to_be_disabled()
        assert not backend.calls
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#evaluation")).to_contain_text("PASS")
        page.reload()
        expect(page.locator("#scenario")).to_have_value("d1")
        page.get_by_label("验证需求").fill("Change only the overshoot threshold to 4%.")
        page.locator("#send").click()
        expect(page.locator("#baseline")).to_contain_text("v2")
        expect(page.locator("#trusted")).not_to_be_checked()
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#evaluation")).to_contain_text("FAIL")
        assert len(backend.calls) == 1
        page.locator("#new-conversation").click()
        expect(page.locator("#scenario")).to_have_value("d1")
        page.locator("#conversation-history button").first.click()
        expect(page.locator("#scenario")).to_have_value("d1")
        expect(page.locator("#evaluation")).to_contain_text("FAIL")


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_renders_registered_generic_baseline_and_outputs_without_d1_fields(
    project, tmp_path, width
):
    runtime, _ = shell(tmp_path, [
        tool_message("plan_intent", {
            "action": "plan", "text": "汇总质量。",
            "steps": [{"workflow": "mass_summary", "request": "Sum synthetic masses.",
                       "inputs": {"components": [2.0, 3.5]}}],
        }),
        tool_message("propose_general", proposal().model_dump(mode="json")),
    ])
    calls = []
    register_mass(runtime, calls)
    with browser_page(project, runtime, width=width) as page:
        page.locator("#message").fill("Sum synthetic masses 2 and 3.5 kg.")
        page.locator("#send").click()
        expect(page.locator("#confirm")).to_be_visible()
        expect(page.locator("#baseline-heading")).to_have_text("任务基线")
        expect(page.locator("#baseline")).to_contain_text("mass_summary")
        expect(page.locator("#baseline")).to_contain_text("2.3")
        expect(page.locator("#baseline")).not_to_contain_text("配平点")
        expect(page.locator("#trust-text")).not_to_contain_text("工程代码可信")
        assert not calls
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#conversation-status")).to_have_text("已完成")
        expect(page.locator("#evaluation-heading")).to_have_text("能力输出")
        expect(page.locator("#evaluation")).to_contain_text("5.5")
        expect(page.locator("#evaluation")).not_to_contain_text("PASS")
        expect(page.locator("#artifacts")).to_contain_text("execution_record.json")
        assert calls == [[2.0, 3.5]]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_browser_can_switch_modes_in_one_conversation_without_forwarding_old_directories(
    project, tmp_path
):
    runtime, _ = shell(tmp_path, [tool_message("plan_intent", {
        "action": "answer", "text": "This is a knowledge answer.",
    })])
    with browser_page(project, runtime) as page:
        page.locator("#message").fill("Explain a concept.")
        page.locator("#send").click()
        expect(page.locator("#conversation-status")).to_have_text("已完成")
        identifier = runtime.store.recent()[0].id
        page.locator("#project-root").fill("/old/project")
        page.locator("#output-root").fill("/old/output")
        page.locator("#scenario").select_option("d2")
        expect(page.locator("#project-root")).to_have_value("")
        expect(page.locator("#output-root")).to_have_value("")
        page.locator("#message").fill("Investigate separate recorded data.")
        page.locator("#send").click()
        expect(page.locator("#messages")).to_contain_text("Agent 可见的已登记诊断数据目录")
        expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
        saved = runtime.store.get(identifier)
        assert saved.scenario == "d2"
        assert saved.project_root is None
        assert saved.output_root is None
        assert len(runtime.store.recent()) == 1


def test_browser_auto_next_stage_browses_agent_data_not_the_old_worker(project, tmp_path):
    next_stage = {"workflow": "d2", "request": "Diagnose separately registered data."}
    runtime, _ = shell(tmp_path, [
        tool_message("plan_intent", {
            "action": "plan", "text": "先汇总，再诊断。",
            "steps": [
                {"workflow": "mass_summary", "request": "Sum masses.",
                 "inputs": {"components": [2.0, 3.5]}},
                next_stage,
            ],
        }),
        tool_message("propose_general", proposal().model_dump(mode="json")),
        tool_message("plan_intent", {
            "action": "plan", "text": "需要独立诊断数据。", "steps": [next_stage],
        }),
    ])
    register_mass(runtime, [])
    with browser_page(project, runtime) as page:
        page.locator("#message").fill("Sum masses, then diagnose.")
        page.locator("#send").click()
        expect(page.locator("#confirm")).to_be_visible()
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
        expect(page.locator("#project-label")).to_have_text("诊断数据目录")
        expect(page.locator("#next-action")).to_contain_text("补充")
        expect(page.locator("#resume")).not_to_be_visible()
        with page.expect_request("**/api/directories?**") as request:
            page.locator("#browse-project").click()
        assert "scenario=d2" in request.value.url
        expect(page.locator("#directory-host")).to_contain_text("Agent 所在机器")
        assert runtime.store.recent()[0].active_workflow == "d2"
