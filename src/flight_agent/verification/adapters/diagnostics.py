import json
from typing import Self

from openai import APIError
from pydantic import model_validator

from ...runtime.agent_contracts import AgentNode, AgentPlan
from ...runtime.capabilities import Capability
from ...runtime.capability_metadata import CapabilityMetadata, ResourceRequirement
from ...runtime.contracts import Name, Version
from ...runtime.observations import CapabilityOutcome, Finding, OutputArtifact, ResultTable
from ...runtime.resources import AgentContract, ResourceReference
from ..agent_runtime import AgentContext, ExecutionUncertain
from ..diagnosis_contracts import DiagnosisBaseline, DiagnosisProposal, HypothesisAssessment
from ..diagnostic_data import (
    DiagnosticManifest,
    DiagnosticProject,
    diagnostic_capabilities,
    inspect_diagnostic_project,
)
from ..diagnostic_engine import DiagnosticEngine
from ..planner import VerificationPlanner


class DiagnosisInput(AgentContract):
    proposal: DiagnosisProposal
    capability_versions: dict[Name, Version]

    @model_validator(mode="after")
    def confirmed_scope(self) -> Self:
        if self.proposal.action != "plan":
            raise ValueError("Diagnosis capability requires an explicit analysis plan")
        if set(self.capability_versions) != set(self.proposal.allowed_capabilities):
            raise ValueError("Diagnosis capability versions must cover exactly the proposed scope")
        return self


def _manifest(resource: ResourceReference) -> DiagnosticManifest:
    if (resource.kind, resource.location) != ("diagnostic_data", "agent"):
        raise ValueError("Diagnosis requires registered Agent-visible diagnostic data")
    return DiagnosticManifest.model_validate(resource.metadata.get("manifest"))


def _project(resource: ResourceReference) -> DiagnosticProject:
    manifest = _manifest(resource)
    project = inspect_diagnostic_project(resource.locator)
    if project.fingerprint != resource.fingerprint or project.manifest != manifest:
        raise ValueError("Diagnostic manifest or data fingerprint changed")
    return project


def _validate_inputs(inputs: DiagnosisInput) -> None:
    tools = diagnostic_capabilities()
    for name in inputs.proposal.allowed_capabilities:
        definition = tools.definition(name)
        if definition.effect != "read_only":
            raise ValueError("Diagnosis only permits registered read-only analysis capabilities")
        if definition.version != inputs.capability_versions[name]:
            raise ValueError("Diagnostic capability version changed after proposal")
    for analysis in inputs.proposal.analyses:
        tools.validate_inputs(analysis.capability, analysis.inputs)


def _validate_plan(plan: AgentPlan, node: AgentNode) -> None:
    inputs = DiagnosisInput.model_validate(node.inputs)
    _validate_inputs(inputs)
    resource = next(
        item for item in plan.resources if item.id == node.resource_bindings["diagnostic_data"]
    )
    _manifest(resource)


def diagnostic_capability(
    planner: VerificationPlanner,
) -> Capability[AgentContext, DiagnosisInput, CapabilityOutcome]:
    def execute(
        context: AgentContext,
        inputs: DiagnosisInput,
        *,
        resume: bool,
    ) -> CapabilityOutcome:
        from ..contracts import DiagnosisTask

        context.scope.authorize("read_only")
        resource = context.resource("diagnostic_data")
        if resource not in context.scope.resources:
            raise ValueError("Diagnostic resource is outside the confirmed node scope")
        _validate_inputs(inputs)
        saved = context.load_checkpoint()
        original = None
        if resume:
            if saved is None or "task" not in saved:
                raise ExecutionUncertain("Original diagnostic checkpoint is missing")
            original = DiagnosisTask.model_validate(saved["task"])
            project = original.project
            if (
                project.root != resource.locator
                or project.fingerprint != resource.fingerprint
                or project.manifest != _manifest(resource)
            ):
                raise ValueError(
                    "Diagnostic checkpoint resource differs from its confirmed identity"
                )
        else:
            if saved is not None:
                raise ExecutionUncertain("Existing diagnostic checkpoint requires reconciliation")
            project = _project(resource)
        engine = DiagnosticEngine(diagnostic_capabilities(), planner)
        prefix = f"calls/{context.call_id}/diagnosis"
        directory = context.directory / prefix
        baseline = DiagnosisBaseline(
            version=context.scope.baseline_version,
            project_fingerprint=project.fingerprint,
            design=project.manifest.design,
            objective=inputs.proposal.title,
            hypotheses=inputs.proposal.hypotheses,
            allowed_capabilities=inputs.proposal.allowed_capabilities,
            capability_versions=inputs.capability_versions,
            budget=inputs.proposal.budget,
            confirmed=True,
        )
        plan = engine._plan(
            baseline,
            project,
            inputs.proposal.analyses,
            inputs.proposal.rationale,
            1,
            [],
        )
        task = DiagnosisTask(
            id=context.call_id,
            project=project,
            baseline=baseline,
            proposal=inputs.proposal,
            result_directory=str(directory),
            plan_revisions=[plan],
            nodes=engine._nodes(plan),
            assessments=[
                HypothesisAssessment(
                    hypothesis_id=item.id,
                    status="unknown",
                    reason="No confirmed analysis executed.",
                )
                for item in baseline.hypotheses
            ],
        )
        if original is not None:
            if any(
                getattr(original, field) != getattr(task, field)
                for field in (
                    "id",
                    "project",
                    "baseline",
                    "proposal",
                    "result_directory",
                    "output_root",
                )
            ) or original.plan_revisions[:1] != [plan]:
                raise ValueError("Diagnostic checkpoint differs from its confirmed invocation")
            task = original
            if task.termination and task.termination.reason == "unknown_execution":
                task.termination = None

        def checkpoint(current: DiagnosisTask) -> None:
            context.save_checkpoint({"task": current.model_dump(mode="json")})

        checkpoint(task)
        try:
            engine.run(task, checkpoint=checkpoint, budget=context.budget)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            if task.pending_analysis is not None:
                raise ExecutionUncertain(
                    f"Original diagnostic analysis needs reconciliation: {exc}"
                ) from exc
            if isinstance(exc, APIError):
                raise ValueError(f"Diagnostic evidence review failed: {exc}") from exc
            raise
        if task.pending_analysis is not None:
            raise ExecutionUncertain("Original diagnostic analysis has no verified receipt")
        if task.termination is None:
            raise ValueError("Diagnosis stopped without an explicit termination")
        artifacts = [
            OutputArtifact(
                node_id=context.node_id,
                path=f"{prefix}/{item.path}",
                sha256=item.sha256,
                size=item.size,
            )
            for item in task.artifacts
        ]
        evidence = [f"{prefix}/numerical/analysis-runs/{run.id}.json" for run in task.analysis_runs]
        summary = task.rounds[-1].review.summary if task.rounds else None
        findings = [
            Finding(
                id="diagnosis_summary",
                node_id=context.node_id,
                text=(
                    f"{summary.problem} {summary.localization} {summary.reason}"
                    if summary
                    else task.termination.detail
                ),
                status=summary.responsibility if summary else "undetermined",
                evidence=evidence,
            ),
            *(
                Finding(
                    id=item.hypothesis_id,
                    node_id=context.node_id,
                    text=item.reason,
                    status=item.status,
                    evidence=[
                        f"{prefix}/numerical/analysis-runs/{citation.run_id}.json"
                        for citation in item.evidence
                    ],
                )
                for item in task.assessments
            ),
        ]
        return CapabilityOutcome(
            findings=findings,
            artifacts=artifacts,
            tables=[
                ResultTable(
                    id="diagnostic_unknowns",
                    node_id=context.node_id,
                    columns=["Unknown or limitation"],
                    rows=[[item] for item in task.termination.unknowns],
                    evidence=evidence,
                )
            ],
            details={
                "termination": task.termination.model_dump(mode="json"),
                "summary": summary.model_dump(mode="json") if summary else None,
                "internal_run_ids": [run.id for run in task.analysis_runs],
                "design": project.manifest.design.model_dump(mode="json"),
                "conditions": project.manifest.conditions,
                "synthetic": project.manifest.synthetic,
            },
        )

    return Capability(
        name="offline_diagnosis",
        version="1",
        description="Diagnose registered offline evidence through bounded read-only rounds.",
        input_model=DiagnosisInput,
        output_model=CapabilityOutcome,
        effect="read_only",
        operation=lambda context, inputs: execute(context, inputs, resume=False),
        reconcile=lambda context, inputs: execute(context, inputs, resume=True),
        validate_plan=_validate_plan,
        metadata=CapabilityMetadata(
            resources=[
                ResourceRequirement(
                    role="diagnostic_data",
                    kind="diagnostic_data",
                    location="agent",
                )
            ],
            validity_policy={
                "version": "1",
                "basis": "Authenticated original internal analysis receipts",
                "limitations": "Diagnostic support is not unique causation or acceptance proof.",
            },
            planning_notes=(
                "Confirm hypotheses, exact read-only internal capabilities/versions and budget. "
                "Use the registered diagnostic manifest and data summary. No Worker is required. "
                "Outputs: finding diagnosis_summary, hypothesis findings, "
                "and table diagnostic_unknowns. "
                "Internal analyses and evidence reviews consume the shared outer budget.\n"
                + json.dumps(
                    [
                        {key: item[key] for key in ("name", "version", "effect", "input_schema")}
                        for item in diagnostic_capabilities().catalog()
                    ],
                    separators=(",", ":"),
                )
            ),
        ),
    )
