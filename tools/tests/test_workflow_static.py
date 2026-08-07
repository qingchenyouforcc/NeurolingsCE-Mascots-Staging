"""Static security checks for GitHub Actions workflows (stdlib-only)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

FULL_SHA_RE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@([0-9a-f]{40})\s*(#.*)?$"
)
SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")


def workflow_lines(name: str) -> list[str]:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8").splitlines()


class WorkflowStaticTest(unittest.TestCase):
    def test_all_actions_pinned_to_full_commit_sha(self):
        self.assertTrue(WORKFLOW_DIR.is_dir(), WORKFLOW_DIR)
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if "uses:" not in stripped:
                    continue
                match = re.search(r"uses:\s*(\S+)", stripped)
                if match is None:
                    continue
                uses = match.group(1).strip("'\"")
                with self.subTest(file=path.name, uses=uses):
                    self.assertRegex(
                        uses, FULL_SHA_RE,
                        f"{path.name}: action {uses!r} is not pinned to a full SHA",
                    )

    def test_every_workflow_declares_permissions(self):
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            with self.subTest(file=path.name):
                self.assertIn(
                    "permissions:",
                    path.read_text(encoding="utf-8"),
                    f"{path.name} must declare explicit permissions",
                )

    def test_no_pull_request_target(self):
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            with self.subTest(file=path.name):
                self.assertNotIn(
                    "pull_request_target",
                    path.read_text(encoding="utf-8"),
                    f"{path.name} must not use pull_request_target",
                )

    def test_pr_validation_checks_out_base_sha_only(self):
        text = "\n".join(workflow_lines("pr-validation.yml"))
        self.assertIn("github.event.pull_request.base.sha", text)
        self.assertNotIn("github.event.pull_request.head.sha", text)

    def test_pr_validation_never_touches_draft_assets(self):
        text = "\n".join(workflow_lines("pr-validation.yml"))
        self.assertIn("verify_pr_registry_only", text)
        self.assertNotIn("verify_pr_manifest_and_download_asset", text)
        self.assertNotIn("download_release_asset", text)
        self.assertNotIn("package-validation", text)
        self.assertNotIn('safe_download(meta["url"], ""', text)
        self.assertNotIn('safe_download(url, "",', text)
        self.assertNotIn('safe_download(meta["url"], "",', text)

    def test_pr_validation_has_no_package_validation_job(self):
        text = "\n".join(workflow_lines("pr-validation.yml"))
        self.assertNotIn("package-validation:", text)
        self.assertNotIn("Build public validator", text)

    def test_publish_workflow_does_not_push_main(self):
        text = "\n".join(workflow_lines("publish-and-deploy.yml"))
        for forbidden in ("git push", "git commit", "manifest_path.write_text"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_publish_workflow_revalidates_draft_before_publishing(self):
        text = "\n".join(workflow_lines("publish-and-deploy.yml"))
        self.assertIn("download_release_asset", text)
        self.assertIn("cli_validate", text)
        self.assertIn("publish_release", text)
        self.assertIn("sys.exit(1)", text)

    def test_publish_and_deploy_job_chain(self):
        text = "\n".join(workflow_lines("publish-and-deploy.yml"))
        self.assertIn("workflow_dispatch", text)
        self.assertIn("concurrency:", text)
        self.assertIn("group: publish-and-deploy", text)
        self.assertIn("queue: max", text)
        self.assertNotIn("cancel-in-progress", text)
        publish_pos = text.find("publish_releases:")
        index_pos = text.find("generate_index:")
        deploy_pos = text.find("deploy_pages:")
        self.assertLess(publish_pos, index_pos)
        self.assertLess(index_pos, deploy_pos)
        self.assertIn("needs: publish_releases", text)
        self.assertIn("needs: generate_index", text)
        self.assertIn("permissions:\n      contents: write", text)
        self.assertIn("permissions:\n      contents: read", text)
        self.assertIn("permissions:\n      pages: write", text)
        self.assertIn("id-token: write", text)

    def test_no_legacy_publish_or_deploy_workflow(self):
        self.assertFalse((WORKFLOW_DIR / "publish-release.yml").exists())
        self.assertFalse((WORKFLOW_DIR / "deploy-pages.yml").exists())

    def test_staging_repo_parameterized_with_production_defaults(self):
        for name in (
            "pr-validation.yml",
            "publish-and-deploy.yml",
            "cleanup-submissions.yml",
        ):
            text = "\n".join(workflow_lines(name))
            with self.subTest(file=name):
                self.assertIn("vars.REGISTRY_OWNER", text)
                self.assertIn("vars.REGISTRY_REPO", text)
                self.assertIn("'qingchenyouforcc'", text)
                self.assertIn("'NeurolingsCE-Mascots'", text)

    def test_cleanup_checks_out_base_sha_only(self):
        text = "\n".join(workflow_lines("cleanup-submissions.yml"))
        self.assertIn("actions/checkout", text)
        self.assertIn("github.event.pull_request.base.sha", text)
        self.assertNotIn("github.event.pull_request.head.sha", text)

    def test_only_automatic_github_token_used(self):
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            secrets = set(SECRET_RE.findall(text))
            with self.subTest(file=path.name, secrets=secrets):
                self.assertLessEqual(secrets, {"GITHUB_TOKEN"})

    def test_no_private_key_or_client_secret_in_workflows(self):
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            text = path.read_text(encoding="utf-8").lower()
            with self.subTest(file=path.name):
                self.assertNotIn("client_secret", text)
                self.assertNotIn("private_key", text)


if __name__ == "__main__":
    unittest.main()
