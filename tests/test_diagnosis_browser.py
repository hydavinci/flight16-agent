import os

import pytest
from playwright.sync_api import expect, sync_playwright

from flight_agent.web import create_web_app
from tests.helpers import serve_app
from tests.test_diagnosis_runtime import runtime_fixture

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in"
)


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_d2_runs_adaptive_rounds_and_shows_cited_unknowns(project, tmp_path, width):
    runtime, _, backend, root = runtime_fixture(tmp_path)
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
            page.get_by_label("诊断数据目录", exact=True).fill(str(root))
            page.get_by_label("诊断需求", exact=True).fill("Investigate the pitch mismatch.")
            page.locator("#send").click()
            expect(page.locator("#baseline-heading")).to_have_text("诊断基线")
            expect(page.locator("#confirm")).to_be_visible()
            expect(page.locator("#confirm")).to_be_disabled()
            expect(page.locator("#scenario")).to_be_disabled()
            expect(page.locator("#trust-warning")).to_contain_text("不执行源工程代码")
            page.locator("#trusted").check()
            page.locator("#confirm").click()
            expect(page.locator("#conversation-status")).to_have_text("已完成", timeout=15000)
            expect(page.locator("#evaluation")).to_contain_text("支持")
            expect(page.locator("#evaluation")).to_contain_text("反驳")
            expect(page.locator("#evaluation")).to_contain_text("未知")
            expect(page.locator("#evaluation")).to_contain_text("evidence_sufficient")
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
