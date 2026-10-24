from math import floor
from typing import Literal

from pydantic import field_validator

from ...remote import RemoteError
from ...runtime.budget import BudgetExhausted
from ...runtime.capabilities import Capability
from ...runtime.capability_metadata import (
    Availability,
    CapabilityMetadata,
    MetricDefinition,
    ResourceRequirement,
)
from ...runtime.observations import CapabilityOutcome, Finding, Observation, OutputArtifact
from ...runtime.resources import AgentContract, ResourceReference
from ..agent_runtime import AgentContext, ExecutionUncertain, ReconciledFailure
from ..artifacts import artifact_path, describe_artifact, verify_artifact
from ..backend import AnalysisBackend
from ..contracts import AnalysisKind, AnalysisResult, AnalysisRunRequest, RunRecord
from ..intake import safe_directory, validate_output_root
from ..store import request_digest


class SimulinkInput(AgentContract):
    trim_point: Literal[1]

    @field_validator("trim_point", mode="before")
    @classmethod
    def strict_trim(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Trim point must be an integer")
        return value


def _request(
    context: AgentContext, inputs: SimulinkInput, kind: AnalysisKind, *, resume: bool
) -> AnalysisRunRequest:
    context.scope.authorize("execute_project")
    resource = context.resource("project")
    if resource not in context.scope.resources or (resource.kind, resource.location) != (
        "simulink_project",
        "worker",
    ):
        raise ValueError("Simulink requires its confirmed bound Worker project resource")
    saved = context.load_checkpoint()
    if resume:
        if saved is None or "request" not in saved:
            raise ExecutionUncertain("Original Worker request checkpoint is missing")
        request = AnalysisRunRequest.model_validate(saved["request"])
    else:
        if saved is not None:
            raise ExecutionUncertain("Original Worker request must be reconciled, not dispatched")
        context.budget.check_limits()
        timeout = min(300, floor(context.budget.remaining_seconds()))
        if timeout < 1:
            raise BudgetExhausted("Less than one second remains for Worker execution")
        request = AnalysisRunRequest(
            id=context.call_id,
            project_root=resource.locator,
            project_fingerprint=resource.fingerprint,
            trim_point=inputs.trim_point,
            analyses=[kind],
            trusted=True,
            timeout_seconds=timeout,
        )
        context.save_checkpoint({"request": request.model_dump(mode="json")})
    if (
        request.id != context.call_id
        or request.project_root != resource.locator
        or request.project_fingerprint != resource.fingerprint
        or request.trim_point != inputs.trim_point
        or request.analyses != [kind]
    ):
        raise ValueError("Worker checkpoint differs from the original confirmed invocation")
    return request


def _outcome(
    context: AgentContext, request: AnalysisRunRequest, result: AnalysisResult
) -> CapabilityOutcome:
    if (
        result.run_id != request.id
        or result.project_fingerprint != request.project_fingerprint
        or result.trim_point != request.trim_point
        or [item.kind for item in result.analyses] != request.analyses
    ):
        raise ValueError("Worker result differs from the original confirmed analysis request")
    prefix = f"calls/{context.call_id}/simulink"
    directory = context.directory / prefix
    artifacts = []
    for artifact in result.artifacts:
        verify_artifact(directory, artifact)
        artifacts.append(
            OutputArtifact(
                node_id=context.node_id,
                path=f"{prefix}/{artifact.path}",
                sha256=artifact.sha256,
                size=artifact.size,
            )
        )
    record = describe_artifact(directory, "analysis.json")
    if (
        AnalysisResult.model_validate_json(artifact_path(directory, record.path).read_bytes())
        != result
    ):
        raise ValueError("Worker result differs from its persisted analysis record")
    artifacts.append(
        OutputArtifact(
            id="analysis_record",
            node_id=context.node_id,
            path=f"{prefix}/{record.path}",
            sha256=record.sha256,
            size=record.size,
        )
    )
    evidence_paths = {item.path for item in result.artifacts}
    observations = []
    findings = []
    for analysis in result.analyses:
        if not set(analysis.artifact_paths) <= evidence_paths:
            raise ValueError("Worker observation references unregistered evidence")
        evidence = [f"{prefix}/{path}" for path in analysis.artifact_paths]
        reason = "; ".join(analysis.diagnostics) or "Worker analysis is numerically invalid"
        for name, metric in analysis.metrics.items():
            valid = analysis.valid and metric.valid
            observations.append(
                Observation(
                    **{
                        **metric.model_dump(),
                        "valid": valid,
                        "reason": metric.reason if metric.reason else "" if valid else reason,
                    },
                    node_id=context.node_id,
                    metric_id=name,
                    label=name.replace("_", " "),
                    conditions={"trim_point": result.trim_point},
                    evidence=evidence,
                )
            )
        if analysis.diagnostics:
            findings.append(
                Finding(
                    id=f"{analysis.kind}_diagnostics",
                    node_id=context.node_id,
                    text="; ".join(analysis.diagnostics),
                    status="observed" if analysis.valid else "invalid",
                    evidence=evidence,
                )
            )
    return CapabilityOutcome(
        observations=observations,
        findings=findings,
        artifacts=artifacts,
        details={"analysis_result": result.model_dump(mode="json")},
    )


def simulink_capabilities(
    backend: AnalysisBackend | None,
) -> list[Capability[AgentContext, SimulinkInput, CapabilityOutcome]]:
    def capability(
        kind: AnalysisKind,
    ) -> Capability[AgentContext, SimulinkInput, CapabilityOutcome]:
        def readiness(resources: tuple[ResourceReference, ...]) -> Availability:
            return (
                Availability(state="available")
                if backend is not None
                else Availability(
                    state="unavailable", reason="A configured Simulink Worker is required"
                )
            )

        def execute(
            context: AgentContext, inputs: SimulinkInput, *, resume: bool
        ) -> CapabilityOutcome:
            if backend is None:
                raise ValueError("A configured Simulink Worker is required")
            request = _request(context, inputs, kind, resume=resume)
            directory = context.directory / f"calls/{context.call_id}/simulink"
            validate_output_root(str(directory), request.project_root)
            safe_directory(directory)

            def progress(record: RunRecord) -> None:
                if record.id != request.id or record.request_sha256 != request_digest(request):
                    raise ValueError("Worker status differs from its original request identity")
                context.save_checkpoint(
                    {
                        "request": request.model_dump(mode="json"),
                        "record": record.model_dump(mode="json"),
                    }
                )

            try:
                result = backend.execute(request, directory, progress, resume=resume)
                return _outcome(context, request, result)
            except (OSError, ValueError, RuntimeError, RemoteError) as exc:
                saved = context.load_checkpoint()
                if saved is not None and saved.get("record") is not None:
                    record = RunRecord.model_validate(saved["record"])
                    if (
                        record.id == request.id
                        and record.request_sha256 == request_digest(request)
                        and record.status in ("failed", "interrupted")
                    ):
                        if resume:
                            raise ReconciledFailure(record.error or record.status) from exc
                        raise ValueError(record.error or record.status) from exc
                raise ExecutionUncertain(
                    f"Worker run {request.id} needs original-ID reconciliation: {exc}"
                ) from exc

        metrics = (
            [
                MetricDefinition(id="overshoot", label="Overshoot", unit="%"),
                MetricDefinition(id="settling_time", label="Settling time", unit="s"),
            ]
            if kind == "step_response"
            else [
                MetricDefinition(
                    id="gain_margin",
                    label="Gain margin",
                    unit="dB",
                    comparable_specials=["positive_infinity"],
                ),
                MetricDefinition(
                    id="phase_margin",
                    label="Phase margin",
                    unit="deg",
                    comparable_specials=["positive_infinity"],
                ),
            ]
        )
        return Capability(
            name=f"simulink_{kind}",
            version="1",
            description=f"Run registered Simulink {kind} analysis on the confirmed project.",
            input_model=SimulinkInput,
            output_model=CapabilityOutcome,
            effect="execute_project",
            operation=lambda context, inputs: execute(context, inputs, resume=False),
            reconcile=lambda context, inputs: execute(context, inputs, resume=True),
            readiness=readiness,
            metadata=CapabilityMetadata(
                metrics=metrics,
                resources=[
                    ResourceRequirement(
                        role="project",
                        kind="simulink_project",
                        location="worker",
                    )
                ],
                conditions_schema={
                    "type": "object",
                    "properties": {"trim_point": {"const": 1}},
                    "required": ["trim_point"],
                    "additionalProperties": False,
                },
                validity_policy={"version": "1", "source": "Worker metric and analysis validity"},
                planning_notes=(
                    "Only trim point 1 is supported. Bind the Worker project role. "
                    "Metrics are analysis evidence, not acceptance standards. "
                    "Criteria must use conditions {trim_point: 1}; no thresholds are supplied."
                ),
                cacheable=True,
            ),
        )

    return [capability(kind) for kind in ("step_response", "stability_margins")]
