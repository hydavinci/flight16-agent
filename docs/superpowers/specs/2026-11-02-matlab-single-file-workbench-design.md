# MATLAB Single-File Workbench Design

## Context

The current `/workbench` route returns the same `agent.html` page as `/`.
This is intentional in the current code and tests, but it removed the earlier
MATLAB-specific workflow that uploaded a trusted `.m` file, executed it through
the MATLAB Worker, and displayed JSON results, logs, and figures.

Commit `be0929e` removed the complete fixed-upload subsystem, not only its UI:
the upload models, job store, Worker execution path, examples, browser assets,
and tests were deleted. Restoring the workbench therefore requires a dedicated
execution path; changing the route to an old HTML file would not restore a
working feature.

## Goals

- Restore `/workbench` as a distinct MATLAB single-file execution page.
- Accept one trusted UTF-8 `.m` file up to 1 MB.
- Preserve the original script/function contract:
  - scripts read `params` and must assign `result`;
  - functions are called as `result = functionName(params)`.
- Execute uploaded code only through the configured MATLAB Worker.
- Display bounded JSON results, MATLAB version, logs, and generated PNG figures.
- Persist Worker-side run status and expose recent runs.
- Preserve the current Agent homepage and generic planning/runtime behavior.

## Non-goals

- Do not restore the deleted fixed Agent analysis, rules, knowledge base, or
  demonstration assessment flow.
- Do not execute uploaded MATLAB code in the Web process.
- Do not fall back to a local MATLAB Engine when the Worker is unavailable.
- Do not support arbitrary MATLAB expressions, additional paths, multiple input
  files, multiple positional parameters, or configurable output expressions.
- Do not merge uploaded-file execution into conversation planning.

## Architecture

### Page and API separation

- `/` continues to serve the current Agent homepage.
- `/workbench` serves a dedicated `matlab-workbench.html`.
- Dedicated browser assets use explicit allowlisted names:
  `matlab-workbench.js` and `matlab-workbench.css`.
- Web-facing endpoints use the `/api/workbench` namespace.
- Worker-facing endpoints use the `/workbench` namespace.

This keeps the single-file execution surface independent from conversation
routes and prevents compatibility endpoints from becoming part of the generic
Agent contract.

### Components

1. **Workbench browser client**
   - Reads the selected `.m` file as UTF-8.
   - Validates basic input before submission.
   - Polls run status and renders results, logs, figures, and recent runs.

2. **Web workbench router**
   - Applies existing same-origin, host, API token, body-size, CSP, and
     no-store protections.
   - Validates the public request contract.
   - Proxies bounded requests and responses through `RemoteClient`.
   - Never evaluates or writes the MATLAB source locally for execution.

3. **Worker workbench router**
   - Uses the existing authenticated Worker application.
   - Stores requests and run state beneath a dedicated Worker run directory.
   - Uses the existing bounded background runner and single execution slot.
   - Exposes only manifest-listed result and figure artifacts.

4. **Single-file MATLAB executor**
   - Creates an isolated run directory.
   - Writes the validated source under its validated file name.
   - Generates a fixed wrapper from structured request fields.
   - Runs MATLAB with the same bounded subprocess/cleanup principles as the
     current local MATLAB execution path.
   - Validates the receipt before reporting success.

## Public contracts

### Submission request

The Web and Worker share a strict model containing:

- `id`: 32 lowercase hexadecimal characters;
- `filename`: a simple MATLAB identifier plus `.m`;
- `source`: UTF-8 text, at most 1,000,000 bytes;
- `kind`: `script` or `function`;
- `parameters`: a finite JSON object;
- `trusted`: literal `true`;
- `timeout_seconds`: integer from 1 through 600.

The request does not accept paths, commands, entry expressions, output
expressions, environment variables, or MATLAB startup options.

### Run record

A run record contains:

- request digest;
- status: `queued`, `running`, `completed`, `failed`, or `interrupted`;
- stage and timestamps;
- file name and kind;
- MATLAB version when available;
- finite bounded JSON result when completed;
- bounded sanitized log;
- explicit error text when unsuccessful;
- manifest-listed artifacts with path, digest, size, media type, and role.

The source is retained in the Worker request record for audit and recovery but
is not exposed through an artifact endpoint.

### Idempotency

Submitting an existing run ID with the same request digest returns the existing
record. Reusing an ID with different input returns HTTP 409. The Web client does
not automatically retry a submission whose outcome is uncertain.

## Execution flow

1. The browser obtains the existing page token from workbench bootstrap.
2. The user selects or loads a `.m` file, chooses script/function, enters JSON
   parameters and timeout, and confirms that the code is trusted.
3. The Web validates the request and forwards it to the configured Worker.
4. The Worker creates the run record before scheduling execution.
5. The executor writes the uploaded source and a fixed wrapper into the isolated
   run directory.
6. For a script, the wrapper defines `params`, executes the script, and requires
   a `result` variable. For a function, it calls the validated file stem with
   the single `params` object and captures the return value as `result`.
7. Figures created by this run are exported to bounded PNG artifacts. Preexisting
   figures are not attributed to the run.
8. The result is converted to finite bounded JSON. Unsupported MATLAB values,
   oversized output, missing `result`, execution errors, cleanup uncertainty,
   or artifact validation failures prevent successful completion.
9. The browser polls the run, then renders the result, figures, log, and status.

## Safety and failure handling

- Worker execution remains opt-in through `MATLAB_ALLOW_TRUSTED_PROJECTS=1`.
- The Worker token remains mandatory and is never embedded in page content.
- HTTP is allowed only on loopback; remote Workers require HTTPS or an SSH
  tunnel under the existing `RemoteClient` policy.
- File names must be simple MATLAB identifiers; traversal and nested paths are
  rejected.
- Source, JSON result, log, figure count, artifact size, response size, and
  execution duration are bounded.
- Worker busy is returned as HTTP 409 without queuing an unbounded backlog.
- Failed, timed-out, interrupted, or cleanup-uncertain execution never produces
  a successful run record.
- Artifact downloads verify the stored digest and remain constrained to the run
  directory.
- Errors are explicit; the Web does not silently fall back to another backend.

## User interface

The workbench uses a distinct page title and layout:

- header: `MATLAB 分析工作台`, Worker/Engine status, and a return-to-Agent link;
- input panel: file picker, examples, script/function selector, timeout, JSON
  parameters, trusted-code confirmation, and run button;
- output panel: run status, ID, MATLAB version, JSON result, JSON download,
  figures, and execution log;
- recent-runs panel: file name, time, and status.

Desktop uses input/output columns; narrow screens use one column. The page does
not include conversation controls or an Agent analysis panel.

Two general examples are restored:

- a waveform script using `params` and assigning `result`;
- a waveform function returning `result`.

Examples demonstrate execution and figure capture only; deleted rule and
knowledge demonstrations remain deleted.

## Persistence

Worker runs use a dedicated persistent store beneath the Worker root. The UI
shows a bounded recent list, while stored runs remain on the Worker filesystem
until an administrator removes them. Web restart does not lose Worker run status.
No second Web-side source-of-truth store is introduced.

## Testing

### Contract and routing tests

- `/workbench` differs from `/` and serves only its allowlisted assets.
- CSP, no-store, same-origin, host, token, and body-size protections apply.
- Invalid names, encodings, sizes, JSON, trust, and timeout values are rejected.
- Worker-only behavior reports configuration and connectivity failures.

### Worker and executor tests

- Script and function contracts execute through fake/controlled MATLAB seams.
- Missing result, invalid result, errors, timeout, output limits, log limits,
  figure limits, artifact mismatch, cleanup uncertainty, and busy state fail
  explicitly.
- Same-ID/same-request is idempotent; same-ID/different-request conflicts.
- Recent runs and completed records survive store recreation.

### Browser tests

- Desktop and mobile layouts submit a script and a function.
- Status polling reaches the correct terminal state.
- JSON result, MATLAB version, logs, figures, download, and recent history render.
- Failed runs display errors without success-shaped result content.
- The Agent homepage remains unchanged.

### Gates

- Targeted workbench tests.
- Full software selection on Python 3.11 and 3.12.
- Full Chromium selection.
- Ruff, mypy, build, and lock freshness.
- Licensed MATLAB acceptance remains opt-in and must cover a real script,
  function, and generated figure before release.

## Migration

- Replace the test that requires `/workbench` content to equal `/`.
- Add the dedicated page/assets and explicit static allowlist entries.
- Add Web proxy contracts/routes without changing conversation APIs.
- Add Worker contracts/routes/store/executor without restoring deleted fixed
  Agent modules.
- Restore only the two execution examples required by the workbench.
- Update usage documentation and startup instructions to state that the Worker
  is required for workbench execution.
