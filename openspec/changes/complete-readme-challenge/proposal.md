# Proposal: Complete the Intelligent Notification Service challenge

## Intent

Implement the application required by `readme.md`: accept a natural-language notification request, extract structured data through the supplied AI service, validate it, send a notification, and expose its processing status. `app/main.py` currently only instantiates FastAPI; all three routes and the processing pipeline are pending.

This change is a planning handoff for another agent. The research baseline, provider behavior, and evidence are in [exploration.md](exploration.md). Implementation must preserve the supplied provider and evaluation fixtures.

## Scope

### In Scope

- Implement the exact README success contracts: create with HTTP 201 and string `id`; process with HTTP 202; retrieve the ID and `queued|processing|sent|failed` state. Preserve `app/main.py`, the `app` instance, and port 5000.
- Add atomic, duplicate-safe processing admission, explicit unknown-ID/invalid-input/overload behavior, finite queues and record capacity, and managed worker/client lifecycle.
- Call the authenticated AI endpoint with separate system/user messages and a precise extraction prompt. Parse the response envelope and normalize supported noisy output without fabricating recipient, message, or channel.
- Validate the canonical notification payload and only mark a task `sent` after the provider acknowledges delivery. Keep terminal failure categories available internally.
- Use bounded, classified retries and notification pacing that accounts for every admitted attempt against the provider's 50-per-sliding-10-second limit.
- Add deterministic unit/integration tests, terminal-state end-to-end checks, and reproducible execution instructions that supplement the supplied stochastic k6 checks.
- Document operational assumptions, failure semantics, configuration, meaningful load-test evidence, and safe diagnostic logging.

### Out of Scope

- Durable storage, restart recovery, multiple Uvicorn workers, distributed queues, Redis/Celery, and horizontal scaling under the default assumption below.
- Changing `provider/` or the existing `platform/` evaluator to make results pass; replacing AI extraction with application-side regex extraction.
- New product endpoints, UI, real external notification/LLM integrations, or application authentication absent from the README.
- Exactly-once delivery guarantees unsupported by the provider, and automatic resending of terminal tasks.
- General repository modernization, dependency upgrades, and dashboard repairs unrelated to delivering the challenge.

## Approach

Use one application process with an in-memory request store and a bounded asynchronous processing pipeline. Creation records the original input in `queued`; processing atomically admits work and returns promptly while lifespan-managed workers call a shared HTTPX asynchronous client. The design must define precisely when admitted work becomes `processing`, how repeated process calls respond, and how overload preserves retryable state without duplicate admission.

Keep extraction normalization separate from schema and semantic validation. Support the evidenced aliases, wrappers, extra fields, and unambiguous malformed representations. Reject conflicting values, missing required values, incompatible destination/channel pairs, and unsafe or ambiguous repairs. Treat genuine refusals as terminal failures without notification or attempts to circumvent refusal. Never use unrestricted evaluation of AI output.

Classify upstream failures by stage. A low, bounded AI-attempt cap limits avoidable scorecard overhead. Notification retries never restart extraction. A shared notification admission policy must respect the sliding window, including failed admitted attempts, and distinguish known rejection from uncertain delivery after a timeout or connection interruption. The latter cannot safely promise a resend without duplication risk.

**Architecture assumption:** the user was asked whether restart persistence/multiple workers are needed; no answer has been received at proposal time. Plan for the supplied one-worker topology and process-local state. This is an explicit scope assumption, not a README requirement. Revisit the store, job lifecycle, and shared rate limiter together if the user requires durability or multiple workers.

The README allows asynchronous processing; the dashboard's fast process-response cue is a mean of at most 400 ms, while AI calls alone take 1.5–3 seconds. Treat this cue as an evaluation target, not a newly invented contractual SLO. Finite intake must be coherent with approximately five notification admissions per second; report backlog and drain time separately from HTTP responsiveness.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/main.py` | Modified | Required routes, models/wiring, and lifespan ownership |
| `app/` new cohesive modules | New | Configuration, state/admission, extraction, provider client, and pipeline; exact paths finalized in design |
| `app/requirements.txt` | Conditional modification | Preserve runtime pins; change only if a justified implementation dependency is selected |
| New test files and development dependency configuration | New | Deterministic contracts, malformed-output matrix, concurrency, failures, rate limits, lifecycle, and terminal delivery checks |
| `readme.md` | Modified later | Append setup/testing/configuration, design decisions, limitations, and measured evidence without replacing the challenge |
| `app/Dockerfile`, `docker-compose.yaml` | Conditional modification | Only if required for imports/test setup; preserve `main:app` and shared network topology |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| HTTP checks pass while no notifications finish | High | Deterministic notify assertions and polling to terminal states; report completion separately from HTTP availability |
| Load intake exceeds provider throughput | High | Finite record/job capacities, explicit overload behavior, and a documented drain policy |
| Normalization alters message or recipient meaning | Medium | Bounded syntax-only repairs, semantic validation, conflict rejection, all 16 provider variants plus escaping edge cases |
| Retrying an ambiguous notify outcome duplicates delivery | Medium | Stage-specific retries, conservative unknown-delivery handling, no exactly-once claim |
| Local test/runtime imports differ from container | High | Verify Python 3.12 and pinned dependencies with the actual `uvicorn main:app` image layout |
| Worker crash or shutdown leaves tasks unresolved | Medium | Exception isolation, bounded shutdown, explicit cancellation accounting, and lifecycle tests |
| Mock extraction/telemetry limitations distort results | High | Preserve fixtures, document lowercasing/last-colon/unescaped-string limitations, and distinguish observed metrics from correctness |

## Rollback Plan

Keep the implementation in reviewable commits after this planning-only change. To roll back, revert those application/test/configuration commits and rebuild the previous app image; retain the SDD documents as the record of intent. No database migration or external persistent state is introduced under the default design. Stop accepting work and apply the documented bounded shutdown before replacing a running image; process-local outstanding requests cannot survive rollback or restart. Never reset or rewrite the supplied provider/evaluator to recover an application failure.

## Dependencies

- Supplied Python 3.12 application image and pinned FastAPI, Uvicorn, HTTPX, and Pydantic dependencies; development test dependencies must be declared reproducibly.
- Provider at configurable URL defaulting to `http://localhost:3001`, with `X-API-Key` default matching the challenge's development key. Never log the key.
- Docker Compose services for live integration/k6/Grafana verification; deterministic tests should run without a live provider or InfluxDB.
- The final spec/design must settle capacities, timeouts, retry ceilings, overload/error responses, normalization boundaries, and shutdown behavior before implementation starts.

## Success Criteria

- [ ] All README routes, response codes/shapes, statuses, entry-point and authentication contracts have deterministic passing tests.
- [ ] Supported recoverable AI variants produce validated notify payloads; incomplete, conflicting, ambiguous, and refused outputs never trigger a notification.
- [ ] Concurrent/repeated process calls admit at most one execution for a request; terminal requests do not resend.
- [ ] Notification attempts respect the supplied sliding-window capacity, including retry accounting; transient and permanent failure handling is bounded and tested.
- [ ] Healthy accepted work reaches `sent` only after a valid delivered acknowledgment; exhausted or unsafe work reaches `failed`; no worker exception silently strands a request.
- [ ] Capacity, overload, lifecycle, and shutdown behavior are deterministic and documented for one process, including loss of state across restart.
- [ ] The unchanged k6 harness is run in the intended container topology and inspected alongside terminal-delivery evidence, process latency, AI-attempt ratio, backlog, and drain time; command exit alone is not treated as a pass.
- [ ] The handoff contains full specifications, a concrete technical design, and dependency-ordered unchecked tasks with validation commands for the next agent.
