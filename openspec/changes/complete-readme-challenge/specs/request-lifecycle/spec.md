# Request Lifecycle Specification

## Purpose

Define the externally observable request contract and bounded processing lifecycle for the pending README challenge. This is a new specification; no earlier domain specification exists.

**Origin:** `README` labels identify explicit `readme.md` obligations. `Decision` labels identify chosen behavior for unspecified cases, under the single-process, in-memory assumption. Numeric configuration defaults belong in `design.md`.

## ADDED Requirements

### Requirement: API-1 — Create an independent request

**README:** `POST /v1/requests` MUST accept a JSON object containing string `user_input` and return HTTP 201 with a nonempty string `id`. The required FastAPI application MUST remain available through `app/main.py` on port 5000. **Decision:** creation MUST retain the original input unchanged, expose `queued`, and MUST NOT call either provider endpoint. Each creation MUST have its own ID; identical input is not an idempotency key.

#### Scenario: Create and observe a request

- GIVEN the service has record capacity and a valid `user_input`
- WHEN a client creates a request and retrieves its returned ID
- THEN creation returns 201 with `{id: string}` and retrieval returns 200 with that ID and status `queued`
- AND no AI or notification call has occurred.

#### Scenario: Equal input represents two requests

- GIVEN capacity for two records
- WHEN the same valid body is submitted twice
- THEN two distinct IDs are returned and neither request starts processing automatically.

### Requirement: API-2 — Reject invalid input and unknown IDs

**Decision:** `user_input` MUST be a string with non-whitespace content within the configured character limit; coercion from numbers, arrays, objects, booleans, or null MUST NOT occur. Invalid JSON/request bodies MUST return 422 without creating a record. Both ID routes MUST return 404 for unknown IDs. Public errors MUST NOT disclose input, provider credentials, or raw upstream responses.

#### Scenario: Invalid creation matrix

- GIVEN a missing, null, non-string, empty, whitespace-only, malformed-JSON, or over-limit input body
- WHEN it is submitted to the creation route
- THEN the response is 422, record count is unchanged, and neither provider is called.

#### Scenario: Unknown request

- GIVEN an ID that has never been created or whose eligible record has expired
- WHEN GET or POST-process is called for it
- THEN the response is 404 and no processing begins.

### Requirement: API-3 — Admit processing promptly and at most once

**README:** processing MUST be triggered by `POST /v1/requests/{id}/process`. **Decision:** a successfully admitted nonterminal request MUST return 202 with `{id, status}` without awaiting upstream work. Concurrent/repeated process calls MUST admit at most one execution per ID. An admitted request waiting to run remains `queued`; it becomes `processing` when execution starts. Repeated calls for admitted queued/processing work MUST return 202 with its current status; terminal requests MUST return 200 with their terminal status and MUST NOT resend.

#### Scenario: Asynchronous acceptance

- GIVEN a created request and an AI response intentionally held pending
- WHEN processing is requested
- THEN the process response completes with 202 before AI is released
- AND GET reports `queued` or `processing` until a terminal outcome occurs.

#### Scenario: Concurrent and terminal duplicates

- GIVEN one request and a deterministic valid extraction/delivery provider
- WHEN many concurrent process calls are issued, followed by another call after completion
- THEN one execution is admitted, exactly one AI call and one notification occur, and the later call returns 200 with `sent`
- AND the same terminal no-resend rule applies to `failed` requests.

### Requirement: API-4 — Preserve truthful monotonic state

**README:** GET MUST return HTTP 200 with `{id, status}`, where status is exactly one of `queued`, `processing`, `sent`, `failed`. **Decision:** accepted work MUST follow `queued → processing → sent|failed`, except shutdown or deadline expiry MAY fail admitted queued work directly; unadmitted retention expiry also transitions `queued → failed`. `sent` MUST require the delivered acknowledgment specified by delivery requirements. Terminal records MUST NOT revert to an earlier state. Internal failure categories MUST remain separate from the public response.

#### Scenario: Valid delivery and exhausted failure

- GIVEN two admitted requests, one receiving a valid delivered acknowledgment and one exhausting its permitted extraction attempts
- WHEN processing completes
- THEN the first is `sent`, the second is `failed`, and repeated GET calls retain those outcomes.

### Requirement: API-5 — Bound retention and admission

**Decision:** retained records and admitted jobs MUST have finite configurable capacities. When record capacity is exhausted with no eligible terminal record to evict, creation MUST return 503 without a new record; otherwise the oldest terminal record MAY be evicted to admit a new record. When processing capacity is exhausted, a previously unadmitted request MUST return 503, remain `queued` and unadmitted, and be eligible for a later process attempt. Duplicate calls for already admitted work MUST NOT consume another capacity slot or fail solely because capacity is full. Active/admitted work MUST NOT be evicted to make space. Unadmitted records MUST become `failed` when their configured inactivity retention expires; admitted work uses its completion deadline instead.

#### Scenario: Overload and later admission

- GIVEN all processing slots are occupied and a separate unadmitted queued request exists
- WHEN its process route is called, then called again after capacity becomes available
- THEN the first call returns 503 without provider calls or state corruption and the later call returns 202 with exactly one execution admitted.

#### Scenario: Retention never discards active work

- GIVEN a full record store containing admitted work, terminal records, and an unadmitted record past its retention limit
- WHEN cleanup and creation run concurrently
- THEN the expired unadmitted record becomes `failed`, only terminal records may be evicted, active work remains retrievable, and creation either succeeds within capacity or returns 503.

### Requirement: API-6 — Bound work and service shutdown

**Decision:** accepted work MUST have a finite overall completion budget that includes waiting, attempts, and backoff. Exhaustion MUST result in `failed` with an internal deadline category. Unexpected per-job errors MUST fail that job without stopping other jobs. On shutdown the service MUST stop new admission, permit a configured finite drain period, then account for remaining admitted work as failed before releasing resources. Restart persistence and multiple application workers are unsupported in this scope and MUST be documented.

#### Scenario: Deadline and isolated failure

- GIVEN one deadline-expired request, one request whose worker operation raises unexpectedly, and a healthy request
- WHEN processing advances
- THEN the first two become `failed`, the healthy request can complete, and no execution slot is permanently leaked.

#### Scenario: Bounded shutdown

- GIVEN admitted queued and active requests, including a stalled upstream operation
- WHEN shutdown begins and the drain budget expires
- THEN new work is rejected, outstanding admitted work is accounted for, pending operations terminate, and shared resources close within the documented bound.
