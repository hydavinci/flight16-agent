# Simulink Worker resources

[README](../README.md) · [Usage](usage.md) · [Architecture](architecture.md) ·
[Inputs and outputs](inputs-outputs.md) · [Offline diagnostics](offline-diagnostics.md)

There is one generic v2 Agent. A prepared Worker project is a registered
`simulink_project` resource on the Worker host, not a separate product or
scenario. There is no D1/D2 selection. Local `matlab_project` resources and
Agent-host diagnostic data use the same confirmation, execution, evidence,
evaluation and delivery machinery, with different resource contracts.
Old upload/demo execution and v1 history are unsupported.

## Deployment

Install this package and a MATLAB-compatible Python/Engine on the execution
machine. MATLAB, Simulink and the project's required toolboxes/native
dependencies must already be prepared.

On the Worker, set a private `MATLAB_WORKER_TOKEN` of at least 32 random ASCII
characters and `MATLAB_ALLOW_TRUSTED_PROJECTS=1`, then run:

```bash
flight-agent worker --port 9001
```

On the Web host, configure the same token, `MATLAB_WORKER_URL`, and a supported
model provider, then run:

```bash
flight-agent web --port 8000
```

Use an SSH tunnel or an approved HTTPS endpoint for remote Workers. The client
rejects non-loopback plain HTTP, redirects and automatic submission retries.
Run each service as one process against its own storage, without reload or
multiple Uvicorn workers. A busy service rejects new work rather than creating
an unbounded queue. Admission remains occupied until its Future completes.

Paths are on the resource's host: selecting a Worker directory does not upload
files. Output roots always belong to Agent/Web. Browsing or selecting a
directory neither confirms a baseline nor executes project code. Native macOS
selection requires an explicitly same-desktop topology, not merely a loopback
address supplied by an SSH tunnel.

## Prepared project contract

Discovery is read-only. The built-in F-16 profile recognizes a narrow prepared
layout; other projects supply `flight-agent.project.json` at their root.
The model, initializer, excitation, logging and analysis points must already
exist. This is not arbitrary Simulink import, repair or automatic signal
selection.

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

| Setting | Requirement |
|---|---|
| Initializer | Existing no-argument function, or registered script with `initializer_kind: "script"`; no interactive input |
| Signals | Two named scalar `timeseries` in `logsout`, time axes in seconds, reviewed units; no implicit conversion |
| Linearization | Exactly one active saved `openinput` and `openoutput`, read using `getlinio`, never guessed or edited |
| Feedback | `feedback_sign` multiplies the linearized loop before `allmargin`; verify the physical convention rather than guessing |
| Operating point | `initialized` uses `operpoint` after initialization; `workspace` requires a named prepared object |
| Trim | The current Worker capabilities accept only integer point 1; optional registered selector and selection-field checks remain adapter-specific |
| Dependencies | Safe relative files/directories, with optional relative working directory |
| Native modules | Registered `name/directory/source`; compilation is limited to declared trusted sources in the copy |
| Steady state | Optional bounded `steady_state` configuration, fingerprinted with the project |

Paths use `/`, without absolute paths, traversal or symlinks. Discovery
fingerprints bounded project contents and configuration, up to 5000 files and
512 MiB, ignoring generated caches such as `slprj`, `codegen`, `.slxc` and `.asv`.
Execution verifies the fingerprint and uses an isolated project copy. It does
not intentionally modify the source package. Trusted callbacks retain the
Worker account's privileges: copying is not an operating-system sandbox.

## Registered capabilities and numerical evidence

`simulink_step_response` and `simulink_stability_margins` bind the Worker project
resource explicitly and declare `execute_project`. Their adapter conventions
are not generic Agent restrictions or acceptance standards.

Step processing checks increasing time axes, aligned input/response, a single
step, nonzero response change, pre-step mean and verified steady-state tail.
The default tail is the last 20% of the post-step interval, at least 1 s,
relative range at most 2% and half-window drift at most 1%. `stepinfo` uses a
5% settling band measured from the detected step. These are validity/calculation
policies, not acceptance thresholds.

Margin analysis records all crossings and explicit infinite/undefined values.
Finite gain multipliers become dB before comparison. Missing or invalid
evidence cannot produce a passing check. Numerical success, validity and
meeting user criteria are separate outcomes; 0 dB/0 degrees alone does not
establish robustness or flight-envelope stability.

The generic evaluator requires sourced criteria with matching node, metric,
conditions and units. Analysis need not request evaluation or reports.
Requested delivery uses the generic JSON/Word adapter and verified artifacts,
not a mandatory fixed report sequence.

## Confirmation, recovery and delivery

Before execution the user confirms the current version, resources, capability
declarations, permissions and budget. A model claim of consent is insufficient.
New scope, resource fingerprints, thresholds, output roots or capability
declarations require a new unconfirmed baseline. Exact-spec numerical evidence
may be reused without overwriting prior tasks or deliveries.

Each Worker submission binds an original call ID and durable request digest.
Unknown execution reconciles that identity only; it is not redispatched.
Known failures need explicit retry with current confirmation. Closing a page,
client timeout or lost acknowledgement does not prove remote termination.
Receipts, dependencies and artifact hashes are checked at execution, reuse,
completion and recovery.

Web state is stored in `runs/verification/<conversation_id>/conversation.json`;
default task artifacts are under `tasks/<task_id>/`. A custom Agent-host output
root gets `flight-agent-<task_id>/`; source/output overlap is rejected.
Worker records and copies live under `runs/verification-worker/<run_id>/`.
Use artifact manifests, not guessed filenames, for authenticated downloads.

The supported Web API is documented in [Inputs and outputs](inputs-outputs.md).
Worker project transport remains:

| Route | Purpose |
|---|---|
| `POST /verification/projects/inspect` | Read-only discovery |
| `POST /verification/runs` | Idempotent, consented project submission |
| `GET /verification/runs/{id}` | Saved status and evidence |
| `GET /verification/runs/{id}/artifacts/{path}` | Allowlisted artifacts |

Worker routes require the private bearer token. Web uses its bootstrap token
and same-origin checks; neither is multi-user access control.

## Local MATLAB alternative

Use `--location matlab_project` for existing local MATLAB/Simulink projects.
No HTTP Worker is needed. The selected `MATLAB_PYTHON_EXECUTABLE` must have this
package and a compatible Engine. Readiness is an import probe, not license,
startup or model verification.

Local capabilities retain existing entry/model execution, independent
`stepinfo`, sourced linearization and `allmargin`. Working-point and feedback
sources, saved active SISO points and immutable producer artifacts are required.
Nodes do not share hidden workspaces, invent signals or repair models.
See [Usage](usage.md#运行本机工程) for preparation and confirmation.

Synthetic reference tests include an analytic second-order response
(overshoot about 9.478%, 5% settling about 2.6145 s) and a saturated nonlinear
loop (final 0.5, 5% settling `log(20)`). Independent tolerances are recorded in
`tests/test_local_matlab_integration.py`; low-level Worker step/margin
references remain in `tests/test_verification_matlab.py`. These fixtures prove
software composition, not arbitrary-project or aircraft qualification.

## Commands and unsupported history

Supported commands are `doctor`, `web`, `worker`,
`directory-task propose/confirm/status/resume`, and `cleanup-legacy`.
For example:

```bash
flight-agent directory-task propose --location simulink_project \
  --project-root /absolute/worker/project --request "Analyze the recorded step."
flight-agent directory-task confirm CONVERSATION_ID --version 1 --trusted-project
flight-agent directory-task status CONVERSATION_ID
flight-agent directory-task resume CONVERSATION_ID
```

There is no scenario/workflow/recovery-proposal dispatch or compatibility
projection of v1 tasks. `cleanup-legacy --project-root ...` defaults to dry-run;
only explicit `--apply` deletes positively identified v1 conversation
directories. v2, unknown records and external deliveries remain. Unresolvable
source roots or source roots inside a candidate directory block deletion.
See [cleanup safety](usage.md#历史清理).
