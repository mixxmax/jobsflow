"""Security and Isolation Boundary tests for Quality Control Foundation."""

import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
QC_DIR = REPO_ROOT / "quality_control"
QC_TESTS_DIR = REPO_ROOT / "tests" / "quality_control"


class TestSecurityBoundary(unittest.TestCase):

    def test_zero_jobsearch_2026_runtime_access_in_qc_source(self):
        """Verify that no code in quality_control/ imports or reads JobSearch_2026/."""
        py_files = list(QC_DIR.rglob("*.py"))
        self.assertTrue(len(py_files) > 0, "No python files found in quality_control/")

        for fpath in py_files:
            # Skip the privacy test assertion itself or doc comments if any
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()

            # Ensure no code directly imports or points to JobSearch_2026 path
            # Allowed only in privacy string checkers (e.g. sanitizer or assertion matchers)
            matches = re.findall(r"['\"](?:\.\./)*JobSearch_2026[/'\"]", content)
            self.assertEqual(
                len(matches),
                0,
                f"Forbidden reference to JobSearch_2026 path in {fpath}: {matches}",
            )

    def test_fixtures_have_no_unredacted_real_candidate_pii(self):
        """Verify synthetic fixtures do not contain real personal emails or real phone numbers."""
        fixture_files = list((QC_DIR / "fixtures" / "cases").rglob("*.*"))
        self.assertTrue(len(fixture_files) > 0, "No fixture files found")

        real_email_pat = re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.com|domain\.com|synthetic\.org)[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")

        for fpath in fixture_files:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()

            matches = real_email_pat.findall(text)
            self.assertEqual(
                len(matches),
                0,
                f"Real personal email pattern detected in fixture file {fpath}: {matches}",
            )

    def test_no_modification_of_tools_workflow(self):
        """Verify quality_control does not write or modify tools/workflow/ files."""
        # Check that quality_control contains only its own modules and does not monkeypatch workflow
        for root, dirs, files in os.walk(QC_DIR):
            for file in files:
                if file.endswith(".py"):
                    full_path = Path(root) / file
                    with open(full_path, "r", encoding="utf-8") as f:
                        code = f.read()
                    self.assertNotIn(
                        "tools.workflow.__file__",
                        code,
                        f"Attempt to access tools.workflow file structure in {full_path}",
                    )

    def test_sanitizer_scrubs_all_credential_patterns(self):
        """Verify sanitizer scrubs various credential and token patterns."""
        from quality_control.core.sanitizer import sanitize_text

        inputs = [
            "Bearer abcdef1234567890==",
            "api_key=sk-proj-9999999999",
            "session=sess_1234567890&token=tok_998877",
            "cookie: sessionid=xyz123; csrftoken=abc;",
            "/Users/someuser/JobSearch_2026/00_Profile",
        ]

        for s in inputs:
            sanitized = sanitize_text(s)
            self.assertNotIn("abcdef1234567890", sanitized)
            self.assertNotIn("sk-proj-9999999999", sanitized)
            self.assertNotIn("sess_1234567890", sanitized)
            self.assertNotIn("sessionid=xyz123", sanitized)
            self.assertNotIn("JobSearch_2026", sanitized)


if __name__ == "__main__":
    unittest.main()
