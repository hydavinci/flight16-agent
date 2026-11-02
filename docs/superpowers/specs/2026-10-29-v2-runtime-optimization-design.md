# Flight Agent v2 Runtime Optimization Design

Status: approved in conversation on 2026-10-29

## Summary

Flight Agent will converge on one generic engineering runtime:

```text
project directory + requirements + acceptance criteria
    -> constrained planning
    -> explicit confirmation
    -> registered MATLAB/Simulink capabilities
    -> deterministic evaluation
    -> evidence-backed delivery
```

The fixed v1 verification, diagnosis, general-workflow, upload-analysis, and
compatibility paths will be removed. Existing v1 records do not need migration or
archival. The v2 runtime will gain SQLite WAL persistence, bounded concurrency,
task-level immutable project snapshots, smaller planning contexts, structured
Copilot diagnostics, reproducible dependencies, CI, and focused application and
frontend modules.

The work will be delivered in stages on one feature branch. Every stage must leave
the repository in a testable state and receive its own commit.

## Goals

- Keep only the generic v2 Agent execution model.
- Remove v1 runtime code, schemas, routes, tests, documentation, and stored records.
- Preserve the current confirmation, permission, source-attribution, hashing,
  evidence, recovery, and deterministic-evaluation guarantees.
- Replace whole-record JSON rewrites with transactional SQLite WAL state.
- Permit independent conversations to plan concurrently while keeping local MATLAB
  execution single-slot by default.
- Create one immutable project snapshot per confirmed baseline and reuse it across
  all dependent capability calls.
- Reduce model context to information relevant to the requested resources and
  outputs.
- Return actionable, redacted Copilot failure categories.
- Add reproducible dependency locking and continuous integration.
- Split oversized backend and frontend composition files without introducing a
  JavaScript build system.

## Non-goals

- Dynamically installing or executing model-generated tools.
- Weakening explicit confirmation, effects permissions, path validation, source
  binding, capability versioning, budgets, or artifact verification.
- Running multiple local MATLAB Engine operations concurrently by default.
- Migrating or preserving v1 conversation history.
- Replacing disk evidence artifacts with database blobs.
- Adding Redis, Celery, or another external service.
- Redesigning the workbench appearance.

## Product Boundary

The supported product surface after this work is:

- Generic directory conversations.
- Registered local MATLAB project execution.
- Registered local `stepinfo`, linearization, and `allmargin` analysis.
- Registered Simulink Worker capabilities.
- Registered offline diagnostic-data analysis.
- Deterministic criterion evaluation.
- JSON and Word delivery with immutable evidence.
- The generic workbench and its conversation API.

The following legacy surface is removed:

- Fixed D1/D2 scenario selection and workflow registration.
- `VerificationRuntime` and its fixed verification planner.
- Legacy general and diagnosis orchestration.
- Legacy task confirmation, retry, replan, and resume adapters.
- Legacy task view projection.
- Fixed upload-analysis APIs and UI behavior that bypass the generic conversation
  model.
- v1-only CLI commands, documentation, tests, schemas, and dependencies.

Worker transport and diagnostic computation components that are used by registered
v2 capabilities remain supported. Removing v1 orchestration must not remove those
capabilities.

## Target Architecture

```text
FastAPI application
    |
    +-- conversation routes
    |       |
    |       v
    |   EngineeringRuntime
    |       |
    |       +-- ConversationLockRegistry
    |       +-- PlanningScheduler
    |       +-- AgentPlanner
    |       +-- AgentExecutor
    |               |
    |               +-- CapabilityRegistry
    |               +-- ResourceRegistry
    |               +-- ExecutionScheduler
    |               +-- EvidenceStore
    |
    +-- SQLiteConversationStore (WAL)
    |
    +-- artifact and snapshot directories
```

There is no runtime type switch between old and new tasks. Every active task is an
`AgentTask`, every plan is an `AgentPlan`, and every executable operation is a
registered capability.

## v1 Removal and Stored Data

### Code removal

The implementation must first build an import and usage inventory. Modules that only
serve v1 are deleted. Mixed modules are split so that v2 contracts remain independent
before old definitions are removed.

Expected removal areas include:

- `verification/runtime.py`
- `verification/planner.py`
- `verification/general.py`
- `verification/diagnosis.py`
- `verification/compat.py`
- v1-only tool, evaluation, and report orchestration
- v1 task models in `verification/contracts.py`
- v1 branches in routes, views, web assembly, and CLI assembly
- v1-only tests and fixtures

The inventory, not this candidate list, is authoritative. A file remains only when a
generic v2 capability or supported transport imports it.

### Record removal

Existing v1 records are not migrated. Upgrade cleanup may delete only conversation or
job directories positively identified as v1 from their raw JSON structure. It must
not delete:

- v2 `AgentTask` conversations,
- task artifact directories referenced by v2 records,
- user-selected external delivery directories,
- project source directories.

Ambiguous or unreadable records are not deleted automatically. They are reported as
unsupported cleanup candidates with their paths and require explicit manual removal.

After the cleanup release, startup no longer contains v1 deserialization or migration
logic.

## SQLite WAL Persistence

### Storage boundary

SQLite stores metadata and state. Large evidence remains on disk and is referenced by
relative path, SHA256, size, media type, and role.

The initial schema contains:

- `conversations`: identity, timestamps, status, selected roots, pending decision,
  error summary, and current task ID.
- `messages`: ordered user and assistant messages with response kind and gaps.
- `tasks`: task identity, conversation identity, baseline version, stage, status,
  confirmed plan payload, result payload, and timestamps.
- `task_events`: append-only state transitions used for audit and recovery.
- `calls`: invocation identity, node, capability version, status, input/spec/output
  payloads, errors, and timing.
- `artifacts`: task or call ownership, relative path, digest, size, media type, and
  role.
- `schema_migrations`: applied database schema versions.

Domain payload columns use canonical JSON validated by current Pydantic contracts.
Frequently queried identity and status fields remain normal columns.

### Transaction rules

- A user message and the corresponding conversation state transition commit in one
  transaction.
- A dispatched call record is committed before the external operation starts.
- Terminal call state, invocation receipt metadata, node state, and budget state
  commit together.
- Artifact files are written atomically before their database rows become visible.
- Database rows never claim a completed artifact that is absent or hash-invalid.
- Startup marks locally running calls interrupted unless their capability has a
  supported reconciliation path.

### Database lifecycle

- Enable WAL mode, foreign keys, and a bounded busy timeout.
- Apply versioned migrations at startup before accepting requests.
- Refuse startup on a newer unknown schema version.
- Provide deterministic export of a conversation and its artifact index for support
  and testing.

## Scheduling and Concurrency

### Locks

A `ConversationLockRegistry` serializes operations for one conversation. Different
conversations do not block one another during intake, planning, confirmation, or
read-only work.

Locks are removed when no operation references them. Lock identity is the validated
conversation ID, not a user-controlled path.

### Schedulers

- Planning pool: default four workers, configurable by environment.
- Local MATLAB execution: default one slot, configurable but never unbounded.
- Remote Worker execution: separately bounded from local MATLAB.
- Read-only evaluation and delivery: bounded general execution pool.

The scheduler selects a lane from declared capability effects and metadata. Capability
implementations cannot self-select a less restrictive lane.

Queued work is represented as queued state rather than a global `BusyError`.
Cancellation is out of scope unless already supported by the underlying operation.

### Safety invariant

Parallel planning must not allow two active turns in one conversation. Parallel
execution must not permit two nodes to mutate the same task simultaneously. A node
may run only after all dependencies have verified terminal outputs.

## Project Discovery and Immutable Snapshots

Static discovery before confirmation remains bounded and read-only.

When a user confirms a baseline that requires a local project:

1. Revalidate the source project against the confirmed resource fingerprint.
2. Compute the content identity from the confirmed inventory.
3. Reuse an existing verified immutable snapshot with the same identity, or create one
   by atomic copy into the managed snapshot store.
4. Record the snapshot identity and manifest in the task baseline state.
5. Execute all dependent local capabilities from that snapshot.

Snapshots are read-only from the Agent perspective. Capability-specific mutable work
occurs in task work directories derived from the immutable snapshot.

Snapshot reuse requires full manifest and digest verification. File timestamps alone
are never sufficient. Cleanup must retain snapshots referenced by nonterminal tasks
or retained evidence.

## Planning Context Reduction

The planner pipeline becomes:

1. Normalize the request, selected resources, requested outputs, criteria, and
   permissions.
2. Deterministically select capability candidates using declared resource roles,
   output kinds, metrics, and dependency metadata.
3. Select only source excerpts relevant to those candidates while retaining the
   complete source documents in local storage.
4. Ask the model for an answer, clarification, capability gap, or typed plan skeleton.
5. Bind full resource references and source selections locally.
6. Validate capability versions, inputs, dependencies, effects, outputs, criteria,
   source attribution, and budgets locally.

The model never receives hidden permissions or gains authority from capability
filtering. If deterministic filtering cannot prove relevance, it includes the
candidate rather than silently making the task impossible.

The context builder must expose:

- serialized byte size,
- selected capability count,
- selected source excerpt count,
- planning duration,
- correction-round count.

The context hard limit is reduced from 240 KB to 120 KB. Exceeding it produces a
specific clarification or capability-gap response; it does not truncate authorized
requirements or source quotations silently.

## Copilot Diagnostics

Copilot execution remains in an empty temporary working directory with denied file,
shell, write, URL, and memory tools.

The adapter introduces typed failures:

- `authentication`: login absent, expired, or HTTP 401.
- `authorization`: model or account access denied.
- `transport`: WebSocket, proxy, DNS, or connection failure.
- `timeout`: process exceeded the remaining planning deadline.
- `invalid_response`: incomplete JSONL, unexpected tool request, or schema failure.
- `process_failure`: other nonzero CLI termination.

Diagnostics may persist:

- failure category,
- redacted provider request ID when available,
- process exit code,
- elapsed time,
- response byte count,
- bounded redacted error summary.

Diagnostics must not persist prompts, credentials, environment secrets, or unrestricted
stderr.

Use supported Copilot CLI configuration to prevent unrelated custom MCP or plugin
initialization where possible without breaking authenticated access. If the installed
CLI cannot provide that isolation, record it as an operational limitation rather than
modifying user configuration.

No automatic inference retry is added. Existing bounded structural correction remains
the only second model call.

## Application and Frontend Modules

### Backend

`create_web_app()` becomes composition only. Route groups move to focused modules:

- conversation routes,
- artifact/download routes,
- capability and resource metadata routes,
- application health and configuration routes.

Runtime construction moves to a dedicated application factory. Tests may inject the
generic runtime and schedulers through explicit interfaces.

### Frontend

The workbench remains native HTML, CSS, and JavaScript. `agent.js` is split into
browser ES modules for:

- API transport,
- conversation state,
- rendering,
- task and evidence views,
- workbench resizing and preferences,
- application bootstrap.

There is no Node runtime or frontend compilation step.

## Dependency Reproducibility and CI

- Add and commit `uv.lock`.
- Document `uv sync --extra dev` as the development bootstrap command.
- Keep supported Python at 3.11 and newer as declared by the package.
- Add GitHub Actions for Python 3.11 and 3.12.
- Run unit tests excluding the `matlab` marker, Ruff, mypy, and package build.
- Run browser tests in a separate CI job when their required browser is installed.
- Keep real MATLAB acceptance opt-in for a licensed local or self-hosted environment.
- Cache uv downloads, not virtual environments containing machine-specific MATLAB
  Engine installations.

Dependencies used only by deleted v1 code are removed after import verification.

## Error Handling and Recovery

- Database or migration failures stop startup with a clear error.
- Queue saturation leaves work queued or returns a typed capacity response; it does
  not masquerade as success.
- A failed snapshot creation leaves no registered partial snapshot.
- An unknown external execution state remains unretryable until reconciled.
- Planning diagnostics distinguish provider failure from capability gaps.
- Existing evidence survives later evaluation or delivery failures.
- Deleting v1 must not broaden exception handling or introduce silent defaults.

## Delivery Stages

### Stage 1: Remove v1

- Add raw-record classification and targeted cleanup.
- Remove v1 assembly, routes, schemas, orchestration, tests, and docs.
- Preserve generic Worker and diagnostic adapters.
- Remove dependencies proven to be v1-only.

### Stage 2: Introduce SQLite WAL

- Add schema and migrations.
- Implement the conversation-store interface.
- Convert v2 runtime and tests to transactional persistence.
- Remove v2 `conversation.json` writes.

### Stage 3: Add scheduler isolation

- Add per-conversation locking.
- Add planning and execution lanes.
- Replace global busy behavior with queued state.

### Stage 4: Add immutable project snapshots

- Create the managed snapshot store and manifests.
- Bind confirmed baselines to snapshot identities.
- Reuse snapshots across dependent calls.

### Stage 5: Reduce planner context and improve Copilot diagnostics

- Add deterministic capability and source selection.
- Add context metrics and the 120 KB limit.
- Add typed redacted provider failures.

### Stage 6: Reproducibility, CI, and modularization

- Add `uv.lock` and GitHub Actions.
- Split backend assembly and frontend modules.
- Update focused documentation.

Each stage receives targeted tests, full software-only verification, and a separate
commit. Later stages may adjust boundaries discovered by earlier dependency inventory,
but may not restore v1 compatibility.

## Verification

The completed work must demonstrate:

- No production import or runtime reference to `VerificationRuntime`,
  `LegacyRuntimeAdapter`, v1 task models, or legacy scenario workflow IDs.
- Old records positively identified as v1 are removed without deleting v2 records.
- New and recovered v2 conversations persist through SQLite restart tests.
- Simulated process interruption leaves a recoverable, internally consistent ledger.
- Two conversations can plan concurrently.
- Two operations in one conversation remain serialized.
- Local MATLAB execution never exceeds its configured slot count.
- A confirmed project snapshot is created once and reused by dependent capabilities.
- Snapshot tampering is detected before use.
- Irrelevant capabilities and source excerpts are absent from planner context.
- Oversized context fails explicitly without silent source truncation.
- Copilot authentication, authorization, transport, timeout, invalid response, and
  generic process failures map to distinct typed errors.
- Existing confirmation, effects, budget, source, resource, receipt, artifact, and
  recovery security tests remain effective.
- CI passes tests, Ruff, mypy, and package build on Python 3.11 and 3.12.
- The workbench functions without a frontend build step.

Real MATLAB acceptance must rerun at least:

- one local project execution plus `stepinfo`,
- one linearization plus `allmargin`,
- evidence and report hash verification,
- source project immutability verification.

## Risks and Mitigations

### Accidental removal of reusable v2 components

Mitigation: build an import inventory and make v2 adapters pass targeted tests before
deleting each legacy module.

### Database migration defects

Mitigation: versioned migrations, transactional tests, restart tests, and refusal to
open unknown future schemas.

### Concurrency races

Mitigation: per-conversation locks, database transactions, explicit execution lanes,
and barrier-based concurrency tests.

### Snapshot storage growth

Mitigation: content addressing, reference-aware cleanup, and retained digest manifests.

### Planner filtering hides a valid capability

Mitigation: conservative inclusion and tests that every registered capability remains
selectable from its declared metadata.

### Provider diagnostics expose sensitive data

Mitigation: allowlisted fields, bounded summaries, redaction tests, and no prompt or
environment persistence.

## Rollback

Each stage is a separate commit. Before release, rollback is commit-level.

After SQLite becomes active, rollback to the JSON-store release is unsupported because
v1 history is intentionally discarded and new state is stored in SQLite. Before
deploying Stage 2, the application must make a backup copy of the current v2 run
directory or require an explicitly empty development state.

External user project and delivery directories are never modified by rollback or
legacy cleanup.
