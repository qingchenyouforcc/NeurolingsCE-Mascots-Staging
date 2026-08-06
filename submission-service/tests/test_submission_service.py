from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import Config, SubmissionService  # noqa: E402
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
                 version: str = "1.0.0", token: str = "user-token") -> dict:
    return {
        "id": mid,
        "name": "Sample",
        "version": version,
        "summary": "A test mascot",
        "description": "Longer description for the test mascot.",
        "authors": [{"githubLogin": login, "displayName": "Octo Cat"}],
        "maintainers": [login],
        "license": "MIT",
        "isDerivative": False,
        "tags": ["test"],
        "categories": ["test"],
        "minimumNeurolingsCEVersion": "0.5.1",
        "githubToken": token,
    }


class MockGitHubServer:
    def __init__(self):
        self.requests: list[dict] = []
        self.existing_manifest: dict | None = None
        self.user_login = "octocat"
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

        if self.path == "/user" and self.command in ("GET", "POST"):
            return self._json(200, {"login": mock.user_login})
        if self.path.endswith("/access_tokens") and self.command == "POST":
            return self._json(201, {"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        if "/releases" in self.path and self.command == "POST" and "assets" not in self.path:
            return self._json(201, {"id": 42, "tag_name": "draft/sample-1.0.0", "draft": True})
        if "/assets?" in self.path and self.command == "POST":
            return self._json(201, {
                "id": 99,
                "browser_download_url": "https://github.com/owner/repo/releases/download/draft/sample-1.0.0/sample.mascot",
            })
        if self.path.endswith("/branches/main") and self.command == "GET":
            return self._json(200, {"commit": {"sha": "base-sha"}})
        if self.path.endswith("/git/refs") and self.command == "POST":
            return self._json(201, {"ref": json.loads(body or b"{}").get("ref", "")})
        if "/contents/mascots/" in self.path and self.command == "PUT":
            return self._json(201, {"content": {"sha": "manifest-sha"}})
        if "/pulls" in self.path and self.command == "POST":
            return self._json(201, {"number": 7, "html_url": "https://github.com/owner/repo/pull/7"})
        if "/pulls/" in self.path and self.command == "PATCH":
            return self._json(200, {"state": "closed"})
        if "/releases/" in self.path and self.command == "DELETE":
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

    def _json(self, status: int, obj: dict):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()


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


class SubmissionServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mock = MockGitHubServer()
        self.base_url = f"http://127.0.0.1:{self.mock.port}"
        self.old_environ = dict(os.environ)
        os.environ["GITHUB_APP_ID"] = "123"
        os.environ["GITHUB_APP_INSTALLATION_ID"] = "1"
        os.environ["GITHUB_APP_PRIVATE_KEY_PATH"] = str(self.root / "dummy-key.pem")
        os.environ["SUBMISSION_STORAGE_DIR"] = str(self.root / "data")
        os.environ["SUBMISSION_BASE_URL"] = self.base_url
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

    def test_healthz(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"ok": True})
        connection.close()

    def test_successful_submission_flow(self):
        boundary = "test-boundary-1"
        status, payload = self._post(
            boundary,
            multipart_body(boundary, metadata_for(), self.valid),
            {"X-Idempotency-Key": "key-1"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["pr"]["number"], 7)
        methods = [entry["method"] for entry in self.mock.requests]
        self.assertIn("GET /user", " ".join(f"{entry['method']} {entry['path']}" for entry in self.mock.requests))
        self.assertTrue(
            any("/pulls" in entry["path"] for entry in self.mock.requests),
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
        self.assertEqual(written["package"]["sha256"], hashlib.sha256(self.valid.read_bytes()).hexdigest())

    def test_idempotent_replay_returns_same_submission(self):
        boundary = "test-boundary-2"
        body = multipart_body(boundary, metadata_for(), self.valid)
        status1, payload1 = self._post(boundary, body, {"X-Idempotency-Key": "same-key"})
        status2, payload2 = self._post(boundary, body, {"X-Idempotency-Key": "same-key"})
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertEqual(payload1["id"], payload2["id"])

    def test_invalid_package_is_rejected(self):
        bad = self.root / "bad.mascot"
        write_bad_mascot(bad)
        boundary = "test-boundary-3"
        status, payload = self._post(boundary, multipart_body(boundary, metadata_for(), bad))
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "package_invalid")

    def test_duplicate_id_version_is_rejected(self):
        self.mock.existing_manifest = {
            "id": "sample",
            "version": "1.0.0",
        }
        boundary = "test-boundary-4"
        status, payload = self._post(boundary, multipart_body(boundary, metadata_for(), self.valid))
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "duplicate_id_version")

    def test_identity_mismatch_is_rejected(self):
        boundary = "test-boundary-5"
        metadata = metadata_for(login="other-user")
        status, payload = self._post(boundary, multipart_body(boundary, metadata, self.valid))
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "identity_mismatch")

    def test_rate_limit(self):
        os.environ["RATE_LIMIT_SUBMISSIONS_PER_MINUTE"] = "1"
        limited_config = Config()
        from app import Handler
        previous = Handler.service
        limited_service = SubmissionService(limited_config)
        limited_service.github.get_installation_token = lambda *a, **k: "installation-token"
        Handler.service = limited_service
        try:
            boundary = "test-boundary-6"
            body = multipart_body(boundary, metadata_for(), self.valid)
            status1, _ = self._post(boundary, body)
            status2, payload2 = self._post(boundary, body)
            self.assertEqual(status1, 201)
            self.assertEqual(status2, 429)
            self.assertEqual(payload2["error"]["code"], "rate_limited")
        finally:
            Handler.service = previous

    def test_delete_requires_authorization(self):
        boundary = "test-boundary-7"
        _, payload = self._post(boundary, multipart_body(boundary, metadata_for(), self.valid))
        submission_id = payload["id"]
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request("DELETE", f"/v1/submissions/{submission_id}")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_delete_with_user_token_cleans_up(self):
        boundary = "test-boundary-8"
        _, payload = self._post(boundary, multipart_body(boundary, metadata_for(), self.valid))
        submission_id = payload["id"]
        connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=15)
        connection.request(
            "DELETE",
            f"/v1/submissions/{submission_id}",
            headers={"Authorization": "Bearer user-token"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "cancelled")
        self.assertTrue(
            any(entry["method"] == "DELETE" and "/releases/" in entry["path"]
                for entry in self.mock.requests)
        )

    def test_redaction(self):
        text = 'Authorization: Bearer gh_token_abcdef123456; access_token="secret1234567890"'
        redacted = redact_text(text)
        self.assertNotIn("gh_token_abcdef123456", redacted)
        self.assertNotIn("secret1234567890", redacted)
        self.assertIn("[REDACTED]", redacted)
        headers = {"Authorization": "Bearer xyz", "Cookie": "session=abc", "X-Id": "7"}
        redacted_headers = redact_headers(headers)
        self.assertEqual(redacted_headers["Authorization"], "[REDACTED]")
        self.assertEqual(redacted_headers["Cookie"], "[REDACTED]")
        self.assertEqual(redacted_headers["X-Id"], "7")


if __name__ == "__main__":
    unittest.main()
