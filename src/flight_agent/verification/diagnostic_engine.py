from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..runtime.budget import BudgetAccount, BudgetExhausted
from ..runtime.capabilities import CapabilityRegistry
from ..runtime.contracts import AnalysisRun, AnalysisSpec, EvidenceFile, ExecutionScope, fingerprint
from ..runtime.evidence import load_run, persist_run, receipt_name
from ..runtime.loop import LoopDecision, run_loop
from .artifacts import describe_artifact
from .contracts import Artifact, DiagnosisTask, ExecutionNode, ToolCall
from .diagnosis_contracts import (
    DiagnosisBaseline,
    DiagnosisPlanRevision,
    DiagnosisReview,
    DiagnosisTermination,
    DiagnosticAnalysis,
    DiagnosticRound,
    Hypothesis,
    PendingAnalysis,
    StopReason,
    validate_assessments,
    validate_history,
    validate_review,
)
from .diagnostic_data import (
    DiagnosticContext,
    DiagnosticOutput,
    DiagnosticProject,
    inspect_diagnostic_project,
    snapshot_diagnostic_project,
)
from .intake import safe_directory, validate_output_root
from .planner import VerificationPlanner
from .store import now

Checkpoint = Callable[[DiagnosisTask], None]


class DiagnosticEngine:
    def __init__(
        self,
        tools: CapabilityRegistry[DiagnosticContext],
        planner: VerificationPlanner,
        *,
        persist_receipt: Callable[[Path, AnalysisRun], Path] | None = None,
    ):
        self.tools = tools
        self.planner = planner
        self._persist_receipt = persist_receipt

    def _scope(self, task: DiagnosisTask) -> ExecutionScope:
        return ExecutionScope(
            baseline_version=task.baseline.version,
            confirmed=task.baseline.confirmed,
            design=task.baseline.design,
            allowed_effects=("read_only", "write_delivery"),
        )

    def _spec(
        self,
        baseline: DiagnosisBaseline,
        project: DiagnosticProject,
        analysis: DiagnosticAnalysis,
        *,
        hypotheses: list[Hypothesis] | None = None,
    ) -> AnalysisSpec:
        if analysis.capability not in baseline.allowed_capabilities:
            raise ValueError("Analysis capability is outside the confirmed diagnosis scope")
        current = baseline.hypotheses if hypotheses is None else hypotheses
        if not set(analysis.hypothesis_ids).issubset(item.id for item in current):
            raise ValueError("Analysis refers to an unknown hypothesis")
        definition = self.tools.definition(analysis.capability)
        if (
            definition.effect != "read_only"
            or baseline.capability_versions.get(analysis.capability) != definition.version
        ):
            raise ValueError("Diagnostic capability version changed after baseline proposal")
        files = {item.path: item.sha256 for item in project.files}
        return AnalysisSpec(
            capability=analysis.capability,
            capability_version=definition.version,
            design=baseline.design,
            inputs=self.tools.validate_inputs(analysis.capability, analysis.inputs),
            data_fingerprints={item.id: files[item.path] for item in project.manifest.datasets},
            conditions=project.manifest.conditions,
            numerics={"project_fingerprint": baseline.project_fingerprint},
        )

    def _plan(
        self,
        baseline: DiagnosisBaseline,
        project: DiagnosticProject,
        analyses: list[DiagnosticAnalysis],
        reason: str,
        version: int,
        evidence: list[str],
        *,
        hypotheses: list[Hypothesis] | None = None,
    ) -> DiagnosisPlanRevision:
        specs = [self._spec(baseline, project, item, hypotheses=hypotheses) for item in analyses]
        return DiagnosisPlanRevision(
            version=version,
            baseline_version=baseline.version,
            reason=reason,
            evidence=evidence,
            analysis_specs=specs,
            analyses=[
                analysis.model_copy(update={"inputs": spec.inputs})
                for analysis, spec in zip(analyses, specs, strict=True)
            ],
        )

    def _nodes(self, plan: DiagnosisPlanRevision) -> list[ExecutionNode]:
        return [
            ExecutionNode(
                id=f"round-{plan.version}-{index + 1}",
                tool=analysis.capability,
                inputs=analysis.inputs,
            )
            for index, analysis in enumerate(plan.analyses)
        ]

    def _verify_plan(
        self,
        task: DiagnosisTask,
        plan: DiagnosisPlanRevision,
        *,
        hypotheses: list[Hypothesis] | None = None,
        analyses: list[DiagnosticAnalysis] | None = None,
    ) -> None:
        if plan.baseline_version != task.baseline.version:
            raise ValueError("Diagnostic plan is bound to a different baseline")
        expected = self._plan(
            task.baseline,
            task.project,
            plan.analyses if analyses is None else analyses,
            plan.reason,
            plan.version,
            plan.evidence,
            hypotheses=hypotheses,
        )
        if plan != expected:
            raise ValueError("Persisted diagnostic plan specifications do not match the baseline")

    def _inputs(self, task: DiagnosisTask) -> None:
        target = Path(task.result_directory) / "inputs"
        if task.input_snapshot is None:
            self._scope(task).authorize("write_delivery")
            task.input_snapshot = snapshot_diagnostic_project(task.project, target)
        elif task.input_snapshot.root != str(target):
            raise ValueError("Input snapshot does not match the task directory")
        current = inspect_diagnostic_project(target)
        if (
            current.fingerprint != task.baseline.project_fingerprint
            or current.fingerprint != task.input_snapshot.fingerprint
            or task.project.fingerprint != task.baseline.project_fingerprint
            or task.project.manifest.design != task.baseline.design
            or current.manifest != task.input_snapshot.manifest
            or current.files != task.input_snapshot.files
            or current.manifest != task.project.manifest
            or current.files != task.project.files
        ):
            raise ValueError("Diagnostic input snapshot integrity mismatch")
        self._merge_artifacts(
            task,
            [
                describe_artifact(Path(task.result_directory), f"inputs/{item.path}")
                for item in current.files
            ],
        )

    def _output(self, run: AnalysisRun) -> DiagnosticOutput:
        result = DiagnosticOutput.model_validate(
            self.tools.validate_result(run.spec.capability, run.output)
        )
        if result.valid and not result.data_fingerprints:
            raise ValueError("Valid diagnostic evidence must identify its source data")
        if any(
            run.spec.data_fingerprints.get(key) != value
            for key, value in result.data_fingerprints.items()
        ):
            raise ValueError("Diagnostic output contains unregistered source fingerprints")
        return result

    def _verified_run(self, task: DiagnosisTask, run_id: str, spec: AnalysisSpec) -> AnalysisRun:
        if not any(spec == saved for plan in task.plan_revisions for saved in plan.analysis_specs):
            raise ValueError("Diagnostic evidence is outside the persisted baseline plans")
        run = next((item for item in task.analysis_runs if item.id == run_id), None)
        if run is None or load_run(Path(task.result_directory), run_id, spec) != run:
            raise ValueError("Recorded diagnostic evidence receipt is missing or inconsistent")
        self._output(run)
        return run

    def _accept(
        self,
        task: DiagnosisTask,
        node: ExecutionNode,
        call: ToolCall,
        run: AnalysisRun,
        source: str,
        checkpoint: Checkpoint,
    ) -> AnalysisRun:
        self._output(run)
        previous = next((item for item in task.analysis_runs if item.id == run.id), None)
        if previous is not None and previous != run:
            raise ValueError("An existing diagnostic evidence record cannot be overwritten")
        if previous is None:
            task.analysis_runs.append(run)
        self._merge_artifacts(
            task, [describe_artifact(Path(task.result_directory), receipt_name(run.id))]
        )
        call.output = {"analysis_run_id": run.id, "source": source}
        call.status = "completed"
        call.finished_at = now()
        call.error = None
        node.status = "completed"
        node.error = None
        task.pending_analysis = None
        checkpoint(task)
        return run

    def _execute(
        self,
        task: DiagnosisTask,
        plan: DiagnosisPlanRevision,
        index: int,
        node: ExecutionNode,
        *,
        checkpoint: Checkpoint,
        budget: BudgetAccount | None,
    ) -> AnalysisRun | None:
        spec = plan.analysis_specs[index]
        directory = Path(task.result_directory)
        if node.status == "completed":
            call = next(
                (
                    item
                    for item in reversed(task.calls)
                    if item.node_id == node.id and item.status == "completed"
                ),
                None,
            )
            run_id = call.output.get("analysis_run_id") if call else None
            if not isinstance(run_id, str):
                raise ValueError("Completed analysis node lacks its evidence reference")
            return self._verified_run(task, run_id, spec)
        pending = task.pending_analysis
        if pending is not None:
            if (
                pending.plan_version != plan.version
                or pending.analysis_index != index
                or pending.spec != spec
            ):
                raise ValueError("Pending analysis does not match its saved plan")
            call = next((item for item in task.calls if item.id == pending.id), None)
            if call is None or call.node_id != node.id or call.tool != spec.capability:
                raise ValueError("Pending analysis lacks its original durable tool call")
            saved = load_run(directory, pending.id, pending.spec)
            if saved is None:
                self._stop(
                    task,
                    "unknown_execution",
                    "Original analysis has no complete receipt; it will not be redispatched.",
                )
                node.status = "interrupted"
                call.status = "interrupted"
                call.error = task.termination.detail if task.termination else None
                call.finished_at = now()
                return None
            return self._accept(task, node, call, saved, "recovered", checkpoint)
        cached = next(
            (run for run in task.analysis_runs if run.spec.fingerprint == spec.fingerprint), None
        )
        if cached is None and self._budget(task, for_dispatch=True, budget=budget):
            return None
        if cached is None and budget is not None:
            budget.charge_call()
        call = ToolCall(
            id=uuid4().hex,
            node_id=node.id,
            tool=spec.capability,
            tool_version=spec.capability_version,
            baseline_version=task.baseline.version,
            started_at=now(),
            inputs={"spec": spec.model_dump(mode="json"), "plan_version": plan.version},
        )
        task.calls.append(call)
        node.status = "running"
        task.stage = spec.capability
        if cached is not None:
            return self._accept(
                task, node, call, self._verified_run(task, cached.id, spec), "cache", checkpoint
            )
        task.pending_analysis = PendingAnalysis(
            id=call.id,
            spec=spec,
            plan_version=plan.version,
            analysis_index=index,
            started_at=call.started_at,
        )
        task.dispatch_count += 1
        checkpoint(task)
        if task.input_snapshot is None:
            raise ValueError("Analysis requires a verified input snapshot")
        output = self.tools.invoke(
            spec.capability,
            spec.inputs,
            DiagnosticContext(project=task.input_snapshot.model_copy(deep=True)),
            self._scope(task),
        )
        run = AnalysisRun(
            id=call.id,
            spec=spec,
            output=output,
            output_sha256=fingerprint(output),
            started_at=call.started_at,
            finished_at=now(),
            artifacts=[
                EvidenceFile(path=f"inputs/{item.path}", sha256=item.sha256, size=item.size)
                for item in task.input_snapshot.files
            ],
        )
        self._output(run)
        (self._persist_receipt or persist_run)(directory, run)
        return self._accept(task, node, call, run, "execution", checkpoint)

    def _review(self, task: DiagnosisTask, review: DiagnosisReview) -> list[Hypothesis]:
        if review.summary is None:
            raise ValueError("A new diagnosis review requires an explicit structured summary")
        return validate_review(task.current_hypotheses, review, task.analysis_runs)

    def _stop(self, task: DiagnosisTask, reason: StopReason, detail: str) -> None:
        unknowns = [
            *(item.reason for item in task.assessments if item.status == "unknown"),
            *(task.rounds[-1].review.unknowns if task.rounds else []),
            *(item for run in task.analysis_runs for item in self._output(run).unknowns),
            "Recorded evidence is not a unique physical root-cause "
            "or engineering-acceptance proof.",
        ]
        if task.project.manifest.synthetic:
            unknowns.append(
                "Synthetic data only; real labeled engineering evidence is unavailable."
            )
        if reason != "evidence_sufficient":
            unknowns.insert(0, detail)
        task.termination = DiagnosisTermination(
            reason=reason,
            detail=detail,
            created_at=now(),
            unknowns=list(dict.fromkeys(unknowns))[:40],
        )

    def _budget(
        self,
        task: DiagnosisTask,
        *,
        for_dispatch: bool = False,
        budget: BudgetAccount | None = None,
        for_round: bool = False,
        for_review: bool = False,
    ) -> bool:
        elapsed = (
            (datetime.now(UTC) - datetime.fromisoformat(task.started_at)).total_seconds()
            if task.started_at
            else 0
        )
        if elapsed >= task.baseline.budget.max_seconds or (
            budget is not None and budget.remaining_seconds() <= 0
        ):
            self._stop(task, "time_budget", "The confirmed elapsed-time budget is exhausted.")
        elif budget is not None and budget.state.no_progress >= budget.budget.max_no_progress:
            self._stop(task, "no_progress", "The shared no-progress budget is exhausted.")
        elif (
            for_dispatch
            and task.dispatch_count >= task.baseline.budget.max_calls
            or (
                (for_dispatch or for_review)
                and budget is not None
                and budget.state.calls >= budget.budget.max_calls
            )
        ):
            self._stop(task, "call_budget", "The confirmed call budget is exhausted.")
        elif for_round and budget is not None and budget.state.rounds >= budget.budget.max_rounds:
            self._stop(task, "round_budget", "The shared round budget is exhausted.")
        else:
            return False
        return True

    def run(
        self,
        task: DiagnosisTask,
        *,
        checkpoint: Checkpoint,
        budget: BudgetAccount | None = None,
    ) -> DiagnosisTask:
        self._scope(task).authorize("read_only")
        validate_output_root(task.result_directory, task.project.root)
        safe_directory(Path(task.result_directory))
        Path(task.result_directory).mkdir(parents=True, exist_ok=True)
        task.started_at = task.started_at or now()
        checkpoint(task)
        self._inputs(task)
        current = validate_history(
            task.baseline.hypotheses, task.plan_revisions, task.rounds, task.analysis_runs
        )
        validate_assessments(current, task.assessments, task.analysis_runs)
        if task.rounds and task.assessments != task.rounds[-1].review.assessments:
            raise ValueError("Recorded assessments differ from the final evidence review")
        if len(task.rounds) == len(task.plan_revisions) and task.termination is None:
            raise ValueError("Completed diagnostic rounds require an explicit stop reason")
        hypotheses = task.baseline.hypotheses
        for index, plan in enumerate(task.plan_revisions):
            prior = task.rounds[index - 1].review if index else None
            if prior and prior.hypothesis_revision:
                hypotheses = prior.hypothesis_revision.hypotheses
            self._verify_plan(
                task,
                plan,
                hypotheses=hypotheses,
                analyses=prior.analyses if prior else None,
            )
        expected = [
            node.model_dump(include={"id", "tool", "inputs", "depends_on"})
            for plan in task.plan_revisions
            for node in self._nodes(plan)
        ]
        actual = [
            node.model_dump(include={"id", "tool", "inputs", "depends_on"}) for node in task.nodes
        ]
        if actual != expected:
            raise ValueError("Persisted analysis nodes differ from their plan revisions")
        for recorded in task.analysis_runs:
            self._verified_run(task, recorded.id, recorded.spec)
        checkpoint(task)

        def decide() -> LoopDecision:
            return LoopDecision(
                action="stop" if task.termination is not None else "continue",
                reason=task.termination.detail
                if task.termination
                else "Execute and review the next confirmed evidence round",
            )

        def execute() -> None:
            plan = task.plan_revisions[-1]
            phase = f"round-{plan.version}"
            charge_key = f"diagnostic-{task.id}-{plan.version}"
            started = (
                (budget is not None and charge_key in budget.state.round_charges)
                or task.stage == phase
                or any(call.node_id.startswith(f"{phase}-") for call in task.calls)
            )
            if not started:
                if self._budget(task, for_round=True, budget=budget):
                    return
                if budget is not None:
                    budget.charge_round(key=charge_key)
                task.stage = phase
                checkpoint(task)
            run_ids = []
            for index in range(len(plan.analyses)):
                node = next(
                    item for item in task.nodes if item.id == f"round-{plan.version}-{index + 1}"
                )
                run = self._execute(task, plan, index, node, checkpoint=checkpoint, budget=budget)
                if run is None:
                    break
                run_ids.append(run.id)
            if task.termination is not None or self._budget(task, for_review=True, budget=budget):
                return
            task.stage = "review_evidence"
            checkpoint(task)
            planner = self.planner
            if budget is not None:
                budget.charge_call()
                planner = self.planner.with_timeout(
                    min(
                        budget.remaining_seconds(),
                        task.baseline.budget.max_seconds
                        - (
                            datetime.now(UTC) - datetime.fromisoformat(task.started_at or now())
                        ).total_seconds(),
                    )
                )
            review = planner.review_diagnosis(
                task,
                tools=[
                    item
                    for item in self.tools.catalog()
                    if item["name"] in task.baseline.allowed_capabilities
                ],
            )
            current = self._review(task, review)
            next_plan = (
                self._plan(
                    task.baseline,
                    task.project,
                    review.analyses,
                    review.reason,
                    plan.version + 1,
                    run_ids,
                    hypotheses=current,
                )
                if review.action == "continue"
                else None
            )
            progressed = any(
                call.node_id.startswith(f"round-{plan.version}-")
                and call.output.get("source") != "cache"
                and call.output.get("analysis_run_id")
                in {run.id for run in task.analysis_runs if self._output(run).valid}
                for call in task.calls
            )
            task.no_progress_rounds = 0 if progressed else task.no_progress_rounds + 1
            task.assessments = review.assessments
            task.rounds.append(
                DiagnosticRound(
                    plan_version=plan.version,
                    run_ids=run_ids,
                    review=review,
                    created_at=now(),
                )
            )
            if budget is not None:
                try:
                    budget.note_progress(progressed)
                except BudgetExhausted:
                    if budget.remaining_seconds() <= 0:
                        self._stop(
                            task, "time_budget", "The shared elapsed-time budget is exhausted."
                        )
                    else:
                        self._stop(
                            task, "no_progress", "The shared no-progress budget is exhausted."
                        )
                    return
            if review.action == "stop":
                if review.stop_reason is None:
                    raise ValueError("Diagnostic stop reason is missing")
                self._stop(task, review.stop_reason, review.reason)
            elif task.no_progress_rounds >= task.baseline.budget.max_no_progress:
                self._stop(task, "no_progress", "No new valid evidence was obtained.")
            elif len(task.rounds) >= task.baseline.budget.max_rounds:
                self._stop(
                    task, "round_budget", "The confirmed diagnostic round budget is exhausted."
                )
            elif not self._budget(task, budget=budget) and next_plan is not None:
                task.plan_revisions.append(next_plan)
                task.nodes.extend(self._nodes(next_plan))

        run_loop(
            decide=decide,
            execute=execute,
            persist=lambda: checkpoint(task),
            scope=self._scope(task),
            max_steps=task.baseline.budget.max_rounds,
        )
        checkpoint(task)
        return task

    def _merge_artifacts(self, task: DiagnosisTask, artifacts: list[Artifact]) -> None:
        merged = {item.path: item for item in task.artifacts}
        merged.update({item.path: item for item in artifacts})
        task.artifacts = list(merged.values())
