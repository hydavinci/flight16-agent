import importlib
from pathlib import Path

import pytest
from pydantic import Field, ValidationError

from flight_agent.models import Contract, FiniteNumber


def kernel(name):
    try:
        return importlib.import_module(f"flight_agent.runtime.{name}")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Shared runtime module is missing: {exc.name}")


def design_data():
    return {
        "controller": {"id": "controller", "version": "1", "sha256": "a" * 64},
        "plant": {"id": "plant", "version": "1", "sha256": "b" * 64},
    }


def spec_data():
    return {
        "capability": "compare_signals",
        "capability_version": "1",
        "design": design_data(),
        "inputs": {"observed": "test.pitch", "reference": "simulation.pitch"},
        "data_fingerprints": {"test": "c" * 64, "simulation": "d" * 64},
        "conditions": {"trim_point": 1},
        "numerics": {"alignment": "linear-overlap"},
    }


class NumberInput(Contract):
    value: FiniteNumber = Field(ge=0)


class NumberOutput(Contract):
    value: FiniteNumber


def capability(operation=None, **changes):
    registry = kernel("capabilities")
    settings = {
        "name": "measure",
        "version": "1",
        "description": "Compute a bounded read-only measurement.",
        "input_model": NumberInput,
        "output_model": NumberOutput,
        "effect": "read_only",
        "operation": operation or (lambda context, inputs: NumberOutput(value=inputs.value)),
    }
    settings.update(changes)
    return registry.Capability(**settings)


def scope(**changes):
    settings = {
        "baseline_version": 1,
        "confirmed": True,
        "design": design_data(),
    }
    settings.update(changes)
    return kernel("contracts").ExecutionScope.model_validate(settings)


def test_analysis_fingerprint_is_canonical_and_excludes_evaluation_and_delivery():
    contracts = kernel("contracts")
    original = contracts.AnalysisSpec.model_validate(spec_data())
    reordered = spec_data()
    reordered["data_fingerprints"] = dict(reversed(list(reordered["data_fingerprints"].items())))
    assert original.fingerprint == contracts.AnalysisSpec.model_validate(reordered).fingerprint
    assert len(original.fingerprint) == 64
    for unrelated in ("criteria", "report_grouping", "confirmed"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            contracts.AnalysisSpec.model_validate({**spec_data(), unrelated: "changed"})


@pytest.mark.parametrize(
    "field",
    ["inputs", "conditions", "numerics", "data", "controller", "plant", "candidate", "version"],
)
def test_every_numerical_input_and_design_version_changes_the_cache_key(field):
    contracts = kernel("contracts")
    original = contracts.AnalysisSpec.model_validate(spec_data())
    changed = spec_data()
    if field in ("inputs", "conditions", "numerics"):
        changed[field]["new_input"] = 2
    elif field == "data":
        changed["data_fingerprints"]["test"] = "e" * 64
    elif field in ("controller", "plant"):
        changed["design"][field]["sha256"] = "f" * 64
    elif field == "candidate":
        changed["design"]["candidate"] = {
            "id": "candidate-1", "version": "1", "sha256": "e" * 64,
        }
    else:
        changed["capability_version"] = "2"
    assert contracts.AnalysisSpec.model_validate(changed).fingerprint != original.fingerprint


def test_analysis_contracts_reject_nonfinite_inputs_and_mutated_receipts():
    contracts = kernel("contracts")
    with pytest.raises(ValidationError, match="finite"):
        contracts.AnalysisSpec.model_validate({**spec_data(), "inputs": {"value": float("nan")}})
    payload = {
        "id": "1" * 32,
        "spec": spec_data(),
        "output": {"value": 1.0},
        "output_sha256": contracts.fingerprint({"value": 1.0}),
        "started_at": "2026-10-19T00:00:00Z",
        "finished_at": "2026-10-19T00:00:01Z",
    }
    run = contracts.AnalysisRun.model_validate(payload)
    assert run.spec.capability == "compare_signals"
    with pytest.raises(ValidationError, match="digest|fingerprint"):
        contracts.AnalysisRun.model_validate({**payload, "output": {"value": 2.0}})


def test_registry_validates_registration_input_output_and_confirmation():
    registry = kernel("capabilities").CapabilityRegistry()
    registry.register(capability())
    result = registry.invoke("measure", {"value": 2.0}, None, scope())
    assert result == {"value": 2.0}
    assert registry.definition("measure").version == "1"
    assert registry.catalog()[0]["input_schema"]["additionalProperties"] is False
    with pytest.raises(ValueError, match="registered"):
        registry.register(capability())
    with pytest.raises(ValueError, match="registered"):
        registry.invoke("missing", {}, None, scope())
    with pytest.raises(ValueError, match="confirm"):
        registry.invoke("measure", {"value": 1.0}, None, scope(confirmed=False))
    with pytest.raises(ValidationError):
        registry.invoke("measure", {"value": -1.0}, None, scope())
    with pytest.raises(ValidationError):
        registry.invoke("measure", {"value": 1.0, "code": "run"}, None, scope())


@pytest.mark.parametrize("invalid", ["missing", "nonfinite", "constructed"])
def test_untrusted_capability_outputs_cannot_skip_schema_validation(invalid):
    def operation(context, inputs):
        if invalid == "missing":
            return {}
        if invalid == "nonfinite":
            return {"value": float("inf")}
        return NumberOutput.model_construct(value="not a number")

    registry = kernel("capabilities").CapabilityRegistry()
    registry.register(capability(operation))
    with pytest.raises(ValidationError):
        registry.invoke("measure", {"value": 1.0}, None, scope())


def test_future_candidate_effect_requires_scope_without_changing_the_kernel(tmp_path):
    contracts = kernel("contracts")
    source = tmp_path / "source"
    candidate = tmp_path / "candidate"
    source.mkdir()
    candidate.mkdir()
    (source / "controller.txt").write_text("original")
    calls = []

    def modify(context: Path, inputs: NumberInput):
        calls.append(inputs.value)
        (context / "controller.txt").write_text(str(inputs.value))
        return NumberOutput(value=inputs.value)

    registry = kernel("capabilities").CapabilityRegistry()
    registry.register(capability(modify, name="tune_candidate", effect="write_candidate"))
    with pytest.raises(ValueError, match="effect|permission"):
        registry.invoke("tune_candidate", {"value": 2.0}, candidate, scope())
    design = design_data()
    design["candidate"] = {"id": "candidate-1", "version": "1", "sha256": "e" * 64}
    grant = {
        "design": design,
        "allowed_effects": ["read_only", "write_candidate", "adopt_candidate"],
        "candidate": {
            "candidate_id": "candidate-1",
            "source_roots": [str(source)],
            "root": str(candidate),
        },
    }
    registry.invoke("tune_candidate", {"value": 2.0}, candidate, scope(**grant))
    assert calls == [2.0]
    assert (candidate / "controller.txt").read_text() == "2.0"
    assert (source / "controller.txt").read_text() == "original"
    with pytest.raises(ValidationError, match="isolat|source"):
        scope(**{**grant, "candidate": {**grant["candidate"], "root": str(source)}})
    with pytest.raises(ValidationError, match="candidate"):
        scope(**{**grant, "candidate": {**grant["candidate"], "candidate_id": "another"}})

    registry.register(capability(name="adopt_candidate", effect="adopt_candidate"))
    with pytest.raises(ValueError, match="adoption|adopt"):
        registry.invoke("adopt_candidate", {"value": 2.0}, candidate, scope(**grant))
    approval = contracts.AdoptionApproval(candidate_id="candidate-1", baseline_version=1)
    registry.invoke(
        "adopt_candidate", {"value": 2.0}, candidate, scope(**grant, adoption=approval)
    )
    with pytest.raises(ValidationError, match="version|baseline"):
        scope(**grant, adoption={"candidate_id": "candidate-1", "baseline_version": 2})


def test_candidate_workspace_rejects_symlink_aliases(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValidationError, match="symlink|isolat|source"):
        kernel("contracts").CandidateScope(
            candidate_id="candidate-1", root=str(alias), source_roots=[str(source)]
        )
