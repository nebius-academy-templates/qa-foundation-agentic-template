#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from describe_ai_provider_error import describe_execution_error


HUMAN_VERIFICATION = "Human verification"
RECOMMENDATION_MAX_LENGTH = 1000
SUMMARY_ITEM_MAX_LENGTH = 250
FINDING_TEXT_MAX_LENGTH = 250
FINDING_PATH_MAX_LENGTH = 500
QUESTION_MAX_LENGTH = 500
UNVERIFIED_MAX_LENGTH = 1000
MAX_FINDINGS = 10
MAX_SUMMARY_ITEMS = 10
CHECK_SEVERITIES = {
    "Correctness": "blocker",
    "Security": "blocker",
    "Claim accuracy": "blocker",
    "Assertion strength": "blocker",
    "Test reliability": "request changes",
    "Scope alignment": "request changes",
    "Deletion impact": "request changes",
    "Repository policy": "blocker",
    HUMAN_VERIFICATION: "human review required",
}
SUPPORTED_CHECKS = frozenset(CHECK_SEVERITIES)
HUNK_RE = re.compile(
    r"^@@ -(?P<old>[0-9]+)(?:,[0-9]+)? "
    r"\+(?P<new>[0-9]+)(?:,[0-9]+)? @@"
)
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
SECRET_LIKE_RE = re.compile(r"(?:sk-ant-|ghs_|github_pat_)", re.IGNORECASE)
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\u2600-\u27BF"
    "]|\uFE0F|\u20E3"
)
STRUCTURED_KEYS = {
    "complete",
    "summary",
    "recommendation",
    "findings",
    "unverified",
    "question",
}
FINDING_KEYS = {
    "check",
    "path",
    "line",
    "issue",
    "impact",
    "required_change",
}


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    line: int
    issue: str
    impact: str
    required_change: str
    severity: str


@dataclass(frozen=True)
class ModelReview:
    verdict: str
    summary: tuple[str, ...]
    recommendation: str
    findings: tuple[Finding, ...]
    unverified: str
    question: str


class ReviewValidationError(ValueError):
    """A validation failure whose message is safe to expose in workflow logs."""


class IncompleteReviewError(ReviewValidationError):
    """The agent returned a valid result before completing repository analysis."""


def public_validation_error(error: ValueError) -> str:
    if isinstance(error, ReviewValidationError):
        return str(error)
    return "validation failed"


def _structured_line(
    value: object,
    field: str,
    *,
    allow_semicolon: bool = True,
    max_length: int = 1000,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewValidationError(f"{field} must be a non-empty string")
    if "\r" in value or "\n" in value:
        raise ReviewValidationError(f"{field} must be one line")
    if len(value) > max_length:
        raise ReviewValidationError(f"{field} is too long")
    if SECRET_LIKE_RE.search(value):
        raise ReviewValidationError(f"{field} contains secret-like content")
    if EMOJI_RE.search(value):
        raise ReviewValidationError(f"{field} contains emoji")
    if not allow_semicolon and ";" in value:
        raise ReviewValidationError(f"{field} must not contain a semicolon")
    return value.strip()


def _safe_repo_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return not candidate.is_absolute() and ".." not in candidate.parts and "\\" not in path


def derive_verdict(findings: tuple[Finding, ...]) -> str:
    if not findings:
        return "Approve"
    if any(finding.check == HUMAN_VERIFICATION for finding in findings):
        return "Human review required"
    return "Request changes"


def parse_structured_review(payload: object) -> ModelReview:
    if not isinstance(payload, dict) or set(payload) != STRUCTURED_KEYS:
        raise ReviewValidationError("review has the wrong fields")
    if not isinstance(payload["complete"], bool):
        raise ReviewValidationError("review completeness must be a boolean")
    if not payload["complete"]:
        raise IncompleteReviewError("review is incomplete")

    raw_summary = payload["summary"]
    if (
        not isinstance(raw_summary, list)
        or not raw_summary
        or len(raw_summary) > MAX_SUMMARY_ITEMS
    ):
        raise ReviewValidationError(
            f"summary must contain between 1 and {MAX_SUMMARY_ITEMS} items"
        )
    summary = tuple(
        _structured_line(item, "summary item", max_length=SUMMARY_ITEM_MAX_LENGTH)
        for item in raw_summary
    )
    recommendation = _structured_line(
        payload["recommendation"],
        "recommendation",
        max_length=RECOMMENDATION_MAX_LENGTH,
    )
    unverified = _structured_line(
        payload["unverified"],
        "unverified",
        max_length=UNVERIFIED_MAX_LENGTH,
    )
    if unverified != "none" and re.fullmatch(r".+ - missing evidence", unverified) is None:
        raise ReviewValidationError("unverified has the wrong format")
    question = _structured_line(
        payload["question"],
        "question",
        max_length=QUESTION_MAX_LENGTH,
    )

    raw_findings = payload["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
        raise ReviewValidationError(
            f"findings must be an array with at most {MAX_FINDINGS} items"
        )

    findings = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict) or set(raw_finding) != FINDING_KEYS:
            raise ReviewValidationError("finding has the wrong fields")
        check = _structured_line(raw_finding["check"], "finding check")
        if check not in SUPPORTED_CHECKS:
            raise ReviewValidationError("finding check is unsupported")
        path = _structured_line(
            raw_finding["path"],
            "finding path",
            max_length=FINDING_PATH_MAX_LENGTH,
        )
        if "`" in path or not _safe_repo_path(path):
            raise ReviewValidationError("finding path is unsafe")
        line = raw_finding["line"]
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ReviewValidationError(
                "finding line must be a positive integer"
            )
        findings.append(
            Finding(
                check=check,
                path=path,
                line=line,
                issue=_structured_line(
                    raw_finding["issue"],
                    "finding issue",
                    allow_semicolon=False,
                    max_length=FINDING_TEXT_MAX_LENGTH,
                ),
                impact=_structured_line(
                    raw_finding["impact"],
                    "finding impact",
                    allow_semicolon=False,
                    max_length=FINDING_TEXT_MAX_LENGTH,
                ),
                required_change=_structured_line(
                    raw_finding["required_change"],
                    "finding required change",
                    allow_semicolon=False,
                    max_length=FINDING_TEXT_MAX_LENGTH,
                ),
                severity=CHECK_SEVERITIES[check],
            )
        )

    parsed_findings = tuple(findings)
    return ModelReview(
        verdict=derive_verdict(parsed_findings),
        summary=summary,
        recommendation=recommendation,
        findings=parsed_findings,
        unverified=unverified,
        question=question,
    )


def render_finding(finding: Finding) -> str:
    return (
        f"- {finding.check} `{finding.path}:{finding.line}` - {finding.issue}; "
        f"{finding.impact}; {finding.required_change}; {finding.severity}"
    )


def _optional_review_lines(review: ModelReview) -> tuple[str, ...]:
    lines = []
    if review.unverified != "none":
        lines.append(f"Unverified: {review.unverified}")
    if review.question != "none":
        lines.append(f"Question: {review.question}")
    return tuple(lines)


def _review_header(review: ModelReview) -> tuple[str, ...]:
    return (
        f"Review recommendation: {review.verdict}",
        "",
        review.recommendation,
        "",
        "Summary:",
        *(f"- {item}" for item in review.summary),
    )


def render_model_review(review: ModelReview) -> str:
    finding_lines = tuple(render_finding(finding) for finding in review.findings)
    lines = [
        *_review_header(review),
        "",
        "Findings:",
        *(finding_lines or ("- none",)),
    ]
    optional_lines = _optional_review_lines(review)
    if optional_lines:
        lines.extend(("", *optional_lines))
    return "\n".join(lines)


def render_published_review(review: ModelReview) -> str:
    return "\n".join(("## Repository code review", "", render_model_review(review)))


def shell_quote_json_schema(schema_text: str) -> str:
    schema = json.loads(schema_text)
    if not isinstance(schema, dict):
        raise ValueError("review schema must be a JSON object")
    compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return shlex.quote(compact)


def _parse_diff_path(raw_path: str) -> str | None:
    value = raw_path.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        parsed = shlex.split(value)
        if len(parsed) != 1:
            raise ValueError(f"cannot parse diff path: {raw_path}")
        value = parsed[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def index_diff(diff_text: str) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    right_lines: set[tuple[str, int]] = set()
    left_lines: set[tuple[str, int]] = set()
    old_path: str | None = None
    new_path: str | None = None
    old_line = 0
    new_line = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            old_path = None
            new_path = None
            in_hunk = False
            continue
        if line.startswith("--- "):
            old_path = _parse_diff_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _parse_diff_path(line[4:])
            continue

        hunk = HUNK_RE.match(line)
        if hunk is not None:
            old_line = int(hunk.group("old"))
            new_line = int(hunk.group("new"))
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\ No newline at end of file"):
            continue

        prefix = line[:1]
        if prefix == "+":
            if new_path is not None:
                right_lines.add((new_path, new_line))
            new_line += 1
        elif prefix == "-":
            if old_path is not None:
                left_lines.add((old_path, old_line))
            old_line += 1
        elif prefix == " ":
            if new_path is not None:
                right_lines.add((new_path, new_line))
            if old_path is not None:
                left_lines.add((old_path, old_line))
            old_line += 1
            new_line += 1
        else:
            in_hunk = False

    return right_lines, left_lines


def _locate_finding(
    finding: Finding,
    right_lines: set[tuple[str, int]],
    left_lines: set[tuple[str, int]],
) -> str | None:
    location = (finding.path, finding.line)
    if location in right_lines:
        return "RIGHT"
    if location in left_lines:
        return "LEFT"
    return None


def _inline_body(findings: list[Finding], review_hash: str) -> str:
    lines = [f"<!-- repository-code-review-finding:v1 review={review_hash} -->"]
    for index, finding in enumerate(findings):
        if index:
            lines.extend(("", "---"))
        lines.extend(
            (
                "",
                f"**{finding.check} · {finding.severity}**",
                "",
                finding.issue,
                "",
                f"Consequence: {finding.impact}",
                "",
                f"Required change: {finding.required_change}",
            )
        )
    return "\n".join(lines)


def _summary_body(
    review: ModelReview,
    head_sha: str,
    review_hash: str,
    placed_finding_count: int,
    inline_comment_count: int,
    unplaced: list[Finding],
) -> str:
    lines = [
        f"<!-- repository-code-review:v2 head={head_sha} review={review_hash} -->",
        "## Repository code review",
        "",
        *_review_header(review),
        "",
    ]
    if not review.findings:
        lines.append("Findings: none")
    elif unplaced:
        finding_word = "finding" if placed_finding_count == 1 else "findings"
        comment_word = "comment" if inline_comment_count == 1 else "comments"
        lines.append(
            f"Findings: {placed_finding_count} {finding_word} in "
            f"{inline_comment_count} inline {comment_word}; {len(unplaced)} could not "
            "be attached and are listed below"
        )
    elif placed_finding_count != inline_comment_count:
        finding_word = "finding" if placed_finding_count == 1 else "findings"
        comment_word = "comment" if inline_comment_count == 1 else "comments"
        lines.append(
            f"Findings: {placed_finding_count} {finding_word} in "
            f"{inline_comment_count} inline {comment_word}"
        )
    else:
        suffix = "comment" if inline_comment_count == 1 else "comments"
        lines.append(f"Findings: {inline_comment_count} inline {suffix}")
    optional_lines = _optional_review_lines(review)
    if optional_lines:
        lines.extend(("", *optional_lines))
    if unplaced:
        lines.extend(("", "Unplaced findings:", *(render_finding(item) for item in unplaced)))
    return "\n".join(lines)


def _review_hash(review: ModelReview) -> str:
    return hashlib.sha256(render_model_review(review).encode("utf-8")).hexdigest()[:16]


def build_payload(review: ModelReview, diff_text: str, head_sha: str) -> dict[str, object]:
    if SHA_RE.fullmatch(head_sha) is None:
        raise ReviewValidationError("head SHA has the wrong format")

    review_hash = _review_hash(review)
    right_lines, left_lines = index_diff(diff_text)
    comment_groups: dict[tuple[str, int, str], list[Finding]] = {}
    unplaced = []
    for finding in review.findings:
        side = _locate_finding(finding, right_lines, left_lines)
        if side is None:
            unplaced.append(finding)
            continue
        key = (finding.path, finding.line, side)
        comment_groups.setdefault(key, []).append(finding)

    comments = [
        {
            "path": path,
            "line": line,
            "side": side,
            "body": _inline_body(findings, review_hash),
        }
        for (path, line, side), findings in comment_groups.items()
    ]
    return {
        "commit_id": head_sha,
        "body": _summary_body(
            review,
            head_sha,
            review_hash,
            sum(len(findings) for findings in comment_groups.values()),
            len(comments),
            unplaced,
        ),
        "event": "COMMENT",
        "comments": comments,
    }


def _fallback_report(reason: str) -> str:
    return "\n".join(
        (
            "## Repository code review unavailable",
            "",
            reason,
            "",
            "No partial review was published. Human review is required.",
        )
    )


def build_fallback_payload(report: str, head_sha: str) -> dict[str, object]:
    stripped = report.strip()
    if not stripped.startswith("## Repository code review unavailable"):
        raise ReviewValidationError("fallback report has the wrong heading")
    report_hash = hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]
    safe_head = head_sha if SHA_RE.fullmatch(head_sha) else "unavailable"
    body = "\n".join(
        (
            f"<!-- repository-code-review:v2 head={safe_head} review={report_hash} -->",
            stripped,
        )
    )
    payload: dict[str, object] = {
        "body": body,
        "event": "COMMENT",
        "comments": [],
    }
    if safe_head != "unavailable":
        payload["commit_id"] = head_sha
    return payload


def _invalid_structured_output(diagnostic_detail: str) -> tuple[str, str]:
    diagnostic = (
        "Repository code review validator diagnostic: "
        f"{diagnostic_detail}"
    )
    report = _fallback_report(
        "The provider response did not match the required review format. "
        "See the Actions log for the validator diagnostic."
    )
    return diagnostic, report


def finalize_review(
    *,
    agent_expected: bool | None,
    agent_outcome: str,
    structured_output: str,
    execution_text: str,
    existing_report: str,
    diff_text: str,
    head_sha: str,
) -> tuple[str, dict[str, object], str | None]:
    diagnostic: str | None = None
    review: ModelReview | None = None
    if agent_expected is None:
        diagnostic = (
            "Repository code review workflow diagnostic: "
            "review context status is unavailable"
        )
        report = _fallback_report(
            "Review context preparation did not complete, so the repository "
            "review agent could not start."
        )
    elif not agent_expected and existing_report.strip().startswith(
        "## Repository code review unavailable"
    ):
        report = existing_report.strip()
    elif not agent_expected:
        diagnostic = (
            "Repository code review workflow diagnostic: "
            "agent execution was disabled without a prepared fallback report"
        )
        report = _fallback_report(
            "The workflow disabled agent execution without recording a reason."
        )
    elif agent_outcome == "failure":
        provider = describe_execution_error(execution_text)
        diagnostic = provider.log_line
        report = _fallback_report(provider.public_reason)
    elif agent_outcome != "success":
        safe_outcome = (
            agent_outcome
            if agent_outcome in {"skipped", "cancelled"}
            else "unavailable"
        )
        diagnostic = (
            "Repository code review workflow diagnostic: "
            f"agent step outcome={safe_outcome}"
        )
        report = _fallback_report(
            "The repository review agent did not run to completion because the "
            f"workflow reported the step as {safe_outcome}."
        )
    else:
        try:
            review = parse_structured_review(json.loads(structured_output))
            report = render_published_review(review)
        except json.JSONDecodeError:
            diagnostic, report = _invalid_structured_output(
                "structured output is not valid JSON"
            )
        except IncompleteReviewError as error:
            diagnostic = (
                "Repository code review completion diagnostic: "
                f"{public_validation_error(error)}"
            )
            report = _fallback_report(
                "The agent did not complete all required repository-reading "
                "passes, so no review was published."
            )
        except ValueError as error:
            diagnostic, report = _invalid_structured_output(
                public_validation_error(error)
            )

    if review is None:
        payload = build_fallback_payload(report, head_sha)
    else:
        try:
            payload = build_payload(review, diff_text, head_sha)
        except ValueError as error:
            diagnostic = (
                "Repository code review payload diagnostic: "
                f"{public_validation_error(error)}"
            )
            report = _fallback_report("No complete review payload was produced.")
            payload = build_fallback_payload(report, head_sha)
    return report, payload, diagnostic


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _parse_agent_expected(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a structured repository review and build its GitHub payload."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    quote_schema = subparsers.add_parser(
        "quote-schema", help="Quote a JSON schema as one safe shell argument."
    )
    quote_schema.add_argument("--schema", required=True, type=Path)

    finalize = subparsers.add_parser(
        "finalize",
        help="Render or fall back, build the review payload, and append the summary.",
    )
    finalize.add_argument("--agent-expected", default="")
    finalize.add_argument("--agent-outcome", default="")
    finalize.add_argument("--structured-json", default="")
    finalize.add_argument("--execution-file", default="")
    finalize.add_argument("--existing-report", required=True, type=Path)
    finalize.add_argument("--diff", required=True, type=Path)
    finalize.add_argument("--head-sha", required=True)
    finalize.add_argument("--report-output", required=True, type=Path)
    finalize.add_argument("--payload-output", required=True, type=Path)
    finalize.add_argument("--summary-output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "quote-schema":
            quoted = shell_quote_json_schema(args.schema.read_text(encoding="utf-8"))
            print(f"shell={quoted}")
            return 0

        execution_text = ""
        if args.execution_file:
            try:
                execution_text = Path(args.execution_file).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                pass
        existing_report = ""
        if args.existing_report.is_file():
            existing_report = args.existing_report.read_text(encoding="utf-8")
        agent_expected = _parse_agent_expected(args.agent_expected)
        try:
            diff_text = args.diff.read_text(encoding="utf-8")
        except OSError:
            diff_text = ""
            agent_expected = None
        report, payload, diagnostic = finalize_review(
            agent_expected=agent_expected,
            agent_outcome=args.agent_outcome,
            structured_output=args.structured_json,
            execution_text=execution_text,
            existing_report=existing_report,
            diff_text=diff_text,
            head_sha=args.head_sha,
        )
        _write_atomic(args.report_output, report.rstrip() + "\n")
        _write_atomic(
            args.payload_output,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        with args.summary_output.open("a", encoding="utf-8") as summary:
            summary.write(report.rstrip() + "\n")
        if diagnostic:
            print(diagnostic, file=sys.stderr)
        return 0
    except (OSError, ValueError) as error:
        print(f"Repository code review payload error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
