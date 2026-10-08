import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from flight_agent.models import Parameters
from tests.helpers import SampleRunner


def session_module():
    try:
        return importlib.import_module("flight_agent.session")
    except ModuleNotFoundError:
        pytest.fail("Persistent analysis session has not been implemented")


def make_session(project, parameter_data, result_data):
    return session_module().AnalysisSession(
        project, Parameters.model_validate(parameter_data), SampleRunner(result_data)
    )


def test_run_saves_input_and_deterministic_assessment(project, parameter_data, result_data):
    session = make_session(project, parameter_data, result_data)
    session.analyze()
    checks = session.check_requirements()
    assert checks["status"] == "pass"
    assert checks["evaluated"] == 1
    assert json.loads((session.run_dir / "input.json").read_text())["mass_kg"] == 10.0
    assert json.loads((session.run_dir / "result.json").read_text())["valid"] is True
    assert json.loads((session.run_dir / "assessment.json").read_text())["status"] == "pass"
    assert len(json.loads((session.run_dir / "metadata.json").read_text())["input_sha256"]) == 64


def test_concurrent_tool_calls_reuse_one_result(project, parameter_data, result_data):
    session = make_session(project, parameter_data, result_data)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: session.analyze(), range(3)))
    assert all(result == results[0] for result in results)
    assert (session.run_dir / "result.json").is_file()


def test_cannot_assess_before_analysis(project, parameter_data, result_data):
    session = make_session(project, parameter_data, result_data)
    with pytest.raises(ValueError, match="analysis"):
        session.check_requirements()
    assert not (session.run_dir / "assessment.json").exists()


def test_failed_analysis_is_persisted_and_not_retried(project, parameter_data, result_data):
    module = session_module()
    from flight_agent.matlab import MatlabError

    class BrokenRunner(SampleRunner):
        def run(self, parameters):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("Failed analysis must not be automatically rerun")
            raise MatlabError("license unavailable")

    session = module.AnalysisSession(
        project, Parameters.model_validate(parameter_data), BrokenRunner(result_data)
    )
    for _ in range(2):
        with pytest.raises(MatlabError, match="license unavailable"):
            session.analyze()
    metadata = json.loads((session.run_dir / "metadata.json").read_text())
    assert metadata["status"] == "failed"
    assert "license unavailable" in (session.run_dir / "report.md").read_text()
    assert not (session.run_dir / "assessment.json").exists()


def test_each_run_has_distinct_artifacts(project, parameter_data, result_data):
    first = make_session(project, parameter_data, result_data)
    second = make_session(project, parameter_data, result_data)
    assert first.run_dir != second.run_dir
    assert first.run_dir.is_dir() and second.run_dir.is_dir()


def test_rules_are_snapshotted_before_external_execution(project, parameter_data, result_data):
    session = make_session(project, parameter_data, result_data)
    original = (session.run_dir / "rules.json").read_text()
    (project / "rules" / "demo.json").write_text("{}")
    session.analyze()
    assert session.check_requirements()["status"] == "pass"
    assert (session.run_dir / "rules.json").read_text() == original


def test_mismatched_run_context_cannot_be_assessed(project, parameter_data, result_data):
    result_data["condition"] = "different-condition"
    session = make_session(project, parameter_data, result_data)
    with pytest.raises(ValueError, match="condition"):
        session.analyze()
    assert not (session.run_dir / "result.json").exists()
