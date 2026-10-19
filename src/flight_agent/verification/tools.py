import copy
import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, JsonValue

from ..models import Contract, Nonempty
from ..upload_models import finite_json
from .artifacts import verify_artifact
from .contracts import (
    AnalysisKind,
    AnalysisResult,
    Conversation,
    Evaluation,
    EvaluationBaseline,
    Identifier,
    PlanNode,
    VerificationTask,
    plan_ancestors,
)

ToolInput = TypeVar("ToolInput", bound=Contract)
ToolOutput = TypeVar("ToolOutput", bound=Contract)
BuiltinOperation = Callable[[Conversation, VerificationTask, bool], dict[str, JsonValue]]
RESERVED = frozenset({"confirmed_baseline", "numerical_result", "evaluation", "delivery"})


@dataclass(frozen=True)
class ToolContext:
    baseline: EvaluationBaseline
    result: AnalysisResult | None
    evaluation: Evaluation | None
    outputs: dict[str, dict[str, JsonValue]]
    read_artifact: Callable[[str], bytes]


@dataclass(frozen=True)
class ReadOnlyTool(Generic[ToolInput, ToolOutput]):
    name: str
    description: str
    input_model: type[ToolInput]
    output_model: type[ToolOutput]
    operation: Callable[[ToolContext, ToolInput], ToolOutput]
    requires: frozenset[str]
    provides: frozenset[str]
    permission: Literal["read_only"] = "read_only"


class _NoInput(Contract):
    pass


class _AnalysisOutput(Contract):
    source: Nonempty
    run_id: Identifier
    analyses: list[AnalysisKind] = []


class _DeliveryOutput(Contract):
    source: Nonempty
    artifacts: list[str]


@dataclass(frozen=True)
class _Invocation:
    conversation: Conversation
    task: VerificationTask
    node: PlanNode
    resume: bool


@dataclass(frozen=True)
class _Entry:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    requires: frozenset[str]
    provides: frozenset[str]
    permission: str
    operation: Callable[[_Invocation], object]


def _output(model: type[BaseModel], value: object) -> dict[str, JsonValue]:
    if isinstance(value, BaseModel):
        value = value.model_dump(warnings=False)
    output = model.model_validate(value)
    finite_json(output.model_dump())
    return output.model_dump(mode="json")


def _read_context(invocation: _Invocation) -> ToolContext:
    task = invocation.task
    artifacts = {item.path: item for item in task.artifacts}
    ancestors = plan_ancestors(task.proposal.nodes)[invocation.node.id]

    def read_artifact(name: str) -> bytes:
        if name not in artifacts:
            raise ValueError(f"Artifact is not registered: {name}")
        artifact = artifacts[name]
        data = verify_artifact(Path(task.result_directory), artifact).read_bytes()
        if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError(f"Artifact integrity mismatch: {name}")
        return data

    return ToolContext(
        baseline=task.baseline.model_copy(deep=True),
        result=task.result.model_copy(deep=True) if task.result else None,
        evaluation=task.evaluation.model_copy(deep=True) if task.evaluation else None,
        outputs={
            call.node_id: copy.deepcopy(call.output)
            for call in task.calls
            if call.node_id in ancestors
            and call.baseline_version == task.baseline.version
            and call.status == "completed"
        },
        read_artifact=read_artifact,
    )


class ToolRegistry:
    def __init__(
        self, analyze: BuiltinOperation, evaluate: BuiltinOperation, deliver: BuiltinOperation
    ):
        self._entries: dict[str, _Entry] = {}
        self._builtin(
            "analyze_project",
            "Run or reuse registered numerical analyses.",
            analyze,
            _AnalysisOutput,
            "execute_project",
            {"confirmed_baseline"},
            {"numerical_result"},
        )
        self._builtin(
            "evaluate_results",
            "Evaluate confirmed criteria deterministically.",
            evaluate,
            Evaluation,
            "evaluate",
            {"numerical_result"},
            {"evaluation"},
        )
        self._builtin(
            "deliver_report",
            "Write verified reports and the execution record.",
            deliver,
            _DeliveryOutput,
            "write_delivery",
            {"numerical_result", "evaluation"},
            {"delivery"},
        )

    def _builtin(
        self,
        name: str,
        description: str,
        operation: BuiltinOperation,
        output: type[BaseModel],
        permission: str,
        requires: set[str],
        provides: set[str],
    ) -> None:
        self._entries[name] = _Entry(
            name,
            description,
            _NoInput,
            output,
            frozenset(requires),
            frozenset(provides),
            permission,
            lambda call: operation(call.conversation, call.task, call.resume),
        )

    def register(self, tool: ReadOnlyTool[ToolInput, ToolOutput]) -> None:
        if not isinstance(tool, ReadOnlyTool) or tool.permission != "read_only":
            raise ValueError("Extensions require the read_only permission")
        if tool.name in self._entries:
            raise ValueError(f"Tool is already registered: {tool.name}")
        for name in (tool.name, *tool.requires, *tool.provides):
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                raise ValueError(f"Invalid tool or resource identifier: {name}")
        if not tool.description.strip() or not tool.provides:
            raise ValueError("A tool requires a description and a provided capability")
        if tool.provides & RESERVED:
            raise ValueError("Extensions cannot provide reserved capabilities")
        if not issubclass(tool.input_model, Contract) or not issubclass(
            tool.output_model, Contract
        ):
            raise ValueError("Tool input and output schemas must extend Contract")

        def invoke(call: _Invocation) -> ToolOutput:
            inputs = tool.input_model.model_validate(call.node.inputs)
            return tool.operation(_read_context(call), inputs)

        self._entries[tool.name] = _Entry(
            tool.name,
            tool.description,
            tool.input_model,
            tool.output_model,
            frozenset(tool.requires),
            frozenset(tool.provides),
            tool.permission,
            invoke,
        )

    def _entry(self, name: str) -> _Entry:
        if name not in self._entries:
            raise ValueError(f"Tool is not registered or available: {name}")
        return self._entries[name]

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                "name": entry.name,
                "description": entry.description,
                "permission": entry.permission,
                "requires": sorted(entry.requires),
                "provides": sorted(entry.provides),
                "input_schema": entry.input_model.model_json_schema(),
                "output_schema": entry.output_model.model_json_schema(),
            }
            for entry in self._entries.values()
        ]

    def validate_plan(self, nodes: Sequence[PlanNode]) -> None:
        ancestors = plan_ancestors(nodes)
        entries = {node.id: self._entry(node.tool) for node in nodes}
        for node in nodes:
            entry = entries[node.id]
            finite_json(entry.input_model.model_validate(node.inputs).model_dump())
            available = {"confirmed_baseline"}
            for dependency in ancestors[node.id]:
                available.update(entries[dependency].provides)
            if not entry.requires.issubset(available):
                raise ValueError(f"Missing required resource for tool {node.tool}")
        providers: dict[str, str] = {}
        for resource in ("numerical_result", "evaluation", "delivery"):
            matches = [node_id for node_id, entry in entries.items() if resource in entry.provides]
            if len(matches) != 1:
                raise ValueError(f"A plan needs exactly one provider of {resource}")
            providers[resource] = matches[0]
        for node_id, entry in entries.items():
            if (
                entry.permission == "read_only"
                and node_id not in ancestors[providers["evaluation"]]
            ):
                raise ValueError("Every analysis extension must precede evaluation")
            if node_id != providers["delivery"] and node_id not in ancestors[providers["delivery"]]:
                raise ValueError("Delivery must depend on every analysis and evaluation node")

    def invoke(
        self, node: PlanNode, conversation: Conversation, task: VerificationTask, *, resume: bool
    ) -> dict[str, JsonValue]:
        entry = self._entry(node.tool)
        entry.input_model.model_validate(node.inputs)
        invocation = _Invocation(conversation, task, node, resume)
        return _output(entry.output_model, entry.operation(invocation))
