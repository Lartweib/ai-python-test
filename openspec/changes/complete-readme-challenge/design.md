# Design: Complete the Intelligent Notification Service challenge

## Technical Approach

Implement the three README routes around one process-local request store and one bounded asynchronous queue. Thirty-two lifespan-owned workers each run the complete extraction → normalization → notification pipeline using one shared HTTPX client. A shared gate regulates notification attempts independently of AI concurrency. All constants below are explicit challenge-sized design choices, not additional README requirements.

The source baseline is documented in [exploration.md](exploration.md). Relevant contracts were checked directly in `readme.md`, `app/main.py`, `app/requirements.txt`, `app/Dockerfile`, `docker-compose.yaml`, `provider/app.py`, and `provider/responses.py`. There is no existing application architecture or test suite to extend. Behavioral requirements are in [request lifecycle](specs/request-lifecycle/spec.md) (API-1–6), [extraction](specs/ai-extraction/spec.md) (AI-1–5), and [delivery](specs/notification-delivery/spec.md) (DEL-1–4).

## Architecture Decisions

### Decision: One process, one queue, full-pipeline workers

**Choice:** A dictionary-backed store, `asyncio.Queue`, and 32 workers. Creation records intent; only the process endpoint admits execution. Keep the supplied one-worker `uvicorn main:app` entry point and Compose network namespace.

**Alternatives considered:** Synchronous processing; unbounded request background tasks; separate extraction and notification queues; database/broker workers.

**Rationale:** AI takes 1.5–3 seconds, so synchronous handling misses the dashboard's fast-response cue. A fixed worker pool bounds tasks and upstream concurrency. Workers waiting for notification capacity intentionally apply backpressure to extraction. Two queues would improve stage independence but add storage, cancellation and ownership complexity without increasing the provider's five-per-second ceiling. Durability and multiple workers remain outside the assumed scope.

### Decision: Atomic admission and immutable terminal outcomes

**Choice:** One short-lived `asyncio.Lock` protects store changes and admission. Do not await network, delay, or queue capacity while holding it. Use `put_nowait` within the same critical section as marking admission. Terminal records cannot be processed again.

**Alternatives considered:** Marking processing before an awaited enqueue; duplicate background callbacks; restarting failed requests automatically.

**Rationale:** Concurrent process calls must not enqueue the same ID twice or leave a record admitted after overload. Separate admission from public `queued` status so waiting jobs remain understandable without adding a fifth public state. A caller wanting a new attempt creates a new request explicitly.

### Decision: Bounded normalization, conservative notification retries

**Choice:** Support the evidenced syntax variants using a small allowlisted normalizer; preserve values and reject ambiguous repairs. Retry extraction at most once. Retry notification only for a known rejection or failure before transmission, with three total attempts.

**Alternatives considered:** Broad JSON repair libraries; replacing quotes globally; recovering missing fields from user text; retrying every provider failure.

**Rationale:** Existing dependencies suffice, while blind repair changes user meaning and whole-pipeline retries can duplicate notifications. The provider has no idempotency contract. Even though the mock's injected 500 occurs before its return, conservatively treat notification 5xx as an uncertain outcome rather than generalize that implementation detail into an external-service guarantee.

### Decision: No new runtime or test dependency

**Choice:** Use pinned FastAPI/Pydantic/HTTPX plus Python 3.12 standard library. Use `unittest.IsolatedAsyncioTestCase`, HTTPX mock/ASGI transports, and injected clock/sleep dependencies for tests. Leave existing requirements pinned; Tenacity need not be used when explicit small retry loops make classification clearer.

**Rationale:** This avoids unnecessary packaging work and aligns the test runtime with the image. Flat application modules import by their top-level names, because Docker copies `app/` into `/code` and runs `main:app`. Tests set `PYTHONPATH=app` rather than introduce incompatible `app.*` imports.

## Data Flow

```text
POST create → validate input → store queued/unadmitted → 201 {id}
POST process → atomic admission → queue ID → 202 {id,status}
                                      │
                              one of 32 workers
                                      │
                             processing + deadline
                                      │
                      AI request → normalize → validate
                                      │
                          shared notification gate
                                      │
                         notify → validate receipt
                                      │
                              sent or failed
GET status ─────────────────────────→ store snapshot
```

## File Changes

These are future implementation changes; this planning change creates no application code.

| File | Action | Responsibility |
|---|---|---|
| `app/main.py` | Modify | `create_app(...)` factory, module-level `app`, lifespan, three route handlers, dependency wiring |
| `app/settings.py` | Create | Validated environment-backed settings and defaults |
| `app/models.py` | Create | Strict API/payload models, status enum, record and internal failure categories |
| `app/store.py` | Create | Records, lock, admission/state transitions, expiry and terminal eviction |
| `app/extraction.py` | Create | System prompt, bounded candidate extraction, syntax repair, canonicalization and semantic validation |
| `app/provider_client.py` | Create | HTTP requests, bounded response reads/envelope checks, typed stage errors, timeout configuration |
| `app/pipeline.py` | Create | Queue/workers, retry classification, notification gate, deadlines and shutdown |
| `app/tests/test_api.py` | Create | Public contracts, invalid input, duplicate admission and overload |
| `app/tests/test_extraction.py` | Create | Deterministic 16-case variant matrix and adversarial/escaping boundaries |
| `app/tests/test_provider_client.py` | Create | Headers, body/envelope, limits, timeout/error classification |
| `app/tests/test_pipeline.py` | Create | Terminal outcomes, retry budgets, gate windows and worker isolation |
| `app/tests/test_lifecycle.py` | Create | Startup, drain/cancellation, expiry, cleanup and instance isolation |
| `app/tests/test_end_to_end.py` | Create | In-process app with scripted upstream; actual notify payload/count and terminal polling |
| `app/smoke.py` | Create | Explicitly invoked live acceptance probe: create/process/poll, summaries and deadline exit |
| `readme.md` | Append later | Commands, settings, chosen semantics, limits, evidence and operational caveats |

No change is planned for `provider/`, existing `platform/` files, `app/Dockerfile`, `docker-compose.yaml`, or runtime pins. If implementation discovers a necessary deviation, document the reason before changing that scope.

## Interfaces / Contracts

### API and state

| Request | Result |
|---|---|
| Valid create | 201, exactly `{id: string}`; UUID4; state `queued`, `admitted=false`; no upstream call |
| First admitted process | 202, `{id, status}`; normally `queued`, possibly `processing` if a later snapshot observes worker start |
| Repeat process, queued/admitted or processing | 202, current `{id, status}`; no additional queue entry |
| Repeat process, sent/failed | 200, current `{id, status}`; no upstream work |
| Known GET | 200, exactly `{id, status}` |
| Unknown/evicted ID | 404 with stable `detail` value `request_not_found` |
| Invalid create body | 422 using sanitized validation detail; missing/non-string/empty/whitespace-only/oversized input rejected |
| Queue/store saturated, or new work during shutdown | 503 with `detail` of `queue_full`, `store_full`, or `shutting_down`; failed process admission remains queued/unadmitted |

Preserve accepted `user_input` byte-for-byte as its decoded string; use whitespace stripping only to test emptiness. Reject unexpected create fields for a precise contract. Register a `RequestValidationError` handler: return safe validation categories and fixed body/field locations, excluding Pydantic `input`, `ctx`, exception strings and unknown field names that may contain caller data. Malformed JSON uses the same sanitized 422 policy. IDs remain arbitrary path strings for lookup, so malformed UUID-looking input is still a 404 rather than a different route-validation behavior.

Record fields: `id`, `user_input`, `status`, `admitted`, monotonic `created_at`, optional `admitted_at`/`deadline`/`finished_at`, `ai_attempts`, `notify_attempts`, optional `provider_id`, and internal `failure_code`. `admitted` is historical: it stays true after completion. Store snapshots do not expose mutable record references. Failure messages, input text, recipient, and provider credentials are not returned by the public status route.

Allowed transitions are `queued/unadmitted → queued/admitted → processing → sent|failed`, plus `queued → failed` on expiry/shutdown. Overload is not a state transition. Every state mutation is conditional on its expected nonterminal state so a late response cannot overwrite a timeout/shutdown outcome. A delivery acknowledgment arriving after local cancellation cannot prove what the remote system did; retain an internal uncertain-outcome category.

Suggested interfaces (signatures are design guidance, not generated implementation):

```text
create_app(settings=None, transport=None, clock=None, sleep=None) -> FastAPI
RequestStore.create(user_input) -> request snapshot
RequestStore.admit(id, queue, now) -> snapshot + accepted/already_active/terminal
RequestStore.start(id, now) -> record or skip
RequestStore.finish(id, outcome, metadata) -> conditional update
extract_notification(content) -> Notification | typed ExtractionFailure
ProviderClient.extract(user_input, retry_feedback=None) -> bounded content string
ProviderClient.notify(Notification) -> validated provider_id
NotificationGate.acquire(deadline) -> reserved attempt slot
Pipeline.start() / Pipeline.stop(grace_seconds)
```

### Capacity, retention and configuration

Use environment names prefixed `NOTIFY_`, except the descriptive provider settings below. Parse once at startup; reject invalid nonpositive values and inconsistent worker/queue limits before serving. Tests override settings directly and use tiny capacities/time values.

| Setting | Default | Reason |
|---|---:|---|
| `PROVIDER_BASE_URL` | `http://localhost:3001` | Supplied host/shared-network topology |
| `PROVIDER_API_KEY` | supplied development key | README; never logged |
| `NOTIFY_WORKERS` | 32 | Overlaps AI latency with bounded memory/connections |
| `NOTIFY_QUEUE_CAPACITY` | 4096 waiting IDs | Finite burst absorption; not a delivery promise |
| `NOTIFY_RECORD_CAPACITY` | 10000 records | Includes unadmitted and completed records |
| `NOTIFY_JOB_DEADLINE_SECONDS` | 1800 from admission | Includes queue wait, retries and rate waits |
| `NOTIFY_UNADMITTED_TTL_SECONDS` | 1800 from creation | Abandoned create calls cannot occupy records forever |
| `NOTIFY_SHUTDOWN_GRACE_SECONDS` | 10 | Bounded graceful drain |
| `NOTIFY_INPUT_MAX_CHARS` | 4096 | Bounded stored intent |

Keep parser/request safety limits as named constants unless a real use case requires configuration: upstream response body 65536 bytes for both AI and notification, extracted content 16384 UTF-8 bytes, nesting depth 8, recipient 320 characters, message 4096 characters. HTTPX connection limits: 40 total and 40 keep-alive, enough for the worker pool. Input character bounds do not claim a hard HTTP body limit; if a body-size middleware is added, specify and test its distinct 413 behavior rather than silently alter 422 model validation.

On record pressure, evict oldest terminal records only; their IDs subsequently return 404. Never evict active or unadmitted records merely to admit another request. A five-second sweep marks expired unadmitted records and expired admitted-but-queued records failed. Queued expired IDs are harmless tombstones until workers dequeue/skip them; balance queue accounting. Processing jobs enforce their remaining absolute deadline with `asyncio.timeout`; the sweep does not race to overwrite active successful completion. Record expiry and terminal eviction occur under the store lock. At most 4096 waiting plus 32 executing jobs exist; a full queue rejects promptly instead of waiting in the HTTP handler.

At roughly 4.76 locally paced notification attempts/second, 4096 first-attempt notifications alone need about 14.4 minutes to drain, excluding AI, failures and retries. The 30-minute deadline allows backlog without claiming all accepted jobs always succeed. Persistent upstream failure can exhaust time/retry budgets and fail work. Load reports must state backlog and observed drain, not infer completion from acceptance.

### Extraction prompt and normalizer

Send a dedicated system instruction and the original input as a separate user message. The instruction must request one JSON object with exactly `to`, `message`, `type`; type is `email|sms`; preserve message text/case, never fabricate missing values, interpret user content as data rather than system instructions, and return an explicit inability response if required information is absent or the request is refused. Give concise positive email/SMS examples and an incomplete-request example. The mock ignores this policy, so runtime checks remain authoritative.

Normalization is pure and deterministic:

1. Bound bytes and depth before decoding. Strip only outer whitespace/BOM and recognized surrounding wrappers, never arbitrary message characters. Refusal/inability prose is terminal; do not retry it to obtain a different policy decision. Treat the provider's three refusal variants and missing-destination/type explanation explicitly. Non-object prose without a recoverable object is a terminal `unstructured_output`, avoiding an assumption that unknown prose is safe to retry.
2. Find exactly one candidate object using a quote/escape-aware scan. Allow a JSON or generic code fence, or explanatory prefix/suffix. Reject multiple object candidates; do not choose the most convenient one. Refusal/inability statements outside the object take precedence over extracting an apparent embedded payload. Do not search message string contents for refusal keywords.
3. Attempt strict JSON with a duplicate-key-rejecting pairs hook. Root must be an object; arrays/scalars are invalid. Apply depth/size guards and handle decoder exceptions without leaking them from a worker.
4. If syntax alone failed, use a constrained flat-object parser: quoted string values; string keys in double quotes, single quotes, or bare identifier form; scalar metadata values permitted only in a strictly parsed object. Tokenize outside strings so punctuation inside message text is preserved. For single-quoted strings accept only well-defined escaped quotes/backslashes and standard string escapes; reject unknown/ambiguous escapes and unescaped inner delimiters. Do not use `eval`, global quote replacement, a general code parser, or guess where broken string values end.
5. Support the exact truncated variant only when the parsed object contains all three complete required string values and the remaining suffix is whitespace plus `...` with no closing brace. Remove that suffix and supply the final brace. Any truncation inside a key/value or missing required field remains invalid. Combining supported repairs is allowed only when the same bounded grammar yields one unique object.
6. Case-fold keys and map `to|recipient|destination`, `message|body|text`, and `type|channel|method`. Reject duplicate canonical fields even if values agree; conflicting aliases must never silently win. Ignore unrelated extras after bounded parsing. Canonical fields must be strict strings; trim outer whitespace from recipient/type, lowercase type, and preserve message exactly. Reject empty/whitespace-only values and length violations.
7. Validate email as one `@`, nonempty local part, a dotted domain, and no whitespace/control characters. Validate SMS as optional leading `+`, digits/spaces/hyphens only and 9–15 digits after removing formatting. Preserve recipient formatting apart from outer whitespace. This is a conservative challenge validation policy, not full international delivery validation. Reject type/destination mismatch. Do not infer absent type/recipient from other fields or re-extract from original text.

Canonical, three alias variants, extra metadata, three wrappers, and three unambiguous malformed variants should normalize successfully. Two missing-field variants and three refusal variants fail terminally. Embedded unescaped quotes/backslashes/newlines may make even the mock's nominal branch ambiguous: retry a syntax failure once, then fail rather than corrupt content. The application must not reproduce the mock's lowercasing/last-colon extraction, replace the AI with regex extraction, or modify the provider to mask this limitation.

### Upstream requests, retries and delivery outcomes

Use a shared `httpx.AsyncClient` with `base_url`, `X-API-Key`, `follow_redirects=False`, and explicit timeout settings. Stream/read responses with a byte cap rather than accepting an unbounded body before checking its size. AI read timeout 10s; notify read timeout 5s; connect/write/pool timeouts 2s. Bound each entire AI attempt to 15s and notify attempt to 10s in addition to inactivity timeouts; every operation also observes the job's remaining absolute deadline. Disable automatic transport retries so attempt counters match policy.

AI succeeds only for HTTP 200 with nonempty `choices[0].message.content` of strict string type and within limits. Notification succeeds only for HTTP 200 with `status == "delivered"` and nonempty string `provider_id`. Malformed success bodies never mark `sent`.

| Stage / outcome | Action and total attempt limit |
|---|---|
| AI connection/read/write/pool/transient transport error; 429, 500, 502, 503, 504 | Retry once (two total); delay 0.25s plus injected uniform 0–0.25s jitter |
| AI malformed envelope or recoverable-looking syntax that remains unparsable | Retry once with concise system feedback; same unchanged user message |
| AI missing/invalid field, duplicate/conflicting aliases, ambiguous multiple objects, refusal/inability, oversize/depth violation | Terminal failure; no notification |
| AI other 4xx or unexpected HTTP status | Terminal provider/auth failure |
| Notify 429 | Shared cooldown 10.1s, then reacquire gate; maximum three total attempts |
| Notify `ConnectError`, `ConnectTimeout`, `PoolTimeout` before transmission | Retry with 0.25s + 0–0.25s jitter, reacquiring gate; maximum three total attempts |
| Notify read/write error/timeout, protocol failure, overall timeout, malformed 200 receipt, other 2xx, 5xx | Terminal `delivery_unknown`; no resend because delivery may have occurred |
| Notify other 4xx/unexpected status | Terminal `notification_rejected`; no retry |
| Unexpected internal exception | Mark `failed/internal_error`, keep other workers alive; log sanitized exception category |

Count attempts immediately before each actual request; rate reservations may exceed request attempts if a deadline expires after reservation. Never restart AI after notification begins. A job deadline or cancellation during possible notify transmission becomes `delivery_unknown`; before transmission it is a known local expiry/shutdown failure. Exhausting explicit 429/pre-send failures is `notification_unavailable`. Public status remains the four-state contract; detailed reasons are internal.

HTTPX distinguishes connect, pool, read and write failures in its [exception taxonomy](https://www.python-httpx.org/exceptions/). The retry policy above is this design's conservative inference from transmission stage, not an HTTPX exactly-once guarantee. Do not put all `RequestError` exceptions into one retry branch.

### Notification gate

One shared gate uses an injected monotonic clock, a deque of reserved attempt times, a lock and a shared `cooldown_until`. Before each attempt, remove reservations older than 10.1s; wait until there are fewer than 50, the last dispatch is at least 0.21s old, and cooldown has elapsed. Recheck under the lock after every sleep, then append a reservation immediately before dispatch. Never hold the gate lock during HTTP or sleep. Reservations are not refunded on errors: the provider may already have counted the call, and its admitted 500 calls consume budget.

This combines a rolling-window cap with even pacing and modest margin. It bounds local dispatches; network scheduling and other provider clients can still cause a 429. Update shared cooldown on any 429 even without `Retry-After` (the fixture supplies none); all workers honor it. Honor a valid larger `Retry-After` duration or HTTP date, converted to a monotonic delay at receipt. If that exceeds a job's deadline, fail that job rather than retry early. Never shorten the 10.1s fallback. There is no global coordination across app processes: document that limitation and use one worker process only.

### Lifecycle and observability

Create store, queue, client, workers and expiry sweep inside FastAPI lifespan and keep references on `app.state`. The factory allows isolated test instances. [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) provides the ownership boundary; module import must not start event-loop tasks or open clients.

On shutdown: close admission; allow queued/in-flight work to drain for up to 10s; cancel and await all worker/sweep tasks; mark remaining nonterminal records failed with shutdown/unknown-delivery reasons; remove pending queue items and balance `task_done`; close the client last. Workers always balance each successful `get` in `finally`, isolate per-job exceptions, and re-raise cancellation after accounting. A fatal worker exit outside the job loop must disable new admission and fail outstanding work rather than silently reducing the pool forever. No persistence survives process exit.

Use Python 3.12 queue operations rather than newer shutdown APIs. [`asyncio.Queue`](https://docs.python.org/3.12/library/asyncio-queue.html) supports finite admission and matching `get`/`task_done` accounting; explicitly test join/drain and cancellation paths.

Structured logs include request ID, stage, outcome/failure code, attempts, duration, queue depth, state counts and drain summaries. Exclude original input, full AI content, recipient, message, API key and unrestricted exception bodies. These logs support diagnosis; no new observability endpoint is needed.

## Testing Strategy

| Layer | Evidence required | Mechanism |
|---|---|---|
| API/store | Correct codes/shapes; create never calls provider; unknown IDs; duplicate/concurrent admission; capacities and terminal eviction | ASGITransport inside explicit lifespan; tiny store/queue settings |
| Extraction | All 16 variants with exact expected payload/failure; wrappers/braces/escaped quotes; duplicate aliases/keys; types, recipient mismatch, Unicode, size/depth, refusal | Literal deterministic fixtures derived from provider behavior, plus independently authored edge cases; never depend on randomness |
| Client | Both auth headers and exact paths/body; nested AI envelope; bounded responses; receipt verification; error classification | HTTPX MockTransport scripted responses/exceptions |
| Pipeline | Sent only after receipt; failures terminal; AI ≤2; notify ≤3; no AI redo during notify; no unsafe resend; other jobs survive error | Scripted transport, event barriers, isolated async tests |
| Rate/deadline | No >50 reservations in 10.1s; minimum spacing; all attempted failures counted; shared429 cooldown; remaining deadline includes waits | Inject fake monotonic clock/sleep and explicit attempt timestamps; no ten-second real sleeps |
| Lifecycle | Startup resources scoped; graceful drain; forced cancellation/accounting; unadmitted/queued expiry; no client/task leaks | Explicit lifespan entry/exit with controlled blocking calls |
| Deterministic end-to-end | Create→process→terminal; expected notify destination/message/channel and exactly one notification for duplicate process calls | Real app routes with fake upstream; poll to bounded terminal deadline |
| Live provider/load | Container import/start; actual auth/integration; terminal outcomes, backlog/drain and process latency | Unmodified provider/k6 plus `app/smoke.py`; stochastic failures classified, not hidden |

Future commands, not executed during this design phase:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
PYTHONPATH=app .venv/bin/python -m unittest discover -s app/tests -v
docker compose up -d provider influxdb grafana
docker compose up -d --build app
.venv/bin/python app/smoke.py --base-url http://localhost:5000 --timeout 120
docker compose run --rm load-test
docker compose logs app
git diff --check
```

If Python 3.12 is unavailable, use the built app image to run `python -m unittest discover -s tests -v` through `docker compose run --rm --no-deps app`; tests must not need a live provider. Do not treat host Python 3.10 or differing installed dependency versions as equivalent validation.

`smoke.py` uses a small fixed set of valid inputs and polls every returned ID to a terminal state; it prints counts and elapsed times and fails on HTTP contract errors or unresolved deadlines. Because the real mock may refuse or omit values, report terminal failures explicitly without asserting that every random sample must be sent. Deterministic integration tests supply the strict delivery-success assertion. Keep live samples small enough to avoid compounding the k6 backlog. After load, use state-count logs to report outstanding work and wait/report drain separately; the k6 script discards IDs after a single poll, so it cannot alone establish eventual completion. Inspect its check counts despite its lack of configured thresholds. Record dashboard ≤400ms process-latency cue and AI-attempt ratio as measurements, not unconditional pass guarantees.

## Migration / Rollout

No migration required. Add tests and cohesive modules, wire routes/lifespan, validate with pinned Python 3.12, then rebuild the existing image. Keep one app process and unchanged network configuration. Append operational guidance without replacing the original challenge text. The proposal contains rollback guidance; in-memory records and unsent work are lost on restart or rollback.

## Open Questions

- Nonblocking scope assumption: persistence across restarts and multiple worker processes were offered as a clarification; absent a response, this plan explicitly chooses one process and in-memory state. A later durability requirement changes store, claim/recovery and limiter design together.
- No blocking implementation choices remain under that assumption. Capacity defaults are initial engineering values to verify with the supplied load, not measured capacity claims; changes after measurement must preserve the stated behavior and bounded-resource tests.
