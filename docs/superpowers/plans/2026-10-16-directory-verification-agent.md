# Directory-based D1 Verification Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a folder-input conversational Agent that confirms a verification baseline, runs actual Simulink time/frequency analysis and saves traceable engineering reports.

**Architecture:** Preserve the existing Web and authenticated worker; add a bounded verification module with typed project, plan, execution and evidence contracts. A registered-tool Runtime delegates numerical work to a Simulink adapter operating on an isolated project copy, then performs deterministic evaluation and Word delivery. Existing upload behavior remains available at `/workbench`.

**Tech Stack:** Python >=3.11, Pydantic 2, FastAPI, LangChain 1 Chat Completions tools, MATLAB Engine, Simulink, Simulink Control Design, Control System Toolbox, python-docx, pytest, Playwright, Ruff and mypy.

**Spec:** `docs/d1-agent.md`

**Progress (2026-10-17):** Tasks 1-7 are implemented, including real synthetic
directory-to-report integration, deployment documentation, whole-project
checks, final recovery-path review and packaged delivery. Live F-16 compilation,
independent F-16 references/tolerances and a configured real LLM remain separate
external prerequisites, not synthetic-test substitutes.

## Global Constraints

- User engineering input is one folder path on the MATLAB worker.
- D1 supports an existing trusted project, trim point 1, step response and open-loop margins.
- Step settling band is 5%; demonstration criteria are <=10%, <=3 s, >=0 dB and >=0 degrees, subject to explicit baseline confirmation.
- Never intentionally modify source `.m`, `.slx`, control parameters, plant models or topology.
- No arbitrary model-generated MATLAB, automatic trimming, optimization or root-cause diagnosis.
- Integrate existing changes when useful. The user subsequently authorized replacing
  the old project UX; legacy compatibility is not a delivery constraint.
- Separate execution status from PASS / FAIL / UNDETERMINED.
- Questions terminate the current turn; subsequent turns resume persisted nodes.
- No automatic duplicate remote submission or repeated completed simulation.
- No numerical acceptance claim without independent F-16 reference results and agreed tolerances.
- Work in the current directory, as explicitly selected by the user.

## File and interface map

`src/flight_agent/verification/` owns the new bounded context:

| File | Responsibility |
|---|---|
| `contracts.py` | Validated project, baseline, plan, metric, artifact, run and conversation schemas |
| `projects.py` | Read-only discovery, manifest/profile resolution, fingerprints and project snapshots |
| `store.py` | Atomic task/conversation/worker state |
| `artifacts.py` | Bounded, allowlisted artifact metadata and integrity verification |
| `evaluation.py` | Metric validity, units, criteria and aggregate compliance |
| `reports.py` | Evidence-backed Word reports, JSON records and artifact indexes |
| `matlab.py` | Bounded isolated MATLAB child execution and output validation |
| `backend.py` | Backend protocol and authenticated remote Simulink transport |
| `worker.py` | Project inspection and idempotent worker-run routes |
| `planner.py` | Typed LLM proposals, evidence explanation and bounded replan responses |
| `runtime.py` | Registered-tool executor, confirmation, continuation and result reuse |
| `routes.py` | Authenticated conversation/task/artifact Web API |

MATLAB functions are packaged under `src/flight_agent/matlab_support/`.
New static assets are `agent.html`, `agent.js`, `agent.css`.
Existing `web.py`, `worker.py`, `cli.py`, `remote.py`, `pyproject.toml`,
`.env.example`, README and directly affected tests are changed only as required.

## Task 1: Validated project and evaluation contracts

Progress: implemented; 34 project/evaluation tests passed. The actual supplied
directory is recognized as an 81-file package without executing any project code.

**Files:** Create `verification/__init__.py`, `contracts.py`, `projects.py`,
`evaluation.py`; create `tests/test_verification_projects.py` and
`tests/test_verification_evaluation.py`.

**Interfaces:**
- Produces `ProjectInspector.inspect(root: str) -> ProjectDescriptor`.
- Produces `snapshot_project(project: ProjectDescriptor, destination: Path) -> Path`.
- Produces `evaluate(baseline: EvaluationBaseline, result: AnalysisResult) -> Evaluation`.
- Contracts define relative resource paths, fingerprints, analysis kinds,
  criteria, metric special values and evidence references for every later task.

- [x] Write project-discovery tests using a small prepared project tree. Reject a
  single file input, traversal, external symlinks, ambiguous entries and missing
  dependencies; changing a dependency must change the fingerprint.

```python
def test_project_fingerprint_changes_with_dependency(prepared_project):
    inspector = ProjectInspector()
    first = inspector.inspect(str(prepared_project))
    (prepared_project / "data" / "input.mat").write_bytes(b"new data")
    second = inspector.inspect(str(prepared_project))
    assert second.fingerprint != first.fingerprint
```

- [x] Run `.venv/bin/python -m pytest -q tests/test_verification_projects.py`.
  Confirm the expected missing-feature failure.
- [x] Implement strict contracts, the supplied F-16 profile and the generic
  `flight-agent.project.json` path. Walk bounded regular files without following
  symlinks, hash contents and copy only the verified package into a new directory.
- [x] Write table-driven evaluation tests. Literal cases cover boundary equality,
  invalid values, wrong units, required-metric omission and both analyses.

```python
@pytest.mark.parametrize(
    "overshoot, settling, expected",
    [(10.0, 3.0, "pass"), (10.1, 2.0, "fail"), (1.0, 3.01, "fail")],
)
def test_step_criteria_boundaries(step_baseline, step_result, overshoot, settling, expected):
    result = step_result(overshoot=overshoot, settling=settling)
    assert evaluate(step_baseline, result).status == expected
```

- [x] Run the new tests, implement deterministic comparison and explicit special
  values, and repeat until green. A missing required metric yields undetermined,
  not a partial PASS or FAIL.
- [x] Review the diff for source-directory writes and keep all numerical
  assertions independent of the implementation.

## Task 2: Numerical adapter and isolated worker execution

**Files:** Create `verification/matlab.py`; create
`matlab_support/flight_agent_verify.m`,
`matlab_support/flight_agent_step_metrics.m`,
`matlab_support/flight_agent_margin_metrics.m`;
create `tests/test_verification_matlab.py` and controlled MATLAB fixtures.
Modify the existing cleanup helper only where cross-platform execution requires it.

**Interfaces:**
- Consumes `ProjectDescriptor`, `AnalysisRunRequest`, `AnalysisResult`.
- Produces `execute_project(directory: Path, request: AnalysisRunRequest) -> AnalysisResult`.
- MATLAB `flight_agent_verify(job_directory)` reads a validated request, runs
  only registered analysis branches and writes `analysis.json` plus artifacts.

- [x] Write tests for timeout, sanitized child environment, corrupted output,
  wrong project/trim context, missing artifacts and cleanup failure.
- [x] Run `.venv/bin/python -m pytest -q tests/test_verification_matlab.py -m 'not matlab'`.
  Confirm failures before implementing the adapter.
- [x] Implement a dedicated child process with the selected worker interpreter,
  bounded logs/results, source snapshot, explicit MATLAB cleanup and persisted
  progress. Do not add an eval/code-generation endpoint.
- [x] Write a real-MATLAB synthetic model fixture and marked tests:

```python
@pytest.mark.matlab
def test_real_simulink_margins(synthetic_project, run_real_project):
    result = run_real_project(synthetic_project, ["stability_margins"])
    margins = result.analysis("stability_margins")
    assert margins.metrics["phase_margin"].value == pytest.approx(60.0, abs=0.1)
    assert margins.metrics["gain_margin"].special == "positive_infinity"
```

- [x] Run the marked test with the MATLAB-compatible Python and observe the
  missing analysis behavior.
- [x] Implement explicit initialization, trim verification, model loading,
  `getlinio`, initialized/workspace operating points, `linearize`, `allmargin`
  and the registered physical loop-break sign (`feedback_sign=1` for the supplied
  profile), without blindly negating the open loop. Retain all crossings and special cases.
- [x] Add and run delayed/nonzero-initial-value step tests, no-change and
  nonsettling cases; implement actual signal alignment, steady-state screening
  and `stepinfo(..., SettlingTimeThreshold=0.05)`.
- [x] Export actual time/Bode PNG and MAT evidence. A failed branch retains its
  error and does not invent metrics; independent branches may still finish.

## Task 3: Authenticated project transport and durable worker runs

**Files:** Create `verification/backend.py`, `verification/store.py`,
`verification/worker.py`; modify `worker.py`, `cli.py` and `.env.example`;
create `tests/test_verification_worker.py`.

**Interfaces:**
- `AnalysisBackend.inspect(root: str) -> ProjectDescriptor`.
- `AnalysisBackend.execute(request: AnalysisRunRequest, directory: Path,
  on_progress: Callable[[RunRecord], None], *, resume: bool = False) -> AnalysisResult`.
- `RemoteSimulinkBackend` implements this protocol through authenticated
  `/verification/projects/inspect` and `/verification/runs` routes.
- `RunRecord` persists the request hash, idempotency key, state, result, log and
  allowlisted artifact metadata.

- [x] Write API tests rejecting missing auth/project consent, unknown artifact
  paths and concurrent work. Repeat a run ID with identical content and assert
  one execution; a changed payload with the same ID returns conflict.

```python
def test_repeated_submission_reuses_worker_run(worker_client, run_request, auth):
    first = worker_client.post("/verification/runs", json=run_request, headers=auth)
    again = worker_client.post("/verification/runs", json=run_request, headers=auth)
    assert first.json()["id"] == again.json()["id"]
    assert first.status_code == again.status_code == 202
```

- [x] Run worker tests and observe failures.
- [x] Implement opt-in trusted-project routes with existing bearer/body guards.
  Share the numerical execution slot with the existing upload manager.
- [x] Persist request identity before scheduling; on restart mark unknown
  in-flight work interrupted rather than running it again.
- [x] Implement bounded authenticated polling and artifact transfer with digest
  verification. Never expose a generic file-read endpoint.
- [x] Run old/new worker and HTTP tests together to preserve legacy behavior.

## Task 4: Evidence-backed local delivery

**Files:** Create `verification/reports.py`; modify `pyproject.toml`;
create `tests/test_verification_reports.py`.

**Interfaces:**
- Consumes confirmed baseline, analysis result, evaluation, recorded explanation
  and execution history.
- Produces `deliver_report(request: ReportRequest, directory: Path) -> list[Artifact]`.
- Writes evaluation, execution and artifact-index JSON plus combined/separate Word reports.

- [x] Add tests that open the produced DOCX ZIP and inspect document content,
  image relationships and analysis coverage. Test no missing metrics/figures,
  exact units/criteria, multi-report indexing and unsafe artifact rejection.
- [x] Run tests and confirm missing implementation/dependency failures.
- [x] Add `python-docx>=1.1,<2` to the manifest and install the changed project
  dependency in the existing environment.
- [x] Implement templated numerical tables and evidence indexes. LLM explanation
  is clearly labeled and cannot replace the deterministic result.
- [x] Test report regrouping using existing MAT/PNG files without calling MATLAB.
  Verify every recorded artifact exists and its hash matches before completion.

## Task 5: Planning, confirmation, continuation and generic execution

**Files:** Create `verification/planner.py`, `verification/runtime.py`;
extend `verification/store.py`; create `tests/test_verification_runtime.py`.

**Interfaces:**
- `VerificationPlanner.propose(message, conversation, project) -> PlanProposal`.
- `VerificationPlanner.explain(task, question=None) -> EvidenceExplanation`.
- `VerificationPlanner.replan(task, failure) -> PlanProposal`.
- `VerificationRuntime.submit(conversation_id, message, project_root=None) -> Conversation`.
- The Runtime owns a registered tool catalog and invokes nodes only after a
  current baseline is confirmed by the user.

- [x] Write behavioral tests using the repository's scripted chat model and a
  controlled numerical backend. Assert no numerical call before confirmation,
  versioned confirmation, rejected invented tools, and failure persistence.

```python
def test_confirmation_resumes_once(runtime, conversation_request, prepared_project):
    conversation = runtime.submit(None, conversation_request, str(prepared_project))
    assert conversation.status == "waiting_for_user"
    assert conversation.task.result is None
    confirmed = runtime.confirm(
        conversation.id, conversation.task.baseline.version, trusted=True
    )
    assert confirmed.task.evaluation.status in {"pass", "fail", "undetermined"}
    assert confirmed.task.result is not None
```

- [x] Run tests and observe the missing Runtime.
- [x] Implement typed tool-call planning with tracing disabled. Bind project
  identity and approval server-side, validate criteria/capabilities and persist
  plan versions before execution.
- [x] Implement generic registered nodes for analysis, evaluation and delivery,
  recording each input/result/error. Normal computation does not call the LLM
  between every node.
- [x] Implement bounded feedback/replanning and waiting questions that return
  control to the user. Restore saved state on the next turn without an idle
  inference loop.
- [x] Add threshold-only/report-only reuse, changed-project invalidation,
  combined tasks, additional-analysis turns, interrupted remote reconciliation
  and missing-input tests; implement only after each corresponding red test.
- [x] Verify the model receives bounded semantic evidence, not source files,
  credentials or raw full-size time histories.

## Task 6: Conversation Web UI and compatibility

**Files:** Create `verification/routes.py`, `static/agent.html`,
`static/agent.js`, `static/agent.css`; modify `web.py`; create
`tests/test_verification_web.py`, `tests/test_verification_browser.py`;
adjust legacy browser navigation to `/workbench` without changing its assertions.

**Interfaces:**
- Authenticated `/api/conversations` submission/list/status/message/decision routes.
- Conversation responses include current baseline, plan stage, pending decision,
  numerical summary, artifact manifest and local result paths.
- `/` is the directory-based dialogue; `/workbench` preserves the upload page.

- [x] Write API tests for same-origin/token enforcement, validation errors,
  persisted confirmation/resume and protected downloads.
- [x] Write a browser test that enters a folder and natural-language request,
  sees the baseline, confirms, receives a result and downloads a DOCX.
- [x] Run API/browser tests and observe failures.
- [x] Implement the routes and accessible conversation interface: a folder-path
  field, message composer, baseline confirmation, stage/status feedback, history,
  result paths and artifact downloads. Render all model/project text as text.
- [x] Keep requests bound to the displayed conversation and baseline version;
  ignore stale polling responses after switching conversations.
- [x] Run the old/new browser tests and exercise disconnected/unconfigured states.

## Task 7: Integration, documentation and handoff

**Files:** Update README and `docs/d1-agent.md` with verified commands and limits.

- [x] Run targeted suites together, then `.venv/bin/python -m pytest -q -m 'not matlab'`.
- [x] Run `.venv/bin/python -m ruff check .` and `.venv/bin/python -m mypy src`.
- [x] Run browser coverage with `RUN_BROWSER_TESTS=1`.
- [x] Run marked numerical tests in `.venv-matlab`; use a prepared copy of the
  user's F-16 package if its platform dependency can be built without source edits.
- [x] If platform/compiler/license or model credentials block live integration,
  record the exact failure and keep the product error actionable; do not invent
  data or weaken trust/permission controls.
- [x] Review the complete diff and source-fingerprint evidence. Preserve all
  unrelated original changes and remove only task-owned temporary probes.
- [x] Build source/wheel distributions and inspect the packaged verification
  modules, MATLAB support routines and directory UI resources.
- [x] Document environment setup, folder ownership, manifest format, confirmation,
  output paths, resume behavior and the legacy workbench.
- [x] Deliver verified implementation status, concrete startup/configuration
  requirements and the separate outstanding manual F-16 numerical acceptance.

Real synthetic end-to-end evidence: the
`test_real_directory_agent_delivers_and_reuses_simulink_evidence` test passed
with `.venv-matlab` on 2026-10-17 (98.18 s), retaining artifacts under
`runs/validation/directory-synthetic-20261017/`. Two actual worker runs cover
step then margins; changing the threshold and splitting reports after restoring
the Runtime does not add a third simulation. The planning model in this test
is a test double. The actual F-16 run stopped at `nlplant` compilation because
the C compiler/Xcode license is not configured; the source package remained
unchanged. These are software-delivery results, not F-16 engineering acceptance.

The full offline/browser suite passed with 262 tests and 14 MATLAB tests
deselected. Final review reproduced and fixed a worker dispatch failure that
left a durable run queued; its regression and the affected worker, Runtime,
Web and legacy worker suites passed together (52 tests). Type checking passed
for all 31 source files, lint passed, and both source and wheel distributions
were built under `runs/validation/package-20261017/`.

## Plan self-review

The tasks cover business F01-F13 and scenarios T01-T09: discovery and baseline
(1, 5, 6), actual analysis and assessment (1-3), history/resume (3, 5),
local evidence/report delivery (4, 6), feedback and permissions (3, 5, 6).
Backend isolation and future tool registration are implemented in Tasks 3/5;
future diagnosis/optimization/Simate execution are explicitly not implemented.
All code steps have a failing-test gate, and task interfaces use the contracts
introduced in Task 1.
