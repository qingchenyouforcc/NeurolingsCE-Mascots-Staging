from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow_helpers as wh  # noqa: E402


def submission_manifest(mid: str = "sample", version: str = "1.2.3",
                        release_id: int = 42) -> dict:
    return {
        "schemaVersion": "1",
        "id": mid,
        "name": "Sample",
        "version": version,
        "summary": "summary",
        "description": "description",
        "authors": [{"githubLogin": "octocat", "displayName": "Octo Cat"}],
        "maintainers": ["octocat"],
        "license": "MIT",
        "status": "draft",
        "release": {
            "releaseId": release_id,
            "assetId": 7,
            "tag": f"draft/{mid}-{version}",
        },
        "package": {
            "fileName": f"{mid}.mascot",
            "url": "https://github.com/owner/repo/releases/download/draft/sample-1.2.3/sample.mascot",
            "size": 1,
            "sha256": "a" * 64,
            "contentType": "application/octet-stream",
        },
    }


def encoded_manifest(manifest: dict) -> dict:
    return {
        "encoding": "base64",
        "content": base64.b64encode(
            json.dumps(manifest).encode("utf-8")
        ).decode("ascii"),
    }


def submission_pr(**overrides) -> dict:
    pr = {
        "state": "open",
        "merged": False,
        "number": 7,
        "head": {
            "ref": "submission/sample-1.2.3",
            "sha": "head-sha",
            "repo": {"full_name": "owner/repo"},
        },
    }
    pr.update(overrides)
    return pr


def submission_manifest_full() -> dict:
    manifest = submission_manifest()
    manifest["submissionId"] = "ab" * 12
    manifest["minimumNeurolingsCEVersion"] = "0.5.1"
    manifest["createdAt"] = "2026-08-06T00:00:00Z"
    manifest["updatedAt"] = "2026-08-06T00:00:00Z"
    manifest["package"] = {
        "fileName": "sample.mascot",
        "url": "https://github.com/owner/repo/releases/download/draft/sample-1.2.3/sample.mascot",
        "size": 5,
        "sha256": "a" * 64,
        "contentType": "application/octet-stream",
    }
    return manifest


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:size], self._body[size:]
        return data


class WorkflowHelpersTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, str]] = []

    def install_fake(self, routes: dict):
        """routes: {(method, url): (status, body_or_None)} with callable support."""
        def fake(method: str, url: str, token: str, payload: dict | None = None) -> dict:
            self.calls.append((method, url))
            key = (method, url)
            handler = routes.get(key)
            if handler is None:
                candidates = [
                    (route_key[1], route_handler)
                    for route_key, route_handler in routes.items()
                    if route_key[0] == method and route_key[1] in url
                ]
                if candidates:
                    # Most specific (longest) URL pattern wins so that
                    # /pulls/7 does not shadow /pulls/7/files.
                    _, handler = max(candidates, key=lambda item: len(item[0]))
            if handler is None:
                raise wh.WorkflowApiError(f"no route for {method} {url}", 501)
            if callable(handler):
                return handler(method, url)
            status, body = handler
            if status == 404:
                raise wh.WorkflowApiError("Not Found", 404)
            return body

        wh.github_request = fake  # type: ignore[assignment]
        wh.github_request_with_headers = (  # type: ignore[assignment]
            lambda method, url, token, payload=None: (fake(method, url, token, payload), {})
        )

    def test_publish_verify_accepts_draft(self):
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/releases/42"): (
                200, {"id": 42, "tag_name": "draft/sample-1.2.3", "draft": True},
            ),
        })
        self.assertEqual(
            wh.verify_release_before_publish("t", "owner", "repo", 42, manifest),
            "draft",
        )

    def test_publish_verify_accepts_already_published(self):
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/releases/42"): (
                200, {"id": 42, "tag_name": "draft/sample-1.2.3", "draft": False},
            ),
        })
        self.assertEqual(
            wh.verify_release_before_publish("t", "owner", "repo", 42, manifest),
            "already_published",
        )

    def test_publish_verify_rejects_tag_mismatch(self):
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/releases/42"): (
                200, {"id": 42, "tag_name": "official/other", "draft": True},
            ),
        })
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_release_before_publish("t", "owner", "repo", 42, manifest)

    def test_publish_verify_rejects_missing_release(self):
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/releases/42"): (404, None),
        })
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_release_before_publish("t", "owner", "repo", 42, manifest)

    def _pr(self, **overrides) -> dict:
        pr = {
            "state": "closed",
            "merged": False,
            "number": 7,
            "head": {
                "ref": "submission/sample-1.2.3",
                "sha": "head-sha",
                "repo": {"full_name": "owner/repo"},
            },
        }
        pr.update(overrides)
        return pr

    def test_cleanup_skips_merged_pr(self):
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (200, self._pr(merged=True)),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "pr_not_closed_unmerged")

    def test_cleanup_skips_fork_head(self):
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (
                200,
                self._pr(head={"ref": "submission/sample-1.2.3", "sha": "s",
                              "repo": {"full_name": "attacker/repo"}}),
            ),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "head_repo_mismatch: 'attacker/repo'")

    def test_cleanup_skips_arbitrary_branch(self):
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (
                200,
                self._pr(head={"ref": "feature/evil", "sha": "s",
                              "repo": {"full_name": "owner/repo"}}),
            ),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertFalse(result["verified"])
        self.assertIn("branch_not_submission_format", result["reason"])

    def test_cleanup_skips_outside_allowlist(self):
        pr = self._pr(head={"ref": "submission/sample-1.2.3", "sha": "s",
                            "repo": {"full_name": "owner/repo"}})
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200,
                [{"filename": ".github/workflows/evil.yml"},
                 {"filename": "mascots/sample/manifest.json"}],
            ),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertFalse(result["verified"])
        self.assertIn("changed_files_outside_allowlist", result["reason"])

    def test_cleanup_skips_non_draft_release(self):
        pr = self._pr()
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200, [{"filename": "mascots/sample/manifest.json"}],
            ),
            ("GET", "/repos/owner/repo/contents/mascots/sample/manifest.json"): (
                200, encoded_manifest(manifest),
            ),
            ("GET", "/repos/owner/repo/releases/42"): (
                200, {"id": 42, "tag_name": "draft/sample-1.2.3", "draft": False},
            ),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "release_not_draft")
        self.assertFalse(any(m == "DELETE" for m, _ in self.calls))

    def test_cleanup_skips_release_tag_mismatch(self):
        pr = self._pr()
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200, [{"filename": "mascots/sample/manifest.json"}],
            ),
            ("GET", "/repos/owner/repo/contents/mascots/sample/manifest.json"): (
                200, encoded_manifest(manifest),
            ),
            ("GET", "/repos/owner/repo/releases/42"): (
                200, {"id": 42, "tag_name": "official/sample", "draft": True},
            ),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "release_tag_mismatch: 'official/sample'")

    def test_cleanup_absent_release_is_safe_noop(self):
        pr = self._pr()
        manifest = submission_manifest(release_id=999)
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200, [{"filename": "mascots/sample/manifest.json"}],
            ),
            ("GET", "/repos/owner/repo/contents/mascots/sample/manifest.json"): (
                200, encoded_manifest(manifest),
            ),
            ("GET", "/repos/owner/repo/releases/999"): (404, None),
            ("DELETE", "/repos/owner/repo/git/refs/heads/submission/sample-1.2.3"): (204, {}),
        })
        # A missing release is safe: no release DELETE, but the verified
        # submission branch is still removed (idempotent cleanup).
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertTrue(result["verified"])
        self.assertEqual(result["reason"], "release_already_absent")
        self.assertFalse(any(
            method == "DELETE" and "/releases/" in url
            for method, url in self.calls
        ))
        self.assertTrue(any(
            method == "DELETE" and "/git/refs/" in url
            for method, url in self.calls
        ))
        self.assertEqual(result["deletedBranch"], "submission/sample-1.2.3")

    def test_cleanup_deletes_verified_draft(self):
        pr = self._pr()
        manifest = submission_manifest()
        self.install_fake({
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200, [{"filename": "mascots/sample/manifest.json"}],
            ),
            ("GET", "/repos/owner/repo/contents/mascots/sample/manifest.json"): (
                200, encoded_manifest(manifest),
            ),
            ("GET", "/repos/owner/repo/releases/42"): (
                200, {"id": 42, "tag_name": "draft/sample-1.2.3", "draft": True},
            ),
            ("DELETE", "/repos/owner/repo/releases/42"): (204, {}),
            ("DELETE", "/repos/owner/repo/git/refs/heads/submission/sample-1.2.3"): (204, {}),
        })
        result = wh.verify_and_cleanup_submission_pr("t", "owner", "repo", 7)
        self.assertTrue(result["verified"])
        self.assertEqual(result["deletedReleaseId"], 42)
        self.assertEqual(result["deletedBranch"], "submission/sample-1.2.3")
        self.assertTrue(any(m == "DELETE" for m, _ in self.calls))


class PrAssetValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        self.calls: list[tuple[str, str]] = []
        self.download_headers: dict | None = None
        self._orig_build_opener = wh._build_opener

    def tearDown(self):
        wh._build_opener = self._orig_build_opener  # type: ignore[assignment]
        self.tmp.cleanup()

    def install_fake(self, routes: dict):
        def fake(method: str, url: str, token: str, payload: dict | None = None) -> dict:
            self.calls.append((method, url))
            key = (method, url)
            handler = routes.get(key)
            if handler is None:
                candidates = [
                    (route_key[1], route_handler)
                    for route_key, route_handler in routes.items()
                    if route_key[0] == method and route_key[1] in url
                ]
                if candidates:
                    _, handler = max(candidates, key=lambda item: len(item[0]))
            if handler is None:
                raise wh.WorkflowApiError(f"no route for {method} {url}", 501)
            if callable(handler):
                return handler(method, url)
            status, body = handler
            if status == 404:
                raise wh.WorkflowApiError("Not Found", 404)
            return body

        wh.github_request = fake  # type: ignore[assignment]
        wh.github_request_with_headers = (  # type: ignore[assignment]
            lambda method, url, token, payload=None: (fake(method, url, token, payload), {})
        )

    def install_download(self, body: bytes):
        class FakeOpener:
            def __init__(self, payload: bytes):
                self.payload = payload
                self.last_request = None

            def open(self, request, timeout=120):
                self.last_request = request
                return FakeResponse(self.payload)

        opener = FakeOpener(body)
        wh._build_opener = lambda: opener  # type: ignore[assignment]
        self._opener = opener

    def _routes(self, pr=None, manifest=None, release=None, assets=None):
        pr = pr or submission_pr()
        manifest = manifest or submission_manifest_full()
        release = release or {
            "id": 42, "tag_name": "draft/sample-1.2.3", "draft": True,
        }
        assets = assets or [{"id": 7, "name": "sample.mascot", "state": "uploaded"}]
        return {
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200, [{"filename": "mascots/sample/manifest.json"}],
            ),
            ("GET", "/repos/owner/repo/contents/mascots/sample/manifest.json"): (
                200, encoded_manifest(manifest),
            ),
            ("GET", "/repos/owner/repo/releases/42"): (200, release),
            ("GET", "/repos/owner/repo/releases/42/assets"): (200, assets),
        }

    def test_downloads_draft_asset_with_authentication(self):
        manifest = submission_manifest_full()
        manifest["package"]["sha256"] = hashlib.sha256(b"hello").hexdigest()
        self.install_fake(self._routes(manifest=manifest))
        self.install_download(b"hello")
        result = wh.verify_pr_manifest_and_download_asset(
            "token-1", "owner", "repo", 7, self.dest
        )
        self.assertEqual(result["mascotId"], "sample")
        self.assertEqual(result["assetPath"].read_bytes(), b"hello")
        self.assertEqual(
            self._opener.last_request.headers["Authorization"], "Bearer token-1"
        )
        self.assertEqual(
            self._opener.last_request.headers["Accept"], "application/octet-stream"
        )
        self.assertEqual(result["apiTrace"]["releaseHttpStatus"], 200)
        self.assertEqual(result["apiTrace"]["releaseDraft"], True)
        self.assertEqual(result["apiTrace"]["assetListHttpStatus"], 200)
        self.assertEqual(result["apiTrace"]["assetDownloadHttpStatus"], 200)
        self.assertEqual(result["apiTrace"]["downloadSize"], 5)
        self.assertEqual(
            result["apiTrace"]["authorizationForwardedToFinalHost"], False
        )

    def test_extra_changed_file_fails(self):
        routes = self._routes()
        routes[("GET", "/repos/owner/repo/pulls/7/files")] = (
            200,
            [{"filename": "mascots/sample/manifest.json"},
             {"filename": ".github/workflows/evil.yml"}],
        )
        self.install_fake(routes)
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_fork_head_fails(self):
        self.install_fake(self._routes(pr=submission_pr(
            head={"ref": "submission/sample-1.2.3", "sha": "s",
                  "repo": {"full_name": "attacker/repo"}}
        )))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_release_not_draft_fails(self):
        self.install_fake(self._routes(release={
            "id": 42, "tag_name": "draft/sample-1.2.3", "draft": False,
        }))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_release_tag_mismatch_fails(self):
        self.install_fake(self._routes(release={
            "id": 42, "tag_name": "official/sample", "draft": True,
        }))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_missing_release_fails(self):
        routes = self._routes()
        routes[("GET", "/repos/owner/repo/releases/42")] = (404, None)
        self.install_fake(routes)
        with self.assertRaises(wh.WorkflowApiError) as ctx:
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )
        self.assertEqual(ctx.exception.status, 404)

    def test_asset_id_mismatch_fails(self):
        self.install_fake(self._routes(assets=[
            {"id": 99, "name": "sample.mascot", "state": "uploaded"},
        ]))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_asset_name_mismatch_fails(self):
        self.install_fake(self._routes(assets=[
            {"id": 7, "name": "other.mascot", "state": "uploaded"},
        ]))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_asset_not_uploaded_fails(self):
        self.install_fake(self._routes(assets=[
            {"id": 7, "name": "sample.mascot", "state": "starter"},
        ]))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_sha256_mismatch_fails(self):
        self.install_fake(self._routes())
        self.install_download(b"different bytes")
        with self.assertRaises(ValueError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )

    def test_manifest_tag_mismatch_fails(self):
        manifest = submission_manifest_full()
        manifest["release"]["tag"] = "official/sample"
        self.install_fake(self._routes(manifest=manifest))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_manifest_and_download_asset(
                "t", "owner", "repo", 7, self.dest
            )


class PrRegistryOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls: list[tuple[str, str]] = []

    def tearDown(self):
        self.tmp.cleanup()

    def install_fake(self, routes: dict):
        def fake(method: str, url: str, token: str, payload: dict | None = None) -> dict:
            self.calls.append((method, url))
            key = (method, url)
            handler = routes.get(key)
            if handler is None:
                candidates = [
                    (route_key[1], route_handler)
                    for route_key, route_handler in routes.items()
                    if route_key[0] == method and route_key[1] in url
                ]
                if candidates:
                    _, handler = max(candidates, key=lambda item: len(item[0]))
            if handler is None:
                raise wh.WorkflowApiError(f"no route for {method} {url}", 501)
            if callable(handler):
                return handler(method, url)
            status, body = handler
            if status == 404:
                raise wh.WorkflowApiError("Not Found", 404)
            return body

        wh.github_request = fake  # type: ignore[assignment]
        wh.github_request_with_headers = (  # type: ignore[assignment]
            lambda method, url, token, payload=None: (fake(method, url, token, payload), {})
        )

    def _routes(self, pr=None, manifest=None):
        pr = pr or submission_pr()
        manifest = manifest or submission_manifest_full()
        return {
            ("GET", "/repos/owner/repo/pulls/7"): (200, pr),
            ("GET", "/repos/owner/repo/pulls/7/files"): (
                200, [{"filename": "mascots/sample/manifest.json"}],
            ),
            ("GET", "/repos/owner/repo/contents/mascots/sample/manifest.json"): (
                200, encoded_manifest(manifest),
            ),
        }

    def test_accepts_valid_registry_only_submission(self):
        self.install_fake(self._routes())
        result = wh.verify_pr_registry_only(
            "t", "owner", "repo", 7, self.root
        )
        self.assertEqual(result["mascotId"], "sample")
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["headSha"], "head-sha")
        self.assertEqual(result["changedFiles"], ["mascots/sample/manifest.json"])

    def test_rejects_extra_changed_file(self):
        routes = self._routes()
        routes[("GET", "/repos/owner/repo/pulls/7/files")] = (
            200,
            [{"filename": "mascots/sample/manifest.json"},
             {"filename": ".github/workflows/evil.yml"}],
        )
        self.install_fake(routes)
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_registry_only("t", "owner", "repo", 7, self.root)

    def test_rejects_duplicate_id_version_on_main(self):
        base = self.root / "mascots" / "sample" / "manifest.json"
        base.parent.mkdir(parents=True)
        base.write_text(json.dumps({
            "id": "sample", "version": "1.2.3",
        }), encoding="utf-8")
        self.install_fake(self._routes())
        with self.assertRaises(wh.WorkflowApiError) as ctx:
            wh.verify_pr_registry_only("t", "owner", "repo", 7, self.root)
        self.assertIn("already exists on main", str(ctx.exception))

    def test_rejects_version_downgrade_against_main(self):
        base = self.root / "mascots" / "sample" / "manifest.json"
        base.parent.mkdir(parents=True)
        base.write_text(json.dumps({
            "id": "sample", "version": "2.0.0",
        }), encoding="utf-8")
        self.install_fake(self._routes())
        with self.assertRaises(wh.WorkflowApiError) as ctx:
            wh.verify_pr_registry_only("t", "owner", "repo", 7, self.root)
        self.assertIn("strictly higher", str(ctx.exception))

    def test_rejects_manifest_schema_violation(self):
        manifest = submission_manifest_full()
        del manifest["description"]
        self.install_fake(self._routes(manifest=manifest))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_registry_only("t", "owner", "repo", 7, self.root)

    def test_rejects_non_submission_branch(self):
        self.install_fake(self._routes(pr=submission_pr(
            head={"ref": "feature/evil", "sha": "s",
                  "repo": {"full_name": "owner/repo"}}
        )))
        with self.assertRaises(wh.WorkflowApiError):
            wh.verify_pr_registry_only("t", "owner", "repo", 7, self.root)

    def test_rejects_release_metadata_format(self):
        manifest = submission_manifest_full()
        manifest["release"]["releaseId"] = "42"
        self.install_fake(self._routes(manifest=manifest))
        with self.assertRaises(wh.WorkflowApiError) as ctx:
            wh.verify_pr_registry_only("t", "owner", "repo", 7, self.root)
        self.assertIn("must be integers", str(ctx.exception))


class PaginationTest(unittest.TestCase):
    BASE = "https://api.github.com/repos/owner/repo"

    def setUp(self):
        self.routes: dict[str, tuple] = {}

    def install(self):
        def with_headers(method: str, url: str, token: str, payload=None):
            candidates = [
                (key, value)
                for key, value in self.routes.items()
                if method == "GET" and key in url
            ]
            if not candidates:
                raise wh.WorkflowApiError(f"no route for {url}", 501)
            key, (body, link) = max(candidates, key=lambda item: len(item[0]))
            headers = {"Link": link} if link else {}
            return body, headers

        wh.github_request_with_headers = with_headers  # type: ignore[assignment]

    def test_pr_files_follows_link_header(self):
        self.routes = {
            "/pulls/7/files?per_page=100": (
                [{"filename": "mascots/sample/manifest.json"}],
                f'<{self.BASE}/pulls/7/files?per_page=100&page=2>; rel="next"',
            ),
            "/pulls/7/files?per_page=100&page=2": (
                [{"filename": "mascots/sample/extra.json"}],
                None,
            ),
        }
        self.install()
        files = wh.get_pr_files("t", "owner", "repo", 7, expected_count=2)
        self.assertEqual(len(files), 2)

    def test_pr_files_more_than_100(self):
        self.routes = {
            "/pulls/7/files?per_page=100": (
                [{"filename": f"mascots/sample/manifest.json"} for _ in range(100)],
                f'<{self.BASE}/pulls/7/files?per_page=100&page=2>; rel="next"',
            ),
            "/pulls/7/files?per_page=100&page=2": (
                [{"filename": "mascots/sample/manifest.json"}],
                None,
            ),
        }
        self.install()
        files = wh.get_pr_files("t", "owner", "repo", 7, expected_count=101)
        self.assertEqual(len(files), 101)

    def test_changed_files_count_mismatch_fails(self):
        self.routes = {
            "/pulls/7/files?per_page=100": (
                [
                    {"filename": "mascots/sample/manifest.json"},
                    {"filename": "mascots/sample/manifest.json"},
                ],
                None,
            ),
        }
        self.install()
        with self.assertRaises(wh.WorkflowApiError):
            wh.get_pr_files("t", "owner", "repo", 7, expected_count=1)

    def test_link_cycle_detected(self):
        self.routes = {
            "/pulls/7/files?per_page=100": (
                [{"filename": "mascots/sample/manifest.json"}],
                f'<{self.BASE}/pulls/7/files?per_page=100>; rel="next"',
            ),
        }
        self.install()
        with self.assertRaises(wh.WorkflowApiError):
            wh.get_pr_files("t", "owner", "repo", 7)

    def test_second_page_api_failure_fails_closed(self):
        def failing(method: str, url: str, token: str, payload=None):
            if "page=2" in url:
                raise wh.WorkflowApiError("GitHub API 500", 500)
            link = f'<{self.BASE}/pulls/7/files?per_page=100&page=2>; rel="next"'
            return (
                [{"filename": "mascots/sample/manifest.json"}],
                {"Link": link},
            )

        wh.github_request_with_headers = failing  # type: ignore[assignment]
        with self.assertRaises(wh.WorkflowApiError):
            wh.get_pr_files("t", "owner", "repo", 7)

    def test_150_published_releases(self):
        first_page = [
            {"tag_name": f"v{i}", "draft": False} for i in range(100)
        ]
        second_page = [
            {"tag_name": f"v{i + 100}", "draft": False} for i in range(50)
        ]
        self.routes = {
            "/releases?per_page=100": (
                first_page,
                f'<{self.BASE}/releases?per_page=100&page=2>; rel="next"',
            ),
            "/releases?per_page=100&page=2": (second_page, None),
        }
        self.install()
        tags = wh.list_published_release_tags("t", "owner", "repo")
        self.assertEqual(len(tags), 150)


class RedirectDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "asset.bin"
        self.server1_authorization: str | None = None
        self.server2_authorization: str | None = None
        self._orig_build_opener = wh._build_opener
        self._orig_allow_insecure = wh._ALLOW_INSECURE_REDIRECTS
        wh._ALLOW_INSECURE_REDIRECTS = True

    def tearDown(self):
        wh._build_opener = self._orig_build_opener  # type: ignore[assignment]
        wh._ALLOW_INSECURE_REDIRECTS = self._orig_allow_insecure
        wh._ACTIVE_DOWNLOAD_TRACE = None
        self.tmp.cleanup()

    def _serve(self, handler_factory):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _stop(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def test_authorization_stripped_on_cross_host_redirect(self):
        def make_handler(target_url, record_holder):
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    record_holder["auth"] = self.headers.get("Authorization")
                    if target_url:
                        self.send_response(302)
                        self.send_header("Location", target_url)
                        self.end_headers()
                    else:
                        body = b"hello-asset"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                def log_message(self, *args):
                    pass

            return Handler

        holder1: dict = {}
        holder2: dict = {}
        server2, thread2 = self._serve(
            make_handler(None, holder2)
        )
        server1, thread1 = self._serve(
            make_handler(
                f"http://127.0.0.1:{server2.server_address[1]}/asset", holder1
            )
        )
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server1.server_address[1]}/asset", method="GET"
            )
            request.add_header("Authorization", "Bearer secret-token")
            sha = hashlib.sha256(b"hello-asset").hexdigest()
            wh._stream_request(request, self.dest, sha)
            self.assertEqual(holder1["auth"], "Bearer secret-token")
            self.assertIsNone(holder2["auth"])
            self.assertEqual(self.dest.read_bytes(), b"hello-asset")
        finally:
            self._stop(server1, thread1)
            self._stop(server2, thread2)

    def test_redirect_trace_records_sanitized_hops(self):
        def make_handler(target_url, record_holder):
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    record_holder["auth"] = self.headers.get("Authorization")
                    if target_url:
                        self.send_response(302)
                        self.send_header("Location", target_url)
                        self.end_headers()
                    else:
                        body = b"traced-asset"
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                def log_message(self, *args):
                    pass

            return Handler

        holder1: dict = {}
        holder2: dict = {}
        server2, thread2 = self._serve(make_handler(None, holder2))
        server1, thread1 = self._serve(
            make_handler(
                f"http://127.0.0.1:{server2.server_address[1]}/asset", holder1
            )
        )
        trace: list[dict] = []
        wh._ACTIVE_DOWNLOAD_TRACE = trace
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server1.server_address[1]}/asset", method="GET"
            )
            request.add_header("Authorization", "Bearer secret-token")
            sha = hashlib.sha256(b"traced-asset").hexdigest()
            wh._stream_request(request, self.dest, sha)
            self.assertEqual(len(trace), 1)
            self.assertEqual(trace[0]["status"], 302)
            self.assertIn("127.0.0.1", trace[0]["fromHost"])
            self.assertIn("127.0.0.1", trace[0]["toHost"])
            self.assertFalse(trace[0]["authorizationForwarded"])
            self.assertIsNone(holder2["auth"])
        finally:
            wh._ACTIVE_DOWNLOAD_TRACE = None
            self._stop(server1, thread1)
            self._stop(server2, thread2)

    def test_http_redirect_rejected(self):
        wh._ALLOW_INSECURE_REDIRECTS = False
        server2, thread2 = self._serve(
            type("Handler", (http.server.BaseHTTPRequestHandler,), {
                "do_GET": lambda self: (self.send_response(200), self.end_headers()),
                "log_message": lambda self, *a: None,
            })
        )
        server1, thread1 = self._serve(
            type("Handler", (http.server.BaseHTTPRequestHandler,), {
                "do_GET": lambda self: (
                    self.send_response(302),
                    self.send_header("Location", f"http://127.0.0.1:{server2.server_address[1]}/x"),
                    self.end_headers(),
                ),
                "log_message": lambda self, *a: None,
            })
        )
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server1.server_address[1]}/asset", method="GET"
            )
            request.add_header("Authorization", "Bearer secret-token")
            with self.assertRaises(urllib.error.HTTPError):
                wh._stream_request(
                    request, self.dest, hashlib.sha256(b"x").hexdigest()
                )
        finally:
            self._stop(server1, thread1)
            self._stop(server2, thread2)

    def test_redirect_loop_rejected(self):
        class LoopHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                port = self.server.server_address[1]
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/loop")
                self.end_headers()

            def log_message(self, *args):
                pass

        server1, thread1 = self._serve(LoopHandler)
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server1.server_address[1]}/loop", method="GET"
            )
            with self.assertRaises(urllib.error.HTTPError):
                wh._stream_request(
                    request, self.dest, hashlib.sha256(b"x").hexdigest()
                )
            self.assertFalse(self.dest.exists())
        finally:
            self._stop(server1, thread1)

    def test_size_limit_after_redirect_deletes_temp_file(self):
        def make_handler(target_url, body):
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if target_url:
                        self.send_response(302)
                        self.send_header("Location", target_url)
                        self.end_headers()
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                def log_message(self, *args):
                    pass

            return Handler

        body = b"x" * 16
        server2, thread2 = self._serve(make_handler(None, body))
        server1, thread1 = self._serve(
            make_handler(
                f"http://127.0.0.1:{server2.server_address[1]}/asset", body
            )
        )
        old_limit = wh.MAX_DOWNLOAD_BYTES
        wh.MAX_DOWNLOAD_BYTES = 8
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server1.server_address[1]}/asset", method="GET"
            )
            with self.assertRaises(ValueError):
                wh._stream_request(
                    request, self.dest, hashlib.sha256(body).hexdigest()
                )
            self.assertFalse(self.dest.exists())
        finally:
            wh.MAX_DOWNLOAD_BYTES = old_limit
            self._stop(server1, thread1)
            self._stop(server2, thread2)


if __name__ == "__main__":
    unittest.main()
