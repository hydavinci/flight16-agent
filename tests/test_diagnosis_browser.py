import os

import pytest
from playwright.sync_api import expect, sync_playwright

from flight_agent.web import create_web_app
from tests.diagnosis_helpers import EvolvingPlanner
from tests.helpers import serve_app
from tests.test_diagnosis_runtime import confirm, propose, runtime_fixture

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in"
)


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_d2_runs_adaptive_rounds_and_shows_cited_unknowns(project, tmp_path, width):
    runtime, _, backend, root = runtime_fixture(tmp_path, EvolvingPlanner())
    with (
        serve_app(create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            page.get_by_label("任务类型", exact=True).select_option("d2")
            expect(page.locator("#scenario-boundary")).to_contain_text("离线只读")
            expect(page.locator("#scenario-boundary")).to_contain_text("不主动补跑 Simulink")
            page.get_by_label("诊断数据目录", exact=True).fill(str(root))
            page.get_by_label("诊断需求", exact=True).fill("Investigate the pitch mismatch.")
            page.locator("#send").click()
            expect(page.locator("#baseline-heading")).to_have_text("诊断基线")
            expect(page.locator("#confirm")).to_be_visible()
            expect(page.locator("#confirm")).to_be_disabled()
            expect(page.locator("#scenario")).to_be_enabled()
            expect(page.locator("#trust-warning")).to_contain_text("不执行源工程代码")
            expect(page.locator("#task-progress [aria-current='step']")).to_contain_text("确认")
            expect(page.locator("#next-action")).to_contain_text("核对")
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            expect(page.locator("#evaluation")).to_contain_text("支持")
            expect(page.locator("#evaluation")).to_contain_text("反驳")
            expect(page.locator("#evaluation")).to_contain_text("未知")
            expect(page.locator("#evaluation")).to_contain_text("缺少必要输入")
            expect(page.locator("#stage")).to_have_text("当前阶段：已交付")
            expect(page.locator("#task-progress [aria-current='step']")).to_contain_text("交付")
            summary = page.locator("#diagnosis-summary")
            expect(summary).to_contain_text("Recorded pitch differs from the reference.")
            expect(summary).to_contain_text("尚无法归属")
            expect(summary).to_contain_text("Verify acquisition timing")
            hypotheses = page.locator("#hypothesis-assessments")
            expect(hypotheses.locator("tr").filter(has_text="alignment")).to_contain_text("低")
            expect(hypotheses.locator("tr").filter(has_text="bias")).to_contain_text("高")
            page.get_by_text("假设演进（1 次修订）", exact=True).click()
            expect(page.locator("#hypothesis-history")).to_contain_text("新增")
            expect(page.locator("#hypothesis-history")).to_contain_text("mean_error")
            task = runtime.store.recent()[0].task
            assert len(task.analysis_runs) == 2
            for run in task.analysis_runs:
                expect(page.locator("#evaluation")).to_contain_text(run.id)
            expect(page.locator("#artifacts")).to_contain_text("diagnosis.docx")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
            assert not backend.calls
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_light_task_first_layout_and_keyboard_controls(project, tmp_path, width):
    runtime, _, _, _ = runtime_fixture(tmp_path)
    with (
        serve_app(create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            assert page.evaluate(
                "getComputedStyle(document.documentElement).backgroundColor"
            ) == "rgb(245, 245, 247)"
            history = page.locator("#history-panel")
            assert history.evaluate("(node) => node.open") is (width > 820)
            assert page.locator("main h1").bounding_box()["y"] < 300
            expect(page.locator("#task-progress [aria-current='step']")).to_contain_text("需求")
            expect(page.locator("#next-action")).to_contain_text("直接描述需求")
            page.get_by_label("任务需求").focus()
            page.keyboard.press("Tab")
            expect(page.locator("#send")).to_be_focused()
            assert page.locator("#send").evaluate(
                "(node) => getComputedStyle(node).outlineStyle"
            ) != "none"
            for selector in (
                "#send", "#new-conversation", "#browse-project", "#scenario",
                "#history-panel > summary",
            ):
                assert page.locator(selector).bounding_box()["height"] >= 44
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
            if width < 820:
                page.locator("#history-panel > summary").click()
                assert history.evaluate("(node) => node.open") is True
        finally:
            browser.close()


@pytest.mark.parametrize("confirmed", [False, True])
def test_browser_followup_planning_does_not_claim_execution(project, tmp_path, confirmed):
    runtime, _, _, root = runtime_fixture(tmp_path)
    previous = propose(runtime, root)
    if confirmed:
        previous = confirm(runtime, previous)
    with (
        serve_app(create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator("#conversation-history button").first.click()
            expect(page.locator("#baseline-heading")).to_have_text("诊断基线")
            queued = runtime.queue_message(previous.id, "Explain the remaining unknowns.")
            assert queued.status == "planning"
            assert queued.task.id == previous.task.id
            page.evaluate("(record) => render(record)", queued.model_dump(mode="json"))
            expect(page.locator("#task-progress [aria-current='step']")).to_contain_text("需求")
            expect(page.locator("#next-action")).to_contain_text("正在处理追问或变更")
            expect(page.locator("#next-action")).not_to_contain_text("正在按已确认")
        finally:
            browser.close()


def test_browser_poll_render_preserves_reading_position_and_expanded_details(project, tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path)
    done = confirm(runtime, propose(runtime, root))
    done.messages.extend([
        done.messages[-1].model_copy(update={"content": f"Recorded message {index}\n" * 6})
        for index in range(20)
    ])
    runtime.store.save(done)
    with (
        serve_app(create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator("#conversation-history button").first.click()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            page.get_by_text("诊断技术记录", exact=True).click()
            details = page.locator("#diagnosis-technical")
            messages = page.locator("#messages")
            assert messages.evaluate("(node) => node.scrollHeight > node.clientHeight")
            messages.evaluate("(node) => { node.scrollTop = 0; }")
            page.evaluate("() => render(JSON.parse(JSON.stringify(current)))")
            assert messages.evaluate("(node) => node.scrollTop") == 0
            assert details.evaluate("(node) => node.open") is True
            page.evaluate("""() => {
                const record = JSON.parse(JSON.stringify(current));
                record.task.stage = "review_evidence";
                record.messages.push({role: "assistant", content: "Another recorded message"});
                render(record);
            }""")
            assert messages.evaluate("(node) => node.scrollTop") == 0
            assert details.evaluate("(node) => node.open") is True
        finally:
            browser.close()


def test_browser_legacy_diagnosis_does_not_invent_a_structured_conclusion(project, tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path)
    done = confirm(runtime, propose(runtime, root))
    done.task.rounds = [
        item.model_copy(update={"review": item.review.model_copy(update={"summary": None})})
        for item in done.task.rounds
    ]
    runtime.store.save(done)
    with (
        serve_app(create_web_app(project, verification_runtime=runtime)) as url,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url)
            page.locator("#conversation-history button").first.click()
            expect(page.locator("#diagnosis-summary")).to_contain_text("结构化诊断未记录")
            expect(page.locator("#diagnosis-summary")).not_to_contain_text("undefined")
            expect(page.locator("#artifacts")).to_contain_text("diagnosis.docx")
        finally:
            browser.close()
