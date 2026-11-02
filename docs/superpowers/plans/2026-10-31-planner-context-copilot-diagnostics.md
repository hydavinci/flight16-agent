# Planner Context and Copilot Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Stage 5 with deterministic, conservative planning-context selection, a hard 120,000-byte context ceiling, locally bound sources and resources, observable planning metrics, and typed, bounded, non-leaking Copilot failures.

**Architecture:** Keep normalization and selection deterministic and separate from inference; retain the complete authorized sources, resources, and catalog locally for binding and validation. Add immutable telemetry contracts and publish one observation per planning operation through a call-local callback, never shared planner state. Translate Copilot process/protocol failures into allowlisted diagnostics at the adapter boundary and persist only those diagnostics and numeric context metrics through the existing conversation store.

**Tech Stack:** Python 3.11+, Pydantic v2, existing LangChain structured-call boundary, GitHub Copilot CLI, pytest, Ruff, mypy; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-29-v2-runtime-optimization-design.md` — “Planning Context Reduction,” “Copilot Diagnostics,” “Error Handling and Recovery,” Stage 5, and their verification requirements.

## Global Constraints

- Preserve the current confirmation, permission, source-attribution, hashing, evidence, recovery, and deterministic-evaluation guarantees.
- The model never receives hidden permissions or gains authority from capability filtering.
- If deterministic filtering cannot prove relevance, it includes the candidate rather than silently making the task impossible.
- Exceeding it produces a specific clarification or capability-gap response; it does not truncate authorized requirements or source quotations silently.
- Copilot execution remains in an empty temporary working directory with denied file, shell, write, URL, and memory tools.
- Diagnostics must not persist prompts, credentials, environment secrets, or unrestricted stderr.
- No automatic inference retry is added. Existing bounded structural correction remains the only second model call.
- Use supported Copilot CLI configuration to prevent unrelated custom MCP or plugin initialization where possible without breaking authenticated access.
- If the installed CLI cannot provide that isolation, record it as an operational limitation rather than modifying user configuration.
- Keep supported Python at 3.11 and newer as declared by the package.
- The context hard limit is **120,000 UTF-8 bytes**, not 122,880 bytes; equality is permitted, one byte over is rejected.
- The six failure categories are exactly `authentication`, `authorization`, `transport`, `timeout`, `invalid_response`, and `process_failure`.
- Only Stage 5 is implemented by this plan. Do not introduce SQLite, schedulers, snapshots, dependency locking, CI, frontend modules, v1 compatibility, or a new document reader.
- All commands and commits below are instructions for a later implementation session. Authoring this plan does not execute them or authorize implementation.

---

## Grounding, Boundaries, and Decisions

The inspected worktree is `/Users/yhe/Work/flight-agent/.worktrees/stages-2-6`, at `89cb7a7` when this plan was authored. Its code already removes v1 but still uses the original conversation-store implementation. Do not assume Stages 2–4 exist merely because of the worktree name. The changes here use the existing `ConversationStore.get/create/save` interface and validated conversation payload, so a preceding SQLite implementation can preserve the same behavior without a new Stage 5 table.

Read these existing implementation anchors before changing them:

| Anchor | Present behavior to preserve |
|---|---|
| `verification/agent_planner.py:1182-1283` | `validate_sources()` and `bind_plan()` verify source identity, substring fidelity, registered resources, versions, effects, readiness, roles, local settings, and metric producers. |
| `verification/agent_planner.py:1286-1383` | `message_sources()`, `_authorized_sources()`, `_redact_context()`, and `_restore_source_quotes()` preserve message provenance and restore exact, complete redacted quotes. |
| `verification/agent_planner.py:1386-1583` | Two calls at most, one shared deadline, source-selection and output-permission corrections; currently a 240,000-byte ceiling. |
| `verification/structured_planner.py:63-111` | `with_timeout()` copies the planner; API calls disable LangSmith tracing; Copilot delegates to `structured_response()`. |
| `copilot.py:22-172` | Lossless whole-schema catalog sharing, an isolated working directory, denied tools, process-group cleanup, and strict JSONL parsing. |
| `verification/coordinator.py:139-197` | The runtime supplies full message sources and resources; the executor alone proposes an unconfirmed baseline. |
| `verification/routes.py:103-128` | `_fail()` currently logs an exception traceback and persists `str(error)`; typed Copilot failures must bypass that generic logging path. |

### Options and trade-offs

1. **Chosen: closed deterministic requests plus explicit complete metadata.** Normalize a deliberately small, fully matched metric-request grammar. Use registered resource roles, declared metric/output kinds, and dependency closure. Keep all candidates for ambiguous requests or incomplete dependency metadata. This is reproducible and cannot silently turn unknown language into exclusion, at the cost of smaller reductions for free-form requests.
2. **Rejected: keyword-ranked top-K or another model selecting context.** These reduce more context, but ranking is not proof of irrelevance, loses dependencies/conflicts, introduces another inference, and violates the conservative fallback and retry requirements.

Source selection uses existing `CriterionSource` objects as whole excerpts: intake already limits each authorized quote to 6,000 characters, and the complete message or source file remains in its existing local location. Do not summarize, split sentences, normalize whitespace, introduce synthetic source IDs, invent a full-document reader, or increase `CriterionSource.quote` limits. Explicit local source-scope declarations may omit an unrelated whole excerpt; an unscoped source is always retained. All user-message excerpts are retained regardless of scope hints.

Capability selection is **not authorization**. Missing resources remain visible as `needs_input`; unavailable relevant capabilities remain visible for an actionable gap. Effects are never added, removed, or inferred by selection. An omitted registered capability returned by the model is still checked against the **full** catalog, never treated as authority gained or lost from the selection.

### Context-size and telemetry semantics

- Measure `json.dumps(context, ensure_ascii=False, allow_nan=False).encode("utf-8")`, matching the API planner's current serialization. The measured logical `request_context` includes redacted resources, selected catalog/sources, unchanged request/history, observations, source selections, and correction feedback.
- Measure before Copilot's existing lossless schema sharing. Sharing cannot bypass the ceiling. The transport's system instruction/schema envelope is not the logical context being limited.
- `serialized_bytes` is the maximum measured logical context size during the operation, including an oversized attempted correction.
- `correction_round_count` is `0` or `1`, incremented immediately before the second inference, not merely when feedback is prepared.
- `planning_duration_seconds` measures the complete operation, including normalization, selection, inference, and local validation. Every return or exception publishes one telemetry value if a callback was supplied.
- Full source files/messages are retained by their existing owners. No prompt, source-document copy, raw response, stderr, credentials, or environment dump is added to telemetry storage.

### File structure

| File | Responsibility |
|---|---|
| Create `src/flight_agent/runtime/planning_contracts.py` | Immutable metrics, six-category diagnostics, and one-operation telemetry payload. No process or persistence imports. |
| Modify `src/flight_agent/runtime/capability_metadata.py` | Optional, complete output/dependency selection declarations. Omission means unknown, not empty. |
| Modify `src/flight_agent/runtime/capabilities.py` | Retain selection declarations in registration/catalog output, but exclude them from execution snapshots and baseline fingerprints. |
| Modify `src/flight_agent/verification/adapters/{local_matlab,simulink,diagnostics,evaluation,delivery}.py` | Declare each built-in's output kinds and dependency behavior without changing execution contracts. |
| Create `src/flight_agent/verification/planning_context.py` | Closed request normalization, candidate closure, source-scope selection, byte measurement, and call-local metric accumulator. |
| Modify `src/flight_agent/verification/agent_planner.py` | Integrate selection and metrics, reduce the ceiling, preserve full local binding, and retain typed provider failures through correction. |
| Create `src/flight_agent/copilot_diagnostics.py` | Bounded classification, hashed provider request IDs, fixed safe summaries, and typed errors. |
| Modify `src/flight_agent/copilot.py` | Adapt process outcomes, JSONL/schema failures, and temporary-profile isolation to typed diagnostics. Keep schema sharing lossless. |
| Modify `src/flight_agent/verification/{contracts,coordinator,routes}.py` | Persist and expose allowlisted telemetry; log provider failures without raw exception chains. |
| Create `tests/test_planning_context.py`, `tests/test_copilot_diagnostics.py`, `tests/test_planning_telemetry.py` | Deterministic-selection, classifier, and persistence/privacy tests. |
| Modify `tests/test_agent_planner.py`, `tests/test_copilot.py` | Integration, quote/permission, deadline, correction, and subprocess regression tests. |
| Modify `docs/architecture.md`, `docs/usage.md` | Explain the ceiling, observable fields, authentication/isolation limitation, and no-retry behavior. |

Use the existing test interpreter:

```bash
cd /Users/yhe/Work/flight-agent/.worktrees/stages-2-6
export PATH="/Users/yhe/Work/flight-agent/.venv-matlab/bin:$PATH"
python --version
```

Expected: an installed Python at least 3.11. The inspected interpreter was Python 3.12.14; do not install dependencies or require `uv.lock` for this stage.

All imports shown beside test examples belong in the destination file's top import block. Merge duplicate imports rather than appending imports between test functions. New production modules shown as complete modules keep their displayed imports.

### Task 1: Declare Selection Metadata and Safe Telemetry Contracts

**Files:**
- Create: `src/flight_agent/runtime/planning_contracts.py`
- Modify: `src/flight_agent/runtime/capability_metadata.py:1-63`
- Modify: `src/flight_agent/runtime/capabilities.py:80-84,132-145,191-214`
- Modify: `src/flight_agent/verification/adapters/local_matlab.py:286-425`
- Modify: `src/flight_agent/verification/adapters/simulink.py:294-339`
- Modify: `src/flight_agent/verification/adapters/diagnostics.py:280-319`
- Modify: `src/flight_agent/verification/adapters/evaluation.py:165-180`
- Modify: `src/flight_agent/verification/adapters/delivery.py:291-316`
- Create/Test: `tests/test_planning_context.py`
- Create/Test: `tests/test_copilot_diagnostics.py`

**Interfaces:**
- Consumes: `CapabilityMetadata`, `MetricDefinition`, `AgentContract`, `Name`, and existing capability factories.
- Produces: `CapabilityPlanning(output_kinds, depends_on=[], dynamic_dependencies=False)` and `CapabilityMetadata.planning: CapabilityPlanning | None`, defaulting to `None` and excluded from execution serialization.
- Produces: `PlanningMetrics(serialized_bytes=0, selected_capability_count=0, selected_source_excerpt_count=0, planning_duration_seconds=0.0, correction_round_count=0)`.
- Produces: `CopilotFailureCategory`, `CopilotDiagnostics(category, redacted_request_id, exit_code, elapsed_seconds, response_bytes, summary)`.
- Produces: `PlanningTelemetry(metrics: PlanningMetrics, failure: CopilotDiagnostics | None = None)`.

- [ ] **Step 1: Write failing contract tests**

```python
# tests/test_planning_context.py
import pytest
from pydantic import ValidationError

from flight_agent.runtime.capability_metadata import CapabilityMetadata, CapabilityPlanning
from flight_agent.runtime.planning_contracts import PlanningMetrics


def test_missing_planning_declaration_is_unknown_not_an_empty_capability():
    assert CapabilityMetadata().planning is None
    metadata = CapabilityMetadata(planning=CapabilityPlanning(output_kinds=["metric"]))
    assert metadata.planning.depends_on == []


def test_planning_contracts_reject_duplicate_and_unbounded_values():
    with pytest.raises(ValidationError):
        CapabilityPlanning(output_kinds=["metric", "metric"])
    with pytest.raises(ValidationError):
        CapabilityPlanning(output_kinds=["metric"], depends_on=["prepare", "prepare"])
    with pytest.raises(ValidationError):
        PlanningMetrics(planning_duration_seconds=float("nan"))
    with pytest.raises(ValidationError):
        PlanningMetrics(correction_round_count=2)
```

```python
# tests/test_copilot_diagnostics.py
import pytest
from pydantic import ValidationError

from flight_agent.runtime.planning_contracts import CopilotDiagnostics, SAFE_SUMMARIES


def test_diagnostics_allow_only_six_safe_fields():
    diagnostic = CopilotDiagnostics(
        category="transport", redacted_request_id=None, exit_code=7,
        elapsed_seconds=0.25, response_bytes=12, summary=SAFE_SUMMARIES["transport"],
    )
    assert set(diagnostic.model_dump()) == {
        "category", "redacted_request_id", "exit_code",
        "elapsed_seconds", "response_bytes", "summary",
    }
    for extra in ("prompt", "stderr", "environment", "credentials", "stdout"):
        with pytest.raises(ValidationError):
            CopilotDiagnostics.model_validate({**diagnostic.model_dump(), extra: "private"})
    with pytest.raises(ValidationError):
        CopilotDiagnostics.model_validate({
            **diagnostic.model_dump(), "summary": "token=private-provider-text",
        })
    with pytest.raises(ValidationError):
        CopilotDiagnostics.model_validate({
            **diagnostic.model_dump(), "redacted_request_id": "raw-provider-id",
        })
```

- [ ] **Step 2: Run tests to verify missing interfaces fail**

Run: `python -m pytest tests/test_planning_context.py tests/test_copilot_diagnostics.py -q`

Expected: collection fails for missing `CapabilityPlanning` and `runtime.planning_contracts`; no provider calls.

- [ ] **Step 3: Add the minimal contracts and built-in declarations**

Add to `capability_metadata.py`, importing `StrictBool`, and add `planning` to `CapabilityMetadata`:

```python
OutputKind = Literal["metric", "finding", "table", "artifact"]


class CapabilityPlanning(AgentContract):
    output_kinds: Annotated[list[OutputKind], Field(min_length=1, max_length=4)]
    depends_on: Annotated[list[Name], Field(max_length=80)] = []
    dynamic_dependencies: StrictBool = False

    @model_validator(mode="after")
    def unique_selection_declarations(self) -> Self:
        if len(set(self.output_kinds)) != len(self.output_kinds):
            raise ValueError("Planning output kinds must be unique")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Planning dependencies must be unique")
        return self
```

The exact new field is:

```python
planning: CapabilityPlanning | None = Field(default=None, exclude=True)
```

Selection metadata must not change previously confirmed baseline fingerprints or make live capabilities differ from their confirmed execution snapshots. The field is catalog-only: `CapabilityMetadata.model_dump()` omits it whether present or absent. In `CapabilityRegistry.register()`, replace the existing metadata cloning expression with:

```python
metadata = (
    CapabilityMetadata.model_validate({
        **capability.metadata.model_dump(mode="json"),
        "planning": capability.metadata.planning,
    })
    if capability.metadata is not None
    else None
)
```

In `CapabilityRegistry.catalog()`, replace the existing metadata entry with:

```python
"metadata": {
    **entry.metadata.model_dump(mode="json"),
    "planning": (
        entry.metadata.planning.model_dump(mode="json")
        if entry.metadata.planning is not None else None
    ),
},
```

Leave `CapabilityRegistry.snapshot()` unchanged: its existing serialization/revalidation deliberately drops the planning-only field. Add this regression test to `tests/test_planning_context.py`:

```python
def test_catalog_only_metadata_does_not_change_execution_snapshots():
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.runtime.capability_metadata import CapabilitySnapshot
    from flight_agent.runtime.contracts import fingerprint
    from flight_agent.verification.diagnostic_planner import DiagnosticReviewer
    from flight_agent.verification.plugins import register_builtin_plugins
    from flight_agent.verification.resources import ResourceRegistry

    tools = CapabilityRegistry(agent_mode=True)
    register_builtin_plugins(
        tools, ResourceRegistry(), backend=None, diagnostic_reviewer=DiagnosticReviewer(),
    )
    catalog = {entry["name"]: entry for entry in tools.catalog()}
    assert catalog["matlab_stepinfo"]["metadata"]["planning"]["depends_on"] == [
        "matlab_execute_project",
    ]
    snapshot = tools.snapshot("matlab_stepinfo")
    old_payload = snapshot.model_dump(mode="json")
    assert "planning" not in old_payload["metadata"]
    restored = CapabilitySnapshot.model_validate(old_payload)
    assert fingerprint(restored.model_dump(mode="json")) == fingerprint(old_payload)
    assert restored == snapshot
```

Create `runtime/planning_contracts.py`:

```python
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .resources import AgentContract

CopilotFailureCategory = Literal[
    "authentication", "authorization", "transport", "timeout",
    "invalid_response", "process_failure",
]

SAFE_SUMMARIES: dict[CopilotFailureCategory, str] = {
    "authentication": "Copilot CLI authentication unavailable; check isolated login access.",
    "authorization": "Copilot CLI account or model access denied; not retried.",
    "transport": "Copilot CLI connection, proxy, DNS, or WebSocket failure; not retried.",
    "timeout": "Copilot CLI timed out; not retried.",
    "invalid_response": (
        "Copilot CLI returned invalid JSON, tool activity, or an incomplete plan/schema response."
    ),
    "process_failure": "Copilot CLI process failed; not retried.",
}


class PlanningMetrics(AgentContract):
    serialized_bytes: Annotated[int, Field(strict=True, ge=0)] = 0
    selected_capability_count: Annotated[int, Field(strict=True, ge=0)] = 0
    selected_source_excerpt_count: Annotated[int, Field(strict=True, ge=0)] = 0
    planning_duration_seconds: Annotated[
        float, Field(ge=0, allow_inf_nan=False)
    ] = 0.0
    correction_round_count: Annotated[int, Field(strict=True, ge=0, le=1)] = 0


class CopilotDiagnostics(AgentContract):
    category: CopilotFailureCategory
    redacted_request_id: Annotated[
        str, Field(pattern=r"^sha256:[a-f0-9]{16}$")
    ] | None
    exit_code: Annotated[int, Field(strict=True)] | None
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    response_bytes: Annotated[int, Field(strict=True, ge=0)]
    summary: Annotated[str, Field(min_length=1, max_length=320)]

    @model_validator(mode="after")
    def allowlisted_summary(self) -> Self:
        if self.summary != SAFE_SUMMARIES[self.category]:
            raise ValueError("Diagnostics require the category's fixed safe summary")
        return self


class PlanningTelemetry(AgentContract):
    metrics: PlanningMetrics
    failure: CopilotDiagnostics | None = None
```

Import `CapabilityPlanning` in each adapter. Insert the exact declaration from this table in that capability's existing `CapabilityMetadata(...)`; do not change schemas, effects, readiness, or computation:

| Capability | `planning=` expression |
|---|---|
| `matlab_execute_project` | `CapabilityPlanning(output_kinds=["artifact"])` |
| `matlab_stepinfo` | `CapabilityPlanning(output_kinds=["metric", "artifact"], depends_on=["matlab_execute_project"])` |
| `matlab_linearize` | `CapabilityPlanning(output_kinds=["artifact"])` |
| `matlab_allmargin` | `CapabilityPlanning(output_kinds=["metric", "artifact"], depends_on=["matlab_linearize"])` |
| `simulink_step_response` | `CapabilityPlanning(output_kinds=["metric", "artifact"])` |
| `simulink_stability_margins` | `CapabilityPlanning(output_kinds=["metric", "artifact"])` |
| `offline_diagnosis` | `CapabilityPlanning(output_kinds=["finding", "table", "artifact"])` |
| `evaluate_criteria` | `CapabilityPlanning(output_kinds=["finding"], dynamic_dependencies=True)` |
| `deliver_results` | `CapabilityPlanning(output_kinds=["artifact"], dynamic_dependencies=True)` |

These are complete **requestable output-kind** declarations for selection, not a change to `CapabilityOutcome` or a claim that every successful call must emit every listed kind. Existing metric declarations enumerate selectable metrics. Static `depends_on` means “keep these potential producers in context,” not “insert these nodes into a plan.” Dynamic dependencies force inclusion of the entire catalog if that capability becomes a selected candidate.

- [ ] **Step 4: Run contract and existing registration tests**

Run: `python -m pytest tests/test_planning_context.py tests/test_copilot_diagnostics.py tests/test_agent_capabilities.py tests/test_agent_adapters.py tests/test_agent_recovery.py -q`

Expected: PASS; existing capability snapshots remain valid, and absent optional metadata stays conservative.

- [ ] **Step 5: Commit the independently testable contracts**

```bash
git add src/flight_agent/runtime/planning_contracts.py src/flight_agent/runtime/capability_metadata.py src/flight_agent/runtime/capabilities.py src/flight_agent/verification/adapters/local_matlab.py src/flight_agent/verification/adapters/simulink.py src/flight_agent/verification/adapters/diagnostics.py src/flight_agent/verification/adapters/evaluation.py src/flight_agent/verification/adapters/delivery.py tests/test_planning_context.py tests/test_copilot_diagnostics.py
git commit -m "feat: declare conservative planner metadata and safe telemetry"
```

### Task 2: Implement Deterministic Candidate and Whole-Excerpt Selection

**Files:**
- Create: `src/flight_agent/verification/planning_context.py`
- Modify/Test: `tests/test_planning_context.py`

**Interfaces:**
- Consumes: Task 1 contracts; existing `Conversation`, `CriterionSource`, `ResourceReference`, `TaskRequirements` output-kind vocabulary.
- Produces: `PlanningIntent(metric_ids: frozenset[str] | None)`; `None` means open/uncertain scope.
- Produces: `SourceScope(metric_ids: list[Name], capability_names: list[Name])`; declarations are complete alternatives, not authorization.
- Produces: `ContextSelection(catalog: tuple[dict[str, object], ...], sources: tuple[CriterionSource, ...])`.
- Produces: `normalize_request(message: str, conversation: Conversation, catalog: list[dict[str, object]]) -> PlanningIntent`.
- Produces: `select_context(message: str, conversation: Conversation, *, catalog: list[dict[str, object]], resources: dict[str, ResourceReference], sources: dict[str, CriterionSource]) -> ContextSelection`.
- Produces: `PlanningState` and `context_byte_size(context: dict[str, object]) -> int`.

- [ ] **Step 1: Add failing deterministic-selection tests**

Append these imports and helpers to `tests/test_planning_context.py`:

```python
from copy import deepcopy

from flight_agent.runtime.requirements import CriterionSource
from flight_agent.runtime.resources import ResourceReference
from flight_agent.verification.contracts import Conversation, ConversationMessage
from flight_agent.verification.planning_context import select_context


def selection_catalog():
    def item(name, metric_ids, kinds, dependencies=(), dynamic=False, roles=()):
        return {
            "name": name, "version": "1", "effect": "read_only",
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
            "metadata": {
                "metrics": [{"id": metric, "label": metric, "unit": "m"}
                            for metric in metric_ids],
                "resources": list(roles),
                "planning": {
                    "output_kinds": list(kinds), "depends_on": list(dependencies),
                    "dynamic_dependencies": dynamic,
                },
            },
            "availability": {"state": "available", "reason": ""},
        }
    return [
        item("execute", (), ("artifact",)),
        item("rmse", ("rmse",), ("metric",), ("execute",)),
        item("margin", ("phase_margin",), ("metric",)),
        item("deliver", (), ("artifact",), dynamic=True),
    ]


def selection_conversation(message="Report rmse."):
    return Conversation(
        id="a" * 32, created_at="2026-10-31", updated_at="2026-10-31",
        messages=[ConversationMessage(role="user", content=message, created_at="2026-10-31")],
    )


def test_selection_is_order_independent_and_keeps_dependency_producers():
    catalog = selection_catalog()
    original = deepcopy(catalog)
    record = selection_conversation()
    first = select_context(
        "Report rmse.", record, catalog=catalog, resources={}, sources={},
    )
    second = select_context(
        "Report rmse.", record, catalog=list(reversed(catalog)), resources={}, sources={},
    )
    assert first == second
    assert [entry["name"] for entry in first.catalog] == ["execute", "rmse"]
    assert catalog == original


@pytest.mark.parametrize("request", [
    "Report rmse and explain every issue.", "Do the usual analysis.",
    "Report a_new_metric.", "Report rmse; deliver Word.", "不要只 Report rmse.",
])
def test_uncertain_or_unrecognized_request_keeps_every_capability(request):
    catalog = selection_catalog()
    result = select_context(
        request, selection_conversation(request), catalog=catalog, resources={}, sources={},
    )
    assert {entry["name"] for entry in result.catalog} == {entry["name"] for entry in catalog}


def test_unknown_dependency_metadata_keeps_the_entire_catalog():
    catalog = selection_catalog()
    catalog[2]["metadata"].pop("planning")
    result = select_context(
        "Report rmse.", selection_conversation(), catalog=catalog, resources={}, sources={},
    )
    assert len(result.catalog) == len(catalog)


def test_missing_and_unavailable_resources_do_not_hide_a_relevant_capability():
    catalog = selection_catalog()
    catalog[1]["metadata"]["resources"] = [{
        "role": "data", "kind": "signal_data", "location": "agent", "required": True,
    }]
    catalog[1]["availability"] = {"state": "needs_input", "reason": "Missing resource role: data"}
    for state in ("needs_input", "unavailable"):
        catalog[1]["availability"]["state"] = state
        result = select_context(
            "Report rmse.", selection_conversation(), catalog=catalog, resources={}, sources={},
        )
        selected = {entry["name"]: entry for entry in result.catalog}
        assert selected["rmse"]["availability"]["state"] == state
```

Add a source test that proves selection is not destruction:

```python
def test_explicit_unrelated_excerpt_is_omitted_but_originals_and_unknowns_are_retained():
    record = selection_conversation()
    reference = f"message:{record.id}:0"
    sources = {
        reference: CriterionSource(
            kind="user_message", reference=reference, quote="Report rmse.",
        ),
        "standard#rmse": CriterionSource(
            kind="standard_file", reference="standard#rmse",
            quote="  RMSE <= 2 m.\n", sha256="b" * 64, version="edition-1",
        ),
        "standard#margin": CriterionSource(
            kind="standard_file", reference="standard#margin",
            quote="Phase margin >= 45 deg.", sha256="b" * 64, version="edition-1",
        ),
        "standard#unknown": CriterionSource(
            kind="standard_file", reference="standard#unknown",
            quote="Keep this unclassified conflicting requirement.",
            sha256="b" * 64, version="edition-1",
        ),
    }
    resource = ResourceReference(
        id="standard", kind="standard", location="agent", locator="/private/standard.txt",
        fingerprint="b" * 64, version="edition-1",
        metadata={"planning_source_scopes": {
            "standard#rmse": {"metric_ids": ["rmse"], "capability_names": []},
            "standard#margin": {"metric_ids": ["phase_margin"], "capability_names": []},
        }},
    )
    original = deepcopy(sources)
    result = select_context(
        "Report rmse.", record, catalog=selection_catalog(),
        resources={"standard": resource}, sources=sources,
    )
    assert {item.reference for item in result.sources} == {
        reference, "standard#rmse", "standard#unknown",
    }
    assert next(item.quote for item in result.sources if item.reference == "standard#rmse") == (
        "  RMSE <= 2 m.\n"
    )
    assert sources == original
    assert sources["standard#margin"].quote == "Phase margin >= 45 deg."
```

- [ ] **Step 2: Run tests to verify the selector is missing**

Run: `python -m pytest tests/test_planning_context.py -q`

Expected: collection fails for missing `verification.planning_context`.

- [ ] **Step 3: Implement the complete deterministic selector**

Create `verification/planning_context.py` with the following code. Optional source-scope hints are read only from the registered resource owning that reference; malformed hints mean “unknown/keep.” They are never accepted as a new requirement, permission, source, resource, or executable node.

```python
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import Field, ValidationError, model_validator

from ..runtime.capability_metadata import CapabilityMetadata
from ..runtime.contracts import Name
from ..runtime.requirements import CriterionSource
from ..runtime.resources import AgentContract, ResourceReference
from .contracts import Conversation


@dataclass(frozen=True)
class PlanningIntent:
    metric_ids: frozenset[str] | None


class SourceScope(AgentContract):
    metric_ids: Annotated[list[Name], Field(max_length=200)] = []
    capability_names: Annotated[list[Name], Field(max_length=80)] = []

    @model_validator(mode="after")
    def complete_nonempty_scope(self) -> Self:
        if not self.metric_ids and not self.capability_names:
            raise ValueError("Empty source scope cannot prove irrelevance")
        if len(set(self.metric_ids)) != len(self.metric_ids):
            raise ValueError("Source metric scope must be unique")
        if len(set(self.capability_names)) != len(self.capability_names):
            raise ValueError("Source capability scope must be unique")
        return self


@dataclass(frozen=True)
class ContextSelection:
    catalog: tuple[dict[str, object], ...]
    sources: tuple[CriterionSource, ...]


@dataclass
class PlanningState:
    started_at: float
    serialized_bytes: int = 0
    selected_capability_count: int = 0
    selected_source_excerpt_count: int = 0
    correction_round_count: int = 0


def context_byte_size(context: dict[str, object]) -> int:
    return len(json.dumps(context, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def normalize_request(
    message: str, conversation: Conversation, catalog: list[dict[str, object]]
) -> PlanningIntent:
    user_messages = [item.content for item in conversation.messages if item.role == "user"]
    if conversation.task is not None or len(user_messages) > 1:
        return PlanningIntent(None)
    if user_messages and user_messages[0] != message:
        return PlanningIntent(None)
    match = re.fullmatch(
        r"(?:Report|Compute|Analyze|报告|计算|分析)\s+([^.!。\n]+)[.!。]?",
        message.strip(), re.IGNORECASE,
    )
    if match is None:
        return PlanningIntent(None)
    aliases: dict[str, set[str]] = {}
    for item in catalog:
        metadata = CapabilityMetadata.model_validate(item.get("metadata", {}))
        for metric in metadata.metrics:
            for alias in (metric.id, metric.label):
                aliases.setdefault(alias.casefold(), set()).add(metric.id)
    identifiers: set[str] = set()
    for token in re.split(r"\s+(?:and|和)\s+|[,、]", match[1], flags=re.IGNORECASE):
        options = aliases.get(token.strip().casefold(), set())
        if len(options) != 1:
            return PlanningIntent(None)
        identifiers.update(options)
    return PlanningIntent(frozenset(identifiers) if identifiers else None)


def _candidate_names(
    intent: PlanningIntent, catalog: list[dict[str, object]],
) -> set[str]:
    entries = {str(item["name"]): item for item in catalog}
    if len(entries) != len(catalog):
        raise ValueError("Capability catalog contains duplicate names")
    metadata = {
        name: CapabilityMetadata.model_validate(item.get("metadata", {}))
        for name, item in entries.items()
    }
    all_names = set(entries)
    if intent.metric_ids is None or any(item.planning is None for item in metadata.values()):
        return all_names
    selected = {
        name for name, item in metadata.items()
        if item.planning is not None and "metric" in item.planning.output_kinds
        and intent.metric_ids.intersection(metric.id for metric in item.metrics)
    }
    if not intent.metric_ids <= {
        metric.id for name in selected for metric in metadata[name].metrics
    }:
        return all_names
    pending = list(sorted(selected))
    while pending:
        name = pending.pop()
        declaration = metadata[name].planning
        if declaration is None or declaration.dynamic_dependencies:
            return all_names
        for dependency in declaration.depends_on:
            if dependency not in entries:
                return all_names
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def _resource_rank(
    item: dict[str, object], resources: dict[str, ResourceReference]
) -> tuple[int, str, str]:
    metadata = CapabilityMetadata.model_validate(item.get("metadata", {}))
    missing = sum(
        1 for role in metadata.resources
        if role.required and not any(
            resource.kind == role.kind
            and (role.location is None or resource.location == role.location)
            for resource in resources.values()
        )
    )
    return missing, str(item["name"]), str(item["version"])


def _source_scope(
    source: CriterionSource, resources: dict[str, ResourceReference]
) -> SourceScope | None:
    owner = resources.get(source.reference.split("#", 1)[0])
    if owner is None:
        return None
    hints = owner.metadata.get("planning_source_scopes")
    if not isinstance(hints, dict) or source.reference not in hints:
        return None
    try:
        return SourceScope.model_validate(hints[source.reference])
    except ValidationError:
        return None


def select_context(
    message: str, conversation: Conversation, *,
    catalog: list[dict[str, object]], resources: dict[str, ResourceReference],
    sources: dict[str, CriterionSource],
) -> ContextSelection:
    intent = normalize_request(message, conversation, catalog)
    selected = _candidate_names(intent, catalog)
    known_metrics = {
        metric.id for item in catalog
        for metric in CapabilityMetadata.model_validate(item.get("metadata", {})).metrics
    }
    known_names = {str(item["name"]) for item in catalog}
    # A full-catalog fallback is not proof that any associated excerpt is irrelevant.
    metric_scope = intent.metric_ids if selected != known_names else None
    excerpts: list[CriterionSource] = []
    for reference in sorted(sources):
        source = sources[reference]
        scope = _source_scope(source, resources)
        uncertain_scope = (
            scope is None
            or not set(scope.metric_ids) <= known_metrics
            or not set(scope.capability_names) <= known_names
        )
        if (
            metric_scope is None or source.kind == "user_message" or uncertain_scope
            or scope is not None and (
                bool(metric_scope.intersection(scope.metric_ids))
                or bool(selected.intersection(scope.capability_names))
            )
        ):
            excerpts.append(CriterionSource.model_validate(source.model_dump(mode="json")))
    return ContextSelection(
        catalog=tuple(deepcopy(item) for item in sorted(
            catalog, key=lambda entry: _resource_rank(entry, resources)
        ) if str(item["name"]) in selected),
        sources=tuple(excerpts),
    )
```

Add these concrete closure/uncertainty cases to the same test file:

```python
@pytest.mark.parametrize("case", ["missing-dependency", "cycle", "dynamic", "multi-metric"])
def test_dependency_closure_never_drops_required_context(case):
    catalog = selection_catalog()
    request = "Report rmse."
    expected = {"execute", "rmse"}
    if case == "missing-dependency":
        catalog[1]["metadata"]["planning"]["depends_on"] = ["not_registered"]
        expected = {entry["name"] for entry in catalog}
    elif case == "cycle":
        catalog[0]["metadata"]["planning"]["depends_on"] = ["rmse"]
    elif case == "dynamic":
        catalog[1]["metadata"]["planning"]["dynamic_dependencies"] = True
        expected = {entry["name"] for entry in catalog}
    else:
        request = "Report rmse and phase_margin."
        expected.add("margin")
    result = select_context(
        request, selection_conversation(request), catalog=catalog, resources={}, sources={},
    )
    assert {entry["name"] for entry in result.catalog} == expected


def test_followup_scope_is_not_reduced_to_the_last_message():
    record = selection_conversation("Report phase_margin.")
    record.messages.append(ConversationMessage(
        role="user", content="Report rmse.", created_at="2026-10-31",
    ))
    result = select_context(
        "Report rmse.", record, catalog=selection_catalog(), resources={}, sources={},
    )
    assert len(result.catalog) == 4


def test_source_selection_never_rewrites_the_complete_local_document(tmp_path):
    import hashlib

    text = "  RMSE <= 2 m.\n" + ("Unclassified original text.\n" * 1000)
    text += "Phase margin >= 45 deg.\n"
    document = tmp_path / "standard.txt"
    document.write_text(text, encoding="utf-8")
    before = document.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    resource = ResourceReference(
        id="standard", kind="standard", location="agent", locator=str(document),
        fingerprint=digest, version="edition-1",
        metadata={"planning_source_scopes": {
            "standard#margin": {"metric_ids": ["phase_margin"], "capability_names": []},
        }},
    )
    excerpt = CriterionSource(
        kind="standard_file", reference="standard#margin",
        quote="Phase margin >= 45 deg.", sha256=digest, version="edition-1",
    )
    result = select_context(
        "Report rmse.", selection_conversation(), catalog=selection_catalog(),
        resources={"standard": resource}, sources={excerpt.reference: excerpt},
    )
    assert result.sources == ()
    assert document.read_bytes() == before
    assert excerpt.quote in document.read_text("utf-8")
```

- [ ] **Step 4: Run selector tests**

Run: `python -m pytest tests/test_planning_context.py tests/test_agent_capabilities.py -q`

Expected: PASS; deterministic ordering, unknown-metadata fallback, dependency closure, role/readiness retention, unrelated excerpt omission, and untouched original quotes are all exercised.

- [ ] **Step 5: Commit the pure selector**

```bash
git add src/flight_agent/verification/planning_context.py tests/test_planning_context.py
git commit -m "feat: select planner capabilities and source excerpts conservatively"
```

### Task 3: Integrate Context Selection, Local Binding, Metrics, and the Hard Ceiling

**Files:**
- Modify: `src/flight_agent/verification/agent_planner.py:1-30,1386-1583`
- Modify/Test: `tests/test_agent_planner.py`
- Modify/Test: `tests/test_planning_context.py`

**Interfaces:**
- Consumes: Task 2 `select_context()`, `PlanningState`, and `context_byte_size()`; Task 1 telemetry contracts.
- Preserves: `bind_plan(draft: AgentPlanDraft, resources: dict[str, ResourceReference], sources: dict[str, CriterionSource], catalog: list[dict[str, object]]) -> AgentPlan`.
- Preserves: `_authorized_sources()`, `_source_selections()`, `_restore_source_quotes()`, `AgentDecision`, and existing correction branches.
- Produces: `AgentPlanner.propose(message: str, conversation: Conversation, *, catalog: list[dict[str, object]], resources: dict[str, ResourceReference], sources: dict[str, CriterionSource], observe: Callable[[PlanningTelemetry], None] | None = None) -> AgentDecision`.
- Produces private helper: `AgentPlanner._propose(message: str, conversation: Conversation, *, catalog: list[dict[str, object]], resources: dict[str, ResourceReference], sources: dict[str, CriterionSource], state: PlanningState) -> AgentDecision`.
- Produces: module-level `MAX_CONTEXT_BYTES = 120_000`, retained in `agent_planner.py` so existing monkeypatches still target the right symbol.

- [ ] **Step 1: Add failing integration and byte-boundary tests**

Append to `tests/test_agent_planner.py`:

```python
from flight_agent.runtime.capability_metadata import CapabilityPlanning
from flight_agent.verification.planning_context import context_byte_size


def test_planner_exposes_numeric_metrics_without_exposing_or_mutating_context(tmp_path):
    planner, _, conversation, tools, calls, model = setup(
        tmp_path, {"action": "clarify", "text": "Provide arrays."},
    )
    observed = []
    catalog = tools.catalog()
    for entry in catalog:
        entry["metadata"]["planning"] = CapabilityPlanning(
            output_kinds=["metric"] if entry["name"] == "offline_rmse" else ["finding"],
        ).model_dump(mode="json")
    decision = planner.propose(
        "Report RMSE.", conversation, catalog=catalog, resources={}, sources={},
        observe=observed.append,
    )
    assert decision.action == "clarify"
    payload = json.loads(model._seen[0][-1].content)
    assert [entry["name"] for entry in payload["catalog"]] == ["offline_rmse"]
    assert len(observed) == 1
    metrics = observed[0].metrics
    assert metrics.serialized_bytes == context_byte_size(payload)
    assert metrics.selected_capability_count == 1
    assert metrics.selected_source_excerpt_count == 0
    assert metrics.correction_round_count == 0
    assert metrics.planning_duration_seconds >= 0
    assert observed[0].failure is None
    assert "request" not in observed[0].model_dump()
    assert calls == []


@pytest.mark.parametrize("limit_delta,expected_calls", [(0, 1), (-1, 0)])
def test_context_exact_utf8_boundary_is_allowed_but_one_byte_over_clarifies(
    tmp_path, monkeypatch, limit_delta, expected_calls,
):
    planner, _, record, tools, calls, model = setup(
        tmp_path, {"action": "clarify", "text": "提供输入。"}, "分析未知指标。",
    )
    observed = []
    planner.propose(
        "分析未知指标。", record, catalog=tools.catalog(), resources={}, sources={},
        observe=observed.append,
    )
    size = observed[0].metrics.serialized_bytes
    model._seen.clear()
    monkeypatch.setattr(module(), "MAX_CONTEXT_BYTES", size + limit_delta)
    result = planner.propose(
        "分析未知指标。", record, catalog=tools.catalog(), resources={}, sources={},
    )
    assert result.action == "clarify"
    assert len(model._seen) == expected_calls
    assert calls == []


def test_real_120000_byte_limit_rejects_context_without_cutting_last_requirement(tmp_path):
    planner, store, record, tools, calls, model = setup(
        tmp_path, {"action": "answer", "text": "Must not run."},
    )
    for index in range(30):
        record.messages.append(ConversationMessage(
            role="user", content=("中" * 3000) + f" final criterion {index}",
            created_at=now(),
        ))
    before = record.model_dump(mode="json")
    observed = []
    result = planner.propose(
        "Report RMSE.", record, catalog=tools.catalog(), resources={}, sources={},
        observe=observed.append,
    )
    assert module().MAX_CONTEXT_BYTES == 120_000
    assert result.action == "clarify" and result.plan is None
    assert "120" in result.text and "未截断" in result.text
    assert observed[0].metrics.serialized_bytes > 120_000
    assert record.model_dump(mode="json") == before
    assert "final criterion 29" in record.messages[-1].content
    assert model._seen == [] and calls == []
    assert store.get(record.id).task is None


def test_actual_120000_byte_boundary_not_122880_is_enforced(tmp_path):
    planner, _, record, tools, calls, model = setup(
        tmp_path, {"action": "clarify", "text": "Supply arrays."},
    )
    catalog = tools.catalog()
    observed = []
    planner.propose(
        "Report RMSE.", record, catalog=catalog, resources={}, sources={},
        observe=observed.append,
    )
    catalog[0]["description"] += "x" * (120_000 - observed[0].metrics.serialized_bytes)
    model._seen.clear()
    observed.clear()
    exact = planner.propose(
        "Report RMSE.", record, catalog=catalog, resources={}, sources={},
        observe=observed.append,
    )
    assert exact.action == "clarify"
    assert observed[0].metrics.serialized_bytes == 120_000
    assert len(model._seen) == 1
    catalog[0]["description"] += "x"
    model._seen.clear()
    observed.clear()
    over = planner.propose(
        "Report RMSE.", record, catalog=catalog, resources={}, sources={},
        observe=observed.append,
    )
    assert over.action == "clarify" and "未截断" in over.text
    assert observed[0].metrics.serialized_bytes == 120_001
    assert model._seen == [] and calls == []


def test_selected_model_excerpts_do_not_replace_full_local_binding_inputs(tmp_path, monkeypatch):
    from copy import deepcopy

    planner, _, record, tools, calls, model = setup(
        tmp_path, {"action": "plan", "text": "Confirm analysis.", "plan": draft()},
    )
    catalog = tools.catalog()
    for entry in catalog:
        entry["metadata"]["planning"] = {
            "output_kinds": ["metric"] if entry["name"] == "offline_rmse" else ["finding"],
            "depends_on": [], "dynamic_dependencies": False,
        }
    unrelated = deepcopy(catalog[0])
    unrelated["name"] = "phase_analysis"
    unrelated["metadata"]["metrics"] = [
        {"id": "phase_margin", "label": "Phase margin", "unit": "deg"},
    ]
    catalog.append(unrelated)
    resource = ResourceReference.model_validate(resource_data(
        id="standard", kind="standard", location="agent", version="edition-1",
        metadata={"planning_source_scopes": {
            "standard#margin": {"metric_ids": ["phase_margin"], "capability_names": []},
        }},
    ))
    excerpt = CriterionSource(
        kind="standard_file", reference="standard#margin", quote="Phase margin >= 45 deg.",
        sha256=resource.fingerprint, version=resource.version,
    )
    sources = {excerpt.reference: excerpt}
    original_bind = module().bind_plan
    bound_inputs = []

    def capture_binding(proposal, full_resources, full_sources, full_catalog):
        bound_inputs.append((full_resources, full_sources, full_catalog))
        return original_bind(proposal, full_resources, full_sources, full_catalog)

    monkeypatch.setattr(module(), "bind_plan", capture_binding)
    result = planner.propose(
        "Report RMSE.", record, catalog=catalog,
        resources={resource.id: resource}, sources=sources,
    )
    assert result.action == "plan"
    sent = json.loads(model._seen[0][-1].content)
    assert [entry["name"] for entry in sent["catalog"]] == ["offline_rmse"]
    assert sent["sources"] == []
    assert len(bound_inputs) == 1
    assert bound_inputs[0] == ({resource.id: resource}, sources, catalog)
    assert sources[excerpt.reference] == excerpt
    assert calls == []
```

- [ ] **Step 2: Run targeted tests to verify the missing callback/ceiling behavior**

Run: `python -m pytest tests/test_agent_planner.py -k 'numeric_metrics or utf8_boundary or real_120000 or actual_120000 or full_local_binding' -q`

Expected: FAIL for unexpected `observe` keyword and the old ceiling.

- [ ] **Step 3: Wrap the existing planner and replace only the context boundary**

Import `Callable`, `PlanningMetrics`, `PlanningTelemetry`, and the Task 2 symbols. Rename the existing `propose()` body to `_propose()` and add its `state` keyword; keep its local source validation and decision/correction logic. Add this public wrapper:

```python
def propose(
    self, message: str, conversation: Conversation, *,
    catalog: list[dict[str, object]], resources: dict[str, ResourceReference],
    sources: dict[str, CriterionSource],
    observe: Callable[[PlanningTelemetry], None] | None = None,
) -> AgentDecision:
    state = PlanningState(started_at=monotonic())
    try:
        return self._propose(
            message, conversation, catalog=catalog, resources=resources,
            sources=sources, state=state,
        )
    finally:
        if observe is not None:
            observe(PlanningTelemetry(metrics=PlanningMetrics(
                serialized_bytes=state.serialized_bytes,
                selected_capability_count=state.selected_capability_count,
                selected_source_excerpt_count=state.selected_source_excerpt_count,
                planning_duration_seconds=max(0.0, monotonic() - state.started_at),
                correction_round_count=state.correction_round_count,
            )))
```

Immediately **after** `_authorized_sources()` in `_propose()`, select context and retain full locals:

```python
selection = select_context(
    message, conversation, catalog=catalog, resources=resources, sources=sources,
)
state.selected_capability_count = len(selection.catalog)
state.selected_source_excerpt_count = len(selection.sources)
```

Replace only these two entries in `raw_context`:

```python
"catalog": list(selection.catalog),
"sources": [source.model_dump(mode="json") for source in selection.sources],
```

Keep all resource descriptors except their existing excluded `locator`; retain the existing request, `has_output_directory`, observations, recent messages, `history_omitted`, and the **complete** `clarification_context`. Continue computing `_source_selections(resources, sources)` from the full local inputs. Do not strip project documentation/settings, duplicate source text, thresholds, missing-setting warnings, or prior corrections to make the context fit.

Replace the first size check with:

```python
state.serialized_bytes = context_byte_size(context)
if state.serialized_bytes > MAX_CONTEXT_BYTES:
    return AgentDecision(
        action="clarify",
        text=(
            "上下文超过 120 KB（120000 UTF-8 bytes）大小限制；"
            "未截断需求、判据或来源原文，请缩小本次输入范围。"
        ),
    )
```

Set `deadline = state.started_at + timeout` instead of starting a new deadline after context construction. Immediately before `_decision()` inside the existing `for attempt in range(2)` loop, set:

```python
state.correction_round_count = attempt
```

Keep these existing calls using full inputs, **not** `selection.catalog` or `selection.sources`:

```python
plan = _restore_source_quotes(decision.plan, sources, paths)
bind_plan(plan, resources, sources, catalog)
```

Replace the correction-context size check with:

```python
state.serialized_bytes = max(state.serialized_bytes, context_byte_size(context))
if state.serialized_bytes > MAX_CONTEXT_BYTES:
    return AgentDecision(
        action="clarify",
        text=(
            "方案校验反馈超过 120 KB（120000 UTF-8 bytes）大小限制；"
            "未截断需求、判据或来源原文，本次未生成新方案或触发执行。"
        ),
    )
```

In the existing `test_correction_does_not_truncate_request_to_fit_feedback`, replace its `pytest.raises` block with:

```python
decision = planner.propose(
    "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={},
)
assert decision.action == "clarify"
assert "大小限制" in decision.text and "未截断" in decision.text
assert len(model._seen) == 1
assert calls == []
```

The selector orders retained candidates by the number of unbound required resource roles and then capability identity. This is stable affinity ordering, not top-K filtering; a missing resource never removes a candidate. Replace positional catalog comparisons in `test_registered_schema_references_are_not_redacted_as_local_paths` and `test_nested_metadata_credentials_are_not_forwarded_to_the_model` with dictionaries keyed by `name`:

```python
sent = {entry["name"]: entry for entry in payload["catalog"]}
registered = {entry["name"]: entry for entry in tools.catalog()}
assert sent["evaluate_criteria"]["input_schema"] == registered["evaluate_criteria"]["input_schema"]
assert sent["offline_rmse"]["output_schema"] == registered["offline_rmse"]["output_schema"]
```

Do not alter source-restoration matching: a redacted **complete** excerpt restores only its exact original quote, and kind/reference/digest/version are still validated. Ordinary unredacted exact substrings remain valid; partial redaction-marker substitutions are not reverse-engineered.

- [ ] **Step 4: Verify planner, source, permissions, local-source, and deadline regressions**

Run: `python -m pytest tests/test_planning_context.py tests/test_agent_planner.py tests/test_local_matlab_planning.py tests/test_local_matlab_frequency.py tests/test_structured_planner.py -q`

Expected: PASS. In particular, existing quote round-trip/digest-forgery tests, external `write_delivery` correction tests, local initializer-conflict tests, and shared-timeout tests stay effective.

- [ ] **Step 5: Commit context integration**

```bash
git add src/flight_agent/verification/agent_planner.py tests/test_agent_planner.py
git commit -m "feat: enforce measured 120KB planner context without truncation"
```

### Task 4: Classify Copilot Failures Without Retaining Provider Text

**Files:**
- Create: `src/flight_agent/copilot_diagnostics.py`
- Modify/Test: `tests/test_copilot_diagnostics.py`

**Interfaces:**
- Consumes: Task 1 `CopilotDiagnostics`, `CopilotFailureCategory`, `SAFE_SUMMARIES`.
- Produces: `CopilotError(ValueError)` with `.diagnostics: CopilotDiagnostics`.
- Produces: `CopilotInvalidResponseError(CopilotError)` with `.correction_feedback: dict[str, object] | None`; feedback contains schema field paths and codes only, not response values.
- Produces: `CopilotTimeoutError(CopilotError)` with `.timeout_seconds: float`, optional round/budget context, and backward-compatible `CopilotTimeoutError(timeout_seconds)` construction.
- Produces: `classify_failure(stdout: str, stderr: str) -> CopilotFailureCategory`.
- Produces: `make_diagnostics(category: CopilotFailureCategory, *, stdout: str, stderr: str, exit_code: int | None, elapsed_seconds: float) -> CopilotDiagnostics`.
- Produces: `schema_feedback(error: ValidationError, schema: type[BaseModel]) -> dict[str, object]`.

- [ ] **Step 1: Add failing category and redaction tests**

Append to `tests/test_copilot_diagnostics.py`:

```python
import json

from flight_agent.copilot_diagnostics import (
    CopilotError, classify_failure, make_diagnostics, schema_feedback,
)
from flight_agent.verification.agent_planner import AgentDecision


@pytest.mark.parametrize("text,category", [
    ("HTTP 401 Unauthorized", "authentication"),
    ("not logged in", "authentication"),
    ("token has expired", "authentication"),
    ("HTTP 403 Forbidden", "authorization"),
    ("model access denied", "authorization"),
    ("account access denied", "authorization"),
    ("WebSocket closed", "transport"),
    ("proxy connection failed", "transport"),
    ("getaddrinfo ENOTFOUND", "transport"),
    ("ECONNRESET", "transport"),
    ("unknown CLI problem", "process_failure"),
])
def test_failure_categories_are_distinct(text, category):
    assert classify_failure("", text) == category


def test_diagnostic_summary_and_request_id_cannot_echo_secrets():
    raw = (
        "HTTP 401 Unauthorized\nrequest-id: private-request-token\n"
        "Authorization: Bearer private-token\n/private/project\n"
        "password=private-password\nprivate prompt text\n"
    )
    diagnostic = make_diagnostics(
        "authentication", stdout="秘密", stderr=raw, exit_code=7, elapsed_seconds=0.125,
    )
    encoded = diagnostic.model_dump_json()
    for private in (
        "private-token", "private-password", "private prompt", "/private/project",
        "private-request-token", "秘密",
    ):
        assert private not in encoded
    assert diagnostic.redacted_request_id.startswith("sha256:")
    assert len(diagnostic.redacted_request_id) == 23
    assert diagnostic.response_bytes == len("秘密".encode("utf-8"))
    assert diagnostic.summary == SAFE_SUMMARIES["authentication"]
    assert str(CopilotError(diagnostic)) == diagnostic.summary + " exit 7"


def test_schema_feedback_omits_response_values_and_unknown_field_names():
    try:
        AgentDecision.model_validate({
            "action": "plan", "text": "private-response",
            "private-secret-field": "private-value",
        })
    except ValidationError as error:
        feedback = schema_feedback(error, AgentDecision)
    encoded = json.dumps(feedback)
    assert "private" not in encoded
    assert feedback["errors"]
    assert all(set(item) == {"location", "code", "message"} for item in feedback["errors"])


def test_jsonl_request_identity_is_hashed_without_persisting_the_event():
    output = json.dumps({
        "type": "error",
        "data": {"requestId": "private-provider-request", "message": "private-response"},
    })
    diagnostic = make_diagnostics(
        "transport", stdout=output, stderr="", exit_code=7, elapsed_seconds=0.1,
    )
    assert diagnostic.redacted_request_id.startswith("sha256:")
    assert "private" not in diagnostic.model_dump_json()
    assert diagnostic.response_bytes == len(output.encode("utf-8"))
```

- [ ] **Step 2: Run tests to verify the classifier module is missing**

Run: `python -m pytest tests/test_copilot_diagnostics.py -q`

Expected: collection fails for missing `copilot_diagnostics`.

- [ ] **Step 3: Implement typed errors, bounded classification, and code-only feedback**

Create `copilot_diagnostics.py`:

```python
import hashlib
import json
import re

from pydantic import BaseModel, ValidationError

from .runtime.planning_contracts import (
    SAFE_SUMMARIES,
    CopilotDiagnostics,
    CopilotFailureCategory,
)

DIAGNOSTIC_SCAN_BYTES = 8192


def _bounded_text(value: str) -> str:
    return value.encode("utf-8")[:DIAGNOSTIC_SCAN_BYTES].decode("utf-8", errors="ignore")


def classify_failure(stdout: str, stderr: str) -> CopilotFailureCategory:
    text = _bounded_text(stderr) + "\n" + _bounded_text(stdout)
    patterns: tuple[tuple[CopilotFailureCategory, str], ...] = (
        ("authentication", r"\b401\b|not logged in|not authenticated|login required|"
                           r"authentication (?:failed|required)|token (?:has )?expired|"
                           r"expired (?:token|credentials)|please (?:log|sign) in"),
        ("authorization", r"\b403\b|model access denied|account access denied|forbidden"),
        ("transport", r"websocket|proxy|dns|ENOTFOUND|EAI_AGAIN|ECONNRESET|"
                      r"ECONNREFUSED|connection (?:failed|closed|reset)|network unreachable"),
    )
    for category, expression in patterns:
        if re.search(expression, text, re.IGNORECASE):
            return category
    return "process_failure"


def make_diagnostics(
    category: CopilotFailureCategory, *, stdout: str, stderr: str,
    exit_code: int | None, elapsed_seconds: float,
) -> CopilotDiagnostics:
    request_id = None
    match = re.search(
        r"(?im)^\s*(?:x-github-request-id|request-id)\s*:\s*"
        r"([a-z0-9][a-z0-9_.:-]{0,127})\s*$",
        _bounded_text(stderr),
    )
    if match is not None:
        request_id = "sha256:" + hashlib.sha256(
            match[1].encode("utf-8")
        ).hexdigest()[:16]
    if request_id is None:
        for line in _bounded_text(stdout).splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            containers = [event, data] if isinstance(data, dict) else [event]
            for container in containers:
                value = container.get("requestId", container.get("request_id"))
                if isinstance(value, str) and re.fullmatch(
                    r"[a-z0-9][a-z0-9_.:-]{0,127}", value, re.IGNORECASE,
                ):
                    request_id = "sha256:" + hashlib.sha256(
                        value.encode("utf-8")
                    ).hexdigest()[:16]
                    break
            if request_id is not None:
                break
    return CopilotDiagnostics(
        category=category, redacted_request_id=request_id, exit_code=exit_code,
        elapsed_seconds=max(0.0, elapsed_seconds),
        response_bytes=len(stdout.encode("utf-8")), summary=SAFE_SUMMARIES[category],
    )


class CopilotError(ValueError):
    def __init__(self, diagnostics: CopilotDiagnostics):
        self.diagnostics = diagnostics
        suffix = "" if diagnostics.exit_code is None else f" exit {diagnostics.exit_code}"
        super().__init__(diagnostics.summary + suffix)


class CopilotInvalidResponseError(CopilotError):
    def __init__(
        self, diagnostics: CopilotDiagnostics, *,
        correction_feedback: dict[str, object] | None = None,
    ):
        self.correction_feedback = correction_feedback
        super().__init__(diagnostics)


class CopilotTimeoutError(CopilotError):
    def __init__(
        self, timeout_seconds: float, *, diagnostics: CopilotDiagnostics | None = None,
        round_number: int | None = None, total_budget_seconds: float | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        super().__init__(diagnostics or make_diagnostics(
            "timeout", stdout="", stderr="", exit_code=None, elapsed_seconds=timeout_seconds,
        ))
        if round_number is None or total_budget_seconds is None:
            self.args = (f"Copilot CLI timed out after {timeout_seconds:g}s; not retried",)
        else:
            self.args = (
                f"Copilot CLI 规划第 {round_number} 轮超时："
                f"本轮可用 {timeout_seconds:g}s，原总预算 {total_budget_seconds:g}s"
                "（含前轮耗时，两轮共用）。未自动重试；"
                "本次未生成新方案或触发执行。",
            )


def schema_feedback(error: ValidationError, schema: type[BaseModel]) -> dict[str, object]:
    field_names: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                field_names.update(str(key) for key in properties)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema.model_json_schema())
    errors = []
    for issue in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
        code = str(issue["type"])
        if re.fullmatch(r"[a-z0-9_]{1,100}", code) is None:
            code = "schema_validation"
        errors.append({
            "location": [
                str(part) if isinstance(part, int) or part in field_names else "[field]"
                for part in issue["loc"][:10]
            ],
            "code": code,
            "message": "Structured response schema validation failed.",
        })
    return {"errors": errors}
```

Only the bounded classification copy is shortened. The response byte count uses the complete received stdout; no prompt or source content is truncated. Fixed summaries deliberately give up verbatim provider explanations to make persistence safe by construction. Do not attach raw subprocess exceptions, `ValidationError`, event payloads, stdout, stderr, or environment values as attributes on these typed errors.

- [ ] **Step 4: Verify classifier and contract tests**

Run: `python -m pytest tests/test_copilot_diagnostics.py tests/test_planning_context.py -q`

Expected: PASS; no network, CLI inference, login, or real credential access.

- [ ] **Step 5: Commit typed classification**

```bash
git add src/flight_agent/copilot_diagnostics.py tests/test_copilot_diagnostics.py
git commit -m "feat: classify Copilot failures with allowlisted redacted diagnostics"
```

### Task 5: Apply Typed Failures, Ephemeral Isolation, and Bounded Structural Correction

**Files:**
- Modify: `src/flight_agent/copilot.py:1-172`
- Modify: `src/flight_agent/verification/agent_planner.py:1-30` and the Task 3 `_propose()` correction loop.
- Modify/Test: `tests/test_copilot.py`
- Modify/Test: `tests/test_copilot_diagnostics.py`
- Modify/Test: `tests/test_agent_planner.py`

**Interfaces:**
- Preserves: `structured_response(name: str, schema: type[ResponseModel], context: dict[str, object], system_prompt: str, settings: ModelSettings) -> ResponseModel`.
- Preserves: `_catalog_context(context: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]` and lossless whole-schema sharing.
- Preserves/re-exports: `flight_agent.copilot.CopilotTimeoutError`; import the Task 4 class here instead of defining a second class.
- Produces: `_runtime_environment(home: str) -> dict[str, str]`.
- Produces: `_copilot_args(executable: str, model: str, prompt: str, directory: str) -> list[str]`.
- Preserves: `_final_message(output: str) -> str`; its internal parse failures remain local `ValueError` until wrapped by `structured_response()`.
- Produces: `_provider_error_category(output: str, stderr: str) -> CopilotFailureCategory | None`.
- Preserves: one initial inference plus at most one existing structural/source-selection/output-permission correction, using the original deadline.
- Produces: typed `CopilotTimeoutError` or `CopilotInvalidResponseError` after terminal Copilot failure, not a generic `ValueError` wrapper.
- Produces: `PlanningTelemetry.failure` populated from the terminal `CopilotError.diagnostics`; a successfully corrected schema response has `failure=None`.

- [ ] **Step 1: Extend the fake CLI and add failing subprocess/privacy tests**

In the existing `copilot_cli` fixture, add a test-only stderr output immediately before the fake executable's `sys.exit(...)`:

```python
"print(os.environ.get('PROBE_STDERR', ''), file=sys.stderr, flush=True)\n"
```

Extend its captured JSON with:

```python
"'copilot_home': os.environ.get('COPILOT_HOME'), "
"'has_copilot_auth': 'COPILOT_GITHUB_TOKEN' in os.environ, "
```

Give fake-CLI tests a synthetic supported authentication variable:

```python
monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "synthetic-auth-do-not-persist")
```

`PROBE_*` variables are test hooks only. The production environment builder below intentionally does not forward them. In this fixture, monkeypatch the builder to add test hooks **after** calling the real builder:

```python
import flight_agent.copilot as adapter

original_environment = adapter._runtime_environment

def probe_environment(home):
    environment = original_environment(home)
    environment.update({key: value for key, value in os.environ.items()
                        if key.startswith("PROBE_")})
    return environment

monkeypatch.setattr(adapter, "_runtime_environment", probe_environment)
```

Until `_runtime_environment` exists, the fixture's failure is the expected red result. Do not add a production `PROBE_*` exception.

Append:

```python
from flight_agent.copilot_diagnostics import CopilotError, CopilotInvalidResponseError


@pytest.mark.parametrize("stderr,category", [
    ("HTTP 401 Unauthorized", "authentication"),
    ("HTTP 403 Forbidden", "authorization"),
    ("WebSocket ECONNRESET", "transport"),
    ("unclassified CLI termination", "process_failure"),
])
def test_copilot_process_failure_is_typed_redacted_and_not_retried(
    copilot_cli, monkeypatch, stderr, category,
):
    monkeypatch.setenv("PROBE_STDERR", stderr + "\nsecret=private-stderr-value")
    monkeypatch.setenv("PROBE_EVENTS", "private-response-payload")
    monkeypatch.setenv("PROBE_EXIT", "7")
    with pytest.raises(CopilotError) as caught:
        call_planner()
    assert caught.value.diagnostics.category == category
    assert caught.value.diagnostics.exit_code == 7
    assert caught.value.__cause__ is None
    assert "private" not in caught.value.diagnostics.model_dump_json()
    assert "private" not in str(caught.value)
    assert json.loads(copilot_cli.read_text())["calls"] == 1


def test_isolated_profile_keeps_environment_auth_without_loading_user_profile(
    copilot_cli, monkeypatch, tmp_path,
):
    user_profile = tmp_path / "existing-profile"
    user_profile.mkdir()
    config = user_profile / "config.json"
    config.write_text('{"private":"unchanged"}', encoding="utf-8")
    monkeypatch.setenv("COPILOT_HOME", str(user_profile))
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "true")
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"clarify","text":"Input?"}'))
    call_planner()
    capture = json.loads(copilot_cli.read_text())
    assert capture["has_copilot_auth"] is True
    assert capture["copilot_home"] != str(user_profile)
    assert not Path(capture["copilot_home"]).exists()
    assert config.read_text("utf-8") == '{"private":"unchanged"}'
    assert "--no-custom-instructions" in capture["args"]
    assert "--sync-session=off" in capture["args"]
    assert "--disable-client-telemetry" in capture["args"]
    assert "--log-level" in capture["args"] and "none" in capture["args"]
    assert not any(arg.startswith("--allow") for arg in capture["args"])


def test_schema_failure_is_typed_with_only_structural_feedback(copilot_cli, monkeypatch):
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"plan","text":"private-text"}'))
    with pytest.raises(CopilotInvalidResponseError) as caught:
        call_planner()
    assert caught.value.diagnostics.category == "invalid_response"
    assert caught.value.correction_feedback["errors"]
    assert "private-text" not in repr(caught.value.correction_feedback)
    assert caught.value.__cause__ is None


def test_absent_isolated_authentication_does_not_fall_back_to_user_state(
    copilot_cli, monkeypatch,
):
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CopilotError) as caught:
        call_planner()
    assert caught.value.diagnostics.category == "authentication"
    assert not copilot_cli.exists()


def test_spawn_failure_is_typed_without_the_original_os_error(
    copilot_cli, monkeypatch,
):
    import flight_agent.copilot as adapter

    def failed_start(*args, **kwargs):
        raise OSError("private executable path and secret=private-token")

    monkeypatch.setattr(adapter.subprocess, "Popen", failed_start)
    with pytest.raises(CopilotError) as caught:
        call_planner()
    assert caught.value.diagnostics.category == "process_failure"
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert not copilot_cli.exists()
```

- [ ] **Step 2: Run new tests and verify untyped/unisolated behavior fails**

Run: `python -m pytest tests/test_copilot.py -k 'typed_redacted or isolated_profile or structural_feedback or isolated_authentication' -q`

Expected: FAIL for missing `_runtime_environment` and typed wrapping; no real inference is permitted.

- [ ] **Step 3: Isolate CLI state and wrap every adapter failure**

Local evidence from `copilot --version`, `copilot --help`, `copilot help environment`, and `copilot help logging` at authoring time:

- The installed CLI identified itself as `1.0.105`.
- It documents `COPILOT_HOME` as the configuration/state directory override and `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN` as authentication sources.
- It documents `--no-custom-instructions`, `--disable-builtin-mcps`, `--log-level none`, `--log-dir`, `--disable-client-telemetry`, `--sync-session=off`, `--no-auto-update`, and `--no-bash-env`.
- `--additional-mcp-config` **augments** user configuration; it is not an isolation switch. Do not use an empty additional config to claim isolation.
- No supported “disable all custom plugins and all persisted session data while retaining an arbitrary saved-login profile” switch was established. Record that limitation in `docs/usage.md`, do not invent a flag, read/copy saved tokens, invoke `copilot login`, rewrite user settings, or retry inference with weaker isolation.

Choose a temporary `COPILOT_HOME` and forward supported environment authentication unchanged. If only a saved-login profile is available, fail explicitly as `authentication` **before** launching inference, with the documented operational limitation and remediation to provide supported environment authentication. This preserves an authenticated route without copying credentials or silently initializing user MCP/plugins. It intentionally does not claim transparent support for every persisted-login mechanism.

Add:

```python
AUTH_ENVIRONMENT = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
RUNTIME_ENVIRONMENT = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    *AUTH_ENVIRONMENT,
}


def _runtime_environment(home: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in RUNTIME_ENVIRONMENT}
    environment["COPILOT_HOME"] = home
    environment["COPILOT_OTEL_ENABLED"] = "false"
    return environment


def _copilot_args(executable: str, model: str, prompt: str, directory: str) -> list[str]:
    args = [
        executable, "--available-tools=skill",
        "--deny-tool=shell", "--deny-tool=write", "--deny-tool=read",
        "--deny-tool=url", "--deny-tool=memory",
        "--disable-builtin-mcps", "--no-custom-instructions", "--no-bash-env",
        "--disable-client-telemetry", "--sync-session=off", "--no-auto-update",
        "--log-level", "none", "--log-dir", os.path.join(directory, "logs"),
        "--stream", "off", "--output-format", "json", "--silent",
    ]
    if model:
        args.extend(["--model", model])
    args.extend(["-p", prompt])
    return args
```

Keep the current payload/prompt assembly and `_catalog_context()` unchanged. Delete the old adapter-local `CopilotTimeoutError` class. Add these imports alongside the existing imports:

```python
from time import monotonic

from pydantic import ValidationError

from .copilot_diagnostics import (
    CopilotError,
    CopilotInvalidResponseError,
    CopilotTimeoutError,
    classify_failure,
    make_diagnostics,
    schema_feedback,
)
from .runtime.planning_contracts import CopilotFailureCategory
```

Replace the old argument/environment/process block and the final schema-validation return with this process boundary:

```python
started_at = monotonic()
if not any(os.environ.get(name, "").strip() for name in AUTH_ENVIRONMENT):
    raise CopilotError(make_diagnostics(
        "authentication", stdout="", stderr="", exit_code=None,
        elapsed_seconds=monotonic() - started_at,
    ))
with tempfile.TemporaryDirectory(prefix="flight-agent-copilot-") as directory:
    home = os.path.join(directory, "profile")
    os.mkdir(home, mode=0o700)
    environment = _runtime_environment(home)
    args = _copilot_args(executable, settings.model, prompt, directory)
    try:
        process = subprocess.Popen(
            args, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", start_new_session=True,
        )
    except OSError:
        raise CopilotError(make_diagnostics(
            "process_failure", stdout="", stderr="", exit_code=None,
            elapsed_seconds=monotonic() - started_at,
        )) from None
    stdout = ""
    stderr = ""
    timed_out = False
    io_failed = False
    cleanup_failed = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=settings.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError:
            io_failed = True
    finally:
        try:
            cleanup = _stop_worker(process, label="Copilot CLI")
        except OSError:
            cleanup_failed = True
        else:
            cleanup_failed = cleanup.error is not None
            if timed_out:
                stdout, stderr = cleanup.stdout, cleanup.stderr
    elapsed = monotonic() - started_at
    if timed_out:
        raise CopilotTimeoutError(
            settings.timeout_seconds,
            diagnostics=make_diagnostics(
                "timeout", stdout=stdout, stderr=stderr, exit_code=process.returncode,
                elapsed_seconds=elapsed,
            ),
        ) from None
    if cleanup_failed or io_failed:
        raise CopilotError(make_diagnostics(
            "process_failure", stdout=stdout, stderr=stderr, exit_code=process.returncode,
            elapsed_seconds=elapsed,
        )) from None
    if process.returncode != 0:
        raise CopilotError(make_diagnostics(
            classify_failure(stdout, stderr), stdout=stdout, stderr=stderr,
            exit_code=process.returncode, elapsed_seconds=elapsed,
        )) from None
    try:
        final = _final_message(stdout)
    except (ValueError, RecursionError):
        category = _provider_error_category(stdout, stderr)
        if category is not None:
            raise CopilotError(make_diagnostics(
                category, stdout=stdout, stderr=stderr, exit_code=process.returncode,
                elapsed_seconds=elapsed,
            )) from None
        raise CopilotInvalidResponseError(make_diagnostics(
            "invalid_response", stdout=stdout, stderr=stderr, exit_code=process.returncode,
            elapsed_seconds=elapsed,
        )) from None
    try:
        parsed = json.loads(final)
    except (json.JSONDecodeError, RecursionError):
        raise CopilotInvalidResponseError(make_diagnostics(
            "invalid_response", stdout=stdout, stderr=stderr, exit_code=process.returncode,
            elapsed_seconds=elapsed,
        )) from None
    try:
        return schema.model_validate(parsed)
    except ValidationError as error:
        raise CopilotInvalidResponseError(
            make_diagnostics(
                "invalid_response", stdout=stdout, stderr=stderr,
                exit_code=process.returncode, elapsed_seconds=elapsed,
            ),
            correction_feedback=schema_feedback(error, schema),
        ) from None
```

Also replace the initial missing-executable `ValueError` in `structured_response()` with:

```python
if executable is None:
    raise CopilotError(make_diagnostics(
        "process_failure", stdout="", stderr="", exit_code=None, elapsed_seconds=0.0,
    ))
```

Do not change `ModelSettings.from_environment()` bootstrap validation in this task: its existing missing-installation configuration error is distinct from an adapter failing after successful configuration. Keep existing POSIX validation.

For JSONL, keep the 4,000,000-byte response ceiling, require a successful terminal `result`, reject malformed lines, reject unexpected tool execution, and reject assistant tool requests. Strengthen `_final_message()` so **any** assistant event with nonempty `toolRequests` is rejected immediately rather than being erased by a later clean message:

```python
if event.get("type") == "assistant.message" and data.get("toolRequests"):
    raise ValueError("Copilot CLI attempted an unexpected tool request")
```

Provider error events must also be typed even if the outer process exits zero. Add before normal assistant/result handling:

```python
if event.get("type") == "error":
    raise ValueError("Copilot CLI reported a provider error event")
```

At the adapter parse-failure boundary, distinguish a recognized provider error from invalid protocol only when an actual JSONL `error` event exists:

```python
def _provider_error_category(output: str, stderr: str) -> CopilotFailureCategory | None:
    if len(output.encode("utf-8")) > 4_000_000:
        return None
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return None
        if isinstance(event, dict) and event.get("type") == "error":
            category = classify_failure(line, stderr)
            return category if category != "process_failure" else None
    return None
```

The process-boundary code above calls this helper only on a protocol parse failure. Do not classify arbitrary successful assistant narrative containing “401” as a provider failure. JSON decoding of the final assistant text is separated from schema validation: malformed JSON gets no correction feedback and is not retried.

All raises that follow raw subprocess/JSON/schema failures use `from None`. Do not log prompts or raw output. Application code creates no prompt file; CLI-owned temporary state and disabled-log destinations are removed with the temporary directory on success and all failure paths. A killed parent cannot guarantee cleanup after `SIGKILL`; documentation must identify orphaned `flight-agent-copilot-*` temporary directories as sensitive and require local cleanup, not ingestion into support bundles.

- [ ] **Step 4: Verify fake-CLI behavior and parser edge cases**

Add to `tests/test_copilot.py`:

```python
@pytest.mark.parametrize("output,category", [
    ("not JSONL", "invalid_response"),
    (events('{"action":"clarify","text":"Input?"}', complete=False), "invalid_response"),
    (events('{"action":"clarify","text":"Input?"}', tool="shell"), "invalid_response"),
    (json.dumps({"type": "error", "data": {"message": "HTTP 401 Unauthorized"}}),
     "authentication"),
    (json.dumps({"type": "error", "data": {"message": "WebSocket closed"}}),
     "transport"),
])
def test_protocol_and_zero_exit_provider_errors_are_typed(
    copilot_cli, monkeypatch, output, category,
):
    monkeypatch.setenv("PROBE_EVENTS", output)
    with pytest.raises(CopilotError) as caught:
        call_planner()
    assert caught.value.diagnostics.category == category
    assert json.loads(copilot_cli.read_text())["calls"] == 1
```

Run: `python -m pytest tests/test_copilot_diagnostics.py tests/test_copilot.py -q`

Expected: adapter-only subprocess, schema-sharing, and protocol tests pass. AgentPlanner schema-correction tests remain red until Steps 5–8 below integrate the typed errors. This is an intermediate TDD checkpoint within one task, not a commit gate. Do not run real inference.

- [ ] **Step 5: Add failing no-retry and two-call correction tests**

Append to `tests/test_copilot.py`:

```python
from flight_agent.copilot_diagnostics import CopilotTimeoutError


@pytest.mark.parametrize("stderr,category", [
    ("HTTP 401 Unauthorized", "authentication"),
    ("HTTP 403 Forbidden", "authorization"),
    ("WebSocket closed", "transport"),
    ("ordinary process failure", "process_failure"),
])
def test_agent_does_not_structurally_retry_a_provider_failure(
    copilot_cli, monkeypatch, tmp_path, stderr, category,
):
    from tests.test_agent_planner import setup

    planner, store, record, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    monkeypatch.setenv("PROBE_STDERR", stderr)
    monkeypatch.setenv("PROBE_EXIT", "7")
    monkeypatch.setenv("PROBE_EVENTS", "private-output")
    observed = []
    with pytest.raises(CopilotError) as caught:
        planner.propose(
            "Report RMSE.", record, catalog=tools.catalog(), resources={}, sources={},
            observe=observed.append,
        )
    assert caught.value.diagnostics.category == category
    assert observed[0].failure == caught.value.diagnostics
    assert observed[0].metrics.correction_round_count == 0
    assert json.loads(copilot_cli.read_text())["calls"] == 1
    assert calls == [] and store.get(record.id).task is None


def test_schema_correction_is_the_only_second_inference_and_keeps_original_context(
    copilot_cli, monkeypatch, tmp_path,
):
    from tests.test_agent_planner import setup

    planner, _, record, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    monkeypatch.setenv("PROBE_EVENTS_SEQUENCE", json.dumps([
        events('{"action":"plan","text":"private-invalid-proposal"}'),
        events('{"action":"clarify","text":"Supply arrays."}'),
    ]))
    observed = []
    result = planner.propose(
        "Report RMSE.", record, catalog=tools.catalog(), resources={}, sources={},
        observe=observed.append,
    )
    assert result.action == "clarify"
    assert observed[0].failure is None
    assert observed[0].metrics.correction_round_count == 1
    assert json.loads(copilot_cli.read_text())["calls"] == 2
    assert calls == []


def test_repeated_schema_failure_stays_typed_and_stops_after_two_calls(
    copilot_cli, monkeypatch, tmp_path,
):
    from tests.test_agent_planner import setup

    planner, store, record, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"plan","text":"private-invalid"}'))
    with pytest.raises(CopilotInvalidResponseError) as caught:
        planner.propose(
            "Report RMSE.", record, catalog=tools.catalog(), resources={}, sources={},
        )
    assert caught.value.diagnostics.category == "invalid_response"
    assert "private-invalid" not in str(caught.value)
    assert json.loads(copilot_cli.read_text())["calls"] == 2
    assert calls == [] and store.get(record.id).task is None
```

- [ ] **Step 6: Run tests to observe lost correction and lost timeout typing**

Run: `python -m pytest tests/test_copilot.py -k 'structurally_retry or only_second or stays_typed or timeout_reports_round' -q`

Expected: FAIL until AgentPlanner handles typed schema feedback and stops wrapping timeout in a plain `ValueError`.

- [ ] **Step 7: Integrate typed failure flow without another retry loop**

Add `CopilotError`, `CopilotInvalidResponseError`, and `make_diagnostics` imports from `copilot_diagnostics`. In Task 3's public wrapper, capture only terminal diagnostics:

```python
failure = None
try:
    return self._propose(
        message, conversation, catalog=catalog, resources=resources,
        sources=sources, state=state,
    )
except CopilotError as error:
    failure = error.diagnostics
    raise
finally:
    if observe is not None:
        observe(PlanningTelemetry(
            metrics=PlanningMetrics(
                serialized_bytes=state.serialized_bytes,
                selected_capability_count=state.selected_capability_count,
                selected_source_excerpt_count=state.selected_source_excerpt_count,
                planning_duration_seconds=max(0.0, monotonic() - state.started_at),
                correction_round_count=state.correction_round_count,
            ),
            failure=failure,
        ))
```

In the existing two-iteration loop, order exception handlers as timeout, typed schema failure, other typed failure, then existing Pydantic `ValidationError`. Replace the timeout handler and insert:

```python
except CopilotTimeoutError as error:
    raise CopilotTimeoutError(
        error.timeout_seconds, diagnostics=error.diagnostics,
        round_number=attempt + 1, total_budget_seconds=timeout,
    ) from None
except CopilotInvalidResponseError as error:
    if attempt or error.correction_feedback is None:
        raise
    feedback = error.correction_feedback
except CopilotError:
    raise
```

Only schema failures get `correction_feedback`; malformed JSONL, incomplete events, tools, nonzero exits, authentication, authorization, transport, timeout, and cleanup failures do not. Keep API-provider `ValidationError` correction unchanged. Keep existing local `source_selection_mismatch` and `external_output_permission_required` feedback, with the same full request and authorization rules.

If the original deadline expires before a second Copilot call, raise typed timeout without launching another process. Replace `_propose()`'s timeout-resolution block after the initial context-size check with this code; oversized context must not require provider configuration:

```python
settings = ModelSettings.from_environment() if self.model is None else None
timeout = self._timeout_seconds
if timeout is None:
    timeout = settings.timeout_seconds if settings is not None else 60.0
deadline = state.started_at + timeout
```

Replace the `remaining <= 0` block with:

```python
if remaining <= 0:
    if settings is not None and settings.provider == "copilot-cli":
        raise CopilotTimeoutError(
            0.0,
            diagnostics=make_diagnostics(
                "timeout", stdout="", stderr="", exit_code=None,
                elapsed_seconds=monotonic() - state.started_at,
            ),
            round_number=attempt + 1, total_budget_seconds=timeout,
        ) from None
    raise ValueError(
        "方案修正已达到原总超时，本次未生成新方案或触发执行。"
    ) from last_error
```

This timeout exception does not cause a second inference; `correction_round_count` remains zero if the second call never began. For a terminal typed schema failure, use its fixed English safe summary in persisted diagnostics; update the existing `test_copilot_agent_rejects_mixed_clarification_with_safe_error` to expect `CopilotInvalidResponseError` instead of matching the old Chinese generic error. Keep its two-call, no-secret, no-task, no-execution assertions.

Upgrade `test_agent_timeout_reports_round_and_shared_total_budget` from `pytest.raises(ValueError)` to `pytest.raises(CopilotTimeoutError)` and assert `.diagnostics.category == "timeout"` in addition to its existing Chinese round/budget/no-retry assertions. Keep all API-provider tests unchanged.

- [ ] **Step 8: Run the combined adapter/correction green gate**

Run: `python -m pytest tests/test_copilot.py tests/test_copilot_diagnostics.py tests/test_agent_planner.py tests/test_structured_planner.py tests/test_local_matlab_planning.py tests/test_local_matlab_frequency.py -q`

Expected: PASS, including schema sharing, initializer correction, output-permission correction, redacted source restoration, two-call structural bounds, and original shared time budgets. No real Copilot or MATLAB inference occurs.

- [ ] **Step 9: Commit the green adapter/correction deliverable**

```bash
git add src/flight_agent/copilot.py src/flight_agent/verification/agent_planner.py tests/test_copilot.py tests/test_agent_planner.py
git commit -m "feat: isolate Copilot planning and preserve typed provider failures"
```

### Task 6: Persist Metrics and Allowlisted Failure Diagnostics Without Traceback Leaks

**Files:**
- Modify: `src/flight_agent/verification/contracts.py:305-351`
- Modify: `src/flight_agent/verification/coordinator.py:139-197`
- Modify: `src/flight_agent/verification/routes.py:103-128`
- Create/Test: `tests/test_planning_telemetry.py`

**Interfaces:**
- Consumes: `PlanningTelemetry`, `CopilotError`, `AgentPlanner.propose(..., observe=...)`, existing store interface.
- Produces: `Conversation.planning_telemetry: PlanningTelemetry | None = None`.
- Preserves: `ConversationManager._fail(conversation_id: str, error: Exception) -> None`, origin restoration, existing pending-decision logic, and `GET /api/conversations/{conversation_id}` returning `Conversation`.
- Publishes: latest per-operation telemetry through that existing authenticated status response. No new endpoint, table, diagnostic file, or UI state is needed.

- [ ] **Step 1: Write failing success, restart, and failure-privacy tests**

Create `tests/test_planning_telemetry.py`:

```python
import json
import logging

import pytest

from flight_agent.background import BackgroundRunner
from flight_agent.copilot_diagnostics import CopilotError, make_diagnostics
from flight_agent.runtime.planning_contracts import PlanningMetrics, PlanningTelemetry
from flight_agent.verification.agent_planner import AgentPlanner
from flight_agent.verification.routes import ConversationManager
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import make_agent_web


def test_successful_planning_metrics_survive_store_restart(tmp_path):
    _, runtime, calls, _ = make_agent_web(
        tmp_path, [{"action": "clarify", "text": "Supply arrays."}],
    )
    queued = runtime.queue_message(None, "Report RMSE.")
    record = runtime.continue_submission(queued.id)
    saved = ConversationStore(runtime.store.root).get(record.id)
    assert saved.planning_telemetry is not None
    assert saved.planning_telemetry.metrics.serialized_bytes > 0
    assert saved.planning_telemetry.failure is None
    assert saved.task is None and calls == []


@pytest.mark.parametrize("category", [
    "authentication", "authorization", "transport", "timeout",
    "invalid_response", "process_failure",
])
def test_provider_failure_storage_and_logs_contain_only_safe_diagnostics(
    tmp_path, caplog, category,
):
    _, runtime, calls, _ = make_agent_web(tmp_path, [])
    diagnostic = make_diagnostics(
        category, stdout="private-response", stderr=(
            "WebSocket closed\nrequest-id: private-request-id\n"
            "Authorization: Bearer private-token\n/private/project\n"
        ),
        exit_code=7, elapsed_seconds=0.2,
    )

    class FailedPlanner(AgentPlanner):
        def propose(self, message, conversation, *, catalog, resources, sources, observe=None):
            if observe is not None:
                observe(PlanningTelemetry(
                    metrics=PlanningMetrics(serialized_bytes=100, selected_capability_count=2),
                    failure=diagnostic,
                ))
            try:
                raise ValueError("private chained prompt and stderr")
            except ValueError:
                raise CopilotError(diagnostic) from None

    runtime.planner = FailedPlanner()
    queued = runtime.queue_message(None, "Report RMSE.")
    jobs = BackgroundRunner()
    try:
        manager = ConversationManager(runtime, jobs)
        with caplog.at_level(logging.ERROR):
            manager._run(queued.id, lambda: runtime.continue_submission(queued.id))
    finally:
        jobs.close()
    restarted = ConversationStore(runtime.store.root).get(queued.id)
    assert restarted.status == "failed"
    assert restarted.planning_telemetry.failure == diagnostic
    assert restarted.task is None and calls == []
    persisted = json.dumps(restarted.model_dump(mode="json"))
    for secret in (
        "private-response", "private-token", "private-request-id",
        "/private/project", "private chained prompt",
    ):
        assert secret not in persisted
        assert secret not in caplog.text
    provider_logs = [record for record in caplog.records if "Copilot" in record.getMessage()]
    assert provider_logs
    assert all(record.exc_info is None for record in provider_logs)
```

- [ ] **Step 2: Run tests to verify telemetry is not yet persisted**

Run: `python -m pytest tests/test_planning_telemetry.py -q`

Expected: FAIL for missing `Conversation.planning_telemetry` and missing callback integration; the privacy test also demonstrates why a provider-specific log path is necessary.

- [ ] **Step 3: Persist the call-local observation and safely handle provider errors**

Import `PlanningTelemetry` in `verification/contracts.py` and add:

```python
planning_telemetry: PlanningTelemetry | None = None
```

In `queue_message()`, clear `record.planning_telemetry` when entering a new `planning` state. In `continue_submission()`, replace only the `self.planner.propose(...)` assignment with:

```python
observations: list[PlanningTelemetry] = []
try:
    decision = self.planner.propose(
        record.messages[-1].content, record, catalog=catalog,
        resources=resources, sources=sources, observe=observations.append,
    )
finally:
    if observations:
        record.planning_telemetry = observations[-1]
        self.store.save(record)
```

Do not attach an observation list or “last metrics” field to a reused planner/runtime instance. The existing per-conversation operation boundary governs the save; do not add a global lock. A prior SQLite version should persist the validated payload in its existing save transaction rather than reintroducing JSON files.

Keep the existing successful plan/non-plan handling. The full catalog/resources/sources still feed the runtime's second `bind_plan()` call before executor proposal.

In `ConversationManager._fail()`, load the record first, then branch before generic `logger.exception(...)`:

```python
record = self.runtime.store.get(conversation_id)
if isinstance(error, CopilotError):
    logger.error(
        "Copilot planning failed for conversation %s category=%s exit_code=%s",
        conversation_id, error.diagnostics.category, error.diagnostics.exit_code,
    )
    message = str(error)
    metrics = (
        record.planning_telemetry.metrics
        if record.planning_telemetry is not None else PlanningMetrics()
    )
    record.planning_telemetry = PlanningTelemetry(
        metrics=metrics, failure=error.diagnostics,
    )
else:
    logger.exception("Conversation %s failed", conversation_id)
    message = str(error)[:4000] or "Conversation operation failed"
```

Continue with the existing `restore_turn_origin()` branch and pending-decision/status/error save logic. Do not reset a previous confirmed task, propose a replacement baseline, or transform a provider failure into a capability gap. The typed diagnostic lives separately from user-facing `error`. Fixed summaries and numeric round/budget suffixes are safe; no raw provider text or exception chain reaches logs or persisted fields.

No schema migration is necessary for the inspected validated JSON payload store. If an earlier SQLite stage stores `Conversation` as a payload plus projected fields, the new optional payload field uses that existing mechanism. Existing records default it to `None`.

- [ ] **Step 4: Verify persistence, origin recovery, and authenticated status exposure**

Append:

```python
from fastapi.testclient import TestClient

from flight_agent.verification.contracts import ConversationState, PendingDecision


def test_provider_failure_on_followup_restores_previous_task_state(tmp_path):
    _, runtime, _, _ = make_agent_web(tmp_path, [])
    record = runtime.store.create()
    previous = PendingDecision(kind="clarify", message="Previous question.")
    record.status = "planning"
    record.turn_origin = ConversationState(
        status="waiting_for_user", pending_decision=previous, error=None,
    )
    runtime.store.save(record)
    diagnostic = make_diagnostics(
        "authorization", stdout="", stderr="", exit_code=1, elapsed_seconds=0.1,
    )
    jobs = BackgroundRunner()
    try:
        ConversationManager(runtime, jobs)._fail(record.id, CopilotError(diagnostic))
    finally:
        jobs.close()
    restored = runtime.store.get(record.id)
    assert restored.status == "waiting_for_user"
    assert restored.pending_decision == previous
    assert restored.error is None
    assert restored.planning_telemetry.failure == diagnostic


def test_existing_conversation_endpoint_exposes_allowlisted_telemetry(tmp_path):
    app, runtime, _, _ = make_agent_web(tmp_path, [])
    record = runtime.store.create()
    record.planning_telemetry = PlanningTelemetry(
        metrics=PlanningMetrics(serialized_bytes=100),
    )
    runtime.store.save(record)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap").json()
        response = client.get(
            f"/api/conversations/{record.id}",
            headers={"x-flight-token": bootstrap["token"]},
        )
    assert response.status_code == 200
    assert response.json()["planning_telemetry"]["metrics"]["serialized_bytes"] == 100
    assert response.json()["planning_telemetry"]["failure"] is None
```

The token extraction and `x-flight-token` header above match `tests/test_agent_web.py:24-25` and `web.py:94-96`. The status route stays unchanged and protected.

Run: `python -m pytest tests/test_planning_telemetry.py tests/test_agent_web.py tests/test_agent_history.py tests/test_agent_recovery.py tests/test_copilot.py -q`

Expected: PASS; telemetry survives restart, follow-up origin state is preserved, protected status responses expose only the typed payload, and provider logs contain no traceback or private output.

- [ ] **Step 5: Commit persisted safe telemetry**

```bash
git add src/flight_agent/verification/contracts.py src/flight_agent/verification/coordinator.py src/flight_agent/verification/routes.py tests/test_planning_telemetry.py
git commit -m "feat: persist planner metrics and safe Copilot failure diagnostics"
```

### Task 7: Complete Stage 5 Acceptance and Operational Documentation

**Files:**
- Modify/Test: `tests/test_planning_context.py`
- Modify/Test: `tests/test_agent_planner.py`
- Modify/Test: `tests/test_copilot.py`
- Modify: `docs/architecture.md`
- Modify: `docs/usage.md`

**Interfaces:**
- Consumes: every interface defined in Tasks 1–6 and all existing confirmation/evidence/recovery contracts.
- Produces: executable Stage 5 acceptance coverage and accurate operational instructions.
- Preserves: existing no-automatic-retry behavior; no calls to real Copilot inference or licensed MATLAB are part of software verification.

- [ ] **Step 1: Add failing registry reachability and post-correction provider-failure tests**

Append to `tests/test_planning_context.py`:

```python
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.diagnostic_planner import DiagnosticReviewer
from flight_agent.verification.plugins import register_builtin_plugins
from flight_agent.verification.resources import ResourceRegistry


def test_every_registered_capability_remains_reachable_with_uncertain_intent():
    tools = CapabilityRegistry(agent_mode=True)
    register_builtin_plugins(
        tools, ResourceRegistry(), backend=None, diagnostic_reviewer=DiagnosticReviewer(),
    )
    catalog = tools.catalog()
    result = select_context(
        "Describe all registered engineering capabilities.",
        selection_conversation("Describe all registered engineering capabilities."),
        catalog=catalog, resources={}, sources={},
    )
    assert {entry["name"] for entry in result.catalog} == {entry["name"] for entry in catalog}
    for entry in catalog:
        assert entry["metadata"]["planning"] is not None
        for metric in entry["metadata"]["metrics"]:
            request = f"Report {metric['id']}."
            selected = select_context(
                request, selection_conversation(request),
                catalog=catalog, resources={}, sources={},
            )
            assert entry["name"] in {item["name"] for item in selected.catalog}
```

Extend the fake CLI fixture with a test-only per-attempt stderr and exit sequence, replacing its single stderr/exit statements:

```python
"stderr_sequence = os.environ.get('PROBE_STDERR_SEQUENCE')\n"
"print(json.loads(stderr_sequence)[attempt] if stderr_sequence "
"else os.environ.get('PROBE_STDERR', ''), file=sys.stderr, flush=True)\n"
"exit_sequence = os.environ.get('PROBE_EXIT_SEQUENCE')\n"
"sys.exit(int(json.loads(exit_sequence)[attempt] if exit_sequence "
"else os.environ.get('PROBE_EXIT', '0')))\n"
```

Append to `tests/test_copilot.py`:

```python
def test_transport_failure_on_structural_correction_never_triggers_a_third_call(
    copilot_cli, monkeypatch, tmp_path,
):
    from tests.test_agent_planner import setup

    planner, store, record, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    monkeypatch.setenv("PROBE_EVENTS_SEQUENCE", json.dumps([
        events('{"action":"plan","text":"Incomplete."}'), "private-provider-output",
    ]))
    monkeypatch.setenv("PROBE_STDERR_SEQUENCE", json.dumps(["", "WebSocket closed"]))
    monkeypatch.setenv("PROBE_EXIT_SEQUENCE", json.dumps([0, 7]))
    observed = []
    with pytest.raises(CopilotError) as caught:
        planner.propose(
            "Report RMSE.", record, catalog=tools.catalog(), resources={}, sources={},
            observe=observed.append,
        )
    assert caught.value.diagnostics.category == "transport"
    assert observed[0].metrics.correction_round_count == 1
    assert observed[0].failure.category == "transport"
    assert json.loads(copilot_cli.read_text())["calls"] == 2
    assert calls == [] and store.get(record.id).task is None
```

- [ ] **Step 2: Run acceptance tests and verify uncovered behavior**

Run: `python -m pytest tests/test_planning_context.py tests/test_copilot.py -q`

Expected: all preceding behavior is green; a missing built-in declaration or accidental third retry is a concrete failing assertion to fix before proceeding. No test may be weakened to hide an unsupported metric producer or a privacy regression.

- [ ] **Step 3: Add exact operational documentation**

Append this section to `docs/architecture.md`:

```markdown
## Planning context and provider telemetry

Planning normalizes a closed subset of metric requests and selects candidates from
registered output, metric, resource-role, and dependency metadata. Unknown intent,
unknown dependency metadata, unresolved dependencies, or selected dynamic
dependencies retain the full catalog. Missing resources and unavailable relevant
backends remain visible for clarification or a capability gap.
Selection metadata is catalog-only and excluded from execution snapshots, so it
does not rewrite confirmed baseline fingerprints or change capability versions.

Source selection keeps complete authorized excerpts. Only explicitly scoped,
unrelated non-user excerpts may be omitted from model context; unclassified
sources and all user-message sources are retained. The complete authorized
sources, resource references, project declarations, and catalog remain local for
binding and validation. Selection grants no permission or execution authority.

The logical redacted request context is limited to 120,000 UTF-8 bytes before
lossless catalog-schema packing. Neither the initial request nor correction
feedback can silently truncate requirements or quotations to fit. An oversized
context returns clarification without dispatch.

Conversation planning_telemetry contains metrics.serialized_bytes,
metrics.selected_capability_count, metrics.selected_source_excerpt_count,
metrics.planning_duration_seconds, and metrics.correction_round_count.
An optional failure contains only category, redacted_request_id, exit_code,
elapsed_seconds, response_bytes, and a fixed bounded summary. Provider request
IDs are represented by a 16-hex SHA256 prefix, never their original values.

No assembled prompt, response body, credentials, environment dump, or raw stderr
is stored in telemetry. Original user messages and source documents remain under
their existing local retention rules; telemetry does not copy them. A provider
failure is not a capability gap and does not erase the previous confirmed task.
```

Append this section to `docs/usage.md`:

```markdown
## Copilot planning failures and isolation

Copilot planning uses an empty temporary working directory and temporary
COPILOT_HOME. Shell, file reads, writes, URL, and memory tools remain denied.
Builtin MCP initialization and custom instructions are disabled; the temporary
profile prevents unrelated user MCP/plugin configuration from being loaded.
User configuration is never rewritten or copied.

For the locally inspected Copilot CLI 1.0.105, use a supported environment
authentication source: COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN.
The adapter forwards authentication only to the child process, not to diagnostics
or files. Do not paste credentials into a request or a support report.

Operational limitation: no supported switch was established that both isolates
all user MCP/plugins/session state and transparently reuses every saved-login
profile. With saved-login-only authentication the adapter fails explicitly before
inference; provide a supported environment authentication source. It does not
copy saved credentials, edit settings, or retry with a less isolated profile.
An installed CLI rejecting isolation flags is a process_failure; update the
installation deliberately rather than removing restrictions automatically.

Categories and next actions:
- authentication: verify the supported authentication source and account login.
- authorization: verify account entitlement and requested model access.
- transport: check DNS, proxy, WebSocket, and network connectivity.
- timeout: inspect the remaining shared planning deadline and reduce request scope.
- invalid_response: inspect the fixed summary and structured-response compatibility.
- process_failure: verify installation, supported flags, and process cleanup.

There is no automatic inference retry. A single existing structural correction
may follow a complete JSON response that fails schema validation, a deterministic
source-selection mismatch, or missing proposed delivery permission. It shares the
original deadline and cannot grant permission or execute a plan. Invalid JSONL,
tool activity, incomplete responses, and provider failures are not retried.

The authenticated conversation status API exposes planning_telemetry for support.
Share only its allowlisted fields, not prompts, raw CLI output, environment
variables, credentials, or source documents.

Temporary state is removed on success and handled failures. A forcibly killed
parent can leave flight-agent-copilot-* directories in the operating-system
temporary location. Treat orphaned directories as sensitive, remove them locally
after confirming no associated process is active, and never attach them to a
support bundle. Disabled application diagnostics do not imply any guarantee
about the provider's own server-side retention policy.
```

- [ ] **Step 4: Run Stage 5 and full software-only verification**

Run these commands sequentially from the worktree with the previously specified interpreter:

```bash
python -m pytest tests/test_planning_context.py tests/test_copilot_diagnostics.py tests/test_planning_telemetry.py tests/test_agent_planner.py tests/test_copilot.py tests/test_structured_planner.py -q
python -m pytest -m "not matlab" -q
python -m ruff check src tests
python -m mypy
python -m build
git diff --check
git --no-pager diff --stat
```

Expected: targeted tests PASS; all software-only tests PASS, with environment-gated browser/MATLAB skips reported explicitly rather than called passes; Ruff reports `All checks passed!`; mypy reports success; build produces wheel and sdist; diff whitespace check exits zero. Preserve existing source/resource/effects/budget/receipt/artifact/recovery tests. If an optional browser dependency is unavailable, record that limitation and run the installed browser job separately rather than hiding a failure. Real MATLAB acceptance remains opt-in and is not required for this context/provider-only change.

Inspect the final diff for these concrete negative guarantees:

- No replacement of complete local sources with selected model excerpts in either binding call.
- No `context[:limit]`, shortened quote, top-K, summary, request rewrite, or silent history trimming.
- No permission assignment from a filter, resource choice, source scope, previous baseline, or model response.
- No `logger.exception()`/`exc_info=True` for a typed Copilot failure.
- No raw stderr/stdout/prompt/environment/credentials in a persisted field or exception attribute.
- No inference retry loop outside the existing `range(2)` structural-correction loop.
- No CLI flag that disables restrictions after an error.
- No Stage 2–4/6 implementation or generated dependency changes.

- [ ] **Step 5: Commit acceptance and documentation**

```bash
git add tests/test_planning_context.py tests/test_copilot.py docs/architecture.md docs/usage.md
git commit -m "test: verify Stage 5 context and Copilot diagnostic guarantees"
```

## Self-Review Checklist for the Implementation

- [ ] Closed-scope reduction is deterministic; uncertain intent/metadata includes candidates.
- [ ] Every registered metric producer remains selectable; static dependency closure and dynamic fallback are tested.
- [ ] Missing resources, unavailable backends, unknown source scopes, user messages, prior clarifications, and project conflicts remain visible.
- [ ] Selected excerpts retain exact kind/reference/quote/digest/version; full authorized sources and resource locators stay local.
- [ ] Initial and correction contexts enforce the same 120,000-byte ceiling; UTF-8 and equality boundaries are tested.
- [ ] Context metrics cover every exit and use call-local state; correction count reflects actual inference calls.
- [ ] All six categories survive adapter, planner, runtime persistence, and authenticated status serialization.
- [ ] Diagnostics contain exactly the six allowlisted fields; summaries are fixed; provider IDs are hashed and bounded.
- [ ] Provider failure logs contain no exception chain, prompt, secrets, source paths, raw response, or unrestricted stderr.
- [ ] Authentication and isolation are explicit; user settings and saved tokens are not rewritten/copied; unsupported isolation is documented.
- [ ] One initial inference and at most one existing structural correction share the original deadline; provider failures never trigger a retry.
- [ ] Existing confirmation, effects, source, resource, version, budget, receipt, artifact, and recovery suites remain effective.
- [ ] Targeted tests, full software-only tests, lint, type checks, package build, and whitespace checks have fresh recorded results.

## Plan-Author Self-Review

Coverage: Tasks 1–3 cover declared metadata, conservative selection, stable execution snapshots, local binding, full local source retention, metrics, and both hard-limit boundaries. Tasks 4–5 cover all six provider failures, bounded redacted diagnostics, process isolation, shared deadlines, and structural correction only. Tasks 6–7 cover durable allowlisted telemetry, safe logging, recovery/permissions regressions, acceptance, and operational limitations.

Type consistency: the public planner still returns `AgentDecision`; telemetry travels only through the optional call-local callback and `Conversation.planning_telemetry`. `CopilotTimeoutError` is one class re-exported from `copilot.py`; terminal provider failures retain `CopilotError` typing. Candidate selection never replaces the full binder inputs.

Trade-off recorded: unknown/free-form requests and unscoped sources retain more context and may hit the explicit ceiling. Saved-login-only Copilot authentication is not silently combined with an unisolated user profile. These limitations are intentional, actionable, and do not weaken source fidelity or permissions.

Authoring review: checked the required header, exact interfaces and API header, source/permission invariants, catalog-only snapshot behavior, checkbox test-first steps, commands/results, and explicit commits. No implementation or test-suite results are claimed by this document; its Python examples receive syntax-only validation during plan authoring.
