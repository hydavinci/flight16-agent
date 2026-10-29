import importlib
from types import SimpleNamespace

import pytest
from pydantic import Field

from flight_agent.verification.contracts import Conversation, DiagnosisTask, VerificationTask
from flight_agent.verification.diagnosis_contracts import DiagnosisProposal
from flight_agent.verification.structured_planner import StructuredPlanner, redact_text
from tests.diagnosis_helpers import diagnosis_proposal
from tests.helpers import ScriptedModel, tool_message


def test_redact_text_hides_local_paths_and_credentials():
    value = redact_text(
        "root=/private/project token=secret-value",
        ["/private/project"],
    )
    assert "/private/project" not in value
    assert "secret-value" not in value


def test_legacy_conversation_paths_redact_current_and_archived_task_projects():
    archived = VerificationTask.model_construct(
        project=SimpleNamespace(root="/private/archived-project"),
        output_root="/private/archived-output",
        result_directory="/private/archived-result",
    )
    current = DiagnosisTask.model_construct(
        project=SimpleNamespace(root="/private/current-project"),
        output_root="/private/current-output",
        result_directory="/private/current-result",
    )
    conversation = Conversation.model_construct(
        project_root="/private/different-conversation-project",
        output_root="/private/conversation-output",
        archived_tasks=[archived],
        task=current,
    )
    planner = importlib.import_module("flight_agent.verification.planner")

    value = redact_text(
        "current=/private/current-project archived=/private/archived-project",
        planner._conversation_paths(conversation),
    )

    assert "/private/current-project" not in value
    assert "/private/archived-project" not in value


def test_structured_planner_rejects_nonpositive_timeout():
    with pytest.raises(ValueError, match="finite and positive"):
        StructuredPlanner().with_timeout(0)


def test_bounded_planner_passes_remaining_timeout_without_mutating_original():
    class TimedModel(ScriptedModel):
        timeouts: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.timeouts.append(kwargs.get("timeout"))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = TimedModel(
        responses=[
            tool_message("proposal", diagnosis_proposal()),
            tool_message("proposal", diagnosis_proposal()),
        ]
    )
    planner = StructuredPlanner(model)
    bounded = planner.with_timeout(1.25)
    bounded.call("proposal", DiagnosisProposal, {}, system_prompt="test")
    planner.call("proposal", DiagnosisProposal, {}, system_prompt="test")
    assert model.timeouts == [1.25, None]
