# D2 read-only, evidence-driven diagnosis

## Scope and deployment

D2 analyzes explicitly registered time-series and frequency-response JSON data
on the **Agent/Web host**. It does not run the MATLAB worker, execute project
code, import arbitrary files, generate new simulations or modify a controller.
D1 still accepts a prepared Simulink project on the **Worker host** and keeps
its existing fixed verification plan; see `docs/d1-agent.md`.

Both scenarios share conversation persistence, explicit baseline confirmation,
typed capability contracts, immutable analysis receipts and authenticated
artifact downloads. D2 adds a bounded policy loop:

```text
inspect registered data -> propose hypotheses, scope and initial analyses
  -> user confirms the current baseline
  -> verify and snapshot inputs -> run registered read-only analyses
  -> review cited evidence and update every confirmed hypothesis
  -> append the next plan within the confirmed scope, or stop explicitly
  -> persist diagnosis JSON, Word report and evidence index
```

A real tool-calling model service is required for planning and review. Configure
the existing `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` and optional
`LLM_TIMEOUT_SECONDS` on Web, then run `flight-agent web --port 8000`.
No MATLAB installation, Worker token or trusted-project Worker switch is needed
for these D2 adapters. There is no production scripted-model fallback.

Choose D2 in a **new** Web conversation. D1 remains the default, and an existing
conversation cannot switch scenarios. D2 project browsing lists Agent-host
directories; output browsing also uses that host. The optional native macOS
picker still requires a same-machine desktop, but D2 does not require a local
Worker. Browsing or selecting a folder never grants confirmation.

The Web process retains its existing single execution slot and local storage.
Do not run multiple processes against the same conversation store. Existing
Web authentication and same-origin rules apply; they are not multi-user access
control or an operating-system sandbox.

## Registered data format

Use `examples/diagnosis/synthetic-read-only/` for a software-only example. Its
`flight-diagnosis.json`, `series.json` and `loop.json` are explicitly synthetic.
For other data, place `flight-diagnosis.json` at the selected directory root:

```json
{
  "schema_version": "1.0",
  "name": "Reviewed diagnostic dataset",
  "design": {
    "controller": {
      "id": "controller",
      "version": "1",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    "plant": {
      "id": "plant",
      "version": "1",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  },
  "conditions": {"case": "reviewed-condition"},
  "synthetic": false,
  "datasets": [
    {
      "id": "response",
      "path": "response.json",
      "kind": "time_series",
      "source": "test",
      "parameters": {"recorded_gain": 1.0}
    }
  ]
}
```

The hashes above are placeholders. Replace them with reviewed design identities;
the manifest records controller/plant provenance, it does not independently
prove that a dataset came from those designs. An optional `design.candidate`
uses the same `{id, version, sha256}` contract. IDs use lowercase letters,
digits, `_` or `-`, start with a letter and have at most 64 characters.
Digests are 64 lowercase hexadecimal characters. Dataset source is
`simulation`, `test` or `synthetic`. Do not relabel synthetic evidence as a
physical test.

Each registered file uses one of these schemas:

| Kind | Required fields and constraints |
|---|---|
| `time_series` | `schema_version: "1.0"`, `kind: "time_series"`, `time_unit: "s"`, strictly increasing finite `time` array, and `signals` mapping names to `{unit, values}`; every finite value array matches the time axis |
| `frequency_response` | `schema_version: "1.0"`, `kind: "frequency_response"`, explicit `loop`, positive strictly increasing `frequency_hz`, matching finite `gain_db` and `phase_deg` arrays, `phase_convention: "unwrapped"`, `feedback_sign: -1` |

A minimal time-series file for the manifest above is:

```json
{
  "schema_version": "1.0",
  "kind": "time_series",
  "time_unit": "s",
  "time": [0.0, 1.0, 2.0],
  "signals": {
    "pitch": {"unit": "deg", "values": [0.0, 1.0, 1.1]}
  }
}
```

There is no implicit unit conversion, signal inference or phase unwrapping.
Files must use safe relative JSON paths without traversal or symlinks. Only
registered files are read; this is not a general engineering-directory import.
Limits are explicit, not silent truncation:

| Resource | Limit |
|---|---|
| Each JSON file | 8,000,000 bytes |
| Registered datasets | 16 |
| Samples per dataset | 2 to 20,000 |
| Signals per time-series dataset | 32 |
| Project name / unit | 200 / 80 characters |
| Recorded parameter map | 32 finite numeric entries, keys at most 64 characters |
| Conditions | Finite JSON, at most 16,000 canonical UTF-8 bytes and depth 8 |

Discovery fingerprints the manifest, design references, conditions and
registered data files. Confirmation rechecks the original project and the
registered capability versions. Execution uses independently copied, read-only
input files outside the source directory; snapshot metadata and hashes are
checked before further analysis. Source edits do not silently alter an approved
analysis. The source location itself is not the data identity.

Completed input files are staged before atomic, no-clobber publication. After
a process interruption, recovery removes an orphaned staging link only when
the snapshot's ownership record, contents and inode identity account for both
links. Unmarked or inconsistent hardlinks still fail closed; unrelated
leftovers are preserved. This does not guarantee power-loss durability.

## Built-in capabilities

All four adapters are registered as version `1`, with `read_only` effects and
strict input/output schemas. Times are seconds; frequency windows are Hz.

| Capability | Inputs | Recorded metrics |
|---|---|---|
| `window_statistics` | `dataset`, `signal`, `start`, `end` | `count`, `mean`, `minimum`, `maximum`, `rms`, `range` over the inclusive window |
| `compare_signals` | `observed_dataset`, `observed_signal`, `reference_dataset`, `reference_signal`, `start`, `end`, `alignment: "linear_overlap"`, `reference_time_offset` | `count`, `rmse`, `mean_error`, `max_abs_error`, and correlation when defined; units must match, and comparison never extrapolates |
| `parameter_sensitivity` | 2-16 unique `datasets`, `signal`, `parameter`, `start`, `end` | `count`, `gradient` and `r_squared` when defined, using recorded parameter values and window means |
| `local_loop` | `dataset`, `loop`, `start_hz`, `end_hz` | `phase_margin_deg`, `gain_margin_db`, `gain_crossover_hz`, `phase_crossover_hz` when identifiable from the recorded frequency response |

Signal comparison uses the explicitly requested reference time offset, not an
automatically optimized alignment. Parameter sensitivity is an association
across existing records; it does not perturb a model, hold confounders fixed or
establish causation. Local-loop analysis interpolates crossings in log
frequency, selects the minimum-absolute signed margins independently, and
retains explicit unknowns for missing or ambiguous crossings. A local margin
does not prove closed-loop or flight-envelope stability.

Outputs include validity, bounded metrics with units, observations, unknowns
and data fingerprints. Missing signals or unsupported analysis conditions yield
explicit invalid/unknown evidence, not invented metrics. Malformed data,
integrity failures and execution errors are reported separately.

## Confirmation, review and stopping

The proposed baseline records the objective, design and project identity,
up to eight hypotheses, an allowlist of capability names and versions, and a
resource budget. Each plan revision has up to four analyses, exact validated
inputs, the confirmed baseline version and its preceding evidence references.
Confirmation must identify the current version and explicitly set `trusted`.
For D2 this authorizes the recorded read-only scope, not project execution,
model editing, parameter changes or arbitrary Python.

After each completed round, the model must assess **every confirmed
hypothesis** as `supported`, `refuted` or `unknown`. Support/refutation requires
citations to recorded analysis IDs and, when supplied, real metric names.
Invalid evidence cannot support or refute a hypothesis. Unknown hypotheses
require explicit unknowns. New analyses must stay within the confirmed
capabilities and hypothesis set; a new broader task requires a new baseline.

The runtime verifies citation existence and output validity. It does **not**
independently prove that a model's causal explanation follows from the data.
Treat the explanation and its sufficiency judgment as reviewable hypotheses,
not deterministic flight-control acceptance.

| Budget field | Default | Allowed range |
|---|---|---|
| `max_rounds` | 6 | 1-20 |
| `max_calls` | 12 | 1-40 |
| `max_seconds` | 300 | 1-3600 |
| `max_no_progress` | 2 | 1-5 |

Budgets are checked at loop boundaries, not enforced as operating-system
resource quotas. The wall-clock budget starts with execution and includes
interruption time. The runtime stops with a persisted reason:
`evidence_sufficient`, `missing_input`, `unsupported`, `round_budget`,
`call_budget`, `time_budget`, `no_progress` or `unknown_execution`.
`evidence_sufficient` requires all hypotheses resolved and at least one
supported hypothesis; it is not a unique-root-cause or airworthiness finding.
Budget stops retain unresolved questions. A completed report may document
insufficient evidence; completion is not a diagnosis-success verdict.

## Persistence, cache and recovery

Conversations use the existing
`runs/verification/<conversation-id>/conversation.json` store. Each task has a
unique directory under that conversation, or under a user-selected Agent-host
`output_root`. Source/output overlap, traversal and symlinks are rejected.
The store rejects an oversized conversation before replacing its prior state.

For clarification follow-ups, the planner receives up to six recent messages
(at most 1,000 characters each) and the pending clarification context, with
local paths and credentials redacted. This preserves the original objective and
question across restarts. Conversation history does not authorize execution;
the scope and baseline still require explicit confirmation.

An immutable `AnalysisRun` records a unique ID, full `AnalysisSpec`, output
digest, input artifact hashes and timestamps. The spec includes the capability
version, design versions/digests, exact parameters, data fingerprints,
conditions and numerical settings. Only an exact matching, integrity-checked
spec can reuse a numerical result; a different window or design is another
branch, even when its capability name is unchanged. The same evidence kernel
serves D1; D1 evaluation and report caches remain separate from numerical data.

Receipts live in `numerical/analysis-runs/<run-id>.json`. A call is recorded as
pending before dispatch and its receipt is persisted before task acknowledgement.
On explicit resume, a matching valid receipt is recovered without calling the
adapter again. If an in-flight call has no receipt, execution is unknown:
the runtime records `unknown_execution` instead of silently redispatching.
This is a fail-closed recovery contract, not a general exactly-once execution
guarantee. Restarted work requires explicit continuation.

Deliveries under `delivery-v<baseline-version>/` include a DOCX diagnosis
report, `diagnosis.json`, `execution_record.json` and an artifact index.
They retain design/baseline identity, plan revisions, hypothesis assessments,
run/metric citations, unknowns, budget state and the termination reason.
Citation validation is repeated at delivery. Downloads use recorded paths and
verified hashes, not arbitrary filesystem paths. These local files are not a
cryptographically signed or externally witnessed audit log.

Model requests contain bounded metadata and metrics, not full raw arrays or
project source. Path/credential-shaped text is redacted recursively, including
baseline and review fields. This is not a general data-loss-prevention system;
do not submit secrets or material unapproved for the configured model service.

## API and CLI

Use the existing Web bootstrap token (`X-Flight-Token`) and same-origin policy:

| Request | D2 behavior |
|---|---|
| `POST /api/conversations` | `{"scenario":"d2","message":"...","project_root":"/absolute/agent/data"}` proposes a baseline; optional `output_root` selects the Agent-host results root |
| `POST /api/conversations/{id}/messages` | Follow-up, clarification or a new proposal in the same scenario; questions preserve an existing task's lifecycle |
| `POST /api/conversations/{id}/confirm` | `{"version":1,"trusted":true}` confirms the current baseline |
| `POST /api/conversations/{id}/resume` | Continue saved work with receipt reconciliation, not a duplicate unknown dispatch |
| `GET /api/conversations/{id}` | Read durable baseline, plans, analysis runs, reviews, unknowns, termination and artifacts |
| `GET /api/conversations/{id}/tasks/{task-id}/artifacts/{path}` | Download a registered, integrity-checked artifact |

Mutation acknowledgements are not completion responses; poll the conversation.
The existing D1 `retry` and failure-proposal `replan` controls reject D2.
Omitting `output_root` preserves the current choice, and `""` resets it to the
default. D2 never falls back to a Worker or silently switches host.

The CLI talks to the already-running local Web API, so it uses the same state
owner as the browser:

```bash
flight-agent directory-task --server http://127.0.0.1:8000 --timeout 360 propose \
  --scenario d2 --project-root "$PWD/examples/diagnosis/synthetic-read-only" \
  --request "Inspect the registered data, propose testable hypotheses and state unknowns."

flight-agent directory-task confirm CONVERSATION_ID --version 1 --trusted-project
flight-agent directory-task status CONVERSATION_ID
flight-agent directory-task resume CONVERSATION_ID
flight-agent directory-task propose --conversation CONVERSATION_ID \
  --request "Explain the cited evidence and remaining limitations."
```

Replace the ID and version with the proposed values and inspect the plan before
confirming. `--server` and `--timeout` precede the action; defaults are
`http://127.0.0.1:8000` and 360 seconds. Only local HTTP origins at `127.0.0.1`
or `localhost` are accepted. The client obtains the bootstrap token, does not
follow redirects or environment proxies, and does not load `.env` from the
data directory. A client timeout does not cancel the server-side task.
Output is the persisted conversation JSON. Exit code 0 does not establish
diagnostic sufficiency; inspect the termination and unknowns.

## Extension and acceptance boundaries

The shared kernel distinguishes read-only analysis, trusted project execution,
delivery writes, isolated candidate writes and separately approved candidate
adoption. A typed capability validates its input and output and must pass the
confirmed effect scope. These are trusted-code contracts, not a sandbox for a
malicious local plugin. D3-D6 have design/effect extension seams only: no
optimization algorithms, topology representation, automatic design changes or
adoption UI is implemented.

Automated synthetic cases cover known calculations, multiple successful
evidence-driven rounds, exact-spec reuse, real run/metric references, explicit
unknowns, budget stops, snapshot integrity and receipt-based recovery. They
establish software behavior, not real fault attribution. Real labeled D2 data
and engineering-reviewed diagnostic criteria are still needed. D1 likewise
still needs independent numerical references and agreed tolerances; D2 does not
replace those acceptance requirements.
