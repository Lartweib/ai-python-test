# notification-delivery Specification

## Purpose

Define authenticated delivery, rate/attempt bounds, and evidence needed to assess challenge completion. `README` denotes explicit challenge behavior; `Decision` denotes selected reliability policy informed by `provider/app.py`.

## Requirements

### Requirement: DEL-1 — Send validated payload and verify acknowledgment

**README:** a valid extraction MUST be submitted to `/v1/notify` with `X-API-Key`. **Decision:** the body MUST contain only validated `to`, `message`, and `type`. A request MUST become `sent` only after HTTP 200 with `status: delivered` and a nonempty string `provider_id`. Other successful HTTP codes or malformed acknowledgments MUST NOT count as delivered and MUST NOT trigger a potentially duplicating resend.

#### Scenario: Delivery evidence

- GIVEN a valid extraction
- WHEN the provider acknowledges HTTP 200 with the expected delivered response
- THEN the exact canonical payload is sent with auth and the request becomes `sent` with its receipt retained internally.

#### Scenario: Invalid acknowledgment

- GIVEN a 200 response with missing receipt, wrong status, or malformed body, or a different 2xx response
- WHEN notification returns
- THEN the request becomes `failed` with an internal uncertain-delivery category and no automatic resend.

### Requirement: DEL-2 — Respect notification capacity across jobs and retries

**Decision:** notification dispatch MUST be coordinated across all jobs in this application process and MUST admit no more than 50 attempts in any sliding ten-second interval. Every actual attempt MUST consume local rate capacity, including failed attempts and retries; concurrency limits alone are insufficient. Provider 429 responses MUST respect a shared cooldown of at least ten seconds when no valid retry guidance exists, bounded by the job deadline. Valid longer retry guidance MUST be honored within that deadline. Independent external consumers are outside the local limiter's control and MUST NOT be assumed absent when classifying 429.

#### Scenario: Sliding-window boundary

- GIVEN more than 50 ready notifications and a controllable clock
- WHEN dispatch crosses ten-second boundaries
- THEN no sliding ten-second interval contains more than 50 local attempts, including retries and failed responses, and later work waits without blocking API responses.

### Requirement: DEL-3 — Retry only outcomes that permit a safe resend

**Decision:** each extracted payload MUST have a total notification-attempt cap of three. A definitive 429 rejection or a proven pre-send connection failure MAY be retried within that cap and the overall deadline. Authentication/validation/permanent 4xx errors MUST fail without retry. Read/write timeouts, connection loss after a possible send, malformed acknowledgments, and upstream 5xx responses without a documented non-delivery guarantee MUST fail as uncertain delivery without resend. Exactly-once delivery MUST NOT be claimed. Notification retries MUST reuse the validated payload and MUST NOT re-run AI extraction.

#### Scenario: Safe retry and exhaustion

- GIVEN a 429 then delivered response, or repeated proven pre-send failures
- WHEN notification attempts run
- THEN the first case waits for the required cooldown and reaches `sent` without another AI call; the latter fails after at most three attempts.

#### Scenario: Ambiguous delivery

- GIVEN the provider may have accepted a notification before a read timeout or connection interruption, or returns a 5xx without a non-delivery guarantee
- WHEN the failure is classified
- THEN the task becomes `failed` with an uncertain-delivery category and no further notification attempt occurs.

### Requirement: DEL-4 — Provide useful evidence without sensitive payloads

**Decision:** diagnostic events SHOULD include request ID, stage, outcome/failure category, attempt count, and elapsed duration. Logs MUST NOT include credentials, full user input, destination, message, or raw provider bodies. The verification report MUST distinguish HTTP acceptance/availability, terminal outcomes, backlog/drain time, and AI attempts. The supplied k6/Grafana metrics alone MUST NOT be represented as delivery verification.

#### Scenario: Failure diagnostics and delivery verification

- GIVEN healthy and failing controlled end-to-end requests plus captured logs
- WHEN verification polls to terminal states and inspects provider call records
- THEN it verifies notify payload/count and terminal outcomes separately from HTTP response checks, and diagnostics expose useful failure categories without sensitive input or credentials.

### Requirement: DEL-5 — Expose operational metrics

**Decision:** the service SHOULD expose `GET /metrics` in the Prometheus text format (0.0.4) without adding a client library. It MUST report current requests by status, queue depth and capacity, configured workers, an availability gauge, and monotonic counters for created, replayed, admitted and evicted requests, terminal outcomes by failure code (every code pre-declared at zero), AI and delivery attempts, provider dispatches and 429 pauses, plus an admission-to-terminal time summary. Counters MUST survive record eviction. Labels MUST be fixed codes only and MUST NOT include user input, destinations, messages or idempotency keys. Scraping MUST NOT call the provider.

#### Scenario: Metrics reflect delivery without leaking content

- GIVEN a completed request created with an idempotency key and containing an email address
- WHEN `/metrics` is scraped
- THEN the output is well-formed exposition text with the sent outcome and dispatch counted, no upstream call is made, and neither the address nor the key appears.

