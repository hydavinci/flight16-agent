from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, JsonValue

from ..models import Contract, Nonempty
from .contracts import Effect, ExecutionScope, Name, Version, finite_mapping

Context = TypeVar("Context")
Input = TypeVar("Input", bound=Contract)
Output = TypeVar("Output", bound=Contract)


@dataclass(frozen=True)
class Capability(Generic[Context, Input, Output]):
    name: str
    version: str
    description: str
    input_model: type[Input]
    output_model: type[Output]
    effect: Effect
    operation: Callable[[Context, Input], Output]


class CapabilityDefinition(Contract):
    name: Name
    version: Version
    description: Nonempty
    effect: Effect


@dataclass(frozen=True)
class _Entry(Generic[Context]):
    definition: CapabilityDefinition
    input_model: type[Contract]
    output_model: type[Contract]
    operation: Callable[[Context, dict[str, JsonValue]], object]


def validate_output(model: type[BaseModel], value: object) -> dict[str, JsonValue]:
    if isinstance(value, BaseModel):
        value = value.model_dump(warnings=False)
    output = model.model_validate(value)
    return finite_mapping(output.model_dump(mode="json"))


class CapabilityRegistry(Generic[Context]):
    def __init__(self) -> None:
        self._entries: dict[str, _Entry[Context]] = {}

    def register(self, capability: Capability[Context, Input, Output]) -> None:
        definition = CapabilityDefinition(
            name=capability.name,
            version=capability.version,
            description=capability.description,
            effect=capability.effect,
        )
        if capability.name in self._entries:
            raise ValueError(f"Capability is already registered: {capability.name}")
        for model in (capability.input_model, capability.output_model):
            if not issubclass(model, Contract) or model.model_config.get("extra") != "forbid":
                raise ValueError("Capability schemas must be strict Contract subclasses")

        def invoke(context: Context, inputs: dict[str, JsonValue]) -> object:
            validated = capability.input_model.model_validate(inputs)
            return capability.operation(context, validated)

        self._entries[capability.name] = _Entry(
            definition, capability.input_model, capability.output_model, invoke
        )

    def _entry(self, name: str) -> _Entry[Context]:
        if name not in self._entries:
            raise ValueError(f"Capability is not registered: {name}")
        return self._entries[name]

    def definition(self, name: str) -> CapabilityDefinition:
        return self._entry(name).definition

    def validate_inputs(self, name: str, inputs: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return validate_output(self._entry(name).input_model, inputs)

    def validate_result(self, name: str, output: object) -> dict[str, JsonValue]:
        return validate_output(self._entry(name).output_model, output)

    def catalog(self) -> list[dict[str, object]]:
        return [
            {
                **entry.definition.model_dump(),
                "input_schema": entry.input_model.model_json_schema(),
                "output_schema": entry.output_model.model_json_schema(),
            }
            for entry in self._entries.values()
        ]

    def invoke(
        self,
        name: str,
        inputs: dict[str, JsonValue],
        context: Context,
        scope: ExecutionScope,
    ) -> dict[str, JsonValue]:
        entry = self._entry(name)
        scope = ExecutionScope.model_validate(scope.model_dump())
        scope.authorize(entry.definition.effect)
        inputs = self.validate_inputs(name, inputs)
        return self.validate_result(name, entry.operation(context, inputs))
