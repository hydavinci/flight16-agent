# Immutable Project Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind each newly confirmed local-project baseline to a managed, content-addressed, verified immutable snapshot and reuse that snapshot for every dependent local capability without executing from or modifying the external source.

**Architecture:** Keep source resources as confirmation-time provenance, store canonical file manifests and immutable bytes under the managed state root, and persist snapshot bindings inside the baseline and a transactional SQLite reachability index. Copy verified snapshot bytes into separate managed per-call work directories; preserve dependency-artifact verification, scheduler lanes, original-ID reconciliation, and immutable delivery evidence. Coordinate use and cleanup with a shared/exclusive cross-process lifecycle guard and serialize only same-content creation with an identity lock, never holding either during MATLAB execution.

**Tech Stack:** Python 3.11+, Pydantic v2, standard-library SHA256/filesystem locking/SQLite, existing Stage 2 SQLite WAL conversation store, existing Stage 3 conversation locks and execution scheduler, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-29-v2-runtime-optimization-design.md`, especially “Project Discovery and Immutable Snapshots,” “SQLite WAL Persistence,” “Scheduling and Concurrency,” “Error Handling and Recovery,” and Stage 4.

## Global Constraints

- Keep only the generic v2 Agent execution model.
- Preserve the current confirmation, permission, source-attribution, hashing, evidence, recovery, and deterministic-evaluation guarantees.
- Static discovery before confirmation remains bounded and read-only.
- Create one immutable project snapshot per confirmed baseline and reuse it across all dependent capability calls.
- Snapshots are read-only from the Agent perspective. Capability-specific mutable work occurs in task work directories derived from the immutable snapshot.
- Snapshot reuse requires full manifest and digest verification. File timestamps alone are never sufficient.
- Cleanup must retain snapshots referenced by nonterminal tasks or retained evidence.
- A failed snapshot creation leaves no registered partial snapshot.
- An unknown external execution state remains unretryable until reconciled.
- Existing evidence survives later evaluation or delivery failures.
- Worker transport and diagnostic computation components that are used by registered v2 capabilities remain supported.
- External user project and delivery directories are never modified by rollback or legacy cleanup.
- Keep supported Python at 3.11 and newer as declared by the package.
- Adding Redis, Celery, or another external service is out of scope.

---

## Execution Boundary and Upstream Contracts

This is **Stage 4 only**. Stage 2 and Stage 3 are execution prerequisites, not work
to implement here. Read
`docs/superpowers/plans/2026-10-31-sqlite-conversation-store.md` and
`docs/superpowers/plans/2026-10-31-scheduler-isolation.md` first. The checked-in
production starting point is Stage 1; the following are exact interfaces consumed
from those plans, not claims that their implementations already exist.
Verify them before executing Task 1 and stop if a prerequisite is missing.

| Upstream contract | Exact interface used by this plan |
|---|---|
| SQLite store | `verification.store.ConversationStore` re-exports `sqlite_store.SQLiteConversationStore`; `root: Path`, `database_path: Path`, `create() -> Conversation`, `get(conversation_id: str) -> Conversation`, `save(record: Conversation) -> None` |
| SQLite transaction | `ConversationRepository.transaction() -> AbstractContextManager[None]`; nested store calls share the owning thread's transaction/savepoints; no connection appears in application signatures |
| Internal database access | `SQLiteConversationStore._local.connection` inside `transaction()`; `_read() -> Iterator[sqlite3.Connection]` is an internal context manager for consistent reads |
| Migration registry | `verification.sqlite_schema.Migration(version: int, name: str, statements: tuple[str, ...])`, `MIGRATIONS: tuple[Migration, ...]`, `connect_database(path: Path) -> sqlite3.Connection`; Stage 2 supplies version `1` |
| Task identity | SQLite `tasks.id` is the primary-key task ID; `save()` persists both `record.task` and `record.archived_tasks` in its transaction before calling the Stage 4 reference-index helper |
| Storage paths | `default_task_root(conversation_id: str) -> Path` and `task_directory(conversation_id: str, task_id: str, output_root: str | None = None, *, create: bool = False) -> Path` retain their existing meanings |
| Stage 3 | `AgentExecutor(store: ConversationRepository, tools: CapabilityRegistry[AgentContext], resources: ResourceRegistry, *, locks: ConversationLockRegistry | None = None, scheduler: ExecutionScheduler | None = None)`; `_dispatch()` acquires the declared permit and calls `_dispatch_admitted()`; `_recover()` acquires the existing reconciliation lane |

Use `with store.transaction():`, never `with store.transaction() as connection`
in application code. The Stage 2 and Stage 3 prerequisite contracts both yield
`None`; application code treats the context as an opaque atomic boundary.
Stage 3's queued statuses must already work before this stage. This plan appends
schema version `2` after the version-`1` ledger; it never edits the recorded
checksum of migration `1`.

Do not rebuild the JSON store, reintroduce the old global busy lock, invent a
second database, or bypass the scheduler to accommodate an absent prerequisite.
All commands below run from
`/Users/yhe/Work/flight-agent/.worktrees/stages-2-6`.
Use the selected Python 3.11+ development environment with this package and its
declared development dependencies installed; `python` denotes that environment's
interpreter, not a required system-wide alias.
The commits below are instructions for a later implementation session; authoring
this plan does not run them.

### Decisions and Trade-offs

| Option | Benefit | Cost / decision |
|---|---|---|
| Per-call source copy | Small change to the current backend | Repeated source access; dependent calls can see different bytes. Rejected. |
| Managed immutable object plus independent work copies | One verified content identity, reproducible execution, safe reuse | Extra disk space and hashing at each use. Selected; no hard links or writable shared checkouts. |
| Delete immediately when a task finishes | Lower storage use | Can destroy provenance needed by retained receipts or archived tasks. Rejected. |
| Retain every persisted baseline reference | Conservative, transactionally checkable reachability | Retains more than the minimum; deletion requires explicit removal of the owning task/evidence records. Selected. |

“Immutable” is an Agent storage invariant, not a sandbox against malicious trusted
MATLAB code or a privileged filesystem writer. Read-only permissions discourage
accidental writes; full byte verification detects changes. Do not promise operating
system isolation that this product does not provide.

### Exact File Map

New production files:

| File | Responsibility |
|---|---|
| `src/flight_agent/runtime/project_snapshots.py` | Framework-independent manifest, file entry, and baseline binding contracts |
| `src/flight_agent/verification/snapshot_lock.py` | Standard-library shared/exclusive lifecycle and exclusive identity guards |
| `src/flight_agent/verification/project_snapshots.py` | Canonical object layout, exact-tree verification, atomic publication, independent work-copy materialization |
| `src/flight_agent/verification/snapshot_references.py` | Version-2 SQLite migration and reference-index replacement/query helpers |
| `src/flight_agent/verification/snapshot_binding.py` | Local-project selection, source revalidation, snapshot/baseline compatibility, node lineage |

Modified production files:

| File / existing symbol | Change |
|---|---|
| `src/flight_agent/runtime/agent_contracts.py:AgentBaseline` | Add snapshot binding map; omit an empty map from serialization to preserve historical v2 confirmation hashes |
| `src/flight_agent/verification/sqlite_schema.py:MIGRATIONS` | Install snapshot-reference migration |
| `src/flight_agent/verification/sqlite_store.py:SQLiteConversationStore.save` | Update reference rows in the existing task-save transaction |
| `src/flight_agent/verification/sqlite_store.py:SQLiteConversationStore.cleanup_project_snapshots` | Explicit dry-run-by-default reference-aware cleanup entry point |
| `src/flight_agent/verification/agent_runtime.py:AgentContext, build_context` | Add managed work root and bound snapshot access without rewriting source resources |
| `src/flight_agent/verification/agent_runtime.py:build_invocation_spec` | Include transitive snapshot lineage in new invocation identity |
| `src/flight_agent/verification/agent_runtime.py:AgentExecutor.__init__, confirm, _dispatch_admitted, _recover, _source, _validate_resource_boundary` | Bind at confirmation; verify at admitted use/cache/recovery; preserve Stage 3 routing |
| `src/flight_agent/verification/adapters/local_matlab.py:_request, _work_directory, local_matlab_capabilities` | Read bound snapshot, use internal work directory for new calls, preserve original historical recovery location and evidence-binding checks |
| `src/flight_agent/verification/local_matlab.py:LocalMatlabBackend.__init__, from_environment, execute` | Inject managed store; materialize from it, never copy or rediscover source after confirmation |
| `src/flight_agent/verification/matlab_contracts.py:LocalMatlabRequest` | Add canonical optional project-snapshot binding, preserving old request hashes when absent |
| `src/flight_agent/web.py:create_web_app` | Share one snapshot store between executor and local backend |
| `docs/architecture.md` | Document immutable-store/work-directory boundary and cleanup ownership |
| `docs/usage.md` | Document confirmation, changed-source behavior, cleanup command, and opt-in acceptance |

New test files:

- `tests/snapshot_helpers.py`
- `tests/test_project_snapshot_contracts.py`
- `tests/test_project_snapshot_store.py`
- `tests/test_project_snapshot_references.py`
- `tests/test_agent_project_snapshots.py`
- `tests/test_project_snapshot_cleanup.py`

Modified tests/fixtures:

- `tests/test_local_matlab.py:local_backend, local_request`
- `tests/test_local_matlab_adapters.py:RecordingBackend, local_adapter_case, metric_case`
- `tests/test_local_matlab_frequency.py:linear_request, prepare_frequency_project`
- `tests/test_matlab_project_acceptance.py` (software-only HTTP/restart assertions)
- `tests/test_local_matlab_integration.py` (licensed opt-in direct-request fixtures)
- `tests/test_sqlite_schema.py` (expect the current migration tip, not hard-coded version `1`)

Do not modify `verification/projects.py:snapshot_project`, Worker transport, the
Simulink adapter, or offline diagnostic computation. The existing
`matlab_projects.py:snapshot_matlab_project` may remain for direct utility tests,
but no registered v2 local execution path may call it after this stage.

### Storage Layout and Invariants

```text
<ConversationStore.root>/
  snapshots/
    lifecycle.lock
    locks/
      <64-character-SHA256>.lock
    staging/
      snap-<32-lowercase-hex-id>/
        manifest.json
        tree/
    objects/
      <first-two-hex-characters>/
        <64-character-SHA256>/
          manifest.json
          tree/
  work/
    <conversation-id>/
      <task-id>/
        calls/
          <call-id>/
            local-matlab/
              request.json
              state.json
              execution.json
              project/
              result.json
```

The immutable manifest is canonical UTF-8 JSON with sorted keys, compact separators,
no nonfinite values, and no trailing newline. Its SHA256 is the object ID. It
contains only schema version and sorted relative-path/size/digest entries; source
root, resource ID, timestamps, and task ID are excluded from content identity.
Different source roots with identical bytes can reuse the same object. Source
resource fingerprint and the full manifest travel in `AgentBaseline.project_snapshots`.

The exact tree verifier does **not** apply discovery ignore rules. Missing,
extra, renamed, symlinked, hard-linked, special, or byte-altered managed files fail
verification, including added files named `.DS_Store` or inside `slprj`. Only
directories implied by file paths are permitted; empty source directories are not
part of the confirmed inventory. Preserve the existing discovery limit of 5000
files / 512 MB; reject a manifest exceeding those limits before copying.

Lock order is **conversation lock → shared snapshot lifecycle guard → per-content
creation lock**, followed by the short SQLite binding transaction after releasing
the content lock. Cleanup takes the **exclusive lifecycle guard → consistent
SQLite reference read**, never a conversation lock. Hold the shared guard across
publication and binding/reference commit; hold the exclusive guard across checking
references and deleting objects. Materialization holds a shared guard only for
verification/copy/verification. Different-content confirmations and work copies
can proceed concurrently. Never hold a snapshot guard or a SQLite transaction
while queued for a lane or while a MATLAB process runs.

---

### Task 1: Define Canonical Snapshot Identity and Historical-v2 Serialization

**Files:**
- Create: `src/flight_agent/runtime/project_snapshots.py`
- Modify: `src/flight_agent/runtime/agent_contracts.py:AgentBaseline`
- Create: `tests/snapshot_helpers.py`
- Create: `tests/test_project_snapshot_contracts.py`

**Interfaces:**
- Produces: `SnapshotFile(path: str, sha256: Digest, size: int)`
- Produces: `SnapshotManifest(schema_version: Literal["1"] = "1", files: list[SnapshotFile])`
- Produces: `SnapshotManifest.canonical_bytes() -> bytes`, `SnapshotManifest.snapshot_id -> str`
- Produces: `ProjectSnapshot(snapshot_id: Digest, source_fingerprint: Digest, manifest: SnapshotManifest)`
- Produces: `AgentBaseline.project_snapshots: dict[Name, ProjectSnapshot]`
- Consumes: `runtime.contracts.Digest`, `Name`, `runtime.resources.AgentContract`, existing `AgentBaseline` confirmation validator.

- [ ] **Step 1: RED — write identity, path, bound, and compatibility tests**

Create the shared test helper:

```python
# tests/snapshot_helpers.py
from pathlib import Path

from flight_agent.runtime.project_snapshots import SnapshotFile, SnapshotManifest
from flight_agent.verification.matlab_projects import discover_matlab_project
from tests.matlab_project_helpers import prepared_project


def manifest_for(project):
    return SnapshotManifest(files=[
        SnapshotFile.model_validate(item.model_dump(mode="json"))
        for item in project.files
    ])


def snapshot_case(tmp_path: Path):
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    with snapshots.guard():
        binding = snapshots.ensure(project)
    return source, project, snapshots, binding
```

The storage import stays inside `snapshot_case` so contract-only collection does
not require Task 2's implementation.

```python
# tests/test_project_snapshot_contracts.py
import hashlib

import pytest

from flight_agent.runtime.agent_contracts import AgentBaseline, AgentPlan
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.project_snapshots import (
    ProjectSnapshot,
    SnapshotFile,
    SnapshotManifest,
)
from tests.agent_helpers import analysis_plan_data


def test_manifest_identity_is_exact_canonical_content():
    manifest = SnapshotManifest(files=[
        SnapshotFile(path="a.m", sha256="a" * 64, size=1),
        SnapshotFile(path="data/b.mat", sha256="b" * 64, size=2),
    ])
    expected = (
        '{"files":[{"path":"a.m","sha256":"' + "a" * 64
        + '","size":1},{"path":"data/b.mat","sha256":"' + "b" * 64
        + '","size":2}],"schema_version":"1"}'
    ).encode()
    assert manifest.canonical_bytes() == expected
    assert manifest.snapshot_id == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("path", ["../a.m", "/a.m", "a//b", "a\\b", "C:x", "a\nb"])
def test_manifest_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        SnapshotFile(path=path, sha256="a" * 64, size=1)


def test_manifest_rejects_duplicates_unsorted_paths_and_budget_overflow():
    a = SnapshotFile(path="a", sha256="a" * 64, size=1)
    b = SnapshotFile(path="b", sha256="b" * 64, size=1)
    for files in ([a, a], [b, a], [a] * 5001,
                  [a.model_copy(update={"size": 512 * 1024 * 1024 + 1})]):
        with pytest.raises(ValueError):
            SnapshotManifest(files=files)


def test_binding_checks_content_identity():
    manifest = SnapshotManifest(files=[])
    with pytest.raises(ValueError, match="identity"):
        ProjectSnapshot(
            snapshot_id="f" * 64, source_fingerprint="a" * 64, manifest=manifest
        )


def test_empty_snapshot_map_preserves_historical_baseline_digest():
    old = {
        **AgentPlan.model_validate(analysis_plan_data()).model_dump(mode="json"),
        "version": 1, "confirmed": True,
        "confirmed_at": "2026-10-31T00:00:00+00:00", "capability_snapshots": {},
    }
    baseline = AgentBaseline.model_validate(old)
    assert "project_snapshots" not in baseline.model_dump(mode="json")
    assert fingerprint(baseline.model_dump(mode="json")) == fingerprint(old)
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_project_snapshot_contracts.py -q`

Expected: collection fails because `flight_agent.runtime.project_snapshots` is
absent. After introducing the module, compatibility assertions must fail until
the serializer is added; do not accept a missing unrelated dependency as RED.

- [ ] **Step 3: GREEN — implement the complete manifest/binding contracts**

```python
# src/flight_agent/runtime/project_snapshots.py
import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .contracts import Digest
from .resources import AgentContract

MAX_SNAPSHOT_FILES = 5000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


class SnapshotFile(AgentContract):
    path: Annotated[str, Field(min_length=1, max_length=6000)]
    sha256: Digest
    size: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if (
            PurePosixPath(value).is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(char in value for char in ("\\", ":", "\0", "\r", "\n"))
        ):
            raise ValueError("Snapshot requires a relative path without traversal")
        return value


class SnapshotManifest(AgentContract):
    schema_version: Literal["1"] = "1"
    files: Annotated[list[SnapshotFile], Field(max_length=MAX_SNAPSHOT_FILES)]

    @model_validator(mode="after")
    def bounded_inventory(self) -> Self:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("Snapshot paths must be unique and sorted")
        if sum(item.size for item in self.files) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Snapshot exceeds the 512 MB limit")
        path_set = set(paths)
        if any(
            parent.as_posix() in path_set
            for path in paths
            for parent in PurePosixPath(path).parents
            if parent != PurePosixPath(".")
        ):
            raise ValueError("Snapshot path is both a file and directory")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ProjectSnapshot(AgentContract):
    snapshot_id: Digest
    source_fingerprint: Digest
    manifest: SnapshotManifest

    @model_validator(mode="after")
    def verified_identity(self) -> Self:
        if self.snapshot_id != self.manifest.snapshot_id:
            raise ValueError("Snapshot identity differs from its manifest")
        return self
```

Add this field and serializer to `AgentBaseline`, importing `ProjectSnapshot`,
`model_serializer`, and `SerializerFunctionWrapHandler`. Do not replace its
existing confirmation/capability validators.

```python
project_snapshots: dict[Name, ProjectSnapshot] = {}

@model_serializer(mode="wrap")
def stable_snapshot_serialization(
    self, handler: SerializerFunctionWrapHandler
) -> dict[str, object]:
    result: dict[str, object] = handler(self)
    if not self.project_snapshots:
        result.pop("project_snapshots", None)
    return result
```

The binding is operational state added **only** by trusted confirmation, not a
planner field on `AgentPlan` or `AgentPlanDraft`. Do not change their schemas.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_project_snapshot_contracts.py tests/test_agent_contracts.py tests/test_agent_recovery.py -q`

Expected: all pass; previously recorded v2 hashes remain verifiable without
rewriting a confirmation, capability version, or receipt.

- [ ] **Step 5: Commit**

```bash
git add src/flight_agent/runtime/project_snapshots.py src/flight_agent/runtime/agent_contracts.py tests/snapshot_helpers.py tests/test_project_snapshot_contracts.py
git commit -m "feat: define canonical immutable project snapshot contracts"
```

### Task 2: Publish and Materialize Fully Verified Managed Objects Atomically

**Files:**
- Create: `src/flight_agent/verification/snapshot_lock.py`
- Create: `src/flight_agent/verification/project_snapshots.py`
- Create: `tests/test_project_snapshot_store.py`
- Modify: `tests/snapshot_helpers.py:snapshot_case`

**Interfaces:**
- Produces: `lifecycle_lock(path: Path, *, exclusive: bool) -> ContextManager[None]`
- Produces: `ProjectSnapshotStore(root: Path)`, `root: Path`
- Produces: `guard(*, exclusive: bool = False) -> ContextManager[None]`
- Produces: `ensure(project: MatlabProjectDescriptor) -> ProjectSnapshot` (caller holds guard through baseline/reference commit)
- Produces: `object_directory(snapshot_id: str) -> Path`
- Produces: `verify(binding: ProjectSnapshot) -> Path` (returns verified immutable `tree` root)
- Produces: `materialize(binding: ProjectSnapshot, destination: Path) -> Path` (locks internally, destination must not exist)
- Produces: `remove_object(binding: ProjectSnapshot) -> None` (caller holds exclusive guard and has proven no references)
- Consumes: Task 1 contracts, `discover_matlab_project`, `safe_directory`, `file_digest`.

- [ ] **Step 1: RED — prove reuse, tamper detection, and copy failure behavior**

```python
# tests/test_project_snapshot_store.py
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.verification.project_snapshots import ProjectSnapshotStore
from tests.snapshot_helpers import snapshot_case


def test_verified_reuse_does_not_replace_object(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    inode = store.object_directory(binding.snapshot_id).stat().st_ino
    with store.guard():
        reused = store.ensure(project)
    assert reused == binding
    assert store.object_directory(binding.snapshot_id).stat().st_ino == inode
    assert source.is_dir()
    assert list((store.root / "staging").iterdir()) == []


def test_same_content_at_another_root_has_same_object_identity(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    second = tmp_path / "another-source"
    shutil.copytree(source, second)
    with store.guard():
        reused = store.ensure(discover_matlab_project(str(second)))
    assert reused.snapshot_id == binding.snapshot_id


def test_source_changed_before_confirmation_leaves_no_object(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    empty = ProjectSnapshotStore(tmp_path / "empty" / "snapshots")
    (source / "initialize.m").write_text("different = 1;\n")
    with empty.guard(), pytest.raises(ValueError, match="changed"):
        empty.ensure(project)
    assert not list((empty.root / "objects").glob("*/*"))
    assert not list((empty.root / "staging").iterdir())
    store.verify(binding)


@pytest.mark.parametrize("damage", [
    "bytes", "extra", "ignored", "symlink", "manifest",
    "missing", "renamed", "hardlink", "directory",
])
def test_managed_tamper_is_never_reused(tmp_path, damage):
    source, project, store, binding = snapshot_case(tmp_path)
    obj = store.object_directory(binding.snapshot_id)
    tree = obj / "tree"
    tree.chmod(0o755)
    if damage == "manifest":
        path = obj / "manifest.json"
        path.chmod(0o644)
        path.write_text("{}")
    elif damage in {"extra", "ignored"}:
        (tree / (".DS_Store" if damage == "ignored" else "unexpected.m")).write_text("x")
    elif damage == "directory":
        (tree / "unexpected-empty-directory").mkdir()
    else:
        path = tree / "initialize.m"
        path.chmod(0o644)
        if damage == "renamed":
            path.rename(tree / "renamed.m")
        else:
            path.unlink()
            if damage == "symlink":
                path.symlink_to(source / "initialize.m")
            elif damage == "hardlink":
                os.link(source / "initialize.m", path)
            elif damage == "bytes":
                path.write_text("changed")
                path.chmod(0o444)
    tree.chmod(0o555)
    with store.guard(), pytest.raises(ValueError):
        store.ensure(project)
    assert obj.exists()  # No automatic repair or replacement of a corrupt identity.


def test_work_copy_is_independent_and_source_is_not_reopened(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    shutil.rmtree(source)
    work = tmp_path / "work" / "project"
    store.materialize(binding, work)
    immutable = store.verify(binding)
    assert (work / "initialize.m").stat().st_ino != (
        immutable / "initialize.m"
    ).stat().st_ino
    (work / "initialize.m").write_text("mutable = 1;\n")
    assert (immutable / "initialize.m").read_text() != "mutable = 1;\n"
    assert os.access(work / "initialize.m", os.W_OK)
    store.verify(binding)


def test_existing_work_directory_is_never_removed_on_copy_refusal(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    work = tmp_path / "work" / "project"
    work.mkdir(parents=True)
    marker = work / "keep.txt"
    marker.write_bytes(b"preexisting work")
    with pytest.raises(ValueError, match="already exists"):
        store.materialize(binding, work)
    assert marker.read_bytes() == b"preexisting work"
    store.verify(binding)


def test_two_store_instances_publish_one_complete_object(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    with store.guard(exclusive=True):
        store.remove_object(binding)
    other = ProjectSnapshotStore(store.root)
    barrier = Barrier(2)

    def create(instance):
        barrier.wait()
        with instance.guard():
            return instance.ensure(project)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(create, store)
        right = pool.submit(create, other)
        assert left.result(timeout=10) == right.result(timeout=10) == binding
    assert len(list((store.root / "objects").glob("*/*"))) == 1
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_project_snapshot_store.py -q`

Expected: missing `ProjectSnapshotStore`; after scaffolding, reuse/tamper/materialize
assertions must fail for their stated behavior.

- [ ] **Step 3: GREEN — add a cross-process guard, not a per-instance mutex**

```python
# src/flight_agent/verification/snapshot_lock.py
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .intake import safe_directory


@contextmanager
def lifecycle_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    safe_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Snapshot lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Snapshot lock must be an independent regular file")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped),
            ]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, ctypes.POINTER(Overlapped),
            ]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            overlapped = Overlapped()
            flags = 0x00000002 if exclusive else 0
            if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                yield
            finally:
                if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(stream.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
```

Windows uses `LockFileEx`, not `msvcrt.locking`, because readers must share the
lifecycle lock. A lock error raises; it never silently proceeds unlocked. The
lifecycle guard is not reentrant: call `verify`/`ensure` inside a shared guard and
deletion inside an exclusive guard, but do not call `materialize` while holding
either. Per-content creation locks are separate files and always exclusive.

- [ ] **Step 4: GREEN — implement exact manifests, verified copying, and publication**

```python
# src/flight_agent/verification/project_snapshots.py
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ..runtime.project_snapshots import ProjectSnapshot, SnapshotFile, SnapshotManifest
from .intake import safe_directory
from .matlab_contracts import MatlabProjectDescriptor
from .matlab_projects import discover_matlab_project
from .snapshot_lock import lifecycle_lock

_DIGEST = re.compile(r"[a-f0-9]{64}")
MAX_MANIFEST_BYTES = 4_000_000


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read_regular(path: Path, limit: int) -> bytes:
    safe_directory(path.parent)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"Snapshot is not an independent regular file: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Snapshot file changed while opening")
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Snapshot manifest exceeds its limit")
    return content


def _file_sha256(path: Path, expected: SnapshotFile) -> None:
    safe_directory(path.parent)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
        or before.st_size != expected.size
    ):
        raise ValueError(f"Snapshot file type/size mismatch: {expected.path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Snapshot file changed while opening")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > expected.size:
                raise ValueError(f"Snapshot grew while reading: {expected.path}")
            digest.update(chunk)
    if size != expected.size or digest.hexdigest() != expected.sha256:
        raise ValueError(f"Snapshot digest mismatch: {expected.path}")


def _verify_tree(root: Path, manifest: SnapshotManifest, *, readonly: bool) -> None:
    safe_directory(root)
    if not root.is_dir():
        raise ValueError("Snapshot tree is missing")
    expected_files = {item.path: item for item in manifest.files}
    expected_dirs = {
        parent.as_posix()
        for name in expected_files
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for current, directories, names in os.walk(root, followlinks=False):
        directory = Path(current)
        if readonly and directory.stat().st_mode & 0o222:
            raise ValueError("Immutable snapshot directory is writable")
        for name in directories:
            child = directory / name
            if child.is_symlink() or not child.is_dir():
                raise ValueError("Snapshot contains a symlink or special directory")
            actual_dirs.add(child.relative_to(root).as_posix())
        for name in names:
            child = directory / name
            relative = child.relative_to(root).as_posix()
            if relative not in expected_files:
                raise ValueError(f"Unexpected snapshot file: {relative}")
            _file_sha256(child, expected_files[relative])
            if readonly and child.stat().st_mode & 0o222:
                raise ValueError("Immutable snapshot file is writable")
            actual_files.add(relative)
    if actual_files != set(expected_files) or actual_dirs != expected_dirs:
        raise ValueError("Snapshot inventory differs from its manifest")


def _copy_inventory(source: Path, destination: Path, manifest: SnapshotManifest) -> None:
    safe_directory(source)
    safe_directory(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for item in manifest.files:
        original = source / item.path
        target = destination / item.path
        safe_directory(original.parent)
        safe_directory(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = original.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size != item.size:
            raise ValueError(f"Project changed type/size: {item.path}")
        descriptor = os.open(original, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
            opened = os.fstat(reader.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("Project changed while opening")
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                size += len(chunk)
                if size > item.size:
                    raise ValueError(f"Project grew while copying: {item.path}")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if size != item.size or digest.hexdigest() != item.sha256:
            raise ValueError(f"Project changed while copying: {item.path}")
        target.chmod(0o644)
    _verify_tree(destination, manifest, readonly=False)


def _seal(directory: Path) -> None:
    for current, directories, names in os.walk(directory, topdown=False):
        root = Path(current)
        for name in names:
            (root / name).chmod(0o444)
        for name in directories:
            (root / name).chmod(0o555)
        _sync_directory(root)
    directory.chmod(0o555)


def _remove_owned_directory(directory: Path) -> None:
    safe_directory(directory)
    for current, directories, names in os.walk(directory, followlinks=False):
        root = Path(current)
        root.chmod(0o700)
        for name in directories:
            if (root / name).is_symlink():
                raise ValueError("Refusing to remove a symlinked managed directory")
        for name in names:
            child = root / name
            if (
                child.is_symlink() or not child.is_file()
                or child.lstat().st_nlink != 1
            ):
                raise ValueError("Refusing to remove a special managed file")
            child.chmod(0o600)
    shutil.rmtree(directory)


class ProjectSnapshotStore:
    def __init__(self, root: Path):
        safe_directory(root)
        self.root = root.resolve()

    @contextmanager
    def guard(self, *, exclusive: bool = False) -> Iterator[None]:
        safe_directory(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        with lifecycle_lock(self.root / "lifecycle.lock", exclusive=exclusive):
            for name in ("objects", "staging", "locks"):
                child = self.root / name
                safe_directory(child)
                child.mkdir(exist_ok=True)
            yield

    def object_directory(self, snapshot_id: str) -> Path:
        if _DIGEST.fullmatch(snapshot_id) is None:
            raise ValueError("Invalid snapshot ID")
        directory = self.root / "objects" / snapshot_id[:2] / snapshot_id
        safe_directory(directory)
        return directory

    def verify(self, binding: ProjectSnapshot) -> Path:
        binding = ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
        obj = self.object_directory(binding.snapshot_id)
        if not obj.is_dir() or {item.name for item in obj.iterdir()} != {
            "manifest.json", "tree"
        }:
            raise ValueError("Snapshot object is missing or has unexpected members")
        raw = _read_regular(obj / "manifest.json", MAX_MANIFEST_BYTES)
        manifest = SnapshotManifest.model_validate_json(raw)
        if (
            manifest != binding.manifest or raw != manifest.canonical_bytes()
            or manifest.snapshot_id != binding.snapshot_id
        ):
            raise ValueError("Snapshot manifest identity mismatch")
        if obj.stat().st_mode & 0o222 or (obj / "manifest.json").stat().st_mode & 0o222:
            raise ValueError("Immutable snapshot object is writable")
        _verify_tree(obj / "tree", manifest, readonly=True)
        return obj / "tree"

    def ensure(self, project: MatlabProjectDescriptor) -> ProjectSnapshot:
        project = MatlabProjectDescriptor.model_validate(project.model_dump(mode="json"))
        manifest = SnapshotManifest(files=[
            SnapshotFile.model_validate(item.model_dump(mode="json"))
            for item in project.files
        ])
        lock_path = self.root / "locks" / f"{manifest.snapshot_id}.lock"
        with lifecycle_lock(lock_path, exclusive=True):
            return self._ensure_locked(project)

    def _ensure_locked(self, project: MatlabProjectDescriptor) -> ProjectSnapshot:
        project = MatlabProjectDescriptor.model_validate(project.model_dump(mode="json"))
        source = Path(project.root)
        safe_directory(source)
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise ValueError("Managed snapshots must not overlap the source project")
        if discover_matlab_project(project.root) != project:
            raise ValueError("Project changed; confirm a new resource fingerprint")
        manifest = SnapshotManifest(files=[
            SnapshotFile.model_validate(item.model_dump(mode="json"))
            for item in project.files
        ])
        binding = ProjectSnapshot(
            snapshot_id=manifest.snapshot_id,
            source_fingerprint=project.fingerprint,
            manifest=manifest,
        )
        final = self.object_directory(binding.snapshot_id)
        if final.exists():
            self.verify(binding)
            if discover_matlab_project(project.root) != project:
                raise ValueError("Project changed during snapshot reuse")
            return binding
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "staging" / f"snap-{uuid4().hex}"
        temporary.mkdir()
        try:
            _copy_inventory(source, temporary / "tree", manifest)
            if discover_matlab_project(project.root) != project:
                raise ValueError("Project changed while copying its snapshot")
            with (temporary / "manifest.json").open("xb") as stream:
                stream.write(manifest.canonical_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            _seal(temporary)
            os.rename(temporary, final)
            _sync_directory(final.parent)
            self.verify(binding)
            return binding
        finally:
            if temporary.exists():
                _remove_owned_directory(temporary)

    def materialize(self, binding: ProjectSnapshot, destination: Path) -> Path:
        safe_directory(destination)
        destination = destination.resolve()
        if destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
            raise ValueError("Mutable work must not overlap immutable snapshots")
        with self.guard():
            immutable = self.verify(binding)
            if destination.exists() or destination.is_symlink():
                raise ValueError("Mutable work destination already exists")
            try:
                _copy_inventory(immutable, destination, binding.manifest)
                self.verify(binding)
            except BaseException:
                if destination.exists():
                    _remove_owned_directory(destination)
                raise
        return destination

    def remove_object(self, binding: ProjectSnapshot) -> None:
        self.verify(binding)
        obj = self.object_directory(binding.snapshot_id)
        _remove_owned_directory(obj)
        _sync_directory(obj.parent)
```

The object contains verified complete bytes before rename. If rename succeeds
but a later database commit fails, it is an unreferenced **complete** object,
eligible for verified reuse or cleanup, not a registered partial snapshot.
Directory-fsync or post-publication verification failure prevents confirmation.
Do not overwrite a corrupt existing object to make a task proceed.

- [ ] **Step 5: Add and run deterministic mid-copy fault tests**

Append these tests:

```python
def test_copy_fault_leaves_no_visible_object_or_staging(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module
    from tests.matlab_project_helpers import prepared_project

    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    original = module._copy_inventory

    def fail_after_copy(source, destination, manifest):
        original(source, destination, manifest)
        raise OSError("injected disk failure")

    monkeypatch.setattr(module, "_copy_inventory", fail_after_copy)
    with store.guard(), pytest.raises(OSError, match="injected"):
        store.ensure(project)
    assert not list((store.root / "objects").glob("*/*"))
    assert not list((store.root / "staging").iterdir())


def test_source_addition_during_copy_invalidates_confirmation(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module
    from tests.matlab_project_helpers import prepared_project

    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    original = module._copy_inventory

    def change_after_copy(source, destination, manifest):
        original(source, destination, manifest)
        (source / "new-entry.m").write_text("x = 1;\n")

    monkeypatch.setattr(module, "_copy_inventory", change_after_copy)
    with store.guard(), pytest.raises(ValueError, match="changed"):
        store.ensure(project)
    assert not list((store.root / "objects").glob("*/*"))


def test_different_content_confirmations_can_copy_concurrently(tmp_path, monkeypatch):
    from flight_agent.verification import project_snapshots as module
    from tests.matlab_project_helpers import prepared_project

    left = prepared_project(tmp_path / "left")
    right = prepared_project(tmp_path / "right")
    (right / "README.md").write_text("A different confirmed project.\n")
    projects = [discover_matlab_project(str(root)) for root in (left, right)]
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    both_copying = Barrier(2)
    original = module._copy_inventory

    def coordinated_copy(source, destination, manifest):
        both_copying.wait(timeout=10)
        original(source, destination, manifest)

    def confirm(project):
        with store.guard():
            return store.ensure(project)

    monkeypatch.setattr(module, "_copy_inventory", coordinated_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, project) for project in projects]
        bindings = [future.result(timeout=15) for future in futures]
    assert bindings[0].snapshot_id != bindings[1].snapshot_id
    assert len(list((store.root / "objects").glob("*/*"))) == 2
```

Run: `python -m pytest tests/test_project_snapshot_contracts.py tests/test_project_snapshot_store.py -q`

Add this process-level publication test before running the command:

```python
def publish_from_process(root, project_data, gate, results):
    from flight_agent.verification.matlab_contracts import MatlabProjectDescriptor

    project = MatlabProjectDescriptor.model_validate(project_data)
    snapshots = ProjectSnapshotStore(Path(root))
    gate.wait(timeout=15)
    with snapshots.guard():
        results.put(snapshots.ensure(project).snapshot_id)


def test_spawned_processes_publish_one_complete_object(tmp_path):
    import multiprocessing

    from tests.matlab_project_helpers import prepared_project

    project = discover_matlab_project(str(prepared_project(tmp_path / "source")))
    store = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=publish_from_process,
            args=(str(store.root), project.model_dump(mode="json"), gate, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        identifiers = [results.get(timeout=20), results.get(timeout=20)]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert identifiers[0] == identifiers[1]
        assert len(list((store.root / "objects").glob("*/*"))) == 1
        assert list((store.root / "staging").iterdir()) == []
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()
```

Expected: all pass without a MATLAB installation. The process test starts with
no published object, testing the OS guard rather than only thread serialization
or reuse of an already-existing object.

- [ ] **Step 6: Commit**

```bash
git add src/flight_agent/verification/snapshot_lock.py src/flight_agent/verification/project_snapshots.py tests/snapshot_helpers.py tests/test_project_snapshot_store.py
git commit -m "feat: atomically publish and verify managed project snapshots"
```

### Task 3: Persist Snapshot Reachability with the Baseline Transaction

**Files:**
- Create: `src/flight_agent/verification/snapshot_references.py`
- Modify: `src/flight_agent/verification/sqlite_schema.py:MIGRATIONS`
- Modify: `src/flight_agent/verification/sqlite_store.py:SQLiteConversationStore.save`
- Modify: `tests/test_sqlite_schema.py`
- Create: `tests/test_project_snapshot_references.py`

**Interfaces:**
- Produces: `SNAPSHOT_STATEMENTS: tuple[str, ...]`, installed as `Migration(2, "project-snapshots", SNAPSHOT_STATEMENTS)`
- Produces: `replace_snapshot_references(connection: sqlite3.Connection, record: Conversation) -> None`
- Produces: `retained_snapshot_ids(connection: sqlite3.Connection) -> set[str]`
- Consumes: upstream Stage 2 transaction/save/migration contracts and Task 1 binding types.
- Ownership rule: `save()` cannot silently remove/change an existing confirmed baseline's snapshot binding; deletion of a task and its retained evidence is a distinct explicit retention operation.

- [ ] **Step 1: RED — test transaction rollback and restart reachability**

```python
# tests/test_project_snapshot_references.py
from copy import deepcopy

import pytest

from flight_agent.runtime.agent_contracts import AgentBaseline
from flight_agent.runtime.contracts import fingerprint
from flight_agent.verification.contracts import AgentExecutionNode, AgentTask
from flight_agent.verification.snapshot_references import retained_snapshot_ids
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import analysis_plan_data
from tests.snapshot_helpers import snapshot_case


def record_with_binding(store, binding):
    record = store.create()
    data = analysis_plan_data()
    data["resources"] = [{
        "id": "project", "kind": "matlab_project", "location": "agent",
        "locator": "/source/original", "fingerprint": binding.source_fingerprint,
        "version": "1", "metadata": {},
    }]
    data["nodes"][0]["resource_bindings"] = {"project": "project"}
    baseline = AgentBaseline.model_validate({
        **data, "version": 1, "confirmed": True,
        "confirmed_at": "2026-10-31T00:00:00+00:00",
        "project_snapshots": {"project": binding.model_dump(mode="json")},
    })
    task_id = "d" * 32
    record.task = AgentTask(
        id=task_id, baseline=baseline,
        baseline_sha256=fingerprint(baseline.model_dump(mode="json")),
        nodes=[AgentExecutionNode(**node.model_dump()) for node in baseline.nodes],
        result_directory=str(store.default_task_root(record.id) / task_id),
    )
    return record


def test_snapshot_reference_commits_with_baseline_and_survives_restart(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    record = record_with_binding(store, binding)
    with snapshots.guard(), store.transaction():
        store.save(record)
    restarted = ConversationStore(store.root)
    with restarted._read() as connection:
        assert retained_snapshot_ids(connection) == {binding.snapshot_id}
    assert restarted.get(record.id).task.baseline.project_snapshots["project"] == binding


def test_failed_transaction_registers_neither_baseline_nor_reference(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    record = record_with_binding(store, binding)
    with pytest.raises(RuntimeError, match="abort"):
        with snapshots.guard(), store.transaction():
            store.save(record)
            raise RuntimeError("abort confirmation")
    assert store.get(record.id).task is None
    with store._read() as connection:
        assert retained_snapshot_ids(connection) == set()


def test_saved_confirmed_binding_is_not_mutable(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "state")
    record = record_with_binding(store, binding)
    store.save(record)
    changed = deepcopy(record)
    changed.task.baseline = changed.task.baseline.model_copy(update={"project_snapshots": {}})
    with pytest.raises(ValueError, match="immutable"):
        store.save(changed)
    assert store.get(record.id).task.baseline.project_snapshots["project"] == binding
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_project_snapshot_references.py -q`

Expected: missing reference module/table. The rollback test must actually exercise
Stage 2 SQLite, not a dictionary fake.

- [ ] **Step 3: GREEN — install a foreign-keyed reachability index**

```python
# src/flight_agent/verification/snapshot_references.py
import sqlite3

from ..runtime.agent_contracts import AgentBaseline
from ..runtime.project_snapshots import ProjectSnapshot
from .contracts import Conversation

SNAPSHOT_STATEMENTS = (
    """
    CREATE TABLE project_snapshot_references (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        resource_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL
            CHECK(length(snapshot_id) = 64 AND snapshot_id NOT GLOB '*[^0-9a-f]*'),
        source_fingerprint TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        PRIMARY KEY(task_id, resource_id)
    )
    """,
    """
    CREATE INDEX project_snapshot_references_by_snapshot
    ON project_snapshot_references(snapshot_id)
    """,
)


def replace_snapshot_references(
    connection: sqlite3.Connection, record: Conversation
) -> None:
    tasks = [*record.archived_tasks, *([record.task] if record.task else [])]
    for task in tasks:
        expected = {
            resource_id: (
                binding.snapshot_id,
                binding.source_fingerprint,
                binding.manifest.canonical_bytes().decode("utf-8"),
            )
            for resource_id, binding in task.baseline.project_snapshots.items()
        }
        rows = connection.execute(
            "SELECT resource_id, snapshot_id, source_fingerprint, manifest_json "
            "FROM project_snapshot_references WHERE task_id = ?",
            (task.id,),
        ).fetchall()
        existing = {row[0]: (row[1], row[2], row[3]) for row in rows}
        if existing and existing != expected:
            raise ValueError("Confirmed snapshot references are immutable")
        if expected and not task.baseline.confirmed:
            raise ValueError("Unconfirmed tasks cannot retain project snapshots")
        for resource_id, binding in task.baseline.project_snapshots.items():
            ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
            connection.execute(
                "INSERT INTO project_snapshot_references "
                "(task_id, resource_id, snapshot_id, source_fingerprint, manifest_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id, resource_id) DO NOTHING",
                (task.id, resource_id, *expected[resource_id]),
            )


def retained_snapshot_ids(connection: sqlite3.Connection) -> set[str]:
    expected = {}
    for task_id, raw in connection.execute("SELECT id, baseline_json FROM tasks"):
        baseline = AgentBaseline.model_validate_json(raw)
        if baseline.project_snapshots and not baseline.confirmed:
            raise ValueError("Unconfirmed baseline has snapshot references")
        for resource_id, binding in baseline.project_snapshots.items():
            expected[(task_id, resource_id)] = (
                binding.snapshot_id, binding.source_fingerprint,
                binding.manifest.canonical_bytes().decode("utf-8"),
            )
    actual = {
        (row[0], row[1]): (row[2], row[3], row[4])
        for row in connection.execute(
            "SELECT task_id, resource_id, snapshot_id, source_fingerprint, manifest_json "
            "FROM project_snapshot_references"
        )
    }
    if expected != actual:
        raise ValueError("Snapshot reference index differs from retained task baselines")
    return {entry[0] for entry in actual.values()}
```

In `sqlite_schema.py`, import `SNAPSHOT_STATEMENTS` and append:

```python
MIGRATIONS = (*MIGRATIONS, Migration(2, "project-snapshots", SNAPSHOT_STATEMENTS))
```

In `sqlite_store.py:save()`, call the helper **after** task upserts and **before**
the enclosing transaction commits. Use its already-bound `connection` and
validated aggregate:

```python
replace_snapshot_references(connection, validated)
```

Keep task baseline payload, baseline SHA256, confirmation decision, task event,
and reference index in that same transaction. Normal execution-state updates can
save without a lifecycle guard because they cannot change existing references.
Creating references, explicitly deleting owning tasks, or releasing retention
requires the lifecycle guard. Do not prune archived tasks or their references
based on `recent()`'s 20-conversation window.

Update Stage 2 schema tests to use `schema.MIGRATIONS[-1].version` and
`len(schema.MIGRATIONS)` for the current supported tip and ledger count.
Failure/upgrade probes must allocate `schema.MIGRATIONS[-1].version + 1`,
not reuse the now-real snapshot migration version `2`. For example:

```python
version = schema.MIGRATIONS[-1].version
broken = schema.Migration(version + 1, "failure-probe", (
    "CREATE TABLE migration_probe (value TEXT)",
    "INSERT INTO missing_migration_table VALUES (1)",
))
```

Keep the original rollback assertions, comparing `PRAGMA user_version` with
`version` after failure; do not remove migration drift/future-version tests.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_project_snapshot_references.py tests/test_sqlite_schema.py tests/test_agent_history.py tests/test_agent_recovery.py -q`

Expected: pass; migration version `2` is recorded once, foreign keys reject an
unknown task ID, rollback removes task and index changes together, and restart
does not reconstruct references from external project paths.

- [ ] **Step 5: Commit**

```bash
git add src/flight_agent/verification/snapshot_references.py src/flight_agent/verification/sqlite_schema.py src/flight_agent/verification/sqlite_store.py tests/test_project_snapshot_references.py tests/test_sqlite_schema.py
git commit -m "feat: persist snapshot reachability with confirmed task state"
```

### Task 4: Bind Snapshots Only During Exact-Version Trusted Confirmation

**Files:**
- Create: `src/flight_agent/verification/snapshot_binding.py`
- Modify: `src/flight_agent/verification/agent_runtime.py:AgentExecutor.__init__, confirm, _validate_structure, _validate_resource_boundary`
- Create: `tests/test_agent_project_snapshots.py`

**Interfaces:**
- Produces: `local_projects(baseline: AgentPlan) -> dict[str, ResourceReference]`
- Produces: `prepare_project_snapshots(baseline: AgentBaseline, snapshots: ProjectSnapshotStore) -> dict[str, ProjectSnapshot]`
- Produces: `validate_snapshot_bindings(baseline: AgentBaseline, *, require: bool) -> None`
- Produces: `validate_managed_boundaries(baseline: AgentPlan, registry: ResourceRegistry, snapshots: ProjectSnapshotStore, work_root: Path, output: str | None) -> None`
- Extends: `AgentExecutor.__init__` with keyword-only `snapshots: ProjectSnapshotStore | None = None`; preserves every Stage 3 constructor argument.
- Consumes: `ResourceReference.metadata["project"]`, original resource ID/location/version/fingerprint, Task 2 `ensure`, Stage 2 nested `transaction()`/`save(record)`.

- [ ] **Step 1: RED — add a real executor fixture with two bound local calls**

```python
# tests/test_agent_project_snapshots.py
from pathlib import Path

import pytest

from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import CapabilityMetadata, ResourceRequirement
from flight_agent.runtime.observations import CapabilityOutcome, Finding
from flight_agent.runtime.resources import AgentContract, ResourceReference
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.verification.project_snapshots import ProjectSnapshotStore
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.store import ConversationStore
from tests.matlab_project_helpers import prepared_project


class ProbeInput(AgentContract):
    tag: str


def project_executor(tmp_path, *, after_first=None):
    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    resource = ResourceReference(
        id="local", kind="matlab_project", location="agent", locator=project.root,
        fingerprint=project.fingerprint, version="1",
        metadata={"project": project.model_dump(mode="json")},
    )
    verifications = []
    resources = ResourceRegistry()

    def verify(current):
        verifications.append(current.id)
        if discover_matlab_project(current.locator) != project or current != resource:
            raise ValueError("Source changed before confirmation")

    resources.register("matlab_project", "agent", verify, local=True)
    store = ConversationStore(tmp_path / "state")
    snapshots = ProjectSnapshotStore(store.root / "snapshots")
    calls = []

    def execute(context, inputs):
        binding = context.project_snapshot("project")
        tree = snapshots.verify(binding)
        calls.append((context.call_id, binding.snapshot_id, (tree / "initialize.m").read_bytes()))
        if inputs.tag == "first" and after_first is not None:
            after_first(source)
        return CapabilityOutcome(findings=[
            Finding(id="seen", node_id=context.node_id, text=inputs.tag, status="observed")
        ])

    tools = CapabilityRegistry(agent_mode=True)
    tools.register(Capability(
        name="snapshot_probe", version="1", description="Read verified bound project bytes.",
        input_model=ProbeInput, output_model=CapabilityOutcome, effect="execute_project",
        operation=execute, scheduling=SchedulingMetadata(lane="local_matlab"),
        metadata=CapabilityMetadata(
            resources=[ResourceRequirement(
                role="project", kind="matlab_project", location="agent"
            )],
        ),
    ))
    executor = AgentExecutor(store, tools, resources, snapshots=snapshots)
    plan = AgentPlan.model_validate({
        "title": "Read one confirmed immutable project",
        "rationale": "Two dependent registered calls use the same content.",
        "requirements": {
            "objective": "Read both findings without a compliance claim.", "mode": "analysis",
            "requested_outputs": [
                {"id": "first-output", "kind": "finding", "node_id": "first",
                 "item_id": "seen", "description": "First finding"},
                {"id": "second-output", "kind": "finding", "node_id": "second",
                 "item_id": "seen", "description": "Second finding"},
            ],
            "criteria": [], "overall_requested": False, "delivery_formats": [],
        },
        "resources": [resource.model_dump(mode="json")],
        "nodes": [
            {"id": "first", "tool": "snapshot_probe", "tool_version": "1",
             "inputs": {"tag": "first"}, "resource_bindings": {"project": "local"}},
            {"id": "second", "tool": "snapshot_probe", "tool_version": "1",
             "depends_on": ["first"], "inputs": {"tag": "second"},
             "resource_bindings": {"project": "local"}},
        ],
        "allowed_effects": ["read_only", "execute_project"],
        "budget": {"max_rounds": 4, "max_calls": 4, "max_seconds": 60, "max_no_progress": 4},
    })
    return executor, store, source, plan, calls, verifications


def test_proposal_and_untrusted_confirmation_create_no_snapshot(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    assert pending.task.baseline.project_snapshots == {}
    assert not executor.snapshots.root.exists()
    with pytest.raises(ValueError, match="consent"):
        executor.confirm(pending.id, pending.task.baseline.version, trusted=False)
    assert not executor.snapshots.root.exists()
    assert calls == []


def test_changed_source_requires_new_confirmation_without_dispatch(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    (source / "initialize.m").write_text("new = 1;\n")
    with pytest.raises(ValueError, match="changed"):
        executor.confirm(pending.id, pending.task.baseline.version, trusted=True)
    restored = store.get(pending.id)
    assert not restored.task.baseline.confirmed
    assert restored.task.baseline.project_snapshots == {}
    assert restored.task.calls == []


```

This fixture deliberately calls the new `context.project_snapshot()` interface.
The first two tests reject execution before reaching it. Successful execution
tests are added only in Task 5 so this commit does not introduce a known-failing
future-task test.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_agent_project_snapshots.py -k 'proposal_and_untrusted or changed_source' -q`

Expected: `AgentExecutor.__init__` rejects `snapshots=` before implementation.
After constructor wiring, assertions must enforce no creation before trusted
exact-version confirmation.

- [ ] **Step 3: GREEN — implement project selection and baseline binding checks**

```python
# src/flight_agent/verification/snapshot_binding.py
from pathlib import Path

from ..runtime.agent_contracts import AgentBaseline, AgentPlan
from ..runtime.project_snapshots import ProjectSnapshot, SnapshotFile
from ..runtime.resources import ResourceReference
from .intake import safe_directory, validate_output_root
from .matlab_contracts import MatlabProjectDescriptor
from .project_snapshots import ProjectSnapshotStore
from .resources import ResourceRegistry


def local_projects(baseline: AgentPlan) -> dict[str, ResourceReference]:
    used = {
        identifier for node in baseline.nodes for identifier in node.resource_bindings.values()
    }
    return {
        resource.id: resource for resource in baseline.resources
        if resource.id in used
        and (resource.kind, resource.location) == ("matlab_project", "agent")
    }


def _descriptor(resource: ResourceReference) -> MatlabProjectDescriptor:
    project = MatlabProjectDescriptor.model_validate(resource.metadata.get("project"))
    if resource.version != "1" or (
        project.root, project.fingerprint
    ) != (resource.locator, resource.fingerprint):
        raise ValueError("Project descriptor differs from the confirmed resource")
    return project


def validate_snapshot_bindings(baseline: AgentBaseline, *, require: bool) -> None:
    expected = local_projects(baseline)
    bindings = baseline.project_snapshots
    if bindings and not baseline.confirmed:
        raise ValueError("Unconfirmed baseline cannot contain managed snapshot bindings")
    if (require or bindings) and set(bindings) != set(expected):
        raise ValueError("Local execution requires a newly confirmed snapshot baseline")
    for identifier, binding in bindings.items():
        resource = expected[identifier]
        project = _descriptor(resource)
        manifest_files = [
            SnapshotFile.model_validate(item.model_dump(mode="json"))
            for item in project.files
        ]
        if (
            binding.source_fingerprint != resource.fingerprint
            or binding.manifest.files != manifest_files
        ):
            raise ValueError("Snapshot binding differs from the confirmed project inventory")
        ProjectSnapshot.model_validate(binding.model_dump(mode="json"))


def prepare_project_snapshots(
    baseline: AgentBaseline, snapshots: ProjectSnapshotStore
) -> dict[str, ProjectSnapshot]:
    return {
        identifier: snapshots.ensure(_descriptor(resource))
        for identifier, resource in sorted(local_projects(baseline).items())
    }


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def validate_managed_boundaries(
    baseline: AgentPlan,
    registry: ResourceRegistry,
    snapshots: ProjectSnapshotStore,
    work_root: Path,
    output: str | None,
) -> None:
    validate_output_root(output)
    safe_directory(work_root)
    managed = [snapshots.root, work_root]
    if _overlap(managed[0], managed[1]):
        raise ValueError("Snapshot and mutable work roots must be separate")
    source_roots = (
        [Path(resource.locator) for resource in baseline.resources
         if resource.location == "agent"]
        if isinstance(baseline, AgentBaseline) and baseline.confirmed
        else registry.local_roots(baseline.resources)
    )
    protected = [*source_roots, *([Path(output)] if output else [])]
    for owned in managed:
        for external in protected:
            if _overlap(owned, external):
                raise ValueError("Managed snapshot/work storage overlaps an external resource")
    if output:
        for source in source_roots:
            if _overlap(Path(output), source):
                raise ValueError("Output directory must not overlap the source project")
```

Postconfirmation boundary checks use persisted absolute source locators, not
`Path.is_dir()`, discovery, or symlink resolution of the original project.
For registered local file resources, retain the original registry's parent-root
boundary checks at proposal/confirmation; no diagnostic resource semantics change.

In the executor constructor, add the following without changing Stage 3 locks,
scheduler injection, or operation wrappers:

```python
self.snapshots = snapshots if snapshots is not None else ProjectSnapshotStore(
    store.root / "snapshots"
)
self.work_root = store.root / "work"
```

Add `validate_snapshot_bindings(baseline, require=False)` to
`_validate_structure()`. Replace `_validate_resource_boundary()`'s body with the
call below, retaining preconfirmation `registry.local_roots()` validation through
the helper:

```python
validate_managed_boundaries(
    baseline, self.resources, self.snapshots, self.work_root, output
)
```

- [ ] **Step 4: GREEN — publish first, then commit a confirmed candidate atomically**

In `confirm()`, retain the existing trusted/version, structure, budget, permission,
capability-version, resource, and output validation **before** filesystem creation.
Replace the confirmation mutation/save region with:

```python
guard = self.snapshots.guard() if local_projects(task.baseline) else nullcontext()
with guard:
    bindings = prepare_project_snapshots(task.baseline, self.snapshots)
    # Revalidate every original resource after all projects have been copied.
    self.resources.verify(task.baseline.resources)
    candidate = conversation.model_copy(deep=True)
    candidate_task = self._task(candidate)
    candidate_task.baseline = AgentBaseline.model_validate({
        **task.baseline.model_dump(mode="json"),
        "project_snapshots": {
            identifier: binding.model_dump(mode="json")
            for identifier, binding in bindings.items()
        },
        "confirmed": True,
        "confirmed_at": now(),
    })
    validate_snapshot_bindings(candidate_task.baseline, require=True)
    candidate_task.baseline_sha256 = fingerprint(
        candidate_task.baseline.model_dump(mode="json")
    )
    candidate_task.decisions.append(Decision(
        action="confirm_baseline", baseline_version=version, trusted=True, created_at=now()
    ))
    self._directory(candidate, candidate_task, create=True)
    with self.store.transaction():
        self.store.save(candidate)
return self._run(candidate, candidate_task)
```

Import `contextlib.nullcontext`; the conditional guard above avoids creating a
snapshot directory for a plan with no selected local MATLAB project.

Use the same candidate/transaction path for Worker-only, diagnostic-only, and
resource-free plans, but leave their verification/adapters untouched. Do not mark
the original in-memory or persisted baseline confirmed when copying or saving
fails. A second trusted confirmation of an already-completed baseline retains the
existing idempotent return; it does not re-open the external source.

- [ ] **Step 5: Run GREEN**

Run: `python -m pytest tests/test_agent_project_snapshots.py -k 'proposal_and_untrusted or changed_source' -q`

Expected: pass. Also run
`python -m pytest tests/test_agent_runtime.py tests/test_agent_adapters.py tests/test_agent_diagnostic_engine.py -q`.
These existing non-local-project tests must remain green.

- [ ] **Step 6: Commit**

```bash
git add src/flight_agent/verification/snapshot_binding.py src/flight_agent/verification/agent_runtime.py tests/test_agent_project_snapshots.py
git commit -m "feat: bind trusted confirmed baselines to managed project snapshots"
```

### Task 5: Route Execution, Cache Identity, and Recovery Through Snapshot Lineage

**Files:**
- Modify: `src/flight_agent/verification/snapshot_binding.py`
- Modify: `src/flight_agent/verification/agent_runtime.py:AgentContext, build_context, build_invocation_spec, AgentExecutor._dispatch_admitted, _recover, _source`
- Modify: `tests/test_agent_project_snapshots.py`

**Interfaces:**
- Produces: `snapshot_lineage(baseline: AgentBaseline, node: AgentNode) -> dict[str, ProjectSnapshot]`
- Produces: `verify_snapshot_lineage(baseline: AgentBaseline, node: AgentNode, snapshots: ProjectSnapshotStore) -> None`
- Produces: `verify_execution_resources(baseline: AgentBaseline, registry: ResourceRegistry) -> None`
- Produces: `AgentContext.project_snapshot(role: str) -> ProjectSnapshot`
- Produces: `AgentContext.work_directory: Path | None`, `_project_snapshots: dict[str, ProjectSnapshot]`
- Extends: `build_context(conversation: Conversation, task: AgentTask, node: AgentExecutionNode, call: ToolCall, budget: BudgetAccount, *, store: ConversationRepository, snapshots: ProjectSnapshotStore | None = None) -> AgentContext`
- Consumes: `plan_ancestors`, Task 4 original resource bindings, existing receipt/cache/reconciliation logic.

- [ ] **Step 1: RED — add source-removal, tamper, lineage, and boundary tests**

Append these executable cases:

```python
def test_confirmation_creates_one_snapshot_for_two_dependent_calls(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    done = executor.confirm(pending.id, pending.task.baseline.version, trusted=True)
    assert done.status == "completed", done.error
    binding = done.task.baseline.project_snapshots["local"]
    assert binding.source_fingerprint == plan.resources[0].fingerprint
    assert len(calls) == 2 and {item[1] for item in calls} == {binding.snapshot_id}
    assert len(list((executor.snapshots.root / "objects").glob("*/*"))) == 1
    assert done.task.baseline.resources == plan.resources
    assert store.get(done.id).task.baseline.project_snapshots["local"] == binding


def test_second_call_uses_same_snapshot_after_source_disappears(tmp_path):
    import shutil

    executor, store, source, plan, calls, verifications = project_executor(
        tmp_path, after_first=shutil.rmtree
    )
    pending = executor.propose(store.create(), plan)
    done = executor.confirm(pending.id, pending.task.baseline.version, trusted=True)
    assert done.status == "completed", done.error
    assert len(calls) == 2 and calls[0][1:] == calls[1][1:]
    assert not source.exists()


def test_snapshot_tamper_blocks_next_call_before_dispatch(tmp_path):
    holder = {}

    def tamper(source):
        snapshots = holder["executor"].snapshots
        obj = next((snapshots.root / "objects").glob("*/*"))
        path = obj / "tree" / "initialize.m"
        path.chmod(0o644)
        path.write_text("tampered = 1;\n")
        path.chmod(0o444)

    executor, store, source, plan, calls, verifications = project_executor(
        tmp_path, after_first=tamper
    )
    holder["executor"] = executor
    pending = executor.propose(store.create(), plan)
    done = executor.confirm(pending.id, pending.task.baseline.version, trusted=True)
    assert done.status == "failed"
    assert len(calls) == 1
    assert len(done.task.calls) == 1
    assert done.task.calls[0].status == "completed"
    assert done.task.outputs["first"].findings[0].text == "first"


def test_invocation_identity_includes_transitive_snapshot_lineage(tmp_path):
    from flight_agent.runtime.agent_contracts import AgentNode
    from flight_agent.verification.agent_runtime import build_invocation_spec

    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    done = executor.confirm(pending.id, pending.task.baseline.version, trusted=True)
    metric = AgentNode(
        id="measure", tool="snapshot_probe", tool_version="1",
        depends_on=["second"], inputs={"tag": "metric"},
    )
    baseline = done.task.baseline.model_copy(update={"nodes": [*plan.nodes, metric]})
    spec = build_invocation_spec(baseline, metric, {"tag": "metric"}, done.task.outputs)
    binding = baseline.project_snapshots["local"]
    assert spec.numerics["project_snapshots"] == {"local": binding.model_dump(mode="json")}
    assert spec.resource_fingerprints == {}


def test_snapshot_and_work_roots_cannot_overlap_external_delivery(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    record = store.create()
    record.output_root = str(store.root)
    store.save(record)
    delivery_plan = plan.model_copy(update={
        "allowed_effects": ("read_only", "execute_project", "write_delivery")
    })
    with pytest.raises(ValueError, match="overlap"):
        executor.propose(record, delivery_plan)
    assert not executor.snapshots.root.exists()
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_agent_project_snapshots.py -q`

Expected: missing context access and/or repeated-source-verification failures.
The tamper test must fail because a second external call would otherwise run,
not because the tamper fixture itself raises a filesystem-permission error.

- [ ] **Step 3: GREEN — define lineage once and apply it at every use boundary**

Append to `snapshot_binding.py`:

```python
from ..runtime.agent_contracts import AgentNode
from ..runtime.tasks import plan_ancestors


def snapshot_lineage(
    baseline: AgentBaseline, node: AgentNode
) -> dict[str, ProjectSnapshot]:
    producers = {node.id, *plan_ancestors(baseline.nodes)[node.id]}
    resource_ids = {
        identifier for candidate in baseline.nodes if candidate.id in producers
        for identifier in candidate.resource_bindings.values()
    }
    return {
        identifier: binding for identifier, binding in sorted(baseline.project_snapshots.items())
        if identifier in resource_ids
    }


def verify_snapshot_lineage(
    baseline: AgentBaseline, node: AgentNode, snapshots: ProjectSnapshotStore
) -> None:
    validate_snapshot_bindings(baseline, require=bool(baseline.project_snapshots))
    bindings = snapshot_lineage(baseline, node)
    if bindings:
        with snapshots.guard():
            for binding in bindings.values():
                snapshots.verify(binding)


def verify_execution_resources(
    baseline: AgentBaseline, registry: ResourceRegistry
) -> None:
    registry.verify([
        resource for resource in baseline.resources
        if resource.id not in baseline.project_snapshots
    ])
```

Immediately before a **new dispatch** using a local project or its transitive
outputs, require complete bindings. Old confirmed local tasks without bindings
are still readable and reconcilable, but may not run a new operation until a
new proposal receives explicit confirmation.

```python
ancestors = plan_ancestors(task.baseline.nodes)[node.id] | {node.id}
local_ids = set(local_projects(task.baseline))
needs_snapshot = any(
    local_ids.intersection(candidate.resource_bindings.values())
    for candidate in task.baseline.nodes if candidate.id in ancestors
)
if needs_snapshot:
    validate_snapshot_bindings(task.baseline, require=True)
verify_snapshot_lineage(task.baseline, _node(node), self.snapshots)
verify_execution_resources(task.baseline, self.resources)
```

Place this after `_live_node()` and before budgeting, cache lookup, running-call
creation, or `tools.invoke()` in `_dispatch_admitted()`. Replace its old
`self.resources.verify(task.baseline.resources)` call; do not retain both.
This intentionally stops treating later source edits as changes to already
confirmed execution bytes.

Use `verify_snapshot_lineage()` in `_source()` before accepting an archived
receipt as a cache source. In `_recover()`, check it for a **new-style** call
whose original invocation specification contains `numerics["project_snapshots"]`.
For an actual reconciliation operation, check **after** acquiring Stage 3's
original capability lane and immediately before `tools.reconcile()`:

```python
if "project_snapshots" in spec.numerics:
    verify_snapshot_lineage(task.baseline, _node(node), self.snapshots)
```

Apply the same check before accepting a loaded new-style receipt during
recovery. Do not wrap the backend call or scheduler wait with a snapshot guard.

For unresolved **pre-Stage-4** calls, allow the existing original-ID receipt-only
reconciliation path. Never synthesize a snapshot, rewrite the old request/spec,
signal an untrusted saved PID, or redispatch during that path. If original
termination remains unproven, retain `ExecutionUncertain` and the interrupted
ledger. A known failure still requires explicit retry; retry cannot turn a
legacy baseline into an implicitly confirmed snapshot baseline.

- [ ] **Step 4: GREEN — expose bound snapshots and an internal work root**

Add defaults at the end of the `AgentContext` dataclass so existing non-project
contexts remain source compatible. Import `dataclasses.field`.

```python
work_directory: Path | None = None
_project_snapshots: dict[str, ProjectSnapshot] = field(default_factory=dict)

def project_snapshot(self, role: str) -> ProjectSnapshot:
    resource = self.resource(role)
    if resource.id not in self._project_snapshots:
        raise ValueError("Local project requires its confirmed managed snapshot")
    binding = self._project_snapshots[resource.id]
    if binding.source_fingerprint != resource.fingerprint:
        raise ValueError("Snapshot differs from the bound resource fingerprint")
    return ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
```

In `build_context()`, retain `directory` and checkpoints as the evidence location.
Validate IDs through the existing contract/store boundaries and add:

```python
work_directory = store.root / "work" / conversation.id / task.id / "calls" / call.id
safe_directory(work_directory)
lineage = snapshot_lineage(task.baseline, _node(node))
if snapshots is not None:
    verify_snapshot_lineage(task.baseline, _node(node), snapshots)
```

Pass `work_directory=work_directory` and `_project_snapshots=lineage` to the
returned context. Pass `snapshots=self.snapshots` at **both** executor call sites
of `build_context()` (`_dispatch_admitted` and `_recover`). The original resource locator,
metadata, scope permissions, and direct-dependency `outputs` remain unchanged.

Replace `build_invocation_spec()` with:

```python
def build_invocation_spec(
    baseline: AgentBaseline,
    node: AgentNode,
    inputs: dict[str, JsonValue],
    outputs: dict[str, CapabilityOutcome],
) -> InvocationSpec:
    snapshot = baseline.capability_snapshots[node.tool]
    by_id = {resource.id: resource for resource in baseline.resources}
    resources = {
        role: by_id[identifier] for role, identifier in node.resource_bindings.items()
    }
    lineage = snapshot_lineage(baseline, node)
    snapshot_numerics = (
        {"project_snapshots": {
            identifier: binding.model_dump(mode="json")
            for identifier, binding in lineage.items()
        }}
        if lineage else {}
    )
    conditions = inputs.get("conditions", {})
    return InvocationSpec(
        capability=node.tool,
        capability_version=node.tool_version,
        inputs=inputs,
        resource_fingerprints={
            resource.id: resource.fingerprint for resource in resources.values()
        },
        conditions=conditions if isinstance(conditions, dict) else {},
        numerics={
            "capability_declaration_sha256": fingerprint(snapshot.model_dump(mode="json")),
            "confirmed_inputs_sha256": fingerprint(resolve_inputs(node, outputs)),
            "validity_policy": snapshot.metadata.validity_policy,
            "resources": {
                role: resource.model_dump(mode="json") for role, resource in resources.items()
            },
            **snapshot_numerics,
        },
        dependency_output_sha256={
            name: fingerprint(outputs[name].model_dump(mode="json"))
            for name in node.depends_on
        },
    )
```

This binds cached receipts and checkpoints to the full confirmed snapshot
identity/manifest, including artifact-only `stepinfo` and `allmargin` descendants.
Do not include mutable work paths in the invocation spec. Do not add an empty
lineage field to historical or Worker/diagnostic-only specifications.

- [ ] **Step 5: Run GREEN**

Run: `python -m pytest tests/test_agent_project_snapshots.py tests/test_agent_runtime.py tests/test_agent_recovery.py tests/test_agent_history.py tests/test_agent_evidence.py -q`

Expected: pass; two dependent local operations see one object, source removal
does not redirect execution, tampering stops before a second running call exists,
and receipt/budget/cache recovery tests remain effective.

- [ ] **Step 6: Commit**

```bash
git add src/flight_agent/verification/snapshot_binding.py src/flight_agent/verification/agent_runtime.py tests/test_agent_project_snapshots.py
git commit -m "feat: execute confirmed nodes with verified snapshot lineage"
```

### Task 6: Run Every Registered Local MATLAB Operation in Separate Managed Work

**Files:**
- Modify: `src/flight_agent/verification/matlab_contracts.py:LocalMatlabRequest`
- Modify: `src/flight_agent/verification/adapters/local_matlab.py:_request, _work_directory, local_matlab_capabilities`
- Modify: `src/flight_agent/verification/local_matlab.py:LocalMatlabBackend.__init__, from_environment, execute, _resume`
- Modify: `src/flight_agent/web.py:create_web_app`
- Modify: `tests/snapshot_helpers.py`
- Modify: `tests/test_local_matlab.py:local_backend, local_request`
- Modify: `tests/test_local_matlab_adapters.py:RecordingBackend, local_adapter_case, metric_case`
- Modify: `tests/test_local_matlab_frequency.py:linear_request, frequency_case`
- Modify: `tests/test_matlab_project_acceptance.py`
- Modify: `tests/test_local_matlab_integration.py`

**Interfaces:**
- Produces: `LocalMatlabRequest.project_snapshot: ProjectSnapshot | None = None`
- Extends: `LocalMatlabBackend(interpreter: str, *, snapshots: ProjectSnapshotStore | None = None)`
- Extends: `LocalMatlabBackend.from_environment(*, snapshots: ProjectSnapshotStore | None = None) -> LocalMatlabBackend`
- Preserves: `execute(request: LocalMatlabRequest, directory: Path, *, resume: bool = False) -> LocalMatlabResult`
- Produces test helper: `bind_request(request: LocalMatlabRequest, snapshots: ProjectSnapshotStore) -> LocalMatlabRequest`
- Produces: `_work_directory(context: AgentContext, *, resume: bool) -> Path`
- Consumes: `AgentContext.project_snapshot`, managed `work_directory`, Task 2 `materialize`.
- Preserves: `LocalMatlabExecution.snapshot_root` denotes the **per-call execution copy** at `directory / "project"`; changing the historic worker wire field name is out of scope.

- [ ] **Step 1: RED — test real fake-Engine execution without the original source**

Add these tests to `tests/test_local_matlab.py`:

```python
def test_backend_executes_only_managed_snapshot_after_source_removal(
    local_backend, local_request, tmp_path, monkeypatch
):
    import shutil

    shutil.rmtree(local_request.project.root)
    module = importlib.import_module("flight_agent.verification.local_matlab")

    def forbidden(*args, **kwargs):
        raise AssertionError("Execution must not copy the original source")

    monkeypatch.setattr(module, "snapshot_matlab_project", forbidden, raising=False)
    directory = tmp_path / "work" / "local-matlab"
    result = local_backend.execute(local_request, directory)
    assert result.valid, result.diagnostics
    binding = local_request.project_snapshot
    immutable = local_backend.snapshots.verify(binding)
    assert (directory / "project" / "initialize.m").read_bytes() == (
        immutable / "initialize.m"
    ).read_bytes()
    assert result.request_sha256 == fingerprint(local_request.model_dump(mode="json"))


def test_tampered_managed_snapshot_never_starts_engine(
    local_backend, local_request, tmp_path, monkeypatch
):
    marker = tmp_path / "starts"
    monkeypatch.setenv("TEST_LOCAL_STARTS", str(marker))
    tree = local_backend.snapshots.verify(local_request.project_snapshot)
    path = tree / "initialize.m"
    path.chmod(0o644)
    path.write_text("changed = 1;\n")
    path.chmod(0o444)
    with pytest.raises(ValueError, match="digest"):
        local_backend.execute(local_request, tmp_path / "work")
    assert not marker.exists()


def test_new_project_execution_cannot_fall_back_to_bare_source(
    local_backend, local_request, tmp_path
):
    bare = local_request.model_copy(update={"project_snapshot": None})
    with pytest.raises(ValueError, match="snapshot"):
        local_backend.execute(bare, tmp_path / "bare")
    assert not (tmp_path / "bare").exists()
```

Add to `tests/test_local_matlab_adapters.py`:

```python
def test_adapter_uses_internal_work_not_external_evidence_directory(local_adapter_case):
    context, inputs, backend, tools = local_adapter_case
    output = CapabilityOutcome.model_validate(
        tools.invoke("matlab_execute_project", inputs, context, context.scope)
    )
    run = context.work_directory / "local-matlab"
    assert (run / "request.json").is_file()
    assert not list(context.directory.rglob("request.json"))
    assert not list(context.directory.rglob("state.json"))
    assert not list(context.directory.rglob("execution.json"))
    assert all((context.directory / item.path).is_file() for item in output.artifacts)
    assert backend.requests[0][0].project_snapshot == context.project_snapshot("project")


def test_historical_metric_checkpoint_reconciles_original_location(
    local_adapter_case, tmp_path
):
    context, inputs, backend, tools = metric_case(local_adapter_case)
    original_directory = context.directory / f"calls/{context.call_id}"
    historical = replace(context, work_directory=original_directory)
    backend.checkpoint = historical.load_checkpoint
    backend.lose_ack = True
    with pytest.raises(ExecutionUncertain):
        tools.invoke("matlab_stepinfo", inputs, historical, historical.scope)
    original_request = backend.requests[-1][0]
    historical.load_checkpoint().pop("backend_directory")
    restored = replace(
        historical, work_directory=tmp_path / "managed" / "restored-call"
    )
    backend.lose_ack = False
    output = tools.reconcile("matlab_stepinfo", inputs, restored, restored.scope)
    assert output is not None
    assert backend.requests[-2:] == [(original_request, False), (original_request, True)]
    assert (original_directory / "local-matlab" / "engine-starts.txt").read_text() == "1"
    assert not restored.work_directory.exists()
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_local_matlab.py tests/test_local_matlab_adapters.py -k 'internal_work or only_managed_snapshot or tampered_managed or bare_source or historical_metric_checkpoint' -q`

Expected: new request/binding fields or managed-work behavior are absent. The
fixtures are upgraded in Step 5 before GREEN; retain these behavioral assertions.

- [ ] **Step 3: GREEN — bind request identity without changing original provenance**

Add the field and serialization hook below to `LocalMatlabRequest`, importing
`ProjectSnapshot`, `model_serializer`, and `SerializerFunctionWrapHandler`.
Keep its existing operation, entry, signature, candidate, and working-directory
validators.

```python
project_snapshot: ProjectSnapshot | None = None

@model_serializer(mode="wrap")
def stable_project_snapshot_serialization(
    self, handler: SerializerFunctionWrapHandler
) -> dict[str, object]:
    result: dict[str, object] = handler(self)
    if self.project_snapshot is None:
        result.pop("project_snapshot", None)
    return result
```

Add this separate validator:

```python
@model_validator(mode="after")
def matching_project_snapshot(self) -> Self:
    if self.project_snapshot is not None:
        if self.project is None:
            raise ValueError("Artifact-only operations cannot supply a project snapshot")
        if (
            self.project_snapshot.source_fingerprint != self.project.fingerprint
            or [item.model_dump(mode="json") for item in self.project_snapshot.manifest.files]
            != [item.model_dump(mode="json") for item in self.project.files]
        ):
            raise ValueError("Request snapshot differs from the original project inventory")
    return self
```

Absence is allowed **only for deserialization/reconciliation compatibility**.
The backend rejects new source-bearing requests without a binding.

Replace `_request()` with this complete implementation; it retains the existing
scope/resource/version, selected input, artifact, budget, and original-request
checks while removing execution-time source rediscovery:

```python
def _request(
    context: AgentContext, inputs: LocalMatlabInput, *, resume: bool
) -> LocalMatlabRequest:
    context.scope.authorize("execute_project")
    project = (
        _project(context)
        if isinstance(inputs, (ProjectExecutionInput, LinearizeInput)) else None
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
        if context.work_directory is None:
            raise ValueError("Local MATLAB requires its managed per-call work directory")
        binding = context.project_snapshot("project") if project is not None else None
        if isinstance(inputs, StepinfoInput):
            _series(context, inputs)
        context.budget.check_limits()
        timeout = min(300, floor(context.budget.remaining_seconds()))
        if timeout < 1:
            raise BudgetExhausted("Less than one second remains for local MATLAB execution")
        request = LocalMatlabRequest(
            id=context.call_id, operation=operation, inputs=inputs, project=project,
            project_snapshot=binding, input_evidence=evidence, timeout_seconds=timeout,
        )
        context.save_checkpoint({
            "request": request.model_dump(mode="json"),
            "backend_directory": str(context.work_directory / "local-matlab"),
        })
    if (
        request.id != context.call_id or request.operation != operation
        or request.inputs != inputs or request.project != project
        or request.input_evidence != evidence
    ):
        raise ValueError("Local MATLAB checkpoint differs from the original confirmed invocation")
    if request.project_snapshot is not None:
        if request.project_snapshot != context.project_snapshot("project"):
            raise ValueError("Original request snapshot differs from the confirmed baseline")
    elif not resume and project is not None:
        raise ValueError("New local execution requires a confirmed managed snapshot")
    return request
```

For an original request without a binding, this permits only the historical
receipt-only reconciliation path; a new request has no bare-source fallback.

At the start of the adapter's `execute()` closure, before `_request()`, reject a
damaged project snapshot without entering the backend or creating a checkpoint:

```python
context.scope.authorize("execute_project")
if not resume and isinstance(inputs, (ProjectExecutionInput, LinearizeInput)):
    if backend.snapshots is None:
        raise ValueError("Local MATLAB requires its managed snapshot store")
    with backend.snapshots.guard():
        backend.snapshots.verify(context.project_snapshot("project"))
```

Add this helper and replace the old directory expression with
`directory = _work_directory(context, resume=resume)` for all four operations:

```python
def _work_directory(context: AgentContext, *, resume: bool) -> Path:
    saved = context.load_checkpoint()
    if saved is None:
        raise ExecutionUncertain("Original local MATLAB checkpoint is missing")
    if "backend_directory" not in saved:
        if not resume:
            raise ExecutionUncertain("Historical local execution requires reconciliation")
        # Reconstruct the original location from validated task/call identities.
        return context.directory / f"calls/{context.call_id}/local-matlab"
    if context.work_directory is None:
        raise ValueError("Local MATLAB requires its managed per-call work directory")
    expected = context.work_directory / "local-matlab"
    if saved["backend_directory"] != str(expected):
        raise ValueError("Local work directory differs from its authenticated checkpoint")
    return expected
```

The checkpoint marker is authenticated by the existing call/spec-bound
checkpoint digest. Never execute a path supplied by the marker: compare it to
the reconstructed managed path. A marker-free historical request reconciles
only at its original derived location; do not move old working files or rewrite
their absolute-path-bearing metadata. This also distinguishes new artifact-only
metric calls from historical calls without relying on `project is None`.

Keep the existing verified input-artifact staging into `directory.parent`,
`_series`, `_linear_model`, condition equality, producer metadata, and rechecks
before and after computation. `_outcome()` still publishes verified artifacts to
`context.directory` under `calls/<call-id>/published`; it must not expose mutable
working files as evidence. Add the following after constructing outcome details:

```python
if request.project_snapshot is not None:
    details["project_snapshot_id"] = request.project_snapshot.snapshot_id
```

This leaves historical outcomes unchanged. Metric-only lineage remains in the
invocation specification and its verified dependency receipts.

- [ ] **Step 4: GREEN — materialize from the managed store in the backend**

Extend the constructor and classmethod:

```python
def __init__(
    self, interpreter: str, *, snapshots: ProjectSnapshotStore | None = None
) -> None:
    self.interpreter = interpreter
    self.snapshots = snapshots
    self._readiness: tuple[str, dict[str, str], Availability] | None = None

@classmethod
def from_environment(
    cls, *, snapshots: ProjectSnapshotStore | None = None
) -> "LocalMatlabBackend":
    return cls(
        os.environ.get("MATLAB_PYTHON_EXECUTABLE") or sys.executable,
        snapshots=snapshots,
    )
```

In `execute()`, after request/path validation and after the existing early
`resume` branch, but **before** creating the run directory or probing readiness:

```python
if request.project is not None:
    if request.project_snapshot is None or self.snapshots is None:
        raise ValueError("New project execution requires a managed snapshot store and binding")
    with self.snapshots.guard():
        self.snapshots.verify(request.project_snapshot)
```

Replace only the project-bearing `snapshot_matlab_project` call branch with:

```python
if request.project is not None:
    assert self.snapshots is not None and request.project_snapshot is not None
    self.snapshots.materialize(request.project_snapshot, directory / "project")
```

Leave the `StepinfoInput` and `AllmarginInput` branches creating their independent
work input trees from verified dependency artifacts. No MATLAB operation uses the
immutable object as CWD, adds it as a writable project root, compiles into it, or
writes generated Simulink cache files there.

Replace the **post-execution** rediscovery of `request.project.root` with:

```python
if request.project_snapshot is not None:
    assert self.snapshots is not None
    with self.snapshots.guard():
        self.snapshots.verify(request.project_snapshot)
```

In `_resume()`'s existing `try`, verify the binding only when the original request
contains one:

```python
if request.project_snapshot is not None:
    if self.snapshots is None:
        raise ValueError("Original snapshot store is unavailable")
    with self.snapshots.guard():
        self.snapshots.verify(request.project_snapshot)
```

Keep `_resume()`'s existing request digest, worker-exit proof, Engine cleanup
proof, receipt/result hashes, and “no redispatch” handling. Do not materialize
again, rerun discovery, check current trusted-execution permission to block
receipt-only reconciliation, or start a process during resume.

Remove `discover_matlab_project` from the adapter's imports and remove
`discover_matlab_project`/`snapshot_matlab_project` from the backend's imports.
Keep `local_matlab_worker.py`'s `discover_matlab_project` import and its existing
work-copy descriptor comparison unchanged.

In `create_web_app()`, derive the snapshot store from the actual conversation
store (or injected runtime), call `LocalMatlabBackend.from_environment()` using
its existing no-argument injection seam, and then assign the shared store:

```python
snapshots = (
    engineering_runtime.executor.snapshots
    if engineering_runtime is not None
    else ProjectSnapshotStore(conversations.root / "snapshots")
)
local_backend.snapshots = snapshots
```

Construct a new executor with `snapshots=snapshots`. Ensure `conversations` is
created before this expression in the non-injected branch. Keep a single local
backend instance for readiness/catalog and plugin execution, as before. No store
directory is created merely by opening the workbench or inspecting capabilities.

- [ ] **Step 5: GREEN — upgrade fixtures without weakening confirmation tests**

Append the concrete helper:

```python
# tests/snapshot_helpers.py
def bind_request(request, snapshots):
    if request.project is None:
        return request
    with snapshots.guard():
        binding = snapshots.ensure(request.project)
    return type(request).model_validate({
        **request.model_dump(mode="json"),
        "project_snapshot": binding.model_dump(mode="json"),
    })
```

In `tests/test_local_matlab.py`, give `local_backend` a `tmp_path` parameter and
instantiate with
`ProjectSnapshotStore(tmp_path / "managed" / "snapshots")`. In `local_request`,
assign the existing request expression to `request` and return:

```python
return bind_request(
    request, ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
)
```

In `local_adapter_case`, create a store at that same fixture-relative managed
path, call `ensure(project)` while guarded, and add these context arguments:

```python
work_directory=tmp_path / "managed" / "work" / "calls" / ("a" * 32),
_project_snapshots={resource.id: binding},
```

Give `RecordingBackend.__init__` a `snapshots` parameter, call
`super().__init__(sys.executable, snapshots=snapshots)`, and construct it using
the fixture's store. Its `execute()` recording/checkpoint assertions remain
unchanged. In `metric_case` and `frequency_case`, give the derived metric context
its own work directory:

```python
work_directory=context.work_directory.parent / ("b" * 32),
```

When `frequency_case` deliberately changes the fixture **before** its simulated
confirmation, prepare a new binding and replace `_project_snapshots` along with
the resource:

```python
with backend.snapshots.guard():
    binding = backend.snapshots.ensure(project)
context = replace(
    context,
    resources=(resource,),
    scope=context.scope.model_copy(update={"resources": [resource]}),
    _resource_roles={"project": resource},
    _project_snapshots={resource.id: binding},
)
```

In `linear_request`, bind the newly discovered frequency project after creating
its typed request:

```python
return bind_request(
    request,
    ProjectSnapshotStore(Path(project.root).parent / "managed" / "snapshots"),
)
```

Replace the old adapter parameter case `"source"` in
`test_execution_refuses_unconfirmed_or_changed_binding` with `"snapshot"` and
tamper with the bound managed file instead:

```python
tree = backend.snapshots.verify(context.project_snapshot("project"))
path = tree / "initialize.m"
path.chmod(0o644)
path.write_text("changed = 1;\n")
path.chmod(0o444)
```

Keep the existing entry/signature/resource/version/permission/flag cases.
Source changes **before confirmation** remain rejected by Task 4 tests; source
changes **after confirmation** no longer mean the selected execution bytes
changed.

Replace assertions locating backend working files under
`context.directory / f"calls/{context.call_id}/local-matlab"` with
`context.work_directory / "local-matlab"`. In HTTP acceptance/restart tests,
enumerate original `request.json` files under
`runtime.store.root / "work" / conversation_id / task_id`, not the external
delivery result directory. Preserve byte-for-byte original-request, original-ID,
single-start, budget, receipt, and evidence assertions.

For direct licensed requests in `test_local_matlab_integration.py`, set
`actual_backend.snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")`
and use `bind_request(request, actual_backend.snapshots)` before `execute()`.
HTTP tests obtain their binding from real confirmation, never this test helper.

- [ ] **Step 6: Run GREEN**

Run:

```bash
python -m pytest tests/test_local_matlab.py tests/test_local_matlab_adapters.py tests/test_local_matlab_frequency.py tests/test_local_matlab_planning.py tests/test_matlab_project_acceptance.py -m "not matlab" -q
```

Expected: pass using the explicitly fake Engine. Check that project execution,
`stepinfo`, linearization, and `allmargin` all retain exact source artifact,
condition, metadata, numerical-validity, and unknown-state semantics.

- [ ] **Step 7: Commit**

```bash
git add src/flight_agent/verification/matlab_contracts.py src/flight_agent/verification/adapters/local_matlab.py src/flight_agent/verification/local_matlab.py src/flight_agent/web.py tests/snapshot_helpers.py tests/test_local_matlab.py tests/test_local_matlab_adapters.py tests/test_local_matlab_frequency.py tests/test_matlab_project_acceptance.py tests/test_local_matlab_integration.py
git commit -m "feat: isolate local MATLAB work from immutable project snapshots"
```

### Task 7: Add Explicit Reference-Aware Cleanup That Cannot Traverse External Roots

**Files:**
- Modify: `src/flight_agent/verification/project_snapshots.py`
- Modify: `src/flight_agent/verification/sqlite_store.py:SQLiteConversationStore.cleanup_project_snapshots`
- Create: `tests/test_project_snapshot_cleanup.py`

**Interfaces:**
- Produces: `SnapshotCleanupReport` with `retained: list[str]`, `would_delete: list[str]`, `deleted: list[str]`, `staging: list[str]`, `skipped: list[str]`
- Produces: `ProjectSnapshotStore.cleanup(retained_ids: set[str], *, dry_run: bool) -> SnapshotCleanupReport` (caller holds guard)
- Produces: `verify_object(snapshot_id: str, expected: SnapshotManifest) -> Path`, `remove_verified_object(snapshot_id: str, manifest: SnapshotManifest) -> None`
- Preserves: `remove_object(binding: ProjectSnapshot) -> None`
- Produces: `ConversationStore.cleanup_project_snapshots(*, dry_run: bool = True) -> SnapshotCleanupReport`
- Consumes: complete SQLite `retained_snapshot_ids`, never `recent()`, task status alone, or filesystem timestamps.
- Retention policy: every persisted current **or archived** confirmed task retains all its bindings, including completed/failed/interrupted tasks and tasks whose artifacts are delivered externally. No task/evidence purge or pin-release API is added in Stage 4.

- [ ] **Step 1: RED — protect retained evidence and delete only proven orphans**

```python
# tests/test_project_snapshot_cleanup.py
from pathlib import Path

from flight_agent.verification.store import ConversationStore
from tests.snapshot_helpers import snapshot_case
from tests.test_project_snapshot_references import record_with_binding


def test_dry_run_and_explicit_orphan_cleanup_preserve_external_directories(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "report.docx").write_bytes(b"retained external report")
    original = (source / "initialize.m").read_bytes()
    work = store.root / "work"
    work.mkdir()
    (work / "keep.txt").write_text("mutable work is not snapshot garbage")
    report = store.cleanup_project_snapshots()
    assert report.would_delete == [binding.snapshot_id]
    assert report.deleted == []
    snapshots.verify(binding)
    removed = store.cleanup_project_snapshots(dry_run=False)
    assert removed.deleted == [binding.snapshot_id]
    assert not snapshots.object_directory(binding.snapshot_id).exists()
    assert (source / "initialize.m").read_bytes() == original
    assert (delivery / "report.docx").read_bytes() == b"retained external report"
    assert (work / "keep.txt").is_file()


def test_archived_terminal_baseline_retains_snapshot_after_restart(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    record.task.stage = "completed"
    record.archived_tasks.append(record.task)
    record.task = None
    record.status = "completed"
    with snapshots.guard():
        store.save(record)
    restarted = ConversationStore(store.root)
    report = restarted.cleanup_project_snapshots(dry_run=False)
    assert report.retained == [binding.snapshot_id]
    assert report.deleted == []
    snapshots.verify(binding)


def test_corrupt_unreferenced_object_is_reported_not_deleted(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    path = snapshots.object_directory(binding.snapshot_id) / "manifest.json"
    path.chmod(0o644)
    path.write_text("{}")
    report = store.cleanup_project_snapshots(dry_run=False)
    assert binding.snapshot_id in report.skipped
    assert report.deleted == []
    assert path.exists()


def test_symlinked_object_is_never_followed(tmp_path):
    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    foreign_id = "f" * 64
    shard = snapshots.root / "objects" / "ff"
    shard.mkdir(exist_ok=True)
    (shard / foreign_id).symlink_to(source, target_is_directory=True)
    report = store.cleanup_project_snapshots(dry_run=False)
    assert foreign_id in report.skipped
    assert (source / "initialize.m").is_file()


def test_missing_reference_index_row_prevents_all_cleanup(tmp_path):
    import pytest

    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    with snapshots.guard():
        store.save(record)
    with store.transaction():
        store._local.connection.execute("DELETE FROM project_snapshot_references")
    with pytest.raises(ValueError, match="reference index"):
        store.cleanup_project_snapshots(dry_run=False)
    snapshots.verify(binding)
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_project_snapshot_cleanup.py -q`

Expected: missing cleanup method. No test may delete its source fixture merely
to make the expected object list smaller.

- [ ] **Step 3: GREEN — enumerate only owned canonical objects and staged copies**

Add the report and method to `project_snapshots.py`:

```python
from dataclasses import dataclass, field


@dataclass
class SnapshotCleanupReport:
    retained: list[str] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    staging: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
```

Add this method inside `ProjectSnapshotStore`:

```python
def cleanup(
    self, retained_ids: set[str], *, dry_run: bool
) -> SnapshotCleanupReport:
    report = SnapshotCleanupReport(retained=sorted(retained_ids))
    for shard in sorted((self.root / "objects").iterdir()):
        if (
            re.fullmatch(r"[a-f0-9]{2}", shard.name) is None
            or shard.is_symlink() or not shard.is_dir()
        ):
            report.skipped.append(shard.name)
            continue
        for obj in sorted(shard.iterdir()):
            identifier = obj.name
            if identifier in retained_ids:
                continue
            if (
                _DIGEST.fullmatch(identifier) is None
                or not identifier.startswith(shard.name)
                or obj.is_symlink() or not obj.is_dir()
            ):
                report.skipped.append(identifier)
                continue
            try:
                raw = _read_regular(obj / "manifest.json", MAX_MANIFEST_BYTES)
                manifest = SnapshotManifest.model_validate_json(raw)
                self.verify_object(identifier, manifest)
            except (OSError, ValueError):
                report.skipped.append(identifier)
                continue
            report.would_delete.append(identifier)
            if not dry_run:
                self.remove_verified_object(identifier, manifest)
                report.deleted.append(identifier)
    for temporary in sorted((self.root / "staging").iterdir()):
        if (
            re.fullmatch(r"snap-[a-f0-9]{32}", temporary.name) is None
            or temporary.is_symlink() or not temporary.is_dir()
        ):
            report.skipped.append(temporary.name)
            continue
        report.staging.append(temporary.name)
        if not dry_run:
            _remove_owned_directory(temporary)
    return report
```

`staging` lists cleanup-eligible abandoned staging directories in both modes;
`dry_run=False` removes them. There is no active creator while this guard is
held. Unknown names, corrupt objects, and symlinks are reported, not repaired
or recursively followed. Snapshot cleanup never enumerates the sibling `work`
tree, any `result_directory`, `output_root`, Worker locator, or source locator.

Expose content-only verification/deletion to cleanup without inventing a source
fingerprint. Make these exact Task 2 method changes:

```python
def verify(self, binding: ProjectSnapshot) -> Path:
    binding = ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
    return self.verify_object(binding.snapshot_id, binding.manifest)

def verify_object(self, snapshot_id: str, expected: SnapshotManifest) -> Path:
    obj = self.object_directory(snapshot_id)
    if not obj.is_dir() or {item.name for item in obj.iterdir()} != {
        "manifest.json", "tree"
    }:
        raise ValueError("Snapshot object is missing or has unexpected members")
    raw = _read_regular(obj / "manifest.json", MAX_MANIFEST_BYTES)
    manifest = SnapshotManifest.model_validate_json(raw)
    if (
        manifest != expected or raw != manifest.canonical_bytes()
        or manifest.snapshot_id != snapshot_id
    ):
        raise ValueError("Snapshot manifest identity mismatch")
    if obj.stat().st_mode & 0o222 or (obj / "manifest.json").stat().st_mode & 0o222:
        raise ValueError("Immutable snapshot object is writable")
    _verify_tree(obj / "tree", manifest, readonly=True)
    return obj / "tree"

def remove_verified_object(self, snapshot_id: str, manifest: SnapshotManifest) -> None:
    self.verify_object(snapshot_id, manifest)
    obj = self.object_directory(snapshot_id)
    _remove_owned_directory(obj)
    _sync_directory(obj.parent)

def remove_object(self, binding: ProjectSnapshot) -> None:
    binding = ProjectSnapshot.model_validate(binding.model_dump(mode="json"))
    self.remove_verified_object(binding.snapshot_id, binding.manifest)
```

Task 2's `_remove_owned_directory()` already requires `st_nlink == 1` before
changing a regular file's permissions, preventing chmod of linked external
files. A deletion error is explicit; it does not report a successful deletion.
An interruption may leave an unreferenced partial garbage object, but can never
delete a referenced snapshot.

In `SQLiteConversationStore` (also available through the existing `ConversationStore` alias):

```python
def cleanup_project_snapshots(
    self, *, dry_run: bool = True
) -> SnapshotCleanupReport:
    snapshots = ProjectSnapshotStore(self.root / "snapshots")
    with snapshots.guard(exclusive=True):
        with self._read() as connection:
            retained = retained_snapshot_ids(connection)
        return snapshots.cleanup(retained, dry_run=dry_run)
```

Do not delete when the SQLite reference query/migration fails. Never fall back to
an empty retained set, a partial conversation listing, or “terminal means unused.”
Retained receipt/delivery ownership remains rooted by its persisted task row; a
future retention feature must not delete that row while evidence is retained.

- [ ] **Step 4: RED/GREEN — prove publication-to-reference cleanup race safety**

Append:

```python
def test_cleanup_cannot_race_confirmation_before_reference_commit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    source, project, snapshots, binding = snapshot_case(tmp_path)
    store = ConversationStore(tmp_path / "managed")
    record = record_with_binding(store, binding)
    published = Event()
    permit_commit = Event()
    cleanup_started = Event()

    def confirm():
        with snapshots.guard():
            snapshots.ensure(project)
            published.set()
            assert permit_commit.wait(timeout=10)
            with store.transaction():
                store.save(record)

    def collect():
        cleanup_started.set()
        return store.cleanup_project_snapshots(dry_run=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirming = pool.submit(confirm)
        assert published.wait(timeout=10)
        collecting = pool.submit(collect)
        assert cleanup_started.wait(timeout=10)
        permit_commit.set()
        confirming.result(timeout=10)
        report = collecting.result(timeout=10)
    assert report.deleted == []
    assert report.retained == [binding.snapshot_id]
    snapshots.verify(binding)
```

The publication worker holds the guard until the reference commit; collection
uses the same guard through deletion. Keep this event/barrier-controlled race,
not sleep-based timing or a process-local mock mutex.

Run: `python -m pytest tests/test_project_snapshot_cleanup.py tests/test_project_snapshot_references.py tests/test_project_snapshot_store.py -q`

Expected: pass; dry run is nondestructive, archived terminal evidence stays
reachable after restart, and cleanup cannot observe the publication/reference gap.

- [ ] **Step 5: Commit**

```bash
git add src/flight_agent/verification/project_snapshots.py src/flight_agent/verification/sqlite_store.py tests/test_project_snapshot_cleanup.py
git commit -m "feat: clean only unreferenced managed project snapshots"
```

### Task 8: Close Integration Regressions and Document the Storage Contract

**Files:**
- Modify: `tests/test_agent_project_snapshots.py`
- Modify: `tests/test_project_snapshot_store.py`
- Modify: `docs/architecture.md`
- Modify: `docs/usage.md`

**Interfaces:**
- Consumes: all Tasks 1–7 interfaces and existing Worker/offline diagnostic capability contracts.
- Produces: software-only acceptance coverage, documented cleanup/operator behavior, licensed acceptance commands.

- [ ] **Step 1: RED — retain Worker and offline diagnostic verification semantics**

Append to `tests/test_agent_project_snapshots.py`:

```python
@pytest.mark.parametrize(
    "kind,location", [("simulink_project", "worker"), ("diagnostic_data", "agent")]
)
def test_non_snapshot_resources_still_use_registered_verification(tmp_path, kind, location):
    from flight_agent.runtime.agent_contracts import AgentBaseline
    from flight_agent.verification.snapshot_binding import verify_execution_resources
    from tests.agent_helpers import analysis_plan_data

    resource = ResourceReference(
        id="external", kind=kind, location=location,
        locator=str(tmp_path / "external"), fingerprint="a" * 64,
    )
    data = analysis_plan_data()
    baseline = AgentBaseline.model_validate({
        **data, "resources": [resource.model_dump(mode="json")],
        "version": 1, "confirmed": True, "confirmed_at": "2026-10-31T00:00:00+00:00",
    })
    registry = ResourceRegistry()
    seen = []

    def reject_changed(current):
        seen.append(current)
        raise ValueError("Registered resource changed")

    registry.register(kind, location, reject_changed, local=location == "agent")
    with pytest.raises(ValueError, match="changed"):
        verify_execution_resources(baseline, registry)
    assert seen == [resource]
    assert baseline.project_snapshots == {}
```

Add the concrete source-symlink/special-file and same-timestamp test to
`tests/test_project_snapshot_store.py`:

```python
def test_same_size_and_timestamp_do_not_hide_tampering(tmp_path):
    source, project, store, binding = snapshot_case(tmp_path)
    path = store.verify(binding) / "initialize.m"
    before = path.stat()
    raw = path.read_bytes()
    changed = bytes([raw[0] ^ 1]) + raw[1:]
    path.chmod(0o644)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    path.chmod(0o444)
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(ValueError, match="digest"):
        store.verify(binding)


def test_source_symlink_replacement_before_copy_fails_closed(tmp_path):
    source, project, existing, binding = snapshot_case(tmp_path)
    store = ProjectSnapshotStore(tmp_path / "other" / "snapshots")
    path = source / "initialize.m"
    path.unlink()
    path.symlink_to(source / "README.md")
    with store.guard(), pytest.raises(ValueError, match="symlink"):
        store.ensure(project)
    assert not list((store.root / "objects").glob("*/*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO type test")
def test_source_special_file_is_never_copied(tmp_path):
    source, project, existing, binding = snapshot_case(tmp_path)
    store = ProjectSnapshotStore(tmp_path / "other" / "snapshots")
    os.mkfifo(source / "pipe")
    with store.guard(), pytest.raises(ValueError, match="special file"):
        store.ensure(project)
    assert not list((store.root / "objects").glob("*/*"))
```

Run: `python -m pytest tests/test_agent_project_snapshots.py tests/test_project_snapshot_store.py -q`

Expected: existing implementation should already satisfy these regressions.
If any fails, keep it RED until fixing the specific use boundary; never relax
the registered resource or digest checks.

- [ ] **Step 2: GREEN — document actual operator-visible behavior**

Add this section to `docs/architecture.md`:

```markdown
## Confirmed Local Project Snapshots

Static discovery is read-only. Exact-version trusted confirmation revalidates the
selected source fingerprint, verifies or atomically publishes a canonical
content-addressed snapshot, and commits its identity and manifest with the task.
The immutable object lives under the conversation store's `snapshots/objects`.
The original resource reference remains provenance; it is not rewritten into an
execution path.

New local project execution and linearization copy verified immutable bytes into
managed `work/<conversation>/<task>/calls/<call>/local-matlab/project` directories.
stepinfo and allmargin use independently copied, verified direct-dependency
artifacts, with the originating snapshot lineage in their invocation receipts.
MATLAB never runs in the immutable object. New backend working files, `request.json`, and `state.json` do not live in an external
delivery directory. Authenticated Agent checkpoints and immutable receipts retain
their established evidence-store location and digest checks.

Every use, including cache reuse and new-style reconciliation, verifies the full
manifest and file digests. Source changes after confirmation do not redirect
execution. A later source version requires a new proposal and confirmation.
Tampered managed objects fail closed and are never silently repaired.

Cleanup uses a cross-process lifecycle guard and the complete SQLite reference
index. Current and archived persisted task baselines retain snapshots regardless
of terminal status, preserving retained evidence. Cleanup is explicit and dry-run
by default; it never traverses sources, Worker locators, delivery roots, or work
directories. Read-only permissions are not a security sandbox.
```

Add to `docs/usage.md`:

```markdown
### Snapshot confirmation and cleanup

A changed source fingerprint before confirmation requires rediscovery and a new
confirmation. After confirmation, local operations use the selected immutable
snapshot even if the original source is edited or removed. Worker project and
offline diagnostic resource verification retain their existing behavior.

Historical v2 confirmations and invocation hashes remain readable. A historical
local task without a snapshot binding cannot dispatch new project work; propose
and confirm a new baseline. An unresolved historical call must first reconcile
its original receipt and is never resubmitted automatically.

From the application checkout, preview snapshot cleanup for the default store:

    python -c "from pathlib import Path; from flight_agent.verification.store import ConversationStore; print(ConversationStore(Path('runs/verification').resolve()).cleanup_project_snapshots())"

After reviewing the report, delete only verified unreferenced managed objects:

    python -c "from pathlib import Path; from flight_agent.verification.store import ConversationStore; print(ConversationStore(Path('runs/verification').resolve()).cleanup_project_snapshots(dry_run=False))"

Use the actual conversation-store root if the application runs in another state
directory. A database error prevents deletion. Corrupt or ambiguous objects are
reported for manual investigation. Completion alone never releases evidence
retention, and deleting external files is not a request to release their snapshot.
```

No automatic startup cleanup is introduced.

- [ ] **Step 3: Run targeted software-only acceptance**

```bash
python -m pytest tests/test_project_snapshot_contracts.py tests/test_project_snapshot_store.py tests/test_project_snapshot_references.py tests/test_project_snapshot_cleanup.py tests/test_agent_project_snapshots.py tests/test_agent_runtime.py tests/test_agent_recovery.py tests/test_agent_adapters.py tests/test_agent_diagnostic_engine.py tests/test_agent_delivery.py tests/test_local_matlab.py tests/test_local_matlab_adapters.py tests/test_local_matlab_frequency.py tests/test_matlab_project_acceptance.py -m "not matlab" -q
```

Expected: pass, including source immutability, snapshot reuse, digest tamper
detection, original-ID reconciliation, retained evidence, and both supported
non-local-project capability families.

- [ ] **Step 4: Run the complete Stage 4 software gate**

```bash
python -m pytest -m "not matlab" -q
python -m ruff check src tests
python -m mypy src
python -m build
git diff --check
```

Expected: all exit `0`. Browser tests retain their established browser-installed
prerequisite; do not disguise an unavailable browser as a passing acceptance run.
Check Stage 3 concurrency regressions in the full suite: independent conversations
can plan concurrently, one conversation remains serialized, and local MATLAB
does not exceed its configured slot count. No lifecycle lock surrounds the
scheduler wait or backend process execution.

- [ ] **Step 5: Run licensed acceptance only in an explicitly enabled environment**

```bash
RUN_MATLAB_TESTS=1 MATLAB_ALLOW_TRUSTED_PROJECTS=1 python -m pytest tests/test_local_matlab_integration.py -m matlab -q
```

Expected: real project execution plus `stepinfo`, real linearization plus
`allmargin`, verified JSON/Word report artifacts, unchanged external source
inventory, and unchanged immutable snapshot inventory after mutable work. Retain
the existing licensed test's platform/toolbox requirements. If MATLAB is absent,
report this gate as **not run**, not passed; the software-only gate remains required.

- [ ] **Step 6: Commit the Stage 4 acceptance/documentation boundary**

```bash
git add tests/test_agent_project_snapshots.py tests/test_project_snapshot_store.py docs/architecture.md docs/usage.md
git commit -m "test: verify immutable snapshot lifecycle and document Stage 4"
```

## Plan Self-Review and Acceptance Traceability

Run these checks while authoring/reviewing this document, not by dispatching an
implementation worker.

| Approved requirement | Implementation / proof |
|---|---|
| Bounded read-only discovery | Existing discovery preserved; Tasks 1/2 enforce 5000-file / 512 MB inventory, Task 4 forbids preconfirmation snapshot creation |
| Confirmed source fingerprint revalidation | Task 4 trusted/version checks and all-resource post-copy revalidation; Task 2 pre/post source discovery and copy digest checks |
| Content identity from confirmed inventory | Task 1 canonical path/size/SHA256 manifest; source roots and timestamps excluded |
| Atomic verified creation/reuse | Task 2 staging/fsync/rename/full verification, concurrent store/process tests, copy-failure tests |
| Snapshot identity and manifest in task baseline | Tasks 1/3/4 full binding plus SQLite transactional reference index and confirmation hash |
| All dependent local calls use one immutable baseline | Tasks 5/6 direct/transitive lineage and project/metric execution tests |
| Separate mutable work | Task 6 internal call work directories; no hard links or execution inside immutable objects |
| Tamper detection before use | Tasks 2/5/6/8 exact-tree/manifest/digest checks, same-timestamp test, blocked second dispatch |
| Reference-aware cleanup | Task 7 complete SQLite reachability, archived terminal retention, dry run, cross-process guard, orphan-only deletion |
| Failure/restart safety | Tasks 2/3 orphan-safe publication and transaction rollback; Tasks 5/6 original-ID receipt-only reconciliation |
| Existing v2 evidence readable | Tasks 1/6 omit absent new fields from serialization; no historical baseline or receipt rewriting |
| Worker and offline diagnostics preserved | Task 8 unchanged verifiers; existing adapter/diagnostic acceptance remains required |
| Source/delivery immutability | Tasks 4/6/7 disjoint roots, no original-source copy after confirmation, only verified evidence publication, cleanup never traverses external roots |
| Stage 2/3 boundaries | Explicit upstream contracts; one database, retained scheduler routing, no new global busy behavior |

Self-review must also check every downstream signature against the Interfaces
blocks, scan for unresolved instructions or empty code steps, verify each Task's
RED/GREEN command and explicit commit file list, and ensure the only output of
this authoring session is this plan. No Stage 5 planner/context work, Stage 6
dependency/CI changes, production implementation, commit, or dispatch belongs to
the authoring session.
