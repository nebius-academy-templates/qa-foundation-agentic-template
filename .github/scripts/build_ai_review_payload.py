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


CHECK_NAMES = (
    "Claim accuracy",
    "Assertion strength",
    "Test reliability",
    "Scope alignment",
    "Deletion impact",
)
HUMAN_VERIFICATION = "Human verification"
FINDING_RE = re.compile(
    r"^- (?P<check>"
    + "|".join(re.escape(name) for name in (*CHECK_NAMES, HUMAN_VERIFICATION))
    + r") `(?P<path>[^`]+):(?P<line>[1-9][0-9]*)` - "
    r"(?P<issue>.+); (?P<impact>.+); "
    r"(?P<severity>blocker|request changes|human review required)$"
)
HUNK_RE = re.compile(
    r"^@@ -(?P<old>[0-9]+)(?:,[0-9]+)? "
    r"\+(?P<new>[0-9]+)(?:,[0-9]+)? @@"
)
SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    line: int
    issue: str
    impact: str
    severity: str
    raw: str


@dataclass(frozen=True)
class ModelReview:
    verdict: str
    summary: str
    findings: tuple[Finding, ...] = ()
    unverified: str | None = None
    question: str | None = None


@dataclass(frozen=True)
class FinalReview:
    model_review: ModelReview | None
    footer: str | None
    fallback_body: str | None


def _nonblank_lines(text: str) -> list[str]:
    return [line.rstrip("\r") for line in text.splitlines() if line.strip()]


def parse_model_review(text: str) -> ModelReview:
    lines = _nonblank_lines(text)
    if not lines or any("```" in line for line in lines):
        raise ValueError("review is empty or contains a code fence")

    verdict_prefix = "Verdict: "
    summary_prefix = "Summary: "
    if not lines[0].startswith(verdict_prefix):
        raise ValueError("review does not start with Verdict")

    verdict = lines[0][len(verdict_prefix) :]
    if verdict not in {"APPROVE", "REQUEST CHANGES", "HUMAN REVIEW REQUIRED"}:
        raise ValueError(f"unsupported verdict: {verdict}")
    if len(lines) < 2 or not lines[1].startswith(summary_prefix) or not lines[1][len(summary_prefix) :].strip():
        raise ValueError("Summary is missing or empty")
    if not 6 <= len(lines) <= 8:
        raise ValueError("in-scope output has the wrong number of lines")
    if lines[2] != "Findings:":
        raise ValueError("Findings heading is missing")
    if not re.fullmatch(r"Unverified: (none|.+ - missing evidence)", lines[-2]):
        raise ValueError("Unverified has the wrong format")
    if not re.fullmatch(r"Question: (none|.+)", lines[-1]):
        raise ValueError("Question has the wrong format")

    finding_lines = lines[3:-2]
    if finding_lines == ["- none"]:
        if verdict != "APPROVE":
            raise ValueError("a no-finding review must recommend APPROVE")
        findings: tuple[Finding, ...] = ()
    else:
        parsed_findings = []
        for line in finding_lines:
            match = FINDING_RE.fullmatch(line)
            if match is None:
                raise ValueError(f"finding has the wrong format: {line}")
            finding = Finding(
                check=match.group("check"),
                path=match.group("path"),
                line=int(match.group("line")),
                issue=match.group("issue"),
                impact=match.group("impact"),
                severity=match.group("severity"),
                raw=line,
            )
            if (finding.check == HUMAN_VERIFICATION) != (
                finding.severity == "human review required"
            ):
                raise ValueError("Human verification requires human review required severity")
            parsed_findings.append(finding)
        findings = tuple(parsed_findings)
        has_human_verification = any(
            finding.check == HUMAN_VERIFICATION for finding in findings
        )
        if verdict == "APPROVE":
            raise ValueError("a review with findings cannot recommend APPROVE")
        if verdict == "REQUEST CHANGES" and has_human_verification:
            raise ValueError("Human verification requires HUMAN REVIEW REQUIRED")
        if verdict == "HUMAN REVIEW REQUIRED" and not has_human_verification:
            raise ValueError("HUMAN REVIEW REQUIRED needs a Human verification finding")

    return ModelReview(
        verdict=verdict,
        summary=lines[1][len(summary_prefix) :],
        findings=findings,
        unverified=lines[-2][len("Unverified: ") :],
        question=lines[-1][len("Question: ") :],
    )


def parse_final_review(text: str) -> FinalReview:
    stripped = text.strip()
    if stripped.startswith("## AI PR review unavailable"):
        return FinalReview(model_review=None, footer=None, fallback_body=stripped)
    if not stripped.startswith("## AI PR review\n"):
        raise ValueError("published review has the wrong heading")

    lines = stripped.splitlines()
    footer_index = next(
        (index for index in range(len(lines) - 1, 0, -1) if lines[index].startswith("<sub>")),
        None,
    )
    if footer_index is None:
        raise ValueError("published review footer is missing")

    model_text = "\n".join(lines[1:footer_index]).strip()
    return FinalReview(
        model_review=parse_model_review(model_text),
        footer=lines[footer_index],
        fallback_body=None,
    )


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


def index_diff(
    diff_text: str,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
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


def _safe_repo_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return not candidate.is_absolute() and ".." not in candidate.parts and "\\" not in path


def _locate_finding(
    finding: Finding,
    right_lines: set[tuple[str, int]],
    left_lines: set[tuple[str, int]],
) -> str | None:
    if not _safe_repo_path(finding.path):
        return None
    location = (finding.path, finding.line)
    if location in right_lines:
        return "RIGHT"
    if location in left_lines:
        return "LEFT"
    return None


def _inline_body(finding: Finding, review_hash: str) -> str:
    return "\n".join(
        (
            f"<!-- ai-pr-review-finding:v1 review={review_hash} -->",
            f"**{finding.check} · {finding.severity}**",
            "",
            finding.issue,
            "",
            f"Impact: {finding.impact}",
        )
    )


def _summary_body(
    review: ModelReview,
    footer: str,
    head_sha: str,
    review_hash: str,
    placed_count: int,
    unplaced: list[Finding],
) -> str:
    lines = [
        f"<!-- ai-pr-review:v2 head={head_sha} review={review_hash} -->",
        "## AI PR review",
        "",
        f"Verdict: {review.verdict}",
        f"Summary: {review.summary}",
    ]
    if not review.findings:
        lines.append("Findings: none")
    elif unplaced:
        lines.append(
            f"Findings: {placed_count} inline; {len(unplaced)} could not be attached and are listed below"
        )
    else:
        suffix = "comment" if placed_count == 1 else "comments"
        lines.append(f"Findings: {placed_count} inline {suffix}")
    lines.extend(
        (
            f"Unverified: {review.unverified}",
            f"Question: {review.question}",
        )
    )
    if unplaced:
        lines.extend(("", "Unplaced findings:", *(finding.raw for finding in unplaced)))
    lines.extend(("", footer))
    return "\n".join(lines)


def build_payload(review_text: str, diff_text: str, head_sha: str) -> dict[str, object]:
    if SHA_RE.fullmatch(head_sha) is None:
        raise ValueError("head SHA has the wrong format")

    final_review = parse_final_review(review_text)
    review_hash = hashlib.sha256(review_text.encode("utf-8")).hexdigest()[:16]
    if final_review.fallback_body is not None:
        body = "\n".join(
            (
                f"<!-- ai-pr-review:v2 head={head_sha} review={review_hash} -->",
                final_review.fallback_body,
            )
        )
        return {"commit_id": head_sha, "body": body, "event": "COMMENT", "comments": []}

    assert final_review.model_review is not None
    assert final_review.footer is not None
    right_lines, left_lines = index_diff(diff_text)
    comments = []
    unplaced = []
    for finding in final_review.model_review.findings:
        side = _locate_finding(finding, right_lines, left_lines)
        if side is None:
            unplaced.append(finding)
            continue
        comments.append(
            {
                "path": finding.path,
                "line": finding.line,
                "side": side,
                "body": _inline_body(finding, review_hash),
            }
        )

    return {
        "commit_id": head_sha,
        "body": _summary_body(
            final_review.model_review,
            final_review.footer,
            head_sha,
            review_hash,
            len(comments),
            unplaced,
        ),
        "event": "COMMENT",
        "comments": comments,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate AI review output and build a GitHub review payload.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate raw model review text.")
    validate.add_argument("--model-review", required=True, type=Path)

    build = subparsers.add_parser("build", help="Build a GitHub pull request review payload.")
    build.add_argument("--review", required=True, type=Path)
    build.add_argument("--diff", required=True, type=Path)
    build.add_argument("--head-sha", required=True)
    build.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "validate":
            parse_model_review(args.model_review.read_text(encoding="utf-8"))
            return 0

        payload = build_payload(
            args.review.read_text(encoding="utf-8"),
            args.diff.read_text(encoding="utf-8"),
            args.head_sha,
        )
        output_temp = args.output.with_name(f".{args.output.name}.tmp")
        output_temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        output_temp.replace(args.output)
        return 0
    except (OSError, ValueError) as error:
        print(f"AI review payload error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
