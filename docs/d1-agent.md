# D1 directory-based flight-control verification

## Scope

The user supplies a project **directory**, a natural-language verification request,
and confirmation of the evaluation baseline. The system discovers the model,
initialization entry and registered analysis configuration inside the directory.
It does not require individual `.m`, `.slx` or `.mat` uploads.

This design implements the supplied D1 case-preparation and business-workflow
documents. Their filenames are V0.3 and v0.5; their internal version labels are
V0.2 and V0.4 respectively. The business document defines the product; the case
document and the engineering package define the initial Simulink profile.

The initial case is the F-16 longitudinal pitch inner loop at trim point 1:

- Nonlinear step response: recorded input and response, `stepinfo`, overshoot
  <= 10%, settling time <= 3 s with a 5% settling band.
- Open-loop stability margins: registered linearization points, an initialized
  operating point, `linearize` and `allmargin`, gain margin >= 0 dB and phase
  margin >= 0 degrees.
- Either task or both, with user-confirmed thresholds rather than hardcoded
  compliance decisions.

Numerical analysis, input validation, permissions, confirmation, persistence and
threshold comparisons are deterministic. The LLM understands requests, proposes
plans, selects available capabilities, organizes reports and explains recorded
evidence. It cannot execute arbitrary MATLAB, change model/control parameters,
invent missing signals or override numerical results.

Root-cause diagnosis, optimization, automatic trimming, model/topology editing,
autonomous signal/analysis-point selection and a working Simate adapter are out
of scope. A Simate capability can be represented but must report unsupported
until an adapter is implemented.

## Architecture

```text
Directory + conversation
  -> local FastAPI Web / verification Runtime
  -> typed, registered verification tools
  -> authenticated MATLAB worker
  -> Simulink adapter in a dedicated subprocess
  -> isolated project copy + actual MATLAB/Simulink
  -> structured evidence / deterministic evaluation
  -> Word reports, PNG, MAT, JSON and conversation summary
```

The existing upload workbench remains available at `/workbench`. The new
directory-based conversation becomes `/`. Existing upload APIs and CLI demo
analysis remain compatible.

The engineering directory is on the **worker's filesystem**. With a local
worker this is the user's local directory. With a remote worker it must be a
worker-visible directory; submitting a path does not transfer files. Output
files on the Web host and numerical artifacts on the worker have explicit,
separate locations.

Paths may come from the form fields or labeled natural-language text, for
example `project: "/srv/models/F16 case"; output: "/Users/me/flight-results"`.
Chinese labels such as `工程目录` and `输出目录` are also supported. Quote paths
containing spaces. A single bare absolute path can identify the project;
multiple unlabeled paths, or conflicts between explicit fields and chat, require
clarification before discovery or an LLM call. Project paths can use POSIX,
Windows drive or UNC syntax appropriate to the worker. An output root must be
an absolute path usable on the **Agent/Web host**, not a remote worker path.
Later messages can select a different project or output root.

### Project contract

A project descriptor records a content fingerprint, backend, relative model and
initializer, dependencies, trim-selection method, signal metadata,
linearization/feedback convention and analysis policy. Backend-specific paths
stay here and in the adapter, not in the planner.

The supplied F-16 package is recognized by a narrowly scoped, built-in Simulink
profile. Other prepared projects use `flight-agent.project.json`. Discovery is
read-only and never executes a script or a model callback. Ambiguous or
incomplete projects produce a missing-configuration response, not a guessed
entry point.

The adapter snapshots the complete declared package outside its source
directory, verifies its fingerprint, initializes explicitly selected trim point
1 and runs only registered analyses. The source package is never intentionally
saved, compiled into or overwritten. Trusted model callbacks still execute with
the worker account's privileges: copying is **not** a security sandbox.

#### Preparing another Simulink project

Place `flight-agent.project.json` at the engineering directory root. This
minimal configuration matches the synthetic integration fixture; the named
model and initializer must already exist:

```json
{
  "schema_version": "1.0",
  "name": "Prepared longitudinal verification",
  "backend": "simulink",
  "model": "verification_fixture.slx",
  "initializer": "initialize.m",
  "initializer_kind": "function",
  "supported_trim_points": [1],
  "input_signal": {"name": "stick", "unit": "1"},
  "response_signal": {"name": "response", "unit": "1"},
  "operating_point_mode": "initialized",
  "feedback_sign": 1
}
```

The initializer is a no-argument function, or a registered script when
`initializer_kind` is `script`. It must prepare the model workspace/initial
conditions without interactive input. The model retains its existing
excitation, solver, simulation horizon and control parameters.

| Configuration | Preparation requirement |
|---|---|
| Signals | `logsout` must contain the two named scalar `timeseries`, with time axes in seconds. Units come from this reviewed configuration; no automatic conversion is performed. |
| Linearization | Save exactly one active `openinput` and one active `openoutput` with the model. The adapter uses `getlinio`; it never guesses or edits the points. |
| `feedback_sign` | Multiplies the `linearize` result before `allmargin`. Confirm the physical loop-break convention; do not automatically negate a negative-feedback model. The fixture and built-in F-16 profile use `1`. |
| Operating point | `initialized` uses `operpoint(model)` after initialization, without solving a new trim. `workspace` requires `operating_point_variable` naming a prepared workspace object. |
| Trim selection | Optional `trim_selector` names a registered `.m` function accepting the point index. Optional `trim_selection_variable` names a workspace struct whose `trim_selection_field` (default `PointIndex`) is checked before and after analysis. Without it, selection relies on the registered initializer. D1 only runs point 1. |
| Dependencies | `dependencies` lists required relative files/directories; their directories are added to the MATLAB path. Optional `working_directory` is relative to the project copy. |
| Native code | `native_modules` entries have `name`, relative `directory` and optional relative `source`. Missing platform MEX files are compiled only from registered trusted sources in the copy; missing compiler/license is an error. |
| Steady state | Optional `steady_state` overrides the bounded screening policy below; it is fingerprinted and is not a model-controlled tool argument. |

All resource paths use `/` separators relative to the engineering root, with no
absolute paths, traversal or symlinks. Discovery fingerprints the configuration
and bounded project contents, not just entry files: at most 5000 files and
512 MB. Generated caches such as `slprj`, `codegen`, `.slxc` and backup `.asv`
files are ignored. This is a prepared-project contract, not a general-purpose
Simulink import or automatic signal-discovery feature.

### Evaluation contract

Every numerical result carries its analysis kind, actual conditions, MATLAB
version, signal/linearization configuration, metrics, validity, diagnostics and
artifact references. NaN and infinity are represented explicitly, not as
nonstandard JSON numbers. All crossings remain in the evidence; finite gain
multipliers are converted to dB before comparison.

Step-response processing checks monotonic time axes, input/response alignment,
a single identifiable step, nonzero response change, pre-step initial value and
a conservative, recorded steady-state policy. The default policy checks the
last 20% of the post-step interval (at least 1 s), with relative range <= 2% and
half-window drift <= 1% of the response change. These are reproducible
validity-screening settings, not the numerical tolerances for final engineering
acceptance. They are recorded with the baseline and can be supplied by a
prepared project's configuration.

PASS requires all required metrics to be valid and satisfy the confirmed
criteria. FAIL requires all required metrics to be valid, with at least one
violation. Missing or unreliable required evidence takes precedence and yields
UNDETERMINED, while known individual failures remain visible. Execution failure remains
distinct from a valid result that fails its criteria. Passing 0 dB / 0 degrees
does not establish sufficient robustness or full-envelope nonlinear stability.

### Runtime and confirmation

Conversation, project reference, baseline version, plan nodes, tool results,
pending decisions and messages are persisted atomically. A user confirmation
must identify the current baseline; a model-generated claim of approval does
not authorize execution.

Normal execution uses one initial plan and a generic tool executor, without an
LLM request after every numerical step. Unsupported tools and tools declaring
mutation permissions are rejected. Answering a question does not complete a
verification task: the original task status, error and pending decision survive
explanations, clarifications and a restart during a follow-up turn. Standalone
clarification waits for user input; a question about an existing failed or
unconfirmed task preserves that task's lifecycle. No background LLM loop runs
while waiting.

An execution failure may trigger at most one automatic replanning attempt per
baseline. The planner receives the actual failed stage and bounded, redacted
error, not a generic retry prompt. It can ask an actionable clarification or
save a candidate plan as `failure_proposal`. A failed replanning call is recorded
separately as `replan_error`; the original error and resume/retry decision remain.
The user must explicitly adopt a candidate through `replan`, then separately
confirm the new baseline before any execution. Adoption validates the registered
tools, current project and D1 scope; it is not permission to run the proposal.

Each remote submission has a durable idempotency key. Re-entry reconciles the
same worker run instead of starting another simulation. Interrupted work is
explicit and requires user continuation. Valid completed numerical results can
be reused when only thresholds or report organization change, provided the
project fingerprint and analysis conditions are unchanged.

`resume` only polls the saved remote ID; it never repeats a POST to create a run.
After a known failed/interrupted worker run, `retry` requires a new explicit
confirmation of the current baseline and trusted project before allocating a
new ID. An unknown remote state must be reconciled first. Threshold and report
changes increment the baseline version, preserve previous deliveries and require
confirmation even when numerical results are reusable.

After a saved worker run has been verified as failed or interrupted, repaired
project contents can be rediscovered into a new, unconfirmed task. The old task
is archived only after a replacement plan has been constructed successfully.
Unchanged project evidence remains eligible for reuse; changed fingerprints
invalidate it. An unknown saved worker run cannot be replaced through chat,
project changes or replanning: only reconciliation of its original ID is
allowed until its state is known.

Simple combined tasks normally produce one report. Explicit requests can split
reports; significant organizational ambiguity prompts a persisted question.
Changing report grouping never reruns a completed simulation.

### Controlled tool extensions

Trusted application code can call `runtime.tools.register(ReadOnlyTool(...))`
before serving requests. The registry supplies the planner's tool catalog,
validates plan capabilities and dependencies, and validates each invocation's
input and output against its declared `Contract` schemas. Duplicate names,
invalid identifiers, non-read-only extensions and attempts to provide reserved
capabilities (`confirmed_baseline`, `numerical_result`, `evaluation`, `delivery`)
are rejected.

For example, with the application's existing `runtime`:

```python
from flight_agent.models import Contract
from flight_agent.verification.tools import ReadOnlyTool, ToolContext


class CountInput(Contract):
    pass


class CountOutput(Contract):
    analysis_count: int


def count_analyses(context: ToolContext, request: CountInput) -> CountOutput:
    if context.result is None:
        raise ValueError("Numerical evidence is required")
    return CountOutput(analysis_count=len(context.result.analyses))


runtime.tools.register(
    ReadOnlyTool(
        name="count_analyses",
        description="Count recorded numerical analyses without modifying them.",
        input_model=CountInput,
        output_model=CountOutput,
        operation=count_analyses,
        requires=frozenset({"numerical_result"}),
        provides=frozenset({"analysis_count"}),
    )
)
```

A plan using this extension can order its nodes as
`analyze_project -> count_analyses -> evaluate_results -> deliver_report`,
with explicit dependencies and empty input for `count_analyses`. Plans are
bounded to eight nodes, retain exactly one of each built-in tool, and place
every extension before evaluation and delivery. An extension cannot replace the
confirmed numerical analysis or deterministic evaluation.

`ToolContext` contains deep copies of the baseline, numerical result,
evaluation and completed ancestor-node outputs, not live task or project
handles. `read_artifact(name)` accepts only a recorded artifact name and verifies
its integrity before returning bytes. This is a controlled API for **trusted
local Python**, not a Python sandbox: a malicious registered callable could
still use the host account's privileges. The model cannot dynamically import
code or install/register plugins, and no HTTP plugin-registration endpoint is
provided. Register the same extensions during startup before resuming saved
plans; a missing registration fails closed after restart.

### Local delivery

Each task has a unique result directory with:

- One or more `.docx` verification reports.
- Applicable time-domain and Bode `.png` figures.
- Necessary raw/linearized `.mat` data.
- `evaluation.json`: baseline, criteria, results and evidence references.
- `execution_record.json`: plan versions, calls, state, decisions and errors.
- An artifact index, including report-to-analysis coverage.

An optional `output_root` selects a results root on the Agent/Web host. The
default remains the conversation's `tasks/<task-id>/` directory; a custom root
gets a unique child directory per task. The actual task directory is shown
before confirmation. Roots must be absolute, must not contain control
characters, traversal or symlinks, and must not overlap the source project.
These checks apply before planning to default roots and resets as well as
custom roots. A persisted unsafe task directory cannot be resumed.
Conversation records remain in the default store even when artifacts are
elsewhere.

Changing only the output root, including resetting it to the default, creates
a new task and an unconfirmed baseline. Applicable cached numerical evidence
is copied only after manifest and hash verification; the old directory,
deliveries and download links remain unchanged. Moving results does not
authorize a new simulation or bypass an unresolved worker run.

Files are validated before a task is marked delivered. Downloads are restricted
to the recorded artifact manifest. Source files, arbitrary paths and secrets
are never exposed as artifacts. LLM requests contain the user's task and
bounded semantic evidence, not project source code or full raw histories;
routing paths are redacted, including paths embedded in quoted prose, and
third-party tracing remains disabled.

### Deployment and persistence

Set the same private `MATLAB_WORKER_TOKEN` on Web and worker, and configure
`MATLAB_WORKER_URL` on Web. Enable `MATLAB_ALLOW_TRUSTED_PROJECTS=1` on the
worker. `MATLAB_ALLOW_TRUSTED_UPLOADS` is independent and can stay `0`.
Configure `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` and optionally
`LLM_TIMEOUT_SECONDS` on Web for a real tool-calling Chat Completions service.
There is no production scripted-model fallback.

Run each command in its own terminal from the repository root, using the
appropriate virtual environment:

```bash
flight-agent worker --port 9001
flight-agent web --port 8000
```

Open `http://127.0.0.1:8000`; use an SSH tunnel or an approved HTTPS endpoint
for a remote worker. Both services default to loopback. Each service must run
as one process against its own storage: no multiple Uvicorn workers or shared
storage instances. Directory and upload jobs share the same single execution
slot. A busy service rejects additional work instead of growing an unbounded
queue. `doctor` checks the existing CLI configuration, not full directory-model
or toolbox readiness.

| Host | Default path relative to its project root |
|---|---|
| Web | `runs/verification/<conversation-id>/conversation.json` |
| Web task | `runs/verification/<conversation-id>/tasks/<task-id>/` |
| Web task with custom output root | A unique task child under the Agent-host `output_root` |
| Web delivery | Task directory plus `delivery-v<baseline-version>/` |
| Worker | `runs/verification-worker/<run-id>/`, including the isolated `project/` copy |

Web downloads use locally verified artifact copies and remain available when
the worker is offline. Old baseline artifacts and archived tasks remain
addressable. Neither closing the page nor losing a connection cancels a worker
run. Restarted services mark unfinished work interrupted; no simulation is
silently replayed. These files may contain confidential models/results; use
appropriate account permissions and an explicit retention policy.

### HTTP interfaces

Web routes use the existing same-origin checks and `X-Flight-Token` obtained
from `/api/bootstrap`. Worker routes require the shared bearer token. Tokens
are not multi-user authentication, and the worker is not a sandbox.

| Route | Purpose |
|---|---|
| `GET /api/conversations` | Recent persisted conversations |
| `POST /api/conversations` | Start with `message`, optional `project_root` and optional `output_root` |
| `GET /api/conversations/{id}` | Baseline, pending decision, plan, calls, status and artifact manifest |
| `POST /api/conversations/{id}/messages` | Continue with `message`; optionally select a new `project_root` or `output_root` |
| `POST /api/conversations/{id}/confirm` | Confirm with `{"version": 1, "trusted": true}` using the current version |
| `POST /api/conversations/{id}/resume` | Reconcile/continue saved confirmed work, without a new remote submission |
| `POST /api/conversations/{id}/retry` | Explicitly retry a known terminal worker run; same confirmation body |
| `POST /api/conversations/{id}/replan` | Adopt the saved failure proposal with the current `{"version": 1, "trusted": true}`; a separate confirmation of the new baseline is still required |
| `GET /api/conversations/{id}/tasks/{task-id}/artifacts/{path}` | Download only a registered, hash-verified artifact |
| `POST /verification/projects/inspect` | Worker-side read-only discovery with `{"root": "/absolute/worker/project"}` |
| `POST /verification/runs` | Idempotent, consented registered-analysis submission |
| `GET /verification/runs/{id}` | Durable worker status and evidence metadata |
| `GET /verification/runs/{id}/artifacts/{path}` | Worker allowlisted numerical artifact |

For `output_root`, omission or JSON `null` preserves the current selection
(the default on a new conversation); `""` explicitly resets it to the default.
A nonempty value selects an absolute Agent-host root. A labeled output path
in `message` can also select it; conflicting field and message selections
require clarification. Clearing the browser's output field and sending a
message performs the explicit reset. The persisted conversation and task each
record their selected root; older records without the field retain the default.

Web mutation responses are `202` acknowledgements, not completion claims; poll
the conversation. Stale baseline/invalid state is rejected, not silently
confirmed. Storage/scheduling errors are explicit and background failures are
persisted for subsequent inspection. A follow-up scheduling or background
failure records a separate diagnostic while preserving the original task's
status, error and pending recovery decision. A worker dispatch failure returns `503`
and persists a terminal failed run rather than leaving it queued. Repeating
that run ID returns the failed record; it does not silently execute it again.

## Acceptance

Automated coverage includes single/combined tasks, equivalent intents through
typed model responses, threshold changes, context reuse, missing inputs, failed
tools, confirmation/resume, source changes, idempotency, report regrouping,
PASS/FAIL/UNDETERMINED, artifact integrity and browser behavior. Regression
coverage also exercises follow-up lifecycle preservation, known-failed repair
versus unknown-state blocking, explicit failure-plan adoption, typed read-only
tool extensions, chat/form path ambiguity, output-root migration/reset and
historical downloads.

Real MATLAB tests exercise synthetic Simulink projects with independently known
step/margin behavior. The supplied F-16 package is used for local integration
when its native dependencies are available. Its independent manual reference
results and agreed numerical tolerances are still required for final
flight-control numerical acceptance; workflow tests cannot substitute for them.

MATLAB and the LLM service are external prerequisites. Missing tools, licenses,
platform-compatible MEX binaries or model credentials produce actionable errors,
never fabricated successful results.

As of October 17, 2026, the real synthetic integration has passed on macOS with
MATLAB R2025b and Python 3.12. It covers directory discovery, explicit baseline
confirmation, a real authenticated HTTP worker, actual Simulink step/margin
analysis, DOCX with embedded figures, integrity-checked downloads, an additional
analysis without repeating the first, and threshold/report regrouping after
Runtime reconstruction. The planner is scripted only in this test. Retained
local evidence is under `runs/validation/directory-synthetic-20261017/`.

The supplied F-16 package reached native dependency preparation but failed
building `nlplant`: a supported C compiler is not configured and the local
Xcode license has not been accepted. The source fingerprint was unchanged.
The failed local run is
`runs/validation/7060e7bfac1a48e69b80a8fe0c498443/`. No license acceptance,
system compiler reconfiguration or substitute numerical success was performed.
A live LLM service is not configured in this validation environment. F-16
numerical acceptance and the real LLM joint run therefore remain outstanding,
separate from the completed synthetic software workflow.
