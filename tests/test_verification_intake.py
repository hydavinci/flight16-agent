import json

import pytest
from pydantic import Field

from flight_agent.runtime.resources import ResourceReference
from flight_agent.verification.resources import ResourceAdapter
from tests.agent_helpers import draft_data, make_agent_web
from tests.helpers import ScriptedModel, tool_message
from tests.verification_helpers import verification_module


class RecordingModel(ScriptedModel):
    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append([message.content for message in messages])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
def intake_runtime(tmp_path):
    _, runtime, calls, _ = make_agent_web(tmp_path, [])
    root = tmp_path / "source"
    root.mkdir()
    runtime.resources.register(ResourceAdapter(
        name="input_data", kind="signal_data", location="test-host",
        inspect=lambda locator: ResourceReference(
            id="input", kind="signal_data", location="test-host",
            locator=locator, fingerprint="a" * 64,
        ),
        verify=lambda _: None,
        browse=lambda _: pytest.fail("Intake must not browse resources"),
    ))
    model = RecordingModel(responses=[tool_message(
        "propose_agent", {"action": "plan", "text": "Confirm analysis.", "plan": draft_data()},
    )])
    runtime.planner.model = model
    return runtime, calls, model, root


def submit(runtime, *args, **kwargs):
    record = runtime.queue_message(*args, **kwargs)
    return runtime.continue_submission(record.id) if record.status == "queued" else record


@pytest.mark.parametrize(
    ("message", "project", "output"),
    [
        ("工程目录：/srv/f16，输出目录：/srv/results；验证阶跃。", "/srv/f16", "/srv/results"),
        ('Project: "/srv/private model"; output: "/srv/private results". Verify step.',
         "/srv/private model", "/srv/private results"),
        ("工程目录为“D:\\Private Model\\F16”；输出目录为‘/srv/private results’。",
         "D:\\Private Model\\F16", "/srv/private results"),
        (r"project_root=D:\F16; output_root=/srv/results", r"D:\F16", "/srv/results"),
        (r"工程目录：\\worker\projects\F16", r"\\worker\projects\F16", None),
        ('"/srv/private model"', "/srv/private model", None),
    ],
)
def test_paths_are_parsed_locally_and_fully_redacted(message, project, output):
    result = verification_module("intake").parse_directories(message)
    assert result.question is None
    assert result.project_root == project
    assert result.output_root == output
    assert project not in result.redacted_message
    assert "private" not in result.redacted_message.lower()
    if output:
        assert output not in result.redacted_message


def test_intake_reuses_context_and_distinguishes_an_output_only_change():
    intake = verification_module("intake")
    unchanged = intake.parse_directories(
        "把超调阈值改为 4%。", current_project="/srv/model", current_output="/srv/results"
    )
    assert unchanged.project_root == "/srv/model"
    assert unchanged.output_root == "/srv/results"
    changed = intake.parse_directories(
        '输出目录："/srv/new results"', current_project="/srv/model", current_output="/srv/results"
    )
    assert changed.project_root == "/srv/model"
    assert changed.output_root == "/srv/new results"


@pytest.mark.parametrize("message", ["这个项目是否满足判据？", "The project is not ready."])
def test_ordinary_project_questions_are_not_misread_as_directory_assignments(message):
    result = verification_module("intake").parse_directories(
        message, current_project="/srv/model"
    )
    assert result.project_root == "/srv/model"
    assert result.question is None


@pytest.mark.parametrize(
    ("message", "project_field", "output_field"),
    [
        ("检查 /srv/first 和 /srv/second", None, None),
        ("工程目录：/srv/first；工程目录：/srv/second", None, None),
        ("工程目录：/srv/first", "/srv/second", None),
        ("输出目录：/srv/first", "/srv/model", "/srv/second"),
        ("输出目录：relative/results", "/srv/model", None),
    ],
)
def test_ambiguous_directories_clarify_before_inspection_or_model(
    intake_runtime, monkeypatch, message, project_field, output_field
):
    runtime, calls, model, _ = intake_runtime

    def unexpected_inspection(*args):
        pytest.fail("Ambiguous paths must be clarified before project inspection")

    monkeypatch.setattr(runtime.resources, "inspect", unexpected_inspection)
    record = submit(runtime, None, message, project_field, output_root=output_field)
    assert record.task is None
    assert record.pending_decision.kind == "clarify"
    assert record.pending_decision.message
    assert not model.seen
    assert not calls


def test_missing_directory_accepts_a_bare_path_in_the_next_turn(intake_runtime):
    runtime, calls, model, root = intake_runtime
    model.responses.insert(0, tool_message(
        "propose_agent", {"action": "clarify", "text": "Provide the project directory."},
    ))
    first = submit(runtime, None, "Verify pitch step response.")
    assert first.pending_decision.kind == "clarify"
    assert len(model.seen) == 1
    planned = submit(runtime, first.id, str(root))
    assert planned.project_root == str(root)
    assert planned.pending_decision.kind == "confirm_baseline"
    assert len(model.seen) == 2
    assert "Verify pitch step response." in json.dumps(model.seen)
    assert str(root) not in json.dumps(model.seen)
    assert not calls


def test_chat_only_paths_are_not_sent_to_model_even_with_spaces(tmp_path, intake_runtime):
    runtime, calls, model, root = intake_runtime
    root = root.rename(tmp_path / "private model")
    draft = draft_data()
    draft["allowed_effects"].append("write_delivery")
    model.responses = [tool_message(
        "propose_agent", {"action": "plan", "text": "Confirm evidence storage.", "plan": draft},
    )]
    output = tmp_path / "private results"
    planned = submit(runtime, None, f'工程目录：“{root}”；输出目录：“{output}”。验证阶跃。')
    assert planned.pending_decision.kind == "confirm_baseline"
    assert planned.output_root == str(output)
    assert str(output) not in json.dumps(model.seen)
    assert "private results" not in json.dumps(model.seen)
    assert str(root) not in json.dumps(model.seen)
    assert not calls


@pytest.mark.parametrize(
    "diagnostic",
    [
        'Worker failed: "cannot open /private/unshared-project/diagnostic.mat".',
        r"Worker failed: 'cannot open D:\private\unshared-project\diagnostic.mat'.",
        r"Worker failed: `cannot open \\worker\unshared-project\diagnostic.mat`.",
        "Worker failed: “cannot open /private/unshared-project/diagnostic.mat”.",
        """Worker failed: 'cannot open "/private/unshared-project with spaces/diagnostic.mat"'.""",
    ],
)
def test_quoted_diagnostics_redact_paths_in_requests_and_history(intake_runtime, diagnostic):
    runtime, calls, model, root = intake_runtime
    model.responses.append(tool_message(
        "propose_agent", {"action": "clarify", "text": "Repair output?"},
    ))
    planned = submit(runtime, None, "Verify step. " + diagnostic, str(root))
    assert planned.project_root == str(root)
    assert planned.pending_decision.kind == "confirm_baseline"
    submit(runtime, planned.id, "Explain the earlier diagnostic.")
    assert len(model.seen) == 2
    for messages in model.seen:
        sent = json.dumps(messages)
        assert "cannot open" in sent
        assert "unshared-project" not in sent
        assert "diagnostic.mat" not in sent
        assert str(root) not in sent
    assert not calls
