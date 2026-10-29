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
    AllmarginInput,
    LinearizeInput,
    LocalMatlabInput,
    LocalMatlabRequest,
    LocalMatlabResult,
    MatlabExecutionMetadata,
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


def _linear_model(context: AgentContext, inputs: AllmarginInput) -> list[OutputArtifact]:
    model = inputs.linear_model
    for producer, output in context.outputs.items():
        if (
            model.node_id == producer and model.id == "linear_model"
            and model in output.artifacts
            and output.details.get("linear_model") == model.model_dump(mode="json")
            and output.details.get("operation") == "linearize"
            and output.details.get("valid") is True
            and output.details.get("execution_status") == "completed"
            and model.path == f"calls/{output.details.get('call_id')}/published/linear-model.mat"
        ):
            if output.details.get("conditions") != inputs.conditions:
                raise ValueError("Margin conditions differ from the linear model conditions")
            metadata = next(
                (item for item in output.artifacts
                 if item.id == "metadata" and item.node_id == producer
                 and item.path == f"calls/{output.details.get('call_id')}/published/metadata.json"),
                None,
            )
            if metadata is None:
                raise ValueError("Missing published linear model producer metadata")
            verify_evidence(context.directory, model)
            path = verify_evidence(context.directory, metadata)
            actual = _read_model(path, MatlabExecutionMetadata)
            if (
                actual.id != output.details.get("call_id")
                or not isinstance(actual.inputs, LinearizeInput)
                or actual.conditions != inputs.conditions
                or actual.inputs.conditions != inputs.conditions
            ):
                raise ValueError("Linear model producer metadata/conditions differ")
            return [model, metadata]
    raise ValueError("allmargin requires the exact published linear model of a direct dependency")


def _request(
    context: AgentContext, inputs: LocalMatlabInput, *, resume: bool
) -> LocalMatlabRequest:
    context.scope.authorize("execute_project")
    project = (
        _project(context) if isinstance(inputs, (ProjectExecutionInput, LinearizeInput)) else None
    )
    saved = context.load_checkpoint()
    operation: Literal["execute_project", "linearize", "stepinfo", "allmargin"] = (
        "execute_project" if isinstance(inputs, ProjectExecutionInput) else
        "linearize" if isinstance(inputs, LinearizeInput) else
        "stepinfo" if isinstance(inputs, StepinfoInput) else "allmargin"
    )
    evidence = (
        [inputs.series] if isinstance(inputs, StepinfoInput) else
        _linear_model(context, inputs) if isinstance(inputs, AllmarginInput) else []
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
            input_evidence=evidence,
            timeout_seconds=timeout,
        )
        context.save_checkpoint({"request": request.model_dump(mode="json")})
    if (
        request.id != context.call_id
        or request.operation != operation
        or request.inputs != inputs
        or request.project != project
        or request.input_evidence != evidence
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
        "operation": request.operation,
    }
    metadata = _read_model(directory / "metadata.json", MatlabExecutionMetadata)
    details.update(
        matlab_version=metadata.matlab_version,
        matlab_release=metadata.matlab_release,
        settings=metadata.settings,
        inputs=request.inputs.model_dump(mode="json"),
    )
    if request.project is not None:
        details["project_fingerprint"] = request.project.fingerprint
    for artifact in artifacts:
        if artifact.id in {"series", "linear_model"}:
            details[artifact.id] = artifact.model_dump(mode="json")
    observations = []
    if isinstance(request.inputs, (StepinfoInput, AllmarginInput)) and result.valid:
        metrics = result.details["metrics"]
        assert isinstance(metrics, dict)
        for name in (
            ("overshoot", "settling_time") if isinstance(request.inputs, StepinfoInput)
            else ("gain_margin", "phase_margin")
        ):
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
) -> list[Capability[AgentContext, LocalMatlabInput, CapabilityOutcome]]:
    def readiness(resources: tuple[ResourceReference, ...]) -> Availability:
        return backend.readiness()

    def execute(
        context: AgentContext, inputs: LocalMatlabInput, *, resume: bool
    ) -> CapabilityOutcome:
        request = _request(context, inputs, resume=resume)
        directory = context.directory / f"calls/{context.call_id}/local-matlab"
        if not resume and isinstance(inputs, StepinfoInput):
            source = _series(context, inputs)
            # Backend evidence locators are relative to the enclosing call directory.
            publish_evidence(directory.parent, inputs.series.path, source.read_bytes())
            _series(context, inputs)
        if not resume and isinstance(inputs, AllmarginInput):
            for item in _linear_model(context, inputs):
                source = verify_evidence(context.directory, item)
                publish_evidence(directory.parent, item.path, source.read_bytes())
            _linear_model(context, inputs)
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
            if isinstance(inputs, AllmarginInput):
                _linear_model(context, inputs)
            return _outcome(context, request, result, directory)
        except (OSError, ValueError) as exc:
            raise ExecutionUncertain(f"Local MATLAB evidence changed: {exc}") from exc

    return [
        Capability[AgentContext, LocalMatlabInput, CapabilityOutcome](
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
        Capability[AgentContext, LocalMatlabInput, CapabilityOutcome](
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
        Capability[AgentContext, LocalMatlabInput, CapabilityOutcome](
            name="matlab_linearize", version="1",
            description=(
                "Linearize a sourced existing model at its explicitly selected operating point."
            ),
            input_model=LinearizeInput, output_model=CapabilityOutcome, effect="execute_project",
            operation=lambda context, inputs: execute(context, inputs, resume=False),
            reconcile=lambda context, inputs: execute(context, inputs, resume=True),
            readiness=readiness,
            metadata=CapabilityMetadata(
                resources=[
                    ResourceRequirement(role="project", kind="matlab_project", location="agent")
                ],
                conditions_schema={"type": "object"},
                validity_policy={
                    "version": "1", "source": "existing_active_SISO_getlinio_points",
                    "operating_point": "explicit_sourced_initialized_or_workspace",
                    "feedback_sign": "explicit_sourced_no_default",
                    "artifact": "hash_verified_immutable_MAT_with_runtime_provenance",
                },
                planning_notes=(
                    "Bind exact project. Use discovered model and authoritative initializer; "
                    "never replace initializer with a preparation. Preparations run before "
                    "initialization within this isolated node; discovered functions accept JSON "
                    "varargin. Sources are project documentation/workspace_sources paths, "
                    "setting locations or authorized user-message references, with exact quotes. "
                    "Map preparation.argument_conditions to sourced conditions for a symbolic "
                    "documented call. workspace_assertions compare a workspace field to its named "
                    "condition and record actual values; cover every claimed condition. "
                    "operating_point.mode initialized "
                    "means operpoint after initialization, not trim search; workspace needs path. "
                    "feedback_sign must follow engineering declarations, never guessed. Uses "
                    "one existing active openinput and openoutput; rejects other loops. Publishes "
                    "details.linear_model for optional dependency analysis. No fixed sequence."
                ),
            ),
        ),
        Capability[AgentContext, LocalMatlabInput, CapabilityOutcome](
            name="matlab_allmargin", version="1",
            description="Compute SISO loop margins from a verified dependency linear-model MAT.",
            input_model=AllmarginInput, output_model=CapabilityOutcome, effect="execute_project",
            operation=lambda context, inputs: execute(context, inputs, resume=False),
            reconcile=lambda context, inputs: execute(context, inputs, resume=True),
            readiness=readiness,
            metadata=CapabilityMetadata(
                metrics=[
                    MetricDefinition(
                        id="gain_margin", label="Gain margin", unit="dB",
                        comparable_specials=["positive_infinity", "negative_infinity"],
                    ),
                    MetricDefinition(
                        id="phase_margin", label="Phase margin", unit="deg",
                        comparable_specials=["positive_infinity", "negative_infinity"],
                    ),
                ],
                conditions_schema={"type": "object"},
                validity_policy={
                    "version": "1",
                    "source": "MATLAB_allmargin_on_verified_dependency_linear_model",
                    "reduction": "least_margin_across_all_retained_crossings",
                    "specials": "explicit_signed_infinities_undefined_is_invalid",
                    "nominal_stability": "reported_separately_not_threshold_compliance",
                },
                planning_notes=(
                    "Bind linear_model only from a direct matlab_linearize dependency's "
                    "['details','linear_model']; never invent artifacts or accept directories. "
                    "Conditions must equal producer and criterion conditions. Retains crossings, "
                    "closed_loop_stable, validity, diagnostics, raw MAT and bode plot. Negative "
                    "margins and nominal instability are reported, not masked. Optional generic "
                    "evaluation/delivery only as requested; no threshold defaults."
                ),
            ),
        ),
    ]
