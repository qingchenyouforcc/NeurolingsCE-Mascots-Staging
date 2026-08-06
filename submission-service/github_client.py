"""GitHub REST client for the submission service.

The service talks to GitHub with a GitHub App installation token for official
repository operations. A user's access token is used only to verify identity
via ``GET /user`` and is never persisted or logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

class GitHubApiError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt_rs256(private_key_pem: str, payload: bytes) -> bytes:
    """Sign a JWT payload using the app private key.

    Prefers the ``cryptography`` package; falls back to system ``openssl``.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        return key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    except ImportError:
        pass

    openssl = os.environ.get("OPENSSL_BIN", "openssl")
    with tempfile.TemporaryDirectory(prefix="neurolingsce-jwt-") as tmp:
        key_path = Path(tmp) / "app-key.pem"
        payload_path = Path(tmp) / "payload.bin"
        signature_path = Path(tmp) / "signature.bin"
        key_path.write_text(private_key_pem, encoding="utf-8")
        payload_path.write_bytes(payload)
        result = subprocess.run(
            [openssl, "dgst", "-sha256", "-sign", str(key_path), "-out", str(signature_path), str(payload_path)],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise GitHubApiError(
                "github_jwt_unavailable",
                "JWT signing requires 'cryptography' or a system 'openssl' binary",
            )
        return signature_path.read_bytes()


def create_app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    signature = sign_jwt_rs256(private_key_pem, signing_input.encode("ascii"))
    return signing_input + "." + _b64url(signature)


class GitHubClient:
    def __init__(self, owner: str, repo: str):
        self.owner = owner
        self.repo = repo
        self.api_base = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
        self.uploads_base = os.environ.get(
            "GITHUB_UPLOADS_BASE", "https://uploads.github.com"
        )

    def _request(self, method: str, url: str, token: str, payload=None,
                 raw: bytes | None = None, content_type: str | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = raw
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type is not None and data is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            try:
                parsed = json.loads(detail)
                message = parsed.get("message", detail)
            except json.JSONDecodeError:
                message = detail
            raise GitHubApiError("github_api_error", message, exc.code) from exc

    def get_user(self, access_token: str) -> dict:
        return self._request("GET", f"{self.api_base}/user", access_token)

    def get_installation_token(self, app_id: str, installation_id: str,
                               private_key_pem: str) -> str:
        jwt = create_app_jwt(app_id, private_key_pem)
        url = f"{self.api_base}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        request = urllib.request.Request(
            url, data=b"", headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GitHubApiError(
                "github_installation_token_failed",
                exc.read().decode("utf-8", "replace")[:500],
                exc.code,
            ) from exc
        token = body.get("token", "")
        if not token:
            raise GitHubApiError(
                "github_installation_token_failed",
                "installation token response did not contain a token",
            )
        return token

    def create_draft_release(self, token: str, tag: str, name: str, body: str) -> dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases"
        return self._request(
            "POST",
            url,
            token,
            {
                "tag_name": tag,
                "name": name,
                "body": body,
                "draft": True,
            },
        )

    def upload_release_asset(self, token: str, release_id: int, file_path: Path,
                             file_name: str, content_type: str = "application/octet-stream") -> dict:
        query = urllib.parse.urlencode({"name": file_name})
        url = f"{self.uploads_base}/repos/{self.owner}/{self.repo}/releases/{release_id}/assets?{query}"
        raw = file_path.read_bytes()
        return self._request("POST", url, token, raw=raw, content_type=content_type)

    def get_branch_head_sha(self, token: str, branch: str = "main") -> str:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/branches/{urllib.parse.quote(branch)}"
        data = self._request("GET", url, token)
        sha = data.get("commit", {}).get("sha", "")
        if not sha:
            raise GitHubApiError("github_branch_not_found", f"branch {branch!r} has no commit sha")
        return sha

    def create_branch(self, token: str, branch: str, sha: str) -> dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/git/refs"
        return self._request(
            "POST", url, token, {"ref": f"refs/heads/{branch}", "sha": sha}
        )

    def create_or_update_file(self, token: str, path: str, message: str,
                              content: str, branch: str, sha: str | None = None) -> dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/contents/{path}"
        payload: dict = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return self._request("PUT", url, token, payload)

    def create_pull_request(self, token: str, title: str, head: str, base: str,
                            body: str) -> dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls"
        return self._request(
            "POST",
            url,
            token,
            {"title": title, "head": head, "base": base, "body": body},
        )

    def delete_release(self, token: str, release_id: int) -> None:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/{release_id}"
        self._request("DELETE", url, token)

    def get_file(self, token: str, path: str, ref: str = "main") -> dict | None:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/contents/{path}?ref={urllib.parse.quote(ref)}"
        try:
            return self._request("GET", url, token)
        except GitHubApiError as exc:
            if exc.status == 404:
                return None
            raise

    def close_pull_request(self, token: str, pr_number: int) -> None:
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/pulls/{pr_number}"
        self._request("PATCH", url, token, {"state": "closed"})
