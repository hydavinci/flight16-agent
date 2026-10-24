import logging
from uuid import uuid4

from openai import APIError

from ..runtime.capabilities import CapabilityRegistry
from ..runtime.evidence import persist_run
from .contracts import (
    Conversation,
    ConversationMessage,
    Decision,
    DiagnosisTask,
    PendingDecision,
)
from .diagnosis_contracts import DiagnosisBaseline, HypothesisAssessment
from .diagnosis_reports import deliver_diagnosis_report
from .diagnostic_data import (
    DiagnosticContext,
    diagnostic_capabilities,
    inspect_diagnostic_project,
)
from .diagnostic_engine import DiagnosticEngine
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
        self.tools = tools or diagnostic_capabilities()
        self.planner = planner
        self.engine = DiagnosticEngine(
            self.tools, planner,
            persist_receipt=lambda directory, run: persist_run(directory, run),
        )
        self.store = store

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
        plan = self.engine._plan(baseline, project, proposal.analyses, proposal.rationale, 1, [])
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
            nodes=self.engine._nodes(plan),
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
                self.engine._verify_plan(task, plan)
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

    def run(self, conversation: Conversation, task: DiagnosisTask) -> Conversation:
        self.engine._scope(task).authorize("read_only")
        validate_output_root(task.result_directory, task.project.root)
        conversation.turn_origin = None
        conversation.status = "running"
        conversation.pending_decision = None
        conversation.error = None
        task.started_at = task.started_at or now()
        self.store.save(conversation)
        try:
            self.engine.run(task, checkpoint=lambda task: self.store.save(conversation))
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
        self.engine._scope(task).authorize("write_delivery")
        task.stage = "deliver_report"
        completion = ConversationMessage(
            role="assistant",
            content=f"D2 证据报告已交付；停止原因：{task.termination.reason}。"
            "支持、反驳与未知项已分别记录，不代表唯一根因、全包线安全或工程验收。",
            created_at=now(),
        )
        artifacts = deliver_diagnosis_report(task, [*conversation.messages, completion])
        self.engine._merge_artifacts(task, artifacts)
        task.stage = "completed"
        conversation.status = "completed"
        conversation.error = None
        conversation.pending_decision = None
        conversation.messages.append(completion)
        self.store.save(conversation)

    def _message(self, conversation: Conversation, role: str, text: str) -> None:
        conversation.messages.append(
            ConversationMessage.model_validate(
                {"role": role, "content": text, "created_at": now()}
            )
        )
