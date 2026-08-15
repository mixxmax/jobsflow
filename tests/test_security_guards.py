import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SCRIPT = REPO_ROOT / "tools" / "security_guards.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))
import security_guards  # noqa: E402  (imported for its allowlist constants)


def run_guards(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "tools" / "security_guards.py")],
        capture_output=True,
        text=True,
    )


class GuardRepoFixture(unittest.TestCase):
    """Builds a minimal repo tree the guards pass on, then breaks one thing per test.

    The guard script resolves the repo root from its own location, so each test
    copies it into a temp tree and runs it as a subprocess - the same way CI
    invokes it - asserting on real exit codes and messages.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        (self.root / "tools").mkdir()
        shutil.copy(GUARD_SCRIPT, self.root / "tools" / "security_guards.py")

        self.settings = self.root / ".claude" / "settings.json"
        self.settings.parent.mkdir()
        self.write_settings(sorted(security_guards.ALLOWED_PERMISSIONS))

        self.gitignore = self.root / ".gitignore"
        self.write_gitignore(security_guards.REQUIRED_IGNORE_RULES)

        self.manifest = self.root / ".agents" / "skills" / "example-search" / "cli" / "package.json"
        self.manifest.parent.mkdir(parents=True)
        self.write_manifest({"name": "example-cli", "scripts": {"start": "bun run src/cli.ts"}})
        for relpath, required_tokens in security_guards.PUBLIC_TEMPLATE_REQUIREMENTS.items():
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(required_tokens) + "\n", encoding="utf-8")

    def write_settings(self, allow):
        self.settings.write_text(json.dumps({"permissions": {"allow": list(allow)}}))

    def write_gitignore(self, rules):
        self.gitignore.write_text("\n".join(rules) + "\n")

    def write_manifest(self, data, path=None):
        (path or self.manifest).write_text(json.dumps(data))


class CleanTreeTests(GuardRepoFixture):
    def test_clean_tree_passes(self):
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("security_guards: OK", result.stdout)


class PermissionGuardTests(GuardRepoFixture):
    def test_wildcard_bash_permission_fails(self):
        self.write_settings(sorted(security_guards.ALLOWED_PERMISSIONS) + ["Bash(*)"])
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in the reviewed allowlist", result.stdout)
        self.assertIn("Bash(*)", result.stdout)

    def test_network_fetch_permission_fails(self):
        self.write_settings(sorted(security_guards.ALLOWED_PERMISSIONS) + ["Bash(curl:*)"])
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in the reviewed allowlist", result.stdout)

    def test_dropped_allowlisted_permission_still_passes(self):
        # Removing a shipped permission narrows exposure; the guard only
        # rejects additions, it must not force entries to exist.
        allow = sorted(security_guards.ALLOWED_PERMISSIONS)[:-1]
        self.write_settings(allow)
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_settings_json_fails(self):
        self.settings.write_text("{not json")
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.stdout)


class GitignoreGuardTests(GuardRepoFixture):
    def test_each_missing_personal_data_rule_fails(self):
        for rule in security_guards.REQUIRED_IGNORE_RULES:
            with self.subTest(rule=rule):
                remaining = [r for r in security_guards.REQUIRED_IGNORE_RULES if r != rule]
                self.write_gitignore(remaining)
                result = run_guards(self.root)
                self.assertEqual(result.returncode, 1)
                self.assertIn("required personal-data rule missing", result.stdout)
                self.assertIn(rule, result.stdout)
        self.write_gitignore(security_guards.REQUIRED_IGNORE_RULES)

    def test_extra_rules_are_allowed(self):
        self.write_gitignore(list(security_guards.REQUIRED_IGNORE_RULES) + ["*.bak", "scratch/"])
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ManifestGuardTests(GuardRepoFixture):
    def test_each_lifecycle_script_fails(self):
        for script in sorted(security_guards.FORBIDDEN_SCRIPTS):
            with self.subTest(script=script):
                # The guard flags the script KEY; the value is never inspected,
                # so it must stay benign: attack-shaped values (curl-pipe-to-sh
                # etc.) written to disk trip AV heuristics - Windows Defender
                # quarantines the fixture mid-test and the suite goes flaky.
                self.write_manifest(
                    {"name": "example-cli", "scripts": {script: "echo test"}}
                )
                result = run_guards(self.root)
                self.assertEqual(result.returncode, 1)
                self.assertIn("lifecycle script", result.stdout)
                self.assertIn(script, result.stdout)
        self.write_manifest({"name": "example-cli", "scripts": {}})

    def test_trusted_dependencies_fails(self):
        self.write_manifest({"name": "example-cli", "trustedDependencies": ["left-pad"]})
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("trustedDependencies", result.stdout)

    def test_benign_scripts_pass(self):
        self.write_manifest(
            {"name": "example-cli", "scripts": {"start": "bun run src/cli.ts", "test": "bun test", "typecheck": "tsc --noEmit"}}
        )
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_node_modules_manifests_are_ignored(self):
        # Installed dependencies are not repo-tracked code; a hostile manifest
        # inside node_modules must not fail the guard (and bun blocks its
        # lifecycle scripts anyway).
        nm = self.manifest.parent / "node_modules" / "some-dep" / "package.json"
        nm.parent.mkdir(parents=True)
        self.write_manifest({"name": "some-dep", "scripts": {"postinstall": "echo test"}}, path=nm)
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_manifests_at_all_fails(self):
        self.manifest.unlink()
        result = run_guards(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no package.json files found", result.stdout)


class PublicTemplateGuardTests(GuardRepoFixture):
    def test_missing_product_template_marker_fails(self):
        path = self.root / "CLAUDE.md"
        path.write_text("# Filled personal workspace\n", encoding="utf-8")

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("public template token missing", result.stdout)
        self.assertIn("CLAUDE.md", result.stdout)

    def test_resume_metrics_in_public_template_fail(self):
        path = self.root / ".claude/skills/job-application-assistant/01-candidate-profile.md"
        path.write_text(
            "\n".join(
                security_guards.PUBLIC_TEMPLATE_REQUIREMENTS[
                    ".claude/skills/job-application-assistant/01-candidate-profile.md"
                ]
            )
            + "\nManaged 30 cases with a 95% win rate.\n",
            encoding="utf-8",
        )

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("resume-shaped personal metric", result.stdout)

    def test_personal_contact_data_in_public_template_fails(self):
        path = self.root / ".claude/skills/job-application-assistant/01-candidate-profile.md"
        path.write_text(
            "\n".join(
                security_guards.PUBLIC_TEMPLATE_REQUIREMENTS[
                    ".claude/skills/job-application-assistant/01-candidate-profile.md"
                ]
            )
            + "\nContact: Alice Wang <alice.wang@example.net> +852 6123 4567\n",
            encoding="utf-8",
        )

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("personal contact data", result.stdout)

    def test_historical_candidate_marker_anywhere_in_product_fails(self):
        path = self.root / "docs" / "example.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "Example candidate worked at " + "Jun" + "ZeJun.",
            encoding="utf-8",
        )

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("historical candidate marker", result.stdout)

    def test_legacy_personal_workflow_path_fails(self):
        path = self.root / "tools" / "fresh_24h" / "cv_temu_baseline_export.py"
        path.parent.mkdir(parents=True)
        path.write_text("# legacy personal exporter\n", encoding="utf-8")

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("private/legacy workflow", result.stdout)


class RuntimeInstanceBoundaryGuardTests(GuardRepoFixture):
    def test_runtime_private_scanner_implementation_fails(self):
        path = self.root / "JobSearch_2026" / "scripts" / "portal_jd_cdp.py"
        path.parent.mkdir(parents=True)
        path.write_text(
            "from playwright.sync_api import sync_playwright\n"
            "# connect_over_cdp and write directly to a private JD cache\n",
            encoding="utf-8",
        )

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("runtime instance script is not a reviewed thin delegate", result.stdout)
        self.assertIn("portal_jd_cdp.py", result.stdout)

    def test_reviewed_runtime_delegate_cannot_grow_a_private_implementation(self):
        path = self.root / "JobSearch_2026" / "scripts" / "private_temp_two_pass.sh"
        path.parent.mkdir(parents=True)
        path.write_text(
            "#!/usr/bin/env bash\npython3 -m tools.workflow scan\n# connect_over_cdp\n",
            encoding="utf-8",
        )

        result = run_guards(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("runtime thin delegate contains private implementation token", result.stdout)


class RealRepoTests(unittest.TestCase):
    def test_guards_pass_on_this_repo(self):
        # The live check CI runs: the actual repo tree must satisfy its own guards.
        result = run_guards(REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
