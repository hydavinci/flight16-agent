import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from openai import APIError

from ..runtime.capabilities import CapabilityRegistry
from ..runtime.contracts import AnalysisRun, AnalysisSpec, EvidenceFile, ExecutionScope, fingerprint
from ..runtime.evidence import load_run, persist_run, receipt_name
from .artifacts import describe_artifact
from .contracts import (
    Artifact,
    Conversation,
    ConversationMessage,
    Decision,
    DiagnosisTask,
    ExecutionNode,
    PendingDecision,
    ToolCall,
)
from .diagnosis_contracts import (
    DiagnosisBaseline,
    DiagnosisPlanRevision,
    DiagnosisReview,
    DiagnosisTermination,
    DiagnosticAnalysis,
    DiagnosticRound,
    HypothesisAssessment,
    PendingAnalysis,
    StopReason,
    validate_assessments,
)
from .diagnosis_reports import deliver_diagnosis_report
from .diagnostic_data import (
    DiagnosticContext,
    DiagnosticOutput,
    DiagnosticProject,
    diagnostic_capabilities,
    inspect_diagnostic_project,
    snapshot_diagnostic_project,
)
from .intake import validate_output_root
from .planner import VerificationPlanner
from .store import ConversationStore, now

logger = logging.getLogger(__name__)


class DiagnosisRuntime:
    def __init__(
        self,
        store: ConversationStore,
        planner: VerificationPlanner,
        tools: CapabilityRegistry[DiagnosticContext] | None = None,
    ):
        self.store = store
        self.planner = planner
        self.tools = tools or diagnostic_capabilities()

    def propose(self, conversation: Conversation, message: str) -> Conversation:
        if conversation.project_root is None:
            raise ValueError("D2 requires an Agent-visible registered diagnostic data directory")
        project = inspect_diagnostic_project(conversation.project_root)
        effective = conversation.output_root or str(self.store.default_task_root(conversation.id))
        resolved = validate_output_root(effective, project.root)
        output_root = resolved if conversation.output_root is not None else None
        proposal = self.planner.propose_diagnosis(
            message, conversation, project, tools=self.tools.catalog()
        )
        if proposal.action == "clarify":
            restored = conversation.restore_turn_origin()
            if not restored:
                conversation.status = "waiting_for_user"
            kind = (
                conversation.pending_decision.kind
                if restored and conversation.pending_decision else "clarify"
            )
            conversation.pending_decision = PendingDecision(
                kind=kind, message=proposal.question
            )
            self._message(conversation, "assistant", proposal.question)
            self.store.save(conversation)
            return conversation
        versions = {}
        for name in proposal.allowed_capabilities:
            definition = self.tools.definition(name)
            if definition.effect != "read_only":
                raise ValueError("D2 only permits registered read-only analysis capabilities")
            versions[name] = definition.version
        prior = [*conversation.archived_tasks, *([conversation.task] if conversation.task else [])]
        version = max((task.baseline.version for task in prior), default=0) + 1
        baseline = DiagnosisBaseline(
            version=version,
            project_fingerprint=project.fingerprint,
            design=project.manifest.design,
            objective=proposal.title,
            hypotheses=proposal.hypotheses,
            allowed_capabilities=proposal.allowed_capabilities,
            capability_versions=versions,
            budget=proposal.budget,
        )
        plan = self._plan(baseline, project, proposal.analyses, proposal.rationale, 1, [])
        task_id = uuid4().hex
        task = DiagnosisTask(
            id=task_id,
            project=project,
            baseline=baseline,
            proposal=proposal,
            result_directory=str(
                self.store.task_directory(conversation.id, task_id, output_root, create=True)
            ),
            output_root=output_root,
            plan_revisions=[plan],
            nodes=self._nodes(plan),
            assessments=[
                HypothesisAssessment(
                    hypothesis_id=item.id, status="unknown", reason="尚未执行已确认范围内的分析。"
                )
                for item in baseline.hypotheses
            ],
        )
        if conversation.task is not None:
            conversation.archived_tasks.append(conversation.task)
        conversation.task = task
        conversation.project_root = project.root
        conversation.output_root = output_root
        conversation.turn_origin = None
        conversation.status = "waiting_for_user"
        conversation.error = None
        conversation.pending_decision = PendingDecision(
            kind="confirm_baseline",
            message=f"请核对 D2 基线 v{version} 的假设、数据快照范围、只读能力与预算后确认。"
            "后续分析仅在这个范围内按证据调整，不修改控制器或模型。"
            f" 成果目录：{task.result_directory}",
        )
        self._message(conversation, "assistant", conversation.pending_decision.message)
        self.store.save(conversation)
        return conversation

    def confirm(self, conversation: Conversation, task: DiagnosisTask) -> Conversation:
        try:
            current = inspect_diagnostic_project(task.project.root)
            if current != task.project or current.fingerprint != task.baseline.project_fingerprint:
                raise ValueError("Diagnostic project changed after the baseline was proposed")
            for plan in task.plan_revisions:
                self._verify_plan(task, plan)
        except (OSError, ValueError) as exc:
            conversation.status = "waiting_for_user"
            conversation.error = str(exc)[:4000]
            conversation.pending_decision = PendingDecision(
                kind="clarify", message="确认前数据或能力已变化，请重新规划：" + str(exc)[:2000]
            )
            self._message(conversation, "assistant", conversation.pending_decision.message)
            self.store.save(conversation)
            return conversation
        task.baseline = DiagnosisBaseline.model_validate(
            {**task.baseline.model_dump(), "confirmed": True, "confirmed_at": now()}
        )
        task.decisions.append(
            Decision(
                action="confirm_baseline", baseline_version=task.baseline.version,
                created_at=now(), trusted=True,
            )
        )
        self._message(
            conversation, "user", f"确认 D2 基线 v{task.baseline.version} 的只读范围与预算。"
        )
        self.store.save(conversation)
        return self.run(conversation, task)

    def _scope(self, task: DiagnosisTask) -> ExecutionScope:
        return ExecutionScope(
            baseline_version=task.baseline.version,
            confirmed=task.baseline.confirmed,
            design=task.baseline.design,
            allowed_effects=("read_only", "write_delivery"),
        )

    def _spec(
        self, baseline: DiagnosisBaseline, project: DiagnosticProject, analysis: DiagnosticAnalysis
    ) -> AnalysisSpec:
        if analysis.capability not in baseline.allowed_capabilities:
            raise ValueError("Analysis capability is outside the confirmed diagnosis scope")
        if not set(analysis.hypothesis_ids).issubset(item.id for item in baseline.hypotheses):
            raise ValueError("Analysis refers to an unconfirmed hypothesis")
        definition = self.tools.definition(analysis.capability)
        if (
            definition.effect != "read_only"
            or baseline.capability_versions.get(analysis.capability) != definition.version
        ):
            raise ValueError("Diagnostic capability changed after baseline proposal")
        files = {item.path: item.sha256 for item in project.files}
        return AnalysisSpec(
            capability=analysis.capability,
            capability_version=definition.version,
            design=baseline.design,
            inputs=self.tools.validate_inputs(analysis.capability, analysis.inputs),
            data_fingerprints={
                item.id: files[item.path] for item in project.manifest.datasets
            },
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
    ) -> DiagnosisPlanRevision:
        specs = [self._spec(baseline, project, analysis) for analysis in analyses]
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

    def _verify_plan(self, task: DiagnosisTask, plan: DiagnosisPlanRevision) -> None:
        if plan.baseline_version != task.baseline.version:
            raise ValueError("Diagnostic plan is bound to a different baseline")
        expected = self._plan(
            task.baseline, task.project, plan.analyses, plan.reason, plan.version, plan.evidence
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
            [describe_artifact(Path(task.result_directory), f"inputs/{item.path}")
             for item in current.files],
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
        if not any(
            spec == recorded for plan in task.plan_revisions for recorded in plan.analysis_specs
        ):
            raise ValueError("Diagnostic evidence is outside the persisted baseline plans")
        run = next((item for item in task.analysis_runs if item.id == run_id), None)
        if run is None or load_run(Path(task.result_directory), run_id, spec) != run:
            raise ValueError("Recorded diagnostic evidence receipt is missing or inconsistent")
        self._output(run)
        return run

    def _accept(
        self, conversation: Conversation, task: DiagnosisTask, node: ExecutionNode,
        call: ToolCall, run: AnalysisRun, source: str,
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
        self.store.save(conversation)
        return run

    def _execute(
        self,
        conversation: Conversation,
        task: DiagnosisTask,
        plan: DiagnosisPlanRevision,
        index: int,
        node: ExecutionNode,
    ) -> AnalysisRun | None:
        spec = plan.analysis_specs[index]
        directory = Path(task.result_directory)
        if node.status == "completed":
            call = next(
                (item for item in reversed(task.calls)
                 if item.node_id == node.id and item.status == "completed"),
                None,
            )
            run_id = call.output.get("analysis_run_id") if call else None
            if not isinstance(run_id, str):
                raise ValueError("Completed analysis node lacks its evidence reference")
            return self._verified_run(task, run_id, spec)
        pending = task.pending_analysis
        if pending is not None:
            if (
                pending.plan_version != plan.version or pending.analysis_index != index
                or pending.spec != spec
            ):
                raise ValueError("Pending analysis does not match its saved plan")
            call = next((item for item in task.calls if item.id == pending.id), None)
            if call is None or call.node_id != node.id or call.tool != spec.capability:
                raise ValueError("Pending analysis lacks its original durable tool call")
            saved = load_run(directory, pending.id, pending.spec)
            if saved is None:
                self._stop(
                    task, "unknown_execution",
                    "已记录分析调用，但没有完整回执；不会自动重复执行，请核对后另行确认新任务。",
                )
                node.status = "interrupted"
                call.status = "interrupted"
                call.error = task.termination.detail if task.termination else None
                call.finished_at = now()
                return None
            return self._accept(conversation, task, node, call, saved, "recovered")
        cached = next(
            (run for run in task.analysis_runs if run.spec.fingerprint == spec.fingerprint), None
        )
        if cached is None and self._budget(task, for_dispatch=True):
            return None
        call = ToolCall(
            id=uuid4().hex, node_id=node.id, tool=spec.capability,
            baseline_version=task.baseline.version, started_at=now(),
            inputs={"spec": spec.model_dump(mode="json"), "plan_version": plan.version},
        )
        task.calls.append(call)
        node.status = "running"
        task.stage = spec.capability
        if cached is not None:
            verified = self._verified_run(task, cached.id, spec)
            return self._accept(conversation, task, node, call, verified, "cache")
        task.pending_analysis = PendingAnalysis(
            id=call.id, spec=spec, plan_version=plan.version, analysis_index=index,
            started_at=call.started_at,
        )
        task.dispatch_count += 1
        self.store.save(conversation)
        if task.input_snapshot is None:
            raise ValueError("Analysis requires a verified input snapshot")
        output = self.tools.invoke(
            spec.capability, spec.inputs,
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
        persist_run(directory, run)
        return self._accept(conversation, task, node, call, run, "execution")

    def _review(self, task: DiagnosisTask, review: DiagnosisReview) -> None:
        validate_assessments(task.baseline.hypotheses, review.assessments, task.analysis_runs)

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
            reason=reason, detail=detail, created_at=now(),
            unknowns=list(dict.fromkeys(unknowns))[:40],
        )

    def _budget(self, task: DiagnosisTask, *, for_dispatch: bool = False) -> bool:
        budget = task.baseline.budget
        elapsed = (
            (datetime.now(UTC) - datetime.fromisoformat(task.started_at)).total_seconds()
            if task.started_at else 0
        )
        if elapsed >= budget.max_seconds:
            self._stop(task, "time_budget", "已达到已确认的墙钟时间预算，停止新增分析。")
        elif for_dispatch and task.dispatch_count >= budget.max_calls:
            self._stop(task, "call_budget", "已达到已确认的分析调用次数预算。")
        else:
            return False
        return True

    def run(self, conversation: Conversation, task: DiagnosisTask) -> Conversation:
        self._scope(task).authorize("read_only")
        validate_output_root(task.result_directory, task.project.root)
        conversation.turn_origin = None
        conversation.status = "running"
        conversation.pending_decision = None
        conversation.error = None
        task.started_at = task.started_at or now()
        self.store.save(conversation)
        try:
            self._inputs(task)
            if [plan.version for plan in task.plan_revisions] != list(
                range(1, len(task.plan_revisions) + 1)
            ):
                raise ValueError("Diagnostic plan revisions must be contiguous and append-only")
            for plan in task.plan_revisions:
                self._verify_plan(task, plan)
            expected = [
                node.model_dump(include={"id", "tool", "inputs", "depends_on"})
                for plan in task.plan_revisions for node in self._nodes(plan)
            ]
            actual = [
                node.model_dump(include={"id", "tool", "inputs", "depends_on"})
                for node in task.nodes
            ]
            if actual != expected:
                raise ValueError("Persisted analysis nodes differ from their plan revisions")
            for recorded_run in task.analysis_runs:
                self._verified_run(task, recorded_run.id, recorded_run.spec)
            self.store.save(conversation)
            while task.termination is None:
                plan = task.plan_revisions[-1]
                run_ids = []
                for index in range(len(plan.analyses)):
                    node = next(
                        item for item in task.nodes
                        if item.id == f"round-{plan.version}-{index + 1}"
                    )
                    run = self._execute(conversation, task, plan, index, node)
                    if run is None:
                        break
                    run_ids.append(run.id)
                if task.termination is not None or self._budget(task):
                    break
                task.stage = "review_evidence"
                self.store.save(conversation)
                review = self.planner.review_diagnosis(
                    task,
                    tools=[
                        item for item in self.tools.catalog()
                        if item["name"] in task.baseline.allowed_capabilities
                    ],
                )
                self._review(task, review)
                next_plan = (
                    self._plan(
                        task.baseline, task.project, review.analyses, review.reason,
                        plan.version + 1, run_ids,
                    ) if review.action == "continue" else None
                )
                progressed = any(
                    call.node_id.startswith(f"round-{plan.version}-")
                    and call.output.get("source") != "cache"
                    and call.output.get("analysis_run_id") in {
                        run.id for run in task.analysis_runs if self._output(run).valid
                    }
                    for call in task.calls
                )
                task.no_progress_rounds = 0 if progressed else task.no_progress_rounds + 1
                task.assessments = review.assessments
                task.rounds.append(
                    DiagnosticRound(
                        plan_version=plan.version, run_ids=run_ids, review=review, created_at=now()
                    )
                )
                if review.action == "stop":
                    if review.stop_reason is None:
                        raise ValueError("Diagnostic stop reason is missing")
                    self._stop(task, review.stop_reason, review.reason)
                elif task.no_progress_rounds >= task.baseline.budget.max_no_progress:
                    self._stop(task, "no_progress", "连续多轮没有新增有效证据，停止重复分析。")
                elif len(task.rounds) >= task.baseline.budget.max_rounds:
                    self._stop(task, "round_budget", "已达到已确认的诊断轮次预算。")
                elif not self._budget(task) and next_plan is not None:
                    task.plan_revisions.append(next_plan)
                    task.nodes.extend(self._nodes(next_plan))
                self.store.save(conversation)
            self._finish(conversation, task)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Diagnosis execution failed for %s", task.id)
            conversation.status = "failed"
            conversation.error = str(exc)[:4000]
            conversation.pending_decision = PendingDecision(
                kind="resume",
                message="诊断已停止，错误已保存。续接将核验原回执，不会重复派发状态未知的分析。",
            )
            for node in task.nodes:
                if node.status == "running":
                    node.status = "failed"
                    node.error = conversation.error
            for call in task.calls:
                if call.status == "running":
                    call.status = "failed"
                    call.error = conversation.error
                    call.finished_at = now()
            self._message(conversation, "assistant", conversation.pending_decision.message)
            self.store.save(conversation)
        return conversation

    def _finish(self, conversation: Conversation, task: DiagnosisTask) -> None:
        if task.termination is None:
            raise ValueError("Diagnosis cannot deliver without an explicit stop reason")
        self._scope(task).authorize("write_delivery")
        task.stage = "deliver_report"
        completion = ConversationMessage(
            role="assistant",
            content=f"D2 证据报告已交付；停止原因：{task.termination.reason}。"
            "支持、反驳与未知项已分别记录，不代表唯一根因、全包线安全或工程验收。",
            created_at=now(),
        )
        artifacts = deliver_diagnosis_report(task, [*conversation.messages, completion])
        self._merge_artifacts(task, artifacts)
        task.stage = "completed"
        conversation.status = "completed"
        conversation.error = None
        conversation.pending_decision = None
        conversation.messages.append(completion)
        self.store.save(conversation)

    def _merge_artifacts(self, task: DiagnosisTask, artifacts: list[Artifact]) -> None:
        merged = {item.path: item for item in task.artifacts}
        merged.update({item.path: item for item in artifacts})
        task.artifacts = list(merged.values())

    def _message(self, conversation: Conversation, role: str, text: str) -> None:
        conversation.messages.append(
            ConversationMessage.model_validate(
                {"role": role, "content": text, "created_at": now()}
            )
        )
