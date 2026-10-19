import copy
import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar

from pydantic import JsonValue

from ..models import Contract, Nonempty
from ..runtime.capabilities import Capability, CapabilityRegistry
from ..runtime.contracts import (
    AnalysisSpec,
    DesignContext,
    DesignReference,
    Effect,
    ExecutionScope,
    fingerprint,
)
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
SIMULINK_ANALYSIS_VERSION = "1"


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
    input_model: type[Contract]
    output_model: type[Contract]
    requires: frozenset[str]
    provides: frozenset[str]
    permission: str
    operation: Callable[[_Invocation], Contract]


def _design(task: VerificationTask) -> DesignContext:
    return DesignContext(
        controller=DesignReference(
            id="project-controller",
            version=task.project.fingerprint,
            sha256=task.project.fingerprint,
        ),
        plant=DesignReference(
            id="project-plant",
            version=task.project.fingerprint,
            sha256=task.project.fingerprint,
        ),
    )


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
        self._capabilities: CapabilityRegistry[_Invocation] = CapabilityRegistry()
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
        output: type[Contract],
        permission: str,
        requires: set[str],
        provides: set[str],
    ) -> None:
        self._install(
            _Entry(
                name,
                description,
                _NoInput,
                output,
                frozenset(requires),
                frozenset(provides),
                permission,
                lambda call: output.model_validate(
                    operation(call.conversation, call.task, call.resume)
                ),
            ),
            version=SIMULINK_ANALYSIS_VERSION if name == "analyze_project" else "1",
        )

    def _install(self, entry: _Entry, *, version: str = "1") -> None:
        effect: Effect = (
            "execute_project" if entry.permission == "execute_project"
            else "write_delivery" if entry.permission == "write_delivery"
            else "read_only"
        )
        self._capabilities.register(
            Capability(
                name=entry.name,
                version=version,
                description=entry.description,
                input_model=entry.input_model,
                output_model=entry.output_model,
                effect=effect,
                operation=lambda invocation, inputs: entry.operation(invocation),
            )
        )
        self._entries[entry.name] = entry

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

        self._install(
            _Entry(
                tool.name,
                tool.description,
                tool.input_model,
                tool.output_model,
                frozenset(tool.requires),
                frozenset(tool.provides),
                tool.permission,
                invoke,
            )
        )

    def _entry(self, name: str) -> _Entry:
        if name not in self._entries:
            raise ValueError(f"Tool is not registered or available: {name}")
        return self._entries[name]

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                "name": entry.name,
                "version": self._capabilities.definition(entry.name).version,
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
        self._entry(node.tool)
        invocation = _Invocation(conversation, task, node, resume)
        return self._capabilities.invoke(
            node.tool,
            node.inputs,
            invocation,
            ExecutionScope(
                baseline_version=task.baseline.version,
                confirmed=task.baseline.confirmed,
                design=_design(task),
                allowed_effects=("read_only", "execute_project", "write_delivery"),
            ),
        )

    def analysis_spec(self, task: VerificationTask, kind: AnalysisKind) -> AnalysisSpec:
        return AnalysisSpec(
            capability="analyze_project",
            capability_version=self._capabilities.definition("analyze_project").version,
            design=_design(task),
            inputs={
                "analysis": kind,
                "trim_point": task.baseline.trim_point,
                "project_fingerprint": task.project.fingerprint,
            },
            data_fingerprints={item.path: item.sha256 for item in task.project.files},
            conditions=task.project.config.model_dump(mode="json"),
            numerics={"steady_state": task.baseline.steady_state.model_dump(mode="json")},
        )

    def evaluation_spec(self, task: VerificationTask) -> AnalysisSpec:
        if task.result is None:
            raise ValueError("Evaluation requires recorded numerical evidence")
        return AnalysisSpec(
            capability="evaluate_results",
            capability_version=self._capabilities.definition("evaluate_results").version,
            design=_design(task),
            inputs={
                "analyses": list(task.baseline.analyses),
                "criteria": [item.model_dump(mode="json") for item in task.baseline.criteria],
                "trim_point": task.baseline.trim_point,
                "steady_state": task.baseline.steady_state.model_dump(mode="json"),
            },
            data_fingerprints={
                "numerical_result": fingerprint(task.result.model_dump(mode="json"))
            },
            conditions={"project_fingerprint": task.baseline.project_fingerprint},
        )
