"""Guardrails: parse and normalize AI extraction output to {to, message, type}."""

import json
import re
# Alternate keys the mock may return -> canonical keys
_TO_KEYS = ("to", "recipient", "destination")  # "To" -> to via lowercase
_MESSAGE_KEYS = ("message", "body", "text")
_TYPE_KEYS = ("type", "channel", "method")


def _extract_json_candidate(raw: str) -> str | None:
    """Extract a JSON object string from raw text (Markdown blocks, embedded, etc)."""
    if not raw or not raw.strip():
        return None

    # 1. Try ```json ... ``` or ``` ... ```
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if code_block:
        return code_block.group(1).strip()

    # 2. Find {...} in text
    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    quote_char = None
    i = start

    while i < len(raw):
        c = raw[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\" and in_string:
            escape = True
            i += 1
            continue
        if not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return raw[start : i + 1]
            elif c in ('"', "'"):
                in_string = True
                quote_char = c
        else:
            if c == quote_char:
                in_string = False
        i += 1

    return None  # Unbalanced braces (truncated)


def _try_parse_json(s: str) -> dict | None:
    """Try to parse JSON, including single-quoted variant."""
    # 1. Standard JSON
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2. Single-quoted keys/values (invalid JSON but common in Python)
    try:
        normalized = re.sub(
            r"'([^']*)'(\s*[:,}\]])",
            lambda m: json.dumps(m.group(1)) + m.group(2),
            s,
        )
        return json.loads(normalized)
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. Unquoted keys: {to: "x", message: "y", type: "email"}
    try:
        fixed = re.sub(r"(\w+)\s*:", r'"\1":', s)
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    return None


def _normalize_keys(data: dict) -> dict | None:
    """Map alternate keys to canonical {to, message, type}. Validate type."""
    result = {}
    by_lower = {k.lower(): (k, v) for k, v in data.items() if isinstance(k, str)}

    # to
    for k in _TO_KEYS:
        if k in by_lower:
            _, val = by_lower[k]
            if val and isinstance(val, str):
                result["to"] = val.strip()
                break
    if "to" not in result:
        return None

    # message
    for k in _MESSAGE_KEYS:
        if k in by_lower:
            _, val = by_lower[k]
            result["message"] = str(val).strip() if val else ""
            break
    result.setdefault("message", "")

    # type: must be email or sms
    for k in _TYPE_KEYS:
        if k in by_lower:
            _, val = by_lower[k]
            if val and isinstance(val, str):
                t = val.strip().lower()
                if t in ("email", "sms"):
                    result["type"] = t
                    return result
                break
    return None


def parse_extraction(raw: str) -> dict | None:
    """
    Parse AI extraction output into {to, message, type} or None if invalid.
    Handles: Markdown blocks, alternate keys, single quotes, unquoted keys,
    extra fields, missing message. Fails on: truncation, refusals, missing to/type.
    """
    candidate = _extract_json_candidate(raw)
    if not candidate:
        return None

    parsed = _try_parse_json(candidate)
    if not isinstance(parsed, dict):
        return None

    return _normalize_keys(parsed)
