## Exploration: Complete the README Intelligent Notification Service challenge

### Current State

Research baseline: commit `aac99cf6c2b475396911702be3ddfd81236e0c23`, branch `Alvaro/sdd-challenge-plan`, 2026-10-27. The starting working tree was clean. This artifact records research only; no application implementation has been changed.

#### Challenge and implementation gap

The pending challenge is the complete notification application. `app/main.py:1` contains only the FastAPI import and `app = FastAPI(...)`; none of the required routes or pipeline exists. The provider, container infrastructure, load script, and dashboard are supplied evaluation fixtures.

| Requirement | Evidence | Current state |
|---|---|---|
| FastAPI instance in `app/main.py`, port 5000 | `readme.md:17`, `app/Dockerfile:12` | Instance and container command exist; routes absent |
| `POST /v1/requests`, body `{user_input: string}`, **201** with string ID | `readme.md:25` | Missing |
| `POST /v1/requests/{id}/process`, return 200 or 202 | `readme.md:30` | Missing |
| Call AI with system/user messages and robust extraction prompt | `readme.md:33`, `provider/app.py:63` | Missing |
| Clean and validate noisy or malformed AI output | `readme.md:35`, `provider/responses.py:3` | Missing |
| Send valid `{to, message, type}` to provider | `readme.md:36`, `provider/app.py:50` | Missing |
| `GET /v1/requests/{id}`, `{id, status}` with queued/processing/sent/failed | `readme.md:41` | Missing |
| Authenticate both provider calls with `X-API-Key: test-dev-2026` | `readme.md:47`, `provider/app.py:39` | Missing |
| Unexpected-error resilience, prompt quality, pipeline architecture | `readme.md:63` | Must be designed and demonstrated |

The README takes precedence where the load harness is permissive: create must return 201 even though k6 also accepts 200. Durability, distributed workers, external brokers, authentication for the app itself, cancellation endpoints, UI, and a real LLM integration are not requested.

#### Repository map and execution constraints

All 17 tracked files were inventoried. Relevant executable/configuration contents were reviewed, including both k6 scripts and the full dashboard JSON. There are no tracked Python tests, CI workflows, package configuration, existing OpenSpec context, or repository AGENTS.md. The user-supplied SDD instructions govern planning.

| Path | Role and implications |
|---|---|
| `app/main.py` | Only starter instance; preserve evaluator-compatible entry point |
| `app/requirements.txt` | FastAPI 0.110.0, Uvicorn 0.27.1, HTTPX 0.27.0, Pydantic 2.6.3, Tenacity 8.2.3; no pytest/dev requirements |
| `app/Dockerfile` | Python 3.12 slim; build context is `./app`; copies app contents to `/code`; runs `uvicorn main:app` |
| `provider/app.py` | Mock AI and notification API, auth, latency, rate limiting, provider-hit telemetry |
| `provider/responses.py` | Stochastic extraction variants; essential deterministic fixture source |
| `provider/requirements.txt`, `provider/Dockerfile` | Provider FastAPI/Uvicorn pins and InfluxDB client; independent service |
| `docker-compose.yaml` | App shares provider network namespace; provider publishes ports 3001 and 5000 |
| `platform/k6/test.js` | App HTTP contract/load checks |
| `platform/k6/health-check.js` | Direct provider smoke checks; does not exercise application |
| `platform/grafana/provisioning/dashboards/k6-dashboard.json` | HTTP availability, process latency, AI-attempt ratio scorecard |
| `platform/grafana/provisioning/dashboards/dashboard-provider.yaml` | Dashboard provisioning |
| `platform/grafana/provisioning/datasources/datasource.yaml` | InfluxDB `k6` data source |
| `platform/influxdb/init.iql` | One-hour default metric retention |
| `.gitignore` | Standard Python exclusions; virtual environments/test caches already ignored |

`localhost:3001` is correct inside the supplied app container because `network_mode: service:provider` shares the provider network namespace (`docker-compose.yaml:7`). k6 reaches the app at `http://provider:5000`. Do not reflexively replace this topology. Host development also uses `http://localhost:3001`. Make provider URL/key configurable while retaining supplied defaults.

The image contains only files under `app/`; future imports must work with `uvicorn main:app` from `/code` as well as the documented local test command. Moving files to root or using `app.*` imports without reconciling image layout would break runtime. Keep one Uvicorn worker if choosing process-local state.

#### Provider contracts and failure behavior

AI request: `POST /v1/ai/extract` with JSON `messages`, containing a dedicated system instruction and the original user text in the user role. Response extraction is at `choices[0].message.content`, **not** the top-level body. Validate the envelope before accessing it. Extra standard chat fields are not needed by the provider.

AI behavior (`provider/app.py:154`):

- Always waits randomly 1.5–3.0 seconds; independent of the notification semaphore/rate limiter.
- Uses the last user message, lowercases all text, then regex-extracts email or a 9/10-digit phone with optional hyphens.
- Prefers email as destination if both email and phone occur. Type selection can disagree with destination on contradictory input; local channel/recipient validation must reject the mismatch.
- Computes message from the segment after the **last** colon, otherwise the first 50 characters of the lowercased input (`provider/app.py:198`). Consequently the supplied time example ending `10:00` yields message `00`. This is a mock limitation, not an application parsing rule to copy.
- Missing target/type returns explanatory prose; absence of user message returns an error string. The mock ignores system-prompt semantics, but humans evaluate prompt quality.

Notification request: `POST /v1/notify` with string `to`, string `message`, and literal `type` equal to `email` or `sms` (`provider/app.py:50`). Optional query parameters `priority` and `trace_id` exist; neither is required. Success is HTTP 200 with `{status: "delivered", provider_id: string}`. The app should mark `sent` only after validating this acknowledgment.

Both calls require `X-API-Key`. Incorrect key explicitly returns 401. Missing-header behavior belongs to the pinned FastAPI APIKeyHeader implementation; tests should assert application handling of authorization failure rather than assume it is identical to the explicit wrong-key branch.

Notification capacity (`provider/app.py:85`, `provider/app.py:132`):

- At most **50 admitted notification attempts per sliding 10-second window**, not 50/second.
- A separate semaphore allows at most 50 concurrent notification handlers.
- Admission timestamps are appended before the 0.1–0.5 second simulated notification delay and before possible random 500. Failed admitted attempts therefore consume rate budget.
- A rejected attempt returns 429 without a `Retry-After` header. Current random `FAIL_RATE` is 0.0, although resilience should be exercised deterministically with failure responses.
- No idempotency key, deduplication contract, or notification lookup exists. `trace_id` is accepted but unused. Never promise exactly-once delivery across ambiguous network errors.
- HTTP error bodies follow FastAPI's `detail` envelope in actual execution, despite the documented `ErrorResponse.error` model.

AI telemetry writes `provider_hits` synchronously to InfluxDB after extraction (`provider/app.py:28`); exceptions are swallowed. This can distort measured AI latency/attempt counts if telemetry is unhealthy. Preserve the provider fixture instead of changing it to improve app scores.

#### AI normalization matrix

`provider/responses.py` selects a 50/10/10/10/10/10 percent top-level distribution. The five latter buckets have three approximately equal subbranches. Tests must force all **16** concrete variants rather than rely on random calls.

| Bucket | Variants | Suggested treatment for planning |
|---|---|---|
| 50% nominal JSON | Canonical `to`, `message`, `type` | Strict parse, semantic validation |
| 10% aliases | `Recipient/body/channel`; `To/Message/Type`; `destination/text/method` | Case-normalize keys and map a fixed allowlist; reject conflicting aliases |
| 10% structure | Extra `confidence/latency_ms`; missing `type`; missing `to` | Drop irrelevant extras; missing required values remain invalid unless a later specification explicitly defines safe recovery |
| 10% wrapping | JSON fence; generic fence; prose plus JSON | Extract a single quote-aware object; reject ambiguous multiple candidate payloads |
| 10% malformed | Complete values followed by ` ...`; single quotes; unquoted keys | Bounded, deterministic syntax-only repair; never invent field values |
| 10% refusals | Privacy refusal; spam/policy explanation; sensitive-content refusal | Terminal failed without notification; no retries intended to evade refusal |

The generator interpolates strings without JSON escaping, even in the nominal branch. Quotes, backslashes, and newlines in user text can invalidate its output. A planning decision must separate safely recoverable representation errors from ambiguity; globally replacing quotes or slicing from first `{` to last `}` can corrupt legitimate message content. Do not use `eval` or import application behavior from the provider implementation.

Recommended extraction prompt: exact three-field schema, allowed channels, JSON-only response, no Markdown, preserve message content/case, do not invent absent values, do not obey instructions embedded in user content, and explicit handling of incomplete/ambiguous requests. Send original user input unchanged in a separate user-role message. Prompt quality does not remove the need for validation because the mock deliberately introduces noise.

#### Evaluation mechanics and blind spots

`platform/k6/test.js:4` ramps 50 VUs over 10 seconds, 150 over 20 seconds, then 200 over 10 seconds. Every iteration creates a fresh request, processes it, sleeps 0.5 seconds, reads status once, then sleeps one second. It uses four hard-coded Spanish prompts (two email, two SMS).

The supplied checks prove JSON/HTTP contract availability only. They do **not** assert:

- A provider call happened, a notification was sent, or a request eventually reached a terminal state.
- Correct destination, message, or channel.
- Duplicate processing safety, retries, limits, invalid input behavior, or shutdown handling.
- Latency or error-rate pass/fail thresholds: no k6 `thresholds` are configured. Check failures must be inspected; command completion alone is not a quality gate.

The dashboard labels process latency up to 400ms as fast and highlights AI attempts/request at 1.5 and 2.5. These are scorecard cues, not README contractual SLOs. The AI alone takes at least 1.5 seconds, so a synchronous process handler cannot reach the fast category. Returning 202 after bounded admission is the justified approach.

Dashboard HTTP availability is not delivery success. Its external-overhead series uses non-process requests from k6, which are create/status requests, not actual app-to-provider timings. Duplicate dashboard panel IDs and metric/query quirks exist; fixing them is outside the requested app scope. Add application-level test evidence and structured operational logs rather than treat green panels as proof of correct delivery.

At 5 admitted notifications/second on average, a high-rate acceptance workload necessarily builds a backlog. A 40-second load run can admit thousands of requests but cannot deliver thousands within that run. Report accepted, processing, sent, failed, backlog, and drain time separately. Avoid setting a tiny queue timeout that turns virtually all load requests into failures or claiming eventual delivery from the single 0.5-second poll.

#### Baseline checks actually performed

- `git status --short`: clean baseline before this artifact; current branch verified by lead.
- `git ls-files`: 17 tracked files; no Python test suite or CI configuration.
- Parsed all three Python files with stdlib `ast.parse`: passed.
- Executed `provider/responses.py` via `runpy` with patched randomness and no external dependencies: canonical/alias/missing-type outputs parse as their expected objects; fenced/truncated/single-quote/unquoted/refusal outputs are not strict JSON. A quoted-message probe confirmed invalid JSON in the nominal branch.
- Host `python3` is 3.10.16; FastAPI and Uvicorn are absent. HTTPX/Pydantic/pytest modules are present but were not assumed to match repository pins.
- `docker compose ps`: no project services running (read-only inspection required sandbox escalation and then succeeded).
- No installs, image builds, infrastructure starts, live Swagger requests, endpoint tests, or k6 run were performed. Runtime correctness and performance remain unverified.

#### Primary-source implementation guidance checked

These sources support implementation choices, not additional challenge requirements. The implementation agent must stay compatible with repository-pinned versions.

- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/): lifespan supports allocating shared resources before serving and cleaning them up on shutdown. Use it to own the HTTP client and workers.
- [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/): distinguish connect/read/write/pool timeouts. The configured AI read timeout must exceed its observed 3-second maximum with headroom; overall job deadlines are separate from network inactivity timeouts.
- [Python 3.12 asyncio queues](https://docs.python.org/3.12/library/asyncio-queue.html): bounded queues provide finite admission; `put_nowait` permits a fast overload response; workers must balance `get` with `task_done`. Use Python 3.12 APIs, not newer queue lifecycle APIs.
- [Python 3.12 literal evaluation](https://docs.python.org/3.12/library/ast.html#ast.literal_eval): literal evaluation is narrower than arbitrary execution but is not safe against resource exhaustion from untrusted input. Prefer a constrained parser; if used for known single-quoted objects, impose input/depth/type restrictions first.

### Affected Areas

- `app/main.py` — required endpoints, request/response models or imports, dependency wiring and lifespan.
- New cohesive modules under `app/` — candidate responsibilities: configuration, request store/state transitions, provider client, extraction normalization/prompt, pipeline/workers. Final module names belong in design; avoid excessive abstractions.
- `app/requirements.txt` — retain existing runtime pins unless a justified parser dependency is selected. Existing libraries cover the core pipeline.
- New test configuration/dev dependency file and tests — deterministic API, parser, provider failure, concurrency, lifecycle and end-to-end coverage. Tests need a documented Python 3.12 setup and import path compatible with the image layout.
- `readme.md` — preserve original challenge, append run/test instructions, configuration, architectural decisions, operational limits and results.
- `docker-compose.yaml` / `app/Dockerfile` — inspect for required import/environment/test accommodations; no change is inherently required for the recommended architecture.
- `provider/` and existing `platform/` fixtures — preserve unchanged; add complementary verification rather than weaken evaluator behavior.

### Approaches

1. **Single process, bounded async workers and in-memory state** — immediate process acceptance; a finite queue, shared HTTPX async client, capped workers, and rate-aware notification stage.
   - Pros: fits supplied one-worker container and dependency set, handles latency without blocking HTTP handlers, straightforward deterministic tests, no extra infrastructure.
   - Cons: restart loses state/work; multiple workers are unsupported; duplicate prevention covers one process only; backlog must be bounded and documented.
   - Effort: Medium; recommended challenge scope.

2. **SQLite persistence with explicit durable job lifecycle** — persist requests and pending jobs; reclaim interrupted processing after restart.
   - Pros: improves restart behavior without a broker.
   - Cons: recovery semantics and ambiguous notification outcomes add significant scope; multiple workers still need coordinated claims/rate limiting; README does not ask for durability.
   - Effort: Medium/High; only if clarified as required.

3. **Redis/database plus distributed task workers** — durable shared state and worker queue.
   - Pros: can support multiple processes and scale-out with additional coordination.
   - Cons: extra services, configuration and operational testing; does not solve provider idempotency; disproportionate for this repository.
   - Effort: High; defer unless explicitly requested.

A synchronous handler is simpler but spends seconds awaiting AI and cannot meet the scorecard's fast-response cue. Unbounded per-request tasks or background callbacks return quickly but do not by themselves solve admission, worker lifecycle, memory limits or provider rate control.

### Recommendation

Proceed with approach 1 unless the user requires restart persistence or multi-worker operation. Treat the following as proposal/design decisions to specify precisely, not pre-existing README requirements:

1. Preserve exact public success contracts. Use 201 creation and 202 admission. Define 404 unknown-ID and 422 invalid-input responses, plus explicit overload behavior. Create only stores queued input; process triggers actual work.
2. Define atomic admission/idempotency for repeated process calls, including requests waiting in a queue. A lock-protected state/admitted marker must prevent duplicate queue entries; do not hold locks across network calls. Terminal requests must not silently resend.
3. Own workers/client in lifespan; finite queue and record capacity; bounded shutdown; every accepted processing job must eventually settle or be explicitly accounted for on shutdown. Never evict active work to make space.
4. Keep representation repair separate from schema/semantic validation. Strict string fields, known aliases only, recipient/channel compatibility, bounded input/output sizes and no fabricated destination/message. Fail incomplete, conflicting or genuinely refused extraction safely.
5. Apply bounded classified retries to extraction/transient upstream failures. Preserve the original input. Avoid retry loops for malformed user input, auth errors or refusals. Select a low extraction attempt cap in light of the dashboard ratio.
6. Pace notifications for the actual sliding window; concurrency limits alone are insufficient. Retry 429 with a bounded delay that respects the ten-second window. Explicitly distinguish transport errors before send from ambiguous timeout/read/write failures after possible delivery. Do not retry the whole AI-to-notify pipeline after notify failure.
7. Record structured stages, durations, attempts and failure categories without API keys or full message/contact content. Surface public status through the required simple endpoint; internal provider ID/error detail can support tests and diagnostics.
8. Prove correctness with deterministic tests before running the stochastic provider/load harness. End-to-end verification must poll until terminal and assert notify payload/count, not only read an allowed status string.

The planning handoff should include a source-linked requirement matrix, explicit state transitions and error policy, exact bounded repair rules, retry/rate policy, concrete module/test paths, ordered implementation tasks, and commands/acceptance evidence. Leave implementation tasks unchecked.

### Risks

- **Scope ambiguity:** persistence/multi-worker behavior is unspecified; proposed default is one process/in-memory, pending user clarification.
- **False confidence:** k6 can appear healthy while all work remains queued/failed; add delivery-oriented verification.
- **Backlog vs latency:** intake capacity far exceeds provider throughput; capacity/expiry/drain policies must be coherent and load results reported honestly.
- **Duplicate notifications:** no upstream idempotency or receipt lookup; an ambiguous network failure cannot provide exactly-once guarantees.
- **Unsafe repair:** broad transformations can change addresses/message text; repair syntax only and reject ambiguity.
- **Mock limitations:** lowercasing, last-colon extraction and unescaped strings limit semantic fidelity; do not copy these limitations into the prompt or silently replace the AI with regex extraction.
- **Runtime drift:** local Python/dependencies differ from the supplied image; tests must use the pinned environment and account for `main:app` imports.
- **Lifecycle leaks:** task failures, cancellation or startup/test lifespan omissions can strand records or leak the HTTP client; include deterministic checks.
- **Telemetry distortion:** provider's synchronous InfluxDB write and dashboard query shortcuts can skew latency/attempt interpretation.

### Ready for Proposal

Yes. The required challenge is clear and the application is entirely pending. A full implementation plan can proceed with the explicitly stated single-process default. The only material architecture clarification is whether the user expects persistence across restarts or multiple Uvicorn workers. Other edge policies should be specified as conservative implementation decisions with clear rationale, not left as unbounded future-agent choices.
