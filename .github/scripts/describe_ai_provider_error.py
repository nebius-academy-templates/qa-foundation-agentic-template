#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


SECRET_RE = re.compile(
    r"(?:sk-ant-[A-Za-z0-9_-]+|ghs_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderDiagnostic:
    category: str
    public_reason: str
    log_line: str


def _error_fragments(value: object) -> list[str]:
    fragments: list[str] = []

    def visit(node: object, inside_error: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, inside_error)
            return
        if not isinstance(node, dict):
            if inside_error and isinstance(node, str):
                fragments.append(node)
            return

        record_type = str(node.get("type", "")).lower()
        subtype = str(node.get("subtype", "")).lower()
        is_error = (
            inside_error
            or node.get("is_error") is True
            or record_type == "error"
            or "error" in subtype
            or "error" in node
            or "errors" in node
        )
        for child in node.values():
            visit(child, is_error)

    visit(value)
    return fragments


def _parse_execution_log(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def _safe_detail(fragments: list[str]) -> str:
    if not fragments:
        return "no structured provider error was available"
    printable = " ".join(fragments)
    printable = "".join(
        character if not unicodedata.category(character).startswith("C") else " "
        for character in printable
    )
    single_line = " ".join(printable.split())
    redacted = SECRET_RE.sub("[redacted-secret]", single_line)
    if len(redacted) > 500:
        return f"{redacted[:497]}..."
    return redacted


def describe_execution_error(execution_text: str) -> ProviderDiagnostic:
    fragments = _error_fragments(_parse_execution_log(execution_text))
    evidence = " ".join(fragments).lower()

    if re.search(
        r"authentication[_ -]?error|unauthorized|invalid.{0,30}(?:api|x-api)[_ -]?key|\b401\b",
        evidence,
    ):
        category = "authentication"
        public_reason = (
            "Provider authentication failed. Verify the `ANTHROPIC_API_KEY` "
            "repository secret."
        )
    elif re.search(r"rate[_ -]?limit|too many requests|\b429\b", evidence):
        category = "rate_limit"
        public_reason = (
            "The provider rate limit was exceeded. Retry after the limit resets."
        )
    elif re.search(r"overload|capacity|\b529\b", evidence):
        category = "overloaded"
        public_reason = "The provider reported temporary overload. Retry the workflow."
    elif re.search(r"timed? out|timeout|deadline exceeded", evidence):
        category = "timeout"
        public_reason = "Claude Code execution timed out before completing the review."
    else:
        category = "unknown"
        public_reason = (
            "Claude Code Action failed before producing a complete review. "
            "See the Actions log for the diagnostic category."
        )

    return ProviderDiagnostic(
        category=category,
        public_reason=public_reason,
        log_line=(
            "Repository code review provider diagnostic: "
            f"category={category} detail={_safe_detail(fragments)}"
        ),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a safe provider diagnostic from Claude Code execution output."
    )
    parser.add_argument("--execution-file", default="")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    execution_text = ""
    if args.execution_file:
        try:
            execution_text = Path(args.execution_file).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            pass

    diagnostic = describe_execution_error(execution_text)
    try:
        output_temp = args.output.with_name(f".{args.output.name}.tmp")
        output_temp.write_text(diagnostic.public_reason + "\n", encoding="utf-8")
        output_temp.replace(args.output)
    except OSError as error:
        print(f"Provider diagnostic output error: {error}", file=sys.stderr)
        return 1

    print(diagnostic.log_line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
