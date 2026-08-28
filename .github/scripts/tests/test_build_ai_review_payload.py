from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_ai_review_payload import build_payload, main, parse_model_review  # noqa: E402


HEAD_SHA = "a" * 40
DIFF = """\
diff --git a/api-tests/src/test/kotlin/tests/AuthApiTest.kt b/api-tests/src/test/kotlin/tests/AuthApiTest.kt
--- a/api-tests/src/test/kotlin/tests/AuthApiTest.kt
+++ b/api-tests/src/test/kotlin/tests/AuthApiTest.kt
@@ -34,3 +34,3 @@
 context
-old assertion
+new assertion
 context
diff --git a/api-tests/src/test/kotlin/tests/LegacyApiTest.kt b/api-tests/src/test/kotlin/tests/LegacyApiTest.kt
--- a/api-tests/src/test/kotlin/tests/LegacyApiTest.kt
+++ b/api-tests/src/test/kotlin/tests/LegacyApiTest.kt
@@ -20,1 +20,0 @@
-deleted assertion
"""
INFRA_DIFF = """\
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -46,2 +46,3 @@ jobs:
       - name: Test fake API and run API suite
+        continue-on-error: true
         run: |
"""


def published(model_text: str) -> str:
    return "\n".join(
        (
            "## AI PR review",
            "",
            model_text,
            "",
            "<sub>Non-blocking · Model: `claude-sonnet-5`</sub>",
        )
    )


class BuildAiReviewPayloadTest(unittest.TestCase):
    def test_summary_is_always_rendered_for_approve(self) -> None:
        payload = build_payload(
            published(
                "\n".join(
                    (
                        "Verdict: APPROVE",
                        "Summary: Renames tariff variables in one API test.",
                        "Findings:",
                        "- none",
                        "Unverified: none",
                        "Question: none",
                    )
                )
            ),
            DIFF,
            HEAD_SHA,
        )

        self.assertEqual("COMMENT", payload["event"])
        self.assertEqual([], payload["comments"])
        self.assertIn("Summary: Renames tariff variables in one API test.", payload["body"])
        self.assertIn("Findings: none", payload["body"])

    def test_finding_becomes_inline_comment_on_added_line(self) -> None:
        payload = build_payload(
            published(
                "\n".join(
                    (
                        "Verdict: REQUEST CHANGES",
                        "Summary: Weakens the OTP error assertion.",
                        "Findings:",
                        "- Assertion strength `api-tests/src/test/kotlin/tests/AuthApiTest.kt:35` - exact copy check became presence-only; wrong copy now passes; blocker",
                        "Unverified: none",
                        "Question: none",
                    )
                )
            ),
            DIFF,
            HEAD_SHA,
        )

        self.assertIn("Summary: Weakens the OTP error assertion.", payload["body"])
        self.assertIn("Findings: 1 inline comment", payload["body"])
        self.assertNotIn("exact copy check became presence-only", payload["body"])
        self.assertEqual(1, len(payload["comments"]))
        comment = payload["comments"][0]
        self.assertEqual("api-tests/src/test/kotlin/tests/AuthApiTest.kt", comment["path"])
        self.assertEqual(35, comment["line"])
        self.assertEqual("RIGHT", comment["side"])
        self.assertIn("**Assertion strength · blocker**", comment["body"])

    def test_deleted_line_uses_left_side(self) -> None:
        payload = build_payload(
            published(
                "\n".join(
                    (
                        "Verdict: REQUEST CHANGES",
                        "Summary: Deletes a test assertion.",
                        "Findings:",
                        "- Deletion impact `api-tests/src/test/kotlin/tests/LegacyApiTest.kt:20` - assertion was removed; the regression is no longer detected; request changes",
                        "Unverified: none",
                        "Question: none",
                    )
                )
            ),
            DIFF,
            HEAD_SHA,
        )

        self.assertEqual("LEFT", payload["comments"][0]["side"])

    def test_uncommentable_finding_remains_in_summary(self) -> None:
        payload = build_payload(
            published(
                "\n".join(
                    (
                        "Verdict: REQUEST CHANGES",
                        "Summary: Weakens the OTP error assertion.",
                        "Findings:",
                        "- Assertion strength `api-tests/src/test/kotlin/tests/AuthApiTest.kt:999` - exact copy check became presence-only; wrong copy now passes; blocker",
                        "Unverified: none",
                        "Question: none",
                    )
                )
            ),
            DIFF,
            HEAD_SHA,
        )

        self.assertEqual([], payload["comments"])
        self.assertIn("Unplaced findings:", payload["body"])
        self.assertIn("AuthApiTest.kt:999", payload["body"])

    def test_missing_summary_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Summary"):
            parse_model_review(
                "\n".join(
                    (
                        "Verdict: APPROVE",
                        "Findings:",
                        "- none",
                        "Unverified: none",
                        "Question: none",
                    )
                )
            )

    def test_human_review_required_adds_inline_scope_comment(self) -> None:
        payload = build_payload(
            published(
                "\n".join(
                    (
                        "Verdict: HUMAN REVIEW REQUIRED",
                        "Summary: Allows API-suite failures to continue in CI.",
                        "Scope: .github/workflows/ci.yml",
                    )
                )
            ),
            INFRA_DIFF,
            HEAD_SHA,
        )

        self.assertEqual("COMMENT", payload["event"])
        self.assertIn("Verdict: HUMAN REVIEW REQUIRED", payload["body"])
        self.assertIn("Scope: .github/workflows/ci.yml", payload["body"])
        self.assertEqual(1, len(payload["comments"]))
        comment = payload["comments"][0]
        self.assertEqual(".github/workflows/ci.yml", comment["path"])
        self.assertEqual(47, comment["line"])
        self.assertEqual("RIGHT", comment["side"])
        self.assertIn("**Human review required**", comment["body"])
        self.assertIn("`.github/workflows/ci.yml`", comment["body"])

    def test_human_review_without_a_matching_scope_path_stays_summary_only(self) -> None:
        payload = build_payload(
            published(
                "\n".join(
                    (
                        "Verdict: HUMAN REVIEW REQUIRED",
                        "Summary: Changes application behavior.",
                        "Scope: app/",
                    )
                )
            ),
            INFRA_DIFF,
            HEAD_SHA,
        )

        self.assertEqual([], payload["comments"])
        self.assertIn("Scope: app/", payload["body"])

    def test_fallback_is_a_non_blocking_review_without_comments(self) -> None:
        payload = build_payload(
            "## AI PR review unavailable\n\nHuman review is required.",
            DIFF,
            HEAD_SHA,
        )

        self.assertEqual("COMMENT", payload["event"])
        self.assertEqual([], payload["comments"])
        self.assertIn("## AI PR review unavailable", payload["body"])

    def test_build_command_writes_the_payload_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review = root / "review.md"
            diff = root / "review.diff"
            output = root / "payload.json"
            review.write_text(
                published(
                    "\n".join(
                        (
                            "Verdict: APPROVE",
                            "Summary: Renames tariff variables in one API test.",
                            "Findings:",
                            "- none",
                            "Unverified: none",
                            "Question: none",
                        )
                    )
                ),
                encoding="utf-8",
            )
            diff.write_text(DIFF, encoding="utf-8")

            exit_code = main(
                [
                    "build",
                    "--review",
                    str(review),
                    "--diff",
                    str(diff),
                    "--head-sha",
                    HEAD_SHA,
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(0, exit_code)
            self.assertEqual("COMMENT", json.loads(output.read_text(encoding="utf-8"))["event"])


if __name__ == "__main__":
    unittest.main()
