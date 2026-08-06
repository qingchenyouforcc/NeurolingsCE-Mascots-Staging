from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate_index import build_index  # noqa: E402
from registry_checks import validate_registry  # noqa: E402


def valid_manifest(mid: str = "sample", version: str = "1.2.3") -> dict:
    return {
        "schemaVersion": "1",
        "id": mid,
        "name": "Sample Mascot",
        "version": version,
        "summary": "A short summary",
        "description": "A longer description.",
        "authors": [{"githubLogin": "octocat", "displayName": "Octo Cat"}],
        "maintainers": ["octocat"],
        "license": "MIT",
        "isDerivative": False,
        "minimumNeurolingsCEVersion": "0.5.1",
        "package": {
            "fileName": f"{mid}.mascot",
            "url": "https://example.invalid/download/sample.mascot",
            "size": 12345,
            "sha256": "a" * 64,
            "contentType": "application/octet-stream",
        },
        "tags": ["cat"],
        "categories": ["animal"],
        "createdAt": "2026-08-06T00:00:00Z",
        "updatedAt": "2026-08-06T00:00:00Z",
    }


def write_manifest(root: Path, mid: str, manifest: dict) -> Path:
    path = root / "mascots" / mid / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


class RegistryToolsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_accepts_valid_registry(self):
        write_manifest(self.root, "sample", valid_manifest())
        self.assertEqual(validate_registry(self.root), [])

    def test_rejects_directory_id_mismatch(self):
        write_manifest(self.root, "other", valid_manifest(mid="sample"))
        errors = validate_registry(self.root)
        self.assertTrue(any("must equal the directory name" in e for e in errors))

    def test_rejects_duplicate_id(self):
        write_manifest(self.root, "sample", valid_manifest())
        write_manifest(self.root, "sample-copy", valid_manifest(mid="sample"))
        errors = validate_registry(self.root)
        self.assertTrue(any("duplicate id" in e for e in errors))

    def test_rejects_duplicate_id_version(self):
        write_manifest(self.root, "sample", valid_manifest(version="1.0.0"))
        write_manifest(self.root, "sample-v2", valid_manifest(mid="sample", version="1.0.0"))
        errors = validate_registry(self.root)
        self.assertTrue(any("duplicate id+version" in e for e in errors))

    def test_rejects_bad_semver(self):
        manifest = valid_manifest()
        manifest["version"] = "1.2"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("valid SemVer" in e for e in errors))

    def test_rejects_bad_sha256(self):
        manifest = valid_manifest()
        manifest["package"]["sha256"] = "ABC"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("sha256" in e for e in errors))

    def test_rejects_bad_id_pattern(self):
        manifest = valid_manifest(mid="Bad_ID")
        write_manifest(self.root, "Bad_ID", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("id must match" in e for e in errors))

    def test_rejects_missing_required_fields(self):
        manifest = valid_manifest()
        del manifest["description"]
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("description" in e for e in errors))

    def test_rejects_unsafe_filename(self):
        manifest = valid_manifest()
        manifest["package"]["fileName"] = "../evil.mascot"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("fileName" in e for e in errors))

    def test_accepts_owner_and_maintainer_user_ids(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["42"]
        manifest["authors"][0]["githubUserId"] = "42"
        write_manifest(self.root, "sample", manifest)
        self.assertEqual(validate_registry(self.root), [])

    def test_rejects_maintainer_ids_length_mismatch(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["42", "43"]
        manifest["authors"][0]["githubUserId"] = "42"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("maintainerUserIds length" in e for e in errors))

    def test_rejects_invalid_owner_user_id(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "../42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["42"]
        manifest["authors"][0]["githubUserId"] = "42"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("owner.userId" in e for e in errors))

    def test_rejects_new_format_without_author_user_id(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["42"]
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("githubUserId is required" in e for e in errors))

    def test_rejects_duplicate_maintainer_ids(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["42", "42"]
        manifest["maintainers"] = ["octocat", "octocat"]
        manifest["authors"][0]["githubUserId"] = "42"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(any("maintainerUserIds must be unique" in e for e in errors))

    def test_rejects_owner_login_mismatch_with_maintainer(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "other"}
        manifest["maintainerUserIds"] = ["42"]
        manifest["maintainers"] = ["octocat"]
        manifest["authors"][0]["githubUserId"] = "42"
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(
            any("owner.login must match maintainers[0]" in e for e in errors)
        )

    def test_rejects_author_login_mismatch_with_maintainer(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["42", "43"]
        manifest["maintainers"] = ["octocat", "bob"]
        manifest["authors"] = [
            {
                "githubLogin": "octocat",
                "githubUserId": "42",
                "displayName": "Octo Cat",
            },
            {
                "githubLogin": "alice",
                "githubUserId": "43",
                "displayName": "Alice",
            },
        ]
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(
            any("authors[1].githubLogin must match maintainers[1]" in e for e in errors)
        )

    def test_rejects_author_login_mismatch_with_owner(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "octocat"}
        manifest["maintainerUserIds"] = ["43"]
        manifest["maintainers"] = ["bob"]
        manifest["authors"] = [
            {
                "githubLogin": "renamed",
                "githubUserId": "42",
                "displayName": "Octo Cat",
            },
        ]
        write_manifest(self.root, "sample", manifest)
        errors = validate_registry(self.root)
        self.assertTrue(
            any("authors[0].githubLogin must match owner.login" in e for e in errors)
        )

    def test_accepts_consistent_renamed_logins(self):
        manifest = valid_manifest()
        manifest["owner"] = {"userId": "42", "login": "renamed"}
        manifest["maintainerUserIds"] = ["42"]
        manifest["maintainers"] = ["renamed"]
        manifest["authors"][0] = {
            "githubLogin": "renamed",
            "githubUserId": "42",
            "displayName": "Octo Cat",
        }
        write_manifest(self.root, "sample", manifest)
        self.assertEqual(validate_registry(self.root), [])

    def test_index_entry_has_derived_published_status(self):
        write_manifest(self.root, "sample", valid_manifest())
        index = build_index(self.root, "2026-08-06T00:00:00Z", "test/registry")
        self.assertEqual(index["mascots"][0]["status"], "published")

    def test_index_is_deterministic_and_sorted(self):
        write_manifest(self.root, "zebra", valid_manifest(mid="zebra", version="2.0.0"))
        write_manifest(self.root, "alpha", valid_manifest(mid="alpha", version="1.0.0"))
        first = build_index(self.root, "2026-08-06T00:00:00Z", "test/registry")
        second = build_index(self.root, "2026-08-06T00:00:00Z", "test/registry")
        text_first = json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
        text_second = json.dumps(second, indent=2, sort_keys=True, ensure_ascii=False)
        self.assertEqual(text_first, text_second)
        self.assertEqual(
            [item["id"] for item in first["mascots"]], ["alpha", "zebra"]
        )
        self.assertEqual(first["schemaVersion"], 1)

    def test_index_excludes_drafts(self):
        write_manifest(self.root, "sample", valid_manifest())
        draft = valid_manifest(mid="draft", version="0.0.1")
        draft["status"] = "draft"
        write_manifest(self.root, "draft", draft)
        index = build_index(self.root, "2026-08-06T00:00:00Z", "test/registry")
        self.assertEqual([item["id"] for item in index["mascots"]], ["sample"])

    def test_index_includes_manifest_when_release_published(self):
        manifest = valid_manifest()
        manifest["status"] = "draft"
        manifest["release"] = {
            "releaseId": 42,
            "assetId": 7,
            "tag": "draft/sample-1.2.3",
        }
        write_manifest(self.root, "sample", manifest)
        index = build_index(
            self.root, "2026-08-06T00:00:00Z", "test/registry",
            published_tags={"draft/sample-1.2.3"},
        )
        self.assertEqual([item["id"] for item in index["mascots"]], ["sample"])

    def test_index_excludes_unpublished_release(self):
        manifest = valid_manifest()
        manifest["status"] = "draft"
        manifest["release"] = {
            "releaseId": 42,
            "assetId": 7,
            "tag": "draft/sample-1.2.3",
        }
        write_manifest(self.root, "sample", manifest)
        index = build_index(
            self.root, "2026-08-06T00:00:00Z", "test/registry",
            published_tags=set(),
        )
        self.assertEqual(index["mascots"], [])

    def test_index_authors_are_logins(self):
        write_manifest(self.root, "sample", valid_manifest())
        index = build_index(self.root, "2026-08-06T00:00:00Z", "test/registry")
        self.assertEqual(index["mascots"][0]["authors"], ["octocat"])


if __name__ == "__main__":
    unittest.main()
