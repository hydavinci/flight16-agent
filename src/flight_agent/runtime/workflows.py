from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from ..models import Contract
from .capabilities import CapabilityDefinition, validate_output
from .intents import WorkflowRequest

State = TypeVar("State")


class DirectoryInputs(Contract):
    pass


@dataclass(frozen=True)
class Workflow(Generic[State]):
    definition: CapabilityDefinition
    directory: Literal["none", "worker_project", "diagnostic_data"]
    input_model: type[Contract]
    propose: Callable[[State, WorkflowRequest], State]


class WorkflowRegistry(Generic[State]):
    def __init__(self) -> None:
        self._entries: dict[str, Workflow[State]] = {}

    def register(self, workflow: Workflow[State]) -> None:
        name = workflow.definition.name
        if name in self._entries:
            raise ValueError(f"Workflow is already registered: {name}")
        self._entries[name] = workflow

    def get(self, name: str) -> Workflow[State]:
        if name not in self._entries:
            raise ValueError(f"Workflow is not registered: {name}")
        return self._entries[name]

    def validate(self, request: WorkflowRequest) -> WorkflowRequest:
        workflow = self.get(request.workflow)
        return request.model_copy(update={
            "inputs": validate_output(workflow.input_model, request.inputs)
        })

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                **workflow.definition.model_dump(),
                "directory": workflow.directory,
                "input_schema": workflow.input_model.model_json_schema(),
            }
            for workflow in self._entries.values()
        ]
