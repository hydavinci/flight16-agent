import logging
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from openai import APIError
from pydantic import JsonValue

from ..jobs import BusyError
from ..runtime.capabilities import Capability, CapabilityDefinition, Input, Output
from ..runtime.contracts import AnalysisRun, EvidenceFile, fingerprint
from ..runtime.evidence import load_run, persist_run, receipt_name
from ..runtime.intents import Scenario, WorkflowRequest
from ..runtime.loop import LoopDecision, run_loop
from ..runtime.workflows import DirectoryInputs, Workflow, WorkflowRegistry
from ..session import _write_json
from .artifacts import describe_artifact, verify_artifact
from .backend import AnalysisBackend, RemoteSimulinkBackend
from .contracts import (
    AgentTask,
    AnalysisKind,
    AnalysisResult,
    AnalysisRunRequest,
    Artifact,
    Conversation,
    ConversationMessage,
    ConversationState,
    Decision,
    DiagnosisTask,
    Evaluation,
    EvaluationBaseline,
    ExecutionNode,
    GeneralTask,
    LegacyTask,
    PendingDecision,
    PlanNode,
    PlanProposal,
    PlanVersion,
    ProjectDescriptor,
    RunRecord,
    Task,
    ToolCall,
    VerificationTask,
)
from .diagnosis import DiagnosisRuntime
from .evaluation import evaluate
from .general import GeneralContext, GeneralRuntime
from .intake import parse_directories, safe_directory, validate_output_root
from .planner import VerificationPlanner
from .reports import ReportRequest, deliver_report, finalize_execution_record
from .store import ConversationStore, now
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


class VerificationRuntime:
    def __init__(
        self,
        root: Path,
        *,
        backend: AnalysisBackend | None = None,
        planner: VerificationPlanner | None = None,
        store: ConversationStore | None = None,
    ):
        self.store = store if store is not None else ConversationStore(root)
        self.backend = backend
        self.planner = planner or VerificationPlanner()
        self.lock = threading.Lock()
        self.tools = ToolRegistry(self._analyze, self._evaluate, self._deliver)
        self.diagnosis = DiagnosisRuntime(self.store, self.planner)
        self.general = GeneralRuntime(self.store)
        self.workflows: WorkflowRegistry[Conversation] = WorkflowRegistry()
        self.workflows.register(Workflow(
            CapabilityDefinition(
                name="d1", version="1", description="Registered Simulink verification.",
                effect="execute_project",
            ), "worker_project", DirectoryInputs, self._propose_verification,
        ))
        self.workflows.register(Workflow(
            CapabilityDefinition(
                name="d2", version="1", description="Registered offline diagnostic data analysis.",
                effect="read_only",
            ), "diagnostic_data", DirectoryInputs,
            lambda conversation, request: self.diagnosis.propose(conversation, request.request),
        ))

    def register_capability(self, capability: Capability[GeneralContext, Input, Output]) -> None:
        if any(item["name"] == capability.name for item in self.workflows.catalog()):
            raise ValueError(f"Workflow is already registered: {capability.name}")
        self.general.tools.register(capability)
        self.workflows.register(Workflow(
            self.general.tools.definition(capability.name), "none", capability.input_model,
            self._propose_general,
        ))

    def _propose_general(
        self, conversation: Conversation, request: WorkflowRequest
    ) -> Conversation:
        proposal = self.planner.propose_general(
            request, conversation, tools=self.general.tools.catalog()
        )
        return self.general.propose(conversation, proposal, workflow=request.workflow)

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self.lock.acquire(blocking=False):
            raise BusyError("Another verification operation is running")
        try:
            yield
        finally:
            self.lock.release()

    def _backend(self) -> AnalysisBackend:
        return self.backend or RemoteSimulinkBackend.from_environment()

    def _message(self, conversation: Conversation, role: str, content: str) -> None:
        conversation.messages.append(
            ConversationMessage.model_validate(
                {"role": role, "content": content, "created_at": now()}
            )
        )

    def _wait(
        self, conversation: Conversation, message: str, *, error: str | None = None
    ) -> Conversation:
        restored = conversation.restore_turn_origin()
        switching = (
            conversation.task is not None and conversation.active_workflow is not None
            and conversation.active_workflow != self._task_workflow(conversation.task)
        )
        if not restored:
            conversation.status = "waiting_for_user"
            conversation.error = error
        elif conversation.error is None:
            conversation.error = error
        kind = (
            conversation.pending_decision.kind
            if restored and conversation.pending_decision and not switching
            else "clarify"
        )
        if switching:
            conversation.status = "waiting_for_user"
        conversation.pending_decision = PendingDecision(kind=kind, message=message)
        self._message(conversation, "assistant", message)
        self.store.save(conversation)
        return conversation

    def submit(
        self,
        conversation_id: str | None,
        message: str,
        project_root: str | None = None,
        *,
        output_root: str | None = None,
        scenario: Scenario | None = None,
    ) -> Conversation:
        with self._operation():
            conversation = self._queue_message(
                conversation_id, message, project_root, output_root, scenario
            )
            if conversation.status == "planning":
                return self._propose(conversation, message)
            return conversation

    def queue_message(
        self,
        conversation_id: str | None,
        message: str,
        project_root: str | None = None,
        *,
        output_root: str | None = None,
        scenario: Scenario | None = None,
    ) -> Conversation:
        with self._operation():
            return self._queue_message(
                conversation_id, message, project_root, output_root, scenario
            )

    def _queue_message(
        self,
        conversation_id: str | None,
        message: str,
        project_root: str | None,
        output_root: str | None,
        scenario: Scenario | None,
    ) -> Conversation:
        if not message.strip() or len(message) > 4000:
            raise ValueError("A message between 1 and 4000 characters is required")
        conversation = self.store.get(conversation_id) if conversation_id else self.store.create()
        self._message(conversation, "user", message.strip())
        if self._unknown_execution(conversation.task):
            self._message(
                conversation,
                "assistant",
                "已有 Worker 或能力运行待核对；请先续接该运行，不能切换任务或重复派发。",
            )
            self.store.save(conversation)
            return conversation
        selected = scenario or conversation.scenario
        changed = selected != conversation.scenario
        if changed or conversation_id is None:
            conversation.scenario = selected
            conversation.workflow_agenda = []
        if selected != "auto":
            conversation.active_workflow = selected
        if conversation.task is not None:
            conversation.turn_origin = ConversationState(
                status=conversation.status,
                pending_decision=conversation.pending_decision,
                error=conversation.error,
            )
        intake = parse_directories(
            message,
            project_root,
            output_root,
            current_project=None if changed or selected == "auto" else conversation.project_root,
            current_output=None if changed or selected == "auto" else conversation.output_root,
        )
        if intake.question is not None:
            return self._wait(conversation, intake.question)
        conversation.project_root = intake.project_root
        conversation.output_root = intake.output_root
        conversation.output_root_reset = output_root is not None and not output_root.strip()
        if selected == "auto" and not conversation.workflow_agenda:
            conversation.goal = message.strip()
        conversation.status = "planning"
        conversation.error = None
        conversation.pending_decision = None
        self.store.save(conversation)
        return conversation

    def continue_submission(self, conversation_id: str) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            if (
                conversation.status != "planning"
                or not conversation.messages
                or conversation.messages[-1].role != "user"
            ):
                raise ValueError("No queued user message is available for planning")
            return self._propose(conversation, conversation.messages[-1].content)

    def _resolve_output_root(
        self, conversation: Conversation, project: ProjectDescriptor
    ) -> str | None:
        effective = conversation.output_root or str(self.store.default_task_root(conversation.id))
        resolved = validate_output_root(effective, project.root)
        return resolved if conversation.output_root is not None else None

    def _propose(self, conversation: Conversation, message: str) -> Conversation:
        try:
            if conversation.scenario == "auto":
                if not conversation.workflow_agenda:
                    decision = self._intent(conversation, message)
                    if decision is not None:
                        return decision
                request = conversation.workflow_agenda[0]
            else:
                request = WorkflowRequest(workflow=conversation.scenario, request=message)
            return self._propose_workflow(conversation, request)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Verification planning failed for %s", conversation.id)
            return self._wait(
                conversation,
                "无法形成可执行计划，请补充或修正配置：" + str(exc)[:4000],
                error=str(exc)[:4000],
            )

    def _intent(
        self, conversation: Conversation, message: str, *, phase: str = "request"
    ) -> Conversation | None:
        proposal = self.planner.plan_intent(
            message, conversation, workflows=self.workflows.catalog(), phase=phase
        )
        if proposal.action == "clarify":
            return self._wait(conversation, proposal.text)
        if proposal.action == "answer":
            if not conversation.restore_turn_origin():
                conversation.status = "completed"
                conversation.error = None
                conversation.pending_decision = None
            conversation.workflow_agenda = []
            self._message(conversation, "assistant", proposal.text)
            self.store.save(conversation)
            return conversation
        requests = [self.workflows.validate(item) for item in proposal.steps]
        conversation.workflow_agenda = requests
        self._message(conversation, "assistant", proposal.text)
        self.store.save(conversation)
        return None

    @staticmethod
    def _task_workflow(task: Task) -> str:
        if isinstance(task, AgentTask):
            raise ValueError("Use the generic v2 coordinator for this task")
        return task.workflow if isinstance(task, GeneralTask) else task.scenario

    def _propose_workflow(
        self, conversation: Conversation, request: WorkflowRequest
    ) -> Conversation:
        request = self.workflows.validate(request)
        workflow = self.workflows.get(request.workflow)
        previous = conversation.task
        previous_version = previous.baseline.version if previous is not None else 0
        same = previous is not None and self._task_workflow(previous) == request.workflow
        conversation.active_workflow = request.workflow
        if (
            conversation.project_root is None and same
            and isinstance(previous, (VerificationTask, DiagnosisTask))
        ):
            conversation.project_root = previous.project.root
        if (
            conversation.output_root is None and not conversation.output_root_reset
            and same and previous is not None
        ):
            conversation.output_root = previous.output_root
        if workflow.directory != "none" and not conversation.project_root:
            return self._wait(
                conversation,
                "请提供 Agent 可见的已登记诊断数据目录；不能复用验证工程或报告目录。"
                if workflow.directory == "diagnostic_data"
                else "请提供 MATLAB Worker 可见的已登记工程目录路径。",
            )
        planned = workflow.propose(conversation, request)
        if planned.task is not previous:
            conversation.workflow_agenda = conversation.workflow_agenda[1:]
        elif planned.task is not None and planned.task.baseline.version != previous_version:
            conversation.workflow_agenda = conversation.workflow_agenda[1:]
        self.store.save(planned)
        return planned

    def _propose_verification(
        self, conversation: Conversation, request: WorkflowRequest
    ) -> Conversation:
        if conversation.project_root is None:
            raise ValueError("A project directory is required")
        project = self._backend().inspect(conversation.project_root)
        conversation.output_root = self._resolve_output_root(conversation, project)
        if project.config.backend != "simulink" or 1 not in project.config.supported_trim_points:
            raise ValueError("D1 only supports a registered Simulink project at trim point 1")
        proposal = self.planner.propose(
            request.request, conversation, project, tools=self.tools.catalog()
        )
        if proposal.action == "clarify":
            return self._wait(conversation, proposal.question)
        if proposal.action == "explain":
            if (
                conversation.task is not None
                and not isinstance(conversation.task, VerificationTask)
            ):
                raise ValueError("D1 cannot reuse a task from a different workflow")
            if conversation.task and not self._same_project(conversation.task.project, project):
                return self._wait(
                    conversation, "工程已经变化，请先确认新基线；不能沿用旧结论。"
                )
            if conversation.task and conversation.output_root != conversation.task.output_root:
                return self._wait(conversation, "输出目录已变化，请先生成并确认新的交付计划。")
            return self._answer(conversation, request.request)
        self._plan(conversation, project, proposal)
        return conversation

    def _advance(self, conversation: Conversation) -> Conversation:
        if (
            conversation.status != "completed" or conversation.scenario != "auto"
            or not conversation.workflow_agenda
        ):
            return conversation
        conversation.project_root = None
        conversation.output_root = None
        conversation.output_root_reset = False
        try:
            decision = self._intent(conversation, conversation.goal, phase="observation")
            if decision is not None:
                return decision
            return self._propose(conversation, conversation.workflow_agenda[0].request)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Workflow progression failed for %s", conversation.id)
            return self._wait(
                conversation, "后续阶段需要补充输入或修正计划：" + str(exc)[:4000],
                error=str(exc)[:4000],
            )

    def _unknown_execution(self, task: Task | None) -> bool:
        if isinstance(task, VerificationTask):
            return task.pending_request is not None and not self._known_failed_run(task)
        if isinstance(task, DiagnosisTask):
            return task.pending_analysis is not None
        return task is not None and any(
            call.status in ("running", "interrupted") for call in task.calls
        )

    def _validate_plan(
        self, project: ProjectDescriptor, proposal: PlanProposal
    ) -> None:
        if proposal.action != "plan":
            raise ValueError("An executable plan is required")
        if project.config.backend != "simulink" or 1 not in project.config.supported_trim_points:
            raise ValueError("D1 only supports a registered Simulink project at trim point 1")
        if not set(proposal.analyses).issubset(project.capabilities):
            raise ValueError("The project does not provide the proposed analysis capability")
        self.tools.validate_plan(proposal.nodes)

    def _plan(
        self, conversation: Conversation, project: ProjectDescriptor, proposal: PlanProposal
    ) -> None:
        self._validate_plan(project, proposal)
        proposal = proposal.model_copy(
            update={"tool_versions": self.tools.versions(proposal.nodes)}
        )
        original = conversation.task
        task = original if isinstance(original, VerificationTask) else None
        prior_tasks = [*conversation.archived_tasks, *([original] if original else [])]
        version = max((item.baseline.version for item in prior_tasks), default=0) + 1
        baseline = EvaluationBaseline(
            version=version,
            project_fingerprint=project.fingerprint,
            trim_point=proposal.trim_point,
            analyses=proposal.analyses,
            criteria=proposal.criteria,
            steady_state=project.config.steady_state,
        )
        nodes = [ExecutionNode(**node.model_dump()) for node in proposal.nodes]
        output_root = self._resolve_output_root(conversation, project)
        output_changed = task is not None and task.output_root != output_root
        if (
            task is None
            or not self._same_project(task.project, project)
            or self._known_failed_run(task)
            or output_changed
        ):
            previous = task
            reusable = (
                previous is not None
                and output_changed
                and self._same_project(previous.project, project)
                and previous.pending_request is None
                and previous.result is not None
                and previous.result.project_fingerprint == baseline.project_fingerprint
                and previous.result.trim_point == baseline.trim_point
                and previous.baseline.steady_state == baseline.steady_state
            )
            if reusable and previous is not None:
                self._task(conversation)
                self._verify_numerical(previous)
            task_id = uuid4().hex
            task = VerificationTask(
                id=task_id,
                project=project,
                baseline=baseline,
                proposal=proposal,
                nodes=nodes,
                result_directory=str(
                    self.store.task_directory(conversation.id, task_id, output_root, create=True)
                ),
                output_root=output_root,
            )
            if reusable and previous is not None:
                self._copy_numerical(previous, task)
            if original is not None:
                conversation.archived_tasks.append(original)
            conversation.task = task
        else:
            task.baseline_history.append(task.baseline)
            task.plan_history.append(
                PlanVersion(
                    baseline_version=task.baseline.version,
                    proposal=task.proposal,
                    nodes=[node.model_copy(deep=True) for node in task.nodes],
                )
            )
            task.baseline = baseline
            task.proposal = proposal
            task.nodes = nodes
            task.evaluation = None
            task.explanation = None
            task.explanation_error = None
            task.replan_attempts = 0
            task.failure_proposal = None
            task.replan_error = None
        task.stage = "awaiting_confirmation"
        conversation.project_root = project.root
        conversation.output_root = output_root
        conversation.turn_origin = None
        conversation.status = "waiting_for_user"
        conversation.error = None
        conversation.pending_decision = PendingDecision(
            kind="confirm_baseline",
            message=f"请核对基线 v{version}、判据和报告分组，并确认工程代码可信后执行。"
            f" 成果将写入 Agent 所在机器的目录：{task.result_directory}",
        )
        self._message(conversation, "assistant", conversation.pending_decision.message)

    def _task(self, conversation: Conversation) -> LegacyTask:
        if conversation.task is None:
            raise ValueError("No verification task has been planned")
        if isinstance(conversation.task, AgentTask):
            raise ValueError("Use the generic v2 coordinator for this task")
        directory = self.store.task_directory(
            conversation.id, conversation.task.id, conversation.task.output_root
        )
        if str(directory) != conversation.task.result_directory:
            raise ValueError("Task result directory does not match its persisted identity")
        return conversation.task

    def _verify_numerical(self, task: VerificationTask) -> None:
        if task.result is None:
            raise ValueError("No recorded numerical evidence is available to copy")
        for artifact in task.result.artifacts:
            if (
                artifact not in task.artifacts
                or not artifact.path.startswith("numerical/")
                or artifact.role == "report"
            ):
                raise ValueError("Only registered numerical evidence may be copied")
            verify_artifact(Path(task.result_directory), artifact)
        for run in task.analysis_runs:
            if load_run(Path(task.result_directory), run.id, run.spec) != run:
                raise ValueError("Recorded analysis receipt is missing or inconsistent")

    def _copy_numerical(self, previous: VerificationTask, task: VerificationTask) -> None:
        if previous.result is None:
            raise ValueError("No recorded numerical evidence is available to copy")
        source = Path(previous.result_directory)
        target = Path(task.result_directory)
        for artifact in previous.result.artifacts:
            original = verify_artifact(source, artifact)
            destination = target / artifact.path
            safe_directory(destination.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with original.open("rb") as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            verify_artifact(source, artifact)
            verify_artifact(target, artifact)
        task.result = previous.result.model_copy(deep=True)
        task.artifacts = list(task.result.artifacts)
        task.analysis_runs = [run.model_copy(deep=True) for run in previous.analysis_runs]

    def _check_confirmation(self, task: Task, version: int, trusted: bool) -> None:
        if version != task.baseline.version:
            raise ValueError("Confirmation baseline version is stale")
        if trusted is not True:
            raise ValueError("Explicit trusted-project consent is required")

    def _check_active_task(self, conversation: Conversation, task: Task) -> None:
        if (
            conversation.active_workflow is not None
            and conversation.active_workflow != self._task_workflow(task)
        ):
            raise ValueError("The selected workflow stage requires a new plan and confirmation")

    def _same_project(self, first: ProjectDescriptor, second: ProjectDescriptor) -> bool:
        return (
            first.root == second.root
            and first.fingerprint == second.fingerprint
            and first.config == second.config
            and first.capabilities == second.capabilities
        )

    def _known_failed_run(self, task: Task) -> bool:
        return (
            isinstance(task, VerificationTask)
            and task.pending_request is not None
            and task.worker_run is not None
            and task.worker_run.id == task.pending_request.id
            and task.worker_run.status in ("failed", "interrupted")
        )

    def confirm(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            self._check_active_task(conversation, task)
            self._check_confirmation(task, version, trusted)
            if conversation.status == "completed" and task.baseline.confirmed:
                return conversation
            if task.baseline.confirmed:
                raise ValueError("This baseline is already confirmed; use resume for saved work")
            if isinstance(task, DiagnosisTask):
                return self._advance(self.diagnosis.confirm(conversation, task))
            if isinstance(task, GeneralTask):
                return self._advance(self.general.confirm(conversation, task))
            try:
                self.tools.validate_versions(task)
                project = self._backend().inspect(task.project.root)
                if not self._same_project(task.project, project):
                    return self._wait(
                        conversation,
                        "工程在规划后发生变化，请重新提交请求并确认新基线。",
                        error="Project changed after the baseline was proposed",
                    )
            except (OSError, ValueError, RuntimeError) as exc:
                return self._wait(conversation, "确认前无法核对工程：" + str(exc), error=str(exc))
            task.baseline = EvaluationBaseline.model_validate(
                {**task.baseline.model_dump(), "confirmed": True, "confirmed_at": now()}
            )
            task.decisions.append(
                Decision(
                    action="confirm_baseline",
                    baseline_version=version,
                    created_at=now(),
                    trusted=True,
                )
            )
            self._message(conversation, "user", f"确认基线 v{version}，并同意执行可信工程。")
            self.store.save(conversation)
            return self._advance(self._run(conversation, resume=False))

    def resume(self, conversation_id: str) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            self._check_active_task(conversation, task)
            if not task.baseline.confirmed:
                raise ValueError("Confirm the current baseline before continuing")
            if conversation.status == "completed":
                return conversation
            task.decisions.append(
                Decision(action="resume", baseline_version=task.baseline.version, created_at=now())
            )
            self._message(
                conversation, "user",
                "续接已有 D2 任务，核验已记录的证据回执，不重复派发未知调用。"
                if isinstance(task, DiagnosisTask)
                else "续接已有任务；核对同一 Worker 运行，不重复提交。",
            )
            self.store.save(conversation)
            return self._advance(self._run(conversation, resume=True))

    def retry(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            if not isinstance(task, VerificationTask):
                raise ValueError("Worker retry is only available for D1 tasks")
            self._check_confirmation(task, version, trusted)
            if not task.baseline.confirmed or not self._known_failed_run(task):
                raise ValueError("Retry requires a known failed or interrupted worker run")
            project = self._backend().inspect(task.project.root)
            if not self._same_project(task.project, project):
                raise ValueError("Project changed; propose and confirm a new baseline before retry")
            task.decisions.append(
                Decision(action="retry", baseline_version=version, created_at=now(), trusted=True)
            )
            task.pending_request = None
            task.worker_run = None
            self._message(conversation, "user", "确认重试已明确失败的 Worker 运行。")
            self.store.save(conversation)
            return self._run(conversation, resume=False)

    def replan(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
        with self._operation():
            conversation = self.store.get(conversation_id)
            task = self._task(conversation)
            if not isinstance(task, VerificationTask):
                raise ValueError("Worker recovery plans are only available for D1 tasks")
            self._check_confirmation(task, version, trusted)
            if task.pending_request is not None and not self._known_failed_run(task):
                raise ValueError("Reconcile the unknown Worker run before adopting another plan")
            candidate = task.failure_proposal
            if (
                not task.baseline.confirmed
                or candidate is None
                or candidate.action != "plan"
                or conversation.pending_decision is None
                or conversation.pending_decision.kind != "confirm_replan"
            ):
                raise ValueError("No recovery plan is awaiting explicit adoption")
            project = self._backend().inspect(task.project.root)
            self._message(conversation, "user", f"采用基线 v{version} 的恢复建议，重新核对基线。")
            self._plan(conversation, project, candidate)
            task.decisions.append(
                Decision(
                    action="confirm_replan",
                    baseline_version=version,
                    created_at=now(),
                    trusted=True,
                )
            )
            self.store.save(conversation)
            return conversation

    def _run(self, conversation: Conversation, *, resume: bool) -> Conversation:
        task = self._task(conversation)
        if isinstance(task, DiagnosisTask):
            return self.diagnosis.run(conversation, task)
        if isinstance(task, GeneralTask):
            return self.general.run(conversation, task)
        if not task.baseline.confirmed:
            raise ValueError("Execution requires a confirmed baseline")
        validate_output_root(task.result_directory, task.project.root)
        conversation.turn_origin = None
        conversation.status = "running"
        conversation.error = None
        conversation.pending_decision = None
        self.store.save(conversation)
        current: ToolCall | None = None
        node: ExecutionNode | None = None
        try:
            planned = [
                PlanNode.model_validate(
                    item.model_dump(include={"id", "tool", "depends_on", "inputs"})
                )
                for item in task.nodes
            ]
            if planned != task.proposal.nodes:
                raise ValueError("Execution nodes do not match the confirmed plan")
            self.tools.validate_plan(planned)
            self.tools.validate_versions(task)
            def decide() -> LoopDecision:
                nonlocal node
                if all(item.status == "completed" for item in task.nodes):
                    return LoopDecision(action="stop", reason="Every confirmed node is complete")
                completed = {item.id for item in task.nodes if item.status == "completed"}
                node = next(
                    (
                        item
                        for item in task.nodes
                        if item.status != "completed" and set(item.depends_on).issubset(completed)
                    ),
                    None,
                )
                if node is None:
                    raise ValueError("No executable node remains in the persisted plan")
                return LoopDecision(
                    action="continue", effect=self.tools.effect(node.tool),
                    reason=f"Execute the next ready node: {node.id}",
                )

            def execute() -> None:
                nonlocal current
                if node is None:
                    raise ValueError("No confirmed node was selected")
                node.status = "running"
                node.error = None
                task.stage = node.tool
                current = ToolCall(
                    id=uuid4().hex,
                    node_id=node.id,
                    tool=node.tool,
                    tool_version=task.proposal.tool_versions.get(node.tool),
                    baseline_version=task.baseline.version,
                    started_at=now(),
                    inputs={
                        **node.inputs,
                        "baseline_version": task.baseline.version,
                        "project_fingerprint": task.project.fingerprint,
                    },
                )
                if node.tool == "analyze_project" and task.pending_request is not None:
                    versions = {spec.capability_version for spec in task.pending_specs.values()}
                    current.tool_version = next(iter(versions)) if len(versions) == 1 else None
                task.calls.append(current)
                self.store.save(conversation)
                current.output = self.tools.invoke(
                    next(item for item in planned if item.id == node.id),
                    conversation,
                    task,
                    resume=resume,
                )
                current.status = "completed"
                current.finished_at = now()
                node.status = "completed"

            run_loop(
                decide=decide, execute=execute,
                persist=lambda: self.store.save(conversation),
                scope=self.tools.scope(task), max_steps=len(task.nodes),
            )
            status = task.evaluation.status.upper() if task.evaluation else "UNDETERMINED"
            completion = ConversationMessage(
                role="assistant",
                content=f"验证报告已交付，基线 v{task.baseline.version}，程序判定 {status}。"
                "结论仅适用于记录的工况与判据，不代表适航或全包线安全证明。"
                + (" LLM 解释未生成，原因已记录。" if task.explanation_error else ""),
                created_at=now(),
            )
            request = self._report_request(conversation, task)
            execution = self._execution_record(conversation, task)
            execution["status"] = "completed"
            execution["messages"] = [
                item.model_dump(mode="json") for item in [*conversation.messages, completion]
            ]
            request = ReportRequest.model_validate(
                {**request.model_dump(), "execution_record": execution}
            )
            prefix = f"delivery-v{task.baseline.version}/"
            current_artifacts = [
                item
                for item in task.artifacts
                if item.path.startswith(prefix)
                or item in request.result.artifacts or item in request.derived_artifacts
            ]
            final = finalize_execution_record(
                request, Path(task.result_directory), current_artifacts
            )
            self._merge_artifacts(task, final)
            conversation.status = "completed"
            task.stage = "completed"
            conversation.messages.append(completion)
            self.store.save(conversation)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Verification execution failed for %s", task.id)
            if node is not None:
                node.status = "failed"
                node.error = str(exc)[:4000]
            if current is not None:
                current.status = "failed"
                current.error = str(exc)[:4000]
                current.finished_at = now()
            self._fail(conversation, task, exc)
        return conversation

    def _analyze(
        self, conversation: Conversation, task: VerificationTask, resume: bool
    ) -> dict[str, JsonValue]:
        specs = {kind: self.tools.analysis_spec(task, kind) for kind in task.baseline.analyses}
        request = task.pending_request
        reconcile = request is not None
        if request is not None:
            if not resume:
                raise ValueError("A recorded worker run must be reconciled, never resubmitted")
            if (
                request.project_root != task.project.root
                or request.project_fingerprint != task.baseline.project_fingerprint
                or request.trim_point != task.baseline.trim_point
                or not set(request.analyses).issubset(specs)
            ):
                raise ValueError("Recorded worker request does not match the confirmed baseline")
            versions = {spec.capability_version for spec in task.pending_specs.values()}
            if len(versions) > 1:
                raise ValueError(
                    "Pending numerical specifications have inconsistent adapter versions"
                )
            version = next(iter(versions)) if versions else "unrecorded"
            specs = {
                kind: spec.model_copy(update={"capability_version": version})
                for kind, spec in specs.items()
            }
            pending_specs = {kind: specs[kind] for kind in request.analyses}
            if task.pending_specs and task.pending_specs != pending_specs:
                raise ValueError(
                    "Pending numerical specifications differ from the confirmed inputs"
                )
            if not task.pending_specs:
                logger.warning("Adapter version was not recorded for Worker request %s", request.id)
                task.pending_specs = pending_specs
        cached: dict[AnalysisKind, AnalysisRun] = {}
        directory = Path(task.result_directory)
        for kind, spec in specs.items():
            match = next(
                (
                    run for run in reversed(task.analysis_runs)
                    if run.spec.fingerprint == spec.fingerprint
                ),
                None,
            )
            if match is not None:
                if load_run(directory, match.id, spec) != match:
                    raise ValueError("Recorded analysis receipt is missing or inconsistent")
                cached[kind] = match
        missing = [
            kind for kind in task.baseline.analyses if kind not in cached
        ]
        if not missing and request is None:
            task.result = self._numerical_projection(task, cached)
            for artifact in task.result.artifacts:
                verify_artifact(directory, artifact)
            return {"source": "reused recorded numerical evidence", "run_id": task.result.run_id}
        if request is None:
            request = AnalysisRunRequest(
                id=uuid4().hex,
                project_root=task.project.root,
                project_fingerprint=task.project.fingerprint,
                trim_point=task.baseline.trim_point,
                analyses=missing,
                trusted=True,
            )
            task.pending_request = request
            task.pending_specs = {kind: specs[kind] for kind in missing}
            task.worker_run = None
        elif set(missing) - set(request.analyses):
            raise ValueError(
                "Recorded worker request is missing required cached numerical branches"
            )
        task.calls[-1].inputs["request"] = request.model_dump(mode="json")
        self.store.save(conversation)
        run_directory = directory / "numerical" / request.id
        if run_directory.is_symlink() or run_directory.parent.is_symlink():
            raise ValueError("Unsafe numerical evidence directory")

        def progress(record: RunRecord) -> None:
            task.worker_run = record
            task.stage = record.stage
            self.store.save(conversation)

        result = self._backend().execute(request, run_directory, progress, resume=reconcile)
        if (
            result.run_id != request.id
            or result.project_fingerprint != task.baseline.project_fingerprint
            or result.trim_point != task.baseline.trim_point
            or {item.kind for item in result.analyses} != set(request.analyses)
        ):
            raise ValueError("Numerical result does not match the confirmed request")
        for artifact in result.artifacts:
            verify_artifact(run_directory, artifact)
        _write_json(run_directory / "analysis.json", result.model_dump())
        paths = [*result.artifacts, describe_artifact(run_directory, "analysis.json")]
        if (run_directory / "matlab.log").is_file():
            paths.append(describe_artifact(run_directory, "matlab.log"))
        prefix = f"numerical/{request.id}/"
        records = [
            record.model_copy(
                update={"artifact_paths": [prefix + name for name in record.artifact_paths]}
            )
            for record in result.analyses
        ]
        artifacts = [
            artifact.model_copy(update={"path": prefix + artifact.path}) for artifact in paths
        ]
        self._merge_artifacts(task, artifacts)
        for record in records:
            branch = AnalysisResult(
                run_id=result.run_id,
                project_fingerprint=result.project_fingerprint,
                trim_point=result.trim_point,
                matlab_version=result.matlab_version,
                analyses=[record],
                artifacts=artifacts,
                provenance={
                    request.id: {
                        "matlab_version": result.matlab_version,
                        "provenance": result.provenance,
                    }
                },
            )
            output = branch.model_dump(mode="json")
            run_id = uuid5(NAMESPACE_URL, f"flight-agent:{request.id}:{record.kind}").hex
            saved = load_run(directory, run_id, specs[record.kind])
            if saved is not None and saved.output != output:
                raise ValueError("Reconciled numerical evidence differs from its immutable receipt")
            receipt = saved or AnalysisRun(
                id=run_id,
                execution_id=request.id,
                spec=specs[record.kind],
                output=output,
                output_sha256=fingerprint(output),
                started_at=task.calls[-1].started_at,
                finished_at=now(),
                artifacts=[
                    EvidenceFile(path=item.path, sha256=item.sha256, size=item.size)
                    for item in artifacts
                ],
            )
            persist_run(directory, receipt)
            self._merge_artifacts(task, [describe_artifact(directory, receipt_name(run_id))])
            if not any(run.id == receipt.id for run in task.analysis_runs):
                task.analysis_runs.append(receipt)
            cached[record.kind] = receipt
        task.result = self._numerical_projection(task, cached)
        task.pending_request = None
        task.pending_specs = {}
        self.store.save(conversation)
        return {
            "source": "reconciled worker run" if reconcile else "worker execution",
            "run_id": request.id,
            "analyses": list(request.analyses),
        }

    def _numerical_projection(
        self, task: VerificationTask, runs: dict[AnalysisKind, AnalysisRun]
    ) -> AnalysisResult:
        results = [
            AnalysisResult.model_validate(runs[kind].output) for kind in task.baseline.analyses
        ]
        for kind, result in zip(task.baseline.analyses, results, strict=True):
            if (
                result.project_fingerprint != task.baseline.project_fingerprint
                or result.trim_point != task.baseline.trim_point
                or len(result.analyses) != 1
                or result.analyses[0].kind != kind
            ):
                raise ValueError("Recorded numerical branch does not match its baseline")
        return AnalysisResult(
            run_id=results[-1].run_id,
            project_fingerprint=task.baseline.project_fingerprint,
            trim_point=task.baseline.trim_point,
            matlab_version=results[-1].matlab_version,
            analyses=[result.analyses[0] for result in results],
            artifacts=[
                item for item in task.artifacts
                if item.path.startswith("numerical/")
            ],
            provenance={
                key: value for result in results for key, value in result.provenance.items()
            },
        )

    def _evaluate(
        self, conversation: Conversation, task: VerificationTask, resume: bool
    ) -> dict[str, JsonValue]:
        if task.result is None:
            raise ValueError("Evaluation requires numerical evidence")
        spec = self.tools.evaluation_spec(task)
        run_id = uuid5(NAMESPACE_URL, f"evaluation:{task.id}:{spec.fingerprint}").hex
        directory = Path(task.result_directory)
        saved = load_run(directory, run_id, spec, layer="evaluation")
        recorded = next((item for item in task.evaluation_runs if item.id == run_id), None)
        if recorded is not None and saved != recorded:
            raise ValueError("Recorded evaluation receipt is missing or inconsistent")
        if saved is None:
            evaluation = evaluate(task.baseline, task.result)
            output = evaluation.model_dump(mode="json")
            saved = AnalysisRun(
                id=run_id, spec=spec, output=output, output_sha256=fingerprint(output),
                started_at=task.calls[-1].started_at, finished_at=now(),
                artifacts=[
                    EvidenceFile(path=item.path, sha256=item.sha256, size=item.size)
                    for item in task.result.artifacts
                ],
            )
            persist_run(directory, saved, layer="evaluation")
        task.evaluation = Evaluation.model_validate(
            {**saved.output, "baseline_version": task.baseline.version}
        )
        if recorded is None:
            task.evaluation_runs.append(saved)
        name = receipt_name(run_id, layer="evaluation")
        self._merge_artifacts(task, [describe_artifact(directory, name)])
        task.calls[-1].inputs.update(evaluation_fingerprint=spec.fingerprint, receipt=name)
        self.store.save(conversation)
        return task.evaluation.model_dump(mode="json")

    def _explain(self, task: VerificationTask) -> None:
        if task.explanation is not None or task.explanation_error is not None:
            return
        try:
            task.explanation = self.planner.explain(task)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Evidence explanation failed for %s", task.id)
            task.explanation_error = str(exc)[:4000]

    def _answer(self, conversation: Conversation, question: str) -> Conversation:
        task = conversation.task
        if not isinstance(task, VerificationTask) or task.evaluation is None:
            return self._wait(conversation, "尚无已判定的数值证据；请先完成基线确认和验证。")
        try:
            explanation = self.planner.explain(task, question)
        except (APIError, OSError, ValueError, RuntimeError) as exc:
            logger.exception("Follow-up explanation failed for %s", task.id)
            return self._wait(conversation, "LLM 解释未生成：" + str(exc), error=str(exc))
        if not conversation.restore_turn_origin():
            return self._wait(conversation, explanation.text)
        self._message(conversation, "assistant", explanation.text)
        self.store.save(conversation)
        return conversation

    def _execution_record(
        self, conversation: Conversation, task: VerificationTask
    ) -> dict[str, JsonValue]:
        return {
            "conversation_id": conversation.id,
            "task_id": task.id,
            "output_root": task.output_root,
            "result_directory": task.result_directory,
            "status": conversation.status,
            "error": conversation.error,
            "messages": [item.model_dump(mode="json") for item in conversation.messages],
            "baseline": task.baseline.model_dump(mode="json"),
            "baseline_history": [item.model_dump(mode="json") for item in task.baseline_history],
            "proposal": task.proposal.model_dump(mode="json"),
            "plan_history": [item.model_dump(mode="json") for item in task.plan_history],
            "nodes": [item.model_dump(mode="json") for item in task.nodes],
            "calls": [item.model_dump(mode="json") for item in task.calls],
            "decisions": [item.model_dump(mode="json") for item in task.decisions],
            "analysis_runs": [item.model_dump(mode="json") for item in task.analysis_runs],
            "evaluation_runs": [item.model_dump(mode="json") for item in task.evaluation_runs],
            "failure_proposal": (
                task.failure_proposal.model_dump(mode="json") if task.failure_proposal else None
            ),
            "replan_error": task.replan_error,
        }

    def _report_request(self, conversation: Conversation, task: VerificationTask) -> ReportRequest:
        if task.result is None or task.evaluation is None:
            raise ValueError("Delivery requires recorded analysis and deterministic evaluation")
        return ReportRequest(
            task_id=task.id,
            title=task.proposal.title,
            project=task.project,
            baseline=task.baseline,
            result=task.result,
            evaluation=task.evaluation,
            derived_artifacts=[
                item for item in task.artifacts if item.path.startswith("evaluation/")
            ],
            grouping=task.proposal.grouping,
            explanation=task.explanation.text if task.explanation else None,
            explanation_evidence=task.explanation.evidence if task.explanation else [],
            explanation_error=task.explanation_error,
            execution_record=self._execution_record(conversation, task),
        )

    def _deliver(
        self, conversation: Conversation, task: VerificationTask, resume: bool
    ) -> dict[str, JsonValue]:
        self._explain(task)
        self.store.save(conversation)
        artifacts = deliver_report(
            self._report_request(conversation, task), Path(task.result_directory)
        )
        self._merge_artifacts(task, artifacts)
        self.store.save(conversation)
        return {"source": "local delivery", "artifacts": [item.path for item in artifacts]}

    def _merge_artifacts(self, task: VerificationTask, artifacts: list[Artifact]) -> None:
        by_path = {item.path: item for item in task.artifacts}
        by_path.update({item.path: item for item in artifacts})
        task.artifacts = list(by_path.values())

    def _fail(self, conversation: Conversation, task: VerificationTask, error: Exception) -> None:
        known_failure = self._known_failed_run(task)
        unknown = task.pending_request is not None and not known_failure
        conversation.status = "interrupted" if unknown else "failed"
        conversation.error = str(error)[:4000]
        conversation.pending_decision = PendingDecision(
            kind="retry" if task.pending_request and known_failure else "resume",
            message="执行已停止，错误已保存。续接只核对已记录的运行；重试必须单独确认。",
        )
        self._message(conversation, "assistant", conversation.pending_decision.message)
        self.store.save(conversation)
        if task.replan_attempts == 0:
            task.replan_attempts += 1
            self.store.save(conversation)
            try:
                candidate = self.planner.replan(
                    task,
                    f"{task.stage}: {type(error).__name__}: {str(error)[:4000]}",
                    tools=self.tools.catalog(),
                )
                if candidate.action == "plan":
                    self._validate_plan(task.project, candidate)
                elif candidate.action != "clarify":
                    raise ValueError("Failure recovery requires a plan or a clarification")
                task.failure_proposal = candidate
            except (APIError, OSError, ValueError, RuntimeError) as exc:
                logger.exception("Failure clarification failed for %s", task.id)
                task.replan_error = str(exc)[:4000]
        saved_proposal = task.failure_proposal
        if saved_proposal is not None:
            if saved_proposal.action == "clarify":
                conversation.pending_decision = PendingDecision(
                    kind=conversation.pending_decision.kind,
                    message=saved_proposal.question,
                )
            elif saved_proposal.action == "plan":
                conversation.pending_decision = PendingDecision(
                    kind="resume" if unknown else "confirm_replan",
                    message=(
                        "已有恢复建议，但 Worker 状态未知；请先续接原运行，不能替换或重复提交。"
                        if unknown
                        else f"恢复建议：{saved_proposal.title}。{saved_proposal.rationale}"
                        " 采纳后会生成待确认的新基线；采纳本身不会执行。"
                    ),
                )
            self._message(conversation, "assistant", conversation.pending_decision.message)
        self.store.save(conversation)
