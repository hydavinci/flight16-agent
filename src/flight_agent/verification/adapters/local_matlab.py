import os
from math import floor
from pathlib import Path
from typing import Literal

from ...runtime.budget import BudgetExhausted
from ...runtime.capabilities import Capability
from ...runtime.capability_metadata import (
    Availability,
    CapabilityMetadata,
    MetricDefinition,
    ResourceRequirement,
)
from ...runtime.contracts import fingerprint
from ...runtime.evidence import describe_evidence, publish_evidence, verify_evidence
from ...runtime.observations import CapabilityOutcome, Finding, Observation, OutputArtifact
from ...runtime.resources import ResourceReference
from ..agent_runtime import AgentContext, ExecutionUncertain, ReconciledFailure
from ..local_matlab import LocalMatlabBackend, _read_model, _verify_result
from ..matlab_contracts import (
    LocalMatlabRequest,
    LocalMatlabResult,
    MatlabProjectDescriptor,
    ProjectExecutionInput,
    StepinfoInput,
)
from ..matlab_projects import discover_matlab_project


def _project(context: AgentContext) -> MatlabProjectDescriptor:
    resource = context.resource("project")
    if resource not in context.scope.resources or (resource.kind, resource.location) != (
        "matlab_project",
        "agent",
    ):
        raise ValueError("MATLAB requires its exact confirmed bound local project resource")
    if resource.version != "1":
        raise ValueError("Local project resource version differs from the supported version")
    project = MatlabProjectDescriptor.model_validate(resource.metadata.get("project"))
    if (project.root, project.fingerprint) != (resource.locator, resource.fingerprint):
        raise ValueError("Local project descriptor differs from the confirmed resource")
    return project


def _series(context: AgentContext, inputs: StepinfoInput) -> Path:
    series = inputs.series
    for producer, output in context.outputs.items():
        if (
            series.node_id == producer
            and series.id == "series"
            and series in output.artifacts
            and output.details.get("series") == series.model_dump(mode="json")
            and series.path == f"calls/{output.details.get('call_id')}/published/series.json"
        ):
            if output.details.get("conditions") != inputs.conditions:
                raise ValueError("Metric conditions differ from the actual source conditions")
            return verify_evidence(context.directory, series)
    raise ValueError("stepinfo requires the exact published series of a direct dependency")


def _request(
    context: AgentContext, inputs: ProjectExecutionInput | StepinfoInput, *, resume: bool
) -> LocalMatlabRequest:
    context.scope.authorize("execute_project")
    project = _project(context) if isinstance(inputs, ProjectExecutionInput) else None
    saved = context.load_checkpoint()
    operation: Literal["execute_project", "stepinfo"] = (
        "execute_project" if project is not None else "stepinfo"
    )
    if resume:
        if saved is None or "request" not in saved:
            raise ExecutionUncertain("Original local MATLAB request checkpoint is missing")
        request = LocalMatlabRequest.model_validate(saved["request"])
    else:
        if saved is not None:
            raise ExecutionUncertain("Original local MATLAB request requires reconciliation")
        if os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") != "1":
            raise ValueError("Set MATLAB_ALLOW_TRUSTED_PROJECTS=1")
        if project is not None and discover_matlab_project(project.root) != project:
            raise ValueError("Local project fingerprint/selected entries changed")
        if isinstance(inputs, StepinfoInput):
            _series(context, inputs)
        context.budget.check_limits()
        timeout = min(300, floor(context.budget.remaining_seconds()))
        if timeout < 1:
            raise BudgetExhausted("Less than one second remains for local MATLAB execution")
        request = LocalMatlabRequest(
            id=context.call_id,
            operation=operation,
            inputs=inputs,
            project=project,
            input_evidence=[inputs.series] if isinstance(inputs, StepinfoInput) else [],
            timeout_seconds=timeout,
        )
        context.save_checkpoint({"request": request.model_dump(mode="json")})
    if (
        request.id != context.call_id
        or request.operation != operation
        or request.inputs != inputs
        or request.project != project
        or request.input_evidence != ([inputs.series] if isinstance(inputs, StepinfoInput) else [])
    ):
        raise ValueError("Local MATLAB checkpoint differs from the original confirmed invocation")
    return request


def _outcome(
    context: AgentContext, request: LocalMatlabRequest, result: LocalMatlabResult, directory: Path
) -> CapabilityOutcome:
    _verify_result(request, result, directory)
    if _read_model(directory / "result.json", LocalMatlabResult) != result:
        raise ValueError("Local result differs from its persisted original execution record")
    artifacts = []
    prefix = f"calls/{context.call_id}/published"
    records = [
        *result.artifacts,
        OutputArtifact(**describe_evidence(directory, "result.json").model_dump()),
    ]
    for source in records:
        path = verify_evidence(directory, source)
        publish_evidence(context.directory, f"{prefix}/{source.path}", path.read_bytes())
        verify_evidence(directory, source)
        published = OutputArtifact(
            id=Path(source.path).stem.replace("-", "_"),
            node_id=context.node_id,
            path=f"{prefix}/{source.path}",
            sha256=source.sha256,
            size=source.size,
        )
        verify_evidence(context.directory, published)
        artifacts.append(published)
    details = {
        **result.details,
        "valid": result.valid,
        "execution_status": result.execution_status,
        "call_id": context.call_id,
        "conditions": request.inputs.conditions,
    }
    for artifact in artifacts:
        if artifact.id == "series":
            details["series"] = artifact.model_dump(mode="json")
    observations = []
    if isinstance(request.inputs, StepinfoInput) and result.valid:
        metrics = result.details["metrics"]
        assert isinstance(metrics, dict)
        for name in ("overshoot", "settling_time"):
            metric = metrics[name]
            assert isinstance(metric, dict)
            observations.append(
                Observation.model_validate(
                    {
                        **metric,
                        "node_id": context.node_id,
                        "metric_id": name,
                        "label": name.replace("_", " "),
                        "conditions": request.inputs.conditions,
                        "evidence": [item.path for item in artifacts],
                    }
                )
            )
    findings = (
        [
            Finding(
                id="matlab_diagnostics",
                node_id=context.node_id,
                text="; ".join(result.diagnostics),
                status="observed" if result.valid else "invalid",
                evidence=[item.path for item in artifacts],
            )
        ]
        if result.diagnostics
        else []
    )
    return CapabilityOutcome(
        observations=observations, findings=findings, artifacts=artifacts, details=details
    )


def local_matlab_capabilities(
    backend: LocalMatlabBackend,
) -> list[Capability[AgentContext, ProjectExecutionInput | StepinfoInput, CapabilityOutcome]]:
    def readiness(resources: tuple[ResourceReference, ...]) -> Availability:
        return backend.availability()

    def execute(
        context: AgentContext, inputs: ProjectExecutionInput | StepinfoInput, *, resume: bool
    ) -> CapabilityOutcome:
        request = _request(context, inputs, resume=resume)
        directory = context.directory / f"calls/{context.call_id}/local-matlab"
        if not resume and isinstance(inputs, StepinfoInput):
            source = _series(context, inputs)
            # Backend evidence locators are relative to the enclosing call directory.
            publish_evidence(directory.parent, inputs.series.path, source.read_bytes())
            _series(context, inputs)
        try:
            result = backend.execute(request, directory, resume=resume)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ExecutionUncertain(
                f"Local MATLAB run {request.id} needs original-ID reconciliation: {exc}"
            ) from exc
        if result.id != request.id or result.request_sha256 != fingerprint(
            request.model_dump(mode="json")
        ):
            raise ExecutionUncertain("Local MATLAB receipt differs from the original request")
        if result.execution_status == "unknown":
            raise ExecutionUncertain("; ".join(result.diagnostics) or "Unknown local execution")
        if result.execution_status in ("failed", "interrupted"):
            error = ReconciledFailure if resume else ValueError
            raise error("; ".join(result.diagnostics) or result.execution_status)
        try:
            if isinstance(inputs, StepinfoInput):
                _series(context, inputs)
            return _outcome(context, request, result, directory)
        except (OSError, ValueError) as exc:
            raise ExecutionUncertain(f"Local MATLAB evidence changed: {exc}") from exc

    return [
        Capability[AgentContext, ProjectExecutionInput | StepinfoInput, CapabilityOutcome](
            name="matlab_execute_project",
            version="1",
            description="Execute a confirmed existing entry or model in a local project snapshot.",
            input_model=ProjectExecutionInput,
            output_model=CapabilityOutcome,
            effect="execute_project",
            operation=lambda context, inputs: execute(context, inputs, resume=False),
            reconcile=lambda context, inputs: execute(context, inputs, resume=True),
            readiness=readiness,
            metadata=CapabilityMetadata(
                resources=[
                    ResourceRequirement(role="project", kind="matlab_project", location="agent")
                ],
                conditions_schema={"type": "object"},
                validity_policy={"version": "1", "source": "verified_existing_project_outputs"},
                planning_notes=(
                    "Bind project metadata.project (MatlabProjectDescriptor), resource version 1. "
                    "Use discovered entries/settings only. Paired explicit selectors publish "
                    "details.series as an OutputArtifact, never arrays. No metrics or thresholds."
                ),
            ),
        ),
        Capability[AgentContext, ProjectExecutionInput | StepinfoInput, CapabilityOutcome](
            name="matlab_stepinfo",
            version="1",
            description="Compute MATLAB stepinfo from a verified dependency series artifact.",
            input_model=StepinfoInput,
            output_model=CapabilityOutcome,
            effect="execute_project",
            operation=lambda context, inputs: execute(context, inputs, resume=False),
            reconcile=lambda context, inputs: execute(context, inputs, resume=True),
            readiness=readiness,
            metadata=CapabilityMetadata(
                metrics=[
                    MetricDefinition(id="overshoot", label="Overshoot", unit="%"),
                    MetricDefinition(id="settling_time", label="Settling time", unit="s"),
                ],
                conditions_schema={"type": "object"},
                validity_policy={
                    "version": "1",
                    "source": "MATLAB_stepinfo_on_verified_published_series",
                    "settling_band": "explicit_finite_input_between_0_and_1",
                    "time_origin": "detected_step",
                    "initial_reference": "mean_pre_step_response",
                    "final_reference": "mean_verified_steady_state_tail",
                    "steady_state": "explicit_input_or_published_SteadyStatePolicy_defaults",
                },
                planning_notes=(
                    "Bind series from a direct dependency's details.series; conditions must "
                    "match its actual conditions. Explicit settling_band defines calculation "
                    "and plots, not acceptance. Invalid evidence retains diagnostics/plots "
                    "without valid observations. No signal, aircraft or trim constants."
                ),
            ),
        ),
    ]
