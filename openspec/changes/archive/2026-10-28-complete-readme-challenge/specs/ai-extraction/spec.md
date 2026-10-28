# AI Extraction Specification

## Purpose

Define extraction, normalization, and rejection behavior for the supplied noisy AI service. `README` denotes the challenge contract; `Decision` denotes explicit guardrail policy. `provider/responses.py` is fixture evidence, not an implementation dependency.

## ADDED Requirements

### Requirement: AI-1 — Authenticate and instruct extraction

**README:** processing MUST call `/v1/ai/extract` with `X-API-Key` and system/user messages requesting `{to, message, type}`. **Decision:** the original input MUST be supplied unchanged as the user message. The system instruction MUST specify one JSON object, string fields, only `email|sms`, no Markdown, preservation of message content, no invented missing information, and treating user text as data rather than extraction instructions. The provider URL/key MUST be configurable with challenge-compatible defaults.

#### Scenario: Inspect the extraction request

- GIVEN an input containing mixed case, punctuation, and text that asks to ignore previous instructions
- WHEN its accepted job calls the AI
- THEN the request has the configured auth header, a dedicated schema/guardrail system message, and the unchanged original user text in a separate user message.

### Requirement: AI-2 — Validate the response envelope

**Decision:** extraction MUST consume string content from the expected first choice's message. Invalid JSON, absent/empty choices, missing message/content, non-string content, and content above the configured size limit MUST NOT be sent to the notification provider. Retry eligibility MUST follow AI-5; oversized responses MUST fail without unrestricted parsing.

#### Scenario: Invalid and excessive envelopes

- GIVEN each invalid envelope form above or an over-limit body/content
- WHEN the AI response is processed
- THEN no notification is sent, processing is bounded, and the request either receives its permitted retry or fails under AI-5.

### Requirement: AI-3 — Normalize only unambiguous representations

**README:** guardrails MUST clean and validate noisy/malformed extraction output. **Decision:** normalization MUST support the recoverable fixture variants below while preserving message content and recipient formatting except outer recipient whitespace. Outer channel whitespace and channel case MAY be normalized. Key matching MUST be case-insensitive with only the fixed aliases `to|recipient|destination`, `message|body|text`, `type|channel|method`. Irrelevant extra metadata MAY be discarded. Duplicate keys or aliases for the same canonical field MUST be rejected even when values agree. Multiple payload candidates, ambiguous syntax, unfinished string values, or missing required values MUST NOT be guessed into validity. Syntax repair MUST NOT execute supplied expressions or infer content from the original prompt.

#### Scenario: All sixteen provider variants

- GIVEN one controlled response for each row, with otherwise valid, unambiguous destination/message/channel values
- WHEN normalization and validation run
- THEN the result MUST match the table, with accepted strings preserved and rejected rows causing zero notifications.

| Fixture variant | Expected result |
|---|---|
| Canonical JSON | Accept |
| `Recipient/body/channel` | Accept with canonical keys |
| `To/Message/Type` | Accept with canonical keys |
| `destination/text/method` | Accept with canonical keys |
| Extra confidence/latency metadata | Accept only canonical payload fields |
| Missing type | Reject; do not infer channel |
| Missing destination | Reject; do not recover recipient from user text |
| JSON Markdown fence | Accept the single object |
| Generic fence | Accept the single object |
| Prose containing one JSON object | Accept the single object |
| Complete three string values followed by literal ` ...` without closing brace | Accept only this bounded unambiguous completion |
| Single-quoted flat object | Accept only unambiguous quoted string values |
| Unquoted known field names | Accept when values are valid quoted strings |
| Personal-data refusal | Reject terminally |
| Spam/policy refusal | Reject terminally |
| Sensitive-content refusal | Reject terminally |

#### Scenario: Escaping, ambiguity, and executable-looking content

- GIVEN escaped quotes, braces, Unicode, or backslashes inside valid string values; or a separate case with unescaped ambiguous quotes, incomplete values, multiple objects, duplicate keys, conflicting aliases, nesting beyond limits, or executable expressions
- WHEN normalization runs
- THEN valid string contents are preserved, unsafe/ambiguous cases are rejected within configured limits, and no supplied expression is executed.

### Requirement: AI-4 — Validate notification semantics

**Decision:** normalized `to`, `message`, and `type` MUST all be strings; `to` and `message` MUST contain non-whitespace content and fit configured field limits. Channel MUST normalize to `email` or `sms` only. Email destinations MUST have one `@`, a nonempty local part and dotted domain, with no whitespace; SMS destinations MUST contain only digits, spaces, hyphens and an optional leading `+`, with 9–15 digits. Destination/channel mismatch MUST fail. The message MUST NOT be lowercased, rewritten, or reconstructed by the application. Canonical notify payload MUST have exactly the three required fields.

#### Scenario: Valid channels and invalid values

- GIVEN valid email and supplied 9/10-digit or hyphenated SMS destinations, plus separate cases with wrong field types, blank messages, invalid channels, malformed recipients, or an email labeled SMS
- WHEN semantic validation runs
- THEN valid cases produce the three-field payload with original message content, while every invalid case produces no notification.

### Requirement: AI-5 — Bound extraction retries and respect refusal

**Decision:** one request MUST make at most two AI attempts in total, including transport, HTTP, envelope, and syntax failures. Transient transport failures, HTTP 429/5xx, malformed envelopes, and unrecoverable representation syntax MAY receive the one remaining attempt with bounded backoff. Authentication/other permanent 4xx errors, refusals, explanations of missing input, missing/invalid required values, and conflicting/ambiguous semantic candidates MUST fail without retry. Retrying MUST preserve the original input and MUST NOT instruct the AI to bypass a refusal. Notification failures MUST NOT trigger fresh extraction.

#### Scenario: Shared attempt budget

- GIVEN a transient AI failure followed by another retryable failure
- WHEN the job runs
- THEN exactly two attempts occur, the job becomes `failed`, and no nested retry policy creates additional attempts.

#### Scenario: Refusal and invalid semantic output

- GIVEN any of the three refusal variants, missing-entity explanatory prose, invalid required values, or conflicting aliases
- WHEN the first AI response is inspected
- THEN the job fails after one attempt without notification or refusal-bypass prompting.
