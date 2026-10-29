# Remove v1 Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the fixed v1 runtime, upload/demo Agent, legacy task schemas, APIs, tests, and records while preserving every supported generic v2 MATLAB, Simulink Worker, diagnostic, evaluation, and delivery capability.

**Architecture:** Extract the shared model transport and diagnostic engine state from legacy orchestration first, then make `AgentTask` and `EngineeringRuntime` the only conversation contracts. Replace upload-specific background infrastructure with a generic bounded runner, assemble only the v2 application, and delete legacy modules after import guards prove no supported code depends on them.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, LangChain Core structured model calls, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-29-v2-runtime-optimization-design.md`

## Global Constraints

- Keep only the generic v2 Agent execution model.
- Existing v1 records do not need migration or archival.
- Preserve the current confirmation, permission, source-attribution, hashing, evidence, recovery, and deterministic-evaluation guarantees.
- Worker transport and diagnostic computation components used by registered v2 capabilities remain supported.
- Do not delete v2 conversations, task artifacts, external delivery directories, or source projects.
- Ambiguous records are reported and are not deleted automatically.
- Local MATLAB remains single-slot until the scheduling stage changes it explicitly.
- Large evidence remains on disk; this stage does not introduce SQLite.
- Do not add Redis, Celery, Node, or a frontend build system.
- Every task below ends in a green targeted test and a separate commit.

---

### Task 1: Extract v2 Shared Planning and JSON Utilities

**Files:**
- Create: `src/flight_agent/json_io.py`
- Create: `src/flight_agent/verification/structured_planner.py`
- Modify: `src/flight_agent/verification/agent_planner.py:1-30,1325-1415`
- Modify: `src/flight_agent/verification/store.py:1-20`
- Modify: `src/flight_agent/verification/backend.py:1-20,110-135`
- Modify: `src/flight_agent/jobs.py:1-20`
- Modify: `src/flight_agent/session.py:1-35`
- Test: `tests/test_structured_planner.py`
- Test: `tests/test_json_io.py`
- Modify: `tests/test_agent_diagnostic_engine.py:185-210`

**Interfaces:**
- Produces: `write_json(path: Path, data: object) -> None`
- Produces: `redact_text(text: str, local_paths: Sequence[str | None] = ()) -> str`
- Produces: `conversation_paths(conversation: Conversation) -> list[str | None]`
- Produces: `StructuredPlanner.with_timeout(seconds: float) -> StructuredPlanner`
- Produces: `StructuredPlanner.call(name, schema, context, *, system_prompt)`
- Consumes: existing `ModelSettings`, `structured_response()`, and LangChain chat-model interfaces.

- [ ] **Step 1: Write failing utility and planner import tests**

```python
# tests/test_json_io.py
import json

from flight_agent.json_io import write_json


def test_write_json_atomically_replaces_complete_document(tmp_path):
    target = tmp_path / "state.json"
    write_json(target, {"version": 1})
    write_json(target, {"version": 2, "ready": True})
    assert json.loads(target.read_text("utf-8")) == {"version": 2, "ready": True}
    assert list(tmp_path.glob(".*.tmp")) == []
```

```python
# tests/test_structured_planner.py
import pytest

from flight_agent.verification.structured_planner import StructuredPlanner, redact_text


def test_redact_text_hides_local_paths_and_credentials():
    value = redact_text(
        "root=/private/project token=secret-value",
        ["/private/project"],
    )
    assert "/private/project" not in value
    assert "secret-value" not in value


def test_structured_planner_rejects_nonpositive_timeout():
    with pytest.raises(ValueError, match="finite and positive"):
        StructuredPlanner().with_timeout(0)
```

- [ ] **Step 2: Run the tests and verify the new modules are missing**

Run:

```bash
python -m pytest tests/test_json_io.py tests/test_structured_planner.py -q
```

Expected: collection fails because `flight_agent.json_io` and
`flight_agent.verification.structured_planner` do not exist.

- [ ] **Step 3: Move atomic JSON writing out of the legacy session**

Create `src/flight_agent/json_io.py` with the existing atomic replacement behavior:

```python
import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
```

Replace imports of `session._write_json` with `json_io.write_json`. Keep a temporary
alias in `session.py` only until Task 5 deletes the legacy session:

```python
from .json_io import write_json

_write_json = write_json
```

- [ ] **Step 4: Move shared structured planning code**

Create `verification/structured_planner.py` by moving `StructuredPlanner`,
`redact_text`, and the v2-safe path helper from `verification/planner.py`.

The path helper must not inspect legacy task fields:

```python
def conversation_paths(conversation: Conversation) -> list[str | None]:
    tasks = [
        *conversation.archived_tasks,
        *([conversation.task] if conversation.task is not None else []),
    ]
    return [
        conversation.project_root,
        conversation.output_root,
        *(path for task in tasks for path in (task.output_root, task.result_directory)),
    ]
```

Update `agent_planner.py`:

```python
from .structured_planner import StructuredPlanner, conversation_paths, redact_text
```

Replace `_conversation_paths(conversation)` calls with
`conversation_paths(conversation)`.

- [ ] **Step 5: Move the existing timeout test to the new module**

Change the bounded timeout test in `tests/test_agent_diagnostic_engine.py` to import:

```python
from flight_agent.verification.structured_planner import StructuredPlanner
```

Move that test into `tests/test_structured_planner.py` so the shared transport has a
focused test owner.

- [ ] **Step 6: Run targeted tests**

Run:

```bash
python -m pytest \
  tests/test_json_io.py \
  tests/test_structured_planner.py \
  tests/test_agent_planner.py \
  tests/test_agent_diagnostic_engine.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the shared extraction**

```bash
git add \
  src/flight_agent/json_io.py \
  src/flight_agent/verification/structured_planner.py \
  src/flight_agent/verification/agent_planner.py \
  src/flight_agent/verification/store.py \
  src/flight_agent/verification/backend.py \
  src/flight_agent/jobs.py \
  src/flight_agent/session.py \
  tests/test_json_io.py \
  tests/test_structured_planner.py \
  tests/test_agent_diagnostic_engine.py
git commit -m "refactor: extract v2 planning utilities"
```

---

### Task 2: Make Offline Diagnosis a Pure v2 Capability

**Files:**
- Create: `src/flight_agent/verification/diagnostic_planner.py`
- Modify: `src/flight_agent/verification/diagnosis_contracts.py:1-360`
- Modify: `src/flight_agent/verification/diagnostic_engine.py:1-570`
- Modify: `src/flight_agent/verification/adapters/diagnostics.py:1-230`
- Modify: `src/flight_agent/verification/plugins.py:1-125`
- Modify: `src/flight_agent/web.py:75-105`
- Modify: `tests/diagnosis_helpers.py`
- Modify: `tests/test_agent_diagnostic_engine.py`
- Create: `tests/test_diagnostic_planner.py`

**Interfaces:**
- Produces: `DiagnosticRunState`, the mutable checkpoint state owned by the v2
  `offline_diagnosis` capability.
- Produces: `DiagnosticReviewer.review_diagnosis(state, *, tools) -> DiagnosisReview`.
- Produces for tests: `tests.diagnosis_helpers.diagnostic_state(tmp_path) ->
  DiagnosticRunState`.
- Changes: `register_builtin_plugins` receives a keyword-only
  `diagnostic_reviewer: DiagnosticReviewer` argument instead of
  `planner: VerificationPlanner`.
- Removes from the v2 path: `DiagnosisTask`, `VerificationPlanner`, and
  `DiagnosisRuntime`.

- [ ] **Step 1: Write failing v2 diagnostic checkpoint tests**

Add to `tests/test_agent_diagnostic_engine.py`:

```python
def test_diagnostic_checkpoint_uses_v2_internal_state(tmp_path):
    runtime, store, pending, _ = proposed_diagnosis(tmp_path)
    done = runtime.confirm(pending.id, 1, trusted=True)
    checkpoint_path = next(iter(done.task.checkpoints.values()))
    checkpoint = json.loads(
        (Path(done.task.result_directory) / checkpoint_path).read_text("utf-8")
    )
    state = checkpoint["task"]
    assert state["schema_version"] == "2.0"
    assert "scenario" not in state
    assert "proposal" not in state
    assert "decisions" not in state
    assert "output_root" not in state
```

Create `tests/test_diagnostic_planner.py` with a scripted structured response and assert
that only the review API is exposed:

```python
def test_diagnostic_reviewer_returns_grounded_review(tmp_path):
    state = diagnostic_state(tmp_path)
    expected = {
        "action": "stop",
        "reason": "The registered data cannot distinguish the remaining hypothesis.",
        "assessments": [
            {
                "hypothesis_id": item.id,
                "status": "unknown",
                "reason": "No discriminating evidence is available.",
                "evidence": [],
            }
            for item in state.current_hypotheses
        ],
        "unknowns": ["Additional independent evidence is required."],
        "analyses": [],
        "stop_reason": "unsupported",
        "hypothesis_revision": None,
        "summary": None,
    }
    model = RecordingModel(
        responses=[tool_message("review_diagnosis", expected)]
    )
    reviewer = DiagnosticReviewer(model)
    review = reviewer.review_diagnosis(
        state,
        tools=diagnostic_capabilities().catalog(),
    )
    assert review.action in ("continue", "stop")
    assert not hasattr(reviewer, "propose_diagnosis")
```

Use the existing `RecordingModel` and `tool_message` helpers currently imported by
`tests/test_diagnosis_planner.py`; move their review-only imports into the new test.

- [ ] **Step 2: Run the tests and verify they fail on legacy state**

Run:

```bash
python -m pytest \
  tests/test_agent_diagnostic_engine.py::test_diagnostic_checkpoint_uses_v2_internal_state \
  tests/test_diagnostic_planner.py -q
```

Expected: failure because checkpoints contain `DiagnosisTask` and
`DiagnosticReviewer` does not exist.

- [ ] **Step 3: Define `DiagnosticRunState`**

Add to `diagnosis_contracts.py`:

```python
class DiagnosticExecutionNode(MutableRecord):
    id: Name
    tool: Name
    inputs: dict[str, JsonValue] = {}
    status: Literal["pending", "running", "completed", "failed", "interrupted"] = "pending"
    error: str | None = None


class DiagnosticRunState(MutableRecord):
    schema_version: Literal["2.0"] = "2.0"
    id: RunId
    project: DiagnosticProject
    baseline: DiagnosisBaseline
    result_directory: Nonempty
    nodes: list[DiagnosticExecutionNode] = []
    calls: list[ToolCall] = []
    analysis_runs: list[AnalysisRun] = []
    plan_revisions: list[DiagnosisPlanRevision] = []
    rounds: list[DiagnosticRound] = []
    assessments: list[HypothesisAssessment] = []
    artifacts: list[EvidenceFile] = []
    input_snapshot: DiagnosticProject | None = None
    pending_analysis: PendingAnalysis | None = None
    termination: DiagnosisTermination | None = None
    started_at: str | None = None
    dispatch_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 0
    no_progress_rounds: Annotated[int, Field(strict=True, ge=0)] = 0

    @property
    def current_hypotheses(self) -> list[Hypothesis]:
        for round_ in reversed(self.rounds):
            if round_.review.hypothesis_revision is not None:
                return round_.review.hypothesis_revision.hypotheses
        return self.baseline.hypotheses
```

Use the existing mutable Pydantic base, `ToolCall`, `AnalysisRun`, and `EvidenceFile`.
Do not duplicate outer confirmation, permissions, decisions, or delivery fields.

- [ ] **Step 4: Extract the diagnostic reviewer**

Create `diagnostic_planner.py` containing only `DIAGNOSIS_PROMPT` and:

```python
class DiagnosticReviewer(StructuredPlanner):
    def review_diagnosis(
        self,
        state: DiagnosticRunState,
        *,
        tools: list[dict[str, object]],
    ) -> DiagnosisReview:
        paths = [state.project.root, state.result_directory]
        return self.call(
            "review_diagnosis",
            DiagnosisReview,
            build_review_context(state, tools, paths),
            system_prompt=DIAGNOSIS_PROMPT,
        )
```

Move the current `VerificationPlanner.review_diagnosis()` context construction without
changing evidence, hypothesis, redaction, or timeout semantics.

- [ ] **Step 5: Convert the engine and adapter**

Change every `DiagnosisTask` annotation and constructor in `diagnostic_engine.py` and
`adapters/diagnostics.py` to `DiagnosticRunState`.

Create state in the adapter:

```python
state = DiagnosticRunState(
    id=context.call_id,
    project=project,
    baseline=baseline,
    result_directory=str(directory),
    plan_revisions=[plan],
    nodes=engine.nodes(plan),
    assessments=[
        HypothesisAssessment(
            hypothesis_id=item.id,
            status="unknown",
            reason="No confirmed analysis executed.",
        )
        for item in baseline.hypotheses
    ],
)
```

Rename private engine methods used by the adapter:

```python
def plan(
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
    return self._plan(
        baseline,
        project,
        analyses,
        reason,
        version,
        evidence,
        hypotheses=hypotheses,
    )


def nodes(self, plan: DiagnosisPlanRevision) -> list[DiagnosticExecutionNode]:
    return [
        DiagnosticExecutionNode(
            id=f"round-{plan.version}-{index + 1}",
            tool=analysis.capability,
            inputs=analysis.inputs,
        )
        for index, analysis in enumerate(plan.analyses)
    ]
```

The adapter must not call underscore-prefixed engine methods after this task.

- [ ] **Step 6: Inject the reviewer without constructing a legacy planner**

Change the plugin parameter line to:

```python
diagnostic_reviewer: DiagnosticReviewer,
```

Change the diagnostic registration line to:

```python
tools.register(diagnostic_capability(diagnostic_reviewer))
```

The Web factory creates `DiagnosticReviewer()` independently of the outer
`AgentPlanner`.

- [ ] **Step 7: Replace legacy diagnosis test helpers**

Update `tests/diagnosis_helpers.py` so `AdaptivePlanner` and `EvolvingPlanner`
subclass or emulate `DiagnosticReviewer` only. Add `diagnostic_state(tmp_path)` using
`diagnostic_project(tmp_path / "data")`, a confirmed `DiagnosisBaseline`, the first
validated plan, and unknown initial assessments. Remove tests that exercise
`DiagnosisRuntime.propose()` or a standalone `DiagnosisTask`.

Keep tests for:

- two evidence rounds,
- budget exhaustion,
- lost acknowledgement recovery,
- unknown execution reconciliation,
- capability version mismatch,
- no-progress termination,
- grounded review validation.

- [ ] **Step 8: Run diagnostic and generic Agent tests**

Run:

```bash
python -m pytest \
  tests/test_diagnosis_contracts.py \
  tests/test_diagnostic_data.py \
  tests/test_agent_diagnostic_engine.py \
  tests/test_diagnostic_planner.py \
  tests/test_agent_adapters.py -q
```

Expected: all selected tests pass without importing `DiagnosisTask`,
`DiagnosisRuntime`, or `VerificationPlanner`.

- [ ] **Step 9: Commit the v2 diagnostic capability**

```bash
git add \
  src/flight_agent/verification/diagnostic_planner.py \
  src/flight_agent/verification/diagnosis_contracts.py \
  src/flight_agent/verification/diagnostic_engine.py \
  src/flight_agent/verification/adapters/diagnostics.py \
  src/flight_agent/verification/plugins.py \
  src/flight_agent/web.py \
  tests/diagnosis_helpers.py \
  tests/test_agent_diagnostic_engine.py \
  tests/test_diagnostic_planner.py
git commit -m "refactor: make diagnostics v2 native"
```

---

### Task 3: Collapse Conversation Contracts to v2 and Add Safe Legacy Cleanup

**Files:**
- Create: `src/flight_agent/verification/legacy_cleanup.py`
- Modify: `src/flight_agent/verification/contracts.py:1-565`
- Modify: `src/flight_agent/verification/views.py:1-205`
- Modify: `src/flight_agent/verification/store.py:85-170`
- Modify: `src/flight_agent/verification/agent_runtime.py:40-55`
- Test: `tests/test_agent_contracts.py`
- Test: `tests/test_agent_views.py`
- Create: `tests/test_legacy_cleanup.py`

**Interfaces:**
- Produces: `Conversation.schema_version == "2.0"`.
- Produces: `Task = AgentTask`; no runtime task union.
- Produces: `classify_record(data: object) -> Literal["v1", "v2", "unknown"]`.
- Produces: `normalize_v2_record(data: dict[str, object]) -> dict[str, object]`.
- Produces: `cleanup_legacy_records(root: Path, *, apply: bool) -> CleanupReport`.
- Produces: `has_unresolved_call(task: AgentTask) -> bool` in `agent_runtime.py`.

- [ ] **Step 1: Write failing contract and cleanup tests**

```python
def test_conversation_rejects_legacy_task():
    data = conversation_data()
    data["task"] = {"scenario": "d1"}
    with pytest.raises(ValidationError):
        Conversation.model_validate(data)
```

```python
def test_cleanup_deletes_only_positive_v1_records(tmp_path):
    def write_record(directory, data):
        directory.mkdir()
        path = directory / "conversation.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    v1 = write_record(tmp_path / ("a" * 32), {"task": {"scenario": "d1"}})
    v2 = write_record(
        tmp_path / ("b" * 32),
        {"schema_version": "2.0", "task": {"schema_version": "2.0"}},
    )
    unknown = write_record(tmp_path / ("c" * 32), {"task": None})

    report = cleanup_legacy_records(tmp_path, apply=True)

    assert not v1.parent.exists()
    assert v2.parent.exists()
    assert unknown.parent.exists()
    assert report.deleted == [v1.parent.name]
    assert report.unknown == [unknown.parent.name]
```

```python
def test_normalize_existing_v2_record_removes_legacy_envelope_fields():
    normalized = normalize_v2_record({
        "scenario": "auto",
        "goal": "",
        "active_workflow": None,
        "workflow_agenda": [],
        "task": {"schema_version": "2.0"},
        "archived_tasks": [],
    })
    assert normalized["schema_version"] == "2.0"
    assert "scenario" not in normalized
    assert "active_workflow" not in normalized
```

- [ ] **Step 2: Run tests and verify legacy unions still accept v1**

Run:

```bash
python -m pytest \
  tests/test_agent_contracts.py \
  tests/test_agent_views.py \
  tests/test_legacy_cleanup.py -q
```

Expected: new assertions fail because `Conversation` still accepts the legacy union
and cleanup functions do not exist.

- [ ] **Step 3: Reduce task and conversation contracts**

Keep Worker transport contracts used by `backend.py` and `verification/worker.py`.
Remove only orchestration contracts:

- `EvaluationBaseline`
- `PlanProposal`
- `EvidenceExplanation`
- `ExecutionNode`
- `PlanVersion`
- `VerificationTask`
- `DiagnosisTask`
- `GeneralTask`
- `LegacyTask`

Set:

```python
Task = AgentTask


class Conversation(MutableRecord):
    schema_version: Literal["2.0"] = "2.0"
    id: Identifier
    created_at: Nonempty
    updated_at: Nonempty
    status: ConversationStatus = "waiting_for_user"
    project_root: str | None = None
    resource_location: Name | None = None
    output_root: str | None = None
    output_root_reset: bool = False
    task: AgentTask | None = None
    archived_tasks: list[AgentTask] = []
    pending_decision: PendingDecision | None = None
    messages: list[ConversationMessage] = []
    error: str | None = None
    turn_origin: ConversationState | None = None
```

Keep `ToolCall`, `Decision`, `PendingDecision`, `ConversationMessage`,
`AgentExecutionNode`, and `AgentTask`.

- [ ] **Step 4: Make views v2-only**

Delete the lazy `compat.to_legacy_view` import and project directly:

```python
def to_conversation_view(record: Conversation) -> ConversationView:
    return ConversationView(
        id=record.id,
        status=record.status,
        project_root=record.project_root,
        resource_location=record.resource_location,
        output_root=record.output_root,
        task=to_task_view(record.task) if record.task is not None else None,
        archived_tasks=[to_task_view(task) for task in record.archived_tasks],
        messages=record.messages,
        pending_decision=record.pending_decision,
        error=record.error,
    )
```

Change `TaskView.schema_version` to `Literal["2.0"]`.

- [ ] **Step 5: Implement raw cleanup and v2 normalization**

`classify_record` must operate on untrusted JSON without Pydantic legacy models:

```python
def classify_record(data: object) -> RecordKind:
    if not isinstance(data, dict):
        return "unknown"
    if data.get("schema_version") == "2.0":
        return "v2"
    tasks = [
        data.get("task"),
        *(
            data.get("archived_tasks", [])
            if isinstance(data.get("archived_tasks"), list)
            else []
        ),
    ]
    concrete = [item for item in tasks if isinstance(item, dict)]
    if concrete and all(item.get("schema_version") == "2.0" for item in concrete):
        return "v2"
    if any(item.get("scenario") in {"d1", "d2", "general"} for item in concrete):
        return "v1"
    return "unknown"
```

`cleanup_legacy_records` validates directory IDs, rejects symlinks, never follows
paths outside `root`, and deletes only directories classified `v1` when `apply=True`.
Use targeted `shutil.rmtree(candidate)` only after the resolved-path containment check.

- [ ] **Step 6: Normalize old v2 envelopes before validation**

In `ConversationStore.get()`, parse bounded JSON to a Python value, classify it, and:

- normalize `v2`,
- raise `ValueError("Legacy conversation is unsupported; run cleanup-legacy")` for `v1`,
- raise `ValueError("Conversation schema is unknown")` for `unknown`.

Do not silently delete records during startup.

- [ ] **Step 7: Move the unresolved-call helper**

Add to `agent_runtime.py`:

```python
def has_unresolved_call(task: AgentTask) -> bool:
    return any(call.status in ("running", "interrupted") for call in task.calls)
```

Update coordinator imports later in Task 4. Remove the `compat` dependency from
`agent_runtime.py` now.

- [ ] **Step 8: Run contract, store, view, history, and recovery tests**

Run:

```bash
python -m pytest \
  tests/test_agent_contracts.py \
  tests/test_agent_views.py \
  tests/test_agent_history.py \
  tests/test_agent_recovery.py \
  tests/test_legacy_cleanup.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit v2-only contracts**

```bash
git add \
  src/flight_agent/verification/legacy_cleanup.py \
  src/flight_agent/verification/contracts.py \
  src/flight_agent/verification/views.py \
  src/flight_agent/verification/store.py \
  src/flight_agent/verification/agent_runtime.py \
  tests/test_agent_contracts.py \
  tests/test_agent_views.py \
  tests/test_agent_history.py \
  tests/test_agent_recovery.py \
  tests/test_legacy_cleanup.py
git commit -m "refactor: make conversation contracts v2 only"
```

---

### Task 4: Remove Legacy Routing, Scenario Aliases, and Runtime Switching

**Files:**
- Create: `src/flight_agent/background.py`
- Modify: `src/flight_agent/verification/resources.py`
- Modify: `src/flight_agent/verification/coordinator.py:1-260`
- Modify: `src/flight_agent/verification/routes.py:1-300`
- Modify: `src/flight_agent/cli.py:45-190`
- Modify: `src/flight_agent/static/agent.js`
- Modify: `tests/agent_helpers.py`
- Modify: `tests/test_agent_web.py`
- Modify: `tests/test_agent_browser.py`
- Modify: `tests/test_cli.py`
- Test: `tests/test_legacy_cleanup.py`

**Interfaces:**
- Produces: `BusyError` and `BackgroundRunner(max_workers: int = 1)` with
  `submit(operation: Callable[[], None]) -> None` and `close() -> None`.
- Changes: `EngineeringRuntime.__init__(store, planner, executor, resources)`.
- Changes: `MessageRequest` accepts `message`, structured requirements, project root,
  output root, and registered `location`; it no longer accepts `scenario`.
- Produces: `resolve_location(location: str | None, locations: list[ResourceLocation])`.
- Keeps: confirmation, resume, and known-failure retry.
- Removes: legacy replan endpoint and workflow catalog endpoint.

- [ ] **Step 1: Write failing API tests for removed legacy inputs**

```python
def test_scenario_alias_is_rejected_by_generic_api(tmp_path):
    app, _, _, _ = make_agent_web(
        tmp_path,
        [{"action": "answer", "text": "No execution."}],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        response = client.post(
            "/api/conversations",
            json={"message": "Analyze.", "scenario": "d1"},
            headers=headers,
        )
        assert response.status_code == 422


def test_legacy_workflow_catalog_is_removed(tmp_path):
    app, _, _, _ = make_agent_web(
        tmp_path,
        [{"action": "answer", "text": "No execution."}],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(
            "/api/conversations/workflows",
            headers=auth(client),
        )
        assert response.status_code == 404
```

Add a CLI parser test asserting `directory-task propose --scenario d1` fails.

- [ ] **Step 2: Run API and CLI tests and verify legacy inputs still exist**

Run:

```bash
python -m pytest \
  tests/test_agent_web.py \
  tests/test_agent_browser.py \
  tests/test_cli.py -q
```

Expected: the new removal tests fail.

- [ ] **Step 3: Extract the temporary generic background runner**

Create `background.py`:

```python
class BusyError(ValueError):
    pass


class BackgroundRunner:
    def __init__(self, max_workers: int = 1):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="flight-agent-task",
        )
        self.slot = threading.BoundedSemaphore(max_workers)

    def submit(self, operation: Callable[[], None]) -> None:
        if not self.slot.acquire(blocking=False):
            raise BusyError("Another task is running; wait for it to finish")
        try:
            self.pool.submit(self._run, operation)
        except RuntimeError:
            self.slot.release()
            raise

    def _run(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        finally:
            self.slot.release()

    def close(self) -> None:
        self.pool.shutdown(wait=True)
```

Adapt `ConversationManager` to call:

```python
self.jobs.submit(lambda: self._run(record.id, operation))
```

It must no longer access `.pool` and `.slot` directly.

- [ ] **Step 4: Move registered-location resolution**

Add to `verification/resources.py`:

```python
def resolve_location(
    requested: str | None,
    locations: list[ResourceLocation],
) -> str | None:
    if requested is None:
        return None
    matches = [
        item.id
        for item in locations
        if item.id == requested or item.location == requested
    ]
    if len(matches) != 1:
        raise ValueError(
            "Resource location is unknown or ambiguous; select a registered location"
        )
    return matches[0]
```

Do not retain `agent`, `worker`, `d1`, or `d2` aliases.

- [ ] **Step 5: Simplify `EngineeringRuntime`**

Change construction:

```python
class EngineeringRuntime:
    def __init__(
        self,
        store: ConversationStore,
        planner: AgentPlanner,
        executor: AgentExecutor,
        resources: ResourceRegistry,
    ):
        if executor.store is not store or executor.resources is not resources:
            raise ValueError("Coordinator and executor must share store and resources")
        self.store = store
        self.planner = planner
        self.executor = executor
        self.resources = resources
        self.lock = threading.Lock()
```

Remove:

- `legacy`,
- task-type switching,
- `legacy_decision`,
- `workflow_catalog`,
- scenario-based location selection.

Import `has_unresolved_call` from `agent_runtime`.

- [ ] **Step 6: Simplify routes**

Change:

```python
class ConversationManager:
    def __init__(self, runtime: EngineeringRuntime, jobs: BackgroundRunner):
        self.runtime = runtime
        self.jobs = jobs
```

Remove `LegacyRuntimeAdapter`, `VerificationRuntime`, `/workflows`, and `/replan`.
The recent list title is always:

```python
record.task.baseline.title if record.task else (
    record.messages[0].content[:100] if record.messages else "新会话"
)
```

- [ ] **Step 7: Simplify CLI request fields**

Remove `--scenario` and legacy host alias help. When a project root is supplied and
`--location` is absent, allow the server to select only when exactly one registered
location exists.

Add explicit cleanup:

```text
flight-agent cleanup-legacy --project-root PATH
flight-agent cleanup-legacy --project-root PATH --apply
```

Dry-run prints `v1`, `v2`, and `unknown` identifiers. `--apply` deletes only `v1`.

- [ ] **Step 8: Remove scenario fields from the frontend**

Delete scenario option generation and request properties from `agent.js`. Resource
selection uses only `/api/resource-locations` identifiers.

- [ ] **Step 9: Run generic Web, browser, CLI, and intake tests**

Run:

```bash
python -m pytest \
  tests/test_agent_web.py \
  tests/test_agent_browser.py \
  tests/test_agent_layout_browser.py \
  tests/test_agent_column_resize_browser.py \
  tests/test_verification_intake.py \
  tests/test_cli.py \
  tests/test_legacy_cleanup.py -q
```

Expected: all selected tests pass with no `scenario`, legacy workflow, or runtime
adapter behavior.

- [ ] **Step 10: Commit routing convergence**

```bash
git add \
  src/flight_agent/background.py \
  src/flight_agent/verification/resources.py \
  src/flight_agent/verification/coordinator.py \
  src/flight_agent/verification/routes.py \
  src/flight_agent/cli.py \
  src/flight_agent/static/agent.js \
  tests/agent_helpers.py \
  tests/test_agent_web.py \
  tests/test_agent_browser.py \
  tests/test_cli.py \
  tests/test_legacy_cleanup.py
git commit -m "refactor: route all conversations through v2"
```

---

### Task 5: Remove the Fixed Upload/Demo Product and Assemble One Web Runtime

**Files:**
- Modify: `src/flight_agent/web.py:1-405`
- Modify: `src/flight_agent/worker.py:1-110`
- Modify: `src/flight_agent/verification/worker.py`
- Modify: `src/flight_agent/verification/routes.py`
- Modify: `src/flight_agent/cli.py`
- Modify: `src/flight_agent/matlab.py`
- Modify: `src/flight_agent/models.py`
- Modify: `src/flight_agent/remote.py`
- Delete: `src/flight_agent/jobs.py`
- Delete: `src/flight_agent/agent.py`
- Delete: `src/flight_agent/knowledge.py`
- Delete: `src/flight_agent/rules.py`
- Delete: `src/flight_agent/session.py`
- Delete: `src/flight_agent/upload_engine.py`
- Delete: `src/flight_agent/upload_models.py`
- Delete: `src/flight_agent/matlab_worker.py`
- Delete: `src/flight_agent/static/index.html`
- Delete: `src/flight_agent/static/app.js`
- Delete: `src/flight_agent/static/style.css`
- Delete: `rules/demo.json`
- Delete: `knowledge/demo.md`
- Delete: `matlab/analyze_demo.m`
- Delete: `examples/web_script.m`
- Delete: `examples/web_function.m`
- Delete: `examples/web_metrics.m`
- Delete: `examples/parameters.json`
- Create: `tests/test_web_surface.py`
- Delete: `tests/test_web.py`
- Modify: `tests/test_verification_worker.py`
- Modify: `tests/test_cli.py`
- Delete: `tests/test_uploads.py`
- Delete: `tests/test_session.py`
- Delete: `tests/test_agent.py`
- Delete: `tests/test_matlab.py`
- Delete: `tests/test_worker.py`
- Delete: `tests/test_http_integration.py`
- Delete: `tests/test_web_browser.py`

**Interfaces:**
- Consumes: `BackgroundRunner` from Task 4.
- Changes: `create_web_app(root, *, engineering_runtime=None,
  native_directory_picker=False)`.
- Keeps: `/`, `/workbench`, generic conversation APIs, directory APIs, bootstrap,
  local MATLAB, Simulink Worker project routes, and Worker health.
- Removes: `/api/jobs`, Worker `/jobs`, `analyze` CLI, trusted upload configuration,
  and the upload workbench.

- [ ] **Step 1: Write failing product-boundary tests**

```python
def test_web_exposes_only_generic_workbench(tmp_path):
    app, _, _, _ = make_agent_web(
        tmp_path,
        [{"action": "answer", "text": "No execution."}],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/workbench").status_code == 200
        assert client.get("/api/jobs").status_code == 404
        assert client.get("/static/app.js").status_code == 404


def test_worker_rejects_removed_upload_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    app = create_worker_app(tmp_path, "secret")
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        assert client.post("/jobs", json={}, headers=headers).status_code == 404
        assert client.get("/health", headers=headers).json()["project_execution"] is True
```

Add a CLI test asserting the `analyze` subcommand is no longer present.

- [ ] **Step 2: Run boundary tests and verify old endpoints are still available**

Run:

```bash
python -m pytest \
  tests/test_web_surface.py \
  tests/test_verification_worker.py \
  tests/test_cli.py -q
```

Expected: new assertions fail because upload/demo endpoints and commands exist.

- [ ] **Step 3: Assemble only the generic runtime**

Change the Web factory signature:

```python
def create_web_app(
    root: Path,
    *,
    engineering_runtime: EngineeringRuntime | None = None,
    native_directory_picker: bool = False,
) -> FastAPI:
```

When no runtime is injected:

```python
conversations = ConversationStore(root / "runs" / "verification")
try:
    backend: AnalysisBackend | None = RemoteSimulinkBackend.from_environment()
except ValueError:
    backend = None
resources = ResourceRegistry()
tools: CapabilityRegistry[AgentContext] = CapabilityRegistry(agent_mode=True)
register_builtin_plugins(
    tools,
    resources,
    backend=backend,
    diagnostic_reviewer=DiagnosticReviewer(),
    local_backend=LocalMatlabBackend.from_environment(),
)
engineering_runtime = EngineeringRuntime(
    conversations,
    AgentPlanner(),
    AgentExecutor(conversations, tools, resources),
    resources,
)
```

Remove every `VerificationRuntime` branch and import.

- [ ] **Step 4: Delete upload/demo Web routes and assets**

Remove:

- `AnalysisRequest`,
- `CachedRunner`,
- `JobStore`,
- `/api/examples`,
- `/api/jobs` routes,
- upload result analysis,
- upload artifact routes.

Serve `agent.html` from both `/` and `/workbench`. Restrict static assets to:

```python
{"agent.js", "agent.css", "favicon.svg"}
```

- [ ] **Step 5: Make the Worker project-only**

Remove trusted-upload environment handling and `/jobs` routes. Startup requires:

```text
MATLAB_ALLOW_TRUSTED_PROJECTS=1
```

Health returns:

```python
{
    "service": "matlab-worker",
    "engine_package": engine_available(),
    "trusted_code_only": True,
    "project_execution": True,
}
```

Keep `/verification/projects/inspect`, `/verification/runs`, artifact download,
directory browsing, interruption marking, and reconciliation behavior.

Rename the upload-oriented remote base to a transport-only client:

```python
class RemoteClient:
    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "MATLAB_WORKER_URL must be an HTTP(S) URL without embedded credentials"
            )
        if parts.scheme == "http" and parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise ValueError(
                "Use HTTPS for remote hosts, or HTTP loopback through an SSH tunnel"
            )
        if len(token) < 32 or not token.isascii():
            raise ValueError(
                "MATLAB_WORKER_TOKEN must contain at least 32 ASCII characters"
            )
        self.url = url.rstrip("/")
        self.token = token
        self.client = client

    @classmethod
    def from_environment(cls) -> Self:
        url = os.environ.get("MATLAB_WORKER_URL", "").strip()
        token = os.environ.get("MATLAB_WORKER_TOKEN", "").strip()
        if not url or not token:
            raise ValueError("Configure MATLAB_WORKER_URL and MATLAB_WORKER_TOKEN first")
        return cls(url, token)

    def _request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        deadline: float,
        payload: dict[str, object] | None = None,
        *,
        limit: int = 8_000_000,
    ) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteError("Remote deadline exceeded; execution may still be active")
        with client.stream(
            method,
            self.url + path,
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=min(10, remaining),
            follow_redirects=False,
        ) as response:
            if not response.is_success:
                raise RemoteError(
                    f"Worker returned HTTP {response.status_code}; submission not retried"
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise RemoteError("Worker response exceeds the size limit")
                if time.monotonic() >= deadline:
                    raise RemoteError(
                        "Remote deadline exceeded; execution may still be active"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
```

Delete `RemoteWorker.run()` and upload-model imports. Change
`RemoteSimulinkBackend(RemoteClient)`. The Web directory picker checks
`backend.url` when `backend` is a `RemoteSimulinkBackend`; it does not construct an
upload client.

- [ ] **Step 6: Remove the fixed CLI and demo modules**

Delete the `analyze` command and demo asset checks from `doctor`. `doctor` checks:

- current Python,
- configured LLM provider,
- local MATLAB package availability,
- configured Worker URL,
- no external process or model request.

Retain `web`, `worker`, `directory-task`, and `cleanup-legacy`.

Keep `matlab.py` only if generic code still imports `engine_available()` or process
cleanup helpers. Remove `MatlabRunner` and fixed `Parameters`/`AnalysisResult` logic.

Reduce `models.py` to shared validation primitives still imported by v2:

```python
Contract
Nonempty
FiniteNumber
```

Retain any additional helper only when `rg` shows a generic runtime import. Remove
fixed parameters, rules, assessments, and demo-result models.

- [ ] **Step 7: Delete obsolete tests and retain generic coverage**

Delete only tests for removed upload/demo behavior. Preserve:

- Worker project execution tests,
- generic Agent Web/browser tests,
- local MATLAB capability tests,
- remote Simulink adapter tests,
- diagnostic capability tests.

Update fixtures so they no longer create `rules/demo.json`, `knowledge/demo.md`, or
`matlab/analyze_demo.m`.

- [ ] **Step 8: Run product-boundary and retained integration tests**

Run:

```bash
python -m pytest \
  tests/test_web_surface.py \
  tests/test_agent_web.py \
  tests/test_agent_browser.py \
  tests/test_verification_worker.py \
  tests/test_agent_adapters.py \
  tests/test_local_matlab_adapters.py \
  tests/test_cli.py -q
```

Expected: all selected tests pass; removed endpoints return 404.

- [ ] **Step 9: Commit the single-product Web and Worker**

```bash
git add -A
git commit -m "refactor: remove fixed upload agent"
```

Before committing, inspect `git diff --name-status` and confirm no generic diagnostic
example under `examples/diagnosis/` was deleted.

---

### Task 6: Delete Legacy Runtime Modules, Tests, Dependencies, and Documentation

**Files:**
- Delete: `src/flight_agent/verification/compat.py`
- Delete: `src/flight_agent/verification/runtime.py`
- Delete: `src/flight_agent/verification/planner.py`
- Delete: `src/flight_agent/verification/general.py`
- Delete: `src/flight_agent/verification/diagnosis.py`
- Delete: `src/flight_agent/verification/reports.py`
- Delete: `src/flight_agent/verification/evaluation.py`
- Delete: `src/flight_agent/verification/tools.py`
- Delete: legacy-only tests listed below
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/usage.md`
- Modify: `docs/inputs-outputs.md`
- Move: `docs/d1-agent.md` to `docs/simulink-worker.md`
- Move: `docs/d2-agent.md` to `docs/offline-diagnostics.md`
- Create: `tests/test_v2_only.py`

**Interfaces:**
- Produces: a repository with no production import or runtime reference to v1 task
  types, fixed scenario workflow IDs, or legacy adapters.
- Keeps: Worker project contracts and transport modules used by v2 Simulink
  capabilities.
- Keeps: generic diagnostic contracts, data tools, reviewer, engine, and adapter.

- [ ] **Step 1: Write the failing v2-only import guard**

```python
# tests/test_v2_only.py
from pathlib import Path


REMOVED_SYMBOLS = {
    "VerificationRuntime",
    "LegacyRuntimeAdapter",
    "VerificationTask",
    "DiagnosisTask",
    "GeneralTask",
    "LegacyTask",
    "legacy_decision",
    "to_legacy_view",
}


def test_production_source_has_no_legacy_runtime_symbols():
    root = Path("src/flight_agent")
    violations = {
        path.as_posix(): sorted(
            symbol for symbol in REMOVED_SYMBOLS
            if symbol in path.read_text("utf-8")
        )
        for path in root.rglob("*.py")
        if any(symbol in path.read_text("utf-8") for symbol in REMOVED_SYMBOLS)
    }
    assert violations == {}
```

Add an import test:

```python
def test_removed_legacy_modules_are_not_importable():
    for name in (
        "flight_agent.verification.compat",
        "flight_agent.verification.runtime",
        "flight_agent.verification.planner",
        "flight_agent.verification.general",
        "flight_agent.verification.diagnosis",
    ):
        assert importlib.util.find_spec(name) is None
```

- [ ] **Step 2: Run the guard and verify it reports current legacy files**

Run:

```bash
python -m pytest tests/test_v2_only.py -q
```

Expected: failure listing legacy symbols and importable modules.

- [ ] **Step 3: Delete legacy modules and their tests**

Delete the production modules listed in this task after confirming `rg` shows no v2
imports.

Delete these legacy test families:

```text
tests/test_verification_runtime.py
tests/test_verification_replanning.py
tests/test_verification_reports.py
tests/test_verification_web.py
tests/test_verification_browser.py
tests/test_verification_tools.py
tests/test_verification_ledger.py
tests/test_verification_evaluation.py
tests/test_verification_output.py
tests/test_general_runtime.py
tests/test_general_web.py
tests/test_general_orchestration.py
tests/test_general_browser.py
tests/test_diagnosis_runtime.py
tests/test_diagnosis_planner.py
tests/test_diagnosis_web.py
tests/test_diagnosis_browser.py
```

Do not delete `tests/test_agent_diagnostic_engine.py`,
`tests/test_diagnosis_contracts.py`, or `tests/test_diagnostic_data.py`.

- [ ] **Step 4: Remove dependencies proven legacy-only**

After running:

```bash
rg "import langchain|from langchain|import langgraph|from langgraph" src tests
```

remove `langchain` and `langgraph` from `pyproject.toml` when no retained import uses
them. Keep:

- `langchain-core`,
- `langchain-openai`,
- `langsmith`,
- `openai`.

Do not generate `uv.lock` in this stage; dependency locking belongs to Stage 6.

- [ ] **Step 5: Rewrite documentation around one runtime**

Documentation must state:

- there is one generic v2 runtime,
- no D1/D2 scenario selection exists,
- Worker and offline diagnostic data are registered resources, not separate products,
- confirmation and execution flow are unchanged,
- old upload/demo and legacy history are unsupported,
- `cleanup-legacy` dry-run and `--apply` behavior,
- supported CLI commands.

Remove claims that old tasks execute under original contracts or project into a
compatibility view.

Rename the focused guides so resource documentation no longer exposes fixed scenario
IDs:

```bash
git mv docs/d1-agent.md docs/simulink-worker.md
git mv docs/d2-agent.md docs/offline-diagnostics.md
```

Update all README and documentation links to the new names.

- [ ] **Step 6: Run import guards and focused generic tests**

Run:

```bash
python -m pytest \
  tests/test_v2_only.py \
  tests/test_agent_contracts.py \
  tests/test_agent_planner.py \
  tests/test_agent_runtime.py \
  tests/test_agent_recovery.py \
  tests/test_agent_web.py \
  tests/test_agent_views.py \
  tests/test_agent_diagnostic_engine.py \
  tests/test_local_matlab_adapters.py \
  tests/test_local_matlab_frequency.py \
  tests/test_verification_worker.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Run the full software-only verification**

Run:

```bash
python -m pytest -m "not matlab" -q
python -m ruff check .
python -m mypy src
python -m build
```

Expected:

- pytest exits 0,
- Ruff exits 0,
- mypy exits 0,
- source and wheel distributions are created successfully.

- [ ] **Step 8: Verify the exact removal boundary**

Run:

```bash
rg \
  "VerificationRuntime|LegacyRuntimeAdapter|VerificationTask|DiagnosisTask|GeneralTask|LegacyTask|scenario.*d1|scenario.*d2" \
  src tests README.md docs
```

Expected: no legacy runtime references. References explaining removed history are
allowed only in the approved design and implementation plan documents.

Run:

```bash
git --no-pager status --short
git --no-pager diff --check
```

Expected: only intended Stage 1 files are changed and no whitespace errors exist.

- [ ] **Step 9: Commit Stage 1 completion**

```bash
git add -A
git commit -m "refactor: remove v1 runtime"
```

The commit body must state that v1 records are intentionally unsupported and that
generic MATLAB, Worker, diagnostic, evaluation, and delivery capabilities remain.

---

## Stage 1 Completion Gate

Do not start the SQLite stage until all of these are true:

- The full software-only suite passes.
- Ruff, mypy, and package build pass.
- No production v1 import or task union remains.
- Generic local MATLAB planning and adapters pass.
- Generic Simulink Worker project execution passes.
- Generic offline diagnosis passes with v2-only checkpoints.
- The generic workbench passes browser tests.
- `cleanup-legacy` deletes only positive v1 records with explicit `--apply`.
- The worktree is clean after the six Stage 1 commits.

The next plan is `docs/superpowers/plans/2026-10-29-sqlite-conversation-store.md`
and must be written only after this gate passes.
