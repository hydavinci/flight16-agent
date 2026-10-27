import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from tests.helpers import tool_message
from tests.test_general_orchestration import register_mass, shell
from tests.test_general_runtime import proposal
from tests.test_verification_runtime import confirm, runtime_fixture
from tests.test_verification_runtime import proposal as verification_proposal
from tests.test_web import web_module

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1",
    reason="Browser tests are opt-in",
)


@contextmanager
def browser_page(project, runtime, *, width=1280):
    with (
        TestClient(
            web_module().create_web_app(
                project,
                verification_runtime=runtime,
            ),
            base_url="http://127.0.0.1",
        ) as client,
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
                request.method,
                request.url,
                headers=request.headers,
                content=request.post_data,
            )
            route.fulfill(
                status=response.status_code,
                headers=dict(response.headers),
                body=response.content,
            )

        page.route("http://127.0.0.1/**", route_request)
        try:
            page.goto("http://127.0.0.1")
            yield page
            assert errors == []
        finally:
            browser.close()


def test_browser_reads_historical_knowledge_without_a_directory(project, tmp_path):
    runtime, _ = shell(
        tmp_path,
        [
            tool_message(
                "plan_intent",
                {
                    "action": "answer",
                    "text": "增益裕度表示距离临界稳定的增益余量。",
                },
            )
        ],
    )
    saved = runtime.submit(None, "增益裕度是什么？", scenario="auto")
    with browser_page(project, runtime) as page:
        page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
        expect(page.locator("#scenario")).to_have_count(0)
        expect(page.locator("#conversation-status")).to_have_text("已完成")
        expect(page.locator("#messages")).to_contain_text("距离临界稳定")
        expect(page.locator("#baseline-card")).not_to_be_visible()
        expect(page.locator("#project-root")).to_have_value("")
        assert runtime.store.recent()[0].task is None


def test_browser_historical_verification_confirmation_revision_and_reuse(project, tmp_path):
    runtime, backend, _, root = runtime_fixture(
        tmp_path,
        [verification_proposal(), verification_proposal(threshold=4)],
    )
    saved = runtime.submit(None, "Verify synthetic pitch step.", str(root))
    with browser_page(project, runtime) as page:
        page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
        expect(page.locator("#baseline")).to_contain_text("v1")
        expect(page.locator("#confirm")).to_be_disabled()
        expect(page.locator("#baseline")).to_contain_text("历史记录未提供来源")
        assert not backend.calls
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#evaluation")).to_contain_text("PASS")
        page.reload()
        expect(page.locator("#conversation-status")).to_have_text("已完成")
        page.get_by_label("任务需求", exact=True).fill("Change only the overshoot threshold to 4%.")
        page.locator("#send").click()
        expect(page.locator("#baseline")).to_contain_text("v2")
        expect(page.locator("#trusted")).not_to_be_checked()
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#evaluation")).to_contain_text("FAIL")
        assert len(backend.calls) == 1
        page.locator("#new-conversation").click()
        expect(page.locator("#trusted")).not_to_be_checked()
        page.locator("#conversation-history button").first.click()
        expect(page.locator("#evaluation")).to_contain_text("FAIL")


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_historical_generic_baseline_outputs_without_f16_fields(project, tmp_path, width):
    runtime, _ = shell(
        tmp_path,
        [
            tool_message(
                "plan_intent",
                {
                    "action": "plan",
                    "text": "汇总质量。",
                    "steps": [
                        {
                            "workflow": "mass_summary",
                            "request": "Sum synthetic masses.",
                            "inputs": {"components": [2.0, 3.5]},
                        }
                    ],
                },
            ),
            tool_message("propose_general", proposal().model_dump(mode="json")),
        ],
    )
    calls = []
    register_mass(runtime, calls)
    saved = runtime.submit(None, "Sum synthetic masses 2 and 3.5 kg.", scenario="auto")
    with browser_page(project, runtime, width=width) as page:
        if width < 820:
            page.locator("#history-panel > summary").click()
        page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
        expect(page.locator("#confirm")).to_be_visible()
        expect(page.locator("#baseline-heading")).to_have_text("执行方案")
        expect(page.locator("#baseline")).to_contain_text("mass_summary")
        expect(page.locator("#baseline")).to_contain_text("2.3")
        expect(page.locator("#baseline")).not_to_contain_text("配平点")
        expect(page.locator("#trust-text")).not_to_contain_text("工程代码可信")
        assert not calls
        page.locator("#trusted").check()
        page.locator("#confirm").click()
        expect(page.locator("#conversation-status")).to_have_text("已完成")
        expect(page.locator("#evaluation")).to_contain_text("5.5")
        expect(page.locator("#evaluation")).not_to_contain_text("PASS")
        expect(page.locator("#artifacts")).to_contain_text("execution_record.json")
        assert calls == [[2.0, 3.5]]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_browser_resource_location_change_does_not_grant_or_forward_authorization(
    project, tmp_path
):
    runtime, _, _, root = runtime_fixture(tmp_path)
    saved = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    with browser_page(project, runtime) as page:
        page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
        expect(page.locator("#evaluation")).to_contain_text("PASS")
        page.locator("#project-root").fill("/old/project")
        page.locator("#output-root").fill("/old/output")
        page.locator("#resource-location").select_option("diagnostic_data")
        expect(page.locator("#project-root")).to_have_value("")
        expect(page.locator("#trusted")).not_to_be_checked()
        expect(page.locator("#project-hint")).to_contain_text("agent")
        assert runtime.store.get(saved.id).task.id == saved.task.id
        assert len(runtime.store.recent()) == 1
