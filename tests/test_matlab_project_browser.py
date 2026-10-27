import json
import os

import pytest
from docx import Document
from playwright.sync_api import expect, sync_playwright

from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.local_matlab import LocalMatlabBackend
from flight_agent.verification.planner import VerificationPlanner
from flight_agent.verification.plugins import register_builtin_plugins
from tests.agent_helpers import delivery_draft, make_agent_web
from tests.helpers import serve_app, tool_message

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in",
)


def local_web(tmp_path, monkeypatch):
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Browsing and synthetic fixtures must never invoke MATLAB")

    monkeypatch.setattr(LocalMatlabBackend, "_spawn", forbidden)
    monkeypatch.setattr(LocalMatlabBackend, "execute", forbidden)
    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    register_builtin_plugins(
        CapabilityRegistry(agent_mode=True), runtime.resources,
        backend=None, planner=VerificationPlanner(),
    )
    return app, runtime, calls


@pytest.mark.parametrize("width", [1280, 390])
def test_local_default_static_browsing_unavailable_engine_and_saved_selection(
    tmp_path, monkeypatch, width,
):
    app, runtime, calls = local_web(tmp_path, monkeypatch)
    saved = runtime.store.create()
    saved.resource_location = "diagnostic_data"
    runtime.store.save(saved)
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.get_by_label("任务需求", exact=True)).to_be_visible()
            expect(page.get_by_label("评价指标（可选）", exact=True)).to_be_visible()
            expect(page.locator("#resource-location")).to_have_value("matlab_project")
            page.locator("#history-panel").evaluate("(node) => node.open = true")
            page.locator(".environment > summary").click()
            expect(page.locator("#local-matlab-state")).to_contain_text("不可用")
            expect(page.locator("#local-matlab-state")).to_contain_text("静态发现")
            page.locator("#project-root").fill(str(tmp_path))
            page.locator("#browse-project").click()
            expect(page.locator("#directory-picker")).to_be_visible()
            expect(page.locator("#directory-host")).to_contain_text("Agent")
            expect(page.locator("#directory-address")).to_have_value(str(tmp_path))
            page.locator("#directory-choose").click()
            expect(page.locator("#project-root")).to_have_value(str(tmp_path))
            assert len(runtime.store.recent()) == 1
            assert calls == []
            page.evaluate(
                "(id) => localStorage.setItem('verification-conversation', id)", saved.id,
            )
            page.reload()
            expect(page.locator("#resource-location")).to_have_value("diagnostic_data")
            page.locator("#new-conversation").click()
            expect(page.locator("#resource-location")).to_have_value("matlab_project")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("legacy_status", ["missing", "null"])
def test_legacy_bootstrap_keeps_history_and_knowledge_chat_usable(
    tmp_path, monkeypatch, legacy_status,
):
    app, runtime, calls = local_web(tmp_path, monkeypatch)
    saved = runtime.store.create()
    saved.resource_location = "diagnostic_data"
    runtime.store.save(saved)
    runtime.planner.model.responses = [
        tool_message("propose_agent", {"action": "answer", "text": "Knowledge answer."}),
    ]
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def legacy_bootstrap(route):
            response = route.fetch()
            data = response.json()
            if legacy_status == "missing":
                data.pop("local_matlab")
            else:
                data["local_matlab"] = None
            route.fulfill(response=response, json=data)

        def legacy_locations(route):
            response = route.fetch()
            route.fulfill(response=response, json=[
                item for item in response.json() if item["id"] != "matlab_project"
            ])

        page.route("**/api/bootstrap", legacy_bootstrap)
        page.route("**/api/resource-locations", legacy_locations)
        try:
            page.goto(url)
            expect(page.locator("#local-matlab-state")).to_contain_text("状态未知")
            expect(page.locator("#local-matlab-state")).to_contain_text("重启后端")
            expect(page.locator("#notice")).not_to_contain_text("初始化失败")
            expect(page.locator("#resource-location")).to_have_value("")
            page.locator("#history-panel").evaluate("(node) => node.open = true")
            page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
            expect(page.locator("#resource-location")).to_have_value("diagnostic_data")
            page.locator("#new-conversation").click()
            expect(page.locator("#resource-location")).to_have_value("")
            page.locator("#message").fill("Explain a step response without running a project.")
            page.locator("#send").click()
            expect(page.locator("#messages")).to_contain_text("Knowledge answer.")
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            assert calls == []
            assert errors == []
        finally:
            browser.close()


def test_structured_sources_require_consent_and_download_real_json_and_word(
    tmp_path, monkeypatch,
):
    app, runtime, calls = local_web(tmp_path, monkeypatch)
    saved = runtime.store.create()
    analysis = "  Analyze supplied synthetic arrays; deliver JSON and Word.  "
    criteria = " Require RMSE <= 0.8 m. "
    canonical = (
        "[工程分析要求]\n  Analyze supplied synthetic arrays; deliver JSON and Word.  \n"
        "[工程分析要求结束]\n\n[评价指标]\n Require RMSE <= 0.8 m. \n[评价指标结束]"
    )
    source = {
        "kind": "user_message", "reference": f"message:{saved.id}:0", "quote": criteria,
    }
    runtime.planner.model.responses = [tool_message("propose_agent", {
        "action": "plan", "text": "Confirm sourced synthetic delivery.",
        "plan": delivery_draft(source=source),
    })]
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(10000)
        posted = []
        page.on("request", lambda request: posted.append(request.post_data_json)
                if request.method == "POST" and request.url.endswith("/messages") else None)
        try:
            page.goto(url)
            page.locator(f'#conversation-history button[data-id="{saved.id}"]').click()
            expect(page.locator("#conversation-status")).to_have_text("等待确认或补充")
            page.locator("#message").fill(analysis)
            page.locator("#evaluation-criteria").fill(criteria)
            page.locator("#send").click()
            expect(page.locator("#baseline")).to_contain_text(source["reference"])
            expect(page.locator("#messages")).to_contain_text(canonical)
            expect(page.locator("#message")).to_have_value("")
            expect(page.locator("#evaluation-criteria")).to_have_value("")
            assert posted == [{"analysis_request": analysis, "evaluation_criteria": criteria}]
            assert runtime.store.get(saved.id).messages[0].content == canonical
            expect(page.locator("#confirm")).to_be_disabled()
            assert calls == []
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            expect(page.locator("#evaluation .verdict")).to_have_text("FAIL")
            expect(page.locator("#evaluation")).to_contain_text(criteria.strip())
            assert len(calls) == 1
            with page.expect_download() as word:
                page.locator("#artifacts .artifact-report").click()
            text = "\n".join(p.text for p in Document(word.value.path()).paragraphs)
            assert source["reference"] in text and criteria in text and "FAIL" in text
            page.locator("#artifacts details > summary").click()
            with page.expect_download() as report:
                page.locator("#artifacts button").filter(has_text="result.json").click()
            data = json.loads(report.value.path().read_text("utf-8"))
            assert criteria in json.dumps(data, ensure_ascii=False)
            assert source["reference"] in json.dumps(data)
        finally:
            browser.close()


def test_rejected_request_keeps_both_fields_and_no_criteria_uses_legacy_payload(
    tmp_path, monkeypatch,
):
    app, runtime, calls = local_web(tmp_path, monkeypatch)
    runtime.planner.model.responses = [
        tool_message("propose_agent", {"action": "answer", "text": "Knowledge only."}),
    ]
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(10000)
        bodies = []

        def reject(route):
            if route.request.method != "POST":
                route.continue_()
                return
            bodies.append(route.request.post_data_json)
            route.fulfill(status=422, json={"detail": "Controlled rejection"})

        try:
            page.goto(url)
            page.route("**/api/conversations", reject)
            page.locator("#message").fill("  Keep original text.  ")
            page.locator("#evaluation-criteria").fill(" Require a limit. ")
            page.locator("#send").click()
            expect(page.locator("#notice")).to_contain_text("Controlled rejection")
            expect(page.locator("#message")).to_have_value("  Keep original text.  ")
            expect(page.locator("#evaluation-criteria")).to_have_value(" Require a limit. ")
            assert bodies == [{
                "analysis_request": "  Keep original text.  ",
                "evaluation_criteria": " Require a limit. ",
                "location": "matlab_project",
            }]
            assert runtime.store.recent() == []
            page.unroute("**/api/conversations", reject)
            page.locator("#evaluation-criteria").fill("")
            with page.expect_request(
                lambda request: request.method == "POST"
                and request.url.endswith("/api/conversations")
            ) as accepted:
                page.locator("#send").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            assert accepted.value.post_data_json == {
                "message": "  Keep original text.  ", "location": "matlab_project",
            }
            expect(page.locator("#message")).to_have_value("")
            assert calls == []
        finally:
            browser.close()
