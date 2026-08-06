from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import io
import json
import os
import platform
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402
from app import Config, ServiceError, SubmissionService  # noqa: E402
from github_client import GitHubApiError, GitHubClient  # noqa: E402
from package_checks import run_external_validator  # noqa: E402
from redact import redact_headers, redact_text  # noqa: E402


PNG_BYTES = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41,
    0x54, 0x78, 0x9C, 0x63, 0xF8, 0xCF, 0xC0, 0xF0,
    0x1F, 0x00, 0x05, 0x00, 0x01, 0xFF, 0x89, 0x99,
    0x3D, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
    0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
])


def write_valid_mascot(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "info.json",
            '{"name":"Test","version":"1.0.0","description":"fixture","author":"octocat"}',
        )
        archive.writestr("actions.xml", "<Mascot><ActionList /></Mascot>")
        archive.writestr("behaviors.xml", "<Mascot><BehaviorList /></Mascot>")
        archive.writestr("img/shime1.png", PNG_BYTES)


def write_bad_mascot(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "info.json", '{"name":"Bad","version":"1.0.0"}'
        )
        archive.writestr("actions.xml", "<Mascot/>")
        archive.writestr("behaviors.xml", "<Mascot/>")
        archive.writestr("img/shime1.png", b"not a png")
        archive.writestr("sound/evil.exe", b"MZ")


def metadata_for(login: str = "octocat", mid: str = "sample",
                 version: str = "1.0.0") -> dict:
    return {
        "id": mid,
        "name": "Sample",
        "version": version,
        "summary": "A test mascot",
        "description": "Longer description for the test mascot.",
        "authors": [{
            "githubLogin": login,
            "githubUserId": "1",
            "displayName": "Octo Cat",
        }],
        "maintainers": [login],
        "license": "MIT",
        "isDerivative": False,
        "tags": ["test"],
        "categories": ["test"],
        "minimumNeurolingsCEVersion": "0.5.1",
    }


def multipart_body(boundary: str, metadata: dict, file_path: Path) -> bytes:
    chunks = []
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(b'Content-Disposition: form-data; name="metadata"\r\n')
    chunks.append(b"Content-Type: application/json\r\n\r\n")
    chunks.append(json.dumps(metadata).encode("utf-8"))
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'.encode()
    )
    chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


class MockGitHubServer:
    def __init__(self):
        self.requests: list[dict] = []
        self.existing_manifest: dict | None = None
        self.user_login = "octocat"
        self.changed_files: list[str] | None = None
        self.release_draft = True
        self.created_pr: dict | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self._server.mock = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self, *args, **kwargs):
        return _MockHandler(*args, **kwargs)

    def record(self, method: str, path: str, body: bytes, headers: dict):
        self.requests.append(
            {"method": method, "path": path, "body": body.decode("utf-8", "replace"),
             "authorization": headers.get("Authorization", "")}
        )


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.mock.record(self.command, self.path, body, dict(self.headers))
        mock = self.server.mock

        if self.path == "/user" and self.command == "GET":
            return self._json(200, {"id": 1, "login": mock.user_login})
        if self.path.endswith("/access_tokens") and self.command == "POST":
            return self._json(201, {"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        if "/releases/tags/" in self.path and self.command == "GET":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"message":"Not Found"}')
            return
        if "/releases" in self.path and self.command == "POST" and "assets" not in self.path:
            return self._json(201, {"id": 42, "tag_name": "draft/sample-1.0.0", "draft": True})
        if "/assets?" in self.path and self.command == "POST":
            return self._json(201, {
                "id": 99,
                "browser_download_url": "https://github.com/owner/repo/releases/download/draft/sample-1.0.0/sample.mascot",
            })
        if "/assets?" in self.path and self.command == "GET":
            return self._json(200, [])
        if self.path.endswith("/branches/main") and self.command == "GET":
            return self._json(200, {"commit": {"sha": "base-sha"}})
        if self.path.endswith("/git/refs") and self.command == "POST":
            return self._json(201, {"ref": json.loads(body or b"{}").get("ref", "")})
        if "/git/ref/heads" in self.path and self.command == "GET":
            return self._json(200, {"ref": self.path.split("/")[-1], "object": {"sha": "branch-sha"}})
        if "/git/ref/heads" in self.path and self.command == "DELETE":
            self.send_response(204)
            self.end_headers()
            return
        if "/contents/mascots/" in self.path and self.command == "PUT":
            return self._json(201, {"content": {"sha": "manifest-sha"}})
        if "/pulls?" in self.path and self.command == "GET":
            return self._json(200, [mock.created_pr] if mock.created_pr else [])
        if self.path.endswith("/pulls") and self.command == "POST":
            mock.created_pr = {
                "number": 7,
                "html_url": "https://github.com/owner/repo/pull/7",
                "head": {"ref": json.loads(body or b"{}").get("head", "")},
            }
            return self._json(201, mock.created_pr)
        if "/pulls/" in self.path and "/files" in self.path and self.command == "GET":
            files = mock.changed_files
            if files is None:
                files = ["mascots/sample/manifest.json"]
            return self._json(200, [{"filename": name} for name in files])
        if "/pulls/" in self.path and self.command == "PATCH":
            return self._json(200, {"state": "closed"})
        if re_match_release_id(self.path) and self.command == "GET":
            if mock.release_draft:
                return self._json(200, {"id": 42, "tag_name": "draft/sample-1.0.0", "draft": True})
            return self._json(200, {"id": 42, "tag_name": "draft/sample-1.0.0", "draft": False})
        if re_match_release_id(self.path) and self.command == "DELETE":
            self.send_response(204)
            self.end_headers()
            return
        if "/contents/mascots/" in self.path and self.command == "GET":
            if mock.existing_manifest is None:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"message":"Not Found"}')
                return
            content = base64.b64encode(
                json.dumps(mock.existing_manifest).encode("utf-8")
            ).decode("ascii")
            return self._json(200, {"content": content})
        self.send_response(501)
        self.end_headers()

    def _json(self, status: int, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def re_match_release_id(path: str) -> bool:
    import re
    return bool(re.match(r"^/repos/[^/]+/[^/]+/releases/[0-9]+$", path.split("?")[0]))


class FakeGitHub:
    """In-process GitHub adapter for fault injection and resume tests."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.faults: dict[str, list[GitHubApiError]] = {}
        self.release: dict | None = None
        self.asset: dict | None = None
        self.branch: dict | None = None
        self.manifest: dict | None = None
        self.pr: dict | None = None
        self.pr_files: list[str] = ["mascots/sample/manifest.json"]
        self.release_draft = True
        self.main_manifest: dict | None = None

    def _record(self, name: str, *args):
        self.calls.append((name, args))
        queue = self.faults.get(name)
        if queue:
            error = queue.pop(0)
            raise error

    def _release_view(self) -> dict | None:
        if self.release is None:
            return None
        return {**self.release, "draft": self.release_draft}

    def fail_next(self, name: str, status: int = 0, code: str = "github_timeout"):
        self.faults.setdefault(name, []).append(
            GitHubApiError(code, "injected fault", status)
        )

    def get_user(self, access_token: str) -> dict:
        self._record("get_user", access_token)
        return {"id": 1, "login": "octocat"}

    def get_installation_token(self, app_id, installation_id, key) -> str:
        self._record("get_installation_token")
        return "installation-token"

    def get_release_by_tag(self, token, tag: str) -> dict | None:
        self._record("get_release_by_tag", tag)
        view = self._release_view()
        return view if view is not None and view.get("tag_name") == tag else None

    def create_draft_release(self, token, tag, name, body) -> dict:
        self.calls.append(("create_draft_release", (tag,)))
        queue = self.faults.get("create_draft_release")
        if queue:
            error = queue.pop(0)
            if self.release is None:
                self.release = {"id": 42, "tag_name": tag, "draft": True}
            raise error
        if self.release is None:
            self.release = {"id": 42, "tag_name": tag, "draft": True}
        return self.release

    def get_release_assets(self, token, release_id: int) -> list[dict]:
        self._record("get_release_assets", release_id)
        return [self.asset] if self.asset else []

    def upload_release_asset(self, token, release_id, file_path, file_name,
                             content_type="application/octet-stream") -> dict:
        self.calls.append(("upload_release_asset", (release_id, file_name)))
        queue = self.faults.get("upload_release_asset")
        self.asset = {
            "id": 99,
            "name": file_name,
            "browser_download_url": "https://github.com/owner/repo/releases/download/draft/sample-1.0.0/sample.mascot",
        }
        if queue:
            raise queue.pop(0)
        return self.asset

    def get_branch_head_sha(self, token, branch="main") -> str:
        self._record("get_branch_head_sha", branch)
        return "base-sha"

    def get_branch_ref(self, token, branch: str) -> dict | None:
        self._record("get_branch_ref", branch)
        return self.branch

    def create_branch(self, token, branch: str, sha: str) -> dict:
        self.calls.append(("create_branch", (branch,)))
        queue = self.faults.get("create_branch")
        self.branch = {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}
        if queue:
            raise queue.pop(0)
        return self.branch

    def delete_branch_ref(self, token, branch: str) -> None:
        self._record("delete_branch_ref", branch)
        if self.branch is not None:
            self.branch = None

    def get_file(self, token, path: str, ref: str = "main") -> dict | None:
        self._record("get_file", path, ref)
        manifest = self.main_manifest if ref == "main" else self.manifest
        if manifest is None:
            return None
        content = base64.b64encode(
            json.dumps(manifest).encode("utf-8")
        ).decode("ascii")
        return {"content": content, "sha": "manifest-sha"}

    def create_or_update_file(self, token, path, message, content, branch,
                              sha=None) -> dict:
        self._record("create_or_update_file", path, branch)
        self.manifest = json.loads(content)
        return {"content": {"sha": "manifest-sha"}}

    def get_pull_request_by_head(self, token, head: str) -> dict | None:
        self._record("get_pull_request_by_head", head)
        if self.pr is None:
            return None
        return self.pr

    def create_pull_request(self, token, title, head, base, body) -> dict:
        self.calls.append(("create_pull_request", (head,)))
        queue = self.faults.get("create_pull_request")
        self.pr = {"number": 7, "html_url": "https://github.com/owner/repo/pull/7",
                   "head": {"ref": head}}
        if queue:
            raise queue.pop(0)
        return self.pr

    def get_pull_request_files(self, token, pr_number: int) -> list[str]:
        self._record("get_pull_request_files", pr_number)
        return self.pr_files

    def close_pull_request(self, token, pr_number: int) -> None:
        self._record("close_pull_request", pr_number)

    def get_release(self, token, release_id: int) -> dict | None:
        self._record("get_release", release_id)
        return self._release_view()

    def delete_release(self, token, release_id: int) -> None:
        self._record("delete_release", release_id)
        self.release = None


class SubmissionServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mock = MockGitHubServer()
        self.base_url = f"http://127.0.0.1:{self.mock.port}"
        self.old_environ = dict(os.environ)
        os.environ["GITHUB_PUBLISHER_APP_ID"] = "123"
        os.environ["GITHUB_PUBLISHER_INSTALLATION_ID"] = "1"
        os.environ["GITHUB_PUBLISHER_PRIVATE_KEY_PATH"] = str(self.root / "dummy-key.pem")
        os.environ["SUBMISSION_STORAGE_DIR"] = str(self.root / "data")
        os.environ["SUBMISSION_BASE_URL"] = self.base_url
        os.environ["SUBMISSION_ENV"] = "development"
        os.environ["GITHUB_API_BASE"] = f"http://127.0.0.1:{self.mock.port}"
        os.environ["GITHUB_UPLOADS_BASE"] = f"http://127.0.0.1:{self.mock.port}"
        (self.root / "dummy-key.pem").write_text("dummy", encoding="utf-8")
        self.config = Config()
        self.service = SubmissionService(self.config)
        self.service.github.get_installation_token = lambda *a, **k: "installation-token"
        from app import Handler
        Handler.service = self.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.valid = self.root / "valid.mascot"
        write_valid_mascot(self.valid)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.mock.stop()
        os.environ.clear()
        os.environ.update(self.old_environ)
        self.tmp.cleanup()

    def _auth(self, token: str = "user-token") -> str:
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request(
            "POST", "/v1/auth/github", body=b"",
            headers={"Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 200, payload)
        return payload["token"]

    def _post(self, boundary: str, body: bytes, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        merged = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        if headers:
            merged.update(headers)
        connection.request("POST", "/v1/submissions", body=body, headers=merged)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def _submit(self, metadata: dict | None = None, file_path: Path | None = None,
                key: str = "key-1", headers: dict | None = None) -> tuple[int, dict]:
        boundary = f"boundary-{uuid_hex()}"
        session = self._auth()
        merged = {"Authorization": f"Bearer {session}", "X-Idempotency-Key": key}
        if headers:
            merged.update(headers)
        return self._post(
            boundary,
            multipart_body(boundary, metadata or metadata_for(), file_path or self.valid),
            merged,
        )

    def _written_manifest(self) -> dict:
        manifest_writes = [
            entry for entry in self.mock.requests
            if entry["method"] == "PUT" and "/contents/mascots/" in entry["path"]
        ]
        self.assertEqual(len(manifest_writes), 1)
        return json.loads(
            base64.b64decode(
                json.loads(manifest_writes[0]["body"])["content"]
            ).decode("utf-8")
        )

    def test_healthz(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"ok": True})
        connection.close()

    def test_auth_requires_token(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request("POST", "/v1/auth/github", body=b"")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 401)
        self.assertEqual(payload["error"]["code"], "auth_required")

    def test_submission_requires_session_token(self):
        boundary = "boundary-no-session"
        status, payload = self._post(
            boundary,
            multipart_body(boundary, metadata_for(), self.valid),
            {"Authorization": "Bearer user-token"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "auth_invalid")

    def test_successful_submission_flow(self):
        status, payload = self._submit()
        self.assertEqual(status, 201)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["pr"]["number"], 7)
        calls = " ".join(
            f"{entry['method']} {entry['path']}" for entry in self.mock.requests
        )
        self.assertIn("GET /user", calls)
        self.assertTrue(
            any(
                entry["method"] == "POST" and entry["path"].endswith("/pulls")
                for entry in self.mock.requests
            ),
            "PR should be created",
        )
        manifest_writes = [
            entry for entry in self.mock.requests
            if entry["method"] == "PUT" and "/contents/mascots/" in entry["path"]
        ]
        self.assertEqual(len(manifest_writes), 1)
        written = json.loads(
            base64.b64decode(json.loads(manifest_writes[0]["body"])["content"]).decode("utf-8")
        )
        self.assertEqual(written["id"], "sample")
        self.assertEqual(
            written["package"]["sha256"],
            hashlib.sha256(self.valid.read_bytes()).hexdigest(),
        )
        self.assertNotIn("status", written)
        self.assertEqual(written["owner"], {"userId": "1", "login": "octocat"})
        self.assertEqual(written["maintainerUserIds"], ["1"])
        self.assertEqual(written["authors"][0]["githubUserId"], "1")

    def test_github_token_in_metadata_is_rejected(self):
        metadata = metadata_for()
        metadata["githubToken"] = "gh_user_token_1234567890"
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_metadata")

    def test_idempotent_replay_returns_same_submission(self):
        boundary = "boundary-dup"
        body = multipart_body(boundary, metadata_for(), self.valid)
        session = self._auth()
        status1, payload1 = self._post(
            boundary, body,
            {"Authorization": f"Bearer {session}", "X-Idempotency-Key": "same-key"},
        )
        status2, payload2 = self._post(
            boundary, body,
            {"Authorization": f"Bearer {session}", "X-Idempotency-Key": "same-key"},
        )
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertEqual(payload1["id"], payload2["id"])

    def test_invalid_package_is_rejected(self):
        bad = self.root / "bad.mascot"
        write_bad_mascot(bad)
        status, payload = self._submit(file_path=bad)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "package_invalid")

    def test_duplicate_id_version_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
        }
        status, payload = self._submit()
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "duplicate_id_version")

    def test_identity_mismatch_is_rejected(self):
        status, payload = self._submit(metadata=metadata_for(login="other-user"))
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "identity_mismatch")

    def test_non_maintainer_update_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["other"],
            "maintainerUserIds": ["999"],
            "owner": {"userId": "999", "login": "other"},
        }
        status, payload = self._submit(metadata=metadata_for(version="2.0.0"))
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "non_maintainer")

    def test_same_login_different_numeric_id_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["octocat"],
            "maintainerUserIds": ["999"],
            "owner": {"userId": "999", "login": "octocat"},
        }
        status, payload = self._submit(metadata=metadata_for(version="2.0.0"))
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "non_maintainer")

    def test_login_rename_keeps_permission_by_numeric_id(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["oldlogin"],
            "maintainerUserIds": ["1"],
            "owner": {"userId": "1", "login": "oldlogin"},
        }
        metadata = metadata_for(version="2.0.0")
        metadata["maintainers"] = ["oldlogin"]
        status, payload = self._submit(
            metadata=metadata,
        )
        self.assertEqual(status, 201, payload)
        written = self._written_manifest()
        self.assertEqual(written["owner"], {"userId": "1", "login": "octocat"})
        self.assertEqual(written["maintainerUserIds"], ["1"])
        self.assertEqual(written["maintainers"], ["octocat"])
        self.assertEqual(written["authors"][0]["githubLogin"], "octocat")

    def test_other_maintainer_login_cannot_be_changed(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["octocat", "bob"],
            "maintainerUserIds": ["1", "2"],
            "owner": {"userId": "1", "login": "octocat"},
        }
        metadata = metadata_for(version="2.0.0")
        metadata["maintainers"] = ["octocat", "mallory"]
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 403)
        self.assertEqual(
            payload["error"]["code"], "maintainers_change_requires_approval"
        )

    def test_maintainer_set_removal_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["octocat", "bob"],
            "maintainerUserIds": ["1", "2"],
            "owner": {"userId": "1", "login": "octocat"},
        }
        metadata = metadata_for(version="2.0.0")
        metadata["maintainers"] = ["octocat"]
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 403)
        self.assertEqual(
            payload["error"]["code"], "maintainers_change_requires_approval"
        )

    def test_legacy_manifest_without_user_ids_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["octocat"],
            "owner": {"userId": "1", "login": "octocat"},
        }
        status, payload = self._submit(metadata=metadata_for(version="2.0.0"))
        self.assertEqual(status, 403)
        self.assertEqual(
            payload["error"]["code"], "legacy_manifest_no_user_ids"
        )

    def test_login_case_change_is_normalized_from_session(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["OldLogin"],
            "maintainerUserIds": ["1"],
            "owner": {"userId": "1", "login": "OldLogin"},
        }
        metadata = metadata_for(version="2.0.0")
        metadata["maintainers"] = ["oldlogin"]
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 201, payload)
        written = self._written_manifest()
        self.assertEqual(written["owner"], {"userId": "1", "login": "octocat"})
        self.assertEqual(written["maintainers"], ["octocat"])
        self.assertEqual(written["authors"][0]["githubLogin"], "octocat")

    def test_owner_login_refreshed_and_other_author_kept(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["oldlogin", "bob"],
            "maintainerUserIds": ["1", "2"],
            "owner": {"userId": "1", "login": "oldlogin"},
        }
        metadata = metadata_for(version="2.0.0")
        metadata["maintainers"] = ["oldlogin", "bob"]
        metadata["authors"] = [
            {
                "githubLogin": "octocat",
                "githubUserId": "1",
                "displayName": "Octo Cat",
            },
            {"githubLogin": "bob", "displayName": "Bob"},
        ]
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 201, payload)
        written = self._written_manifest()
        self.assertEqual(written["owner"], {"userId": "1", "login": "octocat"})
        self.assertEqual(written["maintainers"], ["octocat", "bob"])
        self.assertEqual(written["maintainerUserIds"], ["1", "2"])
        self.assertEqual(written["authors"][0]["githubLogin"], "octocat")
        self.assertEqual(written["authors"][1]["githubLogin"], "bob")

    def test_version_downgrade_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "2.0.0",
            "maintainers": ["octocat"],
            "maintainerUserIds": ["1"],
            "owner": {"userId": "1", "login": "octocat"},
        }
        status, payload = self._submit(metadata=metadata_for(version="1.5.0"))
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "version_not_higher")

    def test_self_added_maintainer_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
            "maintainers": ["octocat"],
            "maintainerUserIds": ["1"],
            "owner": {"userId": "1", "login": "octocat"},
        }
        metadata = metadata_for(version="2.0.0")
        metadata["maintainers"] = ["octocat", "other"]
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "maintainers_change_requires_approval")

    def test_owner_field_tamper_is_rejected(self):
        metadata = metadata_for()
        metadata["owner"] = {"userId": "999", "login": "evil"}
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "invalid_metadata")

    def test_author_without_numeric_user_id_is_rejected(self):
        metadata = metadata_for()
        del metadata["authors"][0]["githubUserId"]
        status, payload = self._submit(metadata=metadata)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "invalid_metadata")

    def test_malicious_ids_are_rejected(self):
        self.service.rate_limiter.limit = 100
        self.service.auth_rate_limiter.limit = 100
        for bad_id in [
            ".github/workflows",
            "../evil",
            "..%2Fevil",
            "evil\\path",
            "evil\u2215path",
            "evil\uFF0Fpath",
            "A" * 65,
            "",
            "con",
            "tools",
            ".github",
            "Bad_ID",
            "mascots",
        ]:
            with self.subTest(bad_id=bad_id):
                status, payload = self._submit(metadata=metadata_for(mid=bad_id))
                self.assertEqual(status, 422, payload)

    def test_untrusted_changed_files_close_and_clean(self):
        self.mock.changed_files = [
            ".github/workflows/evil.yml",
            "mascots/sample/manifest.json",
        ]
        status, payload = self._submit()
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "untrusted_changes")
        calls = " ".join(
            f"{entry['method']} {entry['path']}" for entry in self.mock.requests
        )
        self.assertTrue(
            any(
                entry["method"] == "PATCH" and "/pulls/7" in entry["path"]
                for entry in self.mock.requests
            ),
            "untrusted PR should be closed",
        )
        self.assertTrue(
            any(
                entry["method"] == "DELETE" and "/releases/42" in entry["path"]
                for entry in self.mock.requests
            ),
            "draft release should be deleted",
        )
        self.assertTrue(
            any("DELETE" in entry["method"] and "/git/ref/heads" in entry["path"]
                for entry in self.mock.requests)
        )

    def test_rate_limit(self):
        os.environ["RATE_LIMIT_SUBMISSIONS_PER_MINUTE"] = "1"
        limited_config = Config()
        from app import Handler
        previous = Handler.service
        limited_service = SubmissionService(limited_config)
        limited_service.github.get_installation_token = lambda *a, **k: "installation-token"
        Handler.service = limited_service
        try:
            status1, _ = self._submit()
            status2, payload2 = self._submit()
            self.assertEqual(status1, 201)
            self.assertEqual(status2, 429)
            self.assertEqual(payload2["error"]["code"], "rate_limited")
        finally:
            Handler.service = previous

    def test_delete_requires_authorization(self):
        _, payload = self._submit()
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request("DELETE", f"/v1/submissions/{payload['id']}")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_delete_with_session_token_cleans_up(self):
        _, payload = self._submit()
        session = self._auth()
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request(
            "DELETE", f"/v1/submissions/{payload['id']}",
            headers={"Authorization": f"Bearer {session}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "cancelled")

    def test_redaction(self):
        text = (
            "Authorization: Bearer gh_token_abcdef123456; "
            "access_token=\"secret1234567890\"; "
            "https://example.com/oauth?access_token=querytoken1234567890&x=1"
        )
        redacted = redact_text(text)
        self.assertNotIn("gh_token_abcdef123456", redacted)
        self.assertNotIn("secret1234567890", redacted)
        self.assertNotIn("querytoken1234567890", redacted)
        self.assertIn("[REDACTED]", redacted)
        headers = {"Authorization": "Bearer xyz", "Cookie": "session=abc", "X-Id": "7"}
        redacted_headers = redact_headers(headers)
        self.assertEqual(redacted_headers["Authorization"], "[REDACTED]")
        self.assertEqual(redacted_headers["Cookie"], "[REDACTED]")
        self.assertEqual(redacted_headers["X-Id"], "7")


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


class FaultInjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_environ = dict(os.environ)
        os.environ["GITHUB_PUBLISHER_APP_ID"] = "123"
        os.environ["GITHUB_PUBLISHER_INSTALLATION_ID"] = "1"
        os.environ["GITHUB_PUBLISHER_PRIVATE_KEY_PATH"] = str(self.root / "dummy-key.pem")
        os.environ["SUBMISSION_STORAGE_DIR"] = str(self.root / "data")
        os.environ["SUBMISSION_ENV"] = "development"
        (self.root / "dummy-key.pem").write_text("dummy", encoding="utf-8")
        self.config = Config()
        self.service = SubmissionService(self.config)
        self.fake = FakeGitHub()
        self.service.github = self.fake  # type: ignore[assignment]
        self.valid = self.root / "valid.mascot"
        write_valid_mascot(self.valid)
        self.session = self.service.sessions.issue("octocat", "1")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_environ)
        self.tmp.cleanup()

    def _parsed(self, metadata: dict | None = None) -> object:
        class Uploaded:
            file_name = "sample.mascot"
            content_type = "application/octet-stream"
            size = 0
            temp_path = self.valid
        class Fields:
            def __init__(self, text):
                self.text = text
        class Parsed:
            fields = {"metadata": json.dumps(metadata or metadata_for())}
            files = {"file": Uploaded()}
        return Parsed()

    def _submit(self, key: str = "fault-key", metadata: dict | None = None):
        return self.service.create_submission(
            self._parsed(metadata), "127.0.0.1", key,
            f"Bearer {self.session}",
        )

    def test_release_timeout_then_resume(self):
        self.fake.fail_next("create_draft_release")
        with self.assertRaises(ServiceError) as ctx:
            self._submit("resume-key")
        self.assertEqual(ctx.exception.status, 502)
        stored = self.service.store.load(
            self.service._find_by_idempotency(
                hashlib.sha256(b"resume-key").hexdigest()
            )["id"]
        )
        self.assertNotEqual(stored["status"], "failed")
        self.assertNotIn("release", stored["steps"])
        self.assertEqual(
            sum(1 for name, _ in self.fake.calls if name == "create_draft_release"),
            1,
        )
        result, replayed = self._submit("resume-key")
        self.assertEqual(result["status"], "pending")
        self.assertTrue(replayed)
        self.assertEqual(
            sum(1 for name, _ in self.fake.calls if name == "create_draft_release"),
            1,
        )

    def test_asset_timeout_then_resume(self):
        self.fake.fail_next("upload_release_asset")
        with self.assertRaises(ServiceError):
            self._submit("asset-key")
        result, _ = self._submit("asset-key")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            sum(1 for name, _ in self.fake.calls if name == "upload_release_asset"),
            1,
        )

    def test_branch_500_then_resume(self):
        self.fake.fail_next("create_branch", status=500, code="github_api_error")
        with self.assertRaises(ServiceError):
            self._submit("branch-key")
        result, _ = self._submit("branch-key")
        self.assertEqual(result["status"], "pending")

    def test_pr_timeout_then_resume(self):
        self.fake.fail_next("create_pull_request")
        with self.assertRaises(ServiceError):
            self._submit("pr-key")
        result, _ = self._submit("pr-key")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["pr"]["number"], 7)

    def test_non_retryable_failure_compensates(self):
        self.fake.fail_next(
            "get_branch_head_sha", status=422, code="github_api_error"
        )
        with self.assertRaises(ServiceError):
            self._submit("fail-key")
        stored_id = self.service._find_by_idempotency(
            hashlib.sha256(b"fail-key").hexdigest()
        )["id"]
        stored = self.service.store.load(stored_id)
        self.assertEqual(stored["status"], "failed")
        self.assertIn("delete_release", [name for name, _ in self.fake.calls])

    def test_concurrent_same_submission(self):
        results: list = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                result, replayed = self._submit("concurrent-key")
                results.append((result["id"], replayed))
            except Exception as exc:  # noqa: BLE001
                results.append(("error", str(exc)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(len(results), 2)
        ids = {item[0] for item in results}
        self.assertEqual(len(ids), 1, results)
        self.assertEqual(
            sum(1 for name, _ in self.fake.calls if name == "create_draft_release"),
            1,
        )

    def test_double_delete_is_safe(self):
        result, _ = self._submit("delete-key")
        self.service.delete_submission(result["id"], f"Bearer {self.session}")
        self.service.delete_submission(result["id"], f"Bearer {self.session}")
        stored = self.service.store.load(result["id"])
        self.assertEqual(stored["status"], "cancelled")
        self.assertLessEqual(
            sum(1 for name, _ in self.fake.calls if name == "delete_release"), 1
        )

    def test_late_cleanup_after_merge_keeps_release(self):
        result, _ = self._submit("merged-key")
        self.fake.release_draft = False
        self.fake.pr = None  # merged PR is closed, so by-head lookup finds none
        self.service.delete_submission(result["id"], f"Bearer {self.session}")
        self.assertEqual(
            sum(1 for name, _ in self.fake.calls if name == "delete_release"), 0
        )
        stored = self.service.store.load(result["id"])
        self.assertEqual(stored["status"], "cancelled")


def make_validator_wrapper(root: Path, body: str, name: str = "validator") -> Path:
    if platform.system() == "Windows":
        path = root / f"{name}.cmd"
        path.write_text(f"@echo off\r\n{body}\r\n", encoding="utf-8")
    else:
        path = root / f"{name}.sh"
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ProductionValidatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_environ = dict(os.environ)
        os.environ["SUBMISSION_ENV"] = "production"
        os.environ["SUBMISSION_STORAGE_DIR"] = str(self.root / "data")
        os.environ["SUBMISSION_SESSION_SECRET"] = "a" * 64
        self.valid = self.root / "valid.mascot"
        write_valid_mascot(self.valid)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_environ)
        self.tmp.cleanup()

    def _config(self, cli: str) -> Config:
        os.environ["VALIDATOR_CLI"] = cli
        return Config()

    def test_production_requires_validator_cli(self):
        os.environ["VALIDATOR_CLI"] = ""
        config = Config()
        with self.assertRaises(RuntimeError):
            app_module.validate_production_config(config)

    def test_production_rejects_missing_path(self):
        config = self._config(str(self.root / "does-not-exist.exe"))
        with self.assertRaises(RuntimeError):
            app_module.validate_production_config(config)

    def test_production_rejects_non_executable(self):
        wrapper = make_validator_wrapper(self.root, 'echo {"ok": true}')
        if platform.system() == "Windows":
            self.skipTest("Windows os.access X_OK is not meaningful")
        wrapper.chmod(wrapper.stat().st_mode & ~stat.S_IXUSR)
        config = self._config(str(wrapper))
        with self.assertRaises(RuntimeError):
            app_module.validate_production_config(config)

    def test_production_self_check_rejects_bad_validator(self):
        wrapper = make_validator_wrapper(self.root, "exit 1")
        config = self._config(str(wrapper))
        with self.assertRaises(RuntimeError):
            app_module.validate_production_config(config)

    def test_production_self_check_accepts_validator(self):
        wrapper = make_validator_wrapper(
            self.root, 'echo {"ok": true, "errors": []}'
        )
        config = self._config(str(wrapper))
        app_module.validate_production_config(config)

    def test_validator_timeout_fails_closed(self):
        if platform.system() == "Windows":
            code = "ping -n 30 127.0.0.1 > nul"
        else:
            code = "sleep 30"
        wrapper = make_validator_wrapper(self.root, code)
        ok, errors = run_external_validator(self.valid, str(wrapper), timeout_seconds=1)
        self.assertFalse(ok)
        self.assertTrue(any("timed out" in error for error in errors))

    def test_validator_exit_codes_fail_closed(self):
        for code in ("exit 1", "exit 2"):
            with self.subTest(code=code):
                wrapper = make_validator_wrapper(self.root, code)
                ok, errors = run_external_validator(self.valid, str(wrapper))
                self.assertFalse(ok)
                self.assertTrue(
                    any("exited with code" in error for error in errors)
                )

    def test_validator_invalid_json_fails_closed(self):
        wrapper = make_validator_wrapper(self.root, "echo not-json")
        ok, errors = run_external_validator(self.valid, str(wrapper))
        self.assertFalse(ok)
        self.assertTrue(any("malformed JSON" in error for error in errors))

    def test_validator_ok_false_fails_closed(self):
        wrapper = make_validator_wrapper(
            self.root, 'echo {"ok": false, "errors": ["bad package"]}'
        )
        ok, errors = run_external_validator(self.valid, str(wrapper))
        self.assertFalse(ok)
        self.assertEqual(errors, ["bad package"])

    def test_validator_huge_output_fails_closed(self):
        if platform.system() == "Windows":
            code = "powershell -NoProfile -Command \"1..30000 | ForEach-Object { 'x' * 60 }\""
        else:
            code = "head -c 2000000 /dev/zero | tr '\\0' 'x'"
        wrapper = make_validator_wrapper(self.root, code)
        ok, errors = run_external_validator(self.valid, str(wrapper))
        self.assertFalse(ok)
        self.assertTrue(any("size limit" in error for error in errors))

    def test_validator_crash_fails_closed(self):
        if platform.system() == "Windows":
            code = f'"{sys.executable}" -c "import os; os.abort()"'
        else:
            code = "kill -SEGV $$"
        wrapper = make_validator_wrapper(self.root, code)
        ok, errors = run_external_validator(self.valid, str(wrapper))
        self.assertFalse(ok)

    def test_validator_output_is_sanitized(self):
        wrapper = make_validator_wrapper(
            self.root,
            'echo {"ok": false, "errors": ["bad \\u001b[31mpath\\u001b[0m ../evil"]}',
        )
        ok, errors = run_external_validator(self.valid, str(wrapper))
        self.assertFalse(ok)
        self.assertTrue(all("\x1b" not in error for error in errors))

    def test_production_never_falls_back_without_validator(self):
        config = self._config("")
        from package_checks import validate_mascot
        ok, errors = validate_mascot(
            self.valid, config.validator_cli, "production", 5
        )
        self.assertFalse(ok)
        self.assertTrue(any("production mode requires" in error for error in errors))

    def test_production_requires_session_secret(self):
        os.environ.pop("SUBMISSION_SESSION_SECRET", None)
        with self.assertRaises(ValueError):
            Config()

    def test_production_rejects_short_session_secret(self):
        os.environ["SUBMISSION_SESSION_SECRET"] = "a" * 32  # 16 bytes after hex decode
        with self.assertRaises(ValueError):
            Config()

    def test_production_rejects_invalid_session_secret_encoding(self):
        os.environ["SUBMISSION_SESSION_SECRET"] = "!" * 40
        with self.assertRaises(ValueError):
            Config()

    def test_production_accepts_hex_and_base64_secret(self):
        os.environ["SUBMISSION_SESSION_SECRET"] = "a" * 64
        self.assertEqual(len(Config().session_secret), 32)
        os.environ["SUBMISSION_SESSION_SECRET"] = base64.b64encode(b"x" * 32).decode()
        self.assertEqual(len(Config().session_secret), 32)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _craft_token(payload: dict, header: dict | None = None,
                 secret: bytes = b"x" * 32) -> str:
    header = header or {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = header_b64 + "." + body_b64
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(signature)


def _default_payload(**overrides) -> dict:
    now = int(time.time())
    payload = {
        "sub": "1",
        "login": "octocat",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "aud": "neurolingsce-submission",
        "iss": "neurolingsce-submission-service",
        "jti": "token-id-1",
    }
    payload.update(overrides)
    return payload


class SessionSecurityTest(unittest.TestCase):
    SECRET = b"s" * 32

    def setUp(self):
        self.manager = app_module.SessionManager(self.SECRET, 300)

    def test_valid_token_verifies(self):
        token = self.manager.issue("octocat", "42")
        claims = self.manager.verify(token)
        self.assertIsNotNone(claims)
        self.assertEqual(claims["login"], "octocat")
        self.assertEqual(claims["sub"], "42")
        self.assertTrue(claims["jti"])

    def test_tampered_payload_fails(self):
        token = self.manager.issue("octocat", "42")
        header_b64, body_b64, _ = token.split(".")
        payload = json.loads(_b64url_decode(body_b64).decode("utf-8"))
        payload["login"] = "attacker"
        forged = header_b64 + "." + _b64url(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ) + "." + token.split(".")[2]
        self.assertIsNone(self.manager.verify(forged))

    def test_wrong_signature_fails(self):
        token = self.manager.issue("octocat", "42")
        header_b64, body_b64, _ = token.split(".")
        other = _craft_token(
            json.loads(_b64url_decode(body_b64).decode("utf-8")),
            json.loads(_b64url_decode(header_b64).decode("utf-8")),
            secret=b"y" * 32,
        )
        self.assertIsNone(self.manager.verify(other))

    def test_expired_token_fails(self):
        token = _craft_token(_default_payload(exp=int(time.time()) - 1))
        self.assertIsNone(self.manager.verify(token))

    def test_not_yet_valid_token_fails(self):
        token = _craft_token(_default_payload(nbf=int(time.time()) + 120))
        self.assertIsNone(self.manager.verify(token))

    def test_wrong_audience_fails(self):
        token = _craft_token(_default_payload(aud="other-audience"))
        self.assertIsNone(self.manager.verify(token))

    def test_wrong_issuer_fails(self):
        token = _craft_token(_default_payload(iss="other-issuer"))
        self.assertIsNone(self.manager.verify(token))

    def test_algorithm_tamper_fails(self):
        token = _craft_token(
            _default_payload(), header={"alg": "none", "typ": "JWT"}
        )
        self.assertIsNone(self.manager.verify(token))

    def test_production_token_survives_restart(self):
        os.environ["SUBMISSION_ENV"] = "production"
        os.environ["SUBMISSION_SESSION_SECRET"] = "b" * 64
        first = app_module.SessionManager(Config().session_secret, 300)
        second = app_module.SessionManager(Config().session_secret, 300)
        token = first.issue("octocat", "42")
        self.assertIsNotNone(second.verify(token))

    def test_dev_ephemeral_token_does_not_survive_restart(self):
        os.environ["SUBMISSION_ENV"] = "development"
        os.environ.pop("SUBMISSION_SESSION_SECRET", None)
        first = app_module.SessionManager(Config().session_secret, 300)
        second = app_module.SessionManager(Config().session_secret, 300)
        token = first.issue("octocat", "42")
        self.assertIsNone(second.verify(token))


class ClientPaginationTest(unittest.TestCase):
    def setUp(self):
        import github_client as gc_module
        self._gc_module = gc_module
        self._old_allow_insecure = gc_module._ALLOW_INSECURE_LINKS
        gc_module._ALLOW_INSECURE_LINKS = True
        self.routes: dict[str, tuple] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self._server.routes = self.routes
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.old_api_base = os.environ.get("GITHUB_API_BASE")
        os.environ["GITHUB_API_BASE"] = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.client = GitHubClient("owner", "repo")

    def tearDown(self):
        self._gc_module._ALLOW_INSECURE_LINKS = self._old_allow_insecure
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self.old_api_base is None:
            os.environ.pop("GITHUB_API_BASE", None)
        else:
            os.environ["GITHUB_API_BASE"] = self.old_api_base

    def _handler(self, *args, **kwargs):
        return _PaginationHandler(*args, **kwargs)

    def _base(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def test_release_assets_follows_link(self):
        base = self._base()
        self.routes.clear()
        self.routes.update({
            "/assets?per_page=100": (
                [{"id": 1}, {"id": 2}],
                f'<{base}/repos/owner/repo/releases/9/assets?per_page=100&page=2>; rel="next"',
            ),
            "/assets?per_page=100&page=2": ([{"id": 3}], None),
        })
        assets = self.client.get_release_assets("t", 9)
        self.assertEqual([item["id"] for item in assets], [1, 2, 3])

    def test_pull_request_files_follows_link(self):
        base = self._base()
        self.routes.clear()
        self.routes.update({
            "/pulls/7/files?per_page=100": (
                [{"filename": "mascots/sample/manifest.json"}],
                f'<{base}/repos/owner/repo/pulls/7/files?per_page=100&page=2>; rel="next"',
            ),
            "/pulls/7/files?per_page=100&page=2": (
                [{"filename": "mascots/sample/extra.json"}],
                None,
            ),
        })
        files = self.client.get_pull_request_files("t", 7)
        self.assertEqual(
            files, ["mascots/sample/manifest.json", "mascots/sample/extra.json"]
        )

    def test_pull_request_search_across_pages(self):
        base = self._base()
        self.routes.clear()
        self.routes.update({
            "/pulls?state=open": (
                [],
                f'<{base}/repos/owner/repo/pulls?state=open&per_page=100&page=2>; rel="next"',
            ),
            "/pulls?state=open&per_page=100&page=2": (
                [{"number": 7, "head": {"ref": "submission/sample-1.0.0"}}],
                None,
            ),
        })
        pr = self.client.get_pull_request_by_head("t", "submission/sample-1.0.0")
        self.assertEqual(pr["number"], 7)

    def test_link_cycle_fails_closed(self):
        base = self._base()
        self.routes.clear()
        self.routes.update({
            "/assets?per_page=100": (
                [{"id": 1}],
                f'<{base}/repos/owner/repo/releases/9/assets?per_page=100>; rel="next"',
            ),
        })
        with self.assertRaises(GitHubApiError):
            self.client.get_release_assets("t", 9)

    def test_second_page_failure_fails_closed(self):
        base = self._base()
        self.routes.clear()
        self.routes.update({
            "/assets?per_page=100": (
                [{"id": 1}],
                f'<{base}/repos/owner/repo/releases/9/assets?per_page=100&page=2>; rel="next"',
            ),
            "/assets?per_page=100&page=2": "error",
        })
        with self.assertRaises(GitHubApiError):
            self.client.get_release_assets("t", 9)


class _PaginationHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _handle(self):
        routes = self.server.routes
        candidates = [
            (key, value)
            for key, value in routes.items()
            if self.command == "GET" and key in self.path
        ]
        if not candidates:
            self.send_response(404)
            self.end_headers()
            return
        key, value = max(candidates, key=lambda item: len(item[0]))
        if value == "error":
            self.send_response(500)
            self.end_headers()
            return
        body, link = value
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle


if __name__ == "__main__":
    unittest.main()
