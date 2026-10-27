# Tasks: Complete the Intelligent Notification Service challenge

## Handoff and scope

This is the implementation entry point for change `complete-readme-challenge` on branch `Alvaro/sdd-challenge-plan`. **All tasks below are pending.** This planning pass changed documentation only; a checked planning artifact does not mean the application is implemented or tested.

Read in order: the repository `readme.md`, [proposal](proposal.md), [exploration](exploration.md), [request lifecycle specification](specs/request-lifecycle/spec.md), [AI extraction specification](specs/ai-extraction/spec.md), [delivery specification](specs/notification-delivery/spec.md), then [technical design](design.md). The README defines challenge obligations; specifications explicitly distinguish those obligations from chosen policies. The design owns numerical defaults and file/interface choices.

Assumptions: one Python 3.12 application process; in-memory state; no restart recovery; bounded queue and workers; no new runtime or test dependencies. Persistence/multiple workers were offered for clarification and remain a documented default, not a confirmed additional user requirement. If that scope changes, revise store, admission/recovery, and shared rate limiting together before coding.

Preserve `provider/`, existing `platform/` fixtures, `app/Dockerfile`, `docker-compose.yaml`, and pinned runtime dependencies. Preserve Docker's flat `/code` module layout and `uvicorn main:app`; local tests use `PYTHONPATH=app`. Do not implement application-side extraction from user text to bypass the AI or change the mock to improve scores.

## Phase 1: Foundation and test harness

- [ ] 1.1 Establish the intended runtime from `app/Dockerfile` and `app/requirements.txt`: use a Python 3.12 virtual environment or the built app image, install only the pinned requirements, and record interpreter/dependency versions. Verify a module-level `main.app` can be imported without opening clients or spawning tasks. **Acceptance:** environment/import evidence matches the container entry point (API-1, API-6).
- [ ] 1.2 Create `app/settings.py` with the design's environment names, defaults, parser limits, and startup validation. Allow direct test overrides, reject invalid capacities/timeouts, and retain challenge-compatible provider URL/key defaults. **Acceptance:** focused cases in `app/tests/test_lifecycle.py` prove invalid settings fail startup and valid overrides do not leak between instances (API-5, API-6, AI-1).
- [ ] 1.3 Create `app/models.py` for strict creation/notification schemas, four public statuses, immutable snapshots, record metadata, and internal failure categories. Preserve original accepted input/message strings, validate without coercion, and keep internal fields out of public output. **Acceptance:** boundary/type cases for API-2 and AI-4 are explicit; no credentials or raw text in public errors.
- [ ] 1.4 Establish `unittest` discovery under `app/tests/` with `IsolatedAsyncioTestCase`, deterministic HTTPX transports, explicit lifespan entry, and injected time/event controls. Keep small helpers local to tests unless reuse justifies a helper module. **Acceptance:** the documented discovery command runs a meaningful initial model/settings test; no live provider, InfluxDB, randomness, or real ten-second sleep is needed.

## Phase 2: Request storage and admission

- [ ] 2.1 Create `app/store.py` for unique creation, snapshots, lookup, and conditional state transitions. Store original input; maintain admission and terminal metadata; never expose mutable records. **Acceptance:** `app/tests/test_api.py` covers separate IDs for equal input, initial queued state, unknown IDs, and immutable terminal outcomes (API-1, API-2, API-4).
- [ ] 2.2 Implement lock-protected admission in `app/store.py` using nonblocking queue insertion. Check terminal/already-admitted records before queue saturation; make rejection leave the record unadmitted; never hold the store lock across I/O or delay. **Acceptance:** concurrent duplicate, full-queue, and later-retry tests admit one execution or no execution as appropriate (API-3, API-5).
- [ ] 2.3 Implement record capacity, oldest-terminal eviction, unadmitted expiry, and admitted queued deadline handling in `app/store.py`. Never evict active work; queued expired IDs must be safe to dequeue and skip. **Acceptance:** tiny-capacity and fake-time cases in `app/tests/test_lifecycle.py` cover pressure, expiry, concurrent creation/cleanup, and expected 404 after eviction (API-4, API-5, API-6).
- [ ] 2.4 Complete the store concurrency tests in `app/tests/test_api.py` and `app/tests/test_lifecycle.py`: competing admission calls, completion versus expiry, and late completion after terminal failure. **Acceptance:** state never regresses, terminal results are not overwritten, and rejected admissions consume no slot (API-3 through API-6).

## Phase 3: Extraction and provider contracts

- [ ] 3.1 Create `app/extraction.py` with the dedicated system prompt and typed extraction outcomes. Include the exact schema, supported channels, preservation/nonfabrication rules, user-as-data boundary, positive examples, and explicit inability behavior. **Acceptance:** `app/tests/test_provider_client.py` later asserts separate system/user messages and unchanged original input, including mixed case and instruction-like text (AI-1).
- [ ] 3.2 Implement bounded candidate scanning and strict JSON decoding in `app/extraction.py`. Handle BOM/outer whitespace and supported wrappers; enforce size/depth limits, reject duplicate keys and multiple candidate objects, and preserve quoted punctuation. **Acceptance:** focused cases in `app/tests/test_extraction.py` cover wrappers, embedded braces/quotes, oversized/deep input, duplicate keys, and ambiguous candidates (AI-2, AI-3).
- [ ] 3.3 Implement only the design's constrained flat-object repairs in `app/extraction.py`: unambiguous single-quoted strings, unquoted known keys, and the exact complete-values-plus-ellipsis truncation. Separate syntax failure from terminal semantic/refusal failure. **Acceptance:** no evaluation, global quote replacement, invented fields, or repair of unfinished/ambiguous string values; all three recoverable malformed fixtures pass (AI-3, AI-5).
- [ ] 3.4 Add alias canonicalization and semantic validation in `app/extraction.py` using `app/models.py`: fixed case-insensitive aliases, duplicate canonical-field rejection, strict strings, allowed type, bounded nonblank message, email/SMS compatibility, and exactly three outgoing fields. **Acceptance:** table-driven tests preserve message content and reject missing fields, conflicts, invalid recipients/types, and refusals without notification (AI-3, AI-4, AI-5).
- [ ] 3.5 Complete `app/tests/test_extraction.py` with a deterministic case for each of the 16 fixture variants, plus independent escaping/Unicode/control-character/type/length/expression cases. Derive literal examples from fixture behavior; do not import provider code into the application or rely on probabilistic generation. **Acceptance:** each expected accept/reject result maps to AI-3's matrix, and rejected semantic/refusal cases are distinguishable from one-retry syntax cases.
- [ ] 3.6 Create `app/provider_client.py` using the lifespan-owned HTTPX client: exact endpoint/header/body contracts, bounded streamed responses, strict AI envelope and delivered receipt validation, explicit timeouts, disabled redirects/transport retries, and typed errors. Add `app/tests/test_provider_client.py` for success, auth/status failures, malformed/oversized bodies, and pre-send versus uncertain transmission errors. **Acceptance:** AI-1, AI-2 and DEL-1 contracts pass; no malformed acknowledgment can count as delivery.

## Phase 4: Pipeline, rate control and lifecycle

- [ ] 4.1 Create `app/pipeline.py` queue consumption and per-job execution using store transitions, extraction, and provider client. Enforce the absolute admission deadline across queue wait and every subsequent operation; balance every queue `get` with `task_done`, including skipped tombstones. **Acceptance:** scripted healthy, expired, and unexpected-error jobs reach truthful terminal states without stranding slots or killing healthy work (API-4, API-6).
- [ ] 4.2 Implement the single shared AI-attempt budget in `app/pipeline.py`: at most two actual calls, classified retry eligibility, bounded backoff/jitter, unchanged user input, and terminal refusal/semantic errors. Add corresponding `app/tests/test_pipeline.py` cases. **Acceptance:** transport and parsing retries share one cap, no nested retries exceed it, and all no-retry cases stop after one attempt (AI-5).
- [ ] 4.3 Implement `NotificationGate` in `app/pipeline.py` with injected monotonic time/sleep, locked reservations, rolling window, spacing, and shared 429 cooldown. Retain reservations after failures; handle valid longer retry guidance within the remaining job deadline. **Acceptance:** fake-time concurrent tests prove every sliding ten-second interval has at most 50 local dispatches, retries consume capacity, all workers honor cooldown, and waiting does not block routes (DEL-2).
- [ ] 4.4 Implement notification delivery and retry classification in `app/pipeline.py`, retaining the validated payload: at most three attempts, safe resend only for definitive rejection/pre-send failure, no AI restart, and no resend after uncertain delivery. **Acceptance:** scripted 429-then-success, pre-send exhaustion, 401/validation errors, malformed receipts, 5xx and read/write/protocol failures prove DEL-1 and DEL-3; attempt counts and failure categories remain accurate.
- [ ] 4.5 Implement startup/stop supervision in `app/pipeline.py`: fixed workers and expiry sweep, bounded graceful drain, cancellation/accounting, pending queue cleanup, and explicit handling of a fatal worker exit. **Acceptance:** `app/tests/test_lifecycle.py` holds upstream calls with barriers, triggers shutdown, and proves admission closes, workers terminate, all queued items are accounted for, and the client closes last (API-6).
- [ ] 4.6 Add structured, sanitized stage/outcome logging in `app/pipeline.py` and `app/provider_client.py`, including attempts, elapsed times, queue/state counts and drain summaries. **Acceptance:** captured logs from success and failure cases contain useful categories but no original input, destination, message, provider body, API key, or unrestricted exception text (DEL-4).

## Phase 5: Public API and deterministic end-to-end verification

- [ ] 5.1 Update `app/main.py` with `create_app(...)`, module-level `app`, and lifespan wiring for settings/store/queue/client/workers. Expose only the three challenge routes with the design's exact success, duplicate, unknown-ID, overload, and shutdown responses. Sanitize request-validation errors instead of returning sensitive input through default exception details. **Acceptance:** `app/tests/test_api.py` covers API-1 through API-5 and importing the module does not start resources (API-6).
- [ ] 5.2 Complete `app/tests/test_api.py` against actual ASGI routes, including invalid JSON/body matrices, exact public shapes, create-without-processing, held-AI asynchronous acceptance, concurrent duplicates, terminal no-resend, and saturated capacity. **Acceptance:** process returns before upstream release, no invalid request calls providers, and every specified HTTP/state outcome is asserted (API-1 through API-5).
- [ ] 5.3 Create `app/tests/test_end_to_end.py` using real routes and a scripted upstream transport. Poll to a bounded terminal outcome, assert exact notify payload/count, and test healthy email/SMS, refused/invalid extraction, safe notify retry, ambiguous notify failure, and concurrent repeated process calls. **Acceptance:** terminal delivery evidence covers API-4, AI-1 through AI-5, and DEL-1 through DEL-4; allowed status strings alone are insufficient.
- [ ] 5.4 Run the full deterministic suite in Python 3.12 with repository pins; resolve failures and repeat only affected checks, then one final full run. Verify isolated app instances and no leaked workers/client resources. **Acceptance:** all scenarios in the coverage table below have passing test evidence; report interpreter, command, test count, and any limitations without implying the live provider was tested.

## Phase 6: Live verification and documentation

- [ ] 6.1 Create `app/smoke.py` as the design's explicitly invoked small live probe: create/process/poll every ID, bounded timeout, contract checks, and terminal counts/durations. **Acceptance:** HTTP contract failures or unresolved deadlines return failure; legitimate stochastic terminal failures are reported explicitly rather than concealed or interpreted as guaranteed delivery. Exercise the script's timeout/reporting path against a controlled service (DEL-4).
- [ ] 6.2 Build/start the unchanged Compose topology and run `app/smoke.py` against `localhost:5000`. Verify startup/imports, authenticated integration, statuses and terminal results. **Acceptance:** record the commands and actual observations in appended `readme.md` implementation notes; investigate unexpected failures rather than modifying provider fixtures (API-1, AI-1, DEL-1, DEL-4).
- [ ] 6.3 Run the supplied `load-test` after the small smoke run has settled. Inspect k6 check counts, application logs and dashboard measurements, then observe/report backlog and drain separately. **Acceptance:** record acceptance/HTTP errors, process latency, AI attempt ratio, terminal state counts and outstanding work; do not call a green exit or <=400ms latency proof of notification delivery (DEL-2, DEL-4).
- [ ] 6.4 Append setup/test commands, configuration, chosen errors/state semantics, throughput/backlog limits, safe retry/uncertain-delivery caveat, one-process restart loss, provider extraction limitations, and measured evidence to `readme.md` without replacing the challenge. Run whitespace/scope checks and update task checkboxes only for completed verified work. **Acceptance:** implementation handoff is reproducible, fixtures remain unchanged, and any deviation or unverified check is explicit.

## Dependency and coverage map

Execute phases in order. Within phase 3 the pure normalizer and HTTP client can be built independently once models/settings exist; both must be ready before pipeline integration. Rate control must be implemented before any live notification load. Phase 5 wires and verifies the complete application; do not mistake component tests for route completion.

| Requirement | Main implementation tasks | Required evidence |
|---|---|---|
| API-1 | 1.1, 1.3, 2.1, 5.1 | Creation/status/import contracts in 5.2; live startup in 6.2 |
| API-2 | 1.3, 2.1, 5.1 | Invalid/unknown-ID and sanitized422 matrix in 5.2 |
| API-3 | 2.2, 4.1, 5.1 | Admission races in 2.4; held-AI and duplicate calls in 5.2–5.3 |
| API-4 | 2.1, 2.3, 4.1, 4.4 | Monotonic transitions in 2.4; terminal evidence in 5.3 |
| API-5 | 1.2, 2.2–2.3 | Capacity/retention and race cases in 2.4, 5.2 |
| API-6 | 1.2, 2.3, 4.1, 4.5, 5.1 | Deadline/error isolation/shutdown and resource-leak checks in 4.1, 4.5, 5.4 |
| AI-1 | 1.2, 3.1, 3.6 | Auth/prompt/original input in 3.6, 5.3 |
| AI-2 | 3.2, 3.6 | Envelope/body/content bounds in 3.6 |
| AI-3 | 3.2–3.5 | All sixteen variants plus ambiguity/escaping cases in 3.5 |
| AI-4 | 1.3, 3.4 | Channel/recipient/type/message matrix in 3.5 |
| AI-5 | 3.3–3.4, 4.2 | Shared attempt budget, refusal/no-retry cases in 4.2, 5.3 |
| DEL-1 | 3.6, 4.4 | Valid/invalid receipt and exact payload in 3.6, 5.3 |
| DEL-2 | 4.3–4.4 | Fake-time sliding windows, shared cooldown, longer retry guidance and failures in 4.3 |
| DEL-3 | 4.4 | Safe/unsafe retry matrix and no AI redo in 4.4, 5.3 |
| DEL-4 | 4.6, 6.1, 6.3–6.4 | Captured sanitized logs, terminal E2E, honest live/load results |

## Commands for the implementation agent

Run from repository root. These are **future commands**, not checks completed by the planning pass. Docker must be available for live verification; Python 3.12 is needed for the host test path. Installing dependencies or starting services may require environment approval.

```sh
git branch --show-current
git status --short
python3.12 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
PYTHONPATH=app .venv/bin/python -m unittest discover -s app/tests -v
docker compose up -d provider influxdb grafana
docker compose up -d --build app
.venv/bin/python app/smoke.py --base-url http://localhost:5000 --timeout 120
docker compose run --rm load-test
docker compose logs app
git diff --check
git diff --name-only
```

If host Python 3.12 is unavailable, start the provider namespace, build the app image, then run its standard-library tests with:

```sh
docker compose run --rm --no-deps app python -m unittest discover -s tests -v
```

The suite itself uses fake upstreams and needs no live provider calls; the existing Compose network namespace still requires the provider container for this command. A host virtual environment with mismatched Python/dependencies is not equivalent evidence. After the suite passes, use the documented image/host path for the live smoke probe.

Baseline research completed only source/AST inspection and deterministic mock-output probes; no application endpoint, integration or load test has passed yet. See [exploration](exploration.md) for the actual evidence and environment limits.

## Next-agent launch prompt

> Implement SDD change `complete-readme-challenge` on branch `Alvaro/sdd-challenge-plan` using the `sdd-apply` workflow. Start with `openspec/changes/complete-readme-challenge/tasks.md` and its linked proposal/specs/design. Follow the task order and mark only completed verified tasks. Preserve the README API, provider/evaluator fixtures, pinned dependencies, Docker `main:app` imports, and one-process in-memory scope. Use deterministic tests before live Compose/k6 checks; report terminal delivery separately from availability. Do not guess missing AI fields or resend uncertain notifications. Record actual verification and remaining limitations. After implementation, hand off to `sdd-verify`; do not archive an unverified change.
