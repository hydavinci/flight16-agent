import importlib
import json

import pytest
from langchain_core.messages import AIMessage

from flight_agent.models import Parameters
from flight_agent.session import AnalysisSession
from tests.helpers import SampleRunner, ScriptedModel, successful_responses, tool_message


def agent_module():
    try:
        return importlib.import_module("flight_agent.agent")
    except ModuleNotFoundError:
        pytest.fail("LangChain tool-calling agent has not been implemented")


@pytest.fixture
def session(project, parameter_data, result_data):
    return AnalysisSession(
        project, Parameters.model_validate(parameter_data), SampleRunner(result_data)
    )


def test_real_langchain_loop_saves_evidence_and_report(session):
    model = ScriptedModel(responses=successful_responses())
    report = agent_module().execute_agent(model, session, "分析输入并解释", max_steps=20)
    assert report.is_file()
    content = report.read_text()
    assert "[KB:demo.md]" in content
    assert "DEMO-CL" in content
    metadata = json.loads((session.run_dir / "metadata.json").read_text())
    assert metadata["status"] == "completed"
    transcript = json.loads((session.run_dir / "messages.json").read_text())
    results = [item["data"]["content"] for item in transcript if item["type"] == "tool"]
    assert len(results) == 3
    assert json.loads(results[0])["metrics"]["dynamic_pressure"]["value"] == 245.0
    assert json.loads(results[1])["status"] == "pass"
    assert json.loads(results[2])[0]["source_id"] == "demo.md"


@pytest.mark.parametrize("failure", ["no_analysis", "no_checks", "no_evidence", "bad_citation"])
def test_incomplete_or_unsupported_model_answer_is_rejected(session, failure):
    module = agent_module()
    script = successful_responses()
    if failure == "no_analysis":
        script = script[-1:]
    elif failure == "no_checks":
        script.pop(1)
    elif failure == "no_evidence":
        script.pop(2)
    else:
        script[-1] = AIMessage(content="Everything passes [KB:invented.md]")
    with pytest.raises(module.AgentError):
        module.execute_agent(ScriptedModel(responses=script), session, "分析", max_steps=20)
    assert json.loads((session.run_dir / "metadata.json").read_text())["status"] == "failed"
    assert "Analysis failed" in (session.run_dir / "report.md").read_text()


def test_repeated_agent_analysis_does_not_rerun_matlab(session):
    script = successful_responses()
    script.insert(1, tool_message("run_matlab_analysis", call_id="second-analysis"))
    report = agent_module().execute_agent(ScriptedModel(responses=script), session, "分析", 20)
    assert "DEMO-CL" in report.read_text()


def test_agent_can_recover_from_checking_before_analysis(session):
    script = successful_responses()
    script.insert(0, tool_message("check_requirements", call_id="early-check"))
    report = agent_module().execute_agent(ScriptedModel(responses=script), session, "分析", 20)
    assert json.loads((session.run_dir / "metadata.json").read_text())["status"] == "completed"
    assert report.is_file()


def test_agent_loop_is_bounded(session):
    module = agent_module()
    model = ScriptedModel(
        responses=[
            tool_message("search_knowledge", {"query": "升力系数"}, call_id=f"search-{index}")
            for index in range(5)
        ]
    )
    with pytest.raises(module.AgentError, match="limit"):
        module.execute_agent(model, session, "分析", max_steps=4)
    assert json.loads((session.run_dir / "metadata.json").read_text())["status"] == "failed"
    assert (session.run_dir / "messages.json").is_file()


def test_model_cannot_override_failing_rule(session):
    from flight_agent.models import RuleSet

    data = session.rules.model_dump()
    data["rules"][0]["threshold"] = 0.1
    session.rules = RuleSet.model_validate(data)
    report = agent_module().execute_agent(
        ScriptedModel(responses=successful_responses()), session, "分析", 20
    )
    assert json.loads((session.run_dir / "assessment.json").read_text())["status"] == "fail"
    assert "判定状态：`fail`" in report.read_text()


def test_tools_do_not_accept_arbitrary_paths_or_code(session):
    tools = agent_module().build_tools(session)
    assert {tool.name for tool in tools} == {
        "run_matlab_analysis",
        "check_requirements",
        "search_knowledge",
    }
    for tool in tools:
        properties = tool.get_input_schema().model_json_schema().get("properties", {})
        assert not {"path", "code", "command", "function", "parameters"} & properties.keys()


def test_model_failure_retains_completed_analysis(session):
    module = agent_module()

    class BrokenModel(ScriptedModel):
        def _generate(self, *args, **kwargs):
            if self.i == 2:
                raise ValueError("model service returned invalid output")
            return super()._generate(*args, **kwargs)

    with pytest.raises(module.AgentError):
        module.execute_agent(BrokenModel(responses=successful_responses()), session, "分析", 20)
    assert (session.run_dir / "result.json").is_file()
    assert (session.run_dir / "assessment.json").is_file()
    assert json.loads((session.run_dir / "metadata.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_truncated_or_filtered_explanation_is_not_complete(session, finish_reason):
    module = agent_module()
    script = successful_responses()
    script[-1] = AIMessage(
        content="参考 [KB:demo.md]。尚未完成的分析：",
        response_metadata={"finish_reason": finish_reason},
    )
    with pytest.raises(module.AgentError, match=finish_reason):
        module.execute_agent(ScriptedModel(responses=script), session, "分析", 20)
    assert json.loads((session.run_dir / "metadata.json").read_text())["status"] == "failed"
    assert json.loads((session.run_dir / "assessment.json").read_text())["status"] == "pass"
