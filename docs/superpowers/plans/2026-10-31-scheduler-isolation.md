# Scheduler Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Stage 3 with independent-conversation planning, serialized conversation/task mutation, bounded execution lanes, durable queued states, and conservative shutdown/restart recovery.

**Architecture:** Share one reference-counted `ConversationLockRegistry` between intake, planning, decisions, execution, and failure handling. Keep the audited synchronous executor and its serial dependency loop; use a bounded planning executor, a separately bounded execution-turn executor, and server-selected local-MATLAB, remote-Worker, and read-only permits around invocation and reconciliation. Persist queued state before scheduling, acquire an execution permit before creating a dispatched call, and never replay uncertain execution automatically.

**Tech Stack:** Python 3.11+, standard-library threading/concurrent.futures, Pydantic 2, FastAPI, Stage 2 SQLite WAL conversation storage, pytest, Ruff, mypy, native browser JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-29-v2-runtime-optimization-design.md`, especially “Scheduling and Concurrency”, “Error Handling and Recovery”, “Stage 3: Add scheduler isolation”, and “Verification”.

## Global Constraints

- “Preserve the current confirmation, permission, source-attribution, hashing, evidence, recovery, and deterministic-evaluation guarantees.”
- “Planning pool: default four workers, configurable by environment.”
- “Local MATLAB execution: default one slot, configurable but never unbounded.”
- “Remote Worker execution: separately bounded from local MATLAB.”
- “Read-only evaluation and delivery: bounded general execution pool.”
- “The scheduler selects a lane from declared capability effects and metadata. Capability implementations cannot self-select a less restrictive lane.”
- “Queued work is represented as queued state rather than a global `BusyError`.”
- “Cancellation is out of scope unless already supported by the underlying operation.”
- “Parallel planning must not allow two active turns in one conversation.”
- “Parallel execution must not permit two nodes to mutate the same task simultaneously.”
- “A node may run only after all dependencies have verified terminal outputs.”
- “Locks are removed when no operation references them.”
- “Lock identity is the validated conversation ID, not a user-controlled path.”
- “An unknown external execution state remains unretryable until reconciled.”
- “Adding Redis, Celery, or another external service.” remains a non-goal.
- “Keep supported Python at 3.11 and newer as declared by the package.”
- “The workbench remains native HTML, CSS, and JavaScript.”
- “There is no Node runtime or frontend compilation step.”
- “Each stage receives targeted tests, full software-only verification, and a separate commit.”

---

## Execution Boundary and Stage 2 Prerequisite

This document is the Stage 3 implementation plan, not an instruction to implement
Stages 2, 4, 5, or 6. The authoring checkout contains Stage 1 and the approved
design, but **does not yet contain the Stage 2 SQLite implementation**. The store
contracts below are explicit prerequisites, not claims about the JSON store in
the authoring checkout.

Execute from `/Users/yhe/Work/flight-agent/.worktrees/stages-2-6` after Stage 2.
Read this plan and the spec before changing code. The commit commands below are
instructions for the later implementation session; do not execute them while
authoring this document.

For every fresh command shell, first select the existing development
environment; shell activation does not persist between separate tool calls:

```bash
cd /Users/yhe/Work/flight-agent/.worktrees/stages-2-6
source /Users/yhe/Work/flight-agent/.venv-matlab/bin/activate
```

### Exact consumed Stage 2 APIs

Assume Stage 2 implements the interfaces in
`docs/superpowers/plans/2026-10-31-sqlite-conversation-store.md`.
`flight_agent.verification.store.ConversationStore` is an alias of
`flight_agent.verification.sqlite_store.SQLiteConversationStore`.
Runtime annotations use
`flight_agent.verification.conversation_repository.ConversationRepository`,
not the concrete SQLite class. Consume these exact signatures:

```python
from contextlib import AbstractContextManager
from pathlib import Path

from flight_agent.verification.contracts import Conversation

# Constructor: SQLiteConversationStore(root: Path, *, require_empty: bool = False)
# Public attribute: root: Path
# create(self) -> Conversation
# get(self, conversation_id: str) -> Conversation
# save(self, record: Conversation) -> None
# recent(self) -> list[Conversation]
# directory(self, conversation_id: str) -> Path
# default_task_root(self, conversation_id: str) -> Path
# task_directory(
#     self, conversation_id: str, task_id: str,
#     output_root: str | None = None, *, create: bool = False,
# ) -> Path
# transaction(self) -> AbstractContextManager[None]
# SQLite implementation: database_path: Path
# SQLite implementation: _recover_startup(self) -> None
# SQLite implementation: _read(self) -> AbstractContextManager[sqlite3.Connection]
# SQLite implementation: export(self, conversation_id: str) -> bytes
# Module helper: now() -> str
```

Additional required semantics:

1. `get()` returns an independent validated aggregate. `save()` atomically
   persists its messages, conversation state, task state, calls, budget state,
   and associated audit changes. No SQLite transaction remains open during a
   planner/provider/capability call or while acquiring a conversation/lane lock.
2. `transaction()` yields `None`, owns a thread-local connection, and implements
   nested units with savepoints. `_read()` and `transaction()` close their
   connections on exit. There is no repository `close()` or public connection
   accessor. WAL, foreign keys, busy timeout, and initial migrations belong to
   Stage 2.
3. `sqlite_codec.encode(record, existing_artifacts=None)` and
   `decode(connection, conversation_id)` map validated aggregates to normalized
   rows. The conversation `state_json` projection explicitly enumerates
   `schema_version`, `output_root_reset`, and `turn_origin`; Task 6 must add
   scheduling fields. It is not assumed to persist unknown/new fields.
4. Migration 1 constrains `conversations.status` to the pre-scheduler statuses.
   Never edit an applied migration or its checksum. Stage 3 Task 6 adds migration 2's
   nullable, indexed `scheduling_status` column and its exact codec mapping.
5. After migrations and before accepting traffic, the constructor invokes
   `_recover_startup()`. Stage 3 extends that method in Task 6, preserving its
   scan for running calls even under non-running conversation headers and in
   archived tasks. A second application process must not open the same writable state
   root while the first is running; process-local locks are not distributed
   locks.
6. Stage 2's `_accept(conversation, task, node, call, run, budget)` commits
   terminal call, receipt metadata, node state, and progress/budget atomically.
   Keep its cleanup-and-reraise rollback and post-commit `BudgetExhausted`
   handling. No Stage 3 wrapper reverts `_accept()` to its Stage 1 signature.
   Evidence paths, receipts, hashes, and task-directory validation retain Stage 2 behavior.

Preflight:

```bash
pwd
git status --short
rg -n 'class ConversationRepository|def transaction' src/flight_agent/verification/conversation_repository.py
rg -n 'class SQLiteConversationStore|def (_recover_startup|_read|transaction)' src/flight_agent/verification/sqlite_store.py
python -m pytest tests/test_agent_runtime.py tests/test_agent_recovery.py -q
```

Expected: correct worktree, understood existing changes, SQLite APIs present,
and existing runtime/recovery tests passing. If these contracts are absent,
stop and reconcile the Stage 2 interface **before implementation**. Do not
silently fall back to `conversation.json`, invent another persistence backend,
or run this plan against Stage 1.

## Decisions and Operational Semantics

### Options considered

| Option | Benefit | Cost / reason |
|---|---|---|
| Bounded synchronous turn orchestration plus lane permits — selected | Retains the executor's existing evidence, checkpoint, and budget sequence; no cross-thread mutation of a task | One coordination thread per admitted execution turn, including turns waiting for a lane; cap those threads explicitly |
| Continuation-based, per-node dispatcher | Fewer waiting threads; easier future fairness policies | Rewrites the audited synchronous executor and recovery loop; unnecessary for Stage 3 |

The coordination executor is **not another capability lane**. It owns task
orchestration only. A capability runs inline on its conversation's coordination
thread after acquiring its declared resource lane; it is never submitted back
to the same executor followed by `.result()`. An admitted remote/read-only turn
has its own coordination thread, so queued local turns cannot consume its lane's
workers. Exhausting the explicit total-turn admission cap returns a typed
capacity response; it does not allocate another thread.

One operation is admitted per conversation. HTTP admission first rejects an
already committed `queued`/`planning`/`running` state with a conversation-specific
`409`, without holding runner capacity while waiting for that conversation.
Requests racing from an idle snapshot serialize on the registry and recheck
state before mutation. Direct synchronous runtime operations also serialize
on the registry. There is **no durable multi-message mailbox** in this stage;
a rejected request is not appended to history. Different conversation IDs
never share the operation lock.

### States and lock ordering

| Work | Before admission | After durable admission | While performing work |
|---|---|---|---|
| Message | Existing idle state or a new conversation | `queued`, `queued_action="message"`; append user message in the same save | `planning`, then existing answer/clarification/proposal result |
| Confirm/resume/retry | Existing valid decision state | `queued`, `queued_action` set; `queue_origin` records the prior state | Restore origin and revalidate the action under the same lock, then existing executor transitions |
| Ready node | `pending` | Node `queued`; conversation `queued`; no call ID or budget charge yet | After lane acquisition, revalidate, charge, commit running call, invoke, verify receipt, accept |
| Reconciliation | Original unresolved call | Conversation `queued`; original call remains `interrupted`/`running` | Acquire original capability's lane, reconcile original ID only |

Ordering: runner admission reservation → conversation registry → short store
transaction. A lane can be acquired while holding the conversation lock, but
never while holding a store transaction. Capability code executes on the same
thread that owns the reentrant conversation lock, so checkpoint callbacks do
not deadlock. Registry's bookkeeping mutex is never held while waiting for an
entry lock. GET/status/view requests read committed SQLite snapshots without
the conversation operation lock.

### Configuration contract

Invalid, empty, signed, non-integer, zero-for-positive, or out-of-range values
stop startup with `ValueError` naming the variable. Do not clamp or interpret
zero as unlimited. Whitespace around an otherwise valid integer is accepted.

| Environment variable | Default | Inclusive bounds |
|---|---:|---:|
| `FLIGHT_PLANNING_WORKERS` | 4 | 1–32 |
| `FLIGHT_PLANNING_QUEUE` | 32 | 0–256 |
| `FLIGHT_LOCAL_MATLAB_SLOTS` | 1 | 1–8 |
| `FLIGHT_REMOTE_WORKER_SLOTS` | 1 | 1–16 |
| `FLIGHT_READ_ONLY_SLOTS` | 4 | 1–32 |
| `FLIGHT_EXECUTION_TURNS` | 32 | 1–64 |

Also require `execution_turns >= local_matlab_slots + remote_worker_slots +
read_only_slots`. Raising the local limit is explicit operator configuration,
not inferred from a project, request, model response, or capability input.
Remote limits bound host-side invocations; they do not increase the remote
Worker service's own MATLAB capacity.

## File Structure

Line anchors below refer to the Stage 1 authoring checkout; Stage 2 may move
them. Use the named class/method as the authoritative edit anchor.

| File | Responsibility |
|---|---|
| Create `src/flight_agent/runtime/conversation_locks.py` | Validated-ID, ref-counted reentrant locks |
| Create `src/flight_agent/runtime/scheduling_contracts.py` | Server-owned execution-lane declaration and effect validation |
| Create `src/flight_agent/runtime/scheduling.py` | Admission reservations, bounded planning/turn executors, execution permits |
| Modify `src/flight_agent/config.py` | Validated scheduler environment settings |
| Modify `src/flight_agent/runtime/capabilities.py:23-54,68-112` | Copy scheduling metadata into registry entries and expose it to scheduler |
| Modify `src/flight_agent/verification/adapters/local_matlab.py:285-410` | Declare local lane for all four MATLAB capabilities |
| Modify `src/flight_agent/verification/adapters/simulink.py:295-325` | Declare remote lane |
| Modify `src/flight_agent/verification/adapters/evaluation.py:155-180` | Declare read-only lane |
| Modify `src/flight_agent/verification/adapters/delivery.py:290-320` | Declare read-only/general lane while retaining write permission |
| Modify `src/flight_agent/verification/adapters/diagnostics.py:279-310` | Declare read-only/general lane |
| Modify `src/flight_agent/verification/contracts.py:235-332` | Queued conversation/node statuses and queued-decision origin |
| Modify `src/flight_agent/verification/coordinator.py:25-258` | Per-conversation intake/planning/decision serialization |
| Modify `src/flight_agent/verification/agent_runtime.py:337-535,690-818` | Same registry, per-invocation permits, serial task mutation |
| Modify `src/flight_agent/verification/routes.py:73-190` | Reserved admission, typed saturation, no manager-wide lock |
| Modify `src/flight_agent/web.py:47-105` | Shared settings/runners, injected runtime validation, drain all store users |
| Create `src/flight_agent/verification/scheduling_recovery.py` | Pure recovery transition for queued/active aggregates |
| Modify `src/flight_agent/verification/sqlite_schema.py` | Add version 2 scheduling-status migration without changing migration 1 |
| Modify `src/flight_agent/verification/sqlite_codec.py` | Round-trip queued fields and the indexed scheduling-status overlay before runtime integration |
| Modify `src/flight_agent/verification/sqlite_store.py` | Extend Stage 2 startup recovery, including hidden running calls |
| Modify `src/flight_agent/verification/views.py:118-130,188-201` | Expose queued action consistently in view |
| Modify `src/flight_agent/static/agent.js:5-11,87-89,102-113` | Queued labels, disabled controls and continued polling |
| Modify `src/flight_agent/cli.py:111-122` | Poll queued conversations |
| Modify `.env.example`, `docs/usage.md` | Exact configuration and lifecycle contract |
| Create `tests/test_conversation_locks.py` | Lock overlap, serialization, identity and cleanup |
| Create `tests/test_scheduling_contracts.py` | Lane declarations, effects, and hash compatibility |
| Create `tests/test_scheduler_settings.py` | Bounds and environment parsing |
| Create `tests/test_scheduling.py` | Queueing, permits, reservation rollback and close |
| Create `tests/scheduling_helpers.py` | Deterministic fake-only runtime and scheduler fixtures |
| Create `tests/test_scheduler_runtime.py` | Concurrent planning and serialized task mutation |
| Create `tests/test_scheduler_persistence.py` | Upgrade from Stage 2 schema and round-trip queued metadata |
| Modify `tests/test_sqlite_schema.py` | Keep schema tests relative to the supported migration version |
| Create `tests/test_scheduler_web.py` | HTTP admission, queued states, shutdown |
| Create `tests/test_scheduler_recovery.py` | Queued/active SQLite restart boundaries |
| Modify `tests/test_verification_intake.py`, `tests/test_agent_web.py`, `tests/test_cli.py`, `tests/test_agent_browser.py` | Replace obsolete global-busy/intake assumptions; exercise queued consumers |

Do not change `runtime/capability_metadata.py`, `runtime/agent_contracts.py`,
baseline fingerprints, invocation fingerprints, or evidence serialization to
add scheduling fields. Scheduling metadata is trusted registration metadata,
not numerical/authorization metadata in `CapabilitySnapshot`; old baseline and
receipt hashes must remain byte-for-byte reproducible. The supported Worker
transport retains its existing `BackgroundRunner` and its own backpressure;
do not remove `background.py` merely because Web no longer imports it.

### Task 1: Validated, Leak-Free Conversation Locks

**Files:**
- Create: `src/flight_agent/runtime/conversation_locks.py`
- Test: `tests/test_conversation_locks.py`

**Interfaces:**
- Consumes: `flight_agent.models.JOB_ID: re.Pattern[str]`.
- Produces: `ConversationLockRegistry.hold(conversation_id: str) -> AbstractContextManager[None]`.
- Produces: `ConversationLockRegistry.entry_count: int`, a diagnostic for cleanup tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_conversation_locks.py
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from flight_agent.runtime.conversation_locks import ConversationLockRegistry

A, B = "a" * 32, "b" * 32


def test_different_conversations_overlap():
    locks = ConversationLockRegistry()
    barrier = Barrier(2)

    def run(identifier):
        with locks.hold(identifier):
            barrier.wait(timeout=5)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, identifier) for identifier in (A, B)]
        for future in futures:
            future.result(timeout=5)
    assert locks.entry_count == 0


def test_same_conversation_waiter_keeps_entry_alive():
    locks = ConversationLockRegistry()
    attempted, entered = Event(), Event()

    def waiter():
        attempted.set()
        with locks.hold(A):
            entered.set()

    with ThreadPoolExecutor(1) as pool:
        with locks.hold(A):
            future = pool.submit(waiter)
            assert attempted.wait(5)
            assert not entered.is_set()
            assert locks.entry_count == 1
            with locks.hold(A):
                assert locks.entry_count == 1
        future.result(timeout=5)
    assert entered.is_set()
    assert locks.entry_count == 0


def test_exception_and_repeated_conversations_release_entries():
    locks = ConversationLockRegistry()
    for number in range(1000):
        with pytest.raises(RuntimeError, match="failed turn"):
            with locks.hold(f"{number:032x}"):
                raise RuntimeError("failed turn")
    assert locks.entry_count == 0


@pytest.mark.parametrize("identifier", ["../task", "/tmp/project", "A" * 32, "", "a" * 31])
def test_invalid_identity_is_rejected_without_allocating(identifier):
    locks = ConversationLockRegistry()
    with pytest.raises(ValueError, match="conversation ID"):
        with locks.hold(identifier):
            pytest.fail("invalid identity acquired a lock")
    assert locks.entry_count == 0
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `python -m pytest tests/test_conversation_locks.py -q`

Expected: collection fails with
`ModuleNotFoundError: No module named 'flight_agent.runtime.conversation_locks'`.

- [ ] **Step 3: Implement the registry**

```python
# src/flight_agent/runtime/conversation_locks.py
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock

from ..models import JOB_ID


@dataclass
class _Entry:
    lock: RLock = field(default_factory=RLock)
    references: int = 0


class ConversationLockRegistry:
    def __init__(self) -> None:
        self._guard = Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def entry_count(self) -> int:
        with self._guard:
            return len(self._entries)

    @contextmanager
    def hold(self, conversation_id: str) -> Iterator[None]:
        if not isinstance(conversation_id, str) or not JOB_ID.fullmatch(conversation_id):
            raise ValueError("Invalid conversation ID")
        with self._guard:
            entry = self._entries.setdefault(conversation_id, _Entry())
            entry.references += 1
        acquired = False
        try:
            entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._guard:
                entry.references -= 1
                if entry.references == 0:
                    del self._entries[conversation_id]
```

The reference increment happens before waiting. Removing entries only on an
unlocked check would permit two different locks for one ID while waiters exist.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_conversation_locks.py -q`

Expected: all tests pass, including cleanup after exceptions and nested holds.

- [ ] **Step 5: Commit the independently tested lock primitive**

```bash
git add src/flight_agent/runtime/conversation_locks.py tests/test_conversation_locks.py
git commit -m "feat: add reference-counted conversation locks"
```

### Task 2: Server-Owned Capability Lane Declarations

**Files:**
- Create: `src/flight_agent/runtime/scheduling_contracts.py`
- Modify: `src/flight_agent/runtime/capabilities.py:23-54,68-112,119-121`
- Modify: `src/flight_agent/verification/adapters/local_matlab.py:285-410`
- Modify: `src/flight_agent/verification/adapters/simulink.py:295-325`
- Modify: `src/flight_agent/verification/adapters/evaluation.py:155-180`
- Modify: `src/flight_agent/verification/adapters/delivery.py:290-320`
- Modify: `src/flight_agent/verification/adapters/diagnostics.py:279-310`
- Test: `tests/test_scheduling_contracts.py`

**Interfaces:**
- Consumes: `CapabilityDefinition.effect: Effect`; `CapabilityRegistry.register(capability) -> None`.
- Produces: `ExecutionLane = Literal["local_matlab", "remote_worker", "read_only"]`.
- Produces: frozen `SchedulingMetadata(lane: ExecutionLane)`.
- Produces: `validate_scheduling(effect: Effect, metadata: SchedulingMetadata | None) -> SchedulingMetadata`.
- Produces: optional `Capability.scheduling: SchedulingMetadata | None = None` as its last dataclass field.
- Produces: `CapabilityRegistry.scheduling(name: str) -> SchedulingMetadata`.
- Does not alter: `CapabilitySnapshot`, `CapabilityRegistry.snapshot()`, `catalog()`, or confirmed input schemas.

- [ ] **Step 1: Write lane-authority and snapshot-compatibility tests**

```python
# tests/test_scheduling_contracts.py
from dataclasses import replace

import pytest

from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import CapabilityMetadata
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata, validate_scheduling
from tests.agent_helpers import RmseInput


def capability(effect="read_only", scheduling=None):
    return Capability(
        name="synthetic", version="1", description="Synthetic bounded operation.",
        input_model=RmseInput, output_model=CapabilityOutcome, effect=effect,
        operation=lambda context, inputs: CapabilityOutcome(),
        metadata=CapabilityMetadata(), scheduling=scheduling,
    )


@pytest.mark.parametrize("effect,lane", [
    ("execute_project", "local_matlab"),
    ("execute_project", "remote_worker"),
    ("read_only", "read_only"),
    ("write_delivery", "read_only"),
])
def test_declared_effect_and_lane_are_validated(effect, lane):
    registry = CapabilityRegistry(agent_mode=True)
    registry.register(capability(effect, SchedulingMetadata(lane)))
    assert registry.scheduling("synthetic").lane == lane


@pytest.mark.parametrize("effect,lane", [
    ("execute_project", "read_only"),
    ("read_only", "local_matlab"),
    ("write_delivery", "remote_worker"),
    ("write_candidate", "read_only"),
])
def test_effect_cannot_downgrade_or_invent_an_execution_lane(effect, lane):
    with pytest.raises(ValueError, match="scheduling"):
        validate_scheduling(effect, SchedulingMetadata(lane))


def test_execute_project_requires_a_declaration_and_invalid_lanes_fail_closed():
    with pytest.raises(ValueError, match="scheduling"):
        validate_scheduling("execute_project", None)
    with pytest.raises(ValueError, match="scheduling"):
        validate_scheduling("execute_project", SchedulingMetadata("planning"))


def test_read_only_default_does_not_change_snapshot_or_receipt_identity():
    first, second = CapabilityRegistry(agent_mode=True), CapabilityRegistry(agent_mode=True)
    original = capability()
    first.register(original)
    second.register(replace(original, scheduling=SchedulingMetadata("read_only")))
    expected = first.snapshot("synthetic").model_dump(mode="json")
    assert second.snapshot("synthetic").model_dump(mode="json") == expected
    actual = second.snapshot("synthetic").model_dump(mode="json")
    assert fingerprint(actual) == fingerprint(expected)
    assert "scheduling" not in second.catalog()[0]
    assert "scheduling" not in second.snapshot("synthetic").metadata.model_dump()
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_scheduling_contracts.py -q`

Expected: missing `flight_agent.runtime.scheduling_contracts`.

- [ ] **Step 3: Implement the declaration validation and registry boundary**

```python
# src/flight_agent/runtime/scheduling_contracts.py
from dataclasses import dataclass
from typing import Literal

from .contracts import Effect

ExecutionLane = Literal["local_matlab", "remote_worker", "read_only"]


@dataclass(frozen=True)
class SchedulingMetadata:
    lane: ExecutionLane


def validate_scheduling(
    effect: Effect, metadata: SchedulingMetadata | None,
) -> SchedulingMetadata:
    if metadata is None and effect in ("read_only", "write_delivery"):
        metadata = SchedulingMetadata("read_only")
    if metadata is None or metadata.lane not in ("local_matlab", "remote_worker", "read_only"):
        raise ValueError("Capability requires valid scheduling metadata")
    if effect == "execute_project" and metadata.lane in ("local_matlab", "remote_worker"):
        return SchedulingMetadata(metadata.lane)
    if effect in ("read_only", "write_delivery") and metadata.lane == "read_only":
        return SchedulingMetadata("read_only")
    raise ValueError("Capability effect conflicts with scheduling metadata")
```

In `capabilities.py`, import `SchedulingMetadata, validate_scheduling`. Append
`scheduling: SchedulingMetadata | None = None` to `Capability` and append
`scheduling: SchedulingMetadata | None` to `_Entry`. Immediately before building
the `_Entry`, compute:

```python
scheduling = (
    validate_scheduling(definition.effect, capability.scheduling)
    if self._agent_mode or metadata is not None
    else capability.scheduling
)
```

Append `scheduling` as the final `_Entry(...)` argument. Non-agent registry
users retain their existing candidate effects and do not gain scheduling
authority. Add this method after `definition()`:

```python
def scheduling(self, name: str) -> SchedulingMetadata:
    entry = self._entry(name)
    return validate_scheduling(entry.definition.effect, entry.scheduling)
```

Import `SchedulingMetadata` in each adapter and add the exact keyword to its
`Capability(...)` registration:

| Registered capability | Keyword |
|---|---|
| `matlab_execute_project` | `scheduling=SchedulingMetadata("local_matlab")` |
| `matlab_stepinfo` | `scheduling=SchedulingMetadata("local_matlab")` |
| `matlab_linearize` | `scheduling=SchedulingMetadata("local_matlab")` |
| `matlab_allmargin` | `scheduling=SchedulingMetadata("local_matlab")` |
| `simulink_step_response` and `simulink_stability_margins` | `scheduling=SchedulingMetadata("remote_worker")` in their shared `capability(kind)` factory |
| `evaluate_criteria` | `scheduling=SchedulingMetadata("read_only")` |
| `deliver_results` | `scheduling=SchedulingMetadata("read_only")` |
| `offline_diagnosis` | `scheduling=SchedulingMetadata("read_only")` |

Use the factory registration in the named adapter, not a map keyed by tool name
inside the scheduler. `write_delivery` remains required and authorized even
though delivery uses the general/read-only resource lane. Do not add a lane
parameter to `AgentContext`, `AgentNode`, request bodies, or input models.

- [ ] **Step 4: Verify GREEN and preserve existing authorization tests**

```bash
python -m pytest tests/test_scheduling_contracts.py tests/test_agent_capabilities.py tests/test_runtime_kernel.py tests/test_agent_adapters.py tests/test_local_matlab_adapters.py -q
```

Expected: pass; existing factory names and snapshots retain their existing
numerical contracts. Read-only synthetic capabilities retain the validated
read-only default. Do not introduce an implicit default for `execute_project`.

- [ ] **Step 5: Commit the lane contract**

```bash
git add src/flight_agent/runtime/scheduling_contracts.py src/flight_agent/runtime/capabilities.py src/flight_agent/verification/adapters/local_matlab.py src/flight_agent/verification/adapters/simulink.py src/flight_agent/verification/adapters/evaluation.py src/flight_agent/verification/adapters/delivery.py src/flight_agent/verification/adapters/diagnostics.py tests/test_scheduling_contracts.py
git commit -m "feat: declare server-owned capability execution lanes"
```

### Task 3: Safe Settings, Bounded Queues, and Execution Permits

**Files:**
- Modify: `src/flight_agent/config.py`, append `SchedulerSettings`
- Create: `src/flight_agent/runtime/scheduling.py`
- Test: `tests/test_scheduler_settings.py`
- Test: `tests/test_scheduling.py`

**Interfaces:**
- Consumes: `SchedulingMetadata`, `validate_scheduling()`, `CapabilityDefinition`, `BudgetExhausted`.
- Produces: frozen `SchedulerSettings` with the six integer fields in the configuration table and `from_environment() -> SchedulerSettings`.
- Produces: `CapacityExceeded(RuntimeError)`, `SchedulerClosed(RuntimeError)`.
- Produces: `BoundedRunner.reserve() -> AbstractContextManager[Submission]`, `Submission.submit(operation: Callable[[], None]) -> Future[None]`, `BoundedRunner.close() -> None`.
- Produces: `PlanningScheduler(workers: int, queued: int)` and `ExecutionTurnScheduler(turns: int)`.
- Produces: `ExecutionScheduler(settings: SchedulerSettings).slot(definition: CapabilityDefinition, metadata: SchedulingMetadata, *, timeout: float | None = None) -> AbstractContextManager[None]`.

- [ ] **Step 1: Write failing settings and scheduling tests**

```python
# tests/test_scheduler_settings.py
from dataclasses import replace

import pytest

from flight_agent.config import SchedulerSettings

VARIABLES = {
    "FLIGHT_PLANNING_WORKERS": ("planning_workers", 4, 1, 32),
    "FLIGHT_PLANNING_QUEUE": ("planning_queue", 32, 0, 256),
    "FLIGHT_LOCAL_MATLAB_SLOTS": ("local_matlab_slots", 1, 1, 8),
    "FLIGHT_REMOTE_WORKER_SLOTS": ("remote_worker_slots", 1, 1, 16),
    "FLIGHT_READ_ONLY_SLOTS": ("read_only_slots", 4, 1, 32),
    "FLIGHT_EXECUTION_TURNS": ("execution_turns", 32, 1, 64),
}


def clean(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_defaults_preserve_four_planners_and_one_local_matlab(monkeypatch):
    clean(monkeypatch)
    settings = SchedulerSettings.from_environment()
    for field, default, lower, upper in VARIABLES.values():
        assert getattr(settings, field) == default


@pytest.mark.parametrize("variable", list(VARIABLES))
@pytest.mark.parametrize("value", ["", " ", "0.5", "inf", "NaN", "-1", "+2", "999999"])
def test_invalid_environment_is_not_silently_clamped(monkeypatch, variable, value):
    clean(monkeypatch)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        SchedulerSettings.from_environment()


@pytest.mark.parametrize("variable", list(VARIABLES))
def test_both_inclusive_bounds_are_accepted(monkeypatch, variable):
    clean(monkeypatch)
    monkeypatch.setenv("FLIGHT_EXECUTION_TURNS", "64")
    field, default, lower, upper = VARIABLES[variable]
    for value in (lower, upper):
        if variable == "FLIGHT_EXECUTION_TURNS" and value == 1:
            for name in ("FLIGHT_LOCAL_MATLAB_SLOTS", "FLIGHT_REMOTE_WORKER_SLOTS",
                         "FLIGHT_READ_ONLY_SLOTS"):
                monkeypatch.setenv(name, "1")
            with pytest.raises(ValueError, match="FLIGHT_EXECUTION_TURNS"):
                monkeypatch.setenv(variable, "1")
                SchedulerSettings.from_environment()
            continue
        monkeypatch.setenv(variable, f" {value} ")
        assert getattr(SchedulerSettings.from_environment(), field) == value


def test_direct_settings_construction_cannot_bypass_bounds():
    with pytest.raises(ValueError, match="FLIGHT_LOCAL_MATLAB_SLOTS"):
        replace(SchedulerSettings(), local_matlab_slots=0)
    with pytest.raises(ValueError, match="FLIGHT_EXECUTION_TURNS"):
        replace(SchedulerSettings(), execution_turns=2)
```

```python
# tests/test_scheduling.py
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from flight_agent.config import SchedulerSettings
from flight_agent.runtime.budget import BudgetExhausted
from flight_agent.runtime.capabilities import CapabilityDefinition
from flight_agent.runtime.scheduling import (
    CapacityExceeded,
    ExecutionScheduler,
    PlanningScheduler,
    SchedulerClosed,
)
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata


def definition(effect="execute_project"):
    return CapabilityDefinition(name="synthetic", version="1", description="Test.", effect=effect)


def test_planning_queue_accepts_waiting_work_without_global_busy():
    runner = PlanningScheduler(1, 1)
    entered, release, second = Event(), Event(), Event()

    def first():
        entered.set()
        assert release.wait(5)

    try:
        with runner.reserve() as reservation:
            first_future = reservation.submit(first)
        assert entered.wait(5)
        with runner.reserve() as reservation:
            second_future = reservation.submit(second.set)
        assert not second.is_set()
        with pytest.raises(CapacityExceeded):
            with runner.reserve():
                pytest.fail("over-capacity work was admitted")
        release.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)
    finally:
        release.set()
        runner.close()
    assert second.is_set()
    with pytest.raises(SchedulerClosed):
        with runner.reserve():
            pytest.fail("closed scheduler admitted work")


def test_unused_and_failed_reservations_release_capacity():
    runner = PlanningScheduler(1, 0)
    try:
        with pytest.raises(ValueError, match="invalid request"):
            with runner.reserve():
                raise ValueError("invalid request")
        with runner.reserve() as reservation:
            future = reservation.submit(lambda: (_ for _ in ()).throw(RuntimeError("failure")))
        with pytest.raises(RuntimeError, match="failure"):
            future.result(timeout=5)
        with runner.reserve() as reservation:
            reservation.submit(lambda: None).result(timeout=5)
    finally:
        runner.close()
        runner.close()


def test_local_wait_does_not_occupy_remote_or_read_only_permits():
    scheduler = ExecutionScheduler(SchedulerSettings())
    attempted, remote, readonly = Event(), Event(), Event()

    def wait_local():
        attempted.set()
        with pytest.raises(BudgetExhausted):
            with scheduler.slot(definition(), SchedulingMetadata("local_matlab"), timeout=0):
                pytest.fail("a second local call acquired the default single slot")

    def other(lane, effect, event):
        with scheduler.slot(definition(effect), SchedulingMetadata(lane), timeout=0):
            event.set()

    with scheduler.slot(definition(), SchedulingMetadata("local_matlab")):
        with ThreadPoolExecutor(3) as pool:
            futures = [
                pool.submit(wait_local),
                pool.submit(other, "remote_worker", "execute_project", remote),
                pool.submit(other, "read_only", "read_only", readonly),
            ]
            for future in futures:
                future.result(timeout=5)
        assert attempted.is_set() and remote.is_set() and readonly.is_set()


def test_lane_permit_is_released_after_exception():
    scheduler = ExecutionScheduler(SchedulerSettings())
    with pytest.raises(RuntimeError, match="known failure"):
        with scheduler.slot(definition(), SchedulingMetadata("local_matlab")):
            raise RuntimeError("known failure")
    with scheduler.slot(definition(), SchedulingMetadata("local_matlab"), timeout=0):
        pass
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_scheduler_settings.py tests/test_scheduling.py -q
```

Expected: missing `SchedulerSettings` and scheduling module during collection.

- [ ] **Step 3: Add exact bounded settings**

Append to `config.py`; reuse its existing `os` and `dataclass` imports:

```python
_SCHEDULER_FIELDS = {
    "FLIGHT_PLANNING_WORKERS": ("planning_workers", 4, 1, 32),
    "FLIGHT_PLANNING_QUEUE": ("planning_queue", 32, 0, 256),
    "FLIGHT_LOCAL_MATLAB_SLOTS": ("local_matlab_slots", 1, 1, 8),
    "FLIGHT_REMOTE_WORKER_SLOTS": ("remote_worker_slots", 1, 1, 16),
    "FLIGHT_READ_ONLY_SLOTS": ("read_only_slots", 4, 1, 32),
    "FLIGHT_EXECUTION_TURNS": ("execution_turns", 32, 1, 64),
}


@dataclass(frozen=True)
class SchedulerSettings:
    planning_workers: int = 4
    planning_queue: int = 32
    local_matlab_slots: int = 1
    remote_worker_slots: int = 1
    read_only_slots: int = 4
    execution_turns: int = 32

    def __post_init__(self) -> None:
        for variable, (field, default, lower, upper) in _SCHEDULER_FIELDS.items():
            value = getattr(self, field)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{variable} must be an integer between {lower} and {upper}")
        if self.execution_turns < (
            self.local_matlab_slots + self.remote_worker_slots + self.read_only_slots
        ):
            raise ValueError("FLIGHT_EXECUTION_TURNS must cover the sum of execution lane slots")

    @classmethod
    def from_environment(cls) -> "SchedulerSettings":
        values: dict[str, int] = {}
        for variable, (field, default, lower, upper) in _SCHEDULER_FIELDS.items():
            text = os.environ.get(variable, str(default)).strip()
            if not text or not text.isascii() or not text.isdecimal():
                raise ValueError(f"{variable} must be an integer between {lower} and {upper}")
            value = int(text)
            if not lower <= value <= upper:
                raise ValueError(f"{variable} must be an integer between {lower} and {upper}")
            values[field] = value
        return cls(**values)
```

- [ ] **Step 4: Implement reservations, independent runners, and resource permits**

```python
# src/flight_agent/runtime/scheduling.py
import math
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import BoundedSemaphore, Condition, Lock

from ..config import SchedulerSettings
from .budget import BudgetExhausted
from .capabilities import CapabilityDefinition
from .scheduling_contracts import SchedulingMetadata, validate_scheduling


class CapacityExceeded(RuntimeError):
    pass


class SchedulerClosed(RuntimeError):
    pass


class Submission:
    def __init__(self, owner: "BoundedRunner") -> None:
        self._owner = owner
        self._submitted = False
        self._released = False

    def _release(self) -> None:
        with self._owner._condition:
            if not self._released:
                self._released = True
                self._owner._in_use -= 1
                self._owner._condition.notify_all()

    def submit(self, operation: Callable[[], None]) -> Future[None]:
        with self._owner._condition:
            if self._submitted or self._released:
                raise RuntimeError("Submission reservation has already been consumed")
            future = self._owner._pool.submit(self._run, operation)
            self._submitted = True
            return future

    def _run(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        finally:
            self._release()


class BoundedRunner:
    def __init__(self, workers: int, capacity: int, prefix: str) -> None:
        if workers < 1 or capacity < workers:
            raise ValueError("Runner capacity must cover positive worker count")
        self._condition = Condition()
        self._close_lock = Lock()
        self._in_use = 0
        self._capacity = capacity
        self._closing = False
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)

    @contextmanager
    def reserve(self) -> Iterator[Submission]:
        with self._condition:
            if self._closing:
                raise SchedulerClosed("Scheduler is shutting down")
            if self._in_use >= self._capacity:
                raise CapacityExceeded("Scheduler admission capacity is exhausted")
            self._in_use += 1
        reservation = Submission(self)
        try:
            yield reservation
        finally:
            if not reservation._submitted:
                reservation._release()

    def close(self) -> None:
        with self._close_lock:
            with self._condition:
                self._closing = True
                while self._in_use:
                    self._condition.wait()
            self._pool.shutdown(wait=True, cancel_futures=False)


class PlanningScheduler(BoundedRunner):
    def __init__(self, workers: int, queued: int) -> None:
        if not 1 <= workers <= 32 or not 0 <= queued <= 256:
            raise ValueError("Planning scheduler bounds are invalid")
        super().__init__(workers, workers + queued, "flight-planning")


class ExecutionTurnScheduler(BoundedRunner):
    def __init__(self, turns: int) -> None:
        if not 1 <= turns <= 64:
            raise ValueError("Execution turn bound must be between 1 and 64")
        super().__init__(turns, turns, "flight-execution-turn")


class ExecutionScheduler:
    def __init__(self, settings: SchedulerSettings) -> None:
        self.settings = settings
        self._slots = {
            "local_matlab": BoundedSemaphore(settings.local_matlab_slots),
            "remote_worker": BoundedSemaphore(settings.remote_worker_slots),
            "read_only": BoundedSemaphore(settings.read_only_slots),
        }

    @contextmanager
    def slot(
        self, definition: CapabilityDefinition, metadata: SchedulingMetadata,
        *, timeout: float | None = None,
    ) -> Iterator[None]:
        declared = validate_scheduling(definition.effect, metadata)
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Lane timeout must be finite and nonnegative")
        semaphore = self._slots[declared.lane]
        acquired = semaphore.acquire() if timeout is None else semaphore.acquire(timeout=timeout)
        if not acquired:
            raise BudgetExhausted("The confirmed elapsed-time budget expired waiting for a lane")
        try:
            yield
        finally:
            semaphore.release()
```

A reservation covers both queued and running jobs. An already reserved producer
can publish during shutdown; `close()` waits for its reservation before shutting
down the underlying pool. Closing never cancels queued jobs or releases a
running MATLAB permit early.

- [ ] **Step 5: Verify GREEN**

```bash
python -m pytest tests/test_scheduler_settings.py tests/test_scheduling.py tests/test_scheduling_contracts.py tests/test_conversation_locks.py -q
python -m ruff check src/flight_agent/config.py src/flight_agent/runtime/conversation_locks.py src/flight_agent/runtime/scheduling_contracts.py src/flight_agent/runtime/scheduling.py
```

Expected: tests and targeted lint pass. Keep standard-library imports sorted.

- [ ] **Step 6: Commit the bounded scheduler primitives**

```bash
git add src/flight_agent/config.py src/flight_agent/runtime/scheduling.py tests/test_scheduler_settings.py tests/test_scheduling.py
git commit -m "feat: add bounded planning and execution scheduling primitives"
```

### Task 4: Isolate Runtime Operations and Queue Ready Nodes Before Dispatch

**Files:**
- Modify: `src/flight_agent/verification/contracts.py:235-332`
- Modify: `src/flight_agent/verification/sqlite_schema.py`, append migration 2
- Modify: `src/flight_agent/verification/sqlite_codec.py`, conversation header encode/decode
- Modify: `src/flight_agent/verification/coordinator.py:25-258`
- Modify: `src/flight_agent/verification/agent_runtime.py:337-535,690-818`
- Create: `tests/scheduling_helpers.py`
- Test: `tests/test_scheduler_runtime.py`
- Test: `tests/test_scheduler_persistence.py`
- Modify: `tests/test_sqlite_schema.py`
- Modify: `tests/test_verification_intake.py:42-44`

**Interfaces:**
- Consumes: Stage 2 `get/create/save/task_directory/default_task_root`, shared `ConversationLockRegistry`, `ExecutionScheduler.slot()`, registry `definition()/scheduling()`.
- Produces: `AgentExecutor(store: ConversationRepository, tools: CapabilityRegistry[AgentContext], resources: ResourceRegistry, *, locks: ConversationLockRegistry | None = None, scheduler: ExecutionScheduler | None = None)`.
- Produces: `AgentExecutor.locks`, `AgentExecutor.scheduler`, and `EngineeringRuntime.locks` referring to the executor's exact registry.
- Produces: `ConversationActive(ValueError)` in `coordinator.py`.
- Produces: `ScheduledAction = Literal["message", "confirm", "resume", "retry"]`, `Conversation.queued_action: ScheduledAction | None`, `Conversation.queue_origin: ConversationState | None`.
- Produces: schema version 2 with `conversations.scheduling_status`; codec round-trips both new domain fields.
- Changes: `EngineeringRuntime.queue_message(conversation_id: str | None, message: str, project_root: str | None = None, *, output_root: str | None = None, location: str | None = None) -> Conversation` now returns `queued` for a plannable message.
- Changes: `EngineeringRuntime.continue_submission(conversation_id: str) -> Conversation` promotes its admitted message to `planning`.
- Preserves: public `propose/confirm/resume/retry/decide` return types and their consent checks; `_run()` remains serial.

- [ ] **Step 1: Write deterministic runtime fixtures and failing concurrency tests**

```python
# tests/scheduling_helpers.py
from flight_agent.config import SchedulerSettings
from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.runtime.scheduling import ExecutionScheduler
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.coordinator import EngineeringRuntime
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import RmseInput, analysis_plan_data


def make_runtime(tmp_path, planner, operation, *, lane="read_only", settings=None):
    settings = settings or SchedulerSettings()
    effect = "read_only" if lane == "read_only" else "execute_project"
    tools = CapabilityRegistry(agent_mode=True)
    tools.register(Capability(
        name="offline_rmse", version="1", description="Fake scheduled metric.",
        input_model=RmseInput, output_model=CapabilityOutcome, effect=effect,
        operation=operation, scheduling=SchedulingMetadata(lane),
        metadata=CapabilityMetadata(metrics=[
            MetricDefinition(id="rmse", label="RMSE", unit="m"),
        ]),
    ))
    resources = ResourceRegistry()
    store = ConversationStore(tmp_path / "state")
    executor = AgentExecutor(store, tools, resources, scheduler=ExecutionScheduler(settings))
    runtime = EngineeringRuntime(store, planner, executor, resources)
    return runtime


def propose_fake(runtime, *, lane="read_only"):
    from flight_agent.runtime.agent_contracts import AgentPlan

    data = analysis_plan_data()
    data["allowed_effects"] = ["read_only" if lane == "read_only" else "execute_project"]
    record = runtime.store.create()
    return runtime.executor.propose(record, AgentPlan.model_validate(data))
```

```python
# tests/test_scheduler_runtime.py
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.agent_planner import AgentDecision
from tests.scheduling_helpers import make_runtime, propose_fake


def test_two_conversations_plan_concurrently(tmp_path):
    barrier = Barrier(2)

    class Planner:
        def propose(self, message, conversation, **kwargs):
            barrier.wait(timeout=5)
            return AgentDecision(action="answer", text=f"Answered {message}")

    runtime = make_runtime(tmp_path, Planner(), lambda context, inputs: CapabilityOutcome())
    a = runtime.queue_message(None, "First question")
    b = runtime.queue_message(None, "Second question")
    assert a.status == b.status == "queued"
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(runtime.continue_submission, item.id) for item in (a, b)]
        assert [future.result(timeout=5).status for future in futures] == ["completed"] * 2
    assert runtime.locks is runtime.executor.locks
    assert runtime.locks.entry_count == 0


def test_same_conversation_confirm_is_serial_and_idempotent(tmp_path):
    entered, release, attempted = Event(), Event(), Event()
    calls = []

    def operation(context, inputs):
        calls.append(context.call_id)
        entered.set()
        assert release.wait(5)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, None, operation)
    pending = propose_fake(runtime)

    def confirm():
        return runtime.executor.confirm(pending.id, 1, trusted=True)

    def second():
        attempted.set()
        return confirm()

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(confirm)
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert attempted.wait(5)
            assert not other.done()
        finally:
            release.set()
        assert first.result(timeout=5).status == "completed"
        assert other.result(timeout=5).status == "completed"
    assert len(calls) == 1
    assert len(runtime.store.get(pending.id).task.calls) == 1
    assert runtime.locks.entry_count == 0


def test_waiting_local_node_has_no_dispatched_call_or_charged_budget(tmp_path):
    entered = Event()
    runtime = make_runtime(
        tmp_path, None,
        lambda context, inputs: (entered.set(), CapabilityOutcome())[1],
        lane="local_matlab",
    )
    pending = propose_fake(runtime, lane="local_matlab")
    scheduler = runtime.executor.scheduler
    definition = runtime.executor.tools.definition("offline_rmse")
    metadata = runtime.executor.tools.scheduling("offline_rmse")
    saved = Event()
    original_save = runtime.store.save

    def observe(record):
        original_save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            saved.set()

    runtime.store.save = observe
    with ThreadPoolExecutor(1) as pool:
        with scheduler.slot(definition, metadata):
            future = pool.submit(runtime.executor.confirm, pending.id, 1, trusted=True)
            assert saved.wait(5)
            queued = runtime.store.get(pending.id)
            assert queued.status == "queued"
            assert queued.task.calls == []
            assert queued.task.budget_state.calls == 0
            assert queued.task.budget_state.rounds == 0
            assert not entered.is_set()
        assert future.result(timeout=5).status == "completed"
    assert entered.is_set()
```

```python
# tests/test_scheduler_persistence.py
import json
import sqlite3

from flight_agent.verification import sqlite_schema
from flight_agent.verification.contracts import ConversationState
from flight_agent.verification.sqlite_store import SQLiteConversationStore


def test_queued_fields_round_trip_without_opening_a_second_store(tmp_path):
    store = SQLiteConversationStore(tmp_path / "state", require_empty=True)
    record = store.create()
    record.queue_origin = ConversationState(status=record.status)
    record.queued_action = "confirm"
    record.status = "queued"
    store.save(record)
    restored = store.get(record.id)
    assert restored.status == "queued"
    assert restored.queued_action == "confirm"
    assert restored.queue_origin == record.queue_origin
    with sqlite3.connect(store.database_path) as connection:
        status, scheduling, payload = connection.execute(
            "SELECT status, scheduling_status, state_json FROM conversations WHERE id=?",
            (record.id,),
        ).fetchone()
    assert status == "running"
    assert scheduling == "queued"
    assert json.loads(payload)["queued_action"] == "confirm"
    record.status = "waiting_for_user"
    record.queued_action = None
    record.queue_origin = None
    store.save(record)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT scheduling_status FROM conversations WHERE id=?", (record.id,),
        ).fetchone()[0] is None


def test_stage_two_database_upgrades_without_changing_migration_one(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    identifier = "d" * 32
    (root / identifier).mkdir()
    path = root / "conversations.sqlite3"
    migrations = sqlite_schema.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "MIGRATIONS", migrations[:1])
        sqlite_schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        digest = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=1"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO conversations
               (id, created_at, updated_at, status, pending_decision_json, state_json)
               VALUES (?, ?, ?, 'waiting_for_user', 'null', ?)""",
            (identifier, "2026-10-31T00:00:00+00:00", "2026-10-31T00:00:00+00:00",
             '{"schema_version":"2.0","output_root_reset":false,"turn_origin":null}'),
        )
    store = SQLiteConversationStore(root)
    restored = store.get(identifier)
    assert restored.status == "waiting_for_user"
    assert restored.queued_action is None
    assert restored.queue_origin is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=1"
        ).fetchone()[0] == digest
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_scheduler_runtime.py tests/test_scheduler_persistence.py -q`

Expected: `AgentExecutor.__init__()` rejects `scheduler`; after injection is
added, queued-state assertions and overlap barrier expose remaining global
locking. Persistence rejects the new domain status or lacks `scheduling_status`.
Do not accept a broken barrier as a passing concurrency test.

- [ ] **Step 3: Add queued domain state without touching baseline contracts**

In `verification/contracts.py`, replace the status aliases and add the two
fields after `turn_origin`:

```python
NodeStatus = Literal["pending", "queued", "running", "completed", "failed", "interrupted"]
ConversationStatus = Literal[
    "queued", "planning", "waiting_for_user", "running", "completed", "failed", "interrupted"
]
ScheduledAction = Literal["message", "confirm", "resume", "retry"]

# Fields inside Conversation:
queued_action: ScheduledAction | None = None
queue_origin: ConversationState | None = None
```

`ToolCall.status` is unchanged. Waiting for a lane is not an external invocation.

- [ ] **Step 4: Persist queued state through an additive versioned migration**

In `sqlite_schema.py`, change only the assignment prefix `MIGRATIONS =` to
`MIGRATIONS: tuple[Migration, ...] =`, retaining its complete existing
right-hand side and checksum. After that assignment append:

```python
MIGRATIONS = (*MIGRATIONS, Migration(2, "scheduler-queued-state", (
    """ALTER TABLE conversations ADD COLUMN scheduling_status TEXT
       CHECK (scheduling_status IS NULL OR scheduling_status='queued')""",
    """CREATE INDEX conversations_scheduling_status
       ON conversations(scheduling_status, status, updated_at, id)""",
)))
```

Keep every byte of migration 1's statements unchanged.
Do not rebuild the foreign-key-linked conversations table merely to widen its
original status check. Use the normal-column overlay for queued state:
`COALESCE(scheduling_status, status)` is the logical conversation status.
The old column is an internal compatibility projection (`planning` for a queued
message; `running` for queued execution), not a claim that execution started.
Public state and recovery always use the overlay.

Apply these exact changes to `sqlite_codec.encode()`'s conversation row:

```diff
-        "status": record.status, "project_root": record.project_root,
+        "status": (
+            "planning" if record.queued_action == "message" else "running"
+        ) if record.status == "queued" else record.status,
+        "scheduling_status": "queued" if record.status == "queued" else None,
+        "project_root": record.project_root,
@@
-            key: data[key] for key in ("schema_version", "output_root_reset", "turn_origin")
+            key: data[key] for key in (
+                "schema_version", "output_root_reset", "turn_origin",
+                "queued_action", "queue_origin",
+            )
```

In `sqlite_codec.decode()`, immediately after `data.update()` copies the
conversation header and before decoding `pending_decision`, add:

```python
data["status"] = header["scheduling_status"] or header["status"]
```

New fields default to `None` when decoding pre-upgrade `state_json`. Task state
already includes execution nodes, so queued nodes require no new task column.
Do not add queued state to immutable baselines, invocation specs, or receipts.

Make Stage 2's schema tests version-relative rather than freezing them at
version 1. In `test_schema_is_versioned_idempotent_and_wal`, change its
`PRAGMA user_version` expectation to `schema.MIGRATIONS[-1].version`.
In `test_future_schema_is_refused_without_rewriting_it`, change the migration
count expectation to `len(schema.MIGRATIONS)`. In the two upgrade-probe tests,
make these exact replacements:

```diff
 def test_failed_upgrade_rolls_back_ddl_and_version(tmp_path, monkeypatch):
@@
-    broken = schema.Migration(2, "failure-probe", (
+    before = schema.MIGRATIONS[-1].version
+    broken = schema.Migration(before + 1, "failure-probe", (
@@
-        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
+        assert connection.execute("PRAGMA user_version").fetchone()[0] == before
@@
-        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
+        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == before

 def test_next_migration_preserves_existing_data(tmp_path, monkeypatch):
@@
-    upgrade = schema.Migration(2, "upgrade-probe", (
+    before = schema.MIGRATIONS[-1].version
+    upgrade = schema.Migration(before + 1, "upgrade-probe", (
@@
-        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
-        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
+        assert connection.execute("PRAGMA user_version").fetchone()[0] == before + 1
+        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == before + 1
```

Keep the explicit version 1 → 2 upgrade test in `test_scheduler_persistence.py`;
it tests this stage's real migration, not a synthetic future upgrade.

- [ ] **Step 5: Replace executor-wide locking with the shared registry**

Remove `threading`, the `_operation()` context manager, and their now-unused
`Iterator/contextmanager/BusyError` imports from `agent_runtime.py`. Import
`SchedulerSettings`, `ConversationLockRegistry`, and `ExecutionScheduler`.
Replace its constructor with:

```python
def __init__(
    self, store: ConversationRepository, tools: CapabilityRegistry[AgentContext],
    resources: ResourceRegistry, *,
    locks: ConversationLockRegistry | None = None,
    scheduler: ExecutionScheduler | None = None,
) -> None:
    self.store = store
    self.tools = tools
    self.resources = resources
    self.locks = locks if locks is not None else ConversationLockRegistry()
    self.scheduler = scheduler if scheduler is not None else ExecutionScheduler(
        SchedulerSettings.from_environment()
    )
```

Apply these exact entry-guard replacements; keep each existing guarded body:

```diff
 def propose(self, conversation: Conversation, plan: AgentPlan) -> Conversation:
-    with self._operation():
+    with self.locks.hold(conversation.id):
+        conversation = self.store.get(conversation.id)

 def confirm(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
-    with self._operation():
+    with self.locks.hold(conversation_id):

 def resume(self, conversation_id: str) -> Conversation:
-    with self._operation():
+    with self.locks.hold(conversation_id):

 def retry(self, conversation_id: str, version: int, *, trusted: bool) -> Conversation:
-    with self._operation():
+    with self.locks.hold(conversation_id):
```

Reloading in `propose()` prevents a caller's old aggregate from replacing newer
messages or a completed task. Keep `_run()`'s dependency readiness, receipt
verification, `_live_node()`, budget accounting, and confirmation fingerprint
checks unchanged.

- [ ] **Step 6: Wrap dispatch and reconciliation with queued resource admission**

Rename the existing `_dispatch()` implementation to `_dispatch_admitted()`;
do not change its body. Insert this new `_dispatch()` before it:

```python
def _dispatch(
    self, conversation: Conversation, task: AgentTask,
    node: AgentExecutionNode, budget: BudgetAccount,
) -> None:
    self._live_node(task, node)
    definition = self.tools.definition(node.tool)
    scheduling = self.tools.scheduling(node.tool)
    conversation.status = "queued"
    node.status = "queued"
    task.stage = f"queued:{scheduling.lane}:{node.id}"
    self.store.save(conversation)
    try:
        with self.scheduler.slot(
            definition, scheduling, timeout=budget.remaining_seconds(),
        ):
            budget.check_limits()
            conversation.status = "running"
            node.status = "pending"
            task.stage = node.id
            self.store.save(conversation)
            self._dispatch_admitted(conversation, task, node, budget)
    finally:
        if node.status == "queued":
            node.status = "pending"
            self.store.save(conversation)
```

Because `_dispatch_admitted()` still revalidates the live capability, resources,
inputs, and dependency outputs after permit acquisition, time spent queued does
not make stale validation authoritative. Its existing persisted-call-before-
operation sequence stays inside the permit. Cache reuse retains existing
call/round charges; it conservatively uses the declared lane too.

In `_recover()`, replace only the line invoking `self.tools.reconcile(...)`
with:

```python
scheduling = self.tools.scheduling(node.tool)
conversation.status = "queued"
task.stage = f"queued:{scheduling.lane}:{node.id}"
self.store.save(conversation)
with self.scheduler.slot(self.tools.definition(node.tool), scheduling):
    conversation.status = "running"
    task.stage = node.id
    self.store.save(conversation)
    output = self.tools.reconcile(node.tool, spec.inputs, context, context.scope)
```

Do not create a new call or charge another dispatch to reconcile the old one.
Receipt-only recovery still works without a currently registered capability:
the existing `if run is None:` guard stays around this entire block.
Reconciliation is allowed after the execution budget expires; it resolves the
original call rather than authorizing new computation.

- [ ] **Step 7: Replace coordinator-wide locking and move the active check before append**

Remove coordinator `threading`, `_operation()` and unused context-manager
imports. Replace `self.lock = threading.Lock()` with
`self.locks = executor.locks`. Add:

```python
class ConversationActive(ValueError):
    pass
```

At the start of `queue_message()`, replace its global operation guard and
initial validation/loading block with:

```python
if not message.strip() or len(message) > 4000:
    raise ValueError("A message between 1 and 4000 characters is required")
selected = resolve_location(location, self.resources.locations())
identifier = conversation_id if conversation_id is not None else self.store.create().id
with self.locks.hold(identifier):
    record = self.store.get(identifier)
    if record.status in ("queued", "planning", "running"):
        raise ConversationActive("This conversation already has an admitted turn")
    self._message(record, "user", message.strip())
```

Keep the remaining existing intake/unresolved-call/origin logic inside that
guard. Delete its later `if record.status in ("planning", "running")` block.
Replace the final planning transition with:

```python
record.status = "queued"
record.queued_action = "message"
record.queue_origin = None
record.pending_decision = None
record.error = None
self.store.save(record)
return record
```

Replace `continue_submission()`'s guard and validation with:

```python
with self.locks.hold(conversation_id):
    record = self.store.get(conversation_id)
    if (
        record.status != "queued"
        or record.queued_action != "message"
        or not record.messages
        or record.messages[-1].role != "user"
    ):
        raise ValueError("No queued user message is available for planning")
    record.status = "planning"
    record.queued_action = None
    self.store.save(record)
```

Keep its existing resource discovery, planner call, binding, proposal, and
answer logic inside the same guard. At `_wait()`'s beginning, clear
`record.queued_action` and `record.queue_origin`; invalid intake must not leave
a queued marker.

Wrap the **entire existing body** of `prepare_decision()` in
`with self.locks.hold(conversation_id):`. Replace `decide()`'s body with:

```python
with self.locks.hold(conversation_id):
    record = self.store.get(conversation_id)
    if record.status in ("queued", "planning", "running"):
        raise ConversationActive("This conversation already has an admitted turn")
    operation = self.prepare_decision(
        conversation_id, action, version=version, trusted=trusted,
    )
    return operation() if operation is not None else self.store.get(conversation_id)
```

No background worker may hold only its own executor-specific lock. All
coordinator/executor failure, checkpoint, and mutation paths share the registry.

- [ ] **Step 8: Verify GREEN and the existing safety sequence**

Replace the intake test helper's pre-scheduler condition exactly:

```diff
 def submit(runtime, *args, **kwargs):
     record = runtime.queue_message(*args, **kwargs)
-    return runtime.continue_submission(record.id) if record.status == "planning" else record
+    return runtime.continue_submission(record.id) if record.status == "queued" else record
```

```bash
python -m pytest tests/test_scheduler_runtime.py tests/test_scheduler_persistence.py tests/test_sqlite_schema.py tests/test_verification_intake.py tests/test_agent_runtime.py tests/test_agent_recovery.py tests/test_agent_evidence.py tests/test_agent_evaluation.py tests/test_local_matlab_adapters.py -q
```

Expected: all pass. Do not weaken consent, receipt, source, resource, or dependency checks.

- [ ] **Step 9: Commit runtime isolation**

```bash
git add src/flight_agent/verification/contracts.py src/flight_agent/verification/sqlite_schema.py src/flight_agent/verification/sqlite_codec.py src/flight_agent/verification/coordinator.py src/flight_agent/verification/agent_runtime.py tests/scheduling_helpers.py tests/test_scheduler_runtime.py tests/test_scheduler_persistence.py tests/test_sqlite_schema.py tests/test_verification_intake.py
git commit -m "feat: isolate conversation turns and queue capability dispatch"
```

### Task 5: HTTP Admission, Typed Capacity Responses, and Owned Lifecycles

**Files:**
- Modify: `src/flight_agent/verification/routes.py:73-190`
- Modify: `src/flight_agent/web.py:47-105`
- Modify: `src/flight_agent/runtime/scheduling.py`, add `stop_accepting()`
- Test: `tests/test_scheduler_web.py`
- Modify: `tests/test_agent_web.py:113-169`

**Interfaces:**
- Consumes: `PlanningScheduler.reserve()`, `ExecutionTurnScheduler.reserve()`, shared registry and queued contract from Task 4.
- Produces: `ConversationManager(runtime: EngineeringRuntime, planning: PlanningScheduler, execution: ExecutionTurnScheduler)`.
- Produces: `ConversationManager.close() -> None`, idempotent and drain-only.
- Produces: `BoundedRunner.stop_accepting() -> None`.
- Changes: `verification_router(manager: ConversationManager, authorize: Callable[[Request], None]) -> APIRouter`.
- Changes: `create_web_app(root: Path, *, engineering_runtime: EngineeringRuntime | None = None, native_directory_picker: bool = False, scheduler_settings: SchedulerSettings | None = None) -> FastAPI`.
- Produces: `app.state.conversation_manager`, available for explicit lifecycle testing.
- HTTP: valid scheduled work returns `202` with a deep-copied `queued` record; existing synchronous clarification/idempotent results retain their current response behavior; exhaustion returns `429`/`Retry-After: 1` before mutation; closed/unavailable scheduler returns `503`; a conversation-specific admitted-turn conflict returns `409`.

- [ ] **Step 1: Write failing HTTP admission tests**

```python
# tests/test_scheduler_web.py
from threading import Event

from fastapi.testclient import TestClient

from flight_agent.config import SchedulerSettings
from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.agent_planner import AgentDecision, AgentPlanner
from flight_agent.web import create_web_app
from tests.agent_helpers import wait_for_conversation
from tests.scheduling_helpers import make_runtime

API = "/api/conversations"


def build(tmp_path, entered, release):
    class Planner(AgentPlanner):
        def propose(self, message, conversation, **kwargs):
            entered.set()
            assert release.wait(5)
            return AgentDecision(action="answer", text="Answered without execution.")

    settings = SchedulerSettings(planning_workers=1, planning_queue=1)
    runtime = make_runtime(
        tmp_path, Planner(), lambda context, inputs: CapabilityOutcome(), settings=settings,
    )
    return create_web_app(
        tmp_path, engineering_runtime=runtime, scheduler_settings=settings,
    ), runtime


def test_saturation_is_queued_then_typed_capacity_without_phantom_messages(tmp_path):
    entered, release = Event(), Event()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            a = client.post(API, headers=headers, json={"message": "First"})
            assert a.status_code == 202
            assert a.json()["status"] == "queued"
            assert entered.wait(5)
            b = client.post(API, headers=headers, json={"message": "Second"})
            assert b.status_code == 202
            identifier = b.json()["id"]
            queued = client.get(f"{API}/{identifier}", headers=headers).json()
            assert queued["status"] == "queued"
            assert queued["queued_action"] == "message"
            c = client.post(API, headers=headers, json={"message": "Rejected"})
            assert c.status_code == 429
            assert c.json()["detail"]["code"] == "scheduler_capacity"
            assert c.headers["retry-after"] == "1"
            assert len(runtime.store.recent()) == 2
        finally:
            release.set()
        wait_for_conversation(
            client, identifier, headers, lambda record: record["status"] == "completed",
        )


def test_same_queued_conversation_conflict_does_not_append_another_turn(tmp_path):
    entered, release = Event(), Event()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            assert client.post(API, headers=headers, json={"message": "Blocker"}).status_code == 202
            assert entered.wait(5)
            b = client.post(API, headers=headers, json={"message": "Queued"}).json()
            before = runtime.store.get(b["id"]).model_dump()
            response = client.post(
                f"{API}/{b['id']}/messages", headers=headers,
                json={"message": "Must not append"},
            )
            assert response.status_code == 409
            assert runtime.store.get(b["id"]).model_dump() == before
        finally:
            release.set()


def test_closed_manager_returns_503_without_creating_a_conversation(tmp_path):
    entered, release = Event(), Event()
    release.set()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        app.state.conversation_manager.close()
        result = client.post(API, headers=headers, json={"message": "Not accepted"})
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "scheduler_closed"
        assert runtime.store.recent() == []
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_scheduler_web.py -q`

Expected: `create_web_app()` rejects `scheduler_settings`; existing global
background admission cannot return the required queued/capacity semantics.

- [ ] **Step 3: Make stopping admission explicit before draining either runner**

Add this method to `BoundedRunner`, and replace the first assignment to
`self._closing` inside `close()` with a call to it **before** entering the
condition wait:

```python
def stop_accepting(self) -> None:
    with self._condition:
        self._closing = True
        self._condition.notify_all()

def close(self) -> None:
    with self._close_lock:
        self.stop_accepting()
        with self._condition:
            while self._in_use:
                self._condition.wait()
        self._pool.shutdown(wait=True, cancel_futures=False)
```

- [ ] **Step 4: Replace `ConversationManager` completely**

Remove `threading`, `BackgroundRunner`, and `BusyError` imports from
`verification/routes.py`. Import `PlanningScheduler`, `ExecutionTurnScheduler`,
`Submission`, `CapacityExceeded`, `SchedulerClosed`, `ConversationActive`,
`has_unresolved_call`, and `ConversationState` from their defined modules.
Replace the manager with:

```python
class ConversationManager:
    def __init__(
        self, runtime: EngineeringRuntime,
        planning: PlanningScheduler, execution: ExecutionTurnScheduler,
    ) -> None:
        self.runtime = runtime
        self.planning = planning
        self.execution = execution

    def close(self) -> None:
        self.planning.stop_accepting()
        self.execution.stop_accepting()
        self.planning.close()
        self.execution.close()

    def _fail(self, conversation_id: str, error: Exception) -> None:
        logger.exception("Conversation %s failed", conversation_id)
        with self.runtime.locks.hold(conversation_id):
            record = self.runtime.store.get(conversation_id)
            message = str(error)[:4000] or "Conversation operation failed"
            origin = record.queue_origin
            record.queued_action = None
            record.queue_origin = None
            restored = record.restore_turn_origin()
            if not restored and origin is not None:
                record.status = origin.status
                record.pending_decision = origin.pending_decision
                record.error = origin.error
                restored = True
            if restored:
                record.messages.append(ConversationMessage(
                    role="assistant", content="Request failed: " + message, created_at=now(),
                ))
            else:
                uncertain = record.task is not None and has_unresolved_call(record.task)
                record.status = "interrupted" if uncertain else "failed"
                record.error = message
                record.pending_decision = PendingDecision(
                    kind="resume" if uncertain else "clarify", message=message,
                )
            self.runtime.store.save(record)

    def _run(self, conversation_id: str, operation: Callable[[], Conversation]) -> None:
        with self.runtime.locks.hold(conversation_id):
            try:
                operation()
            except Exception as exc:
                self._fail(conversation_id, exc)

    def _schedule(
        self, record: Conversation, reservation: Submission,
        operation: Callable[[], Conversation],
    ) -> Conversation:
        response = record.model_copy(deep=True)
        try:
            reservation.submit(lambda: self._run(record.id, operation))
        except RuntimeError as exc:
            self._fail(record.id, exc)
            raise SchedulerClosed("Unable to publish reserved conversation work") from exc
        return response

    def submit(self, conversation_id: str | None, request: MessageRequest) -> Conversation:
        if conversation_id is not None:
            current = self.runtime.store.get(conversation_id)
            if current.status in ("queued", "planning", "running"):
                raise ConversationActive("This conversation already has an admitted turn")
        with self.planning.reserve() as reservation:
            if conversation_id is None:
                # queue_message validates text/location before allocating an ID.
                record = self.runtime.queue_message(
                    None, request.canonical_message(), request.project_root,
                    output_root=request.output_root, location=request.location,
                )
                identifier = record.id
            else:
                identifier = conversation_id
                record = None
            with self.runtime.locks.hold(identifier):
                if record is None:
                    record = self.runtime.queue_message(
                        identifier, request.canonical_message(), request.project_root,
                        output_root=request.output_root, location=request.location,
                    )
                if record.status != "queued" or record.queued_action != "message":
                    return record.model_copy(deep=True)
                return self._schedule(
                    record, reservation, lambda: self.runtime.continue_submission(identifier),
                )

    def _decision(
        self, conversation_id: str, action: Literal["confirm", "resume", "retry"],
        request: ConfirmationRequest | None,
    ) -> Conversation:
        record = self.runtime.store.get(conversation_id)
        origin = record.queue_origin
        if record.status != "queued" or record.queued_action != action or origin is None:
            raise ValueError("The queued decision is no longer current")
        record.status = origin.status
        record.pending_decision = origin.pending_decision
        record.error = origin.error
        record.queue_origin = None
        record.queued_action = None
        self.runtime.store.save(record)
        return self.runtime.decide(
            conversation_id, action,
            version=request.version if request is not None else None,
            trusted=request.trusted if request is not None else False,
        )

    def decide(
        self, conversation_id: str, action: Literal["confirm", "resume", "retry"],
        request: ConfirmationRequest | None = None,
    ) -> Conversation:
        current = self.runtime.store.get(conversation_id)
        if current.status in ("queued", "planning", "running"):
            raise ConversationActive("This conversation already has an admitted turn")
        with self.execution.reserve() as reservation:
            with self.runtime.locks.hold(conversation_id):
                record = self.runtime.store.get(conversation_id)
                if record.status in ("queued", "planning", "running"):
                    raise ConversationActive("This conversation already has an admitted turn")
                operation = self.runtime.prepare_decision(
                    conversation_id, action,
                    version=request.version if request is not None else None,
                    trusted=request.trusted if request is not None else False,
                )
                if operation is None:
                    return record.model_copy(deep=True)
                record.queue_origin = ConversationState(
                    status=record.status, pending_decision=record.pending_decision,
                    error=record.error,
                )
                record.queued_action = action
                record.status = "queued"
                self.runtime.store.save(record)
                return self._schedule(
                    record, reservation, lambda: self._decision(conversation_id, action, request),
                )
```

For a new conversation, the ID is private until this method returns; no other
request can target it during the short interval between creation and the
manager's registry hold. Existing-ID admission and scheduling occur under one
registry hold. The response is a copy: background mutation must not change the
HTTP response from `queued` into a later state while FastAPI serializes it.

- [ ] **Step 5: Wire typed errors and both schedulers into the existing router**

Change the router factory header and initialization to:

```python
def verification_router(
    manager: ConversationManager, authorize: Callable[[Request], None],
) -> APIRouter:
    runtime = manager.runtime
    router = APIRouter(prefix="/api/conversations", dependencies=[Depends(authorize)])
```

Keep the current routes and status codes. Add these handlers in
`request_errors()` before its `ValueError` branch:

```python
@contextmanager
def request_errors() -> Iterator[None]:
    try:
        yield
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown conversation or task") from exc
    except CapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "scheduler_capacity", "message": str(exc)},
            headers={"Retry-After": "1"},
        ) from exc
    except SchedulerClosed as exc:
        raise HTTPException(
            status_code=503, detail={"code": "scheduler_closed", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Conversation storage is unavailable")
        raise HTTPException(
            status_code=503, detail="Conversation storage is unavailable",
        ) from exc
```

`ConversationActive` is a `ValueError`, so the existing `409` branch handles it.
Keep `KeyError -> 404`, storage failures, authentication, same-origin checks,
body limits, and artifact verification unchanged. A failed pool publication
after a durable save produces a visible failure/origin-restoration, never
`202`; admission exhaustion does not create a conversation at all.

- [ ] **Step 6: Own scheduler composition and shutdown in `web.py`**

Import `SchedulerSettings`, `ExecutionScheduler`, `PlanningScheduler`,
`ExecutionTurnScheduler`, and `ConversationManager`; remove Web's
`BackgroundRunner` import. Add
`scheduler_settings: SchedulerSettings | None = None` to `create_web_app()`'s
keyword-only parameters. At its beginning:

```python
settings = scheduler_settings if scheduler_settings is not None else (
    engineering_runtime.executor.scheduler.settings
    if engineering_runtime is not None
    else SchedulerSettings.from_environment()
)
if engineering_runtime is not None and (
    engineering_runtime.executor.scheduler.settings != settings
):
    raise ValueError("Injected runtime and Web scheduler settings must match")
```

In the existing non-injected executor construction, use:

```python
AgentExecutor(
    conversations, tools, resources, scheduler=ExecutionScheduler(settings),
)
```

Replace `jobs = BackgroundRunner()` with:

```python
manager = ConversationManager(
    runtime,
    PlanningScheduler(settings.planning_workers, settings.planning_queue),
    ExecutionTurnScheduler(settings.execution_turns),
)
```

Replace the lifespan and router installation with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        manager.close()

# After constructing app and installing the existing middleware:
app.state.conversation_manager = manager
app.include_router(verification_router(manager, authorize))
```

Stage 2 closes each SQLite connection at the end of its `_read()` or
`transaction()` context; there is no long-lived repository handle to close
here. Draining both schedulers ensures no admitted worker can open another
store transaction after application shutdown completes. Do not reconstruct or
recover an injected store, reconfigure its executor, or create an unused second
`ExecutionScheduler`.

- [ ] **Step 7: Replace obsolete Web-global-busy tests and verify GREEN**

Delete the three old Web-only tests
`test_scheduler_failure_is_durable_and_does_not_leak_the_slot` and
`test_admission_stays_locked_until_background_slot_is_released`, plus
`test_model_planning_occupies_the_shared_job_slot`, from
`tests/test_agent_web.py`. Their assumptions about patching Web's
`BackgroundRunner` and rejecting every other conversation are intentionally
removed. Keep the two `test_background_runner_*` tests: Worker still uses that
class. Add the following publication-failure test to `test_scheduler_web.py`:

```python
def test_failed_pool_publication_is_visible_and_reservation_can_be_reused(tmp_path, monkeypatch):
    from flight_agent.runtime.scheduling import Submission

    entered, release = Event(), Event()
    release.set()
    app, runtime = build(tmp_path, entered, release)

    def unavailable(self, operation):
        raise RuntimeError("Pool publication failed")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        monkeypatch.setattr(Submission, "submit", unavailable)
        for index in range(3):
            response = client.post(API, headers=headers, json={"message": f"Question {index}"})
            assert response.status_code == 503
        records = runtime.store.recent()
        assert len(records) == 3
        assert all(record.status == "failed" for record in records)
        assert all(record.queued_action is None for record in records)
        assert all(record.error == "Pool publication failed" for record in records)
```

```bash
python -m pytest tests/test_scheduler_web.py tests/test_agent_web.py tests/test_agent_history.py tests/test_native_directories.py tests/test_verification_worker.py -q
```

Expected: pass; unrelated conversations no longer receive global busy errors,
and supported Worker behavior stays unchanged.

- [ ] **Step 8: Commit admission and lifecycle integration**

```bash
git add src/flight_agent/verification/routes.py src/flight_agent/web.py src/flight_agent/runtime/scheduling.py tests/test_scheduler_web.py tests/test_agent_web.py
git commit -m "feat: queue conversation admission and drain schedulers on shutdown"
```

### Task 6: Conservative SQLite Restart Recovery and Shutdown Races

**Files:**
- Create: `src/flight_agent/verification/scheduling_recovery.py`
- Modify: `src/flight_agent/verification/sqlite_store.py`, `SQLiteConversationStore._recover_startup()`
- Test: `tests/test_scheduler_recovery.py`
- Modify: `tests/test_scheduling.py`

**Interfaces:**
- Consumes: Stage 2 `transaction()`, `save()`, the SQLite implementation's thread-local connection, `sqlite_codec.decode()`; migration/queued contract from Task 4.
- Produces: `recover_scheduling(record: Conversation) -> bool`; mutates only the supplied aggregate, returns whether startup must save it.
- Changes: `SQLiteConversationStore._recover_startup() -> None` includes queued/active headers and running calls under any current/archived task.
- Recovery policy: never start a model call, implicitly confirm a baseline, retry an unknown invocation, discard evidence, refund budget, or generate a new call ID at startup.

- [ ] **Step 1: Write failing queued/restart boundary tests**

```python
# tests/test_scheduler_recovery.py
import pytest

from flight_agent.runtime.observations import CapabilityOutcome
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.contracts import ConversationState
from flight_agent.verification.store import ConversationStore
from tests.scheduling_helpers import make_runtime, propose_fake


def reopen(runtime):
    # All prior read/transaction contexts have exited; Stage 2 retains no connection.
    store = ConversationStore(runtime.store.root)
    executor = AgentExecutor(
        store, runtime.executor.tools, runtime.resources,
        scheduler=runtime.executor.scheduler,
    )
    return store, executor


def test_queued_message_restart_does_not_call_a_planner_or_lose_the_message(tmp_path):
    runtime = make_runtime(tmp_path, None, lambda context, inputs: CapabilityOutcome())
    queued = runtime.queue_message(None, "Explain the saved measurement.")
    store, executor = reopen(runtime)
    restored = store.get(queued.id)
    assert restored.status == "interrupted"
    assert restored.pending_decision.kind == "clarify"
    assert restored.messages[0].content == "Explain the saved measurement."
    assert restored.queued_action is None
    assert restored.task is None


def test_queued_followup_restores_original_pending_confirmation(tmp_path):
    runtime = make_runtime(tmp_path, None, lambda context, inputs: CapabilityOutcome())
    original = propose_fake(runtime)
    runtime.queue_message(original.id, "Explain this plan before I confirm.")
    store, executor = reopen(runtime)
    restored = store.get(original.id)
    assert restored.status == original.status
    assert restored.pending_decision == original.pending_decision
    assert restored.task == original.task
    assert restored.turn_origin is None
    assert restored.messages[-1].role == "assistant"


def test_queued_confirmation_restart_never_replays_consent(tmp_path):
    calls = []
    runtime = make_runtime(
        tmp_path, None,
        lambda context, inputs: (calls.append(context.call_id), CapabilityOutcome())[1],
    )
    pending = propose_fake(runtime)
    pending.queue_origin = ConversationState(
        status=pending.status, pending_decision=pending.pending_decision, error=pending.error,
    )
    pending.queued_action = "confirm"
    pending.status = "queued"
    runtime.store.save(pending)
    store, executor = reopen(runtime)
    restored = store.get(pending.id)
    assert restored.status == "waiting_for_user"
    assert restored.pending_decision.kind == "confirm_baseline"
    assert not restored.task.baseline.confirmed
    assert not restored.task.calls
    assert calls == []


def test_crash_waiting_for_local_slot_is_not_an_unknown_external_call(tmp_path, monkeypatch):
    calls = []
    runtime = make_runtime(
        tmp_path, None,
        lambda context, inputs: (calls.append(context.call_id), CapabilityOutcome())[1],
        lane="local_matlab",
    )
    pending = propose_fake(runtime, lane="local_matlab")
    save = runtime.store.save

    def crash_after_queue(record):
        save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            raise KeyboardInterrupt("Process stopped before acquiring a lane")

    monkeypatch.setattr(runtime.store, "save", crash_after_queue)
    with pytest.raises(KeyboardInterrupt):
        runtime.executor.confirm(pending.id, 1, trusted=True)
    store, executor = reopen(runtime)
    restored = store.get(pending.id)
    assert restored.status == "interrupted"
    assert restored.pending_decision.kind == "resume"
    assert restored.task.nodes[0].status == "pending"
    assert restored.task.calls == []
    assert restored.task.budget_state.calls == 0
    assert calls == []
    done = executor.resume(pending.id)
    assert done.status == "completed"
    assert len(calls) == 1


def test_unknown_dispatched_call_keeps_original_identity_and_blocks_retry(tmp_path):
    calls = []

    def uncertain(context, inputs):
        calls.append(context.call_id)
        raise KeyboardInterrupt("Process stopped after dispatched-call commit")

    runtime = make_runtime(tmp_path, None, uncertain, lane="local_matlab")
    pending = propose_fake(runtime, lane="local_matlab")
    with pytest.raises(KeyboardInterrupt):
        runtime.executor.confirm(pending.id, 1, trusted=True)
    original = runtime.store.get(pending.id).task.calls[0].id
    store, executor = reopen(runtime)
    restored = store.get(pending.id)
    assert restored.task.calls[0].status == "interrupted"
    assert restored.task.calls[0].id == original
    assert restored.task.budget_state.calls == 1
    assert original in restored.task.pending_specs
    blocked = executor.resume(pending.id)
    assert blocked.status == "interrupted"
    assert calls == [original]
    with pytest.raises(ValueError, match="unknown|unresolved|reconcile"):
        executor.retry(pending.id, 1, trusted=True)


def test_recovery_is_not_limited_to_recent_twenty_conversations(tmp_path):
    runtime = make_runtime(tmp_path, None, lambda context, inputs: CapabilityOutcome())
    identifiers = [runtime.queue_message(None, f"Question {index}").id for index in range(25)]
    store, executor = reopen(runtime)
    assert all(store.get(identifier).status == "interrupted" for identifier in identifiers)
```

Add the following shutdown tests to `tests/test_scheduling.py`:

```python
def test_shutdown_drains_running_and_queued_work_but_refuses_new_admission():
    runner = PlanningScheduler(1, 1)
    entered, release, stopped, second = Event(), Event(), Event(), Event()

    def first():
        entered.set()
        assert release.wait(5)

    def close():
        runner.stop_accepting()
        stopped.set()
        runner.close()

    with ThreadPoolExecutor(1) as pool:
        with runner.reserve() as reservation:
            a = reservation.submit(first)
        assert entered.wait(5)
        with runner.reserve() as reservation:
            b = reservation.submit(second.set)
        closing = pool.submit(close)
        try:
            assert stopped.wait(5)
            assert not closing.done()
            with pytest.raises(SchedulerClosed):
                with runner.reserve():
                    pytest.fail("shutdown admitted work")
        finally:
            release.set()
        closing.result(timeout=5)
        a.result(timeout=5)
        b.result(timeout=5)
    assert second.is_set()


def test_shutdown_waits_for_a_reserved_but_not_yet_published_request():
    runner = PlanningScheduler(1, 0)
    stopped = Event()

    def close():
        runner.stop_accepting()
        stopped.set()
        runner.close()

    with ThreadPoolExecutor(1) as pool:
        with runner.reserve() as reservation:
            closing = pool.submit(close)
            assert stopped.wait(5)
            assert not closing.done()
            future = reservation.submit(lambda: None)
        future.result(timeout=5)
        closing.result(timeout=5)
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_scheduler_recovery.py tests/test_scheduling.py -q
```

Expected: queued records remain queued, or Stage 2 recovery excludes them.
Shutdown tests should already pass with Task 5; do not make recovery tests
pass by bypassing an actual new `SQLiteConversationStore` instance. Stage 2
owns no persistent connection requiring a `close()` method.

- [ ] **Step 3: Implement the pure recovery transition**

```python
# src/flight_agent/verification/scheduling_recovery.py
from datetime import UTC, datetime

from .contracts import Conversation, ConversationMessage, PendingDecision

RESTART_MESSAGE = (
    "Service restarted before this request completed. No queued request was replayed; "
    "review the saved state and explicitly resend, confirm, or resume."
)


def recover_scheduling(record: Conversation) -> bool:
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    has_running = any(call.status == "running" for task in tasks for call in task.calls)
    if record.status not in ("queued", "planning", "running") and not has_running:
        return False
    has_unknown = any(
        call.status in ("running", "interrupted") for task in tasks for call in task.calls
    )
    for task in tasks:
        unresolved_nodes = {
            call.node_id for call in task.calls if call.status in ("running", "interrupted")
        }
        changed = False
        for call in task.calls:
            if call.status == "running":
                call.status = "interrupted"
                call.error = RESTART_MESSAGE
                call.finished_at = datetime.now(UTC).isoformat()
                changed = True
        for node in task.nodes:
            if node.status == "running" or node.id in unresolved_nodes:
                node.status = "interrupted"
                node.error = RESTART_MESSAGE
                changed = True
            elif node.status == "queued":
                node.status = "pending"
                changed = True
        if changed:
            task.stage = "interrupted"
    origin = record.queue_origin
    action = record.queued_action
    record.queued_action = None
    record.queue_origin = None
    restored = False
    if not has_unknown and action in ("confirm", "resume", "retry") and origin is not None:
        record.status = origin.status
        record.pending_decision = origin.pending_decision
        record.error = origin.error
        restored = True
    elif not has_unknown and record.status in ("queued", "planning"):
        restored = record.restore_turn_origin()
    if not restored:
        record.status = "interrupted"
        record.error = RESTART_MESSAGE
        confirmed = record.task is not None and record.task.baseline.confirmed
        record.pending_decision = PendingDecision(
            kind="resume" if confirmed else "clarify", message=RESTART_MESSAGE,
        )
        record.turn_origin = None
        if record.task is not None:
            record.task.stage = "interrupted"
    record.messages.append(ConversationMessage(
        role="assistant", content=RESTART_MESSAGE, created_at=datetime.now(UTC).isoformat(),
    ))
    return True
```

This helper imports no scheduler, store, registry, planner, or adapter. It does
not erase `pending_specs`, checkpoints, original IDs, receipts, output hashes,
artifacts, or budget charges. A queued node with no dispatched call becomes
pending; a previously dispatched call remains unknown until receipt verification
or its registered reconciliation path resolves it.

- [ ] **Step 4: Replace Stage 2's startup recovery loop using its consumed APIs**

Import `recover_scheduling` in `verification/sqlite_store.py`. Replace
`SQLiteConversationStore._recover_startup()` with:

```python
def _recover_startup(self) -> None:
    with self.transaction():
        connection = self._local.connection
        identifiers = [
            row[0] for row in connection.execute(
                """SELECT DISTINCT conversations.id FROM conversations
                   LEFT JOIN tasks ON tasks.conversation_id=conversations.id
                   LEFT JOIN calls ON calls.task_id=tasks.id
                   WHERE conversations.scheduling_status='queued'
                      OR conversations.status IN ('planning','running')
                      OR calls.status='running'
                   ORDER BY conversations.id"""
            )
        ]
        for conversation_id in identifiers:
            record = decode(connection, conversation_id)
            if recover_scheduling(record):
                self.save(record)
```

The constructor calls this after migrations, before building the Web manager.
Do not invoke recovery on a live application's injected store. This loop
replaces, rather than precedes, Stage 2's older recovery so the older loop
cannot destroy `turn_origin` first. It preserves recovery of running calls
under failed/completed headers and archived tasks. All startup changes commit
in the existing startup transaction; no external operation runs in it.
Remove `ConversationMessage` and `PendingDecision` from `sqlite_store.py`'s
imports if they were only used by the replaced recovery body; keep
`Conversation` and import `recover_scheduling` instead.

- [ ] **Step 5: Verify GREEN plus existing receipt-only and checkpoint recovery**

```bash
python -m pytest tests/test_scheduler_recovery.py tests/test_scheduling.py tests/test_sqlite_store.py tests/test_sqlite_runtime.py tests/test_agent_recovery.py tests/test_agent_history.py tests/test_agent_evidence.py tests/test_conversation_bounds.py -q
```

Expected: pass, including original receipt recovery with a removed/upgraded
capability and recovery after receipt publication but before acceptance.
The tests must not start MATLAB or contact an actual Worker/model.

- [ ] **Step 6: Commit restart-safe scheduling recovery**

```bash
git add src/flight_agent/verification/scheduling_recovery.py src/flight_agent/verification/sqlite_store.py tests/test_scheduler_recovery.py tests/test_scheduling.py
git commit -m "feat: recover queued and interrupted scheduler work conservatively"
```

### Task 7: Queued Workbench/CLI States and Operator Configuration

**Files:**
- Modify: `src/flight_agent/verification/views.py:118-130,188-201`
- Modify: `src/flight_agent/static/agent.js:5-11,87-89,102-113`
- Modify: `src/flight_agent/cli.py:111-122`
- Modify: `.env.example`
- Modify: `docs/usage.md`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_browser.py`
- Modify: `tests/test_scheduler_web.py`

**Interfaces:**
- Consumes: `Conversation.queued_action`, `ConversationStatus` including `queued`.
- Produces: `ConversationView.queued_action: ScheduledAction | None = None`.
- Preserves: existing native JavaScript workbench, endpoints, authentication and explicit-baseline confirmation controls; no frontend build tooling.
- CLI: `directory-task` polls `queued`, `planning`, and `running`, preserving the same conversation ID and timeout behavior.

- [ ] **Step 1: Add failing CLI, view, and browser tests**

Append to `tests/test_cli.py`:

```python
def test_directory_task_waits_for_queued_work_without_resubmitting(monkeypatch, capsys):
    import httpx

    module = cli_module()
    identifier = "c" * 32
    real_client = httpx.Client
    requests = []
    statuses = iter(["queued", "planning", "completed"])

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/bootstrap":
            return httpx.Response(200, json={"token": "test-token"})
        if request.method == "POST":
            return httpx.Response(202, json={"id": identifier, "status": "queued"})
        return httpx.Response(200, json={"id": identifier, "status": next(statuses)})

    monkeypatch.setattr(
        module.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    assert module.main(["directory-task", "propose", "--request", "Explain the result."]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert sum(method == "POST" for method, path in requests) == 1
    assert requests[2:] == [("GET", f"/api/conversations/{identifier}")] * 3
```

Append to `tests/test_scheduler_web.py`:

```python
def test_queued_view_exposes_action_without_claiming_execution(tmp_path):
    entered, release = Event(), Event()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            client.post(API, headers=headers, json={"message": "Blocker"})
            assert entered.wait(5)
            queued = client.post(API, headers=headers, json={"message": "Queued"}).json()
            view = client.get(f"{API}/{queued['id']}/view", headers=headers).json()
            assert view["status"] == "queued"
            assert view["queued_action"] == "message"
            assert view["task"] is None
        finally:
            release.set()
```

Append to `tests/test_agent_browser.py`:

```python
def test_queued_state_keeps_polling_and_disables_confirmation(tmp_path):
    from threading import Event

    app, runtime, calls, tools = make_agent_web(tmp_path, [])
    record = runtime.store.create()
    identifier = record.id
    queued = {
        "id": identifier, "status": "queued", "queued_action": "message",
        "project_root": None, "resource_location": None, "output_root": None,
        "task": None, "archived_tasks": [], "messages": [],
        "pending_decision": None, "error": None,
    }
    completed = {**queued, "status": "completed", "queued_action": None}
    views = []
    show_completed = Event()
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(
            f"localStorage.setItem('verification-conversation', '{identifier}')"
        )

        def reply(route):
            views.append(route.request.url)
            route.fulfill(json=completed if show_completed.is_set() else queued)

        page.route(f"**/api/conversations/{identifier}/view", reply)
        try:
            page.goto(url)
            expect(page.locator("#conversation-status")).to_have_text("排队中")
            expect(page.locator("#send")).to_be_disabled()
            expect(page.locator("#trusted")).to_be_disabled()
            expect(page.locator("#next-action")).to_contain_text("尚未")
            show_completed.set()
            expect(page.locator("#conversation-status")).to_have_text("已完成")
            assert len(views) >= 2
            assert not calls
            assert not errors
        finally:
            browser.close()
```

- [ ] **Step 2: Verify RED**

```bash
python -m pytest tests/test_cli.py::test_directory_task_waits_for_queued_work_without_resubmitting tests/test_scheduler_web.py::test_queued_view_exposes_action_without_claiming_execution -q
RUN_BROWSER_TESTS=1 python -m pytest tests/test_agent_browser.py::test_queued_state_keeps_polling_and_disables_confirmation -q
```

Expected: CLI stops at `queued`; view lacks `queued_action`; browser has no
queued label and stops polling. If Chromium is absent, install the project's
declared test browser with `python -m playwright install chromium` before
running the browser test. A missing-browser failure is not a successful RED
assertion about queued behavior.

- [ ] **Step 3: Expose queued action and keep both clients polling**

Import `ScheduledAction` from `.contracts` in `views.py`. Add the field inside
`ConversationView`:

```python
queued_action: ScheduledAction | None = None
```

Pass `queued_action=record.queued_action` in `to_conversation_view()`.
`NodeView.status` is already `str`, and its projection accepts queued node
status without adding a separate node schema.

Apply these JavaScript edits:

```diff
 const statuses = {
+  queued: "排队中",
   planning: "正在规划", waiting_for_user: "等待确认或补充", running: "执行中",

 const nodeStatuses = {
-  pending: "待执行", running: "运行中", completed: "已完成", failed: "失败", interrupted: "已中断",
+  pending: "待执行", queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", interrupted: "已中断",

 function active() {
-  return pendingView !== null || (current && ["planning", "running"].includes(current.status));
+  return pendingView !== null || (current && ["queued", "planning", "running"].includes(current.status));

-  if (record?.status === "planning") {
+  if (record?.status === "queued") {
+    next = record.queued_action === "message"
+      ? "请求已排队，尚未开始规划；不会据此获得执行授权。"
+      : "工作已排队，尚未开始下一项操作；现有确认与证据保持不变。";
+  } else if (record?.status === "planning") {
```

The existing polling loop calls `active()`, so changing that shared predicate
also keeps polling alive and confirmation/input controls disabled.

In both occurrences of `record.get("status") in ("planning", "running")` in
`cli.py`, use:

```python
record.get("status") in ("queued", "planning", "running")
```

- [ ] **Step 4: Document exact settings and lifecycle behavior**

Append this block to `.env.example`:

```dotenv
# Stage 3 scheduler limits. Invalid or out-of-range values refuse Web startup.
# Planning workers: 1..32; waiting planning jobs: 0..256.
FLIGHT_PLANNING_WORKERS=4
FLIGHT_PLANNING_QUEUE=32
# Local MATLAB remains one slot unless an operator explicitly raises it (1..8).
FLIGHT_LOCAL_MATLAB_SLOTS=1
# Host-side remote invocations: 1..16; does not increase Worker service capacity.
FLIGHT_REMOTE_WORKER_SLOTS=1
# Evaluation/delivery/general execution permits: 1..32.
FLIGHT_READ_ONLY_SLOTS=4
# Execution coordination threads: 1..64, at least the sum of the three lane limits.
FLIGHT_EXECUTION_TURNS=32
```

Append this exact section to `docs/usage.md`:

```markdown
## Scheduler isolation

Independent conversations can plan concurrently. The default planning limit is
four active jobs plus 32 queued jobs. A conversation admits one operation at a
time; sending another request does not create a second active turn or authorize
a new baseline. A conflict response does not append another user message.

Local MATLAB defaults to **one** slot. `FLIGHT_LOCAL_MATLAB_SLOTS` accepts 1–8.
`FLIGHT_REMOTE_WORKER_SLOTS` defaults to 1 and accepts 1–16.
`FLIGHT_READ_ONLY_SLOTS` defaults to 4 and accepts 1–32. Delivery still requires
its confirmed write permission even though it uses the general/read-only lane.
The server selects lanes from registered effects and scheduling metadata;
requests and model plans cannot select a lane.

`FLIGHT_PLANNING_WORKERS` accepts 1–32; `FLIGHT_PLANNING_QUEUE` accepts 0–256.
`FLIGHT_EXECUTION_TURNS` defaults to 32, accepts 1–64, and must cover the sum of
the three execution-lane limits. It bounds coordination threads, including
threads waiting for a lane. These are per-process limits, not distributed
quotas. Run one Web process per writable state root. A remote limit does not
override the remote Worker's independent capacity.

Queued requests return HTTP 202 and remain visible as `queued`; the CLI and
workbench continue polling. Full admission capacity returns HTTP 429 with
`detail.code=scheduler_capacity` and `Retry-After: 1`, without creating a
conversation or appending a message. Shutdown refuses new work with HTTP 503
and drains admitted work so all thread-owned SQLite contexts have exited. No cancellation
or automatic inference retry is introduced.

After an abrupt restart, queued work is not replayed. A queued confirmation
returns to its original confirmation state. Interrupted follow-up planning
restores the original task state. A confirmed task waiting for a lane can be
explicitly resumed; no dispatched call existed until the permit was acquired.
An already dispatched unknown call retains its identity and budget charge and
cannot be retried until the original operation is reconciled. Existing evidence
and source project directories are not removed by scheduler recovery.
```

- [ ] **Step 5: Verify GREEN**

```bash
python -m pytest tests/test_cli.py tests/test_directory_cli.py tests/test_agent_views.py tests/test_scheduler_web.py -q
RUN_BROWSER_TESTS=1 python -m pytest tests/test_agent_browser.py::test_queued_state_keeps_polling_and_disables_confirmation -q
```

Expected: pass; browser remains a native ES script with no frontend build step.

- [ ] **Step 6: Commit queued clients and configuration documentation**

```bash
git add src/flight_agent/verification/views.py src/flight_agent/static/agent.js src/flight_agent/cli.py .env.example docs/usage.md tests/test_cli.py tests/test_agent_browser.py tests/test_scheduler_web.py
git commit -m "feat: expose queued scheduler states and bounded configuration"
```

### Task 8: Adversarial Concurrency Matrix and Stage 3 Acceptance

**Files:**
- Modify: `tests/test_scheduling.py`
- Modify: `tests/test_scheduler_runtime.py`
- Modify: `tests/test_scheduler_web.py`

**Interfaces:**
- Consumes: all Stage 3 public interfaces defined in Tasks 1–7.
- Produces: regression coverage for configured lane counts, cross-lane progress,
  forbidden lane selection, budget expiration before dispatch, and state
  revalidation after waiting.
- No new production interface, dependency, capability, or architectural layer.

- [ ] **Step 1: Add exact tests for every configured lane limit**

Append to `tests/test_scheduling.py`:

```python
@pytest.mark.parametrize("lane,effect,field", [
    ("local_matlab", "execute_project", "local_matlab_slots"),
    ("remote_worker", "execute_project", "remote_worker_slots"),
    ("read_only", "read_only", "read_only_slots"),
])
def test_every_lane_enforces_its_configured_two_slots(lane, effect, field):
    from dataclasses import replace

    scheduler = ExecutionScheduler(replace(SchedulerSettings(), **{field: 2}))
    declared = SchedulingMetadata(lane)
    with scheduler.slot(definition(effect), declared, timeout=0):
        with scheduler.slot(definition(effect), declared, timeout=0):
            with pytest.raises(BudgetExhausted):
                with scheduler.slot(definition(effect), declared, timeout=0):
                    pytest.fail("third invocation exceeded configured two slots")
        with scheduler.slot(definition(effect), declared, timeout=0):
            pass


def test_scheduler_revalidates_effect_instead_of_trusting_callers_lane():
    scheduler = ExecutionScheduler(SchedulerSettings())
    with pytest.raises(ValueError, match="scheduling"):
        with scheduler.slot(definition("execute_project"), SchedulingMetadata("read_only")):
            pytest.fail("execute_project ran in the general pool")
```

Append to `tests/test_scheduler_runtime.py`:

```python
def test_budget_expiry_in_lane_queue_does_not_create_or_charge_a_call(tmp_path):
    from flight_agent.runtime.budget import BudgetExhausted

    calls = []
    runtime = make_runtime(
        tmp_path, None,
        lambda context, inputs: (calls.append(context.call_id), CapabilityOutcome())[1],
        lane="local_matlab",
    )
    pending = propose_fake(runtime, lane="local_matlab")
    scheduler = runtime.executor.scheduler
    original_slot = scheduler.slot
    from contextlib import contextmanager

    @contextmanager
    def expired(definition, metadata, *, timeout=None):
        raise BudgetExhausted("Budget expired while queued")
        yield

    scheduler.slot = expired
    try:
        failed = runtime.executor.confirm(pending.id, 1, trusted=True)
    finally:
        scheduler.slot = original_slot
    assert failed.status == "failed"
    assert failed.task.nodes[0].status == "pending"
    assert failed.task.calls == []
    assert failed.task.budget_state.calls == 0
    assert failed.task.budget_state.rounds == 0
    assert calls == []


def test_source_changed_during_lane_wait_is_revalidated_before_dispatch(tmp_path):
    calls = []
    runtime = make_runtime(
        tmp_path, None,
        lambda context, inputs: (calls.append(context.call_id), CapabilityOutcome())[1],
        lane="local_matlab",
    )
    pending = propose_fake(runtime, lane="local_matlab")
    original_save = runtime.store.save
    verify = runtime.resources.verify

    def save_and_change_source(record):
        original_save(record)
        if record.status == "queued" and record.task.nodes[0].status == "queued":
            def reject(resources):
                raise ValueError("Resource changed while queued")
            runtime.resources.verify = reject

    runtime.store.save = save_and_change_source
    try:
        failed = runtime.executor.confirm(pending.id, 1, trusted=True)
    finally:
        runtime.resources.verify = verify
    assert failed.status == "failed"
    assert failed.task.calls == []
    assert failed.task.budget_state.calls == 0
    assert calls == []


def test_admitted_remote_and_readonly_turns_progress_while_local_is_queued(tmp_path):
    from flight_agent.runtime.agent_contracts import AgentPlan
    from flight_agent.runtime.capabilities import Capability
    from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
    from flight_agent.runtime.scheduling import ExecutionTurnScheduler
    from flight_agent.runtime.scheduling_contracts import SchedulingMetadata
    from tests.agent_helpers import RmseInput, analysis_plan_data

    local_queued = Event()
    runtime = make_runtime(
        tmp_path, None, lambda context, inputs: CapabilityOutcome(), lane="local_matlab",
    )
    scheduler = runtime.executor.scheduler
    runner = ExecutionTurnScheduler(scheduler.settings.execution_turns)
    original_save = runtime.store.save

    def confirm(identifier):
        runtime.executor.confirm(identifier, 1, trusted=True)

    def observe(record):
        original_save(record)
        if record.status == "queued" and record.task.nodes[0].tool == "offline_rmse":
            local_queued.set()

    runtime.store.save = observe
    local = propose_fake(runtime, lane="local_matlab")
    others = []
    for name, lane, effect in (
        ("remote_fake", "remote_worker", "execute_project"),
        ("readonly_fake", "read_only", "read_only"),
    ):
        runtime.executor.tools.register(Capability(
            name=name, version="1", description="Cross-lane fake.",
            input_model=RmseInput, output_model=CapabilityOutcome, effect=effect,
            operation=lambda context, inputs: CapabilityOutcome(),
            scheduling=SchedulingMetadata(lane),
            metadata=CapabilityMetadata(metrics=[
                MetricDefinition(id="rmse", label="RMSE", unit="m"),
            ]),
        ))
        data = analysis_plan_data()
        data["nodes"][0]["tool"] = name
        data["allowed_effects"] = [effect]
        others.append(runtime.executor.propose(
            runtime.store.create(), AgentPlan.model_validate(data),
        ))
    try:
        with scheduler.slot(
            runtime.executor.tools.definition("offline_rmse"),
            runtime.executor.tools.scheduling("offline_rmse"),
        ):
            with runner.reserve() as reservation:
                waiting = reservation.submit(
                    lambda: confirm(local.id),
                )
            assert local_queued.wait(5)
            futures = []
            for record in others:
                with runner.reserve() as reservation:
                    futures.append(reservation.submit(
                        lambda identifier=record.id: confirm(identifier),
                    ))
            for future in futures:
                future.result(timeout=5)
            assert all(runtime.store.get(record.id).status == "completed" for record in others)
            assert not waiting.done()
        waiting.result(timeout=5)
    finally:
        runner.close()
```

Append to `tests/test_scheduler_web.py`:

```python
def test_request_cannot_supply_a_lane_to_bypass_server_metadata(tmp_path):
    entered, release = Event(), Event()
    release.set()
    app, runtime = build(tmp_path, entered, release)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        response = client.post(
            API, headers=headers, json={"message": "Run this", "lane": "read_only"},
        )
        assert response.status_code == 422
        assert runtime.store.recent() == []


def test_planning_progresses_while_another_conversation_executes_local_matlab(tmp_path):
    from tests.scheduling_helpers import propose_fake

    entered, release = Event(), Event()

    class Planner(AgentPlanner):
        def propose(self, message, conversation, **kwargs):
            return AgentDecision(action="answer", text="Independent planning completed.")

    def local_operation(context, inputs):
        entered.set()
        assert release.wait(5)
        return CapabilityOutcome()

    runtime = make_runtime(tmp_path, Planner(), local_operation, lane="local_matlab")
    pending = propose_fake(runtime, lane="local_matlab")
    app = create_web_app(tmp_path, engineering_runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        try:
            response = client.post(
                f"{API}/{pending.id}/confirm", headers=headers,
                json={"version": 1, "trusted": True},
            )
            assert response.status_code == 202
            assert entered.wait(5)
            question = client.post(API, headers=headers, json={"message": "Explain RMSE."})
            assert question.status_code == 202
            wait_for_conversation(
                client, question.json()["id"], headers,
                lambda record: record["status"] == "completed",
            )
        finally:
            release.set()
```

- [ ] **Step 2: Run the acceptance matrix**

```bash
python -m pytest tests/test_conversation_locks.py tests/test_scheduling_contracts.py tests/test_scheduler_settings.py tests/test_scheduling.py tests/test_scheduler_persistence.py tests/test_scheduler_runtime.py tests/test_scheduler_web.py tests/test_scheduler_recovery.py -q
```

Expected: pass with no external services. These are regression tests over
previously implemented behavior, so GREEN is expected on first run. If a
production defect is exposed, keep that failing assertion, apply the smallest
fix in its owning Task 1–7 file, rerun it to GREEN, and include that file in
the final acceptance commit. Do not introduce a new scheduling abstraction.

- [ ] **Step 3: Run complete software-only verification**

```bash
python -m pytest -m "not matlab" -q
python -m ruff check .
python -m mypy src
python -m build
RUN_BROWSER_TESTS=1 python -m pytest tests/test_agent_browser.py tests/test_agent_layout_browser.py tests/test_agent_column_resize_browser.py -q
```

Expected: all selected tests, lint, type checking, package build, and opted-in
browser tests pass. Real MATLAB acceptance is not evidence for scheduler
correctness and is not required for this fake-only Stage 3 acceptance matrix.
Report missing browser/runtime dependencies as blocked verification, not a
pass. Dependency locking and CI configuration remain Stage 6 work.

- [ ] **Step 4: Inspect architectural and safety invariants before committing**

```bash
git diff --check
git --no-pager diff --stat
rg -n 'BusyError|BackgroundRunner|self\.lock\b|self\._operation' src/flight_agent/verification/coordinator.py src/flight_agent/verification/agent_runtime.py src/flight_agent/verification/routes.py src/flight_agent/web.py
rg -n 'scheduling=' src/flight_agent/verification/adapters
rg -n 'queued|FLIGHT_.*(WORKERS|QUEUE|SLOTS|TURNS)' src/flight_agent/cli.py src/flight_agent/static/agent.js .env.example docs/usage.md
```

Expected: no whitespace errors; no process-wide busy/operation locks in the four
Web/runtime files (`rg` exits 1 for that no-match check); all MATLAB/Worker
factories explicitly declare their lane; CLI/UI/environment documentation
includes queued state and exact bounds. Native-directory-picker locking is a
separate GUI exclusivity mechanism and remains unchanged. Worker transport
retains its own existing `BackgroundRunner`; that is not Web-global admission.

- [ ] **Step 5: Commit acceptance coverage and record the Stage 3 boundary**

```bash
git add tests/test_scheduling.py tests/test_scheduler_runtime.py tests/test_scheduler_web.py
git commit -m "test: verify scheduler isolation and recovery acceptance"
git status --short
```

Expected: only understood pre-existing/unrelated changes remain. This plan uses
small testable commits inside Stage 3; if the delivery branch requires one
commit per stage, squash these Stage 3 commits at integration without combining
them with Stage 2 or Stage 4. Do not implement immutable snapshots, context
filtering, Copilot diagnostics, dependency locks, CI, or application-module
extraction as part of this stage.

## Plan Self-Review

| Requirement | Implementation / evidence |
|---|---|
| Approved Stage 3 scope, not adjacent stages | Execution boundary and Task 8 scope check |
| Exact Stage 2 consumption, explicitly assumed | Prerequisite signatures and semantics; Task 6 store integration |
| Validated conversation identity, waiter-safe cleanup | Task 1 |
| Different conversations plan concurrently | Task 4 barrier test |
| Same-conversation turns and task mutation serialized | Task 4 shared reentrant registry; Task 5 admission and failure guards |
| Planning default four, bounded waiting queue | Tasks 3 and 5 |
| Local MATLAB default one, configurable finite bounds | Tasks 2, 3 and 8 |
| Independent remote and general/read-only capacity | Tasks 2, 3 and 8 |
| Capability-declared authority, no request-selected lane | Tasks 2 and 8 |
| Queued state replaces Web-global busy behavior | Tasks 4, 5 and 7 |
| Call commit only after lane acquisition | Task 4 queued ledger test |
| Verified dependencies and no task-node parallelism | Retained `_run()`/`_verify_saved()`/`_live_node()`; runtime/recovery regression suites |
| Shutdown drains; no admission/close publication race | Tasks 3, 5 and 6 |
| Restart queued vs dispatched work distinguished | Task 6 new-store-instance restart tests |
| Unknown external state never implicitly retried | Task 6 and existing recovery suite |
| Baseline/receipt hashes and existing consent unchanged | Task 2 snapshot test; no snapshot schema changes; existing evidence/authorization suites |
| Queued status visible and polled by clients | Task 7 |
| Configuration rejects invalid values instead of unbounded defaults | Task 3 bounds tests and Task 7 docs |

No deferred implementation placeholders are used. Every consumed production
interface is either an explicit Stage 2 prerequisite, an existing named
interface, or produced by a numbered task. Thread counts, queue limits, failure
statuses, lock ownership, lifecycle ownership, and restart consent behavior
are specified rather than left to executor preference.
