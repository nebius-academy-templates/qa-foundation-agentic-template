#!/usr/bin/env python3

from __future__ import annotations

import json
import re
from dataclasses import dataclass


ERROR_RESULT_SUBTYPES = frozenset(
    {
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    }
)
PUBLIC_REASONS = {
    "authentication": (
        "Provider authentication failed. Verify the `ANTHROPIC_API_KEY` "
        "repository secret."
    ),
    "rate_limit": (
        "The provider rate limit was exceeded. Retry after the limit resets."
    ),
    "overloaded": "The provider reported temporary overload. Retry the workflow.",
    "timeout": "Claude Code execution timed out before completing the review.",
    "unknown": (
        "Claude Code Action failed before producing a complete review. "
        "See the Actions log for the diagnostic category."
    ),
}
STATUS_CATEGORIES = {
    401: "authentication",
    429: "rate_limit",
    529: "overloaded",
}
MESSAGE_CATEGORIES = (
    (
        re.compile(
            r"authentication[_ -]?error|unauthorized|"
            r"invalid.{0,30}(?:api|x-api)[_ -]?key",
            re.IGNORECASE,
        ),
        "authentication",
    ),
    (re.compile(r"rate[_ -]?limit|too many requests", re.IGNORECASE), "rate_limit"),
    (re.compile(r"overload|capacity", re.IGNORECASE), "overloaded"),
    (re.compile(r"timed? out|timeout|deadline exceeded", re.IGNORECASE), "timeout"),
)

ExecutionRecord = dict[str, object]


@dataclass(frozen=True)
class ProviderDiagnostic:
    public_reason: str
    log_line: str


@dataclass(frozen=True)
class ExecutionError:
    subtype: str
    status: int | None
    message: str


def _parse_execution_log(text: str) -> list[ExecutionRecord]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [record for record in value if isinstance(record, dict)]


def _first_string(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return next(
            (
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip()
            ),
            "",
        )
    return ""


def _execution_error(records: list[ExecutionRecord]) -> ExecutionError | None:
    record = next(
        (record for record in reversed(records) if record.get("type") == "result"),
        None,
    )
    if record is None:
        return None

    subtype = record.get("subtype")
    if not isinstance(subtype, str):
        return None
    if subtype == "success":
        if record.get("is_error") is not True:
            return None
        message = _first_string(record.get("result"))
    elif subtype in ERROR_RESULT_SUBTYPES:
        message = _first_string(record.get("errors"))
    else:
        return None

    raw_status = record.get("api_error_status")
    status = (
        raw_status
        if isinstance(raw_status, int) and not isinstance(raw_status, bool)
        else None
    )
    return ExecutionError(subtype=subtype, status=status, message=message)


def _classify(error: ExecutionError | None) -> str:
    if error is None:
        return "unknown"
    if error.status in STATUS_CATEGORIES:
        return STATUS_CATEGORIES[error.status]
    return next(
        (
            category
            for pattern, category in MESSAGE_CATEGORIES
            if pattern.search(error.message)
        ),
        "unknown",
    )


def describe_execution_error(execution_text: str) -> ProviderDiagnostic:
    error = _execution_error(_parse_execution_log(execution_text))
    category = _classify(error)
    subtype = error.subtype if error is not None else "unavailable"
    status = (
        error.status
        if error is not None and error.status is not None
        else "unavailable"
    )

    return ProviderDiagnostic(
        public_reason=PUBLIC_REASONS[category],
        log_line=(
            "Repository code review provider diagnostic: "
            f"category={category} subtype={subtype} status={status}"
        ),
    )
